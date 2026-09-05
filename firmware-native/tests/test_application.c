#define _POSIX_C_SOURCE 200809L
#include "bosun/application.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static bosun_application_t app;
static char root[] = "/tmp/bosun-application-XXXXXX";
static uint32_t now, feeds, tasks, writes, drops, rows;
static uint32_t data_stall_ms;
static bool cdc, midi_up[2], led_busy, overrun_on_read;
static size_t midi_limit[2], data_limit;
static uint8_t input[4096], output[32768], midi_output[2][32768], midi_input[2][512];
static size_t input_length, output_length, midi_length[2], midi_input_length[2];
static uint32_t leds[BOSUN_LED_COUNT];
static uint16_t adc[2];
static bool adc_present[2], probe_driven[2], probe_high[2];
static unsigned probe_reads[2], probe_charges[2];

bool bosun_board_init(const bosun_board_config_t *config) { assert(!config); return true; }
void bosun_board_task(void) { ++tasks; }
uint32_t bosun_board_millis(void) { return now; }
bool bosun_board_usb_connected(void) { return cdc; }
bool bosun_board_midi_connected(bosun_midi_port_t port) { return midi_up[port]; }
size_t bosun_board_data_read(uint8_t *data, size_t capacity) {
    now += data_stall_ms; data_stall_ms = 0;
    size_t length = input_length < capacity ? input_length : capacity;
    memcpy(data, input, length); memmove(input, input + length, input_length - length);
    input_length -= length; return length;
}
size_t bosun_board_data_write(const uint8_t *data, size_t length) {
    if (length > data_limit) length = data_limit;
    assert(output_length + length < sizeof output);
    memcpy(output + output_length, data, length); output_length += length; output[output_length] = 0;
    return length;
}
size_t bosun_board_console_write(const uint8_t *data, size_t length) { (void)data; return length; }
size_t bosun_board_midi_read(bosun_midi_port_t port, uint8_t *data, size_t capacity) {
    size_t length = midi_input_length[port];
    assert(length <= capacity); memcpy(data, midi_input[port], length); midi_input_length[port] = 0;
    if (port == BOSUN_MIDI_DIN && overrun_on_read) { ++drops; overrun_on_read = false; }
    return length;
}
size_t bosun_board_midi_write(bosun_midi_port_t port, const uint8_t *data, size_t length) {
    if (length > midi_limit[port]) length = midi_limit[port];
    assert(midi_length[port] + length <= sizeof midi_output[port]);
    memcpy(midi_output[port] + midi_length[port], data, length); midi_length[port] += length;
    ++writes; return length;
}
uint32_t bosun_board_midi_rx_dropped(void) { return drops; }
uint16_t bosun_board_switches(void) { return 0; }
uint16_t bosun_board_expression_read(uint8_t jack) {
    assert(jack >= 1 && jack <= 2 && !probe_driven[jack - 1]);
    if (bosun_expression_presence_busy(&app.expression_presence, jack)) {
        assert(app.expression_presence.phase == BOSUN_PRESENCE_SETTLE_HIGH ||
               app.expression_presence.phase == BOSUN_PRESENCE_SETTLE_LOW);
        assert((int32_t)(now - app.expression_presence.deadline_ms) >= 0);
        ++probe_reads[jack - 1];
        if (!adc_present[jack - 1]) return probe_high[jack - 1] ? 65535 : 0;
    }
    return adc[jack - 1];
}
bool bosun_board_expression_charge(uint8_t jack, bool high) {
    assert(jack >= 1 && jack <= 2 && !probe_driven[jack - 1]);
    probe_driven[jack - 1] = true; probe_high[jack - 1] = high;
    ++probe_charges[jack - 1];
    return true;
}
bool bosun_board_expression_release(uint8_t jack) {
    assert(jack >= 1 && jack <= 2);
    probe_driven[jack - 1] = false;
    return true;
}
void bosun_board_leds_set(uint8_t index, uint32_t rgb) { leds[index] = rgb; }
bool bosun_board_leds_show(void) { return !led_busy; }
bool bosun_board_display_rotation(uint16_t degrees) { return degrees <= 270 && degrees % 90 == 0; }
void bosun_board_display_brightness(uint8_t value) { (void)value; }
bool bosun_board_display_blit_rgb565(int16_t x, int16_t y, uint16_t width, uint16_t height,
                                   const uint16_t *pixels, uint16_t stride) {
    assert(x == 0 && y >= 0 && y < 240 && width == 240 && height == 1 && stride == 240 && pixels);
    ++rows; return true;
}
bool bosun_board_watchdog_enable(uint32_t timeout) { assert(timeout == 8000); return true; }
void bosun_board_watchdog_feed(void) { ++feeds; }
void bosun_board_reboot(bool bootloader) { (void)bootloader; assert(!"Unexpected reboot"); }

static void tick(void) {
    uint32_t before = rows; ++now; bosun_application_tick(&app);
    assert(rows - before <= 8 && feeds == app.ticks && tasks == app.ticks);
}
static void fixture(void) {
    assert(bosun_store_mount(root));
    /* Only tests explicitly clear their own mkdtemp directory. Application
     * initialization never has a formatting path. */
    assert(bosun_store_format() == BOSUN_STORE_OK);
    static const char marker[] = "pre-existing storage survives boot";
    assert(bosun_store_write_atomic("/preserve", marker, sizeof marker) == BOSUN_STORE_OK);
    now = feeds = tasks = writes = drops = rows = data_stall_ms = 0;
    cdc = led_busy = overrun_on_read = false;
    midi_up[0] = midi_up[1] = true;
    midi_limit[0] = midi_limit[1] = 256; data_limit = 256;
    input_length = output_length = midi_length[0] = midi_length[1] = 0;
    midi_input_length[0] = midi_input_length[1] = 0;
    adc[0] = adc[1] = 0;
    memset(adc_present, 0, sizeof adc_present); memset(probe_driven, 0, sizeof probe_driven);
    memset(probe_high, 0, sizeof probe_high); memset(probe_reads, 0, sizeof probe_reads);
    memset(probe_charges, 0, sizeof probe_charges);
    memset(leds, 0, sizeof leds);
    assert(bosun_application_init(&app, root));
    assert(!writes && !app.runtime.midi_tx_count && !app.runtime.queue_count);
    char preserved[64]; size_t length;
    assert(bosun_store_read("/preserve", preserved, sizeof preserved, &length) == BOSUN_STORE_OK);
    assert(length == sizeof marker && !memcmp(preserved, marker, length));
    bosun_dirent_t entries[4]; size_t count;
    assert(bosun_store_list("/", entries, 4, &count) == BOSUN_STORE_OK && count == 1);
}

static void test_midi_backpressure(void) {
    fixture();
    uint8_t message[1026];
    for (size_t i = 0; i < sizeof message; ++i) message[i] = (uint8_t)(i & 127);
    message[0] = 0xf0; message[sizeof message - 1] = 0xf7;
    midi_limit[0] = 1; midi_limit[1] = 7;
    assert(bosun_application_send_midi(&app, message, sizeof message));
    assert(!writes); /* Admission itself must never transmit a prefix. */
    for (unsigned i = 0; i < 600; ++i) tick();
    assert(midi_length[0] == sizeof message && midi_length[1] == sizeof message);
    assert(!memcmp(message, midi_output[0], sizeof message) && !memcmp(message, midi_output[1], sizeof message));
    /* Cross the ring boundary with different drain rates, then force a full
     * USB queue while DIN is empty. Rejection must admit nothing to DIN. */
    midi_limit[0] = 0; midi_limit[1] = 256;
    assert(bosun_application_send_midi(&app, message, sizeof message));
    for (unsigned i = 0; i < 10; ++i) tick();
    assert(app.midi[0].count == sizeof message && app.midi[1].count == 0);
    assert(!bosun_application_send_midi(&app, message, sizeof message));
    assert(app.midi_rejected == 1 && app.midi[1].count == 0 && app.midi[0].count == sizeof message);
    midi_limit[0] = 13;
    for (unsigned i = 0; i < 50; ++i) tick();
    assert(midi_length[0] == 2 * sizeof message && midi_length[1] == 2 * sizeof message);
    assert(!memcmp(midi_output[0] + sizeof message, message, sizeof message));
    assert(!memcmp(midi_output[1] + sizeof message, message, sizeof message));
    /* CDC DTR is false throughout: USB MIDI still works. A physical USB
     * disconnect discards that stream's pending tail and never blocks DIN. */
    midi_limit[0] = 1;
    assert(bosun_application_send_midi(&app, message, sizeof message)); tick();
    size_t sent = midi_length[0], remaining = app.midi[0].count;
    midi_up[0] = false; tick();
    assert(!app.midi[0].count && app.midi_abandoned == remaining);
    for (unsigned i = 0; i < 10; ++i) tick();
    const uint8_t cc[] = {0xb0, 7, 127};
    assert(bosun_application_send_midi(&app, cc, sizeof cc)); tick();
    assert(midi_length[0] == sent);
    midi_up[0] = true; tick();
    assert(bosun_application_send_midi(&app, cc, sizeof cc)); tick(); tick();
    assert(midi_length[0] == sent + sizeof cc && !memcmp(midi_output[0] + sent, cc, sizeof cc));
}

static void test_cdc_and_overruns(void) {
    fixture(); cdc = true; data_limit = 3;
    static const char requests[] = "{\"type\":\"PING\",\"id\":\"one\"}\n{\"type\":\"PING\",\"id\":\"two\"}\n{\"type\":\"PING\",\"id\":\"three\"}\n";
    memcpy(input, requests, sizeof requests - 1); input_length = sizeof requests - 1;
    tick(); assert(app.input_length > 0);
    for (unsigned i = 0; i < 500; ++i) tick();
    const char *one = strstr((char *)output, "\"id\":\"one\"");
    const char *two = strstr((char *)output, "\"id\":\"two\"");
    const char *three = strstr((char *)output, "\"id\":\"three\"");
    assert(one && two && three && one < two && two < three);
    assert(!strstr(one + 1, "\"id\":\"one\"") && !app.input_length && !input_length);
    data_limit = 0; memcpy(input, requests, sizeof requests - 1); input_length = sizeof requests - 1;
    tick(); assert(app.input_length);
    cdc = false; tick(); assert(!app.input_length && !app.protocol.rx_length && !app.protocol.tx_length);
    /* Overrun is detected inside board_midi_read, after the initial counter
     * check. An incomplete old CC must not consume a new data-only byte. */
    midi_input[1][0] = 0xb0; midi_input[1][1] = 7; midi_input_length[1] = 2; tick();
    assert(!app.runtime.midi_rx_count);
    overrun_on_read = true; midi_input[1][0] = 127; midi_input_length[1] = 1; tick();
    assert(!app.runtime.midi_rx_count && app.din_dropped == 1);
    const uint8_t complete[] = {0xb0, 8, 64}; memcpy(midi_input[1], complete, sizeof complete);
    midi_input_length[1] = sizeof complete; tick(); assert(app.runtime.midi_rx_count == 1);
}

static void test_leds_and_expression(void) {
    fixture();
    assert(bosun_config_create("test", "Test", "generic", NULL) == BOSUN_STORE_OK);
    assert(bosun_config_activate(&app.config, "test", false) == BOSUN_STORE_OK);
    const char *patch = "{\"name\":\"Test\",\"bindings\":[{\"switch\":\"1\",\"mode\":\"latched\",\"led\":{\"on\":\"#ff0080\"}}]}";
    assert(bosun_config_put_patch(&app.config, NULL, 1, 1, patch, strlen(patch), now) == BOSUN_STORE_OK);
    assert(bosun_config_put_patch(&app.config, NULL, 1, 2, "{}", 2, now) == BOSUN_STORE_OK);
    assert(bosun_config_select(&app.config, 1, 1) == BOSUN_STORE_OK);
    const char *device = "{\"leds\":{\"brightness\":64,\"dim\":4},\"preset_navigation\":{\"switches\":{\"A\":1,\"B\":2,\"C\":3},\"bank_colors\":{\"1\":\"#0080ff\"}},\"expression\":[{\"jack\":1,\"enabled\":true,\"calibration\":{\"min\":0,\"max\":65535},\"message\":{\"type\":\"cc\",\"channel\":1,\"cc\":11}}]}";
    assert(bosun_config_put_device(&app.config, NULL, device, strlen(device)) == BOSUN_STORE_OK);
    led_busy = true; tick(); assert(app.leds_dirty);
    assert(app.leds[0] == 0x010001 && app.leds[15] == 0x002040 && app.leds[18] == 0x000101 && !app.leds[21]);
    for (unsigned sw = 0; sw < BOSUN_SWITCH_COUNT; ++sw)
        assert(app.leds[sw * 3] == app.leds[sw * 3 + 1] && app.leds[sw * 3] == app.leds[sw * 3 + 2]);
    led_busy = false; tick(); assert(!app.leds_dirty && !memcmp(leds, app.leds, sizeof leds));
    app.runtime.switches[0].latched_on = true;
    for (unsigned i = 0; i < 20; ++i) tick();
    assert(app.leds[0] == 0x400020);
    /* The floating jack retains the probe's rails. Ordinary ADC jitter or
     * motion must remain silent when presence was never confirmed. */
    adc[0] = 65535;
    for (unsigned i = 0; i < 300; ++i) tick();
    assert(app.runtime.expression[0].raw == 65535 && !app.runtime.expression[0].present);
    assert(!midi_length[0] && !midi_length[1]);
}

static void test_expression_presence(void) {
    fixture();
    assert(bosun_config_create("test", "Test", "generic", NULL) == BOSUN_STORE_OK);
    assert(bosun_config_activate(&app.config, "test", false) == BOSUN_STORE_OK);
    const char device[] = "{\"expression\":[{\"jack\":1,\"enabled\":true,\"calibration\":{\"min\":0,\"max\":65535},\"message\":{\"type\":\"cc\",\"cc\":11}},{\"jack\":2,\"enabled\":true,\"calibration\":{\"min\":0,\"max\":65535},\"message\":{\"type\":\"cc\",\"cc\":7}}]}";
    assert(bosun_config_put_device(&app.config, NULL, device, strlen(device)) == BOSUN_STORE_OK);
    adc[0] = 50000; adc[1] = 25000; adc_present[0] = true;
    tick();
    assert(probe_driven[0] && app.runtime.expression[0].raw == 50000 && app.runtime.expression[0].baseline == 97);
    assert(!app.runtime.expression[0].present && !app.runtime.expression[0].armed);
    while (now < 25) {
        tick();
        assert(app.runtime.expression[0].raw == 50000 && !app.runtime.expression[0].armed);
    }
    assert(app.runtime.expression[0].present && probe_reads[0] == 2 && !probe_reads[1]);
    assert(!midi_length[0] && !midi_length[1]);
    adc[0] = 0;
    while (now < 200) tick();
    assert(app.runtime.expression[0].armed && midi_length[0] > 0 && midi_length[0] == midi_length[1]);
    for (size_t i = 0; i < midi_length[0]; i += 3)
        assert(midi_output[0][i] == 0xb0 && midi_output[0][i + 1] == 11);
    /* Round robin probes both jacks; a confirmed pedal survives two absent
     * observations and is muted on the third, while MIDI/CDC keep ticking. */
    adc_present[0] = false;
    while (now < 3025) tick();
    assert(app.runtime.expression[0].present && app.expression_presence.absent_streak[0] == 1);
    assert(!app.runtime.expression[1].present && probe_reads[1] == 2);
    while (now < 6025) tick();
    assert(app.runtime.expression[0].present && app.expression_presence.absent_streak[0] == 2);
    while (now < 9025) tick();
    assert(!app.runtime.expression[0].present && app.expression_presence.absent_streak[0] == 3);
    size_t sent = midi_length[0]; adc[0] = 65535;
    while (now < 10000) tick();
    assert(midi_length[0] == sent && app.runtime.expression[0].raw == 65535);
    adc_present[0] = true;
    while (now < 12025) tick();
    assert(app.runtime.expression[0].present && app.expression_presence.absent_streak[0] == 0);
    /* Disabling a charged jack releases immediately and retains its last
     * normal sample until recovery settling has finished. */
    while (now < 13501) tick();
    assert(probe_driven[1]);
    assert(bosun_config_put_device(&app.config, NULL, "{}", 2) == BOSUN_STORE_OK);
    tick(); assert(!probe_driven[1] && !app.runtime.expression[1].enabled);
    assert(bosun_expression_presence_busy(&app.expression_presence, 2));
    while (now < 13512) tick();
    assert(!bosun_expression_presence_busy(&app.expression_presence, 2));
    unsigned charges = probe_charges[0] + probe_charges[1];
    while (now < 15100) tick();
    assert(probe_charges[0] + probe_charges[1] == charges);
    /* Slow protocol work must not consume the required physical charge time
     * before a GPIO is even driven. Use the current clock at phase entry. */
    fixture();
    assert(bosun_config_create("test", "Test", "generic", NULL) == BOSUN_STORE_OK);
    assert(bosun_config_activate(&app.config, "test", false) == BOSUN_STORE_OK);
    assert(bosun_config_put_device(&app.config, NULL, device, strlen(device)) == BOSUN_STORE_OK);
    cdc = true; data_stall_ms = 100;
    tick();
    assert(now == 101 && probe_driven[0] && app.expression_presence.deadline_ms == 103);
    tick(); assert(probe_driven[0]);
    tick(); assert(!probe_driven[0] && app.expression_presence.phase == BOSUN_PRESENCE_SETTLE_HIGH);
}

int main(void) {
    assert(mkdtemp(root));
    test_midi_backpressure(); test_cdc_and_overruns(); test_leds_and_expression(); test_expression_presence();
    assert(bosun_store_format() == BOSUN_STORE_OK && rmdir(root) == 0);
    puts("Application: non-destructive boot, bounded/atomic dual MIDI queues, partial CDC, session reset, DMA overrun, LED parity, absent expression, display and watchdog passed");
    return 0;
}
