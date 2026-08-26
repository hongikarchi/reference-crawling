# WP2.4 작업 지시 — N100 진단 기반 규칙 개정 (2026-08-26, Claude 발행)

## WP2.3 판정 — **PASS 인정**

- Core A70: same 66 / related 1 / insufficient 3 / **different 0**. 정밀도 94.29%
  (95% CI 86.01–98.42%). false merge 0, hard-conflict 위반 0, unreviewable 0.
  리뷰 완결 105/105 fresh × 2인 + 조정 (불일치 20건 중 14건 조정, 6건 insufficient 유지).
- n=70이므로 99% 주장 없음, 자동 병합 미허가, **v1 전건 사람 승인 유지** — 명시 확인.

## 진단 결과 해석 (규칙 개정의 근거)

| 스트라텀 | fresh 결과 | 해석 |
|---|---|---|
| B1 year gate | same 8 / insuf 1 / **different 0** (오거절 88.9%) | 연도 게이트가 hard conflict로 쓰인 것이 오류 |
| B2 pHash veto | same 3 / insuf 2 / different 4 (오거절 33.3%) | veto는 유효하나 **발화 조건이 과광범** |
| B3 fan-out | same 3 / related 1 / insuf 3 / **different 0** (오거절 42.9%) | 가드는 필요하나 defer 종착이 문제 |
| B4 name-only | same 0 / different 6 / insuf 4 (오거절 **0%**) | 현행 규칙 정확 — **변경 금지** |

핵심 패턴 2가지:

1. **B1 9건 전부** `architect_firm_identity`=100 + `verified_pixel_image_overlap` 양성인데
   연도 3~5년 차이로 차단됐다. 두 소스의 연도 필드는 준공/발표 시점이 섞여 있다.
   **공유된 사진 증거를 메타데이터 연도 필드가 뒤집을 수 없다.**
   근인은 계약 §3이 레거시의 *blocking(후보 생성 제한)* 규칙을 *veto(하드 충돌)* 로
   승격 번역한 것 — Claude 귀책.
2. **B2는 firm 일치 여부로 완벽히 갈린다**: firm 양성 4/4 = same(오거절),
   firm 충돌 6/6 = different·insufficient(정당). veto의 설계 목적은 "일반명 충돌
   과병합"(레거시 House H 버그) 차단이며, 같은 설계사·같은 이름에서 픽셀 미겹침은
   두 소스가 서로 다른 사진 세트를 실었다는 뜻일 뿐이다.

## 개정 원칙 (비대칭 비용)

v1은 모든 병합이 사람 승인이다. 따라서 **오후보 비용 = 사람 리뷰 1건**,
**오거절 비용 = 영구·불가시 데이터 손실**이다. 경계 사례는 거절이 아니라 사람 큐로
보내는 방향으로 완화한다. 단, 완화가 B4 밴드를 침범하면 안 된다(회귀로 증명).

## 개정 지시 (CONTRACT_IDENTITY §3/§4 개정 + 새 SHA pin)

- **R1 연도**: `matcher_architect_year_gate_block`을 hard conflict 목록에서 **삭제**한다.
  연도는 gap ≤1 양성 / gap 2–5 중립 / gap ≥6 약한 음성으로 재정의하고, gap ≥6은
  **`verified_pixel_image_overlap` 양성도 아니고 `architect_firm_identity` 양성도
  아닐 때만** `project_phase_incompatible` hard conflict가 된다.
  (B4 12년 격차 사례는 이 조건을 그대로 만족해 계속 차단됨 — 회귀로 확인할 것.)
- **R2 pHash veto**: `architect_firm_identity`와 `name_alias_identity`가 **둘 다 양성**이면
  veto는 hard conflict가 아니라 `phash_veto_review_required`로 강등한다. 이때 제안은
  `merge_candidate`, **auto-merge-eligible은 false**(픽셀 앵커 부재를 존중). 그 외
  (firm 충돌·부재)에서는 현행대로 hard conflict를 유지한다.
- **R3 fan-out**: 가드는 유지하되 종착 상태를 바꾼다. fan-out에 걸린 쌍이 앵커 포함
  양성 2계열 이상이면 `defer`가 아니라 `merge_candidate` + `fanout_review_required`
  (eligible=false)로 둔다. 그 미만만 `defer`.
- **R4 구조적 결함 수정**: 사람 승인 큐는 `merge_candidate`뿐 아니라 **앵커 포함 양성
  2계열 이상인 `defer`도 반드시 포함**한다. 큐에 오르지 않는 defer는 사실상 조용한
  거절이며, 이는 이번 진단이 드러낸 가장 큰 구조 위험이다. 계약 §4에 명문화한다.
- **R5**: name-only 밴드(B4 유형)와 국가 충돌·앵커 요건은 **변경 금지**.

## 실행

1. `vnext_identity_candidates_v4` Stage A 재생(offline, 유료 0). v3 불변·SHA pin.
2. **v3→v4 전이 전수 집계 + 볼륨 영향 보고**(가장 중요한 산출물):
   - 상태별 증감, R1/R2/R3 각각이 만든 전이 수를 **규칙별로 분리** 집계
   - 최종 `merge_candidate` 총수, `eligible` 총수, **사람 승인 큐 예상 규모**
     (merge_candidate + R4 대상 defer), 증거 강도 밴드별 분포
     (예: 이름+firm+이미지 3계열 / 2계열+앵커 / veto·fanout 플래그부)
3. **회귀 게이트**(표본 교체 없이 기존 gold 재사용, 신규 리뷰 불요):
   - B4 10쌍 전부 여전히 reject 유지 — 1건이라도 완화되면 FAIL
   - WP2.2의 지정 오류 4쌍(세네갈 다카르 3 + Casa Martha/Villa Manzanillo) reject 유지 — FAIL 조건
   - B2의 different 4쌍 여전히 hard conflict 유지 — FAIL 조건
   - B1 9쌍 / B3 fan-out same 6쌍이 사람 큐 대상(merge_candidate 또는 R4 defer)으로
     승격됐는지 확인 — 미승격 시 규칙 미반영으로 FAIL
4. 독립 validator·테스트·job card·SHA pin. 유료 API 0, merge·ID 발급·Neon/R2 0.
5. **fresh N10은 이번에 실행하지 않는다** — 회귀 게이트가 핵심이고, 신규 표본 검증은
   S1 착수 전 Claude가 볼륨 보고를 보고 필요 여부를 판단한다.

## 다음 (Claude)

볼륨 보고 검토 → 사람 승인 큐가 1인 운영으로 감당 가능한 규모인지 판정 →
필요하면 증거 강도별 티어링(일괄 승인 + 표본 검사 / 개별 리뷰)을 사용자 결정으로
상정 → S1 registry + 승인 큐 설계 발행.
