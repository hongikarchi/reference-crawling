# WP2.9-R 작업 지시 — URI 결함 수정 후 재개 (2026-09-07, Claude 발행)

전제: WP2.9 FAIL 리포트(`vnext_identity_registry_v1_report.md`). 정지는 지시 준수였고
수리한다. Stage A 결과(티어 1,534쌍 / component 1,524개 / 제외 0)는 유효하며 재사용한다.
사전 선언 규칙·감사 기준·티어 정의는 **변경 금지**. Neon/R2 쓰기 0, 유료 API 0.

## 수정 1 — 연결 결함 (확인된 근인)

`canonical/vnext_identity_registry.py:356`이 출력 DB를 `uri=True` 없이 열어, 그 연결의
`ATTACH 'file:...?mode=ro&immutable=1'`이 URI로 해석되지 않았다(입력 연결 :72는 정상).

- 출력 연결을 URI 모드로 통일하고, **연결 통합 스모크**(출력 연결 + 실제 read-only
  ATTACH 왕복)를 테스트로 추가한다. FAIL 리포트가 요구한 그대로다.
- 같은 파일 안의 모든 connect/ATTACH 경로를 일괄 점검해 동일 결함이 없는지 확인한다.

## 수정 2 — 크기 3 component 10개 해명 (Stage B 전 필수)

티어는 무플래그 쌍만 포함하는데 크기 3 component가 존재한다는 것은, 한 건물이 상대
소스 2개 건물과 동시에 티어 병합된다는 뜻이다. fanout 가드가 왜 침묵했는지 설명하라.

1. 10개 component 각각에 대해: 공유 건물, 두 상대 건물, 각 쌍의 증거 요약, 두 쌍 모두
   플래그가 없는 **정확한 이유**(예: fanout 판정이 eligible 쌍만 대상이었는지)를 보고.
2. 예상 가설: 상대 소스의 within-source 중복(같은 건물이 provisional 레코드 2개) —
   이 경우 3자 병합이 올바르다. 가설과 다른 형태(서로 다른 두 건물과의 병합)가 하나라도
   있으면 해당 component를 티어에서 제외하고 사유를 기록한다.
3. **10개 component의 쌍 전부(20쌍)를 사용자 감사 100쌍 표본에 필수 포함**한다
   (층화 표본의 일부로, 잔여 80쌍은 기존 층화대로).

## 재개

- 새 실행 provenance(run id) 고정 후 Stage B → Stage C를 지시서
  `20260827_vnext_wp29_s1_registry_order.md` 그대로 실행한다.
- Stage A 산출물은 SHA pin으로 결박해 재사용한다(재계산 시 동일 결과 검증 1회).
- 독립 validator·job card·SHA pin. 실패 시 정지 후 FAIL 리포트.
