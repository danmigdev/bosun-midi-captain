#include "bosun/switch_fsm.h"

#include <string.h>

void bosun_switch_init(bosun_switch_fsm *s, const bosun_switch_config *config) {
    if (!s) return;
    memset(s, 0, sizeof(*s));
    s->config = config ? *config : (bosun_switch_config){600, 250, 500, true};
    s->last_raw = s->stable = true;
}

void bosun_switch_reset(bosun_switch_fsm *s) {
    if (!s) return;
    s->press_start_ms = 0;
    s->fired_long_press = !s->stable;
    s->latched_on = s->latched_pre_press = s->tap_pending = false;
    s->tap_pending_until_ms = 0;
}

static uint8_t toggle(const bosun_switch_fsm *s) {
    return s->latched_on ? BOSUN_TRIGGER_TOGGLE_ON : BOSUN_TRIGGER_TOGGLE_OFF;
}

static uint8_t press(bosun_switch_fsm *s, uint32_t now, bosun_switch_mode mode) {
    switch (mode) {
    case BOSUN_SWITCH_TAP: case BOSUN_SWITCH_MOMENTARY:
        return BOSUN_TRIGGER_PRESS;
    case BOSUN_SWITCH_LATCHED:
        s->latched_pre_press = s->latched_on;
        s->press_start_ms = now;
        s->latched_on = !s->latched_on;
        return toggle(s);
    case BOSUN_SWITCH_LONG_PRESS_ALT:
        s->press_start_ms = now;
        s->fired_long_press = false;
        break;
    case BOSUN_SWITCH_DOUBLE_TAP:
        if (s->tap_pending && (int32_t)(now - s->tap_pending_until_ms) <= 0) {
            s->tap_pending = false;
            return BOSUN_TRIGGER_DOUBLE_TAP;
        }
        s->tap_pending = true;
        s->tap_pending_until_ms = now + s->config.double_tap_window_ms;
        break;
    }
    return 0;
}

static uint8_t release(bosun_switch_fsm *s, uint32_t now, bosun_switch_mode mode) {
    if (mode == BOSUN_SWITCH_MOMENTARY) return BOSUN_TRIGGER_RELEASE;
    uint32_t held = now - s->press_start_ms;
    if (mode == BOSUN_SWITCH_LONG_PRESS_ALT && !s->fired_long_press &&
        held < s->config.long_press_ms) return BOSUN_TRIGGER_PRESS;
    if (mode == BOSUN_SWITCH_LATCHED && s->config.auto_momentary_on_hold &&
        held >= s->config.auto_momentary_ms && s->latched_on != s->latched_pre_press) {
        s->latched_on = s->latched_pre_press;
        return toggle(s);
    }
    return 0;
}

bosun_switch_result bosun_switch_poll(bosun_switch_fsm *s, uint32_t now,
                                      bool raw, bosun_switch_mode mode) {
    bosun_switch_result result = {BOSUN_SWITCH_NO_EDGE, 0};
    if (!s) return result;
    if (raw != s->last_raw) {
        s->last_raw = raw;
        s->last_raw_change_ms = now;
    }
    if (raw != s->stable && now - s->last_raw_change_ms >= 5) {
        s->stable = raw;
        result.edge = raw ? BOSUN_SWITCH_RELEASE_EDGE : BOSUN_SWITCH_PRESS_EDGE;
        result.triggers = raw ? release(s, now, mode) : press(s, now, mode);
    }
    if (mode == BOSUN_SWITCH_LONG_PRESS_ALT && !s->stable && !s->fired_long_press &&
        now - s->press_start_ms >= s->config.long_press_ms) {
        s->fired_long_press = true;
        result.triggers |= BOSUN_TRIGGER_LONG_PRESS;
    }
    if (mode == BOSUN_SWITCH_DOUBLE_TAP && s->tap_pending &&
        (int32_t)(now - s->tap_pending_until_ms) > 0) {
        s->tap_pending = false;
        result.triggers |= BOSUN_TRIGGER_PRESS;
    }
    return result;
}

bool bosun_switch_momentary_active(const bosun_switch_fsm *s, uint32_t now,
                                   bosun_switch_mode mode) {
    return s && mode == BOSUN_SWITCH_LATCHED && s->config.auto_momentary_on_hold &&
        !s->stable && now - s->press_start_ms >= s->config.auto_momentary_ms;
}
