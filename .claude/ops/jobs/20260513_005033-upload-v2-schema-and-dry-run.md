# Job: upload-v2-schema-and-dry-run

created: 2026-05-13 00:50:33
owner: UPLOAD
stage: UPLOAD-V2
status: blocked

## Scope

write_scope: tools/ dry-run helpers and data/reports only; do not modify upload/ or run upload/*.py without explicit user approval
input: data/canonical/canonical_buildings_strict_embedded.json, data/reports/upload_strict_gaps.md
output: canonical_v2_buildings schema proposal + dry-run upload validation
claude_gate: required

## Goal

Design the U3 upload path for the completed v2 canonical dataset without
touching production data: propose `canonical_v2_buildings`, validate mappings
against N=10/N=100/full dry-runs, then stop at the user approval gate.

## Plan

1. Write `data/reports/canonical_v2_schema.md`.
   - Define columns, types, nullable policy, JSONB fields, vector dimensions,
     and indexes.
   - Include compatibility notes for make_web cutover.
2. Add a read-only validator under `tools/`, not `upload/`.
   - It reads `data/canonical/canonical_buildings_strict_embedded.json`.
   - It maps rows into the proposed table shape.
   - It validates required fields, vector length 384, JSON serializability,
     image fields, source refs, and primary key uniqueness.
   - It never opens a Neon/R2 connection.
3. Run N=10 and N=100 dry-runs.
   - Save compact reports in `data/reports/`.
   - Check representative rows manually for schema mapping quality.
4. Run full dry-run only after N=10 and N=100 pass.
   - Output count must be 39,759.
   - Missing critical fields must be zero except explicitly nullable fields.
5. Create a Claude Gate packet once Claude quota is available.
   - Question: does the U3 schema preserve the canonical data needed by
     make_web without collapsing back into legacy metalocus IDs?
6. Stop before live upload.
   - Actual Neon schema creation, upsert, R2 upload, or make_web cutover is a
     separate user-approved job.

## Smoke Ladder

### N=10

- command: `python3 tools/canonical_v2_upload_validator.py --limit 10 --report data/reports/canonical_v2_upload_dry_run_n10.json`
- schema verdict: PASS
- sample quality: PASS for upload shape; metadata warnings only
- tokens/cid: 0
- failure rate: 0 upload-blocking failures
- decision: continue to N=100

### N=100

- command: `python3 tools/canonical_v2_upload_validator.py --limit 100 --report data/reports/canonical_v2_upload_dry_run_n100.json`
- schema verdict: PASS
- sample quality: PASS for upload shape; metadata warnings only
- tokens/cid: 0
- projected full cost: 0 LLM tokens; local CPU/file IO only
- failure rate: 0 upload-blocking failures
- decision: continue to full dry-run

### Full

- approval: allowed for dry-run only; live DB/R2 approval is separate
- command: `python3 tools/canonical_v2_upload_validator.py --report data/reports/canonical_v2_upload_dry_run_full.json`
- run record: `.claude/ops/runs/20260513_010117-upload-v2-dry-run-full.md`
- monitor cadence: one-shot
- abort condition: row count != 39,759, duplicate PK, bad vector, invalid JSON,
  or unexpected required-field loss

Result: structural upload dry-run PASS, 39,759 rows, 39,759 unique PKs, 0
upload-blocking failures.

## Abort Conditions

- Schema mismatch.
- Unexpected writes outside write_scope.
- Cost projection exceeds approved budget.
- Failure rate or sample quality fails the stage-specific gate.
- User approval required but missing.
- Any attempt to modify `upload/` or run `upload/*.py` before explicit user
  approval.

## Notes

- Keep logs in `logs/`; link paths here instead of pasting full logs.
- Add handoff lines to `.claude/Task.md` only for state transitions.
- This job assumes U3 from `data/reports/upload_strict_gaps.md` because U1
  drops most non-metalocus canonicals and U2 mutates the legacy production
  table. User can still override before live work begins.
- Blocked after dry-run by `canonical_v2_generic_merge_audit.py`: remaining
  generic/code-name overmerge candidates found. See
  `data/reports/canonical_v2_preupload_qc.md`.
