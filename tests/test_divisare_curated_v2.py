import json
import os
import sqlite3
import unittest

from tests import test_divisare_curated as v1_tests
from tools.build_divisare_curated import build as build_v1
from tools.build_divisare_curated_v2 import build as build_v2
from tools.build_divisare_curated_v2 import file_sha256


class DivisareV2BuilderTests(unittest.TestCase):
    def setUp(self):
        self.fixture = v1_tests.DivisareBuilderTests(
            methodName="test_full_fixture_build"
        )
        self.fixture.setUp()
        self.root = self.fixture.root
        self.parent = self.root / "parent_v1_5.db"
        self.parent_report = self.root / "parent_v1_5.md"
        self.output = self.root / "metadata_v2.db"
        self.report = self.root / "metadata_v2.md"

    def tearDown(self):
        self.fixture.tearDown()

    def _build_parent(self):
        build_v1(
            source_path=self.fixture.source,
            output_path=self.parent,
            report_path=self.parent_report,
            limit_rows=None,
            replace=False,
            skip_source_hash=True,
        )

    def _build_v2(self, decisions=None):
        return build_v2(
            parent_path=self.parent,
            output_path=self.output,
            report_path=self.report,
            decisions_path=decisions,
        )

    def test_overlay_preserves_parent_and_materializes_complete_v2_state(self):
        self._build_parent()
        before = file_sha256(self.parent)

        result = self._build_v2()

        self.assertEqual(before, file_sha256(self.parent))
        self.assertFalse(os.path.samefile(self.parent, self.output))
        self.assertEqual(result["validation"]["failed"], 0)
        conn = sqlite3.connect(self.output)
        conn.row_factory = sqlite3.Row
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(
                conn.execute(
                    "SELECT parent_sha256 FROM artifact_lineage_v2"
                ).fetchone()[0],
                before,
            )
            articles = conn.execute(
                "SELECT COUNT(*) FROM source_articles"
            ).fetchone()[0]
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM article_kind_resolution_v2"
                ).fetchone()[0],
                articles,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM article_recrawl_queue_v2"
                ).fetchone()[0],
                articles,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM metadata_validation_v2 WHERE passed=0"
                ).fetchone()[0],
                0,
            )
            plan_article = conn.execute(
                """
                SELECT ak.article_kind,ak.status
                FROM article_kind_resolution_v2 ak
                WHERE ak.article_id=2
                """
            ).fetchone()
            self.assertEqual(plan_article["article_kind"], "drawing_feature")
            self.assertEqual(plan_article["status"], "candidate")
            unresolved = conn.execute(
                """
                SELECT article_kind,status
                FROM article_kind_resolution_v2
                WHERE article_id=1
                """
            ).fetchone()
            self.assertEqual(unresolved["article_kind"], "unresolved")
            self.assertEqual(unresolved["status"], "unresolved")
        finally:
            conn.close()

    def test_multiple_confirmed_programs_are_exported_without_scalar_primary(self):
        self.fixture._add_tag("types", "primary-schools", "Primary Schools")
        self.fixture._insert_project(
            4,
            "Mixed Program Building",
            ["museums", "primary-schools"],
        )
        self._build_parent()

        self._build_v2()

        conn = sqlite3.connect(self.output)
        conn.row_factory = sqlite3.Row
        try:
            building_id = conn.execute(
                """
                SELECT building_id
                FROM building_articles
                WHERE article_id=4
                """
            ).fetchone()[0]
            row = conn.execute(
                """
                SELECT program_primary,programs_json,mixed_use
                FROM building_attributes_v2
                WHERE building_id=?
                """,
                (building_id,),
            ).fetchone()
            self.assertIsNone(row["program_primary"])
            self.assertEqual(
                json.loads(row["programs_json"]),
                ["Education", "Museum"],
            )
            self.assertEqual(row["mixed_use"], 1)
            exported = conn.execute(
                """
                SELECT program,programs
                FROM v_divisare_buildings_export_v2
                WHERE canonical_bld_id=?
                """,
                (building_id,),
            ).fetchone()
            self.assertIsNone(exported["program"])
            self.assertEqual(
                json.loads(exported["programs"]),
                ["Education", "Museum"],
            )
        finally:
            conn.close()

    def test_versioned_merge_decision_creates_terminal_redirect(self):
        self.fixture._insert_project(4, "House A", ["spanish-houses"])
        self.fixture._insert_project(
            5,
            "House A",
            ["spanish-houses"],
            year=2023,
        )
        self._build_parent()
        decisions = self.root / "d2_decisions.json"
        decisions.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "version": "fixture-review-v1",
                    "decisions": [
                        {
                            "article_id_a": 4,
                            "article_id_b": 5,
                            "decision": "merge",
                            "approved": True,
                            "reason": "fixture-confirmed identity",
                            "reviewer": "unit-test",
                            "reviewed_at": "2026-07-27T00:00:00+00:00",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        self._build_v2(decisions)

        conn = sqlite3.connect(self.output)
        conn.row_factory = sqlite3.Row
        try:
            redirect = conn.execute(
                "SELECT * FROM building_redirects_v2"
            ).fetchone()
            self.assertIsNotNone(redirect)
            self.assertNotEqual(
                redirect["source_building_id"],
                redirect["target_building_id"],
            )
            active_ids = {
                row["building_id"]
                for row in conn.execute(
                    """
                    SELECT building_id
                    FROM v_active_building_articles_v2
                    WHERE article_id IN (4,5)
                    """
                )
            }
            self.assertEqual(active_ids, {redirect["target_building_id"]})
            export_row = conn.execute(
                """
                SELECT source_refs,project_year,core_conflicts_json
                FROM v_divisare_buildings_export_v2
                WHERE canonical_bld_id=?
                """,
                (redirect["target_building_id"],),
            ).fetchone()
            self.assertTrue(
                {4, 5}.issubset(
                    set(json.loads(export_row["source_refs"])["divisare"])
                )
            )
            self.assertIsNone(export_row["project_year"])
            self.assertEqual(
                json.loads(export_row["core_conflicts_json"])["project_year"],
                [2022, 2023],
            )
            self.assertEqual(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM v_divisare_buildings_export_v2
                    WHERE canonical_bld_id=?
                    """,
                    (redirect["source_building_id"],),
                ).fetchone()[0],
                0,
            )
        finally:
            conn.close()

    def test_unapproved_merge_decision_is_rejected(self):
        self.fixture._insert_project(4, "House A", ["spanish-houses"])
        self.fixture._insert_project(5, "House A", ["spanish-houses"])
        self._build_parent()
        decisions = self.root / "unapproved_decisions.json"
        decisions.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "version": "fixture-review-v1",
                    "decisions": [
                        {
                            "article_id_a": 4,
                            "article_id_b": 5,
                            "decision": "merge",
                            "reason": "not actually approved",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "approved=true"):
            self._build_v2(decisions)

        self.assertFalse(self.output.exists())

    def test_published_v2_artifact_is_no_clobber(self):
        self._build_parent()
        self._build_v2()
        before = file_sha256(self.output)

        with self.assertRaises(FileExistsError):
            self._build_v2()

        self.assertEqual(before, file_sha256(self.output))


if __name__ == "__main__":
    unittest.main()
