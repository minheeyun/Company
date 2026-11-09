#include "debug.h"

static uint16_t s_dbg_reads;

/* 디버그 카운터 조회(호출할 때마다 조회 횟수 증가) */
uint16_t Dbg_ReadCounter(void)
{
    s_dbg_reads++;
    return s_dbg_reads;
}
