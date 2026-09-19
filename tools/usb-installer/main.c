/* RAM-only USB installer. No flash writes at startup or on an incomplete command. */
#include "protocol.h"
#include <stdio.h>
#include <string.h>
#include "pico/stdlib.h"
#include "pico/stdio_usb.h"
#include "pico/bootrom.h"
#include "hardware/flash.h"
#include "hardware/sync.h"
#include "hardware/watchdog.h"

static uint8_t payload[BI_SECTOR], uid[8];
char bi_usb_serial[17];
static bool armed;
static uint32_t capacity;

static bool receive(uint8_t *data, size_t count) {
    for (size_t i = 0; i < count; ++i) {
        int c = getchar_timeout_us(2000000);
        if (c < 0) { armed = false; return false; }
        data[i] = (uint8_t)c;
    }
    return true;
}
static void reply(uint32_t sequence, uint32_t command, uint32_t status, size_t length) {
    uint8_t header[BI_HEADER];
    bi_put(header, BI_MAGIC); bi_put(header + 4, sequence);
    bi_put(header + 8, command | 0x80000000u); bi_put(header + 12, status);
    bi_put(header + 16, (uint32_t)length); bi_put(header + 20, bi_crc(payload, length));
    bi_put(header + 24, bi_crc(header, 24));
    fwrite(header, 1, sizeof header, stdout);
    if (length) fwrite(payload, 1, length, stdout);
    fflush(stdout);
}
int main(void) {
    /* NO_FLASH helpers must initialise generic XIP before accessing the chip. */
    flash_start_xip();
    /* pico_unique_id's pre-main constructor returns EE placeholders in NO_FLASH
     * builds. Query the real chip only AFTER generic XIP/pads are initialised,
     * and use that identity for both CDC descriptors and the installer protocol. */
    uint8_t id_command[13] = {0x4b}, id_reply[13];
    flash_do_cmd(id_command, id_reply, sizeof id_command);
    memcpy(uid, id_reply + 5, sizeof uid);
    static const char hex[] = "0123456789ABCDEF";
    for (unsigned i = 0; i < sizeof uid; ++i) {
        bi_usb_serial[i * 2] = hex[uid[i] >> 4];
        bi_usb_serial[i * 2 + 1] = hex[uid[i] & 15];
    }
    uint8_t command[4] = {0x9f, 0, 0, 0}, jedec[4];
    flash_do_cmd(command, jedec, sizeof command);
    capacity = jedec[3] == 23 ? BI_FLASH : 0;
    stdio_usb_init();
    stdio_set_translate_crlf(&stdio_usb, false);
    uint32_t magic = 0;
    for (;;) {
        int c = getchar_timeout_us(100000);
        if (c < 0) { if (!stdio_usb_connected()) armed = false; continue; }
        magic = (magic >> 8) | ((uint32_t)(uint8_t)c << 24);
        if (magic != BI_MAGIC) continue;
        magic = 0;
        uint8_t header[BI_HEADER]; bi_put(header, BI_MAGIC);
        if (!receive(header + 4, BI_HEADER - 4) || !bi_header_valid(header)) { armed = false; continue; }
        uint32_t seq = bi_word(header + 4), cmd = bi_word(header + 8);
        uint32_t offset = bi_word(header + 12), length = bi_word(header + 16);
        if (!bi_request_valid(cmd, offset, length)) { armed = false; reply(seq, cmd, BI_INVALID, 0); continue; }
        size_t incoming = bi_input_bytes(header);
        if (!receive(payload, incoming)) continue;
        if (bi_crc(payload, incoming) != bi_word(header + 20)) {
            armed = false; reply(seq, cmd, BI_CHECKSUM, 0); continue;
        }
        if (cmd == BI_INFO) {
            memcpy(payload, uid, sizeof uid); bi_put(payload + 8, capacity); bi_put(payload + 12, BI_VERSION);
            reply(seq, cmd, BI_OK, 16);
        } else if (cmd == BI_ARM) {
            armed = capacity == BI_FLASH && !memcmp(payload, uid, sizeof uid);
            reply(seq, cmd, armed ? BI_OK : BI_LOCKED, 0);
        } else if (cmd == BI_READ) {
            if (capacity != BI_FLASH) { reply(seq, cmd, BI_LOCKED, 0); continue; }
            memcpy(payload, (const void *)(XIP_BASE + offset), length);
            reply(seq, cmd, BI_OK, length);
        } else if (cmd == BI_WRITE) {
            if (!armed || capacity != BI_FLASH) { reply(seq, cmd, BI_LOCKED, 0); continue; }
            uint32_t interrupts = save_and_disable_interrupts();
            flash_range_erase(offset, BI_SECTOR);
            flash_range_program(offset, payload, BI_SECTOR);
            restore_interrupts(interrupts);
            uint32_t status = memcmp(payload, (const void *)(XIP_BASE + offset), BI_SECTOR) ? BI_VERIFY : BI_OK;
            if (status) armed = false;
            reply(seq, cmd, status, 0);
        } else {
            armed = false; reply(seq, cmd, BI_OK, 0); sleep_ms(100);
            if (cmd == BI_BOOTSEL) reset_usb_boot(0, 0);
            else watchdog_reboot(0, 0, 10);
            for (;;) tight_loop_contents();
        }
    }
}
