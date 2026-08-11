"""Independent, immutable validator for terminal E3 selection artifacts.

The validator deliberately does not import the E3 builder/pipeline.  It opens
the E3 artifact and its E2 input read-only, reconstructs the sample, candidate
features, P0/P1/P2 rankings, chosen-star direct-pHash suppression, queue
accounting, and logical manifest from frozen contracts, and performs no
network, Vision, LLM, or representative-image work.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from itertools import groupby
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from canonical.cross_source_image_selection import (
    Candidate,
    DirectPHashEdge,
    E3_POLICY_VERSION,
    E3_SAMPLE_POLICY_VERSION,
    E3_SELECTION_VERSION,
    SamplingItem,
    canonical_json,
    canonical_sha256,
    compare_standard_policies,
    deterministic_stratified_sample,
    editorial_sort_key,
    ordered_sample_manifest_sha256,
    policy_definitions,
)
from canonical.cross_source_image_selection_sources import (
    BuildingImageCandidate,
    BuildingSummary,
    E2ArtifactSpec,
    E2SelectionSources,
    SameBuildingDirectPhashEdge,
)


VALIDATOR_VERSION = "archibe-e3-cross-source-image-selection-validator-v2"
LOGICAL_MANIFEST_VERSION = "archibe-e3-selection-logical-manifest-v1"
HASH_CHUNK_SIZE = 8 * 1024 * 1024
MAX_MISMATCH_EXAMPLES = 100
EXPECTED_APPLICATION_ID = int.from_bytes(b"E3IS", "big")
EXPECTED_SCHEMA_VERSION = 1

FORBIDDEN_POLICY_TABLES = frozenset(
    {
        "representatives",
        "representative_images",
        "vision_queue",
        "vision_tasks",
        "final_matches",
        "merge_decisions",
        "semantic_labels",
    }
)

LOGICAL_SELECTION_TABLES = (
    "policy_definitions",
    "population_strata",
    "selected_buildings",
    "image_candidates",
    "policy_rankings",
    "shortlist_items",
    "queue_estimates",
)

EXPECTED_TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "selection_runs": (
        "run_id", "contract_version", "builder_version", "e2_artifact_path",
        "e2_size_bytes", "e2_byte_sha256", "e2_logical_sha256",
        "policy_set_sha256", "selection_mode", "sample_size", "sample_seed",
        "shortlist_size", "ordered_selection_manifest_sha256", "config_json",
        "network_requests", "vision_requests", "llm_requests", "authoritative",
        "artifact_scope", "status", "started_at", "completed_at", "error",
    ),
    "selection_inputs": (
        "run_id", "input_name", "input_role", "file_path", "size_bytes",
        "sha256_before", "sha256_after", "logical_sha256", "application_id",
        "user_version", "schema_manifest_sha256", "recorded_at", "detail_json",
    ),
    "policy_definitions": (
        "run_id", "policy_id", "policy_version", "policy_name", "description",
        "shortlist_size", "enabled", "definition_json", "policy_config_sha256",
        "policy_record_sha256", "created_at",
    ),
    "population_strata": (
        "run_id", "stratum_id", "stratum_key", "stratum_json",
        "population_count", "eligible_count", "selected_building_count",
        "selected_candidate_count", "stratum_record_sha256",
    ),
    "selected_buildings": (
        "run_id", "selection_id", "selection_rank", "stratum_id", "source",
        "entity_type", "source_entity_id", "source_building_id",
        "source_project_id", "name", "normalized_name", "selection_reason",
        "e2_source_record_sha256", "e2_relation_record_sha256",
        "selection_record_sha256", "detail_json",
    ),
    "image_candidates": (
        "run_id", "candidate_id", "selection_id", "source",
        "source_building_id", "source_project_id", "source_asset_id",
        "fingerprint_status", "canonical_url", "fetch_url", "final_url",
        "roles_json", "primary_role", "role_rank", "source_ordinal",
        "ordinal_is_derived", "original_width", "original_height",
        "normalized_width", "normalized_height", "quality_flags_json",
        "low_information", "normalized_pixel_sha256", "exact_cluster_id",
        "phash_node_id", "source_record_sha256", "occurrence_record_sha256",
        "project_relation_record_sha256", "building_relation_record_sha256",
        "candidate_record_sha256", "detail_json",
    ),
    "policy_rankings": (
        "run_id", "policy_id", "policy_version", "policy_config_sha256",
        "selection_id", "candidate_id", "ranking_state", "editorial_rank",
        "shortlist_rank", "selected", "qa_fallback", "hard_risk",
        "rank_tuple_json", "component_scores_json", "reasons_json",
        "suppressed_by_candidate_id", "suppression_reason", "fallback_reason",
        "ranking_record_sha256", "detail_json",
    ),
    "shortlist_items": (
        "run_id", "policy_id", "selection_id", "shortlist_rank",
        "candidate_id", "shortlist_state", "authoritative",
        "item_record_sha256", "rationale_json",
    ),
    "queue_estimates": (
        "run_id", "estimate_id", "policy_id", "stratum_id", "queue_unit",
        "population_count", "estimated_queue_items", "tokens_per_item_low",
        "tokens_per_item_point", "tokens_per_item_high",
        "projected_input_tokens", "projected_output_tokens",
        "projected_total_tokens", "estimated_calls", "retry_factor",
        "estimated_cost_usd", "pricing_snapshot_json", "quota_basis",
        "projected_quota_percent", "requests_executed", "authoritative",
        "estimate_record_sha256", "detail_json", "created_at",
    ),
    "selection_metrics": (
        "run_id", "phase", "metric_name", "stratum_json", "value_integer",
        "value_real", "value_text", "recorded_at",
    ),
    "selection_validations": (
        "run_id", "validation_name", "severity", "passed", "expected",
        "actual", "detail_json", "recorded_at",
    ),
    "build_checkpoints": (
        "run_id", "phase", "cursor_json", "completed_rows",
        "phase_complete", "updated_at",
    ),
}

EXPECTED_INDEX_NAMES = frozenset(
    {
        "idx_selection_inputs_role",
        "idx_policy_definitions_enabled",
        "idx_population_strata_key",
        "idx_selected_buildings_entity",
        "idx_image_candidates_building",
        "idx_image_candidates_asset",
        "idx_image_candidates_exact",
        "idx_image_candidates_phash",
        "idx_policy_rankings_order",
        "idx_policy_rankings_selected",
        "idx_shortlist_items_candidate",
        "idx_queue_estimates_policy",
        "idx_selection_metrics_name",
        "idx_selection_validations_failed",
    }
)

_TERMINAL_TABLES = tuple(
    table for table in EXPECTED_TABLE_COLUMNS if table != "selection_runs"
)
EXPECTED_TRIGGER_NAMES = frozenset(
    {
        "selection_runs_single_run",
        "selection_runs_provenance_immutable",
        "selection_runs_status_transition",
        "selection_runs_terminal_immutable",
        "selection_runs_complete_requires_validations",
        "selection_runs_failed_requires_validation",
        "selection_runs_immutable_delete",
        *(
            f"{table}_terminal_{operation}_guard"
            for table in _TERMINAL_TABLES
            for operation in ("insert", "update", "delete")
        ),
    }
)


@dataclass(frozen=True)
class ValidationCheck:
    name: str
    passed: bool
    expected: Any
    actual: Any
    detail: dict[str, Any]


@dataclass(frozen=True)
class InputFileCheck:
    input_name: str
    path: str
    size_bytes: int | None
    sha256: str | None
    sidecars: tuple[str, ...]
    unchanged_during_read: bool
    passed: bool
    error: str | None = None


@dataclass(frozen=True)
class SelectionValidationReport:
    artifact_path: str
    run_id: str | None
    logical_sha256: str | None
    table_manifests: dict[str, dict[str, Any]]
    input_files: tuple[InputFileCheck, ...]
    checks: tuple[ValidationCheck, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def failed_check_names(self) -> tuple[str, ...]:
        return tuple(check.name for check in self.checks if not check.passed)

    def require_valid(self) -> None:
        if not self.passed:
            raise SelectionValidationError(
                "E3 validation failed: " + ", ".join(self.failed_check_names)
            )


class SelectionValidationError(RuntimeError):
    """Raised by :meth:`SelectionValidationReport.require_valid`."""


@dataclass
class _MismatchCollector:
    """Count every mismatch while retaining only a bounded diagnostic sample."""

    count: int = 0
    examples: list[dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if self.examples is None:
            self.examples = []

    def append(self, value: dict[str, Any]) -> None:
        self.count += 1
        assert self.examples is not None
        if len(self.examples) < MAX_MISMATCH_EXAMPLES:
            self.examples.append(value)

    def __bool__(self) -> bool:
        return self.count > 0


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(HASH_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def sqlite_sidecars(path: Path | str) -> tuple[Path, ...]:
    target = Path(path)
    return tuple(
        candidate
        for suffix in ("-wal", "-shm", "-journal", ".lock")
        if (candidate := Path(str(target) + suffix)).exists()
    )


def open_immutable(path: Path | str) -> sqlite3.Connection:
    uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _add(
    checks: list[ValidationCheck],
    name: str,
    passed: bool,
    expected: Any,
    actual: Any,
    **detail: Any,
) -> None:
    checks.append(ValidationCheck(name, bool(passed), expected, actual, detail))


def _schema_names(connection: sqlite3.Connection, kind: str) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type=? AND name NOT LIKE 'sqlite_%'",
            (kind,),
        )
    }


def _table_columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")'))


def _primary_key_columns(
    connection: sqlite3.Connection, table: str
) -> tuple[str, ...]:
    rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    return tuple(
        str(row[1])
        for row in sorted(
            (row for row in rows if int(row[5]) > 0), key=lambda row: int(row[5])
        )
    )


def _schema_manifest_sha256(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        """
        SELECT type,name,tbl_name,sql FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%'
        ORDER BY type,name
        """
    ).fetchall()
    return canonical_sha256(
        [
            {
                "name": row[1],
                "sql": row[3],
                "table": row[2],
                "type": row[0],
            }
            for row in rows
        ]
    )


def logical_selection_manifest(
    connection: sqlite3.Connection, run_id: str
) -> tuple[str, dict[str, dict[str, Any]]]:
    """Stream the frozen logical manifest independently from the builder."""

    table_manifests: dict[str, dict[str, Any]] = {}
    for table in LOGICAL_SELECTION_TABLES:
        columns = list(_table_columns(connection, table))
        if not columns:
            raise sqlite3.DatabaseError(f"logical table is missing: {table}")
        included = [
            column
            for column in columns
            if column not in {"created_at", "recorded_at", "updated_at"}
        ]
        primary_key = _primary_key_columns(connection, table)
        if not primary_key:
            raise sqlite3.DatabaseError(f"logical table has no primary key: {table}")
        selected = ",".join(f'"{column}"' for column in included)
        ordering = ",".join(f'"{column}"' for column in primary_key)
        digest = hashlib.sha256()
        count = 0
        for row in connection.execute(
            f'SELECT {selected} FROM "{table}" WHERE run_id=? ORDER BY {ordering}',
            (run_id,),
        ):
            record = {column: row[index] for index, column in enumerate(included)}
            digest.update(canonical_json(record).encode("utf-8"))
            digest.update(b"\n")
            count += 1
        table_manifests[table] = {"count": count, "sha256": digest.hexdigest()}
    run = connection.execute(
        """
        SELECT e2_logical_sha256,policy_set_sha256,selection_mode,
               sample_size,sample_seed,shortlist_size,
               ordered_selection_manifest_sha256
        FROM selection_runs WHERE run_id=?
        """,
        (run_id,),
    ).fetchone()
    if run is None:
        raise sqlite3.DatabaseError("logical manifest run is missing")
    body = {
        "e2_logical_sha256": str(run["e2_logical_sha256"]),
        "manifest_version": LOGICAL_MANIFEST_VERSION,
        "ordered_selection_manifest_sha256": str(
            run["ordered_selection_manifest_sha256"]
        ),
        "policy_set_sha256": str(run["policy_set_sha256"]),
        "sample_seed": run["sample_seed"],
        "sample_size": run["sample_size"],
        "selection_mode": str(run["selection_mode"]),
        "shortlist_size": int(run["shortlist_size"]),
        "tables": table_manifests,
    }
    return canonical_sha256(body), table_manifests


def _json(value: object, *, expected: type, label: str) -> Any:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError) as exc:
        raise SelectionValidationError(f"invalid {label}") from exc
    if not isinstance(parsed, expected):
        raise SelectionValidationError(f"{label} has wrong JSON type")
    return parsed


def _sampling_item(summary: BuildingSummary) -> SamplingItem:
    summary_sha = canonical_sha256(
        {
            "cross_source_candidate": summary.cross_source_candidate,
            "name": summary.name,
            "quality_risk_cover_count": summary.quality_risk_cover_count,
            "source": summary.source,
            "source_building_id": summary.source_building_id,
            "source_record_sha256": summary.source_record_sha256,
            "stratum": summary.stratum,
            "successful_asset_count": summary.successful_asset_count,
            "successful_cover_count": summary.successful_cover_count,
        }
    )
    return SamplingItem(
        identity=(
            f"{summary.source}:building:{summary.source_building_id}"
        ),
        source=summary.source,
        stratum=summary.stratum,
        input_record_sha256=summary_sha,
    )


class _OrderedSelectionManifestHasher:
    """Incrementally reproduce ``ordered_sample_manifest_sha256`` exactly."""

    def __init__(self) -> None:
        self._digest = hashlib.sha256()
        self._digest.update(b'{"ordered_items":[')
        self._count = 0

    def add(self, item: SamplingItem) -> None:
        self._count += 1
        if self._count > 1:
            self._digest.update(b",")
        record = {
            "item": item.as_record(),
            "item_record_sha256": item.record_sha256,
            "rank": self._count,
        }
        self._digest.update(canonical_json(record).encode("utf-8"))

    def hexdigest(self) -> str:
        digest = self._digest.copy()
        digest.update(b'],"policy_version":')
        digest.update(canonical_json(E3_SAMPLE_POLICY_VERSION).encode("utf-8"))
        digest.update(b"}")
        return digest.hexdigest()


def _primary_role(roles: Sequence[str]) -> str:
    values = tuple(sorted(set(str(role) for role in roles)))
    if not values:
        raise SelectionValidationError("E2 candidate has no source role")
    if "cover" in values:
        return "cover"
    if "gallery" in values:
        return "gallery"
    return values[0]


def _candidate(source: BuildingImageCandidate) -> Candidate:
    return Candidate(
        source=source.source,
        source_building_id=source.source_building_id,
        source_asset_id=source.source_asset_id,
        fingerprint_status="success",
        role=_primary_role(source.roles),
        ordinal=source.lowest_project_ordinal,
        original_width=source.original_width,
        original_height=source.original_height,
        quality_flags=tuple(source.quality_flags),
        source_record_sha256=source.source_asset_record_sha256,
        exact_cluster_id=source.exact_cluster_id,
        phash_node_id=source.phash_node_id,
        canonical_url=source.canonical_url,
    )


def _normal_name(value: str) -> str:
    return " ".join(str(value).casefold().split())


def _role_rank(role: str) -> int:
    return 0 if role == "cover" else 1 if role == "gallery" else 2


def _iter_rows(
    connection: sqlite3.Connection,
    query: str,
    parameters: Sequence[object] = (),
    *,
    batch_size: int = 1_000,
) -> Iterator[sqlite3.Row]:
    cursor = connection.execute(query, tuple(parameters))
    try:
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                return
            yield from rows
    finally:
        cursor.close()


def _validate_schema(
    connection: sqlite3.Connection, checks: list[ValidationCheck]
) -> bool:
    application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    _add(
        checks,
        "application_id_contract",
        application_id == EXPECTED_APPLICATION_ID,
        EXPECTED_APPLICATION_ID,
        application_id,
    )
    _add(
        checks,
        "schema_version_contract",
        user_version == EXPECTED_SCHEMA_VERSION,
        EXPECTED_SCHEMA_VERSION,
        user_version,
    )
    expected_tables = set(EXPECTED_TABLE_COLUMNS)
    tables = _schema_names(connection, "table")
    views = _schema_names(connection, "view")
    indexes = _schema_names(connection, "index")
    triggers = _schema_names(connection, "trigger")
    _add(
        checks,
        "exact_table_set",
        tables == expected_tables,
        expected_tables,
        tables,
        missing=sorted(expected_tables - tables),
        unexpected=sorted(tables - expected_tables),
    )
    _add(checks, "views_absent", not views, [], sorted(views))
    forbidden = sorted((tables | views) & FORBIDDEN_POLICY_TABLES)
    _add(checks, "forbidden_policy_tables_absent", not forbidden, [], forbidden)
    _add(
        checks,
        "exact_index_set",
        indexes == EXPECTED_INDEX_NAMES,
        EXPECTED_INDEX_NAMES,
        indexes,
        missing=sorted(EXPECTED_INDEX_NAMES - indexes),
        unexpected=sorted(indexes - EXPECTED_INDEX_NAMES),
    )
    _add(
        checks,
        "exact_trigger_set",
        triggers == EXPECTED_TRIGGER_NAMES,
        EXPECTED_TRIGGER_NAMES,
        triggers,
        missing=sorted(EXPECTED_TRIGGER_NAMES - triggers),
        unexpected=sorted(triggers - EXPECTED_TRIGGER_NAMES),
    )
    if tables != expected_tables:
        return False
    mismatches = {
        table: {
            "expected": EXPECTED_TABLE_COLUMNS[table],
            "actual": _table_columns(connection, table),
        }
        for table in sorted(expected_tables)
        if _table_columns(connection, table) != EXPECTED_TABLE_COLUMNS[table]
    }
    _add(checks, "exact_table_columns", not mismatches, {}, mismatches)
    return not mismatches


def _validate_request_policy(
    connection: sqlite3.Connection,
    run: sqlite3.Row,
    checks: list[ValidationCheck],
) -> None:
    counters = {
        "network_requests": int(run["network_requests"]),
        "vision_requests": int(run["vision_requests"]),
        "llm_requests": int(run["llm_requests"]),
    }
    _add(
        checks,
        "run_request_counters_zero",
        all(value == 0 for value in counters.values()),
        {key: 0 for key in counters},
        counters,
    )
    _add(
        checks,
        "candidate_only_non_authoritative",
        str(run["artifact_scope"]) == "candidate_only"
        and int(run["authoritative"]) == 0,
        {"artifact_scope": "candidate_only", "authoritative": 0},
        {
            "artifact_scope": str(run["artifact_scope"]),
            "authoritative": int(run["authoritative"]),
        },
    )
    try:
        config = _json(run["config_json"], expected=dict, label="config_json")
    except SelectionValidationError as exc:
        _add(checks, "candidate_only_config", False, "valid object", str(exc))
        config = {}
    forbidden_truthy = {
        key: config.get(key)
        for key in (
            "creates_final_representative",
            "creates_vision_tasks",
            "network_enabled",
            "semantic_reuse_allowed",
            "phash_transitive_closure_allowed",
        )
        if bool(config.get(key))
    }
    _add(
        checks,
        "candidate_only_config",
        not forbidden_truthy,
        {},
        forbidden_truthy,
        config=config,
    )
    metric_rows = connection.execute(
        """
        SELECT metric_name,value_integer,value_real,value_text
        FROM selection_metrics
        WHERE run_id=? AND metric_name IN
          ('network_requests','vision_requests','llm_requests')
        ORDER BY metric_name,phase,stratum_json
        """,
        (str(run["run_id"]),),
    ).fetchall()
    by_name: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in metric_rows:
        by_name[str(row[0])].append(row)
    metric_actual = {
        name: [
            {
                "value_integer": row[1],
                "value_real": row[2],
                "value_text": row[3],
            }
            for row in rows
        ]
        for name, rows in by_name.items()
    }
    metrics_ok = all(
        len(by_name.get(name, ())) == 1
        and int(by_name[name][0][1]) == 0
        and by_name[name][0][2] is None
        and by_name[name][0][3] is None
        for name in counters
    )
    _add(
        checks,
        "request_metrics_exact_zero",
        metrics_ok,
        {name: [{"value_integer": 0, "value_real": None, "value_text": None}] for name in counters},
        metric_actual,
    )


def _validate_input(
    connection: sqlite3.Connection,
    run: sqlite3.Row,
    checks: list[ValidationCheck],
    *,
    verify_file_hashes: bool,
) -> tuple[tuple[InputFileCheck, ...], E2SelectionSources | None]:
    if not verify_file_hashes:
        raise ValueError("E3 validation cannot disable the E2 byte hash check")
    run_id = str(run["run_id"])
    rows = connection.execute(
        "SELECT * FROM selection_inputs WHERE run_id=? ORDER BY input_name",
        (run_id,),
    ).fetchall()
    e2_rows = [row for row in rows if str(row["input_role"]) == "e2_evidence"]
    _add(
        checks,
        "exactly_one_e2_input",
        len(e2_rows) == 1,
        1,
        len(e2_rows),
    )
    if len(e2_rows) != 1:
        return (), None
    row = e2_rows[0]
    target = Path(str(row["file_path"])).resolve()
    run_path = Path(str(run["e2_artifact_path"])).resolve()
    stored = {
        "path": str(target),
        "size": int(row["size_bytes"]),
        "sha_before": str(row["sha256_before"]),
        "sha_after": None if row["sha256_after"] is None else str(row["sha256_after"]),
        "logical": None if row["logical_sha256"] is None else str(row["logical_sha256"]),
    }
    run_values = {
        "path": str(run_path),
        "size": int(run["e2_size_bytes"]),
        "sha_before": str(run["e2_byte_sha256"]),
        "sha_after": str(run["e2_byte_sha256"]),
        "logical": str(run["e2_logical_sha256"]),
    }
    _add(
        checks,
        "e2_input_matches_run_provenance",
        stored == run_values,
        run_values,
        stored,
    )
    detail: dict[str, Any]
    try:
        detail = _json(row["detail_json"], expected=dict, label="E2 input detail_json")
    except SelectionValidationError:
        detail = {}
    # The source adapter performs the one authoritative streaming byte hash,
    # verifies size/no-sidecars before and after that hash, then opens E2 with
    # mode=ro&immutable=1.  Do not hash a multi-gigabyte E2 input again here.
    size_before = target.stat().st_size if target.is_file() else None
    sidecars_before = tuple(str(item) for item in sqlite_sidecars(target))
    try:
        source = E2SelectionSources(
            E2ArtifactSpec(
                path=target,
                expected_size=int(row["size_bytes"]),
                expected_sha256=str(row["sha256_before"]),
                expected_logical_sha256=str(row["logical_sha256"]),
                expected_contract_version=(
                    str(detail["e2_contract_version"])
                    if detail.get("e2_contract_version") is not None
                    else None
                ),
                expected_builder_version=(
                    str(detail["e2_builder_version"])
                    if detail.get("e2_builder_version") is not None
                    else None
                ),
            )
        )
    except Exception as exc:
        file_check = InputFileCheck(
            input_name=str(row["input_name"]),
            path=str(target),
            size_bytes=size_before,
            sha256=None,
            sidecars=sidecars_before,
            unchanged_during_read=False,
            passed=False,
            error=f"{type(exc).__name__}: {exc}",
        )
        _add(
            checks,
            "e2_current_byte_identity_and_no_sidecars",
            False,
            {
                "size": int(row["size_bytes"]),
                "sha256": str(row["sha256_before"]),
                "sidecars": [],
                "unchanged": True,
            },
            {
                "size": size_before,
                "sha256": None,
                "sidecars": sidecars_before,
                "unchanged": False,
                "error": file_check.error,
            },
        )
        _add(
            checks,
            "e2_immutable_contract_and_stored_logical_sha",
            False,
            "valid complete full E2 source",
            f"{type(exc).__name__}: {exc}",
        )
        return (file_check,), None
    size_after = target.stat().st_size
    sidecars_after = tuple(str(item) for item in sqlite_sidecars(target))
    observed_sha = source.lineage.artifact_sha256
    unchanged = size_before == size_after and not sidecars_before and not sidecars_after
    passed = (
        size_after == int(row["size_bytes"])
        and observed_sha == str(row["sha256_before"])
        and str(row["sha256_after"]) == str(row["sha256_before"])
        and unchanged
    )
    file_check = InputFileCheck(
        input_name=str(row["input_name"]),
        path=str(target),
        size_bytes=size_after,
        sha256=observed_sha,
        sidecars=sidecars_after,
        unchanged_during_read=unchanged,
        passed=passed,
        error=None,
    )
    _add(
        checks,
        "e2_current_byte_identity_and_no_sidecars",
        passed,
        {
            "size": int(row["size_bytes"]),
            "sha256": str(row["sha256_before"]),
            "sidecars": [],
            "unchanged": True,
        },
        {
            "size": size_after,
            "sha256": observed_sha,
            "sidecars": sidecars_after,
            "unchanged": unchanged,
            "error": None,
        },
    )
    lineage_actual = {
        "e2_run_id": source.lineage.run_id,
        "e2_contract_version": source.lineage.contract_version,
        "e2_builder_version": source.lineage.builder_version,
        "e2_selection_mode": source.lineage.selection_mode,
        "e2_ordered_selection_manifest_sha256": source.lineage.ordered_selection_manifest_sha256,
        "e2_logical_sha256": source.lineage.stored_logical_sha256,
    }
    lineage_expected = {key: detail.get(key) for key in lineage_actual}
    lineage_ok = (
        source.lineage.selection_mode == "full"
        and source.lineage.stored_logical_sha256 == str(row["logical_sha256"])
        and all(key in detail for key in lineage_actual)
        and all(lineage_actual[key] == value for key, value in lineage_expected.items())
    )
    _add(
        checks,
        "e2_immutable_contract_and_stored_logical_sha",
        lineage_ok,
        {
            "selection_mode": "full",
            "stored_logical_sha256": str(row["logical_sha256"]),
            **lineage_expected,
        },
        lineage_actual,
    )
    schema_actual = {
        "application_id": int(
            source.connection.execute("PRAGMA application_id").fetchone()[0]
        ),
        "user_version": int(
            source.connection.execute("PRAGMA user_version").fetchone()[0]
        ),
        "schema_manifest_sha256": _schema_manifest_sha256(source.connection),
    }
    schema_expected = {
        "application_id": row["application_id"],
        "user_version": row["user_version"],
        "schema_manifest_sha256": row["schema_manifest_sha256"],
    }
    _add(
        checks,
        "e2_schema_lineage",
        schema_actual == schema_expected,
        schema_actual,
        schema_expected,
    )
    return (file_check,), source


def _policy_record_body(row: sqlite3.Row, definition: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "definition": dict(definition),
        "description": row["description"],
        "enabled": bool(row["enabled"]),
        "policy_config_sha256": str(row["policy_config_sha256"]),
        "policy_id": str(row["policy_id"]),
        "policy_name": str(row["policy_name"]),
        "policy_version": str(row["policy_version"]),
        "shortlist_size": int(row["shortlist_size"]),
    }


def _policy_set_sha(policy_rows: Sequence[sqlite3.Row]) -> str:
    return canonical_sha256(
        {
            "contract_version": E3_SELECTION_VERSION,
            "ordered_policies": [
                {
                    "policy_config_sha256": str(row["policy_config_sha256"]),
                    "policy_id": str(row["policy_id"]),
                    "policy_record_sha256": str(row["policy_record_sha256"]),
                }
                for row in sorted(policy_rows, key=lambda value: str(value["policy_id"]))
            ],
        }
    )


def _stratum_record_body(row: sqlite3.Row, payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "eligible_count": int(row["eligible_count"]),
        "population_count": int(row["population_count"]),
        "selected_building_count": int(row["selected_building_count"]),
        "selected_candidate_count": int(row["selected_candidate_count"]),
        "stratum": dict(payload),
        "stratum_id": str(row["stratum_id"]),
        "stratum_key": str(row["stratum_key"]),
    }


def _selection_record_body(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "e2_relation_record_sha256": row["e2_relation_record_sha256"],
        "e2_source_record_sha256": str(row["e2_source_record_sha256"]),
        "entity_type": str(row["entity_type"]),
        "name": row["name"],
        "normalized_name": row["normalized_name"],
        "selection_id": str(row["selection_id"]),
        "selection_rank": int(row["selection_rank"]),
        "selection_reason": str(row["selection_reason"]),
        "source": str(row["source"]),
        "source_building_id": row["source_building_id"],
        "source_entity_id": str(row["source_entity_id"]),
        "source_project_id": row["source_project_id"],
        "stratum_id": str(row["stratum_id"]),
    }


def _shortlist_record_body(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "authoritative": bool(row["authoritative"]),
        "candidate_id": str(row["candidate_id"]),
        "policy_id": str(row["policy_id"]),
        "selection_id": str(row["selection_id"]),
        "shortlist_rank": int(row["shortlist_rank"]),
        "shortlist_state": str(row["shortlist_state"]),
    }


def _queue_record_body(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "authoritative": bool(row["authoritative"]),
        "estimated_calls": row["estimated_calls"],
        "estimated_cost_usd": row["estimated_cost_usd"],
        "estimated_queue_items": int(row["estimated_queue_items"]),
        "estimate_id": str(row["estimate_id"]),
        "policy_id": str(row["policy_id"]),
        "population_count": int(row["population_count"]),
        "pricing_snapshot": _json(
            row["pricing_snapshot_json"], expected=dict, label="pricing_snapshot_json"
        ),
        "projected_input_tokens": row["projected_input_tokens"],
        "projected_output_tokens": row["projected_output_tokens"],
        "projected_quota_percent": row["projected_quota_percent"],
        "projected_total_tokens": row["projected_total_tokens"],
        "queue_unit": str(row["queue_unit"]),
        "quota_basis": row["quota_basis"],
        "requests_executed": int(row["requests_executed"]),
        "retry_factor": float(row["retry_factor"]),
        "stratum_id": row["stratum_id"],
        "tokens_per_item_high": row["tokens_per_item_high"],
        "tokens_per_item_low": row["tokens_per_item_low"],
        "tokens_per_item_point": row["tokens_per_item_point"],
    }


def _validate_policies(
    connection: sqlite3.Connection,
    run: sqlite3.Row,
    checks: list[ValidationCheck],
) -> None:
    run_id = str(run["run_id"])
    rows = connection.execute(
        "SELECT * FROM policy_definitions WHERE run_id=? ORDER BY policy_id",
        (run_id,),
    ).fetchall()
    expected = {
        policy.policy_id: policy
        for policy in policy_definitions(int(run["shortlist_size"]))
    }
    actual_ids = {str(row["policy_id"]) for row in rows}
    _add(
        checks,
        "exact_enabled_policy_set",
        actual_ids == set(expected) and all(int(row["enabled"]) == 1 for row in rows),
        sorted(expected),
        sorted(actual_ids),
        enabled={str(row["policy_id"]): int(row["enabled"]) for row in rows},
    )
    mismatches: list[dict[str, Any]] = []
    for row in rows:
        policy_id = str(row["policy_id"])
        policy = expected.get(policy_id)
        try:
            definition = _json(
                row["definition_json"], expected=dict, label="definition_json"
            )
        except SelectionValidationError as exc:
            mismatches.append({"policy_id": policy_id, "error": str(exc)})
            continue
        expected_sha = canonical_sha256(_policy_record_body(row, definition))
        row_mismatch: dict[str, Any] = {}
        if policy is None:
            row_mismatch["unknown_policy"] = True
        else:
            expected_values = {
                "policy_version": E3_POLICY_VERSION,
                "shortlist_size": policy.shortlist_size,
                "definition": policy.as_config(),
                "policy_config_sha256": policy.config_sha256,
                "description": policy.description,
                "policy_name": policy.policy_id,
            }
            actual_values = {
                "policy_version": str(row["policy_version"]),
                "shortlist_size": int(row["shortlist_size"]),
                "definition": definition,
                "policy_config_sha256": str(row["policy_config_sha256"]),
                "description": row["description"],
                "policy_name": str(row["policy_name"]),
            }
            if expected_values != actual_values:
                row_mismatch["expected_values"] = expected_values
                row_mismatch["actual_values"] = actual_values
        if str(row["policy_record_sha256"]) != expected_sha:
            row_mismatch["expected_record_sha256"] = expected_sha
            row_mismatch["actual_record_sha256"] = str(row["policy_record_sha256"])
        if row_mismatch:
            row_mismatch["policy_id"] = policy_id
            mismatches.append(row_mismatch)
    _add(checks, "policy_configs_and_record_hashes", not mismatches, [], mismatches[:100])
    expected_set_sha = _policy_set_sha(rows)
    _add(
        checks,
        "policy_set_sha256",
        str(run["policy_set_sha256"]) == expected_set_sha,
        expected_set_sha,
        str(run["policy_set_sha256"]),
    )


@dataclass
class _PopulationState:
    selected: tuple[BuildingSummary, ...]
    selected_rows: dict[tuple[str, str], sqlite3.Row]
    stratum_rows: dict[tuple[str, str], sqlite3.Row]
    selected_candidate_counts: Counter[tuple[str, str]]


@dataclass(frozen=True)
class _FullPopulationState:
    population_count: int
    population_counts: Counter[tuple[str, str]]
    eligible_counts: Counter[tuple[str, str]]
    last_key: tuple[str, str] | None
    ordered_manifest_sha256: str
    stratum_rows: dict[tuple[str, str], sqlite3.Row]


@dataclass(frozen=True)
class _FullCandidateState:
    candidate_count: int
    completed_buildings: int
    direct_edge_rows: int
    last_key: tuple[str, str] | None
    ranking_rows: int
    shortlist_rows: int


def _validate_population_and_selection(
    connection: sqlite3.Connection,
    run: sqlite3.Row,
    source: E2SelectionSources,
    checks: list[ValidationCheck],
) -> _PopulationState:
    run_id = str(run["run_id"])
    summaries = tuple(source.iter_building_summaries())
    eligible = tuple(summary for summary in summaries if summary.successful_asset_count > 0)
    sample_items = tuple(_sampling_item(summary) for summary in summaries)
    summary_by_identity = {
        item.identity: summary for item, summary in zip(sample_items, summaries)
    }
    if str(run["selection_mode"]) == "sample":
        selected_items = deterministic_stratified_sample(
            sample_items,
            sample_size=int(run["sample_size"]),
            seed=str(run["sample_seed"]),
        )
    else:
        selected_items = sample_items
    expected_manifest = ordered_sample_manifest_sha256(selected_items)
    _add(
        checks,
        "ordered_selection_manifest",
        str(run["ordered_selection_manifest_sha256"]) == expected_manifest,
        expected_manifest,
        str(run["ordered_selection_manifest_sha256"]),
        selected_count=len(selected_items),
    )
    selected = tuple(summary_by_identity[item.identity] for item in selected_items)

    rows = connection.execute(
        "SELECT * FROM selected_buildings WHERE run_id=? ORDER BY selection_rank",
        (run_id,),
    ).fetchall()
    mismatches: list[dict[str, Any]] = []
    if len(rows) != len(selected):
        mismatches.append(
            {"field": "row_count", "expected": len(selected), "actual": len(rows)}
        )
    selected_rows: dict[tuple[str, str], sqlite3.Row] = {}
    for rank, (item, summary) in enumerate(zip(selected_items, selected), 1):
        if rank > len(rows):
            break
        row = rows[rank - 1]
        expected_values = {
            "selection_id": item.identity,
            "selection_rank": rank,
            "source": summary.source,
            "entity_type": "building",
            "source_entity_id": summary.source_building_id,
            "source_building_id": summary.source_building_id,
            "source_project_id": None,
            "name": summary.name,
            "normalized_name": _normal_name(summary.name),
            "e2_source_record_sha256": summary.source_record_sha256,
            "e2_relation_record_sha256": None,
            "selection_reason": "deterministic_stratified_sample",
        }
        actual_values = {key: row[key] for key in expected_values}
        actual_values["selection_rank"] = int(actual_values["selection_rank"])
        row_mismatch: dict[str, Any] = {}
        if expected_values != actual_values:
            row_mismatch.update(
                {"expected": expected_values, "actual": actual_values}
            )
        expected_record_sha = canonical_sha256(_selection_record_body(row))
        if str(row["selection_record_sha256"]) != expected_record_sha:
            row_mismatch["expected_record_sha256"] = expected_record_sha
            row_mismatch["actual_record_sha256"] = str(
                row["selection_record_sha256"]
            )
        if row_mismatch:
            row_mismatch.update({"rank": rank, "selection_id": item.identity})
            mismatches.append(row_mismatch)
        selected_rows[(summary.source, summary.source_building_id)] = row
    _add(
        checks,
        "selected_building_sample_source_and_hashes",
        not mismatches,
        [],
        mismatches[:100],
        mismatch_count=len(mismatches),
    )

    population_counts = Counter((value.source, value.stratum) for value in summaries)
    eligible_counts = Counter((value.source, value.stratum) for value in eligible)
    selected_counts = Counter((value.source, value.stratum) for value in selected)
    strata = connection.execute(
        "SELECT * FROM population_strata WHERE run_id=? ORDER BY stratum_key",
        (run_id,),
    ).fetchall()
    stratum_rows: dict[tuple[str, str], sqlite3.Row] = {}
    stratum_mismatches: list[dict[str, Any]] = []
    for row in strata:
        try:
            payload = _json(row["stratum_json"], expected=dict, label="stratum_json")
        except SelectionValidationError as exc:
            stratum_mismatches.append(
                {"stratum_id": str(row["stratum_id"]), "error": str(exc)}
            )
            continue
        cell = (str(payload.get("source", "")), str(payload.get("stratum", "")))
        if not all(cell) or cell in stratum_rows:
            stratum_mismatches.append(
                {"stratum_id": str(row["stratum_id"]), "invalid_cell": cell}
            )
            continue
        stratum_rows[cell] = row
        expected_payload = {"source": cell[0], "stratum": cell[1]}
        if payload != expected_payload:
            stratum_mismatches.append(
                {
                    "stratum_id": str(row["stratum_id"]),
                    "expected_stratum_json": expected_payload,
                    "actual_stratum_json": payload,
                }
            )
        expected_counts = {
            "population_count": population_counts[cell],
            "eligible_count": eligible_counts[cell],
            "selected_building_count": selected_counts[cell],
        }
        actual_counts = {
            key: int(row[key]) for key in expected_counts
        }
        row_mismatch: dict[str, Any] = {}
        if expected_counts != actual_counts:
            row_mismatch.update({"expected": expected_counts, "actual": actual_counts})
        expected_key = f"{cell[0]}:{cell[1]}"
        if str(row["stratum_key"]) != expected_key:
            row_mismatch["expected_stratum_key"] = expected_key
            row_mismatch["actual_stratum_key"] = str(row["stratum_key"])
        expected_id = "e3str_" + canonical_sha256(
            {
                "source": cell[0],
                "stratum": cell[1],
                "version": E3_SELECTION_VERSION,
            }
        )
        if str(row["stratum_id"]) != expected_id:
            row_mismatch["expected_stratum_id"] = expected_id
            row_mismatch["actual_stratum_id"] = str(row["stratum_id"])
        expected_sha = canonical_sha256(_stratum_record_body(row, payload))
        if str(row["stratum_record_sha256"]) != expected_sha:
            row_mismatch["expected_record_sha256"] = expected_sha
            row_mismatch["actual_record_sha256"] = str(row["stratum_record_sha256"])
        if row_mismatch:
            row_mismatch["cell"] = cell
            stratum_mismatches.append(row_mismatch)
    expected_cells = set(population_counts)
    if set(stratum_rows) != expected_cells:
        stratum_mismatches.append(
            {
                "field": "cell_set",
                "missing": sorted(expected_cells - set(stratum_rows)),
                "unexpected": sorted(set(stratum_rows) - expected_cells),
            }
        )
    selected_summary_by_key = {
        (value.source, value.source_building_id): value for value in selected
    }
    for key, row in selected_rows.items():
        summary = selected_summary_by_key[key]
        expected_stratum = stratum_rows.get((summary.source, summary.stratum))
        if expected_stratum is None or str(row["stratum_id"]) != str(
            expected_stratum["stratum_id"]
        ):
            stratum_mismatches.append(
                {
                    "selection_id": str(row["selection_id"]),
                    "expected_stratum_id": (
                        None if expected_stratum is None else str(expected_stratum["stratum_id"])
                    ),
                    "actual_stratum_id": str(row["stratum_id"]),
                }
            )
    _add(
        checks,
        "population_eligibility_sample_quotas",
        not stratum_mismatches,
        [],
        stratum_mismatches[:100],
        population=len(summaries),
        eligible=len(eligible),
        selected=len(selected),
    )
    return _PopulationState(
        selected=selected,
        selected_rows=selected_rows,
        stratum_rows=stratum_rows,
        selected_candidate_counts=Counter(),
    )


def _validate_full_population_and_selection(
    connection: sqlite3.Connection,
    run: sqlite3.Row,
    source: E2SelectionSources,
    checks: list[ValidationCheck],
) -> _FullPopulationState:
    """Validate a full population with O(strata) memory and streaming rows."""

    run_id = str(run["run_id"])
    selected_rows = _iter_rows(
        connection,
        "SELECT * FROM selected_buildings WHERE run_id=? ORDER BY selection_rank",
        (run_id,),
    )
    selected_iterator = iter(selected_rows)
    selected_row = next(selected_iterator, None)
    manifest = _OrderedSelectionManifestHasher()
    population_counts: Counter[tuple[str, str]] = Counter()
    eligible_counts: Counter[tuple[str, str]] = Counter()
    mismatches = _MismatchCollector()
    population_count = 0
    last_key: tuple[str, str] | None = None

    for rank, summary in enumerate(source.iter_building_summaries(), 1):
        population_count = rank
        last_key = (summary.source, summary.source_building_id)
        item = _sampling_item(summary)
        manifest.add(item)
        cell = (summary.source, summary.stratum)
        population_counts[cell] += 1
        if summary.successful_asset_count > 0:
            eligible_counts[cell] += 1
        if selected_row is None:
            mismatches.append(
                {
                    "rank": rank,
                    "selection_id": item.identity,
                    "error": "missing selected_buildings row",
                }
            )
            continue
        expected_values = {
            "selection_id": item.identity,
            "selection_rank": rank,
            "source": summary.source,
            "entity_type": "building",
            "source_entity_id": summary.source_building_id,
            "source_building_id": summary.source_building_id,
            "source_project_id": None,
            "name": summary.name,
            "normalized_name": _normal_name(summary.name),
            "e2_source_record_sha256": summary.source_record_sha256,
            "e2_relation_record_sha256": None,
            "selection_reason": "full_population",
        }
        actual_values = {key: selected_row[key] for key in expected_values}
        actual_values["selection_rank"] = int(actual_values["selection_rank"])
        row_mismatch: dict[str, Any] = {}
        if expected_values != actual_values:
            row_mismatch.update({"expected": expected_values, "actual": actual_values})
        expected_stratum_id = "e3str_" + canonical_sha256(
            {
                "source": summary.source,
                "stratum": summary.stratum,
                "version": E3_SELECTION_VERSION,
            }
        )
        if str(selected_row["stratum_id"]) != expected_stratum_id:
            row_mismatch["expected_stratum_id"] = expected_stratum_id
            row_mismatch["actual_stratum_id"] = str(selected_row["stratum_id"])
        expected_record_sha = canonical_sha256(_selection_record_body(selected_row))
        if str(selected_row["selection_record_sha256"]) != expected_record_sha:
            row_mismatch["expected_record_sha256"] = expected_record_sha
            row_mismatch["actual_record_sha256"] = str(
                selected_row["selection_record_sha256"]
            )
        if row_mismatch:
            row_mismatch.update({"rank": rank, "selection_id": item.identity})
            mismatches.append(row_mismatch)
        selected_row = next(selected_iterator, None)

    extra_selected = 0
    while selected_row is not None:
        extra_selected += 1
        if extra_selected <= MAX_MISMATCH_EXAMPLES:
            mismatches.append(
                {
                    "selection_id": str(selected_row["selection_id"]),
                    "selection_rank": int(selected_row["selection_rank"]),
                    "error": "unexpected selected_buildings row",
                }
            )
        else:
            mismatches.count += 1
        selected_row = next(selected_iterator, None)

    expected_manifest = manifest.hexdigest()
    _add(
        checks,
        "ordered_selection_manifest",
        str(run["ordered_selection_manifest_sha256"]) == expected_manifest,
        expected_manifest,
        str(run["ordered_selection_manifest_sha256"]),
        selected_count=population_count,
        streaming=True,
    )
    _add(
        checks,
        "selected_building_full_population_source_and_hashes",
        not mismatches,
        [],
        mismatches.examples,
        mismatch_count=mismatches.count,
        population_count=population_count,
    )

    strata = connection.execute(
        "SELECT * FROM population_strata WHERE run_id=? ORDER BY stratum_key",
        (run_id,),
    ).fetchall()
    stratum_rows: dict[tuple[str, str], sqlite3.Row] = {}
    stratum_mismatches: list[dict[str, Any]] = []
    for row in strata:
        try:
            payload = _json(row["stratum_json"], expected=dict, label="stratum_json")
        except SelectionValidationError as exc:
            stratum_mismatches.append(
                {"stratum_id": str(row["stratum_id"]), "error": str(exc)}
            )
            continue
        cell = (str(payload.get("source", "")), str(payload.get("stratum", "")))
        if not all(cell) or cell in stratum_rows:
            stratum_mismatches.append(
                {"stratum_id": str(row["stratum_id"]), "invalid_cell": cell}
            )
            continue
        stratum_rows[cell] = row
        expected_payload = {"source": cell[0], "stratum": cell[1]}
        expected_counts = {
            "population_count": population_counts[cell],
            "eligible_count": eligible_counts[cell],
            "selected_building_count": population_counts[cell],
        }
        actual_counts = {key: int(row[key]) for key in expected_counts}
        row_mismatch: dict[str, Any] = {}
        if payload != expected_payload:
            row_mismatch["expected_stratum_json"] = expected_payload
            row_mismatch["actual_stratum_json"] = payload
        if expected_counts != actual_counts:
            row_mismatch["expected"] = expected_counts
            row_mismatch["actual"] = actual_counts
        expected_key = f"{cell[0]}:{cell[1]}"
        if str(row["stratum_key"]) != expected_key:
            row_mismatch["expected_stratum_key"] = expected_key
            row_mismatch["actual_stratum_key"] = str(row["stratum_key"])
        expected_id = "e3str_" + canonical_sha256(
            {"source": cell[0], "stratum": cell[1], "version": E3_SELECTION_VERSION}
        )
        if str(row["stratum_id"]) != expected_id:
            row_mismatch["expected_stratum_id"] = expected_id
            row_mismatch["actual_stratum_id"] = str(row["stratum_id"])
        expected_sha = canonical_sha256(_stratum_record_body(row, payload))
        if str(row["stratum_record_sha256"]) != expected_sha:
            row_mismatch["expected_record_sha256"] = expected_sha
            row_mismatch["actual_record_sha256"] = str(row["stratum_record_sha256"])
        if row_mismatch:
            row_mismatch["cell"] = cell
            stratum_mismatches.append(row_mismatch)
    expected_cells = set(population_counts)
    if set(stratum_rows) != expected_cells:
        stratum_mismatches.append(
            {
                "field": "cell_set",
                "missing": sorted(expected_cells - set(stratum_rows)),
                "unexpected": sorted(set(stratum_rows) - expected_cells),
            }
        )
    _add(
        checks,
        "population_eligibility_full_accounting",
        not stratum_mismatches,
        [],
        stratum_mismatches[:MAX_MISMATCH_EXAMPLES],
        population=population_count,
        eligible=sum(eligible_counts.values()),
        selected=population_count,
        streaming=True,
    )
    return _FullPopulationState(
        population_count=population_count,
        population_counts=population_counts,
        eligible_counts=eligible_counts,
        last_key=last_key,
        ordered_manifest_sha256=expected_manifest,
        stratum_rows=stratum_rows,
    )


def _candidate_expected_values(
    mapped: Candidate,
    source: BuildingImageCandidate,
    selection_id: str,
) -> dict[str, Any]:
    values = {
        "candidate_id": mapped.candidate_id,
        "selection_id": selection_id,
        "source": source.source,
        "source_building_id": source.source_building_id,
        "source_project_id": None,
        "source_asset_id": source.source_asset_id,
        "fingerprint_status": "success",
        "canonical_url": source.canonical_url,
        "fetch_url": source.fetch_url,
        "final_url": source.final_url,
        "roles_json": list(source.roles),
        "primary_role": mapped.role,
        "role_rank": _role_rank(mapped.role),
        "source_ordinal": source.lowest_project_ordinal,
        "ordinal_is_derived": int(source.lowest_project_ordinal is not None),
        "original_width": source.original_width,
        "original_height": source.original_height,
        "normalized_width": source.normalized_width,
        "normalized_height": source.normalized_height,
        "quality_flags_json": list(source.quality_flags),
        "low_information": int("low_information" in source.quality_flags),
        "normalized_pixel_sha256": source.normalized_pixel_sha256,
        "exact_cluster_id": source.exact_cluster_id,
        "phash_node_id": source.phash_node_id,
        "source_record_sha256": source.source_asset_record_sha256,
        "occurrence_record_sha256": None,
        "project_relation_record_sha256": None,
        "building_relation_record_sha256": source.building_relation_record_sha256,
    }
    values["candidate_record_sha256"] = canonical_sha256(
        _candidate_mapping_record_body(values)
    )
    return values


def _candidate_mapping_record_body(values: Mapping[str, Any]) -> dict[str, Any]:
    """Freeze the full persisted E2-to-E3 candidate mapping hash body."""

    return {
        key: values[key]
        for key in (
            "building_relation_record_sha256",
            "candidate_id",
            "canonical_url",
            "exact_cluster_id",
            "fetch_url",
            "final_url",
            "fingerprint_status",
            "low_information",
            "normalized_height",
            "normalized_pixel_sha256",
            "normalized_width",
            "occurrence_record_sha256",
            "ordinal_is_derived",
            "original_height",
            "original_width",
            "phash_node_id",
            "primary_role",
            "project_relation_record_sha256",
            "quality_flags_json",
            "role_rank",
            "roles_json",
            "selection_id",
            "source",
            "source_asset_id",
            "source_building_id",
            "source_ordinal",
            "source_project_id",
            "source_record_sha256",
        )
    }


def _ranking_state(reasons: Sequence[str], selected: bool) -> str:
    if selected:
        return "shortlisted"
    if any(str(reason).startswith("suppressed_") for reason in reasons):
        return "suppressed"
    if any(
        str(reason).startswith("excluded_non_success")
        or str(reason).startswith("excluded_quality_hard_risk")
        for reason in reasons
    ):
        return "ineligible"
    return "ranked"


def _validate_candidate_rows(
    connection: sqlite3.Connection,
    run_id: str,
    selection_id: str,
    source_rows: Sequence[BuildingImageCandidate],
    checks_accumulator: list[dict[str, Any]],
) -> tuple[Candidate, ...]:
    mapped = tuple(_candidate(value) for value in source_rows)
    rows = connection.execute(
        """
        SELECT * FROM image_candidates
        WHERE run_id=? AND selection_id=? ORDER BY source_asset_id,candidate_id
        """,
        (run_id, selection_id),
    ).fetchall()
    expected_order = sorted(
        zip(mapped, source_rows), key=lambda value: (value[1].source_asset_id, value[0].candidate_id)
    )
    if len(rows) != len(expected_order):
        checks_accumulator.append(
            {
                "selection_id": selection_id,
                "field": "candidate_count",
                "expected": len(expected_order),
                "actual": len(rows),
            }
        )
    for row, (candidate, source) in zip(rows, expected_order):
        expected = _candidate_expected_values(candidate, source, selection_id)
        actual: dict[str, Any] = {}
        parse_error: str | None = None
        for key in expected:
            if key in {"roles_json", "quality_flags_json"}:
                try:
                    actual[key] = _json(row[key], expected=list, label=key)
                except SelectionValidationError as exc:
                    parse_error = str(exc)
                    actual[key] = None
            else:
                actual[key] = row[key]
        for integer_key in (
            "role_rank",
            "ordinal_is_derived",
            "original_width",
            "original_height",
            "normalized_width",
            "normalized_height",
            "low_information",
        ):
            if actual[integer_key] is not None:
                actual[integer_key] = int(actual[integer_key])
        row_mismatch: dict[str, Any] = {}
        if expected != actual:
            row_mismatch.update({"expected": expected, "actual": actual})
        try:
            detail = _json(row["detail_json"], expected=dict, label="candidate detail_json")
        except SelectionValidationError as exc:
            detail = {}
            row_mismatch["detail_error"] = str(exc)
        if detail.get("phash_hex") != source.phash_hex:
            row_mismatch["expected_phash_hex"] = source.phash_hex
            row_mismatch["actual_phash_hex"] = detail.get("phash_hex")
        if detail.get("ranking_feature_record_sha256") != candidate.record_sha256:
            row_mismatch["expected_ranking_feature_record_sha256"] = (
                candidate.record_sha256
            )
            row_mismatch["actual_ranking_feature_record_sha256"] = detail.get(
                "ranking_feature_record_sha256"
            )
        if parse_error:
            row_mismatch["parse_error"] = parse_error
        if row_mismatch:
            row_mismatch.update(
                {
                    "selection_id": selection_id,
                    "candidate_id": candidate.candidate_id,
                }
            )
            checks_accumulator.append(row_mismatch)
    return mapped


def _validate_ranking_rows(
    connection: sqlite3.Connection,
    run_id: str,
    selection_id: str,
    candidates: Sequence[Candidate],
    direct_edges: Sequence[DirectPHashEdge],
    shortlist_size: int,
    mismatches: list[dict[str, Any]],
) -> None:
    if candidates:
        expected_shortlists = compare_standard_policies(
            candidates,
            shortlist_size=shortlist_size,
            direct_phash_edges=direct_edges,
        )
    else:
        expected_shortlists = ()
    rows = connection.execute(
        """
        SELECT * FROM policy_rankings
        WHERE run_id=? AND selection_id=?
        ORDER BY policy_id,editorial_rank,candidate_id
        """,
        (run_id, selection_id),
    ).fetchall()
    expected_evaluations = [
        (shortlist.policy, evaluation)
        for shortlist in expected_shortlists
        for evaluation in shortlist.evaluations
    ]
    expected_evaluations.sort(
        key=lambda value: (
            value[0].policy_id,
            value[1].editorial_rank,
            value[1].candidate_id,
        )
    )
    if len(rows) != len(expected_evaluations):
        mismatches.append(
            {
                "selection_id": selection_id,
                "field": "ranking_count",
                "expected": len(expected_evaluations),
                "actual": len(rows),
            }
        )
    by_candidate = {candidate.candidate_id: candidate for candidate in candidates}
    for row, (policy, evaluation) in zip(rows, expected_evaluations):
        candidate = by_candidate[evaluation.candidate_id]
        suppression_reason = next(
            (
                reason
                for reason in evaluation.reasons
                if reason.startswith("suppressed_")
            ),
            None,
        )
        expected = {
            "policy_id": policy.policy_id,
            "policy_version": E3_POLICY_VERSION,
            "policy_config_sha256": policy.config_sha256,
            "selection_id": selection_id,
            "candidate_id": evaluation.candidate_id,
            "ranking_state": _ranking_state(evaluation.reasons, evaluation.selected),
            "editorial_rank": evaluation.editorial_rank,
            "shortlist_rank": evaluation.shortlist_rank,
            "selected": int(evaluation.selected),
            "qa_fallback": int(evaluation.qa_fallback),
            "hard_risk": int(evaluation.hard_risk),
            "rank_tuple_json": list(editorial_sort_key(candidate)),
            "component_scores_json": dict(evaluation.component_scores),
            "reasons_json": list(evaluation.reasons),
            "suppressed_by_candidate_id": evaluation.suppressed_by_candidate_id,
            "suppression_reason": suppression_reason,
            "fallback_reason": (
                "all_successful_candidates_hard_risk"
                if evaluation.qa_fallback
                else None
            ),
            "ranking_record_sha256": evaluation.record_sha256,
        }
        actual: dict[str, Any] = {}
        parse_errors: list[str] = []
        for key in expected:
            if key == "rank_tuple_json" or key == "reasons_json":
                try:
                    actual[key] = _json(row[key], expected=list, label=key)
                except SelectionValidationError as exc:
                    actual[key] = None
                    parse_errors.append(str(exc))
            elif key == "component_scores_json":
                try:
                    actual[key] = _json(row[key], expected=dict, label=key)
                except SelectionValidationError as exc:
                    actual[key] = None
                    parse_errors.append(str(exc))
            else:
                actual[key] = row[key]
        for integer_key in (
            "editorial_rank",
            "shortlist_rank",
            "selected",
            "qa_fallback",
            "hard_risk",
        ):
            if actual[integer_key] is not None:
                actual[integer_key] = int(actual[integer_key])
        if expected != actual or parse_errors:
            mismatches.append(
                {
                    "selection_id": selection_id,
                    "policy_id": policy.policy_id,
                    "candidate_id": evaluation.candidate_id,
                    "expected": expected,
                    "actual": actual,
                    "parse_errors": parse_errors,
                }
            )

    shortlist_rows = connection.execute(
        """
        SELECT * FROM shortlist_items
        WHERE run_id=? AND selection_id=?
        ORDER BY policy_id,shortlist_rank
        """,
        (run_id, selection_id),
    ).fetchall()
    expected_items: list[dict[str, Any]] = []
    for shortlist in expected_shortlists:
        evaluations = {value.candidate_id: value for value in shortlist.evaluations}
        for rank, candidate_id in enumerate(shortlist.selected_candidate_ids, 1):
            evaluation = evaluations[candidate_id]
            expected_items.append(
                {
                    "policy_id": shortlist.policy.policy_id,
                    "selection_id": selection_id,
                    "shortlist_rank": rank,
                    "candidate_id": candidate_id,
                    "shortlist_state": (
                        "qa_fallback" if evaluation.qa_fallback else "primary"
                    ),
                    "authoritative": 0,
                }
            )
    expected_items.sort(key=lambda value: (value["policy_id"], value["shortlist_rank"]))
    if len(shortlist_rows) != len(expected_items):
        mismatches.append(
            {
                "selection_id": selection_id,
                "field": "shortlist_count",
                "expected": len(expected_items),
                "actual": len(shortlist_rows),
            }
        )
    for row, expected in zip(shortlist_rows, expected_items):
        actual = {key: row[key] for key in expected}
        actual["shortlist_rank"] = int(actual["shortlist_rank"])
        actual["authoritative"] = int(actual["authoritative"])
        expected_sha = canonical_sha256(_shortlist_record_body(row))
        if expected != actual or str(row["item_record_sha256"]) != expected_sha:
            mismatches.append(
                {
                    "selection_id": selection_id,
                    "field": "shortlist_item",
                    "expected": expected,
                    "actual": actual,
                    "expected_record_sha256": expected_sha,
                    "actual_record_sha256": str(row["item_record_sha256"]),
                }
            )


def _validate_candidates_and_rankings(
    connection: sqlite3.Connection,
    run: sqlite3.Row,
    source: E2SelectionSources,
    state: _PopulationState,
    checks: list[ValidationCheck],
) -> None:
    run_id = str(run["run_id"])
    candidate_mismatches: list[dict[str, Any]] = []
    ranking_mismatches: list[dict[str, Any]] = []
    candidate_total = 0
    summary_by_key = {
        (value.source, value.source_building_id): value for value in state.selected
    }
    candidate_groups: dict[
        tuple[str, str], tuple[tuple[BuildingImageCandidate, ...], tuple[Candidate, ...]]
    ] = {}
    node_members: dict[
        str, list[tuple[tuple[str, str], str]]
    ] = defaultdict(list)
    for key in sorted(summary_by_key):
        summary = summary_by_key[key]
        selected_row = state.selected_rows.get(key)
        if selected_row is None:
            continue
        source_rows = tuple(source.iter_candidates(*key))
        candidate_total += len(source_rows)
        state.selected_candidate_counts[(summary.source, summary.stratum)] += len(
            source_rows
        )
        mapped = _validate_candidate_rows(
            connection,
            run_id,
            str(selected_row["selection_id"]),
            source_rows,
            candidate_mismatches,
        )
        candidate_groups[key] = (source_rows, mapped)
        for candidate in mapped:
            if candidate.phash_node_id is not None:
                node_members[candidate.phash_node_id].append(
                    (key, candidate.candidate_id)
                )

    edge_mismatches: list[dict[str, Any]] = []
    direct_by_building: dict[
        tuple[str, str], dict[tuple[str, str], DirectPHashEdge]
    ] = defaultdict(dict)
    direct_node_edge_count = 0
    for edge in source.direct_phash_pairs(set(node_members)):
        direct_node_edge_count += 1
        for left_key, left_candidate_id in node_members.get(edge.left_node_id, ()):
            for right_key, right_candidate_id in node_members.get(edge.right_node_id, ()):
                if left_key != right_key:
                    continue
                candidate_edge = DirectPHashEdge(
                    left_candidate_id=left_candidate_id,
                    right_candidate_id=right_candidate_id,
                    distance=edge.hamming_distance,
                )
                pair = candidate_edge.pair
                prior = direct_by_building[left_key].get(pair)
                if prior is not None and prior.distance != candidate_edge.distance:
                    edge_mismatches.append(
                        {
                            "building": left_key,
                            "pair": pair,
                            "first": prior.distance,
                            "second": candidate_edge.distance,
                        }
                    )
                direct_by_building[left_key][pair] = candidate_edge
    _add(
        checks,
        "e2_selected_direct_edges_consistent",
        not edge_mismatches,
        [],
        edge_mismatches[:100],
        selected_node_count=len(node_members),
        direct_node_edge_count=direct_node_edge_count,
        expanded_candidate_edge_count=sum(len(value) for value in direct_by_building.values()),
    )

    for key in sorted(candidate_groups):
        selected_row = state.selected_rows[key]
        _source_rows, mapped = candidate_groups[key]
        _validate_ranking_rows(
            connection,
            run_id,
            str(selected_row["selection_id"]),
            mapped,
            tuple(direct_by_building[key].values()),
            int(run["shortlist_size"]),
            ranking_mismatches,
        )
    actual_candidate_total = int(
        connection.execute(
            "SELECT count(*) FROM image_candidates WHERE run_id=?", (run_id,)
        ).fetchone()[0]
    )
    if actual_candidate_total != candidate_total:
        candidate_mismatches.append(
            {
                "field": "total_candidate_count",
                "expected": candidate_total,
                "actual": actual_candidate_total,
            }
        )
    _add(
        checks,
        "candidate_e2_fields_relations_and_record_hashes",
        not candidate_mismatches,
        [],
        candidate_mismatches[:100],
        mismatch_count=len(candidate_mismatches),
        candidate_count=candidate_total,
    )
    _add(
        checks,
        "p0_p1_p2_rankings_and_nontransitive_chosen_star",
        not ranking_mismatches,
        [],
        ranking_mismatches[:100],
        mismatch_count=len(ranking_mismatches),
    )
    stratum_candidate_mismatches: list[dict[str, Any]] = []
    for cell, row in state.stratum_rows.items():
        expected = state.selected_candidate_counts[cell]
        actual = int(row["selected_candidate_count"])
        if expected != actual:
            stratum_candidate_mismatches.append(
                {"cell": cell, "expected": expected, "actual": actual}
            )
    _add(
        checks,
        "stratum_candidate_accounting",
        not stratum_candidate_mismatches,
        [],
        stratum_candidate_mismatches[:100],
    )


class _RowGroupStream:
    """Bounded look-ahead over rows ordered by ``selection_id``."""

    def __init__(self, rows: Iterable[sqlite3.Row]) -> None:
        self._rows = iter(rows)
        self._next = next(self._rows, None)
        self.consumed_rows = 0

    def _pop_group(
        self,
        *,
        max_rows: int,
    ) -> tuple[str, tuple[sqlite3.Row, ...], int]:
        if self._next is None:
            raise StopIteration
        selection_id = str(self._next["selection_id"])
        values: list[sqlite3.Row] = []
        total = 0
        while self._next is not None and str(self._next["selection_id"]) == selection_id:
            if len(values) < max_rows:
                values.append(self._next)
            total += 1
            self.consumed_rows += 1
            self._next = next(self._rows, None)
        return selection_id, tuple(values), total

    def take(
        self,
        selection_id: str,
        mismatches: _MismatchCollector,
        *,
        table: str,
        max_rows: int,
    ) -> tuple[sqlite3.Row, ...]:
        while self._next is not None and str(self._next["selection_id"]) < selection_id:
            unexpected_id, _rows, total = self._pop_group(max_rows=0)
            mismatches.append(
                {
                    "selection_id": unexpected_id,
                    "field": f"unexpected_{table}_group",
                    "actual_count": total,
                }
            )
        if self._next is None or str(self._next["selection_id"]) > selection_id:
            return ()
        _actual_id, rows, total = self._pop_group(max_rows=max_rows)
        if total > len(rows):
            mismatches.append(
                {
                    "selection_id": selection_id,
                    "field": f"oversized_{table}_group",
                    "stored_for_comparison": len(rows),
                    "actual_count": total,
                }
            )
        return rows

    def drain(
        self,
        mismatches: _MismatchCollector,
        *,
        table: str,
    ) -> None:
        while self._next is not None:
            selection_id, _rows, total = self._pop_group(max_rows=0)
            mismatches.append(
                {
                    "selection_id": selection_id,
                    "field": f"unexpected_{table}_group",
                    "actual_count": total,
                }
            )


def _compare_full_candidate_group(
    actual_rows: Sequence[sqlite3.Row],
    source_rows: Sequence[BuildingImageCandidate],
    selection_id: str,
    mismatches: _MismatchCollector,
) -> tuple[Candidate, ...]:
    mapped = tuple(_candidate(value) for value in source_rows)
    expected_order = sorted(
        zip(mapped, source_rows),
        key=lambda value: (value[1].source_asset_id, value[0].candidate_id),
    )
    rows = sorted(
        actual_rows,
        key=lambda row: (str(row["source_asset_id"]), str(row["candidate_id"])),
    )
    if len(rows) != len(expected_order):
        mismatches.append(
            {
                "selection_id": selection_id,
                "field": "candidate_count",
                "expected": len(expected_order),
                "actual": len(rows),
            }
        )
    for row, (candidate, source_row) in zip(rows, expected_order):
        expected = _candidate_expected_values(candidate, source_row, selection_id)
        actual: dict[str, Any] = {}
        parse_errors: list[str] = []
        for key in expected:
            if key in {"roles_json", "quality_flags_json"}:
                try:
                    actual[key] = _json(row[key], expected=list, label=key)
                except SelectionValidationError as exc:
                    actual[key] = None
                    parse_errors.append(str(exc))
            else:
                actual[key] = row[key]
        for integer_key in (
            "role_rank",
            "ordinal_is_derived",
            "original_width",
            "original_height",
            "normalized_width",
            "normalized_height",
            "low_information",
        ):
            if actual[integer_key] is not None:
                actual[integer_key] = int(actual[integer_key])
        row_mismatch: dict[str, Any] = {}
        if expected != actual:
            row_mismatch.update({"expected": expected, "actual": actual})
        try:
            detail = _json(row["detail_json"], expected=dict, label="candidate detail_json")
        except SelectionValidationError as exc:
            detail = {}
            parse_errors.append(str(exc))
        if detail.get("phash_hex") != source_row.phash_hex:
            row_mismatch["expected_phash_hex"] = source_row.phash_hex
            row_mismatch["actual_phash_hex"] = detail.get("phash_hex")
        if detail.get("ranking_feature_record_sha256") != candidate.record_sha256:
            row_mismatch["expected_ranking_feature_record_sha256"] = (
                candidate.record_sha256
            )
            row_mismatch["actual_ranking_feature_record_sha256"] = detail.get(
                "ranking_feature_record_sha256"
            )
        if parse_errors:
            row_mismatch["parse_errors"] = parse_errors
        if row_mismatch:
            row_mismatch.update(
                {"selection_id": selection_id, "candidate_id": candidate.candidate_id}
            )
            mismatches.append(row_mismatch)
    return mapped


def _compare_full_policy_group(
    ranking_rows: Sequence[sqlite3.Row],
    shortlist_rows: Sequence[sqlite3.Row],
    shortlist: Any,
    candidates: Sequence[Candidate],
    selection_id: str,
    mismatches: _MismatchCollector,
    direct_edge_evidence: Mapping[tuple[str, str], Mapping[str, Any]],
    direct_evidence_mismatches: _MismatchCollector,
) -> None:
    expected_evaluations = sorted(
        shortlist.evaluations,
        key=lambda value: (value.editorial_rank, value.candidate_id),
    )
    actual_rankings = sorted(
        ranking_rows,
        key=lambda row: (
            row["editorial_rank"] is None,
            0 if row["editorial_rank"] is None else int(row["editorial_rank"]),
            str(row["candidate_id"]),
        ),
    )
    if len(actual_rankings) != len(expected_evaluations):
        mismatches.append(
            {
                "selection_id": selection_id,
                "policy_id": shortlist.policy.policy_id,
                "field": "ranking_count",
                "expected": len(expected_evaluations),
                "actual": len(actual_rankings),
            }
        )
    by_candidate = {candidate.candidate_id: candidate for candidate in candidates}
    for row, evaluation in zip(actual_rankings, expected_evaluations):
        candidate = by_candidate[evaluation.candidate_id]
        suppression_reason = next(
            (
                reason
                for reason in evaluation.reasons
                if reason.startswith("suppressed_")
            ),
            None,
        )
        expected = {
            "policy_id": shortlist.policy.policy_id,
            "policy_version": E3_POLICY_VERSION,
            "policy_config_sha256": shortlist.policy.config_sha256,
            "selection_id": selection_id,
            "candidate_id": evaluation.candidate_id,
            "ranking_state": _ranking_state(evaluation.reasons, evaluation.selected),
            "editorial_rank": evaluation.editorial_rank,
            "shortlist_rank": evaluation.shortlist_rank,
            "selected": int(evaluation.selected),
            "qa_fallback": int(evaluation.qa_fallback),
            "hard_risk": int(evaluation.hard_risk),
            "rank_tuple_json": list(editorial_sort_key(candidate)),
            "component_scores_json": dict(evaluation.component_scores),
            "reasons_json": list(evaluation.reasons),
            "suppressed_by_candidate_id": evaluation.suppressed_by_candidate_id,
            "suppression_reason": suppression_reason,
            "fallback_reason": (
                "all_successful_candidates_hard_risk"
                if evaluation.qa_fallback
                else None
            ),
            "ranking_record_sha256": evaluation.record_sha256,
        }
        actual: dict[str, Any] = {}
        parse_errors: list[str] = []
        for key in expected:
            if key in {"rank_tuple_json", "reasons_json"}:
                try:
                    actual[key] = _json(row[key], expected=list, label=key)
                except SelectionValidationError as exc:
                    actual[key] = None
                    parse_errors.append(str(exc))
            elif key == "component_scores_json":
                try:
                    actual[key] = _json(row[key], expected=dict, label=key)
                except SelectionValidationError as exc:
                    actual[key] = None
                    parse_errors.append(str(exc))
            else:
                actual[key] = row[key]
        for integer_key in (
            "editorial_rank",
            "shortlist_rank",
            "selected",
            "qa_fallback",
            "hard_risk",
        ):
            if actual[integer_key] is not None:
                actual[integer_key] = int(actual[integer_key])
        if expected != actual or parse_errors:
            mismatches.append(
                {
                    "selection_id": selection_id,
                    "policy_id": shortlist.policy.policy_id,
                    "candidate_id": evaluation.candidate_id,
                    "expected": expected,
                    "actual": actual,
                    "parse_errors": parse_errors,
                }
            )
        edge_detail: Mapping[str, Any] | None = None
        if (
            suppression_reason == "suppressed_direct_phash_le8"
            and evaluation.suppressed_by_candidate_id is not None
        ):
            pair = tuple(
                sorted(
                    (
                        evaluation.candidate_id,
                        evaluation.suppressed_by_candidate_id,
                    )
                )
            )
            edge_detail = direct_edge_evidence.get(pair)
            if edge_detail is None:
                direct_evidence_mismatches.append(
                    {
                        "selection_id": selection_id,
                        "policy_id": shortlist.policy.policy_id,
                        "candidate_id": evaluation.candidate_id,
                        "field": "missing_e2_direct_edge_evidence",
                        "candidate_pair": pair,
                    }
                )
        expected_detail = {
            "direct_phash_edge": edge_detail,
            "ranking_feature_record_sha256": evaluation.candidate_record_sha256,
        }
        try:
            actual_detail = _json(
                row["detail_json"], expected=dict, label="ranking detail_json"
            )
            detail_error = None
        except SelectionValidationError as exc:
            actual_detail = None
            detail_error = str(exc)
        if actual_detail != expected_detail or detail_error is not None:
            direct_evidence_mismatches.append(
                {
                    "selection_id": selection_id,
                    "policy_id": shortlist.policy.policy_id,
                    "candidate_id": evaluation.candidate_id,
                    "suppression_reason": suppression_reason,
                    "expected": expected_detail,
                    "actual": actual_detail,
                    "parse_error": detail_error,
                }
            )

    evaluations = {value.candidate_id: value for value in shortlist.evaluations}
    expected_items: list[dict[str, Any]] = []
    for rank, candidate_id in enumerate(shortlist.selected_candidate_ids, 1):
        evaluation = evaluations[candidate_id]
        expected_items.append(
            {
                "policy_id": shortlist.policy.policy_id,
                "selection_id": selection_id,
                "shortlist_rank": rank,
                "candidate_id": candidate_id,
                "shortlist_state": (
                    "qa_fallback" if evaluation.qa_fallback else "primary"
                ),
                "authoritative": 0,
            }
        )
    actual_shortlists = sorted(
        shortlist_rows,
        key=lambda row: (int(row["shortlist_rank"]), str(row["candidate_id"])),
    )
    if len(actual_shortlists) != len(expected_items):
        mismatches.append(
            {
                "selection_id": selection_id,
                "policy_id": shortlist.policy.policy_id,
                "field": "shortlist_count",
                "expected": len(expected_items),
                "actual": len(actual_shortlists),
            }
        )
    for row, expected in zip(actual_shortlists, expected_items):
        actual = {key: row[key] for key in expected}
        actual["shortlist_rank"] = int(actual["shortlist_rank"])
        actual["authoritative"] = int(actual["authoritative"])
        expected_sha = canonical_sha256(_shortlist_record_body(row))
        if expected != actual or str(row["item_record_sha256"]) != expected_sha:
            mismatches.append(
                {
                    "selection_id": selection_id,
                    "policy_id": shortlist.policy.policy_id,
                    "field": "shortlist_item",
                    "expected": expected,
                    "actual": actual,
                    "expected_record_sha256": expected_sha,
                    "actual_record_sha256": str(row["item_record_sha256"]),
                }
            )


class _SameBuildingDirectEdgeStream:
    """Merge-stream E2 direct edges with memory bounded to one building."""

    def __init__(self, rows: Iterable[SameBuildingDirectPhashEdge]) -> None:
        self._rows = iter(rows)
        self._next = next(self._rows, None)

    @staticmethod
    def _key(row: SameBuildingDirectPhashEdge) -> tuple[str, str]:
        return (row.source, row.source_building_id)

    def _pop_group(self) -> tuple[tuple[str, str], tuple[SameBuildingDirectPhashEdge, ...]]:
        if self._next is None:
            raise StopIteration
        key = self._key(self._next)
        rows: list[SameBuildingDirectPhashEdge] = []
        while self._next is not None and self._key(self._next) == key:
            rows.append(self._next)
            self._next = next(self._rows, None)
        return key, tuple(rows)

    def take(
        self,
        key: tuple[str, str],
        mismatches: _MismatchCollector,
    ) -> tuple[SameBuildingDirectPhashEdge, ...]:
        while self._next is not None and self._key(self._next) < key:
            unexpected_key, rows = self._pop_group()
            mismatches.append(
                {
                    "building": unexpected_key,
                    "field": "direct_edge_group_without_successful_candidates",
                    "edge_rows": len(rows),
                }
            )
        if self._next is None or self._key(self._next) > key:
            return ()
        _actual_key, rows = self._pop_group()
        return rows

    def drain(self, mismatches: _MismatchCollector) -> None:
        while self._next is not None:
            key, rows = self._pop_group()
            mismatches.append(
                {
                    "building": key,
                    "field": "direct_edge_group_without_successful_candidates",
                    "edge_rows": len(rows),
                }
            )


def _full_direct_edges_for_building(
    source_rows: Sequence[BuildingImageCandidate],
    candidates: Sequence[Candidate],
    edge_rows: Sequence[SameBuildingDirectPhashEdge],
    mismatches: _MismatchCollector,
) -> tuple[
    tuple[DirectPHashEdge, ...],
    Mapping[tuple[str, str], Mapping[str, Any]],
]:
    by_asset = {
        row.source_asset_id: candidate for row, candidate in zip(source_rows, candidates)
    }
    edges: dict[tuple[str, str], DirectPHashEdge] = {}
    evidence: dict[tuple[str, str], Mapping[str, Any]] = {}
    for row in edge_rows:
        left = by_asset.get(row.left_source_asset_id)
        right = by_asset.get(row.right_source_asset_id)
        if left is None or right is None:
            mismatches.append(
                {
                    "building": (row.source, row.source_building_id),
                    "field": "direct_edge_references_non_candidate_asset",
                    "left_source_asset_id": row.left_source_asset_id,
                    "right_source_asset_id": row.right_source_asset_id,
                }
            )
            continue
        if left.phash_node_id != row.left_node_id or right.phash_node_id != row.right_node_id:
            mismatches.append(
                {
                    "building": (row.source, row.source_building_id),
                    "field": "direct_edge_node_identity",
                    "expected": (left.phash_node_id, right.phash_node_id),
                    "actual": (row.left_node_id, row.right_node_id),
                }
            )
            continue
        edge = DirectPHashEdge(
            left_candidate_id=left.candidate_id,
            right_candidate_id=right.candidate_id,
            distance=row.hamming_distance,
        )
        detail = {
            "distance": row.hamming_distance,
            "edge_id": row.edge_id,
            "edge_record_sha256": row.edge_record_sha256,
            "left_candidate_id": edge.pair[0],
            "right_candidate_id": edge.pair[1],
        }
        prior_edge = edges.get(edge.pair)
        prior_detail = evidence.get(edge.pair)
        if (
            (prior_edge is not None and prior_edge.distance != edge.distance)
            or (prior_detail is not None and prior_detail != detail)
        ):
            mismatches.append(
                {
                    "building": (row.source, row.source_building_id),
                    "field": "conflicting_direct_edge_evidence",
                    "candidate_pair": edge.pair,
                    "first": prior_detail,
                    "second": detail,
                }
            )
            continue
        edges[edge.pair] = edge
        evidence[edge.pair] = detail
    return tuple(edges[key] for key in sorted(edges)), evidence


def _validate_full_candidates_and_rankings(
    connection: sqlite3.Connection,
    run: sqlite3.Row,
    source: E2SelectionSources,
    state: _FullPopulationState,
    checks: list[ValidationCheck],
) -> _FullCandidateState:
    """Merge-stream full candidates/rankings with bounded per-building state."""

    run_id = str(run["run_id"])
    policies = policy_definitions(int(run["shortlist_size"]))
    candidate_mismatches = _MismatchCollector()
    ranking_mismatches = _MismatchCollector()
    edge_mismatches = _MismatchCollector()
    direct_evidence_mismatches = _MismatchCollector()

    candidate_stream = _RowGroupStream(
        _iter_rows(
            connection,
            """
            SELECT * FROM image_candidates
            WHERE run_id=?
            ORDER BY selection_id,source,source_asset_id
            """,
            (run_id,),
        )
    )
    ranking_streams = {
        policy.policy_id: _RowGroupStream(
            _iter_rows(
                connection,
                """
                SELECT * FROM policy_rankings
                WHERE run_id=? AND policy_id=?
                ORDER BY selection_id,candidate_id
                """,
                (run_id, policy.policy_id),
            )
        )
        for policy in policies
    }
    shortlist_streams = {
        policy.policy_id: _RowGroupStream(
            _iter_rows(
                connection,
                """
                SELECT * FROM shortlist_items
                WHERE run_id=? AND policy_id=?
                ORDER BY selection_id,shortlist_rank
                """,
                (run_id, policy.policy_id),
            )
        )
        for policy in policies
    }

    candidate_total = 0
    completed_buildings = 0
    direct_edge_rows = 0
    shortlist_total = 0
    last_key: tuple[str, str] | None = None
    edge_stream = _SameBuildingDirectEdgeStream(
        source.iter_same_building_direct_phash_edges()
    )
    all_candidates = source.iter_all_candidates()
    for key, grouped in groupby(
        all_candidates,
        key=lambda value: (value.source, value.source_building_id),
    ):
        source_rows = tuple(grouped)
        candidate_total += len(source_rows)
        completed_buildings += 1
        last_key = key
        selection_id = f"{key[0]}:building:{key[1]}"
        actual_candidates = candidate_stream.take(
            selection_id,
            candidate_mismatches,
            table="image_candidates",
            max_rows=len(source_rows) + 1,
        )
        mapped = _compare_full_candidate_group(
            actual_candidates,
            source_rows,
            selection_id,
            candidate_mismatches,
        )
        source_edge_rows = edge_stream.take(key, edge_mismatches)
        direct_edge_rows += len(source_edge_rows)
        direct_edges, direct_edge_evidence = _full_direct_edges_for_building(
            source_rows,
            mapped,
            source_edge_rows,
            edge_mismatches,
        )
        shortlists = compare_standard_policies(
            mapped,
            shortlist_size=int(run["shortlist_size"]),
            direct_phash_edges=direct_edges,
        )
        for shortlist in shortlists:
            shortlist_total += len(shortlist.selected_candidate_ids)
            policy_id = shortlist.policy.policy_id
            actual_rankings = ranking_streams[policy_id].take(
                selection_id,
                ranking_mismatches,
                table="policy_rankings",
                max_rows=len(mapped) + 1,
            )
            actual_shortlists = shortlist_streams[policy_id].take(
                selection_id,
                ranking_mismatches,
                table="shortlist_items",
                max_rows=min(int(run["shortlist_size"]), len(mapped)) + 1,
            )
            _compare_full_policy_group(
                actual_rankings,
                actual_shortlists,
                shortlist,
                mapped,
                selection_id,
                ranking_mismatches,
                direct_edge_evidence,
                direct_evidence_mismatches,
            )

    edge_stream.drain(edge_mismatches)
    candidate_stream.drain(candidate_mismatches, table="image_candidates")
    for policy in policies:
        ranking_streams[policy.policy_id].drain(
            ranking_mismatches,
            table="policy_rankings",
        )
        shortlist_streams[policy.policy_id].drain(
            ranking_mismatches,
            table="shortlist_items",
        )

    actual_candidate_total = int(
        connection.execute(
            "SELECT count(*) FROM image_candidates WHERE run_id=?", (run_id,)
        ).fetchone()[0]
    )
    if actual_candidate_total != candidate_total:
        candidate_mismatches.append(
            {
                "field": "total_candidate_count",
                "expected": candidate_total,
                "actual": actual_candidate_total,
            }
        )
    _add(
        checks,
        "e2_full_direct_edges_consistent",
        not edge_mismatches,
        [],
        edge_mismatches.examples,
        mismatch_count=edge_mismatches.count,
        same_building_direct_edge_rows=direct_edge_rows,
        streaming=True,
        memory_scope="current_building",
    )
    _add(
        checks,
        "candidate_e2_fields_relations_and_record_hashes",
        not candidate_mismatches,
        [],
        candidate_mismatches.examples,
        mismatch_count=candidate_mismatches.count,
        candidate_count=candidate_total,
        streaming=True,
    )
    _add(
        checks,
        "p0_p1_p2_rankings_and_nontransitive_chosen_star",
        not ranking_mismatches,
        [],
        ranking_mismatches.examples,
        mismatch_count=ranking_mismatches.count,
        streaming=True,
    )
    _add(
        checks,
        "p2_direct_suppression_e2_edge_provenance",
        not direct_evidence_mismatches,
        [],
        direct_evidence_mismatches.examples,
        mismatch_count=direct_evidence_mismatches.count,
        direct_edge_rows=direct_edge_rows,
        streaming=True,
    )

    actual_by_stratum = {
        str(row[0]): int(row[1])
        for row in connection.execute(
            """
            SELECT ps.stratum_id,count(ic.candidate_id)
            FROM population_strata ps
            LEFT JOIN selected_buildings sb
              ON sb.run_id=ps.run_id AND sb.stratum_id=ps.stratum_id
            LEFT JOIN image_candidates ic
              ON ic.run_id=sb.run_id AND ic.selection_id=sb.selection_id
            WHERE ps.run_id=?
            GROUP BY ps.stratum_id
            """,
            (run_id,),
        )
    }
    stratum_mismatches: list[dict[str, Any]] = []
    for cell, row in state.stratum_rows.items():
        expected = actual_by_stratum.get(str(row["stratum_id"]), 0)
        actual = int(row["selected_candidate_count"])
        if expected != actual:
            stratum_mismatches.append(
                {"cell": cell, "expected": expected, "actual": actual}
            )
    _add(
        checks,
        "stratum_candidate_accounting",
        not stratum_mismatches,
        [],
        stratum_mismatches[:MAX_MISMATCH_EXAMPLES],
        streaming=True,
    )
    return _FullCandidateState(
        candidate_count=candidate_total,
        completed_buildings=completed_buildings,
        direct_edge_rows=direct_edge_rows,
        last_key=last_key,
        ranking_rows=candidate_total * len(policies),
        shortlist_rows=shortlist_total,
    )


def _validate_full_checkpoints(
    connection: sqlite3.Connection,
    run: sqlite3.Row,
    *,
    population: _FullPopulationState,
    candidates: _FullCandidateState,
    checks: list[ValidationCheck],
) -> None:
    def counter_json(values: Mapping[tuple[str, str], int]) -> dict[str, int]:
        return {
            f"{source}:{stratum}": int(values[(source, stratum)])
            for source, stratum in sorted(values)
        }

    expected_cursors = {
        "inventory": {
            "eligible_counts": counter_json(population.eligible_counts),
            "last_key": list(population.last_key) if population.last_key else None,
            "population_counts": counter_json(population.population_counts),
        },
        "selection": {
            "last_key": list(population.last_key) if population.last_key else None,
            "ordered_manifest_sha256": population.ordered_manifest_sha256,
        },
        "candidates": {
            "completed_buildings": candidates.completed_buildings,
            "direct_edge_rows": candidates.direct_edge_rows,
            "last_key": list(candidates.last_key) if candidates.last_key else None,
            "ranking_rows": candidates.ranking_rows,
            "shortlist_rows": candidates.shortlist_rows,
        },
    }
    expected = {
        "inventory": {
            "cursor": expected_cursors["inventory"],
            "completed_rows": population.population_count,
            "phase_complete": 1,
        },
        "selection": {
            "cursor": expected_cursors["selection"],
            "completed_rows": population.population_count,
            "phase_complete": 1,
        },
        "candidates": {
            "cursor": expected_cursors["candidates"],
            "completed_rows": candidates.candidate_count,
            "phase_complete": 1,
        },
    }
    rows = connection.execute(
        """
        SELECT phase,cursor_json,completed_rows,phase_complete
        FROM build_checkpoints
        WHERE run_id=?
        ORDER BY phase
        """,
        (str(run["run_id"]),),
    ).fetchall()
    actual: dict[str, dict[str, Any]] = {}
    for row in rows:
        phase = str(row["phase"])
        try:
            cursor = _json(
                row["cursor_json"],
                expected=dict,
                label=f"{phase} checkpoint cursor_json",
            )
        except SelectionValidationError as exc:
            cursor = {"parse_error": str(exc)}
        actual[phase] = {
            "cursor": cursor,
            "completed_rows": int(row["completed_rows"]),
            "phase_complete": int(row["phase_complete"]),
        }

    run_id = str(run["run_id"])
    actual_tables = {
        "population_rows": int(
            connection.execute(
                "SELECT coalesce(sum(population_count),0) FROM population_strata WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
        ),
        "eligible_rows": int(
            connection.execute(
                "SELECT coalesce(sum(eligible_count),0) FROM population_strata WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
        ),
        "selected_rows": int(
            connection.execute(
                "SELECT count(*) FROM selected_buildings WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        ),
        "candidate_rows": int(
            connection.execute(
                "SELECT count(*) FROM image_candidates WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        ),
        "candidate_buildings": int(
            connection.execute(
                "SELECT count(DISTINCT selection_id) FROM image_candidates WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
        ),
        "ranking_rows": int(
            connection.execute(
                "SELECT count(*) FROM policy_rankings WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        ),
        "shortlist_rows": int(
            connection.execute(
                "SELECT count(*) FROM shortlist_items WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        ),
        "ordered_manifest_sha256": str(run["ordered_selection_manifest_sha256"]),
    }
    expected_tables = {
        "population_rows": population.population_count,
        "eligible_rows": sum(population.eligible_counts.values()),
        "selected_rows": population.population_count,
        "candidate_rows": candidates.candidate_count,
        "candidate_buildings": candidates.completed_buildings,
        "ranking_rows": candidates.ranking_rows,
        "shortlist_rows": candidates.shortlist_rows,
        "ordered_manifest_sha256": population.ordered_manifest_sha256,
    }
    passed = actual == expected and actual_tables == expected_tables
    _add(
        checks,
        "full_streaming_checkpoints",
        passed,
        {"checkpoints": expected, "source_and_tables": expected_tables},
        {"checkpoints": actual, "source_and_tables": actual_tables},
        cursor_fields=(
            "last_key",
            "population_counts",
            "eligible_counts",
            "ordered_manifest_sha256",
            "completed_buildings",
            "ranking_rows",
            "shortlist_rows",
            "direct_edge_rows",
        ),
        independently_recomputed=True,
    )


def _queue_expected_counts(
    connection: sqlite3.Connection,
    run_id: str,
    policy_id: str,
) -> dict[str, int]:
    row = connection.execute(
        """
        SELECT
          sum(si.shortlist_rank=1),
          count(DISTINCT CASE WHEN si.shortlist_rank=1
                         THEN ic.normalized_pixel_sha256 END),
          sum(si.shortlist_rank<=3),
          count(DISTINCT CASE WHEN si.shortlist_rank<=3
                         THEN ic.normalized_pixel_sha256 END)
        FROM shortlist_items si
        JOIN image_candidates ic
          ON ic.run_id=si.run_id AND ic.candidate_id=si.candidate_id
        WHERE si.run_id=? AND si.policy_id=?
        """,
        (run_id, policy_id),
    ).fetchone()
    return {
        "top1_no_reuse": int(row[0] or 0),
        "top1_exact_reuse": int(row[1] or 0),
        "top3_no_reuse": int(row[2] or 0),
        "top3_exact_reuse": int(row[3] or 0),
    }


def _validate_queue_estimates(
    connection: sqlite3.Connection,
    run: sqlite3.Row,
    checks: list[ValidationCheck],
) -> None:
    run_id = str(run["run_id"])
    policy_ids = {
        str(row[0])
        for row in connection.execute(
            "SELECT policy_id FROM policy_definitions WHERE run_id=? AND enabled=1",
            (run_id,),
        )
    }
    rows = connection.execute(
        "SELECT * FROM queue_estimates WHERE run_id=? ORDER BY estimate_id",
        (run_id,),
    ).fetchall()
    mismatches: list[dict[str, Any]] = []
    policies_seen: set[str] = set()
    scenarios_seen: Counter[tuple[str, str]] = Counter()
    expected_scenarios = {
        "top1_no_reuse": "selected_entity",
        "top1_exact_reuse": "exact_unique_asset",
        "top3_no_reuse": "shortlist_item",
        "top3_exact_reuse": "exact_unique_asset",
    }
    expected_counts = {
        policy_id: _queue_expected_counts(connection, run_id, policy_id)
        for policy_id in policy_ids
    }
    for row in rows:
        estimate_id = str(row["estimate_id"])
        policy_id = str(row["policy_id"])
        policies_seen.add(policy_id)
        row_mismatch: dict[str, Any] = {}
        try:
            detail = _json(row["detail_json"], expected=dict, label="queue detail_json")
        except SelectionValidationError as exc:
            detail = {}
            row_mismatch["detail_error"] = str(exc)
        scenario = str(detail.get("scenario", ""))
        scenarios_seen[(policy_id, scenario)] += 1
        if policy_id not in policy_ids:
            row_mismatch["unknown_policy"] = policy_id
        expected_unit = expected_scenarios.get(scenario)
        if expected_unit is None or str(row["queue_unit"]) != expected_unit:
            row_mismatch["scenario_queue_unit"] = {
                "scenario": scenario,
                "expected": expected_unit,
                "actual": str(row["queue_unit"]),
            }
        if row["stratum_id"] is not None:
            row_mismatch["stratum_id_must_be_null"] = row["stratum_id"]
        expected_detail = {
            "creates_vision_tasks": False,
            "executable": False,
            "non_executable": True,
            "planning_only": True,
            "scenario": scenario,
            "semantic_reuse_allowed": False,
        }
        if detail != expected_detail:
            row_mismatch["expected_detail"] = expected_detail
            row_mismatch["actual_detail"] = detail
        if int(row["requests_executed"]) != 0 or int(row["authoritative"]) != 0:
            row_mismatch["execution_flags"] = {
                "requests_executed": int(row["requests_executed"]),
                "authoritative": int(row["authoritative"]),
            }
        try:
            expected_sha = canonical_sha256(_queue_record_body(row))
        except SelectionValidationError as exc:
            expected_sha = None
            row_mismatch["record_body_error"] = str(exc)
        if expected_sha is not None and str(row["estimate_record_sha256"]) != expected_sha:
            row_mismatch["expected_record_sha256"] = expected_sha
            row_mismatch["actual_record_sha256"] = str(row["estimate_record_sha256"])
        expected_count = expected_counts.get(policy_id, {}).get(scenario)
        if expected_count is not None and int(row["estimated_queue_items"]) != expected_count:
            row_mismatch["expected_queue_items"] = expected_count
            row_mismatch["actual_queue_items"] = int(row["estimated_queue_items"])
        population_scenario = (
            "top1_no_reuse" if scenario.startswith("top1_") else "top3_no_reuse"
        )
        population_count = expected_counts.get(policy_id, {}).get(population_scenario)
        if population_count is not None and int(row["population_count"]) != population_count:
            row_mismatch["expected_population_count"] = population_count
            row_mismatch["actual_population_count"] = int(row["population_count"])
        expected_id = f"{policy_id}:{scenario}"
        if estimate_id != expected_id:
            row_mismatch["expected_estimate_id"] = expected_id
        low = row["tokens_per_item_low"]
        point = row["tokens_per_item_point"]
        high = row["tokens_per_item_high"]
        if all(value is not None for value in (low, point, high)) and not (
            float(low) <= float(point) <= float(high)
        ):
            row_mismatch["token_range"] = [low, point, high]
        projected = (
            row["projected_input_tokens"],
            row["projected_output_tokens"],
            row["projected_total_tokens"],
        )
        if projected[2] is not None:
            if projected[0] is None or projected[1] is None or int(projected[2]) != int(
                projected[0]
            ) + int(projected[1]):
                row_mismatch["projected_token_accounting"] = projected
        nullable_planning = {
            "tokens_per_item_low": row["tokens_per_item_low"],
            "tokens_per_item_point": row["tokens_per_item_point"],
            "tokens_per_item_high": row["tokens_per_item_high"],
            "projected_input_tokens": row["projected_input_tokens"],
            "projected_output_tokens": row["projected_output_tokens"],
            "projected_total_tokens": row["projected_total_tokens"],
            "estimated_calls": row["estimated_calls"],
            "estimated_cost_usd": row["estimated_cost_usd"],
            "quota_basis": row["quota_basis"],
            "projected_quota_percent": row["projected_quota_percent"],
        }
        if any(value is not None for value in nullable_planning.values()):
            row_mismatch["undecided_cost_fields_must_be_null"] = nullable_planning
        if float(row["retry_factor"]) != 1.0:
            row_mismatch["retry_factor"] = float(row["retry_factor"])
        try:
            pricing = _json(
                row["pricing_snapshot_json"], expected=dict, label="pricing_snapshot_json"
            )
        except SelectionValidationError:
            pricing = None
        if pricing != {}:
            row_mismatch["pricing_snapshot"] = pricing
        if row_mismatch:
            row_mismatch["estimate_id"] = estimate_id
            mismatches.append(row_mismatch)
    expected_cells = {
        (policy_id, scenario)
        for policy_id in policy_ids
        for scenario in expected_scenarios
    }
    scenario_exact = (
        set(scenarios_seen) == expected_cells
        and all(count == 1 for count in scenarios_seen.values())
    )
    _add(
        checks,
        "queue_estimate_accounting_and_non_authoritative_flags",
        len(rows) == len(expected_cells)
        and not mismatches
        and policies_seen == policy_ids
        and scenario_exact,
        {
            "rows": len(expected_cells),
            "scenarios": sorted(expected_cells),
            "mismatches": [],
        },
        {
            "rows": len(rows),
            "policies": sorted(policies_seen),
            "scenarios": sorted(scenarios_seen),
            "mismatches": mismatches[:100],
        },
    )


def validate_e3_artifact(
    path: Path | str,
    *,
    expected_logical_sha256: str | None = None,
    verify_input_file_hashes: bool = True,
) -> SelectionValidationReport:
    """Validate one terminal sample/full E3 artifact without mutating inputs.

    ``verify_input_file_hashes`` is retained as an explicit safety switch but
    may not be disabled: the immutable E2 adapter hashes E2 at open, and full
    mode hashes every E2 byte again after validation.  Full mode uses merge-
    stream validation and retains only the current building's candidate and
    direct-edge groups in memory.
    """

    target = Path(path).resolve()
    if not target.is_file():
        raise FileNotFoundError(target)
    checks: list[ValidationCheck] = []
    output_sidecars_before = tuple(str(item) for item in sqlite_sidecars(target))
    output_size_before = target.stat().st_size
    _add(
        checks,
        "output_sqlite_sidecars_absent_at_open",
        not output_sidecars_before,
        [],
        output_sidecars_before,
    )
    connection = open_immutable(target)
    run_id: str | None = None
    logical_sha: str | None = None
    table_manifests: dict[str, dict[str, Any]] = {}
    input_files: tuple[InputFileCheck, ...] = ()
    source: E2SelectionSources | None = None
    try:
        quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = list(connection.execute("PRAGMA foreign_key_check"))
        _add(checks, "sqlite_quick_check", quick == "ok", "ok", quick)
        _add(checks, "sqlite_integrity_check", integrity == "ok", "ok", integrity)
        _add(checks, "sqlite_foreign_key_check", not foreign_keys, 0, len(foreign_keys))
        if not _validate_schema(connection, checks):
            return SelectionValidationReport(
                str(target), None, None, {}, (), tuple(checks)
            )
        runs = connection.execute("SELECT * FROM selection_runs ORDER BY run_id").fetchall()
        statuses = [str(row["status"]) for row in runs]
        _add(
            checks,
            "exactly_one_complete_candidate_run",
            len(runs) == 1 and statuses == ["complete"],
            {"count": 1, "statuses": ["complete"]},
            {"count": len(runs), "statuses": statuses},
        )
        if len(runs) != 1:
            return SelectionValidationReport(
                str(target), None, None, {}, (), tuple(checks)
            )
        run = runs[0]
        run_id = str(run["run_id"])
        _add(
            checks,
            "selection_contract_version",
            str(run["contract_version"]) == E3_SELECTION_VERSION,
            E3_SELECTION_VERSION,
            str(run["contract_version"]),
        )
        failed_validations = int(
            connection.execute(
                """
                SELECT count(*) FROM selection_validations
                WHERE run_id=? AND severity='error' AND passed=0
                """,
                (run_id,),
            ).fetchone()[0]
        )
        _add(
            checks,
            "complete_run_has_no_failed_error_validation",
            failed_validations == 0,
            0,
            failed_validations,
        )
        _validate_request_policy(connection, run, checks)
        _validate_policies(connection, run, checks)
        input_files, source = _validate_input(
            connection,
            run,
            checks,
            verify_file_hashes=verify_input_file_hashes,
        )
        selection_mode = str(run["selection_mode"])
        supported_mode = selection_mode in {"sample", "full"}
        _add(
            checks,
            "validator_selection_mode_supported",
            supported_mode,
            ["full", "sample"],
            selection_mode,
        )
        if source is not None and supported_mode:
            if selection_mode == "sample":
                state = _validate_population_and_selection(
                    connection, run, source, checks
                )
                _validate_candidates_and_rankings(
                    connection, run, source, state, checks
                )
            else:
                full_state = _validate_full_population_and_selection(
                    connection, run, source, checks
                )
                full_candidate_state = _validate_full_candidates_and_rankings(
                    connection, run, source, full_state, checks
                )
                _validate_full_checkpoints(
                    connection,
                    run,
                    population=full_state,
                    candidates=full_candidate_state,
                    checks=checks,
                )
        _validate_queue_estimates(connection, run, checks)
        logical_sha, table_manifests = logical_selection_manifest(connection, run_id)
        stored_rows = connection.execute(
            """
            SELECT value_text FROM selection_metrics
            WHERE run_id=? AND phase='validation'
              AND metric_name='output_logical_sha256' AND stratum_json='{}'
            """,
            (run_id,),
        ).fetchall()
        stored = str(stored_rows[0][0]) if len(stored_rows) == 1 else None
        _add(
            checks,
            "logical_manifest_matches_stored",
            len(stored_rows) == 1 and stored == logical_sha,
            logical_sha,
            stored,
            manifest_version=LOGICAL_MANIFEST_VERSION,
            tables=table_manifests,
        )
        if expected_logical_sha256 is not None:
            _add(
                checks,
                "logical_manifest_matches_expected",
                logical_sha == expected_logical_sha256,
                expected_logical_sha256,
                logical_sha,
            )
        if source is not None:
            final_sidecars_before_hash = tuple(
                str(item) for item in sqlite_sidecars(source.path)
            )
            final_size_before_hash = source.path.stat().st_size
            final_sha: str | None = None
            final_hash_error: str | None = None
            if selection_mode == "full":
                try:
                    final_sha = sha256_file(source.path)
                except OSError as exc:
                    final_hash_error = f"{type(exc).__name__}: {exc}"
            final_sidecars = tuple(str(item) for item in sqlite_sidecars(source.path))
            final_size = source.path.stat().st_size
            final_unchanged = (
                final_size_before_hash == source.lineage.artifact_size
                and final_size == source.lineage.artifact_size
                and not final_sidecars_before_hash
                and not final_sidecars
            )
            if selection_mode == "full":
                manifest_row = connection.execute(
                    """
                    SELECT sha256_before,sha256_after FROM selection_inputs
                    WHERE run_id=? AND input_role='e2_evidence'
                    """,
                    (run_id,),
                ).fetchone()
                expected_sha = str(run["e2_byte_sha256"])
                sha_actual = {
                    "start_sha256": source.lineage.artifact_sha256,
                    "manifest_sha256_before": (
                        None if manifest_row is None else str(manifest_row[0])
                    ),
                    "manifest_sha256_after": (
                        None if manifest_row is None else str(manifest_row[1])
                    ),
                    "run_sha256": expected_sha,
                    "end_sha256": final_sha,
                }
                sha_passed = (
                    final_hash_error is None
                    and final_unchanged
                    and all(value == expected_sha for value in sha_actual.values())
                )
                _add(
                    checks,
                    "e2_full_byte_sha_start_manifest_end",
                    sha_passed,
                    {
                        "size": source.lineage.artifact_size,
                        "sha256": expected_sha,
                        "sidecars": [],
                    },
                    {
                        "size_before_final_hash": final_size_before_hash,
                        "size_after_final_hash": final_size,
                        "sidecars_before_final_hash": final_sidecars_before_hash,
                        "sidecars_after_final_hash": final_sidecars,
                        "hashes": sha_actual,
                        "error": final_hash_error,
                    },
                    full_byte_rehash=True,
                )
                final_unchanged = final_unchanged and sha_passed
            _add(
                checks,
                "e2_unchanged_after_full_validation_read",
                final_unchanged,
                {
                    "size": source.lineage.artifact_size,
                    "sha256": (
                        source.lineage.artifact_sha256
                        if selection_mode == "full"
                        else "not_rehashed_in_sample_mode"
                    ),
                    "sidecars": [],
                },
                {
                    "size": final_size,
                    "sha256": (
                        final_sha
                        if selection_mode == "full"
                        else "not_rehashed_in_sample_mode"
                    ),
                    "sidecars": final_sidecars,
                    "error": final_hash_error,
                },
            )
            if input_files and not final_unchanged:
                input_files = (
                    replace(
                        input_files[0],
                        size_bytes=final_size,
                        sha256=final_sha or input_files[0].sha256,
                        sidecars=final_sidecars,
                        unchanged_during_read=False,
                        passed=False,
                        error=(
                            "E2 byte identity, size, or SQLite sidecars changed "
                            "during validation"
                        ),
                    ),
                    *input_files[1:],
                )
    finally:
        if source is not None:
            source.close()
        connection.close()
    output_sidecars_after = tuple(str(item) for item in sqlite_sidecars(target))
    output_size_after = target.stat().st_size
    _add(
        checks,
        "output_unchanged_and_no_sidecars_at_close",
        output_size_after == output_size_before and not output_sidecars_after,
        {"size": output_size_before, "sidecars": []},
        {"size": output_size_after, "sidecars": output_sidecars_after},
    )
    return SelectionValidationReport(
        artifact_path=str(target),
        run_id=run_id,
        logical_sha256=logical_sha,
        table_manifests=table_manifests,
        input_files=input_files,
        checks=tuple(checks),
    )


__all__ = [
    "EXPECTED_APPLICATION_ID",
    "EXPECTED_INDEX_NAMES",
    "EXPECTED_SCHEMA_VERSION",
    "EXPECTED_TABLE_COLUMNS",
    "EXPECTED_TRIGGER_NAMES",
    "FORBIDDEN_POLICY_TABLES",
    "InputFileCheck",
    "LOGICAL_MANIFEST_VERSION",
    "LOGICAL_SELECTION_TABLES",
    "SelectionValidationError",
    "SelectionValidationReport",
    "VALIDATOR_VERSION",
    "ValidationCheck",
    "logical_selection_manifest",
    "open_immutable",
    "sha256_file",
    "sqlite_sidecars",
    "validate_e3_artifact",
]
