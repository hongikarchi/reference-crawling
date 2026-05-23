# Architect (Firm) Recommendation — make_web Handoff

Source table: Neon `canonical_v2_architects` (loaded by
`tools/canonical_v2_architects_neon_loader.py`).

## Schema (essentials for recommendation)

| Column | Type | Purpose |
|---|---|---|
| `canonical_arch_id` | TEXT PK | Stable firm ID (`arch_NNNNNN`) |
| `canonical_name` | TEXT | Display name |
| `primary_country` / `primary_city` | TEXT | HQ location (divisare > archello > architizer) |
| `website`, `social_links` (JSONB), `phone` | — | Contact / online presence |
| `building_ids` | TEXT[] | Firm's portfolio (FK to `canonical_v2_buildings`) |
| `n_buildings`, `n_buildings_publishable` | INTEGER | Portfolio size |
| `top_programs/styles/color_tones/atmospheres/materials/typologies/arch_elements` | TEXT[] | Top-5 frequency-sorted signature |
| `feature_distribution` | JSONB | Full counters per field for richer matching |
| `portfolio_embedding` | VECTOR(384) | Mean of publishable building embeddings (paraphrase-multilingual-MiniLM-L12-v2) |
| `is_recommendable` | BOOLEAN | `n_buildings_publishable ≥ 3 AND (website OR description OR primary_country)` |
| `hero_building_id` | TEXT | Best preview building (T1/T2 > T3, then most n_sources) |
| `logo_url` | TEXT | Firm logo (archello cover_image_url; nullable) |
| `confidence_tier` | T1/T2/T3 | Cross-source identity verification level |

Indexes: pgvector HNSW on `portfolio_embedding`; B-tree on `primary_country/city/
is_recommendable/confidence_tier`; GIN on `top_programs/top_typologies/countries`.

## Embedding compatibility

Architect `portfolio_embedding` lives in the same 384-dim space as building
`embedding` (model: `paraphrase-multilingual-MiniLM-L12-v2`). Cosine distance
between user-vector (derived from liked buildings) and architect vector is
meaningful.

## Recommendation algorithm

### Cold start (user has no swipes yet)

Show top firms by portfolio depth:

```sql
SELECT canonical_arch_id, canonical_name, primary_country,
       n_buildings_publishable, hero_building_id, logo_url, social_links
FROM canonical_v2_architects
WHERE is_recommendable = TRUE
ORDER BY n_buildings_publishable DESC, confidence_tier
LIMIT 10;
```

### After user swipes K buildings

```python
# liked_embeddings: shape (K, 384) from canonical_v2_buildings.embedding
# Optional disliked_embeddings for negative signal
user_vector = liked_embeddings.mean(axis=0)
if disliked_embeddings is not None and len(disliked_embeddings) > 0:
    alpha = 0.3  # negative weight
    user_vector = user_vector - alpha * disliked_embeddings.mean(axis=0)
user_vector_str = "[" + ",".join(f"{v:.8f}" for v in user_vector) + "]"
```

```sql
SELECT canonical_arch_id, canonical_name, primary_country, primary_city,
       n_buildings_publishable, hero_building_id, logo_url, social_links,
       top_programs, top_styles, top_typologies,
       1 - (portfolio_embedding <=> %(uv)s::vector) AS similarity
FROM canonical_v2_architects
WHERE is_recommendable = TRUE
  AND canonical_arch_id != ALL(%(exclude_ids)s)  -- already shown
ORDER BY portfolio_embedding <=> %(uv)s::vector
LIMIT 10;
```

### Optional refinements

- **Country boost** (if user has location pref): `+ 0.05 IF primary_country = %s`
  in ORDER BY composite score, or pre-filter `WHERE primary_country = %s`.
- **Diversity** (avoid all 10 firms being modernist concrete): after fetching
  top-30 by cosine, re-rank via greedy MMR (Maximal Marginal Relevance) with
  diversity weight on `top_styles` / `top_color_tones`.
- **Pagination / dedup**: pass already-shown `canonical_arch_id` array via
  `!= ALL(...)`.
- **Geographic spread**: `array_length(countries, 1) > 1` for international firms.

## Coverage at C23

- Architects in table: 14,216 (firms with ≥1 building in canonical_v2_buildings)
- `is_recommendable = TRUE`: 4,159 (29%)
- Distinct `primary_country`: 606
- Architects with ≥3 publishable: 4,365

The 25,417 firms in `id_registry_architects.json` without any building in
canonical (likely auxiliary credits — landscape, lighting, structural, etc.) are
not loaded.

## Provenance

- Source-of-truth ID: `data/id_registry_architects.json`
- Per-source profile metadata: `archello_firms`, `architizer_firms`,
  `divisare_architects` (priority: divisare > archello > architizer for
  conflicting fields; social_links + offices merged across sources).
- Builder: `tools/canonical_v2_architects_build.py`
- Loader: `tools/canonical_v2_architects_neon_loader.py`
- Build report: `data/reports/canonical_v2_architects_build_report.json`

## Deferred / known gaps

- Email field: no source DB has email — column is always `null`. Future
  external enrichment cycle.
- Logo coverage: archello-only (`cover_image_url`); ~30% of firms have logo.
- Firm-name dedup (e.g., "Foster + Partners" vs "Foster and Partners"): handled
  by source-of-truth registry merge; residual translation variants may exist.
- Email/instagram-handle freshness: source DBs were last crawled in 2026-05.
- Recommendation feedback loop (user clicks "follow firm" → boost similar) is
  client-side (make_web), not in this DB.
