#include "bosun/protocol.h"
#include "bosun_manifest.h"
#include <string.h>

static bool field(bosun_json_writer_t *w, const char *name) {
    return bosun_json_puts(w, ",") && bosun_json_quote(w, name) && bosun_json_puts(w, ":");
}
static void integer(bosun_json_writer_t *w, const char *name, int32_t value) {
    field(w, name); bosun_json_write_integer(w, value);
}
static void unsigned_integer(bosun_json_writer_t *w, const char *name, uint32_t value) {
    char digits[10]; size_t used = sizeof digits;
    do { digits[--used] = (char)('0' + value % 10u); value /= 10u; } while (value);
    field(w, name); bosun_json_write(w, digits + used, sizeof digits - used);
}
static void string(bosun_json_writer_t *w, const char *name, const char *value) {
    field(w, name); bosun_json_quote(w, value);
}
static int get(bosun_protocol_t *p, const char *name) { return bosun_json_get(&p->request, 0, name); }
static bool raw(bosun_json_writer_t *w, const bosun_json_doc_t *d, int token) {
    const char *data; size_t length;
    return bosun_json_raw(d, token, &data, &length) && bosun_json_write(w, data, length);
}
static void begin(bosun_protocol_t *p, const char *type) {
    bosun_json_writer_init(&p->writer, p->tx, sizeof p->tx);
    bosun_json_puts(&p->writer, "{\"type\":"); bosun_json_quote(&p->writer, type);
    field(&p->writer, "id"); bosun_json_puts(&p->writer, p->id);
}
static void error(bosun_protocol_t *p, const char *reason) {
    ++p->errors;
    begin(p, "ERROR"); string(&p->writer, "error", reason);
    string(&p->writer, "of", p->type);
}
static void finish(bosun_protocol_t *p) {
    if (p->writer.failed || p->writer.length + 2 >= sizeof p->tx) error(p, "response_too_large");
    bosun_json_puts(&p->writer, "}\n");
    p->tx_length = p->writer.length; p->tx_offset = 0;
}
static const char *store_error(bosun_store_result_t result) {
    switch (result) {
    case BOSUN_STORE_NOT_FOUND: return "not_found";
    case BOSUN_STORE_UNAVAILABLE: return "storage_unavailable";
    case BOSUN_STORE_INVALID: return "invalid_request";
    case BOSUN_STORE_LIMIT: return "limit_exceeded";
    default: return "storage_error";
    }
}
static bool text_arg(bosun_protocol_t *p, const char *name, char *out, size_t capacity, bool optional) {
    int token = get(p, name);
    if (token < 0 && optional) { out[0] = 0; return true; }
    return bosun_json_string(&p->request, token, out, capacity);
}
static bool coordinates(bosun_protocol_t *p, unsigned *bank, unsigned *slot, bool optional) {
    int b = get(p, "bank"), s = get(p, "slot"); int32_t bn = 0, sn = 0;
    if (optional && b < 0 && s < 0) { *bank = *slot = 0; return true; }
    if (!bosun_json_integer(&p->request, b, &bn) || !bosun_json_integer(&p->request, s, &sn) ||
        bn < 0 || sn < 0 || !bosun_config_coordinates((unsigned)bn, (unsigned)sn)) return false;
    *bank = (unsigned)bn; *slot = (unsigned)sn; return true;
}
static bool object_arg(bosun_protocol_t *p, const char *name, const char **data, size_t *length) {
    int t = get(p, name);
    return t >= 0 && p->request.tokens[t].type == BOSUN_JSON_OBJECT &&
        bosun_json_raw(&p->request, t, data, length);
}
static bool unique_arguments(const bosun_json_doc_t *d) {
    /* Unknown application JSON keeps Python's last-key-wins semantics. A
     * command envelope must have one unambiguous meaning before any I/O. */
    static const char *const names[] = {"type", "id", "profile", "bank", "slot",
        "profile_id", "name", "kind", "color", "device", "patch", "binding",
        "table", "on", "request", "mode"};
    uint32_t seen = 0;
    for (unsigned i = 1; i < d->tokens[0].next; i = d->tokens[i + 1].next)
        for (unsigned k = 0; k < sizeof names / sizeof *names; ++k)
            if (bosun_json_equal(d, (int)i, names[k])) {
                uint32_t bit = UINT32_C(1) << k;
                if (seen & bit) return false;
                seen |= bit; break;
            }
    return true;
}
static uint8_t event_byte(const bosun_protocol_t *p, unsigned offset) {
    return p->midi_events[(p->event_head + offset) % BOSUN_PROTOCOL_EVENT_BYTES];
}
static void event_append(bosun_protocol_t *p, uint8_t byte) {
    p->midi_events[(p->event_head + p->event_length) % BOSUN_PROTOCOL_EVENT_BYTES] = byte;
    ++p->event_length;
}
static void monitor(void *context, bool outbound, uint8_t port, uint8_t channel,
                    uint8_t status, const uint8_t *data, size_t length) {
    bosun_protocol_t *p = context;
    uint8_t flags = (p->runtime->midi_monitor ? 1u : 0u) |
        (!outbound && p->runtime->midi_learn ? 2u : 0u);
    if (!p->connected || !flags || p->reboot_requested) return;
    if (length > BOSUN_MIDI_MAX_SYSEX + 2u ||
        length + 6u > BOSUN_PROTOCOL_EVENT_BYTES - p->event_length) {
        ++p->midi_events_dropped; return;
    }
    /* Binary records preserve every byte with only six bytes of overhead.
     * Encoding is deferred until the response owns a completely empty TX. */
    event_append(p, (uint8_t)length); event_append(p, (uint8_t)(length >> 8));
    event_append(p, (uint8_t)(flags | (outbound ? 4u : 0u)));
    event_append(p, port); event_append(p, channel); event_append(p, status);
    for (size_t i = 0; i < length; ++i) event_append(p, data[i]);
}
static bool emit_midi_event(bosun_protocol_t *p) {
    while (p->event_length) {
        unsigned length = event_byte(p, 0) | ((unsigned)event_byte(p, 1) << 8);
        uint8_t flags = event_byte(p, 2), channel = event_byte(p, 4), status = event_byte(p, 5);
        bool outbound = (flags & 4u) != 0;
        bool midi = (flags & 1u) && p->runtime->midi_monitor;
        bool learn = (flags & 2u) && p->runtime->midi_learn;
        if (midi || learn) {
            strcpy(p->id, "null"); p->type[0] = 0;
            begin(p, "EVENT"); string(&p->writer, "event", midi ? "midi" : "midi_in_captured");
            if (!outbound) string(&p->writer, "port", event_byte(p, 3) ? "din" : "usb");
            if (midi) string(&p->writer, "dir", outbound ? "out" : "in");
            else {
                static const char *const kinds[] = {"note_off", "note_on", "poly_pressure",
                    "cc", "pc", "channel_pressure", "pitch_bend"};
                integer(&p->writer, "channel", channel);
                string(&p->writer, "kind", status >= 0x80 && status <= 0xe0 && !(status & 15u) ?
                    kinds[(status - 0x80) >> 4] : "unknown");
            }
            field(&p->writer, midi ? "raw" : "data"); bosun_json_puts(&p->writer, "[");
            bool comma = false;
            if (midi && !outbound) {
                bosun_json_write_integer(&p->writer, status == 0xf0 ? 0xf0 : status | ((channel - 1u) & 15u));
                comma = true;
            }
            for (unsigned i = 0; i < length; ++i) {
                if (comma) bosun_json_puts(&p->writer, ",");
                bosun_json_write_integer(&p->writer, event_byte(p, i + 6)); comma = true;
            }
            if (midi && !outbound && status == 0xf0) bosun_json_puts(&p->writer, ",247");
            bosun_json_puts(&p->writer, "]"); finish(p);
        }
        flags &= (uint8_t)~(midi ? 1u : 3u);
        if (!p->runtime->midi_learn) flags &= (uint8_t)~2u;
        if (!p->runtime->midi_monitor) flags &= (uint8_t)~1u;
        if (flags & 3u) p->midi_events[(p->event_head + 2u) % BOSUN_PROTOCOL_EVENT_BYTES] = flags;
        else {
            p->event_head = (uint16_t)((p->event_head + length + 6u) % BOSUN_PROTOCOL_EVENT_BYTES);
            p->event_length = (uint16_t)(p->event_length - length - 6u);
        }
        if (midi || learn) return true;
    }
    return false;
}
static void dirty(bosun_protocol_t *p) {
    bosun_config_t *c = p->runtime->config;
    field(&p->writer, "patches"); bosun_json_puts(&p->writer, "[");
    for (unsigned i = 0; i < c->dirty_count; ++i) {
        if (i) bosun_json_puts(&p->writer, ",");
        bosun_json_puts(&p->writer, "{\"bank\":");
        bosun_json_write_integer(&p->writer, c->dirty[i].bank);
        integer(&p->writer, "slot", c->dirty[i].slot); bosun_json_puts(&p->writer, "}");
    }
    bosun_json_puts(&p->writer, "]");
}
enum { UI_GLOBAL = 1, UI_PATCH = 2, UI_DIRTY = 4, UI_SAVED = 8, UI_DISCARDED = 16 };

static uint32_t device_hash(const bosun_config_t *c) {
    uint32_t hash = UINT32_C(2166136261);
    for (size_t i = 0; i < c->device_doc.length; ++i)
        hash = (hash ^ (uint8_t)c->device[i]) * UINT32_C(16777619);
    return hash;
}
static void observe_changes(bosun_protocol_t *p, bool discarded, uint8_t source) {
    const bosun_config_t *c = p->runtime->config;
    if (p->ui_observed && p->observed_revision == c->revision) return;
    uint8_t current[BOSUN_PROTOCOL_PATCH_BITMAP_BYTES] = {0};
    for (unsigned i = 0; i < c->dirty_count; ++i) {
        unsigned key = (c->dirty[i].bank - 1u) * 10u + c->dirty[i].slot - 1u;
        current[key / 8u] |= (uint8_t)(1u << (key % 8u));
    }
    uint32_t hash = device_hash(c);
    bool profile_changed = strcmp(c->profile, p->observed_profile) != 0;
    if (p->ui_observed && profile_changed) {
        memset(p->saved_pending, 0, sizeof p->saved_pending);
        memset(p->discarded_pending, 0, sizeof p->discarded_pending);
        p->ui_pending = UI_GLOBAL | UI_PATCH | UI_DIRTY;
    } else if (p->ui_observed) {
        for (unsigned i = 0; i < sizeof current; ++i) {
            uint8_t removed = (uint8_t)(p->dirty_snapshot[i] & ~current[i]);
            if (removed) {
                if (discarded) {
                    p->discarded_pending[i] |= removed; p->saved_pending[i] &= (uint8_t)~removed;
                    p->ui_pending |= UI_DISCARDED;
                } else {
                    p->saved_pending[i] |= removed; p->discarded_pending[i] &= (uint8_t)~removed;
                    p->ui_pending |= UI_SAVED;
                }
            }
            if (current[i] != p->dirty_snapshot[i]) p->ui_pending |= UI_DIRTY;
        }
        if (hash != p->observed_device_hash) p->ui_pending |= UI_GLOBAL;
    }
    if (p->ui_observed && (profile_changed || c->patch_revision != p->observed_patch_revision ||
        c->bank != p->observed_bank || c->slot != p->observed_slot)) {
        p->ui_pending |= UI_PATCH;
        p->patch_source = !source && p->runtime->kemper.state.external_rig_changes != p->observed_external_rigs ? 2 : source;
        /* Actions belonging to a retired patch must never repaint its new
         * successor. New-patch actions arrive after this snapshot is updated. */
        memset(p->binding_pending, 0, sizeof p->binding_pending);
    }
    memcpy(p->dirty_snapshot, current, sizeof current);
    memcpy(p->observed_profile, c->profile, sizeof p->observed_profile);
    p->observed_revision = c->revision; p->observed_patch_revision = c->patch_revision;
    p->observed_bank = c->bank; p->observed_slot = c->slot;
    p->observed_external_rigs = p->runtime->kemper.state.external_rig_changes;
    p->observed_device_hash = hash; p->ui_observed = true;
}
static void binding_fired(void *context, uint8_t index, uint8_t action) {
    bosun_protocol_t *p = context;
    if (!p->connected || p->reboot_requested || index >= BOSUN_RUNTIME_SWITCHES || action >= 6) return;
    observe_changes(p, false, 0);
    p->binding_pending[index] = (uint8_t)(action + 1u);
}
static void begin_event(bosun_protocol_t *p, const char *name) {
    strcpy(p->id, "null"); p->type[0] = 0;
    begin(p, "EVENT"); string(&p->writer, "event", name);
}
static void bitmap_coordinates(bosun_json_writer_t *w, const uint8_t *bits) {
    field(w, "patches"); bosun_json_puts(w, "[");
    bool comma = false;
    for (unsigned key = 0; key < 990; ++key) if (bits[key / 8u] & (1u << (key % 8u))) {
        if (comma) bosun_json_puts(w, ",");
        bosun_json_puts(w, "{\"bank\":"); bosun_json_write_integer(w, (int32_t)(key / 10u + 1u));
        integer(w, "slot", (int32_t)(key % 10u + 1u)); bosun_json_puts(w, "}"); comma = true;
    }
    bosun_json_puts(w, "]");
}
static bool emit_ui_event(bosun_protocol_t *p) {
    if (p->ui_pending & UI_GLOBAL) {
        p->ui_pending &= (uint8_t)~UI_GLOBAL; begin_event(p, "global_changed");
    } else if (p->ui_pending & UI_PATCH) {
        p->ui_pending &= (uint8_t)~UI_PATCH; begin_event(p, "patch_switched");
        integer(&p->writer, "bank", p->runtime->config->bank);
        integer(&p->writer, "slot", p->runtime->config->slot);
        string(&p->writer, "source", p->patch_source == 2 ? "midi_in" : p->patch_source == 1 ? "editor" : "binding");
        /* The consumer discards the previous patch context on this event. */
        p->context_revision = p->kemper_revision = UINT32_MAX;
    } else if (p->ui_pending & (UI_SAVED | UI_DISCARDED)) {
        bool saved = (p->ui_pending & UI_SAVED) != 0;
        uint8_t *bits = saved ? p->saved_pending : p->discarded_pending;
        p->ui_pending &= (uint8_t)~(saved ? UI_SAVED : UI_DISCARDED);
        begin_event(p, saved ? "saved" : "discarded"); bitmap_coordinates(&p->writer, bits);
        memset(bits, 0, BOSUN_PROTOCOL_PATCH_BITMAP_BYTES);
    } else if (p->ui_pending & UI_DIRTY) {
        p->ui_pending &= (uint8_t)~UI_DIRTY; begin_event(p, "dirty_state_changed"); dirty(p);
    } else {
        static const char *const switches[] = {"1", "2", "3", "4", "up", "A", "B", "C", "D", "down"};
        static const char *const actions[] = {"press", "release", "toggle_on", "toggle_off", "long_press", "double_tap"};
        unsigned index = 0;
        while (index < BOSUN_RUNTIME_SWITCHES && !p->binding_pending[index]) ++index;
        if (index == BOSUN_RUNTIME_SWITCHES) return false;
        begin_event(p, "binding_fired"); string(&p->writer, "switch", switches[index]);
        string(&p->writer, "action", actions[p->binding_pending[index] - 1u]);
        p->binding_pending[index] = 0;
    }
    finish(p); return true;
}
static void tft_projection(bosun_protocol_t *p, const bosun_json_doc_t *d) {
    int tft = bosun_json_get(d, 0, "tft");
    int layout = bosun_json_get(d, tft, "layout");
    bool array = layout >= 0 && d->tokens[layout].type == BOSUN_JSON_ARRAY;
    field(&p->writer, "tft_colors"); bosun_json_puts(&p->writer, "{");
    bool comma = false;
    if (array) for (int entry = layout + 1; entry < d->tokens[layout].next; entry = d->tokens[entry].next) {
        int color = bosun_json_get(d, entry, "color"), name_token = bosun_json_get(d, entry, "field");
        char name[129];
        if (color < 0 || d->tokens[color].type != BOSUN_JSON_STRING ||
            !bosun_json_string(d, name_token, name, sizeof name) || !*name) continue;
        bool duplicate = false;
        for (int earlier = layout + 1; earlier < entry; earlier = d->tokens[earlier].next) {
            int prior_color = bosun_json_get(d, earlier, "color");
            if (prior_color >= 0 && d->tokens[prior_color].type == BOSUN_JSON_STRING &&
                bosun_json_equal(d, bosun_json_get(d, earlier, "field"), name)) { duplicate = true; break; }
        }
        if (duplicate) continue;
        if (comma) bosun_json_puts(&p->writer, ",");
        raw(&p->writer, d, name_token); bosun_json_puts(&p->writer, ":"); raw(&p->writer, d, color);
        comma = true;
    }
    bosun_json_puts(&p->writer, "}");
    field(&p->writer, "tft_labels"); bosun_json_puts(&p->writer, "{");
    static const char *const fields[] = {"bank", "kemper_bank", "kemper_rig_in_bank", "kemper_rig", "slot"};
    uint8_t seen = 0; comma = false;
    if (array) for (int entry = layout + 1; entry < d->tokens[layout].next; entry = d->tokens[entry].next) {
        int name_token = bosun_json_get(d, entry, "field");
        unsigned index = 0;
        while (index < sizeof fields / sizeof *fields && !bosun_json_equal(d, name_token, fields[index])) ++index;
        if (index == sizeof fields / sizeof *fields || (seen & (1u << index))) continue;
        seen |= (uint8_t)(1u << index);
        if (comma) bosun_json_puts(&p->writer, ",");
        raw(&p->writer, d, name_token); bosun_json_puts(&p->writer, ":{\"prefix\":");
        int prefix = bosun_json_get(d, entry, "prefix"), suffix = bosun_json_get(d, entry, "suffix");
        if (prefix < 0 || d->tokens[prefix].type != BOSUN_JSON_STRING || !raw(&p->writer, d, prefix))
            bosun_json_quote(&p->writer, "");
        field(&p->writer, "suffix");
        if (suffix < 0 || d->tokens[suffix].type != BOSUN_JSON_STRING || !raw(&p->writer, d, suffix))
            bosun_json_quote(&p->writer, "");
        bosun_json_puts(&p->writer, "}"); comma = true;
    }
    bosun_json_puts(&p->writer, "}");
}
static void device_info(bosun_protocol_t *p) {
    bosun_config_t *c = p->runtime->config; const bosun_json_doc_t *d = &c->device_doc;
    begin(p, "DEVICE_INFO"); string(&p->writer, "fw", BOSUN_NATIVE_VERSION);
    field(&p->writer, "device");
    if (!raw(&p->writer, d, bosun_json_get(d, 0, "device_name"))) bosun_json_quote(&p->writer, "MIDI Captain");
    field(&p->writer, "current"); bosun_json_puts(&p->writer, "{\"bank\":");
    bosun_json_write_integer(&p->writer, c->bank); integer(&p->writer, "slot", c->slot);
    bosun_json_puts(&p->writer, "}"); string(&p->writer, "profile", c->profile);
    field(&p->writer, "preset_navigation");
    int nav = bosun_json_get(d, 0, "preset_navigation");
    if (nav < 0 || d->tokens[nav].type != BOSUN_JSON_OBJECT || !raw(&p->writer, d, nav))
        bosun_json_puts(&p->writer, "{}");
    /* Kiosk treats preset_navigation as the fast-bootstrap capability marker
     * and skips GET_GLOBAL, so it also needs the compact Screen projection. */
    tft_projection(p, d);
    bosun_json_puts(&p->writer, ",\"native_experimental\":true,\"firmware_ota\":false,\"reboot_modes\":[\"normal\",\"bootloader\"]");
}
/* The reply buffer is also the file read workspace. Validate saved JSON before
 * exposing it on wire; these handlers no longer need request tokens afterward. */
static bosun_store_result_t read_value(bosun_protocol_t *p, const char *profile,
    const char *file, unsigned bank, unsigned slot) {
    size_t length = 0, available = sizeof p->tx - p->writer.length - 3;
    char *out = p->tx + p->writer.length;
    bosun_store_result_t r = file ? bosun_config_read(p->runtime->config, profile, file, out, available, &length) :
        bosun_config_read_patch(p->runtime->config, profile, bank, slot, out, available, &length);
    if (r != BOSUN_STORE_OK) return r;
    bosun_json_doc_t doc;
    bosun_json_result_t parsed = bosun_json_parse(&doc, out, length, p->tokens, BOSUN_PROTOCOL_TOKENS);
    if (parsed != BOSUN_JSON_OK || doc.tokens[0].type != BOSUN_JSON_OBJECT)
        return parsed == BOSUN_JSON_LIMIT ? BOSUN_STORE_LIMIT : BOSUN_STORE_INVALID;
    p->writer.length += length; return BOSUN_STORE_OK;
}
static void handle(bosun_protocol_t *p, uint32_t now_ms) {
    bosun_runtime_t *rt = p->runtime; bosun_config_t *c = rt->config;
    bosun_store_result_t r = BOSUN_STORE_OK;
    char profile[BOSUN_PROFILE_ID_BYTES]; unsigned bank = 0, slot = 0;
    const char *data = NULL; size_t length = 0;
    observe_changes(p, false, 0);
    strcpy(p->id, "null"); p->type[0] = 0; ++p->requests;
    if (bosun_json_parse(&p->request, p->rx, p->rx_length, p->tokens, BOSUN_PROTOCOL_TOKENS) != BOSUN_JSON_OK ||
        p->request.tokens[0].type != BOSUN_JSON_OBJECT || !unique_arguments(&p->request))
        { error(p, "invalid_json"); goto done; }
    int id = get(p, "id");
    if (id >= 0) {
        uint8_t type = p->request.tokens[id].type;
        if ((type != BOSUN_JSON_STRING && type != BOSUN_JSON_NUMBER && type != BOSUN_JSON_NULL) ||
            !bosun_json_raw(&p->request, id, &data, &length) || length >= sizeof p->id) {
            error(p, "invalid_id"); goto done;
        }
        memcpy(p->id, data, length); p->id[length] = 0;
    }
    if (!text_arg(p, "type", p->type, sizeof p->type, false) || !*p->type ||
        !text_arg(p, "profile", profile, sizeof profile, true)) { error(p, "invalid_request"); goto done; }
    if (*profile && !bosun_config_profile_exists(profile)) { error(p, "no_such_profile"); goto done; }
    begin(p, "ACK");
    if (!strcmp(p->type, "PING")) string(&p->writer, "fw", BOSUN_NATIVE_VERSION);
    else if (!strcmp(p->type, "GET_DEVICE_INFO")) device_info(p);
    else if (!strcmp(p->type, "GET_CONTEXT")) {
        begin(p, "CONTEXT"); field(&p->writer, "context");
        if (!bosun_runtime_context(rt, &p->writer)) p->writer.failed = true;
    } else if (!strcmp(p->type, "GET_MANIFEST")) {
        begin(p, "MANIFEST"); bosun_json_puts(&p->writer, BOSUN_MANIFEST_FIELDS);
    } else if (!strcmp(p->type, "GET_GLOBAL")) {
        begin(p, "GLOBAL"); string(&p->writer, "profile", *profile ? profile : c->profile);
        field(&p->writer, "device"); r = read_value(p, profile, "device.json", 0, 0);
    } else if (!strcmp(p->type, "PUT_GLOBAL")) {
        if (!object_arg(p, "device", &data, &length)) r = BOSUN_STORE_INVALID;
        else r = bosun_config_put_device(c, profile, data, length);
        if (r == BOSUN_STORE_OK && !*profile) p->ui_pending |= UI_GLOBAL;
    } else if (!strcmp(p->type, "LIST_PATCHES")) {
        begin(p, "PATCH_LIST"); string(&p->writer, "profile", *profile ? profile : c->profile);
        field(&p->writer, "patches"); r = bosun_config_patches(c, profile, &p->writer);
    } else if (!strcmp(p->type, "GET_PATCH") || !strcmp(p->type, "PUT_PATCH") ||
               !strcmp(p->type, "PUT_BINDING") || !strcmp(p->type, "SWITCH_PATCH") || !strcmp(p->type, "DELETE_PATCH")) {
        if (!coordinates(p, &bank, &slot, false)) r = BOSUN_STORE_INVALID;
        else if (!strcmp(p->type, "GET_PATCH")) {
            begin(p, "PATCH"); integer(&p->writer, "bank", bank); integer(&p->writer, "slot", slot);
            /* Empty profile identifies the active store to hub cache keys;
             * the active profile id is already available via DEVICE_INFO. */
            string(&p->writer, "profile", *profile ? profile : "");
            field(&p->writer, "patch"); r = read_value(p, profile, NULL, bank, slot);
        } else if (!strcmp(p->type, "PUT_PATCH")) {
            if (!object_arg(p, "patch", &data, &length)) r = BOSUN_STORE_INVALID;
            else r = bosun_config_put_patch(c, profile, bank, slot, data, length, now_ms);
        } else if (*profile) r = BOSUN_STORE_INVALID;
        else if (!strcmp(p->type, "PUT_BINDING")) {
            if (!object_arg(p, "binding", &data, &length)) r = BOSUN_STORE_INVALID;
            else r = bosun_config_put_binding(c, bank, slot, data, length, p->tx, sizeof p->tx, now_ms);
            begin(p, "ACK");
        } else if (!strcmp(p->type, "SWITCH_PATCH")) r = bosun_runtime_switch_patch(rt, bank, slot, true);
        else r = bosun_config_remove_patch(c, bank, slot);
    } else if (!strcmp(p->type, "SAVE_NOW") || !strcmp(p->type, "DISCARD")) {
        if (*profile || !coordinates(p, &bank, &slot, true)) r = BOSUN_STORE_INVALID;
        else {
            bool discard = !strcmp(p->type, "DISCARD");
            if (!discard) { begin(p, "SAVED"); field(&p->writer, "patches"); }
            r = bosun_config_save(c, bank, slot, discard, discard ? NULL : &p->writer);
        }
    } else if (!strcmp(p->type, "GET_DIRTY")) { begin(p, "DIRTY"); dirty(p); }
    else if (!strcmp(p->type, "LIST_PROFILES")) {
        begin(p, "PROFILE_LIST"); string(&p->writer, "active", c->profile);
        field(&p->writer, "profiles"); r = bosun_config_profiles(c, &p->writer);
    } else if (!strcmp(p->type, "CREATE_PROFILE") || !strcmp(p->type, "RENAME_PROFILE") ||
               !strcmp(p->type, "SWITCH_PROFILE") || !strcmp(p->type, "DELETE_PROFILE")) {
        char name[97], kind[40], color[33], target[BOSUN_PROFILE_ID_BYTES];
        if (!text_arg(p, "profile_id", target, sizeof target, false) || !bosun_config_profile_id(target))
            r = BOSUN_STORE_INVALID;
        else if (!strcmp(p->type, "CREATE_PROFILE")) {
            if (!text_arg(p, "name", name, sizeof name, true) || !text_arg(p, "kind", kind, sizeof kind, true) ||
                !text_arg(p, "color", color, sizeof color, true)) r = BOSUN_STORE_INVALID;
            else if (*kind && strcmp(kind, "kemper_player") && strcmp(kind, "generic_midi") && strcmp(kind, "unknown"))
                { error(p, "unsupported_plugin"); goto done; }
            else r = bosun_config_create(target, name, kind, color);
        } else if (!strcmp(p->type, "RENAME_PROFILE")) {
            if (!text_arg(p, "name", name, sizeof name, false)) r = BOSUN_STORE_INVALID;
            else r = bosun_config_rename(target, name);
        } else if (!strcmp(p->type, "SWITCH_PROFILE")) r = bosun_config_activate(c, target, true);
        else r = bosun_config_delete(c, target);
    } else if (!strcmp(p->type, "GET_MIDI_LEARN")) {
        begin(p, "MIDI_LEARN"); string(&p->writer, "profile", *profile ? profile : c->profile);
        field(&p->writer, "table"); r = read_value(p, profile, "midi_learn.json", 0, 0);
    } else if (!strcmp(p->type, "PUT_MIDI_LEARN")) {
        char path[BOSUN_PATH_MAX];
        if (!object_arg(p, "table", &data, &length) ||
            !bosun_config_path(path, sizeof path, *profile ? profile : c->profile, "midi_learn.json")) r = BOSUN_STORE_INVALID;
        else if (!bosun_config_profile_exists(*profile ? profile : c->profile)) r = BOSUN_STORE_NOT_FOUND;
        else if (length > BOSUN_DEVICE_BYTES) r = BOSUN_STORE_LIMIT;
        else r = bosun_store_write_atomic(path, data, length);
    } else if (!strcmp(p->type, "START_MIDI_LEARN") || !strcmp(p->type, "STOP_MIDI_LEARN")) {
        rt->midi_learn = !strcmp(p->type, "START_MIDI_LEARN"); rt->learn.fresh = false;
        p->event_head = p->event_length = 0;
    }
    else if (!strcmp(p->type, "SET_MIDI_MONITOR")) {
        bool on;
        if (!bosun_json_boolean(&p->request, get(p, "on"), &on)) r = BOSUN_STORE_INVALID;
        else {
            if (rt->midi_monitor != on) p->event_head = p->event_length = 0;
            rt->midi_monitor = on; field(&p->writer, "on"); bosun_json_puts(&p->writer, on ? "true" : "false");
        }
    } else if (!strcmp(p->type, "GET_RIG_INFO")) {
        bool want_request = true;
        int token = get(p, "request");
        if (token >= 0 && !bosun_json_boolean(&p->request, token, &want_request)) r = BOSUN_STORE_INVALID;
        else if (!rt->kemper_enabled) { error(p, "no_rig_info"); goto done; }
        else {
            if (want_request) (void)bosun_kemper_request_rig_name(&rt->kemper, now_ms);
            begin(p, "RIG_INFO"); string(&p->writer, "name", rt->kemper.last_name);
            if (rt->kemper.last_name_rig) integer(&p->writer, "rig", rt->kemper.last_name_rig);
            else bosun_json_puts(&p->writer, ",\"rig\":null");
            static const char *const colors[] = {"#3a8eff", "#f5dc34", "#e54848", "#2a2a2a", "#3ecb6e",
                "#3a8eff", "#f5dc34", "#e54848", "#3ecb6e", "#c08aff"};
            unsigned rig = (c->bank - 1u) * 5u + c->slot;
            string(&p->writer, "color", rig >= 1 && rig <= 5 ? colors[rig - 1] :
                rig >= 11 && rig <= 15 ? colors[rig - 6] : "#666666");
            field(&p->writer, "fresh"); bosun_json_puts(&p->writer,
                rt->kemper.state.rig_name_fresh && rt->kemper.state.rig == rig &&
                rt->kemper.last_name_rig == rig ? "true" : "false");
        }
    } else if (!strcmp(p->type, "LED_DUMP")) {
        if (!p->read_led) { error(p, "leds_unavailable"); goto done; }
        static const char *const names[] = {"1", "2", "3", "4", "up", "A", "B", "C", "D", "down"};
        begin(p, "LED_DUMP"); bosun_json_puts(&p->writer, ",\"pixels\":[");
        for (unsigned i = 0; i < 30; ++i) {
            uint32_t rgb = p->read_led((uint8_t)i);
            bosun_json_puts(&p->writer, i ? ",[" : "[");
            bosun_json_write_integer(&p->writer, (int32_t)((rgb >> 16) & 255));
            bosun_json_puts(&p->writer, ",");
            bosun_json_write_integer(&p->writer, (int32_t)((rgb >> 8) & 255));
            bosun_json_puts(&p->writer, ",");
            bosun_json_write_integer(&p->writer, (int32_t)(rgb & 255));
            bosun_json_puts(&p->writer, "]");
        }
        bosun_json_puts(&p->writer, "],\"switch_indices\":{");
        for (unsigned sw = 0; sw < 10; ++sw) {
            if (sw) bosun_json_puts(&p->writer, ",");
            bosun_json_quote(&p->writer, names[sw]); bosun_json_puts(&p->writer, ":[");
            for (unsigned i = 0; i < 3; ++i) {
                if (i) bosun_json_puts(&p->writer, ",");
                /* Bottom row ring order matches Captain board.py. */
                unsigned within = sw >= 5 && i ? 3 - i : i;
                bosun_json_write_integer(&p->writer, (int32_t)(sw * 3 + within));
            }
            bosun_json_puts(&p->writer, "]");
        }
        bosun_json_puts(&p->writer, "},\"current\":{\"bank\":");
        bosun_json_write_integer(&p->writer, c->bank);
        integer(&p->writer, "slot", c->slot); bosun_json_puts(&p->writer, "}");
    } else if (!strcmp(p->type, "LIST_FONTS")) {
        begin(p, "FONT_LIST"); bosun_json_puts(&p->writer, ",\"fonts\":[\"system\"]");
    } else if (!strcmp(p->type, "STATS")) {
        begin(p, "STATS"); string(&p->writer, "fw", BOSUN_NATIVE_VERSION);
        unsigned_integer(&p->writer, "uptime_ms", now_ms); unsigned_integer(&p->writer, "midi_rx_count", rt->midi_rx_count);
        unsigned_integer(&p->writer, "midi_tx_count", rt->midi_tx_count); unsigned_integer(&p->writer, "midi_tx_failed", rt->midi_tx_failed);
        unsigned_integer(&p->writer, "queue_overflows", rt->queue_overflows); unsigned_integer(&p->writer, "unsupported_messages", rt->unsupported_messages);
        unsigned_integer(&p->writer, "invalid_messages", rt->invalid_messages); unsigned_integer(&p->writer, "protocol_errors", p->errors);
        unsigned_integer(&p->writer, "storage_errors", rt->storage_errors);
        unsigned_integer(&p->writer, "midi_events_dropped", p->midi_events_dropped);
        field(&p->writer, "storage_ready"); bosun_json_puts(&p->writer, bosun_store_ready() ? "true" : "false");
    } else if (!strcmp(p->type, "REBOOT")) {
        int mode = get(p, "mode");
        bool bootloader = mode >= 0 && bosun_json_equal(&p->request, mode, "bootloader");
        if (mode >= 0 && !bootloader && !bosun_json_equal(&p->request, mode, "normal"))
            r = BOSUN_STORE_INVALID;
        else {
            p->reboot_bootloader = bootloader;
            p->reboot_requested = true;
        }
    }
    else if (!strncmp(p->type, "PUT_FILE_", 9)) error(p, "unsupported_native_firmware_ota");
    else error(p, "unknown_type");
    if (r != BOSUN_STORE_OK) error(p, store_error(r));
done:
    observe_changes(p, !strcmp(p->type, "DISCARD") || !strcmp(p->type, "DELETE_PATCH"), 1);
    finish(p);
}

void bosun_protocol_init(bosun_protocol_t *p, bosun_runtime_t *runtime) {
    memset(p, 0, sizeof *p); p->runtime = runtime; strcpy(p->id, "null");
    runtime->monitor = monitor; runtime->monitor_context = p;
    runtime->binding_fired = binding_fired; runtime->binding_context = p;
}
void bosun_protocol_session(bosun_protocol_t *p, bool connected) {
    if (p->connected == connected) return;
    p->connected = connected; p->rx_length = p->tx_length = p->tx_offset = 0;
    p->discarding = p->reboot_requested = p->reboot_bootloader = false;
    p->event_head = p->event_length = 0;
    p->context_revision = p->kemper_revision = UINT32_MAX;
    p->runtime->midi_monitor = p->runtime->midi_learn = false;
    p->runtime->learn.fresh = false;
    p->ui_pending = 0; p->ui_observed = false;
    memset(p->binding_pending, 0, sizeof p->binding_pending);
    memset(p->saved_pending, 0, sizeof p->saved_pending);
    memset(p->discarded_pending, 0, sizeof p->discarded_pending);
    observe_changes(p, false, 0);
}
size_t bosun_protocol_feed(bosun_protocol_t *p, const uint8_t *data, size_t length, uint32_t now_ms) {
    if (!p->connected || !data || p->tx_length || p->reboot_requested) return 0;
    size_t consumed = 0;
    while (consumed < length && !p->tx_length) {
        uint8_t byte = data[consumed++]; p->last_rx_ms = now_ms;
        if (byte == '\n') {
            if (p->discarding) {
                p->discarding = false; p->rx_length = 0; strcpy(p->id, "null"); p->type[0] = 0;
                error(p, "request_too_large"); finish(p);
            } else if (p->rx_length) {
                p->rx[p->rx_length] = 0; handle(p, now_ms); p->rx_length = 0;
            }
        } else if (!p->discarding) {
            if (p->rx_length == BOSUN_PROTOCOL_RX_BYTES) { p->discarding = true; p->rx_length = 0; ++p->oversized; }
            else p->rx[p->rx_length++] = (char)byte;
        }
    }
    return consumed;
}
const uint8_t *bosun_protocol_output(const bosun_protocol_t *p, size_t *length) {
    *length = p->tx_length - p->tx_offset; return (const uint8_t *)p->tx + p->tx_offset;
}
void bosun_protocol_consume_output(bosun_protocol_t *p, size_t length) {
    if (length > p->tx_length - p->tx_offset) length = p->tx_length - p->tx_offset;
    p->tx_offset += length;
    if (p->tx_offset == p->tx_length) p->tx_offset = p->tx_length = 0;
}
void bosun_protocol_tick(bosun_protocol_t *p, uint32_t now_ms) {
    if (!p->connected || p->reboot_requested) return;
    observe_changes(p, false, 0);
    if (p->tx_length) return;
    if (p->rx_length && (uint32_t)(now_ms - p->last_rx_ms) >= BOSUN_PROTOCOL_TIMEOUT_MS) {
        p->rx_length = 0; p->discarding = true; ++p->timeouts;
        strcpy(p->id, "null"); p->type[0] = 0; error(p, "receive_timeout"); finish(p); return;
    }
    if (p->rx_length || p->discarding) return;
    if (emit_ui_event(p)) return;
    if ((uint32_t)(now_ms - p->last_context_ms) < 50 ||
        (p->context_revision == p->runtime->revision && p->kemper_revision == p->runtime->kemper.state.revision)) {
        (void)emit_midi_event(p); return;
    }
    strcpy(p->id, "null"); p->type[0] = 0;
    begin(p, "CONTEXT"); field(&p->writer, "context");
    if (!bosun_runtime_context(p->runtime, &p->writer)) p->writer.failed = true;
    finish(p); p->last_context_ms = now_ms;
    p->context_revision = p->runtime->revision; p->kemper_revision = p->runtime->kemper.state.revision;
}
