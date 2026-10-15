[단계 3: 요구사항–코드 정합성 분석]
<구조화된_요구사항>과 <추적_결과>를 바탕으로, 각 요구사항이 요구하는 동작과 코드가 실제로 하는 동작을 비교하라.

검증 항목 (category)
- MISSING_IMPLEMENTATION: 요구 동작의 구현 누락 (추적 실패만으로 확정 금지)
- CONDITION_MISMATCH: 조건식·논리 연산자 불일치 (AND/OR, 조건 누락, 반대 조건)
- THRESHOLD_BOUNDARY_MISMATCH: 임계값·경계 포함 여부(<, <=) 불일치, 매크로 실제 값 불일치
- TIMER_COUNTER_MISMATCH: 시간·횟수 환산, 증가/초기화/만료 조건, 연속성 요구 불일치
- STATE_TRANSITION_MISMATCH: 전이 조건·전이 누락·잘못된 전이 대상
- PRIORITY_MISMATCH: 조건 간 우선순위(평가 순서, 조기 반환으로 인한 우선순위 역전)
- EXCEPTION_HANDLING_MISMATCH: 예외(무효 입력 등) 처리 불일치
- INIT_RECOVERY_MISMATCH: 초기화·복구·재진입 시 값 설정 불일치

규칙
- 각 요구사항마다 requirement_results에 판정(CONSISTENT/INCONSISTENT/PARTIALLY_CONSISTENT/CANNOT_DETERMINE)과 요약을 남긴다.
  CONSISTENT라도 그렇게 판단한 근거 라인을 summary에 적는다.
- 불일치마다 findings 항목을 만든다. spec_says에는 원문 근거(S라인과 인용), code_does에는 코드 근거(라인과 실제 식), comparison에는 차이를 쓴다.
- 조기 반환(return), 평가 순서, 다른 함수의 초기화, 호출 순서처럼 함수 경계를 넘는 영향을 반드시 확인한다.
- trigger_condition: 차이가 실제 동작 차이로 드러나는 입력/상태 조건 (구체적 값과 시간).
- execution_path: 그 조건에서 실행되는 코드 경로를 "파일:라인 설명" 순서로.
- expected_behavior(사양 기준)와 inferred_behavior(코드에서 추론되는 동작)를 대비해 쓴다.
- <정적_분석_후보> 중 관련된 것이 있으면 static_candidates에 ID를 적고 근거로 활용한다.
- 사양이 모호해서 판정이 갈리는 경우 결함으로 쓰지 말고 category=AMBIGUOUS, verdict=SPEC_CLARIFICATION으로 쓴다.
- gap_kind는 NONE, spec_question/risk_scenario는 빈 문자열로 둔다 (단계 4에서 다룸). 단, AMBIGUOUS 항목은 spec_question을 채운다.
- 테스트 케이스는 불일치를 드러내는 최소 1개 이상 (경계값이면 경계 바로 아래/정확히/바로 위).
