# Windows stock-to-native installation test

Tested on 19 September 2026 with a **10-switch RP2040 MIDI Captain, 8 MiB flash**,
Windows Desktop, and the official **PaintAudio 5.15** firmware from
[PaintAudio Downloads](https://paintaudio.com/pages/surports).

## Result and release status

The published **Bosun Desktop 0.6.7 does not discover PaintAudio 5.15**. Its USB
discovery only recognizes the earlier `239a:80f4` identity; the official 5.15
firmware uses `239a:cafe` and presents a standard USB serial port.

The corrected Desktop source and local Windows test build successfully installed
**0.6.7-native directly from PaintAudio 5.15** through the actual setup wizard.
The correction ships in **Desktop 0.6.8**, bundled with **0.6.8-native**.
The hardware trial used the corrected local 0.6.7 test build and 0.6.7-native;
it was not repeated with the final 0.6.8 release binaries. The published 0.6.7
binaries do not include the fix and must not be described as hardware-validated.

The correction allows the stock identity only in the first-installation path.
Existing native updates retain their stricter identity check. Device/model
confirmation, physical USB path, flash UID, 8 MiB capacity, verified backup and
full flash readback checks remain in place.

## Hardware procedure and evidence

1. Exported the existing profile, all six patches, global settings and MIDI-learn
   data before changing firmware. Read the full original 8 MiB flash twice,
   compared SHA-256 hashes, and retained verified copies on both the Pi and PC.
   Also generated a full recovery UF2 and checked that its payload reproduces the
   exact backup. Restarted and compared the original configuration again.
2. Loaded the official 10-switch PaintAudio 5.15 UF2. Used its documented USB
   Setup mode to prepare the Captain's settings drive, copied all 75 official
   files and checked each file's hash. Restarted normally into stock firmware.
3. Confirmed the published Desktop 0.6.7 returned no installer candidates while
   Windows enumerated the stock Captain's USB serial device.
4. Opened the corrected Desktop setup guide, selected the Captain, confirmed the
   model and pressed **Install**. The app requested BOOTSEL automatically using
   the stock firmware's 1200-baud procedure and loaded its RAM-only helper.
5. The installer saved and verified its own complete stock backup before writing,
   installed the bundled native firmware, verified full flash readback, restarted
   and confirmed the native version, healthy storage and an empty profile list.
   No CircuitPython bootstrap or Pi was involved in this installation.
6. Compared the installer's stock backup against every UF2 payload block in the
   official PaintAudio package: all programmed bytes matched.
7. Restored the original profile metadata, global settings, six patches and
   MIDI-learn table. Compared every field with the original export, restarted
   the Captain, and verified those settings and storage readiness again.
8. Reconnected the Captain to its original Pi/Kemper/Stage setup. The user
   confirmed that rig switching and Stage worked again; the hub readback also
   confirmed the restored configuration and native firmware version.

The official ZIP used was `MIDICAPTAIN_10s_FW5.15.zip`, SHA-256
`ee500c5a3d5146a0fd29d964ca44762c4df2e5d5e31ab7cc794411405b5d57ae`.
Private full-flash backups, journals and screenshots are retained locally and
are not published with the source.

## Scope

This verifies one complete Windows installation from PaintAudio 5.15 on the
supported 10-switch hardware. It does not establish compatibility with every
older stock firmware or other Captain models. macOS/Linux runtime operation,
fresh Pi-card installation and interrupted-write recovery remain unverified
by this hardware test.

Holding footswitch **1** while powering on opens PaintAudio **USB Setup**. It is
not the same as entering the `RPI-RP2` bootloader. PaintAudio 5 also supplies
`MIDICAPTAINBOOT.HTML` for its documented bootloader procedure.
