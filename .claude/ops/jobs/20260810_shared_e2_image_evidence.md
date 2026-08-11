# Shared E2 cross-source image evidence

## Scope

- state: `COMPLETE`
- date: `2026-08-10`
- sources: Divisare and Architizer
- stage: E2 offline cross-source image evidence
- network/Vision/LLM requests: `0 / 0 / 0`
- representative image selection: not performed
- Vision queue generation: not performed
- project/building merge or final-match decision: not performed
- source/E1 input mutation: `0`
- Neon/R2/vector DB: not touched
- Git commit/push: not performed in this run

## Immutable inputs

| Role | Path | Bytes | SHA-256 |
|---|---|---:|---|
| Divisare metadata | `data/curated/divisare_metadata_v2_4.db` | 2,225,299,456 | `9c523f3393d20ae8732677981c207abd02247ca5ce905dec422a05fa0398f70f` |
| Architizer metadata | `data/curated/architizer_curated_v2_0.db` | 8,767,438,848 | `605f4f534fc74267d49ea0b7b3f9b3bed6b55acc3a44a18ae7cafdc53633fbbc` |
| Divisare E1 v1.2 | `data/enrichment/divisare_image_fingerprints_e1_full_v1_2.db` | 2,646,114,304 | `869a79fee9fd65ddeffa299fef4dd9e2ba15a9c7c7170964b03fee1f4c96a819` |
| Architizer E1 v1.2 | `data/enrichment/architizer_image_fingerprints_e1_full_v1_2.db` | 4,373,962,752 | `58aecdcda936f7327ef7bb4bf3fe21a39ad070e784ab7061e989b62c2dcfe937` |

All four inputs were opened read-only. Builder and independent validator bind
their paths, byte sizes, SHA-256 values, SQLite identity and schema manifests.

## Implementation

- pipeline: `archibe-e2-cross-source-image-evidence-pipeline-v5`;
- exact equivalence is normalized-pixel SHA equality only;
- pHash distance 1–8 uses nine interleaved bands for complete candidate
  discovery, followed by exact 256-bit Hamming recomputation;
- pHash distance 9–16 is considered only inside a conservative exact
  normalized-building-name metadata block;
- pHash edges remain direct evidence; no transitive connected-component
  equivalence is created;
- source project/building memberships and image occurrence role/ordinal are
  preserved;
- low-information evidence is retained as QA-only, not silently discarded;
- bounded streaming and indexed joins avoid whole-corpus Cartesian materialization;
- single-run terminal state, input lineage, logical manifest, advisory lock,
  no-clobber and immutable validation are enforced;
- schema contains no representative, Vision, final-match or merge-decision
  tables.

Code and tests:

- `canonical/cross_source_image_evidence*.py`
- `tools/build_cross_source_image_evidence_e2.py`
- `tools/validate_cross_source_image_evidence_e2.py`
- `tests/test_cross_source_image_evidence*.py`
- `tests/test_validate_cross_source_image_evidence_e2_cli.py`

## Smoke ladder

| Run | Artifact | Elapsed | Logical SHA-256 | Independent validation |
|---|---|---:|---|---:|
| N10 v5 | `data/enrichment/divisare_architizer_image_evidence_e2_smoke_n10_v5.db` | 84.9494 s | `e23fa1957e80ed00d7bf2f309d039c70ededd06d9d03d60a0cb9ebd92de6bdbe` | 31/31 PASS |
| N100 v5 | `data/enrichment/divisare_architizer_image_evidence_e2_smoke_n100_v5.db` | 105.3693 s | `beaa2fdca162883df6c3ef4bc509df0c1bbae491f800571ccbe9d68b5c3e31ba` | 31/31 PASS |

Both runs completed offline. Network, Vision, LLM, representative-selection
and Vision-queue request counters were all zero.

## Rejected drafts

| Draft | Result |
|---|---|
| full v1 | rejected: exact-cluster aggregation query-plan bottleneck |
| full v2 | rejected: quadratic pHash node-pair query plan |
| full v3 | rejected: candidate/edge FK batch-boundary validation failure |
| full v4 | rejected: quadratic metadata-building join plan |

The drafts were not overwritten or deleted. Only v5 is the accepted artifact.

## Immutable full v5 output

- path: `data/enrichment/divisare_architizer_image_evidence_e2_full_v5.db`
- run ID: `e2-e61327cad29ba08b272febe3`
- terminal status: `complete`
- elapsed: `3,466.4432 s` (about 57m 46s)
- bytes: `10,164,682,752`
- byte SHA-256:
  `4728f9f015d7fba303923df40f550c59835a7e2a5c1316eef03c499ee3ca7b19`
- logical SHA-256:
  `795e2fa6239ed88c462a61179c957db705fda9ca65e5adf03953f699af3b86bc`

## Full accounting

| Area | Metric | Count |
|---|---|---:|
| Source | assets | 1,432,025 |
| Source | image occurrences | 1,524,434 |
| Relations | project assets | 1,432,604 |
| Relations | building assets | 1,432,588 |
| pHash | nodes / members | 1,406,740 / 1,429,576 |
| Exact | clusters / members | 6,420 / 13,488 |
| Global pHash | candidates | 89,636 |
| Global pHash | accepted direct edges at distance 1–8 | 50,580 |
| Global pHash | rejected candidates | 39,056 |
| Metadata | building pairs | 6,754 |
| Metadata | distinct node pairs accounted | 2,520,561 |
| Metadata | accepted direct edges at distance 9–16 | 2,341 |
| Evidence | distinct compared node pairs | 2,545,879 |
| Evidence | direct asset evidence rows | 59,187 |
| Evidence | cross-source building candidates | 9,026 |
| Evidence | cross-source project pairs | 4,932 |

Source breakdown:

- Architizer: assets `884,773`, occurrences `947,322`, projects/memberships
  `61,970 / 61,970`, buildings `61,912`;
- Divisare: assets `547,252`, occurrences `577,112`, projects/memberships
  `29,955 / 29,955`, buildings `29,891`.

These are evidence counts, not accepted same-building matches.

## Validation and closeout

- builder terminal validation: complete;
- builder validation ledger: `14` required checks, all pass;
- independent validator: `31/31 PASS`;
- SQLite quick/integrity/FK: `ok / ok / 0`;
- forbidden policy tables: absent;
- request metrics: network `0`, Vision `0`, LLM `0`, representative `0`;
- input SHA mismatch: `0`;
- output WAL/SHM/journal/lock: none;
- E2 tests: `79 passed`;
- full repository pytest: `753 passed, 22 skipped, 1453 subtests passed`;
- E2 module/tool py_compile: pass;
- git diff --check: pass.

## Next policy gate

E2 provides role/ordinal, source membership, E1 quality flags, exact clusters,
direct pHash edges, metadata blocks and project/building evidence. It does not
choose the UI representative image or decide which images receive semantic
Vision analysis.

Before any Vision call, choose and version two separate policies:

1. representative-image ranking and deterministic fallback;
2. Vision queue unit, deduplication, uncertainty targeting and budget cap.

The Vision stage must use a separate immutable sidecar, bind the accepted E2
logical SHA and ordered selection manifest, run N10 then N100, estimate token
cost, and obtain explicit approval before any full run.
