# data/ 레이아웃 (2026-10-16 기준)

`data/`에 무엇을 두고 왜 두는지, 무엇을 아카이브로 옮겼는지(옮길 예정인지), 다른 PC에서 vNext를 이어가려면 무엇이 필요한지 정리한 문서다.
크기는 10진 GB(1 GB = 10^9 B)이고, `os.scandir`로 2026-10-16에 측정했다.

> 상태: **이동 완료 (2026-10-16 23:2x KST).** 계획 `C:/Users/user/reference-crawling_archive_20261016/data_cold_PLAN.tsv`는
> 처음 380행(17.60 GB)이었고, challenge 검토에서 48행(8.65 GB)을 빼 **332행(8.95 GB)**을 옮겼다.
> 332개 모두 OK, 건너뜀 0, 되돌림 0. 삭제는 하지 않았다. 로그: `.move_log_data_cold.tsv`.

## 1. 현재 규모

| 디렉터리 | 이동 전 | 옮김 (개수) | 이동 후 (실측) |
|---|---|---|---|
| data/enrichment | 46.92 GB | 7.24 GB (114) | 39.68 GB |
| data/curated | 13.04 GB | 1.36 GB (11) | 11.68 GB |
| data/reports | 1.18 GB | 0.35 GB (183) | 0.83 GB |
| data/canonical | 0.73 GB | 0 | 0.73 GB |
| data/crawl | 0.09 GB | 0 | 0.09 GB |
| data/smoke | 0.01 GB | 0.01 GB (24) | 0 |
| **합계** | **61.97 GB** | **8.95 GB (332)** | **53.02 GB** |

## 2. data/에 남는 것 (최소 작업 세트)

| 묶음 | 크기 | 남기는 이유 |
|---|---|---|
| Frozen 입력 6종: `curated/architizer_curated_v2_0.db`, `curated/divisare_metadata_v2_4.db`, `enrichment/*_image_fingerprints_e1_full_v1_2.db` x2 (+`.lock`), `enrichment/divisare_architizer_image_evidence_e2_full_v5.db`, `enrichment/divisare_architizer_image_selection_e3_full_v1.db` | 38.4 GB | `vnext_workspace_freeze_v1.json`과 registry manifest가 SHA를 pin했다. WP2.9-R 검증이 이 pin을 다시 해시한다. |
| S1 registry: `enrichment/vnext_identity_registry_v1.db`, `reports/vnext_identity_registry_v1*` | 0.53 GB | 지금 감사 중이다. **손대지 않는다.** |
| Candidates 체인 v1→v4 (`enrichment/vnext_identity_candidates_v{1,2,3,4}.db`) | 2.47 GB | v4는 registry 입력이다. v3는 v4 manifest가 pin했다. v2는 `tests/test_vnext_identity_wp22.py`가 SHA를 assert한다. v1은 v2 manifest가 체인으로 pin했다. |
| n100 gold와 캐시 4종 (`vnext_identity_n100_v1.db`, `vnext_p4b_pixel_cache_v1/`, `vnext_identity_{n100_cache_v1,review_cache_v1,review_cache_v3}/`) | 0.13 GB | n100 manifest가 tree SHA를 pin했다. p4b 캐시는 S2 verified-pixel의 기본 경로다. |
| Semantic availability n10 DB와 `_cache` (48개) | 0.50 GB | registry `AVAIL_GLOB`(`live_then_local` 정책)과 `tests/test_vnext_identity_registry.py`가 참조한다. |
| S2 gallery 선행물 (`vnext_gallery_draft_n10_v1*.db`, `reports/vnext_gallery_*`) | ~4 MB | 다음 S2 패키지의 기본 입력이다. |
| `canonical/legacy_neon_snapshot_2026q3.jsonl` (+report, validation) | 0.73 GB | registry의 LEGACY 입력이다. S6 projection이 legacy ID와 R4를 유지할 때 쓴다. |
| `canonical/tag_vocabulary_labels.json`, `remediation_typmat_changed_rem2026q2.jsonl` | 6 MB | git으로 추적한다. tag rebuild와 re-upsert 내구성에 쓴다. |
| `crawl/architizer.db` (+0바이트 placeholder 3개) | 0.09 GB | Stage-1 crawl DB다. D-1과 build_strict의 기본 경로다. |
| `curated/architizer_curated_v1_3.db` | 0.69 GB | **보류(unsure).** v2_0 재빌드의 base이고, 테스트가 이 파일이 있을 때만 검증한다. |
| `enrichment/architizer_awards_v2.db` (+READY) | 8 MB | curated_v2 lineage다. 작아서 그냥 둔다. |
| `enrichment/architizer_source_recrawl_v2.db` + `architizer_html_snapshots_v2/` | 8.43 GB | challenge에서 제외. Stage-1 크롤러 `crawl/architizer/recrawl_v2.py`의 기본 resume/snapshot 경로이고, 대체할 수 없는 원본 크롤이다. |
| ID-matching probe 체인 (band_precision, instrument probe v1/v2, v2/v21 probe DB와 캐시) | 0.17 GB | challenge에서 제외. `canonical/` 검증기와 유지되는 manifest가 경로/SHA로 pin했다. |
| Semantic vision v1/v1_1/v1_2/n100 세트 (27개) + 2026-08-13 handoff receipt 3개 | ~41 MB | challenge에서 제외. `tools/build_current_image_handoff_manifest.py`(테스트 있음)가 SHA로 pin했다. |
| `architizer_curated_v2_reconciliation.db.READY.json`, `reports/architizer_curated_v2_{0,reconciliation}.md`, v2_7_2 availability pin 대상 3개 | 작음 | challenge에서 제외. 유지되는 READY/terminal_pins JSON이 SHA를 pin했다(대문자 SHA 포함). |
| `reports/architizer_host_migrate/` | 0.31 GB | 오늘 Neon 이미지 호스트를 마이그레이션했고, 이 폴더가 유일한 롤백 백업이다. 사용자가 확정하기 전까지 유지한다. |
| `reports/*_blind/` 4개 (band_precision, n100, instrument_probe v1/v2) | 0.47 GB | **보류(unsure).** 종료된 WP 검증기의 기본 경로(`DEFAULT_SHEETS_ROOT`)다. |
| 그 밖의 reports (v1-v4 manifest와 validation, n100/band/instrument의 locked/manifest JSON, freeze, E3 진단, loader report, search_quality) | ~0.06 GB | 다음 단계 검증기가 읽는다. 작다. |

이 머신에 **없는 것**: c27 artifact, `r4_results*.jsonl`, `data/id_registry*.json`, `canonical_architects_v2.json`, D-1/build_strict 산출물.
나중에 복원하면 이 경로들은 보호 대상이다. `id_registry*.json`은 절대 삭제하지 않는다.

## 3. 아카이브로 옮기는 것과 복원 방법

- 위치: `C:/Users/user/reference-crawling_archive_20261016/data_cold/<repo 상대경로>`
  (예: `data_cold/data/enrichment/architizer_curated_v2_reconciliation.db`). 오전 1차 정리분은 `data_superseded/`에 있다.
- 같은 드라이브 안의 rename이라 즉시 끝났다. 로그는 `.move_log_data_cold.tsv`(src, dst, bytes, status), `MANIFEST.md`에 `data_cold` 섹션을 추가했다.
- DB는 사이드카(`.lock`, `.partial`, `.READY.json`, `.manifest.json`, `_cache/`, `_review_cache/`, `_review_sheets/`)와 함께 옮긴다.
- 계획 TSV의 `reason`은 등급 태그로 시작한다.
  - **[A] 317개, 1.91 GB (옮김)**: 코드와 테스트에 참조가 없다. smoke/n10/n100 DB, recovery 잔여물, curated v1_1, v2_0 smoke, `data/smoke`, 대형 실패/race JSON 등이다.
  - **[B] 15개, 7.04 GB (옮김)**: 끝난 빌더의 *기본 경로*로만 참조된다. reconciliation DB 6.34 GB(recrawl DB + curated v1_3으로 재생성 가능), `curated/architizer_curated_v1.db` 0.64 GB, 끝난 vNext 단계의 candidates/review-run 리포트 13개(<0.07 GB). 해당 빌더를 다시 돌리려면 먼저 복원한다.
- 복원 (Git Bash, 한 항목):
  `mv /c/Users/user/reference-crawling_archive_20261016/data_cold/data/<경로> /c/Users/user/reference-crawling/data/<경로>`
  전체 복원은 `MANIFEST.md`의 `data_cold` 섹션 명령을 쓴다.
- `architizer_source_recrawl_v2.db`와 `architizer_html_snapshots_v2/`는 **대체할 수 없는 원본 크롤**이라 `data/`에 남겼다.

### 이동 후 검증 (2026-10-16)

| 검사 | 결과 |
|---|---|
| `pytest tests/ -q` | 1290 passed, 23 skipped (skip 사유는 symlink 권한 1 + 이 머신에 원래 없는 `data/review/*`, `divisare_metadata_v2_2.db`; 옮긴 파일 아님) |
| `validate_vnext_identity_registry.py --stage c` (출력은 scratchpad로 우회) | PASS, 2025 checks |
| `validate_vnext_identity_registry.py --stage b` (출력 우회) | FAIL at check 9 `sha_pin:canonical/vnext_identity_registry.py` — **이동과 무관한 기존 문제.** registry manifest가 48,605 B 버전을 pin했지만 커밋 f99cdcb(15:29 KST)의 현재 파일은 50,640 B다. 기존 stage_b PASS는 15:16 KST에 기록됐다. 옮긴 332개 중 registry 리포트/manifest/검증기가 참조하는 경로는 0개다. |
| `canonical_v2_neon_loader.py --help` | exit 0 |
| `validate_vnext_workspace_freeze.py` live-workspace 검사 | 실행 안 함. 계획 단계에서 이미 실패 상태(오전 653개 이동)로 확인됐고, 이번 이동으로 125개 경로가 더 빠진다. `frozen_inputs` 섹션은 영향 없음. |

## 4. 다른 PC에서 vNext를 이어가려면

| 범위 | 대상 | 크기 |
|---|---|---|
| 최소 (S2 이후 작업) | Frozen 6종 (+E1 `.lock`), registry v1 DB와 reports, candidates v4, legacy snapshot, `vnext_workspace_freeze_v1.json`, v4 manifest/stage_a/gate, gallery draft n10, p4b 캐시 | **~40.5 GB** |
| 체인 재검증 포함 | 위 항목 + candidates v1-v3, n100 DB와 캐시 3종, n100/band/instrument의 locked/manifest JSON, availability DB/cache | ~42.6 GB |
| 코드 | git repo (`main`)와 `.env` (git으로 추적하지 않으므로 별도로 옮긴다) | 작음 |

E3(10.2 GB)는 registry 코드가 읽지 않는다. S2 gallery 빌더만 읽는다. S2를 다른 곳에서 한다면 E3 없이 ~30 GB로 줄일 수 있다.

## 5. 권장 사항 (사용자 결정, 실행하지 않음)

1. **stage b 코드 pin 드리프트**: registry manifest의 `canonical/vnext_identity_registry.py` SHA를 현재 커밋 기준으로 다시 pin할지(감사 종료 뒤) 정한다. 이번 데이터 이동과는 무관하다.
1a. **보류 항목**: curated v1_3(0.69 GB), blind 시트(0.47 GB)는 여전히 `data/`에 있다. 옮길지 정한다.
2. **WP2.9-R 종료 뒤**: pin을 다시 잘라 E3(10.2 GB)와 candidates v1-v3(1.67 GB)를 cold로 보낼지 정한다. v2는 테스트를 고쳐야 옮길 수 있다.
3. **최소 세트 백업**: ~40.5 GB를 외장 디스크, Dropbox, R2 중 어디에 둘지 정한다. R2는 make_db가 쓰지 않는 버킷이다. 쓰려면 별도 버킷과 비용을 따져야 한다.
4. **cold SQLite 압축**: 아카이브 DB에 `zstd -19 --long=31`을 적용하면 흔히 3-5배 줄어든다(추정치이며 측정하지 않았다). 압축 전에 SHA256 목록을 기록하고, 원본을 지우는 것은 별도로 승인받는다.
5. **architizer_host_migrate 백업**: Neon 마이그레이션을 확정한 뒤 cold로 보낸다.
6. **availability n10 (0.5 GB)**: registry에서 `live_then_local` 정책을 빼거나 고정한 뒤 cold로 보낸다. 테스트도 함께 정리한다.
