"""Immutable reconciliation of a complete E1 sidecar with recovery sidecars.

The base and recovery databases are always opened read-only.  Reconciliation
is materialized into a new sidecar and published only after the standard E1
validator and merge-specific no-clobber checks both pass.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence

from canonical.image_fingerprint_adapters import (
    InventoryDecision,
    SourceAsset,
)
from canonical.image_fingerprint_pipeline import (
    PipelineError,
    _acquire_lock,
    _finish_run,
    _publish_hardlink,
    _release_lock,
)
from canonical.image_fingerprint_recovery import (
    RECOVERY_LINEAGE_ADDITIVE_FIELDS,
    validate_failure_recovery_sidecar,
)
from canonical.image_fingerprint_sidecar import (
    initialize_sidecar,
    open_sidecar,
    recover_sidecar,
)
from canonical.image_fingerprint_validator import validate_image_fingerprint_sidecar


MERGE_VERSION = "image-fingerprint-recovery-merge-v2"
MERGE_LINEAGE_KIND = "failure_recovery_merge_v1"
MERGE_BATCH_SIZE = 5_000
_MERGE_PHASES = (
    "inventory_assets",
    "inventory_exclusions",
    "base_attempts",
    "base_fingerprints",
    "recoveries",
    "ready_validation",
)
MERGE_TABLE_NAMES = frozenset(
    {
        "recovery_merge_lineage",
        "recovery_merge_decisions",
        "recovery_merge_progress",
    }
)
MERGE_TRIGGER_NAMES = frozenset(
    {
        "recovery_merge_lineage_initializing_insert",
        "recovery_merge_lineage_immutable_update",
        "recovery_merge_lineage_immutable_delete",
        "recovery_merge_decisions_running_insert",
        "recovery_merge_decisions_immutable_update",
        "recovery_merge_decisions_immutable_delete",
        "recovery_merge_progress_initializing_insert",
        "recovery_merge_progress_active_update",
        "recovery_merge_progress_immutable_delete",
    }
)

_CheckpointHook = Callable[[str, int], None]


@dataclass(frozen=True)
class RecoveryMergeResult:
    output_path: Path
    source: str
    base_sidecar_sha256: str
    recovery_sidecar_sha256s: tuple[str, ...]
    output_sha256: str
    base_success: int
    base_failed: int
    recovered: int
    final_success: int
    final_failed: int
    run_status: str
    manifest_sha256: str


@dataclass(frozen=True)
class RecoveryMergeValidation:
    passed: bool
    checks: tuple[tuple[str, bool, object, object], ...]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


def _sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _immutable_sqlite_uri(path: Path) -> str:
    """Match the Windows-safe URI form used by ``open_sidecar``."""

    return f"file:{path.resolve().as_posix()}?mode=ro&immutable=1"


def _assert_snapshot(path: Path) -> None:
    active = [
        Path(str(path) + suffix)
        for suffix in ("-wal", "-shm", "-journal")
        if Path(str(path) + suffix).exists()
    ]
    if active:
        raise PipelineError(
            "SQLite input has active sidecars: "
            + ", ".join(item.name for item in active)
        )


def _asset_from_provenance(payload: str) -> SourceAsset:
    record = json.loads(payload)
    return SourceAsset(
        source=str(record["source"]),
        source_asset_id=str(record["source_asset_id"]),
        source_asset_key=str(record["source_asset_key"]),
        normalized_url=str(record["normalized_url"]),
        selected_raw_url=str(record["selected_raw_url"]),
        effective_fetch_url=str(record["effective_fetch_url"]),
        source_urls=tuple(str(value) for value in record["source_urls"]),
        occurrence_count=int(record["occurrence_count"]),
        parent_count=int(record["parent_count"]),
        roles=tuple(str(value) for value in record["roles"]),
        format_lane=str(record["format_lane"]),
        fetch_profile_version=str(record["fetch_profile_version"]),
    )


def _sidecar_run(path: Path) -> tuple[dict[str, object], dict[str, int]]:
    connection = open_sidecar(path, readonly=True)
    try:
        rows = connection.execute("SELECT * FROM fingerprint_runs").fetchall()
        if len(rows) != 1:
            raise PipelineError(f"sidecar must contain one run: {path}")
        counts = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT status,count(*) FROM fingerprints GROUP BY status"
            )
        }
        return dict(rows[0]), counts
    finally:
        connection.close()


def _recovery_assets(path: Path) -> tuple[SourceAsset, ...]:
    connection = open_sidecar(path, readonly=True)
    try:
        return tuple(
            _asset_from_provenance(str(row[0]))
            for row in connection.execute(
                "SELECT provenance_json FROM source_assets ORDER BY selection_rank"
            )
        )
    finally:
        connection.close()


def _lineage_payload(run: dict[str, object]) -> dict[str, object]:
    try:
        payload = json.loads(str(run["dependency_manifest_json"]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PipelineError("invalid recovery dependency manifest") from exc
    lineage = payload.get("_run_lineage")
    if not isinstance(lineage, dict) or lineage.get("kind") != "failure_recovery_v1":
        raise PipelineError("recovery sidecar lacks failure_recovery_v1 lineage")
    return lineage


def _dependency_document(
    run: dict[str, object], *, label: str
) -> tuple[dict[str, object], dict[str, object] | None, str]:
    raw = str(run["dependency_manifest_json"])
    stored_sha = str(run["dependency_manifest_sha256"])
    if hashlib.sha256(raw.encode("ascii")).hexdigest() != stored_sha:
        raise PipelineError(f"{label} dependency manifest SHA mismatch")
    try:
        document = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PipelineError(f"{label} dependency manifest is invalid") from exc
    if not isinstance(document, dict) or _canonical_json(document) != raw:
        raise PipelineError(f"{label} dependency manifest is not canonical")
    lineage = document.pop("_run_lineage", None)
    if lineage is not None and not isinstance(lineage, dict):
        raise PipelineError(f"{label} dependency lineage is invalid")
    return document, lineage, _canonical_json(document)


def _merged_dependency_manifest(
    stripped_dependencies: dict[str, object],
    *,
    manifest_sha256: str,
    base_sha256: str,
    recovery_sha256s: Sequence[str],
    recovery_lineage_validations: Sequence[dict[str, object]],
    source_sha256: str,
) -> tuple[str, str, dict[str, object]]:
    lineage: dict[str, object] = {
        "kind": MERGE_LINEAGE_KIND,
        "merge_version": MERGE_VERSION,
        "merge_manifest_sha256": manifest_sha256,
        "base_sidecar_sha256": base_sha256,
        "recovery_sidecar_sha256s": list(recovery_sha256s),
        "recovery_lineage_validations": list(recovery_lineage_validations),
        "source_db_sha256": source_sha256,
    }
    document = dict(stripped_dependencies)
    document["_run_lineage"] = lineage
    payload = _canonical_json(document)
    return payload, hashlib.sha256(payload.encode("ascii")).hexdigest(), lineage


def _assert_inputs_unchanged(
    inputs: Sequence[tuple[Path, str]], *, label: str
) -> None:
    for path, expected_sha in inputs:
        _assert_snapshot(path)
        actual = _sha256_file(path)
        if actual != expected_sha:
            raise PipelineError(
                f"{label} input changed: {path} (expected {expected_sha}, got {actual})"
            )


def _copy_base_inventory(
    connection: sqlite3.Connection,
    *,
    base_run_id: str,
    new_run_id: str,
) -> None:
    connection.execute(
            """
            INSERT INTO source_assets(
              run_id,source_asset_id,selection_rank,canonical_url,fetch_url,
              source_record_sha256,provenance_json
            )
            SELECT ?,source_asset_id,selection_rank,canonical_url,fetch_url,
                   source_record_sha256,provenance_json
            FROM base_e1.source_assets WHERE run_id=? ORDER BY selection_rank
            """,
            (new_run_id, base_run_id),
        )
    connection.execute(
            """
            INSERT INTO source_asset_exclusions(
              run_id,source_asset_id,source_asset_key,inventory_rank,reason_code,
              source_record_sha256,provenance_json,detail_json
            )
            SELECT ?,source_asset_id,source_asset_key,inventory_rank,reason_code,
                   source_record_sha256,provenance_json,detail_json
            FROM base_e1.source_asset_exclusions
            WHERE run_id=? ORDER BY inventory_rank
            """,
            (new_run_id, base_run_id),
        )
    connection.execute(
            """
            INSERT INTO fingerprints(run_id,source_asset_id,status)
            SELECT ?,source_asset_id,'pending'
            FROM base_e1.source_assets WHERE run_id=? ORDER BY selection_rank
            """,
            (new_run_id, base_run_id),
        )
def _copy_base_results(
    connection: sqlite3.Connection,
    *,
    base_run_id: str,
    new_run_id: str,
) -> None:
    connection.execute(
            """
            INSERT INTO fetch_attempts(
              run_id,source_asset_id,attempt_no,request_url,started_at,completed_at,
              elapsed_ms,outcome,http_status,response_mime,response_bytes,final_url,
              raw_response_sha256,error_kind,error_message,retry_after_seconds,
              scheduled_delay_seconds,worker_no
            )
            SELECT ?,source_asset_id,attempt_no,request_url,started_at,completed_at,
                   elapsed_ms,outcome,http_status,response_mime,response_bytes,final_url,
                   raw_response_sha256,error_kind,error_message,retry_after_seconds,
                   scheduled_delay_seconds,worker_no
            FROM base_e1.fetch_attempts WHERE run_id=?
            ORDER BY source_asset_id,attempt_no
            """,
            (new_run_id, base_run_id),
        )
    connection.execute(
            """
            UPDATE fingerprints AS target SET
              status=b.status,
              selected_attempt_no=b.selected_attempt_no,
              raw_response_sha256=b.raw_response_sha256,
              normalized_pixel_sha256=b.normalized_pixel_sha256,
              phash_hex=b.phash_hex,
              decoded_format=b.decoded_format,
              original_width=b.original_width,
              original_height=b.original_height,
              normalized_width=b.normalized_width,
              normalized_height=b.normalized_height,
              metadata_json=b.metadata_json,
              completed_at=b.completed_at,
              error_kind=b.error_kind,
              error_message=b.error_message
            FROM base_e1.fingerprints AS b
            WHERE target.run_id=? AND b.run_id=?
              AND b.source_asset_id=target.source_asset_id
            """,
            (new_run_id, base_run_id),
        )
_ATTEMPT_COLUMNS = (
    "request_url",
    "started_at",
    "completed_at",
    "elapsed_ms",
    "outcome",
    "http_status",
    "response_mime",
    "response_bytes",
    "final_url",
    "raw_response_sha256",
    "error_kind",
    "error_message",
    "retry_after_seconds",
    "scheduled_delay_seconds",
    "worker_no",
)

_FINGERPRINT_COLUMNS = (
    "status",
    "selected_attempt_no",
    "raw_response_sha256",
    "normalized_pixel_sha256",
    "phash_hex",
    "decoded_format",
    "original_width",
    "original_height",
    "normalized_width",
    "normalized_height",
    "metadata_json",
    "completed_at",
    "error_kind",
    "error_message",
)


def _apply_recovery(
    connection: sqlite3.Connection,
    *,
    recovery_path: Path,
    recovery_ordinal: int,
    recovery_sha: str,
    new_run_id: str,
    after_selection_rank: int,
    limit: int,
) -> tuple[int, int, int]:
    recovery = open_sidecar(recovery_path, readonly=True)
    applied = 0
    try:
        recovery_run = recovery.execute("SELECT run_id FROM fingerprint_runs").fetchone()[0]
        rows = recovery.execute(
            """
            SELECT f.*,s.source_record_sha256,s.selection_rank AS recovery_selection_rank
            FROM fingerprints AS f JOIN source_assets AS s USING(run_id,source_asset_id)
            WHERE f.run_id=? AND s.selection_rank>?
            ORDER BY s.selection_rank LIMIT ?
            """,
            (recovery_run, after_selection_rank, limit),
        ).fetchall()
        for row in rows:
            asset_id = str(row["source_asset_id"])
            base = connection.execute(
                """SELECT f.status,f.error_kind,s.source_record_sha256
                   FROM fingerprints AS f JOIN source_assets AS s USING(run_id,source_asset_id)
                   WHERE f.run_id=? AND f.source_asset_id=?""",
                (new_run_id, asset_id),
            ).fetchone()
            if base is None:
                raise PipelineError(f"recovery asset is absent from base: {asset_id}")
            if str(base["source_record_sha256"]) != str(row["source_record_sha256"]):
                raise PipelineError(f"recovery source-record SHA mismatch: {asset_id}")
            if str(base["status"]) != "failed":
                raise PipelineError(
                    "recovery target must be exactly a failed base fingerprint: "
                    f"{asset_id} ({base['status']})"
                )

            offset = int(
                connection.execute(
                    "SELECT coalesce(max(attempt_no),0) FROM fetch_attempts "
                    "WHERE run_id=? AND source_asset_id=?",
                    (new_run_id, asset_id),
                ).fetchone()[0]
            )
            attempts = recovery.execute(
                "SELECT * FROM fetch_attempts WHERE run_id=? AND source_asset_id=? "
                "ORDER BY attempt_no",
                (recovery_run, asset_id),
            ).fetchall()
            attempt_numbers = [int(attempt["attempt_no"]) for attempt in attempts]
            if attempt_numbers != list(range(1, len(attempts) + 1)):
                raise PipelineError(
                    f"recovery attempt numbers are not contiguous: {asset_id}"
                )
            for attempt in attempts:
                connection.execute(
                    """
                    INSERT INTO fetch_attempts(
                      run_id,source_asset_id,attempt_no,request_url,started_at,completed_at,
                      elapsed_ms,outcome,http_status,response_mime,response_bytes,final_url,
                      raw_response_sha256,error_kind,error_message,retry_after_seconds,
                      scheduled_delay_seconds,worker_no
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        new_run_id,
                        asset_id,
                        offset + int(attempt["attempt_no"]),
                        *[attempt[name] for name in _ATTEMPT_COLUMNS],
                    ),
                )

            recovery_status = str(row["status"])
            should_apply = recovery_status == "success"
            if should_apply:
                selected = row["selected_attempt_no"]
                values = [row[name] for name in _FINGERPRINT_COLUMNS]
                values[1] = None if selected is None else offset + int(selected)
                assignments = ",".join(f"{name}=?" for name in _FINGERPRINT_COLUMNS)
                connection.execute(
                    f"UPDATE fingerprints SET {assignments} "
                    "WHERE run_id=? AND source_asset_id=?",
                    (*values, new_run_id, asset_id),
                )
                applied += 1

            connection.execute(
                """
                INSERT INTO recovery_merge_decisions(
                  merge_id,recovery_ordinal,recovery_sha256,source_asset_id,
                  base_error_kind,recovery_status,recovery_error_kind,
                  attempt_offset,attempt_count,applied_success
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    new_run_id,
                    recovery_ordinal,
                    recovery_sha,
                    asset_id,
                    base["error_kind"],
                    recovery_status,
                    row["error_kind"],
                    offset,
                    len(attempts),
                    int(should_apply),
                ),
            )
        last_rank = (
            after_selection_rank
            if not rows
            else int(rows[-1]["recovery_selection_rank"])
        )
        return applied, last_rank, len(rows)
    finally:
        recovery.close()


def _max_merged_attempts(base_path: Path, recovery_paths: Sequence[Path]) -> int:
    counts: defaultdict[str, int] = defaultdict(int)
    maximum = 1
    for path in (base_path, *recovery_paths):
        connection = open_sidecar(path, readonly=True)
        try:
            for row in connection.execute(
                "SELECT source_asset_id,count(*) FROM fetch_attempts GROUP BY source_asset_id"
            ):
                asset_id = str(row[0])
                if path == base_path:
                    counts[asset_id] = int(row[1])
                else:
                    counts[asset_id] += int(row[1])
                maximum = max(maximum, counts[asset_id])
        finally:
            connection.close()
    return maximum


def _validate_base_success_unchanged(
    merged_path: Path,
    base_path: Path,
    *,
    expected_recovered: int,
) -> tuple[int, int, int]:
    merged = open_sidecar(merged_path, readonly=True)
    base_uri = _immutable_sqlite_uri(base_path)
    merged.execute("ATTACH DATABASE ? AS base_e1", (base_uri,))
    try:
        merged_run = str(merged.execute("SELECT run_id FROM fingerprint_runs").fetchone()[0])
        base_run = str(
            merged.execute("SELECT run_id FROM base_e1.fingerprint_runs").fetchone()[0]
        )
        fields = (
            "status",
            "selected_attempt_no",
            "raw_response_sha256",
            "normalized_pixel_sha256",
            "phash_hex",
            "decoded_format",
            "original_width",
            "original_height",
            "normalized_width",
            "normalized_height",
            "metadata_json",
            "completed_at",
            "error_kind",
            "error_message",
        )
        difference = " OR ".join(f"m.{name} IS NOT b.{name}" for name in fields)
        changed_success = int(
            merged.execute(
                f"""
                SELECT count(*) FROM fingerprints AS m
                JOIN base_e1.fingerprints AS b ON b.source_asset_id=m.source_asset_id
                WHERE m.run_id=? AND b.run_id=? AND b.status='success'
                  AND ({difference})
                """,
                (merged_run, base_run),
            ).fetchone()[0]
        )
        recovered = int(
            merged.execute(
                """
                SELECT count(*) FROM fingerprints AS m
                JOIN base_e1.fingerprints AS b ON b.source_asset_id=m.source_asset_id
                WHERE m.run_id=? AND b.run_id=? AND b.status='failed' AND m.status='success'
                """,
                (merged_run, base_run),
            ).fetchone()[0]
        )
        total = int(
            merged.execute(
                "SELECT count(*) FROM fingerprints WHERE run_id=?", (merged_run,)
            ).fetchone()[0]
        )
        if changed_success:
            raise PipelineError(f"merge changed {changed_success} prior successful rows")
        if recovered != expected_recovered:
            raise PipelineError(
                f"merge recovery count mismatch: expected {expected_recovered}, got {recovered}"
            )
        return total, changed_success, recovered
    finally:
        merged.execute("DETACH DATABASE base_e1")
        merged.close()


def _validate_recovery_successes_applied(
    merged_path: Path,
    recovery_paths: Sequence[Path],
) -> None:
    """Re-read every successful child row and compare it with the merge."""

    merged = open_sidecar(merged_path, readonly=True)
    try:
        merged_run = str(merged.execute("SELECT run_id FROM fingerprint_runs").fetchone()[0])
        for ordinal, recovery_path in enumerate(recovery_paths, 1):
            recovery = open_sidecar(recovery_path, readonly=True)
            try:
                recovery_run = str(
                    recovery.execute("SELECT run_id FROM fingerprint_runs").fetchone()[0]
                )
                for row in recovery.execute(
                    "SELECT * FROM fingerprints WHERE run_id=? AND status='success'",
                    (recovery_run,),
                ):
                    decision = merged.execute(
                        """
                        SELECT attempt_offset,applied_success
                        FROM recovery_merge_decisions
                        WHERE merge_id=? AND recovery_ordinal=? AND source_asset_id=?
                        """,
                        (merged_run, ordinal, row["source_asset_id"]),
                    ).fetchone()
                    target = merged.execute(
                        "SELECT * FROM fingerprints WHERE run_id=? AND source_asset_id=?",
                        (merged_run, row["source_asset_id"]),
                    ).fetchone()
                    if decision is None or target is None or int(decision["applied_success"]) != 1:
                        raise PipelineError(
                            f"successful recovery was not applied: {row['source_asset_id']}"
                        )
                    expected_attempt = int(decision["attempt_offset"]) + int(
                        row["selected_attempt_no"]
                    )
                    mismatched = target["selected_attempt_no"] != expected_attempt or any(
                        target[name] != row[name]
                        for name in _FINGERPRINT_COLUMNS
                        if name != "selected_attempt_no"
                    )
                    if mismatched:
                        raise PipelineError(
                            f"merged recovery fingerprint mismatch: {row['source_asset_id']}"
                        )
            finally:
                recovery.close()
    finally:
        merged.close()


def validate_image_fingerprint_recovery_merge(
    merged_sidecar: Path | str,
    source_db: Path | str,
    base_sidecar: Path | str,
    recovery_sidecars: Sequence[Path | str],
    *,
    inventory_factory: Callable[[], Iterable[InventoryDecision]] | None = None,
) -> RecoveryMergeValidation:
    """Independently validate a materialized recovery merge.

    The merge's embedded manifest and decision ledger are evidence, not
    authority.  This function re-hashes every immutable input, re-validates
    each recovery selection from the base failures/current inventory, and
    recomputes attempt offsets and final fingerprint values row by row.
    """

    merged_path = Path(merged_sidecar).resolve()
    source_path = Path(source_db).resolve()
    base_path = Path(base_sidecar).resolve()
    recovery_paths = tuple(Path(path).resolve() for path in recovery_sidecars)
    checks: list[tuple[str, bool, object, object]] = []

    def add(name: str, passed: bool, expected: object, actual: object) -> None:
        checks.append((name, bool(passed), expected, actual))

    paths = (source_path, base_path, *recovery_paths, merged_path)
    for path in paths:
        if not path.is_file():
            add("input_exists", False, "existing file", str(path))
            return RecoveryMergeValidation(False, tuple(checks))
        try:
            _assert_snapshot(path)
        except PipelineError as exc:
            add("sqlite_sidecars_absent", False, [], str(exc))
            return RecoveryMergeValidation(False, tuple(checks))
    initial_hashes = {path: _sha256_file(path) for path in paths}

    standard = validate_image_fingerprint_sidecar(
        merged_path, source_path, inventory_factory=inventory_factory
    )
    add("standard_e1_validation", standard.passed, True, standard.passed)
    try:
        base_run, base_counts = _sidecar_run(base_path)
        merged_run, merged_counts = _sidecar_run(merged_path)
        unsupported_statuses: dict[str, list[str]] = {}
        base_unsupported = sorted(set(base_counts) - {"success", "failed"})
        merged_unsupported = sorted(set(merged_counts) - {"success", "failed"})
        if base_unsupported:
            unsupported_statuses["base"] = base_unsupported
        if merged_unsupported:
            unsupported_statuses["merged"] = merged_unsupported
        base_dependencies, base_lineage, stripped_base = _dependency_document(
            base_run, label="base"
        )
        add("ordinary_base_lineage", base_lineage is None, None, base_lineage)
        source_sha = initial_hashes[source_path]
        base_sha = initial_hashes[base_path]
        recovery_shas = tuple(initial_hashes[path] for path in recovery_paths)
        source = str(base_run["source_name"])
        base_run_id = str(base_run["run_id"])

        recovery_metadata: list[dict[str, object]] = []
        recovery_lineage_validations: list[dict[str, object]] = []
        selected_by_recovery: list[tuple[SourceAsset, ...]] = []
        all_ids: set[str] = set()
        overlap_ids: set[str] = set()
        recovery_valid = True
        dependency_compatible = True
        for path, sha in zip(recovery_paths, recovery_shas, strict=True):
            run, recovery_counts = _sidecar_run(path)
            recovery_unsupported = sorted(
                set(recovery_counts) - {"success", "failed"}
            )
            if recovery_unsupported:
                unsupported_statuses[f"recovery:{path.name}"] = recovery_unsupported
            recovery_lineage = _lineage_payload(run)
            missing_additive = sorted(
                RECOVERY_LINEAGE_ADDITIVE_FIELDS - set(recovery_lineage)
            )
            derived_values = {
                "base_source_db_sha256_before": str(
                    base_run["source_db_sha256_before"]
                ),
                "base_source_db_sha256_after": str(
                    base_run["source_db_sha256_after"]
                ),
                "base_fingerprint_contract_version": str(
                    base_run["fingerprint_contract_version"]
                ),
                "base_selection_version": str(base_run["selection_version"]),
                "base_dependency_manifest_sha256": str(
                    base_run["dependency_manifest_sha256"]
                ),
            }
            lineage_validation: dict[str, object] = {
                "recovery_sha256": sha,
                "validation_mode": (
                    "legacy_additive_upgrade" if missing_additive else "strict"
                ),
                "missing_additive_fields": missing_additive,
                "derived_fields": {
                    field: derived_values[field] for field in missing_additive
                },
            }
            try:
                # Generic E1 validation only proves the child sidecar contract;
                # this wrapper-specific validator is the trust boundary that
                # binds recovery lineage to the real immutable parent/source
                # and recomputes its deterministic selection.
                assets = validate_failure_recovery_sidecar(
                    path,
                    source_path,
                    base_path,
                    inventory_factory=inventory_factory,
                    allow_legacy_lineage_upgrade=True,
                )
            except Exception as exc:
                recovery_valid = False
                assets = ()
                add(f"recovery_lineage:{path.name}", False, True, str(exc))
            else:
                add(f"recovery_lineage:{path.name}", True, True, True)
            dependency, _, stripped = _dependency_document(
                run, label=f"recovery {path.name}"
            )
            del dependency
            dependency_compatible &= stripped == stripped_base
            ids = {asset.source_asset_id for asset in assets}
            overlap_ids.update(all_ids & ids)
            all_ids.update(ids)
            selected_by_recovery.append(assets)
            recovery_lineage_validations.append(lineage_validation)
            recovery_metadata.append(
                {
                    "path": str(path),
                    "sha256": sha,
                    "run_id": str(run["run_id"]),
                    "selection_count": int(run["selection_count"]),
                    "selection_manifest_sha256": str(
                        run["selection_manifest_sha256"]
                    ),
                    "lineage_validation": lineage_validation,
                }
            )
        add("recovery_lineage_validation", recovery_valid, True, recovery_valid)
        add(
            "supported_fingerprint_statuses",
            not unsupported_statuses,
            {},
            unsupported_statuses,
        )
        add(
            "stripped_dependency_compatibility",
            dependency_compatible,
            stripped_base,
            dependency_compatible,
        )
        add("recovery_selection_disjoint", not overlap_ids, [], sorted(overlap_ids))

        manifest = {
            "base": {
                "path": str(base_path),
                "run_id": base_run_id,
                "sha256": base_sha,
            },
            "merge_version": MERGE_VERSION,
            "recoveries": recovery_metadata,
            "source": {
                "name": source,
                "path": str(source_path),
                "sha256": source_sha,
            },
        }
        manifest_json = _canonical_json(manifest)
        manifest_sha = hashlib.sha256(manifest_json.encode("ascii")).hexdigest()
        expected_dependency_json, expected_dependency_sha, expected_lineage = (
            _merged_dependency_manifest(
                base_dependencies,
                manifest_sha256=manifest_sha,
                base_sha256=base_sha,
                recovery_sha256s=recovery_shas,
                recovery_lineage_validations=recovery_lineage_validations,
                source_sha256=source_sha,
            )
        )
        add(
            "merged_dependency_manifest",
            str(merged_run["dependency_manifest_json"]) == expected_dependency_json
            and str(merged_run["dependency_manifest_sha256"])
            == expected_dependency_sha,
            {"json": expected_dependency_json, "sha256": expected_dependency_sha},
            {
                "json": str(merged_run["dependency_manifest_json"]),
                "sha256": str(merged_run["dependency_manifest_sha256"]),
            },
        )

        merged = open_sidecar(merged_path, readonly=True)
        base = open_sidecar(base_path, readonly=True)
        recoveries = [open_sidecar(path, readonly=True) for path in recovery_paths]
        try:
            schema = {
                (str(row[0]), str(row[1]))
                for row in merged.execute(
                    "SELECT name,type FROM sqlite_schema WHERE type IN ('table','trigger')"
                )
            }
            actual_tables = {name for name, kind in schema if kind == "table"}
            actual_triggers = {name for name, kind in schema if kind == "trigger"}
            add(
                "merge_tables",
                MERGE_TABLE_NAMES <= actual_tables,
                sorted(MERGE_TABLE_NAMES),
                sorted(MERGE_TABLE_NAMES & actual_tables),
            )
            add(
                "merge_triggers",
                MERGE_TRIGGER_NAMES <= actual_triggers,
                sorted(MERGE_TRIGGER_NAMES),
                sorted(MERGE_TRIGGER_NAMES & actual_triggers),
            )
            fk_violations = sum(1 for _ in merged.execute("PRAGMA foreign_key_check"))
            lineage_fks = list(
                merged.execute("PRAGMA foreign_key_list(recovery_merge_lineage)")
            )
            decision_fks = list(
                merged.execute("PRAGMA foreign_key_list(recovery_merge_decisions)")
            )
            fk_targets = {str(row[2]) for row in decision_fks}
            fk_schema_ok = (
                any(str(row[2]) == "fingerprint_runs" for row in lineage_fks)
                and {"recovery_merge_lineage", "fingerprints"} <= fk_targets
            )
            add(
                "merge_foreign_keys",
                fk_violations == 0 and fk_schema_ok,
                {"violations": 0, "declared": True},
                {"violations": fk_violations, "declared": fk_schema_ok},
            )
            lineage_rows = merged.execute(
                "SELECT * FROM recovery_merge_lineage"
            ).fetchall()
            lineage_ok = False
            if len(lineage_rows) == 1:
                row = lineage_rows[0]
                lineage_ok = (
                    str(row["merge_id"]) == str(merged_run["run_id"])
                    and str(row["merge_version"]) == MERGE_VERSION
                    and str(row["manifest_json"]) == manifest_json
                    and str(row["manifest_sha256"]) == manifest_sha
                    and hashlib.sha256(
                        str(row["manifest_json"]).encode("ascii")
                    ).hexdigest()
                    == str(row["manifest_sha256"])
                )
            add(
                "merge_lineage_manifest",
                lineage_ok,
                {"count": 1, "sha256": manifest_sha},
                {
                    "count": len(lineage_rows),
                    "sha256": (
                        str(lineage_rows[0]["manifest_sha256"])
                        if lineage_rows
                        else None
                    ),
                },
            )
            dependency_lineage = json.loads(expected_dependency_json).get(
                "_run_lineage"
            )
            add(
                "merge_dependency_lineage",
                dependency_lineage == expected_lineage,
                expected_lineage,
                dependency_lineage,
            )

            # The base sidecar is immutable evidence, not merely a source of
            # final hashes.  Recovery attempts are appended after each asset's
            # base attempt-number prefix, so every base attempt can be compared
            # directly by its original primary key and every column.  These
            # attached-database comparisons are evaluated by SQLite and do not
            # materialize the full attempt ledger in Python memory.
            merged.execute(
                "ATTACH DATABASE ? AS base_compare",
                (_immutable_sqlite_uri(base_path),),
            )
            try:
                attempt_difference = " OR ".join(
                    f"m.{name} IS NOT b.{name}" for name in _ATTEMPT_COLUMNS
                )
                base_attempt_mismatches = int(
                    merged.execute(
                        f"""
                        SELECT count(*)
                        FROM base_compare.fetch_attempts AS b
                        LEFT JOIN fetch_attempts AS m
                          ON m.run_id=?
                         AND m.source_asset_id=b.source_asset_id
                         AND m.attempt_no=b.attempt_no
                        WHERE b.run_id=?
                          AND (m.source_asset_id IS NULL OR {attempt_difference})
                        """,
                        (str(merged_run["run_id"]), str(base_run["run_id"])),
                    ).fetchone()[0]
                )
                extra_base_prefix_attempts = int(
                    merged.execute(
                        """
                        SELECT count(*)
                        FROM fetch_attempts AS m
                        JOIN (
                          SELECT source_asset_id,max(attempt_no) AS max_attempt_no
                          FROM base_compare.fetch_attempts
                          WHERE run_id=?
                          GROUP BY source_asset_id
                        ) AS bounds
                          ON bounds.source_asset_id=m.source_asset_id
                         AND m.attempt_no<=bounds.max_attempt_no
                        LEFT JOIN base_compare.fetch_attempts AS b
                          ON b.run_id=?
                         AND b.source_asset_id=m.source_asset_id
                         AND b.attempt_no=m.attempt_no
                        WHERE m.run_id=? AND b.source_asset_id IS NULL
                        """,
                        (
                            str(base_run["run_id"]),
                            str(base_run["run_id"]),
                            str(merged_run["run_id"]),
                        ),
                    ).fetchone()[0]
                )
                add(
                    "base_attempt_prefix_exact",
                    base_attempt_mismatches == 0
                    and extra_base_prefix_attempts == 0,
                    {"mismatches": 0, "extra": 0},
                    {
                        "mismatches": base_attempt_mismatches,
                        "extra": extra_base_prefix_attempts,
                    },
                )

                fingerprint_difference = " OR ".join(
                    f"m.{name} IS NOT b.{name}" for name in _FINGERPRINT_COLUMNS
                )
                untouched_fingerprint_mismatches = int(
                    merged.execute(
                        f"""
                        SELECT count(*)
                        FROM base_compare.fingerprints AS b
                        LEFT JOIN fingerprints AS m
                          ON m.run_id=? AND m.source_asset_id=b.source_asset_id
                        LEFT JOIN recovery_merge_decisions AS d
                          ON d.merge_id=?
                         AND d.source_asset_id=b.source_asset_id
                         AND d.applied_success=1
                        WHERE b.run_id=? AND d.source_asset_id IS NULL
                          AND (m.source_asset_id IS NULL OR {fingerprint_difference})
                        """,
                        (
                            str(merged_run["run_id"]),
                            str(merged_run["run_id"]),
                            str(base_run["run_id"]),
                        ),
                    ).fetchone()[0]
                )
                add(
                    "base_unrecovered_fingerprints_exact",
                    untouched_fingerprint_mismatches == 0,
                    0,
                    untouched_fingerprint_mismatches,
                )
            finally:
                merged.execute("DETACH DATABASE base_compare")

            base_id = str(base_run["run_id"])
            merged_id = str(merged_run["run_id"])
            decision_mismatches = 0
            attempt_mismatches = 0
            fingerprint_mismatches = 0
            expected_decisions = 0
            for ordinal, (recovery, recovery_path, recovery_sha, assets) in enumerate(
                zip(
                    recoveries,
                    recovery_paths,
                    recovery_shas,
                    selected_by_recovery,
                    strict=True,
                ),
                1,
            ):
                del recovery_path
                recovery_id = str(
                    recovery.execute("SELECT run_id FROM fingerprint_runs").fetchone()[0]
                )
                for asset in assets:
                    asset_id = asset.source_asset_id
                    expected_decisions += 1
                    base_fp = base.execute(
                        "SELECT * FROM fingerprints WHERE run_id=? AND source_asset_id=?",
                        (base_id, asset_id),
                    ).fetchone()
                    recovery_fp = recovery.execute(
                        "SELECT * FROM fingerprints WHERE run_id=? AND source_asset_id=?",
                        (recovery_id, asset_id),
                    ).fetchone()
                    merged_fp = merged.execute(
                        "SELECT * FROM fingerprints WHERE run_id=? AND source_asset_id=?",
                        (merged_id, asset_id),
                    ).fetchone()
                    decision = merged.execute(
                        """SELECT * FROM recovery_merge_decisions
                           WHERE merge_id=? AND recovery_ordinal=?
                             AND source_asset_id=?""",
                        (merged_id, ordinal, asset_id),
                    ).fetchone()
                    if (
                        base_fp is None
                        or recovery_fp is None
                        or merged_fp is None
                        or decision is None
                        or str(base_fp["status"]) != "failed"
                    ):
                        decision_mismatches += 1
                        continue
                    base_attempts = base.execute(
                        """SELECT * FROM fetch_attempts WHERE run_id=?
                           AND source_asset_id=? ORDER BY attempt_no""",
                        (base_id, asset_id),
                    ).fetchall()
                    recovery_attempts = recovery.execute(
                        """SELECT * FROM fetch_attempts WHERE run_id=?
                           AND source_asset_id=? ORDER BY attempt_no""",
                        (recovery_id, asset_id),
                    ).fetchall()
                    offset = max(
                        (int(row["attempt_no"]) for row in base_attempts), default=0
                    )
                    recovery_success = str(recovery_fp["status"]) == "success"
                    decision_mismatches += int(
                        str(decision["recovery_sha256"]) != recovery_sha
                        or str(decision["base_error_kind"])
                        != str(base_fp["error_kind"])
                        or str(decision["recovery_status"])
                        != str(recovery_fp["status"])
                        or decision["recovery_error_kind"]
                        != recovery_fp["error_kind"]
                        or int(decision["attempt_offset"]) != offset
                        or int(decision["attempt_count"]) != len(recovery_attempts)
                        or int(decision["applied_success"]) != int(recovery_success)
                    )
                    for recovery_attempt in recovery_attempts:
                        merged_attempt = merged.execute(
                            """SELECT * FROM fetch_attempts WHERE run_id=?
                               AND source_asset_id=? AND attempt_no=?""",
                            (
                                merged_id,
                                asset_id,
                                offset + int(recovery_attempt["attempt_no"]),
                            ),
                        ).fetchone()
                        if merged_attempt is None or any(
                            merged_attempt[name] != recovery_attempt[name]
                            for name in _ATTEMPT_COLUMNS
                        ):
                            attempt_mismatches += 1
                    expected_fp = recovery_fp if recovery_success else base_fp
                    expected_selected = expected_fp["selected_attempt_no"]
                    if recovery_success and expected_selected is not None:
                        expected_selected = offset + int(expected_selected)
                    fingerprint_mismatches += int(
                        merged_fp["selected_attempt_no"] != expected_selected
                        or any(
                            merged_fp[name] != expected_fp[name]
                            for name in _FINGERPRINT_COLUMNS
                            if name != "selected_attempt_no"
                        )
                    )
            actual_decisions = int(
                merged.execute("SELECT count(*) FROM recovery_merge_decisions").fetchone()[0]
            )
            base_attempt_count = int(
                base.execute("SELECT count(*) FROM fetch_attempts").fetchone()[0]
            )
            recovery_attempt_count = sum(
                int(recovery.execute("SELECT count(*) FROM fetch_attempts").fetchone()[0])
                for recovery in recoveries
            )
            merged_attempt_count = int(
                merged.execute("SELECT count(*) FROM fetch_attempts").fetchone()[0]
            )
            add(
                "merge_decision_ledger",
                decision_mismatches == 0 and actual_decisions == expected_decisions,
                {"count": expected_decisions, "mismatches": 0},
                {"count": actual_decisions, "mismatches": decision_mismatches},
            )
            add("merge_attempt_offsets", attempt_mismatches == 0, 0, attempt_mismatches)
            add(
                "merge_attempt_accounting",
                merged_attempt_count == base_attempt_count + recovery_attempt_count,
                base_attempt_count + recovery_attempt_count,
                merged_attempt_count,
            )
            add(
                "merge_fingerprint_values",
                fingerprint_mismatches == 0,
                0,
                fingerprint_mismatches,
            )
        finally:
            for recovery in recoveries:
                recovery.close()
            base.close()
            merged.close()

        base_success = base_counts.get("success", 0)
        base_failed = base_counts.get("failed", 0)
        final_success = merged_counts.get("success", 0)
        final_failed = merged_counts.get("failed", 0)
        recovered = final_success - base_success
        add(
            "merge_accounting",
            recovered >= 0
            and final_failed == base_failed - recovered
            and not (set(merged_counts) - {"success", "failed"}),
            {"success_delta": recovered, "failed": base_failed - recovered},
            {"success_delta": recovered, "failed": final_failed},
        )
    except Exception as exc:
        add("merge_validator_exception", False, "no exception", str(exc))

    ending_hashes = {path: _sha256_file(path) for path in paths}
    changed = {
        str(path): {"before": initial_hashes[path], "after": ending_hashes[path]}
        for path in paths
        if ending_hashes[path] != initial_hashes[path]
    }
    add("immutable_inputs_unchanged", not changed, {}, changed)
    return RecoveryMergeValidation(
        bool(checks) and all(check[1] for check in checks), tuple(checks)
    )


_MERGE_SCHEMA = """
CREATE TABLE recovery_merge_lineage(
  merge_id TEXT PRIMARY KEY
    REFERENCES fingerprint_runs(run_id)
    ON UPDATE RESTRICT ON DELETE RESTRICT,
  merge_version TEXT NOT NULL,
  manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json)),
  manifest_sha256 TEXT NOT NULL
    CHECK(length(manifest_sha256)=64
      AND manifest_sha256=lower(manifest_sha256)
      AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  created_at TEXT NOT NULL
);
CREATE TABLE recovery_merge_decisions(
  merge_id TEXT NOT NULL,
  recovery_ordinal INTEGER NOT NULL CHECK(recovery_ordinal >= 1),
  recovery_sha256 TEXT NOT NULL
    CHECK(length(recovery_sha256)=64
      AND recovery_sha256=lower(recovery_sha256)
      AND recovery_sha256 NOT GLOB '*[^0-9a-f]*'),
  source_asset_id TEXT NOT NULL,
  base_error_kind TEXT,
  recovery_status TEXT NOT NULL
    CHECK(recovery_status IN ('success','failed')),
  recovery_error_kind TEXT,
  attempt_offset INTEGER NOT NULL CHECK(attempt_offset >= 0),
  attempt_count INTEGER NOT NULL CHECK(attempt_count >= 1),
  applied_success INTEGER NOT NULL CHECK(applied_success IN (0,1)),
  PRIMARY KEY(merge_id,recovery_ordinal,source_asset_id),
  FOREIGN KEY(merge_id) REFERENCES recovery_merge_lineage(merge_id)
    ON UPDATE RESTRICT ON DELETE RESTRICT,
  FOREIGN KEY(merge_id,source_asset_id)
    REFERENCES fingerprints(run_id,source_asset_id)
    ON UPDATE RESTRICT ON DELETE RESTRICT,
  CHECK((recovery_status='success' AND applied_success=1
         AND recovery_error_kind IS NULL)
     OR (recovery_status='failed' AND applied_success=0
         AND recovery_error_kind IS NOT NULL))
);
CREATE TABLE recovery_merge_progress(
  merge_id TEXT PRIMARY KEY
    REFERENCES recovery_merge_lineage(merge_id)
    ON UPDATE RESTRICT ON DELETE RESTRICT,
  phase TEXT NOT NULL CHECK(phase IN (
    'inventory_assets','inventory_exclusions','base_attempts',
    'base_fingerprints','recoveries','ready_validation'
  )),
  selected_cursor INTEGER NOT NULL DEFAULT 0 CHECK(selected_cursor >= 0),
  exclusion_cursor INTEGER NOT NULL DEFAULT 0 CHECK(exclusion_cursor >= 0),
  base_fingerprint_cursor INTEGER NOT NULL DEFAULT 0
    CHECK(base_fingerprint_cursor >= 0),
  base_attempt_selection_cursor INTEGER NOT NULL DEFAULT 0
    CHECK(base_attempt_selection_cursor >= 0),
  base_attempt_no_cursor INTEGER NOT NULL DEFAULT 0
    CHECK(base_attempt_no_cursor >= 0),
  recovery_ordinal INTEGER NOT NULL DEFAULT 1 CHECK(recovery_ordinal >= 1),
  recovery_selection_cursor INTEGER NOT NULL DEFAULT 0
    CHECK(recovery_selection_cursor >= 0),
  applied_success INTEGER NOT NULL DEFAULT 0 CHECK(applied_success >= 0),
  updated_at TEXT NOT NULL
);
CREATE TRIGGER recovery_merge_lineage_initializing_insert
BEFORE INSERT ON recovery_merge_lineage
WHEN coalesce((SELECT status FROM fingerprint_runs
               WHERE run_id=NEW.merge_id), '')<>'initializing'
BEGIN
  SELECT RAISE(ABORT, 'merge lineage requires an initializing run');
END;
CREATE TRIGGER recovery_merge_lineage_immutable_update
BEFORE UPDATE ON recovery_merge_lineage
BEGIN
  SELECT RAISE(ABORT, 'merge lineage is immutable');
END;
CREATE TRIGGER recovery_merge_lineage_immutable_delete
BEFORE DELETE ON recovery_merge_lineage
BEGIN
  SELECT RAISE(ABORT, 'merge lineage is immutable');
END;
CREATE TRIGGER recovery_merge_decisions_running_insert
BEFORE INSERT ON recovery_merge_decisions
WHEN coalesce((SELECT status FROM fingerprint_runs
               WHERE run_id=NEW.merge_id), '')<>'running'
BEGIN
  SELECT RAISE(ABORT, 'merge decisions require a running run');
END;
CREATE TRIGGER recovery_merge_decisions_immutable_update
BEFORE UPDATE ON recovery_merge_decisions
BEGIN
  SELECT RAISE(ABORT, 'merge decisions are immutable');
END;
CREATE TRIGGER recovery_merge_decisions_immutable_delete
BEFORE DELETE ON recovery_merge_decisions
BEGIN
  SELECT RAISE(ABORT, 'merge decisions are immutable');
END;
CREATE TRIGGER recovery_merge_progress_initializing_insert
BEFORE INSERT ON recovery_merge_progress
WHEN coalesce((SELECT status FROM fingerprint_runs
               WHERE run_id=NEW.merge_id), '')<>'initializing'
BEGIN
  SELECT RAISE(ABORT, 'merge progress requires an initializing run');
END;
CREATE TRIGGER recovery_merge_progress_active_update
BEFORE UPDATE ON recovery_merge_progress
WHEN coalesce((SELECT status FROM fingerprint_runs
               WHERE run_id=NEW.merge_id), '') NOT IN ('initializing','running')
BEGIN
  SELECT RAISE(ABORT, 'terminal merge progress is immutable');
END;
CREATE TRIGGER recovery_merge_progress_immutable_delete
BEFORE DELETE ON recovery_merge_progress
BEGIN
  SELECT RAISE(ABORT, 'merge progress is immutable');
END;
"""


def _checkpoint(
    hook: _CheckpointHook | None,
    phase: str,
    cursor: int,
) -> None:
    if hook is not None:
        hook(phase, cursor)


def _progress(connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM recovery_merge_progress WHERE merge_id=?", (run_id,)
    ).fetchone()
    if row is None:
        raise PipelineError("merge partial has no durable progress row")
    if str(row["phase"]) not in _MERGE_PHASES:
        raise PipelineError("merge partial has an invalid progress phase")
    return row


def _assert_partial_identity(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    manifest_json: str,
    manifest_sha: str,
    merge_version: str,
    source: str,
    source_path: Path,
    source_sha: str,
    dependency_json: str,
    dependency_sha: str,
    runner_version: str,
    max_attempts: int,
    base_run: dict[str, object],
) -> tuple[dict[str, object], sqlite3.Row]:
    rows = connection.execute("SELECT * FROM fingerprint_runs").fetchall()
    if len(rows) != 1:
        raise PipelineError("merge partial must contain exactly one run")
    run = dict(rows[0])
    expected = {
        "run_id": run_id,
        "source_name": source,
        "source_db_path": str(source_path),
        "source_db_sha256_before": source_sha,
        "fingerprint_contract_version": base_run["fingerprint_contract_version"],
        "dependency_manifest_json": dependency_json,
        "dependency_manifest_sha256": dependency_sha,
        "runner_version": runner_version,
        "retry_policy_version": base_run["retry_policy_version"],
        "max_attempts": max_attempts,
        "selection_manifest_sha256": base_run["selection_manifest_sha256"],
        "selection_mode": "full",
        "selection_count": int(base_run["selection_count"]),
        "sample_seed": None,
        "selection_version": base_run["selection_version"],
        "source_inventory_manifest_sha256": base_run[
            "source_inventory_manifest_sha256"
        ],
        "exclusion_manifest_sha256": base_run["exclusion_manifest_sha256"],
        "source_total_count": int(base_run["source_total_count"]),
        "eligible_count": int(base_run["eligible_count"]),
        "excluded_count": int(base_run["excluded_count"]),
    }
    mismatches = {
        name: {"expected": value, "actual": run.get(name)}
        for name, value in expected.items()
        if run.get(name) != value
    }
    if mismatches:
        raise PipelineError(
            "merge partial provenance does not match requested manifest: "
            + _canonical_json(mismatches)
        )
    lineage_rows = connection.execute(
        "SELECT * FROM recovery_merge_lineage"
    ).fetchall()
    if len(lineage_rows) != 1:
        raise PipelineError("merge partial must contain exactly one lineage row")
    lineage = lineage_rows[0]
    if not (
        str(lineage["merge_id"]) == run_id
        and str(lineage["merge_version"]) == merge_version
        and str(lineage["manifest_json"]) == manifest_json
        and str(lineage["manifest_sha256"]) == manifest_sha
    ):
        raise PipelineError("merge partial lineage does not match requested manifest")
    progress = _progress(connection, run_id)
    selected_rows = int(
        connection.execute(
            "SELECT count(*) FROM source_assets WHERE run_id=?", (run_id,)
        ).fetchone()[0]
    )
    exclusion_rows = int(
        connection.execute(
            "SELECT count(*) FROM source_asset_exclusions WHERE run_id=?", (run_id,)
        ).fetchone()[0]
    )
    fingerprint_rows = int(
        connection.execute(
            "SELECT count(*) FROM fingerprints WHERE run_id=?", (run_id,)
        ).fetchone()[0]
    )
    if not (
        selected_rows == fingerprint_rows == int(run["initialized_selected_count"])
        and exclusion_rows == int(run["initialized_excluded_count"])
        and int(run["initialized_inventory_count"])
        == selected_rows + exclusion_rows
    ):
        raise PipelineError("merge partial initialization counters are inconsistent")
    max_selected = int(
        connection.execute(
            "SELECT coalesce(max(selection_rank),0) FROM source_assets WHERE run_id=?",
            (run_id,),
        ).fetchone()[0]
    )
    max_exclusion = int(
        connection.execute(
            "SELECT coalesce(max(inventory_rank),0) FROM source_asset_exclusions "
            "WHERE run_id=?",
            (run_id,),
        ).fetchone()[0]
    )
    if (
        max_selected != int(progress["selected_cursor"])
        or max_exclusion != int(progress["exclusion_cursor"])
    ):
        raise PipelineError("merge partial inventory cursor is inconsistent")
    decision_count = int(
        connection.execute(
            "SELECT count(*) FROM recovery_merge_decisions WHERE merge_id=?",
            (run_id,),
        ).fetchone()[0]
    )
    applied = int(
        connection.execute(
            "SELECT coalesce(sum(applied_success),0) "
            "FROM recovery_merge_decisions WHERE merge_id=?",
            (run_id,),
        ).fetchone()[0]
    )
    if decision_count and str(progress["phase"]) != "recoveries" and str(
        progress["phase"]
    ) != "ready_validation":
        raise PipelineError("merge decisions exist before the recovery phase")
    if applied != int(progress["applied_success"]):
        raise PipelineError("merge applied-success progress is inconsistent")
    return run, progress


def _seed_partial(
    partial: Path,
    *,
    run_id: str,
    source: str,
    source_path: Path,
    source_sha: str,
    dependency_json: str,
    dependency_sha: str,
    manifest_json: str,
    manifest_sha: str,
    runner_version: str,
    max_attempts: int,
    base_run: dict[str, object],
) -> None:
    connection = initialize_sidecar(partial)
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(_MERGE_SCHEMA)
        connection.execute("BEGIN IMMEDIATE")
        now = _utc_now()
        connection.execute(
            """
            INSERT INTO fingerprint_runs(
              run_id,source_name,source_db_path,source_db_sha256_before,
              fingerprint_contract_version,dependency_manifest_json,
              dependency_manifest_sha256,runner_version,retry_policy_version,
              max_attempts,selection_manifest_sha256,selection_mode,
              selection_count,sample_seed,selection_version,
              source_inventory_manifest_sha256,exclusion_manifest_sha256,
              source_total_count,eligible_count,excluded_count,status,started_at,
              initialization_updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'initializing',?,?)
            """,
            (
                run_id,
                source,
                str(source_path),
                source_sha,
                base_run["fingerprint_contract_version"],
                dependency_json,
                dependency_sha,
                runner_version,
                base_run["retry_policy_version"],
                max_attempts,
                base_run["selection_manifest_sha256"],
                "full",
                int(base_run["selection_count"]),
                None,
                base_run["selection_version"],
                base_run["source_inventory_manifest_sha256"],
                base_run["exclusion_manifest_sha256"],
                int(base_run["source_total_count"]),
                int(base_run["eligible_count"]),
                int(base_run["excluded_count"]),
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO recovery_merge_lineage VALUES(?,?,?,?,?)",
            (run_id, MERGE_VERSION, manifest_json, manifest_sha, now),
        )
        connection.execute(
            """INSERT INTO recovery_merge_progress(
                 merge_id,phase,updated_at
               ) VALUES(?,?,?)""",
            (run_id, "inventory_assets", now),
        )
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def _advance_merge_batches(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    base_run_id: str,
    recovery_paths: Sequence[Path],
    recovery_shas: Sequence[str],
    batch_size: int,
    checkpoint_hook: _CheckpointHook | None,
) -> int:
    while True:
        progress = _progress(connection, run_id)
        phase = str(progress["phase"])
        if phase == "ready_validation":
            return int(progress["applied_success"])

        if phase == "inventory_assets":
            cursor = int(progress["selected_cursor"])
            ranks = connection.execute(
                """SELECT selection_rank FROM base_e1.source_assets
                   WHERE run_id=? AND selection_rank>?
                   ORDER BY selection_rank LIMIT ?""",
                (base_run_id, cursor, batch_size),
            ).fetchall()
            if ranks:
                last = int(ranks[-1][0])
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO source_assets(
                      run_id,source_asset_id,selection_rank,canonical_url,fetch_url,
                      source_record_sha256,provenance_json
                    )
                    SELECT ?,source_asset_id,selection_rank,canonical_url,fetch_url,
                           source_record_sha256,provenance_json
                    FROM base_e1.source_assets
                    WHERE run_id=? AND selection_rank>? AND selection_rank<=?
                    ORDER BY selection_rank
                    """,
                    (run_id, base_run_id, cursor, last),
                )
                connection.execute(
                    """INSERT INTO fingerprints(run_id,source_asset_id,status)
                       SELECT ?,source_asset_id,'pending'
                       FROM base_e1.source_assets
                       WHERE run_id=? AND selection_rank>? AND selection_rank<=?
                       ORDER BY selection_rank""",
                    (run_id, base_run_id, cursor, last),
                )
                count = len(ranks)
                now = _utc_now()
                connection.execute(
                    """UPDATE fingerprint_runs SET
                         initialized_inventory_count=initialized_inventory_count+?,
                         initialized_selected_count=initialized_selected_count+?,
                         initialization_updated_at=? WHERE run_id=?""",
                    (count, count, now, run_id),
                )
                connection.execute(
                    """UPDATE recovery_merge_progress SET selected_cursor=?,updated_at=?
                       WHERE merge_id=?""",
                    (last, now, run_id),
                )
                connection.commit()
                _checkpoint(checkpoint_hook, phase, last)
                continue
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE recovery_merge_progress
                   SET phase='inventory_exclusions',updated_at=? WHERE merge_id=?""",
                (_utc_now(), run_id),
            )
            connection.commit()
            continue

        if phase == "inventory_exclusions":
            cursor = int(progress["exclusion_cursor"])
            ranks = connection.execute(
                """SELECT inventory_rank FROM base_e1.source_asset_exclusions
                   WHERE run_id=? AND inventory_rank>?
                   ORDER BY inventory_rank LIMIT ?""",
                (base_run_id, cursor, batch_size),
            ).fetchall()
            if ranks:
                last = int(ranks[-1][0])
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO source_asset_exclusions(
                      run_id,source_asset_id,source_asset_key,inventory_rank,reason_code,
                      source_record_sha256,provenance_json,detail_json
                    )
                    SELECT ?,source_asset_id,source_asset_key,inventory_rank,reason_code,
                           source_record_sha256,provenance_json,detail_json
                    FROM base_e1.source_asset_exclusions
                    WHERE run_id=? AND inventory_rank>? AND inventory_rank<=?
                    ORDER BY inventory_rank
                    """,
                    (run_id, base_run_id, cursor, last),
                )
                count = len(ranks)
                now = _utc_now()
                connection.execute(
                    """UPDATE fingerprint_runs SET
                         initialized_inventory_count=initialized_inventory_count+?,
                         initialized_excluded_count=initialized_excluded_count+?,
                         initialization_updated_at=? WHERE run_id=?""",
                    (count, count, now, run_id),
                )
                connection.execute(
                    """UPDATE recovery_merge_progress SET exclusion_cursor=?,updated_at=?
                       WHERE merge_id=?""",
                    (last, now, run_id),
                )
                connection.commit()
                _checkpoint(checkpoint_hook, phase, last)
                continue
            run = connection.execute(
                "SELECT * FROM fingerprint_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if not (
                int(run["initialized_inventory_count"])
                == int(run["source_total_count"])
                and int(run["initialized_selected_count"])
                == int(run["selection_count"])
                and int(run["initialized_excluded_count"])
                == int(run["excluded_count"])
            ):
                raise PipelineError("merge inventory copy completed with wrong counts")
            now = _utc_now()
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE fingerprint_runs SET status='running',
                     initialization_updated_at=?,initialization_completed_at=?
                   WHERE run_id=? AND status='initializing'""",
                (now, now, run_id),
            )
            connection.execute(
                """UPDATE recovery_merge_progress
                   SET phase='base_attempts',updated_at=? WHERE merge_id=?""",
                (now, run_id),
            )
            connection.commit()
            continue

        if phase == "base_fingerprints":
            cursor = int(progress["base_fingerprint_cursor"])
            ranks = connection.execute(
                """SELECT selection_rank FROM base_e1.source_assets
                   WHERE run_id=? AND selection_rank>?
                   ORDER BY selection_rank LIMIT ?""",
                (base_run_id, cursor, batch_size),
            ).fetchall()
            if ranks:
                last = int(ranks[-1][0])
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    UPDATE fingerprints AS target SET
                      status=b.status,selected_attempt_no=b.selected_attempt_no,
                      raw_response_sha256=b.raw_response_sha256,
                      normalized_pixel_sha256=b.normalized_pixel_sha256,
                      phash_hex=b.phash_hex,decoded_format=b.decoded_format,
                      original_width=b.original_width,original_height=b.original_height,
                      normalized_width=b.normalized_width,
                      normalized_height=b.normalized_height,
                      metadata_json=b.metadata_json,completed_at=b.completed_at,
                      error_kind=b.error_kind,error_message=b.error_message
                    FROM base_e1.fingerprints AS b, base_e1.source_assets AS s
                    WHERE target.run_id=? AND b.run_id=? AND s.run_id=?
                      AND b.source_asset_id=target.source_asset_id
                      AND s.source_asset_id=b.source_asset_id
                      AND s.selection_rank>? AND s.selection_rank<=?
                    """,
                    (run_id, base_run_id, base_run_id, cursor, last),
                )
                connection.execute(
                    """UPDATE recovery_merge_progress
                       SET base_fingerprint_cursor=?,updated_at=? WHERE merge_id=?""",
                    (last, _utc_now(), run_id),
                )
                connection.commit()
                _checkpoint(checkpoint_hook, phase, last)
                continue
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE recovery_merge_progress
                   SET phase='recoveries',updated_at=? WHERE merge_id=?""",
                (_utc_now(), run_id),
            )
            connection.commit()
            continue

        if phase == "base_attempts":
            rank_cursor = int(progress["base_attempt_selection_cursor"])
            attempt_cursor = int(progress["base_attempt_no_cursor"])
            keys = connection.execute(
                """SELECT s.selection_rank,a.attempt_no
                   FROM base_e1.fetch_attempts AS a
                   JOIN base_e1.source_assets AS s
                     ON s.run_id=a.run_id AND s.source_asset_id=a.source_asset_id
                   WHERE a.run_id=? AND
                     (s.selection_rank>? OR
                      (s.selection_rank=? AND a.attempt_no>?))
                   ORDER BY s.selection_rank,a.attempt_no LIMIT ?""",
                (
                    base_run_id,
                    rank_cursor,
                    rank_cursor,
                    attempt_cursor,
                    batch_size,
                ),
            ).fetchall()
            if keys:
                last_rank = int(keys[-1][0])
                last_attempt = int(keys[-1][1])
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO fetch_attempts(
                      run_id,source_asset_id,attempt_no,request_url,started_at,completed_at,
                      elapsed_ms,outcome,http_status,response_mime,response_bytes,final_url,
                      raw_response_sha256,error_kind,error_message,retry_after_seconds,
                      scheduled_delay_seconds,worker_no
                    )
                    SELECT ?,a.source_asset_id,a.attempt_no,a.request_url,a.started_at,
                           a.completed_at,a.elapsed_ms,a.outcome,a.http_status,
                           a.response_mime,a.response_bytes,a.final_url,
                           a.raw_response_sha256,a.error_kind,a.error_message,
                           a.retry_after_seconds,a.scheduled_delay_seconds,a.worker_no
                    FROM base_e1.fetch_attempts AS a
                    JOIN base_e1.source_assets AS s
                      ON s.run_id=a.run_id AND s.source_asset_id=a.source_asset_id
                    WHERE a.run_id=?
                      AND (s.selection_rank>? OR
                           (s.selection_rank=? AND a.attempt_no>?))
                      AND (s.selection_rank<? OR
                           (s.selection_rank=? AND a.attempt_no<=?))
                    ORDER BY s.selection_rank,a.attempt_no
                    """,
                    (
                        run_id,
                        base_run_id,
                        rank_cursor,
                        rank_cursor,
                        attempt_cursor,
                        last_rank,
                        last_rank,
                        last_attempt,
                    ),
                )
                connection.execute(
                    """UPDATE recovery_merge_progress SET
                         base_attempt_selection_cursor=?,base_attempt_no_cursor=?,
                         updated_at=? WHERE merge_id=?""",
                    (last_rank, last_attempt, _utc_now(), run_id),
                )
                connection.commit()
                _checkpoint(checkpoint_hook, phase, last_rank)
                continue
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE recovery_merge_progress SET phase='base_fingerprints',
                     updated_at=? WHERE merge_id=?""",
                (_utc_now(), run_id),
            )
            connection.commit()
            continue

        if phase == "recoveries":
            ordinal = int(progress["recovery_ordinal"])
            cursor = int(progress["recovery_selection_cursor"])
            if ordinal > len(recovery_paths):
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """UPDATE recovery_merge_progress
                       SET phase='ready_validation',updated_at=? WHERE merge_id=?""",
                    (_utc_now(), run_id),
                )
                connection.commit()
                _checkpoint(
                    checkpoint_hook,
                    "ready_validation",
                    int(progress["applied_success"]),
                )
                continue
            connection.execute("BEGIN IMMEDIATE")
            applied, last, count = _apply_recovery(
                connection,
                recovery_path=recovery_paths[ordinal - 1],
                recovery_ordinal=ordinal,
                recovery_sha=recovery_shas[ordinal - 1],
                new_run_id=run_id,
                after_selection_rank=cursor,
                limit=batch_size,
            )
            if count:
                connection.execute(
                    """UPDATE recovery_merge_progress SET
                         recovery_selection_cursor=?,
                         applied_success=applied_success+?,updated_at=?
                       WHERE merge_id=?""",
                    (last, applied, _utc_now(), run_id),
                )
                connection.commit()
                _checkpoint(checkpoint_hook, phase, last)
            else:
                connection.execute(
                    """UPDATE recovery_merge_progress SET recovery_ordinal=?,
                         recovery_selection_cursor=0,updated_at=? WHERE merge_id=?""",
                    (ordinal + 1, _utc_now(), run_id),
                )
                connection.commit()


def merge_image_fingerprint_recoveries(
    *,
    source_db: Path | str,
    base_sidecar: Path | str,
    recovery_sidecars: Sequence[Path | str],
    output: Path | str,
    inventory_factory: Callable[[], Iterable[InventoryDecision]] | None = None,
    resume: bool = False,
    batch_size: int = MERGE_BATCH_SIZE,
    _checkpoint_hook: _CheckpointHook | None = None,
) -> RecoveryMergeResult:
    """Build and atomically publish a new full E1 sidecar.

    Only successful recovery fingerprints may supersede base rows, and a base
    success is never a valid recovery target.
    """

    source_path = Path(source_db).resolve()
    base_path = Path(base_sidecar).resolve()
    recovery_paths = tuple(Path(path).resolve() for path in recovery_sidecars)
    output_path = Path(output).resolve()
    if not recovery_paths:
        raise ValueError("at least one recovery sidecar is required")
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    for path in (source_path, base_path, *recovery_paths):
        if not path.is_file():
            raise FileNotFoundError(path)
        _assert_snapshot(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    partial = Path(str(output_path) + ".partial")

    lock_path = Path(str(output_path) + ".lock")
    descriptor = _acquire_lock(lock_path)
    try:
        source_sha = _sha256_file(source_path)
        base_sha = _sha256_file(base_path)
        recovery_shas = tuple(_sha256_file(path) for path in recovery_paths)
        base_run, base_counts = _sidecar_run(base_path)
        source = str(base_run["source_name"])
        base_run_id = str(base_run["run_id"])
        unsupported_base_statuses = sorted(
            set(base_counts) - {"success", "failed"}
        )
        if unsupported_base_statuses:
            raise PipelineError(
                "base E1 sidecar contains unsupported fingerprint statuses for "
                "recovery merge: " + ", ".join(unsupported_base_statuses)
            )
        if str(base_run["selection_mode"]) != "full":
            raise PipelineError("base E1 sidecar must be a full inventory run")
        if str(base_run["status"]) not in {"complete", "complete_with_failures"}:
            raise PipelineError("base E1 sidecar is not complete")
        if str(base_run["source_db_sha256_before"]) != source_sha:
            raise PipelineError("base/source SHA mismatch")
        base_validation = validate_image_fingerprint_sidecar(
            base_path, source_path, inventory_factory=inventory_factory
        )
        if not base_validation.passed:
            raise PipelineError("base sidecar failed independent validation")
        base_dependencies, base_dependency_lineage, stripped_base_dependency = (
            _dependency_document(base_run, label="base")
        )
        if base_dependency_lineage is not None:
            raise PipelineError("merge base must be an ordinary full E1 run")

        recovery_metadata: list[dict[str, object]] = []
        recovery_lineage_validations: list[dict[str, object]] = []
        seen_recovery_assets: set[str] = set()
        for path, sha in zip(recovery_paths, recovery_shas, strict=True):
            run, recovery_counts = _sidecar_run(path)
            unsupported_recovery_statuses = sorted(
                set(recovery_counts) - {"success", "failed"}
            )
            if unsupported_recovery_statuses:
                raise PipelineError(
                    "recovery E1 sidecar contains unsupported fingerprint statuses "
                    "for recovery merge: "
                    + ", ".join(unsupported_recovery_statuses)
                )
            lineage = _lineage_payload(run)
            if str(run["source_name"]) != source:
                raise PipelineError("recovery source mismatch")
            if not (
                str(run["source_db_sha256_before"])
                == str(run["source_db_sha256_after"])
                == source_sha
            ):
                raise PipelineError("recovery source before/after SHA mismatch")
            if str(run["fingerprint_contract_version"]) != str(
                base_run["fingerprint_contract_version"]
            ):
                raise PipelineError("recovery fingerprint contract mismatch")
            if str(run["selection_version"]) != str(base_run["selection_version"]):
                raise PipelineError("recovery selection version mismatch")
            recovery_dependencies, _, stripped_recovery_dependency = (
                _dependency_document(run, label=f"recovery {path.name}")
            )
            del recovery_dependencies
            if stripped_recovery_dependency != stripped_base_dependency:
                raise PipelineError("recovery/base stripped dependency mismatch")
            if lineage.get("base_sidecar_sha256") != base_sha:
                raise PipelineError("recovery lineage base SHA mismatch")
            if lineage.get("base_run_id") != base_run_id:
                raise PipelineError("recovery lineage base run mismatch")
            missing_additive = sorted(
                RECOVERY_LINEAGE_ADDITIVE_FIELDS - set(lineage)
            )
            derived_values = {
                "base_source_db_sha256_before": str(
                    base_run["source_db_sha256_before"]
                ),
                "base_source_db_sha256_after": str(
                    base_run["source_db_sha256_after"]
                ),
                "base_fingerprint_contract_version": str(
                    base_run["fingerprint_contract_version"]
                ),
                "base_selection_version": str(base_run["selection_version"]),
                "base_dependency_manifest_sha256": str(
                    base_run["dependency_manifest_sha256"]
                ),
            }
            lineage_validation: dict[str, object] = {
                "recovery_sha256": sha,
                "validation_mode": (
                    "legacy_additive_upgrade" if missing_additive else "strict"
                ),
                "missing_additive_fields": missing_additive,
                "derived_fields": {
                    field: derived_values[field] for field in missing_additive
                },
            }
            try:
                # A shape-valid ``_run_lineage`` accepted by the generic E1
                # runner is not sufficient authority.  Rebind every child to
                # this exact parent/source before any output rows are created.
                assets = validate_failure_recovery_sidecar(
                    path,
                    source_path,
                    base_path,
                    inventory_factory=inventory_factory,
                    allow_legacy_lineage_upgrade=True,
                )
            except Exception as exc:
                raise PipelineError(
                    f"recovery sidecar failed lineage validation: {path}"
                ) from exc
            asset_ids = {asset.source_asset_id for asset in assets}
            overlap = sorted(seen_recovery_assets & asset_ids)
            if overlap:
                raise PipelineError(
                    "recovery sidecars overlap on source asset IDs: "
                    + ", ".join(overlap[:10])
                )
            seen_recovery_assets.update(asset_ids)
            recovery_lineage_validations.append(lineage_validation)
            recovery_metadata.append(
                {
                    "path": str(path),
                    "sha256": sha,
                    "run_id": str(run["run_id"]),
                    "selection_count": int(run["selection_count"]),
                    "selection_manifest_sha256": str(run["selection_manifest_sha256"]),
                    "lineage_validation": lineage_validation,
                }
            )

        manifest = {
            "base": {
                "path": str(base_path),
                "run_id": base_run_id,
                "sha256": base_sha,
            },
            "merge_version": MERGE_VERSION,
            "recoveries": recovery_metadata,
            "source": {
                "name": source,
                "path": str(source_path),
                "sha256": source_sha,
            },
        }
        manifest_json = _canonical_json(manifest)
        manifest_sha = hashlib.sha256(manifest_json.encode("ascii")).hexdigest()
        dependency_json, dependency_sha, _merge_lineage = _merged_dependency_manifest(
            base_dependencies,
            manifest_sha256=manifest_sha,
            base_sha256=base_sha,
            recovery_sha256s=recovery_shas,
            recovery_lineage_validations=recovery_lineage_validations,
            source_sha256=source_sha,
        )
        new_run_id = f"e1-{source}-merged-{manifest_sha[:20]}"
        max_attempts = max(
            int(base_run["max_attempts"]),
            _max_merged_attempts(base_path, recovery_paths),
        )

        runner_version = f"{base_run['runner_version']}+{MERGE_VERSION}"
        immutable_inputs = (
            (source_path, source_sha),
            (base_path, base_sha),
            *tuple(zip(recovery_paths, recovery_shas, strict=True)),
        )

        def validated_result(path: Path, applied: int) -> RecoveryMergeResult:
            _, _, recovered = _validate_base_success_unchanged(
                path, base_path, expected_recovered=applied
            )
            _validate_recovery_successes_applied(path, recovery_paths)
            final_run, final_counts = _sidecar_run(path)
            if str(final_run["status"]) not in {
                "complete",
                "complete_with_failures",
            }:
                raise PipelineError("merged sidecar is not terminal and complete")
            final_success = final_counts.get("success", 0)
            final_failed = final_counts.get("failed", 0) + final_counts.get(
                "skipped", 0
            )
            base_success = base_counts.get("success", 0)
            base_failed = base_counts.get("failed", 0) + base_counts.get(
                "skipped", 0
            )
            if (
                final_success != base_success + recovered
                or final_failed != base_failed - recovered
            ):
                raise PipelineError("merged fingerprint accounting is inconsistent")
            independent = validate_image_fingerprint_recovery_merge(
                path,
                source_path,
                base_path,
                recovery_paths,
                inventory_factory=inventory_factory,
            )
            if not independent.passed:
                failed = [
                    name for name, passed, _, _ in independent.checks if not passed
                ]
                raise PipelineError(
                    "merged sidecar failed merge-specific independent validation: "
                    + ", ".join(failed)
                )
            _assert_inputs_unchanged(immutable_inputs, label="final validation")
            return RecoveryMergeResult(
                output_path=path,
                source=source,
                base_sidecar_sha256=base_sha,
                recovery_sidecar_sha256s=recovery_shas,
                output_sha256=_sha256_file(path),
                base_success=base_success,
                base_failed=base_failed,
                recovered=recovered,
                final_success=final_success,
                final_failed=final_failed,
                run_status=str(final_run["status"]),
                manifest_sha256=manifest_sha,
            )

        def checked_progress(path: Path, *, writable: bool) -> tuple[str, int]:
            connection = (
                sqlite3.connect(f"file:{path.as_posix()}?mode=rw", uri=True)
                if writable
                else open_sidecar(path, readonly=True)
            )
            if writable:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
            try:
                run, progress = _assert_partial_identity(
                    connection,
                    run_id=new_run_id,
                    manifest_json=manifest_json,
                    manifest_sha=manifest_sha,
                    merge_version=MERGE_VERSION,
                    source=source,
                    source_path=source_path,
                    source_sha=source_sha,
                    dependency_json=dependency_json,
                    dependency_sha=dependency_sha,
                    runner_version=runner_version,
                    max_attempts=max_attempts,
                    base_run=base_run,
                )
                return str(run["status"]), int(progress["applied_success"])
            finally:
                connection.close()

        if output_path.exists():
            if not resume:
                raise FileExistsError(f"refusing to clobber output: {output_path}")
            _assert_snapshot(output_path)
            status, applied = checked_progress(output_path, writable=False)
            if status not in {"complete", "complete_with_failures"}:
                raise PipelineError("published merge sidecar is not complete")
            return validated_result(output_path, applied)

        if partial.exists() and not resume:
            raise FileExistsError(
                f"partial exists; use --resume or choose another output: {partial}"
            )
        if not partial.exists() and resume:
            raise FileNotFoundError(f"resume partial does not exist: {partial}")
        if partial.exists():
            recover_sidecar(partial)
            status, applied = checked_progress(partial, writable=True)
            if status not in {
                "initializing",
                "running",
                "complete",
                "complete_with_failures",
            }:
                raise PipelineError(f"terminal {status!r} merge cannot be resumed")
        else:
            _seed_partial(
                partial,
                run_id=new_run_id,
                source=source,
                source_path=source_path,
                source_sha=source_sha,
                dependency_json=dependency_json,
                dependency_sha=dependency_sha,
                manifest_json=manifest_json,
                manifest_sha=manifest_sha,
                runner_version=runner_version,
                max_attempts=max_attempts,
                base_run=base_run,
            )
            status = "initializing"
            applied = 0

        if status in {"initializing", "running"}:
            # ``uri=True`` on the main writable connection is required for
            # SQLite to honor immutable mode on the attached base.
            connection = sqlite3.connect(
                f"file:{partial.as_posix()}?mode=rw", uri=True
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            attached = False
            try:
                connection.execute(
                    "ATTACH DATABASE ? AS base_e1",
                    (_immutable_sqlite_uri(base_path),),
                )
                attached = True
                applied = _advance_merge_batches(
                    connection,
                    run_id=new_run_id,
                    base_run_id=base_run_id,
                    recovery_paths=recovery_paths,
                    recovery_shas=recovery_shas,
                    batch_size=batch_size,
                    checkpoint_hook=_checkpoint_hook,
                )
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise
            finally:
                if attached and not connection.in_transaction:
                    connection.execute("DETACH DATABASE base_e1")
                connection.close()
            _finish_run(
                partial,
                new_run_id,
                source_path,
                source_sha,
                inventory_factory=inventory_factory,
            )
            _checkpoint(_checkpoint_hook, "terminal", applied)

        result = validated_result(partial, applied)
        _assert_inputs_unchanged(immutable_inputs, label="pre-publish")
        _assert_snapshot(partial)
        _publish_hardlink(partial, output_path)
        return RecoveryMergeResult(
            output_path=output_path,
            source=result.source,
            base_sidecar_sha256=result.base_sidecar_sha256,
            recovery_sidecar_sha256s=result.recovery_sidecar_sha256s,
            output_sha256=_sha256_file(output_path),
            base_success=result.base_success,
            base_failed=result.base_failed,
            recovered=result.recovered,
            final_success=result.final_success,
            final_failed=result.final_failed,
            run_status=result.run_status,
            manifest_sha256=result.manifest_sha256,
        )
    finally:
        _release_lock(lock_path, descriptor)
