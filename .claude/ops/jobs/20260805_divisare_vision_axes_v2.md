# Divisare 1024px image axes v2

## Status

`FRESH ONE-SHOT N50 COMPLETE; FULL RUN BLOCKED ON SEMANTIC QUALITY`

## Objective

Replace the overloaded v1 five-class image prompt with independent visible
facts at a maximum 1024-pixel long edge. Derive search classes in code, then
test the new contract on a fixed development-only N10/N20/N50 ladder.

This work does not establish production accuracy. Development labels and the
later fresh-holdout labels were produced by isolated Codex-agent reviews, not
independent humans.

## Scope

Included:

- pixel-only axes for scope, medium, spatial context, framing, camera angle,
  drawing kind, and visible project state
- deterministic primary/secondary search classes and use status
- immutable N50 development candidates with nested N10/N20 prefixes
- two blinded 1024px reviews plus explicit adjudication
- frozen max2048 response SHA verification and transient local 1024px input
- SQLite result, plain-language report, token accounting, resume, and
  no-clobber validation

Excluded:

- production/full Divisare image processing
- material or architectural-element Vision labels
- final accuracy claims or an independent human holdout
- persistent source/image corpus
- vector DB, Neon, or R2 writes

## Frozen inputs

- Source DB: `data/curated/divisare_metadata_v2_4.db`
- Source DB SHA: `9c523f3393d20ae8732677981c207abd02247ca5ce905dec422a05fa0398f70f`
- Candidate manifest:
  `data/review/divisare_vision_axes_dev_n50_candidates_v1.json`
- Candidate file SHA:
  `8bddc0cf1210c0fc63390943b8802ca194c927ff2454d292851b9ed87cb1cc5a`
- Candidate logical SHA:
  `7acf3e0cb18fb951511ef08c2b24a1e16bcb179f11d10a697d7ad06e06353913`
- Candidate manifest SHA:
  `414f74db5530e320013a62b7e0056f29af8de5ff62a9db49c6daf672f9341a29`
- Review codebook SHA:
  `9d9686b5857844d1b4cfa6a81edbfd8ed3e47ee03536fa854a8dfd78fca0cb1b`

The N50 set contains 50 unique assets, articles, buildings, and 512px identity
pixel hashes. It contains no exact duplicate or pHash-distance <=8 pair.
Composition is modern 37 / legacy 13 and includes prior errors, boundary
views, clear controls, representation/state cases, and out-of-scope cases.

## Contract

- Axis contract: `divisare-image-axes-v2.0.0`
- Prompt: `divisare-image-axes-prompt-v2.0.0`
- Executed runner: `divisare-vision-axes-development-1024-v1.0.0`
- Current evaluator: `divisare-vision-axes-development-1024-v1.1.0`
- Model: `gpt-5.6-sol`, reasoning `low`, service tier `fast`
- Batch: 5 images
- Limits: exactly 10, 20, or 50
- Source fetch: Divisare max2048 JPEG q92, frozen content SHA required
- Model input: local JPEG q92, 4:4:4, long edge at most 1024, no upscaling
- Images: temporary only; no image bytes in SQLite

Technical success and classification quality are separate. A transport/schema
PASS does not mean the labels are good enough for production.

## Verification

- New axes pipeline tests before model use: `50 passed`
- Current focused tests after scoring corrections: `39 passed`
- Current five-module axes pipeline tests: `55 passed`
- Current broader Vision regression: `113 passed`
- Python compilation: PASS
- `git diff --check`: PASS
- Review templates: 50 opaque IDs each; URL/source/historical labels absent
- Transient review staging: 50/50 frozen source responses and 1024 derivatives

## Outputs

- Compatible gold: `data/review/divisare_vision_axes_dev_gold_n50_v1_1.json`
- N10 DB/report: `data/smoke/divisare_vision_axes_n10_v1_1.db`,
  `data/reports/smoke/divisare_vision_axes_n10_v1_1.md`
- N20 DB/report: `data/smoke/divisare_vision_axes_n20_v1_1.db`,
  `data/reports/smoke/divisare_vision_axes_n20_v1_1.md`
- N50 DB/report: `data/smoke/divisare_vision_axes_n50_v1_1.db`,
  `data/reports/smoke/divisare_vision_axes_n50_v1_1.md`
- N50 interpretation: `data/reports/smoke/divisare_vision_axes_n50_v1_1_audit.md`
- Preserved schema-failure evidence:
  `data/smoke/divisare_vision_axes_n10_v1.db.partial`

## Result

The first N10 attempt was rejected before token use because the Codex response
schema does not accept JSON Schema `uniqueItems`. The unsupported keyword was
removed while duplicate uncertainty axes remain rejected by Python validation.
N10, N20, and N50 then completed with all technical checks passing.

N50 quality:

- every judgeable field acceptable on 40/50 images
- acceptable field answers 276/289 (95.5%)
- clear-image derived main class 37/43 (86.0%)
- clear-image supporting classes 39/43 (90.7%)
- clear-image use/review/exclude decision 39/43 (90.7%)
- weakest fact: framing scale 29/33 (87.9%)
- reviewer uncertainty: 7 images / 9 axis occurrences; model flagged 0
- `resolution_insufficient`: 0/50

The original N50 report's downstream applicability values forced the preferred
gold branch for ambiguous scope cases. Evaluator v1.1 excludes conditionally
applicable rows; corrected applicability is 46/48 for spatial/framing/camera,
48/49 for drawing kind, and 47/48 for project state. Executed artifacts remain
immutable and the correction is documented in the audit report.

Successful model usage across N10/N20/N50 was 16 calls, 311,221 input tokens
(77,056 cached input included), 9,861 output tokens, and 263,374 ms of model
time. Downloads totaled 24,043,925 bytes. The runner cannot observe the Codex
weekly-limit percentage. No image corpus, Neon, R2, vector DB, or source DB was
written.

Verdict: keep the axis design and 1024px lane, but do not start the full image
run until uncertainty, framing, and out-of-scope rules are revised and retested.

## Prompt v2.5 and fresh one-shot holdout

Prompt `divisare-image-axes-prompt-v2.5.0` improved the reused development N50
to 47/50 images with every judged field accepted and 285/289 accepted answers.
Because those examples had informed prompt changes, that result was not used as
the production decision.

A new candidate N100 was selected with zero overlap against the earlier N560 at
asset, article, and building levels. The real-image probe downloaded 96/100;
four frozen URLs returned HTTP 404. No exact, pixel, pHash <=8, or pHash 9-16
collision was found inside the new probe or against the prior set. A balanced
N50 with nested N10/N20 prefixes was frozen from the 96 successful rows.

Fresh review inputs were labeled twice in isolated Codex contexts and 29
disagreements across 20 images were adjudicated in a third context. The final
gold has 50 rows and explicitly records `independent_human=false`.

Frozen holdout inputs and outputs:

- Candidate N50:
  `data/review/divisare_vision_axes_holdout_n50_candidates_v1_1.json`
- Gold N50: `data/review/divisare_vision_axes_holdout_gold_n50_v1.json`
- Gold file SHA:
  `0429e2299434d03f5c1ec19db4bd551a7461cf459b0cebf8180200553574898f`
- Gold logical SHA:
  `d7086edc1e9220c8e3652e6376dabc53237fb52b606ce3f088083c4f2b7e5026`
- Result DB: `data/smoke/divisare_vision_axes_holdout_n50_v2_5.db`
- Report: `data/reports/smoke/divisare_vision_axes_holdout_n50_v2_5.md`
- Result logical SHA:
  `fa538d41378bcdb7087d8f41d8a29a503d51ef12050f447affb7a91e83ef8ebc`
- One-shot receipt SHA:
  `b5eb62b251f242af6cd63ae69c417ecf85441f57df4f576cafeb9675ab81ad83`

The runner permits only a single N50 logical run for this holdout. Its receipt
binds source, gold, row order, prompt text, schema, runtime, model, and output
paths. N10/N20 metrics are computed from ranks 1-10 and 1-20 of that same N50.

Fresh result:

- N10: all fields accepted on 8/10; accepted answers 62/64
- N20: all fields accepted on 17/20; accepted answers 128/131
- N50: all fields accepted on 41/50; accepted answers 314/327
- framing scale: 38/43 accepted
- main/supporting/use-status derivation: 46/50, 45/50, 40/50
- reviewer-ambiguous images detected: 3/11
- ambiguous field occurrences detected: 3/12, with four extra flags
- resolution insufficient: 0/50
- model calls: 10 successful, 0 failed
- exact run usage: 205,382 input, 33,024 cached input included, 7,091 output
- downloads: 15,646,337 bytes
- source SHA before/after: unchanged
- persistent image bytes: none

The nine images with at least one rejected field concentrate on framing scale,
visible completion state, threshold versus exterior, physical-model medium,
scope/reject consistency, and eye-level versus elevated views. Some are clear
model errors; some expose debatable model-assisted gold. Either way, uncertainty
recognition is too weak for a 547k-image production run.

Next action: treat this consumed holdout as diagnostic only, tighten definitions
on development data, obtain independent human decisions for the ambiguous
cases, then create a new disjoint one-shot holdout. Do not start the full run
from prompt v2.5.
