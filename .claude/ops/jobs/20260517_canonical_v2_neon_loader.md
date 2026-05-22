# Job: canonical-v2-neon-loader

created: 2026-05-17 KST
owner: DB-CODEX-OPS
stage: UPLOAD-V2
status: complete

## Scope

write_scope: `tools/canonical_v2_neon_loader.py`,
`tools/canonical_v2_upload_validator.py`, `data/reports/`,
`.claude/ops/jobs/`, `.claude/Task.md`

input:

- `data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.resume10_complete.json`

output:

- `tools/canonical_v2_neon_loader.py`
- `data/reports/canonical_v2_neon_schema.sql`

## Goal

Prepare the U3 fresh-table Neon loader for `canonical_v2_buildings` without
touching `upload/` and without committing live Neon/R2 writes unless explicitly
confirmed.

## Cost Arithmetic

No LLM batch work launched.

```
0 cids x (~0 prompt tokens + ~0 output tokens + ~0 codex batch overhead)
= 0 pipeline tokens
projected weekly burn: 0 / 2B = 0%
```

## Modes

Safe local/no-write modes:

```bash
python3 tools/canonical_v2_neon_loader.py --emit-sql
python3 tools/canonical_v2_neon_loader.py --check-env
python3 tools/canonical_v2_neon_loader.py --inspect-table
```

DB transaction smoke mode:

```bash
python3 tools/canonical_v2_neon_loader.py --dry-run-upsert --limit 10
```

Live write modes require `--confirm-db-write`:

```bash
python3 tools/canonical_v2_neon_loader.py --create-table --confirm-db-write
python3 tools/canonical_v2_neon_loader.py --upsert --confirm-db-write
```

## Guardrails

- `upload/` was read for reference only; no files under `upload/` were
  modified or executed.
- Default input is the resume10 complete embedded artifact.
- `--upsert` refuses to run without `--confirm-db-write`.
- `--dry-run-upsert` rolls back the transaction.
- Neon/R2 live writes remain user-gated.

## Next

Run `--check-env`, then an N=10 `--dry-run-upsert` if DB connection env is
available and the user approves opening a Neon connection.

## Smoke Log

- N=10 dry-run attempt 1 opened Neon transaction and rolled back on error:
  existing `canonical_v2_buildings` table was a prior schema without
  `created_at`.
- Root cause: `CREATE TABLE IF NOT EXISTS` does not evolve existing tables.
- Fix: loader now applies additive `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`
  for `cover_image_cdn_url`, `cover_blurhash`, `created_at`, and `updated_at`
  before inserting rows.
- N=10 dry-run attempt 2 PASS: 10 rows mapped/upserted inside transaction,
  0 row mapping failures, ROLLBACK executed. Table count seen in transaction was
  39,776, suggesting Neon may already contain the full resume10 row count.
- Inspect attempt 1 was read-only and reached report generation, then failed
  locally because `datetime` values from `updated_at` were not JSON
  serializable. Fix: report writer now uses `default=str`.
- Inspect attempt 2 PASS read-only:
  - table rows: 39,776
  - unique PKs: 39,776
  - publishable/nonpublishable: 39,737/39
  - `needs_image_derived_backfill`: 23,008
  - `updated_at`: 2026-05-14 00:55:03 UTC
- Interpretation: existing Neon table has the right row count but stale
  pre-resume10 content. It needs final resume10 full upsert.
- N=100 dry-run attempt 1 reached post-upsert count query, then failed locally
  converting a regular psycopg2 tuple row to `dict`. Transaction rolled back.
  Fix: convert `cursor.description` + tuple into a dict explicitly.
- N=100 dry-run attempt 2 PASS:
  - rows loaded in transaction: 100
  - row mapping failures: 0
  - total rows: 39,776
  - unique PKs: 39,776
  - `needs_image_derived_backfill` dropped from 23,008 to 22,913 inside the
    rolled-back transaction, proving final rows are being applied.
  - writes: rolled back
- Full upsert PASS after explicit user approval:
  - command:
    `python3 tools/canonical_v2_neon_loader.py --upsert --confirm-db-write --report data/reports/canonical_v2_neon_loader_upsert_full.json`
  - rows loaded in transaction: 39,776
  - row mapping failures: 0
  - total rows: 39,776
  - unique PKs: 39,776
  - publishable/nonpublishable: 39,736/40
  - missing embedding: 0
  - missing display cover URL: 39
  - `needs_image_derived_backfill`: 0
  - writes: committed

## Completion

Neon `canonical_v2_buildings` now contains the resume10 complete canonical
dataset. R2 mutation and make_web cutover remain separate user-gated jobs.
