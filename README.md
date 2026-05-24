# make_db

Builds and maintains the vector database behind **ArchiTinder** — a swipe-based
architecture recommendation app. This repo is the data pipeline; `make_web` is
the consumer app that reads from it.

## What it does

Crawls architecture-publication sites, enriches each building with LLM-derived
descriptive and visual metadata, consolidates four sources into one canonical
record per building, embeds it as a vector, and uploads to Neon Postgres
(pgvector) + Cloudflare R2.

The database's worth is judged by a single question:

> **Will an architect get meaningful, visually accurate recommendations from it?**

Metrics serve that judgment — not the reverse.

## Current state

- **Production table:** Neon `canonical_v2_buildings` — 39,776 buildings
  (release `completeness_c8`).
- **Quality:** independently audited 2026-05 → **PASS with WARNINGS**. Core
  fields (name, architect, location, year, program) measure 98.8–100% accurate;
  the AI image-analysis field `image_derived` is the known weak spot. Full
  report: `data/reports/db_quality_audit.md`.
- **Sources crawled:** divisare, architizer, archello, metalocus.

## Navigate

- **See the pipeline + DB state** → open `docs/dashboard.html` in a browser.
- **Operate the pipeline** → `CLAUDE.md` is the operating manual.
- **Technical reference** (schema, vocabularies, tools, runbooks) → `docs/REFERENCE.md`.
- **Architects recommendation** (schema + SQL templates) → `docs/ARCHITECT_RECOMMENDATION.md`.
- **Run history** → `.claude/ops/jobs/`.

## Repository layout

```
core/             shared infrastructure (vocab, config, utils)
crawl/<source>/   stage 1 — per-source crawlers
enrich/           stages 2-3 — LLM text + image enrichment
canonical/        stage 4 — matching + canonical consolidation
tools/            stage 5 (canonical_v2_neon_loader.py) + audit/dashboard scripts
data/             data artifacts (gitignored)
docs/             reference docs + the web dashboard
.claude/ops/      job cards = run history
```

## Quickstart

```bash
python3 tools/canonical_v2_neon_loader.py --inspect-table   # inspect the live DB
python3 tools/build_dashboard.py                            # refresh docs/dashboard.html
```

Pipeline operations are described in `CLAUDE.md` and `docs/REFERENCE.md`.

## Principles

Batch-oriented, idempotent, smoke-tested before scaling, upload user-gated.
No metric-gaming, no unapproved uploads, no silent vocabulary changes.
