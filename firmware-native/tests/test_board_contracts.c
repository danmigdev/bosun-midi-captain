#include "board_contracts.h"
#include <assert.h>
#include <limits.h>
#include <stdio.h>

static void flash_bounds(void) {
    const uint32_t flash = 2 * 1024 * 1024, start = flash - BOSUN_STORAGE_BYTES;
    assert(bosun_flash_range(flash, start, BOSUN_STORAGE_BYTES, 4096));
    assert(bosun_flash_range(flash, flash - 256, 256, 256));
    assert(bosun_flash_range(flash, flash - 1, 1, 1));
    assert(bosun_flash_range(flash, flash, 0, 256));
    assert(!bosun_flash_range(flash, start - 256, 256, 256));
    assert(!bosun_flash_range(flash, flash - 256, 512, 256));
    assert(!bosun_flash_range(flash, flash, 1, 1));
    assert(!bosun_flash_range(flash, UINT32_MAX, 2, 1));
    assert(!bosun_flash_range(flash, start, SIZE_MAX, 1));
    assert(!bosun_flash_range(flash, start + 1, 256, 256));
    assert(!bosun_flash_range(flash, start, 257, 256));
    assert(!bosun_flash_range(flash, start + 256, 4096, 4096));
    assert(!bosun_flash_range(flash, start, 4095, 4096));
    assert(!bosun_flash_range(flash, start, 0, 0));
    assert(!bosun_flash_range(1024, 0, 1, 1));
}

static void rectangle_clipping(void) {
    int32_t x = -3, y = -2;
    uint32_t w = 8, h = 9, sx, sy;
    assert(bosun_clip_rect(&x, &y, &w, &h, &sx, &sy));
    assert(x == 0 && y == 0 && w == 5 && h == 7 && sx == 3 && sy == 2);
    x = 238; y = 239; w = 8; h = 9;
    assert(bosun_clip_rect(&x, &y, &w, &h, &sx, &sy));
    assert(w == 2 && h == 1 && sx == 0 && sy == 0);
    x = -5; y = 0; w = 5; h = 1;
    assert(!bosun_clip_rect(&x, &y, &w, &h, &sx, &sy));
    x = 240; y = 0; w = 1; h = 1;
    assert(!bosun_clip_rect(&x, &y, &w, &h, &sx, &sy));
    x = INT32_MIN; y = INT32_MIN; w = UINT16_MAX; h = UINT16_MAX;
    assert(!bosun_clip_rect(&x, &y, &w, &h, &sx, &sy));
    /* Exhaustive edge positions: every retained destination pixel corresponds
     * to exactly the same source coordinate as before clipping. */
    for (int32_t left = -260; left <= 260; ++left) {
        x = left; y = left; w = 240; h = 240;
        bool visible = bosun_clip_rect(&x, &y, &w, &h, &sx, &sy);
        assert(visible == (left > -240 && left < 240));
        if (visible) {
            assert(x >= 0 && y >= 0 && x + w <= 240 && y + h <= 240);
            assert((int64_t)left + sx == x && (int64_t)left + sy == y);
        }
    }
}

static void dma_ring_counter(void) {
    enum { CAPACITY = 2048 };
    uint8_t ring[CAPACITY];
    bosun_dma_rx_tracker_t tracker = {0};
    uint64_t completed = 0;
    uint32_t remaining = UINT32_MAX;
    assert(bosun_dma_rx_available(&tracker, bosun_dma_rx_produced(completed, remaining), CAPACITY) == 0);
    /* Producer completes more than one whole ring before the consumer polls. */
    for (uint32_t byte = 0; byte < 2 * CAPACITY + 317; ++byte) {
        ring[byte & (CAPACITY - 1)] = (uint8_t)(byte * 29u + 17u);
        --remaining;
    }
    uint64_t produced = bosun_dma_rx_produced(completed, remaining);
    assert(bosun_dma_rx_available(&tracker, produced, CAPACITY) == CAPACITY);
    assert(tracker.dropped == CAPACITY + 317);
    for (unsigned i = 0; i < 900; ++i) {
        assert(ring[tracker.consumed & (CAPACITY - 1)] == (uint8_t)(tracker.consumed * 29u + 17u));
        ++tracker.consumed;
    }
    assert(bosun_dma_rx_available(&tracker, produced, CAPACITY) == CAPACITY - 900);
    assert(tracker.dropped == CAPACITY + 317);
    /* An exact full lap is full, not empty as a write-address-only scheme
     * would claim; the partial read position remains meaningful. */
    remaining -= CAPACITY;
    produced = bosun_dma_rx_produced(completed, remaining);
    assert(bosun_dma_rx_available(&tracker, produced, CAPACITY) == CAPACITY);
    assert(tracker.dropped == 2 * CAPACITY + 317 - 900);
    /* Re-arming the DMA transfer counter preserves its absolute producer.
     * Crossing 2^32 bytes cannot appear as a backwards/empty producer. */
    remaining = 8000;
    completed = bosun_dma_rx_produced(completed, remaining);
    uint64_t before = completed;
    remaining = UINT32_MAX;
    assert(bosun_dma_rx_produced(completed, remaining) == before);
    remaining -= 16000;
    produced = bosun_dma_rx_produced(completed, remaining);
    assert(produced == before + 16000 && produced > UINT32_MAX);
    tracker.consumed = produced - 73;
    assert(bosun_dma_rx_available(&tracker, produced, CAPACITY) == 73);
}
int main(void) {
    flash_bounds();
    rectangle_clipping();
    dma_ring_counter();
    puts("board flash bounds, display clipping and DMA ring overflow: PASS");
    return 0;
}
