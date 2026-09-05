#include "bosun/application.h"
#include <stdio.h>
#include <string.h>

_Static_assert(BOSUN_APPLICATION_MIDI_BYTES >= BOSUN_MIDI_MAX_SYSEX + 2u,
               "A complete bounded SysEx must fit in each output queue");

static size_t bounded(size_t a, size_t b) { return a < b ? a : b; }

static bool expression_charge(void *context, uint8_t jack, bool high) {
    (void)context;
    return bosun_board_expression_charge(jack, high);
}

static bool expression_release(void *context, uint8_t jack) {
    (void)context;
    return bosun_board_expression_release(jack);
}

static bool expression_read(void *context, uint8_t jack, uint16_t *raw) {
    (void)context;
    *raw = bosun_board_expression_read(jack);
    return true;
}

static void expression_input(bosun_application_t *app) {
    uint8_t enabled = 0;
    for (unsigned i = 0; i < 2; ++i) {
        /* Capture the ordinary value before the first charge; the runtime
         * movement gate must start at the pedal's real parked position. */
        if (!bosun_expression_presence_busy(&app->expression_presence, (uint8_t)(i + 1)))
            app->expression_raw[i] = bosun_board_expression_read((uint8_t)(i + 1));
        if (app->runtime.expression[i].enabled) enabled |= (uint8_t)(1u << i);
    }
    /* Protocol/storage work earlier in the tick can take time. Anchor GPIO
     * deadlines at this phase's actual start, not the older loop timestamp. */
    bosun_expression_presence_tick(&app->expression_presence, bosun_board_millis(), enabled);
    for (unsigned i = 0; i < 2; ++i)
        bosun_runtime_expression_present(&app->runtime, i + 1,
            bosun_expression_presence_present(&app->expression_presence, (uint8_t)(i + 1)));
}

static void midi_connections(bosun_application_t *app) {
    for (unsigned port = 0; port < 2; ++port) {
        bosun_application_midi_queue_t *queue = &app->midi[port];
        bool connected = bosun_board_midi_connected((bosun_midi_port_t)port);
        if (connected != queue->connected) {
            /* A USB cable loss starts a new MIDI stream, independent of CDC
             * DTR. Never replay the tail of a packet on a new USB session. */
            app->midi_abandoned += queue->count;
            queue->head = queue->count = 0;
            bosun_runtime_reset_midi_input(&app->runtime, (uint8_t)port);
            queue->connected = connected;
        }
    }
}

bool bosun_application_send_midi(void *context, const uint8_t *data, size_t length) {
    bosun_application_t *app = context;
    if (!app || !data || !length) return false;
    midi_connections(app);
    bool output = false;
    for (unsigned port = 0; port < 2; ++port) {
        const bosun_application_midi_queue_t *queue = &app->midi[port];
        if (!queue->connected) continue;
        output = true;
        if (length > BOSUN_APPLICATION_MIDI_BYTES - queue->count) {
            ++app->midi_rejected;
            return false;
        }
    }
    if (!output) { ++app->midi_rejected; return false; }
    for (unsigned port = 0; port < 2; ++port) {
        bosun_application_midi_queue_t *queue = &app->midi[port];
        if (!queue->connected) continue;
        size_t tail = (queue->head + queue->count) % BOSUN_APPLICATION_MIDI_BYTES;
        size_t first = bounded(length, BOSUN_APPLICATION_MIDI_BYTES - tail);
        memcpy(queue->bytes + tail, data, first);
        memcpy(queue->bytes, data + first, length - first);
        queue->count += (uint16_t)length;
    }
    return true;
}

static void flush_midi(bosun_application_t *app) {
    for (unsigned port = 0; port < 2; ++port) {
        bosun_application_midi_queue_t *queue = &app->midi[port];
        if (!queue->connected || !queue->count) continue;
        size_t length = bounded(queue->count, BOSUN_APPLICATION_MIDI_BYTES - queue->head);
        length = bounded(length, BOSUN_APPLICATION_IO_BYTES);
        size_t written = bosun_board_midi_write((bosun_midi_port_t)port,
                                               queue->bytes + queue->head, length);
        written = bounded(written, length);
        queue->head = (uint16_t)((queue->head + written) % BOSUN_APPLICATION_MIDI_BYTES);
        queue->count -= (uint16_t)written;
    }
}

static void reset_overrun(bosun_application_t *app) {
    uint32_t dropped = bosun_board_midi_rx_dropped();
    if (dropped != app->din_dropped) {
        bosun_runtime_reset_midi_input(&app->runtime, BOSUN_MIDI_DIN);
        app->din_dropped = dropped;
    }
}

static void receive_midi(bosun_application_t *app, uint32_t now) {
    uint8_t bytes[BOSUN_APPLICATION_IO_BYTES];
    for (unsigned port = 0; port < 2; ++port) {
        if (!app->midi[port].connected) continue;
        if (port == BOSUN_MIDI_DIN) reset_overrun(app);
        size_t length = bosun_board_midi_read((bosun_midi_port_t)port, bytes, sizeof bytes);
        /* The DMA read itself can discover an overrun. Reset before consuming
         * the surviving bytes so stale running status cannot join two streams. */
        if (port == BOSUN_MIDI_DIN) reset_overrun(app);
        if (length) bosun_runtime_feed_midi(&app->runtime, (uint8_t)port, bytes, length, now);
    }
}

static void flush_protocol(bosun_application_t *app) {
    size_t length;
    const uint8_t *bytes = bosun_protocol_output(&app->protocol, &length);
    if (length) bosun_protocol_consume_output(&app->protocol,
        bosun_board_data_write(bytes, bounded(length, BOSUN_APPLICATION_IO_BYTES)));
}

static void protocol_input(bosun_application_t *app, uint32_t now) {
    bool connected = bosun_board_usb_connected();
    if (connected != app->connected) {
        app->connected = connected;
        app->input_offset = app->input_length = 0;
        bosun_protocol_session(&app->protocol, connected);
    }
    if (!connected) return;
    flush_protocol(app);
    if (!app->input_length) {
        app->input_length = bosun_board_data_read(app->input, sizeof app->input);
        app->input_offset = 0;
    }
    if (app->input_length) {
        size_t consumed = bosun_protocol_feed(&app->protocol,
            app->input + app->input_offset, app->input_length, now);
        app->input_offset += consumed;
        app->input_length -= consumed;
    }
}

static uint32_t color(const bosun_json_doc_t *doc, int token, uint32_t fallback) {
    char text[8];
    if (!bosun_json_string(doc, token, text, sizeof text) || strlen(text) != 7 || text[0] != '#')
        return fallback;
    uint32_t rgb = 0;
    for (unsigned i = 1; i < 7; ++i) {
        unsigned digit = (unsigned char)text[i];
        if (digit >= '0' && digit <= '9') digit -= '0';
        else if (digit >= 'a' && digit <= 'f') digit -= 'a' - 10;
        else if (digit >= 'A' && digit <= 'F') digit -= 'A' - 10;
        else return fallback;
        rgb = (rgb << 4) | digit;
    }
    return rgb;
}

static uint32_t scale(uint32_t rgb, unsigned amount) {
    uint32_t output = 0;
    for (unsigned shift = 0; shift < 24; shift += 8)
        output |= ((((rgb >> shift) & 255u) * amount + 127u) / 255u) << shift;
    return output;
}

static unsigned level(const bosun_json_doc_t *doc, int object, const char *key, unsigned fallback) {
    int32_t value = bosun_config_int(doc, object, key, (int32_t)fallback);
    return value < 0 ? 0u : value > 255 ? 255u : (unsigned)value;
}

static void render_leds(bosun_application_t *app, uint32_t now) {
    if (!app->led_started || (uint32_t)(now - app->led_ms) >= 20) {
        app->led_started = true; app->led_ms = now;
        const bosun_runtime_t *runtime = &app->runtime;
        const bosun_json_doc_t *device = &app->config.device_doc;
        const bosun_json_doc_t *patch = &app->config.patch_doc;
        int leds = bosun_json_get(device, 0, "leds");
        unsigned brightness = level(device, leds, "brightness", 64);
        unsigned dim = level(device, leds, "dim", 64);
        int nav = bosun_json_get(device, 0, "preset_navigation");
        int colors = bosun_json_get(device, nav, "bank_colors");
        char bank[8]; (void)snprintf(bank, sizeof bank, "%u", app->config.bank);
        uint32_t bank_color = color(device, bosun_json_get(device, colors, bank), 0x888888);
        for (unsigned sw = 0; sw < BOSUN_SWITCH_COUNT; ++sw) {
            const bosun_runtime_binding_t *binding = &runtime->bindings[sw];
            uint32_t rgb = 0;
            if (binding->patch_token >= 0) {
                int led = bosun_json_get(patch, binding->patch_token, "led");
                rgb = color(patch, bosun_json_get(patch, led, "on"), 0);
                /* Match CircuitPython: only latched bindings dim when off;
                 * momentary/tap bindings keep their configured on colour. */
                bool active = runtime->switches[sw].latched_on || (runtime->held_mask & (1u << sw));
                if (binding->mode == BOSUN_SWITCH_LATCHED && !active) {
                    uint32_t off = color(patch, bosun_json_get(patch, led, "off"), 0);
                    rgb = off ? off : scale(rgb, dim);
                }
            } else if (binding->preset_slot) {
                rgb = binding->preset_slot == app->config.slot ? bank_color : scale(bank_color, dim);
            }
            rgb = scale(rgb, brightness);
            /* CP reverses the last two physical LED indices on the lower
             * row; all three get one colour, so the resulting bytes agree. */
            for (unsigned pixel = 3 * sw; pixel < 3 * sw + 3; ++pixel) {
                if (app->leds[pixel] != rgb) { app->leds[pixel] = rgb; app->leds_dirty = true; }
            }
        }
    }
    if (app->leds_dirty) {
        for (unsigned pixel = 0; pixel < BOSUN_LED_COUNT; ++pixel)
            bosun_board_leds_set((uint8_t)pixel, app->leds[pixel]);
        if (bosun_board_leds_show()) app->leds_dirty = false;
    }
}

static void console_status(bosun_application_t *app, uint32_t now) {
    if (!app->console_length && (uint32_t)(now - app->console_ms) >= 3000) {
        app->console_ms = now;
        int count = snprintf(app->console, sizeof app->console,
            "Bosun native experimental storage=%s boot=%u ticks=%lu rx=%lu tx=%lu rejected=%lu abandoned=%lu display=%u\r\n",
            bosun_store_ready() ? "ready" : "unavailable", (unsigned)app->boot_result,
            (unsigned long)app->ticks, (unsigned long)app->runtime.midi_rx_count,
            (unsigned long)app->runtime.midi_tx_count, (unsigned long)app->midi_rejected,
            (unsigned long)app->midi_abandoned, app->display.status);
        if (count > 0) app->console_length = bounded((size_t)count, sizeof app->console - 1);
        app->console_offset = 0;
    }
    if (app->console_length) {
        size_t written = bosun_board_console_write((const uint8_t *)app->console + app->console_offset,
                                                   app->console_length);
        written = bounded(written, app->console_length);
        app->console_offset += written; app->console_length -= written;
    }
}

bool bosun_application_init(bosun_application_t *app, const char *host_root) {
    if (!app) return false;
    memset(app, 0, sizeof *app);
    if (!bosun_board_init(NULL)) return false;
    (void)bosun_store_mount(host_root); /* Deliberately no format/fallback write. */
    app->boot_result = bosun_config_init(&app->config);
    bosun_runtime_init(&app->runtime, &app->config, bosun_application_send_midi, app);
    bosun_protocol_init(&app->protocol, &app->runtime);
    bosun_display_init(&app->display);
    const bosun_expression_presence_backend_t presence_backend = {
        expression_charge, expression_release, expression_read
    };
    bosun_expression_presence_init(&app->expression_presence, &presence_backend, app);
    app->leds_dirty = true;
    app->console_ms = bosun_board_millis() - 3000u;
    return bosun_board_watchdog_enable(8000);
}

void bosun_application_tick(bosun_application_t *app) {
    bosun_board_task();
    uint32_t now = bosun_board_millis();
    ++app->ticks;
    midi_connections(app);
    flush_midi(app);
    receive_midi(app, now);
    protocol_input(app, now);
    if (app->runtime.config_revision != app->config.revision ||
        app->runtime.patch_revision != app->config.patch_revision)
        bosun_runtime_config_changed(&app->runtime);
    expression_input(app);
    bosun_runtime_tick(&app->runtime, now, bosun_board_switches(),
                       app->expression_raw[0], app->expression_raw[1]);
    bosun_config_tick(&app->config, now);
    flush_midi(app);
    if (!app->input_length) bosun_protocol_tick(&app->protocol, now);
    if (app->connected) flush_protocol(app);
    render_leds(app, now);
    (void)bosun_display_render(&app->display, &app->config,
        app->runtime.kemper_enabled ? &app->runtime.kemper.state : NULL, now);
    console_status(app, now);
    bosun_board_watchdog_feed();
    if (app->protocol.reboot_requested) {
        if (!app->reboot_pending) { app->reboot_pending = true; app->reboot_ms = now; }
        size_t remaining; (void)bosun_protocol_output(&app->protocol, &remaining);
        /* Allow the ACK to drain; a vanished client cannot postpone an
         * explicitly requested reboot indefinitely. No ROM BOOTSEL shortcut. */
        if ((!remaining && (uint32_t)(now - app->reboot_ms) >= 100) ||
            (uint32_t)(now - app->reboot_ms) >= 1000) bosun_board_reboot(false);
    } else app->reboot_pending = false;
}
