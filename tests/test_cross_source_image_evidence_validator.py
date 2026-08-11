from __future__ import annotations

import ast
import hashlib
import inspect
import sqlite3
import textwrap
from pathlib import Path

import pytest

from canonical.cross_source_image_evidence import (
    E2_EVIDENCE_VERSION,
    METADATA_NORMALIZATION_VERSION,
    PHASH_PAIR_POLICY_VERSION,
    SAMPLE_POLICY_VERSION,
    canonical_json,
    canonical_sha256,
    deterministic_sample_score,
    phash_band_keys,
    stable_edge_id,
    stable_phash_id,
)
from canonical.cross_source_image_evidence_sidecar import initialize_sidecar
from canonical.cross_source_image_evidence_validator import (
    _validate_phash_pairs,
    logical_evidence_manifest,
    validate_e2_artifact,
)


RUN_ID = "e2-validator-fixture"
STAMP = "2026-08-10T00:00:00Z"
DIV_ASSET = "div-asset"
ARCH_ASSET = "arch-asset"
DIV_BUILDING = "div-building"
ARCH_BUILDING = "arch-building"
DIV_PHASH = "0" * 64
ARCH_PHASH = f"{1:064x}"


def _selection_sha() -> str:
    digest = hashlib.sha256()
    for source, asset in sorted(
        (("divisare", DIV_ASSET), ("architizer", ARCH_ASSET))
    ):
        digest.update(
            canonical_json(
                {"asset_id": asset, "source": source, "status": "success"}
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _pair_id(prefix: str, left: str, right: str) -> str:
    return prefix + canonical_sha256(
        {"left": left, "right": right, "version": E2_EVIDENCE_VERSION}
    )


def _evidence_id(candidate_id: str) -> str:
    return "e2ie_" + canonical_sha256(
        {
            "architizer_asset_id": ARCH_ASSET,
            "building_candidate_id": candidate_id,
            "divisare_asset_id": DIV_ASSET,
            "evidence_kind": "phash_le8",
            "version": E2_EVIDENCE_VERSION,
        }
    )


def _build_fixture(
    root: Path,
    *,
    edge_distance: int = 1,
    metadata_total: int = 1,
    building_evidence_count: int = 1,
    bad_smoke_score: bool = False,
    forbidden_table: bool = False,
    bad_logical_metric: bool = False,
    schema_tamper: str | None = None,
    config_override: dict[str, object] | None = None,
    metric_tamper: str | None = None,
) -> tuple[Path, tuple[Path, ...]]:
    root.mkdir()
    input_paths: list[Path] = []
    roles = (
        ("divisare_curated", "divisare", "source_db"),
        ("divisare_e1", "divisare", "e1_sidecar"),
        ("architizer_curated", "architizer", "source_db"),
        ("architizer_e1", "architizer", "e1_sidecar"),
    )
    input_rows = []
    for role, source, input_role in roles:
        path = root / f"{role}.db"
        path.write_bytes((role + "\n").encode("ascii"))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        input_paths.append(path)
        input_rows.append(
            (
                RUN_ID,
                role,
                source,
                input_role,
                str(path.resolve()),
                path.stat().st_size,
                digest,
                digest,
                STAMP,
            )
        )

    database = root / "e2.db"
    connection = initialize_sidecar(database)
    evidence_only_config = {
        "network_requests": 0,
        "representative_selection": False,
        "vision_requests": 0,
    }
    connection.execute(
        """
        INSERT INTO e2_runs(
          run_id,contract_version,builder_version,selection_mode,sample_size,
          sample_seed,ordered_selection_manifest_sha256,config_json,status,started_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            RUN_ID,
            E2_EVIDENCE_VERSION,
            "fixture-builder-v1",
            "sample",
            2,
            "fixture-seed",
            _selection_sha(),
            canonical_json(
                evidence_only_config
                if config_override is None
                else config_override
            ),
            "building",
            STAMP,
        ),
    )
    connection.executemany(
        """
        INSERT INTO e2_inputs(
          run_id,input_name,source,input_role,file_path,size_bytes,
          sha256_before,sha256_after,recorded_at
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        input_rows,
    )

    for source, building_id in (
        ("divisare", DIV_BUILDING),
        ("architizer", ARCH_BUILDING),
    ):
        connection.execute(
            """
            INSERT INTO source_buildings(
              run_id,source,source_building_id,name,normalized_name,
              source_record_sha256,metadata_json
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                RUN_ID,
                source,
                building_id,
                "Fixture Building",
                "fixture building",
                canonical_sha256({"building": building_id}),
                canonical_json({}),
            ),
        )

    asset_values = (
        (
            "divisare",
            DIV_ASSET,
            "a" * 64,
            DIV_PHASH,
            "https://divisare.example/image.jpg",
        ),
        (
            "architizer",
            ARCH_ASSET,
            "b" * 64,
            ARCH_PHASH,
            "https://architizer.example/image.jpg",
        ),
    )
    for source, asset_id, pixel_sha, phash, url in asset_values:
        connection.execute(
            """
            INSERT INTO assets(
              run_id,source,source_asset_id,e1_run_id,fingerprint_status,
              canonical_url,raw_response_sha256,normalized_pixel_sha256,
              phash_hex,source_record_sha256,provenance_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                RUN_ID,
                source,
                asset_id,
                f"{source}-e1",
                "success",
                url,
                canonical_sha256({"response": asset_id}),
                pixel_sha,
                phash,
                canonical_sha256({"asset": asset_id}),
                canonical_json({}),
            ),
        )
        node_id = stable_phash_id(phash)
        connection.execute(
            """
            INSERT INTO phash_nodes(
              run_id,node_id,phash_hex,member_count,source_count,is_cross_source
            ) VALUES(?,?,?,?,?,?)
            """,
            (RUN_ID, node_id, phash, 1, 1, 0),
        )
        connection.execute(
            """
            INSERT INTO phash_node_members(
              run_id,node_id,source,source_asset_id
            ) VALUES(?,?,?,?)
            """,
            (RUN_ID, node_id, source, asset_id),
        )

    for source, building_id, asset_id in (
        ("divisare", DIV_BUILDING, DIV_ASSET),
        ("architizer", ARCH_BUILDING, ARCH_ASSET),
    ):
        connection.execute(
            """
            INSERT INTO building_assets(
              run_id,source,source_building_id,source_asset_id,project_count,
              occurrence_count,roles_json,relation_record_sha256
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                RUN_ID,
                source,
                building_id,
                asset_id,
                1,
                1,
                canonical_json(["gallery"]),
                canonical_sha256({"building": building_id, "asset": asset_id}),
            ),
        )

    left_node, right_node = sorted(
        (stable_phash_id(DIV_PHASH), stable_phash_id(ARCH_PHASH))
    )
    distance = 1
    shared_bands = sum(
        left == right
        for left, right in zip(phash_band_keys(DIV_PHASH), phash_band_keys(ARCH_PHASH))
    )
    global_candidate_id = stable_edge_id(
        left_node, right_node, "phash-global-le8-candidate"
    )
    global_record = {
        "candidate_id": global_candidate_id,
        "distance": distance,
        "left_node_id": left_node,
        "right_node_id": right_node,
        "scope": "global_le8",
        "shared_band_count": shared_bands,
    }
    connection.execute(
        """
        INSERT INTO phash_candidates(
          run_id,candidate_id,left_node_id,right_node_id,candidate_scope,
          shared_band_count,recomputed_distance,passed_threshold,
          candidate_record_sha256,detail_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            RUN_ID,
            global_candidate_id,
            left_node,
            right_node,
            "global_le8",
            shared_bands,
            distance,
            1,
            canonical_sha256(global_record),
            canonical_json({}),
        ),
    )
    metadata_candidate_id = stable_edge_id(
        left_node, right_node, "phash-metadata-le16-candidate"
    )
    metadata_candidate_record = {
        "candidate_id": metadata_candidate_id,
        "distance": distance,
        "left_node_id": left_node,
        "right_node_id": right_node,
        "scope": "metadata_le16",
    }
    connection.execute(
        """
        INSERT INTO phash_candidates(
          run_id,candidate_id,left_node_id,right_node_id,candidate_scope,
          shared_band_count,recomputed_distance,passed_threshold,
          candidate_record_sha256,detail_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            RUN_ID,
            metadata_candidate_id,
            left_node,
            right_node,
            "metadata_le16",
            0,
            distance,
            1,
            canonical_sha256(metadata_candidate_record),
            canonical_json({"pair_policy_version": PHASH_PAIR_POLICY_VERSION}),
        ),
    )
    edge_id = stable_edge_id(left_node, right_node, "phash-global-le8")
    edge_record = {
        "candidate_id": global_candidate_id,
        "distance": edge_distance,
        "edge_id": edge_id,
        "left_node_id": left_node,
        "right_node_id": right_node,
        "scope": "global_le8",
    }
    connection.execute(
        """
        INSERT INTO phash_edges(
          run_id,edge_id,left_node_id,right_node_id,hamming_distance,edge_scope,
          candidate_id,edge_record_sha256,detail_json
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            RUN_ID,
            edge_id,
            left_node,
            right_node,
            edge_distance,
            "global_le8",
            global_candidate_id,
            canonical_sha256(edge_record),
            canonical_json({"direct_edge": True}),
        ),
    )

    metadata_pair_id = _pair_id("e2mp_", DIV_BUILDING, ARCH_BUILDING)
    accounting = {
        "architizer_node_count": 1,
        "compared_distinct_node_pairs": 1,
        "distance_9_16_pairs": 0,
        "distance_above_16_pairs": 0,
        "divisare_node_count": 1,
        "identical_node_pairs": 0,
        "total_cartesian_node_pairs": metadata_total,
    }
    metadata_evidence = {
        "no_identity_decision": True,
        "phash_cartesian_accounting": accounting,
    }
    metadata_record = {
        "architizer_building_id": ARCH_BUILDING,
        "blocker_version": METADATA_NORMALIZATION_VERSION,
        "divisare_building_id": DIV_BUILDING,
        "evidence": metadata_evidence,
        "metadata_pair_id": metadata_pair_id,
    }
    connection.execute(
        """
        INSERT INTO metadata_building_pairs(
          run_id,metadata_pair_id,left_source,left_source_building_id,
          right_source,right_source_building_id,blocker_version,discovery_reason,
          normalized_name_equal,metadata_record_sha256,evidence_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            RUN_ID,
            metadata_pair_id,
            "divisare",
            DIV_BUILDING,
            "architizer",
            ARCH_BUILDING,
            METADATA_NORMALIZATION_VERSION,
            "exact_conservative_normalized_name",
            1,
            canonical_sha256(metadata_record),
            canonical_json(metadata_evidence),
        ),
    )

    building_candidate_id = _pair_id("e2bc_", DIV_BUILDING, ARCH_BUILDING)
    building_record = {
        "architizer_building_id": ARCH_BUILDING,
        "candidate_id": building_candidate_id,
        "counts": {
            "exact_pixel": 0,
            "identical_phash": 0,
            "phash_9_16": 0,
            "phash_le8": building_evidence_count,
        },
        "divisare_building_id": DIV_BUILDING,
        "metadata_pair_id": metadata_pair_id,
        "min_phash_distance": edge_distance,
    }
    connection.execute(
        """
        INSERT INTO cross_source_building_candidates(
          run_id,building_candidate_id,left_source,left_source_building_id,
          right_source,right_source_building_id,metadata_pair_id,
          phash_le8_pair_count,min_phash_distance,discovery_basis_json,
          candidate_record_sha256
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            RUN_ID,
            building_candidate_id,
            "divisare",
            DIV_BUILDING,
            "architizer",
            ARCH_BUILDING,
            metadata_pair_id,
            building_evidence_count,
            edge_distance,
            canonical_json({"evidence_only": True}),
            canonical_sha256(building_record),
        ),
    )
    evidence_id = _evidence_id(building_candidate_id)
    image_record = {
        "architizer_asset_id": ARCH_ASSET,
        "building_candidate_id": building_candidate_id,
        "divisare_asset_id": DIV_ASSET,
        "evidence_id": evidence_id,
        "evidence_kind": "phash_le8",
        "exact_cluster_id": None,
        "phash_distance": edge_distance,
        "phash_edge_id": edge_id,
    }
    connection.execute(
        """
        INSERT INTO candidate_image_evidence(
          run_id,evidence_id,building_candidate_id,left_source,
          left_source_asset_id,right_source,right_source_asset_id,evidence_kind,
          phash_edge_id,phash_distance,evidence_record_sha256,detail_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            RUN_ID,
            evidence_id,
            building_candidate_id,
            "divisare",
            DIV_ASSET,
            "architizer",
            ARCH_ASSET,
            "phash_le8",
            edge_id,
            edge_distance,
            canonical_sha256(image_record),
            canonical_json({}),
        ),
    )

    sample_records = []
    for rank, (source, asset_id) in enumerate(
        (("architizer", ARCH_ASSET), ("divisare", DIV_ASSET)), 1
    ):
        score = deterministic_sample_score(
            "fixture-seed", f"{source}:{asset_id}"
        )
        stored_score = "f" * 64 if bad_smoke_score and rank == 1 else score
        record = {
            "asset_id": asset_id,
            "rank": rank,
            "reason": "deterministic_fill",
            "score": stored_score,
            "source": source,
        }
        sample_records.append(record)
    connection.execute(
        """
        INSERT INTO smoke_manifests(
          run_id,manifest_name,sample_size,sample_seed,selection_version,
          ordered_manifest_sha256,selection_scope_json,created_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            RUN_ID,
            "real_n2",
            2,
            "fixture-seed",
            SAMPLE_POLICY_VERSION,
            canonical_sha256(sample_records),
            canonical_json({"entity": "source_asset"}),
            STAMP,
        ),
    )
    for record in sample_records:
        connection.execute(
            """
            INSERT INTO smoke_manifest_items(
              run_id,manifest_name,selection_rank,entity_kind,source,
              source_entity_id,stratum,score_sha256,item_record_sha256,detail_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                RUN_ID,
                "real_n2",
                record["rank"],
                "asset",
                record["source"],
                record["asset_id"],
                record["reason"],
                record["score"],
                canonical_sha256(record),
                canonical_json({}),
            ),
        )

    if forbidden_table:
        connection.execute("CREATE TABLE vision_queue(item_id TEXT PRIMARY KEY)")
    if schema_tamper:
        connection.execute(schema_tamper)
    logical_sha, _ = logical_evidence_manifest(connection, RUN_ID)
    connection.execute(
        """
        INSERT INTO e2_metrics(
          run_id,phase,metric_name,stratum_json,value_text,recorded_at
        ) VALUES(?,?,?,?,?,?)
        """,
        (
            RUN_ID,
            "validation",
            "output_logical_sha256",
            canonical_json({}),
            "0" * 64 if bad_logical_metric else logical_sha,
            STAMP,
        ),
    )
    connection.executemany(
        """
        INSERT INTO e2_metrics(
          run_id,phase,metric_name,stratum_json,value_integer,recorded_at
        ) VALUES(?,?,?,?,?,?)
        """,
        [
            (RUN_ID, "validation", name, canonical_json({}), 0, STAMP)
            for name in ("network_requests", "vision_requests", "llm_requests")
        ],
    )
    if metric_tamper:
        connection.execute(metric_tamper)
    connection.execute(
        """
        INSERT INTO e2_validations(
          run_id,validation_name,severity,passed,expected,actual,recorded_at
        ) VALUES(?,?,?,?,?,?,?)
        """,
        (RUN_ID, "fixture", "error", 1, "0", "0", STAMP),
    )
    connection.execute(
        """
        UPDATE e2_runs SET status='complete',completed_at=? WHERE run_id=?
        """,
        (STAMP, RUN_ID),
    )
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    assert connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    connection.close()
    return database, tuple(input_paths)


def test_valid_terminal_fixture_passes_independent_validation(tmp_path: Path) -> None:
    database, _ = _build_fixture(tmp_path / "valid")
    report = validate_e2_artifact(database)
    assert report.passed, report.failed_check_names
    assert report.run_id == RUN_ID
    assert report.logical_sha256 is not None
    assert len(report.input_files) == 4


def test_missing_edge_validation_uses_node_pair_scope_point_lookup(
    tmp_path: Path,
) -> None:
    database, _ = _build_fixture(tmp_path / "missing_edge_query_plan")
    parsed = ast.parse(textwrap.dedent(inspect.getsource(_validate_phash_pairs)))
    matches = [
        node.value
        for node in ast.walk(parsed)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "SELECT count(*) FROM phash_candidates c WHERE c.run_id=?" in node.value
    ]
    assert len(matches) == 1
    missing_edge_sql = matches[0]
    normalized_sql = " ".join(missing_edge_sql.split())
    assert "e.candidate_id=c.candidate_id" not in normalized_sql
    assert "e.left_node_id=c.left_node_id" in normalized_sql
    assert "e.right_node_id=c.right_node_id" in normalized_sql

    connection = sqlite3.connect(
        database.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
    )
    try:
        details = [
            str(row[3])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN " + missing_edge_sql, (RUN_ID,)
            )
        ]
    finally:
        connection.close()

    edge_searches = [detail for detail in details if "SEARCH e USING" in detail]
    assert len(edge_searches) == 2
    point_lookup = (
        "sqlite_autoindex_phash_edges_2 "
        "(run_id=? AND left_node_id=? AND right_node_id=? AND edge_scope=?)"
    )
    assert all(point_lookup in detail for detail in edge_searches)
    assert not any("idx_phash_edges_distance" in detail for detail in edge_searches)


@pytest.mark.parametrize(
    ("kwargs", "failed_check"),
    [
        ({"edge_distance": 2}, "phash_edge_recomputation"),
        ({"metadata_total": 2}, "metadata_cartesian_accounting"),
        ({"building_evidence_count": 2}, "building_candidate_aggregate_counts"),
        ({"bad_smoke_score": True}, "ordered_smoke_manifests"),
        ({"forbidden_table": True}, "forbidden_policy_tables_absent"),
        (
            {"schema_tamper": "CREATE TABLE image_rankings(id TEXT PRIMARY KEY)"},
            "only_contract_tables_present",
        ),
        (
            {
                "schema_tamper":
                    "CREATE VIEW vision_queue AS SELECT run_id FROM e2_runs"
            },
            "forbidden_policy_tables_absent",
        ),
        (
            {
                "schema_tamper":
                    "CREATE VIEW evidence_summary AS SELECT run_id FROM e2_runs"
            },
            "views_absent",
        ),
        (
            {
                "schema_tamper":
                    "ALTER TABLE assets ADD COLUMN representative_score REAL"
            },
            "exact_table_columns",
        ),
        ({"schema_tamper": "PRAGMA user_version=2"}, "schema_version_contract"),
        ({"schema_tamper": "PRAGMA application_id=0"}, "application_id_contract"),
        (
            {
                "config_override": {
                    "network_requests": 0,
                    "vision_requests": 0,
                }
            },
            "evidence_only_run_config",
        ),
        (
            {
                "config_override": {
                    "network_requests": 0,
                    "representative_selection": False,
                    "vision_requests": 1,
                }
            },
            "evidence_only_run_config",
        ),
        (
            {
                "metric_tamper":
                    "DELETE FROM e2_metrics WHERE metric_name='llm_requests'"
            },
            "evidence_only_zero_request_metrics",
        ),
        (
            {
                "metric_tamper":
                    "UPDATE e2_metrics SET value_integer=1 "
                    "WHERE metric_name='vision_requests'"
            },
            "evidence_only_zero_request_metrics",
        ),
        ({"bad_logical_metric": True}, "logical_manifest_matches_stored"),
    ],
)
def test_validator_detects_independent_contract_tampering(
    tmp_path: Path, kwargs: dict[str, object], failed_check: str
) -> None:
    database, _ = _build_fixture(tmp_path / failed_check, **kwargs)
    report = validate_e2_artifact(database)
    assert not report.passed
    assert failed_check in report.failed_check_names


def test_validator_detects_input_file_changed_after_build(tmp_path: Path) -> None:
    database, inputs = _build_fixture(tmp_path / "input_changed")
    inputs[0].write_bytes(b"changed after the terminal build\n")
    report = validate_e2_artifact(database)
    assert not report.passed
    assert "immutable_input_files" in report.failed_check_names


def test_validator_can_compare_an_external_expected_logical_sha(tmp_path: Path) -> None:
    database, _ = _build_fixture(tmp_path / "expected_sha")
    report = validate_e2_artifact(database, expected_logical_sha256="f" * 64)
    assert not report.passed
    assert "logical_manifest_matches_expected" in report.failed_check_names
