# WP4 실행 기록: S2 full gallery v1 + continuity attestation (2026-10-20)

상태(최신, r3 2026-10-23): **PASS** — continuity v2 양 source attested, FULL 91,803 빌드 완료, 독립 validator
54/54 PASS. 상세는 맨 아래 §8. (§1–§7은 r1 FAIL·r2 수정 기록이며 증거로 보존.)

r1 상태: **FAIL (continuity attestation 미달 → 지시서대로 정지)**. full 91,803 빌드는 실행하지 않았다.
지시서: `20261020_vnext_wp4_s2_full_order.md`. 비용: 유료 LLM/Vision **$0**, Neon/R2 쓰기 0.
네트워크: continuity 표본 HTTP GET 2,000건(사용자 승인 범위, ≤4 rps). S2 빌더 network 0.

## 1. 산출물 A: continuity 표본 — NOT_ATTESTED

도구 `tools/vnext_asset_continuity_sample.py` (SHA `bea5113a…2e59`), seed `vnext-asset-continuity-v1`,
source별 E2 success 1,000, 4 workers ≤4 rps, 바이트 미저장. 경과 752.5 s (2026-10-20 10:21–10:34Z).
표본 manifest SHA `11ddf3dc…1d03`. E2 SHA 전/후 불변.

| source | n | continuous | content_changed | unavailable | rate | Hamming max | pix_eq | 판정 |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| architizer | 1000 | 999 | **1** | 0 | 0.999 | 24 | 0 | not_attested |
| divisare | 1000 | 998 | **2** | 0 | 0.998 | 8 | 997 | not_attested |

content_changed 3건은 모두 **같은 그림**(Hamming 4 / 0 / 6)이며 크기 규칙(각 축 ±2px)만 위반:
- `atz_asset_fb2e11de…`: 1024×263 파노라마가 1021×263 (새 host 리사이즈 반올림 3px)
- `divisare|pv60dsxm…`(.tiff): 같은 frozen URL이 489×643 → 779×1024로 재렌더
- `divisare|tiljnekm…`(.pdf pg_1): 1024×723 → 842×595로 재렌더

비율 조건(≥99%)은 양쪽 충족, `content_changed = 0` 조건이 양쪽 실패. FAIL 리포트:
`data/reports/vnext_gallery_full_v1_FAIL.json`. **규칙 재설계는 하지 않았다** — 결정 필요(아래 §6).

## 2. 산출물 C: 계약 §2 v1.2

`docs/vnext/CONTRACT_GALLERY_SEMANTICS.md`에 §2.1 추가(attestation, 대표 자격, star 미적용,
서빙 URL, sort_order). 기존 문구 유지, WP0c anchor validator PASS.
- 새 contract SHA: **`7845f9a5c8eabaa9c3739159f5ee9ad823d82acac4cf4250187d43ec53d5028c`** (9,746 B)
- 이전 v1.1 SHA `49133125…`는 git(3f358ca)에 보존. WP1.1 validator를 재실행하면 `contract_current_sha`
  체크는 이제 FAIL이 정상(계약 개정 때문, WP1 산출물 자체는 불변).

## 3. 산출물 B: S2 full 빌더/validator (코드 완료, full 미실행)

- `canonical/vnext_gallery_full.py` (`7c75767c…`), 독립 `canonical/vnext_gallery_full_validator.py`
  (`0ad84ee8…`, builder·WP1·S1 registry 미import), CLI `tools/run_vnext_gallery_full.py`,
  `tools/validate_vnext_gallery_full.py`, 테스트 `tests/test_vnext_gallery_full_wp4.py`(합성 fixture 10건).
- WP1 재사용: `representative_sort_key/score`, `_retryable`, freeze 로더, `_atomic_json`, `_open_immutable`.
  WP1 Stage A loader는 run_id 없는 쿼리라 full에 부적합 → run_id 포함 loader를 새로 작성.
- 규약: 모든 행 `record_sha256`(run_id/record_sha 제외 전 컬럼 canonical SHA), run `logical_sha256`
  (테이블별 정렬 record SHA 스트리밍), `.building`→`os.replace`, sidecar 없음, no-clobber,
  `application_id=0x564E5834`(WP1 0x…31, WP1.1 0x…32와 구분), E2/E3/freeze/continuity/계약/WP1 4개 SHA pin.
- 테이블: runs, buildings, gallery_groups, group_members, exclusion_ledger, unresolved_ledger,
  source_occurrences, direct_edge_evidence(`deferred_no_per_asset_verification`), validations, metrics.
- 쿼리 플랜 수정: occurrence(ORDER BY로 전체 스캔) → `CROSS JOIN`; phash_edges → `INDEXED BY` unique index.
  N1000 행 생성 9.2 s → full 추정 ≈15 min + 입력 해시 4×~17 s.

### sort_order 규칙 (근거)
E2 `project_asset_occurrences`(project, role, ordinal, url)는 curated `source_image_occurrences`
position과 **6,000/6,000 일치**(source별 3,000 표본). E3 `source_ordinal`은 전부 `ordinal_is_derived=1`.
→ 건물별 0..k-1, exact class 내 모든 member occurrence의 최소 page position
`(membership_ordinal, source_project_id, cover<gallery, ordinal, occurrence_id)` → 없으면 E3
`(role_rank, source_ordinal)` → 대표 `source_asset_id`. (cover 우선 = 현행 Neon image_order와 동일.)

### URL 정책
- Architizer: `migrate_image_url(E2 canonical_url)` = `https://images.architizer.com` + `quote(path,"/%")` +
  `?width=1680&quality=60` (`architizer_host_migrated_v1`).
- Divisare: 대표 asset의 frozen E2 `occurrence_url` 원문, gallery occurrence 우선, 없으면 cover
  (`divisare_e2_occurrence_url_v1`). 현행 Neon divisare `all_images` 532,539 URL 전부가 E2 occurrence
  URL과 문자 그대로 일치(gallery `//images/f_auto,q_auto,w_auto/…`, cover `//image/upload/c_fit,f_jpg,q_80,w_1200/…`).
- 메모리 내 counterfactual(attested 가정, N1000, 산출물 없음): Divisare 대표 URL 94%(5,248/5,567)가
  현행 Neon 값과 동일; 정렬 근거 100% source_page_order.

## 4. 사다리 (fail-closed 코드 스모크, 실제 not-attested summary 사용)

| 단계 | 건물 | 입력 자산 | gallery | exclusion | unresolved | 빌드 elapsed | validator |
|---|---:|---:|---:|---:|---:|---:|---|
| N10 | 10 (A8/D2) | 90 | 0 | 0 | 90 | 34.9 s | 40/40 PASS (1 s) |
| N1000 | 1000 (A702/D298) | 15,332 | 0 | 23 | 15,309 | 45.0 s | 40/40 PASS (9 s) |

미-attested이므로 success 자산 전부 `source_continuity_not_attested`로 unresolved(설계대로 fail-closed).
**FULL은 실행하지 않음** → `data/enrichment/vnext_gallery_full_v1.db` 경로는 비어 있음.

## 5. 산출물 pin

| 파일 | bytes | SHA-256 |
|---|---:|---|
| `data/reports/vnext_asset_continuity_sample_v1.jsonl` | 998,078 | `086a77dd6d660d3f58606736d6fd11212087a6b9691c3088cab9f508738de9e4` |
| `data/reports/vnext_asset_continuity_sample_v1_summary.json` | 3,795 | `26fd2248f8b7dbe84cd73634c0100274fc36480823e0a8dc0c7ad9051de7f512` |
| `data/reports/vnext_gallery_full_v1_FAIL.json` | 5,237 | `eaf8a9b117f16ce38e0ac9314554aed1df1340f8b645658aba7de73959b88aaa` |
| `data/smoke_vnext/vnext_gallery_full_v1_n10_smoke_failclosed.db` | 253,952 | `ea38913775e6acc883497dfb7342261d9d2516707adc2726bc12d7dab934ca3d` |
| `data/smoke_vnext/vnext_gallery_full_v1_n1000_smoke_failclosed.db` | 23,928,832 | `7d5cf8f8e2cca6675f5628a25dd3469a4d712e17f4bafbb5b6c87b7e0564302f` |
| `data/smoke_vnext/vnext_gallery_full_v1_n10_smoke_failclosed_manifest.json` | | `92ee4a94a92ce3526d1da2f4adadf575d7ce95db92beb897de3fb5a60cfd4604` |
| `data/smoke_vnext/vnext_gallery_full_v1_n10_smoke_failclosed_report.json` | | `17aa087bde521c12709959b90d568dde7f56cd15b8f1e5a5b05af621691e210c` |
| `data/smoke_vnext/vnext_gallery_full_v1_n10_smoke_failclosed_validation.json` | | `952251e81f6908dbfc70fc94f0e4a72f42458344e713f77e14d50f6614840f11` |
| `data/smoke_vnext/vnext_gallery_full_v1_n1000_smoke_failclosed_manifest.json` | | `16c1b960c989a5987c7d29c4e1551c1a96a68d6ec1ef534c84170471757befc0` |
| `data/smoke_vnext/vnext_gallery_full_v1_n1000_smoke_failclosed_report.json` | | `febc0958ca88a73b0dd8aebd314b349d5b2465b94d605c974dbb436c8804126a` |
| `data/smoke_vnext/vnext_gallery_full_v1_n1000_smoke_failclosed_validation.json` | | `b24d530b8abde02f725f5146edd3eb09b628734394ecef25343d886c9db5ce7e` |

(위 r1 스모크 8개 파일은 §7 리뷰 수정으로 **superseded**. 삭제·이동하지 않고 증거로 둔다. 새 validator
v1.1로 재검증하면 contract SHA·rule version 불일치로 FAIL이 정상.) 입력: E2 `4728f9f0…7b19`,
E3 `8512e11f…41a4` 전/후 불변, WP1/WP1.1 DB·manifest 4개 SHA 불변, WAL/SHM/journal 없음.

## 6. 검증·미결

- `pytest tests/ -q`: **1300 passed, 23 skipped** (3:57).
- **결정 필요(사용자/Claude):** 크기만 다른 같은 그림(재렌더·반올림)을 어떻게 볼지.
  A) 규칙 유지 → S2 차단, B) §2.1 continuous를 pHash ≤32 + 종횡비 허용으로 개정 후 같은 seed 재실행,
  C) 비율 판정은 유지하되 pHash 동일 그림의 크기 변화는 `reencode_resized`로 분리·S3에서 per-asset 기록.
  결정 후 summary를 새 버전(v2)으로 다시 만들고 N10→N1000→FULL을 재실행(스모크 경로는 새 이름).
- 금요일 축소 릴리스의 gallery 부분은 이 결정 전까지 진행 불가.

## 7. 리뷰 수정 r2 (2026-10-20, 같은 날)

검증된 리뷰 finding을 반영했다. 네트워크·LLM·Vision·Neon·R2 0, 커밋 없음. 입력(E2/E3/curated/registry/WP1)
불변. continuity summary v1(not_attested)은 그대로라 스모크는 여전히 fail-closed.

### 수정 내역
| finding | 조치 |
|---|---|
| M1 Architizer URL ≠ Neon 형식(non-ASCII) | **정책 유지(quote)**, 계약 §2.1의 "현행 Neon과 동일" 문구를 사실대로 수정(E2 success 3,135 자산 non-ASCII path만 문자열 상이). report/metrics에 `architizer_serving_url_neon_divergent` 추가(Neon migration regex 재현, 도구 상수와 동일성 테스트). 형식 통일(a 유지 / b 미인코딩 / c Neon 재이전)은 **사용자 결정 D2**. |
| M2 Divisare 비-raster URL | 서빙 URL = 대표 occurrence 중 gallery→cover, page 순으로 **브라우저 raster인 첫 URL**(`f_jpg` 등 명시 transform 또는 확장자 jpg/jpeg/png/gif/webp/avif/bmp). 없으면 `unresolved`/`no_browser_renderable_serving_url`(engine `serving_url_policy`). URL 재작성(`f_auto`→`f_jpg`)은 하지 않음 → **사용자 결정 D3**. E2 success 9,985 자산(pdf 8,740·ai 766·tiff 397·psd 28·octet-stream 25·eps 22·jp2 7) 해당. N1000: 112 unresolved + 1 cover(f_jpg) fallback (= 리뷰 113). |
| M3 validator 위조 행 통과 | `_replay_building`을 전면 재작성: 모든 테이블의 **모든 저장 컬럼**(record_sha256 포함)을 E2/E3에서 재유도한 행과 비교(buildings name/e3_successful_asset_count, ledger error_kind/retryable/pixel_verification_status/e1_exclusion_reason/decision_engine, occurrence resolution_status/raw_asset_key/detail_json, group 대표 candidate/크기/score/anchor/url_provenance/occurrence id, member node/cluster/크기, edge pair_id/hamming/scope/edge SHA). 리뷰어 `forge.py`를 r2 N1000 사본에 재실행 → `passed=False (per_building_replay)`. |
| m1 metadata_9_16 / same-node | 두 E2 scope 모두 보존(`EDGE_SCOPES`, hamming≥1). same-node 서로 다른 class는 E2 edge 행이 없으므로 행은 만들지 않고 `same_phash_node_group_pairs`로 집계 + §2.1 명시. |
| m2 §1 vnx_bld 키 | §2.1에 "S2 full v1 키 = (source, source_building_id), vnx re-key는 P3" 추가. |
| m3/m8 probe ≠ 서빙 profile | §2.1에 "attestation은 그림 동일성만 증명, 서빙 rendition은 증명하지 않음" 명시. 서빙 profile 표본 GET은 하지 않음(네트워크 0 범위) → 결정 노트 참고. |
| m4 Check TypeError | `Check("artifact_exists", False, True, False)` + 테스트. |
| m5 manifest·run 행 신뢰 | `--manifest`(output SHA/size, run·logical·config, row_counts, input pins) 검사, E2/E3 complete run이 정확히 1개이고 runs.*_run_id와 같아야 함, config_sha·canonical config_json·config scope/version·run_id 재계산. |
| m6 테스트 공백 | not-attested e2e, 7개 테이블 위조(SHA 재계산) 파라미터 테스트, manifest/run 변조 테스트, missing artifact, subprocess `sys.modules` 독립성, raster/Neon-form 단위 테스트. WP4 테스트 10→22. |
| m7 job card SHA 누락 | §5에 r1 스모크 JSON 6개 SHA 추가(나머지 2개 DB는 기존). |

버전: builder `vnext-gallery-full-builder-v1.1`, URL policy `vnext-gallery-serving-url-v1.1`
(Divisare label `divisare_e2_raster_occurrence_url_v1_1`), edge evidence `vnext-gallery-deferred-edge-evidence-v1.1`,
validator `vnext-gallery-full-independent-validator-v1.1`. FULL 출력 경로 `data/enrichment/vnext_gallery_full_v1.db`는 여전히 비어 있음.

### 계약
- 새 contract SHA: **`9a0d1f26ed9c1ef44ff3a4914e0c7be375995f35f3bf1f442b7a67aaa7975d74`** (이전 `7845f9a5…028c` 대체).

### 사다리 r2 (fail-closed, 같은 not-attested summary)
| 단계 | 건물 | 입력 | gallery | exclusion | unresolved (not_attested / no_raster_url / no_e3_cand) | edges | 빌드 | validator |
|---|---:|---:|---:|---:|---|---:|---:|---|
| N10 | 10 | 90 | 0 | 0 | 90 (90/0/0) | 0 | 36.1 s | 51/51 PASS (manifest 포함, 0.7 s) |
| N1000 | 1000 | 15,332 | 0 | 23 | 15,309 (15,197/112/0) | 0 | 47.3 s | 51/51 PASS (manifest 포함, 9.7 s) |

attested 가정 counterfactual(N1000, scratchpad, repo 미기록): groups 15,173, members 15,197, deferred edges 25
(metadata_9_16 추가분 0), same-node group pairs 9, Architizer Neon 형식 상이 16, sort 100% page order,
Divisare f_auto 비-raster 서빙 URL 0, **독립 replay 실패 0**.

| 파일 | bytes | SHA-256 |
|---|---:|---|
| `data/smoke_vnext/vnext_gallery_full_v1_r2_n10_smoke_failclosed.db` | 258,048 | `706d0af88f76965e0db8c31620c3642753e66f694cc56d8201fb86f0a9f12e60` |
| `data/smoke_vnext/vnext_gallery_full_v1_r2_n10_smoke_failclosed_manifest.json` | 2,793 | `ceedec3b8c24e3e2bf5ddd912bc5de7a7b6dc35b2a9986b1715c0dac2527acf1` |
| `data/smoke_vnext/vnext_gallery_full_v1_r2_n10_smoke_failclosed_report.json` | 5,732 | `ef17ca7c8a5e65e7f222a557b81c9f7dea5b48a0052432a31f6d661db4c22b6d` |
| `data/smoke_vnext/vnext_gallery_full_v1_r2_n10_smoke_failclosed_validation.json` | 24,088 | `3ede4290e4e9755230a5e5e2e28490595f970e5edbd8db59c6939eaa29a4998f` |
| `data/smoke_vnext/vnext_gallery_full_v1_r2_n1000_smoke_failclosed.db` | 23,932,928 | `cf806307f99cdfc29e1ed26437155b6cde0c066bb2577bcf1fa250218c8fa7ba` |
| `data/smoke_vnext/vnext_gallery_full_v1_r2_n1000_smoke_failclosed_manifest.json` | 2,807 | `1d1d05028453a7bce7ed89765c64493aecde04e0cf5027d2deb0517fa05f4e5d` |
| `data/smoke_vnext/vnext_gallery_full_v1_r2_n1000_smoke_failclosed_report.json` | 5,929 | `0d1db981fae9c663da373a539fc7f509a951f99dbb6d37d366cf7545803043a0` |
| `data/smoke_vnext/vnext_gallery_full_v1_r2_n1000_smoke_failclosed_validation.json` | 190,552 | `dc66ac83765b5c6cb4420e1d288d2c11de004302381c923ff54e8ec0a0127bcf` |

E2/E3 SHA 전/후 불변(빌더 pre/post 해시), WP1/WP1.1 4개 SHA 불변, WAL/SHM/journal/.building 없음.

### 열린 결정 (사용자)
- **D1** continuity 크기 규칙 A/B/C (§6, 변동 없음). B/C를 고르면 attestation이 identity만 증명한다는
  §2.1 문구와 함께, 서빙 profile 표본(status + content-type)을 v2 summary에 추가할지 함께 결정.
- **D2** Architizer non-ASCII URL 형식: (a) quote 유지 + S6/URL-join에서 3,135건 차이 감수, (b) Neon처럼 미인코딩,
  (c) Neon을 quote 형식으로 재이전.
- **D3** Divisare 비-raster 9,985 자산: 현재 fail-closed unresolved. 대안 = `f_auto`→`f_jpg`(+`pg_1`) 재작성으로 gallery
  편입(현행 Neon에도 같은 깨진 타일이 있을 가능성).

### 검증 (r2)
- `pytest tests/ -q`: **1312 passed, 23 skipped** (3:41). WP4 테스트 22건 포함, WP0c 계약 테스트 PASS.
- validator v1.1: N10/N1000 r2 둘 다 51/51 PASS(`--manifest` 포함). 리뷰어 위조 재현 → FAIL(정상).

## 8. r3 (2026-10-23): 결정 D1/D2/D3 반영 → continuity v2 PASS → FULL 빌드 PASS

지시서 `20261020_vnext_wp4_s2_full_order.md` §결정(2026-10-23 09:15 KST) 적용. 유료 LLM/Vision **$0**,
Neon/R2 읽기·쓰기 0, 커밋 없음. 네트워크 = continuity 표본 HTTP GET 2,000건(승인 범위, 4 workers ≤4 rps).

### 8.1 변경
| 결정 | 구현 |
|---|---|
| D1 = B | `tools/vnext_asset_continuity_sample.py`에 `--rule-version {v1,v2}`(기본 v2). 규칙별 seed·출력 경로 분리, v1은 그대로 재현(v1 jsonl 2,000행 `classify(row,"v1")` 재분류 불일치 0). v2: continuous = 200 ∧ Hamming ≤ 32 ∧ 종횡비 상대차 `abs((w·h_E1)/(h·w_E1) − 1)` ≤ 2%, content_changed = 200 ∧ (Hamming > 32 ∨ 종횡비 > 2%). summary에 `rule_version=vnext-asset-continuity-rule-v2`. builder·validator는 summary의 rule/seed가 v2가 아니면 거부. |
| D2 | Architizer 서빙 URL = `tools/architizer_image_host_migrate.py` `PATTERN`/`REPLACEMENT` regex 치환 결과 그대로(path 미인코딩, `?width=1680&quality=60`). E2 `canonical_url`이 PATTERN에 fullmatch해야 함(아니면 unresolved; 실측 E2 Architizer success 884,249건 전부 fullmatch, query 있는 URL 0). `migrate_image_url` import 제거. policy `architizer_neon_regex_host_rewrite_v1_2`. validator가 같은 규칙을 독립 재현, `architizer_url_neon_form_identical` 검사 추가(테스트로 도구 상수와 동일성 고정). |
| D3 | 유지: 비-raster Divisare → `unresolved`/`no_browser_renderable_serving_url`. |
| 계약 | §2.1 v1.3 문구(규칙 v2, seed v2, D2 Neon 형식, D3 확정). 새 contract SHA **`81687d1b04f312903e6afea3829edde92ab44d107be02f82d81adaaa2104c201`** (13,030 B, 이전 `9a0d1f26…5d74` 대체). WP0c anchor validator PASS. |

버전: builder `vnext-gallery-full-builder-v1.2`, contract `vnext-gallery-contract-v1.3`, URL policy
`vnext-gallery-serving-url-v1.2`, validator `vnext-gallery-full-independent-validator-v1.2`, continuity tool
`vnext-asset-continuity-sample-v2`. WP4 테스트 22 → 24(v1 재현, v2 경계, Neon 형식 동일, v1 summary 거부).

### 8.2 continuity v2 표본 — 양 source `continuity_attested` (status PASS)
seed `vnext-asset-continuity-v2`, source별 E2 success 1,000, 2026-10-23 00:15:49–00:28:21Z, **744.6 s**,
요청 2,000(재시도 0), 표본 manifest SHA `b673725a…9a87`, 도구 SHA `82826d09…2a7d`, E2 SHA 전/후 불변.

| source | n | continuous | content_changed | unavailable | rate | Hamming max / p99 | 종횡비 상대차 max | 크기 변화 max | pix_eq | 판정 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| architizer | 1000 | 1000 | 0 | 0 | 1.000 | **32** / 16 | 0.49% | 5 px | 0 | continuity_attested |
| divisare | 1000 | 1000 | 0 | 0 | 1.000 | 14 / 0 | 0.08% | 182 px | 999 | continuity_attested |

참고(규칙 조정 없음): Architizer 1건(`atz_asset_afcbc18f…`, 크기 동일 1024×549)이 Hamming **32 = 임계값 경계**,
다음 26, 나머지 ≤ 18. 2% 경계 ±1e-6 행 0. 경계 사례는 S3 per-asset 픽셀 pass에서 다시 본다.
(2026-10-23 리뷰가 v2 jsonl에서 재계산: Architizer Hamming > 26은 [32] 1행뿐, Divisare max 14 → 규칙 ≤32상 continuous,
gate 변경 없음. S3 watch list 유지.)

### 8.3 사다리 + FULL (같은 v2 summary, 같은 contract)
| 단계 | 건물 (A/D) | 입력 자산 | gallery member | group | exclusion | unresolved | deferred edge | 빌드 | validator |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| N10 | 10 (8/2) | 90 | 90 | 90 | 0 | 0 | 0 | 33.7 s | 53/53 PASS (`--manifest`, <1 s) |
| N1000 | 1000 (702/298) | 15,332 | 15,197 | 15,173 | 23 | 112 | 25 | 44.2 s | 53/53 PASS (`--manifest`, 10 s) |
| **FULL** | **91,803** | **1,432,588** | **1,419,480** | **1,416,641** | **3,165** | **9,943** | **2,136** | **532.4 s** | **54/54 PASS** (`--manifest --verify-input-hashes`, 598 s) |

N1000은 r2 counterfactual(groups 15,173 / members 15,197 / edges 25)과 정확히 같다.
FULL phase: 입력 SHA pre 15.9 s, 행 생성 428.3 s, 검사+logical 70.6 s, 입력 SHA post 15.5 s.
run `vnx_gallery_full_2824179e4d9ff0cc661838aa`, logical SHA `ab8ebc1050a43c838af22a7ec7fd0a385a4344477e364dbd9507e7252e418d34`.

**FULL source별**
| 항목 | architizer | divisare | 합계 |
|---|---:|---:|---:|
| buildings | 61,912 | 29,891 | 91,803 |
| (입력 자산 0인 건물) | 0 | 54 | 54 |
| 입력 자산 | 885,336 | 547,252 | 1,432,588 |
| gallery member | 884,127 | 535,353 | 1,419,480 |
| gallery group (대표) | 881,796 | 534,845 | 1,416,641 |
| source occurrence | 947,322 | 577,112 | 1,524,434 |
| exclusion: `e1_ledger_excluded` | 1,014 | 23 | 1,037 |
| exclusion: `fingerprint_or_decoder_failure` | 68 | 1,902 | 1,970 |
| exclusion: `low_information` | 127 | 31 | 158 |
| exclusion 합계 | 1,209 | 1,956 | 3,165 |
| unresolved: `no_browser_renderable_serving_url` (D3) | 0 | 9,943 | 9,943 |
| unresolved: `source_continuity_not_attested` / `successful_e2_asset_missing_e3_candidate` | 0 / 0 | 0 / 0 | 0 |
| deferred direct edge (`deferred_no_per_asset_verification`) | 1,698 | 438 | 2,136 |
| same pHash node group pair (evidence 행 없음, 집계만) | 1,729 | 77 | 1,806 |
| Architizer 대표 URL non-ASCII path (D2, Neon과 동일 문자열) | 3,071 | – | 3,071 |
| Architizer Neon 형식 불일치 | 0 | – | 0 |

회계: 1,419,480 + 3,165 + 9,943 = 1,432,588 (건물별·전체·테이블 행 모두 일치). sort 근거 100% source page
order(1,416,641 / e3_rank 0). star 부착 0, multi-pixel group 0. Divisare unresolved 9,943 vs r2에서 센 E2 success 비-raster 9,985 — **분해 완료(2026-10-23 리뷰, 검증됨)**:
비-raster gallery occurrence가 있는 success 자산 9,985 − 다른 raster occurrence(cover `f_jpg` 경로)로 서빙 가능 41
= raster occurrence가 전혀 없는 자산 9,944 − 앞선 exclusion `low_information` 1 = **9,943** = `unresolved_ledger` 행 수
= distinct unresolved 자산 수. unresolved 자산 중 raster occurrence 보유 0. (스크립트 `C:/Users/user/AppData/Local/Temp/claude/C--Users-user-reference-crawling/c1cc4388-e0ee-442c-8371-05d6e3a8c7fc/scratchpad/fri_review_s2/gap.py`,
frozen E2 + FULL gallery DB read-only, 재실행 결과 동일.)

**서빙 rendition 크기(리뷰 관찰, S2 변경 없음)**: live GET 30건 모두 200·image/*·decode OK, E1 대비 종횡비 차 ≤0.17%.
단 rendition 크기 상한이 없다 — Divisare `f_auto,q_auto,w_auto` 원본 예 2,582,980 B(4961×2030), 1,985,036 B(5434×6617),
Architizer PNG(`…Manuel_S%C3%A1.png?width=1680&quality=60`) 4,971,935 B. Divisare URL은 frozen `//images/f_auto,q_auto,w_auto/`
(이중 슬래시) 그대로이며 현재 동작하나 정규화되지 않았다. 계약 §2.1은 renderability만 보장(크기 보장 없음) → 위반 아님.
보류 중인 Divisare URL rewrite 결정(예: `w_`/`c_limit` 상한 변환)과 make_web 이미지 사이징의 입력으로 기록.

### 8.4 산출물 pin
| 파일 | bytes | SHA-256 |
|---|---:|---|
| `data/reports/vnext_asset_continuity_sample_v2.jsonl` | 1,045,434 | `cf38dd572f99f9ce35ba9513870af31d24189175549fefe5ad097e13e62caf0c` |
| `data/reports/vnext_asset_continuity_sample_v2_summary.json` | 4,125 | `72fe45a45ccd7d4fe2076d3f4b9477ae9c75841b25fdedcb31936e6b597a3f44` |
| `data/enrichment/vnext_gallery_full_v1.db` | 5,148,098,560 | `7d7c4a239efd5d945c341b5b61a8fad4dce233486dcf616fa1904cfd2862722d` |
| `data/reports/vnext_gallery_full_v1_manifest.json` | 2,793 | `e578fed5521ae0734614dc4e7109b6e2bab83136cb3c8117f752e66ce03b0d29` |
| `data/reports/vnext_gallery_full_v1_report.json` | 6,699 | `07713bd3ad27ea82139dca247d0a865c227370a104159e419d589f794920e14a` |
| `data/reports/vnext_gallery_full_v1_validation.json` | 15,352,747 | `bd6b50b6501cdc97df3b0e4d53023f7e593fde49252cf72d5e785be6a2811105` |
| `data/smoke_vnext/vnext_gallery_full_v1_r3_n10_smoke.db` | 438,272 | `421207ac988145476baec58bcb4cccd64c92ba3b91a3828a87329fce5aaac7c6` |
| `data/smoke_vnext/vnext_gallery_full_v1_r3_n10_smoke_manifest.json` | 2,783 | `82fc48a7485b0dd03df1c2c63e579203467b0ad4a03bede58c1b7016bffda726` |
| `data/smoke_vnext/vnext_gallery_full_v1_r3_n10_smoke_report.json` | 6,297 | `8ea3d3f7e9164165af8054b6c0d324029f0ed2952c5d0a60a1c3d60f1238f53e` |
| `data/smoke_vnext/vnext_gallery_full_v1_r3_n10_smoke_validation.json` | 25,151 | `343c0e5285df5b42dab07b55ebf651de41cdb68585d3d28489f3257ae68fec3b` |
| `data/smoke_vnext/vnext_gallery_full_v1_r3_n1000_smoke.db` | 55,078,912 | `28d65479c45d73a0e21476f66368d8f37d441fdc17a935d66d29387d2cce2f53` |
| `data/smoke_vnext/vnext_gallery_full_v1_r3_n1000_smoke_manifest.json` | 2,803 | `a2f32527dad1e044e8f5a50da612717b81bddc368d63dde92ffb94eba0eb8973` |
| `data/smoke_vnext/vnext_gallery_full_v1_r3_n1000_smoke_report.json` | 6,562 | `08c7168fa1f2e2c8ca6928516de5ab5bd05fde4459a894d273410234cf578bdb` |
| `data/smoke_vnext/vnext_gallery_full_v1_r3_n1000_smoke_validation.json` | 191,656 | `0b89628cc99ad18b7f32b23e023429f78074997fd9eb9cd0f3ac1ea0d2ab32e6` |
| `docs/vnext/CONTRACT_GALLERY_SEMANTICS.md` (v1.3) | 13,030 | `81687d1b04f312903e6afea3829edde92ab44d107be02f82d81adaaa2104c201` |

코드: `tools/vnext_asset_continuity_sample.py` `82826d09…2a7d`, `canonical/vnext_gallery_full.py` `b1920e28…6058`,
`canonical/vnext_gallery_full_validator.py` `184fabe6…328c`(builder·WP1·S1 registry 미import, subprocess 테스트),
`tools/run_vnext_gallery_full.py` `6fb7454e…b0a`, `tools/validate_vnext_gallery_full.py` `fe31e6e6…e7f9`,
`tests/test_vnext_gallery_full_wp4.py` `9a829c3f…847`.
입력: E2 `4728f9f0…7b19`, E3 `8512e11f…41a4` 빌더 pre/post + validator 현재 재해시 모두 일치. WP1/WP1.1 4개 SHA
불변. WAL/SHM/journal/`.building` 없음. r1·r2 스모크와 v1 continuity 표본·FAIL 리포트는 그대로 보존(superseded).

### 8.5 검증
- validator v1.2: N10 53/53, N1000 53/53, FULL 54/54 PASS(FULL은 `ladder_seed` 대신 `--verify-input-hashes`의 E2/E3 현재 SHA 2건 추가).
- `pytest tests/ -q`: 수정 직후 1회 **1314 passed, 23 skipped** (3:42); FULL 후 재실행 **1314 passed, 23 skipped** (3:25).
