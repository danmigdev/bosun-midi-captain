#ifndef BOSUN_KEMPER_H
#define BOSUN_KEMPER_H

#include "bosun/midi.h"

#define BOSUN_KEMPER_BLOCKS 8u
#define BOSUN_KEMPER_NAME_CAPACITY 65u
#define BOSUN_KEMPER_PC_ORPHANS 8u

typedef enum {
    BOSUN_KEMPER_A, BOSUN_KEMPER_B, BOSUN_KEMPER_C, BOSUN_KEMPER_D,
    BOSUN_KEMPER_X, BOSUN_KEMPER_MOD, BOSUN_KEMPER_DELAY, BOSUN_KEMPER_REVERB
} bosun_kemper_block;
typedef enum {
    BOSUN_EXPRESSION_UNKNOWN, BOSUN_EXPRESSION_VOL, BOSUN_EXPRESSION_WAH
} bosun_expression_mode;
typedef enum {
    BOSUN_KEMPER_EFFECT, BOSUN_KEMPER_FIXED, BOSUN_KEMPER_TUNER,
    BOSUN_KEMPER_TAP, BOSUN_KEMPER_TEMPO, BOSUN_KEMPER_MORPH,
    BOSUN_KEMPER_MORPH_TRIGGER, BOSUN_KEMPER_WAH, BOSUN_KEMPER_VOLUME,
    BOSUN_KEMPER_LOOPER, BOSUN_KEMPER_ROTARY, BOSUN_KEMPER_STEP
} bosun_kemper_command_type;

typedef struct {
    char rig_name[BOSUN_KEMPER_NAME_CAPACITY];
    char tuner_note[3];
    uint32_t revision, external_rig_changes;
    uint16_t bpm, tuner_deviance;
    uint8_t rig, bank, rig_in_bank, effect_known;
    bool effects[BOSUN_KEMPER_BLOCKS];
    bool connected, tuner_active, rig_name_fresh;
    bosun_expression_mode expression_mode;
} bosun_kemper_state;

typedef struct {
    uint32_t generation, expires_ms;
    uint8_t rig;
    bool valid;
} bosun_kemper_pc_token;

/* Fixed storage: one instance per target device; no module-global state.
 * Treat fields after state as private. Read state/revision to publish changes.
 * TX callback must copy/consume synchronously and must not reenter this object. */
typedef struct {
    bosun_kemper_state state;
    bosun_midi_send_fn send;
    void *send_context;
    uint32_t generation, tx_failures;
    uint32_t last_beacon_ms, last_sensed_ms, settle_until_ms;
    uint32_t reconcile_deadline_ms, query_retire_ms, orphan_until_ms;
    uint32_t pending_name_ms, pending_name_generation;
    uint32_t block_generation[BOSUN_KEMPER_BLOCKS];
    uint32_t guard_until_ms[BOSUN_KEMPER_BLOCKS];
    uint32_t wah_query_generation, wah_retire_ms, wah_next_ms;
    uint32_t wah_slots_retire_ms, scheduled_pc_ms;
    char last_name[BOSUN_KEMPER_NAME_CAPACITY];
    char pending_name[BOSUN_KEMPER_NAME_CAPACITY];
    bosun_kemper_pc_token local_pc, orphan_pc[BOSUN_KEMPER_PC_ORPHANS];
    uint8_t channel, bound_blocks, cache_known, cache_on;
    uint8_t reconcile_pending, reconcile_attempt, reconcile_queried;
    uint8_t orphan_blocks, orphan_pc_count, last_name_rig;
    uint8_t guard_budget[BOSUN_KEMPER_BLOCKS], guard_on;
    uint8_t wah_attempts, wah_types, wah_slots, wah_states, wah_on;
    uint8_t wah_target, wah_cursor, wah_queried_slots, scheduled_pc_rig;
    uint8_t scheduled_pc_channel;
    int8_t wah_fixed;
    bool init_sent, settle_active, wah_pending, wah_query_valid, scheduled_pc;
    bool wah_retire_active;
} bosun_kemper;

void bosun_kemper_init(bosun_kemper *kemper, uint8_t channel,
    uint8_t bound_blocks, bosun_midi_send_fn send, void *context);
void bosun_kemper_set_bound_blocks(bosun_kemper *kemper, uint8_t mask);
void bosun_kemper_tick(bosun_kemper *kemper, uint32_t now_ms);
void bosun_kemper_handle(bosun_kemper *kemper, uint8_t channel, uint8_t status,
    const uint8_t *data, size_t length, uint32_t now_ms);
/* Local selection emits CC0/CC32 now; PC follows from tick after at least 5 ms.
 * One-based bank 1..25, slot 1..5. No sleep, no hardware dependency. */
bool bosun_kemper_select_rig(bosun_kemper *kemper, uint8_t bank, uint8_t slot,
    uint32_t now_ms);
/* Per-message channel overrides do not change the configured inbound channel.
 * The deferred PC retains this override through later configuration changes. */
bool bosun_kemper_select_rig_channel(bosun_kemper *kemper, uint8_t channel,
    uint8_t bank, uint8_t slot, uint32_t now_ms);
/* Establish a local patch generation without MIDI, e.g. a non-rig patch edit. */
bool bosun_kemper_begin_rig(bosun_kemper *kemper, uint8_t flat_rig,
    uint32_t now_ms);
bool bosun_kemper_request_rig_name(bosun_kemper *kemper);
bool bosun_kemper_query_blocks(bosun_kemper *kemper, uint8_t mask);
/* EFFECT index=block enum; FIXED index=Compressor/Gate/Booster/Wah/Transpose
 * (0..4); LOOP index=rec-play/stop-erase/trigger/reverse/half-speed (0..4).
 * Boolean commands use value!=0; STEP value>=0 next, value<0 previous. */
bool bosun_kemper_command(bosun_kemper *kemper,
    bosun_kemper_command_type command, uint8_t index, int value);
bool bosun_kemper_command_channel(bosun_kemper *kemper, uint8_t channel,
    bosun_kemper_command_type command, uint8_t index, int value);
bool bosun_kemper_transition_active(const bosun_kemper *kemper);
const char *bosun_kemper_expression_label(bosun_expression_mode mode);

#endif
