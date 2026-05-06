import json
import sqlite3
from pathlib import Path

from tools import image_dedup_5type


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _make_source_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE divisare_projects ("
        "id INTEGER PRIMARY KEY, "
        "cover_image_url TEXT, "
        "gallery_urls TEXT"
        ")"
    )
    conn.execute(
        "CREATE TABLE archello_projects ("
        "id INTEGER PRIMARY KEY, "
        "cover_image_url TEXT, "
        "gallery_image_urls TEXT"
        ")"
    )
    conn.execute(
        "INSERT INTO divisare_projects "
        "(id, cover_image_url, gallery_urls) VALUES (?, ?, ?)",
        (
            1,
            "https://example.test/divisare/cover.jpg",
            json.dumps(["https://example.test/divisare/floor-plan.jpg"]),
        ),
    )
    conn.execute(
        "INSERT INTO archello_projects "
        "(id, cover_image_url, gallery_image_urls) VALUES (?, ?, ?)",
        (
            2,
            "https://example.test/archello/cover.jpg",
            json.dumps(["https://example.test/archello/detail.jpg"]),
        ),
    )
    conn.commit()
    conn.close()


def test_run_all_writes_jsonl_with_cached_phashes_and_5type_covers(tmp_path):
    canonical_path = tmp_path / "canonical.json"
    output_path = tmp_path / "e_image_results.jsonl"
    phash_cache_path = tmp_path / "phash_cache.json"
    db_path = tmp_path / "sources.db"

    _write_json(
        canonical_path,
        {
            "clusters": [
                {
                    "canonical_bld_id": "bld_test",
                    "source_refs": {
                        "divisare": ["1"],
                        "archello": ["2"],
                    },
                }
            ]
        },
    )
    _write_json(
        phash_cache_path,
        {
            "divisare:1": [
                "0000000000000000",
                "1111111111111111",
            ],
        },
    )
    _make_source_db(db_path)

    source_specs = {
        "divisare": image_dedup_5type.SourceImageSpec(
            source="divisare",
            db_path=db_path,
            table="divisare_projects",
            id_col="id",
            cover_col="cover_image_url",
            gallery_col="gallery_urls",
        ),
        "archello": image_dedup_5type.SourceImageSpec(
            source="archello",
            db_path=db_path,
            table="archello_projects",
            id_col="id",
            cover_col="cover_image_url",
            gallery_col="gallery_image_urls",
        ),
    }
    fetched = []

    def fake_fetcher(url):
        fetched.append(url)
        if url.endswith("cover.jpg"):
            return {"phash": "0000000000000000", "w": 1200, "h": 900, "bytes": 500000}
        return {"phash": "ffffffffffffffff", "w": 800, "h": 600, "bytes": 100000}

    def fake_classifier(url):
        if "detail" in url:
            return "detail"
        return "interior"

    summary = image_dedup_5type.run_all(
        canonical_path=canonical_path,
        output_path=output_path,
        phash_cache_path=phash_cache_path,
        workers=2,
        source_specs=source_specs,
        fetcher=fake_fetcher,
        classifier=fake_classifier,
    )

    assert summary["rows_processed"] == 1
    assert summary["images_written"] == 4
    assert fetched == [
        "https://example.test/archello/cover.jpg",
        "https://example.test/archello/detail.jpg",
    ]

    rows = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    rec = rows[0]
    assert rec["cid"] == "bld_test"
    assert len(rec["all_images"]) == 4
    assert rec["covers_by_type"]["drawing"] == "https://example.test/divisare/floor-plan.jpg"
    assert rec["covers_by_type"]["detail"] == "https://example.test/archello/detail.jpg"

    by_url = {image["url"]: image for image in rec["all_images"]}
    assert by_url["https://example.test/divisare/cover.jpg"]["phash"] == "0000000000000000"
    assert by_url["https://example.test/divisare/floor-plan.jpg"]["type"] == "drawing"
    assert by_url["https://example.test/archello/cover.jpg"]["phash_cluster_id"] == by_url[
        "https://example.test/divisare/cover.jpg"
    ]["phash_cluster_id"]
