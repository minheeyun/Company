#include "diag.h"
#include "speed.h"
#include "torque.h"

bool g_sensor_fault;
uint8_t g_dtc_sensor;
uint8_t g_dtc_plaus;
static uint8_t s_fault_cnt;
static uint8_t s_events;

static void Diag_OnSensorFault(void)
{
    g_dtc_sensor = 1U;
}

static void Diag_OnPlausibility(void)
{
    g_dtc_plaus = 1U;
}

/* 진단 이벤트 콜백 테이블(직접 호출 없이 함수 포인터로 실행) */
static const Diag_EventEntry_t Diag_Events[2] = {
    { 0U, Diag_OnSensorFault },
    { 1U, Diag_OnPlausibility },
};

/* 센서 고장 디바운스: 연속 고장 횟수가 기준을 넘으면 고장 확정 */
void Diag_UpdateSensor(bool raw_fault)
{
    if (raw_fault) {
        if (s_fault_cnt < 255U) {
            s_fault_cnt++;
        }
    } else {
        s_fault_cnt = 0U;
    }
    if (s_fault_cnt > DIAG_DEBOUNCE_LIMIT) {
        g_sensor_fault = true;
        s_events |= 0x01U;
    } else if (s_fault_cnt == 0U) {
        g_sensor_fault = false;
    }
}

bool Diag_IsSensorFault(void)
{
    return g_sensor_fault;
}

/* 차속 타당성 및 토크 제한 기록 감시 */
static void Diag_CheckPlausibility(void)
{
    uint16_t spd = 0U;
    Spd_GetFiltered(&spd);
    if ((spd > SPD_MAX_KPH) || (g_tq_state.last_limit > 100)) {
        s_events |= 0x02U;
    }
}

/* 진단 메인 함수(10ms) */
void Diag_MainFunction(void)
{
    uint8_t i;
    Diag_UpdateSensor(!g_rx_valid);
    Diag_CheckPlausibility();
    for (i = 0U; i < 2U; i++) {
        if ((s_events & (uint8_t)(1U << Diag_Events[i].event_id)) != 0U) {
            Diag_Events[i].cb();
        }
    }
    s_events = 0U;
}
