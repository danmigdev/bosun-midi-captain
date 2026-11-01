#define _POSIX_C_SOURCE 200809L
#include "bosun/display.h"
#include "bosun/protocol.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "../src/boot_logo.c" /* Independent decode of the built-in asset. */

static uint16_t pixels[240][240]; /* Host fake only: never linked into RP2040. */
static uint32_t writes;
static uint16_t rotation;
static uint8_t brightness;
static uint32_t fills, brightness_writes;
static bool fail_write, half_rows;
static bosun_config_t config;
static bosun_display_t display;
static bosun_kemper_state kemper;
static char hold_effect[129];

bool bosun_board_display_rotation(uint16_t value) { rotation = value; return value <= 270 && value % 90 == 0; }
void bosun_board_display_brightness(uint8_t value) { brightness = value; ++brightness_writes; }
bool bosun_board_display_blit_rgb565(int16_t x, int16_t y, uint16_t w, uint16_t h,
                                    const uint16_t *source, uint16_t stride) {
    assert(x >= 0 && y >= 0 && x + w <= 240 && y + h <= 240);
    /* A single stripe of at most 480 bytes: a whole row, or a half-size
     * screensaver row while half_rows allows it. */
    assert(h == 1 && stride == w && (w == 240 || (half_rows && w == 120)));
    if (fail_write) return false;
    memcpy(&pixels[y][x], source, w * sizeof(*source));
    ++writes;
    return true;
}
bool bosun_board_display_fill_rect(int16_t x, int16_t y, uint16_t w, uint16_t h, uint16_t color) {
    assert(x >= 0 && y >= 0 && w && h && x + w <= 240 && y + h <= 240);
    if (fail_write) return false;
    for (unsigned row = 0; row < h; ++row)
        for (unsigned column = 0; column < w; ++column) pixels[y + row][x + column] = color;
    ++fills;
    return true;
}
static void load(const char *device, const char *patch) {
    memset(&config, 0, sizeof config);
    memset(&kemper, 0, sizeof kemper);
    hold_effect[0] = 0;
    memset(pixels, 0xa5, sizeof pixels);
    config.bank = 2; config.slot = 3;
    config.revision = 1; config.patch_revision = 1;
    assert(strlen(device) < sizeof config.device && strlen(patch) < sizeof config.patch);
    strcpy(config.device, device); strcpy(config.patch, patch);
    assert(bosun_json_parse(&config.device_doc, config.device, strlen(device), config.device_tokens, BOSUN_DEVICE_TOKENS) == BOSUN_JSON_OK);
    assert(bosun_json_parse(&config.patch_doc, config.patch, strlen(patch), config.patch_tokens, BOSUN_PATCH_TOKENS) == BOSUN_JSON_OK);
    bosun_display_init(&display);
    bosun_display_skip_logo(&display); /* Layout cases; the logo has its own. */
    writes = 0; fail_write = false;
}
static unsigned frame(uint32_t now) {
    unsigned status = 0;
    uint32_t before = writes;
    for (unsigned i = 0; i < 30; ++i) {
        uint32_t previous = writes;
        status = bosun_display_render(&display, &config, &kemper, hold_effect, now);
        assert(writes - previous <= 8);
    }
    assert(writes - before == 240);
    return status;
}
static unsigned colored(uint16_t color) {
    unsigned count = 0;
    for (unsigned y = 0; y < 240; ++y)
        for (unsigned x = 0; x < 240; ++x) if (pixels[y][x] == color) ++count;
    return count;
}
static void geometry_and_colors(void) {
    load("{\"tft\":{\"rotation\":90,\"brightness\":40,\"layout\":["
         "{\"text\":\"E\",\"x\":10,\"y\":20,\"size\":2,\"color\":\"#ff0000\"},"
         "{\"text\":\"E\",\"x\":-2,\"y\":-2,\"halign\":\"right\",\"valign\":\"bottom\",\"size\":1,\"color\":65280}]} }", "{}");
    assert(frame(0) == BOSUN_DISPLAY_OK);
    assert(rotation == 90 && brightness == 102);
    assert(display.labels[0].x == 10 && display.labels[0].y == 20);
    assert(display.labels[1].x == 232 && display.labels[1].y == 226);
    for (unsigned x = 10; x < 20; ++x) assert(pixels[24][x] == 0xf800 && pixels[25][x] == 0xf800);
    assert(pixels[26][10] == 0xf800 && pixels[26][12] == 0);
    assert(pixels[228][232] == 0x07e0 && pixels[228][236] == 0x07e0);
    assert(colored(0xf800) == 18 * 4 && colored(0x07e0) == 18);
    uint32_t before = writes;
    assert(bosun_display_render(&display, &config, &kemper, hold_effect, 1) == BOSUN_DISPLAY_OK);
    assert(writes == before); /* Unchanged frame has no display I/O. */
}
static void expression_labels_are_text_only(void) {
    const char *layout = "{\"tft\":{\"layout\":[{\"field\":\"expression_mode\","
        "\"x\":-6,\"y\":-6,\"halign\":\"right\",\"valign\":\"bottom\",\"size\":2,\"color\":\"#ffffff\"}]}}";
    load(layout, "{}");
    const bosun_expression_mode modes[] = {BOSUN_EXPRESSION_VOL, BOSUN_EXPRESSION_WAH, BOSUN_EXPRESSION_OFF};
    const char *texts[] = {"VOL", "WAH", "OFF"};
    for (unsigned i = 0; i < 3; ++i) {
        kemper.expression_mode = modes[i]; ++kemper.revision;
        assert(frame(i) == BOSUN_DISPLAY_OK);
        assert(display.labels[0].x == 198 && display.labels[0].y == 210);
        assert(display.labels[0].width == 36 && display.labels[0].height == 24);
        assert(display.labels[0].length == 3 && !memcmp(display.labels[0].glyphs, texts[i], 3));
        assert(colored(0xffff) > 0);
        /* The former icon area is empty for every live expression mode. */
        for (unsigned y = 210; y < 234; ++y)
            for (unsigned x = 162; x < 198; ++x) assert(pixels[y][x] == 0);
    }
    kemper.expression_mode = BOSUN_EXPRESSION_UNKNOWN; ++kemper.revision;
    assert(frame(3) == BOSUN_DISPLAY_OK);
    assert(display.labels[0].length == 0 && colored(0xffff) == 0);
    load("{\"tft\":{\"layout\":[{\"field\":\"expression_mode\",\"prefix\":\"WAH \",\"size\":1}]}}", "{}");
    kemper.expression_mode = BOSUN_EXPRESSION_VOL;
    assert(frame(0) == 0);
    assert(display.labels[0].width == 42);
    assert(display.labels[0].length == 7 && !memcmp(display.labels[0].glyphs, "WAH VOL", 7));
}
static void fields_and_clipping(void) {
    load("{\"tft\":{\"layout\":[{\"field\":\"patch_name\",\"prefix\":\"RIG \",\"suffix\":\"!\",\"x\":-10},"
        "{\"field\":\"missing_field\",\"prefix\":\"ORPHAN \"},"
        "{\"text\":\"Caf\\u00e9\",\"y\":20}]}}", "{\"name\":\"Crunch\"}");
    unsigned status = frame(0);
    assert(status == BOSUN_DISPLAY_UNKNOWN_FIELD);
    assert(display.labels[0].length == 11 && !memcmp(display.labels[0].glyphs, "RIG Crunch!", 11));
    assert(display.labels[1].length == 0);
    assert(display.labels[2].length == 4 && display.labels[2].glyphs[3] == 0x82);
    strcpy(kemper.rig_name, "Live Name"); kemper.rig_name_fresh = true; ++kemper.revision;
    frame(10);
    assert(!memcmp(display.labels[0].glyphs, "RIG Live Name!", 14));
    /* Completely off-screen coordinates and excessive scales are bounded. */
    load("{\"tft\":{\"layout\":[{\"text\":\"X\",\"x\":2147483647,\"y\":-2147483648,\"size\":2147483647}]}}", "{}");
    assert(frame(0) & BOSUN_DISPLAY_LIMIT);
    assert(colored(0) == 240 * 240);
}
static void scroll_snapshot_and_tuner(void) {
    load("{\"tft\":{\"layout\":[{\"text\":\"ABCDEFGHIJKLMNOPQRSTUVWXYZ\",\"size\":2,\"scroll\":true,\"scroll_speed\":40}]}}", "{}");
    assert(frame(UINT32_MAX - 1000) == 0);
    assert(display.scrolling && display.labels[0].x == 6 && display.labels[0].scroll_span == 84);
    uint16_t first[240]; memcpy(first, pixels[4], sizeof first);
    assert(frame(200) == 0); /* Unsigned elapsed remains correct over wrap. */
    assert(memcmp(first, pixels[4], sizeof first));
    kemper.tuner_active = true; kemper.tuner_deviance = 8192;
    strcpy(kemper.tuner_note, "A"); ++kemper.revision;
    assert(frame(300) == 0);
    assert(display.tuner && display.count == 2 && pixels[150][119] == 0x07f1);
    assert(pixels[150][10] == 0x3186 && pixels[150][229] == 0x3186);
    kemper.tuner_deviance = 12000; ++kemper.revision;
    assert(frame(400) == 0 && display.tuner_x == 222);
    assert(pixels[150][225] == 0xfa28);
}
static void failed_transfer_is_visible(void) {
    load("{}", "{\"name\":\"Fallback\"}");
    fail_write = true;
    assert(bosun_display_render(&display, &config, &kemper, hold_effect, 0) & BOSUN_DISPLAY_IO);
    assert(writes == 0 && display.row == 0);
    fail_write = false;
    frame(1);
    assert(colored(0xffff) > 0);
}
static void slow_scroll_long_labels_do_not_overflow(void) {
    load("{\"tft\":{\"layout\":[{\"text\":\"ABCDEFGHIJKLMNOPQRSTUVWXYZ"
         "abcdefghijklmnopqrstuvwxyz0123456789!@#$%^&*()-_=+[]{}\","
         "\"size\":8,\"scroll\":true,\"scroll_speed\":1}]}}", "{}");
    assert(frame(0) == BOSUN_DISPLAY_OK);
    unsigned span = display.labels[0].scroll_span;
    assert(span > 3500);
    uint32_t travel = span * 1000u;
    const uint32_t times[] = {800 + travel / 2, 800 + travel,
        1600 + travel + travel / 2, 1600 + travel * 2};
    const unsigned offsets[] = {span / 2, span, span / 2, 0};
    for (unsigned i = 0; i < sizeof times / sizeof *times; ++i) {
        assert(frame(times[i]) == BOSUN_DISPLAY_OK);
        uint16_t observed[240]; memcpy(observed, pixels[32], sizeof observed);
        /* Compare the animated bitmap with the same glyph snapshot placed
         * at the independently known half-way/end/start pixel coordinate. */
        display.row = 0;
        display.labels[0].scroll_span = 0;
        display.labels[0].x = (int16_t)(6 - (int)offsets[i]);
        assert(frame(times[i]) == BOSUN_DISPLAY_OK);
        assert(!memcmp(observed, pixels[32], sizeof observed));
    }
}
static void momentary_hold_repaints_without_configuration_change(void) {
    /* Actual Captain layout: the hold label appears below BANK/RIG and must
     * clear on release even if neither configuration nor Kemper changed. */
    load("{\"tft\":{\"layout\":[{\"field\":\"hold_effect\",\"x\":0,\"y\":180,"
         "\"size\":5,\"font\":\"system\",\"color\":\"#ffffff\"}]}}", "{}");
    assert(frame(0) == BOSUN_DISPLAY_OK && colored(0xffff) == 0);
    strcpy(hold_effect, "BOOST");
    assert(frame(1) == BOSUN_DISPLAY_OK && colored(0xffff) > 0);
    assert(display.labels[0].length == 5 && !memcmp(display.labels[0].glyphs, "BOOST", 5));
    assert(display.labels[0].x == 0 && display.labels[0].y == 180);
    assert(config.revision == 1 && config.patch_revision == 1 && kemper.revision == 0);
    uint32_t before = writes;
    assert(bosun_display_render(&display, &config, &kemper, hold_effect, 2) == BOSUN_DISPLAY_OK);
    assert(writes == before);
    hold_effect[0] = 0;
    assert(frame(3) == BOSUN_DISPLAY_OK && colored(0xffff) == 0);
    assert(display.labels[0].length == 0);
}
static void morph_text_distinguishes_command_from_unknown(void) {
    load("{\"tft\":{\"layout\":[{\"field\":\"kemper_morph\",\"size\":2}]}}", "{}");
    kemper.morph_value = 83;
    assert(frame(0) == BOSUN_DISPLAY_OK);
    assert(display.labels[0].length == 7 && !memcmp(display.labels[0].glyphs, "SET 65%", 7));
    kemper.morph_value = -1; ++kemper.revision;
    assert(frame(1) == BOSUN_DISPLAY_OK);
    assert(display.labels[0].length == 1 && display.labels[0].glyphs[0] == '?');
}

static uint16_t builtin[240][240], custom[240][240];
static uint8_t custom_bytes[BOSUN_BOOT_LOGO_BYTES];
static void decode_builtin(void) {
    memset(builtin, 0, sizeof builtin);
    unsigned run = 0;
    for (unsigned y = 0; y < BOOT_LOGO_SIZE; ++y)
        for (unsigned x = 0; x < BOOT_LOGO_SIZE; ++run) {
            /* Runs never cross rows: every row ends exactly on a boundary. */
            assert(run < BOOT_LOGO_RUNS && boot_logo_runs[run][0] && x + boot_logo_runs[run][0] <= BOOT_LOGO_SIZE);
            for (unsigned n = 0; n < boot_logo_runs[run][0]; ++n)
                builtin[BOOT_LOGO_Y + y][BOOT_LOGO_X + x++] = boot_logo_runs[run][1];
        }
    assert(run == BOOT_LOGO_RUNS && BOOT_LOGO_Y + BOOT_LOGO_SIZE <= BOSUN_BOOT_LOGO_HEIGHT);
    assert(builtin[100][120] && !builtin[5][120] && !builtin[100][10]);
}
static void logo_frame_with(const uint16_t (*image)[240], const char *text) {
    assert(!memcmp(pixels, image, BOSUN_BOOT_LOGO_BYTES));
    assert(display.count == 1 && display.labels[0].length == strlen(text));
    assert(!memcmp(display.labels[0].glyphs, text, strlen(text)));
    assert(display.labels[0].scale == 2 && display.labels[0].y == 208);
    assert(display.labels[0].x == (240 - (int)display.labels[0].width) / 2);
    unsigned label = 0;
    for (unsigned y = BOSUN_BOOT_LOGO_HEIGHT; y < 240; ++y)
        for (unsigned x = 0; x < 240; ++x) {
            assert(pixels[y][x] == 0 || pixels[y][x] == 0x9d15); /* #9aa1ad */
            if (pixels[y][x]) { ++label; assert(y >= 208 && y < 232); }
        }
    assert(label > 0);
}
static void logo_frame_is(const uint16_t (*image)[240]) { logo_frame_with(image, "v" BOSUN_FIRMWARE_VERSION); }
static void boot(const char *device) {
    load(device, "{\"name\":\"Clean\"}");
    bosun_display_init(&display); /* load() skipped the startup logo. */
}
static void render_at(uint32_t now) {
    (void)bosun_display_render(&display, &config, &kemper, hold_effect, now);
}
static void layout_is(const char *name) {
    assert(display.count == 1 && display.labels[0].length == strlen(name));
    assert(!memcmp(display.labels[0].glyphs, name, strlen(name)));
}
static void holds_until(uint32_t start, uint32_t end) {
    uint32_t before = writes;
    for (uint32_t t = start; t - start < end - start; t += 97) render_at(t);
    render_at(end - 1);
    assert(writes == before);
    assert(frame(end) == BOSUN_DISPLAY_OK);
}
static void startup_logo_timing(void) {
    boot("{\"tft\":{\"rotation\":90,\"brightness\":40}}");
    rotation = 0; brightness = 0;
    assert(frame(100) == BOSUN_DISPLAY_OK);
    assert(rotation == 90 && brightness == 102); /* Applied before the logo. */
    logo_frame_is(builtin);
    /* Two seconds by default after the frame completes; broadcasts, config
     * and patch changes during the hold never shorten it. */
    strcpy(kemper.rig_name, "Lead"); kemper.rig_name_fresh = true; ++kemper.revision;
    ++config.revision; ++config.patch_revision;
    holds_until(100, 2100);
    layout_is("Lead");
    uint32_t before = writes;
    render_at(9000); assert(writes == before);
    boot("{\"tft\":{\"boot_logo_seconds\":0}}");
    assert(frame(0) == BOSUN_DISPLAY_OK); layout_is("Clean");
    boot("{\"tft\":{\"boot_logo_seconds\":-3}}");
    assert(frame(0) == BOSUN_DISPLAY_OK); layout_is("Clean");
    boot("{\"tft\":{\"boot_logo_seconds\":1}}");
    assert(frame(0) == BOSUN_DISPLAY_OK); logo_frame_is(builtin);
    holds_until(0, 1000); layout_is("Clean");
    boot("{\"tft\":{\"boot_logo_seconds\":600}}"); /* Clamped to 60 s, across the clock wrap. */
    assert(frame(UINT32_MAX - 10) == BOSUN_DISPLAY_OK); logo_frame_is(builtin);
    holds_until(UINT32_MAX - 10, UINT32_MAX - 10 + 60000u); layout_is("Clean");
    boot("{\"tft\":{\"boot_logo_seconds\":\"5\"}}"); /* Not an integer: default. */
    assert(frame(0) == BOSUN_DISPLAY_OK); logo_frame_is(builtin);
    holds_until(0, 2000);
}
static void startup_logo_skip_and_preview(void) {
    boot("{}");
    for (unsigned i = 0; i < 4; ++i) render_at(0);
    assert(writes == 32 && pixels[20][120] == builtin[20][120] && builtin[20][120]);
    bosun_display_skip_logo(&display); /* Mid-frame: the layout redraws from row 0. */
    assert(frame(1) == BOSUN_DISPLAY_OK); layout_is("Clean");
    for (unsigned y = 0; y < 100; ++y) for (unsigned x = 0; x < 240; ++x) assert(!pixels[y][x]);
    uint32_t before = writes;
    bosun_display_skip_logo(&display); render_at(2); assert(writes == before);
    boot("{}");
    assert(frame(0) == BOSUN_DISPLAY_OK); logo_frame_is(builtin);
    bosun_display_skip_logo(&display); /* During the hold. */
    assert(frame(5) == BOSUN_DISPLAY_OK); layout_is("Clean");
    bosun_display_logo(&display, 3000); /* Preview, independent of the boot setting. */
    assert(frame(10) == BOSUN_DISPLAY_OK); logo_frame_is(builtin);
    bosun_display_logo(&display, 3000); /* A new logo restarts the frame and its hold. */
    assert(frame(1000) == BOSUN_DISPLAY_OK); logo_frame_is(builtin);
    holds_until(1000, 4000); layout_is("Clean");
    before = writes;
    bosun_display_logo(&display, 0); render_at(5000); assert(writes == before);
    /* A failed blit keeps the row and the run cursor for the retry. */
    boot("{}");
    render_at(0); render_at(0);
    fail_write = true;
    assert(bosun_display_render(&display, &config, &kemper, hold_effect, 0) & BOSUN_DISPLAY_IO);
    assert(writes == 16 && display.row == 16);
    fail_write = false;
    for (unsigned i = 0; i < 28; ++i) render_at(0);
    assert(writes == 240); logo_frame_is(builtin);
}
static void make_custom(void) {
    for (unsigned y = 0; y < BOSUN_BOOT_LOGO_HEIGHT; ++y)
        for (unsigned x = 0; x < 240; ++x) {
            uint16_t value = (uint16_t)(y * 263u + x * 7u + 1u);
            custom[y][x] = value;
            custom_bytes[(y * 240u + x) * 2u] = (uint8_t)value;
            custom_bytes[(y * 240u + x) * 2u + 1u] = (uint8_t)(value >> 8);
        }
}
static void no_writes_until(uint32_t start, uint32_t end) {
    uint32_t before = writes;
    for (uint32_t t = start; t - start < end - start; t += 997) render_at(t);
    render_at(end);
    assert(writes == before);
}
static void updating_screen(void) {
    /* Over the layout: the logo frame with "Updating" and no timeout. */
    boot("{\"tft\":{\"boot_logo_seconds\":0}}");
    assert(frame(0) == BOSUN_DISPLAY_OK); layout_is("Clean");
    bosun_display_updating(&display, true);
    bosun_display_updating(&display, true); /* Repeated ticks keep the frame. */
    assert(frame(10) == BOSUN_DISPLAY_OK); logo_frame_with(builtin, "Updating");
    assert(display.labels[0].scale == 2 && display.labels[0].y == 208);
    /* A switch press, context changes and a 10 minute lock (across the clock
     * wrap) never end it. */
    bosun_display_skip_logo(&display);
    strcpy(kemper.rig_name, "Lead"); kemper.rig_name_fresh = true; ++kemper.revision;
    ++config.revision; ++config.patch_revision;
    no_writes_until(10, 600000);
    no_writes_until(UINT32_MAX - 1000, 5000);
    logo_frame_with(builtin, "Updating");
    /* A logo preview meanwhile redraws the Updating frame and ends with it. */
    bosun_display_logo(&display, 3000);
    assert(frame(20) == BOSUN_DISPLAY_OK); logo_frame_with(builtin, "Updating");
    bosun_display_logo(&display, 0);
    no_writes_until(20, 10000);
    /* Unlocking redraws the whole layout from row 0. */
    bosun_display_updating(&display, false);
    assert(frame(10001) == BOSUN_DISPLAY_OK); layout_is("Lead");
    for (unsigned y = 0; y < 100; ++y) for (unsigned x = 0; x < 240; ++x) assert(!pixels[y][x]);
    no_writes_until(10002, 20000);
    bosun_display_updating(&display, false); render_at(20001); assert(writes == 240 * 4);

    /* A startup hold is taken over and does not resume after the lock. */
    boot("{\"tft\":{\"boot_logo_seconds\":5}}");
    assert(frame(0) == BOSUN_DISPLAY_OK); logo_frame_is(builtin);
    bosun_display_updating(&display, true);
    assert(frame(100) == BOSUN_DISPLAY_OK); logo_frame_with(builtin, "Updating");
    no_writes_until(100, 7000);
    bosun_display_updating(&display, false);
    assert(frame(7001) == BOSUN_DISPLAY_OK); layout_is("Clean");
    no_writes_until(7002, 9000);
    /* Mid-draw takeover restarts at row 0; mid-draw unlock redraws the layout. */
    boot("{}");
    for (unsigned i = 0; i < 4; ++i) render_at(0);
    assert(writes == 32);
    bosun_display_updating(&display, true);
    assert(frame(1) == BOSUN_DISPLAY_OK && writes == 32 + 240); logo_frame_with(builtin, "Updating");
    bosun_display_updating(&display, false);
    assert(frame(2) == BOSUN_DISPLAY_OK); layout_is("Clean");
    boot("{}");
    assert(frame(0) == BOSUN_DISPLAY_OK); logo_frame_is(builtin);
    bosun_display_updating(&display, true);
    render_at(1); render_at(1); assert(writes == 240 + 16 && display.row == 16);
    bosun_display_updating(&display, false);
    assert(frame(2) == BOSUN_DISPLAY_OK); layout_is("Clean");
    /* A preview running when the lock starts does not come back either. */
    bosun_display_logo(&display, 3000);
    assert(frame(10) == BOSUN_DISPLAY_OK); logo_frame_is(builtin);
    bosun_display_updating(&display, true);
    assert(frame(20) == BOSUN_DISPLAY_OK); logo_frame_with(builtin, "Updating");
    bosun_display_updating(&display, false);
    assert(frame(30) == BOSUN_DISPLAY_OK); layout_is("Clean");
    no_writes_until(31, 5000);

    /* The custom logo, read row by row like the startup logo. */
    make_custom();
    assert(bosun_store_mkdir(BOSUN_BOOT_LOGO_DIR) == BOSUN_STORE_OK);
    assert(bosun_store_write_atomic(BOSUN_BOOT_LOGO_PATH, custom_bytes, sizeof custom_bytes) == BOSUN_STORE_OK);
    boot("{}");
    bosun_display_updating(&display, true); /* Before the first render: no startup logo. */
    assert(frame(0) == BOSUN_DISPLAY_OK); logo_frame_with(custom, "Updating");
    no_writes_until(0, 3000);
    bosun_display_updating(&display, false);
    assert(frame(3001) == BOSUN_DISPLAY_OK); layout_is("Clean");
    assert(bosun_store_remove(BOSUN_BOOT_LOGO_PATH) == BOSUN_STORE_OK);
}
static void custom_logo_rows_and_fallback(void) {
    make_custom();
    assert(bosun_store_mkdir(BOSUN_BOOT_LOGO_DIR) == BOSUN_STORE_OK);
    assert(bosun_store_write_atomic(BOSUN_BOOT_LOGO_PATH, custom_bytes, sizeof custom_bytes) == BOSUN_STORE_OK);
    boot("{}");
    assert(frame(0) == BOSUN_DISPLAY_OK); logo_frame_is(custom);
    holds_until(0, 2000); layout_is("Clean");
    /* A short file fails at row 100: the whole frame becomes the built-in logo. */
    assert(bosun_store_write_atomic(BOSUN_BOOT_LOGO_PATH, custom_bytes, sizeof custom_bytes / 2) == BOSUN_STORE_OK);
    boot("{}");
    for (unsigned i = 0; i < 60; ++i) {
        uint32_t previous = writes;
        assert(bosun_display_render(&display, &config, &kemper, hold_effect, 0) == BOSUN_DISPLAY_OK);
        assert(writes - previous <= 8);
    }
    assert(writes == 100 + 240); logo_frame_is(builtin);
    assert(bosun_store_remove(BOSUN_BOOT_LOGO_PATH) == BOSUN_STORE_OK);
    boot("{}");
    assert(frame(0) == BOSUN_DISPLAY_OK); logo_frame_is(builtin);
    assert(bosun_store_write_atomic(BOSUN_BOOT_LOGO_PATH, custom_bytes, sizeof custom_bytes) == BOSUN_STORE_OK);
    assert(!bosun_store_mount("/no-such-display-root")); /* Unavailable storage. */
    boot("{}");
    assert(frame(0) == BOSUN_DISPLAY_OK); logo_frame_is(builtin);
}

static uint16_t half_image[BOSUN_BOOT_LOGO_HEIGHT / 2][120];
static unsigned channel_mean(unsigned a, unsigned b, unsigned c, unsigned d) { return (a + b + c + d + 2) / 4; }
static void make_half(const uint16_t (*image)[240]) {
    for (unsigned y = 0; y < BOSUN_BOOT_LOGO_HEIGHT / 2; ++y)
        for (unsigned x = 0; x < 120; ++x) {
            uint16_t a = image[2 * y][2 * x], b = image[2 * y][2 * x + 1];
            uint16_t c = image[2 * y + 1][2 * x], d = image[2 * y + 1][2 * x + 1];
            half_image[y][x] = (uint16_t)(channel_mean(a >> 11, b >> 11, c >> 11, d >> 11) << 11 |
                channel_mean(a >> 5 & 63, b >> 5 & 63, c >> 5 & 63, d >> 5 & 63) << 5 |
                channel_mean(a & 31, b & 31, c & 31, d & 31));
        }
}
/* Bounded work: at most eight rows and two rectangles per render. */
static void renders(unsigned count, uint32_t now) {
    while (count--) {
        uint32_t before = writes, filled = fills;
        (void)bosun_display_render(&display, &config, &kemper, hold_effect, now);
        assert(writes - before <= 8 && fills - filled <= 2);
    }
}
static bool black(void) {
    for (unsigned y = 0; y < 240; ++y) for (unsigned x = 0; x < 240; ++x) if (pixels[y][x]) return false;
    return true;
}
static void half_box_at(int left, int top) {
    assert(left >= 0 && left <= 120 && top >= 0 && top <= 140);
    for (int y = 0; y < 240; ++y)
        for (int x = 0; x < 240; ++x) {
            bool inside = x >= left && x < left + 120 && y >= top && y < top + 100;
            assert(pixels[y][x] == (inside ? half_image[y - top][x - left] : 0));
        }
}
static void full_box(const uint16_t (*image)[240]) {
    for (unsigned y = 0; y < 240; ++y)
        for (unsigned x = 0; x < 240; ++x)
            assert(pixels[y][x] == (y >= 20 && y < 220 ? image[y - 20][x] : 0));
}
static void screensaver_settings(void) {
    static const struct { const char *device; unsigned minutes, effect; } cases[] = {
        {"{}", 0, BOSUN_SCREENSAVER_MIX},
        {"{\"tft\":[]}", 0, BOSUN_SCREENSAVER_MIX},
        {"{\"tft\":{\"screensaver_minutes\":5,\"screensaver_effect\":\"bounce\"}}", 5, BOSUN_SCREENSAVER_BOUNCE},
        {"{\"tft\":{\"screensaver_minutes\":120,\"screensaver_effect\":\"breathe\"}}", 120, BOSUN_SCREENSAVER_BREATHE},
        {"{\"tft\":{\"screensaver_minutes\":121,\"screensaver_effect\":\"drift\"}}", 120, BOSUN_SCREENSAVER_DRIFT},
        {"{\"tft\":{\"screensaver_minutes\":100000,\"screensaver_effect\":\"mix\"}}", 120, BOSUN_SCREENSAVER_MIX},
        {"{\"tft\":{\"screensaver_minutes\":-1,\"screensaver_effect\":\"Bounce\"}}", 0, BOSUN_SCREENSAVER_MIX},
        {"{\"tft\":{\"screensaver_minutes\":1.5,\"screensaver_effect\":\"off\"}}", 0, BOSUN_SCREENSAVER_MIX},
        {"{\"tft\":{\"screensaver_minutes\":\"5\",\"screensaver_effect\":1}}", 0, BOSUN_SCREENSAVER_MIX},
        {"{\"tft\":{\"screensaver_minutes\":true,\"screensaver_effect\":null}}", 0, BOSUN_SCREENSAVER_MIX},
        {"{\"tft\":{\"screensaver_minutes\":0,\"screensaver_effect\":\"\\u0062ounce\"}}", 0, BOSUN_SCREENSAVER_BOUNCE},
    };
    for (unsigned i = 0; i < sizeof cases / sizeof *cases; ++i) {
        load(cases[i].device, "{}");
        assert(bosun_screensaver_minutes(&config) == cases[i].minutes);
        assert(bosun_screensaver_effect(&config) == cases[i].effect);
    }
    load(bosun_default_device, "{}");
    assert(!bosun_screensaver_minutes(&config) && bosun_screensaver_effect(&config) == BOSUN_SCREENSAVER_MIX);
    assert(strstr(bosun_default_device, "\"screensaver_minutes\":0,\"screensaver_effect\":\"mix\""));
    static const char *const names[] = {"off", "bounce", "breathe", "drift", "mix", "off"};
    for (unsigned effect = 0; effect < 6; ++effect) assert(!strcmp(bosun_screensaver_name(effect), names[effect]));
}
static void screensaver_effects(void) {
    boot("{\"tft\":{\"boot_logo_seconds\":0,\"brightness\":80}}");
    assert(frame(0) == BOSUN_DISPLAY_OK); layout_is("Clean");
    assert(!bosun_display_logo_active(&display));
    half_rows = true; make_half(builtin);
    /* Bounce: the panel is cleared, then the half-size logo drawn. */
    uint32_t t = 1000;
    rotation = 0; brightness = 0;
    bosun_display_screensaver(&display, BOSUN_SCREENSAVER_BOUNCE);
    renders(30, t);
    assert(black() && rotation == 180 && brightness == 204);
    renders(26, t); /* The first render finds no custom logo. */
    half_box_at(display.saver_x, display.saver_y);
    uint32_t before = writes;
    renders(5, t + 39); assert(writes == before);
    int x = display.saver_x, y = display.saver_y, dx = display.saver_dx, dy = display.saver_dy;
    bool turned_x = false, turned_y = false;
    for (unsigned step = 0; step < 160; ++step) {
        t += 40;
        uint32_t filled = fills;
        renders(1, t); assert(fills - filled == 2); /* Only the two exposed strips. */
        renders(25, t);
        assert(abs(display.saver_x - x) == 2 && abs(display.saver_y - y) == 2);
        turned_x |= display.saver_dx != dx; turned_y |= display.saver_dy != dy;
        x = display.saver_x; y = display.saver_y; dx = display.saver_dx; dy = display.saver_dy;
        half_box_at(x, y);
    }
    assert(turned_x && turned_y && brightness == 204);
    /* Breathe: a change of effect clears and draws the full logo box, centered. */
    bosun_display_screensaver(&display, BOSUN_SCREENSAVER_BREATHE);
    renders(30, t); assert(black());
    renders(26, t); full_box(builtin);
    assert(brightness == 204);
    unsigned lowest = 255, previous = brightness;
    before = writes;
    for (uint32_t elapsed = 0; elapsed <= 12000; elapsed += 20) {
        renders(1, t + elapsed);
        assert(brightness >= 26 && brightness <= 204);
        assert(abs((int)brightness - (int)previous) <= 4); /* Smooth. */
        if (brightness < lowest) lowest = brightness;
        previous = brightness;
    }
    assert(writes == before && lowest == 26); /* Never off; no redraw. */
    renders(1, t + 3000); assert(brightness == 26);
    renders(1, t + 6000); assert(brightness == 204);
    /* Drift: fades in from dark, holds, fades out and moves. */
    t += 20000;
    bosun_display_screensaver(&display, BOSUN_SCREENSAVER_DRIFT);
    renders(30, t); assert(black() && brightness == 0);
    renders(26, t); half_box_at(display.saver_x, display.saver_y);
    bool moved = false;
    for (unsigned cycle = 0; cycle < 3; ++cycle) {
        int left = display.saver_x, top = display.saver_y;
        previous = 0;
        for (uint32_t elapsed = 0; elapsed <= 1200; elapsed += 20) {
            renders(1, t + elapsed); assert(brightness >= previous); previous = brightness;
        }
        assert(brightness == 204);
        renders(1, t + 5200); assert(brightness == 204);
        renders(1, t + 5800); assert(brightness > 0 && brightness < 204);
        previous = brightness;
        for (uint32_t elapsed = 5820; elapsed < 6400; elapsed += 20) {
            renders(1, t + elapsed); assert(brightness <= previous); previous = brightness;
        }
        t += 6400;
        renders(1, t); assert(brightness == 0);
        renders(30, t); assert(black() && brightness == 0);
        renders(25, t); half_box_at(display.saver_x, display.saver_y);
        moved |= display.saver_x != left || display.saver_y != top;
    }
    assert(moved);
    /* Leaving dark Drift for Bounce clears without lighting the old frame. */
    bosun_display_screensaver(&display, BOSUN_SCREENSAVER_BOUNCE);
    renders(29, t); assert(brightness == 0);
    renders(1, t); assert(black() && brightness == 204);
    renders(26, t); half_box_at(display.saver_x, display.saver_y);
    /* Off: the layout returns with the configured backlight. */
    bosun_display_screensaver(&display, BOSUN_SCREENSAVER_OFF);
    brightness = 0; rotation = 0;
    assert(frame(t) == BOSUN_DISPLAY_OK); layout_is("Clean");
    assert(brightness == 204 && rotation == 180);
    before = writes; renders(5, t + 1000); assert(writes == before);
    /* The Updating frame and a logo preview take over and end it. */
    bosun_display_screensaver(&display, BOSUN_SCREENSAVER_BREATHE);
    renders(70, t);
    bosun_display_updating(&display, true);
    assert(!display.saver && bosun_display_logo_active(&display));
    assert(frame(t) == BOSUN_DISPLAY_OK); logo_frame_with(builtin, "Updating");
    bosun_display_updating(&display, false);
    assert(frame(t) == BOSUN_DISPLAY_OK); layout_is("Clean");
    bosun_display_screensaver(&display, BOSUN_SCREENSAVER_DRIFT);
    renders(40, t);
    bosun_display_logo(&display, 3000);
    assert(!display.saver);
    assert(frame(t) == BOSUN_DISPLAY_OK); logo_frame_is(builtin);
    holds_until(t, t + 3000); layout_is("Clean");
    assert(brightness == 204);
    /* Requested while a logo shows: it starts from a cleared panel afterwards. */
    boot("{}");
    renders(2, 0);
    bosun_display_screensaver(&display, BOSUN_SCREENSAVER_BOUNCE);
    renders(28, 0); logo_frame_is(builtin);
    renders(10, 1000); logo_frame_is(builtin);
    renders(30, 2100); assert(black());
    renders(26, 2100); half_box_at(display.saver_x, display.saver_y);
    /* A failed row keeps its place for the retry. */
    bosun_display_screensaver(&display, BOSUN_SCREENSAVER_OFF);
    bosun_display_screensaver(&display, BOSUN_SCREENSAVER_BOUNCE);
    renders(30, 3000); renders(3, 3000);
    fail_write = true;
    assert(bosun_display_render(&display, &config, &kemper, hold_effect, 3000) & BOSUN_DISPLAY_IO);
    assert(display.row == 8);
    fail_write = false;
    renders(23, 3000); half_box_at(display.saver_x, display.saver_y);
    half_rows = false;
}
static void screensaver_custom_logo(void) {
    make_custom();
    assert(bosun_store_mkdir(BOSUN_BOOT_LOGO_DIR) == BOSUN_STORE_OK);
    assert(bosun_store_write_atomic(BOSUN_BOOT_LOGO_PATH, custom_bytes, sizeof custom_bytes) == BOSUN_STORE_OK);
    boot("{\"tft\":{\"boot_logo_seconds\":0}}");
    assert(frame(0) == BOSUN_DISPLAY_OK); layout_is("Clean");
    half_rows = true; make_half(custom);
    bosun_display_screensaver(&display, BOSUN_SCREENSAVER_DRIFT);
    renders(55, 0); half_box_at(display.saver_x, display.saver_y);
    bosun_display_screensaver(&display, BOSUN_SCREENSAVER_BREATHE);
    renders(55, 0); full_box(custom);
    /* A short file fails part way: the box is redrawn with the built-in logo. */
    assert(bosun_store_write_atomic(BOSUN_BOOT_LOGO_PATH, custom_bytes, sizeof custom_bytes / 2) == BOSUN_STORE_OK);
    bosun_display_screensaver(&display, BOSUN_SCREENSAVER_BOUNCE);
    make_half(builtin);
    renders(100, 0); half_box_at(display.saver_x, display.saver_y);
    bosun_display_screensaver(&display, BOSUN_SCREENSAVER_BREATHE);
    renders(100, 0); full_box(builtin);
    assert(bosun_store_remove(BOSUN_BOOT_LOGO_PATH) == BOSUN_STORE_OK);
    half_rows = false;
}

int main(void) {
    char root[] = "/tmp/bosun-display-XXXXXX";
    assert(mkdtemp(root) && bosun_store_mount(root));
    decode_builtin();
    morph_text_distinguishes_command_from_unknown();
    assert(sizeof(bosun_display_t) < 4096);
    geometry_and_colors();
    expression_labels_are_text_only();
    fields_and_clipping();
    scroll_snapshot_and_tuner();
    failed_transfer_is_visible();
    slow_scroll_long_labels_do_not_overflow();
    momentary_hold_repaints_without_configuration_change();
    startup_logo_timing();
    startup_logo_skip_and_preview();
    updating_screen();
    screensaver_settings();
    screensaver_effects();
    screensaver_custom_logo();
    custom_logo_rows_and_fallback();
    assert(bosun_store_mount(root) && bosun_store_format() == BOSUN_STORE_OK && rmdir(root) == 0);
    printf("display snapshots, geometry, color, expression text, UTF-8, scrolling, tuner, startup logo "
           "(built-in %u runs, custom rows, fallback, hold/skip/preview), Updating screen, screensaver "
           "(bounce, breathe, drift, custom logo): PASS (%zu bytes)\n",
           (unsigned)BOOT_LOGO_RUNS, sizeof display);
    return 0;
}
