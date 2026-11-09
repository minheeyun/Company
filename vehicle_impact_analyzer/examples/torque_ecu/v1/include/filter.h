#ifndef FILTER_H
#define FILTER_H
#include "std_types.h"
/* 공용 1차 저역 필터 상태 */
typedef struct {
    uint16_t y;
    uint8_t  k_shift;
} Flt_State_t;
uint16_t Flt_LowPass(Flt_State_t *st, uint16_t x);
#endif
