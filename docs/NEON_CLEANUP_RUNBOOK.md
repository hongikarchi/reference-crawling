# Neon `archi_data` Cleanup Runbook

Goal: turn `archi_data` into pure **architecture data** (buildings + architects).
Migrate or drop everything else. From make_web request 2026-05-24.

**All destructive steps gated on (a) snapshot + (b) make_web "migration done"
confirmation.**

---

## Step 1 — Snapshot (REQUIRED before any DROP)

**Recommended: Neon UI branch (1-click, copy-on-write, free).**

1. Open https://console.neon.tech → archi-tinder project → Branches.
2. Click "Create branch" → source `production` (or current branch holding
   `archi_data`) → name `pre-cleanup-2026-05-24` (or similar).
3. Confirm branch creation; verify it shows the current LSN.

Alternative: `neonctl branches create --project-id <pid> --name
pre-cleanup-2026-05-24` (needs `neonctl auth` first).

Verify rollback recipe: branch can be restored to original by switching the
production endpoint back to the snapshot branch.

---

## Step 2 — KEEP list (confirmed)

In `archi_data`, **KEEP**:

| Object | Type | Size | Notes |
|---|---|---|---|
| `canonical_v2_buildings` | TABLE | ~1.1 GB | 39,478 rows / 36,864 publishable, C23 |
| `canonical_v2_architects` | TABLE | ~150 MB | 14,216 rows / 4,357 recommendable |
| `idx_canonical_v2_buildings_*` | INDEX | — | All 11 indexes (B-tree + GIN + HNSW) |
| `idx_canonical_v2_architects_*` | INDEX | — | All 8 indexes |
| `vector` extension | EXTENSION | — | pgvector for embedding similarity |
| `canonical_v2_buildings_year_kind_check` | CONSTRAINT | — | C23 schema migration |

**KEEP under review** (decision pending):

| Object | Rows | Notes |
|---|---|---|
| `architecture_vectors` | 3,465 | Legacy metalocus-only table. Per CLAUDE.md "retired". **Recommended: DROP** unless make_web still reads from it. |

---

## Step 3 — DELETE candidates (gated on make_web "migration done" confirm)

App / user / Django tables in `archi_data` (should live in `user_data` per
2026-05-24 architecture decision):

| Table | Rows | Size |
|---|---|---|
| `auth_user` | 10 | 64 kB |
| `auth_group`, `auth_group_permissions`, `auth_permission` | 0/0/76 | ~110 kB |
| `auth_user_groups`, `auth_user_user_permissions` | 0/0 | ~64 kB |
| `accounts_userprofile` | 9 | 48 kB |
| `accounts_socialaccount` | 8 | 56 kB |
| `django_session` | 1 | 64 kB |
| `django_admin_log` | 0 | 32 kB |
| `django_content_type` | 19 | 40 kB |
| `django_migrations` | 57 | 32 kB |
| `profiles_office` | 0 | 40 kB |
| `profiles_officeprojectlink` | 0 | 64 kB |
| `recommendation_swipeevent` | 91 | 1.5 MB |
| `recommendation_sessionevent` | 2,275 | 1.9 MB |
| `recommendation_analysissession` | 7 | 1.6 MB |
| `recommendation_project` | 8 | 104 kB |
| `social_follow`, `social_officefollow`, `social_reaction` | 0/0/2 | ~200 kB |
| `token_blacklist_outstandingtoken` | 511 | 368 kB |
| `token_blacklist_blacklistedtoken` | 28 | 40 kB |

Total ~7 MB. Drop SQL template (do NOT run without make_web GO + snapshot):

```sql
BEGIN;
DROP TABLE IF EXISTS
  token_blacklist_blacklistedtoken, token_blacklist_outstandingtoken,
  social_reaction, social_officefollow, social_follow,
  recommendation_swipeevent, recommendation_sessionevent,
  recommendation_analysissession, recommendation_project,
  profiles_officeprojectlink, profiles_office,
  django_admin_log, django_session,
  accounts_socialaccount, accounts_userprofile,
  auth_user_user_permissions, auth_user_groups, auth_user,
  auth_group_permissions, auth_group, auth_permission,
  django_content_type, django_migrations
CASCADE;
COMMIT;
```

**Suggested order**:
1. make_web team confirms user data is fully migrated to `user_data` DB OR
   declares "no migration needed; these tables can be dropped".
2. Verify snapshot exists (Step 1).
3. Run the DROP block above.
4. Re-inspect to confirm `archi_data` has only `canonical_v2_buildings` +
   `canonical_v2_architects` (+ `architecture_vectors` if not yet dropped).

---

## Step 4 — Role / connection-string separation

Goal: read-only role for make_web's buildings queries; user_data role distinct.

```sql
-- Read-only role for make_web building/architect access
CREATE ROLE make_web LOGIN PASSWORD '<set-in-neon-secrets>';
GRANT CONNECT ON DATABASE archi_data TO make_web;
GRANT USAGE ON SCHEMA public TO make_web;
GRANT SELECT ON canonical_v2_buildings, canonical_v2_architects
  TO make_web;
-- Future tables in same schema get SELECT auto:
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT ON TABLES TO make_web;

-- Optional: writer role for make_db (this is the existing archi_data_owner; no change)
```

make_web `.env`:
```
BUILDINGS_DB_URL=postgresql://make_web:<pw>@<host>/archi_data?sslmode=require
USER_DATA_DB_URL=postgresql://<userdata_role>:<pw>@<host>/user_data?sslmode=require
```

make_db `.env` unchanged (continues to use writer role for upserts).

**Execution order**:
1. Create role + grant SELECT in Neon SQL editor (or via psql by `archi_data_owner`).
2. Store password in Neon's secret vault or pass to make_web team via secure
   channel.
3. make_web updates `BUILDINGS_DB_URL` env var in deployment.

---

## Step 5 — Schema doc currency

`docs/REFERENCE.md` §2 (canonical_v2_buildings, 40 columns) and §2b
(canonical_v2_architects, new) reflect current Neon schema as of C23 + A3.

Verify any time via:
```bash
python3 tools/canonical_v2_neon_loader.py --emit-sql       # buildings DDL
python3 tools/canonical_v2_architects_neon_loader.py --inspect-table  # cols
```

make_web column contract:
- Building cards: `canonical_bld_id`, `name`, `location_country`,
  `location_city`, `project_year`, `year_kind`, `program`, `style`,
  `architect_canonical_ids`, `architect_names`, `display_cover_url`,
  `is_publishable`, `publishability_reasons`, `embedding`.
- Architect cards: `canonical_arch_id`, `canonical_name`, `primary_country`,
  `primary_city`, `website`, `social_links`, `logo_url`, `hero_building_id`,
  `n_buildings_publishable`, `top_programs/styles/typologies`,
  `portfolio_embedding`, `is_recommendable`.
- Provenance (for debugging / source attribution): `source_refs`,
  `source_urls`.

---

## Ownership

- Steps 1-3: this terminal (make_db) executes after snapshot + make_web GO.
- Step 4: split — make_db creates role + grants; make_web rotates env vars.
- Step 5: kept current here.
