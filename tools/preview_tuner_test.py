#!/usr/bin/env python3
"""Offline tests for the app-level preset-preview + tuner-exit logic.

Exercises the REAL methods on captain.app.Captain (preview_step / preview_commit
/ preview_cancel / _resolve_preview_timeout / _tuner_is_on / _exit_tuner) against
a lightweight fake instance - no CircuitPython runtime, no hardware. The methods
are called unbound (Captain.method(fake, ...)) so we cover the shipping code
without running the heavy __init__.

Usage
-----
    python tools/preview_tuner_test.py
"""
import sys
import types
from pathlib import Path

FIRMWARE_LIB = Path(__file__).resolve().parent.parent / "firmware" / "lib"
sys.path.insert(0, str(FIRMWARE_LIB))

# Stub the CircuitPython surface captain.app imports (directly/transitively).
for mod_name in ("busio", "usb_midi", "usb_cdc", "digitalio", "board", "neopixel",
                 "displayio", "fourwire", "pwmio", "terminalio", "analogio",
                 "adafruit_display_text", "adafruit_st7789",
                 "adafruit_bitmap_font", "microcontroller"):
    sys.modules.setdefault(mod_name, types.ModuleType(mod_name))
import board  # noqa: E402
for _n in [f"GP{i}" for i in range(30)]:
    setattr(board, _n, _n)
import adafruit_display_text  # noqa: E402
adafruit_display_text.label = types.ModuleType("adafruit_display_text.label")
adafruit_display_text.label.Label = lambda *a, **kw: None
sys.modules["adafruit_display_text.label"] = adafruit_display_text.label

# Class/attr names the firmware `from`-imports (import must succeed; we never
# instantiate these since we skip __init__).
sys.modules["adafruit_st7789"].ST7789 = object
sys.modules["neopixel"].NeoPixel = object
sys.modules["neopixel"].GRB = "GRB"
sys.modules["analogio"].AnalogIn = object
sys.modules["digitalio"].DigitalInOut = object
sys.modules["digitalio"].Direction = types.SimpleNamespace(INPUT="in", OUTPUT="out")
sys.modules["digitalio"].Pull = types.SimpleNamespace(UP="up", DOWN="down")
sys.modules["terminalio"].FONT = object()

from captain.app import Captain  # noqa: E402


PASS = 0
FAIL = 0
FAILS = []


def test(name):
    def wrap(fn):
        global PASS, FAIL
        try:
            fn()
            PASS += 1
            print("  PASS ", name)
        except AssertionError as e:
            FAIL += 1
            FAILS.append("%s: %s" % (name, e))
            print("  FAIL ", name, "-", e)
        except Exception as e:  # noqa: BLE001
            FAIL += 1
            FAILS.append("%s: %s: %s" % (name, type(e).__name__, e))
            print("  ERROR", name, "-", type(e).__name__, e)
        return fn
    return wrap


class FakePatches:
    def __init__(self, patches):
        # patches: {(bank, slot): {"name": ...}}
        self._p = patches

    def list(self):
        return [{"bank": b, "slot": s} for (b, s) in self._p]

    def get(self, bank, slot):
        return self._p.get((bank, slot))

    def has(self, bank, slot):
        return (bank, slot) in self._p


class FakePlugins:
    def __init__(self):
        self.preview_calls = []
        self.tuner_off_calls = 0

    def on_preview(self, app, bank, slot):
        self.preview_calls.append((bank, slot))

    def tuner_off(self, app):
        self.tuner_off_calls += 1


def make_fake(patches=None, device=None, context=None):
    """A minimal object with just what the preview/tuner methods touch."""
    if patches is None:
        patches = {(1, 1): {"name": "A"}, (1, 2): {"name": "B"}, (2, 1): {"name": "C"}}
    fake = types.SimpleNamespace()
    fake._preview = None
    fake.current_bank = 1
    fake.current_slot = 1
    fake.device = device if device is not None else {}
    fake.display_context = context if context is not None else {"patch_name": "A", "bank": 1, "slot": 1}
    fake.patches = FakePatches(patches)
    fake.plugins = FakePlugins()
    fake._time = 1000
    fake._now_ms = lambda: fake._time
    fake._dirty = 0

    def mark_dirty():
        fake._dirty += 1
    fake._mark_display_dirty = mark_dirty

    fake.switch_calls = []

    def switch_patch(bank, slot, source="editor", fire_on_enter=True):
        fake.switch_calls.append((bank, slot, source))
        fake.current_bank, fake.current_slot = bank, slot
        return True
    fake.switch_patch = switch_patch

    def update_context(updates):
        if not updates:
            return
        fake.display_context.update(updates)
        fake._dirty += 1
    fake.update_context = update_context

    # _resolve_preview_timeout calls self.preview_commit()/preview_cancel() as
    # bound methods - route them to the real Captain methods on this fake.
    fake.preview_commit = lambda: Captain.preview_commit(fake)
    fake.preview_cancel = lambda: Captain.preview_cancel(fake)
    return fake


# ---------------- preview ----------------

@test("preview_step enters preview, shows target, sends NO device MIDI/switch")
def _():
    f = make_fake()
    ok = Captain.preview_step(f, 1, "patch")
    assert ok is True
    assert f._preview is not None
    assert (f._preview["bank"], f._preview["slot"]) == (1, 2)
    assert f.display_context["patch_name"] == "B"
    assert f.display_context["preview"] == "on"
    # No real load happened while previewing.
    assert f.switch_calls == []
    # Plugin got a chance to fill its own fields.
    assert f.plugins.preview_calls == [(1, 2)]
    # current patch is untouched.
    assert (f.current_bank, f.current_slot) == (1, 1)


@test("preview_step snapshots context once, keeps stepping from cursor")
def _():
    f = make_fake()
    Captain.preview_step(f, 1, "patch")           # -> (1,2)
    saved = f._preview["saved_context"]
    Captain.preview_step(f, 1, "patch")           # -> (2,1)
    assert (f._preview["bank"], f._preview["slot"]) == (2, 1)
    # saved_context is the original, not re-snapshotted from the preview view.
    assert f._preview["saved_context"] is saved
    assert "preview" not in saved
    assert saved["patch_name"] == "A"


@test("preview_commit loads the previewed patch for real")
def _():
    f = make_fake()
    Captain.preview_step(f, 1, "patch")           # -> (1,2)
    ok = Captain.preview_commit(f)
    assert ok is True
    assert f._preview is None
    assert f.switch_calls == [(1, 2, "binding")]


@test("preview_cancel restores the pre-preview context")
def _():
    f = make_fake()
    Captain.preview_step(f, 1, "patch")           # -> (1,2), context now shows B + preview
    assert f.display_context.get("preview") == "on"
    ok = Captain.preview_cancel(f)
    assert ok is True
    assert f._preview is None
    assert f.display_context.get("preview") is None
    assert f.display_context["patch_name"] == "A"
    assert f.switch_calls == []


@test("preview_commit / cancel are no-ops when not previewing")
def _():
    f = make_fake()
    assert Captain.preview_commit(f) is False
    assert Captain.preview_cancel(f) is False


@test("preview_step no-op when there are no patches")
def _():
    f = make_fake(patches={})
    assert Captain.preview_step(f, 1, "patch") is False
    assert f._preview is None


@test("timeout resolves to commit by default")
def _():
    f = make_fake()
    Captain.preview_step(f, 1, "patch")           # -> (1,2)
    Captain._resolve_preview_timeout(f)
    assert f.switch_calls == [(1, 2, "binding")]
    assert f._preview is None


@test("timeout resolves to cancel when configured")
def _():
    f = make_fake(device={"preview": {"on_timeout": "cancel"}})
    Captain.preview_step(f, 1, "patch")           # -> (1,2)
    Captain._resolve_preview_timeout(f)
    assert f.switch_calls == []
    assert f._preview is None
    assert f.display_context["patch_name"] == "A"


@test("preview timeout window honors device.preview.timeout_ms")
def _():
    f = make_fake(device={"preview": {"timeout_ms": 500}})
    Captain.preview_step(f, 1, "patch")
    assert f._preview["until_ms"] == 1000 + 500


# ---------------- tuner ----------------

@test("_tuner_is_on reads generic + kemper fields")
def _():
    f = make_fake(context={"tuner": "off"})
    assert Captain._tuner_is_on(f) is False
    f.display_context["tuner"] = "on"
    assert Captain._tuner_is_on(f) is True
    f.display_context = {"kemper_tuner": "on"}
    assert Captain._tuner_is_on(f) is True


@test("_exit_tuner tells plugins and clears local tuner context")
def _():
    f = make_fake(context={"tuner": "on", "kemper_tuner": "on", "patch_name": "A"})
    Captain._exit_tuner(f)
    assert f.plugins.tuner_off_calls == 1
    assert f.display_context["tuner"] == "off"
    assert f.display_context["kemper_tuner"] == "off"
    # unrelated fields untouched
    assert f.display_context["patch_name"] == "A"


def main():
    print()
    if FAILS:
        print("%d FAILURE(S):" % len(FAILS))
        for m in FAILS:
            print("  -", m)
        return 1
    print("%d passed, 0 failed" % PASS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
