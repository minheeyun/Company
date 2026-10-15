#include "app.h"

static U8  s_traceBuf[TRACE_SIZE];
static U8  s_traceIdx;
static U16 s_maxTemp;

/* 디버그용 트레이스 (기록만 하고 읽는 곳 없음) */
void Diag_Trace(U8 code)
{
    s_traceBuf[s_traceIdx] = code;
    s_traceIdx = (U8)((s_traceIdx + 1U) % TRACE_SIZE);
#ifdef FEATURE_TRACE_EXT
    s_traceBuf[s_traceIdx] = 0xFFU;
#endif
}

U16 Diag_GetMaxTemp(void)
{
    return s_maxTemp;
}

void Diag_Task100ms(void)
{
    U16 t = Util_ScaleTemp(g_adcRaw);
    if (t > s_maxTemp) {
        s_maxTemp = t;
    }
}
