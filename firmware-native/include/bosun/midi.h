#ifndef BOSUN_MIDI_H
#define BOSUN_MIDI_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define BOSUN_MIDI_MAX_SYSEX 1024u

/* Callbacks consume data synchronously; buffers expire on return. SysEx RX
 * excludes F0/F7, channel is 0 and status is F0. Voice channels are 1..16. */
typedef void (*bosun_midi_receive_fn)(void *context, uint8_t channel,
    uint8_t status, const uint8_t *data, size_t length);
typedef bool (*bosun_midi_send_fn)(void *context, const uint8_t *data,
    size_t length);

typedef struct {
    uint8_t sysex[BOSUN_MIDI_MAX_SYSEX];
    size_t sysex_length;
    uint8_t status, data[2], data_length, expected;
    bool in_sysex, sysex_overflow;
    uint32_t discarded_sysex;
} bosun_midi_parser;

void bosun_midi_init(bosun_midi_parser *parser);
void bosun_midi_feed_byte(bosun_midi_parser *parser, uint8_t byte,
    bosun_midi_receive_fn receive, void *context);
void bosun_midi_feed(bosun_midi_parser *parser, const uint8_t *data,
    size_t length, bosun_midi_receive_fn receive, void *context);
/* Encodes voice messages, returns 0 for invalid status/channel/capacity.
 * Data bytes are masked to seven bits, as in the original firmware. */
size_t bosun_midi_encode(uint8_t *output, size_t capacity, uint8_t channel,
    uint8_t status, uint8_t first, uint8_t second);
size_t bosun_midi_encode_sysex(uint8_t *output, size_t capacity,
    const uint8_t *payload, size_t length);

#endif
