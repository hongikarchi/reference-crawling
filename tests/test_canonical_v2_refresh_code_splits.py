import json
import sqlite3
from pathlib import Path

from tools import canonical_v2_refresh_code_splits as refresh
from tools.image_dedup_5type import SourceImageSpec


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE divisare_projects ("
        "id INTEGER PRIMARY KEY, cover_image_url TEXT, gallery_urls TEXT)"
    )
    conn.execute(
        "CREATE TABLE archello_projects ("
        "id INTEGER PRIMARY KEY, cover_image_url TEXT, gallery_image_urls TEXT)"
    )
    conn.execute(
        "INSERT INTO divisare_projects VALUES (?, ?, ?)",
        (1, "https://img.test/d1.jpg", json.dumps([])),
    )
    conn.execute(
        "INSERT INTO archello_projects VALUES (?, ?, ?)",
        (2, "https://img.test/a2.jpg", json.dumps([])),
    )
    conn.commit()
    conn.close()


def test_patch_stage_jsonl_drops_stale_and_appends_replacements(tmp_path):
    input_path = tmp_path / "stage.jsonl"
    output_path = tmp_path / "patched.jsonl"
    _write_jsonl(
        input_path,
        [
            {"cid": "bld_1", "value": "stale"},
            {"cid": "bld_2", "value": "keep"},
        ],
    )

    report = refresh.patch_stage_jsonl(
        input_path,
        output_path,
        affected_cids={"bld_1", "bld_3"},
        replacement_rows=[
            {"cid": "bld_1", "value": "fresh"},
            {"cid": "bld_3", "value": "new"},
        ],
    )

    assert report == {
        "input_rows": 2,
        "dropped_stale_rows": 1,
        "replacement_rows": 2,
        "output_rows": 3,
    }
    rows = _read_jsonl(output_path)
    assert [row["cid"] for row in rows] == ["bld_2", "bld_1", "bld_3"]
    assert rows[1]["value"] == "fresh"


def test_prepare_refresh_writes_subset_and_patched_outputs(tmp_path):
    split_report = tmp_path / "split_report.json"
    canonical = tmp_path / "canonical.json"
    output_dir = tmp_path / "refresh"
    phash_cache = tmp_path / "phash_cache.json"
    db_path = tmp_path / "sources.db"
    d1_path = tmp_path / "d1.jsonl"
    e1_path = tmp_path / "e1.jsonl"
    e2_path = tmp_path / "e2.jsonl"
    d2_path = tmp_path / "d2.jsonl"

    _write_json(
        split_report,
        {"touched_existing": ["bld_1"], "created": ["bld_3"]},
    )
    _write_json(
        canonical,
        {
            "clusters": [
                {"canonical_bld_id": "bld_1", "source_refs": {"divisare": ["1"]}},
                {"canonical_bld_id": "bld_2", "source_refs": {"divisare": ["9"]}},
                {"canonical_bld_id": "bld_3", "source_refs": {"archello": ["2"]}},
            ]
        },
    )
    _write_json(phash_cache, {"divisare:1": ["0000000000000000"], "archello:2": ["1111111111111111"]})
    _make_db(db_path)

    for path in (d1_path, e2_path, d2_path):
        _write_jsonl(path, [{"cid": "bld_1"}, {"cid": "bld_2"}])
    _write_jsonl(e1_path, [{"cid": "bld_1", "all_images": ["stale"]}, {"cid": "bld_2", "all_images": []}])

    specs = {
        "divisare": SourceImageSpec("divisare", db_path, "divisare_projects", "id", "cover_image_url", "gallery_urls"),
        "archello": SourceImageSpec("archello", db_path, "archello_projects", "id", "cover_image_url", "gallery_image_urls"),
    }

    report = refresh.prepare_refresh(
        split_report_path=split_report,
        canonical_path=canonical,
        output_dir=output_dir,
        stage_paths={"d1": d1_path, "e1": e1_path, "e2": e2_path, "d2": d2_path},
        phash_cache_path=phash_cache,
        source_specs=specs,
    )

    assert report["affected_count"] == 2
    assert report["e1_recomputed_rows"] == 2
    assert report["patch_reports"]["d1"]["output_rows"] == 1
    assert report["patch_reports"]["e1"]["output_rows"] == 3

    subset = json.loads((output_dir / "canonical_subset.json").read_text(encoding="utf-8"))
    assert [row["canonical_bld_id"] for row in subset["clusters"]] == ["bld_1", "bld_3"]

    e1_rows = {row["cid"]: row for row in _read_jsonl(output_dir / "e1_clusters.patched.jsonl")}
    assert sorted(e1_rows) == ["bld_1", "bld_2", "bld_3"]
    assert e1_rows["bld_1"]["all_images"][0]["url"] == "https://img.test/d1.jpg"
    assert e1_rows["bld_3"]["all_images"][0]["url"] == "https://img.test/a2.jpg"

    d1_rows = _read_jsonl(output_dir / "d1_results.patched.jsonl")
    assert [row["cid"] for row in d1_rows] == ["bld_2"]
