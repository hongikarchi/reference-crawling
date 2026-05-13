import json
import sqlite3

from core import config
from crawl.divisare import db as divisare_db


def test_deep_project_upsert_preserves_existing_architects_when_parser_returns_empty(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "divisare.db"
    monkeypatch.setattr(config, "DIVISARE_DB_PATH", str(db_path))

    divisare_db.init_db()
    divisare_db.upsert_project_lite(
        {
            "id": 1,
            "slug": "studio-a-house-a",
            "name": "House A",
            "architect_names": ["Studio A"],
            "location_city": "Seoul",
            "location_country": "South Korea",
        },
        primary_architect_id=101,
    )
    divisare_db.upsert_project(
        {
            "id": 1,
            "slug": "studio-a-house-a",
            "name": "House A",
            "architect_ids": [],
            "architect_names": [],
            "location_city": "Seoul",
            "location_country": "South Korea",
            "project_year": 2024,
            "description": "Deep fetched text.",
        }
    )

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT architect_ids, architect_names, project_year, description "
        "FROM divisare_projects WHERE id=1"
    ).fetchone()
    conn.close()

    assert json.loads(row[0]) == [101]
    assert json.loads(row[1]) == ["Studio A"]
    assert row[2] == 2024
    assert row[3] == "Deep fetched text."


def test_deep_project_upsert_replaces_architects_when_parser_finds_values(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "divisare.db"
    monkeypatch.setattr(config, "DIVISARE_DB_PATH", str(db_path))

    divisare_db.init_db()
    divisare_db.upsert_project_lite(
        {
            "id": 1,
            "slug": "studio-a-house-a",
            "name": "House A",
            "architect_names": ["Studio A"],
        },
        primary_architect_id=101,
    )
    divisare_db.upsert_project(
        {
            "id": 1,
            "slug": "studio-b-house-a",
            "name": "House A",
            "architect_ids": [202],
            "architect_names": ["Studio B"],
        }
    )

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT architect_ids, architect_names FROM divisare_projects WHERE id=1"
    ).fetchone()
    conn.close()

    assert json.loads(row[0]) == [202]
    assert json.loads(row[1]) == ["Studio B"]
