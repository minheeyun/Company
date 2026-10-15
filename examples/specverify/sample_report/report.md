# SW 검증 리뷰 보고서 — spec_blv.md

> 자연어 기능사양서와 Embedded C 코드 간 의미적 정합성 검증 및 사양 공백·잠재 결함 분석 결과입니다. 모든 판정은 검증자의 최종 확인이 필요합니다.

## 0. 분석 개요

| 항목 | 내용 |
|---|---|
| 사양서 | spec_blv.md |
| 소스 | examples/specverify/src |
| 실행 환경 정보 | examples/specverify/env.json |
| LLM | 파일 교환 모드 (examples/specverify/sample_llm) |
| 수행 단계 | 0 정적 분석, 1 사양 분석 (요구사항 구조화), 2 구현 추적, 3 정합성 분석, 4 사양 공백 분석, 5 코드 결함 분석 |
| 전처리기 | /usr/bin/gcc -E |
| 외부 도구 | gcc (호스트 자료형 기준: 대상 int 16bit와 다를 수 있음); 스텁 헤더로 인한 컴파일 오류 3건 제외 |
| 생성 시각 | 2026-10-15 03:57:41 |

### 분석 전제 및 제한

- 대상: 16-bit 차체 제어 MCU (예시) (int 16bit, long 32bit, char unsigned)

## 1. 요약

| 판정 | 건수 |
|---|---|
| 결함 확정 | 5 |
| 결함 가능성 높음 | 5 |
| 잠재 위험 | 4 |
| 검토 필요 | 1 |
| 사양 보완 필요 | 4 |

| 유형 | 건수 | HIGH |
|---|---|---|
| 사양 공백 | 5 | 1 |
| 타이머·카운터 불일치 | 3 | 1 |
| 조건식 불일치 | 2 | 1 |
| 기능·논리 오류 | 2 | 1 |
| 사양 모호 | 2 | 0 |
| 우선순위 불일치 | 1 | 1 |
| 런타임 오류 | 1 | 1 |
| 임계값·경계 불일치 | 1 | 0 |
| 초기화·복구 불일치 | 1 | 0 |
| 동시성 | 1 | 0 |

### 요구사항 커버리지

| 요구사항 | 제목 | 추적 | 정합성 | 검출 결과 | 모호/미정의 |
|---|---|---|---|---|---|
| SWR-BLV-001 | 진단 실행 조건 | 추적됨 | 불일치 | C-002, C-001, D-004, G-003 | 3 |
| SWR-BLV-002 | 저전압 판정 | 추적됨 | 불일치 | D-002, C-003, G-006 | - |
| SWR-BLV-003 | 고장 확정 | 추적됨 | 부분 일치 | D-001, G-001, D-003, G-005, C-006 | 2 |
| SWR-BLV-004 | 경고등 출력 | 추적됨 | 불일치 | C-004, D-001, G-001, G-003 | - |
| SWR-BLV-005 | 고장 해제 | 추적됨 | 부분 일치 | C-005, G-004 | - |
| SWR-BLV-006 | 히스테리시스 | 추적됨 | 일치 | C-005, C-006 | 1 |
| SWR-BLV-007 | 시동 후 진단 유예 | 추적됨 | 불일치 | C-007 | 2 |
| SWR-BLV-008 | 입력 유효성 | 추적됨 | 부분 일치 | G-005, G-002 | 1 |
| SWR-BLV-009 | 초기화 | 부분 추적 | 불일치 | C-008, G-004 | 2 |
| SWR-BLV-010 | 경고등 우선순위 | 추적됨 | 불일치 | C-009 | - |

## 2. 검출 결과 상세

### [C-004] 경고등이 '확정 상태'가 아니라 순간 검출 플래그를 따름

**결함 확정** · 심각도 HIGH · 조건식 불일치 · 단계: 정합성

1. **요구사항**: SWR-BLV-004
   > 고장이 확정된 상태에서는 경고등을 점등한다. 고장이 해제되면 경고등을 소등한다.
2. **코드 위치**:
   - ✔ `Blv_Diag.c:181` BlvDiag_MainFunction() `s_bFaultDetected` — 경고등 출력
     ```c
       179  |     }
       180  | 
       181> |     BlvWarnLamp = s_bFaultDetected;
       182  | }
     ```
   - ✔ `Blv_Diag.c:146` BlvDiag_MainFunction() `s_u8FaultCnt` — 회복 시작 시 카운터 0
     ```c
       144  |     else if (u16Volt >= BLV_HEAL_MV)
       145  |     {
       146> |         s_u8FaultCnt = 0U;
       147  |         if (s_u16HealCnt < BLV_HEAL_CNT)
       148  |         {
     ```
   - ✔ `Blv_Diag.c:157` BlvDiag_MainFunction() `s_bFaultDetected` — 검출 플래그 재계산
     ```c
       155  |     }
       156  | 
       157> |     s_bFaultDetected = (s_u8FaultCnt >= BLV_CONFIRM_CNT) ? TRUE : FALSE;
       158  | 
       159  |     switch (s_eBlvState)
     ```
3. **유형/심각도**: 조건식 불일치 / HIGH
4. **사양 vs 코드**
   - 사양: S32: 확정 상태 동안 점등, 해제 시 소등
   - 코드: L181 BlvWarnLamp = s_bFaultDetected; s_bFaultDetected는 매 주기 s_u8FaultCnt >= 200으로 재계산(L157)
   - 비교: 확정 상태(s_eBlvState==FAULT)와 경고등이 분리됨
5. **발생 조건**: 고장 확정 후 BattVolt ≥ 10500mV로 회복 시작 (해제 조건 5초 미충족)
   1) Blv_Diag.c:144 10500mV 이상
   2) Blv_Diag.c:146 s_u8FaultCnt=0
   3) Blv_Diag.c:157 s_bFaultDetected=FALSE
   4) Blv_Diag.c:170 해제 조건 미충족 → FAULT 유지
   5) Blv_Diag.c:181 경고등 소등
6. **예상 동작(사양)**: 해제(5초) 전까지 점등 유지
   **코드 추론 동작**: 회복 시작 즉시 소등, 상태는 5초간 FAULT 유지 → 상태와 표시 불일치. 카운터 순환 시에도 소등(D-001). ※ 현재 코드는 C-007(유예 타이머 무한 반복) 때문에 L116 이후가 실행되지 않으므로, 이 불일치는 C-007 수정 후 드러나는 잠복 결함이다.
7. **판단 근거**
   - [사실] 경고등은 s_bFaultDetected를 그대로 출력한다 — 근거: Blv_Diag.c:181
   - [사실] 10500mV 이상에서 고장 카운터를 즉시 0으로 만든다 — 근거: Blv_Diag.c:146
   - [추론] 회복 첫 주기에 s_bFaultDetected가 FALSE가 되어 경고등이 꺼진다 — 근거: Blv_Diag.c:157
   - 불확실성: 없음 (코드 경로가 단순)
8. **권장 수정 방향**: `BlvWarnLamp = (s_eBlvState == BLV_STATE_FAULT) ? 1U : 0U;`로 확정 상태를 출력한다.
9. **추가 확인 필요**: -
10. **테스트 케이스**

| # | 기법 | 사전 조건 | 조작 | 기대 결과 | 관찰 수단 |
|---|---|---|---|---|---|
| 1 | STATE_TRANSITION | 고장 확정(경고등 점등) | BattVolt 11000mV 인가 후 0~6초 관찰 | 5초까지 점등, 5초 후 소등·PASSED | BlvWarnLamp, s_eBlvState, Dem |

관련 정적 후보: SC-012 · 같은 코드 위치의 다른 결과: D-001

### [C-007] 시동 후 3초 유예가 끝나지 않아 저전압 진단이 영구 정지

**결함 확정** · 심각도 HIGH · 타이머·카운터 불일치 · 단계: 정합성

1. **요구사항**: SWR-BLV-007
   > IgnSts가 START에서 ON으로 전환된 후 3초 동안은 진단 판정을 유예한다.
2. **코드 위치**:
   - ✔ `Blv_Diag.c:116` BlvDiag_MainFunction() `BLV_START_MASK_CNT` — 유예 판정
     ```c
       114  | 
       115  |     /* START 종료 후 마스킹 */
       116> |     if (s_u8StartMaskCnt < BLV_START_MASK_CNT)
       117  |     {
       118  |         s_u8StartMaskCnt++;
     ```
   - ✔ `Blv_Diag.c:118` BlvDiag_MainFunction() `s_u8StartMaskCnt` — 유예 카운터 증가
     ```c
       116  |     if (s_u8StartMaskCnt < BLV_START_MASK_CNT)
       117  |     {
       118> |         s_u8StartMaskCnt++;
       119  |         return;
       120  |     }
     ```
   - ✔ `Blv_Diag.c:83` BlvDiag_Init() `BLV_START_MASK_CNT` — 초기화 시 300 대입
     ```c
        81  |     s_bFaultDetected = FALSE;
        82  |     s_u8FaultCnt = 0U;
        83> |     s_u8StartMaskCnt = BLV_START_MASK_CNT;
        84  | #if (BLV_USE_FILTER == STD_ON)
        85  |     s_u8BufIdx = 0U;
     ```
   - ✔ `Blv_Cfg.h:13`  `BLV_START_MASK_CNT` — 300U 정의
     ```c
        11  | #define BLV_CONFIRM_CNT         (200U)      /* 2s */
        12  | #define BLV_HEAL_CNT            (500U)      /* 5s */
        13> | #define BLV_START_MASK_CNT      (300U)      /* 3s */
        14  | 
        15  | /* ADC 변환 */
     ```
3. **유형/심각도**: 타이머·카운터 불일치 / HIGH
4. **사양 vs 코드**
   - 사양: S42: START→ON 후 3초 유예, 이후 진단
   - 코드: s_u8StartMaskCnt(uint8, 0..255) < 300U는 항상 참 → 매 주기 증가 후 반환, 255→0 순환
   - 비교: 유예 종료 조건이 자료형상 성립 불가
5. **발생 조건**: IgnSts=ON 진입 후 모든 주기 (전원 투입 직후 포함: Init에서 300→44 저장)
   1) Blv_Diag.c:83 Init: s_u8StartMaskCnt = 44 (300 잘림)
   2) Blv_Diag.c:116 44 < 300 참
   3) Blv_Diag.c:118 증가 … 255 → 0
   4) Blv_Diag.c:119 반환 (L122 이후 미실행)
6. **예상 동작(사양)**: 3초(300주기) 후 진단 재개
   **코드 추론 동작**: 진단(L122~L181)이 한 번도 실행되지 않음 → 저전압 DTC·경고등 기능 상실
7. **판단 근거**
   - [사실] uint8 변수와 300U 비교는 항상 참 (정적 계산 CERTAIN, gcc -Wtype-limits 경고) — 근거: SC-008
   - [사실] Init에서 300 대입 시 44로 저장 (CERTAIN, gcc -Woverflow) — 근거: SC-007
   - [사실] L119에서 반환하므로 L122 이후가 실행되지 않는다 — 근거: Blv_Diag.c:116-120
   - 불확실성: 없음. 3초=300주기 환산은 10ms 주기 전제(S56).
8. **권장 수정 방향**: s_u8StartMaskCnt를 uint16으로 변경하거나 마스크 단위를 조정한다. 초기화 값 의미(유예 없음)를 명확히 한다.
9. **추가 확인 필요**: -
10. **테스트 케이스**

| # | 기법 | 사전 조건 | 조작 | 기대 결과 | 관찰 수단 |
|---|---|---|---|---|---|
| 1 | TIMING | IgnSts=START | ON 전환 후 BattVolt 8500mV 6초 유지 | 3초 유예 후 2초 경과 시점(5초)에 FAILED | Dem 이벤트 상태, s_u8StartMaskCnt |
| 2 | REQUIREMENT_BASED | Init 직후 IgnSts=ON (START 없음) | BattVolt 8500mV 3초 | 2초 시점 FAILED (유예 적용 여부는 사양 확인) | Dem 이벤트 상태 |

관련 정적 후보: SC-007, SC-008, SC-009

### [C-009] START 중 확정 고장의 경고등을 소등 (우선순위 역전)

**결함 확정** · 심각도 HIGH · 우선순위 불일치 · 단계: 정합성

1. **요구사항**: SWR-BLV-010
   > 진단 중단 조건(IgnSts = START)에서도 고장 확정 상태라면 경고등 점등을 유지한다.
2. **코드 위치**:
   - ✔ `Blv_Diag.c:106` BlvDiag_MainFunction() `BlvWarnLamp` — START 분기 경고등 0
     ```c
       104  |         s_u16HealCnt = 0U;
       105  |         s_u8StartMaskCnt = 0U;
       106> |         BlvWarnLamp = 0U;
       107  |         return;
       108  |     }
     ```
3. **유형/심각도**: 우선순위 불일치 / HIGH
4. **사양 vs 코드**
   - 사양: S52: START에서도 확정 상태면 점등 유지
   - 코드: L106 BlvWarnLamp = 0U (상태 확인 없음)
   - 비교: 진단 중단 처리가 경고 유지 요구보다 우선함
5. **발생 조건**: 고장 확정(점등) 상태에서 IgnSts=START
   1) Blv_Diag.c:100 START
   2) Blv_Diag.c:106 경고등 0
   3) Blv_Diag.c:107 반환
6. **예상 동작(사양)**: 점등 유지
   **코드 추론 동작**: 크랭킹 동안 소등
7. **판단 근거**
   - [사실] START 분기는 상태와 무관하게 경고등에 0을 대입한다 — 근거: Blv_Diag.c:106
   - 불확실성: 없음
8. **권장 수정 방향**: START 분기에서 경고등 대입을 제거하거나 `s_eBlvState == BLV_STATE_FAULT`이면 1을 유지한다.
9. **추가 확인 필요**: -
10. **테스트 케이스**

| # | 기법 | 사전 조건 | 조작 | 기대 결과 | 관찰 수단 |
|---|---|---|---|---|---|
| 1 | STATE_TRANSITION | 고장 확정, 경고등 점등 | IgnSts=START 2초 | 경고등 점등 유지 | BlvWarnLamp |

### [D-001] 8bit 고장 카운터 순환으로 지속 고장 중 경고등이 주기적으로 소등

**결함 가능성 높음** · 심각도 HIGH · 기능·논리 오류 · 단계: 코드 결함

1. **요구사항**: SWR-BLV-003, SWR-BLV-004
   > 고장이 확정된 상태에서는 경고등을 점등한다.
2. **코드 위치**:
   - ✔ `Blv_Diag.c:141` BlvDiag_MainFunction() `s_u8FaultCnt` — 포화 없는 증가
     ```c
       139  |     if (bLow == TRUE)
       140  |     {
       141> |         s_u8FaultCnt++;
       142  |         s_u16HealCnt = 0U;
       143  |     }
     ```
   - ✔ `Blv_Diag.c:157` BlvDiag_MainFunction() `BLV_CONFIRM_CNT` — 임계 판정
     ```c
       155  |     }
       156  | 
       157> |     s_bFaultDetected = (s_u8FaultCnt >= BLV_CONFIRM_CNT) ? TRUE : FALSE;
       158  | 
       159  |     switch (s_eBlvState)
     ```
   - ✔ `Blv_Diag.c:181` BlvDiag_MainFunction() `BlvWarnLamp` — 출력
     ```c
       179  |     }
       180  | 
       181> |     BlvWarnLamp = s_bFaultDetected;
       182  | }
     ```
3. **유형/심각도**: 기능·논리 오류 / HIGH
4. **사양 vs 코드**
   - 사양: S32: 확정 상태 동안 점등
   - 코드: uint8 카운터가 256회째 0 → s_bFaultDetected FALSE → 경고등 0 (상태는 FAULT 유지)
   - 비교: 지속 고장에서 표시가 해제됨
5. **발생 조건**: 저전압(≤9000mV)이 2.56초 이상 지속
   1) Blv_Diag.c:141 200회(2.0s) 점등
   2) Blv_Diag.c:141 256회(2.56s) 0으로 순환
   3) Blv_Diag.c:157 FALSE
   4) Blv_Diag.c:181 소등
   5) Blv_Diag.c:141 456회(4.56s) 재점등
6. **예상 동작(사양)**: 고장 지속 동안 계속 점등
   **코드 추론 동작**: 2.0s 점등 → 2.56s 소등 → 4.56s 재점등 → 5.12s 소등 … 반복. DTC는 재보고 없음. ※ 현재 코드는 C-007(유예 타이머 무한 반복) 때문에 L116 이후가 실행되지 않으므로, 이 불일치는 C-007 수정 후 드러나는 잠복 결함이다.
7. **판단 근거**
   - [사실] s_u8FaultCnt는 uint8이고 증가에 상한 검사가 없다 — 근거: SC-012
   - [사실] 경고등은 카운터 기반 플래그를 출력한다 — 근거: Blv_Diag.c:181
   - [추론] 256회째 순환 후 200회까지 플래그가 FALSE — 근거: Blv_Diag.c:157
   - 불확실성: 사양이 확정 후 카운터 동작을 정의하지 않음(G-001). C-004 수정 시 경고등 영향은 사라지나 카운터 순환 자체는 남음.
8. **권장 수정 방향**: `if (s_u8FaultCnt < BLV_CONFIRM_CNT) { s_u8FaultCnt++; }`로 포화하고, 경고등은 확정 상태 기반으로 출력 (C-004).
9. **추가 확인 필요**: 확정 후 카운터 포화가 사양 의도인지 (G-001)
10. **테스트 케이스**

| # | 기법 | 사전 조건 | 조작 | 기대 결과 | 관찰 수단 |
|---|---|---|---|---|---|
| 1 | BOUNDARY_VALUE | IgnSts=ON, 유예 종료 | 8500mV 6초 | 2초 이후 계속 점등 | BlvWarnLamp 시계열, s_u8FaultCnt |

관련 정적 후보: SC-012 · 같은 코드 위치의 다른 결과: C-004, G-001

### [D-002] 16bit int 대상에서 전압 변환 곱셈 오버플로 → 정상 전압을 저전압으로 오판정

**결함 가능성 높음** · 심각도 HIGH · 런타임 오류 · 단계: 코드 결함

1. **요구사항**: SWR-BLV-002
   > 배터리 전압이 9000mV 이하이면 저전압 조건으로 판단한다.
2. **코드 위치**:
   - ✔ `Blv_Diag.c:38` Blv_ConvertToMv() `BLV_ADC_REF_MV` — u16Raw * 5000U
     ```c
        36  | 
        37  |     /* ADC raw → 분압 전 전압 [mV] */
        38> |     u16Mv = (uint16)((u16Raw * BLV_ADC_REF_MV) / BLV_ADC_MAX);
        39  | 
        40  |     return (uint16)(u16Mv * BLV_DIVIDER_RATIO);
     ```
3. **유형/심각도**: 런타임 오류 / HIGH
4. **사양 vs 코드**
   - 사양: S14: BattVolt 0~20000mV
   - 코드: int 16bit에서 u16Raw * 5000U는 unsigned int(16bit) 연산 → raw 613(12V)에서 3,065,000 mod 65536 = 50,344 → /1023 = 49 → ×4 = 196mV
   - 비교: 실제 12V가 196mV로 계산
5. **발생 조건**: 정상 배터리 전압 12V (raw≈613), 대상 int=16bit
   1) Blv_Diag.c:134 변환 호출
   2) Blv_Diag.c:38 곱셈 순환
   3) Blv_Diag.c:137 196 < 9000 → 저전압
   4) Blv_Diag.c:164 FAILED
6. **예상 동작(사양)**: 12V는 정상
   **코드 추론 동작**: 정상 전압에서도 2초 후 FAILED·경고등 점등 (C-007 수정 후)
7. **판단 근거**
   - [사실] env.json: int 16bit, ADC 10bit — 근거: env.json
   - [사실] 피연산자 범위상 결과가 0..327,675,000으로 16bit를 넘는다 — 근거: SC-005
   - [추론] raw ≥ 14에서 순환 발생 — 근거: 14×5000 > 65535
   - [가설] 대상 컴파일러가 int를 16bit로 처리 — 근거: 컴파일러 설정 미제공
   - 불확실성: 대상 컴파일러의 int 크기·정수 승격 옵션 미확인 (호스트 gcc는 32bit라 경고 없음).
8. **권장 수정 방향**: `(uint16)(((uint32)u16Raw * BLV_ADC_REF_MV) / BLV_ADC_MAX)`로 32bit 연산을 강제한다.
9. **추가 확인 필요**: 대상 컴파일러 int 크기; ADC 결과 범위
10. **테스트 케이스**

| # | 기법 | 사전 조건 | 조작 | 기대 결과 | 관찰 수단 |
|---|---|---|---|---|---|
| 1 | EQUIVALENCE_CLASS | 대상 HW/SIL(int 16bit) | raw=613 입력 | u16Volt ≈ 11980mV | u16Volt |
| 2 | BOUNDARY_VALUE | 대상 HW/SIL | raw=1023 | u16Volt = 20000mV | u16Volt |

관련 정적 후보: SC-005, SC-006

### [G-001] 고장 확정 후 저전압 지속 시 고장 카운터 상한 미정의 (8bit 순환)

**잠재 위험** · 심각도 HIGH · 사양 공백 · 단계: 사양 공백

1. **요구사항**: SWR-BLV-003, SWR-BLV-004
   > 저전압 조건이 2초 동안 연속으로 유지되면 고장으로 확정하고
2. **코드 위치**:
   - ✔ `Blv_Diag.c:141` BlvDiag_MainFunction() `s_u8FaultCnt` — 포화 없는 증가
     ```c
       139  |     if (bLow == TRUE)
       140  |     {
       141> |         s_u8FaultCnt++;
       142  |         s_u16HealCnt = 0U;
       143  |     }
     ```
3. **유형/심각도**: 사양 공백 / HIGH
4. **사양 vs 코드**
   - 사양: 확정 이후 카운터 동작 정의 없음
   - 코드: s_u8FaultCnt++ 포화 없음 → 256회째 0으로 순환
   - 비교: 사양 공백
5. **발생 조건**: 저전압이 2.56초 이상 지속 (방전 배터리, 발전기 고장)
   1) Blv_Diag.c:141 255→0
   2) Blv_Diag.c:157 검출 FALSE
   3) Blv_Diag.c:181 경고등 소등
6. **예상 동작(사양)**: 사양 미정의
   **코드 추론 동작**: 2.0초 점등 → 2.56초 소등 → 4.56초 재점등 반복 (D-001)
7. **판단 근거**
   - [사실] 카운터는 uint8이며 증가에 상한 검사가 없다 — 근거: SC-012
   - [추론] 지속 고장에서 순환이 발생한다 — 근거: Blv_Diag.c:141
   - 불확실성: 사양이 포화를 요구하는지 미정
8. **권장 수정 방향**: 확정 후 카운터 포화(또는 증가 정지)를 사양과 코드에 명시
9. **추가 확인 필요**: 사양 작성자 확인
   - 사양 질문: 고장 확정 후에도 저전압이 지속되면 고장 카운터는 포화(정지)되어야 합니까?
   - 위험 시나리오: 방전 배터리로 장시간 저전압 시 경고등이 깜빡이듯 꺼져 운전자가 정상으로 오인
10. **테스트 케이스**

| # | 기법 | 사전 조건 | 조작 | 기대 결과 | 관찰 수단 |
|---|---|---|---|---|---|
| 1 | BOUNDARY_VALUE | IgnSts=ON | 8500mV 10초 유지 | 사양 미정의 — 확인 필요 (현재 코드: 경고등 주기적 소등) | BlvWarnLamp, s_u8FaultCnt |

관련 정적 후보: SC-012 · 같은 코드 위치의 다른 결과: D-001

### [C-002] START 중 고장·정상 카운터를 유지하지 않고 0으로 초기화

**결함 확정** · 심각도 MEDIUM · 타이머·카운터 불일치 · 단계: 정합성

1. **요구사항**: SWR-BLV-001
   > 이때 고장 카운터와 정상 카운터는 현재 값을 유지한다.
2. **코드 위치**:
   - ✔ `Blv_Diag.c:103` BlvDiag_MainFunction() `s_u8FaultCnt` — 고장 카운터 초기화
     ```c
       101  |     {
       102  |         /* 크랭킹 중: 진단 중단 */
       103> |         s_u8FaultCnt = 0U;
       104  |         s_u16HealCnt = 0U;
       105  |         s_u8StartMaskCnt = 0U;
     ```
   - ✔ `Blv_Diag.c:104` BlvDiag_MainFunction() `s_u16HealCnt` — 정상 카운터 초기화
     ```c
       102  |         /* 크랭킹 중: 진단 중단 */
       103  |         s_u8FaultCnt = 0U;
       104> |         s_u16HealCnt = 0U;
       105  |         s_u8StartMaskCnt = 0U;
       106  |         BlvWarnLamp = 0U;
     ```
3. **유형/심각도**: 타이머·카운터 불일치 / MEDIUM
4. **사양 vs 코드**
   - 사양: S23: START 동안 고장/정상 카운터 현재 값 유지
   - 코드: L103 s_u8FaultCnt = 0U, L104 s_u16HealCnt = 0U
   - 비교: 유지 요구 대비 초기화
5. **발생 조건**: 저전압 누적(예: 1.5초) 또는 해제 누적(예: 4초) 중 IgnSts=START 진입
   1) Blv_Diag.c:100 START
   2) Blv_Diag.c:103 고장 카운터 0
   3) Blv_Diag.c:104 정상 카운터 0
   4) Blv_Diag.c:107 반환
6. **예상 동작(사양)**: START 종료 후 누적값에서 이어서 카운트
   **코드 추론 동작**: START 후 처음부터 다시 카운트 → 고장 확정/해제 지연
7. **판단 근거**
   - [사실] START 분기에서 두 카운터에 0을 대입한다 — 근거: Blv_Diag.c:103-104
   - [사실] 사양은 현재 값 유지를 요구한다 — 근거: S23
   - 불확실성: '정상 카운터'가 s_u16HealCnt를 뜻한다는 해석에 의존 (단계 1 AMBIGUOUS).
8. **권장 수정 방향**: START 분기에서 카운터 대입을 제거한다. 유예(SWR-BLV-007)와의 관계를 함께 정리한다.
9. **추가 확인 필요**: '정상 카운터' 용어 정의 확인
10. **테스트 케이스**

| # | 기법 | 사전 조건 | 조작 | 기대 결과 | 관찰 수단 |
|---|---|---|---|---|---|
| 1 | STATE_TRANSITION | IgnSts=ON, BattVolt 8500mV 1.5초 유지 | IgnSts=START 0.5초 → ON 복귀, 8500mV 유지 | 유예 3초 후 누적 1.5초에서 이어서 0.5초 뒤 FAILED | s_u8FaultCnt, Dem 이벤트 상태 |

### [C-003] 저전압 경계 9000mV 포함 여부 불일치 ('이하' vs '<')

**결함 확정** · 심각도 MEDIUM · 임계값·경계 불일치 · 단계: 정합성

1. **요구사항**: SWR-BLV-002
   > 배터리 전압이 9000mV 이하이면 저전압 조건으로 판단한다.
2. **코드 위치**:
   - ✔ `Blv_Diag.c:137` BlvDiag_MainFunction() `BLV_LOW_MV` — 임계 비교
     ```c
       135  |     u16Volt = Blv_Filter(u16Volt);
       136  | 
       137> |     bLow = (u16Volt < BLV_LOW_MV) ? TRUE : FALSE;
       138  | 
       139  |     if (bLow == TRUE)
     ```
   - ✔ `Blv_Cfg.h:7`  `BLV_LOW_MV` — 임계값 정의
     ```c
         5  | 
         6  | /* 판정 전압 [mV] */
         7> | #define BLV_LOW_MV              (9000U)
         8  | #define BLV_HEAL_MV             (10500U)
         9  | 
     ```
3. **유형/심각도**: 임계값·경계 불일치 / MEDIUM
4. **사양 vs 코드**
   - 사양: S26: 9000mV 이하 → 저전압 (경계 포함)
   - 코드: L137: u16Volt < BLV_LOW_MV(9000U) → 9000mV는 정상
   - 비교: 경계값 9000mV에서 판정이 반대
5. **발생 조건**: 필터 후 u16Volt가 정확히 9000mV
   1) Blv_Diag.c:134-135 변환·필터
   2) Blv_Diag.c:137 9000 < 9000 거짓 → bLow=FALSE
   3) Blv_Diag.c:152 히스테리시스 분기
6. **예상 동작(사양)**: 9000mV에서 저전압으로 판정
   **코드 추론 동작**: 9000mV에서 정상(히스테리시스 구간)으로 처리
7. **판단 근거**
   - [사실] 비교 연산자가 '<'이다 — 근거: Blv_Diag.c:137
   - [사실] BLV_LOW_MV = 9000U — 근거: Blv_Cfg.h:7
   - [사실] 사양 표현은 '이하' — 근거: S26
   - 불확실성: ADC 분해능(약 19.5mV/LSB)과 4점 평균 때문에 정확히 9000mV가 산출되는 입력이 존재하는지는 확인 필요.
8. **권장 수정 방향**: `u16Volt <= BLV_LOW_MV`로 수정한다.
9. **추가 확인 필요**: 변환식에서 9000mV가 산출되는 raw 조합 확인
10. **테스트 케이스**

| # | 기법 | 사전 조건 | 조작 | 기대 결과 | 관찰 수단 |
|---|---|---|---|---|---|
| 1 | BOUNDARY_VALUE | IgnSts=ON, 유예 종료 | u16Volt가 8999/9000/9001mV가 되도록 입력(또는 변수 강제) 각 2.5초 | 8999·9000: FAILED, 9001: 미보고 | bLow, s_u8FaultCnt, Dem |

### [C-001] IgnSts=ACC에서도 진단 수행

**결함 가능성 높음** · 심각도 MEDIUM · 조건식 불일치 · 단계: 정합성

1. **요구사항**: SWR-BLV-001
   > 진단은 IgnSts가 ON일 때 수행한다.
2. **코드 위치**:
   - ✔ `Blv_Diag.c:110` BlvDiag_MainFunction() `IGN_OFF` — 실행 조건
     ```c
       108  |     }
       109  | 
       110> |     if (eIgn == IGN_OFF)
       111  |     {
       112  |         return;
     ```
3. **유형/심각도**: 조건식 불일치 / MEDIUM
4. **사양 vs 코드**
   - 사양: S22: IgnSts가 ON일 때 진단 수행
   - 코드: L100 START, L110 OFF만 반환하고 ACC(=1)는 L116 이후 진단 경로로 진행
   - 비교: 사양은 ON만 허용, 코드는 ON과 ACC를 허용
5. **발생 조건**: IgnSts=ACC 상태에서 BattVolt ≤ 9000mV가 2초 이상 지속
   1) Blv_Diag.c:98 IgnSts=ACC 획득
   2) Blv_Diag.c:100 START 아님
   3) Blv_Diag.c:110 OFF 아님 → 계속
   4) Blv_Diag.c:137 저전압 판정
   5) Blv_Diag.c:164 FAULT, FAILED 보고
6. **예상 동작(사양)**: ACC에서는 진단하지 않음 (DTC·경고등 변화 없음)
   **코드 추론 동작**: ACC에서도 저전압 판정·DTC FAILED 보고·경고등 점등이 일어남. ※ 현재 코드는 C-007(유예 타이머 무한 반복) 때문에 L116 이후가 실행되지 않으므로, 이 불일치는 C-007 수정 후 드러나는 잠복 결함이다.
7. **판단 근거**
   - [사실] L110의 조건은 eIgn == IGN_OFF 뿐이다 — 근거: Blv_Diag.c:110
   - [사실] IGN_ACC=1은 L100, L110 어느 조건에도 해당하지 않는다 — 근거: Blv_Diag.h enum
   - [추론] ACC 상태에서도 L137 이후 판정이 수행된다 — 근거: 조기 반환 조건 분석
   - 불확실성: 사양 문장이 'ON일 때만'으로 명시하지 않아 ACC 제외가 의도인지 확인 필요 (단계 1 AMBIGUOUS).
8. **권장 수정 방향**: 실행 조건을 `if (eIgn != IGN_ON) { return; }` 형태로 명시하고 START 처리와 분리한다.
9. **추가 확인 필요**: ACC에서 진단 제외가 사양 의도인지 사양 작성자 확인
10. **테스트 케이스**

| # | 기법 | 사전 조건 | 조작 | 기대 결과 | 관찰 수단 |
|---|---|---|---|---|---|
| 1 | REQUIREMENT_BASED | IgnSts=ACC, 고장 이력 없음 | BattVolt 8500mV를 3초 인가 | DTC B1A00 미보고, BlvWarnLamp=0 | Dem 이벤트 상태, BlvWarnLamp |

### [C-005] 해제 카운터가 '연속 5초'를 보장하지 않음 (히스테리시스 구간에서 유지)

**결함 가능성 높음** · 심각도 MEDIUM · 타이머·카운터 불일치 · 단계: 정합성

1. **요구사항**: SWR-BLV-005, SWR-BLV-006
   > 배터리 전압이 10500mV 이상인 상태가 5초 동안 연속으로 유지되면 고장을 해제하고
2. **코드 위치**:
   - ✔ `Blv_Diag.c:149` BlvDiag_MainFunction() `s_u16HealCnt` — 해제 카운터 증가
     ```c
       147  |         if (s_u16HealCnt < BLV_HEAL_CNT)
       148  |         {
       149> |             s_u16HealCnt++;
       150  |         }
       151  |     }
     ```
   - ✔ `Blv_Diag.c:142` BlvDiag_MainFunction() `s_u16HealCnt` — 저전압에서만 초기화
     ```c
       140  |     {
       141  |         s_u8FaultCnt++;
       142> |         s_u16HealCnt = 0U;
       143  |     }
       144  |     else if (u16Volt >= BLV_HEAL_MV)
     ```
3. **유형/심각도**: 타이머·카운터 불일치 / MEDIUM
4. **사양 vs 코드**
   - 사양: S35: 10500mV 이상 연속 5초
   - 코드: s_u16HealCnt는 저전압(L142)에서만 0, 9000~10500mV 구간(L152)에서는 유지
   - 비교: 중간에 10500mV 미만으로 내려가도 누적이 이어짐
5. **발생 조건**: 10500mV 이상 3초 → 10000mV 1초 → 10500mV 이상 2초
   1) Blv_Diag.c:149 3초 누적
   2) Blv_Diag.c:152 구간 유지(초기화 없음)
   3) Blv_Diag.c:149 2초 누적 → 500
   4) Blv_Diag.c:170 해제
6. **예상 동작(사양)**: 연속 5초가 아니므로 해제하지 않음
   **코드 추론 동작**: 합산 5초로 해제·PASSED 보고
7. **판단 근거**
   - [사실] 해제 카운터 초기화는 L142(저전압)뿐이다 — 근거: Blv_Diag.c:142
   - [추론] 히스테리시스 구간이 연속성을 끊지 않는다 — 근거: Blv_Diag.c:152-155
   - 불확실성: SWR-BLV-006 '현재 진단 상태 유지'가 카운터 유지를 의도했다면 사양 간 충돌 (C-006 참조).
8. **권장 수정 방향**: 10500mV 미만이면 해제 카운터를 0으로 하거나, 사양에서 연속성 규칙을 명확히 한다.
9. **추가 확인 필요**: SWR-BLV-005/006 해석 확인
10. **테스트 케이스**

| # | 기법 | 사전 조건 | 조작 | 기대 결과 | 관찰 수단 |
|---|---|---|---|---|---|
| 1 | TIMING | 고장 확정 | 11000mV 3초 → 10000mV 1초 → 11000mV 2초 | 해제되지 않음 (연속 5초 미충족) | s_u16HealCnt, s_eBlvState, Dem |

### [C-008] 초기화 시 정상(해제) 카운터 s_u16HealCnt 미초기화

**결함 가능성 높음** · 심각도 MEDIUM · 초기화·복구 불일치 · 단계: 정합성

1. **요구사항**: SWR-BLV-009
   > 제어기 초기화 시 고장 카운터, 정상 카운터, 진단 상태는 초기값으로 설정하고
2. **코드 위치**:
   - ✔ `Blv_Diag.c:82` BlvDiag_Init() `s_u8FaultCnt` — 고장 카운터만 초기화
     ```c
        80  |     s_eBlvState = BLV_STATE_NORMAL;
        81  |     s_bFaultDetected = FALSE;
        82> |     s_u8FaultCnt = 0U;
        83  |     s_u8StartMaskCnt = BLV_START_MASK_CNT;
        84  | #if (BLV_USE_FILTER == STD_ON)
     ```
3. **유형/심각도**: 초기화·복구 불일치 / MEDIUM
4. **사양 vs 코드**
   - 사양: S48: 고장·정상 카운터, 진단 상태 초기화
   - 코드: BlvDiag_Init(L78-89)에 s_u16HealCnt 대입 없음
   - 비교: 정상 카운터 초기화 누락
5. **발생 조건**: Wake-up 등으로 Init이 재호출되고 직전 해제 카운터가 남아 있는 경우
   1) EcuM → BlvDiag_Init (s_u16HealCnt 이전 값 유지)
   2) Blv_Diag.c:170 해제 판정에 이전 누적 사용
6. **예상 동작(사양)**: 초기화 후 해제 카운터 0부터 계수
   **코드 추론 동작**: 재초기화 시 이전 누적값이 남아 해제가 앞당겨질 수 있음
7. **판단 근거**
   - [사실] Init에 s_u16HealCnt 대입이 없다 — 근거: Blv_Diag.c:78-89
   - [추론] 최초 전원 투입은 정적 0 초기화로 문제없고 재호출 시에만 영향 — 근거: C 정적 저장기간
   - [가설] Init이 운전 중 재호출된다 — 근거: env.json notes: Wake-up 시 재호출
   - 불확실성: Init 재호출 시나리오의 실제 존재 여부
8. **권장 수정 방향**: Init에 `s_u16HealCnt = 0U;` 추가
9. **추가 확인 필요**: EcuM Wake-up 시 BlvDiag_Init 호출 여부
10. **테스트 케이스**

| # | 기법 | 사전 조건 | 조작 | 기대 결과 | 관찰 수단 |
|---|---|---|---|---|---|
| 1 | STATE_TRANSITION | 고장 확정 후 11000mV 4초 누적 | BlvDiag_Init 호출(Wake-up 모사) → 저전압 재확정 → 11000mV 1.5초 | 해제되지 않음(5초 필요) | s_u16HealCnt, s_eBlvState |

### [D-003] ADC ISR 공유 변수의 비원자적 접근 (샘플 카운트 유실, 16bit 분할 읽기)

**잠재 위험** · 심각도 MEDIUM · 동시성 · 단계: 코드 결함

1. **요구사항**: SWR-BLV-003
   > 저전압 조건이 2초 동안 연속으로 유지되면
2. **코드 위치**:
   - ✔ `Blv_Diag.c:122` BlvDiag_MainFunction() `g_u8AdcSampleCnt` — 읽기
     ```c
       120  |     }
       121  | 
       122> |     if (g_u8AdcSampleCnt == 0U)
       123  |     {
       124  |         return;     /* 새 샘플 없음 */
     ```
   - ✔ `Blv_Diag.c:127` BlvDiag_MainFunction() `g_u8AdcSampleCnt` — 쓰기
     ```c
       125  |     }
       126  |     u16Raw = g_u16BattRaw;
       127> |     g_u8AdcSampleCnt = 0U;
       128  | 
       129  |     if (u16Raw == BLV_INVALID_RAW)
     ```
   - ✔ `Blv_Diag.c:126` BlvDiag_MainFunction() `g_u16BattRaw` — 16bit 읽기
     ```c
       124  |         return;     /* 새 샘플 없음 */
       125  |     }
       126> |     u16Raw = g_u16BattRaw;
       127  |     g_u8AdcSampleCnt = 0U;
       128  | 
     ```
   - ✔ `Adc_Isr.c:12` Adc_ConvEndIsr() `g_u8AdcSampleCnt` — ISR 증가
     ```c
        10  | {
        11  |     g_u16BattRaw = Adc_ReadResultReg(BLV_ADC_CH);
        12> |     g_u8AdcSampleCnt++;
        13  | }
     ```
3. **유형/심각도**: 동시성 / MEDIUM
4. **사양 vs 코드**
   - 사양: 주기 판정의 연속성 전제
   - 코드: L122 읽기~L127 쓰기 사이 ISR 증가분이 0으로 덮임; 16bit g_u16BattRaw를 8bit 접근 MCU에서 보호 없이 읽음
   - 비교: 보호 구간 없음
5. **발생 조건**: L122~L127 실행 중 ADC 변환 완료 인터럽트
   1) Blv_Diag.c:122 읽기
   2) Adc_Isr.c:12 ISR 증가
   3) Blv_Diag.c:127 0으로 덮음
6. **예상 동작(사양)**: 모든 샘플 반영
   **코드 추론 동작**: 다음 주기 '새 샘플 없음'으로 판정 생략 가능, 분할 읽기 시 순간 오값
7. **판단 근거**
   - [사실] ISR이 증가, 태스크가 읽고 0을 쓴다 — 근거: SC-010
   - [사실] 보호 함수 호출이 없다 — 근거: Blv_Diag.c:91-182
   - [가설] 16bit 로드가 원자적이지 않다 — 근거: atomic_access_bits=8
   - 불확실성: 명령 수준 원자성, 실행 시간 미확인
8. **권장 수정 방향**: SuspendAllInterrupts/ResumeAllInterrupts로 L122~L127을 보호하고 volatile을 선언
9. **추가 확인 필요**: 대상 MCU의 16bit 로드 원자성; L122~L127 실행 시간
10. **테스트 케이스**

| # | 기법 | 사전 조건 | 조작 | 기대 결과 | 관찰 수단 |
|---|---|---|---|---|---|
| 1 | CONCURRENCY | SIL에서 ISR 주입 가능 | L126 실행 직후 ISR 강제 발생 | 샘플 유실 없음 | g_u8AdcSampleCnt 추적 |

관련 정적 후보: SC-003, SC-004, SC-010, SC-011 · 같은 코드 위치의 다른 결과: G-005

### [G-004] 이전 사이클 확정 DTC의 해제(PASSED) 경로 미정의

**잠재 위험** · 심각도 MEDIUM · 사양 공백 · 단계: 사양 공백

1. **요구사항**: SWR-BLV-009, SWR-BLV-005
   > 단, 이전 운전 사이클에서 확정된 DTC는 유지한다.
2. **코드 위치**:
   - ✔ `Blv_Diag.c:80` BlvDiag_Init() `BLV_STATE_NORMAL` — Init 상태 NORMAL
     ```c
        78  | void BlvDiag_Init(void)
        79  | {
        80> |     s_eBlvState = BLV_STATE_NORMAL;
        81  |     s_bFaultDetected = FALSE;
        82  |     s_u8FaultCnt = 0U;
     ```
   - ✔ `Blv_Diag.c:173` BlvDiag_MainFunction() `DEM_EVENT_STATUS_PASSED` — PASSED는 FAULT→NORMAL에서만
     ```c
       171  |             {
       172  |                 s_eBlvState = BLV_STATE_NORMAL;
       173> |                 Dem_SetEventStatus(DemConf_DemEventParameter_B1A00, DEM_EVENT_STATUS_PASSED);
       174  |             }
       175  |             break;
     ```
3. **유형/심각도**: 사양 공백 / MEDIUM
4. **사양 vs 코드**
   - 사양: 이전 확정 DTC 유지만 정의, 해제 조건 없음
   - 코드: Init 후 상태 NORMAL → 정상 전압이어도 PASSED 보고 경로 없음
   - 비교: 사양 공백
5. **발생 조건**: 이전 사이클에 확정, 이번 사이클은 정상 전압
   1) EcuM → BlvDiag_Init: NORMAL
   2) Blv_Diag.c:161 NORMAL: 고장 미검출 → 보고 없음
6. **예상 동작(사양)**: 사양 미정의
   **코드 추론 동작**: Dem에 PASSED가 보고되지 않아 테스트 미완료/확정 상태가 유지될 수 있음
7. **판단 근거**
   - [사실] PASSED 보고는 L173(FAULT→NORMAL)뿐 — 근거: Blv_Diag.c:173
   - [가설] Dem 설정(에이징/운전 사이클 처리)에 따라 DTC가 남는다 — 근거: Dem 설정 미제공
   - 불확실성: Dem 설정 미제공
8. **권장 수정 방향**: 정상 판정 시 PASSED 보고 정책 정의
9. **추가 확인 필요**: Dem 에이징·운전 사이클 설정
   - 사양 질문: 이전 사이클에 확정된 DTC는 이번 사이클 정상 전압 확인 시 PASSED로 보고합니까?
   - 위험 시나리오: 실제로 회복된 차량에 DTC가 계속 남아 불필요한 정비
10. **테스트 케이스**

| # | 기법 | 사전 조건 | 조작 | 기대 결과 | 관찰 수단 |
|---|---|---|---|---|---|
| 1 | REQUIREMENT_BASED | 이전 사이클 B1A00 확정 | 재시동, 12V 10초 | 사양 미정의 — 확인 필요 (현재 코드: PASSED 미보고) | Dem 상태 바이트 |

### [G-005] ADC 샘플 갱신 중단(타임아웃) 시 동작 미정의

**잠재 위험** · 심각도 MEDIUM · 사양 공백 · 단계: 사양 공백

1. **요구사항**: SWR-BLV-003, SWR-BLV-008
   > BattVolt 측정값이 무효(ADC 오류 값)인 경우 해당 주기의 판정을 수행하지 않는다.
2. **코드 위치**:
   - ✔ `Blv_Diag.c:122` BlvDiag_MainFunction() `g_u8AdcSampleCnt` — 새 샘플 없으면 반환
     ```c
       120  |     }
       121  | 
       122> |     if (g_u8AdcSampleCnt == 0U)
       123  |     {
       124  |         return;     /* 새 샘플 없음 */
     ```
3. **유형/심각도**: 사양 공백 / MEDIUM
4. **사양 vs 코드**
   - 사양: 샘플 미갱신 정의 없음
   - 코드: g_u8AdcSampleCnt==0이면 무기한 판정 생략 (진단 정지 감지 없음)
   - 비교: 사양 공백
5. **발생 조건**: ADC ISR 정지 (ADC 고장, 인터럽트 미발생)
   1) Blv_Diag.c:122 0 → 반환 (매 주기)
6. **예상 동작(사양)**: 사양 미정의
   **코드 추론 동작**: 진단이 조용히 정지, 경고등·DTC 마지막 상태 유지
7. **판단 근거**
   - [사실] 새 샘플이 없으면 L124에서 반환 — 근거: Blv_Diag.c:122-125
   - 불확실성: ADC 감시가 다른 모듈에 있는지 미확인
8. **권장 수정 방향**: 샘플 미갱신 시간 한계와 처리 정의
9. **추가 확인 필요**: ADC 드라이버 감시 기능
   - 사양 질문: 배터리 전압 샘플이 일정 시간 갱신되지 않으면 어떻게 처리합니까?
   - 위험 시나리오: ADC 고장 시 저전압이 검출되지 않음
10. **테스트 케이스**

| # | 기법 | 사전 조건 | 조작 | 기대 결과 | 관찰 수단 |
|---|---|---|---|---|---|
| 1 | FAULT_INJECTION | IgnSts=ON, 저전압 1초 누적 | ADC ISR 중지 5초 | 사양 미정의 — 확인 필요 (현재 코드: 진단 정지) | s_u8FaultCnt, Dem |

같은 코드 위치의 다른 결과: D-003

### [D-004] BLV_STATE_SUSPEND 상태 미사용 (START 중단 상태 설계 누락 가능)

**검토 필요** · 심각도 LOW · 기능·논리 오류 · 단계: 코드 결함

1. **요구사항**: SWR-BLV-001
   > IgnSts가 START인 동안에는 ... 진단을 중단하며
2. **코드 위치**:
   - ✔ `Blv_Diag.c:159` BlvDiag_MainFunction() `s_eBlvState` — switch
     ```c
       157  |     s_bFaultDetected = (s_u8FaultCnt >= BLV_CONFIRM_CNT) ? TRUE : FALSE;
       158  | 
       159> |     switch (s_eBlvState)
       160  |     {
       161  |         case BLV_STATE_NORMAL:
     ```
   - ✔ `Blv_Diag.c:14`  `BLV_STATE_SUSPEND` — 정의
     ```c
        12  |     BLV_STATE_NORMAL = 0,
        13  |     BLV_STATE_FAULT,
        14> |     BLV_STATE_SUSPEND
        15  | } Blv_StateType;
        16  | 
     ```
3. **유형/심각도**: 기능·논리 오류 / LOW
4. **사양 vs 코드**
   - 사양: START 중 진단 중단
   - 코드: SUSPEND 정의만 있고 전이 없음; START는 조기 반환으로 처리
   - 비교: 설계 의도 불명
5. **발생 조건**: -
6. **예상 동작(사양)**: -
   **코드 추론 동작**: SUSPEND 상태 미도달
7. **판단 근거**
   - [사실] BLV_STATE_SUSPEND로의 대입이 없다 — 근거: SC-002
   - [가설] START 처리를 SUSPEND로 하려던 설계 잔재 — 근거: -
   - 불확실성: 설계 문서 미제공
8. **권장 수정 방향**: 미사용이면 제거, 필요하면 START 처리 구현
9. **추가 확인 필요**: 설계 문서의 상태 정의
10. **테스트 케이스**

| # | 기법 | 사전 조건 | 조작 | 기대 결과 | 관찰 수단 |
|---|---|---|---|---|---|
| 1 | STATE_TRANSITION | IgnSts=ON | START 진입 | 설계 기준 확인 | s_eBlvState |

관련 정적 후보: SC-002, SC-013

### [C-006] 히스테리시스 구간에서 고장 카운터 유지 → '연속 2초' 요구와 해석 충돌

**사양 보완 필요** · 심각도 MEDIUM · 사양 모호 · 단계: 정합성

1. **요구사항**: SWR-BLV-003, SWR-BLV-006
   > 저전압 조건이 2초 동안 연속으로 유지되면 / 현재 진단 상태를 유지한다.
2. **코드 위치**:
   - ✔ `Blv_Diag.c:152` BlvDiag_MainFunction() `else` — 카운터 유지
     ```c
       150  |         }
       151  |     }
       152> |     else
       153  |     {
       154  |         /* 히스테리시스 구간: 현재 상태 유지 */
     ```
3. **유형/심각도**: 사양 모호 / MEDIUM
4. **사양 vs 코드**
   - 사양: S29: 저전압 연속 2초 / S39: 구간에서 상태 유지
   - 코드: 9000~10500mV 구간에서 s_u8FaultCnt 유지 → 저전압 1.5초, 구간 0.1초, 저전압 0.5초로 확정
   - 비교: '연속' 요구와 '유지' 요구의 우선순위가 정의되지 않음
5. **발생 조건**: 저전압과 히스테리시스 구간을 오가는 전압
   1) Blv_Diag.c:141 누적
   2) Blv_Diag.c:152 유지
   3) Blv_Diag.c:141 누적 계속
6. **예상 동작(사양)**: 사양 미정의 — 확인 필요
   **코드 추론 동작**: 구간 진입 시에도 누적 유지(비연속 합산으로 확정)
7. **판단 근거**
   - [사실] 구간에서 카운터를 변경하지 않는다 — 근거: Blv_Diag.c:152-155
   - [가설] 사양 작성자는 '진단 상태'를 FAULT/NORMAL로만 의도했을 수 있다 — 근거: S39
   - 불확실성: 사양 해석에 따라 정상일 수도 결함일 수도 있음
8. **권장 수정 방향**: 사양 확정 후 카운터 처리 결정
9. **추가 확인 필요**: 사양 작성자 확인
   - 사양 질문: 저전압 누적 중 9000~10500mV 구간에 진입하면 고장 카운터를 유지합니까, 초기화합니까?
10. **테스트 케이스**

| # | 기법 | 사전 조건 | 조작 | 기대 결과 | 관찰 수단 |
|---|---|---|---|---|---|
| 1 | TIMING | IgnSts=ON | 8500mV 1.5초 → 9500mV 0.1초 → 8500mV 0.6초 | 사양 미정의 — 확인 필요 (현재 코드: 2.1초 시점 FAILED) | s_u8FaultCnt, Dem |

### [G-002] 무효값(ADC 오류 값) 및 범위 초과(>20000mV) 처리 미정의

**사양 보완 필요** · 심각도 MEDIUM · 사양 모호 · 단계: 사양 공백

1. **요구사항**: SWR-BLV-008
   > BattVolt 측정값이 무효(ADC 오류 값)인 경우 해당 주기의 판정을 수행하지 않는다.
2. **코드 위치**:
   - ✔ `Blv_Diag.c:129` BlvDiag_MainFunction() `BLV_INVALID_RAW` — 0xFFFF만 무효
     ```c
       127  |     g_u8AdcSampleCnt = 0U;
       128  | 
       129> |     if (u16Raw == BLV_INVALID_RAW)
       130  |     {
       131  |         return;
     ```
   - ✔ `Blv_Cfg.h:20`  `BLV_INVALID_RAW` — 0xFFFFU
     ```c
        18  | #define BLV_ADC_MAX             (1023U)
        19  | #define BLV_DIVIDER_RATIO       (4U)
        20> | #define BLV_INVALID_RAW         (0xFFFFU)
        21  | 
        22  | /* 이동평균 필터 */
     ```
3. **유형/심각도**: 사양 모호 / MEDIUM
4. **사양 vs 코드**
   - 사양: 무효의 정의 없음, 범위 0~20000mV
   - 코드: raw==0xFFFF만 무효; 범위 초과 raw는 그대로 변환·판정
   - 비교: 무효 판정 기준 불명
5. **발생 조건**: ADC 단선/단락으로 raw가 0 또는 1023 고정
   1) Blv_Diag.c:129 0xFFFF 아님
   2) Blv_Diag.c:134 변환
   3) Blv_Diag.c:137 판정
6. **예상 동작(사양)**: 사양 미정의
   **코드 추론 동작**: raw=0 → 0mV → 저전압으로 FAILED; raw=1023 → 20000mV → 정상
7. **판단 근거**
   - [사실] 무효 검사는 0xFFFF 비교뿐 — 근거: Blv_Diag.c:129
   - [가설] ADC 드라이버가 오류 시 0xFFFF를 쓴다 — 근거: Adc_ReadResultReg 미제공
   - 불확실성: ADC 드라이버의 오류 표현 방식 미확인
8. **권장 수정 방향**: 무효 정의를 사양에 추가하고 코드 일치
9. **추가 확인 필요**: Adc_ReadResultReg 오류 반환값
   - 사양 질문: 무효로 판단할 값(0xFFFF, raw 0, 20000mV 초과 등)과 그때의 카운터 처리를 정의해 주십시오.
   - 위험 시나리오: 센서 단락을 저전압 고장으로 기록하여 오정비 유발
10. **테스트 케이스**

| # | 기법 | 사전 조건 | 조작 | 기대 결과 | 관찰 수단 |
|---|---|---|---|---|---|
| 1 | FAULT_INJECTION | IgnSts=ON | raw=0 3초 | 사양 미정의 — 확인 필요 (현재 코드: FAILED) | Dem |

### [G-003] IgnSts OFF/ACC 전환 시 경고등·진단 상태 처리 미정의

**사양 보완 필요** · 심각도 MEDIUM · 사양 공백 · 단계: 사양 공백

1. **요구사항**: SWR-BLV-001, SWR-BLV-004
   > 진단은 IgnSts가 ON일 때 수행한다.
2. **코드 위치**:
   - ✔ `Blv_Diag.c:112` BlvDiag_MainFunction() `return` — OFF 반환
     ```c
       110  |     if (eIgn == IGN_OFF)
       111  |     {
       112> |         return;
       113  |     }
       114  | 
     ```
3. **유형/심각도**: 사양 공백 / MEDIUM
4. **사양 vs 코드**
   - 사양: OFF/ACC에서 출력 처리 없음
   - 코드: OFF에서는 경고등을 변경하지 않고 반환 → 마지막 값 유지
   - 비교: 사양 공백
5. **발생 조건**: 고장 확정(점등) 중 IgnSts=OFF
   1) Blv_Diag.c:110 OFF → 반환 (BlvWarnLamp 유지)
6. **예상 동작(사양)**: 사양 미정의
   **코드 추론 동작**: IG OFF 동안 경고등 요청 1 유지
7. **판단 근거**
   - [사실] OFF 분기에서 출력 대입이 없다 — 근거: Blv_Diag.c:110-113
   - 불확실성: 다른 모듈이 IG OFF 시 출력을 차단하는지 미확인
8. **권장 수정 방향**: OFF/ACC 진입 시 출력 정책을 사양에 정의
9. **추가 확인 필요**: 경고등 구동 모듈의 IG OFF 처리
   - 사양 질문: IgnSts가 OFF/ACC로 바뀌면 경고등 요청과 진단 상태를 유지합니까, 초기화합니까?
   - 위험 시나리오: IG OFF 후에도 경고 요청이 유지되어 암전류 증가 또는 다음 시동 시 잘못된 경고
10. **테스트 케이스**

| # | 기법 | 사전 조건 | 조작 | 기대 결과 | 관찰 수단 |
|---|---|---|---|---|---|
| 1 | STATE_TRANSITION | 고장 확정·점등 | IgnSts=OFF | 사양 미정의 — 확인 필요 (현재 코드: 요청 1 유지) | BlvWarnLamp |

### [G-006] 초기 필터 미충전 구간(4샘플 미만)의 판정 여부 미정의

**사양 보완 필요** · 심각도 LOW · 사양 공백 · 단계: 사양 공백

1. **요구사항**: SWR-BLV-002
   > 배터리 전압이 9000mV 이하이면 저전압 조건으로 판단한다.
2. **코드 위치**:
   - ✔ `Blv_Diag.c:60` Blv_Filter() `s_u8BufNum` — 가용 샘플 평균
     ```c
        58  |     }
        59  | 
        60> |     for (u8I = 0U; u8I < s_u8BufNum; u8I++)
        61  |     {
        62  |         u32Sum += s_au16Buf[u8I];
     ```
3. **유형/심각도**: 사양 공백 / LOW
4. **사양 vs 코드**
   - 사양: 필터 정의 없음
   - 코드: 4개 미만이면 가용 샘플만 평균하여 즉시 판정
   - 비교: 사양 공백
5. **발생 조건**: 초기화 직후 첫 3주기
   1) Blv_Diag.c:55 BufNum 증가
   2) Blv_Diag.c:67 평균
6. **예상 동작(사양)**: 사양 미정의
   **코드 추론 동작**: 첫 주기 단일 샘플로 판정
7. **판단 근거**
   - [사실] s_u8BufNum 기준 평균 — 근거: Blv_Diag.c:60-68
   - 불확실성: 필터 사용이 사양 요구인지 불명
8. **권장 수정 방향**: 필터 동작을 사양/설계서에 명시
9. **추가 확인 필요**: -
   - 사양 질문: 초기 필터가 채워지기 전에도 판정합니까?
   - 위험 시나리오: 초기 노이즈 샘플로 카운트 시작
10. **테스트 케이스**

| # | 기법 | 사전 조건 | 조작 | 기대 결과 | 관찰 수단 |
|---|---|---|---|---|---|
| 1 | EQUIVALENCE_CLASS | Init 직후 | 첫 샘플 8500mV, 이후 12000mV | 사양 미정의 — 확인 필요 | u16Volt |


## 3. 사양 보완 질문 목록

| ID | 관련 요구사항 | 질문 | 위험 시나리오 | 현재 코드 동작 |
|---|---|---|---|---|
| G-001 | SWR-BLV-003, SWR-BLV-004 | 고장 확정 후에도 저전압이 지속되면 고장 카운터는 포화(정지)되어야 합니까? | 방전 배터리로 장시간 저전압 시 경고등이 깜빡이듯 꺼져 운전자가 정상으로 오인 | 2.0초 점등 → 2.56초 소등 → 4.56초 재점등 반복 (D-001) |
| G-004 | SWR-BLV-009, SWR-BLV-005 | 이전 사이클에 확정된 DTC는 이번 사이클 정상 전압 확인 시 PASSED로 보고합니까? | 실제로 회복된 차량에 DTC가 계속 남아 불필요한 정비 | Dem에 PASSED가 보고되지 않아 테스트 미완료/확정 상태가 유지될 수 있음 |
| G-005 | SWR-BLV-003, SWR-BLV-008 | 배터리 전압 샘플이 일정 시간 갱신되지 않으면 어떻게 처리합니까? | ADC 고장 시 저전압이 검출되지 않음 | 진단이 조용히 정지, 경고등·DTC 마지막 상태 유지 |
| C-006 | SWR-BLV-003, SWR-BLV-006 | 저전압 누적 중 9000~10500mV 구간에 진입하면 고장 카운터를 유지합니까, 초기화합니까? |  | 구간 진입 시에도 누적 유지(비연속 합산으로 확정) |
| G-002 | SWR-BLV-008 | 무효로 판단할 값(0xFFFF, raw 0, 20000mV 초과 등)과 그때의 카운터 처리를 정의해 주십시오. | 센서 단락을 저전압 고장으로 기록하여 오정비 유발 | raw=0 → 0mV → 저전압으로 FAILED; raw=1023 → 20000mV → 정상 |
| G-003 | SWR-BLV-001, SWR-BLV-004 | IgnSts가 OFF/ACC로 바뀌면 경고등 요청과 진단 상태를 유지합니까, 초기화합니까? | IG OFF 후에도 경고 요청이 유지되어 암전류 증가 또는 다음 시동 시 잘못된 경고 | IG OFF 동안 경고등 요청 1 유지 |
| G-006 | SWR-BLV-002 | 초기 필터가 채워지기 전에도 판정합니까? | 초기 노이즈 샘플로 카운트 시작 | 첫 주기 단일 샘플로 판정 |

## 4. 요구사항 구조화 결과

### SWR-BLV-001 진단 실행 조건
- 원문(S22-S23): 진단은 IgnSts가 ON일 때 수행한다. IgnSts가 START인 동안에는 크랭킹에 의한 일시적 전압 강하가 발생하므로 진단을 중단하며, 이때 고장 카운터와 정상 카운터는 현재 값을 유지한다.
- 목적: 진단 수행 구간을 IGN ON으로 한정하고, 크랭킹(START) 중에는 진단을 중단하되 카운터를 보존한다.

| 요소 | 이름 | 내용 | 상태 |
|---|---|---|---|
| 입력 | IgnSts | 이그니션 상태 OFF/ACC/ON/START | 정의됨 |
| 실행 조건 | 진단 수행 | IgnSts == ON | 정의됨 |
| 실행 조건 | 진단 중단 | IgnSts == START | 정의됨 |
| 예외 | START 중 카운터 | 고장 카운터와 정상 카운터는 현재 값 유지 | 정의됨 |
- **AMBIGUOUS** ACC 상태: "진단은 IgnSts가 ON일 때 수행한다." — ON에서 수행한다고만 되어 있어 ACC/OFF에서 '수행하지 않음'이 명시적이지 않다. → 질문: IgnSts가 ACC일 때 진단을 수행하지 않는 것이 맞습니까? (OFF도 동일)
- **AMBIGUOUS** 정상 카운터: "고장 카운터와 정상 카운터" — '정상 카운터'가 SWR-BLV-005의 해제 판정용 카운터인지 용어 정의가 없다. → 질문: '정상 카운터'는 고장 해제(10500mV 이상 5초) 판정용 카운터를 의미합니까?
- **UNDEFINED** ON 이외 상태에서의 출력: 진단을 수행하지 않는 IgnSts(OFF/ACC)에서 경고등·진단 상태를 어떻게 유지/초기화할지 정의되지 않음 → 질문: IgnSts가 OFF/ACC로 바뀌면 경고등과 진단 상태는 유지합니까, 초기화합니까?

### SWR-BLV-002 저전압 판정
- 원문(S26-S26): 배터리 전압이 9000mV 이하이면 저전압 조건으로 판단한다.
- 목적: 저전압 조건을 정의한다.

| 요소 | 이름 | 내용 | 상태 |
|---|---|---|---|
| 입력 | BattVolt | 배터리 전압 (분압 보정 후) 0 ~ 20000mV | 정의됨 |
| 판단 조건 | 저전압 조건 | BattVolt <= 9000mV 9000mV | 정의됨 |
| 임계/경계 | 저전압 임계값 | 9000mV (INCLUSIVE) | 정의됨 |

### SWR-BLV-003 고장 확정
- 원문(S29-S29): 저전압 조건이 2초 동안 연속으로 유지되면 고장으로 확정하고 DTC B1A00을 FAILED로 보고한다.
- 목적: 저전압 지속 시간으로 고장을 확정하고 DTC를 보고한다.

| 요소 | 이름 | 내용 | 상태 |
|---|---|---|---|
| 출력 | DTC B1A00 | 배터리 저전압 고장 FAILED / PASSED | 정의됨 |
| 판단 조건 | 고장 확정 | 저전압 조건 2초 연속 2s | 정의됨 |
| 상태 | 고장 확정 | Confirmed | 정의됨 |
| 상태 전이 | 정상(미확정) → 고장 확정 | 저전압 조건 2초 연속 | 정의됨 |
| 타이머/카운터(DEBOUNCE) | 고장 확정 타이머 | 2s 저전압 조건 연속 시간. (추론) 10ms 주기 기준 200회 | 정의됨 |
- **AMBIGUOUS** 연속성 판단: "2초 동안 연속으로 유지되면" — 히스테리시스 구간(SWR-BLV-006) 진입, 무효 샘플(SWR-BLV-008), 진단 중단(START)이 '연속'을 끊는지 정의되지 않음 → 질문: 저전압 누적 중 히스테리시스 구간 진입·무효 샘플·START가 발생하면 2초 카운트를 초기화합니까, 유지합니까?
- **UNDEFINED** 확정 후 카운터: 고장 확정 후 저전압이 계속될 때 카운터의 포화/정지 여부 → 질문: 고장 확정 이후에도 저전압이 지속되면 고장 카운터는 포화(정지)되어야 합니까?

### SWR-BLV-004 경고등 출력
- 원문(S32-S32): 고장이 확정된 상태에서는 경고등을 점등한다. 고장이 해제되면 경고등을 소등한다.
- 목적: 고장 확정 상태를 운전자에게 알린다.

| 요소 | 이름 | 내용 | 상태 |
|---|---|---|---|
| 출력 | BlvWarnLamp | 저전압 경고등 요청 0: 소등, 1: 점등 | 정의됨 |
| 판단 조건 | 점등 | 고장 확정 상태 | 정의됨 |
| 판단 조건 | 소등 | 고장 해제 | 정의됨 |

### SWR-BLV-005 고장 해제
- 원문(S35-S36): 고장 확정 이후 배터리 전압이 10500mV 이상인 상태가 5초 동안 연속으로 유지되면 고장을 해제하고 DTC B1A00을 PASSED로 보고한다. 고장 해제 시 고장 카운터는 0으로 초기화한다.
- 목적: 회복 조건과 회복 시 처리를 정의한다.

| 요소 | 이름 | 내용 | 상태 |
|---|---|---|---|
| 입력 | BattVolt | 배터리 전압 (분압 보정 후) 0 ~ 20000mV | 정의됨 |
| 출력 | DTC B1A00 | 배터리 저전압 고장 FAILED / PASSED | 정의됨 |
| 예외 | 해제 시 카운터 | 고장 카운터 0 0 | 정의됨 |
| 상태 전이 | 고장 확정 → 정상 | BattVolt >= 10500mV 5초 연속 | 정의됨 |
| 타이머/카운터(DEBOUNCE) | 고장 해제 타이머 | 5s 10500mV 이상 연속 시간. (추론) 10ms 주기 기준 500회 | 정의됨 |
| 임계/경계 | 해제 임계값 | 10500mV (INCLUSIVE) | 정의됨 |

### SWR-BLV-006 히스테리시스
- 원문(S39-S39): 배터리 전압이 9000mV 초과 10500mV 미만인 구간에서는 현재 진단 상태를 유지한다.
- 목적: 판정 채터링 방지.

| 요소 | 이름 | 내용 | 상태 |
|---|---|---|---|
| 입력 | BattVolt | 배터리 전압 (분압 보정 후) 0 ~ 20000mV | 정의됨 |
| 임계/경계 | 히스테리시스 하한 | 9000mV (EXCLUSIVE) | 정의됨 |
| 임계/경계 | 히스테리시스 상한 | 10500mV (EXCLUSIVE) | 정의됨 |
- **AMBIGUOUS** 유지 대상: "현재 진단 상태를 유지한다." — '진단 상태'에 진행 중인 고장/해제 카운터가 포함되는지 불명확 (상태만 유지하고 카운터는 초기화할 수도 있음) → 질문: 히스테리시스 구간에서 진행 중이던 고장 카운터·해제 카운터는 유지합니까, 초기화합니까?

### SWR-BLV-007 시동 후 진단 유예
- 원문(S42-S42): IgnSts가 START에서 ON으로 전환된 후 3초 동안은 진단 판정을 유예한다.
- 목적: 크랭킹 직후 전압 회복 구간의 오검출 방지.

| 요소 | 이름 | 내용 | 상태 |
|---|---|---|---|
| 입력 | IgnSts | 이그니션 상태 OFF/ACC/ON/START | 정의됨 |
| 타이머/카운터(DELAY) | 시동 후 유예 타이머 | 3s START→ON 전환 시점부터 계시. (추론) 10ms 주기 기준 300회 | 정의됨 |
- **UNDEFINED** 유예 중 카운터: 유예 중 고장/해제 카운터를 유지할지 초기화할지 → 질문: 유예 기간 동안 카운터는 유지합니까?
- **UNDEFINED** 전원 투입 시 유예: START를 거치지 않은 ON(예: 리셋 후)에서의 유예 여부 → 질문: 제어기 초기화 직후에도 3초 유예를 적용합니까?

### SWR-BLV-008 입력 유효성
- 원문(S45-S45): BattVolt 측정값이 무효(ADC 오류 값)인 경우 해당 주기의 판정을 수행하지 않는다.
- 목적: 무효 입력으로 인한 오판정 방지.

| 요소 | 이름 | 내용 | 상태 |
|---|---|---|---|
| 입력 | BattVolt | 배터리 전압 (분압 보정 후) 0 ~ 20000mV | 정의됨 |
| 예외 | 무효 입력 | 판정 미수행 | AMBIGUOUS |
- **AMBIGUOUS** 무효값 정의: "무효(ADC 오류 값)" — 어떤 값이 ADC 오류 값인지 정의되지 않음 (특정 코드값? 범위 0~20000 초과?) → 질문: 무효로 판단할 값(예: 0xFFFF, 20000mV 초과, ADC 오류 플래그)을 정의해 주십시오.

### SWR-BLV-009 초기화
- 원문(S48-S49): 제어기 초기화 시 고장 카운터, 정상 카운터, 진단 상태는 초기값으로 설정하고 경고등은 소등한다. 단, 이전 운전 사이클에서 확정된 DTC는 유지한다.
- 목적: 초기화 시 내부 상태 정의.

| 요소 | 이름 | 내용 | 상태 |
|---|---|---|---|
| 출력 | BlvWarnLamp | 저전압 경고등 요청 0: 소등, 1: 점등 | 정의됨 |
| 출력 | DTC B1A00 | 배터리 저전압 고장 FAILED / PASSED | 정의됨 |
| 예외 | 확정 DTC | 이전 사이클 확정 DTC 유지 | 정의됨 |
- **UNDEFINED** 초기값: 카운터·진단 상태의 '초기값'이 무엇인지 (0, 정상 상태 등) 명시되지 않음 → 질문: 초기값은 카운터 0, 진단 상태 '정상'이 맞습니까?
- **UNDEFINED** 이전 확정 DTC의 해제: 이전 사이클에서 확정된 DTC를 이번 사이클에서 어떤 조건으로 PASSED 보고하는지 → 질문: 이전 사이클 확정 DTC는 이번 사이클에서 정상 전압 5초 확인 시 PASSED로 보고합니까?

### SWR-BLV-010 경고등 우선순위
- 원문(S52-S52): 진단 중단 조건(IgnSts = START)에서도 고장 확정 상태라면 경고등 점등을 유지한다.
- 목적: 크랭킹 중에도 확정된 고장 경고를 유지.

| 요소 | 이름 | 내용 | 상태 |
|---|---|---|---|
| 입력 | IgnSts | 이그니션 상태 OFF/ACC/ON/START | 정의됨 |
| 출력 | BlvWarnLamp | 저전압 경고등 요청 0: 소등, 1: 점등 | 정의됨 |
| 우선순위 | 경고등 유지 | 고장 확정 상태 > 진단 중단 | 정의됨 |


## 5. 추적 매트릭스

### SWR-BLV-001 — 추적됨 (신뢰도 HIGH)

- ✔ `Blv_Diag.c:98` BlvDiag_MainFunction() [INPUT_READ] IgnSts: IgnSts 획득
- ✔ `Blv_Diag.c:100-107` BlvDiag_MainFunction() [CONDITION] START 시 진단 중단: START면 카운터·경고등을 0으로 만들고 반환
- ✔ `Blv_Diag.c:110-113` BlvDiag_MainFunction() [CONDITION] ON에서만 수행: OFF만 반환, ACC는 진단 계속
- 호출 경로: OsTask_10ms → BlvDiag_MainFunction

### SWR-BLV-002 — 추적됨 (신뢰도 HIGH)

- ✔ `Blv_Diag.c:137` BlvDiag_MainFunction() [THRESHOLD] 9000mV 이하: bLow = (u16Volt < BLV_LOW_MV)
- ✔ `Blv_Cfg.h:7`  [THRESHOLD] 9000mV: 임계값 9000U 정의
- ✔ `Blv_Diag.c:38-40` Blv_ConvertToMv() [INPUT_READ] BattVolt(mV): ADC raw → mV 변환
- 호출 경로: BlvDiag_MainFunction → Blv_ConvertToMv → Blv_Filter

### SWR-BLV-003 — 추적됨 (신뢰도 HIGH)

- ✔ `Blv_Diag.c:139-143` BlvDiag_MainFunction() [TIMER_COUNTER] 저전압 지속 카운트: 저전압이면 고장 카운터 증가
- ✔ `Blv_Diag.c:157` BlvDiag_MainFunction() [CONDITION] 2초(200회) 판정: 카운터 >= 200이면 s_bFaultDetected
- ✔ `Blv_Diag.c:162-166` BlvDiag_MainFunction() [STATE_TRANSITION] 고장 확정, DTC FAILED: NORMAL→FAULT, FAILED 보고

### SWR-BLV-004 — 추적됨 (신뢰도 HIGH)

- ✔ `Blv_Diag.c:181` BlvDiag_MainFunction() [OUTPUT_WRITE] 경고등: 경고등 = s_bFaultDetected (확정 상태 s_eBlvState가 아님)

### SWR-BLV-005 — 추적됨 (신뢰도 HIGH)

- ✔ `Blv_Diag.c:144-151` BlvDiag_MainFunction() [TIMER_COUNTER] 10500mV 이상 5초: 10500mV 이상이면 해제 카운터 증가(500 포화), 고장 카운터 0
- ✔ `Blv_Diag.c:170-174` BlvDiag_MainFunction() [STATE_TRANSITION] 고장 해제, DTC PASSED: FAULT→NORMAL, PASSED 보고

### SWR-BLV-006 — 추적됨 (신뢰도 MEDIUM)

- ✔ `Blv_Diag.c:152-155` BlvDiag_MainFunction() [CONDITION] 히스테리시스 구간 유지: 카운터 변경 없음

### SWR-BLV-007 — 추적됨 (신뢰도 HIGH)

- ✔ `Blv_Diag.c:105` BlvDiag_MainFunction() [TIMER_COUNTER] START에서 유예 타이머 시작: START 중 0으로 설정
- ✔ `Blv_Diag.c:116-120` BlvDiag_MainFunction() [TIMER_COUNTER] 3초 유예: s_u8StartMaskCnt < 300 동안 증가 후 반환
- ✔ `Blv_Diag.c:83` BlvDiag_Init() [INIT] 초기화 시 유예 없음 의도: 300을 uint8에 대입

### SWR-BLV-008 — 추적됨 (신뢰도 MEDIUM)

- ✔ `Blv_Diag.c:129-132` BlvDiag_MainFunction() [EXCEPTION_HANDLING] 무효값 판정 미수행: raw == 0xFFFF면 반환
- 대응 코드 없음: 무효값(ADC 오류 값)의 정의 — 0xFFFF 이외의 오류 판정 없음

### SWR-BLV-009 — 부분 추적 (신뢰도 HIGH)

- ✔ `Blv_Diag.c:80-88` BlvDiag_Init() [INIT] 카운터·상태·경고등 초기화: 상태 NORMAL, 고장 카운터 0, 경고등 0
- 호출 경로: EcuM_Init → BlvDiag_Init
- 대응 코드 없음: 정상 카운터(s_u16HealCnt) 초기화
- 탐색 메모: s_u16HealCnt에 대한 대입을 BlvDiag_Init에서 찾지 못함

### SWR-BLV-010 — 추적됨 (신뢰도 HIGH)

- ✔ `Blv_Diag.c:106` BlvDiag_MainFunction() [OUTPUT_WRITE] START 중 경고등: START면 경고등 0


## 6. 정적 분석 후보 및 판정

| ID | 규칙 | 확실성 | 심각도 | 위치 | 내용 | LLM 판정 |
|---|---|---|---|---|---|---|
| SC-001 | COUNTER_WRAP | POSSIBLE | HIGH | `Adc_Isr.c:12` Adc_ConvEndIsr() | `g_u8AdcSampleCnt`(uint8(unsigned 8bit, 0..255))가 포화(상한) 처리 없이 증가한다. 증가 조건이 256회 이상 연속 성립하면 0으로 순환하여, 이 값을 쓰는 판정 결과가 뒤집힐 수 있다. | 오탐 추정: ISR 약 1ms, 태스크 10ms로 주기당 약 10회 증가 후 L127에서 0으로 초기화되므로 256회 누적은 태스크가 2.5초 이상 멈춰야 발생 |
| SC-002 | STATE_NEVER_ENTERED | POSSIBLE | MEDIUM | `Blv_Diag.c:10`  | `s_eBlvState`(Blv_StateType)를 `BLV_STATE_SUSPEND`로 바꾸는 대입이 분석 범위에 없다. | 잠재: BLV_STATE_SUSPEND로의 전이가 없어 START 중단 상태 설계가 누락되었을 가능성 |
| SC-003 | MISSING_VOLATILE | POSSIBLE | LOW | `Blv_Diag.c:17`  | `g_u16BattRaw`는 ISR(ADC_ConvEnd_ISR)과 다른 컨텍스트가 공유하지만 volatile이 아니다. 컴파일러 최적화로 반복 읽기가 캐시될 수 있다. | 잠재: 반복 폴링은 없으나 ISR 공유 변수 volatile 누락 |
| SC-004 | MISSING_VOLATILE | POSSIBLE | LOW | `Blv_Diag.c:18`  | `g_u8AdcSampleCnt`는 ISR(ADC_ConvEnd_ISR)과 다른 컨텍스트가 공유하지만 volatile이 아니다. 컴파일러 최적화로 반복 읽기가 캐시될 수 있다. | 잠재: 위와 동일 |
| SC-005 | ARITH_OVERFLOW | POSSIBLE | MEDIUM | `Blv_Diag.c:38` Blv_ConvertToMv() | `u16Raw * 5000U`의 연산 자료형은 C 정수 승격 결과 unsigned 16bit(int=16bit 기준)이며 피연산자 자료형 범위상 결과가 0..327675000까지 가능해 0..65535를 넘는다 (부호 없는 연산: 모듈러 순환). | 가능성 높음: int 16bit 대상에서 raw ≥ 14이면 u16Raw*5000U가 65535를 넘음. 정상 전압(raw≈613)에서도 순환 |
| SC-006 | ARITH_OVERFLOW | POSSIBLE | MEDIUM | `Blv_Diag.c:40` Blv_ConvertToMv() | `u16Mv * 4U`의 연산 자료형은 C 정수 승격 결과 unsigned 16bit(int=16bit 기준)이며 피연산자 자료형 범위상 결과가 0..262140까지 가능해 0..65535를 넘는다 (부호 없는 연산: 모듈러 순환). | 잠재: 변환이 정상이면 u16Mv ≤ 5000이라 ×4 ≤ 20000으로 문제없음. SC-005 결과에 종속 |
| SC-007 | CONST_TRUNCATION | CERTAIN | HIGH | `Blv_Diag.c:83` BlvDiag_Init() | `s_u8StartMaskCnt`(uint8, 0..255)에 상수 300를 대입하면 44로 저장된다.<br>도구: gcc: Blv_Diag.c:83: conversion from ‘unsigned int’ to ‘uint8’ {aka ‘unsigned char’} changes value from ‘300’ to ‘44’ [-Woverflow] | 확정: 상수 300이 uint8에 44로 저장 (gcc -Woverflow 동일) |
| SC-008 | CONST_COMPARE_RANGE | CERTAIN | HIGH | `Blv_Diag.c:116` BlvDiag_MainFunction() | `s_u8StartMaskCnt < 300U`은(는) `s_u8StartMaskCnt`의 자료형 uint8(unsigned 8bit, 0..255)에서 항상 참이다.<br>도구: gcc: Blv_Diag.c:116: comparison is always true due to limited range of data type [-Wtype-limits] | 확정: uint8 < 300 항상 참 (gcc -Wtype-limits 동일) |
| SC-009 | INEFFECTIVE_GUARD | PROBABLE | HIGH | `Blv_Diag.c:118` BlvDiag_MainFunction() | `s_u8StartMaskCnt` 증가 가드 `s_u8StartMaskCnt < 300U`(L116)는 uint8(unsigned 8bit, 0..255)에서 항상 참이므로 포화 처리가 동작하지 않는다. 255 다음 증가 시 0으로 순환한다. | 확정: 가드가 무효하여 255→0 순환, 유예 무한 반복 |
| SC-010 | SHARED_RCW | PROBABLE | HIGH | `Blv_Diag.c:122` BlvDiag_MainFunction() | OsTask_10ms가 `g_u8AdcSampleCnt`를 L122에서 읽고 L127에서 쓰는 사이에 ADC_ConvEnd_ISR가 값을 바꾸면, 그 변경이 L127의 쓰기로 덮여 유실된다. | 가능성 높음: L122 읽기와 L127 쓰기 사이 ISR 발생 시 증가분 유실. 영향은 한 주기 판정 생략 수준 |
| SC-011 | TORN_ACCESS | PROBABLE | MEDIUM | `Blv_Diag.c:126` BlvDiag_MainFunction() | `g_u16BattRaw`(16bit)는 원자적 접근 폭(8bit)보다 커서 OsTask_10ms의 접근(L126) 도중 ADC_ConvEnd_ISR가 끼어들면 상·하위가 다른 시점의 값이 섞일 수 있다. | 정보 필요: 대상 MCU가 16bit 로드를 단일 명령으로 수행하는지 확인 필요 |
| SC-012 | COUNTER_WRAP | POSSIBLE | HIGH | `Blv_Diag.c:141` BlvDiag_MainFunction() | `s_u8FaultCnt`(uint8(unsigned 8bit, 0..255))가 포화(상한) 처리 없이 증가한다. 증가 조건이 256회 이상 연속 성립하면 0으로 순환하여, 이 값을 쓰는 판정 결과가 뒤집힐 수 있다. | 가능성 높음: 지속 저전압은 실차에서 가능한 조건이며 순환 시 경고등이 꺼짐 (C-004와 결합) |
| SC-013 | TOOL_WARNING | POSSIBLE | LOW | `Blv_Diag.c:159`  | enumeration value ‘BLV_STATE_SUSPEND’ not handled in switch<br>도구: gcc: Blv_Diag.c:159: enumeration value ‘BLV_STATE_SUSPEND’ not handled in switch [-Wswitch-enum] | 잠재: default가 있어 동작상 문제는 없으나 SC-002와 같은 원인 |

## 7. 테스트 케이스 일람

| TC | 관련 | 기법 | 사전 조건 | 조작 | 기대 결과 | 관찰 수단 |
|---|---|---|---|---|---|---|
| TC-C-004-1 | SWR-BLV-004 | STATE_TRANSITION | 고장 확정(경고등 점등) | BattVolt 11000mV 인가 후 0~6초 관찰 | 5초까지 점등, 5초 후 소등·PASSED | BlvWarnLamp, s_eBlvState, Dem |
| TC-C-007-1 | SWR-BLV-007 | TIMING | IgnSts=START | ON 전환 후 BattVolt 8500mV 6초 유지 | 3초 유예 후 2초 경과 시점(5초)에 FAILED | Dem 이벤트 상태, s_u8StartMaskCnt |
| TC-C-007-2 | SWR-BLV-007 | REQUIREMENT_BASED | Init 직후 IgnSts=ON (START 없음) | BattVolt 8500mV 3초 | 2초 시점 FAILED (유예 적용 여부는 사양 확인) | Dem 이벤트 상태 |
| TC-C-009-1 | SWR-BLV-010 | STATE_TRANSITION | 고장 확정, 경고등 점등 | IgnSts=START 2초 | 경고등 점등 유지 | BlvWarnLamp |
| TC-D-001-1 | SWR-BLV-003, SWR-BLV-004 | BOUNDARY_VALUE | IgnSts=ON, 유예 종료 | 8500mV 6초 | 2초 이후 계속 점등 | BlvWarnLamp 시계열, s_u8FaultCnt |
| TC-D-002-1 | SWR-BLV-002 | EQUIVALENCE_CLASS | 대상 HW/SIL(int 16bit) | raw=613 입력 | u16Volt ≈ 11980mV | u16Volt |
| TC-D-002-2 | SWR-BLV-002 | BOUNDARY_VALUE | 대상 HW/SIL | raw=1023 | u16Volt = 20000mV | u16Volt |
| TC-G-001-1 | SWR-BLV-003, SWR-BLV-004 | BOUNDARY_VALUE | IgnSts=ON | 8500mV 10초 유지 | 사양 미정의 — 확인 필요 (현재 코드: 경고등 주기적 소등) | BlvWarnLamp, s_u8FaultCnt |
| TC-C-002-1 | SWR-BLV-001 | STATE_TRANSITION | IgnSts=ON, BattVolt 8500mV 1.5초 유지 | IgnSts=START 0.5초 → ON 복귀, 8500mV 유지 | 유예 3초 후 누적 1.5초에서 이어서 0.5초 뒤 FAILED | s_u8FaultCnt, Dem 이벤트 상태 |
| TC-C-003-1 | SWR-BLV-002 | BOUNDARY_VALUE | IgnSts=ON, 유예 종료 | u16Volt가 8999/9000/9001mV가 되도록 입력(또는 변수 강제) 각 2.5초 | 8999·9000: FAILED, 9001: 미보고 | bLow, s_u8FaultCnt, Dem |
| TC-C-001-1 | SWR-BLV-001 | REQUIREMENT_BASED | IgnSts=ACC, 고장 이력 없음 | BattVolt 8500mV를 3초 인가 | DTC B1A00 미보고, BlvWarnLamp=0 | Dem 이벤트 상태, BlvWarnLamp |
| TC-C-005-1 | SWR-BLV-005, SWR-BLV-006 | TIMING | 고장 확정 | 11000mV 3초 → 10000mV 1초 → 11000mV 2초 | 해제되지 않음 (연속 5초 미충족) | s_u16HealCnt, s_eBlvState, Dem |
| TC-C-008-1 | SWR-BLV-009 | STATE_TRANSITION | 고장 확정 후 11000mV 4초 누적 | BlvDiag_Init 호출(Wake-up 모사) → 저전압 재확정 → 11000mV 1.5초 | 해제되지 않음(5초 필요) | s_u16HealCnt, s_eBlvState |
| TC-D-003-1 | SWR-BLV-003 | CONCURRENCY | SIL에서 ISR 주입 가능 | L126 실행 직후 ISR 강제 발생 | 샘플 유실 없음 | g_u8AdcSampleCnt 추적 |
| TC-G-004-1 | SWR-BLV-009, SWR-BLV-005 | REQUIREMENT_BASED | 이전 사이클 B1A00 확정 | 재시동, 12V 10초 | 사양 미정의 — 확인 필요 (현재 코드: PASSED 미보고) | Dem 상태 바이트 |
| TC-G-005-1 | SWR-BLV-003, SWR-BLV-008 | FAULT_INJECTION | IgnSts=ON, 저전압 1초 누적 | ADC ISR 중지 5초 | 사양 미정의 — 확인 필요 (현재 코드: 진단 정지) | s_u8FaultCnt, Dem |
| TC-D-004-1 | SWR-BLV-001 | STATE_TRANSITION | IgnSts=ON | START 진입 | 설계 기준 확인 | s_eBlvState |
| TC-C-006-1 | SWR-BLV-003, SWR-BLV-006 | TIMING | IgnSts=ON | 8500mV 1.5초 → 9500mV 0.1초 → 8500mV 0.6초 | 사양 미정의 — 확인 필요 (현재 코드: 2.1초 시점 FAILED) | s_u8FaultCnt, Dem |
| TC-G-002-1 | SWR-BLV-008 | FAULT_INJECTION | IgnSts=ON | raw=0 3초 | 사양 미정의 — 확인 필요 (현재 코드: FAILED) | Dem |
| TC-G-003-1 | SWR-BLV-001, SWR-BLV-004 | STATE_TRANSITION | 고장 확정·점등 | IgnSts=OFF | 사양 미정의 — 확인 필요 (현재 코드: 요청 1 유지) | BlvWarnLamp |
| TC-G-006-1 | SWR-BLV-002 | EQUIVALENCE_CLASS | Init 직후 | 첫 샘플 8500mV, 이후 12000mV | 사양 미정의 — 확인 필요 | u16Volt |

## 부록. LLM 호출 기록

| 호출 | 모델 | 입력 | 캐시 읽기 | 출력 | 시간(s) |
|---|---|---|---|---|---|
| stage1_requirements | file | - | - | - | - |
| stage2_traces_b1 | file | - | - | - | - |
| stage3_consistency_b1 | file | - | - | - | - |
| stage3_consistency_b2 | file | - | - | - | - |
| stage4_gaps_b1 | file | - | - | - | - |
| stage5_defects_b1 | file | - | - | - | - |
