/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef BOSUN_SCREENSAVER_H
#define BOSUN_SCREENSAVER_H
#include "bosun/config.h"

/* Screensaver effects; MIX rotates through the other three every minute.
 * The application owns the idle timer, the display draws the effect and the
 * protocol reports it. This file has no board dependency. */
typedef enum {
    BOSUN_SCREENSAVER_OFF, BOSUN_SCREENSAVER_BOUNCE, BOSUN_SCREENSAVER_BREATHE,
    BOSUN_SCREENSAVER_DRIFT, BOSUN_SCREENSAVER_MIX
} bosun_screensaver_t;

/* device.json tft.screensaver_minutes (0..120, 0 = off, default 0, clamped)
 * and tft.screensaver_effect ("bounce", "breathe", "drift" or "mix"; anything
 * else reads as mix). parse returns OFF for anything but those four strings. */
unsigned bosun_screensaver_minutes(const bosun_config_t *config);
unsigned bosun_screensaver_effect(const bosun_config_t *config);
unsigned bosun_screensaver_parse(const bosun_json_doc_t *doc, int token);
/* Wire name of an effect; "off" for OFF or an out-of-range value. */
const char *bosun_screensaver_name(unsigned effect);
#endif
