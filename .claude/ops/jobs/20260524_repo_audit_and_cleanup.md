# Repo audit + cleanup (2026-05-24)

## Scope

Full-repo audit after make_web swap completion. Goal: identify outdated docs,
retired tools (cmux/dispatch/multi-team era), dead code, and historical
artifacts no longer load-bearing.

## Method

1. `find` over repo → ~250 source files identified
2. Cross-reference imports for suspect categories (upload/, gallery, audit_l*,
   db_ops_*, dispatch_enrich_batch, early-cycle c3-c8 apply tools)
3. Verify no live imports before deleting
4. Present categorized table to user with 4 strategic decisions

## User decisions

| Decision | Choice |
|---|---|
| Stage A/B dormant regen tools | KEEP (regen path) |
| Completed handoff/runbook docs (MAKEWEB_DB_SWAP, NEON_CLEANUP_RUNBOOK) | DELETE immediately |
| logs/ + crawl.log + .claude/ops/{snapshots,runs,reviews} | DELETE all |
| run.py + tests | KEEP (conservative) |

## Files removed

**docs (4)**:
- `docs/MAKE_WEB_HANDOFF.md` (Stage A/B era, 146,432 buildings + `architecture_vectors`)
- `docs/canonical_v2_schema.sql` (39,776 rows, no `year_kind`, pre-C23)
- `docs/MAKEWEB_DB_SWAP_HANDOFF.md` (swap completed 2026-05-24)
- `docs/NEON_CLEANUP_RUNBOOK.md` (cleanup completed 2026-05-24)

**upload/ package (5)**: `__init__.py`, `neon.py`, `neon_v2.py`, `neon_strict.py`,
`r2_uploader.py`. Superseded by `tools/canonical_v2_neon_loader.py` + architects
equivalent. Zero imports.

**tools/ cmux/dispatch shells + dead python (14)**:
- Shells: `cmux_setup.sh`, `db_ops_{cmux_setup,poll,send,snapshot_cmux,status}.sh`,
  `dispatch.sh`, `poll.sh`, `quota_check.sh`, `safe_commit.sh`
- Python: `db_job_card.py`, `db_review_packet.py`, `db_run_record.py`
- Doc: `dispatch_enrich_batch_surfaces.md`
- KEPT: `dispatch_enrich_batch.py` (imported by `canonical_v2_local_enrich.py`
  + has tests)

**tools/ gallery (4)**: `divisare_gallery.html`, `divisare_server.py`,
`gallery.html`, `generate_gallery.py`. Ad-hoc inspection UIs, no longer used.

**tools/ early-cycle (7)**:
- `canonical_v2_c5_local_candidate_verdict.py`
- `canonical_v2_c6_candidate_queue.py`
- `canonical_v2_apply_completeness_c{3,4,6,7,8}.py`
Results coalesced into strict canonical; superseded by C9+ cycles.

**tools/ old audit_l*.py (5)**: `audit_l1_structural.py`, `audit_l3_aggregate.py`,
`audit_l3_prep.py`, `audit_l3_sampler.py`, `audit_l4l5.py`. Superseded by
Codex external audits (`canonical_v2_full_reaudit.py`,
`canonical_v2_architects_audit.py`).

**.claude/_archive/ (~33 files)**: agents/, dispatch_plans/, docs/, escalations/.
Multi-team era, already explicitly archived.

**.claude/ops/ (~30 files + 2 stub docs)**:
- `snapshots/20260512_233312/` — cmux workspace dump (13 files, gitignored)
- `runs/` — d2-image-backfill-resume5 era (16 files)
- `reviews/` — resume10 + crawler_gap_c5 (2 files)
- `README.md` + `decisions.md` — described retired DB_OPS workflow

**Local-only disposable**: `logs/` (75 files, 20MB), `crawl.log` root (29MB).

## Files modified

- `CLAUDE.md`:
  - Role: `archi_data_owner` → `neondb_owner` (kept original name for Neon
    platform compat per actual state)
  - Removed `docs/NEON_CLEANUP_RUNBOOK.md` reference
  - Fixed cleanup job card name (`neondb_cleanup_role_separation`, not
    `archi_data_cleanup`)
  - Stage 5 row: removed `upload/`, added architects loader
  - Removed `upload/*.py --confirm` rule wording
  - Removed `docs/MAKE_WEB_HANDOFF.md` from Documents list; added
    `docs/ARCHITECT_RECOMMENDATION.md`
- `README.md`:
  - Removed MAKE_WEB_HANDOFF link, added ARCHITECT_RECOMMENDATION link
  - Removed `upload/` from repo layout
- `docs/REFERENCE.md`:
  - Stage 5: removed `upload/`, added architects loader
  - Completeness tools: replaced deleted `apply_completeness_*` glob with
    `build_completeness_c9` + `build_completeness_c11_taxonomy`
  - Audit list: removed deleted `audit_l*.py`, kept current Codex tools

## Files KEPT (per user decision)

- Stage A/B canonical regen tools (~27 files in `canonical/` + `tools/`):
  match_architects_*, match_buildings_*, apply_tiebreak_*, backfill_*,
  build_tiebreak_*, split_*, etc. Dormant but produce JSON inputs to
  current canonical_v2 strict canonical build.
- `run.py` (legacy metalocus dispatcher per CLAUDE.md)
- All tests (`tests/test_*.py`)
- C14-C19 polish tools (5/23 reference for future redo)
- `.claude/ops/jobs/` (full history retained)
- `.claude/research/` (source schemas — onboarding reference)
- `data/reports/` (audit history, including pre-cleanup audit outputs)

## Net change

| Metric | Before | After |
|---|---|---|
| `tools/` file count | ~100 | 71 |
| `docs/` file count | 8 | 4 |
| `.claude/` total | ~75 files | ~17 files |
| Disk freed (local-only) | — | ~50MB (logs + crawl.log) |
| Git deletions | — | 92 |

## Verification

- All current Python tools import cleanly
- Tests still import cleanly
- No broken doc refs after grep
- Pre-existing `tools/canonical_v2_full_reaudit.py` script-mode import path
  unaffected (was already non-standard; runs fine as
  `python3 tools/canonical_v2_full_reaudit.py`)

## Result

Repo state: clean. All retired multi-team/cmux/dispatch artifacts removed.
Active canonical_v2 + architects pipeline + Stage A/B regen path intact.
Documentation now reflects post-make_web-swap reality.
