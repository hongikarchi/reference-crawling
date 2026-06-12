# make_db — Technical Reference

Schema, vocabularies, pipeline architecture, tools, and runbooks. Operating
rules are in `CLAUDE.md`; live pipeline + DB state in `docs/dashboard.html`.

## 1. Pipeline architecture

Five stages; each owns one code subpackage and one `data/` sub-folder.

**Stage 1 — Crawl** (`crawl/<source>/` → `data/crawl/<source>.db`)
Per-source scrapers — divisare, architizer, archello, metalocus. Pure HTTP +
HTML parsing, no LLM. URL-only: image bytes are not downloaded at crawl time.
Each source has `db.py` (SQLite schema + `pending_*` queue), `parsers.py`,
`crawler.py` (phased CLI). New source = new directory here — see §7.

**Stage 2-3 — Enrich** (`enrich/`, `tools/d1_*`, `tools/d2_*`, `tools/e1_*`, `tools/e2_*`)
- **D-1 text enrichment** (`tools/d1_enrich_codex.py`): an LLM reads the source
  description → `program, style, color_tone, atmosphere, material_visual,
  visual_description`. Output is strictly vocab-validated with a retry loop.
- **D-2 image vision** (`tools/d2_cover_vision.py`): an LLM reads the cover
  image → the `image_derived` JSONB sub-object. *Audit note: D-2 output is
  not vocab-validated — the database's least reliable field.*
- **E-1** (`tools/e1_phash_dedup.py`): deterministic perceptual-hash image
  clustering.
- **E-2** (`tools/e2_vision_5type.py`): image-type classification
  (exterior / interior / drawing / aerial / detail).

**Stage 4 — Canonical** (`canonical/`, `tools/build_strict_canonical.py`)
Matches the 4 sources into one record per real building (divisare + architizer
as the base, metalocus + archello as match-or-drop enrichment), assigns a
`confidence_tier` (T1 ≥3 sources / T2 =2 / T3 =1), and assembles the strict
canonical artifact. Perceptual-hash false-merge gate in
`canonical/match_phash_check.py`.

**Stage 5 — Upload** (`tools/canonical_v2_neon_loader.py`,
`tools/canonical_v2_architects_neon_loader.py`)
Loads the embedded canonical artifact into Neon `canonical_v2_buildings`
and `canonical_v2_architects` (UPSERT on PK). User-gated. Cover images host
on Cloudflare R2.

Data flow: `data/crawl/*.db` → D-1/D-2/E-1/E-2 →
`canonical_buildings_strict_embedded.completeness_cN.json` → Neon.

## 2. Schema — Neon `canonical_v2_buildings`

```sql
CREATE TABLE canonical_v2_buildings (
  canonical_bld_id              TEXT        PRIMARY KEY,
  name                          TEXT        NOT NULL,
  names_alts                    TEXT[]      NOT NULL DEFAULT '{}',
  location_city                 TEXT,
  location_country              TEXT,
  project_year                  INTEGER,
  architect_canonical_ids       TEXT[]      NOT NULL DEFAULT '{}',
  architect_names               TEXT[]      NOT NULL DEFAULT '{}',
  architects_text               TEXT,
  program                       TEXT        NOT NULL,
  style                         TEXT        NOT NULL,
  color_tone                    TEXT        NOT NULL,
  atmosphere                    TEXT        NOT NULL,
  material_visual               TEXT[]      NOT NULL DEFAULT '{}',
  visual_description            TEXT        NOT NULL,
  image_derived                 JSONB       NOT NULL DEFAULT '{}',   -- D-2 vision
  covers_by_type                JSONB       NOT NULL DEFAULT '{}',
  all_images                    JSONB       NOT NULL DEFAULT '[]',
  best_image_per_cluster        JSONB       NOT NULL DEFAULT '{}',
  source_refs                   JSONB       NOT NULL,                -- {source: [ids]}
  source_urls                   JSONB       NOT NULL DEFAULT '{}',
  identity_source               TEXT,
  confidence_tier               TEXT        NOT NULL CHECK (confidence_tier IN ('T1','T2','T3')),
  n_sources                     INTEGER     NOT NULL CHECK (n_sources >= 1),
  cover_image_url_default       TEXT,
  cover_image_cdn_url           TEXT,
  cover_blurhash                TEXT,
  display_cover_url             TEXT,
  is_publishable                BOOLEAN     NOT NULL DEFAULT FALSE,
  publishability_reasons        TEXT[]      NOT NULL DEFAULT '{}',
  needs_image_derived_backfill  BOOLEAN     NOT NULL DEFAULT FALSE,
  typology_primary              TEXT,                                -- fine-grained use
  typology_primary_source       TEXT,                                -- source_tags|name|program
  typology_tags                 TEXT[]      NOT NULL DEFAULT '{}',
  architectural_elements        TEXT[]      NOT NULL DEFAULT '{}',
  source_categories             JSONB       NOT NULL DEFAULT '{}',    -- raw source taxonomy
  year_kind                     TEXT        NOT NULL DEFAULT 'unknown', -- completed | future | unknown (derived from project_year vs current_year)
  embedding                     VECTOR(384) NOT NULL,
  created_at                    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at                    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

Indexes: B-tree on country/city/year/program/style/tier/is_publishable,
`typology_primary`, and `year_kind`; GIN on `architect_canonical_ids`, `source_refs`,
`typology_tags`, `architectural_elements`, `source_categories`; HNSW
(`vector_cosine_ops`) on `embedding`. Embedding model:
`paraphrase-multilingual-MiniLM-L12-v2`, 384-dim. `python3
tools/canonical_v2_neon_loader.py --emit-sql` prints the authoritative DDL
plus the additive migration (`SCHEMA_EVOLUTION_SQL`).

## 2b. Schema — Neon `canonical_v2_architects` (firm DB)

Derived from `canonical_v2_buildings` + 3 source firm tables. Same Neon DB
(`archi_data`). Powers make_web's "swipe buildings → recommend firms" feature.

```sql
CREATE TABLE canonical_v2_architects (
  canonical_arch_id        TEXT        PRIMARY KEY,           -- arch_NNNNNN
  canonical_name           TEXT        NOT NULL,
  name_alts                TEXT[]      NOT NULL DEFAULT '{}',
  description              TEXT,
  primary_country          TEXT,                              -- portfolio modal (validated)
  primary_city             TEXT,                              -- modal city in primary_country
  office_locations         JSONB       NOT NULL DEFAULT '[]', -- archello/architizer offices merged
  website                  TEXT,                              -- https:// normalized
  email                    TEXT,                              -- always null (no source has email)
  phone                    TEXT,
  social_links             JSONB       NOT NULL DEFAULT '{}', -- {instagram, facebook, linkedin, ...} brand-leak filtered
  building_ids             TEXT[]      NOT NULL DEFAULT '{}', -- FK → canonical_v2_buildings.canonical_bld_id
  n_buildings              INTEGER     NOT NULL DEFAULT 0,
  n_buildings_publishable  INTEGER     NOT NULL DEFAULT 0,
  countries                TEXT[]      NOT NULL DEFAULT '{}', -- all countries in portfolio
  cities                   TEXT[]      NOT NULL DEFAULT '{}',
  top_programs             TEXT[]      NOT NULL DEFAULT '{}', -- top-5 frequency
  top_styles               TEXT[]      NOT NULL DEFAULT '{}',
  top_color_tones          TEXT[]      NOT NULL DEFAULT '{}',
  top_atmospheres          TEXT[]      NOT NULL DEFAULT '{}',
  top_materials            TEXT[]      NOT NULL DEFAULT '{}',
  top_typologies           TEXT[]      NOT NULL DEFAULT '{}',
  top_arch_elements        TEXT[]      NOT NULL DEFAULT '{}',
  feature_distribution     JSONB       NOT NULL DEFAULT '{}', -- full counters per field
  earliest_project_year    INTEGER,
  latest_project_year      INTEGER,
  source_refs              JSONB       NOT NULL,              -- {archello/architizer/divisare/metalocus: [ids]}
  source_urls              JSONB       NOT NULL DEFAULT '{}', -- per-source profile URL
  source_descriptions      JSONB       NOT NULL DEFAULT '{}', -- per-source bio
  n_sources                INTEGER     NOT NULL CHECK (n_sources >= 1),
  confidence_tier          TEXT        NOT NULL CHECK (confidence_tier IN ('T1','T2','T3')),
  logo_url                 TEXT,                              -- archello.cover_image_url only (~30% coverage)
  hero_building_id         TEXT,                              -- best-confidence publishable building
  portfolio_embedding      VECTOR(384) NOT NULL,              -- mean of publishable building embeddings; same space
  is_recommendable         BOOLEAN     NOT NULL DEFAULT FALSE,-- n_buildings_publishable >= 3 AND has source metadata
  created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

Indexes: B-tree on `primary_country/primary_city/is_recommendable/confidence_tier`;
GIN on `top_programs/top_typologies/countries`; HNSW (`vector_cosine_ops`) on
`portfolio_embedding`. Recommendation algorithm + SQL templates:
`docs/ARCHITECT_RECOMMENDATION.md`. Loader: `tools/canonical_v2_architects_neon_loader.py`.

**Coverage at C-A3**: 14,216 firms / 4,357 recommendable / 86 distinct countries.
25,417 registry firms without ≥1 building in canonical are not loaded.

## 2c. Schema — Neon tag precompute tables (make_web algo support)

Three sibling tables derived from publishable `canonical_v2_buildings` rows,
rebuilt in one transaction every crawl by `tools/canonical_v2_tag_stats_build.py`
(in-txn QC, FAIL → ROLLBACK). 6 axes: program / style / color_tone /
atmosphere / material_visual / architectural_elements. Contract + make_web
query crib: `docs/MAKEDB_ALGO_SUPPORT_RESPONSE.md`.

```sql
canonical_v2_tag_stats      (axis, tag) PK → doc_freq, total_n, idf, corpus_version, computed_at
canonical_v2_tag_centroids  (axis, tag) PK → centroid VECTOR(384) L2-normalized, n_buildings, …
canonical_v2_tag_vocabulary (axis, tag) PK → display_ko, display_en, is_generic, sort_rank
```

- `idf = ln(total_n/(doc_freq+1))`; R1 keys == R2 keys; R1 ⊆ R3 (LEFT JOIN).
- Labels/is_generic source: `data/canonical/tag_vocabulary_labels.json`,
  reviewed via `manual_review_workflow.py serve` vocab cards →
  `apply-vocab-labels`.
- material_visual is free-text: ~18.6k distinct tags, ~13k singletons —
  consumers should filter `doc_freq` or weight by `n_buildings`.

## 3. Controlled vocabularies

`core/vocab.py` is the source of truth (`VOCAB_VERSION = "v2"`). Never edit it
without explicit user approval.

- **program** (14): Housing, Office, Museum, Education, Religion, Sports,
  Transport, Hospitality, Healthcare, Public, Mixed Use, Landscape,
  Infrastructure, Other
- **style** (12): Minimalist, Brutalist, High-Tech, Postmodern, Vernacular,
  Contemporary, Deconstructivist, Industrial, Neo-Classical, Organic,
  Modernist, Parametric
- **color_tone** (8): Monochrome, Warm, Cool, Earth, Vibrant, Neutral, Dark, Light
- **atmosphere** (12): Serene, Dynamic, Raw, Warm, Urban, Industrial, Playful,
  Monumental, Intimate, Futuristic, Rustic, Contemplative
- **material_visual** (suggested, not strict): concrete, glass, timber, brick,
  stone, steel, corten, aluminum, copper, plaster, tile, marble, rammed earth,
  bamboo, polycarbonate, fabric
- **typology** (35): fine-grained building use — House, Apartment, Housing,
  Student Housing, Care Home, Office, Retail, Restaurant, Hotel, Shopping
  Centre, Museum, Gallery, Library, Theatre, Concert Hall, School, University,
  Kindergarten, Hospital, Civic Building, Bank, Religious Building, Sports
  Centre, Stadium, Pavilion, Airport, Train Station, Car Park, Industrial,
  Warehouse, Winery, Park, Bridge, Memorial, Mixed Use
- **architectural_element** (14): Stair, Facade, Roof, Courtyard, Entrance,
  Corridor, Atrium, Terrace, Balcony, Garden, Fireplace, Column, Canopy,
  Skylight

The top-level `style/color_tone/atmosphere` columns are vocab-clean. The
`image_derived` JSONB sub-object (D-2) is NOT — ~24% out-of-vocab; see the audit.

## 4. Canonical artifact lineage

Canonical artifacts are **immutable stage snapshots** — never mutated in place.
A "completeness Cn" pass reviews a small evidence-backed set of location/year
backfills, writes a new `completeness_cN` artifact + an affected-rows file,
runs QC, then upserts only the affected rows to Neon. Lineage to date:
`resume10_complete` → C3 → C4 → C6 → C7 → **C8 (current Neon release)** → C9
(post-audit data corrections) → C10 (matcher-recall recovery — drops 107 merge
duplicates) → **C11 (fine-grained taxonomy; 39,669 rows, built + QC-clean,
Neon upsert pending user approval)**.
Artifacts live under `data/canonical/country_conflict_refresh/`.

## 5. Tool inventory (`tools/`)

- **Build / QC:** `build_strict_canonical.py`, `qc_strict.py`, `embed_strict.py`,
  `canonical_v2_upload_validator.py`, `canonical_v2_neon_loader.py`
- **Completeness:** `canonical_v2_gap_inventory.py`,
  `build_completeness_c9.py`, `build_completeness_c11_taxonomy.py`,
  `canonical_v2_crawler_gap_audit.py`
- **Taxonomy + recall (C10/C11):** `taxonomy_tag_inventory.py`,
  `build_typology_crosswalk.py`, `build_completeness_c11_taxonomy.py`,
  `canonical_v2_matcher_recall_audit.py`, `canonical_v2_recover_dropped_twins.py`
- **Enrichment:** `d1_enrich_codex.py`, `d2_cover_vision.py`,
  `e1_phash_dedup.py`, `e2_vision_5type.py`, `image_dedup_5type.py`
- **Audit:** `audit_canonical_data_integrity.py`,
  `canonical_v2_full_reaudit.py`, `canonical_v2_architects_audit.py`
- **make_web precompute:** `canonical_v2_tag_stats_build.py` (tag stats /
  centroids / vocabulary, --discover/--dry-run/--build), `r4_axis_smoke.py`
  (proposed-axis LLM smoke)
- **Manual review:** `manual_review_workflow.py` (audit/snapshot/serve
  dashboard/apply; case + term + vocab_label cards), `cover_review_app.py`
- **Dashboard:** `build_dashboard.py`

`run.py` is the legacy metalocus-pipeline dispatcher. `tools/` has accumulated
one-off scripts from iterative work; `data/reports/audit/cleanup_final.md`
records what the 2026-05 cleanup retires.

## 6. Crawler content filter (metalocus)

`crawl/metalocus/crawler.py` filters non-building articles before saving:
drops junk tags (`metalocus music project`, …), junk-title keywords
(`music video`, `pritzker`, `obituary`, …), and rows with no architect AND no
area AND no building-type. Other crawlers do not filter.

## 7. Adding a new crawl source — runbook

1. **Recon** → write `.claude/research/<source>-schema.md`: site overview,
   access policy, `robots.txt` (+ `ai-train=no` flags), anti-bot, URL
   patterns, data shape (sample fetch), pagination, rate-limit estimate,
   feasibility verdict.
2. **User policy gate** — if `ai-train=no` or a ToS scrape ban is present,
   stop until the user decides. Do not write crawler code before this clears.
3. **Scaffold** `crawl/<source>/{__init__,db,parsers,crawler}.py` — mirror
   `crawl/architizer/` (sitemap-driven public) or `crawl/divisare/`
   (authenticated). One entity table per noun, one `pending_*` queue per
   discovery axis with `status` enum.
4. **`core/config.py`** — add the source's base URL, request delay (≥2 s),
   user-agent, DB path, auth constants.
5. **Smoke** — sitemap phase populates the queue; deep-fetch `--limit 10`;
   inspect 3-5 rows; then a ~100-row batch watching for parse errors.
6. **Wire into canonical** — extend `canonical/` matching to read the new
   source's DB; fold into the strict-canonical builder with per-field
   provenance.

## 8. Stages vs phases

**Stages (1-5)** are the fixed structure of the data flow — always the same.
**Phases / "completeness Cn"** are time-ordered work units (a feature, a
backfill). One phase usually touches one stage. Run history is in
`.claude/ops/jobs/`.
