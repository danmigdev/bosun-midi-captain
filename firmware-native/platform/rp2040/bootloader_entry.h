#ifndef BOSUN_BOOTLOADER_ENTRY_H
#define BOSUN_BOOTLOADER_ENTRY_H
#include <stdbool.h>
#include <stdint.h>

/* Board bit order: 1,2,3,4,UP,A,B,C,D,DOWN. Only evaluated at boot, before the
 * hardware record is readable, so only the switches every supported model
 * wires (1,2,3,A,B,C) take part: unwired pins can never block recovery.
 * Releasing switch 1 or pressing another scanned switch cancels the request. */
enum { BOSUN_BOOTLOADER_CHORD = (1u << 0), BOSUN_BOOTLOADER_SCAN = 0x00e7u,
       BOSUN_BOOTLOADER_HOLD_MS = 3000 };
static inline bool bosun_bootloader_held(uint16_t (*read_switches)(void),
                                       void (*wait_ms)(uint32_t)) {
    if ((read_switches() & BOSUN_BOOTLOADER_SCAN) != BOSUN_BOOTLOADER_CHORD) return false;
    for (unsigned elapsed = 0; elapsed < BOSUN_BOOTLOADER_HOLD_MS; elapsed += 10) {
        wait_ms(10);
        if ((read_switches() & BOSUN_BOOTLOADER_SCAN) != BOSUN_BOOTLOADER_CHORD) return false;
    }
    return true;
}
#endif
