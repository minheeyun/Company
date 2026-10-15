#include "app.h"
#include "dio.h"

/* C 함수: 고장 상태/온도에 따라 경고등 출력 (fault.c 와 직접 호출 관계 없음) */
void Output_Update(void)
{
    if ((g_faultActive == TRUE) || (g_tempFiltered >= LAMP_WARN_TEMP)) {
        Dio_WriteChannel(LAMP_PIN, 1U);
    } else {
        Dio_WriteChannel(LAMP_PIN, 0U);
    }
}

/* 온도 게이지 PWM 출력 */
void Output_UpdateGauge(void)
{
    U16 duty;
    duty = Util_Clamp(g_tempFiltered, 0U, 150U);
    Pwm_SetDutyCycle(1U, (U16)(duty * 10U));
}

void Output_SetBuzzer(U8 mode)
{
    if (mode == BUZZER_PULSE) {
        Dio_WriteChannel(BUZZER_PIN, 2U);
    } else if (mode == BUZZER_ON) {
        Dio_WriteChannel(BUZZER_PIN, 1U);
    } else {
        Dio_WriteChannel(BUZZER_PIN, 0U);
    }
}
