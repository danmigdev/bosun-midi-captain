#include "bosun/midi.h"

#include <assert.h>
#include <stdio.h>
#include <string.h>

typedef struct {
    uint8_t channel, status, data[BOSUN_MIDI_MAX_SYSEX];
    size_t length, calls;
} capture;

static void receive(void *context, uint8_t channel, uint8_t status,
                    const uint8_t *data, size_t length) {
    capture *c = context;
    assert(length <= sizeof(c->data));
    c->channel = channel; c->status = status; c->length = length;
    memcpy(c->data, data, length); ++c->calls;
}

static void running_status(void) {
    bosun_midi_parser p;
    capture c = {0};
    bosun_midi_init(&p);
    const uint8_t start[] = {0xb2,17,0xf8};
    bosun_midi_feed(&p, start, sizeof(start), receive, &c);
    assert(c.calls == 0);
    const uint8_t end[] = {127,18,0xfa,0};
    bosun_midi_feed(&p, end, sizeof(end), receive, &c);
    assert(c.calls == 2 && c.channel == 3 && c.status == 0xb0);
    assert(c.length == 2 && c.data[0] == 18 && c.data[1] == 0);
    const uint8_t pc[] = {0xcf,3,4,0xff,5};
    bosun_midi_feed(&p, pc, sizeof(pc), receive, &c);
    assert(c.calls == 5 && c.channel == 16 && c.length == 1 && c.data[0] == 5);
    const uint8_t common[] = {0xf2,0,0,9,10};
    bosun_midi_feed(&p, common, sizeof(common), receive, &c);
    assert(c.calls == 5);
}

static void bounded_sysex(void) {
    bosun_midi_parser p;
    capture c = {0};
    bosun_midi_init(&p);
    bosun_midi_feed_byte(&p, 0xf0, receive, &c);
    for (size_t i = 0; i < BOSUN_MIDI_MAX_SYSEX; ++i)
        bosun_midi_feed_byte(&p, (uint8_t)(i & 127), receive, &c);
    bosun_midi_feed_byte(&p, 0xf8, receive, &c);
    bosun_midi_feed_byte(&p, 0xf7, receive, &c);
    assert(c.calls == 1 && c.length == BOSUN_MIDI_MAX_SYSEX);
    assert(c.channel == 0 && c.status == 0xf0 && c.data[1023] == 127);
    bosun_midi_feed_byte(&p, 0xf0, receive, &c);
    for (size_t i = 0; i < BOSUN_MIDI_MAX_SYSEX + 90; ++i)
        bosun_midi_feed_byte(&p, 1, receive, &c);
    assert(p.sysex_overflow && p.sysex_length == 0 && p.discarded_sysex == 1);
    bosun_midi_feed_byte(&p, 0xf7, receive, &c);
    assert(c.calls == 1);
    const uint8_t recover[] = {0xf0,1,2,0x90,60,127,0xf0,3,0xf0,4,0xf7};
    bosun_midi_feed(&p, recover, sizeof(recover), receive, &c);
    assert(c.calls == 3 && c.length == 1 && c.data[0] == 4 && c.status == 0xf0);
    const uint8_t broken[] = {0xf0,1,0xf1,3,4,5,0xf7};
    bosun_midi_feed(&p, broken, sizeof(broken), receive, &c);
    assert(c.calls == 3); /* system-common inside a broken frame is not voice */
}

static void independent_ports_and_encoding(void) {
    bosun_midi_parser usb, din;
    capture c = {0};
    bosun_midi_init(&usb); bosun_midi_init(&din);
    const uint8_t a[] = {0x90,60}, b[] = {127,17};
    bosun_midi_feed(&usb, a, sizeof(a), receive, &c);
    bosun_midi_feed(&din, b, sizeof(b), receive, &c);
    assert(c.calls == 0);
    bosun_midi_feed_byte(&usb, 100, receive, &c);
    assert(c.calls == 1 && c.data[0] == 60 && c.data[1] == 100);
    uint8_t out[16];
    assert(bosun_midi_encode(out, sizeof(out), 16, 0xb0, 255, 128) == 3);
    assert(out[0] == 0xbf && out[1] == 127 && out[2] == 0);
    assert(bosun_midi_encode(out, 2, 1, 0xb0, 1, 1) == 0);
    assert(bosun_midi_encode(out, sizeof(out), 0, 0xb0, 1, 1) == 0);
    assert(bosun_midi_encode(out, sizeof(out), 1, 0xf0, 1, 1) == 0);
    const uint8_t payload[] = {0,0x20,0x33,2,127,0x41,0,5,21};
    assert(bosun_midi_encode_sysex(out, sizeof(out), payload, sizeof(payload)) == 11);
    assert(out[0] == 0xf0 && out[10] == 0xf7 && !memcmp(out + 1, payload, 9));
    assert(bosun_midi_encode_sysex(out, sizeof(out), out, 11) == 0);
    assert(bosun_midi_encode_sysex(out, 8, payload, sizeof(payload)) == 0);
}

int main(void) {
    running_status(); bounded_sysex(); independent_ports_and_encoding();
    puts("MIDI: running status, realtime, framing, overflow, ports, codecs passed");
    return 0;
}
