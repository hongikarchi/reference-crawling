# Cross-source image semantic coverage

## Status and boundary

The fixed cross-source Vision v1 N10 and population-shaped v1 N100 are
complete and independently validated. A subsequent prompt-only v1.1
calibration also completed on a fresh disjoint N10: all 53 inputs and model
results passed technical validation, but the blind semantic quality gate
failed on medium classification and uncertainty sensitivity. Selective full
Vision remains blocked. No v1.1 N100 or full run was started, and no run
changed E1, E2, E3, curated metadata, canonical, Neon, or R2 data.

The completed N100 boundary was:

1. read the immutable E2 and E3 SQLite artifacts;
2. create and independently replay a population-shaped N100 manifest disjoint
   from N10 by building, expected E1 pixel, and fetch URL;
3. fetch only its 562 pinned image URLs and require exact E1 response and
   pixel identity before inference;
4. run the unchanged source-blind semantic contract on the 555 exact inputs;
5. validate the sidecar independently, verify completed resume makes zero
   HTTP and model requests, and freeze a source-blind agent-reference review;
6. stop before selective full Vision.

The seven changed-source inputs need an explicit refresh/reconciliation policy
in a new immutable version. A selective full Vision run remains a later,
independent approval decision based on N100 quality results and exact queue
sizes.

The offline preflight used fixed seed
`archibe-semantic-coverage-n10-v1` selected 10 buildings (five per source) and
57 occurrence memberships. All 57 have distinct E1 normalized-pixel hashes in
this sample. The independent validator replayed the complete population,
guarded selection, candidate decisions, and E2 joins with 18/18 checks passing.
The canonical ignored manifest is
`data/reports/cross_source_semantic_coverage_n10_v1.json`; its file SHA-256 is
`81fa13340e584e6d874ab7145a9d003ec57093db5a4dbe41f206c6e7ac85ce1f` and
its self SHA-256 is
`bf5ac74479ac305e11dc5aa17f17d02102a7eb2499d15680384d21848801ab5b`.
That preflight itself made no network, image, Vision, or LLM request. The later
approved N10 execution is recorded below.

## Actual Vision N10 result

| Measure | Observed result |
|---|---:|
| Buildings / occurrences | 10 / 57 |
| Exact E1 response and pixel matches | 57 / 57 |
| Fetch attempts / retries | 57 / 0 |
| Successful model batches | 12 / 12 |
| Valid schema results | 57 / 57 |
| Downloaded bytes | 6,658,781 |
| Input / cached input / output tokens | 222,983 / 13,056 / 7,050 |
| Vision elapsed, summed | 171.776 seconds |
| Core run wall time | 250.241 seconds |
| SQLite quick / integrity / FK | `ok` / `ok` / 0 |
| Completed-run resume requests | fetch 0 / model 0 |

The immutable result is
`data/enrichment/divisare_architizer_semantic_vision_n10_v1.db`: 835,584
bytes, byte SHA-256
`30cfdce39b8ac0ecc0d1de0b52f05a5f1f5d7bec7390c03439096118e08ee31a`,
logical SHA-256
`10de6fc2a4678c0566beebd93774e97776c2e28239a48f01f4a0ef02001e65dc`.
The run used contract `cross-source-image-semantics-v1.0.0`, prompt
`cross-source-image-semantics-prompt-v1.0.0`, model `gpt-5.6-sol`, high image
detail, batch size five, and stored raw model JSONL separately from normalized
results and deterministic derivatives.

An independent validator recomputed the frozen manifest, E2/E3 lineage,
response and pixel identity, gzip payloads, token totals, every normalized
result, every occurrence link, hero tier, coverage slot, trigger set, and
logical SHA. All error-severity checks passed. E2 and E3 retained their exact
start SHA values and had no WAL, SHM, or journal sidecars.

The completed artifact records runner v1.0.0. A post-run static review found
no defect in that uninterrupted success path, but exposed crash/resume and
partial-publish risks. The committed runner/retry implementation is therefore
v1.1 and remains able to validate the immutable v1.0.0 result. It adds a
SHA-verified durable input spool so a committed `ready` row resumes without a
second request, process-wide 2 requests/second pacing, atomic cache writes,
persistent advisory locking, DB/report publish rollback, report content
binding for new artifacts, bounded credential-redacted subprocess payloads,
literal frozen-manifest identity pins, failure-aware inspection, and stricter
attempt-ledger replay. The complete semantic test group passed 112 tests and
the full repository suite passed 966 tests with 22 skips and 1,453 subtests.

### What the semantic pass added

The pixel-derived hero ledger contains 37 `preferred`, 8 `eligible`, 9
`fallback`, 2 `qa_only`, and 1 `rejected` candidates. These are evidence
tiers, not a final product hero choice.

The expanded early/middle/late gallery probes produced at least one semantic
slot absent from P2 top three for 7 of 10 buildings. They added 15 distinct
building-slot pairs: interior 5, detail 3, plan 2, exterior-overall 2, other
drawing 1, section 1, and model/render 1. This demonstrates measurable
coverage gain without claiming that an unobserved slot is absent from the
full gallery.

Among nine non-QA P1 representative anchors, eight contained an in-scope
architectural subject. `semv_000013` was correctly rejected because the frame
was dominated by landscape with only tiny distant buildings. Another anchor,
`semv_000046`, was retained only as `qa_only` because it was a low-legibility
brick material detail. Downstream hero selection must therefore skip rejected
anchors and prefer a better candidate from the same building when available.

### Blind semantic review

All 57 inputs were rendered to source-blind contact sheets showing only an
opaque inference ID. No unambiguous scope or medium error was found in this
agent review; the rendering/photo boundary on `semv_000019` was considered
defensible. This is not an independent human gold set and must not be reported
as production accuracy.

The main open QA is uncertainty calibration. The model returned no
`uncertain_axes` and no `resolution_insufficient` flag for any input. Review
identified six defensible primary labels whose boundary should nevertheless
have been surfaced as uncertain:

| Inference ID | Boundary omitted from uncertainty |
|---|---|
| `semv_000013` | no-project-visible vs very-low-legibility site context |
| `semv_000019` | rendering vs highly polished photograph |
| `semv_000035` | coherent project vs low-legibility multi-building context |
| `semv_000036` | threshold vs exterior rooftop terrace |
| `semv_000054` | threshold vs exterior element detail |
| `semv_000055` | threshold vs exterior element detail |

These are uncertainty false negatives, not confirmed primary-label errors.
The semantic N10 therefore passes its technical gate and its zero-clear-
scope/medium-error gate, with uncertainty sensitivity carried as open QA into
N100.

### Actual Vision N100 result

The approved N100 used seed `archibe-semantic-coverage-n100-v1`. Its sample
frame contained 91,173 eligible buildings after excluding the ten N10
buildings. Hamilton allocation over source and population stratum selected 67
Architizer and 33 Divisare buildings without deliberately over-sampling known
failures. The frozen manifest contains 100 buildings, 562 occurrences, 562
unique expected E1 pixels, and zero N10 building, E1-pixel, or fetch-URL
overlap. Its byte SHA-256 is
`37d917dc2c5b7c89308641b97035255e1fab5e469a0fc86935bf716559ead540`.

| Measure | Observed result |
|---|---:|
| Buildings / fixed occurrences | 100 / 562 |
| Exact E1 response and pixel matches | 555 / 562 |
| Changed-source rows retained as failures | 7 |
| Fetch attempts / retries | 562 / 0 |
| Successful model batches / attempts | 113 / 116 |
| Recovered model retries | 3 |
| Valid schema results | 555 / 555 exact inputs |
| Downloaded bytes | 83,719,902 |
| Input / cached input / output tokens | 2,216,339 / 1,303,552 / 71,935 |
| Non-double-counted input + output | 2,288,274 tokens |
| Core run wall time | 2,168 seconds |
| SQLite quick / integrity / FK | `ok` / `ok` / 0 |
| Completed-run resume requests | fetch 0 / model 0 |

The seven failures were all Architizer rows whose newly fetched response and
normalized-pixel identities no longer matched frozen E1. Architizer produced
361 exact inputs from 368 selected occurrences; Divisare produced 194/194.
The immutable result is
`data/enrichment/divisare_architizer_semantic_vision_n100_v1.db`: 7,106,560
bytes, byte SHA-256
`e2a03e004a27ce662a93f60ad16190014789abf60f9433926ff9e38e747c8a8e`,
logical SHA-256
`717abc3df21c9fa5e182a52fe099d8c52af71ba8c6ef5256673c41ac1c75205d`.
Status is `complete_with_failures`: this is a valid, immutable calibration
artifact, but its seven fixed-input failures block automatic Full promotion.

The independent validator replayed the entire manifest and E2/E3/N10
lineage, schema and trigger SQL, attempt and raw-payload ledgers, normalized
results, hero and coverage derivations, the 125-call cap and observed
client-side token stop thresholds, report binding,
logical SHA, and temporary-file accounting. Every error-severity validation
passed. Source-blind review used 93 opaque-ID sheets for the 555 exact inputs;
the resulting labels are an `agent_reference`, not human gold. Final quality
metrics and the selective-Full decision are recorded below after that review
is frozen before model-result unblinding.

The Codex runtime does not expose a reliable conversion from these tokens to
weekly-quota percentage or an actual API charge, so neither is fabricated.

### N100 blind quality result and Full decision

All 555 exact inputs were reviewed on 93 opaque-ID sheets before model labels
were opened. The frozen reference is a source-blind Codex
`agent_reference`, not independent human gold. Its semantic ledger SHA-256 is
`6365e16f7732afe0802700e18098f528c94a3e0f71264d8b0880c576a6612a10`.
The post-freeze inspection made zero network, Vision, LLM, or database-write
operations and preserved the semantic DB byte SHA.

| Quality measure | Result |
|---|---:|
| Reviewed / semantic results | 555 / 555 |
| Unrendered fixed-input failures | 7 |
| Strict controlled-field agreement | 4,587 / 4,995 (91.83%) |
| Acceptable-set field agreement | 4,644 / 4,995 (92.97%) |
| Scope acceptable / clear errors | 545 / 555; 10 errors |
| Medium acceptable / clear errors | 527 / 555; 28 errors |
| Unique rows with scope or medium error | 37 |
| Reviewer uncertain rows / model uncertain rows | 70 / 6 |
| Uncertainty axis TP / FP / FN | 3 / 7 / 73 |
| Uncertainty precision / recall / F1 | 30.00% / 3.95% / 6.98% |
| Hero-tier disagreements / critical false positives | 89 / 26 |

The aggregate acceptable-set rate exceeded 90%, but the predeclared gate also
required zero unambiguous scope/medium errors. That requirement failed. The
largest medium pattern was 15 photographs labelled as renderings. Uncertainty
sensitivity also repeated the N10 weakness: the reviewer marked 12.61% of
rows as genuine boundaries while the model marked 1.08%, and it missed all
three reference resolution-insufficient cases.

The expanded gallery lane remains useful independently of that failure.
Reviewer-primary labels found at least one slot absent from P2 top three in
70/100 buildings, adding 118 building-slot pairs from 267 expanded-only
images (0.442 pairs per extra image). The model found the same 70 buildings
and 112 pairs (0.419 per extra image).

Selective Full is therefore blocked, not merely awaiting routine approval.
The next semantic contract should improve photo/render discrimination,
isolated-object and low-legibility scope handling, and explicit uncertainty.
It then needs a new disjoint N10 smoke and, if materially changed, a new N100.
The seven changed-source rows also need a versioned refresh/reconciliation
policy; they must not be silently replaced in this completed sample.

### Prompt v1.1 disjoint N10 result

Prompt v1.1 kept the v1 output schema, controlled vocabularies, normalization,
derivations, and 1024-pixel transform unchanged. It shortened the instructions
and targeted only the measured v1 N100 failures: project-versus-object scope,
photo-versus-rendering evidence, low legibility, construction/ruin, and an
independent uncertainty pass. The new manifest excluded every prior v1 N10 and
N100 building, expected E1 pixel, fetch URL, and successful actual derivative.
It selected 10 buildings and 53 occurrences: 35 Architizer and 18 Divisare
occurrences across eight population-core buildings and two bounded risk
sentinels.

The run fetched all 53 frozen URLs exactly once. Response SHA, E1 pixel SHA,
and actual 1024-pixel derivative identity matched for all 53; there were no
redirects, retries, replacements, or prior-derivative collisions. Eleven
fixed batches returned 53 schema-valid results. The independent validator
passed all 30 checks, and completed resume made zero HTTP and model requests.

| v1.1 N10 measure | Result |
|---|---:|
| Buildings / occurrences | 10 / 53 |
| Exact inputs / semantic results | 53 / 53 |
| HTTP / model calls | 53 / 11 |
| Input / cached input / output tokens | 207,042 / 39,168 / 6,981 |
| Acceptable-set field agreement | 457 / 477 (95.81%) |
| Scope clear errors | 0 |
| Medium clear errors | 1 |
| Critical hero false positives | 0 |
| Usable hero retention | 30 / 30 (100%) |
| Reviewer / model uncertain rows | 11 / 5 |
| Uncertainty row precision / recall | 20.00% / 9.09% |
| Uncertainty axis TP / FP / FN | 0 / 7 / 12 |
| Resolution-insufficient TP / FP / FN | 0 / 1 / 1 |

The blind reference was frozen before model-result inspection and is an
opaque-ID Codex `agent_reference`, not human gold. The 95.81% aggregate and
zero scope/hero-critical errors are encouraging directional evidence, but the
predeclared gate required zero medium errors, uncertainty row precision and
recall of at least 50%, axis recall of at least 40%, and no missed
resolution-insufficient case. The verdict is therefore `FAIL`; reasonable
adjudication of the one medium boundary does not repair the uncertainty or
resolution failures. Because the samples differ, v1 N100 versus v1.1 N10 is
not a controlled statistical comparison.

The next candidate should be a new immutable prompt version, not an edit to
this completed artifact. The smallest measured delta is to clarify that text,
watermarks, or page layout over one photograph do not by themselves make it
mixed; require a per-axis uncertainty audit with visible evidence; and tighten
framing, legibility, elevated-view, and resolution-insufficient rules. It must
first pass another fresh disjoint N10 with two independently frozen blind
references reconciled before unblinding. N100 remains unauthorized until all
hard N10 gates pass.

## Beginner map

The image pipeline separates deterministic image evidence from semantic
interpretation:

1. **E1 fingerprints** requested bounded source derivatives and stored the raw
   response SHA-256, a normalized 512-pixel RGB SHA-256, and a 256-bit pHash.
   E1 did not retain image bytes or infer image meaning.
2. **E2 evidence** combined the two sources' E1 results and recorded exact
   normalized-pixel clusters and direct pHash-neighbour evidence. Similar
   pixels are evidence, not a same-building decision.
3. **E3 selection** retained every successful candidate occurrence and ranked
   transparent P0/P1/P2 top-three shortlists. It did not label an image as an
   exterior, interior, drawing, or detail, and its top three are not an
   automatic Vision queue.
4. **Semantic coverage**, specified here, compares the E3 representative
   shortlist with a gallery-spread probe. A later approved Vision run may
   label only the frozen union of those candidates.
5. Hero choice, coverage slots, and cross-source building reconciliation are
   downstream decisions. They must preserve their evidence and uncertainty
   separately.

## Immutable inputs

| Input | Frozen value |
|---|---|
| E3 DB | `data/enrichment/divisare_architizer_image_selection_e3_full_v1.db` |
| E3 byte SHA-256 | `8512e11f8e1fd581038f790b27a67c0a8b1949067bf53b3ef30c4ea3534141a4` |
| E3 logical SHA-256 | `6b99e4cda9af7c877213a0708f8ba08b1e3780ba3b75c88b7eb9177fc953d3ce` |
| E3 buildings | 91,803 total / 91,183 with successful images / 620 without |
| E3 candidate occurrences | 1,429,581 |
| E3 P2 top-three items | 270,159 |
| E2 DB | `data/enrichment/divisare_architizer_image_evidence_e2_full_v5.db` |
| E2 byte SHA-256 | `4728f9f015d7fba303923df40f550c59835a7e2a5c1316eef03c499ee3ca7b19` |
| E2 logical SHA-256 | `795e2fa6239ed88c462a61179c957db705fda9ca65e5adf03953f699af3b86bc` |

E3 is candidate-only evidence. Its `image_candidates` table contains the
candidate URL, source order, quality flags, normalized-pixel SHA, and E2
record hashes, but it intentionally does not copy E2's raw response SHA.
Consequently the semantic planner must read both immutable artifacts. It joins
them by `(source, source_asset_id)` and verifies the E3 source-record SHA
against the E2 asset record before admitting a candidate.

The planner must open both databases read-only, record complete byte and
logical SHA lineage, and verify their byte SHA values again at the end. It may
not modify either input or create WAL, SHM, or journal sidecars beside them.

## Two selection lanes

The selection manifest keeps representative intent and semantic exploration
as separate memberships. The same occurrence may belong to both lanes, but it
is inferred only once after exact input identity has been established.

### Representative lane

`representative_top3` is the frozen E3 P2 shortlist. P2 preserves the P1
rank-one result and reduces repeated top-three views using exact and direct
pHash evidence. It expresses source editorial order, minimum technical
quality, and low-level visual redundancy only. It does not express image type
or semantic diversity.

### Coverage lane

`coverage_probe` samples a wider part of each source gallery without claiming
to know what those positions depict. The frozen union contains at most six
candidates per building:

1. the P2 representative shortlist at ranks one through three;
2. the nearest eligible gallery candidate to the start of the gallery span;
3. the nearest eligible gallery candidate to the middle of the gallery span;
4. the nearest eligible gallery candidate to the end of the gallery span.

P2 rank one is also the P1 rank-one anchor, so that occurrence carries both
the representative and coverage-anchor memberships without consuming another
slot.

Candidates are considered in stable source ordinal and asset-ID order. For
each early, middle, or late target, the planner scans outward by distance from
that position and admits the first candidate that is not redundant with a
previously admitted anchor or probe. Redundancy is tested in this order:
exact normalized pixels, identical pHash, then a direct pHash Hamming distance
of at most eight. This is a chosen-star comparison only; no transitive graph
closure is allowed. If every candidate for a target is redundant, the target
remains unfilled with an explicit reason rather than admitting a hidden
fallback.

The planner exposes the E3 P2 top three and the expanded representative-plus-
coverage union of at most six positions. This permits a direct coverage-gain
comparison without paying twice for an identical Vision input. Gallery
position is only an exploration heuristic. It must never be described as
proof that an interior, drawing, or any other type exists.

## Pixel-only semantic contract

The model receives opaque inference IDs and image pixels. It does not receive
source URLs, filenames, existing tags, source type hints, historical labels,
project names, or E3 policy scores. The response contains visible facts, not a
final hero decision or same-building judgment.

Every result has exactly these required fields:

| Field | Controlled meaning |
|---|---|
| `asset_id` | Exact opaque inference ID assigned to the attachment |
| `in_scope` | Whether coherent architecture or architectural representation is visibly dominant |
| `reject_reason` | `none`, blank/unreadable, non-architectural subject, people/event, isolated object/artwork/sample, text/logo only, no project visible, non-dominant collage, or `other` |
| `medium` | `photograph`, `drawing`, `rendering`, `physical_model`, `mixed`, `other`, `unknown` |
| `spatial_context` | `exterior`, `interior`, `threshold`, `not_applicable`, `unknown` |
| `framing_scale` | `site_context`, `overall`, `element_detail`, `material_detail`, `not_applicable`, `unknown` |
| `camera_angle` | `eye_level`, `elevated`, `aerial_oblique`, `aerial_top_down`, `not_applicable`, `unknown` |
| `drawing_kind` | `plan`, `site_plan`, `section`, `elevation`, `axonometric`, `perspective`, `detail`, `diagram`, `sketch`, `composite`, `other`, `not_applicable`, `unknown` |
| `project_state` | `visibly_finished`, `construction_visible`, `ruin_or_abandoned_visible`, `demolition_visible`, `not_applicable`, `unknown` |
| `project_legibility` | `high`, `medium`, `low`, `none`, `unknown` |
| `uncertain_axes` | Controlled names of genuinely unresolved axes |
| `resolution_insufficient` | Whether more pixels could materially resolve an uncertainty |
| `evidence` | One short sentence grounded only in visible pixels |

The JSON Schema uses `additionalProperties: false`, requires every field, and
uses enums for every controlled value. Codex CLI previously rejected the JSON
Schema `uniqueItems` keyword, so duplicate uncertainty axes are rejected by
the Python validator instead. Applicability rules are also enforced after the
schema check: for example, a photograph cannot have a drawing kind, a drawing
must have a specific drawing kind, out-of-scope rows require a reject reason,
and every `unknown` axis must be named as uncertain. Invalid combinations are
retained as failed attempts and are never silently rewritten to `exterior` or
another default.

The existing Divisare axes vocabulary and invariant code are useful design
references, but its v2.5 prompt is not a production contract. Its fresh N50
holdout found every judged field acceptable on only 41/50 images and detected
only 3/11 reviewer-ambiguous images. The semantic-coverage contract therefore
gets a new source-neutral version and its own N10/N100 evidence.

## Hero and coverage remain separate

The model does not output `final_hero`. Two deterministic, auditable products
are derived later from the pixel facts:

- `hero_candidate_decisions` combines E3 editorial rank with in-scope status,
  project legibility, framing, project state, and uncertainty. Drawings,
  renderings, and details remain explicit fallback candidates rather than
  being deleted.
- `coverage_slot_assignments` is many-to-many. Proposed slots include
  `exterior_overall`, `exterior_context`, `interior`, `drawing_plan`,
  `drawing_section`, `drawing_other`, `detail`, `aerial_context`,
  `model_or_render`, and `construction_or_archive`.

An unfilled slot is `not_observed_in_sample`, never proof that the project has
no such image. A later hero comparison or UI choice is a separate policy and
must not overwrite the per-image observations.

## Fail-closed image fetch and input identity

The offline manifest pins, for each selected occurrence:

- E3 selection, candidate, and source-record SHA values;
- E2 asset and relation record SHA values;
- expected E1 raw response SHA-256;
- expected E1 normalized 512-pixel SHA-256;
- frozen E1 fetch URL and all input run/version identifiers.

Only a separately approved N10 runner may fetch those URLs. For every HTTP
attempt it immediately records attempt number, request and final URL, HTTP
status, content type, byte count, elapsed time, expected and actual response
SHA, error, and retry disposition. It then runs the frozen E1 normalization
again and compares the actual normalized-pixel SHA.

The terminal fetch states are distinct:

- `exact_match`: raw response and normalized pixels both match E1;
- `delivery_changed_pixel_stable`: raw bytes changed but normalized pixels
  still match;
- `source_changed`: normalized pixels changed;
- `http_failed`, `invalid_content`, `decode_failed`, or `oversize`.

N10 is fail closed: only `exact_match` becomes a Vision input. A changed or
failed row remains attached to the fixed manifest and is not replaced after
its result is known.

The runner freezes a deterministic no-crop, no-upscale Vision-input transform
before N10 and records its version, encoded SHA, decoded pixel SHA, width, and
height. Semantic-result reuse requires equality of the actual Vision-input
pixel SHA, dimensions, prompt version, output-schema SHA, and model. E1
normalized-pixel equality may be used to estimate deduplication during
planning, but it does not authorize reuse when the actual Vision inputs have
not been shown identical.

pHash similarity never authorizes semantic-result reuse. It only reduces
obviously repetitive candidates in the coverage planner. No transitive pHash
component is treated as an image identity.

All image files exist only inside a batch-local temporary directory and are
deleted after the durable attempt/result commit. The final sidecar retains
hashes and metadata, not image bytes.

## Sidecar plan

The Vision artifact is a new immutable SQLite sidecar, not a modification of
E1, E2, E3, curated metadata, or legacy `image_derived` fields. Its minimum
ledger is:

| Table | Purpose |
|---|---|
| `semantic_runs` | Run state, all input paths and SHA values, versions, model settings, totals, logical SHA, error |
| `selected_buildings` | Fixed building population, stratum, and selection-record SHA |
| `selected_occurrences` | Candidate membership in representative and coverage lanes, positions, reasons, and E2/E3 lineage |
| `fetch_attempts` | Every HTTP attempt and exact-SHA comparison, committed before retry |
| `vision_inputs` | Verified response identity and the exact derivative bytes/pixels shown to the model |
| `vision_attempts` | Ordered IDs, model/prompt/schema/runtime lineage, status, elapsed time, tokens, and errors |
| `vision_attempt_payloads` | Full raw JSONL or compressed raw payload plus SHA; stderr is retained or hashed with a bounded excerpt |
| `semantic_results` | Raw model result JSON and separately normalized result JSON |
| `occurrence_result_links` | One inference result to every occurrence that may reuse it, including the exact reuse basis |
| `hero_candidate_decisions` | Non-authoritative deterministic hero tiers and reason codes |
| `coverage_slot_assignments` | Non-authoritative derived slot memberships and uncertainty |
| `validations` | Independent expected/actual checks with severity and detail |

Suggested run states are `initializing`, `running`, `complete`,
`complete_with_failures`, and `failed_validation`. A successful inference is
committed before its temporary input is deleted and before the worker claims
another item. Resume is permitted only when the input SHAs, ordered manifest,
selection/prompt/schema/runtime versions, model settings, and output path all
match. A completed artifact resumes with zero network and model requests.

The raw model response and normalized interpretation are separate. Token
fields store observed input, cached-input, and output counts. A list-price
equivalent is derived only from a versioned pricing snapshot; it is not called
an actual charge. The Codex weekly quota percentage is unobservable unless the
service exposes it and must not be fabricated from token counts.

The legacy `tools/d2_cover_vision.py`, `tools/e2_vision_5type.py`, global
`enrich/tasks_db.py`, and `enrich/image_analysis.py` are not suitable as this
sidecar's authority. They lack the combined asset-byte lineage, attempt
ledger, or semantic quality boundary required here. The strict batch runtime,
E1 normalizer, and immutable benchmark publication patterns may be reused.

## Offline preflight validation

Before requesting N10 approval, an independent validator reopens E2 and E3
read-only and recomputes:

- complete building and candidate accounting;
- every E2/E3 join and source-record SHA;
- representative and coverage membership and reason codes;
- ordered building, occurrence, URL, and expected-SHA manifest digests;
- exact-dedup planning groups without using pHash as identity;
- expected unique fetches, Vision inputs, batches, and temporary storage;
- source balance and gallery-depth strata;
- zero network, Vision, LLM, Neon, and R2 operations;
- SQLite quick, integrity, and foreign-key checks for the planning sidecar.

The preflight report presents the fixed N10 rows, not just aggregate counts,
and includes the E2/E3 byte SHA values before and after planning. It must also
state that planned exact reuse is provisional until actual Vision-input pixel
identity is verified after fetch.

## N10 gate

N10 means ten fixed buildings, not ten images. Its representative/coverage
union will normally contain roughly 40-60 occurrence memberships; the offline
manifest must report the exact occurrence, expected fetch, actual-input, and
batch counts before approval. The sample balances sources and gallery depths
while including a bounded number of P1 changes, P2 suppressions, QA fallbacks,
and ordinary controls. Known failures must not dominate the sample.

Technical acceptance requires:

- no post-result sample replacement;
- 100% exact raw-response and normalized-pixel match for selected fetches;
- one valid result for every planned unique Vision input;
- exact opaque-ID accounting and 100% schema/invariant validity;
- no coercion of invalid or unknown output to a plausible default;
- a durable row for every fetch and model attempt, including failures;
- complete input, cached-input, output-token, elapsed-time, and byte totals;
- unchanged E2 and E3 SHA values before and after the run;
- SQLite quick/integrity/foreign-key checks and independent manifest/logical
  SHA recomputation passing;
- zero retained temporary images;
- a completed-run resume producing zero fetch and model requests;
- no source, canonical, Neon, R2, or vector-database writes.

Every N10 image is then reviewed blind to source hints. The report separates
technical success from semantic quality and includes:

- clear-image scope/medium errors;
- acceptable decisions over every judgeable axis;
- row-level uncertainty false negatives and false positives;
- whether each non-QA representative anchor visibly contains a legible
  architectural subject;
- distinct coverage slots per building under P2 top-three and the expanded
  representative-plus-coverage union;
- incremental exterior/interior/drawing/detail discoveries;
- exact-input reuse rate;
- calls, tokens, latency, downloaded bytes, and N100 projections.

N10 calibrates the contract and cost; it cannot establish production
accuracy. A useful initial quality gate is zero unambiguous scope/medium
errors and at least 90% acceptable decisions over judgeable fields, with every
uncertainty disagreement shown row by row. Failure triggers prompt/schema
revision and a new version rather than patching the completed artifact.

## N100 and full boundary

N100 uses a new fixed sample disjoint from N10 at building and actual
Vision-input pixel identity. It mirrors the real source/gallery-depth
population rather than over-sampling known failures. Quality claims require a
reviewed reference set; agent-produced labels must be identified as such and
must not be described as independent human gold. The prior Divisare axes
development and consumed holdout sets cannot serve as a fresh production
holdout.

N100 reports per-axis confusion and acceptable-label agreement, uncertainty
performance, source and stratum failure rates, coverage gain over P2 top-three,
hero-candidate disagreement, exact reuse, token and cost distribution, retry
rate, and projected selective-full sizes. It also repeats every N10 technical
integrity gate.

Only after N100 may a separate proposal choose among:

- hero-anchor-only Vision;
- representative top-three Vision;
- expanded semantic coverage;
- a recommended selective queue limited to disagreement, QA, cross-source,
  or missing-coverage cases.

That proposal must state exact unique inputs, expected calls, token and
list-price-equivalent ranges, weekly-quota uncertainty, runtime, storage, and
resume behavior. It requires explicit user approval and creates another
immutable artifact. It does not overwrite E3 or make an automatic
Divisare-Architizer building merge.
