"""C 코드 변경 영향 분석(Change Impact Analysis) 패키지.

구성
  frontend  소스 수집, compile_commands.json, 전처리, pycparser 파싱, 타입 모델
  program   함수별 CFG/PDG(데이터·제어 의존성), 포인터(points-to) 분석, 호출 관계
  diff      변경 전후 AST 비교로 변경 지점(시드) 추출, 단일 변수 조건식의 차이 구간 계산
  slicer    변경 지점에서 시작하는 전방 슬라이싱(호출 문맥 구분)
  taint     위험 연산 도달 분석: 변경 영향이 배열 인덱스·분모·시프트·복사 길이·좁아지는 변환 등에 닿는지
  cpg       AST·CFG·CDG·DDG·호출을 묶은 CPG 질의·내보내기, 외부 입력(Source) Taint 전파
  symexec   위험 연산까지의 경로를 z3 로 풀어 안전 속성 위반 입력(반례) 탐색
  security  변경 기반 보안 취약점 파이프라인 (CPG + Taint + 기호 실행 + 변경 전후 비교 + CWE)
  rules     Source·Sink·보안 검사 규칙 (config/security_rules.yaml)
  external  Joern·CodeQL 질의, KLEE 하네스 생성·실행
  report    Markdown / JSON / Graphviz DOT 보고서
  sil       변경 전후 버전을 같은 시나리오로 실행해 변수·출력 차이를 비교하는 SIL 하네스
"""

__version__ = "0.1.0"
