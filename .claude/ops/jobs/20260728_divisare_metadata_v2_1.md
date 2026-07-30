# Divisare metadata SQLite v2.1

## Objective

Reprocess Divisare metadata before any cross-site comparison. Preserve raw
source provenance, apply evidence-aware taxonomy rules, retain multi-value
program and typology, expose article-kind candidates without overclaiming, and
carry the existing D2 state into an immutable SQLite overlay.

## Scope

Included:

- Tag claim provenance and evidence independence
- Building facets and canonical program/typology arrays
- Abstention-first article-kind resolution
- Existing D2 review state and approved-decision redirect machinery
- Redirect-aware memberships, core metadata, facets, and image URL gallery
- Metadata HTML recrawl queue
- Divisare-only export view

Excluded:

- Per-image semantic classification
- Image download, SHA-256 generation, or pHash computation
- Embeddings and vector DB
- Cross-site building matching
- Neon and R2 writes
- Additional credit collection

External API/LLM cost: `$0`.

## Inputs

- Parent: `data/curated/divisare_curated_v1_5.db`
- Parent schema: `PRAGMA user_version = 2`
- Parent SHA-256:
  `0939b15c55e6151e61be022893e1c86e6397455416bc1a113e3d0aa008277737`
- Manual decision file: none

The parent was opened read-only and copied with the SQLite backup API. Its
SHA-256 was checked before and after the build and remained unchanged.

## Code

- `canonical/divisare_curated_v2.py`
- `tools/build_divisare_curated_v2.py`
- `tests/test_divisare_curated_v2_policy.py`
- `tests/test_divisare_curated_v2.py`
- `docs/DIVISARE_METADATA_V2.md`

## Smoke ladder

- N=10: PASS
  - Output SHA-256:
    `9754a31f3a092aa03246196a8129200faf8378d4ff8e07b556df86b829cefb31`
  - 10 articles / 10 active buildings
  - 20 confirmed / 8 candidate facets
  - 4 program / 2 typology compatibility primaries
  - Article kind: 8 unresolved / 2 candidate
  - 35 validation checks passed
- N=100: PASS
  - Output SHA-256:
    `44b14203b262ea94e0f4c78f58f0d5996523becd5a7811fb5a288636df043d3b`
  - 100 articles / 100 active buildings
  - 224 confirmed / 66 candidate facets
  - 4 v1 confirmed facets downgraded
  - 46 program / 35 typology compatibility primaries
  - Article kind: 71 unresolved / 27 candidate / 2 ambiguous
  - 35 validation checks passed

## Full result

COMPLETE on 2026-07-28.

- Builder: `divisare-metadata-v2-builder-v2.1`
- Metadata version: `divisare-metadata-v2.1`
- Schema: `PRAGMA user_version = 4`
- Elapsed: `70.02 seconds`
- Output: `data/curated/divisare_metadata_v2_1.db`
- Output size: `1,621,966,848 bytes`
- Output SHA-256:
  `8186f49eac8199e0a5cfbd671c952169646b8829840ba9b8b6f85c2244b9deca`
- 29,955 articles / 29,891 active buildings
- 93,425 confirmed / 28,285 candidate facets
- 1,309 v1 confirmed facets downgraded for insufficient independent evidence
- 16,599 program / 15,228 typology compatibility primaries
- 842 multi-program / 1,182 multi-typology buildings
- D2: 66 confirmed / 220 pending / 0 redirects
- Metadata recrawl queue: 29,955 articles
- Article-kind status:
  22,450 unresolved / 6,508 candidate / 997 ambiguous
- 35 build validation checks passed / 0 failed
- SQLite integrity and foreign keys: PASS
- Metadata builder/policy unit and regression tests: 35 PASS
- Combined suite after the v2.4 recrawler: 51 PASS
- API/LLM cost: `$0`

## Publication

The output is a separate immutable artifact. Existing output and report paths
cannot be overwritten. A changed decision file or policy requires a new
versioned path. The build report is
`data/reports/divisare_metadata_v2_1.md`.

## Known limitations

- `reject` applies only to existing open D2 pairs. It cannot split a pair
  already auto-clustered in v1.5.
- A false auto cluster requires a v1 rebuild or a future explicit split policy.
- The decision input accepts only the existing 286 D2 candidate pairs. New
  duplicate pairs outside that set require a future input-contract extension.
- No approved manual merge file was supplied, so this artifact has no new
  redirects.
- Historical descriptions and area values still require the separate DOM-aware
  HTML recrawl sidecar.
- Per-image classification, pHash, vectors, cross-site matching, Neon, and R2
  remain outside this job.

## Remaining HTML work

The N=10 and N=100 authenticated recrawl smokes completed with 10/10 and
100/100 fetch success. Their retained snapshots were reparsed with parser v2.3:
all 110 current rows parsed successfully, descriptions used direct DOM
paragraphs, and `Built Surface` was available for 72 rows. HTML snapshots and
parsed metadata remain in a mutable sidecar so later parser changes do not
require rebuilding this immutable DB. The authenticated full crawl now runs
against `data/enrichment/divisare_metadata_recrawl_v2_4.db` with a three-second
request delay, exclusive state lock, authentication-expiry abort, and
resumable per-article commits. Its expected duration is about 25 hours. See
`docs/DIVISARE_METADATA_RECRAWL.md`.
