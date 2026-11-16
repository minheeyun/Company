# 내장(벤더링) 라이브러리

보고서(`impact.html`)를 인터넷 없이 열 수 있도록 그래프 라이브러리를 HTML 파일 안에 그대로 넣는다.

| 파일 | 라이브러리 | 버전 | 라이선스 | 출처 |
|---|---|---|---|---|
| `cytoscape.min.js` | Cytoscape.js | 3.34.4 | MIT (`cytoscape.LICENSE`) | npm `cytoscape@3.34.4` `dist/cytoscape.min.js` |

npm 무결성 값(sha512): `msY99tMY8iWTg+sIPGfSqDcPNOt4xcj+v/K/7qUFaCUxxlqnMkUYLBjRUzn9W5CU/R2hrmtJa5+UtovrEM2ctg==`
(패키지 tarball 기준). 버전을 바꿀 때는 이 표와 `vfia/impact_graph.py`의 `CYTOSCAPE_VERSION`을 함께 고친다.
