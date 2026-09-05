#include "bosun/storage.h"
#include "bosun/board.h"
#include "lfs.h"
#include <string.h>

enum { BLOCK_SIZE = 4096, CACHE_SIZE = 256, LOOKAHEAD_SIZE = 16,
       STORAGE_SIZE = 512 * 1024 };
static lfs_t filesystem;
static struct lfs_config config;
static bool mounted;
static uint32_t flash_base;
static _Alignas(4) uint8_t read_cache[CACHE_SIZE];
static _Alignas(4) uint8_t program_cache[CACHE_SIZE];
static _Alignas(4) uint8_t lookahead[LOOKAHEAD_SIZE];
static _Alignas(4) uint8_t file_cache[CACHE_SIZE];
static const struct lfs_file_config file_config = { .buffer = file_cache };
static const char temporary_name[] = ".bosun-atomic.tmp";

static int flash_read(const struct lfs_config *c, lfs_block_t block,
                      lfs_off_t offset, void *buffer, lfs_size_t size) {
    if (block >= c->block_count || offset > BLOCK_SIZE || size > BLOCK_SIZE - offset)
        return LFS_ERR_IO;
    return bosun_board_flash_read(flash_base + block * BLOCK_SIZE + offset,
                                  buffer, size) ? 0 : LFS_ERR_IO;
}

static int flash_program(const struct lfs_config *c, lfs_block_t block,
                         lfs_off_t offset, const void *buffer, lfs_size_t size) {
    if (block >= c->block_count || offset > BLOCK_SIZE || size > BLOCK_SIZE - offset)
        return LFS_ERR_IO;
    return bosun_board_flash_program(flash_base + block * BLOCK_SIZE + offset,
                                     buffer, size) ? 0 : LFS_ERR_IO;
}

static int flash_erase(const struct lfs_config *c, lfs_block_t block) {
    if (block >= c->block_count) return LFS_ERR_IO;
    return bosun_board_flash_erase(flash_base + block * BLOCK_SIZE, BLOCK_SIZE)
        ? 0 : LFS_ERR_IO;
}

static int flash_sync(const struct lfs_config *c) {
    (void)c; /* Board program/erase complete synchronously. No pending DMA. */
    return 0;
}

static bool configure(void) {
    flash_base = bosun_board_storage_offset();
    if (bosun_board_storage_size() != STORAGE_SIZE || flash_base % BLOCK_SIZE ||
        flash_base > UINT32_MAX - STORAGE_SIZE) return false;
    memset(&config, 0, sizeof config);
    config.read = flash_read;
    config.prog = flash_program;
    config.erase = flash_erase;
    config.sync = flash_sync;
    config.read_size = CACHE_SIZE;
    config.prog_size = CACHE_SIZE;
    config.block_size = BLOCK_SIZE;
    config.block_count = STORAGE_SIZE / BLOCK_SIZE;
    config.block_cycles = 500;
    config.cache_size = CACHE_SIZE;
    config.lookahead_size = LOOKAHEAD_SIZE;
    config.read_buffer = read_cache;
    config.prog_buffer = program_cache;
    config.lookahead_buffer = lookahead;
    config.name_max = BOSUN_NAME_MAX - 1;
    config.file_max = BOSUN_STORE_FILE_MAX;
    return true;
}

static bosun_store_result_t result(int error) {
    switch (error) {
    case 0: return BOSUN_STORE_OK;
    case LFS_ERR_NOENT: return BOSUN_STORE_NOT_FOUND;
    case LFS_ERR_NOSPC: case LFS_ERR_NOMEM: case LFS_ERR_FBIG:
    case LFS_ERR_NAMETOOLONG: return BOSUN_STORE_LIMIT;
    case LFS_ERR_INVAL: case LFS_ERR_ISDIR: case LFS_ERR_NOTDIR:
    case LFS_ERR_EXIST: case LFS_ERR_NOTEMPTY: return BOSUN_STORE_INVALID;
    default: return BOSUN_STORE_IO;
    }
}

bool bosun_store_mount(const char *host_root) {
    (void)host_root;
    if (mounted) lfs_unmount(&filesystem);
    mounted = false;
    memset(&filesystem, 0, sizeof filesystem);
    if (!configure()) return false;
    /* Unknown, damaged and erased media all stay untouched. In particular,
     * never fall back to formatting a pre-existing CircuitPython FAT volume. */
    mounted = lfs_mount(&filesystem, &config) == 0;
    return mounted;
}

bool bosun_store_ready(void) { return mounted; }

bosun_store_result_t bosun_store_format(void) {
    if (mounted) lfs_unmount(&filesystem);
    mounted = false;
    memset(&filesystem, 0, sizeof filesystem);
    if (!configure()) return BOSUN_STORE_UNAVAILABLE;
    int error = lfs_format(&filesystem, &config);
    if (error) return result(error);
    error = lfs_mount(&filesystem, &config);
    mounted = error == 0;
    return result(error);
}

static bosun_store_result_t read_file(const char *path, uint32_t offset, void *buffer,
                                      size_t capacity, size_t *length, bool complete) {
    if (length) *length = 0;
    if (!length || (!buffer && capacity) || !bosun_store_safe_path(path))
        return BOSUN_STORE_INVALID;
    if (!mounted) return BOSUN_STORE_UNAVAILABLE;
    lfs_file_t file;
    int error = lfs_file_opencfg(&filesystem, &file, path, LFS_O_RDONLY, &file_config);
    if (error) return result(error);
    lfs_soff_t size = lfs_file_size(&filesystem, &file);
    if (size < 0) error = (int)size;
    else if ((uint32_t)size > BOSUN_STORE_FILE_MAX || (complete && (size_t)size > capacity))
        error = LFS_ERR_FBIG;
    else if (offset < (uint32_t)size && capacity) {
        size_t amount = (uint32_t)size - offset;
        if (amount > capacity) amount = capacity;
        lfs_soff_t position = lfs_file_seek(&filesystem, &file, (lfs_soff_t)offset, LFS_SEEK_SET);
        if (position < 0) error = (int)position;
        else {
            lfs_ssize_t got = lfs_file_read(&filesystem, &file, buffer, (lfs_size_t)amount);
            if (got < 0) error = (int)got;
            else if ((size_t)got != amount) error = LFS_ERR_IO;
            else *length = amount;
        }
    }
    int close_error = lfs_file_close(&filesystem, &file);
    if (!error) error = close_error;
    if (error) *length = 0;
    return result(error);
}

bosun_store_result_t bosun_store_read(const char *path, void *buffer, size_t capacity, size_t *length) {
    return read_file(path, 0, buffer, capacity, length, true);
}

bosun_store_result_t bosun_store_read_at(const char *path, uint32_t offset, void *buffer,
                                       size_t capacity, size_t *length) {
    return read_file(path, offset, buffer, capacity, length, false);
}

bosun_store_result_t bosun_store_write_atomic(const char *path, const void *data, size_t length) {
    if (!bosun_store_safe_path(path) || (!data && length) || strcmp(path, "/") == 0)
        return BOSUN_STORE_INVALID;
    if (length > BOSUN_STORE_FILE_MAX) return BOSUN_STORE_LIMIT;
    if (!mounted) return BOSUN_STORE_UNAVAILABLE;
    char temporary[BOSUN_PATH_MAX + sizeof temporary_name];
    const char *slash = strrchr(path, '/');
    size_t prefix = slash ? (size_t)(slash - path + 1) : 0;
    memcpy(temporary, path, prefix);
    memcpy(temporary + prefix, temporary_name, sizeof temporary_name);
    int error = lfs_remove(&filesystem, temporary);
    if (error && error != LFS_ERR_NOENT) return result(error);
    lfs_file_t file;
    error = lfs_file_opencfg(&filesystem, &file, temporary,
                           LFS_O_WRONLY | LFS_O_CREAT | LFS_O_EXCL, &file_config);
    if (error) return result(error);
    if (length) {
        lfs_ssize_t wrote = lfs_file_write(&filesystem, &file, data, (lfs_size_t)length);
        if (wrote < 0) error = (int)wrote;
        else if ((size_t)wrote != length) error = LFS_ERR_IO;
    }
    if (!error) error = lfs_file_sync(&filesystem, &file);
    int close_error = lfs_file_close(&filesystem, &file);
    if (!error) error = close_error;
    /* littlefs commits rename atomically, including the directory metadata.
     * Do not remove the original file first (that creates a power-loss gap). */
    if (!error) error = lfs_rename(&filesystem, temporary, path);
    if (error) (void)lfs_remove(&filesystem, temporary);
    return result(error);
}

bosun_store_result_t bosun_store_remove(const char *path) {
    if (!bosun_store_safe_path(path) || strcmp(path, "/") == 0) return BOSUN_STORE_INVALID;
    if (!mounted) return BOSUN_STORE_UNAVAILABLE;
    return result(lfs_remove(&filesystem, path));
}

bosun_store_result_t bosun_store_mkdir(const char *path) {
    if (!bosun_store_safe_path(path)) return BOSUN_STORE_INVALID;
    if (!mounted) return BOSUN_STORE_UNAVAILABLE;
    if (strcmp(path, "/") == 0) return BOSUN_STORE_OK;
    char part[BOSUN_PATH_MAX];
    strcpy(part, path);
    for (char *p = part + (part[0] == '/');; ++p) {
        if (*p && *p != '/') continue;
        char delimiter = *p;
        *p = '\0';
        int error = lfs_mkdir(&filesystem, part);
        if (error == LFS_ERR_EXIST) {
            struct lfs_info info;
            error = lfs_stat(&filesystem, part, &info);
            if (!error && info.type != LFS_TYPE_DIR) error = LFS_ERR_NOTDIR;
        }
        if (error) return result(error);
        if (!delimiter) break;
        *p = delimiter;
    }
    return BOSUN_STORE_OK;
}

bosun_store_result_t bosun_store_list(const char *path, bosun_dirent_t *entries,
                                    size_t capacity, size_t *count) {
    if (count) *count = 0;
    if (!count || (!entries && capacity) || !bosun_store_safe_path(path))
        return BOSUN_STORE_INVALID;
    if (capacity > BOSUN_STORE_LIST_MAX) return BOSUN_STORE_LIMIT;
    if (!mounted) return BOSUN_STORE_UNAVAILABLE;
    lfs_dir_t directory;
    int error = lfs_dir_open(&filesystem, &directory, path);
    if (error) return result(error);
    struct lfs_info info;
    size_t used = 0;
    while ((error = lfs_dir_read(&filesystem, &directory, &info)) > 0) {
        if (!strcmp(info.name, ".") || !strcmp(info.name, "..") ||
            !strcmp(info.name, temporary_name)) continue;
        if (used == capacity || strlen(info.name) >= BOSUN_NAME_MAX) { error = LFS_ERR_NOMEM; break; }
        strcpy(entries[used].name, info.name);
        entries[used].directory = info.type == LFS_TYPE_DIR;
        entries[used].size = entries[used].directory ? 0 : info.size;
        ++used;
    }
    int close_error = lfs_dir_close(&filesystem, &directory);
    if (!error) error = close_error;
    if (!error) *count = used;
    return result(error);
}
