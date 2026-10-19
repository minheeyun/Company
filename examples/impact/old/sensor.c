#include <stdint.h>
#include "config.h"

typedef struct {
    uint16_t raw;
    uint8_t  counter;
    uint8_t  valid;
} SensorState_t;

SensorState_t g_sensor;
uint8_t g_sensorFault;
static uint8_t g_unrelated;

extern void Dem_SetEventStatus(uint16_t eventId, uint8_t status);

static void Debounce(SensorState_t *s, uint16_t raw)
{
    s->raw = raw;
    if (raw > 1000U) {
        s->counter++;
    } else {
        s->counter = 0U;
    }
}

static uint8_t Saturate(uint8_t v)
{
    return (v > 10U) ? 10U : v;
}

void Sensor_MainFunction(uint16_t raw)
{
    SensorState_t *p = &g_sensor;
    Debounce(p, raw);
    g_sensor.valid = (g_sensor.counter < DEBOUNCE_LIMIT) ? 1U : 0U;
    g_unrelated = Saturate(5U);
}

void Sensor_Report(void)
{
    if (g_sensor.valid == 0U) {
        g_sensorFault = 1U;
        Dem_SetEventStatus(0x10U, 1U);
    }
}

uint8_t Sensor_GetRaw(void)
{
    return (uint8_t)g_sensor.raw;
}
