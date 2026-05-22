# Job: post-claude-recovery

created: 2026-05-13 00:49:32
owner: CONTROL
stage: RECOVERY
status: done

## Scope

write_scope: .claude/ops/jobs, .claude/REPORT.md
input: .claude/ops/snapshots/20260512_233312, data/canonical, logs
output: recovery status and next upload plan
claude_gate: required

## Goal

Recover the real pipeline state after the Claude quota stop, separate stale
resume instructions from current artifacts, and identify the next actionable
DB step.

## Smoke Ladder

Not applicable. This was a read-only recovery/QC job and made no LLM batch
calls.

### N=10

- command: not run
- schema verdict: N/A
- sample quality: N/A
- tokens/cid: 0
- failure rate: N/A
- decision: use deterministic artifact checks instead

### N=100

- command: not run
- schema verdict: N/A
- sample quality: N/A
- tokens/cid: 0
- projected full cost: 0
- failure rate: N/A
- decision: use deterministic artifact checks instead

### Full

- approval: not required for read-only verification
- command: `python3 tools/qc_strict.py --input data/canonical/canonical_buildings_strict.json`
- run record: terminal output in current Codex session
- monitor cadence: one-shot
- abort condition: any FAIL in strict QC or mismatched row counts

## Abort Conditions

- Schema mismatch.
- Unexpected writes outside write_scope.
- Cost projection exceeds approved budget.
- Failure rate or sample quality fails the stage-specific gate.
- User approval required but missing.

## Notes

- Root cause of confusion: `.claude/REPORT.md` and parts of `.claude/Task.md`
  were stale and still described D-1 as paused, while later Claude terminal
  snapshot and data artifacts showed D/F/G completion.
- Snapshot evidence:
  `.claude/ops/snapshots/20260512_233312/DB-MAIN.txt` reports all stages 100%
  after Stage B bug fix + re-enrichment, then stops at upload option choice.
- Verified artifacts:
  - `data/canonical/d1_results.jsonl`: 39,759 lines, 39,759 unique `cid`, 0 bad JSON.
  - `data/canonical/d2_results.jsonl`: 39,759 lines, 39,759 unique `cid`, 0 bad JSON.
  - `data/canonical/e1_clusters.jsonl`: 39,759 lines, 39,759 unique `cid`, 0 bad JSON.
  - `data/canonical/e2_image_types.jsonl`: 39,759 lines, 39,759 unique `cid`, 0 bad JSON.
  - `data/canonical/canonical_buildings_strict.json`: 39,759 `canonical_bld_id`.
  - `data/canonical/canonical_buildings_strict_embedded.json`: 39,759 `canonical_bld_id`, 39,759 `embedding`, no null/empty embedding pattern.
- QC result:
  `tools/qc_strict.py --input data/canonical/canonical_buildings_strict.json`
  returns `OVERALL: WARN`, with only `image_derived` warning. Schema, PK,
  coverage, architect links, vocab, covers, source refs, and architect
  consistency pass.
- Upload gap:
  `data/reports/upload_strict_gaps.md` says current `upload/neon_strict.py`
  targets the old metalocus-centric schema. Recommended path is U3: create a
  fresh `canonical_v2_buildings` table and upload/cut over make_web to it.
- Next job:
  `upload-v2-schema-and-dry-run` should design `canonical_v2_buildings`, write
  a new dry-run upload path, and stay gated before any live Neon/R2 writes.
