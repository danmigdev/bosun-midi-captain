#ifndef BOSUN_APPLICATION_H
#define BOSUN_APPLICATION_H
#include "bosun/display.h"
#include "bosun/protocol.h"
#include "bosun/expression_presence.h"

#define BOSUN_APPLICATION_MIDI_BYTES 2048u
#define BOSUN_APPLICATION_IO_BYTES 256u

typedef struct {
    uint8_t bytes[BOSUN_APPLICATION_MIDI_BYTES];
    uint16_t head, count;
    bool connected;
} bosun_application_midi_queue_t;

/* One single-core application, caller-owned static storage. The board API is
 * shared by RP2040 and the loopback emulator; no heap is used by this layer. */
typedef struct {
    bosun_config_t config;
    bosun_runtime_t runtime;
    bosun_protocol_t protocol;
    bosun_display_t display;
    bosun_expression_presence_t expression_presence;
    uint16_t expression_raw[2];
    bosun_application_midi_queue_t midi[2];
    uint8_t input[BOSUN_APPLICATION_IO_BYTES];
    size_t input_offset, input_length;
    uint32_t leds[BOSUN_LED_COUNT];
    uint32_t midi_rejected, midi_abandoned, din_dropped, ticks;
    uint32_t led_ms, console_ms, reboot_ms;
    char console[192];
    size_t console_offset, console_length;
    bosun_store_result_t boot_result;
    bool connected, leds_dirty, led_started, reboot_pending;
} bosun_application_t;

/* Initializes board, mounts existing storage, loads configuration. A missing or
 * unrecognized filesystem stays untouched; diagnostics remain available. No
 * MIDI is generated or transmitted until the first application tick. */
bool bosun_application_init(bosun_application_t *app, const char *host_root);
void bosun_application_tick(bosun_application_t *app);
/* Whole-packet admission to every currently connected output; false admits
 * nothing. Once admitted, partial board writes retain their exact suffix. */
bool bosun_application_send_midi(void *context, const uint8_t *data, size_t length);
#endif
