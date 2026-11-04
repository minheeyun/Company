#include "Std_Types.h"
#pragma ghs section bss=".lib_bss"
static VAR(uint16, LIB_VAR) s_cnt;
__interrupt void Lib_Isr(void)
{
    s_cnt = (uint16)(s_cnt + 2U);
}
FUNC(uint16, LIB_CODE) Lib_Get(void)
{
    return s_cnt;
}
