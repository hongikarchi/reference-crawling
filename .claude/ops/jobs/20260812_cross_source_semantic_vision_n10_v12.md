# Cross-source semantic Vision N10 v1.2

## Status

`N10_COMPLETE_WITH_5_SCHEMA_REJECTED_RESULTS; TECHNICAL_GATE_FAIL; BLIND_QA_FAIL; V1.2_N100_AND_FULL_NOT_RUN`

This job calibrated a prompt-only v1.2 semantic contract on a fresh,
source-blind, disjoint Divisare + Architizer N10. It did not build a semantic
corpus, merge buildings, or modify E1/E2/E3, source/curated metadata,
canonical data, Neon, R2, or vector data.

The run is complete as an immutable failed calibration artifact. Five inputs
lack semantic results because batch-level validation rejected one invalid
drawing row twice. The reconciled blind review also failed the semantic gate.
No v1.2 N100, Full, selective queue, or downstream merge was run.

## Prompt-only measured delta

Prompt v1.2 kept the v1.1 output schema, controlled vocabularies,
normalization, deterministic hero/coverage derivations, 1024-pixel transform,
model, reasoning, service tier, and image detail unchanged. It changed only
the measured v1.1 failure boundaries:

- text, watermark, or page-layout overlays on one photograph do not alone
  make the image `mixed`;
- each axis requires a second uncertainty pass grounded in visible evidence;
- photo/render, material/element, element/overall/site, eye/elevated,
  detail/composite, and project/object boundaries are explicit;
- framing, legibility, and `resolution_insufficient` rules are tighter.

Frozen prompt pins:

- contract: `cross-source-image-semantics-v1.2.0`
- prompt: `cross-source-image-semantics-prompt-v1.2.0`
- prompt sentinel SHA-256:
  `041d6b7b0c6de4abc9989c585cd7a45267932eb6812f0225e0d12f11b99f7d73`
- prompt size: 5,116 bytes/characters, +6.54% from v1.1 and -4.39% from v1
- output schema SHA-256:
  `e72058e372e7883fc8ca80d690728fb16b9a7fdd469037f13311a8e04069eff4`
- transform: `cross-source-vision-input-1024-jpeg-v1.0.0`

## Frozen disjoint selection

Manifest:
`data/reports/cross_source_semantic_coverage_n10_v1_2.json`

- seed: `archibe-semantic-coverage-n10-v1.2`
- 10 buildings / 50 occurrences / 50 unique planned E1 pixels
- source allocation: Architizer 7 buildings / 32 occurrences; Divisare 3 / 18
- population core 8: Architizer ordinary 5 + cross-source candidate 1;
  Divisare ordinary 2
- risk sentinels 2: Architizer cover-quality 1; Divisare gallery-fallback 1
- eligible population 91,183; exclusion union 121; sampling frame 91,062
- prior exclusion ledger: 120 buildings, 672 expected E1 pixels, 665
  successful actual derivatives, 1,344 URLs, and seven expected-only
  `source_changed` failures
- selected overlap with v1 N10, v1 N100, and v1.1 N10: building 0 /
  expected E1 0 / fetch URL 0 / successful derivative 0

Manifest identities:

- bytes / byte SHA-256: 983,229 /
  `00d494c58590a9a0ca3d78de858f857f6a1e226390018f5426d799a846ead8fe`
- self SHA-256:
  `44f9c5ca47adbacd8fc0f87be11e59b6468d57205831e2c5d78b0f96438a495a`
- ordered building SHA-256:
  `8008b9972ce779488e280a0709013a3a13916d9936ce1aaafb1fa1a13c3363fb`
- ordered occurrence SHA-256:
  `939cea9f514a0caeb4fa4ec7bc9688f062ae212318f4d458ae551b04574cc314`
- ordered fetch URL SHA-256:
  `1cf542b62edd93defd375d1e070f06975bae92ce4b5d80a2834a63d77e990d3e`
- ordered E1 pixel SHA-256:
  `613bce3f755497e9944c7368c6d720bd913a7ee58d1523afef2bef09d3e05d5c`

The independent production planner replay regenerated the canonical manifest
byte-for-byte twice. Final validation passed 7/7 checks with zero network,
Vision, or LLM requests.

## Hardened runtime pins

- runner: `cross-source-semantic-vision-n10-v1.2-runner-v2.0.0`
- retry: `cross-source-semantic-vision-n10-v1.2-retry-v2.0.0`
- schema version: 4
- schema SQL SHA-256:
  `7eebb5135000125d3e2a453bf48526d28518fe78fe2d861672b18b30bd382640`
- SQLite schema objects SHA-256:
  `e77a4be8896aa474720155e217162a7592d0f5a6018c3b8c2c5ea1f09efc2401`
- dependency manifest SHA-256:
  `31e93728ab3c2743d251cfa1e2392d36664bcb4322199fa21ce78ed032af0aba`
- Codex CLI / model / reasoning / service tier / detail:
  `0.147.0` / `gpt-5.6-sol` / `low` / `fast` / `high`
- batch size / model-call reservation cap: 5 / 12
- observed token stops, input / output / total: 250,000 / 10,000 / 260,000

Every physical HTTP and model call was reservation-ledgered before execution.
All 50 exact derivatives had to materialize before the first Vision call, and
the prior 665 successful derivative identities were excluded. Initialization,
retry/circuit state, crash-open reservations, publication, completed resume,
and an independent terminal replay were fail-closed and recoverable. Token
thresholds were post-call observations, not server-enforced hard ceilings.

## Actual N10 execution

- run ID: `semn10v12-82ddad3e9cb37d3f640209c7`
- status: `complete_with_failures`
- wall time including preflight and terminal validation: about 372.7 seconds
- buildings / occurrences: 10 / 50
- HTTP requests / fetch attempts: 50 / 50
- exact response and E1 pixel identities: 50 / 50
- downloaded bytes: 5,992,703
- model attempts / reservations / completions: 12 / 12 / 12
- successful / failed model attempts: 9 / 3
- semantic results: 45
- input states: 45 `success`, 5 `vision_failed`
- input / cached-input / output tokens: 225,713 / 39,168 / 8,635
- non-double-counted input plus output: 234,348 tokens
- unknown-usage attempts: 0

Batch 9 (`semv12_000041` through `semv12_000045`) was tried twice. The raw
JSON and ordered IDs were intact, but `semv12_000043` was a drawing whose
`framing_scale` was not `not_applicable`. The strict drawing invariant rejected
the entire five-row batch atomically on both attempts. Batch 10 failed once
under the same invariant and then succeeded on its second attempt. The fixed
model-call cap was exhausted, so the terminal state correctly retained the
five batch-9 inputs as `vision_failed` rather than fabricating or replacing
results.

Independent validation passed SQLite quick/integrity checks, zero foreign-key
violations, request/reservation replay, raw JSONL and result replay, cache and
manifest identity, report binding, logical SHA, and absence of WAL/SHM/journal.
A completed `--resume` made HTTP 0 / Vision 0 requests and reproduced the same
logical SHA.

Outputs:

- DB: `data/enrichment/divisare_architizer_semantic_vision_n10_v1_2.db`
- DB bytes / byte SHA-256: 851,968 /
  `3b37136dae709c1d47945e40a0596600b18b4cafee6fb64f8fbd872ea34e7c95`
- logical SHA-256:
  `306f256fee76e718eed1aaea0a78edfd816e75332bb28f6783606766dc86c78c`
- report bytes / byte SHA-256: 742 /
  `ea4466fbcc8a1a359688eb64065728ccdb4be3e6262b878c7d0df4642cca4544`
- retained review cache: 50 exact JPEG derivatives / 8,339,177 bytes

## Two-reference blind QA

All 50 review images were rendered under opaque `semv12_*` IDs. Two Codex
agents independently reviewed the pixels without source, project/name, URL,
selection, peer, or model semantic labels. Both full 50-row ledgers were
frozen before exchange or model unblinding. These are agent references, not
independent human gold.

- review manifest bytes / byte SHA-256: 29,463 /
  `614aa209713fba211ca1a845f6f985bff420cd0554570c3f9ffccf2bc25985f4`
- review manifest self SHA-256:
  `42ff6eb4943890e0199640fc0252b2b9034961b4744a9e35bbcfde0fb11f5311`
- reviewer A frozen byte / semantic SHA-256:
  `3c22fb2ba5721a6585ebab11dc0b583c01ea364cfe216e7d2a5ee850c3f96338` /
  `fcc1458a1417394977e1a4470c39ce9e5a1120f5be184a11d0d99165a6e0eb46`
- reviewer B frozen byte / semantic SHA-256:
  `57b1487ae2af5399b9cf4e872014fd3d234da3e1807e8789d7e26b91bcd6b8d6` /
  `4ffb347237a7213766876838d8c941e3cacb786335d4c84e7dd2b9e4aab5135d`
- reconciled frozen byte / semantic SHA-256:
  `7e37b916daf180b7548328c78fe53d2232d396bf7bca382afe53535cb07e832d` /
  `6732899b7ab49892048b0f062839656a52d8d71bef22138b0f7b18679975338f`
- disagreements: 31 rows / 122 dimensions / 0 unresolved
- reconciliation completed before unblinding: true
- inspection bytes / byte SHA-256: 17,667 /
  `962bf3ae2520cbfd75c960dd9575a5f168dcb91dc509747264dc04cb0899ac3a`
- inspection semantic SHA-256:
  `70c1e952cb80be9150dbf6ead9480268130592cea6cdfccf4d7967e0688c2fa9`

Inspection was read-only and compared the reconciled reference with the 45
available results:

- exact population accounting: PASS, but result coverage 45/50: FAIL
- nine-field acceptable agreement: 380/405 = 0.938272: PASS
- scope clear errors: 2: FAIL
- medium clear errors: 1: FAIL
- critical hero false positives: 5: FAIL
- usable hero retention: 19/24 = 0.791667: FAIL
- reference/model uncertain rows: 20/11
- uncertainty rows TP/FP/FN: 7/4/13; precision 0.636364, recall 0.35,
  F1 0.451613: FAIL
- uncertainty axes TP/FP/FN: 3/9/23; precision 0.25, recall 0.115385,
  F1 0.157895: FAIL
- resolution-insufficient TP/FP/FN: 0/0/1: FAIL

Final inspection verdict: `FAIL`. The missing five results alone violate the
100% technical-coverage gate, and the matched subset independently violates
scope, medium, uncertainty, and hero gates. No inference about the missing
five was substituted into the model comparison.

## Immutable artifact and possible follow-up

The stored highest batch-9 payload could be normalized offline by setting the
drawing row's framing axis to `not_applicable`; all five rows then satisfy the
existing schema. That would require zero network and zero model calls. It is a
proposal for a separately versioned v1.2.1 recovery and regression test, not a
repair performed in this job. The completed v1.2 DB, report, raw attempts, and
quality verdict remain immutable and were not patched.

Because this N10 failed, v1.2 N100 and Full remain unauthorized. Any next paid
run first needs an immutable runtime/prompt decision and a new approved smoke
boundary; this job does not authorize it.

## Tests and Git handoff

- prompt/selection/runtime/review focused tests: PASS
- independent manifest validation: 7/7 PASS
- independent terminal validation: PASS
- completed resume: HTTP 0 / Vision 0
- final repository pytest: 1,115 passed / 22 skipped / 1,453 subtests passed
- Git commit: recorded in the final handoff after this job card is included in
  the same logical commit
- origin/main push: not performed; no push was requested for this job
- staging boundary: v1.2 canonical/tools/tests, this job card, and the
  semantic coverage document only; no `data/` or temporary artifacts
