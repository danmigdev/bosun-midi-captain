#include "bosun/midi.h"

#include <string.h>

void bosun_midi_init(bosun_midi_parser *p) {
    if (p) memset(p, 0, sizeof(*p));
}

void bosun_midi_feed_byte(bosun_midi_parser *p, uint8_t b,
                         bosun_midi_receive_fn receive, void *context) {
    if (!p || b >= 0xf8) return; /* Realtime never disturbs running status. */
    if (p->in_sysex) {
        if (b == 0xf7) {
            if (!p->sysex_overflow && receive)
                receive(context, 0, 0xf0, p->sysex, p->sysex_length);
            p->in_sysex = p->sysex_overflow = false;
            p->sysex_length = 0;
            return;
        }
        if (b < 0x80) {
            if (!p->sysex_overflow) {
                if (p->sysex_length < sizeof(p->sysex))
                    p->sysex[p->sysex_length++] = b;
                else {
                    p->sysex_overflow = true;
                    p->sysex_length = 0;
                    p->discarded_sysex++;
                }
            }
            return;
        }
        /* Abort malformed SysEx, then process this status normally. */
        p->in_sysex = p->sysex_overflow = false;
        p->sysex_length = 0;
    }
    if (b >= 0xf0) {
        p->status = p->data_length = p->expected = 0;
        if (b == 0xf0) {
            p->in_sysex = true;
            p->sysex_length = 0;
        }
        return;
    }
    if (b >= 0x80) {
        p->status = b;
        p->data_length = 0;
        p->expected = ((b & 0xf0) == 0xc0 || (b & 0xf0) == 0xd0) ? 1 : 2;
        return;
    }
    if (!p->status) return;
    p->data[p->data_length++] = b;
    if (p->data_length == p->expected) {
        if (receive) receive(context, (uint8_t)((p->status & 15) + 1),
            p->status & 0xf0, p->data, p->data_length);
        p->data_length = 0;
    }
}

void bosun_midi_feed(bosun_midi_parser *p, const uint8_t *data, size_t length,
                    bosun_midi_receive_fn receive, void *context) {
    if (!data) return;
    for (size_t i = 0; i < length; ++i)
        bosun_midi_feed_byte(p, data[i], receive, context);
}

size_t bosun_midi_encode(uint8_t *output, size_t capacity, uint8_t channel,
                         uint8_t status, uint8_t first, uint8_t second) {
    if (!output || channel < 1 || channel > 16 || status < 0x80 ||
        status > 0xe0 || (status & 15)) return 0;
    size_t length = (status == 0xc0 || status == 0xd0) ? 2u : 3u;
    if (capacity < length) return 0;
    output[0] = status | (channel - 1);
    output[1] = first & 0x7f;
    if (length == 3) output[2] = second & 0x7f;
    return length;
}

size_t bosun_midi_encode_sysex(uint8_t *output, size_t capacity,
                               const uint8_t *payload, size_t length) {
    if (!output || (!payload && length) || capacity < 2 ||
        length > capacity - 2 || length > BOSUN_MIDI_MAX_SYSEX) return 0;
    for (size_t i = 0; i < length; ++i) if (payload[i] >= 0x80) return 0;
    if (length) memmove(output + 1, payload, length);
    output[0] = 0xf0;
    output[length + 1] = 0xf7;
    return length + 2;
}
