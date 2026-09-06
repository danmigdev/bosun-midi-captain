#include "uart_tx.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

/* PL011 model: a cold empty FIFO does NOT request TX service when its mask is
 * enabled. Only a shift after actual DR writes generates the threshold event. */
typedef struct {
    uint8_t fifo[32], output[20000];
    size_t head, count, output_length, writes;
    bool irq_enabled, irq_raw, instantaneous;
} fake_uart_t;

static bool writable(void *context) {
    fake_uart_t *uart = context;
    return uart->count < sizeof uart->fifo;
}
static void put(void *context, uint8_t byte) {
    fake_uart_t *uart = context;
    assert(writable(context)); ++uart->writes;
    if (uart->instantaneous) {
        assert(uart->output_length < sizeof uart->output);
        uart->output[uart->output_length++] = byte;
    } else {
        uart->fifo[(uart->head + uart->count) % sizeof uart->fifo] = byte;
        ++uart->count;
        if (uart->count > 4) uart->irq_raw = false;
    }
}
static void interrupt(void *context, bool enabled) {
    fake_uart_t *uart = context; uart->irq_enabled = enabled;
}
static const bosun_uart_tx_fifo_t backend = {writable, put, interrupt};

static void shift(bosun_uart_tx_t *queue, fake_uart_t *uart) {
    if (uart->count) {
        assert(uart->output_length < sizeof uart->output);
        uart->output[uart->output_length++] = uart->fifo[uart->head];
        uart->head = (uart->head + 1u) % sizeof uart->fifo;
        --uart->count;
        if (uart->count <= 4) uart->irq_raw = true;
    }
    if (uart->irq_enabled && uart->irq_raw) bosun_uart_tx_service(queue, &backend, uart);
}

static void finish(bosun_uart_tx_t *queue, fake_uart_t *uart) {
    unsigned ticks = 0;
    while (bosun_uart_tx_pending(queue) || uart->count) {
        assert(++ticks < 20000); shift(queue, uart);
    }
    assert(!uart->irq_enabled);
}

static void cold_and_idle(void) {
    bosun_uart_tx_t queue = {0}; fake_uart_t uart = {0};
    interrupt(&uart, true);
    assert(!uart.irq_raw && !uart.count); /* enabling an empty PL011 is insufficient */
    const uint8_t bytes[] = {0xf0, 0x00, 0x20, 0x33, 0x02, 0x7f, 0x7e, 0xf7};
    assert(bosun_uart_tx_write(&queue, &backend, &uart, bytes, sizeof bytes) == sizeof bytes);
    assert(uart.count == sizeof bytes); /* immediate FIFO prime, before any IRQ */
    finish(&queue, &uart);
    assert(uart.output_length == sizeof bytes && !memcmp(uart.output, bytes, sizeof bytes));
    uart.irq_raw = false; /* no residual event is required for a later idle restart */
    assert(bosun_uart_tx_write(&queue, &backend, &uart, bytes, sizeof bytes) == sizeof bytes);
    assert(uart.count == sizeof bytes);
    finish(&queue, &uart);
    assert(uart.output_length == 2 * sizeof bytes && !memcmp(uart.output + sizeof bytes, bytes, sizeof bytes));
}

static void backpressure_and_wrap(void) {
    bosun_uart_tx_t queue = {0}; fake_uart_t uart = {0};
    uint8_t bytes[12000];
    for (size_t i = 0; i < sizeof bytes; ++i) bytes[i] = (uint8_t)(i * 17u + 3u);
    /* Occupied hardware cannot be overwritten, even when the software queue fills. */
    for (unsigned i = 0; i < sizeof uart.fifo; ++i) put(&uart, 0xee);
    size_t accepted = bosun_uart_tx_write(&queue, &backend, &uart, bytes, sizeof bytes);
    assert(accepted == BOSUN_UART_TX_BYTES - 1 && bosun_uart_tx_pending(&queue) == accepted);
    assert(bosun_uart_tx_write(&queue, &backend, &uart, bytes + accepted, 1) == 0);
    assert(uart.irq_enabled);
    unsigned ticks = 0;
    while (accepted < sizeof bytes) {
        assert(++ticks < 20000);
        shift(&queue, &uart);
        size_t remaining = sizeof bytes - accepted;
        size_t amount = remaining < 97 ? remaining : 97;
        accepted += bosun_uart_tx_write(&queue, &backend, &uart, bytes + accepted, amount);
        assert(bosun_uart_tx_pending(&queue) < BOSUN_UART_TX_BYTES);
    }
    finish(&queue, &uart);
    assert(uart.output_length == sizeof uart.fifo + sizeof bytes);
    for (unsigned i = 0; i < sizeof uart.fifo; ++i) assert(uart.output[i] == 0xee);
    assert(!memcmp(uart.output + sizeof uart.fifo, bytes, sizeof bytes));
}

static void bounded_service(void) {
    bosun_uart_tx_t queue = {0}; fake_uart_t uart = {.instantaneous = true};
    uint8_t bytes[511]; memset(bytes, 0x35, sizeof bytes);
    assert(bosun_uart_tx_write(&queue, &backend, &uart, bytes, sizeof bytes) == sizeof bytes);
    assert(uart.writes == BOSUN_UART_TX_SERVICE_BYTES);
    while (bosun_uart_tx_pending(&queue)) {
        size_t before = uart.writes;
        bosun_uart_tx_service(&queue, &backend, &uart);
        assert(uart.writes - before <= BOSUN_UART_TX_SERVICE_BYTES);
    }
    assert(uart.output_length == sizeof bytes && !memcmp(uart.output, bytes, sizeof bytes));
    assert(!uart.irq_enabled);
}

int main(void) {
    cold_and_idle(); backpressure_and_wrap(); bounded_service();
    puts("UART TX: cold FIFO prime, idle restart, bounded service, full FIFO/backpressure and 12000-byte wrap passed");
    return 0;
}
