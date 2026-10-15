#ifndef DIO_H
#define DIO_H
#include "std_types.h"

/* MCAL 드라이버 (프로젝트 외부 구현 — 정의 없음) */
void Dio_WriteChannel(U8 channel, U8 level);
void Pwm_SetDutyCycle(U8 channel, U16 duty);

#endif
