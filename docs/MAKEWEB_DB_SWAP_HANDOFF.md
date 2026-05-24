# make_web Neon Swap Handoff — 2026-05-24 (final)

## Why

`make_db` finished a comprehensive Neon cleanup today. **make_web's
`BUILDINGS_DB_*` env vars + any hard-coded references must update** before
make_web's production runtime can read current building/architect data.

## What changed in Neon (already done by make_db)

| Before | After |
|---|---|
| Branch `local-dev` existed (5/23-created, stale, 24 user tables) | **Dropped** |
| Database `neondb` | **Renamed to `archi_data`** |
| Role `makeweb_buildings_ro` | **Renamed to `make_web`** (password unchanged, grants intact) |
| 24 user/app/Django tables + `architecture_vectors` in `neondb` | **Dropped** (preserved in `pre-cleanup-2026-05-24` snapshot branch for rollback) |
| `canonical_v2_buildings` schema | unchanged (40 cols incl. `year_kind`) |
| `canonical_v2_architects` table | **NEW** (added today, 14,216 firms, 4,357 recommendable) |

Final state on **production** branch (endpoint
`ep-broad-hat-a1jaomn7.ap-southeast-1.aws.neon.tech`):

| Database | Tables | Used by |
|---|---|---|
| `archi_data` | `canonical_v2_buildings` (39,478 rows) + `canonical_v2_architects` (14,216 rows) | make_db (writer via `neondb_owner`), make_web (read-only via `make_web` role) |
| `user_data` | Django/auth/swipes/sessions/follows | **make_web only** (read+write) |

Snapshot for rollback (1-week archive):
- Branch `pre-cleanup-2026-05-24` (endpoint `ep-old-queen-a1jsu2do`), DB still
  named `neondb`, still has 24 user tables + stale C8 buildings.

## make_web actions required

### 1. Env vars (local `.env` + Railway prod)

```bash
# Buildings/Architects (READ-ONLY)
BUILDINGS_DB_HOST=ep-broad-hat-a1jaomn7.ap-southeast-1.aws.neon.tech
BUILDINGS_DB_PORT=5432
BUILDINGS_DB_USER=make_web                # ← was makeweb_buildings_ro
BUILDINGS_DB_PASSWORD=<see secret file>   # unchanged from previous rotation
BUILDINGS_DB_NAME=archi_data              # ← was neondb
BUILDINGS_DB_SSLMODE=require

# Composed:
# BUILDINGS_DB_URL=postgresql://make_web:<pw>@ep-broad-hat-a1jaomn7.ap-southeast-1.aws.neon.tech/archi_data?sslmode=require

# user_data unchanged
# USER_DATA_DB_URL=postgresql://<userdata_role>:<pw>@<same_host>/user_data?sslmode=require
```

Password lives in `make_db/.env.make-web` (mode 0600, gitignored). Hand off via
1Password or Neon secret vault — **never plaintext Slack/email**.

### 2. Code reference grep

Search make_web codebase for any hardcoded reference and update:

| Old | New |
|---|---|
| `"neondb"` (database name string) | `"archi_data"` |
| `"makeweb_buildings_ro"` (role name) | `"make_web"` |
| `BUILDINGS_DB_NAME=neondb` (config files) | `BUILDINGS_DB_NAME=archi_data` |

```bash
# In make_web repo:
grep -rn "neondb\|makeweb_buildings_ro" --include="*.py" --include="*.env*" --include="*.toml" --include="*.yaml" --include="*.yml"
```

### 3. Railway redeploy

After env var update, trigger Railway service redeploy. Confirm pod logs show
new connection params.

### 4. Verification SQL (run after swap)

```python
import os, psycopg2
conn = psycopg2.connect(os.environ['BUILDINGS_DB_URL'])
with conn.cursor() as cur:
    cur.execute("SELECT current_database()")
    print(cur.fetchone())  # expect: ('archi_data',)

    cur.execute("SELECT COUNT(*) FROM canonical_v2_buildings")
    print(cur.fetchone())  # expect: (39478,)

    cur.execute("SELECT COUNT(*) FROM canonical_v2_architects")
    print(cur.fetchone())  # expect: (14216,)

    cur.execute("""
        SELECT canonical_arch_id, canonical_name, primary_country,
               n_buildings_publishable
        FROM canonical_v2_architects
        WHERE canonical_arch_id = 'arch_000000'
    """)
    print(cur.fetchone())  # expect: ('arch_000000', 'Foster + Partners', 'United States', 82)
```

Pass criteria: all 4 outputs match.

### 5. Rollback (if swap breaks make_web)

- Revert env to previous values (will fail — old role `makeweb_buildings_ro`
  + old DB `neondb` no longer exist on production branch).
- Real rollback option: point env at snapshot branch endpoint
  `ep-old-queen-a1jsu2do` with old creds (DB still named `neondb`, role still
  `neondb_owner`). 1-week rollback window.

## Schema reference for make_web queries

- Building card: `canonical_bld_id`, `name`, `location_country`,
  `location_city`, `project_year`, `year_kind`, `program`, `style`,
  `architect_canonical_ids`, `architect_names`, `display_cover_url`,
  `is_publishable`, `publishability_reasons`, `embedding`.
- Architect card: `canonical_arch_id`, `canonical_name`, `primary_country`,
  `primary_city`, `website`, `social_links`, `logo_url`, `hero_building_id`,
  `n_buildings_publishable`, `top_programs/styles/typologies`,
  `portfolio_embedding`, `is_recommendable`.

Full schema: `docs/REFERENCE.md` §2 (buildings) + §2b (architects).
Recommendation algorithm + SQL templates: `docs/ARCHITECT_RECOMMENDATION.md`.

## Open questions for make_web

1. Confirm Railway redeploy completed + verification SQL passed
2. `user_data` DB: any role separation needed there? (currently uses
   neondb_owner)
3. Caching layer (Redis/CDN/MV) invalidation needed?
4. After 1-week snapshot retention: OK to drop `pre-cleanup-2026-05-24` branch?

## Post-swap follow-ups (make_db side)

- Codex audit re-run on new endpoint state (current C23 + architects).
- Drop `pre-cleanup-2026-05-24` snapshot branch after 1 week.
