#ifndef BOSUN_TEST_CLOCKS_H
#define BOSUN_TEST_CLOCKS_H
#include <stdint.h>
enum clock_index { clk_sys, clk_usb };
uint32_t clock_get_hz(enum clock_index clock);
#endif
