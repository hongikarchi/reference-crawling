import hashlib
import json
import sqlite3
import unittest

from tests import test_divisare_curated as v1_tests
from tools.build_divisare_curated import build as build_v1
from tools.build_divisare_curated_v2 import build as build_v2
from tools.build_divisare_curated_v2 import file_sha256
from tools.build_divisare_reconciled import (
    build as build_reconciled,
    count_unknown_auto_area_residuals,
    preserved_table_logical_hashes,
)
from tools.recrawl_divisare_metadata_v2 import STATE_SCHEMA_SQL


PARSER_VERSION = "divisare-html-metadata-v2.3"
CRAWLER_VERSION = "divisare-html-recrawl-v2.1"
STAMP = "2026-08-04T00:00:00+00:00"


class DivisareReconciliationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = v1_tests.DivisareBuilderTests(
            methodName="test_full_fixture_build"
        )
        self.fixture.setUp()
        self.root = self.fixture.root
        self.v1_parent = self.root / "metadata_v1_5.db"
        self.v1_report = self.root / "metadata_v1_5.md"
        self.parent = self.root / "metadata_v2_1.db"
        self.parent_report = self.root / "metadata_v2_1.md"
        self.recrawl = self.root / "recrawl.db"
        self.output = self.root / "metadata_v2_2.db"
        self.report = self.root / "metadata_v2_2.md"
        self.decisions = self.root / "partial_decisions.json"

        build_v1(
            source_path=self.fixture.source,
            output_path=self.v1_parent,
            report_path=self.v1_report,
            limit_rows=None,
            replace=False,
            skip_source_hash=True,
        )
        build_v2(
            parent_path=self.v1_parent,
            output_path=self.parent,
            report_path=self.parent_report,
        )

    def tearDown(self):
        self.fixture.tearDown()

    @staticmethod
    def _prose_sha(prose):
        return hashlib.sha256((prose or "").encode("utf-8")).hexdigest()

    def _write_decisions(self, items):
        self.decisions.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "version": "fixture-partial-decisions-v1",
                    "decided_by": "integration-test",
                    "decided_at": STAMP,
                    "decisions": items,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def _seed_recrawl(self, overrides=None):
        overrides = overrides or {}
        parent_sha = file_sha256(self.parent)
        parent = sqlite3.connect(self.parent)
        parent.row_factory = sqlite3.Row
        try:
            articles = {
                int(row["article_id"]): dict(row)
                for row in parent.execute(
                    """
                    SELECT article_id,source_url,name_raw,abstract_raw,
                           location_country,location_city,project_year
                    FROM source_articles
                    ORDER BY article_id
                    """
                )
            }
        finally:
            parent.close()

        defaults = {
            1: {
                "fetch_status": "success",
                "parse_status": "no_content",
                "http_status": 200,
                "description_prose": None,
                "description_quality": "no_prose_content",
                "country": "Korea, Republic of",
                "city": "Seoul",
                "year": 2020,
                "area_sqm": None,
                "area_raw": None,
            },
            2: {
                "fetch_status": "success",
                "parse_status": "success",
                "http_status": 200,
                "description_prose": "Recrawled direct project prose.",
                "description_quality": "dom_prose_paragraphs",
                "country": "Korea, Republic of",
                "city": "Seoul",
                "year": 2020,
                "area_sqm": 1.0,
                "area_raw": "1'670 m2",
            },
            3: {
                "fetch_status": "not_found",
                "parse_status": "skipped",
                "http_status": 404,
                "description_prose": None,
                "description_quality": None,
                "country": None,
                "city": None,
                "year": None,
                "area_sqm": None,
                "area_raw": None,
            },
        }
        for article_id, values in overrides.items():
            defaults[article_id].update(values)

        conn = sqlite3.connect(self.recrawl)
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            conn.executescript(STATE_SCHEMA_SQL)
            conn.execute("PRAGMA user_version=1")
            conn.execute(
                """
                INSERT INTO recrawl_lineage(
                    lineage_id,parent_db_path,parent_sha256,
                    parent_metadata_version,parent_schema_version,
                    crawler_version,parser_version,snapshot_root,created_at
                ) VALUES (1,?,?,?,?,?,?,?,?)
                """,
                (
                    str(self.parent),
                    parent_sha,
                    "divisare-metadata-v2.1",
                    4,
                    CRAWLER_VERSION,
                    PARSER_VERSION,
                    str(self.root / "snapshots"),
                    STAMP,
                ),
            )
            run_id = conn.execute(
                """
                INSERT INTO recrawl_runs(
                    started_at,completed_at,status,max_items,delay_seconds,
                    refresh_mode,processed,metrics_json
                ) VALUES (?,?, 'complete',NULL,0,0,3,'{}')
                """,
                (STAMP, STAMP),
            ).lastrowid

            for article_id in sorted(articles):
                article = articles[article_id]
                spec = defaults[article_id]
                fetched = spec["fetch_status"] == "success"
                html_sha = hashlib.sha256(
                    ("fixture-html-%d" % article_id).encode("ascii")
                ).hexdigest()
                snapshot_path = (
                    "snapshots/%d/%s.html.gz" % (article_id, html_sha)
                    if fetched
                    else None
                )
                conn.execute(
                    """
                    INSERT INTO article_html_jobs(
                        article_id,source_url,priority,reasons_json,
                        fetch_status,parse_status,attempt_count,http_status,
                        final_url,content_type,current_html_sha256,
                        snapshot_path,html_byte_size,queued_at,started_at,
                        fetched_at,parsed_at,last_error,updated_at
                    ) VALUES (?,?,100,'[]',?,?,?,?,?,?,?,?,64,?,?,?,?,NULL,?)
                    """,
                    (
                        article_id,
                        article["source_url"],
                        spec["fetch_status"],
                        spec["parse_status"],
                        1,
                        spec["http_status"],
                        article["source_url"],
                        "text/html",
                        html_sha if fetched else None,
                        snapshot_path,
                        STAMP,
                        STAMP,
                        STAMP,
                        STAMP if fetched else None,
                        STAMP,
                    ),
                )
                if not fetched:
                    continue
                snapshot_id = conn.execute(
                    """
                    INSERT INTO article_html_snapshots(
                        article_id,html_sha256,byte_size,snapshot_path,
                        http_status,final_url,response_headers_json,
                        fetched_at,run_id,is_current
                    ) VALUES (?,?,64,?,?,?,'{}',?,?,1)
                    """,
                    (
                        article_id,
                        html_sha,
                        snapshot_path,
                        spec["http_status"],
                        article["source_url"],
                        STAMP,
                        run_id,
                    ),
                ).lastrowid
                prose = spec["description_prose"]
                details = {
                    "fixture": True,
                    "prose_sha256": self._prose_sha(prose),
                }
                conn.execute(
                    """
                    INSERT INTO article_metadata_versions(
                        article_id,snapshot_id,name,abstract,location_country,
                        location_city,project_year,area_sqm,area_raw,
                        description_prose,description_quality,
                        explicit_article_kind,explicit_article_kind_raw,
                        parser_version,details_json,parsed_at,is_current
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,?,?,?,1)
                    """,
                    (
                        article_id,
                        snapshot_id,
                        article["name_raw"],
                        article["abstract_raw"],
                        spec["country"],
                        spec["city"],
                        spec["year"],
                        spec["area_sqm"],
                        spec["area_raw"],
                        prose,
                        spec["description_quality"],
                        PARSER_VERSION,
                        json.dumps(details, sort_keys=True),
                        STAMP,
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    def _build(self):
        return build_reconciled(
            parent_path=self.parent,
            recrawl_path=self.recrawl,
            output_path=self.output,
            report_path=self.report,
            partial_decisions_path=self.decisions,
        )

    def test_reconciles_metadata_without_clobbering_inputs_or_images(self):
        self._seed_recrawl()
        self._write_decisions([])
        parent_sha = file_sha256(self.parent)
        recrawl_sha = file_sha256(self.recrawl)

        result = self._build()

        self.assertEqual(0, result["validation"]["failed"])
        self.assertEqual(parent_sha, file_sha256(self.parent))
        self.assertEqual(recrawl_sha, file_sha256(self.recrawl))
        conn = sqlite3.connect(self.output)
        conn.row_factory = sqlite3.Row
        try:
            article_2 = conn.execute(
                "SELECT * FROM v_article_metadata_reconciled_v2_2 WHERE article_id=2"
            ).fetchone()
            self.assertEqual(
                "Recrawled direct project prose.", article_2["description"]
            )
            self.assertEqual("recrawl", article_2["description_source"])
            self.assertEqual("South Korea", article_2["location_country"])
            self.assertEqual("confirmed", article_2["country_status"])
            self.assertEqual(1670.0, article_2["area_sqm"])

            area = json.loads(article_2["area_evidence_json"])
            self.assertEqual(1670.0, area["value_sqm"])
            self.assertEqual("accepted_reparsed", area["status"])
            self.assertEqual("1'670 m2", area["details"]["raw"])
            self.assertFalse(area["details"]["parsed_matches"])
            evidence = conn.execute(
                """
                SELECT recrawl_area_raw,recrawl_area_sqm
                FROM article_recrawl_evidence_v2_2 WHERE article_id=2
                """
            ).fetchone()
            self.assertEqual("1'670 m2", evidence["recrawl_area_raw"])
            self.assertEqual(1.0, evidence["recrawl_area_sqm"])

            direct_export = conn.execute(
                """
                SELECT description,area_sqm,location_country
                FROM v_divisare_buildings_export_v2_2
                WHERE primary_divisare_id=2
                """
            ).fetchone()
            self.assertIsNotNone(direct_export)
            self.assertEqual(
                "Recrawled direct project prose.", direct_export["description"]
            )
            self.assertEqual(1670.0, direct_export["area_sqm"])
            self.assertEqual("South Korea", direct_export["location_country"])

            no_content = conn.execute(
                "SELECT * FROM v_article_metadata_reconciled_v2_2 WHERE article_id=1"
            ).fetchone()
            self.assertIsNone(no_content["description"])
            self.assertIsNotNone(no_content["historical_parent_description"])
            self.assertEqual("none", no_content["description_source"])
            self.assertEqual("source_has_no_prose", no_content["description_status"])
            self.assertEqual(0, no_content["description_publishable"])

            tombstone = conn.execute(
                "SELECT * FROM v_article_metadata_reconciled_v2_2 WHERE article_id=3"
            ).fetchone()
            self.assertEqual(
                tombstone["historical_parent_description"],
                tombstone["description"],
            )
            self.assertEqual("source_unavailable", tombstone["availability_status"])
            self.assertEqual("parent", tombstone["description_source"])
            self.assertEqual(
                "parent_fallback_tombstone", tombstone["description_status"]
            )

            image_rows = conn.execute(
                """
                SELECT old.canonical_bld_id,
                       old.cover_image_url AS old_cover,
                       new.cover_image_url AS new_cover,
                       old.gallery_image_urls AS old_gallery,
                       new.gallery_image_urls AS new_gallery
                FROM v_divisare_buildings_export_v2 old
                JOIN v_divisare_buildings_export_v2_2 new
                  ON new.canonical_bld_id=old.canonical_bld_id
                ORDER BY old.canonical_bld_id
                """
            ).fetchall()
            self.assertTrue(image_rows)
            for row in image_rows:
                self.assertEqual(row["old_cover"], row["new_cover"])
                self.assertEqual(row["old_gallery"], row["new_gallery"])

            saw_confirmed = False
            saw_candidate = False
            for exported in conn.execute(
                """
                SELECT canonical_bld_id,confirmed_facets_json,
                       candidate_facets_json
                FROM v_divisare_buildings_export_v2_2
                ORDER BY canonical_bld_id
                """
            ):
                for status, column in (
                    ("confirmed", "confirmed_facets_json"),
                    ("candidate", "candidate_facets_json"),
                ):
                    expected = [
                        dict(row)
                        for row in conn.execute(
                            """
                            SELECT axis,value,role,confidence,search_tier
                            FROM building_facets_v2
                            WHERE building_id=? AND status=?
                            ORDER BY axis,confidence DESC,value
                            """,
                            (exported["canonical_bld_id"], status),
                        )
                    ]
                    actual = json.loads(exported[column])
                    self.assertEqual(expected, actual)
                    saw_confirmed = saw_confirmed or bool(actual and status == "confirmed")
                    saw_candidate = saw_candidate or bool(actual and status == "candidate")
            self.assertTrue(saw_confirmed)
            self.assertTrue(saw_candidate)

            lineage = conn.execute(
                "SELECT * FROM metadata_reconciliation_lineage_v2_2"
            ).fetchone()
            self.assertEqual(parent_sha, lineage["parent_sha256"])
            self.assertEqual(recrawl_sha, lineage["recrawl_sha256"])
            preservation = conn.execute(
                """
                SELECT passed,actual_json,expected_json
                FROM metadata_reconciliation_validation_v2_2
                WHERE check_name='preserved_parent_content_exact'
                """
            ).fetchone()
            self.assertIsNotNone(preservation)
            self.assertEqual(1, preservation["passed"])
            self.assertEqual(
                json.loads(preservation["expected_json"]),
                json.loads(preservation["actual_json"]),
            )
            self.assertIn(
                "source_articles", json.loads(preservation["actual_json"])
            )
            area_residual_gate = conn.execute(
                """
                SELECT passed,actual_json,expected_json
                FROM metadata_reconciliation_validation_v2_2
                WHERE check_name='accepted_area_residual_classification_unknown'
                """
            ).fetchone()
            self.assertIsNotNone(area_residual_gate)
            self.assertEqual(1, area_residual_gate["passed"])
            self.assertEqual("0", area_residual_gate["actual_json"])
            self.assertEqual("0", area_residual_gate["expected_json"])
        finally:
            conn.close()

        output_sha = file_sha256(self.output)
        with self.assertRaises(FileExistsError):
            self._build()
        self.assertEqual(output_sha, file_sha256(self.output))
        self.assertEqual(parent_sha, file_sha256(self.parent))
        self.assertEqual(recrawl_sha, file_sha256(self.recrawl))

    def test_preserved_content_hash_detects_same_cardinality_mutation(self):
        source = sqlite3.connect(
            "file:%s?mode=ro" % self.parent.resolve().as_posix(), uri=True
        )
        clone = sqlite3.connect(":memory:")
        try:
            source.backup(clone)
            expected = preserved_table_logical_hashes(source)
            before_count = clone.execute(
                "SELECT COUNT(*) FROM source_articles"
            ).fetchone()[0]
            clone.execute(
                """
                UPDATE source_articles
                SET name_raw=COALESCE(name_raw,'') || ' changed'
                WHERE article_id=(SELECT MIN(article_id) FROM source_articles)
                """
            )
            actual = preserved_table_logical_hashes(clone, tuple(expected))
            self.assertEqual(
                before_count,
                clone.execute(
                    "SELECT COUNT(*) FROM source_articles"
                ).fetchone()[0],
            )
            self.assertNotEqual(
                expected["source_articles"], actual["source_articles"]
            )
            self.assertEqual(expected["image_urls"], actual["image_urls"])
        finally:
            clone.close()
            source.close()

    def test_auto_area_residual_gate_rejects_unknown_and_risky_evidence(self):
        self._seed_recrawl(
            {
                2: {
                    "area_sqm": 4150.0,
                    "area_raw": "4.150 m\u00b2 (gross)",
                }
            }
        )
        self._write_decisions([])
        self._build()

        conn = sqlite3.connect(self.output)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """
                SELECT area_source,area_evidence_json
                FROM article_metadata_resolution_v2_2
                WHERE article_id=2
                """
            ).fetchone()
            self.assertEqual("recrawl", row["area_source"])
            evidence = json.loads(row["area_evidence_json"])
            self.assertEqual("gross", evidence["details"]["qualifier"])
            self.assertEqual(0, count_unknown_auto_area_residuals(conn))

            evidence["details"]["qualifier"] = "unclassified basis"
            conn.execute(
                """
                UPDATE article_metadata_resolution_v2_2
                SET area_evidence_json=?
                WHERE article_id=2
                """,
                (json.dumps(evidence, sort_keys=True),),
            )
            self.assertEqual(1, count_unknown_auto_area_residuals(conn))

            evidence["details"]["qualifier"] = "gross"
            evidence["details"]["reason"] = "residual_additive_or_range_scope"
            evidence["details"]["scope"] = "additive_or_range"
            conn.execute(
                """
                UPDATE article_metadata_resolution_v2_2
                SET area_evidence_json=?
                WHERE article_id=2
                """,
                (json.dumps(evidence, sort_keys=True),),
            )
            self.assertEqual(1, count_unknown_auto_area_residuals(conn))
        finally:
            conn.close()

    def test_partial_accept_and_reject_are_hash_guarded_and_export_safe(self):
        accepted = "Accepted fallback project prose."
        rejected = "Caption labels that must not be published."
        self._seed_recrawl(
            {
                1: {
                    "parse_status": "partial",
                    "description_prose": accepted,
                    "description_quality": "dom_text_fallback_review",
                    "area_sqm": None,
                    "area_raw": None,
                },
                2: {
                    "parse_status": "partial",
                    "description_prose": rejected,
                    "description_quality": "dom_text_fallback_review",
                },
            }
        )
        self._write_decisions(
            [
                {
                    "article_id": 1,
                    "parser_version": PARSER_VERSION,
                    "prose_sha256": self._prose_sha(accepted),
                    "decision": "accept",
                    "reason_code": "fixture_project_prose",
                    "note": "Meaningful fixture project prose.",
                },
                {
                    "article_id": 2,
                    "parser_version": PARSER_VERSION,
                    "prose_sha256": self._prose_sha(rejected),
                    "decision": "reject",
                    "reason_code": "fixture_caption_labels",
                    "note": "Fixture caption labels are not prose.",
                },
            ]
        )

        self._build()

        conn = sqlite3.connect(self.output)
        conn.row_factory = sqlite3.Row
        try:
            accepted_row = conn.execute(
                "SELECT * FROM v_article_metadata_reconciled_v2_2 WHERE article_id=1"
            ).fetchone()
            rejected_row = conn.execute(
                "SELECT * FROM v_article_metadata_reconciled_v2_2 WHERE article_id=2"
            ).fetchone()
            self.assertEqual(accepted, accepted_row["description"])
            self.assertEqual("manual_accept_fallback", accepted_row["description_status"])
            self.assertEqual(1, accepted_row["description_publishable"])
            self.assertIsNone(rejected_row["description"])
            self.assertEqual("manual_reject_fallback", rejected_row["description_status"])
            self.assertEqual(0, rejected_row["description_publishable"])
            decisions = conn.execute(
                """
                SELECT article_id,decision,hash_guard_matched
                FROM article_partial_text_decisions_v2_2
                ORDER BY article_id
                """
            ).fetchall()
            self.assertEqual(
                [(1, "accept", 1), (2, "reject", 1)],
                [tuple(row) for row in decisions],
            )
        finally:
            conn.close()

    def test_partial_decision_hash_mismatch_aborts_without_output(self):
        prose = "Fallback prose guarded by its content hash."
        self._seed_recrawl(
            {
                1: {
                    "parse_status": "partial",
                    "description_prose": prose,
                    "description_quality": "dom_text_fallback_review",
                    "area_sqm": None,
                    "area_raw": None,
                }
            }
        )
        self._write_decisions(
            [
                {
                    "article_id": 1,
                    "parser_version": PARSER_VERSION,
                    "prose_sha256": "0" * 64,
                    "decision": "accept",
                    "reason_code": "stale_fixture_decision",
                    "note": "This intentionally stale decision must not apply.",
                }
            ]
        )
        parent_sha = file_sha256(self.parent)
        recrawl_sha = file_sha256(self.recrawl)

        with self.assertRaisesRegex(RuntimeError, "hash/parser guard failed"):
            self._build()

        self.assertFalse(self.output.exists())
        self.assertFalse(self.report.exists())
        self.assertEqual(parent_sha, file_sha256(self.parent))
        self.assertEqual(recrawl_sha, file_sha256(self.recrawl))

    def test_missing_partial_decision_aborts_without_output(self):
        accepted = "Reviewed fallback prose."
        missing = "Fallback prose without a decision."
        self._seed_recrawl(
            {
                1: {
                    "parse_status": "partial",
                    "description_prose": accepted,
                    "description_quality": "dom_text_fallback_review",
                },
                2: {
                    "parse_status": "partial",
                    "description_prose": missing,
                    "description_quality": "dom_text_fallback_review",
                },
            }
        )
        self._write_decisions(
            [
                {
                    "article_id": 1,
                    "parser_version": PARSER_VERSION,
                    "prose_sha256": self._prose_sha(accepted),
                    "decision": "accept",
                    "reason_code": "fixture_project_prose",
                    "note": "Only one of two partial rows is decided.",
                }
            ]
        )

        with self.assertRaisesRegex(
            RuntimeError,
            r"partial decision ID set mismatch: missing=\[2\] extra=\[\]",
        ):
            self._build()

        self.assertFalse(self.output.exists())
        self.assertFalse(self.report.exists())

    def test_extra_partial_decision_aborts_without_output(self):
        self._seed_recrawl()
        self._write_decisions(
            [
                {
                    "article_id": 1,
                    "parser_version": PARSER_VERSION,
                    "prose_sha256": self._prose_sha(None),
                    "decision": "reject",
                    "reason_code": "fixture_stale_decision",
                    "note": "This article is not currently partial.",
                }
            ]
        )

        with self.assertRaisesRegex(
            RuntimeError,
            r"partial decision ID set mismatch: missing=\[\] extra=\[1\]",
        ):
            self._build()

        self.assertFalse(self.output.exists())
        self.assertFalse(self.report.exists())


if __name__ == "__main__":
    unittest.main()
