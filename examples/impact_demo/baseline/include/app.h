#ifndef APP_H
#define APP_H
#include "std_types.h"
#include "app_config.h"

typedef enum {
    CTRL_INIT = 0,
    CTRL_NORMAL,
    CTRL_DEGRADED,
    CTRL_FAULT
} CtrlState_t;

typedef void (*EventHandler_t)(void);

/* sensor.c */
extern volatile U16 g_adcRaw;
extern U16 g_tempFiltered;
void Sensor_Init(void);
void Sensor_Task(void);
BOOL Sensor_ReadTemp(U16 *outTemp);
void ADC_IRQHandler(void);

/* util.c */
U16 Util_ScaleTemp(U16 raw);
U16 Util_Clamp(U16 v, U16 lo, U16 hi);

/* fault.c */
extern BOOL g_faultActive;
void Fault_Init(void);
void Fault_Monitor(U16 temp);

/* control.c */
extern CtrlState_t g_ctrlState;
void Ctrl_Init(void);
void Ctrl_Main10ms(void);
void Ctrl_CheckOverheat(U16 temp);

/* heater.c */
extern U8 g_heaterCmd;
void Heater_On(void);
void Heater_Off(void);

/* output.c */
void Output_Update(void);
void Output_UpdateGauge(void);
void Output_SetBuzzer(U8 mode);

/* diag.c */
void Diag_Trace(U8 code);
U16 Diag_GetMaxTemp(void);
void Diag_Task100ms(void);

/* event.c */
void Event_Dispatch(U8 evt);
void Evt_OnOverheat(void);
void Evt_OnSensorFail(void);
void Evt_OnRecover(void);

/* main.c */
void Task_10ms(void);
void Task_100ms(void);

#endif
