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
  - `canonical_v2_buildings`: 39,478 rows / 36,673 publishable, artifact
    `completeness_c26_rem2026q2` (= c23_final + 2026-Q2 audit remediation overlay,
    == live Neon, upload-validator PASS; loader `DEFAULT_INPUT` points here — re-upsert
    from c26, NOT c23_final. c26 base lacks the 5 R4 columns BY DESIGN — the loader
    overlays R4 at upsert from `data/canonical/r4_results.merged.jsonl`), schema includes `year_kind` + R4 discriminative
    axes `era`/`scale`/`structural_system`/`roof_type`/`facade_pattern`
    (deployed 2026-06-14; `NULL` = unresolved, not `'Unknown'`).
  - `canonical_v2_architects`: 14,216 rows / 4,348 recommendable, derived from
    `id_registry_architects.json` + 3 source firm DBs + buildings reverse-index;
    portfolio embedding = mean of publishable building embeddings (384-dim, same
    space). See `docs/ARCHITECT_RECOMMENDATION.md`.
  - Tag precompute siblings (make_web algo support, R1-R3 **deployed
    2026-06-12**, R4 axes **deployed 2026-06-14**):
    `canonical_v2_tag_stats` / `_tag_centroids` / `_tag_vocabulary` (**15,655
    rows each; 11 axes**, `corpus_version c23_final+matstrip+r4+rem2026q2`) via
    `tools/canonical_v2_tag_stats_build.py --with-r4` — rebuild every crawl, one
    txn, in-txn QC. Material reclassify applied in the R1-R3 commit: publishable
    **36,673**, architects recommendable **4,348**. R4 = 5 new axes (era derived
    from `project_year`; scale/structural/roof/facade LLM-tagged text+vision,
    merged via `tools/r4_axis_merge.py`; G3 review ≥90%/axis). External QC
    benchmark post-deploy: 10 PASS / 0 FAIL. Contract:
    `docs/MAKEDB_ALGO_SUPPORT_RESPONSE.md`; job cards
    `20260612_makeweb_algo_support_tables.md`,
    `20260614_r4_discriminative_axes.md`.
  - **Audit + remediation (2026-06-14/15):** full census audit found semantic
    label issues (not structure). Applied to Neon: F1 `typology_primary`+`typology_tags`
    LLM re-derivation (9,403 rows, `typology_primary_source='llm_rederive_2026q2'`,
    typology↔program contradictions 2,825→828); F3 `material_visual` noise cleanup
    (14,654 rows, tag rows 18,655→15,655). Benchmark still 10 PASS/0 FAIL.
    **Pending user approval:** architect top_typologies refresh
    (`tools/refresh_architect_typologies.py`, 6,899 rows). F2 style over-generality
    NOT fixed (re-enrich empirically unreliable — make_web product decision).
    Reports: `data/reports/full_census_audit_2026Q2.md`,
    `remediation_log_2026Q2.md`; job cards `20260614_full_census_audit.md`,
    `20260615_audit_remediation_F1_F3.md`.
  - **Accuracy eval + typology refine (2026-06-19/21):** quantified two-tier accuracy
    (conformance + sampled veracity). Key result: the naive single-judge typology metric
    (Sonnet, 9.3% error) was inflated; an **independent Opus** judge on a disjoint holdout
    measured **2.7% clear error — already ≤5% target**. Applied a description-based typology
    re-derive anyway as a precision refinement: **1,647 rows**, `typology_primary_source=
    'descr_rederive_2026q2'`, net-validated 92% (Opus blind A/B), holdout error 2.7%→1.6%,
    **benchmark 10 PASS/0 FAIL**. Architect top_typologies refreshed (740). Contradiction
    proxy 828→906 (program-side lag + narrow Civic acceptable-set, not typology errors).
    Durability: override+c26+changed-only(24,792) sidecars regenerated (re-upsert safe).
    Reversible via `typology_corrections_descr.jsonl`. Vision proven UNRELIABLE for
    scale/structural/facade veracity (conformance-only). Report
    `data/reports/accuracy_eval_2026Q2.{md,json}`; job card `20260619_accuracy_eval_loop.md`.
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
