/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef BOSUN_DISPLAY_H
#define BOSUN_DISPLAY_H
#include "bosun/board.h"
#include "bosun/config.h"
#include "bosun/kemper.h"

#define BOSUN_DISPLAY_LABELS 16u
#define BOSUN_DISPLAY_GLYPHS 128u
typedef enum {
    BOSUN_DISPLAY_OK = 0, BOSUN_DISPLAY_UNSUPPORTED_FONT = 1,
    BOSUN_DISPLAY_UNKNOWN_FIELD = 2, BOSUN_DISPLAY_LIMIT = 4,
    BOSUN_DISPLAY_INVALID_LAYOUT = 8, BOSUN_DISPLAY_IO = 16,
    BOSUN_DISPLAY_MISSING_GLYPH = 32
} bosun_display_status_t;
typedef struct {
    uint8_t glyphs[BOSUN_DISPLAY_GLYPHS];
    uint16_t length, width, height, color, scroll_span, scroll_speed;
    int16_t x, y;
    uint8_t scale, icon;
    bool badge;
} bosun_display_label_t;
typedef struct {
    bosun_display_label_t labels[BOSUN_DISPLAY_LABELS];
    uint16_t line[BOSUN_DISPLAY_WIDTH];
    uint32_t config_revision, patch_revision, kemper_revision;
    uint32_t frame_ms, animation_started_ms;
    uint16_t row, rotation, tuner_x, tuner_color;
    uint8_t count, brightness;
    unsigned status;
    bool has_frame, scrolling, tuner;
} bosun_display_t;

/* Retains one bounded label snapshot and one 480-byte RGB565 stripe. A call
 * transfers at most eight rows; MIDI processing stays in the outer loop.
 * Unsupported BDF fonts explicitly set UNSUPPORTED_FONT and use the ROM font.
 * Unknown context fields stay empty instead of displaying orphan prefixes. */
void bosun_display_init(bosun_display_t *display);
unsigned bosun_display_render(bosun_display_t *display, const bosun_config_t *config,
                              const bosun_kemper_state *kemper, uint32_t now_ms);
#endif
