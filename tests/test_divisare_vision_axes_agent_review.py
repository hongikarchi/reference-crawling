from __future__ import annotations

from canonical import divisare_vision_axes_agent_review as agent_review


def test_annotation_clarity_is_derived_from_applicability_and_uncertainty() -> None:
    row = agent_review._annotation_from_result(
        {
            "asset_id": "axis-123456789abc",
            "in_scope": True,
            "reject_reason": "none",
            "medium": "photograph",
            "spatial_context": "interior",
            "framing_scale": "overall",
            "camera_angle": "eye_level",
            "drawing_kind": "not_applicable",
            "project_state": "visibly_finished",
            "uncertain_axes": ["framing_scale"],
            "resolution_insufficient": False,
            "evidence": "A complete room is visible from an ordinary viewpoint.",
        }
    )
    assert row["clarity"]["in_scope"] == "clear"
    assert row["clarity"]["framing_scale"] == "boundary"
    assert row["clarity"]["drawing_kind"] == "not_judgeable"
    assert row["uncertain_axes"] == ["framing_scale"]


def test_scope_uncertainty_marks_both_scope_decisions_boundary() -> None:
    row = agent_review._annotation_from_result(
        {
            "asset_id": "axis-fedcba987654",
            "in_scope": False,
            "reject_reason": "people_or_event",
            "medium": "photograph",
            "spatial_context": "not_applicable",
            "framing_scale": "not_applicable",
            "camera_angle": "not_applicable",
            "drawing_kind": "not_applicable",
            "project_state": "not_applicable",
            "uncertain_axes": ["scope"],
            "resolution_insufficient": False,
            "evidence": "A posed person dominates while a designed room remains visible.",
        }
    )
    assert row["clarity"]["in_scope"] == "boundary"
    assert row["clarity"]["reject_reason"] == "boundary"
    assert row["clarity"]["medium"] == "clear"
    assert all(
        row["clarity"][field] == "not_judgeable"
        for field in agent_review.AXIS_FIELDS[1:]
    )
