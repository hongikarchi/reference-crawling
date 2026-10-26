# S6-lite: Neon `building_images` 투영 (금요일 릴리스, 2026-10-23)

상태: **r3 BUILD + VALIDATION PASS (offline, 23/23)** — url policy v3(legacy 문자열 재사용) 후 재빌드, 현행 SHA는 **§7.1**
(§3/§4의 r1, §7의 r2 artifact·코드 SHA는 superseded). Neon/R2 쓰기 0, LLM/Vision 0 ($0), network 0. 커밋 안 함.
Neon 반영은 **사용자 승인 후** dev → prod 순서(§5). 지시: `20261020_vnext_wp4_s2_full_order.md` 최종 결정
(금요일 identity = legacy 행의 현행 `source_refs`, S1 병합 미적용).

## 1. 범위 / 규칙 (`vnext-s6-lite-v1`)

- parent(`canonical_v2_buildings`) 불변. child `building_images`만 생성 — 스냅샷 legacy 39,478행 전부가 대상(FK).
- ref 매핑: `source_refs[divisare|architizer][i]` = source **project** id → E2 `source_project_buildings`
  (S2 run의 `e2_run_id`) → S2 source building. project→building은 1:1(검증). archello/metalocus는 S2 밖 → unmapped.
- ref 순서: `identity_source` 먼저, 나머지 키는 스냅샷 순서, source 내부는 리스트 순서. 같은 S2 건물은 1회.
- child = 매핑된 S2 건물들의 gallery group 합집합. 같은 `representative_pixel_sha256` group은 1행으로 병합
  (대표 = usable_resolution, area, short, long, low_info 순 최선 → tie `(source, asset_id)`).
  `image_asset_id` = 대표 `source_asset_id`, `url` = S2 `serving_url`.
- 순서: (ref 위치, S2 sort_order) 최소값 → legacy `display_cover_url`(Architizer host 이전형으로 변환) 일치 행을
  0번으로. 일치 기준 우선순위: 행 url → 병합 구성 group url → 구성 asset의 occurrence url(S2 `source_occurrences`).
  **(r2, builder v2)** 모든 비교는 양쪽 host 이전 후 `urllib.parse.unquote`(percent-decode)한 형태로 한다 — legacy Neon은
  Architizer 비ASCII 파일명을 raw로, S2 `serving_url`은 %-인코딩으로 저장하기 때문.
- **(r3, url policy v3, builder v3)** 정렬·커버 승격 후 각 행: 같은 legacy 건물 `all_images` url(host 이전형) 중
  `unquote`가 S2 `serving_url`의 `unquote`와 같은 것이 있으면 그 legacy 문자열을 그대로 `url`로(여럿이면 all_images 순서 첫째),
  없으면 S2 `serving_url`. 충돌 방지: 앞 행이 이미 쓴 문자열이거나 다른 행의 S2 url과 같으면 S2 url 유지
  (`s2_serving_url_collision`). provenance `url_basis` / `s2_serving_url`(다를 때만) 기록.
- 분류 컬럼 NULL, `semantic_type`/`cover_slot` `{}`, `is_cover_default` false, `engine*` NULL,
  `projection_version='vnext-s6-lite-v1'`.

## 2. DDL v1.1

`docs/vnext/BUILDING_IMAGES_DDL_v1_1.sql` (v1 파일은 불변). 컬럼 동일, 분류 컬럼·engine nullable,
`semantic_type NOT NULL DEFAULT '{}'`. CHECK = (완전 미분류 행) OR (v1 분류 행 제약 + cardinality≥1 + engine NOT NULL).
PK/UNIQUE(bld, sort_order)/FK/GIN/partial unique cover index 유지, `GRANT SELECT … TO make_web`. 대상 archi_data.public.

## 3. 결과 (r1 — superseded, 현행은 §7.1; 행 집합·분포·매핑·병합 수치는 r3까지 동일)

빌드 206.4 s (입력 해시 전/후 포함), 독립 validator **21/21 PASS** (입력 재해시 + full replay 행 단위 일치).
run `vnx_s6lite_c38d9c362a2e885491413586`. scratch 시험 빌드와 동일 rowset SHA(결정론 확인).

| 항목 | 값 |
|---|---:|
| child 행 (`building_images`) | **699,038** |
| legacy 건물 | 39,478 |
| 이미지 ≥1 건물 | 39,248 (publishable 36,636 / non-publishable 2,612) |
| 이미지 0 건물 | 230 (publishable 37 / non-publishable 193) |
| 건물당 이미지 | mean 17.7, p50 16, p90 31, p99 55, max 182 |
| 분포 | 0:230, 1-4:1,542, 5-9:7,098, 10-19:16,684, 20-49:13,316, 50-99:588, 100+:20 |
| 매핑 ref | divisare 29,955 (S2 건물 29,886), architizer 10,131 (S2 건물 10,081) |
| unmapped ref | archello 9,826, metalocus 263 (S2 범위 밖), architizer project 미존재 4 (bld_029955/029956/029958/029962) |
| S2 건물 없음 legacy 행 | 12 · 복수 S2 건물 행 474 · 여러 legacy 행이 공유하는 S2 건물 3 |
| pixel 병합 | same-source cross-building 304, cross-source 0 |
| display_cover gallery 포함 | **38,000 / 39,275 = 96.75%** (row_url 37,548, occurrence_url 444, constituent_url 8) |
| 미포함 1,275 host | divisare 663, architizer(imgix) 422, archello 175, metalocus 15 |
| legacy all_images 대비 (URL, host 이전형) | same 661,779 / added 37,259 / dropped 187,616 (**r3**: same 670,121 / added 28,917 / dropped 179,274, %-인코딩만 다름 0) |
| dropped 분해 | archello 153,563, metalocus 4,366, divisare 14,703 (+ URL 형식만 다름 5,703), architizer 9,281 |

dropped의 대부분은 S2 범위 밖 source(archello/metalocus)다. Divisare 잔여는 S2 exclusion/unresolved(non-raster 등)와
c_fit 커버 URL 형식 차이로 추정. ~~Architizer 잔여는 S2 exclusion으로 추정~~ → **틀림(r2 리뷰)**: Architizer 9,281 중 8,342는
같은 건물에 percent-encoding만 다른 같은 URL로 존재, 실제 부재는 939(§7).

| 산출물 | SHA-256 |
|---|---|
| `data/canonical/vnext_s6_lite_building_images_v1.jsonl` (688,688,345 B) | `fdc1dd99cde30d9a5fa9f1b2612ac08f3c67e7f79dbed8933dab0659af3ee74c` |
| ordered rowset (DDL 컬럼) | `450777dadf4864108ed22ebbd70a978aac298971eb76affa6ddc07752d483edc` |
| `data/reports/vnext_s6_lite_building_images_v1_manifest.json` | `f4da9b18c7e9c121c1d5041eb6bb3bd4dba86691f633c69b9c6bcac4c5cd7e53` |
| `data/reports/vnext_s6_lite_building_images_v1_report.json` | `3578ebb0abb4d065dac286f6ee584c5335ee854b8c576a88668ffdb1d9ead0c8` |
| `data/reports/vnext_s6_lite_building_images_v1_validation.json` | `7da2ec7a968d1c8b3273fa404daef9a71a5ec15125d1ce06123495a655daabca` |
| apply content md5 (Neon 대조용) | `a9c027d55d7a78e6bcfce030cea21f08` |

입력 pin (전/후 불변): legacy 스냅샷 `17e8da1d…f527f`, S2 DB `7d7c4a23…722d`, S2 manifest `e578fed5…0b29`,
S2 validation `bd6b50b6…1105`, E2 `4728f9f0…b19`, DDL v1.1 `14c29e0d…367d`. (DDL v1 `d924143d…aea` 불변.)

## 4. 코드

| 파일 | SHA-256 |
|---|---|
| `docs/vnext/BUILDING_IMAGES_DDL_v1_1.sql` | `14c29e0d0bb733d37ca3d688032aaf4257b87b6d7ef5e1aa2bfe97a4d291367d` |
| `canonical/vnext_s6_lite_projection.py` (r1, superseded) | `cdd7e4ffc67688a4647c5e4c4f75146ee75412e2784c47259593c451330f0026` |
| `canonical/vnext_s6_lite_projection_validator.py` (r1, superseded; builder import 안 함) | `4d0a289c565218e49acaa5dc13595ae912dfdbdfa411e33ad3307365cd024b8c` |
| `tools/run_vnext_s6_lite_projection.py` | `c5bd9adec39d7e4feab5d9d202519dc02efdeeafc154436d0d83c3b93cadc617` |
| `tools/validate_vnext_s6_lite_projection.py` | `bbf0a5f5b028c4abab4722f9b19e2a728aad73bd66a1caea9ba11f5ef3cae9c4` |
| `tools/vnext_building_images_apply.py` (r1, superseded) | `915871ecce4bea585f5e3b416ecf68aae2b90555f24504a67806d2e4cc69a1eb` |
| `tests/test_vnext_s6_lite_projection.py` (r1, 11 tests, superseded) | `a37494f4acc94bf09316896796c0ca0d14bf34676b1afc08268a83577279a0f0` |

r2 코드 SHA는 §7, r3 코드 SHA는 §7.1.

pytest: `pytest tests/ -q` **1325 passed, 23 skipped** (1456 subtests) — 기존 1314 + 신규 11.

## 5. Neon 반영 방법 (사용자 승인 게이트)

`tools/vnext_building_images_apply.py` — 기본 **dry-run**: 한 transaction에서 DDL v1.1(IF NOT EXISTS) →
COPY stage → parent 누락 시 거부 → diff(unchanged/insert/delete/stale) → 건물 단위 delete-then-insert + stale 삭제
(exact-set) → 검증(행/건물 수, FK orphan 0, sort 연속, 건물 내 url 유일, 미분류 shape, make_web SELECT grant,
정렬 content md5 = 로컬) → **ROLLBACK**. manifest/validation PASS·SHA 불일치면 접속 전 거부. 이 작업에서는 어떤 DB에도
실행하지 않았다(가짜 connection 단위 테스트만).

```
# 1) dev (dev-ksangjo) dry-run → 2) dev apply → 3) prod dry-run → 4) prod apply (각 단계 사용자 승인)
.venv/Scripts/python.exe tools/vnext_building_images_apply.py --dsn-env DEV_DATABASE_URL
.venv/Scripts/python.exe tools/vnext_building_images_apply.py --dsn-env DEV_DATABASE_URL --apply --confirm-db-write
.venv/Scripts/python.exe tools/vnext_building_images_apply.py
.venv/Scripts/python.exe tools/vnext_building_images_apply.py --apply --confirm-db-write
```

운영 주의 (r2 리뷰 반영):
- **dry-run도 lock-free가 아니다.** DDL을 실제 실행(CREATE TABLE + `canonical_v2_buildings` FK → parent에 SHARE ROW
  EXCLUSIVE lock), DELETE·약 70만 행 INSERT 후 ROLLBACK한다. 커밋은 없지만 그동안 parent 쓰기가 막힌다 →
  **prod dry-run은 parent loader가 돌지 않을 때만** 실행. SQL은 가짜 connection으로만 시험했으므로 **dev dry-run이
  실제 Postgres에서의 첫 SQL 시험**이다(실패 시 prod로 진행하지 않음).
- **FK ON DELETE CASCADE × parent loader DELETE-first.** `tools/canonical_v2_neon_loader.py`(424–431행)는 upsert 전에
  `removed_ids`를 DELETE한다. 그 id가 upsert로 복원되면 parent 행은 돌아오지만 `building_images` 행은 cascade로 사라지고
  복원되지 않는다. → **`removed_ids`를 쓰는 parent re-upsert 후에는 반드시 `vnext_building_images_apply.py`를
  다시 실행**(dry-run → 승인 → apply). apply 도구 docstring에도 기록. loader 쪽 경고/거부 추가는 미적용(별도 결정).

주의: 기존 `public.building_images`가 v1 형태(medium NOT NULL)면 거부한다(현재 prod/dev public에는 없음; dev의
mock은 `vnext_preview` schema라 무관). 반영 후 `docs/schema/` 스냅샷·CHANGELOG 갱신 필요(CLAUDE.md 규칙).
롤백 = 직전 artifact로 같은 절차, 또는 `DROP TABLE public.building_images`(신규 테이블).

## 6. 남은 점 / 다음

- display cover 미포함 행 대부분은 archello/metalocus 커버(S2 범위 밖) — §3 host 분해 참조.
- legacy `all_images` 대비 dropped 다수는 archello/metalocus 이미지와 Divisare c_fit 커버 URL 형식 차이.
- S1 병합(P3 re-key), S3 분류, S4 커버 선택은 다음 릴리스.

## 7. r2 (2026-10-23): 리뷰 수정 → 재빌드 PASS

| 리뷰 항목 | 판정 | 조치 |
|---|---|---|
| (major) cover-first가 percent-encoding 차이로 357동 누락 | **확인**(재실행: decode 후 일치·0번 아님 357, 우연히 0번 12) | builder v2 / validator v2: row·constituent·occurrence url 비교를 모두 host 이전 후 `unquote` 형태로. 저장 `url`은 S2 `serving_url`(인코딩형) 유지. validator에 독립 불변식 `cover_row_url_decoded_match_at_sort_order_0` 추가. 테스트 +2(비ASCII Architizer cover, tamper). |
| (minor) Architizer dropped 9,281 과대 | **확인** | report에 `dropped_url_form_only_percent_encoding`(source별) + `dropped_percent_encoding_only`(전체) 추가: Architizer 8,342 / 실제 dropped **939**(ASCII 785 + 비ASCII 154, 리뷰 재계산과 일치). Divisare 512,133 / 5,703 / 14,703 불변. |
| (minor) CASCADE × loader DELETE-first | 확인(loader 424–431행) | §5 운영 주의 + apply docstring에 기록. loader 변경은 미적용. |
| (minor) dry-run lock/부작용 | 확인 | §5 운영 주의: prod dry-run은 loader 미실행 시에만, dev dry-run = 첫 실 SQL 시험. |

~~**URL 형식 결정(제안, 사용자 확인 대상):** `building_images.url`은 S2 serving url(Architizer 비ASCII는 %-인코딩, RFC 3986형,
live GET 200 확인)로 둔다.~~ → **해결(r3, §7.1): legacy 문자열 재사용으로 parent와 문자열 일치.** (아래 원문은 이력) live `canonical_v2_buildings.display_cover_url`/`all_images`는 raw 형태이므로 make_web은
`building_images.url = display_cover_url` 문자열 join을 쓰지 말고 `sort_order = 0`을 커버로 쓰거나 decode 후 비교해야 한다
(HANDOFF 갱신 시 반영). 해당 비ASCII 대표 URL 3,071(S2 집계).

r1 산출물 4개는 커밋·Neon 참조 없음 → 이동 보존:
`C:/Users/user/AppData/Local/Temp/claude/C--Users-user-reference-crawling/c1cc4388-e0ee-442c-8371-05d6e3a8c7fc/scratchpad/superseded/s6_lite_v1_r1_exact_cover_match/`
(jsonl `fdc1dd99…c0ca`, manifest `f4da9b18…7e53`, report `3578ebb0…c8`, validation `7da2ec7a…abca`). 같은 이름으로 재빌드.

**결과** run `vnx_s6lite_c9592890f4892d474c4bed44`, 빌드 205.7 s, validator **22/22 PASS**(입력 재해시 + full replay).
행 699,038 · 건물 39,248(+0행 230) · 분포/publishable/unmapped/pixel 병합은 r1과 동일(행 집합 동일, 순서·provenance만 변경).

| 항목 | r1 | r2 |
|---|---:|---:|
| display_cover gallery 포함 | 38,000 (96.75%) | **38,369 / 39,275 = 97.69%** (row_url 37,917, occurrence_url 444, constituent_url 8) |
| 미포함 | 1,275 | 906 (divisare 663, archello 175, architizer(imgix) 53, metalocus 15) |
| 리뷰 check3 재실행 | decode 일치·0번 아님 357 | **0** (0번 decode 일치 369 = 357 + 12) |
| legacy 대비 Architizer | dropped 9,281 | same 149,646 / %-인코딩만 다름 8,342 / dropped 939 |

| 산출물 | SHA-256 |
|---|---|
| `data/canonical/vnext_s6_lite_building_images_v1.jsonl` (688,690,190 B) | `d3207b0fb5cb8e5e1fd55a9f05231d3f7907c43981f8c8223fe7e8323cb2f423` |
| ordered rowset (DDL 컬럼) | `f31b4ea0ebf78b3439434692813d498073743add2ec4139ec68297dcdf811295` |
| `data/reports/vnext_s6_lite_building_images_v1_manifest.json` | `6cefe0f931827ccf33c609416ae04e121cfc19e5b28cc30f415906452e82874b` |
| `data/reports/vnext_s6_lite_building_images_v1_report.json` | `22c2d9137e73c7c6c1bf2783d37a38e4e8303dbef692b28d67b7ffdc8b6be577` |
| `data/reports/vnext_s6_lite_building_images_v1_validation.json` | `0b5e5ebf457ba5c21b9bbc0b0b73d2bc6bc7d8de6eaa15c857725aa1f7c718a6` |
| apply content md5 (Neon 대조용, offline preflight 699,038행/39,248동) | `d34c6ac5b13a47c98cb7bfe2cf809627` |

입력 pin 전/후 불변(legacy `17e8…`, S2 DB `7d7c…`, S2 manifest `e578…`, S2 validation `bd6b…`, E2 `4728…`, DDL v1.1 `14c29e0d…`).
`.building`/WAL/SHM 잔여 없음.

| 코드 (r2) | SHA-256 |
|---|---|
| `canonical/vnext_s6_lite_projection.py` (builder v2) | `443c3c463d092d0a81004eaaba76271eb26280fc3398f075be13cb77a1b4df86` |
| `canonical/vnext_s6_lite_projection_validator.py` (validator v2, builder import 안 함) | `75e2f73948574ad9e667bdd7154b113fe2b3494045f0448d83cb81c7d5868097` |
| `tools/run_vnext_s6_lite_projection.py` (불변) | `c5bd9adec39d7e4feab5d9d202519dc02efdeeafc154436d0d83c3b93cadc617` |
| `tools/validate_vnext_s6_lite_projection.py` (불변) | `bbf0a5f5b028c4abab4722f9b19e2a728aad73bd66a1caea9ba11f5ef3cae9c4` |
| `tools/vnext_building_images_apply.py` (docstring만) | `076d4e14e31c953d025fa38589bb64abbf14a54f73ccbe0d72da36536b636f58` |
| `tests/test_vnext_s6_lite_projection.py` (13 tests) | `437e297a634303e6866fe449e5d9099f9ce03ee68c4be3a067cfd96aaf3bd31f` |

pytest `tests/ -q`: **1327 passed, 23 skipped** (1456 subtests, 3:39) — 1325 + 신규 2.

## 7.1 r3 (2026-10-23): url policy v3 — parent와 URL 문자열 일치 (§7 URL 형식 문제 **해결**)

문제: r2 `url` = S2 serving url(%-인코딩)이라 live `all_images`/`display_cover_url`(raw 비ASCII)과 문자열 join 불가.
조치: builder v3 / validator v3 (`projection_version`은 `vnext-s6-lite-v1` 유지 — 아직 미반영). 규칙은 §1 (r3) 항목.
validator에 독립 불변식 `url_equals_decoded_equal_legacy_string` 추가(decode 일치 legacy 문자열이 있는데 다른 url이면 FAIL,
단 그 문자열을 같은 건물의 다른 행이 쓰고 있으면 허용). 테스트 +2(충돌 2종 단위, tamper e2e) + 비ASCII e2e 갱신 → 15개.

r2 산출물 4개 이동 보존(커밋·Neon 참조 없음):
`C:/Users/user/AppData/Local/Temp/claude/C--Users-user-reference-crawling/c1cc4388-e0ee-442c-8371-05d6e3a8c7fc/scratchpad/superseded/s6_lite_v1_r2_encoded_urls/`
(jsonl `d3207b0f…f423`, manifest `6cefe0f9…874b`, report `22c2d913…77`, validation `0b5e5ebf…18a6` — §7 기록과 일치 확인). 같은 이름으로 재빌드.

**결과** run `vnx_s6lite_00eb99fe935f13472d9119b8`, 빌드 186.3 s, validator **23/23 PASS**. 행 699,038 · 건물 39,248(+0행 230),
순서·커버 승격은 r2와 동일(display_cover 포함 38,369 / 39,275 = 97.69%). `url`과 provenance만 변경.

| url policy (대표 source별) | legacy 문자열 재사용 | (그중 S2와 다른 문자열) | S2 url 유지 | 충돌로 S2 유지 |
|---|---:|---:|---:|---:|
| architizer | 157,988 | 8,342 | 6,191 | 0 |
| divisare | 512,133 | 0 | 22,726 | 0 |
| 합계 | **670,121** | **8,342** | **28,917** | **0** |

- diff vs legacy: same 670,121 / added 28,917 / dropped 179,274, **`dropped_percent_encoding_only` 0**
  (Architizer bucket: same 157,988 / dropped 939, %-인코딩만 다름 **0** ← r2 8,342). Divisare same 512,133 / 형식차 5,703 / dropped 14,703 불변.
- sort_order 0 url == host 이전형 `display_cover_url` 문자열 일치 **37,915**; decode로만 일치 2
  (bld_037243, bld_038367: 커버 url이 해당 건물 `all_images`에 없어 규칙 범위 밖 → S2 url 유지. 필요 시 `display_cover_url`도 후보에 넣는 규칙 확장 = 별도 결정).
- url 건물 내 유일 확인(validator `url_unique_per_building` + builder 단언), 충돌 0.

| 산출물 (r3) | SHA-256 |
|---|---|
| `data/canonical/vnext_s6_lite_building_images_v1.jsonl` (727,414,739 B) | `4b7c7eee89aecc8a8c723a95ae8bfa803223d3b08e7e2add9323d9dc732803e6` |
| ordered rowset (DDL 컬럼) | `c0fec5ebdb1ecf3785e5718456e6e0b2f3bf19087222f5ac1c7312b4c24ebb53` |
| `data/reports/vnext_s6_lite_building_images_v1_manifest.json` | `49795e769b867d5d09b43079063a531ee989e5ad586b7f491058841f4b126685` |
| `data/reports/vnext_s6_lite_building_images_v1_report.json` | `63ef0ee1239df0dc0d10d3810485908a4814a2a6f305a22820667aec1edc362d` |
| `data/reports/vnext_s6_lite_building_images_v1_validation.json` | `800fe921e655848082fadd7ba9a62c7900e262bcc24ee1d3d905d53535a8e70a` |
| apply content md5 (offline `preflight()`, 699,038행/39,248동, DB 접속 없음) | `7cae88f65de70215ea0a72adadd57451` |

입력 pin 전/후 불변(빌드 내부 재해시 + validator `inputs_unchanged_during_validation`). `.building` 잔여 없음.

| 코드 (r3) | SHA-256 |
|---|---|
| `canonical/vnext_s6_lite_projection.py` (builder v3) | `6d39caf2cc0c1b1141bb39b464f542dc21df051889a7969a1b817e480103af70` |
| `canonical/vnext_s6_lite_projection_validator.py` (validator v3, builder import 안 함) | `88180e1059f85f80e6e7b83b7cf18b3673c87fdd7581cad2a8809e5d6b3376b9` |
| `tests/test_vnext_s6_lite_projection.py` (15 tests) | `7315668ef412fd9a62da767e146fe454c11bbd7a47ff1cbd7b79a6e5627ad0a7` |
| `tools/run_…` / `tools/validate_…` / `tools/vnext_building_images_apply.py` | 불변 (`c5bd9ade…`, `bbf0a5f5…`, `076d4e14…`) |

pytest `tests/ -q`: **1329 passed, 23 skipped** (1456 subtests, 3:19) — 1327 + 신규 2.
make_web 영향: `building_images.url = display_cover_url` / `all_images` url 문자열 join 가능(위 2동 제외). HANDOFF 갱신 시 반영.

## Neon 반영 (2026-10-26, 사용자 실행)

| 대상 | 모드 | 결과 |
|---|---|---|
| dev-ksangjo (`ep-weathered-base-a1xc6frq`) | dry-run | PASS, rollback (table_existed false, inserted 699,038, 9/9 checks) |
| prod (`archi_data`, neondb_owner) | dry-run | PASS, rollback (동일) |
| prod | **apply** | **PASS, committed** — 699,038행 / 39,248동, missing_parents 0, content md5 local == db `7cae88f65de70215ea0a72adadd57451`, make_web SELECT grant 확인 |
| dev-ksangjo | **apply** (2026-10-27) | **PASS, committed** — prod와 동일 수치·md5. dev `--compare` prod 스냅샷: schema identical, data identical |

- run_id `vnx_s6lite_00eb99fe935f13472d9119b8`, manifest `49795e76…`.
- prod 스키마 `--compare`: drift = `building_images` 신규뿐, 기존 5테이블 데이터 identical. 스냅샷 `docs/schema/archi_data.schema.json` 갱신, CHANGELOG 2026-10-26 항목, HANDOFF §5.4 / C11–C13 추가.
- 주의: 건물 로더 재-upsert에서 행이 삭제되면 CASCADE로 이미지 행도 지워진다 → 그 뒤 apply 도구 재실행.
