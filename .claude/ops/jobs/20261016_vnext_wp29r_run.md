# WP2.9-R 실행 기록 — 2026-10-16 (run `wp29r_20261016`)

**결과: PASS. 로컬 S1 병합 1,534건을 반영했고 사용자 100쌍 감사를 기다린다.** 상세 내용은 `data/reports/vnext_identity_registry_v1_report.md`에 있다.

## 범위
- 지시서: `20260907_vnext_wp29r_resume_order.md`(재개)와 `20260827_vnext_wp29_s1_registry_order.md`(기본).
- 실행 순서: Stage A 재계산·동일성 검증 → 크기 3 감사 적용 → Stage B registry → Stage C 감사 패킷.
- 실행 방식: 독립 validator를 돌렸다. 실패 시 정지하도록 했다(FAIL 게이트는 `_v1r_FAIL.json`).
- 바꾸지 않은 것: 티어 규칙, 사전 선언, 감사 기준, 앱. 커밋하지 않았다.

## 입력 SHA pin (전부 검증 통과)
| SHA256 (앞 16) | 경로 |
|---|---|
| 6fd7b5077803666e | data/reports/vnext_identity_candidates_v4_manifest.json |
| 5ab133138c26ca6d | data/enrichment/vnext_identity_candidates_v4.db |
| cd787a0088161fd0 | data/reports/vnext_workspace_freeze_v1.json (WP0a) |
| 17e8da1d74f1508b | data/canonical/legacy_neon_snapshot_2026q3.jsonl (WP0b) |
| 4728f9f015d7fba3 | data/enrichment/divisare_architizer_image_evidence_e2_full_v5.db (E2) |
| 9c523f3393d20ae8 / 605f4f534fc74267 | curated divisare v2_4 / architizer v2_0 |
| 869a79fee9fd65dd / 58aecdcda936f732 / 8512e11f8e1fd581 | E1 divisare / E1 architizer / E3 |
| 502e6258b9c7803e | docs/vnext/CONTRACT_IDENTITY.md |
| 2eee544c12fac5dd | data/reports/vnext_identity_registry_v1_predeclared.json |
| 2241f2979f32ad7a | data/reports/vnext_identity_registry_v1_stage_a.json (원본 Stage A) |
| 540b7a1ce365574d | data/reports/vnext_identity_registry_v1r_size3_audit.json |
| 8aa98a37d7cbd637 | data/reports/vnext_identity_registry_v1_FAIL.json (이전 실패 기록, 보존) |
| a48c81a577b6ef0e / c5a5b80e6eb6a845 | 기본 지시 / 재개 지시 |
| eb88560018030332 | tools/vnext_merge_audit_app.html (불변) |
| a4e68622ed82a126 / 8bbdf543e590f9ae | producer / validator 코드 |

## 산출물 SHA256
| SHA256 (앞 16) | 경로 |
|---|---|
| 4e7f45e93b8f9d8c | data/reports/vnext_identity_registry_v1r_stage_a_reuse.json |
| d0cbdf0febcd7818 | data/enrichment/vnext_identity_registry_v1.db (526.6 MB) |
| b988ecfd729fa3b1 | data/reports/vnext_identity_registry_v1_manifest.json |
| a96038b9bf75520c | data/reports/vnext_identity_registry_v1_stage_b_validation.json (PASS 60/60) |
| de4322b63bda9cd9 | data/reports/vnext_identity_registry_v1_sample_selection.json |
| 0e798bd50dfc2738 | data/reports/vnext_identity_registry_v1_sample_manifest.json |
| 7b218d2336322828 | tools/vnext_merge_audit_pairs.js |
| 92e53dfbfb315206 | data/reports/vnext_identity_registry_v1_stage_c_validation.json (PASS 1,623/1,623) |

## 수치
- **Stage A**
  - v4 후보 68,049쌍, merge_candidate 5,100쌍. 티어는 1,534쌍에서 1,534쌍으로, 줄지 않았다.
  - component 1,524개 (크기 2는 1,514개, 크기 3은 10개). 원본과의 동일성 검증은 13개 필드와 non-code pin 전부 일치.
- **제외**
  - 크기 3 component 10개는 모두 `within_source_duplicate`로 분류됐고, 반증 검토에서 뒤집힌 판정은 0건이다.
  - 그래서 제외 component 0개, 제외 건물 0개.
- **vnx ID**
  - 발급 91,803 / active 90,269 / alias 1,534. 병합으로 줄어든 건물은 1,534.
- **이월 edge (disposition)**
  - merged_rule_tier 1,534 / carry_merge_candidate 3,566 / carry_defer 2,192
  - carry_related_but_separate 4 / carry_excluded_component 0 / preserved_reject 60,753
- **crosswalk**
  - legacy 측: one_to_one 39,142 / one_to_many 314 / many_to_one 4 / conflict 6 / unmatched 12 (합계 39,478).
  - vnext 측: one_to_one 39,142 / one_to_many 654 / many_to_one 2 / conflict 3 / unmatched 50,468.
- **legacy PK 승계 가능: 39,142.**
- **Stage C 표본** (100쌍)
  - 필수 크기 3 쌍 20/20, `size_2|identical_phash_only` 80쌍. seed 고정.
  - 라벨 0. 추천·제안 필드 누출 0. node 파싱 OK.
- **이미지** (정책 `predeclared_live_only`)
  - HEAD 380건, body 0바이트. Divisare는 179건 200 / 1건 404, Architizer는 200건 전부 410.
  - Divisare 측: 99측 2장, 1측 비어 있음 (`div_bld_144914`, 이미지 1개 404).
  - Architizer 측: 100/100 비어 있음 (CDN 410).
  - 빈 측은 모두 101, 양쪽 모두 이미지 없는 쌍은 1.

## Stage C 재생성 (Architizer host 이전, 2026-10-16)

- **왜:** `architizer-prod.imgix.net`이 410을 반환해 Architizer 100측이 모두 비어 있었다. Architizer가 이미지 host를 옮겼다.
  같은 path가 `https://images.architizer.com`에서 같은 파일로 제공된다(Architizer 자체 og:image와 같은 형식).
- **정책 `predeclared_live_host_migrated`:** 선택 규칙(측별 첫 두 asset)과 liveness 규칙(선택 URL 전부 live여야 하며, 아니면 측을 비움)은 그대로 둔다.
  - 원본 host가 `architizer-prod.imgix.net`일 때만 URL을 `https://images.architizer.com/<같은 path>?width=1680&quality=60`로 바꾼다.
  - 기존 imgix 쿼리는 버린다. 비ASCII path는 `quote(path, safe="/%")`로 인코딩한다. 다른 host는 바꾸지 않는다.
  - 이 매핑은 순수 결정적 함수다. validator에 import 없이 따로 복제해 독립 replay한다.
- **provenance:** sample manifest에만 기록한다. 측 단위는 `side_image_provenance`(`original` 또는 `host_migrated:architizer-prod.imgix.net->images.architizer.com`)이고, asset 단위는 `checked_url`·`url_provenance`다. 공개 pair 데이터에는 넣지 않았다.
- **불변:** 표본 100쌍과 seed는 같다. `_sample_selection.json`은 SHA `de4322b6…`로 바이트까지 동일하다. pairs.js에서 이미지 외 필드도 모두 동일하다.
  티어 규칙, 사전 선언, 기준, 앱(`eb885600…`), registry DB(`d0cbdf0f…`)도 그대로다. Stage B는 재실행하지 않았다.
- **코드 pin:** Stage C 코드만 바뀌었다. non-code pin은 전부 엄격히 재검증했다. 코드 SHA는 manifest `code_pins`에 기록했다.
  - Stage B 시점: producer `a4e68622…`, validator `8bbdf543…`
  - Stage C 시점: producer `c77ab192…`, validator `1e54a735…`
- **liveness:** HEAD 380건(동시성 4, timeout 15초)을 보냈고 body는 0바이트다.
  - `images.architizer.com` 200건: 전부 200 image/*.
  - Divisare 180건: 200이 179건, 404가 1건.
- **측별 이미지:**

| 측 | 2장 | 빈 측 |
|---|---|---|
| Architizer | 100 | 0 |
| Divisare | 99 | 1 (`div_bld_144914`, 404, 이전과 같음) |

  빈 측은 모두 1개(이전 101개)이고, 양쪽 모두 이미지가 없는 쌍은 0개다. 방출 이미지는 398장이다.
- **이전 산출물 보존:** 삭제하지 않고 이름만 바꿨다.
  - `tools/vnext_merge_audit_pairs_prev_live_only.js` `7b218d23…`
  - `_sample_manifest_prev_live_only.json` `0e798bd5…`
  - `_sample_selection_prev_live_only.json` `de4322b6…`
  - `_stage_c_validation_prev_live_only.json` `92e53dfb…`
- **새 SHA256:**
  - `tools/vnext_merge_audit_pairs.js` `c0e80f73d36175c08aa2fdbe5ffde0daa82d4758d7e97626ed56ac5978a0a762`
  - `_sample_manifest.json` `ec1a756762c7a0d8ca30de0c0aeb4c97ba70e2d4141fc45262fc9f8a4662eba6`
  - `_sample_selection.json` `de4322b63bda9cd9af8b4ee88b505abf7fc28969ca5729c9f57dc75155993c22` (불변)
  - `_stage_c_validation.json` `3d23b446f7b95cde8097a574b521284f46e5fbdefeeccdd19e646f91a7502f9d` (**PASS 2,025/2,025**)
- **테스트:** `pytest tests/ -q`: 1290 passed, 23 skipped.
- 라벨은 0개다. 사용자 감사 대기 상태는 그대로다.

## 비용 / 금지 사항
- 비용 $0 (유료 API 0, LLM 0). 네트워크는 무료 CDN HEAD 380건 + Stage C 재생성 HEAD 380건뿐이다.
- Neon 쓰기 0, R2 쓰기 0, canonical_v2 접근 0. 감사 라벨은 미리 채우지 않았다.

## 다음
- 사용자가 `tools/vnext_merge_audit_app.html`로 100쌍을 감사한 뒤 JSONL을 전달한다.
- 판정 기준(사전 선언): different가 0건이면 확정한다. different가 1건 이상이거나 related가 5건 이상이면 정지하고 사용자 결정으로 넘긴다.
- (해소됨) Architizer 측 이미지 부재는 host 이전 정책 `predeclared_live_host_migrated`로 Stage C를 재생성해 해결했다(100/100측 2장). `live_then_local` 결정은 더 이상 필요 없다.

## 독립 검증 (2026-10-16, read-only, validator 미사용) — **PASS 14/14**
자체 SQL/python으로 pinned v4 DB(immutable)와 registry DB(ro)에서 재계산했다. 코드와 산출물은 수정하지 않았다.
- membership 91,803 = v4 source_buildings 91,803 (집합 일치). 발급 순서는 ordinal 1..91,803 연속이고, `vnx_bld_NNNNNN`이 (source, source_building_id) BINARY 정렬 순위와 일치한다.
- 규칙 티어를 독립 재계산했다: 1,534쌍. union-find 결과 1,524 component(크기 2: 1,514 / 크기 3: 10), 안전 검사 제외 0. 이는 merged edge 1,534, merge decision 1,534와 집합이 같다. 티어 밖 쌍 중 active ID를 공유하는 쌍은 0이다.
- survivor는 1,524개 전부 최소 발급 ordinal이다. active 90,269 / alias 1,534 (aliases 테이블 1,534).
- 68,049 v4 쌍이 모두 edge로 남아 있고 disposition 분포는 기대값과 일치한다. 제외 component가 0이므로 carry_excluded_component도 0이다.
- crosswalk: legacy 측 합계 39,478 = WP0b 스냅샷 ID 39,478 전량. vnext 측 합계 90,269 = active 수. edge 39,806. one_to_one 39,142건은 양방향 차수가 모두 1이고, inherited_legacy_pk 39,142와 일치한다.
- 표본: 100쌍, 고유 ID 100. 재계산한 크기 3 쌍 20개가 전부 포함됐다. seed `vnext-wp29-merge-audit-20260827-v1`로 size-2 80쌍과 표시 순서를 재현했다.
- pairs.js: node 파싱 OK. 라벨·제안 키 0, 금지 토큰 0. human_reviews 0행.
- SHA: 입력 pin 20개와 registry DB 모두 일치한다. app.html은 git HEAD blob과 동일하다.
- `pytest tests/ -q`: 1280 passed, 23 skipped.

## Stage C 재생성 독립 검증 (2026-10-16, read-only) — **PASS 10/10**
자체 python으로 확인했다. E2 DB와 registry DB는 immutable/ro로 열었다. 코드와 산출물은 수정하지 않았다.
- 후보 ID: 새 pairs.js 100개(고유 100)의 ID와 순서가 `_prev_live_only.js`와 같다. `_sample_selection.json`과도 같다(SHA `de4322b6…` 불변).
- 이미지 외 필드는 100쌍 모두 이전과 같다(차이 0). 최상위 키 run_id와 sample_version도 같다. 이미지가 바뀐 측은 100개이고, 모두 Architizer 측이다.
- Architizer: 100측 모두 2장씩, 200 URL이다. 200/200이 `https://images.architizer.com`에 있고, query가 정확히 `width=1680&quality=60`이다. 경로는 E2 `first_two_assets`를 독립 SQL로 재계산한 원본 경로와 200/200 일치한다(비ASCII 4건은 percent-encoding).
- Divisare: 100측 모두 이전 packet과 같다. E2 원본 URL과도 100/100 일치한다. 2장 99측, 빈 측 1개(이전과 동일).
- 라벨·제안 키: 0개. JS 키는 id/left/right(source,name,architect,year,city,country,url,images)/evidence(name_score,firm_score,year_gap,exact,identical_phash,component_size)뿐이다. provenance와 checked_url은 JS에 없다.
- app.html SHA `eb885600…`은 git HEAD blob과 같다.
- registry DB SHA는 `d0cbdf0febcd7818…`로 그대로다.
- 실시간 스팟체크: 랜덤 20 URL에 Range GET(0-1023)을 보냈다(seed 20261016). 20/20이 image/jpeg로 응답했다. Architizer 12건은 206, Divisare 8건은 200이었다. 추가로 비ASCII Architizer 4건도 4/4가 206 image/jpeg였다.
- validator `--stage c` 재실행: PASS 2,025/2,025. 출력은 scratchpad로 보냈다(write-once 파일 보호). checks 목록은 pinned `_stage_c_validation.json`(`3d23b446…`, 불변)과 같다.
- `pytest tests/test_vnext_identity_registry.py -q`: 36 passed.
