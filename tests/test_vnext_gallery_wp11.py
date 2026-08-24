from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

from canonical.vnext_gallery_drift_overlay import (
    SCHEMA_SQL,
    _recompute,
    normalize_pixel_status,
)


def _member(
    asset: str,
    pixel: str,
    *,
    rank: int,
    status: str = "exact",
    pair: str | None = None,
    class_rep: str | None = None,
) -> dict[str, object]:
    actual = pixel if status == "exact" else "f" * 64
    return {
        "source": "architizer", "source_building_id": "b1", "source_asset_id": asset,
        "candidate_id": "c-" + asset, "group_id": "g-base", "gallery_rank": rank,
        "group_rank": 1, "base_representative_asset_id": "center",
        "normalized_pixel_sha256": pixel, "class_representative_source_asset_id": class_rep or asset,
        "direct_pair_id": pair, "record_sha256": (asset[0] * 64),
        "v1_pixel_status": status, "status": status,
        "expected_pixel_sha256": pixel, "actual_pixel_sha256": actual,
    }


def _edge(pair: str, member: str) -> dict[str, object]:
    return {
        "pair_id": pair, "group_id": "g-base", "representative_source_asset_id": "center",
        "member_class_representative_asset_id": member, "edge_id": "edge-" + pair,
        "hamming_distance": 4, "edge_scope": "global_le8", "edge_record_sha256": "e" * 64,
        "record_sha256": (pair[-1] * 64), "transitive_closure_allowed": 0,
    }


def test_schema_is_valid_delete_journal_sqlite() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA_SQL)
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"overlay_runs", "asset_overlay", "effective_groups", "edge_overlay", "blind_review_items"} <= tables


def test_status_requires_matching_actual_pixel() -> None:
    assert normalize_pixel_status({"status": "exact", "expected_pixel_sha256": "a", "actual_pixel_sha256": "a"}) == "exact_match"
    assert normalize_pixel_status({"status": "delivery_changed", "expected_pixel_sha256": "a", "actual_pixel_sha256": "a"}) == "exact_match"
    assert normalize_pixel_status({"status": "source_changed", "expected_pixel_sha256": "a", "actual_pixel_sha256": "b"}) == "pixel_unverified"
    assert normalize_pixel_status({"status": "failed", "expected_pixel_sha256": "a", "actual_pixel_sha256": None}) == "pixel_unverified"


def test_recompute_keeps_exact_class_and_splits_only_unverified_edge() -> None:
    p_center, p_same, p_drift = "1" * 64, "2" * 64, "3" * 64
    members = [
        _member("center", p_center, rank=1, class_rep="center"),
        _member("same", p_same, rank=2, pair="pair-a", class_rep="same"),
        _member("drift", p_drift, rank=3, status="source_changed", pair="pair-b", class_rep="drift"),
        _member("center-copy", p_center, rank=4, status="source_changed", class_rep="center"),
    ]
    groups, effective_members, assets, edges = _recompute(members, [_edge("pair-a", "same"), _edge("pair-b", "drift")])
    assert len(groups) == 2
    main = next(row for row in groups if row["effective_group_id"] == "g-base")
    split = next(row for row in groups if row["effective_group_id"] != "g-base")
    assert (main["group_status"], main["exact_class_count"], main["direct_class_count"]) == ("serveable", 2, 1)
    assert (split["group_status"], split["exact_class_count"], split["pixel_unverified_count"]) == ("unresolved", 1, 1)
    by_pair = {row["pair_id"]: row for row in edges}
    assert by_pair["pair-a"]["disposition"] == "admissible"
    assert by_pair["pair-a"]["center_effective_group_id"] == by_pair["pair-a"]["member_effective_group_id"]
    assert by_pair["pair-b"]["disposition"] == "split"
    assert by_pair["pair-b"]["reason_code"] == "unverifiable_edge_source_changed"
    assert by_pair["pair-b"]["center_effective_group_id"] != by_pair["pair-b"]["member_effective_group_id"]
    by_asset = {row["source_asset_id"]: row for row in assets}
    assert by_asset["center-copy"]["effective_group_id"] == "g-base"
    assert by_asset["center-copy"]["pixel_unverified"] == 1
    assert len(effective_members) == len(members)


def test_overlay_module_has_no_network_client_import() -> None:
    path = Path(__file__).resolve().parents[1] / "canonical" / "vnext_gallery_drift_overlay.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = {"requests", "httpx", "urllib.request", "canonical.cross_source_semantic_fetch"}
    assert not imported & forbidden
