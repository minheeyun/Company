"""C 코드 변경 영향 분석(Change Impact Analysis) 패키지.

구성
  frontend  소스 수집, compile_commands.json, 전처리, pycparser 파싱, 타입 모델
  program   함수별 CFG/PDG(데이터·제어 의존성), 포인터(points-to) 분석, 호출 관계
  diff      변경 전후 AST 비교로 변경 지점(시드) 추출, 단일 변수 조건식의 차이 구간 계산
  slicer    변경 지점에서 시작하는 전방 슬라이싱(호출 문맥 구분)
  report    Markdown / JSON / Graphviz DOT 보고서
  sil       변경 전후 버전을 같은 시나리오로 실행해 변수·출력 차이를 비교하는 SIL 하네스
"""

__version__ = "0.1.0"
