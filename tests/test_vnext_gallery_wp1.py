from __future__ import annotations

import io
import sqlite3

from PIL import Image, ImageDraw

from canonical.vnext_gallery_draft import (
    SCHEMA_SQL,
    group_gallery_assets,
    representative_sort_key,
)
from canonical.vnext_gallery_verified_pixels import (
    compare_local_features,
    extract_local_features,
)


def _asset(
    asset_id: str,
    pixel: str,
    node: str,
    *,
    width: int = 1024,
    height: int = 768,
    role: str = "gallery",
    ordinal: int = 1,
) -> dict[str, object]:
    return {
        "source": "divisare",
        "source_building_id": "b1",
        "source_asset_id": asset_id,
        "candidate_id": "candidate-" + asset_id,
        "disposition": "gallery",
        "normalized_pixel_sha256": pixel,
        "node_id": node,
        "fingerprint_status": "success",
        "original_width": width,
        "original_height": height,
        "low_information": 0,
        "primary_role": role,
        "source_ordinal": ordinal,
    }


def _e2() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE phash_edges(edge_id TEXT,left_node_id TEXT,right_node_id TEXT,"
        "hamming_distance INTEGER,edge_scope TEXT,edge_record_sha256 TEXT)"
    )
    return connection


def test_schema_is_valid_delete_journal_sqlite() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA_SQL)
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"runs", "input_assets", "gallery_groups", "pixel_verifications"} <= tables


def test_exact_classes_then_direct_star_never_transitive() -> None:
    connection = _e2()
    connection.executemany(
        "INSERT INTO phash_edges VALUES(?,?,?,?,?,?)",
        [
            ("edge-ab", "n-a", "n-b", 3, "global_le8", "a" * 64),
            ("edge-bc", "n-b", "n-c", 4, "global_le8", "b" * 64),
        ],
    )
    # a-hi wins its exact class by usable resolution even though a-low is cover.
    assets = [
        _asset("a-low", "1" * 64, "n-a", width=500, height=400, role="cover", ordinal=0),
        _asset("a-hi", "1" * 64, "n-a", width=1200, height=900, ordinal=3),
        _asset("b", "2" * 64, "n-b", width=1000, height=800, ordinal=4),
        _asset("c", "3" * 64, "n-c", width=900, height=700, ordinal=5),
    ]
    groups, members, edges = group_gallery_assets(connection, assets)
    by_asset = {row["source_asset_id"]: row["group_id"] for row in members}
    assert len(groups) == 2
    assert by_asset["a-hi"] == by_asset["a-low"] == by_asset["b"]
    assert by_asset["c"] != by_asset["b"]
    assert groups[0]["representative_source_asset_id"] == "a-hi"
    assert len(edges) == 1
    assert edges[0]["edge_id"] == "edge-ab"
    assert edges[0]["transitive_closure_allowed"] == 0


def test_identical_phash_node_without_saved_edge_does_not_group() -> None:
    connection = _e2()
    assets = [
        _asset("a", "1" * 64, "same-node", ordinal=0),
        _asset("b", "2" * 64, "same-node", ordinal=1),
    ]
    groups, members, edges = group_gallery_assets(connection, assets)
    assert len(groups) == 2
    assert len({row["group_id"] for row in members}) == 2
    assert edges == []


def test_representative_order_puts_resolution_before_editorial_role() -> None:
    low_cover = _asset("cover", "1" * 64, "n1", width=500, height=400, role="cover", ordinal=0)
    usable_gallery = _asset("gallery", "2" * 64, "n2", width=1024, height=768, role="gallery", ordinal=10)
    assert representative_sort_key(usable_gallery) < representative_sort_key(low_cover)


def test_local_feature_exact_pixel_smoke_is_deterministic() -> None:
    image = Image.new("RGB", (512, 384), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 60, 470, 330), outline="black", width=8)
    draw.line((40, 330, 260, 80, 470, 330), fill="navy", width=7)
    draw.rectangle((180, 180, 330, 330), fill="gray", outline="black", width=4)
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=92, subsampling=0)
    raw = output.getvalue()
    left = extract_local_features(raw)
    right = extract_local_features(raw)
    assert left == right
    comparison = compare_local_features(left, right, same_pixel=True)
    assert comparison["matched_keypoints"] >= 3
    assert comparison["outcome"] == "same_pixel_match"
