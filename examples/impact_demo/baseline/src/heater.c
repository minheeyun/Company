#include "app.h"
#include "dio.h"

U8 g_heaterCmd;

void Heater_On(void)
{
    g_heaterCmd = 1U;
}

void Heater_Off(void)
{
    g_heaterCmd = 0U;
    Dio_WriteChannel(HEATER_PIN, 0U);
}
