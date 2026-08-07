import sqlite3
import hashlib
from pathlib import Path

import pytest

from canonical.image_fingerprint_sidecar import (
    APPLICATION_ID,
    SCHEMA_VERSION,
    SidecarSchemaError,
    initialize_sidecar,
    open_sidecar,
    validate_sidecar,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
DEPENDENCY_JSON = '{"pillow":"test"}'
DEPENDENCY_SHA = hashlib.sha256(DEPENDENCY_JSON.encode("utf-8")).hexdigest()


def _insert_run(connection: sqlite3.Connection, *, status: str = "running") -> None:
    connection.execute(
        """
        INSERT INTO fingerprint_runs(
          run_id,source_name,source_db_path,source_db_sha256_before,
          source_db_sha256_after,
          fingerprint_contract_version,dependency_manifest_json,
          dependency_manifest_sha256,runner_version,selection_manifest_sha256,
          status,started_at,completed_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "run-1",
            "divisare",
            "data/curated/divisare.db",
            SHA_A,
            None if status == "running" else SHA_A,
            "e1-v1",
            DEPENDENCY_JSON,
            DEPENDENCY_SHA,
            "test-runner-v1",
            SHA_B,
            status,
            "2026-08-07T00:00:00Z",
            None if status == "running" else "2026-08-07T00:01:00Z",
        ),
    )


def _insert_asset(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        INSERT INTO source_assets(
          run_id,source_asset_id,selection_rank,canonical_url,fetch_url,
          source_record_sha256,provenance_json
        ) VALUES(?,?,?,?,?,?,?)
        """,
        (
            "run-1",
            "asset-1",
            1,
            "https://example.test/image",
            "https://example.test/image?w=2048",
            SHA_C,
            '{"role":"cover"}',
        ),
    )


def _insert_success_attempt(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        INSERT INTO fetch_attempts(
          run_id,source_asset_id,attempt_no,request_url,started_at,completed_at,
          elapsed_ms,outcome,http_status,response_mime,response_bytes,final_url,
          raw_response_sha256
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "run-1",
            "asset-1",
            1,
            "https://example.test/image?w=2048",
            "2026-08-07T00:00:01Z",
            "2026-08-07T00:00:02Z",
            1000,
            "success",
            200,
            "image/jpeg",
            1234,
            "https://cdn.example.test/image?w=2048",
            SHA_A,
        ),
    )


def _insert_success_fingerprint(
    connection: sqlite3.Connection,
    *,
    raw_sha: str = SHA_A,
    phash: str = SHA_C,
) -> None:
    connection.execute(
        """
        INSERT INTO fingerprints(
          run_id,source_asset_id,status,selected_attempt_no,raw_response_sha256,
          normalized_pixel_sha256,phash_hex,decoded_format,
          original_width,original_height,normalized_width,normalized_height,
          metadata_json,completed_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "run-1",
            "asset-1",
            "success",
            1,
            raw_sha,
            SHA_B,
            phash,
            "JPEG",
            1600,
            1200,
            512,
            384,
            '{"alpha":false}',
            "2026-08-07T00:00:03Z",
        ),
    )


def test_initialize_open_and_validate_complete_sidecar(tmp_path: Path) -> None:
    path = tmp_path / "fingerprints.db"
    connection = initialize_sidecar(path)
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert connection.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
    assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION

    _insert_run(connection)
    _insert_asset(connection)
    _insert_success_attempt(connection)
    _insert_success_fingerprint(connection)
    connection.execute(
        """
        INSERT INTO validations(
          run_id,validation_name,severity,passed,expected,actual
        ) VALUES(?,?,?,?,?,?)
        """,
        ("run-1", "fingerprint_accounting", "error", 1, "1", "1"),
    )
    connection.execute(
        """UPDATE fingerprint_runs
           SET status='complete',completed_at=?,source_db_sha256_after=?
           WHERE run_id=?""",
        ("2026-08-07T00:01:00Z", SHA_A, "run-1"),
    )
    with pytest.raises(sqlite3.IntegrityError, match="only be selected for a running run"):
        connection.execute(
            """
            INSERT INTO source_assets(
              run_id,source_asset_id,selection_rank,canonical_url,fetch_url,
              source_record_sha256
            ) VALUES('run-1','late-asset',2,'https://x','https://x',?)
            """,
            (SHA_A,),
        )
    connection.commit()
    connection.close()

    readonly = open_sidecar(path)
    assert readonly.execute("PRAGMA query_only").fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError):
        readonly.execute("DELETE FROM validations")
    readonly.close()

    result = validate_sidecar(path)
    assert result.passed
    assert dict(result.table_counts) == {
        "fingerprint_runs": 1,
        "source_assets": 1,
        "fetch_attempts": 1,
        "fingerprints": 1,
        "validations": 1,
    }
    assert dict(result.semantic_violations) == {
        "successful_fingerprint_attempt_not_success": 0,
        "successful_fingerprint_response_sha_mismatch": 0,
        "complete_run_asset_without_fingerprint": 0,
        "complete_run_pending_fingerprint": 0,
        "complete_run_with_failed_error_validation": 0,
            "complete_run_without_validation": 0,
            "failed_validation_without_failed_error_validation": 0,
            "complete_run_status_mismatch": 0,
            "dependency_manifest_sha_mismatch": 0,
    }


def test_source_asset_provenance_is_immutable(tmp_path: Path) -> None:
    path = tmp_path / "fingerprints.db"
    connection = initialize_sidecar(path)
    _insert_run(connection)
    _insert_asset(connection)

    with pytest.raises(sqlite3.IntegrityError, match="provenance is immutable"):
        connection.execute(
            "UPDATE source_assets SET fetch_url=? WHERE run_id=? AND source_asset_id=?",
            ("https://changed.test", "run-1", "asset-1"),
        )
    with pytest.raises(sqlite3.IntegrityError, match="source provenance is immutable"):
        connection.execute(
            "UPDATE fingerprint_runs SET source_db_sha256_before=? WHERE run_id=?",
            (SHA_B, "run-1"),
        )
    with pytest.raises(sqlite3.IntegrityError, match="provenance is immutable"):
        connection.execute(
            "DELETE FROM source_assets WHERE run_id=? AND source_asset_id=?",
            ("run-1", "asset-1"),
        )
    connection.close()


@pytest.mark.parametrize("bad_hash", ["f" * 63, "g" * 64, "A" * 64])
def test_success_hashes_require_exactly_64_hex_characters(
    tmp_path: Path,
    bad_hash: str,
) -> None:
    path = tmp_path / "fingerprints.db"
    connection = initialize_sidecar(path)
    _insert_run(connection)
    _insert_asset(connection)

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO fetch_attempts(
              run_id,source_asset_id,attempt_no,request_url,started_at,completed_at,
              elapsed_ms,outcome,response_bytes,final_url,raw_response_sha256
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "run-1",
                "asset-1",
                1,
                "https://example.test/image",
                "start",
                "end",
                1,
                "success",
                1,
                "https://example.test/image",
                bad_hash,
            ),
        )

    _insert_success_attempt(connection)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_success_fingerprint(connection, phash=bad_hash)
    connection.close()


def test_foreign_keys_reject_unknown_run_and_asset(tmp_path: Path) -> None:
    path = tmp_path / "fingerprints.db"
    connection = initialize_sidecar(path)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO source_assets(
              run_id,source_asset_id,selection_rank,canonical_url,fetch_url,
              source_record_sha256
            ) VALUES('missing','asset-1',1,'https://x','https://x',?)
            """,
            (SHA_A,),
        )
    connection.close()


def test_run_must_finish_with_the_same_source_sha(tmp_path: Path) -> None:
    path = tmp_path / "fingerprints.db"
    connection = initialize_sidecar(path)
    _insert_run(connection)

    with pytest.raises(sqlite3.IntegrityError, match="ending source SHA"):
        connection.execute(
            "UPDATE fingerprint_runs SET source_db_sha256_after=? WHERE run_id=?",
            (SHA_B, "run-1"),
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE fingerprint_runs SET status='failed',completed_at='done' WHERE run_id=?",
            ("run-1",),
        )
    connection.close()


def test_terminal_run_tables_are_immutable(tmp_path: Path) -> None:
    path = tmp_path / "fingerprints.db"
    connection = initialize_sidecar(path)
    _insert_run(connection)
    _insert_asset(connection)
    _insert_success_attempt(connection)
    _insert_success_fingerprint(connection)
    connection.execute(
        """UPDATE fingerprint_runs
           SET status='complete',completed_at='done',source_db_sha256_after=?""",
        (SHA_A,),
    )

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute(
            """UPDATE fetch_attempts SET elapsed_ms=2
               WHERE run_id='run-1' AND source_asset_id='asset-1'"""
        )
    with pytest.raises(sqlite3.IntegrityError, match="terminal fingerprints"):
        connection.execute(
            """UPDATE fingerprints SET decoded_format='PNG'
               WHERE run_id='run-1' AND source_asset_id='asset-1'"""
        )
    with pytest.raises(sqlite3.IntegrityError, match="terminal fingerprint run"):
        connection.execute(
            "UPDATE fingerprint_runs SET error='changed' WHERE run_id='run-1'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="fetch attempts require"):
        connection.execute(
            """INSERT INTO fetch_attempts(
              run_id,source_asset_id,attempt_no,request_url,started_at,completed_at,
              elapsed_ms,outcome,error_kind
            ) VALUES('run-1','asset-1',2,'https://x','s','e',1,'failed','late')"""
        )
    connection.close()


@pytest.mark.parametrize("column", ["response_bytes", "original_width"])
def test_success_rows_reject_null_required_numbers(
    tmp_path: Path, column: str
) -> None:
    path = tmp_path / "fingerprints.db"
    connection = initialize_sidecar(path)
    _insert_run(connection)
    _insert_asset(connection)
    if column == "response_bytes":
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO fetch_attempts(
                  run_id,source_asset_id,attempt_no,request_url,started_at,completed_at,
                  elapsed_ms,outcome,final_url,raw_response_sha256
                ) VALUES('run-1','asset-1',1,'https://x','s','e',1,'success',
                         'https://x',?)""",
                (SHA_A,),
            )
    else:
        _insert_success_attempt(connection)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO fingerprints(
                  run_id,source_asset_id,status,selected_attempt_no,
                  raw_response_sha256,normalized_pixel_sha256,phash_hex,
                  decoded_format,original_height,normalized_width,
                  normalized_height,completed_at
                ) VALUES('run-1','asset-1','success',1,?,?,?,'JPEG',10,512,384,'e')""",
                (SHA_A, SHA_B, SHA_C),
            )
    connection.close()


def test_validation_detects_cross_table_semantic_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "fingerprints.db"
    connection = initialize_sidecar(path)
    _insert_run(connection)
    _insert_asset(connection)
    _insert_success_attempt(connection)
    _insert_success_fingerprint(connection, raw_sha=SHA_B)
    connection.execute(
        """UPDATE fingerprint_runs
           SET status='complete',completed_at='done',source_db_sha256_after=?""",
        (SHA_A,),
    )
    connection.commit()
    connection.close()

    result = validate_sidecar(path)
    assert not result.passed
    assert dict(result.semantic_violations)[
        "successful_fingerprint_response_sha_mismatch"
    ] == 1


def test_validation_rejects_complete_run_with_failed_error_check(
    tmp_path: Path,
) -> None:
    path = tmp_path / "fingerprints.db"
    connection = initialize_sidecar(path)
    _insert_run(connection)
    _insert_asset(connection)
    _insert_success_attempt(connection)
    _insert_success_fingerprint(connection)
    connection.execute(
        """INSERT INTO validations(
          run_id,validation_name,severity,passed,expected,actual
        ) VALUES('run-1','bad_check','error',0,'0','1')"""
    )
    connection.execute(
        """UPDATE fingerprint_runs
           SET status='complete',completed_at='done',source_db_sha256_after=?""",
        (SHA_A,),
    )
    connection.commit()
    connection.close()

    result = validate_sidecar(path)
    assert not result.passed
    assert dict(result.semantic_violations)[
        "complete_run_with_failed_error_validation"
    ] == 1


def test_initializer_refuses_clobber_and_opener_rejects_wrong_schema(
    tmp_path: Path,
) -> None:
    path = tmp_path / "fingerprints.db"
    connection = initialize_sidecar(path)
    connection.close()
    with pytest.raises(FileExistsError):
        initialize_sidecar(path)

    wrong = tmp_path / "wrong.db"
    sqlite3.connect(wrong).close()
    with pytest.raises(SidecarSchemaError, match="application_id mismatch"):
        open_sidecar(wrong)
