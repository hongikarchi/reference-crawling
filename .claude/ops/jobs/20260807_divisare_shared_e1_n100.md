# Divisare shared E1 fingerprint N100

## Objective

Define and validate one source-neutral image fingerprint contract that can be
reused for Divisare and Architizer before any Vision classification.

Included:

- 1024px bounded source adapters
- local 512px RGB normalization
- response SHA, normalized pixel SHA, and 256-bit pHash
- immutable provenance SQLite sidecar
- deterministic N10/N100 selection, resume, and validation
- offline codec/resize/brightness/crop calibration

Excluded:

- Vision or semantic labels
- N1000/full image processing
- building merge decisions
- curated DB, Neon, R2, vector DB, or vocabulary writes

External LLM / Vision / Neon / R2 cost: `$0`.
Pipeline model/API tokens: `0`.

## Input

- DB: `data/curated/divisare_metadata_v2_4.db`
- size: `2,225,299,456 bytes`
- SHA-256:
  `9c523f3393d20ae8732677981c207abd02247ca5ce905dec422a05fa0398f70f`
- image assets: 547,252
- eligible image assets: 547,229
- excluded: 23

The source SHA was identical before and after every run.

## Artifacts

Offline benchmark:

- `data/reports/smoke/image_fingerprint_benchmark_n100_v1_2.json`
- 54,231 bytes
- SHA-256:
  `fb8bf15aae481c9a1dde0386fa58425c7acce066f139f41b71155bb8d4cd75d7`

Final N10:

- `data/smoke/divisare_e1_common_n10_v3.db`
- 143,360 bytes
- SHA-256:
  `fe226966d5f6c4c32882c667f36acf5a2201b0417531525fd734dd852c47af3e`
- manifest SHA-256:
  `519ad9ea264eeec8d004bd766aee4e8c1404a7144b36b25555e48427d6f4c550`
- 10 success / 0 failed / 10 requests
- resume: 0 requests

Independent repeat of the same N10:

- `data/smoke/divisare_e1_common_n10_v4_repeat.db`
- 143,360 bytes
- SHA-256:
  `e8b162fe8783b48f6fcbfcb3f55b9cc7cc4ed36faf1cfeffbc23a383ec8b0f80`
- response SHA, pixel SHA, pHash, and dimensions: 10/10 equal

Final N100:

- `data/smoke/divisare_e1_common_n100_v1.db`
- 622,592 bytes
- SHA-256:
  `78d3328e3babe821a4267522c1ad9caca1515559f23bc70b0ba8b9af3a8460f3`
- manifest SHA-256:
  `91afa6a2601080e2b70c45d5b3f13a988f270a994c0a20e43ef44d3e74f26efb`
- 100 success / 0 failed / 0 skipped
- HTTP 200: 100; retry: 0
- response bytes: 9,641,448
- wall time: 216.4 seconds
- decoded format: 100 JPEG
- SQLite quick/integrity: `ok`; foreign keys: 0

## Interpretation

Across the N100 images there were no exact response, normalized-pixel, or
pHash duplicate groups. All 4,950 pairwise comparisons were above the broad
candidate threshold: minimum 94, median 128, and zero at or below 16.

The offline benchmark found that distance 16 retained 100% of codec/resize and
brightness variants but only 20.67% of crop variants. pHash is therefore a
candidate filter, not a complete duplicate detector or building merge rule.

A sandbox-only diagnostic run was published before the network restriction was
recognized; all 10 rows failed with Windows socket access error 10013. It is
not an image-quality result and is not the accepted N10 artifact.

## Implementation verification

- focused tests: 52 passed
- exact host and no-explicit-port enforcement: pass
- response byte cap and source DB read-only SHA checks: pass
- durable retry accounting and cumulative resume budget: pass
- all-failed run rejection: pass
- keyset pending batches: pass
- `git diff --check`: pass

## Full-run gate

N100 validates the raster and hash method. It does not approve a full run.
Before N1000/full, add bounded workers plus a single writer, global pacing and
`Retry-After`, a sustained-error circuit breaker, stale-lock/hot-journal
recovery, resumable initialization, row-level exclusion provenance, and shared
manifest recomputation. The serial N100 rate extrapolates to about 249 hours
and 52.8 GB for 547,229 Divisare assets.
