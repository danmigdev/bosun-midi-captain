# Scaffolding plan - firmware + editor

Date: 2026-06-14 10:53:29

## Goal

Stand up an empty-but-runnable skeleton for both halves of the project so subsequent
iteration has somewhere to land. No real FSM, no real protocol dispatch yet - stubs
that boot, expose the module structure, and prove the wiring choices from the design
phase.

References:
- `memory/project_firmware_v1_design.md` - locked architectural decisions
- `memory/project_protocol_and_schema.md` - protocol + JSON schema
- `memory/hardware_paint_audio_midi_captain_std.md` - pin map (PySwitch-derived,
  pin numbers for footswitches still need verification on actual hardware)

## Layout

```
midi_captain/
  docs/
    plans/                                # this folder
  firmware/                               # copied to CIRCUITPY root
    boot.py                               # enables dual USB CDC + USB MIDI
    code.py                               # entry point, runs Captain.run()
    lib/captain/
      __init__.py
      app.py                              # main loop, glues subsystems
      board.py                            # pin constants (TODO: verify GP numbers)
      bindings.py                         # binding registry + per-switch FSM stubs
      config.py                           # JSON load/save
      display.py                          # ST7789 TFT init + splash
      leds.py                             # WS2812 NeoPixel strip
      midi.py                             # USB MIDI + DIN UART MIDI engines
      protocol.py                         # USB CDC line-JSON parser + dispatch
    config/
      device.json                         # globals (defaults)
      midi_learn.json                     # PC -> Captain patch (empty)
      patches/01/01.json                  # seed patch
  editor/                                 # Tauri app
    package.json
    vite.config.ts
    tsconfig.json
    svelte.config.js
    index.html
    src/
      main.ts
      App.svelte                          # placeholder UI with Connect button
      lib/
        protocol.ts                       # Tauri invoke() wrappers
    src-tauri/
      Cargo.toml
      tauri.conf.json
      build.rs
      src/
        main.rs                           # Tauri runtime + command registration
        serial.rs                         # serialport stub (list ports, open, send)
    SETUP.md                              # Rust install + dev workflow
    .gitignore
```

## Frontend stack choice

Svelte 5 + TypeScript + Vite. Reasons:
- Small bundle, no virtual DOM overhead
- Reactive primitives map cleanly to "live edit a value, see firmware respond"
- Quick to iterate; cheap to swap to React later if Svelte ergonomics bite

If user prefers React: only `package.json`, `vite.config.ts`, `App.svelte` → `App.tsx`
need rewriting; protocol layer and Rust side are framework-agnostic.

## Toolchain status

- Node 20.19.5 + npm 11.8.0 - present
- Rust + Cargo - **MISSING**, install via rustup-init.exe before `npm run tauri dev`
- WebView2 - assumed present on Win11; SETUP.md mentions fallback

## Pin map TODOs

`firmware/lib/captain/board.py` uses placeholder GP numbers for the 10 footswitches.
Real values must be sourced from `Tunetown/PySwitch` (`lib/pyswitch/hardware/devices/
pa_midicaptain_10_v2.py`) or verified directly on hardware. Marked with `# TODO:
verify` so this is obvious.

## Out of scope for this scaffolding pass

- Real switch FSM (Tap/Latched/Momentary/LongPress/DoubleTap state machines)
- Real protocol dispatch (only `PING` works in this pass)
- Real binding execution (logs "would fire" instead of sending MIDI)
- Hardware sanity test (user opted to skip)
- Tauri serialport plugin (stubbed; real impl after Rust is installed)
- Editor UI for binding editing (placeholder Connect button only)

These all land in subsequent passes on top of the skeleton.
