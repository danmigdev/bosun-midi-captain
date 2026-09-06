#define _POSIX_C_SOURCE 200809L
#include "bosun/runtime.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static bosun_config_t config;
static bosun_runtime_t runtime;
static struct { uint8_t data[256]; size_t length; } packets[2048];
static size_t sent, monitored;
static bool fail_send;
static struct { bool outbound; uint8_t port, channel, status, data[256]; size_t length; } last_monitor;

static bool send_midi(void *context, const uint8_t *data, size_t length) {
    assert(context == &sent && sent < 2048 && length <= sizeof packets[0].data);
    memcpy(packets[sent].data, data, length); packets[sent++].length = length;
    return !fail_send;
}
static void monitor(void *context, bool outbound, uint8_t port, uint8_t channel,
                    uint8_t status, const uint8_t *data, size_t length) {
    assert(context == &monitored && length <= sizeof last_monitor.data);
    ++monitored; last_monitor.outbound = outbound; last_monitor.port = port;
    last_monitor.channel = channel; last_monitor.status = status; last_monitor.length = length;
    memcpy(last_monitor.data, data, length);
}
static void patch(unsigned bank, unsigned slot, const char *json) {
    assert(bosun_config_put_patch(&config, "test", bank, slot, json, strlen(json), 0) == BOSUN_STORE_OK);
}
static void device(const char *json) {
    assert(bosun_config_put_device(&config, NULL, json, strlen(json)) == BOSUN_STORE_OK);
    bosun_runtime_config_changed(&runtime);
}
static void fixture(const char *device_json, const char *patch_json) {
    assert(bosun_store_format() == BOSUN_STORE_OK);
    assert(bosun_config_init(&config) == BOSUN_STORE_OK);
    assert(bosun_config_create("test", "Test", "generic", NULL) == BOSUN_STORE_OK);
    assert(bosun_config_activate(&config, "test", false) == BOSUN_STORE_OK);
    patch(1, 1, patch_json);
    assert(bosun_config_select(&config, 1, 1) == BOSUN_STORE_OK);
    if (device_json) assert(bosun_config_put_device(&config, NULL, device_json, strlen(device_json)) == BOSUN_STORE_OK);
    sent = monitored = 0; fail_send = false;
    bosun_runtime_init(&runtime, &config, send_midi, &sent);
    assert(sent == 0 && runtime.queue_count == 0);
}
static bool submit(const char *json, bool action) {
    bosun_json_token_t tokens[1024]; bosun_json_doc_t doc;
    assert(bosun_json_parse(&doc, json, strlen(json), tokens, 1024) == BOSUN_JSON_OK);
    return action ? bosun_runtime_action(&runtime, &doc, 0) : bosun_runtime_dispatch(&runtime, &doc, 0);
}
static void tick(uint32_t now, uint16_t pressed) { bosun_runtime_tick(&runtime, now, pressed, 0, 0); }
static void edge(uint32_t now, uint16_t pressed) { tick(now, pressed); tick(now + 5, pressed); }
static void expect(size_t index, uint8_t status, uint8_t a, uint8_t b, size_t length) {
    assert(index < sent && packets[index].length == length);
    assert(packets[index].data[0] == status && packets[index].data[1] == a);
    if (length == 3) assert(packets[index].data[2] == b);
}

static void test_queue(void) {
    fixture(NULL, "{}");
    const char *bad[] = {"{}", "[]", "{\"type\":\"cc\",\"channel\":0}",
        "{\"type\":\"cc\",\"channel\":17}", "{\"type\":\"cc\",\"cc\":128}",
        "{\"type\":\"cc\",\"value\":-1}", "{\"type\":\"delay\",\"ms\":60001}",
        "{\"type\":\"cc\",\"value\":1.5}", "{\"type\":\"pc\",\"program\":\"3\"}",
        "{\"type\":\"cc_toggle\",\"state\":\"maybe\"}",
        "{\"type\":\"captain_patch\",\"bank\":100}", "{\"type\":\"captain_patch\",\"slot\":11}",
        "{\"type\":\"captain_preview_step\",\"scope\":\"unknown\"}"};
    for (size_t i = 0; i < sizeof bad / sizeof *bad; ++i) assert(!submit(bad[i], false));
    assert(!submit("{\"type\":\"kemper_tuner\"}", false));
    assert(!submit("{\"type\":\"unknown\"}", false));
    assert(runtime.invalid_messages == sizeof bad / sizeof *bad && runtime.unsupported_messages == 2);
    assert(!submit("{\"messages\":[{\"type\":\"cc\"},{\"type\":\"unknown\"}]}", true));
    assert(!submit("{\"messages\":{}}", true));
    assert(runtime.queue_count == 0 && sent == 0);
    assert(submit("{\"messages\":[{\"type\":\"cc\",\"channel\":16,\"cc\":7,\"value\":127},{\"type\":\"pc\",\"program\":5},{\"type\":\"note_on\",\"note\":60},{\"type\":\"note_off\",\"note\":60},{\"type\":\"cc_toggle\",\"state\":\"off\",\"cc\":4,\"off_value\":9},{\"type\":\"program_change_bank\",\"channel\":2,\"msb\":3,\"lsb\":4,\"program\":5}]}", true));
    assert(sent == 0); tick(0, 0);
    expect(0, 0xbf, 7, 127, 3); expect(1, 0xc0, 5, 0, 2);
    expect(2, 0x90, 60, 100, 3); expect(3, 0x80, 60, 64, 3);
    expect(4, 0xb0, 4, 9, 3); expect(5, 0xb1, 0, 3, 3);
    expect(6, 0xb1, 32, 4, 3); expect(7, 0xc1, 5, 0, 2);
    assert(sent == 8 && runtime.queue_count == 0);

    for (unsigned i = 0; i < BOSUN_RUNTIME_COMMANDS - 1; ++i) assert(submit("{\"type\":\"cc\"}", false));
    assert(!submit("{\"messages\":[{\"type\":\"cc\"},{\"type\":\"cc\"}]}", true));
    assert(runtime.queue_count == BOSUN_RUNTIME_COMMANDS - 1 && runtime.queue_overflows == 1);
    assert(submit("{\"type\":\"cc\"}", false));
    assert(!submit("{\"type\":\"cc\"}", false));
    for (unsigned i = 0; i < 4; ++i) {
        size_t before = sent; tick(1 + i, 0);
        assert(sent == before + BOSUN_RUNTIME_COMMANDS_PER_TICK);
    }
    assert(runtime.queue_count == 0);
    fail_send = true; assert(submit("{\"type\":\"cc\"}", false)); tick(10, 0);
    assert(runtime.midi_tx_failed == 1 && runtime.last_error == BOSUN_STORE_IO);

    fixture(NULL, "{}");
    assert(submit("{\"messages\":[{\"type\":\"cc\",\"cc\":1},{\"type\":\"delay\",\"ms\":10},{\"type\":\"cc\",\"cc\":2}]}", true));
    tick(UINT32_MAX - 4, 0); assert(sent == 1 && runtime.waiting && runtime.queue_count == 1);
    tick(4, 0); assert(sent == 1); tick(5, 0); assert(sent == 2 && !runtime.waiting);
    expect(1, 0xb0, 2, 0, 3);
}

static void test_patch_macros(void) {
    fixture(NULL, "{\"name\":\"One\",\"on_exit\":{\"messages\":[{\"type\":\"cc\",\"cc\":1}]},\"bindings\":[{\"switch\":\"1\",\"actions\":{\"press\":{\"messages\":[{\"type\":\"captain_patch\",\"bank\":1,\"slot\":2},{\"type\":\"cc\",\"cc\":3}]}}}]}");
    patch(1, 2, "{\"name\":\"Two\",\"on_enter\":{\"messages\":[{\"type\":\"cc\",\"cc\":2}]}}");
    edge(0, 1);
    assert(config.slot == 2 && runtime.queue_count == 0 && sent == 3);
    expect(0, 0xb0, 1, 0, 3); expect(1, 0xb0, 2, 0, 3); expect(2, 0xb0, 3, 0, 3);
    tick(1000, 1); assert(sent == 3); edge(1001, 0); assert(sent == 3);
    assert(bosun_runtime_switch_patch(&runtime, 1, 2, true) == BOSUN_STORE_OK);
    tick(1010, 0); assert(sent == 4); expect(3, 0xb0, 2, 0, 3);
    assert(bosun_runtime_switch_patch(&runtime, 1, 1, false) == BOSUN_STORE_OK);
    tick(1020, 0); assert(sent == 4);
    patch(1, 3, "{\"name\":\"Bad\",\"on_enter\":{\"messages\":[{\"type\":\"cc\"},{\"type\":\"not-supported\"}]}}");
    uint32_t revision = config.revision;
    assert(bosun_runtime_switch_patch(&runtime, 1, 3, true) == BOSUN_STORE_INVALID);
    assert(config.slot == 1 && config.revision == revision && runtime.queue_count == 0 && sent == 4);
    for (unsigned i = 0; i < BOSUN_RUNTIME_COMMANDS - 1; ++i) assert(submit("{\"type\":\"cc\"}", false));
    assert(bosun_runtime_switch_patch(&runtime, 1, 2, true) != BOSUN_STORE_OK);
    assert(config.slot == 1 && config.revision == revision && runtime.queue_count == BOSUN_RUNTIME_COMMANDS - 1);
    assert(bosun_runtime_switch_patch(&runtime, 99, 10, true) == BOSUN_STORE_NOT_FOUND);
    /* Recursive user macros remain cooperative and cannot monopolize a tick. */
    fixture(NULL, "{\"on_enter\":{\"messages\":[{\"type\":\"captain_patch\"}]}}");
    assert(bosun_runtime_switch_patch(&runtime, 1, 1, true) == BOSUN_STORE_OK);
    revision = config.revision; tick(0, 0);
    assert(runtime.queue_count == 1 && config.revision == revision + BOSUN_RUNTIME_COMMANDS_PER_TICK && sent == 0);
}

static void test_bindings(void) {
    fixture("{\"long_press_ms\":30,\"auto_momentary_ms\":20,\"double_tap_window_ms\":30,\"long_press_actions\":{\"1\":[{\"type\":\"cc\",\"cc\":9}],\"2\":[{\"type\":\"cc\",\"cc\":99}],\"4\":[{\"type\":\"cc\",\"cc\":8}]},\"preset_navigation\":{\"switches\":{\"4\":2,\"up\":3}}}",
        "{\"bindings\":[{\"switch\":\"1\",\"actions\":{\"press\":{\"messages\":[{\"type\":\"cc\",\"cc\":1}]}}},{\"switch\":\"2\",\"mode\":\"latched\",\"actions\":{\"toggle_on\":{\"messages\":[{\"type\":\"cc\",\"cc\":2,\"value\":127}]},\"toggle_off\":{\"messages\":[{\"type\":\"cc\",\"cc\":2,\"value\":0}]}}},{\"switch\":\"3\",\"mode\":\"momentary\",\"actions\":{\"press\":{\"messages\":[{\"type\":\"note_on\",\"note\":60}]},\"release\":{\"messages\":[{\"type\":\"note_off\",\"note\":60}]}}},{\"switch\":\"A\",\"mode\":\"double_tap\",\"actions\":{\"press\":{\"messages\":[{\"type\":\"cc\",\"cc\":5}]},\"double_tap\":{\"messages\":[{\"type\":\"cc\",\"cc\":6}]}}}]}");
    patch(1, 2, "{}"); bosun_runtime_config_changed(&runtime);
    assert(runtime.bindings[0].mode == BOSUN_SWITCH_LONG_PRESS_ALT);
    assert(runtime.bindings[1].mode == BOSUN_SWITCH_LATCHED);
    assert(runtime.bindings[3].preset_slot == 2 && runtime.bindings[4].preset_slot == 0);
    edge(0, 1); assert(sent == 0); edge(10, 0); expect(0, 0xb0, 1, 0, 3);
    edge(20, 1); tick(55, 1); expect(1, 0xb0, 9, 0, 3);
    tick(90, 1); edge(91, 0); assert(sent == 2);
    edge(100, 2); assert(runtime.switches[1].latched_on); expect(2, 0xb0, 2, 127, 3);
    tick(125, 2); assert(runtime.held_mask == 2); edge(126, 0);
    assert(!runtime.switches[1].latched_on && runtime.held_mask == 0); expect(3, 0xb0, 2, 0, 3);
    edge(140, 4); expect(4, 0x90, 60, 100, 3); edge(150, 0); expect(5, 0x80, 60, 64, 3);
    edge(160, 32); edge(168, 0); edge(177, 32); expect(6, 0xb0, 6, 0, 3);
    edge(184, 0); tick(220, 0); assert(sent == 7);
    edge(230, 32); edge(240, 0); tick(266, 0); expect(7, 0xb0, 5, 0, 3);
    edge(280, 8); tick(315, 8); expect(8, 0xb0, 8, 0, 3);
    assert(config.slot == 1); edge(320, 0); edge(340, 8); edge(350, 0); assert(config.slot == 2);
}

static void test_navigation_context(void) {
    fixture("{\"preview\":{\"timeout_ms\":20,\"on_timeout\":\"cancel\"},\"setlist\":{\"items\":[[2,3],{\"bank\":1,\"slot\":2},[99,1],{},[1,1]]}}", "{\"name\":\"One\"}");
    patch(1, 2, "{\"name\":\"Two \\\"clean\\\"\"}"); patch(2, 1, "{\"name\":\"Three\"}"); patch(2, 3, "{\"name\":\"Four\"}");
    assert(submit("{\"type\":\"captain_preview_step\"}", false)); tick(0, 0);
    assert(config.slot == 1 && runtime.preview_active && runtime.preview_slot == 2 && sent == 0);
    char json[4096]; bosun_json_writer_t w; bosun_json_writer_init(&w, json, sizeof json);
    assert(bosun_runtime_context(&runtime, &w));
    bosun_json_token_t tokens[128]; bosun_json_doc_t doc;
    assert(bosun_json_parse(&doc, json, w.length, tokens, 128) == BOSUN_JSON_OK);
    assert(bosun_config_int(&doc, 0, "slot", 0) == 2);
    assert(bosun_json_equal(&doc, bosun_json_get(&doc, 0, "patch_name"), "Two \"clean\""));
    assert(bosun_json_equal(&doc, bosun_json_get(&doc, 0, "preview"), "on"));
    assert(bosun_json_get(&doc, 0, "kemper_connected") < 0);
    bosun_json_writer_init(&w, json, 3); assert(!bosun_runtime_context(&runtime, &w) && w.failed);
    tick(19, 0); assert(runtime.preview_active); tick(20, 0); assert(!runtime.preview_active && config.slot == 1);
    assert(submit("{\"type\":\"captain_preview_step\",\"delta\":-1}", false)); tick(21, 0);
    assert(runtime.preview_bank == 2 && runtime.preview_slot == 3);
    assert(submit("{\"type\":\"captain_preview_commit\"}", false)); tick(22, 0);
    assert(config.bank == 2 && config.slot == 3 && !runtime.preview_active);
    assert(submit("{\"type\":\"captain_preview_step\",\"scope\":\"bank\"}", false)); tick(23, 0);
    assert(runtime.preview_bank == 1 && runtime.preview_slot == 2);
    assert(submit("{\"type\":\"captain_preview_cancel\"}", false)); tick(24, 0);
    assert(!runtime.preview_active && config.bank == 2 && config.slot == 3);
    assert(submit("{\"type\":\"captain_bank_step\"}", false)); tick(25, 0);
    assert(config.bank == 1 && config.slot == 1);
    assert(submit("{\"type\":\"captain_setlist_step\"}", false)); tick(26, 0);
    assert(config.bank == 2 && config.slot == 3);
    assert(submit("{\"type\":\"captain_setlist_step\",\"delta\":-1}", false)); tick(27, 0);
    assert(config.bank == 1 && config.slot == 1 && sent == 0);
    device("{\"preview\":{\"timeout_ms\":20}}" );
    assert(submit("{\"type\":\"captain_preview_step\"}", false)); tick(UINT32_MAX - 9, 0);
    tick(9, 0); assert(runtime.preview_active); tick(10, 0);
    assert(!runtime.preview_active && config.slot == 2);
}

static void test_expression(void) {
    static const char settings[] = "{\"expression\":[{\"jack\":1,\"enabled\":true,\"calibration\":{\"min\":0,\"max\":65535},\"message\":{\"type\":\"cc\",\"cc\":11,\"channel\":2}},{\"jack\":2,\"enabled\":false}]}";
    fixture(settings, "{}");
    bosun_runtime_tick(&runtime, 0, 0, 0, 65535);
    assert(sent == 0 && !runtime.expression[0].armed && runtime.expression[0].value == 0);
    bosun_runtime_tick(&runtime, 9, 0, 65535, 0); assert(runtime.expression[0].raw == 0 && sent == 0);
    bosun_runtime_tick(&runtime, 10, 0, 65535, 0);
    assert(runtime.expression[0].armed && runtime.expression[0].value == 32); expect(0, 0xb1, 11, 32, 3);
    bosun_runtime_expression_present(&runtime, 1, false);
    bosun_runtime_tick(&runtime, 20, 0, 65535, 0); assert(sent == 1 && runtime.expression[0].value == 56);
    bosun_runtime_expression_t previous = runtime.expression[0];
    device(settings);
    assert(runtime.expression[0].armed && !runtime.expression[0].present && runtime.expression[0].smooth == previous.smooth);
    assert(runtime.expression[0].value == previous.value && sent == 1);
    bosun_runtime_expression_present(&runtime, 1, true);
    bosun_runtime_tick(&runtime, 30, 0, 65535, 0); expect(1, 0xb1, 11, 73, 3);
    patch(1, 2, "{\"expression\":[{\"jack\":1,\"invert\":true,\"message\":{\"type\":\"cc\",\"cc\":7}}]}");
    assert(bosun_runtime_switch_patch(&runtime, 1, 2, false) == BOSUN_STORE_OK);
    assert(!runtime.expression[0].armed && runtime.expression[0].value == -1);
    bosun_runtime_tick(&runtime, 40, 0, 65535, 0); assert(sent == 2 && runtime.expression[0].value == 0);
    bosun_runtime_tick(&runtime, 50, 0, 0, 0); expect(2, 0xb0, 7, 32, 3);
    device("{\"expression\":[{\"jack\":1,\"enabled\":true,\"message\":{\"type\":\"pc\"}}]}");
    /* The active patch override is still a supported CC template. */
    assert(runtime.expression[0].enabled);
    assert(bosun_runtime_switch_patch(&runtime, 1, 1, false) == BOSUN_STORE_OK);
    assert(!runtime.expression[0].enabled);
    fixture("{\"expression\":[{\"jack\":1,\"enabled\":true,\"curve\":\"log\",\"calibration\":{\"min\":0,\"max\":65535},\"message\":{\"type\":\"cc\"}},{\"jack\":2,\"enabled\":true,\"curve\":\"exp\",\"calibration\":{\"min\":0,\"max\":65535},\"message\":{\"type\":\"cc\"}}]}", "{}");
    bosun_runtime_tick(&runtime, 0, 0, 16384, 32768);
    assert(runtime.expression[0].value == 64 && runtime.expression[1].value == 32 && sent == 0);
}

static void test_midi(void) {
    fixture(NULL, "{}"); runtime.midi_monitor = runtime.midi_learn = true;
    runtime.monitor = monitor; runtime.monitor_context = &monitored;
    const uint8_t partial[] = {0xb2, 7}, din[] = {0x94, 60, 100}, finish[] = {99};
    bosun_runtime_feed_midi(&runtime, 0, partial, sizeof partial, 0);
    bosun_runtime_feed_midi(&runtime, 1, din, sizeof din, 1);
    assert(runtime.learn.sequence == 1 && runtime.learn.port == 1 && runtime.learn.channel == 5);
    bosun_runtime_feed_midi(&runtime, 0, finish, sizeof finish, 2);
    assert(runtime.midi_rx_count == 2 && monitored == 2 && runtime.learn.channel == 3);
    assert(runtime.learn.data[0] == 7 && runtime.learn.data[1] == 99 && runtime.learn.fresh);
    assert(!last_monitor.outbound && last_monitor.port == 0 && last_monitor.status == 0xb0);
    const uint8_t realtime[] = {0xf8};
    bosun_runtime_feed_midi(&runtime, 1, realtime, 1, 3); assert(runtime.learn.sequence == 2);
    const uint8_t running[] = {8, 1};
    bosun_runtime_feed_midi(&runtime, 0, running, sizeof running, 4); assert(runtime.learn.sequence == 3);
    bosun_runtime_feed_midi(&runtime, 0, partial, sizeof partial, 5);
    bosun_runtime_reset_midi_input(&runtime, 0);
    bosun_runtime_feed_midi(&runtime, 0, finish, sizeof finish, 6);
    bosun_runtime_feed_midi(&runtime, 0, running, sizeof running, 7); assert(runtime.learn.sequence == 3);
    bosun_runtime_feed_midi(&runtime, 1, running, sizeof running, 8); assert(runtime.learn.sequence == 4 && runtime.learn.port == 1);
    bosun_runtime_feed_midi(&runtime, 2, din, sizeof din, 9);
    bosun_runtime_feed_midi(&runtime, 0, NULL, 3, 9); assert(runtime.learn.sequence == 4);
    assert(submit("{\"type\":\"cc\",\"channel\":2,\"cc\":7,\"value\":1}", false)); tick(10, 0);
    assert(last_monitor.outbound && last_monitor.channel == 0 && last_monitor.status == 0 && last_monitor.length == 3);
    assert(last_monitor.data[0] == 0xb1 && runtime.learn.sequence == 4);
    runtime.midi_monitor = runtime.midi_learn = false; size_t before = monitored;
    bosun_runtime_feed_midi(&runtime, 1, din, sizeof din, 11);
    assert(monitored == before && runtime.learn.sequence == 4);
    runtime.midi_learn = true;
    bosun_runtime_feed_midi(&runtime, 1, din, sizeof din, 12);
    assert(monitored == before + 1 && runtime.learn.sequence == 5 && !last_monitor.outbound);
}

static void midi_param(uint8_t page, uint8_t address, uint16_t value, uint32_t now) {
    const uint8_t bytes[] = {0xf0,0,0x20,0x33,2,127,1,0,page,address,
        (uint8_t)(value >> 7), (uint8_t)(value & 127),0xf7};
    bosun_runtime_feed_midi(&runtime, 0, bytes, sizeof bytes, now);
}
static bosun_json_doc_t context_doc(char *json, size_t capacity, bosun_json_token_t *tokens) {
    bosun_json_writer_t w; bosun_json_writer_init(&w, json, capacity);
    assert(bosun_runtime_context(&runtime, &w));
    bosun_json_doc_t doc; assert(bosun_json_parse(&doc, json, w.length, tokens, 256) == BOSUN_JSON_OK);
    return doc;
}
static void test_kemper_context_and_follow(void) {
    fixture("{\"kemper\":{},\"midi_channel\":3}",
        "{\"name\":\"Local\",\"bindings\":[{\"switch\":\"1\",\"mode\":\"latched\",\"actions\":{\"toggle_on\":{\"messages\":[{\"type\":\"kemper_effect_toggle\",\"slot\":\"Reverb\"},{\"type\":\"kemper_effect_toggle\",\"slot\":\"A\"}]}}}]}");
    patch(1, 2, "{\"name\":\"Second\",\"on_enter\":{\"messages\":[{\"type\":\"cc\",\"cc\":99}]}}");
    assert(runtime.kemper_enabled && runtime.kemper.bound_blocks == 0x81 && runtime.bindings[0].mirror_block == BOSUN_KEMPER_REVERB);
    char json[4096]; bosun_json_token_t tokens[256]; bosun_json_doc_t doc = context_doc(json, sizeof json, tokens);
    assert(bosun_config_int(&doc, 0, "kemper_bank", 0) == 1 && bosun_config_int(&doc, 0, "kemper_rig_in_bank", 0) == 1);
    assert(bosun_json_get(&doc, 0, "kemper_block_A") < 0 && bosun_json_get(&doc, 0, "kemper_rig_name") < 0);
    const uint8_t effects[] = {0xb2,29,127,17,0};
    bosun_runtime_feed_midi(&runtime, 0, effects, sizeof effects, 1);
    assert(runtime.switches[0].latched_on); /* The first declared block owns the LED. */
    doc = context_doc(json, sizeof json, tokens);
    assert(bosun_json_equal(&doc, bosun_json_get(&doc, 0, "kemper_block_A"), "off"));
    assert(bosun_json_equal(&doc, bosun_json_get(&doc, 0, "kemper_block_Reverb"), "on"));
    assert(bosun_json_get(&doc, 0, "kemper_block_B") < 0);
    midi_param(127, 126, 1, 2); midi_param(125, 84, 9, 3); midi_param(124, 15, 8200, 4);
    doc = context_doc(json, sizeof json, tokens);
    assert(bosun_json_equal(&doc, bosun_json_get(&doc, 0, "kemper_tuner"), "on"));
    assert(bosun_json_equal(&doc, bosun_json_get(&doc, 0, "kemper_tuner_note"), "A"));
    assert(bosun_config_int(&doc, 0, "kemper_tuner_deviance", 0) == 8200);
    assert(bosun_config_int(&doc, 0, "tuner_deviance", 0) == 8200);
    edge(10, 2); assert(!runtime.kemper.state.tuner_active);
    bool exited = false;
    for (size_t i = 0; i < sent; ++i) if (packets[i].length == 3 && packets[i].data[0] == 0xb2 &&
        packets[i].data[1] == 31 && packets[i].data[2] == 0) exited = true;
    assert(exited);
    const uint8_t wrong_channel[] = {0xc1,1};
    bosun_runtime_feed_midi(&runtime, 0, wrong_channel, sizeof wrong_channel, 20); assert(config.slot == 1);
    size_t before = sent; const uint8_t external_pc[] = {0xc2,1};
    bosun_runtime_feed_midi(&runtime, 0, external_pc, sizeof external_pc, 21);
    assert(config.slot == 2 && runtime.kemper.state.rig == 2 && runtime.queue_count == 0 && sent == before);
    doc = context_doc(json, sizeof json, tokens);
    assert(bosun_config_int(&doc, 0, "kemper_rig", 0) == 2);
    assert(bosun_json_equal(&doc, bosun_json_get(&doc, 0, "patch_name"), "Second"));
    assert(bosun_json_get(&doc, 0, "kemper_rig_name") < 0);
    /* Full context never exposes a retired name or invents unknown block values. */
    strcpy(runtime.kemper.state.rig_name, "Retired"); runtime.kemper.state.rig_name_fresh = false;
    doc = context_doc(json, sizeof json, tokens); assert(bosun_json_get(&doc, 0, "kemper_rig_name") < 0);
    runtime.kemper.state.rig_name_fresh = true;
    doc = context_doc(json, sizeof json, tokens);
    assert(bosun_json_equal(&doc, bosun_json_get(&doc, 0, "patch_name"), "Retired"));
    assert(submit("{\"type\":\"captain_preview_step\"}", false)); tick(22, 0);
    doc = context_doc(json, sizeof json, tokens);
    assert(bosun_config_int(&doc, 0, "kemper_rig", 0) == 1);
    assert(bosun_json_equal(&doc, bosun_json_get(&doc, 0, "kemper_rig_name"), "Local"));
    assert(submit("{\"messages\":[{\"type\":\"delay\",\"ms\":100},{\"type\":\"cc\",\"cc\":99}]}", true)); tick(23, 0);
    assert(runtime.waiting && runtime.queue_count == 1);
    assert(bosun_config_create("other", "Other", "generic", NULL) == BOSUN_STORE_OK);
    assert(bosun_config_activate(&config, "other", false) == BOSUN_STORE_OK); before = sent;
    tick(200, 0);
    assert(!runtime.kemper_enabled && !runtime.waiting && !runtime.preview_active && runtime.queue_count == 0 && sent == before);
    doc = context_doc(json, sizeof json, tokens);
    assert(bosun_json_get(&doc, 0, "kemper_tuner") < 0 && bosun_json_get(&doc, 0, "kemper_block_A") < 0);
    fixture("{\"auto_momentary_ms\":10}", "{\"bindings\":[{\"switch\":\"1\",\"mode\":\"latched\",\"label\":\"Fallback\",\"hold_text\":\"Harmonizer\"}]}");
    edge(0, 1); tick(15, 1); doc = context_doc(json, sizeof json, tokens);
    assert(bosun_json_equal(&doc, bosun_json_get(&doc, 0, "hold_effect"), "Harmonizer"));
    edge(20, 0); doc = context_doc(json, sizeof json, tokens);
    assert(bosun_json_equal(&doc, bosun_json_get(&doc, 0, "hold_effect"), ""));
}

static void test_kemper_bank_snapshot_follow(void) {
    fixture("{\"kemper\":{},\"midi_channel\":1}", "{\"name\":\"ACOUSTIC\"}");
    patch(1, 2, "{\"name\":\"CLEAN\",\"on_enter\":{\"messages\":[{\"type\":\"kemper_rig\",\"bank\":1,\"rig\":2}]}}");
    patch(1, 3, "{\"name\":\"CRUNCH\"}");
    patch(1, 4, "{\"name\":\"HEAVY\"}");
    patch(2, 4, "{\"name\":\"HEAVY\",\"on_enter\":{\"messages\":[{\"type\":\"kemper_rig\",\"bank\":2,\"rig\":4}]}}");
    assert(!bosun_config_has_patch(&config, 2, 3));
    const uint8_t initial[] = {0xc0,2};
    const uint8_t header[] = {0xf0,0,0x20,0x33,0,0,7,0,0,0,1,0,0,'B','a','n','k',0,0xf7};
    const uint8_t to_second[] = {0xc0,7,0xf0,0,0x20,0x33,0,0,3,0,0,1,'H','E','A','V','Y',0,0xf7,0xc0,8};
    const uint8_t to_first[] = {0xc0,3,0xf0,0,0x20,0x33,0,0,3,0,0,1,'C','L','E','A','N',0,0xf7,0xc0,1};
    bosun_runtime_feed_midi(&runtime, 0, initial, sizeof initial, 0);
    assert(config.bank == 1 && config.slot == 3);
    assert(bosun_runtime_switch_patch(&runtime, 2, 4, true) == BOSUN_STORE_OK);
    tick(100, 0); tick(105, 0);
    bosun_runtime_feed_midi(&runtime, 0, header, sizeof header, 110);
    /* Split at the actual intermediate PC, before the name and final PC. */
    bosun_runtime_feed_midi(&runtime, 0, to_second, 2, 111);
    assert(config.bank == 2 && config.slot == 4 && runtime.kemper.state.rig == 9);
    assert(!runtime.storage_errors);
    bosun_runtime_feed_midi(&runtime, 0, to_second + 2, sizeof to_second - 2, 120);
    tick(600, 0);
    assert(runtime.kemper.state.rig_name_fresh && runtime.kemper.last_name_rig == 9);
    assert(!runtime.storage_errors && config.bank == 2 && config.slot == 4);

    assert(bosun_runtime_switch_patch(&runtime, 1, 2, true) == BOSUN_STORE_OK);
    tick(700, 0); tick(705, 0);
    bosun_runtime_feed_midi(&runtime, 0, header, sizeof header, 710);
    bosun_runtime_feed_midi(&runtime, 0, to_first, 2, 711);
    assert(config.bank == 1 && config.slot == 2 && runtime.kemper.state.rig == 2);
    bosun_runtime_feed_midi(&runtime, 0, to_first + 2, sizeof to_first - 2, 720);
    tick(1200, 0);
    assert(runtime.kemper.state.rig_name_fresh && runtime.kemper.last_name_rig == 2);
    assert(!strcmp(runtime.kemper.last_name, "CLEAN") && !runtime.storage_errors);

    assert(bosun_runtime_switch_patch(&runtime, 1, 2, true) == BOSUN_STORE_OK);
    tick(2000, 0); tick(2005, 0);
    bosun_runtime_feed_midi(&runtime, 0, header, sizeof header, 2010);
    bosun_runtime_feed_midi(&runtime, 0, to_first, 2, 2011);
    tick(4509, 0);
    assert(config.slot == 2);
    tick(4510, 0); /* Without a final PC, tick follows the deferred physical rig. */
    assert(config.bank == 1 && config.slot == 4 && runtime.kemper.state.rig == 4);
    assert(!runtime.storage_errors && !runtime.queue_count && !runtime.kemper.state.rig_name_fresh);
    bosun_runtime_feed_midi(&runtime, 0, to_first + sizeof to_first - 2, 2, 4520);
    assert(config.bank == 1 && config.slot == 2 && runtime.kemper.state.rig == 2);
    assert(!runtime.storage_errors && !runtime.kemper.bank_snapshot_fallback);
}

static void test_kemper_slow_bank_snapshot(void) {
    const uint32_t delays[] = {1100, 2400};
    for (unsigned i = 0; i < sizeof delays / sizeof *delays; ++i) {
        fixture("{\"kemper\":{},\"midi_channel\":1}", "{\"name\":\"ACOUSTIC\"}");
        patch(2, 4, "{\"name\":\"HEAVY\",\"on_enter\":{\"messages\":[{\"type\":\"kemper_rig\",\"bank\":2,\"rig\":4}]}}");
        assert(!bosun_config_has_patch(&config, 2, 1));
        const uint8_t header[] = {0xf0,0,0x20,0x33,0,0,7,0,0,0,1,0,0,'B','a','n','k',0,0xf7};
        const uint8_t interim[] = {0xc0,5,0xf0,0,0x20,0x33,0,0,3,0,0,1,'H','E','A','V','Y',0,0xf7};
        const uint8_t final[] = {0xc0,8};
        assert(bosun_runtime_switch_patch(&runtime, 2, 4, true) == BOSUN_STORE_OK);
        tick(100, 0); tick(105, 0);
        bosun_runtime_feed_midi(&runtime, 0, header, sizeof header, 110);
        bosun_runtime_feed_midi(&runtime, 0, interim, sizeof interim, 111);
        tick(110 + delays[i] - 1, 0);
        assert(config.bank == 2 && config.slot == 4 && runtime.kemper.state.rig == 9);
        assert(!runtime.storage_errors && !runtime.kemper.state.rig_name_fresh);
        bosun_runtime_feed_midi(&runtime, 0, final, sizeof final, 110 + delays[i]);
        tick(160 + delays[i], 0);
        assert(config.bank == 2 && config.slot == 4 && runtime.kemper.last_name_rig == 9);
        assert(runtime.kemper.state.rig_name_fresh && !strcmp(runtime.kemper.last_name, "HEAVY"));
        assert(!runtime.storage_errors);
    }
}

int main(void) {
    char root[] = "/tmp/bosun-runtime-XXXXXX";
    assert(mkdtemp(root) && bosun_store_mount(root));
    test_queue(); test_patch_macros(); test_bindings(); test_navigation_context(); test_expression(); test_midi();
    test_kemper_context_and_follow();
    test_kemper_bank_snapshot_follow();
    test_kemper_slow_bank_snapshot();
    assert(bosun_store_format() == BOSUN_STORE_OK && rmdir(root) == 0);
    puts("Runtime: atomic queue/patch macros, delays and rollover, switch bindings, preview/setlist, expression and MIDI monitor/learn/reconnect passed");
    return 0;
}
