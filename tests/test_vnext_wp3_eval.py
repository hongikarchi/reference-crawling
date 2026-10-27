"""WP3 N10 eval tool: synthetic fixtures only (no images, no network, no model)."""

from __future__ import annotations

from tools import vnext_wp3_eval as ev

BASE = {
    "in_scope": True, "reject_reason": "none", "medium": "photograph",
    "spatial_context": "exterior", "framing_scale": "overall", "camera_angle": "eye_level",
    "drawing_kind": "not_applicable", "project_state": "visibly_finished", "project_legibility": "high",
}


def _scores(m):
    return {k: {"margin": m} for k in ("scope", "medium", "spatial_context", "framing_scale",
                                         "camera_angle", "project_state", "project_legibility")}


def _local(idx, bld, margin=0.9, **over):
    return {"idx": idx, "canonical_bld_id": bld, "stratum": "divisare",
            "labels": {**BASE, **over}, "scores": _scores(margin)}


def _gold(idx, bld, acceptable=None, **over):
    return {"idx": idx, "canonical_bld_id": bld, **BASE, **over, "acceptable": acceptable or {}}


def test_acceptable_alternative_counts_and_bool_normalised():
    g = _gold(0, "b", acceptable={"project_legibility": ["high", "medium"]})
    assert ev.is_accepted({**BASE, "project_legibility": "medium"}, g, "project_legibility")
    assert not ev.is_accepted({**BASE, "project_legibility": "low"}, g, "project_legibility")
    assert ev.is_accepted({**BASE}, g, "in_scope")
    assert not ev.is_accepted({**BASE, "in_scope": False}, g, "in_scope")


def test_routing_curve_finds_minimal_fraction():
    # 10 wrong low-confidence + 20 correct high-confidence items
    items = [(0.01 * i, False, i) for i in range(10)] + [(0.5 + 0.01 * i, True, 100 + i) for i in range(20)]
    r = ev.routing_curve(items, target=0.99, min_keep=5)
    assert r["reached"] and r["routed_n"] == 10 and r["auto_accuracy"] == 1.0
    assert r["auroc_margin_vs_correct"] == 1.0
    # uninformative margin: never reaches target before min_keep
    items2 = [(0.5, i % 2 == 0, i) for i in range(30)]
    assert not ev.routing_curve(items2, target=0.95, min_keep=5)["reached"]


def test_evaluate_gates_and_cover_lane():
    local = [_local(0, "b1"), _local(1, "b1", medium="rendering", margin=0.1), _local(2, "b2")]
    gold = {
        "labels": [_gold(0, "b1"), _gold(1, "b1"), _gold(2, "b2")],
        "covers": [
            {"canonical_bld_id": "b1", "acceptable_default_idx": [0]},
            {"canonical_bld_id": "b2", "acceptable_default_idx": []},
        ],
    }
    run = {"buildings": {
        "b1": {"default_cover_idx": 0, "top3_idx": [0, 1]},
        "b2": {"default_cover_idx": 2, "top3_idx": [2]},  # gold says NULL -> miss
        "b3": {"default_cover_idx": None, "top3_idx": []},  # no gold
    }}
    out = ev.evaluate(local, gold, run)
    la = out["lane_a"]
    assert la["medium_obvious_errors"] == 1 and la["scope_obvious_errors"] == 0
    assert la["pooled_accepted"] == 26 and not la["pass"]
    lb = out["lane_b"]["gate"]
    assert lb["critical_cover_errors"] == 0
    assert lb["top1_in_acceptable_of_scored"] == "1/2" and not lb["pass"]
    rows = {r["canonical_bld_id"]: r for r in out["lane_b"]["buildings"]}
    assert rows["b2"]["miss_kind"] == "non_null_when_gold_null"
    assert rows["b3"]["gold"] is False


def test_critical_cover_reasons():
    assert ev.critical_cover_reasons(_gold(0, "b")) == []
    assert "interior" in ev.critical_cover_reasons(_gold(0, "b", spatial_context="interior"))
    assert "medium=drawing" in ev.critical_cover_reasons(_gold(0, "b", medium="drawing"))
