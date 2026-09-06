/* SPDX-License-Identifier: GPL-3.0-or-later */
#include "bosun/board.h"
#include "cdc_session.h"
#include "board_contracts.h"
#include "uart_tx.h"
#include <string.h>
#include "hardware/adc.h"
#include "hardware/clocks.h"
#include "hardware/dma.h"
#include "hardware/flash.h"
#include "hardware/gpio.h"
#include "hardware/irq.h"
#include "hardware/pio.h"
#include "hardware/pwm.h"
#include "hardware/spi.h"
#include "hardware/sync.h"
#include "hardware/uart.h"
#include "hardware/watchdog.h"
#include "pico/bootrom.h"
#include "pico/platform.h"
#include "pico/time.h"
#include "tusb.h"
#include "ws2812.pio.h"

/* Verified against firmware/lib/captain/board.py. Never use Pico's on-board
 * LED helpers: GP25 is switch 2 on the Captain. */
enum { LED_PIN = 7, TFT_PWM = 8, TFT_DC = 12, TFT_CS = 13,
       TFT_SCK = 14, TFT_MOSI = 15, MIDI_TX = 16, MIDI_RX = 17,
       EXP1 = 27, EXP2 = 28, UART_RX_SIZE = 2048 };
static const uint8_t switch_pins[BOSUN_SWITCH_COUNT] = {
    1, 25, 24, 23, 20, 9, 10, 11, 18, 19
};
_Static_assert(PICO_FLASH_SIZE_BYTES > BOSUN_STORAGE_BYTES, "Flash too small for storage reserve");

static bool initialised;
static bool display_ready;
static bool watchdog_enabled;
static uint16_t display_col_offset, display_row_offset;
static uint8_t display_line[BOSUN_DISPLAY_WIDTH * 2];
/* DMA keeps receiving during flash operations with IRQs disabled. Hardware
 * ring addressing requires the buffer's alignment to equal its power-of-two
 * size. 2048 bytes covers about 655 ms at MIDI's 31250 baud (8N1). */
static _Alignas(UART_RX_SIZE) volatile uint8_t din_rx[UART_RX_SIZE];
static bosun_uart_tx_t din_tx;
static int din_rx_dma = -1;
static uint64_t din_rx_completed;
static bosun_dma_rx_tracker_t din_rx_tracker;
static uint32_t led_colors[BOSUN_LED_COUNT], led_dma_words[BOSUN_LED_COUNT];
static uint led_sm;
static int led_dma = -1;
static uint64_t led_ready_at;
static _Alignas(4) uint8_t flash_page[FLASH_PAGE_SIZE];

static size_t din_rx_poll(void) {
    if (din_rx_dma < 0) return 0;
    dma_channel_hw_t *channel = dma_channel_hw_addr((uint)din_rx_dma);
    /* UINT32_MAX bytes lasts over 15 days. Re-arm well before it expires;
     * abort pauses DMA only for these register writes, while UART's FIFO
     * continues receiving. Preserve the current ring write address. */
    if (channel->transfer_count <= 8192u) {
        dma_channel_abort((uint)din_rx_dma);
        din_rx_completed = bosun_dma_rx_produced(din_rx_completed, channel->transfer_count);
        dma_channel_set_trans_count((uint)din_rx_dma, UINT32_MAX, true);
    }
    uint64_t produced = bosun_dma_rx_produced(din_rx_completed, channel->transfer_count);
    size_t available = bosun_dma_rx_available(&din_rx_tracker, produced, UART_RX_SIZE);
    if (uart_get_hw(uart0)->rsr & 15u) {
        /* A hardware framing/overrun error has no reliable byte location.
         * Discard the pending span and report the loss to the application. */
        uart_get_hw(uart0)->rsr = 0;
        din_rx_tracker.dropped += (uint32_t)available + 1u;
        din_rx_tracker.consumed = produced;
        return 0;
    }
    return available;
}

static bool din_writable(void *context) { (void)context; return uart_is_writable(uart0); }
static void din_write(void *context, uint8_t byte) { (void)context; uart_get_hw(uart0)->dr = byte; }
static void din_interrupt(void *context, bool enabled) {
    (void)context; uart_set_irq_enables(uart0, false, enabled);
}
static const bosun_uart_tx_fifo_t din_fifo = {din_writable, din_write, din_interrupt};
static void din_irq(void) { bosun_uart_tx_service(&din_tx, &din_fifo, NULL); }

void bosun_board_task(void) {
    din_rx_poll();
    tud_task();
    if (tud_cdc_n_connected(0)) tud_cdc_n_write_flush(0);
    bosun_cdc_task();
}

static void display_delay(uint32_t duration_ms) {
    uint64_t until = time_us_64() + (uint64_t)duration_ms * 1000;
    while (time_us_64() < until) {
        bosun_board_task();
        sleep_us(1000);
    }
}

static void display_command(uint8_t command, const uint8_t *data, size_t length) {
    gpio_put(TFT_CS, 0);
    gpio_put(TFT_DC, 0);
    spi_write_blocking(spi1, &command, 1);
    if (length) {
        gpio_put(TFT_DC, 1);
        spi_write_blocking(spi1, data, length);
    }
    gpio_put(TFT_CS, 1);
}

bool bosun_board_display_rotation(uint16_t degrees) {
    if (!display_ready || degrees > 270 || degrees % 90) return false;
    /* ST7789 has 240x320 RAM and an 80-row crop. The MX/MY base orientation
     * matches Adafruit's CircuitPython ST7789 (MADCTL C0, rowstart 80), then
     * adjusts the crop when rotating the controller's addressing axes. */
    static const uint8_t madctl[] = {0xc0, 0xa0, 0x00, 0x60};
    unsigned rotation = degrees / 90;
    display_col_offset = rotation == 1 ? 80 : 0;
    display_row_offset = rotation == 0 ? 80 : 0;
    display_command(0x36, &madctl[rotation], 1);
    return true;
}

void bosun_board_display_brightness(uint8_t brightness) {
    if (display_ready) pwm_set_gpio_level(TFT_PWM, (uint16_t)brightness * 257u);
}

bool bosun_board_init(const bosun_board_config_t *config) {
    uint16_t rotation = config ? config->rotation : 180;
    uint8_t brightness = config ? config->brightness : 255;
    if (initialised || rotation > 270 || rotation % 90) return false;
    for (unsigned i = 0; i < BOSUN_SWITCH_COUNT; ++i) {
        gpio_init(switch_pins[i]);
        gpio_set_dir(switch_pins[i], GPIO_IN);
        gpio_pull_up(switch_pins[i]);
    }
    adc_init();
    adc_gpio_init(EXP1);
    adc_gpio_init(EXP2);

    uart_init(uart0, 31250);
    gpio_set_function(MIDI_TX, GPIO_FUNC_UART);
    gpio_set_function(MIDI_RX, GPIO_FUNC_UART);
    uart_set_format(uart0, 8, 1, UART_PARITY_NONE);
    uart_set_hw_flow(uart0, false, false);
    uart_set_fifo_enabled(uart0, true);
    irq_set_exclusive_handler(UART0_IRQ, din_irq);
    irq_set_enabled(UART0_IRQ, true);
    uart_set_irq_enables(uart0, false, false);

    int sm = pio_claim_unused_sm(pio0, false);
    if (sm < 0) return false;
    led_dma = dma_claim_unused_channel(false);
    if (led_dma < 0 || !pio_can_add_program(pio0, &bosun_ws2812_program)) {
        if (led_dma >= 0) dma_channel_unclaim((uint)led_dma);
        pio_sm_unclaim(pio0, (uint)sm);
        return false;
    }
    led_sm = (uint)sm;
    uint program = pio_add_program(pio0, &bosun_ws2812_program);
    pio_gpio_init(pio0, LED_PIN);
    pio_sm_set_consecutive_pindirs(pio0, led_sm, LED_PIN, 1, true);
    pio_sm_config pio_config = bosun_ws2812_program_get_default_config(program);
    sm_config_set_sideset_pins(&pio_config, LED_PIN);
    sm_config_set_out_shift(&pio_config, false, true, 24);
    sm_config_set_fifo_join(&pio_config, PIO_FIFO_JOIN_TX);
    sm_config_set_clkdiv(&pio_config, (float)clock_get_hz(clk_sys) / (800000.0f * 10.0f));
    pio_sm_init(pio0, led_sm, program, &pio_config);
    pio_sm_set_enabled(pio0, led_sm, true);
    dma_channel_config dma_config = dma_channel_get_default_config((uint)led_dma);
    channel_config_set_transfer_data_size(&dma_config, DMA_SIZE_32);
    channel_config_set_read_increment(&dma_config, true);
    channel_config_set_write_increment(&dma_config, false);
    channel_config_set_dreq(&dma_config, pio_get_dreq(pio0, led_sm, true));
    dma_channel_configure((uint)led_dma, &dma_config, &pio0->txf[led_sm],
                          led_dma_words, 0, false);

    din_rx_dma = dma_claim_unused_channel(false);
    if (din_rx_dma < 0) return false;
    dma_channel_config rx_config = dma_channel_get_default_config((uint)din_rx_dma);
    channel_config_set_transfer_data_size(&rx_config, DMA_SIZE_8);
    channel_config_set_read_increment(&rx_config, false);
    channel_config_set_write_increment(&rx_config, true);
    channel_config_set_ring(&rx_config, true, 11); /* 2^11 = UART_RX_SIZE */
    channel_config_set_dreq(&rx_config, uart_get_dreq(uart0, false));
    /* Continue draining after a framing error; din_rx_poll discards the
     * affected pending span instead of leaving DMA permanently stalled. */
    uart_get_hw(uart0)->dmacr = UART_UARTDMACR_RXDMAE_BITS | UART_UARTDMACR_DMAONERR_BITS;
    dma_channel_configure((uint)din_rx_dma, &rx_config, (void *)din_rx,
                          &uart_get_hw(uart0)->dr, UINT32_MAX, true);

    spi_init(spi1, 24000000);
    spi_set_format(spi1, 8, SPI_CPOL_0, SPI_CPHA_0, SPI_MSB_FIRST);
    gpio_set_function(TFT_SCK, GPIO_FUNC_SPI);
    gpio_set_function(TFT_MOSI, GPIO_FUNC_SPI);
    gpio_init(TFT_CS); gpio_set_dir(TFT_CS, GPIO_OUT); gpio_put(TFT_CS, 1);
    gpio_init(TFT_DC); gpio_set_dir(TFT_DC, GPIO_OUT); gpio_put(TFT_DC, 1);
    gpio_set_function(TFT_PWM, GPIO_FUNC_PWM);
    pwm_config pwm = pwm_get_default_config();
    pwm_config_set_wrap(&pwm, 65535);
    pwm_config_set_clkdiv(&pwm, (float)clock_get_hz(clk_sys) / (1000.0f * 65536.0f));
    pwm_init(pwm_gpio_to_slice_num(TFT_PWM), &pwm, true);
    pwm_set_gpio_level(TFT_PWM, 0);
    if (!tud_init(0)) return false;
    display_command(0x01, NULL, 0); /* software reset; no reset pin on this board */
    display_delay(150);
    display_command(0x11, NULL, 0); /* sleep out */
    display_delay(500);
    const uint8_t rgb565 = 0x55;
    display_command(0x3a, &rgb565, 1);
    display_delay(10);
    display_ready = true;
    bosun_board_display_rotation(rotation);
    display_command(0x21, NULL, 0); /* panel inversion matches CP driver */
    display_command(0x13, NULL, 0); /* normal mode */
    display_delay(10);
    display_command(0x29, NULL, 0); /* display on */
    display_delay(500);
    bosun_board_display_fill_rect(0, 0, BOSUN_DISPLAY_WIDTH, BOSUN_DISPLAY_HEIGHT, 0);
    bosun_board_display_brightness(brightness);
    initialised = true;
    bosun_board_leds_show();
    return true;
}

uint32_t bosun_board_millis(void) { return to_ms_since_boot(get_absolute_time()); }
bool bosun_board_midi_connected(bosun_midi_port_t port) {
    return port == BOSUN_MIDI_USB ? tud_mounted() : port == BOSUN_MIDI_DIN;
}

static size_t cdc_write(uint8_t interface, const uint8_t *data, size_t length) {
    if (!data || !length || !tud_cdc_n_connected(interface)) return 0;
    uint32_t available = tud_cdc_n_write_available(interface);
    if (length > available) length = available;
    size_t accepted = tud_cdc_n_write(interface, data, length);
    tud_cdc_n_write_flush(interface);
    return accepted;
}
size_t bosun_board_console_write(const uint8_t *data, size_t length) { return cdc_write(0, data, length); }

size_t bosun_board_midi_read(bosun_midi_port_t port, uint8_t *data, size_t capacity) {
    if (!data || !capacity) return 0;
    if (port == BOSUN_MIDI_USB) return tud_midi_stream_read(data, capacity);
    if (port != BOSUN_MIDI_DIN) return 0;
    size_t available = din_rx_poll();
    if (capacity > available) capacity = available;
    size_t count = 0;
    while (count < capacity) {
        data[count++] = din_rx[din_rx_tracker.consumed & (UART_RX_SIZE - 1)];
        ++din_rx_tracker.consumed;
    }
    return count;
}

size_t bosun_board_midi_write(bosun_midi_port_t port, const uint8_t *data, size_t length) {
    if (!data || !length) return 0;
    if (port == BOSUN_MIDI_USB) return tud_midi_stream_write(0, data, length);
    if (port != BOSUN_MIDI_DIN) return 0;
    uint32_t interrupts = save_and_disable_interrupts();
    size_t count = bosun_uart_tx_write(&din_tx, &din_fifo, NULL, data, length);
    restore_interrupts(interrupts);
    return count;
}
uint32_t bosun_board_midi_rx_dropped(void) { return din_rx_tracker.dropped; }

uint16_t bosun_board_switches(void) {
    uint16_t mask = 0;
    uint32_t levels = gpio_get_all();
    for (unsigned i = 0; i < BOSUN_SWITCH_COUNT; ++i)
        if (!(levels & (1u << switch_pins[i]))) mask |= (uint16_t)(1u << i);
    return mask;
}
uint16_t bosun_board_expression_read(uint8_t jack) {
    if (jack != 1 && jack != 2) return 0;
    adc_select_input(jack); /* GP27/28 = ADC1/2 */
    uint32_t sample = adc_read();
    return (uint16_t)((sample * 65535u + 2047u) / 4095u);
}
bool bosun_board_expression_charge(uint8_t jack, bool high) {
    if (!initialised || (jack != 1 && jack != 2)) return false;
    uint pin = jack == 1 ? EXP1 : EXP2;
    gpio_init(pin); /* SIO input first: set the intended rail before enabling output. */
    gpio_disable_pulls(pin);
    gpio_put(pin, high);
    gpio_set_dir(pin, GPIO_OUT);
    return true;
}
bool bosun_board_expression_release(uint8_t jack) {
    if (!initialised || (jack != 1 && jack != 2)) return false;
    uint pin = jack == 1 ? EXP1 : EXP2;
    gpio_set_dir(pin, GPIO_IN);
    adc_gpio_init(pin); /* NULL function, no pulls, digital receiver disabled. */
    return true;
}
void bosun_board_leds_set(uint8_t index, uint32_t rgb24) {
    if (index < BOSUN_LED_COUNT) led_colors[index] = rgb24 & 0xffffffu;
}
uint32_t bosun_board_leds_get(uint8_t index) {
    if (index >= BOSUN_LED_COUNT) return 0;
    uint32_t grb = led_dma_words[index] >> 8;
    return ((grb & 0xff00u) << 8) | ((grb >> 8) & 0xff00u) | (grb & 0xffu);
}
bool bosun_board_leds_show(void) {
    if (!initialised || dma_channel_is_busy((uint)led_dma) || time_us_64() < led_ready_at) return false;
    for (unsigned i = 0; i < BOSUN_LED_COUNT; ++i) {
        uint32_t rgb = led_colors[i];
        uint32_t grb = ((rgb & 0x00ff00u) << 8) | ((rgb & 0xff0000u) >> 8) | (rgb & 0xffu);
        led_dma_words[i] = grb << 8;
    }
    /* DMA finishes while up to eight pixels remain in PIO's FIFO. Reserve
     * the whole 30*30 us transfer plus a 300 us low reset/latch interval. */
    led_ready_at = time_us_64() + BOSUN_LED_COUNT * 30u + 300u;
    dma_channel_set_read_addr((uint)led_dma, led_dma_words, false);
    dma_channel_set_trans_count((uint)led_dma, BOSUN_LED_COUNT, true);
    return true;
}

static void display_window(uint16_t x, uint16_t y, uint16_t width, uint16_t height) {
    uint16_t left = x + display_col_offset, top = y + display_row_offset;
    uint16_t right = left + width - 1, bottom = top + height - 1;
    uint8_t columns[] = {left >> 8, left & 255, right >> 8, right & 255};
    uint8_t rows[] = {top >> 8, top & 255, bottom >> 8, bottom & 255};
    display_command(0x2a, columns, sizeof(columns));
    display_command(0x2b, rows, sizeof(rows));
    display_command(0x2c, NULL, 0);
    gpio_put(TFT_CS, 0);
    gpio_put(TFT_DC, 1);
}
bool bosun_board_display_fill_rect(int16_t x, int16_t y, uint16_t width,
                                   uint16_t height, uint16_t rgb565) {
    if (!display_ready) return false;
    int32_t clipped_x = x, clipped_y = y;
    uint32_t clipped_w = width, clipped_h = height, skip_x, skip_y;
    if (!bosun_clip_rect(&clipped_x, &clipped_y, &clipped_w, &clipped_h, &skip_x, &skip_y)) return true;
    for (uint32_t i = 0; i < clipped_w; ++i) {
        display_line[2 * i] = (uint8_t)(rgb565 >> 8);
        display_line[2 * i + 1] = (uint8_t)rgb565;
    }
    display_window((uint16_t)clipped_x, (uint16_t)clipped_y, (uint16_t)clipped_w, (uint16_t)clipped_h);
    for (uint32_t row = 0; row < clipped_h; ++row) {
        spi_write_blocking(spi1, display_line, 2 * clipped_w);
        bosun_board_task();
    }
    gpio_put(TFT_CS, 1);
    return true;
}
bool bosun_board_display_blit_rgb565(int16_t x, int16_t y, uint16_t width,
                                    uint16_t height, const uint16_t *pixels, uint16_t stride) {
    if (!display_ready || !pixels || stride < width) return false;
    int32_t clipped_x = x, clipped_y = y;
    uint32_t clipped_w = width, clipped_h = height, skip_x, skip_y;
    if (!bosun_clip_rect(&clipped_x, &clipped_y, &clipped_w, &clipped_h, &skip_x, &skip_y)) return true;
    pixels += skip_y * stride + skip_x;
    display_window((uint16_t)clipped_x, (uint16_t)clipped_y, (uint16_t)clipped_w, (uint16_t)clipped_h);
    for (uint32_t row = 0; row < clipped_h; ++row) {
        for (uint32_t col = 0; col < clipped_w; ++col) {
            uint16_t pixel = pixels[row * stride + col];
            display_line[2 * col] = (uint8_t)(pixel >> 8);
            display_line[2 * col + 1] = (uint8_t)pixel;
        }
        spi_write_blocking(spi1, display_line, 2 * clipped_w);
        bosun_board_task();
    }
    gpio_put(TFT_CS, 1);
    return true;
}

bool bosun_board_watchdog_enable(uint32_t timeout_ms) {
    if (!timeout_ms || timeout_ms > 8000) return false;
    watchdog_enable(timeout_ms, true);
    watchdog_enabled = true;
    return true;
}
void bosun_board_watchdog_feed(void) { if (watchdog_enabled) watchdog_update(); }
void bosun_board_reboot(bool bootloader) {
    if (bootloader) reset_usb_boot(0, 0);
    else watchdog_reboot(0, 0, 10);
    while (true) tight_loop_contents();
}

uint32_t bosun_board_storage_offset(void) { return PICO_FLASH_SIZE_BYTES - BOSUN_STORAGE_BYTES; }
uint32_t bosun_board_storage_size(void) { return BOSUN_STORAGE_BYTES; }
static bool storage_range(uint32_t offset, size_t length, uint32_t alignment) {
    return get_core_num() == 0 && bosun_flash_range(PICO_FLASH_SIZE_BYTES, offset, length, alignment);
}
bool bosun_board_flash_read(uint32_t offset, uint8_t *data, size_t length) {
    if (!data || !storage_range(offset, length, 1)) return false;
    memcpy(data, (const uint8_t *)(XIP_BASE + offset), length);
    return true;
}
bool bosun_board_flash_program(uint32_t offset, const uint8_t *data, size_t length) {
    if (!data || !storage_range(offset, length, FLASH_PAGE_SIZE)) return false;
    for (size_t done = 0; done < length; done += FLASH_PAGE_SIZE) {
        memcpy(flash_page, data + done, FLASH_PAGE_SIZE);
        bosun_board_task();
        bosun_board_watchdog_feed();
        uint32_t interrupts = save_and_disable_interrupts();
        flash_range_program(offset + done, flash_page, FLASH_PAGE_SIZE);
        restore_interrupts(interrupts);
        bosun_board_task();
        if (memcmp((const uint8_t *)(XIP_BASE + offset + done), flash_page, FLASH_PAGE_SIZE)) return false;
    }
    return true;
}
bool bosun_board_flash_erase(uint32_t offset, size_t length) {
    if (!storage_range(offset, length, FLASH_SECTOR_SIZE)) return false;
    for (size_t done = 0; done < length; done += FLASH_SECTOR_SIZE) {
        bosun_board_task();
        bosun_board_watchdog_feed();
        uint32_t interrupts = save_and_disable_interrupts();
        flash_range_erase(offset + done, FLASH_SECTOR_SIZE);
        restore_interrupts(interrupts);
        bosun_board_task();
        const uint32_t *words = (const uint32_t *)(XIP_BASE + offset + done);
        for (size_t i = 0; i < FLASH_SECTOR_SIZE / sizeof(*words); ++i)
            if (words[i] != UINT32_MAX) return false;
    }
    return true;
}
