# make_db

Pipeline that builds the **ArchiTinder** building database: crawl architecture
sites → LLM text + image enrichment → canonical consolidation → Neon Postgres
(+ R2) upload. Consumed by the `make_web` app.

This file is the operating manual. For schema/vocab/tool detail see
`docs/REFERENCE.md`; for live pipeline + DB state open `docs/dashboard.html`.

## Operating model

- **Single operator.** Claude runs the whole pipeline end to end. There is no
  multi-team / cmux / Codex-dispatch layer — that was retired in the 2026-05
  refactor (recover from git history if ever needed).
- **State lives in files, never terminal memory.** The durable record is: the
  data artifacts, `.claude/ops/jobs/` job cards, and the dashboard. Re-derive
  state by reading those — never assume in-memory continuity between sessions.
- **Current production dataset (Neon `archi_data`):**
  - `canonical_v2_buildings`: 39,478 rows / 36,864 publishable, artifact
    `completeness_c23_final`, schema includes `year_kind`.
  - `canonical_v2_architects`: 14,216 rows / 4,357 recommendable, derived from
    `id_registry_architects.json` + 3 source firm DBs + buildings reverse-index;
    portfolio embedding = mean of publishable building embeddings (384-dim, same
    space). See `docs/ARCHITECT_RECOMMENDATION.md`.
  - Tag precompute siblings (make_web algo support, 2026-06): built + dry-run
    PASS, **deploy pending final user gate** — `canonical_v2_tag_stats` /
    `_tag_centroids` / `_tag_vocabulary` via
    `tools/canonical_v2_tag_stats_build.py` (rebuild every crawl, one txn).
    Bundled with the held material reclassify (publishable 36,864→36,673).
    See `docs/MAKEDB_ALGO_SUPPORT_RESPONSE.md` + job card
    `20260612_makeweb_algo_support_tables.md`.
  - Mental model: **`archi_data` = architecture data** (only 2 tables above);
    **`user_data`** (separate Neon DB) holds Django auth/profiles/swipes for
    make_web. As of 2026-05-24, all 23 user/app tables + legacy
    `architecture_vectors` dropped from `archi_data`.
  - Roles: `neondb_owner` (writer, used by make_db — kept original name for
    Neon platform compatibility); `make_web` (SELECT-only on canonical_v2_*,
    used by make_web; creds in gitignored `.env.make-web`).
  - Cleanup history: job card
    `.claude/ops/jobs/20260524_neondb_cleanup_role_separation.md`.

## Pipeline (5 stages — detail in docs/REFERENCE.md)

| Stage | Code | Output |
|---|---|---|
| 1 Crawl | `crawl/{divisare,architizer,archello,metalocus}/` | `data/crawl/<source>.db` |
| 2-3 Enrich | `enrich/`, `tools/d1_enrich_codex.py`, `tools/d2_cover_vision.py` | D-1 text + D-2 image fields |
| 4 Canonical | `canonical/`, `tools/build_strict_canonical.py` | `canonical_buildings_strict_embedded.*.json` |
| 5 Upload | `tools/canonical_v2_neon_loader.py`, `tools/canonical_v2_architects_neon_loader.py` | Neon `canonical_v2_buildings` + `canonical_v2_architects` + R2 |

## Running work

- **Smoke ladder before any large or LLM-cost run:** N=10 (verify schema +
  sample quality + measure tokens/cost per item) → N=100 (failure rate + cost
  extrapolation) → full. Never launch a full LLM run un-smoked — a 2026-05
  incident burned a weekly quota on an unmeasured run.
- **Cost estimate first.** Any run costing more than ~$5 of LLM tokens, or a
  meaningful API-quota fraction, gets a projected-cost line and explicit user
  approval before launch.
- **Batch, not per-item.** Size work in batches (50-500). Steps are idempotent
  (`enrich/tasks_db.py` / resume files) — re-running a completed step is free.
- **Sub-agent concurrency cap ≈ 3-4.** Launching many sub-agents at once hits
  API rate limits (observed 2026-05). Run waves, not floods.
- **Record every non-trivial run** as a job card in `.claude/ops/jobs/`
  (`YYYYMMDD_<slug>.md`: scope, inputs, outputs, cost, result).

## CLI

`run.py` is the legacy-metalocus dispatcher (`run.py --help`). The current
`canonical_v2` pipeline runs through `tools/` scripts — `tools/canonical_v2_*`
(build/QC/completeness), `tools/d1_enrich_codex.py` / `d2_cover_vision.py`
(enrichment), `tools/canonical_v2_neon_loader.py` (Neon load). `docs/REFERENCE.md`
§Tools is the map. Read-only DB inspection:
`python3 tools/canonical_v2_neon_loader.py --inspect-table`.

## Rules

- **Never edit `core/vocab.py`** on your own judgment — vocabulary changes are
  user decisions.
- **Never delete `data/id_registry*.json`** — stable building/architect IDs;
  losing them breaks every downstream join.
- **Upload is user-gated.** Never run a Neon write
  (`tools/canonical_v2_neon_loader.py --upsert --confirm-db-write` or the
  architects equivalent) without explicit user approval. Always dry-run and
  present row counts first.
- **Git:** solo dev, single `main` branch, no feature branches. Commit per
  logical change. `git push` only on an explicit user request ("push" /
  "푸시해" / "올려"). Never force-push or rewrite history.
- **Plan mode** (user rule): plan in Korean — short structured explanation
  (tables / bullets), surface decisions one at a time as objective
  multiple-choice via `AskUserQuestion`, confirm each before the next, write
  the formal plan only after all decisions are confirmed. Keep plan files
  concise (no 200-line English dumps).
- **Risky/destructive actions** (file/folder deletes, schema changes, anything
  touching Neon / R2 / shared state) — confirm with the user before acting.

## Known gotchas

- Neon credentials are in `.env` (gitignored); connect via
  `tools/canonical_v2_neon_loader.py._connect`.
- `image_derived` (the D-2 vision field) is the database's least-reliable
  field — ~24% out-of-controlled-vocabulary. Treat it as advisory; the
  top-level `style/color_tone/atmosphere` (from D-1) are clean. See the audit.
- Don't re-run `enrich/dedup.py` on already-processed buildings.
- `data/` and `images/` stay at repo root regardless of which code subpackage
  produced them.

## Documents

- `README.md` — what make_db is + quickstart
- `docs/REFERENCE.md` — schema, vocabularies, tool specs, 5-stage architecture, new-source runbook
- `docs/ARCHITECT_RECOMMENDATION.md` — architects table schema + cold-start + cosine SQL templates
- `docs/dashboard.html` — live pipeline + DB state (regenerate: `python3 tools/build_dashboard.py`)
- `data/reports/db_quality_audit.md` — 2026-05 database quality audit (verdict: PASS with WARNINGS)
- `.claude/ops/jobs/` — job cards = run history
