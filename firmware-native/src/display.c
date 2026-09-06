/* SPDX-License-Identifier: GPL-3.0-or-later */
#include "bosun/display.h"
#include <limits.h>
#include <stdio.h>
#include <string.h>
#include "../third_party/adafruit-gfx-font/glcdfont.c"
#include "../third_party/adafruit-gfx-font/unicode_cp437.h"

enum { CELL_W = 6, CELL_H = 12, ICON_W = 16, ICON_GAP = 2,
       SCROLL_MARGIN = 6, SCROLL_PAUSE = 800, ROWS_PER_TICK = 8 };

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
    } else if (!strcmp(field, "kemper_bpm")) {
        if (kemper) snprintf(out, capacity, "%u", kemper->bpm);
    } else if (!strcmp(field, "kemper_connected")) {
        if (kemper) snprintf(out, capacity, "%s", kemper->connected ? "on" : "off");
    } else if (!strcmp(field, "tuner") || !strcmp(field, "kemper_tuner")) {
        if (kemper) snprintf(out, capacity, "%s", kemper->tuner_active ? "on" : "off");
    } else if (!strcmp(field, "tuner_note") || !strcmp(field, "kemper_tuner_note")) {
        if (kemper) snprintf(out, capacity, "%s", kemper->tuner_note);
    } else if (!strcmp(field, "tuner_deviance") || !strcmp(field, "kemper_tuner_deviance")) {
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
    label->width = (uint16_t)((max_columns * CELL_W + (label->badge ? ICON_W + ICON_GAP : 0)) * label->scale);
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
    char field[48], value[192], prefix[96], suffix[96], text[384], font_name[64];
    string_field(doc, entry, "font", font_name, sizeof font_name, &display->status);
    if (*font_name && strcmp(font_name, "system")) display->status |= BOSUN_DISPLAY_UNSUPPORTED_FONT;
    string_field(doc, entry, "field", field, sizeof field, &display->status);
    if (*field) resolve(field, config, kemper, display->hold_effect, value, sizeof value, &display->status);
    else string_field(doc, entry, "text", value, sizeof value, &display->status);
    label->badge = !strcmp(field, "expression_mode");
    label->icon = label->badge && kemper ? (uint8_t)kemper->expression_mode : 0;
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
static void prepare(bosun_display_t *display, const bosun_config_t *config,
                    const bosun_kemper_state *kemper) {
    display->status = BOSUN_DISPLAY_OK;
    display->count = 0; display->scrolling = false;
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
        simple_label(display, *name ? name : "Bosun Native", 3, 0xffff, 120);
    }
}
static bool icon_pixel(uint8_t icon, int x, int y) {
    if (x < 1 || x > 14 || y < 0 || y >= CELL_H) return false;
    if (icon == BOSUN_EXPRESSION_VOL)
        return y >= 2 && y <= 10 && 13 * (10 - y) <= 8 * (x - 1);
    if (icon == BOSUN_EXPRESSION_WAH) {
        int treadle = 6 - (x - 1) * 4 / 13;
        return y == treadle || y == treadle + 1 || y == 9 || y == 10 || ((x == 7 || x == 8) && y >= 6 && y <= 8);
    }
    return false;
}
static void row_label(bosun_display_t *display, const bosun_display_label_t *label) {
    int y = (int)display->row - label->y;
    if (y < 0 || y >= label->height || !label->length) return;
    int x = label->x - scroll_offset(display->frame_ms - display->animation_started_ms,
                                     label->scroll_span, label->scroll_speed);
    unsigned local_y = (unsigned)y / label->scale;
    int text_x = x + (label->badge ? (ICON_W + ICON_GAP) * label->scale : 0);
    unsigned line_number = local_y / CELL_H, character = 0, line = 0;
    int glyph_y = (int)(local_y % CELL_H) - 2;
    if (label->badge) {
        int icon_y = (int)local_y - ((int)label->height / label->scale - CELL_H) / 2;
        for (int px = 0; px < ICON_W * label->scale; ++px) {
            int dest = x + px;
            if (dest >= 0 && dest < 240 && icon_pixel(label->icon, px / label->scale, icon_y))
                display->line[dest] = label->color;
        }
    }
    for (unsigned i = 0; i < label->length; ++i) {
        uint8_t glyph = label->glyphs[i];
        if (glyph == '\n') { ++line; character = 0; continue; }
        if (line == line_number && glyph_y >= 0 && glyph_y < 8) {
            int left = text_x + (int)character * CELL_W * label->scale;
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
void bosun_display_init(bosun_display_t *display) {
    if (!display) return;
    memset(display, 0, sizeof *display);
    display->row = 240;
}
unsigned bosun_display_render(bosun_display_t *display, const bosun_config_t *config,
                              const bosun_kemper_state *kemper, const char *hold_effect,
                              uint32_t now_ms) {
    if (!display || !config) return BOSUN_DISPLAY_INVALID_LAYOUT;
    if (!hold_effect) hold_effect = "";
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
