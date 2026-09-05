/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef BOSUN_MIDI_CAPTAIN_PICO_BOARD_H
#define BOSUN_MIDI_CAPTAIN_PICO_BOARD_H

/* Also parsed by Pico's CMake: preprocessor definitions only. The board pin
 * inventory is verified; physical flash capacity is not yet independently
 * read from this Captain. 2 MiB is the conservative configurable build size.
 * Do not flash until the hardware capacity and filesystem backup are checked. */
pico_board_cmake_set(PICO_PLATFORM, rp2040)
pico_board_cmake_set_default(PICO_FLASH_SIZE_BYTES, (2 * 1024 * 1024))
#ifndef PICO_FLASH_SIZE_BYTES
#define PICO_FLASH_SIZE_BYTES (2 * 1024 * 1024)
#endif
#define PICO_BOOT_STAGE2_CHOOSE_GENERIC_03H 1
#define PICO_FLASH_SPI_CLKDIV 4
#define PICO_RP2040_B0_SUPPORTED 1

#endif
