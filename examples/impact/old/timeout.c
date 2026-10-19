#include <stdint.h>
#include "config.h"

static uint8_t g_timeout;
static uint8_t g_faultLatched;

void UpdateTimeout(uint16_t tick)
{
    g_timeout = (tick >= TIMEOUT_TICKS);
}

void UpdateDiagnostic(void)
{
    if (g_timeout != 0U) {
        g_faultLatched = 1U;
    }
}

uint8_t ReadStatus(void)
{
    return g_faultLatched;
}
