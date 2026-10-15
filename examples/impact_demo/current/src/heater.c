#include "app.h"
#include "dio.h"

U8 g_heaterCmd;

void Heater_On(void)
{
    g_heaterCmd = 1U;
}

/* 히터 출력 차단 (안전 상태) */
void Heater_Off( void )
{
    g_heaterCmd = 0U;   /* 명령 해제 */
    Dio_WriteChannel( HEATER_PIN, 0U );
}
