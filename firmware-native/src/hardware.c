/* SPDX-License-Identifier: GPL-3.0-or-later */
#include "bosun/hardware.h"
#include "bosun/board.h"
#include "bosun/json.h"
#include <stdio.h>
#include <string.h>

static const char *const captain10_names[] = {"1", "2", "3", "4", "up", "A", "B", "C", "D", "down"};
static const uint8_t captain10_bits[] = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9};
/* PySwitch pa_midicaptain_mini_6.py: switches 1, 2, 3, A, B and C use GP1,
 * GP25, GP24, GP9, GP10 and GP11, the 10-switch Captain's pins for the same
 * names, and their 18 pixels follow this order. Not verified on hardware. */
static const char *const mini6_names[] = {"1", "2", "3", "A", "B", "C"};
static const uint8_t mini6_bits[] = {0, 1, 2, 5, 6, 7};

const bosun_hardware_t bosun_hardware_captain10 = {
    "captain10", "MIDI Captain", captain10_names, captain10_bits, 10, 5, 30, 2
};
const bosun_hardware_t bosun_hardware_mini6 = {
    "mini6", "MIDI Captain Mini 6", mini6_names, mini6_bits, 6, 3, 18, 0
};
static const bosun_hardware_t *const models[] = {&bosun_hardware_captain10, &bosun_hardware_mini6};

_Static_assert(sizeof captain10_names / sizeof *captain10_names == BOSUN_SWITCH_COUNT &&
               sizeof captain10_bits == BOSUN_SWITCH_COUNT, "10-switch model covers every board bit");
_Static_assert(sizeof mini6_names / sizeof *mini6_names == sizeof mini6_bits, "Mini 6 table sizes");
_Static_assert(BOSUN_HARDWARE_SWITCHES_MAX == BOSUN_SWITCH_COUNT, "board mask bounds every model");
_Static_assert(BOSUN_LED_COUNT == 30, "board clocks the longest supported chain");

const bosun_hardware_t *bosun_hardware_or_default(const bosun_hardware_t *hardware) {
    return hardware ? hardware : &bosun_hardware_captain10;
}
unsigned bosun_hardware_count(void) { return sizeof models / sizeof *models; }
const bosun_hardware_t *bosun_hardware_at(unsigned index) {
    return index < bosun_hardware_count() ? models[index] : NULL;
}
const bosun_hardware_t *bosun_hardware_find(const char *model) {
    if (!model) return NULL;
    for (unsigned i = 0; i < bosun_hardware_count(); ++i)
        if (!strcmp(model, models[i]->model)) return models[i];
    return NULL;
}
int bosun_hardware_switch_index(const bosun_hardware_t *hardware, const char *name) {
    hardware = bosun_hardware_or_default(hardware);
    if (!name) return -1;
    for (unsigned i = 0; i < hardware->switch_count; ++i)
        if (!strcmp(name, hardware->switch_names[i])) return (int)i;
    return -1;
}
uint16_t bosun_hardware_board_mask(const bosun_hardware_t *hardware) {
    hardware = bosun_hardware_or_default(hardware);
    uint16_t mask = 0;
    for (unsigned i = 0; i < hardware->switch_count; ++i) mask |= (uint16_t)(1u << hardware->board_bits[i]);
    return mask;
}
uint16_t bosun_hardware_logical_mask(const bosun_hardware_t *hardware, uint16_t board_mask) {
    hardware = bosun_hardware_or_default(hardware);
    uint16_t mask = 0;
    for (unsigned i = 0; i < hardware->switch_count; ++i)
        if (board_mask & (1u << hardware->board_bits[i])) mask |= (uint16_t)(1u << i);
    return mask;
}
uint16_t bosun_hardware_common_board_mask(void) {
    uint16_t mask = UINT16_MAX;
    for (unsigned i = 0; i < bosun_hardware_count(); ++i) mask &= bosun_hardware_board_mask(models[i]);
    return mask;
}
const bosun_hardware_t *bosun_hardware_load(bool *configured) {
    char text[BOSUN_HARDWARE_RECORD_BYTES + 1], model[32];
    bosun_json_token_t tokens[32];
    bosun_json_doc_t doc;
    size_t length = 0;
    const bosun_hardware_t *found = NULL;
    if (bosun_store_read(BOSUN_HARDWARE_RECORD, text, sizeof text - 1, &length) == BOSUN_STORE_OK &&
        bosun_json_parse(&doc, text, length, tokens, sizeof tokens / sizeof *tokens) == BOSUN_JSON_OK &&
        doc.tokens[0].type == BOSUN_JSON_OBJECT &&
        bosun_json_string(&doc, bosun_json_get(&doc, 0, "model"), model, sizeof model))
        found = bosun_hardware_find(model);
    if (configured) *configured = found != NULL;
    return bosun_hardware_or_default(found);
}
bosun_store_result_t bosun_hardware_save(const bosun_hardware_t *hardware) {
    char text[96];
    if (!hardware || bosun_hardware_find(hardware->model) != hardware) return BOSUN_STORE_INVALID;
    int length = snprintf(text, sizeof text, "{\"version\":1,\"model\":\"%s\"}", hardware->model);
    if (length <= 0 || (size_t)length >= sizeof text) return BOSUN_STORE_LIMIT;
    bosun_store_result_t result = bosun_store_mkdir("/config");
    return result == BOSUN_STORE_OK ? bosun_store_write_atomic(BOSUN_HARDWARE_RECORD, text, (size_t)length) : result;
}
