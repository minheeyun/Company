#include "app.h"

BOOL g_faultActive;
static U16 s_overTempCnt;

void Fault_Init(void)
{
    g_faultActive = FALSE;
    s_overTempCnt = 0U;
}

/* 과열 고장 판정: 디바운스 후 확정, 해제 온도 이하에서 해제 */
void Fault_Monitor(U16 temp)
{
    if (temp > TEMP_HIGH_LIMIT) {
        s_overTempCnt++;
        if (s_overTempCnt >= FAULT_DEBOUNCE_CNT) {
            g_faultActive = TRUE;
        }
    } else {
        s_overTempCnt = 0U;
        if (temp < FAULT_CLEAR_TEMP) {
            g_faultActive = FALSE;
        }
    }
}
