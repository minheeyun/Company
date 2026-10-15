#include "app.h"

/* 10ms 주기 태스크 */
void Task_10ms(void)
{
    Sensor_Task();
    Fault_Monitor(g_tempFiltered);
    Ctrl_Main10ms();
    Output_Update();
}

/* 100ms 주기 태스크 */
void Task_100ms(void)
{
    Output_UpdateGauge();
    Diag_Task100ms();
}

int main(void)
{
    Sensor_Init();
    Fault_Init();
    Ctrl_Init();
    for (;;) {
        /* 스케줄러가 Task_10ms / Task_100ms 를 호출 */
    }
    return 0;
}
