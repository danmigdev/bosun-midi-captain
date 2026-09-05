#ifndef BOSUN_BOARD_H
#define BOSUN_BOARD_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

enum { BOSUN_SWITCH_COUNT = 10, BOSUN_LED_COUNT = 30,
       BOSUN_DISPLAY_WIDTH = 240, BOSUN_DISPLAY_HEIGHT = 240 };

typedef enum { BOSUN_MIDI_USB = 0, BOSUN_MIDI_DIN = 1 } bosun_midi_port_t;
typedef struct {
    uint16_t rotation; /* 0, 90, 180 or 270; NULL config defaults to 180. */
    uint8_t brightness; /* TFT backlight, 0..255. */
} bosun_board_config_t;

/* Single-core API: call on core 0, outside interrupts. No heap allocation.
 * init does not touch configuration flash or mount/format a filesystem. */
bool bosun_board_init(const bosun_board_config_t *config);
void bosun_board_task(void);
uint32_t bosun_board_millis(void);
bool bosun_board_usb_connected(void); /* CDC data interface 1, DTR asserted. */
/* Advances on each data-session edge, including gaps missed by polling. */
uint32_t bosun_board_usb_session_generation(void);
/* MIDI USB availability follows enumeration, independently of CDC DTR. */
bool bosun_board_midi_connected(bosun_midi_port_t port);

/* Nonblocking. Return bytes transferred/accepted; retain any unsent suffix.
 * Console is CDC 0; firmware JSON is CDC 1. No implicit protocol framing. */
size_t bosun_board_data_read(uint8_t *data, size_t capacity);
size_t bosun_board_data_write(const uint8_t *data, size_t length);
size_t bosun_board_console_write(const uint8_t *data, size_t length);
size_t bosun_board_midi_read(bosun_midi_port_t port, uint8_t *data, size_t capacity);
size_t bosun_board_midi_write(bosun_midi_port_t port, const uint8_t *data, size_t length);
/* DIN RX is a 2048-byte DMA ring, active while flash IRQs are suspended.
 * Counter reports overwritten bytes and hardware error events; callers must
 * reset their DIN parser when it changes. No unlimited blackout guarantee. */
uint32_t bosun_board_midi_rx_dropped(void);

/* Bit order and LED chain: 1,2,3,4,up,A,B,C,D,down. A set bit means pressed.
 * Debouncing and gesture recognition belong to the application. */
uint16_t bosun_board_switches(void);
uint16_t bosun_board_expression_read(uint8_t jack); /* 1/2, scaled 0..65535. */
/* Presence probe GPIO phases: charge drives one rail, release restores the
 * high-impedance ADC input. Neither waits; the presence FSM owns timing.
 * An unavailable backend returns false from charge and stays silent. */
bool bosun_board_expression_charge(uint8_t jack, bool high);
bool bosun_board_expression_release(uint8_t jack);
void bosun_board_leds_set(uint8_t index, uint32_t rgb24);
bool bosun_board_leds_show(void); /* false if previous DMA/latch is busy. */
/* Last submitted DMA frame, not a pending render or a physical light sensor. */
uint32_t bosun_board_leds_get(uint8_t index);

/* RGB565 words in host byte order. Rectangles are clipped to 240x240.
 * blit stride is in pixels and must be >= width. No framebuffer: at most
 * one 480-byte line is staged; USB is serviced between display rows. */
bool bosun_board_display_rotation(uint16_t degrees);
void bosun_board_display_brightness(uint8_t brightness);
bool bosun_board_display_fill_rect(int16_t x, int16_t y, uint16_t width,
                                   uint16_t height, uint16_t rgb565);
bool bosun_board_display_blit_rgb565(int16_t x, int16_t y, uint16_t width,
                                    uint16_t height, const uint16_t *pixels,
                                    uint16_t stride);

bool bosun_board_watchdog_enable(uint32_t timeout_ms); /* 1..8000 ms. */
void bosun_board_watchdog_feed(void);
void bosun_board_reboot(bool bootloader); /* ROM BOOTSEL when true. */

/* Absolute offsets from physical flash start, NOT XIP pointers. Every access
 * is restricted to the reserved final 512 KiB. program/erase reject bad
 * alignment and never format implicitly. Image linker excludes this area.
 * Flash erase/program briefly suspend IRQs on core 0, one sector/page at a
 * time, servicing USB before/after. Core 1 must remain unused. */
uint32_t bosun_board_storage_offset(void);
uint32_t bosun_board_storage_size(void);
bool bosun_board_flash_read(uint32_t offset, uint8_t *data, size_t length);
bool bosun_board_flash_program(uint32_t offset, const uint8_t *data, size_t length);
bool bosun_board_flash_erase(uint32_t offset, size_t length);

#ifdef __cplusplus
}
#endif
#endif
