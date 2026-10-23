# WP4 작업 지시: S2 full gallery v1 (offline) + continuity attestation (2026-10-20, Claude 발행)

사용자 결정 (2026-10-20): 금요일(10/23) 범위 = **축소 릴리스**. S1 병합(감사 확정분) + 실제 gallery
(`building_images`)를 dev → prod 반영. S3 분류/S4 커버/S5 신규 건물 텍스트는 다음 주부터.
유료 LLM/Vision 0, Neon/R2 쓰기 0인 WP다. 근거: `20261019_vnext_asset_refresh_probe.md`.

## 배경 사실 (실측)

- Architizer 이미지 host 이전(imgix 410 → images.architizer.com). E1 pixel SHA 연속성은 원천적으로 불가
  (새 인코더). N172 + N1000 표본: HTTP 200 100%, 크기 ±2px 100%, pHash-256 Hamming 최대 24 (N1000 최대 14).
- Divisare N1000: 998 byte 동일, 1 재인코딩(Hamming 10), 1 404.
- 결론: **per-asset verified-pixel pass 없이** source 단위 표본 attestation으로 S2를 만든다. 전수 픽셀
  pass는 S3(P4B)에서 1회 수행하며 그때 per-asset 연속성도 함께 기록한다(다운로드 1회 원칙).

## 계약 개정 (CONTRACT_GALLERY_SEMANTICS.md §2, v1.2)

1. **population continuity attestation**: source × fetch-profile 단위의 고정 표본(seed 고정, N=1000)에서
   `continuous` 비율 ≥ 99.0% 이고 `content_changed` = 0 이면 그 source의 E1 success 자산은
   `continuity_attested`다. continuous = HTTP 200 ∧ 크기 각 축 ±2px ∧ pHash-256 Hamming ≤ 32.
   pixel SHA 일치는 요구하지 않는다(Architizer는 불가능). 404/410은 `unavailable`로 집계하되
   attestation 실패 사유가 아니다(비율 분모에서 제외하지 않고 보고만).
2. S2 full v1의 대표 자격 = E1 fingerprint success ∧ `continuity_attested` ∧ 기존 exclusion 규칙 통과.
   per-asset `exact_match`(WP1.1 규칙)는 S3 픽셀 pass에서 사후 기록한다.
3. **direct-edge star 부착은 S2 full v1에서 적용하지 않는다.** per-asset 검증이 없으므로 group은 exact
   normalized-pixel SHA 동치류만이다. E2 direct edge(pHash/crop)는 `direct_edge_evidence`로 보존만 하고
   `attachment_status='deferred_no_per_asset_verification'`.
4. Architizer 서빙 URL은 frozen `source_asset_id`를 유지한 채 host mapping으로 만든다:
   `https://images.architizer.com` + `quote(path, safe="/%")` + `?width=1680&quality=60`
   (`canonical/vnext_identity_registry.py` `migrate_image_url` 재사용, 현행 Neon과 동일 형식).
   Divisare 서빙 URL은 현행 Neon `all_images`와 같은 형식을 쓴다(구현자가 WP0b 스냅샷에서 형식 확인).
   key 재계산 금지.

## 산출물

A. `tools/vnext_asset_continuity_sample.py` + 결과
   - scratch 프로브(`probe/cont2000.py`)를 정식 도구로 승격: seed `vnext-asset-continuity-v1`, E2 `assets`
     success 중 source별 1000, profile = Architizer `?width=1024&height=1024&fit=max&quality=85&format=jpg`
     on images.architizer.com, Divisare = frozen E1 `fetch_url`. 4 workers, ≤4 rps.
   - 출력: `data/reports/vnext_asset_continuity_sample_v1.jsonl`(행별: asset id, request URL, status, bytes,
     dims, raw_eq, pix_eq, hamming) + `_summary.json`(source별 판정, 임계값, 입력 SHA). no-clobber.
   - 바이트는 저장하지 않는다.
B. S2 full: `canonical/vnext_gallery_full.py`(+ 독립 `canonical/vnext_gallery_full_validator.py`,
   `tools/run_vnext_gallery_full.py`, `tools/validate_vnext_gallery_full.py`, tests)
   - WP1 Stage A(`canonical/vnext_gallery_draft.py`) 로직 재사용(import 또는 리팩터 없이 호출 가능하면
     호출; WP1 산출물·테스트를 깨면 안 된다). 모집단 = E3 `selected_buildings` building 91,803 전부.
   - 키는 source building `(source, source_building_id)`. vnx re-key(P3)는 S1 감사 확정 후 별도 단계.
   - 입력 pin: E2 full_v5, E3 full_v1(WP0a freeze의 size+SHA), continuity summary SHA, 계약 SHA.
   - 테이블: runs, buildings, gallery_groups(대표, sort_order), group_members, exclusion_ledger,
     unresolved_ledger, source_occurrences, direct_edge_evidence(부착 deferred), validations, metrics.
     각 행 `record_sha256`, run `logical_sha256`, `.building` → `os.replace`, sidecar 없음, no-clobber.
   - **sort_order**: 건물 내 0부터 연속. 우선순위 = 소스 페이지 원래 순서(구현자가 curated DB/E2
     provenance에서 위치 정보 존재 여부를 확인; 있으면 그것) → 없으면 E3 rank → stable key.
     선택한 규칙과 근거를 report에 명시.
   - 각 대표 행에 serving URL(위 4번)과 `url_policy` 기록.
   - 회계: building별 gallery + exclusion + unresolved = 입력 자산 수. 전체 합계도 검사.
   - 성능: 1.43M 자산. 필요한 인덱스는 출력 DB에만 만들고 입력 DB는 immutable read-only.
     N10 → N1000 buildings → full 순서로 실행해 각 단계 elapsed를 기록.
C. 계약 문서 §2 개정 반영 + 새 contract SHA를 job card에 pin.
D. Job card `20261020_vnext_wp4_s2_full_run.md`: 입력/출력 SHA, 건수, validator 결과, elapsed.

## 게이트

- validator PASS(입력 SHA 불변, 회계 일치, 대표 자격, exact-class만 group, URL 정책, sort_order 연속,
  logical SHA 재계산, WP1 산출물 SHA 불변).
- `pytest tests/` 전체 회귀 통과.
- 실패 시 FAIL 리포트 작성 후 정지. 임의 재설계 금지.

## 다음 (이 WP 범위 아님)

- P3: S1 감사 확정 후 vnx re-key(union, cross-source exact-SHA 동치류 병합).
- S6-lite projection: legacy one_to_one 39,142 parent는 값 불변, `building_images` child만 S2에서 생성.
  `BUILDING_IMAGES_DDL.sql`은 S3 이전 상태(분류 미정)를 허용하도록 v1.1 개정 필요 → 사용자 확인.

## 결정 (2026-10-23 09:15 KST, Claude — WP4 r1 FAIL 및 리뷰 D1~D3 처리)

- **D1 = B (사전 선언 후 새 seed로 재표본).** r1 실패 3건은 모두 같은 사진(pHash 0/4/6)이고 크기만 바뀐 경우였다
  (Architizer 파노라마 1024→1021, Divisare tiff/pdf 렌더 크기 변경). ±2px 규칙은 내용 교체 탐지용이었으나
  재렌더 크기 변화를 오탐했다. **v2 규칙(실행 전 고정):** continuous = HTTP 200 ∧ pHash-256 Hamming ≤ 32 ∧
  종횡비 상대차 ≤ 2%. content_changed = HTTP 200 ∧ (Hamming > 32 ∨ 종횡비 상대차 > 2%). 게이트 동일
  (continuous ≥ 99.0% ∧ content_changed = 0). 관측 데이터에 맞춘 사후 조정을 피하려고 **새 seed
  `vnext-asset-continuity-v2`**의 새 표본(source별 1000)으로 판정한다. v1 표본·FAIL 기록은 보존.
- **D2 = 현행 Neon 형식과 일치(경로 quote 안 함).** `canonical_v2_buildings.all_images`와 `building_images.url`의
  문자열이 같아야 조인과 비교가 된다. 브라우저는 비ASCII 경로를 스스로 인코딩한다. 해당 3,135건은 report에 계상.
- **D3 = unresolved 유지(fail-closed).** 브라우저가 렌더할 수 없는 Divisare 원본 형식(pdf/ai/tiff 등) 9,985건은
  `no_browser_renderable_serving_url`. `f_jpg` 재작성은 다음 주 별도 결정.
- **금요일 릴리스의 identity 기준:** S1 100쌍 감사 결과가 아직 없다. 그래서 `building_images`는 **legacy 건물의
  현행 identity(source_refs)** 기준으로 투영한다. S1 병합 반영(P3 re-key)은 감사 확정 후 다음 릴리스에서 한다.
