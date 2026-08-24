# vNext gallery + shallow semantics contract v1

상태: WP0c 확정 계약. S2는 사용자 gallery와 계보를 만들고, S3는 그 gallery의
대표 이미지별 얕은 분류만 저장한다. 건물에 대한 심층 시각 분석은 P6이며 보류다.

## 1. gallery 포함 단위

- source occurrence는 `(source, source_asset_id)`로 전량 보존한다.
- 사용자에게 노출하는 gallery는 duplicate/variant group마다 대표 이미지 1개다.
- S3 분류도 source occurrence 전부가 아니라 이 canonical representative에 1회 수행한다.
- 같은 사진이 다른 건물에 반복되면 조용히 삭제하지 않고 P2 entity 후보 증거로 보낸다.
- S2/S3 로컬 store의 건물 키는 S1 registry의 `vnx_bld_*`다.
  serving `canonical_bld_id`는 S6 projection에서만 부여한다.

## 2. image identity와 대표 변형

- exact pixel SHA 일치는 동일 pixel equivalence로 묶을 수 있다.
- pHash/crop/local feature는 direct edge만 기록한다. A-B와 B-C가 맞아도 A-C를
  자동으로 같은 변형으로 보지 않는다.
- 실제 group은 정확한 normalized-pixel SHA 동치류를 먼저 만들고, 그 동치류에서 고른
  대표에 직접 연결된 검증 edge만 star 형태로 부착한다. 대표와 direct edge가 없는
  member는 같은 group에 넣지 않으며 connected component/transitive closure를 쓰지 않는다.
- direct edge의 동결 출처는 E2 `full_v5`이고, E3 P2의 대표 중심 부착 방식을 재사용한다.
- pHash 단독으로 multi-source duplicate를 확정하지 않는다. 갤러리 후보와
  cross-source 후보쌍에만 verified pixel/crop feature를 추가한다.
- verified-pixel pass는 E1 SHA를 검증하고 파생 이미지를 로컬 cache에 1회 저장하며
  P4B와 공유한다.
- 대표 변형은 검증 성공, usable resolution, crop/overlay 손실, E3 quality 신호 순으로
  고르고 마지막 tie는 stable image key로 결정한다. 실제 score/config는 manifest에 고정한다.

S2는 대표 이미지뿐 아니라 group member, direct edge, 모든 source occurrence,
선정 reason code를 보존한다.

## 3. exclusion ledger

다음은 gallery에서 제외하되 행을 삭제하지 않는다.

- placeholder/blank/unreadable
- logo/text/editorial UI
- 비건축 subject 또는 프로젝트가 보이지 않음
- 독립 사용이 불가능한 multi-panel
- fetch/pixel hash/decoder 실패

각 행은 reason, 판정 engine/version, pixel 검증 상태, 재시도 가능 여부를 갖는다.
실패를 누락으로 표현하지 않는다. P3 종료 시 S2의 gallery + exclusion + unresolved 수가
입력 representative 모수와 정확히 맞아야 한다.

## 4. compact semantic 필드

v2.7에서 아래 9개 core field와 applicability만 가져온다. 긴 설명문은 생성하지 않는다.

| 필드 | controlled value |
|---|---|
| `in_scope` | boolean |
| `reject_reason` | `none`, `unreadable_or_blank`, `non_architectural_subject`, `people_or_event`, `isolated_product_artwork_or_sample`, `text_or_logo_only`, `no_project_visible`, `multi_panel_without_dominant_image`, `other` |
| `medium` | `photograph`, `drawing`, `rendering`, `physical_model`, `mixed`, `other`, `unknown` |
| `spatial_context` | `exterior`, `interior`, `threshold`, `not_applicable`, `unknown` |
| `framing_scale` | `site_context`, `overall`, `element_detail`, `material_detail`, `not_applicable`, `unknown` |
| `camera_angle` | `eye_level`, `elevated`, `aerial_oblique`, `aerial_top_down`, `not_applicable`, `unknown` |
| `drawing_kind` | `plan`, `site_plan`, `section`, `elevation`, `axonometric`, `perspective`, `detail`, `diagram`, `sketch`, `composite`, `other`, `not_applicable`, `unknown` |
| `project_state` | `visibly_finished`, `construction_visible`, `ruin_or_abandoned_visible`, `demolition_visible`, `not_applicable`, `unknown` |
| `project_legibility` | `high`, `medium`, `low`, `none`, `unknown` |

`perspective`는 외관 사진 분류나 cover slot이 아니다. drawing 또는 rendering의
표현 방식인 `drawing_kind` 하위값이다.

## 5. medium별 applicability

| 조건 | view axes | `drawing_kind` | `project_state` | legibility |
|---|---|---|---|---|
| out of scope | 모두 `not_applicable` | `not_applicable` | `not_applicable` | `none` |
| photograph | spatial/framing/camera 적용 | `not_applicable` | 적용 | high/medium/low/unknown |
| drawing | `not_applicable` | 구체값 원칙; 불명확하면 `unknown`+advisory 플래그 | `not_applicable` | high/medium/low/unknown |
| rendering | spatial/framing/camera 적용 | 구체값 원칙; 불명확하면 `unknown`+advisory 플래그 | `not_applicable` | high/medium/low/unknown |
| physical_model/mixed/other/unknown | 모두 `not_applicable` | `not_applicable` | `not_applicable` | high/medium/low/unknown |

따라서 drawing에 camera angle이나 framing scale을 억지로 채우지 않는다. aerial angle은
spatial context가 `exterior` 또는 `unknown`일 때만 유효하다.

엔진 출력이 이 적용성 표를 위반하면 행을 임의 기본값이나 `not_applicable`로 강제
변환하지 않는다. 해당 출력을 실패한 attempt로 원문·engine/version·오류 code와 함께
보존하고 그 행은 fail-closed 처리한다. 재시도나 다른 엔진의 성공 행만 active가 될 수 있다.

## 6. derived semantic types

core field에서 다음 non-exclusive tag를 결정론적으로 파생한다.

- `exterior_overall`, `exterior_context`, `interior`, `threshold`
- `drawing`, `rendering`, `physical_model`
- `aerial`, `detail`, `construction_archive`

예를 들어 exterior+aerial+overall 이미지는 세 의미를 동시에 가질 수 있다. 이 tag는
cover 선정 자체가 아니며, rendering은 semantic tag가 있어도 photo cover 자격이 없다.

## 7. engine, 불일치, uncertainty

각 S3 행은 `engine`, `engine_version`, `contract_version`, `transform_version`,
`pixel_sha256`, `status`, core fields, derived tag, 가능한 경우 axis score를 갖는다.

- local과 remote는 같은 compact 출력 계약을 사용한다.
- local 엔진 전수 + 저신뢰(이중판정 불일치·low score)·커버 후보만 remote 승격.
- **1.43M 모집단 전수 원격 Vision은 WP3 결과와 무관하게 금지.**
- WP3 N10은 엔진 선택·임계값 캘리브레이션만 결정한다.
- 위 전수 원격 금지와 퍼널 구조는 WP3에서 다시 미결화할 수 없다.
- local vs remote(필요 시 remote 2회) 값 차이를 axis별 `disagreement`로 기록한다.
- engine의 `uncertain_axes`나 resolution 신호를 저장할 수 있으나 advisory일 뿐 PASS/FAIL
  게이트가 아니다. 실제 escalation은 불일치·낮은 score·cover 후보 여부로 결정한다.
- 완료 아티팩트를 resume하면 model/network request가 0이어야 한다.

## 8. QA

- N10: scope/medium 명백 오류 0, 9축 수용률 90% 이상
- N100: 전체 수용률 95% 이상 + per-axis confusion 보고
- 표본 manifest와 exact pixel은 실행 전에 고정하며 사후 교체하지 않는다.
- N10은 블라인드 1인 review + 판정자 재확인, N100/승격만 2인+조정이다.
- semantic lane의 PASS/FAIL은 cover lane과 독립이다. P6 심층 분석을 대신하지 않는다.
