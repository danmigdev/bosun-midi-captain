#ifndef BOSUN_TEST_UNIQUE_ID_H
#define BOSUN_TEST_UNIQUE_ID_H
#include <stddef.h>
#define PICO_UNIQUE_BOARD_ID_SIZE_BYTES 8
void pico_get_unique_board_id_string(char *buffer, size_t length);
#endif
