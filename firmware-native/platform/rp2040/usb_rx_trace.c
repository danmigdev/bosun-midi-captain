#include "bosun/board.h"
#include "usb_rx_trace.h"
#include "../usb_diagnostics.h"
#include "tusb.h"
#include "device/dcd.h"
#include "hardware/clocks.h"
#include "hardware/structs/usb.h"
#include "hardware/sync.h"
#include "pico.h"

/* GNU ld wrappers, verified in the ARM image: no TinyUSB source modifications.
 * Core 0 only. Each hash touches at most one 64-byte packet; no payload log,
 * heap, waiting, endpoint changes, or USB calls are made by the IRQ wrapper. */
enum { DATA_EP = 0x04, DATA_CDC = 1, PACKET_BYTES = 64 };
static bosun_board_usb_rx_diagnostics_t trace = {
    .dcd_fnv1a = UINT32_C(2166136261), .cdc_fnv1a = UINT32_C(2166136261),
};
static uint8_t *rx_buffer;
static uint16_t rx_capacity;
static bool tracing, armed;

bool __real_dcd_edpt_xfer(uint8_t rhport, uint8_t ep_addr, uint8_t *buffer, uint16_t length);
void __real_dcd_event_handler(const dcd_event_t *event, bool in_isr);
bool __real_cdcd_xfer_cb(uint8_t rhport, uint8_t ep_addr, xfer_result_t result, uint32_t length);

void bosun_usb_rx_trace_session(uint32_t generation, bool connected) {
    uint32_t saved = save_and_disable_interrupts();
    trace = (bosun_board_usb_rx_diagnostics_t){
        .generation = generation,
        .dcd_fnv1a = UINT32_C(2166136261), .cdc_fnv1a = UINT32_C(2166136261),
    };
    tracing = connected;
    restore_interrupts(saved);
}

bool bosun_board_usb_rx_diagnostics(bosun_board_usb_rx_diagnostics_t *result) {
    if (!result) return false;
    uint32_t saved = save_and_disable_interrupts();
    *result = trace;
    result->sie_status = usb_hw->sie_status;
    restore_interrupts(saved);
    result->sys_hz = clock_get_hz(clk_sys);
    result->usb_hz = clock_get_hz(clk_usb);
    return true;
}

bool __wrap_dcd_edpt_xfer(uint8_t rhport, uint8_t ep_addr, uint8_t *buffer, uint16_t length) {
    if (rhport == 0 && ep_addr == DATA_EP) {
        uint32_t saved = save_and_disable_interrupts();
        if (tracing) {
            ++trace.arms;
            if (armed || !buffer || length != PACKET_BYTES) ++trace.errors;
        }
        /* Publish before arming: completion can interrupt the real call. */
        rx_buffer = buffer; rx_capacity = length; armed = true;
        restore_interrupts(saved);
    }
    bool accepted = __real_dcd_edpt_xfer(rhport, ep_addr, buffer, length);
    if (!accepted && rhport == 0 && ep_addr == DATA_EP) {
        uint32_t saved = save_and_disable_interrupts();
        armed = false;
        if (tracing) ++trace.arm_failures;
        restore_interrupts(saved);
    }
    return accepted;
}

void __no_inline_not_in_flash_func(__wrap_dcd_event_handler)(const dcd_event_t *event, bool in_isr) {
    if (event->rhport == 0 && (event->event_id == DCD_EVENT_BUS_RESET ||
                              event->event_id == DCD_EVENT_UNPLUGGED)) {
        /* Unlike DTR, these events cancel the controller's pending transfer. */
        uint32_t saved = save_and_disable_interrupts();
        armed = false; rx_buffer = NULL; rx_capacity = 0;
        restore_interrupts(saved);
    }
    if (event->rhport == 0 && event->event_id == DCD_EVENT_XFER_COMPLETE &&
        event->xfer_complete.ep_addr == DATA_EP) {
        /* RP2040 calls here in the USB IRQ after copying DPRAM into rx_buffer.
         * Masking also makes the wrapper safe for an injected non-IRQ event. */
        uint32_t saved = save_and_disable_interrupts();
        uint32_t length = event->xfer_complete.len;
        if (tracing) {
            ++trace.dcd_packets;
            if (!armed || !rx_buffer || length > rx_capacity || length > PACKET_BYTES ||
                event->xfer_complete.result != XFER_RESULT_SUCCESS) ++trace.errors;
            else bosun_usb_diagnostics_add(&trace.dcd_bytes, &trace.dcd_fnv1a, rx_buffer, length);
        }
        armed = false;
        restore_interrupts(saved);
    }
    __real_dcd_event_handler(event, in_isr);
}

bool __wrap_cdcd_xfer_cb(uint8_t rhport, uint8_t ep_addr, xfer_result_t result, uint32_t length) {
    bool observe = rhport == 0 && ep_addr == DATA_EP && tracing;
    uint32_t before = 0;
    if (observe) {
        before = tud_cdc_n_available(DATA_CDC);
        uint32_t saved = save_and_disable_interrupts();
        ++trace.cdc_packets;
        if (!rx_buffer || length > rx_capacity || length > PACKET_BYTES || result != XFER_RESULT_SUCCESS)
            ++trace.errors;
        else bosun_usb_diagnostics_add(&trace.cdc_bytes, &trace.cdc_fnv1a, rx_buffer, length);
        restore_interrupts(saved);
    }
    bool accepted = __real_cdcd_xfer_cb(rhport, ep_addr, result, length);
    if (observe) {
        /* CDC's synchronous callback inserts the packet, invokes our RX
         * callback (which never reads in an open session), then rearms. An IRQ
         * may fill the next RAM packet, but cannot insert it into this FIFO. */
        uint32_t after = tud_cdc_n_available(DATA_CDC);
        uint32_t saved = save_and_disable_interrupts();
        if (!accepted || after < before || after - before > length) ++trace.errors;
        else trace.fifo_dropped_bytes += length - (after - before);
        restore_interrupts(saved);
    }
    return accepted;
}
