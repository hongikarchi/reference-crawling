"""Independent, immutable validator for terminal E2 image-evidence artifacts.

The validator intentionally does not import the E2 pipeline.  It reimplements
the logical-manifest contract and evidence accounting so a shared builder bug
cannot make both production and validation agree by construction.  It opens
the artifact and every recorded SQLite input read-only, performs no network or
Vision work, and never selects a representative image or final entity match.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from canonical.cross_source_image_evidence import (
    E2_EVIDENCE_VERSION,
    SAMPLE_POLICY_VERSION,
    canonical_json,
    canonical_sha256,
    classify_phash_pair,
    deterministic_sample_score,
    phash_band_keys,
    stable_edge_id,
    stable_exact_id,
    stable_phash_id,
)


VALIDATOR_VERSION = "archibe-e2-cross-source-image-evidence-validator-v1"
LOGICAL_MANIFEST_VERSION = "archibe-e2-evidence-logical-manifest-v1"
HASH_CHUNK_SIZE = 8 * 1024 * 1024
EXPECTED_APPLICATION_ID = int.from_bytes(b"E2IE", "big")
EXPECTED_SCHEMA_VERSION = 1

FORBIDDEN_POLICY_TABLES = frozenset(
    {
        "representatives",
        "representative_images",
        "vision_queue",
        "vision_tasks",
        "final_matches",
        "merge_decisions",
    }
)

# Keep this tuple frozen and independent from the builder.  Runtime tables and
# timestamps are excluded, while every source/evidence/selection row remains.
LOGICAL_EVIDENCE_TABLES = (
    "source_projects",
    "source_buildings",
    "source_project_buildings",
    "assets",
    "project_asset_occurrences",
    "project_assets",
    "building_assets",
    "exact_pixel_clusters",
    "exact_pixel_cluster_members",
    "phash_nodes",
    "phash_node_members",
    "phash_candidates",
    "phash_edges",
    "metadata_building_pairs",
    "cross_source_project_image_evidence",
    "candidate_image_evidence",
    "cross_source_building_candidates",
    "smoke_manifests",
    "smoke_manifest_items",
)

REQUIRED_CONTROL_TABLES = frozenset(
    {"e2_runs", "e2_inputs", "e2_metrics", "e2_validations", "build_checkpoints"}
)

# Frozen independently from the builder/sidecar implementation.  Any table or
# column change requires a new schema version rather than silently extending
# the evidence-only v1 artifact.
EXPECTED_TABLE_COLUMNS = {
    "e2_runs": (
        "run_id", "contract_version", "builder_version", "selection_mode",
        "sample_size", "sample_seed", "ordered_selection_manifest_sha256",
        "config_json", "status", "started_at", "completed_at", "error",
    ),
    "e2_inputs": (
        "run_id", "input_name", "source", "input_role", "file_path",
        "size_bytes", "sha256_before", "sha256_after", "application_id",
        "user_version", "schema_manifest_sha256", "recorded_at", "detail_json",
    ),
    "source_projects": (
        "run_id", "source", "source_project_id", "canonical_url", "slug",
        "global_id", "name", "normalized_name", "country", "region",
        "locality", "completion_year_min", "completion_year_max",
        "source_record_sha256", "metadata_json",
    ),
    "source_buildings": (
        "run_id", "source", "source_building_id", "canonical_url", "slug",
        "global_id", "name", "normalized_name", "country", "region",
        "locality", "completion_year_min", "completion_year_max",
        "source_record_sha256", "metadata_json",
    ),
    "source_project_buildings": (
        "run_id", "source", "source_project_id", "source_building_id",
        "membership_reason", "membership_ordinal", "source_record_sha256",
        "detail_json",
    ),
    "assets": (
        "run_id", "source", "source_asset_id", "e1_run_id",
        "fingerprint_status", "canonical_url", "fetch_url", "final_url",
        "raw_response_sha256", "normalized_pixel_sha256", "phash_hex",
        "original_width", "original_height", "normalized_width",
        "normalized_height", "source_record_sha256", "provenance_json",
        "error_kind", "error_message",
    ),
    "project_asset_occurrences": (
        "run_id", "source", "occurrence_id", "source_project_id",
        "raw_asset_key", "source_asset_id", "resolution_status", "role",
        "ordinal", "occurrence_url", "source_record_sha256", "detail_json",
    ),
    "project_assets": (
        "run_id", "source", "source_project_id", "source_asset_id",
        "occurrence_count", "roles_json", "first_ordinal",
        "relation_record_sha256",
    ),
    "building_assets": (
        "run_id", "source", "source_building_id", "source_asset_id",
        "project_count", "occurrence_count", "roles_json",
        "relation_record_sha256",
    ),
    "exact_pixel_clusters": (
        "run_id", "cluster_id", "normalized_pixel_sha256", "member_count",
        "source_count", "project_count", "building_count", "is_cross_source",
    ),
    "exact_pixel_cluster_members": (
        "run_id", "cluster_id", "source", "source_asset_id",
    ),
    "phash_nodes": (
        "run_id", "node_id", "phash_hex", "member_count", "source_count",
        "is_cross_source",
    ),
    "phash_node_members": (
        "run_id", "node_id", "source", "source_asset_id",
    ),
    "phash_candidates": (
        "run_id", "candidate_id", "left_node_id", "right_node_id",
        "candidate_scope", "shared_band_count", "recomputed_distance",
        "passed_threshold", "candidate_record_sha256", "detail_json",
    ),
    "phash_edges": (
        "run_id", "edge_id", "left_node_id", "right_node_id",
        "hamming_distance", "edge_scope", "candidate_id",
        "edge_record_sha256", "detail_json",
    ),
    "metadata_building_pairs": (
        "run_id", "metadata_pair_id", "left_source", "left_source_building_id",
        "right_source", "right_source_building_id", "blocker_version",
        "discovery_reason", "normalized_name_equal", "country_equal",
        "locality_equal", "year_overlap", "metadata_record_sha256",
        "evidence_json",
    ),
    "cross_source_project_image_evidence": (
        "run_id", "project_pair_id", "left_source", "left_source_project_id",
        "right_source", "right_source_project_id", "exact_asset_pair_count",
        "identical_phash_pair_count", "phash_le8_pair_count",
        "phash_9_16_pair_count", "min_phash_distance",
        "evidence_record_sha256", "evidence_json",
    ),
    "candidate_image_evidence": (
        "run_id", "evidence_id", "building_candidate_id", "project_pair_id",
        "left_source", "left_source_asset_id", "right_source",
        "right_source_asset_id", "evidence_kind", "exact_cluster_id",
        "phash_edge_id", "phash_distance", "direct_evidence",
        "evidence_record_sha256", "detail_json",
    ),
    "cross_source_building_candidates": (
        "run_id", "building_candidate_id", "left_source",
        "left_source_building_id", "right_source", "right_source_building_id",
        "metadata_pair_id", "exact_asset_pair_count",
        "identical_phash_pair_count", "phash_le8_pair_count",
        "phash_9_16_pair_count", "min_phash_distance",
        "discovery_basis_json", "candidate_record_sha256",
    ),
    "e2_metrics": (
        "run_id", "phase", "metric_name", "stratum_json", "value_integer",
        "value_real", "value_text", "recorded_at",
    ),
    "e2_validations": (
        "run_id", "validation_name", "severity", "passed", "expected",
        "actual", "detail_json", "recorded_at",
    ),
    "smoke_manifests": (
        "run_id", "manifest_name", "sample_size", "sample_seed",
        "selection_version", "ordered_manifest_sha256", "selection_scope_json",
        "created_at",
    ),
    "smoke_manifest_items": (
        "run_id", "manifest_name", "selection_rank", "entity_kind", "source",
        "source_entity_id", "stratum", "score_sha256", "item_record_sha256",
        "detail_json",
    ),
    "build_checkpoints": (
        "run_id", "phase", "cursor_json", "completed_rows", "phase_complete",
        "updated_at",
    ),
}


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
class EvidenceValidationReport:
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
            raise EvidenceValidationError(
                "E2 validation failed: " + ", ".join(self.failed_check_names)
            )


class EvidenceValidationError(RuntimeError):
    """Raised by :meth:`EvidenceValidationReport.require_valid`."""


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
        for suffix in ("-wal", "-shm", "-journal")
        if (candidate := Path(str(target) + suffix)).exists()
    )


def open_immutable(path: Path | str) -> sqlite3.Connection:
    uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


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


def logical_evidence_manifest(
    connection: sqlite3.Connection, run_id: str
) -> tuple[str, dict[str, dict[str, Any]]]:
    """Independently stream the frozen logical evidence manifest."""

    table_manifests: dict[str, dict[str, Any]] = {}
    for table in LOGICAL_EVIDENCE_TABLES:
        info = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        if not info:
            raise sqlite3.DatabaseError(f"logical table is missing: {table}")
        columns = [str(row[1]) for row in info]
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
    return canonical_sha256(table_manifests), table_manifests


def _selection_manifest(connection: sqlite3.Connection, run_id: str) -> str:
    digest = hashlib.sha256()
    for row in connection.execute(
        """
        SELECT source,source_asset_id,fingerprint_status
        FROM assets WHERE run_id=? ORDER BY source,source_asset_id
        """,
        (run_id,),
    ):
        record = canonical_json(
            {"asset_id": str(row[1]), "source": str(row[0]), "status": str(row[2])}
        )
        digest.update(record.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _pair_id(prefix: str, left_id: str, right_id: str) -> str:
    return prefix + canonical_sha256(
        {"left": left_id, "right": right_id, "version": E2_EVIDENCE_VERSION}
    )


def _evidence_id(
    building_candidate_id: str,
    divisare_asset_id: str,
    architizer_asset_id: str,
    evidence_kind: str,
) -> str:
    return "e2ie_" + canonical_sha256(
        {
            "architizer_asset_id": architizer_asset_id,
            "building_candidate_id": building_candidate_id,
            "divisare_asset_id": divisare_asset_id,
            "evidence_kind": evidence_kind,
            "version": E2_EVIDENCE_VERSION,
        }
    )


def _add(
    checks: list[ValidationCheck],
    name: str,
    passed: bool,
    expected: Any,
    actual: Any,
    **detail: Any,
) -> None:
    checks.append(ValidationCheck(name, bool(passed), expected, actual, detail))


def _schema_object_names(connection: sqlite3.Connection, kind: str) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type=?", (kind,)
        )
    }


def _table_columns(
    connection: sqlite3.Connection, table: str
) -> tuple[str, ...]:
    return tuple(
        str(row[1])
        for row in connection.execute(f'PRAGMA table_info("{table}")')
    )


def _validate_evidence_only_policy(
    connection: sqlite3.Connection,
    run_id: str,
    checks: list[ValidationCheck],
) -> None:
    row = connection.execute(
        "SELECT config_json FROM e2_runs WHERE run_id=?", (run_id,)
    ).fetchone()
    config: object = None
    if row is not None:
        try:
            config = json.loads(str(row[0]))
        except (TypeError, ValueError):
            config = None
    config_passed = (
        isinstance(config, dict)
        and config.get("representative_selection") is False
        and type(config.get("network_requests")) in {int, float}
        and config.get("network_requests") == 0
        and type(config.get("vision_requests")) in {int, float}
        and config.get("vision_requests") == 0
    )
    _add(
        checks,
        "evidence_only_run_config",
        config_passed,
        {
            "representative_selection": False,
            "network_requests": 0,
            "vision_requests": 0,
        },
        config,
    )

    required = {"network_requests", "vision_requests", "llm_requests"}
    rows = connection.execute(
        """
        SELECT phase,metric_name,stratum_json,
               value_integer,value_real,value_text
        FROM e2_metrics
        WHERE run_id=? AND metric_name IN (
          'network_requests','vision_requests','llm_requests'
        )
        ORDER BY metric_name,phase,stratum_json
        """,
        (run_id,),
    ).fetchall()
    observed = {
        str(metric[1]): {
            "phase": str(metric[0]),
            "stratum_json": str(metric[2]),
            "value_integer": metric[3],
            "value_real": metric[4],
            "value_text": metric[5],
        }
        for metric in rows
    }
    metrics_passed = (
        len(rows) == 3
        and set(observed) == required
        and all(
            value["phase"] == "validation"
            and value["stratum_json"] == "{}"
            and value["value_integer"] == 0
            and value["value_real"] is None
            and value["value_text"] is None
            for value in observed.values()
        )
    )
    _add(
        checks,
        "evidence_only_zero_request_metrics",
        metrics_passed,
        {
            name: {
                "phase": "validation",
                "stratum_json": "{}",
                "value_integer": 0,
                "value_real": None,
                "value_text": None,
            }
            for name in sorted(required)
        },
        observed,
        row_count=len(rows),
    )


def _validate_inputs(
    connection: sqlite3.Connection,
    run_id: str,
    checks: list[ValidationCheck],
    *,
    verify_file_hashes: bool,
) -> tuple[InputFileCheck, ...]:
    results: list[InputFileCheck] = []
    rows = connection.execute(
        """
        SELECT input_name,file_path,size_bytes,sha256_before,sha256_after
        FROM e2_inputs WHERE run_id=? ORDER BY input_name
        """,
        (run_id,),
    ).fetchall()
    expected_roles = {
        "architizer_curated",
        "architizer_e1",
        "divisare_curated",
        "divisare_e1",
    }
    names = {str(row[0]) for row in rows}
    _add(checks, "required_input_roles", names == expected_roles, expected_roles, names)
    for row in rows:
        name = str(row[0])
        path = Path(str(row[1])).resolve()
        expected_size = int(row[2])
        before = str(row[3])
        after = str(row[4]) if row[4] is not None else None
        sidecars_before = sqlite_sidecars(path)
        actual_size: int | None = None
        actual_sha: str | None = None
        unchanged = False
        error: str | None = None
        try:
            stat_before = path.stat()
            actual_size = stat_before.st_size
            if verify_file_hashes:
                actual_sha = sha256_file(path)
            sidecars_after = sqlite_sidecars(path)
            stat_after = path.stat()
            unchanged = (
                stat_before.st_size == stat_after.st_size
                and stat_before.st_mtime_ns == stat_after.st_mtime_ns
                and sidecars_before == sidecars_after
            )
            sidecars = tuple(str(item) for item in sidecars_after)
            passed = (
                after == before
                and actual_size == expected_size
                and not sidecars
                and unchanged
                and (not verify_file_hashes or actual_sha == before)
            )
        except OSError as exc:
            passed = False
            sidecars = tuple(str(item) for item in sidecars_before)
            error = f"{type(exc).__name__}: {exc}"
        results.append(
            InputFileCheck(
                input_name=name,
                path=str(path),
                size_bytes=actual_size,
                sha256=actual_sha,
                sidecars=sidecars,
                unchanged_during_read=unchanged,
                passed=passed,
                error=error,
            )
        )
    _add(
        checks,
        "immutable_input_files",
        bool(results) and all(result.passed for result in results),
        "recorded path/size/SHA unchanged and no sidecars",
        [result.__dict__ for result in results],
        hashes_recomputed=verify_file_hashes,
    )
    return tuple(results)


def _validate_asset_accounting(
    connection: sqlite3.Connection, run_id: str, checks: list[ValidationCheck]
) -> None:
    success = int(
        connection.execute(
            "SELECT count(*) FROM assets WHERE run_id=? AND fingerprint_status='success'",
            (run_id,),
        ).fetchone()[0]
    )
    malformed = int(
        connection.execute(
            """
            SELECT count(*) FROM assets WHERE run_id=? AND (
              (fingerprint_status='success' AND
               (raw_response_sha256 IS NULL OR normalized_pixel_sha256 IS NULL
                OR phash_hex IS NULL))
              OR (fingerprint_status<>'success' AND
                  (normalized_pixel_sha256 IS NOT NULL OR phash_hex IS NOT NULL))
            )
            """,
            (run_id,),
        ).fetchone()[0]
    )
    _add(checks, "asset_hash_contract", malformed == 0, 0, malformed)

    node_members = int(
        connection.execute(
            "SELECT count(*) FROM phash_node_members WHERE run_id=?", (run_id,)
        ).fetchone()[0]
    )
    missing_or_wrong = int(
        connection.execute(
            """
            SELECT count(*) FROM assets a WHERE a.run_id=? AND (
              (a.fingerprint_status='success' AND 1<>(
                SELECT count(*) FROM phash_node_members m
                WHERE m.run_id=a.run_id AND m.source=a.source
                  AND m.source_asset_id=a.source_asset_id))
              OR (a.fingerprint_status<>'success' AND 0<>(
                SELECT count(*) FROM phash_node_members m
                WHERE m.run_id=a.run_id AND m.source=a.source
                  AND m.source_asset_id=a.source_asset_id))
            )
            """,
            (run_id,),
        ).fetchone()[0]
    )
    _add(
        checks,
        "phash_member_full_accounting",
        success == node_members and missing_or_wrong == 0,
        {"success_assets": success, "membership_mismatches": 0},
        {"node_members": node_members, "membership_mismatches": missing_or_wrong},
    )

    node_mismatches = 0
    for row in connection.execute(
        """
        SELECT n.node_id,n.phash_hex,n.member_count,n.source_count,n.is_cross_source,
               count(m.source_asset_id),count(DISTINCT m.source),
               sum(CASE WHEN a.phash_hex<>n.phash_hex THEN 1 ELSE 0 END)
        FROM phash_nodes n
        LEFT JOIN phash_node_members m
          ON m.run_id=n.run_id AND m.node_id=n.node_id
        LEFT JOIN assets a ON a.run_id=m.run_id AND a.source=m.source
                          AND a.source_asset_id=m.source_asset_id
        WHERE n.run_id=? GROUP BY n.node_id ORDER BY n.node_id
        """,
        (run_id,),
    ):
        actual_members = int(row[5])
        actual_sources = int(row[6])
        node_mismatches += int(
            str(row[0]) != stable_phash_id(str(row[1]))
            or int(row[2]) != actual_members
            or int(row[3]) != actual_sources
            or int(row[4]) != int(actual_sources == 2)
            or int(row[7] or 0) != 0
        )
    distinct_phashes = int(
        connection.execute(
            """
            SELECT count(DISTINCT phash_hex) FROM assets
            WHERE run_id=? AND fingerprint_status='success'
            """,
            (run_id,),
        ).fetchone()[0]
    )
    node_count = int(
        connection.execute(
            "SELECT count(*) FROM phash_nodes WHERE run_id=?", (run_id,)
        ).fetchone()[0]
    )
    _add(
        checks,
        "phash_node_full_accounting",
        node_mismatches == 0 and node_count == distinct_phashes,
        {"distinct_phashes": distinct_phashes, "mismatches": 0},
        {"nodes": node_count, "mismatches": node_mismatches},
    )

    expected_cluster_count, expected_cluster_members = connection.execute(
        """
        SELECT count(*),coalesce(sum(n),0) FROM (
          SELECT count(*) AS n FROM assets
          WHERE run_id=? AND fingerprint_status='success'
          GROUP BY normalized_pixel_sha256 HAVING count(*)>1
        )
        """,
        (run_id,),
    ).fetchone()
    actual_cluster_count = int(
        connection.execute(
            "SELECT count(*) FROM exact_pixel_clusters WHERE run_id=?", (run_id,)
        ).fetchone()[0]
    )
    actual_cluster_members = int(
        connection.execute(
            "SELECT count(*) FROM exact_pixel_cluster_members WHERE run_id=?", (run_id,)
        ).fetchone()[0]
    )
    cluster_mismatches = 0
    for row in connection.execute(
        """
        WITH member_aggregate AS (
          SELECT m.cluster_id,count(*) AS member_count,
                 count(DISTINCT m.source) AS source_count,
                 sum(CASE WHEN a.normalized_pixel_sha256<>c.normalized_pixel_sha256
                          THEN 1 ELSE 0 END) AS hash_mismatches
          FROM exact_pixel_cluster_members m
          JOIN exact_pixel_clusters c
            ON c.run_id=m.run_id AND c.cluster_id=m.cluster_id
          JOIN assets a ON a.run_id=m.run_id AND a.source=m.source
                       AND a.source_asset_id=m.source_asset_id
          WHERE m.run_id=? GROUP BY m.cluster_id
        ), project_aggregate AS (
          SELECT cluster_id,count(*) AS project_count FROM (
            SELECT DISTINCT m.cluster_id,p.source,p.source_project_id
            FROM exact_pixel_cluster_members m JOIN project_assets p
              ON p.run_id=m.run_id AND p.source=m.source
             AND p.source_asset_id=m.source_asset_id
            WHERE m.run_id=?
          ) GROUP BY cluster_id
        ), building_aggregate AS (
          SELECT cluster_id,count(*) AS building_count FROM (
            SELECT DISTINCT m.cluster_id,b.source,b.source_building_id
            FROM exact_pixel_cluster_members m JOIN building_assets b
              ON b.run_id=m.run_id AND b.source=m.source
             AND b.source_asset_id=m.source_asset_id
            WHERE m.run_id=?
          ) GROUP BY cluster_id
        )
        SELECT c.cluster_id,c.normalized_pixel_sha256,c.member_count,c.source_count,
               c.project_count,c.building_count,c.is_cross_source,
               coalesce(m.member_count,0),coalesce(m.source_count,0),
               coalesce(p.project_count,0),coalesce(b.building_count,0),
               coalesce(m.hash_mismatches,0)
        FROM exact_pixel_clusters c
        LEFT JOIN member_aggregate m ON m.cluster_id=c.cluster_id
        LEFT JOIN project_aggregate p ON p.cluster_id=c.cluster_id
        LEFT JOIN building_aggregate b ON b.cluster_id=c.cluster_id
        WHERE c.run_id=? ORDER BY c.cluster_id
        """,
        (run_id, run_id, run_id, run_id),
    ):
        member_count = int(row[7])
        source_count = int(row[8])
        cluster_mismatches += int(
            str(row[0]) != stable_exact_id(str(row[1]))
            or int(row[2]) != member_count
            or member_count < 2
            or int(row[3]) != source_count
            or int(row[4]) != int(row[9])
            or int(row[5]) != int(row[10])
            or int(row[6]) != int(source_count == 2)
            or int(row[11] or 0) != 0
        )
    _add(
        checks,
        "exact_cluster_full_accounting",
        int(expected_cluster_count) == actual_cluster_count
        and int(expected_cluster_members) == actual_cluster_members
        and cluster_mismatches == 0,
        {
            "clusters": int(expected_cluster_count),
            "members": int(expected_cluster_members),
            "mismatches": 0,
        },
        {
            "clusters": actual_cluster_count,
            "members": actual_cluster_members,
            "mismatches": cluster_mismatches,
        },
    )


def _validate_phash_pairs(
    connection: sqlite3.Connection, run_id: str, checks: list[ValidationCheck]
) -> None:
    candidate_count = candidate_mismatches = 0
    for row in connection.execute(
        """
        SELECT c.candidate_id,c.left_node_id,c.right_node_id,c.candidate_scope,
               c.shared_band_count,c.recomputed_distance,c.passed_threshold,
               c.candidate_record_sha256,l.phash_hex,r.phash_hex
        FROM phash_candidates c
        JOIN phash_nodes l ON l.run_id=c.run_id AND l.node_id=c.left_node_id
        JOIN phash_nodes r ON r.run_id=c.run_id AND r.node_id=c.right_node_id
        WHERE c.run_id=? ORDER BY c.candidate_id
        """,
        (run_id,),
    ):
        scope = str(row[3])
        decision = classify_phash_pair(
            str(row[8]), str(row[9]), metadata_blocked=scope == "metadata_le16"
        )
        if scope == "global_le8":
            kind = "phash-global-le8-candidate"
            threshold = 8
            shared = sum(
                left == right
                for left, right in zip(
                    phash_band_keys(str(row[8])), phash_band_keys(str(row[9]))
                )
            )
        else:
            kind = "phash-metadata-le16-candidate"
            threshold = 16
            shared = 0
        candidate_id = stable_edge_id(str(row[1]), str(row[2]), kind)
        record = {
            "candidate_id": candidate_id,
            "distance": decision.distance,
            "left_node_id": str(row[1]),
            "right_node_id": str(row[2]),
            "scope": scope,
        }
        if scope == "global_le8":
            record["shared_band_count"] = shared
        candidate_mismatches += int(
            str(row[0]) != candidate_id
            or int(row[4]) != shared
            or int(row[5]) != decision.distance
            or int(row[6]) != int(decision.distance <= threshold)
            or str(row[7]) != canonical_sha256(record)
            or (scope == "global_le8" and shared < 1)
        )
        candidate_count += 1
    _add(
        checks,
        "phash_candidate_recomputation",
        candidate_mismatches == 0,
        0,
        candidate_mismatches,
        checked=candidate_count,
    )

    edge_count = edge_mismatches = 0
    for row in connection.execute(
        """
        SELECT e.edge_id,e.left_node_id,e.right_node_id,e.hamming_distance,
               e.edge_scope,e.candidate_id,e.edge_record_sha256,
               c.candidate_scope,c.recomputed_distance,l.phash_hex,r.phash_hex
        FROM phash_edges e
        JOIN phash_candidates c
          ON c.run_id=e.run_id AND c.candidate_id=e.candidate_id
        JOIN phash_nodes l ON l.run_id=e.run_id AND l.node_id=e.left_node_id
        JOIN phash_nodes r ON r.run_id=e.run_id AND r.node_id=e.right_node_id
        WHERE e.run_id=? ORDER BY e.edge_id
        """,
        (run_id,),
    ):
        scope = str(row[4])
        decision = classify_phash_pair(
            str(row[9]), str(row[10]), metadata_blocked=scope == "metadata_9_16"
        )
        if scope == "global_le8":
            kind = "phash-global-le8"
            valid_distance = 1 <= decision.distance <= 8
            candidate_scope = "global_le8"
        else:
            kind = "phash-metadata-9-16"
            valid_distance = 9 <= decision.distance <= 16
            candidate_scope = "metadata_le16"
        edge_id = stable_edge_id(str(row[1]), str(row[2]), kind)
        record = {
            "candidate_id": str(row[5]),
            "distance": decision.distance,
            "edge_id": edge_id,
            "left_node_id": str(row[1]),
            "right_node_id": str(row[2]),
            "scope": scope,
        }
        edge_mismatches += int(
            str(row[0]) != edge_id
            or int(row[3]) != decision.distance
            or not valid_distance
            or str(row[7]) != candidate_scope
            or int(row[8]) != decision.distance
            or str(row[6]) != canonical_sha256(record)
        )
        edge_count += 1
    missing_edges = int(
        connection.execute(
            """
            SELECT count(*) FROM phash_candidates c WHERE c.run_id=? AND (
              (c.candidate_scope='global_le8' AND c.recomputed_distance BETWEEN 1 AND 8
               AND NOT EXISTS(SELECT 1 FROM phash_edges e WHERE e.run_id=c.run_id
                 AND e.left_node_id=c.left_node_id
                 AND e.right_node_id=c.right_node_id
                 AND e.edge_scope='global_le8'))
              OR
              (c.candidate_scope='metadata_le16' AND c.recomputed_distance BETWEEN 9 AND 16
               AND NOT EXISTS(SELECT 1 FROM phash_edges e WHERE e.run_id=c.run_id
                 AND e.left_node_id=c.left_node_id
                 AND e.right_node_id=c.right_node_id
                 AND e.edge_scope='metadata_9_16'))
            )
            """,
            (run_id,),
        ).fetchone()[0]
    )
    _add(
        checks,
        "phash_edge_recomputation",
        edge_mismatches == 0 and missing_edges == 0,
        {"mismatches": 0, "missing_required_edges": 0},
        {"mismatches": edge_mismatches, "missing_required_edges": missing_edges},
        checked=edge_count,
    )


def _building_nodes(
    connection: sqlite3.Connection, run_id: str, source: str, building_id: str
) -> dict[str, str]:
    return {
        str(row[0]): str(row[1])
        for row in connection.execute(
            """
            SELECT DISTINCT n.node_id,n.phash_hex FROM building_assets b
            JOIN phash_node_members m
              ON m.run_id=b.run_id AND m.source=b.source
             AND m.source_asset_id=b.source_asset_id
            JOIN phash_nodes n ON n.run_id=m.run_id AND n.node_id=m.node_id
            WHERE b.run_id=? AND b.source=? AND b.source_building_id=?
            ORDER BY n.node_id
            """,
            (run_id, source, building_id),
        )
    }


def _validate_metadata_cartesian(
    connection: sqlite3.Connection, run_id: str, checks: list[ValidationCheck]
) -> None:
    checked = mismatches = 0
    for row in connection.execute(
        """
        SELECT metadata_pair_id,left_source,left_source_building_id,
               right_source,right_source_building_id,evidence_json
        FROM metadata_building_pairs WHERE run_id=? ORDER BY metadata_pair_id
        """,
        (run_id,),
    ):
        left = _building_nodes(connection, run_id, str(row[1]), str(row[2]))
        right = _building_nodes(connection, run_id, str(row[3]), str(row[4]))
        identical = review = above = compared = 0
        for left_id, left_phash in left.items():
            for right_id, right_phash in right.items():
                if left_id == right_id:
                    identical += 1
                    continue
                compared += 1
                distance = classify_phash_pair(
                    left_phash, right_phash, metadata_blocked=True
                ).distance
                review += int(9 <= distance <= 16)
                above += int(distance > 16)
        expected = {
            "architizer_node_count": len(right),
            "compared_distinct_node_pairs": compared,
            "distance_9_16_pairs": review,
            "distance_above_16_pairs": above,
            "divisare_node_count": len(left),
            "identical_node_pairs": identical,
            "total_cartesian_node_pairs": len(left) * len(right),
        }
        try:
            actual = json.loads(str(row[5])).get("phash_cartesian_accounting")
        except (json.JSONDecodeError, AttributeError, TypeError):
            actual = None
        mismatches += int(
            actual != expected
            or str(row[1]) != "divisare"
            or str(row[3]) != "architizer"
        )
        checked += 1
    _add(
        checks,
        "metadata_cartesian_accounting",
        mismatches == 0,
        0,
        mismatches,
        checked=checked,
    )


def _validate_building_candidates(
    connection: sqlite3.Connection, run_id: str, checks: list[ValidationCheck]
) -> None:
    mismatches = checked = 0
    for row in connection.execute(
        """
        WITH aggregate AS (
          SELECT building_candidate_id,
            sum(evidence_kind='exact_pixel') AS exact_count,
            sum(evidence_kind='identical_phash') AS identical_count,
            sum(evidence_kind='phash_le8') AS le8_count,
            sum(evidence_kind='phash_9_16') AS review_count,
            min(phash_distance) AS min_distance
          FROM candidate_image_evidence WHERE run_id=? GROUP BY building_candidate_id
        )
        SELECT c.building_candidate_id,c.left_source_building_id,
               c.right_source_building_id,c.metadata_pair_id,c.left_source,c.right_source,
               c.exact_asset_pair_count,c.identical_phash_pair_count,
               c.phash_le8_pair_count,c.phash_9_16_pair_count,
               c.min_phash_distance,c.candidate_record_sha256,
               coalesce(a.exact_count,0),coalesce(a.identical_count,0),
               coalesce(a.le8_count,0),coalesce(a.review_count,0),a.min_distance
        FROM cross_source_building_candidates c LEFT JOIN aggregate a
          ON a.building_candidate_id=c.building_candidate_id
        WHERE c.run_id=? ORDER BY c.building_candidate_id
        """,
        (run_id, run_id),
    ):
        counts = {
            "exact_pixel": int(row[12]),
            "identical_phash": int(row[13]),
            "phash_le8": int(row[14]),
            "phash_9_16": int(row[15]),
        }
        candidate_id = _pair_id("e2bc_", str(row[1]), str(row[2]))
        record = {
            "architizer_building_id": str(row[2]),
            "candidate_id": candidate_id,
            "counts": counts,
            "divisare_building_id": str(row[1]),
            "metadata_pair_id": str(row[3]) if row[3] is not None else None,
            "min_phash_distance": int(row[16]) if row[16] is not None else None,
        }
        mismatches += int(
            str(row[0]) != candidate_id
            or str(row[4]) != "divisare"
            or str(row[5]) != "architizer"
            or tuple(int(row[index]) for index in range(6, 10))
            != tuple(counts[key] for key in ("exact_pixel", "identical_phash", "phash_le8", "phash_9_16"))
            or (int(row[10]) if row[10] is not None else None)
            != (int(row[16]) if row[16] is not None else None)
            or str(row[11]) != canonical_sha256(record)
        )
        checked += 1
    _add(
        checks,
        "building_candidate_aggregate_counts",
        mismatches == 0,
        0,
        mismatches,
        checked=checked,
    )


def _validate_direct_image_evidence(
    connection: sqlite3.Connection, run_id: str, checks: list[ValidationCheck]
) -> None:
    checked = mismatches = 0
    for row in connection.execute(
        """
        SELECT e.evidence_id,e.building_candidate_id,e.left_source,
               e.left_source_asset_id,e.right_source,e.right_source_asset_id,
               e.evidence_kind,e.exact_cluster_id,e.phash_edge_id,
               e.phash_distance,e.evidence_record_sha256,
               l.normalized_pixel_sha256,l.phash_hex,
               r.normalized_pixel_sha256,r.phash_hex,
               c.normalized_pixel_sha256,
               pe.hamming_distance
        FROM candidate_image_evidence e
        JOIN assets l ON l.run_id=e.run_id AND l.source=e.left_source
                     AND l.source_asset_id=e.left_source_asset_id
        JOIN assets r ON r.run_id=e.run_id AND r.source=e.right_source
                     AND r.source_asset_id=e.right_source_asset_id
        LEFT JOIN exact_pixel_clusters c
          ON c.run_id=e.run_id AND c.cluster_id=e.exact_cluster_id
        LEFT JOIN phash_edges pe
          ON pe.run_id=e.run_id AND pe.edge_id=e.phash_edge_id
        WHERE e.run_id=? ORDER BY e.evidence_id
        """,
        (run_id,),
    ):
        kind = str(row[6])
        distance = classify_phash_pair(
            str(row[12]), str(row[14]), metadata_blocked=kind == "phash_9_16"
        ).distance
        evidence_id = _evidence_id(str(row[1]), str(row[3]), str(row[5]), kind)
        record = {
            "architizer_asset_id": str(row[5]),
            "building_candidate_id": str(row[1]),
            "divisare_asset_id": str(row[3]),
            "evidence_id": evidence_id,
            "evidence_kind": kind,
            "exact_cluster_id": str(row[7]) if row[7] is not None else None,
            "phash_distance": int(row[9]) if row[9] is not None else None,
            "phash_edge_id": str(row[8]) if row[8] is not None else None,
        }
        valid = (
            str(row[0]) == evidence_id
            and str(row[2]) == "divisare"
            and str(row[4]) == "architizer"
            and str(row[10]) == canonical_sha256(record)
        )
        if kind == "exact_pixel":
            valid = valid and row[11] == row[13] == row[15]
        elif kind == "identical_phash":
            valid = valid and distance == 0 and int(row[9]) == 0
        elif kind == "phash_le8":
            valid = (
                valid and 1 <= distance <= 8 and int(row[9]) == distance
                and int(row[16]) == distance
            )
        elif kind == "phash_9_16":
            valid = (
                valid and 9 <= distance <= 16 and int(row[9]) == distance
                and int(row[16]) == distance
            )
        else:
            valid = False
        mismatches += int(not valid)
        checked += 1
    unattached = int(
        connection.execute(
            """
            SELECT count(*) FROM candidate_image_evidence e
            JOIN cross_source_building_candidates c
              ON c.run_id=e.run_id AND c.building_candidate_id=e.building_candidate_id
            WHERE e.run_id=? AND (
              NOT EXISTS(SELECT 1 FROM building_assets b WHERE b.run_id=e.run_id
                AND b.source=e.left_source AND b.source_building_id=c.left_source_building_id
                AND b.source_asset_id=e.left_source_asset_id)
              OR NOT EXISTS(SELECT 1 FROM building_assets b WHERE b.run_id=e.run_id
                AND b.source=e.right_source AND b.source_building_id=c.right_source_building_id
                AND b.source_asset_id=e.right_source_asset_id)
            )
            """,
            (run_id,),
        ).fetchone()[0]
    )
    edge_membership_mismatches = int(
        connection.execute(
            """
            SELECT count(*) FROM candidate_image_evidence e
            JOIN phash_edges pe
              ON pe.run_id=e.run_id AND pe.edge_id=e.phash_edge_id
            WHERE e.run_id=? AND e.evidence_kind IN ('phash_le8','phash_9_16')
              AND NOT (
                EXISTS(SELECT 1 FROM phash_node_members m
                  WHERE m.run_id=e.run_id AND m.source=e.left_source
                    AND m.source_asset_id=e.left_source_asset_id
                    AND m.node_id IN (pe.left_node_id,pe.right_node_id))
                AND EXISTS(SELECT 1 FROM phash_node_members m
                  WHERE m.run_id=e.run_id AND m.source=e.right_source
                    AND m.source_asset_id=e.right_source_asset_id
                    AND m.node_id IN (pe.left_node_id,pe.right_node_id))
                AND (SELECT m.node_id FROM phash_node_members m
                     WHERE m.run_id=e.run_id AND m.source=e.left_source
                       AND m.source_asset_id=e.left_source_asset_id)
                    <>(SELECT m.node_id FROM phash_node_members m
                       WHERE m.run_id=e.run_id AND m.source=e.right_source
                         AND m.source_asset_id=e.right_source_asset_id)
              )
            """,
            (run_id,),
        ).fetchone()[0]
    )
    exact_membership_mismatches = int(
        connection.execute(
            """
            SELECT count(*) FROM candidate_image_evidence e
            WHERE e.run_id=? AND e.evidence_kind='exact_pixel' AND (
              NOT EXISTS(SELECT 1 FROM exact_pixel_cluster_members m
                WHERE m.run_id=e.run_id AND m.cluster_id=e.exact_cluster_id
                  AND m.source=e.left_source
                  AND m.source_asset_id=e.left_source_asset_id)
              OR NOT EXISTS(SELECT 1 FROM exact_pixel_cluster_members m
                WHERE m.run_id=e.run_id AND m.cluster_id=e.exact_cluster_id
                  AND m.source=e.right_source
                  AND m.source_asset_id=e.right_source_asset_id)
            )
            """,
            (run_id,),
        ).fetchone()[0]
    )
    _add(
        checks,
        "direct_image_evidence",
        mismatches == 0
        and unattached == 0
        and edge_membership_mismatches == 0
        and exact_membership_mismatches == 0,
        {
            "record_mismatches": 0,
            "building_attachment_mismatches": 0,
            "edge_membership_mismatches": 0,
            "exact_membership_mismatches": 0,
        },
        {
            "record_mismatches": mismatches,
            "building_attachment_mismatches": unattached,
            "edge_membership_mismatches": edge_membership_mismatches,
            "exact_membership_mismatches": exact_membership_mismatches,
        },
        checked=checked,
    )


def _validate_smoke_manifests(
    connection: sqlite3.Connection, run_id: str, checks: list[ValidationCheck]
) -> None:
    run = connection.execute(
        "SELECT selection_mode,sample_size,sample_seed FROM e2_runs WHERE run_id=?",
        (run_id,),
    ).fetchone()
    manifest_count = mismatch_count = 0
    for manifest in connection.execute(
        """
        SELECT manifest_name,sample_size,sample_seed,selection_version,
               ordered_manifest_sha256
        FROM smoke_manifests WHERE run_id=? ORDER BY manifest_name
        """,
        (run_id,),
    ):
        records: list[dict[str, Any]] = []
        expected_rank = 1
        for item in connection.execute(
            """
            SELECT selection_rank,entity_kind,source,source_entity_id,stratum,
                   score_sha256,item_record_sha256
            FROM smoke_manifest_items
            WHERE run_id=? AND manifest_name=? ORDER BY selection_rank
            """,
            (run_id, str(manifest[0])),
        ):
            score = deterministic_sample_score(
                str(manifest[2]), f"{item[2]}:{item[3]}"
            )
            record = {
                "asset_id": str(item[3]),
                "rank": int(item[0]),
                "reason": str(item[4]),
                "score": score,
                "source": str(item[2]),
            }
            exists = int(
                connection.execute(
                    """
                    SELECT count(*) FROM assets
                    WHERE run_id=? AND source=? AND source_asset_id=?
                    """,
                    (run_id, str(item[2]), str(item[3])),
                ).fetchone()[0]
            )
            mismatch_count += int(
                int(item[0]) != expected_rank
                or str(item[1]) != "asset"
                or str(item[5]) != score
                or str(item[6]) != canonical_sha256(record)
                or exists != 1
            )
            expected_rank += 1
            records.append(record)
        mismatch_count += int(
            int(manifest[1]) != len(records)
            or str(manifest[3]) != SAMPLE_POLICY_VERSION
            or str(manifest[4]) != canonical_sha256(records)
        )
        manifest_count += 1
    if run is not None and str(run[0]) == "sample":
        mismatch_count += int(manifest_count != 1)
        if manifest_count == 1:
            only = connection.execute(
                "SELECT sample_size,sample_seed FROM smoke_manifests WHERE run_id=?",
                (run_id,),
            ).fetchone()
            mismatch_count += int(
                int(only[0]) != int(run[1]) or str(only[1]) != str(run[2])
            )
    _add(
        checks,
        "ordered_smoke_manifests",
        mismatch_count == 0,
        0,
        mismatch_count,
        checked=manifest_count,
    )


def validate_e2_artifact(
    path: Path | str,
    *,
    expected_logical_sha256: str | None = None,
    verify_input_file_hashes: bool = True,
) -> EvidenceValidationReport:
    """Validate one terminal E2 artifact without mutating it or its inputs."""

    target = Path(path).resolve()
    if not target.is_file():
        raise FileNotFoundError(target)
    checks: list[ValidationCheck] = []
    artifact_sidecars = tuple(str(item) for item in sqlite_sidecars(target))
    _add(checks, "artifact_sqlite_sidecars_absent", not artifact_sidecars, [], artifact_sidecars)
    connection = open_immutable(target)
    run_id: str | None = None
    logical_sha: str | None = None
    table_manifests: dict[str, dict[str, Any]] = {}
    input_files: tuple[InputFileCheck, ...] = ()
    try:
        quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = list(connection.execute("PRAGMA foreign_key_check"))
        _add(checks, "sqlite_quick_check", quick == "ok", "ok", quick)
        _add(checks, "sqlite_integrity_check", integrity == "ok", "ok", integrity)
        _add(checks, "sqlite_foreign_key_check", not foreign_keys, 0, len(foreign_keys))

        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
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
            schema_version == EXPECTED_SCHEMA_VERSION,
            EXPECTED_SCHEMA_VERSION,
            schema_version,
        )

        tables = _schema_object_names(connection, "table")
        views = _schema_object_names(connection, "view")
        required = set(LOGICAL_EVIDENCE_TABLES) | set(REQUIRED_CONTROL_TABLES)
        _add(checks, "required_tables_present", required <= tables, required, tables)
        _add(
            checks,
            "only_contract_tables_present",
            tables == required,
            required,
            tables,
            missing=sorted(required - tables),
            unexpected=sorted(tables - required),
        )
        _add(checks, "views_absent", not views, [], sorted(views))
        forbidden = sorted((tables | views) & FORBIDDEN_POLICY_TABLES)
        _add(checks, "forbidden_policy_tables_absent", not forbidden, [], forbidden)
        column_mismatches = {
            table: {
                "expected": EXPECTED_TABLE_COLUMNS[table],
                "actual": _table_columns(connection, table),
            }
            for table in sorted(required & tables)
            if _table_columns(connection, table) != EXPECTED_TABLE_COLUMNS[table]
        }
        _add(
            checks,
            "exact_table_columns",
            not column_mismatches,
            {},
            column_mismatches,
        )
        if not required <= tables:
            return EvidenceValidationReport(
                str(target), None, None, {}, (), tuple(checks)
            )
        if column_mismatches:
            return EvidenceValidationReport(
                str(target), None, None, {}, (), tuple(checks)
            )

        runs = connection.execute(
            """
            SELECT run_id,status,ordered_selection_manifest_sha256,contract_version
            FROM e2_runs
            """
        ).fetchall()
        complete = len(runs) == 1 and str(runs[0][1]) == "complete"
        _add(
            checks,
            "exactly_one_complete_run",
            complete,
            {"count": 1, "status": "complete"},
            {"count": len(runs), "statuses": [str(row[1]) for row in runs]},
        )
        if len(runs) != 1:
            return EvidenceValidationReport(
                str(target), None, None, {}, (), tuple(checks)
            )
        run_id = str(runs[0][0])
        _validate_evidence_only_policy(connection, run_id, checks)
        _add(
            checks,
            "evidence_contract_version",
            str(runs[0][3]) == E2_EVIDENCE_VERSION,
            E2_EVIDENCE_VERSION,
            str(runs[0][3]),
        )
        failed_error_validations = int(
            connection.execute(
                """
                SELECT count(*) FROM e2_validations
                WHERE run_id=? AND severity='error' AND passed=0
                """,
                (run_id,),
            ).fetchone()[0]
        )
        _add(
            checks,
            "complete_run_has_no_failed_error_validation",
            failed_error_validations == 0,
            0,
            failed_error_validations,
        )
        selection_sha = _selection_manifest(connection, run_id)
        _add(
            checks,
            "ordered_selection_manifest",
            str(runs[0][2]) == selection_sha,
            selection_sha,
            str(runs[0][2]),
        )

        input_files = _validate_inputs(
            connection,
            run_id,
            checks,
            verify_file_hashes=verify_input_file_hashes,
        )
        _validate_asset_accounting(connection, run_id, checks)
        _validate_phash_pairs(connection, run_id, checks)
        _validate_metadata_cartesian(connection, run_id, checks)
        _validate_direct_image_evidence(connection, run_id, checks)
        _validate_building_candidates(connection, run_id, checks)
        _validate_smoke_manifests(connection, run_id, checks)

        logical_sha, table_manifests = logical_evidence_manifest(connection, run_id)
        stored_rows = connection.execute(
            """
            SELECT value_text FROM e2_metrics
            WHERE run_id=? AND phase='validation'
              AND metric_name='output_logical_sha256'
            ORDER BY stratum_json
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
    finally:
        connection.close()
    return EvidenceValidationReport(
        artifact_path=str(target),
        run_id=run_id,
        logical_sha256=logical_sha,
        table_manifests=table_manifests,
        input_files=input_files,
        checks=tuple(checks),
    )


__all__ = [
    "EvidenceValidationError",
    "EvidenceValidationReport",
    "EXPECTED_APPLICATION_ID",
    "EXPECTED_SCHEMA_VERSION",
    "EXPECTED_TABLE_COLUMNS",
    "FORBIDDEN_POLICY_TABLES",
    "InputFileCheck",
    "LOGICAL_EVIDENCE_TABLES",
    "LOGICAL_MANIFEST_VERSION",
    "VALIDATOR_VERSION",
    "ValidationCheck",
    "logical_evidence_manifest",
    "open_immutable",
    "sha256_file",
    "sqlite_sidecars",
    "validate_e2_artifact",
]
