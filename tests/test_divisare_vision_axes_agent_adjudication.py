from __future__ import annotations

from canonical import divisare_vision_axes_agent_adjudication as adjudication


def _record() -> dict:
    return {
        "asset_id": "axis-123456789abc",
        "in_scope": True,
        "reject_reason": "none",
        "medium": "photograph",
        "spatial_context": "exterior",
        "framing_scale": "overall",
        "camera_angle": "eye_level",
        "drawing_kind": "not_applicable",
        "project_state": "visibly_finished",
        "uncertain_axes": ["framing_scale"],
        "resolution_insufficient": False,
        "evidence": "The whole facade is visible but the crop also emphasizes one bay.",
        "acceptable_labels": {
            "in_scope": [True],
            "reject_reason": ["none"],
            "medium": ["photograph"],
            "spatial_context": ["exterior"],
            "framing_scale": ["overall", "element_detail"],
            "camera_angle": ["eye_level"],
            "drawing_kind": [],
            "project_state": ["visibly_finished"],
        },
    }


def test_adjudication_schema_adds_typed_acceptable_labels() -> None:
    schema = adjudication.adjudication_output_schema()
    item = schema["properties"]["results"]["items"]
    assert "acceptable_labels" in item["required"]
    labels = item["properties"]["acceptable_labels"]
    assert labels["additionalProperties"] is False
    assert labels["properties"]["in_scope"]["items"] == {"type": "boolean"}


def test_decision_rows_preserve_real_boundary_and_not_applicable() -> None:
    rows = adjudication._decision_rows(
        _record(), conflict_fields=["framing_scale", "drawing_kind"]
    )
    by_field = {row["field"]: row for row in rows}
    assert by_field["framing_scale"]["clarity"] == "boundary"
    assert by_field["framing_scale"]["acceptable_labels"] == [
        "overall",
        "element_detail",
    ]
    assert by_field["drawing_kind"]["clarity"] == "not_judgeable"
    assert by_field["drawing_kind"]["primary"] is None
    assert by_field["drawing_kind"]["acceptable_labels"] == []
