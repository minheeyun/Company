#include "Std_Types.h"
static uint8 __far * g_ptr;
static uint8 g_limit = 10U;
FUNC(void, AP_CODE) Ap_Main(uint8 in)
{
    if (in > g_limit) {
        g_ptr = 0;
    }
}
