#define _POSIX_C_SOURCE 200809L
#include "bosun/protocol.h"
#include "bosun/display.h"
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
static bool fail_midi;
static bool send_midi(void *context, const uint8_t *data, size_t length) {
    (void)context; (void)data; (void)length; ++sent; return !fail_midi;
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
static void full_catalog(void) {
    assert(bosun_config_create("full", "All Performances", "kemper_head", "") == BOSUN_STORE_OK);
    char path[160], patch[256], command[160];
    for (unsigned bank = 1; bank <= 125; ++bank) {
        snprintf(path, sizeof path, "/config/profiles/full/patches/%02u", bank);
        assert(bosun_store_mkdir(path) == BOSUN_STORE_OK);
        for (unsigned slot = 1; slot <= 5; ++slot) {
            snprintf(path, sizeof path, "/config/profiles/full/patches/%02u/%02u.json", bank, slot);
            snprintf(patch, sizeof patch, "{\"name\":\"Performance %u Slot %u %0120u\"}", bank, slot, bank);
            assert(bosun_store_write_atomic(path, patch, strlen(patch)) == BOSUN_STORE_OK);
        }
    }
    for (unsigned offset = 0; offset < 625; offset += 64) {
        snprintf(command, sizeof command, "{\"type\":\"LIST_PATCHES\",\"profile\":\"full\",\"offset\":%u,\"limit\":64}", offset);
        request(command, "PATCH_LIST");
        assert(bosun_config_int(&reply, 0, "total", -1) == 625);
        assert(bosun_config_int(&reply, 0, "offset", -1) == (int)offset);
        assert(bosun_config_int(&reply, 0, "next_offset", -2) == (offset + 64 < 625 ? (int)offset + 64 : -1));
        if (offset == 576) assert(strstr(output, "Performance 125 Slot 5"));
    }
    request("{\"type\":\"LIST_PATCHES\",\"profile\":\"full\",\"limit\":65}", "ERROR");
}
static void is_error(const char *value) {
    if (!bosun_json_equal(&reply, bosun_json_get(&reply, 0, "error"), value))
        fprintf(stderr, "Expected error %s, received %s\n", value, output);
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "error"), value));
}
static uint16_t physical_switch_mask;
static uint16_t read_switches(void) { return physical_switch_mask; }
static void activate_switch(void) {
    const char device[] = "{}";
    const char patch[] = "{\"bindings\":[{\"switch\":\"1\",\"mode\":\"momentary\",\"actions\":{\"press\":{\"messages\":[{\"type\":\"note_on\",\"note\":60}]},\"release\":{\"messages\":[{\"type\":\"note_off\",\"note\":60}]}}}]}";
    assert(bosun_config_activate(&config, "test", false) == BOSUN_STORE_OK);
    assert(bosun_config_put_device(&config, NULL, device, sizeof device - 1) == BOSUN_STORE_OK);
    assert(bosun_config_put_patch(&config, "test", 1, 1, patch, sizeof patch - 1, 0) == BOSUN_STORE_OK);
    assert(bosun_config_select(&config, 1, 1) == BOSUN_STORE_OK);
    bosun_runtime_init(&runtime, &config, send_midi, NULL);
    bosun_protocol_init(&protocol, &runtime); bosun_protocol_session(&protocol, true);
    protocol.read_switches = read_switches;
    sent = 0; physical_switch_mask = 0;
    request("{\"type\":\"GET_DEVICE_INFO\"}", "DEVICE_INFO");
    assert(bosun_config_bool(&reply, 0, "stage_input", false));
    static const char *const invalid[] = {
        "\"switch\":1,\"bank\":1,\"slot\":1", "\"switch\":\"UP\",\"bank\":1,\"slot\":1",
        "\"switch\":\"1\",\"bank\":true,\"slot\":1", "\"switch\":\"1\",\"bank\":1.5,\"slot\":1",
        "\"switch\":\"1\",\"bank\":1,\"slot\":null", "\"switch\":\"1\",\"bank\":0,\"slot\":1",
        "\"switch\":\"1\",\"bank\":1,\"slot\":11", "\"switch\":\"1\",\"bank\":999999999999,\"slot\":1",
        "\"switch\":\"1\",\"bank\":1", "\"switch\":\"1\",\"bank\":1,\"slot\":1,\"profile\":false",
        "\"switch\":\"1\\u0000\",\"bank\":1,\"slot\":1"
    };
    char command[240];
    for (unsigned i = 0; i < sizeof invalid / sizeof *invalid; ++i) {
        snprintf(command, sizeof command, "{\"type\":\"ACTIVATE_SWITCH\",%s}", invalid[i]);
        request(command, "ERROR"); is_error("invalid_request");
        assert(!sent && !runtime.queue_count && runtime.switches[0].stable);
    }
    request("{\"type\":\"ACTIVATE_SWITCH\",\"switch\":\"1\",\"switch\":\"2\",\"bank\":1,\"slot\":1}", "ERROR");
    is_error("invalid_json");
    request("{\"type\":\"ACTIVATE_SWITCH\",\"switch\":\"1\",\"bank\":2,\"slot\":1}", "ERROR"); is_error("stale_state");
    request("{\"type\":\"ACTIVATE_SWITCH\",\"switch\":\"1\",\"bank\":1,\"slot\":2}", "ERROR"); is_error("stale_state");
    request("{\"type\":\"ACTIVATE_SWITCH\",\"switch\":\"1\",\"bank\":1,\"slot\":1,\"profile\":\"removed\"}", "ERROR"); is_error("stale_state");
    request("{\"type\":\"ACTIVATE_SWITCH\",\"switch\":\"1\",\"bank\":1,\"slot\":1,\"profile\":\"\"}", "ERROR"); is_error("stale_state");
    request("{\"type\":\"ACTIVATE_SWITCH\",\"switch\":\"2\",\"bank\":1,\"slot\":1}", "ERROR"); is_error("unbound");
    physical_switch_mask = 4; /* GPIO changed before the next ordinary FSM poll. */
    request("{\"type\":\"ACTIVATE_SWITCH\",\"switch\":\"1\",\"bank\":1,\"slot\":1}", "ERROR"); is_error("busy");
    assert(!sent && !runtime.queue_count);
    physical_switch_mask = 0;
    request("{\"type\":\"ACTIVATE_SWITCH\",\"id\":\"tap\",\"switch\":\"1\",\"bank\":1,\"slot\":1,\"profile\":\"test\"}", "ACK");
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "id"), "tap"));
    assert(sent == 2 && !runtime.queue_count && runtime.switches[0].stable && !runtime.held_mask);
    bosun_protocol_session(&protocol, false);
    bosun_runtime_tick(&runtime, 1000, 0, 0, 0); assert(sent == 2);
    bosun_protocol_session(&protocol, true);
    request("{\"type\":\"ACTIVATE_SWITCH\",\"switch\":\"1\",\"bank\":1,\"slot\":1}", "ACK");
    assert(sent == 4 && !runtime.queue_count);
    protocol.read_switches = NULL;
}
static uint32_t diagnostic_led(uint8_t index) { return ((uint32_t)index << 16) | 0x8000u | (255u - index); }
static void active_patch_profile_and_draft(void) {
    request("{\"type\":\"PUT_BINDING\",\"bank\":1,\"slot\":1,\"binding\":{\"switch\":\"1\",\"mode\":\"latched\",\"label\":\"Draft Stage binding\"}}", "ACK");
    assert(bosun_config_dirty(&config, 1, 1));
    request("{\"type\":\"GET_PATCH\",\"bank\":1,\"slot\":1}", "PATCH");
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "profile"), ""));
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "active_profile"), "test"));
    assert(strstr(output, "Draft Stage binding") && !strstr(output, "note_on"));
    request("{\"type\":\"GET_PATCH\",\"profile\":\"\",\"bank\":1,\"slot\":1}", "PATCH");
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "active_profile"), "test"));
    assert(strstr(output, "Draft Stage binding"));
    /* The same profile ID, requested explicitly, continues to mean saved
     * bytes. Its reply must not masquerade as the active runtime binding. */
    request("{\"type\":\"GET_PATCH\",\"profile\":\"test\",\"bank\":1,\"slot\":1}", "PATCH");
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "profile"), "test"));
    assert(bosun_json_get(&reply, 0, "active_profile") < 0);
    assert(!strstr(output, "Draft Stage binding") && strstr(output, "note_on"));
    assert(bosun_config_dirty(&config, 1, 1));
}
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
    const char device[] = "{\"bank_count\":2,\"tft\":{\"layout\":["
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
    assert(bosun_config_int(&reply, 0, "bank_count", 0) == 2);
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
static void monitor_events(void) {
    bosun_protocol_session(&protocol, false); bosun_protocol_session(&protocol, true);
    const uint8_t first[] = {0xb3, 7, 42}, second[] = {0xc3, 11}, sysex[] = {0xf0, 0, 0x20, 0x33, 2, 0xf7};
    bosun_runtime_feed_midi(&runtime, 0, first, sizeof first, 1);
    assert(!protocol.event_length);
    request("{\"type\":\"SET_MIDI_MONITOR\",\"on\":true}", "ACK");
    bosun_runtime_feed_midi(&runtime, 0, first, sizeof first, 2);
    bosun_runtime_feed_midi(&runtime, 1, second, sizeof second, 2);
    bosun_protocol_tick(&protocol, 2); event_is("midi");
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "port"), "usb"));
    array_is("raw", first, sizeof first);
    bosun_protocol_tick(&protocol, 3); event_is("midi");
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "port"), "din"));
    array_is("raw", second, sizeof second);
    const char *manifest = "{\"type\":\"GET_MANIFEST\",\"id\":\"monitor-drain\"}\n";
    assert(bosun_protocol_feed(&protocol, (const uint8_t *)manifest, strlen(manifest), 4) == strlen(manifest));
    size_t before = protocol.tx_length;
    bosun_runtime_feed_midi(&runtime, 1, sysex, sizeof sysex, 4);
    bosun_protocol_tick(&protocol, 4);
    assert(protocol.tx_length == before);
    read_reply("MANIFEST", "monitor-drain");
    bosun_protocol_tick(&protocol, 5); event_is("midi"); array_is("raw", sysex, sizeof sysex);
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "dir"), "in"));
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
    assert(!protocol.event_length && !runtime.midi_monitor);
    /* No event from the previous connection survives reconnect. */
    bosun_protocol_tick(&protocol, 50050); read_reply("CONTEXT", NULL);
    bosun_protocol_tick(&protocol, 50050); assert(!protocol.tx_length);
}
static void rig_info(void) {
    request("{\"type\":\"GET_RIG_INFO\",\"request\":false}", "ERROR"); is_error("no_rig_info");
    /* Only a Kemper profile runs the Kemper engine and has rig info. */
    assert(bosun_config_create("player", "Player", "kemper_player", NULL) == BOSUN_STORE_OK);
    assert(bosun_config_activate(&config, "player", false) == BOSUN_STORE_OK);
    assert(bosun_config_put_patch(&config, "player", 1, 2, "{}", 2, 0) == BOSUN_STORE_OK);
    assert(bosun_config_select(&config, 1, 2) == BOSUN_STORE_OK);
    request("{\"type\":\"PUT_GLOBAL\",\"device\":{\"midi_channel\":1}}", "ACK");
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
    /* Browse program assignment is independent of Bosun patch coordinates. */
    runtime.kemper.browse_mode = true;
    runtime.kemper.state.rig = runtime.kemper.last_name_rig = 128;
    runtime.kemper.state.rig_name_fresh = true;
    request("{\"type\":\"GET_RIG_INFO\",\"request\":false}", "RIG_INFO");
    assert(bosun_config_bool(&reply, 0, "fresh", false));
    assert(bosun_config_int(&reply, 0, "rig", 0) == 128);
    runtime.kemper.last_name_rig = 127;
    request("{\"type\":\"GET_RIG_INFO\",\"request\":false}", "RIG_INFO");
    assert(!bosun_config_bool(&reply, 0, "fresh", true));
    runtime.kemper.browse_mode = false;
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
    assert(bosun_config_create("ui", "UI", "kemper_player", NULL) == BOSUN_STORE_OK);
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
    request("{\"type\":\"PUT_PATCH\",\"bank\":125,\"slot\":10,\"patch\":{\"name\":\"Far\"}}", "ACK");
    bosun_protocol_tick(&protocol, 201); event_is("dirty_state_changed");
    request("{\"type\":\"SAVE_NOW\",\"bank\":125,\"slot\":10}", "SAVED");
    bosun_protocol_tick(&protocol, 202); event_is("saved"); one_patch_is(125, 10);
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

static void morph_control(void) {
    assert(bosun_config_activate(&config, "player", false) == BOSUN_STORE_OK);
    const char device[] = "{\"kemper\":{}}";
    assert(bosun_config_put_device(&config, NULL, device, sizeof device - 1) == BOSUN_STORE_OK);
    assert(bosun_config_put_patch(&config, "player", 1, 1, "{}", 2, 0) == BOSUN_STORE_OK);
    assert(bosun_config_select(&config, 1, 1) == BOSUN_STORE_OK);
    bosun_runtime_init(&runtime, &config, send_midi, NULL);
    bosun_protocol_init(&protocol, &runtime); bosun_protocol_session(&protocol, true);
    protocol.read_switches = read_switches; physical_switch_mask = 0;
    runtime.kemper.state.connected = runtime.kemper.state.rig_name_fresh = true;
    sent = 0;
    const char position[] = "{\"type\":\"MORPH_CONTROL\",\"profile\":\"player\",\"bank\":1,\"slot\":1,\"generation\":0,\"action\":\"position\",\"percent\":100}";
    request(position, "ACK"); assert(sent == 1 && runtime.kemper.state.morph_value == 127);
    request("{\"type\":\"GET_CONTEXT\"}", "CONTEXT");
    assert(strstr(output, "\"kemper_morph_source\":\"commanded\"") && strstr(output, "\"kemper_morph_value\":127"));
    request("{\"type\":\"MORPH_CONTROL\",\"profile\":\"player\",\"bank\":1,\"slot\":1,\"generation\":0,\"action\":\"trigger\"}", "ACK");
    assert(sent == 3 && runtime.kemper.state.morph_value == -1);
    fail_midi = true;
    request(position, "ERROR"); is_error("midi_send_failed");
    assert(runtime.kemper.state.morph_value == -1); fail_midi = false;
    unsigned before = sent;
    request("{\"type\":\"MORPH_CONTROL\",\"profile\":\"player\",\"bank\":1,\"slot\":1,\"generation\":0,\"action\":\"position\",\"percent\":101}", "ERROR");
    is_error("invalid_request"); assert(sent == before);
    request("{\"type\":\"MORPH_CONTROL\",\"profile\":\"player\",\"bank\":1,\"slot\":1,\"generation\":1,\"action\":\"position\",\"percent\":0}", "ERROR");
    is_error("stale_state"); assert(sent == before);
    physical_switch_mask = 1; request(position, "ERROR"); is_error("busy"); assert(sent == before);
    physical_switch_mask = 0; runtime.preview_active = true;
    request(position, "ERROR"); is_error("busy"); assert(sent == before); runtime.preview_active = false;
    runtime.kemper.state.connected = false; request(position, "ERROR"); is_error("busy"); assert(sent == before);
    runtime.kemper.state.connected = true;
    request(position, "ACK"); assert(runtime.kemper.state.morph_value == 127);
    bosun_protocol_session(&protocol, false); bosun_protocol_session(&protocol, true);
    assert(runtime.kemper.state.morph_value == -1);
}

static void request_at(const char *json, const char *type, uint32_t now) {
    size_t n = strlen(json);
    assert(bosun_protocol_feed(&protocol, (const uint8_t *)json, n, now) == n);
    assert(bosun_protocol_feed(&protocol, (const uint8_t *)"\n", 1, now) == 1);
    read_reply(type, NULL);
}
static void locked_between(uint32_t first, uint32_t end) {
    assert(bosun_protocol_update_locked(&protocol, first) && bosun_protocol_update_locked(&protocol, end - 1));
    assert(!bosun_protocol_update_locked(&protocol, end) && !protocol.update_lock_seconds);
    /* Cleared once expired: the same instant one clock wrap later stays unlocked. */
    assert(!bosun_protocol_update_locked(&protocol, first));
}
/* Runs on activate_switch()'s profile: switch 1 is a bound momentary note. */
static void update_lock(void) {
    static const char *const invalid[] = {
        "", "\"seconds\":true", "\"seconds\":false", "\"seconds\":null", "\"seconds\":1.5",
        "\"seconds\":60.0", "\"seconds\":1e1", "\"seconds\":\"10\"", "\"seconds\":-1", "\"seconds\":121",
        "\"seconds\":4294967356", "\"seconds\":999999999999", "\"seconds\":[]", "\"seconds\":{}",
        "\"second\":10"
    };
    char command[160];
    for (unsigned i = 0; i < sizeof invalid / sizeof *invalid; ++i) {
        snprintf(command, sizeof command, "{\"type\":\"UPDATE_LOCK\"%s%s}", *invalid[i] ? "," : "", invalid[i]);
        request_at(command, "ERROR", 1000); is_error("invalid_request");
        assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "of"), "UPDATE_LOCK"));
        assert(!bosun_protocol_update_locked(&protocol, 1000) && !protocol.update_lock_seconds);
    }
    request_at("{\"type\":\"UPDATE_LOCK\",\"seconds\":10,\"seconds\":0}", "ERROR", 1000); is_error("invalid_json");
    assert(!bosun_protocol_update_locked(&protocol, 1000));
    request_at("{\"type\":\"UPDATE_LOCK\",\"id\":\"lock\",\"seconds\":60}", "ACK", 1000);
    assert(!strcmp(output, "{\"type\":\"ACK\",\"id\":\"lock\"}\n")); /* No extra fields. */
    locked_between(1000, 61000);
    /* Renewal restarts the lease from its own time, shorter or longer. */
    request_at("{\"type\":\"UPDATE_LOCK\",\"seconds\":60}", "ACK", 1000);
    request_at("{\"type\":\"UPDATE_LOCK\",\"seconds\":5}", "ACK", 30000);
    locked_between(30000, 35000);
    request_at("{\"type\":\"UPDATE_LOCK\",\"seconds\":120}", "ACK", 0);
    locked_between(0, 120000);
    /* 0 releases at once; releasing while unlocked is a harmless ACK. */
    request_at("{\"type\":\"UPDATE_LOCK\",\"seconds\":20}", "ACK", 5000);
    request_at("{\"type\":\"UPDATE_LOCK\",\"seconds\":0}", "ACK", 6000);
    assert(!bosun_protocol_update_locked(&protocol, 6000) && !protocol.update_lock_seconds);
    request_at("{\"type\":\"UPDATE_LOCK\",\"seconds\":-0}", "ACK", 6000);
    assert(!bosun_protocol_update_locked(&protocol, 6000));
    /* The lease runs across the millisecond clock wrap. */
    request_at("{\"type\":\"UPDATE_LOCK\",\"seconds\":1}", "ACK", UINT32_MAX - 100u);
    assert(bosun_protocol_update_locked(&protocol, UINT32_MAX) && bosun_protocol_update_locked(&protocol, 0));
    locked_between(UINT32_MAX - 100u, 899);
    /* While locked ACTIVATE_SWITCH is refused before its arguments are read;
     * other commands work and a session reset keeps the lease. */
    physical_switch_mask = 0; sent = 0;
    request_at("{\"type\":\"UPDATE_LOCK\",\"seconds\":30}", "ACK", 2000);
    request_at("{\"type\":\"ACTIVATE_SWITCH\",\"id\":\"tap\",\"switch\":\"1\",\"bank\":1,\"slot\":1}", "ERROR", 2001);
    is_error("update_lock"); assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "of"), "ACTIVATE_SWITCH"));
    request_at("{\"type\":\"ACTIVATE_SWITCH\",\"switch\":\"UP\"}", "ERROR", 2002); is_error("update_lock");
    assert(!sent && !runtime.queue_count && runtime.switches[0].stable);
    request_at("{\"type\":\"PING\"}", "ACK", 2003);
    request_at("{\"type\":\"GET_CONTEXT\"}", "CONTEXT", 2004);
    bosun_protocol_session(&protocol, false); bosun_protocol_session(&protocol, true);
    assert(bosun_protocol_update_locked(&protocol, 2005));
    request_at("{\"type\":\"ACTIVATE_SWITCH\",\"switch\":\"1\",\"bank\":1,\"slot\":1}", "ERROR", 31999); is_error("update_lock");
    request_at("{\"type\":\"ACTIVATE_SWITCH\",\"switch\":\"1\",\"bank\":1,\"slot\":1}", "ACK", 32000); /* Expired. */
    assert(sent == 2 && !protocol.update_lock_seconds);
    request_at("{\"type\":\"UPDATE_LOCK\",\"seconds\":30}", "ACK", 40000);
    request_at("{\"type\":\"UPDATE_LOCK\",\"seconds\":0}", "ACK", 40001);
    request_at("{\"type\":\"ACTIVATE_SWITCH\",\"switch\":\"1\",\"bank\":1,\"slot\":1}", "ACK", 40002);
    assert(sent == 4 && !runtime.queue_count);
}
static void identity_status_is(const char *status) {
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "status"), status));
}
static void use_profile(const char *id) {
    assert(bosun_config_activate(&config, id, false) == BOSUN_STORE_OK);
    bosun_runtime_init(&runtime, &config, send_midi, NULL);
    bosun_protocol_init(&protocol, &runtime); bosun_protocol_session(&protocol, true);
    sent = 0;
}
static void kemper_identity(void) {
    /* Pending inside the 2 s window, unavailable after it, received once the
     * Kemper answered. A probe that cannot be sent is a MIDI failure. */
    const char *probe = "{\"type\":\"GET_KEMPER_IDENTITY\",\"request\":true}";
    const char *read = "{\"type\":\"GET_KEMPER_IDENTITY\"}";
    use_profile("player");
    request_at(probe, "KEMPER_IDENTITY", 1000); identity_status_is("pending"); assert(sent == 1);
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "profile"), "player"));
    request_at(read, "KEMPER_IDENTITY", 2999); identity_status_is("pending"); assert(sent == 1);
    request_at(read, "KEMPER_IDENTITY", 3000); identity_status_is("unavailable");
    request_at(probe, "KEMPER_IDENTITY", 4000); identity_status_is("pending");
    const uint8_t answer[] = {0x7e,0,6,2,0,0x20,0x33,1,2,3,4,5,6,7,8};
    bosun_kemper_handle(&runtime.kemper, 0, 0xf0, answer, sizeof answer, 4001);
    request_at(read, "KEMPER_IDENTITY", 4002); identity_status_is("received");
    assert(strstr(output, "\"raw\":[1,2,3,4,5,6,7,8]"));
    fail_midi = true;
    request_at(probe, "ERROR", 5000); is_error("midi_send_failed");
    fail_midi = false;
    request_at(read, "KEMPER_IDENTITY", 5001); identity_status_is("unavailable");
    use_profile("test");
    request(probe, "ERROR"); is_error("no_rig_info");
}
static void draft_limit(void) {
    /* The active profile holds at most BOSUN_DIRTY_PATCHES unsaved drafts.
     * Saving one frees its entry; explicit-profile writes go to saved files
     * and never take one. A backup restore depends on both. */
    char command[160];
    request("{\"type\":\"CREATE_PROFILE\",\"profile_id\":\"drafts\",\"name\":\"Drafts\",\"kind\":\"generic_midi\"}", "ACK");
    use_profile("drafts");
    for (unsigned i = 0; i < BOSUN_DIRTY_PATCHES; ++i) {
        snprintf(command, sizeof command, "{\"type\":\"PUT_PATCH\",\"bank\":%u,\"slot\":%u,\"patch\":{}}", i / 10 + 1, i % 10 + 1);
        request(command, "ACK");
    }
    assert(config.dirty_count == BOSUN_DIRTY_PATCHES);
    request("{\"type\":\"PUT_PATCH\",\"bank\":13,\"slot\":9,\"patch\":{}}", "ERROR"); is_error("limit_exceeded");
    request("{\"type\":\"PUT_PATCH\",\"bank\":1,\"slot\":1,\"patch\":{\"name\":\"again\"}}", "ACK");
    request("{\"type\":\"PUT_PATCH\",\"profile\":\"drafts\",\"bank\":13,\"slot\":10,\"patch\":{}}", "ACK");
    assert(config.dirty_count == BOSUN_DIRTY_PATCHES && !bosun_config_dirty(&config, 13, 10));
    request("{\"type\":\"SAVE_NOW\",\"bank\":1,\"slot\":1}", "SAVED");
    assert(config.dirty_count == BOSUN_DIRTY_PATCHES - 1 && !bosun_config_dirty(&config, 1, 1));
    request("{\"type\":\"PUT_PATCH\",\"bank\":13,\"slot\":9,\"patch\":{}}", "ACK");
    assert(config.dirty_count == BOSUN_DIRTY_PATCHES);
    use_profile("test");
    request("{\"type\":\"DELETE_PROFILE\",\"profile_id\":\"drafts\"}", "ACK");
    assert(bosun_config_dirty(&config, 1, 1));
}

enum { LOGO_CHUNK = 12288 };
static uint8_t logo_image[BOSUN_BOOT_LOGO_BYTES], logo_big[LOGO_CHUNK + 1];
static uint32_t fnv1a(const uint8_t *data, size_t length) {
    uint32_t hash = UINT32_C(0x811c9dc5);
    for (size_t i = 0; i < length; ++i) hash = (hash ^ data[i]) * UINT32_C(0x01000193);
    return hash;
}
static void put_logo(uint32_t offset, const uint8_t *data, size_t length, const char *type) {
    static const char digits[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    char *out = large + snprintf(large, sizeof large, "{\"type\":\"PUT_LOGO\",\"id\":\"logo\",\"offset\":%u,\"data\":\"", (unsigned)offset);
    for (size_t i = 0; i < length; i += 3) {
        uint32_t bits = (uint32_t)data[i] << 16 | (i + 1 < length ? (uint32_t)data[i + 1] << 8 : 0) |
            (i + 2 < length ? data[i + 2] : 0);
        *out++ = digits[bits >> 18 & 63]; *out++ = digits[bits >> 12 & 63];
        *out++ = i + 1 < length ? digits[bits >> 6 & 63] : '=';
        *out++ = i + 2 < length ? digits[bits & 63] : '=';
    }
    strcpy(out, "\"}");
    request_at(large, type, 1);
}
static void received_is(uint32_t value) { assert(bosun_config_int(&reply, 0, "received", -1) == (int32_t)value); }
static void boot_logo_is(const char *state) {
    request("{\"type\":\"GET_DEVICE_INFO\"}", "DEVICE_INFO");
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "boot_logo"), state));
}
static void commit_logo(uint32_t hash, const char *type) {
    char command[96];
    snprintf(command, sizeof command, "{\"type\":\"COMMIT_LOGO\",\"size\":96000,\"fnv1a\":%lu}", (unsigned long)hash);
    request(command, type);
}
static void startup_logo(void) {
    uint32_t hash = 0;
    for (unsigned seed = 0; hash <= INT32_MAX; ++seed) { /* Exercise the full unsigned range. */
        for (size_t i = 0; i < sizeof logo_image; ++i) logo_image[i] = (uint8_t)(i * 31u + seed * 7u + (i >> 9));
        hash = fnv1a(logo_image, sizeof logo_image);
    }
    boot_logo_is("default");
    protocol.logo_preview = false;
    /* Strictly sequential: offset 0 (re)starts, otherwise offset == received. */
    put_logo(LOGO_CHUNK, logo_image + LOGO_CHUNK, LOGO_CHUNK, "ERROR"); is_error("offset_mismatch"); received_is(0);
    put_logo(0, logo_image, LOGO_CHUNK, "ACK"); received_is(LOGO_CHUNK);
    put_logo(0, logo_image, LOGO_CHUNK, "ACK"); received_is(LOGO_CHUNK);
    put_logo(LOGO_CHUNK, logo_image + LOGO_CHUNK, LOGO_CHUNK, "ACK"); received_is(2 * LOGO_CHUNK);
    put_logo(LOGO_CHUNK, logo_image + LOGO_CHUNK, LOGO_CHUNK, "ACK"); received_is(2 * LOGO_CHUNK);
    put_logo(LOGO_CHUNK, logo_image, LOGO_CHUNK, "ERROR"); is_error("offset_mismatch"); received_is(2 * LOGO_CHUNK);
    put_logo(LOGO_CHUNK, logo_image + LOGO_CHUNK, 100, "ERROR"); is_error("offset_mismatch");
    put_logo(3 * LOGO_CHUNK, logo_image + 3 * LOGO_CHUNK, LOGO_CHUNK, "ERROR"); is_error("offset_mismatch");
    commit_logo(hash, "ERROR"); is_error("incomplete_upload"); received_is(2 * LOGO_CHUNK);
    for (uint32_t offset = 2 * LOGO_CHUNK; offset < sizeof logo_image; offset += LOGO_CHUNK) {
        size_t length = sizeof logo_image - offset < LOGO_CHUNK ? sizeof logo_image - offset : LOGO_CHUNK;
        put_logo(offset, logo_image + offset, length, "ACK"); received_is(offset + (uint32_t)length);
    }
    received_is(sizeof logo_image);
    /* Never beyond 96000 bytes or 12288 per chunk; rejects keep the upload. */
    put_logo(sizeof logo_image, logo_image, 1, "ERROR"); is_error("limit_exceeded");
    put_logo(90000, logo_image, LOGO_CHUNK, "ERROR"); is_error("limit_exceeded");
    put_logo(0, logo_big, sizeof logo_big, "ERROR"); is_error("limit_exceeded");
    static const char *const bad_data[] = {"", "QQ", "QQ=", "QR==", "QUJ=", "Q===", "====", "QQ==QQ==",
        "QQ-_", "QQ_A", "QQ Q", "QQ\\/A", "QQ\\u0041A", "QUJD\\n"};
    char command[160];
    for (unsigned i = 0; i < sizeof bad_data / sizeof *bad_data; ++i) {
        snprintf(command, sizeof command, "{\"type\":\"PUT_LOGO\",\"offset\":0,\"data\":\"%s\"}", bad_data[i]);
        request(command, "ERROR"); is_error("invalid_data");
    }
    static const char *const bad_put[] = {"\"offset\":0", "\"data\":\"QUJD\"", "\"offset\":-1,\"data\":\"QUJD\"",
        "\"offset\":1.5,\"data\":\"QUJD\"", "\"offset\":\"0\",\"data\":\"QUJD\"", "\"offset\":0,\"data\":65",
        "\"offset\":0,\"data\":[\"QUJD\"]", "\"offset\":null,\"data\":\"QUJD\""};
    for (unsigned i = 0; i < sizeof bad_put / sizeof *bad_put; ++i) {
        snprintf(command, sizeof command, "{\"type\":\"PUT_LOGO\",%s}", bad_put[i]);
        request(command, "ERROR"); is_error("invalid_request");
    }
    request("{\"type\":\"PUT_LOGO\",\"offset\":0,\"offset\":96000,\"data\":\"QUJD\"}", "ERROR"); is_error("invalid_json");
    static const char *const bad_commit[] = {"\"fnv1a\":1", "\"size\":95999,\"fnv1a\":1", "\"size\":\"96000\",\"fnv1a\":1",
        "\"size\":96000", "\"size\":96000,\"fnv1a\":\"1\"", "\"size\":96000,\"fnv1a\":-1", "\"size\":96000,\"fnv1a\":1.0",
        "\"size\":96000,\"fnv1a\":4294967296", "\"size\":96000,\"fnv1a\":12345678901", "\"size\":96000,\"fnv1a\":1e3"};
    for (unsigned i = 0; i < sizeof bad_commit / sizeof *bad_commit; ++i) {
        snprintf(command, sizeof command, "{\"type\":\"COMMIT_LOGO\",%s}", bad_commit[i]);
        request(command, "ERROR"); is_error("invalid_request");
    }
    commit_logo(hash ^ 1u, "ERROR"); is_error("checksum_mismatch");
    assert(!protocol.logo_preview); boot_logo_is("default");
    commit_logo(hash, "ACK");
    assert(protocol.logo_preview); boot_logo_is("custom");
    static uint8_t stored[sizeof logo_image + 1]; size_t length = 0;
    assert(bosun_store_read(BOSUN_BOOT_LOGO_PATH, stored, sizeof stored, &length) == BOSUN_STORE_OK);
    assert(length == sizeof logo_image && !memcmp(stored, logo_image, length));
    commit_logo(hash, "ERROR"); is_error("incomplete_upload"); received_is(0);
    /* An abandoned replacement leaves the committed logo; DELETE drops both. */
    protocol.logo_preview = false;
    put_logo(0, logo_image + 100, LOGO_CHUNK, "ACK"); received_is(LOGO_CHUNK);
    assert(bosun_store_read(BOSUN_BOOT_LOGO_PATH, stored, sizeof stored, &length) == BOSUN_STORE_OK && length == sizeof logo_image);
    bosun_dirent_t entries[2]; size_t count = 9;
    assert(bosun_store_list(BOSUN_BOOT_LOGO_DIR, entries, 2, &count) == BOSUN_STORE_OK && count == 1);
    request("{\"type\":\"DELETE_LOGO\",\"id\":\"drop\"}", "ACK");
    assert(protocol.logo_preview); boot_logo_is("default");
    assert(bosun_store_read(BOSUN_BOOT_LOGO_PATH, stored, sizeof stored, &length) == BOSUN_STORE_NOT_FOUND);
    commit_logo(hash, "ERROR"); is_error("incomplete_upload"); received_is(0);
    protocol.logo_preview = false;
    request("{\"type\":\"DELETE_LOGO\"}", "ACK"); assert(protocol.logo_preview);
    assert(bosun_store_list(BOSUN_BOOT_LOGO_DIR, entries, 2, &count) == BOSUN_STORE_OK && count == 0);
    protocol.logo_preview = false;
}

/* Any reply; true when the request counted as activity. */
static bool wakes(const char *json) {
    protocol.activity = false;
    size_t n = strlen(json), length;
    assert(bosun_protocol_feed(&protocol, (const uint8_t *)json, n, 50000) == n);
    assert(bosun_protocol_feed(&protocol, (const uint8_t *)"\n", 1, 50000) == 1);
    (void)bosun_protocol_output(&protocol, &length);
    assert(length); bosun_protocol_consume_output(&protocol, length);
    bool woke = protocol.activity; protocol.activity = false;
    return woke;
}
/* One tick: exactly frame, or (NULL) anything but a screensaver frame. */
static void tick_is(uint32_t now, const char *frame) {
    bosun_protocol_tick(&protocol, now);
    size_t length; const uint8_t *bytes = bosun_protocol_output(&protocol, &length);
    assert(length < sizeof output);
    memcpy(output, bytes, length); output[length] = 0;
    bosun_protocol_consume_output(&protocol, length);
    if (frame) assert(!strcmp(output, frame));
    else assert(!strstr(output, "SCREENSAVER"));
}
static void quiet(uint32_t now) { for (unsigned i = 0; i < 20; ++i) tick_is(now + i * 100, NULL); }
static void screensaver_protocol(void) {
    static const char bounce[] = "{\"type\":\"SCREENSAVER\",\"id\":null,\"active\":true,\"effect\":\"bounce\"}\n";
    static const char breathe[] = "{\"type\":\"SCREENSAVER\",\"id\":null,\"active\":true,\"effect\":\"breathe\"}\n";
    static const char inactive[] = "{\"type\":\"SCREENSAVER\",\"id\":null,\"active\":false}\n";
    bosun_runtime_init(&runtime, &config, send_midi, NULL);
    bosun_protocol_init(&protocol, &runtime); bosun_protocol_session(&protocol, true);
    const char device[] = "{\"tft\":{\"screensaver_effect\":\"drift\"}}";
    assert(bosun_config_put_device(&config, NULL, device, sizeof device - 1) == BOSUN_STORE_OK);
    request("{\"type\":\"GET_DEVICE_INFO\"}", "DEVICE_INFO");
    assert(bosun_config_bool(&reply, 0, "screensaver", false));
    request("{\"type\":\"GET_SCREENSAVER\",\"id\":\"ss\"}", "SCREENSAVER");
    assert(!strcmp(output, "{\"type\":\"SCREENSAVER\",\"id\":\"ss\",\"active\":false}\n"));
    quiet(1000);
    /* Every change of the shown effect is announced once, to the session. */
    protocol.screensaver = BOSUN_SCREENSAVER_BOUNCE;
    tick_is(3000, bounce); quiet(3100);
    request("{\"type\":\"GET_SCREENSAVER\",\"id\":7}", "SCREENSAVER");
    assert(!strcmp(output, "{\"type\":\"SCREENSAVER\",\"id\":7,\"active\":true,\"effect\":\"bounce\"}\n"));
    protocol.screensaver = BOSUN_SCREENSAVER_BREATHE;
    tick_is(6000, breathe);
    protocol.screensaver = BOSUN_SCREENSAVER_DRIFT;
    tick_is(6100, "{\"type\":\"SCREENSAVER\",\"id\":null,\"active\":true,\"effect\":\"drift\"}\n");
    /* A new session learns an active screensaver at once. */
    bosun_protocol_session(&protocol, false);
    tick_is(6200, NULL);
    protocol.screensaver = BOSUN_SCREENSAVER_BREATHE;
    bosun_protocol_session(&protocol, true);
    tick_is(6300, breathe); quiet(6400);
    protocol.screensaver = BOSUN_SCREENSAVER_OFF;
    tick_is(9000, inactive); quiet(9100);
    bosun_protocol_session(&protocol, false); bosun_protocol_session(&protocol, true);
    quiet(12000); /* Inactive: nothing to announce. */
    /* SCREENSAVER_START: the given effect, else the configured one. */
    request("{\"type\":\"SCREENSAVER_START\",\"id\":\"go\"}", "ACK");
    assert(!strcmp(output, "{\"type\":\"ACK\",\"id\":\"go\"}\n") && protocol.screensaver_start == BOSUN_SCREENSAVER_DRIFT);
    static const char *const effects[] = {"bounce", "breathe", "drift", "mix"};
    char command[160];
    for (unsigned i = 0; i < 4; ++i) {
        protocol.screensaver_start = 0;
        snprintf(command, sizeof command, "{\"type\":\"SCREENSAVER_START\",\"effect\":\"%s\"}", effects[i]);
        request(command, "ACK"); assert(protocol.screensaver_start == i + 1);
    }
    static const char *const invalid[] = {"\"sparkle\"", "\"Bounce\"", "\"off\"", "\"\"", "null", "1", "[]", "{}", "true"};
    for (unsigned i = 0; i < sizeof invalid / sizeof *invalid; ++i) {
        protocol.screensaver_start = 0;
        snprintf(command, sizeof command, "{\"type\":\"SCREENSAVER_START\",\"effect\":%s}", invalid[i]);
        request(command, "ERROR"); is_error("invalid_request");
        assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "of"), "SCREENSAVER_START"));
        assert(!protocol.screensaver_start);
    }
    request("{\"type\":\"SCREENSAVER_START\",\"effect\":\"drift\",\"effect\":\"mix\"}", "ERROR"); is_error("invalid_json");
    assert(!protocol.screensaver_start);
    const char unknown[] = "{\"tft\":{\"screensaver_effect\":\"sparkle\"}}";
    assert(bosun_config_put_device(&config, NULL, unknown, sizeof unknown - 1) == BOSUN_STORE_OK);
    request("{\"type\":\"SCREENSAVER_START\"}", "ACK"); assert(protocol.screensaver_start == BOSUN_SCREENSAVER_MIX);
    protocol.screensaver_start = 0;
    /* Refused while the update lock is held, before the effect is read. */
    request_at("{\"type\":\"UPDATE_LOCK\",\"seconds\":30}", "ACK", 20000);
    request_at("{\"type\":\"SCREENSAVER_START\",\"id\":\"no\"}", "ERROR", 20001); is_error("update_lock");
    request_at("{\"type\":\"SCREENSAVER_START\",\"effect\":\"sparkle\"}", "ERROR", 20002); is_error("update_lock");
    assert(!protocol.screensaver_start);
    request_at("{\"type\":\"UPDATE_LOCK\",\"seconds\":0}", "ACK", 20003);
    request_at("{\"type\":\"SCREENSAVER_START\",\"effect\":\"bounce\"}", "ACK", 20004);
    assert(protocol.screensaver_start == BOSUN_SCREENSAVER_BOUNCE);
    protocol.screensaver_start = 0;
    /* Reads, polling, the lock and the screensaver's own requests never wake it. */
    static const char *const passive[] = {
        "{\"type\":\"PING\",\"id\":\"__hub_keepalive\"}", "{\"type\":\"GET_CONTEXT\"}",
        "{\"type\":\"GET_DEVICE_INFO\"}", "{\"type\":\"GET_GLOBAL\"}", "{\"type\":\"GET_PATCH\",\"bank\":1,\"slot\":1}",
        "{\"type\":\"GET_DIRTY\"}", "{\"type\":\"GET_MANIFEST\"}", "{\"type\":\"LIST_PATCHES\",\"limit\":8}",
        "{\"type\":\"LIST_PROFILES\"}", "{\"type\":\"STATS\"}", "{\"type\":\"LED_DUMP\"}",
        "{\"type\":\"GET_RIG_INFO\"}", "{\"type\":\"GET_KEMPER_IDENTITY\"}", "{\"type\":\"GET_SCREENSAVER\"}",
        "{\"type\":\"SCREENSAVER_START\",\"effect\":\"drift\"}", "{\"type\":\"UPDATE_LOCK\",\"seconds\":0}",
        "{\"type\":\"HUB_INFO\"}", "{\"type\":\"PUT_SOMETHING\"}", "{\"type\":\"SET_ANYTHING\"}",
        "{\"type\":\"PING\",\"type\":\"PUT_GLOBAL\"}", "{bad", "{\"id\":\"no-type\"}"
    };
    for (unsigned i = 0; i < sizeof passive / sizeof *passive; ++i) {
        bool woke = wakes(passive[i]);
        if (woke) fprintf(stderr, "Woke on %s\n", passive[i]);
        assert(!woke);
    }
    protocol.screensaver_start = 0;
    /* State-changing requests wake it, accepted or refused. */
    static const char *const changing[] = {
        "{\"type\":\"PUT_GLOBAL\",\"device\":{\"tft\":{\"screensaver_minutes\":5}}}",
        "{\"type\":\"PUT_PATCH\",\"bank\":2,\"slot\":5,\"patch\":{\"name\":\"Saver\"}}",
        "{\"type\":\"PUT_BINDING\",\"bank\":2,\"slot\":5,\"binding\":{}}",
        "{\"type\":\"DISCARD\"}", "{\"type\":\"SAVE_NOW\"}",
        "{\"type\":\"SWITCH_PATCH\",\"bank\":1,\"slot\":1}", "{\"type\":\"DELETE_PATCH\",\"bank\":9,\"slot\":9}",
        "{\"type\":\"ACTIVATE_SWITCH\",\"switch\":\"1\",\"bank\":1,\"slot\":1}", "{\"type\":\"ACTIVATE_SWITCH\"}",
        "{\"type\":\"MORPH_CONTROL\"}", "{\"type\":\"SET_MIDI_MONITOR\",\"on\":false}",
        "{\"type\":\"CREATE_PROFILE\",\"profile_id\":\"BAD ID\"}", "{\"type\":\"RENAME_PROFILE\",\"profile_id\":\"BAD ID\"}",
        "{\"type\":\"SWITCH_PROFILE\",\"profile_id\":\"test\"}", "{\"type\":\"DELETE_PROFILE\",\"profile_id\":\"BAD ID\"}",
        "{\"type\":\"SET_HARDWARE\",\"model\":\"unknown\"}", "{\"type\":\"PUT_LOGO\"}",
        "{\"type\":\"COMMIT_LOGO\"}", "{\"type\":\"DELETE_LOGO\"}",
        "{\"type\":\"PUT_GLOBAL\",\"profile\":\"missing\",\"device\":{}}"
    };
    for (unsigned i = 0; i < sizeof changing / sizeof *changing; ++i) {
        bool woke = wakes(changing[i]);
        if (!woke) fprintf(stderr, "Did not wake on %s\n", changing[i]);
        assert(woke);
    }
    protocol.logo_preview = false;
    assert(bosun_config_put_device(&config, NULL, "{}", 2) == BOSUN_STORE_OK);
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
    int head = bosun_json_get(&reply, plugins, "kemper_head");
    assert(head >= 0);
    int head_messages = bosun_json_get(&reply, head, "messages");
    int fixed = bosun_json_get(&reply, head_messages, "kemper_fixed_toggle");
    assert(fixed >= 0);
    int requires = bosun_json_get(&reply, fixed, "requires");
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, requires, "generation"), "MK2"));
    assert(bosun_json_get(&reply, head_messages, "kemper_browse_rig") >= 0);
    assert(bosun_json_get(&reply, head_messages, "kemper_morph") >= 0);
    assert(bosun_json_get(&reply, plugins, "generic_midi") >= 0);
    assert(bosun_json_get(&reply, plugins, "headrush_core") < 0);
    request("{\"type\":\"CREATE_PROFILE\",\"profile_id\":\"head\",\"name\":\"Head\",\"kind\":\"kemper_head\"}", "ACK");
    request("{\"type\":\"SWITCH_PROFILE\",\"profile_id\":\"head\"}", "ACK");
    assert(!strcmp(config.kind, "kemper_head"));
    request("{\"type\":\"CREATE_PROFILE\",\"profile_id\":\"test\",\"name\":\"Test\",\"kind\":\"generic_midi\"}", "ACK");
    request("{\"type\":\"SWITCH_PROFILE\",\"profile_id\":\"test\"}", "ACK");
    request("{\"type\":\"DELETE_PROFILE\",\"profile_id\":\"head\"}", "ACK");
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
    /* MIDI Learn was retired; its commands are unknown like any other. */
    request("{\"type\":\"GET_MIDI_LEARN\"}", "ERROR"); is_error("unknown_type");
    request("{\"type\":\"START_MIDI_LEARN\"}", "ERROR"); is_error("unknown_type");
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
    assert(!protocol.reboot_requested && !runtime.midi_monitor);
    assert(!protocol.tx_length && !protocol.rx_length);
    request("{\"type\":\"PING\"}", "ACK");
    assert(sent == 0);
    full_catalog();
    monitor_events(); rig_info(); ui_events(); morph_control(); activate_switch(); update_lock();
    active_patch_profile_and_draft();
    kemper_identity(); draft_limit(); startup_logo(); screensaver_protocol();
    const char *stats = "{\"type\":\"STATS\"}\n";
    assert(bosun_protocol_feed(&protocol, (const uint8_t *)stats, strlen(stats), UINT32_MAX) == strlen(stats));
    read_reply("STATS", NULL); assert(strstr(output, "\"uptime_ms\":4294967295"));
    const char *broken = "{broken";
    assert(bosun_store_write_atomic("/config/profiles/test/patches/01/02.json", broken, strlen(broken)) == BOSUN_STORE_OK);
    request("{\"type\":\"GET_PATCH\",\"bank\":1,\"slot\":2}", "ERROR"); is_error("invalid_request");
    assert(!bosun_store_mount("/no-such-native-storage-root"));
    request("{\"type\":\"GET_PATCH\",\"bank\":1,\"slot\":2}", "ERROR"); is_error("storage_unavailable");
    boot_logo_is("default");
    request("{\"type\":\"DELETE_LOGO\"}", "ERROR"); is_error("storage_unavailable");
    put_logo(0, logo_image, 3, "ERROR"); is_error("storage_unavailable");
    request("{\"type\":\"REBOOT\",\"mode\":\"bootloader\"}", "ACK");
    assert(protocol.reboot_requested && protocol.reboot_bootloader);
    puts("protocol: CRUD, manifest, exact bounds, storage failures, MIDI/UI events, auto-save/discard, binding callbacks, external rigs, backpressure, timeout/reboot/reconnect, startup logo upload, update lock, screensaver passed");
    return 0;
}
