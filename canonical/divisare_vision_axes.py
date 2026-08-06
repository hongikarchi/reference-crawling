"""Orthogonal, pixel-only semantic contract for Divisare images.

The model reports independent visual axes.  A deterministic projection then
derives the legacy five-class search label; the model never chooses that
combined label directly.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


AXIS_CONTRACT_VERSION = "divisare-image-axes-v2.0.0"
AXIS_PROMPT_VERSION = "divisare-image-axes-prompt-v2.5.0"

REJECT_REASON_VALUES = (
    "none",
    "unreadable_or_blank",
    "non_architectural_subject",
    "people_or_event",
    "isolated_product_artwork_or_sample",
    "text_or_logo_only",
    "no_project_visible",
    "multi_panel_without_dominant_image",
    "other",
)
MEDIUM_VALUES = (
    "photograph",
    "drawing",
    "rendering",
    "physical_model",
    "mixed",
    "other",
    "unknown",
)
SPATIAL_CONTEXT_VALUES = (
    "exterior",
    "interior",
    "threshold",
    "not_applicable",
    "unknown",
)
FRAMING_SCALE_VALUES = (
    "site_context",
    "overall",
    "element_detail",
    "material_detail",
    "not_applicable",
    "unknown",
)
CAMERA_ANGLE_VALUES = (
    "eye_level",
    "elevated",
    "aerial_oblique",
    "aerial_top_down",
    "not_applicable",
    "unknown",
)
DRAWING_KIND_VALUES = (
    "plan",
    "site_plan",
    "section",
    "elevation",
    "axonometric",
    "perspective",
    "detail",
    "diagram",
    "sketch",
    "composite",
    "other",
    "not_applicable",
    "unknown",
)
PROJECT_STATE_VALUES = (
    "visibly_finished",
    "construction_visible",
    "ruin_or_abandoned_visible",
    "demolition_visible",
    "not_applicable",
    "unknown",
)
UNCERTAIN_AXIS_VALUES = (
    "scope",
    "medium",
    "spatial_context",
    "framing_scale",
    "camera_angle",
    "drawing_kind",
    "project_state",
)
LEGACY_CLASS_ORDER = ("drawing", "aerial", "detail", "interior", "exterior")
PRIMARY_CLASS_VALUES = (*LEGACY_CLASS_ORDER, "unknown")
USAGE_STATUS_VALUES = ("rejected", "archive_only", "review_required", "eligible")

SEMANTIC_AXIS_FIELDS = (
    "spatial_context",
    "framing_scale",
    "camera_angle",
    "drawing_kind",
    "project_state",
)

_VOCABULARIES: dict[str, tuple[str, ...]] = {
    "reject_reason": REJECT_REASON_VALUES,
    "medium": MEDIUM_VALUES,
    "spatial_context": SPATIAL_CONTEXT_VALUES,
    "framing_scale": FRAMING_SCALE_VALUES,
    "camera_angle": CAMERA_ANGLE_VALUES,
    "drawing_kind": DRAWING_KIND_VALUES,
    "project_state": PROJECT_STATE_VALUES,
}
_VOCABULARY_SETS = {name: frozenset(values) for name, values in _VOCABULARIES.items()}
_UNCERTAIN_AXIS_SET = frozenset(UNCERTAIN_AXIS_VALUES)
_AERIAL_ANGLES = frozenset({"aerial_oblique", "aerial_top_down"})
_DETAIL_FRAMING = frozenset({"element_detail", "material_detail"})
_ARCHIVE_STATES = frozenset(
    {"construction_visible", "ruin_or_abandoned_visible", "demolition_visible"}
)

_MODEL_RESULT_FIELDS = (
    "asset_id",
    "in_scope",
    "reject_reason",
    "medium",
    "spatial_context",
    "framing_scale",
    "camera_angle",
    "drawing_kind",
    "project_state",
    "uncertain_axes",
    "resolution_insufficient",
    "evidence",
)


def _enum_schema(values: Sequence[str]) -> dict[str, Any]:
    return {"type": "string", "enum": list(values)}


_AXIS_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": list(_MODEL_RESULT_FIELDS),
    "properties": {
        "asset_id": {"type": "string", "minLength": 1},
        "in_scope": {"type": "boolean"},
        "reject_reason": _enum_schema(REJECT_REASON_VALUES),
        "medium": _enum_schema(MEDIUM_VALUES),
        "spatial_context": _enum_schema(SPATIAL_CONTEXT_VALUES),
        "framing_scale": _enum_schema(FRAMING_SCALE_VALUES),
        "camera_angle": _enum_schema(CAMERA_ANGLE_VALUES),
        "drawing_kind": _enum_schema(DRAWING_KIND_VALUES),
        "project_state": _enum_schema(PROJECT_STATE_VALUES),
        "uncertain_axes": {
            "type": "array",
            "items": _enum_schema(UNCERTAIN_AXIS_VALUES),
        },
        "resolution_insufficient": {"type": "boolean"},
        "evidence": {"type": "string", "minLength": 1, "maxLength": 500},
    },
}

AXIS_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["results"],
    "properties": {
        "results": {
            "type": "array",
            "items": _AXIS_RESULT_SCHEMA,
        }
    },
}


def _validated_asset_ids(asset_ids: Sequence[str]) -> tuple[str, ...]:
    if isinstance(asset_ids, (str, bytes)):
        raise ValueError("asset_ids must be a sequence of identifiers")
    values = tuple(asset_ids)
    if not values:
        raise ValueError("asset_ids must not be empty")
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError("asset_ids must contain non-empty strings")
    duplicates = sorted({value for value in values if values.count(value) > 1})
    if duplicates:
        raise ValueError(f"asset_ids contains duplicates: {duplicates}")
    return values


def compose_axes_prompt(asset_ids: Sequence[str]) -> str:
    """Compose the frozen pixel-only prompt for a 1024-pixel Vision batch."""

    asset_ids = _validated_asset_ids(asset_ids)
    ordered = "\n".join(
        f"{index}. {asset_id}" for index, asset_id in enumerate(asset_ids, 1)
    )
    return f"""Classify each attached architecture image using only visible pixels.

The attachments are in this exact order:
{ordered}

Return one object per attachment in the same order inside {{"results": [...]}}.
Return exactly these fields and only controlled values:
- asset_id: the exact identifier above
- in_scope: true when the dominant image visibly shows an architectural project,
  space, component, architectural representation, physical model, construction,
  ruin, or demolition
- reject_reason: one of {list(REJECT_REASON_VALUES)}
- medium: one of {list(MEDIUM_VALUES)}
- spatial_context: one of {list(SPATIAL_CONTEXT_VALUES)}
- framing_scale: one of {list(FRAMING_SCALE_VALUES)}
- camera_angle: one of {list(CAMERA_ANGLE_VALUES)}
- drawing_kind: one of {list(DRAWING_KIND_VALUES)}
- project_state: one of {list(PROJECT_STATE_VALUES)}
- uncertain_axes: zero or more of {list(UNCERTAIN_AXIS_VALUES)}
- resolution_insufficient: true only when 1024-pixel legibility prevents a
  reliable decision that a higher-resolution view could materially change
- evidence: one short sentence describing only the visible basis

Make decisions in this order: scope, medium, each applicable semantic axis,
then uncertainty. Judge every axis independently from the dominant visible
content; do not let one label stand in for another axis.

Scope and rejection rules:
- Keep construction, ruins, demolition, architectural drawings, renderings,
  and clearly intentional architectural project models in scope.
- A physical model is in scope only when a coherent building, space, or site
  proposal is visibly presented. Reject an isolated relief, material mock-up,
  sample, product, artwork, or sculptural object even when it looks
  architectural.
- A coherent architectural drawing sheet or drawing montage whose panels
  collectively describe one project remains in scope as medium drawing with
  drawing_kind composite, even when no panel dominates. For other multi-panel
  or collage images, keep the image in scope only when one architectural panel
  clearly dominates and can be classified on its own. Otherwise reject it as
  multi_panel_without_dominant_image.
- Reject a people/event view only when people or the event dominate and the
  project is incidental. People inside an architecture-dominant view are in
  scope. Text or a logo over a visible dominant project does not cause
  rejection.

Medium means the representation, not the file format. A camera photograph of
a straight-on facade is photograph, never drawing or elevation. A photograph
of a clearly intentional architectural scale model is physical_model. Use
drawing for visible linework and rendering when the dominant architectural
view is visibly computer-generated, including a photorealistic rendering. Use
mixed only when two or more representation media each contribute substantially
to reading the main architectural subject, such as a substantial generated
proposal embedded in a photographic context. Incidental text, line annotation,
or a minor overlay does not make an otherwise single-medium image mixed. A
multi-panel drawing is medium drawing and drawing_kind composite, not medium
mixed.

Spatial context: exterior is outdoors or an outside building face; interior is
an enclosed indoor space; threshold requires substantial visible interior and
exterior with their transition as a main subject. An internal courtyard,
atrium, room, or construction void viewed from within the surrounding building
can remain interior even when its roof or enclosure is incomplete. A large
door, window, or opening alone does not make an otherwise clear interior or
exterior uncertain.

Choose framing_scale with this decision order:
1. material_detail when a surface, texture, or material is the primary subject
   and a larger architectural element is not the intended reading.
2. element_detail when a component, junction, assembly, or cropped portion of
   a building/room is the primary subject and the whole cannot be meaningfully
   read. A stair, facade bay, opening, or similar component remains
   element_detail when that component is the clear subject, even if enough of
   the surrounding room or facade is visible to orient the viewer.
3. overall when a building, facade, or room is the primary subject and is
   sufficiently complete to read as a whole, even if some surroundings appear.
4. site_context only when landscape, streets, neighboring fabric, or site
   relationships are essential to the image and the project is read as part of
   that wider setting. Do not choose site_context merely because background or
   foreground context is visible.

For camera_angle, aerial_top_down looks almost vertically down on roofs or the
site. Aerial_oblique looks down from unmistakably above the project's principal
roof planes while still showing oblique faces and enough plan or site to read
the view from above. A hillside viewpoint or one visible lower roof alone does
not make a view aerial. Aerial requires an exterior viewpoint looking over the
project, its roofs, or its site. Looking down into an internal room,
atrium, courtyard, or construction void from an upper floor, balcony, or
platform is elevated, not aerial. Elevated is a raised viewpoint that does not
look down across the project from above its roof plane. Eye_level includes
ordinary ground, room, and facade viewpoints.

For project_state, use construction_visible only with visible unfinished work:
incomplete structure or enclosure, formwork, active-work scaffolding, exposed
unfinished services/fabric, excavation, or comparable construction evidence.
A worker, ladder, tool, protective sheet, cleaning, or maintenance alone is not
proof of construction. If the architecture itself looks complete and no
unfinished work is visible, use visibly_finished.

Applicability rules:
- If in_scope is false, use a reject_reason other than none and set every axis
  except medium to not_applicable.
- If in_scope is true, reject_reason must be none.
- For photograph, drawing_kind is not_applicable; populate the other axes, or
  use unknown rather than guessing.
- For drawing, drawing_kind must be specific; spatial_context, framing_scale,
  camera_angle, and project_state are not_applicable.
- For rendering, drawing_kind must describe its representation/view (usually
  perspective); populate spatial_context, framing_scale, and camera_angle, and
  set project_state to not_applicable.
- For physical_model, all other axes are not_applicable.
- For mixed, other, or unknown medium, all other axes are not_applicable.
- Aerial camera angles may have exterior or unknown spatial context, never
  interior or threshold.
- Project state describes visibly supported state in photographs. Construction
  remains in scope; do not reject it.

Also reject a dominant blank/unreadable image, non-architectural subject,
text/logo-only image, or image with no visible project.

Uncertainty decision procedure, applied separately to every applicable axis:
1. Identify the visible evidence and the labels that evidence supports.
2. If exactly one label is supported, choose it and do not flag uncertainty.
3. If two or more labels remain genuinely plausible after applying the rules,
   choose the single best-supported controlled value and add that axis name to
   uncertain_axes. A concrete best value and an uncertainty flag can coexist.
4. Use unknown only when no controlled value can be preferred at all, and add
   that axis name to uncertain_axes.
5. Because scope is boolean, choose the better-supported in_scope branch and
   add scope to uncertain_axes when both branches remain plausible. Because an
   in-scope drawing requires a specific drawing_kind, choose the best-supported
   specific kind and add drawing_kind when two kinds remain plausible.
6. Do not flag uncertainty merely because an unsupported alternative can be
   imagined. There must be visible support for competing labels or missing
   visible evidence that prevents the distinction.

Mandatory second-pass uncertainty audit before returning JSON:
- For scope, flag scope when a sharply recognizable person is a central,
  foreground, posed, or portrait-like subject while a coherent designed space
  also remains substantial and legible. This does not apply to a cropped limb,
  motion-blurred silhouette, tiny scale figure, construction worker documenting
  work, or incidental occupant in a clearly architecture-dominant view. In all
  other cases, flag only when a person, event, artwork, or object and the
  architecture have genuinely comparable visual dominance. Also flag scope
  when an architectural-looking painting/object can reasonably be either
  project representation or standalone artwork. If a person clearly dominates
  and the remaining architecture is only a generic or minimally legible
  backdrop, reject without scope uncertainty. A blurred or cutout person used
  as a scale figure in an otherwise coherent architectural composite is
  incidental and does not create scope uncertainty.
- For medium, flag medium when handmade or stylized imagery can reasonably be
  either architectural drawing/painting or standalone artwork/other. A
  substantial architectural proposal or graphic layer visibly embedded in a
  photographic scene is clear mixed, not uncertain merely because one layer
  could be read as dominant.
- For spatial_context, flag spatial_context when incomplete construction or a
  missing enclosure around a camera positioned within or below the structure
  makes a room or roofed void plausibly either interior or threshold. A clearly
  bounded internal atrium or construction void seen from above remains clear
  interior even when open to the sky. Otherwise flag only when enclosure, camera
  position, and the depicted transition give substantial visible support to
  two labels. Do not flag merely because an indoor view has a large opening or
  because an outdoor view is covered by a roof or bridge; a clearly depicted
  passage whose interior-exterior transition is the main subject is a clear
  threshold.
- For framing_scale, flag framing_scale when the constructed project and its
  wider landscape/site are both compositionally substantial and neither
  clearly dominates, or when both a complete whole and a component reading are
  visibly supported. A repeated facade-bay crop that does not show a complete
  facade is clear element_detail, not an automatic boundary.
- For camera_angle, flag camera_angle when ground, horizon, roof, and
  convergence cues are insufficient. In particular, flag an element-detail
  facade crop with no ground, horizon, or roof-height reference when eye_level
  versus elevated cannot be recovered, and flag a near-horizontal viewpoint
  around upper-floor or roof height when eye_level versus elevated remains
  plausible. When sloping terrain makes a ground-based camera appear close to
  roof height, flag eye_level versus elevated unless visible camera/ground cues
  resolve it. Also flag the elevated/aerial boundary when the camera is near
  roof height and the amount of downward view does not resolve it.
- After checking every applicable axis against its closest adjacent label,
  return uncertain_axes=[] only when no listed or equivalent visible boundary
  applies. Do not flag a clear control merely because an alternative can be
  imagined.

An unknown value must name that axis in uncertain_axes. A concrete choice may
also name any applicable axis when another controlled value remains visibly
plausible as described above.
Use resolution_insufficient only when more pixels could materially resolve the
uncertain axis; ordinary category ambiguity is not a resolution problem. Do
not infer from filenames, URLs, project knowledge, or likely building type.
Combined search classes are derived later. Output JSON only."""


def _controlled_value(row: Mapping[str, Any], field: str) -> str:
    raw = row.get(field)
    if not isinstance(raw, str):
        raise ValueError(f"{field} must be a string")
    value = raw.strip().casefold()
    if value not in _VOCABULARY_SETS[field]:
        raise ValueError(f"unsupported {field}: {value}")
    return value


def _normalize_uncertain_axes(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("uncertain_axes must be a list")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("uncertain_axes must contain strings")
        axis = item.strip().casefold()
        if axis not in _UNCERTAIN_AXIS_SET:
            raise ValueError(f"uncertain_axes contains unsupported value: {axis}")
        if axis in normalized:
            raise ValueError(f"uncertain_axes contains duplicate value: {axis}")
        normalized.append(axis)
    return tuple(axis for axis in UNCERTAIN_AXIS_VALUES if axis in normalized)


def validate_axes_invariants(row: Mapping[str, Any]) -> None:
    """Reject internally inconsistent axis combinations.

    The function expects controlled, normalized values.  It deliberately
    rejects invalid applicability instead of silently rewriting model output.
    """

    in_scope = row.get("in_scope")
    resolution_insufficient = row.get("resolution_insufficient")
    if not isinstance(in_scope, bool):
        raise ValueError("in_scope must be boolean")
    if not isinstance(resolution_insufficient, bool):
        raise ValueError("resolution_insufficient must be boolean")

    for field in _VOCABULARIES:
        value = row.get(field)
        if value not in _VOCABULARY_SETS[field]:
            raise ValueError(f"unsupported {field}: {value}")

    uncertain = row.get("uncertain_axes")
    if not isinstance(uncertain, (list, tuple)):
        raise ValueError("uncertain_axes must be a list or tuple")
    if len(set(uncertain)) != len(uncertain) or any(
        axis not in _UNCERTAIN_AXIS_SET for axis in uncertain
    ):
        raise ValueError("uncertain_axes contains invalid or duplicate values")
    uncertain_set = set(uncertain)

    reject_reason = row["reject_reason"]
    medium = row["medium"]
    if in_scope and reject_reason != "none":
        raise ValueError("in-scope images must use reject_reason=none")
    if not in_scope and reject_reason == "none":
        raise ValueError("out-of-scope images require a reject reason")

    semantic_values = {field: row[field] for field in SEMANTIC_AXIS_FIELDS}
    if not in_scope:
        invalid = [field for field, value in semantic_values.items() if value != "not_applicable"]
        if invalid:
            raise ValueError(
                "out-of-scope images require not_applicable semantic axes: "
                + ", ".join(invalid)
            )
    elif medium == "photograph":
        if row["drawing_kind"] != "not_applicable":
            raise ValueError("photograph requires drawing_kind=not_applicable")
        invalid = [
            field
            for field in ("spatial_context", "framing_scale", "camera_angle", "project_state")
            if row[field] == "not_applicable"
        ]
        if invalid:
            raise ValueError(
                "photograph requires applicable or unknown axes: " + ", ".join(invalid)
            )
    elif medium == "drawing":
        if row["drawing_kind"] in {"not_applicable", "unknown"}:
            raise ValueError("drawing requires a specific drawing_kind")
        invalid = [
            field
            for field in ("spatial_context", "framing_scale", "camera_angle", "project_state")
            if row[field] != "not_applicable"
        ]
        if invalid:
            raise ValueError("drawing requires not_applicable axes: " + ", ".join(invalid))
    elif medium == "rendering":
        if row["drawing_kind"] in {"not_applicable", "unknown"}:
            raise ValueError("rendering requires a specific drawing_kind")
        invalid = [
            field
            for field in ("spatial_context", "framing_scale", "camera_angle")
            if row[field] == "not_applicable"
        ]
        if invalid:
            raise ValueError(
                "rendering requires applicable or unknown axes: " + ", ".join(invalid)
            )
        if row["project_state"] != "not_applicable":
            raise ValueError("rendering requires project_state=not_applicable")
    elif medium in {"physical_model", "mixed", "other", "unknown"}:
        invalid = [field for field, value in semantic_values.items() if value != "not_applicable"]
        if invalid:
            raise ValueError(
                f"{medium} requires not_applicable semantic axes: " + ", ".join(invalid)
            )

    if row["camera_angle"] in _AERIAL_ANGLES and row["spatial_context"] not in {
        "exterior",
        "unknown",
    }:
        raise ValueError("aerial camera angles require exterior or unknown spatial_context")

    axis_to_field = {
        "medium": "medium",
        "spatial_context": "spatial_context",
        "framing_scale": "framing_scale",
        "camera_angle": "camera_angle",
        "drawing_kind": "drawing_kind",
        "project_state": "project_state",
    }
    for axis, field in axis_to_field.items():
        value = row[field]
        if value == "unknown" and axis not in uncertain_set:
            raise ValueError(f"unknown {field} must be listed in uncertain_axes")
        if value == "not_applicable" and axis in uncertain_set:
            raise ValueError(f"not_applicable {field} cannot be uncertain")

    if resolution_insufficient and not uncertain_set:
        raise ValueError("resolution_insufficient requires at least one uncertain axis")


def derive_classification(row: Mapping[str, Any]) -> dict[str, Any]:
    """Derive the legacy search class and publication status deterministically."""

    validate_axes_invariants(row)
    in_scope = row["in_scope"]
    medium = row["medium"]
    spatial = row["spatial_context"]
    framing = row["framing_scale"]
    camera = row["camera_angle"]

    if not in_scope:
        return {
            "primary_class": "unknown",
            "secondary_classes": (),
            "usage_status": "rejected",
        }

    signals: set[str] = set()
    if medium in {"drawing", "rendering", "physical_model"}:
        signals.add("drawing")
    if camera in _AERIAL_ANGLES:
        signals.add("aerial")
    if framing in _DETAIL_FRAMING:
        signals.add("detail")
    if spatial in {"interior", "threshold"}:
        signals.add("interior")
    if spatial in {"exterior", "threshold"}:
        signals.add("exterior")

    if medium in {"drawing", "rendering", "physical_model"}:
        primary = "drawing"
    elif medium != "photograph":
        primary = "unknown"
    elif camera in _AERIAL_ANGLES:
        primary = "aerial"
    elif framing in _DETAIL_FRAMING:
        primary = "detail"
    elif spatial == "interior":
        primary = "interior"
    elif spatial == "exterior":
        primary = "exterior"
    else:
        primary = "unknown"

    secondary = tuple(
        label for label in LEGACY_CLASS_ORDER if label in signals and label != primary
    )
    if row["project_state"] in _ARCHIVE_STATES:
        usage_status = "archive_only"
    elif (
        primary == "unknown"
        or medium in {"mixed", "other", "unknown"}
        or spatial == "threshold"
        or bool(row["uncertain_axes"])
        or row["resolution_insufficient"]
    ):
        usage_status = "review_required"
    else:
        usage_status = "eligible"

    return {
        "primary_class": primary,
        "secondary_classes": secondary,
        "usage_status": usage_status,
    }


def normalize_axes_result(
    row: Mapping[str, Any], expected_asset_id: str
) -> dict[str, Any]:
    """Normalize and strictly validate one model result."""

    if not isinstance(row, Mapping):
        raise ValueError("vision result must be a JSON object")
    actual_fields = set(row)
    expected_fields = set(_MODEL_RESULT_FIELDS)
    missing = sorted(expected_fields - actual_fields)
    unexpected = sorted(actual_fields - expected_fields)
    if missing or unexpected:
        raise ValueError(
            f"vision result fields mismatch: missing={missing}, unexpected={unexpected}"
        )
    if not isinstance(expected_asset_id, str) or not expected_asset_id:
        raise ValueError("expected_asset_id must be a non-empty string")
    asset_id = row["asset_id"]
    if not isinstance(asset_id, str) or asset_id != expected_asset_id:
        raise ValueError("vision result asset_id does not match attachment order")

    in_scope = row["in_scope"]
    resolution_insufficient = row["resolution_insufficient"]
    if not isinstance(in_scope, bool):
        raise ValueError("in_scope must be boolean")
    if not isinstance(resolution_insufficient, bool):
        raise ValueError("resolution_insufficient must be boolean")
    evidence = row["evidence"]
    if not isinstance(evidence, str):
        raise ValueError("evidence must be a string")
    evidence = evidence.strip()
    if not evidence or len(evidence) > 500:
        raise ValueError("evidence must contain 1-500 characters")

    normalized: dict[str, Any] = {
        "inference_asset_id": asset_id,
        "in_scope": in_scope,
        **{field: _controlled_value(row, field) for field in _VOCABULARIES},
        "uncertain_axes": _normalize_uncertain_axes(row["uncertain_axes"]),
        "resolution_insufficient": resolution_insufficient,
        "evidence": evidence,
    }
    validate_axes_invariants(normalized)
    normalized.update(derive_classification(normalized))
    return normalized


def normalize_axes_batch(
    payload: Any, expected_asset_ids: Sequence[str]
) -> list[dict[str, Any]]:
    """Normalize an exact ordered batch returned by the shared Vision runtime."""

    expected = _validated_asset_ids(expected_asset_ids)
    if not isinstance(payload, (list, tuple)):
        raise ValueError("vision response must be a JSON array")
    if len(payload) != len(expected):
        raise ValueError("vision response count does not match attachment count")
    return [
        normalize_axes_result(row, asset_id)
        for row, asset_id in zip(payload, expected)
    ]
