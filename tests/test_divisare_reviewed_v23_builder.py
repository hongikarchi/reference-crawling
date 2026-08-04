from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from canonical.divisare_review_v23 import (
    EXPECTED_PARSER_VERSION,
    load_d2_manifest,
    validate_d2_guard,
)
from tools.build_divisare_reviewed_v23 import (
    ARTICLE_RESOLUTION_COLUMNS,
    SCHEMA_SQL,
    _apply_article_decisions,
    _pending_component_ids,
    build_artifact,
    validate_only,
)


PARENT_SHA = "a" * 64


def _guard(article_id: int) -> dict:
    return {
        "article_id": article_id,
        "source_url": "https://divisare.com/projects/%d-test" % article_id,
        "parser_version": EXPECTED_PARSER_VERSION,
        "description_prose_sha256": hashlib.sha256(b"description").hexdigest(),
        "abstract_sha256": hashlib.sha256(b"abstract").hexdigest(),
        "html_sha256": "b" * 64,
        "source_row_hash": "c" * 64,
        "snapshot_path": "data/enrichment/snapshots/%d.html.gz" % article_id,
    }


def _d2_payload() -> dict:
    evidence = [
        {
            "evidence_family": "article_record",
            "supports": "same_identity",
            "independent_for_merge": True,
        },
        {
            "evidence_family": "institutional_record",
            "supports": "same_identity",
            "independent_for_merge": True,
        },
        {
            "evidence_family": "name_similarity",
            "supports": "candidate_only",
            "independent_for_merge": False,
        },
    ]
    return {
        "schema_version": 1,
        "version": "fixture-d2-v1",
        "policy": "fixture-policy",
        "frozen_at": "2026-08-04T00:00:00Z",
        "parent_sha256": PARENT_SHA,
        "counts": {
            "total_pairs": 1,
            "unique_components": 1,
            "unique_building_pairs": 1,
            "merge": 1,
            "reject": 0,
            "defer": 0,
            "approved": 1,
            "approved_abstentions": 0,
        },
        "reject_relation_counts": {},
        "decisions": [
            {
                "article_id_a": 1,
                "article_id_b": 2,
                "building_id_a_before": "b1",
                "building_id_b_before": "b2",
                "component_id": "d2c_000001",
                "building_pair_id": "b1|b2",
                "source_candidate_kind": "fixture",
                "source_score": 0.9,
                "decision": "merge",
                "decision_id": "fixture-d2-1-2",
                "approved": True,
                "reviewer": "fixture-reviewer",
                "reviewed_at": "2026-08-04T00:00:00Z",
                "identity_scope": "same_architectural_project_intervention",
                "relation_type": "same_project_duplicate",
                "related_project": False,
                "related_relation": None,
                "related_group_id": None,
                "reason_code": "two_independent_records",
                "note": "Fixture merge with two independent records.",
                "evidence": evidence,
                "evidence_family_count": 2,
                "hard_conflicts": [],
                "guards": {"article_a": _guard(1), "article_b": _guard(2)},
            }
        ],
    }


def _parent_resolution() -> dict:
    values = {column: None for column in ARTICLE_RESOLUTION_COLUMNS}
    values.update(
        {
            "article_id": 1,
            "availability_status": "available",
            "area_evidence_status": "none",
            "area_unit_kind": "none",
            "area_confidence": 0.0,
            "name_source": "parent",
            "name_status": "resolved",
            "abstract_source": "parent",
            "country_source": "none",
            "country_status": "unresolved",
            "city_source": "none",
            "city_status": "unresolved",
            "year_source": "none",
            "year_status": "unresolved",
            "area_source": "none",
            "area_status": "unresolved",
            "description_source": "parent",
            "description_status": "resolved",
            "description_publishable": 1,
            "area_evidence_json": "{}",
            "field_sources_json": "{}",
            "field_conflicts_json": "{}",
            "review_reasons_json": "[]",
            "metadata_needs_review": 0,
            "reconciliation_status": "complete_with_nulls",
            "policy_version": "parent-policy",
            "reconciled_at": "2026-01-01T00:00:00Z",
        }
    )
    return values


class DivisareReviewedV23BuilderTests(unittest.TestCase):
    def test_overlay_schema_compiles_with_guard_columns(self) -> None:
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(
                """
                CREATE TABLE source_articles(article_id INTEGER PRIMARY KEY);
                CREATE TABLE buildings(building_id TEXT PRIMARY KEY);
                CREATE TABLE article_text_versions(text_id INTEGER PRIMARY KEY);
                CREATE TABLE attribute_claims(claim_id INTEGER PRIMARY KEY);
                CREATE TABLE image_assets(asset_key TEXT PRIMARY KEY);
                """
            )
            conn.executescript(SCHEMA_SQL)
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(article_d2_decisions_v2_3)")
            }
            self.assertIn("guards_json", columns)
            self.assertIn("hash_guard_matched", columns)
            self.assertIn("evidence_family_count", columns)
        finally:
            conn.close()

    def test_unaffected_article_resolution_is_exactly_preserved(self) -> None:
        parent = _parent_resolution()
        actual = _apply_article_decisions(
            parent, None, None, "2026-08-04T00:00:00Z"
        )
        self.assertEqual(
            tuple(parent[column] for column in ARTICLE_RESOLUTION_COLUMNS), actual
        )

    def test_partial_reject_and_scoped_area_never_publish_generic_area(self) -> None:
        parent = _parent_resolution()
        partial = {
            "decision": "reject",
            "reason_code": "partial_not_project_prose",
            "decision_policy_version": "partial-v2",
        }
        area = {
            "decision_type": "keep_scoped_candidate",
            "resolved_area_sqm": None,
            "candidate_area_sqm": 575000.0,
            "area_scope": "development_program_area",
            "confidence": 0.98,
            "closure_status": "final",
            "rationale_code": "explicit_scoped_quantity",
            "decision_policy_version": "area-v1",
        }
        result = dict(
            zip(
                ARTICLE_RESOLUTION_COLUMNS,
                _apply_article_decisions(
                    parent, partial, area, "2026-08-04T00:00:00Z"
                ),
            )
        )
        self.assertEqual(0, result["description_publishable"])
        self.assertEqual("manual_reject_fallback", result["description_status"])
        self.assertIsNone(result["area_sqm"])
        self.assertEqual(575000.0, result["area_candidate_sqm"])
        self.assertEqual("manual_scoped_candidate", result["area_status"])

    def test_d2_loader_derives_independent_families_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "d2.json"
            payload = _d2_payload()
            path.write_text(json.dumps(payload), encoding="utf-8")
            loaded = load_d2_manifest(
                path,
                expected_parent_sha256=PARENT_SHA,
                expected_pairs={(1, 2)},
                expected_counts=None,
            )
            self.assertEqual(
                ["article_record", "institutional_record"],
                loaded.decisions[(1, 2)]["evidence_families"],
            )

            fabricated = copy.deepcopy(payload)
            fabricated["decisions"][0]["evidence"][0][
                "independent_for_merge"
            ] = False
            fabricated["decisions"][0]["evidence_families"] = [
                "article_record",
                "institutional_record",
            ]
            path.write_text(json.dumps(fabricated), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "supplied and derived"):
                load_d2_manifest(
                    path,
                    expected_parent_sha256=PARENT_SHA,
                    expected_counts=None,
                )

    def test_d2_snapshot_guard_accepts_portable_repo_relative_path(self) -> None:
        decision = _d2_payload()["decisions"][0]
        validate_d2_guard(
            {"guards": decision["guards"]},
            side="article_a",
            article_id=1,
            source_url=decision["guards"]["article_a"]["source_url"],
            parser_version=EXPECTED_PARSER_VERSION,
            description_prose="description",
            recrawl_abstract="abstract",
            html_sha256="b" * 64,
            source_row_hash="c" * 64,
            snapshot_path=(
                "C:\\repo\\data\\enrichment\\snapshots\\1.html.gz"
            ),
        )

    def test_pending_component_id_uses_minimum_article_in_graph(self) -> None:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                """
                CREATE TABLE article_match_reviews_v2(
                  article_id_a INTEGER,article_id_b INTEGER,decision_status TEXT
                )
                """
            )
            conn.executemany(
                "INSERT INTO article_match_reviews_v2 VALUES (?,?,?)",
                [(20, 30, "pending"), (10, 20, "pending"), (40, 50, "rejected")],
            )
            self.assertEqual(
                {10: "d2c_000010", 20: "d2c_000010", 30: "d2c_000010"},
                _pending_component_ids(conn),
            )
        finally:
            conn.close()

    def test_missing_d2_and_existing_output_fail_before_any_build(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "parent.db"
            partial = root / "partial.json"
            area = root / "area.json"
            missing_d2 = root / "d2.json"
            for path in (parent, partial, area):
                path.write_bytes(b"fixture")
            with self.assertRaisesRegex(FileNotFoundError, "requires all immutable"):
                validate_only(
                    parent_path=parent,
                    partial_path=partial,
                    area_path=area,
                    d2_path=missing_d2,
                    production_contract=False,
                )

            output = root / "existing.db"
            report = root / "report.md"
            output.write_bytes(b"do-not-clobber")
            with self.assertRaisesRegex(FileExistsError, "already exists"):
                build_artifact(
                    parent_path=parent,
                    partial_path=partial,
                    area_path=area,
                    d2_path=missing_d2,
                    output_path=output,
                    report_path=report,
                    production_contract=False,
                )
            self.assertEqual(b"do-not-clobber", output.read_bytes())
            self.assertFalse(report.exists())


if __name__ == "__main__":
    unittest.main()
