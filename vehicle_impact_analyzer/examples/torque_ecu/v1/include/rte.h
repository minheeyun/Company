#ifndef RTE_H
#define RTE_H
#include "std_types.h"
/* RTE API (생성 코드: 본 분석에서는 외부 함수) */
Std_ReturnType Rte_Read_TqReq_Value(int16_t *data);
Std_ReturnType Rte_Write_TqCmd_Value(int16_t data);
/* RTE 생성기가 함수형 매크로로 만든 암묵적 쓰기(호출 그래프에 나타나지 않음) */
extern uint8_t Rte_Buf_TqLimitActive;
#define Rte_IWrite_RE_TqTask_TqLimitActive_Value(v) (Rte_Buf_TqLimitActive = (uint8_t)(v))
#endif
