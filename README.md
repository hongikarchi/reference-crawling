# make_db — ArchiTinder Database Builder

3-stage pipeline: crawl metalocus.es → ML enrich + embed → load PostgreSQL.

## Setup

```bash
pip install -r requirements.txt
```

Create `.env` in the project root:
```env
DB_HOST=localhost
DB_PORT=5432
DB_USER=<your_user>
DB_PASSWORD=<your_password>
DB_NAME=architon
```

## Usage

### Full pipeline

```bash
python3 run.py make-db --limit 20    # crawl until 20 total buildings, then process
python3 run.py make-db --limit 100   # crawl until 100 total buildings
python3 run.py make-db               # unlimited — crawl everything
```

This runs: crawl → export → dedup + assign IDs → **pause for Claude enrichment**.

After the enrichment session:
```bash
python3 run.py post-enrich           # embed + load into PostgreSQL
```

Check progress at any time:
```bash
python3 run.py stats
```

### Incremental runs

The pipeline is fully incremental. Re-running `make-db --limit 100` when you already have 40 buildings will crawl only 60 more — no duplicates.

## Project Structure

```
run.py                  # Unified CLI entry point
stage1_crawl.py         # Stage 1: export SQLite → 1_buildings_raw.json
stage2_ml.py            # Stage 2: dedup + enrich + embed
stage3_postgres.py      # Stage 3: bulk INSERT into PostgreSQL
crawler.py              # 4-phase crawl orchestrator
database.py             # SQLite operations
parsers.py              # HTML parsing
downloader.py           # Image downloader with retry
models.py               # Dataclasses (BuildingData, ImageData)
utils.py                # Logger, rate limiter, HTTP helpers
config.py               # Configuration constants

data/
├── metalocus.db                  # SQLite (Stage 1 internal)
├── 1_buildings_raw.json          # Stage 1 output
├── 2_buildings_to_enrich.json    # Stage 2 pre-enrich output
├── 3_buildings_enriched.json     # Claude enrichment output
└── 4_buildings_processed.json    # Stage 2 final output → Stage 3 input

images/
└── {building_id}/                # e.g. B00001/, B00042/
    ├── 0_cover.jpg
    └── 1_gallery.jpg
```

## Pipeline Overview

```
Stage 1 — CRAWL
  metalocus.es → SQLite + images/
  └─► stage1_crawl.py --export → 1_buildings_raw.json

Stage 2 — ML
  1_buildings_raw.json
  └─► --pre-enrich: dedup + assign IDs → 2_buildings_to_enrich.json
  └─► Claude enrichment session → 3_buildings_enriched.json
  └─► --post-enrich: embed (384-dim) → 4_buildings_processed.json

Stage 3 — POSTGRESQL
  4_buildings_processed.json → architecture_vectors table
```

## Output Schema

See `00_FLOW.md` Section 4 for the full shared database contract.
PostgreSQL table: `architecture_vectors` with pgvector `VECTOR(384)` embedding column.
