from __future__ import annotations

import json
import os
import socket
import sqlite3
from pathlib import Path

import pytest

import canonical.cross_source_image_evidence_sidecar as sidecar
from canonical.cross_source_image_evidence_sidecar import (
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
    validate_sidecar,
)


SHA_A = "a" * 64
EVIDENCE_ONLY_CONFIG = json.dumps(
    {
        "network_requests": 0,
        "representative_selection": False,
        "vision_requests": 0,
    },
    sort_keys=True,
    separators=(",", ":"),
)


def _insert_run(
    connection: sqlite3.Connection,
    *,
    config_json: str = EVIDENCE_ONLY_CONFIG,
) -> None:
    connection.execute(
        """
        INSERT INTO e2_runs(
          run_id,contract_version,builder_version,selection_mode,
          config_json,status,started_at
        ) VALUES('e2-test','e2-evidence-v1','test-builder-v1','full',?,
                 'building','2026-08-10T00:00:00Z')
        """,
        (config_json,),
    )
    connection.executemany(
        """
        INSERT INTO e2_metrics(
          run_id,phase,metric_name,stratum_json,value_integer,recorded_at
        ) VALUES('e2-test','validation',?,'{}',0,'2026-08-10T00:00:30Z')
        """,
        [("network_requests",), ("vision_requests",), ("llm_requests",)],
    )


def _insert_validation(
    connection: sqlite3.Connection,
    *,
    passed: bool,
) -> None:
    connection.execute(
        """
        INSERT INTO e2_validations(
          run_id,validation_name,severity,passed,expected,actual,recorded_at
        ) VALUES('e2-test','fixture_validation','error',?,?,?,
                 '2026-08-10T00:01:00Z')
        """,
        (int(passed), "pass", "pass" if passed else "fail"),
    )


def _finish_complete(connection: sqlite3.Connection) -> Path:
    _insert_validation(connection, passed=True)
    connection.execute(
        """UPDATE e2_runs SET ordered_selection_manifest_sha256=?
           WHERE run_id='e2-test'""",
        (SHA_A,),
    )
    return finalize_sidecar(
        connection,
        status="complete",
        completed_at="2026-08-10T00:02:00Z",
    )


def test_schema_is_evidence_only_and_initializer_never_clobbers(
    tmp_path: Path,
) -> None:
    path = tmp_path / "e2.db"
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
        assert set(TABLE_NAMES) <= tables
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
        (
            "CREATE TABLE image_rankings(item_id TEXT PRIMARY KEY)",
            "unexpected E2 tables",
        ),
        (
            "CREATE VIEW vision_queue AS SELECT run_id FROM e2_runs",
            "forbidden policy schema objects",
        ),
        (
            "CREATE VIEW evidence_summary AS SELECT run_id FROM e2_runs",
            "must not contain views",
        ),
        (
            "ALTER TABLE assets ADD COLUMN representative_score REAL",
            "column contract mismatch",
        ),
        (
            "CREATE INDEX extra_asset_source ON assets(source)",
            "unexpected E2 indexes",
        ),
        (
            "CREATE TRIGGER extra_assets_guard BEFORE INSERT ON assets "
            "BEGIN SELECT RAISE(ABORT, 'extra'); END",
            "unexpected E2 triggers",
        ),
    ],
)
def test_schema_allow_list_rejects_policy_and_extra_objects(
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


def test_immutable_open_requires_checkpoint_then_validates_complete_run(
    tmp_path: Path,
) -> None:
    path = tmp_path / "e2.db"
    connection = initialize_sidecar(path)
    _insert_run(connection)
    connection.commit()

    assert Path(str(path) + "-wal").exists()
    with pytest.raises(SidecarSchemaError, match="recovery/checkpoint"):
        open_sidecar(path, readonly=True, immutable=True)
    ordinary_readonly = open_sidecar(path, readonly=True, immutable=False)
    ordinary_readonly.close()

    final_path = _finish_complete(connection)
    assert final_path == path.resolve()
    assert not tuple(sidecar.sqlite_sidecar_paths(path))

    immutable = open_sidecar(path, readonly=True, immutable=True)
    assert immutable.execute("PRAGMA query_only").fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError):
        immutable.execute("DELETE FROM e2_validations")
    immutable.close()

    result = validate_sidecar(path)
    assert result.passed
    assert result.structurally_valid
    assert result.run_status == "complete"
    assert result.quick_check == "ok"
    assert result.integrity_check == "ok"
    assert result.foreign_key_violations == 0


@pytest.mark.parametrize(
    ("config", "metric_mutation", "violation_name"),
    [
        (
            json.dumps({"network_requests": 0, "vision_requests": 0}),
            None,
            "evidence_only_config_mismatch",
        ),
        (
            json.dumps(
                {
                    "network_requests": 0,
                    "representative_selection": False,
                    "vision_requests": 1,
                }
            ),
            None,
            "evidence_only_config_mismatch",
        ),
        (
            EVIDENCE_ONLY_CONFIG,
            "DELETE FROM e2_metrics WHERE metric_name='llm_requests'",
            "evidence_only_metric_mismatch",
        ),
        (
            EVIDENCE_ONLY_CONFIG,
            "UPDATE e2_metrics SET value_integer=1 "
            "WHERE metric_name='vision_requests'",
            "evidence_only_metric_mismatch",
        ),
    ],
)
def test_evidence_only_policy_semantics_reject_config_and_metric_tampering(
    tmp_path: Path,
    config: str,
    metric_mutation: str | None,
    violation_name: str,
) -> None:
    path = tmp_path / "policy-tampered.db"
    connection = initialize_sidecar(path)
    _insert_run(connection, config_json=config)
    if metric_mutation:
        connection.execute(metric_mutation)
    _finish_complete(connection)

    result = validate_sidecar(path)
    violations = dict(result.semantic_violations)
    assert violations[violation_name] == 1
    assert not result.structurally_valid
    assert not result.passed
    assert not result.sqlite_sidecars
    assert dict(result.table_counts)["e2_runs"] == 1
    assert dict(result.table_counts)["e2_validations"] == 1


def test_terminal_status_contract_and_evidence_immutability(tmp_path: Path) -> None:
    complete_path = tmp_path / "complete.db"
    complete = initialize_sidecar(complete_path)
    _insert_run(complete)
    _insert_validation(complete, passed=False)
    complete.execute(
        "UPDATE e2_runs SET ordered_selection_manifest_sha256=?",
        (SHA_A,),
    )
    with pytest.raises(sqlite3.IntegrityError, match="passing error validations"):
        finalize_sidecar(complete, status="complete", close=False)
    complete.rollback()
    complete.close()

    failed_path = tmp_path / "failed.db"
    failed = initialize_sidecar(failed_path)
    _insert_run(failed)
    _insert_validation(failed, passed=False)
    finalize_sidecar(failed, status="failed_validation", error="fixture failure")
    result = validate_sidecar(failed_path)
    assert result.structurally_valid
    assert not result.passed
    assert result.run_status == "failed_validation"

    with pytest.raises(SidecarSchemaError, match="cannot be opened writable"):
        open_sidecar(failed_path, readonly=False)
    direct = sqlite3.connect(failed_path)
    direct.execute("PRAGMA foreign_keys=ON")
    with pytest.raises(sqlite3.IntegrityError, match="terminal E2 evidence"):
        direct.execute(
            """INSERT INTO e2_metrics(
                 run_id,phase,metric_name,value_integer,recorded_at
               ) VALUES('e2-test','late','late_metric',1,'late')"""
        )
    direct.close()


def test_lock_rejects_live_owner_and_releases_only_its_token(tmp_path: Path) -> None:
    path = tmp_path / "e2.db.lock"
    lock = acquire_build_lock(path)
    assert path.is_file()
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
    lock.release()  # idempotent


def test_lock_recovers_only_dead_same_host_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "e2.db.lock"
    stale = {
        "created_at": "2026-08-10T00:00:00Z",
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
    path = tmp_path / "e2.db.lock"
    path.write_text("not-json", encoding="ascii")
    with pytest.raises(BuildLockError, match="manual inspection"):
        acquire_build_lock(path)
    assert path.read_text(encoding="ascii") == "not-json"


def test_schema_identity_rejects_unrelated_sqlite_file(tmp_path: Path) -> None:
    path = tmp_path / "wrong.db"
    sqlite3.connect(path).close()
    with pytest.raises(SidecarSchemaError, match="application_id mismatch"):
        open_sidecar(path, readonly=True, immutable=True)
