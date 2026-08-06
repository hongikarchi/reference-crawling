# Divisare image smoke N100

## Superseded finding

The asset-identity blocker recorded by this first v2.3 smoke has been fixed in
the immutable v2.4 overlay. Fresh N10/N100 runs now pass with zero identity
conflicts. The authoritative follow-up is
`.claude/ops/jobs/20260804_divisare_image_identity_v24_n100.md`.

## 범위

- 불변 입력: `data/curated/divisare_metadata_v2_3.db`
- 실행 순서: offline tests -> N10 r1 -> N10 r2 -> N100 -> cache/DB audit
- 모델/API/LLM/Vision 사용: 0
- Neon/R2 쓰기: 없음

## 구현

- `canonical/divisare_image_smoke.py`
- `tools/run_divisare_image_smoke.py`
- `tests/test_divisare_image_smoke.py`
- 실행 환경: repo-local Python 3.12.12, Pillow 12.3.0, ImageHash 4.3.2
- runner: `divisare-image-smoke-v1.2.0`
- sidecar schema: 3
- pHash: `imagehash.phash(hash_size=16)`, 256 bit
- transform: `c_limit,f_jpg,h_512,q_80,w_512`
- PDF transform: `pg_1,c_limit,f_jpg,h_512,q_80,w_512`

v2.3 본체는 UPDATE하지 않았다. 모든 fetch/hash 결과는 `asset_key`와
source `url_id`를 명시적으로 보존하는 별도 sidecar에 기록했다. 기존
positional JSON cache는 사용하지 않았다.

## N10

초기 r1은 정상 PNG 응답을 JPEG가 아니라는 이유로 거부해 raster 성공률
85.71%로 validation fail했다. Cloudinary가 `.png` delivery URL에서
`f_jpg`를 무시할 수 있음을 확인하고, 실제 decoded format을 provenance로
저장하면서 모든 정상 raster를 RGB 정규화하도록 v1.1로 수정했다.

N10 r2 결과:

- 9 success / 1 intentional hard skip / 0 failed
- raster success 100%
- PDF first page 및 AI/vector conversion 성공
- JPEG 41 responses / PNG 1 response
- 42 unique derivative requests, 모두 HTTP 200
- resume 재실행 신규 요청 0
- N10 r1/r2 공통 성공 8개 normalized-pixel SHA와 pHash 전부 동일

## N100 결과

Artifact:

- DB: `data/smoke/divisare_image_smoke_n100_r2.db`
- Report: `data/reports/smoke/divisare_image_smoke_n100_r2.md`
- DB byte SHA: `257e83d36cf981535fac96a83b77d179be95b86e39ea1ce02745ce02acc80f25`
- Report SHA: `2473cebe32cedc8fae21169c49886a050099868b0fd59b4b2e1f0073a03ac34a`
- Logical SHA: `951f8027a7ef386c07345b6a347375cb97f4d1d3ddaf14eaad38d6bbbae61231`
- Sample manifest SHA: `f611ec5375716f3e4ef0dbe6d66e221d194cb0376f0ad0935184e763ed25ea91`
- DB size: 569,344 bytes

Lineage:

- Source SHA before: `7c263f430709dbfe4a2747407d736268331976cd77f66fe25d7037b77fd68038`
- Source SHA after: `7c263f430709dbfe4a2747407d736268331976cd77f66fe25d7037b77fd68038`
- Source byte mutation: 없음

Accounting:

| Cohort | Total | Success | Failed | Skipped |
|---|---:|---:|---:|---:|
| modern raster | 50 | 50 | 0 | 0 |
| legacy raster | 25 | 25 | 0 | 0 |
| convertible PDF/vector | 15 | 15 | 0 | 0 |
| edge | 5 | 5 | 0 | 0 |
| unsupported resource | 5 | 0 | 0 | 5 |
| Total | 100 | 95 | 0 | 5 |

- Source URL rows 134
- Unique derivative requests 129
- HTTP 200: 129 / 129
- Decoded JPEG 128 / PNG 1
- Downloaded response bytes 3,459,869
- Cold N100 wall time 40.1s; schema-v3 warm rerun wall time 21.3s
- Per-request latency p50 922ms, p95 1,656ms, max 2,516ms
- Response size p50 25,056 bytes, p95 48,195 bytes, max 66,283 bytes
- N10 prefix 10/10 동일, 공통 성공 9/9 normalized SHA+pHash 동일
- resume 재실행 신규 요청 0

## 검증

- SQLite integrity check: ok
- foreign key violations: 0
- source asset refs missing: 0
- source URL refs missing: 0
- cache files checked: 125
- cache response SHA mismatch: 0
- cache normalized-pixel SHA/pHash mismatch: 0
- logical SHA recomputation: 동일
- cross-asset exact normalized pixels: 0 groups
- representative asset pHash distance <= 4/8/16: 0/0/0 pairs
- visual check: PNG drawing, PDF first-page drawing, AI/vector drawing 모두 정상 render
- schema-v3 N100 versus 이전 N100 공통 성공 hash: 95/95 동일
- targeted image tests and legacy image tests: 10 passed
- Divisare regression: 145 passed, 1 deselected, 1,263 subtests passed

제외한 테스트는 기존
`test_unapproved_merge_decision_is_rejected`의 Windows SQLite handle teardown이다.
무제외 실행의 유일한 실패는 assertion이 아니라 임시 DB 삭제 시 WinError 32였고,
이번 이미지 코드와 무관하다.

## 발견한 blocker

`divisare|7f2fedf69ca074197bf77b221731ff5cca8a0812`는 THE GYAAN CENTER의
cover/gallery 32 URL을 하나의 asset으로 잘못 묶고 있다.

- 32 fetch success
- 31 distinct normalized-pixel SHA
- cover와 첫 gallery만 같은 이미지
- 나머지 30개는 서로 다른 이미지
- 496 variant pairs의 pHash distance: min 0, median 126, max 158
- distance <= 8은 exact cover/gallery 한 쌍뿐

따라서 현재 v2.3 asset identity로 full hash를 실행하면 실제 이미지 30개를
누락한다. Full run 전에 이 한 건을 delivery-version-aware asset identity로
분리하고, image occurrence/materialized building image surfaces를 재생성해야 한다.

N100 무작위 대표 이미지에는 near-duplicate가 없어 pHash threshold 8의 precision은
깨지지 않았지만 recall calibration은 아직 할 수 없다. Identity 수정 뒤
known transform/crop/compression pair를 포함한 별도 labeled threshold 표본이 필요하다.

코드리뷰 후 smoke runner에는 다음 보강을 적용했다.

- complete resume에서 source SHA, limit, status, algorithm/schema versions,
  error validation, report 존재를 재검증
- failed-validation artifact를 resume 성공으로 오인하지 않음
- source/output/report/partial 경로 충돌 차단
- success hash NULL을 SQLite CHECK와 validation 양쪽에서 차단
- foreign keys 활성화 및 `foreign_key_check` 추가
- logical SHA에 source SHA와 처리 계약 버전 포함

다만 full runner는 별도로 구현해야 한다. 현재 smoke 코드를 전수에 그대로 쓰지 않는다.

- 54.7만 Future를 한꺼번에 만들지 않는 bounded transactional batch 필요
- asset 내 일부 URL 성공/일부 실패를 success로 은폐하지 않는 partial 상태 필요
- cache read/resume 및 host-wide start-rate limiter 필요
- 동시 실행 build lock과 production v2.3 source-contract 검사 필요

## Full extrapolation

N100 실측 평균 response는 약 26.8KB이고 처리량은 약 5.2 requests/s였다.
현재 5-worker 정책을 유지하면 547,222 assets의 clean fetch는 약 29.5시간이다.
retry와 checkpoint를 포함한 계획치는 30~40시간, response cache는 약 15~25GB로
잡는다. 이 추정은 identity blocker 수정 후 다시 계산한다.
