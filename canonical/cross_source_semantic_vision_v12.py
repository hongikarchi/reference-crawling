"""Measured v1.2 pixel-only semantic prompt for architecture images.

The v1.0 closed schema, normalization, deterministic derivations, and 1024px
transform remain unchanged.  V1.2 is a lean prompt-only correction for the
frozen v1.1 N10 gaps in overlays, boundary uncertainty, framing, legibility,
camera elevation, and resolution evidence.  Source and project metadata remain
unavailable to the model.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from canonical.cross_source_semantic_vision import (
    CAMERA_ANGLES,
    DRAWING_KINDS,
    FRAMING_SCALES,
    MEDIA,
    MODEL_FIELDS,
    OUTPUT_SCHEMA,
    PROJECT_LEGIBILITY,
    PROJECT_STATES,
    REJECT_REASONS,
    RESULT_SCHEMA,
    SPATIAL_CONTEXTS,
    TRANSFORM_VERSION,
    UNCERTAIN_AXES,
    derive_coverage_slots,
    derive_hero_decision,
    normalize_batch,
    normalize_result,
    validate_invariants,
)


CONTRACT_VERSION = "cross-source-image-semantics-v1.2.0"
PROMPT_VERSION = "cross-source-image-semantics-prompt-v1.2.0"


def _asset_ids(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError("asset_ids must be a sequence")
    result = tuple(values)
    if not result or any(not isinstance(value, str) or not value for value in result):
        raise ValueError("asset_ids must contain non-empty strings")
    if len(result) != len(set(result)):
        raise ValueError("asset_ids must be unique")
    return result


def compose_prompt(asset_ids: Sequence[str]) -> str:
    """Return the v1.2 metadata-blind prompt for an ordered image batch."""

    ordered_ids = _asset_ids(asset_ids)
    ordered = "\n".join(
        f"{index}. {asset_id}" for index, asset_id in enumerate(ordered_ids, 1)
    )
    return f"""Classify each attached architecture image using only visible pixels.

The attachments are in this exact order:
{ordered}

Return exactly one object per attachment, in the same order, inside
{{"results": [...]}}. Copy each opaque asset_id exactly. Return every field and
use only the controlled values in the supplied closed JSON schema. Evidence is
one short sentence grounded only in visible pixels.

Decide in this order: scope, medium, applicable axes, then uncertainty.

Scope:
- In scope means a coherent project, space, integrated component,
  representation/model, construction/ruin/demolition is dominant and readable.
- Reject dominant furniture/products, samples, specimens, art, or isolated
  objects when architecture is only generic context. Indoors is not enough.
- Reject a person/event when activity dominates and the project is not readable;
  keep it when a coherent architectural space stays dominant despite people.
- Keep component/material details visibly integrated into architecture; reject
  workshop/process/object views with no coherent project.
- Keep a coherent single-project representation sheet. Reject a multi-panel
  board with no dominant or coherent project view.
- When project versus product/art/person/model dominance is close, or the project
  is tiny, cropped, or obscured, choose the best value and mark scope uncertain.

Medium:
- Classify visible representation, not file format or polish. Photograph includes
  staged, retouched, dramatic, aerial, and tilt-shift camera images. Branding or
  perfection is not proof of rendering.
- Rendering requires positive visible synthesis/illustration evidence. If
  photoreal CGI and photography both remain plausible, choose the better value
  and include medium in uncertain_axes.
- Overlays, captions, watermarks, borders, or magazine layout on one photograph
  do not make mixed. Use mixed only when substantive scene content in two or more
  representation media is comparably important.
- A photographed 3-D scale/massing model is physical_model, even when monochrome
  or low contrast.

Applicable axes:
- threshold requires a substantial visible inside/outside transition.
- framing_scale: material_detail centers a surface/material/junction;
  element_detail an integrated component/cropped assembly; overall a coherent
  whole room/building/facade; site_context essential wider relationships.
  Surrounding walls alone do not make a crop overall.
- eye_level includes a normal standing view tilted up/down. elevated requires
  evidence that the camera is above normal standing height.
  aerial_top_down is nearly vertical; aerial_oblique unmistakably looks over
  principal roof planes. Raised indoor/balcony/hillside views are only elevated.
- construction_visible needs unfinished work: exposed services/substrate,
  incomplete enclosure, active facade work, or project-tied scaffolding/cranes;
  maintenance is insufficient. Weathering/overgrowth/deterioration can support
  ruin_or_abandoned_visible. Mark project_state when ambiguous.
- project_legibility: high requires a clear coherent whole, not just sharp pixels;
  a readable partial component/crop is normally medium; low is small, distant,
  fragmentary, dark, or obscured; none=no project; unknown=no preferred grade.

Applicability invariants:
- Out of scope: reject_reason is not none, project_legibility is none, medium
  still describes representation, and other axes are not_applicable.
- In scope: reject_reason is none and project_legibility is never none.
- Photograph: drawing_kind is not_applicable; view axes/project_state apply.
  Drawing: drawing_kind is specific; other view/state axes are not_applicable.
  Rendering: drawing_kind and view axes apply; project_state is not_applicable.
  Physical_model/mixed/other/unknown: view/drawing/state axes are not_applicable.
  Aerial angles require exterior or unknown spatial_context.

Mandatory uncertainty second pass:
- Before returning, recheck scope and each applicable axis: could a second
  controlled value remain credibly plausible from pixels? If yes, list that axis
  even when the primary is likelier, and name the visible ambiguity in evidence.
  Empty uncertain_axes asserts no credible alternative; do not add speculation.
- Recheck project/product/art/model; photograph/photoreal-rendering;
  drawing/model/mixed; material/element; element/overall/site;
  interior/threshold/exterior; eye_level/elevated; detail/composite;
  finished/construction/ruin; and adjacent legibility grades.
- Use unknown only when no controlled value can be preferred, and always mark
  that axis uncertain. Do not mark a not_applicable axis uncertain.
- resolution_insufficient is true only when more pixels could change a controlled
  decision, not merely reveal detail. If a tiny, blurry, or cropped subject leaves
  scope, medium, drawing_kind, or project_legibility unresolved and more pixels
  could decide it, set true and mark those axes.

Do not use or infer filenames, URLs, source/project identity, rank, editorial
context, or outside knowledge. Output JSON only."""


def prompt_sha256(asset_ids: Sequence[str]) -> str:
    """Return the exact UTF-8 SHA-256 of a composed batch prompt."""

    return hashlib.sha256(compose_prompt(asset_ids).encode("utf-8")).hexdigest()


PROMPT_TEMPLATE_SENTINEL_IDS = ("__opaque_asset_1__", "__opaque_asset_2__")
PROMPT_TEMPLATE_SHA256 = prompt_sha256(PROMPT_TEMPLATE_SENTINEL_IDS)


__all__ = [
    "CAMERA_ANGLES",
    "CONTRACT_VERSION",
    "DRAWING_KINDS",
    "FRAMING_SCALES",
    "MEDIA",
    "MODEL_FIELDS",
    "OUTPUT_SCHEMA",
    "PROJECT_LEGIBILITY",
    "PROJECT_STATES",
    "PROMPT_TEMPLATE_SENTINEL_IDS",
    "PROMPT_TEMPLATE_SHA256",
    "PROMPT_VERSION",
    "REJECT_REASONS",
    "RESULT_SCHEMA",
    "SPATIAL_CONTEXTS",
    "TRANSFORM_VERSION",
    "UNCERTAIN_AXES",
    "compose_prompt",
    "derive_coverage_slots",
    "derive_hero_decision",
    "normalize_batch",
    "normalize_result",
    "prompt_sha256",
    "validate_invariants",
]
