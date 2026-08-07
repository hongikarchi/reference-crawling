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

## Divisare N100 result

Input: `data/curated/divisare_metadata_v2_4.db`

- source SHA-256:
  `9c523f3393d20ae8732677981c207abd02247ca5ce905dec422a05fa0398f70f`
- source assets: 547,252
- E1 eligible: 547,229
- excluded by the adapter: 23 non-image endpoints/files
- N100 manifest SHA-256:
  `91afa6a2601080e2b70c45d5b3f13a988f270a994c0a20e43ef44d3e74f26efb`
- 100 success / 0 failed / 0 skipped / 100 HTTP requests
- 100 JPEG responses, 9,641,448 response bytes
- median response: 90,595.5 bytes and 1,641 ms
- 100 normalized rasters with a 512px long edge
- exact response, pixel, and pHash duplicate groups: 0
- 4,950 cross-image pairs: minimum pHash distance 94, none at or below 16
- SQLite quick/integrity checks: `ok`; foreign-key violations: 0
- source SHA before/after: identical

The same fixed N10 was downloaded twice. All 10 source asset IDs, response
SHA values, pixel SHA values, pHash values, and dimensions matched exactly.
Completed-run resume made zero network requests.

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

Do not start N1000 or full source processing until the production runner adds:

- bounded worker concurrency with one SQLite writer and host-wide pacing;
- `Retry-After`, cooldown, and a sustained-error circuit breaker;
- recoverable process locking and hot-journal recovery;
- resumable initialization and immediate durable fetch-attempt accounting;
- a row-level exclusion ledger with `source total = eligible + excluded`;
- provenance manifest recomputation in independent validation.

At the N100 response rate, a serial Divisare full run is approximately 249
hours and 52.8 GB of downloaded responses. E1 itself uses no LLM or Vision
tokens. Vision-based semantic classification is a separate later stage.
