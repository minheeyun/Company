"""AST / CFG 기반 Embedded C 변경 영향성 분석기.

단계별 모듈:
  1단계 ast_extract / project   : libclang AST → 함수·변수·문장 모델, 심볼 ID
  2단계 change_detect / textdiff: 변경점 탐지 및 분류
  3단계 callgraph / dataflow     : Call Graph, Def-Use, 별칭
  4단계 cfg / valuerange         : CFG, 경로·도달성 비교
  5단계 impact                   : 통합 의존 그래프 기반 영향 전파
  6단계 defects / testgen / report_*: 잠재 결함, 회귀 테스트, JSON/CSV/HTML/DOT 출력
"""

__version__ = "0.1.0"
