"""Offline tests for the Architizer source census and recrawl-v2 sidecar."""

from __future__ import annotations

import gzip
import hashlib
import html
import json
import sqlite3
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from crawl.architizer import recrawl_v2 as recrawl
from tools import recrawl_architizer_source_v2 as recrawl_cli


SOURCE_SCHEMA = """
CREATE TABLE architizer_projects (
    id INTEGER PRIMARY KEY,
    global_id TEXT,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    firm_slug TEXT,
    firm_name TEXT,
    description TEXT,
    description_short TEXT,
    completion_year INTEGER,
    building_size_slug TEXT,
    building_size_display TEXT,
    constr_status TEXT,
    budget REAL,
    location_full TEXT,
    location_country TEXT,
    location_city TEXT,
    categories TEXT,
    cover_image_url TEXT,
    gallery_image_urls TEXT,
    image_global_ids TEXT,
    published_time TEXT,
    modified_time TEXT,
    fetched_at TEXT
);

CREATE TABLE architizer_firms (
    slug TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    office_locations TEXT,
    description TEXT,
    awards_summary TEXT,
    project_count_seen INTEGER,
    social_links TEXT,
    fetched_at TEXT
);

CREATE TABLE architizer_awards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    award_year INTEGER NOT NULL,
    award_track TEXT NOT NULL,
    award_category TEXT,
    award_tier TEXT NOT NULL,
    project_slug TEXT,
    firm_slug TEXT,
    source_url TEXT NOT NULL,
    fetched_at TEXT
);

CREATE TABLE pending_projects (
    url TEXT PRIMARY KEY,
    source_url TEXT,
    lastmod TEXT,
    status TEXT,
    discovered_at TEXT,
    fetched_at TEXT,
    error TEXT
);

CREATE TABLE pending_firms (
    url TEXT PRIMARY KEY,
    source_url TEXT,
    lastmod TEXT,
    status TEXT,
    discovered_at TEXT,
    fetched_at TEXT,
    error TEXT
);
"""


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def create_source(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SOURCE_SCHEMA)
        connection.execute(
            """
            INSERT INTO architizer_projects(
                id,global_id,slug,name,firm_slug,firm_name,description,
                description_short,completion_year,building_size_slug,
                building_size_display,constr_status,budget,location_full,
                location_country,location_city,categories,cover_image_url,
                gallery_image_urls,image_global_ids,published_time,
                modified_time,fetched_at
            ) VALUES (
                1,'projects.project.1','old-a','Old A','missing-studio',
                'Missing Studio','Old description','Old short',2020,
                'sqft_10_25','10,000 sqft - 25,000 sqft','built',NULL,
                'Seoul, South Korea','South Korea','Seoul','["Cultural"]',
                'https://images.example/old.jpg',
                '["https://images.example/old.jpg"]',
                '["media.mediaitemattribution.1"]',
                '2020-01-01','2025-01-01','2026-04-28'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO architizer_firms(
                slug,name,office_locations,description,awards_summary,
                project_count_seen,social_links,fetched_at
            ) VALUES (
                'firm-one','Firm One','["Seoul, South Korea"]',
                'Firm description',NULL,1,'{}','2026-04-28'
            )
            """
        )
        project_queue = [
            (
                "https://architizer.com/projects/old-a/",
                "https://architizer.com/sitemap-projects.xml",
                "2026-04-01",
                "done",
                None,
            ),
            (
                "https://architizer.com/projects/old-window-only/",
                "https://architizer.com/sitemap-projects.xml?p=2",
                "2025-04-01",
                "done",
                None,
            ),
            (
                "https://architizer.com/projects/failed-one/",
                "https://architizer.com/sitemap-projects.xml",
                "2026-04-02",
                "failed",
                "no_pk_in_data_data",
            ),
            (
                "https://architizer.com/projects/done-mismatch/",
                "https://architizer.com/sitemap-projects.xml",
                "2026-04-03",
                "done",
                None,
            ),
        ]
        connection.executemany(
            """
            INSERT INTO pending_projects(
                url,source_url,lastmod,status,discovered_at,fetched_at,error
            ) VALUES (?,?,?,?, '2026-04-28','2026-04-28',?)
            """,
            project_queue,
        )
        connection.execute(
            """
            INSERT INTO pending_firms(
                url,source_url,lastmod,status,discovered_at,fetched_at,error
            ) VALUES (
                'https://architizer.com/firms/firm-one/',
                'https://architizer.com/sitemap-firms.xml',
                '2026-04-01','done','2026-04-28','2026-04-28',NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO architizer_awards(
                award_year,award_track,award_category,award_tier,
                project_slug,firm_slug,source_url,fetched_at
            ) VALUES (
                2025,'Typology','Cultural','Jury',
                'award-project','award-firm',
                'https://winners.architizer.com/2025/Typology/',
                '2026-04-28'
            )
            """
        )
        connection.commit()
    finally:
        connection.close()


INDEX_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://architizer.com/sitemap-projects.xml</loc></sitemap>
  <sitemap><loc>https://architizer.com/sitemap-projects.xml?p=2</loc></sitemap>
  <sitemap><loc>https://architizer.com/sitemap-firms.xml</loc></sitemap>
  <sitemap><loc>https://architizer.com/sitemap-products.xml</loc></sitemap>
</sitemapindex>
"""

PROJECT_XML_1 = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://architizer.com/projects/old-a/</loc>
       <lastmod>2026-07-01</lastmod></url>
  <url><loc>https://architizer.com/projects/new-a/</loc>
       <lastmod>2026-07-02</lastmod></url>
</urlset>
"""

PROJECT_XML_2 = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://architizer.com/projects/new-a/</loc>
       <lastmod>2026-07-02</lastmod></url>
</urlset>
"""

FIRM_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://architizer.com/firms/firm-one/</loc>
       <lastmod>2026-07-03</lastmod></url>
</urlset>
"""


def http_result(url: str, body: bytes, content_type: str) -> recrawl.HttpResult:
    return recrawl.HttpResult(
        [
            recrawl.HttpAttempt(
                requested_url=url,
                attempt_number=1,
                started_at="2026-07-31T00:00:00+00:00",
                finished_at="2026-07-31T00:00:01+00:00",
                duration_ms=10,
                outcome="success",
                http_status=200,
                final_url=url,
                content_type=content_type,
                body=body,
                retryable=False,
                block_signals=[],
                error=None,
            )
        ]
    )


PROJECT_FIXTURE = """<!doctype html>
<html>
<head>
  <title>Alpha House by Studio Alpha | Architizer</title>
  <link rel="canonical" href="https://architizer.com/projects/alpha-house/">
  <meta property="og:title" content="Alpha House">
  <meta property="og:description" content="Short Alpha description">
  <meta property="og:image" content="https://img.example/alpha.jpg">
  <meta property="article:tag" content="Residential">
  <meta property="article:author"
        content="https://architizer.com/firms/studio-alpha/">
  <meta property="article:published_time" content="2024-01-01T00:00:00Z">
  <meta property="article:modified_time" content="2026-07-30T00:00:00Z">
</head>
<body>
  <h1>Alpha House</h1><h2>Seoul, South Korea</h2>
  <div data-data='{"pk":101,"global_id":"projects.project.101",
       "slug":"alpha-house","absolute_url":"/projects/alpha-house/",
       "name":"Alpha House","description":"Full Alpha description",
       "completion_date":"2024-01-01","building_size":"sqft_10_25",
       "constr_status":"built","location":"Seoul, South Korea",
       "firm":{"name":"Studio Alpha",
               "url":"https://architizer.com/firms/studio-alpha/"},
       "categories":["Residential"],
       "cover_image_url":"https://img.example/alpha.jpg",
       "images":["https://img.example/alpha.jpg"],
       "published_time":"2024-01-01T00:00:00Z",
       "modified_time":"2026-07-30T00:00:00Z"}'></div>
  <figure data-globalid="media.mediaitemattribution.101"></figure>
</body>
</html>
"""


def project_fixture(*, description: str | None, dom_name: str = "Alpha House") -> bytes:
    description_json = (
        f',"description":{json.dumps(description)}' if description is not None else ""
    )
    return f"""<!doctype html><html><head>
<title>{dom_name} by Studio Alpha | Architizer</title>
<link rel="canonical" href="https://architizer.com/projects/alpha-house/">
<meta property="og:title" content="{dom_name}">
</head><body><h1>{dom_name}</h1>
<div data-data='{{"pk":101,"global_id":"projects.project.101",
"slug":"alpha-house","absolute_url":"/projects/alpha-house/",
"name":"Alpha House"{description_json}}}'></div>
{'x' * 600}</body></html>""".encode()


def firm_location_fixture(
    *,
    embedded_locations: tuple[str, ...] = (),
    dom_locations: tuple[str, ...] | None = None,
    unrelated_location: str | None = None,
) -> bytes:
    dom_values = embedded_locations if dom_locations is None else dom_locations

    def location_record(
        index: int,
        embedded_value: str | None,
        dom_value: str | None,
    ) -> str:
        data_attribute = ""
        if embedded_value is not None:
            payload = json.dumps(
                {
                    "global_id": f"locations.geolocation.{1000 + index}",
                    "pk": 1000 + index,
                    "for_humans": embedded_value,
                }
            )
            data_attribute = f" data-data='{html.escape(payload, quote=True)}'"
        visible = ""
        if dom_value is not None:
            visible = (
                "<span class='grey icon marker'></span>"
                "<span class='placeholder single-line js-rendered-content'>"
                f"{html.escape(dom_value)}</span>"
            )
        return f"<div class='attribution'{data_attribute}>{visible}</div>"

    locations = []
    for index in range(max(len(embedded_locations), len(dom_values))):
        embedded_value = (
            embedded_locations[index]
            if index < len(embedded_locations)
            else None
        )
        dom_value = dom_values[index] if index < len(dom_values) else None
        locations.append(location_record(index, embedded_value, dom_value))
    expected_container = (
        "<div id='studio-alpha-locations'>"
        + "".join(locations)
        + "</div>"
        if locations
        else ""
    )
    unrelated = ""
    if unrelated_location is not None:
        unrelated = (
            location_record(900, unrelated_location, None)
            + "<div id='different-firm-locations'>"
            + location_record(901, unrelated_location, unrelated_location)
            + "</div>"
        )
    firm_payload = html.escape(
        json.dumps(
            {
                "global_id": "firms.firm.101",
                "pk": 101,
                "name": "Studio Alpha",
                "absolute_url": "/firms/studio-alpha/",
            }
        ),
        quote=True,
    )
    return (
        "<!doctype html><html><head>"
        "<title>Studio Alpha | Architizer</title>"
        "<link rel='canonical' "
        "href='https://architizer.com/firms/studio-alpha/'>"
        "</head><body><h1>Studio Alpha</h1>"
        f"<div data-data='{firm_payload}'></div>"
        f"{unrelated}{expected_container}"
        + "x" * 600
        + "</body></html>"
    ).encode()


def passing_smoke_summary(size: int) -> dict[str, object]:
    type_stats = {
        name: {"selected": 1, "http_success": 1, "parse_success": 1}
        for name in (
            "sitemap_new_project",
            "sitemap_modified_project",
            "legacy_recovery",
            "firm_stub",
            "award_seed",
            "unchanged",
        )
    }
    return recrawl.evaluate_smoke_quality(
        {
            "selected": size,
            "http_success": size,
            "snapshot_saved": size,
            "metadata_version_count": size,
            "input_db_unchanged": True,
            "block_signal_counts": {},
            "type_stats": type_stats,
            "identity_valid": size,
            "identity_exception_details": [],
            "parse_status_counts": {"complete": size},
            "parse_exception_details": [],
            "field_coverage_rates": {"name": 1.0, "slug": 1.0},
        },
        smoke_size=size,
    )


def seed_failed_full_finalization_fixture(
    root: Path,
    *,
    count: int = 5,
) -> dict[str, object]:
    """Create a small schema-2.1 analogue of the run15 terminal state."""

    source = root / "source.db"
    state_path = root / "state.db"
    snapshot_root = root / "snapshots"
    snapshot_root.mkdir()
    create_source(source)
    source_sha = sha256(source)
    state = recrawl.connect_state(
        state_path,
        source_path=source,
        source_sha256=source_sha,
        source_size=source.stat().st_size,
    )
    snapshot_manifest: list[tuple[str, str]] = []
    try:
        prior_run_id = recrawl.start_run(
            state,
            run_kind="network_smoke_n100",
            source_path=source,
            source_sha256=source_sha,
            source_size=source.stat().st_size,
            arguments={"smoke_size": 100},
        )
        storage_after = {
            "state_bytes": 16_384,
            "snapshot_bytes": 8_192,
            "combined_bytes": 24_576,
        }
        recrawl.finish_run(
            state,
            prior_run_id,
            status="completed",
            source_sha256_after=source_sha,
            summary={"runtime_storage": {"after": storage_after}},
            selected_count=100,
        )
        state.execute(
            """
            UPDATE runs
            SET started_at='2026-08-03T00:00:00+00:00',
                finished_at='2026-08-03T00:10:00+00:00'
            WHERE id=?
            """,
            (prior_run_id,),
        )

        urls = [
            f"https://architizer.com/projects/finalize-fixture-{index}/"
            for index in range(count)
        ]
        arguments = {
            "confirmed": True,
            "delay_seconds": 2.0,
            "timeout_seconds": 30.0,
            "max_attempts": 3,
            "expanded_counts": {"project": count},
            "frozen_target_count": count,
            "frozen_target_urls_sha256": recrawl._url_set_sha256(urls),
        }
        run_id = recrawl.start_run(
            state,
            run_kind="full_recrawl_v2",
            source_path=source,
            source_sha256=source_sha,
            source_size=source.stat().st_size,
            arguments=arguments,
        )
        metadata_version_ids: list[int] = []
        for index, url in enumerate(urls):
            failed = index == count - 1
            final_status = "failed" if failed else "done"
            parse_status = "no_content" if failed else "complete"
            identity_status = "missing" if failed else "valid"
            identity = {
                "status": identity_status,
                "errors": [],
                "missing": ["project_id", "global_id"] if failed else [],
            }
            recrawl.upsert_target(
                state,
                url=url,
                entity_type="project",
                source_lastmod=f"2026-08-{index + 1:02d}",
                priority=10,
                reason="sitemap_new_project",
                discovery_source="fixture:sitemap",
                input_lineage={"fixture": True},
            )
            state.execute(
                """
                INSERT INTO run_targets(
                    run_id,url,selection_order,selected_reason,
                    status_before,status_after
                ) VALUES (?,?,?,?,?,?)
                """,
                (
                    run_id,
                    url,
                    index + 1,
                    "sitemap_new_project",
                    "pending",
                    final_status,
                ),
            )
            body = f"<html>full finalization fixture {index}</html>".encode()
            content_sha, gzip_path, _ = recrawl._write_gzip_snapshot(
                snapshot_root,
                kind="pages",
                content=body,
                extension="html",
            )
            snapshot_manifest.append((gzip_path, content_sha))
            state.execute(
                """
                INSERT INTO http_attempts(
                    run_id,target_url,request_kind,requested_url,
                    attempt_number,started_at,finished_at,duration_ms,
                    outcome,http_status,final_url,content_type,response_bytes,
                    sha256,gzip_path,retryable,block_signals_json,error
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    url,
                    "entity_page",
                    url,
                    1,
                    "2026-08-04T00:54:27+00:00",
                    "2026-08-04T00:54:28+00:00",
                    1_000 + index,
                    "success",
                    200,
                    url,
                    "text/html; charset=utf-8",
                    len(body),
                    content_sha,
                    gzip_path,
                    0,
                    "[]",
                    None,
                ),
            )
            version_id = int(
                state.execute(
                    """
                    INSERT INTO metadata_versions(
                        run_id,target_url,entity_type,snapshot_sha256,
                        parser_version,metadata_version,parsed_at,parse_status,
                        quality,identity_status,identity_json,
                        raw_embedded_json,dom_json,resolved_json,conflict_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        run_id,
                        url,
                        "project",
                        content_sha,
                        "architizer-source-parser-v2.2.0",
                        "architizer-source-metadata-v2.2",
                        "2026-08-04T00:54:28+00:00",
                        parse_status,
                        "invalid" if failed else "high",
                        identity_status,
                        recrawl.canonical_json(identity),
                        "[]",
                        "{}",
                        "{}",
                        "{}",
                    ),
                ).lastrowid
            )
            metadata_version_ids.append(version_id)
            state.execute(
                """
                INSERT INTO run_metadata_versions(
                    run_id,version_id,target_url
                ) VALUES (?,?,?)
                """,
                (run_id, version_id, url),
            )
            if not failed:
                state.execute(
                    """
                    INSERT INTO resolved_fields(
                        version_id,field_name,value_json,status,quality,
                        conflict_json
                    ) VALUES (?,?,?,?,?,NULL)
                    """,
                    (
                        version_id,
                        "name",
                        recrawl.canonical_json(f"Fixture {index}"),
                        "confirmed",
                        "high",
                    ),
                )
            state.execute(
                """
                INSERT INTO legacy_field_comparisons(
                    version_id,field_name,legacy_value_json,
                    observed_value_json,comparison_status
                ) VALUES (?,?,?,?,?)
                """,
                (version_id, "name", "null", "null", "same"),
            )
            state.execute(
                """
                UPDATE targets
                SET status=?,retryable=0,attempt_count=1,
                    last_attempt_at='2026-08-04T00:54:28+00:00',
                    last_error=?,last_http_status=200,
                    last_snapshot_sha256=?,last_parse_status=?,
                    current_metadata_version_id=?,last_good_version_id=?,
                    updated_at='2026-08-04T00:54:28+00:00'
                WHERE url=?
                """,
                (
                    final_status,
                    "project_id;global_id" if failed else None,
                    content_sha,
                    parse_status,
                    version_id if not failed else None,
                    version_id if not failed else None,
                    url,
                ),
            )

        pending_url = "https://architizer.com/firms/discovered-after-full/"
        recrawl.upsert_target(
            state,
            url=pending_url,
            entity_type="firm",
            source_lastmod=None,
            priority=40,
            reason="recrawl_project_firm_relation",
            discovery_source=f"metadata_version:{metadata_version_ids[0]}",
            input_lineage={"metadata_version_id": metadata_version_ids[0]},
        )
        state.execute(
            """
            UPDATE runs
            SET started_at='2026-08-04T00:54:27+00:00',
                finished_at='2026-08-04T22:38:10+00:00',
                status='failed',
                parser_version='architizer-source-parser-v2.2.0',
                source_db_sha256_after=?,selected_count=?,summary_json='{}',
                error=?
            WHERE id=?
            """,
            (
                source_sha,
                count,
                recrawl.FULL_RUN_SQL_VARIABLE_ERROR,
                run_id,
            ),
        )
        state.commit()
    finally:
        state.close()

    downgrade = sqlite3.connect(state_path)
    try:
        downgrade.execute("PRAGMA foreign_keys=OFF")
        downgrade.execute("DROP TABLE snapshot_reparse_lineage")
        downgrade.execute("DROP TABLE snapshot_reparse_inputs")
        downgrade.execute(
            "UPDATE state_meta SET value='2.1' WHERE key='schema_version'"
        )
        downgrade.commit()
    finally:
        downgrade.close()
    return {
        "source": source,
        "state": state_path,
        "snapshots": snapshot_root,
        "source_sha": source_sha,
        "run_id": run_id,
        "prior_run_id": prior_run_id,
        "urls": urls,
        "pending_url": pending_url,
        "snapshot_manifest": snapshot_manifest,
    }


class SitemapAndCensusTests(unittest.TestCase):
    def test_official_sitemap_input_and_redirect_provenance_are_enforced(
        self,
    ) -> None:
        with self.assertRaises(recrawl.RecrawlError):
            recrawl.validate_official_sitemap_url(
                "https://example.com/sitemap.xml",
                index=True,
            )
        with self.assertRaises(recrawl.RecrawlError):
            recrawl.validate_official_sitemap_url(
                "https://architizer.com/sitemap-projects.xml?p=guessed"
            )
        handler = recrawl._RestrictedRedirectHandler(
            {recrawl.ARCHITIZER_HOST}
        )
        with self.assertRaises(urllib.error.URLError) as caught:
            handler.redirect_request(
                mock.Mock(),
                None,
                302,
                "Found",
                {},
                "https://example.com/sitemap.xml",
            )
        self.assertIn("outside allowlist", str(caught.exception))

        with tempfile.TemporaryDirectory(prefix="architizer-redirect-test-") as root:
            root_path = Path(root)
            source = root_path / "source.db"
            state = root_path / "state.db"
            snapshots = root_path / "snapshots"
            create_source(source)
            before = sha256(source)
            redirected = http_result(
                recrawl.OFFICIAL_SITEMAP_URL,
                INDEX_XML,
                "application/xml",
            )
            redirected.final.final_url = (
                "https://architizer.com/not-the-official-index.xml"
            )
            with mock.patch.object(
                recrawl.PoliteHttpClient,
                "fetch",
                autospec=True,
                return_value=redirected,
            ):
                with self.assertRaises(recrawl.RecrawlError):
                    recrawl.run_source_census(
                        source_path=source,
                        state_path=state,
                        snapshot_root=snapshots,
                        delay_seconds=0,
                        max_attempts=1,
                    )
            self.assertEqual(sha256(source), before)

    def test_only_registered_index_children_are_used_and_snapshot_is_immutable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="architizer-census-test-") as root:
            root_path = Path(root)
            source = root_path / "source.db"
            state = root_path / "state.db"
            snapshots = root_path / "snapshots"
            create_source(source)
            before = sha256(source)
            responses = {
                recrawl.OFFICIAL_SITEMAP_URL: http_result(
                    recrawl.OFFICIAL_SITEMAP_URL,
                    INDEX_XML,
                    "application/xml",
                ),
                "https://architizer.com/sitemap-projects.xml": http_result(
                    "https://architizer.com/sitemap-projects.xml",
                    PROJECT_XML_1,
                    "application/xml",
                ),
                "https://architizer.com/sitemap-projects.xml?p=2": http_result(
                    "https://architizer.com/sitemap-projects.xml?p=2",
                    PROJECT_XML_2,
                    "application/xml",
                ),
                "https://architizer.com/sitemap-firms.xml": http_result(
                    "https://architizer.com/sitemap-firms.xml",
                    FIRM_XML,
                    "application/xml",
                ),
            }

            def fake_fetch(_: recrawl.PoliteHttpClient, url: str) -> recrawl.HttpResult:
                self.assertIn(url, responses)
                return responses[url]

            with mock.patch.object(
                recrawl.PoliteHttpClient,
                "fetch",
                autospec=True,
                side_effect=fake_fetch,
            ):
                manifest = recrawl.run_source_census(
                    source_path=source,
                    state_path=state,
                    snapshot_root=snapshots,
                    delay_seconds=0,
                    max_attempts=1,
                    unchanged_sample_size=1,
                )
            self.assertEqual(sha256(source), before)
            self.assertTrue(manifest["legacy_input"]["immutable"])
            self.assertEqual(
                manifest["official_sitemap"]["registered_project_sitemaps"], 2
            )
            self.assertEqual(
                manifest["official_sitemap"]["registered_firm_sitemaps"], 1
            )
            project = manifest["comparison"]["project"]
            self.assertEqual(project["current_sitemap_entry_occurrences"], 3)
            self.assertEqual(project["current_sitemap_url_count"], 2)
            self.assertEqual(project["duplicate_occurrence_count"], 1)
            self.assertEqual(project["overlap"], 1)
            self.assertEqual(project["current_new"], 1)
            self.assertEqual(project["legacy_not_in_current"], 3)
            self.assertEqual(project["overlap_lastmod_changed"], 1)
            self.assertEqual(project["current_without_entity_row"], 1)
            connection = recrawl.open_state_readonly(state)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM sitemap_entry_occurrences"
                    ).fetchone()[0],
                    4,
                )
                self.assertEqual(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM targets
                        WHERE primary_reason='sitemap_new'
                        """
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM target_reasons
                        WHERE reason='legacy_failed_retry'
                        """
                    ).fetchone()[0],
                    1,
                )
            finally:
                connection.close()
            snapshot_connection = recrawl.open_state_readonly(state)
            try:
                snapshot_rows = snapshot_connection.execute(
                    "SELECT sha256,gzip_path FROM sitemap_snapshots"
                ).fetchall()
            finally:
                snapshot_connection.close()
            for row in snapshot_rows:
                self.assertTrue(
                    recrawl.verify_snapshot(
                        snapshots, row["gzip_path"], row["sha256"]
                    )
                )

            writable = recrawl.connect_state(
                state,
                source_path=source,
                source_sha256=before,
                source_size=source.stat().st_size,
            )
            try:
                writable.execute(
                    """
                    UPDATE targets SET status='done'
                    WHERE url='https://architizer.com/projects/new-a/'
                    """
                )
                writable.commit()
            finally:
                writable.close()
            with mock.patch.object(
                recrawl.PoliteHttpClient,
                "fetch",
                autospec=True,
                side_effect=fake_fetch,
            ):
                recrawl.run_source_census(
                    source_path=source,
                    state_path=state,
                    snapshot_root=snapshots,
                    delay_seconds=0,
                    max_attempts=1,
                    unchanged_sample_size=1,
                )
            repeated = recrawl.open_state_readonly(state)
            try:
                self.assertEqual(
                    repeated.execute(
                        """
                        SELECT status FROM targets
                        WHERE url='https://architizer.com/projects/new-a/'
                        """
                    ).fetchone()[0],
                    "done",
                )
            finally:
                repeated.close()

    def test_sitemap_rejects_wrong_root_and_external_entity_url(self) -> None:
        with self.assertRaises(recrawl.RecrawlError):
            recrawl.parse_sitemap_index(b"<urlset/>")
        with self.assertRaises(recrawl.RecrawlError):
            recrawl.parse_sitemap_urls(b"<!DOCTYPE x><urlset/>")
        with self.assertRaises(recrawl.RecrawlError):
            recrawl.validate_sitemap_entity_url(
                "https://example.com/projects/not-allowed/",
                "project",
            )

    def test_duplicate_sitemap_lastmod_conflict_is_order_independent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="architizer-lastmod-test-") as root:
            root_path = Path(root)
            source = root_path / "source.db"
            state = root_path / "state.db"
            snapshots = root_path / "snapshots"
            create_source(source)
            first = b"""<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
            <url><loc>https://architizer.com/projects/old-a/</loc></url>
            </urlset>"""
            second = b"""<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
            <url><loc>https://architizer.com/projects/old-a/</loc>
            <lastmod>2026-07-01</lastmod></url></urlset>"""
            responses = {
                recrawl.OFFICIAL_SITEMAP_URL: http_result(
                    recrawl.OFFICIAL_SITEMAP_URL, INDEX_XML, "application/xml"
                ),
                "https://architizer.com/sitemap-projects.xml": http_result(
                    "https://architizer.com/sitemap-projects.xml",
                    first,
                    "application/xml",
                ),
                "https://architizer.com/sitemap-projects.xml?p=2": http_result(
                    "https://architizer.com/sitemap-projects.xml?p=2",
                    second,
                    "application/xml",
                ),
                "https://architizer.com/sitemap-firms.xml": http_result(
                    "https://architizer.com/sitemap-firms.xml",
                    FIRM_XML,
                    "application/xml",
                ),
            }

            def fake_fetch(_: recrawl.PoliteHttpClient, url: str) -> recrawl.HttpResult:
                return responses[url]

            with mock.patch.object(
                recrawl.PoliteHttpClient,
                "fetch",
                autospec=True,
                side_effect=fake_fetch,
            ):
                with self.assertRaises(recrawl.RecrawlError):
                    recrawl.run_source_census(
                        source_path=source,
                        state_path=state,
                        snapshot_root=snapshots,
                        delay_seconds=0,
                        max_attempts=1,
                    )


class ParserFixtureTests(unittest.TestCase):
    def test_scanner_tolerates_meta_without_name_and_keeps_links(self) -> None:
        body = (
            b"<html><head><meta charset='utf-8'></head>"
            b"<body><a href='/2026/Typology/'>Typology</a></body></html>"
        )
        self.assertEqual(
            recrawl._scan_links(body, "text/html; charset=utf-8"),
            ["/2026/Typology/"],
        )

    def test_project_fixture_preserves_embedded_dom_identity_and_fields(self) -> None:
        result = recrawl.parse_entity_page(
            PROJECT_FIXTURE.encode(),
            requested_url="https://architizer.com/projects/alpha-house/",
            final_url="https://architizer.com/projects/alpha-house/",
            http_status=200,
            content_type="text/html; charset=utf-8",
            entity_type="project",
        )
        self.assertEqual(result["parser_version"], recrawl.PARSER_VERSION)
        self.assertEqual(result["identity"]["status"], "valid")
        self.assertEqual(result["identity"]["project_id"], 101)
        self.assertEqual(result["identity"]["global_id"], "projects.project.101")
        self.assertEqual(result["resolved"]["name"]["value"], "Alpha House")
        self.assertEqual(
            result["resolved"]["firm_slug"]["value"], "studio-alpha"
        )
        self.assertEqual(
            result["resolved"]["completion_year"]["value"], 2024
        )
        self.assertEqual(
            result["observations"]["completion_year"]["raw"]["embedded_json"],
            "2024-01-01",
        )
        self.assertEqual(
            result["resolved"]["construction_status"]["value"], "built"
        )
        self.assertEqual(
            result["resolved"]["size_bucket"]["value"], "sqft_10_25"
        )
        self.assertEqual(
            result["resolved"]["description"]["value"],
            "Full Alpha description",
        )
        self.assertEqual(
            result["observations"]["location"]["embedded_json"],
            "Seoul, South Korea",
        )
        self.assertEqual(
            result["observations"]["location"]["dom"],
            "Seoul, South Korea",
        )
        self.assertTrue(result["embedded_records"][0]["raw"])
        self.assertTrue(result["relationships"])

    def test_data_data_raw_json_wins_before_html_unescape_fallback(self) -> None:
        payload = json.dumps(
            {
                "pk": 176967,
                "global_id": "projects.project.176967",
                "slug": "architektursalon",
                "absolute_url": "/projects/architektursalon/",
                "name": "Architektursalon",
                "description": "A literal &quot;quoted&quot; source value",
            }
        )
        attribute = html.escape(payload, quote=True)
        body = (
            "<!doctype html><html><head>"
            "<title>Architektursalon | Architizer</title>"
            "<link rel='canonical' href='https://architizer.com/projects/"
            "architektursalon/'>"
            "</head><body><h1>Architektursalon</h1>"
            f"<div data-data='{attribute}'></div>"
            + "x" * 600
            + "</body></html>"
        ).encode()
        result = recrawl.parse_entity_page(
            body,
            requested_url=(
                "https://architizer.com/projects/architektursalon/"
            ),
            final_url="https://architizer.com/projects/architektursalon/",
            http_status=200,
            content_type="text/html",
            entity_type="project",
        )
        self.assertEqual(result["identity"]["status"], "valid")
        self.assertEqual(
            result["resolved"]["project_id"]["value"],
            176967,
        )
        self.assertEqual(
            result["resolved"]["global_id"]["value"],
            "projects.project.176967",
        )
        self.assertEqual(result["embedded_records"][0]["parse_variant"], "raw")
        self.assertIn("&quot;quoted&quot;", result["embedded_records"][0]["raw"])

    def test_firm_location_one_is_confirmed_from_scoped_json_and_dom(self) -> None:
        result = recrawl.parse_entity_page(
            firm_location_fixture(embedded_locations=("Seoul, South Korea",)),
            requested_url="https://architizer.com/firms/studio-alpha/",
            final_url="https://architizer.com/firms/studio-alpha/",
            http_status=200,
            content_type="text/html; charset=utf-8",
            entity_type="firm",
        )
        self.assertEqual(
            recrawl.PARSER_VERSION,
            "architizer-source-parser-v2.3.0",
        )
        self.assertEqual(
            recrawl.METADATA_VERSION,
            "architizer-source-metadata-v2.3",
        )
        self.assertEqual(result["identity"]["status"], "valid")
        self.assertEqual(
            result["observations"]["office_locations"]["embedded_json"],
            ["Seoul, South Korea"],
        )
        self.assertEqual(
            result["observations"]["office_locations"]["dom"],
            ["Seoul, South Korea"],
        )
        self.assertEqual(
            result["resolved"]["office_locations"],
            {
                "value": ["Seoul, South Korea"],
                "status": "confirmed",
                "quality": "high",
                "conflict": None,
            },
        )
        location_record = next(
            record
            for record in result["embedded_records"]
            if "locations.geolocation." in record["raw"]
        )
        self.assertEqual(
            location_record["context"],
            {"firm_location_slug": "studio-alpha"},
        )

    def test_firm_location_multiple_values_keep_source_order_and_dedupe(self) -> None:
        locations = (
            "Seoul, South Korea",
            "Busan, South Korea",
            "Seoul, South Korea",
        )
        result = recrawl.parse_entity_page(
            firm_location_fixture(embedded_locations=locations),
            requested_url="https://architizer.com/firms/studio-alpha/",
            final_url="https://architizer.com/firms/studio-alpha/",
            http_status=200,
            content_type="text/html",
            entity_type="firm",
        )
        self.assertEqual(
            result["resolved"]["office_locations"]["value"],
            ["Seoul, South Korea", "Busan, South Korea"],
        )
        self.assertEqual(
            result["resolved"]["office_locations"]["status"],
            "confirmed",
        )

    def test_firm_location_none_remains_missing_without_inference(self) -> None:
        result = recrawl.parse_entity_page(
            firm_location_fixture(),
            requested_url="https://architizer.com/firms/studio-alpha/",
            final_url="https://architizer.com/firms/studio-alpha/",
            http_status=200,
            content_type="text/html",
            entity_type="firm",
        )
        self.assertEqual(
            result["resolved"]["office_locations"]["status"],
            "missing",
        )
        self.assertIsNone(result["resolved"]["office_locations"]["value"])

    def test_firm_location_ignores_unrelated_container_and_json_record(self) -> None:
        result = recrawl.parse_entity_page(
            firm_location_fixture(unrelated_location="Paris, France"),
            requested_url="https://architizer.com/firms/studio-alpha/",
            final_url="https://architizer.com/firms/studio-alpha/",
            http_status=200,
            content_type="text/html",
            entity_type="firm",
        )
        self.assertEqual(
            result["observations"]["office_locations"]["embedded_json"],
            [],
        )
        self.assertEqual(
            result["observations"]["office_locations"]["dom"],
            [],
        )
        self.assertEqual(
            result["resolved"]["office_locations"]["status"],
            "missing",
        )

    def test_firm_location_requires_geolocation_id_and_marker_sibling(self) -> None:
        wrong_id = firm_location_fixture(
            embedded_locations=("Seoul, South Korea",),
        ).replace(b"locations.geolocation.1000", b"projects.project.1000")
        parsed_wrong_id = recrawl.parse_entity_page(
            wrong_id,
            requested_url="https://architizer.com/firms/studio-alpha/",
            final_url="https://architizer.com/firms/studio-alpha/",
            http_status=200,
            content_type="text/html",
            entity_type="firm",
        )
        self.assertEqual(
            parsed_wrong_id["observations"]["office_locations"][
                "embedded_json"
            ],
            [],
        )
        self.assertEqual(
            parsed_wrong_id["resolved"]["office_locations"]["status"],
            "single_source",
        )

        wrong_field = firm_location_fixture(
            embedded_locations=("Seoul, South Korea",),
        ).replace(b"for_humans", b"address")
        parsed_wrong_field = recrawl.parse_entity_page(
            wrong_field,
            requested_url="https://architizer.com/firms/studio-alpha/",
            final_url="https://architizer.com/firms/studio-alpha/",
            http_status=200,
            content_type="text/html",
            entity_type="firm",
        )
        self.assertEqual(
            parsed_wrong_field["observations"]["office_locations"][
                "embedded_json"
            ],
            [],
        )
        self.assertEqual(
            parsed_wrong_field["resolved"]["office_locations"]["status"],
            "single_source",
        )

        not_a_sibling = firm_location_fixture(
            embedded_locations=("Seoul, South Korea",),
        ).replace(
            b"</span><span class='placeholder",
            b"</span><em>not a sibling</em><span class='placeholder",
        )
        parsed_not_a_sibling = recrawl.parse_entity_page(
            not_a_sibling,
            requested_url="https://architizer.com/firms/studio-alpha/",
            final_url="https://architizer.com/firms/studio-alpha/",
            http_status=200,
            content_type="text/html",
            entity_type="firm",
        )
        self.assertEqual(
            parsed_not_a_sibling["observations"]["office_locations"]["dom"],
            [],
        )
        self.assertEqual(
            parsed_not_a_sibling["resolved"]["office_locations"]["status"],
            "single_source",
        )

        missing_single_line = firm_location_fixture(
            embedded_locations=("Seoul, South Korea",),
        ).replace(
            b"placeholder single-line js-rendered-content",
            b"placeholder js-rendered-content",
        )
        parsed_missing_single_line = recrawl.parse_entity_page(
            missing_single_line,
            requested_url="https://architizer.com/firms/studio-alpha/",
            final_url="https://architizer.com/firms/studio-alpha/",
            http_status=200,
            content_type="text/html",
            entity_type="firm",
        )
        self.assertEqual(
            parsed_missing_single_line["observations"]["office_locations"]["dom"],
            [],
        )
        self.assertEqual(
            parsed_missing_single_line["resolved"]["office_locations"]["status"],
            "single_source",
        )

        missing_marker_icon = firm_location_fixture(
            embedded_locations=("Seoul, South Korea",),
        ).replace(b"grey icon marker", b"grey marker")
        parsed_missing_marker_icon = recrawl.parse_entity_page(
            missing_marker_icon,
            requested_url="https://architizer.com/firms/studio-alpha/",
            final_url="https://architizer.com/firms/studio-alpha/",
            http_status=200,
            content_type="text/html",
            entity_type="firm",
        )
        self.assertEqual(
            parsed_missing_marker_icon["observations"]["office_locations"]["dom"],
            [],
        )
        self.assertEqual(
            parsed_missing_marker_icon["resolved"]["office_locations"]["status"],
            "single_source",
        )

    def test_firm_location_embedded_dom_disagreement_is_a_conflict(self) -> None:
        result = recrawl.parse_entity_page(
            firm_location_fixture(
                embedded_locations=("Seoul, South Korea",),
                dom_locations=("Busan, South Korea",),
            ),
            requested_url="https://architizer.com/firms/studio-alpha/",
            final_url="https://architizer.com/firms/studio-alpha/",
            http_status=200,
            content_type="text/html",
            entity_type="firm",
        )
        self.assertEqual(result["parse_status"], "conflict")
        self.assertEqual(
            result["resolved"]["office_locations"]["status"],
            "conflict",
        )
        self.assertIsNone(result["resolved"]["office_locations"]["value"])
        self.assertEqual(
            result["conflicts"]["office_locations"],
            {
                "embedded_json": ["Seoul, South Korea"],
                "dom": ["Busan, South Korea"],
            },
        )

    def test_firm_location_notfound_page_is_never_valid_content(self) -> None:
        result = recrawl.parse_entity_page(
            firm_location_fixture(embedded_locations=("Seoul, South Korea",)),
            requested_url="https://architizer.com/firms/studio-alpha/",
            final_url="https://architizer.com/firms/?notfound=1",
            http_status=200,
            content_type="text/html",
            entity_type="firm",
        )
        self.assertEqual(result["identity"]["status"], "conflict")
        self.assertIn(
            "final_url_slug_mismatch",
            result["identity"]["errors"],
        )
        self.assertEqual(result["parse_status"], "no_content")

    def test_requested_slug_wins_over_richer_related_json_object(self) -> None:
        body = (
            """<!doctype html><html><head>
            <link rel="canonical"
                  href="https://architizer.com/projects/alpha-house/">
            <title>Alpha House | Architizer</title></head><body>
            <h1>Alpha House</h1>
            <div data-data='{"pk":999,"global_id":"projects.project.999",
            "slug":"related-project","name":"Related Project",
            "completion_date":"2025-01-01","building_size":"sqft_100_300",
            "constr_status":"built","absolute_url":"/projects/related-project/"}'>
            </div>
            <div data-data='{"pk":101,"global_id":"projects.project.101",
            "slug":"alpha-house","name":"Alpha House",
            "absolute_url":"/projects/alpha-house/"}'></div>"""
            + "x" * 600
            + "</body></html>"
        ).encode()
        result = recrawl.parse_entity_page(
            body,
            requested_url="https://architizer.com/projects/alpha-house/",
            final_url="https://architizer.com/projects/alpha-house/",
            http_status=200,
            content_type="text/html",
            entity_type="project",
        )
        self.assertEqual(result["identity"]["status"], "valid")
        self.assertEqual(result["identity"]["project_id"], 101)

    def test_external_redirect_and_firm_soft_404_are_rejected(self) -> None:
        project = recrawl.parse_entity_page(
            project_fixture(description="x"),
            requested_url="https://architizer.com/projects/alpha-house/",
            final_url="https://example.com/projects/alpha-house/",
            http_status=200,
            content_type="text/html",
            entity_type="project",
        )
        self.assertIn("final_url_external_host", project["identity"]["errors"])
        self.assertNotEqual(project["identity"]["status"], "valid")
        firm_body = (
            "<html><head><title>Generic profile</title></head>"
            "<body><h1>Generic profile</h1>"
            + "x" * 700
            + "</body></html>"
        ).encode()
        firm = recrawl.parse_entity_page(
            firm_body,
            requested_url="https://architizer.com/firms/not-real/",
            final_url="https://architizer.com/firms/not-real/",
            http_status=200,
            content_type="text/html",
            entity_type="firm",
        )
        self.assertEqual(firm["identity"]["status"], "missing")
        self.assertIn("firm_identity_signal", firm["identity"]["missing"])
        with self.assertRaises(recrawl.RecrawlError):
            recrawl.normalize_entity_url(
                "https://example.com/projects/external/",
                "project",
            )

    def test_login_block_and_error_fixtures_are_never_valid_projects(self) -> None:
        fixtures = [
            (
                (
                    "<html><head><title>Log In</title></head>"
                    "<body><form action='/login'>Log in</form>"
                    + "x" * 700
                    + "</body></html>"
                ).encode(),
                "https://architizer.com/login/",
                200,
                "login",
            ),
            (
                (
                    "<html><head><title>Attention Required! | Cloudflare</title>"
                    "</head><body>cf-chl-captcha "
                    + "x" * 700
                    + "</body></html>"
                ).encode(),
                "https://architizer.com/projects/alpha-house/",
                200,
                "block",
            ),
            (
                (
                    "<html><head><title>404 Not Found</title></head><body>"
                    + "x" * 700
                    + "</body></html>"
                ).encode(),
                "https://architizer.com/projects/alpha-house/",
                404,
                "error",
            ),
        ]
        for body, final_url, status, classification in fixtures:
            with self.subTest(classification=classification):
                result = recrawl.parse_entity_page(
                    body,
                    requested_url="https://architizer.com/projects/alpha-house/",
                    final_url=final_url,
                    http_status=status,
                    content_type="text/html",
                    entity_type="project",
                )
                self.assertEqual(
                    result["page_classification"]["classification"],
                    classification,
                )
                self.assertNotEqual(result["identity"]["status"], "valid")
                self.assertEqual(result["parse_status"], "no_content")

    def test_wrong_global_id_is_identity_conflict(self) -> None:
        body = project_fixture(description="x").replace(
            b"projects.project.101",
            b"firms.firm.101",
        )
        result = recrawl.parse_entity_page(
            body,
            requested_url="https://architizer.com/projects/alpha-house/",
            final_url="https://architizer.com/projects/alpha-house/",
            http_status=200,
            content_type="text/html",
            entity_type="project",
        )
        self.assertEqual(result["identity"]["status"], "conflict")
        self.assertIn(
            "global_id_wrong_entity_type", result["identity"]["errors"]
        )

    def test_malformed_same_prefix_global_ids_are_conflicts(self) -> None:
        project_body = project_fixture(description="x").replace(
            b"projects.project.101",
            b"projects.project.foo",
        )
        project = recrawl.parse_entity_page(
            project_body,
            requested_url="https://architizer.com/projects/alpha-house/",
            final_url="https://architizer.com/projects/alpha-house/",
            http_status=200,
            content_type="text/html",
            entity_type="project",
        )
        self.assertEqual(project["identity"]["status"], "conflict")
        self.assertIn("global_id_invalid_format", project["identity"]["errors"])

        firm_body = (
            """<!doctype html><html><head>
            <link rel="canonical"
                  href="https://architizer.com/firms/studio-alpha/">
            <title>Studio Alpha | Architizer</title></head><body>
            <h1>Studio Alpha</h1>
            <div data-data='{"global_id":"firms.firm.foo",
            "slug":"studio-alpha","name":"Studio Alpha",
            "absolute_url":"/firms/studio-alpha/"}'></div>"""
            + "x" * 600
            + "</body></html>"
        ).encode()
        firm = recrawl.parse_entity_page(
            firm_body,
            requested_url="https://architizer.com/firms/studio-alpha/",
            final_url="https://architizer.com/firms/studio-alpha/",
            http_status=200,
            content_type="text/html",
            entity_type="firm",
        )
        self.assertEqual(firm["identity"]["status"], "conflict")
        self.assertIn("global_id_invalid_format", firm["identity"]["errors"])


class StateAndIntegrityTests(unittest.TestCase):
    def test_network_summary_uses_constant_bind_count(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="architizer-finalize-summary-test-"
        ) as root:
            fixture = seed_failed_full_finalization_fixture(Path(root))
            connection = sqlite3.connect(fixture["state"])
            connection.row_factory = sqlite3.Row
            original_limit = connection.setlimit(
                sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER,
                4,
            )
            try:
                summary = recrawl._network_run_summary(
                    connection,
                    run_id=int(fixture["run_id"]),
                    source_sha_before=str(fixture["source_sha"]),
                    source_sha_after=str(fixture["source_sha"]),
                    delay_seconds=2.0,
                    state_path=Path(fixture["state"]),
                    snapshot_root=Path(fixture["snapshots"]),
                    storage_before={
                        "state_bytes": 0,
                        "snapshot_bytes": 0,
                        "combined_bytes": 0,
                    },
                    elapsed_seconds=1.0,
                )
            finally:
                connection.setlimit(
                    sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER,
                    original_limit,
                )
                connection.close()
            self.assertEqual(summary["selected"], 5)
            self.assertEqual(summary["metadata_version_count"], 5)
            self.assertEqual(summary["physical_request_attempts"], 5)
            self.assertEqual(summary["identity_status_counts"], {
                "missing": 1,
                "valid": 4,
            })
            self.assertEqual(summary["field_coverage_counts"]["name"], 4)
            self.assertEqual(
                summary["legacy_field_comparison_counts"],
                {"same": 5},
            )
            integrity = summary["snapshot_integrity"]
            self.assertEqual(
                integrity["policy_version"],
                recrawl.NETWORK_SNAPSHOT_INTEGRITY_POLICY_VERSION,
            )
            self.assertEqual(integrity["validated_count"], 5)
            for field_name in (
                "evidence_manifest_sha256",
                "content_sha256_manifest_sha256",
                "gzip_sha256_manifest_sha256",
                "gzip_path_manifest_sha256",
            ):
                self.assertRegex(integrity[field_name], r"^[0-9A-F]{64}$")

    def test_network_summary_uses_run_scoped_target_evidence(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="architizer-run-scoped-summary-test-"
        ) as root:
            fixture = seed_failed_full_finalization_fixture(Path(root))
            connection = sqlite3.connect(fixture["state"])
            connection.row_factory = sqlite3.Row
            try:
                connection.execute(
                    """
                    UPDATE targets
                    SET entity_type='firm',status='pending',
                        last_parse_status='no_content',last_error='later wave'
                    """
                )
                connection.commit()
                summary = recrawl._network_run_summary(
                    connection,
                    run_id=int(fixture["run_id"]),
                    source_sha_before=str(fixture["source_sha"]),
                    source_sha_after=str(fixture["source_sha"]),
                    delay_seconds=2.0,
                    state_path=Path(fixture["state"]),
                    snapshot_root=Path(fixture["snapshots"]),
                    storage_before={
                        "state_bytes": 0,
                        "snapshot_bytes": 0,
                        "combined_bytes": 0,
                    },
                    elapsed_seconds=1.0,
                )
            finally:
                connection.close()
            self.assertEqual(
                summary["field_coverage_denominators"]["completion_year"],
                5,
            )
            self.assertEqual(
                summary["type_stats"]["sitemap_new_project"],
                {
                    "failure": 1,
                    "http_success": 5,
                    "parse_success": 4,
                    "selected": 5,
                },
            )

    def test_network_summary_rejects_unverified_snapshot_evidence(self) -> None:
        cases = (
            "escape",
            "missing",
            "content_sha",
            "wrong_content",
            "corrupt_gzip",
            "oversized",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory(
                prefix=f"architizer-summary-snapshot-{case}-"
            ) as root:
                fixture = seed_failed_full_finalization_fixture(Path(root))
                state_path = Path(fixture["state"])
                snapshot_root = Path(fixture["snapshots"])
                connection = sqlite3.connect(state_path)
                connection.row_factory = sqlite3.Row
                try:
                    first_url = str(fixture["urls"][0])
                    if case == "escape":
                        connection.execute(
                            """
                            UPDATE http_attempts SET gzip_path='../escape.gz'
                            WHERE run_id=? AND target_url=?
                            """,
                            (fixture["run_id"], first_url),
                        )
                    elif case == "missing":
                        relative_path = str(fixture["snapshot_manifest"][0][0])
                        (snapshot_root / relative_path).unlink()
                    elif case == "content_sha":
                        connection.execute(
                            """
                            UPDATE http_attempts SET sha256=?
                            WHERE run_id=? AND target_url=?
                            """,
                            ("0" * 64, fixture["run_id"], first_url),
                        )
                    elif case == "wrong_content":
                        relative_path = str(fixture["snapshot_manifest"][0][0])
                        response_bytes = int(
                            connection.execute(
                                """
                                SELECT response_bytes FROM http_attempts
                                WHERE run_id=? AND target_url=?
                                """,
                                (fixture["run_id"], first_url),
                            ).fetchone()[0]
                        )
                        (snapshot_root / relative_path).write_bytes(
                            gzip.compress(
                                b"x" * response_bytes,
                                compresslevel=9,
                                mtime=0,
                            )
                        )
                    elif case == "corrupt_gzip":
                        relative_path = str(fixture["snapshot_manifest"][0][0])
                        (snapshot_root / relative_path).write_bytes(b"not gzip")
                    else:
                        connection.execute(
                            """
                            UPDATE http_attempts SET response_bytes=?
                            WHERE run_id=? AND target_url=?
                            """,
                            (
                                recrawl.MAX_SNAPSHOT_RESPONSE_BYTES + 1,
                                fixture["run_id"],
                                first_url,
                            ),
                        )
                    connection.commit()
                    with self.assertRaises(recrawl.RecrawlError):
                        recrawl._network_run_summary(
                            connection,
                            run_id=int(fixture["run_id"]),
                            source_sha_before=str(fixture["source_sha"]),
                            source_sha_after=str(fixture["source_sha"]),
                            delay_seconds=2.0,
                            state_path=state_path,
                            snapshot_root=snapshot_root,
                            storage_before={
                                "state_bytes": 0,
                                "snapshot_bytes": 0,
                                "combined_bytes": 0,
                            },
                            elapsed_seconds=1.0,
                        )
                finally:
                    connection.close()

    def test_failed_full_run_offline_finalization_is_safe_and_idempotent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="architizer-finalize-full-test-"
        ) as root:
            fixture = seed_failed_full_finalization_fixture(Path(root))
            source = Path(fixture["source"])
            state_path = Path(fixture["state"])
            snapshot_root = Path(fixture["snapshots"])
            run_id = int(fixture["run_id"])
            original_source_sha = sha256(source)
            original_state_sha = sha256(state_path)
            original_snapshot_hashes = {
                relative: sha256(snapshot_root / relative)
                for relative, _ in fixture["snapshot_manifest"]
            }
            before = sqlite3.connect(state_path)
            try:
                original_finished_at = before.execute(
                    "SELECT finished_at FROM runs WHERE id=?",
                    (run_id,),
                ).fetchone()[0]
                original_counts = {
                    table: before.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
                    for table in (
                        "targets",
                        "target_reasons",
                        "run_targets",
                        "http_attempts",
                        "metadata_versions",
                        "run_metadata_versions",
                        "resolved_fields",
                        "legacy_field_comparisons",
                    )
                }
            finally:
                before.close()

            with mock.patch.object(
                recrawl.PoliteHttpClient,
                "fetch",
                autospec=True,
            ) as fetch:
                with self.assertRaises(recrawl.RecrawlError):
                    recrawl.finalize_full_run(
                        run_id=run_id,
                        confirmed=False,
                        source_path=source,
                        state_path=state_path,
                        snapshot_root=snapshot_root,
                    )
                self.assertEqual(sha256(state_path), original_state_sha)

                held_lock = recrawl.SidecarLock(state_path)
                held_lock.acquire()
                try:
                    with self.assertRaises(recrawl.LockHeldError):
                        recrawl.finalize_full_run(
                            run_id=run_id,
                            confirmed=True,
                            source_path=source,
                            state_path=state_path,
                            snapshot_root=snapshot_root,
                        )
                finally:
                    held_lock.release()

                summary = recrawl.finalize_full_run(
                    run_id=run_id,
                    confirmed=True,
                    source_path=source,
                    state_path=state_path,
                    snapshot_root=snapshot_root,
                )
                fetch.assert_not_called()

            self.assertEqual(summary["run_id"], run_id)
            self.assertEqual(summary["selected"], 5)
            self.assertEqual(summary["frozen_target_count"], 5)
            self.assertEqual(
                summary["frozen_target_urls_sha256"],
                recrawl._url_set_sha256(fixture["urls"]),
            )
            self.assertEqual(summary["newly_discovered_pending_count"], 1)
            self.assertEqual(
                summary["newly_discovered_pending_by_entity_type"],
                {"firm": 1},
            )
            self.assertTrue(summary["additional_full_phase_approval_required"])
            self.assertEqual(summary["run_elapsed_seconds"], 78_223.0)
            self.assertEqual(
                summary["runtime_storage"]["before"],
                {
                    "state_bytes": 16_384,
                    "snapshot_bytes": 8_192,
                    "combined_bytes": 24_576,
                },
            )
            recovery = summary["postprocess_recovery"]
            self.assertEqual(recovery["network_requests"], 0)
            self.assertEqual(recovery["original_status"], "failed")
            self.assertEqual(
                recovery["original_error"],
                recrawl.FULL_RUN_SQL_VARIABLE_ERROR,
            )
            self.assertEqual(recovery["original_summary"], {})
            self.assertEqual(
                recovery["original_finished_at"],
                original_finished_at,
            )
            self.assertEqual(recovery["sidecar_schema_version"], "2.1")
            provenance = summary["runtime_storage"]["before_provenance"]
            self.assertEqual(provenance["run_id"], fixture["prior_run_id"])
            self.assertEqual(
                provenance["kind"],
                "prior_completed_run_runtime_storage_after",
            )

            after = sqlite3.connect(state_path)
            try:
                run_row = after.execute(
                    """
                    SELECT status,finished_at,error,summary_json
                    FROM runs WHERE id=?
                    """,
                    (run_id,),
                ).fetchone()
                self.assertEqual(run_row[0], "completed_with_pending_discoveries")
                self.assertEqual(run_row[1], original_finished_at)
                self.assertIsNone(run_row[2])
                self.assertEqual(json.loads(run_row[3]), summary)
                self.assertEqual(
                    after.execute(
                        "SELECT value FROM state_meta WHERE key='schema_version'"
                    ).fetchone()[0],
                    "2.1",
                )
                tables = {
                    row[0]
                    for row in after.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertNotIn("snapshot_reparse_inputs", tables)
                self.assertNotIn("snapshot_reparse_lineage", tables)
                self.assertEqual(
                    {
                        table: after.execute(
                            f"SELECT COUNT(*) FROM {table}"
                        ).fetchone()[0]
                        for table in original_counts
                    },
                    original_counts,
                )
            finally:
                after.close()
            self.assertEqual(sha256(source), original_source_sha)
            self.assertEqual(
                {
                    relative: sha256(snapshot_root / relative)
                    for relative, _ in fixture["snapshot_manifest"]
                },
                original_snapshot_hashes,
            )

            state_sha_after_first = sha256(state_path)
            second = recrawl.finalize_full_run(
                run_id=run_id,
                confirmed=True,
                source_path=source,
                state_path=state_path,
                snapshot_root=snapshot_root,
            )
            self.assertEqual(second, summary)
            self.assertEqual(sha256(state_path), state_sha_after_first)

            mutable = sqlite3.connect(state_path)
            try:
                mutable.execute(
                    """
                    UPDATE targets
                    SET status='pending',last_parse_status='no_content',
                        last_error='later mutable target state'
                    WHERE url=?
                    """,
                    (fixture["urls"][0],),
                )
                mutable.commit()
            finally:
                mutable.close()
            state_sha_after_mutation = sha256(state_path)
            historical = recrawl.finalize_full_run(
                run_id=run_id,
                confirmed=True,
                source_path=source,
                state_path=state_path,
                snapshot_root=snapshot_root,
            )
            self.assertEqual(historical, summary)
            self.assertEqual(sha256(state_path), state_sha_after_mutation)

            migrated = recrawl.connect_state(
                state_path,
                source_path=source,
                source_sha256=original_source_sha,
                source_size=source.stat().st_size,
            )
            try:
                self.assertEqual(
                    migrated.execute(
                        "SELECT value FROM state_meta WHERE key='schema_version'"
                    ).fetchone()[0],
                    recrawl.STATE_SCHEMA_VERSION,
                )
                migrated_tables = {
                    row[0]
                    for row in migrated.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertIn("snapshot_reparse_inputs", migrated_tables)
                self.assertIn("snapshot_reparse_lineage", migrated_tables)
                self.assertEqual(
                    json.loads(
                        migrated.execute(
                            "SELECT summary_json FROM runs WHERE id=?",
                            (run_id,),
                        ).fetchone()[0]
                    ),
                    summary,
                )
            finally:
                migrated.close()

    def test_failed_full_run_finalization_rejects_invalid_evidence(self) -> None:
        cases = (
            (
                "wrong_error",
                "UPDATE runs SET error='ValueError: fixture' WHERE id=?",
                (),
            ),
            (
                "non_terminal_target",
                """
                UPDATE run_targets SET status_after=NULL
                WHERE run_id=? AND selection_order=1
                """,
                (),
            ),
            (
                "wrong_frozen_count",
                "UPDATE runs SET arguments_json=? WHERE id=?",
                ("arguments",),
            ),
        )
        with mock.patch.object(
            recrawl.PoliteHttpClient,
            "fetch",
            autospec=True,
        ) as fetch:
            for name, statement, mode in cases:
                with self.subTest(name=name), tempfile.TemporaryDirectory(
                    prefix=f"architizer-finalize-guard-{name}-"
                ) as root:
                    fixture = seed_failed_full_finalization_fixture(Path(root))
                    state_path = Path(fixture["state"])
                    connection = sqlite3.connect(state_path)
                    try:
                        if mode == ("arguments",):
                            arguments = json.loads(
                                connection.execute(
                                    "SELECT arguments_json FROM runs WHERE id=?",
                                    (fixture["run_id"],),
                                ).fetchone()[0]
                            )
                            arguments["frozen_target_count"] = 4
                            connection.execute(
                                statement,
                                (
                                    recrawl.canonical_json(arguments),
                                    fixture["run_id"],
                                ),
                            )
                        else:
                            connection.execute(statement, (fixture["run_id"],))
                        connection.commit()
                    finally:
                        connection.close()
                    before_sha = sha256(state_path)
                    with self.assertRaises(recrawl.RecrawlError):
                        recrawl.finalize_full_run(
                            run_id=int(fixture["run_id"]),
                            confirmed=True,
                            source_path=Path(fixture["source"]),
                            state_path=state_path,
                            snapshot_root=Path(fixture["snapshots"]),
                        )
                    self.assertEqual(sha256(state_path), before_sha)
            fetch.assert_not_called()

    def test_first_full_finalization_rejects_later_mutable_run_state(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="architizer-finalize-later-run-test-"
        ) as root:
            fixture = seed_failed_full_finalization_fixture(Path(root))
            state_path = Path(fixture["state"])
            source = Path(fixture["source"])
            connection = sqlite3.connect(state_path)
            try:
                connection.execute(
                    "UPDATE targets SET status='done',retryable=0 WHERE url=?",
                    (fixture["pending_url"],),
                )
                connection.execute(
                    """
                    INSERT INTO runs(
                        run_kind,started_at,finished_at,status,parser_version,
                        arguments_json,source_db_path,
                        source_db_sha256_before,source_db_sha256_after,
                        source_db_size,selected_count,summary_json,error
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL)
                    """,
                    (
                        "full_recrawl_v2",
                        "2026-08-05T00:00:00+00:00",
                        "2026-08-05T00:01:00+00:00",
                        "completed",
                        recrawl.PARSER_VERSION,
                        "{}",
                        str(source.resolve()),
                        fixture["source_sha"],
                        fixture["source_sha"],
                        source.stat().st_size,
                        0,
                        "{}",
                    ),
                )
                connection.commit()
            finally:
                connection.close()
            before_sha = sha256(state_path)
            with self.assertRaisesRegex(
                recrawl.RecrawlError,
                "after a subsequent run",
            ):
                recrawl.finalize_full_run(
                    run_id=int(fixture["run_id"]),
                    confirmed=True,
                    source_path=source,
                    state_path=state_path,
                    snapshot_root=Path(fixture["snapshots"]),
                )
            self.assertEqual(sha256(state_path), before_sha)
            self.assertFalse(Path(str(state_path) + ".lock").exists())

    def test_full_finalizer_remeasures_source_sha_and_size(self) -> None:
        for changed_field in ("sha256", "size"):
            with self.subTest(changed_field=changed_field), tempfile.TemporaryDirectory(
                prefix=f"architizer-finalize-source-{changed_field}-"
            ) as root:
                fixture = seed_failed_full_finalization_fixture(Path(root))
                source = Path(fixture["source"])
                state_path = Path(fixture["state"])
                source_identity = {
                    "sha256": str(fixture["source_sha"]),
                    "size": source.stat().st_size,
                }
                changed_identity = dict(source_identity)
                changed_identity[changed_field] = (
                    "0" * 64
                    if changed_field == "sha256"
                    else int(source_identity["size"]) + 1
                )
                before_state_sha = sha256(state_path)
                with mock.patch.object(
                    recrawl,
                    "_measure_stable_file_identity",
                    side_effect=[source_identity, changed_identity],
                ) as measure:
                    with self.assertRaisesRegex(
                        recrawl.RecrawlError,
                        "SHA-256/size changed",
                    ):
                        recrawl.finalize_full_run(
                            run_id=int(fixture["run_id"]),
                            confirmed=True,
                            source_path=source,
                            state_path=state_path,
                            snapshot_root=Path(fixture["snapshots"]),
                        )
                self.assertEqual(measure.call_count, 2)
                self.assertEqual(sha256(state_path), before_state_sha)

    def test_finalize_full_run_cli_dispatches_explicit_offline_gate(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="architizer-finalize-cli-test-"
        ) as root:
            root_path = Path(root)
            source = root_path / "source.db"
            state = root_path / "state.db"
            snapshots = root_path / "snapshots"
            with mock.patch.object(
                recrawl_cli,
                "finalize_full_run",
                return_value={"run_id": 15},
            ) as finalize, mock.patch("builtins.print"):
                result = recrawl_cli.main(
                    [
                        "finalize-full-run",
                        "--run-id",
                        "15",
                        "--source-db",
                        str(source),
                        "--state-db",
                        str(state),
                        "--snapshot-dir",
                        str(snapshots),
                        "--confirm-offline-finalization",
                    ]
                )
            self.assertEqual(result, 0)
            finalize.assert_called_once_with(
                run_id=15,
                confirmed=True,
                source_path=source,
                state_path=state,
                snapshot_root=snapshots,
            )

    def test_incoming_lastmod_change_semantics(self) -> None:
        self.assertTrue(
            recrawl._incoming_lastmod_changed(None, "2026-07-01")
        )
        self.assertFalse(
            recrawl._incoming_lastmod_changed(None, None)
        )
        self.assertFalse(
            recrawl._incoming_lastmod_changed(
                "2026-07-01",
                "2026-07-01",
            )
        )
        self.assertTrue(
            recrawl._incoming_lastmod_changed(
                "2026-07-01",
                "2026-07-02",
            )
        )
        self.assertFalse(
            recrawl._incoming_lastmod_changed("2026-07-01", None)
        )

    def test_full_preview_is_read_only_and_matches_expansion(self) -> None:
        with tempfile.TemporaryDirectory(prefix="architizer-preview-test-") as root:
            root_path = Path(root)
            source = root_path / "source.db"
            state_path = root_path / "state.db"
            snapshots = root_path / "snapshots"
            create_source(source)
            source_sha = sha256(source)
            connection = recrawl.connect_state(
                state_path,
                source_path=source,
                source_sha256=source_sha,
                source_size=source.stat().st_size,
            )
            null_url = "https://architizer.com/projects/null-lastmod/"
            same_url = "https://architizer.com/projects/same-lastmod/"
            changed_url = "https://architizer.com/projects/changed-lastmod/"
            inserted_url = "https://architizer.com/projects/new-project/"
            retryable_url = "https://architizer.com/projects/retryable/"
            terminal_url = "https://architizer.com/projects/terminal/"
            try:
                census_id = recrawl.start_run(
                    connection,
                    run_kind="sitemap_census",
                    source_path=source,
                    source_sha256=source_sha,
                    source_size=source.stat().st_size,
                    arguments={},
                )
                now = recrawl.utc_now()
                snapshot_id = connection.execute(
                    """
                    INSERT INTO sitemap_snapshots(
                        run_id,entity_type,sitemap_url,discovered_at,fetched_at,
                        http_status,final_url,content_type,content_bytes,sha256,
                        gzip_path,parse_status,url_count,lastmod_min,lastmod_max,
                        error
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
                    """,
                    (
                        census_id,
                        "project",
                        "https://architizer.com/project-sitemap.xml",
                        now,
                        now,
                        200,
                        "https://architizer.com/project-sitemap.xml",
                        "application/xml",
                        0,
                        "A" * 64,
                        "fixture/project.xml.gz",
                        "parsed",
                        4,
                        "2026-01-01",
                        "2026-07-02",
                    ),
                ).lastrowid
                entries = (
                    (null_url, "2026-07-01"),
                    (same_url, "2026-01-01"),
                    (changed_url, "2026-07-02"),
                    (inserted_url, "2026-07-02"),
                )
                connection.executemany(
                    """
                    INSERT INTO sitemap_entries(
                        run_id,snapshot_id,entity_type,source_url,lastmod,
                        discovery_source,discovered_at
                    ) VALUES (?,?,'project',?,?,?,?)
                    """,
                    [
                        (
                            census_id,
                            snapshot_id,
                            url,
                            lastmod,
                            "https://architizer.com/project-sitemap.xml",
                            now,
                        )
                        for url, lastmod in entries
                    ],
                )
                recrawl.finish_run(
                    connection,
                    census_id,
                    status="completed",
                    source_sha256_after=source_sha,
                    summary={"input_db_unchanged": True},
                )
                for url, lastmod in (
                    (null_url, None),
                    (same_url, "2026-01-01"),
                    (changed_url, "2026-01-01"),
                    (retryable_url, None),
                    (terminal_url, None),
                ):
                    recrawl.upsert_target(
                        connection,
                        url=url,
                        entity_type="project",
                        source_lastmod=lastmod,
                        priority=80,
                        reason="fixture",
                        discovery_source="fixture",
                        input_lineage={},
                    )
                connection.execute(
                    "UPDATE targets SET status='done',retryable=0"
                )
                connection.execute(
                    "UPDATE targets SET status='failed',retryable=1 WHERE url=?",
                    (retryable_url,),
                )
                connection.execute(
                    "UPDATE targets SET status='failed',retryable=0 WHERE url=?",
                    (terminal_url,),
                )
                connection.commit()
            finally:
                connection.close()

            state_sha_before = sha256(state_path)
            preview = recrawl.preview_full_recrawl(
                state_path=state_path,
                snapshot_root=snapshots,
            )
            self.assertEqual(sha256(state_path), state_sha_before)
            self.assertEqual(preview["would_insert_targets"], 1)
            self.assertEqual(preview["would_reschedule_lastmod_targets"], 2)
            self.assertEqual(
                preview["terminal_failures_excluded_until_explicit_retry"],
                1,
            )
            self.assertEqual(
                preview["would_reschedule_lastmod_urls_sha256"],
                recrawl._url_set_sha256((null_url, changed_url)),
            )

            writable = recrawl.connect_state(
                state_path,
                source_path=source,
                source_sha256=source_sha,
                source_size=source.stat().st_size,
            )
            try:
                self.assertEqual(
                    recrawl.expand_full_targets(writable),
                    {"project": 4},
                )
                eligible_urls = [
                    row["url"]
                    for row in recrawl._full_eligible_targets(writable)
                ]
                self.assertEqual(
                    preview["remaining_network_targets"],
                    len(eligible_urls),
                )
                self.assertEqual(
                    preview["remaining_network_target_urls_sha256"],
                    recrawl._url_set_sha256(eligible_urls),
                )
                statuses = {
                    row["url"]: (
                        row["status"],
                        row["retryable"],
                        row["source_lastmod"],
                        row["priority"],
                        row["primary_reason"],
                    )
                    for row in writable.execute(
                        """
                        SELECT url,status,retryable,source_lastmod,priority,
                               primary_reason
                        FROM targets
                        """
                    )
                }
                self.assertEqual(
                    statuses[same_url],
                    (
                        "done",
                        0,
                        "2026-01-01",
                        70,
                        "full_current_sitemap",
                    ),
                )
                self.assertEqual(
                    statuses[null_url],
                    (
                        "pending",
                        1,
                        "2026-07-01",
                        70,
                        "full_current_sitemap",
                    ),
                )
                self.assertEqual(
                    statuses[changed_url],
                    (
                        "pending",
                        1,
                        "2026-07-02",
                        70,
                        "full_current_sitemap",
                    ),
                )
                self.assertEqual(
                    statuses[retryable_url][:2],
                    ("failed", 1),
                )
                self.assertEqual(
                    statuses[terminal_url][:2],
                    ("failed", 0),
                )
            finally:
                writable.close()

    def test_full_network_report_includes_discovery_approval_gate(self) -> None:
        summary = {
            "run_id": 14,
            "selected": 2,
            "http_success": 2,
            "http_success_rate": 1.0,
            "identity_valid": 2,
            "identity_valid_rate": 1.0,
            "snapshot_saved": 2,
            "input_db_unchanged": True,
            "run_elapsed_seconds": 4.0,
            "duration_ms": {"median": 100.0},
            "recommended_request_delay_seconds": 2.0,
            "recommended_delay_reason": "fixture",
            "parse_status_counts": {"complete": 2},
            "type_stats": {},
            "field_coverage_counts": {},
            "field_coverage_rates": {},
            "field_coverage_denominators": {},
            "block_signal_counts": {},
            "legacy_field_comparison_counts": {},
            "frozen_target_count": 2,
            "frozen_target_urls_sha256": "A" * 64,
            "newly_discovered_pending_count": 3,
            "newly_discovered_pending_by_entity_type": {
                "firm": 1,
                "project": 2,
            },
            "newly_discovered_pending_urls_sha256": "B" * 64,
            "additional_full_phase_approval_required": True,
        }
        report = recrawl.render_network_report(summary, "fixture full")
        self.assertIn("## Full discovery gate", report)
        self.assertIn("- Newly discovered pending: 3", report)
        self.assertIn(
            '- Pending by entity type: {"firm":1,"project":2}',
            report,
        )
        self.assertIn(f"- Pending URL-set SHA-256: `{'B' * 64}`", report)
        self.assertIn(
            "- Additional full-phase approval required: yes",
            report,
        )

    def test_full_timing_estimate_does_not_double_count_request_delay(
        self,
    ) -> None:
        estimate = recrawl._estimate_full_timing(
            {
                "selected": 100,
                "physical_request_attempts": 100,
                "run_elapsed_seconds": 199.629,
                "duration_ms": {"median": 1117.0},
            },
            delay_seconds=2.0,
        )
        self.assertAlmostEqual(
            estimate["observed_elapsed_seconds_per_target"],
            1.99629,
        )
        self.assertEqual(estimate["seconds_per_target"], 2.0)

    def test_snapshot_integrity_and_content_addressing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="architizer-snapshot-test-") as root:
            snapshot_root = Path(root)
            digest, relative, _ = recrawl._write_gzip_snapshot(
                snapshot_root,
                kind="pages",
                content=b"stable body",
                extension="html",
            )
            digest_two, relative_two, _ = recrawl._write_gzip_snapshot(
                snapshot_root,
                kind="pages",
                content=b"stable body",
                extension="html",
            )
            self.assertEqual((digest, relative), (digest_two, relative_two))
            self.assertTrue(
                recrawl.verify_snapshot(snapshot_root, relative, digest)
            )
            (snapshot_root / relative).write_bytes(b"corrupt")
            self.assertFalse(
                recrawl.verify_snapshot(snapshot_root, relative, digest)
            )
            repaired_digest, repaired_relative, _ = (
                recrawl._write_gzip_snapshot(
                    snapshot_root,
                    kind="pages",
                    content=b"stable body",
                    extension="html",
                )
            )
            self.assertEqual(
                (repaired_digest, repaired_relative),
                (digest, relative),
            )
            self.assertTrue(
                recrawl.verify_snapshot(snapshot_root, relative, digest)
            )
            self.assertFalse(
                any(snapshot_root.rglob("*.tmp")),
                "atomic snapshot publication must clean temporary files",
            )

    def test_relationship_scheduling_requires_valid_identity_and_resolved_field(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="architizer-relation-test-") as root:
            root_path = Path(root)
            source = root_path / "source.db"
            state_path = root_path / "state.db"
            create_source(source)
            source_sha = sha256(source)
            state = recrawl.connect_state(
                state_path,
                source_path=source,
                source_sha256=source_sha,
                source_size=source.stat().st_size,
            )
            legacy = recrawl.open_legacy_readonly(source)
            try:
                run_id = recrawl.start_run(
                    state,
                    run_kind="relationship-fixture",
                    source_path=source,
                    source_sha256=source_sha,
                    source_size=source.stat().st_size,
                    arguments={},
                )
                valid_url = "https://architizer.com/projects/alpha-house/"
                recrawl.upsert_target(
                    state,
                    url=valid_url,
                    entity_type="project",
                    source_lastmod=None,
                    priority=10,
                    reason="fixture",
                    discovery_source="fixture",
                    input_lineage={},
                )
                conflicted_relation_body = PROJECT_FIXTURE.replace(
                    "https://architizer.com/firms/studio-alpha/",
                    "https://architizer.com/firms/studio-beta/",
                    1,
                ).encode()
                conflicted_relation = recrawl.parse_entity_page(
                    conflicted_relation_body,
                    requested_url=valid_url,
                    final_url=valid_url,
                    http_status=200,
                    content_type="text/html",
                    entity_type="project",
                )
                self.assertEqual(
                    conflicted_relation["identity"]["status"], "valid"
                )
                self.assertEqual(
                    conflicted_relation["resolved"]["firm_slug"]["status"],
                    "conflict",
                )
                target = dict(
                    state.execute(
                        "SELECT * FROM targets WHERE url=?",
                        (valid_url,),
                    ).fetchone()
                )
                version_id = recrawl._store_parse_result(
                    state,
                    run_id=run_id,
                    target=target,
                    snapshot_sha=recrawl.sha256_bytes(
                        conflicted_relation_body
                    ),
                    parsed=conflicted_relation,
                    legacy_connection=legacy,
                )
                self.assertEqual(
                    recrawl._schedule_discovered_relationships(
                        state,
                        version_id=version_id,
                        legacy_connection=legacy,
                    ),
                    0,
                )

                invalid_url = (
                    "https://architizer.com/projects/invalid-identity/"
                )
                recrawl.upsert_target(
                    state,
                    url=invalid_url,
                    entity_type="project",
                    source_lastmod=None,
                    priority=10,
                    reason="fixture",
                    discovery_source="fixture",
                    input_lineage={},
                )
                invalid = recrawl.parse_entity_page(
                    PROJECT_FIXTURE.encode(),
                    requested_url=invalid_url,
                    final_url=invalid_url,
                    http_status=200,
                    content_type="text/html",
                    entity_type="project",
                )
                self.assertNotEqual(invalid["identity"]["status"], "valid")
                invalid_target = dict(
                    state.execute(
                        "SELECT * FROM targets WHERE url=?",
                        (invalid_url,),
                    ).fetchone()
                )
                invalid_version = recrawl._store_parse_result(
                    state,
                    run_id=run_id,
                    target=invalid_target,
                    snapshot_sha=recrawl.sha256_bytes(
                        PROJECT_FIXTURE.encode()
                    ),
                    parsed=invalid,
                    legacy_connection=legacy,
                )
                self.assertEqual(
                    recrawl._schedule_discovered_relationships(
                        state,
                        version_id=invalid_version,
                        legacy_connection=legacy,
                    ),
                    0,
                )
                self.assertEqual(
                    state.execute(
                        "SELECT COUNT(*) FROM targets WHERE entity_type='firm'"
                    ).fetchone()[0],
                    0,
                )
            finally:
                legacy.close()
                state.close()

    def test_full_phase_reports_relationship_targets_outside_frozen_universe(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="architizer-full-phase-test-") as root:
            root_path = Path(root)
            source = root_path / "source.db"
            state_path = root_path / "state.db"
            create_source(source)
            source_sha = sha256(source)
            state = recrawl.connect_state(
                state_path,
                source_path=source,
                source_sha256=source_sha,
                source_size=source.stat().st_size,
            )
            legacy = recrawl.open_legacy_readonly(source)
            try:
                project_url = "https://architizer.com/projects/alpha-house/"
                recrawl.upsert_target(
                    state,
                    url=project_url,
                    entity_type="project",
                    source_lastmod=None,
                    priority=10,
                    reason="fixture",
                    discovery_source="fixture",
                    input_lineage={},
                )
                run_id = recrawl.start_run(
                    state,
                    run_kind="full_recrawl_v2",
                    source_path=source,
                    source_sha256=source_sha,
                    source_size=source.stat().st_size,
                    arguments={},
                )
                state.execute(
                    """
                    INSERT INTO run_targets(
                        run_id,url,selection_order,selected_reason,status_before
                    ) VALUES (?,?,?,?,?)
                    """,
                    (run_id, project_url, 1, "fixture", "pending"),
                )
                parsed = recrawl.parse_entity_page(
                    PROJECT_FIXTURE.encode(),
                    requested_url=project_url,
                    final_url=project_url,
                    http_status=200,
                    content_type="text/html",
                    entity_type="project",
                )
                target = dict(
                    state.execute(
                        "SELECT * FROM targets WHERE url=?",
                        (project_url,),
                    ).fetchone()
                )
                version_id = recrawl._store_parse_result(
                    state,
                    run_id=run_id,
                    target=target,
                    snapshot_sha=recrawl.sha256_bytes(
                        PROJECT_FIXTURE.encode()
                    ),
                    parsed=parsed,
                    legacy_connection=legacy,
                )
                self.assertGreater(
                    recrawl._schedule_discovered_relationships(
                        state,
                        version_id=version_id,
                        legacy_connection=legacy,
                    ),
                    0,
                )
                pending = recrawl._pending_discoveries_for_run(
                    state,
                    run_id=run_id,
                )
                self.assertEqual(
                    [row["url"] for row in pending],
                    ["https://architizer.com/firms/studio-alpha/"],
                )
            finally:
                legacy.close()
                state.close()

    def test_second_process_lock_is_exclusive(self) -> None:
        with tempfile.TemporaryDirectory(prefix="architizer-lock-test-") as root:
            state_path = Path(root) / "state.db"
            first = recrawl.SidecarLock(state_path)
            second = recrawl.SidecarLock(state_path)
            first.acquire()
            try:
                with self.assertRaises(recrawl.LockHeldError):
                    second.acquire()
            finally:
                first.release()
            second.acquire()
            second.release()

    def test_stale_lock_recovery_requires_dead_pid_age_and_confirmation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="architizer-stale-lock-test-") as root:
            state_path = (Path(root) / "state.db").resolve()
            lock_path = Path(str(state_path) + ".lock")
            lock_path.write_text(
                json.dumps(
                    {
                        "pid": 2_000_000_000,
                        "acquired_at": "2020-01-01T00:00:00+00:00",
                        "state": str(state_path),
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(recrawl.RecrawlError):
                recrawl.recover_stale_sidecar_lock(
                    state_path,
                    confirmed=False,
                    minimum_age_seconds=0,
                )
            result = recrawl.recover_stale_sidecar_lock(
                state_path,
                confirmed=True,
                minimum_age_seconds=0,
            )
            self.assertTrue(result["removed"])
            self.assertFalse(lock_path.exists())

    def test_resume_changed_lastmod_failed_retry_and_idempotent_reason(self) -> None:
        with tempfile.TemporaryDirectory(prefix="architizer-state-test-") as root:
            state_path = Path(root) / "state.db"
            connection = recrawl.connect_state(state_path)
            try:
                url = "https://architizer.com/projects/a/"
                recrawl.upsert_target(
                    connection,
                    url=url,
                    entity_type="project",
                    source_lastmod="2026-01-01",
                    priority=20,
                    reason="sitemap_modified",
                    discovery_source="fixture",
                    input_lineage={"fixture": 1},
                )
                connection.execute(
                    "UPDATE targets SET status='done' WHERE url=?", (url,)
                )
                connection.commit()
                recrawl.upsert_target(
                    connection,
                    url=url,
                    entity_type="project",
                    source_lastmod="2026-02-01",
                    priority=20,
                    reason="sitemap_modified",
                    discovery_source="fixture",
                    input_lineage={"fixture": 2},
                )
                row = connection.execute(
                    "SELECT status,source_lastmod FROM targets WHERE url=?",
                    (url,),
                ).fetchone()
                self.assertEqual(tuple(row), ("pending", "2026-02-01"))
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM target_reasons WHERE url=?", (url,)
                    ).fetchone()[0],
                    1,
                )
                connection.execute(
                    """
                    UPDATE targets
                    SET status='failed',retryable=0 WHERE url=?
                    """,
                    (url,),
                )
                connection.commit()
                recrawl.upsert_target(
                    connection,
                    url=url,
                    entity_type="project",
                    source_lastmod="2026-02-01",
                    priority=30,
                    reason="legacy_failed_retry",
                    discovery_source="legacy",
                    input_lineage={"error": "fixture"},
                    reschedule=True,
                )
                row = connection.execute(
                    "SELECT status,retryable FROM targets WHERE url=?", (url,)
                ).fetchone()
                self.assertEqual(tuple(row), ("pending", 1))
                null_url = "https://architizer.com/projects/null-lastmod/"
                recrawl.upsert_target(
                    connection,
                    url=null_url,
                    entity_type="project",
                    source_lastmod=None,
                    priority=20,
                    reason="fixture",
                    discovery_source="fixture",
                    input_lineage={},
                )
                connection.execute(
                    "UPDATE targets SET status='done' WHERE url=?",
                    (null_url,),
                )
                connection.commit()
                recrawl.upsert_target(
                    connection,
                    url=null_url,
                    entity_type="project",
                    source_lastmod="2026-03-01",
                    priority=20,
                    reason="fixture",
                    discovery_source="fixture",
                    input_lineage={},
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT status FROM targets WHERE url=?", (null_url,)
                    ).fetchone()[0],
                    "pending",
                )

                source = Path(root) / "source.db"
                create_source(source)
                run_id = recrawl.start_run(
                    connection,
                    run_kind="fixture",
                    source_path=source,
                    source_sha256=sha256(source),
                    source_size=source.stat().st_size,
                    arguments={},
                )
                connection.execute(
                    "UPDATE targets SET status='in_progress' WHERE url=?", (url,)
                )
                connection.commit()
                self.assertEqual(recrawl.recover_interrupted_state(connection), 1)
                self.assertEqual(
                    connection.execute(
                        "SELECT status FROM targets WHERE url=?", (url,)
                    ).fetchone()[0],
                    "pending",
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT status FROM runs WHERE id=?", (run_id,)
                    ).fetchone()[0],
                    "interrupted",
                )
            finally:
                connection.close()

    def test_missing_or_conflicting_new_fields_do_not_clobber_current(self) -> None:
        with tempfile.TemporaryDirectory(prefix="architizer-clobber-test-") as root:
            root_path = Path(root)
            source = root_path / "source.db"
            state_path = root_path / "state.db"
            create_source(source)
            state = recrawl.connect_state(state_path)
            legacy = recrawl.open_legacy_readonly(source)
            try:
                target_url = "https://architizer.com/projects/alpha-house/"
                recrawl.upsert_target(
                    state,
                    url=target_url,
                    entity_type="project",
                    source_lastmod="2026-07-01",
                    priority=10,
                    reason="fixture",
                    discovery_source="fixture",
                    input_lineage={},
                )
                run_id = recrawl.start_run(
                    state,
                    run_kind="fixture",
                    source_path=source,
                    source_sha256=sha256(source),
                    source_size=source.stat().st_size,
                    arguments={},
                )
                target = dict(
                    state.execute(
                        "SELECT * FROM targets WHERE url=?", (target_url,)
                    ).fetchone()
                )
                first_body = project_fixture(description="Keep me")
                first = recrawl.parse_entity_page(
                    first_body,
                    requested_url=target_url,
                    final_url=target_url,
                    http_status=200,
                    content_type="text/html",
                    entity_type="project",
                )
                first_version = recrawl._store_parse_result(
                    state,
                    run_id=run_id,
                    target=target,
                    snapshot_sha=recrawl.sha256_bytes(first_body),
                    parsed=first,
                    legacy_connection=legacy,
                )
                repeat_run_id = recrawl.start_run(
                    state,
                    run_kind="fixture-repeat",
                    source_path=source,
                    source_sha256=sha256(source),
                    source_size=source.stat().st_size,
                    arguments={},
                )
                repeated_version = recrawl._store_parse_result(
                    state,
                    run_id=repeat_run_id,
                    target=target,
                    snapshot_sha=recrawl.sha256_bytes(first_body),
                    parsed=first,
                    legacy_connection=legacy,
                )
                self.assertEqual(first_version, repeated_version)
                self.assertEqual(
                    state.execute(
                        """
                        SELECT COUNT(*) FROM run_metadata_versions
                        WHERE version_id=?
                        """,
                        (first_version,),
                    ).fetchone()[0],
                    2,
                )
                missing_body = project_fixture(description=None)
                missing = recrawl.parse_entity_page(
                    missing_body,
                    requested_url=target_url,
                    final_url=target_url,
                    http_status=200,
                    content_type="text/html",
                    entity_type="project",
                )
                recrawl._store_parse_result(
                    state,
                    run_id=run_id,
                    target=target,
                    snapshot_sha=recrawl.sha256_bytes(missing_body),
                    parsed=missing,
                    legacy_connection=legacy,
                )
                conflict_body = project_fixture(
                    description=None,
                    dom_name="Conflicting DOM Name",
                )
                conflict = recrawl.parse_entity_page(
                    conflict_body,
                    requested_url=target_url,
                    final_url=target_url,
                    http_status=200,
                    content_type="text/html",
                    entity_type="project",
                )
                self.assertEqual(
                    conflict["resolved"]["name"]["status"], "conflict"
                )
                recrawl._store_parse_result(
                    state,
                    run_id=run_id,
                    target=target,
                    snapshot_sha=recrawl.sha256_bytes(conflict_body),
                    parsed=conflict,
                    legacy_connection=legacy,
                )
                current = {
                    row["field_name"]: json.loads(row["value_json"])
                    for row in state.execute(
                        """
                        SELECT field_name,value_json FROM current_fields
                        WHERE target_url=?
                        """,
                        (target_url,),
                    )
                }
                self.assertEqual(current["description"], "Keep me")
                self.assertEqual(current["name"], "Alpha House")
                self.assertEqual(
                    state.execute(
                        """
                        SELECT COUNT(*) FROM resolved_fields
                        WHERE field_name='name' AND status='conflict'
                        """
                    ).fetchone()[0],
                    1,
                )
            finally:
                legacy.close()
                state.close()

    def test_full_crawl_requires_explicit_confirmation(self) -> None:
        with self.assertRaises(recrawl.RecrawlError):
            recrawl.run_full_recrawl(confirmed=False)

    def test_confirmed_full_with_missing_ladder_never_fetches(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="architizer-full-gate-test-"
        ) as root:
            root_path = Path(root)
            source = root_path / "source.db"
            state = root_path / "state.db"
            snapshots = root_path / "snapshots"
            create_source(source)
            with mock.patch.object(
                recrawl.PoliteHttpClient,
                "fetch",
                autospec=True,
            ) as fetch:
                with self.assertRaises(recrawl.RecrawlError):
                    recrawl.run_full_recrawl(
                        confirmed=True,
                        source_path=source,
                        state_path=state,
                        snapshot_root=snapshots,
                        delay_seconds=2.0,
                        max_attempts=1,
                    )
                fetch.assert_not_called()

    def test_full_delay_cannot_undercut_n100_recommendation(self) -> None:
        summary = {"recommended_request_delay_seconds": 2.0}
        self.assertEqual(
            recrawl._validate_full_delay(summary, delay_seconds=2.0),
            2.0,
        )
        with self.assertRaises(recrawl.RecrawlError):
            recrawl._validate_full_delay(summary, delay_seconds=0.0)
        with self.assertRaises(recrawl.RecrawlError):
            recrawl._validate_full_delay(
                {"recommended_request_delay_seconds": 3.5},
                delay_seconds=3.0,
            )
        with self.assertRaises(recrawl.RecrawlError):
            recrawl._validate_full_delay(
                summary,
                delay_seconds=float("nan"),
            )

    def test_source_state_alias_is_rejected_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory(prefix="architizer-alias-test-") as root:
            source = Path(root) / "source.db"
            snapshots = Path(root) / "snapshots"
            create_source(source)
            before = sha256(source)
            with self.assertRaises(recrawl.RecrawlError):
                recrawl.run_source_census(
                    source_path=source,
                    state_path=source,
                    snapshot_root=snapshots,
                    delay_seconds=0,
                    max_attempts=1,
                )
            self.assertEqual(sha256(source), before)

    def test_sidecar_is_bound_to_one_immutable_source(self) -> None:
        with tempfile.TemporaryDirectory(prefix="architizer-binding-test-") as root:
            root_path = Path(root)
            first = root_path / "first.db"
            second = root_path / "second.db"
            state = root_path / "state.db"
            create_source(first)
            create_source(second)
            second_connection = sqlite3.connect(second)
            try:
                second_connection.execute(
                    "UPDATE architizer_projects SET name='Different'"
                )
                second_connection.commit()
            finally:
                second_connection.close()
            connection = recrawl.connect_state(
                state,
                source_path=first,
                source_sha256=sha256(first),
                source_size=first.stat().st_size,
            )
            connection.close()
            with self.assertRaises(recrawl.RecrawlError):
                recrawl.connect_state(
                    state,
                    source_path=second,
                    source_sha256=sha256(second),
                    source_size=second.stat().st_size,
                )

    def test_populated_unbound_sidecar_requires_matching_run_lineage(self) -> None:
        with tempfile.TemporaryDirectory(prefix="architizer-unbound-test-") as root:
            root_path = Path(root)
            first = root_path / "first.db"
            second = root_path / "second.db"
            state = root_path / "state.db"
            create_source(first)
            create_source(second)
            second_connection = sqlite3.connect(second)
            try:
                second_connection.execute(
                    "UPDATE architizer_projects SET name='Different source'"
                )
                second_connection.commit()
            finally:
                second_connection.close()
            unbound = recrawl.connect_state(state)
            try:
                recrawl.start_run(
                    unbound,
                    run_kind="legacy-unbound-fixture",
                    source_path=first,
                    source_sha256=sha256(first),
                    source_size=first.stat().st_size,
                    arguments={},
                )
            finally:
                unbound.close()
            with self.assertRaises(recrawl.RecrawlError):
                recrawl.connect_state(
                    state,
                    source_path=second,
                    source_sha256=sha256(second),
                    source_size=second.stat().st_size,
                )
            matching = recrawl.connect_state(
                state,
                source_path=first,
                source_sha256=sha256(first),
                source_size=first.stat().st_size,
            )
            matching.close()

    def test_ladder_is_enforced_by_source_census_and_parser_version(self) -> None:
        with tempfile.TemporaryDirectory(prefix="architizer-ladder-test-") as root:
            root_path = Path(root)
            source = root_path / "source.db"
            state_path = root_path / "state.db"
            create_source(source)
            source_sha = sha256(source)
            connection = recrawl.connect_state(
                state_path,
                source_path=source,
                source_sha256=source_sha,
                source_size=source.stat().st_size,
            )
            try:
                census_id = recrawl.start_run(
                    connection,
                    run_kind="sitemap_census",
                    source_path=source,
                    source_sha256=source_sha,
                    source_size=source.stat().st_size,
                    arguments={},
                )
                recrawl.finish_run(
                    connection,
                    census_id,
                    status="completed",
                    source_sha256_after=source_sha,
                    summary={"input_db_unchanged": True},
                )
                with self.assertRaises(recrawl.RecrawlError):
                    recrawl.validate_smoke_ladder_for_n100(
                        connection,
                        census_run_id=census_id,
                        source_sha256=source_sha,
                    )
                bad_n10_id = recrawl.start_run(
                    connection,
                    run_kind="network_smoke_n10",
                    source_path=source,
                    source_sha256=source_sha,
                    source_size=source.stat().st_size,
                    arguments={"census_run_id": census_id},
                )
                recrawl.finish_run(
                    connection,
                    bad_n10_id,
                    status="completed",
                    source_sha256_after=source_sha,
                    summary={
                        "input_db_unchanged": True,
                        "gate_policy_version": recrawl.SMOKE_GATE_POLICY_VERSION,
                        "gate_passed": True,
                        "gate_failures": [],
                    },
                    selected_count=10,
                )
                with self.assertRaises(recrawl.RecrawlError):
                    recrawl.validate_smoke_ladder_for_n100(
                        connection,
                        census_run_id=census_id,
                        source_sha256=source_sha,
                    )
                n10_id = recrawl.start_run(
                    connection,
                    run_kind="network_smoke_n10",
                    source_path=source,
                    source_sha256=source_sha,
                    source_size=source.stat().st_size,
                    arguments={"census_run_id": census_id},
                )
                recrawl.finish_run(
                    connection,
                    n10_id,
                    status="completed",
                    source_sha256_after=source_sha,
                    summary=passing_smoke_summary(10),
                    selected_count=10,
                )
                recrawl.validate_smoke_ladder_for_n100(
                    connection,
                    census_run_id=census_id,
                    source_sha256=source_sha,
                )
                with self.assertRaises(recrawl.RecrawlError):
                    recrawl.validate_full_ladder(
                        connection,
                        source_sha256=source_sha,
                    )
                n100_id = recrawl.start_run(
                    connection,
                    run_kind="network_smoke_n100",
                    source_path=source,
                    source_sha256=source_sha,
                    source_size=source.stat().st_size,
                    arguments={"census_run_id": census_id},
                )
                recrawl.finish_run(
                    connection,
                    n100_id,
                    status="quality_failed",
                    source_sha256_after=source_sha,
                    summary=passing_smoke_summary(100),
                    selected_count=100,
                )
                self.assertEqual(
                    recrawl.validate_full_ladder(
                        connection,
                        source_sha256=source_sha,
                    ),
                    {
                        "census_run_id": census_id,
                        "n10_run_id": n10_id,
                        "n100_run_id": n100_id,
                    },
                )
            finally:
                connection.close()

    def test_smoke_quality_gate_allows_only_declared_identity_exception(
        self,
    ) -> None:
        base = passing_smoke_summary(100)
        base.update(
            {
                "identity_valid": 99,
                "identity_exception_details": [
                    {
                        "url": (
                            "https://architizer.com/projects/"
                            "requiem-for-ruins-2/"
                        ),
                        "final_url": (
                            "https://architizer.com/firms/"
                            "multitude-of-sins/?notfound_project=1"
                        ),
                        "identity_status": "conflict",
                        "parse_status": "no_content",
                        "errors": [
                            "final_url_slug_mismatch",
                            "canonical_slug_mismatch",
                            "global_id_wrong_entity_type",
                        ],
                        "known_exception_reason": "forged-input-is-ignored",
                    }
                ],
                "parse_status_counts": {"complete": 99, "no_content": 1},
                "parse_exception_details": [
                    {
                        "url": (
                            "https://architizer.com/projects/"
                            "requiem-for-ruins-2/"
                        ),
                        "final_url": (
                            "https://architizer.com/firms/"
                            "multitude-of-sins/?notfound_project=1"
                        ),
                        "identity_status": "conflict",
                        "known_exception_reason": "forged-input-is-ignored",
                    }
                ],
            }
        )
        allowed = recrawl.evaluate_smoke_quality(base, smoke_size=100)
        self.assertTrue(allowed["gate_passed"])
        base["identity_exception_details"] = [
            {
                "url": "https://architizer.com/projects/unexpected/",
                "identity_status": "missing",
                "parse_status": "no_content",
                "errors": [],
                "known_exception_reason": "forged-known-reason",
            }
        ]
        rejected = recrawl.evaluate_smoke_quality(base, smoke_size=100)
        self.assertFalse(rejected["gate_passed"])
        self.assertTrue(
            any(
                "unexpected identity" in failure
                for failure in rejected["gate_failures"]
            )
        )

    def test_smoke_gate_bounded_verified_notfound_is_evidence_based(
        self,
    ) -> None:
        def summary_with_absences(
            size: int,
            count: int,
        ) -> dict[str, object]:
            summary = passing_smoke_summary(size)
            absences = [
                {
                    "url": (
                        "https://architizer.com/firms/"
                        f"missing-firm-{index}/"
                    ),
                    "final_url": "https://architizer.com/firms/?notfound=1",
                    "identity_status": "conflict",
                    "parse_status": "no_content",
                    "errors": [
                        "final_url_slug_mismatch",
                        "canonical_slug_mismatch",
                    ],
                    "known_exception_reason": "forged-input-is-ignored",
                }
                for index in range(count)
            ]
            summary.update(
                {
                    "identity_valid": size - count,
                    "identity_exception_details": absences,
                    "parse_status_counts": {
                        "complete": size - count,
                        "no_content": count,
                    },
                    "parse_exception_details": [
                        {
                            "url": absence["url"],
                            "final_url": absence["final_url"],
                            "identity_status": "conflict",
                        }
                        for absence in absences
                    ],
                }
            )
            return summary

        summary = summary_with_absences(100, 1)
        allowed = recrawl.evaluate_smoke_quality(summary, smoke_size=100)
        self.assertTrue(allowed["gate_passed"])
        self.assertEqual(
            len(allowed["verified_source_absences_observed"]),
            1,
        )
        summary["identity_exception_details"] = [
            {
                **summary["identity_exception_details"][0],
                "final_url": "https://example.com/firms/?notfound=1",
            }
        ]
        rejected = recrawl.evaluate_smoke_quality(summary, smoke_size=100)
        self.assertFalse(rejected["gate_passed"])

        n10_rejected = recrawl.evaluate_smoke_quality(
            summary_with_absences(10, 1),
            smoke_size=10,
        )
        self.assertFalse(n10_rejected["gate_passed"])
        self.assertEqual(
            n10_rejected["gate_thresholds"][
                "verified_source_absence_max"
            ],
            0,
        )

        n100_at_boundary = recrawl.evaluate_smoke_quality(
            summary_with_absences(100, 5),
            smoke_size=100,
        )
        self.assertTrue(n100_at_boundary["gate_passed"])
        n100_over_boundary = recrawl.evaluate_smoke_quality(
            summary_with_absences(100, 6),
            smoke_size=100,
        )
        self.assertFalse(n100_over_boundary["gate_passed"])
        self.assertTrue(
            any(
                "exceed 5%" in failure
                for failure in n100_over_boundary["gate_failures"]
            )
        )

    def test_current_award_seed_bucket_and_unattempted_resume(self) -> None:
        with tempfile.TemporaryDirectory(prefix="architizer-select-test-") as root:
            state_path = Path(root) / "state.db"
            connection = recrawl.connect_state(state_path)
            try:
                urls = [
                    f"https://architizer.com/projects/selection-{index}/"
                    for index in range(10)
                ]
                for index, url in enumerate(urls):
                    recrawl.upsert_target(
                        connection,
                        url=url,
                        entity_type="project",
                        source_lastmod=None,
                        priority=45 if index == 0 else 80,
                        reason=(
                            "award_2026_project_seed"
                            if index == 0
                            else "fixture"
                        ),
                        discovery_source="fixture",
                        input_lineage={},
                    )
                source = Path(root) / "source.db"
                create_source(source)
                failed_run = recrawl.start_run(
                    connection,
                    run_kind="network_smoke_n10",
                    source_path=source,
                    source_sha256=sha256(source),
                    source_size=source.stat().st_size,
                    arguments={},
                )
                connection.execute(
                    """
                    INSERT INTO run_targets(
                        run_id,url,selection_order,selected_reason,status_before,
                        status_after
                    ) VALUES (?,?,?,?,?,NULL)
                    """,
                    (failed_run, urls[1], 1, "fixture", "pending"),
                )
                recrawl.finish_run(
                    connection,
                    failed_run,
                    status="failed",
                    source_sha256_after=sha256(source),
                    summary={},
                )
                self.assertNotIn(
                    urls[1],
                    recrawl._selected_in_prior_smoke(connection),
                )
                selected = recrawl.select_network_targets(
                    connection,
                    smoke_size=10,
                    run_kind="network_smoke_n10",
                )
                self.assertIn(
                    ("award_seed", urls[0]),
                    {
                        (row["selected_reason"], row["url"])
                        for row in selected
                    },
                )
            finally:
                connection.close()

    def test_legacy_recovery_is_revalidated_once_per_parser_version(self) -> None:
        with tempfile.TemporaryDirectory(prefix="architizer-parser-smoke-") as root:
            state_path = Path(root) / "state.db"
            connection = recrawl.connect_state(state_path)
            try:
                recovery_url = "https://architizer.com/projects/legacy-failed/"
                recrawl.upsert_target(
                    connection,
                    url=recovery_url,
                    entity_type="project",
                    source_lastmod="2026-04-16",
                    priority=30,
                    reason="legacy_failed_retry",
                    discovery_source="legacy",
                    input_lineage={},
                )
                connection.execute(
                    "UPDATE targets SET status='done',retryable=0 WHERE url=?",
                    (recovery_url,),
                )
                for index in range(10):
                    recrawl.upsert_target(
                        connection,
                        url=f"https://architizer.com/projects/fill-{index}/",
                        entity_type="project",
                        source_lastmod=None,
                        priority=80,
                        reason="fixture",
                        discovery_source="fixture",
                        input_lineage={},
                    )
                source = Path(root) / "source.db"
                create_source(source)
                source_sha = sha256(source)
                old_run = recrawl.start_run(
                    connection,
                    run_kind="network_smoke_n10",
                    source_path=source,
                    source_sha256=source_sha,
                    source_size=source.stat().st_size,
                    arguments={},
                )
                connection.execute(
                    "UPDATE runs SET parser_version='old-parser' WHERE id=?",
                    (old_run,),
                )
                connection.execute(
                    """
                    INSERT INTO run_targets(
                        run_id,url,selection_order,selected_reason,status_before,
                        status_after
                    ) VALUES (?,?,?,?,?,?)
                    """,
                    (
                        old_run,
                        recovery_url,
                        1,
                        "legacy_recovery",
                        "pending",
                        "done",
                    ),
                )
                recrawl.finish_run(
                    connection,
                    old_run,
                    status="completed",
                    source_sha256_after=source_sha,
                    summary={},
                    selected_count=1,
                )
                selected = recrawl.select_network_targets(
                    connection,
                    smoke_size=10,
                    run_kind="network_smoke_n10",
                )
                self.assertIn(
                    ("legacy_recovery", recovery_url),
                    {
                        (row["selected_reason"], row["url"])
                        for row in selected
                    },
                )

                current_run = recrawl.start_run(
                    connection,
                    run_kind="network_smoke_n10",
                    source_path=source,
                    source_sha256=source_sha,
                    source_size=source.stat().st_size,
                    arguments={},
                )
                connection.execute(
                    """
                    INSERT INTO run_targets(
                        run_id,url,selection_order,selected_reason,status_before,
                        status_after
                    ) VALUES (?,?,?,?,?,?)
                    """,
                    (
                        current_run,
                        recovery_url,
                        1,
                        "legacy_recovery",
                        "done",
                        "done",
                    ),
                )
                recrawl.finish_run(
                    connection,
                    current_run,
                    status="completed",
                    source_sha256_after=source_sha,
                    summary={
                        "gate_policy_version": (
                            recrawl.SMOKE_GATE_POLICY_VERSION
                        ),
                        "gate_passed": True,
                    },
                    selected_count=1,
                )
                self.assertIn(
                    recovery_url,
                    recrawl._selected_in_prior_smoke(connection),
                )
                selected = recrawl.select_network_targets(
                    connection,
                    smoke_size=10,
                    run_kind="network_smoke_n10",
                )
                self.assertNotIn(
                    recovery_url,
                    {row["url"] for row in selected},
                )
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
