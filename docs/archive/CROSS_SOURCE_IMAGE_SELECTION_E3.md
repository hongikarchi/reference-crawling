# Cross-source image selection E3

## Status and purpose

E3 is an offline, candidate-only comparison of deterministic image-shortlist
policies over the accepted E2 evidence artifact. It answers which source
images would enter a small review shortlist under P0, P1, and P2. It does not
choose a final representative image, infer visual meaning, create a Vision
queue, merge buildings, or make a cross-source identity decision.

The default output is a top-3 shortlist per source-qualified building for each
policy. “Top 1” remains a policy result to inspect, not an approved UI image.

## Beginner map: where E3 sits

The image pipeline deliberately separates cheap deterministic evidence from
costly semantic review:

1. **E1 fingerprints** download each source image once and record response and
   normalized-pixel SHA-256 plus a 256-bit pHash. No image meaning is inferred.
2. **E2 evidence** joins Divisare and Architizer fingerprints, recording exact
   duplicates and direct pHash-neighbour evidence. It does not decide that two
   buildings or two merely similar images are identical.
3. **E3 selection** ranks each building's available source images under three
   transparent policies and retains up to three review candidates. This is the
   stage documented here.
4. A future, separately approved **Vision stage** may inspect only the chosen
   risk/disagreement subset. Its result will live in another sidecar.
5. Only after that can metadata, deterministic image evidence, and any Vision
   evidence be reconciled into cross-source building decisions.

Thus E3 reduces and explains a future review queue; it is not itself image
recognition and consumes no model tokens.

## Frozen input contract

| Item | Value |
|---|---|
| E2 path | `data/enrichment/divisare_architizer_image_evidence_e2_full_v5.db` |
| E2 run ID | `e2-e61327cad29ba08b272febe3` |
| E2 bytes | `10,164,682,752` |
| E2 byte SHA-256 | `4728f9f015d7fba303923df40f550c59835a7e2a5c1316eef03c499ee3ca7b19` |
| E2 logical SHA-256 | `795e2fa6239ed88c462a61179c957db705fda9ca65e5adf03953f699af3b86bc` |
| E2 contract | `archibe-e2-cross-source-image-evidence-v1` |
| E2 builder | `archibe-e2-cross-source-image-evidence-pipeline-v5` |

E3 opens E2 with `mode=ro&immutable=1`. It verifies the injected expected
byte size and SHA, the stored logical SHA, exactly one `complete` full run,
unchanged E2 input lineage, and absence of WAL/SHM/journal/lock sidecars.
Expected hashes are supplied by the caller; they are not hidden defaults in
the source adapter.

The frozen E2 input lineage is:

| Input | Bytes | SHA-256 |
|---|---:|---|
| Divisare metadata v2.4 | 2,225,299,456 | `9c523f3393d20ae8732677981c207abd02247ca5ce905dec422a05fa0398f70f` |
| Architizer curated v2.0 | 8,767,438,848 | `605f4f534fc74267d49ea0b7b3f9b3bed6b55acc3a44a18ae7cafdc53633fbbc` |
| Divisare E1 v1.2 | 2,646,114,304 | `869a79fee9fd65ddeffa299fef4dd9e2ba15a9c7c7170964b03fee1f4c96a819` |
| Architizer E1 v1.2 | 4,373,962,752 | `58aecdcda936f7327ef7bb4bf3fe21a39ad070e784ab7061e989b62c2dcfe937` |

## Offline population profile

### Buildings and projects

“Usable” means the source-qualified entity has at least one E1-success image.
It does not mean the image has passed visual review.

| Entity | Source | Total | Usable | No successful image |
|---|---|---:|---:|---:|
| Building | Architizer | 61,912 | 61,351 | 561 |
| Building | Divisare | 29,891 | 29,832 | 59 |
| **Building total** | | **91,803** | **91,183** | **620** |
| Project | Architizer | 61,970 | 61,377 | 593 |
| Project | Divisare | 29,955 | 29,904 | 51 |
| **Project total** | | **91,925** | **91,281** | **644** |

Successful images per project have the following distributions:

| Source | p50 | p90 | p95 | p99 | Maximum |
|---|---:|---:|---:|---:|---:|
| Architizer | 12 | 28 | 35 | 54 | 191 |
| Divisare | 16 | 31 | 37 | 50 | 160 |

### Deterministic signals available

- Source occurrence role and ordinal are preserved. The useful role values are
  `cover` and `gallery`; source `image_type` does not provide a populated
  semantic label.
- Project-asset ordinal is present directly. Building-asset ordinal is derived
  once from `source_project_buildings` to `project_assets` and records that it
  is derived.
- All 1,429,576 unique successful assets have decoded E1 dimensions and hashes.
  Building expansion produces 1,429,581 E3 candidate occurrences: two
  Architizer assets legitimately relate to multiple provisional buildings,
  contributing five additional occurrences. E3 ranks occurrences within each
  building and does not collapse those relations.
  Only 158 carry the `low_information` flag (Architizer 127, Divisare 31).
- Canonical and fetch URL references are available. A persisted E1 final URL is
  not available in this E2 artifact.
- Source, occurrence/relation, exact-cluster, and pHash-node lineage is kept so
  every shortlist result can be reproduced and audited.

E1 requested a frozen 1024-pixel source variant. Therefore
`original_width`/`original_height` mean the dimensions of that decoded response,
not the publisher's native file resolution. Dimensions are allowed only as a
short-edge guard and deterministic tie-break. They are not a visual-quality,
composition, or semantic score.

### Exact and pHash evidence

| Evidence | Families/edges | Covered members/nodes | Diagnostic reduction |
|---|---:|---:|---:|
| Exact normalized pixels | 6,420 clusters | 13,488 assets | 7,068 assets (0.494% of successful assets) |
| Identical 256-bit pHash | 21,306 duplicate nodes | 44,142 assets | 22,836 assets (1.597%) |
| Direct global pHash distance 1–8 | 50,580 edges | 99,016 touched nodes | Not an equivalence reduction |
| Metadata-blocked pHash distance 9–16 | 2,341 edges | 4,659 touched nodes | Review evidence only |

The largest exact cluster has 32 assets. The largest identical-pHash node has
69 assets. There is one cross-source exact cluster and 10,456 cross-source
identical-pHash duplicate nodes. Every exact cluster is contained in one
identical-pHash node in this artifact.

pHash distance does not establish semantic identity. E3 never constructs
transitive pHash components. P2 compares a later candidate only with candidates
already chosen for that building's shortlist. A direct pHash edge may suppress
one redundant shortlist slot, but it never authorizes reuse of a future Vision
description or a same-building decision.

### Cross-source candidate context

| Metric | Count |
|---|---:|
| Building candidates | 9,026 |
| Candidates with direct image evidence | 4,924 |
| Metadata-only candidates | 4,102 |
| Image-backed candidates with non-QA evidence | 4,606 |
| Image-backed candidates with QA-only evidence | 318 |
| Cross-source project image pairs | 4,932 |
| Direct candidate image-evidence rows | 59,187 |

Candidate counts by evidence kind overlap: exact pixel 3, identical pHash 3,457,
direct pHash distance 1–8 4,670, and metadata-blocked distance 9–16 1,050.
These counts describe evidence coverage, not accepted cross-source matches.

## Frozen shortlist policies

All policies first require `fingerprint_status=success`, preserve all component
scores and reasons, and use stable source asset identity as the final tie-break.

### P0 — editorial baseline

P0 orders candidates by source editorial role (`cover`, then `gallery`, then
other), source ordinal, decoded-response dimensions, and stable asset ID. It is
the source-intent baseline; dimensions only break otherwise comparable cases.

### P1 — quality-gated editorial

P1 uses P0 order but gates candidates with either of these hard-risk facts:

- `low_information` E1 quality flag;
- decoded 1024-response short edge below 256 pixels.

If at least one non-risk candidate exists, hard-risk candidates are excluded.
If every successful image is risky, P1 emits an explicit QA fallback shortlist
instead of silently producing no result.

### P2 — direct-evidence redundancy suppression

P2 starts from P1 and greedily suppresses a later candidate when it has one of
the following relations to a candidate already selected for the same building:

- same exact-pixel cluster;
- same identical-pHash node;
- one stored direct global pHash edge at distance 1–8.

Suppressed candidates do not become graph bridges. No connected component,
cross-building collapse, semantic-result reuse, or visual inference is allowed.

## Output boundary

E3 writes a separate no-clobber SQLite sidecar. It binds the E2 byte and logical
SHA, policy/config versions, ordered population and sample manifests, source
record hashes, raw component scores, rank/reason codes, QA fallback state, and
direct suppression evidence. E2 and its four frozen inputs remain unchanged.

The sidecar is candidate/shortlist evidence only. It must not contain:

- a final representative-image approval;
- a Vision prompt, response, task, or token ledger;
- semantic image labels;
- building merge or cross-source identity decisions.

Vision, if approved later, uses another immutable sidecar with its own ordered
manifest, request ledger, prompt/model version, response hashes, N10/N100 smoke
results, and cost/quota accounting.

For full materialization, inventory, selection, and candidate phases each have
durable keyset checkpoints. Candidate processing holds one building at a time;
the frozen maximum is 191 candidates. Every checkpoint and its cursor/counts is
replayed independently. The builder rejects an existing output or orphan
SQLite WAL/SHM/journal family, requires adequate disk space before creation,
records the final E2 hash only after a second full byte read, and leaves a
recoverable interruption in `building` state. The validator independently
repeats the terminal E2 byte hash and all shortlist/direct-edge provenance
checks before accepting a full artifact.

## CLI

The implementation CLI exposes no-clobber output, injected E2 byte/logical
expectations, shortlist size, sample size/seed, and independent validation.
The accepted smoke commands are:

```powershell
python tools/build_cross_source_image_selection_e3.py `
  --output data/enrichment/divisare_architizer_image_selection_e3_smoke_n10_v1.db `
  --sample-size 10 --sample-seed archibe-e3-shortlist-smoke-v1 --shortlist-size 3
python tools/validate_cross_source_image_selection_e3.py `
  data/enrichment/divisare_architizer_image_selection_e3_smoke_n10_v1.db `
  --expected-logical-sha256 eb60146699920dc5af81bfa7ce5e75c6df14717bcf0a6c010db9abffcd455436

python tools/build_cross_source_image_selection_e3.py `
  --output data/enrichment/divisare_architizer_image_selection_e3_smoke_n100_v1.db `
  --sample-size 100 --sample-seed archibe-e3-shortlist-smoke-v1 --shortlist-size 3
python tools/validate_cross_source_image_selection_e3.py `
  data/enrichment/divisare_architizer_image_selection_e3_smoke_n100_v1.db `
  --expected-logical-sha256 2a730c157d9a5fad75f6fc2d7e3b17838c9cd2d8124fa39bc8c91862099d85a4
```

These commands are offline and must report network, Vision, and LLM requests as
zero.

P2 suppression has a separate, non-representative diagnostic sampler. It draws
only from buildings where frozen P2 actually suppresses a candidate and
stratifies by source and evidence kind. The N10/N100 manifests are independently
replayed from E2; they do not create an E3 SQLite artifact or a Vision queue.

```powershell
python tools/plan_cross_source_image_selection_e3_diagnostic.py `
  --output data/reports/cross_source_image_selection_e3_diagnostic_n10_v1.json `
  --sample-size 10
python tools/validate_cross_source_image_selection_e3_diagnostic.py `
  --manifest data/reports/cross_source_image_selection_e3_diagnostic_n10_v1.json `
  --expected-sample-size 10

python tools/plan_cross_source_image_selection_e3_diagnostic.py `
  --output data/reports/cross_source_image_selection_e3_diagnostic_n100_v1.json `
  --sample-size 100
python tools/validate_cross_source_image_selection_e3_diagnostic.py `
  --manifest data/reports/cross_source_image_selection_e3_diagnostic_n100_v1.json `
  --expected-sample-size 100
```

The full-population implementation is now bounded and resumable. Its default
command is a read-only preflight and creates no output:

```powershell
python tools/build_cross_source_image_selection_e3_full.py `
  --output data/enrichment/divisare_architizer_image_selection_e3_full_v1.db
```

Actual materialization is intentionally double-gated by `--execute-full` and
the literal confirmation token `RUN_E3_FULL_OFFLINE`. The approved full run
completed on 2026-08-11. An interrupted non-terminal artifact can be resumed
with `--resume` and the same immutable configuration; this terminal artifact
is never overwritten. It passed the independent
`tools/validate_cross_source_image_selection_e3.py` validator after construction.

## Tests

The offline suite covers:

- stable candidate/shortlist IDs and policy-config hashes;
- P0 ordering and P1 hard-risk/all-risk fallback;
- P2 exact, identical-pHash, and direct-edge chosen-star suppression without
  transitive closure;
- deterministic stratified sampling and ordered manifest hashes;
- immutable E2 byte/logical/input lineage and sidecar rejection;
- deterministic, bounded `fetchmany` source streaming, including the full
  source/building/asset iterator and one-time derived ordinal join;
- single-run/no-clobber sidecar schema, lock, terminal state, and independent
  validation;
- full-population keyset checkpoints and exact resume after interruption;
- orphan SQLite-family rejection and disk-space preflight;
- end-of-build and end-of-validation E2 byte rehash;
- independent terminal checkpoint and direct-edge provenance replay.

Current final test results:

```text
Targeted E3: 101 passed
Whole repository: 854 passed, 22 skipped, 1453 subtests passed
```

## Smoke ladder

| Run | State | Selection/network/Vision/LLM result | Artifact and validation |
|---|---|---|---|
| Offline unit/integration | PASS | `0 / 0 / 0` | 101 targeted tests; whole repository passed |
| N10 policy smoke | PASS | 10 buildings / `0 / 0 / 0` | 136 candidates; 72 shortlist rows; independent validation PASS |
| N100 policy smoke | PASS with open QA | 100 buildings / `0 / 0 / 0` | 1,421 candidates; 861 shortlist rows; independent validation PASS |
| P2 diagnostic N10 | PASS | 10 suppression cases / `0 / 0 / 0` | Independent manifest replay PASS |
| P2 diagnostic N100 | PASS | 100 suppression cases / `0 / 0 / 0` | Independent manifest replay PASS |
| Full read-only preflight | PASS | 91,803 buildings / `0 / 0 / 0` | Output file not created; disk and no-clobber gates PASS |
| Full policy materialization | PASS | 91,803 buildings / `0 / 0 / 0` | 1,429,581 candidates; 810,560 shortlist rows; 43/43 independent checks PASS |
| Semantic-coverage N10 plan | PASS | 10 buildings / `0 / 0 / 0` | 57 frozen occurrences; 18/18 independent replay checks PASS |
| Vision N10 | Not started | Separate explicit gate | Separate future sidecar |

The N10 artifact is 1,216,512 bytes, has byte SHA-256
`0d18501f01555dac27056d07d8df7235644aa160dc3f8380afe45794c386f4a9`,
and logical SHA-256
`eb60146699920dc5af81bfa7ce5e75c6df14717bcf0a6c010db9abffcd455436`.
The N100 artifact is 10,457,088 bytes, has byte SHA-256
`15c2aa17ab496c4970d740e7da6f73d9dc637480f2b0195bfbc42dab752ac18f`,
and logical SHA-256
`2a730c157d9a5fad75f6fc2d7e3b17838c9cd2d8124fa39bc8c91862099d85a4`.
Both artifacts are terminal, immutable, and have no WAL, SHM, journal, or lock.

The representative N100 selected 98 buildings with successful images and two explicit
`no_success` controls. P1 changed rank 1 for two buildings (one per source):
each source cover had a decoded short edge below 256 pixels, so P1 selected a
non-risk gallery image. P2 did not change a rank-1 or top-3 set in this sample.
This is not evidence that P2 has no full-population effect: 2,411 buildings in
E2 have at least one repeated identical-pHash node, but none of those repeats
entered a selected N100 top-3 suppression position. The real-artifact P2 branch
therefore remains an explicit coverage warning; synthetic/offline fixtures do
cover exact, identical-pHash, and direct-edge suppression.

The separate P2 diagnostic closes that branch-coverage gap. Its real frozen
population contains 1,409 buildings with at least one suppression. Evidence
labels overlap when one building has more than one kind:

| Source | Exact pixel | Identical pHash, distinct pixel | Direct pHash 1–8 |
|---|---:|---:|---:|
| Architizer | 488 | 441 | 409 |
| Divisare | 114 | 14 | 63 |

Both diagnostic N10 and N100 passed independent replay with zero external
requests. The N100 ordered-selection manifest SHA-256 is
`dc75199486ba267bb687a7891ecfc22d5d7ac344f70dc084d796313481db1abd`;
its canonical JSON byte SHA-256 is
`4436a26e679537abedbdd3592d9ecc4b0b3d5b65eafa2544b2f607bc94bb89f3`.
This diagnostic deliberately over-covers P2 mechanics and is not used to
estimate how often a representative image changes in the general population.

For each policy the N100 planning-only queue was 98 top-1 items or 287 top-3
items. Exact-pixel reuse removed zero items in this sample. No token, call,
price, or quota projection is stored because no Vision model or target policy
has been approved.

The accepted real-input full preflight found 91,803 buildings, 91,183 with at
least one successful image, 1,429,581 building-candidate occurrences, and 2,192
same-building direct-pHash edge expansions. The final preflight took about 110 seconds, created
no output, and reported zero network/Vision/LLM requests. Available output-volume
space was 574,134,558,720 bytes, above both the 15 GiB hard minimum and 25 GiB
recommendation. The completed full sidecar is 10,236,592,128 bytes (about
9.53 GiB), with byte SHA-256
`8512e11f8e1fd581038f790b27a67c0a8b1949067bf53b3ef30c4ea3534141a4`
and logical SHA-256
`6b99e4cda9af7c877213a0708f8ba08b1e3780ba3b75c88b7eb9177fc953d3ce`.
The builder took 1,746.8 seconds and the independent validator took 1,511.4
seconds: about 54 minutes 18 seconds total, substantially faster than the smoke
extrapolation. SQLite quick/integrity/FK and all 43 independent checks passed;
no WAL, SHM, journal, or lock remains.

Full policy accounting is:

| Policy | Shortlist items | Rank-1 buildings | Exact-unique top-3 |
|---|---:|---:|---:|
| P0 | 270,220 | 91,183 | 269,518 |
| P1 | 270,181 | 91,183 | 269,479 |
| P2 | 270,159 | 91,183 | 269,567 |

P1 changed the P0 rank-1 choice for 55 buildings and changed a top-3 set for
261. P2 never changed P1 rank 1, but suppressed 1,756 redundant candidates
across 1,409 buildings; 312 buildings had an actually changed top-3 set. The
suppression rows comprise 723 exact-pixel, 504 identical-pHash, and 529 direct
pHash-distance-1–8 cases. P2 increases exact-unique top-3 coverage relative to
P1 because it replaces duplicates with distinct candidates when available; it
is a diversity policy, not a semantic image-type classifier.

The P2 top-3 is therefore not automatically the future Vision queue. The full
artifact retains all 1,429,581 candidate occurrences, so a separate semantic-
coverage planner can sample cover plus nonduplicate early/middle/late gallery
positions. Vision N10/N100 must compare that coverage probe with the
representative top-3 before deciding how many images per building to analyze.
Exterior, interior, drawing, detail, and other semantic slots are not inferred
by E3.

No offline shortlist metric establishes visual quality. N10/N100 must inspect
source pages or stored image bytes under a separately approved review protocol
before anyone calls a policy result “good-looking” or “representative.”

## Future Vision planning boundary

Historical internal runs observed roughly 3.7k–4.5k tokens per image. This is a
planning-only range, not a current estimate or commitment. Current model cost,
token accounting, rate limits, and weekly quota impact are unknown until an
actual, separately approved Vision N10 measures them. E3 itself makes zero
Vision calls and spends zero Vision tokens.

The offline semantic-coverage preflight is now frozen and documented in
`docs/CROSS_SOURCE_IMAGE_SEMANTIC_COVERAGE.md`. Its fixed source-balanced N10
contains 10 buildings and 57 representative-plus-gallery occurrences. The
canonical manifest is ignored under `data/reports/`; its file SHA-256 is
`81fa13340e584e6d874ab7145a9d003ec57093db5a4dbe41f206c6e7ac85ce1f`.
This closes selection planning only. No image was fetched and no Vision model
was called; the actual N10 remains approval-gated.
