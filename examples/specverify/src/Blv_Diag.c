/*
 * Blv_Diag.c - 배터리 저전압 진단 (10ms 주기)
 */
#include "Std_Types.h"
#include "Dem.h"
#include "IoHwAb.h"
#include "Blv_Cfg.h"
#include "Blv_Diag.h"

typedef enum
{
    BLV_STATE_NORMAL = 0,
    BLV_STATE_FAULT,
    BLV_STATE_SUSPEND
} Blv_StateType;

uint16 g_u16BattRaw;            /* ADC 변환 완료 ISR에서 갱신 */
uint8  g_u8AdcSampleCnt;        /* 마지막 진단 이후 ADC 변환 횟수 */
uint8  BlvWarnLamp;             /* 출력: 저전압 경고등 요청 */

static Blv_StateType s_eBlvState;
static boolean s_bFaultDetected;
static uint8   s_u8FaultCnt;
static uint16  s_u16HealCnt;
static uint8   s_u8StartMaskCnt;

#if (BLV_USE_FILTER == STD_ON)
static uint16  s_au16Buf[BLV_FILTER_N];
static uint8   s_u8BufIdx;
static uint8   s_u8BufNum;
#endif

static uint16 Blv_ConvertToMv(uint16 u16Raw)
{
    uint16 u16Mv;

    /* ADC raw → 분압 전 전압 [mV] */
    u16Mv = (uint16)((u16Raw * BLV_ADC_REF_MV) / BLV_ADC_MAX);

    return (uint16)(u16Mv * BLV_DIVIDER_RATIO);
}

#if (BLV_USE_FILTER == STD_ON)
static uint16 Blv_Filter(uint16 u16Mv)
{
    uint32 u32Sum = 0U;
    uint8  u8I;

    s_au16Buf[s_u8BufIdx] = u16Mv;
    s_u8BufIdx++;
    if (s_u8BufIdx >= BLV_FILTER_N)
    {
        s_u8BufIdx = 0U;
    }
    if (s_u8BufNum < BLV_FILTER_N)
    {
        s_u8BufNum++;
    }

    for (u8I = 0U; u8I < s_u8BufNum; u8I++)
    {
        u32Sum += s_au16Buf[u8I];
    }

    if (s_u8BufNum > 0U)
    {
        return (uint16)(u32Sum / s_u8BufNum);
    }
    return u16Mv;
}
#else
static uint16 Blv_Filter(uint16 u16Mv)
{
    return u16Mv;
}
#endif

void BlvDiag_Init(void)
{
    s_eBlvState = BLV_STATE_NORMAL;
    s_bFaultDetected = FALSE;
    s_u8FaultCnt = 0U;
    s_u8StartMaskCnt = BLV_START_MASK_CNT;
#if (BLV_USE_FILTER == STD_ON)
    s_u8BufIdx = 0U;
    s_u8BufNum = 0U;
#endif
    BlvWarnLamp = 0U;
}

void BlvDiag_MainFunction(void)
{
    uint16 u16Raw;
    uint16 u16Volt;
    boolean bLow;
    Blv_IgnType eIgn;

    eIgn = (Blv_IgnType)IoHwAb_GetIgnSts();

    if (eIgn == IGN_START)
    {
        /* 크랭킹 중: 진단 중단 */
        s_u8FaultCnt = 0U;
        s_u16HealCnt = 0U;
        s_u8StartMaskCnt = 0U;
        BlvWarnLamp = 0U;
        return;
    }

    if (eIgn == IGN_OFF)
    {
        return;
    }

    /* START 종료 후 마스킹 */
    if (s_u8StartMaskCnt < BLV_START_MASK_CNT)
    {
        s_u8StartMaskCnt++;
        return;
    }

    if (g_u8AdcSampleCnt == 0U)
    {
        return;     /* 새 샘플 없음 */
    }
    u16Raw = g_u16BattRaw;
    g_u8AdcSampleCnt = 0U;

    if (u16Raw == BLV_INVALID_RAW)
    {
        return;
    }

    u16Volt = Blv_ConvertToMv(u16Raw);
    u16Volt = Blv_Filter(u16Volt);

    bLow = (u16Volt < BLV_LOW_MV) ? TRUE : FALSE;

    if (bLow == TRUE)
    {
        s_u8FaultCnt++;
        s_u16HealCnt = 0U;
    }
    else if (u16Volt >= BLV_HEAL_MV)
    {
        s_u8FaultCnt = 0U;
        if (s_u16HealCnt < BLV_HEAL_CNT)
        {
            s_u16HealCnt++;
        }
    }
    else
    {
        /* 히스테리시스 구간: 현재 상태 유지 */
    }

    s_bFaultDetected = (s_u8FaultCnt >= BLV_CONFIRM_CNT) ? TRUE : FALSE;

    switch (s_eBlvState)
    {
        case BLV_STATE_NORMAL:
            if (s_bFaultDetected == TRUE)
            {
                s_eBlvState = BLV_STATE_FAULT;
                Dem_SetEventStatus(DemConf_DemEventParameter_B1A00, DEM_EVENT_STATUS_FAILED);
            }
            break;

        case BLV_STATE_FAULT:
            if ((s_bFaultDetected == FALSE) && (s_u16HealCnt >= BLV_HEAL_CNT))
            {
                s_eBlvState = BLV_STATE_NORMAL;
                Dem_SetEventStatus(DemConf_DemEventParameter_B1A00, DEM_EVENT_STATUS_PASSED);
            }
            break;

        default:
            break;
    }

    BlvWarnLamp = s_bFaultDetected;
}
