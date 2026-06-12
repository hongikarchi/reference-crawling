# 2026-05-27 — MATERIAL_TAXONOMY_NOISE strip + audit follow-up

## Scope

Follow-up to the 2026-05-27 make_web-perspective audit
(`docs/MAKE_DB_AUDIT_FINDINGS_2026-05-27.md` in `feature/codex-make-db-audit`).
Per-issue verdict in `data/reports/audit_2026-05-27/verdict.md`.

Of the 17 issue codes flagged on 36,864 publishable rows, only one category
warrants automated data fix:

- **`MATERIAL_TAXONOMY_NOISE`** (8,761 publishable rows): non-material terms
  (`water`, `vegetation`, `terraces`, `skylights`, `planting`, …) polluting
  `material_visual` arrays. 29 noise terms hardcoded in audit tool.

Other categories: false positives (audit rule too aggressive), already covered
by make_web filtering, or sibling-project legitimacy (71% of GALLERY_PHASH
shared images are same-architect portfolio reuse).

29 `COVER_PHASH_SHARED` HIGH rows handled by separate manual review tool
(`tools/build_cover_phash_review.py` → side-by-side HTML).

## Inputs

- Source noise list: `tools/audit_make_db_for_make_web.py::MATERIAL_TAXONOMY_NOISE` (29 terms)
- Mirrored into make_db: `tools/canonical_v2_upload_validator.py::MATERIAL_TAXONOMY_NOISE`

## Code changes (pre-Neon-write)

- `tools/canonical_v2_upload_validator.py`:
  - Added `MATERIAL_TAXONOMY_NOISE` frozenset (29 terms)
  - Added `filter_material_noise()` helper
  - `map_row()` now strips noise from `material_visual`; if result is empty
    AND original was non-empty, flips `is_publishable=False` and appends
    `material_noise_only` to `publishability_reasons`
  - Validator's `bad_material_visual` check relaxed to allow empty when row
    is flagged as `material_noise_only`
- `tools/canonical_v2_architects_build.py`:
  - Imports `MATERIAL_TAXONOMY_NOISE` and skips noise terms when building
    per-architect `top_materials` counter

## Migration tool

- `tools/strip_material_noise_neon.py`
  - `--dry-run`: transaction + ROLLBACK
  - `--apply --confirm-db-write`: COMMIT

UPDATE A: strip noise from `material_visual` where result remains non-empty
UPDATE B: strip noise + `is_publishable=false` + reason where result would be empty
UPDATE C: strip noise from `canonical_v2_architects.top_materials`

## Dry-run output (2026-05-27)

```
rows with at least one noise term:        9606
  of which would become EMPTY (unpublish): 213
is_publishable=true before: 36864
UPDATE A (strip noise, non-empty result):  9393 rows
UPDATE B (strip noise + unpublish empty):  213 rows
is_publishable=true after:  36656 (delta -208)
rows still containing noise (should be 0): 0
architects with noise in top_materials before: 3782
UPDATE C (architects.top_materials):        3782 rows
architects still containing noise (should be 0): 0
ROLLBACK (dry-run)
```

5 of the 213 noise-only buildings were already `is_publishable=false`, so net
publishable delta is −208 (36,864 → 36,656).

## Open

- User approval required to run `--apply --confirm-db-write`.
- Cover phash 29-pair manual review: `data/reports/audit_2026-05-27/cover_phash_review.html`
  + decisions template CSV. User to fill in decisions, then a follow-up SQL
  script (TBD) applies the per-row actions.

## Result

(pending user approval to commit)

## UPDATE 2026-06-05 — superseded by move-not-delete (reclassify)

User feedback: architectural elements must stay searchable, not be deleted.
`strip_material_noise_neon.py` rewritten from plain strip → **reclassify**:
- 12 element-type noise terms (terrace, balcony, courtyard, skylight, column,
  facade, garden, green roof, stairs) MOVE into `architectural_elements`
  (controlled vocab, deduped); the other 17 (water, vegetation, lighting,
  walls, windows, …) are dropped from `material_visual`.
- Unpublish only when material empties AND no element salvaged.
- Shared helper `reclassify_material()` in `canonical_v2_upload_validator.py`
  drives BOTH the loader (`map_row`) and this migration → idempotent on reload.
- Bundles R2: unpublish 2 placeholder-architect rows (`architect_unknown`).

Dry-run (2026-06-05): 9,606 rows changed · 1,962 gain elements
(Terrace 541, Courtyard 342, Roof 271, Garden 252, Balcony 224, Skylight 158,
Facade 158, Column 135, Stair 87) · 189 unpublished · 19 saved-by-element ·
R2 2 rows · is_publishable 36,864 → 36,673 (−191) · noise residual 0 ·
architects.top_materials 3,782 cleaned.

**HELD: user paused the Neon write (`--apply`) on 2026-06-05.** Script + loader
ready; run `tools/strip_material_noise_neon.py --apply --confirm-db-write` when
approved. Follow-up: rebuild architects (`canonical_v2_architects_build.py`) to
refresh `top_arch_elements` with the moved elements.
