# Divisare Vision resolution benchmark

## Status

`N10 COMPLETE / N100 COMPLETE - QUALITY GATE FAILED`

This card records the two-phase image-semantics benchmark. Do not mark it
complete until the N10 result is recorded and the frozen-gold N100 decision is
either completed or explicitly deferred.

## Objective

Measure whether a 1024-pixel or 2048-pixel long-edge input is sufficient for
Divisare image semantics without retaining a local image corpus. Use N10 only
to calibrate runtime, schema, latency, and cost; use a separately frozen,
reviewed N100 for quality gating. The completed N100 used blinded Codex-agent
pixel review, not independent human labels, so it is an agent-reviewed audit
and must not be presented as human accuracy.

## Scope

Included:

- immutable v2.4 source verification
- deterministic weak-prior N10 selection
- one max-2048 Divisare fetch per asset
- comparable local JPEG q92 1024/2048 derivatives from one decoded source
- transient batch-local images
- strict Codex Vision structured output and explicit `unknown`
- opaque model-facing IDs and an isolated read-only temporary working root
- token, latency, provenance, agreement, resume, and no-clobber evidence
- design of the frozen balanced N100 quality gate

Excluded:

- full 547,252-asset image run
- production image classifier
- persistent image-byte storage
- pHash computation or cross-site duplicate matching
- material/element accuracy claims without separate multilabel gold
- vector database, Neon, or R2 writes

## Input contract

- Source: `data/curated/divisare_metadata_v2_4.db`
- Expected published source SHA-256:
  `9c523f3393d20ae8732677981c207abd02247ca5ce905dec422a05fa0398f70f`
- Source mode: read-only and byte-immutable
- Benchmark: `divisare-vision-resolution-benchmark-v1.1.0`
- Sidecar schema: `2`
- Selection: `divisare-vision-semantic-strata-v1.0.0`
- Source derivative:
  `divisare-cloudinary-max2048-jpeg-q92-v1.0.0`
- Local derivative:
  `pillow-exif-rgb-lanczos-jpeg-q92-subsampling0-v1.0.0`
- Prompt: `divisare-image-semantics-v1.1.0`
- Runtime: `divisare-codex-vision-runtime-v1.1.0`
- Default model: `gpt-5.6-sol`
- Codex CLI image detail: observed fixed value `high`

The source fetch is `c_limit,f_jpg,h_2048,q_92,w_2048`; PDFs add `pg_1`.
Both local lanes originate from the same decoded response. No upscaling is
allowed.

## Implementation

- `canonical/divisare_vision_benchmark.py`
- `canonical/divisare_vision_runtime.py`
- `tools/run_divisare_vision_benchmark.py`
- `canonical/divisare_vision_gold.py`
- `canonical/divisare_vision_gold_finalize.py`
- `canonical/divisare_vision_n100.py`
- `canonical/divisare_vision_probe.py`
- `tools/combine_divisare_pixel_reviews.py`
- `tools/finalize_divisare_vision_gold.py`
- `tools/run_divisare_vision_n100.py`
- `tests/test_divisare_vision_benchmark.py`
- `tests/test_divisare_vision_runtime.py`
- `docs/DIVISARE_VISION_BENCHMARK.md`

The CLI limitation means this job does not contain a true original-detail
lane. A crop/tile or explicit-original-detail runtime experiment is separate
work and requires its own smoke/cost gate.

## Phase 1: N10 runtime calibration

Purpose:

- validate the end-to-end technical contract
- measure calls, tokens, wall time, and failures
- extrapolate N100 cost
- compare 1024/2048 agreement without calling it accuracy

Command:

```powershell
.venv-images\Scripts\python.exe tools\run_divisare_vision_benchmark.py `
  --source-db data\curated\divisare_metadata_v2_4.db `
  --output-db data\smoke\divisare_vision_resolution_n10_v2.db `
  --report data\reports\smoke\divisare_vision_resolution_n10_v2.md `
  --limit 10 `
  --batch-size 5
```

Resume uses the same command plus `--resume`. It is accepted only when the
partial run's source SHA, manifest SHA, versions, lanes, batch size, model
settings, CLI version, and image detail match. Completed lanes are not rerun,
and refetched response/derivative hashes must reproduce retained evidence.

Planned outputs:

- DB: `data/smoke/divisare_vision_resolution_n10_v2.db`
- Report: `data/reports/smoke/divisare_vision_resolution_n10_v2.md`
- Interrupted state: DB path plus `.partial`

All image bytes are created in `TemporaryDirectory` scope and deleted after
each batch. The sidecar retains hashes and metadata only.

### N10 acceptance

- 10/10 source fetch rows successful, with no post hoc replacement
- 20/20 derivative rows and 20/20 valid Vision rows
- strict asset-ID accounting and controlled vocabulary
- uncertain content remains `unknown`; no fallback to `exterior`
- source SHA before/after equal
- SQLite quick check `ok`; foreign-key violations zero
- tokens, elapsed time, manifest SHA, logical SHA, and lane agreement recorded
- lane-level token/time accounting and distinct-versus-identical input counts
- resume demonstrated without repeating a successful lane

### N10 result

Completed 2026-08-04 with Codex CLI `0.146.0`.

- Source SHA before/after:
  `9c523f3393d20ae8732677981c207abd02247ca5ce905dec422a05fa0398f70f`
- Sample manifest SHA:
  `a0b5aa93dcbb5a6cae442fb8911e3a8be1b1de3052a344b5efae8451b9e7d8eb`
- Logical SHA:
  `e98d325df6a9708e6c35dfb8e7e0654ce924641b28a5146374472bd23292b10f`
- DB SHA / size:
  `7d14567a4d9f8d356c49b20eb0bdaa6b6c2f427d1b5f9d90722ef803cc4b5532`
  / 131,072 bytes
- Report SHA / size:
  `ef484490b3096fd0bf90fe2586b113220514a12287c0e66a4038b1ea02c059be`
  / 2,324 bytes
- Accounting: 10/10 fetch, 20/20 derivatives, 20/20 Vision results
- Calls: 4/4 successful; model wall time 66.469 seconds
- Tokens: input 86,293 / cached 0 / output 2,145 / total 88,438
- 1024: input 39,258 / output 1,014 / 31.844 seconds
- 2048: input 47,035 / output 1,131 / 34.625 seconds
- Medium+view agreement: 10/10 overall, 7/7 distinct-input pairs, 3/3
  identical-input pairs
- Fine set agreement: materials 7/10, elements 6/10, both 4/10
- Visual review: all ten coarse labels plausible; rank 7 is a boundary view
- Temporary images: deleted; remaining benchmark temp directories 0

An initial `v1` partial remains as failure evidence. CLI
`0.138.0-alpha.7` was rejected before token usage because GPT-5.6 Sol requires
a newer Codex version. The runner now requires CLI `>=0.146.0` and stores
stdout/stderr excerpts for future failures.

API-list-price equivalent for the four successful N10 calls is approximately
USD 0.50. Including the successful one-image CLI diagnostic it is approximately
USD 0.56. ChatGPT Codex quota, not API billing, was actually used.

Projected paired N100: input 862,930 / output 21,450 / total 884,380 across
40 batched calls, approximately USD 4.96 at current GPT-5.6 Sol API prices.
Weekly quota percentage is not directly observable; explicit user approval is
required before N100.

## Cost approval gate

Do not start N100 from an assumed token rate. Present the measured N10 token
and time totals, per-asset rate, projected N100 usage, and uncertainty first.
Obtain explicit user approval before N100 if the estimate exceeds about USD 5
or represents a meaningful weekly-quota fraction. A weekly percentage is an
estimate because Codex quota is not directly interchangeable with API token
pricing and exact remaining quota may be unavailable.

## Phase 2: frozen reviewed N100

The dedicated N100 runner consumes only an immutable 100-row reviewed gold
manifest. It does not use the weak-prior N10 selector. The user approved the
measured 40-call run before launch.

Required frozen composition:

| Class | Count |
|---|---:|
| exterior | 20 |
| interior | 20 |
| drawing | 20 |
| aerial | 20 |
| detail | 20 |

Additional controls:

- raster images only
- 80 clear and 20 boundary examples
- within each class, 16 modern and 4 legacy assets
- at most one image per article/building
- exact and likely visual duplicates removed before freeze
- Divisare tags/hints used only for candidate discovery
- human label, review status, adjudication note, and manifest SHA frozen before
  model execution
- no failed sample replacement after results are observed
- counterbalanced lane order and a frozen same-lane repeat subset

N100 reports per-class confusion matrices, precision, recall, and F1, plus
separate clear/boundary results. Initial clear-set gates are macro F1 >= 0.90
and recall >= 0.85 for every class. Select 1024 only when it is within 0.03
macro F1 of 2048 and has no more than two additional errors. Freeze and repeat
a stability subset before accepting a production profile.

The N100 media/view result does not validate visible material or element
labels; those require their own multilabel gold set.

### N100 result (2026-08-05)

Artifacts:

- gold: `data/review/divisare_vision_gold_n100_v1.json`
- DB: `data/smoke/divisare_vision_resolution_n100_v1.db`
- report: `data/reports/smoke/divisare_vision_resolution_n100_v1.md`
- DB SHA: `3dd65bc7286e70e4c5bfc6adfee2ab0d386e987834ccd9c39d353501c23b426b`
- report SHA: `24badf7cc4f01bb4a5fce67803837a3603f19e63a0d3ec60bb2a0df7c5715ea3`
- logical SHA: `5cca9b4f9cab4de281dd4f5035a65d84faae11c000390d579454d0b3e8c7e3cd`

Accounting and integrity:

- 100/100 frozen source fetches and content SHAs matched
- 200/200 derivatives, 40/40 model calls, and 200/200 results completed
- input 883,422 / cached input 309,760 / output 21,447 tokens
- model wall time 660.6 seconds; downloaded bytes 31,199,162
- SQLite quick check `ok`, FK violations zero, all 12 validations passed
- no partial DB/report or transient image directory remained
- API-list-price equivalent was about USD 5.06; ChatGPT Codex quota was used

Quality result:

| Lane | Clear accuracy | Clear macro-F1 | Minimum class recall | Gate |
|---|---:|---:|---:|---:|
| 1024 | 87.50% | 0.8795 | 0.6875 (`detail`) | FAIL |
| 2048 | 82.50% | 0.8264 | 0.5625 (`detail`) | FAIL |

No resolution was selected. The stability N50 model run was not launched.
The pre-result subset was frozen independently at
`data/review/divisare_vision_stability_n50_subset_v1.json`, but it is valid
only for same-prompt repeatability and not as a holdout for a redesigned
prompt.

Post-run error audit found three separate causes:

- `derive_legacy_type()` maps a photograph with `view=elevation` to `drawing`;
  this alone created two 1024 and three 2048 clear-set errors
- the prompt lists enums without defining aerial/exterior or
  detail/interior/exterior boundaries and never asks for the final five-class
  label directly
- some agent-reviewed clear labels were too strong, including a construction
  image marked clear detail and several genuine boundary views

Diagnostic correction of only the elevation mapping raises 1024 clear
macro-F1 to 0.9043, but detail recall remains 0.6875, so the frozen gate still
fails. The current N100 is now a development/audit set. A revised taxonomy and
prompt must be tuned on development data and evaluated once on a fresh,
building/article/pHash-disjoint, independently human-adjudicated holdout.

## Publication and safety

- final DB and report paths are immutable and never overwritten
- final DB/report are hard-linked from partial artifacts as one no-clobber pair
- stale partial report refuses execution
- interrupted DB is resumed explicitly; contract mismatches refuse execution
- fetch/model/validation failures remain evidence in the partial sidecar
- source mutations, incomplete accounting, SQLite errors, or FK errors block
  final publication
- missing N10 gold is a warning because N10 is calibration, not accuracy
- no Neon/R2 write is part of this job

## pHash boundary

This benchmark intentionally does not recompute pHash. The v2.4 identity/hash
stage owns pixel identity and 256-bit pHash candidate filtering. The SHA values
here identify the exact resolution derivatives shown to Vision; they are not a
replacement for cross-site duplicate comparison.

## Verification

Offline targeted tests:

```powershell
.venv-images\Scripts\python.exe -m pytest -q `
  tests\test_divisare_vision_benchmark.py `
  tests\test_divisare_vision_runtime.py
```

Latest combined gold/probe/review/N100/stability targeted regression:
`87 passed` in 46.17 seconds. New and changed Python modules also pass
`py_compile`.

Full repository regression after N10: `307 passed, 2 skipped, 1 deselected,
1266 subtests passed`. The deselected test is the unrelated existing Windows
SQLite-handle teardown case in `test_divisare_curated_v2.py`.

External Vision usage is recorded above. Do not launch full semantics or the
N50 stability model run from this failed gate.
