#ifndef ECU_CONFIG_H
#define ECU_CONFIG_H
/* 차종(빌드 변형)별 설정 */
#ifdef VARIANT_SPORT
#define TQ_LIMIT_PCT      ((int16_t)90)
#else
#define TQ_LIMIT_PCT      ((int16_t)80)
#endif
#define TQ_SPEED_LIMIT_KPH  30U
#endif
