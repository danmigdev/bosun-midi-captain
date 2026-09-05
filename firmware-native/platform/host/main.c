#define _POSIX_C_SOURCE 200809L
#include "bosun/application.h"
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

/* Only this executable emulates board I/O. It never discovers serial/MIDI
 * devices and binds TCP exclusively to IPv4 loopback. Storage remains the
 * same non-symlink-following POSIX backend used by native host tests. */
static bosun_application_t application;
static int listener = -1, client = -1;
static size_t io_chunk = BOSUN_APPLICATION_IO_BYTES;
static volatile sig_atomic_t stopping;
static struct timespec started;
static uint16_t framebuffer[BOSUN_DISPLAY_HEIGHT][BOSUN_DISPLAY_WIDTH];
static uint32_t led_colors[BOSUN_LED_COUNT], display_rows, led_frames;
static uint64_t midi_bytes[2];
static uint32_t usb_session_generation;
static FILE *midi_log;

static void stop(int signal_number) { (void)signal_number; stopping = 1; }
static bool nonblocking(int fd) {
    int flags = fcntl(fd, F_GETFL);
    return flags >= 0 && fcntl(fd, F_SETFL, flags | O_NONBLOCK) == 0;
}
static void disconnect(void) {
    if (client >= 0) { close(client); client = -1; ++usb_session_generation; }
}

bool bosun_board_init(const bosun_board_config_t *config) { (void)config; return true; }
void bosun_board_task(void) {
    int incoming = accept(listener, NULL, NULL);
    if (incoming < 0) return;
    if (client >= 0 || !nonblocking(incoming)) { close(incoming); return; }
    client = incoming; ++usb_session_generation;
}
uint32_t bosun_board_millis(void) {
    struct timespec now; (void)clock_gettime(CLOCK_MONOTONIC, &now);
    int64_t milliseconds = (int64_t)(now.tv_sec - started.tv_sec) * 1000 +
                          (now.tv_nsec - started.tv_nsec) / 1000000;
    return (uint32_t)milliseconds;
}
bool bosun_board_usb_connected(void) { return client >= 0; }
uint32_t bosun_board_usb_session_generation(void) { return usb_session_generation; }
bool bosun_board_midi_connected(bosun_midi_port_t port) {
    return port == BOSUN_MIDI_USB || port == BOSUN_MIDI_DIN;
}
size_t bosun_board_data_read(uint8_t *data, size_t capacity) {
    if (client < 0) return 0;
    if (capacity > io_chunk) capacity = io_chunk;
    ssize_t count = recv(client, data, capacity, 0);
    if (count > 0) return (size_t)count;
    if (count == 0 || (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR)) disconnect();
    return 0;
}
size_t bosun_board_data_write(const uint8_t *data, size_t length) {
    if (client < 0) return 0;
    if (length > io_chunk) length = io_chunk;
    ssize_t count = send(client, data, length, 0);
    if (count >= 0) return (size_t)count;
    if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) disconnect();
    return 0;
}
size_t bosun_board_console_write(const uint8_t *data, size_t length) {
    return fwrite(data, 1, length, stderr);
}
size_t bosun_board_midi_read(bosun_midi_port_t port, uint8_t *data, size_t capacity) {
    (void)port; (void)data; (void)capacity; return 0;
}
size_t bosun_board_midi_write(bosun_midi_port_t port, const uint8_t *data, size_t length) {
    if (port != BOSUN_MIDI_USB && port != BOSUN_MIDI_DIN) return 0;
    if (length > io_chunk) length = io_chunk;
    midi_bytes[port] += length;
    if (midi_log) {
        fprintf(midi_log, "%s", port == BOSUN_MIDI_USB ? "USB" : "DIN");
        for (size_t i = 0; i < length; ++i) fprintf(midi_log, " %02x", data[i]);
        fputc('\n', midi_log); fflush(midi_log);
    }
    return length;
}
uint32_t bosun_board_midi_rx_dropped(void) { return 0; }
uint16_t bosun_board_switches(void) { return 0; }
uint16_t bosun_board_expression_read(uint8_t jack) { (void)jack; return 0; }
bool bosun_board_expression_charge(uint8_t jack, bool high) { (void)jack; (void)high; return false; }
bool bosun_board_expression_release(uint8_t jack) { return jack == 1 || jack == 2; }
void bosun_board_leds_set(uint8_t index, uint32_t color) {
    if (index < BOSUN_LED_COUNT) led_colors[index] = color;
}
bool bosun_board_leds_show(void) { ++led_frames; return true; }
uint32_t bosun_board_leds_get(uint8_t index) {
    return index < BOSUN_LED_COUNT ? led_colors[index] : 0;
}
bool bosun_board_display_rotation(uint16_t value) { return value <= 270 && value % 90 == 0; }
void bosun_board_display_brightness(uint8_t value) { (void)value; }
bool bosun_board_display_blit_rgb565(int16_t x, int16_t y, uint16_t width, uint16_t height,
                                   const uint16_t *pixels, uint16_t stride) {
    if (!pixels || x < 0 || y < 0 || x + width > BOSUN_DISPLAY_WIDTH ||
        y + height > BOSUN_DISPLAY_HEIGHT || stride < width) return false;
    for (unsigned row = 0; row < height; ++row) {
        memcpy(&framebuffer[y + row][x], pixels + row * stride, width * sizeof *pixels);
        ++display_rows;
    }
    return true;
}
bool bosun_board_watchdog_enable(uint32_t timeout) { return timeout && timeout <= 8000; }
void bosun_board_watchdog_feed(void) {}
void bosun_board_reboot(bool bootloader) { (void)bootloader; stopping = 1; }

static bool number(const char *text, unsigned maximum, unsigned *output) {
    if (!text || !*text || *text == '-') return false;
    char *end; errno = 0;
    unsigned long value = strtoul(text, &end, 10);
    if (errno || *end || value > maximum) return false;
    *output = (unsigned)value; return true;
}
static void usage(const char *program) {
    fprintf(stderr, "Usage: %s --root EXISTING_DIRECTORY [--port 9877] [--io-chunk 256] [--midi-log FILE]\n"
        "Loopback-only experimental emulator. No serial/MIDI hardware. Never formats storage.\n", program);
}

int main(int argc, char **argv) {
    const char *root = NULL, *log_path = NULL;
    unsigned port = 9877;
    for (int i = 1; i < argc; ++i) {
        if (!strcmp(argv[i], "--help")) { usage(argv[0]); return 0; }
        if (i + 1 >= argc) { usage(argv[0]); return 2; }
        if (!strcmp(argv[i], "--root")) root = argv[++i];
        else if (!strcmp(argv[i], "--midi-log")) log_path = argv[++i];
        else if (!strcmp(argv[i], "--port")) {
            if (!number(argv[++i], 65535, &port)) { usage(argv[0]); return 2; }
        } else if (!strcmp(argv[i], "--io-chunk")) {
            unsigned chunk;
            if (!number(argv[++i], BOSUN_APPLICATION_IO_BYTES, &chunk) || !chunk) { usage(argv[0]); return 2; }
            io_chunk = chunk;
        } else { usage(argv[0]); return 2; }
    }
    if (!root) { usage(argv[0]); return 2; }
    /* Validate before opening logs/listeners. Mount only reads the existing
     * directory; the application repeats this to share RP boot semantics. */
    if (!bosun_store_mount(root)) { fprintf(stderr, "Storage root unavailable: %s\n", root); return 2; }
    if (log_path && !(midi_log = fopen(log_path, "wx"))) { perror("midi log (must be new)"); return 2; }
    struct sigaction action;
    memset(&action, 0, sizeof action); action.sa_handler = stop;
    sigemptyset(&action.sa_mask);
    (void)sigaction(SIGTERM, &action, NULL); (void)sigaction(SIGINT, &action, NULL);
    action.sa_handler = SIG_IGN; (void)sigaction(SIGPIPE, &action, NULL);
    (void)clock_gettime(CLOCK_MONOTONIC, &started);
    listener = socket(AF_INET, SOCK_STREAM, 0);
    if (listener < 0) { perror("socket"); if (midi_log) fclose(midi_log); return 1; }
    struct sockaddr_in address;
    memset(&address, 0, sizeof address);
    address.sin_family = AF_INET; address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    address.sin_port = htons((uint16_t)port);
    if (bind(listener, (struct sockaddr *)&address, sizeof address) ||
        listen(listener, 4) || !nonblocking(listener)) {
        perror("listen loopback"); close(listener); if (midi_log) fclose(midi_log); return 1;
    }
    socklen_t address_length = sizeof address;
    if (getsockname(listener, (struct sockaddr *)&address, &address_length) ||
        !bosun_application_init(&application, root)) {
        fprintf(stderr, "Emulator initialization failed\n");
        close(listener); if (midi_log) fclose(midi_log); return 1;
    }
    printf("READY tcp://127.0.0.1:%u storage=%s\n", ntohs(address.sin_port),
           bosun_store_ready() ? "ready" : "unavailable"); fflush(stdout);
    const struct timespec pause = {0, 1000000};
    while (!stopping) { bosun_application_tick(&application); (void)nanosleep(&pause, NULL); }
    disconnect(); close(listener);
    if (midi_log) fclose(midi_log);
    fprintf(stderr, "STOP ticks=%lu display_rows=%lu led_frames=%lu midi_usb=%llu midi_din=%llu\n",
        (unsigned long)application.ticks, (unsigned long)display_rows, (unsigned long)led_frames,
        (unsigned long long)midi_bytes[0], (unsigned long long)midi_bytes[1]);
    return 0;
}
