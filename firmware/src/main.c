#include "bosun/application.h"

static bosun_application_t application;

int main(void) {
    if (!bosun_application_init(&application, NULL)) {
        /* An unavailable filesystem is not fatal; only a failed board or
         * watchdog initialization enters this non-destructive idle path. */
        static const uint8_t failure[] = "Bosun native: board initialization failed\r\n";
        for (;;) {
            bosun_board_task();
            (void)bosun_board_console_write(failure, sizeof failure - 1);
        }
    }
    for (;;) bosun_application_tick(&application);
}
