# Raspberry Pi 3 MIDI hub + external Stage display

Branch: `feature/rpi-midi-hub`

## Goal

Build a standalone appliance around a Raspberry Pi 3 that takes over the
"computer in the loop" role Bosun currently needs, and adds a dedicated
stage screen:

1. **USB host / MIDI hub.** The Pi hosts USB for both the MIDI Captain
   (running Bosun firmware) and the Kemper Player, and relays MIDI between
   them in both directions (including SysEx). This is the job currently
   done by a PC (`tools/midi_bridge.py` / editor `midi.rs`) or an Android
   phone (`BosunMidiBridge.kt`).
2. **Bosun protocol link.** The Pi keeps a connection to the Captain's
   data USB-CDC port and exposes the Bosun line-JSON protocol on the LAN,
   so the desktop/Android editor can connect over WiFi (`tcpConnect`)
   with no cable.
3. **External Stage display.** The Pi drives a wide HDMI panel
   (reference: Waveshare 8.8" Side Monitor, 480x1920 native) showing the
   same **Stage View** currently used on Android, fed live from the
   Captain.

Target outcome: plug in power, the Captain, the Kemper and the monitor;
the rig is playable and the stage screen is live, no laptop or phone
required.

## Hardware

| Part | Notes |
|---|---|
| Raspberry Pi 3B / 3B+ | 1 GB RAM, quad A53. **See risk R1** - a Pi 4 (2 GB+) is strongly recommended for the browser-rendered Stage View. Keep the software Pi-3-compatible regardless. |
| Waveshare 8.8" Side Monitor | 480x1920 IPS, HDMI in + USB for power, **no touch**. Needs a custom HDMI mode (`hdmi_timings`) and a display rotation for landscape 1920x480. Vendor recommends its own 5 V supply rather than drawing from the Pi. [wiki](https://www.waveshare.com/wiki/8.8inch_Side_Monitor) |
| Powered USB hub | Almost certainly required. The Pi 3 caps total USB current low, and its USB + Ethernet share one 480 Mbps bus. The Android bridge already documents needing a powered hub for simultaneous Kemper + Captain. |
| 5 V PSU(s) | One solid supply for the Pi, one for the monitor (or a single high-current supply feeding a powered hub). Power sequencing matters: the bridge must survive the Captain being powered after the Pi. |
| microSD | 16 GB+. A2 card if possible; the browser is I/O sensitive. |

Cabling: Captain USB (1 MIDI-class interface + 2 CDC-ACM interfaces on
one connector), Kemper Player USB (MIDI-class only, no DIN), monitor
HDMI + monitor USB power.

## Distro selection

The user asked for an existing open-source distro that "acts as a MIDI
hub". Findings:

- **RaspiMIDIHub** (`github.com/wamdam/raspimidihub`, fresh 2026 image,
  GPL-3.0). Rejected: it is a locked appliance - read-only rootfs,
  "do not install on a Pi you use for other purposes", **no HDMI/desktop
  output** (config is a phone web UI over its own WiFi AP), and it routes
  **USB-MIDI-class devices only** - it has no concept of the Captain's
  USB-CDC data port. It cannot host the Stage display or the protocol
  link.

- **Patchbox OS** (`github.com/BlokasLabs/patchbox-os-gen`, by Blokas).
  This is *the* open-source "MIDI hub" distro: Raspberry Pi OS
  (now Bookworm arm64) with a low-latency kernel, a module system, and
  Blokas' auto-connection tooling (`amidiauto` / `amidiminder`)
  preinstalled. It stays a normal apt/systemd system, so a Chromium
  kiosk and our bridge service layer on cleanly. Supports Pi 3B/3B+.
  Downsides: it bundles a lot we do not need on a 1 GB Pi (JACK session
  tooling, Pd, SuperCollider, MODEP modules), and its Bookworm image was
  still beta as of 2024.

- **Raspberry Pi OS Bookworm Lite, 64-bit** (recommended base). The most
  current and best-supported option. We add exactly three things:
  1. `amidiminder` (rule-based ALSA auto-connect) from `apt.blokas.io` -
     this is the same component Patchbox uses, and rule-based beats
     `amidiauto`'s all-to-all wiring for our fixed 2-device topology.
  2. `cage` (a minimal Wayland kiosk compositor) + Chromium.
  3. our `bosun-hub` service (below).

**Decision:** base on **Raspberry Pi OS Bookworm Lite 64-bit + amidiminder**
(a minimal purpose-built equivalent of Patchbox). If a turnkey MIDI distro
is preferred, **Patchbox OS** is a drop-in alternative base - the layered
software is identical because it is the same Bookworm/apt/systemd
underneath. Both are open source. Decision D1 confirms which we ship.

## Current architecture (as built today)

Established by codebase exploration on this branch:

- **MIDI relay.** The Captain firmware itself generates the Kemper
  "beacon" (SysEx function `0x7E`, re-sent every 5 s) on its own USB-MIDI
  out - see `firmware/lib/plugins/kemper.py` `tick()`. The host does
  **not** need to synthesize any MIDI. It only needs to forward raw bytes
  both ways, pass SysEx through untouched, and drop only MIDI clock
  (`0xF8`) and active sensing (`0xFE`). `tools/midi_bridge.py`,
  `editor/src-tauri/src/midi.rs` and `BosunMidiBridge.kt` are three
  existing implementations of exactly this.
- **Bosun protocol.** Newline-delimited JSON over the Captain's second
  USB-CDC interface (`/dev/ttyACM*` on Linux; the higher interface number
  is the data port, the lower is the REPL console). Framing is a raw byte
  stream: no length prefix, no checksum. Large responses
  (`GET_MANIFEST` ~22 KB, `GET_GLOBAL`, `GET_PATCH`) are streamed
  field-by-field by the firmware.
- **TCP transport already exists.** `editor/src-tauri/src/tcp_serial.rs`
  plus the `tcpConnect()` export in `editor/src/lib/protocol.ts`: the
  editor is a TCP client that tunnels the same byte stream, default port
  **9876**. `tools/serial_tcp_bridge.py` is the matching PC-side
  serial<->TCP shim. Connecting from the editor is `tcp://<host>:9876`.
- **Stage View is 100% frontend.** `editor/src/components/StageView.svelte`
  is a self-contained Svelte component. Give it 6 props
  (`deviceInfo`, `manifest`, `device` = `device.json`, `connected`,
  `patches`, `onExit`) and a stream of firmware messages
  (`CONTEXT` pushes + `PATCH` + `EVENT`/`binding_fired`) and it renders.
  It reads live state from `context.kemper_rig_name`, `context.kemper_bpm`,
  `context.kemper_tuner*`, `context["kemper_block_<A..REV>"]`, and the
  preset-nav row from `device.preset_navigation` cross-referenced with
  `patches`.
- **Dev harness exists.** `editor/stage-preview.html` +
  `editor/dev-preview/stage-preview.ts` + `editor/vite.stage-preview.config.ts`
  (`npm run stage:preview`, port 4732) mount StageView in a bare browser
  with two tiny shims (`dev-preview/tauri-core-shim.ts`,
  `tauri-event-shim.ts`) that replace `invoke("drain_inbox")` and
  `listen("firmware-data-ready")`. This is the template for the kiosk
  build - it just needs a real transport instead of on-page controls.
- **No ARM Linux build.** Tauri desktop targets are Windows,
  macOS-aarch64, and Linux-**x86_64** only. Android covers the ARM ABIs.
  There is no aarch64/armv7 Linux Rust target, no GTK/WebKit ARM
  packaging. The Rust I/O crates (`serial2`, `serialport`, `midir`/ALSA)
  are portable; the Tauri webview packaging is the missing piece.

## Proposed architecture (Approach A - headless bridge + browser kiosk)

```
                      Raspberry Pi (Bookworm Lite)
  +--------------------------------------------------------------+
  |                                                              |
  |  ALSA seq  Captain USB-MIDI  <--amidiminder-->  Kemper USB-MIDI
  |            (rule-based auto-connect on hotplug, kernel-level) |
  |                                                              |
  |  bosun-hub.service (Python)                                   |
  |    - opens /dev/ttyACM* data port (sentinel sync, self-heal) |
  |    - serves Bosun protocol over:                             |
  |         * raw TCP  :9876   (LAN editor via tcpConnect)       |
  |         * WebSocket :8080/ws  (the kiosk)                    |
  |    - serves the static "stage bundle" on :8080/              |
  |                                                              |
  |  cage + chromium --kiosk http://localhost:8080/              |
  |    -> HDMI -> Waveshare 8.8" (1920x480 landscape)            |
  +--------------------------------------------------------------+
        |                    |                         |
     USB (MIDI+CDC)       USB (MIDI)                 HDMI
     MIDI Captain         Kemper Player          Waveshare monitor
```

Why this shape:

- **MIDI routing is not our code.** ALSA sequencer + `amidiminder`
  connects the two USB-MIDI endpoints in-kernel with zero added latency.
  SysEx and the Kemper sensing frames pass natively. We add at most a
  small clock/active-sensing filter if the Kemper floods clock (a
  `aseqdump` check during bring-up decides this; a userspace filter port
  is the fallback).
- **Reuse the Stage View unchanged.** The kiosk runs the existing Svelte
  component. The only new frontend code is a WebSocket transport shim
  (a productionized `dev-preview` shim) and a thin connection bootstrap.
- **No Tauri/ARM build.** Avoids WebKitGTK on a 1 GB Pi and a whole new
  CI target.
- **LAN editing comes for free.** Because `bosun-hub` also speaks raw TCP
  on 9876, the desktop/Android editor connects with
  `tcp://<pi>:9876` today, no new editor code - you can re-tweak patches
  from the couch while the Pi hosts the rig.

### Components / work breakdown

**1. `tools/rpi-hub/` - the appliance (new)**

- `bosun-hub` Python service:
  - Open the Captain data CDC. Port discovery: enumerate `/dev/ttyACM*`,
    pick the data interface (reuse the `sort_ports_desc` logic from
    `serial/android_helpers.rs` - highest interface number is data; the
    console must not be opened or it resets the RP2040).
  - Sentinel PING/ACK sync on connect and drop stale backlog (port the
    proven logic from `serial_tcp_bridge.py` + `serial/desktop.rs`).
  - Self-heal: wall-clock stall + write-only-stall detection, close/reopen
    with a DTR toggle, wait for re-enumeration (port the hardening from
    the Android I/O-thread work - see memory
    `project_android_serial_architecture`).
  - Idle keepalive PING (~6 s) so a silent Stage link does not look dead.
  - Fan the byte stream out to N clients: raw TCP `:9876` and a WebSocket
    endpoint. Writes from any client are serialized to the port.
  - Serve the static stage bundle over HTTP on the same port.
  - One connection at a time to the port; many readers. Backpressure:
    drop-oldest on a slow WS client, never block the port reader.
- `systemd` units: `bosun-hub.service`, `bosun-kiosk.service`
  (cage + chromium), `amidiminder` rule file for Captain<->Kemper.
- `install.sh` on top of stock Raspberry Pi OS Lite: apt deps, Blokas
  repo + `amidiminder`, `cage`/`chromium`, HDMI mode config, enable
  services. Idempotent, re-runnable for updates.
- `config/` : `/boot/firmware/config.txt` snippet for the Waveshare mode,
  a udev rule so `/dev/ttyACM*` is stable, optional WiFi-AP config
  (decision D3).
- `README.md` with wiring diagram, power notes, flashing steps.

**2. `editor/` - the stage kiosk build (new build target, no runtime changes to StageView)**

- `editor/stage-kiosk.html` + `editor/vite.stage-kiosk.config.ts` +
  `editor/kiosk/` :
  - `ws-transport.ts` - a real implementation of the `@tauri-apps/api`
    surface StageView touches, backed by a WebSocket to
    `bosun-hub`: `invoke("drain_inbox")`, `invoke("send_command", ...)`,
    `invoke("is_connected")`, and `listen("firmware-data-ready")`.
    Auto-reconnect with backoff; re-run the bootstrap on reconnect.
  - `bootstrap.ts` - on connect, issue `GET_DEVICE_INFO`, `GET_MANIFEST`
    (streamed), `GET_GLOBAL`, `LIST_PATCHES`, then `mount(StageView, ...)`
    with the resulting props (mirrors the relevant slice of
    `App.svelte` `refetchAll()`). Feed subsequent `CONTEXT`/`PATCH`/`EVENT`
    lines through the same bus StageView already subscribes to.
  - `?lite` / a query flag to drop the expensive ambient-glow blur layers
    for Pi 3 (see risk R1).
- `package.json` : `"build:stage": "vite build --config vite.stage-kiosk.config.ts"`.
- Output is plain static files; `bosun-hub` serves them. Nothing here
  ships in the desktop/Android app.
- The existing `dev-preview` harness stays as-is for local design work;
  the kiosk build is its production sibling.

**3. Tests**

- `tools/rpi-hub/tests/` - unit tests for port selection, sentinel sync,
  fan-out/backpressure, self-heal state machine (host-runnable, no Pi).
  Reuse `tools/tcp_firmware_emulator.py` as the fake pedal.
- `editor/tests/` - `ws-transport` reconnect/bootstrap tests against a
  mock WebSocket; a StageView render test fed from a recorded
  `CONTEXT`/`PATCH` capture (`dev-preview/device-full.log` is a real one).
- Manual bring-up checklist on hardware (HW rig available - see memory
  `project_hw_test_rig`): `aseqdump` shows Captain<->Kemper both ways,
  rig change on the Player front panel follows on the Captain and the
  Stage screen, tuner/BPM/effect-block LEDs mirror, kiosk survives a
  Captain power-cycle and a Pi reboot.

**4. Docs**

- `README.md` : new "Raspberry Pi hub" section under "How it fits
  together" with the topology diagram.
- Cross-link from the Stage and Connecting sections.

### Data the Stage kiosk needs from the hub

StageView needs more than the live `CONTEXT` stream: `manifest` (message
labels), `device.json` (`preset_navigation`, `bank_colors`), and
`patches` (slot names for the nav row). The bootstrap step fetches these
once per (re)connect. Manifest streaming must work over WS - it does,
since WS carries the identical newline-JSON lines.

## Alternatives considered

**B. Cross-compile the Tauri editor for aarch64 Linux, run it kiosked.**
Gives the full editor on the Pi. Rejected as the primary path: a new
Rust target + WebKitGTK ARM packaging + CI runner, and WebKitGTK on a
1 GB Pi 3 is heavy. Revisit only if "full editor on the panel" becomes a
real requirement; Approach A already gives LAN editing from a real
computer.

**C. Pi as MIDI hub + TCP bridge only; Stage View on a separate tablet.**
This already works today (`tcp://<pi>:9876` from the Android app or
desktop editor) with zero new code. It does not satisfy the "external
HDMI panel on the Pi" goal, but Approach A delivers C as a side effect,
so both are available.

## Decisions needed (D)

- **D1.** Base distro: Raspberry Pi OS Bookworm Lite 64-bit + amidiminder
  (recommended), or Patchbox OS (turnkey MIDI distro, heavier)?
- **D2.** Pi 3 as a hard constraint, or is a Pi 4 acceptable for the
  build target? (Affects how much effort goes into the `?lite`
  render path and whether Chromium is even the renderer - see R1.)
- **D3.** Networking: Pi joins existing WiFi, runs its own access point
  (self-contained at a venue), or both (AP + optional uplink)?
- **D4.** Deliverable form: an `install.sh` on top of stock Raspberry Pi
  OS, or a prebuilt flashable image (pi-gen based) as a release artifact?
- **D5.** Does the hub expose the protocol on the LAN unauthenticated
  (simple, same as `tcp://` today) or gated (token / bound to the AP
  interface only)? It can push patches and reboot the pedal.

## Risks (R)

- **R1 - browser rendering on Pi 3.** The Stage View is 1920 px wide with
  marquee scrolling and layered blur "ambient glow". Chromium on a Pi 3
  may not hold 60 fps. Mitigations: a `?lite` mode that drops the blur
  layers; test early on real hardware; if unacceptable, either mandate
  Pi 4 (D2) or render Stage with a lighter engine. **Prototype this
  first, before building the appliance.**
- **R2 - Waveshare HDMI mode on Bookworm KMS.** Bookworm defaults to the
  KMS driver, where the legacy `hdmi_timings` path is fragile. May need a
  `video=HDMI-A-1:...` cmdline mode or a custom EDID, plus a compositor
  output transform for landscape. Budget bring-up time.
- **R3 - Pi 3 USB/LAN shared bus.** Two USB-MIDI devices + CDC + Ethernet
  on one 480 Mbps bus. A powered hub and using WiFi (not Ethernet) for
  the LAN link should keep MIDI latency sane; measure it.
- **R4 - ALSA seq real-time forwarding.** Confirm with `aseqdump` that a
  plain `amidiminder` connection passes Kemper SysEx and the `0x7E`
  sensing frames intact and does not choke on clock. Userspace filter is
  the fallback.
- **R5 - power sequencing.** Captain powered after the Pi, Kemper
  unplugged mid-set, etc. The `bosun-hub` self-heal and `amidiminder`
  hotplug rules must cover every order. Explicit test matrix in bring-up.

## Out of scope

- No firmware / on-pedal TFT changes. The Captain already emits
  everything the hub needs (`CONTEXT` pushes, the Kemper beacon).
  If this holds, no firmware version bump is triggered
  (`feedback_firmware_changes_bump_version` does not apply).
- No changes to `StageView.svelte` behavior. New theming work in flight
  on `feature/stage-view-theme` is independent; this branch consumes
  whatever Stage View is at merge time.
- No new editor desktop/Android features. The kiosk build is a separate
  static artifact.
- DIN MIDI. The Kemper Player has none; everything here is USB.

## Suggested sequencing

1. **Spike R1**: on whatever Pi is on hand, `npm run stage:preview`
   served to Chromium kiosk on the Waveshare, driven by
   `dev-preview/device-full.log` replayed. Decide D2.
2. Bring up the MIDI hub: distro + `amidiminder`, verify Captain<->Kemper
   with `aseqdump` and a real rig change (R4).
3. `bosun-hub` service: CDC open + TCP `:9876`, verify from the desktop
   editor over `tcp://`.
4. Add the WS endpoint + static serving; build `editor` stage-kiosk
   bundle; wire the kiosk.
5. Harden: self-heal, power-sequencing matrix (R5), auto-start on boot.
6. Package (D4), document, demo on the HW rig.
