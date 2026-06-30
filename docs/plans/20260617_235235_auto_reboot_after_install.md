# Auto-reboot after install + conditional install link

Date: 2026-06-17
Version bump: 0.3.10 -> 0.3.11

## Problem

On a blank pedal, after the editor copies the firmware to CIRCUITPY,
CircuitPython does a *soft reload* (reruns code.py) but NOT boot.py. The
data USB CDC port the editor talks to is only enabled by boot.py, so it
never comes up and the editor cannot connect. The user had to manually
unplug/replug (hard reset) for boot.py to run.

## Changes

1. **Firmware self-heal (`firmware/code.py`)**
   - If `usb_cdc.data is None` AND `supervisor.runtime.run_reason ==
     AUTO_RELOAD`, call `microcontroller.reset()` once. A hard reset reruns
     boot.py, which enables the data CDC. Gated on AUTO_RELOAD so a real
     hard reset (STARTUP) can never loop here, and so editing files on an
     already-booted pedal (data already up) never triggers it.

2. **Editor welcome screen (`editor/src/App.svelte`)**
   - Remove the "Auto-detect will find the right port." sentence.
   - Show the "New pedal - never flashed before? Install firmware" link by
     default and hide it only when `detect_pedal()` gives positive evidence
     the firmware IS installed (a CIRCUITPY drive carrying the captain
     firmware tree). Bootloader, CircuitPython-without-firmware, and
     "nothing detected" all keep it visible (most discoverable for a
     first-time user). Driven by a 3s probe that runs only while
     disconnected.

3. **Version bump (all 4 files together)**
   - firmware/lib/captain/__init__.py
   - editor/src-tauri/tauri.conf.json
   - editor/src-tauri/Cargo.toml
   - editor/package.json

## Follow-up (same session): incompatible CircuitPython + auto-bootloader

A factory MIDI Captain ships CircuitPython 7.3.3. The old wizard, seeing a
CIRCUITPY drive already present, skipped the CircuitPython flash and
installed firmware on top - which crashes on boot (`ImportError: no module
named 'fourwire'`, a 9.0+ module). Added:

- `detect_pedal` reads `boot_out.txt`, parses the CircuitPython version, and
  sets `circuitpython_ok` (major must be 9). New phase `circuitpy_wrong_cp`
  takes priority over install/installed and refuses to install on top.
- `serial::reboot_to_bootloader`: drives the CircuitPython REPL over the
  console CDC (Ctrl-C, then `microcontroller.on_next_reset(RunMode.UF2)` +
  reset) to enter RPI-RP2 without the physical footswitch. Best effort;
  broadcasts to all open-able ports. Exposed as a "Reboot into bootloader"
  button in the `circuitpy_wrong_cp` phase, with manual fallback text.
- Welcome-screen "Install firmware" link reverted to always-visible; the
  drive probe added earlier was removed (user decision). The install wizard
  itself decides what the pedal needs.

## Notes / nuance

- Factory pedal plugged in normally (no bootloader, no CIRCUITPY) shows no
  install link, because the editor cannot tell it needs flashing until the
  user enters bootloader (RPI-RP2 appears). Matches the "show only when we
  know firmware is absent" instruction.
