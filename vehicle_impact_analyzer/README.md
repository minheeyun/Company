# VFIA — 차량 기능 분류 · 변경 영향 분석기

목표는 두 단계다.

1. **"이 변경이 이 차량 기능에 영향을 줄 가능성이 있고, 근거는 이 경로다"를 재현 가능하게 보여준다.**
   (문맥 민감 chopping으로 찾은 실현 가능한 의존 경로 + 실행 문맥·신호·태스크까지 연결)
2. 그 위에 **입력 조건과 출력 차이 분석**을 더해 **"어떤 조건에서 차량 기능이 어떻게 달라지는가"**를 보인다.
   (결합 프로그램 기반 차이 조건 — 예: `request_pct ≥ 81 ∧ 30 ≤ speed_kph ≤ 39 ∧ sensor_ok = 1 일 때
   토크 명령이 변경 전 80 → 변경 후 request_pct (+1 ~ +20)` — 정확성은 SMT로 검증해 '정확/부분'을 표시)

같은 **코드 분석 그래프(SDG/CPG)** 를 공유하는 스크립트들:

| 스크립트 | 하는 일 |
|---|---|
| `classify_features.py` | 파일·함수·변수와 차량 기능의 연결(역할·근거·검토 상태) |
| `analyze_impact.py` | 변경 전후 비교 → 영향 경로 → 기능별 상태 → 차이 조건·출력 변화 (`--level function`: 2.5단계 MVP만) |
| `evaluate.py` | 정답셋 대비 정밀도·재현율·후보 수·시간·분석 범위, 기준선(baseline) 비교 |
| `tools/make_compile_db.py` | 빌드 로그(TASKING/GHS/Diab/HighTec/gcc) → compile_commands.json |

## 설치

```bash
pip install -r requirements.txt
```

libclang(휠 또는 `VFIA_LIBCLANG`), z3, networkx, PyYAML. 전후 비교 실행은 호스트 `gcc`, CBMC 기준선은
`goto-cc`/`goto-diff`(선택)가 있으면 사용한다.

## 빠른 시작(예제 ECU)

```bash
python classify_features.py --config examples/torque_ecu/analysis.yaml --version after
```

```bash
python analyze_impact.py --config examples/torque_ecu/analysis.yaml
```

```bash
python evaluate.py --config examples/torque_ecu/analysis.yaml --impact examples/torque_ecu/out/impact.json
```

```bash
python -m pytest -q tests
```

## 검토 의견 반영 내역

| 검토 항목 | 반영 | 위치 |
|---|---|---|
| ① 전방∩후방 교집합은 함수 경계에서 부정확 | Reps–Rosay 방식 **문맥 민감 chopping**(2단계 자동자 + 요약 간선 내부 same-level chop). 단순 교집합은 비교용으로 크기·제외 노드 수만 보고 | `slicing.chop`, 보고서 3장 |
| ② XOR은 특수한 경우 | 변경 전/후를 같은 입력·초기 상태·환경 입력으로 함께 실행하는 **결합 프로그램**에서 관찰값 차이 D를 풀고, 분기 차이도 각 버전의 실제 조건값·**도달 조건**으로 비교 | `behavior_checker`, `conditions` |
| ③ 예제를 결론까지 | D를 **입력 상자의 합집합**으로 도출하고 SMT로 '정확/부분'을 검증. 상자별 출력 변화(전/후 형태, 변화량, 방향)와 **기능 수준 문장** 생성. 예제 MISRA 10.3 위반 수정 | `conditions.derive_boxes/explain_box`, 예제 |
| ④ 후지배자 CD는 단일 종료점 전제 | **비종료 민감 제어 의존(NTSCD)** 기본 적용(대기 루프·태스크 무한 루프). 후지배자 방식은 `control_dependence: classic` | `cfg.ntscd` |
| ⑤ 라벨 전파 구분, Louvain 재현성 | Zhu 등의 **harmonic function을 기능별 one-vs-rest**로 적용해 점수 사용(다중 라벨). Louvain seed·resolution 고정·기록 | `feature_mapper` |
| ⑥ Joern/GumTree 파서 차이 | 변경 검출과 의존 분석이 **같은 Clang AST**를 쓰므로 AST 간 대응 문제가 없음. 이름 변경은 갱신으로 보고, 함수 이름 변경만 구조 유사도로 별도 해석 | `ast_diff`, `change_detector` |
| ⑦ 사소한 것 | 텍스트 줄 대응 대신 AST 대응을 쓰는 이유를 '이동·재포맷·블록 재구성'으로 정정. 지역변수 추가 시 **스택 증가** 보고 | `change_detector`, `resources` |
| 2-① 비Clang 빌드 환경 | TASKING/GHS/Diab/HighTec 프로필: 옵션 변환, 사전 정의 매크로, 대리 target(ILP32), `-ffreestanding`, `-nostdinc`+툴체인 헤더, 확장 키워드·intrinsic **호환 헤더**, **빌드 로그 파서**. **파싱 성공률·오류 수·누락 의심 함수·부분 AST 함수**를 단계 2 완료 기준으로 보고(`--strict`) | `build_env`, `project_indexer` |
| 2-② 전처리 전/후 연결 | 텍스트 diff 헝크를 AST 변경·매크로 전개 지점에 대응. **비활성 #if 구간 안 변경**은 별도 변경으로 기록. RTE 함수형 매크로는 전개 문장을 신호 접근점으로 연결 | `change_detector._text_coverage`, `vehicle_context` |
| 2-③ 번역 단위 간 심벌 통합 | 외부 링크는 이름으로 통합, static은 파일별 분리, 선언·정의 자료형 불일치 보고, static↔extern 변경 대응 | `ProjectIndex.symbol_integration` |
| 2-④ 함수 간 알고리즘·슬라이스 크기 | HRB SDG + 요약 간선, Andersen + 매개변수 부작용의 호출 지점별 해석, 함수 포인터 반복 보강, **구간 추상 해석(widening/narrowing)**. 영향 후보 **순위**(가중 의존 거리·경로 수) | `graph_builder`, `pointsto`, `intervals`, `impact_analyzer` |
| 2-⑤ 동시성을 그래프에 | **간섭 의존 간선**(태스크·ISR·runnable 사이). OS 설정으로 거름(init 순서), 선점 가능성·임계 구역(잠금 API)·원자 폭 기록. **AUTOSAR 암묵/명시 접근** 구분(IRead 시작 시 복사본, IWrite 종료 후 반영) | `vehicle_context._interference`, `symexec` |
| 2-⑥ 임베디드 함정 | **캘리브레이션**(A2L)을 상수가 아닌 자유 입력으로, **MMIO 레지스터**를 이름 있는 입력·관찰점으로, **소스 없는 함수 요약 모델**(memcpy 등 + 프로젝트 스텁), **분석 가정** 항목 | `vehicle_context.parse_a2l`, `graph_builder`, `assumptions` |
| 2-⑦ 변경 유형 추가 | 정수 부호·크기, const/volatile, static↔extern, 전역 초기값(시동 직후), switch/case/fall-through, 배열 크기·인덱스, 비기능(스택·RAM·코드 크기, 태스크 최악 호출 체인 스택) | `change_detector`, `resources` |
| 2-⑧ 기존 도구 | **CBMC goto-diff `--forward-impact`** 기준선 자동 실행(설치 시), Frama-C Impact는 수동 비교 안내 | `evaluate.py` |
| 2-⑨ 기능 위치 찾기 | **Software Reconnaissance**(기능 실행/비실행 시나리오 추적 차이), IR 추적성(TF-IDF), **공변경 마이닝**(git 또는 JSON) | `feature_mapper`, `cochange` |
| 2-⑩ 평가 설계 | 정답셋, 기능·함수 단위 P/R/F1, 변경당 후보 수, 동작 판정 정확도, 분석 시간, 분석 범위(파싱 실패·미해결 간접 호출·모델 없는 외부 함수) | `evaluate.py` |
| 3. 단계 조정(2.5단계) | **함수 단위 간이 영향 분석 MVP**(호출 그래프 + 전역 읽기/쓰기 + 함수 요약)와 호출 그래프만 쓴 기준선 | `mvp_impact`, `--level function` |

## 입력

| 입력 | 설정 | 용도 |
|---|---|---|
| 소스(버전별) | `versions.<v>.root` | `.c/.h`, 생성 코드 |
| compile DB | `versions.<v>.compile_commands` | 번역 단위별 옵션. 없으면 `build.build_log`(빌드 로그) → 없으면 `sources`/`args` 합성 |
| 컴파일러 프로필 | `build.compiler`, `build.target`, `build.system_includes`, `build.predefined`, `build.compat_defines` | 비Clang 컴파일러 재현 |
| 빌드 설정 | `build.name`, `build.extra_args`, `build.variant_filter` | 차종별 `#ifdef`(한 실행 = 한 설정) |
| 차량 모델 | `vehicle_model`(여러 파일 병합, 버전별 재정의 가능) | 기능·요구사항·관찰 대상·실행 문맥(`tasks/isrs/runnables/callbacks/inits`)·신호·`a2l`·`arxml`·`os` |
| 관찰 대상 | `observations[].kind` = `function_return`/`global`/`signal`/`mmio`/`statement`/`function` | `function`+`params`(인자 정의역)+`inputs`(환경 입력 정의역)+`sequence`(주기 수)를 주면 동작 검증 |
| 라이브러리 모델 | `analysis.library_models` | 소스 없는 함수의 읽기/쓰기 인자 요약 |
| 레지스터 이름 | `analysis.mmio_map` | 주소 → 이름 |
| 실행 추적 | `traces` | Software Reconnaissance(`{"scenario", "features": [...], "functions": [...]}`) |
| 변경 이력 | `history: {git: 경로, subdir}` 또는 `{file: json}` | 공변경 규칙 |
| 정답셋 | `ground_truth` | 평가 |
| 검토 기록 | `review_file` | 기능 연결 확정/기각 유지 |

예제(`examples/torque_ecu`)의 `vehicle_model.yaml`, `execution_v*.yaml`, `torque_ecu.a2l`, `swc_torque.arxml`,
`traces/`, `change_history.json`, `ground_truth.yaml`을 참고. 추적·이력·정답셋은 **설명용 예시 데이터**다.

## 결과 상태

| 상태 | 의미 |
|---|---|
| 정적 영향 후보 | 문맥 민감 chop으로 변경점 → 관찰 대상의 실현 가능한 경로가 있음 |
| 모델에서 동작 차이 확인 | 결합 프로그램에서 관찰값이 달라지는 입력이 있음(조건·증거 입력 제시) |
| 모델에서 동작 차이 미발견(검증 범위 내) | 정의역·전개 범위·주기 수 안에서 관찰값 동일 |
| 실행으로 차이 확인 / 실행 비교에서 차이 미발견 | 두 버전 빌드를 같은 입력으로 실행한 결과 |
| 분석 불완전으로 판정 유보 | 경로를 숨길 수 있는 미해결 영역, 지원하지 않는 구문 |
| 현재 분석 범위에서 영향 경로 미발견 | '영향 없음'이 아님 |

동작 검증 결과는 변경별로 **분리 검증**한다: 값·연산자·리터럴 갱신만으로 된 변경은 그 AST 갱신만 변경 전
프로그램에 적용(`granularity: change`), 그 밖에는 함수 단위로 적용(`function`, 같은 함수의 다른 변경도 함께
적용됨을 표시). 관찰 함수 실행 범위 밖의 변경은 '모델 범위 밖'으로 표시하고, `Task_10ms` 같은 상위 진입
함수 + 신호/레지스터 관찰(통합 관찰)로 범위를 넓힌다.

## 예제 결과(요약)

- 임계값 30→40: `request_pct ≥ 81 ∧ 30 ≤ speed_kph ≤ 39 ∧ sensor_ok = 1` 일 때 토크 명령 80 → `request_pct`
  (+1 ~ +20), 정확·검증됨, 실행 비교로 확인.
- 디바운스 `>`→`>=`: 연속 3회 고장 입력 창(정확한 3개 상자)에서 고장 확정이 한 주기 빨라짐.
- `last_limit` 기록 삭제: `Cal_TqPlausMax ≤ 99`(캘리브레이션) 일 때만 타당성 DTC가 달라짐 — 상수로 접었다면
  '차이 없음'으로 잘못 판단했을 경우.
- `Tq_Min` 리팩터링: 분리 검증에서 동치(차이 없음). `SPD_MAX_KPH` 300→280: 차속 처리에는 영향, 3주기 통합
  관찰 범위에서는 토크 신호 차이 없음(필터 지연으로 280에 도달하지 못함 — 범위 표시).
- 평가(예제 정답셋 7건): 기능 단위 F1 — 문맥 민감 chop 0.83, +동작 검증 0.86, MVP 0.65, 호출 그래프만 0.42,
  공변경만 0.36. 전체 변경 기준 함수 단위: CBMC goto-diff P 1.0/R 0.46, chop P 0.80/R 0.92.

## 모듈

| 모듈 | 책임 |
|---|---|
| `build_env` | 컴파일러 프로필·옵션 변환·호환 헤더·빌드 로그 파서·소스 텍스트 함수 정의 탐지 |
| `project_indexer` | 번역 단위 파싱(캐시), 심벌 키, 일반화 AST, 매크로·비활성 구간, 파싱 건전성, 심벌 통합 |
| `cfg` / `dataflow` / `intervals` | CFG, NTSCD/후지배자 CD, Reaching Definitions, 구간 추상 해석 |
| `pointsto` / `graph_builder` | Andersen, SDG(HRB 요약), 전역 위치, MMIO, 라이브러리 모델 |
| `vehicle_context` | 기능·관찰·실행 문맥·신호(RTE 함수/매크로)·A2L·ARXML·임계 구역·간섭 의존 |
| `feature_mapper` / `cochange` | 기능 분류(키워드·TF-IDF·Louvain·harmonic·후방 슬라이스·Reconnaissance·공변경) |
| `ast_diff` / `change_detector` | GumTree 계열 대응, 변경 유형, 텍스트 대응, 자원 차이, 분리 검증용 AST 갱신 |
| `slicing` / `impact_analyzer` | HRB 슬라이싱, 문맥 민감 chop, 순위, 실행 문맥·공유 상태 |
| `symexec` / `conditions` / `behavior_checker` | 유계 기호 실행(C 정수 의미, RTE, 캘리브레이션, MMIO), 차이 조건·설명, 분기 차이별 시험 입력, 전후 비교 실행 |
| `mvp_impact` / `resources` / `assumptions` / `report_generator` | 2.5단계 MVP, 비기능 추정, 분석 가정, JSON/HTML |

## 기존 도구와의 관계(차별점)

| 도구 | 하는 일 | 이 도구와의 관계 |
|---|---|---|
| Frama-C Impact (Eva 기반) | 문장의 부작용이 영향을 주는 문장 집합(없음을 보장) | impact_analyzer와 같은 층위. 수동 비교 기준선 |
| CBMC goto-diff | diff에 전방·후방 의존을 붙여 출력 | `evaluate.py`가 설치 시 자동 기준선으로 실행 |
| CodeSurfer | 상용 SDG 슬라이서 | 같은 HRB 계열 |
| Shadow symbolic execution / SymDiff / Equivalence-refined CIA | 두 버전 함께 실행·등가성·중간값만 다른 경우 거르기 | 결합 프로그램 기반 차이 조건·분리 검증으로 같은 문제를 다룸 |

차별점은 ① 영향 경로를 **차량 기능·신호·태스크(간섭 의존·RTE 의미)** 까지 연결, ② **Clang이 아닌 임베디드
빌드 환경**에서 재현(파싱 건전성 보고), ③ 기능 단위로 **어떤 조건에서 어떻게 달라지는지**를 검증된 조건과
문장으로 도출하는 데 있다.

## 개발 단계(조정안)

| 단계 | 내용 | 이 저장소 |
|---|---|---|
| 1 | 평가 지표·정답셋(과거 변경 10~20건 라벨링) 확정 | `evaluate.py`, 예제 정답셋 형식 |
| 2 | 비Clang 컴파일러 재현, 파싱 성공률을 완료 기준으로 | `build_env`, `--strict` |
| 2.5 | 함수 단위 간이 영향 분석 MVP | `--level function` |
| 3 | 기능 분류기(4와 병행 가능) | `classify_features.py` |
| 4 | AST 변경 + 전처리 전/후 대응, 매크로 확장 지점 | `change_detector` |
| 5 | 종료점 없는 루프를 처리한 CDG | NTSCD |
| 6 | 간섭 의존, 캘리브레이션·MMIO·라이브러리 모델 | `vehicle_context`, `graph_builder` |
| 7 | 문맥 민감 chopping으로 기능 연결 | `slicing.chop` |
| 8 | 결합 프로그램 기반 차이 조건, '분석 가정' | `conditions`, `assumptions` |

## 한계

- **프런트엔드**: libclang 바인딩 기반 프로토타입. 대리 target은 데이터 모델만 맞추며 아키텍처 고유 의미(정렬,
  intrinsic 의미)는 근사다. LibTooling 추출기는 같은 일반화 AST(JSON)를 내면 교체 가능.
- **포인터**: 흐름·문맥 비민감 Andersen(매개변수 부작용만 문맥별). 대규모 코드는 SVF 연동 권장. IFDS/IDE는
  미구현(SDG 요약 간선으로 대체).
- **기호 실행**: 정수·_Bool·enum·구조체 필드·정수 배열·함수 포인터·RTE·MMIO 지원. 부동소수점·goto·동적 메모리는
  판정 유보. 반복 전개·인라인 깊이·주기 수 범위 밖은 검증하지 않음. 차이 조건 도출은 시간 예산이 있어 입력이
  많고 넓으면 '부분'으로 표시.
- **동시성**: 값 전달(간섭 의존)과 보호·원자성은 다루지만 선점 시점·실행시간(WCET)은 모델링하지 않음.
- **도구 신뢰**: 결과는 검토 보조용이다. 안전 관련 변경관리의 근거로 직접 쓰면 ISO 26262-8 소프트웨어 도구 신뢰도
  평가 대상이 될 수 있다.
