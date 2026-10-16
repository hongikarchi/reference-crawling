# Divisare image identity v2.4

## Purpose

`divisare_metadata_v2_4.db` is an immutable overlay on the reviewed v2.3
metadata artifact. It corrects the identity of modern Divisare Cloudinary
images before any full image hash or classification run.

The v2.3 rule used only the Cloudinary public ID:

```text
divisare|{public_id}
```

The v2.4 rule keeps the delivery version that identifies the delivered source
image:

```text
divisare|{public_id}|{vNNN}
```

Transform options such as width, crop, quality, and output format remain
excluded from identity. Legacy `project_images` keys are unchanged.

## Reason

The GYAAN CENTER article contains 32 URLs that share one public ID but use 31
delivery versions. The first version is exposed once as the cover and once as
the first gallery image; those two URLs decode to the same normalized pixels.
The remaining 30 versions are distinct images.

The old rule collapsed this family into one asset. The corrected result is:

```text
32 URLs -> 31 image assets
```

## Artifact

- Parent: `data/curated/divisare_metadata_v2_3.db`
- Parent SHA-256:
  `7c263f430709dbfe4a2747407d736268331976cd77f66fe25d7037b77fd68038`
- Output: `data/curated/divisare_metadata_v2_4.db`
- Output SHA-256:
  `9c523f3393d20ae8732677981c207abd02247ca5ce905dec422a05fa0398f70f`
- Logical SHA-256:
  `d664374325b3cea5dfe9d6b7f5f39eb65762198e8003df779a05df74745e49b9`
- Schema: `PRAGMA user_version = 7`
- Builder: `divisare-image-identity-builder-v2.4.0`
- Asset-key policy: `divisare-asset-key-v1.1`

Population:

| Surface | v2.3 | v2.4 |
|---|---:|---:|
| Image assets | 547,222 | 547,252 |
| Modern Cloudinary assets | 429,291 | 429,321 |
| Legacy project-image assets | 117,931 | 117,931 |
| Image URLs | 577,112 | 577,112 |
| Source/article occurrences | 577,112 | 577,112 |
| Pending pHash tasks | 547,222 | 547,252 |

The overlay records lineage and changed keys in
`image_identity_lineage_v2_4` and `image_asset_key_map_v2_4`. Consumers can
use `v_building_images_v2_4` and `v_divisare_buildings_export_v2_4`.

## Safety contract

The builder refuses to migrate a database after image processing has started.
All hashes must still be pending and image bands, classifications, matches,
and image-scoped claims must be empty. It preserves v2.3 and publishes a new
artifact through a no-clobber build lock.

The production build passed 88 validations with no failures. SQLite integrity
was `ok`, foreign-key violations were zero, and every building, text, area,
D2, taxonomy, and other non-image table matched its v2.3 typed logical hash.

## Image smoke result

Fresh N10 and N100 downloads used runner `divisare-image-smoke-v1.3.0`.

- N10: 9 success, 1 intentional hard skip, 0 conflicts
- N100: 95 success, 5 intentional hard skips, 0 conflicts
- N10 is the exact prefix of N100
- All nine common successful assets have identical pixel SHA and pHash values
- GYAAN cover/gallery version: 2 requests, 1 normalized pixel SHA
- GYAAN next version: a distinct pixel SHA and pHash distance 132
- N100 resume: 0 network requests

This validates the corrected identity and smoke pipeline. It is not the full
547,252-asset image run.
