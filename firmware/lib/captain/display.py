"""TFT display driver - multi-label rendering driven by layout config.

The display state is the (context, layout) pair:
  - `context` is a dict of named field values. The core owns patch_name,
    bank and slot; plugins write their own fields via update_context() and
    on_midi_in() hooks.
  - `layout` is a list of label specs read from the profile's device.json:
        [{"field": "patch_name", "x": 20, "y": 40, "size": 3,
          "color": "#ffffff", "prefix": "", "suffix": ""}, ...]

`render(context, layout)` rebuilds the displayio Group from scratch each frame.
That's fine here - labels are cheap and we redraw on patch/scene change at
human speeds, not at audio rates.
"""
import busio
import displayio
import fourwire
import math
import pwmio
import terminalio

from adafruit_display_text import label
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
    prefix don't render as orphan "SCENE " / "BANK " strings."""
    field = spec.get("field")
    prefix = spec.get("prefix", "") or ""
    suffix = spec.get("suffix", "") or ""

    # Literal text (no `field` key - useful for static labels).
    if not field:
        return prefix + str(spec.get("text", "")) + suffix

    value = context.get(field, "")
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

    def show_splash(self):
        self._scroll = []
        self._scroll_start_ms = None
        group = displayio.Group()
        try:
            group.append(_logo_tilegrid())
        except Exception as e:
            print("splash logo failed:", e)
        group.append(label.Label(
            terminalio.FONT, text="Bosun", color=_LOGO_COLOR, scale=4,
            anchor_point=(0.5, 0.5), anchored_position=(_SCREEN_W // 2, 150),
        ))
        group.append(label.Label(
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
        # Drop any labels registered by the previous frame before we rebuild -
        # those Label objects are about to be replaced and must not be animated.
        self._scroll = []
        self._scroll_start_ms = None

        if context.get("tuner") == "on" or context.get("kemper_tuner") == "on":
            self._render_tuner(context)
            return

        group = displayio.Group()

        if not layout:
            name = context.get("patch_name") or "(unnamed)"
            group.append(label.Label(terminalio.FONT, text=str(name), x=20, y=120))
            self.display.root_group = group
            return

        for spec in layout:
            try:
                text = _format_field(spec, context)
                if text == "":
                    continue
                color = _parse_color(spec.get("color"))
                size = max(1, int(spec.get("size", 1)))
                # `halign`/`valign` pick the corner of the screen
                # AND the corresponding corner of the label. x/y are
                # pixel offsets from that screen corner. Result: the
                # label visually aligns the same way regardless of its
                # text length or scale.
                font = _load_font(spec.get("font"))
                apx, apy = _anchor_point(spec)
                ax, ay = _anchor_pixels(spec)
                lbl = label.Label(
                    font,
                    text=text,
                    color=color,
                    scale=size,
                    anchor_point=(apx, apy),
                    anchored_position=(ax, ay),
                )
                # Marquee: if this label opts in and its text is wider than the
                # usable width, re-anchor it to the left margin (keeping its
                # vertical anchor) and register it for animation in tick().
                if spec.get("scroll"):
                    try:
                        text_w = int(lbl.bounding_box[2]) * size
                    except Exception:
                        text_w = 0
                    avail = _SCREEN_W - 2 * _SCROLL_MARGIN
                    if text_w > avail:
                        lbl.anchor_point = (0.0, apy)
                        lbl.anchored_position = (_SCROLL_MARGIN, ay)
                        try:
                            speed = int(spec.get("scroll_speed"))
                        except (TypeError, ValueError):
                            speed = _SCROLL_SPEED_PX_S
                        if speed <= 0:
                            speed = _SCROLL_SPEED_PX_S
                        # Capture the label's real home x AFTER anchoring: for
                        # some BDF fonts the glyph bounding box origin isn't 0,
                        # so lbl.x != _SCROLL_MARGIN. Animating from the actual
                        # value avoids a one-frame jump at the start of a cycle.
                        self._scroll.append({
                            "label": lbl,
                            "home": lbl.x,
                            "span": text_w - avail,
                            "speed": speed,
                        })
                group.append(lbl)
            except Exception as e:
                # Bad layout entry should never crash the display.
                print("display: bad layout entry", spec, "->", e)

        self.display.root_group = group

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

        Geometry (240x240 screen):
          y  ~70  : note name, scale 6, centered
          y 140   : 200-wide background bar (grey) centered horizontally
          y 134-152: 4-wide indicator (green/red) tracking deviance
          y 195   : optional "TUNER" + cents-ish numeric below
        """
        IN_TUNE  = 0x3ECB6E          # green when within calibration window
        OFF_HUE  = 0xE54848          # red when out of tune
        NEUTRAL  = 0x40484F          # bar background
        CENTER   = 0xFFFFFF          # vertical center reference

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

        bar_w = 200
        bar_h = 6
        bar_x = (_SCREEN_W - bar_w) // 2
        bar_y = 140

        ind_w = 4
        ind_h = 18
        # Window: 8192 +/- 2000 mapped to [0, bar_w - ind_w]
        rel = max(-2000, min(2000, deviance - 8192))
        travel = bar_w - ind_w
        ind_x = bar_x + int((rel + 2000) * travel / 4000)
        ind_y = bar_y - (ind_h - bar_h) // 2

        group = displayio.Group()

        # Note name (big). Falls back to "-" if we never received one yet.
        note = context.get("tuner_note") or context.get("kemper_tuner_note") or "-"
        note_lbl = label.Label(
            terminalio.FONT,
            text=str(note),
            color=IN_TUNE if in_tune else 0xE4E6EB,
            scale=6,
            anchor_point=(0.5, 0.5),
            anchored_position=(_SCREEN_W // 2, 75),
        )
        group.append(note_lbl)

        # Bar background.
        bar_bmp = displayio.Bitmap(bar_w, bar_h, 1)
        bar_pal = displayio.Palette(1)
        bar_pal[0] = NEUTRAL
        group.append(displayio.TileGrid(bar_bmp, pixel_shader=bar_pal, x=bar_x, y=bar_y))

        # Center reference: 2-pixel vertical line at the bar midpoint.
        center_bmp = displayio.Bitmap(2, bar_h + 6, 1)
        center_pal = displayio.Palette(1)
        center_pal[0] = CENTER
        group.append(displayio.TileGrid(
            center_bmp, pixel_shader=center_pal,
            x=bar_x + bar_w // 2 - 1, y=bar_y - 3,
        ))

        # Moving indicator.
        ind_bmp = displayio.Bitmap(ind_w, ind_h, 1)
        ind_pal = displayio.Palette(1)
        ind_pal[0] = IN_TUNE if in_tune else OFF_HUE
        group.append(displayio.TileGrid(ind_bmp, pixel_shader=ind_pal, x=ind_x, y=ind_y))

        # Footer: "TUNER" + signed cents-like offset (rel/8 keeps the
        # number small and roughly readable; the bar is the real signal).
        cents = rel // 8
        sign = "+" if cents > 0 else ("" if cents == 0 else "-")
        footer = label.Label(
            terminalio.FONT,
            text="TUNER  {}{}".format(sign, abs(cents)),
            color=0x9AA1AD,
            scale=2,
            anchor_point=(0.5, 0.5),
            anchored_position=(_SCREEN_W // 2, 195),
        )
        group.append(footer)

        self.display.root_group = group

    # ---- legacy single-string helpers, kept for non-rendered states ----

    def show_patch(self, name):
        """Fallback when no layout is configured."""
        self._scroll = []
        self._scroll_start_ms = None
        group = displayio.Group()
        group.append(label.Label(terminalio.FONT, text=name or "(unnamed)", x=20, y=120))
        self.display.root_group = group

