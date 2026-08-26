# WP2.5 작업 지시 — 밴드별 정밀도 측정 (2026-08-26, Claude 발행)

목적: 5,100건 승인 큐를 **어떤 밴드는 일괄 승인 / 어떤 밴드는 개별 리뷰**로 나눌
근거를 만든다. 이번 WP는 측정만 하고 티어링을 실행하지 않는다. 유료 API 0,
merge·ID 발급·Neon/R2 0. 입력은 v4 아티팩트(SHA pin), 불변 보존.

## 사전 정책 (측정 전 확정)

- **플래그부 쌍은 어떤 일괄 승인 티어에도 들어갈 수 없다** — `fanout_review_required`
  236 / `phash_veto_review_required` 229 / `anchor_missing` 289(중복 포함 최대 754).
  전부 개별 리뷰 대상으로 확정하며, 따라서 정밀도 측정 대상에서 제외한다.
- 측정 모집단 = 각 밴드의 **무플래그 쌍**.

## 표본 (전부 기리뷰 쌍 disjoint, seed 고정, 리뷰 전 동결, 사후 교체 금지)

| 스트라텀 | 모집단 | n |
|---|---|---:|
| S1 | `name_firm_image_3plus` 무플래그, ex-related 제외 | 60 |
| S2 | `other_anchor_3plus` 무플래그, ex-related 제외 | 60 |
| S3 | `anchor_2` + `no_anchor_2plus` 무플래그 통합 | 40 |
| S4 | v3 `related_but_separate`였다가 v4 merge_candidate가 된 119건 | 25 |
| S5 | F-B 신규 거절 36건(v3 하드충돌 0 → R1이 `project_phase_incompatible` 부여) | **36 전수** |

합계 221쌍. S4를 분리하는 이유: 119건 중 99건이 최강 밴드에 있어, 일괄 승인의
핵심 위험이 여기 집중된다(WP2.4 게이트 판정 F-A).

## 프로토콜

- 승격 결정용이므로 계약 §8의 **격리 블라인드 2인 독립 + ledger 동결 + 조정자**
  전체를 적용한다(N100과 동일). 제안 상태·점수·정책 플래그·밴드 은닉.
- gold 4어휘(same/related/different/insufficient). 미해결 불일치는 `insufficient`.
- 이미지는 현재 픽셀 fetch(HTTP만, ~900건 예상), 기존 캐시 재사용, fetch ledger 전건 기록.
  드리프트 상태는 표기하되 판정 배제 사유가 아니다.
- 비용 주의: 리뷰 부하가 N100의 약 2배다. 주간 쿼터 규칙(≤30% 신규 착수 금지,
  ≤20% 전면 중단)을 그대로 적용하고, 착수 전 잔량을 job card에 기록한다.

## 판정 기준 — **측정 전 선언, 사후 변경 금지**

S1~S4 각 스트라텀에서 `same`이 아닌 판정(related/different/insufficient) 수를 오류로 센다.

| 오류 수 (n=60 기준, 다른 n은 비율 환산) | 그 밴드의 후보 처리 등급 |
|---|---|
| 0–1 | 일괄 승인 후보 (사용자 표본 감사 조건부) |
| 2–4 | 에이전트 1차 판정 + 사용자 경계 사례 확인 |
| ≥5 | 개별 직접 리뷰 |

- `different`가 1건이라도 나온 스트라텀은 오류 수와 무관하게 **일괄 승인 후보에서
  자동 제외**한다(잘못된 병합이 실재한다는 직접 증거).
- **S5에서 `same`이 1건이라도 나오면 R1의 ≥6년 규칙은 FAIL** — 규칙 재개정이 필요하며
  S1 착수를 보류한다.
- 등급은 후보일 뿐이며 최종 티어링은 사용자 결정이다.

## 산출물

`vnext_identity_band_precision_v1` manifest/DB/report + 독립 validator + job card.
report에 스트라텀별 gold 분포, 정밀도, Clopper-Pearson 95% CI, 판정 기준 대비 등급,
불일치·조정 내역, fetch ledger 요약, 쿼터 사용량을 포함한다. 실패 시 정지 후 FAIL 리포트.

## 다음 (Claude)

결과 검토 → 밴드별 티어링안을 사용자 결정으로 상정 → S1 registry + 승인 큐/리뷰 앱 설계.
