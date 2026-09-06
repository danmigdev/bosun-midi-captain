#define _POSIX_C_SOURCE 200809L
#include "storage_cases.h"
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <unistd.h>

static void write_external(const char *path, const char *text) {
    FILE *file = fopen(path, "wb");
    assert(file && fwrite(text, 1, strlen(text), file) == strlen(text));
    assert(fclose(file) == 0);
}

int main(void) {
    test_paths();
    char buffer[512], path[512], other_path[512], root[] = "/tmp/bosun-storage-XXXXXX";
    char outside[] = "/tmp/bosun-storage-outside-XXXXXX";
    size_t length = 4, count = 4;
    bosun_dirent_t entries[16];
    assert(!bosun_store_ready());
    assert(bosun_store_read("/config", buffer, sizeof buffer, &length) == BOSUN_STORE_UNAVAILABLE && length == 0);
    assert(bosun_store_format() == BOSUN_STORE_UNAVAILABLE);
    assert(mkdtemp(root) && mkdtemp(outside));
    snprintf(path, sizeof path, "%s/missing", root);
    assert(!bosun_store_mount(path) && access(path, F_OK) != 0);
    snprintf(path, sizeof path, "%s/unknown-volume", root);
    write_external(path, "existing bytes remain");
    assert(bosun_store_mount(root));
    assert(bosun_store_read("/unknown-volume", buffer, sizeof buffer, &length) == BOSUN_STORE_OK && length == 21);
    assert(bosun_store_format() == BOSUN_STORE_OK);
    test_storage_api();
    assert(bosun_store_mount(root));
    assert(bosun_store_read("/config/profiles/kemper_player_buk4/device.json", buffer, sizeof buffer, &length) == BOSUN_STORE_OK && length > 80);

    /* A real short write followed by EFBIG must not replace the destination. */
    assert(bosun_store_write_atomic("/config/atomic", "original", 8) == BOSUN_STORE_OK);
    struct rlimit original_limit, short_limit;
    assert(getrlimit(RLIMIT_FSIZE, &original_limit) == 0);
    short_limit = original_limit; short_limit.rlim_cur = 64;
    void (*previous_handler)(int) = signal(SIGXFSZ, SIG_IGN);
    assert(previous_handler != SIG_ERR && setrlimit(RLIMIT_FSIZE, &short_limit) == 0);
    memset(buffer, 'x', sizeof buffer);
    assert(bosun_store_write_atomic("/config/atomic", buffer, sizeof buffer) == BOSUN_STORE_LIMIT);
    assert(setrlimit(RLIMIT_FSIZE, &original_limit) == 0);
    assert(signal(SIGXFSZ, previous_handler) != SIG_ERR);
    assert(bosun_store_read("/config/atomic", buffer, sizeof buffer, &length) == BOSUN_STORE_OK && length == 8 && !memcmp(buffer, "original", 8));

    /* Host links cannot redirect reads, mkdir or writes outside the root. */
    snprintf(other_path, sizeof other_path, "%s/secret", outside);
    write_external(other_path, "outside unchanged");
    snprintf(path, sizeof path, "%s/escape", root);
    assert(symlink(outside, path) == 0);
    assert(bosun_store_read("/escape/secret", buffer, sizeof buffer, &length) == BOSUN_STORE_INVALID);
    assert(bosun_store_write_atomic("/escape/secret", "bad", 3) == BOSUN_STORE_INVALID);
    assert(bosun_store_mkdir("/escape/created") == BOSUN_STORE_INVALID);
    assert(bosun_store_remove("/escape") == BOSUN_STORE_INVALID);
    assert(bosun_store_list("/", entries, 16, &count) == BOSUN_STORE_INVALID && count == 0);
    assert(!bosun_store_mount(path));
    strcat(path, "/");
    assert(!bosun_store_mount(path));
    assert(bosun_store_mount(root));
    snprintf(path, sizeof path, "%s/link", root);
    assert(symlink(other_path, path) == 0);
    assert(bosun_store_read("/link", buffer, sizeof buffer, &length) == BOSUN_STORE_INVALID);
    assert(bosun_store_write_atomic("/link", "bad", 3) == BOSUN_STORE_INVALID);
    snprintf(path, sizeof path, "%s/hardlink", root);
    assert(link(other_path, path) == 0);
    assert(bosun_store_read("/hardlink", buffer, sizeof buffer, &length) == BOSUN_STORE_INVALID);
    assert(bosun_store_write_atomic("/hardlink", "bad", 3) == BOSUN_STORE_INVALID);
    snprintf(path, sizeof path, "%s/config/.bosun-atomic.tmp", root);
    assert(symlink(other_path, path) == 0);
    assert(bosun_store_write_atomic("/config/atomic", "replacement", 11) == BOSUN_STORE_OK);
    assert(bosun_store_read("/config/atomic", buffer, sizeof buffer, &length) == BOSUN_STORE_OK && length == 11);
    snprintf(path, sizeof path, "%s/pipe", root);
    assert(mkfifo(path, 0600) == 0);
    assert(bosun_store_read("/pipe", buffer, sizeof buffer, &length) == BOSUN_STORE_INVALID);
    snprintf(path, sizeof path, "%s/oversized", root);
    int oversized = open(path, O_WRONLY | O_CREAT | O_EXCL, 0600);
    assert(oversized >= 0 && ftruncate(oversized, BOSUN_STORE_FILE_MAX + 1u) == 0 && close(oversized) == 0);
    assert(bosun_store_read_at("/oversized", 0, buffer, 1, &length) == BOSUN_STORE_LIMIT && length == 0);

    /* Explicit host format removes only descendants, including link objects. */
    assert(bosun_store_format() == BOSUN_STORE_OK);
    assert(bosun_store_list("/", NULL, 0, &count) == BOSUN_STORE_OK && count == 0);
    FILE *file = fopen(other_path, "rb");
    assert(file && fread(buffer, 1, sizeof buffer, file) == 17 && !memcmp(buffer, "outside unchanged", 17));
    assert(fclose(file) == 0);
    assert(!bosun_store_mount(NULL));
    assert(unlink(other_path) == 0 && rmdir(outside) == 0 && rmdir(root) == 0);
    puts("Host storage: CP paths/raw JSON, remount, bounded reads/lists, failed atomic writes, symlink/hardlink containment and explicit format passed");
    return 0;
}
