#ifndef BOSUN_USB_DIAGNOSTICS_H
#define BOSUN_USB_DIAGNOSTICS_H
#include "bosun/board.h"

/* Shared by real CDC and the loopback board; bounded by each I/O call. */
static inline void bosun_usb_diagnostics_reset(bosun_board_usb_diagnostics_t *stats,
                                              uint32_t generation) {
    *stats = (bosun_board_usb_diagnostics_t){
        .generation = generation,
        .rx_fnv1a = UINT32_C(2166136261), .tx_fnv1a = UINT32_C(2166136261),
    };
}

static inline void bosun_usb_diagnostics_add(uint32_t *count, uint32_t *hash,
                                            const uint8_t *data, size_t length) {
    *count += (uint32_t)length;
    uint32_t next = *hash;
    for (size_t i = 0; i < length; ++i) next = (next ^ data[i]) * UINT32_C(16777619);
    *hash = next;
}
#endif
