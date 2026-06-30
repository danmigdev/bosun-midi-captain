# In-editor MIDI bridge (Kemper Player <-> pedal)

Date: 2026-06-20
Goal: relay USB-MIDI between the Kemper (Player) and the pedal from inside the
editor, so MIDI Learn capture (and the whole bidirectional integration) works
without running tools/midi_bridge.py by hand.

## Why
- Kemper Player is USB-MIDI only; pedal is a USB device (not host). They can't
  connect directly, so the PC must relay. The editor backend currently does
  serial/CDC only - no MIDI at all. That's why capture looks dead out of the box.
- DIN-capable Kempers can wire DIN OUT -> pedal DIN IN and skip all this; this
  feature targets the USB-only Player case.

## Behaviour (mirror tools/midi_bridge.py)
- Forward every message both ways: Kemper-in -> pedal-out, pedal-in -> Kemper-out.
- Forward SYSEX verbatim (carries the Kemper bidirectional protocol).
  IMPORTANT: midir ignores SYSEX by default -> must call ignore(Ignore::None).
- Drop clock (0xF8) and active-sensing (0xFE) to keep the link quiet.
- Auto-detect ports by name: Kemper = "profiler"/"kemper", pedal =
  "circuitpython"/"bosun". Optional substring overrides from the UI.
- Does NOT touch the editor's CDC serial (separate USB interface) - both run together.

## Backend (Rust)
- Add dep: midir = "0.10" (winmm backend on Windows, no extra system deps).
- New module src-tauri/src/midi.rs:
  - MidiState { bridge: Mutex<Option<BridgeHandle>> } (managed in main.rs).
  - BridgeHandle holds the two MidiInputConnection (drop = stop). Output conns
    are moved into the input callbacks (kept alive by them).
  - Commands: midi_list_ports, midi_bridge_start{kemper?,pedal?},
    midi_bridge_stop, midi_bridge_status.
- Register the 4 commands + manage(MidiState) in main.rs.

## Frontend
- protocol.ts: wrappers midiListPorts/midiBridgeStart/midiBridgeStop/midiBridgeStatus
  + BridgeStatus type.
- App.svelte: bridgeActive state + startBridge/stopBridge/refreshBridge; auto-start
  (best-effort, toast on failure) when toggleLearn() starts learning; refresh on
  entering the learn page. Pass state + handlers to MidiLearn.
- MidiLearn.svelte: status row ("Bridge: active Kemper<->pedal" / "off") with a
  Start/Stop button and a one-line hint (needed for USB-only Kemper Player).

## Validation
- svelte-check (frontend).
- cargo build (Rust compiles, midir links).
- If a pedal+Kemper are present: toggle bridge on, Start learn, change a rig,
  confirm a capture appears; screenshot.

## Progress
- [x] Cargo dep midir 0.10 added.
- [x] src-tauri/src/midi.rs (start/stop/status/list, SYSEX via ignore(None),
      drop clock+AS, auto-detect profiler/circuitpython).
- [x] main.rs: manage(MidiState) + 4 commands registered.
- [x] protocol.ts wrappers + types.
- [x] App.svelte: bridge state, start/stop/refresh, auto-start on toggleLearn,
      stop on disconnect, refresh on learn page; props to MidiLearn.
- [x] MidiLearn.svelte: bridge status row + Start/Stop button + styles.
- [x] svelte-check: 0 errors. cargo check: OK (midir links, winmm backend).
- [x] MIDI ports confirmed present: "Profiler 0/1", "CircuitPython Audio 1/2".
- [x] Portable rebuilt (release, 1m11s) + launched with debug port.
- [x] LIVE VERIFY: Start bridge -> "Bridge on: Profiler <-> CircuitPython Audio"
      + success toast (30_/31_). Real Kemper PCs (ch1 bank0 PC0..4, port usb)
      captured end-to-end through the bridge and shown with Assign buttons (32_).
- [x] Connection stability: editor stays Connected with the bridge running (the
      USB-MIDI interface doesn't disturb the CDC link).
- [x] Copy refined per user: explain the bridge relays Kemper<->pedal THROUGH the
      PC, only needed when both are on USB and can't see each other; NOT needed
      when connected directly (USB-MIDI link or DIN cable). svelte-check clean.
- [x] Rebuild #2 (copy change) + screenshot new hint (33_): renders as intended.

## Status
- [x] COMPLETE
