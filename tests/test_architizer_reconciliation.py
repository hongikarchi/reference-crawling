"""Offline N10-style coverage for the Architizer v2 reconciliation plan."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from canonical.architizer_curated import SCHEMA_VERSION as BASELINE_SCHEMA_VERSION
from canonical.architizer_reconciliation import (
    Candidate,
    canonical_entity_url,
    entity_slug_from_url,
    reconcile_field,
    validate_last_good_identity,
)
from tools import reconcile_architizer_curated_v2 as reconciliation


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest().upper()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


RAW_DDL = """
CREATE TABLE architizer_projects (
    id INTEGER PRIMARY KEY, global_id TEXT UNIQUE, slug TEXT UNIQUE,
    name TEXT, firm_slug TEXT, firm_name TEXT, description TEXT,
    description_short TEXT, completion_year INTEGER,
    building_size_slug TEXT, building_size_display TEXT, constr_status TEXT,
    budget REAL, location_full TEXT, location_country TEXT,
    location_city TEXT, categories TEXT, cover_image_url TEXT,
    gallery_image_urls TEXT, image_global_ids TEXT, published_time TEXT,
    modified_time TEXT, fetched_at TEXT
);
CREATE TABLE architizer_firms (
    slug TEXT PRIMARY KEY, name TEXT, office_locations TEXT,
    description TEXT, awards_summary TEXT, project_count_seen INTEGER,
    social_links TEXT, fetched_at TEXT
);
"""


BASELINE_DDL = """
CREATE TABLE build_runs (
    build_id TEXT PRIMARY KEY, schema_version TEXT NOT NULL,
    builder_version TEXT NOT NULL, deterministic_timestamp TEXT NOT NULL
);
CREATE TABLE source_snapshots (
    build_id TEXT PRIMARY KEY, source_sha256_before TEXT NOT NULL,
    source_sha256_after TEXT NOT NULL
);
CREATE TABLE source_firms (
    source_firm_slug TEXT PRIMARY KEY, source_name TEXT,
    record_origin TEXT NOT NULL, office_locations_raw_json TEXT,
    description TEXT, awards_summary TEXT, project_count_seen INTEGER,
    social_links_raw_json TEXT, fetched_at TEXT, source_url TEXT NOT NULL
);
CREATE TABLE source_projects (
    source_project_id INTEGER PRIMARY KEY, global_id TEXT NOT NULL UNIQUE,
    slug TEXT NOT NULL UNIQUE, source_url TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL, source_firm_slug TEXT,
    source_firm_name TEXT, description TEXT, description_short TEXT,
    completion_year_raw INTEGER, building_size_slug TEXT,
    building_size_display TEXT, constr_status_raw TEXT, budget_raw REAL,
    location_full TEXT, location_country_raw TEXT, location_city_raw TEXT,
    categories_raw_json TEXT, gallery_image_urls_raw_json TEXT,
    image_global_ids_raw_json TEXT, published_time TEXT, modified_time TEXT,
    fetched_at TEXT, acceptance_status TEXT NOT NULL
);
CREATE INDEX idx_fixture_project_slug ON source_projects(slug);
CREATE VIEW v_contract_probe AS
SELECT source_project_id,slug,name FROM source_projects;
"""


SIDECAR_DDL = """
CREATE TABLE state_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE runs (
    id INTEGER PRIMARY KEY, run_kind TEXT NOT NULL, status TEXT NOT NULL,
    parser_version TEXT NOT NULL, finished_at TEXT, selected_count INTEGER,
    summary_json TEXT
);
CREATE TABLE targets (
    url TEXT PRIMARY KEY, entity_type TEXT NOT NULL, status TEXT NOT NULL,
    retryable INTEGER NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT, last_attempt_at TEXT, last_error TEXT,
    last_http_status INTEGER, last_snapshot_sha256 TEXT,
    last_parse_status TEXT, last_good_version_id INTEGER
);
CREATE TABLE target_reasons (
    url TEXT NOT NULL, reason TEXT NOT NULL, discovery_source TEXT NOT NULL,
    priority INTEGER NOT NULL, source_lastmod TEXT,
    first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
    input_lineage_json TEXT NOT NULL,
    PRIMARY KEY(url,reason,discovery_source)
);
CREATE TABLE metadata_versions (
    id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL,
    target_url TEXT NOT NULL, entity_type TEXT NOT NULL,
    snapshot_sha256 TEXT NOT NULL, parser_version TEXT NOT NULL,
    metadata_version TEXT NOT NULL, parsed_at TEXT NOT NULL,
    parse_status TEXT NOT NULL, quality TEXT NOT NULL,
    identity_status TEXT NOT NULL, identity_json TEXT NOT NULL,
    raw_embedded_json TEXT NOT NULL DEFAULT '[]',
    dom_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE http_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL,
    target_url TEXT, request_kind TEXT, requested_url TEXT,
    outcome TEXT NOT NULL, http_status INTEGER,
    final_url TEXT, content_type TEXT, response_bytes INTEGER,
    sha256 TEXT, gzip_path TEXT, block_signals_json TEXT, error TEXT
);
CREATE TABLE run_targets (run_id INTEGER NOT NULL,url TEXT NOT NULL);
CREATE TABLE resolved_fields (
    version_id INTEGER NOT NULL, field_name TEXT NOT NULL, value_json TEXT,
    status TEXT NOT NULL, quality TEXT NOT NULL, conflict_json TEXT,
    PRIMARY KEY(version_id,field_name)
);
CREATE TABLE field_observations (
    version_id INTEGER NOT NULL, field_name TEXT NOT NULL,
    source_kind TEXT NOT NULL, raw_value_json TEXT,
    normalized_value_json TEXT, parse_status TEXT NOT NULL,
    quality TEXT NOT NULL,
    PRIMARY KEY(version_id,field_name,source_kind)
);
CREATE TABLE relationships (
    version_id INTEGER NOT NULL, relation_kind TEXT NOT NULL,
    related_entity_type TEXT NOT NULL, related_slug TEXT,
    related_url TEXT, source_kind TEXT NOT NULL, parse_status TEXT NOT NULL
);
CREATE TABLE current_fields (
    target_url TEXT NOT NULL, field_name TEXT NOT NULL, value_json TEXT NOT NULL,
    version_id INTEGER NOT NULL
);
CREATE TABLE snapshot_reparse_inputs (
    run_id INTEGER NOT NULL,target_url TEXT NOT NULL,selection_order INTEGER NOT NULL,
    entity_type TEXT NOT NULL,selection_kind TEXT NOT NULL,source_run_id INTEGER NOT NULL,
    source_metadata_version_id INTEGER NOT NULL,source_http_attempt_id INTEGER NOT NULL,
    request_kind TEXT NOT NULL,requested_url TEXT NOT NULL,http_outcome TEXT NOT NULL,
    http_status INTEGER NOT NULL,block_signals_json TEXT NOT NULL,attempt_error TEXT,
    content_sha256 TEXT NOT NULL,final_url TEXT NOT NULL,content_type TEXT NOT NULL,
    response_bytes INTEGER NOT NULL,gzip_path TEXT NOT NULL,gzip_sha256 TEXT NOT NULL,
    integrity_status TEXT NOT NULL,target_network_state_json TEXT NOT NULL,frozen_at TEXT NOT NULL,
    PRIMARY KEY(run_id,target_url),UNIQUE(run_id,selection_order)
);
CREATE TABLE snapshot_reparse_lineage (
    reparse_version_id INTEGER PRIMARY KEY,reparse_run_id INTEGER NOT NULL,
    entity_type TEXT NOT NULL,selection_kind TEXT NOT NULL,source_run_id INTEGER NOT NULL,
    source_metadata_version_id INTEGER NOT NULL,source_http_attempt_id INTEGER NOT NULL,
    request_kind TEXT NOT NULL,requested_url TEXT NOT NULL,http_outcome TEXT NOT NULL,
    http_status INTEGER NOT NULL,block_signals_json TEXT NOT NULL,attempt_error TEXT,
    target_url TEXT NOT NULL,content_sha256 TEXT NOT NULL,final_url TEXT NOT NULL,
    content_type TEXT NOT NULL,response_bytes INTEGER NOT NULL,gzip_path TEXT NOT NULL,
    gzip_sha256 TEXT NOT NULL,integrity_status TEXT NOT NULL,verified_at TEXT NOT NULL,
    UNIQUE(reparse_run_id,target_url)
);
"""


def _make_raw(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(RAW_DDL)
    for project_id in range(1, 9):
        slug = f"project-{project_id:02d}"
        connection.execute(
            """
            INSERT INTO architizer_projects VALUES (
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
            )
            """,
            (
                project_id,
                f"projects.project.{project_id}",
                slug,
                f"Project {project_id}",
                "firm-a" if project_id <= 4 else "firm-b",
                "Firm A" if project_id <= 4 else "Firm B",
                f"Baseline description {project_id}",
                f"Short {project_id}",
                2000 + project_id,
                "sqft_1_3",
                "1,000 - 3,000 sqft",
                "built",
                None,
                "Seoul, South Korea",
                "South Korea",
                "Seoul",
                _json(["Residential", "Apartment"]),
                f"https://static-web-prod.arc.ht/project-{project_id}/cover.jpg",
                _json([f"https://static-web-prod.arc.ht/project-{project_id}/cover.jpg"]),
                _json([f"media.mediaitemattribution.{project_id}"]),
                "2020-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
                "2026-04-28T00:00:00Z",
            ),
        )
    connection.executemany(
        "INSERT INTO architizer_firms VALUES (?,?,?,?,?,?,?,?)",
        [
            (
                "firm-a",
                "Firm A",
                _json(["Seoul"]),
                "Baseline firm A",
                None,
                4,
                _json({"instagram": "https://instagram.com/firm-a"}),
                "2026-04-28T00:00:00Z",
            ),
            (
                "firm-b",
                "Firm B",
                _json(["Busan"]),
                "Baseline firm B",
                None,
                4,
                _json({}),
                "2026-04-28T00:00:00Z",
            ),
        ],
    )
    connection.commit()
    connection.close()


def _make_baseline(path: Path, raw_sha: str) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(BASELINE_DDL)
    existing_tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    for table in sorted(reconciliation.BASELINE_REQUIRED_TABLES - existing_tables):
        connection.execute(f'CREATE TABLE "{table}" (fixture_id INTEGER)')
    for view in sorted(reconciliation.BASELINE_REQUIRED_VIEWS):
        connection.execute(f'CREATE VIEW "{view}" AS SELECT 1 AS fixture_value')
    connection.execute(
        "INSERT INTO build_runs VALUES (?,?,?,?)",
        (
            "atz_fixture_v1_3",
            BASELINE_SCHEMA_VERSION,
            "architizer-curated-builder-v1.6",
            "2026-07-31T00:00:00Z",
        ),
    )
    connection.execute(
        "INSERT INTO source_snapshots VALUES (?,?,?)",
        ("atz_fixture_v1_3", raw_sha, raw_sha),
    )
    connection.executemany(
        "INSERT INTO source_firms VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (
                "firm-a",
                "Firm A",
                "crawled",
                _json(["Seoul"]),
                "Baseline firm A",
                None,
                4,
                _json({"instagram": "https://instagram.com/firm-a"}),
                "2026-04-28T00:00:00Z",
                "https://architizer.com/firms/firm-a/",
            ),
            (
                "firm-b",
                "Firm B",
                "crawled",
                _json(["Busan"]),
                "Baseline firm B",
                None,
                4,
                _json({}),
                "2026-04-28T00:00:00Z",
                "https://architizer.com/firms/firm-b/",
            ),
        ],
    )
    raw = sqlite3.connect(path.parent / "raw.db")
    raw.row_factory = sqlite3.Row
    for row in raw.execute("SELECT * FROM architizer_projects ORDER BY id"):
        connection.execute(
            """
            INSERT INTO source_projects VALUES (
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
            )
            """,
            (
                row["id"],
                row["global_id"],
                row["slug"],
                f"https://architizer.com/projects/{row['slug']}/",
                row["name"],
                row["firm_slug"],
                row["firm_name"],
                row["description"],
                row["description_short"],
                row["completion_year"],
                row["building_size_slug"],
                row["building_size_display"],
                row["constr_status"],
                row["budget"],
                row["location_full"],
                row["location_country"],
                row["location_city"],
                row["categories"],
                (
                    _json(["https://curated.example/project-03/gallery.jpg"])
                    if row["id"] == 3
                    else row["gallery_image_urls"]
                ),
                (
                    _json(["media.mediaitemattribution.curated-3"])
                    if row["id"] == 3
                    else row["image_global_ids"]
                ),
                row["published_time"],
                row["modified_time"],
                row["fetched_at"],
                "accepted",
            ),
        )
    raw.close()
    connection.commit()
    connection.close()


def _resolved(
    connection: sqlite3.Connection,
    version_id: int,
    field: str,
    value: object,
    status: str = "confirmed",
    quality: str = "high",
    conflict: object = None,
) -> None:
    connection.execute(
        "INSERT INTO resolved_fields VALUES (?,?,?,?,?,?)",
        (
            version_id,
            field,
            None if value is None else _json(value),
            status,
            quality,
            None if conflict is None else _json(conflict),
        ),
    )


def _observation(
    connection: sqlite3.Connection,
    version_id: int,
    field: str,
    source: str,
    value: object,
) -> None:
    connection.execute(
        "INSERT INTO field_observations VALUES (?,?,?,?,?,?,?)",
        (
            version_id,
            field,
            source,
            _json(value),
            _json(value),
            "observed",
            "high" if source == "embedded_json" else "medium",
        ),
    )


def _make_sidecar(
    path: Path, raw_sha: str, raw_size: int, *, pending: bool = False
) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(SIDECAR_DDL)
    connection.executemany(
        "INSERT INTO state_meta VALUES (?,?)",
        [
            ("schema_version", reconciliation.STATE_SCHEMA_VERSION),
            ("source_db_sha256", raw_sha),
            ("source_db_size", str(raw_size)),
        ],
    )
    connection.execute(
        "INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
        (
            1,
            "full",
            "completed",
            "architizer-source-parser-v2.2.0",
            "2026-08-04T00:00:00Z",
            0,
            _json({}),
        ),
    )

    versions = [
        (101, "project", "project-01", 1, "projects.project.1", "Project 1 updated", "firm-a", True),
        (102, "project", "project-02", 2, "projects.project.2", "Project 2 poisoned", "firm-a", False),
        (109, "project", "project-09", 9, "projects.project.9", "Project 9 new", "firm-new", True),
        (110, "project", "project-10", 10, "projects.project.10", "Project 10 rejected", "firm-new", False),
    ]
    for version_id, entity_type, slug, project_id, global_id, name, firm_slug, valid_payload in versions:
        url = f"https://architizer.com/projects/{slug}/"
        payload_slug = slug if valid_payload else f"wrong-{slug}"
        payload = {
            "status": "valid",
            "expected_slug": payload_slug,
            "final_slug": slug,
            "canonical_slug": slug,
            "embedded_slug": slug,
            "project_id": project_id,
            "global_id": global_id,
            "errors": [],
            "missing": [],
        }
        connection.execute(
            "INSERT INTO targets(url,entity_type,status,retryable,last_good_version_id) "
            "VALUES (?,?,?,?,?)",
            (url, entity_type, "done", 1, version_id),
        )
        connection.execute("INSERT INTO run_targets VALUES (1,?)", (url,))
        connection.execute(
            """INSERT INTO metadata_versions(
                id,run_id,target_url,entity_type,snapshot_sha256,parser_version,
                metadata_version,parsed_at,parse_status,quality,identity_status,
                identity_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                version_id,
                1,
                url,
                entity_type,
                f"snapshot-{version_id}",
                "architizer-source-parser-v2.2.0",
                "architizer-source-metadata-v2.2",
                "2026-08-04T00:00:00Z",
                "conflict" if version_id == 101 else "complete",
                "review" if version_id == 101 else "high",
                "valid",
                _json(payload),
            ),
        )
        connection.execute(
            """
            INSERT INTO http_attempts(
                run_id,target_url,request_kind,requested_url,outcome,http_status,
                final_url,content_type,response_bytes,sha256,gzip_path,
                block_signals_json,error
            ) VALUES (?,?, 'project_page',?,'success',200,?,'text/html',100,?,?, '[]',NULL)
            """,
            (1, url, url, url, f"snapshot-{version_id}", f"{version_id}.html.gz"),
        )
        for field, value in {
            "project_id": project_id,
            "global_id": global_id,
            "slug": slug,
            "name": name,
            "firm_slug": firm_slug,
            "firm_name": "Firm A" if firm_slug == "firm-a" else "Firm New",
            "location": None if version_id == 101 else "Tokyo, Japan",
            "completion_year": 2026,
            "construction_status": "built",
            "size_bucket": "sqft_3_5",
            "description_short": f"New short {project_id}",
            "categories": ["Commercial", "Office"],
            "cover_image_url": f"https://static-web-prod.arc.ht/{slug}/cover.jpg",
            "gallery_image_urls": [f"https://static-web-prod.arc.ht/{slug}/cover.jpg"],
            "image_global_ids": [f"media.mediaitemattribution.{project_id}"],
            "published_time": "2026-01-01T00:00:00Z",
            "modified_time": "2026-08-01T00:00:00Z",
        }.items():
            status = "missing" if value is None else "confirmed"
            _resolved(connection, version_id, field, value, status, "none" if value is None else "high")
        if version_id == 101:
            _resolved(
                connection,
                version_id,
                "description",
                None,
                "conflict",
                "review",
                {"embedded_json": "New embedded", "dom": "New DOM"},
            )
            _observation(connection, version_id, "name", "embedded_json", name)
            _observation(connection, version_id, "name", "dom", name)
            _observation(connection, version_id, "description", "embedded_json", "New embedded")
            _observation(connection, version_id, "description", "dom", "New DOM")
        else:
            _resolved(connection, version_id, "description", f"New description {project_id}")
        connection.execute(
            "INSERT INTO relationships VALUES (?,?,?,?,?,?,?)",
            (
                version_id,
                "project_firm",
                "firm",
                firm_slug,
                f"https://architizer.com/firms/{firm_slug}/",
                "embedded_json",
                "observed",
            ),
        )

    firm_versions = [
        (201, "firm-a", "Firm A", "Updated firm A"),
        (202, "firm-new", "Firm New", "New firm description"),
    ]
    for version_id, slug, name, description in firm_versions:
        url = f"https://architizer.com/firms/{slug}/"
        payload = {
            "status": "valid",
            "expected_slug": slug,
            "final_slug": slug,
            "canonical_slug": slug,
            "embedded_slug": slug,
            "errors": [],
            "missing": [],
        }
        connection.execute(
            "INSERT INTO targets(url,entity_type,status,retryable,last_good_version_id) "
            "VALUES (?,?,'done',1,?)", (url, "firm", version_id)
        )
        connection.execute("INSERT INTO run_targets VALUES (1,?)", (url,))
        connection.execute(
            """INSERT INTO metadata_versions(
                id,run_id,target_url,entity_type,snapshot_sha256,parser_version,
                metadata_version,parsed_at,parse_status,quality,identity_status,
                identity_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                version_id,
                1,
                url,
                "firm",
                f"snapshot-{version_id}",
                "architizer-source-parser-v2.2.0",
                "architizer-source-metadata-v2.2",
                "2026-08-04T00:00:00Z",
                "complete",
                "high",
                "valid",
                _json(payload),
            ),
        )
        connection.execute(
            """
            INSERT INTO http_attempts(
                run_id,target_url,request_kind,requested_url,outcome,http_status,
                final_url,content_type,response_bytes,sha256,gzip_path,
                block_signals_json,error
            ) VALUES (?,?, 'firm_page',?,'success',200,?,'text/html',100,?,?, '[]',NULL)
            """,
            (1, url, url, url, f"snapshot-{version_id}", f"{version_id}.html.gz"),
        )
        for field, value in {
            "slug": slug,
            "name": name,
            "description": description,
            "office_locations": ["Seoul"],
            "project_urls": ["https://architizer.com/projects/project-01/"],
            "social_links": {"instagram": f"https://instagram.com/{slug}"},
        }.items():
            _resolved(connection, version_id, field, value)

    # This intentionally contradicts the selected last-good metadata.  The
    # reconciliation builder must never read current_fields.
    connection.execute(
        "INSERT INTO current_fields VALUES (?,?,?,?)",
        (
            "https://architizer.com/projects/project-01/",
            "name",
            _json("CURRENT FIELDS POISON"),
            999,
        ),
    )
    if pending:
        connection.execute(
            "INSERT INTO targets(url,entity_type,status,retryable,last_good_version_id) "
            "VALUES (?,?,?,1,NULL)",
            ("https://architizer.com/projects/pending-project/", "project", "pending"),
        )
    frozen_sha = reconciliation._url_set_sha256(
        row[0] for row in connection.execute("SELECT url FROM run_targets WHERE run_id=1")
    )
    connection.execute(
        "UPDATE runs SET selected_count=(SELECT COUNT(*) FROM run_targets WHERE run_id=1),"
        "summary_json=? WHERE id=1",
        (_json({"frozen_target_urls_sha256": frozen_sha}),),
    )
    connection.commit()
    connection.close()


def _insert_project_target(
    path: Path,
    *,
    version_id: int,
    target_url: str,
    final_url: str,
    slug: str,
    project_id: int,
    global_id: str,
    snapshot_sha: str | None = None,
    firm_slug: str = "firm-new",
) -> None:
    snapshot_sha = snapshot_sha or f"snapshot-{version_id}"
    connection = sqlite3.connect(path)
    payload = {
        "status": "valid",
        "expected_slug": slug,
        "final_slug": slug,
        "canonical_slug": slug,
        "embedded_slug": slug,
        "project_id": project_id,
        "global_id": global_id,
        "errors": [],
        "missing": [],
    }
    connection.execute(
        "INSERT INTO targets(url,entity_type,status,retryable,last_good_version_id) "
        "VALUES (?,?,'done',1,?)",
        (target_url, "project", version_id),
    )
    connection.execute("INSERT INTO run_targets VALUES (1,?)", (target_url,))
    connection.execute(
        """INSERT INTO metadata_versions(
            id,run_id,target_url,entity_type,snapshot_sha256,parser_version,
            metadata_version,parsed_at,parse_status,quality,identity_status,
            identity_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            version_id,
            1,
            target_url,
            "project",
            snapshot_sha,
            "architizer-source-parser-v2.2.0",
            "architizer-source-metadata-v2.2",
            "2026-08-04T00:00:00Z",
            "complete",
            "high",
            "valid",
            _json(payload),
        ),
    )
    connection.execute(
        """
        INSERT INTO http_attempts(
            run_id,target_url,request_kind,requested_url,outcome,http_status,
            final_url,content_type,response_bytes,sha256,gzip_path,
            block_signals_json,error
        ) VALUES (?,?, 'project_page',?,'success',200,?,'text/html',100,?,?, '[]',NULL)
        """,
        (1, target_url, target_url, final_url, snapshot_sha, f"{version_id}.html.gz"),
    )
    for field, value in {
        "project_id": project_id,
        "global_id": global_id,
        "slug": slug,
        "name": f"Project {slug}",
        "firm_slug": firm_slug,
        "firm_name": "Firm New",
        "description": f"Description {slug}",
    }.items():
        _resolved(connection, version_id, field, value)
    connection.execute(
        "INSERT INTO relationships VALUES (?,?,?,?,?,?,?)",
        (
            version_id,
            "project_firm",
            "firm",
            firm_slug,
            f"https://architizer.com/firms/{firm_slug}/",
            "embedded_json",
            "observed",
        ),
    )
    frozen_sha = reconciliation._url_set_sha256(
        row[0] for row in connection.execute("SELECT url FROM run_targets WHERE run_id=1")
    )
    connection.execute(
        "UPDATE runs SET selected_count=(SELECT COUNT(*) FROM run_targets WHERE run_id=1),"
        "summary_json=? WHERE id=1",
        (_json({"frozen_target_urls_sha256": frozen_sha}),),
    )
    connection.commit()
    connection.close()


def _insert_project_reparse_recovery(path: Path) -> str:
    target_url = "https://architizer.com/projects/regression-recovered/"
    source_sha = "D" * 64
    connection = sqlite3.connect(path)
    connection.execute(
        """
        INSERT INTO targets(
            url,entity_type,status,retryable,attempt_count,next_retry_at,
            last_attempt_at,last_error,last_http_status,last_snapshot_sha256,
            last_parse_status,last_good_version_id
        ) VALUES (?,?,'failed',0,1,NULL,?,?,200,?,'no_content',NULL)
        """,
        (
            target_url,
            "project",
            "2026-08-04T00:15:00Z",
            "legacy parser returned no_content",
            source_sha,
        ),
    )
    connection.execute("INSERT INTO run_targets VALUES (1,?)", (target_url,))
    connection.execute(
        """INSERT INTO metadata_versions(
            id,run_id,target_url,entity_type,snapshot_sha256,parser_version,
            metadata_version,parsed_at,parse_status,quality,identity_status,
            identity_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            301,
            1,
            target_url,
            "project",
            source_sha,
            "architizer-source-parser-v2.2.0",
            "architizer-source-metadata-v2.2",
            "2026-08-04T00:00:00Z",
            "identity_mismatch",
            "blocked",
            "invalid",
            _json({"status": "invalid", "expected_slug": "regression-recovered"}),
        ),
    )
    connection.execute(
        "UPDATE metadata_versions SET dom_json=? WHERE id=301",
        (_json({"_canonical_url": target_url}),),
    )
    attempt = connection.execute(
        """
        INSERT INTO http_attempts(
            run_id,target_url,request_kind,requested_url,outcome,http_status,
            final_url,content_type,response_bytes,sha256,gzip_path,
            block_signals_json,error
        ) VALUES (1,?,'project_page',?,'success',200,?,'text/html',100,?,?, '[]',NULL)
        """,
        (target_url, target_url, target_url, source_sha, "301.html.gz"),
    )
    attempt_id = int(attempt.lastrowid)
    gate_summary = {
        "frozen_target_count": 1,
        "frozen_target_urls_sha256": reconciliation._url_set_sha256([target_url]),
        "gate_policy_version": reconciliation.SNAPSHOT_REPARSE_GATE_POLICY_VERSION,
        "gate_passed": True,
        "state_schema_version": reconciliation.STATE_SCHEMA_VERSION,
        "parser_version": reconciliation.CURRENT_PARSER_VERSION,
        "metadata_version": reconciliation.CURRENT_METADATA_VERSION,
    }
    connection.execute(
        "INSERT INTO runs VALUES (2,'snapshot_reparse_n10','completed',?,?,1,?)",
        (
            reconciliation.CURRENT_PARSER_VERSION,
            "2026-08-04T01:00:00Z",
            _json(gate_summary),
        ),
    )
    connection.execute("INSERT INTO run_targets VALUES (2,?)", (target_url,))
    valid_identity = {
        "status": "valid",
        "expected_slug": "regression-recovered",
        "final_slug": "regression-recovered",
        "canonical_slug": "regression-recovered",
        "embedded_slug": "regression-recovered",
        "project_id": 31,
        "global_id": "projects.project.31",
        "errors": [],
        "missing": [],
    }
    connection.execute(
        """INSERT INTO metadata_versions(
            id,run_id,target_url,entity_type,snapshot_sha256,parser_version,
            metadata_version,parsed_at,parse_status,quality,identity_status,
            identity_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            302,
            2,
            target_url,
            "project",
            source_sha,
            reconciliation.CURRENT_PARSER_VERSION,
            reconciliation.CURRENT_METADATA_VERSION,
            "2026-08-04T01:00:00Z",
            "complete",
            "high",
            "valid",
            _json(valid_identity),
        ),
    )
    connection.execute(
        "UPDATE metadata_versions SET raw_embedded_json=? WHERE id=302",
        (
            _json(
                [
                    {
                        "source": "next_data",
                        "raw": '{"value":"&quot;"}',
                        "parse_status": "parsed",
                        "parse_variant": "raw",
                        "context": {},
                    }
                ]
            ),
        ),
    )
    frozen_values = (
        2,
        target_url,
        1,
        "project",
        "project_parser_regression_recovery",
        1,
        301,
        attempt_id,
        "project_page",
        target_url,
        "success",
        200,
        "[]",
        None,
        source_sha,
        target_url,
        "text/html",
        100,
        "301.html.gz",
        "E" * 64,
        "verified",
        _json(
            {
                "status": "failed",
                "retryable": 0,
                "attempt_count": 1,
                "next_retry_at": None,
                "last_attempt_at": "2026-08-04T00:15:00Z",
                "last_error": "legacy parser returned no_content",
                "last_http_status": 200,
                "last_snapshot_sha256": source_sha,
                "last_parse_status": "no_content",
            }
        ),
        "2026-08-04T00:30:00Z",
    )
    connection.execute(
        "INSERT INTO snapshot_reparse_inputs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        frozen_values,
    )
    connection.execute(
        "INSERT INTO snapshot_reparse_lineage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            302,
            2,
            "project",
            "project_parser_regression_recovery",
            1,
            301,
            attempt_id,
            "project_page",
            target_url,
            "success",
            200,
            "[]",
            None,
            target_url,
            source_sha,
            target_url,
            "text/html",
            100,
            "301.html.gz",
            "E" * 64,
            "verified",
            "2026-08-04T01:00:00Z",
        ),
    )
    for field, value in {
        "project_id": 31,
        "global_id": "projects.project.31",
        "slug": "regression-recovered",
        "name": "Regression Recovered",
        "firm_slug": "firm-new",
        "firm_name": "Firm New",
        "description": "Recovered from exact raw snapshot.",
    }.items():
        _resolved(connection, 302, field, value)
    connection.execute(
        "INSERT INTO relationships VALUES (302,'project_firm','firm','firm-new',?, 'embedded_json','observed')",
        ("https://architizer.com/firms/firm-new/",),
    )
    connection.execute(
        "UPDATE targets SET last_good_version_id=302 WHERE url=?", (target_url,)
    )
    connection.execute(
        "INSERT INTO target_reasons VALUES (?,?,?,?,?,?,?,?)",
        (
            target_url,
            "legacy_done_row_mismatch",
            "legacy_pending_projects",
            5,
            None,
            "2026-08-04T00:00:00Z",
            "2026-08-04T01:00:00Z",
            "{}",
        ),
    )
    run_one_urls = [
        row[0]
        for row in connection.execute(
            "SELECT url FROM run_targets WHERE run_id=1 ORDER BY url"
        )
    ]
    connection.execute(
        "UPDATE runs SET selected_count=?,summary_json=? WHERE id=1",
        (
            len(run_one_urls),
            _json(
                {
                    "frozen_target_count": len(run_one_urls),
                    "frozen_target_urls_sha256": reconciliation._url_set_sha256(
                        run_one_urls
                    ),
                }
            ),
        ),
    )
    connection.commit()
    connection.close()
    return target_url


def _make_trusted_manifest(
    path: Path,
    *,
    raw: Path,
    baseline: Path,
    sidecar: Path,
) -> None:
    connection = sqlite3.connect(sidecar)
    connection.row_factory = sqlite3.Row
    referenced = connection.execute(
        """
        SELECT t.url,t.entity_type,t.last_good_version_id,m.run_id,
               m.snapshot_sha256,m.parser_version,m.metadata_version,
               m.identity_status
        FROM targets t JOIN metadata_versions m ON m.id=t.last_good_version_id
        WHERE t.last_good_version_id IS NOT NULL
        ORDER BY m.run_id,t.url
        """
    ).fetchall()
    required_runs = []
    for run_id in sorted({int(row["run_id"]) for row in referenced}):
        run = connection.execute(
            "SELECT id,run_kind,status,finished_at,parser_version,selected_count,summary_json "
            "FROM runs WHERE id=?",
            (run_id,),
        ).fetchone()
        run_urls = [
            str(row[0])
            for row in connection.execute(
                "SELECT url FROM run_targets WHERE run_id=? ORDER BY url", (run_id,)
            )
        ]
        run_referenced = [row for row in referenced if int(row["run_id"]) == run_id]
        summary = json.loads(run["summary_json"])
        reparse_gate = None
        if run["run_kind"] in reconciliation.REPARSE_RUN_KINDS:
            reparse_gate = {
                "gate_policy_version": summary["gate_policy_version"],
                "gate_passed": summary["gate_passed"],
                "state_schema_version": summary["state_schema_version"],
                "parser_version": summary["parser_version"],
                "metadata_version": summary["metadata_version"],
            }
        required_runs.append(
            {
                "id": run_id,
                "run_kind": run["run_kind"],
                "status": run["status"],
                "finished_at": run["finished_at"],
                "parser_version": run["parser_version"],
                "selected_count": run["selected_count"],
                "frozen_target_urls_sha256": reconciliation._url_set_sha256(run_urls),
                "referenced_last_good_count": len(run_referenced),
                "referenced_last_good_urls_sha256": reconciliation._url_set_sha256(
                    row["url"] for row in run_referenced
                ),
                "referenced_parser_versions": sorted(
                    {str(row["parser_version"]) for row in run_referenced}
                ),
                "referenced_metadata_versions": sorted(
                    {str(row["metadata_version"]) for row in run_referenced}
                ),
                "snapshot_reparse_gate": reparse_gate,
            }
        )
    evidence_counts = Counter(
        reconciliation._fetch_evidence_for_version(connection, dict(row))[
            "evidence_kind"
        ]
        for row in referenced
    )
    connection.close()
    payload = {
        "manifest_version": reconciliation.TRUSTED_MANIFEST_VERSION,
        "artifact_kind": "trusted_architizer_reconciliation_inputs",
        "inputs": {
            "legacy_raw": {
                "sha256": _sha256(raw),
                "size_bytes": raw.stat().st_size,
            },
            "curated_v1_3": {
                "sha256": _sha256(baseline),
                "size_bytes": baseline.stat().st_size,
            },
            "recrawl_sidecar": {
                "sha256": _sha256(sidecar),
                "size_bytes": sidecar.stat().st_size,
            },
        },
        "sidecar_contract": {
            "schema_version": reconciliation.STATE_SCHEMA_VERSION,
            "source_db_sha256": _sha256(raw),
            "source_db_size": raw.stat().st_size,
            "pending_target_count": 0,
            "active_run_count": 0,
            "done_without_last_good_count": 0,
            "invalid_last_good_link_count": 0,
            "last_good_target_count": len(referenced),
            "last_good_target_urls_sha256": reconciliation._url_set_sha256(
                row["url"] for row in referenced
            ),
            "last_good_evidence_kind_counts": dict(sorted(evidence_counts.items())),
            "required_completed_runs": required_runs,
            "parser_versions": sorted(
                {str(row["parser_version"]) for row in referenced}
            ),
            "metadata_versions": sorted(
                {str(row["metadata_version"]) for row in referenced}
            ),
            "input_integrity": {
                "quick_check": "ok",
                "foreign_key_violation_count": 0,
            },
        },
    }
    path.write_text(_json(payload) + "\n", encoding="utf-8")


class ReconciliationPolicyTests(unittest.TestCase):
    def test_architizer_entity_slug_validation_is_strict_and_type_bound(self) -> None:
        self.assertEqual(
            entity_slug_from_url(
                "https://architizer.com/projects/project-09/", "project"
            ),
            "project-09",
        )
        self.assertEqual(
            canonical_entity_url("firm", "studio.one_2~archive"),
            "https://architizer.com/firms/studio.one_2~archive/",
        )

        invalid_urls = (
            "https://architizer.com/projects/./",
            "https://architizer.com/projects/%2E%2E/",
            "https://architizer.com/projects/bad%2Fslug/",
            "https://architizer.com/projects/bad%5Cslug/",
            "https://architizer.com/projects/bad%252Fslug/",
            "https://architizer.com/projects/bad%00slug/",
            "https://architizer.com/projects/bad%FFslug/",
            "https://architizer.com/projects/bad%09slug/",
            "https://architizer.com/projects/bad%ZZslug/",
            "https://architizer.com/projects/bad%25ZZslug/",
            "https://architizer.com/projects/bad%E2%80%AEslug/",
            "https://architizer.com/projects/bad slug/",
        )
        for url in invalid_urls:
            with self.subTest(url=url):
                self.assertIsNone(entity_slug_from_url(url, "project"))

        for entity_type in ("organization", "PROJECT", "", None):
            with self.subTest(entity_type=entity_type):
                self.assertIsNone(
                    entity_slug_from_url(
                        "https://architizer.com/projects/project-09/",
                        entity_type,
                    )
                )
                with self.assertRaisesRegex(ValueError, "unsupported"):
                    canonical_entity_url(entity_type, "project-09")

        for slug in (
            ".",
            "..",
            "bad/slug",
            "bad\\slug",
            "bad%2Fslug",
            "bad%ZZslug",
            "bad\x00slug",
            "bad\u202eslug",
            "bad slug",
        ):
            with self.subTest(slug=repr(slug)):
                with self.assertRaisesRegex(ValueError, "invalid"):
                    canonical_entity_url("project", slug)

    def test_discovery_alias_rejects_path_segments_that_change_identity(self) -> None:
        self.assertEqual(
            reconciliation._discovery_slug_from_url(
                "https://architizer.com/blog/projects/project-09/", "project"
            ),
            "project-09",
        )
        invalid_aliases = (
            "https://architizer.com/blog/../projects/project-09/",
            "https://architizer.com/blog/%2E%2E/projects/project-09/",
            "https://architizer.com/blog/projects/bad%2Fslug/",
            "https://architizer.com/blog/projects/bad%255Cslug/",
            "https://architizer.com/blog/projects/bad%00slug/",
            "https://architizer.com/blog/projects/bad%FFslug/",
            "https://architizer.com/blog/projects/bad%ZZslug/",
            "https://architizer.com/blog/projects/bad%E2%80%AEslug/",
        )
        for url in invalid_aliases:
            with self.subTest(url=url):
                self.assertIsNone(
                    reconciliation._discovery_slug_from_url(url, "project")
                )
        self.assertIsNone(
            reconciliation._discovery_slug_from_url(
                "https://architizer.com/blog/projects/project-09/", "product"
            )
        )

    def test_no_clobber_update_and_identity_conflict_are_explicit(self) -> None:
        baseline = Candidate("curated_v1_3", "Old", "accepted", "baseline", {})
        missing = Candidate("recrawl_resolved", None, "missing", "none", {})
        conflict = Candidate("recrawl_resolved", None, "conflict", "review", {})
        changed = Candidate("recrawl_resolved", "New", "confirmed", "high", {})

        self.assertEqual(
            reconcile_field(
                baseline=baseline,
                recrawl=missing,
                identity_field=False,
                entity_is_new=False,
            ).value,
            "Old",
        )
        conflict_decision = reconcile_field(
            baseline=baseline,
            recrawl=conflict,
            identity_field=False,
            entity_is_new=False,
        )
        self.assertEqual(conflict_decision.decision_kind, "baseline_retained")
        self.assertEqual(conflict_decision.conflict_kind, "parser_conflict")
        self.assertEqual(
            reconcile_field(
                baseline=baseline,
                recrawl=changed,
                identity_field=False,
                entity_is_new=False,
            ).decision_kind,
            "recrawl_updated",
        )
        identity = reconcile_field(
            baseline=baseline,
            recrawl=changed,
            identity_field=True,
            entity_is_new=False,
        )
        self.assertEqual(identity.value, "Old")
        self.assertEqual(identity.conflict_kind, "identity_change")

    def test_new_project_identity_requires_all_consistent_signals(self) -> None:
        values = {
            "project_id": 9,
            "global_id": "projects.project.9",
            "slug": "project-09",
            "name": "Project 9",
            "firm_slug": "firm-new",
        }
        payload = {
            "status": "valid",
            "expected_slug": "project-09",
            "project_id": 9,
            "global_id": "projects.project.9",
        }
        self.assertEqual(
            validate_last_good_identity(
                entity_type="project",
                target_url="https://architizer.com/projects/project-09/",
                identity_status="valid",
                identity_payload=payload,
                resolved_values=values,
                relationship_slugs={"firm-new"},
            ),
            [],
        )
        issues = validate_last_good_identity(
            entity_type="project",
            target_url="https://architizer.com/projects/project-09/",
            identity_status="valid",
            identity_payload={**payload, "expected_slug": "wrong"},
            resolved_values=values,
            relationship_slugs={"other-firm"},
        )
        self.assertIn("identity_expected_slug_mismatch", issues)
        self.assertIn("project_firm_relationship_mismatch", issues)


class ReconciliationN10IntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.raw = self.root / "raw.db"
        self.baseline = self.root / "baseline.db"
        self.sidecar = self.root / "sidecar.db"
        _make_raw(self.raw)
        self.raw_sha = _sha256(self.raw)
        _make_baseline(self.baseline, self.raw_sha)
        _make_sidecar(
            self.sidecar,
            self.raw_sha,
            self.raw.stat().st_size,
            pending=True,
        )
        self.before = {
            path.name: (_sha256(path), path.stat().st_size)
            for path in (self.raw, self.baseline, self.sidecar)
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _build(self) -> tuple[dict[str, object], Path, Path]:
        output = self.root / "plan.db"
        report = self.root / "plan.md"
        result = reconciliation.build_plan(
            raw_path=self.raw,
            baseline_path=self.baseline,
            sidecar_path=self.sidecar,
            output_path=output,
            report_path=report,
            project_limit=10,
            firm_limit=3,
        )
        return result, output, report

    def test_unsupported_sidecar_entity_type_is_rejected_not_silently_firm(self) -> None:
        connection = sqlite3.connect(self.sidecar)
        target_url = connection.execute(
            "SELECT url FROM targets WHERE last_good_version_id IS NOT NULL LIMIT 1"
        ).fetchone()[0]
        connection.execute(
            "UPDATE targets SET entity_type='product' WHERE url=?", (target_url,)
        )
        connection.execute(
            "UPDATE metadata_versions SET entity_type='product' WHERE target_url=?",
            (target_url,),
        )
        connection.commit()
        connection.close()

        with self.assertRaisesRegex(
            reconciliation.ReconciliationError,
            "unsupported sidecar entity_type",
        ):
            self._build()
        self.assertFalse((self.root / "plan.db").exists())

    def test_n10_plan_uses_last_good_no_clobber_lineage_and_contract(self) -> None:
        result, output, report = self._build()
        self.assertEqual(result["selected_project_count"], 10)
        self.assertEqual(result["selected_firm_count"], 3)
        self.assertEqual(result["publication_eligibility"], "smoke_only")
        self.assertEqual(result["pending_target_count"], 1)
        self.assertTrue(report.is_file())
        ready = Path(str(output) + ".READY.json")
        self.assertTrue(ready.is_file())
        ready_payload = json.loads(ready.read_text(encoding="utf-8"))
        self.assertEqual(ready_payload["database"]["sha256"], _sha256(output))
        self.assertEqual(ready_payload["report"]["sha256"], _sha256(report))
        self.assertIn("intermediate, non-consumer artifact", report.read_text(encoding="utf-8"))

        connection = sqlite3.connect(output)
        connection.row_factory = sqlite3.Row
        project = connection.execute(
            "SELECT * FROM v_reconciled_projects WHERE slug='project-01'"
        ).fetchone()
        self.assertEqual(project["name"], "Project 1 updated")
        self.assertNotEqual(project["name"], "CURRENT FIELDS POISON")
        self.assertEqual(project["description"], "Baseline description 1")
        self.assertEqual(project["location_full"], "Seoul, South Korea")
        self.assertEqual(project["id"], 1)
        baseline_images = connection.execute(
            "SELECT gallery_image_urls,image_global_ids FROM v_reconciled_projects "
            "WHERE slug='project-03'"
        ).fetchone()
        self.assertEqual(
            json.loads(baseline_images["gallery_image_urls"]),
            ["https://curated.example/project-03/gallery.jpg"],
        )
        self.assertEqual(
            json.loads(baseline_images["image_global_ids"]),
            ["media.mediaitemattribution.curated-3"],
        )

        self.assertEqual(
            connection.execute(
                "SELECT COUNT(*) FROM v_reconciled_projects WHERE slug='project-09'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            connection.execute(
                "SELECT COUNT(*) FROM v_reconciled_projects WHERE slug='project-10'"
            ).fetchone()[0],
            0,
        )
        qa = {
            row[0]
            for row in connection.execute(
                "SELECT issue_code FROM qa_issues WHERE entity_key LIKE '%project-10/%'"
            )
        }
        self.assertIn("identity_expected_slug_mismatch", qa)

        decisions = dict(
            connection.execute(
                "SELECT field_name,decision_kind FROM field_decisions "
                "WHERE entity_key='project:https://architizer.com/projects/project-01/'"
            )
        )
        self.assertEqual(decisions["name"], "recrawl_updated")
        self.assertEqual(decisions["description"], "baseline_retained")
        self.assertEqual(decisions["location_full"], "baseline_retained")
        conflicts = {
            (row[0], row[1], row[2])
            for row in connection.execute(
                "SELECT field_name,conflict_kind,disposition FROM field_conflicts "
                "WHERE entity_key LIKE '%project-01/%'"
            )
        }
        self.assertIn(
            ("description", "parser_conflict", "baseline_retained"), conflicts
        )
        self.assertIn(
            ("name", "baseline_recrawl_difference", "recrawl_adopted_with_diff"),
            conflicts,
        )

        lineage_roles = {
            row[0]
            for row in connection.execute(
                "SELECT lineage_role FROM field_lineage "
                "WHERE entity_key LIKE '%project-01/%' AND field_name='name'"
            )
        }
        self.assertIn("selected", lineage_roles)
        self.assertIn("supporting", lineage_roles)
        description_roles = {
            row[0]
            for row in connection.execute(
                "SELECT lineage_role FROM field_lineage "
                "WHERE entity_key LIKE '%project-01/%' AND field_name='description' "
                "AND candidate_id IN (SELECT candidate_id FROM field_candidates "
                "WHERE source_role LIKE 'recrawl_%')"
            )
        }
        self.assertEqual(description_roles, {"rejected_conflict"})
        rejected_identity_roles = {
            row[0]
            for row in connection.execute(
                "SELECT lineage_role FROM field_lineage "
                "WHERE entity_key LIKE '%project-10/%' AND field_name='name' "
                "AND candidate_id IN (SELECT candidate_id FROM field_candidates "
                "WHERE source_role LIKE 'recrawl_%')"
            )
        }
        self.assertEqual(rejected_identity_roles, {"rejected_identity"})
        contract = connection.execute(
            "SELECT sql_sha256,columns_json FROM baseline_contract_objects "
            "WHERE object_type='view' AND object_name='v_contract_probe'"
        ).fetchone()
        self.assertIsNotNone(contract)
        self.assertEqual(len(contract["sql_sha256"]), 64)
        self.assertEqual(
            [item["name"] for item in json.loads(contract["columns_json"])],
            ["source_project_id", "slug", "name"],
        )
        run = connection.execute("SELECT * FROM reconciliation_runs").fetchone()
        self.assertEqual(run["baseline_schema_version"], BASELINE_SCHEMA_VERSION)
        self.assertEqual(run["artifact_kind"], "intermediate_reconciliation_plan")
        self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
        self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        connection.close()

        after = {
            path.name: (_sha256(path), path.stat().st_size)
            for path in (self.raw, self.baseline, self.sidecar)
        }
        self.assertEqual(after, self.before)

    def test_no_overwrite_and_convergence_gate(self) -> None:
        _, output, report = self._build()
        output_sha = _sha256(output)
        report_text = report.read_text(encoding="utf-8")
        ready = Path(str(output) + ".READY.json")
        ready_text = ready.read_text(encoding="utf-8")
        with self.assertRaisesRegex(reconciliation.ReconciliationError, "already exists"):
            reconciliation.build_plan(
                raw_path=self.raw,
                baseline_path=self.baseline,
                sidecar_path=self.sidecar,
                output_path=output,
                report_path=report,
                project_limit=10,
                firm_limit=3,
            )
        self.assertEqual(_sha256(output), output_sha)
        self.assertEqual(report.read_text(encoding="utf-8"), report_text)
        self.assertEqual(ready.read_text(encoding="utf-8"), ready_text)

        with self.assertRaisesRegex(
            reconciliation.ReconciliationError, "trusted input manifest"
        ):
            reconciliation.build_plan(
                raw_path=self.raw,
                baseline_path=self.baseline,
                sidecar_path=self.sidecar,
                output_path=self.root / "full-plan.db",
                report_path=self.root / "full-plan.md",
                require_converged=True,
            )
        self.assertFalse((self.root / "full-plan.db").exists())

        manifest = self.root / "pending-manifest.json"
        manifest.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(reconciliation.ReconciliationError, "not converged"):
            reconciliation.build_plan(
                raw_path=self.raw,
                baseline_path=self.baseline,
                sidecar_path=self.sidecar,
                output_path=self.root / "pending-full-plan.db",
                report_path=self.root / "pending-full-plan.md",
                trusted_manifest_path=manifest,
                require_converged=True,
            )

        with self.assertRaisesRegex(reconciliation.ReconciliationError, "path collision"):
            reconciliation.build_plan(
                raw_path=self.raw,
                baseline_path=self.baseline,
                sidecar_path=self.sidecar,
                output_path=self.root / "namespace.db",
                report_path=Path(str(self.raw) + "-wal"),
                project_limit=10,
                firm_limit=3,
            )

    def test_wrong_baseline_lineage_is_rejected_before_output(self) -> None:
        connection = sqlite3.connect(self.baseline)
        connection.execute(
            "UPDATE source_snapshots SET source_sha256_after='BAD'"
        )
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(reconciliation.ReconciliationError, "lineage mismatch"):
            reconciliation.build_plan(
                raw_path=self.raw,
                baseline_path=self.baseline,
                sidecar_path=self.sidecar,
                output_path=self.root / "bad.db",
                report_path=self.root / "bad.md",
                project_limit=10,
                firm_limit=3,
            )
        self.assertFalse((self.root / "bad.db").exists())

    def test_incomplete_v1_3_contract_is_rejected(self) -> None:
        connection = sqlite3.connect(self.baseline)
        connection.execute("DROP VIEW v_search_facets")
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(
            reconciliation.ReconciliationError, "contract is incomplete"
        ):
            reconciliation.build_plan(
                raw_path=self.raw,
                baseline_path=self.baseline,
                sidecar_path=self.sidecar,
                output_path=self.root / "incomplete.db",
                report_path=self.root / "incomplete.md",
                project_limit=10,
                firm_limit=3,
            )
        self.assertFalse((self.root / "incomplete.db").exists())

    def test_verified_blog_alias_collapses_into_one_canonical_entity(self) -> None:
        _insert_project_target(
            self.sidecar,
            version_id=111,
            target_url="https://architizer.com/blog/projects/project-09/",
            final_url="https://architizer.com/projects/project-09/",
            slug="project-09",
            project_id=9,
            global_id="projects.project.9",
        )
        result = reconciliation.build_plan(
            raw_path=self.raw,
            baseline_path=self.baseline,
            sidecar_path=self.sidecar,
            output_path=self.root / "alias.db",
            report_path=self.root / "alias.md",
            project_limit=20,
            firm_limit=3,
        )
        self.assertEqual(result["selected_project_count"], 10)
        connection = sqlite3.connect(self.root / "alias.db")
        connection.row_factory = sqlite3.Row
        self.assertEqual(
            connection.execute(
                "SELECT COUNT(*) FROM entities WHERE entity_type='project' "
                "AND source_slug='project-09'"
            ).fetchone()[0],
            1,
        )
        aliases = connection.execute(
            "SELECT target_url,alias_kind FROM entity_aliases "
            "WHERE entity_key='project:https://architizer.com/projects/project-09/' "
            "ORDER BY target_url"
        ).fetchall()
        self.assertEqual(len(aliases), 2)
        self.assertEqual(
            {row["alias_kind"] for row in aliases},
            {"canonical_target", "redirect_alias"},
        )
        evidence = json.loads(
            connection.execute(
                "SELECT identity_evidence_json FROM entities "
                "WHERE entity_key='project:https://architizer.com/projects/project-09/'"
            ).fetchone()[0]
        )
        self.assertIn(
            "alias_snapshot_disagreement_canonical_target_preferred",
            evidence["warnings"],
        )
        connection.close()

    def test_unverified_alias_is_qa_only_and_does_not_abort_plan(self) -> None:
        _insert_project_target(
            self.sidecar,
            version_id=111,
            target_url="https://architizer.com/blog/projects/unverified-alias/",
            final_url="https://architizer.com/firms/unverified-alias/",
            slug="unverified-alias",
            project_id=11,
            global_id="projects.project.11",
        )
        reconciliation.build_plan(
            raw_path=self.raw,
            baseline_path=self.baseline,
            sidecar_path=self.sidecar,
            output_path=self.root / "bad-alias.db",
            report_path=self.root / "bad-alias.md",
            project_limit=20,
            firm_limit=3,
        )
        connection = sqlite3.connect(self.root / "bad-alias.db")
        row = connection.execute(
            "SELECT inclusion_status FROM entities WHERE source_url=?",
            ("https://architizer.com/blog/projects/unverified-alias/",),
        ).fetchone()
        self.assertEqual(row[0], "qa_only")
        self.assertGreater(
            connection.execute(
                "SELECT COUNT(*) FROM qa_issues WHERE entity_key LIKE '%unverified-alias%'"
            ).fetchone()[0],
            0,
        )
        connection.close()

    def test_alias_only_valid_identity_materializes_at_canonical_url(self) -> None:
        _insert_project_target(
            self.sidecar,
            version_id=111,
            target_url="https://architizer.com/blog/projects/alias-only/",
            final_url="https://architizer.com/projects/alias-only/",
            slug="alias-only",
            project_id=11,
            global_id="projects.project.11",
        )
        reconciliation.build_plan(
            raw_path=self.raw,
            baseline_path=self.baseline,
            sidecar_path=self.sidecar,
            output_path=self.root / "alias-only.db",
            report_path=self.root / "alias-only.md",
            project_limit=20,
            firm_limit=3,
        )
        connection = sqlite3.connect(self.root / "alias-only.db")
        connection.row_factory = sqlite3.Row
        entity = connection.execute(
            "SELECT source_url,inclusion_status FROM entities WHERE source_slug='alias-only'"
        ).fetchone()
        self.assertEqual(
            entity["source_url"], "https://architizer.com/projects/alias-only/"
        )
        self.assertEqual(entity["inclusion_status"], "included")
        alias = connection.execute(
            "SELECT target_url,alias_kind FROM entity_aliases "
            "WHERE entity_key='project:https://architizer.com/projects/alias-only/'"
        ).fetchone()
        self.assertEqual(
            tuple(alias),
            (
                "https://architizer.com/blog/projects/alias-only/",
                "redirect_alias",
            ),
        )
        connection.close()

    def test_new_identity_collisions_reject_every_claimant(self) -> None:
        for version_id, slug in ((111, "collision-a"), (112, "collision-b")):
            _insert_project_target(
                self.sidecar,
                version_id=version_id,
                target_url=f"https://architizer.com/projects/{slug}/",
                final_url=f"https://architizer.com/projects/{slug}/",
                slug=slug,
                project_id=50,
                global_id="projects.project.50",
            )
        reconciliation.build_plan(
            raw_path=self.raw,
            baseline_path=self.baseline,
            sidecar_path=self.sidecar,
            output_path=self.root / "collisions.db",
            report_path=self.root / "collisions.md",
            project_limit=20,
            firm_limit=3,
        )
        connection = sqlite3.connect(self.root / "collisions.db")
        self.assertEqual(
            connection.execute(
                "SELECT COUNT(*) FROM entities WHERE source_slug LIKE 'collision-%' "
                "AND inclusion_status='qa_only'"
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            connection.execute(
                "SELECT COUNT(*) FROM qa_issues "
                "WHERE issue_code IN ('new_project_id_collides_with_recrawl',"
                "'new_global_id_collides_with_recrawl')"
            ).fetchone()[0],
            4,
        )
        connection.close()

    def test_sidecar_lock_active_run_and_input_integrity_are_hard_gates(self) -> None:
        lock = Path(str(self.sidecar) + ".lock")
        lock.write_text("occupied", encoding="utf-8")
        with self.assertRaisesRegex(reconciliation.ReconciliationError, "sidecar lock"):
            reconciliation.build_plan(
                raw_path=self.raw,
                baseline_path=self.baseline,
                sidecar_path=self.sidecar,
                output_path=self.root / "locked.db",
                report_path=self.root / "locked.md",
                project_limit=10,
                firm_limit=3,
            )
        lock.unlink()

        connection = sqlite3.connect(self.sidecar)
        connection.execute(
            "UPDATE runs SET status='running',finished_at=NULL WHERE id=1"
        )
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(reconciliation.ReconciliationError, "active/incomplete"):
            reconciliation.build_plan(
                raw_path=self.raw,
                baseline_path=self.baseline,
                sidecar_path=self.sidecar,
                output_path=self.root / "active.db",
                report_path=self.root / "active.md",
                project_limit=10,
                firm_limit=3,
            )
        connection = sqlite3.connect(self.sidecar)
        connection.execute(
            "UPDATE runs SET status='completed',finished_at='2026-08-04T00:00:00Z' WHERE id=1"
        )
        connection.commit()
        connection.close()

        with mock.patch.object(reconciliation, "_quick_check", return_value="corrupt"):
            with self.assertRaisesRegex(
                reconciliation.ReconciliationError, "input quick_check failed"
            ):
                reconciliation.build_plan(
                    raw_path=self.raw,
                    baseline_path=self.baseline,
                    sidecar_path=self.sidecar,
                    output_path=self.root / "corrupt.db",
                    report_path=self.root / "corrupt.md",
                    project_limit=10,
                    firm_limit=3,
                )
        self.assertFalse((self.root / "corrupt.db").exists())
        self.assertFalse(Path(str(self.sidecar) + ".lock").exists())

    def test_ready_is_published_last_and_failed_bundle_is_rolled_back(self) -> None:
        output = self.root / "bundle.db"
        report = self.root / "bundle.md"
        ready = Path(str(output) + ".READY.json")
        real_link = reconciliation.os.link
        call_count = 0

        def fail_ready_link(source: Path, destination: Path) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 3:
                raise OSError("fixture READY publication failure")
            real_link(source, destination)

        with mock.patch.object(reconciliation.os, "link", side_effect=fail_ready_link):
            with self.assertRaisesRegex(OSError, "READY publication failure"):
                reconciliation.build_plan(
                    raw_path=self.raw,
                    baseline_path=self.baseline,
                    sidecar_path=self.sidecar,
                    output_path=output,
                    report_path=report,
                    project_limit=10,
                    firm_limit=3,
                )
        self.assertFalse(output.exists())
        self.assertFalse(report.exists())
        self.assertFalse(ready.exists())
        self.assertFalse(Path(str(output) + ".build.lock").exists())
        self.assertFalse(Path(str(self.sidecar) + ".lock").exists())

    def test_input_drift_before_ready_rolls_back_and_lock_keeps_new_owner(self) -> None:
        output = self.root / "pre-ready-drift.db"
        report = self.root / "pre-ready-drift.md"
        ready = Path(str(output) + ".READY.json")
        original_baseline = self.baseline.read_bytes()
        real_link = reconciliation.os.link
        call_count = 0

        def mutate_after_report_link(source: Path, destination: Path) -> None:
            nonlocal call_count
            real_link(source, destination)
            call_count += 1
            if call_count == 2:
                with self.baseline.open("ab") as handle:
                    handle.write(b"drift")

        try:
            with mock.patch.object(
                reconciliation.os,
                "link",
                side_effect=mutate_after_report_link,
            ), self.assertRaisesRegex(
                reconciliation.ReconciliationError,
                "input changed before READY publication: curated_v1_3",
            ):
                reconciliation.build_plan(
                    raw_path=self.raw,
                    baseline_path=self.baseline,
                    sidecar_path=self.sidecar,
                    output_path=output,
                    report_path=report,
                    project_limit=10,
                    firm_limit=3,
                )
        finally:
            self.baseline.write_bytes(original_baseline)
        self.assertFalse(output.exists())
        self.assertFalse(report.exists())
        self.assertFalse(ready.exists())

        lock = Path(str(output) + ".build.lock")
        with reconciliation._build_lock(output):
            payload = json.loads(lock.read_text(encoding="utf-8"))
            self.assertRegex(payload["owner_token"], r"^[0-9a-f]{64}$")
            lock.unlink()
            lock.write_text("replacement-owner", encoding="utf-8")
        self.assertEqual(lock.read_text(encoding="utf-8"), "replacement-owner")
        lock.unlink()

    def test_input_drift_inside_ready_link_rolls_back_bundle(self) -> None:
        output = self.root / "post-ready-drift.db"
        report = self.root / "post-ready-drift.md"
        ready = Path(str(output) + ".READY.json")
        original_baseline = self.baseline.read_bytes()
        real_link = reconciliation.os.link
        call_count = 0

        def link_then_mutate_on_ready(source: Path, destination: Path) -> None:
            nonlocal call_count
            real_link(source, destination)
            call_count += 1
            if call_count == 3:
                with self.baseline.open("ab") as handle:
                    handle.write(b"drift")

        try:
            with mock.patch.object(
                reconciliation.os,
                "link",
                side_effect=link_then_mutate_on_ready,
            ), self.assertRaisesRegex(
                reconciliation.ReconciliationError,
                "input changed before READY publication: curated_v1_3",
            ):
                reconciliation.build_plan(
                    raw_path=self.raw,
                    baseline_path=self.baseline,
                    sidecar_path=self.sidecar,
                    output_path=output,
                    report_path=report,
                    project_limit=10,
                    firm_limit=3,
                )
        finally:
            self.baseline.write_bytes(original_baseline)
        self.assertFalse(output.exists())
        self.assertFalse(report.exists())
        self.assertFalse(ready.exists())
        self.assertFalse(Path(str(output) + ".build.lock").exists())
        self.assertFalse(Path(str(self.sidecar) + ".lock").exists())

    def test_trusted_manifest_is_required_and_bound_for_full_eligibility(self) -> None:
        connection = sqlite3.connect(self.sidecar)
        connection.execute("DELETE FROM targets WHERE status='pending'")
        connection.commit()
        connection.close()

    def test_trusted_manifest_run_contract_tamper_is_rejected(self) -> None:
        connection = sqlite3.connect(self.sidecar)
        connection.execute("DELETE FROM targets WHERE status='pending'")
        connection.commit()
        connection.close()
        manifest = self.root / "trusted-run-contract.json"
        _make_trusted_manifest(
            manifest,
            raw=self.raw,
            baseline=self.baseline,
            sidecar=self.sidecar,
        )
        original = json.loads(manifest.read_text(encoding="utf-8"))
        input_before = {
            "legacy_raw": {
                "sha256": _sha256(self.raw),
                "size_bytes": self.raw.stat().st_size,
                "path_label": "raw.db",
            },
            "curated_v1_3": {
                "sha256": _sha256(self.baseline),
                "size_bytes": self.baseline.stat().st_size,
                "path_label": "baseline.db",
            },
            "recrawl_sidecar": {
                "sha256": _sha256(self.sidecar),
                "size_bytes": self.sidecar.stat().st_size,
                "path_label": "sidecar.db",
            },
        }
        sidecar = sqlite3.connect(self.sidecar)
        sidecar.row_factory = sqlite3.Row
        sidecar_meta = dict(sidecar.execute("SELECT key,value FROM state_meta"))
        fixed_values = {
            "FIXED_RAW_SHA256": _sha256(self.raw),
            "FIXED_RAW_SIZE_BYTES": self.raw.stat().st_size,
            "FIXED_BASELINE_SHA256": _sha256(self.baseline),
            "FIXED_BASELINE_SIZE_BYTES": self.baseline.stat().st_size,
        }
        cases = (
            ("run_kind", "wrong", "run_kind mismatch"),
            ("finished_at", "wrong", "finished_at mismatch"),
            ("parser_version", "wrong", "parser_version mismatch"),
            ("selected_count", 999, "selected_count mismatch"),
            (
                "frozen_target_urls_sha256",
                "0" * 64,
                "frozen URL SHA mismatch",
            ),
            (
                "referenced_last_good_count",
                999,
                "referenced last-good count mismatch",
            ),
            (
                "referenced_last_good_urls_sha256",
                "0" * 64,
                "referenced last-good URL SHA mismatch",
            ),
            (
                "referenced_parser_versions",
                ["wrong"],
                "referenced_parser_versions mismatch",
            ),
            (
                "referenced_metadata_versions",
                ["wrong"],
                "referenced_metadata_versions mismatch",
            ),
        )
        try:
            with mock.patch.multiple(reconciliation, **fixed_values):
                for key, value, message in cases:
                    with self.subTest(field=key):
                        payload = json.loads(_json(original))
                        payload["sidecar_contract"]["required_completed_runs"][0][
                            key
                        ] = value
                        manifest.write_text(_json(payload) + "\n", encoding="utf-8")
                        with self.assertRaisesRegex(
                            reconciliation.ReconciliationError, message
                        ):
                            reconciliation._validate_trusted_manifest(
                                manifest_path=manifest,
                                input_before=input_before,
                                sidecar=sidecar,
                                sidecar_meta=sidecar_meta,
                            )
        finally:
            sidecar.close()
        manifest = self.root / "trusted.json"
        _make_trusted_manifest(
            manifest,
            raw=self.raw,
            baseline=self.baseline,
            sidecar=self.sidecar,
        )
        fixed_values = {
            "FIXED_RAW_SHA256": _sha256(self.raw),
            "FIXED_RAW_SIZE_BYTES": self.raw.stat().st_size,
            "FIXED_BASELINE_SHA256": _sha256(self.baseline),
            "FIXED_BASELINE_SIZE_BYTES": self.baseline.stat().st_size,
        }
        with mock.patch.multiple(reconciliation, **fixed_values):
            result = reconciliation.build_plan(
                raw_path=self.raw,
                baseline_path=self.baseline,
                sidecar_path=self.sidecar,
                output_path=self.root / "eligible.db",
                report_path=self.root / "eligible.md",
                trusted_manifest_path=manifest,
                require_converged=True,
            )
        self.assertEqual(
            result["publication_eligibility"], "eligible_materialization_input"
        )
        ready = json.loads(
            Path(str(self.root / "eligible.db") + ".READY.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(ready["trusted_manifest"]["sha256"], _sha256(manifest))
        connection = sqlite3.connect(self.root / "eligible.db")
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM trusted_input_manifest").fetchone()[0],
            1,
        )
        connection.close()

    def test_unlimited_library_plan_requires_explicit_convergence(self) -> None:
        with self.assertRaisesRegex(
            reconciliation.ReconciliationError, "explicit converged/full"
        ):
            reconciliation.build_plan(
                raw_path=self.raw,
                baseline_path=self.baseline,
                sidecar_path=self.sidecar,
                output_path=self.root / "unconfirmed.db",
                report_path=self.root / "unconfirmed.md",
            )

    def test_full_manifest_rejects_nonfixed_or_tampered_inputs(self) -> None:
        connection = sqlite3.connect(self.sidecar)
        connection.execute("DELETE FROM targets WHERE status='pending'")
        connection.commit()
        connection.close()
        manifest = self.root / "trusted-bad.json"
        _make_trusted_manifest(
            manifest,
            raw=self.raw,
            baseline=self.baseline,
            sidecar=self.sidecar,
        )
        with self.assertRaisesRegex(
            reconciliation.ReconciliationError, "not the fixed production input"
        ):
            reconciliation.build_plan(
                raw_path=self.raw,
                baseline_path=self.baseline,
                sidecar_path=self.sidecar,
                output_path=self.root / "nonfixed.db",
                report_path=self.root / "nonfixed.md",
                trusted_manifest_path=manifest,
                require_converged=True,
            )

        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["inputs"]["recrawl_sidecar"]["sha256"] = "0" * 64
        manifest.write_text(_json(payload) + "\n", encoding="utf-8")
        fixed_values = {
            "FIXED_RAW_SHA256": _sha256(self.raw),
            "FIXED_RAW_SIZE_BYTES": self.raw.stat().st_size,
            "FIXED_BASELINE_SHA256": _sha256(self.baseline),
            "FIXED_BASELINE_SIZE_BYTES": self.baseline.stat().st_size,
        }
        with mock.patch.multiple(reconciliation, **fixed_values):
            with self.assertRaisesRegex(
                reconciliation.ReconciliationError,
                "trusted manifest SHA mismatch: recrawl_sidecar",
            ):
                reconciliation.build_plan(
                    raw_path=self.raw,
                    baseline_path=self.baseline,
                    sidecar_path=self.sidecar,
                    output_path=self.root / "tampered.db",
                    report_path=self.root / "tampered.md",
                    trusted_manifest_path=manifest,
                    require_converged=True,
                )

    def test_limited_smoke_is_stratified(self) -> None:
        reconciliation.build_plan(
            raw_path=self.raw,
            baseline_path=self.baseline,
            sidecar_path=self.sidecar,
            output_path=self.root / "stratified.db",
            report_path=self.root / "stratified.md",
            project_limit=4,
            firm_limit=1,
        )
        connection = sqlite3.connect(self.root / "stratified.db")
        origins = {
            row[0] for row in connection.execute("SELECT DISTINCT origin FROM entities")
        }
        self.assertEqual(
            origins, {"baseline_only", "baseline_recrawled", "recrawl_new"}
        )
        self.assertGreater(
            connection.execute("SELECT COUNT(*) FROM qa_issues").fetchone()[0], 0
        )
        connection.close()

    def test_repeat_plan_is_logically_and_byte_deterministic(self) -> None:
        first = reconciliation.build_plan(
            raw_path=self.raw,
            baseline_path=self.baseline,
            sidecar_path=self.sidecar,
            output_path=self.root / "first.db",
            report_path=self.root / "first.md",
            project_limit=10,
            firm_limit=3,
        )
        second = reconciliation.build_plan(
            raw_path=self.raw,
            baseline_path=self.baseline,
            sidecar_path=self.sidecar,
            output_path=self.root / "second.db",
            report_path=self.root / "second.md",
            project_limit=10,
            firm_limit=3,
        )
        self.assertEqual(first["reconciliation_id"], second["reconciliation_id"])
        self.assertEqual(first["logical_sha256"], second["logical_sha256"])
        self.assertEqual(first["database_sha256"], second["database_sha256"])
        first_ready = json.loads(
            Path(str(self.root / "first.db") + ".READY.json").read_text(
                encoding="utf-8"
            )
        )
        second_ready = json.loads(
            Path(str(self.root / "second.db") + ".READY.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(first_ready["database"]["sha256"], second_ready["database"]["sha256"])
        self.assertEqual(first_ready["database"]["logical_sha256"], second_ready["database"]["logical_sha256"])
        self.assertNotEqual(first_ready["database"]["path"], second_ready["database"]["path"])

    def test_source_reasons_and_snapshot_reparse_recovery_are_preserved(self) -> None:
        _insert_project_target(
            self.sidecar,
            version_id=111,
            target_url="https://architizer.com/projects/project-11/",
            final_url="https://architizer.com/projects/project-11/",
            slug="project-11",
            project_id=11,
            global_id="projects.project.11",
        )
        _insert_project_target(
            self.sidecar,
            version_id=112,
            target_url="https://architizer.com/projects/project-12/",
            final_url="https://architizer.com/projects/project-12/",
            slug="project-12",
            project_id=12,
            global_id="projects.project.12",
        )
        recovered_reparse_url = _insert_project_reparse_recovery(self.sidecar)
        connection = sqlite3.connect(self.sidecar)
        connection.execute("DELETE FROM targets WHERE status='pending'")
        connection.execute(
            "UPDATE targets SET status='failed',retryable=0 "
            "WHERE url='https://architizer.com/projects/project-12/'"
        )
        for url in (
            "https://architizer.com/projects/project-01/",
            "https://architizer.com/projects/project-09/",
            "https://architizer.com/projects/project-11/",
            "https://architizer.com/projects/project-12/",
        ):
            connection.execute(
                "INSERT INTO target_reasons VALUES (?,?,?,?,?,?,?,?)",
                (
                    url,
                    "legacy_failed_retry",
                    "legacy_pending_projects",
                    10,
                    None,
                    "2026-08-04T00:00:00Z",
                    "2026-08-04T01:00:00Z",
                    "{}",
                ),
            )
        terminal_url = "https://architizer.com/projects/terminal-mismatch/"
        connection.execute(
            "INSERT INTO targets(url,entity_type,status,retryable,last_good_version_id) "
            "VALUES (?,?,'failed',0,NULL)",
            (terminal_url, "project"),
        )
        connection.execute(
            "INSERT INTO target_reasons VALUES (?,?,?,?,?,?,?,?)",
            (
                terminal_url,
                "legacy_done_row_mismatch",
                "legacy_pending_projects",
                20,
                None,
                "2026-08-04T00:00:00Z",
                "2026-08-04T01:00:00Z",
                "{}",
            ),
        )
        connection.commit()
        connection.close()
        trusted = self.root / "trusted-recovery.json"
        _make_trusted_manifest(
            trusted,
            raw=self.raw,
            baseline=self.baseline,
            sidecar=self.sidecar,
        )
        fixed_values = {
            "FIXED_RAW_SHA256": _sha256(self.raw),
            "FIXED_RAW_SIZE_BYTES": self.raw.stat().st_size,
            "FIXED_BASELINE_SHA256": _sha256(self.baseline),
            "FIXED_BASELINE_SIZE_BYTES": self.baseline.stat().st_size,
        }
        with mock.patch.multiple(reconciliation, **fixed_values):
            result = reconciliation.build_plan(
                raw_path=self.raw,
                baseline_path=self.baseline,
                sidecar_path=self.sidecar,
                output_path=self.root / "recovery.db",
                report_path=self.root / "recovery.md",
                trusted_manifest_path=trusted,
                require_converged=True,
            )
        metrics = result["metrics"]["source_recovery_counts"]
        self.assertEqual(
            metrics["recovered_legacy_failed_retry_valid_included_count"], 3
        )
        self.assertEqual(
            metrics["unrecovered_legacy_done_row_mismatch_terminal_count"], 1
        )
        self.assertEqual(
            metrics["recovered_project_parser_regression_reparse_count"], 1
        )
        output = sqlite3.connect(self.root / "recovery.db")
        output.row_factory = sqlite3.Row
        try:
            self.assertEqual(
                output.execute(
                    "SELECT COUNT(*) FROM source_target_reasons"
                ).fetchone()[0],
                6,
            )
            evidence = json.loads(
                output.execute(
                    "SELECT identity_evidence_json FROM entities WHERE source_url=?",
                    (recovered_reparse_url,),
                ).fetchone()[0]
            )
        finally:
            output.close()
        self.assertEqual(
            evidence["fetch_evidence"]["selection_kind"],
            "project_parser_regression_recovery",
        )
        report = (self.root / "recovery.md").read_text(encoding="utf-8")
        self.assertIn("Source recovery definitions and counts", report)

    def test_snapshot_reparse_lineage_mismatch_is_rejected(self) -> None:
        _insert_project_reparse_recovery(self.sidecar)
        connection = sqlite3.connect(self.sidecar)
        connection.execute(
            "UPDATE snapshot_reparse_lineage SET final_url=? WHERE reparse_version_id=302",
            ("https://architizer.com/projects/wrong/",),
        )
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(
            reconciliation.ReconciliationError, "lineage/input mismatch"
        ):
            reconciliation.build_plan(
                raw_path=self.raw,
                baseline_path=self.baseline,
                sidecar_path=self.sidecar,
                output_path=self.root / "bad-reparse.db",
                report_path=self.root / "bad-reparse.md",
                project_limit=20,
                firm_limit=3,
            )

    def test_snapshot_reparse_cannot_promote_a_stale_success_snapshot(self) -> None:
        target_url = _insert_project_reparse_recovery(self.sidecar)
        connection = sqlite3.connect(self.sidecar)
        stale_replacement = "F" * 64
        connection.execute(
            "UPDATE targets SET last_snapshot_sha256=? WHERE url=?",
            (stale_replacement, target_url),
        )
        current_state = {
            "status": "failed",
            "retryable": 0,
            "attempt_count": 1,
            "next_retry_at": None,
            "last_attempt_at": "2026-08-04T00:15:00Z",
            "last_error": "legacy parser returned no_content",
            "last_http_status": 200,
            "last_snapshot_sha256": stale_replacement,
            "last_parse_status": "no_content",
        }
        # Even if a corrupt producer rewrites its frozen-state assertion to
        # match the target row, reconciliation independently binds the current
        # last snapshot to the source metadata/HTTP evidence.
        connection.execute(
            "UPDATE snapshot_reparse_inputs SET target_network_state_json=? "
            "WHERE run_id=2 AND target_url=?",
            (_json(current_state), target_url),
        )
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(
            reconciliation.ReconciliationError,
            "selection/source identity mismatch",
        ):
            reconciliation.build_plan(
                raw_path=self.raw,
                baseline_path=self.baseline,
                sidecar_path=self.sidecar,
                output_path=self.root / "stale-reparse.db",
                report_path=self.root / "stale-reparse.md",
                project_limit=20,
                firm_limit=3,
            )

    def test_smoke_default_paths_and_full_resource_preflight_fail_early(self) -> None:
        with self.assertRaisesRegex(
            reconciliation.ReconciliationError, "explicit non-production"
        ):
            reconciliation.build_plan(
                raw_path=self.raw,
                baseline_path=self.baseline,
                sidecar_path=self.sidecar,
                output_path=reconciliation.DEFAULT_OUTPUT_DB,
                report_path=self.root / "smoke.md",
                project_limit=10,
                firm_limit=3,
            )
        with mock.patch.object(
            reconciliation.shutil,
            "disk_usage",
            return_value=mock.Mock(free=1),
        ):
            with self.assertRaisesRegex(
                reconciliation.ReconciliationError, "disk preflight failed"
            ):
                reconciliation._preflight_full_resources(
                    raw_path=self.raw,
                    baseline_path=self.baseline,
                    sidecar_path=self.sidecar,
                    output_path=self.root / "preflight.db",
                )


if __name__ == "__main__":
    unittest.main()
