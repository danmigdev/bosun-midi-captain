"""TFT display driver - multi-label rendering driven by layout config.

The display state is the (context, layout) pair:
  - `context` is a dict of named field values. The core owns patch_name,
    bank and slot; plugins write their own fields via update_context() and
    on_midi_in() hooks.
  - `layout` is a list of label specs read from the profile's device.json:
        [{"field": "patch_name", "x": 20, "y": 40, "size": 3,
          "color": "#ffffff", "prefix": "", "suffix": ""}, ...]
    The special ``expression_mode`` field uses the same positioning/style
    properties but renders a volume ramp or a side-view Wah pedal beside its
    mode. The unknown ``---`` state has no icon.

The ordinary screen retains its labels across frames. System-font text uses
the built-in glyph atlas directly, so changing rigs needs no per-glyph objects
and never builds a second complete screen on top of the first.
"""
import busio
import displayio
import fourwire
import gc
import math
import pwmio
import terminalio

from adafruit_st7789 import ST7789

from . import VERSION
from .board import SPI_CLK, SPI_MOSI, TFT_CS, TFT_DC, TFT_PWM


# Loaded font cache keyed by filename. terminalio.FONT is always available
# as "system" (or any falsy/empty value).
_FONT_CACHE = {"system": terminalio.FONT}


def _load_font(name):
    """Return a font object for the given name. Falls back to terminalio
    if the file is missing or unloadable. Cached for re-use across
    renders (BDF loads are not free)."""
    if not name or name == "system":
        return terminalio.FONT
    if name in _FONT_CACHE:
        return _FONT_CACHE[name]
    try:
        from adafruit_bitmap_font import bitmap_font
        font = bitmap_font.load_font("/fonts/" + name)
        _FONT_CACHE[name] = font
        return font
    except Exception as e:
        print("font load failed:", name, "->", e)
        _FONT_CACHE[name] = terminalio.FONT
        return terminalio.FONT


# Fallback when a layout doesn't specify color.
_DEFAULT_COLOR = 0xFFFFFF


def _parse_color(c, default=_DEFAULT_COLOR):
    if isinstance(c, int):
        return c
    if not isinstance(c, str) or not c.startswith("#"):
        return default
    try:
        return int(c.lstrip("#"), 16)
    except ValueError:
        return default


_HALIGN = {"left": 0.0, "center": 0.5, "right": 1.0}
_VALIGN = {"top":  0.0, "center": 0.5, "bottom": 1.0}

# Screen size - used to compute the anchor's absolute pixel position.
_SCREEN_W = 240
_SCREEN_H = 240


class _SystemLabel(displayio.Group):
    """Fixed-capacity text using terminalio's shared ROM glyph atlas.

    ASCII updates change tile indices only. Keep the same public positioning
    surface as Label so custom fonts can still use Adafruit's implementation.
    """
    def __init__(self, font, text="", color=0xFFFFFF, scale=1, x=0, y=0,
                 anchor_point=None, anchored_position=None):
        super().__init__(scale=scale, x=x, y=y)
        self._cell_w, self._cell_h = font.get_bounding_box()[:2]
        self._font = font
        self._palette = displayio.Palette(2)
        self._palette.make_transparent(0)
        self._palette[1] = color
        self._capacity = max(64, len(text))
        self._grid = self._new_grid(self._capacity)
        self.append(self._grid)
        self._text = ""
        self._anchor_point = anchor_point
        self._anchored_position = anchored_position
        self.text = text

    def _new_grid(self, capacity):
        return displayio.TileGrid(
            self._font.bitmap, pixel_shader=self._palette,
            tile_width=self._cell_w, tile_height=self._cell_h,
            width=capacity, height=1, default_tile=0,
            y=-self._cell_h + self._cell_h // 2,
        )

    @property
    def text(self):
        return self._text

    @text.setter
    def text(self, value):
        value = str(value)
        if value == self._text:
            self.hidden = not value
            return
        grid = self._grid
        # Allocate before touching the visible old text. A failure keeps all
        # its glyphs intact and the caller can retry the pending frame later.
        if len(value) > self._capacity:
            grid = self._new_grid(len(value))
        extras = None
        for ch in value:
            code = ord(ch)
            if not 32 <= code <= 126:
                if extras is None:
                    extras = {}
                if code not in extras:
                    glyph = self._font.get_glyph(code)
                    extras[code] = glyph.tile_index if glyph is not None else ord("?") - 32
        for i in range(max(len(value), len(self._text))):
            code = ord(value[i]) if i < len(value) else 32
            if 32 <= code <= 126:
                tile = code - 32
            else:
                tile = extras[code]
            grid[i] = tile
        if grid is not self._grid:
            self[0] = grid
            self._grid = grid
            self._capacity = len(value)
        self._text = value
        self.hidden = not value
        self._position()

    @property
    def bounding_box(self):
        return (0, -self._cell_h + self._cell_h // 2,
                len(self._text) * self._cell_w, self._cell_h)

    @property
    def color(self):
        return self._palette[1]

    @color.setter
    def color(self, value):
        self._palette[1] = value

    @property
    def anchor_point(self):
        return self._anchor_point

    @anchor_point.setter
    def anchor_point(self, value):
        self._anchor_point = value
        self._position()

    @property
    def anchored_position(self):
        return self._anchored_position

    @anchored_position.setter
    def anchored_position(self, value):
        self._anchored_position = value
        self._position()

    def _position(self):
        if self._anchor_point is not None and self._anchored_position is not None:
            bx, by, w, h = self.bounding_box
            self.x = self._anchored_position[0] - int((bx + w * self._anchor_point[0]) * self.scale)
            self.y = self._anchored_position[1] - int((by + h * self._anchor_point[1]) * self.scale)


def _make_label(font, **kwargs):
    if font is terminalio.FONT and "\n" not in kwargs.get("text", ""):
        return _SystemLabel(font, **kwargs)
    # Custom BDF fonts and multiline text retain their existing renderer.
    # Avoid loading its Python classes at all for the normal system-font TFT.
    from adafruit_display_text import label
    return label.Label(font, **kwargs)


_EXPRESSION_ICON_W = 16
_EXPRESSION_ICON_H = 12
_EXPRESSION_GAP = 2


class _ExpressionBadge(displayio.Group):
    """Persistent pedal icon + text used by an ``expression_mode`` field.

    Its positioning surface intentionally matches a regular label: ``size``
    scales the complete badge, while anchor_point/anchored_position place its
    complete bounds. Keeping the icon and the fixed-capacity system label in
    one retained group means VOL/WAH changes only repaint the retained bitmap
    and alter text tile indices.
    """
    def __init__(self, font, text="---", color=_DEFAULT_COLOR, scale=1,
                 anchor_point=None, anchored_position=None):
        super().__init__(scale=scale)
        icon = displayio.Bitmap(_EXPRESSION_ICON_W, _EXPRESSION_ICON_H, 2)
        self._icon_palette = displayio.Palette(2)
        self._icon_palette.make_transparent(0)
        self._icon_palette[1] = color
        self._icon = displayio.TileGrid(
            icon, pixel_shader=self._icon_palette,
        )
        self._mode = ""
        self._icon.hidden = True
        self.append(self._icon)
        self._label = _make_label(font, text=str(text), color=color)
        self.append(self._label)
        self._text = str(text)
        self._anchor_point = anchor_point
        self._anchored_position = anchored_position
        self._bounds = (0, 0, 0, 0)
        self._layout_children()

    def set_mode(self, value):
        mode = value if value in ("VOL", "WAH") else ""
        if mode == self._mode:
            return
        self._mode = mode
        self._icon.hidden = not mode
        if not mode:
            return
        bitmap = self._icon.bitmap
        for y in range(_EXPRESSION_ICON_H):
            for x in range(_EXPRESSION_ICON_W):
                if mode == "VOL":
                    # Classic crescendo triangle: low at left, high at right.
                    on = (1 <= x <= 14 and 2 <= y <= 10
                          and 13 * (10 - y) <= 8 * (x - 1))
                else:
                    # Side profile: tilted treadle, pivot and horizontal base.
                    treadle = 6 - (x - 1) * 4 // 13
                    on = (1 <= x <= 14 and (
                        y == treadle or y == treadle + 1 or y in (9, 10)
                        or (x in (7, 8) and 6 <= y <= 8)))
                # Flat indices avoid allocating a coordinate tuple per pixel.
                bitmap[y * _EXPRESSION_ICON_W + x] = 1 if on else 0

    @property
    def text(self):
        return self._text

    @text.setter
    def text(self, value):
        value = str(value)
        if self._label.text != value:
            self._label.text = value
        self._text = value
        self.hidden = not value
        self._layout_children()

    @property
    def bounding_box(self):
        return self._bounds

    @property
    def color(self):
        return self._icon_palette[1]

    @color.setter
    def color(self, value):
        self._icon_palette[1] = value
        self._label.color = value

    @property
    def anchor_point(self):
        return self._anchor_point

    @anchor_point.setter
    def anchor_point(self, value):
        self._anchor_point = value
        self._position()

    @property
    def anchored_position(self):
        return self._anchored_position

    @anchored_position.setter
    def anchored_position(self, value):
        self._anchored_position = value
        self._position()

    def _layout_children(self):
        _, _, text_w, text_h = self._label.bounding_box
        height = max(_EXPRESSION_ICON_H, int(text_h))
        self._icon.y = (height - _EXPRESSION_ICON_H) // 2
        self._label.anchor_point = (0.0, 0.5)
        self._label.anchored_position = (
            _EXPRESSION_ICON_W + _EXPRESSION_GAP, height // 2,
        )
        self._bounds = (
            0, 0, _EXPRESSION_ICON_W + _EXPRESSION_GAP + int(text_w), height,
        )
        self._position()

    def _position(self):
        if self._anchor_point is not None and self._anchored_position is not None:
            _, _, width, height = self._bounds
            self.x = self._anchored_position[0] - int(
                width * self._anchor_point[0] * self.scale)
            self.y = self._anchored_position[1] - int(
                height * self._anchor_point[1] * self.scale)

# ---- tuner splash geometry + colours (shared by build + in-place update) ----
_TUNER_IN_TUNE  = 0x3ECB6E       # green when within the calibration window
_TUNER_OFF_HUE  = 0xE54848       # red when out of tune
_TUNER_NEUTRAL  = 0x40484F       # bar background
_TUNER_CENTER   = 0xFFFFFF       # vertical center reference
_TUNER_NOTE_OFF = 0xE4E6EB       # note colour when not in tune
_TUNER_FLAT     = 0xE59A48       # amber flat/sharp direction arrows
# Wider, taller bar and a chunkier indicator so the tuner reads clearly from
# a couple of metres away (the player is looking at the guitar, not the TFT).
_TUNER_BAR_W = 220
_TUNER_BAR_H = 10
_TUNER_IND_W = 8
_TUNER_IND_H = 30
_TUNER_BAR_Y = 150

# ---- horizontal scrolling (marquee) for overflowing labels ----
# When a label opts in (spec["scroll"]) and its text is wider than the space
# from the left margin to the right screen edge, it is re-anchored left and
# animated by tick(): a bounce (pause, scroll to reveal the end, pause, scroll
# back). Only the label's .x moves - a small dirty region - so this stays cheap
# even though the full render() is comparatively expensive.
_SCROLL_MARGIN = 6            # left/right inset, px
_SCROLL_SPEED_PX_S = 40       # default travel speed, px/second
_SCROLL_PAUSE_MS = 800        # dwell at each end before reversing
_SCROLL_FRAME_MS = 40         # min interval between position updates (~25 fps)


def _scroll_offset(elapsed_ms, span, speed):
    """Bounce offset in [0, span] for a marquee at `elapsed_ms` into its cycle.

    0 = text at its left home (start visible); span = fully scrolled left (end
    visible). Dwells _SCROLL_PAUSE_MS at each end. `speed` is px/second."""
    if span <= 0 or speed <= 0:
        return 0
    travel_ms = int(span * 1000 / speed)
    if travel_ms <= 0:
        return span
    period = 2 * (_SCROLL_PAUSE_MS + travel_ms)
    t = elapsed_ms % period
    if t < _SCROLL_PAUSE_MS:
        return 0
    t -= _SCROLL_PAUSE_MS
    if t < travel_ms:
        return int(span * t / travel_ms)
    t -= travel_ms
    if t < _SCROLL_PAUSE_MS:
        return span
    t -= _SCROLL_PAUSE_MS
    return int(span * (travel_ms - t) / travel_ms)

# Brand accent (matches the editor's green). Used for the splash logo + name.
_LOGO_COLOR = 0x6FD99B


def _logo_tilegrid():
    """Render the Bosun anchor mark into a small bitmap and return a
    centered TileGrid. Pure displayio - no extra libraries. Mirrors the
    editor's SVG logo (ring + staff + crossbar + curved fluke), scaled from
    the 24-unit viewBox by K. Index 0 is transparent so only the mark shows."""
    K = 3
    W = H = 24 * K
    bmp = displayio.Bitmap(W, H, 2)
    pal = displayio.Palette(2)
    pal[0] = 0x000000
    pal.make_transparent(0)
    pal[1] = _LOGO_COLOR

    def plot(x, y, t=1):
        x = int(round(x)); y = int(round(y))
        for dx in range(-t, t + 1):
            for dy in range(-t, t + 1):
                px, py = x + dx, y + dy
                if 0 <= px < W and 0 <= py < H:
                    bmp[px, py] = 1

    def line(x0, y0, x1, y1, t=1):
        x0 = int(round(x0)); y0 = int(round(y0))
        x1 = int(round(x1)); y1 = int(round(y1))
        dx = abs(x1 - x0); dy = -abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        while True:
            plot(x0, y0, t)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy; x0 += sx
            if e2 <= dx:
                err += dx; y0 += sy

    # Ring at top (circle outline): cx=12 cy=5 r=2.4 in viewBox units.
    cx, cy, r = 12 * K, 5 * K, 2.4 * K
    for i in range(48):
        a = 2 * math.pi * i / 48
        plot(cx + r * math.cos(a), cy + r * math.sin(a), 1)

    # Staff (vertical) and crossbar (horizontal).
    line(12 * K, 7.5 * K, 12 * K, 18.5 * K, 1)
    line(7 * K, 12 * K, 17 * K, 12 * K, 1)

    # Curved fluke: quadratic bezier M5,16 Q12,22 19,16.
    p0 = (5 * K, 16 * K); pc = (12 * K, 22 * K); p2 = (19 * K, 16 * K)
    prev = p0
    for i in range(1, 25):
        tt = i / 24.0
        mt = 1 - tt
        bx = mt * mt * p0[0] + 2 * mt * tt * pc[0] + tt * tt * p2[0]
        by = mt * mt * p0[1] + 2 * mt * tt * pc[1] + tt * tt * p2[1]
        line(prev[0], prev[1], bx, by, 1)
        prev = (bx, by)

    return displayio.TileGrid(bmp, pixel_shader=pal, x=(_SCREEN_W - W) // 2, y=24)


def _preview_badge():
    """Small 'PREVIEW' tag drawn over the layout while the preset-preview cursor
    is active, so the user knows the shown patch is not yet loaded."""
    return _make_label(
        terminalio.FONT, text="PREVIEW", color=_LOGO_COLOR, scale=2,
        anchor_point=(0.5, 1.0),
        anchored_position=(_SCREEN_W // 2, _SCREEN_H - 4),
    )


def _anchor_point(spec):
    return (
        _HALIGN.get(spec.get("halign", "left"),  0.0),
        _VALIGN.get(spec.get("valign", "top"),   0.0),
    )


def _anchor_pixels(spec):
    """Compute the screen-space anchor point.

    Semantics: halign/valign pick the corner/edge of the screen, then x/y
    are PIXEL OFFSETS from there. For pure absolute positioning use
    halign=left + valign=top with x/y as absolute pixels.
    """
    hx = _HALIGN.get(spec.get("halign", "left"), 0.0)
    vy = _VALIGN.get(spec.get("valign", "top"),  0.0)
    base_x = int(_SCREEN_W * hx)
    base_y = int(_SCREEN_H * vy)
    return (base_x + int(spec.get("x", 0)),
            base_y + int(spec.get("y", 0)))


def _format_field(spec, context):
    """Resolve a layout entry into the text to draw. Returns "" when the
    field has no value yet - caller then skips drawing so labels with a
    prefix don't render as orphan "SCENE " / "BANK " strings. The explicit
    expression-mode field instead renders ``---`` while its value is unknown."""
    field = spec.get("field")
    prefix = spec.get("prefix", "") or ""
    suffix = spec.get("suffix", "") or ""

    # Literal text (no `field` key - useful for static labels).
    if not field:
        return prefix + str(spec.get("text", "")) + suffix

    value = context.get(field, "")
    if field == "expression_mode":
        value = value if value in ("VOL", "WAH") else "---"
    if value is None or value == "":
        return ""
    return prefix + str(value) + suffix


class Display:
    def __init__(self, rotation=180, rowstart=80, colstart=0):
        displayio.release_displays()
        self._spi = busio.SPI(clock=SPI_CLK, MOSI=SPI_MOSI)
        bus = fourwire.FourWire(self._spi, command=TFT_DC, chip_select=TFT_CS)
        self._backlight = pwmio.PWMOut(TFT_PWM, frequency=1000, duty_cycle=0xFFFF)
        self.display = ST7789(
            bus, width=240, height=240,
            rowstart=rowstart, colstart=colstart, rotation=rotation,
        )
        # Marquee state: list of {"label", "left", "span", "speed"} for the
        # labels currently scrolling, plus the shared phase clock. Rebuilt by
        # every render(); animated by tick().
        self._scroll = []
        self._scroll_start_ms = None
        self._last_scroll_ms = None
        # Persistent tuner splash. Built once when the tuner turns on, then
        # updated in place (only the indicator moves) so it can refresh near
        # real time. Reset to inactive whenever a non-tuner frame is drawn.
        self._tuner_active = False
        self._tuner_note_lbl = None
        self._tuner_ind = None
        self._tuner_ind_pal = None
        self._tuner_footer = None
        self._tuner_last = None
        self._layout_group = None
        self._layout_specs = None
        self._layout_labels = []
        self._preview_label = None
        self._suspended = False

    def suspend(self):
        """Lend the cached frame's RAM to a firmware upload."""
        if self._suspended:
            return
        self._suspended = True
        self.display.root_group = None
        self._layout_group = self._layout_specs = self._preview_label = None
        self._layout_labels = []
        self._scroll = []
        self._scroll_start_ms = self._last_scroll_ms = None
        self._tuner_active = False
        self._tuner_note_lbl = self._tuner_footer = self._tuner_ind = None
        self._tuner_ind_pal = self._tuner_last = None
        gc.collect()

    def resume(self):
        """Return whether the caller must schedule a fresh context render."""
        suspended = self._suspended
        self._suspended = False
        return suspended

    def show_splash(self):
        self._scroll = []
        self._scroll_start_ms = None
        self._tuner_active = False
        group = displayio.Group()
        try:
            group.append(_logo_tilegrid())
        except Exception as e:
            print("splash logo failed:", e)
        group.append(_make_label(
            terminalio.FONT, text="Bosun", color=_LOGO_COLOR, scale=4,
            anchor_point=(0.5, 0.5), anchored_position=(_SCREEN_W // 2, 150),
        ))
        group.append(_make_label(
            terminalio.FONT, text=VERSION, color=0x9AA1AD, scale=2,
            anchor_point=(0.5, 0.5), anchored_position=(_SCREEN_W // 2, 185),
        ))
        self.display.root_group = group

    def render(self, context, layout):
        """Repaint the screen from the (context, layout) pair.
        Falls back to a single centered patch name if layout is missing/empty.

        When the generic `context['tuner'] == 'on'` (or the legacy
        `kemper_tuner == 'on'`) the regular layout is bypassed and a dedicated
        tuner splash takes over the whole screen - the user is presumably
        looking at their guitar, not at preset names. Any plugin can drive the
        tuner by publishing `tuner` (and optionally `tuner_note` /
        `tuner_deviance`); the Kemper plugin also keeps its kemper_* aliases."""
        if self._suspended:
            return
        if context.get("tuner") == "on" or context.get("kemper_tuner") == "on":
            self._scroll = []
            self._render_tuner(context)
            return
        self._tuner_active = False
        if not layout:
            layout = [{"field": "patch_name", "x": 20, "y": 113}]

        changed = layout != self._layout_specs
        rebuild = (self._layout_group is None
                   or len(layout) != len(self._layout_labels))
        if not rebuild:
            for spec, old, lbl in zip(layout, self._layout_specs, self._layout_labels):
                badge = isinstance(lbl, _ExpressionBadge)
                text_label = lbl._label if badge else lbl
                if ((spec.get("font") or "system") != (old.get("font") or "system")
                        or (spec.get("field") == "expression_mode") != badge
                        or (isinstance(text_label, _SystemLabel)
                            and "\n" in _format_field(spec, context))):
                    rebuild = True
                    break
        if rebuild:
            gc.collect()
            group = displayio.Group()
            labels = []
            specs = []
            for spec in layout:
                try:
                    text = _format_field(spec, context)
                    font = _load_font(spec.get("font"))
                    label_type = (_ExpressionBadge
                                  if spec.get("field") == "expression_mode"
                                  else _make_label)
                    lbl = label_type(
                        font, text=text, color=_parse_color(spec.get("color")),
                        scale=max(1, int(spec.get("size", 1))),
                        anchor_point=_anchor_point(spec),
                        anchored_position=_anchor_pixels(spec),
                    )
                    specs.append(dict(spec))
                    labels.append(lbl)
                    group.append(lbl)
                except MemoryError:
                    # Never publish an incomplete frame as a successful one.
                    raise
                except Exception as e:
                    print("display: bad layout entry", spec, "->", e)
            preview_label = _preview_badge()
            preview_label.hidden = context.get("preview") != "on"
            group.append(preview_label)
            # All allocations succeeded; replace the cached screen together.
            self._layout_group = group
            self._layout_specs = specs
            self._layout_labels = labels
            self._preview_label = preview_label
        elif changed:
            # Presentation edits keep the native groups, glyph tiles and
            # palettes. Snapshot only on edits, before touching the frame;
            # publish after success so a failed update remains retryable.
            gc.collect()
            specs = [dict(spec) for spec in layout]
        else:
            specs = self._layout_specs

        self._scroll = []
        self._scroll_start_ms = None
        for spec, lbl in zip(specs, self._layout_labels):
            text = _format_field(spec, context)
            size = max(1, int(spec.get("size", 1)))
            avail = _SCREEN_W - 2 * _SCROLL_MARGIN
            if lbl.text != text:
                lbl.text = text
            if isinstance(lbl, _ExpressionBadge):
                # Read the raw mode so user prefixes/suffixes cannot select
                # the wrong icon (e.g. a "WAH " prefix on a VOL field).
                lbl.set_mode(context.get("expression_mode"))
            lbl.scale = size
            lbl.anchor_point = _anchor_point(spec)
            lbl.anchored_position = _anchor_pixels(spec)
            if spec.get("scroll"):
                text_w = int(lbl.bounding_box[2]) * size
                if text_w > avail:
                    _, apy = _anchor_point(spec)
                    _, ay = _anchor_pixels(spec)
                    lbl.anchor_point = (0.0, apy)
                    lbl.anchored_position = (_SCROLL_MARGIN, ay)
                    try:
                        speed = int(spec.get("scroll_speed"))
                    except (TypeError, ValueError):
                        speed = _SCROLL_SPEED_PX_S
                    if speed <= 0:
                        speed = _SCROLL_SPEED_PX_S
                    self._scroll.append({"label": lbl, "home": lbl.x,
                                         "span": text_w - avail, "speed": speed})
        if changed and not rebuild:
            # Parse every color before changing a palette. Keep the prior
            # colors until all setters succeed, including badge icon/text.
            colors = [_parse_color(spec.get("color")) for spec in specs]
            previous = [lbl.color for lbl in self._layout_labels]
            try:
                for lbl, color in zip(self._layout_labels, colors):
                    lbl.color = color
            except Exception:
                for lbl, color in zip(self._layout_labels, previous):
                    lbl.color = color
                raise
        self._preview_label.hidden = context.get("preview") != "on"
        self.display.root_group = self._layout_group
        self._layout_specs = specs
        # Release the inactive tuner's labels once the normal screen owns TFT.
        self._tuner_note_lbl = self._tuner_footer = self._tuner_ind = None
        self._tuner_ind_pal = self._tuner_last = None

    def tick(self, now_ms):
        """Advance the marquee animation. Cheap no-op when nothing scrolls.

        Called from the main loop every iteration; throttled to ~25 fps so the
        SPI refresh of the moving label doesn't starve MIDI. Position is derived
        from absolute elapsed time, so scrolling stays smooth despite loop
        jitter and the occasional dropped frame."""
        if not self._scroll:
            return
        if self._scroll_start_ms is None:
            self._scroll_start_ms = now_ms
        if (self._last_scroll_ms is not None
                and now_ms - self._last_scroll_ms < _SCROLL_FRAME_MS):
            return
        self._last_scroll_ms = now_ms
        elapsed = now_ms - self._scroll_start_ms
        for s in self._scroll:
            try:
                s["label"].x = s["home"] - _scroll_offset(
                    elapsed, s["span"], s["speed"])
            except Exception:
                pass

    def _render_tuner(self, context):
        """Full-screen tuner splash: large note name, horizontal deviance
        bar with center reference, and a small numeric readout. Driven by any
        plugin that publishes `tuner`/`tuner_note`/`tuner_deviance` (the Kemper
        plugin publishes both these and legacy kemper_* aliases). Degrades
        gracefully when a plugin has no pitch feedback (e.g. Ampero only sends
        a tuner on/off toggle): note shows "-" and the needle stays centred.

        Built once on entry, then updated in place: subsequent frames only move
        the indicator (and touch the note/footer when they actually change), a
        small dirty region, so the needle tracks near real time instead of
        rebuilding and full-refreshing the whole 240x240 screen every frame.

        Geometry (240x240 screen):
          y  ~80  : note name, scale 8, centered (big and legible)
          y 150   : 220-wide background bar (grey) centered horizontally
          y ~140  : 8-wide indicator (green/red) tracking deviance
          y 195   : flat/sharp/in-tune direction cue + cents readout
        """
        # Deviance: 0..16383, 8192 = in tune. Show a usable window of
        # 8192 +/- 2000 so the indicator has meaningful travel; clamp
        # outside that to the edges so the user still sees direction.
        deviance = context.get("tuner_deviance")
        if deviance is None:
            deviance = context.get("kemper_tuner_deviance")
        try:
            deviance = int(deviance) if deviance is not None else 8192
        except (TypeError, ValueError):
            deviance = 8192
        in_tune = abs(deviance - 8192) <= 350

        bar_x = (_SCREEN_W - _TUNER_BAR_W) // 2
        rel = max(-2000, min(2000, deviance - 8192))       # window +/- 2000
        travel = _TUNER_BAR_W - _TUNER_IND_W
        ind_x = bar_x + (rel + 2000) * travel // 4000
        cents = rel // 8
        note = context.get("tuner_note") or context.get("kemper_tuner_note") or "-"
        # Direction: -1 flat (tune up), +1 sharp (tune down), 0 in tune.
        direction = 0 if in_tune else (-1 if rel < 0 else 1)

        if self._tuner_active and self._tuner_note_lbl is not None:
            self._update_tuner(note, in_tune, ind_x, cents, direction)
        else:
            self._build_tuner(note, in_tune, ind_x, bar_x, cents, direction)

    def _footer_text(self, cents, direction):
        """Bottom readout: a clear text cue for what to do plus the cents value.
        Arrows point the way to correct: '<< FLAT' means the note is flat and
        the player should tune up, '>> SHARP' means sharp (tune down)."""
        if direction < 0:
            return "<< FLAT  {}".format(abs(cents))
        if direction > 0:
            return "SHARP {} >>".format(abs(cents))
        return "IN TUNE"

    def _footer_color(self, in_tune):
        return _TUNER_IN_TUNE if in_tune else _TUNER_FLAT

    def _build_tuner(self, note, in_tune, ind_x, bar_x, cents, direction):
        """First tuner frame: build the persistent group and cache the parts
        that move so later frames can update them without a full rebuild."""
        ind_y = _TUNER_BAR_Y - (_TUNER_IND_H - _TUNER_BAR_H) // 2
        group = displayio.Group()

        note_lbl = _make_label(
            terminalio.FONT, text=str(note),
            color=_TUNER_IN_TUNE if in_tune else _TUNER_NOTE_OFF,
            scale=8, anchor_point=(0.5, 0.5),
            anchored_position=(_SCREEN_W // 2, 80),
        )
        group.append(note_lbl)

        bar_bmp = displayio.Bitmap(_TUNER_BAR_W, _TUNER_BAR_H, 1)
        bar_pal = displayio.Palette(1)
        bar_pal[0] = _TUNER_NEUTRAL
        group.append(displayio.TileGrid(bar_bmp, pixel_shader=bar_pal, x=bar_x, y=_TUNER_BAR_Y))

        # Taller center reference so "in tune" (indicator over center) is obvious.
        center_bmp = displayio.Bitmap(2, _TUNER_BAR_H + 12, 1)
        center_pal = displayio.Palette(1)
        center_pal[0] = _TUNER_CENTER
        group.append(displayio.TileGrid(
            center_bmp, pixel_shader=center_pal,
            x=bar_x + _TUNER_BAR_W // 2 - 1, y=_TUNER_BAR_Y - 6,
        ))

        ind_bmp = displayio.Bitmap(_TUNER_IND_W, _TUNER_IND_H, 1)
        ind_pal = displayio.Palette(1)
        ind_pal[0] = _TUNER_IN_TUNE if in_tune else _TUNER_OFF_HUE
        ind_tg = displayio.TileGrid(ind_bmp, pixel_shader=ind_pal, x=ind_x, y=ind_y)
        group.append(ind_tg)

        footer = _make_label(
            terminalio.FONT, text=self._footer_text(cents, direction),
            color=self._footer_color(in_tune), scale=2, anchor_point=(0.5, 0.5),
            anchored_position=(_SCREEN_W // 2, 200),
        )
        group.append(footer)

        self.display.root_group = group
        self._tuner_note_lbl = note_lbl
        self._tuner_ind = ind_tg
        self._tuner_ind_pal = ind_pal
        self._tuner_footer = footer
        self._tuner_active = True
        self._tuner_last = {"note": note, "in_tune": in_tune,
                            "ind_x": ind_x, "cents": cents,
                            "direction": direction}

    def _update_tuner(self, note, in_tune, ind_x, cents, direction):
        """Cheap in-place update: only touch the displayio objects whose value
        actually changed. Moving the indicator is a tiny dirty region; the note
        and footer rarely change, so their (glyph re-render) cost is rare."""
        last = self._tuner_last
        if ind_x != last["ind_x"]:
            self._tuner_ind.x = ind_x
            last["ind_x"] = ind_x
        if in_tune != last["in_tune"]:
            self._tuner_ind_pal[0] = _TUNER_IN_TUNE if in_tune else _TUNER_OFF_HUE
            self._tuner_note_lbl.color = _TUNER_IN_TUNE if in_tune else _TUNER_NOTE_OFF
            self._tuner_footer.color = self._footer_color(in_tune)
            last["in_tune"] = in_tune
        if note != last["note"]:
            self._tuner_note_lbl.text = str(note)
            last["note"] = note
        if cents != last["cents"] or direction != last["direction"]:
            self._tuner_footer.text = self._footer_text(cents, direction)
            last["cents"] = cents
            last["direction"] = direction

    # ---- legacy single-string helpers, kept for non-rendered states ----

    def show_patch(self, name):
        """Fallback when no layout is configured."""
        self._scroll = []
        self._scroll_start_ms = None
        self._tuner_active = False
        group = displayio.Group()
        group.append(_make_label(terminalio.FONT, text=name or "(unnamed)", x=20, y=120))
        self.display.root_group = group

