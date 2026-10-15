#include "app.h"

volatile U16 g_adcRaw;
U16 g_tempFiltered;
static U16 s_tempRaw;

void Sensor_Init(void)
{
    g_tempFiltered = 25U;
    s_tempRaw = 0U;
}

/* ADC 변환 완료 인터럽트 */
void ADC_IRQHandler(void)
{
    g_adcRaw = (U16)(*(volatile U16 *)0x40012440UL);
}

BOOL Sensor_ReadTemp(U16 *outTemp)
{
    BOOL ok = FALSE;
    U16 raw = g_adcRaw;
    if (raw < 4095U) {
        *outTemp = Util_ScaleTemp(raw);
        ok = TRUE;
    }
    return ok;
}

void Sensor_Task(void)
{
    U16 t = 0U;
    if (Sensor_ReadTemp(&t) == TRUE) {
        s_tempRaw = t;
        /* 1차 IIR 필터 (가중치 3:1) */
        g_tempFiltered = (U16)((g_tempFiltered * 3U + s_tempRaw) / 4U);
    } else {
        Event_Dispatch(1U);
    }
}
