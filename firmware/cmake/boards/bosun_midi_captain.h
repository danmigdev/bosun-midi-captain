/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef BOSUN_MIDI_CAPTAIN_PICO_BOARD_H
#define BOSUN_MIDI_CAPTAIN_PICO_BOARD_H

/* Also parsed by Pico's CMake: preprocessor definitions only. Pin inventory
 * and 8 MiB flash were verified on the tested MIDI Captain (2026-09-06).
 * This is not a capacity guarantee for other hardware revisions: check each
 * unit, override PICO_FLASH_SIZE_BYTES when needed, and back up its full flash. */
pico_board_cmake_set(PICO_PLATFORM, rp2040)
pico_board_cmake_set_default(PICO_FLASH_SIZE_BYTES, (8 * 1024 * 1024))
#ifndef PICO_FLASH_SIZE_BYTES
#define PICO_FLASH_SIZE_BYTES (8 * 1024 * 1024)
#endif
#define PICO_BOOT_STAGE2_CHOOSE_GENERIC_03H 1
#define PICO_FLASH_SPI_CLKDIV 4
#define PICO_RP2040_B0_SUPPORTED 1

#endif
