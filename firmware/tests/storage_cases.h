#ifndef BOSUN_TEST_STORAGE_CASES_H
#define BOSUN_TEST_STORAGE_CASES_H
#include "bosun/storage.h"
#include <assert.h>
#include <stdint.h>
#include <string.h>

static void test_paths(void) {
    const char *valid[] = { "/", "config", "/config/profiles/kemper_player_buk4/device.json",
                            "/config/profiles/x/patches/01/02.json", "fonts/caf\xc3\xa8.bdf" };
    const char *invalid[] = { "", ".", "..", "/.", "/..", "../outside", "/config/../device.json",
        "/config/./device.json", "//config", "/config/", "config//x", "C:/config", "a\\b",
        "a\n", "a\177", ".bosun-atomic.tmp", "config/.bosun-atomic.tmp/x" };
    assert(!bosun_store_safe_path(NULL));
    for (size_t i = 0; i < sizeof valid / sizeof *valid; ++i) assert(bosun_store_safe_path(valid[i]));
    for (size_t i = 0; i < sizeof invalid / sizeof *invalid; ++i) assert(!bosun_store_safe_path(invalid[i]));
    char name[BOSUN_NAME_MAX + 1];
    memset(name, 'a', sizeof name);
    name[BOSUN_NAME_MAX - 1] = 0;
    assert(bosun_store_safe_path(name));
    name[BOSUN_NAME_MAX - 1] = 'a'; name[BOSUN_NAME_MAX] = 0;
    assert(!bosun_store_safe_path(name));
    char path[BOSUN_PATH_MAX + 1];
    memset(path, 'a', sizeof path);
    path[63] = '/'; path[127] = '/'; path[BOSUN_PATH_MAX - 1] = 0;
    assert(bosun_store_safe_path(path));
    path[BOSUN_PATH_MAX - 1] = 'a'; path[BOSUN_PATH_MAX] = 0;
    assert(!bosun_store_safe_path(path));
}

static void test_storage_api(void) {
    static const char path[] = "/config/profiles/kemper_player_buk4/device.json";
    static const char json[] = "{\"name\":\"Caf\xc3\xa8 \xf0\x9f\x8e\xb8\",\"tft\":{\"layout\":[{\"field\":\"expression_mode\",\"color\":\"#ff7f00\"}]}}\n";
    char buffer[256], slices[sizeof json];
    size_t length = 99, count = 99;
    bosun_dirent_t entries[4];
    assert(bosun_store_ready());
    assert(bosun_store_list("/", NULL, 0, &count) == BOSUN_STORE_OK && count == 0);
    assert(bosun_store_mkdir("/config/profiles/kemper_player_buk4/patches/01") == BOSUN_STORE_OK);
    assert(bosun_store_mkdir("/config/profiles/kemper_player_buk4/patches/01") == BOSUN_STORE_OK);
    assert(bosun_store_mkdir("/") == BOSUN_STORE_OK);
    assert(bosun_store_read(path, buffer, sizeof buffer, &length) == BOSUN_STORE_NOT_FOUND && length == 0);
    assert(bosun_store_write_atomic(path, json, sizeof json - 1) == BOSUN_STORE_OK);
    assert(bosun_store_read(path, buffer, sizeof buffer, &length) == BOSUN_STORE_OK);
    assert(length == sizeof json - 1 && !memcmp(buffer, json, length));
    memset(buffer, 0x5a, sizeof buffer);
    assert(bosun_store_read(path, buffer, 4, &length) == BOSUN_STORE_LIMIT && length == 0 && buffer[0] == 0x5a);
    for (size_t offset = 0; offset < sizeof json - 1;) {
        assert(bosun_store_read_at(path, (uint32_t)offset, slices + offset, 7, &length) == BOSUN_STORE_OK);
        assert(length > 0 && length <= 7);
        offset += length;
    }
    assert(!memcmp(slices, json, sizeof json - 1));
    assert(bosun_store_read_at(path, UINT32_MAX, buffer, sizeof buffer, &length) == BOSUN_STORE_OK && length == 0);
    assert(bosun_store_read_at(path, 0, NULL, 0, &length) == BOSUN_STORE_OK && length == 0);
    assert(bosun_store_read(path, NULL, 1, &length) == BOSUN_STORE_INVALID && length == 0);
    assert(bosun_store_read(path, buffer, sizeof buffer, NULL) == BOSUN_STORE_INVALID);
    assert(bosun_store_write_atomic(path, "x", BOSUN_STORE_FILE_MAX + 1u) == BOSUN_STORE_LIMIT);
    assert(bosun_store_write_atomic(path, NULL, 1) == BOSUN_STORE_INVALID);
    assert(bosun_store_write_atomic(path, "{}", 2) == BOSUN_STORE_OK);
    assert(bosun_store_read(path, buffer, sizeof buffer, &length) == BOSUN_STORE_OK && length == 2 && !memcmp(buffer, "{}", 2));
    assert(bosun_store_write_atomic(path, json, sizeof json - 1) == BOSUN_STORE_OK);
    assert(bosun_store_write_atomic("/config/empty", NULL, 0) == BOSUN_STORE_OK);
    assert(bosun_store_read("/config/empty", NULL, 0, &length) == BOSUN_STORE_OK && length == 0);
    assert(bosun_store_list("/config/profiles/kemper_player_buk4", entries, 1, &count) == BOSUN_STORE_LIMIT && count == 0);
    assert(bosun_store_list("/config/profiles/kemper_player_buk4", entries, 4, &count) == BOSUN_STORE_OK && count == 2);
    bool saw_file = false, saw_directory = false;
    for (size_t i = 0; i < count; ++i) {
        if (!strcmp(entries[i].name, "device.json")) { saw_file = true; assert(!entries[i].directory && entries[i].size == sizeof json - 1); }
        if (!strcmp(entries[i].name, "patches")) { saw_directory = true; assert(entries[i].directory); }
    }
    assert(saw_file && saw_directory);
    assert(bosun_store_list("/", entries, BOSUN_STORE_LIST_MAX + 1, &count) == BOSUN_STORE_LIMIT && count == 0);
    assert(bosun_store_mkdir(path) == BOSUN_STORE_INVALID);
    assert(bosun_store_read("/config", buffer, sizeof buffer, &length) == BOSUN_STORE_INVALID);
    assert(bosun_store_remove("/config") == BOSUN_STORE_INVALID);
    assert(bosun_store_remove("/") == BOSUN_STORE_INVALID);
    assert(bosun_store_remove("/config/empty") == BOSUN_STORE_OK);
    assert(bosun_store_remove("/config/empty") == BOSUN_STORE_NOT_FOUND);
    assert(bosun_store_write_atomic("/no-parent/file", "x", 1) == BOSUN_STORE_NOT_FOUND);
    assert(bosun_store_write_atomic("/config/../escape", "x", 1) == BOSUN_STORE_INVALID);
    char limit_path[BOSUN_PATH_MAX];
    memset(limit_path, 'z', sizeof limit_path);
    limit_path[63] = '/'; limit_path[127] = 0;
    assert(bosun_store_mkdir(limit_path) == BOSUN_STORE_OK);
    limit_path[127] = '/'; limit_path[159] = 0;
    assert(bosun_store_write_atomic(limit_path, "max", 3) == BOSUN_STORE_OK);
    assert(bosun_store_read(limit_path, buffer, sizeof buffer, &length) == BOSUN_STORE_OK && length == 3);
}
#endif
