[단계 4: 사양 공백 분석 — 정의되지 않은 조건 도출과 그 조건에서의 코드 동작]
이 시스템의 핵심 단계다. 사양서가 명시하지 않은 입력·상태·이벤트 조합을 체계적으로 도출하고,
그 조건에서 실제 코드가 어떻게 동작하는지 코드 근거로 추론하라.

도출 관점 (gap_kind)
- INPUT_COMBINATION: 정상·비정상 입력 조합
- INVALID_OR_OUT_OF_RANGE_INPUT: 무효값, 범위 초과, 신호 미수신
- BOUNDARY_VALUE: 최소·최대·경계값 (정확히 임계값인 경우 포함)
- TIMEOUT_TIMER_EXPIRY: 타임아웃, 타이머 만료·재시작
- COUNTER_LIMIT_WRAP: 카운터 상한 도달, 자료형 순환(예: 8bit 카운터의 255→0)과 그때의 판정 변화
- SIMULTANEOUS_CONDITIONS_PRIORITY: 여러 조건 동시 성립 시 우선순위
- UNEXPECTED_EVENT_DURING_TRANSITION: 상태 전이 도중/카운트 진행 중 예상하지 못한 이벤트 (IGN 변화, 리셋, 입력 무효화)
- INITIAL_OR_ABNORMAL_STATE: 초기 상태, 첫 주기, 비정상 상태에서의 동작
- RECOVERY_REENTRY: 고장 후 복구, 재진입, 재초기화(Wake-up 등) 조건

규칙
- 각 항목은 category=SPEC_GAP(사양이 정의하지 않음) 또는 AMBIGUOUS(정의가 모호함)로 쓴다.
- verdict는 SPEC_CLARIFICATION을 기본으로 한다. 그 조건에서 코드 동작이 명백히 위험(고장 오해제, 경고 미점등 등)하면
  POTENTIAL_RISK로 쓰되, 사양이 정의하지 않은 동작을 결함으로 확정하지 않는다.
- spec_question: 사양 작성자에게 보낼 구체적인 질문 (예/아니오 또는 값으로 답할 수 있게).
- risk_scenario: 그 조건이 실차에서 발생하는 시나리오와 영향.
- inferred_behavior: 그 조건에서 현재 코드가 하는 동작 (라인 근거, 실행 경로 포함). 사양 기대값은 "사양 미정의"로 둔다.
- 사양서 원문 근거(관련 요구사항 ID와 인용)를 req_ids/req_quote에 남긴다. 특정 요구사항에 속하지 않으면 관련 요구사항 전체를 적는다.
- 같은 공백을 중복 보고하지 않는다. 의미 없는 조합 나열 대신, 코드 동작이 달라지거나 위험이 생기는 조합을 우선한다.
- 테스트 케이스는 그 조건을 재현하는 절차로 쓰고, 기대 결과는 "사양 미정의 — 확인 필요 (현재 코드: …)"로 쓴다.
- <단계3_결과>에 이미 있는 불일치는 다시 쓰지 않는다.
