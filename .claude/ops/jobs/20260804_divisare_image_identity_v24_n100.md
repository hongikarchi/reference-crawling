# Divisare image identity v2.4 and N100

## Objective

Correct the Divisare Cloudinary asset-key collision discovered by the first
image N100, publish a new immutable metadata SQLite artifact, and repeat the
image smoke ladder through N100.

Included:

- version-aware Cloudinary asset identity
- immutable v2.3 -> v2.4 SQLite migration
- dependency re-key for URL, occurrence, building-image, hint, and pending-hash
  tables
- N10 and N100 image download, normalized pixel SHA, and pHash verification

Excluded:

- full 547,252-asset image processing
- image semantic classification or Vision
- cross-site image comparison
- Neon, R2, vector, or embedding writes

External API / LLM / Vision / Neon / R2 cost: `$0`.
Model/API tokens consumed by the pipeline: `0`.

## Input

- `data/curated/divisare_metadata_v2_3.db`
- SHA-256:
  `7c263f430709dbfe4a2747407d736268331976cd77f66fe25d7037b77fd68038`
- size: `2,115,321,856 bytes`
- schema: `6`

The parent SHA was unchanged after every migration and smoke run.

## Identity correction

Modern Cloudinary keys changed from:

```text
divisare|{public_id}
```

to:

```text
divisare|{public_id}|{delivery_version}
```

Legacy `project_images` keys are unchanged. Width, quality, crop, and output
format transforms do not participate in identity.

The production URL audit found one multi-version public-ID family: GYAAN
CENTER. It has 32 URLs, 31 delivery versions, and 31 corrected assets. The
cover and first gallery URL share `v1678438203` and one exact pixel hash.

## v2.4 build

- Output: `data/curated/divisare_metadata_v2_4.db`
- Report: `data/reports/divisare_metadata_v2_4.md`
- output SHA-256:
  `9c523f3393d20ae8732677981c207abd02247ca5ce905dec422a05fa0398f70f`
- logical SHA-256:
  `d664374325b3cea5dfe9d6b7f5f39eb65762198e8003df779a05df74745e49b9`
- size: `2,225,299,456 bytes`
- elapsed: `244.078 seconds`
- schema: `7`
- validations: `88 passed / 0 failed`
- SQLite integrity: `ok`
- foreign-key violations: `0`

Population:

- image assets: 547,222 -> 547,252
- modern Cloudinary assets: 429,291 -> 429,321
- legacy assets: 117,931 -> 117,931
- URLs and both occurrence surfaces: 577,112 unchanged
- building image materializations: 547,252
- pending pHash tasks: 547,252

All changed parent tables are explicitly allowlisted. Every other v2.3 user
table passed typed logical SHA equality.

## Smoke result

### N10

- final DB: `data/smoke/divisare_image_smoke_v24_n10_r2.db`
- report: `data/reports/smoke/divisare_image_smoke_v24_n10_r2.md`
- DB SHA-256:
  `8c6c537c2c5b6a0e86ee5b5bbf4580c15be3963634453cece47c651de36a03ef`
- report SHA-256:
  `d427ffaafb1213aae608f7432dd9a42c9f3879bca62ac1ad1cda31ea48ebe2f8`
- logical SHA-256:
  `c12dff1de530af6ec1ad168764cd844f2710eb502395a5c05fb52861f8917bb6`
- 9 success / 1 intentional hard skip / 0 failed
- 12 requests, 12 successful attempts, 436,694 response bytes
- raster success: 100%
- identity conflicts: 0
- resume requests: 0

The GYAAN `v1678438203` asset has two successful variants and one normalized
pixel SHA/pHash.

### N100

- DB: `data/smoke/divisare_image_smoke_v24_n100.db`
- report: `data/reports/smoke/divisare_image_smoke_v24_n100.md`
- DB SHA-256:
  `6f26244cb0be7f4c643a1af3f5d5a1ad3ed8da341c40910d99d730fd2a069f35`
- report SHA-256:
  `9167358ae9ee2c8178996ecf1e2b71b5ddd086408ed09db6af12e703265d5682`
- logical SHA-256:
  `d51e8a5cca8ca1047e930be92acc11d73a661120654c01eef268e040250652cf`
- 95 success / 5 intentional hard skips / 0 failed
- 100 requests, all successful, 2,743,191 response bytes
- decoded formats: 99 JPEG / 1 PNG
- raster success: 100%
- identity conflicts: 0
- wall time: 42.4 seconds
- resume requests: 0

Cross-checks:

- N10 is the exact N100 prefix
- 9/9 common successful asset pixel SHA and pHash values match
- GYAAN v1678438203 vs v1678438207 pixel SHA values differ
- their 256-bit pHash Hamming distance is 132
- no exact normalized-pixel duplicate groups across the 95 assets
- 4,465 pHash pairs: minimum 94, median 128, none <= 16
- cache: 95 content-addressed files, 0 filename/SHA mismatches
- output logical SHA recomputation matched exactly
- SQLite integrity `ok`, foreign-key violations 0

## Diagnostics retained

The first v2.4 build attempt hit the command's 15-minute execution limit
before commit because temporary old/new-key lookups lacked indexes. The staging
copy was verified as unchanged, removed, and the mapping gained indexes on old
key, new key, and source URL. The final build completed in 244 seconds.

`divisare_image_smoke_v24_n10.db` is an immutable diagnostic from a sandboxed
run where all nine networkable assets failed with `connection`. The authorized
fresh-path `n10_r2` run is the final N10 artifact.

## Verification

- identity/migration + smoke targeted tests: 9 passed
- complete repository tests excluding the known Windows SQLite teardown case:
  288 passed, 2 skipped, 1 deselected, 1,266 subtests passed
- `py_compile`: pass
- `git diff --check`: pass

The deselected existing test is
`test_unapproved_merge_decision_is_rejected`; its assertion passes but Windows
cannot remove the still-open temporary SQLite parent handle during teardown.

## Remaining work

The v2.4 identity correction is complete. Before the full image run, promote
the smoke implementation into a bounded production runner with transactional
batches, partial-URL accounting, cache reads, host-wide rate limiting, and a
full-run lock. pHash thresholds still require a labeled resize/crop/compression
benchmark; pHash remains a candidate filter, not sole duplicate evidence.
