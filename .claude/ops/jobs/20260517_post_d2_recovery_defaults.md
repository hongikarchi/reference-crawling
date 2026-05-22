# Job: post-d2-recovery-defaults

created: 2026-05-17 KST
owner: DB-CODEX-OPS
stage: POST-D2-RECOVERY
status: complete

## Scope

write_scope: tools/ defaults and ops handoff metadata only
input: `data/canonical/country_conflict_refresh/canonical_buildings_strict.resume10_complete.json`, `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.resume10_complete.json`
output: tool defaults now point at the D-2 resume10 complete artifact family
claude_gate: not required

## Goal

Recover after workstation shutdown, confirm the durable source of truth, and
move local tooling off superseded `.patched` defaults so future read-only audits
and small refresh helpers target the completed D-2 artifact family by default.

## Recovery Evidence

- `.claude/REPORT.md` active snapshot is updated through 2026-05-17 15:45 KST.
- `.claude/ops/runs/20260516_d2-image-backfill-resume10.md` is `status: complete`.
- Last D-2 completion handoff: `ENRICH-DONE: d2_resume10_complete`.
- Current process check found no live canonical/crawl/enrich/matcher runner.

## Cost Arithmetic

No LLM batch work launched.

```
0 cids x (~0 prompt tokens + ~0 output tokens + ~0 codex batch overhead)
= 0 pipeline tokens
projected weekly burn: 0 / 2B = 0%
```

## Changes

- `tools/audit_canonical_data_integrity.py`
  - default strict and embedded paths now use resume10 complete artifacts.
- `tools/backfill_architizer_architect_registry.py`
  - default strict path now uses the resume10 complete strict artifact.
- `tools/canonical_v2_embed_refresh.py`
  - default refresh directory now uses `country_conflict_refresh`.
  - default input/base/affected paths now use the resume10 complete family.
  - default output writes a non-final `.refresh.json` file to avoid overwriting
    the completed embedded artifact if the helper is run without flags.

## Gates Still Closed

- No live Neon/R2 upload.
- No `upload/` edits or `upload/*.py` execution.
- No deletion of superseded large artifacts.

## Next Action

Ask for explicit approval before either:

- deleting superseded multi-GB artifacts listed in
  `.claude/ops/runs/20260514_disk-cleanup-review.md`; or
- starting the live upload/schema creation job.
