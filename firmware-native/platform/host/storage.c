#define _POSIX_C_SOURCE 200809L
#include "bosun/storage.h"
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static int root_fd = -1;
static const char temporary_name[] = ".bosun-atomic.tmp";

static bosun_store_result_t result(int error) {
    switch (error) {
    case 0: return BOSUN_STORE_OK;
    case ENOENT: return BOSUN_STORE_NOT_FOUND;
    case ENOSPC: case EFBIG: case ENAMETOOLONG: case EDQUOT: return BOSUN_STORE_LIMIT;
    case EINVAL: case ELOOP: case ENOTDIR: case EISDIR:
    case ENOTEMPTY: case EEXIST: return BOSUN_STORE_INVALID;
    default: return BOSUN_STORE_IO;
    }
}

/* Resolve one component at a time from an owned directory descriptor. A
 * realpath + string-prefix check would race directory/symlink replacements. */
static int directory_fd(const char *path, bool create) {
    int current = openat(root_fd, ".", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (current < 0) return -1;
    const char *start = path + (path[0] == '/');
    while (*start) {
        const char *slash = strchr(start, '/');
        size_t length = slash ? (size_t)(slash - start) : strlen(start);
        char name[BOSUN_NAME_MAX];
        memcpy(name, start, length);
        name[length] = '\0';
        int next = openat(current, name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        if (next < 0 && errno == ENOENT && create) {
            if (mkdirat(current, name, 0700) == 0) {
                if (fsync(current) != 0) { int error = errno; close(current); errno = error; return -1; }
            } else if (errno != EEXIST) { int error = errno; close(current); errno = error; return -1; }
            next = openat(current, name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        }
        int error = errno;
        close(current);
        if (next < 0) { errno = error; return -1; }
        current = next;
        if (!slash) break;
        start = slash + 1;
    }
    return current;
}

static int parent_fd(const char *path, char *name) {
    const char *slash = strrchr(path, '/');
    const char *leaf = slash ? slash + 1 : path;
    if (!*leaf) { errno = EINVAL; return -1; }
    strcpy(name, leaf);
    if (!slash || slash == path) return openat(root_fd, ".", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    char parent[BOSUN_PATH_MAX];
    size_t length = (size_t)(slash - path);
    memcpy(parent, path, length);
    parent[length] = '\0';
    return directory_fd(parent, false);
}

bool bosun_store_mount(const char *host_root) {
    if (root_fd >= 0) close(root_fd);
    root_fd = -1;
    if (!host_root || !*host_root) return false;
    /* Linux treats "symlink/" as directory traversal even with O_NOFOLLOW;
     * normalize trailing separators before checking the final root object. */
    char target[PATH_MAX];
    size_t length = strnlen(host_root, sizeof target);
    if (length == sizeof target) return false;
    memcpy(target, host_root, length + 1);
    while (length > 1 && target[length - 1] == '/') target[--length] = '\0';
    root_fd = open(target, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    return root_fd >= 0;
}

bool bosun_store_ready(void) { return root_fd >= 0; }

/* Explicit format only. Directory handles remain beneath the chosen root;
 * symlinks are unlinked, never opened or traversed. The depth bound also
 * covers a host root populated outside the public path-limited API. */
static int clear_directory(int fd, unsigned depth) {
    if (depth > BOSUN_PATH_MAX / 2) return EINVAL;
    int scan = openat(fd, ".", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (scan < 0) return errno;
    DIR *directory = fdopendir(scan);
    if (!directory) { int error = errno; close(scan); return error; }
    int error = 0;
    while (!error) {
        errno = 0;
        struct dirent *entry = readdir(directory);
        if (!entry) { error = errno; break; }
        if (!strcmp(entry->d_name, ".") || !strcmp(entry->d_name, "..")) continue;
        struct stat st;
        if (fstatat(fd, entry->d_name, &st, AT_SYMLINK_NOFOLLOW) != 0) { error = errno; break; }
        if (S_ISDIR(st.st_mode)) {
            int child = openat(fd, entry->d_name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
            if (child < 0) { error = errno; break; }
            error = clear_directory(child, depth + 1);
            close(child);
            if (!error && unlinkat(fd, entry->d_name, AT_REMOVEDIR) != 0) error = errno;
        } else if (unlinkat(fd, entry->d_name, 0) != 0) error = errno;
    }
    if (closedir(directory) != 0 && !error) error = errno;
    if (!error && fsync(fd) != 0) error = errno;
    return error;
}

bosun_store_result_t bosun_store_format(void) {
    return root_fd < 0 ? BOSUN_STORE_UNAVAILABLE : result(clear_directory(root_fd, 0));
}

static bosun_store_result_t read_file(const char *path, uint32_t offset, void *buffer,
                                      size_t capacity, size_t *length, bool complete) {
    if (length) *length = 0;
    if (!length || (!buffer && capacity) || !bosun_store_safe_path(path)) return BOSUN_STORE_INVALID;
    if (root_fd < 0) return BOSUN_STORE_UNAVAILABLE;
    char name[BOSUN_NAME_MAX];
    int parent = parent_fd(path, name);
    if (parent < 0) return result(errno);
    int fd = openat(parent, name, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC);
    int error = fd < 0 ? errno : 0;
    close(parent);
    if (fd < 0) return result(error);
    struct stat st;
    if (fstat(fd, &st) != 0) error = errno;
    else if (!S_ISREG(st.st_mode) || st.st_nlink != 1) error = EINVAL;
    else if (st.st_size < 0 || (uint64_t)st.st_size > BOSUN_STORE_FILE_MAX ||
             (complete && (uint64_t)st.st_size > capacity)) error = EFBIG;
    else if ((uint64_t)st.st_size > offset) {
        size_t amount = (size_t)((uint64_t)st.st_size - offset);
        if (amount > capacity) amount = capacity;
        while (!error && *length < amount) {
            ssize_t got = pread(fd, (uint8_t *)buffer + *length, amount - *length,
                                (off_t)offset + (off_t)*length);
            if (got < 0 && errno == EINTR) continue;
            if (got <= 0) error = got < 0 ? errno : EIO;
            else *length += (size_t)got;
        }
    }
    if (close(fd) != 0 && !error) error = errno;
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
    if (!bosun_store_safe_path(path) || (!data && length)) return BOSUN_STORE_INVALID;
    if (length > BOSUN_STORE_FILE_MAX) return BOSUN_STORE_LIMIT;
    if (root_fd < 0) return BOSUN_STORE_UNAVAILABLE;
    char name[BOSUN_NAME_MAX];
    int parent = parent_fd(path, name);
    if (parent < 0) return result(errno);
    int error = 0;
    struct stat st;
    if (fstatat(parent, name, &st, AT_SYMLINK_NOFOLLOW) == 0) {
        if (!S_ISREG(st.st_mode) || st.st_nlink != 1) error = EINVAL;
    } else if (errno != ENOENT) error = errno;
    if (!error && unlinkat(parent, temporary_name, 0) != 0 && errno != ENOENT) error = errno;
    int fd = -1;
    if (!error) {
        fd = openat(parent, temporary_name, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
        if (fd < 0) error = errno;
    }
    size_t written = 0;
    while (!error && written < length) {
        size_t amount = length - written;
        if (amount > 4096) amount = 4096;
        ssize_t done = write(fd, (const uint8_t *)data + written, amount);
        if (done < 0 && errno == EINTR) continue;
        if (done <= 0) error = done < 0 ? errno : EIO;
        else written += (size_t)done;
    }
    if (fd >= 0) {
        if (!error && fsync(fd) != 0) error = errno;
        if (close(fd) != 0 && !error) error = errno;
    }
    if (!error && renameat(parent, temporary_name, parent, name) != 0) error = errno;
    if (!error && fsync(parent) != 0) error = errno;
    if (error) (void)unlinkat(parent, temporary_name, 0);
    close(parent);
    return result(error);
}

bosun_store_result_t bosun_store_remove(const char *path) {
    if (!bosun_store_safe_path(path)) return BOSUN_STORE_INVALID;
    if (root_fd < 0) return BOSUN_STORE_UNAVAILABLE;
    char name[BOSUN_NAME_MAX];
    int parent = parent_fd(path, name);
    if (parent < 0) return result(errno);
    int error = 0;
    struct stat st;
    if (fstatat(parent, name, &st, AT_SYMLINK_NOFOLLOW) != 0) error = errno;
    else if (!S_ISDIR(st.st_mode) && (!S_ISREG(st.st_mode) || st.st_nlink != 1)) error = EINVAL;
    else if (unlinkat(parent, name, S_ISDIR(st.st_mode) ? AT_REMOVEDIR : 0) != 0) error = errno;
    if (!error && fsync(parent) != 0) error = errno;
    close(parent);
    return result(error);
}

bosun_store_result_t bosun_store_mkdir(const char *path) {
    if (!bosun_store_safe_path(path)) return BOSUN_STORE_INVALID;
    if (root_fd < 0) return BOSUN_STORE_UNAVAILABLE;
    int fd = directory_fd(path, true);
    if (fd < 0) return result(errno);
    return close(fd) == 0 ? BOSUN_STORE_OK : result(errno);
}

bosun_store_result_t bosun_store_list(const char *path, bosun_dirent_t *entries,
                                    size_t capacity, size_t *count) {
    if (count) *count = 0;
    if (!count || (!entries && capacity) || !bosun_store_safe_path(path)) return BOSUN_STORE_INVALID;
    if (capacity > BOSUN_STORE_LIST_MAX) return BOSUN_STORE_LIMIT;
    if (root_fd < 0) return BOSUN_STORE_UNAVAILABLE;
    int fd = directory_fd(path, false);
    if (fd < 0) return result(errno);
    DIR *directory = fdopendir(fd);
    if (!directory) { int error = errno; close(fd); return result(error); }
    int error = 0;
    size_t used = 0;
    while (!error) {
        errno = 0;
        struct dirent *entry = readdir(directory);
        if (!entry) { error = errno; break; }
        if (!strcmp(entry->d_name, ".") || !strcmp(entry->d_name, "..") ||
            !strcmp(entry->d_name, temporary_name)) continue;
        if (used == capacity || strlen(entry->d_name) >= BOSUN_NAME_MAX) { error = EFBIG; break; }
        struct stat st;
        if (fstatat(fd, entry->d_name, &st, AT_SYMLINK_NOFOLLOW) != 0) { error = errno; break; }
        if (!S_ISDIR(st.st_mode) && (!S_ISREG(st.st_mode) || st.st_nlink != 1)) { error = EINVAL; break; }
        if (S_ISREG(st.st_mode) && (uint64_t)st.st_size > BOSUN_STORE_FILE_MAX) { error = EFBIG; break; }
        strcpy(entries[used].name, entry->d_name);
        entries[used].directory = S_ISDIR(st.st_mode);
        entries[used].size = S_ISDIR(st.st_mode) ? 0 : (uint32_t)st.st_size;
        ++used;
    }
    if (closedir(directory) != 0 && !error) error = errno;
    if (!error) *count = used;
    return result(error);
}
