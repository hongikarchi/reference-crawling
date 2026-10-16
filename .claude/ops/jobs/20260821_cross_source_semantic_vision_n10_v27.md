# Cross-source semantic Vision N10 v2.7

Status: `IN_PROGRESS`

## Scope

- Prompt-only calibration from frozen v2.6 FAIL evidence.
- Fresh-disjoint N10 population; no reuse or overwrite of v2.6 artifacts.
- HTTP availability census has zero LLM/Vision requests.
- Semantic envelope: 56-60 images, 12 first-attempt batches, at most 2
  strict-invalid selective retries, 14 reservations, and 311,856
  non-double-counted tokens maximum.
- N100/full are excluded.

## Token policy

- Root owns integration and all production commands.
- At most one no-history implementation agent; no repeated quota/status agents.
- Long command output is suppressed and summarized from durable files.
- Pixel QA alone may use isolated no-history reviewers.
- No paid launch when weekly remaining is at or below 20%; stop at or below 15%.

## v2.6 failure targets

- Preserve the passed 461/504 = 91.4683% nine-field agreement margin.
- Recover three false scope rejections of readable architectural interiors that
  also contain exhibits.
- Remove two photograph-to-rendering errors.
- Remove the one critical hero false positive and raise usable retention from
  32/37 = 86.4865% to at least 90%.
- Raise uncertainty row recall from 3/17 = 17.6471% to at least 50% while
  retaining precision at or above 50% and staying within the overflow cap.

## Release order

1. Freeze v2.7 prompt and offline invariants.
2. Freeze and independently validate a fresh-disjoint availability pool.
3. Run one fresh HTTP-only census and independent terminal validation.
4. Freeze semantic runner/validator/renderer pins; compile, regress, preflight.
5. Recheck account/quota; run exactly one bounded semantic N10.
6. Independently validate, render opaque sheets, freeze two blind ledgers,
   reconcile, and perform exactly one first-unblind inspection.

## Frozen prompt checkpoint

- `canonical/cross_source_semantic_vision_v27.py` is a prompt-only v2.6
  calibration. Schema, vocabularies, normalizers, transforms, and hero
  derivation remain object-identical to v2.6/v1.0.
- Prompt template: 16,791 characters; lexical proxy 2,914 versus v2.6 3,443
  (15.4% lower); SHA-256
  `d00121c1abf3eb73aba280cbbc073bb2ad1fb009f88b15f8936fd24250ecd8e6`.
- Prompt module: 14,769 bytes; SHA-256
  `230c41fb4245d17940766957e2a50134c8115e875812375d749563d786ae849a`.
- Focused prompt tests: 13 passed; network/Vision/LLM 0.

## AV27 terminal insufficient-pool evidence

- Live HTTP-only census completed at 2026-08-21 08:37 KST as run
  `semav27-9b94d790078ab5f4313f1806`.
- Frozen pool: 20 buildings / 119 occurrences; 119 reservations; 100 exact
  matches and 19 source-changed results; Vision/LLM 0/0.
- Terminal status is `insufficient_exact_pool`; no semantic selection manifest
  was published. The DB, report, cache, and persistent lock are immutable
  failed-parent evidence.
- Exact coverage by pool-building rank is
  `4,6,4,5,5,3,4,4,4,6,6,4,6,6,6,5,4,6,6,6`.
- The only quota deficit is population-core / Architizer / ordinary: four
  qualifying buildings versus the required five. Every other quota is full.
- A second full 20-building disjoint pool is impossible because only one
  unused Architizer gallery-fallback building remains, below its pool quota of
  two. Do not weaken the frozen five-exact minimum or rebalance strata after
  observing results.

## AV27.1 bounded supplement decision

- Preserve and authenticate the entire AV27 failed-parent ledger.
- Freeze exactly one unused Architizer ordinary building,
  `architizer:building:atz_bld_5148eb979c1a69e211539d22`, with six planned
  occurrences. Exclude every v2.6/v2.7 building, asset, URL, response,
  expected/actual pixel, and derivative identity.
- Run at most the six planned HTTP-only occurrences. If at least five remain
  exact, combine this child with the already-frozen parent winners and publish
  the final 10-building selection. Otherwise preserve a terminal insufficient
  child and stop.
- Keep a split evidence contract: the AV27 SQLite/cache remain the immutable
  119-occurrence parent ledger, AV27.1 SQLite/cache contain only the six child
  occurrences, and the combined final manifest binds and replays both. Do not
  fabricate parent HTTP rows inside the child DB.
- This replaces a wasteful/impossible 119-request rerun with six HTTP calls;
  network evidence only, Vision/LLM 0.

## Semantic structural checkpoint

- The v2.7 sidecar, independent validator, runner, validator CLI, renderer,
  inspector, and reconciler namespaces compile with schema version 20, the
  v2.7 prompt chain, opaque `semv27` identities, and v2.6 terminal/blind-QA
  lineage requirements.
- All production runner/validator/renderer pins remain deliberately `None`
  until AV27.1 publishes a terminal selection; import-time stale blocks cannot
  activate production mode.
- Focused offline semantic structural suite: 71 passed in 45.02 seconds;
  network/Vision/LLM 0.

## AV27.1 zero-call live-path rejection

- The first elevated AV27.1 command on 2026-08-21 exited in 1.3 seconds before
  any request with `pool manifest contract mismatch`; HTTP/Vision/LLM were
  0/0/0 and no DB, report, cache, or final manifest was created. The persistent
  one-byte advisory lock is expected and must remain.
- Cause: source preflight used the v2.7.1 1-building/6-occurrence verifier, but
  the inherited live loader and row parser still enforced the old
  20-building/100--120-occurrence constants. This is an offline release-test
  coverage gap, not a source availability failure.
- Live remains blocked until a local 1/6 loader/parser and a synthetic test
  entering the inherited live initialization/finalization path pass.

## AV27.1 parser repair release gate

- The v2.7.1 contract now replaces and restores both inherited hard-coded
  20-building loader/parser functions with canonical 1-building/6-occurrence
  implementations.
- Focused suite: 6 passed, including an end-to-end fake-exact child run with
  six durable synthetic reservations, child terminal completion, and combined
  10-building/50--60-occurrence manifest/report publication; real network 0.
- `py_compile`, production source replay, and fresh production preflight all
  pass with network/Vision/LLM 0.
- The remaining one-byte `.db.lock` is the intentional persistent advisory
  lock inode. Preflight acquired and released it successfully; do not delete
  it or treat mere existence as an active writer.

## AV27.1 live terminal checkpoint

- The repaired fresh command completed at 2026-08-21 08:59 KST as child run
  `semav271-95b96c8af91b016822f8d9d2`.
- Exactly six HTTP reservations completed: five exact and one source-changed;
  Vision/LLM 0/0. Child selection is 1 building / 5 occurrences; the combined
  final manifest is 10 buildings / 57 occurrences.
- Child DB: 241,664 bytes, byte SHA-256
  `23c40270788bd72813519ac1c661f540bea9c08d1e350c1afb0d1f0f5db9c07c`,
  logical SHA-256
  `a11409bce32b0042b9336fe409bb3a9d256d2b283f445294781af0525f0257ed`.
- Child report: 340 bytes, SHA-256
  `acf7020dd51ff2d11b549c0c3ef3d7aaf25fef592fe9b301962520b88a69c640`.
- Combined final manifest: 4,610,453 bytes, byte SHA-256
  `c86d5cd1aeabe38e320dd8a8e1c5c3af4c559aed78a7b75768b4c5cd790e49e2`,
  self SHA-256
  `6ffe4a174cf722d23e3c1c0a7509479a410a719f0856239a3bd8b24e07839e00`.
- Child cache: 11 files / 1,581,659 bytes / tree SHA-256
  `327ec2a306383b66cf3a442d9310e1f1b078edad1dcbeb68079af542a1d8630a`.
- Standalone production terminal validation has not yet been invoked; exact
  child pins and split-ledger replay must be frozen first.

## AV27.1 terminal-validator release correction

- The first production terminal-validator invocation returned 13/14 PASS.
  The sole failure was `v271_combined_report_projection`; every DB, cache,
  manifest, logical, parent/child pin, and read-only-stability check passed.
- Root cause was a validator-only literal expecting
  `Vision/LLM requests: 0` while the frozen producer report correctly records
  `Vision/LLM requests: 0 / 0`. No production artifact changed.
- Corrected validator release: 127,307 bytes, SHA-256
  `b34b3838fb7cf508fe5819dc2f511aaf7ece0ff294e78e6c0173855436a3f9f8`.
  Compile plus the six focused synthetic/tamper tests pass. Treat the earlier
  invocation as evidence for the superseded validator release; invoke this
  corrected release once.
- Corrected production invocation: exit 0, 14/14 checks PASS, failed checks
  empty. AV27.1 split-ledger availability is terminal and frozen.

## AV27.2 no-network normalization decision

- The semantic v2.7 runtime's existing `AvailabilityContract` intentionally
  accepts one complete standard census ledger. Directly teaching the 255 KB
  writer/validator/reviewer surface about an insufficient parent plus a
  supplement would require a cross-cutting schema/provenance migration.
- Instead, create a local replay-only AV27.2 ledger from the 57 already-selected
  exact parent/child occurrences: standard AV27/schema2 tables, 57 replayed
  reservations/completions/attempts/results, 10 final buildings, 57 final
  occurrences, and 114 selected response/derivative snapshots. Every row and
  byte remains bound to AV27/AV27.1 provenance.
- This normalization performs network/Vision/LLM 0 and lets semantic v2.7 keep
  its already-tested single-ledger code path. AV27 and AV27.1 remain immutable.

## AV27.2 terminal checkpoint

- Offline normalization run `semav272-38af387431ae740b040da282` is complete:
  standard AV27/schema2, 20 technical pool buildings, 57 pool occurrences,
  57 replayed reservations/completions/attempts/exact results, 10 final
  buildings / 57 final occurrences, and seven validations. Actual external
  network/Vision/LLM operations were 0/0/0.
- Pool manifest: 24,880 bytes; byte SHA-256
  `d8f46002bf5117a6ef5abaf2d727dd424356d70cf1c187362617d819c133f2f3`;
  self SHA-256
  `4a3920d43475f7a09211a21167e167bd3854bea9f4d5f00c469637593d7c09d6`.
- DB: 1,212,416 bytes; byte SHA-256
  `86e2ca92d38d4ca0198f42d36275a7fd5d9ec45847430561094a5bf2e56052f3`;
  logical SHA-256
  `a55933d5569cc22971e2504a3d183a8c2e2ec96edd30e5dc03e1ceccce2e1e4f`.
- Report: 337 bytes; SHA-256
  `ec85cf46d0d24f85db11eeca822457b2aae5ffab5f097697e0bf64a18ca24690`.
- Final manifest: 4,610,437 bytes; byte SHA-256
  `07995a0e4554a3e93f1db6f4872e26154fafd42c248c1cf54ba3660ff0d9910a`;
  self SHA-256
  `90f0f2087589320a3dc894bf38298c22e91df32ea46bd4f365844ec71223e454`.
- Cache: 114 files / 19,709,442 bytes / tree SHA-256
  `02c137c95eef8a5ead616dc5ebd555861769ef70197cfb31e7b1d0b4fd87b606`.
- Existing semantic v2.7 availability verifier smoke PASS unchanged. AV27.2
  terminal validator passed all seven checks after pins were frozen; compile
  and focused synthetic/tamper regression passed. Production finalizer and
  terminal validator were not rerun after their successful invocations.

## Resume checkpoint (2026-08-21)

- After an interrupted session, filesystem audit confirmed AV27 and AV27.1
  terminal DB/report/cache/final artifacts are present at their frozen sizes.
- AV27.2 DB/final artifacts and semantic v2.7 DB/report are absent; therefore
  no partial normalization or paid semantic run requires recovery.
- AV27.2 implementation resumed with one isolated no-history implementation
  worker. No duplicate audit/quota workers are running.

## Semantic v2.7 release and paid terminal checkpoint

- AV27.2 pins were installed identically across runner, independent validator,
  and renderer preterminal contracts. `py_compile` and 72 focused tests passed.
- Exact no-model production preflight passed in 44.1 seconds using explicit
  `codex-cli 0.147.0`: AV27.2 whole replay true, network/Vision 0/0, no semantic
  output created. Renderer remains fail-closed until all ten terminal pins exist.
- Immediately before launch, canonical auth exactly matched multi-auth active,
  pinned, and all family indexes at account index 0; plan Pro; weekly used 11%
  / remaining 89%; credits absent. All semantic output paths were absent.
- Fresh paid run `semn10v27-f0ba999ff4f3a6808647ecfe` completed in 399.8
  seconds: 10 buildings / 57 occurrences, 57 successes, 12 model reservations
  and attempts, zero retry, semantic network 0.
- Observed input/cached-subset/output tokens: 261,048 / 44,032 / 10,784.
  Non-double-counted total 271,832, within the 311,856 hard cap by 40,024;
  cached input is a subset and is not added again.
- Semantic DB: 1,347,584 bytes; byte SHA-256
  `f90686bfd87c399399134fef81205c86b20e332c1954c474909fb96cc3f9b1b3`;
  logical SHA-256
  `728a9f1d923d3c8d855980f809630a28171ee0581385566b988256fc6a203a74`.
- Report: 703 bytes; SHA-256
  `48e429b828611301faf23fd13ccca326e89ddbd379897b2927782cbe30df6cbd`.
- Review cache: 57 files / 11,464,050 bytes / tree SHA-256
  `611a5ae90967956155eaf1deba0e4b8d0a9104a9ef771acd0e9624840c43772c`.
- Standalone production semantic validator and opaque blind QA have not yet run;
  terminal pins must be installed and tested first.
- Terminal release wiring: renderer's ten terminal fields installed atomically;
  compile and 73 focused tests PASS. Standalone validator then completed in
  47.5 seconds with exit 0 and 46/46 checks PASS, failed checks empty. It made
  no network/Vision/LLM calls. Opaque blind QA may proceed.
- First renderer release failed before output on a single stale renderer-only
  v2.5 lineage hash (`ea34...` expected versus frozen DB `bcda...`); no sheet
  directory was created. The pin was corrected, compile plus 46 renderer/review
  tests passed, and the corrected renderer produced one manifest plus ten opaque
  sheets for all 57 inputs. No semantic/model/network call was made.
- Two distinct blank drafts were created and handed to isolated no-history
  pixel-only reviewers A/B in parallel. Each reviewer is restricted to the ten
  sheets, manifest, and its own draft; peer/semantic/model/source metadata are
  forbidden until both official ledgers freeze.
- Reviewers A and B each viewed all ten sheets, completed 57/57 rows, and
  produced their official frozen ledgers with exit 0. Neither reviewer read
  the peer ledger or any semantic/model/source metadata before freezing.
- The reconciliation template authenticated both ledgers and found 36
  disagreement rows / 107 unresolved dimensions. A third isolated pixel-only
  reconciler received only the sheets, manifest, two official ledgers, and the
  reconciliation draft; semantic/model data remain hidden.
- The reconciler inspected the relevant original-detail sheets, resolved all
  107 dimensions across the 36 disagreement rows, and froze with unresolved=0.
  First-unblind inspection then ran exactly once with immutable SQLite input
  and operations DB-write/network/Vision/LLM = 0/0/0/0.
- Blind artifact identities:
  - review manifest 37,507 bytes, byte SHA-256
    `ce6f48129cae1394bb3504b3bbb162f5f58119cf8ce309312f6ecf168d2f7750`,
    self SHA-256
    `15bf0c35c8a77ed2b9a6dc20987970f6f8ddf4dc6b14f0e9a513dbc1f7e2f3ec`;
  - reviewer A 46,303 bytes, byte SHA-256
    `23af40d89e4b3f06b5d3e0ae4c28c7d79cd180d703d11c5b19bc4b6ec364b774`,
    ledger SHA-256
    `77ada78e41fc6b53943b2b3dd78a7aa0ea36e8b5e79c601f59ecb089a6de8827`;
  - reviewer B 46,052 bytes, byte SHA-256
    `1e7c900037568b25b8430fecec1380c8a22125fb0ed79c8050c0577bcce7e012`,
    ledger SHA-256
    `716017e4c74fd769f72d8a9a5e8890085a16566361b2cc78c0d986a67fb2dfdc`;
  - reconciled ledger 77,241 bytes, byte SHA-256
    `28d98e25c314d558a93ceb4f11f065fbd38814077996eb667122617226ac2a4c`,
    ledger SHA-256
    `5e42366ee84ba904b5332c178e58a8ffb53b4884fe02ee68bba2dcf8d521d109`.
- Final inspection is 19,593 bytes, byte SHA-256
  `1d548a810bf29b99842e41beb457392f926d4ec509c7fa901f9db57e3134c49e`,
  self SHA-256
  `155693039a9ab19533f89ad88f87304963e814da487d1de83f98bbf4bd76b9b9`.
- Final quality verdict: **FAIL**. Technical/population gates passed: exact
  10 buildings / 57 occurrences, 57/57 results, scope clear errors 0, medium
  clear errors 0, overall nine-field acceptable agreement 497/513 = 96.8811%,
  usable-hero retention 42/42 = 100%, and predicted uncertainty 7 within cap
  34. There were no hero derivation mismatches.
- The two failing quality gates are:
  - critical hero false positives: 8 (required 0), rows
    `semv27_000011`, `semv27_000025`, `semv27_000034`, `semv27_000040`,
    `semv27_000046`, `semv27_000050`, `semv27_000056`, `semv27_000057`;
  - uncertainty quality: row TP/FP/FN/TN = 4/3/24/26, precision 0.571429,
    recall 0.142857; aggregate-axis TP/FP/FN/TN = 4/3/32/417,
    precision 0.571429, recall 0.111111. Resolution-insufficient FN is 0.
- N100/full remain prohibited. Preserve v2.7 artifacts as immutable evidence;
  any continuation must be a new prompt/runtime namespace and fresh N10.
