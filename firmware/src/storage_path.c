#include "bosun/storage.h"
#include <string.h>

bool bosun_store_safe_path(const char *path) {
    if (!path || !*path) return false;
    size_t total = 0, component = 0;
    const char *start = path;
    if (*start == '/') { ++start; ++total; }
    if (!*start) return total == 1;
    for (const char *p = start;; ++p) {
        unsigned char c = (unsigned char)*p;
        if (c == '/' || c == 0) {
            if (component == 0 || component >= BOSUN_NAME_MAX ||
                (component == 1 && start[0] == '.') ||
                (component == 2 && start[0] == '.' && start[1] == '.') ||
                (component == sizeof ".bosun-atomic.tmp" - 1 &&
                 memcmp(start, ".bosun-atomic.tmp", component) == 0)) return false;
            if (c == 0) return true;
            component = 0;
            start = p + 1;
        } else {
            if (c < 32 || c == 127 || c == '\\' || c == ':') return false;
            ++component;
        }
        if (++total >= BOSUN_PATH_MAX) return false;
    }
}
