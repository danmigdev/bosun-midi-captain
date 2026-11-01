/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef BOSUN_DISPLAY_H
#define BOSUN_DISPLAY_H
#include "bosun/board.h"
#include "bosun/config.h"
#include "bosun/kemper.h"
#include "bosun/screensaver.h"

#define BOSUN_DISPLAY_LABELS 16u
#define BOSUN_DISPLAY_GLYPHS 128u
/* Global custom startup logo: rows 0..199 of the TFT, row-major RGB565
 * little-endian words. Absent or unreadable selects the built-in logo. */
#define BOSUN_BOOT_LOGO_DIR "/config/boot_logo"
#define BOSUN_BOOT_LOGO_PATH BOSUN_BOOT_LOGO_DIR "/tft.rgb565"
#define BOSUN_BOOT_LOGO_HEIGHT 200u
#define BOSUN_BOOT_LOGO_BYTES (BOSUN_DISPLAY_WIDTH * BOSUN_BOOT_LOGO_HEIGHT * 2u)
typedef enum {
    BOSUN_DISPLAY_OK = 0,
    BOSUN_DISPLAY_UNKNOWN_FIELD = 2, BOSUN_DISPLAY_LIMIT = 4,
    BOSUN_DISPLAY_INVALID_LAYOUT = 8, BOSUN_DISPLAY_IO = 16,
    BOSUN_DISPLAY_MISSING_GLYPH = 32
} bosun_display_status_t;
typedef struct {
    uint8_t glyphs[BOSUN_DISPLAY_GLYPHS];
    uint16_t length, width, height, color, scroll_span, scroll_speed;
    int16_t x, y;
    uint8_t scale;
} bosun_display_label_t;
typedef struct {
    bosun_display_label_t labels[BOSUN_DISPLAY_LABELS];
    uint16_t line[BOSUN_DISPLAY_WIDTH];
    uint32_t config_revision, patch_revision, kemper_revision;
    uint32_t frame_ms, animation_started_ms, logo_ms, saver_ms, saver_seed;
    char hold_effect[129];
    uint16_t row, rotation, tuner_x, tuner_color, logo_duration, logo_run;
    int16_t saver_x, saver_y; /* Logo box origin while the screensaver shows. */
    uint8_t count, brightness, logo, saver, saver_phase, saver_level;
    bool updating;
    unsigned status;
    bool has_frame, scrolling, tuner, logo_custom;
    int8_t saver_dx, saver_dy;
} bosun_display_t;

/* Retains one bounded label snapshot and one 480-byte RGB565 stripe. A call
 * transfers at most eight rows; MIDI processing stays in the outer loop.
 * Every label uses the ROM font. Unknown context fields stay empty instead of
 * displaying orphan prefixes. The first frame after init is the startup logo
 * when device.json tft.boot_logo_seconds (0..60, default 2) is nonzero. */
void bosun_display_init(bosun_display_t *display);
unsigned bosun_display_render(bosun_display_t *display, const bosun_config_t *config,
                              const bosun_kemper_state *kemper, const char *hold_effect,
                              uint32_t now_ms);
/* Draws the startup logo (custom file or built-in, firmware version below it)
 * from the next render, restarting one in progress, and holds it duration_ms
 * after the frame completes. Context changes never end it; skip (a switch
 * press) does. Either way the layout returns with a full redraw. */
void bosun_display_logo(bosun_display_t *display, uint16_t duration_ms);
void bosun_display_skip_logo(bosun_display_t *display);
/* While active the logo frame shows "Updating" instead of the version, from
 * the next render, with no timeout; skip does not end it. Turning it off
 * ends any logo, including a preview requested meanwhile, with a full redraw. */
void bosun_display_updating(bosun_display_t *display, bool active);
/* True while a logo or the Updating frame owns the panel, and from init until
 * the first render has shown or skipped the startup logo. */
bool bosun_display_logo_active(const bosun_display_t *display);
/* Shows BOUNCE, BREATHE or DRIFT from the next render on a black panel with
 * the startup logo (custom or built-in, no version); LEDs are not touched.
 * OFF ends it: the configured backlight and a full layout redraw return. A
 * different effect restarts from a cleared panel. A logo or the Updating frame
 * ends it. A render draws at most eight rows with one storage read each.
 * Bounce: the half-size logo (2x2 average) moves and bounces off the edges.
 * Breathe: the full logo box, centered, with the backlight fading between
 * an eighth and all of the configured level. Drift: the half-size logo fades
 * in at a pseudo-random place, holds, fades out and moves. */
void bosun_display_screensaver(bosun_display_t *display, unsigned effect);
#endif
