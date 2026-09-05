#include "bosun/kemper.h"

#include <string.h>

static const uint8_t effect_cc[8] = {17,18,19,20,22,24,27,29};
static const uint8_t effect_page[8] = {50,51,52,53,56,58,74,75};
static const uint8_t effect_address[8] = {3,3,3,3,3,3,2,2};
static const uint8_t type_page[8] = {50,51,52,53,56,58,60,61};
static const char *const note_names[12] = {
    "C","Db","D","Eb","E","F","Gb","G","Ab","A","Bb","B"
};

static bool due(uint32_t now, uint32_t deadline) {
    return (int32_t)(now - deadline) >= 0;
}

static uint32_t later(uint32_t a, uint32_t b) {
    return (int32_t)(a - b) > 0 ? a : b;
}

static bool transmit(bosun_kemper *k, const uint8_t *data, size_t length) {
    if (k->send && k->send(k->send_context, data, length)) return true;
    ++k->tx_failures;
    return false;
}

static bool voice_channel(bosun_kemper *k, uint8_t channel,
                          uint8_t status, uint8_t a, uint8_t b) {
    uint8_t packet[3];
    size_t n = bosun_midi_encode(packet, sizeof(packet), channel, status, a, b);
    return n && transmit(k, packet, n);
}

static bool voice(bosun_kemper *k, uint8_t status, uint8_t a, uint8_t b) {
    return voice_channel(k, k->channel, status, a, b);
}

static bool request(bosun_kemper *k, uint8_t page, uint8_t address) {
    const uint8_t packet[] = {0xf0,0,0x20,0x33,2,0x7f,0x41,0,page,address,0xf7};
    return transmit(k, packet, sizeof(packet));
}

bool bosun_kemper_query_blocks(bosun_kemper *k, uint8_t mask) {
    if (!k) return false;
    bool success = true;
    for (unsigned i = 0; i < 8; ++i)
        if (mask & (1u << i))
            if (!request(k, effect_page[i], effect_address[i])) success = false;
    return success;
}

bool bosun_kemper_transition_active(const bosun_kemper *k) {
    return k && (k->settle_active || k->reconcile_pending != 0 || k->bank_snapshot_active);
}

const char *bosun_kemper_expression_label(bosun_expression_mode mode) {
    if (mode == BOSUN_EXPRESSION_WAH) return "WAH";
    if (mode == BOSUN_EXPRESSION_VOL) return "VOL";
    return "";
}

static void expression(bosun_kemper *k, bosun_expression_mode mode) {
    if (k->state.expression_mode != mode) {
        k->state.expression_mode = mode;
        ++k->state.revision;
    }
}

static void invalidate_wah(bosun_kemper *k, uint32_t now) {
    k->wah_query_valid = k->wah_pending = false;
    k->wah_attempts = k->wah_types = k->wah_slots = k->wah_states = k->wah_on = 0;
    k->wah_fixed = -1;
    k->wah_cursor = 0;
    /* An absent deadline is not timestamp zero: after 2^31 ms, modular
     * comparison would mistake that zero for a future quarantine deadline. */
    if (k->wah_retire_active && due(now, k->wah_retire_ms)) k->wah_retire_active = false;
    k->wah_next_ms = k->wah_retire_active ? k->wah_retire_ms : now;
    expression(k, BOSUN_EXPRESSION_UNKNOWN);
}

static void publish_wah(bosun_kemper *k) {
    bool active = (k->wah_on & k->wah_slots & k->wah_states & k->wah_types) != 0;
    bool known = k->wah_fixed == 0 && k->wah_types == 255 &&
        (k->wah_states & k->wah_slots) == k->wah_slots;
    expression(k, k->wah_fixed == 1 || active ? BOSUN_EXPRESSION_WAH :
        known ? BOSUN_EXPRESSION_VOL : BOSUN_EXPRESSION_UNKNOWN);
}

static bool wah_type(uint16_t value) {
    return value == 1 || value == 2 || value == 3 || value == 4 ||
        value == 6 || value == 7 || value == 8 || value == 9 ||
        value == 10 || value == 12;
}

static void receive_wah(bosun_kemper *k, uint16_t value, uint8_t target,
                        bool live, uint32_t now) {
    if ((target < 16 && value > 1) || bosun_kemper_transition_active(k) ||
        !k->wah_query_valid || k->wah_query_generation != k->generation) return;
    if (target == 8) k->wah_fixed = (int8_t)value;
    else {
        uint8_t bit = (uint8_t)(1u << (target >= 16 ? target - 16 : target));
        if (target >= 16) {
            k->wah_types |= bit;
            if (wah_type(value)) {
                if (!(k->wah_slots & bit)) k->wah_states &= (uint8_t)~bit;
                k->wah_slots |= bit;
            } else {
                k->wah_slots &= (uint8_t)~bit;
                k->wah_states &= (uint8_t)~bit;
            }
        } else {
            k->wah_states |= bit;
            if (value) k->wah_on |= bit;
            else k->wah_on &= (uint8_t)~bit;
        }
    }
    publish_wah(k);
    if (!live && target == k->wah_target) {
        k->wah_pending = false;
        k->wah_attempts = 0;
        if (target < 16) k->wah_cursor = target == 8 ? 0 : target + 1;
        k->wah_next_ms = now + (target == 8 && k->wah_types == 255 ? 500u : 20u);
    }
}

static void query_wah(bosun_kemper *k, uint32_t now) {
    if (!k->state.connected || bosun_kemper_transition_active(k) ||
        !k->state.rig_name_fresh || k->last_name_rig != k->state.rig) return;
    if (k->wah_pending) {
        if (!due(now, k->wah_retire_ms)) return;
        k->wah_fixed = -1;
        k->wah_states = 0;
        publish_wah(k);
        k->wah_pending = false;
        if (k->wah_attempts >= 3) {
            k->wah_attempts = 0;
            k->wah_next_ms = now + 5000;
        }
    }
    if (!due(now, k->wah_next_ms)) return;
    uint8_t target = k->wah_attempts ? k->wah_target : 8;
    if (!k->wah_attempts && k->wah_fixed >= 0) {
        bool missing = false;
        for (unsigned i = 0; i < 8; ++i) {
            uint8_t bit = (uint8_t)(1u << i);
            if (!(k->wah_types & bit)) { target = (uint8_t)(i + 16); missing = true; break; }
            if ((k->wah_slots & bit) && !(k->wah_states & bit)) {
                target = (uint8_t)i; missing = true; break;
            }
        }
        if (!missing) {
            for (unsigned i = k->wah_cursor; i < 8; ++i)
                if (k->wah_slots & (1u << i)) { target = (uint8_t)i; break; }
        }
    }
    uint8_t page = target == 8 ? 5 :
        target >= 16 ? type_page[target - 16] : effect_page[target];
    uint8_t address = target == 8 ? 21 : target >= 16 ? 0 : effect_address[target];
    k->wah_query_generation = k->generation;
    k->wah_query_valid = k->wah_pending = true;
    k->wah_target = target;
    ++k->wah_attempts;
    k->wah_retire_ms = now + 1200;
    k->wah_retire_active = true;
    if (target < 8) {
        if (due(now, k->wah_slots_retire_ms)) k->wah_queried_slots = 0;
        k->wah_queried_slots |= (uint8_t)(1u << target);
        k->wah_slots_retire_ms = k->wah_retire_ms;
    }
    (void)request(k, page, address);
}

static void expire_orphans(bosun_kemper *k, uint32_t now) {
    if (k->name_query_retire_active && due(now, k->name_query_retire_ms))
        k->name_query_retire_active = k->name_query_active = false;
    uint8_t count = 0;
    for (unsigned i = 0; i < k->orphan_pc_count; ++i)
        if (!due(now, k->orphan_pc[i].expires_ms)) k->orphan_pc[count++] = k->orphan_pc[i];
    k->orphan_pc_count = count;
    if (k->local_pc.valid && due(now, k->local_pc.expires_ms)) k->local_pc.valid = false;
    if (k->orphan_blocks && due(now, k->orphan_until_ms)) k->orphan_blocks = 0;
}

static void retire_pc(bosun_kemper *k, uint32_t now) {
    expire_orphans(k, now);
    if (k->local_pc.valid) {
        if (k->orphan_pc_count == BOSUN_KEMPER_PC_ORPHANS) {
            memmove(k->orphan_pc, k->orphan_pc + 1,
                (BOSUN_KEMPER_PC_ORPHANS - 1) * sizeof(k->orphan_pc[0]));
            --k->orphan_pc_count;
        }
        k->orphan_pc[k->orphan_pc_count++] = k->local_pc;
    }
    k->local_pc.valid = false;
}

static void quarantine(bosun_kemper *k, uint32_t now) {
    expire_orphans(k, now);
    if (k->reconcile_queried && !due(now, k->query_retire_ms)) {
        k->orphan_until_ms = k->orphan_blocks ?
            later(k->orphan_until_ms, k->query_retire_ms) : k->query_retire_ms;
        k->orphan_blocks |= k->reconcile_queried;
    }
    if (k->wah_queried_slots && !due(now, k->wah_slots_retire_ms)) {
        k->orphan_until_ms = k->orphan_blocks ?
            later(k->orphan_until_ms, k->wah_slots_retire_ms) : k->wah_slots_retire_ms;
        k->orphan_blocks |= k->wah_queried_slots;
    }
}

static void reset_reconcile(bosun_kemper *k, uint32_t now, uint32_t delay) {
    k->settle_active = true;
    k->settle_until_ms = now + delay;
    k->reconcile_pending = k->reconcile_attempt = k->reconcile_queried = 0;
    memset(k->guard_budget, 0, sizeof(k->guard_budget));
    invalidate_wah(k, now);
}

static void arm(bosun_kemper *k, uint8_t rig, uint32_t now) {
    quarantine(k, now);
    retire_pc(k, now);
    ++k->generation;
    if (k->generation > 0x3fffffffu) k->generation = 1;
    k->state.rig = rig;
    k->state.bank = (uint8_t)((rig - 1) / 5 + 1);
    k->state.rig_in_bank = (uint8_t)((rig - 1) % 5 + 1);
    k->state.rig_name[0] = 0;
    k->state.rig_name_fresh = false;
    k->bootstrap_name_pending = true;
    k->state.effect_known = 0;
    k->pending_name[0] = 0;
    k->pending_name_requested = k->name_query_active = false;
    k->bank_snapshot_active = k->bank_snapshot_seen = false;
    k->deferred_bank_pc = 0;
    k->scheduled_pc = false;
    reset_reconcile(k, now, 500);
    ++k->state.revision;
}

static void expire_bank_snapshot(bosun_kemper *k, uint32_t now) {
    if (!k->bank_snapshot_active || !due(now, k->bank_snapshot_deadline_ms)) return;
    uint8_t rig = k->deferred_bank_pc;
    k->bank_snapshot_active = false;
    k->deferred_bank_pc = 0;
    if (rig) {
        /* A physical selection may have superseded the local request. Do
         * not suppress it indefinitely if the expected final echo is lost. */
        arm(k, rig, now);
        ++k->state.external_rig_changes;
    }
}

bool bosun_kemper_begin_rig(bosun_kemper *k, uint8_t rig, uint32_t now) {
    if (!k || rig < 1 || rig > 125) return false;
    arm(k, rig, now);
    return true;
}

void bosun_kemper_init(bosun_kemper *k, uint8_t channel,
    uint8_t bound_blocks, bosun_midi_send_fn send, void *context) {
    if (!k) return;
    memset(k, 0, sizeof(*k));
    k->channel = channel >= 1 && channel <= 16 ? channel : 1;
    k->bound_blocks = bound_blocks;
    k->send = send;
    k->send_context = context;
    k->state.rig = k->state.bank = k->state.rig_in_bank = 1;
    k->state.tuner_deviance = 8192;
    k->wah_fixed = -1;
    k->bootstrap_name_pending = true;
}

void bosun_kemper_set_bound_blocks(bosun_kemper *k, uint8_t mask) {
    if (k) k->bound_blocks = mask;
}

bool bosun_kemper_select_rig(bosun_kemper *k, uint8_t bank, uint8_t slot, uint32_t now) {
    return k && bosun_kemper_select_rig_channel(k, k->channel, bank, slot, now);
}

bool bosun_kemper_select_rig_channel(bosun_kemper *k, uint8_t channel,
                                    uint8_t bank, uint8_t slot, uint32_t now) {
    if (!k || channel < 1 || channel > 16 || bank < 1 || bank > 25 || slot < 1 || slot > 5)
        return false;
    if (!voice_channel(k, channel, 0xb0, 0, 0) || !voice_channel(k, channel, 0xb0, 32, 0))
        return false;
    uint8_t rig = (uint8_t)((bank - 1) * 5 + slot);
    arm(k, rig, now);
    k->scheduled_pc = true;
    k->scheduled_pc_rig = rig;
    k->scheduled_pc_channel = channel;
    k->scheduled_pc_ms = now + 5;
    return true;
}

bool bosun_kemper_request_rig_name(bosun_kemper *k, uint32_t now) {
    if (!k || !k->rig_identity_known || bosun_kemper_transition_active(k) ||
        k->pending_name[0] ||
        (k->name_query_retire_active && !due(now, k->name_query_retire_ms))) return false;
    /* Kemper MIDI Parameter Documentation, Request String Parameter: 0x43
     * returns function 0x03; numeric 0x41 at the same address returns no name.
     * Keep untagged replies from a previous generation out of a new request. */
    const uint8_t packet[] = {0xf0,0,0x20,0x33,2,0x7f,0x43,0,0,1,0xf7};
    k->name_query_generation = k->generation;
    k->name_query_retire_ms = now + 1200;
    k->name_query_retire_active = true;
    k->name_query_active = transmit(k, packet, sizeof(packet));
    return k->name_query_active;
}

static void publish_block(bosun_kemper *k, unsigned i, bool on) {
    uint8_t bit = (uint8_t)(1u << i);
    if (!(k->state.effect_known & bit) || k->state.effects[i] != on) {
        k->state.effects[i] = on;
        k->state.effect_known |= bit;
        ++k->state.revision;
    }
}

static void cache_block(bosun_kemper *k, unsigned i, bool on) {
    uint8_t bit = (uint8_t)(1u << i);
    k->cache_known |= bit;
    if (on) k->cache_on |= bit;
    else k->cache_on &= (uint8_t)~bit;
    k->block_generation[i] = k->generation;
}

static void accept_name(bosun_kemper *k, const char name[BOSUN_KEMPER_NAME_CAPACITY]) {
    if (!k->state.rig_name_fresh || strcmp(k->state.rig_name, name)) {
        memcpy(k->state.rig_name, name, sizeof(k->state.rig_name));
        k->state.rig_name_fresh = true;
        ++k->state.revision;
    }
    memcpy(k->last_name, name, sizeof(k->last_name));
    k->last_name_rig = k->state.rig;
    k->bootstrap_name_pending = false;
}

static void commit_name(bosun_kemper *k, uint32_t now) {
    if (!k->pending_name[0] || bosun_kemper_transition_active(k) ||
        k->pending_name_generation != k->generation || now - k->pending_name_ms < 150) return;
    if (!k->pending_name_requested && k->last_name_rig && k->last_name_rig != k->state.rig &&
        strcmp(k->pending_name, k->last_name) == 0) {
        k->pending_name[0] = 0;
        return;
    }
    accept_name(k, k->pending_name);
    k->pending_name[0] = 0;
}

static void receive_name(bosun_kemper *k, const uint8_t *data, size_t length,
                         uint32_t now) {
    /* The boot configuration is a local fallback, not evidence of the rig
     * currently loaded by the Kemper. Its initial name can precede its PC. */
    if (!k->rig_identity_known) return;
    char name[BOSUN_KEMPER_NAME_CAPACITY] = {0};
    size_t used = 0;
    for (size_t i = 0; i < length && data[i] && used < sizeof(name) - 1; ++i)
        if (data[i] >= 0x20 && data[i] < 0x7f) name[used++] = (char)data[i];
    while (used && name[used - 1] == ' ') name[--used] = 0;
    size_t start = 0;
    while (start < used && name[start] == ' ') ++start;
    if (start) { memmove(name, name + start, used - start + 1); used -= start; }
    if (!used) return;
    bool requested = k->name_query_active && k->name_query_generation == k->generation &&
        !due(now, k->name_query_retire_ms);
    k->name_query_active = false;
    if (!bosun_kemper_transition_active(k) && !k->pending_name[0]) {
        bool initial = !k->last_name[0] && !k->last_name_rig;
        if ((k->last_name[0] && strcmp(name, k->last_name)) ||
            (k->last_name_rig && k->last_name_rig != k->state.rig) ||
            initial) {
            arm(k, k->state.rig, now);
            /* Once its coordinates are established, the first name has no
             * previous rig identity to quarantine while effects settle. */
            if (initial) { accept_name(k, name); return; }
        }
        else {
            accept_name(k, name);
            return;
        }
    }
    /* An identical broadcast during settling cannot revoke the evidence
     * from the current request; a different value still replaces it. */
    requested = requested || (k->pending_name_requested &&
        k->pending_name_generation == k->generation && !strcmp(k->pending_name, name));
    memcpy(k->pending_name, name, sizeof(name));
    k->pending_name_ms = now;
    k->pending_name_generation = k->generation;
    k->pending_name_requested = requested;
}

static void tuner(bosun_kemper *k, bool active) {
    if (k->state.tuner_active != active) {
        k->state.tuner_active = active;
        ++k->state.revision;
    }
}

static void receive_sysex(bosun_kemper *k, const uint8_t *data, size_t length,
                          uint32_t now) {
    if (length < 6 || data[0] != 0 || data[1] != 0x20 || data[2] != 0x33) return;
    for (size_t i = 0; i < length; ++i) if (data[i] >= 0x80) return;
    uint8_t fn = data[5];
    if (fn == 0x7e) {
        if (!k->state.connected) { k->state.connected = true; ++k->state.revision; }
        k->last_sensed_ms = now;
        return;
    }
    if (fn == 3 && length >= 9) {
        if (data[7] == 0 && data[8] == 1) receive_name(k, data + 9, length - 9, now);
        return;
    }
    if (fn == 7 && length > 12 && data[7] == 0 && data[8] == 0 &&
        data[9] == 1 && data[10] == 0 && data[11] == 0 && data[12] &&
        !k->bank_snapshot_seen && k->local_pc.valid &&
        k->local_pc.generation == k->generation && k->local_pc.rig == k->state.rig &&
        !due(now, k->local_pc.expires_ms)) {
        /* Player bank header, extended string address 0x00010000. Real bank
         * changes report the old slot in the new bank before the final PC.
         * Only this observed bank snapshot opens a bounded echo window. */
        k->bank_snapshot_active = k->bank_snapshot_seen = true;
        k->bank_snapshot_deadline_ms = now + 1000;
        return;
    }
    if (fn != 1 || length < 11) return;
    uint8_t page = data[7], address = data[8];
    uint16_t value = (uint16_t)((data[9] << 7) | data[10]);
    if (page == 5 && address == 21) { receive_wah(k, value, 8, false, now); return; }
    if (address == 0) {
        for (unsigned i = 0; i < 8; ++i)
            if (page == type_page[i]) { receive_wah(k, value, (uint8_t)(i + 16), false, now); return; }
    }
    if (page == 4 && address == 0) {
        /* Python round uses ties-to-even, including raw .5 BPM values. */
        uint16_t bpm = value / 64;
        if (value % 64 > 32 || (value % 64 == 32 && (bpm & 1))) ++bpm;
        if (k->state.bpm != bpm) { k->state.bpm = bpm; ++k->state.revision; }
        return;
    }
    if (page == 127 && address == 126) { tuner(k, value == 1); return; }
    if (page == 125 && address == 84) {
        if (k->state.tuner_active && strcmp(k->state.tuner_note, note_names[value % 12])) {
            strcpy(k->state.tuner_note, note_names[value % 12]); ++k->state.revision;
        }
        return;
    }
    if (page == 124 && address == 15) {
        if (k->state.tuner_active && k->state.tuner_deviance != value) {
            k->state.tuner_deviance = value; ++k->state.revision;
        }
        return;
    }
    for (unsigned i = 0; i < 8; ++i) {
        if (page != effect_page[i] || address != effect_address[i]) continue;
        uint8_t bit = (uint8_t)(1u << i);
        bool on = value != 0;
        expire_orphans(k, now);
        if (k->orphan_blocks & bit) return;
        if (k->guard_budget[i]) {
            if (!due(now, k->guard_until_ms[i])) {
                --k->guard_budget[i];
                bool expected = (k->guard_on & bit) != 0;
                if (on != expected) { receive_wah(k, expected, (uint8_t)i, false, now); return; }
            } else k->guard_budget[i] = 0;
        }
        cache_block(k, i, on);
        bool pending = (k->reconcile_pending & bit) && k->reconcile_attempt > 0;
        if (!k->settle_active && (pending || !bosun_kemper_transition_active(k))) {
            publish_block(k, i, on);
            receive_wah(k, value, (uint8_t)i, false, now);
        }
        if (pending) {
            k->reconcile_pending &= (uint8_t)~bit;
            if (!k->reconcile_pending) commit_name(k, now);
        }
        return;
    }
}

static void receive_cc(bosun_kemper *k, uint8_t cc, uint8_t value, uint32_t now) {
    if (cc == 31) { tuner(k, value >= 64); return; }
    for (unsigned i = 0; i < 8; ++i) {
        if (cc != effect_cc[i]) continue;
        uint8_t bit = (uint8_t)(1u << i);
        bool on = value >= 64;
        bool queried = (k->reconcile_queried & bit) && !due(now, k->query_retire_ms);
        bool wah_pending = k->wah_pending && k->wah_target == i;
        cache_block(k, i, on);
        /* MIDI input is drained before tick; a live CC at the deadline is
         * already current even if tick has not cleared settle_active yet. */
        if (!k->settle_active || due(now, k->settle_until_ms)) publish_block(k, i, on);
        k->reconcile_pending &= (uint8_t)~bit;
        if (!k->reconcile_pending) commit_name(k, now);
        if (queried || wah_pending) {
            k->guard_until_ms[i] = now + 1200;
            k->guard_budget[i] = (uint8_t)((queried ?
                (k->reconcile_attempt ? k->reconcile_attempt : 1) : 0) + (wah_pending ? 1 : 0));
            if (on) k->guard_on |= bit;
            else k->guard_on &= (uint8_t)~bit;
        }
        receive_wah(k, on, (uint8_t)i, true, now);
        return;
    }
}

static void receive_pc(bosun_kemper *k, uint8_t pc, uint32_t now) {
    if (pc >= 125) return;
    uint8_t rig = pc + 1;
    expire_bank_snapshot(k, now);
    expire_orphans(k, now);
    if ((k->local_pc.valid || (k->bank_snapshot_seen && !due(now, k->bank_snapshot_deadline_ms))) &&
        k->local_pc.generation == k->generation &&
        k->local_pc.rig == rig && k->state.rig == rig) {
        k->local_pc.valid = false;
        k->bank_snapshot_active = false;
        k->deferred_bank_pc = 0;
        if (k->settle_active) k->settle_until_ms = later(k->settle_until_ms, now + 50);
        else if (!(k->reconcile_pending && k->reconcile_attempt) &&
            (k->reconcile_pending || (k->reconcile_queried && !due(now, k->query_retire_ms)))) {
            quarantine(k, now);
            reset_reconcile(k, now, 50);
        }
        return;
    }
    for (unsigned i = 0; i < k->orphan_pc_count; ++i) {
        if (k->orphan_pc[i].rig != rig) continue;
        memmove(k->orphan_pc + i, k->orphan_pc + i + 1,
            (k->orphan_pc_count - i - 1) * sizeof(k->orphan_pc[0]));
        --k->orphan_pc_count;
        return;
    }
    if (k->bank_snapshot_active && k->local_pc.valid &&
        k->local_pc.generation == k->generation && (rig - 1) / 5 == (k->local_pc.rig - 1) / 5) {
        k->deferred_bank_pc = rig;
        return;
    }
    k->rig_identity_known = true;
    arm(k, rig, now);
    ++k->state.external_rig_changes;
}

void bosun_kemper_handle(bosun_kemper *k, uint8_t channel, uint8_t status,
                         const uint8_t *data, size_t length, uint32_t now) {
    if (!k || (!data && length)) return;
    if (status == 0xf0) { receive_sysex(k, data, length, now); return; }
    if (channel != k->channel) return;
    if (status == 0xb0 && length >= 2 && data[0] < 128 && data[1] < 128)
        receive_cc(k, data[0], data[1], now);
    else if (status == 0xc0 && length >= 1) receive_pc(k, data[0], now);
}

static void reconcile_round(bosun_kemper *k, uint32_t now) {
    if (!k->reconcile_pending || k->orphan_blocks || k->bank_snapshot_active) return;
    ++k->reconcile_attempt;
    k->reconcile_queried |= k->reconcile_pending;
    k->query_retire_ms = now + 1200;
    k->reconcile_deadline_ms = now + 400;
    (void)bosun_kemper_query_blocks(k, k->reconcile_pending);
}

void bosun_kemper_tick(bosun_kemper *k, uint32_t now) {
    if (!k) return;
    expire_bank_snapshot(k, now);
    expire_orphans(k, now);
    if (k->scheduled_pc && due(now, k->scheduled_pc_ms)) {
        k->scheduled_pc = false;
        if (voice_channel(k, k->scheduled_pc_channel, 0xc0, (uint8_t)(k->scheduled_pc_rig - 1), 0)) {
            k->rig_identity_known = true;
            retire_pc(k, now);
            k->local_pc = (bosun_kemper_pc_token){k->generation, now + 10000,
                k->scheduled_pc_rig, true};
        }
    }
    for (unsigned i = 0; i < 8; ++i)
        if (k->guard_budget[i] && due(now, k->guard_until_ms[i])) k->guard_budget[i] = 0;
    if (k->reconcile_queried && due(now, k->query_retire_ms)) {
        k->reconcile_queried = 0;
        if (!k->reconcile_pending) k->reconcile_attempt = 0;
    }
    if (k->settle_active && due(now, k->settle_until_ms)) {
        k->settle_active = false;
        k->reconcile_pending = k->bound_blocks;
        k->reconcile_attempt = k->reconcile_queried = 0;
        reconcile_round(k, now);
    }
    if (k->reconcile_pending && !k->settle_active) {
        if (!k->reconcile_attempt) reconcile_round(k, now);
        else if (due(now, k->reconcile_deadline_ms)) {
            if (k->reconcile_attempt < 2) reconcile_round(k, now);
            else {
                uint8_t missing = k->reconcile_pending;
                k->reconcile_pending = 0;
                for (unsigned i = 0; i < 8; ++i)
                    if ((missing & k->cache_known & (1u << i)) &&
                        k->block_generation[i] == k->generation)
                        publish_block(k, i, (k->cache_on & (1u << i)) != 0);
            }
        }
    }
    commit_name(k, now);
    if (k->state.connected && now - k->last_sensed_ms > 15000) {
        k->state.connected = false;
        k->init_sent = false;
        ++k->state.revision;
        invalidate_wah(k, now);
    }
    query_wah(k, now);
    if (k->state.connected && k->bootstrap_name_pending)
        (void)bosun_kemper_request_rig_name(k, now);
    /* A sensing reply confirms the lease, but does not identify the current
     * rig. Retry the initial snapshot at most once per second until its PC. */
    bool initialize = !k->init_sent || !k->state.connected || !k->rig_identity_known;
    if (k->init_sent && now - k->last_beacon_ms < (initialize ? 1000u : 5000u)) return;
    const uint8_t packet[] = {0xf0,0,0x20,0x33,2,0x7f,0x7e,0,0x40,2,
        initialize ? 0x23 : 0x22,5,0xf7};
    (void)transmit(k, packet, sizeof(packet));
    k->last_beacon_ms = now;
    k->init_sent = true;
}

bool bosun_kemper_command(bosun_kemper *k, bosun_kemper_command_type command,
                          uint8_t index, int value) {
    if (!k) return false;
    uint8_t boolean = value ? 127 : 0;
    switch (command) {
    case BOSUN_KEMPER_EFFECT:
        return index < 8 && voice(k, 0xb0, effect_cc[index], boolean);
    case BOSUN_KEMPER_FIXED: {
        static const uint8_t addresses[] = {11,6,16,21,1};
        return index < 5 && voice(k, 0xb0, 99, 5) &&
            voice(k, 0xb0, 98, addresses[index]) && voice(k, 0xb0, 6, 0) &&
            voice(k, 0xb0, 38, value ? 1 : 0);
    }
    case BOSUN_KEMPER_TUNER: return voice(k, 0xb0, 31, boolean);
    case BOSUN_KEMPER_TAP: return voice(k, 0xb0, 30, 127);
    case BOSUN_KEMPER_TEMPO:
        if (value < 40) value = 40;
        if (value > 250) value = 250;
        return voice(k, 0xb0, 92, (uint8_t)(value / 128)) &&
            voice(k, 0xb0, 93, (uint8_t)(value % 128));
    case BOSUN_KEMPER_MORPH: return voice(k, 0xb0, 4, (uint8_t)value);
    case BOSUN_KEMPER_MORPH_TRIGGER: return voice(k, 0xb0, 80, boolean);
    case BOSUN_KEMPER_WAH: return voice(k, 0xb0, 1, (uint8_t)value);
    case BOSUN_KEMPER_VOLUME: return voice(k, 0xb0, 7, (uint8_t)value);
    case BOSUN_KEMPER_LOOPER: {
        static const uint8_t cc[] = {88,89,91,93,94};
        return index < 5 && voice(k, 0xb0, cc[index], 127);
    }
    case BOSUN_KEMPER_ROTARY: return voice(k, 0xb0, 47, boolean);
    case BOSUN_KEMPER_STEP: return voice(k, 0xb0, value < 0 ? 49 : 48, 0);
    }
    return false;
}

bool bosun_kemper_command_channel(bosun_kemper *k, uint8_t channel,
                                  bosun_kemper_command_type command,
                                  uint8_t index, int value) {
    if (!k || channel < 1 || channel > 16) return false;
    uint8_t saved = k->channel;
    k->channel = channel;
    bool success = bosun_kemper_command(k, command, index, value);
    k->channel = saved;
    return success;
}
