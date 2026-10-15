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

## Embedded C 변경 영향성 분석 (c_impact)

변경 전/후 C 코드(디렉터리, Git 커밋/작업 트리, 패치)를 libclang AST·CFG·Call Graph·Def-Use 로 분석해
변경이 전파되는 함수·변수·조건·출력과 근거 경로, 잠재 결함, 회귀 테스트 후보를 JSON/CSV/HTML/Graphviz 로 만듭니다.
자세한 내용은 [docs/impact_analyzer.md](docs/impact_analyzer.md) 를 보세요.

```bash
pip install -r requirements-impact.txt
python3 -m c_impact analyze \
  --base-dir examples/impact_demo/baseline --curr-dir examples/impact_demo/current \
  --compdb examples/impact_demo/compile_commands.json --compdb-root examples/impact_demo/baseline \
  --config examples/impact_demo/analysis_config.json -o impact_out
# → impact_out/impact_report.html, impact_result.json, impact_table.csv ...

python3 -m c_impact analyze --repo path/to/repo --base-rev HEAD --working-tree -o out   # 작업 트리 변경
python3 -m unittest discover -s tests -p "test_impact*.py"
```

## 테스트

```bash
python3 -m unittest discover -s tests -v
```
