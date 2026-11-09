#include "speed.h"
#include "torque.h"
#include "diag.h"

void Out_Task(void);

/* OS 주기 태스크 */
void Task_10ms(void)
{
    Spd_Process();
    Diag_MainFunction();
    Tq_Task();
    Out_Task();
}
