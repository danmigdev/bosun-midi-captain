#include "bosun/config.h"
#include <stdio.h>
#include <string.h>

/* One cooperative application instance. Workspace is never retained by callers. */
static union {
    char bytes[BOSUN_PATCH_BYTES + 1];
    struct { bosun_dirent_t banks[BOSUN_STORE_LIST_MAX], slots[BOSUN_STORE_LIST_MAX]; } catalog;
    bosun_dirent_t entries[BOSUN_STORE_LIST_MAX];
} scratch;
#define workspace scratch.bytes
static bosun_json_token_t work_tokens[BOSUN_PATCH_TOKENS];
_Static_assert(sizeof work_tokens >= BOSUN_DEVICE_BYTES + 1,
               "activation rollback reuses token workspace for previous device JSON");
const char bosun_default_device[] =
    "{\"version\":1,\"long_press_ms\":600,\"double_tap_window_ms\":250,"
    "\"auto_momentary_on_hold\":true,\"auto_momentary_ms\":500,\"long_press_actions\":{},"
    "\"autosave\":{\"enabled\":false,\"debounce_ms\":2000},\"leds\":{\"brightness\":64,\"dim\":4},"
    "\"tft\":{\"brightness\":80,\"theme_color\":\"#00ff88\",\"rotation\":180,\"rowstart\":80,\"colstart\":0},"
    "\"expression\":[{\"jack\":1,\"enabled\":false,\"invert\":false,\"calibration\":{\"min\":300,\"max\":65200},\"curve\":\"linear\",\"message\":{\"type\":\"cc\",\"channel\":1,\"cc\":11,\"value\":0}},"
    "{\"jack\":2,\"enabled\":false,\"invert\":false,\"calibration\":{\"min\":300,\"max\":65200},\"curve\":\"linear\",\"message\":{\"type\":\"cc\",\"channel\":1,\"cc\":11,\"value\":0}}]}";

int32_t bosun_config_int(const bosun_json_doc_t *d, int object, const char *key, int32_t fallback) {
    int32_t n; return bosun_json_integer(d, bosun_json_get(d, object, key), &n) ? n : fallback;
}
bool bosun_config_bool(const bosun_json_doc_t *d, int object, const char *key, bool fallback) {
    bool b; return bosun_json_boolean(d, bosun_json_get(d, object, key), &b) ? b : fallback;
}
bool bosun_config_profile_id(const char *id) {
    if (!id || !*id || strlen(id) >= BOSUN_PROFILE_ID_BYTES) return false;
    for (const unsigned char *p = (const unsigned char *)id; *p; ++p)
        if (!((*p >= 'a' && *p <= 'z') || (*p >= 'A' && *p <= 'Z') ||
              (*p >= '0' && *p <= '9') || *p == '_' || *p == '-' || *p >= 128)) return false;
    char quoted[BOSUN_PROFILE_ID_BYTES + 2];
    size_t length = strlen(id);
    quoted[0] = '"'; memcpy(quoted + 1, id, length); quoted[length + 1] = '"';
    bosun_json_token_t token; bosun_json_doc_t document;
    return bosun_json_parse(&document, quoted, length + 2, &token, 1) == BOSUN_JSON_OK;
}
bool bosun_config_coordinates(unsigned bank, unsigned slot) { return bank >= 1 && bank <= 99 && slot >= 1 && slot <= 10; }
bool bosun_config_path(char *out, size_t capacity, const char *profile, const char *file) {
    if (!out || !capacity || !bosun_config_profile_id(profile) || !file || !*file) return false;
    int n = snprintf(out, capacity, "/config/profiles/%s/%s", profile, file);
    return n > 0 && (size_t)n < capacity && bosun_store_safe_path(out);
}
bool bosun_config_patch_path(char *out, size_t capacity, const char *profile,
                             unsigned bank, unsigned slot, bool draft) {
    char suffix[48];
    if (!bosun_config_coordinates(bank, slot)) return false;
    snprintf(suffix, sizeof suffix, "%s/%02u/%02u.json", draft ? "native-drafts" : "patches", bank, slot);
    return bosun_config_path(out, capacity, profile, suffix);
}
static bosun_store_result_t object(const char *json, size_t length, bosun_json_doc_t *d,
                                   bosun_json_token_t *tokens, uint16_t count) {
    bosun_json_result_t r = bosun_json_parse(d, json, length, tokens, count);
    if (r == BOSUN_JSON_LIMIT) return BOSUN_STORE_LIMIT;
    return r == BOSUN_JSON_OK && d->tokens[0].type == BOSUN_JSON_OBJECT ? BOSUN_STORE_OK : BOSUN_STORE_INVALID;
}
static bosun_store_result_t read_object(const char *path, char *out, size_t capacity,
                                       bosun_json_doc_t *d, bosun_json_token_t *tokens, uint16_t count) {
    size_t length = 0;
    bosun_store_result_t r = bosun_store_read(path, out, capacity - 1, &length);
    if (r != BOSUN_STORE_OK) return r;
    out[length] = 0;
    return object(out, length, d, tokens, count);
}
static bool raw(bosun_json_writer_t *w, const bosun_json_doc_t *d, int token) {
    const char *s; size_t length;
    return bosun_json_raw(d, token, &s, &length) && bosun_json_write(w, s, length);
}
static bosun_store_result_t write_file(const char *path, const char *data, size_t length) {
    char parent[BOSUN_PATH_MAX];
    size_t n = strlen(path);
    if (n >= sizeof parent) return BOSUN_STORE_INVALID;
    memcpy(parent, path, n + 1);
    char *slash = strrchr(parent, '/');
    if (!slash || slash == parent) return BOSUN_STORE_INVALID;
    *slash = 0;
    bosun_store_result_t r = bosun_store_mkdir(parent);
    return r == BOSUN_STORE_OK ? bosun_store_write_atomic(path, data, length) : r;
}
bool bosun_config_profile_exists(const char *profile) {
    char path[BOSUN_PATH_MAX], byte; size_t length;
    return bosun_config_path(path, sizeof path, profile, "manifest.json") &&
        bosun_store_read_at(path, 0, &byte, 1, &length) == BOSUN_STORE_OK && length != 0;
}
static int dirty_index(const bosun_config_t *c, unsigned bank, unsigned slot) {
    for (unsigned i = 0; i < c->dirty_count; ++i)
        if (c->dirty[i].bank == bank && c->dirty[i].slot == slot) return (int)i;
    return -1;
}
bool bosun_config_dirty(const bosun_config_t *c, unsigned bank, unsigned slot) { return dirty_index(c, bank, slot) >= 0; }
static void default_device(bosun_config_t *c) {
    memcpy(c->device, bosun_default_device, sizeof bosun_default_device);
    (void)object(c->device, sizeof bosun_default_device - 1, &c->device_doc, c->device_tokens, BOSUN_DEVICE_TOKENS);
}
static bosun_store_result_t directories(const char *profile) {
    char path[BOSUN_PATH_MAX];
    if (!bosun_config_path(path, sizeof path, profile, "patches")) return BOSUN_STORE_INVALID;
    return bosun_store_mkdir(path);
}
bosun_store_result_t bosun_config_create(const char *profile, const char *name, const char *kind, const char *color) {
    char path[BOSUN_PATH_MAX]; bosun_json_writer_t w;
    if (!bosun_config_profile_id(profile) || !name || !kind || strlen(name) > 96 || strlen(kind) > 39) return BOSUN_STORE_INVALID;
    if (bosun_config_profile_exists(profile)) return BOSUN_STORE_INVALID;
    size_t count = 0, profiles = 0;
    bosun_store_result_t r = bosun_store_list("/config/profiles", scratch.entries, BOSUN_STORE_LIST_MAX, &count);
    if (r != BOSUN_STORE_OK && r != BOSUN_STORE_NOT_FOUND) return r;
    for (size_t i = 0; i < count; ++i)
        if (scratch.entries[i].directory && bosun_config_profile_exists(scratch.entries[i].name)) ++profiles;
    if (profiles >= BOSUN_PROFILE_MAX) return BOSUN_STORE_LIMIT;
    bosun_json_writer_init(&w, workspace, sizeof workspace);
    bosun_json_puts(&w, "{\"name\":"); bosun_json_quote(&w, *name ? name : profile);
    bosun_json_puts(&w, ",\"kind\":"); bosun_json_quote(&w, *kind ? kind : "unknown");
    if (color && *color) { bosun_json_puts(&w, ",\"color\":"); bosun_json_quote(&w, color); }
    bosun_json_puts(&w, "}");
    if (w.failed) return BOSUN_STORE_LIMIT;
    bosun_json_doc_t d;
    r = object(workspace, w.length, &d, work_tokens, BOSUN_PATCH_TOKENS);
    if (r != BOSUN_STORE_OK) return r;
    r = directories(profile);
    if (r != BOSUN_STORE_OK) return r;
    bosun_config_path(path, sizeof path, profile, "device.json");
    r = bosun_store_write_atomic(path, bosun_default_device, sizeof bosun_default_device - 1);
    if (r != BOSUN_STORE_OK) return r;
    bosun_config_path(path, sizeof path, profile, "midi_learn.json");
    r = bosun_store_write_atomic(path, "{\"pc_to_patch\":[]}", 18);
    if (r != BOSUN_STORE_OK) return r;
    /* Publishing the manifest last keeps a partially created profile hidden. */
    bosun_config_path(path, sizeof path, profile, "manifest.json");
    return bosun_store_write_atomic(path, workspace, w.length);
}
bosun_store_result_t bosun_config_rename(const char *profile, const char *name) {
    char path[BOSUN_PATH_MAX], previous[2048]; bosun_json_doc_t d;
    if (!name || strlen(name) > 96 || !bosun_config_path(path, sizeof path, profile, "manifest.json")) return BOSUN_STORE_INVALID;
    bosun_store_result_t r = read_object(path, previous, sizeof previous, &d, work_tokens, 256);
    if (r != BOSUN_STORE_OK) return r;
    bosun_json_writer_t w; bosun_json_writer_init(&w, workspace, sizeof workspace);
    bosun_json_puts(&w, "{");
    for (unsigned i = 1; i < d.tokens[0].next; i = d.tokens[i + 1].next) {
        if (bosun_json_equal(&d, (int)i, "name")) continue;
        raw(&w, &d, (int)i); bosun_json_puts(&w, ":"); raw(&w, &d, (int)i + 1); bosun_json_puts(&w, ",");
    }
    bosun_json_puts(&w, "\"name\":"); bosun_json_quote(&w, name); bosun_json_puts(&w, "}");
    if (w.failed) return BOSUN_STORE_LIMIT;
    r = object(workspace, w.length, &d, work_tokens, BOSUN_PATCH_TOKENS);
    return r == BOSUN_STORE_OK ? bosun_store_write_atomic(path, workspace, w.length) : r;
}
bosun_store_result_t bosun_config_read(const bosun_config_t *c, const char *profile,
    const char *file, char *out, size_t capacity, size_t *length) {
    if (length) *length = 0;
    if (!c || !file || !length || !out || !capacity) return BOSUN_STORE_INVALID;
    const char *id = profile && *profile ? profile : c->profile; char path[BOSUN_PATH_MAX];
    const char *fallback = !strcmp(file, "device.json") ? bosun_default_device :
        !strcmp(file, "midi_learn.json") ? "{\"pc_to_patch\":[]}" : NULL;
    const char *memory = (!profile || !*profile) && !strcmp(file, "device.json") ? c->device : NULL;
    if (memory || (!*id && fallback)) {
        const char *value = memory ? memory : fallback;
        size_t n = strlen(value);
        if (capacity <= n) return BOSUN_STORE_LIMIT;
        memcpy(out, value, n + 1); *length = n; return BOSUN_STORE_OK;
    }
    if (!bosun_config_profile_exists(id)) return BOSUN_STORE_NOT_FOUND;
    if (!bosun_config_path(path, sizeof path, id, file) || !capacity) return BOSUN_STORE_INVALID;
    bosun_store_result_t r = bosun_store_read(path, out, capacity - 1, length);
    if (r == BOSUN_STORE_NOT_FOUND && fallback) {
        size_t n = strlen(fallback);
        if (capacity <= n) return BOSUN_STORE_LIMIT;
        memcpy(out, fallback, n + 1); *length = n; return BOSUN_STORE_OK;
    }
    if (r == BOSUN_STORE_OK) out[*length] = 0;
    return r;
}
bosun_store_result_t bosun_config_read_patch(const bosun_config_t *c, const char *profile,
    unsigned bank, unsigned slot, char *out, size_t capacity, size_t *length) {
    if (length) *length = 0;
    if (!c || !length || !out || !capacity) return BOSUN_STORE_INVALID;
    const char *id = profile && *profile ? profile : c->profile;
    bool draft = (!profile || !*profile) && bosun_config_dirty(c, bank, slot);
    char path[BOSUN_PATH_MAX];
    if (!capacity || !bosun_config_patch_path(path, sizeof path, id, bank, slot, draft)) return BOSUN_STORE_INVALID;
    bosun_store_result_t r = bosun_store_read(path, out, capacity - 1, length);
    if (r == BOSUN_STORE_OK) out[*length] = 0;
    return r;
}
bosun_store_result_t bosun_config_select_checked(bosun_config_t *c, unsigned bank,
    unsigned slot, bosun_config_validate_patch_fn validate, void *context) {
    size_t length; bosun_json_doc_t d;
    bosun_store_result_t r = bosun_config_read_patch(c, NULL, bank, slot, workspace, sizeof workspace, &length);
    if (r == BOSUN_STORE_OK) r = object(workspace, length, &d, work_tokens, BOSUN_PATCH_TOKENS);
    if (r != BOSUN_STORE_OK) return r;
    if (validate && !validate(&d, context)) return BOSUN_STORE_INVALID;
    memcpy(c->patch, workspace, length + 1);
    (void)object(c->patch, length, &c->patch_doc, c->patch_tokens, BOSUN_PATCH_TOKENS);
    c->bank = (uint16_t)bank; c->slot = (uint16_t)slot; c->has_patch = true; ++c->patch_revision; ++c->revision;
    return BOSUN_STORE_OK;
}
bosun_store_result_t bosun_config_select(bosun_config_t *c, unsigned bank, unsigned slot) {
    return bosun_config_select_checked(c, bank, slot, NULL, NULL);
}
bosun_store_result_t bosun_config_put_device(bosun_config_t *c, const char *profile, const char *json, size_t length) {
    char path[BOSUN_PATH_MAX]; bosun_json_doc_t d;
    const char *id = profile && *profile ? profile : c->profile;
    if (length > BOSUN_DEVICE_BYTES) return BOSUN_STORE_LIMIT;
    if (!bosun_config_profile_exists(id)) return BOSUN_STORE_NOT_FOUND;
    bosun_store_result_t r = object(json, length, &d, work_tokens, BOSUN_DEVICE_TOKENS);
    if (r != BOSUN_STORE_OK) return r;
    bosun_config_path(path, sizeof path, id, "device.json");
    r = write_file(path, json, length);
    if (r != BOSUN_STORE_OK) return r;
    if (!profile || !*profile) {
        memmove(c->device, json, length); c->device[length] = 0;
        (void)object(c->device, length, &c->device_doc, c->device_tokens, BOSUN_DEVICE_TOKENS); ++c->revision;
    }
    return BOSUN_STORE_OK;
}
bosun_store_result_t bosun_config_put_patch(bosun_config_t *c, const char *profile,
    unsigned bank, unsigned slot, const char *json, size_t length, uint32_t now_ms) {
    const char *id = profile && *profile ? profile : c->profile; char path[BOSUN_PATH_MAX]; bosun_json_doc_t d;
    bool active = !profile || !*profile;
    if (length > BOSUN_PATCH_BYTES) return BOSUN_STORE_LIMIT;
    if (!bosun_config_profile_exists(id)) return BOSUN_STORE_NOT_FOUND;
    if (!bosun_config_patch_path(path, sizeof path, id, bank, slot, active)) return BOSUN_STORE_INVALID;
    int index = active ? dirty_index(c, bank, slot) : -1;
    if (active && index < 0 && c->dirty_count == BOSUN_DIRTY_PATCHES) return BOSUN_STORE_LIMIT;
    bosun_store_result_t r = object(json, length, &d, work_tokens, BOSUN_PATCH_TOKENS);
    if (r != BOSUN_STORE_OK) return r;
    r = write_file(path, json, length);
    if (r != BOSUN_STORE_OK) return r;
    if (active) {
        if (index < 0) index = c->dirty_count++;
        c->dirty[index] = (bosun_dirty_patch_t){(uint16_t)bank, (uint16_t)slot, now_ms};
        if (c->bank == bank && c->slot == slot) {
            memmove(c->patch, json, length); c->patch[length] = 0;
            (void)object(c->patch, length, &c->patch_doc, c->patch_tokens, BOSUN_PATCH_TOKENS);
            c->has_patch = true; ++c->patch_revision;
        }
        ++c->revision;
    }
    return BOSUN_STORE_OK;
}
static bool overlaps(const void *a, size_t na, const void *b, size_t nb) {
    uintptr_t x = (uintptr_t)a, y = (uintptr_t)b;
    if (na > UINTPTR_MAX - x || nb > UINTPTR_MAX - y) return true;
    return x < y + nb && y < x + na;
}
bosun_store_result_t bosun_config_put_binding(bosun_config_t *c, unsigned bank, unsigned slot,
    const char *binding, size_t length, char *output, size_t capacity, uint32_t now_ms) {
    if (!c || !binding || !output || !capacity || !bosun_config_coordinates(bank, slot) ||
        overlaps(binding, length, output, capacity) || overlaps(c, sizeof *c, output, capacity))
        return BOSUN_STORE_INVALID;
    if (length > BOSUN_BINDING_BYTES) return BOSUN_STORE_LIMIT;
    bosun_json_doc_t d;
    bosun_store_result_t r = object(binding, length, &d, work_tokens, 512);
    if (r != BOSUN_STORE_OK) return r;
    char target_switch[BOSUN_NAME_MAX];
    if (!bosun_json_string(&d, bosun_json_get(&d, 0, "switch"), target_switch, sizeof target_switch) ||
        !*target_switch) return BOSUN_STORE_INVALID;
    /* Binding validation is complete. Reuse the same tokens for the old patch,
     * retaining only its target switch, rather than adding 6 KiB to the stack. */
    size_t old_length = 0;
    r = bosun_config_read_patch(c, NULL, bank, slot, workspace, sizeof workspace, &old_length);
    if (r == BOSUN_STORE_OK) r = object(workspace, old_length, &d, work_tokens, BOSUN_PATCH_TOKENS);
    if (r != BOSUN_STORE_OK) return r;
    int bindings = bosun_json_get(&d, 0, "bindings");
    if (bindings >= 0 && d.tokens[bindings].type != BOSUN_JSON_ARRAY) return BOSUN_STORE_INVALID;
    bosun_json_writer_t w;
    bosun_json_writer_init(&w, output, capacity > BOSUN_PATCH_BYTES + 1 ? BOSUN_PATCH_BYTES + 1 : capacity);
    bosun_json_puts(&w, "{");
    for (unsigned i = 1; i < d.tokens[0].next; i = d.tokens[i + 1].next) {
        if (bosun_json_equal(&d, (int)i, "bindings")) continue;
        raw(&w, &d, (int)i); bosun_json_puts(&w, ":"); raw(&w, &d, (int)i + 1); bosun_json_puts(&w, ",");
    }
    bosun_json_puts(&w, "\"bindings\":[");
    bool first = true, replaced = false;
    if (bindings >= 0) for (unsigned i = (unsigned)bindings + 1; i < d.tokens[bindings].next; i = d.tokens[i].next) {
        if (d.tokens[i].type != BOSUN_JSON_OBJECT) return BOSUN_STORE_INVALID;
        if (!first) bosun_json_puts(&w, ",");
        first = false;
        if (!replaced && bosun_json_equal(&d, bosun_json_get(&d, (int)i, "switch"), target_switch)) {
            bosun_json_write(&w, binding, length); replaced = true;
        } else raw(&w, &d, (int)i);
    }
    if (!replaced) {
        if (!first) bosun_json_puts(&w, ",");
        bosun_json_write(&w, binding, length);
    }
    bosun_json_puts(&w, "]}");
    return w.failed ? BOSUN_STORE_LIMIT : bosun_config_put_patch(c, NULL, bank, slot, output, w.length, now_ms);
}
static void erase_dirty(bosun_config_t *c, unsigned index) {
    --c->dirty_count;
    memmove(c->dirty + index, c->dirty + index + 1, (c->dirty_count - index) * sizeof c->dirty[0]);
}
bosun_store_result_t bosun_config_save(bosun_config_t *c, unsigned bank, unsigned slot,
                                      bool discard, bosun_json_writer_t *saved) {
    if ((bank || slot) && !bosun_config_coordinates(bank, slot)) return BOSUN_STORE_INVALID;
    /* Reserve the complete response before the first disk mutation. A bounded
     * writer failure must not report LIMIT after silently committing edits. */
    if (saved) {
        bosun_json_puts(saved, "[");
        bool first = true;
        for (unsigned i = 0; i < c->dirty_count; ++i) {
            bosun_dirty_patch_t key = c->dirty[i];
            if ((bank || slot) && (bank != key.bank || slot != key.slot)) continue;
            if (!first) bosun_json_puts(saved, ",");
            first = false;
            bosun_json_puts(saved, "{\"bank\":"); bosun_json_write_integer(saved, key.bank);
            bosun_json_puts(saved, ",\"slot\":"); bosun_json_write_integer(saved, key.slot); bosun_json_puts(saved, "}");
        }
        bosun_json_puts(saved, "]");
        if (saved->failed) return BOSUN_STORE_LIMIT;
    }
    for (unsigned i = 0; i < c->dirty_count;) {
        bosun_dirty_patch_t key = c->dirty[i];
        if ((bank || slot) && (bank != key.bank || slot != key.slot)) { ++i; continue; }
        char draft[BOSUN_PATH_MAX], target[BOSUN_PATH_MAX]; size_t length = 0;
        bosun_config_patch_path(draft, sizeof draft, c->profile, key.bank, key.slot, true);
        bosun_config_patch_path(target, sizeof target, c->profile, key.bank, key.slot, false);
        bosun_store_result_t r = BOSUN_STORE_OK;
        bool reload = discard && c->bank == key.bank && c->slot == key.slot;
        bool has_baseline = false;
        bosun_json_doc_t d;
        if (reload) {
            r = read_object(target, workspace, sizeof workspace, &d, work_tokens, BOSUN_PATCH_TOKENS);
            if (r == BOSUN_STORE_OK) { length = d.length; has_baseline = true; }
            else if (r != BOSUN_STORE_NOT_FOUND) return r;
            r = BOSUN_STORE_OK;
        }
        if (!discard) {
            r = bosun_store_read(draft, workspace, sizeof workspace - 1, &length);
            if (r == BOSUN_STORE_OK) r = object(workspace, length, &d, work_tokens, BOSUN_PATCH_TOKENS);
            if (r == BOSUN_STORE_OK) r = write_file(target, workspace, length);
            if (r != BOSUN_STORE_OK) return r;
        }
        if (r == BOSUN_STORE_OK) r = bosun_store_remove(draft);
        if (r != BOSUN_STORE_OK && r != BOSUN_STORE_NOT_FOUND) return r;
        erase_dirty(c, i); ++c->revision;
        if (reload) {
            if (has_baseline) {
                memcpy(c->patch, workspace, length); c->patch[length] = 0;
                (void)object(c->patch, length, &c->patch_doc, c->patch_tokens, BOSUN_PATCH_TOKENS);
            } else {
                c->patch[0] = 0;
                (void)object(c->patch, 0, &c->patch_doc, c->patch_tokens, BOSUN_PATCH_TOKENS);
            }
            c->has_patch = has_baseline; ++c->patch_revision;
        }
    }
    return BOSUN_STORE_OK;
}
bosun_store_result_t bosun_config_remove_patch(bosun_config_t *c, unsigned bank, unsigned slot) {
    char path[BOSUN_PATH_MAX];
    if (!bosun_config_patch_path(path, sizeof path, c->profile, bank, slot, false)) return BOSUN_STORE_INVALID;
    bosun_store_result_t r = bosun_store_remove(path);
    if (r != BOSUN_STORE_OK && r != BOSUN_STORE_NOT_FOUND) return r;
    r = bosun_config_save(c, bank, slot, true, NULL);
    if (r != BOSUN_STORE_OK) return r;
    if (c->bank == bank && c->slot == slot) {
        c->patch[0] = 0;
        (void)object(c->patch, 0, &c->patch_doc, c->patch_tokens, BOSUN_PATCH_TOKENS);
        c->has_patch = false; ++c->patch_revision;
    }
    ++c->revision; return BOSUN_STORE_OK;
}
void bosun_config_tick(bosun_config_t *c, uint32_t now) {
    int a = bosun_json_get(&c->device_doc, 0, "autosave");
    if (!bosun_config_bool(&c->device_doc, a, "enabled", false)) return;
    int32_t delay = bosun_config_int(&c->device_doc, a, "debounce_ms", 2000);
    if (delay < 0 || delay > 60000) delay = 2000;
    for (unsigned i = 0; i < c->dirty_count; ++i)
        if ((uint32_t)(now - c->dirty[i].modified_ms) >= (uint32_t)delay) {
            c->last_error = bosun_config_save(c, c->dirty[i].bank, c->dirty[i].slot, false, NULL); break;
        }
}
static unsigned number_name(const char *name, bool file) {
    unsigned n = 0, i = 0;
    while (name[i] >= '0' && name[i] <= '9') {
        n = n * 10u + (unsigned)(name[i++] - '0');
        if (i > 2 || n > 99) return 0;
    }
    return i && (file ? !strcmp(name + i, ".json") : name[i] == 0) ? n : 0;
}
static bosun_store_result_t catalog(const char *profile, bool drafts,
                                    bosun_patch_key_t *keys, size_t capacity, size_t *count) {
    bosun_dirent_t *banks = scratch.catalog.banks, *slots = scratch.catalog.slots;
    size_t nb = 0, ns = 0; char path[BOSUN_PATH_MAX], sub[BOSUN_PATH_MAX];
    *count = 0;
    if (!bosun_config_path(path, sizeof path, profile, drafts ? "native-drafts" : "patches")) return BOSUN_STORE_INVALID;
    bosun_store_result_t r = bosun_store_list(path, banks, BOSUN_STORE_LIST_MAX, &nb);
    if (r == BOSUN_STORE_NOT_FOUND) return BOSUN_STORE_OK;
    if (r != BOSUN_STORE_OK) return r;
    for (size_t b = 0; b < nb; ++b) {
        unsigned bank = number_name(banks[b].name, false);
        if (!banks[b].directory || !bank) continue;
        int n = snprintf(sub, sizeof sub, "%s/%s", path, banks[b].name);
        if (n < 0 || (size_t)n >= sizeof sub) return BOSUN_STORE_LIMIT;
        r = bosun_store_list(sub, slots, BOSUN_STORE_LIST_MAX, &ns);
        if (r != BOSUN_STORE_OK) return r;
        for (size_t s = 0; s < ns; ++s) {
            unsigned slot = number_name(slots[s].name, true);
            if (slots[s].directory || !bosun_config_coordinates(bank, slot)) continue;
            if (*count == capacity) return BOSUN_STORE_LIMIT;
            keys[(*count)++] = (bosun_patch_key_t){(uint16_t)bank, (uint16_t)slot};
        }
    }
    return BOSUN_STORE_OK;
}
bosun_store_result_t bosun_config_activate(bosun_config_t *c, const char *profile, bool persist) {
    char path[BOSUN_PATH_MAX], kind[40]; bosun_json_doc_t d;
    if (!bosun_config_path(path, sizeof path, profile, "manifest.json")) return BOSUN_STORE_INVALID;
    bosun_store_result_t r = read_object(path, workspace, sizeof workspace, &d, work_tokens, BOSUN_PATCH_TOKENS);
    if (r != BOSUN_STORE_OK) return r;
    if (!bosun_json_string(&d, bosun_json_get(&d, 0, "kind"), kind, sizeof kind)) strcpy(kind, "unknown");
    bosun_patch_key_t draft_keys[BOSUN_DIRTY_PATCHES]; size_t count = 0;
    r = catalog(profile, true, draft_keys, BOSUN_DIRTY_PATCHES, &count);
    if (r != BOSUN_STORE_OK) return r;
    /* Keep the old raw documents in the two existing workspaces. Future
     * documents are read and validated in-place; failures restore both old
     * documents/tokens before the cooperative API returns. No second 40 KiB
     * config buffer, deep stack frame or partially published profile. */
    size_t old_device_length = c->device_doc.length, old_patch_length = c->patch_doc.length;
    bool old_patch_doc_valid = c->patch_doc.count != 0;
    memcpy(work_tokens, c->device, old_device_length + 1);
    memcpy(workspace, c->patch, old_patch_length + 1);
    bosun_config_path(path, sizeof path, profile, "device.json");
    r = read_object(path, c->device, sizeof c->device, &c->device_doc, c->device_tokens, BOSUN_DEVICE_TOKENS);
    if (r == BOSUN_STORE_NOT_FOUND) {
        default_device(c); r = BOSUN_STORE_OK;
    }
    bool draft = false, has_patch = false;
    for (size_t i = 0; i < count; ++i) if (draft_keys[i].bank == 1 && draft_keys[i].slot == 1) draft = true;
    if (r == BOSUN_STORE_OK) {
        bosun_config_patch_path(path, sizeof path, profile, 1, 1, draft);
        r = read_object(path, c->patch, sizeof c->patch, &c->patch_doc, c->patch_tokens, BOSUN_PATCH_TOKENS);
        has_patch = r == BOSUN_STORE_OK;
        if (r == BOSUN_STORE_NOT_FOUND && !draft) {
            c->patch[0] = 0;
            (void)object(c->patch, 0, &c->patch_doc, c->patch_tokens, BOSUN_PATCH_TOKENS);
            r = BOSUN_STORE_OK;
        }
    }
    if (r != BOSUN_STORE_OK) goto rollback;
    if (persist) {
        char pointer[256]; bosun_json_writer_t w; bosun_json_writer_init(&w, pointer, sizeof pointer);
        bosun_json_puts(&w, "{\"id\":"); bosun_json_quote(&w, profile); bosun_json_puts(&w, "}");
        if (w.failed) { r = BOSUN_STORE_LIMIT; goto rollback; }
        r = write_file("/config/active_profile.json", pointer, w.length);
        if (r != BOSUN_STORE_OK) goto rollback;
    }
    memmove(c->profile, profile, strlen(profile) + 1); strcpy(c->kind, kind);
    c->dirty_count = (uint16_t)count; c->has_patch = has_patch; c->bank = 1; c->slot = 1;
    for (size_t i = 0; i < count; ++i)
        c->dirty[i] = (bosun_dirty_patch_t){draft_keys[i].bank, draft_keys[i].slot, 0};
    ++c->revision; ++c->patch_revision;
    c->last_error = BOSUN_STORE_OK;
    return BOSUN_STORE_OK;
rollback:
    memcpy(c->device, work_tokens, old_device_length + 1);
    memcpy(c->patch, workspace, old_patch_length + 1);
    (void)object(c->device, old_device_length, &c->device_doc, c->device_tokens, BOSUN_DEVICE_TOKENS);
    (void)object(c->patch, old_patch_doc_valid ? old_patch_length : 0, &c->patch_doc, c->patch_tokens, BOSUN_PATCH_TOKENS);
    return r;
}
bosun_store_result_t bosun_config_init(bosun_config_t *c) {
    memset(c, 0, sizeof *c); c->bank = 1; c->slot = 1; default_device(c);
    if (!bosun_store_ready()) return c->last_error = BOSUN_STORE_UNAVAILABLE;
    bosun_json_doc_t d;
    bosun_store_result_t r = read_object("/config/active_profile.json", workspace, sizeof workspace, &d, work_tokens, BOSUN_PATCH_TOKENS);
    if (r == BOSUN_STORE_NOT_FOUND) return BOSUN_STORE_OK;
    if (r != BOSUN_STORE_OK) return c->last_error = r;
    char id[BOSUN_PROFILE_ID_BYTES];
    if (!bosun_json_string(&d, bosun_json_get(&d, 0, "id"), id, sizeof id)) return c->last_error = BOSUN_STORE_INVALID;
    if (!*id) return BOSUN_STORE_OK;
    return c->last_error = bosun_config_activate(c, id, false);
}
bosun_store_result_t bosun_config_profiles(const bosun_config_t *c, bosun_json_writer_t *w) {
    char ids[BOSUN_PROFILE_MAX][BOSUN_PROFILE_ID_BYTES];
    size_t count = 0, profiles = 0; bool first = true; char path[BOSUN_PATH_MAX];
    bosun_store_result_t r = bosun_store_list("/config/profiles", scratch.entries, BOSUN_STORE_LIST_MAX, &count);
    if (r != BOSUN_STORE_OK && r != BOSUN_STORE_NOT_FOUND) return r;
    for (size_t i = 0; i < count; ++i) {
        if (!scratch.entries[i].directory || !bosun_config_profile_id(scratch.entries[i].name) ||
            !bosun_config_profile_exists(scratch.entries[i].name)) continue;
        if (profiles == BOSUN_PROFILE_MAX) return BOSUN_STORE_LIMIT;
        strcpy(ids[profiles++], scratch.entries[i].name);
    }
    bosun_json_puts(w, "[");
    for (size_t i = 0; i < profiles; ++i) {
        bosun_config_path(path, sizeof path, ids[i], "manifest.json"); bosun_json_doc_t d;
        r = read_object(path, workspace, sizeof workspace, &d, work_tokens, BOSUN_PATCH_TOKENS);
        if (r == BOSUN_STORE_NOT_FOUND) continue;
        if (r != BOSUN_STORE_OK) return r;
        if (!first) bosun_json_puts(w, ",");
        first = false;
        bosun_json_puts(w, "{\"id\":"); bosun_json_quote(w, ids[i]);
        bosun_json_puts(w, ",\"name\":");
        if (!raw(w, &d, bosun_json_get(&d, 0, "name"))) bosun_json_quote(w, ids[i]);
        bosun_json_puts(w, ",\"kind\":");
        if (!raw(w, &d, bosun_json_get(&d, 0, "kind"))) bosun_json_quote(w, "unknown");
        bosun_json_puts(w, ",\"color\":");
        if (!raw(w, &d, bosun_json_get(&d, 0, "color"))) bosun_json_puts(w, "null");
        bosun_json_puts(w, ",\"active\":"); bosun_json_puts(w, !strcmp(ids[i], c->profile) ? "true}" : "false}");
    }
    bosun_json_puts(w, "]");
    return w->failed ? BOSUN_STORE_LIMIT : BOSUN_STORE_OK;
}
static void sort_keys(bosun_patch_key_t *keys, size_t count) {
    for (size_t i = 1; i < count; ++i) {
        bosun_patch_key_t key = keys[i]; size_t k = i;
        while (k && (keys[k - 1].bank * 16u + keys[k - 1].slot > key.bank * 16u + key.slot)) {
            keys[k] = keys[k - 1]; --k;
        }
        keys[k] = key;
    }
}
bosun_store_result_t bosun_config_coordinates_list(const bosun_config_t *c,
    bosun_patch_key_t *keys, size_t capacity, size_t *count) {
    if (count) *count = 0;
    if (!c || !count || (!keys && capacity)) return BOSUN_STORE_INVALID;
    if (capacity > BOSUN_PATCH_CATALOG_MAX) return BOSUN_STORE_LIMIT;
    if (!*c->profile) return BOSUN_STORE_OK;
    bosun_store_result_t r = catalog(c->profile, false, keys, capacity, count);
    if (r != BOSUN_STORE_OK) { *count = 0; return r; }
    for (unsigned i = 0; i < c->dirty_count; ++i) {
        bool found = false;
        for (size_t k = 0; k < *count; ++k) if (keys[k].bank == c->dirty[i].bank && keys[k].slot == c->dirty[i].slot) found = true;
        if (!found) {
            if (*count == capacity) { *count = 0; return BOSUN_STORE_LIMIT; }
            keys[(*count)++] = (bosun_patch_key_t){c->dirty[i].bank, c->dirty[i].slot};
        }
    }
    sort_keys(keys, *count);
    return BOSUN_STORE_OK;
}
bool bosun_config_has_patch(const bosun_config_t *c, unsigned bank, unsigned slot) {
    char path[BOSUN_PATH_MAX], byte; size_t length = 0;
    return c && bosun_config_patch_path(path, sizeof path, c->profile, bank, slot, bosun_config_dirty(c, bank, slot)) &&
        bosun_store_read_at(path, 0, &byte, 1, &length) == BOSUN_STORE_OK && length != 0;
}
bosun_store_result_t bosun_config_patch_name(const bosun_config_t *c, unsigned bank,
    unsigned slot, char *output, size_t capacity) {
    if (!output || !capacity) return BOSUN_STORE_INVALID;
    output[0] = 0;
    size_t length = 0; bosun_json_doc_t d;
    bosun_store_result_t r = bosun_config_read_patch(c, NULL, bank, slot, workspace, sizeof workspace, &length);
    if (r == BOSUN_STORE_OK) r = object(workspace, length, &d, work_tokens, BOSUN_PATCH_TOKENS);
    if (r != BOSUN_STORE_OK) return r;
    int name = bosun_json_get(&d, 0, "name");
    if (name < 0) return BOSUN_STORE_OK;
    if (d.tokens[name].type != BOSUN_JSON_STRING) return BOSUN_STORE_INVALID;
    return bosun_json_string(&d, name, output, capacity) ? BOSUN_STORE_OK : BOSUN_STORE_LIMIT;
}
bosun_store_result_t bosun_config_patches(const bosun_config_t *c, const char *profile, bosun_json_writer_t *w) {
    bosun_patch_key_t keys[BOSUN_PATCH_CATALOG_MAX]; size_t count = 0;
    const char *id = profile && *profile ? profile : c->profile;
    if (!*id) { bosun_json_puts(w, "[]"); return w->failed ? BOSUN_STORE_LIMIT : BOSUN_STORE_OK; }
    if (!bosun_config_profile_exists(id)) return BOSUN_STORE_NOT_FOUND;
    bosun_store_result_t r;
    if (!profile || !*profile) r = bosun_config_coordinates_list(c, keys, BOSUN_PATCH_CATALOG_MAX, &count);
    else { r = catalog(id, false, keys, BOSUN_PATCH_CATALOG_MAX, &count); sort_keys(keys, count); }
    if (r != BOSUN_STORE_OK) return r;
    bosun_json_puts(w, "[");
    for (size_t i = 0; i < count; ++i) {
        size_t length; bosun_json_doc_t d;
        r = bosun_config_read_patch(c, profile, keys[i].bank, keys[i].slot, workspace, sizeof workspace, &length);
        if (r == BOSUN_STORE_OK) r = object(workspace, length, &d, work_tokens, BOSUN_PATCH_TOKENS);
        if (r != BOSUN_STORE_OK) return r;
        if (i) bosun_json_puts(w, ",");
        bosun_json_puts(w, "{\"bank\":"); bosun_json_write_integer(w, keys[i].bank);
        bosun_json_puts(w, ",\"slot\":"); bosun_json_write_integer(w, keys[i].slot);
        bosun_json_puts(w, ",\"name\":"); if (!raw(w, &d, bosun_json_get(&d, 0, "name"))) bosun_json_quote(w, "");
        bosun_json_puts(w, ",\"dirty\":"); bosun_json_puts(w, (!profile || !*profile) && bosun_config_dirty(c, keys[i].bank, keys[i].slot) ? "true" : "false");
        int linked = bosun_json_get(&d, 0, "linked_to");
        if (linked >= 0 && d.tokens[linked].type == BOSUN_JSON_OBJECT) { bosun_json_puts(w, ",\"linked_to\":"); raw(w, &d, linked); }
        bosun_json_puts(w, "}");
    }
    bosun_json_puts(w, "]");
    return w->failed ? BOSUN_STORE_LIMIT : BOSUN_STORE_OK;
}
static bosun_store_result_t remove_tree(const char *path, unsigned depth) {
    size_t count; char sub[BOSUN_PATH_MAX];
    if (depth > 4) return BOSUN_STORE_LIMIT;
    for (;;) {
        bosun_store_result_t r = bosun_store_list(path, scratch.entries, BOSUN_STORE_LIST_MAX, &count);
        if (r == BOSUN_STORE_NOT_FOUND) return BOSUN_STORE_OK;
        if (r != BOSUN_STORE_OK) return r;
        if (!count) break;
        bool directory = scratch.entries[0].directory;
        int n = snprintf(sub, sizeof sub, "%s/%s", path, scratch.entries[0].name);
        if (n < 0 || (size_t)n >= sizeof sub) return BOSUN_STORE_LIMIT;
        r = directory ? remove_tree(sub, depth + 1) : bosun_store_remove(sub);
        if (r != BOSUN_STORE_OK && r != BOSUN_STORE_NOT_FOUND) return r;
        /* Recursion reuses the single list workspace. Relist after removing
         * the first child instead of retaining ~7 KiB per directory depth. */
    }
    return bosun_store_remove(path);
}
bosun_store_result_t bosun_config_delete(bosun_config_t *c, const char *profile) {
    char path[BOSUN_PATH_MAX];
    if (!bosun_config_profile_id(profile)) return BOSUN_STORE_INVALID;
    int n = snprintf(path, sizeof path, "/config/profiles/%s", profile);
    if (n < 0 || (size_t)n >= sizeof path) return BOSUN_STORE_INVALID;
    bosun_store_result_t r = remove_tree(path, 0);
    if (r != BOSUN_STORE_OK && r != BOSUN_STORE_NOT_FOUND) return r;
    if (!strcmp(profile, c->profile)) {
        r = bosun_store_remove("/config/active_profile.json");
        if (r != BOSUN_STORE_OK && r != BOSUN_STORE_NOT_FOUND) return r;
        return bosun_config_init(c);
    }
    return BOSUN_STORE_OK;
}
