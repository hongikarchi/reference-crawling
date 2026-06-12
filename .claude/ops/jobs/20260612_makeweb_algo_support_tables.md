# 2026-06-12 — make_web algo-support precompute tables (R1-R3) + R4 smoke

## Scope

Make Web requested corpus precompute tables so its recommendation engine stops
computing global stats at web runtime (39k GROUP BY per axis; session-pool
kw_vec averages). Request doc: "추천 알고리즘 토대 강화 (Corpus Precompute)".
Response doc: `docs/MAKEDB_ALGO_SUPPORT_RESPONSE.md`.

User decisions (plan mode): (1) reclassify migration first → stats on clean
data; (2) R3 labels reviewed via existing manual_review_workflow dashboard
(new vocab_label cards); (3) R4 smoke included this scope, R5 deferred.
**Re-sequencing (user, 2026-06-12): ALL Neon writes bundled into one final
gate** — "deploy는 맨 마지막에... 중간에 하다가 문제가 생기면 돌릴 수가 없잖".

## Built

- `tools/canonical_v2_tag_stats_build.py` — R1 `canonical_v2_tag_stats` +
  R2 `canonical_v2_tag_centroids` (L2-normalized AVG(embedding), pgvector
  0.8.0 server-side) + R3 `canonical_v2_tag_vocabulary`, 6 axes (5 requested +
  architectural_elements), one transaction with in-txn QC (19 checks, FAIL →
  ROLLBACK). `--with-reclassify` composes the held material migration into the
  same transaction via `strip_material_noise_neon.execute(cur)` (refactored).
  Modes: --discover / --dry-run / --build --confirm-db-write / --inspect-tables.
- `data/canonical/tag_vocabulary_labels.json` — 147 ko/en labels + is_generic
  (60 controlled vocab + 87 material doc_freq≥100).
- `tools/manual_review_workflow.py` — vocab_label card kind (approve/edit/
  unsure, undo, keyboard), `vocab_decisions` in decisions JSON,
  `apply-vocab-labels` subcommand patches the labels file. Smoke-tested
  end-to-end (147 cards, save/undo/apply/validation OK).
- `tools/canonical_v2_architects_build.py` — `_apply_reclassify_view()`:
  artifact rows get reclassify + unpublish rules before aggregation (local
  c23_final predates the migration).
- `tools/r4_axis_smoke.py` — N=10→100 codex-exec smoke for proposed axes
  scale/structural_system/roof_type/facade_pattern (era = deterministic from
  project_year, no LLM).

## Verified (all read-only / ROLLBACK)

- `--discover`: material tag space 18,592 distinct (12,985 singletons, 116 with
  df≥100), 0 case/trim variants, pgvector 0.8.0, AVG(vector) supported.
- `--dry-run --with-reclassify --corpus-version c23_final+matstrip`: **PASS,
  19/19 QC**. total_n 36,673; rows: stats/centroids 18,621, vocabulary 18,625.
  Brutalist centroid cosine top-20 → 7 Brutalist (base ~3%). make_web SELECT
  grant auto-applied (default privileges). Generic candidates surfaced:
  program/Housing, material/glass (~40% share each) — user decision.
- Architects rebuild: 14,216 written, is_recommendable 4,357 → **4,348** (−9);
  loader `--dry-run-upsert` PASS (rolled back).
- R4 smoke N=10 + N=100: ok 110/110, retry 0, mean 4.8s, ~482 in-tok/item.
  N=100 Unknown rates: scale **0%** / facade 23% / structural 52% /
  roof **69%**. Verdict: scale+facade ship text-only; roof (+structural)
  need a D-2-style vision pass. 39k extrapolation ~19M in-tokens / ~53h
  serial. era = 99% coverage from project_year (no LLM).
- pytest: 93 passed, 0 failed.

## Pending — FINAL DEPLOY (single user gate, not yet run)

1. User reviews labels: `manual_review_workflow.py serve` → vocab tab →
   `apply-vocab-labels`.
2. `tools/canonical_v2_tag_stats_build.py --build --with-reclassify
   --confirm-db-write --corpus-version c23_final+matstrip`
   (one txn: reclassify 9,606 rows / publishable 36,864→36,673 / 3 tables).
3. `tools/canonical_v2_architects_neon_loader.py --upsert --confirm-db-write`
   (14,216 rows, recommendable 4,348).
4. Post: `--inspect-tables`, dashboard regen, notify make_web.

## Cost

LLM: R4 smoke only (N=10+N=100 codex exec fast tier, ~53k in-tokens total,
≪ $5 gate). Neon: read-only so far.
