from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from canonical.cross_source_image_selection import canonical_sha256
from canonical.cross_source_semantic_coverage import (
    SEMANTIC_COVERAGE_MANIFEST_DOMAIN,
)
from canonical.cross_source_semantic_coverage_sources import (
    DEFAULT_E2_APPLICATION_ID,
    DEFAULT_E3_APPLICATION_ID,
    ArtifactSpec,
    build_semantic_coverage_manifest,
    sha256_file,
    write_semantic_coverage_manifest,
)


E2_RUN = "fixture-e2-full"
E3_RUN = "fixture-e3-full"
E2_LOGICAL = hashlib.sha256(b"fixture-e2-logical").hexdigest()
E3_LOGICAL = hashlib.sha256(b"fixture-e3-logical").hexdigest()


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


BUILDINGS = (
    ("architizer", "a-qa", "ordinary", True, False, False, False, 6),
    ("architizer", "a-p1", "ordinary", False, True, False, False, 6),
    ("divisare", "d-p1", "ordinary", False, True, False, False, 6),
    ("architizer", "a-p2", "ordinary", False, False, True, False, 6),
    ("divisare", "d-p2", "ordinary", False, False, True, False, 6),
    (
        "architizer",
        "a-gallery",
        "gallery_fallback",
        False,
        False,
        False,
        False,
        6,
    ),
    (
        "divisare",
        "d-gallery",
        "gallery_fallback",
        False,
        False,
        False,
        False,
        6,
    ),
    ("architizer", "a-cross", "cross_source_candidate", False, False, False, True, 6),
    ("divisare", "d-cross", "cross_source_candidate", False, False, False, True, 6),
    ("divisare", "d-control", "ordinary", False, False, False, False, 20),
)


def _create_e2(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA user_version=1;
        CREATE TABLE e2_runs(
          run_id TEXT,contract_version TEXT,builder_version TEXT,
          selection_mode TEXT,sample_size INTEGER,sample_seed TEXT,
          ordered_selection_manifest_sha256 TEXT,config_json TEXT,status TEXT,
          started_at TEXT,completed_at TEXT,error TEXT
        );
        CREATE TABLE e2_metrics(
          run_id TEXT,phase TEXT,metric_name TEXT,stratum_json TEXT,
          value_integer INTEGER,value_real REAL,value_text TEXT,recorded_at TEXT
        );
        CREATE TABLE assets(
          run_id TEXT,source TEXT,source_asset_id TEXT,e1_run_id TEXT,
          fingerprint_status TEXT,canonical_url TEXT,fetch_url TEXT,final_url TEXT,
          raw_response_sha256 TEXT,normalized_pixel_sha256 TEXT,phash_hex TEXT,
          original_width INTEGER,original_height INTEGER,normalized_width INTEGER,
          normalized_height INTEGER,source_record_sha256 TEXT,provenance_json TEXT,
          error_kind TEXT,error_message TEXT,
          PRIMARY KEY(run_id,source,source_asset_id)
        );
        CREATE TABLE building_assets(
          run_id TEXT,source TEXT,source_building_id TEXT,source_asset_id TEXT,
          project_count INTEGER,occurrence_count INTEGER,roles_json TEXT,
          relation_record_sha256 TEXT,
          PRIMARY KEY(run_id,source,source_building_id,source_asset_id)
        );
        """
    )
    connection.execute(f"PRAGMA application_id={DEFAULT_E2_APPLICATION_ID}")
    connection.execute(
        "INSERT INTO e2_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            E2_RUN,
            "fixture-e2-contract",
            "fixture-e2-builder",
            "full",
            None,
            None,
            _sha("selection"),
            "{}",
            "complete",
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:01Z",
            None,
        ),
    )
    connection.execute(
        "INSERT INTO e2_metrics VALUES(?,?,?,?,?,?,?,?)",
        (E2_RUN, "validation", "output_logical_sha256", "{}", None, None, E2_LOGICAL, "now"),
    )
    for source, building, _stratum, _qa, _p1, _p2, _cross, count in BUILDINGS:
        for index in range(count):
            asset = f"{building}-asset-{index}"
            asset_sha = _sha(f"asset-record:{source}:{asset}")
            pixel = _sha(f"pixel:{source}:{asset}")
            phash = _sha(f"phash:{source}:{asset}")
            relation = _sha(f"relation:{source}:{building}:{asset}")
            connection.execute(
                "INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    E2_RUN,
                    source,
                    asset,
                    "fixture-e1",
                    "success",
                    f"https://canonical/{asset}",
                    f"https://fetch/{asset}",
                    None,
                    _sha(f"raw:{source}:{asset}"),
                    pixel,
                    phash,
                    1024,
                    768,
                    512,
                    384,
                    asset_sha,
                    '{"quality_flags":[]}',
                    None,
                    None,
                ),
            )
            connection.execute(
                "INSERT INTO building_assets VALUES(?,?,?,?,?,?,?,?)",
                (E2_RUN, source, building, asset, 1, 1, '["gallery"]', relation),
            )
    connection.commit()
    connection.close()


def _create_e3(path: Path, e2_path: Path) -> None:
    e2_size = e2_path.stat().st_size
    e2_sha = sha256_file(e2_path)
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA user_version=1;
        CREATE TABLE selection_runs(
          run_id TEXT,contract_version TEXT,builder_version TEXT,e2_artifact_path TEXT,
          e2_size_bytes INTEGER,e2_byte_sha256 TEXT,e2_logical_sha256 TEXT,
          policy_set_sha256 TEXT,selection_mode TEXT,sample_size INTEGER,
          sample_seed TEXT,shortlist_size INTEGER,ordered_selection_manifest_sha256 TEXT,
          config_json TEXT,network_requests INTEGER,vision_requests INTEGER,
          llm_requests INTEGER,authoritative INTEGER,artifact_scope TEXT,status TEXT,
          started_at TEXT,completed_at TEXT,error TEXT
        );
        CREATE TABLE selection_metrics(
          run_id TEXT,phase TEXT,metric_name TEXT,stratum_json TEXT,
          value_integer INTEGER,value_real REAL,value_text TEXT,recorded_at TEXT
        );
        CREATE TABLE selection_inputs(
          run_id TEXT,input_name TEXT,input_role TEXT,file_path TEXT,size_bytes INTEGER,
          sha256_before TEXT,sha256_after TEXT,logical_sha256 TEXT,application_id INTEGER,
          user_version INTEGER,schema_manifest_sha256 TEXT,recorded_at TEXT,detail_json TEXT
        );
        CREATE TABLE selected_buildings(
          run_id TEXT,selection_id TEXT,selection_rank INTEGER,stratum_id TEXT,
          source TEXT,entity_type TEXT,source_entity_id TEXT,source_building_id TEXT,
          source_project_id TEXT,name TEXT,normalized_name TEXT,selection_reason TEXT,
          e2_source_record_sha256 TEXT,e2_relation_record_sha256 TEXT,
          selection_record_sha256 TEXT,detail_json TEXT,
          PRIMARY KEY(run_id,selection_id)
        );
        CREATE TABLE image_candidates(
          run_id TEXT,candidate_id TEXT,selection_id TEXT,source TEXT,
          source_building_id TEXT,source_project_id TEXT,source_asset_id TEXT,
          fingerprint_status TEXT,canonical_url TEXT,fetch_url TEXT,final_url TEXT,
          roles_json TEXT,primary_role TEXT,role_rank INTEGER,source_ordinal INTEGER,
          ordinal_is_derived INTEGER,original_width INTEGER,original_height INTEGER,
          normalized_width INTEGER,normalized_height INTEGER,quality_flags_json TEXT,
          low_information INTEGER,normalized_pixel_sha256 TEXT,exact_cluster_id TEXT,
          phash_node_id TEXT,source_record_sha256 TEXT,occurrence_record_sha256 TEXT,
          project_relation_record_sha256 TEXT,building_relation_record_sha256 TEXT,
          candidate_record_sha256 TEXT,detail_json TEXT,
          PRIMARY KEY(run_id,candidate_id)
        );
        CREATE TABLE policy_rankings(
          run_id TEXT,policy_id TEXT,policy_version TEXT,policy_config_sha256 TEXT,
          selection_id TEXT,candidate_id TEXT,ranking_state TEXT,editorial_rank INTEGER,
          shortlist_rank INTEGER,selected INTEGER,qa_fallback INTEGER,hard_risk INTEGER,
          rank_tuple_json TEXT,component_scores_json TEXT,reasons_json TEXT,
          suppressed_by_candidate_id TEXT,suppression_reason TEXT,fallback_reason TEXT,
          ranking_record_sha256 TEXT,detail_json TEXT
        );
        CREATE TABLE shortlist_items(
          run_id TEXT,policy_id TEXT,selection_id TEXT,shortlist_rank INTEGER,
          candidate_id TEXT,shortlist_state TEXT,authoritative INTEGER,
          item_record_sha256 TEXT,rationale_json TEXT
        );
        """
    )
    connection.execute(f"PRAGMA application_id={DEFAULT_E3_APPLICATION_ID}")
    connection.execute(
        "INSERT INTO selection_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            E3_RUN,
            "fixture-e3-contract",
            "fixture-e3-builder",
            str(e2_path.resolve()),
            e2_size,
            e2_sha,
            E2_LOGICAL,
            _sha("policy-set"),
            "full",
            None,
            None,
            3,
            _sha("selection-manifest"),
            "{}",
            0,
            0,
            0,
            0,
            "candidate_only",
            "complete",
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:01Z",
            None,
        ),
    )
    connection.execute(
        "INSERT INTO selection_metrics VALUES(?,?,?,?,?,?,?,?)",
        (E3_RUN, "validation", "output_logical_sha256", "{}", None, None, E3_LOGICAL, "now"),
    )
    connection.execute(
        "INSERT INTO selection_inputs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            E3_RUN,
            "e2_evidence",
            "e2_evidence",
            str(e2_path.resolve()),
            e2_size,
            e2_sha,
            e2_sha,
            E2_LOGICAL,
            DEFAULT_E2_APPLICATION_ID,
            1,
            _sha("schema"),
            "now",
            "{}",
        ),
    )
    for rank, (source, building, stratum, qa, p1_changed, p2_changed, cross, count) in enumerate(BUILDINGS, 1):
        selection_id = f"{source}:building:{building}"
        building_sha = _sha(f"building:{source}:{building}")
        detail = json.dumps(
            {
                "building_summary": {
                    "cross_source_candidate": cross,
                    "name": building,
                    "quality_risk_cover_count": 0,
                    "source": source,
                    "source_building_id": building,
                    "source_record_sha256": building_sha,
                    "stratum": stratum,
                    "successful_asset_count": count,
                    "successful_cover_count": 1,
                }
            },
            sort_keys=True,
        )
        connection.execute(
            "INSERT INTO selected_buildings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                E3_RUN,
                selection_id,
                rank,
                f"stratum-{rank}",
                source,
                "building",
                building,
                building,
                None,
                building,
                building,
                "fixture",
                building_sha,
                None,
                _sha(f"selection:{selection_id}"),
                detail,
            ),
        )
        candidate_ids: list[str] = []
        for index in range(count):
            asset = f"{building}-asset-{index}"
            candidate = f"candidate:{source}:{asset}"
            candidate_ids.append(candidate)
            asset_sha = _sha(f"asset-record:{source}:{asset}")
            relation = _sha(f"relation:{source}:{building}:{asset}")
            phash = _sha(f"phash:{source}:{asset}")
            connection.execute(
                "INSERT INTO image_candidates VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    E3_RUN,
                    candidate,
                    selection_id,
                    source,
                    building,
                    None,
                    asset,
                    "success",
                    f"https://canonical/{asset}",
                    f"https://fetch/{asset}",
                    None,
                    '["gallery"]',
                    "gallery",
                    1,
                    index,
                    0,
                    1024,
                    768,
                    512,
                    384,
                    "[]",
                    0,
                    _sha(f"pixel:{source}:{asset}"),
                    None,
                    f"node:{phash}",
                    asset_sha,
                    None,
                    None,
                    relation,
                    _sha(f"candidate-record:{candidate}"),
                    json.dumps({"phash_hex": phash}),
                ),
            )
            p2_rank = index + 1 if index < 3 else None
            connection.execute(
                "INSERT INTO policy_rankings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    E3_RUN,
                    "p2_quality_exact_direct_phash_shortlist",
                    "fixture-policy",
                    _sha("p2-config"),
                    selection_id,
                    candidate,
                    "shortlisted" if p2_rank else "not_selected",
                    index + 1,
                    p2_rank,
                    int(p2_rank is not None),
                    int(qa),
                    0,
                    "[]",
                    "{}",
                    "[]",
                    None,
                    None,
                    None,
                    _sha(f"ranking:p2:{candidate}"),
                    "{}",
                ),
            )
        p1_ids = [candidate_ids[0], candidate_ids[1], candidate_ids[3] if p2_changed else candidate_ids[2]]
        p0_ids = ([candidate_ids[3], candidate_ids[1], candidate_ids[2]] if p1_changed else list(p1_ids))
        for policy, ids in (
            ("p0_editorial_baseline", p0_ids),
            ("p1_quality_gated_editorial", p1_ids),
            ("p2_quality_exact_direct_phash_shortlist", candidate_ids[:3]),
        ):
            for shortlist_rank, candidate in enumerate(ids, 1):
                connection.execute(
                    "INSERT INTO shortlist_items VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        E3_RUN,
                        policy,
                        selection_id,
                        shortlist_rank,
                        candidate,
                        "primary",
                        0,
                        _sha(f"shortlist:{policy}:{selection_id}:{candidate}"),
                        "{}",
                    ),
                )
                if policy == "p1_quality_gated_editorial":
                    connection.execute(
                        "INSERT INTO policy_rankings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            E3_RUN,
                            policy,
                            "fixture-policy",
                            _sha("p1-config"),
                            selection_id,
                            candidate,
                            "shortlisted",
                            shortlist_rank,
                            shortlist_rank,
                            1,
                            int(qa),
                            0,
                            "[]",
                            "{}",
                            "[]",
                            None,
                            None,
                            "all_risk" if qa else None,
                            _sha(f"ranking:p1:{candidate}"),
                            "{}",
                        ),
                    )
    connection.commit()
    connection.close()


def create_semantic_fixture(tmp_path: Path) -> tuple[ArtifactSpec, ArtifactSpec]:
    e2 = tmp_path / "e2.db"
    e3 = tmp_path / "e3.db"
    _create_e2(e2)
    _create_e3(e3, e2)
    return (
        ArtifactSpec(
            "e2_evidence",
            e2,
            e2.stat().st_size,
            sha256_file(e2),
            E2_LOGICAL,
            E2_RUN,
            DEFAULT_E2_APPLICATION_ID,
        ),
        ArtifactSpec(
            "e3_selection",
            e3,
            e3.stat().st_size,
            sha256_file(e3),
            E3_LOGICAL,
            E3_RUN,
            DEFAULT_E3_APPLICATION_ID,
        ),
    )


def test_manifest_binds_both_inputs_and_is_deterministic(tmp_path: Path) -> None:
    e2_spec, e3_spec = create_semantic_fixture(tmp_path)
    manifest = build_semantic_coverage_manifest(
        e2_spec, e3_spec, seed="fixture-seed", enforce_production_counts=False
    )
    replay = build_semantic_coverage_manifest(
        e2_spec, e3_spec, seed="fixture-seed", enforce_production_counts=False
    )
    assert manifest == replay
    assert manifest["sample_size_buildings"] == 10
    assert manifest["planned_occurrence_count"] == 60
    assert manifest["planned_unique_e1_pixel_count"] == 60
    assert manifest["network_requests"] == manifest["vision_requests"] == 0
    assert manifest["e2_input"]["byte_sha256"] == e2_spec.expected_sha256
    assert manifest["e3_input"]["byte_sha256"] == e3_spec.expected_sha256
    stored = manifest["semantic_coverage_manifest_sha256"]
    body = dict(manifest)
    del body["semantic_coverage_manifest_sha256"]
    assert stored == canonical_sha256(
        {"domain": SEMANTIC_COVERAGE_MANIFEST_DOMAIN, "manifest": body}
    )
    assert [
        row["selected_building"]["guard_source"] for row in manifest["selected_buildings"]
    ].count("architizer") == 5


def test_manifest_write_is_canonical_and_no_clobber(tmp_path: Path) -> None:
    e2_spec, e3_spec = create_semantic_fixture(tmp_path)
    manifest = build_semantic_coverage_manifest(
        e2_spec, e3_spec, enforce_production_counts=False
    )
    output = write_semantic_coverage_manifest(tmp_path / "plan.json", manifest)
    before = output.read_bytes()
    assert before.endswith(b"\n")
    assert json.loads(before) == manifest
    with pytest.raises(FileExistsError):
        write_semantic_coverage_manifest(output, manifest)
    assert output.read_bytes() == before


def test_input_sidecar_is_rejected_without_modifying_database(tmp_path: Path) -> None:
    e2_spec, e3_spec = create_semantic_fixture(tmp_path)
    sidecar = Path(str(e2_spec.path) + "-wal")
    sidecar.write_bytes(b"not-a-real-wal")
    before = e2_spec.path.read_bytes()
    with pytest.raises(RuntimeError, match="sidecars"):
        build_semantic_coverage_manifest(
            e2_spec, e3_spec, enforce_production_counts=False
        )
    assert e2_spec.path.read_bytes() == before
