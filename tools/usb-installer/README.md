# RAM-only first-install helper

This helper runs entirely in RP2040 SRAM. Its UF2 is copied to the single, selected
BOOTSEL device using the OS mass-storage driver; the helper then exposes standard
USB CDC. No Zadig/WinUSB installation is needed for this path.

Build and audit, without accessing hardware:

```sh
bash tools/native-build.sh all --fetch-sdk
bash tools/build-factory-installer.sh
python tools/test_factory_installer.py
```

The SDK and TinyUSB revisions are pinned by `firmware-native/cmake/pico_sdk.cmake`.
Both the Python packager and Rust backend check that every UF2 block targets only
contiguous SRAM, with RP2040 family ID, bounded size, block order and vectors.
SDK `no_flash` entry code occupies SRAM + 0; its vector table starts at +0x100.
The bundled manifest binds loader and empty littlefs hashes to the exact native
release's firmware hash. The host storage-image builder creates and remounts an
empty volume using the same littlefs implementation as the firmware.

Frames are little-endian: magic `BSU1`, sequence, command, offset (reply: status),
length, payload CRC32, header CRC32 (first 24 bytes), then payload. INFO reports
the physical flash UID, capacity and protocol version. READ is bounded to 4 KiB.
ARM must match the flash UID before WRITE can erase/program an aligned sector.
The helper checks each written sector; the host verifies the full 8 MiB image.
Incomplete/invalid commands and a disconnected CDC session disarm the helper.

The host pins USB topology and flash UID. It durably saves and verifies the full
original flash before recording write intent and writing firmware plus initial
storage. It retains the RAM helper alongside the backup so recovery does not
depend on a subsequently installed Desktop release. Original factory settings
are not migrated. An existing native image is rejected by this first-install
path to protect its profiles; use native update instead.

The helper deliberately supplies its own CDC descriptors: the SDK's default
`pico_unique_id` returns an EE placeholder in a `NO_FLASH` build. After calling
`flash_start_xip()`, the helper reads command 0x4b from the actual flash and uses
that UID in both USB enumeration and INFO/ARM. USB starts only afterwards.

Software checks are not a substitute for the pending Windows hardware trial:
start from a factory Captain, verify automatic/manual BOOTSEL entry and CDC
enumeration, install, reconnect, create a profile and verify MIDI. Also exercise
interrupted-write recovery on a test device with a separately retained known-good
backup. Do not label this path hardware-verified until that trial is complete.
