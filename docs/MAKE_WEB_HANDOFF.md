# make_db → make_web Handoff

## Final canonical artifact

**Path**: `data/canonical/canonical_buildings_4source.json`
**Size**: 146,432 canonical buildings
**Format**: JSON `{"summary": {...}, "buildings": [...]}`

## Per-building schema

```json
{
  "canonical_bld_id":      "bld_142531",
  "canonical_arch_ids":    ["arch_000370", "arch_001102"],
  "primary_name":          "Apple Park",
  "all_names":             ["Apple Park", "Apple Headquarters Cupertino", "APPLE PARK"],
  "source_refs": {
    "divisare":   ["12345"],
    "architizer": ["foster-partners-apple-park"],
    "archello":   ["41023"],
    "metalocus":  ["B00214"]
  },
  "country":               "USA",
  "city":                  "Cupertino",
  "year":                  2017,
  "typology":              "Office",
  "covers_by_source": {
    "divisare":   "https://images.divisare.com/.../12345/cover.jpg",
    "architizer": "https://images.architizer.com/.../foster-partners-apple-park/cover.jpg",
    "archello":   "https://archello.s3.amazonaws.com/.../41023/cover.jpg",
    "metalocus":  "images/B00214/0_metalocus_apple-park_cover.jpg"
  },
  "gallery_urls_by_source": {
    "divisare":   ["...url1", "...url2", ...],
    "architizer": [...],
    "archello":   [...],
    "metalocus":  ["images/B00214/0_metalocus_apple-park_001.jpg", ...]
  },
  "n_sources":             4,
  "n_members":             8
}
```

**Note on cover URLs**:
- 3 sources (divisare/architizer/archello) provide direct hot-link URLs.
  No download needed — cite the source URL directly.
- metalocus uses local file paths (`images/{building_id}/...`) — these
  are on disk now, but Stage G upload to R2 will rewrite them to R2
  URLs.

## Confidence tiers (DERIVED — make_web computes from `n_sources`)

| Tier | Definition | Count | Use |
|---|---|---|---|
| **T1** | n_sources ≥ 3 | 406 | High-confidence cross-verified buildings; safe to surface prominently |
| **T2** | n_sources = 2 | 10,177 | Cross-verified between 2 sources; very reliable |
| **T3** | n_sources = 1 | 135,849 | Single-source; possibly less canonical (could be misnamed/typo'd source listings) |

## make_web responsibilities (per Stage B/F decisions)

### 1. Image type classification + per-type cover selection
Each `gallery_urls_by_source[src]` is a flat list — no type tags
upstream. make_web should classify each image into one of 5 types:
**exterior / interior / drawing / aerial / detail**

Methods:
- Filename heuristic (drawing/aerial keywords) — fastest
- Visual classification (Vision LLM or vision model) — for the rest

Then pick best image per type:
- Priority: source `image_order=0` → source priority (architizer >
  divisare > archello > metalocus)
- Output schema in DB: `covers_by_type: {exterior, interior, drawing,
  aerial, detail}`

### 2. Primary cover selection (the ONE image to display)
This is intentionally a make_web decision because it depends on user
intent (search query, browse view, etc).

Default fallback: source-priority `image_order=0` (i.e., `covers_by_source`
field directly, in source priority order: architizer > divisare > archello
> metalocus).

### 3. Architect display
- Use `canonical_arch_ids` to join with the architects table.
- Stage A architects export: `data/canonical/architects_canonical.json`
- 39,423 active architect canonicals; 6,640 multi-source.
- For collab buildings (`len(canonical_arch_ids) > 1`), display all.

## DB schema additions for Postgres `architecture_vectors`

```sql
ALTER TABLE architecture_vectors
  ADD COLUMN canonical_bld_id   TEXT UNIQUE,
  ADD COLUMN canonical_arch_ids TEXT[],
  ADD COLUMN n_sources          INT,           -- for tier derivation
  ADD COLUMN source_refs        JSONB,         -- {divisare, architizer, archello, metalocus}
  ADD COLUMN covers_by_source   JSONB,         -- 4 URLs per building
  ADD COLUMN covers_by_type     JSONB,         -- exterior, interior, drawing, aerial, detail
                                                -- (filled by make_web after classification)
  ADD COLUMN gallery_urls       JSONB;         -- per-source array map
```

PRIMARY KEY: keep existing `building_id` for back-compat; `canonical_bld_id`
is the new authoritative ID. Migrate views/queries to `canonical_bld_id`
when convenient.

## Out of scope (deferred to subsequent stages)

- **Stage D**: Description / visual_description / atmosphere / style /
  color_tone enrichment via LLM. Not yet run on the new 4-source data.
  The legacy 3,465 metalocus-only enriched fields remain in
  `4_buildings_final.json` and are NOT carried into
  `canonical_buildings_4source.json` (clean separation).
  When Stage D runs, it should:
  - Concat all source descriptions for each canonical_bld_id
  - LLM enrich (text + cover image vision)
  - Add fields back to the canonical artifact

- **Stage E**: phash-based cross-source image dedup.
  Currently each source's gallery URLs are kept separately (no overlap
  detection). When phash dedup runs, it'll detect "same photo in
  divisare and architizer" and group them.

- **Stage G**: R2 upload of metalocus's local images + Neon write.
  Manual gate via upload-guard agent. Not auto-triggered.

## Stage A architects file

**Path**: `data/canonical/architects_canonical.json`

Schema:
```json
{
  "summary": {
    "n_canonicals": 39423,
    "multi_source": 6640,
    ...
  },
  "clusters": [
    {
      "canonical_arch_id": "arch_000370",
      "canonical_name":    "Foster + Partners",
      "names":             ["Foster + Partners", "Foster Partners", "Foster + Partners (FP)"],
      "source_refs":       {"divisare": [...], "architizer": [...], ...},
      "n_sources":         4,
      "n_members":         8,
      "first_seen":        "2026-04-29",
      "last_seen":         "2026-05-04"
    },
    ...
  ]
}
```

## Pipeline commit history (key milestones)

| Commit | Stage | What |
|---|---|---|
| `0507bdc` | Stage A v1 | Sequential matcher + id_registry (21,135 → 26,719 after tiebreak) |
| `033eb00` | Stage A round 1 | Sonnet 16-batch tiebreak (1,336 SAME merges) |
| `dbb07c8` | Stage A round 2 | Loader extension + 9,624-pair Sonnet tiebreak (39,425 final, 6,640 multi-source) |
| `8576f46` | Stage B | 4-source building matching with Pass 2 + Hybrid feedback (146,432 buildings, 10,583 multi-source) |
| `2f6ac56` | Stage F | 4-source canonical assembly with per-source covers preserved |

## Contact / Questions

For schema questions or merge-policy concerns, see the project
`README.md` and `docs/REFERENCE.md`.
