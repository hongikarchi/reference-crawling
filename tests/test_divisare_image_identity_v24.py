import sqlite3
from pathlib import Path

import pytest

from tools.build_divisare_image_identity_v24 import (
    ASSET_KEY_VERSION,
    METADATA_VERSION,
    build_artifact,
    derive_v24_identity,
    file_sha256,
)


PUBLIC_ID = "a" * 40
OLD_MODERN_KEY = "divisare|" + PUBLIC_ID
V100_KEY = OLD_MODERN_KEY + "|v100"
V101_KEY = OLD_MODERN_KEY + "|v101"
LEGACY_KEY = "divisare|42|plan-main"

MODERN_COVER = (
    "https://images.divisare.com/image/upload/c_fit,f_jpg/v100/"
    + PUBLIC_ID
    + ".jpg"
)
MODERN_GALLERY_V100 = (
    "https://images.divisare.com/images/f_auto/v100/"
    + PUBLIC_ID
    + "/project.jpg"
)
MODERN_GALLERY_V101 = (
    "https://images.divisare.com/images/f_auto/v101/"
    + PUBLIC_ID
    + "/project.jpg"
)
LEGACY_COVER = (
    "https://images.divisare.com/image/upload/c_fit/v1/"
    "project_images/42/plan-main.jpg"
)
LEGACY_GALLERY = (
    "https://images.divisare.com/images/f_auto/v1/"
    "project_images/42/plan-main/project.jpg"
)


FIXTURE_SCHEMA = """
PRAGMA foreign_keys=ON;
PRAGMA user_version=6;

CREATE TABLE metadata_review_lineage_v2_3 (
    lineage_id INTEGER PRIMARY KEY,
    metadata_version TEXT NOT NULL
);
CREATE TABLE metadata_review_validation_v2_3 (
    check_name TEXT PRIMARY KEY,
    passed INTEGER NOT NULL
);
CREATE TABLE source_articles(article_id INTEGER PRIMARY KEY);
CREATE TABLE buildings(building_id TEXT PRIMARY KEY);
CREATE TABLE build_runs(
    run_id INTEGER PRIMARY KEY,
    asset_key_version TEXT NOT NULL
);

CREATE TABLE image_assets (
    asset_key TEXT PRIMARY KEY,
    provider TEXT NOT NULL DEFAULT 'divisare',
    public_id TEXT,
    original_filename TEXT,
    url_generation TEXT NOT NULL,
    first_seen_article_id INTEGER REFERENCES source_articles(article_id),
    mime_type TEXT,
    width INTEGER,
    height INTEGER,
    byte_size INTEGER,
    content_sha256 TEXT,
    fetch_status TEXT NOT NULL DEFAULT 'pending',
    last_fetch_error TEXT,
    fetched_at TEXT
);
CREATE TABLE image_urls (
    url_id INTEGER PRIMARY KEY,
    asset_key TEXT NOT NULL REFERENCES image_assets(asset_key),
    url TEXT NOT NULL UNIQUE,
    transform_signature TEXT,
    url_generation TEXT NOT NULL,
    UNIQUE(url_id,asset_key)
);
CREATE TABLE source_image_occurrences (
    article_id INTEGER NOT NULL REFERENCES source_articles(article_id),
    role TEXT NOT NULL,
    position INTEGER NOT NULL,
    raw_url TEXT NOT NULL,
    parse_status TEXT NOT NULL,
    parse_error TEXT,
    asset_key TEXT REFERENCES image_assets(asset_key),
    PRIMARY KEY(article_id,role,position)
);
CREATE TABLE article_image_occurrences (
    article_id INTEGER NOT NULL REFERENCES source_articles(article_id),
    role TEXT NOT NULL,
    position INTEGER NOT NULL,
    asset_key TEXT NOT NULL REFERENCES image_assets(asset_key),
    url_id INTEGER NOT NULL REFERENCES image_urls(url_id),
    PRIMARY KEY(article_id,role,position)
);
CREATE TABLE image_url_hints (
    asset_key TEXT NOT NULL REFERENCES image_assets(asset_key),
    url_id INTEGER NOT NULL REFERENCES image_urls(url_id),
    hint TEXT NOT NULL,
    confidence REAL NOT NULL,
    rule_version TEXT NOT NULL,
    PRIMARY KEY(asset_key,url_id,hint,rule_version)
);
CREATE TABLE image_hashes (
    asset_key TEXT NOT NULL REFERENCES image_assets(asset_key),
    algorithm TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    hash_bits INTEGER,
    hash_hex TEXT,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    computed_at TEXT,
    run_id INTEGER REFERENCES build_runs(run_id),
    PRIMARY KEY(asset_key,algorithm,algorithm_version)
);
CREATE TABLE image_hash_bands (
    asset_key TEXT NOT NULL REFERENCES image_assets(asset_key),
    algorithm_version TEXT NOT NULL,
    band_index INTEGER NOT NULL,
    band_value TEXT NOT NULL,
    PRIMARY KEY(asset_key,algorithm_version,band_index)
);
CREATE TABLE image_classifications (
    asset_key TEXT NOT NULL REFERENCES image_assets(asset_key),
    axis TEXT NOT NULL,
    value TEXT NOT NULL,
    model_version TEXT NOT NULL,
    PRIMARY KEY(asset_key,axis,value,model_version)
);
CREATE TABLE image_match_candidates (
    asset_key_a TEXT NOT NULL REFERENCES image_assets(asset_key),
    asset_key_b TEXT NOT NULL REFERENCES image_assets(asset_key),
    algorithm_version TEXT NOT NULL,
    PRIMARY KEY(asset_key_a,asset_key_b,algorithm_version)
);
CREATE TABLE attribute_claims (
    claim_id INTEGER PRIMARY KEY,
    image_asset_key TEXT REFERENCES image_assets(asset_key)
);

CREATE TABLE active_building_membership_v2 (
    article_id INTEGER PRIMARY KEY REFERENCES source_articles(article_id),
    building_id TEXT NOT NULL REFERENCES buildings(building_id)
);
CREATE TABLE active_building_membership_v2_3 (
    article_id INTEGER PRIMARY KEY REFERENCES source_articles(article_id),
    building_id TEXT NOT NULL REFERENCES buildings(building_id)
);
CREATE TABLE building_images_materialized_v2 (
    building_id TEXT NOT NULL REFERENCES buildings(building_id),
    asset_key TEXT NOT NULL REFERENCES image_assets(asset_key),
    representative_url TEXT NOT NULL,
    role_rank INTEGER NOT NULL,
    first_position INTEGER NOT NULL,
    PRIMARY KEY(building_id,asset_key)
);
CREATE TABLE building_images_materialized_v2_3 (
    building_id TEXT NOT NULL REFERENCES buildings(building_id),
    asset_key TEXT NOT NULL REFERENCES image_assets(asset_key),
    representative_url TEXT NOT NULL,
    role_rank INTEGER NOT NULL,
    first_position INTEGER NOT NULL,
    PRIMARY KEY(building_id,asset_key)
);
CREATE TABLE unrelated_parent_state(
    state_key TEXT PRIMARY KEY,
    state_value TEXT NOT NULL
);
CREATE VIEW v_divisare_buildings_export_v2_3 AS
SELECT building_id FROM buildings;
"""


def make_parent(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(FIXTURE_SCHEMA)
        conn.execute(
            "INSERT INTO metadata_review_lineage_v2_3 VALUES (1,'divisare-metadata-v2.3')"
        )
        conn.execute("INSERT INTO metadata_review_validation_v2_3 VALUES ('all',1)")
        conn.executemany("INSERT INTO source_articles VALUES (?)", [(1,), (2,)])
        conn.executemany("INSERT INTO buildings VALUES (?)", [("b1",), ("b2",)])
        conn.execute(
            "INSERT INTO build_runs VALUES (1,'divisare-asset-key-v1.0')"
        )
        conn.execute(
            """
            INSERT INTO image_assets(
              asset_key,public_id,url_generation,first_seen_article_id
            ) VALUES (?,?,?,?)
            """,
            (OLD_MODERN_KEY, PUBLIC_ID, "cloudinary_public_id", 1),
        )
        conn.execute(
            """
            INSERT INTO image_assets(
              asset_key,public_id,original_filename,url_generation,
              first_seen_article_id
            ) VALUES (?,?,?,?,?)
            """,
            (LEGACY_KEY, "42", "plan-main", "project_images", 2),
        )
        urls = [
            (1, OLD_MODERN_KEY, MODERN_COVER, "c_fit,f_jpg", "cloudinary_public_id"),
            (2, OLD_MODERN_KEY, MODERN_GALLERY_V100, "f_auto", "cloudinary_public_id"),
            (3, OLD_MODERN_KEY, MODERN_GALLERY_V101, "f_auto", "cloudinary_public_id"),
            (4, LEGACY_KEY, LEGACY_COVER, "c_fit", "project_images"),
            (5, LEGACY_KEY, LEGACY_GALLERY, "f_auto", "project_images"),
        ]
        conn.executemany("INSERT INTO image_urls VALUES (?,?,?,?,?)", urls)
        occurrences = [
            (1, "cover", 0, MODERN_COVER, OLD_MODERN_KEY, 1),
            (1, "gallery", 0, MODERN_GALLERY_V100, OLD_MODERN_KEY, 2),
            (1, "gallery", 1, MODERN_GALLERY_V101, OLD_MODERN_KEY, 3),
            (2, "cover", 0, LEGACY_COVER, LEGACY_KEY, 4),
            (2, "gallery", 0, LEGACY_GALLERY, LEGACY_KEY, 5),
        ]
        conn.executemany(
            """
            INSERT INTO source_image_occurrences(
              article_id,role,position,raw_url,parse_status,asset_key
            ) VALUES (?,?,?,?,'parsed',?)
            """,
            [(a, r, p, u, k) for a, r, p, u, k, _ in occurrences],
        )
        conn.executemany(
            "INSERT INTO article_image_occurrences VALUES (?,?,?,?,?)",
            [(a, r, p, k, url_id) for a, r, p, _, k, url_id in occurrences],
        )
        conn.execute(
            "INSERT INTO image_url_hints VALUES (?,?,?,0.7,?)",
            (OLD_MODERN_KEY, 2, "drawing", "fixture-rule"),
        )
        conn.executemany(
            """
            INSERT INTO image_hashes(
              asset_key,algorithm,algorithm_version,status,run_id
            ) VALUES (?,'phash-256','fixture-phash','pending',1)
            """,
            [(OLD_MODERN_KEY,), (LEGACY_KEY,)],
        )
        for table in ("active_building_membership_v2", "active_building_membership_v2_3"):
            conn.executemany(
                "INSERT INTO %s VALUES (?,?)" % table,
                [(1, "b1"), (2, "b2")],
            )
        for table in (
            "building_images_materialized_v2",
            "building_images_materialized_v2_3",
        ):
            conn.executemany(
                "INSERT INTO %s VALUES (?,?,?,?,?)" % table,
                [
                    ("b1", OLD_MODERN_KEY, MODERN_COVER, 0, 0),
                    ("b2", LEGACY_KEY, LEGACY_COVER, 0, 0),
                ],
            )
        conn.execute("INSERT INTO unrelated_parent_state VALUES ('keep','exactly')")
        conn.commit()
    finally:
        conn.close()


def test_identity_policy_splits_delivery_versions_and_keeps_legacy_key():
    cover = derive_v24_identity(MODERN_COVER)
    gallery_same = derive_v24_identity(MODERN_GALLERY_V100)
    gallery_other = derive_v24_identity(MODERN_GALLERY_V101)
    legacy = derive_v24_identity(LEGACY_GALLERY)

    assert cover[0] == V100_KEY
    assert gallery_same[0] == V100_KEY
    assert gallery_other[0] == V101_KEY
    assert gallery_other[2] == "v101"
    assert legacy[0] == LEGACY_KEY
    assert legacy[2] is None


def test_fixture_build_rekeys_dependencies_and_is_byte_deterministic(tmp_path: Path):
    parent = tmp_path / "parent.db"
    make_parent(parent)
    parent_sha = file_sha256(parent)
    results = []
    outputs = []

    for suffix in ("a", "b"):
        output = tmp_path / ("output_%s.db" % suffix)
        report = tmp_path / ("report_%s.md" % suffix)
        result = build_artifact(
            parent_path=parent,
            output_path=output,
            report_path=report,
            production_contract=False,
        )
        results.append(result)
        outputs.append(output)

    assert file_sha256(parent) == parent_sha
    assert results[0]["logical_sha256"] == results[1]["logical_sha256"]
    assert file_sha256(outputs[0]) == file_sha256(outputs[1])
    assert results[0]["metrics"]["old_assets"] == 2
    assert results[0]["metrics"]["new_assets"] == 3
    assert results[0]["metrics"]["asset_delta"] == 1

    conn = sqlite3.connect(outputs[0])
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        keys = {
            row[0] for row in conn.execute("SELECT asset_key FROM image_assets")
        }
        assert keys == {V100_KEY, V101_KEY, LEGACY_KEY}
        assert conn.execute(
            "SELECT asset_key FROM image_urls WHERE url_id=1"
        ).fetchone()[0] == V100_KEY
        assert conn.execute(
            "SELECT asset_key FROM image_urls WHERE url_id=2"
        ).fetchone()[0] == V100_KEY
        assert conn.execute(
            "SELECT asset_key FROM image_urls WHERE url_id=3"
        ).fetchone()[0] == V101_KEY
        assert conn.execute(
            "SELECT asset_key FROM source_image_occurrences "
            "WHERE article_id=1 AND role='gallery' AND position=1"
        ).fetchone()[0] == V101_KEY
        assert conn.execute(
            "SELECT asset_key FROM image_url_hints WHERE url_id=2"
        ).fetchone()[0] == V100_KEY
        assert conn.execute("SELECT COUNT(*) FROM image_hashes").fetchone()[0] == 3
        assert conn.execute(
            "SELECT COUNT(*) FROM image_hashes WHERE status='pending'"
        ).fetchone()[0] == 3
        assert conn.execute(
            "SELECT COUNT(*) FROM building_images_materialized_v2"
        ).fetchone()[0] == 3
        assert conn.execute(
            "SELECT COUNT(*) FROM building_images_materialized_v2_3"
        ).fetchone()[0] == 3
        assert conn.execute(
            "SELECT COUNT(*) FROM image_asset_key_map_v2_4"
        ).fetchone()[0] == 2
        lineage = conn.execute(
            "SELECT metadata_version,asset_key_version "
            "FROM image_identity_lineage_v2_4"
        ).fetchone()
        assert lineage == (METADATA_VERSION, ASSET_KEY_VERSION)
        assert conn.execute(
            "SELECT state_value FROM unrelated_parent_state WHERE state_key='keep'"
        ).fetchone()[0] == "exactly"
        assert conn.execute(
            "SELECT COUNT(*) FROM image_identity_validation_v2_4 WHERE passed<>1"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_no_clobber_and_processed_image_state_are_rejected(tmp_path: Path):
    parent = tmp_path / "parent.db"
    make_parent(parent)
    output = tmp_path / "output.db"
    report = tmp_path / "report.md"
    output.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        build_artifact(
            parent_path=parent,
            output_path=output,
            report_path=report,
            production_contract=False,
        )
    assert output.read_bytes() == b"existing"

    output.unlink()
    conn = sqlite3.connect(parent)
    try:
        conn.execute(
            """
            UPDATE image_hashes
            SET status='success',attempt_count=1,hash_bits=256,
                hash_hex=?,computed_at='2026-08-04'
            WHERE asset_key=?
            """,
            ("0" * 64, OLD_MODERN_KEY),
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(RuntimeError, match="untouched pending image state"):
        build_artifact(
            parent_path=parent,
            output_path=output,
            report_path=report,
            production_contract=False,
        )
