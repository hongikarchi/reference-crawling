import json
import sqlite3
from pathlib import Path

from tools import e1_phash_dedup
from tools.image_dedup_5type import SourceImageSpec


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


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
        (1, "https://img.test/d1.jpg", json.dumps(["https://img.test/d1_same.jpg"])),
    )
    conn.execute(
        "INSERT INTO divisare_projects VALUES (?, ?, ?)",
        (3, "https://img.test/d3.jpg", json.dumps([])),
    )
    conn.execute(
        "INSERT INTO archello_projects VALUES (?, ?, ?)",
        (2, "https://img.test/a2.jpg", json.dumps([])),
    )
    conn.commit()
    conn.close()


def test_e1_cross_source_only_clustering_and_jsonl_output(tmp_path):
    canonical_path = tmp_path / "canonical.json"
    phash_cache_path = tmp_path / "phash_cache.json"
    output_path = tmp_path / "e1.jsonl"
    db_path = tmp_path / "sources.db"

    _write_json(
        canonical_path,
        {
            "clusters": [
                {"canonical_bld_id": "bld_same_source", "source_refs": {"divisare": ["1"]}},
                {
                    "canonical_bld_id": "bld_cross_source",
                    "source_refs": {"divisare": ["3"], "archello": ["2"]},
                },
                {"canonical_bld_id": "bld_empty", "source_refs": {"archello": ["404"]}},
            ]
        },
    )
    _write_json(
        phash_cache_path,
        {
            "divisare:1": ["0000000000000000", "0000000000000000"],
            "divisare:3": ["1111111111111111"],
            "archello:2": ["1111111111111111"],
        },
    )
    _make_db(db_path)

    specs = {
        "divisare": SourceImageSpec("divisare", db_path, "divisare_projects", "id", "cover_image_url", "gallery_urls"),
        "archello": SourceImageSpec("archello", db_path, "archello_projects", "id", "cover_image_url", "gallery_image_urls"),
    }

    summary = e1_phash_dedup.run_all(
        canonical_path=canonical_path,
        output_path=output_path,
        phash_cache_path=phash_cache_path,
        workers=2,
        source_specs=specs,
    )

    assert summary["rows_processed"] == 3
    rows = {
        row["cid"]: row
        for row in (json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines())
    }

    same = rows["bld_same_source"]
    assert len(same["all_images"]) == 2
    assert len(same["best_image_per_cluster"]) == 2

    cross = rows["bld_cross_source"]
    assert len(cross["all_images"]) == 2
    assert len(cross["best_image_per_cluster"]) == 1
    assert {image["phash_cluster_id"] for image in cross["all_images"]} == {0}

    assert rows["bld_empty"]["all_images"] == []
    assert rows["bld_empty"]["best_image_per_cluster"] == {}
