from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from canonical.divisare_review_v23 import (
    EXPECTED_PARSER_VERSION,
    EXPECTED_PARTIAL_SUPERSEDES_SHA256,
    resolve_identity_components,
)
from tools.build_divisare_reviewed_v23 import (
    DEFAULT_AREA,
    DEFAULT_D2,
    DEFAULT_PARENT,
    DEFAULT_PARTIAL,
    ARTICLE_RESOLUTION_COLUMNS,
    build_artifact,
    file_sha256,
    validate_only,
)


FROZEN_AT = "2026-08-04T06:00:00Z"


def _sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _building_id(article_id: int) -> str:
    return "fixture_bld_%06d" % article_id


def _create_parent(path: Path, article_count: int) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            PRAGMA user_version=5;
            CREATE TABLE metadata_reconciliation_lineage_v2_2(
              lineage_id INTEGER PRIMARY KEY,
              metadata_version TEXT NOT NULL
            );
            CREATE TABLE metadata_reconciliation_validation_v2_2(
              check_name TEXT PRIMARY KEY,passed INTEGER NOT NULL
            );
            CREATE TABLE source_articles(
              article_id INTEGER PRIMARY KEY,source_url TEXT NOT NULL,
              source_row_hash TEXT NOT NULL,description_quality TEXT NOT NULL,
              content_score REAL NOT NULL,image_count INTEGER NOT NULL,
              tag_count INTEGER NOT NULL,description_ui_markers INTEGER NOT NULL
            );
            CREATE TABLE article_text_versions(
              text_id INTEGER PRIMARY KEY,text TEXT NOT NULL
            );
            CREATE TABLE article_recrawl_evidence_v2_2(
              article_id INTEGER PRIMARY KEY,source_url TEXT NOT NULL,
              fetch_status TEXT NOT NULL,parse_status TEXT NOT NULL,
              http_status INTEGER NOT NULL,snapshot_id TEXT NOT NULL,
              html_sha256 TEXT NOT NULL,snapshot_path TEXT NOT NULL,
              parser_version TEXT NOT NULL,details_json TEXT NOT NULL,
              recrawl_area_raw TEXT,description_prose TEXT,
              recrawl_abstract TEXT,description_quality TEXT NOT NULL
            );
            CREATE TABLE article_metadata_resolution_v2_2(
              article_id INTEGER PRIMARY KEY,availability_status TEXT NOT NULL,
              resolved_name TEXT,resolved_name_normalized TEXT,
              resolved_abstract TEXT,location_country TEXT,location_city TEXT,
              project_year INTEGER,area_sqm REAL,area_candidate_sqm REAL,
              area_evidence_status TEXT NOT NULL,area_unit_kind TEXT NOT NULL,
              area_confidence REAL NOT NULL,parent_description_text_id INTEGER,
              name_source TEXT NOT NULL,name_status TEXT NOT NULL,
              abstract_source TEXT NOT NULL,country_source TEXT NOT NULL,
              country_status TEXT NOT NULL,city_source TEXT NOT NULL,
              city_status TEXT NOT NULL,year_source TEXT NOT NULL,
              year_status TEXT NOT NULL,area_source TEXT NOT NULL,
              area_status TEXT NOT NULL,description_source TEXT NOT NULL,
              description_status TEXT NOT NULL,description_publishable INTEGER NOT NULL,
              area_evidence_json TEXT NOT NULL,field_sources_json TEXT NOT NULL,
              field_conflicts_json TEXT NOT NULL,review_reasons_json TEXT NOT NULL,
              metadata_needs_review INTEGER NOT NULL,
              reconciliation_status TEXT NOT NULL,policy_version TEXT NOT NULL,
              reconciled_at TEXT NOT NULL
            );
            CREATE TABLE buildings(
              building_id TEXT PRIMARY KEY,needs_review INTEGER NOT NULL,
              cluster_confidence REAL NOT NULL
            );
            CREATE TABLE building_core_reconciled_v2_2(
              building_id TEXT PRIMARY KEY,is_active INTEGER NOT NULL,
              redirect_to TEXT,article_count INTEGER NOT NULL,
              primary_article_id INTEGER,name TEXT,name_normalized TEXT,
              location_country TEXT,location_city TEXT,
              location_resolution_method TEXT NOT NULL,
              location_confidence REAL NOT NULL,project_year INTEGER,
              year_kind TEXT NOT NULL,area_sqm REAL,
              area_candidates_json TEXT NOT NULL,field_sources_json TEXT NOT NULL,
              core_conflicts_json TEXT NOT NULL,
              reconciliation_conflicts_json TEXT NOT NULL,
              facet_conflicts_json TEXT NOT NULL,
              article_kind_counts_json TEXT NOT NULL,
              review_reasons_json TEXT NOT NULL,metadata_needs_review INTEGER NOT NULL,
              reconciliation_status TEXT NOT NULL,identity_method TEXT NOT NULL,
              resolution_version TEXT NOT NULL,resolved_at TEXT NOT NULL
            );
            CREATE TABLE building_attributes_v2(
              building_id TEXT,axis TEXT,value TEXT
            );
            CREATE TABLE active_building_membership_v2(
              article_id INTEGER PRIMARY KEY,building_id TEXT NOT NULL,
              source_building_id TEXT NOT NULL,source_article_role TEXT NOT NULL,
              membership_confidence REAL NOT NULL
            );
            CREATE TABLE article_match_reviews_v2(
              article_id_a INTEGER NOT NULL,article_id_b INTEGER NOT NULL,
              source_candidate_kind TEXT NOT NULL,source_score REAL NOT NULL,
              source_status TEXT NOT NULL,source_signals_json TEXT NOT NULL,
              building_id_a TEXT NOT NULL,building_id_b TEXT NOT NULL,
              decision_status TEXT NOT NULL,decision_id TEXT NOT NULL,
              recommendation TEXT NOT NULL,decision_source TEXT NOT NULL,
              decision_reason_json TEXT NOT NULL,
              article_kind_context_json TEXT NOT NULL,
              decision_version TEXT NOT NULL,decided_at TEXT,
              PRIMARY KEY(article_id_a,article_id_b)
            );
            CREATE TABLE attribute_claims(
              claim_id INTEGER PRIMARY KEY,article_id INTEGER NOT NULL,
              axis TEXT NOT NULL,value_normalized TEXT NOT NULL,
              confidence REAL NOT NULL,source_ref TEXT,search_tier TEXT NOT NULL,
              details_json TEXT NOT NULL,scope TEXT NOT NULL,polarity TEXT NOT NULL
            );
            CREATE TABLE claim_evidence_v2(
              claim_id INTEGER PRIMARY KEY,mapping_kind TEXT NOT NULL,
              evidence_family TEXT NOT NULL,independence_key TEXT NOT NULL
            );
            CREATE TABLE building_facets_v2(
              facet_v2_id INTEGER PRIMARY KEY,building_id TEXT NOT NULL,
              axis TEXT NOT NULL,value TEXT NOT NULL,status TEXT NOT NULL
            );
            CREATE TABLE building_facet_claims_v2(
              facet_v2_id INTEGER NOT NULL,claim_id INTEGER NOT NULL,
              PRIMARY KEY(facet_v2_id,claim_id)
            );
            CREATE TABLE image_assets(asset_key TEXT PRIMARY KEY);
            CREATE TABLE image_urls(url_id INTEGER PRIMARY KEY,url TEXT NOT NULL);
            CREATE TABLE article_image_occurrences(
              article_id INTEGER NOT NULL,asset_key TEXT NOT NULL,url_id INTEGER NOT NULL,
              role TEXT NOT NULL,position INTEGER NOT NULL,
              PRIMARY KEY(article_id,position)
            );
            CREATE TABLE building_images_materialized_v2(
              building_id TEXT NOT NULL,asset_key TEXT NOT NULL,
              representative_url TEXT NOT NULL,role_rank INTEGER NOT NULL,
              first_position INTEGER NOT NULL,
              PRIMARY KEY(building_id,asset_key)
            );
            CREATE TABLE article_kind_resolution_v2(
              article_id INTEGER PRIMARY KEY,article_kind TEXT NOT NULL,
              status TEXT NOT NULL,confidence REAL NOT NULL
            );
            CREATE TABLE article_architects(
              article_id INTEGER NOT NULL,architect_id TEXT,
              architect_name TEXT,role TEXT,position INTEGER
            );
            CREATE TABLE article_tags(article_id INTEGER NOT NULL,tag_slug TEXT);
            """
        )
        conn.execute(
            "INSERT INTO metadata_reconciliation_lineage_v2_2 VALUES (1,?)",
            ("divisare-metadata-v2.2",),
        )
        conn.execute(
            "INSERT INTO metadata_reconciliation_validation_v2_2 VALUES (?,1)",
            ("fixture_parent_valid",),
        )

        for article_id in range(1, article_count + 1):
            building_id = _building_id(article_id)
            source_url = "https://divisare.com/projects/%d-fixture" % article_id
            description = "Fixture description %d" % article_id
            abstract = "Fixture abstract %d" % article_id
            area_raw = "50 sqm"
            html_sha = _sha_text("fixture html %d" % article_id)
            source_row_hash = _sha_text("fixture source row %d" % article_id)
            snapshot_path = str(
                path.parent
                / "data"
                / "enrichment"
                / "fixture"
                / ("%d.html.gz" % article_id)
            )
            parse_status = "partial" if article_id == 1 else "complete"
            area_review = article_id in {2, 3, 4, 5}
            needs_review = int(area_review)
            review_reasons = ["area_needs_review"] if area_review else []
            area_value = None if area_review else 50.0
            area_evidence = {"needs_review": needs_review}

            conn.execute(
                "INSERT INTO source_articles VALUES (?,?,?,?,?,?,?,?)",
                (
                    article_id,source_url,source_row_hash,"dom_prose_paragraphs",
                    1.0,1,1,0,
                ),
            )
            conn.execute(
                "INSERT INTO article_text_versions VALUES (?,?)",
                (article_id, description),
            )
            conn.execute(
                "INSERT INTO article_recrawl_evidence_v2_2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    article_id,source_url,"ok",parse_status,200,
                    "fixture-snapshot-%d" % article_id,html_sha,snapshot_path,
                    EXPECTED_PARSER_VERSION,
                    _json({"prose_sha256": _sha_text(description)}),
                    area_raw,description,abstract,"dom_prose_paragraphs",
                ),
            )
            resolution = {
                "article_id": article_id,
                "availability_status": "available",
                "resolved_name": "Fixture Project %d" % article_id,
                "resolved_name_normalized": "fixture project %d" % article_id,
                "resolved_abstract": abstract,
                "location_country": "Fixtureland",
                "location_city": "Fixture City %d" % article_id,
                "project_year": 2000 + article_id,
                "area_sqm": area_value,
                "area_candidate_sqm": None,
                "area_evidence_status": "review" if area_review else "accepted",
                "area_unit_kind": "sqm",
                "area_confidence": 0.0 if area_review else 0.9,
                "parent_description_text_id": article_id,
                "name_source": "parent",
                "name_status": "resolved",
                "abstract_source": "parent",
                "country_source": "parent",
                "country_status": "resolved",
                "city_source": "parent",
                "city_status": "resolved",
                "year_source": "parent",
                "year_status": "resolved",
                "area_source": "parent",
                "area_status": "review" if area_review else "resolved",
                "description_source": "parent",
                "description_status": "resolved",
                "description_publishable": 1,
                "area_evidence_json": _json(area_evidence),
                "field_sources_json": "{}",
                "field_conflicts_json": "{}",
                "review_reasons_json": _json(review_reasons),
                "metadata_needs_review": needs_review,
                "reconciliation_status": "review" if area_review else "complete",
                "policy_version": "fixture-parent-policy",
                "reconciled_at": "2026-01-01T00:00:00Z",
            }
            conn.execute(
                "INSERT INTO article_metadata_resolution_v2_2 VALUES (%s)"
                % ",".join("?" for _ in ARTICLE_RESOLUTION_COLUMNS),
                tuple(resolution[column] for column in ARTICLE_RESOLUTION_COLUMNS),
            )
            conn.execute(
                "INSERT INTO buildings VALUES (?,0,0.99)", (building_id,)
            )
            conn.execute(
                "INSERT INTO building_core_reconciled_v2_2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    building_id,1,None,1,article_id,resolution["resolved_name"],
                    resolution["resolved_name_normalized"],"Fixtureland",
                    resolution["location_city"],"fixture_parent",0.99,
                    resolution["project_year"],"completion",area_value,"[]","{}",
                    "{}","{}","{}",_json({"project:confirmed": 1}),"[]",0,
                    "complete","fixture_identity","fixture-v2.2",
                    "2026-01-01T00:00:00Z",
                ),
            )
            conn.execute(
                "INSERT INTO building_attributes_v2 VALUES (?,?,?)",
                (building_id,"material","concrete"),
            )
            conn.execute(
                "INSERT INTO active_building_membership_v2 VALUES (?,?,?,?,?)",
                (article_id,building_id,building_id,"primary",0.99),
            )
            conn.execute(
                "INSERT INTO attribute_claims VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    article_id,article_id,"material","concrete",0.99,
                    "fixture:%d" % article_id,"primary",_json({"priority": 100}),
                    "building","positive",
                ),
            )
            conn.execute(
                "INSERT INTO claim_evidence_v2 VALUES (?,?,?,?)",
                (article_id,"direct","source_tag","article:%d" % article_id),
            )
            conn.execute(
                "INSERT INTO building_facets_v2 VALUES (?,?,?,?,?)",
                (article_id,building_id,"material","concrete","confirmed"),
            )
            conn.execute(
                "INSERT INTO building_facet_claims_v2 VALUES (?,?)",
                (article_id,article_id),
            )
            asset_key = "fixture_asset_006_007" if article_id in (6, 7) else (
                "fixture_asset_%06d" % article_id
            )
            conn.execute(
                "INSERT OR IGNORE INTO image_assets VALUES (?)", (asset_key,)
            )
            image_url = "https://images.example/%d.jpg" % article_id
            conn.execute(
                "INSERT INTO image_urls VALUES (?,?)", (article_id,image_url)
            )
            conn.execute(
                "INSERT INTO article_image_occurrences VALUES (?,?,?,?,?)",
                (article_id,asset_key,article_id,"cover",0),
            )
            conn.execute(
                "INSERT INTO building_images_materialized_v2 VALUES (?,?,?,?,?)",
                (building_id,asset_key,image_url,0,0),
            )
            conn.execute(
                "INSERT INTO article_kind_resolution_v2 VALUES (?,?,?,?)",
                (article_id,"project","confirmed",0.99),
            )
            conn.execute(
                "INSERT INTO article_architects VALUES (?,?,?,?,?)",
                (article_id,"fixture_architect","Fixture Architect","architect",0),
            )
            conn.execute(
                "INSERT INTO article_tags VALUES (?,?)",
                (article_id,"fixture-tag"),
            )

        for left, right in ((6, 7), (8, 9), (9, 10)):
            conn.execute(
                "INSERT INTO article_match_reviews_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    left,right,"fixture_review",0.9,"candidate","{}",
                    _building_id(left),_building_id(right),"pending",
                    "parent-d2-%d-%d" % (left,right),"review","fixture_parent",
                    "{}","{}","fixture-v2.2",None,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _write_manifests(directory: Path, parent_path: Path) -> tuple[Path, Path, Path]:
    parent_sha = file_sha256(parent_path)
    partial_path = directory / "partial.json"
    area_path = directory / "area.json"
    d2_path = directory / "d2.json"

    partial = {
        "schema_version": 1,
        "version": "fixture-partial-v2",
        "decided_by": "fixture-reviewer",
        "decided_at": FROZEN_AT,
        "supersedes": {
            "version": "fixture-partial-v1",
            "sha256": EXPECTED_PARTIAL_SUPERSEDES_SHA256,
        },
        "decisions": [
            {
                "article_id": 1,
                "parser_version": EXPECTED_PARSER_VERSION,
                "prose_sha256": _sha_text("Fixture description 1"),
                "decision": "reject",
                "reason_code": "fixture_partial_reject",
                "note": "Fixture partial prose is not publishable.",
            }
        ],
    }
    partial_path.write_text(_json(partial), encoding="utf-8")

    area_specs = {
        2: ("accept_area",100.0,None,"whole_project_area"),
        3: ("keep_scoped_candidate",None,200.0,"site_area"),
        4: ("reject_non_area",None,None,"non_area_quantity"),
        5: ("keep_null_multi_or_conflict",None,None,"ambiguous_area"),
    }
    area_decisions = []
    for article_id, (decision_type, resolved, candidate, scope) in area_specs.items():
        description = "Fixture description %d" % article_id
        area_decisions.append(
            {
                "article_id": article_id,
                "decision_type": decision_type,
                "resolved_area_sqm": resolved,
                "candidate_area_sqm": candidate,
                "area_scope": scope,
                "confidence": 0.99,
                "closure_status": "final",
                "rationale_code": "fixture_%s" % decision_type,
                "evidence": {
                    "parser_version": EXPECTED_PARSER_VERSION,
                    "area_raw_sha256": _sha_text("50 sqm"),
                    "description_prose_sha256": _sha_text(description),
                    "html_sha256": _sha_text("fixture html %d" % article_id),
                },
            }
        )
    area_counts = {
        "accept_area": 1,
        "keep_scoped_candidate": 1,
        "keep_null_multi_or_conflict": 1,
        "reject_non_area": 1,
        "final": 4,
        "open_external_text_review": 0,
        "total": 4,
    }
    area = {
        "schema_version": 1,
        "version": "fixture-area-v1",
        "policy": "fixture-area-policy",
        "decided_by": "fixture-reviewer",
        "decided_at": FROZEN_AT,
        "frozen_at": FROZEN_AT,
        "parent_sha256": parent_sha,
        "image_policy": "never infer numeric area from images",
        "counts": area_counts,
        "decisions": area_decisions,
    }
    area_path.write_text(_json(area), encoding="utf-8")

    def guard(article_id: int) -> dict:
        return {
            "article_id": article_id,
            "source_url": "https://divisare.com/projects/%d-fixture" % article_id,
            "parser_version": EXPECTED_PARSER_VERSION,
            "description_prose_sha256": _sha_text(
                "Fixture description %d" % article_id
            ),
            "abstract_sha256": _sha_text("Fixture abstract %d" % article_id),
            "html_sha256": _sha_text("fixture html %d" % article_id),
            "source_row_hash": _sha_text("fixture source row %d" % article_id),
            "snapshot_path": "data/enrichment/fixture/%d.html.gz" % article_id,
        }

    def decision(
        left: int,
        right: int,
        action: str,
        relation_type: str,
        component_id: str,
    ) -> dict:
        if action == "merge":
            evidence = [
                {
                    "evidence_family": "architect_record",
                    "supports": "same_identity",
                    "independent_for_merge": True,
                },
                {
                    "evidence_family": "institutional_record",
                    "supports": "same_identity",
                    "independent_for_merge": True,
                },
            ]
            conflicts = []
            family_count = 2
        elif action == "reject":
            evidence = [
                {
                    "evidence_family": "distinct_same_name",
                    "supports": "different_identity",
                    "independent_for_merge": False,
                }
            ]
            conflicts = [{"conflict_type": "distinct_same_name", "fact": "Fixture objects differ."}]
            family_count = 0
        else:
            evidence = [
                {
                    "evidence_family": "insufficient_evidence",
                    "supports": "inconclusive",
                    "independent_for_merge": False,
                }
            ]
            conflicts = []
            family_count = 0
        pair_buildings = sorted((_building_id(left), _building_id(right)))
        return {
            "article_id_a": left,
            "article_id_b": right,
            "building_id_a_before": _building_id(left),
            "building_id_b_before": _building_id(right),
            "component_id": component_id,
            "building_pair_id": "|".join(pair_buildings),
            "source_candidate_kind": "fixture_review",
            "source_score": 0.9,
            "decision": action,
            "decision_id": "fixture-d2-%d-%d" % (left,right),
            "approved": True,
            "reviewer": "fixture-reviewer",
            "reviewed_at": FROZEN_AT,
            "identity_scope": "same_architectural_project_intervention",
            "relation_type": relation_type,
            "related_project": False,
            "related_relation": None,
            "related_group_id": None,
            "reason_code": "fixture_%s" % action,
            "note": "Fixture %s decision with explicit evidence." % action,
            "evidence": evidence,
            "evidence_family_count": family_count,
            "hard_conflicts": conflicts,
            "guards": {"article_a": guard(left), "article_b": guard(right)},
        }

    d2_decisions = [
        decision(6,7,"merge","same_project_duplicate","d2c_000006"),
        decision(8,9,"reject","distinct_same_name","d2c_000008"),
        decision(9,10,"defer","unresolved_identity","d2c_000008"),
    ]
    d2 = {
        "schema_version": 1,
        "version": "fixture-d2-v1",
        "policy": "fixture-d2-policy",
        "frozen_at": FROZEN_AT,
        "parent_sha256": parent_sha,
        "counts": {
            "total_pairs": 3,
            "unique_components": 2,
            "unique_building_pairs": 3,
            "merge": 1,
            "reject": 1,
            "defer": 1,
            "approved": 3,
            "approved_abstentions": 1,
        },
        "reject_relation_counts": {"distinct_same_name": 1},
        "decisions": d2_decisions,
    }
    d2_path.write_text(_json(d2), encoding="utf-8")
    return partial_path,area_path,d2_path


class DivisareReviewedV23IntegrationTests(unittest.TestCase):
    def test_n10_then_n100_builds_are_deterministic_and_no_clobber(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for article_count in (10, 100):
                with self.subTest(article_count=article_count):
                    fixture = root / ("n%d" % article_count)
                    fixture.mkdir()
                    parent = fixture / "parent_v2_2.db"
                    _create_parent(parent, article_count)
                    partial,area,d2 = _write_manifests(fixture,parent)
                    parent_sha_before = file_sha256(parent)
                    manifest_sha_before = {
                        path.name: file_sha256(path) for path in (partial,area,d2)
                    }

                    outputs = []
                    logical_shas = []
                    for run in (1, 2):
                        output = fixture / ("reviewed_run_%d.db" % run)
                        report = fixture / ("reviewed_run_%d.md" % run)
                        result = build_artifact(
                            parent_path=parent,
                            partial_path=partial,
                            area_path=area,
                            d2_path=d2,
                            output_path=output,
                            report_path=report,
                            production_contract=False,
                        )
                        self.assertEqual("built", result["status"])
                        self.assertEqual(0, result["validation"]["failed"])
                        outputs.append(output)
                        logical_shas.append(result["logical_sha256"])

                    self.assertEqual(file_sha256(outputs[0]), file_sha256(outputs[1]))
                    self.assertEqual(logical_shas[0], logical_shas[1])
                    self.assertEqual(parent_sha_before,file_sha256(parent))
                    self.assertEqual(
                        manifest_sha_before,
                        {path.name: file_sha256(path) for path in (partial,area,d2)},
                    )
                    conn = sqlite3.connect(outputs[0])
                    try:
                        self.assertEqual(
                            article_count,
                            conn.execute(
                                "SELECT COUNT(*) FROM article_metadata_resolution_v2_3"
                            ).fetchone()[0],
                        )
                        self.assertEqual(
                            article_count - 1,
                            conn.execute(
                                "SELECT COUNT(*) FROM v_divisare_buildings_export_v2_3"
                            ).fetchone()[0],
                        )
                        merged = conn.execute(
                            """
                            SELECT building_id FROM active_building_membership_v2_3
                            WHERE article_id IN (6,7) GROUP BY building_id
                            """
                        ).fetchall()
                        self.assertEqual(1,len(merged))
                        self.assertEqual(
                            "manual_reject_fallback",
                            conn.execute(
                                """
                                SELECT description_status
                                FROM article_metadata_resolution_v2_3 WHERE article_id=1
                                """
                            ).fetchone()[0],
                        )
                        self.assertEqual(
                            (None,200.0),
                            conn.execute(
                                """
                                SELECT area_sqm,area_candidate_sqm
                                FROM article_metadata_resolution_v2_3 WHERE article_id=3
                                """
                            ).fetchone(),
                        )
                    finally:
                        conn.close()

                    output_sha = file_sha256(outputs[0])
                    with self.assertRaises(FileExistsError):
                        build_artifact(
                            parent_path=parent,
                            partial_path=partial,
                            area_path=area,
                            d2_path=d2,
                            output_path=outputs[0],
                            report_path=fixture / "reviewed_run_1.md",
                            production_contract=False,
                        )
                    self.assertEqual(output_sha,file_sha256(outputs[0]))

    def test_production_validate_only_has_no_output_side_effect(self) -> None:
        self.assertTrue(DEFAULT_PARENT.is_file())
        before = {
            path: file_sha256(path)
            for path in (DEFAULT_PARENT,DEFAULT_PARTIAL,DEFAULT_AREA,DEFAULT_D2)
        }
        result = validate_only(
            parent_path=DEFAULT_PARENT,
            partial_path=DEFAULT_PARTIAL,
            area_path=DEFAULT_AREA,
            d2_path=DEFAULT_D2,
            production_contract=True,
        )
        self.assertEqual("validated",result["status"])
        self.assertFalse(result["output_created"])
        self.assertEqual(
            {"accept": 9,"reject": 12,"review": 0,"total": 21},
            result["partial_counts"],
        )
        self.assertEqual(8,result["d2_counts"]["merge"])
        self.assertEqual(84,result["d2_counts"]["defer"])
        self.assertEqual(before,{path: file_sha256(path) for path in before})

    def test_component_conflict_gate_rejects_transitive_collapse(self) -> None:
        decisions = {
            (1,2): {"decision": "merge"},
            (2,3): {"decision": "merge"},
            (1,3): {"decision": "reject"},
        }
        with self.assertRaisesRegex(ValueError,"collapses through a merge component"):
            resolve_identity_components(
                {"b1","b2","b3"},
                {1: "b1",2: "b2",3: "b3"},
                decisions,
            )


if __name__ == "__main__":
    unittest.main()
