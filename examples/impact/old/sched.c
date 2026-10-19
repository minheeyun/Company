#include <stdint.h>

typedef void (*Task_t)(void);

extern void UpdateDiagnostic(void);
extern void Sensor_Report(void);

static uint32_t g_cycle;

static const Task_t g_tasks[] = { UpdateDiagnostic, Sensor_Report };

void Os_Tick_ISR(void)
{
    uint8_t i;
    g_cycle++;
    for (i = 0U; i < 2U; i++) {
        g_tasks[i]();
    }
}
