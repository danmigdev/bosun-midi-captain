#include "tusb.h"
#include "pico/unique_id.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

void pico_get_unique_board_id_string(char *buffer, size_t length) {
    assert(length >= 17);
    memcpy(buffer, "0123456789ABCDEF", 17);
}
static unsigned little16(const uint8_t *p) { return p[0] | ((unsigned)p[1] << 8); }

int main(void) {
    const uint8_t *device = tud_descriptor_device_cb();
    assert(device[0] == 18 && device[1] == TUSB_DESC_DEVICE);
    assert(little16(device + 8) == 0x239a && little16(device + 10) == 0x80f4);
    assert(device[4] == TUSB_CLASS_MISC && device[7] == 64 && device[17] == 1);
    const uint8_t *config = tud_descriptor_configuration_cb(0);
    assert(config && !tud_descriptor_configuration_cb(1));
    assert(config[0] == 9 && config[1] == TUSB_DESC_CONFIGURATION && config[4] == 6);
    const uint8_t classes[] = {2, 10, 2, 10, 1, 1};
    const uint8_t endpoint_counts[] = {1, 2, 1, 2, 0, 2};
    size_t total = little16(config + 2), position = 0;
    unsigned seen_interfaces = 0, endpoints = 0, interface_endpoints = 0;
    unsigned current_interface = 6, interface_associations = 0;
    bool addresses[256] = {0};
    while (position < total) {
        const uint8_t *d = config + position;
        assert(d[0] >= 2 && position + d[0] <= total);
        if (d[1] == TUSB_DESC_INTERFACE) {
            assert(d[0] == 9 && d[2] == seen_interfaces && d[3] == 0);
            if (current_interface != 6) assert(interface_endpoints == endpoint_counts[current_interface]);
            current_interface = d[2]; interface_endpoints = 0;
            assert(current_interface < 6 && d[4] == endpoint_counts[current_interface]);
            assert(d[5] == classes[current_interface]);
            ++seen_interfaces;
        } else if (d[1] == TUSB_DESC_ENDPOINT) {
            assert(d[0] >= 7 && current_interface < 6);
            assert((d[2] & 15) != 0 && !addresses[d[2]]);
            addresses[d[2]] = true;
            unsigned packet = little16(d + 4);
            assert(packet > 0 && packet <= 64);
            assert((d[3] & 3) == (current_interface == 0 || current_interface == 2 ? 3 : 2));
            ++endpoints; ++interface_endpoints;
        } else if (d[1] == TUSB_DESC_INTERFACE_ASSOCIATION) {
            assert(d[0] == 8 && d[3] == 2);
            assert(d[2] == interface_associations * 2);
            ++interface_associations;
        }
        position += d[0];
    }
    assert(position == total && seen_interfaces == 6 && endpoints == 8);
    assert(interface_associations == 2 && interface_endpoints == 2);
    assert(addresses[0x81] && addresses[0x02] && addresses[0x82]);
    assert(addresses[0x83] && addresses[0x04] && addresses[0x84]);
    assert(addresses[0x05] && addresses[0x85]);
    for (uint8_t index = 0; index <= 6; ++index) {
        const uint16_t *text = tud_descriptor_string_cb(index, 0x0409);
        assert(text && (text[0] >> 8) == TUSB_DESC_STRING);
        unsigned bytes = text[0] & 255;
        assert(bytes >= 4 && bytes <= 66 && bytes % 2 == 0);
        if (index == 0) assert(text[1] == 0x0409);
        if (index == 3) {
            assert(bytes == 34);
            for (unsigned i = 0; i < 16; ++i) assert(text[i + 1] == (uint8_t)"0123456789ABCDEF"[i]);
        }
    }
    assert(!tud_descriptor_string_cb(7, 0x0409));
    assert(!tud_descriptor_string_cb(255, 0x0409));
    puts("actual TinyUSB composite descriptors: PASS");
    return 0;
}
