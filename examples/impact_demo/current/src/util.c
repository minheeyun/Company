#include "app.h"

/* ADC raw(0..4095) → 온도[degC]. 여러 모듈에서 공통 사용 */
U16 Util_ScaleTemp(U16 raw)
{
    U16 t;
    t = (U16)((raw * 160U) / 4095U);
    return t;
}

U16 Util_Clamp(U16 v, U16 lo, U16 hi)
{
    if (v < lo) {
        return lo;
    }
    if (v > hi) {
        return hi;
    }
    return v;
}
