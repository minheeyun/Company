typedef unsigned char uint8;
extern uint8 IsReady(void);
extern void Retry(void);
extern uint8 g_limit;
static uint8 s_cnt;
uint8 g_err;
static uint8 s_ok;

void App_A(void)
{
    if (IsReady() != 0U) {
        if (s_cnt < 10) {
            Retry();
            s_cnt++;
        } else {
            g_err = 1U;
        }
    }
}

void App_B(void)
{
    if (s_ok == 1U) {
        if (s_cnt < g_limit) {
            Retry();
        }
    }
}
