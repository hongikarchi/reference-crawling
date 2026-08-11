# Shared E3 image selection policy

## State

- date: `2026-08-11`
- stage: deterministic E3 candidate/shortlist policy comparison
- state: `FULL_COMPLETE_VALIDATED_PENDING_SEMANTIC_COVERAGE_DESIGN`
- smoke/full result: `PASS_WITH_OPEN_QA`
- final representative selection: not performed
- Vision queue or semantic analysis: not performed
- network / Vision / LLM requests: `0 / 0 / 0`
- Neon / R2 / vector DB: not touched

## Scope

Build a separate, evidence-only E3 SQLite sidecar that compares frozen P0, P1,
and P2 top-3 shortlists for each source-qualified building. Preserve every
input fact, rank component, reason, suppression edge, QA fallback, policy hash,
and ordered manifest. Do not mutate E2, choose a final representative, create a
Vision task, infer image meaning, or decide cross-source identity.

## Immutable input

| Item | Value |
|---|---|
| Path | `data/enrichment/divisare_architizer_image_evidence_e2_full_v5.db` |
| Run ID | `e2-e61327cad29ba08b272febe3` |
| Bytes | `10,164,682,752` |
| Byte SHA-256 | `4728f9f015d7fba303923df40f550c59835a7e2a5c1316eef03c499ee3ca7b19` |
| Logical SHA-256 | `795e2fa6239ed88c462a61179c957db705fda9ca65e5adf03953f699af3b86bc` |
| Contract | `archibe-e2-cross-source-image-evidence-v1` |
| Builder | `archibe-e2-cross-source-image-evidence-pipeline-v5` |

The source adapter requires injected expected hashes, opens only with
`mode=ro&immutable=1`, verifies exactly one complete full run and unchanged
input lineage, and rejects WAL/SHM/journal/lock sidecars.

## Frozen population profile

| Population | Architizer | Divisare | Total |
|---|---:|---:|---:|
| Buildings | 61,912 | 29,891 | 91,803 |
| Buildings with a successful image | 61,351 | 29,832 | 91,183 |
| Projects | 61,970 | 29,955 | 91,925 |
| Projects with a successful image | 61,377 | 29,904 | 91,281 |

- successful assets: `1,429,576`;
- exact clusters/members/reduction: `6,420 / 13,488 / 7,068`;
- duplicate identical-pHash nodes/members/reduction:
  `21,306 / 44,142 / 22,836`;
- direct global pHash distance 1–8 edges: `50,580`;
- metadata-blocked distance 9–16 edges: `2,341`;
- cross-source building candidates: `9,026`;
- image-backed / metadata-only candidates: `4,924 / 4,102`;
- image-backed candidates with non-QA / QA-only evidence: `4,606 / 318`;
- cross-source project pairs: `4,932`.

These are evidence and eligibility counts. They do not establish visual quality
or same-building truth.

## Policy freeze

| Policy | Rule | Explicit limit |
|---|---|---|
| P0 | success, cover/gallery role, ordinal, decoded dimensions, stable asset ID | dimensions are tie-break only |
| P1 | P0 plus `low_information` or decoded short edge below 256 hard-risk gate | all-risk case is QA fallback |
| P2 | P1 plus exact, identical-pHash, or stored direct <=8 chosen-star suppression | no graph component or semantic reuse |

All policies produce a top-3 shortlist for comparison. Rank 1 is not an
approved final representative.

Decoded dimensions come from E1's frozen 1024-pixel response, not native source
resolution. They cannot be interpreted as composition, aesthetics, content,
or overall visual quality.

## Implementation inventory

- `canonical/cross_source_image_selection.py`
- `canonical/cross_source_image_selection_sources.py`
- `canonical/cross_source_image_selection_sidecar.py`
- `canonical/cross_source_image_selection_pipeline.py`
- `canonical/cross_source_image_selection_validator.py`
- `canonical/cross_source_image_selection_diagnostic.py`
- `canonical/cross_source_image_selection_diagnostic_validator.py`
- `canonical/cross_source_image_selection_full_pipeline.py`
- `tools/build_cross_source_image_selection_e3.py`
- `tools/build_cross_source_image_selection_e3_full.py`
- `tools/plan_cross_source_image_selection_e3_diagnostic.py`
- `tools/validate_cross_source_image_selection_e3.py`
- `tools/validate_cross_source_image_selection_e3_diagnostic.py`
- `tests/test_cross_source_image_selection_core.py`
- `tests/test_cross_source_image_selection_sources.py`
- `tests/test_cross_source_image_selection_sidecar.py`
- `tests/test_cross_source_image_selection_pipeline.py`
- `tests/test_cross_source_image_selection_validator.py`
- `tests/test_cross_source_image_selection_diagnostic.py`
- `tests/test_cross_source_image_selection_full_pipeline.py`
- `tests/test_validate_cross_source_image_selection_e3_cli.py`
- method document: `docs/CROSS_SOURCE_IMAGE_SELECTION_E3.md`

## CLI

See `docs/CROSS_SOURCE_IMAGE_SELECTION_E3.md` for the accepted representative
N10/N100, P2-diagnostic N10/N100, full preflight, and independent validation
commands. The user-approved double-gated full command completed and the terminal artifact passed the
independent validator. The gate remains in place for any new output path.

All commands must preserve no-clobber behavior and record zero network, Vision,
and LLM requests.

## Offline verification

Implemented coverage includes policy determinism, exact/direct-pHash
non-transitivity, quality fallback, manifests, immutable lineage, bounded full
candidate streaming, sidecar schema, lock/no-clobber, and validation.

```text
targeted E3 pytest: 101 passed
whole repository pytest: 854 passed, 22 skipped, 1453 subtests passed
py_compile: PASS
git diff --check: PASS
```

## Smoke ladder

| Gate | State | Result |
|---|---|---|
| Offline tests | PASS | 101 targeted; full repository PASS |
| N10 offline policy build | PASS | 10 buildings / 136 candidates / 72 shortlist rows |
| N100 offline policy build | PASS with open QA | 100 buildings / 1,421 candidates / 861 shortlist rows |
| P2 diagnostic N10 | PASS | 10 fixed-seed suppressions; independent replay PASS |
| P2 diagnostic N100 | PASS | 100 fixed-seed suppressions; independent replay PASS |
| Full E3 read-only preflight | PASS | Full output was not created |
| Full E3 policy materialization | PASS | 91,803 buildings / 1,429,581 candidates / 810,560 shortlist rows; 43/43 validator checks PASS |
| Vision N10 | Not started | Separate approval and sidecar |

### Immutable smoke artifacts

| Run | Bytes | Byte SHA-256 | Logical SHA-256 | Independent validator |
|---|---:|---|---|---|
| N10 | 1,216,512 | `0d18501f01555dac27056d07d8df7235644aa160dc3f8380afe45794c386f4a9` | `eb60146699920dc5af81bfa7ce5e75c6df14717bcf0a6c010db9abffcd455436` | PASS |
| N100 | 10,457,088 | `15c2aa17ab496c4970d740e7da6f73d9dc637480f2b0195bfbc42dab752ac18f` | `2a730c157d9a5fad75f6fc2d7e3b17838c9cd2d8124fa39bc8c91862099d85a4` | PASS |

Both used E2 byte SHA
`4728f9f015d7fba303923df40f550c59835a7e2a5c1316eef03c499ee3ca7b19`
and logical SHA
`795e2fa6239ed88c462a61179c957db705fda9ca65e5adf03953f699af3b86bc`.
All SQLite quick/integrity/FK checks passed and no terminal sidecars remained.

### P2 diagnostic artifacts

The deterministic suppression population contains `1,409` buildings. Source ×
evidence counts overlap: Architizer exact/identical/direct are `488 / 441 / 409`;
Divisare counts are `114 / 14 / 63`.

| Run | Bytes | Byte SHA-256 | Ordered selection SHA-256 | Independent validator |
|---|---:|---|---|---|
| N10 | 15,705 | `dde3c2b9a3541f62cafc728bbf87b3a7df2db75ca270000baace6f03aa93455b` | `4e8a5eb1d78dc8ed84cc7667d2c6725a7fe7581a985bbc734fc7c4c7aca59fc6` | PASS |
| N100 | 141,512 | `4436a26e679537abedbdd3592d9ecc4b0b3d5b65eafa2544b2f607bc94bb89f3` | `dc75199486ba267bb687a7891ecfc22d5d7ac344f70dc084d796313481db1abd` | PASS |

Both manifests record `network/Vision/LLM = 0/0/0`, are non-authoritative,
and create neither representatives nor Vision tasks.

### Full readiness preflight

The final full CLI performed a read-only real-input preflight in about
`109.9 s`; no output DB was created.

| Check | Result |
|---|---:|
| source buildings / eligible | `91,803 / 91,183` |
| unique successful assets | `1,429,576` |
| building-candidate occurrences | `1,429,581` |
| extra occurrences from multi-building relations | `5` |
| same-building direct-pHash edge expansions | `2,192` |
| estimated final output | `10,522,671,104` bytes (9.8 GiB) |
| available output-volume space | `574,134,558,720` bytes |
| minimum / recommended gate | `15 GiB / 25 GiB`, both PASS |
| output before / after | absent / absent |
| network / Vision / LLM | `0 / 0 / 0` |

Two Architizer assets relate to three and four provisional buildings,
respectively. Their five additional building occurrences explain the exact
`1,429,581 - 1,429,576` difference; it is not an accidental duplicate row.

The full builder uses building-bounded streams, keyset checkpoints, exact
resume lineage, no-clobber output-family checks, final E2 byte rehash, and an
independent full validator.

### Full artifact and independent validation

| Item | Value |
|---|---|
| Path | `data/enrichment/divisare_architizer_image_selection_e3_full_v1.db` |
| Run ID | `e3-full-d7263341bb074292b582ae17` |
| Bytes | `10,236,592,128` |
| Byte SHA-256 | `8512e11f8e1fd581038f790b27a67c0a8b1949067bf53b3ef30c4ea3534141a4` |
| Logical SHA-256 | `6b99e4cda9af7c877213a0708f8ba08b1e3780ba3b75c88b7eb9177fc953d3ce` |
| Builder elapsed | `1,746.8 s` |
| Independent validator elapsed | `1,511.4 s` |
| Validator | `43 / 43 PASS` |
| SQLite | quick/integrity/FK PASS; WAL/SHM/journal/lock absent |
| Network / Vision / LLM | `0 / 0 / 0` |
| Pre-run executable manifest SHA-256 | `3e2d11189694b53988280ad589064a4630b0fc67adfa5225ec2304c1d7c80f1b` |

Core row accounting:

| Row family | Count |
|---|---:|
| buildings / eligible / no-success | `91,803 / 91,183 / 620` |
| image candidates | `1,429,581` |
| policy rankings | `4,288,743` |
| shortlist items | `810,560` |
| same-building direct edge expansions | `2,192` |
| QA fallback buildings/items | `14 / 22` |

Policy accounting:

| Policy | Top-3 items | Top-1 items | Exact-unique top-3 |
|---|---:|---:|---:|
| P0 | 270,220 | 91,183 | 269,518 |
| P1 | 270,181 | 91,183 | 269,479 |
| P2 | 270,159 | 91,183 | 269,567 |

P1 changed P0 rank 1 for 55 buildings and changed the top-3 set for 261.
P2 changed no P1 rank 1, suppressed 1,756 candidates across 1,409 buildings,
and changed the actual top-3 set for 312. Suppression rows are exact pixel 723,
identical pHash 504, and direct pHash distance 1–8 529.

### Policy observations

- N100 contained 98 usable buildings plus two `no_success` controls.
- P1 changed rank 1 for two buildings: `Design Starts Here` (Architizer) and
  `8-9 Long Acre` (Divisare). In both cases P0 selected a cover whose decoded
  short edge was below 256 pixels; P1 selected a non-risk gallery image.
- P2 produced no additional rank-1 or top-3 change in N100. This is retained
  as open QA, not interpreted as proof of no full-population effect. E2 has
  2,411 buildings with repeated identical-pHash nodes; the real N100 simply
  did not place one of those repeats in a suppressible top-3 position.
- Each policy's planning-only queue was 98 top-1 or 287 top-3 items. Exact
  pixel reuse reduced neither count in N100.
- Full completed with 91,183 top-1 items and 270,159 P2 top-3 items. Exact-only
  reuse leaves 90,945 P2 top-1 assets or 269,567 P2 top-3 assets. These are
  planning counts, not an executable Vision queue.

N10/N100 here refer to offline policy materialization and inspection. They are
not Vision calls.

## Vision and cost boundary

E3 performs zero Vision analysis. Any future semantic image analysis must use a
separate immutable sidecar and repeat N10 then N100 before a full proposal.

Historical internal usage observed approximately 3.7k–4.5k tokens per image.
This range is planning-only. Current model price, prompt/output tokens, weekly
quota percentage, rate limits, and failure/retry cost remain unknown until an
actual approved Vision N10 measures them.

## Closeout checklist

- [x] E2 input remains immutable and hash-bound.
- [x] Candidate-only P0/P1/P2 contract is documented.
- [x] Top-3 is not described as a final representative.
- [x] pHash semantic reuse and transitive closure are forbidden.
- [x] Vision/network/LLM requests remain zero during implementation.
- [x] Offline N10 completed and independently validated.
- [x] Offline N100 completed and independently validated.
- [x] CLI/test/artifact metrics recorded.
- [x] P2 diagnostic N10/N100 completed and independently replayed.
- [x] Full builder/validator implemented with checkpoint/resume safety.
- [x] Real-input read-only preflight passed without creating the full output.
- [x] Explicit approval obtained before full E3 materialization.
- [x] Full E3 materialized and independently validated.
- [ ] Separate approval obtained before any actual Vision N10.
