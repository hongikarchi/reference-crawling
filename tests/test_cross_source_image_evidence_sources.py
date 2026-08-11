from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from canonical.cross_source_image_evidence_sources import (
    SourceMappingError,
    open_architizer_sources,
    open_divisare_sources,
)


def _create_e1(
    path: Path,
    *,
    source: str,
    eligible: list[dict[str, object]],
    excluded: list[dict[str, object]],
    status: str = "complete_with_failures",
) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE fingerprint_runs (
          run_id TEXT PRIMARY KEY,
          source_name TEXT NOT NULL,
          status TEXT NOT NULL,
          source_db_sha256_before TEXT NOT NULL,
          source_db_sha256_after TEXT,
          fingerprint_contract_version TEXT NOT NULL,
          selection_count INTEGER NOT NULL,
          excluded_count INTEGER NOT NULL,
          source_total_count INTEGER NOT NULL
        );
        CREATE TABLE source_assets (
          run_id TEXT NOT NULL,
          source_asset_id TEXT NOT NULL,
          source_record_sha256 TEXT NOT NULL,
          canonical_url TEXT NOT NULL,
          fetch_url TEXT NOT NULL,
          PRIMARY KEY(run_id,source_asset_id)
        );
        CREATE TABLE source_asset_exclusions (
          run_id TEXT NOT NULL,
          source_asset_id TEXT NOT NULL,
          source_asset_key TEXT NOT NULL,
          source_record_sha256 TEXT NOT NULL,
          reason_code TEXT NOT NULL,
          detail_json TEXT NOT NULL,
          PRIMARY KEY(run_id,source_asset_id)
        );
        CREATE TABLE fingerprints (
          run_id TEXT NOT NULL,
          source_asset_id TEXT NOT NULL,
          status TEXT NOT NULL,
          raw_response_sha256 TEXT,
          normalized_pixel_sha256 TEXT,
          phash_hex TEXT,
          original_width INTEGER,
          original_height INTEGER,
          normalized_width INTEGER,
          normalized_height INTEGER,
          metadata_json TEXT NOT NULL,
          error_kind TEXT,
          error_message TEXT,
          PRIMARY KEY(run_id,source_asset_id)
        );
        """
    )
    run_id = f"e1-{source}-fixture"
    connection.execute(
        "INSERT INTO fingerprint_runs VALUES (?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            source,
            status,
            "1" * 64,
            "1" * 64,
            "fixture-contract-v1",
            len(eligible),
            len(excluded),
            len(eligible) + len(excluded),
        ),
    )
    for rank, row in enumerate(eligible, 1):
        asset_id = str(row["source_asset_id"])
        connection.execute(
            "INSERT INTO source_assets VALUES (?,?,?,?,?)",
            (
                run_id,
                asset_id,
                str(rank) * 64,
                f"https://source.test/{asset_id}",
                f"https://fetch.test/{asset_id}",
            ),
        )
        success = row["status"] == "success"
        metadata = (
            json.dumps({"quality_flags": row.get("quality_flags", [])})
            if success
            else "{}"
        )
        connection.execute(
            "INSERT INTO fingerprints VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                asset_id,
                row["status"],
                "a" * 64 if success else None,
                str(row.get("pixel_sha", "b" * 64)) if success else None,
                str(row.get("phash", "c" * 64)) if success else None,
                1200 if success else None,
                800 if success else None,
                512 if success else None,
                341 if success else None,
                metadata,
                None if success else row.get("error_kind", "http_404"),
                None if success else "fixture failure",
            ),
        )
    for rank, row in enumerate(excluded, 1):
        connection.execute(
            "INSERT INTO source_asset_exclusions VALUES (?,?,?,?,?,?)",
            (
                run_id,
                row["source_asset_id"],
                row["source_asset_key"],
                str(rank + 8) * 64,
                row.get("reason_code", "placeholder_candidate"),
                json.dumps({"fixture": True}),
            ),
        )
    connection.commit()
    connection.close()


def _create_divisare(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE source_articles (
          article_id INTEGER PRIMARY KEY,slug TEXT,source_url TEXT,name_raw TEXT,
          name_normalized TEXT,location_country TEXT,location_city TEXT,
          project_year INTEGER,article_kind TEXT,source_row_hash TEXT
        );
        CREATE TABLE article_metadata_resolution_v2_3 (
          article_id INTEGER PRIMARY KEY,availability_status TEXT,
          resolved_name TEXT,resolved_name_normalized TEXT,
          location_country TEXT,location_city TEXT,project_year INTEGER
        );
        CREATE TABLE source_architects (
          architect_id INTEGER PRIMARY KEY,slug TEXT,name TEXT
        );
        CREATE TABLE article_architects (
          article_id INTEGER,position INTEGER,architect_id INTEGER,
          architect_name TEXT
        );
        CREATE TABLE buildings (
          building_id TEXT PRIMARY KEY,primary_article_id INTEGER,name TEXT,
          name_normalized TEXT,location_country TEXT,location_city TEXT,
          project_year INTEGER,cluster_status TEXT
        );
        CREATE TABLE active_building_membership_v2_3 (
          article_id INTEGER,building_id TEXT,source_article_role TEXT,
          membership_confidence REAL,decision_method TEXT
        );
        CREATE TABLE image_assets (asset_key TEXT PRIMARY KEY);
        CREATE TABLE source_image_occurrences (
          article_id INTEGER,role TEXT,position INTEGER,raw_url TEXT,
          parse_status TEXT,parse_error TEXT,asset_key TEXT
        );
        """
    )
    connection.executemany(
        "INSERT INTO source_articles VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (
                1,
                "alpha",
                "https://divisare.com/projects/alpha",
                "Alpha raw",
                "alpha raw",
                "Italy",
                "Rome",
                2020,
                "project",
                "d" * 64,
            ),
            (
                2,
                "alpha-gallery",
                "https://divisare.com/projects/alpha-gallery",
                "Alpha gallery",
                "alpha gallery",
                "Italy",
                "Rome",
                2020,
                "photo_feature",
                "e" * 64,
            ),
        ],
    )
    connection.executemany(
        "INSERT INTO article_metadata_resolution_v2_3 VALUES (?,?,?,?,?,?,?)",
        [
            (1, "available", "Alpha House", "alpha house", "Italy", "Rome", 2021),
            (2, "available", "Alpha Gallery", "alpha gallery", "Italy", "Rome", 2021),
        ],
    )
    connection.execute(
        "INSERT INTO source_architects VALUES (?,?,?)", (7, "atelier-a", "Atelier A")
    )
    connection.executemany(
        "INSERT INTO article_architects VALUES (?,?,?,?)",
        [(1, 0, 7, "Atelier A"), (2, 0, 7, "Atelier A")],
    )
    connection.execute(
        "INSERT INTO buildings VALUES (?,?,?,?,?,?,?,?)",
        ("div-b1", 1, "Alpha House", "alpha house", "Italy", "Rome", 2021, "resolved"),
    )
    connection.executemany(
        "INSERT INTO active_building_membership_v2_3 VALUES (?,?,?,?,?)",
        [(1, "div-b1", "primary", 1.0, "primary"), (2, "div-b1", "support", 0.8, "reviewed")],
    )
    connection.executemany(
        "INSERT INTO image_assets VALUES (?)",
        [("asset-a",), ("asset-b",), ("asset-c",)],
    )
    connection.executemany(
        "INSERT INTO source_image_occurrences VALUES (?,?,?,?,?,?,?)",
        [
            (1, "cover", 0, "https://images.test/a.jpg", "parsed", None, "asset-a"),
            (1, "gallery", 1, "https://images.test/a.jpg", "parsed", None, "asset-a"),
            (2, "gallery", 0, "https://images.test/b.jpg", "parsed", None, "asset-b"),
            (2, "gallery", 1, "bad", "malformed", "bad url", None),
        ],
    )
    connection.commit()
    connection.close()


def _create_architizer(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE source_projects (
          source_project_id INTEGER PRIMARY KEY,global_id TEXT,slug TEXT,
          source_url TEXT,name TEXT,normalized_name TEXT,
          location_country_raw TEXT,location_city_raw TEXT,
          completion_year_raw INTEGER,source_firm_slug TEXT,
          source_firm_name TEXT,acceptance_status TEXT,exclusion_reason TEXT
        );
        CREATE TABLE buildings (
          building_id TEXT PRIMARY KEY,primary_project_id INTEGER,
          preferred_name TEXT,normalized_name TEXT,location_country TEXT,
          location_city TEXT,completion_year INTEGER,source_firm_slug TEXT,
          identity_status TEXT
        );
        CREATE TABLE building_projects (
          building_id TEXT,source_project_id INTEGER,is_primary INTEGER,
          membership_status TEXT,rule_id TEXT
        );
        CREATE TABLE image_assets (
          asset_id TEXT PRIMARY KEY,asset_key TEXT
        );
        CREATE TABLE source_image_occurrences (
          occurrence_id TEXT,source_project_id INTEGER,role TEXT,ordinal INTEGER,
          raw_url TEXT,asset_id TEXT,parse_status TEXT,parse_error TEXT,
          source_field TEXT,image_type TEXT
        );
        """
    )
    connection.executemany(
        "INSERT INTO source_projects VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (10, "projects.project.10", "beta", "https://architizer.com/beta", "Beta Tower", "beta tower", "Japan", "Tokyo", 2022, "firm-b", "Firm B", "accepted", None),
            (11, "projects.project.11", "beta-photos", "https://architizer.com/beta-photos", "Beta Photos", "beta photos", "Japan", "Tokyo", 2022, "firm-b", "Firm B", "accepted", None),
        ],
    )
    connection.execute(
        "INSERT INTO buildings VALUES (?,?,?,?,?,?,?,?,?)",
        ("arch-b1", 10, "Beta Tower", "beta tower", "Japan", "Tokyo", 2022, "firm-b", "resolved"),
    )
    connection.executemany(
        "INSERT INTO building_projects VALUES (?,?,?,?,?)",
        [("arch-b1", 10, 1, "accepted", "primary"), ("arch-b1", 11, 0, "accepted", "same_identity")],
    )
    connection.executemany(
        "INSERT INTO image_assets VALUES (?,?)",
        [("global-1", "imgix:/one.jpg"), ("global-2", "imgix:/two.jpg"), ("global-3", "imgix:/three.jpg")],
    )
    connection.executemany(
        "INSERT INTO source_image_occurrences VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            ("occ-10-cover", 10, "cover", 0, "https://imgix.test/one.jpg", "global-1", "parsed", None, "cover", "photo"),
            ("occ-10-gallery", 10, "gallery", 1, "https://imgix.test/one.jpg", "global-1", "parsed", None, "gallery", "photo"),
            ("occ-11-gallery", 11, "gallery", 0, "https://imgix.test/three.jpg", "global-3", "parsed", None, "gallery", "photo"),
        ],
    )
    connection.commit()
    connection.close()


def test_divisare_mapping_preserves_occurrences_failures_and_exclusions(
    tmp_path: Path,
) -> None:
    source = tmp_path / "divisare.db"
    e1 = tmp_path / "divisare-e1.db"
    _create_divisare(source)
    _create_e1(
        e1,
        source="divisare",
        eligible=[
            {"source_asset_id": "asset-a", "status": "success", "quality_flags": ["alpha"]},
            {"source_asset_id": "asset-b", "status": "failed", "error_kind": "decode"},
        ],
        excluded=[{"source_asset_id": "asset-c", "source_asset_key": "asset-c"}],
    )

    with open_divisare_sources(source, e1, batch_size=1) as adapter:
        projects = list(adapter.iter_projects())
        buildings = list(adapter.iter_buildings())
        memberships = list(adapter.iter_memberships())
        assets = list(adapter.iter_assets())
        occurrences = list(adapter.iter_occurrences())

    assert projects[0].name == "Alpha House"
    assert projects[0].normalized_name == "alpha house"
    assert projects[0].firm_slug == "atelier-a"
    assert projects[0].firm_name == "Atelier A"
    assert buildings[0].country == "Italy"
    assert [row.is_primary for row in memberships] == [True, False]
    assert [row.source_asset_id for row in assets] == ["asset-a", "asset-b", "asset-c"]
    assert assets[0].has_fingerprint is True
    assert assets[0].quality_flags == ("alpha",)
    assert assets[1].fingerprint_status == "failed"
    assert assets[1].has_fingerprint is False
    assert assets[1].error_kind == "decode"
    assert assets[2].ledger_status == "excluded"
    assert assets[2].exclusion_reason == "placeholder_candidate"
    assert [row.source_asset_id for row in occurrences[:2]] == ["asset-a", "asset-a"]
    assert occurrences[0].occurrence_id == "1:cover:0"
    assert occurrences[-1].source_asset_id is None
    assert occurrences[-1].parse_status == "malformed"


def test_architizer_mapping_keeps_asset_id_distinct_from_asset_key_and_membership(
    tmp_path: Path,
) -> None:
    source = tmp_path / "architizer.db"
    e1 = tmp_path / "architizer-e1.db"
    _create_architizer(source)
    _create_e1(
        e1,
        source="architizer",
        eligible=[
            {"source_asset_id": "global-1", "status": "success"},
            {"source_asset_id": "global-3", "status": "failed", "error_kind": "empty_response"},
        ],
        excluded=[
            {
                "source_asset_id": "global-2",
                "source_asset_key": "imgix:/two.jpg",
                "reason_code": "placeholder_candidate",
            }
        ],
    )

    with open_architizer_sources(source, e1, batch_size=1) as adapter:
        projects = list(adapter.iter_projects())
        buildings = list(adapter.iter_buildings())
        memberships = list(adapter.iter_memberships())
        assets = list(adapter.iter_assets())
        occurrences = list(adapter.iter_occurrences())

    assert projects[0].global_id == "projects.project.10"
    assert projects[0].firm_name == "Firm B"
    assert buildings[0].firm_slug == "firm-b"
    assert [row.source_project_id for row in memberships] == ["10", "11"]
    assert [row.is_primary for row in memberships] == [True, False]
    assert assets[0].source_asset_id == "global-1"
    assert assets[0].source_asset_key == "imgix:/one.jpg"
    assert assets[1].source_asset_id == "global-2"
    assert assets[1].source_asset_key == "imgix:/two.jpg"
    assert assets[1].fingerprint_status == "excluded"
    assert assets[2].fingerprint_status == "failed"
    assert occurrences[0].source_asset_id == "global-1"
    assert occurrences[0].source_asset_id != assets[0].source_asset_key
    assert [row.source_asset_id for row in occurrences[:2]] == ["global-1", "global-1"]


@pytest.mark.parametrize("bad_status", ["initializing", "running", "failed_validation"])
def test_adapter_rejects_nonterminal_e1_run(tmp_path: Path, bad_status: str) -> None:
    source = tmp_path / "divisare.db"
    e1 = tmp_path / "e1.db"
    _create_divisare(source)
    _create_e1(
        e1,
        source="divisare",
        eligible=[{"source_asset_id": "asset-a", "status": "success"}],
        excluded=[],
        status=bad_status,
    )
    with pytest.raises(SourceMappingError, match="not complete"):
        open_divisare_sources(source, e1)


def test_adapter_rejects_missing_asset_ledger_row(tmp_path: Path) -> None:
    source = tmp_path / "divisare.db"
    e1 = tmp_path / "e1.db"
    _create_divisare(source)
    _create_e1(
        e1,
        source="divisare",
        eligible=[
            {"source_asset_id": "asset-a", "status": "success"},
            {"source_asset_id": "asset-b", "status": "failed"},
        ],
        excluded=[],
    )
    with open_divisare_sources(source, e1) as adapter:
        with pytest.raises(SourceMappingError, match="missing from E1 ledger"):
            list(adapter.iter_assets())
