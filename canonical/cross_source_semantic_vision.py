"""Frozen pixel-only semantic contract shared by architecture sources.

The Vision model reports orthogonal facts that are visible in the attached
image.  Hero candidacy and coverage slots are deliberately derived in Python;
the model never sees source, project, URL, rank, or filename metadata.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


CONTRACT_VERSION = "cross-source-image-semantics-v1.0.0"
PROMPT_VERSION = "cross-source-image-semantics-prompt-v1.0.0"
TRANSFORM_VERSION = "cross-source-vision-input-1024-jpeg-v1.0.0"

REJECT_REASONS = (
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
MEDIA = ("photograph", "drawing", "rendering", "physical_model", "mixed", "other", "unknown")
SPATIAL_CONTEXTS = ("exterior", "interior", "threshold", "not_applicable", "unknown")
FRAMING_SCALES = ("site_context", "overall", "element_detail", "material_detail", "not_applicable", "unknown")
CAMERA_ANGLES = ("eye_level", "elevated", "aerial_oblique", "aerial_top_down", "not_applicable", "unknown")
DRAWING_KINDS = (
    "plan", "site_plan", "section", "elevation", "axonometric", "perspective",
    "detail", "diagram", "sketch", "composite", "other", "not_applicable", "unknown",
)
PROJECT_STATES = (
    "visibly_finished", "construction_visible", "ruin_or_abandoned_visible",
    "demolition_visible", "not_applicable", "unknown",
)
PROJECT_LEGIBILITY = ("high", "medium", "low", "none", "unknown")
UNCERTAIN_AXES = (
    "scope", "medium", "spatial_context", "framing_scale", "camera_angle",
    "drawing_kind", "project_state", "project_legibility",
)

MODEL_FIELDS = (
    "asset_id", "in_scope", "reject_reason", "medium", "spatial_context",
    "framing_scale", "camera_angle", "drawing_kind", "project_state",
    "project_legibility", "uncertain_axes", "resolution_insufficient", "evidence",
)

_VOCABULARIES = {
    "reject_reason": REJECT_REASONS,
    "medium": MEDIA,
    "spatial_context": SPATIAL_CONTEXTS,
    "framing_scale": FRAMING_SCALES,
    "camera_angle": CAMERA_ANGLES,
    "drawing_kind": DRAWING_KINDS,
    "project_state": PROJECT_STATES,
    "project_legibility": PROJECT_LEGIBILITY,
}
_VOCABULARY_SETS = {key: frozenset(values) for key, values in _VOCABULARIES.items()}
_UNCERTAIN_SET = frozenset(UNCERTAIN_AXES)
_AERIAL = frozenset({"aerial_oblique", "aerial_top_down"})
_ARCHIVE = frozenset({"construction_visible", "ruin_or_abandoned_visible", "demolition_visible"})


def _enum(values: Sequence[str]) -> dict[str, Any]:
    return {"type": "string", "enum": list(values)}


RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": list(MODEL_FIELDS),
    "properties": {
        "asset_id": {"type": "string", "minLength": 1},
        "in_scope": {"type": "boolean"},
        "reject_reason": _enum(REJECT_REASONS),
        "medium": _enum(MEDIA),
        "spatial_context": _enum(SPATIAL_CONTEXTS),
        "framing_scale": _enum(FRAMING_SCALES),
        "camera_angle": _enum(CAMERA_ANGLES),
        "drawing_kind": _enum(DRAWING_KINDS),
        "project_state": _enum(PROJECT_STATES),
        "project_legibility": _enum(PROJECT_LEGIBILITY),
        "uncertain_axes": {"type": "array", "items": _enum(UNCERTAIN_AXES)},
        "resolution_insufficient": {"type": "boolean"},
        "evidence": {"type": "string", "minLength": 1, "maxLength": 500},
    },
}

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["results"],
    "properties": {"results": {"type": "array", "items": RESULT_SCHEMA}},
}


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
    """Return the frozen, metadata-blind prompt for an ordered image batch."""

    ordered_ids = _asset_ids(asset_ids)
    ordered = "\n".join(f"{index}. {asset_id}" for index, asset_id in enumerate(ordered_ids, 1))
    return f"""Classify each attached architecture image using only visible pixels.

The attachments are in this exact order:
{ordered}

Return exactly one object per attachment in the same order inside
{{"results": [...]}}. Copy each opaque asset_id exactly. Return every field:
in_scope, reject_reason, medium, spatial_context, framing_scale, camera_angle,
drawing_kind, project_state, project_legibility, uncertain_axes,
resolution_insufficient, and one short pixel-grounded evidence sentence.

Controlled values:
- reject_reason: {list(REJECT_REASONS)}
- medium: {list(MEDIA)}
- spatial_context: {list(SPATIAL_CONTEXTS)}
- framing_scale: {list(FRAMING_SCALES)}
- camera_angle: {list(CAMERA_ANGLES)}
- drawing_kind: {list(DRAWING_KINDS)}
- project_state: {list(PROJECT_STATES)}
- project_legibility: {list(PROJECT_LEGIBILITY)}
- uncertain_axes: zero or more of {list(UNCERTAIN_AXES)}

Scope rules:
- In scope means a coherent architectural project, space, component,
  representation, intentional architectural model, construction, ruin, or
  demolition is visually dominant.
- Reject blank/unreadable, non-architectural, people/event-dominant,
  product/art/sample-dominant, text/logo-only, or no-project-visible images.
- A coherent single-project drawing sheet or montage remains in scope as a
  drawing/composite. Other multi-panel images require one dominant classifiable
  architectural panel.
- People, text, and objects do not cause rejection when architecture remains
  dominant. Do not use filenames, URLs, source knowledge, or project metadata.

Axis rules:
- medium is the visible representation, not the file format. A photographed
  facade is photograph; a photographed intentional scale model is
  physical_model; visibly computer-generated architecture is rendering.
- spatial_context is exterior, enclosed interior, or threshold only when the
  interior/exterior transition is itself substantial.
- framing_scale: material_detail for a surface/material; element_detail for a
  component or cropped portion; overall for a readable whole building/facade/
  room; site_context only when wider site relationships are essential.
- aerial_top_down looks nearly vertically down. aerial_oblique unmistakably
  looks down from above principal roof planes. A raised indoor/balcony/hillside
  viewpoint is elevated, not automatically aerial.
- project_state uses construction_visible only for visible unfinished work.
  Workers, tools, cleaning, or maintenance alone are insufficient.
- project_legibility rates how clearly one coherent architectural project or
  proposal can be read: high=clear whole or intentional view, medium=clear but
  incomplete/obscured, low=project present but weakly readable, none=no
  coherent project, unknown=no value can be preferred.

Applicability rules:
- Out of scope: reject_reason is not none; all axes except medium and
  project_legibility are not_applicable; project_legibility is none.
- In scope: reject_reason is none and project_legibility is high, medium, low,
  or unknown, never none.
- Photograph: drawing_kind is not_applicable; spatial_context, framing_scale,
  camera_angle, and project_state are applicable or unknown.
- Drawing: drawing_kind is specific; spatial_context, framing_scale,
  camera_angle, and project_state are not_applicable.
- Rendering: drawing_kind is specific (usually perspective); spatial_context,
  framing_scale, and camera_angle are applicable or unknown; project_state is
  not_applicable.
- Physical_model, mixed, other, or unknown medium: spatial_context,
  framing_scale, camera_angle, drawing_kind, and project_state are
  not_applicable.
- Aerial angles require exterior or unknown spatial_context.

Uncertainty rules:
- Judge every applicable axis independently. Use a concrete best value when
  one is best supported; also list the axis when another value remains
  genuinely plausible. Use unknown only when no controlled value can be
  preferred, and always list that axis in uncertain_axes.
- List scope when in-scope and out-of-scope readings are both visibly
  plausible. Do not list uncertainty merely because an alternative can be
  imagined.
- resolution_insufficient is true only when more pixels could materially
  resolve a listed uncertainty. It requires at least one uncertain axis.
- Perform a second pass for people-versus-project dominance, drawing-versus-
  artwork, interior-versus-threshold, overall-versus-detail/site, eye-level-
  versus-elevated/aerial, construction state, and project legibility.

Output JSON only."""


def _controlled(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = value.strip().casefold()
    if normalized not in _VOCABULARY_SETS[field]:
        raise ValueError(f"unsupported {field}: {normalized}")
    return normalized


def _uncertain(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("uncertain_axes must be a list")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("uncertain_axes must contain strings")
        axis = item.strip().casefold()
        if axis not in _UNCERTAIN_SET:
            raise ValueError(f"unsupported uncertain axis: {axis}")
        if axis in normalized:
            raise ValueError(f"duplicate uncertain axis: {axis}")
        normalized.append(axis)
    return tuple(axis for axis in UNCERTAIN_AXES if axis in normalized)


def validate_invariants(row: Mapping[str, Any]) -> None:
    if not isinstance(row.get("in_scope"), bool):
        raise ValueError("in_scope must be boolean")
    if not isinstance(row.get("resolution_insufficient"), bool):
        raise ValueError("resolution_insufficient must be boolean")
    for field, values in _VOCABULARY_SETS.items():
        if row.get(field) not in values:
            raise ValueError(f"unsupported {field}: {row.get(field)}")
    uncertain = tuple(row.get("uncertain_axes", ()))
    if len(uncertain) != len(set(uncertain)) or any(axis not in _UNCERTAIN_SET for axis in uncertain):
        raise ValueError("uncertain_axes contains invalid or duplicate values")
    uncertain_set = set(uncertain)

    in_scope = row["in_scope"]
    if in_scope != (row["reject_reason"] == "none"):
        raise ValueError("scope and reject_reason disagree")
    semantic = ("spatial_context", "framing_scale", "camera_angle", "drawing_kind", "project_state")
    medium = row["medium"]
    if not in_scope:
        if any(row[field] != "not_applicable" for field in semantic):
            raise ValueError("out-of-scope semantic axes must be not_applicable")
        if row["project_legibility"] != "none":
            raise ValueError("out-of-scope project_legibility must be none")
    else:
        if row["project_legibility"] == "none":
            raise ValueError("in-scope project_legibility cannot be none")
        if medium == "photograph":
            if row["drawing_kind"] != "not_applicable":
                raise ValueError("photograph requires drawing_kind=not_applicable")
            if any(row[field] == "not_applicable" for field in ("spatial_context", "framing_scale", "camera_angle", "project_state")):
                raise ValueError("photograph requires applicable visual axes")
        elif medium == "drawing":
            if row["drawing_kind"] in {"not_applicable", "unknown"}:
                raise ValueError("drawing requires a specific drawing_kind")
            if any(row[field] != "not_applicable" for field in ("spatial_context", "framing_scale", "camera_angle", "project_state")):
                raise ValueError("drawing requires not_applicable non-drawing axes")
        elif medium == "rendering":
            if row["drawing_kind"] in {"not_applicable", "unknown"}:
                raise ValueError("rendering requires a specific drawing_kind")
            if any(row[field] == "not_applicable" for field in ("spatial_context", "framing_scale", "camera_angle")):
                raise ValueError("rendering requires applicable view axes")
            if row["project_state"] != "not_applicable":
                raise ValueError("rendering requires project_state=not_applicable")
        elif medium in {"physical_model", "mixed", "other", "unknown"}:
            if any(row[field] != "not_applicable" for field in semantic):
                raise ValueError(f"{medium} requires not_applicable semantic axes")

    if row["camera_angle"] in _AERIAL and row["spatial_context"] not in {"exterior", "unknown"}:
        raise ValueError("aerial angles require exterior or unknown spatial_context")
    for axis in ("medium", "spatial_context", "framing_scale", "camera_angle", "drawing_kind", "project_state", "project_legibility"):
        value = row[axis]
        if value == "unknown" and axis not in uncertain_set:
            raise ValueError(f"unknown {axis} must be uncertain")
        if value == "not_applicable" and axis in uncertain_set:
            raise ValueError(f"not_applicable {axis} cannot be uncertain")
    if row["resolution_insufficient"] and not uncertain_set:
        raise ValueError("resolution_insufficient requires uncertainty")


def normalize_result(row: Mapping[str, Any], expected_asset_id: str) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        raise ValueError("Vision result must be an object")
    missing = sorted(set(MODEL_FIELDS) - set(row))
    extra = sorted(set(row) - set(MODEL_FIELDS))
    if missing or extra:
        raise ValueError(f"Vision result fields mismatch: missing={missing}, extra={extra}")
    if row.get("asset_id") != expected_asset_id:
        raise ValueError("Vision asset_id does not match attachment order")
    if not isinstance(row.get("in_scope"), bool) or not isinstance(row.get("resolution_insufficient"), bool):
        raise ValueError("boolean fields must be booleans")
    evidence = row.get("evidence")
    if not isinstance(evidence, str) or not evidence.strip() or len(evidence.strip()) > 500:
        raise ValueError("evidence must contain 1-500 characters")
    normalized = {
        "asset_id": expected_asset_id,
        "in_scope": row["in_scope"],
        **{field: _controlled(row, field) for field in _VOCABULARIES},
        "uncertain_axes": _uncertain(row["uncertain_axes"]),
        "resolution_insufficient": row["resolution_insufficient"],
        "evidence": evidence.strip(),
    }
    validate_invariants(normalized)
    return normalized


def normalize_batch(rows: Sequence[Mapping[str, Any]], asset_ids: Sequence[str]) -> tuple[dict[str, Any], ...]:
    expected = _asset_ids(asset_ids)
    if not isinstance(rows, (list, tuple)) or len(rows) != len(expected):
        raise ValueError("Vision result count does not match attachment count")
    return tuple(normalize_result(row, asset_id) for row, asset_id in zip(rows, expected))


def derive_hero_decision(row: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
    """Return a non-authoritative deterministic hero tier and reason codes."""

    validate_invariants(row)
    reasons: list[str] = []
    if not row["in_scope"]:
        return "rejected", (f"reject:{row['reject_reason']}",)
    if row["project_state"] in _ARCHIVE:
        reasons.append(f"state:{row['project_state']}")
        return "archive_only", tuple(reasons)
    critical = {"scope", "medium", "project_legibility"}.intersection(row["uncertain_axes"])
    if critical or row["resolution_insufficient"] or row["project_legibility"] in {"low", "unknown"}:
        reasons.extend(f"uncertain:{axis}" for axis in sorted(critical))
        if row["resolution_insufficient"]:
            reasons.append("resolution_insufficient")
        reasons.append(f"legibility:{row['project_legibility']}")
        return "qa_only", tuple(dict.fromkeys(reasons))
    if row["medium"] == "photograph" and row["project_legibility"] in {"high", "medium"}:
        if row["framing_scale"] in {"overall", "site_context"}:
            return "preferred", ("photograph", f"legibility:{row['project_legibility']}", f"framing:{row['framing_scale']}")
        return "eligible", ("photograph", f"legibility:{row['project_legibility']}", f"framing:{row['framing_scale']}")
    return "fallback", (f"medium:{row['medium']}", f"legibility:{row['project_legibility']}")


def derive_coverage_slots(row: Mapping[str, Any]) -> tuple[str, ...]:
    """Return zero or more non-authoritative coverage memberships."""

    validate_invariants(row)
    if not row["in_scope"]:
        return ()
    slots: set[str] = set()
    medium = row["medium"]
    spatial = row["spatial_context"]
    framing = row["framing_scale"]
    angle = row["camera_angle"]
    drawing = row["drawing_kind"]
    state = row["project_state"]
    if spatial == "exterior" and framing == "overall":
        slots.add("exterior_overall")
    if spatial == "exterior" and framing == "site_context":
        slots.add("exterior_context")
    if spatial in {"interior", "threshold"}:
        slots.add("interior")
    if medium in {"drawing", "rendering"}:
        if drawing in {"plan", "site_plan"}:
            slots.add("drawing_plan")
        elif drawing == "section":
            slots.add("drawing_section")
        else:
            slots.add("drawing_other")
    if framing in {"element_detail", "material_detail"} or drawing == "detail":
        slots.add("detail")
    if angle in _AERIAL:
        slots.add("aerial_context")
    if medium in {"physical_model", "rendering"}:
        slots.add("model_or_render")
    if state in _ARCHIVE:
        slots.add("construction_or_archive")
    return tuple(sorted(slots))
