/*
 * Adc_Isr.c - ADC 변환 완료 인터럽트
 */
#include "Std_Types.h"
#include "Blv_Cfg.h"
#include "Blv_Diag.h"
#include "Adc_Reg.h"

void Adc_ConvEndIsr(void)
{
    g_u16BattRaw = Adc_ReadResultReg(BLV_ADC_CH);
    g_u8AdcSampleCnt++;
}
