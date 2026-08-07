"""Source-neutral SQLite sidecar contract for E1 image fingerprints.

The source database remains immutable.  A sidecar records the exact source
asset row selected for a run, every fetch attempt, and the fingerprint derived
from one successful response.  Source adapters own selection and networking;
this module only owns local persistence and validation.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path


SCHEMA_VERSION = 2
APPLICATION_ID = int.from_bytes(b"E1FP", "big")

TABLE_NAMES = (
    "fingerprint_runs",
    "source_assets",
    "fetch_attempts",
    "fingerprints",
    "validations",
)


SIDECAR_SCHEMA = f"""
PRAGMA foreign_keys=ON;
PRAGMA application_id={APPLICATION_ID};
PRAGMA user_version={SCHEMA_VERSION};

CREATE TABLE fingerprint_runs (
    run_id                      TEXT PRIMARY KEY
                                CHECK(length(trim(run_id)) > 0),
    source_name                 TEXT NOT NULL
                                CHECK(length(trim(source_name)) > 0),
    source_db_path              TEXT NOT NULL
                                CHECK(length(trim(source_db_path)) > 0),
    source_db_sha256_before     TEXT NOT NULL
                                CHECK(length(source_db_sha256_before)=64
                                  AND source_db_sha256_before=lower(source_db_sha256_before)
                                  AND source_db_sha256_before
                                      NOT GLOB '*[^0-9a-f]*'),
    source_db_sha256_after      TEXT
                                CHECK(source_db_sha256_after IS NULL OR
                                  (length(source_db_sha256_after)=64
                                  AND source_db_sha256_after=lower(source_db_sha256_after)
                                  AND source_db_sha256_after
                                      NOT GLOB '*[^0-9a-f]*')),
    fingerprint_contract_version TEXT NOT NULL
                                CHECK(length(trim(fingerprint_contract_version)) > 0),
    dependency_manifest_json    TEXT NOT NULL
                                CHECK(json_valid(dependency_manifest_json)),
    dependency_manifest_sha256  TEXT NOT NULL
                                CHECK(length(dependency_manifest_sha256)=64
                                  AND dependency_manifest_sha256=lower(dependency_manifest_sha256)
                                  AND dependency_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
    runner_version              TEXT NOT NULL
                                CHECK(length(trim(runner_version)) > 0),
    selection_manifest_sha256   TEXT NOT NULL
                                CHECK(length(selection_manifest_sha256)=64
                                  AND selection_manifest_sha256=lower(selection_manifest_sha256)
                                  AND selection_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
    status                      TEXT NOT NULL
                                CHECK(status IN
                                  ('running','complete','complete_with_failures',
                                   'failed_validation','failed')),
    started_at                  TEXT NOT NULL CHECK(length(trim(started_at)) > 0),
    completed_at                TEXT,
    error                       TEXT,
    CHECK((status='running' AND completed_at IS NULL
                           AND source_db_sha256_after IS NULL)
       OR (status<>'running' AND completed_at IS NOT NULL
                            AND source_db_sha256_after IS NOT NULL
                            AND source_db_sha256_after=source_db_sha256_before))
);

CREATE TRIGGER fingerprint_runs_provenance_immutable
BEFORE UPDATE OF
  run_id,source_name,source_db_path,source_db_sha256_before,
  fingerprint_contract_version,dependency_manifest_json,
  dependency_manifest_sha256,runner_version,selection_manifest_sha256,started_at
ON fingerprint_runs
BEGIN
    SELECT RAISE(ABORT, 'run source provenance is immutable');
END;

CREATE TRIGGER fingerprint_runs_terminal_immutable
BEFORE UPDATE ON fingerprint_runs
WHEN OLD.status<>'running'
BEGIN
    SELECT RAISE(ABORT, 'terminal fingerprint run is immutable');
END;

CREATE TRIGGER fingerprint_runs_source_after_once
BEFORE UPDATE OF source_db_sha256_after ON fingerprint_runs
WHEN OLD.source_db_sha256_after IS NOT NULL
  OR NEW.source_db_sha256_after<>OLD.source_db_sha256_before
BEGIN
    SELECT RAISE(ABORT, 'ending source SHA must be set once and match the starting SHA');
END;

CREATE TRIGGER fingerprint_runs_immutable_delete
BEFORE DELETE ON fingerprint_runs
BEGIN
    SELECT RAISE(ABORT, 'fingerprint runs are immutable');
END;

CREATE TABLE source_assets (
    run_id                      TEXT NOT NULL
                                REFERENCES fingerprint_runs(run_id)
                                ON UPDATE RESTRICT ON DELETE RESTRICT,
    source_asset_id             TEXT NOT NULL
                                CHECK(length(trim(source_asset_id)) > 0),
    selection_rank              INTEGER NOT NULL CHECK(selection_rank >= 1),
    canonical_url               TEXT NOT NULL CHECK(length(trim(canonical_url)) > 0),
    fetch_url                   TEXT NOT NULL CHECK(length(trim(fetch_url)) > 0),
    source_record_sha256        TEXT NOT NULL
                                CHECK(length(source_record_sha256)=64
                                  AND source_record_sha256=lower(source_record_sha256)
                                  AND source_record_sha256 NOT GLOB '*[^0-9a-f]*'),
    provenance_json             TEXT NOT NULL DEFAULT '{{}}'
                                CHECK(json_valid(provenance_json)),
    PRIMARY KEY(run_id, source_asset_id),
    UNIQUE(run_id, selection_rank)
);

CREATE TRIGGER source_assets_running_insert
BEFORE INSERT ON source_assets
WHEN coalesce((SELECT status FROM fingerprint_runs WHERE run_id=NEW.run_id), '')
     <> 'running'
BEGIN
    SELECT RAISE(ABORT, 'source assets can only be selected for a running run');
END;

CREATE TRIGGER source_assets_immutable_update
BEFORE UPDATE ON source_assets
BEGIN
    SELECT RAISE(ABORT, 'source asset provenance is immutable');
END;

CREATE TRIGGER source_assets_immutable_delete
BEFORE DELETE ON source_assets
BEGIN
    SELECT RAISE(ABORT, 'source asset provenance is immutable');
END;

CREATE TABLE fetch_attempts (
    run_id                      TEXT NOT NULL,
    source_asset_id             TEXT NOT NULL,
    attempt_no                  INTEGER NOT NULL CHECK(attempt_no >= 1),
    request_url                 TEXT NOT NULL CHECK(length(trim(request_url)) > 0),
    started_at                  TEXT NOT NULL CHECK(length(trim(started_at)) > 0),
    completed_at                TEXT NOT NULL CHECK(length(trim(completed_at)) > 0),
    elapsed_ms                  INTEGER NOT NULL CHECK(elapsed_ms >= 0),
    outcome                     TEXT NOT NULL CHECK(outcome IN ('success','failed')),
    http_status                 INTEGER CHECK(http_status IS NULL
                                             OR http_status BETWEEN 100 AND 599),
    response_mime               TEXT,
    response_bytes              INTEGER CHECK(response_bytes IS NULL OR response_bytes >= 0),
    final_url                   TEXT,
    raw_response_sha256         TEXT
                                CHECK(raw_response_sha256 IS NULL
                                  OR (length(raw_response_sha256)=64
                                  AND raw_response_sha256=lower(raw_response_sha256)
                                  AND raw_response_sha256 NOT GLOB '*[^0-9a-f]*')),
    error_kind                  TEXT,
    error_message               TEXT,
    PRIMARY KEY(run_id, source_asset_id, attempt_no),
    FOREIGN KEY(run_id, source_asset_id)
        REFERENCES source_assets(run_id, source_asset_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CHECK((outcome='success'
           AND response_bytes IS NOT NULL AND response_bytes > 0
           AND final_url IS NOT NULL
           AND length(trim(final_url)) > 0
           AND raw_response_sha256 IS NOT NULL
           AND error_kind IS NULL)
       OR (outcome='failed'
           AND error_kind IS NOT NULL
           AND length(trim(error_kind)) > 0))
);

CREATE TRIGGER fetch_attempts_running_insert
BEFORE INSERT ON fetch_attempts
WHEN coalesce((SELECT status FROM fingerprint_runs WHERE run_id=NEW.run_id), '')
     <> 'running'
BEGIN
    SELECT RAISE(ABORT, 'fetch attempts require a running run');
END;

CREATE TRIGGER fetch_attempts_immutable_update
BEFORE UPDATE ON fetch_attempts
BEGIN
    SELECT RAISE(ABORT, 'fetch attempts are append-only');
END;

CREATE TRIGGER fetch_attempts_immutable_delete
BEFORE DELETE ON fetch_attempts
BEGIN
    SELECT RAISE(ABORT, 'fetch attempts are append-only');
END;

CREATE TABLE fingerprints (
    run_id                      TEXT NOT NULL,
    source_asset_id             TEXT NOT NULL,
    status                      TEXT NOT NULL
                                CHECK(status IN ('pending','success','failed','skipped')),
    selected_attempt_no         INTEGER,
    raw_response_sha256         TEXT
                                CHECK(raw_response_sha256 IS NULL
                                  OR (length(raw_response_sha256)=64
                                  AND raw_response_sha256=lower(raw_response_sha256)
                                  AND raw_response_sha256 NOT GLOB '*[^0-9a-f]*')),
    normalized_pixel_sha256     TEXT
                                CHECK(normalized_pixel_sha256 IS NULL
                                  OR (length(normalized_pixel_sha256)=64
                                  AND normalized_pixel_sha256=lower(normalized_pixel_sha256)
                                  AND normalized_pixel_sha256 NOT GLOB '*[^0-9a-f]*')),
    phash_hex                   TEXT
                                CHECK(phash_hex IS NULL
                                  OR (length(phash_hex)=64
                                  AND phash_hex=lower(phash_hex)
                                  AND phash_hex NOT GLOB '*[^0-9a-f]*')),
    decoded_format              TEXT,
    original_width              INTEGER CHECK(original_width IS NULL OR original_width > 0),
    original_height             INTEGER CHECK(original_height IS NULL OR original_height > 0),
    normalized_width            INTEGER CHECK(normalized_width IS NULL OR normalized_width > 0),
    normalized_height           INTEGER CHECK(normalized_height IS NULL OR normalized_height > 0),
    metadata_json               TEXT NOT NULL DEFAULT '{{}}'
                                CHECK(json_valid(metadata_json)),
    completed_at                TEXT,
    error_kind                  TEXT,
    error_message               TEXT,
    PRIMARY KEY(run_id, source_asset_id),
    FOREIGN KEY(run_id, source_asset_id)
        REFERENCES source_assets(run_id, source_asset_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY(run_id, source_asset_id, selected_attempt_no)
        REFERENCES fetch_attempts(run_id, source_asset_id, attempt_no)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CHECK((selected_attempt_no IS NULL AND raw_response_sha256 IS NULL)
       OR (selected_attempt_no IS NOT NULL AND raw_response_sha256 IS NOT NULL)),
    CHECK(
      (status='pending'
       AND selected_attempt_no IS NULL
       AND normalized_pixel_sha256 IS NULL
       AND phash_hex IS NULL
       AND completed_at IS NULL
       AND error_kind IS NULL)
      OR
      (status='success'
       AND selected_attempt_no IS NOT NULL
       AND raw_response_sha256 IS NOT NULL
       AND normalized_pixel_sha256 IS NOT NULL
       AND phash_hex IS NOT NULL
       AND decoded_format IS NOT NULL AND length(trim(decoded_format)) > 0
       AND original_width IS NOT NULL AND original_width > 0
       AND original_height IS NOT NULL AND original_height > 0
       AND normalized_width IS NOT NULL AND normalized_width > 0
       AND normalized_height IS NOT NULL AND normalized_height > 0
       AND completed_at IS NOT NULL
       AND error_kind IS NULL)
      OR
      (status IN ('failed','skipped')
       AND normalized_pixel_sha256 IS NULL
       AND phash_hex IS NULL
       AND completed_at IS NOT NULL
       AND error_kind IS NOT NULL AND length(trim(error_kind)) > 0)
    )
);

CREATE TRIGGER fingerprints_running_insert
BEFORE INSERT ON fingerprints
WHEN coalesce((SELECT status FROM fingerprint_runs WHERE run_id=NEW.run_id), '')
     <> 'running'
BEGIN
    SELECT RAISE(ABORT, 'fingerprints require a running run');
END;

CREATE TRIGGER fingerprints_running_update
BEFORE UPDATE ON fingerprints
WHEN coalesce((SELECT status FROM fingerprint_runs WHERE run_id=OLD.run_id), '')
     <> 'running'
BEGIN
    SELECT RAISE(ABORT, 'terminal fingerprints are immutable');
END;

CREATE TRIGGER fingerprints_immutable_delete
BEFORE DELETE ON fingerprints
BEGIN
    SELECT RAISE(ABORT, 'fingerprints cannot be deleted');
END;

CREATE TABLE validations (
    run_id                      TEXT NOT NULL
                                REFERENCES fingerprint_runs(run_id)
                                ON UPDATE RESTRICT ON DELETE RESTRICT,
    validation_name            TEXT NOT NULL
                                CHECK(length(trim(validation_name)) > 0),
    severity                    TEXT NOT NULL
                                CHECK(severity IN ('info','warning','error')),
    passed                      INTEGER NOT NULL CHECK(passed IN (0,1)),
    expected                    TEXT,
    actual                      TEXT NOT NULL,
    detail                      TEXT,
    PRIMARY KEY(run_id, validation_name)
);

CREATE TRIGGER validations_running_insert
BEFORE INSERT ON validations
WHEN coalesce((SELECT status FROM fingerprint_runs WHERE run_id=NEW.run_id), '')
     <> 'running'
BEGIN
    SELECT RAISE(ABORT, 'validations require a running run');
END;

CREATE TRIGGER validations_running_update
BEFORE UPDATE ON validations
WHEN coalesce((SELECT status FROM fingerprint_runs WHERE run_id=OLD.run_id), '')
     <> 'running'
BEGIN
    SELECT RAISE(ABORT, 'terminal validations are immutable');
END;

CREATE TRIGGER validations_running_delete
BEFORE DELETE ON validations
WHEN coalesce((SELECT status FROM fingerprint_runs WHERE run_id=OLD.run_id), '')
     <> 'running'
BEGIN
    SELECT RAISE(ABORT, 'terminal validations are immutable');
END;

CREATE INDEX idx_source_assets_canonical_url
    ON source_assets(canonical_url);
CREATE INDEX idx_source_assets_record_sha
    ON source_assets(source_record_sha256);
CREATE INDEX idx_fetch_attempts_outcome
    ON fetch_attempts(run_id, outcome, error_kind);
CREATE INDEX idx_fetch_attempts_response_sha
    ON fetch_attempts(raw_response_sha256)
    WHERE raw_response_sha256 IS NOT NULL;
CREATE INDEX idx_fingerprints_status
    ON fingerprints(run_id, status, error_kind);
CREATE INDEX idx_fingerprints_pixel_sha
    ON fingerprints(normalized_pixel_sha256)
    WHERE normalized_pixel_sha256 IS NOT NULL;
CREATE INDEX idx_fingerprints_phash
    ON fingerprints(phash_hex)
    WHERE phash_hex IS NOT NULL;
CREATE INDEX idx_validations_failed
    ON validations(run_id, severity, validation_name)
    WHERE passed=0;
"""


class SidecarSchemaError(RuntimeError):
    """Raised when a file is not the expected E1 sidecar schema."""


@dataclass(frozen=True)
class SidecarValidation:
    quick_check: str
    integrity_check: str
    foreign_key_violations: int
    semantic_violations: tuple[tuple[str, int], ...]
    table_counts: tuple[tuple[str, int], ...]

    @property
    def passed(self) -> bool:
        return (
            self.quick_check == "ok"
            and self.integrity_check == "ok"
            and self.foreign_key_violations == 0
            and all(count == 0 for _, count in self.semantic_violations)
        )


def _configure(connection: sqlite3.Connection, *, readonly: bool) -> None:
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    if readonly:
        connection.execute("PRAGMA query_only=ON")


def _assert_schema(connection: sqlite3.Connection) -> None:
    application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
    schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table'"
        )
    }
    missing = sorted(set(TABLE_NAMES) - tables)
    if application_id != APPLICATION_ID:
        raise SidecarSchemaError(
            f"application_id mismatch: expected {APPLICATION_ID}, got {application_id}"
        )
    if schema_version != SCHEMA_VERSION:
        raise SidecarSchemaError(
            f"schema version mismatch: expected {SCHEMA_VERSION}, got {schema_version}"
        )
    if missing:
        raise SidecarSchemaError(f"missing sidecar tables: {', '.join(missing)}")


def initialize_sidecar(path: Path | str) -> sqlite3.Connection:
    """Create a new sidecar and return a writable connection.

    Existing files are never opened or replaced by this initializer.
    """

    target = Path(path)
    if target.exists():
        raise FileExistsError(f"sidecar already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target)
    try:
        _configure(connection, readonly=False)
        connection.executescript(SIDECAR_SCHEMA)
        connection.commit()
        _assert_schema(connection)
    except Exception:
        connection.close()
        raise
    return connection


def open_sidecar(
    path: Path | str,
    *,
    readonly: bool = True,
) -> sqlite3.Connection:
    """Open an initialized E1 sidecar and verify its schema identity."""

    target = Path(path).resolve()
    if not target.is_file():
        raise FileNotFoundError(f"sidecar does not exist: {target}")
    if readonly:
        uri = f"file:{target.as_posix()}?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True)
    else:
        connection = sqlite3.connect(target)
    try:
        _configure(connection, readonly=readonly)
        _assert_schema(connection)
    except Exception:
        connection.close()
        raise
    return connection


def validate_sidecar(path: Path | str) -> SidecarValidation:
    """Run physical, relational, and E1 contract checks without writing."""

    connection = open_sidecar(path, readonly=True)
    try:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        integrity_check = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_key_violations = len(
            connection.execute("PRAGMA foreign_key_check").fetchall()
        )
        semantic_queries = (
            (
                "successful_fingerprint_attempt_not_success",
                """
                SELECT count(*)
                FROM fingerprints f
                JOIN fetch_attempts a
                  ON a.run_id=f.run_id
                 AND a.source_asset_id=f.source_asset_id
                 AND a.attempt_no=f.selected_attempt_no
                WHERE f.status='success' AND a.outcome<>'success'
                """,
            ),
            (
                "successful_fingerprint_response_sha_mismatch",
                """
                SELECT count(*)
                FROM fingerprints f
                JOIN fetch_attempts a
                  ON a.run_id=f.run_id
                 AND a.source_asset_id=f.source_asset_id
                 AND a.attempt_no=f.selected_attempt_no
                WHERE f.status='success'
                  AND f.raw_response_sha256<>a.raw_response_sha256
                """,
            ),
            (
                "complete_run_asset_without_fingerprint",
                """
                SELECT count(*)
                FROM source_assets s
                JOIN fingerprint_runs r USING(run_id)
                LEFT JOIN fingerprints f
                  ON f.run_id=s.run_id AND f.source_asset_id=s.source_asset_id
                WHERE r.status IN ('complete','complete_with_failures')
                  AND f.source_asset_id IS NULL
                """,
            ),
            (
                "complete_run_pending_fingerprint",
                """
                SELECT count(*)
                FROM fingerprints f
                JOIN fingerprint_runs r USING(run_id)
                WHERE r.status IN ('complete','complete_with_failures')
                  AND f.status='pending'
                """,
            ),
            (
                "complete_run_with_failed_error_validation",
                """
                SELECT count(*)
                FROM fingerprint_runs r
                WHERE r.status IN ('complete','complete_with_failures')
                  AND EXISTS (
                    SELECT 1 FROM validations v
                    WHERE v.run_id=r.run_id
                      AND v.severity='error' AND v.passed=0
                  )
                """,
            ),
            (
                "complete_run_without_validation",
                """
                SELECT count(*)
                FROM fingerprint_runs r
                WHERE r.status IN ('complete','complete_with_failures')
                  AND NOT EXISTS (
                    SELECT 1 FROM validations v WHERE v.run_id=r.run_id
                  )
                """,
            ),
            (
                "failed_validation_without_failed_error_validation",
                """
                SELECT count(*)
                FROM fingerprint_runs r
                WHERE r.status='failed_validation'
                  AND NOT EXISTS (
                    SELECT 1 FROM validations v
                    WHERE v.run_id=r.run_id
                      AND v.severity='error' AND v.passed=0
                  )
                """,
            ),
            (
                "complete_run_status_mismatch",
                """
                SELECT count(*)
                FROM fingerprint_runs r
                WHERE (r.status='complete' AND EXISTS (
                         SELECT 1 FROM fingerprints f
                         WHERE f.run_id=r.run_id AND f.status<>'success'
                       ))
                   OR (r.status='complete_with_failures' AND (
                         NOT EXISTS (
                           SELECT 1 FROM fingerprints f
                           WHERE f.run_id=r.run_id AND f.status='success'
                         )
                         OR NOT EXISTS (
                           SELECT 1 FROM fingerprints f
                           WHERE f.run_id=r.run_id
                             AND f.status IN ('failed','skipped')
                         )
                       ))
                """,
            ),
        )
        semantic_violations = tuple(
            (name, int(connection.execute(query).fetchone()[0]))
            for name, query in semantic_queries
        )
        dependency_manifest_mismatches = sum(
            hashlib.sha256(str(row[0]).encode("utf-8")).hexdigest() != str(row[1])
            for row in connection.execute(
                """SELECT dependency_manifest_json,dependency_manifest_sha256
                   FROM fingerprint_runs"""
            )
        )
        semantic_violations += (
            ("dependency_manifest_sha_mismatch", dependency_manifest_mismatches),
        )
        table_counts = tuple(
            (
                table,
                int(connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]),
            )
            for table in TABLE_NAMES
        )
        return SidecarValidation(
            quick_check=quick_check,
            integrity_check=integrity_check,
            foreign_key_violations=foreign_key_violations,
            semantic_violations=semantic_violations,
            table_counts=table_counts,
        )
    finally:
        connection.close()
