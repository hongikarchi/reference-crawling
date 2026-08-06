"""Build an immutable Architizer curated-v2 reconciliation plan.

The result is deliberately an intermediate, source-specific SQLite artifact,
not the consumer-ready curated v2.0 database.  It freezes field candidates,
decisions, conflicts, input lineage, and the complete curated-v1.3 SQLite
contract.  ``v_reconciled_projects`` and ``v_reconciled_firms`` are the stable
handoff to the later final materializer.

No network service is used.  The legacy raw DB, curated v1.3, and recrawl
sidecar are opened read-only/immutable and their hashes are checked again
before publication. Full eligibility additionally requires the fixed v1.3/raw
identity, a trusted run/universe manifest, and the recrawler's exclusive lock.
The DB and report are valid only when the hash-bearing READY marker is present.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import secrets
import shutil
import sqlite3
import sys
import tempfile
import urllib.parse
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from canonical.architizer_curated import SCHEMA_VERSION as BASELINE_SCHEMA_VERSION
from canonical.architizer_reconciliation import (
    ARCHITIZER_ENTITY_COLLECTIONS,
    FIELD_SPECS,
    FINAL_TARGET_SCHEMA_VERSION,
    RECONCILIATION_POLICY_VERSION,
    RECONCILIATION_READY_VERSION,
    RECONCILIATION_SCHEMA_VERSION,
    RECONCILIATION_TOOL_VERSION,
    Candidate,
    FieldSpec,
    canonical_entity_url,
    canonical_json,
    clean_text,
    entity_slug_from_url,
    is_valid_entity_slug,
    is_empty,
    normalize_value,
    reconcile_field,
    stable_id,
    validate_last_good_identity,
    values_equal,
)
from crawl.architizer.recrawl_v2 import (
    METADATA_VERSION as CURRENT_METADATA_VERSION,
    PARSER_VERSION as CURRENT_PARSER_VERSION,
    SNAPSHOT_REPARSE_GATE_POLICY_VERSION,
    STATE_SCHEMA_VERSION,
    TARGET_NETWORK_STATE_FIELDS,
)


DEFAULT_RAW_DB = REPO_ROOT / "data" / "crawl" / "architizer.db"
DEFAULT_BASELINE_DB = (
    REPO_ROOT / "data" / "curated" / "architizer_curated_v1_3.db"
)
DEFAULT_SIDECAR_DB = (
    REPO_ROOT / "data" / "enrichment" / "architizer_source_recrawl_v2.db"
)
DEFAULT_OUTPUT_DB = (
    REPO_ROOT / "data" / "enrichment" / "architizer_curated_v2_reconciliation.db"
)
DEFAULT_REPORT = (
    REPO_ROOT / "data" / "reports" / "architizer_curated_v2_reconciliation.md"
)

# The only production baseline accepted by this v2 reconciliation policy is
# the immutable curated-v1.3 release backed by the fixed 2026-04-28 raw DB.
# Smoke fixtures can exercise the generic policy, but a full/eligible plan is
# bound to these identities as well as to an explicit trusted sidecar manifest.
FIXED_RAW_SHA256 = "35FAA8AD2B4681033E1F7F74148499B29009777977204C7A65923D8FABB5C985"
FIXED_RAW_SIZE_BYTES = 90_918_912
FIXED_BASELINE_SHA256 = "5AEA8A85FA54B139F31585069C50A19AFA7D87B500A44AAAD32CCDC29937B089"
FIXED_BASELINE_SIZE_BYTES = 687_579_136
TRUSTED_MANIFEST_VERSION = "architizer-reconciliation-input-manifest-v1"

# Object inventory of the immutable v1.3 release.  Exact SQL/column metadata is
# captured per input below; this inventory prevents a partial or look-alike DB
# from being accepted merely because it has source_projects/source_firms.
BASELINE_REQUIRED_TABLES = frozenset(
    {
        "build_runs",
        "source_snapshots",
        "source_queue_summary",
        "source_firms",
        "firm_office_occurrences",
        "firm_social_links",
        "source_projects",
        "project_firms",
        "project_text_versions",
        "source_categories",
        "project_category_occurrences",
        "category_mappings",
        "attribute_claims",
        "source_awards",
        "award_entity_links",
        "image_assets",
        "image_urls",
        "source_image_occurrences",
        "project_image_global_id_occurrences",
        "image_work_queue",
        "duplicate_candidates",
        "buildings",
        "building_projects",
        "cluster_events",
        "building_facets",
        "building_facet_claims",
        "project_completeness",
        "building_completeness",
        "qa_issues",
        "build_metrics",
    }
)
BASELINE_REQUIRED_VIEWS = frozenset(
    {
        "v_project_category_provenance",
        "v_building_project_provenance",
        "v_building_images",
        "v_search_facets",
        "v_duplicate_review_queue",
        "v_unmapped_categories",
        "v_qa_open",
        "v_image_hash_queue",
        "v_image_classification_queue",
        "v_architizer_buildings_export",
    }
)


class ReconciliationError(RuntimeError):
    """Raised before an immutable reconciliation plan can be published."""


def _available_memory_bytes() -> Optional[int]:
    try:
        if os.name == "nt":
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_ulong),
                    ("memory_load", ctypes.c_ulong),
                    ("total_physical", ctypes.c_ulonglong),
                    ("available_physical", ctypes.c_ulonglong),
                    ("total_page_file", ctypes.c_ulonglong),
                    ("available_page_file", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong),
                    ("available_virtual", ctypes.c_ulonglong),
                    ("available_extended_virtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.available_physical)
        return int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_AVPHYS_PAGES"))
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _preflight_full_resources(
    *,
    raw_path: Path,
    baseline_path: Path,
    sidecar_path: Path,
    output_path: Path,
) -> dict[str, int]:
    sizes = {
        "raw": raw_path.stat().st_size,
        "baseline": baseline_path.stat().st_size,
        "sidecar": sidecar_path.stat().st_size,
    }
    estimated_temp_copy_bytes = (
        sizes["baseline"] + sizes["sidecar"] + max(sizes["baseline"], sizes["sidecar"])
    )
    required_disk = int(estimated_temp_copy_bytes * 1.20) + 512 * 1024**2
    available_disk = int(shutil.disk_usage(output_path.parent).free)
    required_memory = 512 * 1024**2 + min(sizes["sidecar"] // 32, 2 * 1024**3)
    available_memory = _available_memory_bytes()
    if available_disk < required_disk:
        raise ReconciliationError(
            "full reconciliation disk preflight failed: "
            f"available={available_disk} required={required_disk} "
            f"estimated_temp_copy={estimated_temp_copy_bytes}"
        )
    if available_memory is None or available_memory < required_memory:
        raise ReconciliationError(
            "full reconciliation RAM preflight failed: "
            f"available={available_memory} required={required_memory}"
        )
    return {
        **{f"{key}_input_bytes": int(value) for key, value in sizes.items()},
        "estimated_temp_copy_bytes": estimated_temp_copy_bytes,
        "required_free_disk_bytes": required_disk,
        "available_disk_bytes": available_disk,
        "required_available_memory_bytes": required_memory,
        "available_memory_bytes": available_memory,
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _url_set_sha256(urls: Iterable[str]) -> str:
    payload = "\n".join(sorted(set(urls))).encode("utf-8")
    return hashlib.sha256(payload).hexdigest().upper()


def _path_label(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return path.name


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def _sqlite_sidecars(path: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    for suffix in ("-wal", "-journal"):
        candidate = Path(str(path) + suffix)
        result[suffix[1:]] = candidate.stat().st_size if candidate.exists() else 0
    return result


def _assert_quiescent_input(path: Path) -> dict[str, int]:
    sidecars = _sqlite_sidecars(path)
    if any(sidecars.values()):
        raise ReconciliationError(
            f"input has uncheckpointed SQLite state: {path} {sidecars}"
        )
    return sidecars


def _validate_paths(
    raw: Path,
    baseline: Path,
    sidecar: Path,
    output: Path,
    report: Path,
    ready: Path,
    trusted_manifest: Optional[Path],
) -> None:
    paths = {
        "raw": raw,
        "baseline": baseline,
        "sidecar": sidecar,
        "output": output,
        "report": report,
        "ready": ready,
        "output_wal": Path(str(output) + "-wal"),
        "output_shm": Path(str(output) + "-shm"),
        "output_journal": Path(str(output) + "-journal"),
        "lock": Path(str(output) + ".build.lock"),
        "sidecar_lock": Path(str(sidecar) + ".lock"),
    }
    if trusted_manifest is not None:
        paths["trusted_manifest"] = trusted_manifest
    for input_role, input_path in (
        ("raw", raw),
        ("baseline", baseline),
        ("sidecar", sidecar),
    ):
        for suffix in ("wal", "shm", "journal"):
            paths[f"{input_role}_{suffix}"] = Path(
                str(input_path) + f"-{suffix}"
            )
    seen: dict[str, str] = {}
    for role, path in paths.items():
        key = _path_key(path)
        if key in seen:
            raise ReconciliationError(
                f"path collision: {role} == {seen[key]} ({path})"
            )
        seen[key] = role
    for role in ("raw", "baseline", "sidecar"):
        if not paths[role].is_file():
            raise ReconciliationError(f"missing {role} input: {paths[role]}")
    if trusted_manifest is not None and not trusted_manifest.is_file():
        raise ReconciliationError(
            f"missing trusted input manifest: {trusted_manifest}"
        )
    if output.exists() or report.exists() or ready.exists():
        raise ReconciliationError("immutable output, report, or READY marker already exists")
    for role in ("output_wal", "output_shm", "output_journal"):
        if paths[role].exists():
            raise ReconciliationError(f"stale output sidecar exists: {paths[role]}")


@contextmanager
def _build_lock(output: Path) -> Iterator[None]:
    lock = Path(str(output) + ".build.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json(
        {
            "owner_token": secrets.token_hex(32),
            "tool_version": RECONCILIATION_TOOL_VERSION,
            "output": str(output.resolve()),
            "pid": os.getpid(),
        }
    ).encode("utf-8")
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise ReconciliationError(f"build lock already exists: {lock}") from exc
    try:
        try:
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)
        yield
    finally:
        try:
            if lock.read_bytes() == payload:
                lock.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def _sidecar_read_lock(sidecar: Path, output: Path) -> Iterator[None]:
    """Exclude the recrawler for the complete hash/read/publish interval.

    The recrawler uses the same ``<state-db>.lock`` O_EXCL namespace.  Merely
    checking ``runs`` leaves races before a run row is inserted and after the
    final input hash, so reconciliation participates in that lock protocol.
    """

    lock = Path(str(sidecar) + ".lock")
    payload = canonical_json(
        {
            "mode": "reconciliation_read_snapshot",
            "owner_token": secrets.token_hex(32),
            "output": str(output.resolve()),
            "pid": os.getpid(),
            "state_path": str(sidecar.resolve()),
            "tool_version": RECONCILIATION_TOOL_VERSION,
        }
    ).encode("utf-8")
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise ReconciliationError(f"sidecar lock already exists: {lock}") from exc
    try:
        try:
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)
        yield
    finally:
        try:
            if lock.read_bytes() == payload:
                lock.unlink()
        except FileNotFoundError:
            pass


def _open_readonly(path: Path) -> sqlite3.Connection:
    _assert_quiescent_input(path)
    connection = sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro&immutable=1",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
        connection.close()
        raise ReconciliationError(f"query_only could not be enabled: {path}")
    return connection


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row["name"])
        for row in connection.execute(f'PRAGMA table_info("{table}")')
    }


def _require_columns(
    connection: sqlite3.Connection,
    table: str,
    required: Iterable[str],
) -> None:
    actual = _columns(connection, table)
    missing = sorted(set(required) - actual)
    if missing:
        raise ReconciliationError(f"{table} missing columns: {missing}")


def _validate_input_schemas(
    raw: sqlite3.Connection,
    baseline: sqlite3.Connection,
    sidecar: sqlite3.Connection,
    *,
    raw_sha: str,
) -> dict[str, Any]:
    object_inventory: dict[str, set[str]] = defaultdict(set)
    for row in baseline.execute(
        "SELECT type,name FROM sqlite_master WHERE type IN ('table','view')"
    ):
        object_inventory[row["type"]].add(row["name"])
    missing_tables = sorted(BASELINE_REQUIRED_TABLES - object_inventory["table"])
    missing_views = sorted(BASELINE_REQUIRED_VIEWS - object_inventory["view"])
    if missing_tables or missing_views:
        raise ReconciliationError(
            "baseline v1.3 contract is incomplete: "
            f"tables={missing_tables}, views={missing_views}"
        )
    _require_columns(
        raw,
        "architizer_projects",
        [spec.raw_column for spec in FIELD_SPECS["project"] if spec.raw_column],
    )
    _require_columns(
        raw,
        "architizer_firms",
        [spec.raw_column for spec in FIELD_SPECS["firm"] if spec.raw_column],
    )
    _require_columns(
        baseline,
        "source_projects",
        {
            "source_project_id",
            "global_id",
            "slug",
            "source_url",
            "acceptance_status",
            *(
                spec.curated_column
                for spec in FIELD_SPECS["project"]
                if spec.curated_column
            ),
        },
    )
    _require_columns(
        baseline,
        "source_firms",
        {
            "source_firm_slug",
            "source_url",
            "record_origin",
            *(
                spec.curated_column
                for spec in FIELD_SPECS["firm"]
                if spec.curated_column
            ),
        },
    )
    _require_columns(
        baseline,
        "build_runs",
        ["build_id", "schema_version", "builder_version", "deterministic_timestamp"],
    )
    _require_columns(
        baseline,
        "source_snapshots",
        ["source_sha256_before", "source_sha256_after"],
    )
    for table, columns in {
        "state_meta": ("key", "value"),
        "runs": (
            "id",
            "run_kind",
            "status",
            "parser_version",
            "finished_at",
            "selected_count",
            "summary_json",
        ),
        "targets": (
            "url",
            "entity_type",
            "status",
            "retryable",
            "attempt_count",
            "next_retry_at",
            "last_attempt_at",
            "last_error",
            "last_http_status",
            "last_snapshot_sha256",
            "last_parse_status",
            "last_good_version_id",
        ),
        "metadata_versions": (
            "id",
            "run_id",
            "target_url",
            "entity_type",
            "snapshot_sha256",
            "parser_version",
            "metadata_version",
            "parsed_at",
            "parse_status",
            "quality",
            "identity_status",
            "identity_json",
            "raw_embedded_json",
            "dom_json",
        ),
        "http_attempts": (
            "id",
            "run_id",
            "target_url",
            "request_kind",
            "requested_url",
            "outcome",
            "http_status",
            "final_url",
            "content_type",
            "response_bytes",
            "sha256",
            "gzip_path",
            "block_signals_json",
            "error",
        ),
        "run_targets": ("run_id", "url"),
        "resolved_fields": (
            "version_id",
            "field_name",
            "value_json",
            "status",
            "quality",
            "conflict_json",
        ),
        "field_observations": (
            "version_id",
            "field_name",
            "source_kind",
            "raw_value_json",
            "normalized_value_json",
            "parse_status",
            "quality",
        ),
        "relationships": (
            "version_id",
            "relation_kind",
            "related_entity_type",
            "related_slug",
            "source_kind",
            "parse_status",
        ),
        "target_reasons": (
            "url",
            "reason",
            "discovery_source",
            "priority",
            "source_lastmod",
            "first_seen_at",
            "last_seen_at",
            "input_lineage_json",
        ),
        "snapshot_reparse_inputs": (
            "run_id",
            "target_url",
            "selection_order",
            "entity_type",
            "selection_kind",
            "source_run_id",
            "source_metadata_version_id",
            "source_http_attempt_id",
            "request_kind",
            "requested_url",
            "http_outcome",
            "http_status",
            "block_signals_json",
            "attempt_error",
            "content_sha256",
            "final_url",
            "content_type",
            "response_bytes",
            "gzip_path",
            "gzip_sha256",
            "integrity_status",
            "target_network_state_json",
            "frozen_at",
        ),
        "snapshot_reparse_lineage": (
            "reparse_version_id",
            "reparse_run_id",
            "entity_type",
            "selection_kind",
            "source_run_id",
            "source_metadata_version_id",
            "source_http_attempt_id",
            "request_kind",
            "requested_url",
            "http_outcome",
            "http_status",
            "block_signals_json",
            "attempt_error",
            "target_url",
            "content_sha256",
            "final_url",
            "content_type",
            "response_bytes",
            "gzip_path",
            "gzip_sha256",
            "integrity_status",
            "verified_at",
        ),
    }.items():
        _require_columns(sidecar, table, columns)

    baseline_rows = baseline.execute(
        "SELECT build_id,schema_version,builder_version,deterministic_timestamp "
        "FROM build_runs ORDER BY build_id"
    ).fetchall()
    if len(baseline_rows) != 1:
        raise ReconciliationError("baseline must contain exactly one build run")
    baseline_row = baseline_rows[0]
    if baseline_row["schema_version"] != BASELINE_SCHEMA_VERSION:
        raise ReconciliationError(
            "baseline schema is not curated v1.3: "
            f"{baseline_row['schema_version']}"
        )
    snapshot_rows = baseline.execute(
        "SELECT source_sha256_before,source_sha256_after FROM source_snapshots"
    ).fetchall()
    if len(snapshot_rows) != 1:
        raise ReconciliationError("baseline must contain one source snapshot")
    for key in ("source_sha256_before", "source_sha256_after"):
        if str(snapshot_rows[0][key]).upper() != raw_sha:
            raise ReconciliationError(
                f"baseline/raw lineage mismatch in {key}: "
                f"{snapshot_rows[0][key]} != {raw_sha}"
            )

    state_meta = dict(sidecar.execute("SELECT key,value FROM state_meta"))
    if state_meta.get("source_db_sha256", "").upper() != raw_sha:
        raise ReconciliationError("sidecar/raw source SHA lineage mismatch")
    if state_meta.get("schema_version") != STATE_SCHEMA_VERSION:
        raise ReconciliationError(
            f"unsupported sidecar schema: {state_meta.get('schema_version')}"
        )
    active_runs = sidecar.execute(
        "SELECT id,status FROM runs WHERE finished_at IS NULL OR status='running'"
    ).fetchall()
    if active_runs:
        raise ReconciliationError(
            "sidecar has an active/incomplete run: "
            + canonical_json([dict(row) for row in active_runs])
        )
    invalid_last_good = sidecar.execute(
        """
        SELECT COUNT(*)
        FROM targets t
        LEFT JOIN metadata_versions m ON m.id=t.last_good_version_id
        WHERE t.last_good_version_id IS NOT NULL
          AND (
            m.id IS NULL OR m.target_url != t.url
            OR m.entity_type != t.entity_type OR m.identity_status != 'valid'
          )
        """
    ).fetchone()[0]
    if invalid_last_good:
        raise ReconciliationError(
            f"invalid targets.last_good_version_id links: {invalid_last_good}"
        )
    pending_count = sidecar.execute(
        """
        SELECT COUNT(*) FROM targets
        WHERE status != 'done'
          AND NOT (status='failed' AND retryable=0)
        """
    ).fetchone()[0]
    return {
        "baseline_build": dict(baseline_row),
        "sidecar_meta": state_meta,
        "pending_target_count": int(pending_count),
    }


def _capture_contract(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    rows = connection.execute(
        """
        SELECT type,name,sql
        FROM sqlite_master
        WHERE type IN ('table','view','index')
          AND name NOT LIKE 'sqlite_%'
          AND sql IS NOT NULL
        ORDER BY type,name
        """
    )
    for row in rows:
        sql = str(row["sql"])
        columns: list[dict[str, Any]] = []
        if row["type"] in {"table", "view"}:
            columns = [
                {
                    "cid": item["cid"],
                    "name": item["name"],
                    "type": item["type"],
                    "notnull": item["notnull"],
                    "default": item["dflt_value"],
                    "pk": item["pk"],
                }
                for item in connection.execute(
                    f'PRAGMA table_info("{row["name"]}")'
                )
            ]
        objects.append(
            {
                "object_type": row["type"],
                "object_name": row["name"],
                "sql": sql,
                "sql_sha256": hashlib.sha256(sql.encode("utf-8")).hexdigest().upper(),
                "columns": columns,
            }
        )
    return objects


PLAN_DDL = r"""
PRAGMA foreign_keys=ON;
PRAGMA journal_mode=DELETE;
PRAGMA synchronous=FULL;
PRAGMA trusted_schema=OFF;

CREATE TABLE reconciliation_runs (
    reconciliation_id TEXT PRIMARY KEY,
    tool_version TEXT NOT NULL,
    plan_schema_version TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    baseline_schema_version TEXT NOT NULL,
    final_target_schema_version TEXT NOT NULL,
    artifact_kind TEXT NOT NULL CHECK (artifact_kind='intermediate_reconciliation_plan'),
    publication_eligibility TEXT NOT NULL CHECK (publication_eligibility IN ('smoke_only','eligible_materialization_input')),
    project_limit INTEGER,
    firm_limit INTEGER,
    selected_project_count INTEGER NOT NULL,
    selected_firm_count INTEGER NOT NULL,
    pending_target_count INTEGER NOT NULL,
    deterministic_cutoff TEXT NOT NULL,
    validation_json TEXT NOT NULL CHECK (json_valid(validation_json))
) STRICT;

CREATE TABLE input_snapshots (
    reconciliation_id TEXT NOT NULL REFERENCES reconciliation_runs(reconciliation_id),
    input_role TEXT NOT NULL CHECK (input_role IN ('legacy_raw','curated_v1_3','recrawl_sidecar')),
    path_label TEXT NOT NULL,
    sha256_before TEXT NOT NULL,
    sha256_after TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    query_only INTEGER NOT NULL CHECK (query_only=1),
    quick_check TEXT NOT NULL,
    foreign_key_violations INTEGER NOT NULL CHECK (foreign_key_violations=0),
    lineage_json TEXT NOT NULL CHECK (json_valid(lineage_json)),
    PRIMARY KEY (reconciliation_id,input_role)
) WITHOUT ROWID, STRICT;

CREATE TABLE trusted_input_manifest (
    reconciliation_id TEXT PRIMARY KEY REFERENCES reconciliation_runs(reconciliation_id),
    manifest_version TEXT NOT NULL,
    path_label TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    manifest_json TEXT NOT NULL CHECK (json_valid(manifest_json))
) WITHOUT ROWID, STRICT;

CREATE TABLE baseline_contract_objects (
    object_type TEXT NOT NULL CHECK (object_type IN ('table','view','index')),
    object_name TEXT NOT NULL,
    sql_sha256 TEXT NOT NULL,
    sql_text TEXT NOT NULL,
    columns_json TEXT NOT NULL CHECK (json_valid(columns_json)),
    PRIMARY KEY (object_type,object_name)
) WITHOUT ROWID, STRICT;

CREATE TABLE entities (
    entity_key TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL CHECK (entity_type IN ('project','firm')),
    source_url TEXT NOT NULL UNIQUE,
    source_slug TEXT NOT NULL,
    origin TEXT NOT NULL CHECK (origin IN ('baseline_only','baseline_recrawled','recrawl_new')),
    baseline_present INTEGER NOT NULL CHECK (baseline_present IN (0,1)),
    baseline_identity_key TEXT,
    baseline_acceptance_status TEXT,
    last_good_version_id INTEGER,
    snapshot_sha256 TEXT,
    parser_version TEXT,
    metadata_version TEXT,
    identity_status TEXT NOT NULL,
    inclusion_status TEXT NOT NULL CHECK (inclusion_status IN ('included','qa_only')),
    identity_evidence_json TEXT NOT NULL CHECK (json_valid(identity_evidence_json)),
    effective_fields_json TEXT NOT NULL CHECK (json_valid(effective_fields_json))
) STRICT;

CREATE TABLE entity_aliases (
    entity_key TEXT NOT NULL REFERENCES entities(entity_key),
    target_url TEXT NOT NULL,
    final_url TEXT,
    metadata_version_id INTEGER,
    alias_kind TEXT NOT NULL CHECK (alias_kind IN ('canonical_target','redirect_alias','unresolved_target')),
    evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json)),
    PRIMARY KEY (entity_key,target_url)
) WITHOUT ROWID, STRICT;

CREATE TABLE source_target_reasons (
    target_url TEXT NOT NULL,
    entity_key TEXT REFERENCES entities(entity_key),
    target_entity_type TEXT NOT NULL CHECK (target_entity_type IN ('project','firm')),
    target_status TEXT NOT NULL,
    target_retryable INTEGER NOT NULL CHECK (target_retryable IN (0,1)),
    last_good_version_id INTEGER,
    reason TEXT NOT NULL,
    discovery_source TEXT NOT NULL,
    priority INTEGER NOT NULL,
    source_lastmod TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    input_lineage_json TEXT NOT NULL CHECK (json_valid(input_lineage_json)),
    PRIMARY KEY (target_url,reason,discovery_source)
) WITHOUT ROWID, STRICT;

CREATE TABLE field_candidates (
    candidate_id TEXT PRIMARY KEY,
    entity_key TEXT NOT NULL REFERENCES entities(entity_key),
    field_name TEXT NOT NULL,
    source_role TEXT NOT NULL CHECK (source_role IN ('legacy_raw','curated_v1_3','recrawl_resolved','recrawl_embedded_json','recrawl_dom')),
    value_json TEXT CHECK (value_json IS NULL OR json_valid(value_json)),
    status TEXT NOT NULL,
    quality TEXT NOT NULL,
    metadata_version_id INTEGER,
    source_locator_json TEXT NOT NULL CHECK (json_valid(source_locator_json)),
    UNIQUE (entity_key,field_name,source_role)
) STRICT;

CREATE TABLE field_decisions (
    entity_key TEXT NOT NULL REFERENCES entities(entity_key),
    field_name TEXT NOT NULL,
    effective_value_json TEXT CHECK (effective_value_json IS NULL OR json_valid(effective_value_json)),
    decision_kind TEXT NOT NULL,
    decision_status TEXT NOT NULL,
    selected_candidate_id TEXT REFERENCES field_candidates(candidate_id),
    baseline_candidate_id TEXT REFERENCES field_candidates(candidate_id),
    recrawl_candidate_id TEXT REFERENCES field_candidates(candidate_id),
    rule_id TEXT NOT NULL,
    PRIMARY KEY (entity_key,field_name)
) WITHOUT ROWID, STRICT;

CREATE TABLE field_lineage (
    entity_key TEXT NOT NULL,
    field_name TEXT NOT NULL,
    candidate_id TEXT NOT NULL REFERENCES field_candidates(candidate_id),
    lineage_role TEXT NOT NULL CHECK (lineage_role IN ('selected','supporting','superseded','rejected_conflict','rejected_identity','missing_observation')),
    PRIMARY KEY (entity_key,field_name,candidate_id),
    FOREIGN KEY (entity_key,field_name) REFERENCES field_decisions(entity_key,field_name)
) WITHOUT ROWID, STRICT;

CREATE TABLE field_conflicts (
    conflict_id TEXT PRIMARY KEY,
    entity_key TEXT NOT NULL REFERENCES entities(entity_key),
    field_name TEXT NOT NULL,
    conflict_kind TEXT NOT NULL,
    baseline_value_json TEXT CHECK (baseline_value_json IS NULL OR json_valid(baseline_value_json)),
    recrawl_value_json TEXT CHECK (recrawl_value_json IS NULL OR json_valid(recrawl_value_json)),
    disposition TEXT NOT NULL CHECK (disposition IN ('baseline_retained','recrawl_adopted_with_diff','entity_qa_only')),
    evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json)),
    rule_id TEXT NOT NULL
) STRICT;

CREATE TABLE qa_issues (
    qa_issue_id TEXT PRIMARY KEY,
    entity_key TEXT NOT NULL REFERENCES entities(entity_key),
    issue_code TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('warning','error')),
    status TEXT NOT NULL CHECK (status='open'),
    details_json TEXT NOT NULL CHECK (json_valid(details_json)),
    UNIQUE (entity_key,issue_code)
) STRICT;

CREATE TABLE reconciliation_metrics (
    metric_name TEXT PRIMARY KEY,
    metric_value_json TEXT NOT NULL CHECK (json_valid(metric_value_json))
) STRICT;

CREATE INDEX idx_entities_type_inclusion ON entities(entity_type,inclusion_status,source_url);
CREATE INDEX idx_candidates_entity_field ON field_candidates(entity_key,field_name,source_role);
CREATE INDEX idx_decisions_kind ON field_decisions(decision_kind,field_name);
CREATE INDEX idx_conflicts_kind ON field_conflicts(conflict_kind,field_name);
CREATE INDEX idx_target_reasons_reason ON source_target_reasons(reason,target_entity_type,target_status);

CREATE VIEW v_reconciliation_diff AS
SELECT e.entity_type,e.source_url,e.source_slug,d.*
FROM field_decisions d JOIN entities e USING (entity_key)
WHERE d.decision_kind IN ('recrawl_updated','recrawl_filled','new_from_recrawl','baseline_identity_retained');

CREATE VIEW v_open_conflicts AS
SELECT e.entity_type,e.source_url,c.*
FROM field_conflicts c JOIN entities e USING (entity_key);

CREATE VIEW v_reconciled_projects AS
SELECT
    e.source_url,
    json_extract(e.effective_fields_json,'$.project_id') AS id,
    json_extract(e.effective_fields_json,'$.global_id') AS global_id,
    json_extract(e.effective_fields_json,'$.slug') AS slug,
    json_extract(e.effective_fields_json,'$.name') AS name,
    json_extract(e.effective_fields_json,'$.firm_slug') AS firm_slug,
    json_extract(e.effective_fields_json,'$.firm_name') AS firm_name,
    json_extract(e.effective_fields_json,'$.description') AS description,
    json_extract(e.effective_fields_json,'$.description_short') AS description_short,
    json_extract(e.effective_fields_json,'$.completion_year') AS completion_year,
    json_extract(e.effective_fields_json,'$.building_size_slug') AS building_size_slug,
    json_extract(e.effective_fields_json,'$.building_size_display') AS building_size_display,
    json_extract(e.effective_fields_json,'$.construction_status') AS constr_status,
    json_extract(e.effective_fields_json,'$.budget') AS budget,
    json_extract(e.effective_fields_json,'$.location_full') AS location_full,
    json_extract(e.effective_fields_json,'$.location_country') AS location_country,
    json_extract(e.effective_fields_json,'$.location_city') AS location_city,
    json_extract(e.effective_fields_json,'$.categories') AS categories,
    json_extract(e.effective_fields_json,'$.cover_image_url') AS cover_image_url,
    json_extract(e.effective_fields_json,'$.gallery_image_urls') AS gallery_image_urls,
    json_extract(e.effective_fields_json,'$.image_global_ids') AS image_global_ids,
    json_extract(e.effective_fields_json,'$.published_time') AS published_time,
    json_extract(e.effective_fields_json,'$.modified_time') AS modified_time,
    json_extract(e.effective_fields_json,'$.fetched_at') AS fetched_at,
    e.origin,e.last_good_version_id,e.identity_status
FROM entities e
WHERE e.entity_type='project' AND e.inclusion_status='included';

CREATE VIEW v_reconciled_firms AS
SELECT
    e.source_url,
    json_extract(e.effective_fields_json,'$.slug') AS slug,
    json_extract(e.effective_fields_json,'$.name') AS name,
    json_extract(e.effective_fields_json,'$.office_locations') AS office_locations,
    json_extract(e.effective_fields_json,'$.description') AS description,
    json_extract(e.effective_fields_json,'$.awards_summary') AS awards_summary,
    json_extract(e.effective_fields_json,'$.project_count_seen') AS project_count_seen,
    json_extract(e.effective_fields_json,'$.project_urls') AS project_urls,
    json_extract(e.effective_fields_json,'$.social_links') AS social_links,
    json_extract(e.effective_fields_json,'$.fetched_at') AS fetched_at,
    e.origin,e.last_good_version_id,e.identity_status
FROM entities e
WHERE e.entity_type='firm' AND e.inclusion_status='included';
"""


def _parse_json(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, (dict, list, int, float, bool)):
        return raw
    try:
        return json.loads(str(raw))
    except (json.JSONDecodeError, TypeError, ValueError):
        return raw


def _all_rows_by_key(
    connection: sqlite3.Connection,
    table: str,
    key: str,
) -> dict[Any, sqlite3.Row]:
    return {row[key]: row for row in connection.execute(f'SELECT * FROM "{table}"')}


def _baseline_descriptors(
    baseline: sqlite3.Connection,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in baseline.execute("SELECT * FROM source_projects"):
        if (
            not is_valid_entity_slug(row["slug"])
            or entity_slug_from_url(row["source_url"], "project") != row["slug"]
        ):
            raise ReconciliationError(
                f"invalid baseline project identity: {row['source_url']!r}"
            )
        result[row["source_url"]] = {
            "entity_type": "project",
            "url": row["source_url"],
            "slug": row["slug"],
            "baseline_row": row,
            "acceptance": row["acceptance_status"],
            "baseline_key": str(row["source_project_id"]),
        }
    for row in baseline.execute("SELECT * FROM source_firms"):
        if (
            not is_valid_entity_slug(row["source_firm_slug"])
            or entity_slug_from_url(row["source_url"], "firm")
            != row["source_firm_slug"]
        ):
            raise ReconciliationError(
                f"invalid baseline firm identity: {row['source_url']!r}"
            )
        result[row["source_url"]] = {
            "entity_type": "firm",
            "url": row["source_url"],
            "slug": row["source_firm_slug"],
            "baseline_row": row,
            "acceptance": row["record_origin"],
            "baseline_key": str(row["source_firm_slug"]),
        }
    return result


REPARSE_RUN_KINDS = frozenset(
    {"snapshot_reparse_n10", "snapshot_reparse_n100", "snapshot_reparse_full"}
)


def _has_stored_raw_first_json_recovery(raw_embedded_json: Any) -> bool:
    records = _parse_json(raw_embedded_json)
    if not isinstance(records, list):
        return False
    for record in records:
        if not isinstance(record, Mapping) or record.get("parse_variant") != "raw":
            continue
        raw = record.get("raw")
        if not isinstance(raw, str):
            continue
        fallback = html.unescape(raw)
        if fallback == raw:
            continue
        try:
            json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        try:
            json.loads(fallback)
        except (json.JSONDecodeError, TypeError, ValueError):
            return True
    return False


def _completed_run(
    sidecar: sqlite3.Connection,
    run_id: int,
    *,
    label: str,
) -> sqlite3.Row:
    run = sidecar.execute(
        "SELECT id,run_kind,status,finished_at,parser_version,summary_json "
        "FROM runs WHERE id=?",
        (run_id,),
    ).fetchone()
    if (
        run is None
        or run["finished_at"] is None
        or not str(run["status"]).startswith("completed")
    ):
        raise ReconciliationError(f"{label} run is not completed: {run_id}")
    return run


def _fetch_evidence_for_version(
    sidecar: sqlite3.Connection,
    item: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve either direct-network or immutable snapshot-reparse lineage."""

    version_id = int(item["last_good_version_id"])
    run_id = int(item["run_id"])
    target_url = str(item["url"])
    entity_type = str(item["entity_type"])
    creator_run = _completed_run(sidecar, run_id, label="last-good creator")
    lineage_rows = sidecar.execute(
        "SELECT * FROM snapshot_reparse_lineage WHERE reparse_version_id=?",
        (version_id,),
    ).fetchall()
    expected_request_kind = f"{entity_type}_page"
    if not lineage_rows:
        if creator_run["run_kind"] in REPARSE_RUN_KINDS:
            raise ReconciliationError(
                f"snapshot reparse metadata lacks lineage: {version_id}"
            )
        attempts = sidecar.execute(
            """
            SELECT * FROM http_attempts
            WHERE run_id=? AND target_url=? AND sha256=?
              AND outcome='success' AND http_status=200
            ORDER BY id
            """,
            (run_id, target_url, item["snapshot_sha256"]),
        ).fetchall()
        if len(attempts) != 1:
            raise ReconciliationError(
                "network last-good requires exactly one same-run HTTP attempt: "
                f"version={version_id} count={len(attempts)}"
            )
        attempt = attempts[0]
        if (
            attempt["request_kind"] != expected_request_kind
            or attempt["requested_url"] != target_url
            or attempt["block_signals_json"] != "[]"
            or attempt["error"] is not None
            or not clean_text(attempt["final_url"])
            or not str(attempt["content_type"] or "").lower().startswith("text/html")
            or int(attempt["response_bytes"] or 0) <= 0
            or not clean_text(attempt["gzip_path"])
        ):
            raise ReconciliationError(
                f"network last-good HTTP evidence mismatch: {version_id}"
            )
        return {
            "evidence_kind": "network_http_attempt",
            "http_attempt_id": int(attempt["id"]),
            "creator_run_id": run_id,
            "final_url": clean_text(attempt["final_url"]),
        }

    if len(lineage_rows) != 1:
        raise ReconciliationError(
            f"snapshot reparse metadata has duplicate lineage: {version_id}"
        )
    lineage = lineage_rows[0]
    if creator_run["run_kind"] not in REPARSE_RUN_KINDS:
        raise ReconciliationError(
            f"non-reparse creator has snapshot lineage: {version_id}"
        )
    summary = _parse_json(creator_run["summary_json"])
    if (
        not isinstance(summary, Mapping)
        or summary.get("gate_policy_version")
        != SNAPSHOT_REPARSE_GATE_POLICY_VERSION
        or summary.get("gate_passed") is not True
        or summary.get("state_schema_version") != STATE_SCHEMA_VERSION
        or summary.get("parser_version") != CURRENT_PARSER_VERSION
        or summary.get("metadata_version") != CURRENT_METADATA_VERSION
        or creator_run["parser_version"] != CURRENT_PARSER_VERSION
        or item["parser_version"] != CURRENT_PARSER_VERSION
        or item["metadata_version"] != CURRENT_METADATA_VERSION
    ):
        raise ReconciliationError(
            f"snapshot reparse creator gate contract mismatch: {run_id}"
        )
    if sidecar.execute(
        "SELECT COUNT(*) FROM http_attempts WHERE run_id=?", (run_id,)
    ).fetchone()[0]:
        raise ReconciliationError(
            f"snapshot reparse creator contains network attempts: {run_id}"
        )
    input_rows = sidecar.execute(
        "SELECT * FROM snapshot_reparse_inputs WHERE run_id=? AND target_url=?",
        (run_id, target_url),
    ).fetchall()
    if len(input_rows) != 1:
        raise ReconciliationError(
            f"snapshot reparse input cardinality mismatch: {version_id}"
        )
    frozen = input_rows[0]
    frozen_network_state = _parse_json(frozen["target_network_state_json"])
    target_network_row = sidecar.execute(
        "SELECT * FROM targets WHERE url=?", (target_url,)
    ).fetchone()
    if (
        target_network_row is None
        or not isinstance(frozen_network_state, Mapping)
        or set(frozen_network_state) != set(TARGET_NETWORK_STATE_FIELDS)
        or frozen_network_state
        != {
            field: target_network_row[field]
            for field in TARGET_NETWORK_STATE_FIELDS
        }
    ):
        raise ReconciliationError(
            f"snapshot reparse target network state mismatch: {version_id}"
        )
    shared_fields = (
        "entity_type",
        "selection_kind",
        "source_run_id",
        "source_metadata_version_id",
        "source_http_attempt_id",
        "request_kind",
        "requested_url",
        "http_outcome",
        "http_status",
        "block_signals_json",
        "attempt_error",
        "content_sha256",
        "final_url",
        "content_type",
        "response_bytes",
        "gzip_path",
        "gzip_sha256",
        "integrity_status",
    )
    if (
        lineage["reparse_run_id"] != run_id
        or lineage["target_url"] != target_url
        or lineage["reparse_version_id"] != version_id
        or any(lineage[field] != frozen[field] for field in shared_fields)
        or lineage["entity_type"] != entity_type
        or lineage["content_sha256"] != item["snapshot_sha256"]
        or lineage["request_kind"] != expected_request_kind
        or lineage["requested_url"] != target_url
        or lineage["http_outcome"] != "success"
        or lineage["http_status"] != 200
        or lineage["block_signals_json"] != "[]"
        or lineage["attempt_error"] is not None
        or lineage["integrity_status"] != "verified"
    ):
        raise ReconciliationError(
            f"snapshot reparse lineage/input mismatch: {version_id}"
        )

    source_version_id = int(lineage["source_metadata_version_id"])
    source_run_id = int(lineage["source_run_id"])
    reparse_metadata = sidecar.execute(
        "SELECT * FROM metadata_versions WHERE id=?", (version_id,)
    ).fetchone()
    source_metadata = sidecar.execute(
        "SELECT * FROM metadata_versions WHERE id=?", (source_version_id,)
    ).fetchone()
    _completed_run(sidecar, source_run_id, label="snapshot reparse source")
    if (
        reparse_metadata is None
        or source_metadata is None
        or int(source_metadata["run_id"]) != source_run_id
        or source_metadata["target_url"] != target_url
        or source_metadata["entity_type"] != entity_type
        or source_metadata["snapshot_sha256"] != lineage["content_sha256"]
        or source_metadata["parser_version"] == CURRENT_PARSER_VERSION
        or sidecar.execute(
            "SELECT COUNT(*) FROM snapshot_reparse_lineage "
            "WHERE reparse_version_id=?",
            (source_version_id,),
        ).fetchone()[0]
    ):
        raise ReconciliationError(
            f"snapshot reparse source metadata mismatch or chain: {version_id}"
        )
    selection_kind = str(lineage["selection_kind"])
    source_dom = _parse_json(source_metadata["dom_json"])
    if (
        selection_kind == "firm_last_good_parser_upgrade"
        and (entity_type != "firm" or source_metadata["identity_status"] != "valid")
    ) or (
        selection_kind == "project_parser_regression_recovery"
        and (
            entity_type != "project"
            or source_metadata["identity_status"] == "valid"
            or frozen_network_state["status"] != "failed"
            or frozen_network_state["retryable"] != 0
            or int(frozen_network_state["attempt_count"] or 0) <= 0
            or frozen_network_state["last_attempt_at"] is None
            or frozen_network_state["last_error"] is None
            or frozen_network_state["last_http_status"] != 200
            or frozen_network_state["last_parse_status"] != "no_content"
            or frozen_network_state["last_snapshot_sha256"]
            != source_metadata["snapshot_sha256"]
            or not isinstance(source_dom, Mapping)
            or source_dom.get("_canonical_url") != target_url
            or not _has_stored_raw_first_json_recovery(
                reparse_metadata["raw_embedded_json"]
            )
        )
    ):
        raise ReconciliationError(
            f"snapshot reparse selection/source identity mismatch: {version_id}"
        )
    source_attempts = sidecar.execute(
        """
        SELECT * FROM http_attempts
        WHERE id=? AND run_id=? AND target_url=? AND request_kind=?
          AND requested_url=? AND outcome=? AND http_status=?
          AND block_signals_json=? AND error IS ? AND sha256=?
          AND final_url=? AND content_type=? AND response_bytes=?
          AND gzip_path=?
        """,
        (
            lineage["source_http_attempt_id"],
            source_run_id,
            target_url,
            lineage["request_kind"],
            lineage["requested_url"],
            lineage["http_outcome"],
            lineage["http_status"],
            lineage["block_signals_json"],
            lineage["attempt_error"],
            lineage["content_sha256"],
            lineage["final_url"],
            lineage["content_type"],
            lineage["response_bytes"],
            lineage["gzip_path"],
        ),
    ).fetchall()
    matching_source_attempt_count = sidecar.execute(
        """
        SELECT COUNT(*) FROM http_attempts
        WHERE run_id=? AND target_url=? AND request_kind=?
          AND requested_url=? AND outcome='success' AND http_status=200
          AND block_signals_json='[]' AND error IS NULL AND sha256=?
        """,
        (
            source_run_id,
            target_url,
            lineage["request_kind"],
            target_url,
            lineage["content_sha256"],
        ),
    ).fetchone()[0]
    if len(source_attempts) != 1 or matching_source_attempt_count != 1:
        raise ReconciliationError(
            f"snapshot reparse source HTTP evidence mismatch: {version_id}"
        )
    return {
        "evidence_kind": "snapshot_reparse_lineage",
        "creator_run_id": run_id,
        "source_run_id": source_run_id,
        "source_metadata_version_id": source_version_id,
        "source_http_attempt_id": int(lineage["source_http_attempt_id"]),
        "selection_kind": selection_kind,
        "final_url": clean_text(lineage["final_url"]),
    }


def _sidecar_descriptors(
    sidecar: sqlite3.Connection,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    rows = sidecar.execute(
        """
        SELECT t.url,t.entity_type,t.last_good_version_id,m.run_id,
               m.snapshot_sha256,m.parser_version,m.metadata_version,
               m.parsed_at,m.parse_status,m.quality,m.identity_status,
               m.identity_json
        FROM targets t
        JOIN metadata_versions m ON m.id=t.last_good_version_id
        WHERE t.last_good_version_id IS NOT NULL
        ORDER BY t.entity_type,t.url
        """
    )
    for row in rows:
        item = dict(row)
        if item["entity_type"] not in ARCHITIZER_ENTITY_COLLECTIONS:
            raise ReconciliationError(
                "unsupported sidecar entity_type for "
                f"{item['url']!r}: {item['entity_type']!r}"
            )
        item["target_url"] = item["url"]
        item["identity_payload"] = _parse_json(item["identity_json"])
        issues: list[str] = []
        fetch_evidence = _fetch_evidence_for_version(sidecar, item)
        final_url = clean_text(fetch_evidence["final_url"])
        item["final_url"] = final_url
        item["fetch_evidence"] = fetch_evidence

        target_slug = _discovery_slug_from_url(item["url"], item["entity_type"])
        final_slug = (
            entity_slug_from_url(final_url, item["entity_type"])
            if final_url
            else None
        )
        payload = item["identity_payload"]
        if not isinstance(payload, Mapping):
            payload = {}
            issues.append("identity_payload_not_object")
        if not target_slug:
            issues.append("target_url_slug_missing")
        if not final_slug:
            issues.append("final_url_not_canonical_entity")
        elif final_url != canonical_entity_url(item["entity_type"], final_slug):
            issues.append("final_url_not_normalized_canonical")
        if payload.get("status") != "valid":
            issues.append("identity_payload_not_valid")
        if clean_text(payload.get("expected_slug")) != target_slug:
            issues.append("identity_expected_slug_mismatch")
        if clean_text(payload.get("final_slug")) != final_slug:
            issues.append("identity_final_slug_mismatch")
        for key in ("canonical_slug", "embedded_slug"):
            claim = clean_text(payload.get(key))
            if claim is not None and claim != final_slug:
                issues.append(f"identity_{key}_mismatch")

        resolved_identity: dict[str, Any] = {}
        for resolved_row in sidecar.execute(
            """
            SELECT field_name,value_json,status
            FROM resolved_fields
            WHERE version_id=?
              AND field_name IN ('slug','project_id','global_id','name','firm_slug')
            ORDER BY field_name
            """,
            (item["last_good_version_id"],),
        ):
            if resolved_row["status"] in {"confirmed", "single_source"}:
                resolved_identity[resolved_row["field_name"]] = _parse_json(
                    resolved_row["value_json"]
                )
        if clean_text(resolved_identity.get("slug")) != final_slug:
            issues.append("resolved_slug_mismatch")
        item["resolved_identity"] = resolved_identity
        item["target_slug"] = target_slug
        item["canonical_slug"] = final_slug
        item["canonical_url"] = (
            canonical_entity_url(item["entity_type"], final_slug)
            if final_slug and not issues
            else None
        )
        item["canonicalization_issues"] = sorted(set(issues))
        result[row["url"]] = item
    return result


def _discovery_slug_from_url(url: str, entity_type: str) -> Optional[str]:
    """Read a discovery slug while allowing an explicitly verified alias path."""

    plural = ARCHITIZER_ENTITY_COLLECTIONS.get(entity_type)
    if plural is None or not isinstance(url, str) or url != url.strip():
        return None
    try:
        parsed = urllib.parse.urlsplit(url)
    except (TypeError, ValueError):
        return None
    if parsed.scheme.casefold() != "https":
        return None
    if (parsed.hostname or "").casefold() not in {
        "architizer.com",
        "www.architizer.com",
    }:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if parsed.username or parsed.password or port not in {None, 443}:
        return None
    if parsed.query or parsed.fragment:
        return None
    try:
        parts = [
            urllib.parse.unquote(part, errors="strict")
            for part in parsed.path.split("/")
            if part
        ]
    except UnicodeDecodeError:
        return None
    if any(not is_valid_entity_slug(part) for part in parts):
        return None
    if len(parts) >= 2 and parts[-2] == plural:
        return parts[-1]
    return None


def _identity_signature(item: Mapping[str, Any]) -> str:
    payload = item.get("identity_payload")
    if not isinstance(payload, Mapping):
        payload = {}
    resolved = item.get("resolved_identity")
    if not isinstance(resolved, Mapping):
        resolved = {}
    return canonical_json(
        {
            "entity_type": item.get("entity_type"),
            "slug": clean_text(resolved.get("slug")),
            "project_id": normalize_value(resolved.get("project_id"), "int"),
            "global_id": clean_text(resolved.get("global_id")),
            "payload_project_id": normalize_value(payload.get("project_id"), "int"),
            "payload_global_id": clean_text(payload.get("global_id")),
        }
    )


def _select_descriptors(
    baseline_items: Mapping[str, dict[str, Any]],
    sidecar_items: Mapping[str, dict[str, Any]],
    *,
    project_limit: Optional[int],
    firm_limit: Optional[int],
) -> list[dict[str, Any]]:
    grouped_sidecars: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in sidecar_items.values():
        grouped_sidecars[item.get("canonical_url") or item["target_url"]].append(item)

    combined: dict[str, dict[str, Any]] = {}
    for url in sorted(set(baseline_items) | set(grouped_sidecars)):
        baseline = baseline_items.get(url)
        sidecars = sorted(
            grouped_sidecars.get(url, []), key=lambda item: item["target_url"]
        )
        entity_type = baseline["entity_type"] if baseline else sidecars[0]["entity_type"]
        if any(item["entity_type"] != entity_type for item in sidecars):
            raise ReconciliationError(f"entity type disagreement for {url}")
        descriptor_issues: list[str] = []
        descriptor_warnings: list[str] = []
        for item in sidecars:
            descriptor_issues.extend(item["canonicalization_issues"])
        canonical_sidecars = [
            item for item in sidecars if item["target_url"] == url
        ]
        if len(sidecars) > 1:
            if len({_identity_signature(item) for item in sidecars}) != 1:
                descriptor_issues.append("alias_identity_disagreement")
            if len({item["snapshot_sha256"] for item in sidecars}) != 1:
                if canonical_sidecars:
                    descriptor_warnings.append(
                        "alias_snapshot_disagreement_canonical_target_preferred"
                    )
                else:
                    descriptor_issues.append("alias_snapshot_disagreement")
        chosen_sidecar = (
            canonical_sidecars[0]
            if canonical_sidecars
            else sidecars[0]
            if sidecars
            else None
        )
        slug = (
            baseline["slug"]
            if baseline
            else chosen_sidecar.get("canonical_slug")
            or chosen_sidecar.get("target_slug")
        )
        combined[url] = {
            "url": url,
            "slug": slug,
            "entity_type": entity_type,
            "baseline": baseline,
            "sidecar": chosen_sidecar,
            "sidecar_aliases": sidecars,
            "descriptor_issues": sorted(set(descriptor_issues)),
            "descriptor_warnings": sorted(set(descriptor_warnings)),
        }
    selected: list[dict[str, Any]] = []
    for entity_type, limit in (("project", project_limit), ("firm", firm_limit)):
        rows = [
            row for row in combined.values() if row["entity_type"] == entity_type
        ]
        rows.sort(key=lambda row: row["url"])
        selected.extend(rows if limit is None else _stratified_rows(rows, limit))
    return selected


def _stratified_rows(
    rows: Sequence[dict[str, Any]], limit: int
) -> list[dict[str, Any]]:
    """Round-robin deterministic smoke across material reconciliation types."""

    strata: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["descriptor_issues"]:
            key = "identity_or_alias_qa"
        elif row["baseline"] is None:
            key = "recrawl_new"
        elif row["sidecar"] is not None:
            key = "baseline_recrawled"
        else:
            key = "baseline_only"
        strata[key].append(row)
    ordered_keys = (
        "recrawl_new",
        "baseline_recrawled",
        "identity_or_alias_qa",
        "baseline_only",
    )
    selected: list[dict[str, Any]] = []
    ordinal = 0
    while len(selected) < min(limit, len(rows)):
        progressed = False
        for key in ordered_keys:
            values = strata.get(key, [])
            if ordinal < len(values) and len(selected) < limit:
                selected.append(values[ordinal])
                progressed = True
        if not progressed:
            break
        ordinal += 1
    return selected


def _load_relationship_slugs(
    sidecar: sqlite3.Connection,
    version_ids: set[int],
) -> dict[int, set[str]]:
    relations: dict[int, set[str]] = defaultdict(set)
    if not version_ids:
        return relations
    # Relationship rows do not currently have a version_id index in the
    # recrawl sidecar, so scan once. Resolved/observation rows are keyed by
    # version_id and are loaded one entity at a time below to keep the future
    # full plan bounded in memory.
    for row in sidecar.execute(
        """
        SELECT * FROM relationships
        WHERE relation_kind='project_firm'
          AND related_entity_type='firm' AND parse_status='observed'
        ORDER BY version_id,related_slug
        """
    ):
        version_id = int(row["version_id"])
        if version_id in version_ids and clean_text(row["related_slug"]):
            relations[version_id].add(clean_text(row["related_slug"]))
    return relations


def _load_one_sidecar_version(
    sidecar: sqlite3.Connection,
    version_id: Optional[int],
) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    if version_id is None:
        return {}, {}
    resolved: dict[str, dict[str, Any]] = {}
    observations: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in sidecar.execute(
        "SELECT * FROM resolved_fields WHERE version_id=? ORDER BY field_name",
        (version_id,),
    ):
        item = dict(row)
        item["value"] = _parse_json(row["value_json"])
        item["conflict"] = _parse_json(row["conflict_json"])
        resolved[row["field_name"]] = item
    for row in sidecar.execute(
        """
        SELECT * FROM field_observations
        WHERE version_id=? ORDER BY field_name,source_kind
        """,
        (version_id,),
    ):
        item = dict(row)
        item["value"] = _parse_json(row["normalized_value_json"])
        item["raw_value"] = _parse_json(row["raw_value_json"])
        observations[row["field_name"]].append(item)
    return resolved, observations


def _candidate(
    *,
    entity_key: str,
    field: FieldSpec,
    source_role: str,
    value: Any,
    status: str,
    quality: str,
    locator: Mapping[str, Any],
    metadata_version_id: Optional[int] = None,
) -> tuple[str, Candidate]:
    normalized = normalize_value(value, field.value_kind)
    candidate_id = stable_id(
        "atzcand_", entity_key, field.name, source_role, metadata_version_id
    )
    return candidate_id, Candidate(
        source_role=source_role,
        value=normalized,
        status=status,
        quality=quality,
        locator=locator,
    )


def _insert_candidate(
    output: sqlite3.Connection,
    *,
    candidate_id: str,
    entity_key: str,
    field_name: str,
    candidate: Candidate,
    metadata_version_id: Optional[int],
) -> None:
    output.execute(
        """
        INSERT INTO field_candidates(
            candidate_id,entity_key,field_name,source_role,value_json,status,
            quality,metadata_version_id,source_locator_json
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            candidate_id,
            entity_key,
            field_name,
            candidate.source_role,
            None if candidate.value is None else canonical_json(candidate.value),
            candidate.status,
            candidate.quality,
            metadata_version_id,
            canonical_json(candidate.locator),
        ),
    )


def _row_value(row: Optional[sqlite3.Row], column: Optional[str]) -> Any:
    if row is None or column is None or column not in row.keys():
        return None
    return row[column]


def _universe_identity_issues(
    selected: Sequence[dict[str, Any]],
    *,
    raw_projects: Mapping[Any, sqlite3.Row],
    baseline_firm_slugs: set[str],
) -> dict[str, set[str]]:
    """Reject every ambiguous new identity, rather than accepting first-by-URL."""

    issues: dict[str, set[str]] = defaultdict(set)
    baseline_ids = {int(row["id"]) for row in raw_projects.values()}
    baseline_globals = {clean_text(row["global_id"]) for row in raw_projects.values()}
    baseline_slugs = {clean_text(row["slug"]) for row in raw_projects.values()}
    claim_owners: dict[tuple[str, str], list[str]] = defaultdict(list)
    for descriptor in selected:
        if descriptor["baseline"] is not None or descriptor["sidecar"] is None:
            continue
        if descriptor["descriptor_issues"]:
            continue
        resolved = descriptor["sidecar"].get("resolved_identity") or {}
        if descriptor["entity_type"] == "project":
            project_id = normalize_value(resolved.get("project_id"), "int")
            global_id = clean_text(resolved.get("global_id"))
            slug = clean_text(resolved.get("slug"))
            if project_id in baseline_ids:
                issues[descriptor["url"]].add("new_project_id_collides_with_baseline")
            if global_id in baseline_globals:
                issues[descriptor["url"]].add("new_global_id_collides_with_baseline")
            if slug in baseline_slugs:
                issues[descriptor["url"]].add("new_slug_collides_with_baseline")
            for kind, value in (
                ("project_id", project_id),
                ("global_id", global_id),
                ("slug", slug),
            ):
                if value is not None:
                    claim_owners[(kind, str(value))].append(descriptor["url"])
        else:
            slug = clean_text(resolved.get("slug"))
            if slug in baseline_firm_slugs:
                issues[descriptor["url"]].add("new_firm_slug_collides_with_baseline")
            if slug is not None:
                claim_owners[("firm_slug", slug)].append(descriptor["url"])
    for (kind, _value), owners in claim_owners.items():
        if len(set(owners)) > 1:
            for owner in owners:
                issues[owner].add(f"new_{kind}_collides_with_recrawl")
    return issues


def _materialize_entities(
    output: sqlite3.Connection,
    *,
    selected: Sequence[dict[str, Any]],
    sidecar_connection: sqlite3.Connection,
    raw_projects: Mapping[Any, sqlite3.Row],
    raw_firms: Mapping[Any, sqlite3.Row],
    baseline_firm_slugs: set[str],
    relationship_slugs: Mapping[int, set[str]],
) -> dict[str, Any]:
    origin_counts: Counter[str] = Counter()
    decision_counts: Counter[str] = Counter()
    conflict_counts: Counter[str] = Counter()
    qa_counts: Counter[str] = Counter()

    collision_issues = _universe_identity_issues(
        selected,
        raw_projects=raw_projects,
        baseline_firm_slugs=baseline_firm_slugs,
    )

    for descriptor in selected:
        entity_type = descriptor["entity_type"]
        url = descriptor["url"]
        baseline = descriptor["baseline"]
        sidecar = descriptor["sidecar"]
        slug = descriptor.get("slug")
        if not slug:
            slug = stable_id("unresolved-", entity_type, url)
        entity_key = f"{entity_type}:{url}"
        version_id = int(sidecar["last_good_version_id"]) if sidecar else None
        resolved_rows, observations = _load_one_sidecar_version(
            sidecar_connection, version_id
        )
        resolved_values = {
            field_name: row.get("value")
            if row.get("status") in {"confirmed", "single_source"}
            else None
            for field_name, row in resolved_rows.items()
        }
        identity_payload = _parse_json(sidecar["identity_json"]) if sidecar else {}
        baseline_row = baseline["baseline_row"] if baseline else None
        baseline_identity: Optional[dict[str, Any]] = None
        if baseline:
            baseline_identity = {"slug": slug}
            if entity_type == "project":
                baseline_identity.update(
                    {
                        "project_id": baseline_row["source_project_id"],
                        "global_id": baseline_row["global_id"],
                    }
                )
        identity_issues: list[str] = list(descriptor.get("descriptor_issues") or [])
        identity_issues.extend(collision_issues.get(url, set()))
        if sidecar:
            identity_issues.extend(
                validate_last_good_identity(
                    entity_type=entity_type,
                    target_url=url,
                    identity_status=sidecar["identity_status"],
                    identity_payload=identity_payload,
                    resolved_values=resolved_values,
                    relationship_slugs=relationship_slugs.get(version_id, set()),
                    baseline_identity=baseline_identity,
                )
            )
        identity_issues = sorted(set(identity_issues))

        origin = (
            "baseline_recrawled" if baseline and sidecar
            else "baseline_only" if baseline
            else "recrawl_new"
        )
        inclusion = "included"
        if baseline and entity_type == "project" and baseline["acceptance"] == "excluded":
            inclusion = "qa_only"
        if not baseline and identity_issues:
            inclusion = "qa_only"
        origin_counts[origin] += 1
        output.execute(
            """
            INSERT INTO entities(
                entity_key,entity_type,source_url,source_slug,origin,
                baseline_present,baseline_identity_key,
                baseline_acceptance_status,last_good_version_id,
                snapshot_sha256,parser_version,metadata_version,
                identity_status,inclusion_status,identity_evidence_json,
                effective_fields_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,json('{}'))
            """,
            (
                entity_key,
                entity_type,
                url,
                slug,
                origin,
                1 if baseline else 0,
                baseline["baseline_key"] if baseline else None,
                baseline["acceptance"] if baseline else None,
                version_id,
                sidecar["snapshot_sha256"] if sidecar else None,
                sidecar["parser_version"] if sidecar else None,
                sidecar["metadata_version"] if sidecar else None,
                "valid" if sidecar and not identity_issues else "baseline_only" if baseline else "invalid",
                inclusion,
                canonical_json(
                    {
                        "identity": identity_payload,
                        "issues": identity_issues,
                        "warnings": descriptor.get("descriptor_warnings", []),
                        "last_good_version_id": version_id,
                        "fetch_evidence": (
                            sidecar.get("fetch_evidence", {}) if sidecar else {}
                        ),
                    }
                ),
            ),
        )
        for alias in descriptor.get("sidecar_aliases") or []:
            alias_kind = (
                "unresolved_target"
                if alias.get("canonical_url") is None
                else "canonical_target"
                if alias["target_url"] == url
                else "redirect_alias"
            )
            output.execute(
                """
                INSERT INTO entity_aliases(
                    entity_key,target_url,final_url,metadata_version_id,
                    alias_kind,evidence_json
                ) VALUES (?,?,?,?,?,?)
                """,
                (
                    entity_key,
                    alias["target_url"],
                    alias.get("final_url"),
                    alias.get("last_good_version_id"),
                    alias_kind,
                    canonical_json(
                        {
                            "canonicalization_issues": alias.get(
                                "canonicalization_issues", []
                            ),
                            "identity": alias.get("identity_payload", {}),
                            "snapshot_sha256": alias.get("snapshot_sha256"),
                            "fetch_evidence": alias.get("fetch_evidence", {}),
                        }
                    ),
                ),
            )
        for issue in identity_issues:
            qa_counts[issue] += 1
            output.execute(
                """
                INSERT INTO qa_issues(
                    qa_issue_id,entity_key,issue_code,severity,status,details_json
                ) VALUES (?,?,?,?, 'open',?)
                """,
                (
                    stable_id("atzqa_", entity_key, issue),
                    entity_key,
                    issue,
                    "error" if not baseline else "warning",
                    canonical_json({"url": url, "version_id": version_id}),
                ),
            )

        raw_row = None
        if baseline:
            raw_row = (
                raw_projects.get(int(baseline_row["source_project_id"]))
                if entity_type == "project"
                else raw_firms.get(slug)
            )
        effective: dict[str, Any] = {}
        for spec in FIELD_SPECS[entity_type]:
            lineage_candidates: list[tuple[str, str, str]] = []
            raw_candidate_id: Optional[str] = None
            raw_candidate: Optional[Candidate] = None
            raw_value = _row_value(raw_row, spec.raw_column)
            if raw_row is not None and spec.raw_column:
                raw_candidate_id, raw_candidate = _candidate(
                    entity_key=entity_key,
                    field=spec,
                    source_role="legacy_raw",
                    value=raw_value,
                    status="observed" if not is_empty(raw_value) else "missing",
                    quality="fixed_snapshot",
                    locator={
                        "table": "architizer_projects" if entity_type == "project" else "architizer_firms",
                        "key": baseline["baseline_key"],
                        "column": spec.raw_column,
                    },
                )
                _insert_candidate(
                    output,
                    candidate_id=raw_candidate_id,
                    entity_key=entity_key,
                    field_name=spec.name,
                    candidate=raw_candidate,
                    metadata_version_id=None,
                )
                lineage_candidates.append(
                    (raw_candidate_id, raw_candidate.status, raw_candidate.source_role)
                )

            curated_candidate_id: Optional[str] = None
            curated_candidate: Optional[Candidate] = None
            curated_value = _row_value(baseline_row, spec.curated_column)
            if baseline_row is not None and spec.curated_column:
                curated_candidate_id, curated_candidate = _candidate(
                    entity_key=entity_key,
                    field=spec,
                    source_role="curated_v1_3",
                    value=curated_value,
                    status="accepted" if not is_empty(curated_value) else "missing",
                    quality="curated_baseline",
                    locator={
                        "table": "source_projects" if entity_type == "project" else "source_firms",
                        "key": baseline["baseline_key"],
                        "column": spec.curated_column,
                    },
                )
                _insert_candidate(
                    output,
                    candidate_id=curated_candidate_id,
                    entity_key=entity_key,
                    field_name=spec.name,
                    candidate=curated_candidate,
                    metadata_version_id=None,
                )
                lineage_candidates.append(
                    (
                        curated_candidate_id,
                        curated_candidate.status,
                        curated_candidate.source_role,
                    )
                )

            baseline_candidate_id: Optional[str] = None
            baseline_candidate: Optional[Candidate] = None
            if curated_candidate is not None and not is_empty(curated_candidate.value):
                baseline_candidate_id = curated_candidate_id
                baseline_candidate = curated_candidate
            elif raw_candidate is not None:
                baseline_candidate_id = raw_candidate_id
                baseline_candidate = raw_candidate

            recrawl_candidate_id: Optional[str] = None
            recrawl_candidate: Optional[Candidate] = None
            resolved = resolved_rows.get(spec.sidecar_field) if spec.sidecar_field else None
            if sidecar and spec.sidecar_field and resolved is not None:
                recrawl_candidate_id, recrawl_candidate = _candidate(
                    entity_key=entity_key,
                    field=spec,
                    source_role="recrawl_resolved",
                    value=resolved.get("value"),
                    status=resolved["status"],
                    quality=resolved["quality"],
                    locator={
                        "table": "resolved_fields",
                        "version_id": version_id,
                        "field_name": spec.sidecar_field,
                        "snapshot_sha256": sidecar["snapshot_sha256"],
                        "parser_version": sidecar["parser_version"],
                    },
                    metadata_version_id=version_id,
                )
                _insert_candidate(
                    output,
                    candidate_id=recrawl_candidate_id,
                    entity_key=entity_key,
                    field_name=spec.name,
                    candidate=recrawl_candidate,
                    metadata_version_id=version_id,
                )
                lineage_candidates.append(
                    (
                        recrawl_candidate_id,
                        recrawl_candidate.status,
                        recrawl_candidate.source_role,
                    )
                )
                for observation in observations.get(spec.sidecar_field, []):
                    role = (
                        "recrawl_embedded_json"
                        if observation["source_kind"] == "embedded_json"
                        else "recrawl_dom"
                    )
                    observation_id, observation_candidate = _candidate(
                        entity_key=entity_key,
                        field=spec,
                        source_role=role,
                        value=observation.get("value"),
                        status=observation["parse_status"],
                        quality=observation["quality"],
                        locator={
                            "table": "field_observations",
                            "version_id": version_id,
                            "field_name": spec.sidecar_field,
                            "source_kind": observation["source_kind"],
                            "raw_value": observation.get("raw_value"),
                        },
                        metadata_version_id=version_id,
                    )
                    _insert_candidate(
                        output,
                        candidate_id=observation_id,
                        entity_key=entity_key,
                        field_name=spec.name,
                        candidate=observation_candidate,
                        metadata_version_id=version_id,
                    )
                    lineage_candidates.append(
                        (
                            observation_id,
                            observation_candidate.status,
                            observation_candidate.source_role,
                        )
                    )

            usable_recrawl = recrawl_candidate if sidecar and not identity_issues else None
            decision = reconcile_field(
                baseline=baseline_candidate,
                recrawl=usable_recrawl,
                identity_field=spec.identity,
                entity_is_new=not bool(baseline),
            )
            decision_counts[decision.decision_kind] += 1
            effective[spec.name] = decision.value
            selected_id = (
                baseline_candidate_id
                if decision.selected_role in {"curated_v1_3", "legacy_raw"}
                else recrawl_candidate_id
                if decision.selected_role == "recrawl_resolved"
                else None
            )
            output.execute(
                """
                INSERT INTO field_decisions(
                    entity_key,field_name,effective_value_json,decision_kind,
                    decision_status,selected_candidate_id,
                    baseline_candidate_id,recrawl_candidate_id,rule_id
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    entity_key,
                    spec.name,
                    None if decision.value is None else canonical_json(decision.value),
                    decision.decision_kind,
                    decision.status,
                    selected_id,
                    baseline_candidate_id,
                    recrawl_candidate_id,
                    decision.rule_id,
                ),
            )
            for candidate_id, candidate_status, candidate_source in sorted(
                lineage_candidates
            ):
                if candidate_id == selected_id:
                    role = "selected"
                elif (
                    identity_issues
                    and candidate_source.startswith("recrawl_")
                ):
                    role = "rejected_identity"
                elif (
                    decision.conflict_kind == "parser_conflict"
                    and candidate_source.startswith("recrawl_")
                ):
                    role = "rejected_conflict"
                elif candidate_status == "conflict":
                    role = "rejected_conflict"
                elif candidate_status == "missing":
                    role = "missing_observation"
                elif (
                    decision.selected_role == "recrawl_resolved"
                    and candidate_source in {"curated_v1_3", "legacy_raw"}
                ):
                    role = "superseded"
                else:
                    role = "supporting"
                output.execute(
                    "INSERT INTO field_lineage(entity_key,field_name,candidate_id,lineage_role) VALUES (?,?,?,?)",
                    (entity_key, spec.name, candidate_id, role),
                )

            if (
                raw_candidate is not None
                and curated_candidate is not None
                and not is_empty(raw_candidate.value)
                and not is_empty(curated_candidate.value)
                and not values_equal(raw_candidate.value, curated_candidate.value)
            ):
                conflict_counts["raw_curated_baseline_difference"] += 1
                output.execute(
                    """
                    INSERT INTO field_conflicts(
                        conflict_id,entity_key,field_name,conflict_kind,
                        baseline_value_json,recrawl_value_json,disposition,
                        evidence_json,rule_id
                    ) VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        stable_id("atzconf_", entity_key, spec.name, "raw-curated"),
                        entity_key,
                        spec.name,
                        "raw_curated_baseline_difference",
                        canonical_json(raw_candidate.value),
                        canonical_json(curated_candidate.value),
                        "baseline_retained",
                        canonical_json(
                            {"raw_candidate": raw_candidate_id, "curated_candidate": curated_candidate_id}
                        ),
                        "architizer-baseline-contract-v1",
                    ),
                )
            if decision.conflict_kind:
                conflict_counts[decision.conflict_kind] += 1
                disposition = (
                    "recrawl_adopted_with_diff"
                    if decision.decision_kind == "recrawl_updated"
                    else "entity_qa_only"
                    if inclusion == "qa_only"
                    else "baseline_retained"
                )
                conflict_payload = (
                    resolved.get("conflict") if resolved is not None else None
                )
                output.execute(
                    """
                    INSERT INTO field_conflicts(
                        conflict_id,entity_key,field_name,conflict_kind,
                        baseline_value_json,recrawl_value_json,disposition,
                        evidence_json,rule_id
                    ) VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        stable_id("atzconf_", entity_key, spec.name, decision.conflict_kind),
                        entity_key,
                        spec.name,
                        decision.conflict_kind,
                        None if baseline_candidate is None or baseline_candidate.value is None else canonical_json(baseline_candidate.value),
                        None if recrawl_candidate is None or recrawl_candidate.value is None else canonical_json(recrawl_candidate.value),
                        disposition,
                        canonical_json(
                            {
                                "baseline_candidate": baseline_candidate_id,
                                "recrawl_candidate": recrawl_candidate_id,
                                "parser_conflict": conflict_payload,
                                "identity_issues": identity_issues,
                            }
                        ),
                        decision.rule_id,
                    ),
                )

        output.execute(
            "UPDATE entities SET effective_fields_json=? WHERE entity_key=?",
            (canonical_json(effective), entity_key),
        )

    return {
        "origin_counts": dict(sorted(origin_counts.items())),
        "decision_counts": dict(sorted(decision_counts.items())),
        "conflict_counts": dict(sorted(conflict_counts.items())),
        "qa_counts": dict(sorted(qa_counts.items())),
    }


def _materialize_target_reasons(
    output: sqlite3.Connection,
    *,
    sidecar: sqlite3.Connection,
    include_unselected: bool,
) -> dict[str, Any]:
    """Preserve discovery/retry provenance and compute explicit recovery facts."""

    url_to_entity = {
        str(row[0]): str(row[1])
        for row in output.execute(
            "SELECT source_url,entity_key FROM entities "
            "UNION ALL SELECT target_url,entity_key FROM entity_aliases"
        )
    }
    reason_counts: Counter[str] = Counter()
    rows = sidecar.execute(
        """
        SELECT t.url,t.entity_type,t.status,t.retryable,t.last_good_version_id,
               r.reason,r.discovery_source,r.priority,r.source_lastmod,
               r.first_seen_at,r.last_seen_at,r.input_lineage_json
        FROM targets AS t
        JOIN target_reasons AS r ON r.url=t.url
        ORDER BY t.url,r.reason,r.discovery_source
        """
    )
    for row in rows:
        entity_key = url_to_entity.get(str(row["url"]))
        if entity_key is None and not include_unselected:
            continue
        reason_counts[str(row["reason"])] += 1
        output.execute(
            """
            INSERT INTO source_target_reasons(
                target_url,entity_key,target_entity_type,target_status,
                target_retryable,last_good_version_id,reason,discovery_source,
                priority,source_lastmod,first_seen_at,last_seen_at,
                input_lineage_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                row["url"],
                entity_key,
                row["entity_type"],
                row["status"],
                row["retryable"],
                row["last_good_version_id"],
                row["reason"],
                row["discovery_source"],
                row["priority"],
                row["source_lastmod"],
                row["first_seen_at"],
                row["last_seen_at"],
                row["input_lineage_json"],
            ),
        )

    new_included_projects = int(
        output.execute(
            "SELECT COUNT(*) FROM entities WHERE entity_type='project' "
            "AND origin='recrawl_new' AND inclusion_status='included'"
        ).fetchone()[0]
    )
    baseline_recrawl_changed = int(
        output.execute(
            """
            SELECT COUNT(DISTINCT e.entity_key)
            FROM entities AS e JOIN field_decisions AS d USING(entity_key)
            WHERE e.entity_type='project' AND e.baseline_present=1
              AND e.inclusion_status='included'
              AND d.decision_kind IN ('recrawl_updated','recrawl_filled')
            """
        ).fetchone()[0]
    )
    recovered_failed_retry = int(
        output.execute(
            """
            SELECT COUNT(DISTINCT r.target_url)
            FROM source_target_reasons AS r
            JOIN entities AS e ON e.entity_key=r.entity_key
            WHERE r.reason='legacy_failed_retry'
              AND r.target_entity_type='project'
              AND r.target_status='done'
              AND r.last_good_version_id IS NOT NULL
              AND e.identity_status='valid' AND e.inclusion_status='included'
            """
        ).fetchone()[0]
    )
    unrecovered_terminal_mismatch = int(
        output.execute(
            """
            SELECT COUNT(DISTINCT r.target_url)
            FROM source_target_reasons AS r
            LEFT JOIN entities AS e ON e.entity_key=r.entity_key
            WHERE r.reason='legacy_done_row_mismatch'
              AND r.target_entity_type='project'
              AND r.target_status='failed' AND r.target_retryable=0
              AND (e.entity_key IS NULL OR e.identity_status!='valid'
                   OR e.inclusion_status!='included')
            """
        ).fetchone()[0]
    )
    recovered_project_regression_reparse = int(
        output.execute(
            """
            SELECT COUNT(*) FROM entities
            WHERE entity_type='project' AND identity_status='valid'
              AND inclusion_status='included'
              AND json_extract(identity_evidence_json,
                  '$.fetch_evidence.selection_kind')
                  ='project_parser_regression_recovery'
            """
        ).fetchone()[0]
    )
    return {
        "reason_counts": dict(sorted(reason_counts.items())),
        "source_recovery_counts": {
            "new_included_project_count": new_included_projects,
            "baseline_project_recrawl_updated_or_filled_count": baseline_recrawl_changed,
            "recovered_legacy_failed_retry_valid_included_count": recovered_failed_retry,
            "unrecovered_legacy_done_row_mismatch_terminal_count": unrecovered_terminal_mismatch,
            "recovered_project_parser_regression_reparse_count": recovered_project_regression_reparse,
            "definitions": {
                "new_included_project_count": "included project absent from curated v1.3 and supplied by a valid last-good recrawl version",
                "baseline_project_recrawl_updated_or_filled_count": "distinct included baseline project with at least one recrawl_updated or recrawl_filled field decision",
                "recovered_legacy_failed_retry_valid_included_count": "legacy_failed_retry project target in done state linked to valid included entity with last-good metadata",
                "unrecovered_legacy_done_row_mismatch_terminal_count": "legacy_done_row_mismatch project target in non-retryable failed state without a valid included entity",
                "recovered_project_parser_regression_reparse_count": "included valid project whose last-good metadata was created by exact project_parser_regression_recovery snapshot lineage",
            },
        },
    }


def _quick_check(connection: sqlite3.Connection) -> str:
    rows = [row[0] for row in connection.execute("PRAGMA quick_check")]
    return "ok" if rows == ["ok"] else canonical_json(rows)


def _foreign_key_violation_count(connection: sqlite3.Connection) -> int:
    return len(connection.execute("PRAGMA foreign_key_check").fetchall())


def _validate_input_integrity(
    connections: Mapping[str, sqlite3.Connection],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for role, connection in sorted(connections.items()):
        quick = _quick_check(connection)
        if quick != "ok":
            raise ReconciliationError(f"input quick_check failed for {role}: {quick}")
        foreign_keys = _foreign_key_violation_count(connection)
        if foreign_keys:
            raise ReconciliationError(
                f"input foreign-key violations for {role}: {foreign_keys}"
            )
        result[role] = {
            "quick_check": quick,
            "foreign_key_violations": foreign_keys,
        }
    return result


def _validate_trusted_manifest(
    *,
    manifest_path: Path,
    input_before: Mapping[str, Mapping[str, Any]],
    sidecar: sqlite3.Connection,
    sidecar_meta: Mapping[str, str],
) -> dict[str, Any]:
    """Bind a full eligible plan to fixed v1.3 and a frozen sidecar universe."""

    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReconciliationError(f"invalid trusted input manifest: {exc}") from exc
    if not isinstance(payload, dict):
        raise ReconciliationError("trusted input manifest must be a JSON object")
    if set(payload) != {
        "manifest_version",
        "artifact_kind",
        "inputs",
        "sidecar_contract",
    }:
        raise ReconciliationError("trusted manifest top-level contract mismatch")
    if payload.get("manifest_version") != TRUSTED_MANIFEST_VERSION:
        raise ReconciliationError(
            f"unsupported trusted manifest version: {payload.get('manifest_version')}"
        )
    if payload.get("artifact_kind") != "trusted_architizer_reconciliation_inputs":
        raise ReconciliationError("trusted manifest artifact_kind mismatch")

    fixed = {
        "legacy_raw": (FIXED_RAW_SHA256, FIXED_RAW_SIZE_BYTES),
        "curated_v1_3": (FIXED_BASELINE_SHA256, FIXED_BASELINE_SIZE_BYTES),
    }
    for role, (expected_sha, expected_size) in fixed.items():
        actual = input_before[role]
        if actual["sha256"] != expected_sha or actual["size_bytes"] != expected_size:
            raise ReconciliationError(
                f"{role} is not the fixed production input: "
                f"{actual['sha256']}/{actual['size_bytes']}"
            )

    inputs = payload.get("inputs")
    if not isinstance(inputs, dict) or set(inputs) != set(input_before):
        raise ReconciliationError("trusted manifest input roles mismatch")
    for role, actual in sorted(input_before.items()):
        expected = inputs.get(role)
        if not isinstance(expected, dict):
            raise ReconciliationError(f"trusted manifest input missing: {role}")
        if set(expected) != {"sha256", "size_bytes"}:
            raise ReconciliationError(
                f"trusted manifest input contract mismatch: {role}"
            )
        if str(expected.get("sha256", "")).upper() != actual["sha256"]:
            raise ReconciliationError(f"trusted manifest SHA mismatch: {role}")
        if expected.get("size_bytes") != actual["size_bytes"]:
            raise ReconciliationError(f"trusted manifest size mismatch: {role}")

    contract = payload.get("sidecar_contract")
    if not isinstance(contract, dict):
        raise ReconciliationError("trusted manifest sidecar_contract missing")
    sidecar_contract_keys = {
        "schema_version",
        "source_db_sha256",
        "source_db_size",
        "pending_target_count",
        "active_run_count",
        "done_without_last_good_count",
        "invalid_last_good_link_count",
        "last_good_target_count",
        "last_good_target_urls_sha256",
        "last_good_evidence_kind_counts",
        "parser_versions",
        "metadata_versions",
        "required_completed_runs",
        "input_integrity",
    }
    if set(contract) != sidecar_contract_keys:
        raise ReconciliationError("trusted manifest sidecar contract mismatch")
    if contract.get("schema_version") != sidecar_meta.get("schema_version"):
        raise ReconciliationError("trusted manifest sidecar schema mismatch")
    raw_identity = input_before["legacy_raw"]
    source_sha = str(contract.get("source_db_sha256") or "").upper()
    try:
        source_size = int(contract.get("source_db_size"))
    except (TypeError, ValueError) as exc:
        raise ReconciliationError("trusted manifest sidecar source size mismatch") from exc
    if isinstance(contract.get("source_db_size"), bool):
        raise ReconciliationError("trusted manifest sidecar source size mismatch")
    if (
        source_sha != raw_identity["sha256"]
        or source_sha != str(sidecar_meta.get("source_db_sha256") or "").upper()
        or source_size != raw_identity["size_bytes"]
        or source_size != int(sidecar_meta.get("source_db_size") or -1)
    ):
        raise ReconciliationError("trusted manifest sidecar source lineage mismatch")

    active_run_count = int(
        sidecar.execute(
            "SELECT COUNT(*) FROM runs WHERE finished_at IS NULL OR status='running'"
        ).fetchone()[0]
    )
    pending_target_count = int(
        sidecar.execute(
            "SELECT COUNT(*) FROM targets WHERE status!='done' "
            "AND NOT (status='failed' AND retryable=0)"
        ).fetchone()[0]
    )
    done_without_last_good_count = int(
        sidecar.execute(
            "SELECT COUNT(*) FROM targets "
            "WHERE status='done' AND last_good_version_id IS NULL"
        ).fetchone()[0]
    )
    invalid_last_good_link_count = int(
        sidecar.execute(
            """
            SELECT COUNT(*)
            FROM targets AS t
            LEFT JOIN metadata_versions AS m ON m.id=t.last_good_version_id
            WHERE t.last_good_version_id IS NOT NULL
              AND (
                m.id IS NULL OR m.target_url!=t.url
                OR m.entity_type!=t.entity_type OR m.identity_status!='valid'
              )
            """
        ).fetchone()[0]
    )
    last_good_urls = [
        str(row[0])
        for row in sidecar.execute(
            "SELECT url FROM targets WHERE last_good_version_id IS NOT NULL ORDER BY url"
        )
    ]
    exact_counts = {
        "pending_target_count": pending_target_count,
        "active_run_count": active_run_count,
        "done_without_last_good_count": done_without_last_good_count,
        "invalid_last_good_link_count": invalid_last_good_link_count,
        "last_good_target_count": len(last_good_urls),
    }
    for key, actual in exact_counts.items():
        expected_value = contract.get(key)
        if (
            isinstance(expected_value, bool)
            or not isinstance(expected_value, int)
            or expected_value != actual
        ):
            raise ReconciliationError(f"trusted manifest {key} mismatch")
    if str(contract.get("last_good_target_urls_sha256") or "").upper() != _url_set_sha256(
        last_good_urls
    ):
        raise ReconciliationError("trusted manifest last-good URL SHA mismatch")
    evidence_kind_counts: Counter[str] = Counter()
    for evidence_row in sidecar.execute(
        """
        SELECT t.url,t.entity_type,t.last_good_version_id,m.run_id,
               m.snapshot_sha256,m.parser_version,m.metadata_version,
               m.identity_status
        FROM targets AS t
        JOIN metadata_versions AS m ON m.id=t.last_good_version_id
        WHERE t.last_good_version_id IS NOT NULL
        ORDER BY t.url
        """
    ):
        evidence = _fetch_evidence_for_version(sidecar, dict(evidence_row))
        evidence_kind_counts[str(evidence["evidence_kind"])] += 1
    expected_evidence_counts = contract.get("last_good_evidence_kind_counts")
    if (
        not isinstance(expected_evidence_counts, dict)
        or expected_evidence_counts != dict(sorted(evidence_kind_counts.items()))
    ):
        raise ReconciliationError(
            "trusted manifest last-good evidence kind counts mismatch"
        )
    input_integrity = contract.get("input_integrity")
    actual_integrity = {
        "quick_check": _quick_check(sidecar),
        "foreign_key_violation_count": _foreign_key_violation_count(sidecar),
    }
    if (
        not isinstance(input_integrity, dict)
        or set(input_integrity) != set(actual_integrity)
        or input_integrity != actual_integrity
    ):
        raise ReconciliationError("trusted manifest input_integrity mismatch")
    if any(exact_counts.values()) and any(
        exact_counts[key]
        for key in (
            "pending_target_count",
            "active_run_count",
            "done_without_last_good_count",
            "invalid_last_good_link_count",
        )
    ):
        raise ReconciliationError("trusted manifest sidecar is not converged and valid")
    required_runs = contract.get("required_completed_runs")
    if not isinstance(required_runs, list) or not required_runs:
        raise ReconciliationError("trusted manifest completed run set is empty")
    manifest_run_ids: set[int] = set()
    required_run_keys = {
        "id",
        "run_kind",
        "status",
        "finished_at",
        "parser_version",
        "selected_count",
        "frozen_target_urls_sha256",
        "referenced_last_good_count",
        "referenced_last_good_urls_sha256",
        "referenced_parser_versions",
        "referenced_metadata_versions",
        "snapshot_reparse_gate",
    }
    for expected in required_runs:
        if not isinstance(expected, dict) or isinstance(expected.get("id"), bool):
            raise ReconciliationError("invalid trusted manifest run entry")
        if set(expected) != required_run_keys:
            raise ReconciliationError("trusted manifest run entry contract mismatch")
        try:
            run_id = int(expected["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ReconciliationError("invalid trusted manifest run id") from exc
        if run_id in manifest_run_ids:
            raise ReconciliationError("duplicate trusted manifest run id")
        manifest_run_ids.add(run_id)
        run = sidecar.execute(
            "SELECT id,run_kind,status,finished_at,parser_version,"
            "selected_count,summary_json FROM runs WHERE id=?",
            (run_id,),
        ).fetchone()
        if run is None or run["finished_at"] is None:
            raise ReconciliationError(f"trusted completed run missing: {run_id}")
        if run["status"] != expected.get("status") or not str(run["status"]).startswith(
            "completed"
        ):
            raise ReconciliationError(f"trusted run status mismatch: {run_id}")
        for key in ("run_kind", "finished_at", "parser_version"):
            if run[key] != expected.get(key):
                raise ReconciliationError(
                    f"trusted run {key} mismatch: {run_id}"
                )
        summary = _parse_json(run["summary_json"])
        if not isinstance(summary, Mapping):
            raise ReconciliationError(f"trusted run summary missing: {run_id}")
        expected_gate = expected.get("snapshot_reparse_gate")
        if str(run["run_kind"]) in REPARSE_RUN_KINDS:
            actual_gate = {
                "gate_policy_version": summary.get("gate_policy_version"),
                "gate_passed": summary.get("gate_passed"),
                "state_schema_version": summary.get("state_schema_version"),
                "parser_version": summary.get("parser_version"),
                "metadata_version": summary.get("metadata_version"),
            }
            if actual_gate != {
                "gate_policy_version": SNAPSHOT_REPARSE_GATE_POLICY_VERSION,
                "gate_passed": True,
                "state_schema_version": STATE_SCHEMA_VERSION,
                "parser_version": CURRENT_PARSER_VERSION,
                "metadata_version": CURRENT_METADATA_VERSION,
            } or expected_gate != actual_gate:
                raise ReconciliationError(
                    f"trusted snapshot reparse gate mismatch: {run_id}"
                )
        elif expected_gate is not None:
            raise ReconciliationError(
                f"trusted non-reparse run has reparse gate: {run_id}"
            )
        frozen_sha = str(expected.get("frozen_target_urls_sha256", "")).upper()
        run_target_urls = [
            str(row[0])
            for row in sidecar.execute(
                "SELECT url FROM run_targets WHERE run_id=? ORDER BY url",
                (run_id,),
            )
        ]
        run_target_sha = _url_set_sha256(
            run_target_urls
        )
        expected_selected = expected.get("selected_count")
        try:
            actual_selected = int(run["selected_count"])
        except (TypeError, ValueError) as exc:
            raise ReconciliationError(
                f"trusted run selected_count mismatch: {run_id}"
            ) from exc
        if (
            isinstance(expected_selected, bool)
            or not isinstance(expected_selected, int)
            or expected_selected != actual_selected
            or actual_selected != len(run_target_urls)
        ):
            raise ReconciliationError(f"trusted run selected_count mismatch: {run_id}")
        summary_sha = str(summary.get("frozen_target_urls_sha256", "")).upper()
        if (
            len(frozen_sha) != 64
            or run_target_sha != frozen_sha
            or (summary_sha and summary_sha != frozen_sha)
        ):
            raise ReconciliationError(f"trusted frozen URL SHA mismatch: {run_id}")

        referenced_rows = sidecar.execute(
            """
            SELECT t.url,m.parser_version,m.metadata_version
            FROM targets t JOIN metadata_versions m
              ON m.id=t.last_good_version_id
            WHERE t.last_good_version_id IS NOT NULL AND m.run_id=?
            ORDER BY t.url
            """,
            (run_id,),
        ).fetchall()
        referenced_urls = [str(row["url"]) for row in referenced_rows]
        referenced_count = expected.get("referenced_last_good_count")
        if (
            isinstance(referenced_count, bool)
            or referenced_count != len(referenced_rows)
        ):
            raise ReconciliationError(
                f"trusted referenced last-good count mismatch: {run_id}"
            )
        if (
            str(expected.get("referenced_last_good_urls_sha256", "")).upper()
            != _url_set_sha256(referenced_urls)
        ):
            raise ReconciliationError(
                f"trusted referenced last-good URL SHA mismatch: {run_id}"
            )
        for key, column in (
            ("referenced_parser_versions", "parser_version"),
            ("referenced_metadata_versions", "metadata_version"),
        ):
            expected_values = expected.get(key)
            if not isinstance(expected_values, list) or sorted(
                str(value) for value in expected_values
            ) != sorted({str(row[column]) for row in referenced_rows}):
                raise ReconciliationError(f"trusted {key} mismatch: {run_id}")

    last_good_run_ids = {
        int(row[0])
        for row in sidecar.execute(
            """
            SELECT DISTINCT m.run_id
            FROM targets t JOIN metadata_versions m ON m.id=t.last_good_version_id
            WHERE t.last_good_version_id IS NOT NULL
            """
        )
    }
    if manifest_run_ids != last_good_run_ids:
        raise ReconciliationError(
            "trusted completed run universe mismatch: "
            f"manifest={sorted(manifest_run_ids)} sidecar={sorted(last_good_run_ids)}"
        )
    for key, column in (
        ("parser_versions", "parser_version"),
        ("metadata_versions", "metadata_version"),
    ):
        expected_values = contract.get(key)
        if not isinstance(expected_values, list) or not expected_values:
            raise ReconciliationError(f"trusted manifest {key} missing")
        actual_values = sorted(
            {
                str(row[0])
                for row in sidecar.execute(
                    f"""
                    SELECT DISTINCT m.{column}
                    FROM targets t JOIN metadata_versions m
                      ON m.id=t.last_good_version_id
                    WHERE t.last_good_version_id IS NOT NULL
                    ORDER BY m.{column}
                    """
                )
            }
        )
        if sorted(str(value) for value in expected_values) != actual_values:
            raise ReconciliationError(f"trusted manifest {key} mismatch")
    return {
        "manifest_version": TRUSTED_MANIFEST_VERSION,
        "path_label": _path_label(manifest_path),
        "sha256": sha256_file(manifest_path),
        "size_bytes": manifest_path.stat().st_size,
        "payload": payload,
    }


def _database_logical_sha(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    objects = connection.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ).fetchall()
    for row in objects:
        digest.update(canonical_json(list(row)).encode("utf-8"))
        if row["type"] != "table":
            continue
        columns = [item["name"] for item in connection.execute(f'PRAGMA table_info("{row["name"]}")')]
        if not columns:
            continue
        quoted = ",".join(f'"{column}"' for column in columns)
        # Ordering by the complete row works for both rowid and WITHOUT ROWID
        # tables and makes the logical digest independent of insertion order.
        for value_row in connection.execute(
            f'SELECT {quoted} FROM "{row["name"]}" ORDER BY {quoted}'
        ):
            digest.update(canonical_json(list(value_row)).encode("utf-8"))
    return digest.hexdigest().upper()


def _publish_bundle(
    database_temp: Path,
    report_temp: Path,
    ready_temp: Path,
    output: Path,
    report: Path,
    ready: Path,
    *,
    pre_ready_check: Callable[[], None] | None = None,
) -> None:
    """Publish immutable members, with READY as the sole validity marker."""

    if output.exists() or report.exists() or ready.exists():
        raise ReconciliationError("immutable output bundle appeared during build")
    members = (
        (database_temp, output),
        (report_temp, report),
        (ready_temp, ready),
    )
    try:
        os.link(database_temp, output)
        os.link(report_temp, report)
        # Consumers must require READY. Publishing it last makes a process-kill
        # between earlier links an incomplete, visibly non-consumable bundle.
        if pre_ready_check is not None:
            pre_ready_check()
        os.link(ready_temp, ready)
        # Close the race where an input changes inside the READY link call.
        # The surrounding rollback still owns all three hard links here.
        if pre_ready_check is not None:
            pre_ready_check()
    except BaseException:
        for temporary, published in reversed(members):
            try:
                if published.exists() and os.path.samefile(temporary, published):
                    published.unlink()
            except (FileNotFoundError, OSError):
                pass
        raise


def render_report(result: Mapping[str, Any]) -> str:
    return "\n".join(
        [
            "# Architizer curated v2 reconciliation plan",
            "",
            "> This is an intermediate, non-consumer artifact. It does not replace curated v1.3 or the future consumer-ready curated v2.0 SQLite.",
            "",
            f"- Reconciliation ID: `{result['reconciliation_id']}`",
            f"- Eligibility: `{result['publication_eligibility']}`",
            f"- Projects / firms: {result['selected_project_count']} / {result['selected_firm_count']}",
            f"- Pending sidecar targets: {result['pending_target_count']}",
            f"- Trusted input manifest: `{(result.get('trusted_manifest') or {}).get('sha256') or 'none (smoke)'}`",
            f"- Output SHA-256: `{result['database_sha256']}`",
            f"- Logical SHA-256: `{result['logical_sha256']}`",
            f"- Resource preflight: `{canonical_json(result.get('resource_preflight'))}`",
            "",
            "## Inputs",
            "",
            *[
                f"- {role}: `{payload['sha256']}` ({payload['size_bytes']} bytes)"
                for role, payload in sorted(result["inputs"].items())
            ],
            "",
            "## Entity origins",
            "",
            f"`{canonical_json(result['metrics']['origin_counts'])}`",
            "",
            "## Field decisions",
            "",
            f"`{canonical_json(result['metrics']['decision_counts'])}`",
            "",
            "## Conflicts and QA",
            "",
            f"- Conflicts: `{canonical_json(result['metrics']['conflict_counts'])}`",
            f"- QA: `{canonical_json(result['metrics']['qa_counts'])}`",
            "",
            "## Source recovery definitions and counts",
            "",
            f"`{canonical_json(result['metrics']['source_recovery_counts'])}`",
            "",
            f"- Preserved target reasons: `{canonical_json(result['metrics']['reason_counts'])}`",
            "",
            "The artifact is consumable only when its sibling READY marker exists and verifies the DB/report hashes. The future final materializer must require `eligible_materialization_input`, consume the reconciled views, rebuild the full v1.3-compatible curated contract as v2.0, and publish to a new immutable path.",
            "",
        ]
    )


def build_plan(
    *,
    raw_path: Path,
    baseline_path: Path,
    sidecar_path: Path,
    output_path: Path,
    report_path: Path,
    ready_path: Optional[Path] = None,
    trusted_manifest_path: Optional[Path] = None,
    project_limit: Optional[int] = None,
    firm_limit: Optional[int] = None,
    require_converged: bool = False,
) -> dict[str, Any]:
    for label, value in (("project_limit", project_limit), ("firm_limit", firm_limit)):
        if value is not None and value <= 0:
            raise ReconciliationError(f"{label} must be positive")
    if (project_limit is None) != (firm_limit is None):
        raise ReconciliationError(
            "limited smoke plans require both project_limit and firm_limit"
        )
    if project_limit is None and not require_converged:
        raise ReconciliationError(
            "an unlimited plan requires explicit converged/full confirmation"
        )
    if project_limit is not None and require_converged:
        raise ReconciliationError(
            "a converged/full plan cannot use smoke limits"
        )
    if not require_converged and trusted_manifest_path is not None:
        raise ReconciliationError(
            "trusted input manifests are accepted only for converged/full plans"
        )
    raw_path = raw_path.resolve()
    baseline_path = baseline_path.resolve()
    sidecar_path = sidecar_path.resolve()
    output_path = output_path.resolve()
    report_path = report_path.resolve()
    ready_path = (
        ready_path.resolve()
        if ready_path is not None
        else Path(str(output_path) + ".READY.json").resolve()
    )
    trusted_manifest_path = (
        trusted_manifest_path.resolve()
        if trusted_manifest_path is not None
        else None
    )
    if project_limit is not None and (
        output_path == DEFAULT_OUTPUT_DB.resolve()
        or report_path == DEFAULT_REPORT.resolve()
        or ready_path == Path(str(DEFAULT_OUTPUT_DB.resolve()) + ".READY.json")
    ):
        raise ReconciliationError(
            "smoke plans require explicit non-production output, report, and READY paths"
        )
    if require_converged and trusted_manifest_path is None:
        raise ReconciliationError(
            "a trusted input manifest is required for a full converged plan"
        )
    _validate_paths(
        raw_path,
        baseline_path,
        sidecar_path,
        output_path,
        report_path,
        ready_path,
        trusted_manifest_path,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    ready_path.parent.mkdir(parents=True, exist_ok=True)

    resource_preflight = (
        _preflight_full_resources(
            raw_path=raw_path,
            baseline_path=baseline_path,
            sidecar_path=sidecar_path,
            output_path=output_path,
        )
        if require_converged
        else None
    )

    input_paths = {
        "legacy_raw": raw_path,
        "curated_v1_3": baseline_path,
        "recrawl_sidecar": sidecar_path,
    }
    with _build_lock(output_path), _sidecar_read_lock(sidecar_path, output_path):
        input_before = {
            role: {
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
                "path_label": _path_label(path),
            }
            for role, path in input_paths.items()
        }
        manifest_before = (
            {
                "sha256": sha256_file(trusted_manifest_path),
                "size_bytes": trusted_manifest_path.stat().st_size,
            }
            if trusted_manifest_path is not None
            else None
        )
        raw = _open_readonly(raw_path)
        baseline = _open_readonly(baseline_path)
        sidecar = _open_readonly(sidecar_path)
        database_temp: Optional[Path] = None
        report_temp: Optional[Path] = None
        ready_temp: Optional[Path] = None
        published = False
        try:
            input_connections = {
                "legacy_raw": raw,
                "curated_v1_3": baseline,
                "recrawl_sidecar": sidecar,
            }
            input_integrity = _validate_input_integrity(input_connections)
            validation = _validate_input_schemas(
                raw,
                baseline,
                sidecar,
                raw_sha=input_before["legacy_raw"]["sha256"],
            )
            validation["input_integrity"] = input_integrity
            pending_count = validation["pending_target_count"]
            if require_converged and pending_count:
                raise ReconciliationError(
                    f"sidecar is not converged: {pending_count} targets remain"
                )
            trusted_manifest = (
                _validate_trusted_manifest(
                    manifest_path=trusted_manifest_path,
                    input_before=input_before,
                    sidecar=sidecar,
                    sidecar_meta=validation["sidecar_meta"],
                )
                if require_converged and trusted_manifest_path is not None
                else None
            )
            baseline_contract = _capture_contract(baseline)
            baseline_items = _baseline_descriptors(baseline)
            baseline_firm_slugs = {
                str(item["slug"])
                for item in baseline_items.values()
                if item["entity_type"] == "firm"
            }
            sidecar_items = _sidecar_descriptors(sidecar)
            selected = _select_descriptors(
                baseline_items,
                sidecar_items,
                project_limit=project_limit,
                firm_limit=firm_limit,
            )
            project_count = sum(row["entity_type"] == "project" for row in selected)
            firm_count = sum(row["entity_type"] == "firm" for row in selected)
            version_ids = {
                int(row["sidecar"]["last_good_version_id"])
                for row in selected
                if row["sidecar"] is not None
            }
            relations = _load_relationship_slugs(sidecar, version_ids)
            raw_projects = _all_rows_by_key(raw, "architizer_projects", "id")
            raw_firms = _all_rows_by_key(raw, "architizer_firms", "slug")

            cutoff_row = sidecar.execute(
                "SELECT COALESCE(MAX(finished_at),'no-completed-run') FROM runs"
            ).fetchone()
            deterministic_cutoff = str(cutoff_row[0])
            publication_eligibility = (
                "eligible_materialization_input"
                if (
                    project_limit is None
                    and firm_limit is None
                    and pending_count == 0
                    and require_converged
                    and trusted_manifest is not None
                )
                else "smoke_only"
            )
            reconciliation_id = stable_id(
                "atzrecon_",
                *(input_before[role]["sha256"] for role in sorted(input_before)),
                project_limit,
                firm_limit,
                deterministic_cutoff,
                RECONCILIATION_POLICY_VERSION,
                trusted_manifest["sha256"] if trusted_manifest else None,
            )

            handle = tempfile.NamedTemporaryFile(
                prefix=output_path.name + ".",
                suffix=".tmp",
                dir=output_path.parent,
                delete=False,
            )
            database_temp = Path(handle.name)
            handle.close()
            database_temp.unlink()
            output = sqlite3.connect(database_temp)
            output.row_factory = sqlite3.Row
            try:
                output.executescript(PLAN_DDL)
                output.execute(
                    """
                    INSERT INTO reconciliation_runs(
                        reconciliation_id,tool_version,plan_schema_version,
                        policy_version,baseline_schema_version,
                        final_target_schema_version,artifact_kind,
                        publication_eligibility,project_limit,firm_limit,
                        selected_project_count,selected_firm_count,
                        pending_target_count,deterministic_cutoff,validation_json
                    ) VALUES (?,?,?,?,?,?, 'intermediate_reconciliation_plan',?,?,?,?,?,?,?,?)
                    """,
                    (
                        reconciliation_id,
                        RECONCILIATION_TOOL_VERSION,
                        RECONCILIATION_SCHEMA_VERSION,
                        RECONCILIATION_POLICY_VERSION,
                        BASELINE_SCHEMA_VERSION,
                        FINAL_TARGET_SCHEMA_VERSION,
                        publication_eligibility,
                        project_limit,
                        firm_limit,
                        project_count,
                        firm_count,
                        pending_count,
                        deterministic_cutoff,
                        canonical_json(validation),
                    ),
                )
                if trusted_manifest is not None:
                    output.execute(
                        """
                        INSERT INTO trusted_input_manifest(
                            reconciliation_id,manifest_version,path_label,
                            sha256,size_bytes,manifest_json
                        ) VALUES (?,?,?,?,?,?)
                        """,
                        (
                            reconciliation_id,
                            trusted_manifest["manifest_version"],
                            trusted_manifest["path_label"],
                            trusted_manifest["sha256"],
                            trusted_manifest["size_bytes"],
                            canonical_json(trusted_manifest["payload"]),
                        ),
                    )
                for item in baseline_contract:
                    output.execute(
                        """
                        INSERT INTO baseline_contract_objects(
                            object_type,object_name,sql_sha256,sql_text,columns_json
                        ) VALUES (?,?,?,?,?)
                        """,
                        (
                            item["object_type"],
                            item["object_name"],
                            item["sql_sha256"],
                            item["sql"],
                            canonical_json(item["columns"]),
                        ),
                    )
                metrics = _materialize_entities(
                    output,
                    selected=selected,
                    sidecar_connection=sidecar,
                    raw_projects=raw_projects,
                    raw_firms=raw_firms,
                    baseline_firm_slugs=baseline_firm_slugs,
                    relationship_slugs=relations,
                )
                metrics.update(
                    _materialize_target_reasons(
                        output,
                        sidecar=sidecar,
                        include_unselected=(
                            project_limit is None and firm_limit is None
                        ),
                    )
                )
                for name, value in sorted(metrics.items()):
                    output.execute(
                        "INSERT INTO reconciliation_metrics(metric_name,metric_value_json) VALUES (?,?)",
                        (name, canonical_json(value)),
                    )
                for role, input_item in sorted(input_before.items()):
                    lineage = (
                        validation["baseline_build"]
                        if role == "curated_v1_3"
                        else validation["sidecar_meta"]
                        if role == "recrawl_sidecar"
                        else {"bound_by": "baseline and sidecar source SHA"}
                    )
                    output.execute(
                        """
                        INSERT INTO input_snapshots(
                            reconciliation_id,input_role,path_label,
                            sha256_before,sha256_after,size_bytes,query_only,
                            quick_check,foreign_key_violations,lineage_json
                        ) VALUES (?,?,?,?,?,?,1,?,?,?)
                        """,
                        (
                            reconciliation_id,
                            role,
                            input_item["path_label"],
                            input_item["sha256"],
                            input_item["sha256"],
                            input_item["size_bytes"],
                            input_integrity[role]["quick_check"],
                            input_integrity[role]["foreign_key_violations"],
                            canonical_json(lineage),
                        ),
                    )
                output.commit()
                foreign_keys = output.execute("PRAGMA foreign_key_check").fetchall()
                if foreign_keys:
                    raise ReconciliationError(
                        f"output foreign-key violations: {len(foreign_keys)}"
                    )
                quick = _quick_check(output)
                if quick != "ok":
                    raise ReconciliationError(f"output quick_check failed: {quick}")
                logical_sha = _database_logical_sha(output)
            finally:
                output.close()

            input_after = {
                role: {
                    "sha256": sha256_file(path),
                    "size_bytes": path.stat().st_size,
                }
                for role, path in input_paths.items()
            }
            for role in input_paths:
                if input_after[role] != {
                    "sha256": input_before[role]["sha256"],
                    "size_bytes": input_before[role]["size_bytes"],
                }:
                    raise ReconciliationError(f"input changed during build: {role}")
                _assert_quiescent_input(input_paths[role])
            if trusted_manifest_path is not None:
                manifest_after = {
                    "sha256": sha256_file(trusted_manifest_path),
                    "size_bytes": trusted_manifest_path.stat().st_size,
                }
                if manifest_after != manifest_before:
                    raise ReconciliationError(
                        "trusted input manifest changed during build"
                    )
            database_sha = sha256_file(database_temp)
            result = {
                "reconciliation_id": reconciliation_id,
                "publication_eligibility": publication_eligibility,
                "selected_project_count": project_count,
                "selected_firm_count": firm_count,
                "pending_target_count": pending_count,
                "inputs": input_before,
                "trusted_manifest": trusted_manifest,
                "metrics": metrics,
                "database_sha256": database_sha,
                "logical_sha256": logical_sha,
                "database_size_bytes": database_temp.stat().st_size,
                "baseline_contract_object_count": len(baseline_contract),
                "resource_preflight": resource_preflight,
            }
            report_handle = tempfile.NamedTemporaryFile(
                prefix=report_path.name + ".",
                suffix=".tmp",
                dir=report_path.parent,
                delete=False,
                mode="w",
                encoding="utf-8",
                newline="\n",
            )
            report_temp = Path(report_handle.name)
            report_handle.write(render_report(result))
            report_handle.flush()
            os.fsync(report_handle.fileno())
            report_handle.close()
            report_sha = sha256_file(report_temp)
            ready_handle = tempfile.NamedTemporaryFile(
                prefix=ready_path.name + ".",
                suffix=".tmp",
                dir=ready_path.parent,
                delete=False,
                mode="w",
                encoding="utf-8",
                newline="\n",
            )
            ready_temp = Path(ready_handle.name)
            ready_payload = {
                "artifact_kind": "architizer_reconciliation_plan",
                "ready_version": RECONCILIATION_READY_VERSION,
                "tool_version": RECONCILIATION_TOOL_VERSION,
                "plan_schema_version": RECONCILIATION_SCHEMA_VERSION,
                "policy_version": RECONCILIATION_POLICY_VERSION,
                "baseline_schema_version": BASELINE_SCHEMA_VERSION,
                "final_target_schema_version": FINAL_TARGET_SCHEMA_VERSION,
                "publication_eligibility": publication_eligibility,
                "reconciliation_id": reconciliation_id,
                "project_limit": project_limit,
                "firm_limit": firm_limit,
                "selected_project_count": project_count,
                "selected_firm_count": firm_count,
                "pending_target_count": pending_count,
                "database": {
                    "path": _path_label(output_path),
                    "sha256": database_sha,
                    "logical_sha256": logical_sha,
                    "size_bytes": database_temp.stat().st_size,
                },
                "report": {
                    "path": _path_label(report_path),
                    "sha256": report_sha,
                    "size_bytes": report_temp.stat().st_size,
                },
                "trusted_manifest": (
                    {
                        "path": trusted_manifest["path_label"],
                        "manifest_version": trusted_manifest["manifest_version"],
                        "sha256": trusted_manifest["sha256"],
                        "size_bytes": trusted_manifest["size_bytes"],
                    }
                    if trusted_manifest
                    else None
                ),
                "validation": {
                    "inputs": validation,
                    "output": {
                        "quick_check": quick,
                        "foreign_key_violation_count": 0,
                    },
                },
            }
            ready_handle.write(canonical_json(ready_payload) + "\n")
            ready_handle.flush()
            os.fsync(ready_handle.fileno())
            ready_handle.close()

            def assert_publication_inputs_unchanged() -> None:
                for role, path in input_paths.items():
                    try:
                        actual = {
                            "sha256": sha256_file(path),
                            "size_bytes": path.stat().st_size,
                        }
                    except OSError as exc:
                        raise ReconciliationError(
                            f"input unavailable before READY publication: {role}"
                        ) from exc
                    expected = {
                        "sha256": input_before[role]["sha256"],
                        "size_bytes": input_before[role]["size_bytes"],
                    }
                    if actual != expected:
                        raise ReconciliationError(
                            f"input changed before READY publication: {role}"
                        )
                    _assert_quiescent_input(path)
                if trusted_manifest_path is not None:
                    try:
                        actual_manifest = {
                            "sha256": sha256_file(trusted_manifest_path),
                            "size_bytes": trusted_manifest_path.stat().st_size,
                        }
                    except OSError as exc:
                        raise ReconciliationError(
                            "trusted input manifest unavailable before READY publication"
                        ) from exc
                    if actual_manifest != manifest_before:
                        raise ReconciliationError(
                            "trusted input manifest changed before READY publication"
                        )

            _publish_bundle(
                database_temp,
                report_temp,
                ready_temp,
                output_path,
                report_path,
                ready_path,
                pre_ready_check=assert_publication_inputs_unchanged,
            )
            published = True
            result["output_path"] = str(output_path)
            result["report_path"] = str(report_path)
            result["ready_path"] = str(ready_path)
            result["report_sha256"] = report_sha
            return result
        finally:
            raw.close()
            baseline.close()
            sidecar.close()
            if not published:
                for temporary in (database_temp, report_temp, ready_temp):
                    if temporary is not None:
                        try:
                            temporary.unlink()
                        except FileNotFoundError:
                            pass
            else:
                for temporary in (database_temp, report_temp, ready_temp):
                    if temporary is not None:
                        try:
                            temporary.unlink()
                        except FileNotFoundError:
                            pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build an immutable intermediate Architizer v2 reconciliation plan"
    )
    parser.add_argument("--raw-db", type=Path, default=DEFAULT_RAW_DB)
    parser.add_argument("--baseline-db", type=Path, default=DEFAULT_BASELINE_DB)
    parser.add_argument("--sidecar-db", type=Path, default=DEFAULT_SIDECAR_DB)
    parser.add_argument("--output-db", type=Path, default=DEFAULT_OUTPUT_DB)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--ready-marker",
        type=Path,
        help="Immutable READY JSON path (default: <output-db>.READY.json).",
    )
    parser.add_argument(
        "--trusted-input-manifest",
        type=Path,
        help="Required exact input/run manifest for a full eligible plan.",
    )
    parser.add_argument("--project-limit", type=int)
    parser.add_argument("--firm-limit", type=int)
    parser.add_argument(
        "--confirm-full-reconciliation-plan",
        action="store_true",
        help="Allow a full offline plan only when the sidecar has converged.",
    )
    args = parser.parse_args(argv)
    if (
        args.project_limit is None
        and args.firm_limit is None
        and not args.confirm_full_reconciliation_plan
    ):
        parser.error(
            "use --project-limit/--firm-limit for smoke, or explicitly confirm a full plan"
        )
    if (args.project_limit is None) != (args.firm_limit is None):
        parser.error(
            "limited smoke plans require both --project-limit and --firm-limit"
        )
    if args.confirm_full_reconciliation_plan and args.trusted_input_manifest is None:
        parser.error("full reconciliation requires --trusted-input-manifest")
    try:
        result = build_plan(
            raw_path=args.raw_db,
            baseline_path=args.baseline_db,
            sidecar_path=args.sidecar_db,
            output_path=args.output_db,
            report_path=args.report,
            ready_path=args.ready_marker,
            trusted_manifest_path=args.trusted_input_manifest,
            project_limit=args.project_limit,
            firm_limit=args.firm_limit,
            require_converged=args.confirm_full_reconciliation_plan,
        )
    except (ReconciliationError, sqlite3.Error, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        canonical_json(
            {
                "reconciliation_id": result["reconciliation_id"],
                "artifact_kind": "intermediate_reconciliation_plan",
                "publication_eligibility": result["publication_eligibility"],
                "output": result["output_path"],
                "report": result["report_path"],
                "ready": result["ready_path"],
                "sha256": result["database_sha256"],
                "projects": result["selected_project_count"],
                "firms": result["selected_firm_count"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
