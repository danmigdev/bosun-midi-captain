# Firmware: build and installation

Bosun's MIDI Captain firmware is written in C. Supported profile kinds are
Kemper Player, Kemper PROFILER (Head/Rack/Stage), and Generic MIDI. Kemper
PROFILER support is experimental.

The shared [experimental Kemper PROFILER plugin](../docs/kemper-head.md)
for Head, Rack and Stage shares the Player engine and schemas. It adds MK1/MK2
capabilities, Performance/Browse selection and banks 1–125. Hardware validation remains pending.

## Install or update

A Captain still running the factory firmware is installed from the Bosun Desktop
setup guide. To update an installed Captain, connect it directly by USB to Bosun
Desktop, save pending changes and select **Update firmware (USB)**. For a Captain
hosted by a Raspberry Pi, connect Bosun Desktop to the Pi and select
**Update Bosun**. Follow the [firmware update instructions](../docs/firmware-updates.md)
for USB driver requirements, Pi prerequisites and backup/recovery. The update package supports the
RP2040 MIDI Captain with **8 MiB flash**.

A raw UF2 copied to RPI-RP2 replaces the firmware without a backup or any
verification. Use the complete update procedure when replacing an existing
installation.

Enter **RPI-RP2** by holding
**switch 1 (top-left)** alone while powering on and keeping it held for three
seconds. Bosun Desktop also offers **Maintenance → Enter bootloader** over a
direct USB connection. See [bootloader entry](../docs/firmware-updates.md#enter-the-bootloader-from-bosun)
for the full procedure and stock firmware recovery.

## Captain models

One firmware image serves the 10-switch MIDI Captain and the experimental
[MIDI Captain Mini 6](../docs/mini6.md). The Mini 6 wires a subset of the same
switch pins, so `bosun_board_switches()` keeps the 10-switch bit order and
`include/bosun/hardware.h` maps each model's logical switches (NeoPixel chain
order) onto it. The model is read at boot from `/config/hardware.json`
(`{"version":1,"model":"mini6"}`); a missing or invalid record selects the
10-switch model. `SET_HARDWARE` stores a model and restarts when it differs,
and `DEVICE_INFO.hardware` reports the active layout. Models without expression
jacks never sample or drive GP27/GP28. The boot recovery chord only scans the
switches every model wires. To run the emulator as a Mini 6, write that record
under its `--root` directory before starting it.

## Startup logo

At power-on the TFT shows the startup logo for `tft.boot_logo_seconds`
(device.json, 0..60, default 2; 0 disables it); any switch press skips it, MIDI
traffic does not. The logo fills rows 0..199 and the firmware version is drawn
below it. A custom logo is stored once for all profiles at
`/config/boot_logo/tft.rgb565`: 240x200 RGB565 little-endian words, row-major,
exactly 96,000 bytes. Without it (or when it cannot be read) the built-in Bosun
logo in `src/boot_logo.c` is used; regenerate that file with
`python firmware/tools/boot_logo.py` (Pillow) if the icon changes.

- `PUT_LOGO {offset, data}`: `data` is base64 of 1..12,288 bytes. Offset 0
  starts a new upload; otherwise it must equal the bytes received so far, and
  an identical retry of the last chunk is accepted. `ACK` carries `received`;
  `ERROR offset_mismatch` carries it too.
- `COMMIT_LOGO {size: 96000, fnv1a}`: checks the staged size
  (`incomplete_upload`) and its FNV-1a 32 (`checksum_mismatch`), then
  atomically replaces the custom logo.
- `DELETE_LOGO`: removes the custom logo and any partial upload.
- After a successful commit or delete the TFT previews the result for 3 s.
  `DEVICE_INFO.boot_logo` is `"custom"` or `"default"`.

## Update lock

`UPDATE_LOCK {seconds}` (an integer 0..120) protects a firmware update from the
footswitches. A nonzero value locks, or renews the lock, until now + seconds;
0 releases it. Anything else is `invalid_request`; the lock lives in RAM only,
so a reboot clears it. While locked the physical switches are ignored (a switch
held when the lock starts is released, so a momentary binding sends its release),
`ACTIVATE_SWITCH` answers `ERROR update_lock` and every other command works.
A switch still held when the lock ends stays ignored until it is let go, so
unlocking never produces a press. Expression pedals are not affected. The TFT
shows the startup logo frame with "Updating" in place of the version until the
lock ends (switch presses do not dismiss it), then redraws the layout.

## Screensaver

After `tft.screensaver_minutes` (device.json, 0..120, default 0 = off,
clamped) without activity the TFT shows the startup logo (custom or built-in,
no version) on black with `tft.screensaver_effect`: `bounce` (half-size logo
moving and bouncing off the edges), `breathe` (full logo, backlight fading
between an eighth and all of `tft.brightness`, never off), `drift` (half-size
logo fading in at a random place, holding, fading out and moving) or `mix`
(the default, and what any other value reads as: the three in turn, one minute
each). Both settings apply as soon as they are saved. The LEDs are unchanged.
Drawing is incremental: at most eight rows, one storage read each, per loop.

Activity restarts the idle time and wakes it, with a full redraw and the
configured backlight:

- a physical switch press, which only wakes it: no action, no MIDI, no long
  press or hold, and the switch stays ignored until it is let go (as with the
  update lock); a switch already held when it engaged keeps working;
- what the runtime presents changing value, from any source: the current
  selection (bank, slot, or the rig a Kemper follows) or the Kemper tuner
  turning on (or off; a tuner left on is no activity, so the screensaver can
  still engage over it); repeated identical state does not count;
- a state-changing request: `PUT_GLOBAL`, `PUT_PATCH`, `PUT_BINDING`,
  `SWITCH_PATCH`, `DELETE_PATCH`, `SAVE_NOW`, `DISCARD`, `CREATE_PROFILE`,
  `RENAME_PROFILE`, `SWITCH_PROFILE`, `DELETE_PROFILE`, `ACTIVATE_SWITCH` (which
  also acts), `MORPH_CONTROL`, `SET_MIDI_MONITOR`, `SET_HARDWARE`, `PUT_LOGO`,
  `COMMIT_LOGO` and `DELETE_LOGO`, accepted or refused;
- `SCREENSAVER_STOP` (no fields), which the Stage sends on a tap: always `ACK`,
  also when the screensaver is off, and nothing else.

Reads and polling (`GET_*`, `LIST_*`, `STATS`, `LED_DUMP`, `PING`),
`UPDATE_LOCK`, `SCREENSAVER_START`, unknown types and expression pedals never
count. It never engages during the update lock or a startup logo or
preview, and the idle time restarts when they end; the update lock takes over
an active screensaver and leaves it off.

- `DEVICE_INFO.screensaver` is `true`.
- `{"type":"SCREENSAVER","id":null,"active":true,"effect":"bounce"}` (or
  `breathe`, `drift`; `effect` is always the one on screen) and
  `{"type":"SCREENSAVER","id":null,"active":false}` are sent unsolicited when
  the state or the effect changes, Mix rotations included, and once to a new
  session while it is active.
- `GET_SCREENSAVER` replies the same frame with the request `id`.
- `SCREENSAVER_START {effect?}` (`bounce`, `breathe`, `drift` or `mix`;
  default the configured one) shows it now, even with minutes 0, replacing a
  startup logo; `invalid_request` for another effect, `update_lock` while
  locked.

## Build requirements

Build on Linux, or on Windows through WSL with an Ubuntu distribution. On
Debian/Ubuntu, install:

```bash
sudo apt-get update
sudo apt-get install -y git cmake ninja-build gcc g++ python3 \
  gcc-arm-none-eabi libnewlib-arm-none-eabi libstdc++-arm-none-eabi-newlib
```

Host tests also require Node.js 22 in the same Linux/WSL environment. It runs
the pinned [PySwitch virtual Kemper integration tests](../docs/kemper-emulator-tests.md);
no npm install is needed for those tests. A `node` on PATH is used first;
otherwise CMake uses a Linux build unpacked under `firmware/.deps`
(for example `firmware/.deps/node-v22.23.2-linux-x64`).

The build helper fetches and verifies the pinned Pico SDK 2.3.0 and TinyUSB when
`--fetch-sdk` is supplied. The message schemas the editor's forms are built from
live in `firmware/schemas/`; `plugins/kemper.json` holds the differences
between the Kemper models. `cmake/build_manifest.py` turns both into the
manifest the firmware serves and checks them against the runtime's supported
messages.

## Build the update package

Run from the repository root on Linux/WSL:

```bash
python3 tools/verify-release-version.py
bash tools/native-build.sh all --fetch-sdk
release=$(python3 -c 'import json; print(json.load(open("editor/package.json"))["version"])')
python3 tools/package-native-update.py \
  --uf2 firmware/build-rp2040/bosun_native.uf2 \
  --output editor/src-tauri/resources/update/bosun-update.zip \
  --release "$release" --firmware-version "$release"
python3 tools/verify-release-version.py \
  --package editor/src-tauri/resources/update/bosun-update.zip
```

`all` builds the host tools, runs their tests and compiles the RP2040 firmware.
The UF2 is written to `firmware/build-rp2040/bosun_native.uf2`; the verified
update archive is ready for the desktop/Android packaging scripts. None of these
commands opens or flashes a device.

To invoke the compilation step from Windows instead:

```powershell
powershell -ExecutionPolicy Bypass -File tools/native-build.ps1 -Platform all -FetchSdk
```

Use `-Distribution <name>` if the WSL distribution is not named `Ubuntu`.
`PICO_SDK_PATH` selects an existing pinned SDK; `BOSUN_BUILD_ROOT` changes build
output directories. The default build reserves the final 4 MiB of the 8 MiB
flash for configuration. The firmware build's `BOSUN_FLASH_BYTES` setting can
change a verified board geometry, but the release updater currently accepts
only the supported 8 MiB target.
