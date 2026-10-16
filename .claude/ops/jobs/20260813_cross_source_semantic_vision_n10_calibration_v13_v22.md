# Cross-source semantic Vision N10 calibration v1.3+

## Status

`IN_PROGRESS; N10_ONLY; N100_AND_FULL_NOT_RUN`

This job continues the fixed-gate, fresh-disjoint semantic Vision N10
calibration after v1.2. Each semantic attempt uses a newly frozen selection
that excludes all prior physical and semantic identities. Failed and
successful artifacts are immutable audit records; no artifact is repaired or
overwritten in place. E1/E2/E3, canonical production data, Neon, R2, source
metadata, vocabularies, the output schema, deterministic hero/coverage
derivations, image transform, model, reasoning effort, service tier, and image
detail remain fixed.

The operator authorized continued N10 attempts until every predeclared hard
gate passes. This does not authorize N100 or Full.

## Fixed release gates

- exact pinned population and 100% result coverage;
- zero clear scope errors and zero clear medium errors;
- at least 90% acceptable agreement across the nine semantic fields;
- zero critical hero false positives and at least 90% usable-hero retention;
- when reference uncertainty support is at least five rows: row precision and
  recall at least 0.5, aggregate-axis recall at least 0.4, and zero
  resolution-insufficient false negatives;
- predicted uncertainty no greater than the frozen overflow cap;
- two independent full-population blind pixel ledgers, frozen before peer or
  model-result exposure, then pixel-only reconciliation with all 20 material
  dimensions audited and zero unresolved disagreements;
- read-only first-unblind inspection, immutable inputs/DB, and zero semantic
  HTTP requests.

## Iteration ledger

| Version | Population | Vision usage | Technical result | First-unblind quality result |
|---|---:|---:|---|---|
| v1.3 | 10 / 60 | 4 reservations, 15 results, 58,185 tokens | aborted after two source-drift discoveries; partial preserved | not eligible |
| v1.4 | 10 / 55 | 0 calls | pre-Vision exactness gate rejected 3 changed sources | not eligible |
| v1.5 | 10 / 59 | 0 calls | pre-Vision exactness gate rejected 2 changed sources | not eligible |
| v1.6 | 10 / 58 | 0 calls | rank-alias preflight bug; failed audit preserved | not eligible |
| v1.6.1 | 10 / 58 | 0 calls | false exact-1024 predicate rejected one valid 639x720 derivative | not eligible |
| v1.6.2 | 10 / 58 | 13 calls, 246,742 tokens | PASS, selective invalid-only retry | FAIL: aggregate 87.36%, scope 5, medium 5, hero FP 5, weak uncertainty |
| v1.7 | 10 / 59 | 12 calls, 232,783 tokens | PASS | FAIL: hero FP 1, retention 82.05%, axis recall 0.346 |
| v1.8 | 10 / 59 | 12 calls, 230,215 tokens | PASS | FAIL: scope 2, medium 1, hero FP 5, uncertainty collapsed to 0 |
| v1.9 | 10 / 60 | 12 calls, 238,144 tokens | PASS | FAIL: scope 1, medium 1, hero FP 3, row recall 0.0556 / axis recall 0.0357 |
| v2.0 | 10 / 59 | 13 calls, 257,097 tokens | PASS, two-row selective retry | FAIL: scope 0 but medium 3, hero FP 2, retention 74.47%; uncertainty overcalled 34 rows |
| v2.1 | 10 / 56 | 12 calls, 239,776 tokens | PASS, no retry/error/HTTP | FAIL: medium 0, aggregate 96.83%, retention 100%; scope 1, hero FP 9, uncertainty collapsed to 0 |
| v2.2 | 10 / 59 | 11 calls, 231,796 tokens | controlled pause after 55/59 at output-token observation | not eligible; continuation pending |

Cached-input tokens in the runtime ledgers are subsets of input tokens and are
not added a second time to the totals above.

## Availability and runtime hardening

The v1.6 availability census introduced a full-byte, bounded HTTP preflight
and immutable local derivative cache so later semantic runs perform zero
source HTTP requests. It also exposed and fixed, in new namespaces only, a
census-rank alias defect and a no-upscale derivative-dimension defect.

From v1.6.2 onward every successful semantic runtime binds the frozen
pool/census/final manifest, all selected occurrences and evidence rows,
E2/E3 identities, deterministic derivative bytes and pixels, review-cache
ledger, raw Codex JSONL, usage, normalized records, retry membership, and
derived link/hero/coverage rows. Model reservations are committed before each
call; accepted records are immutable; only strict-invalid IDs may be retried.
Independent terminal validators replay the same contract before review-sheet
rendering.

## Latest immutable v2.1 evidence

Availability final manifest:
`data/reports/cross_source_semantic_coverage_n10_v2_1.json`

- 10 buildings / 56 occurrences;
- byte SHA-256 `eef5de59796efc48a2d84ba8c226672c8bca8db246827d1743308d8b7907b276`;
- self SHA-256 `9e134f495769da03ff0e7f02d4db5c3cba4b8f99b5ad516c8da8a80949dda867`;
- availability census: 117 HTTP requests, 113 exact, 4 source-changed,
  zero Vision/LLM requests; independent validation 10/10 PASS.

Semantic runtime:
`data/enrichment/divisare_architizer_semantic_vision_n10_v2_1.db`

- run `semn10v21-adbac21b7e650e1a706935c9`;
- 56/56 results; 12/12 successful calls; no retries/errors; semantic HTTP 0;
- input 231,699, cached-input subset 77,056, output 8,077,
  non-double-counted total 239,776;
- DB byte SHA-256 `e4670d563b92726610767ae6e854fed2c54fa8c13794c5ae2d61302383dcd8e9`;
- logical SHA-256 `7cde96d7cafa091481d831851c52bd55c36546960e429790d70ee63bfdbc632e`;
- independent terminal replay PASS; SQLite quick/integrity PASS, FK 0,
  WAL/SHM/journal absent.

Blind reference:

- reviewer A byte/ledger SHA-256:
  `095e46dad8f0a132b6de1146760d514410e0f6e13ed42a47ce936d07fe154d1d` /
  `d257e3d3ebea0d3061f601c15457a716e90806ca9bc9ded404e4b96514ad0ff5`;
- reviewer B byte/ledger SHA-256:
  `303048c6ef668f58f3cfe36376f954a89fb3387502318aac06b117aa3c6c1dbc` /
  `0d6b97852c0ba3c6e7575efc673cf3dbe843d215f031888a77919d6f812d1677`;
- reconciled byte/ledger SHA-256:
  `109d3bfeaf59bf4b0bf7fef9c22df82b03ad63e80d5eb99d183ce74e6229f04d` /
  `01633268bd51de0033c78d6c796cf3443b71b028fbd5b6e27431e5553fe716ba`;
- 21 disagreement rows / 79 dimensions / 0 unresolved; completed before
  unblinding.

First-unblind inspection:
`data/reports/divisare_architizer_semantic_vision_n10_v2_1_inspection.json`

- byte/self SHA-256:
  `3986812e933d37bab1bb4c062b0b28b5b33e9b3d975044b84118833209137ea6` /
  `a6c1df8d69c35c31a47e08aa8dfe302ba01fc6040e68a620636d4fd148d45cff`;
- aggregate nine-field acceptable agreement 488/504 = 96.8254%;
- medium clear errors 0; usable retention 37/37 = 100%;
- scope clear errors 1; critical hero false positives 9;
- predicted uncertainty 0 versus 24 reference rows / 32 axes, recall 0;
- final verdict `FAIL`.

## Recovery note

During v2.1 availability development an untracked v2.0 planner source was
accidentally overwritten. No data artifact changed. The file was reconstructed
with `apply_patch` and compared with the surviving original bytecode:
public constants and the complete lineage tuple matched, both implementations
regenerated the frozen v2.0 pool byte-for-byte, and the v2.0 production
validator and tests passed. The recovered source is 53,021 bytes, SHA-256
`423c92cc523a58285244c3c0c746e400e6085ffa401298239c66099e4bd8477e`;
formatting differs from the former source but observed behavior and frozen
artifacts are identical.

## Completion checklist

- [ ] A fresh v2.2+ N10 passes every fixed hard gate.
- [ ] Final independent runtime and review validators pass.
- [ ] Full repository regression, `py_compile`, and `git diff --check` pass.
- [ ] Exact code/test/job-card staging boundary is reviewed and committed.
- [ ] N100/Full remain unrun pending a separate user decision.

## v2.2 pre-run freeze

Prompt v2.2 removes the v2.1 contradiction between a frozen primary and an
acceptable adjacent label. Every applicable uncertainty axis must be audited
as `N/A`, `CLEAR`, or `BOUNDARY`; a `BOUNDARY` requires positive visible cues
for both values but does not require a tie or an ambiguous primary. An
independent red-team review caught and corrected an over-broad multi-panel
reject rule before any paid call: coherent same-project plan, section,
elevation, or diagram sheets stay in scope, while unrelated editorial/gallery
collections without a dominant or unifying project may be rejected.

Final prompt identity:

- template SHA-256
  `988d8d82212f203aade9a2b349ad68cce9d1117573e5a545da568fe20370862a`;
- module SHA-256
  `19d4e328933bdcd072042a5e9eea057732aec77e5acfd5a9b1142c302d0ed072`;
- 11,983 UTF-8 bytes / lexical proxy 2,143;
- the complete evidence sentence, including its uncertainty suffix, must fit
  the unchanged 500-character schema limit;
- output schema, vocabulary, transform, normalization, model interface,
  deterministic hero, and coverage derivations retain object identity.

Fresh-disjoint v2.2 availability:

- pool: 20 buildings / 119 occurrences; byte/self SHA-256
  `fdaaabf532b58e2f5128c0442b5e216ef3a8e382d8f1f7b7fc3e49533d089b55` /
  `d2841e560deca12bc236f43086aa6015e78389f0279296f7148a8e5e1a48508e`;
- census run `semav22-eeee9af895ff4f47f6e3b432`: 119 HTTP requests,
  115 exact, 4 source-changed, zero Vision/LLM;
- census DB byte/logical SHA-256
  `f319592c5a6bf0ff04a8bf6a09b856c3d8dd3af130d7c8a7f0b700a27fcf0330` /
  `23b83b97891ff31ce6b16511095c8b257baf7a0fa341df5ab12f341eb30869c5`;
- final manifest: 10 buildings / 59 occurrences; byte/self SHA-256
  `da45dbb5ef4b450cb9c3bcb1703c3a7949ece39e673a06bfd8935c1515aa651a` /
  `94837e6e71d29246d06b4ef6c1719a0dbc71a3aec03f3ad100d9903d3cabab5f`;
- selected occurrences are 59/59 exact and eligible, unique by URL, E1, and
  derivative, with zero overlap against all prior buildings, URLs, planned
  pixels, or materialized derivatives; model-result conditioning is false;
- independent source/pool/evidence/cache/final binding validator 10/10 PASS.

The v2.2 paid run passed every technical gate through batch 11. All 11 calls
succeeded, yielding immutable accepted results for 55/59 inputs with no retry,
model error, or semantic HTTP request. After batch 11, observed usage was input
219,548 (cached subset 55,040), output 12,248, and non-double-counted total
231,796. The pre-call guard then stopped before reserving batch 12 because the
frozen output threshold was 12,000, although the 312,000 total threshold still
had 80,204 tokens of headroom. This is a controlled safety pause, not a model
failure.

The parent partial remains untouched:
`data/enrichment/divisare_architizer_semantic_vision_n10_v2_2.db.partial`

- 1,286,144 bytes; SHA-256
  `21d72c72689a187f773c3e29b3b9d40d5a5f03a75aa2568feba5554fa6020c4b`;
- SQLite quick/integrity PASS, FK 0, no WAL/SHM/journal;
- 11 reservations/completions/successful attempts, 55 accepted results, and
  exactly `semv22_000056` through `semv22_000059` still ready;
- current `--resume` is prohibited: it cannot cross the same immutable output
  threshold and would only append a duplicate pause event, changing the parent
  hash without making a model call.

The authorized continuation must therefore use a separately versioned,
exclusive v2.2.1 child artifact that authenticates this parent byte-for-byte,
never recalls or overwrites the 55 accepted rows, targets only the final four
IDs, permits at most one strict-invalid selective retry, keeps the cumulative
312,000-token total ceiling, and independently validates the combined 59-row
result and all parent/import lineage.

The v2.2.1 continuation completed successfully:

- child run `semn10v221-da4b96d0a85ee85c9ca499ac`;
- one child call, no retry/error/HTTP; exactly IDs 56–59 targeted and accepted;
- child usage input 19,341 / cached subset 0 / output 921 / total 20,262;
- cumulative v2.2 + v2.2.1: 12 calls, input 238,889, cached-input subset
  55,040, output 13,169, non-double-counted total 252,058;
- combined results/links/heroes 59/59/59; terminal status `complete`;
- child DB `data/enrichment/divisare_architizer_semantic_vision_n10_v2_2_1.db`,
  421,888 bytes, SHA-256
  `1715ad58bdb63165bdc8dbf227d0628ef73d30fe101d307a1abe82cabcea088f`;
- combined logical SHA-256
  `9e23253a70bff742bb6411455b1a81e7b7eedc09a63128134b1e95c20cf89528`;
- report 4,419 bytes, SHA-256
  `15f9d625fd3be9ead1591566b736db7aecba6a4fc4a4881611038d1fa89f5ee0`;
- child cache 4 files / 444,933 bytes / tree SHA-256
  `0fb15df1d6963b90c7f04c9d03d08e0848f799f39f71792d993e67ed9aa8d353`;
- independent continuation validator 10/10 PASS; all partial aliases absent;
- parent partial remains byte-identical at the pinned SHA-256 above.

## v2.2.1 strict-blind QA and first unblind

The original reviewer attempts were discarded because their access logs could
not satisfy the strict prompt-unread contract.  A second review package was
therefore made from the same opaque 59-input/10-sheet pixel manifest, and two
fresh isolated read-only reviewers were given only their own blank draft and
the ten sheets.  Neither reviewer received the semantic DB/results, the other
ledger, source metadata, URLs/names, prior analyses, or the v2.2 prompt.

- review manifest byte/self SHA-256:
  `a7887b00edb36d5d009ae63ad2ee84dba10e55434b412bb3b4ef714fafdbda0f` /
  `8ee007f55fcfff944314e62586a926fe0199b171070af76025f58849dd7b2135`;
- reviewer A2 byte/ledger SHA-256:
  `c570f6b204f769fa833defdd2b1b81031d1822dc166eabd7916fe44948ef2b54` /
  `5e8ab9f1532427962a1b18954554a3e857f83095edd4aca3b63011f6fc55c46a`;
- reviewer B2 byte/ledger SHA-256:
  `2de19ff50cf20d570a1aa24e4e9c4b0049c83a6c8bd0d4c1a67faa6fcaa34361` /
  `cbcf3ca189b0f5f34d7e286881c01d1e97c023217596b43834216d1a213f1706`;
- both official frozen-ledger validators PASS with exact 59-row population,
  vocabulary/invariants, manifest binding, and exposure flags;
- a third isolated pixel-only reconciler reviewed the same ten sheets and
  resolved 20 disagreement rows / 67 material dimensions, unresolved 0;
- reconciled byte/ledger SHA-256:
  `c3f96bbfcc37b014009a047fbbe7552f5d7e6cbc89e79976c88f988f8fca3843` /
  `97d656f6ed0150cfd6327d91780e90978f474143200facc17160288e77f7f84d`;
- reconciliation completed before unblinding and authenticated both original
  independent ledgers byte-for-byte.

The v2.2.1 first-unblind inspection was executed exactly once and preserved at
`data/reports/divisare_architizer_semantic_vision_n10_v2_2_1_inspection.json`:

- size 21,049 bytes; byte/self SHA-256
  `c7d161809e89ab9c5576a57c8f9035eee47b0dc449ce39e3a0f392e08410d227` /
  `753781d32d54a2dcd4b88aaf0fea4c3518ed35adeeaf128db4704f3f60083d4c`;
- all integrity/pregates PASS: 10 buildings / 59 occurrences / 59 results /
  59 references, missing 0, two ledgers reconciled, SQLite quick/integrity OK,
  FK 0, DB unchanged, sidecars absent, inspection network/LLM/Vision/writes 0;
- PASS: exact population, result coverage, scope clear errors 0, and aggregate
  nine-field acceptable agreement 497/531 = 93.5970%;
- FAIL: medium clear errors 1, critical hero false positives 1, usable hero
  retention 27/49 = 55.1020%, uncertainty overflow/quality;
- uncertainty predicted 46 rows versus 6 reference rows: TP 6 / FP 40 / FN 0,
  precision 0.130435 and recall 1.0; axis TP 6 / FP 67 / FN 2, precision
  0.082192 and recall 0.75.  The dominant overcalls were framing-scale (25 FP)
  and project-legibility (23 FP), while primary framing and legibility still
  had 14 and 13 clear errors respectively;
- terminal verdict `FAIL`; v2.3 fresh-disjoint calibration is required.

## v2.3 prompt and fresh availability freeze

Prompt v2.3 keeps every model-facing schema/vocabulary/normalizer/transform and
deterministic hero/coverage object frozen.  It replaces v2.2's permissive
boundary interpretation with a purpose-based primary pass and a selective
three-condition boundary audit: both full definitions need direct pixels, both
readings must organize the central subject, and a second strict pixel-only
reviewer must be able to choose the adjacent value without relaxing either
definition.  It also makes framing documentary-purpose based, legibility
project-scale based, distinguishes orthographic rendering from line-dominant
drawing, and prevents deliberately retained material fabric from implying an
unfinished/ruin state.

- prompt template SHA-256
  `327a4be89c40fa23801e9340dfce89876dd86c264aa7be031beec079beea1deb`;
- module byte SHA-256
  `714a3c5dcaff403abdffa6a54ef607932f4a94c2836dfc7dbab5a3008c614280`;
- 13,972 UTF-8 bytes/chars, lexical proxy 2,452;
- prompt-only tests 18/18 and v2.2+v2.3 prompt regression 34/34 PASS;
- independent static red-team verdict GO, blocker 0.  Remaining measured risk
  is a possible uncertainty undercall or uncommon close-detail demotion, which
  only a fresh blind N10 can decide.

Fresh-disjoint AV23 was selected without model-result conditioning.  The
lineage authenticates AV22's 119 physical rows and the v2.2.1 combined semantic
population exactly once as parent 55 + continuation 4, together with both
caches and the strict2 blind-QA package, reconciled reference, and first-unblind
inspection.  Selector queries do not read raw/normalized semantic labels or QA
decisions.

- pool 20 buildings / 116 occurrences; size 3,721,951; byte/self SHA-256
  `78f5f72f07fa594602bbc8597222fa81f3fe536bd9e5a35adcf1a4b46ad48741` /
  `830a507715339934c54ba6f06cdb1e9fb8f42ea1704bb44617e8e46a3ac9a8de`;
- pool prior-identity ledger SHA-256
  `8aab815d1f9bc3a6fe0c336b166b7fe1cbf7886363568552d95987a25c18a285`;
- census run `semav23-0428609cbff97d27ffb63c1d`: 116 HTTP requests,
  115 exact / one source-changed, no retry/error, Vision/LLM 0;
- census DB size 2,367,488; byte/logical SHA-256
  `6c076178f19b2d55513b17fea50a3c5e1273dd5ae29e19f0b7b5275744d9d398` /
  `35100d98303de775516b4a408df435a01fe412019e913671b731aec8e96726b3`;
- census cache 231 files / 35,347,600 bytes / tree SHA-256
  `3e563eff1f9c3e39c61b7fa5aec7938a08355d11ed83a29904a0559782d6a816`;
- source-changed row `av23_000003` was excluded from final selection;
- final manifest 10 buildings / 57 occurrences; size 3,401,635;
  byte/self SHA-256
  `53abd4f03c068bf78d5dd6b8401715fe11e8818a948675e21c9877bde8973262` /
  `453f05a8948ee11777cf86e87e1f10cda62068ee9592261b039fd34019789b46`;
- independent production validation 11/11 PASS: full E2/E3 pool replay, exact
  lineage, SQLite integrity/FK/sidecars, accounting, all 116 response/E1/cache
  bytes, fixed-rule final replay, complete 57-row occurrence binding, final and
  report binding; validation network/Vision/LLM 0.

For the v2.3 semantic smoke, 57 occurrences at batch size five plan 12 first
attempt calls, with a hard reservation cap of 14 for strict-invalid selective
retry only.  The total stop remains 312,000 non-double-counted tokens; input
remains 300,000 and output is raised to 15,000 because the completed v2.2+
v2.2.1 measurement was already 13,169 output tokens.  This avoids another
artificial continuation while keeping a small measured envelope.

## v2.3 semantic execution and controlled single-record failure

The first production launch attempts stopped before durable initialization and
before any model reservation.  The first was terminated by an overly short
shell wait; the second exposed a fail-closed consumer-shape bug in the v2.3
semantic lineage gate.  The AV23 manifest correctly stored the v2.2.1 row with
canonical `sidecar_*` / `report_*` keys and nested bound artifacts, while the
semantic sidecar, independent validator, and reviewer renderer expected
nonexistent aliases.  No output, lock, cache, network request, or model call was
created by either attempt.

The three consumers were corrected to authenticate the canonical fields and
the nested continuation DB, continuation report, and parent partial.  The CLI
preflight was also strengthened to run the exact semantic manifest consumer,
not only the AV23 selection validator.  Actual frozen-manifest and scalar/
nested-tamper regressions were added.  Independent re-audit then passed all
three consumers, 1,692/1,692 derivative exclusions, 75 focused tests, and the
explicit Codex CLI 0.147.0 preflight with network/Vision/output all zero.

The corrected paid v2.3 run reached every pre-Vision gate and materialized the
exact 57-file review cache.  It completed 13 reservations/completions with no
HTTP request: 11 successful attempts, one partial-success attempt, and one
strict-invalid selective retry.  Batch 11 accepted four of five rows on its
first attempt; the same remaining opaque row failed both attempts on the
existing cross-field invariant `resolution_insufficient requires uncertainty`.
Batch 12 completed normally, leaving 56/57 accepted results.  Publication was
correctly refused because `result_coverage_100_percent` was 56 rather than 57.

The immutable parent partial is:
`data/enrichment/divisare_architizer_semantic_vision_n10_v2_3.db.partial`

- 1,277,952 bytes; SHA-256
  `c875e33184acdd70bf2a09556ff97c6f25faa269a17c766a706907b20c482624`;
- prefix logical SHA-256
  `f51d33f217db56668b4ecde213e2f5f42132af66e46e3e0f3c900aa75086dea9`;
- SQLite quick/integrity PASS, FK 0, no hot sidecars;
- 56 success / one `vision_failed`, results 56, reservations/completions
  13/13, open 0, fetch/HTTP 0;
- all-attempt usage input 253,942 (cached subset 55,040), output 9,670,
  non-double-counted total 263,612;
- review cache 57 files / 9,469,093 bytes / ordered SHA-256
  `fbaed637f6beb55f54d1a239a8abeb9934dd0915333dddbb17de6b1e0fe415fc`;
- independent replay found the coverage check as the sole failure among 19
  technical validations; all other immutable input, lineage, accounting, and
  integrity checks pass.

Current `--resume` is prohibited: the failed row is no longer `ready`, its
batch has exhausted the fixed two-attempt budget, and resume would make no
model call before repeating the same 56/57 terminal failure.  A separately
versioned v2.3.1 child continuation must instead pin this parent byte-for-byte,
target only the one failed opaque ID, preserve the 312,000-token ceiling, and
independently validate the combined 57-row projection before publication.

The v2.3.1 child continuation subsequently completed successfully after an
independent red-team closed all release blockers around runtime command/
workdir provenance, full parent and child ledger replay, failed-paid-call
durability, path containment, cumulative caps, and recoverable three-artifact
publication.

- run `semn10v231-4e93c2f4294e24753ec31006`;
- one child call, no retry/error/HTTP; only `semv23_000051` targeted and
  accepted;
- child usage input 16,067 / cached subset 0 / output 262 / total 16,329;
- cumulative v2.3 + v2.3.1: 14 calls, input 270,009, cached-input subset
  55,040, output 9,932, non-double-counted total 279,941, all below the fixed
  300,000 / 15,000 / 312,000 ceilings;
- combined results 57/57 and terminal status `complete`;
- child DB `data/enrichment/divisare_architizer_semantic_vision_n10_v2_3_1.db`,
  319,488 bytes, SHA-256
  `6316e603389dd85cf4eb8a80894241fb9b64baa073f31d1b25fccf2639420090`;
- logical SHA-256
  `8579fb6996fefce1e85eae1347c137f318864cbf1628b6474849645158c0a0ea`;
- report 4,487 bytes, SHA-256
  `7a1c7c4a454c30acdf2a8f7e9ab464e9bc9874a049d646151858837d0761caa7`;
- child cache one file / 14,919 bytes;
- independent continuation validator PASS for immutable-parent replay,
  bounded child raw/call replay, exact combined derivations, cumulative caps,
  and logical/report/publication binding;
- all child partial aliases are absent and the parent partial remains
  byte-identical at SHA-256 `c875e331...2624`.

## v2.3.1 strict-blind QA and first unblind

The combined 57-row v2.3.1 population was rendered into a NEW-only opaque
pixel package without exposing semantic labels or model results.  Contaminated
reviewer attempts were discarded before any eligible ledger was frozen.  The
accepted reference used two fresh pixel-only reviews, exact canonical ledgers,
and a third blind reconciler.  All 57 panels on ten sheets were reviewed at
original detail before unblinding.

- review manifest size 31,979; byte/self SHA-256
  `db45c2093b18bf152acb5d487ae8d60bc7011bbdc6f97525ee5b2885799e3c49` /
  `36811f64ea2d034fa7ba9a96f48821bdf834e8fa15a81110a037793c437ad784`;
- canonical reviewer A size 43,818; byte/ledger SHA-256
  `85e093d8cdd04fc4836d76bcdaa0b46f089b0d34eba0044660e7ba97cb08da4e` /
  `eb0e9747917f48b67ff6ad12455134eff97ad51bf902885df097aeceadd1f5ea`;
- reviewer B size 46,419; byte/ledger SHA-256
  `b54f753e5a437c8a59c9e33c67e4b0ec70b8827cd1705f4a52039f42aa628142` /
  `533fdf31540a24d73752f8412c38db32741f0957ece04ba74f09cc0b562e6a84`;
- both exposure attestations are false for semantic labels, model results, and
  peer ledgers, with blind review completed before peer exchange;
- reconciliation covered 29 disagreement rows / 88 material dimensions,
  resolved all 88 with pixel-grounded reasons, and left unresolved 0;
- reconciled frozen ledger size 73,918; byte/self SHA-256
  `4accdbfe445589c2bed32a5fdb16777cb3b8259390c3d55470a156d5dad6a66b` /
  `30518b4e6ccdbb6651d3305482b8fb7bcebb3b4a2d6205cb211b5460baf1ddee`;
- official input authentication and reconciled-ledger validation PASS before
  any semantic DB/model result was opened; network/model calls 0.

The v2.3.1 first-unblind inspector was then executed exactly once.  Its
no-clobber output is
`data/reports/divisare_architizer_semantic_vision_n10_v2_3_1_inspection.json`,
size 20,182, byte SHA-256
`f29151b6a46229d293cae6d64e7dadcc473073c15617c848a195e18de84e01ff`,
embedded inspection SHA-256
`0fb98209323855660acbbea5db4e4362e86499b0e90b6399370489c0aa5faacf`.

The technical and reference pregates all PASS: exact ten buildings / 57
occurrences / 57 results / 57 reference rows, missing 0, two independent
references reconciled, frozen before DB open, SQLite quick/integrity OK, FK 0,
no hot sidecars, semantic DB unchanged, and inspection network/LLM/Vision/DB
writes all 0.  Scope is also exact at 57/57 with zero clear errors, and result
coverage and the predicted-uncertainty overflow cap pass.

The terminal quality verdict is nevertheless `FAIL`:

- nine-field acceptable agreement is 442/513 = 86.1598%, below 90%;
- medium has 11 clear errors, dominated by photograph/drawing images called
  rendering and one rendering called photograph;
- purpose-based framing over-demotes complete documentary views and also
  misapplies photograph axes to drawings: framing has 15 clear errors and
  project legibility has 17;
- critical hero false positives are 2 (`semv23_000052`, `semv23_000051`), and
  usable hero retention is 29/36 = 80.5556% with seven usable false negatives;
- uncertainty predicts 6 rows versus 18 reference rows: TP 4 / FP 2 / FN 14,
  precision 0.666667, recall 0.222222, F1 0.333333;
- aggregate uncertainty axes are TP 4 / FP 2 / FN 18, precision 0.666667,
  recall 0.181818; resolution-insufficient has two false negatives and zero
  true positives;
- row precision and overflow therefore remain controlled, but the v2.3
  three-condition boundary threshold suppresses too many real adjacent-label
  and insufficient-resolution cases.

This cohort is permanently frozen and will not be inspected again.  The next
iteration is v2.4 on a fresh-disjoint N10.  Its prompt correction must restore
medium/applicability recognition and uncertainty recall without reviving
v2.2's broad framing/legibility overcall; the fixed schema, vocabularies,
normalization, hero/coverage derivations, and all hard-gate thresholds remain
unchanged.

## v2.4 prompt freeze

Prompt v2.4 is a prompt-only correction.  It commits medium before dependent
axes, applies a linework-first drawing rule and the full non-photograph
applicability cascade, defaults to photograph when ordinary camera capture is
plausible unless a positive synthesis cluster is visible, measures framing by
the communicated subject rather than generic crop/completeness, and grades
legibility from the delivered pixels.  Resolution insufficiency now requires
the visibly affected uncertainty axes, while boundary uncertainty uses direct,
central, strict-definition overlap without v2.3's hypothetical-reviewer flip
test.  Ordinary context/crop/polish remains explicitly CLEAR to avoid v2.2's
overcall.

- module `canonical/cross_source_semantic_vision_v24.py`, 20,053 bytes,
  byte SHA-256
  `bef39c172f4be716e1be58505d88bd4de1fba07879204a9aa726513ec8da52b9`;
- composed prompt 17,179 UTF-8 bytes/chars, lexical proxy 2,969, SHA-256
  `13d84e3738f29e710db43cc9be727e2644e1e7be3a85afba4edf24bedd49c5ae`;
- test SHA-256
  `7b284205a639a168bbc216deb001d5cd0d859d85a724795f007873ca627231f7`;
- every model-facing schema/vocabulary/normalizer/transform and deterministic
  hero/coverage object remains identity-equal to the frozen base/v2.3 objects;
- independent root replay: v2.2-v2.4 prompt regression 53/53 PASS;
- independent static red-team: GO, blocker 0; no source names, opaque/prior row
  IDs, target counts, or quality/hero/coverage metrics in the composed prompt;
- complete evidence remains feasible below the fixed 500-character schema
  maximum (max-applicable constructed case 440, impossible compact all-eight
  stress case 499).

The measured risks for the fresh sample are a subtle photoreal rendering being
absorbed by the photograph prior, photographed physical models needing their
positive model cues, or the relaxed strict-overlap boundary moving uncertainty
back toward v2.2 overcall.  These are distributional risks, not static
contract blockers, and will be judged only on a fresh-disjoint blind N10.

## AV24 depleted-lane rule and initial census finalizer failure

The result-blind AV24 scan found that the fully disjoint
`architizer/cover_quality_risk` remainder contains five buildings, but their
frozen coverage counts are only 2, 4, 1, 4, and 1.  Thus no candidate can meet
the historical five-occurrence building minimum.  For AV24 only, this lane's
minimum is explicitly four; its pool remains two buildings and its final quota
remains one, retaining one HTTP fallback.  Every other lane remains minimum
five, and no prior row, semantic result, QA judgment, or failure position is
used.  The selection rule/domain and tests pin the exception so it cannot
spill into another lane.  The resulting offline pool is 20 buildings / 115
occurrences, and independent full source replay is byte-identical.

The first bounded census completed all 115 HTTP requests with 113 exact and
two source-changed outcomes, Vision/LLM 0.  It nevertheless published no final
selection because the inherited runtime finalizer still hard-coded the global
minimum of five.  The risk lane's first candidate had only three exact rows
after one source change, while its second candidate retained all four and is
valid under the approved AV24 rule.  All other quotas are feasible.

The terminal `insufficient_exact_pool` DB is preserved as immutable technical
evidence (1,839,104 bytes; byte SHA-256 prefix `ecbeb8f0`, logical SHA-256
prefix `c873d0`), with no final manifest.  It will not be mutated or resumed.
Rather than repeat 115 network requests, a separately versioned NEW-only
replay continuation will authenticate every original reservation, attempt,
completion, response, E1, and cache byte, record network requests 0, apply
only the lane-scoped minimum-four finalizer, and publish to distinct paths.
An independent validator must prove the original full HTTP ledger, the
source-changed exclusions, all other minimum-five lanes, the exact rank-2
four-occurrence risk choice, and final occurrence binding before any semantic
runtime consumes the corrected pins.

## AV24.1 replay finalization

The AV24.1 finalizer completed as a NEW-only, network/model-zero replay after
an independent release red-team GO.  It authenticated and attested all 115
parent reservations, completions, attempts, and results plus the complete
cache, excluded both source-changed rows (`av24_000039`, `av24_000099`),
rejected risk rank 1 at 3 exact rows, selected risk rank 2 at 4, and kept the
minimum at 5 for every other lane.  The corrected final population is exactly
10 buildings / 58 occurrences.

- offline pool: 3,969,135 bytes; byte/self SHA-256
  `4716d9e9fab84ba0bc4eaa67b2a535e9a122a73d6c7b1d49c81aa234afd0d32a` /
  `ee8ab6ec94d9aac632936e6885336c42c16a2532c20f89317e50ee6fd3035adb`;
- immutable parent HTTP DB: 1,839,104 bytes; byte/logical SHA-256
  `ecbeb8f0667cadc3a5650c45a62fba844915f7e79f7331fb9496a4511918213e` /
  `c873d0a878c0d5def518fd6a6d6685fddd31803252228b2c1a801bc131a05a3b`;
  run `semav24-a03692ec59b59884014b8dc4`, terminal
  `insufficient_exact_pool`, HTTP 115, exact/nonexact 113/2;
- parent report: 308 bytes, SHA-256
  `bdbe3e5c5112aea7ae327072c4a94eb136500c33391d78e61cda6ca4b0b4e3b1`;
  cache 228 files / 40,505,068 bytes / tree SHA-256
  `6c906474e04b108e8a876a4f5444d1c6de6d4c4ca7b6fe18f872d83ec59d0126`;
- replay child DB: 577,536 bytes; byte/logical/schema-object SHA-256
  `63927ee8c813e5e11cd3e8e4a8bae7a5105015f3bbf49a7c5642e4b090d4ec01` /
  `7cca493e7a1dcb9d3e684039cb15c8ac29befa5384e2a2b0d467c8847b6b2237` /
  `c8d435515bad999895af74e27fac84328c4183db13a1649a37cbaecbe9af039e`;
  run `semav241-b0ec1222645a3fbafbc94c6c`, network/Vision/LLM 0/0/0;
- child report: 507 bytes, SHA-256
  `00150e4ba59eccd6c3d7bb9ece2b5872075ef4c4ced1eb23c8f733dcaf1bf08f`;
- corrected final manifest: 3,659,737 bytes; byte/self/config SHA-256
  `5174b82e6254fd51d804899a90226d94d3c43f5616594e906f8b31e155fc6e04` /
  `e85099f5ea1e987bf91944857b628c76c1cb34ba755a65bc06aed85575b5293c` /
  `cd43020ff791c3e086b0314345bf4a528a6f17ce3128dc99b314e58e41a716c3`;
- ordered building/candidate/E1/URL/occurrence SHA-256:
  `f93a0fcd073721fed163af5ec0413e5fb24bc642fc597be5cda0eb4316b05186`,
  `62ad1751dd93b255514ac12c920d072d6543473302365cbce4c1e65a34fd958f`,
  `7e4b22d54d3b970bcec806f7f043995b2c147e66eff958e0e3dbcbd012a89cb2`,
  `293aeb944602c46fe857e30300ebbe38eb1ed303c9452edb2dd12c2172fbb3c9`,
  `12edf665fbcd89f67c51156309967e8610e86f8807ab04e26076205ac2f46b83`.

The independent production validator is 15/15 PASS, including full parent
ledger attestations, independently reconstructed final building payloads and
all ordered/config hashes, exact evidence binding for all 58 occurrences,
production pins, path separation, and validator before/after read-only proof
with no SQLite sidecars.  Focused AV24 + AV24.1 tests are 14/14 PASS.  The
sample-depth tradeoff is confined to the one risk-sentinel building, which has
four views instead of five; pool2/final1 still provides one HTTP fallback.

## v2.4 paid semantic terminal

After the frozen runtime and independent release audit both returned GO, the
bounded fresh-disjoint N10 was launched with the exact npm Codex CLI 0.147.0.
All pre-Vision availability, prior-semantic, derivative-exclusion, snapshot,
and immutable-input gates passed.  The run completed without a selective
retry or any HTTP fetch.

- semantic run `semn10v24-cf1f30496bb283c778a7ef47`;
- 10 buildings / 58 occurrences / 58 successful inputs / 58 results;
- 12 reservations, 12 completions, and 12 first-attempt successes; retries 0,
  errors 0, semantic HTTP/fetch 0;
- input 250,362 tokens (cached-input subset 88,064), output 10,705,
  non-double-counted total 261,067, below the fixed 300,000 / 15,000 / 312,000
  observed-stop thresholds;
- DB `data/enrichment/divisare_architizer_semantic_vision_n10_v2_4.db`,
  1,327,104 bytes, byte SHA-256
  `1a12cb638b6e72af226b84d44e5f1c397e044c193d7d360c69db616342266003`;
- logical SHA-256
  `eaa0c25c32b1520d3b01ddf6258353525286c5417ed8a3eec67c9e8176564276`;
- report 703 bytes, SHA-256
  `bae101bb26d43e9380e7a90ae93459bc60ca168216d963d39c00139af379872e`;
- review cache 58 files / 11,629,718 bytes / tree SHA-256
  `3bc5e2b9ddf0291c5578f4884ec321bed9f32591072e3760b6acf7b4f648a681`.

The runner's terminal validation passed, and a separately invoked independent
validator was executed exactly once after the writer exited: exit 0, all error
checks PASS, exact 10/58 population and 12-call replay, SQLite and immutable
input/cache/report binding PASS.  No semantic result content was opened during
monitoring or validation.  The next step is an opaque review render and two
fresh independent strict-blind ledgers before any first-unblind inspection.

The post-run review renderer was then pinned to the exact terminal DB, report,
logical digest, cache tree, run status, and 58 result/success/link rows.  The
post-run v2.4 focused suite is 89/89 PASS and the independent terminal
validator remains PASS.  Rendering used only the opaque review cache and
produced 58 inputs on ten JPG sheets with network/model calls 0:

- manifest 37,439 bytes; byte/self/order SHA-256
  `e7e83a3ae74b5074b321cf0b58eaced4735e257e1aa372b3add96644490805ee` /
  `971c444d188d879f4a85af212b8c856d0b6531f96c3b9c03a5daad1dcbe88d2c` /
  `d3ac4076690320759d45ca4e4ce1f8f210e6868c065cabfbb65d634c28e1bab4`;
- sheets `001` through `010` byte SHA-256:
  `fe760342f385350ac84f9c36e457b18ee8e1be9cc432f70ebc50660810c81143`,
  `1d2dc9ea64a2b6e68c3c71b8a3e62842d4a3dd8b34f05804e210687582f7b411`,
  `c8a67d153771619af67fbccc1d548072d7440276ced94a9e898a9ef00b163ddb`,
  `643465e6b61ec2c35b91ae94cc752b5e541b0826590e568fc2f37c283630e406`,
  `b00e354133061ecd14ae2274a1e1c0e6d5ef52bd4bff23cd14cf3d799677a137`,
  `84a10ba47298785289c24894a4dd6831ac8562b57d493b0a8af71753104a55e1`,
  `edfc3d7ad1b88ae27b3ce849f4da704beda1b11c8c66fb2e9c67d94f2a315641`,
  `faf95d0e5109de152b94766d0ce2f2d14df421bdb5f8c10261a861d3aff12069`,
  `68cb67066eb2dd97f910703909951a3c6178034cfb6c0bfaf4fcd60ba01232a3`,
  `7b075b13d0562e1a803a3b6ecd218804f0c83904fcd5ea5443d7f07ba20e5763`;
- two distinct no-clobber blank drafts each contain 58 rows and zero populated
  review fields: reviewer A 30,277 bytes / SHA-256
  `a1271b7a8fea60f681a3b8b641a648c5329ff3d20fe0d4a3e128d07925284adb`,
  reviewer B 30,277 bytes / SHA-256
  `70b5a764602a20d1fd5c831f544b63fdd1bb2a08481505ad638108367f9fa234`.

Both fresh reviewers completed all ten sheets / 58 panels at original detail
before peer exchange, with semantic/model/peer read flags false and no
network/model/delegation.  Their first locally frozen copies were quarantined
because the local serializer omitted the official `completed_at` and
`ledger_sha256` envelope.  The official freeze then caught only mechanical
cross-field applicability issues in the drafts (drawing/rendering/rejected
N/A axes and one top-level draft status).  Each original reviewer corrected
only their own draft from their already completed pixel judgments; no peer or
semantic content was exposed.  The official no-clobber freeze and validator
then PASS for the exact 58-row population:

- official A 46,573 bytes; byte/ledger SHA-256
  `a61d2a61f5c32465965c34282d715b24ea3a57b9a1f7e7c9f0323beeb8dc1430` /
  `fb697b3b7395b767c32006e9354836072f15a3a2cdf1d2a778441861f1d21b16`;
- official B 46,355 bytes; byte/ledger SHA-256
  `2b1cf7efb941752c1161639ae6f5224a74ea14082bef79796a6fd3b05bf96aa4` /
  `3466ea441d338459cfc869e3acfde5af433c61fcc934b83254186ceeb96bfb7b`.

Only these `*_frozen_official.json` ledgers are eligible for reconciliation;
the earlier local frozen copies are explicitly ineligible and must never be
used as QA inputs.

## v2.4 strict-blind reconciliation and first unblind

The two eligible official ledgers were authenticated against the opaque
manifest before either semantic labels or model results were opened.  A fresh
reconciler reviewed all ten sheets / 58 panels at original detail and resolved
every material A/B disagreement from pixels alone.  The frozen reference is
therefore a pre-unblind artifact, not a model-informed adjudication.

- official A byte/ledger SHA-256:
  `a61d2a61f5c32465965c34282d715b24ea3a57b9a1f7e7c9f0323beeb8dc1430` /
  `fb697b3b7395b767c32006e9354836072f15a3a2cdf1d2a778441861f1d21b16`;
- official B byte/ledger SHA-256:
  `2b1cf7efb941752c1161639ae6f5224a74ea14082bef79796a6fd3b05bf96aa4` /
  `3466ea441d338459cfc869e3acfde5af433c61fcc934b83254186ceeb96bfb7b`;
- 20 disagreement rows / 87 material dimensions / unresolved 0;
- authenticated blank template 137,673 bytes, byte/self SHA-256
  `ba66197afa66ff81e403e9c8a24c3e1d6f48cd5a00ba9f573212f0d33e86acb9` /
  `cf5b4fcc131e319ecef3601259cb0db73d666fd6364b113f23069c00f1d58eef`;
- resolved draft 167,111 bytes, byte SHA-256
  `a8b26a5edb60ee09f505bf38d1598434670167b4dc6f7464f59ce43f6196ad56`;
- final `reconciled_frozen.json` 76,232 bytes, byte/ledger SHA-256
  `51cb9a185eedf30a11537216fa0fbcf29b955a778f2d291e9a52309a72b59d21` /
  `d534e4b60962102c60c4dea2bbbcbb5439469f74eaac836d7559e8324d8386e7`.

The official reconciliation-contract validation passed: exact manifest/order,
canonical bytes, A/B byte and self hashes, all 58 rows, agreed dimensions
locked, one pixel-grounded reason per disputed dimension, unresolved 0, and
`semantic_labels_read=false`, `model_results_read=false`,
`completed_before_unblind=true`.  Network/model calls were 0.

The v2.4 first-unblind inspection was then executed exactly once and must not
be rerun.  It produced
`data/reports/divisare_architizer_semantic_vision_n10_v2_4_inspection.json`,
20,520 bytes, byte SHA-256
`cd925c75130270423d3cb28ae526f5eea0272fc22e508b797eb6372fee77b263`,
inspection self SHA-256
`bcf167de5701592db1a1f492c48b1e2929aacdea6c1ca4d47732f5f1f2fee291`.
The terminal verdict is **FAIL**.

Passing gates:

- exact pinned population and 100% result coverage;
- nine-field acceptable 495/522 = 0.948276;
- usable-hero retention 34/37 = 0.918919;
- resolution-insufficient false negatives 0;
- all operational pre-gates, SQLite/integrity/immutability checks, with
  inspection DB writes/network/Vision/LLM all 0.

Failing gates:

- one critical hero false positive and one clear scope error on
  `semv24_000040` (`eligible` versus reconciled `rejected`; an equal-panel
  photographic cloud/timber layout with no dominant image, reconciled as
  `multi_panel_without_dominant_image`);
- one clear medium error on `semv24_000001` (`drawing` versus reconciled
  `rendering`);
- uncertainty overflow: predicted rows 25 versus reference rows 10 and cap 16;
- uncertainty quality: row TP/FP/FN/TN = 8/17/2/31, precision 0.32, recall 0.80,
  F1 0.457143; aggregate axis TP/FP/FN = 5/27/5, precision 0.15625,
  recall 0.50, F1 0.238095.

The uncertainty row false negatives are `semv24_000037` and
`semv24_000038`.  Axis false positives are dominated by framing (15), then
legibility (5), spatial (3), camera (2), and medium (2); axis false negatives
are framing 2, drawing 2, and medium 1.  Clear per-field error counts are
camera 4, drawing 0, framing 8, scope 1, medium 1, legibility 8, state 1,
reject reason 1, and spatial context 3.  Hero false negatives exist on
`semv24_000022`, `semv24_000053`, and `semv24_000056`, but aggregate usable
retention still passes.

The v2.4 cohort, its two official blind ledgers, reconciliation, and exactly
once inspection are now permanently frozen.  No prompt or metric change will
be judged on this population.  The next calibration is v2.5 on a new
result-blind, fully disjoint N10; N100/full remain prohibited until every hard
gate passes on such a fresh cohort.

## v2.5 prompt and AV25 fresh-pool design

The v2.5 prompt is frozen without a model call.  It keeps the output schema,
vocabularies, normalizers, transform, and deterministic hero/coverage objects
identity-equal to the base and v2.4 implementations.  Its changes tighten
per-axis boundary evidence, distinguish unrelated photographic panels from a
coherent same-project drawing sheet, and prevent volumetric perspective
renderings from falling through the linework-first drawing rule.

- module `canonical/cross_source_semantic_vision_v25.py`: 22,719 bytes,
  SHA-256 `ed0a3a7b30c3ed51dd1501392e00344d0c0e8afb11a494cef33e0ca4533a81aa`;
- composed prompt: 19,837 UTF-8 bytes/chars, lexical proxy 3,403, SHA-256
  `0f777175bf3ae3db7b873500ec8627545ac0a5f17b1c0148e24072f3c267ffa5`;
- output schema remains
  `e72058e372e7883fc8ca80d690728fb16b9a7fdd469037f13311a8e04069eff4`;
- prompt tests 21/21, v2.4+v2.5 40/40, and independent base+v2.4+v2.5
  release audit 92/92 PASS; static release verdict GO;
- a valid maximum-applicability seven-axis result fits exactly 500 evidence
  characters and normalizes; 501 is rejected.

The fully disjoint AV25 scan then proved the historical Architizer
`cover_quality_risk` quota depleted: the four result-blind residual candidates
have deterministic frozen occurrence counts 2, 3, 1, and 1.  Minimum 4 has no
feasible building, minimum 3 has only one (so pool2 fallback is impossible),
and minimum 2 is feasible but produces a weak two-view sentinel.  No semantic
result or QA judgment was read for this decision.

The selected minimal robust sampling-policy change moves that one Architizer
risk slot from `cover_quality_risk` to the pre-existing result-blind
`gallery_fallback` stratum.  Every lane again uses minimum 5, while total 10,
core8/risk2, one risk sentinel per source, pool2/final1, and source balance are
unchanged.  The Architizer gallery fallback has 7 prefilter / 7 feasible
candidates; the chosen pair each has 6 occurrences.  Independent full source
replay produced the same frozen pool bytes:

- `cross_source_semantic_availability_pool_n10_v2_5.json`;
- 20 buildings / 120 occurrences, every candidate 6 occurrences;
- 4,250,181 bytes; byte/self SHA-256
  `72e3b4a7303b49d0ca8d1f22a635a8910e4f0a0845b25ee3a6ed5ba6cc45e6e1` /
  `a38395d1f660287584ba18f76d0ca4fccb828f5ba96f083bf08426fb847a0f01`;
- prior building/pixel/URL overlaps 0 and intra-pool pixel/URL duplicates 0;
- all-exact final forecast 10 buildings / 60 occurrences, with either rank-1
  risk candidate replaceable by rank 2.

Two pre-network defects were found and fixed: a misplaced AV24 lineage block
that referenced an undefined cache path, and an inherited v1.6 finalizer that
still read the old cover-risk quota.  Independent in-memory all-exact and
rank-1 source-change fallback tests now both yield 10/60 with the new quotas.

## AV25 sandboxed HTTP technical failure

The first AV25 HTTP-only census launch was incorrectly run with default
sandbox permissions rather than the required elevated network permission.
Windows denied every socket locally with `WinError 10013`; no HTTP status or
response body was received.  The inherited circuit breaker ignored
status-NULL connection errors, so the silent CLI continued retrying until the
root monitor inspected the growing DB and interrupted it.

The failed run is preserved and must never be resumed or used as availability
evidence:

- run `semav25-8720325f26388ff924fb24f1`, status `running` at interruption;
- partial DB 1,957,888 bytes, SHA-256
  `78ad97d9213d634cdfadfd6e64de9a816d43ed84c87de025bd29b9aaaa1bd1b2`;
- SQLite integrity/quick check PASS, FK violations 0, no WAL/SHM/journal;
- 85 distinct census IDs touched; 253 durable reservations = completions =
  fetch attempts; 84 `retry_exhausted` results and the 85th input with one
  attempt; every attempt `error_kind=connection`, HTTP status NULL;
- final DB/report/manifest/cache absent; model/Vision/LLM calls 0.

This is a technical execution failure, not a source-change or sample-quality
outcome.  A NEW-only AV25.1 census must authenticate the failed parent bytes,
write only distinct paths, perform a complete fresh 120-input census under
explicit network approval, and stop after a small consecutive status-NULL
connection-error threshold.  The original partial remains immutable audit
evidence.

## AV25.1 hardened retry release and external quota block

AV25.1 was implemented as a fresh-only HTTP census with app `A251`, schema 3,
and deterministic run `semav251-180bb4feb35fa1b22ff80191`.  It imports no
failed-parent outcome or cache byte.  Its preflight independently authenticates
the exact 85-attempted / 84-result / 253-call failed parent, the frozen AV25
20/120 pool, E2/E3, path separation, expected absence, and source lineage.
Connection failures with no HTTP status now open a circuit after exactly three
durable attempts; the inherited 429/5xx and bounded Retry-After circuits are
also independently replayed.

The final selection-release implementation uses handle-owned Windows file
operations throughout.  All AV25.1 writes keep their creation handle open,
link the held inode with no replacement through
`NtSetInformationFile(FileLinkInformation)`, validate status/IOSB and inode,
and clean up with held-handle disposition.  There is no `os.link`, inherited
atomic writer, rename/replace, or pathname-unlink fallback in the active
AV25.1 path.  Publication rollback preserves the HTTP ledger while leaving no
invalid public artifact and never removes a raced foreign file.  Unsupported
platforms fail closed before production use.

Frozen release identities:

- sidecar 86,536 bytes, SHA-256
  `8effd899e7469df5b0fabdadc816c5f32ba086dc77c558a5f72966b59eb70cfd`;
- independent validator 106,528 bytes, SHA-256
  `b33574b3976dfb4e6a8a2322538129dd312cc27236872fbf5ebee84e8d0f4292`;
- run CLI SHA-256
  `b0a019f7b5034bc57ef5bf148c8fe789d870588436550aa16645585da0b0c390`;
- validator CLI SHA-256
  `ed22d9a7e7efe8bf4246eca9b4571c227ad3a39849c5085ae1eae06641b0b797`.

Independent final evidence is GO for the HTTP-only selection step: compile
6/6, held-source/all-writer/actual-transport delta 18/18, full producer 50
PASS / 2 Windows-appropriate skips, independent validator 33/33, and exact
production-path preflight PASS in 47.8 seconds.  Preflight recorded pool20/120,
parent85/84/253, exact E2/E3, and network/Vision/LLM 0.  Eleven immutable
hashes remained unchanged and 27 prohibited output/partial/lock/sidecar paths
remained absent.

The authorized elevated HTTP launch was then rejected by the environment's
automatic approval reviewer **before process creation**, citing a usage limit
and a retry time of **2026-08-20 13:13**.  The command was not retried or
circumvented.  Post-rejection checks prove every AV25.1 DB/report/final/cache,
partial, lock, and SQLite-sidecar path remains absent; no census process was
created, no HTTP request was sent, and the frozen source/pool/failed-parent
hashes are unchanged.

Work therefore pauses at an external execution-quota boundary, not a code,
data, or release-safety blocker.  On or after the stated retry time, resume by
rehashing the frozen sources and failed parent, rerunning the exact no-network
preflight, launching the single approved elevated AV25.1 HTTP census once,
waiting for writer exit, and invoking the independent terminal validator
exactly once.  Semantic Vision remains fail-closed until those terminal AV25.1
pins exist.  N100/full remain prohibited.

The offline semantic cost checkpoint remains deliberately fail-closed while
AV25.1 terminal pins are absent.  Runner production pins, independent
validator production contract, and review pins are all `None`; an explicit
Codex 0.147 preflight exits before invoking Codex with
`v2.5 production manifest pins are not finalized`.  Focused v2.5 runtime and
review tests are 85/85 PASS and eight source files compile; all semantic output
paths remain absent.

For the expected all-exact 60-occurrence population, 12 first-attempt batches
are projected at roughly 258,336 input + 11,075 output tokens by direct v2.4
prompt-delta scaling, or about 265,193 + 11,075 by the more conservative
decomposed estimate.  The conservative 12-call compound upper envelope is
299,068 input / 12,788 output / 311,856 non-double-counted total, leaving only
932 / 2,212 / 144 against the fixed 300,000 / 15,000 / 312,000 observed-stop
thresholds.  Fourteen full-batch-equivalent calls would be approximately
348,913 / 14,919 / 363,832 and are not authorized merely because the
reservation-count cap is 14.  Since observed stops act before the *next* call
but cannot undo a just-completed overshoot, v2.5 must add a pinned conservative
next-call token reserve gate before every reservation.  A selective retry is
allowed only when observed usage plus that reserve fits all three thresholds;
otherwise it pauses before mutation/call and requires a separately audited
continuation.  Exact reserve values and validator pins will be frozen offline
before the semantic launch.

## 2026-08-14 fresh N10 restart checkpoint

The user explicitly restarted the fixed-envelope N10 work.  No N100/full
authorization was inferred.  Work resumed only at the existing fail-closed
AV25.1/v2.5 boundary; no HTTP, Vision, or LLM call was made.

The partially added prospective token reserve was completed across the v2.5
writer, production runner, independent validator, and blind-review renderer.
The immutable reserve remains 25,000 input + 2,000 output tokens.  Before each
Vision reservation, the writer now transactionally records the exact observed
usage, reserve, projected usage, thresholds, version, and allow decision in
the canonical reservation request.  Both SQLite and Python reject a call on
unknown prior usage or when any projected input/output/total value exceeds
300,000 / 15,000 / 312,000.  The independent validator reconstructs every
snapshot using only attempts with a lower reservation ID and rejects scalar,
chronology, canonical-JSON, or request-SHA drift.  Terminal publication and
blind review also bind the three reserve pins and reserve metrics.

The reserve is conservative against the frozen v2.4 observed per-call maxima:
22,726 input, 1,177 output, and 23,563 non-double-counted total.  New boundary
tests prove equality is allowed, +1 on input/output/total is denied, unknown
usage is denied, a denial creates no next reservation/attempt, selective
retry uses the same gate, and a rehashed request-snapshot tamper is rejected.

Offline verification on this checkpoint:

- compile of the writer, independent validator, production runner, validator
  CLI, and blind-review renderer: PASS;
- v2.5 prompt/runner/validator/CLI/review suite: 89/89 PASS in 43.78 seconds;
- AV25.1 producer suite: 50 PASS / 2 Windows-appropriate skips in 104.54
  seconds;
- AV25.1 independent validator suite: 33/33 PASS in 277.31 seconds;
- one earlier combined 174-test invocation hit its 244-second command timeout
  without a terminal pytest result; no pytest process remained.  The same
  suites were then split as above and all received terminal PASS results.

The exact AV25.1 no-network production-path preflight was rerun once and
returned `preflight_pass` in 51.1 seconds.  It reauthenticated pool 20/120,
failed parent 85 attempted / 84 results / 253 reservations-completions, E2,
E3, path separation, and absence of all child outputs.  Network, LLM, and
Vision requests were each zero.  Frozen identities remained:

- AV25 pool 4,250,181 bytes, byte/self SHA-256
  `72e3b4a7303b49d0ca8d1f22a635a8910e4f0a0845b25ee3a6ed5ba6cc45e6e1` /
  `a38395d1f660287584ba18f76d0ca4fccb828f5ba96f083bf08426fb847a0f01`;
- failed AV25 parent 1,957,888 bytes, SHA-256
  `78ad97d9213d634cdfadfd6e64de9a816d43ed84c87de025bd29b9aaaa1bd1b2`;
- E2 10,164,682,752 bytes, SHA-256
  `4728f9f015d7fba303923df40f550c59835a7e2a5c1316eef03c499ee3ca7b19`;
- E3 10,236,592,128 bytes, SHA-256
  `8512e11f8e1fd581038f790b27a67c0a8b1949067bf53b3ef30c4ea3534141a4`.

Updated offline semantic release-source identities:

- writer sidecar 254,526 bytes, SHA-256
  `28590cf2dd04395d872d396ddd9572b3342613c14d4822f8b1b2c4ca1adb6e35`;
- independent validator 129,863 bytes, SHA-256
  `7046daeaf686f470b3fe5365220af27fa0b58db92558741429c63e2779588ebe`;
- production run CLI 46,568 bytes, SHA-256
  `77d8fa091086d7635fa5bfee71ceac6465f9a10cf61db91fdfe4a4c11265ae35`;
- independent validator CLI 6,374 bytes, SHA-256
  `2892463a3f765d8f6c944b2db8c32e409fdff90536b9f9ac5e81a93fdb6068bc`;
- blind-review renderer 70,893 bytes, SHA-256
  `a38352b529456d74eba3d4622250be945b0dd8b194e39d4f45c8d75ee941d9a2`.

Every AV25.1 public DB/report/final-manifest/cache path and every semantic
DB/report/review-cache path remains absent.  Production runner, independent
validator, and review pins remain `None` because terminal AV25.1 artifacts do
not yet exist.  The recorded external execution-quota retry boundary remains
2026-08-20 13:13 KST and was not bypassed.

Resume on or after that time by rehashing inputs, rerunning the exact
no-network AV25.1 preflight, launching the one approved fresh elevated HTTP
census, waiting for writer exit, and running the independent AV25.1 terminal
validator exactly once.  Only after a terminal PASS may its exact pins be
installed and the semantic no-model preflight audited.  The fixed paid N10 is
then 60 occurrences, 12 first-attempt calls, at most two strict-invalid retry
reservations, and the prospective reserve above.  N100/full remain prohibited.

## 2026-08-16 AV25.1 terminal PASS and semantic v2.5 release freeze

The user explicitly resumed the N10 work after the active Codex account was
corrected.  The one authorized fresh elevated AV25.1 HTTP census then ran in
its existing `_v2_5_1` namespace.  It did not reuse the failed AV25 outcome or
cache.  The writer exited 0 after 174.7 seconds; its durable run timestamps are
2026-08-15T14:38:57.362012Z through 2026-08-15T14:40:39.634634Z.  The result is
`semav251-180bb4feb35fa1b22ff80191`, status `complete`, app `A251`, schema 3,
with 120 reservations/completions/results, 120 physical HTTP requests, no open
reservation, 112 exact results, 8 source-changed results, and final selection
10 buildings / 56 occurrences.  Vision and LLM requests were zero.

The AV25.1 independent terminal validator CLI was invoked exactly once after
writer exit.  It exited 0 in 130.9 seconds with 31 checks, zero failed checks,
`passed=true`, and network/Vision/LLM all zero.  It must not be rerun.  Terminal
artifacts are:

- census DB 2,441,216 bytes, byte SHA-256
  `35acfae4f39fc963bc83258955082d37375790b274b5b7f6b013a4765169d559`,
  logical SHA-256
  `765e305ed46b99247764bfa1cac626cb42da3726b7526ae88ab5f8d3c4a95fef`;
- report 298 bytes, SHA-256
  `2157e6be65b0d844d54f39032e788f1b1669c942edd315cbbff6cd30d173614b`;
- final selection manifest 3,910,616 bytes, byte/self SHA-256
  `da90f661b83e38426c26479df9890f0ed292389173a1d40c4dfb97aa8951e281` /
  `f61809b55e631755b95bcbe33fc9de9202dd31840c81c4087808347e6783aee6`;
- availability cache 232 files / 34,193,669 bytes, tree SHA-256
  `c37d94571f984784ae78e762a42eb728e7753fbedcabf1c29feb2f7d819482c1`;
- ordered building/occurrence/fetch/E1 pins
  `c37d23445db4557aac71ab44bdfd03aa70f9d38f1a7b0084fb6f7fa063d4d2c0`,
  `eda401b2043ba0bde633c135a0713a84c0d2a77288cc99f7f89f64e21d825f62`,
  `b432a7c52dfc169c0653d2d91c033c5dc220b31dfc3981d1487356af0b02db1c`,
  and `f449882c372f872c358e533151fbd25f1c1cdf65f4622b1eeb01c7bc2023b7ea`.

The 56 selected availability derivatives were independently replayed against
their encoded size/SHA records with zero mismatch.  Their deterministic future
semantic review-cache projection is 56 files / 8,923,865 bytes, ordered
manifest SHA-256
`cedbab5527f7e690c07e0f95c073bfcefa0c9f2455932d0382c38f8c14421539`.
The predicted byte-tree SHA is
`7d89c90ac47e520f2dffafe0dfa54888e1a9a0706543021d27f0458bd4ade2f0`,
but it is not a terminal pin until actual materialization is independently
validated.

The exact AV25.1 pins were independently copied into the semantic producer and
validator.  Their generated `ValidationContract` values compare equal.  The
semantic run is `semn10v25-60fd1966784f8dc678c54552`; its prompt/runtime,
dependency, schema SQL, and schema-object pins are independently frozen.  The
review renderer contains the same selection/runtime values as
`PRETERMINAL_PRODUCTION_PINS`, while its actual `PRODUCTION_PINS` remains
`None`.  This deliberately prevents a `complete_with_failures` or otherwise
unvalidated semantic artifact from reaching blind QA.  After a successful
semantic writer and independent terminal validator, all ten semantic
DB/report/review-cache/status/count pins must be installed atomically.

During release review, two inherited renderer wiring defects were fixed: the
five failed-parent AV25 paths are now present in the function signature and
forwarded both to the independent validator and the local availability gate;
the local child-artifact helper receives only its defined four child paths.
Two regression tests freeze these call boundaries.

Offline release verification completed as follows:

- five semantic writer/validator/CLI/renderer sources compiled successfully;
- the first combined semantic pytest attempt exposed a sandbox-denied default
  Windows pytest temp root plus one synthetic exact/nonexact fixture typo; no
  product artifact was created.  With a repository-local isolated basetemp and
  the fixture corrected, the final semantic suite passed 91/91 in 43.15s;
- AV25.1 producer suite: 50 PASS / 2 Windows-appropriate skips in 103.47s;
- AV25.1 independent-validator synthetic suite: 33/33 PASS in 294.94s.  This
  was not another invocation of the terminal validator CLI;
- exact semantic production `--preflight-only` with explicit npm Codex binary
  exited 0 in 172.7s: `preflight_passed`, production pins loaded, coverage
  preflight PASS, prior v2.3.1 combined57 authenticated, v2.4 FAIL lineage
  preserved, network 0, Vision 0, output created false, output paths `{}`;
- the explicit binary is `codex-cli 0.147.0`, 298,668,336 bytes, SHA-256
  `935a1911ed2556e4ffcec995f4886ac2ac425863ba26fed264df62e30272ad9d`.
  The auto-discovered Desktop binary is older and is not permitted.

Post-freeze source identities are:

- writer sidecar 254,526 bytes,
  `28590cf2dd04395d872d396ddd9572b3342613c14d4822f8b1b2c4ca1adb6e35`;
- independent validator 129,863 bytes,
  `7046daeaf686f470b3fe5365220af27fa0b58db92558741429c63e2779588ebe`;
- production runner 51,165 bytes,
  `84a5e41f27ee36ebf807a799fb72901fb84962b745d0ad42b234a7eb5a3132c3`;
- independent validator CLI 11,061 bytes,
  `75f8420206956a17d44371916017bdecac503c101eebc68fcdd4d05f309711b8`;
- blind-review renderer 77,064 bytes,
  `ab5ea8e8e8afb0941adde6f41d19144975ae89b6bdad5ff2e67bbe4aaa5e1290`.

Every semantic DB/partial/report/partial/review-cache/review-sheets path was
absent both before and after the production preflight.  No paid semantic call
has run.  The actual 56-occurrence population still forms 12 first-attempt
batches (11x5 + 1x1), with at most two strict-invalid selective-retry
reservations and cap 14.  Keep the conservative previously derived approval
envelope of approximately 269,411--276,268 expected non-double-counted tokens
and 311,856 compound upper, with observed stops 300,000 input / 15,000 output /
312,000 total and a 25,000 input + 2,000 output next-call reserve before every
reservation.  The first paid call requires the user's explicit approval after
this line is presented.  N100/full remain prohibited.

## 2026-08-18 semantic v2.5 paid N10 and first-unblind result

The user explicitly approved the bounded semantic N10 after the account and
quota were re-audited.  Canonical auth, active index, pinned index, and every
model-family index all resolved to the same ProLite account.  Immediately
before launch the weekly window reported 46% used / 54% remaining, no credit
fallback, and reset at 2026-08-20 13:36 KST.

The exact production no-model preflight was rerun with the pinned npm
`codex-cli 0.147.0` binary.  It exited 0 in 209.7 seconds with production pins
loaded, coverage PASS, network 0, Vision 0, output created false, and output
paths `{}`.  The fresh paid command then ran once without `--resume`:

```powershell
& .\.venv\Scripts\python.exe -B .\tools\run_cross_source_semantic_vision_n10_v25.py --codex-bin 'C:\Users\user\AppData\Roaming\npm\node_modules\@openai\codex\node_modules\@openai\codex-win32-x64\vendor\x86_64-pc-windows-msvc\bin\codex.exe'
```

The writer exited 0 after 884.7 seconds.  Run
`semn10v25-60fd1966784f8dc678c54552` is `complete`: exact 10 buildings / 56
occurrences, 56 successful results, 12 first-attempt model reservations and
attempts, zero retry reservations, zero semantic HTTP/fetch calls, and unknown
usage 0.  Observed usage is 255,095 input tokens, 77,056 cached input tokens
(a subset, not added again), and 10,341 output tokens, or 265,436
non-double-counted tokens.  This is 46,420 below the approved 311,856 compound
upper bound.  The two optional selective retries were unused.

The runner's mandatory terminal validation passed.  The independently pinned
standalone validator CLI was then invoked exactly once; it exited 0 in 192.7
seconds with failed error checks `[]`, exact 10/56/56 accounting, 12 model
reservations, input immutability PASS, network/LLM/Vision 0, and the following
terminal identities:

- semantic DB 1,323,008 bytes, byte/logical SHA-256
  `00a9a120a14c41e7e67f791bd13e17a7426b1268655453a537a44b1659b0c340` /
  `8262be8a69b013f98f58a4a651bbab1c85ecebca20f93eb08a9ab518ef05d319`;
- report 703 bytes, SHA-256
  `6495d365ff9808f768a38ff5a1bb6ec627664dcd568ca53b635115d1eba2727c`;
- review cache 56 files / 8,923,865 bytes, tree SHA-256
  `7d89c90ac47e520f2dffafe0dfa54888e1a9a0706543021d27f0458bd4ade2f0`.

All ten terminal DB/report/cache/status/result/success/link pins were installed
atomically into the blind renderer.  A `complete_with_failures` status or any
non-56 terminal count is rejected by regression tests.  The persistent one-byte
semantic `.db.lock` is intentional advisory-lock inode reuse evidence and was
not deleted.  DB WAL/SHM/journal and partial outputs are absent.

The production renderer exited 0 in 209.5 seconds with network/Vision 0 and
created a new-only opaque package of ten sheets / 56 panels.  Its manifest is
36,859 bytes, byte/self SHA-256
`2cae63aa191ab2f776bc18c5a35a43557de41f5005d3ffbe05d62c47916e3680` /
`c131e0cb609afd5a39f288610b5a7cfe097bb16a66249eed849e63e3c84fd9ec`.
Two fresh isolated reviewers saw only the ten original-detail sheets, their
own blank draft, and the opaque binding.  Both completed all 56 rows before
peer exchange with `peer_ledger_read=false`, `semantic_labels_read=false`, and
`model_results_read=false`; both official freeze validators passed:

- reviewer A: 44,063 bytes, byte/ledger SHA-256
  `c2eedde2de4a8430084d52ae358f897aa3db542c675cae300e887a3aad583942` /
  `1ea93286b2b44adac44dd149a0b731c9939609f066c9c9fa15d4cae143b6ca1f`;
- reviewer B: 43,771 bytes, byte/ledger SHA-256
  `210b290dd0bac661ab6a9afc65e7304c3b2dc0ef656a40f4f11d0653ef6f073e` /
  `165be14ea191fc4cce9108903456af0832acfba76ffeb89de38d344fec28eaf2`.

A third fresh pixel-only reconciler reviewed all ten sheets and both frozen
ledgers.  It resolved 22 disagreement rows / 68 material dimensions with
pixel-grounded reasons before unblinding, leaving unresolved 0.  The official
reconciled reference is 65,977 bytes, byte/ledger SHA-256
`d9611a1024e082a043cf8458e6221e2c12fbf14ffe733ce993e95fcd60926c6c` /
`d6f347295603c032add47ed3a98bc363bec188f67666e318df848d5131c75bd8`.

The first-unblind inspector was executed exactly once.  Its no-clobber output
`data/reports/divisare_architizer_semantic_vision_n10_v2_5_inspection.json`
is 21,746 bytes, byte/self SHA-256
`373d246ffd9adc36ed2805c191334a19220bfd041ef169af389587bed4dc9c9c` /
`2ea2d4a6219e13f336863f0a70b73aa13fb54e90a59c26859341a30e8964e8dc`.
All technical pregates passed: exact 10/56 population, 56/56 results, missing
0, reconciled independent references, SQLite integrity/quick `ok`, FK 0, DB
byte identity unchanged, sidecars absent, and inspection DB writes/network/
LLM/Vision 0.

The hard quality verdict is **FAIL**:

- PASS: scope clear errors 0;
- FAIL: medium clear errors 5;
- PASS: aggregate nine-field acceptable agreement 461/504 = 91.4683%;
- FAIL: critical hero false positives 3 (`semv25_000028`, `semv25_000046`,
  `semv25_000042`);
- PASS: usable-hero retention 36/37 = 97.2973%;
- PASS: uncertainty overflow, predicted rows 12 versus reference rows 13 and
  frozen cap 19;
- FAIL: uncertainty row precision/recall 0.50 / 0.461538 (recall below 0.50).
  Aggregate-axis recall 0.428571 passes its 0.40 floor and
  resolution-insufficient false negatives are 0, but the row-recall failure
  makes the combined uncertainty gate fail.

Post-pin verification compiled the writer/validator/runner/renderer/inspector/
reconciler sources and passed the final semantic suite 95/95 in 42.38 seconds.
The earlier default pytest temp-root attempt had 52 passes and 39 setup errors
from sandbox-denied `AppData\Local\Temp`; repository-local isolated basetemps
were used for the successful reruns and all three task-owned temp directories
were deleted after verification.  Final changed-source identities include:

- renderer 77,713 bytes, SHA-256
  `80db492c786bc28c2b17cbd3fdfcd42e974c73652812619eca5427d74ed79c01`;
- inspector 45,983 bytes, SHA-256
  `a5eef80600b7c2de3bc92b1063695074ed4816a16e6a2758e759780451b5efc5`;
- reconciler 26,849 bytes, SHA-256
  `dfff8ca78790c9115323f8806d3e0ef8a0178f739b6bf04860284f940f6ce4c5`;
- CLI tests 17,697 bytes, SHA-256
  `547a6676933da0a52f249835d9e211aeccae6c73baf7ea2d7d349aefade5223f`;
- review tests 55,397 bytes, SHA-256
  `f5f496809f74e3240fa69935f02898ed4d144799c47a5ed93e007af47ff86e91`.

The post-QA account sample reports ProLite weekly 54% used / 46% remaining,
reset 2026-08-20 13:36 KST.  The semantic pipeline remains exactly 12 paid
calls; rendering, blind review, reconciliation, and first-unblind added no
pipeline HTTP/LLM/Vision calls.  This v2.5 cohort and reference are now frozen
and must not be reused to judge a retuned prompt.  N100/full were not run and
remain prohibited.  Any v2.6 attempt requires a new result-blind disjoint N10,
a separately frozen prompt/runtime contract, a fresh cost envelope, and new
explicit user approval.
