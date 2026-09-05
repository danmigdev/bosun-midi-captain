/* Build an offline provisioning image with the exact RP2040 littlefs backend.
 * This executable never opens USB, flashes hardware, or changes its input. */
#define _POSIX_C_SOURCE 200809L
#include "bosun/board.h"
#include "bosun/config.h"
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

enum { IMAGE_BYTES = 512 * 1024, IMAGE_BASE = 1536 * 1024, ERASE_BYTES = 4096 };
static uint8_t flash[IMAGE_BYTES], original[IMAGE_BYTES];
static char input[BOSUN_PATCH_BYTES + 1], readback[BOSUN_PATCH_BYTES + 1];
static bosun_json_token_t tokens[BOSUN_PATCH_TOKENS];
static bosun_config_t config;
static size_t file_count, input_bytes;

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

static bool coordinate(const char *name, unsigned maximum, bool file) {
    size_t length = strlen(name);
    if (length != (file ? 7u : 2u) || name[0] < '0' || name[0] > '9' ||
        name[1] < '0' || name[1] > '9' || (file && strcmp(name + 2, ".json"))) return false;
    unsigned number = (unsigned)(name[0] - '0') * 10u + (unsigned)(name[1] - '0');
    return number >= 1 && number <= maximum;
}

typedef enum { BAD_PATH, DIRECTORY, ACTIVE, MANIFEST, DEVICE, LEARN, PATCH } path_kind_t;
static path_kind_t path_kind(const char *path, bool directory) {
    if (!bosun_store_safe_path(path) || strncmp(path, "/config/", 8)) return BAD_PATH;
    char parts[BOSUN_PATH_MAX]; strcpy(parts, path + 8);
    char *component[6], *save = NULL; size_t count = 0;
    for (char *p = strtok_r(parts, "/", &save); p; p = strtok_r(NULL, "/", &save)) {
        if (count == 6) return BAD_PATH;
        component[count++] = p;
    }
    if (!directory && count == 1 && !strcmp(component[0], "active_profile.json")) return ACTIVE;
    if (!count || strcmp(component[0], "profiles")) return BAD_PATH;
    if (directory && count == 1) return DIRECTORY;
    if (count < 2 || !bosun_config_profile_id(component[1])) return BAD_PATH;
    if (directory && count == 2) return DIRECTORY;
    if (!directory && count == 3) {
        if (!strcmp(component[2], "manifest.json")) return MANIFEST;
        if (!strcmp(component[2], "device.json")) return DEVICE;
        if (!strcmp(component[2], "midi_learn.json")) return LEARN;
    }
    if (count < 3 || strcmp(component[2], "patches")) return BAD_PATH;
    if (directory && count == 3) return DIRECTORY;
    if (count < 4 || !coordinate(component[3], 99, false)) return BAD_PATH;
    if (directory && count == 4) return DIRECTORY;
    return !directory && count == 5 && coordinate(component[4], 10, true) ? PATCH : BAD_PATH;
}

static bool read_source(int directory, const char *name, const char *path, size_t *length) {
    int file = openat(directory, name, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC);
    if (file < 0) return fail("cannot open input without following symlinks", path);
    struct stat metadata;
    bool valid = fstat(file, &metadata) == 0 && S_ISREG(metadata.st_mode) &&
        metadata.st_size > 0 && metadata.st_size <= BOSUN_PATCH_BYTES;
    size_t used = 0;
    while (valid && used < sizeof input) {
        ssize_t got = read(file, input + used, sizeof input - used);
        if (got < 0 && errno == EINTR) continue;
        if (got < 0) { valid = false; break; }
        if (!got) break;
        used += (size_t)got;
    }
    if (close(file)) valid = false;
    if (!valid || used != (size_t)metadata.st_size || used > BOSUN_PATCH_BYTES)
        return fail("input must be a stable, nonempty regular file within the native size limit", path);
    *length = used; input[used] = 0; return true;
}

static bool validate_json(path_kind_t kind, const char *path, size_t length) {
    if ((kind == DEVICE || kind == LEARN) && length > BOSUN_DEVICE_BYTES)
        return fail("device or MIDI learn JSON exceeds 16384 bytes", path);
    bosun_json_doc_t document;
    uint16_t capacity = kind == DEVICE ? BOSUN_DEVICE_TOKENS : BOSUN_PATCH_TOKENS;
    if (bosun_json_parse(&document, input, length, tokens, capacity) != BOSUN_JSON_OK ||
        document.tokens[0].type != BOSUN_JSON_OBJECT)
        return fail("invalid JSON object or native token limit exceeded", path);
    if (kind == MANIFEST) {
        int token = bosun_json_get(&document, 0, "kind");
        if (token >= 0 && !bosun_json_equal(&document, token, "generic_midi") &&
            !bosun_json_equal(&document, token, "kemper_player") &&
            !bosun_json_equal(&document, token, "unknown"))
            return fail("unsupported native plugin", path);
    }
    return true;
}

static int compare_names(const void *a, const void *b) { return strcmp(a, b); }
static bool walk(int directory, const char *path, bool verify) {
    int duplicate = openat(directory, ".", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    DIR *stream = duplicate >= 0 ? fdopendir(duplicate) : NULL;
    if (!stream) { if (duplicate >= 0) close(duplicate); return fail("cannot list directory", path); }
    char names[BOSUN_STORE_LIST_MAX][BOSUN_NAME_MAX]; size_t count = 0;
    bool valid = true;
    for (;;) {
        errno = 0; struct dirent *entry = readdir(stream);
        if (!entry) { if (errno) valid = false; break; }
        if (!strcmp(entry->d_name, ".") || !strcmp(entry->d_name, "..")) continue;
        if (count == BOSUN_STORE_LIST_MAX || strlen(entry->d_name) >= BOSUN_NAME_MAX) {
            valid = false; break;
        }
        strcpy(names[count++], entry->d_name);
    }
    if (closedir(stream)) valid = false;
    if (!valid) return fail("directory exceeds native limits or cannot be read", path);
    qsort(names, count, sizeof names[0], compare_names);
    for (size_t i = 0; i < count; ++i) {
        char target[BOSUN_PATH_MAX];
        int size = snprintf(target, sizeof target, "%s/%s", path, names[i]);
        if (size < 0 || (size_t)size >= sizeof target) return fail("path too long", path);
        struct stat metadata;
        if (fstatat(directory, names[i], &metadata, AT_SYMLINK_NOFOLLOW))
            return fail("cannot inspect input", target);
        bool is_directory = S_ISDIR(metadata.st_mode);
        path_kind_t kind = path_kind(target, is_directory);
        if (kind == BAD_PATH || (!is_directory && !S_ISREG(metadata.st_mode)))
            return fail("unsupported path, file type, or symlink", target);
        if (is_directory) {
            int child = openat(directory, names[i], O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
            if (child < 0) return fail("cannot open directory without following symlinks", target);
            valid = (verify || bosun_store_mkdir(target) == BOSUN_STORE_OK) && walk(child, target, verify);
            if (close(child)) valid = false;
            if (!valid) return fail("directory import/verification failed", target);
        } else {
            size_t length;
            if (!read_source(directory, names[i], target, &length) || !validate_json(kind, target, length)) return false;
            if (verify) {
                size_t got = 0;
                if (bosun_store_read(target, readback, sizeof readback, &got) != BOSUN_STORE_OK ||
                    got != length || memcmp(input, readback, length)) return fail("readback differs from input", target);
            } else if (bosun_store_write_atomic(target, input, length) != BOSUN_STORE_OK)
                return fail("littlefs import failed (volume may be full)", target);
            ++file_count; input_bytes += length;
        }
    }
    return true;
}

static bool validate_profiles(void) {
    if (bosun_config_init(&config) != BOSUN_STORE_OK || !*config.profile)
        return fail("an existing active profile is required", "/config/active_profile.json");
    bosun_dirent_t entries[BOSUN_STORE_LIST_MAX]; size_t count = 0;
    if (bosun_store_list("/config/profiles", entries, BOSUN_STORE_LIST_MAX, &count) != BOSUN_STORE_OK ||
        !count || count > BOSUN_PROFILE_MAX) return fail("invalid profile count", "/config/profiles");
    for (size_t i = 0; i < count; ++i) {
        if (!entries[i].directory || bosun_config_activate(&config, entries[i].name, false) != BOSUN_STORE_OK)
            return fail("profile cannot be activated by native configuration code", entries[i].name);
        bosun_patch_key_t keys[BOSUN_PATCH_CATALOG_MAX]; size_t patches = 0;
        if (bosun_config_coordinates_list(&config, keys, BOSUN_PATCH_CATALOG_MAX, &patches) != BOSUN_STORE_OK)
            return fail("patch catalog exceeds native limits", entries[i].name);
    }
    return bosun_config_init(&config) == BOSUN_STORE_OK;
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
    if (argc != 5 || strcmp(argv[1], "--config-root") || strcmp(argv[3], "--output")) {
        fprintf(stderr, "Usage: %s --config-root EXISTING_CONFIG_DIRECTORY --output NEW_IMAGE.bin\n", argv[0]);
        return 2;
    }
    int root = open_directory(argv[2]);
    if (root < 0) { fail("config root must be an existing directory without symlinks or traversal", argv[2]); return 1; }
    memset(flash, 0xff, sizeof flash);
    bool valid = bosun_store_format() == BOSUN_STORE_OK && bosun_store_mkdir("/config") == BOSUN_STORE_OK &&
        walk(root, "/config", false);
    size_t expected_files = file_count, expected_bytes = input_bytes;
    if (valid) {
        memcpy(original, flash, sizeof flash);
        valid = bosun_store_mount(NULL) && validate_profiles();
        file_count = input_bytes = 0;
        valid = valid && walk(root, "/config", true) && file_count == expected_files &&
            input_bytes == expected_bytes && !memcmp(original, flash, sizeof flash);
    }
    close(root);
    if (!valid) { fail("validation failed; no output created", argv[2]); return 1; }
    if (!publish(argv[4])) return 1;
    printf("{\"storage_bytes\":%u,\"block_bytes\":%u,\"files\":%zu,\"input_bytes\":%zu,\"verified\":true}\n",
           IMAGE_BYTES, ERASE_BYTES, expected_files, expected_bytes);
    return 0;
}
