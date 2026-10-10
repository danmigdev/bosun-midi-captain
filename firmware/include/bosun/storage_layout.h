#ifndef BOSUN_STORAGE_LAYOUT_H
#define BOSUN_STORAGE_LAYOUT_H
#include <stdint.h>
enum { BOSUN_STORAGE_BYTES = 4 * 1024 * 1024, BOSUN_LEGACY_STORAGE_BYTES = 512 * 1024 };
/* The old 512 KiB stays at the physical end of flash. New logical blocks
 * extend into the preceding reserve; existing blocks never move. */
static inline uint32_t bosun_storage_physical_offset(uint32_t base, uint32_t size, uint32_t logical) {
    return logical < BOSUN_LEGACY_STORAGE_BYTES
        ? base + size - BOSUN_LEGACY_STORAGE_BYTES + logical
        : base + logical - BOSUN_LEGACY_STORAGE_BYTES;
}
#endif
