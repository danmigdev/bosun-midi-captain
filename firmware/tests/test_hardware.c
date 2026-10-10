#define _POSIX_C_SOURCE 200809L
#include "bosun/board.h"
#include "bosun/protocol.h"
#include "bootloader_entry.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static bosun_config_t config;
static bosun_runtime_t runtime;
static bosun_protocol_t protocol;
static bosun_json_token_t reply_tokens[1024];
static bosun_json_doc_t reply;
static char output[BOSUN_PROTOCOL_TX_BYTES];
static uint8_t packets[64][3];
static size_t sent;
static uint16_t physical;

static bool send_midi(void *context, const uint8_t *data, size_t length) {
    (void)context;
    assert(sent < 64 && length <= 3);
    memcpy(packets[sent++], data, length);
    return true;
}
static uint16_t read_switches(void) { return physical; }
static uint32_t read_led(uint8_t index) { return 0x010000u * index; }

static void descriptors(void) {
    assert(bosun_hardware_count() == 2 && !bosun_hardware_at(2));
    assert(bosun_hardware_or_default(NULL) == &bosun_hardware_captain10);
    assert(bosun_hardware_find("captain10") == &bosun_hardware_captain10);
    assert(bosun_hardware_find("mini6") == &bosun_hardware_mini6);
    assert(!bosun_hardware_find("Mini6") && !bosun_hardware_find("") && !bosun_hardware_find(NULL));
    for (unsigned m = 0; m < bosun_hardware_count(); ++m) {
        const bosun_hardware_t *h = bosun_hardware_at(m);
        assert(h->switch_count && h->switch_count <= BOSUN_HARDWARE_SWITCHES_MAX);
        assert(h->row_length && h->switch_count % h->row_length == 0);
        assert(h->led_count == 3u * h->switch_count && h->led_count <= BOSUN_LED_COUNT);
        assert(h->expression_jacks <= 2);
        uint16_t bits = 0;
        for (unsigned i = 0; i < h->switch_count; ++i) {
            assert(h->board_bits[i] < BOSUN_SWITCH_COUNT && !(bits & (1u << h->board_bits[i])));
            bits |= (uint16_t)(1u << h->board_bits[i]);
            assert(bosun_hardware_switch_index(h, h->switch_names[i]) == (int)i);
            /* A switch name always means the same board pin on every model. */
            int reference = bosun_hardware_switch_index(&bosun_hardware_captain10, h->switch_names[i]);
            assert(reference >= 0 && bosun_hardware_captain10.board_bits[reference] == h->board_bits[i]);
        }
        assert(bosun_hardware_board_mask(h) == bits);
    }
    for (unsigned mask = 0; mask < 1024; ++mask)
        assert(bosun_hardware_logical_mask(&bosun_hardware_captain10, (uint16_t)mask) == mask);
    const bosun_hardware_t *mini = &bosun_hardware_mini6;
    assert(bosun_hardware_switch_index(mini, "up") < 0 && bosun_hardware_switch_index(mini, "4") < 0);
    assert(bosun_hardware_switch_index(mini, "D") < 0 && bosun_hardware_switch_index(mini, "down") < 0);
    assert(bosun_hardware_logical_mask(mini, 1u << 5) == 1u << 3);
    assert(bosun_hardware_logical_mask(mini, 1u << 7) == 1u << 5);
    assert(bosun_hardware_logical_mask(mini, (1u << 3) | (1u << 4) | (1u << 8) | (1u << 9)) == 0);
    assert(bosun_hardware_board_mask(mini) == 0x00e7u);
    assert(bosun_hardware_common_board_mask() == BOSUN_BOOTLOADER_SCAN);
}

static void write_record(const char *text) {
    assert(bosun_store_mkdir("/config") == BOSUN_STORE_OK);
    assert(bosun_store_write_atomic(BOSUN_HARDWARE_RECORD, text, strlen(text)) == BOSUN_STORE_OK);
}

static void record(void) {
    bool configured = true;
    assert(bosun_store_format() == BOSUN_STORE_OK);
    assert(bosun_hardware_load(&configured) == &bosun_hardware_captain10 && !configured);
    assert(bosun_hardware_save(&bosun_hardware_mini6) == BOSUN_STORE_OK);
    assert(bosun_hardware_load(&configured) == &bosun_hardware_mini6 && configured);
    char text[128]; size_t length;
    assert(bosun_store_read(BOSUN_HARDWARE_RECORD, text, sizeof text - 1, &length) == BOSUN_STORE_OK);
    text[length] = 0;
    assert(!strcmp(text, "{\"version\":1,\"model\":\"mini6\"}"));
    const char *invalid[] = {"", "{", "[]", "{\"model\":6}", "{\"model\":\"nano4\"}",
        "{\"model\":\"mini6\"", "{\"Model\":\"mini6\"}"};
    for (size_t i = 0; i < sizeof invalid / sizeof *invalid; ++i) {
        write_record(invalid[i]); configured = true;
        assert(bosun_hardware_load(&configured) == &bosun_hardware_captain10 && !configured);
    }
    /* Fields added by a later firmware keep the model readable. */
    write_record("{\"version\":2,\"model\":\"mini6\",\"revision\":\"B\",\"notes\":[1,2,3]}");
    assert(bosun_hardware_load(&configured) == &bosun_hardware_mini6 && configured);
    bosun_hardware_t copy = bosun_hardware_mini6;
    assert(bosun_hardware_save(&copy) == BOSUN_STORE_INVALID);
    assert(bosun_hardware_save(NULL) == BOSUN_STORE_INVALID);
    assert(bosun_hardware_load(NULL) == &bosun_hardware_mini6);
}

static void fixture(const bosun_hardware_t *hardware, const char *device_json, const char *patch_json) {
    assert(bosun_store_format() == BOSUN_STORE_OK);
    assert(bosun_config_init(&config) == BOSUN_STORE_OK);
    assert(bosun_config_create("test", "Test", "generic", NULL) == BOSUN_STORE_OK);
    assert(bosun_config_activate(&config, "test", false) == BOSUN_STORE_OK);
    assert(bosun_config_put_patch(&config, NULL, 1, 1, patch_json, strlen(patch_json), 0) == BOSUN_STORE_OK);
    assert(bosun_config_select(&config, 1, 1) == BOSUN_STORE_OK);
    if (device_json)
        assert(bosun_config_put_device(&config, NULL, device_json, strlen(device_json)) == BOSUN_STORE_OK);
    sent = 0; physical = 0;
    bosun_runtime_init(&runtime, &config, send_midi, NULL);
    assert(runtime.hardware == &bosun_hardware_captain10);
    bosun_runtime_set_hardware(&runtime, hardware);
    bosun_protocol_init(&protocol, &runtime);
    protocol.read_switches = read_switches;
    protocol.read_led = read_led;
    bosun_protocol_session(&protocol, true);
}
static void tick(uint32_t now, uint16_t pressed) { bosun_runtime_tick(&runtime, now, pressed, 40000, 40000); }
/* Long enough for the 5 ms debounce on both edges, short of any hold. */
static void press(uint32_t *now, uint16_t logical) {
    for (unsigned i = 0; i < 8; ++i) tick((*now)++, logical);
    for (unsigned i = 0; i < 8; ++i) tick((*now)++, 0);
}

static const char mini_patch[] = "{\"bindings\":["
    "{\"switch\":\"1\",\"actions\":{\"press\":{\"messages\":[{\"type\":\"cc\",\"cc\":1}]}}},"
    "{\"switch\":\"A\",\"actions\":{\"press\":{\"messages\":[{\"type\":\"cc\",\"cc\":10}]}}},"
    "{\"switch\":\"C\",\"mode\":\"latched\",\"label\":\"DLY\",\"actions\":{\"toggle_on\":{\"messages\":[{\"type\":\"cc\",\"cc\":12,\"value\":127}]}}},"
    "{\"switch\":\"up\",\"actions\":{\"press\":{\"messages\":[{\"type\":\"cc\",\"cc\":98}]}}},"
    "{\"switch\":\"down\",\"actions\":{\"press\":{\"messages\":[{\"type\":\"cc\",\"cc\":99}]}}}]}";

static void mini6_runtime(void) {
    /* Navigation and long-press maps naming absent switches stay inert. */
    fixture(&bosun_hardware_mini6,
        "{\"preset_navigation\":{\"switches\":{\"B\":1,\"up\":1}},"
        "\"long_press_actions\":{\"down\":[{\"type\":\"cc\",\"cc\":97}]},"
        "\"expression\":[{\"jack\":1,\"enabled\":true,\"calibration\":{\"min\":0,\"max\":65535},\"message\":{\"type\":\"cc\",\"cc\":11}},"
        "{\"jack\":2,\"enabled\":true,\"calibration\":{\"min\":0,\"max\":65535},\"message\":{\"type\":\"cc\",\"cc\":7}}]}",
        mini_patch);
    assert(runtime.hardware == &bosun_hardware_mini6);
    assert(runtime.bindings[0].patch_token >= 0 && runtime.bindings[3].patch_token >= 0);
    assert(runtime.bindings[4].preset_slot == 1 && runtime.bindings[5].patch_token >= 0);
    for (unsigned i = 6; i < BOSUN_RUNTIME_SWITCHES; ++i)
        assert(runtime.bindings[i].patch_token < 0 && !runtime.bindings[i].preset_slot &&
               runtime.bindings[i].global_long_token < 0 && runtime.bindings[i].mirror_block == UINT8_MAX);
    /* No jacks: configured pedals stay disabled, so their GPIOs are never probed. */
    assert(!runtime.expression[0].enabled && !runtime.expression[1].enabled);
    uint32_t now = 0;
    for (unsigned i = 0; i < 40; ++i) tick(now++, 0);
    assert(sent == 0);
    /* Logical bits above C do not exist on this model. */
    press(&now, 0xffc0u);
    assert(sent == 0);
    press(&now, 1u << 3);
    assert(sent == 1 && packets[0][0] == 0xb0 && packets[0][1] == 10);
    press(&now, 1u << 0);
    assert(sent == 2 && packets[1][1] == 1);
    press(&now, 1u << 5);
    assert(sent == 3 && packets[2][1] == 12 && packets[2][2] == 127 && runtime.switches[5].latched_on);
    char context[2048]; bosun_json_writer_t writer;
    bosun_json_writer_init(&writer, context, sizeof context);
    assert(bosun_runtime_context(&runtime, &writer));
    assert(strstr(context, "\"switches\":{\"1\":false,\"2\":false,\"3\":false,\"A\":false,\"B\":false,\"C\":true}}"));
    assert(!strstr(context, "\"up\"") && !strstr(context, "\"down\""));
    assert(bosun_runtime_activate_switch(&runtime, "up", 1, 1, NULL, now, 0) == BOSUN_INPUT_INVALID);
    assert(bosun_runtime_activate_switch(&runtime, "4", 1, 1, NULL, now, 0) == BOSUN_INPUT_INVALID);
    assert(bosun_runtime_activate_switch(&runtime, "A", 1, 1, NULL, now, 0) == BOSUN_INPUT_OK);
    for (unsigned i = 0; i < 4; ++i) tick(now++, 0);
    assert(sent == 4 && packets[3][1] == 10);

    /* The same stored patch on the 10-switch model binds up and down again. */
    fixture(NULL, NULL, mini_patch);
    assert(runtime.bindings[4].patch_token >= 0 && runtime.bindings[9].patch_token >= 0);
    now = 0;
    press(&now, 1u << 4);
    assert(sent == 1 && packets[0][1] == 98);
}

static void read_reply(const char *type) {
    size_t length; const uint8_t *bytes = bosun_protocol_output(&protocol, &length);
    assert(length && length < sizeof output && bytes[length - 1] == '\n');
    memcpy(output, bytes, length); output[length] = 0;
    assert(bosun_json_parse(&reply, output, length, reply_tokens, 1024) == BOSUN_JSON_OK);
    if (!bosun_json_equal(&reply, bosun_json_get(&reply, 0, "type"), type))
        fprintf(stderr, "Expected %s, received %s\n", type, output);
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "type"), type));
    bosun_protocol_consume_output(&protocol, length);
}
static void request(const char *json, const char *type) {
    assert(bosun_protocol_feed(&protocol, (const uint8_t *)json, strlen(json), 1) == strlen(json));
    assert(bosun_protocol_feed(&protocol, (const uint8_t *)"\n", 1, 1) == 1);
    read_reply(type);
}
static void expect_error(const char *value) {
    if (!bosun_json_equal(&reply, bosun_json_get(&reply, 0, "error"), value))
        fprintf(stderr, "Expected error %s, received %s\n", value, output);
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "error"), value));
}
static int hardware_field(const char *key) {
    return bosun_json_get(&reply, bosun_json_get(&reply, 0, "hardware"), key);
}

static void protocol_replies(void) {
    fixture(NULL, NULL, "{}");
    request("{\"type\":\"GET_DEVICE_INFO\"}", "DEVICE_INFO");
    assert(strstr(output, "\"device\":\"MIDI Captain\",\"hardware\":{\"model\":\"captain10\","
        "\"name\":\"MIDI Captain\",\"configured\":false,"
        "\"switches\":[\"1\",\"2\",\"3\",\"4\",\"up\",\"A\",\"B\",\"C\",\"D\",\"down\"],"
        "\"rows\":[[\"1\",\"2\",\"3\",\"4\",\"up\"],[\"A\",\"B\",\"C\",\"D\",\"down\"]],"
        "\"led_count\":30,\"expression_jacks\":2,\"models\":[\"captain10\",\"mini6\"]}"));
    request("{\"type\":\"LED_DUMP\"}", "LED_DUMP");
    assert(bosun_json_get(&reply, bosun_json_get(&reply, 0, "switch_indices"), "down") >= 0);
    assert(bosun_json_at(&reply, bosun_json_get(&reply, 0, "pixels"), 29) >= 0);
    assert(bosun_json_at(&reply, bosun_json_get(&reply, 0, "pixels"), 30) < 0);

    fixture(&bosun_hardware_mini6, NULL, mini_patch);
    protocol.hardware_configured = true;
    request("{\"type\":\"GET_DEVICE_INFO\"}", "DEVICE_INFO");
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "device"), "MIDI Captain Mini 6"));
    assert(bosun_json_equal(&reply, hardware_field("model"), "mini6"));
    assert(reply.tokens[hardware_field("configured")].type == BOSUN_JSON_TRUE);
    assert(strstr(output, "\"switches\":[\"1\",\"2\",\"3\",\"A\",\"B\",\"C\"],"
        "\"rows\":[[\"1\",\"2\",\"3\"],[\"A\",\"B\",\"C\"]],\"led_count\":18,\"expression_jacks\":0"));
    request("{\"type\":\"LED_DUMP\"}", "LED_DUMP");
    int pixels = bosun_json_get(&reply, 0, "pixels"), indices = bosun_json_get(&reply, 0, "switch_indices");
    assert(bosun_json_at(&reply, pixels, 17) >= 0 && bosun_json_at(&reply, pixels, 18) < 0);
    /* PySwitch Mini 6 tuples: A (9, 11, 10), B (12, 14, 13), C (15, 17, 16). */
    assert(strstr(output, "\"switch_indices\":{\"1\":[0,1,2],\"2\":[3,4,5],\"3\":[6,7,8],"
        "\"A\":[9,11,10],\"B\":[12,14,13],\"C\":[15,17,16]}"));
    assert(bosun_json_get(&reply, indices, "up") < 0);

    /* Unwired board pins never make the model busy; a wired press does. */
    physical = (1u << 3) | (1u << 4) | (1u << 8) | (1u << 9);
    request("{\"type\":\"ACTIVATE_SWITCH\",\"switch\":\"A\",\"bank\":1,\"slot\":1}", "ACK");
    for (uint32_t now = 1; now < 10; ++now) bosun_runtime_tick(&runtime, now, 0, 0, 0);
    physical = 1u << 5;
    request("{\"type\":\"ACTIVATE_SWITCH\",\"switch\":\"A\",\"bank\":1,\"slot\":1}", "ERROR");
    expect_error("busy");
    physical = 0;
    request("{\"type\":\"ACTIVATE_SWITCH\",\"switch\":\"up\",\"bank\":1,\"slot\":1}", "ERROR");
    expect_error("invalid_request");

    /* binding_fired names Mini 6 switches (logical 3 is A, 5 is C), never the
     * 10-switch names of the same indices (up, B). Lower indices drain first. */
    request("{\"type\":\"ACTIVATE_SWITCH\",\"switch\":\"C\",\"bank\":1,\"slot\":1}", "ACK");
    char names[2][8]; unsigned named = 0;
    for (unsigned attempt = 0; attempt < 16 && named < 2; ++attempt) {
        bosun_protocol_tick(&protocol, 1000 + attempt * 100);
        size_t length; const uint8_t *bytes = bosun_protocol_output(&protocol, &length);
        if (!length) continue;
        assert(length < sizeof output);
        memcpy(output, bytes, length); output[length] = 0;
        bosun_protocol_consume_output(&protocol, length);
        assert(bosun_json_parse(&reply, output, length, reply_tokens, 1024) == BOSUN_JSON_OK);
        if (bosun_json_equal(&reply, bosun_json_get(&reply, 0, "event"), "binding_fired"))
            assert(bosun_json_string(&reply, bosun_json_get(&reply, 0, "switch"), names[named++], sizeof names[0]));
    }
    assert(named == 2 && !strcmp(names[0], "A") && !strcmp(names[1], "C"));

    request("{\"type\":\"SET_HARDWARE\",\"model\":\"nano4\"}", "ERROR");
    expect_error("unsupported_hardware");
    request("{\"type\":\"SET_HARDWARE\"}", "ERROR");
    expect_error("unsupported_hardware");
    request("{\"type\":\"SET_HARDWARE\",\"model\":\"mini6\",\"model\":\"captain10\"}", "ERROR");
    expect_error("invalid_json");
    /* Confirming the running model persists it without restarting. */
    protocol.hardware_configured = false;
    request("{\"type\":\"SET_HARDWARE\",\"model\":\"mini6\"}", "ACK");
    assert(reply.tokens[bosun_json_get(&reply, 0, "reboot")].type == BOSUN_JSON_FALSE);
    assert(!protocol.reboot_requested && protocol.hardware_configured);
    bool configured = false;
    assert(bosun_hardware_load(&configured) == &bosun_hardware_mini6 && configured);
    /* A different model is persisted, then applied by a normal reboot. */
    request("{\"type\":\"SET_HARDWARE\",\"model\":\"captain10\",\"id\":7}", "ACK");
    assert(bosun_json_equal(&reply, bosun_json_get(&reply, 0, "model"), "captain10"));
    assert(reply.tokens[bosun_json_get(&reply, 0, "reboot")].type == BOSUN_JSON_TRUE);
    assert(protocol.reboot_requested && !protocol.reboot_bootloader);
    assert(bosun_hardware_load(&configured) == &bosun_hardware_captain10 && configured);
    assert(runtime.hardware == &bosun_hardware_mini6);
}

static void default_device_follows_jacks(void) {
    char text[BOSUN_DEVICE_BYTES + 1]; size_t length;
    assert(bosun_store_format() == BOSUN_STORE_OK);
    bosun_config_set_expression_jacks(bosun_hardware_mini6.expression_jacks);
    assert(bosun_config_init(&config) == BOSUN_STORE_OK);
    assert(!strstr(config.device, "\"expression\"") && config.device_doc.tokens[0].type == BOSUN_JSON_OBJECT);
    assert(bosun_config_read(&config, NULL, "device.json", text, sizeof text, &length) == BOSUN_STORE_OK);
    assert(!strstr(text, "\"expression\"") && strstr(text, "\"long_press_ms\":600"));
    assert(bosun_config_create("mini", "Mini", "generic", NULL) == BOSUN_STORE_OK);
    assert(bosun_store_read("/config/profiles/mini/device.json", text, sizeof text - 1, &length) == BOSUN_STORE_OK);
    text[length] = 0;
    assert(!strstr(text, "\"expression\"") && strstr(text, "\"tft\":{"));
    /* The 10-switch default is byte-for-byte what earlier firmware wrote. */
    bosun_config_set_expression_jacks(bosun_hardware_captain10.expression_jacks);
    assert(bosun_config_create("ten", "Ten", "generic", NULL) == BOSUN_STORE_OK);
    assert(bosun_store_read("/config/profiles/ten/device.json", text, sizeof text - 1, &length) == BOSUN_STORE_OK);
    text[length] = 0;
    assert(!strcmp(text, bosun_default_device) && strstr(text, "{\"jack\":2,"));
}

int main(void) {
    char root[] = "/tmp/bosun-hardware-XXXXXX";
    assert(mkdtemp(root) && bosun_store_mount(root));
    descriptors(); record(); mini6_runtime(); protocol_replies(); default_device_follows_jacks();
    assert(bosun_store_format() == BOSUN_STORE_OK && rmdir(root) == 0);
    puts("Hardware: model descriptors, persisted record, Mini 6 bindings/expression/context, DEVICE_INFO, LED_DUMP and SET_HARDWARE passed");
    return 0;
}
