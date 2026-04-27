# make_db — Project Specification

> Source of truth for all schemas, vocabularies, tool specs, and the agent loop.

---

## 1. What This Does

Builds a vector database of architecture projects for ArchiTinder — a swipe-based recommendation app. Users swipe buildings (like/skip) and the engine finds visually + programmatically similar ones using vector similarity (Netflix-style).

**Two repos:**
- `make_db` — builds and maintains the database (this repo)
- `make_web` — Django backend + React frontend that reads from it

---

## 2. Architecture

```
metalocus.es
    │
    ▼  crawler.py — 4 phases: discover → listings → articles → images
SQLite (metalocus.db) + images/{slug}/
    │
    ▼  stage1_export.py + stage2_dedup.py
1_buildings_raw.json
    │
    ▼  Claude: text enrichment (name_en, program, material, atmosphere)
2_buildings_enriched.json
    │
    ▼  Claude: image analysis (style, color_tone, material_visual, visual_description)
3_buildings_analyzed.json
    │
    ▼  stage3_embed.py — 384-dim embedding from all fields
4_buildings_final.json
    │
    ▼  quality.py (review → fix → rate) → [loop until quality passes]
    │
    ▼  Claude reports to user → user approves → upload.py
PostgreSQL (Neon) + Cloudflare R2
```

---

## 3. File Structure

5-stage subpackage layout (mirrors the workflow). New crawl source =
new directory under `crawl/`.

```
make_db/
├── CLAUDE.md                  ← Claude session instructions
├── .claude/PROJECT.md         ← This file: full spec
├── .claude/REPORT.md          ← Current state + status
├── run.py                     ← Unified CLI (sole script at root)
├── requirements.txt
├── .env                       ← gitignored
│
├── core/                      ← Shared infrastructure
│   ├── vocab.py               ← Canonical vocab + migrations + QC
│   ├── config.py              ← Paths + rate limits + constants
│   └── utils.py               ← Logger, rate limiter, HTTP, slugs
│
├── crawl/                     ← Stage 1: per-source raw scraping
│   ├── metalocus/
│   │   ├── crawler.py         ← 4-phase orchestrator + filter
│   │   ├── parsers.py         ← HTML parsing
│   │   ├── database.py        ← metalocus.db CRUD
│   │   ├── models.py          ← BuildingData, ImageData
│   │   └── downloader.py      ← Image downloader
│   └── divisare/
│       ├── crawler.py         ← Authenticated 4-phase crawler
│       ├── auth.py            ← Login + session cookie
│       ├── db.py              ← divisare.db CRUD
│       └── parsers.py         ← HTML parsing for Divisare
│
├── enrich/                    ← Stages 2-3: text + image LLM
│   ├── export.py              ← SQLite → 1_buildings_raw.json
│   ├── dedup.py               ← Dedup + ID assignment
│   ├── embed.py               ← Embeddings → 4_buildings_final.json
│   ├── harness.py             ← Queue-driven AI worker
│   ├── llm_parser.py          ← Text enrichment (Anthropic SDK)
│   ├── image_analysis.py      ← Image analysis (Anthropic SDK)
│   ├── tasks_db.py            ← AI task queue ledger (SQLite)
│   ├── quality.py             ← review + fix + rate + diagnose
│   ├── eval.py                ← Score prompts vs golden
│   ├── label_golden.py        ← Seed eval golden set
│   ├── migrate_vocab.py       ← Vocab migration audit/apply
│   └── reprocess.py           ← Targeted re-processing plan/apply
│
├── canonical/                 ← Stage 4: matching + canonical artefact
│   ├── schema.py              ← CanonicalBuilding dataclass
│   ├── consolidate.py         ← metalocus architect alias clusters
│   ├── match_architects.py    ← cluster ↔ Divisare architect
│   ├── match_buildings.py     ← metalocus building ↔ Divisare project
│   ├── match_to_canonical.py  ← legacy single-pass matcher
│   ├── manual_tiebreaks.py    ← apply manual review decisions
│   ├── build.py               ← assemble canonical_buildings.json
│   └── qc.py                  ← 9 invariant checks
│
├── upload/                    ← Stage 5: Neon + R2 (manual gate)
│   ├── neon.py                ← legacy 4_buildings_final upload + R2
│   └── neon_strict.py         ← strict canonical → in-place migration
│
├── tools/                     ← Dev utilities (not in pipeline)
│   ├── divisare_server.py     ← Local Divisare gallery server
│   ├── generate_gallery.py    ← HTML gallery generator
│   ├── gallery.html
│   └── divisare_gallery.html
│
├── data/                      ← Data artefacts (root regardless of producer)
│   ├── metalocus.db
│   ├── divisare.db
│   ├── tasks.db
│   ├── id_registry.json       ← NEVER delete
│   ├── metalocus_architect_clusters.json
│   ├── 1_buildings_raw.json … 4_buildings_final.json
│   ├── canonical_buildings.json + canonical_buildings_strict.json
│   ├── match/
│   │   ├── metalocus_architect_to_divisare.json
│   │   └── metalocus_to_divisare_buildings.json
│   └── reports/
│       ├── review_report.json
│       ├── rating_report.json
│       ├── canonical_qc.json
│       └── canonical_qc_strict.json
│
└── images/                    ← {building_id}/{n}_{slug}_{caption}.jpg
    └── {building_id}/
        ├── 0_cover.jpg
        └── 1_interior.jpg
```

---

## 4. PostgreSQL Schema

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE architecture_vectors (
    building_id        TEXT PRIMARY KEY,     -- e.g. 'B00042', stable
    slug               TEXT UNIQUE NOT NULL,
    name_en            TEXT NOT NULL,        -- canonical English name
    project_name       TEXT NOT NULL,        -- display name, may be non-English
    architect          TEXT,
    location_country   TEXT,
    city               TEXT,
    year               INTEGER,
    area_sqm           NUMERIC,
    program            TEXT NOT NULL,        -- Section 5.1
    style              TEXT,                 -- Section 5.2, from image
    atmosphere         TEXT,                 -- Section 5.5, from enrichment/image
    color_tone         TEXT,                 -- Section 5.3, from image
    material           TEXT,                 -- from article text
    material_visual    TEXT[],               -- Section 5.4, from image
    description        TEXT,
    visual_description TEXT,                 -- prose from image, feeds embedding
    url                TEXT,
    tags               TEXT[],
    source_slugs       TEXT[],
    image_photos       TEXT[],               -- up to 3 filenames
    image_drawings     TEXT[],               -- up to 3 filenames
    vocab_version      TEXT DEFAULT 'v1',    -- vocab.py VOCAB_VERSION at write time
    prompt_version     TEXT,                 -- "{label}-{sha256(prompt)[:8]}"
    embedding          VECTOR(384) NOT NULL
);

CREATE INDEX ON architecture_vectors (program);
CREATE INDEX ON architecture_vectors (style);
CREATE INDEX ON architecture_vectors (color_tone);
CREATE INDEX ON architecture_vectors (location_country);
CREATE INDEX ON architecture_vectors (year);
```

**Embedding text** (what gets encoded into the vector):
```
name_en + architect + location_country + program + style + atmosphere +
color_tone + material_visual (joined) + visual_description + description
```

---

## 5. Controlled Vocabularies

Canonical source of truth: `vocab.py` (`VOCAB_VERSION = "v2"`). The lists below
mirror the code — if they diverge, `vocab.py` wins. Legacy v1 values (e.g.
"Expressionist", "Warm White") are migrated in `vocab.LEGACY_MIGRATIONS`.

### 5.1 `program` (14 values, unchanged v1→v2)
Housing, Office, Museum, Education, Religion, Sports, Transport,
Hospitality, Healthcare, Public, Mixed Use, Landscape, Infrastructure, Other

### 5.2 `style` (12 values, v2)
Minimalist, Brutalist, High-Tech, Postmodern, Vernacular, Contemporary,
Deconstructivist, Industrial, Neo-Classical, Organic, Modernist, Parametric

### 5.3 `color_tone` (8 values, v2)
Monochrome, Warm, Cool, Earth, Vibrant, Neutral, Dark, Light

### 5.4 `material_visual` (suggested, not strict)
concrete, glass, timber, brick, stone, steel, corten, aluminum, copper,
plaster, tile, marble, rammed earth, bamboo, polycarbonate, fabric

### 5.5 `atmosphere` (12 values, enforced as enum)
Serene, Dynamic, Raw, Warm, Urban, Industrial, Playful,
Monumental, Intimate, Futuristic, Rustic, Contemplative

---

## 6. Data File Sequence

| File | Produced by | Contains |
|------|------------|---------|
| `1_buildings_raw.json` | stage1_export + stage2_dedup | slug, project_name, architect, images, tags |
| `2_buildings_enriched.json` | Claude text enrichment | + name_en, program, material, atmosphere |
| `3_buildings_analyzed.json` | Claude image analysis | + style, color_tone, material_visual, visual_description |
| `4_buildings_final.json` | stage3_embed | + embedding (384-dim array) |

Each step adds fields. Earlier fields are preserved.

---

## 7. Tool Specifications

### `stage1_export.py`
- Reads SQLite → writes `1_buildings_raw.json`
- Skips buildings with no title or no cover image
- Renames images: `{filename}` → `{order}_{filename}` on disk
- Classifies images: photo vs drawing (alt_text keywords)
- Selects upload candidates: best 3 photos + 3 drawings per building

### `stage2_dedup.py`
- Preliminary embedding (throwaway) → fuzzy name match → cosine confirm
- Merges duplicates (keep richer, union images + tags)
- Assigns stable `building_id` (B00001 format)
- Moves `images/{slug}/` → `images/{building_id}/`
- Updates `id_registry.json`

### `stage3_embed.py`
- Reads `3_buildings_analyzed.json`
- Builds embedding text from all enriched + visual fields
- Encodes with `paraphrase-multilingual-MiniLM-L12-v2` → 384-dim
- Writes `4_buildings_final.json`

### `quality.py` (review + fix + rate + diagnose, consolidated in Phase 8A)
Invoke via `python3 run.py quality {review,fix,rate,diagnose}` or directly
`python3 quality.py <cmd>`.

**review**: required fields, null counts, vocabulary compliance, vocab_version
stamp, duplicate IDs, embedding dimensions. Output `review_report.json`:
```json
{
  "status": "pass|fail|warning",
  "field_coverage": { "style": {"filled": 255, "pct": 0.85} },
  "issues": [{"type": "null_field", "field": "style", "count": 45}],
  "recommendations": ["Run image analysis on 45 buildings missing style"]
}
```

**fix**: auto-normalize program/style/color_tone via `vocab.normalize_loose`;
fill atmosphere from legacy `mood`; clean architect / name_en / country.
Cannot auto-fix: missing visual_description, missing name_en. Writes an audit
log of every vocab rewrite to `fix_report.json`.

**rate**: scores 6 dimensions (0–100 each). Output `rating_report.json`:
```json
{
  "overall": 72,
  "dimensions": {
    "completeness":   {"score": 91, "note": "name_en 100%, style 85%"},
    "visual_depth":   {"score": 58, "note": "visual_description only 70% filled"},
    "description":    {"score": 78, "note": "avg 180 chars, target 200"},
    "image_coverage": {"score": 82, "note": "avg 2.8 photos, 1.1 drawings"},
    "diversity":      {"score": 64, "note": "Housing 42% — too high"},
    "scale":          {"score": 60, "note": "300 buildings, target 500"}
  },
  "weaknesses": ["visual_depth is lowest — run image analysis on missing buildings"],
  "judgment": "Not ready. Visual fields incomplete and count below target."
}
```

### `upload.py`
- UPSERT all buildings from `4_buildings_final.json` into PostgreSQL
- Create/update vector index (HNSW < 300 rows, IVFFlat ≥ 300 rows)
- Upload `image_photos` + `image_drawings` to R2
- **Only runs after user explicitly approves**

---

## 8. Crawler Content Filter

In `crawler.py:phase_articles()`, after `parse_article_page()`, before `db.save_building()`:

```python
_JUNK_TAGS = {"metalocus music project", "metalocus recommends"}
_JUNK_TITLES = [
    "music video", "video clip", "pritzker", "riba gold medal",
    "happy holidays", "merry christmas", "best for 20", "obituary",
]

def is_building_project(data) -> bool:
    if {t.lower() for t in data.tags} & _JUNK_TAGS:
        return False
    if any(kw in (data.title or "").lower() for kw in _JUNK_TITLES):
        return False
    if not data.architects and not data.area_sqm and not data.building_type:
        return False
    return True
```

If False → `db.mark_article_skipped()` → skip image download.

---

## 9. Agent Loop

The operational workflow lives in `.claude/WORKFLOW.md` — six Cases covering
new batch processing, quality iteration, targeted re-processing, prompt
tuning, vocabulary evolution, and the upload approval gate. Six agents
(`orchestrator`, `batch-worker`, `quality-reviewer`, `reporter`, `researcher`,
`upload-guard`) under `.claude/agents/` implement those Cases.

The single quality judgment that overrides any metric:

> **Will architects get meaningful, visually accurate recommendations from this database?**

If `quality rate` says 96/100 but the answer is no, it's no. If it says 72
and the answer is yes for the use case at hand, that's enough.

---

## 10. Quality Targets

Claude uses these as guidance, not hard gates. Judgment matters more than scores.

```
minimum_buildings:    500
required 100%:        building_id, name_en, program, embedding
required  90%:        architect, description, atmosphere, style
required  80%:        visual_description, material_visual, color_tone
program balance:      no single program > 35% of total
visual coverage:      avg ≥ 1 photo per building, ≥ 40% have ≥ 1 drawing
description length:   avg ≥ 200 chars
```
