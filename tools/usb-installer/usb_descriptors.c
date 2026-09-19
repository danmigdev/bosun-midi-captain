/* Standard CDC descriptors. Serial is read from physical flash before USB init;
 * the SDK's default serial helper is unsuitable for a NO_FLASH build. */
#include "tusb.h"
#include <string.h>
extern char bi_usb_serial[17];

static const tusb_desc_device_t device = {
    .bLength = sizeof(tusb_desc_device_t),
    .bDescriptorType = TUSB_DESC_DEVICE,
    .bcdUSB = 0x0200,
    .bDeviceClass = TUSB_CLASS_MISC,
    .bDeviceSubClass = MISC_SUBCLASS_COMMON,
    .bDeviceProtocol = MISC_PROTOCOL_IAD,
    .bMaxPacketSize0 = CFG_TUD_ENDPOINT0_SIZE,
    .idVendor = 0x239a, .idProduct = 0x80f4, .bcdDevice = 0x0100,
    .iManufacturer = 1, .iProduct = 2, .iSerialNumber = 3,
    .bNumConfigurations = 1,
};
static const uint8_t configuration[] = {
    TUD_CONFIG_DESCRIPTOR(1, 2, 0, TUD_CONFIG_DESC_LEN + TUD_CDC_DESC_LEN, 0, 250),
    TUD_CDC_DESCRIPTOR(0, 4, 0x81, 8, 0x02, 0x82, 64),
};
const uint8_t *tud_descriptor_device_cb(void) { return (const uint8_t *)&device; }
const uint8_t *tud_descriptor_configuration_cb(uint8_t index) { (void)index; return configuration; }
const uint16_t *tud_descriptor_string_cb(uint8_t index, uint16_t language) {
    (void)language;
    static uint16_t output[64];
    static const char *const strings[] = {"", "Bosun", "Bosun USB Installer", bi_usb_serial, "Installer"};
    size_t count;
    if (!index) { output[1] = 0x0409; count = 1; }
    else {
        if (index >= sizeof strings / sizeof strings[0]) return NULL;
        count = strlen(strings[index]);
        if (count > 63) count = 63;
        for (size_t i = 0; i < count; ++i) output[i + 1] = (uint8_t)strings[index][i];
    }
    output[0] = (uint16_t)((TUSB_DESC_STRING << 8) | (2 * count + 2));
    return output;
}
