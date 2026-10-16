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

## Full-run failure recovery and immutable v1.2

The completed v1.0 sidecars are immutable parents. Terminal failures are never
resumed in place. The recovery workflow performs these steps instead:

1. independently validate the parent and immutable source DB;
2. reconcile only parent `fingerprints.status='failed'` identities against the
   current source adapter and exact source-record SHA;
3. write a separate failure-only child sidecar with parent SHA, run ID, fixed
   ordered selection manifest, every HTTP attempt, and a companion manifest;
4. materialize a new full sidecar from the parent plus successful child rows;
5. bind the source, base, recovery and merge-manifest SHAs into the merged
   dependency lineage;
6. reject publication if any prior successful fingerprint changes, any child
   success is not copied exactly, or standard or merge-specific validation
   fails.

An all-failed child is valid only when its dependency manifest contains the
reserved `failure_recovery_v1` lineage. Ordinary E1 runs retain the original
dependency JSON, runner version behavior, schema version, and requirement for
at least one successful fingerprint. Both child and merged outputs use
no-clobber paths and advisory locks. The merge uses 5,000-row durable keyset
checkpoints and resumes only when its manifest and every immutable input SHA
match exactly. A completed child resume performs zero network requests.

The first smoke retried up to ten rows per failure type. Divisare recovered
0/21; Architizer recovered the one transient HTTP 424 row out of 24. A second
fixed Divisare N100 404 sample recovered 11%, which justified retrying all
2,314 Divisare terminal failures once. No successful v1.0 row was downloaded
again.

| Source | Parent success | Parent failed | Recovery selected | Recovered | v1.2 success | v1.2 failed |
|---|---:|---:|---:|---:|---:|---:|
| Divisare | 544,915 | 2,314 | 2,314 | 412 | 545,327 | 1,902 |
| Architizer | 884,248 | 69 | 69 | 1 | 884,249 | 68 |

Divisare's 412 recoveries were all prior HTTP 404 rows. Its remaining failures
are 1,849 HTTP 404, 52 decode failures, and one response-size row whose 33 MB
JPEG was fetched under an isolated 40 MiB cap but proved truncated. Architizer
recovered the HTTP 424 row; 52 empty responses, 13 HTTP 422 rows, and three
decode failures remain.

| Source | Immutable v1.2 output | Bytes | SHA-256 |
|---|---|---:|---|
| Divisare | `data/enrichment/divisare_image_fingerprints_e1_full_v1_2.db` | 2,646,114,304 | `869a79fee9fd65ddeffa299fef4dd9e2ba15a9c7c7170964b03fee1f4c96a819` |
| Architizer | `data/enrichment/architizer_image_fingerprints_e1_full_v1_2.db` | 4,373,962,752 | `58aecdcda936f7327ef7bb4bf3fe21a39ad070e784ab7061e989b62c2dcfe937` |

Both v1.2 outputs passed independent source-inventory, ordered-manifest,
source-record, attempt-linkage, SQLite quick/integrity/foreign-key, and terminal
accounting checks, plus the merge-specific lineage, decision-ledger, trigger,
prior-success no-clobber, recovery-success exact-copy, full base-attempt prefix,
unrecovered-fingerprint, and total-attempt accounting checks. The legacy
recovery children predate five additive lineage fields. Merge v2 permits only
those five missing fields, derives them from the independently validated base
and source, and records the upgrade mode, missing fields, derived values and
recovery SHA in the new immutable dependency lineage. Missing or changed core
lineage still fails closed.

The earlier `*_full_v1_1.db` files were produced before the merge lineage and
terminal-ledger hardening. Their fingerprint counts passed the standard
validator, but they are retained only as rejected drafts and are not release
inputs. Parent/source/recovery SHA values were unchanged after the v1.2 merge,
and no WAL, SHM, journal, retained image, Vision request, or LLM request was
produced. The remaining failures are explicit no-hash rows, not deleted
metadata or inferred placeholders.

Merge v2 deliberately rejects any base or recovery sidecar containing a
`skipped` (or otherwise unsupported) fingerprint status before creating an
output. The current parents contain only `success` and `failed`. A generic
well-shaped recovery lineage is only a syntactic all-failed marker; authority
comes from the recovery-specific validator re-opening the immutable parent and
source, verifying their SHAs, and recomputing the deterministic selection.
