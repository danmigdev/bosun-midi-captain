#ifndef BOSUN_RUNTIME_H
#define BOSUN_RUNTIME_H

#include "bosun/config.h"
#include "bosun/kemper.h"
#include "bosun/switch_fsm.h"

#define BOSUN_RUNTIME_SWITCHES 10u
#define BOSUN_RUNTIME_COMMANDS 128u
#define BOSUN_RUNTIME_NAV_PATCHES 128u
#define BOSUN_RUNTIME_COMMANDS_PER_TICK 32u

typedef enum {
    BOSUN_COMMAND_CC, BOSUN_COMMAND_PC, BOSUN_COMMAND_NOTE_ON, BOSUN_COMMAND_NOTE_OFF,
    BOSUN_COMMAND_DELAY, BOSUN_COMMAND_KEMPER, BOSUN_COMMAND_KEMPER_RIG,
    BOSUN_COMMAND_PATCH, BOSUN_COMMAND_BANK_STEP, BOSUN_COMMAND_PREVIEW_STEP,
    BOSUN_COMMAND_PREVIEW_COMMIT, BOSUN_COMMAND_PREVIEW_CANCEL, BOSUN_COMMAND_SETLIST_STEP,
    BOSUN_COMMAND_BANK_PC, BOSUN_COMMAND_KEMPER_QUERY
} bosun_command_type_t;

/* A command owns all its operands: no pointers/tokens into a patch that can be
 * replaced during macro execution. A whole action is admitted or rejected. */
typedef struct {
    int32_t value;
    uint16_t first, second;
    uint8_t type, channel, index, flags;
} bosun_runtime_command_t;
typedef struct {
    int16_t patch_token, global_long_token;
    uint16_t preset_slot;
    uint8_t mode, blocks, mirror_block;
} bosun_runtime_binding_t;
typedef struct {
    bosun_runtime_command_t message;
    uint32_t fingerprint;
    int32_t smooth;
    uint16_t raw, minimum, maximum;
    int16_t value, baseline;
    uint8_t curve;
    bool enabled, invert, armed, sampled, present;
} bosun_runtime_expression_t;
typedef bosun_patch_key_t bosun_runtime_coordinate_t;
typedef struct {
    uint32_t sequence;
    uint8_t port, channel, status, length, data[2];
    bool fresh;
} bosun_runtime_learn_t;

/* RX callback is active during monitor or learn. Payload follows MidiParser
 * semantics on RX; on TX (monitor only) status/channel
 * are zero and data is the complete wire packet. Callback consumes it now. */
typedef void (*bosun_runtime_monitor_fn)(void *context, bool outbound,
    uint8_t port, uint8_t channel, uint8_t status, const uint8_t *data, size_t length);
/* Fires only after a switch action has been admitted to the command queue.
 * Indices follow the physical switch order and the six FSM trigger bits. */
typedef void (*bosun_runtime_binding_fn)(void *context, uint8_t switch_index, uint8_t action_index);

typedef struct {
    bosun_config_t *config;
    bosun_midi_send_fn send;
    void *send_context;
    bosun_runtime_monitor_fn monitor;
    void *monitor_context;
    bosun_runtime_binding_fn binding_fired;
    void *binding_context;
    bosun_midi_parser midi[2];
    bosun_kemper kemper;
    bosun_switch_fsm switches[BOSUN_RUNTIME_SWITCHES];
    bosun_runtime_binding_t bindings[BOSUN_RUNTIME_SWITCHES];
    bosun_runtime_expression_t expression[2];
    bosun_runtime_command_t commands[BOSUN_RUNTIME_COMMANDS];
    bosun_runtime_coordinate_t navigation[BOSUN_RUNTIME_NAV_PATCHES];
    bosun_runtime_learn_t learn;
    uint32_t now_ms, wait_until_ms, expression_last_ms, preview_until_ms, revision;
    uint32_t midi_rx_count, midi_tx_count, midi_tx_failed;
    uint32_t queue_overflows, unsupported_messages, invalid_messages, storage_errors;
    uint32_t config_revision, patch_revision;
    uint16_t queue_head, queue_count, navigation_count, held_mask;
    uint16_t preview_bank, preview_slot;
    char preview_name[129], profile[BOSUN_PROFILE_ID_BYTES];
    uint8_t receiving_port;
    bool waiting, preview_active, kemper_enabled, midi_monitor, midi_learn;
    bool initialized, expression_polled;
    bosun_store_result_t last_error;
} bosun_runtime_t;

/* Runtime storage is caller owned and <= 8 KiB (config owns JSON separately).
 * init/config_changed emit no MIDI. tick drains bounded work and never sleeps.
 * pressed_mask follows physical order 1,2,3,4,up,A,B,C,D,down; ADCs are 0..65535. */
void bosun_runtime_init(bosun_runtime_t *runtime, bosun_config_t *config,
    bosun_midi_send_fn send, void *context);
void bosun_runtime_tick(bosun_runtime_t *runtime, uint32_t now_ms,
    uint16_t pressed_mask, uint16_t expression1, uint16_t expression2);
void bosun_runtime_feed_midi(bosun_runtime_t *runtime, uint8_t port,
    const uint8_t *bytes, size_t length, uint32_t now_ms);
void bosun_runtime_reset_midi_input(bosun_runtime_t *runtime, uint8_t port);
bosun_store_result_t bosun_runtime_switch_patch(bosun_runtime_t *runtime,
    unsigned bank, unsigned slot, bool fire_actions);
void bosun_runtime_config_changed(bosun_runtime_t *runtime);
bool bosun_runtime_context(const bosun_runtime_t *runtime, bosun_json_writer_t *writer);
/* The same momentary-hold label is published in CONTEXT and on the TFT. */
void bosun_runtime_hold_effect(const bosun_runtime_t *runtime, char *out, size_t capacity);
/* Queue one validated message or an action {messages:[...]}; unsupported,
 * malformed or over-limit requests return false before anything is queued. */
bool bosun_runtime_dispatch(bosun_runtime_t *runtime, const bosun_json_doc_t *doc,
    int message_token);
bool bosun_runtime_action(bosun_runtime_t *runtime, const bosun_json_doc_t *doc,
    int action_token);
bool bosun_runtime_supported(const char *message_type);
/* Board presence detection is external; absent jacks remain silent. */
void bosun_runtime_expression_present(bosun_runtime_t *runtime, unsigned jack, bool present);

#endif
