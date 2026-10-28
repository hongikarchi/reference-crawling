"""WP3 v1.4 re-score mapping: synthetic fixtures only (no images, no network, no model)."""

from __future__ import annotations

from tools import vnext_wp3_eval as ev
from tools import vnext_wp3_eval_v14 as v14

BASE = {
    "in_scope": True, "reject_reason": "none", "medium": "photograph",
    "spatial_context": "exterior", "framing_scale": "overall", "camera_angle": "eye_level",
    "drawing_kind": "not_applicable", "project_state": "visibly_finished", "project_legibility": "high",
}


def _gold(idx, acceptable=None, **over):
    return {"idx": idx, "canonical_bld_id": "b", **BASE, **over, "acceptable": acceptable or {}}


def test_legibility_medium_maps_to_high_both_sides():
    gold = {"labels": [_gold(0, project_legibility="medium"),
                       _gold(1, acceptable={"project_legibility": ["low", "medium"]}, project_legibility="low")]}
    g14, st = v14.map_gold_v14(gold)
    assert g14["labels"][0]["project_legibility"] == "high"
    assert g14["labels"][1]["acceptable"]["project_legibility"] == ["low", "high"]
    assert st["legibility_primary_medium_to_high"] == 1
    assert st["legibility_rows_high_and_low_both_accepted"] == 1
    assert gold["labels"][0]["project_legibility"] == "medium"  # input not mutated
    recs, _ = v14.map_preds_v14([{"idx": 0, "labels": {**BASE, "project_legibility": "medium"}}])
    assert ev.is_accepted(recs[0]["labels"], g14["labels"][0], "project_legibility")


def test_camera_unknown_only_for_eye_level_or_elevated_gold():
    gold = {"labels": [_gold(0), _gold(1, camera_angle="elevated"),
                       _gold(2, camera_angle="aerial_oblique"), _gold(3, camera_angle="not_applicable")]}
    g14, st = v14.map_gold_v14(gold)
    unk = {**BASE, "camera_angle": "unknown"}
    got = [ev.is_accepted(unk, g, "camera_angle") for g in g14["labels"]]
    assert got == [True, True, False, False]
    assert st["camera_unknown_added"] == 2
    # eye_level vs elevated confusion is NOT relaxed
    assert not ev.is_accepted({**BASE, "camera_angle": "elevated"}, g14["labels"][0], "camera_angle")
