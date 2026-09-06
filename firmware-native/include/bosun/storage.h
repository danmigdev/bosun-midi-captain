#ifndef BOSUN_STORAGE_H
#define BOSUN_STORAGE_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define BOSUN_PATH_MAX 160
#define BOSUN_NAME_MAX 64
#define BOSUN_STORE_FILE_MAX (256u * 1024u)
#define BOSUN_STORE_LIST_MAX 128u
typedef enum {
    BOSUN_STORE_OK, BOSUN_STORE_NOT_FOUND, BOSUN_STORE_UNAVAILABLE,
    BOSUN_STORE_INVALID, BOSUN_STORE_LIMIT, BOSUN_STORE_IO
} bosun_store_result_t;
typedef struct { char name[BOSUN_NAME_MAX]; bool directory; uint32_t size; } bosun_dirent_t;

/* Single-threaded API, no dynamic allocation on hardware. Paths are UTF-8
 * byte strings, absolute or relative to the storage root; at most 159 bytes
 * with components at most 63 bytes. Empty components, traversal, controls,
 * backslashes, colons and the private name .bosun-atomic.tmp are rejected.
 * Bytes/JSON are preserved verbatim: the layout is the CP /config tree.
 * Host root must be an existing POSIX directory; symlinks are not followed.
 * Mount NEVER formats, creates a root, or converts an unknown CP filesystem.
 * Explicit format is destructive, and mounts the resulting empty filesystem. */
bool bosun_store_mount(const char *host_root);
bool bosun_store_ready(void);
bosun_store_result_t bosun_store_format(void);
/* read is all-or-error (LIMIT if capacity is insufficient); no implicit NUL.
 * read_at returns a bounded slice, and OK/length=0 at or beyond EOF. Both
 * reset length to zero on failure. Files are bounded to FILE_MAX bytes. */
bosun_store_result_t bosun_store_read(const char *path, void *buffer, size_t capacity, size_t *length);
bosun_store_result_t bosun_store_read_at(const char *path, uint32_t offset, void *buffer, size_t capacity, size_t *length);
/* Parent must exist. Sync temporary file, atomically replace, sync directory.
 * An interrupted write leaves the old or new complete file, never a partial
 * destination. A stale private temporary file is reclaimed on the next write. */
bosun_store_result_t bosun_store_write_atomic(const char *path, const void *data, size_t length);
bosun_store_result_t bosun_store_remove(const char *path);
/* mkdir creates missing ancestors, and succeeds for an existing directory. */
bosun_store_result_t bosun_store_mkdir(const char *path);
/* Unordered complete listing; capacity <= LIST_MAX. On any error, including
 * LIMIT, count is zero: callers must not consume partially filled entries.
 * Private transaction files and dot entries are omitted. */
bosun_store_result_t bosun_store_list(const char *path, bosun_dirent_t *entries, size_t capacity, size_t *count);
bool bosun_store_safe_path(const char *path);
#endif
