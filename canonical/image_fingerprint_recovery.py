"""Failure-only recovery planning and execution for terminal E1 sidecars.

The parent E1 sidecar and curated source database are immutable inputs.  This
module independently validates the parent, reconciles every failed identity
against the current source adapter, selects a deterministic recovery subset,
and delegates fetching to the existing shared E1 pipeline.  Recovery output is
always a new sidecar plus a no-clobber companion manifest.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import os
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping
from urllib.parse import urlsplit

from canonical.image_fingerprint_adapters import (
    InventoryDecision,
    SourceAsset,
    SourceAssetExclusion,
    iter_architizer_source_inventory,
    iter_divisare_source_inventory,
    source_asset_record_json,
    source_record_sha256,
)
from canonical.image_fingerprint_pipeline import (
    DEFAULT_CIRCUIT_BREAKER_THRESHOLD,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_RESPONSE_BYTES,
    DEFAULT_PENDING_BATCH_SIZE,
    DEFAULT_READ_TIMEOUT,
    DEFAULT_REQUESTS_PER_SECOND,
    DEFAULT_WORKERS,
    Fetcher,
    FetcherFactory,
    PipelineResult,
    run_image_fingerprint_pipeline,
)
from canonical.image_fingerprint_sidecar import open_sidecar
from canonical.image_fingerprint_validator import (
    InventoryFactory,
    validate_image_fingerprint_sidecar,
)


RECOVERY_POLICY_VERSION = "archibe-e1-failure-recovery-v2"
RECOVERY_MANIFEST_VERSION = "archibe-e1-failure-recovery-manifest-v1"
DEFAULT_RECOVERY_SEED = "archibe-e1-failure-recovery-v1"
PER_ERROR_N10 = "per_error_n10"
ALL_NON404_PLUS_404_SAMPLE = "all_non404_plus_404_sample"
RECOVERY_STRATEGIES = frozenset({PER_ERROR_N10, ALL_NON404_PLUS_404_SAMPLE})
PER_ERROR_SAMPLE_SIZE = 10
DEFAULT_HTTP_404_SAMPLE_SIZE = 100
RECOVERY_LINEAGE_REQUIRED_FIELDS = frozenset(
    {
        "kind",
        "base_run_id",
        "base_selection_manifest_sha256",
        "base_sidecar_path",
        "base_sidecar_sha256",
        "base_source_db_sha256_before",
        "base_source_db_sha256_after",
        "base_fingerprint_contract_version",
        "base_selection_version",
        "base_dependency_manifest_sha256",
        "http_404_sample_size",
        "ordered_recovery_manifest_sha256",
        "recovery_policy_version",
        "recovery_seed",
        "recovery_selection_count",
        "recovery_strategy",
    }
)
RECOVERY_LINEAGE_ADDITIVE_FIELDS = frozenset(
    {
        "base_source_db_sha256_before",
        "base_source_db_sha256_after",
        "base_fingerprint_contract_version",
        "base_selection_version",
        "base_dependency_manifest_sha256",
    }
)
RECOVERY_LINEAGE_CORE_FIELDS = (
    RECOVERY_LINEAGE_REQUIRED_FIELDS - RECOVERY_LINEAGE_ADDITIVE_FIELDS
)


class RecoveryError(RuntimeError):
    """Raised when immutable lineage or recovery selection is invalid."""


@dataclass(frozen=True)
class FailedSourceAsset:
    asset: SourceAsset
    error_kind: str
    source_record_sha256: str
    selection_score_sha256: str


@dataclass(frozen=True)
class RecoverySelection:
    strategy: str
    seed: str
    base_failed_count: int
    selected: tuple[FailedSourceAsset, ...]
    base_error_counts: dict[str, int]
    selected_error_counts: dict[str, int]
    ordered_manifest_sha256: str


@dataclass(frozen=True)
class RecoveryRunResult:
    output_path: Path
    manifest_path: Path
    manifest_sha256: str
    payload_sha256: str
    base_failed_count: int
    selected_assets: int
    selection_manifest_sha256: str
    run_status: str
    status_counts: dict[str, int]
    network_requests: int
    resumed: bool
    already_complete: bool


@dataclass(frozen=True)
class _BaseRun:
    run_id: str
    source_name: str
    status: str
    selection_manifest_sha256: str
    source_db_sha256: str
    source_db_sha256_after: str
    fingerprint_contract_version: str
    selection_version: str
    dependency_manifest_json: str
    dependency_manifest_sha256: str


@dataclass(frozen=True)
class _BaseFailure:
    source_asset_id: str
    error_kind: str
    source_record_sha256: str


PipelineRunner = Callable[..., PipelineResult]


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def _sha256_file(path: Path, *, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_no_sqlite_sidecars(path: Path, label: str) -> None:
    active = [
        Path(str(path) + suffix)
        for suffix in ("-wal", "-shm", "-journal")
        if Path(str(path) + suffix).exists()
    ]
    if active:
        raise RecoveryError(
            f"{label} has active SQLite sidecars: "
            + ", ".join(item.name for item in active)
        )


def _default_inventory_factory(source: str, source_db: Path) -> InventoryFactory:
    if source == "divisare":
        return lambda: iter_divisare_source_inventory(source_db)
    if source == "architizer":
        return lambda: iter_architizer_source_inventory(source_db)
    raise ValueError("source must be 'divisare' or 'architizer'")


def _load_base_run_and_failures(
    base_sidecar: Path,
    source: str,
) -> tuple[_BaseRun, tuple[_BaseFailure, ...]]:
    connection = open_sidecar(base_sidecar, readonly=True)
    try:
        runs = connection.execute("SELECT * FROM fingerprint_runs").fetchall()
        if len(runs) != 1:
            raise RecoveryError("base sidecar must contain exactly one E1 run")
        row = runs[0]
        if str(row["source_name"]) != source:
            raise RecoveryError(
                f"base source mismatch: expected {source!r}, got {row['source_name']!r}"
            )
        status = str(row["status"])
        if status not in {"complete", "complete_with_failures"}:
            raise RecoveryError(f"base E1 run is not terminal-success: {status!r}")
        if str(row["selection_mode"]) != "full":
            raise RecoveryError("failure recovery requires a full-inventory base run")
        before = str(row["source_db_sha256_before"])
        after = str(row["source_db_sha256_after"])
        if before != after:
            raise RecoveryError("base run source SHA changed during its execution")
        failures = tuple(
            _BaseFailure(
                source_asset_id=str(item["source_asset_id"]),
                error_kind=str(item["error_kind"]),
                source_record_sha256=str(item["source_record_sha256"]),
            )
            for item in connection.execute(
                """
                SELECT f.source_asset_id,f.error_kind,s.source_record_sha256
                FROM fingerprints AS f
                JOIN source_assets AS s USING(run_id,source_asset_id)
                WHERE f.run_id=? AND f.status='failed'
                ORDER BY f.source_asset_id
                """,
                (row["run_id"],),
            )
        )
        if any(not item.error_kind or item.error_kind == "None" for item in failures):
            raise RecoveryError("base failed fingerprint has no error_kind")
        return (
            _BaseRun(
                run_id=str(row["run_id"]),
                source_name=source,
                status=status,
                selection_manifest_sha256=str(row["selection_manifest_sha256"]),
                source_db_sha256=before,
                source_db_sha256_after=after,
                fingerprint_contract_version=str(
                    row["fingerprint_contract_version"]
                ),
                selection_version=str(row["selection_version"]),
                dependency_manifest_json=str(row["dependency_manifest_json"]),
                dependency_manifest_sha256=str(row["dependency_manifest_sha256"]),
            ),
            failures,
        )
    finally:
        connection.close()


def _selection_score(
    *,
    seed: str,
    failure: _BaseFailure,
) -> str:
    framed = "\0".join(
        (
            RECOVERY_POLICY_VERSION,
            seed,
            failure.error_kind,
            failure.source_asset_id,
            failure.source_record_sha256,
        )
    )
    return hashlib.sha256(framed.encode("utf-8")).hexdigest()


def _reconcile_failed_assets(
    failures: tuple[_BaseFailure, ...],
    inventory_factory: InventoryFactory,
    source: str,
    seed: str,
) -> tuple[FailedSourceAsset, ...]:
    expected = {failure.source_asset_id: failure for failure in failures}
    if len(expected) != len(failures):
        raise RecoveryError("base failed source_asset_id values are not unique")
    found: dict[str, FailedSourceAsset] = {}
    previous_id: str | None = None
    for decision in inventory_factory():
        if not isinstance(decision, (SourceAsset, SourceAssetExclusion)):
            raise RecoveryError(
                f"current adapter yielded unsupported decision: {type(decision)!r}"
            )
        if decision.source != source:
            raise RecoveryError(
                f"current adapter source mismatch: {decision.source!r}"
            )
        current_id = decision.source_asset_id
        if previous_id is not None and current_id <= previous_id:
            raise RecoveryError(
                "current adapter source_asset_id values must be strictly increasing"
            )
        previous_id = current_id
        failure = expected.get(current_id)
        if failure is None:
            continue
        if isinstance(decision, SourceAssetExclusion):
            raise RecoveryError(
                f"base failed asset is now excluded by the adapter: {current_id}"
            )
        current_sha = source_record_sha256(source_asset_record_json(decision))
        if current_sha != failure.source_record_sha256:
            raise RecoveryError(
                f"source-record SHA mismatch for failed asset: {current_id}"
            )
        found[current_id] = FailedSourceAsset(
            asset=decision,
            error_kind=failure.error_kind,
            source_record_sha256=current_sha,
            selection_score_sha256=_selection_score(seed=seed, failure=failure),
        )
    missing = sorted(set(expected) - set(found))
    if missing:
        preview = ", ".join(missing[:10])
        suffix = " ..." if len(missing) > 10 else ""
        raise RecoveryError(
            f"current adapter did not yield {len(missing)} failed identities: "
            f"{preview}{suffix}"
        )
    return tuple(found[asset_id] for asset_id in sorted(found))


def _ordered_manifest_digest(selected: Iterable[FailedSourceAsset]) -> str:
    digest = hashlib.sha256()
    for rank, item in enumerate(selected, 1):
        digest.update(
            _canonical_bytes(
                {
                    "error_kind": item.error_kind,
                    "rank": rank,
                    "selection_score_sha256": item.selection_score_sha256,
                    "source_asset_id": item.asset.source_asset_id,
                    "source_record_sha256": item.source_record_sha256,
                }
            )
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _recovery_rank(item: FailedSourceAsset) -> tuple[int, str, str]:
    """Prefer the nine modern Divisare 404s before legacy project_images.

    The much larger legacy 404 population is then sampled by the same fixed
    score.  Other sources and error kinds retain pure score ordering.
    """

    modern_404_priority = 1
    if item.error_kind == "http_404" and item.asset.source == "divisare":
        path = urlsplit(item.asset.effective_fetch_url).path.casefold()
        modern_404_priority = int("/v1/project_images/" in path)
    return (
        modern_404_priority,
        item.selection_score_sha256,
        item.asset.source_asset_id,
    )


def select_failed_assets(
    reconciled: tuple[FailedSourceAsset, ...],
    *,
    strategy: str,
    seed: str,
    http_404_sample_size: int = DEFAULT_HTTP_404_SAMPLE_SIZE,
) -> RecoverySelection:
    """Return one deterministic failure-only selection."""

    if strategy not in RECOVERY_STRATEGIES:
        raise ValueError(f"unsupported recovery strategy: {strategy!r}")
    if not seed.strip():
        raise ValueError("recovery seed must be non-empty")
    if http_404_sample_size < 1:
        raise ValueError("http_404_sample_size must be positive")
    base_counts = Counter(item.error_kind for item in reconciled)
    chosen: list[FailedSourceAsset] = []
    if strategy == PER_ERROR_N10:
        for error_kind in sorted(base_counts):
            candidates = (item for item in reconciled if item.error_kind == error_kind)
            chosen.extend(
                heapq.nsmallest(
                    PER_ERROR_SAMPLE_SIZE,
                    candidates,
                    key=_recovery_rank,
                )
            )
    else:
        chosen.extend(item for item in reconciled if item.error_kind != "http_404")
        chosen.extend(
            heapq.nsmallest(
                http_404_sample_size,
                (item for item in reconciled if item.error_kind == "http_404"),
                key=_recovery_rank,
            )
        )
    ordered = tuple(sorted(chosen, key=lambda item: item.asset.source_asset_id))
    selected_counts = Counter(item.error_kind for item in ordered)
    return RecoverySelection(
        strategy=strategy,
        seed=seed,
        base_failed_count=len(reconciled),
        selected=ordered,
        base_error_counts=dict(sorted(base_counts.items())),
        selected_error_counts=dict(sorted(selected_counts.items())),
        ordered_manifest_sha256=_ordered_manifest_digest(ordered),
    )


def _lineage_payload(
    *,
    base_sidecar: Path,
    base_sidecar_sha256: str,
    base_run: _BaseRun,
    selection: RecoverySelection,
    http_404_sample_size: int,
) -> dict[str, object]:
    return {
        "kind": "failure_recovery_v1",
        "base_run_id": base_run.run_id,
        "base_selection_manifest_sha256": base_run.selection_manifest_sha256,
        "base_source_db_sha256_before": base_run.source_db_sha256,
        "base_source_db_sha256_after": base_run.source_db_sha256_after,
        "base_fingerprint_contract_version": (
            base_run.fingerprint_contract_version
        ),
        "base_selection_version": base_run.selection_version,
        "base_dependency_manifest_sha256": (
            base_run.dependency_manifest_sha256
        ),
        "base_sidecar_path": str(base_sidecar),
        "base_sidecar_sha256": base_sidecar_sha256,
        "http_404_sample_size": (
            http_404_sample_size
            if selection.strategy == ALL_NON404_PLUS_404_SAMPLE
            else None
        ),
        "ordered_recovery_manifest_sha256": selection.ordered_manifest_sha256,
        "recovery_policy_version": RECOVERY_POLICY_VERSION,
        "recovery_seed": selection.seed,
        "recovery_selection_count": len(selection.selected),
        "recovery_strategy": selection.strategy,
    }


def _dependency_document(
    run: Mapping[str, object], *, label: str
) -> tuple[dict[str, object], dict[str, object] | None, str]:
    """Return a canonical dependency document, lineage, and stripped JSON.

    Stored dependency bytes are part of the E1 provenance contract.  Merely
    parsing equivalent JSON is insufficient: both the stored SHA and the
    canonical encoding must match before lineage is trusted.
    """

    raw = str(run["dependency_manifest_json"])
    stored_sha = str(run["dependency_manifest_sha256"])
    if hashlib.sha256(raw.encode("ascii")).hexdigest() != stored_sha:
        raise RecoveryError(f"{label} dependency manifest SHA mismatch")
    try:
        document = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RecoveryError(f"{label} dependency manifest is invalid") from exc
    if not isinstance(document, dict) or _canonical_bytes(document).decode("ascii") != raw:
        raise RecoveryError(f"{label} dependency manifest is not canonical")
    lineage = document.pop("_run_lineage", None)
    if lineage is not None and not isinstance(lineage, dict):
        raise RecoveryError(f"{label} dependency lineage is invalid")
    return document, lineage, _canonical_bytes(document).decode("ascii")


def _asset_from_stored_provenance(payload: str) -> SourceAsset:
    try:
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
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RecoveryError("recovery source provenance is invalid") from exc


def validate_failure_recovery_sidecar(
    recovery_sidecar: Path | str,
    source_db: Path | str,
    base_sidecar: Path | str,
    *,
    inventory_factory: InventoryFactory | None = None,
    allow_legacy_lineage_upgrade: bool = False,
) -> tuple[SourceAsset, ...]:
    """Independently validate recovery lineage and deterministic selection.

    This deliberately starts from the immutable base failures and the current
    source inventory.  It does not accept the recovery child's own selected
    rows as the authority for what was eligible or selected.
    """

    recovery_path = Path(recovery_sidecar).resolve()
    source_path = Path(source_db).resolve()
    base_path = Path(base_sidecar).resolve()
    for path, label in (
        (recovery_path, "recovery sidecar"),
        (source_path, "source DB"),
        (base_path, "base sidecar"),
    ):
        if not path.is_file():
            raise FileNotFoundError(path)
        _assert_no_sqlite_sidecars(path, label)

    initial_hashes = {
        recovery_path: _sha256_file(recovery_path),
        source_path: _sha256_file(source_path),
        base_path: _sha256_file(base_path),
    }
    base_connection = open_sidecar(base_path, readonly=True)
    recovery_connection = open_sidecar(recovery_path, readonly=True)
    try:
        base_rows = base_connection.execute(
            "SELECT * FROM fingerprint_runs"
        ).fetchall()
        recovery_rows = recovery_connection.execute(
            "SELECT * FROM fingerprint_runs"
        ).fetchall()
        if len(base_rows) != 1 or len(recovery_rows) != 1:
            raise RecoveryError("base and recovery must each contain one run")
        base_row = dict(base_rows[0])
        recovery_row = dict(recovery_rows[0])
    finally:
        base_connection.close()
        recovery_connection.close()

    source = str(base_row["source_name"])
    factory = inventory_factory or _default_inventory_factory(source, source_path)
    base_validation = validate_image_fingerprint_sidecar(
        base_path,
        source_path,
        inventory_factory=(factory if inventory_factory is not None else None),
    )
    if not base_validation.passed:
        raise RecoveryError("base sidecar failed independent validation")
    base_run, failures = _load_base_run_and_failures(base_path, source)
    source_sha = initial_hashes[source_path]
    if source_sha != base_run.source_db_sha256:
        raise RecoveryError("source DB SHA does not match base lineage")

    base_dependencies, base_lineage, stripped_base_json = _dependency_document(
        base_row, label="base"
    )
    if base_lineage is not None:
        raise RecoveryError("recovery base must be an ordinary full E1 run")
    recovery_dependencies, lineage, stripped_recovery_json = _dependency_document(
        recovery_row, label="recovery"
    )
    del base_dependencies, recovery_dependencies
    if stripped_recovery_json != stripped_base_json:
        raise RecoveryError("recovery/base stripped dependency mismatch")
    if not isinstance(lineage, dict):
        raise RecoveryError("recovery sidecar has no lineage")
    missing_fields = RECOVERY_LINEAGE_REQUIRED_FIELDS - set(lineage)
    missing_core = sorted(missing_fields & RECOVERY_LINEAGE_CORE_FIELDS)
    missing_additive = sorted(missing_fields & RECOVERY_LINEAGE_ADDITIVE_FIELDS)
    if missing_core:
        raise RecoveryError(
            "recovery lineage is missing required core fields: "
            + ", ".join(missing_core)
        )
    if missing_additive and not allow_legacy_lineage_upgrade:
        raise RecoveryError(
            "recovery lineage is missing required fields: "
            + ", ".join(missing_additive)
        )

    expected_lineage = {
        "kind": "failure_recovery_v1",
        "base_run_id": base_run.run_id,
        "base_selection_manifest_sha256": base_run.selection_manifest_sha256,
        "base_sidecar_path": str(base_path),
        "base_sidecar_sha256": initial_hashes[base_path],
        "base_source_db_sha256_before": base_run.source_db_sha256,
        "base_source_db_sha256_after": base_run.source_db_sha256_after,
        "base_fingerprint_contract_version": base_run.fingerprint_contract_version,
        "base_selection_version": base_run.selection_version,
        "base_dependency_manifest_sha256": base_run.dependency_manifest_sha256,
        "recovery_policy_version": RECOVERY_POLICY_VERSION,
    }
    for field, expected in expected_lineage.items():
        if field in missing_fields:
            # Legacy upgrade mode derives these additive values from the
            # independently validated base/source runs below.  Core lineage
            # is never inferred.
            continue
        if lineage.get(field) != expected:
            raise RecoveryError(f"recovery lineage {field} mismatch")
    if str(recovery_row["source_name"]) != source:
        raise RecoveryError("recovery source name mismatch")
    if not (
        str(recovery_row["source_db_sha256_before"])
        == str(recovery_row["source_db_sha256_after"])
        == source_sha
    ):
        raise RecoveryError("recovery source before/after SHA mismatch")
    if str(recovery_row["fingerprint_contract_version"]) != base_run.fingerprint_contract_version:
        raise RecoveryError("recovery fingerprint contract mismatch")
    if str(recovery_row["selection_version"]) != base_run.selection_version:
        raise RecoveryError("recovery selection version mismatch")

    strategy = lineage.get("recovery_strategy")
    seed = lineage.get("recovery_seed")
    if strategy not in RECOVERY_STRATEGIES or not isinstance(seed, str) or not seed:
        raise RecoveryError("recovery lineage strategy/seed is invalid")
    sample_size_value = lineage.get("http_404_sample_size")
    if strategy == ALL_NON404_PLUS_404_SAMPLE:
        if not isinstance(sample_size_value, int) or sample_size_value < 1:
            raise RecoveryError("recovery lineage HTTP 404 sample size is invalid")
        sample_size = sample_size_value
    else:
        if sample_size_value is not None:
            raise RecoveryError("per-error recovery must not carry a 404 sample size")
        sample_size = DEFAULT_HTTP_404_SAMPLE_SIZE

    reconciled = _reconcile_failed_assets(failures, factory, source, seed)
    selection = select_failed_assets(
        reconciled,
        strategy=str(strategy),
        seed=seed,
        http_404_sample_size=sample_size,
    )
    expected_assets = tuple(item.asset for item in selection.selected)
    if lineage.get("recovery_selection_count") != len(expected_assets):
        raise RecoveryError("recovery lineage selection count mismatch")
    if lineage.get("ordered_recovery_manifest_sha256") != selection.ordered_manifest_sha256:
        raise RecoveryError("recovery lineage ordered selection manifest mismatch")

    recovery_connection = open_sidecar(recovery_path, readonly=True)
    try:
        stored = recovery_connection.execute(
            """SELECT source_asset_id,source_record_sha256,provenance_json
               FROM source_assets ORDER BY selection_rank"""
        ).fetchall()
    finally:
        recovery_connection.close()
    expected_records = [
        (
            item.asset.source_asset_id,
            item.source_record_sha256,
            item.asset,
        )
        for item in selection.selected
    ]
    if len(stored) != len(expected_records):
        raise RecoveryError("recovery selected row count mismatch")
    for row, (asset_id, record_sha, expected_asset) in zip(
        stored, expected_records, strict=True
    ):
        if str(row["source_asset_id"]) != asset_id or str(row["source_record_sha256"]) != record_sha:
            raise RecoveryError(f"recovery selected identity mismatch: {asset_id}")
        if _asset_from_stored_provenance(str(row["provenance_json"])) != expected_asset:
            raise RecoveryError(f"recovery selected provenance mismatch: {asset_id}")

    validation = validate_image_fingerprint_sidecar(
        recovery_path,
        source_path,
        inventory_factory=lambda: iter(expected_assets),
    )
    if not validation.passed:
        raise RecoveryError("recovery sidecar failed independent validation")
    for path, expected_hash in initial_hashes.items():
        _assert_no_sqlite_sidecars(path, path.name)
        if _sha256_file(path) != expected_hash:
            raise RecoveryError(f"immutable input changed during validation: {path}")
    return expected_assets


def _output_summary(output: Path) -> dict[str, object]:
    _assert_no_sqlite_sidecars(output, "recovery output")
    connection = open_sidecar(output, readonly=True)
    try:
        run = connection.execute("SELECT * FROM fingerprint_runs").fetchone()
        counts = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT status,count(*) FROM fingerprints GROUP BY status"
            )
        }
        attempt_count = int(
            connection.execute("SELECT count(*) FROM fetch_attempts").fetchone()[0]
        )
        return {
            "attempt_count": attempt_count,
            "bytes": output.stat().st_size,
            "run_id": str(run["run_id"]),
            "run_status": str(run["status"]),
            "selection_count": int(run["selection_count"]),
            "selection_manifest_sha256": str(run["selection_manifest_sha256"]),
            "sha256": _sha256_file(output),
            "source_db_sha256_after": str(run["source_db_sha256_after"]),
            "source_db_sha256_before": str(run["source_db_sha256_before"]),
            "status_counts": dict(sorted(counts.items())),
        }
    finally:
        connection.close()


def _manifest_document(payload: Mapping[str, object]) -> tuple[bytes, str, str]:
    payload_bytes = _canonical_bytes(payload)
    payload_sha = hashlib.sha256(payload_bytes).hexdigest()
    document = {
        "manifest_version": RECOVERY_MANIFEST_VERSION,
        "payload": dict(payload),
        "payload_sha256": payload_sha,
    }
    content = _canonical_bytes(document) + b"\n"
    return content, payload_sha, hashlib.sha256(content).hexdigest()


def _publish_manifest(
    path: Path,
    content: bytes,
    *,
    resume: bool,
) -> None:
    partial = Path(str(path) + ".partial")
    if path.exists():
        if not resume:
            raise FileExistsError(f"refusing to clobber recovery manifest: {path}")
        if path.read_bytes() != content:
            raise RecoveryError("existing recovery manifest does not match output")
        if partial.exists():
            if partial.read_bytes() != content:
                raise RecoveryError(
                    "partial recovery manifest does not match published manifest"
                )
            partial.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if partial.exists():
        if not resume:
            raise FileExistsError(
                f"partial recovery manifest exists; use --resume: {partial}"
            )
        if partial.read_bytes() != content:
            raise RecoveryError("partial recovery manifest does not match output")
    else:
        with partial.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    try:
        os.link(partial, path)
    except FileExistsError:
        if not resume or path.read_bytes() != content:
            raise
    except OSError as exc:
        raise RecoveryError(
            "atomic hard-link publication of recovery manifest failed; "
            "partial was preserved"
        ) from exc
    if partial.exists():
        partial.unlink()


def run_failure_recovery(
    *,
    source: str,
    source_db: Path | str,
    base_sidecar: Path | str,
    output: Path | str,
    strategy: str,
    manifest: Path | str | None = None,
    resume: bool = False,
    recovery_seed: str = DEFAULT_RECOVERY_SEED,
    http_404_sample_size: int = DEFAULT_HTTP_404_SAMPLE_SIZE,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    read_timeout: float = DEFAULT_READ_TIMEOUT,
    workers: int = DEFAULT_WORKERS,
    requests_per_second: float = DEFAULT_REQUESTS_PER_SECOND,
    circuit_breaker_threshold: int = DEFAULT_CIRCUIT_BREAKER_THRESHOLD,
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
    batch_size: int = DEFAULT_PENDING_BATCH_SIZE,
    inventory_factory: InventoryFactory | None = None,
    fetcher: Fetcher | None = None,
    fetcher_factory: FetcherFactory | None = None,
    pipeline_runner: PipelineRunner = run_image_fingerprint_pipeline,
) -> RecoveryRunResult:
    """Validate, select, and execute one immutable failure-recovery child run."""

    if source not in {"divisare", "architizer"}:
        raise ValueError("source must be 'divisare' or 'architizer'")
    source_path = Path(source_db).resolve()
    base_path = Path(base_sidecar).resolve()
    output_path = Path(output).resolve()
    manifest_path = (
        Path(manifest).resolve()
        if manifest is not None
        else Path(str(output_path) + ".manifest.json")
    )
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    if not base_path.is_file():
        raise FileNotFoundError(base_path)
    if len({source_path, base_path, output_path, manifest_path}) != 4:
        raise RecoveryError("source, base, output, and manifest paths must be distinct")
    if output_path.exists() and not resume:
        raise FileExistsError(f"refusing to clobber recovery output: {output_path}")
    if manifest_path.exists() and not resume:
        raise FileExistsError(
            f"refusing to clobber recovery manifest: {manifest_path}"
        )
    manifest_partial = Path(str(manifest_path) + ".partial")
    if manifest_partial.exists() and not resume:
        raise FileExistsError(
            "partial recovery manifest exists; use --resume: "
            f"{manifest_partial}"
        )
    if manifest_path.exists() and not output_path.exists():
        raise RecoveryError("recovery manifest exists without its output sidecar")
    if manifest_partial.exists() and not output_path.exists():
        raise RecoveryError(
            "partial recovery manifest exists without its output sidecar"
        )

    _assert_no_sqlite_sidecars(source_path, "source DB")
    _assert_no_sqlite_sidecars(base_path, "base sidecar")
    base_sha_before = _sha256_file(base_path)
    active_inventory = inventory_factory or _default_inventory_factory(
        source, source_path
    )
    base_validation = validate_image_fingerprint_sidecar(
        base_path,
        source_path,
        inventory_factory=(active_inventory if inventory_factory is not None else None),
    )
    if not base_validation.passed:
        failed = [
            check.name for check in base_validation.checks if not check.passed
        ]
        raise RecoveryError(
            "base sidecar failed independent validation: " + ", ".join(failed)
        )
    _assert_no_sqlite_sidecars(base_path, "base sidecar")
    base_sha_after = _sha256_file(base_path)
    if base_sha_before != base_sha_after:
        raise RecoveryError("base sidecar changed during immutable validation")

    base_run, failures = _load_base_run_and_failures(base_path, source)
    source_sha = _sha256_file(source_path)
    if source_sha != base_run.source_db_sha256:
        raise RecoveryError("source DB SHA does not match the base E1 run")
    if not failures:
        raise RecoveryError("base E1 run has no failed fingerprints to recover")

    reconciled = _reconcile_failed_assets(
        failures,
        active_inventory,
        source,
        recovery_seed,
    )
    selection = select_failed_assets(
        reconciled,
        strategy=strategy,
        seed=recovery_seed,
        http_404_sample_size=http_404_sample_size,
    )
    if not selection.selected:
        raise RecoveryError("recovery policy selected no failed assets")
    lineage = _lineage_payload(
        base_sidecar=base_path,
        base_sidecar_sha256=base_sha_after,
        base_run=base_run,
        selection=selection,
        http_404_sample_size=http_404_sample_size,
    )
    selected_assets = tuple(item.asset for item in selection.selected)
    selected_inventory: InventoryFactory = lambda: iter(selected_assets)
    pipeline_result = pipeline_runner(
        source=source,
        source_db=source_path,
        output=output_path,
        sample_size=None,
        resume=resume,
        max_response_bytes=max_response_bytes,
        max_attempts=max_attempts,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        inventory_factory=selected_inventory,
        fetcher=fetcher,
        fetcher_factory=fetcher_factory,
        workers=workers,
        requests_per_second=requests_per_second,
        circuit_breaker_threshold=circuit_breaker_threshold,
        cooldown_seconds=cooldown_seconds,
        batch_size=batch_size,
        run_lineage=lineage,
    )

    validate_failure_recovery_sidecar(
        output_path,
        source_path,
        base_path,
        inventory_factory=(active_inventory if inventory_factory is not None else None),
    )
    _assert_no_sqlite_sidecars(source_path, "source DB")
    _assert_no_sqlite_sidecars(base_path, "base sidecar")
    if _sha256_file(source_path) != source_sha:
        raise RecoveryError("source DB changed during recovery execution")
    if _sha256_file(base_path) != base_sha_after:
        raise RecoveryError("base sidecar changed during recovery execution")
    output_summary = _output_summary(output_path)
    if int(output_summary["selection_count"]) != len(selected_assets):
        raise RecoveryError("recovery output selection count is inconsistent")
    if (
        output_summary["source_db_sha256_before"] != source_sha
        or output_summary["source_db_sha256_after"] != source_sha
    ):
        raise RecoveryError("recovery output does not preserve parent source SHA")
    payload: dict[str, object] = {
        "base": {
            "dependency_manifest_sha256": base_run.dependency_manifest_sha256,
            "failed_count": len(failures),
            "path": str(base_path),
            "run_id": base_run.run_id,
            "selection_manifest_sha256": base_run.selection_manifest_sha256,
            "sha256": base_sha_after,
            "status": base_run.status,
        },
        "lineage": lineage,
        "output": {
            "path": str(output_path),
            **output_summary,
        },
        "selection": {
            "base_error_counts": selection.base_error_counts,
            "ordered_manifest_sha256": selection.ordered_manifest_sha256,
            "records": [
                {
                    "error_kind": item.error_kind,
                    "selection_score_sha256": item.selection_score_sha256,
                    "source_asset_id": item.asset.source_asset_id,
                    "source_record_sha256": item.source_record_sha256,
                }
                for item in selection.selected
            ],
            "seed": selection.seed,
            "selected_error_counts": selection.selected_error_counts,
            "strategy": selection.strategy,
        },
        "source": {
            "bytes": source_path.stat().st_size,
            "name": source,
            "path": str(source_path),
            "sha256": source_sha,
        },
    }
    manifest_bytes, payload_sha, manifest_sha = _manifest_document(payload)
    _publish_manifest(manifest_path, manifest_bytes, resume=resume)
    return RecoveryRunResult(
        output_path=output_path,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha,
        payload_sha256=payload_sha,
        base_failed_count=len(failures),
        selected_assets=len(selected_assets),
        selection_manifest_sha256=selection.ordered_manifest_sha256,
        run_status=str(output_summary["run_status"]),
        status_counts=dict(output_summary["status_counts"]),  # type: ignore[arg-type]
        network_requests=pipeline_result.network_requests,
        resumed=pipeline_result.resumed,
        already_complete=pipeline_result.already_complete,
    )


def recovery_result_json(result: RecoveryRunResult) -> str:
    """Return stable JSON for CLI presentation."""

    payload = asdict(result)
    payload["output_path"] = str(result.output_path)
    payload["manifest_path"] = str(result.manifest_path)
    return json.dumps(payload, indent=2, sort_keys=True)
