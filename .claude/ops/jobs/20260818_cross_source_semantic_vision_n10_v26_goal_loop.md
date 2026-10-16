# Cross-source semantic Vision N10 v2.6 goal loop

Status: `COMPLETE_SAFETY_FLOOR`

## Objective

Iterate fresh-disjoint semantic Vision N10 calibrations beginning with v2.6
until every strict blind-QA hard gate passes, the current weekly quota window
resets, or the safety floor stops another cycle.  N100 and full execution are
out of scope.

## Authorization and hard stops

- Current authorization covers versioned N10 iterations only.
- Each paid cycle is bounded to at most 14 reservations and 311,856
  non-double-counted tokens, with the existing prospective-token guard.
- Re-check canonical account identity and live weekly quota immediately before
  every paid launch and after each batch where telemetry is available.
- Do not start a new paid cycle when weekly remaining is at or below 30%.
- Stop immediately when weekly remaining is at or below 20%.
- Stop on QA PASS, weekly reset, or any required expansion of scope/cost.
- Never run N100 or full under this authorization.

Initial checkpoint (2026-08-18 09:37 KST): ProLite, 44% weekly remaining,
reset 2026-08-20 13:36 KST, canonical auth aligned with multi-auth active and
pinned index 1, no credit fallback.

## Frozen v2.5 baseline

- Technical run: complete, 10 buildings / 56 inputs / 56 results, 12 model
  calls, no retries, 255,095 input + 10,341 output = 265,436 tokens.
- Blind QA verdict: `FAIL`.
- Failing gates: 5 clear medium errors; 3 critical hero false positives;
  uncertainty row recall 6/13 = 0.461538.
- Passing margins that must be protected: 461/504 nine-field acceptable
  agreement; hero retention 36/37; uncertainty precision 6/12 = 0.5;
  aggregate-axis recall 6/14; predicted uncertainty 12 <= cap 19; scope errors
  0; resolution-insufficient false negatives 0.
- All v2.5 DB, report, availability, review ledgers, reconciliation, and
  exactly-once inspection artifacts remain immutable lineage evidence.

## v2.6 change policy

- Prompt-only semantic calibration.  Reuse the v2.5 output schema,
  normalizers, transformations, coverage logic, and hero derivation by object
  identity.
- Correct medium-type classification using substrate evidence, not visual
  style alone.
- Separate project legibility from local sharpness or landscape quality and
  retain explicit construction evidence.
- Add only five high-signal uncertainty boundary pairs with paired positive
  and negative guards.  Preserve primary-label freeze and existing false-
  positive guards.
- Build a new result-blind pool from the full E2/E3 inventory, excluding all
  prior selected buildings and all planned/observed URL and pixel identities
  through terminal v2.5.

## Per-cycle release ladder

1. Authenticate the complete prior identity ledger without reading prior
   semantic results or blind-QA labels during population selection.
2. Freeze and independently validate a fresh-disjoint 20-building HTTP pool.
3. Run one bounded elevated HTTP-only census; Vision and LLM requests must be
   zero.  Independently validate terminal census evidence and freeze pins.
4. Compile and run focused/full offline regressions, then exact production
   no-model preflight with semantic outputs absent.
5. Re-check account and quota; launch one fresh bounded paid N10.
6. Independently validate terminal semantic artifacts.
7. Render opaque sheets; obtain two isolated full-pop pixel-only ledgers;
   freeze both; reconcile every disagreement with a third isolated reviewer;
   freeze reconciliation.
8. Perform exactly one first-unblind inspection.  On FAIL, preserve the whole
   cohort and start a new version only if quota policy still permits it.

## Work log

- 2026-08-18: v2.5 failure taxonomy completed read-only.
- 2026-08-18: v2.6 prompt module added with frozen v2.5 schema/logic identity;
  focused tests: 16 PASS.
- 2026-08-18: v2.6 result-blind population planner started.  Prior lineage
  replay currently authenticates 30 runs, 260 selected buildings, and 2,776
  occurrence identity records; 1,299 are physical-availability records.
- 2026-08-18: two pre-release v2.6 pool files were preserved under explicit
  `superseded_pre_release*` names after audit found ambiguous prefilter-count
  wording and missing terminal-lock lineage pins.  Neither was used for HTTP.
- 2026-08-18: hardened offline v2.6 pool frozen: 20 buildings / 120
  occurrences, 4,516,580 bytes, byte SHA-256
  `58699e4db3f9ecffa2776c1a57027fabca4692ed1b6051bbae77a84a8e95c0db`,
  canonical self SHA-256
  `8f45ae5d7c34ee51e09056cdc89aacfbec26966daf1df1c6cd1808dfd2bf5ace`.
  Sampling counts now explicitly identify the successful-asset prefilter;
  AV25.1 and semantic-v2.5 persistent lock identities and failed-parent
  reserved-output absence are part of the frozen lineage.
- 2026-08-18: initial v2.6 focused + shared-sidecar regression returned 42
  PASS.  The earlier exact preflight was against a subsequently superseded
  pre-release pool, so it is retained as engineering evidence but is not the
  final production preflight; the hardened final pool still requires replay.
- 2026-08-18: the first HTTP release audit found and blocked launch before any
  network request.  Hardening added lock-before-replay freshness, exact pin
  revalidation on the base runner's second pool read, output/cache alias
  rejection, terminal sealing of fresh-only technical failures, accurate
  error-path HTTP accounting, and rollback of owned partial publication.
- 2026-08-18: final v2.6 compile/release regression passed: 42 tests in
  51.73 seconds.  The exact production no-model preflight returned
  `preflight_passed`, authenticated the final 20/120 pool and source lineage,
  created no output, and recorded zero network, Vision, and LLM requests.
- 2026-08-18 10:26 KST: the one authorized fresh HTTP-only census completed
  in 124.4 seconds as run `semav26-2fd1e6575bfb8136345881c5` with 120/120
  requests, 106 exact and 14 source-changed outcomes, and zero Vision/LLM
  requests.  It selected 10 buildings / 56 occurrences.  Cache identity is
  226 files / 36,454,209 bytes / tree SHA-256
  `f45a3f567dc34fa28204c99306cb9e5a73bd01f5811f3c2b692679d4ce053eda`.
- The terminal DB is 2,412,544 bytes with byte SHA-256
  `b885e2e7dc85e7c8220a264be8b18bd9ab3d4252ae453a065831694266302631`
  and independently replayed logical SHA-256
  `a1a0fe41ff4df5d8df75b5b8ee496a6662b17b7b5ff31409d066b5ab1e758666`.
  The 296-byte report SHA-256 is
  `a51977217d2e71acf6bdadf20904f1971fed058efb4479c7c202893de5063b8d`.
  The 4,169,282-byte final manifest byte SHA-256 is
  `e0afd3c09c9af356247d3ed7f375f1e571b7a93d2b93bb6aa7495895cc9cf876`
  and canonical self SHA-256 is
  `3b7404dea1bfa63ec39dc7940b5fb27433f0826d6a3459301063dcbb8e9e2646`.
- Independent terminal validation completed read-only in 8.2 seconds with
  `PASSED=True`, 14/14 checks and zero failures.  The validator replayed the
  SQLite schema/accounting/logical hash, all 120 response fingerprints, all
  106 derivatives, the exact 226-file cache, the 10/56 selection, report, and
  complete final-manifest projection.  Its focused regression passed 9 tests
  in 113.69 seconds.  The terminal invocation used the already independently
  authenticated frozen-pool contract via `--skip-source-replay`; the full
  pool source replay had separately passed before the HTTP launch.
- 2026-08-18 10:39:32 KST: the final root-thread ProLite telemetry reported
  weekly used 83%, remaining 17%, reset 2026-08-20 13:36 KST, and no credit
  fallback.  This is
  at/below the explicit 20% hard stop.  No v2.6 semantic/model run was
  launched and all `semantic_vision_n10_v2_6` output paths remain absent.
  The fresh 10/56 census selection therefore remains unexposed to the v2.6
  semantic prompt/model and is preserved for the next quota window.

## Current next action

Stop this goal loop under the authorized quota-safety terminal condition.  Do
not launch v2.6 semantic inference, another N10 cycle, N100, or full in the
current weekly window.  After the quota reset, begin a new explicitly approved
goal from the frozen v2.6 terminal census, re-authenticate all pins, finish the
semantic offline release surfaces and no-model preflight, re-check the active
account/quota, and only then consider the bounded v2.6 paid N10.
