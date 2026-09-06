#include "bosun/board.h"
#include "usb_rx_trace.h"
#include "tusb.h"
#include "device/dcd.h"
#include "hardware/clocks.h"
#include "hardware/structs/usb.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

bool __wrap_dcd_edpt_xfer(uint8_t, uint8_t, uint8_t *, uint16_t);
void __wrap_dcd_event_handler(const dcd_event_t *, bool);
bool __wrap_cdcd_xfer_cb(uint8_t, uint8_t, xfer_result_t, uint32_t);

static uint8_t packet[64];
static uint32_t irq_disabled, event_calls, class_calls, arm_calls, fifo_count, fifo_limit;
static uint32_t completion_in_arm;
static bool arm_ok, class_ok, rearm_in_class;
static usb_hw_t fake_hw;
usb_hw_t *usb_hw = &fake_hw;

uint32_t save_and_disable_interrupts(void) {
    uint32_t saved = irq_disabled; irq_disabled = 1; return saved;
}
void restore_interrupts(uint32_t saved) { assert(irq_disabled); irq_disabled = saved; }
uint32_t clock_get_hz(enum clock_index clock) {
    assert(!irq_disabled); return clock == clk_sys ? 125000000 : 48000000;
}
uint32_t tud_cdc_n_available(uint8_t itf) { assert(itf == 1); return fifo_count; }
void __real_dcd_event_handler(const dcd_event_t *event, bool in_isr) {
    (void)event; (void)in_isr; assert(!irq_disabled); ++event_calls;
}
static void complete(uint8_t ep, uint32_t length) {
    dcd_event_t event = {.rhport = 0, .event_id = DCD_EVENT_XFER_COMPLETE};
    event.xfer_complete.ep_addr = ep;
    event.xfer_complete.len = length;
    event.xfer_complete.result = XFER_RESULT_SUCCESS;
    __wrap_dcd_event_handler(&event, true);
}
bool __real_dcd_edpt_xfer(uint8_t rhport, uint8_t ep, uint8_t *buffer, uint16_t length) {
    (void)rhport; (void)buffer; (void)length;
    assert(!irq_disabled); ++arm_calls;
    if (completion_in_arm) {
        uint32_t completed = completion_in_arm; completion_in_arm = 0;
        complete(ep, completed);
    }
    return arm_ok;
}
bool __real_cdcd_xfer_cb(uint8_t rhport, uint8_t ep, xfer_result_t result, uint32_t length) {
    (void)rhport; (void)result;
    assert(!irq_disabled); ++class_calls;
    if (ep == 4) {
        uint32_t accepted = length < fifo_limit ? length : fifo_limit;
        fifo_count += accepted;
        if (rearm_in_class) {
            rearm_in_class = false;
            memcpy(packet, "bar", 3); completion_in_arm = 3;
            assert(__wrap_dcd_edpt_xfer(0, 4, packet, 64));
        }
    }
    return class_ok;
}
static bosun_board_usb_rx_diagnostics_t stats(void) {
    bosun_board_usb_rx_diagnostics_t result;
    assert(bosun_board_usb_rx_diagnostics(&result));
    assert(!irq_disabled && result.sys_hz == 125000000 && result.usb_hz == 48000000);
    assert(result.sie_status == UINT32_C(0x07000000));
    return result;
}
static void start(uint32_t generation) {
    irq_disabled = event_calls = class_calls = arm_calls = fifo_count = completion_in_arm = 0;
    arm_ok = class_ok = true; rearm_in_class = false; fifo_limit = 64;
    fake_hw.sie_status = UINT32_C(0x07000000);
    bosun_usb_rx_trace_session(generation, true);
}
static void transfer(const uint8_t *bytes, size_t length, bool dispatch) {
    memcpy(packet, bytes, length);
    assert(__wrap_dcd_edpt_xfer(0, 4, packet, 64));
    complete(4, (uint32_t)length);
    if (dispatch) assert(__wrap_cdcd_xfer_cb(0, 4, XFER_RESULT_SUCCESS, (uint32_t)length));
}
static void test_layers_and_fifo_drop(void) {
    start(12);
    transfer((const uint8_t *)"foo", 3, true);
    bosun_board_usb_rx_diagnostics_t s = stats();
    assert(s.generation == 12 && s.arms == 1 && s.dcd_packets == 1 && s.cdc_packets == 1);
    assert(s.dcd_bytes == 3 && s.cdc_bytes == 3 && s.dcd_fnv1a == UINT32_C(0xa9f37ed7));
    assert(s.cdc_fnv1a == s.dcd_fnv1a && !s.errors && !s.fifo_dropped_bytes);
    fifo_limit = 1; transfer((const uint8_t *)"bar", 3, true);
    s = stats();
    assert(s.dcd_fnv1a == UINT32_C(0xbf9cf968) && s.cdc_fnv1a == s.dcd_fnv1a);
    assert(s.fifo_dropped_bytes == 2 && !s.errors);
    /* Simulate an event lost after DCD: fingerprints diverge at the next
     * stage, independently of FIFO rejection. All callbacks still forward. */
    transfer((const uint8_t *)"!", 1, false);
    s = stats();
    assert(s.dcd_packets == 3 && s.cdc_packets == 2 && s.dcd_bytes == 7 && s.cdc_bytes == 6);
    assert(s.dcd_fnv1a != s.cdc_fnv1a && event_calls == 3 && class_calls == 2);
    transfer((const uint8_t *)"", 0, true);
    s = stats();
    assert(s.dcd_packets == 4 && s.cdc_packets == 3 && s.dcd_bytes == 7 && s.cdc_bytes == 6);
    assert(!s.errors && arm_calls == 4);
}
static void test_edges_and_preemption(void) {
    start(UINT32_MAX);
    /* Preserve an armed buffer across DTR: no invalid-pointer false alarm. */
    memcpy(packet, "foobar", 6);
    assert(__wrap_dcd_edpt_xfer(0, 4, packet, 64));
    bosun_usb_rx_trace_session(0, true);
    complete(4, 6); assert(__wrap_cdcd_xfer_cb(0, 4, XFER_RESULT_SUCCESS, 6));
    bosun_board_usb_rx_diagnostics_t s = stats();
    assert(s.generation == 0 && !s.arms && s.dcd_fnv1a == UINT32_C(0xbf9cf968) && !s.errors);
    bosun_usb_rx_trace_session(1, false);
    transfer((const uint8_t *)"closed", 6, true);
    s = stats();
    assert(!s.dcd_packets && !s.cdc_packets && s.dcd_fnv1a == UINT32_C(2166136261));
    bosun_usb_rx_trace_session(2, true);
    memcpy(packet, "foobar", 6); completion_in_arm = 6;
    assert(__wrap_dcd_edpt_xfer(0, 4, packet, 64));
    assert(__wrap_cdcd_xfer_cb(0, 4, XFER_RESULT_SUCCESS, 6));
    s = stats();
    assert(s.dcd_bytes == 6 && s.dcd_fnv1a == UINT32_C(0xbf9cf968) && !s.errors);
    /* Console/MIDI completions and control events are forwarded untouched. */
    complete(0x84, 64); complete(5, 64);
    assert(__wrap_cdcd_xfer_cb(0, 2, XFER_RESULT_SUCCESS, 64));
    dcd_event_t event = {.rhport = 0, .event_id = DCD_EVENT_BUS_RESET};
    __wrap_dcd_event_handler(&event, true);
    bosun_board_usb_rx_diagnostics_t after = stats();
    assert(!memcmp(&s, &after, sizeof s));
    /* Bus reset/unplug really cancel an arm, whereas DTR above does not. */
    const uint8_t events[] = {DCD_EVENT_BUS_RESET, DCD_EVENT_UNPLUGGED};
    for (unsigned i = 0; i < sizeof events; ++i) {
        assert(__wrap_dcd_edpt_xfer(0, 4, packet, 64));
        event.event_id = events[i]; __wrap_dcd_event_handler(&event, true);
        transfer((const uint8_t *)"x", 1, true);
        after = stats(); assert(!after.errors);
    }
}
static void test_invalid_lengths_and_failure(void) {
    start(20);
    assert(__wrap_dcd_edpt_xfer(0, 4, packet, 64));
    complete(4, 65); /* Must not hash outside the real packet buffer. */
    assert(__wrap_cdcd_xfer_cb(0, 4, XFER_RESULT_SUCCESS, 65));
    bosun_board_usb_rx_diagnostics_t s = stats();
    assert(s.errors == 2 && !s.dcd_bytes && !s.cdc_bytes && s.dcd_packets == 1);
    complete(4, 1); s = stats(); assert(s.errors == 3); /* unarmed completion */
    arm_ok = false;
    assert(!__wrap_dcd_edpt_xfer(0, 4, packet, 64));
    s = stats(); assert(s.arm_failures == 1 && s.arms == 2);
    assert(!bosun_board_usb_rx_diagnostics(NULL));
}
static void test_next_completion_inside_class_rearm(void) {
    start(30); rearm_in_class = true;
    transfer((const uint8_t *)"foo", 3, true);
    /* The next IRQ overwrites the shared packet while the previous CDC
     * callback is returning. It has not yet delivered bytes into the FIFO. */
    bosun_board_usb_rx_diagnostics_t s = stats();
    assert(s.dcd_packets == 2 && s.cdc_packets == 1 && s.dcd_bytes == 6 && s.cdc_bytes == 3);
    assert(s.dcd_fnv1a == UINT32_C(0xbf9cf968) && s.cdc_fnv1a == UINT32_C(0xa9f37ed7));
    assert(!s.errors && !s.fifo_dropped_bytes);
    assert(__wrap_cdcd_xfer_cb(0, 4, XFER_RESULT_SUCCESS, 3));
    s = stats(); assert(s.cdc_fnv1a == s.dcd_fnv1a && s.cdc_bytes == 6 && !s.errors);
}
int main(void) {
    test_layers_and_fifo_drop(); test_edges_and_preemption(); test_invalid_lengths_and_failure();
    test_next_completion_inside_class_rearm();
    puts("USB RX trace: DCD/CDC/FIFO isolation, zero packets, known FNV, session reset, IRQ-safe arm/snapshot, bounded invalid input and transparent forwarding passed");
    return 0;
}
