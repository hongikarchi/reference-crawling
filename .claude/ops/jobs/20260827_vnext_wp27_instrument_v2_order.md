# WP2.7 작업 지시 — 리뷰 계측기 v2 + 동일 seed 대조 재probe (2026-08-27, Claude 발행)

전제: WP2.6 FAIL 진단(`20260827_vnext_wp26_gate_verdict.md`). v1 계측기는 245행에서
`insufficient`를 0건 산출했다 — 기권 능력이 없다. 이번 WP는 그 결함을 고치고,
**동일 입력에 대한 v1 대 v2 대조 실험**으로 수정 효과를 증명한다.
측정·교정만 수행하며 티어 승격·merge·ID 발급·Neon/R2·유료 API는 0.

## 설계 원칙 — 관찰과 파생의 분리

리뷰어가 **최종 라벨을 고르게 하지 않는다.** 리뷰어는 축별 관찰만 기록하고,
라벨은 그 관찰에서 **결정론적으로 파생**한다. 이 프로젝트가 이미 커버 선정에서
적용한 원칙(모델에게 최종 결정을 시키지 않는다)을 리뷰에도 적용하는 것이다.
v1이 `same`으로 수렴한 이유는 라벨을 직접 고르게 했기 때문이다.

## Stage A — 계측기 v2 사양

### 필수 관찰 축 (전부 controlled value, 미기입 불가)

| 축 | 값 |
|---|---|
| `image` | `same_object` / `different_objects` / `inconclusive` |
| `name` | `agree` / `alias` / `conflict` / `unclear` |
| `firm` | `agree` / `overlap` / `conflict` / `unknown` |
| `place` | `agree` / `unclear` / `conflict` |
| `year` | `compatible` / `conflict` / `unclear` |
| `scope` | `same` / `component_vs_umbrella` / `phase_or_extension` / `unclear` / `unrelated` |
| `basis` | 자유 서술(관찰 근거, 필수) |

`image=inconclusive`의 정의를 명시할 것: 한쪽이 도면·다이어그램뿐이거나, 출판물
지면·표지만 있거나, 건물 형상을 비교할 수 있는 이미지가 없을 때. **"비교할 수 없음"은
정상적이고 바람직한 관찰이며 실패가 아니다**를 리뷰어 지시문에 명시한다.

### 라벨 파생 규칙 (결정론, 순서대로 적용)

1. `image=inconclusive` → `insufficient`
2. `image=different_objects` 또는 `scope=unrelated` → `different`
3. `scope ∈ {component_vs_umbrella, phase_or_extension}` → `related`
4. `image=same_object` 그리고 `scope=same` → `same`
5. 그 외(예: `scope=unclear`) → `insufficient`

- 조정자는 **라벨이 아니라 축을 조정**한다. 라벨은 조정된 축에서 재파생한다.
- 추가 지시문 2개를 명시: (a) **사진이 일치한다는 사실은 피사체 공유를 뜻할 뿐
  프로젝트 척도의 동일을 뜻하지 않는다**; (b) 한쪽 이름이 마을·캠퍼스·단지·마스터플랜·
  공원·축제 등 상위 집합을 가리키고 다른 쪽이 그 안의 개별 대상이면
  `scope=component_vs_umbrella`다.

패킷 빌더·렌더러·자산 상한(측당 2장)·블라인드 절차는 v1과 동일하게 유지한다 —
바꾸는 것은 **관찰 form과 라벨 파생뿐**이다(대조 실험의 유효성 조건).

## Stage B — 동일 seed 대조 재probe

WP2.6의 **24행 seed를 그대로 재사용**한다. 리뷰어는 매 실행 무상태 격리 에이전트이므로
오염이 아니며, 동일 입력·다른 계측기 비교가 이번 실험의 목적이다. 기존 gold와 v1 probe
라벨은 두 리뷰어·조정 ledger 동결 후에만 공개한다.

## Stage C — 판정 기준 (측정 전 선언, 사후 변경 금지)

seed의 N100 gold 구성: `insufficient` 7행(P1 2 + P2 3 + P3 2), `related` 2행(P1 1 + P2 1),
`same` 15행(D). PASS는 **네 조건 모두** 충족:

1. D 15행 중 **13행 이상** `same` (과교정 방지)
2. P1 3행 중 **2행 이상** 비-`same`
3. `insufficient` gold 7행 중 **4행 이상**을 `insufficient`로 산출 (v1은 0/7)
4. `related` gold 2행 중 **1행 이상**을 `related`로 산출

하나라도 미달이면 FAIL이며, 원인을 리포트하고 **정지**한다(임의 재설계 금지).

## 산출물

`vnext_identity_instrument_v2_probe_v1` manifest/DB/report + 독립 validator + job card +
계측기 v2 프로토콜 문서. report에 **행별 3자 대조표**(N100 gold / v1 probe 라벨 /
v2 파생 라벨 + v2 축 관찰값), 판정 기준 대비 결과, 축 조정 내역, 파생 규칙 적용 감사
(축→라벨 재계산이 저장 라벨과 일치하는지), fetch·쿼터 기록을 포함한다.
착수 전 주간 쿼터 잔량 기록, 규칙(≤30% 착수 금지 / ≤20% 중단) 적용.

## 다음 (Claude)

PASS 시 WP2.8(계측기 v2로 S1~S4 fresh disjoint 재측정, false merge / related /
insufficient 세 비율 분리 보고) 발행. FAIL 시 원인 검토 후 재설계.
