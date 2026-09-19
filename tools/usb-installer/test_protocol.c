#include "protocol.h"
#include <assert.h>
#include <string.h>
int main(void) {
    assert(bi_crc((const uint8_t *)"123456789", 9) == 0xcbf43926);
    assert(bi_crc(NULL, 0) == 0);
    assert(bi_request_valid(BI_WRITE, BI_FLASH - BI_SECTOR, BI_SECTOR));
    assert(!bi_request_valid(BI_WRITE, BI_FLASH, BI_SECTOR));
    assert(!bi_request_valid(BI_WRITE, UINT32_MAX - 4095, BI_SECTOR));
    assert(!bi_request_valid(BI_WRITE, 1, BI_SECTOR));
    assert(!bi_request_valid(BI_READ, 0, UINT32_MAX));
    assert(!bi_request_valid(BI_ARM, 0, 0));
    assert(!bi_request_valid(99, 0, 0));
    uint8_t header[BI_HEADER] = {0};
    bi_put(header, BI_MAGIC); bi_put(header + 8, BI_READ); bi_put(header + 16, BI_SECTOR);
    bi_put(header + 24, bi_crc(header, 24));
    assert(bi_header_valid(header)); assert(bi_input_bytes(header) == 0);
    header[12] ^= 1; assert(!bi_header_valid(header));
    return 0;
}
