#ifndef BOSUN_CDC_SESSION_H
#define BOSUN_CDC_SESSION_H

/* Called after tud_task. CDC callbacks and this task run on the same core;
 * no callback waits, sleeps, or touches application/configuration state. */
void bosun_cdc_task(void);

#endif
