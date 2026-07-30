# Divisare curated DB v1

## Objective

Build a Divisare-only normalized SQLite before processing or comparing other
architecture sites. Preserve project/article provenance, separate real-building
identity, reinterpret the Divisare taxonomy by axis, and prepare asset-keyed
image work without creating vectors.

## Inputs

- Raw source: external `data/crawl/divisare.db`
- Expected source projects: 29,955
- Expected project tag vocabulary: 668 used tags
- No network, LLM, embedding, Neon, or R2 calls
- Projected API cost: $0

## Code

- `canonical/divisare_curated.py`
- `tools/build_divisare_curated.py`
- `tests/test_divisare_curated.py`
- `docs/DIVISARE_CURATED_DB.md`

## Output

- `data/curated/divisare_curated_v1_5.db`
- `data/reports/divisare_curated_v1_5.md`

The output is gitignored and reproducible. The source DB is opened read-only.
Published DB files are immutable and rebuilds require a new versioned path.

## Smoke ladder

- N=10: PASS
  - integrity/foreign keys/article assignment/image links: PASS
  - 10 articles, 10 buildings, 149 assets
  - Plans tag propagated to image labels: 0
- N=100: PASS
  - integrity/foreign keys/article assignment/image links: PASS
  - 100 articles, 100 buildings, 1,570 assets
  - confirmed tag-only program coverage: 48%
  - confirmed tag-only typology coverage: 37%
  - Plans tag propagated to image labels: 0
- Unit/regression tests: 17 PASS
  - supporting-only scalar abstention
  - direct program conflict abstention
  - missing-year and generic-name non-merge
  - malformed URL preservation
  - source/output path collision rejection
  - immutable-output replacement rejection
  - atomic no-clobber publication
  - downstream build-run/model-text detection
  - pHash enrichment preservation
  - missing architect-index reference preservation
  - location sentinel normalization

## Full-run status

COMPLETE on 2026-07-27.

- Builder: v1.5
- Taxonomy/resolver: v1.2
- Schema user version: 2
- Elapsed: 99.6 seconds
- Output size: 1,259,757,568 bytes (1.17 GiB)
- Output SHA-256:
  `0939B15C55E6151E61BE022893E1C86E6397455416BC1A113E3D0AA008277737`
- 29,955 source articles -> 29,891 provisional buildings
- 62 strict auto-merged building groups / 126 member articles
- 220 open duplicate candidates
  - 181 exact-name/location review pairs
  - 39 fuzzy-name/same-architect-country pairs
- 12,937 architect identities
  - 12,763 from architect index
  - 174 inferred from aligned project references missing from the index
  - 35 unverified display names excluded from identity matching
- 113,326 article tag assignments
- 282,486 evidence claims
- 121,710 building facets
  - 94,728 confirmed
  - 26,982 candidate-only
- 547,222 normalized image assets
- 577,112 raw and normalized image occurrences
- 0 malformed/unpreserved image URLs
- 547,222 pHash work rows pending
- Confirmed building coverage:
  - country: 99.90%
  - city: 99.89%
  - year/description/image: 99.85%
  - program: 57.56%
  - typology: 52.88%
- Validation:
  - SQLite integrity: PASS
  - foreign keys: PASS
  - source/article/building assignment counts: PASS
  - scalar-to-confirmed-facet consistency: PASS
  - confirmed facet evidence/aggregate/link rules: PASS
  - 2,082 scalar conflicts abstained; invalid primary selections: 0
  - strict merge signature and cluster invariants: PASS
  - candidate facet export leakage: 0
  - source tag/image occurrence preservation errors: 0
  - copied raw credit rows: 0
  - dirty location sentinel values remaining: 0
  - non-regenerable/downstream state in published DB: 0
  - independent Critical/High code-review findings after v1.5 fixes: 0

See `data/reports/divisare_curated_v1_5.md` for QA counts, album-level
taxonomy coverage, and high-volume normalized semantic gaps.

## Known limitations

- Historical descriptions lost DOM caption boundaries. Known collection UI is
  removed, but affected text remains quality-flagged.
- Existing positional pHash cache is intentionally not imported.
- pHash and per-image classification remain pending asset work.
- Fuzzy same-building candidates are not automatically merged.
- Building IDs are provisional Divisare-stage IDs. D2 must re-cluster with
  pHash/manual evidence and preserve redirects before cross-site
  canonicalization.
- 29,908 descriptions have known UI removed but remain flagged because the
  historical crawl flattened caption boundaries.
