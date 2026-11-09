"""분석 가정: 보고서의 '분석 가정' 항목. 결과 해석의 전제이므로 항상 함께 출력한다."""
from __future__ import annotations

from typing import List

from .model import NK, AnalysisGraph


def analysis_assumptions(go: AnalysisGraph, gn: AnalysisGraph, options) -> List[dict]:
    ext = sorted(set((go.meta.get("external_models") or {}).get("unmodeled", []))
                 | set((gn.meta.get("external_models") or {}).get("unmodeled", [])))
    cals = [gn.node(k).get("name") for k in gn.nodes_of(NK.GLOBAL) if gn.node(k).get("calibration")]
    mmio = [v.get("name") for v in (gn.meta.get("mmio") or {}).values()]
    b = gn.meta.get("build") or {}
    ph = (gn.meta.get("summary") or {}).get("parse_health") or {}
    items = [
        ("메모리 안전성", "배열 범위 밖 쓰기·잘못된 포인터 쓰기는 없다고 가정한다. 이런 쓰기는 임의 메모리를 오염시킬 수 "
                    "있으나 대부분의 정적 분석과 같이 분석 대상에서 제외한다(정적 검사 도구·런타임 보호로 별도 확인)."),
        ("정의되지 않은 동작", "부호 있는 정수 오버플로는 2의 보수 wrap으로, 0으로 나누기는 SMT 의미로 모델링한다."),
        ("빌드 설정", f"빌드 설정 '{b.get('name')}' 하나만 분석했다(정의 매크로: {', '.join(b.get('defines') or []) or '없음'}). "
                  f"비활성 #if 구간은 다른 설정으로 재분석해야 한다. 컴파일러 프로필: "
                  f"{', '.join(ph.get('compiler_profiles') or []) or '-'}"),
        ("포인터", "Andersen 포인터 분석(흐름·문맥 비민감, 필드 구분, 배열 원소 통합) + 포인터 매개변수 부작용은 호출 지점별로 해석."),
        ("함수 간 경로", "HRB 요약 간선과 문맥 민감 chopping으로 호출·복귀 짝이 맞는 경로만 근거로 사용한다."),
        ("동시성", "다른 실행 문맥 사이 공유 변수 값 전달은 간섭 의존 간선으로 포함한다(init 순서로 불가능한 흐름 제외). "
                "선점 시점·실행시간·캐시는 모델링하지 않으며, 보호·원자성은 검토 항목으로만 표시한다."),
        ("ISR·하드웨어 입력", "volatile 변수와 메모리 맵 레지스터(MMIO) 읽기는 읽을 때마다 새 입력으로 본다"
                         + (f"(레지스터: {', '.join(mmio)})" if mmio else "") + "."),
        ("캘리브레이션", "const(volatile)여도 상수로 접지 않고 A2L/모델 범위의 자유 입력으로 본다"
                    + (f"(대상: {', '.join(cals)})" if cals else "(지정된 캘리브레이션 없음)") + "."),
        ("소스 없는 함수", "라이브러리 모델이 있으면 그 읽기/쓰기 요약을, 없으면 'const가 아닌 포인터 인자에 쓰고 모든 인자가 "
                      "결과에 영향'으로 가정한다" + (f"(모델 없음: {', '.join(ext)})" if ext else "") + "."),
        ("AUTOSAR RTE", "Rte_IRead는 runnable 시작 시 복사본, Rte_IWrite는 종료 후 반영, Rte_Read/Write는 즉시 접근으로 모델링한다. "
                       "다른 SWC·ECU가 생산하는 신호는 단계마다 새 입력이다."),
        ("기호 실행 범위", f"반복 {options.loop_bound}회 전개, 인라인 {options.inline_depth}단계, 초기 상태 '{options.initial_state}'. "
                      "전개 범위를 넘는 실행은 검증하지 않았다."),
        ("도구 신뢰", "이 결과는 검토 보조용이다. 안전 관련 변경관리의 근거로 직접 쓰면 ISO 26262-8 소프트웨어 도구 신뢰도 "
                  "평가 대상이 될 수 있다."),
    ]
    return [{"topic": t, "text": x} for t, x in items]
