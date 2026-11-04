#include "cfg.h"
typedef unsigned char U8; typedef unsigned short U16; typedef unsigned int U32;
typedef enum { ST_IDLE = 0, ST_RUN = 1, ST_ERR = 2 } State_t;
void Dem_SetEventStatus(U8 id, U8 st);
void Mcu_PerformReset(void);
U16 Adc_Read(U8 ch);
static State_t s_state = ST_IDLE;
static U8 s_retryCnt;
static U16 s_timer_tm;
U16 g_limit = 100U;
U8 g_flag;
U16 g_volt;
U8 Check(U16 v, U8 mode) { return (U8)(v > g_limit); }
void App_Main(void)
{
    U16 v = Adc_Read(1U);
    g_volt = v;
    s_timer_tm++;
    if (s_state == ST_IDLE) {
        if (v > g_limit) { s_state = ST_RUN; }
    } else if (s_state == ST_RUN) {
        if (Check(v, 1U) != 0U) { s_retryCnt++; }
        if (s_retryCnt >= RETRY_MAX) { s_state = ST_ERR; Dem_SetEventStatus(1U, 1U); s_retryCnt = 0U; }
    } else {
        Mcu_PerformReset();
    }
    if (s_timer_tm > 50U) { s_timer_tm = 0U; g_flag = 1U; }
#if (APP_DIAG_ENABLE == STD_ON)
    g_flag = 2U;
#endif
}
void Other(void) { if (g_volt > g_limit) { g_flag = 3U; } }
