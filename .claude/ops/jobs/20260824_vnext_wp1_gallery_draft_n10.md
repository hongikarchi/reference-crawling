# WP1 실행 기록 — vNext gallery draft N10

상태: **FAIL (의도된 fail-closed 정지)**

지시서: `20260824_vnext_wp1_gallery_draft_order.md`

run: `vnx_gallery_n10_806da380215c122e71dfb18f`

## 범위와 비용

- Stage A: frozen E2 full_v5 + E3 full_v1만 사용, offline/network 0
- Stage B: N10 gallery member 169건 HTTP only
- Vision 0 / LLM 0 / Neon·R2 write 0 / 유료 토큰 비용 $0
- sample replacement 0 / 자동 retry 0 / 완료 resume HTTP 0

## 구현

- `canonical/vnext_gallery_draft.py`: source-balanced N10, exact class, E2 direct-star, ledger/occurrence/accounting
- `canonical/vnext_gallery_verified_pixels.py`: E1 재검증, shared derivative cache, local feature, opaque review
- `canonical/vnext_gallery_draft_validator.py`: 생산 코드와 독립된 lineage/group/accounting/cache/review 재생
- CLI 3개와 `tests/test_vnext_gallery_wp1.py` 추가

테스트: `5 passed`. 통합 가짜 HTTP smoke에서도 169건 처리, cache, local pair, blind review,
완료 resume 0-request를 확인했다.

## Stage A 결과 — PASS

| 항목 | 값 |
|---|---:|
| buildings | 10 (Divisare 5 / Architizer 5) |
| strata | normal 8 / large 1 / duplicate burst 1 |
| input representative | 169 |
| gallery / exclusion / unresolved | 169 / 0 / 0 |
| source occurrences | 179 |
| groups | 162 |
| frozen E2 direct attachments | 6 |

- accounting: `169 = 169 + 0 + 0`, building별/전체 모두 PASS
- transitive closure 0, same-pHash-node-only grouping 0
- Stage A logical SHA: `70b3c5bc26ef82abfe13f113ebb8d050eb55a1444a10368198c801d71533a77e`
- independent validator: 23/23 PASS

## Stage B 결과

- HTTP 169: exact 152 / source_changed 17 / delivery_changed 0 / failed 0
- active derivative cache: unique verified pixel 151개, 26,909,641 bytes
- local feature: direct match 2, unavailable 4; exact-pixel smoke match 1
- direct match의 mutual keypoints/median cosine:
  - B005: 30 / 0.99981460
  - B006: 32 / 0.99989584
- 완료 artifact resume: HTTP 0

## 블라인드 gate — FAIL

- B005/B006: `same_or_variant`
- B001–B004: `unreviewable` (`source_changed`로 한쪽 또는 양쪽 frozen 픽셀 없음)
- 관찰된 `distinct_false_collapse`: 0
- 그러나 unreviewable 4이므로 false-collapse-zero를 **확정할 수 없어 FAIL**
- final independent validator: 39/41 PASS
  - FAIL `blind_review_unreviewable_zero` (0 expected / 4 actual)
  - FAIL `blind_review_status` (`pass` expected / `fail` actual)

지시서대로 provisional direct group을 임의 split하거나 표본을 교체하지 않고 여기서 정지했다.

## 동결·산출물 pin

- E2 pre/post/current SHA: `4728f9f015d7fba303923df40f550c59835a7e2a5c1316eef03c499ee3ca7b19`
- E3 pre/post/current SHA: `8512e11f8e1fd581038f790b27a67c0a8b1949067bf53b3ef30c4ea3534141a4`
- WAL/SHM/journal: 없음

| 산출물 | bytes | SHA-256 |
|---|---:|---|
| `data/enrichment/vnext_gallery_draft_n10_v1.db` | 2,461,696 | `d63b7a5a279f71d0b9c68b6ca66c6123e934dd67fa5c085a9f8ea16736717cc7` |
| `data/reports/vnext_gallery_draft_n10_v1_manifest.json` | 5,140 | `913a9278dcbd54d6ebe9853aa0aee6c6c45ef8edcfa4b5e3b6dd1c816626ade3` |
| `data/reports/vnext_gallery_draft_n10_v1_stage_a_report.json` | 1,219 | `b2ffad007e3ad8b205914dc2c4bd419deabb82c03bfb08a5417be21f68b5e93d` |
| `data/reports/vnext_gallery_draft_n10_v1_stage_a_validation.json` | 5,364 | `a5c4ead7e5747036f4d6c0fe3d8919c9d3a2f9b98b438c58fa65e4a39787dd07` |
| `data/reports/vnext_gallery_draft_n10_v1_validation.json` | 10,017 | `a78a021e07e7e455bf00c925fad91f3876d2ffd18bfc9cce4d2a9ab189ce7355` |
| blind review form | 814 | `578272e52a6634a1cb28012d03d3962804ae27740e1a7c5b2322aa9240b63832` |
| blind sheet page 1 | 285,935 | `ebdca96350aa7f32155cfc056b5f3fbfbe7daea1dd80b5a9ec904a67cf77b8bd` |
| blind sheet page 2 | 375,453 | `1626cdbb4522c585260f9a0a59aaebcfc7c5c63a03bd8be426a035d5ba08b2c0` |

## 다음 설계 결정 (미실행)

1. `source_changed` direct edge를 effective gallery에서 자동 split하는 fail-closed overlay
2. frozen E1 pixel derivative를 별도 보존·복구한 뒤 동일 N10 재검토
3. E1 baseline을 새로 동결하고 WP1을 새 version으로 재실행

job card의 “실패 후 재설계는 Claude 결정” 규칙에 따라 위 선택은 실행하지 않았다.
