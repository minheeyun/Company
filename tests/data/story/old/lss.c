typedef unsigned char uint8;
typedef struct { uint8 LSS_UVDTChangeConfirm_W; uint8 Other; } LSS_W_01_t;
extern void Rte_Write_LSS_W_01(const LSS_W_01_t *msg);
extern void s_DacUVRefAdjust(uint8 step);
extern uint8 Dio_ReadUV(void);

static uint8 u8s_UVStepAdjRetryCnt;
uint8 u8g_UVStepAdjErrSta;
static uint8 u8s_UVStep;
static LSS_W_01_t s_LSS_W_01;

static void s_DacUVRefFaultStep(void)
{
    uint8 uv = Dio_ReadUV();
    if (uv > 3U) {
        if (u8s_UVStepAdjRetryCnt < 10) {
            s_DacUVRefAdjust(u8s_UVStep);
            u8s_UVStepAdjRetryCnt++;
        } else {
            u8g_UVStepAdjErrSta = 1U;
        }
    }
}

void Runnable_TxConfirm_LSS_W_01_100ms(void)
{
    if (u8g_UVStepAdjErrSta == 1U) {
        s_LSS_W_01.LSS_UVDTChangeConfirm_W = 1U;
    } else {
        s_LSS_W_01.LSS_UVDTChangeConfirm_W = 0U;
    }
}

void s_P2CTX_LSS_W_01_100ms(void)
{
    Rte_Write_LSS_W_01(&s_LSS_W_01);
}

void Task_100ms(void)
{
    s_DacUVRefFaultStep();
    Runnable_TxConfirm_LSS_W_01_100ms();
    s_P2CTX_LSS_W_01_100ms();
}
