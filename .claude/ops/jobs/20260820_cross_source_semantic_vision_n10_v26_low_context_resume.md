# Cross-source semantic Vision N10 v2.6 low-context resume

Status: `COMPLETE_NO_GO`

## Objective

Finish and run the frozen v2.6 N10 through strict blind QA without repeating
the prior full-history, multi-agent quota spike.  N100 and full are excluded.

## Agent and quota policy

- Root owns integration, production commands, evidence, and stop decisions.
- At most one delegated implementation task runs at a time.
- Delegation uses `fork_turns=none` with a compact written contract; no agent
  receives the full conversation history.
- Mechanical implementation uses GPT-5.6 Terra.  Sol is reserved for the
  bounded semantic inference and pixel QA where quality matters.
- No new paid N10 starts at or below 20% weekly remaining; stop all work at or
  below 15%.  These guard bands account for coarse/lagged percentage updates.
- Paid semantic envelope remains 14 reservations and 311,856
  non-double-counted tokens, with the existing prospective reserve guard.

## Resume state

- 2026-08-20 20:57 KST: ProLite weekly usage reset to 0%, remaining 100%,
  next reset 2026-08-27 20:57 KST, no credit fallback.
- Frozen v2.6 HTTP census remains terminal: 10 buildings / 56 selected
  occurrences, zero semantic/Vision exposure.
- The 2026-08-18 interrupted semantic files are untrusted until individually
  compiled, tested, and reconciled.  Initial audit found a literal tool-output
  truncation marker in the sidecar test and missing validator CLI/test.
- Existing v2.6 source modules compile, but compile alone is not a release
  signal; stale lineage/domain literals remain under audit.

## Release sequence

1. Sequentially repair writer tests/contracts, independent validator/CLI,
   then blind-QA surfaces.
2. Reconcile writer-validator-renderer pins and run combined compile/focused
   regression plus exact no-model production preflight.
3. Recheck canonical account and live quota, then run one fresh paid N10.
4. Validate terminal artifacts independently, install terminal renderer pins,
   render opaque sheets, freeze two isolated pixel-only ledgers, reconcile,
   and perform exactly one first-unblind inspection.

## 2026-08-20 offline repair checkpoint

- Delegation remained sequential and low-context.  The writer repair and
  validator repair each used one `fork_turns=none` Terra agent; neither made a
  network, Codex, Vision, or paid semantic call.
- Root independently corrected the production selection to the terminal AV26
  facts: 106 exact / 14 source-changed, 10 buildings / 56 occurrences, and 30
  prior-run names through `semantic_n10_v2_5`.
- The v2.6 prompt/runtime contract now self-replays to run
  `semn10v26-617639dd259f16d61c301656`, dependency SHA
  `355e8410ac004da91828c17add8f72956de14b44dbebeecbca8a9e3c055fb090`,
  schema SQL SHA
  `05c51f55f6d8e399a9e1c43f8bd41dde9af1867bcb0046e058ce34793ba70b94`,
  and schema-object SHA
  `66037e2372412e8496eaf8612946ba0dc2554a39a3c889306336c49075ad0780`.
- The 56-file review-cache projection was independently replayed from the
  frozen manifest and AV26 cache: 10,723,411 bytes, ordered manifest SHA
  `4c739368383cefe95afab668e8764b13a69af7a6fbb01ef73ebd7d5f8f642058`,
  with zero selected-derivative byte mismatches.  Projected tree SHA is
  `c269e433460cd3b56ef11cd7f0b6f4de4f2655ef4a5c5c044e177e0abc2a6d62`.
- Frozen v2.5 technical + blind-QA lineage independently replays to contract
  SHA `ea34c78b720b75d036fa8ad8d7bf818ef5de6f97d94ebe1ca60ce848962407d5`.
- Focused offline results independently rerun by root: writer/sidecar 22/22
  PASS; semantic validator 4/4 PASS; all seven v2.6 runtime/review modules
  compile.  No v2.6 semantic output path exists.
- Latest observed shared weekly telemetry after the two small agents was 6%
  used / 94% remaining.  Canonical credentials exactly matched the enabled,
  active, pinned multi-auth slot; secrets were not printed.

## 2026-08-20 semantic terminal checkpoint

- Combined v2.6 offline regression: 115/115 PASS in 118.23 seconds.
- Exact production `--preflight-only`: PASS in 109.2 seconds; Codex CLI
  `0.147.0`; coverage and all immutable lineage gates PASS; network/Vision 0;
  protected semantic output paths present 0.
- One fresh, non-resume paid N10 completed in 483.5 seconds.  Run
  `semn10v26-617639dd259f16d61c301656`; 10 buildings / 56 occurrences;
  12 reservations/attempts; 56 successes; network/fetch 0; no selective
  retry.  Observed input/output tokens were 266,961 / 9,676, for 276,637
  non-double-counted tokens; cached input 44,032 is a subset and is not added
  again.  The run stayed below the 311,856 cap and all three observed stops.
- Runner-integrated terminal validation PASS.  The separately invoked
  standalone validator then passed 41/41 read-only checks exactly once with
  network/Vision/LLM 0.  Terminal logical SHA is
  `9be4794193092ec77f3bc9f4736ae52a033ed3e47968d51ef75b5bc72ed61d5c`.
- Atomic terminal renderer pins installed from independently recomputed
  artifacts: semantic DB 1,310,720 bytes / byte SHA
  `70eaf1b8097ce472bc01bd5ae8062b9a81234c538dfc510c1a48f546271b1b11`;
  report 702 bytes / SHA
  `fbc84182f832a7ddeca75ec948350e52eaf1642ba03c62ca12592c9ddbbda680`;
  review cache 56 files / 10,723,411 bytes / tree SHA
  `c269e433460cd3b56ef11cd7f0b6f4de4f2655ef4a5c5c044e177e0abc2a6d62`;
  status complete and result/success/link 56/56/56.  Post-pin CLI/review tests
  passed 45/45.
- Opaque renderer passed with network/Vision 0 and created 10 sheets for 56
  inputs.  Review manifest self SHA is
  `edd0bc9fe54a0ce586b294c152cd9a7df1ec0d88fb725c3ab686a1985d4477ad`;
  opaque order SHA is
  `9e134ae262cdc1cc908a6ab6fade0505f8a088f875141beff509f9b80458075e`.

## 2026-08-20 blind-QA terminal checkpoint

- Two isolated `fork_turns=none` pixel-only reviewers each inspected all 10
  sheets / 56 rows and produced official frozen ledgers before peer exchange
  or semantic/model unblinding.  A/B frozen byte SHA-256 values are
  `9a2b0063325a7afca546d66bf25d75e6c8cf3efa92c5bcbd61ab3f38972f9081`
  and
  `23e96d4d806f1a0d029f2195cc0777257d3bf74862b8ec6c7a5807a3241cec6e`.
- A third isolated pixel-only reconciler reviewed all 38 disagreement rows and
  resolved all 121 disagreement dimensions.  The official reconciled ledger
  has unresolved count 0, byte SHA-256
  `d4cc138952a7f1e2c34c3efa1c8ed81ac5debe327908bc95db7c425933d3a569`,
  and canonical ledger SHA-256
  `60b84cd93ebf5b4c8079aefa32d106ad4cc887ad8c179c858a2639a03d4e56e0`.
- Exactly one first-unblind inspection was executed.  The inspection is
  22,417 bytes, byte SHA-256
  `ec0c3f1b049d80fd69799eb5310a2c9f8acc49bd743cace126ca4b1f1058b8bf`,
  canonical inspection SHA-256
  `2de471982226b84bd6b218ad5a2c157050adcb94e0779c6d96af084d9091ea47`,
  and verdict `FAIL`.
- Accounting and technical gates passed: exact 10 buildings / 56 occurrences,
  result coverage 56/56, missing results 0, nine-field acceptable agreement
  461/504 = 91.4683%, predicted uncertainty 4 <= overflow cap 23, DB integrity
  and immutable before/after identity PASS, and unresolved disagreements 0.
- Quality gates that failed: scope clear errors 3; medium clear errors 2;
  critical hero false positives 1 (`semv26_000032`); usable-hero retention
  32/37 = 86.4865% (<90%); uncertainty row precision 3/4 = 75% but recall
  3/17 = 17.6471%; aggregate-axis recall 2/21 = 9.5238%.
- Inspection operations were DB writes 0, network 0, Vision 0, and LLM 0.
  The semantic DB remained 1,310,720 bytes with byte SHA-256
  `70eaf1b8097ce472bc01bd5ae8062b9a81234c538dfc510c1a48f546271b1b11`
  and no WAL/SHM/journal before or after inspection.
- This v2.6 population and all blind artifacts are frozen as failed evidence.
  N100/full remain prohibited.  A subsequent paid N10 would require a new
  prompt/version, a fresh-disjoint population, newly frozen cost/quota gates,
  and a new explicit cycle scope; this run must not be overwritten or rerun.
