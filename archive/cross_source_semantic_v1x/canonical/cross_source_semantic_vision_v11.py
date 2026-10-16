"""Measured v1.1 pixel-only semantic prompt for architecture images.

The v1.0 output schema, normalization, and deterministic derivations remain
unchanged.  This module changes only the contract and prompt versions and the
pixel-grounded decision guidance.  In particular, it does not expose source,
project, URL, filename, rank, or other metadata to the model.
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


CONTRACT_VERSION = "cross-source-image-semantics-v1.1.0"
PROMPT_VERSION = "cross-source-image-semantics-prompt-v1.1.0"


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
    """Return the v1.1 metadata-blind prompt for an ordered image batch."""

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
- In scope means a coherent architectural project, space, integrated component,
  representation, model, construction, ruin, or demolition is dominant and
  independently readable.
- Reject a dominant furniture/product display, material/textile sample,
  specimen, artwork, or isolated object when the room/building is only generic
  context. Being indoors does not make it architectural.
- Reject a person or event when the activity dominates and the designed project
  is not independently readable. Keep it in scope when the architectural space
  remains coherent and dominant despite people.
- Keep a component/material detail when visibly integrated into a facade, room,
  or building. Reject workshop/process/object views with no coherent project.
- Keep a coherent single-project representation sheet. Reject a multi-panel
  board with no dominant or coherent project view.
- If project versus product/art/person dominance is close, or a tiny, distant,
  cropped, or obscured project is weakly readable, choose the best decision and
  include scope in uncertain_axes.

Medium:
- Classify visible representation, not file format or polish.
- Photograph includes polished, retouched, staged, unusually clean, dramatic,
  aerial, and tilt-shift camera images. Perfection alone is not CGI evidence.
- Rendering requires positive visible synthesis/illustration evidence. If
  photoreal CGI and polished photography both remain plausible, choose the
  better-supported value and include medium in uncertain_axes.
- A photographed 3-D scale/massing model is physical_model, even when
  monochrome or low contrast.
- For panels: mostly photos=photograph, mostly drawings=drawing, comparably
  different media=mixed. Layout alone does not change medium.

Applicable axes:
- threshold requires a substantial visible inside/outside transition.
- framing_scale: material_detail=surface; element_detail=integrated component or
  crop; overall=readable whole building/facade/room; site_context=wider
  relationships are essential.
- aerial_top_down is nearly vertical; aerial_oblique unmistakably looks over
  principal roof planes. Raised indoor/balcony/hillside views are only elevated.
- construction_visible needs unfinished work: exposed services/substrate,
  incomplete enclosure, active facade work, or project-tied scaffolding/cranes;
  maintenance alone is insufficient. Weathering/overgrowth/deterioration can
  support ruin_or_abandoned_visible. Mark project_state when ambiguous.
- project_legibility: high=clear intentional whole/view; medium=coherent but
  incomplete/obscured; low=small, distant, fragmentary, dark, or heavily
  obscured and weakly readable; none=no coherent project; unknown=no preferred
  grade. Architecture merely being present does not justify medium.

Applicability invariants:
- Out of scope: reject_reason is not none, project_legibility is none, medium
  still describes representation, and other axes are not_applicable.
- In scope: reject_reason is none and project_legibility is never none.
- Photograph: drawing_kind is not_applicable; view axes/project_state apply.
  Drawing: drawing_kind is specific; other view/state axes are not_applicable.
  Rendering: drawing_kind and view axes apply; project_state is not_applicable.
  Physical_model/mixed/other/unknown: view/drawing/state axes are not_applicable.
  Aerial angles require exterior or unknown spatial_context.

Uncertainty audit:
- Judge each applicable axis independently. Choose the best value, and also list
  the axis whenever a second controlled value remains credibly plausible after
  close inspection. Uncertainty needs no tie and does not replace the best value.
- Check project/product/art/person dominance; photo/photoreal-rendering;
  drawing/model/mixed; interior/threshold/exterior; overall/detail/site;
  eye-level/elevated/aerial; finished/construction/ruin; adjacent legibility.
- Use unknown only when no controlled value can be preferred, and always mark
  that axis uncertain. Do not mark a not_applicable axis uncertain.
- resolution_insufficient is true only when additional pixels could materially
  resolve at least one listed uncertainty.

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
