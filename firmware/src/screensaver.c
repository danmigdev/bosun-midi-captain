/* SPDX-License-Identifier: GPL-3.0-or-later */
#include "bosun/screensaver.h"

static const char *const names[] = {"off", "bounce", "breathe", "drift", "mix"};

unsigned bosun_screensaver_parse(const bosun_json_doc_t *doc, int token) {
    for (unsigned effect = BOSUN_SCREENSAVER_BOUNCE; effect <= BOSUN_SCREENSAVER_MIX; ++effect)
        if (bosun_json_equal(doc, token, names[effect])) return effect;
    return BOSUN_SCREENSAVER_OFF;
}
const char *bosun_screensaver_name(unsigned effect) {
    return names[effect <= BOSUN_SCREENSAVER_MIX ? effect : BOSUN_SCREENSAVER_OFF];
}
unsigned bosun_screensaver_minutes(const bosun_config_t *config) {
    const bosun_json_doc_t *doc = &config->device_doc;
    int32_t minutes = bosun_config_int(doc, bosun_json_get(doc, 0, "tft"), "screensaver_minutes", 0);
    return minutes < 0 ? 0u : minutes > 120 ? 120u : (unsigned)minutes;
}
unsigned bosun_screensaver_effect(const bosun_config_t *config) {
    const bosun_json_doc_t *doc = &config->device_doc;
    unsigned effect = bosun_screensaver_parse(doc,
        bosun_json_get(doc, bosun_json_get(doc, 0, "tft"), "screensaver_effect"));
    return effect ? effect : BOSUN_SCREENSAVER_MIX;
}
