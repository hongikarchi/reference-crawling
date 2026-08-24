# vNext WP0c — identity/gallery/semantic/cover/projection contracts

- Date: 2026-08-24 KST
- Result: **PASS**
- Cost: $0; LLM/model calls 0; external network 0; DB connections 0
- Neon/R2 writes: 0; schema execution: 0; deletions: 0

## Inputs

- `docs/VNEXT_REBUILD_PLAN.md`
  - 9,532 bytes
  - SHA-256 `4624e38a00498a44bc2ac71285145a8ef46ee7256f8c75bf6c61b2987b320e9e`
- frozen semantic field/applicability reference:
  - `canonical/cross_source_semantic_vision.py`
    `3baf6d953b56b6da4907680f9ac907ae82ccec1a0d40bbc5673960534e020927`
  - `canonical/cross_source_semantic_vision_v27.py`
    `230c41fb4245d17940766957e2a50134c8115e875812375d749563d786ae849a`
- current parent serving schema reference:
  - `tools/canonical_v2_neon_loader.py`
    `d81e7e05afaf3408bd39aa3f77299d361c86552848292ad82e1a626f7e30223f`
- E2 full_v5 read-only confirmation: source buildings 91,803
  (Divisare 29,891 + Architizer 61,912), metadata pairs 6,754,
  cross-source candidates 9,026.

## Outputs

| File | SHA-256 |
|---|---|
| `docs/vnext/CONTRACT_IDENTITY.md` | `7e9fd86bfd056293aac901b784097991de5ffbe8784673b9b436412b0a524f6e` |
| `docs/vnext/CONTRACT_GALLERY_SEMANTICS.md` | `ee370a2bcb0aca4b595d113cbb56dc42e3bd9cfae8190354f3d6a15196555115` |
| `docs/vnext/CONTRACT_COVERS.md` | `d555df5a8094ac63ae5c572d86ee4c2acf60809c237d8c14ec06a407d5701313` |
| `docs/vnext/BUILDING_IMAGES_DDL.sql` | `633d0037c675f6b31c0ad6127c25f9815c0e01ba0790161bed2b07d4742b5c12` |
| `docs/vnext/PROJECTION_SPEC.md` | `c70afa10243fc9c4263a10f8dc1eb3a0f52cb4adebc29d88fc48b6907b89a441` |

Supporting validator/test:

- `tools/validate_vnext_wp0c_contracts.py`
- `tests/test_vnext_wp0c_contracts.py`

## Contract outcome

- identity: candidate generation and decision separated; independent evidence
  families ≥2 + hard conflict 0 produces only a candidate; first-release merge
  is always human-approved; reject/defer inside a component is fatal.
- gallery: all source occurrences stay local, one canonical representative is
  served, pHash/crop edges never gain transitive closure, exclusions remain rows.
- semantics: compact v2.7 nine fields only; drawing view axes are N/A;
  `perspective` is drawing/rendering subtype; long prose and uncertainty hard gate removed.
- covers: building-level default+5 comparison; rendering is not a photo-slot
  fallback; ambiguous selections become `review_required`.
- analysis bundle: shallow labels assemble a diverse list with no extra Vision;
  deep P6 remains deferred.
- serving: one `building_images` table. `semantic_type`/`cover_slot` are arrays
  because memberships are non-exclusive; partial unique indexes enforce at most
  one selected image per building/slot. Evidence/lineage remains local.

## Verification

- WP0c validator: **PASS**, five expected files, zero errors
- pytest: **2 passed**
- DDL static safety: exactly one table, default+five unique cover indexes,
  no DROP/ALTER/TRUNCATE/INSERT/UPDATE/DELETE statement, no local evidence columns
- SQL execution/schema mutation was not attempted; that remains P7 user-gated.
- `core/vocab.py` and every frozen artifact were untouched.
