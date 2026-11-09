#include "torque.h"
#include "ecu_config.h"
#include "speed.h"
#include "diag.h"
#include "filter.h"
#include "rte.h"
#include "debug.h"

TorqueState_t g_tq_state;
int16_t g_tq_cmd;
static Flt_State_t s_req_flt = { 0U, 1U };

/* 속도 조건 기반 토크 제한: 센서 정상이고 기준 속도 이상이면 제한 */
int16_t CalcTorqueLimitPct(uint16_t speed_kph, bool sensor_ok)
{
    if (sensor_ok && speed_kph >= TQ_SPEED_LIMIT_KPH) {
        return TQ_LIMIT_PCT;
    }
    return (int16_t)100;
}

/* 최종 토크 명령 = min(요청, 제한) */
int16_t ComputeTorqueCommand(int16_t request_pct, uint16_t speed_kph, bool sensor_ok)
{
    int16_t limit_pct = CalcTorqueLimitPct(speed_kph, sensor_ok);
    int16_t cmd;
    cmd = (request_pct < limit_pct) ? request_pct : limit_pct;
    if (cmd < 0) {
        cmd = 0;
    }
    return cmd;
}

/* 토크 제어 태스크(10ms) */
void Tq_Task(void)
{
    int16_t req = 0;
    uint16_t speed = 0U;
    int16_t limit;
    (void)Rte_Read_TqReq_Value(&req);
    req = (int16_t)Flt_LowPass(&s_req_flt, (uint16_t)req);
    Spd_GetFiltered(&speed);
    limit = CalcTorqueLimitPct(speed, !Diag_IsSensorFault());
    g_tq_state.last_limit = limit;
    g_tq_state.last_speed = speed;
    Rte_IWrite_RE_TqTask_TqLimitActive_Value(limit < (int16_t)100);
    g_tq_cmd = ComputeTorqueCommand(req, speed, !Diag_IsSensorFault());
    g_tq_state.cmd_pct = g_tq_cmd;
}
