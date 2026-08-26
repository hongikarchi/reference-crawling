# WP2.4 게이트 판정 (2026-08-26, Claude)

## 판정 — **PASS**

- 회귀 게이트 4종 33쌍 전부 PASS (B4 name-only 10, WP2.2 국가오류 4, B2 different 4,
  B1/B3 큐 승격 15). validator 111/111. 유료 0, merge·ID·Neon/R2 0.
- 규칙별 전이 분리 집계와 볼륨 보고 모두 지시대로 제출됨.

## 볼륨 (S1 설계 입력)

| 항목 | v3 | v4 |
|---|---:|---:|
| merge_candidate | 4,458 | **5,100** (+642) |
| auto-merge eligible(계산상) | 4,169 | 4,346 (+177) |
| defer | 2,445 | 2,192 |
| reject | 61,025 | 60,753 |
| related_but_separate | 121 | **4** |

사람 승인 큐 = 5,100 (merge_candidate 전량). R4 대상(앵커 2계열+ defer)은 **공집합** —
그런 조건이면 애초에 merge_candidate가 되므로 논리적으로 정합. R4는 무해하나 실효 0.

증거 강도 밴드: `name_firm_image_3plus` 2,402 / `other_anchor_3plus` 1,799 /
`anchor_2` 610 / `no_anchor_2plus` 289(eligible 아님).
중복 플래그: fanout 236 / phash_veto 229 / anchor_missing 289.

## 발견 (S1 전 처리 필요)

- **F-A `related_but_separate` 121→4 붕괴.** 119건 전부 R1 때문이며, v3 하드충돌이
  `matcher_architect_year_gate_block`이었다(93건은 단독). 즉 v3의 "related" 라벨은
  대부분 잘못된 연도 veto의 부산물이었고, N100 B1 증거(9건 중 same 8)에 비추어 이들을
  merge_candidate로 올린 것은 타당하다. **다만 위험이 이동했다**: 이제 단계·증축 관계를
  걸러내는 규칙 수준 사전 필터가 없고 전적으로 리뷰어의 `related` 판정에 의존한다.
  게다가 119건 중 **99건이 최강 밴드(name_firm_image_3plus)에 들어 있다** — 최강 밴드
  일괄 승인의 위험을 A70 수치(94.29%)보다 높게 봐야 하는 직접 근거.
- **F-B 신규 거절 36건.** R1이 `project_phase_incompatible` 하드충돌 1,039건을 새로
  추가했고, 그중 36건은 v3에서 하드충돌이 **하나도 없던** defer가 곧바로 reject로
  바뀐 것이다(나머지 1,003건은 이미 reject라 무해). 이 36건은 검증된 적 없는 신규
  거절 질량이며, 비용 비대칭(오거절 = 영구 손실) 원칙상 소표본 확인이 필요하다.

## 다음 결정 (사용자)

5,100건 승인 큐의 처리 방식. 밴드별 정밀도가 아직 없으므로(A70은 밴드 층화가 아니라
앵커 유형 비례 표본) 전 밴드 일괄 방침을 지금 확정하는 것은 근거가 부족하다.
사용자 결정 후 S1 registry + 승인 큐/리뷰 앱 설계를 발행한다.
