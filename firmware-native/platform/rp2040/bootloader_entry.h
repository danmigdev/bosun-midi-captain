#ifndef BOSUN_BOOTLOADER_ENTRY_H
#define BOSUN_BOOTLOADER_ENTRY_H
#include <stdbool.h>
#include <stdint.h>

/* Physical switch order: 1,2,3,4,UP,A,B,C,D,DOWN. Only evaluated at boot.
 * Releasing switch 1 or pressing another cancels this boot's request. */
enum { BOSUN_BOOTLOADER_CHORD = (1u << 0), BOSUN_BOOTLOADER_HOLD_MS = 3000 };
static inline bool bosun_bootloader_held(uint16_t (*read_switches)(void),
                                       void (*wait_ms)(uint32_t)) {
    if (read_switches() != BOSUN_BOOTLOADER_CHORD) return false;
    for (unsigned elapsed = 0; elapsed < BOSUN_BOOTLOADER_HOLD_MS; elapsed += 10) {
        wait_ms(10);
        if (read_switches() != BOSUN_BOOTLOADER_CHORD) return false;
    }
    return true;
}
#endif
