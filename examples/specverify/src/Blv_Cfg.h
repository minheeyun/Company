#ifndef BLV_CFG_H
#define BLV_CFG_H

#include "Std_Types.h"

/* 판정 전압 [mV] */
#define BLV_LOW_MV              (9000U)
#define BLV_HEAL_MV             (10500U)

/* 10ms 주기 기준 카운트 */
#define BLV_CONFIRM_CNT         (200U)      /* 2s */
#define BLV_HEAL_CNT            (500U)      /* 5s */
#define BLV_START_MASK_CNT      (300U)      /* 3s */

/* ADC 변환 */
#define BLV_ADC_CH              (3U)
#define BLV_ADC_REF_MV          (5000U)
#define BLV_ADC_MAX             (1023U)
#define BLV_DIVIDER_RATIO       (4U)
#define BLV_INVALID_RAW         (0xFFFFU)

/* 이동평균 필터 */
#define BLV_USE_FILTER          STD_ON
#define BLV_FILTER_N            (4U)

#endif /* BLV_CFG_H */
