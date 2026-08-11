from __future__ import annotations

import json
import os
import socket
import sqlite3
from pathlib import Path

import pytest

import canonical.cross_source_image_selection_sidecar as sidecar
from canonical.cross_source_image_selection_sidecar import (
    APPLICATION_ID,
    FORBIDDEN_POLICY_TABLE_NAMES,
    SCHEMA_VERSION,
    TABLE_NAMES,
    BuildLockError,
    SidecarSchemaError,
    acquire_build_lock,
    finalize_sidecar,
    initialize_sidecar,
    open_sidecar,
    recover_sidecar,
    validate_sidecar,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
SHA_F = "f" * 64
E2_PATH = "C:/frozen/divisare_architizer_image_evidence_e2_full_v5.db"
RUN_CONFIG = json.dumps(
    {
        "artifact_scope": "candidate_only",
        "authoritative": 0,
        "llm_requests": 0,
        "network_requests": 0,
        "vision_requests": 0,
    },
    sort_keys=True,
    separators=(",", ":"),
)


def _insert_run(connection: sqlite3.Connection, *, sample_size: int = 1) -> None:
    connection.execute(
        """
        INSERT INTO selection_runs(
          run_id,contract_version,builder_version,e2_artifact_path,e2_size_bytes,
          e2_byte_sha256,e2_logical_sha256,policy_set_sha256,selection_mode,
          sample_size,sample_seed,shortlist_size,config_json,started_at
        ) VALUES(
          'e3-test','e3-selection-v1','test-builder-v1',?,123,?,?,?,'sample',
          ?,'fixed-seed',3,?,'2026-08-11T00:00:00Z'
        )
        """,
        (E2_PATH, SHA_A, SHA_B, SHA_C, sample_size, RUN_CONFIG),
    )
    connection.execute(
        """
        INSERT INTO selection_inputs(
          run_id,input_name,input_role,file_path,size_bytes,sha256_before,
          sha256_after,logical_sha256,application_id,user_version,
          schema_manifest_sha256,recorded_at
        ) VALUES(
          'e3-test','e2','e2_evidence',?,123,?,?,?,?,1,?,
          '2026-08-11T00:00:01Z'
        )
        """,
        (E2_PATH, SHA_A, SHA_A, SHA_B, APPLICATION_ID, SHA_C),
    )
    connection.executemany(
        """
        INSERT INTO selection_metrics(
          run_id,phase,metric_name,stratum_json,value_integer,recorded_at
        ) VALUES('e3-test','validation',?,'{}',0,'2026-08-11T00:00:02Z')
        """,
        [("network_requests",), ("vision_requests",), ("llm_requests",)],
    )


def _insert_policy_population(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        INSERT INTO policy_definitions(
          run_id,policy_id,policy_version,policy_name,description,shortlist_size,
          enabled,definition_json,policy_config_sha256,policy_record_sha256,
          created_at
        ) VALUES(
          'e3-test','p0','policy-v1','Editorial baseline','fixture',3,1,'{}',
          ?,?,'2026-08-11T00:00:03Z'
        )
        """,
        (SHA_D, SHA_E),
    )
    connection.execute(
        """
        INSERT INTO population_strata(
          run_id,stratum_id,stratum_key,stratum_json,population_count,
          eligible_count,selected_building_count,selected_candidate_count,
          stratum_record_sha256
        ) VALUES('e3-test','s0','divisare|cover','{}',10,8,1,1,?)
        """,
        (SHA_F,),
    )
    connection.execute(
        """
        INSERT INTO selected_buildings(
          run_id,selection_id,selection_rank,stratum_id,source,entity_type,
          source_entity_id,source_building_id,source_project_id,name,
          normalized_name,selection_reason,e2_source_record_sha256,
          e2_relation_record_sha256,selection_record_sha256
        ) VALUES(
          'e3-test','sel-1',1,'s0','divisare','building','b1','b1',NULL,
          'Building','building','stratified_sample',?,?,?
        )
        """,
        (SHA_A, SHA_B, SHA_C),
    )
    connection.execute(
        """
        INSERT INTO image_candidates(
          run_id,candidate_id,selection_id,source,source_building_id,
          source_project_id,source_asset_id,fingerprint_status,canonical_url,
          roles_json,primary_role,role_rank,source_ordinal,ordinal_is_derived,
          original_width,original_height,normalized_width,normalized_height,
          quality_flags_json,low_information,normalized_pixel_sha256,
          exact_cluster_id,phash_node_id,source_record_sha256,
          occurrence_record_sha256,project_relation_record_sha256,
          building_relation_record_sha256,candidate_record_sha256
        ) VALUES(
          'e3-test','cand-1','sel-1','divisare','b1','p1','asset-1','success',
          'https://example.test/image.jpg','["cover"]','cover',0,0,0,
          1600,1200,512,384,'[]',0,?,NULL,'phash-node-1',?,?,?,?,?
        )
        """,
        (SHA_A, SHA_B, SHA_C, SHA_D, SHA_E, SHA_F),
    )
    connection.execute(
        """
        INSERT INTO policy_rankings(
          run_id,policy_id,policy_version,policy_config_sha256,selection_id,
          candidate_id,ranking_state,editorial_rank,shortlist_rank,selected,
          qa_fallback,hard_risk,rank_tuple_json,component_scores_json,
          reasons_json,ranking_record_sha256
        ) VALUES(
          'e3-test','p0','policy-v1',?,'sel-1','cand-1','shortlisted',1,1,1,
          0,0,'[0,0]','{"role":0}','["editorial_cover"]',?
        )
        """,
        (SHA_D, SHA_E),
    )
    connection.execute(
        """
        INSERT INTO shortlist_items(
          run_id,policy_id,selection_id,shortlist_rank,candidate_id,
          shortlist_state,authoritative,item_record_sha256
        ) VALUES('e3-test','p0','sel-1',1,'cand-1','primary',0,?)
        """,
        (SHA_F,),
    )
    connection.execute(
        """
        INSERT INTO queue_estimates(
          run_id,estimate_id,policy_id,stratum_id,queue_unit,population_count,
          estimated_queue_items,tokens_per_item_low,tokens_per_item_point,
          tokens_per_item_high,projected_total_tokens,estimated_calls,
          retry_factor,pricing_snapshot_json,requests_executed,authoritative,
          estimate_record_sha256,created_at
        ) VALUES(
          'e3-test','q0','p0','s0','shortlist_item',1,1,3700,4200,4500,
          4200,1,1.0,'{}',0,0,?,'2026-08-11T00:00:04Z'
        )
        """,
        (SHA_A,),
    )


def _insert_validation(connection: sqlite3.Connection, *, passed: bool) -> None:
    connection.execute(
        """
        INSERT INTO selection_validations(
          run_id,validation_name,severity,passed,expected,actual,recorded_at
        ) VALUES(
          'e3-test','fixture_validation','error',?,?,?,
          '2026-08-11T00:00:05Z'
        )
        """,
        (int(passed), "pass", "pass" if passed else "fail"),
    )


def _complete_fixture(connection: sqlite3.Connection) -> Path:
    _insert_validation(connection, passed=True)
    connection.execute(
        """UPDATE selection_runs
           SET ordered_selection_manifest_sha256=? WHERE run_id='e3-test'""",
        (SHA_F,),
    )
    return finalize_sidecar(
        connection,
        status="complete",
        completed_at="2026-08-11T00:01:00Z",
    )


def _build_valid_fixture(path: Path) -> None:
    connection = initialize_sidecar(path)
    _insert_run(connection)
    _insert_policy_population(connection)
    _complete_fixture(connection)


def test_schema_is_candidate_only_and_initializer_never_clobbers(
    tmp_path: Path,
) -> None:
    path = tmp_path / "e3.db"
    connection = initialize_sidecar(path)
    try:
        assert connection.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            )
        }
        assert tables == set(TABLE_NAMES)
        assert not (set(FORBIDDEN_POLICY_TABLE_NAMES) & tables)
    finally:
        connection.close()

    original = path.read_bytes()
    with pytest.raises(FileExistsError):
        initialize_sidecar(path)
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    ("ddl", "error"),
    [
        ("CREATE TABLE extra_policy_output(id TEXT)", "unexpected E3 tables"),
        (
            "CREATE VIEW representative_images AS SELECT run_id FROM selection_runs",
            "forbidden authoritative/Vision",
        ),
        (
            "CREATE VIEW selection_summary AS SELECT run_id FROM selection_runs",
            "must not contain views",
        ),
        (
            "ALTER TABLE image_candidates ADD COLUMN final_winner INTEGER",
            "column contract mismatch",
        ),
        (
            "CREATE INDEX extra_candidate_index ON image_candidates(source)",
            "unexpected E3 indexes",
        ),
    ],
)
def test_schema_allow_list_rejects_extra_or_authoritative_objects(
    tmp_path: Path,
    ddl: str,
    error: str,
) -> None:
    path = tmp_path / "tampered.db"
    connection = initialize_sidecar(path)
    connection.execute(ddl)
    connection.commit()
    connection.close()

    with pytest.raises(SidecarSchemaError, match=error):
        open_sidecar(path, readonly=True, immutable=False)


def test_complete_fixture_is_immutable_and_independently_valid(tmp_path: Path) -> None:
    path = tmp_path / "complete.db"
    connection = initialize_sidecar(path)
    _insert_run(connection)
    _insert_policy_population(connection)
    connection.commit()

    assert Path(str(path) + "-wal").exists()
    with pytest.raises(SidecarSchemaError, match="recovery/checkpoint"):
        open_sidecar(path, readonly=True, immutable=True)

    final_path = _complete_fixture(connection)
    assert final_path == path.resolve()
    assert not tuple(sidecar.sqlite_sidecar_paths(path))

    immutable = open_sidecar(path, readonly=True, immutable=True)
    assert immutable.execute("PRAGMA query_only").fetchone()[0] == 1
    row = immutable.execute(
        "SELECT authoritative,artifact_scope,network_requests,vision_requests,llm_requests "
        "FROM selection_runs"
    ).fetchone()
    assert tuple(row) == (0, "candidate_only", 0, 0, 0)
    with pytest.raises(sqlite3.OperationalError):
        immutable.execute("DELETE FROM shortlist_items")
    immutable.close()

    result = validate_sidecar(path)
    assert result.passed
    assert result.structurally_valid
    assert result.run_status == "complete"
    assert result.quick_check == "ok"
    assert result.integrity_check == "ok"
    assert result.foreign_key_violations == 0
    assert all(count == 0 for _, count in result.semantic_violations)
    assert dict(result.table_counts)["image_candidates"] == 1
    assert dict(result.table_counts)["shortlist_items"] == 1


def test_terminal_status_and_all_payload_tables_are_immutable(tmp_path: Path) -> None:
    path = tmp_path / "terminal.db"
    _build_valid_fixture(path)

    with pytest.raises(SidecarSchemaError, match="cannot be opened writable"):
        open_sidecar(path, readonly=False)

    direct = sqlite3.connect(path)
    direct.execute("PRAGMA foreign_keys=ON")
    with pytest.raises(sqlite3.IntegrityError, match="terminal E3 selection"):
        direct.execute(
            """
            INSERT INTO selection_metrics(
              run_id,phase,metric_name,value_integer,recorded_at
            ) VALUES('e3-test','late','late_metric',1,'late')
            """
        )
    direct.close()


def test_complete_requires_passed_error_validation(tmp_path: Path) -> None:
    path = tmp_path / "failed-gate.db"
    connection = initialize_sidecar(path)
    _insert_run(connection)
    _insert_policy_population(connection)
    _insert_validation(connection, passed=False)
    connection.execute(
        "UPDATE selection_runs SET ordered_selection_manifest_sha256=?",
        (SHA_F,),
    )
    with pytest.raises(sqlite3.IntegrityError, match="passing error validations"):
        finalize_sidecar(connection, status="complete", close=False)
    connection.rollback()
    connection.close()


def test_failed_validation_is_terminal_but_not_passed(tmp_path: Path) -> None:
    path = tmp_path / "failed.db"
    connection = initialize_sidecar(path)
    _insert_run(connection)
    _insert_policy_population(connection)
    _insert_validation(connection, passed=False)
    finalize_sidecar(
        connection,
        status="failed_validation",
        completed_at="2026-08-11T00:01:00Z",
        error="fixture failure",
    )

    result = validate_sidecar(path)
    assert result.structurally_valid
    assert not result.passed
    assert result.run_status == "failed_validation"


def test_validator_detects_zero_request_metric_tampering(tmp_path: Path) -> None:
    path = tmp_path / "metric-tampered.db"
    connection = initialize_sidecar(path)
    _insert_run(connection)
    _insert_policy_population(connection)
    connection.execute(
        """UPDATE selection_metrics SET value_integer=1
           WHERE metric_name='vision_requests'"""
    )
    _complete_fixture(connection)

    result = validate_sidecar(path)
    assert dict(result.semantic_violations)["request_metric_mismatch"] == 1
    assert not result.structurally_valid
    assert not result.passed


def test_validator_binds_e2_byte_and_logical_lineage(tmp_path: Path) -> None:
    path = tmp_path / "lineage-tampered.db"
    connection = initialize_sidecar(path)
    _insert_run(connection)
    _insert_policy_population(connection)
    connection.execute(
        "UPDATE selection_inputs SET logical_sha256=? WHERE input_role='e2_evidence'",
        (SHA_C,),
    )
    _complete_fixture(connection)

    result = validate_sidecar(path)
    assert dict(result.semantic_violations)["e2_input_lineage_mismatch"] == 1
    assert not result.passed


def test_candidate_and_shortlist_contracts_reject_invalid_rows(tmp_path: Path) -> None:
    path = tmp_path / "constraints.db"
    connection = initialize_sidecar(path)
    _insert_run(connection)
    _insert_policy_population(connection)

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO image_candidates(
              run_id,candidate_id,selection_id,source,source_building_id,
              source_asset_id,fingerprint_status,roles_json,primary_role,
              role_rank,quality_flags_json,low_information,
              normalized_pixel_sha256,phash_node_id,source_record_sha256,
              building_relation_record_sha256,candidate_record_sha256
            ) VALUES(
              'e3-test','bad','sel-1','divisare','b1','bad-asset','failed',
              '[]','gallery',1,'[]',0,?,'node',?,?,?
            )
            """,
            (SHA_A, SHA_B, SHA_C, SHA_D),
        )

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO shortlist_items(
              run_id,policy_id,selection_id,shortlist_rank,candidate_id,
              shortlist_state,item_record_sha256
            ) VALUES('e3-test','p0','sel-1',2,'not-ranked','primary',?)
            """,
            (SHA_A,),
        )
    connection.close()


def test_recovery_open_close_preserves_building_rows(tmp_path: Path) -> None:
    path = tmp_path / "recover.db"
    connection = initialize_sidecar(path)
    _insert_run(connection)
    connection.commit()
    connection.close()

    recover_sidecar(path, switch_to_delete=True)
    assert not tuple(sidecar.sqlite_sidecar_paths(path))
    writable = open_sidecar(path, readonly=False, immutable=False)
    assert writable.execute("SELECT count(*) FROM selection_runs").fetchone()[0] == 1
    writable.close()


def test_lock_rejects_live_owner_and_releases_only_its_token(tmp_path: Path) -> None:
    path = tmp_path / "e3.db.lock"
    lock = acquire_build_lock(path)
    payload = json.loads(path.read_text(encoding="ascii"))
    assert payload["pid"] == os.getpid()
    assert payload["hostname"] == socket.gethostname()

    with pytest.raises(BuildLockError, match="lock is held"):
        acquire_build_lock(path)

    changed = dict(payload)
    changed["token"] = "not-our-token"
    path.write_text(json.dumps(changed), encoding="ascii")
    with pytest.raises(BuildLockError, match="another process"):
        lock.release()
    path.write_text(json.dumps(payload), encoding="ascii")
    lock.release()
    assert not path.exists()
    lock.release()


def test_lock_recovers_only_dead_same_host_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "e3.db.lock"
    stale = {
        "created_at": "2026-08-11T00:00:00Z",
        "hostname": socket.gethostname(),
        "pid": 999_999_999,
        "token": "stale-token",
    }
    path.write_text(json.dumps(stale), encoding="ascii")
    monkeypatch.setattr(sidecar, "_pid_is_alive", lambda pid: False)
    recovered = acquire_build_lock(path)
    assert json.loads(path.read_text(encoding="ascii"))["token"] == recovered.token
    recovered.release()
    assert not list(tmp_path.glob("*.stale-*"))

    foreign = dict(stale)
    foreign["hostname"] = "another-host"
    path.write_text(json.dumps(foreign), encoding="ascii")
    with pytest.raises(BuildLockError, match="lock is held"):
        acquire_build_lock(path)
    assert json.loads(path.read_text(encoding="ascii")) == foreign


def test_malformed_lock_is_never_auto_removed(tmp_path: Path) -> None:
    path = tmp_path / "e3.db.lock"
    path.write_text("not-json", encoding="ascii")
    with pytest.raises(BuildLockError, match="manual inspection"):
        acquire_build_lock(path)
    assert path.read_text(encoding="ascii") == "not-json"


def test_schema_identity_rejects_unrelated_sqlite_file(tmp_path: Path) -> None:
    path = tmp_path / "wrong.db"
    sqlite3.connect(path).close()
    with pytest.raises(SidecarSchemaError, match="application_id mismatch"):
        open_sidecar(path, readonly=True, immutable=True)
