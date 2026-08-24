# WP1.1 실행 기록 — vNext source-changed drift overlay N10

상태: **PASS — WP1 종결**

지시서: `20260824_vnext_wp11_drift_overlay_order.md`

overlay run: `vnx_gallery_overlay_b7fe267071dbc42cbc189587`

## 범위와 비용

- v1 수정 없이 별도 overlay `vnext_gallery_draft_n10_v1_1` 생성
- v1 verified-pixel 결과와 기존 derivative cache만 사용
- HTTP 0 / Vision 0 / LLM 0 / Neon write 0 / R2 write 0
- 유료 토큰 비용 `$0`

## 계약·구현

- `docs/vnext/CONTRACT_GALLERY_SEMANTICS.md` §2에 verified-pixel 양 끝점
  `exact_match` 조건과 `unverifiable_edge_source_changed` split 규칙 추가
- `canonical/vnext_gallery_drift_overlay.py`: v1 immutable replay, exact class 보존,
  direct-edge split overlay, cache pin, blind sheet, 최종 manifest/report
- `canonical/vnext_gallery_drift_overlay_validator.py`: 생산 코드를 import하지 않는
  v1 lineage/group/edge/cache/review 독립 재생
- runner/validator CLI와 `tests/test_vnext_gallery_wp11.py` 추가

테스트: WP1+WP1.1 회귀 `9 passed`; WP1.1 재검사 `4 passed`.

## overlay 결과

| 항목 | 값 |
|---|---:|
| gallery 행 유지 | 169 |
| exact_match / pixel_unverified | 152 / 17 |
| v1 group / effective group | 162 / 166 (`+4`) |
| serveable / unresolved group | 149 / 17 |
| v1 direct edge | 6 |
| admissible / split | 2 / 4 |

- frozen exact normalized-pixel equivalence는 유지했다.
- `pixel_unverified` 17행은 gallery lineage에 남겼지만 대표·서빙 자격을 주지 않았다.
- 검증 멤버가 없는 17개 exact class/group은 `unresolved`로 계상했다.
- 4개 split edge의 원래 E2 edge 및 v1 local-feature evidence는 overlay에 보존했다.
- logical SHA: `5ad9cc5bdb135390330e06f7fc895eb550223fd23a58d2a7d1de5280842a044a`

## 블라인드 gate — PASS

- admissible pair 2/2만 opaque sheet에 렌더링; source/project/URL/filename 비공개
- B001/B002 모두 `same_or_variant`
- false collapse: `0`
- unreviewable: `0`
- admissible attachment review coverage: `100%`

독립 validator: **56/56 PASS**. SQLite integrity, record SHA, logical SHA,
v1 lineage, exact class, direct-star/no-transitivity, 4 split, 151개 cache 파일,
blind review와 최종 report/manifest pin을 모두 재검사했다.

## 드리프트 분해

| source | drift / gallery | 비율 |
|---|---:|---:|
| architizer | 17 / 72 | 23.61% |
| divisare | 0 / 97 | 0.00% |

Architizer 17건은 모두 `architizer-prod.imgix.net`이며 query key는
`auto,fit,fm,h,q,w`로 동일했다.

- `/media/{file}`: 14건
- `/media/mediadata/uploads/{file}`: 3건

이는 full P1 전 asset refresh ledger 설계 입력으로 넘긴다. 이번 WP에서는 새 픽셀
버전을 만들거나 frozen E1을 재동결하지 않았다.

## 불변·산출물 pin

- v1 DB pre/post/current SHA:
  `d63b7a5a279f71d0b9c68b6ca66c6123e934dd67fa5c085a9f8ea16736717cc7`
- contract SHA:
  `49133125c1755b050a6276aa6c5ff7a0916ad9d63d9da7793cf181c8db97b8a3`
- v1/overlay WAL·SHM·journal: 없음

| 산출물 | bytes | SHA-256 |
|---|---:|---|
| `data/enrichment/vnext_gallery_draft_n10_v1_1.db` | 589,824 | `ed108eda46099348dff30eebbf4fbbeb0fcc86718dcbec9951d48a5203deb9ac` |
| `data/reports/vnext_gallery_draft_n10_v1_1_manifest.json` | 1,714 | `c8c94dd27d177a0982e02cb5f64eaf2a0177da6976588f48b684e14a41c2f278` |
| `data/reports/vnext_gallery_draft_n10_v1_1_report.json` | 5,138 | `74bd6574774e865743d54c20d422960d7483a55fe34016f9fafd0c4fdfec64de` |
| `data/reports/vnext_gallery_draft_n10_v1_1_report.md` | 1,812 | `04c8a46ac2c89582d9f6cf4df464a8d1845edab6656a0d7d9bf095755895ba3b` |
| `data/reports/vnext_gallery_draft_n10_v1_1_validation.json` | 150,414 | `ccad509441461ff39c25d57316cae95bb93eb0ff9293d47db9992cfceb63a9d8` |
| blind sheet page 1 | 375,836 | `eadc29cb2c8f46ee797af45f2d46887f02d7a6d264799dae212f0e2c986887b6` |
| blind review form | 507 | `faec84df26c61da7c7e4c8bff4188a5c121dce7a98f18f967587318807d763a5` |

## 결론·다음 단계

WP1은 v1의 유효한 fail-closed FAIL 증거와 v1.1 overlay PASS를 한 쌍으로 종결한다.
다음 작업은 **WP2: 건물 병합(entity resolution) 후보 생성 및 N10 사람 검토**다.
WP2는 이 job에서 실행하지 않았다.
