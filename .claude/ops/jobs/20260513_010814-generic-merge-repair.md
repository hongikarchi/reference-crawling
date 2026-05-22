# Job: generic-merge-repair

created: 2026-05-13 01:08:14
owner: MATCHER
stage: PREUPLOAD-QC
status: qc-pass-user-upload-gate

## Scope

write_scope: reports and proposed repair scripts only; no data/id_registry_*.json mutation without explicit approval
input: data/reports/canonical_v2_generic_merge_audit_full.json, data/canonical/canonical_buildings_strict.json, source DBs
output: approved repair plan for code-name/country-conflict overmerge candidates
claude_gate: required

## Goal

Produce a repair plan for remaining generic/code-name overmerge candidates
before upload. Start with high-confidence code-name conflicts, then triage
country conflicts separately. Do not mutate registries or canonical data until
the repair plan is explicit and approved.

## Smoke Ladder

This job is currently in analysis mode. Repair execution will need its own
smoke ladder once the exact write target is chosen.

### N=10

- command: `python3 tools/canonical_v2_generic_merge_audit.py --report data/reports/canonical_v2_generic_merge_audit_full.json --max-findings 3000`
- schema verdict: BLOCK, by design
- sample quality: PASS; audit caught real code-name conflicts and some country-normalization noise
- tokens/cid: 0
- failure rate: 14 code-name candidates, 68 normalized country-conflict candidates
- decision: repair code-name candidates first; do not auto-split broad country conflicts

### N=100

- command: `python3 tools/canonical_v2_split_preview.py --report data/reports/canonical_v2_code_name_split_preview.json`
- schema verdict: READY
- sample quality: PASS; 14 candidates produce deterministic split groups
- tokens/cid: 0 for deterministic repair; LLM only if semantic spot-check is requested
- projected full cost: 0 until Claude Gate / LLM enrichment rerun
- failure rate: 0 source_refs lost, 0 source_refs duplicated
- decision: ready for user approval before mutation

### Full

- approval: required before any `data/id_registry_*.json` or canonical artifact mutation
- command: pending approval
- run record: pending approval
- monitor cadence: one-shot deterministic checks + strict QC rerun
- abort condition: repair creates duplicate PK, loses source_refs, or requires LLM enrichment without quota check

## Abort Conditions

- Schema mismatch.
- Unexpected writes outside write_scope.
- Cost projection exceeds approved budget.
- Failure rate or sample quality fails the stage-specific gate.
- User approval required but missing.
- Weekly quota at or below 10% before any LLM/agent-heavy stage.

## Notes

- Keep logs in `logs/`; link paths here instead of pasting full logs.
- Add handoff lines to `.claude/Task.md` only for state transitions.
- Current preupload summary: `data/reports/canonical_v2_preupload_qc.md`.
- High-priority code-name candidates include House T/S/O, Nika Cosmetics 1/2,
  Apartment B/S, West Point 1000/4000, P533/P539, and similar series-name
  collisions.
- Country-conflict candidates are not automatically false merges because many
  are source typos or aliases. They need normalization/triage before any split.
- Split preview: `data/reports/canonical_v2_code_name_split_preview.json`.
  Planned new canonicals: 16. Source refs lost/duplicated: 0/0.
- Impact report: `data/reports/canonical_v2_split_impact.md`.
- Applied report: `data/reports/canonical_v2_code_name_split_apply_report.json`.
  Backup: `data/backups/code_name_split_20260513_013027/`.
- Downstream refresh dir:
  `data/canonical/code_name_split_refresh/`.
- Affected refresh complete:
  - D-1 30/30
  - E-1 30/30 deterministic, 841 images
  - E-2 30/30 Vision
  - D-2 30/30 Vision
- Patched strict:
  `data/canonical/code_name_split_refresh/canonical_buildings_strict.patched.json`.
  Coverage: D1/E1/E2/D2 all 39,775/39,775.
- Patched embedded:
  `data/canonical/code_name_split_refresh/canonical_buildings_strict_embedded.patched.json`.
  Embeddings copied: 39,745. Encoded: 30. Missing: 0.
- Current validation:
  - strict QC: WARN only `image_derived`
  - upload dry-run: PASS, 39,776 unique PK, 0 failures
  - generic merge audit final: PASS, 67 country-conflict flags documented,
    review_required=0
- Country conflict triage:
  `data/reports/canonical_v2_country_conflict_triage.json`.
  Summary: 35 likely source country-field noise, 33 semantic-review needed.
- Country-conflict false merge applied:
  `bld_018178` Office Building split into France/Muoto and
  Netherlands/B+O Architects rows.
- Second backup:
  `data/backups/code_name_split_20260513_022108/`.
- Final staged refresh dir:
  `data/canonical/country_conflict_refresh/`.
- Final staged embedded artifact:
  `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.patched.json`.
- Updated country conflict triage:
  `data/reports/canonical_v2_country_conflict_triage_after_country_split.json`.
  Summary: 64 image-supported country noise/alias rows, 3 no-image-link
  semantic-review rows (`M HOUSE`, `Creek House`, `Rost Villa`).
- Evidence-aware audit command:
  `python3 tools/canonical_v2_generic_merge_audit.py --input data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.patched.json --e1 data/canonical/country_conflict_refresh/e1_clusters.patched.jsonl --waivers data/reports/canonical_v2_country_conflict_waivers.json --report data/reports/canonical_v2_generic_merge_audit_final_with_evidence.json --max-findings 4000`
- No live upload performed.
