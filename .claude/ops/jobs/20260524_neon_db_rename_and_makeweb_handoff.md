# 2026-05-24 Neon final cleanup — DB rename + role rename + branch prune

## Trigger

Endpoint divergence discovery: make_db has been writing to production branch
(`ep-broad-hat-a1jaomn7`) all along; make_web prod was reading `local-dev`
branch (stale C8, 24 user tables). User decision: do all destructive ops in
make_db side now, hand off to make_web for env+code swap only.

## Phase A — local-dev branch dropped

```bash
neonctl branches delete local-dev --project-id holy-pond-45504245
```

Remaining branches: production + pre-cleanup-2026-05-24.

## Phase A2 — Role rename

```sql
ALTER ROLE makeweb_buildings_ro RENAME TO make_web;
```

Grants intact (SELECT on canonical_v2_buildings + canonical_v2_architects +
ALTER DEFAULT PRIVILEGES). Password unchanged.

## Phase B — DB rename

```sql
-- via postgres DB (not neondb), autocommit
ALTER DATABASE neondb RENAME TO archi_data;
```

Active connections: 0 (verified pre-rename). Snapshot branch
`pre-cleanup-2026-05-24` retains DB name `neondb` (branch independence).

## Phase C — make_db .env + docs update

- `.env` DB_NAME: neondb → archi_data
- 0 hardcoded refs in tools/*.py (env var only)
- Docs updated:
  - CLAUDE.md (production dataset block)
  - docs/REFERENCE.md (§2 + §2b headers)
  - docs/NEON_CLEANUP_RUNBOOK.md
  - docs/MAKEWEB_DB_SWAP_HANDOFF.md (full rewrite as final handoff)
- .gitignore: `.env.makeweb-*` → `.env.*` (already covered, glob removed for clarity)

## Phase D — Verify

- inspect-table (archi_data, role neondb_owner): buildings 39,478 / architects 14,216 ✓
- has_table_privilege('make_web', X, 'SELECT') = True for both KEEP tables
- Other connections to archi_data: 0

## Phase E — Secrets file rename

- `.env.makeweb-buildings-ro` → `.env.make-web` (mode 0600, gitignored)
- Content updated: BUILDINGS_DB_USER=make_web, BUILDINGS_DB_NAME=archi_data
- Password unchanged (role rename preserves it)

## Phase F — Handoff doc

`docs/MAKEWEB_DB_SWAP_HANDOFF.md` rewritten as final handoff:
- What changed (Neon-side already done)
- make_web env vars + code grep instructions
- Verification SQL
- Rollback plan (snapshot branch)
- Schema reference + recommendation algorithm pointers
- Open questions for make_web team

## Final state

Neon archi-tinder project:
- production branch / archi_data DB: 2 tables (canonical_v2_buildings 39,478 +
  canonical_v2_architects 14,216)
- production branch / user_data DB: make_web user tables (unchanged)
- pre-cleanup-2026-05-24 branch: snapshot of pre-cleanup state, 1-week rollback
- Roles: neondb_owner (writer, make_db); make_web (SELECT-only, make_web)

## Pending

- make_web: env swap + code grep + Railway redeploy + verification SQL
- make_db: drop pre-cleanup snapshot after 1 week (if no rollback needed)
- Codex audit re-run on new endpoint state
