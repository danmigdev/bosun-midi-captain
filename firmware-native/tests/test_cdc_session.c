#include "bosun/board.h"
#include "cdc_session.h"
#include "usb_rx_trace.h"
#include "tusb.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

/* TinyUSB's FIFOs and the USB packet already submitted to its endpoint are
 * deliberately separate. Clearing a FIFO cannot revoke that packet. */
static uint8_t tx[2048], rx[2048], endpoint[64], wire[8192];
static size_t tx_length, rx_length, endpoint_length, wire_length;
static uint32_t rx_clears, tx_clears, write_limit;
static bool mounted, dtr, suspended;
void bosun_usb_rx_trace_session(uint32_t generation, bool connected) {
    assert(generation == bosun_board_usb_session_generation());
    (void)connected; /* The low-level wrapper has a separate dedicated test. */
}

bool tud_mounted(void) { return mounted; }
bool tud_suspended(void) { return suspended; }
uint8_t tud_cdc_n_get_line_state(uint8_t itf) { assert(itf == 1); return dtr ? 1 : 0; }
bool tud_cdc_n_connected(uint8_t itf) { assert(itf == 1); return mounted && !suspended && dtr; }
uint32_t tud_cdc_n_write_available(uint8_t itf) { assert(itf == 1); return (uint32_t)(sizeof tx - tx_length); }
uint32_t tud_cdc_n_write_flush(uint8_t itf) {
    assert(itf == 1);
    if (!mounted || suspended || endpoint_length || !tx_length) return 0;
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
    assert(!suspended && endpoint_length && wire_length + endpoint_length < sizeof wire);
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
    mounted = true; dtr = suspended = false; tud_mount_cb();
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
    bosun_board_usb_diagnostics_t stats;
    bosun_board_usb_diagnostics(&stats);
    /* Old in-flight bytes cross the physical wire but belong to the retired
     * session; the new counter includes only its delimiter and fresh JSON. */
    assert(stats.generation == generation + 2 && stats.tx_bytes == sizeof fresh);
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
static void suspend_preserves_session_and_queued_bytes(void) {
    fixture(); line_state(true); bosun_cdc_task(); drain(); wire_length = 0;
    uint8_t payload[2112], input[64];
    for (size_t i = 0; i < sizeof payload; ++i) payload[i] = (uint8_t)(i * 17u + i / 64u);
    assert(bosun_board_data_write(payload, 2048) == 2048);
    assert(bosun_board_data_write(payload + 2048, 64) == 64);
    assert(endpoint_length == 64 && tx_length == sizeof tx);
    const uint8_t request[] = "{\"type\":\"PING\",\"id\":\"kept\"}\n";
    memcpy(rx, request, sizeof request - 1); rx_length = sizeof request - 1;
    uint32_t generation = bosun_board_usb_session_generation();
    uint32_t reads_cleared = rx_clears, writes_cleared = tx_clears;
    suspended = true;
    assert(!tud_cdc_n_connected(1) && bosun_board_usb_connected());
    for (unsigned i = 0; i < 20; ++i) {
        bosun_cdc_task(); tud_cdc_rx_cb(1);
        assert(!bosun_board_data_read(input, sizeof input));
        assert(!bosun_board_data_write(payload, 1));
    }
    assert(bosun_board_usb_session_generation() == generation);
    assert(rx_clears == reads_cleared && tx_clears == writes_cleared);
    assert(rx_length == sizeof request - 1 && tx_length == sizeof tx && endpoint_length == 64);
    suspended = false; bosun_cdc_task(); drain();
    assert(bosun_board_usb_session_generation() == generation);
    assert(wire_length == sizeof payload && !memcmp(wire, payload, sizeof payload));
    assert(bosun_board_data_read(input, sizeof input) == sizeof request - 1);
    assert(!memcmp(input, request, sizeof request - 1));

    /* A new DTR session can begin while suspended, but its boundary is not
     * consumed until resume. Actual DTR edges still discard stale bytes. */
    fixture(); suspended = true; line_state(true);
    generation = bosun_board_usb_session_generation();
    bosun_cdc_task();
    assert(bosun_board_usb_connected() && !tx_length && !endpoint_length);
    assert(!bosun_board_data_write(payload, 1));
    suspended = false; bosun_cdc_task(); drain();
    assert(wire_length == 1 && wire[0] == '\n');
    assert(bosun_board_usb_session_generation() == generation);
    memcpy(rx, request, sizeof request - 1); rx_length = sizeof request - 1;
    assert(bosun_board_data_write(payload, sizeof payload) == sizeof tx);
    suspended = true; line_state(false); bosun_cdc_task();
    assert(!bosun_board_usb_connected() && !tx_length && !rx_length);
    assert(bosun_board_usb_session_generation() == generation + 1);
    line_state(true); bosun_cdc_task();
    assert(bosun_board_usb_connected() && bosun_board_usb_session_generation() == generation + 2);
    mounted = false; bosun_cdc_task();
    assert(!bosun_board_usb_connected() && bosun_board_usb_session_generation() == generation + 3);
}
static void diagnostics_account_only_effective_io(void) {
    fixture(); line_state(true);
    bosun_board_usb_diagnostics_t stats, before;
    bosun_board_usb_diagnostics(NULL); /* Optional snapshot is harmless. */
    bosun_board_usb_diagnostics(&stats);
    assert(stats.generation == bosun_board_usb_session_generation());
    assert(!stats.rx_bytes && !stats.tx_bytes);
    assert(stats.rx_fnv1a == UINT32_C(0x811c9dc5) && stats.tx_fnv1a == UINT32_C(0x811c9dc5));

    static const uint8_t payload[] = "foobar";
    memcpy(rx, payload, 6); rx_length = 6; tud_cdc_rx_cb(1);
    uint8_t data[8];
    assert(bosun_board_data_read(data, 2) == 2 && !memcmp(data, payload, 2));
    assert(!bosun_board_data_read(data, 0));
    assert(bosun_board_data_read(data + 2, sizeof data - 2) == 4 && !memcmp(data, payload, 6));
    assert(!bosun_board_data_read(data, sizeof data));
    bosun_board_usb_diagnostics(&stats);
    assert(stats.rx_bytes == 6 && stats.rx_fnv1a == UINT32_C(0xbf9cf968));

    /* A refused boundary counts nothing. Every later one-byte write counts
     * only its accepted prefix, even while USB has not completed delivery. */
    write_limit = 0;
    assert(!bosun_board_data_write(payload, 6));
    bosun_board_usb_diagnostics(&stats);
    assert(!stats.tx_bytes && stats.tx_fnv1a == UINT32_C(0x811c9dc5));
    for (size_t i = 0; i < 6; ++i) {
        write_limit = 1;
        assert(bosun_board_data_write(payload + i, 6 - i) == 1);
        bosun_board_usb_diagnostics(&before);
        assert(before.tx_bytes == i + 2); /* one boundary byte */
        write_limit = 0;
        assert(!bosun_board_data_write(payload + i, 6 - i));
        bosun_board_usb_diagnostics(&stats);
        assert(!memcmp(&before, &stats, sizeof stats));
    }
    assert(!wire_length && stats.tx_bytes == 7);
    drain();
    assert(wire_length == 7 && !memcmp(wire, "\nfoobar", 7));
    bosun_board_usb_diagnostics(&stats);
    assert(stats.tx_fnv1a == UINT32_C(0xf98f9bc0));
    before = stats;
    suspended = true; bosun_cdc_task();
    assert(!bosun_board_data_read(data, sizeof data) && !bosun_board_data_write(payload, 6));
    suspended = false; bosun_cdc_task();
    bosun_board_usb_diagnostics(&stats);
    assert(!memcmp(&before, &stats, sizeof stats));

    line_state(false);
    bosun_board_usb_diagnostics(&stats);
    assert(stats.generation == before.generation + 1 && !stats.rx_bytes && !stats.tx_bytes);
    assert(stats.rx_fnv1a == UINT32_C(0x811c9dc5) && stats.tx_fnv1a == UINT32_C(0x811c9dc5));
    line_state(true); write_limit = UINT32_MAX; bosun_cdc_task();
    bosun_board_usb_diagnostics(&stats);
    assert(stats.generation == before.generation + 2 && !stats.rx_bytes && stats.tx_bytes == 1);
    assert(stats.tx_fnv1a == UINT32_C(0x0f0c6cdd));
    static const uint8_t binary[] = {0, 255, 128};
    memcpy(rx, binary, sizeof binary); rx_length = sizeof binary;
    assert(bosun_board_data_read(data, sizeof data) == sizeof binary);
    bosun_board_usb_diagnostics(&stats);
    assert(stats.rx_bytes == 3 && stats.rx_fnv1a == UINT32_C(0x728e7760));
    tud_umount_cb();
    bosun_board_usb_diagnostics(&stats);
    assert(!stats.rx_bytes && !stats.tx_bytes && stats.generation == before.generation + 3);
}

int main(void) {
    normal_session_and_partial_writes();
    stale_fifos_and_inflight_packet();
    remount_and_disconnected_io();
    suspend_preserves_session_and_queued_bytes();
    diagnostics_account_only_effective_io();
    puts("CDC sessions: DTR generations, stale RX/TX, full FIFO, in-flight packet delimiter, partial writes, remount and lossless suspend passed");
    return 0;
}
