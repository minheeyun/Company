# GitLab Changes → 파일별 HTML 추출기

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

## 사양–코드 정합성 검증 (spec_verify.py)

**자연어 기능사양서와 Embedded C 코드 간 의미적 정합성을 검증하고, 사양 공백 및 실행 조건에 따른 잠재 기능 결함을 식별하는 근거 기반 SW 검증 지원 도구**입니다.
코딩 규칙 위반 검출이 아니라, 사양서가 정의한 동작뿐 아니라 **정의하지 않은 조건까지 찾아내고 그 조건에서 코드가 실제로 어떻게 동작하는지** 분석하는 것이 목적입니다.
기존 정적 분석 도구를 대체하지 않고, 정적 분석으로 잡기 어려운 의미적 불일치·사양 미정의 조건·기능적 잠재 결함을 보완합니다.

### 분석 단계

```
기능사양서 + Embedded C 소스 + 실행 환경(env.json)
  │
  0. 정적 코드 분석 (결정적, LLM 미사용)  specverify/cfront.py, checks.py, tools.py
  │   gcc 전처리(빌드 정의 반영) → pycparser AST → 함수·호출 그래프·전역 접근·조건식·가드·상태 전이
  │   typedef→실제 비트 폭, 매크로 실제 값, #if 비활성 라인, 규칙 기반 결함 후보 + gcc 경고 교차 확인
  1. 사양 분석      요구사항 구조화 (코드는 보지 않음), AMBIGUOUS / UNDEFINED 분류
  2. 구현 추적      요구사항 → 파일·함수·라인, 분산 구현 경로, TRACE_NOT_FOUND
  3. 정합성 분석    조건식·임계값/경계·타이머/카운터·상태 전이·우선순위·예외·초기화/복구 불일치
  4. 사양 공백 분석 미정의 입력·상태·이벤트 조합 → SPEC_GAP / AMBIGUOUS + 사양 질문 + 현재 코드 동작
  5. 코드 결함 분석 정적 후보 판정(확정/가능성/오탐 추정) + 런타임·논리·동시성 의미 분석
  │
  근거 검증·가드레일 (specverify/evidence.py) → 보고서 → 검증자 최종 확인
```

LLM은 각 단계를 JSON 스키마(structured outputs)로만 답하며, 그 결과는 그대로 믿지 않고 기계적으로 검증합니다.

| 검증 | 내용 |
|---|---|
| 원문 대조 | 요구사항 인용문이 사양서 원문에 실제로 있는지, 라인 범위 보정. 사양서에 없는 ID를 원문 ID로 쓰면 생성 ID로 정정 |
| 코드 위치 대조 | 제시된 파일·라인 존재, 함수 범위 안인지, 심볼이 그 라인 근처에 있는지, 비활성(#if) 라인인지 (보고서에 ✔/✖) |
| 판정 가드레일 | 추적 실패만으로 미구현 확정 금지 · 실행 환경 정보 없이 동시성 결함 확정 금지 · CERTAIN 계산/도구 근거 없는 런타임 결함 확정 금지 · 사실(FACT) 근거 없는 확정 금지 · 사양 공백은 결함으로 확정 금지 · 근거 위치를 확인할 수 없으면 '검토 필요'로 강등. 조정 내역은 항목마다 표시 |

### 사용법

```bash
pip install -r requirements.txt          # anthropic, pycparser (gcc 권장)
export ANTHROPIC_API_KEY=sk-ant-...

# 전체 파이프라인
python3 spec_verify.py --spec docs/BLV_FS.md --src src/ --env env.json -o out/

# LLM 없이 정적 분석만 (결함 후보 + gcc 교차 확인)
python3 spec_verify.py --src src/ --env env.json --static-only -o out/

# 사내 승인 LLM 창구 사용: 단계별 프롬프트 파일을 내보내고 응답 JSON을 받아 이어서 실행
python3 spec_verify.py --spec docs/BLV_FS.md --src src/ --env env.json --manual out/llm -o out/
#   → out/llm/stage1_requirements.prompt.md 생성, 응답을 stage1_requirements.response.json으로 저장 후 같은 명령 재실행
```

| 옵션 | 설명 |
|---|---|
| `--spec` | 기능사양서 (.md/.txt, .pdf는 pypdf 또는 pdftotext, .docx는 python-docx 필요). 원문 라인 추적을 위해 텍스트 변환본 권장 |
| `--src` | .c/.h 파일 또는 폴더 (여러 번 지정 가능) |
| `--env` | 실행 환경 JSON (아래). 없으면 기본값을 가정하고 보고서의 '분석 전제'에 명시 |
| `-D NAME=VAL`, `-I DIR` | 빌드 정의·헤더 경로 추가 (조건부 컴파일을 실제 빌드 구성대로 반영) |
| `--resume` | `out/stages/*.json`에 저장된 단계 결과를 재사용 (중단 후 이어서 실행) |
| `--stop-after N` | N단계까지만 수행 |
| `--tool-cc`, `--gcc-analyzer`, `--cppcheck`, `--no-gcc` | 외부 도구 교차 확인 설정 (대상 교차 컴파일러 지정 권장) |
| `--model`, `--effort`, `--batch-size`, `--max-source-chars` | LLM 설정. 소스가 한도보다 크면 관련 함수만 발췌해 전달 |
| `--report-only report.json` | 보고서만 다시 생성 |
| `--prompt-dir` | 단계별 프롬프트 폴더 교체 (기본 `prompts/specverify/`) |

### 실행 환경 정보 (env.json)

공유변수 경쟁·오버플로 판단은 대상 환경에 따라 달라집니다. 정보가 없으면 결함으로 확정하지 않고 '검토 필요'로 보고합니다.

```json
{
  "target": {"name": "16-bit MCU", "int_bits": 16, "long_bits": 32, "char_signed": false, "atomic_access_bits": 8},
  "contexts": [
    {"name": "OsTask_10ms", "kind": "task", "entries": ["BlvDiag_MainFunction"], "period_ms": 10, "priority": 5},
    {"name": "ADC_ISR", "kind": "isr", "entries": ["Adc_ConvEndIsr"], "priority": 100},
    {"name": "EcuM_Init", "kind": "init", "entries": ["BlvDiag_Init"], "concurrent": false}
  ],
  "protection": {"enter": ["SuspendAllInterrupts", "SchM_Enter_*"], "exit": ["ResumeAllInterrupts", "SchM_Exit_*"]},
  "defines": {"BLV_USE_FILTER": "1"},
  "typedefs": {"U8": "unsigned char"},
  "notes": ["ADC는 10bit (0..1023)"]
}
```

### 결과물

| 파일 | 내용 |
|---|---|
| `report.html` | 단독 실행 보고서. 판정·심각도·단계 필터, 검출 항목별 검증자 판정(수용/기각/보류) 입력과 JSON 내보내기 |
| `report.md` | 같은 내용의 Markdown (리뷰 기록·이슈 첨부용) |
| `report.json` | 전체 결과 (보고서 재생성, 다른 도구 연계용) |
| `test_cases.csv` | 검출 결과별 테스트 케이스 (사전 조건 / 조작 / 기대 결과 / 관찰 수단) |
| `stages/*.json` | 단계별 LLM 결과 (`--resume`용) |

각 검출 결과에는 ① 요구사항 ID·원문 ② 코드 위치(파일·함수·라인, 검증 표시와 코드 발췌) ③ 유형·심각도 ④ 사양 vs 코드 비교 ⑤ 발생 조건·실행 경로 ⑥ 사양 기준 예상 동작 vs 코드 추론 동작 ⑦ 사실/추론/가설로 구분한 근거와 불확실성 ⑧ 권장 수정 ⑨ 추가 확인 사항 ⑩ 테스트 케이스가 들어갑니다.

분류 코드: 요구사항 요소 `DEFINED / AMBIGUOUS / UNDEFINED`, 추적 `TRACED / PARTIAL / TRACE_NOT_FOUND`,
판정 `CONFIRMED_DEFECT / PROBABLE_DEFECT / POTENTIAL_RISK / NEEDS_REVIEW / SPEC_CLARIFICATION`,
정적 후보 확실성 `CERTAIN`(자료형·상수만으로 성립) / `PROBABLE` / `POSSIBLE`(런타임 조건 의존) / `REVIEW`(정보 부족).

### 예제

`examples/specverify/`에 배터리 저전압 진단 사양서·코드·env.json과 단계별 LLM 응답 샘플(`sample_llm/`)이 있습니다.
API 키 없이 전체 흐름을 재현할 수 있고, 결과는 [`examples/specverify/sample_report/report.md`](examples/specverify/sample_report/report.md)에 있습니다.

```bash
python3 spec_verify.py --spec examples/specverify/spec_blv.md --src examples/specverify/src \
  --env examples/specverify/env.json --manual examples/specverify/sample_llm -o out_example
```

예제에서 드러나는 대표 사례:
- `uint8` 유예 카운터와 `BLV_START_MASK_CNT(300U)` 비교가 항상 참 → 시동 후 진단이 영구 정지 (정적 계산 CERTAIN + gcc `-Wtype-limits`로 확정)
- `uint8` 고장 카운터가 지속 저전압에서 255→0 순환 → 지속 고장 중 경고등이 2.56초 주기로 꺼졌다 켜짐 (사양 의도·자료형·누적 조건·판정 로직을 함께 봐야 드러남)
- 사양은 "9000mV 이하", 코드는 `<` / START 중 카운터 "유지" 요구와 달리 0으로 초기화 / START 중 확정 고장의 경고등 소등
- int 16bit 대상에서 `u16Raw * 5000U` 오버플로 (호스트 gcc는 32bit라 경고하지 않음 → 대상 컴파일러 확인 항목으로 보고)
- 사양 공백: 무효값 정의, ADC 샘플 갱신 중단, IG OFF 시 경고등, 이전 사이클 확정 DTC의 해제 경로

### 한계

- C 파싱은 gcc 전처리 + pycparser 기반입니다. 지원하지 않는 컴파일러 확장은 prelude에서 무력화하지만, 파싱에 실패한 파일은 '분석 제한'에 표시되고 함수 위치만 추출합니다.
- 결정적 검사는 경로 민감 분석이 아니라 가드 조건·자료형 범위 기반입니다. 확정 판단은 대상 컴파일러 경고, 정적 분석 도구, 테스트로 검증하세요.
- 함수 포인터·다른 모듈(라이브러리) 내부 동작은 추적하지 않습니다.

> ⚠ LLM 단계에서는 사양서와 소스 코드가 Anthropic API로 전송됩니다. 사내 보안 정책을 먼저 확인하고, 필요하면 `--static-only` 또는 `--manual`(사내 승인 창구)을 사용하세요.

## 테스트

```bash
python3 -m unittest discover -s tests -v
```
