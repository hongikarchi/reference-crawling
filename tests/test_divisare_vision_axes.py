from __future__ import annotations

from copy import deepcopy

import pytest

from canonical.divisare_vision_axes import (
    AXIS_OUTPUT_SCHEMA,
    AXIS_PROMPT_VERSION,
    CAMERA_ANGLE_VALUES,
    DRAWING_KIND_VALUES,
    FRAMING_SCALE_VALUES,
    MEDIUM_VALUES,
    PROJECT_STATE_VALUES,
    REJECT_REASON_VALUES,
    SPATIAL_CONTEXT_VALUES,
    UNCERTAIN_AXIS_VALUES,
    compose_axes_prompt,
    derive_classification,
    normalize_axes_batch,
    normalize_axes_result,
)


def _photo(asset_id: str = "sample-0001", **changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "asset_id": asset_id,
        "in_scope": True,
        "reject_reason": "none",
        "medium": "photograph",
        "spatial_context": "exterior",
        "framing_scale": "overall",
        "camera_angle": "eye_level",
        "drawing_kind": "not_applicable",
        "project_state": "visibly_finished",
        "uncertain_axes": [],
        "resolution_insufficient": False,
        "evidence": "A completed facade is visible from ground level.",
    }
    row.update(changes)
    return row


def _drawing(asset_id: str = "sample-0001", **changes: object) -> dict[str, object]:
    row = _photo(
        asset_id,
        medium="drawing",
        spatial_context="not_applicable",
        framing_scale="not_applicable",
        camera_angle="not_applicable",
        drawing_kind="plan",
        project_state="not_applicable",
        evidence="Linework visibly describes a floor plan.",
    )
    row.update(changes)
    return row


def test_output_schema_exposes_exact_controlled_contract() -> None:
    item = AXIS_OUTPUT_SCHEMA["properties"]["results"]["items"]
    properties = item["properties"]
    assert item["additionalProperties"] is False
    assert set(item["required"]) == set(properties)
    assert tuple(properties["reject_reason"]["enum"]) == REJECT_REASON_VALUES
    assert tuple(properties["medium"]["enum"]) == MEDIUM_VALUES
    assert tuple(properties["spatial_context"]["enum"]) == SPATIAL_CONTEXT_VALUES
    assert tuple(properties["framing_scale"]["enum"]) == FRAMING_SCALE_VALUES
    assert tuple(properties["camera_angle"]["enum"]) == CAMERA_ANGLE_VALUES
    assert tuple(properties["drawing_kind"]["enum"]) == DRAWING_KIND_VALUES
    assert tuple(properties["project_state"]["enum"]) == PROJECT_STATE_VALUES
    assert tuple(properties["uncertain_axes"]["items"]["enum"]) == UNCERTAIN_AXIS_VALUES
    # Codex structured outputs do not accept JSON Schema uniqueItems. Runtime
    # normalization still rejects duplicate uncertainty axes.
    assert "uniqueItems" not in properties["uncertain_axes"]
    assert "primary_class" not in properties


def test_prompt_defines_independent_axes_and_pixel_only_evidence() -> None:
    prompt = compose_axes_prompt(["sample-0002", "sample-0001"])
    assert AXIS_PROMPT_VERSION == "divisare-image-axes-prompt-v2.5.0"
    assert "1. sample-0002\n2. sample-0001" in prompt
    assert "straight-on facade is photograph" in prompt
    assert "A hillside viewpoint or one visible lower roof alone" in prompt
    assert "1024-pixel legibility" in prompt
    assert "filenames, URLs, project knowledge" in prompt
    assert "Combined search classes are derived later" in prompt
    with pytest.raises(ValueError, match="duplicates"):
        compose_axes_prompt(["same", "same"])
    with pytest.raises(ValueError, match="must not be empty"):
        compose_axes_prompt([])


def test_prompt_v25_defines_boundary_decision_rules() -> None:
    prompt = compose_axes_prompt(["sample-0001"])
    assert "Make decisions in this order: scope, medium" in prompt
    assert "isolated relief, material mock-up" in prompt
    assert "drawing_kind composite, even when no panel dominates" in prompt
    assert "Otherwise reject it as\n  multi_panel_without_dominant_image" in prompt
    assert "substantial generated\nproposal embedded in a photographic context" in prompt
    assert "Choose framing_scale with this decision order" in prompt
    assert "component remains\n   element_detail" in prompt
    assert "Do not choose site_context merely because" in prompt
    assert "A worker, ladder, tool, protective sheet" in prompt
    assert "Uncertainty decision procedure" in prompt
    assert "choose the single best-supported controlled value" in prompt
    assert "Use unknown only when no controlled value can be preferred" in prompt
    assert "Mandatory second-pass uncertainty audit" in prompt
    assert "return uncertain_axes=[] only when" in prompt
    assert "repeated facade-bay crop" in prompt
    assert "sharply recognizable person is a central" in prompt
    assert "cropped limb,\n  motion-blurred silhouette" in prompt
    assert "Do not flag merely because an indoor view has a large opening" in prompt
    assert "internal room,\natrium, courtyard, or construction void" in prompt
    assert "element-detail\n  facade crop" in prompt
    assert "generic or minimally legible\n  backdrop" in prompt
    assert "photographic scene is clear mixed" in prompt
    assert "construction void seen from above remains clear\n  interior" in prompt
    assert "A hillside viewpoint or one visible lower roof alone" in prompt
    assert "sloping terrain makes a ground-based camera appear close" in prompt
    assert "scope is boolean" in prompt
    assert "in-scope drawing requires a specific drawing_kind" in prompt
    assert "Do not flag uncertainty merely because" in prompt


def test_photo_detail_derives_detail_plus_exterior() -> None:
    result = normalize_axes_result(
        _photo(framing_scale="element_detail"), "sample-0001"
    )
    assert result["inference_asset_id"] == "sample-0001"
    assert result["primary_class"] == "detail"
    assert result["secondary_classes"] == ("exterior",)
    assert result["usage_status"] == "eligible"


def test_aerial_precedes_detail_and_secondary_order_is_stable() -> None:
    result = normalize_axes_result(
        _photo(camera_angle="aerial_oblique", framing_scale="material_detail"),
        "sample-0001",
    )
    assert result["primary_class"] == "aerial"
    assert result["secondary_classes"] == ("detail", "exterior")


def test_threshold_is_preserved_as_two_secondary_classes_for_review() -> None:
    result = normalize_axes_result(
        _photo(spatial_context="threshold"), "sample-0001"
    )
    assert result["primary_class"] == "unknown"
    assert result["secondary_classes"] == ("interior", "exterior")
    assert result["usage_status"] == "review_required"


def test_drawing_rendering_and_physical_model_project_to_drawing() -> None:
    drawing = normalize_axes_result(_drawing(), "sample-0001")
    assert drawing["primary_class"] == "drawing"
    assert drawing["usage_status"] == "eligible"

    rendering = _drawing(
        medium="rendering",
        spatial_context="interior",
        framing_scale="overall",
        camera_angle="eye_level",
        drawing_kind="perspective",
    )
    rendered = normalize_axes_result(rendering, "sample-0001")
    assert rendered["primary_class"] == "drawing"
    assert rendered["secondary_classes"] == ("interior",)

    model = _drawing(
        medium="physical_model",
        drawing_kind="not_applicable",
    )
    physical = normalize_axes_result(model, "sample-0001")
    assert physical["primary_class"] == "drawing"
    assert physical["secondary_classes"] == ()


def test_rejected_image_is_never_projected_to_a_search_class() -> None:
    rejected = _photo(
        in_scope=False,
        reject_reason="people_or_event",
        spatial_context="not_applicable",
        framing_scale="not_applicable",
        camera_angle="not_applicable",
        project_state="not_applicable",
        evidence="People at an event dominate the image.",
    )
    result = normalize_axes_result(rejected, "sample-0001")
    assert result["primary_class"] == "unknown"
    assert result["secondary_classes"] == ()
    assert result["usage_status"] == "rejected"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"in_scope": False}, "require a reject reason"),
        ({"reject_reason": "other"}, "in-scope images"),
        ({"drawing_kind": "elevation"}, "photograph requires"),
        ({"spatial_context": "not_applicable"}, "photograph requires"),
        (
            {"camera_angle": "aerial_top_down", "spatial_context": "interior"},
            "aerial camera angles",
        ),
        ({"spatial_context": "unknown"}, "must be listed"),
        ({"uncertain_axes": ["drawing_kind"]}, "cannot be uncertain"),
        ({"resolution_insufficient": True}, "requires at least one"),
    ],
)
def test_photo_invariants_are_strict(changes: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        normalize_axes_result(_photo(**changes), "sample-0001")


def test_medium_specific_applicability_is_strict() -> None:
    with pytest.raises(ValueError, match="specific drawing_kind"):
        normalize_axes_result(_drawing(drawing_kind="unknown", uncertain_axes=["drawing_kind"]), "sample-0001")
    with pytest.raises(ValueError, match="drawing requires not_applicable"):
        normalize_axes_result(_drawing(spatial_context="exterior"), "sample-0001")
    with pytest.raises(ValueError, match="rendering requires project_state"):
        normalize_axes_result(
            _drawing(
                medium="rendering",
                spatial_context="exterior",
                framing_scale="overall",
                camera_angle="eye_level",
                drawing_kind="perspective",
                project_state="visibly_finished",
            ),
            "sample-0001",
        )
    with pytest.raises(ValueError, match="mixed requires not_applicable"):
        normalize_axes_result(_photo(medium="mixed"), "sample-0001")


def test_uncertainty_resolution_and_archive_usage_precedence() -> None:
    uncertain = normalize_axes_result(
        _photo(uncertain_axes=["framing_scale"], resolution_insufficient=True),
        "sample-0001",
    )
    assert uncertain["usage_status"] == "review_required"
    assert uncertain["uncertain_axes"] == ("framing_scale",)

    archived = normalize_axes_result(
        _photo(
            project_state="construction_visible",
            uncertain_axes=["framing_scale"],
            resolution_insufficient=True,
        ),
        "sample-0001",
    )
    assert archived["usage_status"] == "archive_only"


def test_normalization_rejects_type_field_and_identity_errors() -> None:
    extra = _photo()
    extra["primary_class"] = "exterior"
    with pytest.raises(ValueError, match="unexpected"):
        normalize_axes_result(extra, "sample-0001")

    missing = _photo()
    del missing["evidence"]
    with pytest.raises(ValueError, match="missing"):
        normalize_axes_result(missing, "sample-0001")

    with pytest.raises(ValueError, match="in_scope must be boolean"):
        normalize_axes_result(_photo(in_scope=1), "sample-0001")
    with pytest.raises(ValueError, match="does not match"):
        normalize_axes_result(_photo(), "sample-9999")
    with pytest.raises(ValueError, match="unsupported medium"):
        normalize_axes_result(_photo(medium="photo"), "sample-0001")


def test_batch_requires_exact_count_order_and_unique_expected_ids() -> None:
    rows = normalize_axes_batch(
        [_photo("sample-0001"), _drawing("sample-0002")],
        ["sample-0001", "sample-0002"],
    )
    assert [row["inference_asset_id"] for row in rows] == ["sample-0001", "sample-0002"]
    with pytest.raises(ValueError, match="count"):
        normalize_axes_batch([_photo()], ["sample-0001", "sample-0002"])
    with pytest.raises(ValueError, match="attachment order"):
        normalize_axes_batch(
            [_drawing("sample-0002"), _photo("sample-0001")],
            ["sample-0001", "sample-0002"],
        )
    with pytest.raises(ValueError, match="duplicates"):
        normalize_axes_batch([_photo(), deepcopy(_photo())], ["sample-0001", "sample-0001"])


def test_derive_classification_rejects_unvalidated_combinations() -> None:
    normalized = normalize_axes_result(_photo(), "sample-0001")
    axes_only = {
        key: value
        for key, value in normalized.items()
        if key not in {"inference_asset_id", "primary_class", "secondary_classes", "usage_status", "evidence"}
    }
    axes_only["camera_angle"] = "aerial_oblique"
    axes_only["spatial_context"] = "threshold"
    with pytest.raises(ValueError, match="aerial camera angles"):
        derive_classification(axes_only)
