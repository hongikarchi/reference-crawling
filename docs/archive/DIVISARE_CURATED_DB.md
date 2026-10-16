# Divisare Curated SQLite

`tools/build_divisare_curated.py` converts the immutable Divisare crawler
SQLite into a Divisare-only, normalized database. It is the source-specific
stage before comparing or merging Divisare with other sites.

The builder is deterministic and makes no network, LLM, embedding, Neon, or R2
calls.

## Build

```powershell
python tools/build_divisare_curated.py `
  --source-db C:\path\to\divisare.db `
  --output-db data\curated\divisare_curated_v1_5.db `
  --report data\reports\divisare_curated_v1_5.md
```

Use the smoke ladder before a full rebuild:

```powershell
python tools/build_divisare_curated.py --source-db C:\path\to\divisare.db `
  --output-db data\curated\smoke\divisare_curated_n10_v1_5.db `
  --report data\reports\smoke\divisare_curated_n10_v1_5.md `
  --limit 10 --skip-source-hash

python tools/build_divisare_curated.py --source-db C:\path\to\divisare.db `
  --output-db data\curated\smoke\divisare_curated_n100_v1_5.db `
  --report data\reports\smoke\divisare_curated_n100_v1_5.md `
  --limit 100 --skip-source-hash
```

Published curated databases are immutable. A rebuild must use a new versioned
output path; the builder refuses an existing path even when `--replace` is
passed. New files are published with an atomic no-clobber hard link so a
concurrent file cannot be overwritten. This avoids discarding later
pHash/fetch/classification, model text/claims, location enrichment, or manual
review/cluster state. The raw source database is always opened read-only.

## Data model

```text
source_articles
  -> article_architects / article_tags / article_text_versions
  -> source_image_occurrences
     -> article_image_occurrences -> image_urls -> image_assets
  -> attribute_claims

source_articles
  -> article_match_candidates
  -> building_articles -> buildings
  -> building_facets -> building_facet_claims
```

Divisare project IDs are stored as source articles. They are not assumed to be
real-building IDs because the same building can have separate drawing or
photographer features.

Only strict same-name, same-country, same-city, shared architect-ID, and the
same non-null project-year pairs are auto-clustered. Missing-year and generic
names such as `House A` never auto-merge. Fuzzy pairs remain in
`v_dedup_review_queue`.

`building_id` is provisional within this source snapshot. A later D2 pass must
re-cluster after pHash/manual evidence and maintain redirects or an identity
registry before these IDs are treated as durable.

## Tag policy

The source album membership is preserved. `tag_crosswalk` projects reviewed
tags into typed claims:

- `types` and valid `houses`: program, typology, or non-program `work_type`
  evidence
- `materiality`: material, color, or hidden editorial topic
- `elements`: structural material/system candidate, facade material/system,
  roof type/form/material, discriminative feature, finish, or editorial topic
- `plans-details`: article content hint plus supporting program/typology
- `private/public-interiors`: article-scoped room/interior material evidence;
  public program evidence remains building-scoped but supporting only
- `topics`: intervention, publication-time status, context, style, or media hint
- `cities` and country-specific house tags: location evidence, not blind overwrite
- `ideas`: hidden source topic only

Tags are article-level source assertions. Their absence is never interpreted as
negative evidence. Two independent tags are not composed into a relationship:
`columns + wooden-structures` remains two claims, not `wooden column`.

A direct claim is confirmed at confidence 0.85 or above. Supporting-only
building evidence needs at least two distinct source references. Conflicting
direct scalar claims abstain instead of selecting by priority. The building
export includes confirmed facets only; `v_search_facets` also exposes
candidates for review.

Raw credit payloads are intentionally not copied into the curated database.
The original crawler DB remains the source of record. Architect IDs are joined
to the architect index; IDs missing from that index are retained as explicit
project-reference records. Unmatched display names are provenance only and
never participate in duplicate matching.

## Text

The historical crawler flattened Divisare's entire description DOM. The known
collection UI string is removed, while the raw version is retained in
`article_text_versions`.

Because the flattening destroyed image-caption boundaries, affected clean text
uses the quality state `ui_removed_caption_residue_possible`. A clean recrawl
from HTML is required to fully resolve that limitation.

## Images

CDN transformation and SEO variants are normalized into `image_assets`.
Cover and gallery occurrences remain separate and retain their original URLs.
Every raw occurrence is preserved in `source_image_occurrences`, including a
parse error if a future URL shape cannot yet be normalized.

`image_hashes` is pre-seeded with asset-keyed pHash work in `pending` state.
No existing positional pHash cache is imported. `v_phash_work_queue` is the
input for a later full-image hash stage.

Plans and media tags populate article-level content hints only.
`image_classifications` remains empty until an image-level model processes each
asset. No tag is propagated to every image in a gallery.

## Primary views

- `v_divisare_buildings_export`: source-specific export for later cross-site work
- `v_building_articles`: canonical building to Divisare article provenance
- `v_building_images`: building-level asset-deduplicated images
- `v_search_facets`: visible candidate and confirmed facets
- `v_article_content_hints`: drawing/model/night/reportage gallery priors
- `v_dedup_review_queue`: unresolved within-Divisare article matches
- `v_phash_work_queue`: asset-keyed hash work
- `v_image_classification_queue`: prioritized E2 work
- `v_unmapped_tags`: used tags without an enabled mapping
- `v_tags_without_normalized_semantics`: tags with no normalized semantic axis
- `v_tags_without_building_projection`: tags intentionally or currently not
  promoted to a building-level normalized axis
- `v_qa_open`: unresolved quality issues
- `v_building_completeness`: core-field coverage

## Deferred stages

1. Clean HTML text recrawl or model-assisted text extraction.
2. Full asset download metadata, SHA-256, and 256-bit pHash.
3. Per-image exterior/interior/drawing/aerial/detail classification.
4. Review and confirm open article duplicate candidates.
5. Re-cluster articles with exact-image/pHash/manual evidence while preserving
   identity redirects.
6. Re-resolve building facets with text/image/manual evidence.
7. Compare and merge with other source-specific databases.

Vector embeddings are intentionally outside this database and this stage.
