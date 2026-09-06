/* Host-only line adapter for exercising the real fixed-buffer engines against
 * the canonical Python implementation. No protocol/runtime production shim. */
#include "bosun/midi.h"
#include "bosun/switch_fsm.h"
#include "bosun/kemper.h"
#include "bosun/json.h"

#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static bosun_midi_parser parsers[2];
static bosun_switch_fsm fsm;
static bosun_kemper kemper;
static bool first_event;

static void hex(const uint8_t *data, size_t length) {
    putchar('"');
    for (size_t i = 0; i < length; ++i) printf("%02x", data[i]);
    putchar('"');
}

static void receive(void *context, uint8_t channel, uint8_t status,
                    const uint8_t *data, size_t length) {
    (void)context;
    if (!first_event) putchar(',');
    first_event = false;
    printf("[%u,%u,", channel, status);
    hex(data, length);
    putchar(']');
}

static bool send_packet(void *context, const uint8_t *data, size_t length) {
    receive(context, 0, 0, data, length);
    return true;
}

static size_t unhex(const char *text, uint8_t *output, size_t capacity) {
    size_t used = 0;
    while (*text && *text != '\n' && *text != '\r') {
        unsigned byte;
        if (text[0] == '-') break;
        assert(text[1] && used < capacity && sscanf(text, "%2x", &byte) == 1);
        output[used++] = (uint8_t)byte;
        text += 2;
    }
    return used;
}

static void inspect_json(const uint8_t *data, size_t length) {
    bosun_json_doc_t document;
    bosun_json_token_t tokens[2048];
    char decoded[4096], quoted[24578];
    bosun_json_result_t result = bosun_json_parse(&document, (const char *)data,
        length, tokens, sizeof tokens / sizeof *tokens);
    printf("{\"result\":%u,\"tokens\":[", result);
    for (unsigned i = 0; i < document.count; ++i) {
        const bosun_json_token_t *token = &tokens[i];
        const char *raw; size_t raw_length;
        assert(bosun_json_raw(&document, (int)i, &raw, &raw_length));
        assert(raw == (const char *)data + token->start && raw_length == token->end - token->start);
        if (i) putchar(',');
        printf("[%u,%u,%u,%u,", token->type, token->start, token->end, token->next);
        if (bosun_json_string(&document, (int)i, decoded, sizeof decoded)) {
            hex((const uint8_t *)decoded, strlen(decoded));
            putchar(',');
            bosun_json_writer_t writer;
            bosun_json_writer_init(&writer, quoted, sizeof quoted);
            assert(bosun_json_quote(&writer, decoded));
            hex((const uint8_t *)quoted, writer.length);
        } else printf("null,null");
        int32_t number;
        if (bosun_json_integer(&document, (int)i, &number)) printf(",%ld]", (long)number);
        else printf(",null]");
    }
    puts("]}");
    fflush(stdout);
}

int main(void) {
    char line[8192], encoded[8192];
    uint8_t data[4096];
    bosun_switch_init(&fsm, NULL);
    bosun_kemper_init(&kemper, 1, 0, send_packet, NULL);
    while (fgets(line, sizeof(line), stdin)) {
        if (line[0] == 'q') return 0;
        if (line[0] == 'J') {
            assert(sscanf(line + 1, "%8191s", encoded) == 1);
            inspect_json(data, unhex(encoded, data, sizeof data));
            continue;
        }
        unsigned a = 0, b = 0, c = 0, d = 0;
        int value = 0;
        bosun_switch_result result = {BOSUN_SWITCH_NO_EDGE, 0};
        bool active = false, ok = true;
        first_event = true;
        printf("{\"events\":[");
        switch (line[0]) {
        case 'I':
            assert(sscanf(line + 1, "%u", &a) == 1 && a < 2);
            bosun_midi_init(&parsers[a]);
            break;
        case 'M':
            assert(sscanf(line + 1, "%u %8191s", &a, encoded) == 2 && a < 2);
            bosun_midi_feed(&parsers[a], data, unhex(encoded, data, sizeof(data)), receive, NULL);
            break;
        case 'S': {
            assert(sscanf(line + 1, "%u %u %u %u", &a, &b, &c, &d) == 4);
            bosun_switch_config config = {a, b, c, d != 0};
            bosun_switch_init(&fsm, &config);
            break;
        }
        case 's':
            assert(sscanf(line + 1, "%u %u %u %u", &a, &b, &c, &d) == 4);
            if (d) bosun_switch_reset(&fsm);
            result = bosun_switch_poll(&fsm, a, b != 0, (bosun_switch_mode)c);
            active = bosun_switch_momentary_active(&fsm, a, (bosun_switch_mode)c);
            break;
        case 'K':
            assert(sscanf(line + 1, "%u %u", &a, &b) == 2);
            bosun_kemper_init(&kemper, (uint8_t)a, (uint8_t)b, send_packet, NULL);
            break;
        case 't':
            assert(sscanf(line + 1, "%u", &a) == 1);
            bosun_kemper_tick(&kemper, a);
            break;
        case 'b':
            assert(sscanf(line + 1, "%u %u", &a, &b) == 2);
            ok = bosun_kemper_begin_rig(&kemper, (uint8_t)b, a);
            break;
        case 'r':
            assert(sscanf(line + 1, "%u %u %u", &a, &b, &c) == 3);
            ok = bosun_kemper_select_rig(&kemper, (uint8_t)b, (uint8_t)c, a);
            break;
        case 'R':
            assert(sscanf(line + 1, "%u %u %u %u", &a, &b, &c, &d) == 4);
            ok = bosun_kemper_select_rig_channel(&kemper, (uint8_t)b, (uint8_t)c, (uint8_t)d, a);
            break;
        case 'c':
            assert(sscanf(line + 1, "%u %u %d", &a, &b, &value) == 3);
            ok = bosun_kemper_command(&kemper, (bosun_kemper_command_type)a, (uint8_t)b, value);
            break;
        case 'C':
            assert(sscanf(line + 1, "%u %u %u %d", &a, &b, &c, &value) == 4);
            ok = bosun_kemper_command_channel(&kemper, (uint8_t)a,
                (bosun_kemper_command_type)b, (uint8_t)c, value);
            break;
        case 'h':
            assert(sscanf(line + 1, "%u %u %u %8191s", &a, &b, &c, encoded) == 4);
            bosun_kemper_handle(&kemper, (uint8_t)b, (uint8_t)c, data,
                unhex(encoded, data, sizeof(data)), a);
            break;
        default: assert(!"unknown differential command");
        }
        unsigned on = 0;
        for (unsigned i = 0; i < 8; ++i) if (kemper.state.effects[i]) on |= 1u << i;
        printf("],\"ok\":%u,\"channel\":%u,\"switch\":[%u,%u,%u,%u],\"state\":[%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u],\"name\":",
            ok, kemper.channel, result.edge, result.triggers, fsm.latched_on, active,
            kemper.state.rig, kemper.state.effect_known, on & kemper.state.effect_known,
            kemper.state.expression_mode, kemper.state.connected, kemper.state.tuner_active,
            kemper.state.bpm, kemper.state.tuner_deviance, kemper.state.rig_name_fresh,
            kemper.generation, kemper.reconcile_pending, kemper.reconcile_attempt,
            bosun_kemper_transition_active(&kemper));
        hex((const uint8_t *)kemper.state.rig_name, strlen(kemper.state.rig_name));
        printf(",\"note\":");
        hex((const uint8_t *)kemper.state.tuner_note, strlen(kemper.state.tuner_note));
        puts("}");
        fflush(stdout);
    }
    return ferror(stdin) ? 1 : 0;
}
