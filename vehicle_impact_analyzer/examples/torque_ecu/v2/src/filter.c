#include "filter.h"

/* 공용 저역 필터: 차속과 토크 요청에서 함께 사용 */
uint16_t Flt_LowPass(Flt_State_t *st, uint16_t x)
{
    int32_t diff = (int32_t)x - (int32_t)st->y;
    st->y = (uint16_t)((int32_t)st->y + (diff >> st->k_shift));
    return st->y;
}
