# WP2.9 실행 기록 — 2026-09-07

- 지시서: `20260827_vnext_wp29_s1_registry_order.md`; 사용자 실행 승인에 따라 로컬 S1 병합만 수행.
- 순서: Stage A 티어·component 안전 검사 → Stage B registry·이월·crosswalk·독립 검증 → Stage C 100쌍 감사 데이터.
- 사전 선언: `data/reports/vnext_identity_registry_v1_predeclared.json`. 규칙·표본 방법·사용자 감사 기준은 결과 확인 후 변경하지 않는다.
- 비용: 유료 API 0, LLM 호출 0, $0. Neon/R2 쓰기 및 canonical_v2 테이블 접근 0.
- 초기 읽기 확인: v4 68,049쌍, merge_candidate 5,100쌍, 규칙 일치 1,534쌍. v4/WP0a manifest/WP0b snapshot SHA 일치.
- 리뷰 앱은 읽기만 수행하고 SHA로 불변을 검증한다. 사용자 판정은 비워 둔다.
- 최종 결과: **FAIL — Stage B 사전 연결 점검에서 정지**.
- Stage A PASS: 최종 티어 1,534쌍 / 안전 component 1,524개(크기 2: 1,514개, 크기 3: 10개). 제외 component·건물 0.
- 7개 경계/충돌 테스트와 실제 후보 N=10 → N=100 스모크는 통과했다.
- 이후 메모리 SQLite의 read-only ATTACH 통합 점검에서 `OperationalError: unable to open database` 발생. producer 출력 연결의 `uri=True` 누락이 원인.
- 지시대로 자동 수정·재시도 없이 정지. 실제 Stage B·C 미시작; S1 DB, 발급 ID, 병합, 이월/crosswalk, 감사 데이터 생성 0.
- 앱 SHA 불변. 사용자 감사 라벨 0. 코드 초안은 완료 산출물이 아니다.
- 전체 FAIL 리포트: `data/reports/vnext_identity_registry_v1_report.md` 및 `_FAIL.json`.
- Stage A 전량 기록: `data/reports/vnext_identity_registry_v1_stage_a.json`.
- 유료 API 0, LLM 호출 0, 비용 $0; CDN 요청 0; Neon/R2 쓰기 0, canonical_v2 테이블 접근 0.
