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


SCHEMA_VERSION = 3
APPLICATION_ID = int.from_bytes(b"E1FP", "big")

REQUIRED_VALIDATIONS = (
    "single_run",
    "source_sha_unchanged",
    "source_inventory_accounting",
    "exclusion_ledger_accounting",
    "source_record_sha256",
    "ordered_selection_manifest",
    "fingerprint_accounting",
    "successful_attempt_linkage",
    "quick_check",
    "integrity_check",
    "foreign_key_check",
)

TABLE_NAMES = (
    "fingerprint_runs",
    "source_assets",
    "source_asset_exclusions",
    "fetch_attempts",
    "fingerprints",
    "validations",
)

TRIGGER_NAMES = (
    "fingerprint_runs_single_run",
    "fingerprint_runs_provenance_immutable",
    "fingerprint_runs_status_transition",
    "fingerprint_runs_terminal_immutable",
    "fingerprint_runs_source_after_once",
    "fingerprint_runs_immutable_delete",
    "source_assets_initializing_insert",
    "source_assets_immutable_update",
    "source_assets_immutable_delete",
    "source_assets_not_excluded",
    "source_asset_exclusions_initializing_insert",
    "source_asset_exclusions_immutable_update",
    "source_asset_exclusions_not_selected",
    "source_asset_exclusions_immutable_delete",
    "fetch_attempts_running_insert",
    "fetch_attempts_retry_budget_insert",
    "fetch_attempts_immutable_update",
    "fetch_attempts_immutable_delete",
    "fingerprints_initializing_insert",
    "fingerprints_running_update",
    "fingerprints_immutable_delete",
    "validations_running_insert",
    "validations_running_update",
    "validations_running_delete",
)

INDEX_NAMES = (
    "idx_source_assets_canonical_url",
    "idx_source_assets_run_rank",
    "idx_source_assets_record_sha",
    "idx_source_asset_exclusions_reason",
    "idx_source_asset_exclusions_record_sha",
    "idx_fetch_attempts_outcome",
    "idx_fetch_attempts_response_sha",
    "idx_fingerprints_status",
    "idx_fingerprints_pixel_sha",
    "idx_fingerprints_phash",
    "idx_validations_failed",
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
    retry_policy_version        TEXT NOT NULL
                                CHECK(length(trim(retry_policy_version)) > 0),
    max_attempts                INTEGER NOT NULL CHECK(max_attempts >= 1),
    selection_manifest_sha256   TEXT NOT NULL
                                CHECK(length(selection_manifest_sha256)=64
                                  AND selection_manifest_sha256=lower(selection_manifest_sha256)
                                  AND selection_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
    selection_mode              TEXT NOT NULL
                                CHECK(selection_mode IN ('sample','full')),
    selection_count             INTEGER NOT NULL CHECK(selection_count >= 0),
    sample_seed                 TEXT,
    selection_version           TEXT NOT NULL
                                CHECK(length(trim(selection_version)) > 0),
    source_inventory_manifest_sha256 TEXT NOT NULL
                                CHECK(length(source_inventory_manifest_sha256)=64
                                  AND source_inventory_manifest_sha256=
                                      lower(source_inventory_manifest_sha256)
                                  AND source_inventory_manifest_sha256
                                      NOT GLOB '*[^0-9a-f]*'),
    exclusion_manifest_sha256   TEXT NOT NULL
                                CHECK(length(exclusion_manifest_sha256)=64
                                  AND exclusion_manifest_sha256=
                                      lower(exclusion_manifest_sha256)
                                  AND exclusion_manifest_sha256
                                      NOT GLOB '*[^0-9a-f]*'),
    source_total_count          INTEGER NOT NULL CHECK(source_total_count >= 0),
    eligible_count              INTEGER NOT NULL CHECK(eligible_count >= 0),
    excluded_count              INTEGER NOT NULL CHECK(excluded_count >= 0),
    initialized_inventory_count INTEGER NOT NULL DEFAULT 0
                                CHECK(initialized_inventory_count >= 0),
    initialized_selected_count  INTEGER NOT NULL DEFAULT 0
                                CHECK(initialized_selected_count >= 0),
    initialized_excluded_count  INTEGER NOT NULL DEFAULT 0
                                CHECK(initialized_excluded_count >= 0),
    initialization_updated_at   TEXT,
    initialization_completed_at TEXT,
    status                      TEXT NOT NULL
                                CHECK(status IN
                                  ('initializing','running','complete','complete_with_failures',
                                   'failed_validation','failed')),
    started_at                  TEXT NOT NULL CHECK(length(trim(started_at)) > 0),
    completed_at                TEXT,
    error                       TEXT,
    CHECK(source_total_count=eligible_count+excluded_count),
    CHECK(selection_count<=eligible_count),
    CHECK((selection_mode='sample'
           AND selection_count>=1
           AND sample_seed IS NOT NULL
           AND length(trim(sample_seed))>0)
       OR (selection_mode='full'
           AND sample_seed IS NULL
           AND selection_count=eligible_count)),
    CHECK(initialized_inventory_count<=source_total_count),
    CHECK(initialized_selected_count<=selection_count),
    CHECK(initialized_excluded_count<=excluded_count),
    CHECK(initialized_inventory_count>=
          initialized_selected_count+initialized_excluded_count),
    CHECK((status='initializing'
           AND initialization_completed_at IS NULL)
       OR (status<>'initializing'
           AND initialization_completed_at IS NOT NULL
           AND initialized_inventory_count=source_total_count
           AND initialized_selected_count=selection_count
           AND initialized_excluded_count=excluded_count)),
    CHECK((status IN ('initializing','running')
           AND completed_at IS NULL
           AND source_db_sha256_after IS NULL)
       OR (status NOT IN ('initializing','running')
           AND completed_at IS NOT NULL
           AND source_db_sha256_after IS NOT NULL
           AND source_db_sha256_after=source_db_sha256_before))
);

CREATE TRIGGER fingerprint_runs_single_run
BEFORE INSERT ON fingerprint_runs
WHEN EXISTS (SELECT 1 FROM fingerprint_runs)
BEGIN
    SELECT RAISE(ABORT, 'an E1 sidecar contains exactly one run');
END;

CREATE TRIGGER fingerprint_runs_provenance_immutable
BEFORE UPDATE OF
  run_id,source_name,source_db_path,source_db_sha256_before,
  fingerprint_contract_version,dependency_manifest_json,
  dependency_manifest_sha256,runner_version,retry_policy_version,max_attempts,
  selection_manifest_sha256,
  selection_mode,selection_count,sample_seed,selection_version,
  source_inventory_manifest_sha256,exclusion_manifest_sha256,
  source_total_count,eligible_count,excluded_count,started_at
ON fingerprint_runs
BEGIN
    SELECT RAISE(ABORT, 'run source provenance is immutable');
END;

CREATE TRIGGER fingerprint_runs_status_transition
BEFORE UPDATE OF status ON fingerprint_runs
WHEN NOT (
       (OLD.status='initializing' AND NEW.status='running')
    OR (OLD.status='running' AND NEW.status IN
        ('complete','complete_with_failures','failed_validation','failed'))
)
BEGIN
    SELECT RAISE(ABORT, 'invalid fingerprint run status transition');
END;

CREATE TRIGGER fingerprint_runs_terminal_immutable
BEFORE UPDATE ON fingerprint_runs
WHEN OLD.status NOT IN ('initializing','running')
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
    PRIMARY KEY(run_id, source_asset_id)
);

CREATE TRIGGER source_assets_initializing_insert
BEFORE INSERT ON source_assets
WHEN coalesce((SELECT status FROM fingerprint_runs WHERE run_id=NEW.run_id), '')
     <> 'initializing'
BEGIN
    SELECT RAISE(ABORT, 'source assets can only be selected while initializing');
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

CREATE TRIGGER source_assets_not_excluded
BEFORE INSERT ON source_assets
WHEN EXISTS (
    SELECT 1 FROM source_asset_exclusions e
    WHERE e.run_id=NEW.run_id AND e.source_asset_id=NEW.source_asset_id
)
BEGIN
    SELECT RAISE(ABORT, 'source asset is already recorded as excluded');
END;

CREATE TABLE source_asset_exclusions (
    run_id                      TEXT NOT NULL
                                REFERENCES fingerprint_runs(run_id)
                                ON UPDATE RESTRICT ON DELETE RESTRICT,
    source_asset_id             TEXT NOT NULL
                                CHECK(length(trim(source_asset_id)) > 0),
    source_asset_key            TEXT NOT NULL
                                CHECK(length(trim(source_asset_key)) > 0),
    inventory_rank              INTEGER NOT NULL CHECK(inventory_rank >= 1),
    reason_code                 TEXT NOT NULL
                                CHECK(length(trim(reason_code)) > 0),
    source_record_sha256        TEXT NOT NULL
                                CHECK(length(source_record_sha256)=64
                                  AND source_record_sha256=lower(source_record_sha256)
                                  AND source_record_sha256 NOT GLOB '*[^0-9a-f]*'),
    provenance_json             TEXT NOT NULL CHECK(json_valid(provenance_json)),
    detail_json                 TEXT NOT NULL CHECK(json_valid(detail_json)),
    PRIMARY KEY(run_id, source_asset_id),
    UNIQUE(run_id, inventory_rank)
);

CREATE TRIGGER source_asset_exclusions_initializing_insert
BEFORE INSERT ON source_asset_exclusions
WHEN coalesce((SELECT status FROM fingerprint_runs WHERE run_id=NEW.run_id), '')
     <> 'initializing'
BEGIN
    SELECT RAISE(ABORT, 'source exclusions can only be recorded while initializing');
END;

CREATE TRIGGER source_asset_exclusions_immutable_update
BEFORE UPDATE ON source_asset_exclusions
BEGIN
    SELECT RAISE(ABORT, 'source exclusion provenance is immutable');
END;

CREATE TRIGGER source_asset_exclusions_not_selected
BEFORE INSERT ON source_asset_exclusions
WHEN EXISTS (
    SELECT 1 FROM source_assets s
    WHERE s.run_id=NEW.run_id AND s.source_asset_id=NEW.source_asset_id
)
BEGIN
    SELECT RAISE(ABORT, 'excluded source asset is already selected');
END;

CREATE TRIGGER source_asset_exclusions_immutable_delete
BEFORE DELETE ON source_asset_exclusions
BEGIN
    SELECT RAISE(ABORT, 'source exclusion provenance is immutable');
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
    retry_after_seconds         REAL
                                CHECK(retry_after_seconds IS NULL
                                  OR retry_after_seconds >= 0),
    scheduled_delay_seconds     REAL
                                CHECK(scheduled_delay_seconds IS NULL
                                  OR scheduled_delay_seconds >= 0),
    worker_no                   INTEGER
                                CHECK(worker_no IS NULL OR worker_no >= 1),
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

CREATE TRIGGER fingerprints_initializing_insert
BEFORE INSERT ON fingerprints
WHEN NEW.status<>'pending'
  OR coalesce((SELECT status FROM fingerprint_runs WHERE run_id=NEW.run_id), '')
     <> 'initializing'
BEGIN
    SELECT RAISE(ABORT, 'pending fingerprints can only be created while initializing');
END;

CREATE TRIGGER fetch_attempts_retry_budget_insert
BEFORE INSERT ON fetch_attempts
WHEN NEW.attempt_no > coalesce((
    SELECT max_attempts FROM fingerprint_runs WHERE run_id=NEW.run_id
), 0)
BEGIN
    SELECT RAISE(ABORT, 'fetch attempt exceeds the immutable retry budget');
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
     NOT IN ('initializing','running')
BEGIN
    SELECT RAISE(ABORT, 'validations require a non-terminal run');
END;

CREATE TRIGGER validations_running_update
BEFORE UPDATE ON validations
WHEN coalesce((SELECT status FROM fingerprint_runs WHERE run_id=OLD.run_id), '')
     NOT IN ('initializing','running')
BEGIN
    SELECT RAISE(ABORT, 'terminal validations are immutable');
END;

CREATE TRIGGER validations_running_delete
BEFORE DELETE ON validations
WHEN coalesce((SELECT status FROM fingerprint_runs WHERE run_id=OLD.run_id), '')
     NOT IN ('initializing','running')
BEGIN
    SELECT RAISE(ABORT, 'terminal validations are immutable');
END;

CREATE INDEX idx_source_assets_canonical_url
    ON source_assets(canonical_url);
CREATE UNIQUE INDEX idx_source_assets_run_rank
    ON source_assets(run_id,selection_rank);
CREATE INDEX idx_source_assets_record_sha
    ON source_assets(source_record_sha256);
CREATE INDEX idx_source_asset_exclusions_reason
    ON source_asset_exclusions(run_id, reason_code, source_asset_id);
CREATE INDEX idx_source_asset_exclusions_record_sha
    ON source_asset_exclusions(source_record_sha256);
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
    triggers = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='trigger'"
        )
    }
    indexes = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='index'"
        )
    }
    missing = sorted(set(TABLE_NAMES) - tables)
    missing_triggers = sorted(set(TRIGGER_NAMES) - triggers)
    missing_indexes = sorted(set(INDEX_NAMES) - indexes)
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
    if missing_triggers:
        raise SidecarSchemaError(
            f"missing sidecar triggers: {', '.join(missing_triggers)}"
        )
    if missing_indexes:
        raise SidecarSchemaError(
            f"missing sidecar indexes: {', '.join(missing_indexes)}"
        )


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


def recover_sidecar(path: Path | str) -> None:
    """Recover a possibly interrupted sidecar before immutable inspection.

    SQLite performs hot-journal recovery only through a normal writable open;
    ``immutable=1`` must not be the first open after an abnormal termination.
    The brief immediate transaction also proves that no live SQLite writer owns
    the database.  No application rows are changed.
    """

    target = Path(path).resolve()
    if not target.is_file():
        raise FileNotFoundError(f"sidecar does not exist: {target}")
    connection = sqlite3.connect(target)
    try:
        _configure(connection, readonly=False)
        _assert_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("SELECT count(*) FROM fingerprint_runs").fetchone()
        connection.commit()
        journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
        if journal_mode.casefold() == "wal":
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def validate_sidecar(path: Path | str) -> SidecarValidation:
    """Run physical, relational, and E1 contract checks without writing."""

    connection = open_sidecar(path, readonly=True)
    try:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        integrity_check = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_key_violations = sum(
            1 for _ in connection.execute("PRAGMA foreign_key_check")
        )
        semantic_queries = (
            (
                "single_run_count_mismatch",
                "SELECT abs(count(*)-1) FROM fingerprint_runs",
            ),
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
                "initialized_run_selected_count_mismatch",
                """
                SELECT count(*)
                FROM fingerprint_runs r
                WHERE r.status<>'initializing'
                  AND r.selection_count<>(
                    SELECT count(*) FROM source_assets s WHERE s.run_id=r.run_id
                  )
                """,
            ),
            (
                "initialized_run_exclusion_count_mismatch",
                """
                SELECT count(*)
                FROM fingerprint_runs r
                WHERE r.status<>'initializing'
                  AND r.excluded_count<>(
                    SELECT count(*) FROM source_asset_exclusions e
                    WHERE e.run_id=r.run_id
                  )
                """,
            ),
            (
                "initialization_progress_row_count_mismatch",
                """
                SELECT count(*)
                FROM fingerprint_runs r
                WHERE r.initialized_selected_count<>(
                        SELECT count(*) FROM source_assets s WHERE s.run_id=r.run_id
                      )
                   OR r.initialized_excluded_count<>(
                        SELECT count(*) FROM source_asset_exclusions e
                        WHERE e.run_id=r.run_id
                      )
                """,
            ),
            (
                "selected_and_excluded_identity_overlap",
                """
                SELECT count(*)
                FROM source_assets s
                JOIN source_asset_exclusions e
                  ON e.run_id=s.run_id
                 AND e.source_asset_id=s.source_asset_id
                """,
            ),
            (
                "fetch_attempt_number_gap",
                """
                SELECT count(*) FROM (
                  SELECT run_id,source_asset_id
                  FROM fetch_attempts
                  GROUP BY run_id,source_asset_id
                  HAVING min(attempt_no)<>1 OR max(attempt_no)<>count(*)
                )
                """,
            ),
            (
                "fetch_attempt_exceeds_retry_budget",
                """
                SELECT count(*)
                FROM fetch_attempts a
                JOIN fingerprint_runs r USING(run_id)
                WHERE a.attempt_no>r.max_attempts
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
                           WHERE f.run_id=r.run_id
                             AND f.status IN ('failed','skipped')
                         )
                         OR (
                           NOT EXISTS (
                             SELECT 1 FROM fingerprints f
                             WHERE f.run_id=r.run_id AND f.status='success'
                           )
                           AND coalesce(json_extract(
                                 r.dependency_manifest_json,
                                 '$._run_lineage.kind'
                               ), '')<>'failure_recovery_v1'
                         )
                       ))
                """,
            ),
        )
        semantic_violations = tuple(
            (name, int(connection.execute(query).fetchone()[0]))
            for name, query in semantic_queries
        )
        required = set(REQUIRED_VALIDATIONS)
        required_validation_violations = 0
        failed_validation_without_required_error = 0
        for run in connection.execute("SELECT run_id,status FROM fingerprint_runs"):
            validation_rows = {
                str(row[0]): (str(row[1]), int(row[2]))
                for row in connection.execute(
                    "SELECT validation_name,severity,passed FROM validations WHERE run_id=?",
                    (run["run_id"],),
                )
            }
            if run["status"] in {"complete", "complete_with_failures"}:
                required_validation_violations += sum(
                    name not in validation_rows
                    or validation_rows[name] != ("error", 1)
                    for name in required
                )
            if run["status"] == "failed_validation":
                failed_validation_without_required_error += int(
                    not any(
                        name in required and severity == "error" and passed == 0
                        for name, (severity, passed) in validation_rows.items()
                    )
                )
        semantic_violations += (
            (
                "terminal_required_validation_mismatch",
                required_validation_violations,
            ),
            (
                "failed_validation_without_failed_required_validation",
                failed_validation_without_required_error,
            ),
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
        exclusion_source_record_mismatches = sum(
            hashlib.sha256(str(row[0]).encode("utf-8")).hexdigest() != str(row[1])
            for row in connection.execute(
                """SELECT provenance_json,source_record_sha256
                   FROM source_asset_exclusions"""
            )
        )
        semantic_violations += (
            (
                "exclusion_source_record_sha_mismatch",
                exclusion_source_record_mismatches,
            ),
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
