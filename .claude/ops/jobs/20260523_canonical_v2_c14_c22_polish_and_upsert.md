# 2026-05-23 C14-C22 make_web polish cycle + Neon upsert

## Scope

9-cycle iterative polish on `canonical_buildings_strict` to reach make_web
pre-upsert readiness, followed by user-gated Neon upsert. All cycles driven
by Codex external read-only audits.

## Lineage

| Cycle | Output | Key changes |
|---|---|---|
| C14 | `completeness_c14_finalize` | 27 near-split merges (Hamming ≤8 + name≥90 + country); 35 spam unpublish; 5 image-recoverable rows restored from source DBs |
| C15 | `completeness_c15_make_web_polish` | 9,697 rows HTTPS-normalized (351k URL rewrites); 21,095 rows internal-phash dedup (27,015 excess); 23-pair cross-card cover swap + split sidecar; SEO/generic unpublish 549; missing-country unpublish 1,885; suspicious-city null-out 156; `year_kind` field added |
| C16 | `completeness_c16_make_web_polish` | Canonical asset dedup with `_canonical_asset_key` (host + content-hash); 4,815 rows / 4,868 image excess; 96 exact-name+arch auto-merges (103 losers); 167 lowres-cover swaps; 2,070 source_url backfills |
| C17 | `completeness_c17_make_web_polish` | URL canon strengthened (Architizer timestamp, Divisare UUID/Cloudinary/extension variants); cover dup residual fix; country alias normalize; registry merge lineage (285 ids → `redirected_to`) |
| C18 | `completeness_c18_make_web_polish` | Non-raster URL strip (.tiff/.pdf/.ai/.psd) on covers (44) + all_images (9,894 URLs); phash priority dedup fix (regression from C17); name+arch residual merge (7 → 103 cumulative); Pixel House country fix |
| C19 | `completeness_c19_make_web_polish` | Raster ext whitelist tightened (.jpe/.mp4/.db/.url/.docx exclusion); BIPC non-raster strip (9,946 URLs / 2,026 rows); full-scan country/year sidecar regen with Metalocus articles join |
| C20 | `completeness_c20_make_web_polish` | Architect source-branding strip 3,130 names + 9,553 text entries (" - Architizer", " \| Archello", " / Divisare"); cover phash download + imagehash backfill (106/110); source_url gap backfill 118 URLs; country alias extension; Architizer URL base length ≥3 fix |
| C21 | `completeness_c21_make_web_polish` | Metalocus source_url backfill 49 via `4_buildings_final.json` B-id map; phash-based cover dup swap (asset-key bug fix); native-name country alias (España/Italia/中国/日本/Türkiye/etc. ~50 entries); split sidecar full re-derive 8 → 40 |
| C22 | `completeness_c22_make_web_polish` | Final 5 blockers: 1 metalocus URL slug fallback (Sornells 21); 5 missing-phash unpublish; 2 cover dup force-swap (phash=None bypass fix); country dirty filter expanded (comma-2+ tokens); split sidecar dedup 40 → 7 unique pairs |

## Quality gates (every cycle)

- `qc_strict.py`: 10/10 PASS
- `canonical_v2_upload_validator.py`: 0 failures
- Codex external audit: critical 0, high progressively reduced

## Schema migration

`year_kind TEXT NOT NULL DEFAULT 'unknown' CHECK IN ('completed','future','unknown')`
added to `canonical_v2_buildings` (Neon). Index `idx_canonical_v2_buildings_year_kind`.

## Neon upsert (final, user-gated)

- Mode: `--upsert --confirm-db-write`
- DELETE 292 cumulative removed_canonical_ids
- UPSERT 39,484 C22 rows
- Result: `writes: committed`, 0 mapping failures

| | Before | After |
|---|---|---|
| Total rows | 39,776 | **39,484** |
| Publishable | 39,736 | **36,870** |
| Tiers | T1 243 / T2 7,675 / T3 31,858 | T1 329 / T2 9,540 / T3 29,615 |

## Outputs

- C8/C18/C19 retained per B retention policy (other generations deleted, ~15 GB reclaimed)
- Active artifacts (CCR):
  - `canonical_buildings_strict.completeness_c22_make_web_polish.json`
  - `canonical_buildings_strict_embedded.completeness_c22_make_web_polish.json`
- Reports: `data/reports/canonical_v2_c14_*` ~ `c22_*`
- Sidecars (manual review pending):
  - `canonical_v2_c22_country_conflict_sidecar.jsonl` (237)
  - `canonical_v2_c22_year_conflict_sidecar.jsonl` (48)
  - `canonical_v2_c22_split_suspect_sidecar.jsonl` (7)
- Registry: `data/id_registry_buildings.json` 292 entries with `redirected_to`
  + `removed_at` (backup `_backup_pre_c17.json`, `_backup_pre_c18.json`)
- Dashboard regenerated at C22

## New tools (committed)

- `tools/canonical_v2_c14_finalize.py`
- `tools/canonical_v2_c15_make_web_polish.py`
- `tools/canonical_v2_c16_make_web_polish.py`
- `tools/canonical_v2_c16_url_canon.py` (asset key + raster whitelist + lowres/GIF)
- `tools/canonical_v2_c17_make_web_polish.py` (extended country alias, registry update helpers)
- `tools/canonical_v2_c18_make_web_polish.py` (non-raster strip + phash priority dedup)
- `tools/canonical_v2_c19_make_web_polish.py` (Metalocus articles join helpers)
- `tools/canonical_v2_c20_make_web_polish.py` (imagehash download + branding strip)
- `tools/canonical_v2_c21_make_web_polish.py` (native country alias dict)
- `tools/canonical_v2_c22_make_web_polish.py` (force-swap bug fix + dirty filter)

## Deferred

- 237 country / 48 year / 7 split sidecars (manual review)
- 4 cover phash download permanent failures (404)
- Local repo currently 17 commits ahead of `origin/main` (push pending)

## Guardrails

- No vocab change (`core/vocab.py` untouched)
- No `data/id_registry*.json` deletion (entries updated with redirected_to)
- Every Neon write user-gated; dry-run + diff presented before live upsert
- All deletions confirmed by user (B retention policy)
