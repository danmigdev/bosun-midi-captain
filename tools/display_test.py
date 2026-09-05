#!/usr/bin/env python3
"""Real TFT renderer against strict CircuitPython display mocks.

The Captain reproduction exhausted RAM while rebuilding Label's per-character
TileGrids: BANK/RIG creation raised MemoryError and render() installed a frame
with those rows missing. These tests model that allocation failure explicitly,
inspect the actual rendered glyph tiles, and require steady-state rig changes
to preserve the display objects. They do not estimate RP2040 heap byte counts.
"""

import importlib
import sys
import types
import unittest
from collections import Counter
from pathlib import Path


class Allocations:
    counts = Counter()
    denied = set()
    fail_label_prefixes = ()

    @classmethod
    def allocate(cls, kind):
        if kind in cls.denied:
            raise MemoryError("simulated exhausted display heap: " + kind)
        cls.counts[kind] += 1


class Group:
    def __init__(self, *, scale=1, x=0, y=0):
        Allocations.allocate("Group")
        self.scale, self.x, self.y = scale, x, y
        self.hidden = False
        self.children = []

    def append(self, child):
        if child in self.children:
            raise ValueError("duplicate display object")
        self.children.append(child)

    def pop(self, index=-1):
        return self.children.pop(index)

    def __len__(self):
        return len(self.children)

    def __iter__(self):
        return iter(self.children)

    def __getitem__(self, index):
        return self.children[index]

    def __setitem__(self, index, value):
        self.children[index] = value


class Bitmap:
    def __init__(self, width, height, value_count):
        Allocations.allocate("Bitmap")
        self.width, self.height = width, height
        self.value_count = value_count
        self.pixels = {}

    def __getitem__(self, index):
        x, y = index if isinstance(index, tuple) else (index % self.width, index // self.width)
        return self.pixels.get((x, y), 0)

    def __setitem__(self, index, value):
        x, y = index if isinstance(index, tuple) else (index % self.width, index // self.width)
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise IndexError(index)
        if not (0 <= value < self.value_count):
            raise ValueError(value)
        self.pixels[x, y] = value


class Palette:
    def __init__(self, color_count):
        Allocations.allocate("Palette")
        self.colors = [0] * color_count
        self.transparent = set()

    def __setitem__(self, index, value):
        self.colors[index] = value

    def __getitem__(self, index):
        return self.colors[index]

    def make_transparent(self, index):
        self.transparent.add(index)

    def make_opaque(self, index):
        self.transparent.discard(index)


class TileGrid:
    def __init__(self, bitmap, *, pixel_shader, width=1, height=1,
                 tile_width=None, tile_height=None, default_tile=0, x=0, y=0):
        Allocations.allocate("TileGrid")
        self.bitmap, self.pixel_shader = bitmap, pixel_shader
        self.width, self.height = width, height
        self.tile_width = tile_width or bitmap.width
        self.tile_height = tile_height or bitmap.height
        self.x, self.y, self.hidden = x, y, False
        if bitmap.width % self.tile_width or bitmap.height % self.tile_height:
            raise ValueError("bitmap is not evenly divided into tiles")
        self.tile_count = (bitmap.width // self.tile_width) * (bitmap.height // self.tile_height)
        self.tiles = [default_tile] * (width * height)
        for value in self.tiles:
            self._check_tile(value)

    def _check_tile(self, value):
        if not (0 <= value < self.tile_count and value <= 255):
            raise ValueError("Tile index out of bounds")

    def _index(self, index):
        return index[1] * self.width + index[0] if isinstance(index, tuple) else index

    def __setitem__(self, index, value):
        self._check_tile(value)
        self.tiles[self._index(index)] = value

    def __getitem__(self, index):
        return self.tiles[self._index(index)]


class BuiltinFont:
    """CP 9.2.7 BuiltinFont.c's shared atlas and fixed ASCII tile mapping."""

    def __init__(self):
        self.bitmap = Bitmap(6 * 96, 14, 2)

    def get_bounding_box(self):
        return (6, 14)

    def get_glyph(self, codepoint):
        Allocations.allocate("Glyph")
        if 32 <= codepoint <= 126:
            index = codepoint - 32
        elif codepoint == ord("é"):
            index = 95
        else:
            return None
        return types.SimpleNamespace(bitmap=self.bitmap, tile_index=index,
                                     width=6, height=14, dx=0, dy=0,
                                     shift_x=6, shift_y=0)


SYSTEM_FONT = BuiltinFont()


class Label(Group):
    """Models Label's allocation and coordinate contract, not its full API."""

    def __init__(self, font, *, text="", color=0xFFFFFF, scale=1, x=0, y=0,
                 anchor_point=None, anchored_position=None):
        Allocations.allocate("Label")
        if text.startswith(Allocations.fail_label_prefixes):
            raise MemoryError("simulated per-glyph allocation failure")
        super().__init__(scale=scale, x=x, y=y)
        self.font, self.color = font, color
        self._anchor_point = anchor_point
        self._anchored_position = anchored_position
        self._text = ""
        self.text = text

    @property
    def text(self):
        return self._text

    @text.setter
    def text(self, value):
        glyphs = []
        palette = Palette(2)
        palette[1] = self.color
        palette.make_transparent(0)
        x = y = 0
        for char in value:
            if char == "\n":
                y += int(self.font.get_bounding_box()[1] * 1.25)
                x = 0
                continue
            glyph = self.font.get_glyph(ord(char))
            if glyph is not None:
                glyphs.append(TileGrid(glyph.bitmap, pixel_shader=palette,
                                       tile_width=glyph.width, tile_height=glyph.height,
                                       default_tile=glyph.tile_index,
                                       x=x, y=y - glyph.height + glyph.height // 2))
                x += glyph.shift_x
        self.children = glyphs
        self._text = value
        self._anchor()

    @property
    def bounding_box(self):
        width, height = self.font.get_bounding_box()[:2]
        lines = self.text.split("\n")
        return (0, -height + height // 2, max(map(len, lines)) * width,
                height + (len(lines) - 1) * int(height * 1.25))

    def _anchor(self):
        if self._anchor_point is not None and self._anchored_position is not None:
            bx, by, width, height = self.bounding_box
            self.x = self._anchored_position[0] - int((bx + self._anchor_point[0] * width) * self.scale)
            self.y = self._anchored_position[1] - int((by + self._anchor_point[1] * height) * self.scale)

    @property
    def anchor_point(self):
        return self._anchor_point

    @anchor_point.setter
    def anchor_point(self, value):
        self._anchor_point = value
        self._anchor()

    @property
    def anchored_position(self):
        return self._anchored_position

    @anchored_position.setter
    def anchored_position(self, value):
        self._anchored_position = value
        self._anchor()


def module(name, **members):
    result = types.ModuleType(name)
    result.__dict__.update(members)
    sys.modules[name] = result
    return result


module("board", **{"GP%d" % i: i for i in range(30)})
module("busio", SPI=lambda **kwargs: None)
module("fourwire", FourWire=lambda *args, **kwargs: None)
module("pwmio", PWMOut=lambda *args, **kwargs: types.SimpleNamespace(duty_cycle=65535))
module("terminalio", FONT=SYSTEM_FONT)
module("displayio", Group=Group, Bitmap=Bitmap, Palette=Palette, TileGrid=TileGrid,
       release_displays=lambda: None)
module("adafruit_display_text", label=module("adafruit_display_text.label", Label=Label))
module("adafruit_st7789", ST7789=lambda *args, **kwargs: types.SimpleNamespace(root_group=None))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "firmware" / "lib"))
display_module = importlib.import_module("captain.display")


def glyphs(root, x=0, y=0, scale=1):
    """Return actual visible atlas glyphs with absolute screen coordinates."""
    if root is None or getattr(root, "hidden", False):
        return []
    x += root.x * scale
    y += root.y * scale
    if isinstance(root, Group):
        scale *= root.scale
        return [item for child in root for item in glyphs(child, x, y, scale)]
    if isinstance(root, TileGrid) and root.bitmap is SYSTEM_FONT.bitmap:
        result = []
        for index, tile in enumerate(root.tiles):
            if tile:
                char = chr(tile + 32) if tile < 95 else "é"
                result.append((x + index % root.width * root.tile_width * scale,
                               y + index // root.width * root.tile_height * scale,
                               root.tile_width * scale, root.tile_height * scale, char))
        return result
    return []


def painted_bitmaps(root, x=0, y=0, scale=1):
    """Return visible non-font bitmap bounds in absolute screen pixels."""
    if root is None or getattr(root, "hidden", False):
        return []
    x += root.x * scale
    y += root.y * scale
    if isinstance(root, Group):
        scale *= root.scale
        return [item for child in root
                for item in painted_bitmaps(child, x, y, scale)]
    if (isinstance(root, TileGrid) and root.bitmap is not SYSTEM_FONT.bitmap
            and any(root.bitmap.pixels.values())):
        return [(x, y, root.bitmap.width * scale,
                 root.bitmap.height * scale, root.pixel_shader)]
    return []


def visible_lines(display):
    rows = {}
    for x, y, width, height, char in glyphs(display.display.root_group):
        if x + width > 0 and x < 240 and y + height > 0 and y < 240:
            rows.setdefault(y, []).append((x, char))
    return ["".join(char for _, char in sorted(rows[y])) for y in sorted(rows)]


LAYOUT = [
    {"field": "patch_name", "x": 8, "y": 8, "size": 2},
    {"field": "bank", "prefix": "BANK ", "x": 8, "y": 80, "size": 2},
    {"field": "slot", "prefix": "RIG ", "x": 8, "y": 120, "size": 2},
]

EXPRESSION_SPEC = {
    "field": "expression_mode", "halign": "right", "valign": "bottom",
    "x": -6, "y": -6, "size": 2, "color": "#ffffff",
}
EXPRESSION_LAYOUT = LAYOUT + [EXPRESSION_SPEC]


class DisplayRegressionTests(unittest.TestCase):
    def setUp(self):
        Allocations.counts.clear()
        Allocations.denied.clear()
        Allocations.fail_label_prefixes = ()
        self.display = display_module.Display()

    def tearDown(self):
        Allocations.denied.clear()
        Allocations.fail_label_prefixes = ()

    def render(self, name="CLEAN", bank=1, slot=2, **context):
        self.display.render(dict(patch_name=name, bank=bank, slot=slot, **context), LAYOUT)

    def test_reproduced_glyph_memory_pressure_keeps_bank_and_rig_visible(self):
        self.render("ACOUSTIC", slot=1)
        Allocations.fail_label_prefixes = ("BANK ", "RIG ")
        self.render("CRUNCH", slot=3)
        self.assertEqual(visible_lines(self.display), ["CRUNCH", "BANK1", "RIG3"])
        self.render("CLEAN", slot=2)
        self.assertEqual(visible_lines(self.display), ["CLEAN", "BANK1", "RIG2"])

    def test_thousand_rig_changes_need_no_new_display_objects_or_ascii_glyphs(self):
        self.render("ACOUSTIC", slot=1)
        root = self.display.display.root_group
        before = Allocations.counts.copy()
        Allocations.denied = {"Group", "TileGrid", "Label", "Palette", "Bitmap", "Glyph"}
        for index in range(1000):
            name, slot = ("CLEAN", 2) if index % 2 else ("CRUNCH", 3)
            self.render(name, slot=slot)
            self.assertIs(self.display.display.root_group, root)
            self.assertEqual(visible_lines(self.display), [name, "BANK1", "RIG%d" % slot])
        self.assertEqual(Allocations.counts, before)

    def test_shorter_and_empty_text_clear_old_glyphs_then_restore_rows(self):
        self.render("ACOUSTIC", bank=123, slot=10)
        self.render("A", bank=1, slot=2)
        self.assertEqual(visible_lines(self.display), ["A", "BANK1", "RIG2"])
        self.render("CLEAN", bank=None, slot="")
        self.assertEqual(visible_lines(self.display), ["CLEAN"])
        self.render("CRUNCH", bank=2, slot=3)
        self.assertEqual(visible_lines(self.display), ["CRUNCH", "BANK2", "RIG3"])

    def test_anchored_label_keeps_requested_edges_when_text_length_changes(self):
        layout = [{"field": "patch_name", "halign": "right", "valign": "bottom",
                   "x": -6, "y": -8, "size": 2}]
        for text in ("ACOUSTIC", "CLEAN", "A"):
            self.display.render({"patch_name": text}, layout)
            cells = glyphs(self.display.display.root_group)
            self.assertEqual(max(x + w for x, y, w, h, c in cells), 234)
            self.assertEqual(max(y + h for x, y, w, h, c in cells), 232)

    def test_scroll_updates_position_without_rebuilding_and_short_text_resets(self):
        layout = [{"field": "patch_name", "x": 8, "y": 20, "size": 2, "scroll": True}]
        self.display.render({"patch_name": "ABCDEFGHIJKLMNOPQRSTUVWXYZ"}, layout)
        before = glyphs(self.display.display.root_group)
        self.display.tick(0)
        self.display.tick(2000)
        after = glyphs(self.display.display.root_group)
        self.assertLess(after[0][0], before[0][0])
        self.display.render({"patch_name": "CLEAN"}, layout)
        self.display.tick(4000)
        cells = glyphs(self.display.display.root_group)
        self.assertEqual(cells[0][0], 8)
        self.assertEqual(visible_lines(self.display), ["CLEAN"])

    def test_unanchored_show_patch_preserves_font_baseline(self):
        self.display.show_patch("CLEAN")
        cells = glyphs(self.display.display.root_group)
        self.assertEqual(cells[0][:2], (20, 113))

    def test_bad_layout_entry_does_not_remove_valid_rows(self):
        layout = [LAYOUT[0], {"field": "bank", "size": "broken"}, LAYOUT[2]]
        self.display.render({"patch_name": "CLEAN", "bank": 1, "slot": 2}, layout)
        self.assertEqual(visible_lines(self.display), ["CLEAN", "RIG2"])

    def test_layout_allocation_failure_retains_previous_complete_frame(self):
        self.render()
        previous_root = self.display.display.root_group
        Allocations.denied = {"Group", "TileGrid", "Label"}
        # Adding a row genuinely needs a new group; presentation edits do not.
        new_layout = [dict(spec, color="#FF0000") for spec in LAYOUT] + [{"text": ""}]
        with self.assertRaises(MemoryError):
            self.display.render({"patch_name": "CRUNCH", "bank": 2, "slot": 3}, new_layout)
        self.assertIs(self.display.display.root_group, previous_root)
        self.assertEqual(visible_lines(self.display), ["CLEAN", "BANK1", "RIG2"])
        Allocations.denied.clear()
        self.display.render({"patch_name": "CRUNCH", "bank": 2, "slot": 3}, new_layout)
        self.assertEqual(visible_lines(self.display), ["CRUNCH", "BANK2", "RIG3"])

    def test_color_save_reuses_every_label_palette_and_expression_icon_under_oom(self):
        context = {"patch_name": "CRUNCH", "bank": 1, "slot": 3, "expression_mode": "WAH"}
        self.display.render(context, EXPRESSION_LAYOUT)
        root = self.display.display.root_group
        labels = tuple(self.display._layout_labels)
        badge = labels[-1]
        bitmap, icon_palette, text_palette = badge._icon.bitmap, badge._icon_palette, badge._label._palette
        before = Allocations.counts.copy()
        Allocations.denied = {"Group", "TileGrid", "Label", "Palette", "Bitmap", "Glyph"}
        colors = ["#123456", "#abcdef", "#6fd99a", "#654321"]
        updated = [dict(spec, color=color) for spec, color in zip(EXPRESSION_LAYOUT, colors)]
        self.display.render(context, updated)
        self.assertIs(self.display.display.root_group, root)
        self.assertEqual(tuple(self.display._layout_labels), labels)
        self.assertEqual([lbl.color for lbl in labels], [int(color[1:], 16) for color in colors])
        self.assertIs(badge._icon.bitmap, bitmap)
        self.assertIs(badge._icon_palette, icon_palette)
        self.assertIs(badge._label._palette, text_palette)
        self.assertEqual((icon_palette[1], text_palette[1]), (0x654321, 0x654321))
        self.assertEqual(self.display._layout_specs, updated)
        self.assertIsNot(self.display._layout_specs[0], updated[0])
        # A later in-place editor mutation must be detected by the snapshot.
        updated[0]["color"] = "#010203"
        self.display.render(context, updated)
        self.assertEqual(labels[0].color, 0x010203)
        self.assertEqual(Allocations.counts, before)

    def test_ota_suspend_releases_layout_and_tuner_and_blocks_rebuild_until_resume(self):
        import gc
        import weakref

        context = {"patch_name": "CRUNCH", "bank": 1, "slot": 3, "expression_mode": "WAH"}
        self.display.render(context, EXPRESSION_LAYOUT)
        badge = self.display._layout_labels[-1]
        resources = [self.display.display.root_group, *self.display._layout_labels,
                     badge._icon, badge._icon.bitmap, badge._icon_palette,
                     badge._label, badge._label._palette]
        refs = [weakref.ref(item) for item in resources]
        del resources, badge
        self.display.render(dict(context, tuner="on", tuner_note="A", tuner_deviance=8192),
                            EXPRESSION_LAYOUT)
        resources = [self.display.display.root_group, self.display._tuner_note_lbl,
                     self.display._tuner_footer, self.display._tuner_ind, self.display._tuner_ind_pal]
        refs.extend(weakref.ref(item) for item in resources)
        del resources
        before = Allocations.counts.copy()
        Allocations.denied = {"Group", "TileGrid", "Label", "Palette", "Bitmap", "Glyph"}
        self.display.suspend()
        self.display.suspend()
        gc.collect()
        self.assertTrue(all(ref() is None for ref in refs), "a cached native frame stayed reachable")
        self.assertIsNone(self.display.display.root_group)
        self.display.render(context, EXPRESSION_LAYOUT)
        self.display.render(dict(context, tuner="on"), EXPRESSION_LAYOUT)
        self.display.tick(1000)
        self.assertIsNone(self.display.display.root_group)
        self.assertEqual(Allocations.counts, before)
        self.assertTrue(self.display.resume())
        self.assertFalse(self.display.resume())
        Allocations.denied.clear()
        self.display.render(dict(context, patch_name="CLEAN", slot=2, expression_mode="VOL"),
                            EXPRESSION_LAYOUT)
        self.assertEqual(visible_lines(self.display), ["CLEAN", "BANK1", "RIG2", "VOL"])

    def test_color_setter_failure_restores_all_palettes_and_keeps_edit_retryable(self):
        from unittest.mock import patch

        context = {"patch_name": "CRUNCH", "bank": 1, "slot": 3, "expression_mode": "WAH"}
        self.display.render(context, EXPRESSION_LAYOUT)
        labels = self.display._layout_labels
        old_specs = self.display._layout_specs
        old_colors = [lbl.color for lbl in labels]
        badge = labels[-1]
        updated = [dict(spec, color="#123456") for spec in EXPRESSION_LAYOUT]
        original_set = Palette.__setitem__
        failed = False

        def fail_badge_text(palette, index, value):
            nonlocal failed
            if palette is badge._label._palette and value == 0x123456 and not failed:
                failed = True
                raise MemoryError("badge text palette update failed after its icon")
            original_set(palette, index, value)

        Allocations.denied = {"Group", "TileGrid", "Label", "Palette", "Bitmap", "Glyph"}
        with patch.object(Palette, "__setitem__", fail_badge_text):
            with self.assertRaises(MemoryError):
                self.display.render(context, updated)
            self.assertTrue(failed)
            self.assertIs(self.display._layout_specs, old_specs)
            self.assertEqual([lbl.color for lbl in labels], old_colors)
            self.assertEqual(badge._label.color, old_colors[-1])
            self.display.render(context, updated)
        self.assertEqual([lbl.color for lbl in labels], [0x123456] * len(labels))
        self.assertEqual(self.display._layout_specs, updated)

    def test_position_size_prefix_and_suffix_edit_reuses_existing_rows(self):
        context = {"patch_name": "CRUNCH", "bank": 1, "slot": 3, "expression_mode": "VOL"}
        self.display.render(context, EXPRESSION_LAYOUT)
        root = self.display.display.root_group
        labels = tuple(self.display._layout_labels)
        before = Allocations.counts.copy()
        Allocations.denied = {"Group", "TileGrid", "Label", "Palette", "Bitmap", "Glyph"}
        updated = [dict(spec) for spec in EXPRESSION_LAYOUT]
        updated[0].update(x=-10, y=12, size=1, halign="right", suffix="!")
        updated[1].update(prefix="BANK: ", suffix=" /", x=20)
        updated[-1].update(x=12, y=18, size=1, halign="left", valign="top", prefix="[", suffix="]")
        self.display.render(context, updated)
        self.assertIs(self.display.display.root_group, root)
        self.assertEqual(tuple(self.display._layout_labels), labels)
        self.assertEqual(labels[0].text, "CRUNCH!")
        self.assertEqual(labels[0].scale, 1)
        self.assertEqual(labels[0].x + labels[0].bounding_box[2], 230)
        self.assertEqual(labels[1].text, "BANK: 1 /")
        self.assertEqual(labels[1].x, 20)
        self.assertEqual((labels[-1].x, labels[-1].y, labels[-1].scale), (12, 18, 1))
        self.assertEqual(labels[-1].text, "[VOL]")
        self.assertEqual(Allocations.counts, before)

    def test_label_kind_or_font_change_still_requires_a_new_frame(self):
        self.render()
        root = self.display.display.root_group
        Allocations.denied = {"Group"}
        for changes in ({"field": "expression_mode"}, {"font": "different.bdf"}):
            updated = [dict(spec) for spec in LAYOUT]
            updated[0].update(changes)
            with self.assertRaises(MemoryError):
                self.display.render({"patch_name": "CRUNCH", "bank": 2, "slot": 3}, updated)
            self.assertIs(self.display.display.root_group, root)
            self.assertEqual(visible_lines(self.display), ["CLEAN", "BANK1", "RIG2"])

    def test_growth_allocation_failure_preserves_previous_title_and_recovers(self):
        self.render()
        Allocations.denied = {"TileGrid", "Label"}
        with self.assertRaises(MemoryError):
            self.render("A" * 100)
        self.assertEqual(visible_lines(self.display), ["CLEAN", "BANK1", "RIG2"])
        Allocations.denied.clear()
        self.render("CRUNCH", slot=3)
        self.assertEqual(visible_lines(self.display), ["CRUNCH", "BANK1", "RIG3"])

    def test_tuner_exit_restores_all_layout_rows(self):
        self.render()
        self.render(tuner="on", tuner_note="A", tuner_deviance=8192)
        self.assertIn("A", visible_lines(self.display))
        self.render("CRUNCH", slot=3, tuner="off")
        self.assertEqual(visible_lines(self.display), ["CRUNCH", "BANK1", "RIG3"])

    def test_custom_font_keeps_supported_fallback_and_updates_in_place(self):
        custom_font = BuiltinFont()
        # Share the atlas for the test observer only; identity differs, as BDF does.
        custom_font.bitmap = SYSTEM_FONT.bitmap
        display_module._FONT_CACHE["test-custom.bdf"] = custom_font
        try:
            layout = [dict(LAYOUT[0], font="test-custom.bdf")]
            self.display.render({"patch_name": "CLEAN"}, layout)
            before = Allocations.counts["Label"]
            self.assertGreater(before, 0)
            self.display.render({"patch_name": "CRUNCH"}, layout)
            self.assertEqual(Allocations.counts["Label"], before)
            self.assertEqual(visible_lines(self.display), ["CRUNCH"])
        finally:
            display_module._FONT_CACHE.pop("test-custom.bdf", None)

    def test_supported_unicode_preserved_and_unknown_character_is_readable(self):
        self.render("Café")
        self.assertEqual(visible_lines(self.display), ["Café", "BANK1", "RIG2"])
        self.render("A\U0001f3b8B")
        self.assertEqual(visible_lines(self.display), ["A?B", "BANK1", "RIG2"])

    def test_unicode_allocation_failure_does_not_change_prefix_of_visible_title(self):
        self.render("CLEAN")
        Allocations.denied = {"Glyph"}
        with self.assertRaises(MemoryError):
            self.render("Café")
        self.assertEqual(visible_lines(self.display), ["CLEAN", "BANK1", "RIG2"])
        Allocations.denied.clear()
        self.render("Café")
        self.assertEqual(visible_lines(self.display), ["Café", "BANK1", "RIG2"])

    def test_single_line_to_multiline_update_preserves_legacy_newline_behavior(self):
        self.render("CLEAN")
        self.render("CLEAN\nRIG")
        self.assertEqual(visible_lines(self.display), ["CLEAN", "RIG", "BANK1", "RIG2"])
        self.render("CRUNCH")
        self.assertEqual(visible_lines(self.display), ["CRUNCH", "BANK1", "RIG2"])

    def test_preview_badge_does_not_drop_or_reallocate_normal_rows(self):
        self.render(preview="on")
        root = self.display.display.root_group
        before = Allocations.counts.copy()
        self.assertIn("PREVIEW", visible_lines(self.display))
        for preview in ("off", "on", "off"):
            self.render("CRUNCH", slot=3, preview=preview)
            self.assertIs(self.display.display.root_group, root)
            self.assertEqual(Allocations.counts, before)
            lines = visible_lines(self.display)
            self.assertEqual(lines[:3], ["CRUNCH", "BANK1", "RIG3"])
            self.assertEqual("PREVIEW" in lines, preview == "on")

    def test_expression_field_does_not_reserve_or_shrink_title_width(self):
        name = "ABCDEFGHIJKLMNOPQRS"  # 228 px at system-font size 2.
        context = {"patch_name": name, "bank": 1, "slot": 2,
                   "expression_mode": "VOL"}
        self.display.render(context, EXPRESSION_LAYOUT)
        title = self.display._layout_labels[0]
        self.assertEqual(title.text, name)
        self.assertEqual(title.scale, 2)
        self.assertEqual(visible_lines(self.display)[0], name)
        title_cells = glyphs(title)
        self.assertEqual(max(x + w for x, y, w, h, c in title_cells), 236)

    def test_expression_context_without_layout_field_draws_no_implicit_badge(self):
        self.render(expression_mode="VOL")
        self.assertEqual(visible_lines(self.display), ["CLEAN", "BANK1", "RIG2"])
        self.assertEqual(painted_bitmaps(self.display.display.root_group), [])
        self.assertFalse(any(isinstance(item, display_module._ExpressionBadge)
                             for item in self.display._layout_labels))

    def test_expression_badge_is_anchored_and_freely_movable(self):
        context = {"patch_name": "CLEAN", "bank": 1, "slot": 2,
                   "expression_mode": "VOL"}
        self.display.render(context, EXPRESSION_LAYOUT)
        badge = self.display._layout_labels[-1]
        self.assertIsInstance(badge, display_module._ExpressionBadge)
        # Base bounds are 16 px icon + 2 px gap + 18 px text, by 14 px;
        # size 2 and bottom-right anchoring therefore place it exactly here.
        self.assertEqual(badge.bounding_box, (0, 0, 36, 14))
        self.assertEqual((badge.x, badge.y), (162, 206))
        self.assertEqual(painted_bitmaps(self.display.display.root_group)[0][:4],
                         (162, 208, 32, 24))
        mode_cells = [cell for cell in glyphs(self.display.display.root_group)
                      if cell[1] >= 200]
        self.assertEqual("".join(cell[4] for cell in mode_cells), "VOL")
        self.assertEqual(max(x + w for x, y, w, h, c in mode_cells), 234)
        self.assertEqual(max(y + h for x, y, w, h, c in mode_cells), 234)

        moved = [dict(LAYOUT[0]), {
            "field": "expression_mode", "x": 12, "y": 18, "size": 1,
            "color": "#12ab34",
        }]
        self.display.render(context, moved)
        badge = self.display._layout_labels[-1]
        self.assertEqual((badge.x, badge.y), (12, 18))
        self.assertEqual(badge.color, 0x12AB34)
        self.assertEqual(painted_bitmaps(self.display.display.root_group)[0][:4],
                         (12, 19, 16, 12))

    def test_multiple_expression_fields_render_independent_badges(self):
        layout = [
            {"field": "expression_mode", "x": 5, "y": 10},
            {"field": "expression_mode", "halign": "right",
             "valign": "bottom", "x": -6, "y": -6, "size": 2},
        ]
        self.display.render({"expression_mode": "WAH"}, layout)
        badges = [item for item in self.display._layout_labels
                  if isinstance(item, display_module._ExpressionBadge)]
        self.assertEqual(len(badges), 2)
        self.assertEqual([item.text for item in badges], ["WAH", "WAH"])
        self.assertEqual([(item.x, item.y) for item in badges],
                         [(5, 10), (162, 206)])
        self.assertEqual(len(painted_bitmaps(self.display.display.root_group)), 2)
        self.assertEqual(visible_lines(self.display), ["WAH", "WAH"])

    def test_expression_badge_honors_font_color_prefix_and_suffix(self):
        custom_font = BuiltinFont()
        custom_font.bitmap = SYSTEM_FONT.bitmap
        display_module._FONT_CACHE["badge-custom.bdf"] = custom_font
        try:
            spec = dict(EXPRESSION_SPEC, font="badge-custom.bdf",
                        color="#12ab34", prefix="[", suffix="]")
            self.display.render({"expression_mode": "VOL"}, [spec])
            badge = self.display._layout_labels[0]
            self.assertIs(badge._label.font, custom_font)
            self.assertEqual(badge.text, "[VOL]")
            self.assertEqual(badge.color, 0x12AB34)
            self.assertEqual(badge._label.color, 0x12AB34)
            self.assertEqual(visible_lines(self.display), ["[VOL]"])
        finally:
            display_module._FONT_CACHE.pop("badge-custom.bdf", None)

    def test_expression_unknown_or_missing_value_uses_placeholder(self):
        root = None
        before = None
        for expression_context in ({}, {"expression_mode": None},
                                   {"expression_mode": "MORPH"}):
            self.display.render(expression_context, [EXPRESSION_SPEC])
            badge = self.display._layout_labels[0]
            self.assertEqual(badge.text, "---")
            self.assertEqual(visible_lines(self.display), ["---"])
            self.assertTrue(badge._icon.hidden)
            if root is None:
                root = self.display.display.root_group
                before = Allocations.counts.copy()
            else:
                self.assertIs(self.display.display.root_group, root)
                self.assertEqual(Allocations.counts, before)

    def test_thousand_expression_mode_changes_allocate_nothing(self):
        context = {"patch_name": "CLEAN", "bank": 1, "slot": 2,
                   "expression_mode": "VOL"}
        self.display.render(context, EXPRESSION_LAYOUT)
        root = self.display.display.root_group
        before = Allocations.counts.copy()
        Allocations.denied = {"Group", "TileGrid", "Label", "Palette",
                              "Bitmap", "Glyph"}
        for index in range(1000):
            mode = "WAH" if index % 2 else "VOL"
            context["expression_mode"] = mode
            self.display.render(context, EXPRESSION_LAYOUT)
            self.assertIs(self.display.display.root_group, root)
            self.assertEqual(self.display._layout_labels[-1].text, mode)
        self.assertEqual(Allocations.counts, before)

    def test_volume_ramp_and_wah_side_profile_repaint_one_retained_bitmap(self):
        spec = dict(EXPRESSION_SPEC, prefix="WAH ", suffix=" VOL")
        self.display.render({"expression_mode": "VOL"}, [spec])
        badge = self.display._layout_labels[0]
        bitmap = badge._icon.bitmap
        volume = dict(bitmap.pixels)
        # The volume triangle grows from a single bottom pixel on the left
        # into a full-height right edge, with a horizontal baseline.
        self.assertEqual(sum(bitmap[1, y] for y in range(12)), 1)
        self.assertEqual(sum(bitmap[14, y] for y in range(12)), 9)
        self.assertTrue(all(bitmap[x, 10] for x in range(1, 15)))
        self.display.render({"expression_mode": "WAH"}, [spec])
        self.assertIs(badge._icon.bitmap, bitmap)
        self.assertNotEqual(bitmap.pixels, volume)
        # Side-view treadle rises to the right above a separate flat base.
        self.assertEqual((bitmap[1, 6], bitmap[14, 2]), (1, 1))
        self.assertTrue(all(bitmap[x, 10] for x in range(1, 15)))
        self.assertEqual(bitmap[12, 7], 0)
        self.display.render({"expression_mode": "VOL"}, [spec])
        self.assertEqual(bitmap.pixels, volume)
        self.display.render({}, [spec])
        self.assertTrue(badge._icon.hidden)
        self.assertIs(badge._icon.bitmap, bitmap)

    def test_expression_icon_allocation_failure_keeps_previous_frame(self):
        self.render()
        previous_root = self.display.display.root_group
        Allocations.denied = {"Bitmap"}
        with self.assertRaises(MemoryError):
            self.display.render({"patch_name": "CRUNCH", "bank": 2,
                                 "slot": 3, "expression_mode": "WAH"},
                                EXPRESSION_LAYOUT)
        self.assertIs(self.display.display.root_group, previous_root)
        self.assertEqual(visible_lines(self.display), ["CLEAN", "BANK1", "RIG2"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
