#ifndef BOSUN_EXPRESSION_PRESENCE_H
#define BOSUN_EXPRESSION_PRESENCE_H
#include <stdbool.h>
#include <stdint.h>

#define BOSUN_PRESENCE_INTERVAL_MS 1500u
#define BOSUN_PRESENCE_CHARGE_MS 2u
#define BOSUN_PRESENCE_SETTLE_MS 10u
#define BOSUN_PRESENCE_ABSENT_GAP 30000
#define BOSUN_PRESENCE_ABSENT_STREAK 3u

/* Synchronous, bounded GPIO/ADC operations; no callback may wait or reenter.
 * release must restore the input/ADC state, including after a failed charge.
 * read returns false when no trustworthy ADC sample is available. */
typedef struct {
    bool (*charge)(void *context, uint8_t jack, bool high);
    bool (*release)(void *context, uint8_t jack);
    bool (*read)(void *context, uint8_t jack, uint16_t *raw);
} bosun_expression_presence_backend_t;

typedef enum {
    BOSUN_PRESENCE_IDLE, BOSUN_PRESENCE_CHARGE_HIGH, BOSUN_PRESENCE_SETTLE_HIGH,
    BOSUN_PRESENCE_CHARGE_LOW, BOSUN_PRESENCE_SETTLE_LOW,
    BOSUN_PRESENCE_RECOVER_RELEASE, BOSUN_PRESENCE_RECOVER_SETTLE
} bosun_expression_presence_phase_t;

typedef struct {
    bosun_expression_presence_backend_t backend;
    void *context;
    uint32_t deadline_ms, next_probe_ms, completed, failures;
    int32_t gap[2];
    uint16_t high;
    uint8_t jack, last_jack, enabled_mask, absent_streak[2];
    bosun_expression_presence_phase_t phase;
    bool present[2], known[2], scheduled;
} bosun_expression_presence_t;

/* Starts silent until a valid probe confirms a pedal. At most one jack is
 * probed every 1500 ms, round robin over enabled jacks. A probe drives high
 * for >=2 ms, releases/settles >=10 ms, then repeats for the low rail.
 * This is a cooperative state machine: each call advances one phase only.
 * An enabled-mask bit maps to jack 1/2; higher bits are ignored. */
void bosun_expression_presence_init(bosun_expression_presence_t *presence,
    const bosun_expression_presence_backend_t *backend, void *context);
void bosun_expression_presence_tick(bosun_expression_presence_t *presence,
    uint32_t now_ms, uint8_t enabled_mask);
bool bosun_expression_presence_busy(const bosun_expression_presence_t *presence, uint8_t jack);
bool bosun_expression_presence_present(const bosun_expression_presence_t *presence, uint8_t jack);
/* Before tick, sample a jack only when busy() is false and retain that normal
 * sample throughout the probe. Never feed charged/settling reads into the
 * expression smoother. On disable, release is immediate; busy remains true
 * through recovery settling, and a release failure is retried each tick. */
#endif
