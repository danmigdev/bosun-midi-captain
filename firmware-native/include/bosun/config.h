#ifndef BOSUN_CONFIG_H
#define BOSUN_CONFIG_H
#include "bosun/json.h"
#include "bosun/storage.h"

#define BOSUN_DEVICE_BYTES 16384u
#define BOSUN_PATCH_BYTES 24576u
#define BOSUN_DEVICE_TOKENS 1024u
#define BOSUN_PATCH_TOKENS 1536u
#define BOSUN_PROFILE_ID_BYTES 33u
#define BOSUN_DIRTY_PATCHES 128u
#define BOSUN_PROFILE_MAX 32u
#define BOSUN_PATCH_CATALOG_MAX 256u
#define BOSUN_BINDING_BYTES 8192u
typedef struct { uint16_t bank, slot; } bosun_patch_key_t;
typedef struct { uint16_t bank, slot; uint32_t modified_ms; } bosun_dirty_patch_t;
typedef struct {
    char profile[BOSUN_PROFILE_ID_BYTES], kind[40];
    char device[BOSUN_DEVICE_BYTES + 1], patch[BOSUN_PATCH_BYTES + 1];
    bosun_json_token_t device_tokens[BOSUN_DEVICE_TOKENS], patch_tokens[BOSUN_PATCH_TOKENS];
    bosun_json_doc_t device_doc, patch_doc;
    bosun_dirty_patch_t dirty[BOSUN_DIRTY_PATCHES];
    uint16_t bank, slot, dirty_count;
    uint32_t revision, patch_revision;
    bool has_patch;
    bosun_store_result_t last_error;
} bosun_config_t;

extern const char bosun_default_device[];
bool bosun_config_profile_id(const char *id);
bool bosun_config_coordinates(unsigned bank, unsigned slot);
bool bosun_config_path(char *out, size_t capacity, const char *profile, const char *file);
bool bosun_config_patch_path(char *out, size_t capacity, const char *profile,
                             unsigned bank, unsigned slot, bool draft);
bool bosun_config_profile_exists(const char *profile);
bosun_store_result_t bosun_config_init(bosun_config_t *config);
bosun_store_result_t bosun_config_activate(bosun_config_t *config, const char *profile, bool persist);
bosun_store_result_t bosun_config_create(const char *profile, const char *name, const char *kind, const char *color);
bosun_store_result_t bosun_config_rename(const char *profile, const char *name);
bosun_store_result_t bosun_config_delete(bosun_config_t *config, const char *profile);
bosun_store_result_t bosun_config_read(const bosun_config_t *config, const char *profile,
    const char *file, char *out, size_t capacity, size_t *length);
bosun_store_result_t bosun_config_put_device(bosun_config_t *config, const char *profile,
                                            const char *json, size_t length);
bosun_store_result_t bosun_config_select(bosun_config_t *config, unsigned bank, unsigned slot);
/* The validator sees temporary JSON before selection changes any live state.
 * It must not retain the document or reenter config/storage operations. */
typedef bool (*bosun_config_validate_patch_fn)(const bosun_json_doc_t *document, void *context);
bosun_store_result_t bosun_config_select_checked(bosun_config_t *config, unsigned bank,
    unsigned slot, bosun_config_validate_patch_fn validate, void *context);
bosun_store_result_t bosun_config_read_patch(const bosun_config_t *config, const char *profile,
    unsigned bank, unsigned slot, char *out, size_t capacity, size_t *length);
bosun_store_result_t bosun_config_put_patch(bosun_config_t *config, const char *profile,
    unsigned bank, unsigned slot, const char *json, size_t length, uint32_t now_ms);
/* output is scratch, separate from binding and config. On success the whole
 * patch is staged atomically as a draft; unknown JSON fields are preserved. */
bosun_store_result_t bosun_config_put_binding(bosun_config_t *config, unsigned bank, unsigned slot,
    const char *binding, size_t length, char *output, size_t capacity, uint32_t now_ms);
bosun_store_result_t bosun_config_remove_patch(bosun_config_t *config, unsigned bank, unsigned slot);
bosun_store_result_t bosun_config_save(bosun_config_t *config, unsigned bank, unsigned slot,
                                      bool discard, bosun_json_writer_t *saved);
bosun_store_result_t bosun_config_profiles(const bosun_config_t *config, bosun_json_writer_t *writer);
bosun_store_result_t bosun_config_patches(const bosun_config_t *config, const char *profile,
                                         bosun_json_writer_t *writer);
bool bosun_config_dirty(const bosun_config_t *config, unsigned bank, unsigned slot);
/* Sorted active-profile persisted+draft coordinates, no patch JSON parsing.
 * LIMIT resets count to zero. A caller may choose a smaller bounded capacity. */
bosun_store_result_t bosun_config_coordinates_list(const bosun_config_t *config,
    bosun_patch_key_t *keys, size_t capacity, size_t *count);
bool bosun_config_has_patch(const bosun_config_t *config, unsigned bank, unsigned slot);
bosun_store_result_t bosun_config_patch_name(const bosun_config_t *config, unsigned bank,
    unsigned slot, char *output, size_t capacity);
/* Application tick: autosave only, no MIDI or display side effects. */
void bosun_config_tick(bosun_config_t *config, uint32_t now_ms);
int32_t bosun_config_int(const bosun_json_doc_t *doc, int object, const char *key, int32_t fallback);
bool bosun_config_bool(const bosun_json_doc_t *doc, int object, const char *key, bool fallback);
#endif
