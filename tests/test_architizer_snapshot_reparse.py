"""Offline-only tests for Architizer firm snapshot reparsing."""

from __future__ import annotations

import gzip
import hashlib
import html
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from crawl.architizer import recrawl_v2 as recrawl


OLD_PARSER = "architizer-source-parser-v2.2.0"
OLD_METADATA = "architizer-source-metadata-v2.2"


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def firm_page(slug: str, location: str) -> bytes:
    firm_payload = html.escape(
        json.dumps(
            {
                "global_id": f"firms.firm.{slug.rsplit('-', 1)[-1]}",
                "name": f"Studio {slug}",
                "absolute_url": f"/firms/{slug}/",
            }
        ),
        quote=True,
    )
    location_payload = html.escape(
        json.dumps(
            {
                "global_id": "locations.geolocation.1000",
                "pk": 1000,
                "for_humans": location,
            }
        ),
        quote=True,
    )
    return (
        "<!doctype html><html><head>"
        f"<title>Studio {slug} | Architizer</title>"
        f"<link rel='canonical' href='https://architizer.com/firms/{slug}/'>"
        "</head><body>"
        f"<h1>Studio {slug}</h1><div data-data='{firm_payload}'></div>"
        f"<div id='{slug}-locations'><div data-data='{location_payload}'>"
        "<span class='grey icon marker'></span>"
        "<span class='placeholder single-line js-rendered-content'>"
        f"{html.escape(location)}</span></div></div>"
        + "x" * 600
        + "</body></html>"
    ).encode()


def project_regression_page() -> bytes:
    payload = html.escape(
        json.dumps(
            {
                "pk": 176967,
                "global_id": "projects.project.176967",
                "slug": "architektursalon",
                "absolute_url": "/projects/architektursalon/",
                "name": "Architektursalon",
                "description": "A literal &quot;quoted&quot; source value",
            }
        ),
        quote=True,
    )
    return (
        "<!doctype html><html><head><title>Architektursalon | Architizer</title>"
        "<link rel='canonical' href='https://architizer.com/projects/"
        "architektursalon/'></head><body><h1>Architektursalon</h1>"
        f"<div data-data='{payload}'></div>"
        + "x" * 600
        + "</body></html>"
    ).encode()


def create_legacy_source(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE architizer_firms(
            slug TEXT PRIMARY KEY,
            name TEXT,
            description TEXT,
            office_locations TEXT,
            social_links TEXT
        );
        CREATE TABLE architizer_projects(
            id INTEGER PRIMARY KEY,
            global_id TEXT,
            slug TEXT,
            name TEXT,
            firm_slug TEXT,
            firm_name TEXT,
            location_full TEXT,
            completion_year INTEGER,
            constr_status TEXT,
            building_size_slug TEXT,
            building_size_display TEXT,
            description TEXT,
            description_short TEXT,
            categories TEXT,
            cover_image_url TEXT,
            gallery_image_urls TEXT,
            image_global_ids TEXT,
            published_time TEXT,
            modified_time TEXT
        );
        """
    )
    connection.commit()
    connection.close()


def seed_network_firms(root: Path, count: int) -> dict[str, object]:
    source = root / "source.db"
    state_path = root / "state.db"
    snapshots = root / "snapshots"
    snapshots.mkdir()
    create_legacy_source(source)
    source_sha = file_sha256(source)
    state = recrawl.connect_state(
        state_path,
        source_path=source,
        source_sha256=source_sha,
        source_size=source.stat().st_size,
    )
    run_id = recrawl.start_run(
        state,
        run_kind="fixture_network_full",
        source_path=source,
        source_sha256=source_sha,
        source_size=source.stat().st_size,
        arguments={},
    )
    state.execute(
        "UPDATE runs SET parser_version=? WHERE id=?",
        (OLD_PARSER, run_id),
    )
    urls: list[str] = []
    gzip_paths: dict[str, Path] = {}
    for index in range(count):
        slug = f"fixture-firm-{index:03d}"
        url = f"https://architizer.com/firms/{slug}/"
        urls.append(url)
        body = firm_page(slug, f"City {index}, Country")
        content_sha = recrawl.sha256_bytes(body)
        relative = f"pages/{content_sha}.html.gz"
        gzip_path = snapshots / relative
        gzip_path.parent.mkdir(parents=True, exist_ok=True)
        gzip_path.write_bytes(gzip.compress(body, mtime=0))
        gzip_paths[url] = gzip_path
        recrawl.upsert_target(
            state,
            url=url,
            entity_type="firm",
            source_lastmod="2026-07-01",
            priority=20,
            reason="fixture",
            discovery_source="fixture",
            input_lineage={},
        )
        state.execute(
            """
            UPDATE targets
            SET status='done',retryable=0,attempt_count=3,
                last_attempt_at='2026-07-31T00:00:00+00:00',
                last_error=NULL,last_http_status=200,
                last_snapshot_sha256=?,last_parse_status='complete'
            WHERE url=?
            """,
            (content_sha, url),
        )
        attempt_id = state.execute(
            """
            INSERT INTO http_attempts(
                run_id,target_url,request_kind,requested_url,attempt_number,
                started_at,finished_at,duration_ms,outcome,http_status,
                final_url,content_type,response_bytes,sha256,gzip_path,
                retryable,block_signals_json,error
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                url,
                "firm_page",
                url,
                1,
                "2026-07-31T00:00:00+00:00",
                "2026-07-31T00:00:01+00:00",
                1000,
                "success",
                200,
                url,
                "text/html; charset=utf-8",
                len(body),
                content_sha,
                relative,
                0,
                "[]",
                None,
            ),
        ).lastrowid
        version_id = state.execute(
            """
            INSERT INTO metadata_versions(
                run_id,target_url,entity_type,snapshot_sha256,parser_version,
                metadata_version,parsed_at,parse_status,quality,identity_status,
                identity_json,raw_embedded_json,dom_json,resolved_json,
                conflict_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                url,
                "firm",
                content_sha,
                OLD_PARSER,
                OLD_METADATA,
                "2026-07-31T00:00:01+00:00",
                "complete",
                "high",
                "valid",
                json.dumps({"status": "valid"}),
                "[]",
                "{}",
                "{}",
                "{}",
            ),
        ).lastrowid
        state.execute(
            "INSERT INTO run_metadata_versions VALUES (?,?,?)",
            (run_id, version_id, url),
        )
        state.execute(
            """
            INSERT INTO resolved_fields(
                version_id,field_name,value_json,status,quality,conflict_json
            ) VALUES (?,?,?,?,?,NULL)
            """,
            (version_id, "description", json.dumps("Keep me"), "confirmed", "high"),
        )
        state.execute(
            """
            INSERT INTO current_fields(
                target_url,field_name,value_json,status,quality,version_id,updated_at
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (
                url,
                "description",
                json.dumps("Keep me"),
                "confirmed",
                "high",
                version_id,
                "2026-07-31T00:00:01+00:00",
            ),
        )
        state.execute(
            """
            UPDATE targets
            SET current_metadata_version_id=?,last_good_version_id=?
            WHERE url=?
            """,
            (version_id, version_id, url),
        )
        self_check = state.execute(
            "SELECT id FROM http_attempts WHERE id=?", (attempt_id,)
        ).fetchone()
        assert self_check is not None
    state.commit()
    recrawl.finish_run(
        state,
        run_id,
        status="completed",
        source_sha256_after=source_sha,
        summary={},
        selected_count=count,
    )
    state.close()
    return {
        "source": source,
        "state": state_path,
        "snapshots": snapshots,
        "urls": urls,
        "gzip_paths": gzip_paths,
        "source_run_id": run_id,
    }


def network_states(state_path: Path) -> dict[str, dict[str, object]]:
    connection = recrawl.open_state_readonly(state_path)
    try:
        return {
            row["url"]: {
                field: row[field] for field in recrawl.TARGET_NETWORK_STATE_FIELDS
            }
            for row in connection.execute("SELECT * FROM targets ORDER BY url")
        }
    finally:
        connection.close()


def add_project_parser_regression(fixture: dict[str, object]) -> str:
    source = Path(fixture["source"])
    state_path = Path(fixture["state"])
    snapshots = Path(fixture["snapshots"])
    source_sha = file_sha256(source)
    state = recrawl.connect_state(
        state_path,
        source_path=source,
        source_sha256=source_sha,
        source_size=source.stat().st_size,
    )
    run_id = recrawl.start_run(
        state,
        run_kind="fixture-project-parser-regression",
        source_path=source,
        source_sha256=source_sha,
        source_size=source.stat().st_size,
        arguments={},
    )
    state.execute(
        "UPDATE runs SET parser_version=? WHERE id=?", (OLD_PARSER, run_id)
    )
    url = "https://architizer.com/projects/architektursalon/"
    body = project_regression_page()
    content_sha = recrawl.sha256_bytes(body)
    relative = f"pages/{content_sha}.html.gz"
    path = snapshots / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(body, mtime=0))
    recrawl.upsert_target(
        state,
        url=url,
        entity_type="project",
        source_lastmod="2026-08-03",
        priority=10,
        reason="fixture_parser_regression",
        discovery_source="fixture",
        input_lineage={},
    )
    state.execute(
        """
        UPDATE targets
        SET status='failed',retryable=0,attempt_count=1,
            last_attempt_at='2026-08-04T00:00:00+00:00',
            last_error='identity missing',last_http_status=200,
            last_snapshot_sha256=?,last_parse_status='no_content'
        WHERE url=?
        """,
        (content_sha, url),
    )
    state.execute(
        """
        INSERT INTO http_attempts(
            run_id,target_url,request_kind,requested_url,attempt_number,
            started_at,finished_at,duration_ms,outcome,http_status,final_url,
            content_type,response_bytes,sha256,gzip_path,retryable,
            block_signals_json,error
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            url,
            "project_page",
            url,
            1,
            "2026-08-04T00:00:00+00:00",
            "2026-08-04T00:00:01+00:00",
            1000,
            "success",
            200,
            url,
            "text/html; charset=utf-8",
            len(body),
            content_sha,
            relative,
            0,
            "[]",
            None,
        ),
    )
    state.execute(
        """
        INSERT INTO metadata_versions(
            run_id,target_url,entity_type,snapshot_sha256,parser_version,
            metadata_version,parsed_at,parse_status,quality,identity_status,
            identity_json,raw_embedded_json,dom_json,resolved_json,conflict_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            url,
            "project",
            content_sha,
            OLD_PARSER,
            OLD_METADATA,
            "2026-08-04T00:00:01+00:00",
            "no_content",
            "none",
            "missing",
            json.dumps({"status": "missing"}),
            json.dumps([{"parse_status": "malformed"}]),
            json.dumps({"_canonical_url": url}),
            "{}",
            "{}",
        ),
    )
    state.commit()
    recrawl.finish_run(
        state,
        run_id,
        status="completed",
        source_sha256_after=source_sha,
        summary={},
        selected_count=1,
    )
    state.close()
    return url


class SnapshotReparseSchemaTests(unittest.TestCase):
    def test_snapshot_reader_bounds_decompression_and_rejects_escape_paths(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="architizer-reparse-reader-") as root:
            snapshot_root = Path(root) / "snapshots"
            snapshot_root.mkdir()
            body = b"z" * 1024
            relative = "pages/body.html.gz"
            path = snapshot_root / relative
            path.parent.mkdir()
            path.write_bytes(gzip.compress(body, mtime=0))
            with self.assertRaises(recrawl.RecrawlError):
                recrawl._read_verified_snapshot(
                    snapshot_root,
                    relative_path=relative,
                    content_sha256=recrawl.sha256_bytes(body),
                    response_bytes=16,
                )
            with self.assertRaises(recrawl.RecrawlError):
                recrawl._read_verified_snapshot(
                    snapshot_root,
                    relative_path=relative,
                    content_sha256=recrawl.sha256_bytes(body),
                    response_bytes=recrawl.MAX_SNAPSHOT_RESPONSE_BYTES + 1,
                )
            compressed_size = path.stat().st_size
            with mock.patch.object(
                recrawl,
                "MAX_COMPRESSED_SNAPSHOT_BYTES",
                compressed_size - 1,
            ), mock.patch.object(
                Path,
                "open",
                side_effect=AssertionError("oversized gzip must not be opened"),
            ):
                with self.assertRaises(recrawl.RecrawlError):
                    recrawl._read_verified_snapshot(
                        snapshot_root,
                        relative_path=relative,
                        content_sha256=recrawl.sha256_bytes(body),
                        response_bytes=len(body),
                    )
            for unsafe in ("../body.html.gz", str(path.resolve()), "C:\\escape.gz"):
                with self.subTest(unsafe=unsafe), self.assertRaises(
                    recrawl.RecrawlError
                ):
                    recrawl._safe_snapshot_path(snapshot_root, unsafe)

    def test_explicit_21_to_22_migration_is_repeatable(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="architizer-reparse-migration-"
        ) as root:
            path = Path(root) / "state.db"
            connection = recrawl.connect_state(path)
            connection.close()
            raw = sqlite3.connect(path)
            raw.execute("PRAGMA foreign_keys=OFF")
            raw.execute("DROP TABLE snapshot_reparse_lineage")
            raw.execute("DROP TABLE snapshot_reparse_inputs")
            raw.execute(
                "UPDATE state_meta SET value='2.1' WHERE key='schema_version'"
            )
            raw.commit()
            raw.close()

            migrated = recrawl.connect_state(path)
            self.assertEqual(
                migrated.execute(
                    "SELECT value FROM state_meta WHERE key='schema_version'"
                ).fetchone()[0],
                "2.2",
            )
            self.assertEqual(
                migrated.execute("PRAGMA foreign_key_check").fetchall(), []
            )
            migrated.close()
            repeated = recrawl.connect_state(path)
            self.assertEqual(
                repeated.execute(
                    "SELECT COUNT(*) FROM sqlite_master "
                    "WHERE type='table' AND name LIKE 'snapshot_reparse_%'"
                ).fetchone()[0],
                2,
            )
            repeated.close()

    def test_failed_21_to_22_ddl_keeps_old_schema_version(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="architizer-reparse-migration-fail-"
        ) as root:
            path = Path(root) / "state.db"
            connection = recrawl.connect_state(path)
            connection.close()
            raw = sqlite3.connect(path)
            raw.execute("PRAGMA foreign_keys=OFF")
            raw.execute("DROP TABLE snapshot_reparse_lineage")
            raw.execute("DROP TABLE snapshot_reparse_inputs")
            raw.execute(
                "UPDATE state_meta SET value='2.1' WHERE key='schema_version'"
            )
            raw.commit()
            raw.close()
            with mock.patch.object(
                recrawl,
                "SNAPSHOT_REPARSE_SCHEMA",
                recrawl.SNAPSHOT_REPARSE_SCHEMA + "\nTHIS IS NOT SQL;",
            ):
                with self.assertRaises(sqlite3.Error):
                    recrawl.connect_state(path)
            check = sqlite3.connect(path)
            self.assertEqual(
                check.execute(
                    "SELECT value FROM state_meta WHERE key='schema_version'"
                ).fetchone()[0],
                "2.1",
            )
            self.assertIsNone(
                check.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='snapshot_reparse_lineage'"
                ).fetchone()
            )
            check.close()

    def test_lineage_trigger_requires_the_exact_frozen_http_attempt(self) -> None:
        with tempfile.TemporaryDirectory(prefix="architizer-reparse-lineage-") as root:
            fixture = seed_network_firms(Path(root), 1)
            source = fixture["source"]
            state = recrawl.connect_state(
                fixture["state"],
                source_path=source,
                source_sha256=file_sha256(source),
                source_size=source.stat().st_size,
            )
            target = recrawl.select_snapshot_reparse_targets(state)[0]
            frozen = recrawl._build_snapshot_reparse_input(
                state,
                target=target,
                snapshot_root=fixture["snapshots"],
            )
            run_id = recrawl.start_run(
                state,
                run_kind="snapshot_reparse_full",
                source_path=source,
                source_sha256=file_sha256(source),
                source_size=source.stat().st_size,
                arguments={},
            )
            recrawl._insert_snapshot_reparse_input(
                state,
                run_id=run_id,
                selection_order=1,
                status_before=target["status"],
                frozen=frozen,
            )
            state.commit()
            body, _ = recrawl._read_verified_snapshot(
                fixture["snapshots"],
                relative_path=frozen["gzip_path"],
                content_sha256=frozen["content_sha256"],
                response_bytes=frozen["response_bytes"],
                gzip_sha256=frozen["gzip_sha256"],
            )
            parsed = recrawl.parse_entity_page(
                body,
                requested_url=frozen["target_url"],
                final_url=frozen["final_url"],
                http_status=200,
                content_type=frozen["content_type"],
                entity_type="firm",
            )
            legacy = recrawl.open_legacy_readonly(source)
            try:
                state.execute("BEGIN IMMEDIATE")
                version_id = recrawl._store_parse_result(
                    state,
                    run_id=run_id,
                    target=target,
                    snapshot_sha=frozen["content_sha256"],
                    parsed=parsed,
                    legacy_connection=legacy,
                    promote_valid=False,
                    commit=False,
                )
                wrong = dict(frozen)
                wrong["source_http_attempt_id"] += 999
                with self.assertRaises(sqlite3.IntegrityError):
                    recrawl._insert_snapshot_reparse_lineage(
                        state,
                        run_id=run_id,
                        version_id=version_id,
                        frozen=wrong,
                    )
                state.rollback()
                self.assertEqual(
                    state.execute(
                        "SELECT COUNT(*) FROM metadata_versions WHERE parser_version=?",
                        (recrawl.PARSER_VERSION,),
                    ).fetchone()[0],
                    0,
                )
            finally:
                legacy.close()
                state.close()


class SnapshotReparseRunnerTests(unittest.TestCase):
    def test_project_recovery_requires_current_target_snapshot_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="architizer-reparse-project-current-"
        ) as root:
            fixture = seed_network_firms(Path(root), 10)
            project_url = add_project_parser_regression(fixture)
            source = Path(fixture["source"])
            state = recrawl.connect_state(
                Path(fixture["state"]),
                source_path=source,
                source_sha256=file_sha256(source),
                source_size=source.stat().st_size,
            )
            candidate = next(
                row
                for row in recrawl.select_snapshot_reparse_targets(state)
                if row["url"] == project_url
            )
            frozen = recrawl._build_snapshot_reparse_input(
                state,
                target=candidate,
                snapshot_root=Path(fixture["snapshots"]),
            )
            mismatched = dict(candidate)
            mismatched["last_snapshot_sha256"] = "F" * 64
            mismatched["last_http_status"] = 503
            with self.assertRaises(recrawl.RecrawlError):
                recrawl._build_snapshot_reparse_input(
                    state,
                    target=mismatched,
                    snapshot_root=Path(fixture["snapshots"]),
                )
            state.execute(
                """
                UPDATE targets
                SET last_snapshot_sha256=?,last_http_status=503
                WHERE url=?
                """,
                ("F" * 64, project_url),
            )
            state.commit()
            self.assertNotIn(
                project_url,
                {
                    row["url"]
                    for row in recrawl.select_snapshot_reparse_targets(state)
                },
            )
            run_id = recrawl.start_run(
                state,
                run_kind="snapshot_reparse_full",
                source_path=source,
                source_sha256=file_sha256(source),
                source_size=source.stat().st_size,
                arguments={},
            )
            with self.assertRaises(sqlite3.IntegrityError):
                recrawl._insert_snapshot_reparse_input(
                    state,
                    run_id=run_id,
                    selection_order=1,
                    status_before="failed",
                    frozen=frozen,
                )
            state.rollback()
            state.close()

    def test_n10_includes_exact_project_parser_regression_recovery(self) -> None:
        with tempfile.TemporaryDirectory(prefix="architizer-reparse-project-") as root:
            fixture = seed_network_firms(Path(root), 10)
            project_url = add_project_parser_regression(fixture)
            before = network_states(fixture["state"])
            summary = recrawl.run_snapshot_reparse(
                smoke_size=10,
                source_path=fixture["source"],
                state_path=fixture["state"],
                snapshot_root=fixture["snapshots"],
            )
            self.assertEqual(
                summary["selection_kind_counts"],
                {
                    "firm_last_good_parser_upgrade": 9,
                    "project_parser_regression_recovery": 1,
                },
            )
            self.assertEqual(network_states(fixture["state"]), before)
            check = recrawl.open_state_readonly(fixture["state"])
            try:
                recovered = check.execute(
                    """
                    SELECT t.status,t.attempt_count,t.last_attempt_at,
                           m.parser_version,m.identity_status,m.entity_type,
                           l.selection_kind,l.request_kind
                    FROM targets t
                    JOIN metadata_versions m ON m.id=t.last_good_version_id
                    JOIN snapshot_reparse_lineage l
                      ON l.reparse_version_id=m.id
                    WHERE t.url=?
                    """,
                    (project_url,),
                ).fetchone()
                self.assertIsNotNone(recovered)
                self.assertEqual(recovered["status"], "failed")
                self.assertEqual(recovered["attempt_count"], 1)
                self.assertEqual(
                    recovered["last_attempt_at"],
                    "2026-08-04T00:00:00+00:00",
                )
                self.assertEqual(recovered["parser_version"], recrawl.PARSER_VERSION)
                self.assertEqual(recovered["identity_status"], "valid")
                self.assertEqual(recovered["entity_type"], "project")
                self.assertEqual(
                    recovered["selection_kind"],
                    "project_parser_regression_recovery",
                )
                self.assertEqual(recovered["request_kind"], "project_page")
                values = {
                    row["field_name"]: json.loads(row["value_json"])
                    for row in check.execute(
                        """
                        SELECT rf.field_name,rf.value_json
                        FROM resolved_fields rf
                        WHERE rf.version_id=? AND rf.value_json IS NOT NULL
                        """,
                        (
                            check.execute(
                                "SELECT last_good_version_id FROM targets WHERE url=?",
                                (project_url,),
                            ).fetchone()[0],
                        ),
                    )
                }
                self.assertEqual(values["project_id"], 176967)
                self.assertEqual(values["global_id"], "projects.project.176967")
            finally:
                check.close()

    def test_n10_is_offline_atomic_and_preserves_network_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="architizer-reparse-n10-") as root:
            fixture = seed_network_firms(Path(root), 12)
            source = fixture["source"]
            state_path = fixture["state"]
            snapshots = fixture["snapshots"]
            before_sha = file_sha256(source)
            before_states = network_states(state_path)
            snapshot_hashes = {
                path: file_sha256(path)
                for path in Path(snapshots).rglob("*.gz")
            }
            with mock.patch.object(
                recrawl.PoliteHttpClient, "fetch", autospec=True
            ) as fetch, mock.patch.object(
                recrawl.urllib.request, "urlopen", autospec=True
            ) as urlopen:
                summary = recrawl.run_snapshot_reparse(
                    smoke_size=10,
                    source_path=source,
                    state_path=state_path,
                    snapshot_root=snapshots,
                )
            fetch.assert_not_called()
            urlopen.assert_not_called()
            self.assertTrue(summary["gate_passed"])
            self.assertEqual(summary["selected"], 10)
            self.assertEqual(summary["processed"], 10)
            self.assertEqual(summary["verified_gzip_count"], 10)
            self.assertEqual(summary["state_schema_version"], "2.2")
            self.assertEqual(file_sha256(source), before_sha)
            self.assertEqual(network_states(state_path), before_states)
            self.assertEqual(
                {path: file_sha256(path) for path in snapshot_hashes},
                snapshot_hashes,
            )
            check = recrawl.open_state_readonly(state_path)
            try:
                self.assertEqual(
                    check.execute(
                        "SELECT COUNT(*) FROM snapshot_reparse_lineage"
                    ).fetchone()[0],
                    10,
                )
                self.assertEqual(
                    check.execute(
                        "SELECT COUNT(*) FROM http_attempts "
                        "WHERE run_id=?",
                        (summary["run_id"],),
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    check.execute(
                        "SELECT COUNT(*) FROM current_fields "
                        "WHERE field_name='description' AND value_json='\"Keep me\"'"
                    ).fetchone()[0],
                    12,
                )
                self.assertEqual(
                    check.execute("PRAGMA foreign_key_check").fetchall(), []
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    writable = sqlite3.connect(state_path)
                    writable.execute("PRAGMA foreign_keys=ON")
                    try:
                        writable.execute(
                            "UPDATE snapshot_reparse_lineage SET integrity_status='bad'"
                        )
                    finally:
                        writable.close()
                with self.assertRaises(sqlite3.IntegrityError):
                    writable = sqlite3.connect(state_path)
                    writable.execute("PRAGMA foreign_keys=ON")
                    try:
                        writable.execute("DELETE FROM snapshot_reparse_lineage")
                    finally:
                        writable.close()
            finally:
                check.close()

    def test_quality_gate_failure_keeps_all_old_last_good_and_current_fields(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="architizer-reparse-quality-") as root:
            fixture = seed_network_firms(Path(root), 10)
            before_states = network_states(fixture["state"])
            original = recrawl.parse_entity_page
            calls = 0

            def invalidate_first(*args: object, **kwargs: object) -> dict[str, object]:
                nonlocal calls
                parsed = original(*args, **kwargs)
                calls += 1
                if calls == 1:
                    parsed["identity"]["status"] = "conflict"
                    parsed["identity"]["errors"] = ["injected_identity_drift"]
                    parsed["parse_status"] = "no_content"
                    parsed["quality"] = "none"
                return parsed

            with mock.patch.object(
                recrawl, "parse_entity_page", side_effect=invalidate_first
            ):
                with self.assertRaisesRegex(
                    recrawl.RecrawlError, "quality gates"
                ):
                    recrawl.run_snapshot_reparse(
                        smoke_size=10,
                        source_path=fixture["source"],
                        state_path=fixture["state"],
                        snapshot_root=fixture["snapshots"],
                    )
            self.assertEqual(network_states(fixture["state"]), before_states)
            check = recrawl.open_state_readonly(fixture["state"])
            try:
                self.assertEqual(
                    check.execute(
                        "SELECT status FROM runs "
                        "WHERE run_kind='snapshot_reparse_n10'"
                    ).fetchone()[0],
                    "quality_failed",
                )
                self.assertEqual(
                    check.execute(
                        "SELECT COUNT(*) FROM snapshot_reparse_lineage"
                    ).fetchone()[0],
                    10,
                )
                self.assertEqual(
                    check.execute(
                        """
                        SELECT COUNT(*)
                        FROM targets t JOIN metadata_versions m
                          ON m.id=t.last_good_version_id
                        WHERE m.parser_version=?
                        """,
                        (OLD_PARSER,),
                    ).fetchone()[0],
                    10,
                )
                self.assertEqual(
                    check.execute(
                        "SELECT COUNT(*) FROM current_fields "
                        "WHERE value_json='\"Keep me\"'"
                    ).fetchone()[0],
                    10,
                )
            finally:
                check.close()

    def test_failure_between_metadata_and_lineage_rolls_back_all_promotion(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="architizer-reparse-rollback-") as root:
            fixture = seed_network_firms(Path(root), 10)
            state_path = fixture["state"]
            before_states = network_states(state_path)
            with mock.patch.object(
                recrawl,
                "_insert_snapshot_reparse_lineage",
                side_effect=RuntimeError("injected after metadata store"),
            ):
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    recrawl.run_snapshot_reparse(
                        smoke_size=10,
                        source_path=fixture["source"],
                        state_path=state_path,
                        snapshot_root=fixture["snapshots"],
                    )
            self.assertEqual(network_states(state_path), before_states)
            check = recrawl.open_state_readonly(state_path)
            try:
                self.assertEqual(
                    check.execute(
                        "SELECT COUNT(*) FROM metadata_versions WHERE parser_version=?",
                        (recrawl.PARSER_VERSION,),
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    check.execute(
                        "SELECT COUNT(*) FROM snapshot_reparse_lineage"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    check.execute(
                        "SELECT COUNT(*) FROM current_fields "
                        "WHERE value_json='\"Keep me\"'"
                    ).fetchone()[0],
                    10,
                )
            finally:
                check.close()

    def test_same_run_resume_is_idempotent_and_rechecks_all_gzip(self) -> None:
        with tempfile.TemporaryDirectory(prefix="architizer-reparse-resume-") as root:
            fixture = seed_network_firms(Path(root), 10)
            original = recrawl._insert_snapshot_reparse_lineage
            calls = 0

            def fail_second(*args: object, **kwargs: object) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("second item crash")
                original(*args, **kwargs)

            with mock.patch.object(
                recrawl, "_insert_snapshot_reparse_lineage", side_effect=fail_second
            ):
                with self.assertRaisesRegex(RuntimeError, "second item"):
                    recrawl.run_snapshot_reparse(
                        smoke_size=10,
                        source_path=fixture["source"],
                        state_path=fixture["state"],
                        snapshot_root=fixture["snapshots"],
                    )
            check = recrawl.open_state_readonly(fixture["state"])
            failed_run_id = check.execute(
                "SELECT id FROM runs WHERE run_kind='snapshot_reparse_n10'"
            ).fetchone()[0]
            self.assertEqual(
                check.execute(
                    "SELECT COUNT(*) FROM snapshot_reparse_lineage"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                check.execute(
                    "SELECT COUNT(*) FROM targets t JOIN metadata_versions m "
                    "ON m.id=t.last_good_version_id WHERE m.parser_version=?",
                    (recrawl.PARSER_VERSION,),
                ).fetchone()[0],
                0,
            )
            check.close()

            first_input = recrawl.open_state_readonly(fixture["state"])
            first_path = first_input.execute(
                "SELECT gzip_path FROM snapshot_reparse_inputs "
                "WHERE run_id=? ORDER BY selection_order LIMIT 1",
                (failed_run_id,),
            ).fetchone()[0]
            first_input.close()
            gzip_path = Path(fixture["snapshots"]) / first_path
            original_gzip = gzip_path.read_bytes()
            gzip_path.write_bytes(gzip.compress(b"tampered", mtime=0))
            with self.assertRaises(recrawl.RecrawlError):
                recrawl.run_snapshot_reparse(
                    smoke_size=10,
                    resume_run_id=failed_run_id,
                    source_path=fixture["source"],
                    state_path=fixture["state"],
                    snapshot_root=fixture["snapshots"],
                )
            gzip_path.write_bytes(original_gzip)
            summary = recrawl.run_snapshot_reparse(
                smoke_size=10,
                resume_run_id=failed_run_id,
                source_path=fixture["source"],
                state_path=fixture["state"],
                snapshot_root=fixture["snapshots"],
            )
            self.assertTrue(summary["gate_passed"])
            check = recrawl.open_state_readonly(fixture["state"])
            try:
                self.assertEqual(
                    check.execute(
                        "SELECT COUNT(*) FROM snapshot_reparse_lineage"
                    ).fetchone()[0],
                    10,
                )
                self.assertEqual(
                    check.execute(
                        "SELECT COUNT(*) FROM metadata_versions WHERE parser_version=?",
                        (recrawl.PARSER_VERSION,),
                    ).fetchone()[0],
                    10,
                )
                self.assertEqual(
                    check.execute(
                        "SELECT status FROM runs WHERE id=?", (failed_run_id,)
                    ).fetchone()[0],
                    "completed",
                )
            finally:
                check.close()

    def test_ladder_and_confirmation_are_hard_gates(self) -> None:
        with tempfile.TemporaryDirectory(prefix="architizer-reparse-ladder-") as root:
            fixture = seed_network_firms(Path(root), 115)
            kwargs = {
                "source_path": fixture["source"],
                "state_path": fixture["state"],
                "snapshot_root": fixture["snapshots"],
            }
            with self.assertRaises(recrawl.RecrawlError):
                recrawl.run_snapshot_reparse(smoke_size=100, **kwargs)
            n10 = recrawl.run_snapshot_reparse(smoke_size=10, **kwargs)
            n100 = recrawl.run_snapshot_reparse(smoke_size=100, **kwargs)
            with self.assertRaises(recrawl.RecrawlError):
                recrawl.run_snapshot_reparse(smoke_size=None, **kwargs)
            full = recrawl.run_snapshot_reparse(
                smoke_size=None, confirmed_full=True, **kwargs
            )
            self.assertEqual(n100["ladder"], {"n10_run_id": n10["run_id"]})
            self.assertEqual(
                full["ladder"],
                {"n10_run_id": n10["run_id"], "n100_run_id": n100["run_id"]},
            )
            self.assertEqual(full["selected"], 5)
            check = recrawl.open_state_readonly(fixture["state"])
            try:
                n10_urls = {
                    row[0]
                    for row in check.execute(
                        "SELECT url FROM run_targets WHERE run_id=?", (n10["run_id"],)
                    )
                }
                n100_urls = {
                    row[0]
                    for row in check.execute(
                        "SELECT url FROM run_targets WHERE run_id=?",
                        (n100["run_id"],),
                    )
                }
                self.assertFalse(n10_urls & n100_urls)
                self.assertEqual(
                    recrawl.select_snapshot_reparse_targets(check), []
                )
            finally:
                check.close()

    def test_snapshot_and_http_evidence_tampering_fail_closed(self) -> None:
        mutations = (
            ("gzip_path", "../escape.gz"),
            ("final_url", "https://architizer.com/firms/wrong-firm/"),
            ("requested_url", "https://architizer.com/firms/wrong-firm/"),
            ("block_signals_json", '["login"]'),
            ("error", "unexpected"),
        )
        for column, value in mutations:
            with self.subTest(column=column), tempfile.TemporaryDirectory(
                prefix=f"architizer-reparse-tamper-{column}-"
            ) as root:
                fixture = seed_network_firms(Path(root), 10)
                raw = sqlite3.connect(fixture["state"])
                raw.execute(f"UPDATE http_attempts SET {column}=?", (value,))
                raw.commit()
                raw.close()
                with self.assertRaises(recrawl.RecrawlError):
                    recrawl.run_snapshot_reparse(
                        smoke_size=10,
                        source_path=fixture["source"],
                        state_path=fixture["state"],
                        snapshot_root=fixture["snapshots"],
                    )
        with tempfile.TemporaryDirectory(
            prefix="architizer-reparse-sha-tamper-"
        ) as root:
            fixture = seed_network_firms(Path(root), 10)
            first = next(iter(fixture["gzip_paths"].values()))
            first.write_bytes(gzip.compress(b"different content", mtime=0))
            with self.assertRaises(recrawl.RecrawlError):
                recrawl.run_snapshot_reparse(
                    smoke_size=10,
                    source_path=fixture["source"],
                    state_path=fixture["state"],
                    snapshot_root=fixture["snapshots"],
                )

    def test_second_process_lock_blocks_reparse_before_state_write(self) -> None:
        with tempfile.TemporaryDirectory(prefix="architizer-reparse-lock-") as root:
            fixture = seed_network_firms(Path(root), 10)
            before = file_sha256(fixture["state"])
            with recrawl.SidecarLock(fixture["state"]):
                with self.assertRaises(recrawl.LockHeldError):
                    recrawl.run_snapshot_reparse(
                        smoke_size=10,
                        source_path=fixture["source"],
                        state_path=fixture["state"],
                        snapshot_root=fixture["snapshots"],
                    )
            self.assertEqual(file_sha256(fixture["state"]), before)


class NetworkParserUpgradeSelectionTests(unittest.TestCase):
    def test_done_old_parser_buckets_are_selected_but_current_and_prior_are_not(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="architizer-parser-upgrade-select-"
        ) as root:
            root_path = Path(root)
            source = root_path / "source.db"
            state_path = root_path / "state.db"
            create_legacy_source(source)
            state = recrawl.connect_state(state_path)
            source_sha = file_sha256(source)
            source_run = recrawl.start_run(
                state,
                run_kind="fixture-old-parser-network",
                source_path=source,
                source_sha256=source_sha,
                source_size=source.stat().st_size,
                arguments={},
            )
            state.execute(
                "UPDATE runs SET parser_version=? WHERE id=?",
                (OLD_PARSER, source_run),
            )

            specs: list[tuple[str, str, str]] = []
            specs.extend((f"new-{i}", "project", "sitemap_new") for i in range(3))
            specs.extend(
                (f"modified-{i}", "project", "sitemap_modified")
                for i in range(2)
            )
            specs.extend(
                (f"recovery-{i}", "project", "legacy_failed_retry")
                for i in range(2)
            )
            specs.extend(
                (f"stub-{i}", "firm", "legacy_project_firm_stub")
                for i in range(3)
            )
            specs.append(("award-0", "project", "legacy_award_project_seed"))
            specs.append(
                ("unchanged-0", "project", "deterministic_unchanged_sample")
            )
            urls: dict[str, str] = {}
            for ordinal, (slug, entity_type, reason) in enumerate(specs, start=1):
                segment = "projects" if entity_type == "project" else "firms"
                url = f"https://architizer.com/{segment}/{slug}/"
                urls[slug] = url
                recrawl.upsert_target(
                    state,
                    url=url,
                    entity_type=entity_type,
                    source_lastmod=None,
                    priority=20,
                    reason=reason,
                    discovery_source="fixture",
                    input_lineage={},
                )
                state.execute("UPDATE targets SET status='done' WHERE url=?", (url,))
                state.execute(
                    """
                    INSERT INTO metadata_versions(
                        run_id,target_url,entity_type,snapshot_sha256,
                        parser_version,metadata_version,parsed_at,parse_status,
                        quality,identity_status,identity_json,raw_embedded_json,
                        dom_json,resolved_json,conflict_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        source_run,
                        url,
                        entity_type,
                        f"{ordinal:064X}",
                        OLD_PARSER,
                        OLD_METADATA,
                        recrawl.utc_now(),
                        "complete",
                        "high",
                        "valid",
                        "{}",
                        "[]",
                        "{}",
                        "{}",
                        "{}",
                    ),
                )
            recrawl.finish_run(
                state,
                source_run,
                status="completed",
                source_sha256_after=source_sha,
                summary={},
            )

            current_url = urls["new-2"]
            state.execute(
                """
                INSERT INTO metadata_versions(
                    run_id,target_url,entity_type,snapshot_sha256,
                    parser_version,metadata_version,parsed_at,parse_status,
                    quality,identity_status,identity_json,raw_embedded_json,
                    dom_json,resolved_json,conflict_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    source_run,
                    current_url,
                    "project",
                    "F" * 64,
                    recrawl.PARSER_VERSION,
                    recrawl.METADATA_VERSION,
                    recrawl.utc_now(),
                    "complete",
                    "high",
                    "valid",
                    "{}",
                    "[]",
                    "{}",
                    "{}",
                    "{}",
                ),
            )
            prior_url = urls["stub-2"]
            prior_run = recrawl.start_run(
                state,
                run_kind="network_smoke_n10",
                source_path=source,
                source_sha256=source_sha,
                source_size=source.stat().st_size,
                arguments={},
            )
            state.execute(
                """
                INSERT INTO run_targets(
                    run_id,url,selection_order,selected_reason,
                    status_before,status_after
                ) VALUES (?,?,?,?,?,?)
                """,
                (prior_run, prior_url, 1, "firm_stub", "done", "done"),
            )
            recrawl.finish_run(
                state,
                prior_run,
                status="completed",
                source_sha256_after=source_sha,
                summary={
                    "gate_policy_version": recrawl.SMOKE_GATE_POLICY_VERSION,
                    "gate_passed": True,
                },
                selected_count=1,
            )
            selected = recrawl.select_network_targets(
                state,
                smoke_size=10,
                run_kind="network_smoke_n10",
            )
            state.close()
            selected_urls = {row["url"] for row in selected}
            self.assertNotIn(current_url, selected_urls)
            self.assertNotIn(prior_url, selected_urls)
            self.assertEqual(len(selected_urls), 10)
            self.assertEqual(
                {row["selected_reason"] for row in selected},
                {
                    "sitemap_new_project",
                    "sitemap_modified_project",
                    "legacy_recovery",
                    "firm_stub",
                    "award_seed",
                    "unchanged",
                },
            )


if __name__ == "__main__":
    unittest.main()
