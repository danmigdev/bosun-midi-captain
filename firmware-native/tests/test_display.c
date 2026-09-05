#include "bosun/display.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

static uint16_t pixels[240][240]; /* Host fake only: never linked into RP2040. */
static uint32_t writes;
static uint16_t rotation;
static uint8_t brightness;
static bool fail_write;
static bosun_config_t config;
static bosun_display_t display;
static bosun_kemper_state kemper;

bool bosun_board_display_rotation(uint16_t value) { rotation = value; return value <= 270 && value % 90 == 0; }
void bosun_board_display_brightness(uint8_t value) { brightness = value; }
bool bosun_board_display_blit_rgb565(int16_t x, int16_t y, uint16_t w, uint16_t h,
                                    const uint16_t *source, uint16_t stride) {
    assert(x >= 0 && y >= 0 && x + w <= 240 && y + h <= 240);
    assert(w == 240 && h == 1 && stride == 240); /* A single 480-byte stripe. */
    if (fail_write) return false;
    memcpy(&pixels[y][x], source, w * sizeof(*source));
    ++writes;
    return true;
}
static void load(const char *device, const char *patch) {
    memset(&config, 0, sizeof config);
    memset(&kemper, 0, sizeof kemper);
    memset(pixels, 0xa5, sizeof pixels);
    config.bank = 2; config.slot = 3;
    config.revision = 1; config.patch_revision = 1;
    assert(strlen(device) < sizeof config.device && strlen(patch) < sizeof config.patch);
    strcpy(config.device, device); strcpy(config.patch, patch);
    assert(bosun_json_parse(&config.device_doc, config.device, strlen(device), config.device_tokens, BOSUN_DEVICE_TOKENS) == BOSUN_JSON_OK);
    assert(bosun_json_parse(&config.patch_doc, config.patch, strlen(patch), config.patch_tokens, BOSUN_PATCH_TOKENS) == BOSUN_JSON_OK);
    bosun_display_init(&display);
    writes = 0; fail_write = false;
}
static unsigned frame(uint32_t now) {
    unsigned status = 0;
    uint32_t before = writes;
    for (unsigned i = 0; i < 30; ++i) {
        uint32_t previous = writes;
        status = bosun_display_render(&display, &config, &kemper, now);
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
    assert(bosun_display_render(&display, &config, &kemper, 1) == BOSUN_DISPLAY_OK);
    assert(writes == before); /* Unchanged frame has no display I/O. */
}
static void expression_badges_and_raw_mode(void) {
    const char *layout = "{\"tft\":{\"layout\":[{\"field\":\"expression_mode\","
        "\"x\":-6,\"y\":-6,\"halign\":\"right\",\"valign\":\"bottom\",\"size\":2,\"color\":\"#ffffff\"}]}}";
    load(layout, "{}");
    kemper.expression_mode = BOSUN_EXPRESSION_VOL;
    assert(frame(0) == 0);
    assert(display.labels[0].x == 162 && display.labels[0].y == 210);
    assert(pixels[222][164] == 0); /* Left treadle location has no VOL ramp. */
    assert(pixels[230][164] == 0xffff);
    kemper.expression_mode = BOSUN_EXPRESSION_WAH; ++kemper.revision;
    assert(frame(1) == 0);
    assert(pixels[222][164] == 0xffff); /* Tilted pedal, with horizontal base. */
    assert(pixels[228][164] == 0xffff);
    kemper.expression_mode = BOSUN_EXPRESSION_UNKNOWN; ++kemper.revision;
    assert(frame(2) == 0);
    for (unsigned y = 210; y < 234; ++y)
        for (unsigned x = 162; x < 194; ++x) assert(pixels[y][x] == 0);
    load("{\"tft\":{\"layout\":[{\"field\":\"expression_mode\",\"prefix\":\"WAH \",\"size\":1}]}}", "{}");
    kemper.expression_mode = BOSUN_EXPRESSION_VOL;
    assert(frame(0) == 0);
    assert(display.labels[0].icon == BOSUN_EXPRESSION_VOL);
}
static void fields_fonts_and_clipping(void) {
    load("{\"tft\":{\"layout\":[{\"field\":\"patch_name\",\"prefix\":\"RIG \",\"suffix\":\"!\",\"x\":-10},"
        "{\"field\":\"missing_field\",\"prefix\":\"ORPHAN \"},"
        "{\"text\":\"Caf\\u00e9\",\"font\":\"custom.bdf\",\"y\":20}]}}", "{\"name\":\"Crunch\"}");
    unsigned status = frame(0);
    assert(status == (BOSUN_DISPLAY_UNKNOWN_FIELD | BOSUN_DISPLAY_UNSUPPORTED_FONT));
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
    assert(bosun_display_render(&display, &config, &kemper, 0) & BOSUN_DISPLAY_IO);
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
int main(void) {
    assert(sizeof(bosun_display_t) < 4096);
    geometry_and_colors();
    expression_badges_and_raw_mode();
    fields_fonts_and_clipping();
    scroll_snapshot_and_tuner();
    failed_transfer_is_visible();
    slow_scroll_long_labels_do_not_overflow();
    printf("display snapshots, geometry, color, icons, UTF-8, scrolling, tuner: PASS (%zu bytes)\n", sizeof display);
    return 0;
}
