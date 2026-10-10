/* Build the empty factory storage image with the exact RP2040 littlefs backend.
 * This executable never opens USB, flashes hardware, or replaces a file. */
#define _POSIX_C_SOURCE 200809L
#include "bosun/board.h"
#include "bosun/config.h"
#include "bosun/storage_layout.h"
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

enum { IMAGE_BYTES = BOSUN_STORAGE_BYTES, IMAGE_BASE = 4 * 1024 * 1024, ERASE_BYTES = 4096 };
static uint8_t flash[IMAGE_BYTES], original[IMAGE_BYTES];
static bosun_config_t config;

uint32_t bosun_board_storage_offset(void) { return IMAGE_BASE; }
uint32_t bosun_board_storage_size(void) { return IMAGE_BYTES; }
static bool range(uint32_t absolute, size_t length) {
    return absolute >= IMAGE_BASE && absolute - IMAGE_BASE <= IMAGE_BYTES &&
        length <= IMAGE_BYTES - (absolute - IMAGE_BASE);
}
bool bosun_board_flash_read(uint32_t absolute, uint8_t *data, size_t length) {
    if (!data || !range(absolute, length)) return false;
    memcpy(data, flash + (absolute - IMAGE_BASE), length); return true;
}
bool bosun_board_flash_program(uint32_t absolute, const uint8_t *data, size_t length) {
    if (!data || !range(absolute, length) || absolute % 256 || length % 256) return false;
    uint8_t *destination = flash + (absolute - IMAGE_BASE);
    for (size_t i = 0; i < length; ++i)
        if ((destination[i] & data[i]) != data[i]) return false;
    for (size_t i = 0; i < length; ++i) destination[i] &= data[i];
    return true;
}
bool bosun_board_flash_erase(uint32_t absolute, size_t length) {
    if (!range(absolute, length) || absolute % ERASE_BYTES || length % ERASE_BYTES) return false;
    memset(flash + (absolute - IMAGE_BASE), 0xff, length); return true;
}

static bool fail(const char *reason, const char *path) {
    fprintf(stderr, "storage image: %s: %s\n", reason, path ? path : ""); return false;
}

/* O_NOFOLLOW on every component, not only the final path, also rejects a
 * symlinked ancestor. '..' is never needed by this explicit provisioning CLI. */
static int open_directory(const char *path) {
    if (!path || !*path) { errno = EINVAL; return -1; }
    int current = open(*path == '/' ? "/" : ".", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    const char *at = path;
    while (current >= 0 && *at) {
        if (*at == '/') { ++at; continue; }
        const char *end = strchr(at, '/');
        size_t length = end ? (size_t)(end - at) : strlen(at);
        if (length > 255 || (length == 2 && !memcmp(at, "..", 2))) {
            close(current); errno = EINVAL; return -1;
        }
        if (!(length == 1 && *at == '.')) {
            char component[256]; memcpy(component, at, length); component[length] = 0;
            int next = openat(current, component, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
            close(current); current = next;
        }
        at += length;
    }
    return current;
}

static bool publish(const char *path) {
    size_t length = strlen(path);
    if (!length || length >= 4096) return fail("invalid output path", path);
    char parent[4096]; strcpy(parent, path);
    char *slash = strrchr(parent, '/'); const char *name = path;
    if (slash) { name = path + (slash - parent) + 1; if (slash == parent) slash[1] = 0; else *slash = 0; }
    else strcpy(parent, ".");
    if (!*name || !strcmp(name, ".") || !strcmp(name, "..")) return fail("invalid output filename", path);
    int directory = open_directory(parent);
    if (directory < 0) return fail("output parent must exist without symlinks or traversal", path);
    int output = openat(directory, name, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (output < 0) { close(directory); return fail("output must be a new file (no overwrite)", path); }
    size_t done = 0; bool valid = true;
    while (done < sizeof flash) {
        ssize_t wrote = write(output, flash + done, sizeof flash - done);
        if (wrote < 0 && errno == EINTR) continue;
        if (wrote <= 0) { valid = false; break; }
        done += (size_t)wrote;
    }
    if (valid && fsync(output)) valid = false;
    if (close(output)) valid = false;
    if (valid && fsync(directory)) valid = false;
    /* This name was created with O_EXCL by this invocation, never preexisting. */
    if (!valid) (void)unlinkat(directory, name, 0);
    close(directory);
    return valid || fail("cannot persist complete output", path);
}

int main(int argc, char **argv) {
    if (argc != 4 || strcmp(argv[1], "--empty") || strcmp(argv[2], "--output")) {
        fprintf(stderr, "Usage: %s --empty --output NEW_IMAGE.bin\n", argv[0]);
        return 2;
    }
    /* Factory provisioning: an empty, mountable volume that the
     * configuration code accepts without writing anything on first mount. */
    memset(flash, 0xff, sizeof flash);
    bool valid = bosun_store_format() == BOSUN_STORE_OK &&
        bosun_store_mkdir("/config") == BOSUN_STORE_OK &&
        bosun_store_mkdir("/config/profiles") == BOSUN_STORE_OK;
    memcpy(original, flash, sizeof flash);
    size_t count = 0;
    bosun_dirent_t entries[1];
    valid = valid && bosun_store_mount(NULL) &&
        bosun_config_init(&config) == BOSUN_STORE_OK && !*config.profile &&
        bosun_store_list("/config/profiles", entries, 1, &count) == BOSUN_STORE_OK &&
        count == 0 && !memcmp(original, flash, sizeof flash);
    if (!valid || !publish(argv[3])) return 1;
    puts("{\"empty\":true,\"storage_bytes\":4194304,\"verified\":true}");
    return 0;
}
