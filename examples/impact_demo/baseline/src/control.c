#include "app.h"

CtrlState_t g_ctrlState;
static U16 s_lastTemp;

void Ctrl_Init(void)
{
    g_ctrlState = CTRL_INIT;
    s_lastTemp = 0U;
}

/* A 함수: 과열 조건에 따라 B 함수(Heater_Off) 호출 */
void Ctrl_CheckOverheat(U16 temp)
{
    if (temp > TEMP_HIGH_LIMIT) {
        Heater_Off();
    } else {
        Heater_On();
    }
}

void Ctrl_Main10ms(void)
{
    U16 temp = g_tempFiltered;

    switch (g_ctrlState) {
    case CTRL_INIT:
        g_ctrlState = CTRL_NORMAL;
        break;
    case CTRL_NORMAL:
        Ctrl_CheckOverheat(temp);
        if (g_faultActive == TRUE) {
            g_ctrlState = CTRL_FAULT;
        }
        break;
    case CTRL_DEGRADED:
        Heater_Off();
        if (g_faultActive == FALSE) {
            g_ctrlState = CTRL_NORMAL;
        }
        break;
    case CTRL_FAULT:
        Heater_Off();
        if (g_faultActive == FALSE) {
            g_ctrlState = CTRL_NORMAL;
        }
        break;
    default:
        g_ctrlState = CTRL_INIT;
        break;
    }
    s_lastTemp = temp;
    Diag_Trace((U8)g_ctrlState);
}
