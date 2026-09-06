#include "bosun/board.h"
#include "cdc_session.h"
#include "usb_rx_trace.h"
#include "../usb_diagnostics.h"
#include "tusb.h"

enum { DATA_CDC = 1 };
static uint32_t generation;
static bool connected, boundary_pending;
static bosun_board_usb_diagnostics_t diagnostics = {
    .rx_fnv1a = UINT32_C(2166136261), .tx_fnv1a = UINT32_C(2166136261),
};

static bool session_open(void) {
    /* tud_cdc_n_connected() also includes !tud_suspended(). Suspension
     * pauses transport progress; it does not end ownership of these FIFOs. */
    return tud_mounted() && (tud_cdc_n_get_line_state(DATA_CDC) & 1u);
}

static void reset_session(bool next_connected) {
    connected = next_connected;
    ++generation;
    bosun_usb_diagnostics_reset(&diagnostics, generation);
    bosun_usb_rx_trace_session(generation, next_connected);
    boundary_pending = next_connected;
    /* DTR does not reset TinyUSB's FIFOs. Discard both directions before
     * allowing a new session to inherit buffered requests or JSON tails. */
    tud_cdc_n_read_flush(DATA_CDC);
    (void)tud_cdc_n_write_clear(DATA_CDC);
}

void tud_cdc_line_state_cb(uint8_t itf, bool dtr, bool rts) {
    (void)rts;
    if (itf == DATA_CDC && dtr != connected) reset_session(dtr);
}

void tud_mount_cb(void) {
    /* A bus reset can be followed by reconfiguration in one tud_task batch.
     * Advance the generation even if application polling missed the gap. */
    reset_session(false);
}

void tud_umount_cb(void) { reset_session(false); }

void tud_cdc_rx_cb(uint8_t itf) {
    /* An already armed OUT transfer may finish after DTR fell. Do not retain
     * those bytes while no data session owns the interface. */
    if (itf == DATA_CDC && !connected) tud_cdc_n_read_flush(DATA_CDC);
}

bool bosun_board_usb_connected(void) {
    return connected && session_open();
}

uint32_t bosun_board_usb_session_generation(void) { return generation; }
void bosun_board_usb_diagnostics(bosun_board_usb_diagnostics_t *result) {
    if (result) *result = diagnostics;
}

static bool write_boundary(void) {
    if (!boundary_pending) return true;
    if (!tud_cdc_n_write_available(DATA_CDC)) return false;
    static const uint8_t newline = '\n';
    if (tud_cdc_n_write(DATA_CDC, &newline, 1) != 1) return false;
    bosun_usb_diagnostics_add(&diagnostics.tx_bytes, &diagnostics.tx_fnv1a, &newline, 1);
    boundary_pending = false;
    /* write_clear cannot cancel the <=64-byte IN packet already armed by
     * TinyUSB. FIFO order puts this delimiter after that packet and before
     * new JSON, so an old partial line cannot absorb the first new frame. */
    return true;
}

void bosun_cdc_task(void) {
    bool open = session_open();
    if (open != connected) reset_session(open);
    if (open && !tud_suspended()) {
        (void)write_boundary();
        (void)tud_cdc_n_write_flush(DATA_CDC);
    }
}

size_t bosun_board_data_read(uint8_t *data, size_t capacity) {
    if (!data || !capacity || !bosun_board_usb_connected() || tud_suspended()) return 0;
#if SIZE_MAX > UINT32_MAX
    if (capacity > UINT32_MAX) capacity = UINT32_MAX;
#endif
    size_t count = tud_cdc_n_read(DATA_CDC, data, (uint32_t)capacity);
    bosun_usb_diagnostics_add(&diagnostics.rx_bytes, &diagnostics.rx_fnv1a, data, count);
    return count;
}

size_t bosun_board_data_write(const uint8_t *data, size_t length) {
    if (!data || !length || !bosun_board_usb_connected() || tud_suspended() || !write_boundary()) return 0;
    uint32_t available = tud_cdc_n_write_available(DATA_CDC);
    if (length > available) length = available;
    size_t accepted = tud_cdc_n_write(DATA_CDC, data, (uint32_t)length);
    bosun_usb_diagnostics_add(&diagnostics.tx_bytes, &diagnostics.tx_fnv1a, data, accepted);
    (void)tud_cdc_n_write_flush(DATA_CDC);
    return accepted;
}
