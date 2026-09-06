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
static unsigned reboots;
static bool reboot_bootloader, reboot_allowed;
static uint16_t switches;
static uint32_t usb_session_generation;
static bool observed_cdc;
static bosun_board_usb_diagnostics_t usb_diagnostics;
static bool capture_console;
static bool capture_usb_rx;
static bosun_board_usb_rx_diagnostics_t usb_rx_diagnostics;
static uint8_t console_output[1024];
static size_t console_length, console_limit;

bool bosun_board_init(const bosun_board_config_t *config) { assert(!config); return true; }
void bosun_board_task(void) {
    ++tasks;
    if (cdc != observed_cdc) { observed_cdc = cdc; ++usb_session_generation; }
}
uint32_t bosun_board_millis(void) { return now; }
bool bosun_board_usb_connected(void) { return cdc; }
uint32_t bosun_board_usb_session_generation(void) { return usb_session_generation; }
void bosun_board_usb_diagnostics(bosun_board_usb_diagnostics_t *result) {
    assert(result); *result = usb_diagnostics; result->generation = usb_session_generation;
}
bool bosun_board_usb_rx_diagnostics(bosun_board_usb_rx_diagnostics_t *result) {
    assert(result); *result = usb_rx_diagnostics; return capture_usb_rx;
}
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
size_t bosun_board_console_write(const uint8_t *data, size_t length) {
    if (!capture_console) return length;
    if (length > console_limit) length = console_limit;
    assert(console_length + length < sizeof console_output);
    memcpy(console_output + console_length, data, length); console_length += length;
    console_output[console_length] = 0;
    return length;
}
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
uint16_t bosun_board_switches(void) { return switches; }
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
uint32_t bosun_board_leds_get(uint8_t index) { return index < BOSUN_LED_COUNT ? leds[index] : 0; }
bool bosun_board_display_rotation(uint16_t degrees) { return degrees <= 270 && degrees % 90 == 0; }
void bosun_board_display_brightness(uint8_t value) { (void)value; }
bool bosun_board_display_blit_rgb565(int16_t x, int16_t y, uint16_t width, uint16_t height,
                                   const uint16_t *pixels, uint16_t stride) {
    assert(x == 0 && y >= 0 && y < 240 && width == 240 && height == 1 && stride == 240 && pixels);
    ++rows; return true;
}
bool bosun_board_watchdog_enable(uint32_t timeout) { assert(timeout == 8000); return true; }
void bosun_board_watchdog_feed(void) { ++feeds; }
void bosun_board_reboot(bool bootloader) {
    assert(reboot_allowed); ++reboots; reboot_bootloader = bootloader;
}

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
    reboots = 0; reboot_bootloader = reboot_allowed = false;
    switches = 0;
    usb_session_generation = 0; observed_cdc = false;
    usb_diagnostics = (bosun_board_usb_diagnostics_t){.rx_fnv1a = UINT32_C(2166136261), .tx_fnv1a = UINT32_C(2166136261)};
    capture_console = false; console_length = 0; console_limit = 17;
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

static void queue_command(const char *command) {
    input_length = strlen(command); assert(input_length < sizeof input);
    memcpy(input, command, input_length);
}

static void test_reboot_modes(void) {
    static const char *const commands[] = {
        "{\"type\":\"REBOOT\",\"id\":\"restart\"}\n",
        "{\"type\":\"REBOOT\",\"id\":\"restart\",\"mode\":\"normal\"}\n",
        "{\"type\":\"REBOOT\",\"id\":\"restart\",\"mode\":\"bootloader\"}\n"
    };
    for (unsigned i = 0; i < sizeof commands / sizeof *commands; ++i) {
        fixture(); cdc = reboot_allowed = true; data_limit = 1; queue_command(commands[i]);
        tick(); assert(app.reboot_pending && !reboots);
        while ((uint32_t)(now - app.reboot_ms) < 99) { tick(); assert(!reboots); }
        assert(!strcmp((char *)output, "{\"type\":\"ACK\",\"id\":\"restart\"}\n"));
        tick(); assert(reboots == 1 && reboot_bootloader == (i == 2));
    }
    fixture(); cdc = true;
    queue_command("{\"type\":\"REBOOT\",\"mode\":true}\n");
    tick();
    assert(strstr((char *)output, "\"type\":\"ERROR\"") && !app.reboot_pending);
    for (unsigned i = 0; i < 1100; ++i) tick();
    assert(!reboots);
}

static void test_unobserved_cdc_edges(void) {
    fixture(); cdc = true; data_limit = 0;
    queue_command("{\"type\":\"PING\",\"id\":\"old\"}\n{\"type\":\"REBOOT\"}\n");
    tick(); assert(app.input_length && app.protocol.tx_length);
    /* Both DTR edges were dispatched before this application tick. The
     * previously buffered REBOOT must never execute in the new session. */
    usb_session_generation += 2;
    data_limit = 256; queue_command("{\"type\":\"PING\",\"id\":\"new\"}\n");
    tick();
    assert(!app.input_length && !app.reboot_pending && !reboots);
    assert(strstr((char *)output, "\"id\":\"new\"") && !strstr((char *)output, "\"id\":\"old\""));
    /* Drop an incomplete parser prefix as well, including uint32 wrap. */
    queue_command("{\"type\":\"REBOOT\",\"id\":"); tick();
    assert(app.protocol.rx_length);
    usb_session_generation = UINT32_MAX; tick();
    assert(!app.protocol.rx_length && !app.reboot_pending);
    usb_session_generation = 0; queue_command("{\"type\":\"PING\",\"id\":\"wrapped\"}\n"); tick();
    assert(!app.protocol.rx_length && !app.reboot_pending && strstr((char *)output, "\"id\":\"wrapped\""));

    /* An already accepted reboot has a separate application latch: a rapid
     * disconnect/reconnect cannot cancel it or replace its bootloader mode. */
    fixture(); cdc = reboot_allowed = true; data_limit = 0;
    queue_command("{\"type\":\"REBOOT\",\"mode\":\"bootloader\"}\n");
    tick(); assert(app.reboot_pending);
    usb_session_generation += 2;
    queue_command("{\"type\":\"REBOOT\",\"mode\":\"normal\"}\n");
    tick(); assert(app.reboot_pending && !app.protocol.reboot_requested && input_length);
    while ((uint32_t)(now - app.reboot_ms) < 99) { tick(); assert(!reboots); }
    tick(); assert(reboots == 1 && reboot_bootloader && input_length);
}

static void test_reboot_backpressure(void) {
    fixture(); cdc = reboot_allowed = true; data_limit = 0;
    midi_limit[0] = midi_limit[1] = 0;
    uint8_t message[BOSUN_APPLICATION_MIDI_BYTES]; memset(message, 0xf8, sizeof message);
    assert(bosun_application_send_midi(&app, message, sizeof message));
    assert(!bosun_application_send_midi(&app, message, 1));
    queue_command("{\"type\":\"REBOOT\",\"mode\":\"bootloader\"}\n");
    tick(); assert(app.reboot_pending && app.protocol.tx_length && !reboots);
    while ((uint32_t)(now - app.reboot_ms) < 999) { tick(); assert(!reboots); }
    tick();
    assert(reboots == 1 && reboot_bootloader && !output_length && app.protocol.tx_length);
    assert(app.midi[0].count == sizeof message && app.midi[1].count == sizeof message);

    /* A disconnected client must not cancel an accepted maintenance request;
     * a new session must not replace it with an ordinary reboot. */
    fixture(); cdc = reboot_allowed = true; data_limit = 0; now = UINT32_MAX - 50u;
    queue_command("{\"type\":\"REBOOT\",\"mode\":\"bootloader\"}\n");
    tick(); assert(app.reboot_pending && !reboots);
    cdc = false; tick(); assert(app.reboot_pending && !app.protocol.reboot_requested);
    cdc = true; queue_command("{\"type\":\"REBOOT\",\"mode\":\"normal\"}\n");
    while ((uint32_t)(now - app.reboot_ms) < 99) { tick(); assert(!reboots); }
    tick(); assert(reboots == 1 && reboot_bootloader && input_length);
}

static void test_reboot_unavailable_storage(void) {
    fixture();
    assert(bosun_application_init(&app, "/no-such-native-storage-root"));
    assert(!bosun_store_ready() && app.boot_result == BOSUN_STORE_UNAVAILABLE);
    cdc = reboot_allowed = true; queue_command("{\"type\":\"REBOOT\",\"mode\":\"bootloader\"}\n");
    tick();
    assert(app.reboot_pending && strstr((char *)output, "\"type\":\"ACK\""));
    while ((uint32_t)(now - app.reboot_ms) < 99) { tick(); assert(!reboots); }
    tick(); assert(reboots == 1 && reboot_bootloader && !bosun_store_ready());
    assert(bosun_store_mount(root));
    char preserved[64]; size_t length;
    assert(bosun_store_read("/preserve", preserved, sizeof preserved, &length) == BOSUN_STORE_OK);
    assert(!strcmp(preserved, "pre-existing storage survives boot"));
}

static void test_hold_label_matches_context(void) {
    fixture();
    assert(bosun_config_create("test", "Test", "generic", NULL) == BOSUN_STORE_OK);
    assert(bosun_config_activate(&app.config, "test", false) == BOSUN_STORE_OK);
    const char patch[] = "{\"bindings\":[{\"switch\":\"up\",\"mode\":\"latched\",\"label\":\"BOOST\","
        "\"led\":{\"on\":\"#ff0000\",\"off\":\"#000000\"}}]}";
    const char device[] = "{\"auto_momentary_on_hold\":true,\"auto_momentary_ms\":500,\"leds\":{\"brightness\":64,\"dim\":4},"
        "\"tft\":{\"layout\":[{\"field\":\"hold_effect\",\"x\":0,\"y\":180,\"size\":5,\"font\":\"system\"}]}}";
    assert(bosun_config_put_patch(&app.config, NULL, 1, 1, patch, sizeof patch - 1, 0) == BOSUN_STORE_OK);
    assert(bosun_config_select(&app.config, 1, 1) == BOSUN_STORE_OK);
    assert(bosun_config_put_device(&app.config, NULL, device, sizeof device - 1) == BOSUN_STORE_OK);
    switches = 1u << 4; /* Physical up: the actual profile's BOOST binding. */
    while (now < 500) tick();
    assert(!app.runtime.held_mask && !app.display.labels[0].length);
    uint32_t revision = app.config.revision, patch_revision = app.config.patch_revision;
    while (now < 560) tick();
    assert(app.runtime.held_mask == switches && app.display.status == BOSUN_DISPLAY_OK);
    assert(app.display.labels[0].length == 5 && !memcmp(app.display.labels[0].glyphs, "BOOST", 5));
    char context[2048]; bosun_json_writer_t writer;
    bosun_json_writer_init(&writer, context, sizeof context);
    assert(bosun_runtime_context(&app.runtime, &writer));
    assert(strstr(context, "\"hold_effect\":\"BOOST\""));
    switches = 0;
    while (now < 620) tick();
    assert(!app.runtime.held_mask && !app.display.labels[0].length && !app.display.hold_effect[0]);
    assert(app.config.revision == revision && app.config.patch_revision == patch_revision);
    bosun_json_writer_init(&writer, context, sizeof context);
    assert(bosun_runtime_context(&app.runtime, &writer));
    assert(strstr(context, "\"hold_effect\":\"\""));
    /* Holding an already-on effect temporarily switches it off: the LED
     * must stay dim while held and restore on release, as CircuitPython does. */
    app.runtime.switches[4].latched_on = true;
    switches = 1u << 4;
    while (now < 1200) tick();
    assert(app.runtime.held_mask == switches && !app.runtime.switches[4].latched_on);
    assert(app.leds[12] == 0x010000 && app.display.labels[0].length == 5);
    switches = 0;
    while (now < 1260) tick();
    assert(!app.runtime.held_mask && app.runtime.switches[4].latched_on);
    assert(app.leds[12] == 0x400000 && !app.display.labels[0].length);
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

static void test_initial_patch_action(void) {
    static const char patch[] = "{\"name\":\"Boot\",\"on_enter\":{\"messages\":["
        "{\"type\":\"cc\",\"cc\":20,\"value\":31},"
        "{\"type\":\"delay\",\"ms\":5},{\"type\":\"pc\",\"program\":9}]}}";
    static const uint8_t expected[] = {0xb0, 20, 31, 0xc0, 9};
    for (unsigned kemper = 0; kemper < 2; ++kemper) {
        fixture();
        assert(bosun_config_create("boot", "Boot", kemper ? "kemper_player" : "generic_midi", NULL) == BOSUN_STORE_OK);
        assert(bosun_config_put_patch(&app.config, "boot", 1, 1, patch, sizeof patch - 1, 0) == BOSUN_STORE_OK);
        if (kemper)
            assert(bosun_config_put_device(&app.config, "boot", "{\"kemper\":{}}", 13) == BOSUN_STORE_OK);
        assert(bosun_config_activate(&app.config, "boot", true) == BOSUN_STORE_OK);
        assert(bosun_application_init(&app, root));
        assert(!writes && !app.runtime.midi_tx_count && !app.runtime.queue_count);
        assert(app.startup_action_pending == !kemper);
        tick();
        assert(!app.startup_action_pending);
        if (!kemper) {
            assert(midi_length[0] == 3 && midi_length[1] == 3);
            assert(!memcmp(midi_output[0], expected, 3));
            while (now < 5) tick();
            assert(midi_length[0] == 3); /* cooperative delay has not elapsed */
            while (now < 20) tick();
            for (unsigned port = 0; port < 2; ++port)
                assert(midi_length[port] == sizeof expected && !memcmp(midi_output[port], expected, sizeof expected));
            assert(bosun_config_put_device(&app.config, NULL, "{}", 2) == BOSUN_STORE_OK);
            while (now < 100) tick();
            assert(midi_length[0] == sizeof expected && midi_length[1] == sizeof expected);
        } else {
            while (now < 100) tick();
            assert(!app.runtime.queue_count);
            for (unsigned port = 0; port < 2; ++port)
                for (size_t i = 0; i + 3 <= midi_length[port]; ++i)
                    assert(memcmp(midi_output[port] + i, expected, 3));
        }
    }
}

static void test_usb_diagnostics_console(void) {
    fixture();
    /* Largest 32-bit fields must remain a complete line even with zero and
     * partial console progress. A subsequent sample cannot splice this one. */
    usb_session_generation = UINT32_MAX;
    usb_diagnostics.rx_bytes = usb_diagnostics.tx_bytes = UINT32_MAX;
    usb_diagnostics.rx_fnv1a = UINT32_C(0x01234567);
    usb_diagnostics.tx_fnv1a = UINT32_MAX;
    app.protocol.requests = UINT32_MAX;
    app.runtime.midi_rx_count = app.runtime.midi_tx_count = UINT32_MAX;
    app.midi_rejected = app.midi_abandoned = UINT32_MAX;
    feeds = tasks = app.ticks = UINT32_MAX - 1;
    capture_console = true; console_limit = 0; now = 3000;
    tick();
    assert(!console_length && app.console_length);
    size_t expected = app.console_length;
    assert(expected < sizeof app.console && app.console[expected - 1] == '\n');
    usb_diagnostics.rx_bytes = usb_diagnostics.tx_bytes = 12;
    console_limit = 17;
    while (app.console_length) tick();
    assert(console_length == expected && console_output[console_length - 1] == '\n');
    assert(strstr((char *)console_output, "usb_session=4294967295 requests=4294967295"));
    assert(strstr((char *)console_output, "cdc_rx_bytes=4294967295 cdc_rx_fnv=01234567"));
    assert(strstr((char *)console_output, "cdc_tx_bytes=4294967295 cdc_tx_fnv=ffffffff\r\n"));
    capture_usb_rx = true;
    memset(&usb_rx_diagnostics, 0xff, sizeof usb_rx_diagnostics);
    console_length = 0; console_limit = 0; tick();
    assert(app.console_length && !console_length);
    expected = app.console_length;
    assert(expected < sizeof app.console && app.console[expected - 1] == '\n');
    memset(&usb_rx_diagnostics, 0, sizeof usb_rx_diagnostics);
    console_limit = 17;
    while (app.console_length) tick();
    assert(console_length == expected && console_output[console_length - 1] == '\n');
    assert(strstr((char *)console_output, "Bosun usb_rx usb_session=4294967295 arms=4294967295"));
    assert(strstr((char *)console_output, "dcd_bytes=4294967295 dcd_fnv=ffffffff"));
    assert(strstr((char *)console_output, "cdc_bytes=4294967295 cdc_fnv=ffffffff"));
    assert(strstr((char *)console_output, "sys_hz=4294967295 usb_hz=4294967295\r\n"));
    capture_usb_rx = false;
}

int main(void) {
    assert(mkdtemp(root));
    test_midi_backpressure(); test_cdc_and_overruns(); test_leds_and_expression(); test_expression_presence();
    test_reboot_modes(); test_reboot_backpressure(); test_reboot_unavailable_storage();
    test_unobserved_cdc_edges();
    test_hold_label_matches_context();
    test_initial_patch_action();
    test_usb_diagnostics_console();
    assert(bosun_store_format() == BOSUN_STORE_OK && rmdir(root) == 0);
    puts("Application: non-destructive boot, bounded/atomic dual MIDI queues, partial CDC, session reset, DMA overrun, LED parity, absent expression, display and watchdog passed");
    return 0;
}
