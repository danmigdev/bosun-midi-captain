/* SPDX-License-Identifier: GPL-3.0-or-later */
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include "pico/unique_id.h"
#include "tusb.h"

/* Preserve the installed Captain's identity and CDC order for Bosun hosts.
 * CDC console = interfaces 0/1, data = 2/3, USB MIDI = 4/5. No MSC. */
static const tusb_desc_device_t device = {
    .bLength = sizeof(tusb_desc_device_t),
    .bDescriptorType = TUSB_DESC_DEVICE,
    .bcdUSB = 0x0200,
    .bDeviceClass = TUSB_CLASS_MISC,
    .bDeviceSubClass = MISC_SUBCLASS_COMMON,
    .bDeviceProtocol = MISC_PROTOCOL_IAD,
    .bMaxPacketSize0 = CFG_TUD_ENDPOINT0_SIZE,
    .idVendor = 0x239a,
    .idProduct = 0x80f4,
    .bcdDevice = 0x0100,
    .iManufacturer = 1, .iProduct = 2, .iSerialNumber = 3,
    .bNumConfigurations = 1,
};

enum { CDC_CONSOLE = 0, CDC_DATA = 2, MIDI_CONTROL = 4, INTERFACE_COUNT = 6 };
enum { CONFIG_LENGTH = TUD_CONFIG_DESC_LEN + 2 * TUD_CDC_DESC_LEN + TUD_MIDI_DESC_LEN };
static const uint8_t configuration[] = {
    TUD_CONFIG_DESCRIPTOR(1, INTERFACE_COUNT, 0, CONFIG_LENGTH, 0, 500),
    TUD_CDC_DESCRIPTOR(CDC_CONSOLE, 4, 0x81, 8, 0x02, 0x82, 64),
    TUD_CDC_DESCRIPTOR(CDC_DATA, 5, 0x83, 8, 0x04, 0x84, 64),
    TUD_MIDI_DESCRIPTOR(MIDI_CONTROL, 6, 0x05, 0x85, 64),
};
_Static_assert(sizeof(configuration) == CONFIG_LENGTH, "USB descriptor length mismatch");

const uint8_t *tud_descriptor_device_cb(void) { return (const uint8_t *)&device; }
const uint8_t *tud_descriptor_configuration_cb(uint8_t index) {
    return index == 0 ? configuration : NULL;
}

const uint16_t *tud_descriptor_string_cb(uint8_t index, uint16_t langid) {
    (void)langid;
    static uint16_t descriptor[33];
    static char serial[2 * PICO_UNIQUE_BOARD_ID_SIZE_BYTES + 1];
    static const char *const strings[] = {
        NULL, "PaintAudio", "MIDI Captain Bosun Native", NULL,
        "Bosun console", "Bosun data", "Bosun MIDI",
    };
    size_t count = 0;
    if (index == 0) {
        descriptor[1] = 0x0409;
        count = 1;
    } else {
        if (index >= sizeof(strings) / sizeof(strings[0])) return NULL;
        const char *text = strings[index];
        if (index == 3) {
            pico_get_unique_board_id_string(serial, sizeof(serial));
            text = serial;
        }
        if (!text) return NULL;
        while (text[count] && count < 32) {
            descriptor[count + 1] = (uint8_t)text[count];
            ++count;
        }
    }
    descriptor[0] = (uint16_t)((TUSB_DESC_STRING << 8) | (2 * count + 2));
    return descriptor;
}
