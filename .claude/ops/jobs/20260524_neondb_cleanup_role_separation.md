# 2026-05-24 neondb cleanup + role separation

## Trigger

make_web confirmed: "branch created, runtime only on user_data, neondb only
for canonical_v2_buildings raw SQL. 24 user/app tables + architecture_vectors
all orphan → DROP. make_db code impact 0."

## Snapshot

User created Neon branch in UI before any DROP (rollback point preserved).

## Step 3 — DROPS

Single transaction, CASCADE:
- 23 Django/auth/recommendation/social/token/profiles tables
- 1 legacy `architecture_vectors` (3,465 rows, retired per CLAUDE.md)

```sql
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
  django_content_type, django_migrations,
  architecture_vectors
CASCADE;
```

Result: 26 tables → 2 (canonical_v2_buildings + canonical_v2_architects).
neondb size: 1151 MB (was ~1158 MB; ~7 MB freed — small because user tables
were tiny).

## Step 4 — Role separation

Created `makeweb_buildings_ro` role:
- LOGIN with auto-generated 24-byte token_urlsafe password
- GRANT CONNECT ON DATABASE neondb
- GRANT USAGE ON SCHEMA public
- GRANT SELECT ON canonical_v2_buildings, canonical_v2_architects
- ALTER DEFAULT PRIVILEGES → future tables in public get SELECT auto

Credentials at `.env.makeweb-buildings-ro` (mode 0600, gitignored). Hand off
to make_web team via Neon secrets or 1Password (not plaintext channel).

Verified: `has_table_privilege('makeweb_buildings_ro', X, 'SELECT')` returns
True for both KEEP tables.

## Final state

| | |
|---|---|
| neondb tables | canonical_v2_buildings (39,478) + canonical_v2_architects (14,216) |
| neondb size | 1151 MB |
| Roles | neondb_owner (writer, existing), makeweb_buildings_ro (SELECT-only, new) |
| Snapshot branch | created by user pre-cleanup |

## Pending follow-ups

- make_web: rotate `BUILDINGS_DB_URL` env var to use makeweb_buildings_ro
  credentials (from `.env.makeweb-buildings-ro`).
- Optional: future DB rename `neondb` → `architecture_data` (deferred per
  earlier decision; coordinate downtime window with make_web).
