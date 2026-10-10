/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef BOSUN_UART_TX_H
#define BOSUN_UART_TX_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

enum { BOSUN_UART_TX_BYTES = 512, BOSUN_UART_TX_SERVICE_BYTES = 32 };
_Static_assert((BOSUN_UART_TX_BYTES & (BOSUN_UART_TX_BYTES - 1)) == 0,
               "UART ring must be a power of two");
typedef struct {
    volatile uint8_t bytes[BOSUN_UART_TX_BYTES];
    volatile uint32_t head, tail;
} bosun_uart_tx_t;
typedef struct {
    bool (*writable)(void *context);
    void (*write)(void *context, uint8_t byte);
    void (*interrupt)(void *context, bool enabled);
} bosun_uart_tx_fifo_t;

static inline size_t bosun_uart_tx_pending(const bosun_uart_tx_t *queue) {
    return (queue->head - queue->tail) & (BOSUN_UART_TX_BYTES - 1u);
}

/* Call in the UART IRQ, or with interrupts disabled in the producer. At most
 * one hardware FIFO's worth of register writes; never wait for a character. */
static inline void bosun_uart_tx_service(bosun_uart_tx_t *queue,
                                        const bosun_uart_tx_fifo_t *fifo, void *context) {
    for (unsigned i = 0; i < BOSUN_UART_TX_SERVICE_BYTES && queue->tail != queue->head &&
         fifo->writable(context); ++i) {
        fifo->write(context, queue->bytes[queue->tail]);
        queue->tail = (queue->tail + 1u) & (BOSUN_UART_TX_BYTES - 1u);
    }
    fifo->interrupt(context, queue->tail != queue->head);
}

/* RP2040 datasheet 4.2.6.3: enabling TX interrupts on the cold empty FIFO
 * does not assert an interrupt. Prime DR now; the subsequent FIFO transition
 * generates the IRQ that drains the remainder. The same kick handles idle
 * restarts without depending on a pending TX interrupt left by earlier data.
 * https://datasheets.raspberrypi.com/rp2040/rp2040-datasheet.pdf#page=427 */
static inline size_t bosun_uart_tx_write(bosun_uart_tx_t *queue,
                                         const bosun_uart_tx_fifo_t *fifo, void *context,
                                         const uint8_t *data, size_t length) {
    size_t accepted = 0;
    while (accepted < length) {
        uint32_t next = (queue->head + 1u) & (BOSUN_UART_TX_BYTES - 1u);
        if (next == queue->tail) break;
        queue->bytes[queue->head] = data[accepted++];
        queue->head = next;
    }
    bosun_uart_tx_service(queue, fifo, context);
    return accepted;
}
#endif
