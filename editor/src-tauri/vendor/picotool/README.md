# RP2040 flash identity helper

`flash_id.bin` and its corresponding `flash_id.c` are from Raspberry Pi
picotool commit `2041936441b48a3cc53ae3da9e805229fe8f4e18`, directory
[`picoboot_flash_id`](https://github.com/raspberrypi/picotool/tree/2041936441b48a3cc53ae3da9e805229fe8f4e18/picoboot_flash_id).
The upstream BSD 3-Clause license is included in `LICENSE.TXT`.

The desktop updater executes this 152-byte helper in RP2040 XIP SRAM to read
the flash unique ID and JEDEC capacity before acquiring a recovery image or
writing flash. The runtime USB serial number is that same flash unique ID.
The helper sends read-only SPI opcodes 0x4b and 0x9f. It is compiled into Bosun;
no downloaded executable is accepted from an update package.

SHA-256: `0c598d8a4dc02ede332a65f96aff27a410fd65d8aaa9fa6dc971539c720725b8`.
