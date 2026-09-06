# Experimental native firmware maintenance

Measured results, backup details and remaining hardware checks are recorded in
[the hardware validation report](../docs/native-firmware-hardware-validation.md).

The native firmware exposes its supported `reboot_modes` in `GET_DEVICE_INFO`.
On the data CDC interface, send one JSON object followed by a newline:

```json
{"type":"REBOOT","id":"maintenance","mode":"bootloader"}
```

The firmware replies with `{"type":"ACK","id":"maintenance"}` and enters
the RP2040 ROM USB bootloader. It waits at least 100 ms after accepting the
request for the reply to drain, with a 1000 ms limit if the host stops reading.
An accepted request survives a CDC disconnect. The bootloader entry itself
does not format or write flash; loading a firmware or storage image is a separate
maintenance operation. Back up the working firmware and configuration before
replacing either image.

Omitting `mode`, or using `"mode":"normal"`, keeps the ordinary restart
behavior. Other values, non-string values, and duplicate `mode` fields return
an error without scheduling a reset. The bootloader command remains available
when the configuration filesystem cannot mount; omit `profile` for this
device-wide operation. It requires a running firmware and a working data CDC
connection, so it is not a recovery mechanism for board initialization failure
or a firmware that cannot boot.

This is a native-firmware maintenance command, not CircuitPython file OTA.
`firmware_ota` remains `false`; the existing `PUT_FILE_*` upload commands remain
unsupported. The host emulator acknowledges the command and exits; it does not
open hardware or emulate ROM USB enumeration.

Data CDC sessions follow DTR transitions. Each transition advances a generation
counter and clears the TinyUSB RX/TX FIFOs; the application also discards old
partial requests and pending replies if a close/reopen occurs within one USB
task. An already accepted reboot remains scheduled. One newline precedes the
new session's JSON, separating any old packet already submitted to the USB IN
endpoint (up to 64 bytes) from the new frames. This does not prevent the host's
serial-open input flush from discarding bytes: clients should align to a complete
line during bounded startup, then enforce strict JSON framing for the session.

Periodic console diagnostics include the data-session generation, cumulative
protocol request count, and CDC RX/TX byte counts with FNV-1a32 fingerprints.
RX counts bytes returned to the application; TX counts bytes accepted by
TinyUSB, including the initial newline. These counters do not establish physical
USB delivery. Byte counts and fingerprints reset at a new data session and
survive suspend; the protocol request count remains cumulative until reboot.
Comparing them with a USB capture requires the capture to include the session's
DTR opening. The fingerprints are non-cryptographic and retain no message text.

USB suspend pauses bus activity without ending the data CDC session. Queued
RX/TX data and the session generation are preserved across suspend/resume;
actual DTR transitions or USB unmount still start a new session and discard
retired traffic. The console's `usb_session` counter makes those boundaries
observable. This behavior is covered by offline suspend/resume tests; it does
not establish the cause of an earlier malformed hardware response.

`LED_DUMP` returns the last submitted LED DMA frame as 30 RGB triplets, the
physical switch-to-pixel mapping and the current bank/slot. It supports the same
Stage feedback checks as CircuitPython. This diagnostic reports the commanded
frame; checking the emitted light and physical footswitches still needs direct
observation of the pedal.

## Flash geometry and provisioning

The default build targets the **8 MiB flash measured on the tested MIDI Captain**
on 2026-09-06. This measurement does not establish the capacity of every Captain
hardware revision. Verify the actual unit before installing. For another verified
capacity, use `BOSUN_FLASH_BYTES=<bytes> bash tools/native-build.sh rp2040`,
PowerShell `tools/native-build.ps1 -Platform rp2040 -FlashBytes <bytes>`, or CMake
`-DPICO_FLASH_SIZE_BYTES=<bytes>`. Values must be sector aligned; existing CMake
caches retain their previous explicit value, so reconfigure them deliberately.

The native filesystem always occupies the final 512 KiB: with the default 8 MiB
geometry its flash offset is `0x780000` (XIP address `0x10780000`). The image audit
checks that firmware UF2 pages stop before this region. It does not prove that
the region is unused by the previous firmware. On the tested unit, the actual
CircuitPython FAT volume extends from `0x100000` through `0x800000`, including the
native filesystem region. Preserve and verify the **complete 8 MiB flash backup**
before provisioning; restoration of that full image restores both the original
firmware and its filesystem. Embedded drive metadata alone did not describe the
formatted FAT volume correctly.

The host-only `bosun_storage_image --config-root EXISTING_CONFIG_DIRECTORY
--output NEW_IMAGE.bin` builds a 512 KiB littlefs image using the production
storage backend. The input must contain `active_profile.json` and supported
`profiles/...` configuration files. The builder checks native JSON limits,
activates every profile, remounts, and compares every file byte for byte before
creating the output. It rejects symlinks, unsupported paths/plugins, and existing
output files. The image contains relative filesystem blocks, so its host fake
flash base does not select a device address. Installing it is a separate operation
that must use the verified unit's storage offset; the builder never opens hardware.
