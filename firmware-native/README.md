# Native firmware: build and installation

Bosun's maintained MIDI Captain firmware runs in C. Supported native profile
kinds are Kemper Player, Kemper PROFILER (Head/Rack/Stage), and Generic MIDI.
Kemper family support is experimental. Existing CircuitPython installations
can migrate through **Update Bosun**; CircuitPython is no longer maintained as a
parallel firmware target.

The shared [experimental Kemper PROFILER plugin](../docs/kemper-head.md)
for Head, Rack and Stage shares the Player engine and schemas. It adds MK1/MK2
capabilities, Performance/Browse selection and banks 1–125. Hardware validation remains pending.

## Install or update

For existing native firmware, connect the Captain directly by USB to Bosun Desktop,
save pending changes and select **Update firmware (USB)**. For CircuitPython
migration or a Captain hosted by a Pi, connect Bosun Desktop to the Pi and select
**Update Bosun**. Follow the [firmware update instructions](../docs/firmware-updates.md)
for USB driver requirements, Pi prerequisites and backup/recovery. The update package supports the
RP2040 MIDI Captain with **8 MiB flash**.

A raw UF2 does not migrate configuration or create a recovery backup. Use the
complete update procedure when replacing an existing installation.

From Bosun 0.7.1, enter **RPI-RP2** by holding
**switch 1 (top-left)** alone while powering on and keeping it held for three
seconds. Bosun Desktop also offers **Maintenance → Enter bootloader** over a
direct USB connection. See [bootloader entry](../docs/firmware-updates.md#enter-the-bootloader-from-bosun)
for the full procedure and stock firmware recovery.

## Build requirements

Build on Linux, or on Windows through WSL with an Ubuntu distribution. On
Debian/Ubuntu, install:

```bash
sudo apt-get update
sudo apt-get install -y git cmake ninja-build gcc g++ python3 \
  gcc-arm-none-eabi libnewlib-arm-none-eabi libstdc++-arm-none-eabi-newlib
```

The build helper fetches and verifies the pinned Pico SDK 2.3.0 and TinyUSB when
`--fetch-sdk` is supplied. Keep the full repository checkout: the native build
still consumes shared schemas retained under `firmware/`.

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
