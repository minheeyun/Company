[단계 5: 코드 잠재 결함 및 취약점 분석]
<정적_분석_후보>를 하나씩 판정하고, 후보에 없는 결함도 의미 분석으로 찾아라.

검토 대상
- 런타임 오류: 정수 오버플로/언더플로, 0으로 나누기, 배열 인덱스 범위 초과, NULL 역참조, 미초기화 변수, 형변환 값 손실
- 기능·논리 오류: 카운터 순환에 따른 잘못된 상태 판정, 타이머 초기화·증가·만료 오류, 조건식 누락·잘못된 연산자,
  상태 전이 누락·도달 불가 상태, 오류 상태에서의 잘못된 복구, 변수 갱신 순서에 따른 기능 오류
- 동시성: 태스크·ISR 간 공유변수, 비원자적 읽기-수정-쓰기, 보호 메커니즘 부재, 실행 순서 의존 동작

후보 판정 (candidate_reviews, 모든 후보에 대해 작성)
- CONFIRMED: 후보의 근거(CERTAIN 계산/도구 경고)와 코드 맥락으로 결함이 성립
- PROBABLE / POTENTIAL: 발생 조건이 사양·환경상 가능함 (조건을 reason에 적음)
- LIKELY_FALSE_POSITIVE: 코드 맥락상 발생하지 않음 (막아 주는 코드 라인을 reason에 적음)
- NEEDS_INFO: 판단에 필요한 정보가 없음 (무엇이 필요한지 reason에 적음)
- 결함으로 보고할 후보는 findings에 항목을 만들고 finding_id로 연결한다. 같은 원인의 후보 여러 개는 한 finding으로 묶는다.

finding 작성 규칙
- category는 RUNTIME_ERROR / LOGIC_ERROR / CONCURRENCY 중 하나. static_candidates에 관련 후보 ID를 모두 적는다.
- 결함이 사양 동작에 어떤 영향을 주는지 req_ids/req_quote로 연결한다 (예: 카운터 순환 → 고장 상태 오해제 → SWR-xxx 위반).
- trigger_condition과 execution_path로 결함이 드러나는 구체적 조건(입력 값, 지속 시간, 인터럽트 시점)을 보인다.
- 확정할 수 없으면 POTENTIAL_RISK/NEEDS_REVIEW로 두고, 확정에 필요한 정보(태스크 주기, 우선순위, 컴파일러, 하드웨어 범위)를 additional_checks에 적는다.
- <분석_환경>에 실행 컨텍스트 정보가 없으면 동시성 결함은 NEEDS_REVIEW로만 쓴다.
- <단계3_결과>, <단계4_결과>와 같은 문제를 다시 쓰지 말고, 코드 결함 관점에서 새로 드러나는 것만 쓴다. 관련 항목이 있으면 recommendation에 그 ID를 언급한다.
- gap_kind는 NONE, spec_question/risk_scenario는 빈 문자열로 둔다.
