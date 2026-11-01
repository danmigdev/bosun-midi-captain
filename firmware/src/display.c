/* SPDX-License-Identifier: GPL-3.0-or-later */
#include "bosun/display.h"
#include "bosun/protocol.h"
#include <limits.h>
#include <stdio.h>
#include <string.h>
#include "../third_party/adafruit-gfx-font/glcdfont.c"
#include "../third_party/adafruit-gfx-font/unicode_cp437.h"
#include "boot_logo.c"

enum { CELL_W = 6, CELL_H = 12,
       SCROLL_MARGIN = 6, SCROLL_PAUSE = 800, ROWS_PER_TICK = 8 };
enum { LOGO_OFF, LOGO_BOOT, LOGO_START, LOGO_DRAW, LOGO_HOLD };
enum { SAVER_START, SAVER_CLEAR, SAVER_DRAW, SAVER_SHOW };
enum { LOGO_ROW_BYTES = BOSUN_DISPLAY_WIDTH * 2, HALF_W = BOSUN_DISPLAY_WIDTH / 2,
       HALF_H = BOSUN_BOOT_LOGO_HEIGHT / 2, BOUNCE_STEP = 2, BOUNCE_MS = 40,
       BREATHE_MS = 6000, DRIFT_FADE_MS = 1200, DRIFT_HOLD_MS = 4000 };

static int clamp(int value, int minimum, int maximum) {
    return value < minimum ? minimum : value > maximum ? maximum : value;
}
static uint16_t rgb565(uint32_t color) {
    return (uint16_t)(((color >> 8) & 0xf800) | ((color >> 5) & 0x07e0) | ((color >> 3) & 0x001f));
}
static uint16_t label_color(const bosun_json_doc_t *doc, int entry) {
    int token = bosun_json_get(doc, entry, "color");
    int32_t number;
    if (bosun_json_integer(doc, token, &number) && number >= 0 && number <= 0xffffff)
        return rgb565((uint32_t)number);
    char text[8];
    if (!bosun_json_string(doc, token, text, sizeof text) || strlen(text) != 7 || text[0] != '#') return 0xffff;
    uint32_t value = 0;
    for (unsigned i = 1; i <= 6; ++i) {
        char c = text[i];
        unsigned digit;
        if (c >= '0' && c <= '9') digit = (unsigned)(c - '0');
        else if (c >= 'a' && c <= 'f') digit = (unsigned)(c - 'a' + 10);
        else if (c >= 'A' && c <= 'F') digit = (unsigned)(c - 'A' + 10);
        else return 0xffff;
        value = value * 16 + digit;
    }
    return rgb565(value);
}
static bool string_field(const bosun_json_doc_t *doc, int entry, const char *key,
                         char *out, size_t capacity, unsigned *status) {
    out[0] = 0;
    int token = bosun_json_get(doc, entry, key);
    if (token < 0 || doc->tokens[token].type == BOSUN_JSON_NULL) return false;
    if (!bosun_json_string(doc, token, out, capacity)) {
        out[0] = 0;
        *status |= BOSUN_DISPLAY_LIMIT;
        return false;
    }
    return true;
}
static unsigned alignment(const bosun_json_doc_t *doc, int entry, const char *key) {
    int token = bosun_json_get(doc, entry, key);
    if (bosun_json_equal(doc, token, "center")) return 1;
    if (bosun_json_equal(doc, token, "right") || bosun_json_equal(doc, token, "bottom")) return 2;
    return 0;
}
static void resolve(const char *field, const bosun_config_t *config,
                    const bosun_kemper_state *kemper, const char *hold_effect, char *out, size_t capacity,
                    unsigned *status) {
    out[0] = 0;
    if (!strcmp(field, "patch_name")) {
        if (kemper && kemper->rig_name_fresh && kemper->rig_name[0])
            snprintf(out, capacity, "%s", kemper->rig_name);
        else string_field(&config->patch_doc, 0, "name", out, capacity, status);
    } else if (!strcmp(field, "hold_effect")) snprintf(out, capacity, "%s", hold_effect);
    else if (!strcmp(field, "bank")) snprintf(out, capacity, "%u", config->bank);
    else if (!strcmp(field, "slot")) snprintf(out, capacity, "%u", config->slot);
    else if (!strcmp(field, "expression_mode"))
        snprintf(out, capacity, "%s", kemper ? bosun_kemper_expression_label(kemper->expression_mode) : "---");
    else if (!strcmp(field, "kemper_rig_name")) {
        if (kemper && kemper->rig_name_fresh) snprintf(out, capacity, "%s", kemper->rig_name);
    } else if (!strcmp(field, "kemper_rig")) {
        if (kemper) snprintf(out, capacity, "%u", kemper->rig);
    } else if (!strcmp(field, "kemper_bank")) {
        if (kemper) snprintf(out, capacity, "%u", kemper->bank);
    } else if (!strcmp(field, "kemper_rig_in_bank")) {
        if (kemper) snprintf(out, capacity, "%u", kemper->rig_in_bank);
    } else if (!strcmp(field, "kemper_morph")) {
        if (kemper && kemper->morph_value >= 0)
            snprintf(out, capacity, "SET %u%%", (unsigned)(kemper->morph_value * 100 + 63) / 127);
        else snprintf(out, capacity, "?");
    } else if (!strcmp(field, "kemper_bpm")) {
        if (kemper) snprintf(out, capacity, "%u", kemper->bpm);
    } else if (!strcmp(field, "kemper_connected")) {
        if (kemper) snprintf(out, capacity, "%s", kemper->connected ? "on" : "off");
    } else if (!strcmp(field, "kemper_tuner")) {
        if (kemper) snprintf(out, capacity, "%s", kemper->tuner_active ? "on" : "off");
    } else if (!strcmp(field, "kemper_tuner_note")) {
        if (kemper) snprintf(out, capacity, "%s", kemper->tuner_note);
    } else if (!strcmp(field, "kemper_tuner_deviance")) {
        if (kemper) snprintf(out, capacity, "%u", kemper->tuner_deviance);
    } else *status |= BOSUN_DISPLAY_UNKNOWN_FIELD;
}
static uint32_t next_utf8(const unsigned char **text) {
    uint32_t value = *(*text)++;
    unsigned remaining = 0;
    if (value >= 0xf0) { value &= 7; remaining = 3; }
    else if (value >= 0xe0) { value &= 15; remaining = 2; }
    else if (value >= 0xc0) { value &= 31; remaining = 1; }
    while (remaining-- && **text) value = (value << 6) | (*(*text)++ & 63);
    return value;
}
static void set_text(bosun_display_label_t *label, const char *text, unsigned *status) {
    const unsigned char *cursor = (const unsigned char *)text;
    unsigned columns = 0, max_columns = 0, lines = 1;
    label->length = 0;
    while (*cursor && label->length < BOSUN_DISPLAY_GLYPHS) {
        uint32_t code = next_utf8(&cursor);
        uint8_t glyph = '?';
        if (code == '\n') { glyph = '\n'; ++lines; columns = 0; }
        else {
            if (code >= 32 && code < 127) glyph = (uint8_t)code;
            else {
                bool found = false;
                for (unsigned i = 0; i < 128; ++i) {
                    if (bosun_cp437_unicode[i] == code) { glyph = (uint8_t)(i + 128); found = true; break; }
                }
                if (!found) *status |= BOSUN_DISPLAY_MISSING_GLYPH;
            }
            if (++columns > max_columns) max_columns = columns;
        }
        label->glyphs[label->length++] = glyph;
    }
    if (*cursor) *status |= BOSUN_DISPLAY_LIMIT;
    label->width = (uint16_t)(max_columns * CELL_W * label->scale);
    label->height = (uint16_t)(lines * CELL_H * label->scale);
}
static uint16_t scroll_offset(uint32_t elapsed, uint16_t span, uint16_t speed) {
    if (!span || !speed) return 0;
    uint32_t travel = (uint32_t)span * 1000u / speed;
    if (!travel) return span;
    uint32_t phase = elapsed % (2u * (SCROLL_PAUSE + travel));
    if (phase < SCROLL_PAUSE) return 0;
    phase -= SCROLL_PAUSE;
    /* Long labels at slow speeds can exceed 2^32 in this intermediate even
     * though every pixel coordinate and the final offset fit in uint16_t. */
    if (phase < travel) return (uint16_t)((uint64_t)span * phase / travel);
    phase -= travel;
    if (phase < SCROLL_PAUSE) return span;
    phase -= SCROLL_PAUSE;
    return (uint16_t)((uint64_t)span * (travel - phase) / travel);
}
static void add_label(bosun_display_t *display, const bosun_json_doc_t *doc, int entry,
                      const bosun_config_t *config, const bosun_kemper_state *kemper) {
    if (display->count >= BOSUN_DISPLAY_LABELS) { display->status |= BOSUN_DISPLAY_LIMIT; return; }
    if (entry < 0 || doc->tokens[entry].type != BOSUN_JSON_OBJECT) { display->status |= BOSUN_DISPLAY_INVALID_LAYOUT; return; }
    bosun_display_label_t *label = &display->labels[display->count++];
    memset(label, 0, sizeof *label);
    int size = bosun_config_int(doc, entry, "size", 1);
    if (size < 1 || size > 8) display->status |= BOSUN_DISPLAY_LIMIT;
    label->scale = (uint8_t)clamp(size, 1, 8);
    label->color = label_color(doc, entry);
    char field[48], value[192], prefix[96], suffix[96], text[384];
    string_field(doc, entry, "field", field, sizeof field, &display->status);
    if (*field) resolve(field, config, kemper, display->hold_effect, value, sizeof value, &display->status);
    else string_field(doc, entry, "text", value, sizeof value, &display->status);
    if (*field && !*value) { label->scale = 1; return; }
    string_field(doc, entry, "prefix", prefix, sizeof prefix, &display->status);
    string_field(doc, entry, "suffix", suffix, sizeof suffix, &display->status);
    int written = snprintf(text, sizeof text, "%s%s%s", prefix, value, suffix);
    if (written < 0 || (size_t)written >= sizeof text) display->status |= BOSUN_DISPLAY_LIMIT;
    set_text(label, text, &display->status);
    unsigned horizontal = alignment(doc, entry, "halign"), vertical = alignment(doc, entry, "valign");
    int32_t x = clamp(bosun_config_int(doc, entry, "x", 0), INT16_MIN, INT16_MAX);
    int32_t y = clamp(bosun_config_int(doc, entry, "y", 0), INT16_MIN, INT16_MAX);
    label->x = (int16_t)clamp(x + (240 - (int32_t)label->width) * (int)horizontal / 2, INT16_MIN, INT16_MAX);
    label->y = (int16_t)clamp(y + (240 - (int32_t)label->height) * (int)vertical / 2, INT16_MIN, INT16_MAX);
    if (bosun_config_bool(doc, entry, "scroll", false) && label->width > 240 - 2 * SCROLL_MARGIN) {
        label->x = SCROLL_MARGIN;
        label->scroll_span = label->width - (240 - 2 * SCROLL_MARGIN);
        int speed = bosun_config_int(doc, entry, "scroll_speed", 40);
        label->scroll_speed = (uint16_t)clamp(speed > 0 ? speed : 40, 1, 1000);
        display->scrolling = true;
    }
}
static void simple_label(bosun_display_t *display, const char *text, uint8_t scale,
                         uint16_t color, int16_t center_y) {
    bosun_display_label_t *label = &display->labels[display->count++];
    memset(label, 0, sizeof *label);
    label->scale = scale; label->color = color;
    set_text(label, text, &display->status);
    label->x = (int16_t)((240 - (int)label->width) / 2);
    label->y = (int16_t)(center_y - label->height / 2);
}
/* Rotation and backlight; both are resent with the first frame. */
static void panel(bosun_display_t *display, const bosun_config_t *config) {
    int tft = bosun_json_get(&config->device_doc, 0, "tft");
    int rotation = bosun_config_int(&config->device_doc, tft, "rotation", 180);
    if (rotation < 0 || rotation > 270 || rotation % 90) { rotation = 180; display->status |= BOSUN_DISPLAY_INVALID_LAYOUT; }
    if (!display->has_frame || display->rotation != (uint16_t)rotation) {
        if (!bosun_board_display_rotation((uint16_t)rotation)) display->status |= BOSUN_DISPLAY_IO;
        display->rotation = (uint16_t)rotation;
    }
    /* Existing device.json stores brightness as a percentage. */
    int brightness = clamp(bosun_config_int(&config->device_doc, tft, "brightness", 80), 0, 100);
    uint8_t level = (uint8_t)((brightness * 255 + 50) / 100);
    if (!display->has_frame || level != display->brightness) bosun_board_display_brightness(level);
    display->brightness = level;
}
static void prepare(bosun_display_t *display, const bosun_config_t *config,
                    const bosun_kemper_state *kemper) {
    display->status = BOSUN_DISPLAY_OK;
    display->count = 0; display->scrolling = false;
    panel(display, config);
    int tft = bosun_json_get(&config->device_doc, 0, "tft");
    display->tuner = kemper && kemper->tuner_active;
    if (display->tuner) {
        int deviation = clamp((int)kemper->tuner_deviance - 8192, -2000, 2000);
        bool in_tune = deviation >= -350 && deviation <= 350;
        display->tuner_color = in_tune ? rgb565(0x00ff88) : rgb565(0xff4444);
        display->tuner_x = (uint16_t)(10 + (deviation + 2000) * 212 / 4000);
        simple_label(display, kemper->tuner_note[0] ? kemper->tuner_note : "-", 8,
                     in_tune ? display->tuner_color : 0xffff, 80);
        char footer[48];
        if (in_tune) snprintf(footer, sizeof footer, "IN TUNE");
        else if (deviation < 0) snprintf(footer, sizeof footer, "<< FLAT  %u", (unsigned)(-deviation + 7) / 8);
        else snprintf(footer, sizeof footer, "SHARP %u >>", (unsigned)deviation / 8);
        simple_label(display, footer, 2, display->tuner_color, 200);
        return;
    }
    int layout = bosun_json_get(&config->device_doc, tft, "layout");
    if (layout >= 0 && config->device_doc.tokens[layout].type != BOSUN_JSON_ARRAY)
        display->status |= BOSUN_DISPLAY_INVALID_LAYOUT;
    for (unsigned i = 0; i <= BOSUN_DISPLAY_LABELS; ++i) {
        int entry = bosun_json_at(&config->device_doc, layout, i);
        if (entry < 0) break;
        add_label(display, &config->device_doc, entry, config, kemper);
    }
    if (!display->count) {
        char name[192];
        resolve("patch_name", config, kemper, display->hold_effect, name, sizeof name, &display->status);
        simple_label(display, *name ? name : "Bosun", 3, 0xffff, 120);
    }
}
static void row_label(bosun_display_t *display, const bosun_display_label_t *label) {
    int y = (int)display->row - label->y;
    if (y < 0 || y >= label->height || !label->length) return;
    int x = label->x - scroll_offset(display->frame_ms - display->animation_started_ms,
                                     label->scroll_span, label->scroll_speed);
    unsigned local_y = (unsigned)y / label->scale;
    unsigned line_number = local_y / CELL_H, character = 0, line = 0;
    int glyph_y = (int)(local_y % CELL_H) - 2;
    for (unsigned i = 0; i < label->length; ++i) {
        uint8_t glyph = label->glyphs[i];
        if (glyph == '\n') { ++line; character = 0; continue; }
        if (line == line_number && glyph_y >= 0 && glyph_y < 8) {
            int left = x + (int)character * CELL_W * label->scale;
            for (unsigned col = 0; col < 5; ++col) {
                if (!(font[glyph * 5u + col] & (1u << glyph_y))) continue;
                for (unsigned scale_x = 0; scale_x < label->scale; ++scale_x) {
                    int dest = left + (int)(col * label->scale + scale_x);
                    if (dest >= 0 && dest < 240) display->line[dest] = label->color;
                }
            }
        }
        ++character;
    }
}
/* Fills out with row y (0..199) of the logo box: one bounded storage read of
 * the custom logo, else the built-in runs from *run, left after the row.
 * False when the custom file cannot supply the row. */
static bool logo_row(bool custom, unsigned y, uint16_t *out, uint16_t *run) {
    if (custom) {
        size_t length = 0;
        if (bosun_store_read_at(BOSUN_BOOT_LOGO_PATH, y * (uint32_t)LOGO_ROW_BYTES, out,
                                LOGO_ROW_BYTES, &length) != BOSUN_STORE_OK || length != LOGO_ROW_BYTES)
            return false;
#if defined(__BYTE_ORDER__) && __BYTE_ORDER__ == __ORDER_BIG_ENDIAN__
        for (unsigned x = 0; x < 240; ++x) out[x] = (uint16_t)(out[x] << 8 | out[x] >> 8);
#endif
        return true;
    }
    memset(out, 0, LOGO_ROW_BYTES);
    if (y >= BOOT_LOGO_Y && y < BOOT_LOGO_Y + BOOT_LOGO_SIZE)
        for (unsigned x = BOOT_LOGO_X; x < BOOT_LOGO_X + BOOT_LOGO_SIZE && *run < BOOT_LOGO_RUNS; ++*run)
            for (unsigned n = 0; n < boot_logo_runs[*run][0] && x < BOOT_LOGO_X + BOOT_LOGO_SIZE; ++n)
                out[x++] = boot_logo_runs[*run][1];
    return true;
}
/* Fills line with logo row display->row and returns the built-in run cursor
 * after it; the caller keeps that cursor only once the row is on the panel.
 * A custom row costs one bounded storage read; any failure redraws the whole
 * frame with the built-in logo instead. */
static uint16_t logo_line(bosun_display_t *display) {
    uint16_t run = display->logo_run;
    if (display->row >= BOSUN_BOOT_LOGO_HEIGHT) {
        memset(display->line, 0, sizeof display->line);
        for (unsigned label = 0; label < display->count; ++label) row_label(display, &display->labels[label]);
        return run;
    }
    if (!logo_row(display->logo_custom, display->row, display->line, &run)) {
        display->logo_custom = false; display->row = 0; run = 0;
        (void)logo_row(false, 0, display->line, &run);
    }
    return run;
}
/* True while the startup logo owns the panel. */
static bool logo(bosun_display_t *display, const bosun_config_t *config, uint32_t now_ms) {
    if (display->logo == LOGO_START) {
        display->status = BOSUN_DISPLAY_OK;
        display->count = 0; display->scrolling = false;
        panel(display, config);
        simple_label(display, display->updating ? "Updating" : "v" BOSUN_FIRMWARE_VERSION, 2, rgb565(0x9aa1ad), 220);
        display->row = 0; display->logo_run = 0; display->logo_custom = true;
        display->logo = LOGO_DRAW;
    }
    if (display->logo == LOGO_DRAW) {
        for (unsigned rows = 0; rows < ROWS_PER_TICK && display->row < 240; ++rows) {
            uint16_t next = logo_line(display);
            if (!bosun_board_display_blit_rgb565(0, (int16_t)display->row, 240, 1, display->line, 240)) {
                display->status |= BOSUN_DISPLAY_IO;
                return true;
            }
            display->logo_run = next;
            ++display->row;
        }
        if (display->row < 240) return true;
        display->logo = LOGO_HOLD; display->logo_ms = now_ms;
        return true;
    }
    if (display->updating || now_ms - display->logo_ms < display->logo_duration) return true;
    bosun_display_skip_logo(display);
    return false;
}
static uint16_t average(uint16_t a, uint16_t b, uint16_t c, uint16_t d) {
    unsigned red = (a >> 11) + (b >> 11) + (c >> 11) + (d >> 11);
    unsigned green = (a >> 5 & 63u) + (b >> 5 & 63u) + (c >> 5 & 63u) + (d >> 5 & 63u);
    unsigned blue = (a & 31u) + (b & 31u) + (c & 31u) + (d & 31u);
    return (uint16_t)((red + 2) / 4 << 11 | (green + 2) / 4 << 5 | (blue + 2) / 4);
}
/* Row y of the half-size logo in line[0..119]: each pixel averages a 2x2
 * block of the two source rows; the second one is a stack temporary. */
static bool half_row(bosun_display_t *display, unsigned y, uint16_t *run) {
    uint16_t below[BOSUN_DISPLAY_WIDTH];
    if (!logo_row(display->logo_custom, 2 * y, display->line, run) ||
        !logo_row(display->logo_custom, 2 * y + 1, below, run)) return false;
    for (unsigned x = 0; x < HALF_W; ++x)
        display->line[x] = average(display->line[2 * x], display->line[2 * x + 1], below[2 * x], below[2 * x + 1]);
    return true;
}
static void backlight(bosun_display_t *display, unsigned level) {
    if (level == display->saver_level) return;
    display->saver_level = (uint8_t)level;
    bosun_board_display_brightness((uint8_t)level);
}
/* From low (amount 0) to the configured level (amount 1024). The squared
 * smoothstep makes the fade look even on the linear PWM backlight. */
static unsigned fade(const bosun_display_t *display, unsigned low, uint32_t amount) {
    uint32_t eased = amount * amount * (3072u - 2u * amount) >> 20;
    return low + ((display->brightness - low) * (eased * eased >> 10) >> 10);
}
static unsigned saver_random(bosun_display_t *display, unsigned range) {
    display->saver_seed = display->saver_seed * 1664525u + 1013904223u;
    return (display->saver_seed >> 16) % range;
}
static void blank(bosun_display_t *display, int x, int y, int width, int height) {
    if (!bosun_board_display_fill_rect((int16_t)x, (int16_t)y, (uint16_t)width, (uint16_t)height, 0))
        display->status |= BOSUN_DISPLAY_IO;
}
/* The panel is black: place the logo box for the effect and draw it. */
static void saver_begin(bosun_display_t *display, uint32_t now_ms) {
    display->saver_seed ^= now_ms;
    if (display->saver == BOSUN_SCREENSAVER_BREATHE) {
        display->saver_x = 0; display->saver_y = (240 - BOSUN_BOOT_LOGO_HEIGHT) / 2;
    } else {
        display->saver_x = (int16_t)saver_random(display, 240 - HALF_W + 1);
        display->saver_y = (int16_t)saver_random(display, 240 - HALF_H + 1);
    }
    display->saver_dx = saver_random(display, 2) ? BOUNCE_STEP : -BOUNCE_STEP;
    display->saver_dy = saver_random(display, 2) ? BOUNCE_STEP : -BOUNCE_STEP;
    /* Drift fades in from a dark panel; the others draw at the configured level. */
    backlight(display, display->saver == BOSUN_SCREENSAVER_DRIFT ? 0 : display->brightness);
    display->saver_phase = SAVER_DRAW; display->saver_ms = now_ms;
    display->row = 0; display->logo_run = 0;
}
/* Moves the half-size logo one step, reversing at the panel edges, and
 * blanks only the strips of the old box that the new one leaves exposed. */
static void bounce_step(bosun_display_t *display) {
    int x = display->saver_x, y = display->saver_y;
    if (x + display->saver_dx < 0 || x + display->saver_dx > 240 - HALF_W) display->saver_dx = (int8_t)-display->saver_dx;
    if (y + display->saver_dy < 0 || y + display->saver_dy > 240 - HALF_H) display->saver_dy = (int8_t)-display->saver_dy;
    int nx = x + display->saver_dx, ny = y + display->saver_dy;
    if (nx > x) blank(display, x, y, nx - x, HALF_H);
    else blank(display, nx + HALF_W, y, x - nx, HALF_H);
    if (ny > y) blank(display, x, y, HALF_W, ny - y);
    else blank(display, x, ny + HALF_H, HALF_W, y - ny);
    display->saver_x = (int16_t)nx; display->saver_y = (int16_t)ny;
    display->saver_phase = SAVER_DRAW; display->row = 0; display->logo_run = 0;
}
static void screensaver(bosun_display_t *display, const bosun_config_t *config, uint32_t now_ms) {
    if (display->saver_phase == SAVER_START) {
        display->status = BOSUN_DISPLAY_OK;
        panel(display, config);
        display->saver_level = display->brightness;
        display->row = 0; display->logo_custom = true;
        display->saver_phase = SAVER_CLEAR;
    }
    if (display->saver_phase == SAVER_CLEAR) {
        blank(display, 0, display->row, 240, ROWS_PER_TICK);
        display->row = (uint16_t)(display->row + ROWS_PER_TICK);
        if (display->row >= 240) saver_begin(display, now_ms);
        return;
    }
    if (display->saver_phase == SAVER_DRAW) {
        /* Half-size rows read two source rows: four of them per render. */
        bool half = display->saver != BOSUN_SCREENSAVER_BREATHE;
        unsigned width = half ? HALF_W : 240, height = half ? HALF_H : BOSUN_BOOT_LOGO_HEIGHT;
        for (unsigned rows = 0; rows < (half ? ROWS_PER_TICK / 2 : ROWS_PER_TICK) && display->row < height; ++rows) {
            uint16_t run = display->logo_run;
            if (!(half ? half_row(display, display->row, &run)
                       : logo_row(display->logo_custom, display->row, display->line, &run))) {
                /* The custom file failed: redraw the box with the built-in logo. */
                display->logo_custom = false; display->row = 0; display->logo_run = 0;
                return;
            }
            if (!bosun_board_display_blit_rgb565(display->saver_x, (int16_t)(display->saver_y + display->row),
                                                 (uint16_t)width, 1, display->line, (uint16_t)width)) {
                display->status |= BOSUN_DISPLAY_IO;
                return;
            }
            display->logo_run = run;
            ++display->row;
        }
        if (display->row < height) return;
        display->saver_phase = SAVER_SHOW;
        if (display->saver != BOSUN_SCREENSAVER_BOUNCE) display->saver_ms = now_ms;
        return;
    }
    uint32_t elapsed = now_ms - display->saver_ms;
    if (display->saver == BOSUN_SCREENSAVER_BOUNCE) {
        /* Paced from the start of the previous step, however long it drew. */
        if (elapsed < BOUNCE_MS) return;
        display->saver_ms = now_ms;
        bounce_step(display);
    } else if (display->saver == BOSUN_SCREENSAVER_BREATHE) {
        uint32_t phase = elapsed % BREATHE_MS, half = BREATHE_MS / 2;
        backlight(display, fade(display, (display->brightness + 7u) / 8u,
                                (phase < half ? half - phase : phase - half) * 1024u / half));
    } else if (elapsed < DRIFT_FADE_MS) {
        backlight(display, fade(display, 0, elapsed * 1024u / DRIFT_FADE_MS));
    } else if (elapsed < DRIFT_FADE_MS + DRIFT_HOLD_MS) {
        backlight(display, display->brightness);
    } else if (elapsed < 2u * DRIFT_FADE_MS + DRIFT_HOLD_MS) {
        backlight(display, fade(display, 0, (2u * DRIFT_FADE_MS + DRIFT_HOLD_MS - elapsed) * 1024u / DRIFT_FADE_MS));
    } else {
        /* Dark: clear the panel and appear somewhere else. */
        backlight(display, 0);
        display->saver_phase = SAVER_CLEAR; display->row = 0;
    }
}
void bosun_display_screensaver(bosun_display_t *display, unsigned effect) {
    if (!display || effect > BOSUN_SCREENSAVER_DRIFT || effect == display->saver) return;
    if (display->saver && effect) {
        /* Mix: clear at the current backlight, so the old effect never flashes. */
        display->saver = (uint8_t)effect; display->saver_phase = SAVER_CLEAR;
        display->row = 0; display->logo_custom = true;
        return;
    }
    display->saver = (uint8_t)effect; display->saver_phase = SAVER_START;
    /* The layout, or a logo that owns the panel meanwhile, resends the backlight. */
    display->has_frame = false;
    if (!effect && display->logo == LOGO_OFF) display->row = 240;
}
bool bosun_display_logo_active(const bosun_display_t *display) {
    return display && display->logo != LOGO_OFF;
}
void bosun_display_logo(bosun_display_t *display, uint16_t duration_ms) {
    if (!display) return;
    if (!duration_ms) { bosun_display_skip_logo(display); return; }
    display->logo = LOGO_START; display->logo_duration = duration_ms;
    display->saver = BOSUN_SCREENSAVER_OFF;
}
void bosun_display_skip_logo(bosun_display_t *display) {
    if (!display || display->updating || display->logo == LOGO_OFF) return;
    display->logo = LOGO_OFF; display->has_frame = false; display->row = 240;
}
void bosun_display_updating(bosun_display_t *display, bool active) {
    if (!display || display->updating == active) return;
    display->updating = active;
    if (active) { /* Takes over any logo, screensaver or layout frame. */
        display->logo = LOGO_START; display->saver = BOSUN_SCREENSAVER_OFF;
    } else bosun_display_skip_logo(display);
}
void bosun_display_init(bosun_display_t *display) {
    if (!display) return;
    memset(display, 0, sizeof *display);
    display->row = 240; display->logo = LOGO_BOOT;
}
unsigned bosun_display_render(bosun_display_t *display, const bosun_config_t *config,
                              const bosun_kemper_state *kemper, const char *hold_effect,
                              uint32_t now_ms) {
    if (!display || !config) return BOSUN_DISPLAY_INVALID_LAYOUT;
    if (!hold_effect) hold_effect = "";
    if (display->logo == LOGO_BOOT) {
        int tft = bosun_json_get(&config->device_doc, 0, "tft");
        int seconds = clamp(bosun_config_int(&config->device_doc, tft, "boot_logo_seconds", 2), 0, 60);
        display->logo = LOGO_OFF;
        bosun_display_logo(display, (uint16_t)(seconds * 1000));
    }
    if (display->logo != LOGO_OFF && logo(display, config, now_ms)) return display->status;
    if (display->saver) { screensaver(display, config, now_ms); return display->status; }
    uint32_t kemper_revision = kemper ? kemper->revision : 0;
    if (display->row >= 240) {
        bool changed = !display->has_frame || config->revision != display->config_revision ||
            config->patch_revision != display->patch_revision || kemper_revision != display->kemper_revision ||
            strcmp(display->hold_effect, hold_effect);
        if (!changed && (!display->scrolling || now_ms - display->frame_ms < 40)) return display->status;
        if (changed) display->animation_started_ms = now_ms;
        snprintf(display->hold_effect, sizeof display->hold_effect, "%s", hold_effect);
        prepare(display, config, kemper);
        display->config_revision = config->revision;
        display->patch_revision = config->patch_revision;
        display->kemper_revision = kemper_revision;
        display->frame_ms = now_ms;
        display->row = 0;
        display->has_frame = true;
    }
    for (unsigned rows = 0; rows < ROWS_PER_TICK && display->row < 240; ++rows) {
        memset(display->line, 0, sizeof display->line);
        for (unsigned label = 0; label < display->count; ++label) row_label(display, &display->labels[label]);
        if (display->tuner) {
            for (unsigned x = 0; x < 240; ++x) {
                if (display->row >= 150 && display->row < 158 && x >= 10 && x < 230) display->line[x] = rgb565(0x333333);
                if (display->row >= 144 && display->row < 164 && x >= 119 && x <= 120) display->line[x] = rgb565(0x999999);
                if (display->row >= 139 && display->row < 169 && x >= display->tuner_x && x < display->tuner_x + 8u)
                    display->line[x] = display->tuner_color;
            }
        }
        if (!bosun_board_display_blit_rgb565(0, (int16_t)display->row, 240, 1, display->line, 240)) {
            display->status |= BOSUN_DISPLAY_IO;
            return display->status;
        }
        ++display->row;
    }
    return display->status;
}
