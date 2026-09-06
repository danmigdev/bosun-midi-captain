#include "storage_cases.h"
#include "bosun/board.h"
#include <setjmp.h>
#include <stdio.h>

enum { FLASH_BYTES = 512 * 1024, FLASH_BASE = 1536 * 1024, ERASE_SIZE = 4096, PAGE_SIZE = 256 };
static uint8_t flash[FLASH_BYTES], snapshot[FLASH_BYTES];
static uint8_t old_data[9000], new_data[17000], large_data[65536], output[65536];
static size_t mutations;
static int cut_at = -1, cut_mode;
static bool write_failure, read_failure;
static uint32_t geometry_size = FLASH_BYTES;
static jmp_buf power_loss;

uint32_t bosun_board_storage_offset(void) { return FLASH_BASE; }
uint32_t bosun_board_storage_size(void) { return geometry_size; }

static size_t checked_offset(uint32_t absolute, size_t length) {
    assert(absolute >= FLASH_BASE && absolute - FLASH_BASE <= FLASH_BYTES);
    size_t offset = absolute - FLASH_BASE;
    assert(length <= FLASH_BYTES - offset);
    return offset;
}

bool bosun_board_flash_read(uint32_t absolute, uint8_t *data, size_t length) {
    size_t offset = checked_offset(absolute, length);
    if (read_failure) return false;
    memcpy(data, flash + offset, length);
    return true;
}

static bool mutate(uint32_t absolute, const uint8_t *data, size_t length, bool erase) {
    size_t offset = checked_offset(absolute, length);
    assert(length > 0);
    if (erase) assert(absolute % ERASE_SIZE == 0 && length == ERASE_SIZE);
    else {
        assert(absolute % PAGE_SIZE == 0 && length % PAGE_SIZE == 0);
        /* Real NOR programming may only change 1 to 0; erase is explicit. */
        for (size_t i = 0; i < length; ++i) assert((flash[offset + i] & data[i]) == data[i]);
    }
    if (write_failure) return false;
    bool cut = (int)mutations++ == cut_at;
    if (cut && cut_mode == 0) longjmp(power_loss, 1);
    size_t amount = cut && cut_mode == 1 ? length / 2 : length;
    if (erase) memset(flash + offset, 0xff, amount);
    else for (size_t i = 0; i < amount; ++i) flash[offset + i] &= data[i];
    if (cut) longjmp(power_loss, 1);
    return true;
}

bool bosun_board_flash_program(uint32_t absolute, const uint8_t *data, size_t length) {
    return mutate(absolute, data, length, false);
}

bool bosun_board_flash_erase(uint32_t absolute, size_t length) {
    return mutate(absolute, NULL, length, true);
}

static void assert_complete_file(void) {
    size_t length = 0;
    assert(bosun_store_read("/config/device.json", output, sizeof output, &length) == BOSUN_STORE_OK);
    assert((length == sizeof old_data && !memcmp(output, old_data, length)) ||
           (length == sizeof new_data && !memcmp(output, new_data, length)));
}

static void test_power_cuts(void) {
    assert(bosun_store_format() == BOSUN_STORE_OK);
    assert(bosun_store_mkdir("/config") == BOSUN_STORE_OK);
    assert(bosun_store_write_atomic("/config/device.json", old_data, sizeof old_data) == BOSUN_STORE_OK);
    memcpy(snapshot, flash, sizeof flash);
    mutations = 0;
    assert(bosun_store_write_atomic("/config/device.json", new_data, sizeof new_data) == BOSUN_STORE_OK);
    size_t transaction_operations = mutations;
    assert(transaction_operations > 10 && transaction_operations < 200);
    size_t tested = 0;
    /* Every erase/program boundary, plus interrupted half-program and
     * half-erase, runs through the actual RP2040 littlefs backend. Re-mount
     * discards all transient filesystem/file caches just like a cold boot. */
    for (int mode = 0; mode < 3; ++mode) {
        for (size_t operation = 0; operation < transaction_operations; ++operation) {
            cut_at = -1;
            memcpy(flash, snapshot, sizeof flash);
            assert(bosun_store_mount(NULL));
            cut_mode = mode;
            cut_at = (int)operation;
            mutations = 0;
            if (setjmp(power_loss) == 0) {
                (void)bosun_store_write_atomic("/config/device.json", new_data, sizeof new_data);
                assert(!"injected power loss was not reached");
            }
            cut_at = -1;
            assert(bosun_store_mount(NULL));
            assert_complete_file();
            bosun_dirent_t entries[2];
            size_t count = 0;
            assert(bosun_store_list("/config", entries, 2, &count) == BOSUN_STORE_OK && count == 1);
            assert(!strcmp(entries[0].name, "device.json"));
            /* A leftover temporary file cannot poison the next transaction. */
            assert(bosun_store_write_atomic("/config/device.json", new_data, sizeof new_data) == BOSUN_STORE_OK);
            assert(bosun_store_mount(NULL));
            size_t length = 0;
            assert(bosun_store_read("/config/device.json", output, sizeof output, &length) == BOSUN_STORE_OK && length == sizeof new_data);
            assert(!memcmp(output, new_data, length));
            ++tested;
        }
    }
    printf("littlefs: %zu injected power cuts across %zu real erase/program operations preserved complete old/new files and recovered\n", tested, transaction_operations);
}

static void test_capacity(void) {
    assert(bosun_store_format() == BOSUN_STORE_OK);
    assert(bosun_store_write_atomic("/sentinel", "keep", 4) == BOSUN_STORE_OK);
    unsigned created = 0;
    for (; created < 12; ++created) {
        char name[32];
        snprintf(name, sizeof name, "/fill-%u", created);
        bosun_store_result_t status = bosun_store_write_atomic(name, large_data, sizeof large_data);
        if (status != BOSUN_STORE_OK) {
            assert(status == BOSUN_STORE_LIMIT);
            size_t length = 0;
            assert(bosun_store_read(name, output, sizeof output, &length) == BOSUN_STORE_NOT_FOUND);
            break;
        }
    }
    assert(created >= 4 && created < 8);
    assert(bosun_store_mount(NULL));
    for (unsigned i = 0; i < created; ++i) {
        char name[32];
        size_t length = 0;
        snprintf(name, sizeof name, "/fill-%u", i);
        assert(bosun_store_read(name, output, sizeof output, &length) == BOSUN_STORE_OK && length == sizeof large_data);
        assert(!memcmp(output, large_data, length));
    }
    /* An out-of-space replacement cannot delete the previous small file. */
    assert(bosun_store_write_atomic("/sentinel", large_data, sizeof large_data) == BOSUN_STORE_LIMIT);
    size_t length = 0;
    assert(bosun_store_read("/sentinel", output, sizeof output, &length) == BOSUN_STORE_OK && length == 4 && !memcmp(output, "keep", 4));
    assert(bosun_store_remove("/fill-0") == BOSUN_STORE_OK);
    assert(bosun_store_write_atomic("/reclaimed", large_data, sizeof large_data) == BOSUN_STORE_OK);
    assert(bosun_store_mount(NULL));
    assert(bosun_store_read("/reclaimed", output, sizeof output, &length) == BOSUN_STORE_OK && length == sizeof large_data);
    assert(!memcmp(output, large_data, length));
    printf("littlefs: full 512 KiB volume retained %u committed 64 KiB files, rejected oversized replacement, and reclaimed space\n", created);
}

int main(void) {
    test_paths();
    for (size_t i = 0; i < sizeof old_data; ++i) old_data[i] = (uint8_t)(i * 17u + 3u);
    for (size_t i = 0; i < sizeof new_data; ++i) new_data[i] = (uint8_t)(i * 29u + 11u);
    for (size_t i = 0; i < sizeof large_data; ++i) large_data[i] = (uint8_t)(i * 7u + 5u);
    /* Unknown CP/FAT-like and erased volumes must never be auto-formatted. */
    memset(flash, 0xa5, sizeof flash);
    memcpy(flash + 3, "MSDOS5.0", 8); flash[510] = 0x55; flash[511] = 0xaa;
    memcpy(snapshot, flash, sizeof flash);
    assert(!bosun_store_mount(NULL) && !bosun_store_ready());
    assert(mutations == 0 && !memcmp(flash, snapshot, sizeof flash));
    size_t length = 1;
    assert(bosun_store_read("/config", output, sizeof output, &length) == BOSUN_STORE_UNAVAILABLE && length == 0);
    memset(flash, 0xff, sizeof flash);
    assert(!bosun_store_mount(NULL) && mutations == 0);
    geometry_size = FLASH_BYTES / 2;
    assert(!bosun_store_mount(NULL) && bosun_store_format() == BOSUN_STORE_UNAVAILABLE && mutations == 0);
    geometry_size = FLASH_BYTES;
    assert(bosun_store_format() == BOSUN_STORE_OK);
    test_storage_api();
    memcpy(snapshot, flash, sizeof flash);
    mutations = 0;
    assert(bosun_store_mount(NULL) && mutations == 0 && !memcmp(flash, snapshot, sizeof flash));
    assert(bosun_store_read("/config/profiles/kemper_player_buk4/device.json", output, sizeof output, &length) == BOSUN_STORE_OK && length > 80);
    write_failure = true;
    assert(bosun_store_write_atomic("/config/profiles/kemper_player_buk4/device.json", "bad", 3) == BOSUN_STORE_IO);
    write_failure = false;
    assert(bosun_store_mount(NULL));
    assert(bosun_store_read("/config/profiles/kemper_player_buk4/device.json", output, sizeof output, &length) == BOSUN_STORE_OK && length > 80);
    read_failure = true;
    assert(!bosun_store_mount(NULL) && !bosun_store_ready());
    read_failure = false;
    assert(bosun_store_mount(NULL));
    test_power_cuts();
    test_capacity();
    puts("littlefs storage: no automatic format, static caches, CP paths/raw JSON, remount, bounded reads/lists and I/O failures passed");
    return 0;
}
