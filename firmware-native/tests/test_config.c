#define _POSIX_C_SOURCE 200809L
#include "bosun/config.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static bosun_config_t config, reloaded;
static char output[BOSUN_PATCH_BYTES + 1], binding_output[BOSUN_PATCH_BYTES + 1];
static void fixture(void) {
    assert(bosun_store_format() == BOSUN_STORE_OK);
    assert(bosun_config_init(&config) == BOSUN_STORE_OK);
    assert(config.bank == 1 && config.slot == 1 && !config.has_patch && !*config.profile);
    assert(bosun_config_create("test", "Test", "generic", "#abcdef") == BOSUN_STORE_OK);
    assert(bosun_config_activate(&config, "test", true) == BOSUN_STORE_OK);
}
static void put(unsigned bank, unsigned slot, const char *json, bool draft, uint32_t now) {
    assert(bosun_config_put_patch(&config, draft ? NULL : "test", bank, slot, json, strlen(json), now) == BOSUN_STORE_OK);
}
static void name_is(const bosun_config_t *c, const char *name) {
    assert(bosun_json_equal(&c->patch_doc, bosun_json_get(&c->patch_doc, 0, "name"), name));
}
static void test_profiles(void) {
    fixture();
    assert(!bosun_config_profile_id(NULL) && !bosun_config_profile_id(""));
    assert(!bosun_config_profile_id("../other") && !bosun_config_profile_id("bad/name"));
    assert(!bosun_config_profile_id("bad\n") && !bosun_config_profile_id("a\xff"));
    assert(bosun_config_profile_id("Caf\xc3\xa8_1-2"));
    assert(!bosun_config_coordinates(0, 1) && !bosun_config_coordinates(100, 1));
    assert(!bosun_config_coordinates(1, 0) && !bosun_config_coordinates(1, 11));
    assert(bosun_config_coordinates(99, 10));
    assert(!bosun_config_path(output, sizeof output, "test", "../bad"));
    assert(!bosun_config_patch_path(output, 2, "test", 1, 1, false));
    assert(bosun_config_create("test", "Duplicate", "generic", NULL) == BOSUN_STORE_INVALID);
    assert(bosun_config_profile_exists("test") && !bosun_config_profile_exists("missing"));
    put(1, 1, "{\"name\":\"One\"}", false, 0);
    assert(bosun_config_select(&config, 1, 1) == BOSUN_STORE_OK);
    assert(bosun_config_init(&reloaded) == BOSUN_STORE_OK);
    assert(!strcmp(reloaded.profile, "test") && !strcmp(reloaded.kind, "generic")); name_is(&reloaded, "One");
    assert(bosun_config_rename("test", "Caf\xc3\xa8 \"Live\"") == BOSUN_STORE_OK);
    bosun_json_writer_t w; bosun_json_writer_init(&w, output, sizeof output);
    assert(bosun_config_profiles(&config, &w) == BOSUN_STORE_OK);
    bosun_json_token_t tokens[64]; bosun_json_doc_t doc;
    assert(bosun_json_parse(&doc, output, w.length, tokens, 64) == BOSUN_JSON_OK);
    int entry = bosun_json_at(&doc, 0, 0);
    assert(bosun_json_equal(&doc, bosun_json_get(&doc, entry, "name"), "Caf\xc3\xa8 \"Live\""));
    assert(bosun_json_equal(&doc, bosun_json_get(&doc, entry, "color"), "#abcdef"));
    assert(bosun_config_bool(&doc, entry, "active", false));

    assert(bosun_config_create("other", "Other", "kemper", NULL) == BOSUN_STORE_OK);
    assert(bosun_store_write_atomic("/config/profiles/other/device.json", "[", 1) == BOSUN_STORE_OK);
    uint32_t revision = config.revision;
    assert(bosun_config_activate(&config, "other", true) == BOSUN_STORE_INVALID);
    assert(config.revision == revision && !strcmp(config.profile, "test")); name_is(&config, "One");
    assert(bosun_config_int(&config.device_doc, 0, "long_press_ms", 0) == 600);
    assert(bosun_config_init(&reloaded) == BOSUN_STORE_OK && !strcmp(reloaded.profile, "test"));
    assert(bosun_store_write_atomic("/config/profiles/other/device.json", "{\"midi_channel\":9}", 18) == BOSUN_STORE_OK);
    assert(bosun_store_mkdir("/config/profiles/other/patches/01") == BOSUN_STORE_OK);
    assert(bosun_store_write_atomic("/config/profiles/other/patches/01/01.json", "null", 4) == BOSUN_STORE_OK);
    assert(bosun_config_activate(&config, "other", true) == BOSUN_STORE_INVALID);
    assert(config.revision == revision && !strcmp(config.profile, "test")); name_is(&config, "One");
    assert(bosun_store_remove("/config/profiles/other/patches/01/01.json") == BOSUN_STORE_OK);
    assert(bosun_config_activate(&config, "other", false) == BOSUN_STORE_OK && !config.has_patch);
    assert(bosun_config_int(&config.device_doc, 0, "midi_channel", 0) == 9);
    assert(bosun_config_init(&reloaded) == BOSUN_STORE_OK && !strcmp(reloaded.profile, "test"));
    assert(bosun_config_delete(&config, "other") == BOSUN_STORE_OK && !*config.profile && !config.has_patch);
    assert(!bosun_config_profile_exists("other") && bosun_config_profile_exists("test"));
}

static void test_drafts_and_save(void) {
    fixture(); put(1, 1, "{\"name\":\"Original\"}", false, 0);
    assert(bosun_config_select(&config, 1, 1) == BOSUN_STORE_OK);
    put(1, 1, "{\"name\":\"Edited\",\"custom\":{\"a\":[1,2,3]}}", true, 10);
    assert(config.dirty_count == 1 && bosun_config_dirty(&config, 1, 1)); name_is(&config, "Edited");
    size_t length = 0;
    assert(bosun_config_read_patch(&config, "test", 1, 1, output, sizeof output, &length) == BOSUN_STORE_OK);
    assert(!strcmp(output, "{\"name\":\"Original\"}"));
    assert(bosun_config_read_patch(&config, NULL, 1, 1, output, sizeof output, &length) == BOSUN_STORE_OK);
    assert(strstr(output, "Edited"));
    assert(bosun_config_init(&reloaded) == BOSUN_STORE_OK && reloaded.dirty_count == 1); name_is(&reloaded, "Edited");
    assert(bosun_config_put_patch(&config, NULL, 1, 1, "[]", 2, 20) == BOSUN_STORE_INVALID);
    assert(config.dirty_count == 1); name_is(&config, "Edited");
    bosun_json_writer_t w; bosun_json_writer_init(&w, output, 2);
    assert(bosun_config_save(&config, 1, 1, false, &w) == BOSUN_STORE_LIMIT);
    assert(bosun_config_dirty(&config, 1, 1));
    assert(bosun_config_save(&config, 1, 1, true, NULL) == BOSUN_STORE_OK);
    assert(config.dirty_count == 0); name_is(&config, "Original");
    put(1, 1, "{\"name\":\"Saved\"}", true, 30);
    put(2, 3, "{\"name\":\"New\"}", true, 40);
    bosun_json_writer_init(&w, output, sizeof output);
    assert(bosun_config_save(&config, 0, 0, false, &w) == BOSUN_STORE_OK);
    assert(!strcmp(output, "[{\"bank\":1,\"slot\":1},{\"bank\":2,\"slot\":3}]") && config.dirty_count == 0);
    assert(bosun_config_init(&reloaded) == BOSUN_STORE_OK && reloaded.dirty_count == 0); name_is(&reloaded, "Saved");
    put(1, 2, "{\"name\":\"Unsaved new\"}", true, 50);
    assert(bosun_config_select(&config, 1, 2) == BOSUN_STORE_OK && config.has_patch);
    assert(bosun_config_save(&config, 1, 2, true, NULL) == BOSUN_STORE_OK);
    assert(!config.has_patch && config.patch_doc.count == 0 && !*config.patch);
    assert(!bosun_config_has_patch(&config, 1, 2));
    assert(bosun_config_select(&config, 1, 1) == BOSUN_STORE_OK);
    uint32_t revision = config.patch_revision;
    assert(bosun_config_select(&config, 9, 9) == BOSUN_STORE_NOT_FOUND);
    assert(config.bank == 1 && config.slot == 1 && config.patch_revision == revision); name_is(&config, "Saved");
    assert(bosun_config_remove_patch(&config, 1, 1) == BOSUN_STORE_OK);
    assert(!config.has_patch && !*config.patch && config.patch_doc.count == 0);
    assert(!bosun_config_has_patch(&config, 1, 1));
}

static bool validate_patch(const bosun_json_doc_t *doc, void *context) {
    unsigned *called = context; ++*called;
    assert(config.slot == 1); name_is(&config, "One");
    return bosun_json_equal(doc, bosun_json_get(doc, 0, "name"), "Allowed");
}
static void test_checked_select_and_binding(void) {
    fixture();
    put(1, 1, "{\"name\":\"One\",\"custom\":{\"keep\":[1,\"x\"]},\"bindings\":[{\"switch\":\"1\",\"mode\":\"tap\",\"custom\":17},{\"switch\":\"2\",\"label\":\"Preserved\"}]}", false, 0);
    put(1, 2, "{\"name\":\"Rejected\"}", false, 0); put(1, 3, "{\"name\":\"Allowed\"}", false, 0);
    assert(bosun_config_select(&config, 1, 1) == BOSUN_STORE_OK);
    unsigned called = 0; uint32_t revision = config.revision;
    assert(bosun_config_select_checked(&config, 1, 2, validate_patch, &called) == BOSUN_STORE_INVALID);
    assert(called == 1 && config.revision == revision && config.slot == 1);
    assert(bosun_config_select_checked(&config, 1, 3, validate_patch, &called) == BOSUN_STORE_OK);
    assert(called == 2 && config.slot == 3); name_is(&config, "Allowed");
    assert(bosun_config_select(&config, 1, 1) == BOSUN_STORE_OK);
    const char binding[] = "{\"switch\":\"1\",\"mode\":\"latched\",\"actions\":{\"toggle_on\":{\"messages\":[{\"type\":\"cc\",\"cc\":7,\"value\":127}]}}}";
    assert(bosun_config_put_binding(&config, 1, 1, binding, strlen(binding), binding_output, sizeof binding_output, 10) == BOSUN_STORE_OK);
    assert(config.dirty_count == 1 && strstr(config.patch, "\"custom\":{\"keep\":[1,\"x\"]}"));
    assert(strstr(config.patch, "Preserved") && strstr(config.patch, "latched") && !strstr(config.patch, "\"custom\":17"));
    strcpy(output, config.patch); revision = config.revision;
    assert(bosun_config_put_binding(&config, 1, 1, binding, strlen(binding), binding_output, 8, 20) == BOSUN_STORE_LIMIT);
    assert(config.revision == revision && !strcmp(config.patch, output));
    assert(bosun_config_put_binding(&config, 1, 1, "{}", 2, binding_output, sizeof binding_output, 20) == BOSUN_STORE_INVALID);
    assert(bosun_config_put_binding(&config, 1, 1, binding, strlen(binding), config.patch, sizeof config.patch, 20) == BOSUN_STORE_INVALID);
    const char add[] = "{\"switch\":\"A\",\"label\":\"New\"}";
    assert(bosun_config_put_binding(&config, 1, 1, add, strlen(add), binding_output, sizeof binding_output, 20) == BOSUN_STORE_OK);
    int bindings = bosun_json_get(&config.patch_doc, 0, "bindings");
    assert(bosun_json_at(&config.patch_doc, bindings, 2) >= 0 && bosun_json_at(&config.patch_doc, bindings, 3) < 0);
}

static void test_inventory_and_autosave(void) {
    fixture();
    put(3, 10, "{\"name\":\"Last\"}", false, 0); put(1, 2, "{\"name\":\"First\"}", false, 0);
    put(2, 3, "{\"name\":\"Draft\"}", true, 0); put(1, 2, "{\"name\":\"Edited\"}", true, 0);
    bosun_patch_key_t keys[8]; size_t count = 99;
    assert(bosun_config_coordinates_list(&config, keys, 8, &count) == BOSUN_STORE_OK && count == 3);
    assert(keys[0].bank == 1 && keys[0].slot == 2 && keys[1].bank == 2 && keys[2].bank == 3 && keys[2].slot == 10);
    assert(bosun_config_coordinates_list(&config, keys, 2, &count) == BOSUN_STORE_LIMIT && count == 0);
    assert(bosun_config_patch_name(&config, 1, 2, output, sizeof output) == BOSUN_STORE_OK && !strcmp(output, "Edited"));
    assert(bosun_config_patch_name(&config, 1, 2, output, 2) == BOSUN_STORE_LIMIT);
    bosun_config_tick(&config, 60000); assert(config.dirty_count == 2);
    assert(bosun_config_save(&config, 0, 0, true, NULL) == BOSUN_STORE_OK);
    const char autosave[] = "{\"autosave\":{\"enabled\":true,\"debounce_ms\":20}}";
    assert(bosun_config_put_device(&config, NULL, autosave, strlen(autosave)) == BOSUN_STORE_OK);
    put(1, 2, "{\"name\":\"Auto\"}", true, UINT32_MAX - 9);
    bosun_config_tick(&config, 9); assert(config.dirty_count == 1);
    bosun_config_tick(&config, 10); assert(config.dirty_count == 0);
    assert(bosun_config_patch_name(&config, 1, 2, output, sizeof output) == BOSUN_STORE_OK && !strcmp(output, "Auto"));
    /* A failed save keeps its recoverable draft and reports the storage error. */
    put(2, 3, "{\"name\":\"Recoverable\"}", true, 20);
    assert(bosun_store_mkdir("/config/profiles/test/patches/02/03.json") == BOSUN_STORE_OK);
    bosun_config_tick(&config, 40);
    assert(config.dirty_count == 1 && config.last_error != BOSUN_STORE_OK);
    assert(bosun_config_patch_name(&config, 2, 3, output, sizeof output) == BOSUN_STORE_OK && !strcmp(output, "Recoverable"));
    assert(bosun_store_remove("/config/profiles/test/patches/02/03.json") == BOSUN_STORE_OK);
    bosun_config_tick(&config, 41); assert(config.dirty_count == 0 && config.last_error == BOSUN_STORE_OK);
}

int main(void) {
    char root[] = "/tmp/bosun-config-XXXXXX";
    assert(mkdtemp(root) && bosun_store_mount(root));
    test_profiles(); test_drafts_and_save(); test_checked_select_and_binding(); test_inventory_and_autosave();
    assert(bosun_store_format() == BOSUN_STORE_OK && rmdir(root) == 0);
    puts("Config: profiles, activation rollback, persisted/draft isolation and reboot recovery, checked selection, binding preservation, save/discard/failed writes, inventory and autosave rollover passed");
    return 0;
}
