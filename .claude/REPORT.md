# make_db — Current State Report

*Updated: 2026-03-31*

---

## Database State

| What | Count |
|------|-------|
| PostgreSQL (Neon) | **3,465 buildings** |
| R2 images | **~17,303 images (~7.09 GB)** |
| SQLite articles (completed) | 3,873 |
| SQLite articles (pending) | 9,206 |
| id_registry.json | 3,465 entries |
| 4_buildings_final.json | 3,465 buildings |
| Last quality rating | **96/100 — Ready** |

---

## What Is Built

| File | Status | Notes |
|------|--------|-------|
| `crawler.py` | ✅ | 4 workers concurrent image download, thread-safe rate limiter |
| `parsers.py` / `database.py` / `downloader.py` / `models.py` / `utils.py` | ✅ | crawl infrastructure |
| `stage1_export.py` | ✅ | SQLite → 1_buildings_raw.json |
| `stage2_dedup.py` | ✅ | dedup + ID assignment |
| `stage3_embed.py` | ✅ | 384-dim embeddings + vocab.migrate_strict |
| `vocab.py` | ✅ | canonical vocabularies + migrations + QC (Phase 1, 8A) |
| `tasks_db.py` | ✅ | SQLite task queue ledger (Phase 3) |
| `agent_llm_parser.py` / `agent_image_analysis.py` | ✅ | Anthropic SDK + tool_use + prompt caching |
| `pipeline_harness.py` | ✅ | queue-driven AI worker (Phase 3) |
| `quality.py` | ✅ | review + fix + rate + diagnose merged (Phase 8A) |
| `migrate_vocab.py` / `eval.py` / `label_golden.py` / `reprocess.py` | ✅ | workflow CLIs (also exposed as `run.py` subcommands) |
| `upload.py` | ✅ | UPSERT to Neon + upload to R2 (manual gate) |
| `run.py` | ✅ | unified CLI — 12 subcommands across build / quality / vocab+eval+reprocess / upload |
| `config.py` | ✅ | constants only |

---

## Batch 8 — Pending (next session)

Batch 8 was started but image downloads were stopped intentionally.

| Item | State |
|------|-------|
| Articles crawled | 408 new buildings in SQLite (B03466+, not yet exported) |
| Images pending | 11,897 in SQLite queue |
| Pipeline files | Still at 3,465 (batch 8 not yet exported) |

**To resume next session:**
```bash
python3 run.py crawl --articles 500   # resumes image downloads (~100 min)
python3 run.py export-dedup           # export + dedup
python3 run.py harness                # text enrichment + image analysis + QC
python3 run.py embed-rate             # embed + quality review/fix/rate
python3 upload.py                     # requires user approval
```

---

## Known Gotchas

- `DB_PASSWORD` excluded from required-fields check — Neon needs it but check is bypassed.
- Images are at `images/{building_id}/` after dedup (reorganized from `images/{slug}/`).
- Don't re-run `stage2_dedup.py` on already-processed buildings.
- `pipeline.py` (legacy phase orchestrator) still coexists with `pipeline_harness.py` (canonical, queue-driven). Removal scheduled in Phase 8A.
- 22 Python files at the repo root (down from 27 after Phase 8A). All operations route through `run.py`; standalone module scripts are still invokable but the unified CLI is canonical.
- 2,784 / 3,465 production records have out-of-V2 atmosphere values (`organic`, `communal`, `historic`, …). See `data/reports/vocab_migration.json`. Re-processing requires user cost approval.
- Upload always requires `--reset` on first run or after schema changes (avoids `mood` column conflict from old schema).

---

## Active Architecture Roadmap

`~/.claude/plans/db-fuzzy-lerdorf.md` — 7-phase plan targeting three present-tense pains: quality ceiling, resumability (batch 8 stuck), vocabulary drift. See plan file for phase order, ship criteria, and open questions.

**Phase 0 (doc hygiene)** — done. Stale "Batch Processing Pattern" removed from docs.

**Phase 1 (vocabulary source-of-truth)** — done. `vocab.py` is canonical; `agent_llm_parser`, `agent_image_analysis`, `fix`, `stage3_embed`, `review`, `agent_qc`, `diagnose` all import from it. `review.py` was silently validating against V1 vocab (bug); now fixed. `migrate_vocab.py` reports drift to `data/reports/vocab_migration.json`. Postgres schema extended with `vocab_version` + `prompt_version` columns.

**Key Phase 1 finding:** program/style/color_tone are clean. **Atmosphere has 2,784 (80%) out-of-vocab values** (organic, communal, historic, dramatic, etc.) — deferred to Phase 6 targeted re-processing with re-embedding, not a silent nullification.

**Phase 2 (structured output via tool use)** — done. `agent_llm_parser` and `agent_image_analysis` use forced `tool_choice` with vocab-derived `input_schema` enums. Zero manual JSON parsing on model output. `image_analysis_tool` is generated per call with/without atmosphere — yields two distinct `prompt_version` labels for eval scoring.

**Phase 3 (idempotent task queue)** — done. New `tasks_db.py` (SQLite at `data/tasks.db`, WAL mode) is the crash-safe ledger; JSON files remain canonical output. `pipeline_harness.py` rewritten as a queue-driven worker with `mark_done` committing BEFORE JSON append (so mid-write crashes don't cause duplicate API calls), `reset_stale` recovering abandoned tasks, exponential backoff on transient errors, and `prompt_version` tagging so prompt edits invalidate cached outputs automatically. New commands: `run.py harness --status`, `run.py harness --enqueue-only`. Batch 8 resume becomes: `pipeline.py crawl` → `pipeline.py export-dedup` → `run.py harness`. Verified on current 3,465-building state: status display works, enqueue_batch idempotent, all smoke tests pass (can't runtime-test the API call path without `anthropic` SDK in this shell).

**Phase 4 (eval harness)** — done. `eval.py` scores live prompts against `data/golden/buildings.json` (enum exact match, free-form cosine similarity via repo embedding model, Jaccard for lists). `label_golden.py` bootstraps the golden set with `--source current` (regression-guard; atmosphere values passed through `vocab.migrate_strict` so out-of-V2 junk is dropped) or `--source opus` (claude-opus-4-7 for ceiling signal). Output `data/reports/eval_report.json`.

**Phase 5 (prompt improvements — infrastructure)** — done. Few-shot examples mechanism in `agent_llm_parser`: reads `data/few_shot/enrich_examples.json` (empty by default), injects as user/assistant/tool_result triplets, auto-bumps `prompt_version`. Iteration itself (picking examples, measuring deltas) is user activity gated on golden-set + API access.

**Phase 6 (targeted re-processing)** — done. `reprocess.py` combines flagging sources (`--from-vocab-migration`, `--from-eval-report`, `--from-ids-file`), dry-run by default, `--apply` deletes stale task rows + removes JSON entries + re-enqueues. Dry-run verified: targets 2,784 / 3,465 buildings (precisely the atmosphere-drift set; no global rebuild). `tasks_db.delete_for_reprocess` added to bypass UNIQUE constraint on forced re-runs. Output `data/reports/reprocess_plan.json`.

**Phase 7 (cost / throughput)** — partial. Anthropic prompt caching added to both agents (`cache_control: ephemeral` on system prompt + tool schema). Message Batches API and parallel-worker automation deferred: Batches needs ~200 lines of packaging+polling; parallel workers need no code — `tasks_db.claim_next` is already atomic, just run `run.py harness` in multiple shells.

**Phase 8B (Claude-native agent orchestration)** — done. Adopted the make_web pattern (which I missed on the first exploration pass) with adaptations for make_db's batch / quality-loop reality. Six agents under `.claude/agents/`: `orchestrator` (opus, top-level router), `batch-worker` (sonnet, runs pipeline_harness), `quality-reviewer` (sonnet, interprets review/rate/diagnose), `reporter` (sonnet, refreshes REPORT.md), `researcher` (opus, vocab/threshold investigations, WebSearch+WebFetch), `upload-guard` (sonnet, pre-upload review gate — never runs upload). Plus `.claude/Goal.md` (vision + acceptance), `.claude/Task.md` (open/in-progress/resolved + Handoffs/Research-Ready), `.claude/WORKFLOW.md` (6 operational Cases + handoff signal vocabulary). CLAUDE.md updated to point at the new layer.

**Phase 8A (Python consolidation)** — done. Same session, executed acting as the orchestrator. **27 → 22 Python files** (-5):
- `quality.py` created — merged `review.py` + `fix.py` + `rate.py` + `diagnose.py` (~907 LOC consolidated). Each operation kept as a top-level function (`run_review`, `run_fix`, `run_rate`, `run_diagnose`) with shared helpers; CLI exposes them as `python3 quality.py {review,fix,rate,diagnose}`.
- `agent_qc.py` folded into `vocab.py` (`QCResult` + `check_building` moved). `pipeline_harness.py` import updated.
- `pipeline.py` deleted — its CLI (`crawl`, `export-dedup`, `embed-rate`, `status`) ported into `run.py`. Shared `_run_export_dedup()` helper extracted from the previous duplication between `cmd_make_db` and `pipeline.run_export_dedup`.
- `run.py` now exposes 12 subcommands across 4 groups (build / quality / vocab+eval+reprocess / upload). `migrate_vocab.py`, `label_golden.py`, `eval.py`, `reprocess.py` kept as standalone modules but invokable as `run.py {migrate-vocab,label-golden,eval,reprocess}`.
- All 12 CLI subcommands smoke-tested. Quality rating after consolidation: 97/100.

---

## Environment

```
PostgreSQL: Neon (ap-southeast-1)
R2 bucket:  archi-tinder (~7.09 GB / 10 GB free tier)
Python:     3.9
Claude:     Max plan subscription (for enrichment + image analysis sessions)
```
