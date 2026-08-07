# Shared image fingerprint method

## Scope

This is the source-neutral E1 method for Divisare and Architizer images. It
downloads one bounded image response, calculates local fingerprints, writes a
SQLite sidecar, and discards the response bytes. It does not run Vision, infer
image meaning, merge buildings, or write to a curated source database.

## Raster contract

1. Request a source-owned derivative with a maximum long edge of 1024 pixels,
   no crop, JPEG output, and quality 85.
2. Decode frame/page zero, apply EXIF orientation, convert a valid ICC profile
   to sRGB, and composite transparency on white.
3. Convert to RGB and resize proportionally to a 512-pixel long edge with
   LANCZOS. Do not crop or add a canvas.
4. Keep only hashes, dimensions, decoder metadata, quality flags, fetch
   provenance, and validation results. Do not retain the image bytes.

The adapter keeps the original URL, normalized asset URL, effective fetch URL,
source asset ID, occurrence count, project count, and cover/gallery roles
separate. A delivery transform is never an asset identity.

## Three fingerprints

| Field | Meaning | Allowed use |
|---|---|---|
| `raw_response_sha256` | Exact bytes returned by the image server | Delivery reproducibility and byte-exact response identity |
| `normalized_pixel_sha256` | Exact RGB pixels after the local 512px contract | Exact normalized-image occurrence dedupe |
| `phash_hex` | 256-bit perceptual hash | Generate similar-looking image candidates |

Neither exact pixel equality nor pHash similarity can merge two building
records by itself. Low-information, animated, or multipage inputs require QA.

## pHash decision rule

- Hamming distance `0-8`: strong similar-image candidate.
- Hamming distance `9-16`: broad review candidate.
- Hamming distance above `16`: no candidate under this method, but not proof
  that the images differ. Even small crops can move the distance far above 16.

The candidate is confirmed with source/project identity, text and location
evidence, exact pixels where available, and later Vision or human review. For
cross-site matching, first narrow the building candidates with metadata, then
compare their image hashes. Do not compare every image with every other image.

## Divisare full-runner v2 smoke results

Input: `data/curated/divisare_metadata_v2_4.db`

- source size: 2,225,299,456 bytes
- source SHA-256:
  `9c523f3393d20ae8732677981c207abd02247ca5ce905dec422a05fa0398f70f`
- inventory accounting: 547,252 total = 547,229 eligible + 23 excluded

| Smoke | Result | Ordered manifest SHA-256 | Response bytes | Sidecar bytes |
|---|---|---|---:|---:|
| N10 | 10/10 success | `c2278c6d124f9447940f07255e93bbd4925ddf86e943d5b1ae4f76d5d5367757` | 842,083 | 196,608 |
| N100 | 99 success, 1 terminal HTTP 404 | `bbb6455532837e9d31a7e566abd9ed924ab25208244307b9171c856949ccbba7` | 8,203,635 | 684,032 |

Both runs passed independent sidecar validation. The source SHA was unchanged,
completed-run resume made zero network requests, and no downloaded image was
retained.

## Architizer full-runner v2 smoke results

Input: `data/curated/architizer_curated_v2_0.db`

- source size: 8,767,438,848 bytes
- source SHA-256:
  `605f4f534fc74267d49ea0b7b3f9b3bed6b55acc3a44a18ae7cafdc53633fbbc`
- metadata corpus: 61,970 projects, 61,912 buildings, and 8,486 firms
- image inventory accounting:
  884,773 total = 884,317 eligible + 456 excluded

The 456 exclusions are rule-based `placeholder_candidate` rows, not a claim
that all 456 are visually confirmed placeholders. They remain in the row-level
exclusion ledger and are open QA for a later source-policy review.

| Smoke | Result | Ordered manifest SHA-256 | Response bytes | Sidecar bytes |
|---|---|---|---:|---:|
| N10 | 10/10 success | `04e74bb53958c84c6007f6e151f94a8d681476ef6d6a71f5ae78ccd3d68ca0da` | 1,227,860 | 1,159,168 |
| N100 | 100/100 success | `fe9fef768dbde90384c45b874c92c7ea2c69ecfe6b59a4e51919d67e07be8aa8` | 14,819,157 | 1,634,304 |

Both runs passed independent sidecar validation, including SQLite quick,
integrity and foreign-key checks, source-record SHA recomputation, ordered
selection manifest recomputation, and complete inventory/exclusion accounting.
The source SHA was unchanged, completed-run resume made zero network requests,
and no downloaded image was retained. N100 used all four workers, made no
retry, and had a 54.175-second request-start span (1.827 requests/second).

## Offline transform calibration

The fixed 100-image benchmark applied 1,200 synthetic transformations.

| Transformation family | Recall at distance <= 8 | Recall at distance <= 16 |
|---|---:|---:|
| Codec and resize | 99.25% | 100% |
| Brightness | 99% | 100% |
| Center crop | 5% | 20.67% |

The 100 deterministic assumed-negative pairs produced no candidates at either
threshold. These were not human-labeled hard negatives, so this is not a
production false-positive guarantee.

## Full-run gate

Full-runner v2 now has bounded worker concurrency, one SQLite writer, global
pacing, durable retry/circuit-breaker state, advisory locking and recovery,
resumable initialization, a row-level exclusion ledger, and independent
manifest validation. N1000 and full processing have not been run and remain
user-gated.

For Divisare's 547,229 eligible assets, the current projection at four workers
and the site-wide two-requests-per-second limit is 547,229 base requests: a
76.0-hour rate-limit floor and about 81-82 hours from the final wave-aware N100
request span. Plan on 81-84 hours, 44.893 GB (41.810 GiB) of responses, and a
2.964 GB (2.760 GiB) sidecar.

For Architizer's 884,317 eligible assets, the same configuration means 884,317
base requests: a 122.8-hour rate-limit floor and a 134.4-hour N100
request-span point estimate. Plan on 135-140 hours (about 5.6-5.8 days),
131.048 GB (122.048 GiB) of responses, and a 4.670 GB (4.349 GiB) sidecar.

The byte, time, and sidecar values are N100 point estimates. Retries,
redirects, response-size tails, CDN variability, SQLite growth, and the
repeated full-source provenance scans can increase them. E1 used $0 of
LLM/Vision; semantic image analysis remains a separate stage.
