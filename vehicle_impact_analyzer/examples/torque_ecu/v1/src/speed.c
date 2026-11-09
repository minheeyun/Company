#include "speed.h"
#include "filter.h"
#include "os.h"

volatile uint16_t g_rx_speed_raw;
volatile bool g_rx_valid;
static Flt_State_t s_spd_flt = { 0U, 2U };
static uint16_t s_filtered;

/* CAN 수신 인터럽트: 차속 원신호 저장 */
void Can_RxIsr(uint16_t raw)
{
    g_rx_speed_raw = raw;
    g_rx_valid = true;
}

/* 차속 신호 처리(10ms) */
void Spd_Process(void)
{
    uint16_t raw;
    SuspendAllInterrupts();
    raw = g_rx_speed_raw;
    ResumeAllInterrupts();
    uint16_t v = Flt_LowPass(&s_spd_flt, raw);
    if (v > SPD_MAX_KPH) {
        v = SPD_MAX_KPH;
    }
    s_filtered = v;
}

void Spd_GetFiltered(uint16_t *out)
{
    *out = s_filtered;
}
