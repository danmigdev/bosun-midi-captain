#include "bosun/switch_fsm.h"

#include <assert.h>
#include <stdio.h>

static bosun_switch_result edge(bosun_switch_fsm *s, uint32_t now,
                                bool high, bosun_switch_mode mode) {
    assert(bosun_switch_poll(s, now, high, mode).edge == BOSUN_SWITCH_NO_EDGE);
    return bosun_switch_poll(s, now + 5, high, mode);
}

static void tap_and_debounce(void) {
    bosun_switch_fsm s; bosun_switch_init(&s, NULL);
    assert(!bosun_switch_poll(&s, 100, false, BOSUN_SWITCH_TAP).triggers);
    assert(!bosun_switch_poll(&s, 102, true, BOSUN_SWITCH_TAP).triggers);
    assert(!bosun_switch_poll(&s, 104, false, BOSUN_SWITCH_TAP).triggers);
    assert(!bosun_switch_poll(&s, 108, false, BOSUN_SWITCH_TAP).triggers);
    bosun_switch_result r = bosun_switch_poll(&s, 109, false, BOSUN_SWITCH_TAP);
    assert(r.edge == BOSUN_SWITCH_PRESS_EDGE && r.triggers == BOSUN_TRIGGER_PRESS);
    assert(!bosun_switch_poll(&s, 1000, false, BOSUN_SWITCH_TAP).triggers);
    r = edge(&s, 1010, true, BOSUN_SWITCH_TAP);
    assert(r.edge == BOSUN_SWITCH_RELEASE_EDGE && !r.triggers);
}

static void long_press_and_reset(void) {
    bosun_switch_fsm s; bosun_switch_init(&s, NULL);
    assert(!edge(&s, 100, false, BOSUN_SWITCH_LONG_PRESS_ALT).triggers);
    assert(!bosun_switch_poll(&s, 704, false, BOSUN_SWITCH_LONG_PRESS_ALT).triggers);
    assert(bosun_switch_poll(&s, 705, false, BOSUN_SWITCH_LONG_PRESS_ALT).triggers == BOSUN_TRIGGER_LONG_PRESS);
    bosun_switch_reset(&s); /* the action loads a new patch while still held */
    for (uint32_t now = 706; now < 900; ++now)
        assert(!bosun_switch_poll(&s, now, false, BOSUN_SWITCH_LONG_PRESS_ALT).triggers);
    assert(!edge(&s, 900, true, BOSUN_SWITCH_LONG_PRESS_ALT).triggers);
    assert(!edge(&s, 1000, false, BOSUN_SWITCH_LONG_PRESS_ALT).triggers);
    assert(edge(&s, 1100, true, BOSUN_SWITCH_LONG_PRESS_ALT).triggers == BOSUN_TRIGGER_PRESS);
}

static void momentary_hold(void) {
    bosun_switch_fsm s; bosun_switch_init(&s, NULL);
    assert(edge(&s, 100, false, BOSUN_SWITCH_LATCHED).triggers == BOSUN_TRIGGER_TOGGLE_ON);
    assert(!bosun_switch_momentary_active(&s, 604, BOSUN_SWITCH_LATCHED));
    assert(bosun_switch_momentary_active(&s, 605, BOSUN_SWITCH_LATCHED));
    assert(edge(&s, 700, true, BOSUN_SWITCH_LATCHED).triggers == BOSUN_TRIGGER_TOGGLE_OFF);
    assert(!s.latched_on);
    assert(edge(&s, 800, false, BOSUN_SWITCH_LATCHED).triggers == BOSUN_TRIGGER_TOGGLE_ON);
    assert(!edge(&s, 900, true, BOSUN_SWITCH_LATCHED).triggers && s.latched_on);
    assert(edge(&s, 1000, false, BOSUN_SWITCH_LATCHED).triggers == BOSUN_TRIGGER_TOGGLE_OFF);
    assert(edge(&s, 1600, true, BOSUN_SWITCH_LATCHED).triggers == BOSUN_TRIGGER_TOGGLE_ON);
    assert(s.latched_on); /* hold restores an initially ON switch, too */
    assert(edge(&s, 2000, false, BOSUN_SWITCH_MOMENTARY).triggers == BOSUN_TRIGGER_PRESS);
    assert(edge(&s, 2100, true, BOSUN_SWITCH_MOMENTARY).triggers == BOSUN_TRIGGER_RELEASE);
}

static void double_tap_and_wrap(void) {
    bosun_switch_fsm s; bosun_switch_init(&s, NULL);
    assert(!edge(&s, 100, false, BOSUN_SWITCH_DOUBLE_TAP).triggers);
    assert(!edge(&s, 150, true, BOSUN_SWITCH_DOUBLE_TAP).triggers);
    assert(!bosun_switch_poll(&s, 355, true, BOSUN_SWITCH_DOUBLE_TAP).triggers);
    assert(bosun_switch_poll(&s, 356, true, BOSUN_SWITCH_DOUBLE_TAP).triggers == BOSUN_TRIGGER_PRESS);
    assert(!edge(&s, 500, false, BOSUN_SWITCH_DOUBLE_TAP).triggers);
    assert(!edge(&s, 550, true, BOSUN_SWITCH_DOUBLE_TAP).triggers);
    assert(edge(&s, 750, false, BOSUN_SWITCH_DOUBLE_TAP).triggers == BOSUN_TRIGGER_DOUBLE_TAP);
    assert(!edge(&s, 800, true, BOSUN_SWITCH_DOUBLE_TAP).triggers);
    assert(!bosun_switch_poll(&s, 1200, true, BOSUN_SWITCH_DOUBLE_TAP).triggers);
    bosun_switch_init(&s, NULL);
    assert(!edge(&s, UINT32_MAX - 100, false, BOSUN_SWITCH_DOUBLE_TAP).triggers);
    assert(!edge(&s, UINT32_MAX - 50, true, BOSUN_SWITCH_DOUBLE_TAP).triggers);
    assert(bosun_switch_poll(&s, 155, true, BOSUN_SWITCH_DOUBLE_TAP).triggers == BOSUN_TRIGGER_PRESS);
    bosun_switch_init(&s, NULL);
    assert(edge(&s, UINT32_MAX - 2, false, BOSUN_SWITCH_TAP).triggers == BOSUN_TRIGGER_PRESS);
}

int main(void) {
    tap_and_debounce(); long_press_and_reset(); momentary_hold(); double_tap_and_wrap();
    puts("switch FSM: bounce, tap, hold, long, reset, double-tap, wrap passed");
    return 0;
}
