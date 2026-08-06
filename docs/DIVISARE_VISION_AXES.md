# Divisare image-axis contract

The v2 Vision contract classifies the 1024-pixel image along independent axes
instead of asking the model to choose one overloaded image type.

| Axis | Question |
|---|---|
| `medium` | Is this a photograph, drawing, rendering, or physical model? |
| `spatial_context` | Does it show exterior, interior, or their threshold? |
| `framing_scale` | Does it show site context, an overall view, an element, or a material? |
| `camera_angle` | Is the viewpoint eye-level, elevated, or aerial? |
| `drawing_kind` | For a drawing/rendering, is it a plan, section, perspective, etc.? |
| `project_state` | Is a photographed project finished, under construction, ruined, or being demolished? |

`in_scope`, `reject_reason`, `uncertain_axes`, and
`resolution_insufficient` keep exclusions and uncertainty explicit. The prompt
uses pixels only; filenames, URLs, source tags, and project knowledge are not
evidence.

The model does not output a combined class. Code derives it with fixed
precedence: drawing/representation, aerial, detail, interior, exterior. Other
applicable classes remain in `secondary_classes`, so an exterior material
close-up can be `primary_class=detail` and `secondary_classes=[exterior]`.

`usage_status` is also deterministic:

- `rejected`: out of scope
- `archive_only`: construction, ruin/abandonment, or demolition is visible
- `review_required`: unknown/ambiguous axes or insufficient resolution
- `eligible`: internally consistent result suitable for downstream use

The strict JSON schema, prompt, normalization, applicability checks, and
projection live in `canonical/divisare_vision_axes.py`. This module is separate
from the v1 benchmark so the completed v1 N100 remains reproducible.

## 1024px validation status

Prompt v2.5 first improved the reused development N50 to 47/50 images with all
judgeable fields accepted and 285/289 accepted field answers. That result was
not treated as final because the same development examples had already informed
prompt changes.

A disjoint fresh N100 was then selected outside the earlier N560 at asset,
article, and building level. Ninety-six images downloaded successfully, with no
exact or pHash-distance <=16 collision inside the new set or against the earlier
set. A balanced N50 was frozen, reviewed twice using opaque IDs, and adjudicated
where the two reviews disagreed. These labels are Codex-assisted, not independent
human ground truth.

The one-shot prompt-v2.5 holdout completed on 2026-08-05. All transport, content
hash, schema, SQLite, source-immutability, and temporary-file checks passed. N10
and N20 are rank prefixes calculated from this single N50 execution, not separate
model runs.

| Prefix | Images with every judged field accepted | Accepted field answers |
|---|---:|---:|
| N10 | 8/10 (80.0%) | 62/64 (96.9%) |
| N20 | 17/20 (85.0%) | 128/131 (97.7%) |
| N50 | 41/50 (82.0%) | 314/327 (96.0%) |

At N50, scope acceptance was 50/50 and drawing kind was 5/5. Framing scale was
the weakest fact at 38/43 (88.4%). The derived main search class was accepted on
46/50 images, supporting classes on 45/50, and use/review/exclude status on
40/50. Most importantly, the model marked only 3 of 11 reviewer-ambiguous images
and 3 of 12 ambiguous field occurrences, while adding four unsupported
uncertainty flags.

The errors are semantic rather than resolution failures: no image was marked
resolution-insufficient at 1024px. Recurrent cases are site context versus a
whole building, whole building versus a cropped element, intentionally exposed
construction versus unfinished work, and photographed physical models. Several
state, threshold, scope, and camera-angle examples also need independent human
review because the model-assisted gold itself is reasonably debatable.

Full Divisare image processing remains blocked. The consumed holdout is now a
diagnostic set and must not be reused to claim final accuracy after prompt or
rule changes. Revise on development data, obtain human decisions for ambiguous
definitions, and run one new disjoint holdout before production. See
`data/reports/smoke/divisare_vision_axes_holdout_n50_v2_5.md` and
`data/reports/smoke/divisare_vision_axes_holdout_n50_v2_5_audit.md`.
