# vNext gallery + shallow semantics contract v1

상태: WP0c 확정 계약, §2 v1.2 개정(2026-10-20, §2.1), v1.3 개정(2026-10-23, §2.1 D1/D2/D3),
v1.4 개정(2026-10-28, §4.1 legibility 2단계·camera 경계 `unknown`). S2는 사용자 gallery와 계보를 만들고, S3는 그 gallery의
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
- direct-edge star 부착은 양 끝점이 같은 verified-pixel run에서 `exact_match`일 때만 허용하며, 어느 한쪽이라도 `source_changed`/`failed`이면 `unverifiable_edge_source_changed`로 split하고 edge 증거는 보존한다.
- pHash 단독으로 multi-source duplicate를 확정하지 않는다. 갤러리 후보와
  cross-source 후보쌍에만 verified pixel/crop feature를 추가한다.
- verified-pixel pass는 E1 SHA를 검증하고 파생 이미지를 로컬 cache에 1회 저장하며
  P4B와 공유한다.
- 대표 변형은 검증 성공, usable resolution, crop/overlay 손실, E3 quality 신호 순으로
  고르고 마지막 tie는 stable image key로 결정한다. 실제 score/config는 manifest에 고정한다.

### 2.1 v1.2 개정 (2026-10-20) + v1.3 (2026-10-23): population continuity attestation

Architizer host 이전(imgix 410 → images.architizer.com, 새 인코더)으로 E1 pixel SHA 연속성은
원천적으로 불가하다. 근거: `20261019_vnext_asset_refresh_probe.md`, 지시서
`20261020_vnext_wp4_s2_full_order.md`(v1.3 = 같은 지시서의 결정 D1/D2/D3). S2 full v1에서는 이 절이
위 per-asset 규칙보다 우선한다.

- **건물 키(§1 예외)**: S2 full v1 store는 source building `(source, source_building_id)`로 키를 잡는다.
  `vnx_bld_*` re-key는 S1 감사 확정 후 P3에서 별도 단계로 한다.

- **continuity attestation (규칙 v2, v1.3)**: source × fetch-profile 단위 고정 표본(seed
  `vnext-asset-continuity-v2`, source별 E2 success N=1000)에서 `continuous` 비율 ≥ 99.0% 이고
  `content_changed` = 0 이면 그 source의 E1 success 자산은 `continuity_attested`다.
  - `continuous` = HTTP 200 ∧ pHash-256 Hamming ≤ 32 ∧ 종횡비 상대차 ≤ 2%.
  - `content_changed` = HTTP 200 ∧ (Hamming > 32 ∨ 종횡비 상대차 > 2%).
  - 종횡비 상대차 = `|(w·h_E1)/(h·w_E1) − 1|`. 크기 절대값(재렌더·리사이즈 반올림)은 판정에 쓰지 않는다.
    pixel SHA 일치는 요구하지 않는다.
  - 404/410은 `unavailable`로 집계·보고한다. 분모에서 빼지 않으며 attestation 실패 사유가 아니다.
  - 도구: `tools/vnext_asset_continuity_sample.py --rule-version v2` →
    `data/reports/vnext_asset_continuity_sample_v2.jsonl` + `_summary.json` (바이트 저장 없음).
    summary의 `rule_version`은 `vnext-asset-continuity-rule-v2`, `sample_seed`는 위 seed여야 한다.
  - 이력: 규칙 v1(v1.2, seed `vnext-asset-continuity-v1`, 크기 각 축 ±2px)은 같은 그림의 재렌더 크기
    변화 3건을 `content_changed`로 오탐해 FAIL했다. v1 표본·FAIL 기록은 보존하고 `--rule-version v1`로
    재현 가능하다. v2는 실행 전 고정한 규칙을 새 seed 표본으로 판정한다(사후 조정 아님).
  - attestation이 증명하는 것은 **그림 동일성(identity)**이다. 표본 profile(Architizer
    `?width=1024&height=1024&fit=max&quality=85&format=jpg`, Divisare = E1 `fetch_url`)은 서빙 URL
    profile과 다르므로 서빙 rendition(포맷·크기·바이트)을 증명하지 않는다. 서빙 URL의 렌더 가능성은
    아래 서빙 URL 규칙(정적 검사)으로만 보장한다.
- **S2 full v1 대표 자격** = E1 fingerprint success ∧ `continuity_attested` ∧ §3 exclusion 규칙 통과
  ∧ 브라우저 렌더 가능한 서빙 URL 존재(없으면 `unresolved`/`no_browser_renderable_serving_url`).
  per-asset `exact_match`(WP1.1 규칙)는 S3 픽셀 pass(P4B)에서 사후 기록한다(다운로드 1회 원칙).
- **S2 full v1은 direct-edge star 부착을 적용하지 않는다.** group은 exact normalized-pixel SHA
  동치류뿐이다. E2 direct edge(pHash/crop)는 `direct_edge_evidence`에 보존만 하고
  `attachment_status='deferred_no_per_asset_verification'`으로 둔다.
  - 보존 범위 = 같은 건물의 서로 다른 두 group 대표 node를 잇는 E2 `phash_edges` 전 scope
    (`global_le8` Hamming 1–8, `metadata_9_16` Hamming 10–16).
  - 같은 pHash node(Hamming 0)를 공유하는 서로 다른 exact class는 E2에 edge 행이 없으므로 evidence
    행을 만들지 않는다. 그 관계는 `gallery_groups.representative_phash_node_id`로 복원하며 report에
    `same_phash_node_group_pairs`로 집계한다(S3/P4B 검증 후보).
- **서빙 URL**: frozen `source_asset_id`를 유지하고 key를 재계산하지 않는다.
  - Architizer (v1.3, D2): **현행 Neon 문자열과 정확히 같다.** frozen E2 `canonical_url` 전체가
    `tools/architizer_image_host_migrate.py`의 `PATTERN`에 fullmatch해야 하고, 서빙 URL은 그 regex
    치환 결과 = `https://images.architizer.com` + path **원문(percent-encoding 안 함)** +
    `?width=1680&quality=60`이다. 원래 query는 버린다. fullmatch하지 않으면 `unresolved`
    (`no_browser_renderable_serving_url`). 근거: `canonical_v2_buildings.all_images`와
    `building_images.url`이 문자열로 조인·비교되어야 하고, 브라우저는 비ASCII path를 스스로 인코딩한다.
    non-ASCII path 대표 수는 report에 `architizer_serving_url_non_ascii_path`로, Neon 형식 불일치는
    `architizer_serving_url_neon_divergent`(= 0이어야 함)로 집계한다.
  - Divisare: frozen E2 occurrence URL 원문(key 재계산 없음). 대표 asset의 occurrence 중 gallery 우선,
    그다음 cover, page 순서로 **브라우저 raster인 첫 URL**을 쓴다. raster 판정 = path에 명시적 raster
    format transform(`f_jpg`/`f_png`/`f_webp`/`f_avif`/`f_gif`)이 있거나 확장자가
    jpg/jpeg/png/gif/webp/avif/bmp. `f_auto`는 원본 포맷을 그대로 내보내므로 `.pdf/.ai/.eps/.psd/
    .tiff/.jp2/.octet-stream` gallery URL(E2 success 9,985 자산)은 raster가 아니다. raster URL이 없는
    자산은 `unresolved`(`no_browser_renderable_serving_url`, fail-closed, v1.3 D3 확정). raster 변환
    URL 재작성(예: `f_auto`→`f_jpg`)은 하지 않는다(다음 주 별도 결정).
- `sort_order`는 건물별 0부터 연속이며 소스 페이지 원래 순서(E2 occurrence ordinal = curated
  DB position)를 따른다. 정확한 규칙은 S2 report/manifest에 고정한다.

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
| `project_legibility` | `high`, `medium`, `low`, `none`, `unknown` (v1.4: `medium` 미출력, §4.1) |

`perspective`는 외관 사진 분류나 cover slot이 아니다. drawing 또는 rendering의
표현 방식인 `drawing_kind` 하위값이다.

### 4.1 v1.4 개정 (2026-10-28): legibility 2단계 + camera 경계 `unknown`

근거: WP3 N10 후속 실험 v2(`20261027_vnext_wp3_s3_engine_n10.md` §후속 실험,
`data/smoke_vnext/wp3_n10_v1/exp_v2/report.md` §6) — gold 모호율 legibility 24%, camera
eye_level↔elevated 경계 오류가 지배적. 사용자 결정 2026-10-28. 위 §4 표의 값 목록과 DDL v1.1 CHECK는
그대로 두고(DDL 변경 없음) **출력 규칙과 채점 규칙만** 바꾼다. v1.3 이하로 만든 행·gold는 보존한다.

- **`project_legibility` 출력값** = `high`(판독 가능: 프로젝트가 읽힘) | `low` | `none` | `unknown`.
  `medium`은 더 이상 출력하지 않는다(DDL CHECK는 계속 허용, 과거 행 호환용). §5 표의
  "high/medium/low/unknown"은 v1.4 출력에서 "high/low/unknown"으로 읽는다. `none`은 out of scope 전용(§5).
- **`camera_angle`**: 시점이 eye level과 약간 높은 시점(elevated) 사이라 어느 쪽인지 분명하지 않으면
  `unknown`을 출력한다. `aerial_oblique`/`aerial_top_down`은 명시적일 때만 출력한다(경계 `unknown`을
  aerial로 해석하지 않는다). 따라서 §6 `aerial` tag는 명시적 aerial 값에서만 파생된다.
  `unknown` 허용은 camera가 적용되는 행(§5: photograph/rendering in scope)에만 해당한다.
- **채점 규칙 (v1.4, gold·예측 양쪽에 같은 매핑 적용)**:
  - legibility: gold primary·acceptable의 `medium` → `high`, 예측의 `medium` → `high`로 매핑 후 비교.
  - camera: gold primary가 `eye_level` 또는 `elevated`이면 예측 `unknown`도 수용한다.
    gold가 aerial·`not_applicable`인 행에서 `unknown`은 수용하지 않는다(acceptable에 명시된 경우 제외).
  - 그 밖의 필드·§8 게이트 기준(scope/medium 명백 오류 0, 9축 수용률)은 v1.3과 같다.
  - 도구: `tools/vnext_wp3_eval_v14.py` (v1 도구 `tools/vnext_wp3_eval.py`는 변경하지 않음).
- 엔진 출력의 `contract_version`은 v1.4 행부터 `vnext-gallery-semantics-contract-v1.4`다.

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
