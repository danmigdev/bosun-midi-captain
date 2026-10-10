#ifndef BOSUN_USB_RX_TRACE_H
#define BOSUN_USB_RX_TRACE_H
#include <stdbool.h>
#include <stdint.h>

/* Called on core 0 at each logical data-session edge. The hardware's pending
 * OUT buffer remains tracked across the edge; only diagnostics are reset. */
void bosun_usb_rx_trace_session(uint32_t generation, bool connected);
#endif
