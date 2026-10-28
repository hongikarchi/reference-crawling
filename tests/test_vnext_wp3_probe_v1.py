"""Unit tests for tools/vnext_wp3_probe_v1.py cascade (no sklearn / no data files needed)."""
from tools import vnext_s3_codex_engine as ce
from tools import vnext_wp3_probe_v1 as pr


def _P(**over):
    P = {"scope": {"true": 0.9, "false": 0.1}, "reject_reason": {"people_or_event": 1.0},
         "medium": {"photograph": 0.8, "rendering": 0.1, "drawing": 0.1},
         "spatial_context": {"exterior": 0.7, "interior": 0.3},
         "framing_scale": {"overall": 0.8, "element_detail": 0.2},
         "camera_angle": {"eye_level": 0.5, "elevated": 0.3, "aerial_oblique": 0.2},
         "drawing_kind": {"plan": 0.6, "section": 0.4},
         "project_state": {"visibly_finished": 0.9, "construction_visible": 0.1},
         "project_legibility": {"high": 0.7, "low": 0.3},
         "oos9": {"in_scope": 0.2, "people_or_event": 0.8}}
    P.update(over)
    return P


def _valid(lab):
    return ce.validate_row({"idx": 1, **lab}) == []


def test_cascade_outputs_satisfy_contract_applicability():
    cases = [_P(), _P(medium={"drawing": 0.9, "photograph": 0.1}), _P(medium={"rendering": 0.9, "photograph": 0.1}),
             _P(medium={"physical_model": 0.9, "photograph": 0.1}), _P(scope={"true": 0.2, "false": 0.8}),
             _P(spatial_context={"interior": 0.9, "exterior": 0.1},
                camera_angle={"aerial_oblique": 0.9, "eye_level": 0.06, "elevated": 0.04})]
    for P in cases:
        for mode in ("scope", "oos9"):
            for t in (0.0, 0.5):
                lab, sc, cov = pr.cascade(P, t, mode)
                assert _valid(lab), (lab, ce.validate_row({"idx": 1, **lab}))


def test_camera_abstention_and_tier():
    lab, _, _ = pr.cascade(_P(), 0.5, "scope")  # eye_level margin 0.2 < 0.5 -> unknown
    assert lab["camera_angle"] == "unknown"
    assert pr.tier(lab) == 1
    lab, _, _ = pr.cascade(_P(), 0.0, "scope")
    assert lab["camera_angle"] == "eye_level"
    lab, _, _ = pr.cascade(_P(spatial_context={"interior": 0.9, "exterior": 0.1},
                              camera_angle={"aerial_oblique": 0.9, "eye_level": 0.06, "elevated": 0.04}), 0.0, "scope")
    assert lab["camera_angle"] in ("eye_level", "elevated")  # aerial only with exterior
    assert pr.cam_accept_vs_codex("unknown", "elevated") and not pr.cam_accept_vs_codex("unknown", "aerial_oblique")


def test_head_targets_follow_applicability():
    photo = {"in_scope": True, "reject_reason": "none", "medium": "photograph", "spatial_context": "exterior",
             "framing_scale": "overall", "camera_angle": "unknown", "drawing_kind": "not_applicable",
             "project_state": "visibly_finished", "project_legibility": "high"}
    assert pr.head_target(photo, "camera_angle") is None  # unknown is not a training target
    assert pr.head_target(photo, "drawing_kind") is None
    assert pr.head_target(photo, "oos9") == "in_scope"
    oos = dict(photo, in_scope=False, reject_reason="people_or_event")
    assert pr.head_target(oos, "reject_reason") == "people_or_event"
    assert pr.head_target(oos, "spatial_context") is None
