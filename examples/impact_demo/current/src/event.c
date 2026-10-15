#include "app.h"

/* 이벤트 핸들러 테이블 (함수 포인터) */
static const EventHandler_t s_handlers[3] = {
    Evt_OnOverheat,
    Evt_OnSensorFail,
    Evt_OnRecover
};

void Event_Dispatch(U8 evt)
{
    if (evt < 3U) {
        s_handlers[evt]();
    }
}

void Evt_OnOverheat(void)
{
    Output_SetBuzzer(BUZZER_ON);
}

void Evt_OnSensorFail(void)
{
    Output_SetBuzzer(BUZZER_PULSE);
}

void Evt_OnRecover(void)
{
    Output_SetBuzzer(BUZZER_OFF);
}
