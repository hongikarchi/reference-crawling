# WP2.6 작업 지시 — 리뷰 계측기 교정 probe (2026-08-27, Claude 발행)

목적: WP2.5 리뷰 프로토콜이 **알려진 비-same 쌍을 실제로 잡아내는지** 검증한다.
결과가 깨끗할수록 계측기가 오류를 낼 능력이 있는지 먼저 증명해야 한다.
측정만 수행하며 티어 승격·merge·ID 발급·Neon/R2·유료 API는 전부 0.

## Step 0 — 원인 가설 확인 (무비용, 선행)

WP2.5 블라인드 리뷰어 form과 N100 리뷰어 form을 **필드 단위로 비교**해 job card에 표로
남긴다. 특히 N100의 축별 rationale(`image=` / `name=` / `firm=` / `place=` / `year=` /
`scope=` / `basis=`)이 WP2.5에 존재하는지 확인한다. 없으면 그 사실을 명시한다.
(추정하지 말고 실제 form 스키마·저장 payload로 확인할 것.)

## Step 1 — seeded control probe

WP2.5의 **패킷 빌더·렌더러·자산 상한(측당 2장)·블라인드 절차를 변경 없이 그대로** 사용한다.
지금 테스트 대상은 후보가 아니라 계측기다.

| 그룹 | 구성 | n | 기존 gold(N100, 은닉) |
|---|---|---:|---|
| P1 | A70 비-same 중 **무플래그 3쌍** | 3 | insufficient 2 / related 1 |
| P2 | B3 fanout의 related 1 + insufficient 3 | 4 | related 1 / insufficient 3 |
| P3 | B2 phash veto의 insufficient 2 | 2 | insufficient 2 |
| D | A70 `same` 판정 쌍(무작위) | 15 | same 15 |

총 24행. 순서 셔플, 그룹·기존 라벨·후보 ID·제안 상태·점수·플래그 전부 은닉.
**기존 gold는 두 리뷰어 ledger와 조정 ledger가 모두 동결된 뒤에만 공개**한다.
WP2.5 표본 221쌍과 중복 금지.

## Step 2 — 판정 기준 (측정 전 선언, 사후 변경 금지)

계측기 **PASS** 조건은 셋 다 충족:

1. P1 3쌍 중 **2쌍 이상**을 `same`이 아닌 라벨로 판정
2. P2+P3 6쌍 중 **3쌍 이상**을 `same`이 아닌 라벨로 판정
3. 대조군 D 15쌍 중 **13쌍 이상**을 `same`으로 판정 (과교정 방지)

하나라도 미달이면 **FAIL**.

- **PASS 시**: WP2.5의 S1~S4 등급 유효. Claude가 티어링안을 사용자 결정으로 상정.
- **FAIL 시**: S1~S4 무효. Step 0에서 확인된 누락 필드를 복원해
  (최소한 축별 `scope=` 강제, 라벨 4종의 정의와 예시, "사진 일치만으로 척도 동일을
  단정하지 말 것" 명시) 프로토콜 v2를 만들고, **fresh disjoint 표본으로 S1~S4 재측정**.
  노출된 221쌍 재사용 금지. 재측정 지시는 Claude가 별도 발행한다.

## 산출물

`vnext_identity_instrument_probe_v1` manifest/DB/report + 독립 validator + job card.
report에 그룹별 판정 대조표(기존 gold vs probe 라벨), 판정 기준 대비 결과, 리뷰어
불일치·조정 내역, Step 0 form 비교표, fetch·쿼터 기록을 포함한다.
착수 전 주간 쿼터 잔량 기록, 규칙(≤30% 착수 금지 / ≤20% 중단) 적용. 실패 시 정지 후 FAIL 리포트.
