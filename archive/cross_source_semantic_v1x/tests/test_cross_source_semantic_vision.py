from __future__ import annotations

import inspect

import pytest

from canonical.cross_source_semantic_vision import (
    MODEL_FIELDS,
    OUTPUT_SCHEMA,
    PROJECT_LEGIBILITY,
    RESULT_SCHEMA,
    UNCERTAIN_AXES,
    compose_prompt,
    derive_coverage_slots,
    derive_hero_decision,
    normalize_batch,
    normalize_result,
    validate_invariants,
)


def _result(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "asset_id": "asset-1",
        "in_scope": True,
        "reject_reason": "none",
        "medium": "photograph",
        "spatial_context": "exterior",
        "framing_scale": "overall",
        "camera_angle": "eye_level",
        "drawing_kind": "not_applicable",
        "project_state": "visibly_finished",
        "project_legibility": "high",
        "uncertain_axes": [],
        "resolution_insufficient": False,
        "evidence": "A coherent building facade is visibly dominant.",
    }
    row.update(overrides)
    return row


def _out_of_scope(**overrides: object) -> dict[str, object]:
    row = _result(
        in_scope=False,
        reject_reason="no_project_visible",
        spatial_context="not_applicable",
        framing_scale="not_applicable",
        camera_angle="not_applicable",
        drawing_kind="not_applicable",
        project_state="not_applicable",
        project_legibility="none",
    )
    row.update(overrides)
    return row


def _drawing(kind: str = "plan", **overrides: object) -> dict[str, object]:
    row = _result(
        medium="drawing",
        spatial_context="not_applicable",
        framing_scale="not_applicable",
        camera_angle="not_applicable",
        drawing_kind=kind,
        project_state="not_applicable",
    )
    row.update(overrides)
    return row


def _rendering(kind: str = "perspective", **overrides: object) -> dict[str, object]:
    row = _result(
        medium="rendering",
        drawing_kind=kind,
        project_state="not_applicable",
    )
    row.update(overrides)
    return row


def _model(**overrides: object) -> dict[str, object]:
    row = _result(
        medium="physical_model",
        spatial_context="not_applicable",
        framing_scale="not_applicable",
        camera_angle="not_applicable",
        drawing_kind="not_applicable",
        project_state="not_applicable",
    )
    row.update(overrides)
    return row


def test_schema_is_closed_source_neutral_and_keeps_derived_fields_out() -> None:
    properties = RESULT_SCHEMA["properties"]
    assert RESULT_SCHEMA["additionalProperties"] is False
    assert set(RESULT_SCHEMA["required"]) == set(MODEL_FIELDS)
    assert set(properties) == set(MODEL_FIELDS)
    assert set(PROJECT_LEGIBILITY) == {"high", "medium", "low", "none", "unknown"}
    assert properties["project_legibility"]["enum"] == list(PROJECT_LEGIBILITY)
    assert properties["uncertain_axes"]["items"]["enum"] == list(UNCERTAIN_AXES)
    assert OUTPUT_SCHEMA["additionalProperties"] is False
    assert OUTPUT_SCHEMA["required"] == ["results"]

    forbidden = {
        "source",
        "source_asset_id",
        "source_building_id",
        "project_id",
        "project_name",
        "canonical_url",
        "fetch_url",
        "filename",
        "editorial_rank",
        "p2_shortlist_rank",
        "hero_tier",
        "coverage_slots",
    }
    assert forbidden.isdisjoint(properties)


def test_prompt_accepts_only_ordered_opaque_ids_and_is_metadata_blind() -> None:
    assert tuple(inspect.signature(compose_prompt).parameters) == ("asset_ids",)
    prompt = compose_prompt(("opaque-A", "opaque-B"))
    assert prompt.index("1. opaque-A") < prompt.index("2. opaque-B")
    assert prompt.count("opaque-A") == 1
    assert prompt.count("opaque-B") == 1
    assert "using only visible pixels" in prompt
    assert "Do not use filenames, URLs, source knowledge, or project metadata." in prompt
    assert "project_legibility" in prompt
    assert "hero_tier" not in prompt
    assert "coverage_slots" not in prompt

    replay = compose_prompt(("replacement-A", "replacement-B"))
    assert prompt.replace("opaque-A", "replacement-A").replace(
        "opaque-B", "replacement-B"
    ) == replay


def test_normalization_casefolds_controlled_values_and_orders_uncertainty() -> None:
    row = _result(
        medium=" Photograph ",
        spatial_context=" EXTERIOR ",
        uncertain_axes=["project_legibility", "Scope"],
        evidence="  Visible facade.  ",
    )
    normalized = normalize_result(row, "asset-1")
    assert normalized["medium"] == "photograph"
    assert normalized["spatial_context"] == "exterior"
    assert normalized["uncertain_axes"] == ("scope", "project_legibility")
    assert normalized["evidence"] == "Visible facade."


@pytest.mark.parametrize("legibility", ["high", "medium", "low", "unknown"])
def test_in_scope_project_legibility_values(legibility: str) -> None:
    uncertain = ["project_legibility"] if legibility == "unknown" else []
    validate_invariants(_result(project_legibility=legibility, uncertain_axes=uncertain))


def test_project_legibility_none_is_reserved_for_out_of_scope() -> None:
    validate_invariants(_out_of_scope())
    with pytest.raises(ValueError, match="in-scope project_legibility cannot be none"):
        validate_invariants(_result(project_legibility="none"))
    with pytest.raises(ValueError, match="out-of-scope project_legibility must be none"):
        validate_invariants(_out_of_scope(project_legibility="low"))


def test_scope_and_reject_reason_must_agree() -> None:
    with pytest.raises(ValueError, match="scope and reject_reason disagree"):
        validate_invariants(_result(reject_reason="people_or_event"))
    with pytest.raises(ValueError, match="scope and reject_reason disagree"):
        validate_invariants(_out_of_scope(reject_reason="none"))


@pytest.mark.parametrize(
    "row",
    [
        _result(),
        _drawing("section"),
        _rendering(),
        _model(),
        _result(
            medium="mixed",
            spatial_context="not_applicable",
            framing_scale="not_applicable",
            camera_angle="not_applicable",
            drawing_kind="not_applicable",
            project_state="not_applicable",
        ),
    ],
)
def test_medium_specific_applicability_accepts_valid_rows(row: dict[str, object]) -> None:
    validate_invariants(row)


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (_result(drawing_kind="perspective"), "photograph requires drawing_kind"),
        (_result(spatial_context="not_applicable"), "photograph requires applicable"),
        (_drawing("unknown"), "drawing requires a specific"),
        (_drawing("plan", spatial_context="exterior"), "drawing requires not_applicable"),
        (_rendering(project_state="visibly_finished"), "rendering requires project_state"),
        (_model(framing_scale="overall"), "physical_model requires not_applicable"),
        (_out_of_scope(camera_angle="eye_level"), "out-of-scope semantic axes"),
    ],
)
def test_medium_specific_applicability_rejects_invalid_rows(
    row: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_invariants(row)


@pytest.mark.parametrize(
    ("field", "axis"),
    [
        ("spatial_context", "spatial_context"),
        ("framing_scale", "framing_scale"),
        ("camera_angle", "camera_angle"),
        ("project_state", "project_state"),
        ("project_legibility", "project_legibility"),
    ],
)
def test_unknown_requires_matching_uncertain_axis(field: str, axis: str) -> None:
    with pytest.raises(ValueError, match=f"unknown {axis} must be uncertain"):
        validate_invariants(_result(**{field: "unknown"}))
    validate_invariants(_result(**{field: "unknown", "uncertain_axes": [axis]}))


def test_unknown_medium_is_valid_only_with_uncertainty_and_nonapplicable_axes() -> None:
    row = _result(
        medium="unknown",
        spatial_context="not_applicable",
        framing_scale="not_applicable",
        camera_angle="not_applicable",
        drawing_kind="not_applicable",
        project_state="not_applicable",
        uncertain_axes=["medium"],
    )
    validate_invariants(row)
    with pytest.raises(ValueError, match="unknown medium must be uncertain"):
        validate_invariants({**row, "uncertain_axes": []})


def test_not_applicable_cannot_be_uncertain_and_resolution_needs_uncertainty() -> None:
    with pytest.raises(ValueError, match="not_applicable drawing_kind cannot be uncertain"):
        validate_invariants(_result(uncertain_axes=["drawing_kind"]))
    with pytest.raises(ValueError, match="resolution_insufficient requires uncertainty"):
        validate_invariants(_result(resolution_insufficient=True))
    validate_invariants(
        _result(
            framing_scale="unknown",
            uncertain_axes=["framing_scale"],
            resolution_insufficient=True,
        )
    )


def test_aerial_angle_requires_exterior_or_unknown_spatial_context() -> None:
    validate_invariants(_result(camera_angle="aerial_oblique"))
    validate_invariants(
        _result(
            camera_angle="aerial_top_down",
            spatial_context="unknown",
            uncertain_axes=["spatial_context"],
        )
    )
    with pytest.raises(ValueError, match="aerial angles require exterior or unknown"):
        validate_invariants(_result(camera_angle="aerial_oblique", spatial_context="interior"))


def test_batch_requires_exact_count_order_case_and_unique_expected_ids() -> None:
    rows = [_result(asset_id="asset-A"), _result(asset_id="asset-B")]
    normalized = normalize_batch(rows, ("asset-A", "asset-B"))
    assert [row["asset_id"] for row in normalized] == ["asset-A", "asset-B"]

    with pytest.raises(ValueError, match="does not match attachment order"):
        normalize_batch(rows, ("asset-B", "asset-A"))
    with pytest.raises(ValueError, match="does not match attachment order"):
        normalize_batch(rows, ("asset-a", "asset-B"))
    with pytest.raises(ValueError, match="count does not match"):
        normalize_batch(rows[:1], ("asset-A", "asset-B"))
    with pytest.raises(ValueError, match="must be unique"):
        normalize_batch(rows, ("asset-A", "asset-A"))


def test_result_rejects_missing_or_metadata_extra_fields() -> None:
    missing = _result()
    del missing["project_legibility"]
    with pytest.raises(ValueError, match="fields mismatch"):
        normalize_result(missing, "asset-1")
    with pytest.raises(ValueError, match="fields mismatch"):
        normalize_result({**_result(), "source": "architizer"}, "asset-1")


@pytest.mark.parametrize(
    ("row", "tier"),
    [
        (_out_of_scope(), "rejected"),
        (_result(project_state="construction_visible"), "archive_only"),
        (_result(uncertain_axes=["scope"]), "qa_only"),
        (_result(project_legibility="low"), "qa_only"),
        (
            _result(
                framing_scale="unknown",
                uncertain_axes=["framing_scale"],
                resolution_insufficient=True,
            ),
            "qa_only",
        ),
        (_result(), "preferred"),
        (_result(framing_scale="site_context"), "preferred"),
        (_result(framing_scale="element_detail"), "eligible"),
        (_drawing(), "fallback"),
        (_model(), "fallback"),
    ],
)
def test_hero_tiers_are_deterministic_python_derivations(
    row: dict[str, object], tier: str
) -> None:
    actual, reasons = derive_hero_decision(row)
    assert actual == tier
    assert reasons


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (_out_of_scope(), ()),
        (_result(), ("exterior_overall",)),
        (
            _result(framing_scale="site_context", camera_angle="aerial_oblique"),
            ("aerial_context", "exterior_context"),
        ),
        (_result(spatial_context="interior"), ("interior",)),
        (
            _result(spatial_context="threshold", framing_scale="material_detail"),
            ("detail", "interior"),
        ),
        (_drawing("plan"), ("drawing_plan",)),
        (_drawing("section"), ("drawing_section",)),
        (_drawing("elevation"), ("drawing_other",)),
        (_rendering(), ("drawing_other", "exterior_overall", "model_or_render")),
        (_model(), ("model_or_render",)),
        (
            _result(project_state="construction_visible"),
            ("construction_or_archive", "exterior_overall"),
        ),
    ],
)
def test_coverage_slots_are_orthogonal_sorted_python_derivations(
    row: dict[str, object], expected: tuple[str, ...]
) -> None:
    assert derive_coverage_slots(row) == expected
