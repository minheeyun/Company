#ifndef APP_CONFIG_H
#define APP_CONFIG_H

/* 과열 판정 온도 [degC] */
#define TEMP_HIGH_LIMIT      (100U)
/* 경고등 점등 온도 [degC] */
#define LAMP_WARN_TEMP       (85U)
/* 과열 고장 확정 디바운스 횟수 (10ms 주기) */
#define FAULT_DEBOUNCE_CNT   (300U)
/* 고장 해제 판정 온도 */
#define FAULT_CLEAR_TEMP     (80U)
/* 진단 트레이스 버퍼 크기 */
#define TRACE_SIZE           (16U)

#define LAMP_PIN    (3U)
#define HEATER_PIN  (4U)
#define BUZZER_PIN  (5U)

#define BUZZER_OFF   (0U)
#define BUZZER_ON    (1U)
#define BUZZER_PULSE (2U)

#endif
