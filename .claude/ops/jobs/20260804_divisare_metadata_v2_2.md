# Divisare metadata SQLite v2.2 reconciliation

## Objective

Produce the final Divisare-only SQLite database for all non-image-semantic
metadata before cross-site comparison. Reconcile the immutable v2.1 metadata
artifact with the completed DOM recrawl without modifying either input.

## Scope

Included:

- Article name, location, year, Built Surface, and description reconciliation
- Raw recrawl/snapshot/parser provenance
- Hash-guarded review of all 21 partial descriptions
- Building-level core recomputation over existing memberships
- Existing taxonomy, D2, source tags, architect links, and image URL retention
- Source-final v2.2 export and review views

Excluded:

- Image download, pHash, image semantics, image duplicate decisions
- Vector/embedding generation
- Cross-site matching
- Neon/R2 writes
- New credit collection

External API/LLM/Vision/Neon/R2 cost: `$0`. The versioned partial-text
decisions were made in the current Codex review session and are identified as
such in the decision file.

## Inputs

- `data/curated/divisare_metadata_v2_1.db`
  - SHA-256:
    `8186f49eac8199e0a5cfbd671c952169646b8829840ba9b8b6f85c2244b9deca`
  - size: `1,621,966,848 bytes`
- `data/enrichment/divisare_metadata_recrawl_v2_4.db`
  - SHA-256:
    `ea7865323fc0c861a7e5cba1a2ef6851683a3da86f57a445dd3b922301bd99b4`
  - size: `153,079,808 bytes`
- `canonical/divisare_partial_text_decisions_v1.json`
  - SHA-256:
    `f31ee5b94afec2c5cde59f2479ea2e06a1e27925b24336f973b571e40838b5df`

Both SQLite inputs were opened read-only. Their SHA-256 values were identical
before and after the build.

## Code

- `canonical/divisare_reconciliation.py`
- `canonical/divisare_partial_text_decisions_v1.json`
- `tools/build_divisare_reconciled.py`
- `tests/test_divisare_reconciliation_policy.py`
- `tests/test_divisare_reconciliation.py`
- `docs/DIVISARE_METADATA_RECONCILIATION.md`
- `docs/DIVISARE_METADATA_RECRAWL.md`

## Smoke ladder

### N=10

- Accepted output:
  `data/curated/smoke/divisare_metadata_n10_v2_2.db`
- SHA-256:
  `dd848182c7ab8173738513276ee6aab2b85227a8b375988e97866575e281ef69`
- logical SHA-256:
  `eca165e29d3e3d7ce6d056e8c09f8f24648f4a75af4cd0c28894bfc860823791`
- 10 direct descriptions, 9 areas, 10 rows reviewed individually
- 43 checks passed / 0 failed

### N=100

- Output: `data/curated/smoke/divisare_metadata_n100_v2_2.db`
- SHA-256:
  `80c596418f5a8afb2eb953bebd09785bc0ccff1c7c53bcd325cb8599ddea28d9`
- logical SHA-256:
  `d77e6b170d55852911d221b3bf15f491eb717ba4b5575ad3e1ea95224d3d5e0c`
- 100 direct descriptions, 61 accepted areas, 2 ambiguous areas quarantined
- 43 checks passed / 0 failed
- repeated build produced the same byte SHA and logical SHA

## Full result

COMPLETE on 2026-08-04.

- Output: `data/curated/divisare_metadata_v2_2.db`
- Report: `data/reports/divisare_metadata_v2_2.md`
- schema: `PRAGMA user_version = 5`
- builder: `divisare-metadata-reconciliation-builder-v1.1`
- policy: `divisare-metadata-reconciliation-v1.1`
- size: `1,807,249,408 bytes`
- elapsed: `88.38 seconds`
- SHA-256:
  `ee7bcd55fedf38fe8cb9a49f51e8f12f69493aef68ff1d201d2fa1e5be8ec95c`
- logical SHA-256:
  `fee79fc8185439a29053f4d94c00673d017c4effcfc52858a2343abe212ba942`

Population and preservation:

- 29,955 articles / 29,891 active buildings
- 93,425 confirmed / 28,285 candidate facets
- D2: 66 confirmed / 220 pending / 0 redirects
- 577,112 image occurrences and URLs preserved
- 547,222 image assets/materialized building images preserved
- image classifications remained `0`
- all 47 copied parent user tables are content-identical by typed logical SHA

Resolved metadata:

- name / country / city / year:
  `29,950 / 29,930 / 29,926 / 29,949`
- Built Surface: `1,419` accepted / `125` review
  - 13 review rows retain a single `area_candidate_sqm`
  - 9 scope/basis candidates use confidence `0.70`
  - one unknown-unit suffix candidate uses confidence `0.55`
  - one hectare and two QA outliers retain review candidates
  - additive expressions with no unique total keep candidate `NULL`
- descriptions: `27,621` publishable
  - 27,602 direct DOM
  - 9 manually accepted fallback
  - 10 404 parent tombstone fallback
  - 10 fallback rejected
  - 2 fallback still review
  - 2,322 source-confirmed no-prose
- article reconciliation review: `154`
- building metadata/reconciliation review: `2,910`

Validation:

- 43 full build checks passed / 0 failed
- SQLite quick/integrity check: `ok`
- foreign-key violations: `0`
- policy tests: `47 passed`
- builder integration tests: `7 passed`
- complete Divisare regression suite: `105 passed`
- N=100 byte and logical determinism: PASS

The repository-wide `unittest discover` was also attempted in the dedicated
Python 3.9 Divisare environment. It ran 156 tests with 2 skips and stopped on
9 unrelated import/setup errors: optional `imagehash`, `pytest`, and
`rapidfuzz` packages are absent, and the current Architizer builder uses
the Python 3.10+ `Path.write_text(newline=...)` argument. Loaded tests had no
assertion failures; the isolated Divisare suite above is complete and green.

## Decisions and limitations

- Normalization-equivalent name/country values are confirmations, not review
  noise. One substantive name conflict remains review.
- Five city-country placeholder names and six invalid one/two-digit years are
  not exposed as canonical values.
- No-content pages do not inherit likely caption/credit residue.
- The 220 pending D2 pairs remain separate. No unapproved merge/reject was
  introduced.
- Candidate taxonomy remains separate from confirmed canonical arrays.
- Scope-qualified area candidates remain separate from canonical `area_sqm`;
  they must not be coalesced automatically by consumers.
- Unknown residual qualifiers, additive/range evidence, or review evidence in
  an automatically adopted area fail the build.
- Image URL identity and order are exact between v2.1 and v2.2; image content
  was not inspected.

## Next stage

Treat `v_divisare_buildings_export_v2_2` as the Divisare source-final export.
Image pHash/semantics and cross-site comparison are separate later jobs.
