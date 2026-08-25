# WP2.2 작업 지시 — known-country 완전화 + fan-out 가드 (2026-08-25, Claude 발행)

## WP2.1 판정

- 형식 게이트(false merge 0 등)와 지시 이행은 인정한다. Turkey/Türkiye·Iran 재분류는
  의도대로다. Codex는 지시("c21에 없는 항목 무단 추가 금지")를 정확히 따랐다.
- 그러나 **전이 리포트가 명백한 오류 승격 4건을 드러냈다** — N100 진행 불가:
  - `idc_016b…`, `idc_3183…`, `idc_55b2…`: 프랑스 3개 프로젝트 ↔ 동일한 세네갈 다카르
    건물(`atz_bld_08d37161…`)이 각각 merge_candidate/eligible.
  - `idc_4142…`: Casa Martha(멕시코) ↔ Villa Manzanillo(에콰도르) merge_candidate.
- 근인 2가지: ① c21 `_KNOWN_COUNTRIES`가 레거시 코퍼스 범위라 **Senegal·Ecuador가
  unknown** → 중립 규칙 발화 → 실재하는 국가 모순 소실(발주 규칙의 전제 결함 —
  Claude 귀책). ② 국가 중립 시 **설계사+연도 2계열만으로 auto-merge-eligible**이
  성립(다작 설계사 문제). ③ 부수: 한 건물이 서로 무관한 상대 3건과 동시에
  eligible이 되는 fan-out을 아무 장치도 잡지 못함.

## 수정 지시 (계약 §3 개정 + 새 SHA pin)

1. **known-country 집합 완전화**: `canonical/vnext_country_norm.py`에 ISO 3166-1
   전 국가 목록(영문 정식명+통용명 alias)을 추가한다. c21 원본과의 diff는 manifest에
   `additions` 목록으로 전량 기록(이번 지시가 그 명시적 승인이다). Senegal, Ecuador가
   known이 되는지 테스트로 고정. 모호 중의어(Georgia류) 목록은 유지.
2. **auto-merge-eligible 강화**: 2계열 요건에 **anchor 계열 필수** 추가 — positive
   계열 중 최소 1개는 `이름·별칭 identity(strict_sim ≥90 계보)` 또는
   `검증된 pixel/image overlap`이어야 한다. 설계사+연도 조합만으로는 eligible 불가
   (merge_candidate 제안은 가능하되 eligible 플래그 불가).
3. **fan-out 가드 (hard-conflict 규칙 추가)**: 한 source building이 서로 candidate
   관계가 아닌 상대 건물 2개 이상과 동시에 eligible이면, 해당 eligible 전부를
   `defer`로 강등하고 `fanout_conflict` reason을 기록한다(상대들끼리 병합 후보인
   정당한 분할 케이스는 예외).

## 실행

1. `vnext_identity_candidates_v3` Stage A 재생(offline, 유료 0). v2 불변 보존·SHA pin.
2. v2→v3 전이 전수 리포트: 위 4건이 reject(또는 defer)로 복귀하는지 명시 확인.
   known-set 완전화로 상태가 바뀌는 쌍 전량 + fan-out 강등 전량 집계.
3. fresh disjoint N10: 층화에 **이번 4건 유형(신규 known 국가 모순) ≥2 + fan-out
   강등 쌍 ≥1 + anchor-미달 강등 쌍 ≥1** 포함. 프로토콜·게이트는 동일(§8, lane C).
4. 독립 validator·테스트·job card·SHA pin. 실패 시 정지.

## N100 예고 (v3 PASS 후 Claude가 확정 발행)

- 구성 초안: 정밀도 코어(모집단 비례 merge_candidate ~70쌍, 게이트: false merge 0,
  2인 블라인드+조정) + 재현율 진단 스트라텀(게이트 아님, 보고만): 연도 게이트 차단
  ~10 / pHash veto 차단 ~10 / 고유사 이름 reject ~10 — WP2.1이 보고한 false-reject
  신호 3건 포함. n<299이므로 99% 통계 주장은 하지 않으며 v1 병합은 전건 사람 승인
  유지(기존 결정).
- HTTP 단계는 처음부터 network 허용 경로로 실행(WinError 10013 재발 방지).
