#ifndef TORQUE_H
#define TORQUE_H
#include "std_types.h"
typedef struct {
    int16_t  cmd_pct;
    uint8_t  mode;
    int16_t  last_limit;
    uint16_t last_speed;
} TorqueState_t;
extern TorqueState_t g_tq_state;
extern int16_t g_tq_cmd;
int16_t CalcTorqueLimitPct(uint16_t speed_kph, bool sensor_ok);
int16_t ComputeTorqueCommand(int16_t request_pct, uint16_t speed_kph, bool sensor_ok);
void Tq_Task(void);
#endif
