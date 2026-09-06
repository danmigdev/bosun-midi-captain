#define _POSIX_C_SOURCE 200809L
#include "bosun/protocol.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static bosun_config_t config;
static bosun_runtime_t runtime;
static bosun_protocol_t protocol;
static bosun_json_token_t reply_tokens[4096];
static bosun_json_doc_t reply;
static char output[BOSUN_PROTOCOL_TX_BYTES], large[BOSUN_PROTOCOL_RX_BYTES + 2048];
static unsigned sent;
static bool send_midi(void *context, const uint8_t *data, size_t length) {
    (void)context; (void)data; (void)length; ++sent; return true;
}
static void read_reply(const char *type, const char *id) {
    size_t length; const uint8_t *bytes = bosun_protocol_output(&protocol, &length);
    if (!length) fprintf(stderr, "Missing %s reply (request=%s, events=%u, context_ms=%lu)\n",
        type, protocol.type, protocol.event_length, (unsigned long)protocol.last_context_ms);
    assert(length && length < sizeof output && bytes[length - 1] == '\n');
    memcpy(output, bytes, length); output[length] = 0;
    assert(bosun_json_parse(&reply, output, length, reply_tokens, 4096) == BOSUN_JSON_OK);
    if (!bosun_json_equal(&reply, bosun_json_get(&reply, 0, "type"), type))
        fprintf(stderr, "Expected %s, received %s\n", type, output);
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "type"), type));
    if (id) assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "id"), id));
    bosun_protocol_consume_output(&protocol, length);
}
static void request(const char *json, const char *type) {
    size_t n = strlen(json);
    /* Arbitrary fragmentation must have identical behavior to a whole line. */
    for (size_t i = 0; i < n; ++i)
        assert(bosun_protocol_feed(&protocol, (const uint8_t *)json + i, 1, 1) == 1);
    assert(bosun_protocol_feed(&protocol, (const uint8_t *)"\n", 1, 1) == 1);
    read_reply(type, NULL);
}
static void is_error(const char *value) {
    if (!bosun_json_equal(&reply, bosun_json_get(&reply, 0, "error"), value))
        fprintf(stderr, "Expected error %s, received %s\n", value, output);
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "error"), value));
}
static uint32_t diagnostic_led(uint8_t index) { return ((uint32_t)index << 16) | 0x8000u | (255u - index); }
static void led_dump(void) {
    request("{\"type\":\"LED_DUMP\"}", "ERROR"); is_error("leds_unavailable");
    protocol.read_led = diagnostic_led;
    request("{\"type\":\"LED_DUMP\",\"id\":\"led-check\"}", "LED_DUMP");
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "id"), "led-check"));
    int pixels = bosun_json_get(&reply, 0, "pixels");
    for (unsigned i = 0; i < 30; ++i) {
        int pixel = bosun_json_at(&reply, pixels, i); int32_t channel;
        assert(bosun_json_integer(&reply, bosun_json_at(&reply, pixel, 0), &channel) && channel == (int32_t)i);
        assert(bosun_json_integer(&reply, bosun_json_at(&reply, pixel, 1), &channel) && channel == 128);
        assert(bosun_json_integer(&reply, bosun_json_at(&reply, pixel, 2), &channel) && channel == 255 - (int32_t)i);
        assert(bosun_json_at(&reply, pixel, 3) < 0);
    }
    assert(bosun_json_at(&reply, pixels, 30) < 0);
    static const char *const names[] = {"1", "2", "3", "4", "up", "A", "B", "C", "D", "down"};
    static const unsigned physical[10][3] = {{0,1,2},{3,4,5},{6,7,8},{9,10,11},{12,13,14},
        {15,17,16},{18,20,19},{21,23,22},{24,26,25},{27,29,28}};
    int mapping = bosun_json_get(&reply, 0, "switch_indices");
    for (unsigned sw = 0; sw < 10; ++sw) {
        int ring = bosun_json_get(&reply, mapping, names[sw]);
        for (unsigned i = 0; i < 3; ++i) {
            int32_t index;
            assert(bosun_json_integer(&reply, bosun_json_at(&reply, ring, i), &index) && index == (int32_t)physical[sw][i]);
        }
    }
    int current = bosun_json_get(&reply, 0, "current"); int32_t coordinate;
    assert(bosun_json_integer(&reply, bosun_json_get(&reply, current, "bank"), &coordinate) && coordinate == config.bank);
    assert(bosun_json_integer(&reply, bosun_json_get(&reply, current, "slot"), &coordinate) && coordinate == config.slot);
    protocol.read_led = NULL;
}
static void reboot_modes(void) {
    static const char *const invalid[] = {
        "null", "true", "false", "0", "1.5", "[]", "{}", "\"\"",
        "\"BOOTLOADER\"", "\"format\"", "\"bootloader\\u0000\""
    };
    char command[160];
    for (unsigned i = 0; i < sizeof invalid / sizeof *invalid; ++i) {
        snprintf(command, sizeof command, "{\"type\":\"REBOOT\",\"mode\":%s}", invalid[i]);
        request(command, "ERROR"); is_error("invalid_request");
        assert(!protocol.reboot_requested && !protocol.reboot_bootloader);
    }
    request("{\"type\":\"REBOOT\",\"mode\":\"normal\",\"mode\":\"bootloader\"}", "ERROR");
    is_error("invalid_json"); assert(!protocol.reboot_requested && !protocol.reboot_bootloader);
    request("{\"type\":\"REBOOT\",\"mode\":\"bootloader\",\"m\\u006fde\":\"normal\"}", "ERROR");
    is_error("invalid_json"); assert(!protocol.reboot_requested && !protocol.reboot_bootloader);
    request("{\"type\":\"REBOOT\",\"mode\":\"normal\"}", "ACK");
    assert(protocol.reboot_requested && !protocol.reboot_bootloader);
    bosun_protocol_session(&protocol, false); bosun_protocol_session(&protocol, true);

    /* A full reply owns TX: a bootloader request cannot reset the board or
     * overwrite that reply until its complete command is admitted. */
    const char manifest[] = "{\"type\":\"GET_MANIFEST\"}\n";
    const char reboot[] = "{\"type\":\"REBOOT\",\"id\":\"recovery\",\"mode\":\"bootloader\"}\n";
    assert(bosun_protocol_feed(&protocol, (const uint8_t *)manifest, sizeof manifest - 1, 1) == sizeof manifest - 1);
    assert(!bosun_protocol_feed(&protocol, (const uint8_t *)reboot, sizeof reboot - 1, 2));
    assert(!protocol.reboot_requested && !protocol.reboot_bootloader);
    read_reply("MANIFEST", NULL);
    assert(bosun_protocol_feed(&protocol, (const uint8_t *)reboot, sizeof reboot - 1, 3) == sizeof reboot - 1);
    assert(protocol.reboot_requested && protocol.reboot_bootloader);
    read_reply("ACK", "recovery");
    bosun_protocol_tick(&protocol, 2000); assert(!protocol.tx_length);
    assert(!bosun_protocol_feed(&protocol, (const uint8_t *)manifest, sizeof manifest - 1, 2001));
    bosun_protocol_session(&protocol, false); bosun_protocol_session(&protocol, true);
    assert(!protocol.reboot_requested && !protocol.reboot_bootloader);
}
static void event_is(const char *name) {
    read_reply("EVENT", NULL);
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "event"), name));
}
static void device_info_screen_projection(void) {
    const char device[] = "{\"tft\":{\"layout\":["
        "{\"field\":\"bank\",\"color\":\"#9aa1ad\",\"prefix\":\"BANK \",\"suffix\":\"\"},"
        "{\"field\":\"kemper_rig_in_bank\",\"color\":\"#6fd99b\",\"prefix\":\"RIG \"},"
        "{\"field\":\"expression_mode\",\"color\":\"#ff7f00\"},"
        "{\"field\":\"hold_effect\",\"color\":\"#ffffff\"},"
        "{\"field\":\"bank\",\"color\":\"#000000\",\"prefix\":\"WRONG\"},"
        "null,{\"field\":\"slot\",\"color\":123,\"prefix\":false,\"suffix\":[]},"
        "{\"field\":\"slot\",\"color\":\"#abcdef\",\"prefix\":\"IGNORED\"}]}}";
    char previous[BOSUN_DEVICE_BYTES + 1]; size_t previous_length = config.device_doc.length;
    memcpy(previous, config.device, previous_length);
    assert(bosun_config_put_device(&config, NULL, device, sizeof device - 1) == BOSUN_STORE_OK);
    request("{\"type\":\"GET_DEVICE_INFO\"}", "DEVICE_INFO");
    int colors = bosun_json_get(&reply, 0, "tft_colors"), labels = bosun_json_get(&reply, 0, "tft_labels");
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, colors, "bank"), "#9aa1ad"));
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, colors, "kemper_rig_in_bank"), "#6fd99b"));
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, colors, "expression_mode"), "#ff7f00"));
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, colors, "hold_effect"), "#ffffff"));
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, colors, "slot"), "#abcdef"));
    int bank = bosun_json_get(&reply, labels, "bank"), rig = bosun_json_get(&reply, labels, "kemper_rig_in_bank");
    int slot = bosun_json_get(&reply, labels, "slot");
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, bank, "prefix"), "BANK "));
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, rig, "prefix"), "RIG "));
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, rig, "suffix"), ""));
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, slot, "prefix"), ""));
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, slot, "suffix"), ""));
    assert(bosun_config_put_device(&config, NULL, previous, previous_length) == BOSUN_STORE_OK);
}
static void array_is(const char *key, const uint8_t *data, size_t length) {
    int array = bosun_json_get(&reply, 0, key);
    for (unsigned i = 0; i < length; ++i) {
        int32_t value;
        assert(bosun_json_integer(&reply, bosun_json_at(&reply, array, i), &value) && value == data[i]);
    }
    assert(bosun_json_at(&reply, array, (unsigned)length) == -1);
}
static void monitor_and_learn(void) {
    bosun_protocol_session(&protocol, false); bosun_protocol_session(&protocol, true);
    const uint8_t first[] = {0xb3, 7, 42}, second[] = {0xc3, 11}, sysex[] = {0xf0, 0, 0x20, 0x33, 2, 0xf7};
    bosun_runtime_feed_midi(&runtime, 0, first, sizeof first, 1);
    assert(!protocol.event_length);
    request("{\"type\":\"START_MIDI_LEARN\"}", "ACK");
    bosun_runtime_feed_midi(&runtime, 0, first, sizeof first, 2);
    bosun_runtime_feed_midi(&runtime, 1, second, sizeof second, 2);
    bosun_protocol_tick(&protocol, 2); event_is("midi_in_captured");
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "kind"), "cc"));
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "port"), "usb"));
    array_is("data", first + 1, 2);
    bosun_protocol_tick(&protocol, 3); event_is("midi_in_captured");
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "kind"), "pc"));
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "port"), "din"));
    array_is("data", second + 1, 1);
    request("{\"type\":\"SET_MIDI_MONITOR\",\"on\":true}", "ACK");
    const char *manifest = "{\"type\":\"GET_MANIFEST\",\"id\":\"monitor-drain\"}\n";
    assert(bosun_protocol_feed(&protocol, (const uint8_t *)manifest, strlen(manifest), 4) == strlen(manifest));
    size_t before = protocol.tx_length;
    bosun_runtime_feed_midi(&runtime, 1, sysex, sizeof sysex, 4);
    bosun_protocol_tick(&protocol, 4);
    assert(protocol.tx_length == before);
    read_reply("MANIFEST", "monitor-drain");
    bosun_protocol_tick(&protocol, 5); event_is("midi"); array_is("raw", sysex, sizeof sysex);
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "dir"), "in"));
    bosun_protocol_tick(&protocol, 6); event_is("midi_in_captured"); array_is("data", sysex + 1, sizeof sysex - 2);
    request("{\"type\":\"STOP_MIDI_LEARN\"}", "ACK");
    runtime.monitor(runtime.monitor_context, true, 0, 0, 0, first, sizeof first);
    bosun_protocol_tick(&protocol, 7); event_is("midi"); array_is("raw", first, sizeof first);
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "dir"), "out"));
    assert(bosun_json_get(&reply, 0, "port") == -1);
    uint8_t maximum[BOSUN_MIDI_MAX_SYSEX + 2];
    memset(maximum, 127, sizeof maximum); maximum[0] = 0xf0; maximum[sizeof maximum - 1] = 0xf7;
    bosun_runtime_feed_midi(&runtime, 0, maximum, sizeof maximum, 8);
    bosun_protocol_tick(&protocol, 8); event_is("midi"); array_is("raw", maximum, sizeof maximum);
    /* Wrap the binary ring repeatedly, with FIFO order checked at each step. */
    for (unsigned batch = 0; batch < 12; ++batch) {
        for (unsigned i = 0; i < 80; ++i) {
            const uint8_t wire[] = {0xb0, (uint8_t)batch, (uint8_t)i};
            bosun_runtime_feed_midi(&runtime, 0, wire, sizeof wire, 8);
        }
        for (unsigned i = 0; i < 80; ++i) {
            const uint8_t wire[] = {0xb0, (uint8_t)batch, (uint8_t)i};
            bosun_protocol_tick(&protocol, 8); event_is("midi"); array_is("raw", wire, sizeof wire);
        }
        assert(!protocol.event_length);
    }
    uint32_t dropped = protocol.midi_events_dropped;
    for (unsigned i = 0; i < 400; ++i) bosun_runtime_feed_midi(&runtime, 0, first, sizeof first, 8);
    assert(protocol.event_length <= BOSUN_PROTOCOL_EVENT_BYTES && protocol.midi_events_dropped > dropped);
    /* A monitor flood must not prevent fresh stage context from being sent. */
    ++runtime.revision;
    bosun_protocol_tick(&protocol, 50000); read_reply("CONTEXT", NULL);
    assert(protocol.event_length);
    bosun_protocol_session(&protocol, false); bosun_protocol_session(&protocol, true);
    assert(!protocol.event_length && !runtime.midi_monitor && !runtime.midi_learn && !runtime.learn.fresh);
    /* No event from the previous connection survives reconnect. */
    bosun_protocol_tick(&protocol, 50050); read_reply("CONTEXT", NULL);
    bosun_protocol_tick(&protocol, 50050); assert(!protocol.tx_length);
}
static void rig_info(void) {
    request("{\"type\":\"GET_RIG_INFO\",\"request\":false}", "ERROR"); is_error("no_rig_info");
    request("{\"type\":\"PUT_GLOBAL\",\"device\":{\"kemper\":{},\"midi_channel\":1}}", "ACK");
    bosun_runtime_config_changed(&runtime);
    request("{\"type\":\"GET_RIG_INFO\",\"request\":false}", "RIG_INFO");
    assert(reply.tokens[bosun_json_get(&reply, 0, "rig")].type == BOSUN_JSON_NULL);
    unsigned before = sent;
    request("{\"type\":\"GET_RIG_INFO\",\"request\":\"yes\"}", "ERROR"); is_error("invalid_request");
    assert(sent == before);
    request("{\"type\":\"GET_RIG_INFO\"}", "RIG_INFO"); assert(sent == before);
    runtime.kemper.rig_identity_known = true;
    request("{\"type\":\"GET_RIG_INFO\"}", "RIG_INFO"); assert(sent == before + 1);
    strcpy(runtime.kemper.last_name, "Cached rig"); runtime.kemper.last_name_rig = 2;
    strcpy(runtime.kemper.state.rig_name, "Cached rig");
    runtime.kemper.state.rig_name_fresh = true;
    request("{\"type\":\"GET_RIG_INFO\",\"request\":false}", "RIG_INFO");
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "name"), "Cached rig"));
    assert(bosun_config_bool(&reply, 0, "fresh", false));
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "color"), "#f5dc34"));
    assert(sent == before + 1);
    /* Returning to the cached coordinates does not make an old-generation
     * name fresh. CONTEXT and RIG_INFO must agree while the rig settles. */
    assert(bosun_kemper_begin_rig(&runtime.kemper, 2, 90));
    request("{\"type\":\"GET_RIG_INFO\",\"request\":false}", "RIG_INFO");
    assert(!bosun_config_bool(&reply, 0, "fresh", true));
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "name"), "Cached rig"));
    config.bank = 3; config.slot = 5;
    assert(bosun_kemper_begin_rig(&runtime.kemper, 15, 100));
    request("{\"type\":\"GET_RIG_INFO\"}", "RIG_INFO");
    assert(!bosun_config_bool(&reply, 0, "fresh", true));
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "name"), "Cached rig"));
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "color"), "#c08aff"));
    assert(sent == before + 1); /* No untagged name request during transition. */
}
static void coordinates_are(unsigned bank, unsigned slot) {
    int32_t actual;
    assert(bosun_json_integer(&reply, bosun_json_get(&reply, 0, "bank"), &actual) && actual == (int32_t)bank);
    assert(bosun_json_integer(&reply, bosun_json_get(&reply, 0, "slot"), &actual) && actual == (int32_t)slot);
}
static void one_patch_is(unsigned bank, unsigned slot) {
    int patches = bosun_json_get(&reply, 0, "patches");
    int item = bosun_json_at(&reply, patches, 0);
    assert(item >= 0 && bosun_json_at(&reply, patches, 1) == -1);
    assert(bosun_config_int(&reply, item, "bank", -1) == (int32_t)bank);
    assert(bosun_config_int(&reply, item, "slot", -1) == (int32_t)slot);
}
static void ui_events(void) {
    static const char active[] = "{\"name\":\"Renamed\",\"bindings\":[{\"switch\":\"1\",\"mode\":\"latched\",\"actions\":{"
        "\"toggle_on\":{\"messages\":[{\"type\":\"cc\",\"cc\":10,\"value\":127}]},"
        "\"toggle_off\":{\"messages\":[{\"type\":\"cc\",\"cc\":10,\"value\":0}]}}}]}";
    assert(bosun_config_create("ui", "UI", "generic_midi", NULL) == BOSUN_STORE_OK);
    assert(bosun_config_put_patch(&config, "ui", 1, 1, active, strlen(active), 0) == BOSUN_STORE_OK);
    assert(bosun_config_put_patch(&config, "ui", 1, 2, "{\"name\":\"Two\"}", 14, 0) == BOSUN_STORE_OK);
    assert(bosun_config_activate(&config, "ui", false) == BOSUN_STORE_OK);
    bosun_runtime_config_changed(&runtime);
    bosun_protocol_init(&protocol, &runtime); bosun_protocol_session(&protocol, true);
    request("{\"type\":\"PUT_GLOBAL\",\"device\":{\"leds\":{\"brightness\":32}}}", "ACK");
    bosun_protocol_tick(&protocol, 10); event_is("global_changed");
    int written = snprintf(large, sizeof large, "{\"type\":\"PUT_PATCH\",\"bank\":1,\"slot\":1,\"patch\":%s}", active);
    assert(written > 0 && (size_t)written < sizeof large); request(large, "ACK");
    bosun_protocol_tick(&protocol, 11); event_is("patch_switched"); coordinates_are(1, 1);
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "source"), "editor"));
    bosun_protocol_tick(&protocol, 12); event_is("dirty_state_changed"); one_patch_is(1, 1);
    request("{\"type\":\"SAVE_NOW\"}", "SAVED");
    bosun_protocol_tick(&protocol, 13); event_is("saved"); one_patch_is(1, 1);
    bosun_protocol_tick(&protocol, 14); event_is("dirty_state_changed");
    assert(bosun_json_at(&reply, bosun_json_get(&reply, 0, "patches"), 0) == -1);
    request("{\"type\":\"PUT_PATCH\",\"bank\":1,\"slot\":1,\"patch\":{\"name\":\"Rejected edit\"}}", "ACK");
    bosun_protocol_tick(&protocol, 15); event_is("patch_switched");
    bosun_protocol_tick(&protocol, 16); event_is("dirty_state_changed");
    request("{\"type\":\"DISCARD\"}", "ACK");
    bosun_protocol_tick(&protocol, 17); event_is("patch_switched");
    bosun_protocol_tick(&protocol, 18); event_is("discarded"); one_patch_is(1, 1);
    bosun_protocol_tick(&protocol, 19); event_is("dirty_state_changed");
    request("{\"type\":\"GET_PATCH\",\"bank\":1,\"slot\":1}", "PATCH"); assert(strstr(output, "Renamed"));
    request("{\"type\":\"PUT_GLOBAL\",\"device\":{\"autosave\":{\"enabled\":true,\"debounce_ms\":10}}}", "ACK");
    bosun_protocol_tick(&protocol, 20); event_is("global_changed");
    request("{\"type\":\"PUT_PATCH\",\"bank\":1,\"slot\":2,\"patch\":{\"name\":\"Auto\"}}", "ACK");
    bosun_protocol_tick(&protocol, 21); event_is("dirty_state_changed"); one_patch_is(1, 2);
    bosun_config_tick(&config, 25);
    bosun_protocol_tick(&protocol, 25); event_is("saved"); one_patch_is(1, 2);
    bosun_protocol_tick(&protocol, 26); event_is("dirty_state_changed");
    const char *manifest = "{\"type\":\"GET_MANIFEST\"}\n";
    assert(bosun_protocol_feed(&protocol, (const uint8_t *)manifest, strlen(manifest), 27) == strlen(manifest));
    size_t response_length = protocol.tx_length; memcpy(output, protocol.tx, response_length);
    bosun_protocol_consume_output(&protocol, 7);
    const char *updated = "{\"leds\":{\"brightness\":50}}";
    assert(bosun_config_put_device(&config, NULL, updated, strlen(updated)) == BOSUN_STORE_OK);
    assert(bosun_runtime_switch_patch(&runtime, 1, 2, false) == BOSUN_STORE_OK);
    bosun_protocol_tick(&protocol, 28);
    assert(bosun_runtime_switch_patch(&runtime, 1, 1, false) == BOSUN_STORE_OK);
    bosun_protocol_tick(&protocol, 29);
    size_t remaining; const uint8_t *bytes = bosun_protocol_output(&protocol, &remaining);
    assert(remaining == response_length - 7 && !memcmp(bytes, output + 7, remaining));
    bosun_protocol_consume_output(&protocol, remaining);
    bosun_protocol_tick(&protocol, 30); event_is("global_changed");
    bosun_protocol_tick(&protocol, 31); event_is("patch_switched"); coordinates_are(1, 1);
    assert(!(protocol.ui_pending & 2u)); /* Only the latest location survives backpressure. */
    bosun_runtime_tick(&runtime, 100, 1, 0, 0); bosun_runtime_tick(&runtime, 105, 1, 0, 0);
    bosun_protocol_tick(&protocol, 105); event_is("binding_fired");
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "switch"), "1"));
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "action"), "toggle_on"));
    bosun_runtime_tick(&runtime, 110, 0, 0, 0); bosun_runtime_tick(&runtime, 115, 0, 0, 0);
    bosun_runtime_tick(&runtime, 120, 1, 0, 0); bosun_runtime_tick(&runtime, 125, 1, 0, 0);
    bosun_protocol_tick(&protocol, 126); event_is("binding_fired");
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "action"), "toggle_off"));
    const char *blocked_ping = "{\"type\":\"PING\"}\n";
    assert(bosun_protocol_feed(&protocol, (const uint8_t *)blocked_ping, strlen(blocked_ping), 127) == strlen(blocked_ping));
    size_t ack_length = protocol.tx_length;
    bosun_runtime_tick(&runtime, 130, 0, 0, 0); bosun_runtime_tick(&runtime, 135, 0, 0, 0);
    bosun_runtime_tick(&runtime, 140, 1, 0, 0); bosun_runtime_tick(&runtime, 145, 1, 0, 0);
    bosun_runtime_tick(&runtime, 150, 0, 0, 0); bosun_runtime_tick(&runtime, 155, 0, 0, 0);
    bosun_runtime_tick(&runtime, 160, 1, 0, 0); bosun_runtime_tick(&runtime, 165, 1, 0, 0);
    assert(protocol.tx_length == ack_length && protocol.binding_pending[0] == 4);
    read_reply("ACK", NULL);
    bosun_protocol_tick(&protocol, 166); event_is("binding_fired");
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "action"), "toggle_off"));
    request("{\"type\":\"PUT_PATCH\",\"bank\":1,\"slot\":1,\"patch\":{\"bindings\":[{\"switch\":\"1\",\"mode\":\"latched\","
        "\"actions\":{\"toggle_on\":{\"messages\":[{\"type\":\"unimplemented\"}]}}}]}}", "ACK");
    bosun_protocol_tick(&protocol, 167); event_is("patch_switched");
    bosun_protocol_tick(&protocol, 168); event_is("dirty_state_changed");
    unsigned rejected_before = runtime.unsupported_messages;
    bosun_runtime_tick(&runtime, 170, 0, 0, 0); bosun_runtime_tick(&runtime, 175, 0, 0, 0);
    bosun_runtime_tick(&runtime, 180, 1, 0, 0); bosun_runtime_tick(&runtime, 185, 1, 0, 0);
    assert(runtime.unsupported_messages == rejected_before + 1 && !protocol.binding_pending[0]);
    request("{\"type\":\"PUT_GLOBAL\",\"device\":{\"kemper\":{}}}", "ACK");
    bosun_runtime_config_changed(&runtime);
    bosun_protocol_tick(&protocol, 186); event_is("global_changed");
    const uint8_t pc[] = {0xc0, 1}; bosun_runtime_feed_midi(&runtime, 0, pc, sizeof pc, 200);
    bosun_protocol_tick(&protocol, 200); event_is("patch_switched"); coordinates_are(1, 2);
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "source"), "midi_in"));
    assert(!protocol.binding_pending[0]);
    request("{\"type\":\"PUT_PATCH\",\"bank\":99,\"slot\":10,\"patch\":{\"name\":\"Far\"}}", "ACK");
    bosun_protocol_tick(&protocol, 201); event_is("dirty_state_changed");
    request("{\"type\":\"SAVE_NOW\",\"bank\":99,\"slot\":10}", "SAVED");
    bosun_protocol_tick(&protocol, 202); event_is("saved"); one_patch_is(99, 10);
    bosun_protocol_tick(&protocol, 203); event_is("dirty_state_changed"); one_patch_is(1, 1);
    request("{\"type\":\"PUT_PATCH\",\"bank\":1,\"slot\":3,\"patch\":{\"name\":\"Cannot save\"}}", "ACK");
    bosun_protocol_tick(&protocol, 204); event_is("dirty_state_changed");
    assert(bosun_store_mkdir("/config/profiles/ui/patches/01/03.json") == BOSUN_STORE_OK);
    request("{\"type\":\"SAVE_NOW\",\"bank\":1,\"slot\":3}", "ERROR"); is_error("invalid_request");
    assert(bosun_config_dirty(&config, 1, 3) && !(protocol.ui_pending & 8u));
    request("{\"type\":\"SAVE_NOW\"}", "ERROR"); is_error("invalid_request");
    assert(!bosun_config_dirty(&config, 1, 1) && bosun_config_dirty(&config, 1, 3));
    bosun_protocol_tick(&protocol, 205); event_is("saved"); one_patch_is(1, 1);
    bosun_protocol_tick(&protocol, 206); event_is("dirty_state_changed"); one_patch_is(1, 3);
    assert(bosun_store_remove("/config/profiles/ui/patches/01/03.json") == BOSUN_STORE_OK);
    request("{\"type\":\"PUT_GLOBAL\",\"device\":{}}", "ACK"); assert(protocol.ui_pending);
    assert(bosun_config_activate(&config, "test", false) == BOSUN_STORE_OK);
    bosun_runtime_config_changed(&runtime);
    bosun_protocol_session(&protocol, false); bosun_protocol_session(&protocol, true);
    assert(!protocol.ui_pending);
    bosun_protocol_tick(&protocol, 300); read_reply("CONTEXT", NULL);
}

int main(void) {
    char root[] = "/tmp/bosun-protocol-XXXXXX";
    assert(mkdtemp(root) && bosun_store_mount(root));
    assert(bosun_config_init(&config) == BOSUN_STORE_OK);
    bosun_runtime_init(&runtime, &config, send_midi, NULL);
    bosun_protocol_init(&protocol, &runtime);
    assert(bosun_protocol_feed(&protocol, (const uint8_t *)"PING\n", 5, 0) == 0);
    bosun_protocol_session(&protocol, true);
    request("{\"type\":\"PING\",\"id\":\"escaped\\\"\\n\\u00e9\"}", "ACK");
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "id"), "escaped\"\n\xc3\xa9"));
    request("{\"type\":\"PING\",\"id\":42}", "ACK");
    int32_t id;
    assert(bosun_json_integer(&reply, bosun_json_get(&reply, 0, "id"), &id) && id == 42);
    request("{\"type\":\"PING\",\"id\":{}}", "ERROR"); is_error("invalid_id");
    request("{\"type\":\"PING\",\"id\":[]}", "ERROR"); is_error("invalid_id");
    const char *id_prefix = "{\"type\":\"PING\",\"id\":\"";
    size_t id_start = strlen(id_prefix); memcpy(large, id_prefix, id_start);
    memset(large + id_start, 'a', 254); memcpy(large + id_start + 254, "\"}", 3);
    request(large, "ACK"); /* Exactly 256 raw JSON bytes, including quotes. */
    memset(large + id_start, 'a', 255); memcpy(large + id_start + 255, "\"}", 3);
    request(large, "ERROR"); is_error("invalid_id");
    request("{\"type\":\"PING\",\"type\":\"REBOOT\"}", "ERROR"); is_error("invalid_json");
    request("{\"type\":\"PING\",\"t\\u0079pe\":\"REBOOT\"}", "ERROR"); is_error("invalid_json");
    request("{\"type\":\"REBOOT\",\"id\":1,\"id\":2}", "ERROR"); assert(!protocol.reboot_requested);
    request("[]", "ERROR"); request("null", "ERROR"); request("{bad", "ERROR");
    request("{\"type\":\"UNRECOGNIZED\"}", "ERROR"); is_error("unknown_type");
    request("{\"type\":\"PUT_FILE_BEGIN\",\"path\":\"code.py\",\"size\":0}", "ERROR");
    is_error("unsupported_native_firmware_ota");
    request("{\"type\":\"GET_DEVICE_INFO\"}", "DEVICE_INFO");
    assert(strstr(output, "native_experimental") && strstr(output, "preset_navigation"));
    int modes = bosun_json_get(&reply, 0, "reboot_modes");
    assert(bosun_json_equal(&reply, bosun_json_at(&reply, modes, 0), "normal"));
    assert(bosun_json_equal(&reply, bosun_json_at(&reply, modes, 1), "bootloader"));
    reboot_modes();
    led_dump();
    request("{\"type\":\"GET_MANIFEST\"}", "MANIFEST");
    int plugins = bosun_json_get(&reply, 0, "plugins");
    assert(bosun_json_get(&reply, plugins, "kemper_player") >= 0);
    assert(bosun_json_get(&reply, plugins, "generic_midi") >= 0);
    assert(bosun_json_get(&reply, plugins, "headrush_core") < 0);
    request("{\"type\":\"CREATE_PROFILE\",\"profile_id\":\"test\",\"name\":\"Test\",\"kind\":\"generic_midi\"}", "ACK");
    request("{\"type\":\"SWITCH_PROFILE\",\"profile_id\":\"test\"}", "ACK");
    device_info_screen_projection();
    request("{\"type\":\"PUT_GLOBAL\",\"device\":{\"unknown\":{\"preserved\":[1,true,null]},\"autosave\":{\"enabled\":false}}}", "ACK");
    request("{\"type\":\"GET_GLOBAL\"}", "GLOBAL");
    int device = bosun_json_get(&reply, 0, "device");
    assert(bosun_json_get(&reply, device, "unknown") >= 0);
    request("{\"type\":\"PUT_PATCH\",\"bank\":1,\"slot\":2,\"patch\":{\"name\":\"CLEAN\",\"bindings\":[],\"future\":123}}", "ACK");
    assert(bosun_config_dirty(&config, 1, 2));
    request("{\"type\":\"GET_PATCH\",\"id\":\"patch-id\",\"bank\":1,\"slot\":2}", "PATCH");
    assert(strstr(output, "CLEAN") && strstr(output, "future"));
    request("{\"type\":\"LIST_PATCHES\"}", "PATCH_LIST"); assert(strstr(output, "CLEAN"));
    request("{\"type\":\"SWITCH_PATCH\",\"bank\":1,\"slot\":2}", "ACK");
    assert(config.bank == 1 && config.slot == 2);
    request("{\"type\":\"PUT_BINDING\",\"bank\":1,\"slot\":2,\"binding\":{\"switch\":\"1\",\"mode\":\"tap\",\"actions\":{}}}", "ACK");
    request("{\"type\":\"GET_PATCH\",\"bank\":1,\"slot\":2}", "PATCH"); assert(strstr(output, "future"));
    request("{\"type\":\"SAVE_NOW\"}", "SAVED"); assert(!bosun_config_dirty(&config, 1, 2));
    request("{\"type\":\"PUT_PATCH\",\"bank\":1,\"slot\":2,\"patch\":{\"name\":\"temporary\"}}", "ACK");
    request("{\"type\":\"GET_DIRTY\"}", "DIRTY"); assert(strstr(output, "\"slot\":2"));
    request("{\"type\":\"DISCARD\"}", "ACK");
    request("{\"type\":\"GET_PATCH\",\"bank\":1,\"slot\":2}", "PATCH"); assert(strstr(output, "CLEAN"));
    request("{\"type\":\"GET_PATCH\",\"bank\":1.5,\"slot\":2}", "ERROR");
    request("{\"type\":\"SWITCH_PATCH\",\"bank\":999999,\"slot\":2}", "ERROR");
    request("{\"type\":\"GET_PATCH\",\"bank\":1,\"slot\":10}", "ERROR"); is_error("not_found");
    request("{\"type\":\"GET_GLOBAL\",\"profile\":\"missing\"}", "ERROR"); is_error("no_such_profile");
    request("{\"type\":\"CREATE_PROFILE\",\"profile_id\":\"other\"}", "ACK");
    request("{\"type\":\"PUT_PATCH\",\"profile\":\"other\",\"bank\":2,\"slot\":3,\"patch\":{\"name\":\"OTHER\"}}", "ACK");
    request("{\"type\":\"GET_PATCH\",\"profile\":\"other\",\"bank\":2,\"slot\":3}", "PATCH");
    assert(strstr(output, "OTHER") && !strcmp(config.profile, "test"));
    request("{\"type\":\"LIST_PROFILES\"}", "PROFILE_LIST"); assert(strstr(output, "other"));
    request("{\"type\":\"PUT_MIDI_LEARN\",\"table\":{\"pc_to_patch\":[]}}", "ACK");
    request("{\"type\":\"GET_MIDI_LEARN\"}", "MIDI_LEARN");
    request("{\"type\":\"PUT_MIDI_LEARN\",\"table\":[]}", "ERROR"); is_error("invalid_request");
    request("{\"type\":\"GET_MIDI_LEARN\"}", "MIDI_LEARN"); assert(strstr(output, "\"pc_to_patch\":[]"));
    const char *broken = "{broken";
    assert(bosun_store_write_atomic("/config/profiles/test/midi_learn.json", broken, strlen(broken)) == BOSUN_STORE_OK);
    request("{\"type\":\"GET_MIDI_LEARN\"}", "ERROR"); is_error("invalid_request");
    request("{\"type\":\"PUT_MIDI_LEARN\",\"table\":{\"pc_to_patch\":[],\"future\":{\"saved\":true}}}", "ACK");
    request("{\"type\":\"GET_MIDI_LEARN\"}", "MIDI_LEARN"); assert(strstr(output, "\"future\""));
    request("{\"type\":\"START_MIDI_LEARN\"}", "ACK"); assert(runtime.midi_learn);
    request("{\"type\":\"STOP_MIDI_LEARN\"}", "ACK"); assert(!runtime.midi_learn);
    request("{\"type\":\"SET_MIDI_MONITOR\",\"on\":true}", "ACK"); assert(runtime.midi_monitor);
    request("{\"type\":\"SET_MIDI_MONITOR\",\"on\":\"yes\"}", "ERROR"); assert(runtime.midi_monitor);
    request("{\"type\":\"GET_CONTEXT\"}", "CONTEXT"); assert(strstr(output, "CLEAN"));
    request("{\"type\":\"STATS\"}", "STATS");

    /* Largest legal patch must round-trip verbatim, without partial JSON. */
    const char *prefix = "{\"type\":\"PUT_PATCH\",\"bank\":1,\"slot\":3,\"patch\":{\"padding\":\"";
    size_t n = strlen(prefix); memcpy(large, prefix, n);
    memset(large + n, 'a', BOSUN_PATCH_BYTES - 14); n += BOSUN_PATCH_BYTES - 14;
    memcpy(large + n, "\"}}", 4);
    request(large, "ACK");
    request("{\"type\":\"GET_PATCH\",\"bank\":1,\"slot\":3}", "PATCH");
    assert(strlen(output) > BOSUN_PATCH_BYTES - 20);

    /* Two requests in one read; retain second until every output byte drains. */
    const char *batch = "{\"type\":\"GET_MANIFEST\",\"id\":\"large\"}\n{\"type\":\"PING\",\"id\":\"after\"}\n";
    size_t consumed = bosun_protocol_feed(&protocol, (const uint8_t *)batch, strlen(batch), 10);
    assert(consumed < strlen(batch) && consumed == (size_t)(strchr(batch, '\n') - batch + 1));
    assert(bosun_protocol_feed(&protocol, (const uint8_t *)batch + consumed, strlen(batch) - consumed, 10) == 0);
    size_t remaining; const uint8_t *pending = bosun_protocol_output(&protocol, &remaining);
    char header[32]; memcpy(header, pending, sizeof header);
    bosun_protocol_tick(&protocol, 200); assert(!memcmp(header, protocol.tx, sizeof header));
    bosun_protocol_consume_output(&protocol, 7);
    size_t rest; pending = bosun_protocol_output(&protocol, &rest); assert(rest == remaining - 7 && pending == (uint8_t *)protocol.tx + 7);
    bosun_protocol_consume_output(&protocol, rest);
    assert(bosun_protocol_feed(&protocol, (const uint8_t *)batch + consumed, strlen(batch) - consumed, 200) == strlen(batch) - consumed);
    read_reply("ACK", "after");

    const char *ping = "{\"type\":\"PING\"}";
    memset(large, ' ', BOSUN_PROTOCOL_RX_BYTES + 1); memcpy(large, ping, strlen(ping));
    large[BOSUN_PROTOCOL_RX_BYTES] = '\n';
    assert(bosun_protocol_feed(&protocol, (uint8_t *)large, BOSUN_PROTOCOL_RX_BYTES + 1, 90) == BOSUN_PROTOCOL_RX_BYTES + 1);
    read_reply("ACK", NULL); /* Exact RX bound, including valid trailing whitespace. */
    large[BOSUN_PROTOCOL_RX_BYTES] = ' '; large[BOSUN_PROTOCOL_RX_BYTES + 1] = '\n';
    assert(bosun_protocol_feed(&protocol, (uint8_t *)large, BOSUN_PROTOCOL_RX_BYTES + 2, 91) == BOSUN_PROTOCOL_RX_BYTES + 2);
    read_reply("ERROR", NULL); is_error("request_too_large");

    memset(large, 'x', sizeof large); large[sizeof large - 1] = '\n';
    assert(bosun_protocol_feed(&protocol, (uint8_t *)large, sizeof large, 100) == sizeof large);
    read_reply("ERROR", NULL); is_error("request_too_large");
    request("{\"type\":\"PING\"}", "ACK");
    assert(bosun_protocol_feed(&protocol, (const uint8_t *)"{\"type\":", 8, UINT32_MAX - 100) == 8);
    bosun_protocol_tick(&protocol, 5000); read_reply("ERROR", NULL); is_error("receive_timeout");
    assert(bosun_protocol_feed(&protocol, (const uint8_t *)"\"REBOOT\"}\n", 10, 5001) == 10);
    read_reply("ERROR", NULL); assert(!protocol.reboot_requested);
    request("{\"type\":\"PING\"}", "ACK");
    assert(bosun_protocol_feed(&protocol, (const uint8_t *)"{\"type\":\"REBOOT\"}\n", 18, 5002) == 18);
    assert(protocol.reboot_requested && !protocol.reboot_bootloader);
    size_t reboot_length = protocol.tx_length;
    bosun_protocol_tick(&protocol, 9000); assert(protocol.tx_length == reboot_length);
    read_reply("ACK", NULL);
    bosun_protocol_tick(&protocol, 10000); assert(!protocol.tx_length);
    assert(bosun_protocol_feed(&protocol, (const uint8_t *)"{\"type\":\"PING\"}\n", 16, 10001) == 0);
    bosun_protocol_session(&protocol, false); bosun_protocol_session(&protocol, true);
    assert(!protocol.reboot_requested && !runtime.midi_monitor && !runtime.midi_learn);
    assert(!protocol.tx_length && !protocol.rx_length);
    request("{\"type\":\"PING\"}", "ACK");
    assert(sent == 0);
    monitor_and_learn(); rig_info(); ui_events();
    const char *stats = "{\"type\":\"STATS\"}\n";
    assert(bosun_protocol_feed(&protocol, (const uint8_t *)stats, strlen(stats), UINT32_MAX) == strlen(stats));
    read_reply("STATS", NULL); assert(strstr(output, "\"uptime_ms\":4294967295"));
    assert(bosun_store_write_atomic("/config/profiles/test/patches/01/02.json", broken, strlen(broken)) == BOSUN_STORE_OK);
    request("{\"type\":\"GET_PATCH\",\"bank\":1,\"slot\":2}", "ERROR"); is_error("invalid_request");
    assert(!bosun_store_mount("/no-such-native-storage-root"));
    request("{\"type\":\"GET_PATCH\",\"bank\":1,\"slot\":2}", "ERROR"); is_error("storage_unavailable");
    request("{\"type\":\"REBOOT\",\"mode\":\"bootloader\"}", "ACK");
    assert(protocol.reboot_requested && protocol.reboot_bootloader);
    puts("protocol: CRUD, manifest, exact bounds, storage failures, MIDI/UI events, auto-save/discard, binding callbacks, external rigs, backpressure, timeout/reboot/reconnect passed");
    return 0;
}
