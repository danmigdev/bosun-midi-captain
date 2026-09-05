#include "bosun/expression_presence.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

typedef struct {
    bool driven[2], high[2], fail_charge, fail_release, fail_read;
    uint16_t hi[2], lo[2];
    uint32_t charges, releases, reads, now;
    struct { char kind; uint8_t jack; uint32_t now; } events[256];
    unsigned count;
} fake_board;

static void event(fake_board *f, char kind, uint8_t jack) {
    assert(jack >= 1 && jack <= 2 && f->count < 256);
    f->events[f->count].kind = kind; f->events[f->count].jack = jack;
    f->events[f->count++].now = f->now;
}
static bool charge(void *context, uint8_t jack, bool high) {
    fake_board *f = context;
    event(f, high ? 'H' : 'L', jack); ++f->charges;
    /* Model partial failure: even false may have changed the GPIO. */
    f->driven[jack - 1] = true; f->high[jack - 1] = high;
    return !f->fail_charge;
}
static bool release(void *context, uint8_t jack) {
    fake_board *f = context;
    event(f, 'R', jack); ++f->releases;
    if (f->fail_release) return false;
    f->driven[jack - 1] = false;
    return true;
}
static bool read_adc(void *context, uint8_t jack, uint16_t *raw) {
    fake_board *f = context;
    event(f, 'A', jack); ++f->reads;
    assert(!f->driven[jack - 1] && raw);
    if (f->fail_read) return false;
    *raw = f->high[jack - 1] ? f->hi[jack - 1] : f->lo[jack - 1];
    return true;
}
static void init(bosun_expression_presence_t *p, fake_board *f) {
    memset(f, 0, sizeof *f);
    f->hi[0] = f->hi[1] = 65535;
    const bosun_expression_presence_backend_t backend = {charge, release, read_adc};
    bosun_expression_presence_init(p, &backend, f);
}
static void tick(bosun_expression_presence_t *p, fake_board *f, uint32_t now, uint8_t mask) {
    f->now = now;
    bosun_expression_presence_tick(p, now, mask);
}
static void probe(bosun_expression_presence_t *p, fake_board *f, uint32_t now, uint8_t mask) {
    unsigned initial = f->count;
    tick(p, f, now, mask);
    uint8_t jack = p->jack;
    assert(bosun_expression_presence_busy(p, jack));
    tick(p, f, now + 1, mask); assert(f->count == initial + 1);
    tick(p, f, now + 2, mask); assert(f->count == initial + 2 && !f->driven[jack - 1]);
    tick(p, f, now + 11, mask); assert(f->count == initial + 2);
    tick(p, f, now + 12, mask); assert(f->count == initial + 4 && f->driven[jack - 1]);
    tick(p, f, now + 13, mask); assert(f->count == initial + 4);
    tick(p, f, now + 14, mask); assert(f->count == initial + 5 && !f->driven[jack - 1]);
    tick(p, f, now + 23, mask); assert(f->count == initial + 5);
    tick(p, f, now + 24, mask);
    assert(f->count == initial + 6 && !bosun_expression_presence_busy(p, jack));
    static const char operations[] = "HRALRA";
    static const uint8_t times[] = {0, 2, 12, 12, 14, 24};
    for (unsigned i = 0; i < 6; ++i) {
        assert(f->events[initial + i].kind == operations[i]);
        assert(f->events[initial + i].jack == jack && f->events[initial + i].now == now + times[i]);
    }
}

static void test_startup_and_debounce(void) {
    bosun_expression_presence_t p; fake_board f; init(&p, &f);
    assert(!bosun_expression_presence_present(&p, 1) && !bosun_expression_presence_present(&p, 2));
    probe(&p, &f, 0, 1);
    assert(p.completed == 1 && p.known[0] && p.gap[0] == 65535 && p.absent_streak[0] == 1 && !p.present[0]);
    tick(&p, &f, 1499, 1); assert(f.count == 6 && !bosun_expression_presence_busy(&p, 1));
    f.hi[0] = 33200; f.lo[0] = 30700;
    probe(&p, &f, 1500, 1); assert(p.present[0] && p.absent_streak[0] == 0 && p.gap[0] == 2500);
    f.hi[0] = 65535; f.lo[0] = 0;
    probe(&p, &f, 3000, 1); assert(p.present[0] && p.absent_streak[0] == 1);
    probe(&p, &f, 4500, 1); assert(p.present[0] && p.absent_streak[0] == 2);
    probe(&p, &f, 6000, 1); assert(!p.present[0] && p.absent_streak[0] == 3);
    probe(&p, &f, 7500, 1); assert(!p.present[0] && p.absent_streak[0] == 3);
    /* Threshold is strict, and a single pot observation restores presence. */
    f.hi[0] = 30000; f.lo[0] = 0;
    probe(&p, &f, 9000, 1); assert(p.present[0] && p.absent_streak[0] == 0);
    f.hi[0] = 0; f.lo[0] = 65535;
    probe(&p, &f, 10500, 1); assert(p.present[0] && p.gap[0] == -65535);
    assert(p.failures == 0 && p.completed == 8);
}

static void test_round_robin_and_wrap(void) {
    bosun_expression_presence_t p; fake_board f; init(&p, &f);
    f.hi[0] = f.lo[0] = 32000;
    probe(&p, &f, 0, 3); assert(p.present[0] && !p.known[1]);
    probe(&p, &f, 1500, 3); assert(p.jack == 2 && !p.present[1] && p.absent_streak[1] == 1);
    probe(&p, &f, 3000, 3); assert(p.jack == 1 && p.present[0]);
    probe(&p, &f, 4500, 2); assert(p.jack == 2 && !p.present[0] && !p.known[0]);
    probe(&p, &f, 6000, 2); assert(p.jack == 2 && !p.present[1]);
    init(&p, &f); f.hi[0] = f.lo[0] = 15000;
    uint32_t start = UINT32_MAX - 1;
    probe(&p, &f, start, 1);
    assert(p.present[0] && p.completed == 1);
    tick(&p, &f, start + 1499, 1); assert(p.completed == 1 && f.count == 6);
    probe(&p, &f, start + 1500, 1); assert(p.completed == 2);
}

static void test_failures_and_late_ticks(void) {
    bosun_expression_presence_t p; fake_board f; init(&p, &f);
    tick(&p, &f, 0, 1);
    tick(&p, &f, 100, 1); /* Late tick releases, then grants a full settle. */
    assert(p.phase == BOSUN_PRESENCE_SETTLE_HIGH && p.deadline_ms == 110 && f.reads == 0);
    tick(&p, &f, 109, 1); assert(f.reads == 0);
    f.fail_read = true; tick(&p, &f, 110, 1);
    assert(p.failures == 1 && p.completed == 0 && !p.known[0] && !p.present[0]);
    assert(p.phase == BOSUN_PRESENCE_RECOVER_SETTLE && !f.driven[0]);
    tick(&p, &f, 119, 1); assert(bosun_expression_presence_busy(&p, 1));
    tick(&p, &f, 120, 1); assert(!bosun_expression_presence_busy(&p, 1));
    f.fail_read = false; f.hi[0] = f.lo[0] = 32000;
    probe(&p, &f, 1500, 1); assert(p.present[0]);
    f.fail_charge = true; tick(&p, &f, 3000, 1);
    assert(p.failures == 2 && p.completed == 1 && p.present[0] && !f.driven[0]);
    tick(&p, &f, 3010, 1); f.fail_charge = false;
    tick(&p, &f, 4500, 1); f.fail_release = true;
    tick(&p, &f, 4502, 1);
    assert(p.failures == 3 && p.phase == BOSUN_PRESENCE_RECOVER_RELEASE && f.driven[0]);
    uint32_t reads = f.reads;
    tick(&p, &f, 4600, 1); tick(&p, &f, 4601, 1);
    assert(f.reads == reads && bosun_expression_presence_busy(&p, 1) && f.driven[0]);
    f.fail_release = false; tick(&p, &f, 4602, 1);
    assert(!f.driven[0] && bosun_expression_presence_busy(&p, 1));
    tick(&p, &f, 4611, 1); assert(bosun_expression_presence_busy(&p, 1));
    tick(&p, &f, 4612, 1); assert(!bosun_expression_presence_busy(&p, 1) && p.present[0]);
    /* Low-rail failures cannot turn an incomplete measurement into presence. */
    init(&p, &f); tick(&p, &f, 0, 1); tick(&p, &f, 2, 1);
    f.fail_charge = true; tick(&p, &f, 12, 1);
    assert(p.failures == 1 && p.completed == 0 && !f.driven[0] && !p.present[0]);
    init(&p, &f); tick(&p, &f, 0, 1); tick(&p, &f, 2, 1);
    tick(&p, &f, 12, 1); tick(&p, &f, 14, 1); f.fail_read = true;
    tick(&p, &f, 24, 1);
    assert(p.failures == 1 && p.completed == 0 && !p.known[0] && !p.present[0]);
}

static void test_disable_and_missing_backend(void) {
    bosun_expression_presence_t p; fake_board f;
    static const uint32_t at[] = {1, 3, 13, 15};
    for (unsigned phase = 0; phase < 4; ++phase) {
        init(&p, &f); tick(&p, &f, 0, 1);
        if (phase >= 1) tick(&p, &f, 2, 1);
        if (phase >= 2) tick(&p, &f, 12, 1);
        if (phase >= 3) tick(&p, &f, 14, 1);
        tick(&p, &f, at[phase], 0);
        assert(!f.driven[0] && !p.present[0] && p.phase == BOSUN_PRESENCE_RECOVER_SETTLE);
        assert(p.completed == 0 && p.failures == 0);
        tick(&p, &f, at[phase] + 9, 0); assert(bosun_expression_presence_busy(&p, 1));
        tick(&p, &f, at[phase] + 10, 0); assert(!bosun_expression_presence_busy(&p, 1));
        uint32_t charges = f.charges;
        tick(&p, &f, 10000, 0); assert(f.charges == charges);
        /* Re-enabled jacks must be confirmed again. */
        f.hi[0] = f.lo[0] = 40000;
        probe(&p, &f, 10001, 1); assert(p.present[0]);
    }
    init(&p, &f); tick(&p, &f, 0, 1); f.fail_release = true;
    tick(&p, &f, 1, 0); assert(f.driven[0] && bosun_expression_presence_busy(&p, 1));
    f.fail_release = false; tick(&p, &f, 2, 0); tick(&p, &f, 12, 0);
    assert(!f.driven[0] && !bosun_expression_presence_busy(&p, 1));
    bosun_expression_presence_init(&p, NULL, NULL);
    bosun_expression_presence_tick(&p, 0, 255);
    assert(!p.present[0] && !p.present[1] && !bosun_expression_presence_busy(&p, 1));
    assert(!bosun_expression_presence_busy(NULL, 1) && !bosun_expression_presence_present(NULL, 1));
    assert(!bosun_expression_presence_present(&p, 0) && !bosun_expression_presence_busy(&p, 3));
    bosun_expression_presence_init(NULL, NULL, NULL); bosun_expression_presence_tick(NULL, 0, 0);
}

int main(void) {
    test_startup_and_debounce(); test_round_robin_and_wrap();
    test_failures_and_late_ticks(); test_disable_and_missing_backend();
    puts("Expression presence: cooperative timing, floating/pot startup, debounce, round robin, rollover, invalid samples, GPIO recovery and disabled cancellation passed");
    return 0;
}
