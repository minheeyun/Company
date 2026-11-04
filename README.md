# GitLab Changes → 파일별 HTML 추출기 / C 변경 영향 분석기

GitLab의 Merge Request **Changes** 탭(API의 `diffs`)에 보이는 변경 내용을 **파일마다 하나의 `.html`** 로 추출합니다.
Python 3.9+ 표준 라이브러리만 사용하며, 별도 설치가 필요 없습니다.

## 사용법

```bash
export GITLAB_TOKEN=glpat-xxxx   # read_api 권한의 Personal/Project Access Token

# MR URL로 바로
python3 gitlab_diff_export.py --mr-url https://gitlab.example.com/group/proj/-/merge_requests/12 -o out

# 프로젝트 + MR IID
python3 gitlab_diff_export.py --gitlab-url https://gitlab.example.com --project group/proj --mr 12 -o out

# 단일 커밋 / 두 ref 비교
python3 gitlab_diff_export.py --project group/proj --commit 1a2b3c4d -o out
python3 gitlab_diff_export.py --project group/proj --compare main..feature -o out

# 좌우 비교 보기
python3 gitlab_diff_export.py --mr-url ... --view side-by-side

# 오프라인: API 응답을 저장해 둔 JSON으로
python3 gitlab_diff_export.py --input examples/sample_diffs.json -o out
```

## 출력 구조

```
out/
├── index.html                 # 변경 파일 목록 (상태, +/− 줄 수, 각 파일 링크)
└── files/
    ├── src/app.py.html        # 저장소 경로 구조를 그대로 유지
    └── docs/README.md.html
```

각 파일 HTML은 CSS가 내장되어 있어 단독으로 열거나 전달해도 그대로 보입니다.
줄 번호(이전/이후), 추가/삭제 색상, 줄 안의 변경 부분 강조, hunk 헤더, 다크 모드를 지원합니다.
추가/삭제/이름 변경/바이너리/너무 큰 diff도 표시합니다.

## 사용하는 GitLab API

| 대상 | 엔드포인트 |
|---|---|
| MR | `GET /projects/:id/merge_requests/:iid/diffs` (페이지네이션, GitLab 15.7+) — 404이면 구버전용 `/changes?access_raw_diffs=true`로 대체 |
| 커밋 | `GET /projects/:id/repository/commits/:sha/diff` |
| 비교 | `GET /projects/:id/repository/compare?from=&to=` |

참고: GitLab은 아주 큰 diff를 `too_large`/`collapsed`로 비워서 내려줄 수 있으며, 이 경우 해당 파일 페이지에 안내 문구가 표시됩니다.
GitLab이 하위 경로(예: `https://host/gitlab/...`)에 설치된 경우 `--mr-url` 대신 `--gitlab-url https://host/gitlab --project ... --mr ...`를 사용하세요.

## 검증 리뷰 (diff_review.py)

추출한 HTML을 읽어 Claude API로 **AUTOSAR Classic SW 검증 담당자 관점의 리뷰**를 만듭니다.
검토 기준은 [`prompts/autosar_verifier.md`](prompts/autosar_verifier.md)에 그대로 들어 있으며, 이 파일을 고치면 리뷰 기준이 바뀝니다 (`--prompt`로 다른 파일 지정 가능).

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...

# 1) diff 추출
python3 gitlab_diff_export.py --mr-url https://gitlab.example.com/ecu/bsw/-/merge_requests/42 -o mr42

# 2) 리뷰 (입력 자료는 텍스트 또는 파일 경로, 여러 번 지정 가능, 사양은 PDF도 가능)
python3 diff_review.py mr42 \
  -t "ECU-1234" \
  -d "S3 타이머 만료 판정이 한 주기 늦는 문제 수정" \
  --spec docs/Dcm_SWS.pdf \
  --reference "Dem_CheckTimeout() 의 >= 비교 패턴 참고" \
  --test-result logs/ECU-1234_retest.txt \
  --include "*.c" --include "*.h"
```

| 옵션 | 프롬프트 [입력] 항목 |
|---|---|
| `-d/--description` | 개선 설명(개발자 작성) |
| `-t/--ticket` | 결함 티켓 번호 |
| `--spec` | 관련 사양 |
| `--reference` | 참고한 기존 패턴 |
| `--test-result` | 테스트 결과 |

넘기지 않은 항목은 "제공되지 않음"으로 표시되어, 모델이 추측하지 않고 "확인 필요"로 적도록 합니다.
코드 diff는 파일명, hunk 헤더(함수명), 이전/이후 라인 번호를 포함해 전달됩니다.

결과물 (`mr42/review/`, `-o`로 변경 가능):

| 파일 | 내용 |
|---|---|
| `review_record.md` | 1. 변경 배경 + 2. 리뷰 결과 (리뷰 기록에 붙여넣기용) |
| `verifier_memo.md` | 3. 검증자 메모 (테스트 케이스 표, 회귀 대상, A~I 해당 항목, 제안) |
| `review.md` | 응답 전체 |
| `review.html` | 위 내용을 렌더링한 페이지 (메모는 점선 박스로 구분) |

기타 옵션:
- `--dry-run`: API를 호출하지 않고 보낼 프롬프트(`prompt_system.md`, `prompt_user.md`)만 저장합니다. 사내 승인된 다른 LLM 창구에 붙여넣거나 내용을 미리 점검할 때 씁니다.
- `--model` (기본 `claude-opus-5-5`), `--effort` (기본 `high`)
- `--no-fallback`: 기본으로 켜져 있는 서버 측 거절 대체(fallbacks)를 끕니다. Bedrock/Vertex 등 이를 지원하지 않는 경로에서 사용하세요.

> ⚠ 리뷰 시 소스 코드 diff와 첨부 자료가 Anthropic API로 전송됩니다. 사내 보안 정책상 외부 전송이 허용되는지 먼저 확인하세요.

## 변경 영향 분석 (change_impact.py)

사양서 없이 **변경된 C 코드가 어떤 함수·변수에, 어떤 경로와 조건으로 영향을 줄 수 있는지** 분석합니다.
변경 전후 코드 비교 → 변수·조건·호출 의존성 추적(전방 프로그램 슬라이싱) → 영향 경로·조건 보고서 → SIL 차등 실행으로 실제 차이 확인까지 CLI 하나로 수행합니다.

| 원하는 기능 | 명령 | 사용하는 분석 |
|---|---|---|
| 이 프로젝트에서 어떤 함수가 어떤 변수를 읽고 쓰는가? | `deps` | 함수 간 의존성 분석 (포인터·구조체 필드·함수 포인터·호출 전이 포함) |
| 이 조건식을 바꾸면 어느 함수·변수까지 확인해야 하는가? | `slice` (바꾸기 전, what-if), `analyze` (바꾼 뒤, 변경 전후 비교) | 전방 프로그램 슬라이싱 |
| 어떤 입력과 상태에서 실제로 결과가 달라지는가? | `explore` (자동 탐색), `sil` (지정한 시나리오) | 경로·값 분석(판정 차이 구간·경계값) + 변경 전후 SIL 비교 |

```bash
pip install -r requirements.txt      # pycparser 필요 (전처리는 gcc 또는 clang)

# 1) 두 폴더 비교 (변경 전/후 소스 루트)
python3 change_impact.py analyze --old v1/src --new v2/src -I include -D VARIANT_A=1 -o impact_out

# 2) git 리비전 비교 (--new-rev 생략 시 작업 트리), compile_commands.json 의 파일별 -I/-D 사용
python3 change_impact.py analyze --git-repo . --old-rev main --new-rev HEAD \
  --compile-commands build/compile_commands.json --entry Os_Task_10ms --isr Can_RxIsr

# 3) SIL 차등 실행만 (analyze 가 만든 초안 시나리오를 고쳐서 사용)
python3 change_impact.py sil --old v1/src --new v2/src --scenario impact_out/sil_scenario.json -o impact_out/sil

# 4) 분석 + SIL 을 한 번에 (실행 결과가 보고서의 '상태' 열에 합쳐짐)
python3 change_impact.py analyze --old v1/src --new v2/src --scenario my_scenario.json

# 5) 함수·변수 의존성 표 (한 버전 전체)
python3 change_impact.py deps --src v2/src --transitive          # impact_out/deps/deps.md, deps.json, deps_access.csv
python3 change_impact.py deps --src v2/src --var g_sensor        # 구조체면 하위 필드까지 터미널에 출력
python3 change_impact.py deps --src v2/src --func Os_Tick_ISR

# 6) 코드를 바꾸기 전에: 이 줄/변수를 바꾸면 어디까지 확인해야 하나
python3 change_impact.py slice --src v1/src --at timeout.c:9 --what-if "tick >= 200U"
python3 change_impact.py slice --src v1/src --var g_sensor.valid

# 7) 결과가 달라지는 입력·상태 자동 탐색 (analyze 결과로 시나리오를 만들고 SIL 로 실행)
python3 change_impact.py explore --old v1/src --new v2/src --max-cases 300 --cycles 6 --seed 1
```

### 함수·변수 의존성 (`deps`)

- 함수별: 읽는 변수·쓰는 변수(줄 번호), 호출을 통해 추가로 읽고 쓰는 변수(`--transitive`), 호출·피호출(함수 포인터 호출 표시), 외부 함수 호출, 도달하는 진입점(ISR 표시)
- 변수별: 쓰는 함수·읽는 함수, 접근 진입점, ISR·태스크 공유 / 쓰기만 함 / 읽기만 함 표시
- `deps_access.csv` 는 함수·변수·read/write·direct/via_call 한 줄씩의 tidy 형식이라 엑셀 피벗에 바로 쓸 수 있습니다.
- 읽기·쓰기는 포인터 분석을 거친 실제 대상 기준입니다 (`p->counter = 0` → `g_sensor.counter` 쓰기).

### 변경 가정 슬라이스 (`slice`)

- `--at 파일:줄` (여러 번 가능): 그 줄의 문장이 바뀐다고 보고 전방 슬라이스를 만듭니다. 파일은 루트 기준 경로나 파일 이름. 줄에 분석 대상 문장이 없으면 가까운 줄을 알려 줍니다.
- `--var 이름`: `--at` 와 함께면 그 줄에서 이 변수를 쓰는 문장만, 단독이면 이 변수에 값을 쓰는 모든 문장이 시작점입니다.
- `--what-if "식"`: 그 줄의 조건식·대입식을 이 식으로 바꾼다고 가정해 **판정이 달라지는 구간과 경계값**을 계산하고 보고서 조건 열에 넣습니다 (예: `tick >= 100U` → `tick >= 200U` 이면 `100 ≤ tick ≤ 199`).
- 결과(`slice_report.md`)는 analyze 보고서와 같은 형식(확인할 함수·변수 표, 경로, 진입점, 입력 후보)입니다.

### 차이 입력 자동 탐색 (`explore`)

1. **후보 값**: 영향받는 함수(변경 전·후 모두)의 단일 변수 비교식 경계값(c-1, c, c+1), switch 의 case 값, 자료형 끝값을 모읍니다.
   진입점 인자(이름 일치), 전역·static **초기 상태**, 외부 함수(스텁)의 **반환값·출력 포인터 값**을 탐색 대상으로 씁니다.
2. **케이스**: 기본값 → 한 변수씩 경계값 → 무작위 조합 → **주기별 입력 시퀀스**(같은 값 유지 후 전환, 카운터·디바운스·래치용) 순으로 `--max-cases` 개까지. `--seed` 가 같으면 같은 케이스입니다.
3. **실행·최소화**: 변경 전/후를 같은 케이스로 실행하고, 관찰 대상마다 차이를 내는 케이스를 더 단순한 입력(시퀀스 → 단일 값, 구간 합치기, 기본값 복원)으로 줄입니다.
   **입력만으로 재현**한 경우와 **초기 상태를 바꿔 재현**한 경우를 따로 보고합니다 (초기 상태는 실제 도달 가능 여부를 확인해야 함).

결과: `explore_report.md`(후보 값과 출처, 관찰 대상별 최소 재현 입력, 값별 차이 여부 ●/○), `explore_witness.json`(최소 재현 케이스만, `sil` 로 재실행 가능),
`explore_scenario.json`(전체 케이스), `impact_report.md`(실행 확인 상태 반영). 예제에서는 `tick` 값별 결과가 `100·149·199 ●, 99·200 ○` 로
정적으로 계산한 구간 `100 ≤ tick ≤ 199` 와 일치하고, 카운터 리셋 변경은 `raw=[1001, 1001, 1000]`(두 주기 높음 → 낮음) 시퀀스로만 재현된다는 것을 찾아냅니다.

예제(`examples/impact`)는 질문의 예시 코드를 그대로 포함합니다. 헤더의 `#define TIMEOUT_TICKS 100U → 200U` 변경이
`g_timeout` → `UpdateDiagnostic()`의 `if` → `g_faultLatched` → `ReadStatus()` 반환값으로 전달되는 경로와 차이 구간 `100 ≤ tick ≤ 199`를 찾아냅니다.

```bash
python3 change_impact.py analyze --old examples/impact/old --new examples/impact/new \
  --scenario examples/impact/scenario.json -o impact_out
```

### 분석 방법

| 단계 | 내용 |
|---|---|
| 실제 빌드 조건으로 해석 | 파일마다 `gcc -E`(또는 `--cpp`)로 전처리 후 파싱. `compile_commands.json`, `-I/-D/-U`, `--force-include` 반영. 같은 파일도 빌드 구성이 다르면 구성별로 따로 실행 |
| 변경 지점 추출 | 전처리 후 AST를 함수별 문장 서명으로 비교(difflib). 매크로·typedef·구조체·전역 초기값·자료형 변경 포함, 줄 이동·상수 표기(`100U`/`0x64`) 차이는 무시. 삭제된 문장은 **변경 전** 프로그램에서, 수정된 문장은 양쪽에서 추적 |
| 의존성 | 함수별 CFG → **제어 의존성**(후위 지배자, 조기 return/break/goto 포함), **데이터 의존성**(지역 변수: 도달 정의, 전역·static·포인터 메모리: 흐름 비민감), **포인터 분석**(Andersen, 구조체 필드 구분·union 겹침·배열 원소 통합, 함수 포인터 호출 대상 해석), 함수 간 인자·반환값·호출 여부 |
| 전방 슬라이싱 | 변경 지점에서 영향 전파. 호출 문맥 구분(`--context-depth`, 기본 2)으로 공용 유틸 함수의 반환값이 모든 호출자에게 퍼지는 과대 보고를 줄임 |
| 값 범위·경로 조건 | 상수 조건으로 꺼진 코드 제외, 지역 변수 덮어쓰기 반영, 단일 변수 비교식(`tick >= 100U → 200U`, `&&`/`\|\|`/`!` 포함)은 **판정이 달라지는 구간과 경계값** 계산(자료형 범위 반영) |
| SIL 차등 실행 | 대상 .c 를 하네스에 `#include`(static 접근 가능), 외부 함수/변수는 링크 오류에서 찾아 **자동 스텁**(호출 인자를 외부 출력으로 기록, 반환값·출력 포인터 값은 시나리오에서 지정), 하드웨어 주소는 `memory_map`으로 호스트 메모리에 매핑 |

### 결과물 (`-o`, 기본 `impact_out/`)

| 파일 | 내용 |
|---|---|
| `impact_report.md` | 1 변경 위치(원본 소스·전처리 후 전/후) · 2 **영향받는 함수·변수 표**(어떻게·차이 조건·위치·상태) · 3 영향 전달 경로 · 4 진입점(태스크·ISR·콜백)과 여러 진입점에서 접근하는 변수 · 5 확인용 테스트(경계값) · 6 SIL 결과 · 7 **분석에서 제외되거나 가정한 구성** |
| `impact.json` | 위 내용 전체 (CI·다른 도구·LLM 입력용) |
| `impact.dot` | Graphviz 영향 그래프 (`dot -Tsvg impact.dot -o impact.svg`) |
| `sil_scenario.json` | SIL 시나리오 초안: 영향 함수에 닿는 진입점을 **상태를 쓰는 쪽 → 읽는 쪽** 순으로 호출, 차이 구간 경계값을 params 로, 영향 전역 변수를 observe 로 채움. 끝나지 않는 태스크 함수는 그 안에서 호출하는 함수로 대체 |
| `sil/sil_report.md`, `sil/sil_result.json` | 케이스별 최초 차이 단계, 관찰 대상별 차이, 차이 값이 바뀐 단계 상세 |

보고서의 상태 열은 **정적 분석상 영향 후보 / 실행으로 차이 확인 / 실행에서 차이 없음(시나리오 한정)** 으로 구분됩니다.
사양서가 없으므로 발견한 동작 차이가 의도된 변경인지 결함인지는 별도로 판단해야 합니다.

### SIL 시나리오 형식

```json
{
  "sources": ["sched.c", "sensor.c", "timeout.c"],
  "include_dirs": ["."], "defines": ["UNIT_TEST=1"],
  "observe": ["g_timeout", "g_sensor.counter", {"expr": "g_ratio", "fmt": "f"}],
  "params": {"tick": [99, 100, 199, 200], "raw": [[1001, 1001, 1001, 0, 1001, 1001]]},
  "init": {"g_faultLatched": 0},
  "memory_map": [["0x40000000", "0x10000"]],
  "steps": [
    {"stub_return": {"Rte_Read_Speed": 0}}, {"stub_out": {"Rte_Read_Speed": {"0": 150}}},
    {"repeat": 6, "steps": [
      {"call": "Sensor_MainFunction", "args": ["${raw}"]},
      {"call": "UpdateTimeout", "args": ["${tick}"]},
      {"call": "Os_Tick_ISR"}
    ]},
    {"call": "ReadStatus", "record": true}
  ]
}
```

- `params` 는 조합(곱집합)마다 케이스 하나. 값이 **리스트 안의 리스트**면 `repeat` 주기별 입력 시퀀스입니다(카운터·리셋·디바운스 확인용).
- 각 `call` 뒤에 `observe` 의 모든 식을 기록하고, `record: true` 면 반환값도 기록합니다. 스텁 호출은 `call:<함수명>` 으로 기록됩니다.
- `set`(변수 대입), `code`(임의 C 문장)도 쓸 수 있습니다. 케이스마다 별도 프로세스로 실행하므로 초기 상태가 섞이지 않습니다.

### 주요 옵션

| 옵션 | 설명 |
|---|---|
| `--entry FUNC`, `--isr FUNC` | 진입점·ISR 지정 (호출자 없는 함수는 자동으로 진입점 후보, 이름에 isr/irq/handler 가 있으면 ISR 추정) |
| `--extern-model JSON` | 본문 없는 함수의 효과 지정 (예: [`examples/impact/extern_model.json`](examples/impact/extern_model.json)). 기본은 포인터 인자가 가리키는 메모리를 읽고 쓴다고 가정(`const` 포인터는 읽기만) |
| `--strip KEYWORD` | 파서가 모르는 컴파일러 확장 키워드 제거 (예: `--strip __interrupt --strip __far`) |
| `--system-headers` | 내장 최소 표준 헤더 대신 시스템 헤더 사용 (기본은 GNU 확장 없는 내장 헤더) |
| `--include/--exclude GLOB` | 분석 대상 파일 (기본 `*.c`) |
| `--context-depth N` | 호출 문맥 구분 길이 (0 = 구분 안 함) |
| `--fail-on-impact` | 영향 항목이 있으면 종료 코드 3 (CI 게이트) |
| `-j N` | 병렬 파싱 프로세스 수 |

### 한계와 가정 (보고서 7절에도 기록됨)

- 전역·static·포인터 메모리는 흐름 비민감으로 연결합니다(실행 주기 간 상태 전달과 ISR 선점을 놓치지 않기 위한 보수적 선택). 중간에 덮어써져 실제 영향이 없는 후보가 포함될 수 있습니다.
- 배열 원소는 구분하지 않습니다. 함수 포인터 테이블의 모든 항목이 호출 후보가 됩니다.
- 파싱에 실패한 파일, 본문 없는 외부 함수, 대상을 풀지 못한 함수 포인터, 대상을 모르는 포인터, 하드웨어 주소 접근은 보고서에 따로 나열합니다. 인라인 어셈블리는 제거하고 분석합니다.
- `explore` 는 후보 값과 케이스 수 안에서의 탐색입니다. 여러 변수가 얽힌 조건·산술식에서만 생기는 차이는 무작위 조합·시퀀스로만 탐색되므로 놓칠 수 있고, 경로 실행 가능성을 기호적으로 증명하지는 않습니다(모델 검사기 연동은 포함하지 않음).
- **SIL 에서 차이가 검출되지 않았다는 결과는 그 시나리오에 한한 것입니다.** 실제 인터럽트 지연·물리적 타이밍은 정적 분석과 SIL 로 확정할 수 없습니다.
- `impact_report.md` 는 경로와 소스 위치를 포함하므로, LLM 에 영향 설명·테스트 케이스 초안 작성을 맡길 때 입력으로 쓰기 적합합니다. 의존성 존재 여부는 분석 결과와 실행 증거로 확인하세요.

## 변경 기반 보안 취약점 분석 (CPG + Taint + Symbolic Execution)

`folder_impact.py`(및 `change_impact.py analyze`)는 변경 영향 분석에 이어 **이번 변경 때문에 새로 생긴 보안 취약점**을 찾습니다.
세 기술을 따로 실행하지 않고 하나의 파이프라인으로 묶어, CPG·Taint 로 후보를 좁힌 뒤 그 후보에만 기호 실행을 합니다.

| 단계 | 모듈 | 질문 | 결과 |
|---|---|---|---|
| 1 변경점 | `diff.py` (AST Diff) | 어떤 함수·조건·변수·상수가 바뀌었나 | 변경 문장, 조건 판정 차이 구간 |
| 2 CPG | `program.py`·`slicer.py`·`cpg.py` | 변경이 어떤 경로·데이터에 영향을 주나 | 변경 → 위험 연산 경로 (제어·데이터 의존) |
| 3 Taint | `cpg.py` + `config/security_rules.yaml` | 외부 입력(Source)이 위험 연산(Sink)까지 오나 | Source → Sink 경로 |
| 4 Symbolic Execution | `symexec.py` (z3) | 안전 속성을 어기는 입력이 실제로 있나 | 반례 입력 (예: `len = 9`) 또는 안전 증명 |
| 5 변경 전후 비교 | `security.py` | 변경 전 코드에서는 어땠나 | **신규 발생 · 기존 존재 · 해결 · 판정 보류** |
| 6 보고서 | `report.html` '보안 취약점' 탭, `summary.md`, `security/vulnerability_report.json` | | CWE · 등급 · 영향 함수 · 발생 조건 · 근거 경로 · 검증 방법 |

- **위험 연산(Sink)**: 배열 인덱스(CWE-787/125), 복사·초기화 길이 대 목적지 버퍼 크기(CWE-787), 0 나누기(CWE-369), 시프트 양(CWE-1335), 값 잘림·넘침(CWE-197/190), 포인터 역참조(CWE-476/822), 반복 조건(CWE-834).
  위험한 **피연산자** 기준으로 판정하고, 감싸는 조건(`if (d != 0U)`)·자료형 범위로 안전이 증명되면 보고하지 않습니다.
- **보안 조건 변경**: 규칙의 권한·세션(CWE-862), 실패 횟수·잠금 시간(CWE-307), 무결성 검증(CWE-354), 보안 상태(CWE-840) 조건,
  외부 입력 검증 조건(CWE-20)이 바뀌거나 삭제되면 보고합니다.
- **규칙 파일** `config/security_rules.yaml`: Source(함수 인자, `Rte_Read_*` 같은 출력 인자, 반환값), Sink(복사 함수와 길이·목적지 인자), 보안 검사 이름 패턴을 프로젝트에 맞게 고칩니다 (`--rules` 로 다른 파일 지정, JSON 도 가능).
- **기호 실행**은 위험 연산이 있는 함수 하나에서 진입점 → 위험 연산 경로를 펼쳐 z3 로 풉니다(반복문은 2회까지, 경로 64개·전체 180초 한도).
  인자·전역은 자료형 범위 안의 임의 값으로 보므로 반례는 '이 값이 들어오면' 이라는 뜻이며, 외부 입력 도달 여부(Taint)와 함께 판단합니다.
  z3 가 없으면 조건 구간 증명으로 대신합니다.
- **외부 도구 교차 검증**: 같은 규칙으로 `security/joern/taint.sc`(Joern `reachableByFlows`), `security/codeql/ChangeTaint.ql`
  (`TaintTracking::Global`), `security/klee/<ID>/`(KLEE 하네스·`klee_assert` 삽입 소스·실행 스크립트)를 만듭니다.
  도구가 설치되어 있으면 `--joern`, `--codeql --codeql-build "빌드 명령"`, `--klee` 로 실행하고 결과를 보고서에 표시합니다.
- CPG 는 `security/cpg.json` 으로 내보냅니다 (노드 label·간선 type 은 Joern 이름: METHOD, CALL, CONTROL_STRUCTURE, CFG, CDG, REACHING_DEF, CALL, ARGUMENT, RETURN).

```bash
pip install pycparser z3-solver pyyaml        # z3·PyYAML 은 선택 (없으면 구간 증명·내장 규칙 사용)
python folder_impact.py before/ after/ -o result --rules config/security_rules.yaml
python folder_impact.py before/ after/ -o result --klee --klee-include /usr/local/include   # KLEE 재현까지
```

`report.html` 의 기본 화면은 **변경 검토**입니다. 변경된 .c 파일 → 변경 함수(라인·변경 전후 코드) 순으로 묶고, 그 함수와 직접 연결된 위험을 먼저,
다른 함수로 전파된 위험은 값 전달·자료형 변경 경로가 있을 때만 따로 보여 줍니다. 함수 호출·변수 읽기쓰기의 추가·삭제는 '의존성 변화(영향 분석)'로
구분해 취약점과 섞지 않습니다. 필터는 **신규 위험 · 우선 검토 · 전체 변경점 · 판정 보류**이고, 기존 존재·해결 위험은 기본 화면에서 뺍니다(체크하면 표시).
각 항목의 **검증 수준**은 기호 실행 반례 발견 > 정적 규칙 위반 > Taint 경로 발견 > 검증 불충분 입니다.

CWE 분류는 기본으로 표시하지 않으며, `--cwe` 를 주면 각 항목에 CWE 번호·이름을 붙입니다.

판정은 정적 분석 근거입니다. `신규 발생`은 변경 후 위반이 증명되고 변경 전에는 안전했다는 뜻이고, 실제 악용 가능성(입력 경로·보안 영향)은
반례 입력으로 재현해 확인해야 합니다.

## 변경점 검토 체크리스트 (review.py)

변경점마다 아래 25개 항목을 자동으로 검토하고, 항목별로 **무엇을 검토했는지**와 **검증자가 추가로 판단할 내용**을 남깁니다.
결과는 적합/부적합이 아니라 다섯 가지로 구분합니다.

| 결과 분류 | 의미 |
|---|---|
| 변경 사실 확인 | 코드 구조·조건·의존성의 변경이 확인됨 |
| 위험 후보 탐지 | 정적 분석으로 잠재적인 취약·결함 경로가 식별됨 |
| 위반 경로 확인 | 정의한 안전 속성을 위반하는 실행 경로 또는 반례가 확인됨 |
| 검증자 확인 필요 | 요구사항·환경·시험 근거가 있어야 적합 여부 판단 가능 |
| 분석 불충분 | 모델링 누락·도구 한계 등으로 판정 불가 |

| 영역 | 항목 | 자동 분석 방법 |
|---|---|---|
| 변경사항·기능 로직 | 1-2, 1-3, 3-1 ~ 3-5 | AST 비교(연산자·상수·호출·형변환·평가 순서), 분기·return 추가·삭제, 문장 이동, 상태 변수의 대입·판정 값과 도달 불가 상태·전이 충돌, 같은 식이 남은 다른 위치, 삭제된 문장이 쓰던 값의 남은 읽기 |
| 초기화·오류 처리 | 4-1 ~ 4-4 | 초기값 변경·새 필드, 지역 변수 도달 정의(초기화 전 사용), 오류·리셋·복구 호출과 그 실행 조건, 카운터 증감·임계값, else-if 사슬 조건의 동시 성립(z3) |
| 데이터·수치 | 5-1 ~ 5-3 | 경계 비교·임계값과 판정 차이 구간, 형변환·연산 순서·자료형 변경, 보안 분석의 인덱스·길이·잘림 위험, 바뀐 파라미터의 모든 참조와 분기 |
| 인터페이스·의존성 | 6-1 ~ 6-5 | 호출·읽기·쓰기 추가·삭제, 함수 원형·선언/정의/호출 불일치·구조체·enum·함수형 매크로, Def-Use, 같은 값을 다른 기준으로 판단하는 위치, 외부 함수·헤더·#include |
| 실행 순서·동시성 | 7-1 ~ 7-3 | 호출·상태 갱신 순서, 반복·종료 조건, 타이머 시작·리셋·만료 조건, ISR·태스크 공유 변수 접근과 보호 구간 (ISR 모델이 없으면 '분석 불충분') |
| 설정·컴파일 조건 | 8-1, 8-2, 8-4 | 변경 파일 원문의 #define·#if·#ifdef·#include 비교, 기능 스위치, #if 블록 안의 변경 (비활성 #else 경로는 '분석 불충분') |

이름으로 찾는 항목(오류 처리, 카운터, 타이머, 초기화 함수, 보호 구간, 기능 스위치)의 패턴은 `config/security_rules.yaml` 의 `review_patterns` 로 바꿉니다.

보고서 구성 (`report.html`):

| 영역 | 보여 주는 것 | 검증자가 이해할 것 |
|---|---|---|
| 상단 요약 | 비교 버전(예: `26A10 → 26A22`), 수정 파일 수, 분석한 C 파일 수(변경 전/후), 분석 범위(`--scope changed` 이면 "변경 파일과 변경 헤더를 사용하는 파일에 한정"), 주요 동작 변경, **미완료 검증**(Joern·CodeQL·KLEE·SIL·기호 실행 실행 여부, 파싱 실패, 요구사항 미첨부) | 이번 report 가 무엇을 다루고 무엇을 확인하지 않았는가 |
| 변경 검토 (기본) | 변경 목록(기능 중심 제목 · 관련 함수 · 우선순위 · 판정 · 비교 상태 · 검토 상태), 필터(전체 / 신규 위험만 / 위험 후보 이상 / 검토 미완료) | 어떤 변경부터 읽을 것인가 |
| 변경 카드 | 변경마다 같은 구조: **① 무엇이 변경되었는가**(2~3문장 동작 설명 → 함수·파일·줄 → 변경 전후 비교표(상위 조건·실행 순서·대입 대상 포함, '달라지는 동작' 행) → 변경 전후 코드 나란히 → 영향 범위(변경 발생 → 상태 판단 → 외부 출력 후보, 공유 변수 연결은 호출과 구분)) → **② 스크립트가 무엇을 확인했는가**(검사 항목 · 실행 상태 · 확인한 내용 · 이 결과로 확정할 수 없는 것) → **③ 어떤 문제가 발견되었는가** → **④ 검증자는 무엇을 추가로 확인해야 하는가**(입력·상태 조건 · 관찰할 내용 · 판정 기준) | 변경의 원인부터 검증 방법까지 |
| 발견 사항 | 같은 위치·원인을 묶은 고유 문제. 제목은 기능의 검토 문제로 쓰고 자동 분류 규칙 이름은 근거 상세에 보존. 판정(근거 수준: 변경 사실 확인 / 위험 후보 / 분석 모델 내 위반 확인 / 실행 재현 확인 / 분석 불충분)과 비교 상태(신규·기존·해결·판정 보류)를 따로 표시 | 무엇이 확정이고 무엇이 후보인가 |
| 검증 기록 | 시험·판단 항목마다 담당자 · 결과 · 증거 · 판정 근거. **결과만 고르면 완료가 되지 않고** 증거와 근거까지 적어야 '완료'. 브라우저에 저장, CSV 내보내기 | 무엇이 검증되었는가 |
| 근거 상세 | 함수 전체 코드 차이, 전체 영향 경로, 자동 분류 규칙 원문·검토 항목 지적, 분석 설정(자동 탐지 포함)·도구 실행 상태·25개 검토 항목·파싱 실패·분석 가정 | 판단의 근거를 직접 확인할 수 있는가 |

변경(C01) · 검사(C01-K1) · 발견(V1, F1) · 시험(T01-1) 은 ID 로 연결되어, 발견 사항에서 원인 코드와 필요한 시험으로 바로 이동합니다.
요구사항이 첨부되지 않으면 판정 기준은 '기대값 미확정 — 관련 요구사항 확인 필요'로 남기고, 변경 의도는 '요구사항 확인 필요'로 표시합니다.
'문제 없음'은 '이번 분석 범위에서 해당 규칙의 위반 미탐지'로 표시합니다. 변경 카드 데이터는 `summary.json` 의 `story` 에 있습니다 (`c_impact/story.py`).

변경점 묶음은 AST 변경 노드를 '함수 + 같은 제어 블록(if/else-if 사슬·반복·switch)의 변경' 단위로 묶은 것이며(`c_impact/units.py`),
새로 추가된 전역 변수는 그 변수를 쓰는 변경의 '제어 변수' 로 합칩니다.

- `summary.md` 2절(①→②→③, 함수별)과 3절(검토 항목 체크리스트), `summary.json` 의 `review`.

## 빌드 설정 자동 탐지 (detect_config.py, `--auto-config`)

프로젝트마다 손으로 넣던 `-D __ghs__ -D CPU_BOARD_REVA -I ...\GHS --exclude *SAMPLECODE* --cpp "... -include cia_memclass.h"` 를
프로젝트 자체에서 찾아 적용합니다.

| 단계 | 근거 |
|---|---|
| 컴파일러 | ① 빌드 파일(*.gpj·*.ewp·.cproject·Makefile·*.mk·*.bat·CMakeLists.txt, 프로젝트 폴더와 그 **상위 폴더**)의 컴파일러 이름 → ② 없으면 후보 컴파일러 매크로마다 **분석 대상 파일을 실제로 전처리·파싱해 보고** 오류가 가장 적은 것 → ③ 결과가 같으면 소스의 컴파일러 고유 구문(`#pragma ghs`, `__no_init`, `__mfcr()` …)·링커 파일(*.lsl·*.icf·*.sct) 점수. 어떤 매크로로도 전처리 결과가 같으면 '분석에 영향 없음'(참고)으로 표시 |
| 컴파일러 매크로 | 계열별 미리 정의 매크로 (GHS `__ghs__`, TASKING `__TASKING__`, IAR `__IAR_SYSTEMS_ICC__` …) |
| Compiler.h | 여러 개면 컴파일러 계열 폴더 |
| 메모리 클래스 헤더 | VAR·FUNC·P2VAR 의 memclass 이름을 빈 매크로로 정의 (memclass_shim 과 같은 방식) |
| 보드·변형 매크로 | `#error` 가 나면 그 `#if/#elif` 사슬에서 선택 (계열 이름 → 빌드 파일 -D → 첫 번째 순), 대안도 기록 |
| 컴파일러 확장 키워드 | 파서가 멈춘 줄의 `__interrupt`, `__far`, `__no_init` 같은 키워드를 `-D 이름=` 으로 지움 (나아지지 않으면 되돌림). IAR 의 `int x @ 0x100;` 은 cpp_filter 가 지움 |
| include 경로 | 못 찾은 헤더를 프로젝트에서, 그다음 `--search` 위치에서 찾아 추가. 어디에도 없으면 '헤더 없음'으로 기록 |
| 제외 폴더 | SAMPLECODE·example·demo 등 예제 폴더 |

시험하는 파일은 실제로 분석할 파일(`--scope changed`: 변경 파일과 그 헤더를 쓰는 파일, 변경 전·후 모두)이 먼저이고,
모자라면 무작위 표본(`--sample`, 기본 60)을 더합니다. 그래도 남은 실패는 원인별(헤더 없음 · #error · 컴파일러 확장 키워드 ·
정의되지 않은 타입 · 구문 오류)로 묶어 해결 방법과 함께 보고합니다. 정의되지 않은 타입은 그 typedef 가 있는 헤더 위치도 알려 줍니다.
`--auto-config` 없이 실행해도 summary.md·report.html 에 같은 '파싱 실패 원인' 표가 나옵니다.

결정마다 근거와 확신도(높음·중간·낮음)를 남기고, '낮음'은 보고서 경고와 '분석 근거 및 범위' 탭에 표시합니다.

```powershell
python detect_config.py "변경전폴더" "변경후폴더"            # cia_config.json, run_auto.ps1 생성 (확인 후 실행)
python folder_impact.py "변경전폴더" "변경후폴더" -o result --scope changed --auto-config   # 탐지 후 바로 분석
python folder_impact.py "변경전폴더" "변경후폴더" -o result --scope changed --auto-config --search "S:\MCAL" --search "C:\ghs\comp_201754"
                                                    # 프로젝트 밖 헤더(MCAL·BSW·컴파일러 include)도 찾기
```

직접 준 `-D`·`-I`·`--exclude` 는 자동 탐지 결과에 더해지고, `--cpp` 를 직접 주면 그 값을 씁니다.
프로젝트에 `compile_commands.json` 이 있으면 `--compile-commands` 를 쓰는 것이 가장 정확합니다.

## 테스트

```bash
python3 -m unittest discover -s tests -v
```
