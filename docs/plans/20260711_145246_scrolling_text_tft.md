# Scrolling text (marquee) for the TFT display + editor preview

Branch: `feature/scrolling-text-tft`

## Goal
Let a TFT label scroll horizontally (marquee) when its text is wider than the
screen, instead of being clipped. Configurable per-label in the editor and
faithfully previewed there.

## Data model (new optional layout-entry fields)
- `scroll: bool` - opt in to marquee for this label.
- `scroll_speed: int` - px/second (optional, default 40).

Backward compatible: entries without these render exactly as before.

## Firmware (`firmware/lib/captain/display.py`)
The main loop already ticks ~200 Hz (`app.py` `_tick_body`). Full `render()`
rebuilds the displayio Group and is expensive/deferred; but moving a single
label's `.x` is cheap (small dirty region, background auto-refresh).

- `render()`: after building each label, if `spec["scroll"]` and the text pixel
  width (`bounding_box[2] * scale`) exceeds `avail = SCREEN_W - 2*MARGIN`,
  re-anchor the label to the left margin and register it in `self._scroll` with
  its overflow span + speed. Reset scroll phase (`_scroll_start_ms = None`).
- `tick(now_ms)`: throttled (~25 fps); for each registered label compute a
  bounce offset (pause left, scroll to reveal end, pause right, scroll back) and
  set `label.x`. Time-based so it is smooth regardless of loop jitter.
- Clear `self._scroll` at the top of `render`, `_render_tuner`, `show_splash`,
  `show_patch` so stale label refs are never animated.

## Firmware (`firmware/lib/captain/app.py`)
- Call `self.display.tick(now_ms)` once per `_tick_body`. Cheap no-op when
  nothing scrolls.

## Firmware default layouts
- Enable `scroll: true` on `patch_name` in kemper + ampero DEFAULT_LAYOUT
  (size-5 patch names overflow past ~8 chars).

## Editor (`editor/src/components/TftLayout.svelte`)
- Extend `LayoutEntry` with `scroll?` + `scroll_speed?`.
- Add a "Scroll" checkbox + speed input to each entry's form row.
- Preview: a `marquee` Svelte action measures the inner text span vs the
  available width; on overflow it left-anchors the label and runs a WAAPI bounce
  animation mirroring the firmware timing (pause/scroll/pause/scroll).

## Version bump (per project rule: editor + firmware version-aligned)
Bump 0.3.29 -> 0.4.0 in the 4 files:
- `firmware/lib/captain/__init__.py`
- `editor/src-tauri/tauri.conf.json`
- `editor/src-tauri/Cargo.toml`
- `editor/package.json`

## Status
- [x] firmware display.py (scroll registration + tick + _scroll_offset)
- [x] firmware app.py tick call in main loop
- [x] default layouts scroll:true on patch_name (kemper + ampero)
- [x] editor schema + form (Scroll checkbox + Speed) + preview (marquee action)
- [x] version bump 0.3.29 -> 0.4.0 (4 files + Cargo.lock)
- [x] README Screen layout section
- [x] svelte-check 0 errors, py_compile OK, vitest 116 passed

## Verification notes
- Manifest streams `default_layout` as a whole JSON blob, so the new `scroll`
  key flows to the editor with no protocol change.
- Firmware animates only `label.x` (small dirty region), gated to ~25 fps, so it
  doesn't reintroduce the MIDI-buffer-overflow the deferred render avoids.
- Not yet exercised on real hardware (no device in this session).
