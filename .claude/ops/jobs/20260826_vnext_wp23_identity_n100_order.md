# WP2.3 작업 지시 — identity N100 (2026-08-26, Claude 발행)

## WP2.2 판정

- **PASS 인정.** Claude 독립 확인: gate json(gold same 7/different 2/insufficient 1,
  false merge 0, 불일치 0), 지정 4쌍 reject 복귀, matcher 2파일의 EOL 외 변경 0
  (`--ignore-cr-at-eol` diff로 검증). fan-out 223 defer·anchor 289 차단은 의도된
  보수화다.
- WP2.2가 보고한 진단 신호를 이번 설계에 반영: fan-out 과잉보수 3쌍(실제 동일),
  pHash/year gate false-reject 2쌍.

## N100 구성 (표본은 전부 seed 고정·리뷰 전 동결·기리뷰 쌍 전량 제외)

### A. 정밀도 코어 — 70쌍, 게이트 대상

- 모집단: v3 merge_candidate(eligible) 전체. anchor 유형(이름/이미지/양쪽)과 부수
  family 조합의 **실분포 비례 층화** — 알려진 실패 유형 과표집 금지.
- 리뷰: 계약 §8 N100 프로토콜 — **격리 블라인드 리뷰어 2인 독립 → ledger 동결 →
  조정자 재판정**, 제안 상태·점수 은닉, gold 4어휘. 미해결 불일치는 삭제 아닌
  `insufficient`로 계상.
- **게이트**: 표본 내 merge_candidate 중 조정 결과 `different` 1건이라도 → FAIL.
  hard conflict 위반 0. 리뷰 완결 100%(unreviewable 0).
- 보고: 정밀도 + Clopper-Pearson 95% CI. **n=70으로 99% 주장 금지** 명문화 —
  v1 병합 전건 사람 승인은 이 결과와 무관하게 유지(기존 결정).

### B. 재현율·보수성 진단 — 40쌍, 게이트 아님(보고 전용)

| 스트라텀 | n | 내용 |
|---|---|---|
| B1 year-gate 차단 | 10 | 다른 증거는 강한데 연도 게이트가 reject시킨 쌍 (기보고 신호 포함) |
| B2 pHash-veto 차단 | 10 | 다른 증거는 강한데 pHash veto가 차단한 쌍 (기보고 신호 포함) |
| B3 fan-out defer | 10 | WP2.2에서 defer된 223쌍 중 표집 — 실제 동일 3쌍 필수 포함 |
| B4 고유사 이름 reject | 10 | strict_sim 고득점인데 reject된 쌍 |

- 산출: 메커니즘별 false-reject율 + 쌍별 증거 메모. **규칙 변경은 실행하지 않는다** —
  N100 데이터를 보고 Claude가 계약 개정(fan-out 예외·연도 완화·veto 조정)을 결정한다.

## 실행 조건

- 리뷰 이미지 fetch: HTTP만, 예상 ~400-500건, **처음부터 network 허용 경로로 실행**
  (WinError 10013 재시도 낭비 금지), fetch ledger 전건 기록, WP1/WP2 캐시 재사용.
- 유료 API 0. merge 실행·vnx ID 발급·Neon/R2·규칙 변경 0.
- 산출물: `vnext_identity_n100_v1` manifest/DB/report + 독립 validator + job card,
  v3 DB SHA pin. 표본 사후 교체 금지. 실패 시 정지 후 FAIL 리포트.

## N100 이후 (Claude)

1. 정밀도 게이트 판정 → 2. 진단 데이터 기반 규칙 개정 결정 → 3. S1 registry 구축 +
   사람 승인 병합 큐 설계 발행 (merge_candidate 전량 + defer 재심 대상 포함).
