#include "bosun/board.h"
#include "cdc_session.h"
#include "tusb.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

/* TinyUSB's FIFOs and the USB packet already submitted to its endpoint are
 * deliberately separate. Clearing a FIFO cannot revoke that packet. */
static uint8_t tx[2048], rx[2048], endpoint[64], wire[8192];
static size_t tx_length, rx_length, endpoint_length, wire_length;
static uint32_t rx_clears, tx_clears, write_limit;
static bool mounted, dtr;

bool tud_mounted(void) { return mounted; }
bool tud_cdc_n_connected(uint8_t itf) { assert(itf == 1); return mounted && dtr; }
uint32_t tud_cdc_n_write_available(uint8_t itf) { assert(itf == 1); return (uint32_t)(sizeof tx - tx_length); }
uint32_t tud_cdc_n_write_flush(uint8_t itf) {
    assert(itf == 1);
    if (!mounted || endpoint_length || !tx_length) return 0;
    endpoint_length = tx_length < sizeof endpoint ? tx_length : sizeof endpoint;
    memcpy(endpoint, tx, endpoint_length);
    memmove(tx, tx + endpoint_length, tx_length - endpoint_length);
    tx_length -= endpoint_length;
    return (uint32_t)endpoint_length;
}
uint32_t tud_cdc_n_write(uint8_t itf, const void *data, uint32_t length) {
    assert(itf == 1);
    if (length > sizeof tx - tx_length) length = (uint32_t)(sizeof tx - tx_length);
    if (length > write_limit) length = write_limit;
    memcpy(tx + tx_length, data, length); tx_length += length;
    if (tx_length >= sizeof endpoint) (void)tud_cdc_n_write_flush(itf);
    return length;
}
bool tud_cdc_n_write_clear(uint8_t itf) { assert(itf == 1); ++tx_clears; tx_length = 0; return true; }
void tud_cdc_n_read_flush(uint8_t itf) { assert(itf == 1); ++rx_clears; rx_length = 0; }
uint32_t tud_cdc_n_read(uint8_t itf, void *data, uint32_t capacity) {
    assert(itf == 1);
    size_t count = capacity < rx_length ? capacity : rx_length;
    memcpy(data, rx, count); memmove(rx, rx + count, rx_length - count);
    rx_length -= count; return (uint32_t)count;
}
static void complete_packet(void) {
    assert(endpoint_length && wire_length + endpoint_length < sizeof wire);
    memcpy(wire + wire_length, endpoint, endpoint_length); wire_length += endpoint_length;
    wire[wire_length] = 0; endpoint_length = 0;
    /* TinyUSB's IN completion chains the FIFO regardless of DTR. */
    (void)tud_cdc_n_write_flush(1);
}
static void drain(void) {
    (void)tud_cdc_n_write_flush(1);
    while (endpoint_length) complete_packet();
}
static void line_state(bool next_dtr) {
    dtr = next_dtr; tud_cdc_line_state_cb(1, next_dtr, false);
}
static void fixture(void) {
    tx_length = rx_length = endpoint_length = wire_length = 0;
    rx_clears = tx_clears = 0; write_limit = UINT32_MAX;
    mounted = true; dtr = false; tud_mount_cb();
    assert(!bosun_board_usb_connected());
}
static void normal_session_and_partial_writes(void) {
    fixture(); line_state(true);
    uint32_t generation = bosun_board_usb_session_generation();
    const uint8_t ack[] = "{\"type\":\"ACK\",\"id\":1}\n";
    write_limit = 0;
    assert(!bosun_board_data_write(ack, sizeof ack - 1) && !tx_length && !endpoint_length);
    write_limit = 1;
    for (size_t offset = 0; offset < sizeof ack - 1;) {
        size_t accepted = bosun_board_data_write(ack + offset, sizeof ack - 1 - offset);
        assert(accepted == 1); offset += accepted;
    }
    drain();
    assert(wire_length == sizeof ack && wire[0] == '\n' && !memcmp(wire + 1, ack, sizeof ack - 1));
    uint32_t cleared = tx_clears;
    tud_cdc_line_state_cb(1, true, true); /* RTS alone is not a new session. */
    tud_cdc_line_state_cb(0, false, false); /* Console DTR does not reset data. */
    bosun_cdc_task();
    assert(bosun_board_usb_session_generation() == generation && tx_clears == cleared);
    write_limit = UINT32_MAX;
    assert(bosun_board_data_write(ack, sizeof ack - 1) == sizeof ack - 1);
    drain();
    assert(wire_length == 2 * (sizeof ack - 1) + 1 && !memcmp(wire + sizeof ack, ack, sizeof ack - 1));
}
static void stale_fifos_and_inflight_packet(void) {
    fixture(); line_state(true); bosun_cdc_task(); drain(); wire_length = 0;
    uint8_t old[2048]; memset(old, 'x', sizeof old);
    assert(bosun_board_data_write(old, sizeof old) == sizeof old);
    assert(endpoint_length == 64 && tx_length == sizeof old - 64);
    assert(bosun_board_data_write(old, 64) == 64 && tx_length == sizeof tx);
    assert(!bosun_board_data_write(old, 1));
    const char request[] = "{\"type\":\"REBOOT\"}\n";
    memcpy(rx, request, sizeof request - 1); rx_length = sizeof request - 1;
    uint32_t generation = bosun_board_usb_session_generation();
    line_state(false);
    assert(!tx_length && !rx_length && endpoint_length == 64);
    /* A late OUT completion while closed is discarded too. */
    memcpy(rx, request, sizeof request - 1); rx_length = sizeof request - 1;
    tud_cdc_rx_cb(1); assert(!rx_length);
    /* Even data queued after the falling callback is flushed on the rise. */
    memcpy(rx, request, sizeof request - 1); rx_length = sizeof request - 1;
    line_state(true); bosun_cdc_task();
    assert(bosun_board_usb_session_generation() == generation + 2 && !rx_length);
    uint8_t input[64]; assert(!bosun_board_data_read(input, sizeof input));
    const uint8_t fresh[] = "{\"type\":\"ACK\",\"id\":\"fresh\"}\n";
    assert(bosun_board_data_write(fresh, sizeof fresh - 1) == sizeof fresh - 1);
    drain();
    assert(wire_length == 64 + 1 + sizeof fresh - 1);
    assert(!memcmp(wire, old, 64) && wire[64] == '\n' && !memcmp(wire + 65, fresh, sizeof fresh - 1));
    memcpy(rx, request, sizeof request - 1); rx_length = sizeof request - 1;
    tud_cdc_rx_cb(1);
    assert(bosun_board_data_read(input, sizeof input) == sizeof request - 1);
    assert(!memcmp(input, request, sizeof request - 1));
}
static void remount_and_disconnected_io(void) {
    fixture(); line_state(true); bosun_cdc_task(); drain();
    uint32_t generation = bosun_board_usb_session_generation();
    mounted = false; bosun_cdc_task();
    assert(!bosun_board_usb_connected() && bosun_board_usb_session_generation() == generation + 1);
    uint8_t byte = 'a'; assert(!bosun_board_data_read(&byte, 1) && !bosun_board_data_write(&byte, 1));
    /* Reconfiguration must reset even without an observed disconnected tick. */
    mounted = true; tud_mount_cb(); line_state(true); bosun_cdc_task();
    assert(bosun_board_usb_connected() && bosun_board_usb_session_generation() == generation + 3);
    assert(!bosun_board_data_read(NULL, 1) && !bosun_board_data_write(NULL, 1));
    tud_umount_cb(); assert(!bosun_board_usb_connected());
}
int main(void) {
    normal_session_and_partial_writes();
    stale_fifos_and_inflight_packet();
    remount_and_disconnected_io();
    puts("CDC sessions: DTR generations, stale RX/TX, full FIFO, in-flight packet delimiter, partial writes and remount passed");
    return 0;
}
