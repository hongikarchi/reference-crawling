# 20261016 Archibe DB handoff to ksangjo

## Scope
Hand the archi_data DB + dev environment to collaborator ksangjo (archi-tinder backend /
recommendation algo) so the service algorithm can align with the current and vNext schema,
without dev/prod drift. Plus repo cleanup.

## Neon (project archi-tinder / holy-pond-45504245, Launch plan)
- Branch `dev-ksangjo` (`br-dawn-violet-a10v9dg9`), endpoint `ep-weathered-base-a1xc6frq`,
  child of production, full data (archi_data, user_data, test_user_data), auto-delete off.
- Role passwords rotated **on the branch only** (neondb_owner, make_web, make_web_app);
  verified: new pw works on dev, old pw rejected on dev, prod pw unchanged on prod.
  Handoff block written OUTSIDE the repo: `C:/Users/user/archibe_handoff_ksangjo/backend.env.dev-ksangjo`.
- `vnext_preview.building_images` seeded on dev via `tools/vnext_preview_seed.py --apply`:
  36,673 publishable buildings -> 830,388 MOCK rows, checker PASS, SELECT granted to make_web
  (verified with make_web; make_web write denied).
- `tools/neon_schema_snapshot.py --compare docs/schema/archi_data.schema.json` on dev:
  schema identical, data identical.
- Production: no writes. user_data copy on dev contains real user rows (owner decision on PII pending).

## Repo
- Cleanup: 653 untracked items (~74.85 GB) moved (not deleted) to
  `C:/Users/user/reference-crawling_archive_20261016/` (MANIFEST.md, restore steps,
  `sandbox_scratch_admin.ps1` for 243 ACL-locked dirs). 109 git renames to `docs/archive/`
  and `archive/cross_source_semantic_v1x/`; `archive/conftest.py` skips archive in pytest.
  `pytest tests/` 1261 passed.
- New: `docs/HANDOFF_ARCHIBE_DB.md`, `docs/schema/{archi_data.schema.json,CHANGELOG.md}`,
  `tools/neon_schema_snapshot.py`, `tools/vnext_preview_seed.py` (+ tests), `.env*.example`.

## Cost
Neon branch compute/storage only (Launch plan). No LLM spend.

## Open (owner)
- After every dev branch reset: re-rotate dev passwords, re-run vnext_preview seed.
- Plaintext secrets in Dropbox `06_Archibe/make_db_migrate/.env*` -> delete.
- PR #437 merge -> `make migrate-prod`.
