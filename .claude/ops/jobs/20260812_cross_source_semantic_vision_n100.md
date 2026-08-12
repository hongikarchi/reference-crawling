# Cross-source semantic Vision N100

## Status

`N100_COMPLETE_WITH_7_FIXED_INPUT_FAILURES; SEMANTIC_QUALITY_GATE_FAIL; FULL_BLOCKED_NOT_RUN`

The approved, population-shaped Divisare + Architizer semantic Vision N100
completed. It is calibration evidence only: no E1/E2/E3, curated/source,
canonical, Neon, R2, or vector data was changed, and no selective/full Vision
queue was run.

## Frozen population

- seed: `archibe-semantic-coverage-n100-v1`
- sample frame: 91,173 buildings after excluding the ten N10 buildings
- selected: 100 buildings / 562 image occurrences / 562 unique E1 pixels
- source allocation: Architizer 67 buildings / Divisare 33 buildings
- stratum allocation: Architizer ordinary 60 + cross-source 7; Divisare
  ordinary 26 + cross-source 6 + gallery-fallback 1
- N10 overlap: building 0 / E1 pixel 0 / fetch URL 0
- manifest byte SHA-256:
  `37d917dc2c5b7c89308641b97035255e1fab5e469a0fc86935bf716559ead540`
- manifest self SHA-256:
  `9d34f68ceba5dc561169ed5c3b6fecfc9284dc36fb6ffd544be9198f9faf80ec`
- ordered building SHA-256:
  `edfc89773225828378c56e7cb7c9a9ef8bb5768ed91c50dc03ae464ec6299802`
- ordered occurrence SHA-256:
  `f8492352487b33540dd25861f299f0ea55079f4fd9395899257d52de7c487e8b`

Independent canonical replay regenerated the entire manifest byte-for-byte.
The ignored manifest is
`data/reports/cross_source_semantic_coverage_n100_v1.json`.

## Actual run

- run ID: `semn100-fc21358c3bf06b9498cc57f6`
- status: `complete_with_failures`
- wall time: 2,168 seconds from stored start/completion timestamps
- fetch attempts / retries: 562 / 0
- exact inputs / fixed-input failures: 555 / 7
- successful Vision batches / total attempts / retries: 113 / 116 / 3
- successful semantic results: 555
- downloaded bytes: 83,719,902
- input / cached input / output tokens: 2,216,339 / 1,303,552 / 71,935
- non-double-counted input + output: 2,288,274 tokens
- summed Vision latency: 1,625.327 seconds
- completed resume: HTTP 0 / Vision 0

The seven failures were all Architizer `source_changed` rows: the current
response and E1 pixel identities no longer matched the frozen E1 evidence.
They were not replaced. Architizer exact success was 361/368; Divisare was
194/194. The retained failed inference IDs are `semv100_000019`,
`semv100_000054`, `semv100_000085`, `semv100_000099`, `semv100_000236`,
`semv100_000282`, and `semv100_000329`.

## Immutable output and validation

- DB:
  `data/enrichment/divisare_architizer_semantic_vision_n100_v1.db`
- DB bytes: 7,106,560
- DB byte SHA-256:
  `e2a03e004a27ce662a93f60ad16190014789abf60f9433926ff9e38e747c8a8e`
- logical SHA-256:
  `717abc3df21c9fa5e182a52fe099d8c52af71ba8c6ef5256673c41ac1c75205d`
- report byte SHA-256:
  `83c1f25cca4dcbd02dcdf633bbd07b4396530422bdce7f062931734fee9d0cfd`

The independent validator passed every error-severity check: manifest replay,
E2/E3/N10 lineage and byte identity, exact table and trigger SQL, fetch and
Vision attempt replay, raw JSONL, normalized semantics, result links, hero and
coverage derivations, the 125-call cap and observed client-side token stop
thresholds, cache identity, SQLite quick/integrity/foreign keys, logical SHA,
report binding, and terminal cleanup.

The 2.7M token threshold is not a server-enforced hard ceiling. Codex CLI does
not reveal the exact input usage before a call, so the client can stop only
after observing the prior/completed calls and a final call could cross the
threshold. This run remained below it at 2,288,274 non-double-counted tokens.

## Runtime open QA

A final static audit found no defect that invalidates this uninterrupted
artifact, but identified hardening required before another paid execution:

- reserve each HTTP and Vision call durably before invoking the external
  process; a hard kill during a call can currently leave an unledgered request
  that resume could repeat;
- add an initialization/publication recovery state machine for crashes before
  the first run commit or between terminal DB commit, report creation, and
  two-file publication;
- treat the call cap as reservation-based and describe tokens as an observed
  stop threshold unless the server exposes enforceable per-call limits;
- make the review renderer/inspector independently reject SQLite sidecars and
  literal-pin the production manifest/run identity;
- pace and account redirect hops individually if the contract claims two
  actual HTTP requests per second rather than two logical fetch starts.

These changes require a new runner/retry/schema version with a legacy
validator branch so this v1.0.0 DB and its byte/logical SHA remain immutable.
They are prerequisites to a paid rerun, not reasons to mutate or repeat the
completed N100.

## Blind QA

- retained exact review derivatives: 555
- blind sheet manifest: 93 sheets, six opaque IDs per sheet except the last
- sheet manifest SHA-256:
  `5d752e65b33b909acde7a8cee1866196eb24bef3389dce8e33813323a066cb1a`
- ordered blind-ID SHA-256:
  `309535aba014e7eeb061f9bbb9b44e240e57ca9b7233a46e1b9fafc6bd70d8f7`
- reference type: `agent_reference`, not human gold
- frozen ledger rows: 555 / 555 rendered inputs
- frozen ledger semantic SHA-256:
  `6365e16f7732afe0802700e18098f528c94a3e0f71264d8b0880c576a6612a10`
- frozen ledger file SHA-256:
  `d109c18033e1e78852594f56ac60b16e905d6fa3a7fa68b3cf337dca4dd71aa4`
- inspection semantic SHA-256:
  `40f5bb93350b7c3caaf5f0929331b85251b37032ba65a2ad49275a699048ea1d`
- inspection file SHA-256:
  `cd28209af0cc49b0de8b328ebd1f87bc557ce2dd3eaf7e82bffab14b5e6630b2`

The ledger was frozen before the semantic DB was opened for comparison.
Inspection made zero network, Vision, LLM, or DB-write operations and found
all 555 rendered inputs accounted for, no missing semantic result, and the
seven fixed-input failures explicitly unrendered.

Across the nine controlled fields, strict agreement was 4,587/4,995
(91.83%) and acceptable-set agreement was 4,644/4,995 (92.97%). The aggregate
90% target passed, but the required zero-clear-error scope/medium gate failed:

- scope: 545/555 acceptable, ten clear disagreements (eight false in-scope,
  two false out-of-scope);
- medium: 527/555 acceptable, 28 clear disagreements, dominated by 15
  photographs labelled as renderings;
- unique rows with a scope or medium clear error: 37.

Scope error IDs: `semv100_000029`, `semv100_000081`, `semv100_000082`,
`semv100_000172`, `semv100_000256`, `semv100_000324`, `semv100_000346`,
`semv100_000352`, `semv100_000427`, `semv100_000548`.

Medium error IDs: `semv100_000062`, `semv100_000064`, `semv100_000074`,
`semv100_000086`, `semv100_000090`, `semv100_000156`, `semv100_000157`,
`semv100_000158`, `semv100_000159`, `semv100_000169`, `semv100_000171`,
`semv100_000206`, `semv100_000237`, `semv100_000263`, `semv100_000264`,
`semv100_000265`, `semv100_000270`, `semv100_000293`, `semv100_000294`,
`semv100_000295`, `semv100_000298`, `semv100_000324`, `semv100_000327`,
`semv100_000387`, `semv100_000388`, `semv100_000416`, `semv100_000459`,
`semv100_000460`.

Uncertainty sensitivity remained poor. The agent reference marked 70/555
rows (12.61%) and 76 axis decisions as uncertain; the model marked 6/555
rows (1.08%) and ten axes. Axis-level TP/FP/FN was 3/7/73, for precision
0.3000, recall 0.0395, and F1 0.0698. The model also missed all three
reference `resolution_insufficient` rows. This is consistent with the N10
open QA and is now a measured N100 failure, even though the predeclared
near-zero heuristic used a strict less-than-one-percent cutoff.

Derived hero tiers disagreed on 89/555 rows, including 26 cases where the
model-derived tier was `preferred` or `eligible` but the agent reference was
`qa_only`, `archive_only`, or `rejected`. Stored and recomputed model-derived
hero decisions had zero derivation mismatch; the problem is semantic input
quality, not ledger corruption.

The wider gallery probe still demonstrated value. Reviewer-primary labels
found new slots beyond P2 top three in 70/100 buildings: 118 new building-slot
pairs from 267 expanded-only images (yield 0.442). Model labels found the same
70 buildings and 112 pairs (yield 0.419).

## Approval boundary

The N100 run does not authorize Full. Full is blocked because both the fixed
input completeness and semantic quality gates failed. The next version must:

1. revise the prompt/contract to distinguish photographs, renderings, isolated
   objects, and low-legibility scope more reliably and to surface uncertainty;
2. pass a new disjoint N10, then a new N100 if the contract changes materially;
3. give the seven fixed-input failures an explicit refresh/reconciliation
   policy in a new immutable version; and
4. present exact hero-only, P2, expanded, and targeted queue sizes/costs for a
   new, separate Full approval.

No Full queue, building merge, source/curated update, Neon, R2, or vector work
was run.

## Final validation and cleanup

- N100 focused tests: 42 passed
- full offline repository suite: 1,008 passed / 22 skipped in 174.59 seconds
- changed Python files: `py_compile` passed
- `git diff --check`: passed
- completed N100 resume: zero HTTP / zero Vision calls
- QA inspection: zero network / zero Vision / zero LLM / zero DB writes
- E2 final byte SHA-256:
  `4728f9f015d7fba303923df40f550c59835a7e2a5c1316eef03c499ee3ca7b19`
- E3 final byte SHA-256:
  `8512e11f8e1fd581038f790b27a67c0a8b1949067bf53b3ef30c4ea3534141a4`
- N10 final byte SHA-256:
  `30cfdce39b8ac0ecc0d1de0b52f05a5f1f5d7bec7390c03439096118e08ee31a`
- N100 final byte SHA-256:
  `e2a03e004a27ce662a93f60ad16190014789abf60f9433926ff9e38e747c8a8e`
- E2/E3/N10/N100 final WAL/SHM/journal sidecars: zero

After the frozen ledger and inspection were verified, the exact retained
review-cache directory (555 JPEGs / 115,837,554 bytes) and review-sheet
directory (93 sheets plus manifest / 51,374,325 bytes) were removed as
temporary QA material. The semantic DB, compact report, manifest, frozen
ledger, inspection, and all immutable inputs remain. Runtime/data artifacts
are ignored and are not included in Git.
