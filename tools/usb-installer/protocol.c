#include "protocol.h"
uint32_t bi_word(const uint8_t *p) {
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}
void bi_put(uint8_t *p, uint32_t v) {
    for (unsigned i = 0; i < 4; ++i) p[i] = (uint8_t)(v >> (i * 8));
}
uint32_t bi_crc(const uint8_t *p, size_t size) {
    uint32_t crc = UINT32_MAX;
    for (size_t i = 0; i < size; ++i) {
        crc ^= p[i];
        for (unsigned b = 0; b < 8; ++b) crc = (crc >> 1) ^ (0xedb88320u & (0u - (crc & 1u)));
    }
    return ~crc;
}
bool bi_header_valid(const uint8_t *h) {
    return bi_word(h) == BI_MAGIC && bi_word(h + 24) == bi_crc(h, 24);
}
size_t bi_input_bytes(const uint8_t *h) {
    uint32_t command = bi_word(h + 8);
    return command == BI_WRITE || command == BI_ARM ? bi_word(h + 16) : 0;
}
bool bi_request_valid(uint32_t cmd, uint32_t offset, uint32_t length) {
    switch (cmd) {
    case BI_INFO: case BI_REBOOT: case BI_BOOTSEL: return offset == 0 && length == 0;
    case BI_ARM: return offset == 0 && length == 8;
    case BI_READ: return length > 0 && length <= BI_SECTOR && offset <= BI_FLASH - length;
    case BI_WRITE: return length == BI_SECTOR && offset % BI_SECTOR == 0 && offset <= BI_FLASH - length;
    default: return false;
    }
}
