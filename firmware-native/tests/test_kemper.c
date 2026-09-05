#include "bosun/kemper.h"

#include <assert.h>
#include <stdio.h>
#include <string.h>

typedef struct {
    uint8_t packets[512][16];
    size_t lengths[512], count;
    bool fail;
} wire;

static bool send_packet(void *context, const uint8_t *data, size_t length) {
    wire *w = context;
    assert(w->count < 512 && length <= 16);
    memcpy(w->packets[w->count], data, length);
    w->lengths[w->count++] = length;
    return !w->fail;
}

static void sense(bosun_kemper *k, uint32_t now) {
    const uint8_t payload[] = {0,0x20,0x33,2,127,0x7e};
    bosun_kemper_handle(k, 0, 0xf0, payload, sizeof(payload), now);
}

static void param(bosun_kemper *k, uint8_t page, uint8_t address,
                   uint16_t value, uint32_t now) {
    const uint8_t payload[] = {0,0x20,0x33,2,127,1,0,page,address,
        (uint8_t)(value >> 7), (uint8_t)(value & 127)};
    bosun_kemper_handle(k, 0, 0xf0, payload, sizeof(payload), now);
}

static void name(bosun_kemper *k, const char *text, uint32_t now) {
    uint8_t payload[128] = {0,0x20,0x33,2,127,3,0,0,1};
    size_t length = strlen(text);
    assert(length < sizeof(payload) - 9);
    memcpy(payload + 9, text, length + 1);
    bosun_kemper_handle(k, 0, 0xf0, payload, length + 10, now);
}

static void pc(bosun_kemper *k, uint8_t value, uint32_t now) {
    bosun_kemper_handle(k, 1, 0xc0, &value, 1, now);
}

static void cc(bosun_kemper *k, uint8_t controller, uint8_t value, uint32_t now) {
    const uint8_t data[] = {controller,value};
    bosun_kemper_handle(k, 1, 0xb0, data, sizeof(data), now);
}

static void bank_header(bosun_kemper *k, uint32_t now) {
    const uint8_t payload[] = {0,0x20,0x33,0,0,7,0,0,0,1,0,0,'B','a','n','k',0};
    bosun_kemper_handle(k, 0, 0xf0, payload, sizeof(payload), now);
}

static size_t queries(const wire *w, uint8_t page, uint8_t address) {
    size_t count = 0;
    for (size_t i = 0; i < w->count; ++i)
        if (w->lengths[i] == 11 && w->packets[i][6] == 0x41 &&
            w->packets[i][8] == page && w->packets[i][9] == address) ++count;
    return count;
}

static size_t name_queries(const wire *w) {
    static const uint8_t request[] = {0xf0,0,0x20,0x33,2,0x7f,0x43,0,0,1,0xf7};
    size_t count = 0;
    for (size_t i = 0; i < w->count; ++i)
        if (w->lengths[i] == sizeof(request) && !memcmp(w->packets[i], request, sizeof(request))) ++count;
    return count;
}

static void ready(bosun_kemper *k, wire *w) {
    memset(w, 0, sizeof(*w));
    bosun_kemper_init(k, 1, 0, send_packet, w);
    sense(k, 100);
    pc(k, 2, 100);
    name(k, "Crunch", 120);
    bosun_kemper_tick(k, 600);
    assert(k->state.rig == 3 && k->state.rig_name_fresh);
    assert(!strcmp(k->state.rig_name, "Crunch"));
    assert(queries(w, 5, 21) == 1);
}

static void codecs_and_beacon(void) {
    bosun_kemper k; wire w = {0};
    bosun_kemper_init(&k, 3, 0, send_packet, &w);
    assert(!bosun_kemper_select_rig(&k, 0, 1, 10));
    assert(!bosun_kemper_select_rig(&k, 1, 6, 10));
    assert(w.count == 0);
    assert(bosun_kemper_select_rig(&k, 3, 2, 100));
    const uint8_t bank_msb[] = {0xb2,0,0}, bank_lsb[] = {0xb2,32,0};
    assert(w.count == 2 && !memcmp(w.packets[0], bank_msb, 3));
    assert(!memcmp(w.packets[1], bank_lsb, 3));
    bosun_kemper_tick(&k, 104);
    assert(w.count == 3 && w.packets[2][10] == 0x23); /* initial lease */
    bosun_kemper_tick(&k, 105);
    assert(w.count == 4 && w.lengths[3] == 2 && w.packets[3][0] == 0xc2 && w.packets[3][1] == 11);
    assert(k.local_pc.valid);
    assert(bosun_kemper_command(&k, BOSUN_KEMPER_FIXED, 3, 1));
    assert(w.packets[4][1] == 99 && w.packets[4][2] == 5);
    assert(w.packets[5][1] == 98 && w.packets[5][2] == 21);
    assert(w.packets[6][1] == 6 && w.packets[6][2] == 0);
    assert(w.packets[7][1] == 38 && w.packets[7][2] == 1);
    assert(k.state.expression_mode == BOSUN_EXPRESSION_UNKNOWN);
    assert(bosun_kemper_command(&k, BOSUN_KEMPER_EFFECT, BOSUN_KEMPER_DELAY, 1));
    assert(w.packets[8][1] == 27 && w.packets[8][2] == 127);
    assert(bosun_kemper_command(&k, BOSUN_KEMPER_TEMPO, 0, 300));
    assert(w.packets[9][1] == 92 && w.packets[9][2] == 1);
    assert(w.packets[10][1] == 93 && w.packets[10][2] == 122);
    assert(!bosun_kemper_command(&k, BOSUN_KEMPER_EFFECT, 8, 1));
    bosun_kemper_tick(&k, 1103);
    size_t before = w.count;
    bosun_kemper_tick(&k, 1104);
    assert(w.count == before + 1 && w.packets[before][10] == 0x23);
    sense(&k, 1105);
    bosun_kemper_tick(&k, 6104);
    assert(w.packets[w.count - 1][10] == 0x22);
    bosun_kemper_tick(&k, 16106);
    assert(!k.state.connected && w.packets[w.count - 1][10] == 0x23);
    w.fail = true;
    assert(!bosun_kemper_command(&k, BOSUN_KEMPER_TUNER, 0, 1) && k.tx_failures == 1);
}

static void tuner_and_defensive_input(void) {
    bosun_kemper k; wire w = {0};
    bosun_kemper_init(&k, 1, 0, send_packet, &w);
    param(&k, 125, 84, 69, 10);
    assert(!k.state.tuner_note[0]);
    param(&k, 127, 126, 1, 20);
    param(&k, 125, 84, 69, 21);
    param(&k, 124, 15, 8100, 22);
    assert(k.state.tuner_active && !strcmp(k.state.tuner_note, "A") && k.state.tuner_deviance == 8100);
    param(&k, 127, 126, 3, 30);
    uint32_t revision = k.state.revision;
    param(&k, 124, 0, 9000, 31);
    param(&k, 124, 15, 9000, 32);
    param(&k, 125, 84, 60, 33);
    assert(!k.state.tuner_active && k.state.revision == revision);
    cc(&k, 31, 127, 34);
    assert(k.state.tuner_active);
    param(&k, 4, 0, 120 * 64 + 32, 40);
    assert(k.state.bpm == 120);
    param(&k, 4, 0, 121 * 64 + 32, 40);
    assert(k.state.bpm == 122);
    const uint8_t bad[] = {1,0x20,0x33,2,127,1,0,127,126,0,0};
    bosun_kemper_handle(&k, 0, 0xf0, bad, sizeof(bad), 50);
    bosun_kemper_handle(&k, 0, 0xf0, bad, 2, 50);
    const uint8_t off[] = {31,0};
    bosun_kemper_handle(&k, 2, 0xb0, off, 2, 50);
    assert(k.state.tuner_active);
    pc(&k, 127, 51);
    assert(k.state.rig == 1);
}

static void pc_echo_and_rig_names(void) {
    bosun_kemper k; wire w = {0};
    bosun_kemper_init(&k, 1, 1u << BOSUN_KEMPER_X, send_packet, &w);
    assert(bosun_kemper_select_rig(&k, 3, 2, 100));
    bosun_kemper_tick(&k, 105);
    name(&k, "CLEAN", 120);
    bosun_kemper_tick(&k, 600);
    assert(queries(&w, 56, 3) == 1 && k.reconcile_pending);
    uint32_t generation = k.generation;
    pc(&k, 11, 601); /* real hardware ordering: query, PC echo, replies */
    assert(k.generation == generation && k.reconcile_pending && !k.local_pc.valid);
    param(&k, 56, 3, 1, 604);
    assert(k.state.effects[BOSUN_KEMPER_X] && k.state.rig_name_fresh);
    assert(k.state.bank == 3 && !strcmp(k.state.rig_name, "CLEAN"));
    assert(k.state.external_rig_changes == 0);
    assert(bosun_kemper_select_rig(&k, 1, 1, 700));
    bosun_kemper_tick(&k, 705);
    pc(&k, 2, 800); /* an external change supersedes the local target */
    assert(k.state.rig == 3 && k.state.external_rig_changes == 1);
    pc(&k, 0, 900); /* late echo must not jump back to local target */
    assert(k.state.rig == 3 && k.state.external_rig_changes == 1);
    name(&k, "CLEAN", 910); name(&k, "Crunch", 1010);
    bosun_kemper_tick(&k, 1300); /* old query is still quarantined */
    bosun_kemper_tick(&k, 1800);
    param(&k, 56, 3, 0, 1801);
    assert(k.state.rig_name_fresh && !strcmp(k.state.rig_name, "Crunch"));
    generation = k.generation;
    name(&k, "Crunch", 1900);
    assert(k.generation == generation);
    name(&k, "External renamed rig", 2000);
    assert(k.generation != generation && !k.state.rig_name_fresh);
}

static void generation_and_live_cc_fences(void) {
    bosun_kemper k; wire w = {0};
    bosun_kemper_init(&k, 1, 1u << BOSUN_KEMPER_X, send_packet, &w);
    assert(bosun_kemper_begin_rig(&k, 1, 100));
    bosun_kemper_tick(&k, 600);
    assert(queries(&w, 56, 3) == 1);
    assert(bosun_kemper_begin_rig(&k, 2, 700));
    param(&k, 56, 3, 0, 800); /* untagged old query reply */
    assert(!k.state.effect_known);
    bosun_kemper_tick(&k, 1200);
    assert(queries(&w, 56, 3) == 1);
    bosun_kemper_tick(&k, 1800);
    assert(queries(&w, 56, 3) == 2);
    bosun_kemper_tick(&k, 2200); /* retry missing X */
    assert(queries(&w, 56, 3) == 3);
    cc(&k, 22, 127, 2201);
    assert(k.state.effects[BOSUN_KEMPER_X] && !k.reconcile_pending);
    uint32_t revision = k.state.revision;
    param(&k, 56, 3, 0, 2202); param(&k, 56, 3, 0, 2203);
    assert(k.state.effects[BOSUN_KEMPER_X] && k.state.revision == revision);
    assert(!k.guard_budget[BOSUN_KEMPER_X]);
    assert(bosun_kemper_begin_rig(&k, 3, 4000));
    bosun_kemper_tick(&k, 4500); bosun_kemper_tick(&k, 4900);
    bosun_kemper_tick(&k, 5300);
    assert(!k.state.effect_known && !bosun_kemper_transition_active(&k));
}

static void crunch_wah_and_discovery(void) {
    static const uint8_t pages[] = {50,51,52,53,56,58,60,61};
    static const uint8_t on_pages[] = {50,51,52,53,56,58,74,75};
    static const uint8_t addresses[] = {3,3,3,3,3,3,2,2};
    static const uint8_t controllers[] = {17,18,19,20,22,24,27,29};
    /* Every physical slot: type discovery and on/off use different pages for
     * Delay/Reverb. Fixed Wah OFF alone cannot establish VOL. */
    for (unsigned slot = 0; slot < 8; ++slot) {
        bosun_kemper k; wire w;
        ready(&k, &w);
        param(&k, 5, 21, 0, 601);
        assert(k.state.expression_mode == BOSUN_EXPRESSION_UNKNOWN);
        uint32_t now = 621;
        for (unsigned i = 0; i < 8; ++i) {
            bosun_kemper_tick(&k, now);
            assert(queries(&w, pages[i], 0) == 1);
            param(&k, pages[i], 0, i == slot ? 1 : 0, now + 1);
            now += 21;
            if (i == slot) {
                bosun_kemper_tick(&k, now);
                assert(queries(&w, on_pages[i], addresses[i]) == 1);
                param(&k, on_pages[i], addresses[i], 1, now + 1);
                assert(k.state.expression_mode == BOSUN_EXPRESSION_WAH);
                now += 21;
            }
        }
        assert(k.wah_types == 255 && k.state.expression_mode == BOSUN_EXPRESSION_WAH);
        cc(&k, controllers[slot], 0, now);
        assert(k.state.expression_mode == BOSUN_EXPRESSION_VOL);
        cc(&k, controllers[slot], 127, now + 1);
        assert(k.state.expression_mode == BOSUN_EXPRESSION_WAH);
        assert(!strcmp(bosun_kemper_expression_label(k.state.expression_mode), "WAH"));
    }
}

static void wah_timeout_generation_and_slot_fence(void) {
    bosun_kemper k; wire w;
    ready(&k, &w);
    param(&k, 5, 21, 1, 601);
    assert(k.state.expression_mode == BOSUN_EXPRESSION_WAH);
    assert(bosun_kemper_begin_rig(&k, 4, 610));
    assert(k.state.expression_mode == BOSUN_EXPRESSION_UNKNOWN);
    name(&k, "Lead", 650);
    bosun_kemper_tick(&k, 1110);
    param(&k, 5, 21, 1, 1120);
    assert(k.state.expression_mode == BOSUN_EXPRESSION_UNKNOWN);
    assert(queries(&w, 5, 21) == 1);
    bosun_kemper_tick(&k, 1800);
    assert(queries(&w, 5, 21) == 2);
    param(&k, 5, 21, 1, 1801);
    assert(k.state.expression_mode == BOSUN_EXPRESSION_WAH);
    bosun_kemper_tick(&k, 1821); /* type poll never answered */
    bosun_kemper_tick(&k, 3021);
    assert(k.state.expression_mode == BOSUN_EXPRESSION_UNKNOWN);
    bosun_kemper_tick(&k, 4221); bosun_kemper_tick(&k, 5421);
    assert(queries(&w, 50, 0) == 3);
    assert(k.wah_next_ms == 10421 && !k.wah_pending);
    ready(&k, &w);
    param(&k, 5, 21, 0, 601);
    bosun_kemper_tick(&k, 621); param(&k, 50, 0, 1, 622);
    bosun_kemper_tick(&k, 642); /* slot A on/off is in flight */
    assert(queries(&w, 50, 3) == 1);
    bosun_kemper_set_bound_blocks(&k, 1);
    assert(bosun_kemper_begin_rig(&k, 5, 650));
    bosun_kemper_tick(&k, 1150);
    param(&k, 50, 3, 1, 1151);
    assert(!k.state.effect_known && k.state.expression_mode == BOSUN_EXPRESSION_UNKNOWN);
    bosun_kemper_tick(&k, 1842);
    assert(queries(&w, 50, 3) == 2);
    param(&k, 50, 3, 0, 1843);
    assert(k.state.effect_known == 1 && !k.state.effects[0]);
}

static void names_bounded_and_pc_wrap(void) {
    bosun_kemper k; wire w = {0};
    bosun_kemper_init(&k, 1, 0, send_packet, &w);
    pc(&k, 0, 0);
    char long_name[90]; memset(long_name, 'A', 89); long_name[89] = 0;
    name(&k, long_name, 10); bosun_kemper_tick(&k, 510);
    assert(strlen(k.state.rig_name) == 64);
    /* Start a separate clock epoch near rollover. Jumping from 510 to
     * UINT32_MAX would also legitimately expire the unrelated beacon lease. */
    memset(&w, 0, sizeof(w));
    bosun_kemper_init(&k, 1, 0, send_packet, &w);
    bosun_kemper_tick(&k, UINT32_MAX - 20);
    assert(bosun_kemper_select_rig(&k, 25, 5, UINT32_MAX - 2));
    size_t before = w.count;
    bosun_kemper_tick(&k, 1);
    assert(w.count == before);
    bosun_kemper_tick(&k, 2);
    assert(w.count == before + 1 && w.packets[before][0] == 0xc0 && w.packets[before][1] == 124);
    uint32_t generation = k.generation;
    pc(&k, 124, 3);
    assert(k.generation == generation && !k.local_pc.valid);
}

static void boot_name_and_live_cc_deadline(void) {
    bosun_kemper k; wire w = {0};
    bosun_kemper_init(&k, 1, 1u << BOSUN_KEMPER_X, send_packet, &w);
    pc(&k, 0, 0);
    name(&k, "First rig", 100);
    bosun_kemper_tick(&k, 500);
    param(&k, 56, 3, 0, 501);
    assert(k.state.rig_name_fresh && !strcmp(k.state.rig_name, "First rig"));
    assert(bosun_kemper_begin_rig(&k, 3, 1000));
    cc(&k, 22, 127, 1499);
    assert(!k.state.effect_known);
    cc(&k, 22, 127, 1500);
    assert(k.state.effect_known == (1u << BOSUN_KEMPER_X));
    assert(k.state.effects[BOSUN_KEMPER_X]);
    assert(k.settle_active); /* publish does not bypass reconciliation */
    bosun_kemper_tick(&k, 1500);
    assert(k.reconcile_pending == (1u << BOSUN_KEMPER_X));
}

static void message_channel_overrides(void) {
    bosun_kemper k; wire w = {0};
    bosun_kemper_init(&k, 3, 0, send_packet, &w);
    assert(!bosun_kemper_select_rig_channel(&k, 0, 1, 1, 100));
    assert(!bosun_kemper_command_channel(&k, 17, BOSUN_KEMPER_TUNER, 0, 1));
    assert(!w.count && k.channel == 3);
    assert(bosun_kemper_select_rig_channel(&k, 10, 3, 2, 100));
    assert(w.count == 2 && w.packets[0][0] == 0xb9 && w.packets[1][0] == 0xb9);
    assert(k.channel == 3);
    assert(bosun_kemper_command_channel(&k, 16, BOSUN_KEMPER_LOOPER, 2, 1));
    assert(w.packets[2][0] == 0xbf && w.packets[2][1] == 91 && w.packets[2][2] == 127);
    assert(k.channel == 3);
    k.channel = 4; /* configuration edit before the deferred PC is emitted */
    bosun_kemper_tick(&k, 105);
    assert(w.packets[3][0] == 0xc9 && w.packets[3][1] == 11);
    assert(k.channel == 4);
    w.fail = true;
    assert(!bosun_kemper_command_channel(&k, 12, BOSUN_KEMPER_FIXED, 3, 1));
    assert(k.channel == 4); /* failure must restore the configured channel */
}

static void reinitialize_wah_after_long_uptime(void) {
    const uint32_t starts[] = {UINT32_C(0x90000000), UINT32_MAX - 1000};
    for (unsigned i = 0; i < sizeof(starts) / sizeof(starts[0]); ++i) {
        uint32_t now = starts[i];
        bosun_kemper k; wire w = {0};
        bosun_kemper_init(&k, 1, 0, send_packet, &w);
        sense(&k, now);
        pc(&k, 0, now);
        name(&k, "Current rig", now + 1);
        bosun_kemper_tick(&k, now + 501);
        assert(queries(&w, 5, 21) == 1);
        param(&k, 5, 21, 1, now + 502);
        assert(k.state.expression_mode == BOSUN_EXPRESSION_WAH);
    }
}

static void bootstrap_identity_and_string_recovery(void) {
    bosun_kemper k; wire w = {0};
    bosun_kemper_init(&k, 1, 0, send_packet, &w);
    bosun_kemper_tick(&k, 0);
    sense(&k, 10);
    name(&k, "CRUNCH", 11); /* Hardware boot: name precedes current PC2. */
    assert(!k.rig_identity_known && !k.state.rig_name_fresh && !k.last_name_rig);
    assert(!k.state.rig_name[0] && !k.last_name[0]);
    assert(!bosun_kemper_request_rig_name(&k, 12));
    for (uint32_t now = 12; now < 1000; ++now) bosun_kemper_tick(&k, now);
    assert(w.count == 1); /* Sensing alone must not end the snapshot retry. */
    bosun_kemper_tick(&k, 1000);
    assert(w.count == 2 && w.packets[1][10] == 0x23);
    pc(&k, 2, 1010);
    assert(k.rig_identity_known && k.state.rig == 3 && k.state.external_rig_changes == 1);
    bosun_kemper_tick(&k, 1509);
    assert(!name_queries(&w));
    bosun_kemper_tick(&k, 1510);
    assert(name_queries(&w) == 1 && !k.state.rig_name_fresh);
    param(&k, 0, 1, 0, 1511); /* Numeric 0x01 is not a name response. */
    assert(!k.state.rig_name_fresh);
    for (uint32_t now = 1511; now < 2710; ++now) bosun_kemper_tick(&k, now);
    assert(name_queries(&w) == 1);
    bosun_kemper_tick(&k, 2710);
    assert(name_queries(&w) == 2);
    name(&k, "CRUNCH", 2711);
    assert(k.last_name_rig == 3 && k.state.rig_name_fresh && !strcmp(k.last_name, "CRUNCH"));
    bosun_kemper_tick(&k, 3300);
    assert(name_queries(&w) == 2 && !k.bootstrap_name_pending);
}

static void duplicate_names_require_current_string_request(void) {
    bosun_kemper k; wire w;
    ready(&k, &w);
    assert(bosun_kemper_select_rig(&k, 2, 4, 700));
    bosun_kemper_tick(&k, 705);
    name(&k, "Crunch", 720); /* Could be the previous rig's delayed broadcast. */
    bosun_kemper_tick(&k, 1200);
    assert(!k.state.rig_name_fresh && !k.pending_name[0]);
    assert(name_queries(&w) == 1 && k.name_query_active); /* Autonomous recovery. */
    name(&k, "Crunch", 1210); /* Same text is valid on two different rigs. */
    name(&k, "Crunch", 1250); /* Repeated broadcast retains the request evidence. */
    bosun_kemper_tick(&k, 1710);
    assert(k.state.rig_name_fresh && k.last_name_rig == 9);
    assert(!strcmp(k.state.rig_name, "Crunch"));

    /* A name request in flight cannot authorize an old name in a later rig.
     * Even after settling, the next request waits out that reply window. */
    assert(bosun_kemper_request_rig_name(&k, 2500));
    assert(bosun_kemper_select_rig(&k, 2, 3, 2510));
    bosun_kemper_tick(&k, 2515);
    name(&k, "Crunch", 2600);
    bosun_kemper_tick(&k, 3010);
    assert(!k.state.rig_name_fresh && !k.pending_name[0]);
    assert(!bosun_kemper_request_rig_name(&k, 3699));
    bosun_kemper_tick(&k, 3700);
    assert(k.name_query_active);
    name(&k, "Crunch", 3710);
    bosun_kemper_tick(&k, 4210);
    assert(k.state.rig_name_fresh && k.last_name_rig == 8);
}

static void bootstrap_query_backpressure_and_wrap(void) {
    bosun_kemper k; wire w = {0};
    uint32_t start = UINT32_MAX - 1000;
    bosun_kemper_init(&k, 1, 0, send_packet, &w);
    sense(&k, start);
    pc(&k, 2, start);
    w.fail = true;
    bosun_kemper_tick(&k, start + 500);
    assert(name_queries(&w) == 1 && !k.name_query_active);
    uint32_t failures = k.tx_failures;
    for (uint32_t delta = 501; delta < 1700; ++delta) bosun_kemper_tick(&k, start + delta);
    assert(name_queries(&w) == 1 && k.tx_failures == failures);
    w.fail = false;
    bosun_kemper_tick(&k, start + 1700);
    assert(name_queries(&w) == 2 && k.name_query_active);
    name(&k, "CRUNCH", start + 1701);
    assert(k.last_name_rig == 3 && k.state.rig_name_fresh);
}

static void selected_rig_recovers_missing_broadcast(void) {
    bosun_kemper k; wire w;
    ready(&k, &w);
    assert(bosun_kemper_select_rig(&k, 1, 5, 700));
    bosun_kemper_tick(&k, 705);
    pc(&k, 4, 800);
    bosun_kemper_tick(&k, 1199);
    assert(!name_queries(&w));
    bosun_kemper_tick(&k, 1200);
    assert(name_queries(&w) == 1 && !k.state.rig_name_fresh);
    name(&k, "LEAD", 1210); /* Reply to the automatic request, no broadcast. */
    bosun_kemper_tick(&k, 1710);
    assert(k.state.rig_name_fresh && k.last_name_rig == 5 && !strcmp(k.last_name, "LEAD"));
}

static void bank_snapshot_intermediate_and_final_pc(void) {
    bosun_kemper k; wire w;
    ready(&k, &w);
    uint32_t external = k.state.external_rig_changes;
    assert(bosun_kemper_select_rig(&k, 2, 4, 2000));
    bosun_kemper_tick(&k, 2005);
    bank_header(&k, 2010);
    uint32_t generation = k.generation;
    pc(&k, 7, 2011); /* Captured old slot3 in the newly selected bank2. */
    name(&k, "HEAVY", 2020);
    bosun_kemper_tick(&k, 2500);
    assert(k.state.rig == 9 && k.generation == generation && k.local_pc.valid);
    assert(k.state.external_rig_changes == external && !k.state.rig_name_fresh);
    pc(&k, 8, 2510); /* Final requested rig, still owned by this generation. */
    bosun_kemper_tick(&k, 2560);
    assert(k.state.rig == 9 && k.state.rig_name_fresh && !strcmp(k.last_name, "HEAVY"));
    assert(k.generation == generation && k.state.external_rig_changes == external);

    assert(bosun_kemper_select_rig(&k, 1, 2, 3000));
    bosun_kemper_tick(&k, 3005);
    bank_header(&k, 3010);
    generation = k.generation;
    pc(&k, 3, 3011); /* Return to bank1 first reports previous slot4. */
    name(&k, "CLEAN", 3020);
    pc(&k, 1, 3050);
    bosun_kemper_tick(&k, 3500);
    assert(k.state.rig == 2 && k.state.rig_name_fresh && !strcmp(k.last_name, "CLEAN"));
    assert(k.generation == generation && k.state.external_rig_changes == external);

    assert(bosun_kemper_select_rig(&k, 2, 2, 4000));
    bosun_kemper_tick(&k, 4005);
    bank_header(&k, 4010);
    generation = k.generation;
    pc(&k, 6, 4011); /* Same slot in both banks: two identical PC echoes. */
    name(&k, "Other clean", 4020);
    pc(&k, 6, 4050);
    bosun_kemper_tick(&k, 4500);
    assert(k.state.rig_name_fresh && k.generation == generation);
    assert(k.state.external_rig_changes == external);

    assert(bosun_kemper_select_rig(&k, 1, 2, 5000));
    bosun_kemper_tick(&k, 5005);
    bosun_kemper_tick(&k, 5500);
    generation = k.generation;
    bank_header(&k, 5510); /* Late bank snapshot, initial settle already ended. */
    name(&k, "CLEAN", 5520); /* Name may precede the intermediate PC too. */
    assert(k.generation == generation && k.local_pc.valid);
    pc(&k, 2, 5530);
    pc(&k, 1, 5540);
    bosun_kemper_tick(&k, 5670);
    assert(k.state.rig == 2 && k.state.rig_name_fresh && !strcmp(k.last_name, "CLEAN"));
    assert(k.generation == generation && k.state.external_rig_changes == external);
}

static void bank_snapshot_fallback_is_bounded_and_scoped(void) {
    const uint32_t starts[] = {2000, UINT32_MAX - 30};
    for (unsigned i = 0; i < sizeof(starts) / sizeof(starts[0]); ++i) {
        bosun_kemper k; wire w = {0};
        uint32_t start = starts[i];
        bosun_kemper_init(&k, 1, 0, send_packet, &w);
        assert(bosun_kemper_select_rig(&k, 2, 4, start));
        bosun_kemper_tick(&k, start + 5);
        bank_header(&k, start + 10);
        pc(&k, 7, start + 11);
        name(&k, "Deferred name", start + 20);
        bank_header(&k, start + 900); /* Repeated metadata cannot extend the deadline. */
        bosun_kemper_tick(&k, start + 2509);
        assert(k.state.rig == 9 && !k.state.external_rig_changes && !k.state.rig_name_fresh);
        bosun_kemper_tick(&k, start + 2510);
        assert(k.state.rig == 8 && k.state.external_rig_changes == 1 && !k.state.rig_name_fresh);
        assert(k.bank_snapshot_fallback && k.orphan_pc_count == 1);
        pc(&k, 8, start + 2511); /* Fallback alone cannot quarantine the final target. */
        assert(k.state.rig == 9 && k.state.external_rig_changes == 2);
        assert(!k.bank_snapshot_fallback && !k.orphan_pc_count);
    }

    bosun_kemper k; wire w;
    ready(&k, &w);
    assert(bosun_kemper_select_rig(&k, 2, 4, 2000));
    bosun_kemper_tick(&k, 2005);
    bank_header(&k, 2010);
    uint32_t external = k.state.external_rig_changes;
    pc(&k, 14, 2011); /* Another bank is an immediate physical selection. */
    assert(k.state.rig == 15 && k.state.external_rig_changes == external + 1);
    assert(bosun_kemper_select_rig(&k, 2, 4, 3000));
    bosun_kemper_tick(&k, 3005);
    pc(&k, 7, 3011); /* No bank-header marker: ordinary external PC unchanged. */
    assert(k.state.rig == 8 && k.state.external_rig_changes == external + 2);

    bosun_kemper_init(&k, 1, 0, send_packet, &w);
    assert(bosun_kemper_select_rig(&k, 2, 3, 4000));
    bosun_kemper_tick(&k, 4005);
    assert(bosun_kemper_select_rig(&k, 2, 4, 4100));
    bosun_kemper_tick(&k, 4105);
    bank_header(&k, 4110);
    pc(&k, 7, 4111); /* A known orphan never becomes a physical fallback. */
    assert(!k.deferred_bank_pc && !k.orphan_pc_count);
    bosun_kemper_tick(&k, 6610);
    assert(k.state.rig == 9 && !k.state.external_rig_changes);
}

static void bank_snapshot_late_final_and_supersession(void) {
    const uint32_t delays[] = {1100, 2400};
    for (unsigned i = 0; i < sizeof delays / sizeof *delays; ++i) {
        bosun_kemper k; wire w;
        ready(&k, &w);
        assert(bosun_kemper_select_rig(&k, 2, 4, 2000));
        bosun_kemper_tick(&k, 2005);
        bank_header(&k, 2010);
        uint32_t generation = k.generation, external = k.state.external_rig_changes;
        pc(&k, 5, 2011); /* The ACOUSTIC slot is reported first in bank2. */
        name(&k, "HEAVY", 2020);
        bosun_kemper_tick(&k, 2010 + delays[i] - 1);
        assert(k.state.rig == 9 && k.generation == generation && k.local_pc.valid);
        assert(k.state.external_rig_changes == external && !k.state.rig_name_fresh);
        pc(&k, 8, 2010 + delays[i]);
        bosun_kemper_tick(&k, 2060 + delays[i]);
        assert(k.state.rig == 9 && k.generation == generation && k.state.rig_name_fresh);
        assert(k.state.external_rig_changes == external && !strcmp(k.last_name, "HEAVY"));
    }

    for (unsigned supersession = 0; supersession < 3; ++supersession) {
        bosun_kemper k; wire w;
        ready(&k, &w);
        assert(bosun_kemper_select_rig(&k, 2, 4, 2000));
        bosun_kemper_tick(&k, 2005);
        bank_header(&k, 2010);
        pc(&k, 5, 2011);
        bosun_kemper_tick(&k, 4510);
        assert(k.state.rig == 6 && k.bank_snapshot_fallback && k.orphan_pc_count == 1);
        if (supersession == 0) {
            /* A name arriving after fallback's settle is not a new selection. */
            bosun_kemper_tick(&k, 5010);
            name(&k, "HEAVY", 5020);
            assert(k.bank_snapshot_fallback);
            pc(&k, 8, 5030);
            assert(k.state.rig == 9 && !k.bank_snapshot_fallback);
        } else {
            if (supersession == 1) {
                assert(bosun_kemper_select_rig(&k, 1, 2, 4520));
                bosun_kemper_tick(&k, 4525);
            } else pc(&k, 1, 4520); /* Physical PC in another bank stays immediate. */
            assert(k.state.rig == 2 && !k.bank_snapshot_fallback);
            uint32_t generation = k.generation;
            pc(&k, 8, 4530); /* Old final PC must not override this newer selection. */
            assert(k.state.rig == 2 && k.generation == generation);
        }
    }
}

int main(void) {
    codecs_and_beacon(); tuner_and_defensive_input(); pc_echo_and_rig_names();
    generation_and_live_cc_fences(); crunch_wah_and_discovery();
    wah_timeout_generation_and_slot_fence(); names_bounded_and_pc_wrap();
    boot_name_and_live_cc_deadline();
    message_channel_overrides();
    reinitialize_wah_after_long_uptime();
    bootstrap_identity_and_string_recovery();
    duplicate_names_require_current_string_request();
    bootstrap_query_backpressure_and_wrap();
    selected_rig_recovers_missing_broadcast();
    bank_snapshot_intermediate_and_final_pc();
    bank_snapshot_fallback_is_bounded_and_scoped();
    bank_snapshot_late_final_and_supersession();
    printf("Kemper: codecs, lease, tuner, PC echo, generations, names, 8-slot WAH, retries passed (%zu bytes state)\n", sizeof(bosun_kemper));
    return 0;
}
