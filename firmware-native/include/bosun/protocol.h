#ifndef BOSUN_PROTOCOL_H
#define BOSUN_PROTOCOL_H
#include "bosun/runtime.h"

#define BOSUN_NATIVE_VERSION "0.1.0-native-experimental"
#define BOSUN_PROTOCOL_RX_BYTES 26624u
#define BOSUN_PROTOCOL_TX_BYTES 30720u
#define BOSUN_PROTOCOL_TOKENS 1792u
#define BOSUN_PROTOCOL_TIMEOUT_MS 5000u
#define BOSUN_PROTOCOL_EVENT_BYTES 2048u
#define BOSUN_PROTOCOL_PATCH_BITMAP_BYTES 124u /* All 99 banks x 10 slots. */

/* One bounded, nonblocking line at a time. feed returns bytes consumed: callers
 * retain the suffix while a reply drains. consume_output accepts partial writes.
 * Session reset discards old input/output without changing configuration/MIDI.
 * No protocol operation formats flash or installs firmware. */
typedef struct {
    bosun_runtime_t *runtime;
    char rx[BOSUN_PROTOCOL_RX_BYTES + 1], tx[BOSUN_PROTOCOL_TX_BYTES];
    bosun_json_token_t tokens[BOSUN_PROTOCOL_TOKENS];
    bosun_json_doc_t request;
    bosun_json_writer_t writer;
    size_t rx_length, tx_length, tx_offset;
    uint32_t last_rx_ms, last_context_ms, context_revision, kemper_revision;
    uint32_t requests, errors, oversized, timeouts;
    uint32_t midi_events_dropped;
    uint8_t midi_events[BOSUN_PROTOCOL_EVENT_BYTES];
    uint16_t event_head, event_length;
    /* UI state coalesces while TX is blocked; it never competes with the
     * lossy MIDI monitor queue. Coordinate bitmaps cover the whole catalog. */
    uint8_t dirty_snapshot[BOSUN_PROTOCOL_PATCH_BITMAP_BYTES];
    uint8_t saved_pending[BOSUN_PROTOCOL_PATCH_BITMAP_BYTES];
    uint8_t discarded_pending[BOSUN_PROTOCOL_PATCH_BITMAP_BYTES];
    uint8_t binding_pending[BOSUN_RUNTIME_SWITCHES], ui_pending, patch_source;
    uint32_t observed_revision, observed_patch_revision, observed_device_hash, observed_external_rigs;
    uint16_t observed_bank, observed_slot;
    char observed_profile[BOSUN_PROFILE_ID_BYTES];
    char id[257], type[49];
    bool discarding, connected, reboot_requested, ui_observed;
} bosun_protocol_t;

void bosun_protocol_init(bosun_protocol_t *protocol, bosun_runtime_t *runtime);
void bosun_protocol_session(bosun_protocol_t *protocol, bool connected);
size_t bosun_protocol_feed(bosun_protocol_t *protocol, const uint8_t *data,
                          size_t length, uint32_t now_ms);
const uint8_t *bosun_protocol_output(const bosun_protocol_t *protocol, size_t *length);
void bosun_protocol_consume_output(bosun_protocol_t *protocol, size_t length);
void bosun_protocol_tick(bosun_protocol_t *protocol, uint32_t now_ms);
#endif
