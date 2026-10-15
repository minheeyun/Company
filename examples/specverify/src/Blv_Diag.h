#ifndef BLV_DIAG_H
#define BLV_DIAG_H

#include "Std_Types.h"

typedef enum
{
    IGN_OFF = 0,
    IGN_ACC,
    IGN_ON,
    IGN_START
} Blv_IgnType;

extern uint16 g_u16BattRaw;
extern uint8  g_u8AdcSampleCnt;
extern uint8  BlvWarnLamp;

extern void BlvDiag_Init(void);
extern void BlvDiag_MainFunction(void);
extern void Adc_ConvEndIsr(void);

#endif /* BLV_DIAG_H */
