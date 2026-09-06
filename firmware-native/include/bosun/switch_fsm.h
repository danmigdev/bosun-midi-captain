#ifndef BOSUN_SWITCH_FSM_H
#define BOSUN_SWITCH_FSM_H

#include <stdbool.h>
#include <stdint.h>

typedef enum {
    BOSUN_SWITCH_TAP, BOSUN_SWITCH_LATCHED, BOSUN_SWITCH_MOMENTARY,
    BOSUN_SWITCH_LONG_PRESS_ALT, BOSUN_SWITCH_DOUBLE_TAP
} bosun_switch_mode;
typedef enum {
    BOSUN_SWITCH_NO_EDGE, BOSUN_SWITCH_PRESS_EDGE, BOSUN_SWITCH_RELEASE_EDGE
} bosun_switch_edge;
typedef enum {
    BOSUN_TRIGGER_PRESS = 1u << 0, BOSUN_TRIGGER_RELEASE = 1u << 1,
    BOSUN_TRIGGER_TOGGLE_ON = 1u << 2, BOSUN_TRIGGER_TOGGLE_OFF = 1u << 3,
    BOSUN_TRIGGER_LONG_PRESS = 1u << 4, BOSUN_TRIGGER_DOUBLE_TAP = 1u << 5
} bosun_switch_trigger;
typedef struct {
    uint32_t long_press_ms, double_tap_window_ms, auto_momentary_ms;
    bool auto_momentary_on_hold;
} bosun_switch_config;
typedef struct {
    bosun_switch_edge edge;
    uint8_t triggers;
} bosun_switch_result;
typedef struct {
    bosun_switch_config config;
    uint32_t last_raw_change_ms, press_start_ms, tap_pending_until_ms;
    bool last_raw, stable, fired_long_press, latched_on, latched_pre_press;
    bool tap_pending;
} bosun_switch_fsm;

void bosun_switch_init(bosun_switch_fsm *fsm, const bosun_switch_config *config);
/* Reset preserves a held switch's consumed long press across patch reloads. */
void bosun_switch_reset(bosun_switch_fsm *fsm);
/* raw_high is the pull-up level: false means pressed. now_ms may wrap. */
bosun_switch_result bosun_switch_poll(bosun_switch_fsm *fsm, uint32_t now_ms,
    bool raw_high, bosun_switch_mode mode);
bool bosun_switch_momentary_active(const bosun_switch_fsm *fsm,
    uint32_t now_ms, bosun_switch_mode mode);

#endif
