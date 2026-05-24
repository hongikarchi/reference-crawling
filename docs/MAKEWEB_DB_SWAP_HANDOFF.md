# make_web BUILDINGS_DB Swap Handoff (2026-05-24)

## Why

Endpoint divergence discovered today:
- **make_db** has been writing to `ep-broad-hat-a1jaomn7` since C8 baseline
  (~Apr/May 2026). All C9-C23 building updates + canonical_v2_architects went here.
- **make_web** prod has been reading a DIFFERENT endpoint (your current
  `BUILDINGS_DB_*`) which is stuck on C8 (39,776 rows, no architects, +24
  legacy user/app tables).

Result: make_web prod is **15+ versions stale** and cannot see the
`canonical_v2_architects` table at all.

Fix: point make_web's `BUILDINGS_DB_*` at the make_db endpoint (this
preserves all C9-C23 work + makes architects available).

## New connection params

| Var | Value |
|---|---|
| `BUILDINGS_DB_HOST` | `ep-broad-hat-a1jaomn7.ap-southeast-1.aws.neon.tech` |
| `BUILDINGS_DB_NAME` | `neondb` |
| `BUILDINGS_DB_USER` | `makeweb_buildings_ro` |
| `BUILDINGS_DB_PASSWORD` | in `make_db/.env.makeweb-buildings-ro` (mode 0600, gitignored) — hand off via 1Password or Neon secret vault, **not** Slack/email plaintext |
| `BUILDINGS_DB_SSLMODE` | `require` |

Composed URL (substitute `<pw>`):
```
BUILDINGS_DB_URL=postgresql://makeweb_buildings_ro:<pw>@ep-broad-hat-a1jaomn7.ap-southeast-1.aws.neon.tech/neondb?sslmode=require
```

## Permissions

`makeweb_buildings_ro` role grants:
- `CONNECT` on `neondb`
- `USAGE` on `public` schema
- `SELECT` on `canonical_v2_buildings`, `canonical_v2_architects`
- `ALTER DEFAULT PRIVILEGES` → future tables in `public` auto-SELECT

No INSERT/UPDATE/DELETE. Writes still flow through make_db's `neondb_owner`.

## Swap procedure

### 1. Local dev
```bash
# make_web local .env
BUILDINGS_DB_URL=postgresql://makeweb_buildings_ro:<pw>@ep-broad-hat-a1jaomn7.ap-southeast-1.aws.neon.tech/neondb?sslmode=require
```

### 2. Railway production env vars
- Open Railway → make_web service → Variables
- Update `BUILDINGS_DB_URL` (or component vars `BUILDINGS_DB_HOST/USER/PASSWORD/NAME`)
- Trigger redeploy

### 3. Verify (run from make_web after swap)
```python
import os, psycopg2
conn = psycopg2.connect(os.environ['BUILDINGS_DB_URL'])
with conn.cursor() as cur:
    cur.execute("SELECT COUNT(*) FROM canonical_v2_buildings")
    print(cur.fetchone())  # expect: (39478,)
    cur.execute("SELECT COUNT(*) FROM canonical_v2_architects")
    print(cur.fetchone())  # expect: (14216,)
    cur.execute("""
        SELECT canonical_arch_id, canonical_name, n_buildings_publishable
        FROM canonical_v2_architects
        WHERE canonical_arch_id = 'arch_000000'
    """)
    print(cur.fetchone())  # expect: ('arch_000000', 'Foster + Partners', 82)
```

Pass criteria:
- buildings: 39,478
- architects: 14,216
- Foster + Partners record exists with 82 publishable buildings

## What happens to the old endpoint

After swap:
- Old endpoint (with 24 legacy user/app tables + stale 39,776 buildings) is
  orphaned — no consumer.
- Options:
  - **Drop the old endpoint entirely** (Neon UI → endpoint settings → delete) — recovers compute/storage cost.
  - **Keep as cold archive** for a week, then delete.
  - **Run cleanup on it** (same DROP SQL from `docs/NEON_CLEANUP_RUNBOOK.md`)
    if you want to preserve it temporarily.

Recommended: keep cold for 1 week as rollback insurance, then drop.

## Rollback

If swap breaks make_web prod:
- Revert `BUILDINGS_DB_URL` to previous endpoint
- Old endpoint still has C8 data + 24 legacy tables (untouched)

## Open questions for make_web

1. `BUILDINGS_DB_HOST` current value (to confirm divergence + identify old endpoint URL)
2. `user_data` DB host — same endpoint as old buildings, or already separate? (likely same project, different DB — but worth verifying)
3. Railway env var convention — single `BUILDINGS_DB_URL` or split into `BUILDINGS_DB_HOST/USER/PASSWORD/NAME`?
4. Any caching layer in front of canonical_v2_buildings (Redis, CDN, materialized view) that needs invalidation post-swap?

## Follow-ups (after swap success)

- Re-run Codex audit on new endpoint state (Stage 3 was meaningless on the wrong endpoint).
- Drop old endpoint after stability window.
- Add endpoint info to `CLAUDE.md` so this divergence doesn't reoccur.
