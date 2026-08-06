"""Offline integration tests for the final Architizer curated-v2 materializer."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from canonical.architizer_curated_v2 import READY_VERSION, SCHEMA_VERSION
from crawl.architizer import awards_store_v2
from tools import build_architizer_curated as curated_v1
from tools import build_architizer_curated_v2 as curated_v2
from tools import reconcile_architizer_curated_v2 as reconciliation_tool


SOURCE_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE architizer_projects (
    id INTEGER PRIMARY KEY, global_id TEXT UNIQUE, slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL, firm_slug TEXT, firm_name TEXT, description TEXT,
    description_short TEXT, completion_year INTEGER, building_size_slug TEXT,
    building_size_display TEXT, constr_status TEXT, budget REAL,
    location_full TEXT, location_country TEXT, location_city TEXT,
    categories TEXT, cover_image_url TEXT, gallery_image_urls TEXT,
    image_global_ids TEXT, published_time TEXT, modified_time TEXT,
    fetched_at TEXT
);
CREATE TABLE architizer_firms (
    slug TEXT PRIMARY KEY, name TEXT NOT NULL, office_locations TEXT,
    description TEXT, awards_summary TEXT, project_count_seen INTEGER DEFAULT 0,
    social_links TEXT, fetched_at TEXT
);
CREATE TABLE architizer_awards (
    id INTEGER PRIMARY KEY AUTOINCREMENT, award_year INTEGER NOT NULL,
    award_track TEXT NOT NULL, award_category TEXT, award_tier TEXT NOT NULL,
    project_slug TEXT, firm_slug TEXT, source_url TEXT NOT NULL, fetched_at TEXT
);
CREATE TABLE pending_projects (
    url TEXT PRIMARY KEY, source_url TEXT, lastmod TEXT, status TEXT DEFAULT 'pending',
    discovered_at TEXT, fetched_at TEXT, error TEXT
);
CREATE TABLE pending_firms (
    url TEXT PRIMARY KEY, source_url TEXT, lastmod TEXT, status TEXT DEFAULT 'pending',
    discovered_at TEXT, fetched_at TEXT, error TEXT
);
"""


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def create_raw(path: Path, project_count: int = 120) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SOURCE_SCHEMA)
        connection.execute(
            "INSERT INTO architizer_firms VALUES (?,?,?,?,?,?,?,?)",
            (
                "studio-one",
                "Studio One",
                '["Seoul, South Korea"]',
                "Baseline firm description.",
                "Winner (1)",
                project_count,
                '{"instagram":"https://instagram.com/studio-one"}',
                "2026-07-30 12:00:00",
            ),
        )
        for project_id in range(1, project_count + 1):
            slug = f"project-{project_id:03d}"
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
                    f"Project {project_id:03d}",
                    "studio-one",
                    "Studio One",
                    f"Description {project_id}",
                    f"Short {project_id}",
                    2000 + (project_id % 25),
                    "sqft_10_25",
                    "10,000 sqft - 25,000 sqft",
                    "built",
                    float(project_id * 1000),
                    "Seoul, South Korea",
                    "South Korea",
                    "Seoul",
                    '["Cultural","Museum"]',
                    f"https://architizer-prod.imgix.net/media/{slug}.jpg?w=1680",
                    f'["https://architizer-prod.imgix.net/media/{slug}-2.jpg?w=900"]',
                    f'["media.mediaitemattribution.{project_id}"]',
                    "2024-01-01T00:00:00Z",
                    "2026-07-30T00:00:00Z",
                    "2026-07-30 12:00:00",
                ),
            )
            connection.execute(
                "INSERT INTO pending_projects VALUES (?,?,?,'done',?,?,NULL)",
                (
                    f"https://architizer.com/projects/{slug}/",
                    "https://architizer.com/sitemap-projects.xml",
                    "2026-07-30",
                    "2026-07-30 10:00:00",
                    "2026-07-30 12:00:00",
                ),
            )
        connection.execute(
            "INSERT INTO pending_firms VALUES (?,?,?,'done',?,?,NULL)",
            (
                "https://architizer.com/firms/studio-one/",
                "https://architizer.com/sitemap-firms.xml",
                "2026-07-30",
                "2026-07-30 10:00:00",
                "2026-07-30 12:00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO architizer_awards(
                award_year,award_track,award_category,award_tier,
                project_slug,firm_slug,source_url,fetched_at
            ) VALUES (2025,'Typology','Cultural > Museum','Jury',
                      'project-001',NULL,
                      'https://winners.architizer.com/2025/Typology/',
                      '2026-07-30 12:00:00')
            """
        )
        connection.commit()
    finally:
        connection.close()


def create_baseline(raw: Path, baseline: Path) -> None:
    curated_v1.materialize_database(
        source_path=raw,
        target_path=baseline,
        limit=None,
        expected_sha256=sha256(raw),
        expected_size=raw.stat().st_size,
    )


def raw_project(connection: sqlite3.Connection, project_id: int) -> dict:
    connection.row_factory = sqlite3.Row
    return dict(
        connection.execute(
            "SELECT * FROM architizer_projects WHERE id=?", (project_id,)
        ).fetchone()
    )


def create_reconciliation_bundle(
    raw: Path,
    baseline: Path,
    database: Path,
    report: Path,
    ready: Path,
    *,
    qa_only_project_ids: frozenset[int] = frozenset(),
) -> None:
    raw_connection = sqlite3.connect(raw)
    raw_connection.row_factory = sqlite3.Row
    baseline_connection = sqlite3.connect(baseline)
    baseline_connection.row_factory = sqlite3.Row
    output = sqlite3.connect(database)
    output.row_factory = sqlite3.Row
    trusted_manifest_payload = {
        "manifest_version": reconciliation_tool.TRUSTED_MANIFEST_VERSION,
        "artifact_kind": "trusted_architizer_reconciliation_inputs",
        "inputs": {
            "legacy_raw": {
                "sha256": sha256(raw),
                "size_bytes": raw.stat().st_size,
            },
            "curated_v1_3": {
                "sha256": sha256(baseline),
                "size_bytes": baseline.stat().st_size,
            },
            "recrawl_sidecar": {"sha256": "A" * 64, "size_bytes": 1234},
        },
        "sidecar_contract": {
            "schema_version": curated_v2.STATE_SCHEMA_VERSION,
            "source_db_sha256": sha256(raw),
            "source_db_size": raw.stat().st_size,
            "pending_target_count": 0,
            "active_run_count": 0,
            "done_without_last_good_count": 0,
            "invalid_last_good_link_count": 0,
            "last_good_target_count": 121,
            "last_good_target_urls_sha256": "E" * 64,
            "parser_versions": ["parser-v2"],
            "metadata_versions": ["meta-v2"],
            "required_completed_runs": [],
            "input_integrity": {
                "quick_check": "ok",
                "foreign_key_violation_count": 0,
            },
        },
    }
    trusted_manifest_bytes = (
        json.dumps(
            trusted_manifest_payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    trusted_manifest_sha = hashlib.sha256(trusted_manifest_bytes).hexdigest().upper()
    try:
        output.executescript(reconciliation_tool.PLAN_DDL)
        reconciliation_id = "atzrecon_fixture_full"
        output.execute(
            """
            INSERT INTO reconciliation_runs VALUES (
                ?,?,?,?,?,'architizer-curated-schema-v2.0',
                'intermediate_reconciliation_plan','eligible_materialization_input',
                NULL,NULL,120,1,0,'2026-08-03T12:00:00+00:00','{}'
            )
            """,
            (
                reconciliation_id,
                reconciliation_tool.RECONCILIATION_TOOL_VERSION,
                reconciliation_tool.RECONCILIATION_SCHEMA_VERSION,
                reconciliation_tool.RECONCILIATION_POLICY_VERSION,
                curated_v1.SCHEMA_VERSION,
            ),
        )
        for role, path in (
            ("legacy_raw", raw),
            ("curated_v1_3", baseline),
        ):
            output.execute(
                "INSERT INTO input_snapshots VALUES (?,?,?,?,?,?,1,'ok',0,'{}')",
                (
                    reconciliation_id,
                    role,
                    path.name,
                    sha256(path),
                    sha256(path),
                    path.stat().st_size,
                ),
            )
        output.execute(
            "INSERT INTO input_snapshots VALUES (?,?,?,?,?,?,1,'ok',0,?)",
            (
                reconciliation_id,
                "recrawl_sidecar",
                "fixture-sidecar.db",
                "A" * 64,
                "A" * 64,
                1234,
                json.dumps(
                    {
                        "schema_version": curated_v2.STATE_SCHEMA_VERSION,
                        "source_db_sha256": sha256(raw),
                        "source_db_size": raw.stat().st_size,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )
        output.execute(
            "INSERT INTO trusted_input_manifest VALUES (?,?,?,?,?,?)",
            (
                reconciliation_id,
                "architizer-reconciliation-input-manifest-v1",
                "fixture-manifest.json",
                trusted_manifest_sha,
                len(trusted_manifest_bytes),
                json.dumps(
                    trusted_manifest_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )
        for (object_type, object_name), item in curated_v2.contract_objects(
            baseline_connection
        ).items():
            output.execute(
                "INSERT INTO baseline_contract_objects VALUES (?,?,?,?,?)",
                (
                    object_type,
                    object_name,
                    item["sql_sha256"],
                    item["sql"],
                    item["columns_json"],
                ),
            )
        firm_fields = {
            "slug": "studio-one",
            "name": "Studio One",
            "office_locations": ["Seoul, South Korea"],
            "description": "Updated firm description.",
            "awards_summary": "Winner (1)",
            "project_count_seen": 120,
            "project_urls": [],
            "social_links": {"instagram": "https://instagram.com/studio-one"},
            "fetched_at": "2026-08-03T12:00:00+00:00",
        }
        output.execute(
            """
            INSERT INTO entities VALUES (
                'firm:studio-one','firm',?,'studio-one','baseline_recrawled',
                1,'studio-one','accepted',1,?, 'parser-v2','meta-v2',
                'verified','included','{}',?
            )
            """,
            (
                "https://architizer.com/firms/studio-one/",
                "E" * 64,
                json.dumps(firm_fields, sort_keys=True),
            ),
        )
        for project_id in range(1, 121):
            project = raw_project(raw_connection, project_id)
            slug = project["slug"]
            fields = {
                "project_id": project_id,
                "global_id": project["global_id"],
                "slug": slug,
                "name": "Updated Project 001" if project_id == 1 else project["name"],
                "firm_slug": project["firm_slug"],
                "firm_name": project["firm_name"],
                "description": project["description"],
                "description_short": project["description_short"],
                "completion_year": project["completion_year"],
                "building_size_slug": project["building_size_slug"],
                "building_size_display": project["building_size_display"],
                "construction_status": project["constr_status"],
                "budget": project["budget"],
                "location_full": project["location_full"],
                "location_country": project["location_country"],
                "location_city": project["location_city"],
                "categories": json.loads(project["categories"]),
                "cover_image_url": project["cover_image_url"],
                "gallery_image_urls": json.loads(project["gallery_image_urls"]),
                "image_global_ids": json.loads(project["image_global_ids"]),
                "published_time": project["published_time"],
                "modified_time": project["modified_time"],
                "fetched_at": "2026-08-03T12:00:00+00:00",
            }
            entity_key = f"project:{slug}"
            source_url = f"https://architizer.com/projects/{slug}/"
            identity_status = (
                "invalid" if project_id in qa_only_project_ids else "verified"
            )
            inclusion_status = (
                "qa_only" if project_id in qa_only_project_ids else "included"
            )
            output.execute(
                """
                INSERT INTO entities VALUES (
                    ?, 'project',?,?, 'baseline_recrawled',1,?,'accepted',?, ?,
                    'parser-v2','meta-v2',?,?,'{}',?
                )
                """,
                (
                    entity_key,
                    source_url,
                    slug,
                    str(project_id),
                    project_id,
                    "F" * 64,
                    identity_status,
                    inclusion_status,
                    json.dumps(fields, sort_keys=True),
                ),
            )
        for project_id in (1, 2, 3):
            slug = f"project-{project_id:03d}"
            url = f"https://architizer.com/projects/{slug}/"
            output.execute(
                "INSERT INTO source_target_reasons VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    url,
                    f"project:{slug}",
                    "project",
                    "done",
                    0,
                    project_id,
                    "legacy_failed_retry",
                    "legacy_pending_projects",
                    10,
                    None,
                    "2026-08-01T00:00:00+00:00",
                    "2026-08-03T12:00:00+00:00",
                    "{}",
                ),
            )
        output.execute(
            "INSERT INTO source_target_reasons VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "https://architizer.com/projects/legacy-mismatch/",
                None,
                "project",
                "failed",
                0,
                None,
                "legacy_done_row_mismatch",
                "legacy_pending_projects",
                20,
                None,
                "2026-08-01T00:00:00+00:00",
                "2026-08-03T12:00:00+00:00",
                "{}",
            ),
        )
        output.execute(
            "INSERT INTO entity_aliases VALUES (?,?,?,?,?,?)",
            (
                "project:project-001",
                "https://architizer.com/projects/old-project-001/",
                "https://architizer.com/projects/project-001/",
                1,
                "redirect_alias",
                '{"fixture":true}',
            ),
        )
        output.execute(
            "INSERT INTO field_candidates VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "candidate-baseline-2",
                "project:project-002",
                "name",
                "curated_v1_3",
                '"Project 002"',
                "confirmed",
                "baseline",
                None,
                '{"table":"source_projects"}',
            ),
        )
        output.execute(
            "INSERT INTO field_candidates VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "candidate-conflict-2",
                "project:project-002",
                "name",
                "recrawl_resolved",
                '"Conflicting Name"',
                "conflict",
                "conflict",
                2,
                '{"table":"field_values"}',
            ),
        )
        output.execute(
            "INSERT INTO field_decisions VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "project:project-002",
                "name",
                '"Project 002"',
                "baseline_retained",
                "conflict_preserved",
                "candidate-baseline-2",
                "candidate-baseline-2",
                "candidate-conflict-2",
                "fixture.no-clobber",
            ),
        )
        output.execute(
            "INSERT INTO field_lineage VALUES (?,?,?,?)",
            (
                "project:project-002",
                "name",
                "candidate-baseline-2",
                "selected",
            ),
        )
        output.execute(
            "INSERT INTO field_lineage VALUES (?,?,?,?)",
            (
                "project:project-002",
                "name",
                "candidate-conflict-2",
                "rejected_conflict",
            ),
        )
        output.execute(
            "INSERT INTO field_conflicts VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "conflict-2-name",
                "project:project-002",
                "name",
                "parser_conflict",
                '"Project 002"',
                '"Conflicting Name"',
                "baseline_retained",
                '{"fixture":true}',
                "fixture.no-clobber",
            ),
        )
        output.execute("INSERT INTO reconciliation_metrics VALUES ('fixture','true')")
        output.execute(
            "INSERT INTO reconciliation_metrics VALUES ('source_recovery_counts',?)",
            (
                json.dumps(
                    {
                        "new_included_project_count": 0,
                        "baseline_project_recrawl_updated_or_filled_count": 1,
                        "recovered_legacy_failed_retry_valid_included_count": 3,
                        "unrecovered_legacy_done_row_mismatch_terminal_count": 1,
                        "definitions": {},
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )
        output.commit()
        output.execute("VACUUM")
        logical_sha = reconciliation_tool._database_logical_sha(output)
    finally:
        output.close()
        baseline_connection.close()
        raw_connection.close()
    report.write_text("fixture reconciliation report\n", encoding="utf-8")
    ready_payload = {
        "artifact_kind": "architizer_reconciliation_plan",
        "ready_version": reconciliation_tool.RECONCILIATION_READY_VERSION,
        "tool_version": reconciliation_tool.RECONCILIATION_TOOL_VERSION,
        "plan_schema_version": reconciliation_tool.RECONCILIATION_SCHEMA_VERSION,
        "policy_version": reconciliation_tool.RECONCILIATION_POLICY_VERSION,
        "baseline_schema_version": curated_v1.SCHEMA_VERSION,
        "final_target_schema_version": reconciliation_tool.FINAL_TARGET_SCHEMA_VERSION,
        "publication_eligibility": "eligible_materialization_input",
        "reconciliation_id": "atzrecon_fixture_full",
        "project_limit": None,
        "firm_limit": None,
        "selected_project_count": 120,
        "selected_firm_count": 1,
        "pending_target_count": 0,
        "database": {
            "path": database.name,
            "sha256": sha256(database),
            "logical_sha256": logical_sha,
            "size_bytes": database.stat().st_size,
        },
        "report": {
            "path": report.name,
            "sha256": sha256(report),
            "size_bytes": report.stat().st_size,
        },
        "trusted_manifest": {
            "path": "fixture-manifest.json",
            "manifest_version": reconciliation_tool.TRUSTED_MANIFEST_VERSION,
            "sha256": trusted_manifest_sha,
            "size_bytes": len(trusted_manifest_bytes),
        },
        "validation": {
            "inputs": {},
            "output": {
                "quick_check": "ok",
                "foreign_key_violation_count": 0,
            },
        },
    }
    ready.write_text(
        json.dumps(ready_payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def create_awards(
    path: Path,
    raw: Path,
    count: int = 120,
    *,
    sidecar_sha: str = "A" * 64,
) -> None:
    tracks = ["Firm", "Plus", "Products", "Sustainability", "Typology"]
    root_url = "https://winners.architizer.com/2026/"
    track_counts = Counter(
        tracks[(index - 1) % len(tracks)]
        for index in range(1, count + 1)
    )
    discovery_counts = {
        track: {
            "project": track_counts[track]
            if track in {"Plus", "Sustainability", "Typology"}
            else 0,
            "firm": 1
            if track in {"Firm", "Plus", "Sustainability", "Typology"}
            and track_counts[track]
            else 0,
        }
        for track in tracks
    }
    run_summary = {
        "award_year": 2026,
        "official_root": root_url,
        "tracks": tracks,
        "track_urls": {track: f"{root_url}{track}/" for track in tracks},
        "track_direct_link_counts": {
            track: {
                entity_type: value
                for entity_type, value in discovery_counts[track].items()
                if value
            }
            for track in tracks
        },
        "distinct_project_seed_urls": sum(
            discovery_counts[track]["project"] for track in tracks
        ),
        "distinct_firm_seed_urls": int(
            any(discovery_counts[track]["firm"] for track in tracks)
        ),
    }
    snapshot_manifest_size = 0
    snapshot_manifest_sha = "0" * 64
    lineage_metadata = {
        "run_summary": run_summary,
        "discovery_counts": discovery_counts,
        "selected_snapshot_count_semantics": "page_versions",
        "selected_page_version_count": 6,
        "distinct_physical_snapshot_count": 5,
        "snapshot_manifest_size_bytes": snapshot_manifest_size,
    }
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(awards_store_v2.OUTPUT_SCHEMA)
        meta = awards_store_v2._expected_schema_meta()
        connection.executemany(
            "INSERT INTO schema_meta VALUES (?,?)", sorted(meta.items())
        )
        connection.execute(
            """
            INSERT INTO input_lineage VALUES (
                1,'architizer_recrawl_v2_award_census','recrawl.db',1234,?,?,
                'mode=ro&immutable=1;query_only=ON',7,
                'award_seed_census_2026','completed','parser-v2',
                '2026-08-01T00:00:00+00:00','2026-08-01T00:01:00+00:00',
                'legacy.db',?,'snapshots',6,?,?
            )
            """,
            (
                sidecar_sha,
                sidecar_sha,
                sha256(raw),
                snapshot_manifest_sha,
                json.dumps(lineage_metadata, sort_keys=True, separators=(",", ":")),
            ),
        )
        pages = [(1, "year_root", None)] + [
            (index + 2, "track", track) for index, track in enumerate(tracks)
        ]
        for page_id, kind, track in pages:
            record_count = 0 if track is None else track_counts[track]
            connection.execute(
                """
                INSERT INTO award_page_versions VALUES (
                    ?,1,7,?,?,2026,?,?,?,?,200,'text/html',1000,?,?,?,?,
                    ?,?,'complete',?,?,?,'[]'
                )
                """,
                (
                    page_id,
                    10 + page_id,
                    kind,
                    track,
                    root_url
                    if track is None
                    else f"{root_url}{track}/",
                    root_url
                    if track is None
                    else f"{root_url}{track}/",
                    "exact",
                    f"{(1 if track == 'Typology' else page_id):064X}",
                    f"{(11 if track == 'Typology' else page_id + 10):064X}",
                    f"awards/{1 if track == 'Typology' else page_id}.html.gz",
                    500,
                    awards_store_v2.PARSER_VERSION,
                    "2026-08-01T00:00:00+00:00",
                    record_count,
                    record_count,
                    json.dumps({} if track is None else {"complete": record_count}),
                ),
            )
        page_for_track = {track: index + 2 for index, track in enumerate(tracks)}
        page_ordinals = Counter()
        subject_counts: Counter[str] = Counter()
        company_counts: Counter[str] = Counter()
        for index in range(1, count + 1):
            track = tracks[(index - 1) % len(tracks)]
            if track == "Firm":
                track, kind, slug, company_kind, company_slug = (
                    "Firm",
                    "firm",
                    "studio-one",
                    None,
                    None,
                )
            elif track in {"Plus", "Sustainability", "Typology"}:
                track, kind, slug, company_kind, company_slug = (
                    track,
                    "project",
                    f"project-{index:03d}",
                    "firm",
                    "studio-one",
                )
            else:
                track, kind, slug, company_kind, company_slug = (
                    "Products",
                    "product",
                    f"product-{index:03d}",
                    "brand",
                    f"brand-{index:03d}",
                )
            page_id = page_for_track[track]
            subject_counts[kind] += 1
            if company_kind:
                company_counts[company_kind] += 1
            ordinal = page_ordinals[page_id]
            page_ordinals[page_id] += 1
            entity_collection = {"firm": "firms", "project": "projects", "product": "products"}[kind]
            subject_url = f"https://architizer.com/{entity_collection}/{slug}/"
            connection.execute(
                """
                INSERT INTO award_attributions VALUES (
                    ?,?,?,?,?,2026,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                )
                """,
                (
                    index,
                    page_id,
                    index,
                    0,
                    ordinal,
                    track,
                    10000 + index,
                    f"projects.awardattribution.{10000 + index}",
                    f"Category > {track}",
                    json.dumps(["Category", track]),
                    kind,
                    slug,
                    f"Name {index}",
                    subject_url,
                    f"Description {index}",
                    f"https://images.example/{index}.jpg",
                    "complete",
                    "[]",
                    "[]",
                    "[]",
                    "{}",
                    "{}",
                    f"{root_url}{track}/",
                ),
            )
            connection.execute(
                "INSERT INTO award_attribution_tiers VALUES (?,?,?,?,?,?)",
                (index, 0, "Jury", "Jury Winner", "Jury Winner", "agreed"),
            )
            if company_kind:
                collection = "firms" if company_kind == "firm" else "brands"
                connection.execute(
                    "INSERT INTO award_attribution_companies VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        index,
                        0,
                        company_kind,
                        company_slug,
                        f"Company {index}",
                        f"https://architizer.com/{collection}/{company_slug}/",
                        "{}",
                        "{}",
                        "agreed",
                    ),
                )
        policies = [
            (
                item["entity_kind"],
                item["preserve_in_source_corpus"],
                item["corpus_role"],
                item["project_firm_curated_projection"],
                item["policy_version"],
            )
            for item in curated_v2.EXPECTED_AWARD_PROJECTION_POLICIES.values()
        ]
        connection.executemany(
            "INSERT INTO corpus_projection_policy VALUES (?,?,?,?,?)", policies
        )
        connection.execute(
            """
            INSERT INTO build_manifest VALUES (
                1,'2026-08-01T00:01:00+00:00',?,?,?,?,NULL,1,6,?,?,
                ?,?,?,?
            )
            """,
            (
                awards_store_v2.BUILDER_VERSION,
                awards_store_v2.SCHEMA_VERSION,
                awards_store_v2.PARSER_VERSION,
                awards_store_v2.POLICY_VERSION,
                count,
                count,
                json.dumps({"complete": count}),
                json.dumps(dict(sorted(subject_counts.items()))),
                json.dumps(dict(sorted(company_counts.items()))),
                json.dumps(
                    {
                        "award_year": 2026,
                        "tracks": tracks,
                        "root_alias_tracks": [],
                        "discovery_counts": discovery_counts,
                        "product_brand_policy": "preserve_source_only",
                    },
                    sort_keys=True,
                ),
            ),
        )
        manifest_bytes = awards_store_v2._snapshot_manifest_bytes_from_output_pages(
            connection.execute(
                "SELECT * FROM award_page_versions ORDER BY id"
            ).fetchall()
        )
        snapshot_manifest_size = len(manifest_bytes)
        snapshot_manifest_sha = hashlib.sha256(manifest_bytes).hexdigest().upper()
        lineage_metadata["snapshot_manifest_size_bytes"] = snapshot_manifest_size
        connection.execute(
            "UPDATE input_lineage SET snapshot_manifest_sha256=?,metadata_json=?",
            (
                snapshot_manifest_sha,
                json.dumps(lineage_metadata, sort_keys=True, separators=(",", ":")),
            ),
        )
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()
    run_identity = {
        "id": 7,
        "run_kind": "award_seed_census_2026",
        "status": "completed",
        "parser_version": "parser-v2",
        "started_at": "2026-08-01T00:00:00+00:00",
        "finished_at": "2026-08-01T00:01:00+00:00",
        "source_db_sha256": sha256(raw),
        "source_db_size_bytes": raw.stat().st_size,
        "summary_sha256": hashlib.sha256(
            json.dumps(
                run_summary,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest().upper(),
    }
    run_identity_bytes = (
        json.dumps(run_identity, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    ready_payload = {
        "artifact": "architizer_awards_v2",
        "ready_version": awards_store_v2.READY_VERSION,
        "builder_version": awards_store_v2.BUILDER_VERSION,
        "schema_version": awards_store_v2.SCHEMA_VERSION,
        "parser_version": awards_store_v2.PARSER_VERSION,
        "policy_version": awards_store_v2.POLICY_VERSION,
        "built_at": "2026-08-01T00:01:00+00:00",
        "award_year": 2026,
        "build_limit": None,
        "database": {
            "path": path.name,
            "size_bytes": path.stat().st_size,
            "sha256": sha256(path),
        },
        "input_sidecar": {
            "path": "recrawl.db",
            "schema_version": curated_v2.STATE_SCHEMA_VERSION,
            "size_bytes": 1234,
            "sha256_before": sidecar_sha,
            "sha256_after": sidecar_sha,
        },
        "recrawl_run": {
            **run_identity,
            "identity_size_bytes": len(run_identity_bytes),
            "identity_sha256": hashlib.sha256(run_identity_bytes).hexdigest().upper(),
        },
        "snapshot_manifest": {
            "size_bytes": snapshot_manifest_size,
            "sha256": snapshot_manifest_sha,
            "page_version_count": 6,
            "distinct_physical_snapshot_count": 5,
        },
        "validation": {
            "quick_check": "ok",
            "integrity_check": "ok",
            "foreign_key_violation_count": 0,
        },
    }
    Path(str(path) + ".READY.json").write_text(
        json.dumps(ready_payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def make_inputs(
    root: Path, *, qa_only_project_ids: frozenset[int] = frozenset()
) -> dict[str, Path]:
    raw = root / "raw.db"
    baseline = root / "baseline.db"
    reconciliation = root / "reconciliation.db"
    reconciliation_report = root / "reconciliation.md"
    reconciliation_ready = root / "reconciliation.db.READY.json"
    awards = root / "awards.db"
    awards_ready = Path(str(awards) + ".READY.json")
    create_raw(raw)
    create_baseline(raw, baseline)
    create_reconciliation_bundle(
        raw,
        baseline,
        reconciliation,
        reconciliation_report,
        reconciliation_ready,
        qa_only_project_ids=qa_only_project_ids,
    )
    create_awards(awards, raw)
    return {
        "raw": raw,
        "baseline": baseline,
        "reconciliation": reconciliation,
        "reconciliation_report": reconciliation_report,
        "reconciliation_ready": reconciliation_ready,
        "awards": awards,
        "awards_ready": awards_ready,
    }


def reseal_awards_ready(
    path: Path,
    *,
    recrawl_status: str | None = None,
    snapshot_manifest_sha256: str | None = None,
) -> Path:
    ready_path = Path(str(path) + ".READY.json")
    payload = json.loads(ready_path.read_text(encoding="utf-8"))
    if recrawl_status is not None:
        payload["recrawl_run"]["status"] = recrawl_status
        run_identity = {
            key: payload["recrawl_run"][key]
            for key in (
                "id",
                "run_kind",
                "status",
                "parser_version",
                "started_at",
                "finished_at",
                "source_db_sha256",
                "source_db_size_bytes",
                "summary_sha256",
            )
        }
        run_identity_bytes = (
            json.dumps(run_identity, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        payload["recrawl_run"]["identity_size_bytes"] = len(run_identity_bytes)
        payload["recrawl_run"]["identity_sha256"] = hashlib.sha256(
            run_identity_bytes
        ).hexdigest().upper()
    if snapshot_manifest_sha256 is not None:
        payload["snapshot_manifest"]["sha256"] = snapshot_manifest_sha256
    payload["database"] = {
        "path": path.name,
        "size_bytes": path.stat().st_size,
        "sha256": sha256(path),
    }
    ready_path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return ready_path


def build_arguments(
    inputs: dict[str, Path],
    root: Path,
    *,
    name: str,
    awards_path: Path | None = None,
    reconciliation_ready_path: Path | None = None,
) -> dict:
    awards = awards_path or inputs["awards"]
    awards_ready = Path(str(awards) + ".READY.json")
    return {
        "raw_path": inputs["raw"],
        "baseline_path": inputs["baseline"],
        "reconciliation_path": inputs["reconciliation"],
        "reconciliation_report_path": inputs["reconciliation_report"],
        "reconciliation_ready_path": (
            reconciliation_ready_path or inputs["reconciliation_ready"]
        ),
        "awards_path": awards,
        "awards_ready_path": awards_ready,
        "output_path": root / f"{name}.db",
        "report_path": root / f"{name}.md",
        "ready_path": root / f"{name}.READY.json",
        "expected_raw_sha256": sha256(inputs["raw"]),
        "expected_raw_size": inputs["raw"].stat().st_size,
        "expected_baseline_sha256": sha256(inputs["baseline"]),
        "expected_baseline_size": inputs["baseline"].stat().st_size,
        "expected_reconciliation_sha256": sha256(inputs["reconciliation"]),
        "expected_reconciliation_size": inputs["reconciliation"].stat().st_size,
        "expected_awards_sha256": sha256(awards),
        "expected_awards_size": awards.stat().st_size,
        "enforce_production_identities": False,
    }


def build_fixture(
    inputs: dict[str, Path],
    root: Path,
    *,
    name: str,
    limit: int | None,
    verify: bool,
) -> dict:
    return curated_v2.build(
        **build_arguments(inputs, root, name=name),
        limit=limit,
        confirm_full=limit is None,
        verify_deterministic=verify,
    )


class ArchitizerCuratedV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._temp.name)
        cls.inputs = make_inputs(cls.root)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temp.cleanup()

    def test_typology_exact_track_url_uses_snapshot_dedupe_not_alias(self) -> None:
        connection = curated_v2.open_readonly(self.inputs["awards"])
        try:
            validation = curated_v2.validate_awards(
                connection,
                identity={
                    "path_label": self.inputs["awards"].name,
                    "sha256": sha256(self.inputs["awards"]),
                    "size_bytes": self.inputs["awards"].stat().st_size,
                },
                awards_path=self.inputs["awards"],
                ready_path=self.inputs["awards_ready"],
                enforce_production_counts=False,
            )
            typology = connection.execute(
                "SELECT * FROM award_page_versions WHERE award_track='Typology'"
            ).fetchone()
            year_root = connection.execute(
                "SELECT * FROM award_page_versions WHERE page_kind='year_root'"
            ).fetchone()
            summary = json.loads(
                connection.execute("SELECT summary_json FROM build_manifest").fetchone()[0]
            )
        finally:
            connection.close()

        self.assertEqual(validation["record_count"], 120)
        self.assertEqual(
            typology["final_url"],
            "https://winners.architizer.com/2026/Typology/",
        )
        self.assertEqual(typology["final_url_policy"], "exact")
        self.assertEqual(summary["root_alias_tracks"], [])
        self.assertEqual(
            (
                typology["snapshot_content_sha256"],
                typology["snapshot_gzip_sha256"],
                typology["snapshot_gzip_path"],
            ),
            (
                year_root["snapshot_content_sha256"],
                year_root["snapshot_gzip_sha256"],
                year_root["snapshot_gzip_path"],
            ),
        )

    def test_n10_preserves_structured_types_and_is_byte_deterministic(self) -> None:
        first = build_fixture(
            self.inputs, self.root, name="n10-a", limit=10, verify=True
        )
        second = build_fixture(
            self.inputs, self.root, name="n10-b", limit=10, verify=False
        )
        self.assertEqual(first["database_sha256"], second["database_sha256"])
        self.assertTrue(first["deterministic_verified"])
        self.assertEqual(first["selected_project_count"], 10)
        self.assertEqual(first["selected_award_count"], 10)
        self.assertEqual(
            first["operational_metrics"]["comparison_status"],
            "not_comparable_smoke",
        )
        self.assertIsNone(
            first["operational_metrics"]["field_coverage_change"]["rate_delta"]
        )
        connection = sqlite3.connect(first["output_path"])
        try:
            kinds = dict(
                connection.execute(
                    "SELECT subject_kind,COUNT(*) FROM v2_structured_award_attributions GROUP BY subject_kind"
                ).fetchall()
            )
            product_projected = connection.execute(
                """
                SELECT COUNT(*) FROM v2_structured_award_base_projections p
                JOIN v2_structured_award_attributions a
                  ON a.source_attribution_id=p.source_attribution_id
                WHERE a.subject_kind='product' AND p.projection_status='projected'
                """
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(set(kinds), {"firm", "product", "project"})
        self.assertEqual(product_projected, 0)

    def test_full_accepts_qa_only_reconciliation_entities_but_excludes_them(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            inputs = make_inputs(root, qa_only_project_ids=frozenset({120}))
            result = build_fixture(
                inputs, root, name="full-with-qa-only", limit=None, verify=True
            )
            self.assertEqual(result["selected_project_count"], 119)
            connection = sqlite3.connect(result["output_path"])
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM v2_reconciliation_entities "
                        "WHERE entity_type='project' AND inclusion_status='qa_only'"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM source_projects").fetchone()[0],
                    119,
                )
            finally:
                connection.close()

    def test_legacy_nameless_award_stub_is_recreated_without_invented_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            raw = root / "raw.db"
            reconciliation = root / "reconciliation.db"
            shutil.copy2(self.inputs["raw"], raw)
            shutil.copy2(self.inputs["reconciliation"], reconciliation)

            connection = sqlite3.connect(raw)
            try:
                connection.execute(
                    """
                    INSERT INTO architizer_awards(
                        award_year,award_track,award_category,award_tier,
                        project_slug,firm_slug,source_url,fetched_at
                    ) VALUES (2024,'Firm',NULL,'Jury',NULL,'legacy-award-stub',
                              'https://winners.architizer.com/2024/Firm/',
                              '2026-07-30 12:00:00')
                    """
                )
                connection.commit()
            finally:
                connection.close()

            stub_fields = {
                "slug": "legacy-award-stub",
                "name": None,
                "office_locations": None,
                "description": None,
                "awards_summary": None,
                "project_count_seen": None,
                "project_urls": None,
                "social_links": None,
                "fetched_at": None,
            }
            connection = sqlite3.connect(reconciliation)
            try:
                connection.execute(
                    """
                    INSERT INTO entities(
                        entity_key,entity_type,source_url,source_slug,origin,
                        baseline_present,baseline_identity_key,
                        baseline_acceptance_status,last_good_version_id,
                        snapshot_sha256,parser_version,metadata_version,
                        identity_status,inclusion_status,identity_evidence_json,
                        effective_fields_json
                    ) VALUES (?,?,?,?,?,1,?,NULL,NULL,NULL,NULL,NULL,
                              'baseline_only','included','{}',?)
                    """,
                    (
                        "firm:legacy-award-stub",
                        "firm",
                        "https://architizer.com/firms/legacy-award-stub/",
                        "legacy-award-stub",
                        "baseline_only",
                        "legacy-award-stub",
                        json.dumps(stub_fields, sort_keys=True),
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            raw_ro = curated_v2.open_readonly(raw)
            reconciliation_ro = curated_v2.open_readonly(reconciliation)
            awards_ro = curated_v2.open_readonly(self.inputs["awards"])
            effective = root / "effective.db"
            try:
                result = curated_v2.build_effective_source(
                    raw=raw_ro,
                    reconciliation=reconciliation_ro,
                    awards=awards_ro,
                    target_path=effective,
                    reconciliation_id="fixture",
                    deterministic_cutoff="2026-08-03T12:00:00+00:00",
                    structured_attribution_ids=set(),
                )
            finally:
                raw_ro.close()
                reconciliation_ro.close()
                awards_ro.close()
            self.assertEqual(result["direct_firm_count"], 1)
            self.assertEqual(result["deferred_legacy_award_stub_count"], 1)

            effective_connection = sqlite3.connect(effective)
            try:
                self.assertEqual(
                    effective_connection.execute(
                        "SELECT COUNT(*) FROM architizer_firms WHERE slug='legacy-award-stub'"
                    ).fetchone()[0],
                    0,
                )
            finally:
                effective_connection.close()

            final = root / "curated.db"
            curated_v1.materialize_database(
                source_path=effective,
                target_path=final,
                limit=None,
                expected_sha256=sha256(effective),
                expected_size=effective.stat().st_size,
            )
            final_connection = sqlite3.connect(final)
            try:
                row = final_connection.execute(
                    "SELECT source_name,record_origin FROM source_firms "
                    "WHERE source_firm_slug='legacy-award-stub'"
                ).fetchone()
            finally:
                final_connection.close()
            self.assertEqual(row, (None, "award_stub"))

    def test_n100_and_full_gates_materialize_expected_counts(self) -> None:
        n100 = build_fixture(
            self.inputs, self.root, name="n100", limit=100, verify=False
        )
        self.assertEqual(n100["selected_project_count"], 100)
        self.assertEqual(n100["selected_award_count"], 100)
        full = build_fixture(
            self.inputs, self.root, name="full", limit=None, verify=True
        )
        self.assertEqual(full["selected_project_count"], 120)
        self.assertEqual(full["selected_award_count"], 120)
        self.assertEqual(full["validation"]["legacy_award_max_year"], 2025)
        self.assertEqual(full["validation"]["projected_2026_source_award_count"], 96)
        operational = full["operational_metrics"]
        self.assertEqual(operational["comparison_status"], "comparable_full")
        self.assertEqual(
            operational["source_recovery"][
                "recovered_legacy_failed_retry_valid_included_count"
            ],
            3,
        )
        self.assertEqual(
            operational["source_recovery"][
                "unrecovered_legacy_done_row_mismatch_terminal_count"
            ],
            1,
        )
        self.assertEqual(
            set(operational),
            {
                "comparison_status",
                "firm_stub_decrease",
                "award_unresolved_decrease",
                "field_coverage_change",
                "taxonomy_claim_change",
                "duplicate_candidate_change",
                "source_recovery",
                "open_qa",
            },
        )
        self.assertEqual(
            set(operational["firm_stub_decrease"]["before"]),
            {"project_stub", "award_stub", "total_union"},
        )
        self.assertIn(
            "legacy_link_rows_before", operational["award_unresolved_decrease"]
        )
        self.assertIn(
            "legacy_distinct_target_slugs_before",
            operational["award_unresolved_decrease"],
        )
        self.assertIn(
            "legacy_resolved_transition_count",
            operational["award_unresolved_decrease"],
        )
        self.assertEqual(
            set(
                operational["field_coverage_change"]["project_core_fields"][
                    "fields"
                ]
            ),
            set(curated_v2.PROJECT_CORE_FIELD_PREDICATES),
        )
        self.assertEqual(
            set(
                operational["field_coverage_change"]["firm_core_fields"][
                    "fields"
                ]
            ),
            set(curated_v2.FIRM_CORE_FIELD_PREDICATES),
        )
        connection = sqlite3.connect(full["output_path"])
        connection.row_factory = sqlite3.Row
        try:
            build = connection.execute("SELECT * FROM build_runs").fetchone()
            updated = connection.execute(
                "SELECT name FROM source_projects WHERE source_project_id=1"
            ).fetchone()[0]
            conflict = connection.execute(
                "SELECT * FROM v2_reconciliation_field_conflicts"
            ).fetchone()
            alias_count = connection.execute(
                "SELECT COUNT(*) FROM v2_reconciliation_entity_aliases"
            ).fetchone()[0]
            years = dict(
                connection.execute(
                    "SELECT award_year,COUNT(*) FROM source_awards GROUP BY award_year"
                ).fetchall()
            )
            input_roles = {
                row[0]
                for row in connection.execute(
                    "SELECT input_role FROM curated_v2_input_snapshots"
                )
            }
            target_reason_count = connection.execute(
                "SELECT COUNT(*) FROM v2_reconciliation_target_reasons"
            ).fetchone()[0]
            stored_operational = json.loads(
                connection.execute(
                    "SELECT metric_value_json FROM curated_v2_metrics "
                    "WHERE metric_name='operational_changes'"
                ).fetchone()[0]
            )
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]
            copied_counts = {
                target: connection.execute(f'SELECT COUNT(*) FROM "{target}"').fetchone()[0]
                for target in (
                    "v2_reconciliation_entities",
                    "v2_reconciliation_target_reasons",
                    "v2_reconciliation_field_candidates",
                    "v2_reconciliation_entity_aliases",
                    "v2_reconciliation_field_decisions",
                    "v2_reconciliation_field_conflicts",
                    "v2_reconciliation_qa_issues",
                    "v2_reconciliation_field_lineage",
                    "v2_reconciliation_metrics",
                )
            }
        finally:
            connection.close()
        self.assertEqual(build["schema_version"], SCHEMA_VERSION)
        self.assertEqual(updated, "Updated Project 001")
        self.assertEqual(conflict["disposition"], "baseline_retained")
        self.assertEqual(alias_count, 1)
        self.assertEqual(target_reason_count, 4)
        self.assertEqual(stored_operational, operational)
        self.assertEqual(journal_mode, "delete")
        self.assertEqual(synchronous, 2)
        for suffix in ("-wal", "-shm", "-journal"):
            self.assertFalse(Path(str(full["output_path"]) + suffix).exists())
        source_counts = {}
        reconciliation_connection = sqlite3.connect(self.inputs["reconciliation"])
        try:
            for source, target in (
                ("entities", "v2_reconciliation_entities"),
                ("source_target_reasons", "v2_reconciliation_target_reasons"),
                ("field_candidates", "v2_reconciliation_field_candidates"),
                ("entity_aliases", "v2_reconciliation_entity_aliases"),
                ("field_decisions", "v2_reconciliation_field_decisions"),
                ("field_conflicts", "v2_reconciliation_field_conflicts"),
                ("qa_issues", "v2_reconciliation_qa_issues"),
                ("field_lineage", "v2_reconciliation_field_lineage"),
                ("reconciliation_metrics", "v2_reconciliation_metrics"),
            ):
                source_counts[target] = reconciliation_connection.execute(
                    f'SELECT COUNT(*) FROM "{source}"'
                ).fetchone()[0]
        finally:
            reconciliation_connection.close()
        self.assertEqual(copied_counts, source_counts)
        self.assertEqual(years, {2025: 1, 2026: 96})
        self.assertEqual(
            input_roles,
            {
                "legacy_raw",
                "curated_v1_3",
                "reconciliation_plan",
                "reconciliation_report",
                "reconciliation_ready",
                "structured_awards_v2",
                "structured_awards_ready",
            },
        )
        ready = json.loads(Path(full["ready_path"]).read_text(encoding="utf-8"))
        self.assertEqual(ready["artifact_kind"], READY_VERSION)
        self.assertTrue(ready["deterministic_verified"])
        self.assertEqual(ready["database_sha256"], sha256(Path(full["output_path"])))
        report = Path(full["report_path"]).read_text(encoding="utf-8")
        self.assertIn("Operational before/after metrics", report)
        self.assertIn("Firm stub decrease", report)
        self.assertIn("Source recovery", report)
        self.assertIn("Open QA", report)

    def test_logical_digest_is_independent_of_primary_key_insertion_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = [Path(temp_dir) / "a.db", Path(temp_dir) / "b.db"]
            for path, rows in (
                (paths[0], [(2, "b", "large-json-b"), (1, "a", "large-json-a")]),
                (paths[1], [(1, "a", "large-json-a"), (2, "b", "large-json-b")]),
            ):
                connection = sqlite3.connect(path)
                try:
                    connection.execute(
                        "CREATE TABLE evidence(id INTEGER PRIMARY KEY, label TEXT, payload TEXT)"
                    )
                    connection.executemany("INSERT INTO evidence VALUES (?,?,?)", rows)
                    connection.commit()
                finally:
                    connection.close()
            digests = []
            for path in paths:
                connection = sqlite3.connect(path)
                try:
                    digests.append(curated_v2.logical_database_digest(connection))
                finally:
                    connection.close()
            self.assertEqual(digests[0], digests[1])

    def test_gates_hash_lock_and_no_overwrite(self) -> None:
        common = dict(
            raw_path=self.inputs["raw"],
            baseline_path=self.inputs["baseline"],
            reconciliation_path=self.inputs["reconciliation"],
            reconciliation_report_path=self.inputs["reconciliation_report"],
            reconciliation_ready_path=self.inputs["reconciliation_ready"],
            awards_path=self.inputs["awards"],
            awards_ready_path=self.inputs["awards_ready"],
            output_path=self.root / "gate.db",
            report_path=self.root / "gate.md",
            ready_path=self.root / "gate.READY.json",
            expected_raw_sha256=sha256(self.inputs["raw"]),
            expected_raw_size=self.inputs["raw"].stat().st_size,
            expected_baseline_sha256=sha256(self.inputs["baseline"]),
            expected_baseline_size=self.inputs["baseline"].stat().st_size,
            expected_reconciliation_sha256=sha256(self.inputs["reconciliation"]),
            expected_reconciliation_size=self.inputs["reconciliation"].stat().st_size,
            expected_awards_sha256=sha256(self.inputs["awards"]),
            expected_awards_size=self.inputs["awards"].stat().st_size,
            enforce_production_identities=False,
        )
        with self.assertRaisesRegex(curated_v2.CuratedV2Error, "exactly 10 or 100"):
            curated_v2.build(**common, limit=11)
        with self.assertRaisesRegex(curated_v2.CuratedV2Error, "explicit confirmation"):
            curated_v2.build(**common, limit=None, verify_deterministic=True)
        with self.assertRaisesRegex(curated_v2.CuratedV2Error, "requires byte determinism"):
            curated_v2.build(**common, limit=None, confirm_full=True)
        bad = dict(common)
        bad["expected_awards_sha256"] = "0" * 64
        with self.assertRaisesRegex(curated_v2.CuratedV2Error, "awards DB SHA mismatch"):
            curated_v2.build(**bad, limit=10)
        missing_ready = dict(common)
        missing_ready["awards_ready_path"] = self.root / "missing-awards.READY.json"
        with self.assertRaisesRegex(curated_v2.CuratedV2Error, "missing input"):
            curated_v2.build(**missing_ready, limit=10)

        awards_ready = self.inputs["awards_ready"]
        original_ready = awards_ready.read_bytes()
        try:
            for field, value, message in (
                (("database", "sha256"), "0" * 64, "database identity mismatch"),
                (("database", "path"), "wrong.db", "database identity mismatch"),
                (("snapshot_manifest", "sha256"), "0" * 64, "snapshot manifest mismatch"),
            ):
                with self.subTest(ready_tamper=field):
                    payload = json.loads(original_ready)
                    payload[field[0]][field[1]] = value
                    awards_ready.write_text(
                        json.dumps(payload, sort_keys=True, separators=(",", ":"))
                        + "\n",
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(curated_v2.CuratedV2Error, message):
                        curated_v2.build(**common, limit=10)
        finally:
            awards_ready.write_bytes(original_ready)
        lock = Path(str(common["output_path"]) + ".build.lock")
        lock.write_text("owner", encoding="utf-8")
        try:
            with self.assertRaisesRegex(curated_v2.CuratedV2Error, "build lock"):
                curated_v2.build(**common, limit=10)
        finally:
            lock.unlink()
        built = curated_v2.build(**common, limit=10)
        before = sha256(common["output_path"])
        with self.assertRaisesRegex(curated_v2.CuratedV2Error, "already exists"):
            curated_v2.build(**common, limit=10)
        self.assertEqual(sha256(common["output_path"]), before)
        self.assertEqual(built["selected_project_count"], 10)

    def test_input_drift_before_ready_rolls_back_and_lock_keeps_new_owner(self) -> None:
        common = build_arguments(
            self.inputs,
            self.root,
            name="pre-ready-input-drift",
        )
        trusted_report = common["reconciliation_report_path"]
        original_report = trusted_report.read_bytes()
        real_link = curated_v2.os.link
        call_count = 0

        def mutate_after_report_link(source: Path, destination: Path) -> None:
            nonlocal call_count
            real_link(source, destination)
            call_count += 1
            if call_count == 2:
                with trusted_report.open("ab") as handle:
                    handle.write(b"drift")

        try:
            with mock.patch.object(
                curated_v2.os,
                "link",
                side_effect=mutate_after_report_link,
            ), self.assertRaisesRegex(
                curated_v2.CuratedV2Error,
                "input changed before READY publication: reconciliation_report",
            ):
                curated_v2.build(**common, limit=10)
        finally:
            trusted_report.write_bytes(original_report)
        self.assertFalse(common["output_path"].exists())
        self.assertFalse(common["report_path"].exists())
        self.assertFalse(common["ready_path"].exists())

        lock = Path(str(common["output_path"]) + ".build.lock")
        with curated_v2.build_lock(common["output_path"]):
            payload = json.loads(lock.read_text(encoding="utf-8"))
            self.assertRegex(payload["owner_token"], r"^[0-9a-f]{64}$")
            lock.unlink()
            lock.write_text("replacement-owner", encoding="utf-8")
        self.assertEqual(lock.read_text(encoding="utf-8"), "replacement-owner")
        lock.unlink()

    def test_input_drift_inside_ready_link_rolls_back_bundle(self) -> None:
        common = build_arguments(
            self.inputs,
            self.root,
            name="post-ready-input-drift",
        )
        trusted_report = common["reconciliation_report_path"]
        original_report = trusted_report.read_bytes()
        real_link = curated_v2.os.link
        call_count = 0

        def link_then_mutate_on_ready(source: Path, destination: Path) -> None:
            nonlocal call_count
            real_link(source, destination)
            call_count += 1
            if call_count == 3:
                with trusted_report.open("ab") as handle:
                    handle.write(b"drift")

        try:
            with mock.patch.object(
                curated_v2.os,
                "link",
                side_effect=link_then_mutate_on_ready,
            ), self.assertRaisesRegex(
                curated_v2.CuratedV2Error,
                "input changed before READY publication: reconciliation_report",
            ):
                curated_v2.build(**common, limit=10)
        finally:
            trusted_report.write_bytes(original_report)
        self.assertFalse(common["output_path"].exists())
        self.assertFalse(common["report_path"].exists())
        self.assertFalse(common["ready_path"].exists())
        self.assertFalse(
            Path(str(common["output_path"]) + ".build.lock").exists()
        )

    def test_mixed_sidecar_and_incomplete_official_awards_are_rejected(self) -> None:
        mixed_awards = self.root / "mixed-sidecar-awards.db"
        create_awards(mixed_awards, self.inputs["raw"], sidecar_sha="B" * 64)
        mixed = build_arguments(
            self.inputs,
            self.root,
            name="mixed-sidecar-final",
            awards_path=mixed_awards,
        )
        with self.assertRaisesRegex(
            curated_v2.CuratedV2Error, "mixed recrawl sidecar lineage"
        ):
            curated_v2.build(**mixed, limit=10)

        cases = (
            (
                "three-track",
                "DROP TRIGGER award_page_versions_child_parity_update; "
                "UPDATE award_page_versions SET page_kind='ignored' "
                "WHERE award_track IN ('Products','Sustainability')",
                "official 2026 page/track contract",
            ),
            (
                "incomplete-track",
                "UPDATE award_page_versions SET source_record_count="
                "source_record_count+1 WHERE award_track='Firm'",
                "official track page mismatch",
            ),
            (
                "typology-final-url",
                "UPDATE award_page_versions SET final_url="
                "'https://winners.architizer.com/2026/' "
                "WHERE award_track='Typology'",
                "official track page mismatch",
            ),
            (
                "typology-final-url-policy",
                "UPDATE award_page_versions SET final_url_policy="
                "'official_year_root_alias_verified' WHERE award_track='Typology'",
                "official track page mismatch",
            ),
            (
                "typology-dedupe",
                "UPDATE award_page_versions SET snapshot_content_sha256='"
                + "B" * 64
                + "' WHERE award_track='Typology'",
                "Typology deduplicated year-root snapshot mismatch",
            ),
            (
                "manifest-root-alias",
                "UPDATE build_manifest SET summary_json=json_set("
                "summary_json,'$.root_alias_tracks',json_array('Typology'))",
                "build manifest parity mismatch",
            ),
            (
                "manifest-parity",
                "UPDATE build_manifest SET page_count=5",
                "build manifest parity mismatch",
            ),
            (
                "policy-mutation",
                "UPDATE corpus_projection_policy SET corpus_role='arbitrary' "
                "WHERE entity_kind='product'",
                "corpus projection policy contract mismatch",
            ),
            (
                "policy-missing",
                "DELETE FROM corpus_projection_policy WHERE entity_kind='brand'",
                "corpus projection policy contract mismatch",
            ),
            (
                "policy-extra",
                "INSERT INTO corpus_projection_policy VALUES "
                "('other',1,'arbitrary','arbitrary','"
                + awards_store_v2.POLICY_VERSION
                + "')",
                "corpus projection policy contract mismatch",
            ),
            (
                "manifest-discovery-count",
                "UPDATE build_manifest SET summary_json=json_set("
                "summary_json,'$.discovery_counts.Firm.firm',99)",
                "discovery-count parity mismatch",
            ),
            (
                "lineage-discovery-count",
                "UPDATE input_lineage SET metadata_json=json_set("
                "metadata_json,'$.discovery_counts.Firm.firm',99)",
                "discovery-count parity mismatch",
            ),
            (
                "run4-direct-link-count",
                "UPDATE input_lineage SET metadata_json=json_set("
                "metadata_json,'$.run_summary.track_direct_link_counts.Firm.firm',99)",
                "discovery-count parity mismatch",
            ),
            (
                "attribution-parent-track",
                "DROP TRIGGER award_attributions_parent_parity_update; "
                "UPDATE award_attributions SET award_track='Firm' WHERE id=3;",
                "attribution parent page parity mismatch",
            ),
            (
                "attribution-nonofficial-track",
                "DROP TRIGGER award_attributions_parent_parity_update; "
                "UPDATE award_attributions SET award_track='Unofficial' WHERE id=3;",
                "attribution parent page parity mismatch",
            ),
            (
                "attribution-parent-year",
                "DROP TRIGGER award_attributions_parent_parity_update; "
                "UPDATE award_attributions SET award_year=2025 WHERE id=3;",
                "attribution parent page parity mismatch",
            ),
            (
                "attribution-parent-source",
                "DROP TRIGGER award_attributions_parent_parity_update; "
                "UPDATE award_attributions SET source_url='https://example.invalid/' "
                "WHERE id=3;",
                "attribution parent page parity mismatch",
            ),
        )
        for label, statement, message in cases:
            with self.subTest(label=label):
                awards = self.root / f"bad-awards-{label}.db"
                create_awards(awards, self.inputs["raw"])
                connection = sqlite3.connect(awards)
                try:
                    connection.executescript(statement)
                    connection.commit()
                finally:
                    connection.close()
                reseal_awards_ready(awards)
                arguments = build_arguments(
                    self.inputs,
                    self.root,
                    name=f"bad-awards-final-{label}",
                    awards_path=awards,
                )
                with self.assertRaisesRegex(curated_v2.CuratedV2Error, message):
                    curated_v2.build(**arguments, limit=10)

    def test_resealed_awards_release_tampering_is_rejected(self) -> None:
        replacement_manifest_sha = "D" * 64
        cases = (
            (
                "schema-meta-parser",
                "UPDATE schema_meta SET value='tampered' WHERE key='parser_version'",
                {},
            ),
            (
                "manifest-parser",
                "UPDATE build_manifest SET parser_version='tampered'",
                {},
            ),
            (
                "input-kind",
                "UPDATE input_lineage SET input_kind='fixture'",
                {},
            ),
            (
                "page-run",
                "UPDATE award_page_versions SET recrawl_run_id=999 "
                "WHERE award_track='Firm'",
                {},
            ),
            (
                "page-parser",
                "UPDATE award_page_versions SET parser_version='tampered' "
                "WHERE award_track='Firm'",
                {},
            ),
            (
                "page-http",
                "UPDATE award_page_versions SET http_status=403 "
                "WHERE award_track='Firm'",
                {},
            ),
            (
                "page-status-counts",
                "UPDATE award_page_versions SET status_counts_json='{}' "
                "WHERE award_track='Products'",
                {},
            ),
            (
                "page-duplicate-attribution-ids",
                "UPDATE award_page_versions SET duplicate_attribution_ids_json='[1]' "
                "WHERE award_track='Firm'",
                {},
            ),
            (
                "duplicate-attribution-global-id",
                "DROP INDEX idx_award_attributions_global_id_unique; "
                "UPDATE award_attributions SET attribution_global_id=("
                "SELECT attribution_global_id FROM award_attributions WHERE id=1"
                ") WHERE id=3",
                {},
            ),
            (
                "complete-attribution-without-tier",
                "DELETE FROM award_attribution_tiers WHERE attribution_id=3",
                {},
            ),
            (
                "root-parse-status",
                "UPDATE award_page_versions SET parse_status='partial' "
                "WHERE page_kind='year_root'",
                {},
            ),
            (
                "page-snapshot-evidence",
                "UPDATE award_page_versions SET snapshot_content_sha256='"
                + "E" * 64
                + "',snapshot_gzip_path='tampered.html.gz' "
                "WHERE award_track='Firm'",
                {},
            ),
            (
                "lineage-snapshot-manifest",
                "UPDATE input_lineage SET snapshot_manifest_sha256='"
                + replacement_manifest_sha
                + "'",
                {"snapshot_manifest_sha256": replacement_manifest_sha},
            ),
            (
                "lineage-failed-run",
                "UPDATE input_lineage SET recrawl_run_status='failed'",
                {"recrawl_status": "failed"},
            ),
        )
        for label, statement, ready_options in cases:
            with self.subTest(label=label):
                awards = self.root / f"resealed-awards-{label}.db"
                create_awards(awards, self.inputs["raw"])
                connection = sqlite3.connect(awards)
                try:
                    connection.executescript(statement)
                    connection.commit()
                    connection.execute("VACUUM")
                finally:
                    connection.close()
                reseal_awards_ready(awards, **ready_options)
                arguments = build_arguments(
                    self.inputs,
                    self.root,
                    name=f"resealed-awards-final-{label}",
                    awards_path=awards,
                )
                with self.assertRaisesRegex(
                    curated_v2.CuratedV2Error,
                    "structured awards release contract mismatch",
                ):
                    curated_v2.build(**arguments, limit=10)

    def test_reconciliation_ready_is_an_exact_versioned_receipt(self) -> None:
        original = json.loads(
            self.inputs["reconciliation_ready"].read_text(encoding="utf-8")
        )
        cases: list[tuple[str, dict]] = []

        missing = json.loads(json.dumps(original))
        del missing["ready_version"]
        cases.append(("missing-key", missing))
        extra = json.loads(json.dumps(original))
        extra["claim"] = "unverified"
        cases.append(("extra-key", extra))
        scalar_keys = (
            "ready_version",
            "tool_version",
            "plan_schema_version",
            "policy_version",
            "baseline_schema_version",
            "final_target_schema_version",
            "publication_eligibility",
            "reconciliation_id",
            "selected_project_count",
            "selected_firm_count",
            "pending_target_count",
        )
        for key in scalar_keys:
            candidate = json.loads(json.dumps(original))
            candidate[key] = "tampered"
            cases.append((key, candidate))
        nested_cases = (
            ("database", "sha256", "0" * 64),
            ("database", "logical_sha256", "0" * 64),
            ("database", "size_bytes", 1),
            ("report", "sha256", "0" * 64),
            ("report", "size_bytes", 1),
            ("trusted_manifest", "sha256", "0" * 64),
            ("trusted_manifest", "size_bytes", 1),
            ("validation", "output", {}),
        )
        for section, field, value in nested_cases:
            candidate = json.loads(json.dumps(original))
            candidate[section][field] = value
            cases.append((f"{section}.{field}", candidate))

        for index, (label, candidate) in enumerate(cases):
            with self.subTest(label=label):
                ready = self.root / f"tampered-reconciliation-{index}.READY.json"
                ready.write_text(
                    json.dumps(candidate, sort_keys=True, separators=(",", ":"))
                    + "\n",
                    encoding="utf-8",
                )
                arguments = build_arguments(
                    self.inputs,
                    self.root,
                    name=f"tampered-reconciliation-final-{index}",
                    reconciliation_ready_path=ready,
                )
                with self.assertRaises(curated_v2.CuratedV2Error):
                    curated_v2.build(**arguments, limit=10)

    def test_smoke_paths_and_full_resource_preflight_are_hard_gates(self) -> None:
        arguments = build_arguments(
            self.inputs, self.root, name="smoke-production-path"
        )
        arguments["output_path"] = curated_v2.DEFAULT_OUTPUT
        with self.assertRaisesRegex(
            curated_v2.CuratedV2Error, "explicit non-production"
        ):
            curated_v2.build(**arguments, limit=10)

        preflight = {
            "raw_path": self.inputs["raw"],
            "baseline_path": self.inputs["baseline"],
            "reconciliation_path": self.inputs["reconciliation"],
            "awards_path": self.inputs["awards"],
            "output_path": self.root / "resource-preflight.db",
            "verify_deterministic": True,
        }
        with mock.patch.object(
            curated_v2.shutil,
            "disk_usage",
            return_value=mock.Mock(free=10**15),
        ), mock.patch.object(
            curated_v2, "_available_memory_bytes", return_value=10**15
        ):
            measured = curated_v2.preflight_full_resources(**preflight)
        self.assertGreaterEqual(
            measured["required_free_disk_bytes"],
            3 * measured["estimated_output_bytes"] + measured["safety_margin_bytes"],
        )
        self.assertEqual(measured["output_workspace_copies"], 3)
        self.assertEqual(
            measured["input_cardinalities"]["reconciliation.entities"], 121
        )

        large_reconciliation = self.root / "large-cardinality-reconciliation.db"
        shutil.copyfile(self.inputs["reconciliation"], large_reconciliation)
        connection = sqlite3.connect(large_reconciliation)
        try:
            connection.executemany(
                """
                INSERT INTO field_candidates
                SELECT ?,entity_key,?,source_role,value_json,status,quality,
                       metadata_version_id,source_locator_json
                FROM field_candidates WHERE candidate_id='candidate-baseline-2'
                """,
                (
                    (f"bulk-candidate-{index}", f"bulk-field-{index}")
                    for index in range(5000)
                ),
            )
            connection.commit()
        finally:
            connection.close()
        large_preflight = {**preflight, "reconciliation_path": large_reconciliation}
        with mock.patch.object(
            curated_v2.shutil,
            "disk_usage",
            return_value=mock.Mock(free=10**15),
        ), mock.patch.object(
            curated_v2, "_available_memory_bytes", return_value=10**15
        ):
            large_measured = curated_v2.preflight_full_resources(**large_preflight)
        self.assertEqual(
            large_measured["input_cardinalities"][
                "reconciliation.field_candidates"
            ],
            measured["input_cardinalities"]["reconciliation.field_candidates"]
            + 5000,
        )
        self.assertGreater(
            large_measured["cardinality_memory_bytes"],
            measured["cardinality_memory_bytes"],
        )
        with mock.patch.object(
            curated_v2.shutil, "disk_usage", return_value=mock.Mock(free=1)
        ), mock.patch.object(
            curated_v2, "_available_memory_bytes", return_value=10**15
        ), self.assertRaisesRegex(curated_v2.CuratedV2Error, "disk preflight failed"):
            curated_v2.preflight_full_resources(**preflight)
        with mock.patch.object(
            curated_v2.shutil,
            "disk_usage",
            return_value=mock.Mock(free=10**15),
        ), mock.patch.object(
            curated_v2, "_available_memory_bytes", return_value=1
        ), self.assertRaisesRegex(curated_v2.CuratedV2Error, "RAM preflight failed"):
            curated_v2.preflight_full_resources(**preflight)

    def test_production_award_count_contract_is_fixed_to_run4(self) -> None:
        connection = curated_v2.open_readonly(self.inputs["awards"])
        try:
            with self.assertRaisesRegex(
                curated_v2.CuratedV2Error, "production run4 record-count contract"
            ):
                curated_v2.validate_awards(
                    connection,
                    identity={
                        "path_label": self.inputs["awards"].name,
                        "sha256": sha256(self.inputs["awards"]),
                        "size_bytes": self.inputs["awards"].stat().st_size,
                    },
                    awards_path=self.inputs["awards"],
                    ready_path=self.inputs["awards_ready"],
                    enforce_production_counts=True,
                )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
