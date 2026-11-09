#ifndef SPEED_H
#define SPEED_H
#include "std_types.h"
#define SPD_MAX_KPH  280U
extern volatile uint16_t g_rx_speed_raw;
extern volatile bool g_rx_valid;
void Can_RxIsr(uint16_t raw);
void Spd_Process(void);
void Spd_GetFiltered(uint16_t *out);
#endif
