"""VFIA - Vehicle Feature Impact Analyzer.

두 스크립트(기능 분류기, 변경 영향 분석기)가 공유하는 C 코드 분석 그래프와
분석 알고리즘을 제공한다.

모듈 구성
---------
config.py            분석 설정(버전별 소스·빌드 설정, 차량 기능 모델) 로딩
model.py             노드/간선 종류, AnalysisGraph, SQLite 저장
clang_api.py         libclang 보조 기능(연산자 종류, 상수 평가, 비활성 #ifdef 구간)
project_indexer.py   compile_commands 기반 번역 단위 파싱, 심벌·AST·매크로 추출
cfg.py               함수별 CFG, 후지배자, 제어 의존성
dataflow.py          worklist 기반 Reaching Definitions, Def-Use
pointsto.py          Andersen 방식 points-to(흐름·문맥 비민감)
graph_builder.py     SDG/CPG 통합 그래프 생성(데이터·제어·함수 간·전역·신호 간선)
vehicle_context.py   기능·요구사항·관찰 대상·태스크·신호·ARXML 연결
feature_mapper.py    기능 분류기(키워드·유사도·군집화·시드 전파·후방 슬라이싱)
ast_diff.py          GumTree 계열 AST 매칭과 편집 스크립트
change_detector.py   변경 전후 요소 대응, 변경 유형 분류, 그래프 앵커 연결
impact_analyzer.py   2단계(HRB) 전방/후방 슬라이싱, 기능별 영향 경로
symexec.py           C 정수 의미를 반영한 z3 기반 기호 실행(유계)
behavior_checker.py  분기 XOR·출력 차이·구간 계산, 전후 비교 실행
report_generator.py  JSON/HTML 보고서
"""

__version__ = "0.1.0"
