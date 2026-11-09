# VFIA — 차량 기능 분류 · 변경 영향 분석기

같은 **코드 분석 그래프(SDG/CPG)** 를 공유하는 두 스크립트입니다.

| 스크립트 | 하는 일 |
|---|---|
| `classify_features.py` | 파일·함수·변수의 관계를 추출하고 차량 기능과 연결(역할·근거·검토 상태 포함) |
| `analyze_impact.py` | 변경 전후 코드를 비교하고, 변경점에서 어떤 데이터·실행 경로를 거쳐 어떤 기능까지 영향이 전달되는지, 어떤 조건에서 출력이 어떻게 달라지는지 분석 |

두 스크립트 모두 `vfia.pipeline.get_graph()`로 같은 그래프(`<output_dir>/graphs/<version>.db`)를 만들거나 재사용합니다.

```
변경 전 소스+빌드 설정 ─▶ 변경 전 그래프 ─┐
                                          ├─▶ AST 대응·변경점 ─▶ 양쪽 그래프에서 각각 영향 추적 ─┐
변경 후 소스+빌드 설정 ─▶ 변경 후 그래프 ─┘                                                      ├─▶ 기능별 영향 후보·근거 ─▶ 동작 차이 검증·보고서
요구사항·기능·신호·태스크(차량 모델, ARXML) ─▶ 코드와 차량 기능 연결 ───────────────────────────┘
```

## 설치

```bash
pip install -r requirements.txt
```

* libclang: `pip install libclang`(휠) 또는 시스템 libclang(`VFIA_LIBCLANG=/path/libclang.so`).
  휠에 `stdbool.h` 같은 컴파일러 내장 헤더가 없으면 시스템 `clang -print-resource-dir`을 자동으로 찾아
  `-isystem`으로 추가합니다(`VFIA_CLANG_RESOURCE_DIR`로 지정 가능).
* 전후 비교 실행(선택): 호스트 `gcc`/`cc`.

## 빠른 시작(예제 ECU)

```bash
python classify_features.py --config examples/torque_ecu/analysis.yaml --version after
```

```bash
python analyze_impact.py --config examples/torque_ecu/analysis.yaml
```

결과: `examples/torque_ecu/out/` 아래 `features_after.{json,html}`, `impact.{json,html}`.

```bash
python -m pytest -q tests
```

## 입력

| 입력 | 설정 위치 | 용도 |
|---|---|---|
| `.c/.h`, 생성 코드 | `versions.<v>.root` | 분석 대상 소스(버전별) |
| `compile_commands.json` | `versions.<v>.compile_commands` | 번역 단위별 include·매크로·옵션 재현. 없으면 `sources`/`args`로 **합성 compile DB**(보고서에 표시) |
| 빌드 설정 이름·추가 인자 | `build.name`, `build.extra_args`, `build.variant_filter` | 차종별 `#ifdef` 분석 설정. 같은 파일에 설정이 여러 개면 `variant_filter`로 선택 |
| 차량 모델(YAML/JSON, 여러 파일 병합) | `vehicle_model` 또는 `versions.<v>.vehicle_model` | 기능·요구사항·관찰 대상·태스크/ISR/콜백·신호·캘리브레이션·ARXML |
| 검토 기록 | `review_file` | 검토자가 확정/기각한 기능 연결(재실행해도 유지) |
| 실행 추적 | `traces: {시나리오: 파일}` | 기능 시나리오에서 실행된 함수 목록(JSON 또는 `# feature: ID` + 함수명 줄) |

.h는 독립 파싱하지 않고, 실제 .c에 포함되는 문맥에서만 분석합니다. 헤더 정의는 원본 위치와 포함한 번역
단위 목록을 따로 관리하고, 매크로 문맥에 따라 다르게 전개되면 진단(`header_variant_conflict`)으로 남깁니다.
이 빌드 설정에서 비활성인 `#if` 구간(libclang skipped ranges)과 조건 매크로를 결과에 기록합니다.

### 차량 모델 예(`examples/torque_ecu/vehicle_model.yaml`)

```yaml
features:
  - id: F_TQ_LIMIT
    name: 속도 조건 기반 토크 제한
    requirements: [REQ_TQ_001]
    keywords: [torque, limit, tq]
    seeds: {functions: [CalcTorqueLimitPct]}       # 설계 정보로 알고 있는 기준점
    observations:                                  # 기능을 판단하는 출력·상태
      - id: OBS_TQ_CMD
        kind: function_return                      # function_return | global | signal | statement | function
        function: ComputeTorqueCommand
        params: {request_pct: [0, 100], speed_kph: [0, 300], sensor_ok: [0, 1]}   # 동작 검증 입력 정의역
      - {id: OBS_TQ_SIGNAL, kind: signal, signal: TqCmd_Value}
  - id: F_SENSOR_DIAG
    observations:
      - {id: OBS_SENSOR_FAULT, kind: global, variable: g_sensor_fault,
         function: Diag_UpdateSensor, sequence: 5, params: {raw_fault: [0, 1]}}   # 상태 누적: 입력 시퀀스
execution:                                         # 직접 호출로 드러나지 않는 실행 관계
  tasks: [{name: Task_10ms, entry: Task_10ms, period_ms: 10, priority: 5}]
  isrs:  [{name: ISR_CanRx, entry: Can_RxIsr, priority: 20}]
signals:
  - {name: VehicleSpeed, readers: [{param: Can_RxIsr.raw}]}   # api / variable / param / return 지점
arxml: [swc_torque.arxml]                          # Runnable SYMBOL, TimingEvent 주기, 포트 데이터 접근
```

`Rte_Write_<P>_<E>` / `Rte_Read_<P>_<E>` / `Com_SendSignal(ID, …)` 호출은 신호 노드로 자동 연결합니다.
ARXML이나 신호 이름만으로 기능을 확정하지 않고, 기능 연결은 이 모델(요구사항·설계 정보)에 근거합니다.

## 모듈과 책임

| 모듈 | 책임 |
|---|---|
| `project_indexer.py` | compile DB 정리, libclang 파싱, 스코프 기반 심벌 키, 일반화 AST, 매크로 정의·전개 위치, 비활성 `#if` 구간, 번역 단위 캐시(포함 헤더 해시로 무효화) |
| `cfg.py` | CFG(분기·반복·switch·break/continue/goto·조기 return, `&&`/`||` 단락 분해), 후지배자, 제어 의존성 |
| `dataflow.py` | worklist 고정점 Reaching Definitions(비트셋), Def-Use |
| `pointsto.py` | Andersen points-to(필드 구분, 배열 통합), 함수 포인터·콜백 테이블 간접 호출 해석 |
| `graph_builder.py` | SDG 구축: DATA/CONTROL, call-site·actual/formal in/out, PARAM_IN/OUT, CALL, HRB SUMMARY, 전역 상태 위치 노드, 포인터 매개변수 부작용의 호출 지점별(문맥 민감) 해석, MOD 요약(SCC 고정점) |
| `vehicle_context.py` | 기능·요구사항·관찰 대상, 태스크/ISR/Runnable(ARXML)과 RUNS_IN, 신호 쓰기·읽기 지점, 캘리브레이션 |
| `feature_mapper.py` | 키워드(약어 확장)·TF-IDF·Louvain·개인화 PageRank(시드 전파)·관찰 대상 후방 슬라이스·실행 추적 → 다중 라벨 역할(직접/지원/공용/후보) |
| `ast_diff.py` | GumTree 계열 매칭(top-down 동형, bottom-up dice+내용 유사도, LCS 복구)과 update/insert/delete/move |
| `change_detector.py` | 심벌·파일 이동·이름 변경 대응, 변경 유형 분류, 매크로 전개 지점 귀속, 그래프 앵커와 노드 대응표 |
| `slicing.py` | 2단계(HRB) 전방/후방 슬라이싱, 근거 경로 재구성 |
| `impact_analyzer.py` | 변경점 전방 ∩ 관찰 대상 후방(양 버전 각각), 삭제로 바뀐 사용 지점 이월, 실행 문맥·공유 상태, 타이밍 영향 |
| `symexec.py` | z3 기반 유계 기호 실행(C 정수 비트폭·부호·승격·복합 대입, 보호 조건 인코딩, 반복 전개, 인라인, 외부 함수=공유 미해석 함수) |
| `behavior_checker.py` | 조건 XOR(분기 차이), 출력 차이·증거 입력·입력 구간, 변경 분리(혼합 프로그램), 상태 시퀀스, gcc 전후 비교 실행 |
| `report_generator.py` | JSON + HTML(무엇이 변경 → 무엇을 확인 → 어떤 영향 → 추가 확인 항목) |

## 설계 항목과 구현 대응

| 분석 목적 | 구현 |
|---|---|
| 변경된 문법 요소 식별 | `ast_diff.match/edit_script` (줄 번호가 아닌 구조 대응) |
| 값의 전달 관계 | `dataflow.ReachingDefs` → `EK.DATA` |
| 반복문 데이터흐름 | worklist 고정점 |
| 조건의 영향 범위 | `cfg.postdominators` + `control_dependence` → `EK.CONTROL` |
| 포인터를 통한 영향 | `pointsto.PointsTo` + 매개변수 부작용(`pmod/pref`) |
| 함수 간 영향 전파 | SDG + HRB 요약 간선 + MOD 요약 |
| 변경점 → 영향 범위 | `slicing.forward_slice` |
| 기능 관련 영향만 | `slicing.backward_slice`(관찰 대상)과의 교집합 |
| 발생 가능한 입력 조건 | `symexec` + z3 (XOR, Optimize로 구간) |
| 실제 발생 확인 | `behavior_checker.exec_compare` (같은 입력, 프로세스별 초기 상태) |

변경 유형별 추적 시작점: 조건/임계값/연산자 → 조건 노드(제어 의존), 대입·계산식 → 정의 문장(데이터),
호출 추가·삭제 → 호출 정점(피호출 함수 전체 부작용), 인자·반환형 → 호출부 actual-in/out,
return/break/continue/goto → 흐름 노드와 그 뒤 문장, 순서 변경 → 이동한 문장, 구조체 필드·자료형 →
필드 사용처(+메모리 배치 검토 항목), 매크로·헤더 → 모든 번역 단위의 전개 지점, 태스크 설정 → 실행 문맥
안 함수와 공유 상태(타이밍 검토). 사용처 없는 지역변수라도 초기화식의 호출·volatile 접근은 부작용으로 추적합니다.

## 결과 상태

| 상태 | 의미 |
|---|---|
| 정적 영향 후보 | 의존 그래프에서 변경점 → 관찰 대상 경로가 있음 |
| 모델에서 동작 차이 확인 | 기호 실행 모델에서 관찰값이 달라지는 입력이 존재(증거 입력 제시) |
| 모델에서 동작 차이 미발견(검증 범위 내) | 모델·정의역·전개 범위 안에서 관찰값 동일 |
| 실행으로 차이 확인 | 두 버전 실행에서 같은 입력으로 출력이 다름 |
| 실행 비교에서 차이 미발견(시험 입력 범위 내) | 생성한 시험 입력에서는 같음 |
| 분석 불완전으로 판정 유보 | 경로를 숨길 수 있는 미해결 영역(간접 호출, 대상 불명 포인터 쓰기, 어셈블리, 지원하지 않는 구문 등) |
| 현재 분석 범위에서 영향 경로 미발견 | '영향 없음'이 아님 |

## 예제가 포함한 검증 사례(`examples/torque_ecu`)

| 사례 | 예제 변경 | 기대 결과 |
|---|---|---|
| 임계값 변경 | `speed_kph >= TQ_SPEED_LIMIT_KPH(30)` → `>= 40U` | 분기 차이 `sensor_ok=1, 30≤speed≤39`, 출력 차이 `request_pct≥81`, request 24는 출력 동일 |
| `>` → `>=` + 상태 누적 | 디바운스 `s_fault_cnt > 3` → `>= 3` | 5단계 시퀀스에서 고장 확정 시점 차이(실행으로 확인) |
| 조기 반환 | `Spd_Process`에 `if (!g_rx_valid) return;` | 이후 문장 제어 의존 변화, ISR 공유 상태 경로 |
| 대입문 삭제 | `g_tq_state.last_limit = limit;` 삭제 | 변경 전 그래프에서 진단 콜백 테이블까지 경로 |
| 전역·포인터 전달 | `Spd_GetFiltered(&speed)`, 공용 `Flt_LowPass(&state)` | 호출 지점별 상태 구분(문맥 민감) |
| 콜백·태스크 공유 상태 | `Diag_Events[i].cb()`, `Can_RxIsr` → `Task_10ms` | 간접 호출 대상 복원, 문맥 간 공유 상태 검토 항목 |
| 중간 값만 다르고 출력 동일 | `(a<b)?a:b` → `Tq_Min(a,b)` | 변경 분리 검증에서 차이 없음 |
| 헤더·매크로·자료형 | `SPD_MAX_KPH` 300→280, `TorqueState_t` 필드 추가 | 여러 번역 단위 전개 지점, 메모리 배치 검토 |
| 사용 안 하는 지역변수 | `uint16_t dbg = Dbg_ReadCounter();` | 초기화 부작용(전역 카운터) 추적 |
| 태스크 설정 | `Task_10ms` 10ms → 20ms | 타이밍 영향·공유 상태 검토 |

## 분석 그래프 모델

노드 ID: `fn:<name>`(static은 `fn:<file>:<name>`), `gv:…`, `lv:<func>:<name>`, `pv:<func>:<param>`,
문장 `fn:<…>#<ast id>`, 호출 정점 `…#<id>c`, actual-in `…c:in<i>`, actual-out `…c:out`,
`#entry/#exit/#fi<i>/#fo_ret/#fo_mem`, 메모리 위치 `loc:<key>|<field>`, `sig:`, `task:`, `feat:`, `obs:`, `req:`.
간선 종류는 `vfia/model.py`의 `EK`. 그래프는 SQLite로 저장되며 `--graphml`로 시각화 도구용 내보내기를 지원합니다.

## 한계와 확장 지점

* **정밀 프런트엔드**: libclang Python 바인딩은 프로토타입용입니다. `project_indexer.TUExtractor`가 만드는
  일반화 AST(JSON: `k, v, ref, rk, ty, l/c/el/ec, mac, ch`)와 같은 형식을 Clang LibTooling 추출기가 내보내면
  나머지 단계는 그대로 사용할 수 있습니다. CFG는 현재 Python에서 AST로부터 생성합니다(`clang::CFG`로 대체 가능).
* **Joern**: 초기 검증용 CPG가 필요하면 Joern 결과를 `AnalysisGraph`로 적재하는 어댑터를 추가할 수 있습니다
  (전처리 설정 재현 정확도는 먼저 확인 필요).
* **포인터 정밀도**: Andersen(문맥 비민감) + 매개변수 부작용의 호출 지점별 해석입니다. 대규모/복잡한 포인터는
  LLVM IR 기반 SVF 연동을 권장합니다.
* **기호 실행 범위**: 정수·_Bool·enum·구조체 필드·정수 배열·함수 포인터를 지원합니다. 부동소수점, goto,
  정수→포인터 변환, 동적 메모리는 '판정 유보'로 보고합니다. 반복 전개·인라인 깊이·시퀀스 길이를
  결과에 기록하며, 전개 범위가 부족하면 전체 정확성을 증명한 것이 아닙니다(CBMC 연동으로 대체 가능).
* **타이밍·동시성**: 실행 문맥과 공유 상태를 식별해 검토 항목으로 제시합니다. 실행 시간·선점 분석은
  태스크 우선순위·실행 시간 모델을 추가해 별도로 수행해야 합니다.
* **여러 빌드 설정**: 한 실행은 한 빌드 설정을 분석합니다. 차종별로 `build.extra_args`/`variant_filter`를
  바꿔 각각 실행하고, 보고서의 '비활성 구간'을 확인하세요.
* **증분 분석**: 번역 단위 파싱은 포함 헤더 해시로 캐시되어 바뀐 번역 단위(헤더 변경 시 포함한 모든
  번역 단위)만 다시 파싱합니다. points-to·함수 요약은 정확성을 위해 매번 전체 재계산합니다.
* **LLM 활용**: `classify_features.py --llm-prompts`는 기능 미연결 군집의 기능명 후보 생성용 프롬프트만
  내보냅니다. 영향 경로의 존재 여부는 항상 분석 그래프로 판단합니다.
