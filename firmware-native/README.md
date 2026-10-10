# Native firmware: build and installation

Bosun's MIDI Captain firmware is written in C. Supported profile kinds are
Kemper Player, Kemper PROFILER (Head/Rack/Stage), and Generic MIDI. Kemper
family support is experimental.

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

From Bosun 0.7.1, enter **RPI-RP2** by holding
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
otherwise CMake uses a Linux build unpacked under `firmware-native/.deps`
(for example `firmware-native/.deps/node-v22.23.2-linux-x64`).

The build helper fetches and verifies the pinned Pico SDK 2.3.0 and TinyUSB when
`--fetch-sdk` is supplied. The message schemas the editor's forms are built from
live in `firmware-native/schemas/`; `plugins/kemper.json` holds the differences
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
  --uf2 firmware-native/build-rp2040/bosun_native.uf2 \
  --output editor/src-tauri/resources/update/bosun-update.zip \
  --release "$release" --firmware-version "${release}-native"
python3 tools/verify-release-version.py \
  --package editor/src-tauri/resources/update/bosun-update.zip
```

`all` builds the host tools, runs their tests and compiles the RP2040 firmware.
The UF2 is written to `firmware-native/build-rp2040/bosun_native.uf2`; the verified
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
