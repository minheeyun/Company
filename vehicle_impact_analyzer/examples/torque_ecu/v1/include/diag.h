#ifndef DIAG_H
#define DIAG_H
#include "std_types.h"
#define DIAG_DEBOUNCE_LIMIT  3U
typedef void (*Diag_Callback_t)(void);
typedef struct {
    uint8_t         event_id;
    Diag_Callback_t cb;
} Diag_EventEntry_t;
extern bool g_sensor_fault;
extern uint8_t g_dtc_sensor;
extern uint8_t g_dtc_plaus;
void Diag_UpdateSensor(bool raw_fault);
bool Diag_IsSensorFault(void);
void Diag_MainFunction(void);
#endif
