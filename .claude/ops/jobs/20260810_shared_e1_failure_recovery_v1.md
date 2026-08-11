# Shared E1 failure recovery and immutable v1.2

## Scope

- state: `COMPLETE`
- date: `2026-08-10`
- sources: Divisare and Architizer
- Vision/LLM: `0`
- semantic image analysis: out of scope
- base/source DB mutation: `0`
- Git commit/push: not performed in this run

## Immutable inputs

| Role | Path | Bytes | SHA-256 |
|---|---|---:|---|
| Divisare source | `data/curated/divisare_metadata_v2_4.db` | 2,225,299,456 | `9c523f3393d20ae8732677981c207abd02247ca5ce905dec422a05fa0398f70f` |
| Architizer source | `data/curated/architizer_curated_v2_0.db` | 8,767,438,848 | `605f4f534fc74267d49ea0b7b3f9b3bed6b55acc3a44a18ae7cafdc53633fbbc` |
| Divisare E1 v1.0 | `data/enrichment/divisare_image_fingerprints_e1_full_v1.db` | 2,785,714,176 | `2a048548afee92d7b222655682a3082ddba535778772b200c674efc6523b1919` |
| Architizer E1 v1.0 | `data/enrichment/architizer_image_fingerprints_e1_full_v1.db` | 4,424,044,544 | `6e9c13c2f2265f56cc6fbbaa55a83b0c275d571fe9e0034d300faf0d36c3889c` |

All four SHA values were unchanged at closeout. No input WAL, SHM, journal, or
writer was present.

## Implementation

- additive `failure_recovery_v1` lineage in the shared pipeline;
- ordinary E1 behavior and schema v3 remain unchanged;
- deterministic failed-only selection with parent/source-record SHA binding;
- no-clobber child manifest and completed resume with zero requests;
- new immutable merge materialization, not in-place parent updates;
- base successes are compared field-for-field before publication;
- successful child rows and attempt offsets are compared field-for-field;
- merge lineage and per-asset decisions are stored inside the new sidecar;
- source/base/recovery/merge-manifest SHAs are bound into the dependency row;
- 5,000-row durable checkpoints support exact-manifest interruption resume;
- merge tables have FK, state-gated insert and terminal immutability triggers;
- legacy children may omit only five additive lineage fields, which are
  derived from independently validated base/source inputs and recorded in the
  final lineage.

Code:

- `canonical/image_fingerprint_recovery.py`
- `canonical/image_fingerprint_merge.py`
- `tools/recover_image_fingerprints.py`
- `tools/merge_image_fingerprint_recovery.py`
- recovery and merge regression tests

## Smoke and recovery

| Run | Selection | Requests | Success | Failed | Result |
|---|---:|---:|---:|---:|---|
| Divisare per-error smoke | 21 | 21 | 0 | 21 | same-url failures reproduced |
| Architizer per-error smoke | 24 | 24 | 1 | 23 | prior HTTP 424 recovered |
| Divisare non-404 + 404 N100 | 153 | 153 | 11 | 142 | 404 sample recovery 11% |
| Architizer all failures | 69 | 69 | 1 | 68 | only HTTP 424 recovered |
| Divisare all failures | 2,314 | 2,314 | 412 | 1,902 | all terminal failures checked |

The final Divisare child transferred 162,449,415 response bytes. The final
Architizer child transferred 10,495,222 response bytes. Responses were hashed
and discarded; downloaded image files were not retained. Both completed child
resume checks returned `already_complete=true` and `network_requests=0`.

## Recovery children

| Source | Path | Bytes | SHA-256 | Selection manifest SHA-256 |
|---|---|---:|---|---|
| Divisare | `data/enrichment/divisare_image_fingerprints_e1_recovery_all_v1.db` | 9,170,944 | `8ffa4545fcd067cc8aae27d2be0accd86d316a9aeae54976c155dd52140d344c` | `032b2812393d1a6f6636c5c3b8f3c120cde7a086e37bfccb79776d49f3739752` |
| Architizer | `data/enrichment/architizer_image_fingerprints_e1_recovery_v1.db` | 385,024 | `f59c48f0f5616cf212963f1e8db2fc050463e380d17a665f7647c06bf86151e1` | `fd018afcbb925787efa0539cf47b45063b70451635bd692535f2f28c176f6029` |

## Immutable v1.2 outputs

| Source | Path | Bytes | SHA-256 | Success | Failed |
|---|---|---:|---|---:|---:|
| Divisare | `data/enrichment/divisare_image_fingerprints_e1_full_v1_2.db` | 2,646,114,304 | `869a79fee9fd65ddeffa299fef4dd9e2ba15a9c7c7170964b03fee1f4c96a819` | 545,327 | 1,902 |
| Architizer | `data/enrichment/architizer_image_fingerprints_e1_full_v1_2.db` | 4,373,962,752 | `58aecdcda936f7327ef7bb4bf3fe21a39ad070e784ab7061e989b62c2dcfe937` | 884,249 | 68 |

The earlier v1.1 materializations have identical success/failure accounting
but predate merge-lineage and terminal-ledger hardening. They are rejected
drafts, remain untouched, and are not release inputs.

Remaining Divisare failures: HTTP 404 `1,849`, decode `52`, original
response-too-large `1` (the isolated 40 MiB retry downloaded 33,078,776 bytes
but the JPEG was truncated). Remaining Architizer failures: empty response
`52`, HTTP 422 `13`, decode `3`.

## Validation

- standard independent validator: pass for both v1.2 outputs after publish;
- merge-specific validator: pass for both v1.2 outputs before atomic publish;
- SQLite quick/integrity/FK: `ok / ok / 0` for both;
- ordered full selection manifests unchanged from v1.0;
- source-record mismatch: `0`;
- successful-attempt linkage mismatch: `0`;
- prior success row change: `0`;
- recovery success copy mismatch: `0`;
- pending rows: `0`;
- input/output WAL, SHM, journal, partial: none at closeout;
- merge decision rows/applied successes: Divisare `2,314 / 412`, Architizer
  `69 / 1`;
- merge progress: `ready_validation`, all base cursors complete, pending `0`;
- recovery/merge offline tests after final ledger hardening: `106 passed`;
- full repository pytest: `674 passed, 22 skipped, 1453 subtests passed`;
- full base-attempt prefix mismatch/extra: `0 / 0` for both outputs;
- unrecovered base fingerprint mismatch: `0` for both outputs;
- merge attempt accounting: Divisare `549,544`, Architizer `884,400`, exact;
- final immutable-input SHA mismatch: `0`.

## Next gate

Do not start semantic Vision analysis yet. The next proposed stage is an
offline exact-pixel and pHash candidate index over the two v1.2 sidecars,
followed by metadata-constrained cross-source project candidate generation.
That design and its N10/N100 acceptance criteria require user approval.
