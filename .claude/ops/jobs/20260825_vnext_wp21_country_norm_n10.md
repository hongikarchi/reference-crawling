# WP2.1 country normalization + identity candidates v2 N10 (2026-08-25)

## Scope / decision

- Order: `.claude/ops/jobs/20260825_vnext_wp21_country_norm_order.md`
- Run ID: `wp21-b8bc8a26c398439afe32463c`
- Result: **PASS**
- N100: **not run**. Wait for a separately issued sample design after review of the v2 distribution.
- Merge execution / `vnx_*` ID issue / registry / Neon / R2: all **0**.

## Contract and implementation

- `docs/vnext/CONTRACT_IDENTITY.md` §3 now states that country conflict is emitted only when both endpoints normalize to known canonical countries and differ.
- Unknown or ambiguous values are neutral: neither positive nor conflict. `Georgia` is the fixed ambiguous overlay.
- Contract bytes/SHA: `6,855` / `119317831186368c078c121d810dda6f866ae5ab32df971093ad61b50aa788d2`.
- Added `canonical/vnext_country_norm.py` with frozen C17/C21 alias/normalizer lineage and the C23 known-country set copied locally. Production has no legacy-script runtime import.
- Legacy provenance is SHA-pinned for:
  - `tools/build_strict_canonical.py`
  - `tools/canonical_v2_c17_make_web_polish.py`
  - `tools/canonical_v2_c21_make_web_polish.py`
  - `tools/canonical_v2_c23_final.py`
- Added v2 producer, compact transition reporter, independent validator, CLIs, and WP2.1 tests.
- The independent validator uses the pinned legacy C21/C23 implementation only as QA reference, reconstructs country classification and all candidate decisions, and does not import the v2 producer.

## Stage A replay

- Inputs: pinned Divisare/Architizer curated DBs, E2 candidate/evidence DB, matcher files, v1 artifact as comparison-only input, and the pinned country lineage.
- Network / Vision / LLM / paid API: `0 / 0 / 0 / 0`.
- Source buildings: `91,803`.
- Candidate pairs: `68,049` (same total as v1).
- Six-family rows: `408,294`.
- Country classification:
  - known `37,208`
  - unknown `54,535`
  - ambiguous `60`
- v1 reviewed-pair overlap in fresh N10: `0`.
- Fresh N10 strata: state transition 2, strong merge 2, metadata-only 2, image-evidence-only 2, conflict-boundary 2; all 20 endpoints are distinct.

### Attempt history

1. First build stopped at sample insertion because the inherited v1 schema CHECK did not allow `state_transition`.
   - Partial DB was preserved, not deleted: `data/enrichment/vnext_identity_candidates_v2.db.building.failed`
   - bytes/SHA: `204,800` / `4e9511b5c4f54009a7d12499a278d0bb164a0a2f0d5445d77134b74cf300ee78`
   - FAIL report: `data/reports/vnext_identity_candidates_v2_attempt1_fail.json`
2. The v2 schema was made explicit and the full Stage A replay then passed.

## v1 -> v2 transition accounting

The initial Stage A report used `changed_transition_count=67,907` for any lineage change, including canonical country representation changes. That is not the proposal-state transition count. The original report/artifact remains immutable; the corrected compact report separates the metrics:

- Proposal-state transitions: **10**
  - `reject -> merge_candidate`: **8**
  - `reject -> defer`: **2**
- Decision/origin lineage changes: **60**
- Country representation-only changes: **67,847**
- No proposal change: `68,039`.

Proposal distribution:

| Proposal | v1 | v2 | Delta |
|---|---:|---:|---:|
| merge_candidate | 4,677 | 4,685 | +8 |
| defer | 2,222 | 2,224 | +2 |
| reject | 61,029 | 61,019 | -10 |
| related_but_separate | 121 | 121 | 0 |

Both designated Turkey/Türkiye regression pairs changed exactly from `reject` with country conflict to `merge_candidate` with no country conflict.

Canonical compact report: `data/reports/vnext_identity_candidates_v2_transition_report.json`.

## Stage B HTTP and blind review

### Connection-blocked attempt

- First sandboxed HTTP attempt: selected 36, connection failures 36, `WinError 10013`.
- The generic runner recorded these as terminal failed assets and rendered unusable placeholder sheets. Those outputs were not reviewed.
- The full failed rows and sheet lineage were archived inside the v2 DB in the append-only `stage_b_retry_*` ledger before requeueing. Existing failed-attempt files were preserved.
- Requeued assets: 36; archived attempt: 1.

### Authorized retry

- Selected assets: 36 (maximum bound 40).
- Logical HTTP fetches: 36; HTTP request count: 36.
- Current pixel results: exact 31, source_changed 5, failed 0.
- Review sheets: 10.
- Cache hits: 0.
- Vision / LLM / paid API: `0 / 0 / 0`.
- Across both attempts: 72 logical attempts = 36 sandbox-blocked connections + 36 authorized retry fetches.

### Review and adjudication

- Blind reviewer saw only opaque sheets; proposal, scores, rank, stratum, origins, candidate ID, and auto-merge flag were hidden.
- Blind labels: `same=10`.
- After review lock, adjudication labels: `same=10`; reviewer/adjudicator disagreements: `0`.
- The two state-transition samples were both true same-building pairs and v2 proposed `merge_candidate` with no hard conflict.
- Additional false-reject signals found after unblinding:
  - 2 same-building pairs blocked by year/matcher gates (`reject` or `related_but_separate`)
  - 1 same-building pair rejected by verified-pHash veto despite exact metadata and visually consistent non-overlapping views
  - These are not WP2.1 false-merge gate failures, but must inform the separately designed N100 sample.

## Gate and validation

- Lane-C gate: **PASS**
  - reviewer `different` intersect auto-merge eligible: `0`
  - auto-merge eligible with hard conflict: `0`
  - review/adjudication disagreement: `0`
  - merge execution / ID issue: `0 / 0`
- Tests: `18 passed`; only the pre-existing `.pytest_cache` ACL warning remained.
- Independent production validation: **97/97 PASS**, with full input SHA verification enabled.
- Validator checks include:
  - all `91,803` country normalization rows and independent C21/C23 equivalence
  - known-only country conflict and location-positive invariants
  - all `68,049` candidate decisions, six-family rows, origins, conflicts, and image evidence
  - all v1→v2 transition rows and record hashes
  - v1 exclusion/disjoint N10 and deterministic fresh sample replay
  - Stage A and v2 full logical hashes
  - retry ledger, E1/E2 lineage, cache/HTTP accounting, sheet SHA, blind form, review/adjudication locks, and gate report
  - v1 artifact SHA preservation and N100 run count 0

## Main artifacts

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| `data/enrichment/vnext_identity_candidates_v1.db` | 511,156,224 | `0541b4c86bb2def88bbadaf53eec33c62d8f14e62800f1ceb8d96761f3aea5c4` |
| `data/enrichment/vnext_identity_candidates_v2.db` | 581,292,032 | `08e28e4e5a1ab5aba9ea5ddd53ca615f417458a6693cdfb1aefc822d57b5b0c4` |
| `data/reports/vnext_identity_candidates_v2_manifest.json` | 17,940 | `24c11769fb1cadc93a1be8aa562cede550d507cbcebc52a99b97983e12939912` |
| `data/reports/vnext_identity_candidates_v2_stage_a_report.json` | 59,104,981 | `33484db5a3e9ff70b86f35d45ac6ab7602846706b49e488ba2f94828fd228683` |
| `data/reports/vnext_identity_candidates_v2_transition_report.json` | 27,031 | `f01ebcd53bc47505f8d436754c369633efe4263c85681d417e037e5d6049027c` |
| `data/reports/vnext_identity_candidates_v2_validation.json` | 6,699,593 | `56648353225a42db81c37ba44e264317475a35b67f472280887d3efe3bc1907b` |
| `data/reports/vnext_identity_candidates_v2_n10_gate.json` | 499 | `c6be7da600d4e1d32daf53545174c4ee53e2a2d96c4bba5547a30b8a8ff062b2` |

Stage A logical SHA: `460ef6b941fb6513844026b4d6ca6bc913f36eaa8ef84f97749d16795527b4db`.

v2 full logical SHA: `e11c0e4fb69d69b67661bae0467e8e946aef011d585ac31d4ed782dad6ae0ff7`.

## Stop condition / next action

WP2.1 is complete. Do not run N100 automatically. Review the +8 `merge_candidate` distribution and the three N10 false-reject signals, then issue a separate N100 sampling order.
