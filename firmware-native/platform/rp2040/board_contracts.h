/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef BOSUN_BOARD_CONTRACTS_H
#define BOSUN_BOARD_CONTRACTS_H
#include "bosun/board.h"

enum { BOSUN_STORAGE_BYTES = 512 * 1024 };

typedef struct { uint64_t consumed; uint32_t dropped; } bosun_dma_rx_tracker_t;

static inline uint64_t bosun_dma_rx_produced(uint64_t completed, uint32_t remaining) {
    return completed + ((uint64_t)UINT32_MAX - remaining);
}

/* DMA writes a circular buffer, but its remaining transfer count is linear.
 * Track the absolute producer count so one or several complete laps cannot
 * masquerade as an empty buffer merely because the write address repeats. */
static inline size_t bosun_dma_rx_available(bosun_dma_rx_tracker_t *tracker,
                                           uint64_t produced, uint32_t capacity) {
    uint64_t pending = produced - tracker->consumed;
    if (pending > capacity) {
        tracker->dropped += (uint32_t)(pending - capacity);
        tracker->consumed = produced - capacity;
        pending = capacity;
    }
    return (size_t)pending;
}

/* Shared with the real driver so the host checks exercise the arithmetic
 * used before every physical flash access and display transfer. */
static inline bool bosun_flash_range(uint32_t flash_size, uint32_t offset,
                                     size_t length, uint32_t alignment) {
    return flash_size >= BOSUN_STORAGE_BYTES && alignment != 0 &&
           offset >= flash_size - BOSUN_STORAGE_BYTES && offset <= flash_size &&
           length <= flash_size - offset && offset % alignment == 0 &&
           length % alignment == 0;
}

static inline bool bosun_clip_rect(int32_t *x, int32_t *y, uint32_t *width,
                                   uint32_t *height, uint32_t *skip_x,
                                   uint32_t *skip_y) {
    *skip_x = *x < 0 ? (uint32_t)-(int64_t)*x : 0;
    *skip_y = *y < 0 ? (uint32_t)-(int64_t)*y : 0;
    if (*skip_x >= *width || *skip_y >= *height ||
        *x >= BOSUN_DISPLAY_WIDTH || *y >= BOSUN_DISPLAY_HEIGHT) return false;
    *width -= *skip_x;
    *height -= *skip_y;
    if (*x < 0) *x = 0;
    if (*y < 0) *y = 0;
    if (*width > BOSUN_DISPLAY_WIDTH - (uint32_t)*x)
        *width = BOSUN_DISPLAY_WIDTH - (uint32_t)*x;
    if (*height > BOSUN_DISPLAY_HEIGHT - (uint32_t)*y)
        *height = BOSUN_DISPLAY_HEIGHT - (uint32_t)*y;
    return *width != 0 && *height != 0;
}
#endif
