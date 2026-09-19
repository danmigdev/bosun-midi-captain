#ifndef BOSUN_INSTALLER_PROTOCOL_H
#define BOSUN_INSTALLER_PROTOCOL_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
enum { BI_MAGIC = 0x31555342, BI_HEADER = 28, BI_SECTOR = 4096,
       BI_FLASH = 8 * 1024 * 1024, BI_VERSION = 1 };
enum { BI_INFO = 1, BI_READ = 2, BI_ARM = 3, BI_WRITE = 4, BI_REBOOT = 5, BI_BOOTSEL = 6 };
enum { BI_OK = 0, BI_INVALID = 1, BI_LOCKED = 2, BI_CHECKSUM = 3, BI_VERIFY = 4 };
uint32_t bi_crc(const uint8_t *data, size_t size);
uint32_t bi_word(const uint8_t *data);
void bi_put(uint8_t *data, uint32_t value);
bool bi_header_valid(const uint8_t *header);
size_t bi_input_bytes(const uint8_t *header);
bool bi_request_valid(uint32_t command, uint32_t offset, uint32_t length);
#endif
