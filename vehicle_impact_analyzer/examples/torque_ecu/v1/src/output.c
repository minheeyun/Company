#include "torque.h"
#include "diag.h"
#include "rte.h"

/* 고장 시 출력 차단 */
int16_t Out_ComputeOutput(int16_t cmd, bool fault)
{
    int16_t out;
    if (fault) {
        out = 0;
    } else {
        out = cmd;
    }
    return out;
}

/* 출력 태스크(10ms): 토크 명령 신호 송신 */
void Out_Task(void)
{
    int16_t out = Out_ComputeOutput(g_tq_cmd, Diag_IsSensorFault());
    (void)Rte_Write_TqCmd_Value(out);
}
