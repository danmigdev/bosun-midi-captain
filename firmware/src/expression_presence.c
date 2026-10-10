#include "bosun/expression_presence.h"
#include <string.h>

_Static_assert(sizeof(bosun_expression_presence_t) <= 128, "presence state exceeds 128-byte budget");

static bool due(uint32_t now, uint32_t deadline) { return (int32_t)(now - deadline) >= 0; }

void bosun_expression_presence_init(bosun_expression_presence_t *p,
    const bosun_expression_presence_backend_t *backend, void *context) {
    if (!p) return;
    memset(p, 0, sizeof *p);
    if (backend) p->backend = *backend;
    p->context = context;
    p->last_jack = 2;
}

bool bosun_expression_presence_busy(const bosun_expression_presence_t *p, uint8_t jack) {
    return p && jack >= 1 && jack <= 2 && p->phase != BOSUN_PRESENCE_IDLE && p->jack == jack;
}

bool bosun_expression_presence_present(const bosun_expression_presence_t *p, uint8_t jack) {
    return p && jack >= 1 && jack <= 2 && p->present[jack - 1];
}

static void recover(bosun_expression_presence_t *p, uint32_t now) {
    if (p->backend.release && p->backend.release(p->context, p->jack)) {
        p->phase = BOSUN_PRESENCE_RECOVER_SETTLE;
        p->deadline_ms = now + BOSUN_PRESENCE_SETTLE_MS;
    } else p->phase = BOSUN_PRESENCE_RECOVER_RELEASE;
}

static void failed(bosun_expression_presence_t *p, uint32_t now) {
    ++p->failures;
    recover(p, now);
}

void bosun_expression_presence_tick(bosun_expression_presence_t *p,
    uint32_t now, uint8_t enabled_mask) {
    if (!p) return;
    enabled_mask &= 3u;
    for (unsigned i = 0; i < 2; ++i) if (!(enabled_mask & (1u << i))) {
        p->present[i] = p->known[i] = false;
        p->absent_streak[i] = 0;
    }
    p->enabled_mask = enabled_mask;
    if (p->phase != BOSUN_PRESENCE_IDLE && p->phase != BOSUN_PRESENCE_RECOVER_RELEASE &&
        p->phase != BOSUN_PRESENCE_RECOVER_SETTLE && !(enabled_mask & (1u << (p->jack - 1)))) {
        recover(p, now);
        return;
    }
    if (p->phase == BOSUN_PRESENCE_RECOVER_RELEASE) { recover(p, now); return; }
    if (p->phase == BOSUN_PRESENCE_IDLE) {
        if (!enabled_mask || (p->scheduled && !due(now, p->next_probe_ms))) return;
        /* An unavailable backend remains silent and never drives a pin. */
        if (!p->backend.charge || !p->backend.release || !p->backend.read) return;
        p->jack = p->last_jack == 1 ? 2 : 1;
        if (!(enabled_mask & (1u << (p->jack - 1)))) p->jack = p->jack == 1 ? 2 : 1;
        p->last_jack = p->jack;
        p->scheduled = true; p->next_probe_ms = now + BOSUN_PRESENCE_INTERVAL_MS;
        if (!p->backend.charge(p->context, p->jack, true)) { failed(p, now); return; }
        p->phase = BOSUN_PRESENCE_CHARGE_HIGH;
        p->deadline_ms = now + BOSUN_PRESENCE_CHARGE_MS;
        return;
    }
    if (!due(now, p->deadline_ms)) return;
    switch (p->phase) {
    case BOSUN_PRESENCE_CHARGE_HIGH:
    case BOSUN_PRESENCE_CHARGE_LOW: {
        bool high = p->phase == BOSUN_PRESENCE_CHARGE_HIGH;
        if (!p->backend.release(p->context, p->jack)) { failed(p, now); return; }
        p->phase = high ? BOSUN_PRESENCE_SETTLE_HIGH : BOSUN_PRESENCE_SETTLE_LOW;
        p->deadline_ms = now + BOSUN_PRESENCE_SETTLE_MS;
        break;
    }
    case BOSUN_PRESENCE_SETTLE_HIGH:
        if (!p->backend.read(p->context, p->jack, &p->high)) { failed(p, now); return; }
        if (!p->backend.charge(p->context, p->jack, false)) { failed(p, now); return; }
        p->phase = BOSUN_PRESENCE_CHARGE_LOW;
        p->deadline_ms = now + BOSUN_PRESENCE_CHARGE_MS;
        break;
    case BOSUN_PRESENCE_SETTLE_LOW: {
        uint16_t low;
        if (!p->backend.read(p->context, p->jack, &low)) { failed(p, now); return; }
        unsigned i = p->jack - 1;
        p->gap[i] = (int32_t)p->high - low;
        p->known[i] = true; ++p->completed;
        if (p->gap[i] > BOSUN_PRESENCE_ABSENT_GAP) {
            if (p->absent_streak[i] < BOSUN_PRESENCE_ABSENT_STREAK) ++p->absent_streak[i];
            if (p->absent_streak[i] >= BOSUN_PRESENCE_ABSENT_STREAK) p->present[i] = false;
        } else {
            p->absent_streak[i] = 0;
            p->present[i] = true;
        }
        p->phase = BOSUN_PRESENCE_IDLE;
        break;
    }
    case BOSUN_PRESENCE_RECOVER_SETTLE:
        p->phase = BOSUN_PRESENCE_IDLE;
        break;
    case BOSUN_PRESENCE_IDLE: case BOSUN_PRESENCE_RECOVER_RELEASE:
        break;
    }
}
