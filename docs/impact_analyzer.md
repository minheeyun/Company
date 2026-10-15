# c_impact — AST / CFG 기반 Embedded C 변경 영향성 분석기

변경된 `.c`/`.h` 코드가 프로젝트의 **어떤 함수·변수·조건·상태 전이·출력**에 어떤 경로로 영향을 줄 수 있는지
AST, Call Graph, CFG, Def-Use(Data Flow)를 결합해 추적하고, SW 검증자가 **회귀 검증 범위**를 정하도록
근거(전파 경로)·잠재 결함·회귀 테스트 후보를 JSON / CSV / HTML / Graphviz 로 출력합니다.

- 분석 대상 소스는 **읽기 전용**으로 다룹니다. Git 리비전·패치 적용 결과는 임시 디렉터리에 만들고 끝나면 지웁니다.
- 정적 분석으로 확정할 수 없는 것은 확정하지 않습니다: 영향 수준 `POTENTIAL`, 결함 판정 `NEEDS_VERIFICATION`,
  기대 결과 `사양 확인 필요`로 구분해 표시합니다.

---

## 1. 전체 아키텍처

```
 입력                                  1단계                          2단계
┌──────────────────────────┐   ┌────────────────────────────┐   ┌─────────────────────────────┐
│ --base-dir / --curr-dir  │   │ project.load_project       │   │ change_detect               │
│ --repo + rev / work tree │──▶│  ├ compdb (compile_commands)│──▶│  ├ textdiff (git diff hunk)  │
│ --base-dir + --patch     │   │  ├ libclang 파싱 + 캐시     │   │  ├ 함수/전역/매크로/자료형 비교│
│ compile_commands / -I -D │   │  └ ast_extract → 파일 모델  │   │  ├ 문장 정렬(difflib) + 분류  │
└──────────────────────────┘   │    (함수·문장·식·심볼 ID)   │   │  └ 포맷만/비활성/미매핑 구분  │
            Baseline, Current ─┴────────────────────────────┘   └──────────────┬──────────────┘
                                                                               │ 변경점(Change)
 3단계                              4단계                          5단계        ▼
┌────────────────────────────┐ ┌──────────────────────────┐ ┌──────────────────────────────────┐
│ callgraph                  │ │ cfg                      │ │ impact (통합 의존 그래프 전파)      │
│  직접/간접(함수 포인터) 호출 │ │  CFG, 후지배, 제어 의존   │ │  시드: 변경 문장/정의 사용 위치      │
│  진입점, 호출 체인, 순환    │ │  경로 열거(한도), 실행가능성│ │  간선: DATA CONTROL GLOBAL PARAM   │
│ dataflow                   │ │  도달 조건/고정 분기 비교  │ │        RETURN OUT_PARAM ALIAS      │
│  도달 정의, def-use,       │ │ valuerange (구간 추론)    │ │        CALL_COND FP_CALL ...       │
│  상향 노출 사용, points-to │ └──────────────────────────┘ │  수준: DIRECT/INDIRECT/POTENTIAL/  │
└────────────────────────────┘                              │        NO_IMPACT_CONFIRMED + 경로  │
 roles: 상태/카운터/고장/출력 역할 태깅(구조 + 이름 패턴)        └───────────────┬──────────────────┘
                                                                              ▼
 6단계  defects(잠재 결함) · testgen(회귀 테스트) · pipeline(요약/표) · reports(JSON/CSV/DOT/SVG/HTML)
```

| 단계 | 모듈 | 핵심 기술 |
|---|---|---|
| 1. 코드 구조 | `clang_frontend`, `ast_extract`, `project`, `compdb` | libclang AST, USR 기반 심볼 해석, 토큰 정규화, 캐시 |
| 2. 변경점 | `textdiff`, `change_detect`, `roles` | Git diff hunk + AST 노드 매핑, 문장 시퀀스 정렬, 조건식 트리 비교 |
| 3. 함수·변수 | `callgraph`, `dataflow` | Call Graph(간접 호출 후보), 도달 정의/Def-Use, 흐름 비민감 points-to |
| 4. 경로 | `cfg`, `valuerange` | CFG, 후지배 트리, 제어 의존(Ferrante), 경로 열거, 구간 추론 |
| 5. 영향 전파 | `impact` | PDG 근사 위 우선순위 작업 목록 전파, 호출 문맥(1-CFA) 구분 |
| 6. 검증 결과 | `defects`, `testgen`, `pipeline`, `reports`, `report_html`, `graph_render` | 규칙 기반 결함 후보, 경계값·상태·카운터 테스트, Graphviz/SVG |

## 2. 디렉터리 구조

```
c_impact/
  __main__.py, cli.py      명령행 (analyze / ast / changes / callgraph / cfg)
  config.py                분석 설정(플랫폼, 태스크/ISR, 역할 패턴, 한도, 요구사항/기존 테스트 매핑)
  sources.py               입력 준비: 디렉터리 비교, Git 커밋/작업 트리, 패치 적용
  compdb.py                compile_commands.json 정규화(트리 간 재사용), 제한 모드 기본 인자
  clang_frontend.py        libclang 로딩, ctypes 보조(연산자 종류, 상수 평가, 비활성 구간), 토큰 인덱스
  ast_extract.py           [1단계] AST → 파일 모델(함수·문장·식·접근·호출·매크로·자료형)
  project.py               [1단계] 프로젝트 파싱/병합, 파싱 캐시, 심볼 연결
  textdiff.py              [2단계] unified hunk, 주석/공백 정규화
  roles.py                 상태/카운터/고장/출력 역할 태깅
  change_detect.py         [2단계] 변경점 탐지·분류
  callgraph.py             [3단계] Call Graph, 함수 포인터 후보, 진입점, 호출 체인, 순환
  dataflow.py              [3단계] 도달 정의, Def-Use, 상향 노출 사용, points-to
  cfg.py                   [4단계] CFG, 제어 의존, 경로, 실행 가능성, 전후 비교
  valuerange.py            [4단계] 정수 구간 추론
  impact.py                [5단계] 통합 영향 전파
  defects.py               [6단계] 잠재 결함
  testgen.py               [6단계] 회귀 테스트 후보
  pipeline.py              1~6단계 실행, 결과 dict(JSON 스키마) 구성
  reports.py, report_html.py, graph_render.py   JSON/CSV/DOT/SVG/HTML 출력
examples/impact_demo/
  baseline/, current/      검증용 Embedded C 예제(변경 전/후, 9개 .c + 4개 .h)
  compile_commands.json    예제 컴파일 DB (directory 는 DB 기준 상대경로 — 확장 허용)
  analysis_config.json     16bit MCU, 태스크/ISR, 요구사항/기존 테스트 매핑
  requirements.json, existing_tests.json
  expected_impact.json     시나리오별 기대 영향 범위(자동 테스트가 비교)
  sample_output/           예제 실행 결과(JSON/CSV/HTML/DOT/SVG)
tests/
  cimpact_util.py          테스트 도우미(임시 C 프로젝트)
  test_impact_ast.py       1단계
  test_impact_changes.py   2단계
  test_impact_flow.py      3·4단계
  test_impact_demo.py      5·6단계 시나리오 + 보고서/CLI/Git/패치/캐시
```

## 3. 설치

```bash
python3 --version              # 3.9 이상 (3.13 에서 검증)
pip install -r requirements-impact.txt   # libclang (LLVM 공식 python 바인딩 + 내장 libclang.so)
# 선택: Graphviz(dot) 가 있으면 impact_graph_graphviz.svg 도 생성 (없어도 자체 SVG 생성)
sudo apt-get install graphviz
```

- `stddef.h`, `stdbool.h` 같은 컴파일러 내장 헤더는 시스템 `clang -print-resource-dir` 를 자동 사용합니다.
  clang 이 없으면 `CLANG_RESOURCE_DIR` 로 지정하세요. 다른 libclang 을 쓰려면 `LIBCLANG_PATH`.
- 대상 MCU 의 자료형 크기를 clang 이 정확히 계산하도록 `--target` (예: `--target=arm-none-eabi`, `msp430`)
  또는 compile_commands 의 `--target`/`-m` 옵션을 사용하세요. 값 범위·원자성 판단은 `platform` 설정을 함께 씁니다.

## 4. 실행 방법

### 4.1 전체 분석

```bash
# (1) 변경 전/후 디렉터리 비교 (Git 이 아니어도 됨)
python3 -m c_impact analyze \
  --base-dir examples/impact_demo/baseline --curr-dir examples/impact_demo/current \
  --compdb examples/impact_demo/compile_commands.json --compdb-root examples/impact_demo/baseline \
  --config examples/impact_demo/analysis_config.json \
  -o impact_out

# (2) Git 커밋 간 비교 / (3) 작업 트리 변경
python3 -m c_impact analyze --repo path/to/ecu --base-rev v1.2.0 --head-rev HEAD -o out
python3 -m c_impact analyze --repo path/to/ecu --base-rev HEAD --working-tree -o out

# (4) Git Diff(패치)를 Baseline 에 적용해 Current 생성
git diff v1.2.0 -- src/ > change.diff
python3 -m c_impact analyze --base-dir path/to/ecu_v1.2.0 --patch change.diff -o out

# 빌드 설정별 분석: 기본 설정 + FEATURE_TRACE_EXT 정의 설정을 함께
python3 -m c_impact analyze ... --build-config release --variant "trace_ext:-DFEATURE_TRACE_EXT"
```

| 옵션 | 설명 |
|---|---|
| `--files a.c b.h` | 변경점 탐지를 해당 파일로 한정(영향 분석은 전체 프로젝트) |
| `--compdb`, `--compdb-root` | compile_commands.json, 그 DB 가 가리키는 소스 루트(Baseline/Current 에 같은 인자를 상대경로로 적용) |
| `-I`, `-D`, `--std`, `--target`, `--extra-arg` | 컴파일 DB 가 없을 때(제한 모드) 또는 추가 인자 |
| `--config` | 분석 설정 JSON (5장) |
| `--max-depth`, `--max-paths`, `--loop-unroll` | 함수 경계 전파 깊이, 함수당 경로 수, 반복 전개 횟수 |
| `--variant "이름:-DA -DB=1"` | 추가 빌드 설정. `<out>/variant_<이름>/` 에 별도 결과 |
| `--cache-dir`, `--no-cache` | 파싱 캐시(기본 `<out>/.cache`). 파일·인자·포함 헤더 해시가 같으면 재사용 |
| `--jobs N` | 병렬 파싱 |
| `--no-per-change` | 변경점별 개별 영향 분석 생략(변경점이 매우 많을 때) |
| `--format json,csv,dot,html` | 출력 형식 |

실행 예 (예제 프로젝트):

```
[release] 변경 10건 (포맷만 3건) | DIRECT 9 / INDIRECT 11 / POTENTIAL 1 / NO_IMPACT 5 함수 | 결함 후보 12(확정 2) | 테스트 27
  json           impact_out/impact_result.json
  impact_table_csv impact_out/impact_table.csv
  html           impact_out/impact_report.html
  ...
```

### 4.2 단계별 명령 (각 단계를 독립 실행)

```bash
python3 -m c_impact ast examples/impact_demo/current --function Fault_Monitor     # 1단계: 문장별 def/use
python3 -m c_impact ast examples/impact_demo/current -o ast.json                    # 1단계: 전체 모델 JSON
python3 -m c_impact changes --base-dir .../baseline --curr-dir .../current          # 2단계: 변경점 분류
python3 -m c_impact callgraph examples/impact_demo/current --dot cg.dot             # 3단계: Call Graph
python3 -m c_impact callgraph examples/impact_demo/current --from Util_ScaleTemp    #        진입점까지 호출 체인
python3 -m c_impact cfg examples/impact_demo/current Ctrl_Main10ms --dot cfg.dot    # 4단계: CFG/경로/도달 불가
```

## 5. 설정 파일

`analysis_config.json` (모든 항목 선택, `_` 로 시작하는 키는 주석):

```json
{
  "platform": {"name": "demo-16bit-mcu", "int_bits": 16, "pointer_bits": 16, "atomic_access_bits": 8},
  "tasks": [
    {"name": "Task_10ms", "kind": "task", "priority": 2, "period_ms": 10},
    {"name": "ADC_IRQHandler", "kind": "isr", "priority": 10}
  ],
  "requirements": "requirements.json",
  "existing_tests": "existing_tests.json",
  "build_config_name": "release",
  "max_depth": 8, "max_paths": 200, "loop_unroll": 1,
  "output_func_patterns": ["^(Dio|Pwm|Can)_(Write|Set|Transmit)"],
  "state_var_patterns": ["(?i)(state|mode)"],
  "external_globs": ["**/external/**"], "exclude_globs": ["**/build/**"]
}
```

- `requirements.json`: `{"REQ-ID": {"title": "...", "functions": ["Func"], "expected": "사양상 기대 동작"}}`
  — `expected` 가 있는 요구사항에 연결된 테스트만 예상 결과를 채웁니다.
- `existing_tests.json`: `{"TEST-ID": {"functions": ["Func"], "description": "...", "type": "unit"}}`
  — 재사용 가능한 기존 테스트로 표시합니다.
- 역할 패턴(`state/counter/fault/output/recovery_*_patterns`)은 **보조 근거**입니다. 상태 변수는
  `switch(state)` 안에서 자기 자신을 갱신하는 구조, 카운터는 `++/+=` 후 상수 비교 구조로도 판정합니다.

## 6. 분석 방법

### 6.1 심볼과 접근 키 (1단계)
- 함수 ID: 외부 연결은 `이름`, static 은 `상대경로::이름` → 같은 이름의 static 함수를 구분합니다.
- 전역: `G:이름` / `G:파일::이름`(static) / `G:함수::이름`(static 지역). 매개변수 `P:x`, 지역 `L:x#n`(동명 지역은 n 으로 구분).
- 필드 `G:g_ctx.mode`, 역참조 `*P:p`, `*L:pc#0.mode`, 필드 기반 별칭 후보 키 `F:Ctx_t.mode`.
- 모든 참조는 libclang 의 `referenced` 로 선언 커서를 따라가 해석합니다(문자열 검색 아님).
- 매크로 사용은 `MACRO_INSTANTIATION` → 정의 위치로 연결하고, 상수식은 `clang_Cursor_Evaluate` 로 값을 구하되 원문(매크로 이름)을 보존합니다.

### 6.2 변경점 (2단계)
- 파일 hunk 는 git diff 와 동일한 difflib 결과. 함수 단위로 토큰(주석·전처리 지시문·비활성 구간 제외) 해시가 같으면
  `FORMAT_ONLY`, 위치만 다르면 `FUNCTION_MOVED`(의미 변경 아님).
- 함수 내부는 문장 `(종류, 정규화 텍스트)` 시퀀스를 정렬해 쌍별로 분류하고, 연속 추가/삭제 문장은 하나의 변경점으로 묶습니다.
- 조건식은 식 트리로 비교합니다: 경계 연산자(`>`↔`>=`), 관계/동등/논리 연산자, 항 추가·삭제, 부정, 괄호 구조,
  임계값(같은 매크로 이름의 값 변화는 매크로 변경점으로만 보고), 반복 조건, else-if 순서.
- 상태 전이: `switch(state){case S: state = T;}` 에서 (S→T) 집합을 비교해 추가/삭제 전이를 보고합니다.
- 어떤 AST 요소로도 설명되지 않는 hunk 는 `UNMAPPED_CHANGE`, 현재 빌드에서 비활성인 `#if` 영역은 `INACTIVE_CODE_CHANGED`.

### 6.3 Call Graph / Data Flow (3단계)
- 간접 호출 후보: 함수 포인터 변수/배열/필드에 대입·초기화된 함수, **대입·복사·인자 전달을 따라간 값 흐름**(고정점),
  최후 수단으로 주소가 취해진 같은 시그니처 함수. 간접 호출은 모두 `potential` 간선입니다.
- 함수 내부: CFG 위 도달 정의 분석. 강한 정의(변수/필드 직접 대입)만 kill, 포인터·배열 원소 쓰기는 약한 정의.
  ENTRY 가 매개변수·전역의 '진입 시점 값'을 정의 → **상향 노출 사용**(다른 함수 또는 이전 호출이 쓴 값을 읽는 위치).
- points-to: `p = &x`, 배열, 포인터 복사, 호출 지점의 `&x` 인자(문맥 비민감). 대상이 하나로 확정되면 확정 별칭.

### 6.4 CFG (4단계)
- if/else, switch(fall-through·default·no-match), while/do/for(`for(;;)` 포함), break/continue/goto/label, return.
- 후지배 트리 + Ferrante 제어 의존. EXIT 에 도달하지 않는 무한 루프는 가상 간선으로 처리.
- 경로 열거는 `max_paths`, `loop_unroll` 한도. 경로 실행 가능성은 '변수 op 상수' 구간 추론으로 모순만 판정하며,
  반복문 조건에서는 본문에서 바뀌는 변수를 '알 수 없음'으로 확장(widening)합니다.
- 변경 전후 비교: 추가/삭제 경로, 주요 문장(호출·전역 쓰기)의 **도달 조건 변화**, 새 도달 불가 문장,
  자료형 범위상 항상 참/거짓이 된 분기(예: `U8 >= 300`).

### 6.5 영향 전파 (5단계)
| 간선 | 의미 | 확실성 |
|---|---|---|
| DATA | 함수 내부 def→use | 확정 |
| CONTROL | 분기 조건 → 제어 의존 문장 | 확정 |
| GLOBAL | 전역/static 쓰기 → 다른 함수(또는 다음 호출)의 상향 노출 읽기 | 확정 |
| PARAM / RETURN / OUT_PARAM | 인자 → 매개변수, return → 호출 지점, `*param` 쓰기 → 호출자 변수 | 확정(직접 호출) |
| ALIAS | 확정된 포인터 별칭을 통한 쓰기 | 대상 1개면 확정 |
| CALL_COND | 호출 여부/조건 변경 → 피호출 함수의 모든 외부 효과 | 확정 |
| FP_CALL, FIELD_ALIAS, UNKNOWN_DEREF | 함수 포인터, 필드 기반 별칭, 비활성/미매핑 변경 | **불확실 → POTENTIAL** |
| CALL(상위 호출) | 관측 가능한 효과가 바뀐 함수를 호출하는 상위 함수(호출 체인) | 경로의 확실성 따름 |

- **DIRECT**: 변경 문장, 변경된 정의(매크로·전역·자료형·시그니처)의 사용 위치, 같은 함수 안 1단계 의존 문장.
- **INDIRECT**: 확정 간선만으로 도달. **POTENTIAL**: 경로에 불확실 간선 포함, 파싱 오류 파일, 깊이 한도로 중단된 영역.
- **NO_IMPACT_CONFIRMED**: 위 어떤 간선으로도 도달하지 않은 함수(설정·가정 범위 내).
- 단순 호출 관계는 영향으로 보지 않습니다: 변경 함수가 호출하는 함수라도 인자·호출 조건·공유 데이터가 그대로면
  NO_IMPACT_CONFIRMED("단순 호출 관계"), 변경 함수의 효과가 외부로 관측되지 않으면(예: 아무도 읽지 않는 버퍼) 호출자도 영향 없음.
- 특정 호출 지점에서 들어온 영향(인자/호출 조건)은 그 호출자로만 돌아갑니다(1단계 호출 문맥).
- 모든 영향 항목에는 시드부터의 전파 경로(함수:라인 [간선] → …)와 근거 문장이 붙습니다.

### 6.6 잠재 결함 (6단계)
| 분류 | 규칙 | 판정 |
|---|---|---|
| 조건/논리 | 경계 연산자, AND/OR, 항 추가/삭제(가드 우회), 우선순위/부정, 분기 순서, 분기 삭제 | 추가 검증 필요 |
| 자료형/연산 | 자료형 범위 밖 상수 비교(항상 참/거짓), 상수 잘림, 축소 형변환·대입(구간 추론), 0 나누기 가드 없음, 배열 인덱스(상수/가드 상한) | 범위 밖 상수·상수 인덱스는 **정적 확정** |
| 상태/타이머 | 상태 전이 삭제·추가, 진입 불가 상태, 초기값/초기화 함수 변경, 카운터 순환·임계 도달 불가, 감소 언더플로, 타이머 임계·주기 변화 | 임계 도달 불가는 **정적 확정** |
| 동시성 | 영향 받은 전역이 서로 다른 우선순위/ISR 문맥에서 접근될 때 volatile 누락, 비원자 크기, read-modify-write | 추가 검증 필요 |
| 제어 흐름 | 새 도달 불가 문장/분기, 비활성 빌드 변형 코드, 미해석 변경, 경로 한도 | 도달 불가는 정적 확정 |

### 6.7 회귀 테스트 후보 (6단계)
변경 함수 직접 테스트, 경계값(변경 전/후 임계 ±1 값의 분기 결과표), 상태 전이, 타이머/카운터(최대값·순환·리셋),
출력/상태 문장의 도달 조건, 영향 함수(공유 데이터·인자·반환 경로), 오류·복구, 입력 조합(MC/DC), 동시성, POTENTIAL 리뷰.
각 항목에 목적·대상·구동 경로(진입점 체인)·입력·사전 조건·시나리오·예상 결과·필요 이유·관련 변경·영향 경로·관련
요구사항·재사용 가능 기존 테스트가 들어갑니다. 예상 결과는 요구사항에 기대 동작이 있을 때만 채우고, 코드에서 계산되는
변경 전/후 동작은 `code_behavior` 로 따로 제시합니다.

## 7. 출력

| 파일 | 내용 |
|---|---|
| `impact_result.json` | 전체 결과 (아래 키) |
| `impact_report.html` | SW 검증 리뷰 보고서(단일 파일): 요약, 1~9장, 영향성 표, 변경점별 그래프 |
| `impact_table.csv` | 변경 위치 × 영향 함수 표 (영향 경로·수준·근거·우선순위) |
| `changes.csv`, `impacted_functions.csv`, `defects.csv`, `regression_tests.csv` | 각 목록 |
| `impact_graph.dot` / `impact_graph.svg` | 영향 그래프(실선=확정, 주황 점선=불확실). `dot` 이 있으면 `impact_graph_graphviz.svg` 추가 |

`impact_result.json` 최상위 키: `summary`, `completeness`(제한 모드·파싱 실패 등 완전성 경고), `inputs`, `config`,
`projects`, `change_summary`, `changes`, `non_semantic`, `callgraph`, `call_chains`, `roles`, `cfg_comparison`,
`impact{per_change, functions, no_impact, variables, outputs, files}`, `impact_table`, `defects`, `tests`,
`requirements_impacted`, `uncertainty{assumptions, limitations, unresolved_indirect_calls, potential_indirect_edges,
parse_failures, unparsed_headers, inactive_regions, truncated, external_functions}`, `manual_review`, `graph`.

## 8. 검증용 예제와 기대 결과

`examples/impact_demo` 는 온도 제어 ECU 를 단순화한 프로젝트입니다(10ms/100ms 태스크, ADC ISR, 상태 머신,
과열 고장 디바운스, 경고등/히터/부저 출력, 함수 포인터 이벤트 테이블). 변경 전후 차이와 실제 분석 결과:

| ID | 변경 (baseline → current) | 기대/실제 영향 |
|---|---|---|
| S1 | `Ctrl_CheckOverheat`: `temp > TEMP_HIGH_LIMIT` → `>=` | DIRECT Ctrl_CheckOverheat → INDIRECT Heater_Off/Heater_On (CALL_COND) → 출력 Dio_WriteChannel. 경계값 100 에서 결과 반전. 상위 Ctrl_Main10ms, Task_10ms 는 호출 체인 |
| S2 | `Sensor_Task`: IIR 필터 식 변경(g_tempFiltered) | 직접 호출 관계 없는 Output_UpdateGauge, Output_Update, Ctrl_Main10ms 가 GLOBAL 로 INDIRECT. Fault_Monitor 는 Task_10ms 의 인자로 INDIRECT |
| S3 | `LAMP_WARN_TEMP` 90 → 85 (매크로) | Output_Update DIRECT(MACRO) → Dio_WriteChannel. 85·86·89 에서 경고등 결과 변경 |
| S4 | `s_overTempCnt` U16 → U8 (임계 300) | Fault_Monitor/Fault_Init DIRECT, g_faultActive 경유 Output_Update·Ctrl_Main10ms INDIRECT. **정적 확정 결함**: 카운터 임계 도달 불가 + 255 에서 순환, `>= 300` 항상 거짓 → 고장 확정 경로 도달 불가 |
| S5 | `Util_ScaleTemp` 보정 계수 150 → 160 (여러 호출자) | 호출자 Sensor_ReadTemp, Diag_Task100ms 가 RETURN 으로, Sensor_Task 가 OUT_PARAM(`*outTemp`)으로 INDIRECT. 진입점 Task_10ms·Task_100ms 두 체인 |
| S6 | `Diag_Trace`: 쓰기 전용 트레이스 값 변경 | Diag_Trace 만 DIRECT. 효과가 관측되지 않아 호출자(Ctrl_Main10ms) 영향 없음 |
| S7 | `Evt_OnSensorFail`: 부저 인자 ON → PULSE (핸들러 테이블 호출) | Output_SetBuzzer INDIRECT(PARAM). 함수 포인터로 호출하는 Event_Dispatch 와 그 상위는 **POTENTIAL**. 다른 핸들러 Evt_OnOverheat/Evt_OnRecover 는 영향 없음 |
| S8 | CTRL_FAULT 복귀 상태 NORMAL → DEGRADED | 상태 전이 삭제 (CTRL_FAULT→CTRL_NORMAL), 추가 (CTRL_FAULT→CTRL_DEGRADED) |
| S0 | `Heater_Off` 주석/공백만 변경 | 의미 변경 아님(FORMAT_ONLY) |
| S9 | `#ifdef FEATURE_TRACE_EXT` 내부 상수 변경 | 기본 빌드: INACTIVE_CODE_CHANGED (POTENTIAL). `--variant trace_ext:-DFEATURE_TRACE_EXT` 에서는 실제 상수 변경으로 분석 |

동시성(설정된 태스크/ISR 기준): `g_adcRaw`(ISR↔태스크), `g_tempFiltered`(10ms↔100ms) 가 16bit 로
8bit 원자 접근 MCU 에서 비원자적 → 경쟁 조건 검증 항목. 영향 없음 확인: ADC_IRQHandler, Sensor_Init, Ctrl_Init 등.

기대 범위는 `examples/impact_demo/expected_impact.json` 에 정의되어 있고 `tests/test_impact_demo.py` 가 자동 비교합니다.
실행 결과 사본은 `examples/impact_demo/sample_output/` 에 있습니다.

## 9. 테스트

```bash
python3 -m unittest discover -s tests -p "test_impact*.py" -v
```

## 10. 구현 범위와 한계

### 구현된 기능
- 입력: 디렉터리 비교, Git 커밋 간/작업 트리, 패치 적용, compile_commands(트리 간 재사용), 제한 모드(-I/-D), 빌드 변형(`--variant`), 대상 파일 한정
- 1단계: libclang AST, static/동명 심볼 구분, 지역 shadowing, static 지역, 구조체 필드, 포인터 역참조, 매크로 정의·사용(중첩 매크로 포함), typedef/구조체/열거형, 함수 포인터 바인딩, 비활성 구간, 파싱 오류 보고, 캐시
- 2단계: 6.2절의 변경 유형 전부(함수·시그니처·전역·지역·조건·연산자·임계값·대입·호출·상태·타이머/카운터·매크로·자료형), 포맷 변경 구분, 위치 이동 처리
- 3~5단계: 6.3~6.5절
- 6단계: 6.6~6.7절, JSON/CSV/HTML/DOT/SVG

### 한계 (보고서 7장에도 표시)
- 하나의 실행은 하나의 빌드 설정(전처리 결과)을 분석합니다. 비활성 영역은 의미 분석하지 않고 변경 여부만 표시합니다.
- points-to 는 흐름·문맥 비민감 근사입니다. 포인터 연산, 정수↔포인터 캐스팅, 외부 함수가 반환한 포인터, memcpy 류는 추적하지 않습니다.
- 정의가 없는 외부 함수(드라이버/라이브러리)는 '외부 출력'으로만 표시하며 내부 부작용은 모델링하지 않습니다.
- 값 범위는 '변수 op 상수' 구간만 다루며 실행 가능성을 확정하지 않습니다(SMT 미사용).
- 역할(상태/카운터/출력) 태깅은 구조 + 이름 패턴 휴리스틱입니다. 프로젝트 명명 규칙에 맞게 패턴을 조정하세요.
- 동시성 분석은 설정된 태스크/ISR 과 호출 관계에 기반한 정적 추정이며, 인터럽트 금지 구간·뮤텍스는 인식하지 않습니다.
- C++ 및 인라인 어셈블리 의미 분석은 지원하지 않습니다(asm 문은 `unknown` 문장으로 표시).

### 향후 확장 후보
- 여러 빌드 설정 결과의 통합 비교 보고서, 비활성 영역 자동 변형 탐색
- 인터럽트 금지/임계 구역 API 인식, 태스크 선점 모델 기반 경쟁 분석
- 문맥 민감(k-CFA) / 필드 민감 points-to, 외부 함수 요약(stub) 정의 파일
- SMT(z3 등) 기반 경로 조건 판정, 루프 불변식
- 기능사양서(문서)에서 요구사항·기대 동작 자동 추출, 기존 테스트 커버리지(gcov/LDRA 등) 결과 연동
- 대규모 프로젝트용 증분 분석(변경 파일의 의존 영역만 재분석), 분산 파싱
