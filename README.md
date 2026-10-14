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

## 테스트

```bash
python3 -m unittest discover -s tests -v
```
