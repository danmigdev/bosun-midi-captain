/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef BOSUN_HARDWARE_H
#define BOSUN_HARDWARE_H
#include "bosun/storage.h"

/* Supported MIDI Captain models. Each model wires a subset of the same switch
 * GPIOs, LED pin, TFT and MIDI pins, so bosun_board_switches() keeps one bit
 * order (1,2,3,4,up,A,B,C,D,down) for every model and this descriptor maps a
 * model's logical switches onto those bits. Logical order is the NeoPixel
 * chain order: switch i owns pixels 3i..3i+2. Rows are row_length switches,
 * top row first. The record lives outside every profile, so configuration
 * backups never carry a hardware identity to another pedal. */
#define BOSUN_HARDWARE_SWITCHES_MAX 10u
#define BOSUN_HARDWARE_RECORD "/config/hardware.json"
#define BOSUN_HARDWARE_RECORD_BYTES 512u

typedef struct {
    const char *model, *name;
    const char *const *switch_names;
    const uint8_t *board_bits;
    uint8_t switch_count, row_length, led_count, expression_jacks;
} bosun_hardware_t;

extern const bosun_hardware_t bosun_hardware_captain10, bosun_hardware_mini6;

/* NULL selects the 10-switch model, the identity of every pre-0.8 install. */
const bosun_hardware_t *bosun_hardware_or_default(const bosun_hardware_t *hardware);
const bosun_hardware_t *bosun_hardware_find(const char *model);
unsigned bosun_hardware_count(void);
const bosun_hardware_t *bosun_hardware_at(unsigned index);
int bosun_hardware_switch_index(const bosun_hardware_t *hardware, const char *name);
/* Board bits wired on this model, and a board mask in logical switch order. */
uint16_t bosun_hardware_board_mask(const bosun_hardware_t *hardware);
uint16_t bosun_hardware_logical_mask(const bosun_hardware_t *hardware, uint16_t board_mask);
/* Board bits wired on every supported model. */
uint16_t bosun_hardware_common_board_mask(void);
/* A missing, unreadable or unknown record selects the 10-switch model;
 * configured reports whether a valid record was found. */
const bosun_hardware_t *bosun_hardware_load(bool *configured);
bosun_store_result_t bosun_hardware_save(const bosun_hardware_t *hardware);
#endif
