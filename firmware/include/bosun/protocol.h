#ifndef BOSUN_PROTOCOL_H
#define BOSUN_PROTOCOL_H
#include "bosun/runtime.h"

#define BOSUN_FIRMWARE_VERSION "0.8.6"
#define BOSUN_PROTOCOL_RX_BYTES 26624u
#define BOSUN_PROTOCOL_TX_BYTES 30720u
#define BOSUN_PROTOCOL_TOKENS 1792u
#define BOSUN_PROTOCOL_TIMEOUT_MS 5000u
#define BOSUN_PROTOCOL_EVENT_BYTES 2048u
#define BOSUN_PROTOCOL_PATCH_BITMAP_BYTES ((BOSUN_BANK_MAX * BOSUN_SLOT_MAX + 7u) / 8u)

/* One bounded, nonblocking line at a time. feed returns bytes consumed: callers
 * retain the suffix while a reply drains. consume_output accepts partial writes.
 * Session reset discards old input/output without changing configuration/MIDI.
 * REBOOT accepts mode="normal" (default) or "bootloader" (ROM BOOTSEL).
 * SET_HARDWARE persists a supported model and reboots normally if it differs.
 * PUT_LOGO {offset, data: base64 of 1..12288 bytes} stages the custom startup
 * logo in order (offset 0 restarts; an identical retry of the last chunk is
 * accepted) and replies received. COMMIT_LOGO {size: 96000, fnv1a} verifies
 * the staged bytes and atomically replaces the logo; DELETE_LOGO restores the
 * built-in one. Both then set logo_preview for the application.
 * UPDATE_LOCK {seconds: 0..120} locks the physical switches until now + seconds
 * (renewing restarts the lease; 0 releases). RAM only; ACTIVATE_SWITCH answers
 * update_lock meanwhile, every other command is unaffected.
 * GET_SCREENSAVER replies SCREENSAVER {active, effect while active} with the
 * application's screensaver; the same frame goes out unsolicited (id null)
 * whenever it changes, and once on a new session while it is active.
 * SCREENSAVER_START {effect?: bounce|breathe|drift|mix, default the configured
 * one} sets screensaver_start for the application; update_lock while locked.
 * A state-changing request (configuration, patch or profile changes, switch,
 * morph, logo, hardware, MIDI monitor) sets activity, and so does
 * SCREENSAVER_STOP (always ACK, nothing else); reads, PING, UPDATE_LOCK,
 * GET_SCREENSAVER and SCREENSAVER_START never do.
 * No protocol operation formats flash or installs firmware. */
typedef struct {
    bosun_runtime_t *runtime;
    uint32_t (*read_led)(uint8_t index); /* Optional last submitted RGB frame. */
    uint16_t (*read_switches)(void); /* Fresh GPIO guard for ACTIVATE_SWITCH. */
    char rx[BOSUN_PROTOCOL_RX_BYTES + 1], tx[BOSUN_PROTOCOL_TX_BYTES];
    bosun_json_token_t tokens[BOSUN_PROTOCOL_TOKENS];
    bosun_json_doc_t request;
    bosun_json_writer_t writer;
    size_t rx_length, tx_length, tx_offset;
    uint32_t last_rx_ms, last_context_ms, context_revision, kemper_revision;
    uint32_t requests, errors, update_lock_ms;
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
    bool discarding, connected, reboot_requested, reboot_bootloader, ui_observed;
    bool hardware_configured; /* A valid hardware record selected runtime->hardware. */
    bool logo_preview; /* The startup logo changed; the consumer clears it. */
    bool activity; /* A state-changing request ran; the consumer clears it. */
    uint8_t update_lock_seconds; /* Lease from update_lock_ms; 0 is unlocked. */
    uint8_t screensaver_start; /* Requested effect (1..4); the consumer clears it. */
    uint8_t screensaver, screensaver_sent; /* Shown effect (0 inactive), last announced. */
} bosun_protocol_t;

void bosun_protocol_init(bosun_protocol_t *protocol, bosun_runtime_t *runtime);
void bosun_protocol_session(bosun_protocol_t *protocol, bool connected);
size_t bosun_protocol_feed(bosun_protocol_t *protocol, const uint8_t *data,
                          size_t length, uint32_t now_ms);
const uint8_t *bosun_protocol_output(const bosun_protocol_t *protocol, size_t *length);
void bosun_protocol_consume_output(bosun_protocol_t *protocol, size_t length);
void bosun_protocol_tick(bosun_protocol_t *protocol, uint32_t now_ms);
/* True while an UPDATE_LOCK lease runs. An expired lease is cleared, so it
 * cannot return when the millisecond clock wraps. */
bool bosun_protocol_update_locked(bosun_protocol_t *protocol, uint32_t now_ms);
#endif
