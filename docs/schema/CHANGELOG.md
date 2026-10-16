# archi_data schema / release changelog

Neon `archi_data` (production endpoint `ep-broad-hat-a1jaomn7`). One entry per make_db
release that touches `archi_data`. Each entry must be accompanied by a refreshed
`docs/schema/archi_data.schema.json` (`tools/neon_schema_snapshot.py`) and a clean
`--compare` run. Rules: `docs/HANDOFF_ARCHIBE_DB.md` §8.

## [planned] vNext projection (P7, date TBD)

Not applied. Spec: `docs/VNEXT_REBUILD_PLAN.md`, `docs/vnext/PROJECTION_SPEC.md`.

- **Added:** `public.building_images` per `docs/vnext/BUILDING_IMAGES_DDL.sql`
  (FK `canonical_bld_id` → `canonical_v2_buildings`, ON DELETE CASCADE).
- **Unchanged shape:** `canonical_v2_buildings` columns; values regenerated from vNext.
- **IDs:** legacy `canonical_bld_id` kept only for clear 1:1 crosswalk; otherwise new IDs.
  Swipes referencing dropped IDs may orphan — no remap planned yet.
- **R4:** scale/structural_system/roof_type/facade_pattern NULL for new / non-1:1 rows;
  era recomputed from project_year.
- **covers_by_type:** always exactly 5 keys, missing slot = JSON null; no default cover →
  `is_publishable = false`.
- Tag tables + architects rebuilt in the same release cycle.
- Pre-release: available only as MOCK in `vnext_preview.building_images` on the
  `dev-ksangjo` branch (`engine = 'mock_preview'`).

## 2026-10-16 — baseline: c27 (`completeness_c27_cover2026q3`)

First recorded baseline. Live state verified read-only 2026-10-16. pgvector 0.8.0.

| table | rows | key subset | columns |
|---|---|---|---|
| `canonical_v2_buildings` | 39,478 | 36,673 `is_publishable` | 45 |
| `canonical_v2_architects` | 14,216 | 4,348 `is_recommendable` | 37 |
| `canonical_v2_tag_stats` | 15,655 | 11 axes | 7 |
| `canonical_v2_tag_centroids` | 15,655 | 11 axes | 6 |
| `canonical_v2_tag_vocabulary` | 15,655 | 11 axes | 6 |

- `corpus_version = c23_final+matstrip+r4+rem2026q2+prog2026q2`.
- Embeddings: `paraphrase-multilingual-MiniLM-L12-v2`, 384-dim, building vectors not
  normalized (centroids L2-normalized); HNSW `vector_cosine_ops` on both embeddings.
- Lineage of this state: c26_rem2026q2 + 145 `display_cover_url` re-picks (applied
  2026-07-06, job card `.claude/ops/jobs/20260705_cover_repick_rereview.md`); R4 axes
  deployed 2026-06-14; audit remediation F1/F3 2026-06-15; typology/program refinements
  2026-06-21/24.
- Known data caveats at baseline: `updated_at` not maintained (max 2026-05-23);
  `cover_image_cdn_url` all NULL (R2 unused); architizer-prod.imgix.net covers return
  HTTP 410 (7,375 publishable rows); 203 rows missing `display_cover_url`.
