# 2026-06-04 — Repo dead-file scan (exhaustive, report-only)

## Scope

User: "필요 없는 파일 있는지 전수검사" — exhaustive scan for unneeded files.
Ran `repo-deadfile-scan` workflow (142 agents): c23_final integrity gate +
7 code classifiers + 3 data classifiers + adversarial skeptic per
delete-candidate. **Report-only — nothing deleted.**

## Gate (linchpin) — c23_final INTACT

`canonical_buildings_strict_embedded.completeness_c23_final.json`: 39,478 rows
/ 36,864 publishable (exact CLAUDE.md match), embeddings 100% uniformly
384-dim, year_kind 100% in {completed:37873, unknown:1293, future:312}.
Verified via streaming ijson. ⇒ older checkpoints are pure superseded history.

## DATA — 11.86 GB reclaimable (⚠️ gitignored → NOT git-recoverable, irreversible)

All in `data/canonical/country_conflict_refresh/`. KEEP both c23_final twins.

| Group | Files | Size |
|---|---|---|
| A · embedded twins (c8, c18–c22) | 6 | 6.97 GB |
| B · non-embedded twins (c8, c18–c22) | 6 | 4.89 GB |
| C · c8 sidecars (affected / loader / cids) | 3 | ~2.5 MB |
| **TOTAL** | **15** | **≈ 11.86 GB** |

**HOLD (not in the prize):**
- `e1_clusters.patched.jsonl` (700 MB) — **sole surviving Stage E-1 artifact**;
  live `data/canonical/e1_clusters.jsonl` is ABSENT. Do NOT delete (or promote
  to live path first).
- RESUME_PARTIAL families (~281 MB): `d2_results.patched.*`,
  `d1_results.patched.jsonl`, `e2_image_types.patched.jsonl`,
  `d2_*.image_backfill.*` (87 shards) — optional, drop per-family only after
  confirming live consolidated `data/canonical/{d1,d2,e2}_*` intact.

## PROTECTED — never auto-delete (CLAUDE.md `id_registry*` glob, ~75 MB)

Live: `id_registry_buildings.json`, `id_registry_architects.json`,
`id_registry.json`. Snapshots (look droppable, rule covers them):
`*.backup_pre_c17/c18`, `*.before_phash`, `*.before_reset` (×2),
`*.json.bak_pre_fix`. **Human override required to reclaim.**

## CODE — 55 confirmed dead (git-recoverable, working-tree only)

37 ONESHOT_DONE + 23 SUPERSEDED + 2 ORPHAN − 7 rescued. Verified core (~28):

**ONESHOT_DONE:** `tools/apply_tiebreak_results.py`, `audit_cleanup_exec.py`,
`audit_per_issue_verify.py`, `build_d1_batches.py`, `build_tiebreak_projects.py`,
`build_typology_crosswalk.py`, `canonical_v2_c14_finalize.py`, `…c15…`, `…c22…`,
`canonical_v2_apply_code_splits.py`, `canonical_v2_refresh_code_splits.py`,
`canonical_v2_llm_location_adjudication.py`,
`canonical_v2_remaining_review_verdict.py`,
`canonical_v2_review_rule_candidates.py`, `canonical/manual_tiebreaks.py`.

**SUPERSEDED:** `tools/build_cover_phash_review.py` (→ `cover_review_app.py`),
`canonical/match_architects_4source.py`, `match_pair.py`, `match_buildings.py`,
`canonical/build.py`, `reality_filter.py`, `match_tiebreaker.py`,
`enrich/reprocess.py`, `label_golden.py`, `harness_agent.py`,
`sonnet_image_runner.py`.

**ORPHAN:** `canonical/feedback_to_stage_a.py`, `canonical/reviewer_gate.py`
(retired multi-team/cmux layer).

Dead-island siblings reachable from these roots (e.g. `build_tiebreak_batches`,
`build_round2_batches`, `match_architects_plan_b`, `eval.py`, remaining
C16–C21 polish) bring the total to 55. **Re-`git grep <module>` per file at
delete time** to confirm zero importers; remove dead islands together.

## RESCUED — classifier flagged, skeptic kept (do NOT remove)

Stage A/B canonical-regen family held by recorded keep-decision in
`20260524_repo_audit_and_cleanup.md` + live data-flow into
`build_strict_canonical.py`: `apply_buildings_unified.py`,
`apply_round2_results.py`, `apply_tiebreak_buildings_results.py`,
`backfill_architects.py`, `backfill_divisare_architect_registry.py`,
`build_completeness_c11_taxonomy.py`, `build_completeness_c9.py`.

## Next actions (all user-gated, NONE executed)

1. DATA: rm 15 superseded checkpoints → 11.86 GB (irreversible; c23_final gate
   passed). 2. DATA optional: ~281 MB resume partials after intact-check.
3. HOLD e1_clusters.patched.jsonl + id_registry*. 4. CODE: rm 55 dead after
   per-file `git grep`. 5. Post-cleanup: regen dashboard + this card.

## EXECUTED (2026-06-05, user-approved deletes)

- **DATA: done.** Removed the 15 superseded checkpoints (exact filenames,
  guarded against c23_final). `country_conflict_refresh/` 14G → 2.6G,
  **~11.3 GB freed**. c23_final twins + `e1_clusters.patched.jsonl` (HELD) +
  `id_registry*` (PROTECTED) untouched.
- **CODE: done — 28 files + 2 paired tests** `git rm`'d (working tree).
  Full pytest after: 78 pass, 1 fail (`test_d2_cover_vision` — pre-existing on
  HEAD, casing assertion, unrelated). The deleted set is the verified
  **safe transitive closure** (no surviving importer), NOT the report's 55.
  - **Correction to scan:** `canonical_v2_c15..c21` are NOT dead — live
    `canonical_v2_architects_build.py` / `architects_audit.py` import c20/c21,
    which chain back to c15. Likewise `canonical/build.py`, `match_buildings.py`,
    `reality_filter.py`, `match_tiebreaker.py` are imported by live core
    (`registry`, `schema`, `image_dedup`, `qc`). All 12 HELD.
  - Deleted: c14, c22, apply_tiebreak_results, audit_cleanup_exec,
    audit_per_issue_verify, build_d1_batches, build_{round2,tiebreak,
    tiebreak_buildings}_batches, build_tiebreak_projects, build_typology_crosswalk,
    canonical_v2_{apply,refresh}_code_splits, llm_location_adjudication,
    remaining_review_verdict, review_rule_candidates, build_cover_phash_review,
    match_architects_4source, match_architects_plan_b, match_pair,
    feedback_to_stage_a, reviewer_gate, enrich/{eval,reprocess,label_golden,
    harness_agent,sonnet_image_runner}, manual_tiebreaks + 2 tests.
- **HELD (not deleted):** 12 live-referenced code files above; data
  `e1_clusters.patched.jsonl` (700 MB, sole E-1 artifact); `id_registry*`
  (protected); ~281 MB resume partials (optional, untouched).

_Workflow w0uhdocoo · 142 agents · 4.1M tokens · full transcript in session
subagents dir._
