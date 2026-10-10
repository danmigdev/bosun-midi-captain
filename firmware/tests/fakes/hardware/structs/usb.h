#ifndef BOSUN_TEST_USB_HW_H
#define BOSUN_TEST_USB_HW_H
#include <stdint.h>
typedef struct { volatile uint32_t sie_status; } usb_hw_t;
extern usb_hw_t *usb_hw;
#endif
