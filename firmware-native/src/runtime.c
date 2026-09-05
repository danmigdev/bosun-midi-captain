#include "bosun/runtime.h"

#include <math.h>
#include <string.h>

_Static_assert(sizeof(bosun_runtime_t) <= 8192, "runtime exceeds its 8 KiB storage budget");

static const char *const switch_names[10] = {"1","2","3","4","up","A","B","C","D","down"};
static const char *const block_names[8] = {"A","B","C","D","X","Mod","Delay","Reverb"};
static const char *const action_names[6] = {"press","release","toggle_on","toggle_off","long_press","double_tap"};
static const char *const mode_names[5] = {"tap","latched","momentary","long_press_alt","double_tap"};
static const char *const supported[] = {
    "cc","pc","note_on","note_off","delay","program_change_bank","cc_toggle",
    "captain_patch","captain_bank_step","captain_preview_step","captain_preview_commit",
    "captain_preview_cancel","captain_setlist_step","kemper_rig","kemper_step_rig",
    "kemper_effect_toggle","kemper_fixed_toggle","kemper_tuner","kemper_tap_tempo",
    "kemper_set_tempo","kemper_morph","kemper_morph_trigger","kemper_wah","kemper_volume",
    "kemper_looper","kemper_rotary","kemper_query_state"
};

static bool due(uint32_t now, uint32_t deadline) { return (int32_t)(now - deadline) >= 0; }
static int field(const bosun_json_doc_t *d, int o, const char *key) { return bosun_json_get(d, o, key); }
static bool is_type(const bosun_json_doc_t *d, int t, bosun_json_type_t type) {
    return d && t >= 0 && t < d->count && d->tokens[t].type == type;
}
static bool number(const bosun_json_doc_t *d, int o, const char *key,
                   int32_t fallback, int32_t lo, int32_t hi, int32_t *out) {
    int token = field(d, o, key);
    *out = fallback;
    return (token < 0 || bosun_json_integer(d, token, out)) && *out >= lo && *out <= hi;
}
static int enumeration(const bosun_json_doc_t *d, int o, const char *key,
                       const char *const *values, unsigned count, int fallback) {
    int token = field(d, o, key);
    if (token < 0) return fallback;
    for (unsigned i = 0; i < count; ++i)
        if (bosun_json_equal(d, token, values[i])) return (int)i;
    return -1;
}
static int on_off(const bosun_json_doc_t *d, int o, const char *key, int fallback) {
    static const char *const values[] = {"off", "on"};
    return enumeration(d, o, key, values, 2, fallback);
}

bool bosun_runtime_supported(const char *type) {
    if (!type) return false;
    for (unsigned i = 0; i < sizeof(supported) / sizeof(supported[0]); ++i)
        if (!strcmp(type, supported[i])) return true;
    return false;
}

static bool decode(bosun_runtime_t *rt, const bosun_json_doc_t *d, int token,
                    bosun_runtime_command_t *c) {
    char type[48];
    int32_t a = 0, b = 0, value = 0, channel = 1;
    memset(c, 0, sizeof(*c));
    if (!is_type(d, token, BOSUN_JSON_OBJECT) ||
        !bosun_json_string(d, field(d, token, "type"), type, sizeof(type))) goto invalid;
    if (!bosun_runtime_supported(type)) { ++rt->unsupported_messages; return false; }
    if (!strncmp(type, "kemper_", 7) && !rt->kemper_enabled) { ++rt->unsupported_messages; return false; }
    if (!number(d, token, "channel", 1, 1, 16, &channel)) goto invalid;
    c->channel = (uint8_t)channel;
    if (!strcmp(type, "cc") || !strcmp(type, "cc_toggle")) {
        if (!number(d, token, "cc", 0, 0, 127, &a)) goto invalid;
        if (!strcmp(type, "cc_toggle")) {
            int on = on_off(d, token, "state", 1);
            if (on < 0 || !number(d, token, on ? "on_value" : "off_value", on ? 127 : 0, 0, 127, &value)) goto invalid;
        } else if (!number(d, token, "value", 0, 0, 127, &value)) goto invalid;
        c->type = BOSUN_COMMAND_CC;
    } else if (!strcmp(type, "pc")) {
        c->type = BOSUN_COMMAND_PC;
        if (!number(d, token, "program", 0, 0, 127, &a)) goto invalid;
    } else if (!strcmp(type, "note_on") || !strcmp(type, "note_off")) {
        c->type = !strcmp(type, "note_on") ? BOSUN_COMMAND_NOTE_ON : BOSUN_COMMAND_NOTE_OFF;
        if (!number(d, token, "note", 0, 0, 127, &a) ||
            !number(d, token, "velocity", c->type == BOSUN_COMMAND_NOTE_ON ? 100 : 64, 0, 127, &value)) goto invalid;
    } else if (!strcmp(type, "delay")) {
        c->type = BOSUN_COMMAND_DELAY;
        if (!number(d, token, "ms", 0, 0, 60000, &value)) goto invalid;
    } else if (!strcmp(type, "program_change_bank")) {
        c->type = BOSUN_COMMAND_BANK_PC;
        if (!number(d, token, "msb", 0, 0, 127, &a) ||
            !number(d, token, "lsb", 0, 0, 127, &b) ||
            !number(d, token, "program", 0, 0, 127, &value)) goto invalid;
    } else if (!strcmp(type, "captain_patch") || !strcmp(type, "kemper_rig")) {
        c->type = !strcmp(type, "captain_patch") ? BOSUN_COMMAND_PATCH : BOSUN_COMMAND_KEMPER_RIG;
        if (!number(d, token, "bank", 1, 1, c->type == BOSUN_COMMAND_PATCH ? 99 : 25, &a) ||
            !number(d, token, c->type == BOSUN_COMMAND_PATCH ? "slot" : "rig", 1,
                    1, c->type == BOSUN_COMMAND_PATCH ? 10 : 5, &b)) goto invalid;
    } else if (!strcmp(type, "captain_bank_step") || !strcmp(type, "captain_preview_step") ||
               !strcmp(type, "captain_setlist_step")) {
        c->type = !strcmp(type, "captain_bank_step") ? BOSUN_COMMAND_BANK_STEP :
            !strcmp(type, "captain_preview_step") ? BOSUN_COMMAND_PREVIEW_STEP : BOSUN_COMMAND_SETLIST_STEP;
        if (!number(d, token, "delta", 1, -32768, 32767, &value)) goto invalid;
        static const char *const scopes[] = {"patch", "bank"};
        int scope = enumeration(d, token, "scope", scopes, 2, 0);
        if (scope < 0) goto invalid;
        c->flags = (uint8_t)scope;
    } else if (!strcmp(type, "captain_preview_commit")) c->type = BOSUN_COMMAND_PREVIEW_COMMIT;
    else if (!strcmp(type, "captain_preview_cancel")) c->type = BOSUN_COMMAND_PREVIEW_CANCEL;
    else if (!strcmp(type, "kemper_query_state")) c->type = BOSUN_COMMAND_KEMPER_QUERY;
    else {
        c->type = BOSUN_COMMAND_KEMPER;
        if (!strcmp(type, "kemper_effect_toggle")) {
            c->index = BOSUN_KEMPER_EFFECT;
            a = enumeration(d, token, "slot", block_names, 8, 0);
            value = on_off(d, token, "value", 1);
            if (a < 0 || value < 0) goto invalid;
        } else if (!strcmp(type, "kemper_fixed_toggle")) {
            static const char *const effects[] = {"Compressor","Noise Gate","Pure Booster","Wah","Transpose"};
            c->index = BOSUN_KEMPER_FIXED;
            a = enumeration(d, token, "effect", effects, 5, 0);
            value = on_off(d, token, "value", 1);
            if (a < 0 || value < 0) goto invalid;
        } else if (!strcmp(type, "kemper_looper")) {
            static const char *const actions[] = {"rec_play","stop_erase","trigger","reverse","half_speed"};
            c->index = BOSUN_KEMPER_LOOPER;
            a = enumeration(d, token, "action", actions, 5, 0);
            if (a < 0) goto invalid;
        } else if (!strcmp(type, "kemper_step_rig")) {
            static const char *const directions[] = {"prev","next"};
            c->index = BOSUN_KEMPER_STEP;
            int direction = enumeration(d, token, "direction", directions, 2, 1);
            if (direction < 0) goto invalid;
            value = direction ? 1 : -1;
        } else if (!strcmp(type, "kemper_tap_tempo")) c->index = BOSUN_KEMPER_TAP;
        else if (!strcmp(type, "kemper_set_tempo")) {
            c->index = BOSUN_KEMPER_TEMPO;
            if (!number(d, token, "bpm", 120, 40, 250, &value)) goto invalid;
        } else if (!strcmp(type, "kemper_tuner") || !strcmp(type, "kemper_morph_trigger")) {
            c->index = !strcmp(type, "kemper_tuner") ? BOSUN_KEMPER_TUNER : BOSUN_KEMPER_MORPH_TRIGGER;
            value = on_off(d, token, "state", 1);
            if (value < 0) goto invalid;
        } else if (!strcmp(type, "kemper_rotary")) {
            static const char *const speeds[] = {"slow","fast"};
            c->index = BOSUN_KEMPER_ROTARY;
            value = enumeration(d, token, "value", speeds, 2, 0);
            if (value < 0) goto invalid;
        } else {
            c->index = !strcmp(type, "kemper_wah") ? BOSUN_KEMPER_WAH :
                !strcmp(type, "kemper_volume") ? BOSUN_KEMPER_VOLUME : BOSUN_KEMPER_MORPH;
            if (!number(d, token, "value", c->index == BOSUN_KEMPER_VOLUME ? 100 : 64, 0, 127, &value)) goto invalid;
        }
    }
    c->first = (uint16_t)a; c->second = (uint16_t)b; c->value = value;
    return true;
invalid:
    ++rt->invalid_messages;
    return false;
}

static bool collect(bosun_runtime_t *rt, const bosun_json_doc_t *doc, int array,
                    bosun_runtime_command_t *commands, unsigned *count, unsigned capacity) {
    if (array < 0) return true;
    if (!is_type(doc, array, BOSUN_JSON_ARRAY)) { ++rt->invalid_messages; return false; }
    for (int token = array + 1; token < doc->tokens[array].next; token = doc->tokens[token].next) {
        if (*count == capacity) { ++rt->queue_overflows; return false; }
        if (!decode(rt, doc, token, &commands[*count])) return false;
        ++*count;
    }
    return true;
}

static bool enqueue(bosun_runtime_t *rt, const bosun_runtime_command_t *commands,
                     unsigned count, bool prepend) {
    if (count > BOSUN_RUNTIME_COMMANDS - rt->queue_count) { ++rt->queue_overflows; return false; }
    if (prepend) rt->queue_head = (uint16_t)((rt->queue_head + BOSUN_RUNTIME_COMMANDS - count) % BOSUN_RUNTIME_COMMANDS);
    for (unsigned i = 0; i < count; ++i) {
        unsigned at = (rt->queue_head + (prepend ? 0 : rt->queue_count) + i) % BOSUN_RUNTIME_COMMANDS;
        rt->commands[at] = commands[i];
    }
    rt->queue_count += (uint16_t)count;
    return true;
}

bool bosun_runtime_dispatch(bosun_runtime_t *rt, const bosun_json_doc_t *doc, int token) {
    bosun_runtime_command_t command;
    return rt && decode(rt, doc, token, &command) && enqueue(rt, &command, 1, false);
}

bool bosun_runtime_action(bosun_runtime_t *rt, const bosun_json_doc_t *doc, int token) {
    if (!rt || !is_type(doc, token, BOSUN_JSON_OBJECT)) return false;
    bosun_runtime_command_t commands[BOSUN_RUNTIME_COMMANDS];
    unsigned count = 0;
    return collect(rt, doc, field(doc, token, "messages"), commands, &count,
        BOSUN_RUNTIME_COMMANDS - rt->queue_count) && enqueue(rt, commands, count, false);
}

static bool transmit(void *context, const uint8_t *data, size_t length) {
    bosun_runtime_t *rt = context;
    ++rt->midi_tx_count;
    if (rt->midi_monitor && rt->monitor) rt->monitor(rt->monitor_context, true, 0, 0, 0, data, length);
    if (rt->send && rt->send(rt->send_context, data, length)) return true;
    ++rt->midi_tx_failed;
    return false;
}

static bool voice(bosun_runtime_t *rt, uint8_t channel, uint8_t status, uint8_t first, uint8_t second) {
    uint8_t bytes[3];
    size_t length = bosun_midi_encode(bytes, sizeof(bytes), channel, status, first, second);
    return length && transmit(rt, bytes, length);
}

static uint32_t setting(const bosun_json_doc_t *doc, int object, const char *key, int32_t fallback) {
    int32_t value = bosun_config_int(doc, object, key, fallback);
    return (uint32_t)(value >= 1 && value <= 60000 ? value : fallback);
}

static int jack_entry(const bosun_json_doc_t *doc, unsigned jack) {
    int array = field(doc, 0, "expression");
    if (!is_type(doc, array, BOSUN_JSON_ARRAY)) return -1;
    int found = -1;
    for (int t = array + 1; t < doc->tokens[array].next; t = doc->tokens[t].next)
        if (bosun_config_int(doc, t, "jack", 0) == (int32_t)jack) found = t;
    return found;
}

static void configure_expression(bosun_runtime_t *rt) {
    const bosun_json_doc_t *device = &rt->config->device_doc, *patch_doc = &rt->config->patch_doc;
    static const char *const curves[] = {"linear", "exp", "log"};
    for (unsigned i = 0; i < 2; ++i) {
        bosun_runtime_expression_t fresh;
        memset(&fresh, 0, sizeof(fresh));
        fresh.value = fresh.baseline = -1;
        fresh.present = true;
        int entry = jack_entry(device, i + 1);
        int32_t minimum = bosun_config_int(device, field(device, entry, "calibration"), "min", 300);
        int32_t maximum = bosun_config_int(device, field(device, entry, "calibration"), "max", 65535);
        fresh.minimum = (uint16_t)(minimum >= 0 && minimum <= 65535 ? minimum : 300);
        fresh.maximum = (uint16_t)(maximum >= 0 && maximum <= 65535 ? maximum : 65535);
        int curve = enumeration(device, entry, "curve", curves, 3, 0);
        fresh.curve = (uint8_t)(curve < 0 ? 0 : curve);
        fresh.invert = bosun_config_bool(device, entry, "invert", false);
        fresh.enabled = entry >= 0 && bosun_config_bool(device, entry, "enabled", false);
        int message = field(device, entry, "message");
        const bosun_json_doc_t *message_doc = device;
        int override = rt->config->has_patch ? jack_entry(patch_doc, i + 1) : -1;
        int replacement = field(patch_doc, override, "message");
        if (entry >= 0 && is_type(patch_doc, replacement, BOSUN_JSON_OBJECT)) {
            message = replacement; message_doc = patch_doc;
        }
        if (entry >= 0 && field(patch_doc, override, "invert") >= 0)
            fresh.invert = bosun_config_bool(patch_doc, override, "invert", fresh.invert);
        if (message < 0 || !decode(rt, message_doc, message, &fresh.message)) fresh.enabled = false;
        if (fresh.enabled && fresh.message.type != BOSUN_COMMAND_CC &&
            !(fresh.message.type == BOSUN_COMMAND_KEMPER &&
              (fresh.message.index == BOSUN_KEMPER_WAH || fresh.message.index == BOSUN_KEMPER_VOLUME ||
               fresh.message.index == BOSUN_KEMPER_MORPH))) {
            fresh.enabled = false; ++rt->unsupported_messages;
        }
        bosun_runtime_expression_t *old = &rt->expression[i];
        if (fresh.minimum == old->minimum && fresh.maximum == old->maximum &&
            fresh.curve == old->curve && fresh.enabled == old->enabled && fresh.invert == old->invert &&
            !memcmp(&fresh.message, &old->message, sizeof(fresh.message))) {
            fresh.raw = old->raw; fresh.smooth = old->smooth; fresh.value = old->value;
            fresh.baseline = old->baseline; fresh.sampled = old->sampled;
            fresh.armed = old->armed; fresh.present = old->present;
        }
        *old = fresh;
    }
}

static uint8_t binding_blocks(const bosun_json_doc_t *doc, int binding, uint8_t *first) {
    *first = UINT8_MAX;
    int actions = field(doc, binding, "actions");
    if (!is_type(doc, actions, BOSUN_JSON_OBJECT)) return 0;
    uint8_t mask = 0;
    for (int key = actions + 1; key < doc->tokens[actions].next;) {
        int action = key + 1;
        int array = field(doc, action, "messages");
        if (is_type(doc, array, BOSUN_JSON_ARRAY)) {
            for (int message = array + 1; message < doc->tokens[array].next; message = doc->tokens[message].next) {
                if (!bosun_json_equal(doc, field(doc, message, "type"), "kemper_effect_toggle")) continue;
                int index = enumeration(doc, message, "slot", block_names, 8, -1);
                if (index >= 0) {
                    if (*first == UINT8_MAX) *first = (uint8_t)index;
                    mask |= (uint8_t)(1u << index);
                }
            }
        }
        key = doc->tokens[action].next;
    }
    return mask;
}

void bosun_runtime_config_changed(bosun_runtime_t *rt) {
    if (!rt || !rt->config) return;
    if (rt->initialized && strcmp(rt->profile, rt->config->profile)) {
        /* A profile switch ends macros and gestures belonging to the old
         * target, including delayed commands and pending double taps. */
        rt->queue_head = rt->queue_count = 0;
        rt->waiting = rt->preview_active = false;
        for (unsigned i = 0; i < BOSUN_RUNTIME_SWITCHES; ++i) bosun_switch_reset(&rt->switches[i]);
    }
    const bosun_json_doc_t *device = &rt->config->device_doc, *patch_doc = &rt->config->patch_doc;
    int nav = field(device, field(device, 0, "preset_navigation"), "switches");
    int global_long = field(device, 0, "long_press_actions");
    int bindings = rt->config->has_patch ? field(patch_doc, 0, "bindings") : -1;
    uint8_t bound = 0;
    for (unsigned i = 0; i < BOSUN_RUNTIME_SWITCHES; ++i) {
        bosun_runtime_binding_t *binding = &rt->bindings[i];
        memset(binding, 0, sizeof(*binding));
        binding->patch_token = binding->global_long_token = -1;
        if (is_type(patch_doc, bindings, BOSUN_JSON_ARRAY)) {
            for (int token = bindings + 1; token < patch_doc->tokens[bindings].next; token = patch_doc->tokens[token].next)
                if (bosun_json_equal(patch_doc, field(patch_doc, token, "switch"), switch_names[i]))
                    binding->patch_token = (int16_t)token;
        }
        int mode = enumeration(patch_doc, binding->patch_token, "mode", mode_names, 5, 0);
        binding->mode = (uint8_t)(mode < 0 ? 0 : mode);
        binding->blocks = binding_blocks(patch_doc, binding->patch_token, &binding->mirror_block);
        bound |= binding->blocks;
        if (binding->patch_token < 0) {
            int32_t slot = bosun_config_int(device, nav, switch_names[i], 0);
            if (slot > 0 && slot <= 999 && bosun_config_has_patch(rt->config, rt->config->bank, (unsigned)slot))
                binding->preset_slot = (uint16_t)slot;
        }
        int long_array = field(device, global_long, switch_names[i]);
        int explicit_long = field(patch_doc, field(patch_doc, binding->patch_token, "actions"), "long_press");
        if (binding->mode == BOSUN_SWITCH_TAP && explicit_long < 0 &&
            is_type(device, long_array, BOSUN_JSON_ARRAY) && device->tokens[long_array].next > long_array + 1) {
            binding->mode = BOSUN_SWITCH_LONG_PRESS_ALT;
            binding->global_long_token = (int16_t)long_array;
        }
        bosun_switch_config *config = &rt->switches[i].config;
        config->long_press_ms = setting(device, 0, "long_press_ms", 600);
        config->double_tap_window_ms = setting(device, 0, "double_tap_window_ms", 250);
        config->auto_momentary_ms = setting(device, 0, "auto_momentary_ms", 500);
        bool global_hold = bosun_config_bool(device, 0, "auto_momentary_on_hold", true);
        config->auto_momentary_on_hold = bosun_config_bool(patch_doc, binding->patch_token, "auto_momentary", global_hold);
    }
    bool enabled = is_type(device, field(device, 0, "kemper"), BOSUN_JSON_OBJECT);
    uint8_t channel = (uint8_t)bosun_config_int(device, 0, "midi_channel", 1);
    if (channel < 1 || channel > 16) channel = 1;
    if (!rt->initialized || enabled != rt->kemper_enabled || strcmp(rt->profile, rt->config->profile)) {
        bosun_kemper_init(&rt->kemper, channel, bound, transmit, rt);
        memcpy(rt->profile, rt->config->profile, sizeof(rt->profile));
        if (rt->config->bank >= 1 && rt->config->bank <= 25 && rt->config->slot >= 1 && rt->config->slot <= 5) {
            rt->kemper.state.rig = (uint8_t)((rt->config->bank - 1) * 5 + rt->config->slot);
            rt->kemper.state.bank = (uint8_t)rt->config->bank;
            rt->kemper.state.rig_in_bank = (uint8_t)rt->config->slot;
        }
    } else {
        rt->kemper.channel = channel;
        bosun_kemper_set_bound_blocks(&rt->kemper, bound);
    }
    rt->kemper_enabled = enabled;
    rt->initialized = true;
    configure_expression(rt);
    rt->config_revision = rt->config->revision;
    rt->patch_revision = rt->config->patch_revision;
    ++rt->revision;
}

void bosun_runtime_init(bosun_runtime_t *rt, bosun_config_t *config,
                         bosun_midi_send_fn send, void *context) {
    if (!rt) return;
    memset(rt, 0, sizeof(*rt));
    rt->config = config; rt->send = send; rt->send_context = context;
    for (unsigned i = 0; i < BOSUN_RUNTIME_SWITCHES; ++i) bosun_switch_init(&rt->switches[i], NULL);
    bosun_runtime_config_changed(rt);
}

static bosun_store_result_t storage_result(bosun_runtime_t *rt, bosun_store_result_t result) {
    rt->last_error = result;
    if (result != BOSUN_STORE_OK) ++rt->storage_errors;
    return result;
}

static void mirror_effects(bosun_runtime_t *rt, uint8_t changed) {
    for (unsigned i = 0; i < BOSUN_RUNTIME_SWITCHES; ++i) {
        unsigned block = rt->bindings[i].mirror_block;
        if (rt->bindings[i].mode == BOSUN_SWITCH_LATCHED && block < BOSUN_KEMPER_BLOCKS &&
            (changed & rt->kemper.state.effect_known & (1u << block)))
            rt->switches[i].latched_on = rt->kemper.state.effects[block];
    }
}

static uint8_t effects_on(const bosun_runtime_t *rt) {
    uint8_t on = 0;
    for (unsigned i = 0; i < BOSUN_KEMPER_BLOCKS; ++i)
        if (rt->kemper.state.effects[i]) on |= (uint8_t)(1u << i);
    return on;
}

typedef struct {
    bosun_runtime_t *runtime;
    bosun_runtime_command_t *commands;
    unsigned count, capacity;
} patch_action_check;

static bool check_patch_actions(const bosun_json_doc_t *doc, void *context) {
    patch_action_check *check = context;
    return collect(check->runtime, doc, field(doc, field(doc, 0, "on_enter"), "messages"),
        check->commands, &check->count, check->capacity);
}

bosun_store_result_t bosun_runtime_switch_patch(bosun_runtime_t *rt,
    unsigned bank, unsigned slot, bool fire_actions) {
    if (!rt || !rt->config) return BOSUN_STORE_INVALID;
    if (!bosun_config_has_patch(rt->config, bank, slot)) return storage_result(rt, BOSUN_STORE_NOT_FOUND);
    bosun_runtime_command_t macros[BOSUN_RUNTIME_COMMANDS];
    unsigned count = 0;
    const bosun_json_doc_t *doc = &rt->config->patch_doc;
    if (fire_actions && rt->config->has_patch && (bank != rt->config->bank || slot != rt->config->slot) &&
        !collect(rt, doc, field(doc, field(doc, 0, "on_exit"), "messages"), macros, &count,
                 BOSUN_RUNTIME_COMMANDS - rt->queue_count)) return BOSUN_STORE_INVALID;
    patch_action_check check = {rt, macros, count, BOSUN_RUNTIME_COMMANDS - rt->queue_count};
    /* Validate both macros against one capacity before publishing the target
     * patch. The config workspace is borrowed only during this callback. */
    bosun_store_result_t result = bosun_config_select_checked(rt->config, bank, slot,
        fire_actions ? check_patch_actions : NULL, &check);
    if (result != BOSUN_STORE_OK) return storage_result(rt, result);
    rt->preview_active = false;
    for (unsigned i = 0; i < BOSUN_RUNTIME_SWITCHES; ++i) bosun_switch_reset(&rt->switches[i]);
    bosun_runtime_config_changed(rt);
    if (rt->kemper_enabled && fire_actions && bank <= 25 && slot <= 5)
        (void)bosun_kemper_begin_rig(&rt->kemper, (uint8_t)((bank - 1) * 5 + slot), rt->now_ms);
    if (!fire_actions) mirror_effects(rt, 0xff);
    if (!enqueue(rt, macros, check.count, true)) return storage_result(rt, BOSUN_STORE_LIMIT);
    return storage_result(rt, BOSUN_STORE_OK);
}

static int wrapped(int current, int delta, int count) {
    int result = (current + delta) % count;
    return result < 0 ? result + count : result;
}

static bool inventory(bosun_runtime_t *rt, bool setlist) {
    rt->navigation_count = 0;
    if (!setlist) {
        size_t count = 0;
        if (storage_result(rt, bosun_config_coordinates_list(rt->config, rt->navigation,
            BOSUN_RUNTIME_NAV_PATCHES, &count)) != BOSUN_STORE_OK) return false;
        rt->navigation_count = (uint16_t)count;
    } else {
        const bosun_json_doc_t *d = &rt->config->device_doc;
        int array = field(d, field(d, 0, "setlist"), "items");
        if (!is_type(d, array, BOSUN_JSON_ARRAY)) return false;
        for (int t = array + 1; t < d->tokens[array].next; t = d->tokens[t].next) {
            int32_t bank, slot;
            int b = is_type(d, t, BOSUN_JSON_ARRAY) ? bosun_json_at(d, t, 0) : field(d, t, "bank");
            int s = is_type(d, t, BOSUN_JSON_ARRAY) ? bosun_json_at(d, t, 1) : field(d, t, "slot");
            if (!bosun_json_integer(d, b, &bank) || !bosun_json_integer(d, s, &slot) || bank < 1 || slot < 1 ||
                !bosun_config_has_patch(rt->config, (unsigned)bank, (unsigned)slot)) continue;
            if (rt->navigation_count == BOSUN_RUNTIME_NAV_PATCHES) {
                rt->navigation_count = 0; storage_result(rt, BOSUN_STORE_LIMIT); return false;
            }
            rt->navigation[rt->navigation_count++] = (bosun_runtime_coordinate_t){(uint16_t)bank, (uint16_t)slot};
        }
    }
    return rt->navigation_count > 0;
}

static bool navigate(bosun_runtime_t *rt, const bosun_runtime_command_t *c) {
    bool preview = c->type == BOSUN_COMMAND_PREVIEW_STEP;
    bool setlist = c->type == BOSUN_COMMAND_SETLIST_STEP;
    if (!c->value || !inventory(rt, setlist)) return true;
    unsigned bank = preview && rt->preview_active ? rt->preview_bank : rt->config->bank;
    unsigned slot = preview && rt->preview_active ? rt->preview_slot : rt->config->slot;
    unsigned target = 0, count = rt->navigation_count;
    if (!setlist && (c->type == BOSUN_COMMAND_BANK_STEP || c->flags)) {
        uint16_t starts[BOSUN_RUNTIME_NAV_PATCHES];
        unsigned banks = 0;
        int current = -1;
        for (unsigned i = 0; i < count; ++i) if (!i || rt->navigation[i].bank != rt->navigation[i - 1].bank) {
            starts[banks] = (uint16_t)i;
            if (rt->navigation[i].bank == bank) current = (int)banks;
            ++banks;
        }
        if (current < 0 && preview) {
            current = 0;
            while (current + 1 < (int)banks && rt->navigation[starts[current]].bank < bank) ++current;
        }
        int next = current < 0 ? (c->value > 0 ? 0 : (int)banks - 1) : wrapped(current, c->value, (int)banks);
        target = starts[next];
        unsigned end = next + 1 < (int)banks ? starts[next + 1] : count;
        for (unsigned i = target; i < end; ++i)
            if (rt->navigation[i].slot == slot || (preview && rt->navigation[i].slot <= slot)) target = i;
        if (!preview && rt->navigation[target].bank == bank) return true;
    } else {
        int current = -1;
        for (unsigned i = 0; i < count; ++i)
            if (rt->navigation[i].bank == bank && rt->navigation[i].slot == slot) { current = (int)i; break; }
        if (current < 0 && !setlist) {
            current = 0;
            while (current + 1 < (int)count && (rt->navigation[current].bank < bank ||
                (rt->navigation[current].bank == bank && rt->navigation[current].slot < slot))) ++current;
        }
        target = (unsigned)(current < 0 ? (c->value > 0 ? 0 : (int)count - 1) : wrapped(current, c->value, (int)count));
    }
    if (preview) {
        rt->preview_bank = rt->navigation[target].bank; rt->preview_slot = rt->navigation[target].slot;
        rt->preview_active = true;
        if (storage_result(rt, bosun_config_patch_name(rt->config, rt->preview_bank,
            rt->preview_slot, rt->preview_name, sizeof(rt->preview_name))) != BOSUN_STORE_OK) {
            rt->preview_active = false; return false;
        }
        rt->preview_until_ms = rt->now_ms + setting(&rt->config->device_doc,
            field(&rt->config->device_doc, 0, "preview"), "timeout_ms", 1500);
        ++rt->revision;
        return true;
    }
    return bosun_runtime_switch_patch(rt, rt->navigation[target].bank, rt->navigation[target].slot, true) == BOSUN_STORE_OK;
}

static bool execute(bosun_runtime_t *rt, const bosun_runtime_command_t *c) {
    switch ((bosun_command_type_t)c->type) {
    case BOSUN_COMMAND_CC: return voice(rt, c->channel, 0xb0, (uint8_t)c->first, (uint8_t)c->value);
    case BOSUN_COMMAND_PC: return voice(rt, c->channel, 0xc0, (uint8_t)c->first, 0);
    case BOSUN_COMMAND_NOTE_ON: return voice(rt, c->channel, 0x90, (uint8_t)c->first, (uint8_t)c->value);
    case BOSUN_COMMAND_NOTE_OFF: return voice(rt, c->channel, 0x80, (uint8_t)c->first, (uint8_t)c->value);
    case BOSUN_COMMAND_DELAY:
        if (c->value) { rt->waiting = true; rt->wait_until_ms = rt->now_ms + (uint32_t)c->value; }
        return true;
    case BOSUN_COMMAND_BANK_PC: {
        bool ok = voice(rt, c->channel, 0xb0, 0, (uint8_t)c->first);
        ok = voice(rt, c->channel, 0xb0, 32, (uint8_t)c->second) && ok;
        return voice(rt, c->channel, 0xc0, (uint8_t)c->value, 0) && ok;
    }
    case BOSUN_COMMAND_KEMPER_RIG:
        if (!rt->kemper_enabled) { ++rt->unsupported_messages; return false; }
        rt->waiting = true; rt->wait_until_ms = rt->now_ms + 5;
        return bosun_kemper_select_rig_channel(&rt->kemper, c->channel, (uint8_t)c->first, (uint8_t)c->second, rt->now_ms);
    case BOSUN_COMMAND_KEMPER:
        if (!rt->kemper_enabled) { ++rt->unsupported_messages; return false; }
        if (c->index == BOSUN_KEMPER_TUNER) {
            rt->kemper.state.tuner_active = c->value != 0; ++rt->kemper.state.revision;
        }
        return bosun_kemper_command_channel(&rt->kemper, c->channel,
            (bosun_kemper_command_type)c->index, (uint8_t)c->first, c->value);
    case BOSUN_COMMAND_KEMPER_QUERY: {
        if (!rt->kemper_enabled) { ++rt->unsupported_messages; return false; }
        bool ok = bosun_kemper_request_rig_name(&rt->kemper);
        return bosun_kemper_query_blocks(&rt->kemper, rt->kemper.bound_blocks) && ok;
    }
    case BOSUN_COMMAND_PATCH: return bosun_runtime_switch_patch(rt, c->first, c->second, true) == BOSUN_STORE_OK;
    case BOSUN_COMMAND_BANK_STEP: case BOSUN_COMMAND_PREVIEW_STEP: case BOSUN_COMMAND_SETLIST_STEP: return navigate(rt, c);
    case BOSUN_COMMAND_PREVIEW_COMMIT:
        return !rt->preview_active || bosun_runtime_switch_patch(rt, rt->preview_bank, rt->preview_slot, true) == BOSUN_STORE_OK;
    case BOSUN_COMMAND_PREVIEW_CANCEL: rt->preview_active = false; ++rt->revision; return true;
    }
    ++rt->invalid_messages;
    return false;
}

static void drain(bosun_runtime_t *rt) {
    if (rt->waiting) {
        if (!due(rt->now_ms, rt->wait_until_ms)) return;
        rt->waiting = false;
    }
    for (unsigned work = 0; work < BOSUN_RUNTIME_COMMANDS_PER_TICK && rt->queue_count && !rt->waiting; ++work) {
        bosun_runtime_command_t command = rt->commands[rt->queue_head];
        rt->queue_head = (uint16_t)((rt->queue_head + 1) % BOSUN_RUNTIME_COMMANDS);
        --rt->queue_count;
        if (!execute(rt, &command) && rt->last_error == BOSUN_STORE_OK) rt->last_error = BOSUN_STORE_IO;
    }
}

static void fire(bosun_runtime_t *rt, unsigned index, unsigned trigger) {
    bosun_runtime_binding_t binding = rt->bindings[index];
    bool admitted = false;
    if (trigger == 4 && binding.global_long_token >= 0) {
        bosun_runtime_command_t commands[BOSUN_RUNTIME_COMMANDS];
        unsigned count = 0;
        if (collect(rt, &rt->config->device_doc, binding.global_long_token, commands, &count,
            BOSUN_RUNTIME_COMMANDS - rt->queue_count)) admitted = enqueue(rt, commands, count, false);
    } else if (trigger == 0 && binding.preset_slot) {
        bosun_runtime_command_t command = {0};
        command.type = BOSUN_COMMAND_PATCH; command.first = rt->config->bank; command.second = binding.preset_slot;
        admitted = enqueue(rt, &command, 1, false);
    } else {
        const bosun_json_doc_t *d = &rt->config->patch_doc;
        int action = field(d, field(d, binding.patch_token, "actions"), action_names[trigger]);
        if (action >= 0) admitted = bosun_runtime_action(rt, d, action);
    }
    if (admitted && rt->binding_fired) rt->binding_fired(rt->binding_context, (uint8_t)index, (uint8_t)trigger);
}

void bosun_runtime_expression_present(bosun_runtime_t *rt, unsigned jack, bool present) {
    if (rt && jack >= 1 && jack <= 2) rt->expression[jack - 1].present = present;
}

static void sample_expression(bosun_runtime_t *rt, unsigned index, uint16_t raw) {
    bosun_runtime_expression_t *e = &rt->expression[index];
    e->raw = raw;
    if (!e->enabled) return;
    if (!e->sampled) { e->sampled = true; e->smooth = raw; }
    else {
        int32_t difference = (int32_t)raw - e->smooth;
        e->smooth += difference >= 0 ? difference / 4 : -((-difference + 3) / 4);
    }
    int32_t maximum = e->maximum > e->minimum ? e->maximum : (int32_t)e->minimum + 1;
    double fraction = (double)(e->smooth - e->minimum) / (maximum - e->minimum);
    if (fraction < 0) fraction = 0;
    if (fraction > 1) fraction = 1;
    if (e->curve == 1) fraction *= fraction;
    else if (e->curve == 2) fraction = sqrt(fraction);
    if (e->invert) fraction = 1 - fraction;
    int value = (int)(fraction * 127 + 0.5);
    if (e->baseline < 0) e->baseline = (int16_t)value;
    if (!e->armed && (value - e->baseline >= 8 || e->baseline - value >= 8)) e->armed = true;
    if (value == e->value) return;
    e->value = (int16_t)value;
    ++rt->revision;
    if (e->armed && e->present) {
        bosun_runtime_command_t command = e->message;
        command.value = value;
        (void)enqueue(rt, &command, 1, false);
    }
}

void bosun_runtime_tick(bosun_runtime_t *rt, uint32_t now_ms, uint16_t pressed_mask,
    uint16_t expression1, uint16_t expression2) {
    if (!rt || !rt->config) return;
    rt->now_ms = now_ms;
    if (rt->config_revision != rt->config->revision || rt->patch_revision != rt->config->patch_revision)
        bosun_runtime_config_changed(rt);
    if (rt->kemper_enabled) {
        uint8_t known = rt->kemper.state.effect_known, on = effects_on(rt);
        bosun_kemper_tick(&rt->kemper, now_ms);
        mirror_effects(rt, (uint8_t)((known ^ rt->kemper.state.effect_known) | (on ^ effects_on(rt))));
    }
    for (unsigned i = 0; i < BOSUN_RUNTIME_SWITCHES; ++i) {
        bosun_switch_result result = bosun_switch_poll(&rt->switches[i], now_ms,
            !(pressed_mask & (1u << i)), (bosun_switch_mode)rt->bindings[i].mode);
        if (result.edge == BOSUN_SWITCH_PRESS_EDGE && rt->kemper_enabled && rt->kemper.state.tuner_active &&
            bosun_config_bool(&rt->config->device_doc, 0, "tuner_exit_on_press", true)) {
            (void)bosun_kemper_command(&rt->kemper, BOSUN_KEMPER_TUNER, 0, 0);
            rt->kemper.state.tuner_active = false; ++rt->kemper.state.revision;
        }
        for (unsigned trigger = 0; trigger < 6; ++trigger)
            if (result.triggers & (1u << trigger)) fire(rt, i, trigger);
        if (result.edge != BOSUN_SWITCH_NO_EDGE || result.triggers) ++rt->revision;
    }
    if (!rt->expression_polled || (uint32_t)(now_ms - rt->expression_last_ms) >= 10) {
        rt->expression_polled = true; rt->expression_last_ms = now_ms;
        sample_expression(rt, 0, expression1); sample_expression(rt, 1, expression2);
    }
    if (rt->preview_active && due(now_ms, rt->preview_until_ms)) {
        const bosun_json_doc_t *d = &rt->config->device_doc;
        if (bosun_json_equal(d, field(d, field(d, 0, "preview"), "on_timeout"), "cancel")) {
            rt->preview_active = false; ++rt->revision;
        } else (void)bosun_runtime_switch_patch(rt, rt->preview_bank, rt->preview_slot, true);
    }
    drain(rt);
    uint16_t held = 0;
    for (unsigned i = 0; i < BOSUN_RUNTIME_SWITCHES; ++i)
        if (bosun_switch_momentary_active(&rt->switches[i], now_ms, (bosun_switch_mode)rt->bindings[i].mode))
            held |= (uint16_t)(1u << i);
    if (held != rt->held_mask) { rt->held_mask = held; ++rt->revision; }
}

static void receive(void *context, uint8_t channel, uint8_t status, const uint8_t *data, size_t length) {
    bosun_runtime_t *rt = context;
    ++rt->midi_rx_count;
    if ((rt->midi_monitor || rt->midi_learn) && rt->monitor)
        rt->monitor(rt->monitor_context, false, rt->receiving_port, channel, status, data, length);
    if (rt->midi_learn && channel && length <= 2) {
        ++rt->learn.sequence; rt->learn.fresh = true; rt->learn.port = rt->receiving_port;
        rt->learn.channel = channel; rt->learn.status = status; rt->learn.length = (uint8_t)length;
        memset(rt->learn.data, 0, sizeof(rt->learn.data));
        memcpy(rt->learn.data, data, length);
    }
    if (!rt->kemper_enabled) return;
    uint32_t changes = rt->kemper.state.external_rig_changes;
    uint8_t previous = rt->kemper.state.effect_known, on = 0;
    for (unsigned i = 0; i < 8; ++i) if (rt->kemper.state.effects[i]) on |= (uint8_t)(1u << i);
    bosun_kemper_handle(&rt->kemper, channel, status, data, length, rt->now_ms);
    uint8_t changed = (uint8_t)(previous ^ rt->kemper.state.effect_known);
    for (unsigned i = 0; i < 8; ++i)
        if (!!(on & (1u << i)) != rt->kemper.state.effects[i]) changed |= (uint8_t)(1u << i);
    mirror_effects(rt, changed);
    if (changes != rt->kemper.state.external_rig_changes)
        (void)bosun_runtime_switch_patch(rt, rt->kemper.state.bank, rt->kemper.state.rig_in_bank, false);
}

void bosun_runtime_feed_midi(bosun_runtime_t *rt, uint8_t port, const uint8_t *bytes,
    size_t length, uint32_t now_ms) {
    if (!rt || port > 1 || (!bytes && length)) return;
    rt->now_ms = now_ms; rt->receiving_port = port;
    bosun_midi_feed(&rt->midi[port], bytes, length, receive, rt);
}

void bosun_runtime_reset_midi_input(bosun_runtime_t *rt, uint8_t port) {
    if (rt && port < 2) bosun_midi_init(&rt->midi[port]);
}

static bool key(bosun_json_writer_t *w, const char *name) {
    return bosun_json_puts(w, ",") && bosun_json_quote(w, name) && bosun_json_puts(w, ":");
}

static bool string_field(bosun_json_writer_t *w, const char *name, const char *value) {
    return key(w, name) && bosun_json_quote(w, value);
}

static bool integer_field(bosun_json_writer_t *w, const char *name, int32_t value) {
    return key(w, name) && bosun_json_write_integer(w, value);
}

bool bosun_runtime_context(const bosun_runtime_t *rt, bosun_json_writer_t *w) {
    if (!rt || !rt->config || !w) return false;
    const bosun_json_doc_t *d = &rt->config->patch_doc;
    const bosun_kemper_state *k = &rt->kemper.state;
    char name[129];
    if (!rt->config->has_patch || !bosun_json_string(d, field(d, 0, "name"), name, sizeof(name))) name[0] = 0;
    const char *title = rt->kemper_enabled && k->rig_name_fresh && *k->rig_name ? k->rig_name : name;
    if (!bosun_json_puts(w, "{\"bank\":") || !bosun_json_write_integer(w, rt->preview_active ? rt->preview_bank : rt->config->bank) ||
        !key(w, "slot") || !bosun_json_write_integer(w, rt->preview_active ? rt->preview_slot : rt->config->slot) ||
        !string_field(w, "patch_name", rt->preview_active ? rt->preview_name : title) ||
        !string_field(w, "preview", rt->preview_active ? "on" : "") ||
        !string_field(w, "expression_mode", rt->kemper_enabled ? bosun_kemper_expression_label(k->expression_mode) : "")) return false;
    if (rt->kemper_enabled) {
        if ((rt->preview_active || k->rig_name_fresh) &&
            !string_field(w, "kemper_rig_name", rt->preview_active ? rt->preview_name : k->rig_name)) return false;
        if (!integer_field(w, "kemper_bank", rt->preview_active ? rt->preview_bank : k->bank) ||
            !integer_field(w, "kemper_rig_in_bank", rt->preview_active ? rt->preview_slot : k->rig_in_bank) ||
            !integer_field(w, "kemper_rig", rt->preview_active ? (rt->preview_bank - 1) * 5 + rt->preview_slot : k->rig) ||
            !string_field(w, "kemper_connected", k->connected ? "on" : "off") ||
            !string_field(w, "tuner", k->tuner_active ? "on" : "off") || !string_field(w, "kemper_tuner", k->tuner_active ? "on" : "off") ||
            !string_field(w, "tuner_note", k->tuner_note) || !string_field(w, "kemper_tuner_note", k->tuner_note) ||
            !integer_field(w, "tuner_deviance", k->tuner_deviance) ||
            !integer_field(w, "kemper_tuner_deviance", k->tuner_deviance) ||
            !integer_field(w, "kemper_bpm", k->bpm)) return false;
        /* Unknown or retired-generation blocks remain absent, allowing Stage
         * to distinguish an unconfirmed state from a confirmed off state. */
        for (unsigned i = 0; i < BOSUN_KEMPER_BLOCKS; ++i) if (k->effect_known & (1u << i)) {
            char block_key[32] = "kemper_block_";
            strcat(block_key, block_names[i]);
            if (!string_field(w, block_key, k->effects[i] ? "on" : "off")) return false;
        }
    }
    name[0] = 0;
    for (unsigned i = 0; i < BOSUN_RUNTIME_SWITCHES; ++i) if (rt->held_mask & (1u << i)) {
        int binding = rt->bindings[i].patch_token;
        if (!bosun_json_string(d, field(d, binding, "hold_text"), name, sizeof name) || !*name)
            if (!bosun_json_string(d, field(d, binding, "label"), name, sizeof name)) name[0] = 0;
        if (*name) break;
    }
    if (!string_field(w, "hold_effect", name) || !integer_field(w, "hold_mask", rt->held_mask) ||
        !key(w, "switches") || !bosun_json_puts(w, "{")) return false;
    for (unsigned i = 0; i < BOSUN_RUNTIME_SWITCHES; ++i) {
        if ((i && !bosun_json_puts(w, ",")) || !bosun_json_quote(w, switch_names[i]) || !bosun_json_puts(w, ":") ||
            !bosun_json_puts(w, rt->switches[i].latched_on ? "true" : "false")) return false;
    }
    return bosun_json_puts(w, "}}");
}
