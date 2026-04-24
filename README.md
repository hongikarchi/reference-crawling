# make_db — ArchiTinder Database Builder

Crawl `metalocus.es` → enrich + analyze with Anthropic API → embed → upload to
Neon Postgres + Cloudflare R2. Produces the vector database that powers the
ArchiTinder swipe recommendation engine.

## Setup

```bash
pip install -r requirements.txt
```

Create `.env` in the project root:
```env
ANTHROPIC_API_KEY=<anthropic_key>          # for enrichment + image analysis
DB_HOST=localhost
DB_PORT=5432
DB_USER=<your_user>
DB_PASSWORD=<your_password>
DB_NAME=architon
R2_ACCOUNT_ID=<cloudflare_account_id>
R2_ACCESS_KEY_ID=<r2_key>
R2_SECRET_ACCESS_KEY=<r2_secret>
R2_BUCKET=<bucket_name>
```

## Quick Start

```bash
# crawl + dedup the next batch
python3 run.py make-db --limit 20

# run AI enrichment + image analysis (queue-driven, crash-safe)
python3 run.py harness

# embed + run quality review/fix/rate
python3 run.py embed-rate

# inspect status
python3 run.py stats
python3 run.py harness --status

# upload (manual gate — agent layer never runs this)
python3 upload.py --dry-run
python3 upload.py
```

`python3 run.py --help` lists all 12 subcommands.

## Pipeline

```
metalocus.es
  └─► crawler         → SQLite + images/{slug}/
        └─► stage1_export    → 1_buildings_raw.json
        └─► stage2_dedup     → 1_buildings_raw.json (with stable building_id)
              └─► pipeline_harness (Anthropic API, queue-driven, crash-safe)
                    ├─► agent_llm_parser     → 2_buildings_enriched.json
                    └─► agent_image_analysis → 3_buildings_analyzed.json
                          └─► stage3_embed   → 4_buildings_final.json
                                └─► quality (review → fix → rate → diagnose)
                                      └─► upload (Neon Postgres + Cloudflare R2)
```

Operations are routed through `run.py`. AI calls go through `pipeline_harness`
which uses a SQLite task queue (`data/tasks.db`) for crash-safe idempotent
processing — restarting after a crash never re-pays for completed API calls.

## Project Structure

```
run.py                      # Unified CLI (12 subcommands)
config.py                   # Constants

# Crawl
crawler.py                  # 4-phase orchestrator
parsers.py                  # HTML parsing
database.py                 # SQLite CRUD
downloader.py               # Image downloader with retry
models.py                   # BuildingData / ImageData dataclasses
utils.py                    # Logger, rate limiter, HTTP

# Pipeline stages
stage1_export.py            # SQLite → 1_buildings_raw.json
stage2_dedup.py             # Dedup + assign stable building_ids
stage3_embed.py             # Sentence-transformer embeddings → 4_buildings_final.json

# AI agents (Anthropic SDK, tool_use, prompt caching)
agent_llm_parser.py         # Text enrichment (1_ → 2_)
agent_image_analysis.py     # Vision analysis (2_ → 3_)
pipeline_harness.py         # Queue-driven worker (uses tasks_db)

# Foundations
vocab.py                    # Canonical vocabularies + migrations + per-building QC
tasks_db.py                 # SQLite task ledger (Phase 3)

# Quality + workflows
quality.py                  # review + fix + rate + diagnose (subcommands)
migrate_vocab.py            # Vocab migration audit + apply
label_golden.py             # Seed eval golden set (current values or Opus)
eval.py                     # Score current prompts vs golden set
reprocess.py                # Targeted re-processing for flagged records

# Upload
upload.py                   # Postgres UPSERT + R2 image upload (manual gate)
```

```
data/
├── metalocus.db                          # SQLite — crawler state + content
├── id_registry.json                      # NEVER delete — building_id stability
├── 1_buildings_raw.json
├── 2_buildings_enriched.json
├── 3_buildings_analyzed.json
├── 4_buildings_final.json                # Ready for upload
├── tasks.db                              # AI task queue ledger
├── duplicates_review.json                # stage2_dedup uncertain pairs
├── golden/buildings.json                 # Eval golden set
├── few_shot/enrich_examples.json         # Optional few-shot for enrichment
└── reports/
    ├── review_report.json
    ├── fix_report.json
    ├── rating_report.json
    ├── eval_report.json
    ├── vocab_migration.json
    └── reprocess_plan.json

images/
└── {building_id}/                        # e.g. B00001/
    ├── 0_cover.jpg
    └── 1_interior.jpg
```

## Output Schema

PostgreSQL table `architecture_vectors` with `VECTOR(384)` embedding column.
Full schema in `.claude/PROJECT.md` §4.

## Where to look next

- **Operating the pipeline** — `.claude/WORKFLOW.md` (six Cases: new batch /
  quality iteration / re-processing / prompt tuning / vocab evolution / upload)
- **Architecture spec** — `.claude/PROJECT.md` (schemas, vocabularies, tool specs)
- **Current state** — `.claude/REPORT.md` (counts, quality, known gotchas)
- **Vision + quality targets** — `.claude/Goal.md`
- **Agent definitions** — `.claude/agents/{orchestrator,batch-worker,quality-reviewer,reporter,researcher,upload-guard}.md`
- **Working with Claude in this repo** — `CLAUDE.md`
