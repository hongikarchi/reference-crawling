"""Crash-safe runtime sidecar for the approved cross-source semantic N100.

This module is intentionally separate from the frozen N10 runner.  It reuses
only the N10 runner's source-neutral storage helpers and semantic write
helpers; none of the N10 constants, manifest identities, outputs, or resume
semantics are widened in place.

The N100 population is supplied as a byte-frozen manifest plus an explicit
``RuntimeContract``.  One hundred buildings and at most six hundred occurrence
memberships are allowed.  All images are materialized and checked against the
N10 *actual Vision derivative* pixel identities before the first model call.
An identical derivative inside N100 is a fail-closed data error until a future
schema adds an explicit input/result reuse link.

Every HTTP and Vision attempt is committed before retry.  A global retry cap,
consecutive-failure circuit breakers, and long Retry-After guard leave the
``.partial`` database in ``running`` state with unprocessed rows pending.  A
later explicit ``resume`` continues from the stored attempt numbers and retry
budgets.  Terminal DB/report publication is an exclusive two-file operation.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

from canonical.cross_source_image_selection import canonical_json, canonical_sha256
from canonical.cross_source_semantic_coverage_n100 import (
    SEMANTIC_COVERAGE_N100_MANIFEST_DOMAIN,
)
from canonical.cross_source_semantic_fetch import (
    SOURCE_HOSTS,
    FetchFailure,
    FetchPayload as HttpPayload,
    fetch_once as network_fetch,
)
from canonical.cross_source_semantic_vision import (
    CONTRACT_VERSION,
    OUTPUT_SCHEMA,
    PROMPT_VERSION,
    TRANSFORM_VERSION,
    compose_prompt,
    normalize_batch,
)
from canonical.cross_source_semantic_vision_sidecar import (
    APPLICATION_ID,
    FIXED_BUILDING_COUNT as N10_BUILDING_COUNT,
    FIXED_OCCURRENCE_COUNT as N10_OCCURRENCE_COUNT,
    SIDECAR_SCHEMA as N10_SIDECAR_SCHEMA,
    _GlobalRequestRateLimiter,
    _LOGICAL_TABLES as N10_LOGICAL_TABLES,
    _acquire_lock,
    _atomic_write_bytes,
    _clear_stale_atomic_temps,
    _dependency_manifest,
    _derivative_identity,
    _fill_unobserved_slots,
    _insert_fetch_attempt,
    _publish,
    _release_lock,
    _schema_sha256,
    _sidecars,
    _write_success_results,
    _write_vision_attempt,
    file_sha256,
    open_immutable,
    recover_sqlite,
    utc_now,
    validate_terminal_sidecar as validate_n10_terminal_sidecar,
)
from canonical.divisare_vision_benchmark import decode_source, prepare_derivative
from canonical.divisare_vision_runtime import (
    CLI_IMAGE_DETAIL,
    DEFAULT_MODEL,
    DEFAULT_REASONING,
    DEFAULT_SERVICE_TIER,
    RUNTIME_SANDBOX,
    RUNTIME_VERSION,
    VisionRuntimeProvenance,
    VisionRuntimeResult,
    run_codex_vision_batch,
)
from canonical.image_fingerprint import (
    FINGERPRINT_CONTRACT_VERSION,
    fingerprint_bytes,
)


SCHEMA_VERSION = 2
RUNNER_VERSION = "cross-source-semantic-vision-n100-runner-v1.0.0"
RETRY_POLICY_VERSION = "cross-source-semantic-vision-n100-retry-v1.0.0"
LOGICAL_MANIFEST_VERSION = "cross-source-semantic-vision-n100-logical-v1"
FIXED_BUILDING_COUNT = 100
MAX_OCCURRENCE_COUNT = 600
FIXED_BATCH_SIZE = 5
MAX_PLANNED_BATCHES = 120
FIXED_FETCH_ATTEMPTS = 3
FIXED_VISION_ATTEMPTS = 2
MAX_TOTAL_FETCH_RETRIES = 30
MAX_TOTAL_VISION_RETRIES = 12
MAX_TOTAL_INPUT_TOKENS = 2_600_000
MAX_TOTAL_OUTPUT_TOKENS = 100_000
MAX_TOTAL_TOKENS = 2_700_000
HTTP_CIRCUIT_THRESHOLD = 5
VISION_CIRCUIT_THRESHOLD = 3
DEFAULT_REQUESTS_PER_SECOND = 2.0
MAX_AUTOMATIC_COOLDOWN_SECONDS = 120.0
FROZEN_N10_SIDECAR_SHA256 = (
    "30cfdce39b8ac0ecc0d1de0b52f05a5f1f5d7bec7390c03439096118e08ee31a"
)
REPORT_LOGICAL_PLACEHOLDER = "0" * 64

REQUIRED_VALIDATIONS = (
    "manifest_identity",
    "fixed_population_accounting",
    "input_files_immutable",
    "n10_exclusion",
    "distinct_derivative_inputs",
    "attempt_accounting",
    "global_retry_caps",
    "result_accounting",
    "no_pending_work",
    "materialization_cache_integrity",
    "sqlite_quick_check",
    "sqlite_integrity_check",
    "foreign_key_check",
    "report_binding",
)


class N100SemanticVisionError(RuntimeError):
    """A fail-closed N100 provenance, state, or publication error."""


class N100RunPaused(N100SemanticVisionError):
    """A resumable breaker/cost boundary; the partial sidecar remains running."""

    def __init__(self, reason: str, detail: Mapping[str, Any]) -> None:
        super().__init__(f"{reason}: {canonical_json(detail)}")
        self.reason = reason
        self.detail = dict(detail)


class N100DataContractError(N100SemanticVisionError):
    """A frozen population or actual-input identity violated the N100 contract."""


def _sha256(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError(f"{label} must be lowercase SHA-256 hex")
    return value


@dataclass(frozen=True)
class RuntimeContract:
    """Exact, pre-approved population identity supplied by the N100 CLI."""

    manifest_byte_sha256: str
    manifest_self_sha256: str
    sample_seed: str
    building_count: int
    occurrence_count: int
    ordered_building_manifest_sha256: str
    ordered_occurrence_manifest_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "manifest_byte_sha256",
            "manifest_self_sha256",
            "ordered_building_manifest_sha256",
            "ordered_occurrence_manifest_sha256",
        ):
            _sha256(str(getattr(self, name)), label=name)
        if (
            not isinstance(self.sample_seed, str)
            or not self.sample_seed
            or self.sample_seed != self.sample_seed.strip()
        ):
            raise ValueError("sample_seed must be non-empty without outer whitespace")
        if self.building_count != FIXED_BUILDING_COUNT:
            raise ValueError("N100 contract requires exactly 100 buildings")
        if not FIXED_BUILDING_COUNT <= self.occurrence_count <= MAX_OCCURRENCE_COUNT:
            raise ValueError("N100 occurrence count must be between 100 and 600")
        if self.planned_batch_count > MAX_PLANNED_BATCHES:
            raise ValueError("N100 planned Vision batches exceed the approved cap")

    @property
    def planned_batch_count(self) -> int:
        return math.ceil(self.occurrence_count / FIXED_BATCH_SIZE)

    def as_record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class N10Exclusions:
    sidecar_sha256: str
    selection_ids: frozenset[str]
    derivative_pixel_sha256s: frozenset[str]

    def __post_init__(self) -> None:
        _sha256(self.sidecar_sha256, label="N10 sidecar SHA")
        for value in self.derivative_pixel_sha256s:
            _sha256(value, label="N10 derivative pixel SHA")

    @property
    def manifest_sha256(self) -> str:
        return canonical_sha256(
            {
                "derivative_pixel_sha256s": sorted(self.derivative_pixel_sha256s),
                "selection_ids": sorted(self.selection_ids),
                "sidecar_sha256": self.sidecar_sha256,
            }
        )


@dataclass(frozen=True)
class N100RunResult:
    output_db: Path
    report_path: Path
    status: str
    logical_sha256: str
    requests_made: int
    vision_requests: int
    resumed: bool
    already_complete: bool
    metrics: Mapping[str, Any]


@dataclass
class _CircuitState:
    consecutive_http_failures: int = 0
    consecutive_vision_failures: int = 0


def _n100_schema(contract: RuntimeContract) -> str:
    """Adapt the frozen table/trigger layout without widening the N10 module."""

    schema = N10_SIDECAR_SCHEMA
    replacements = (
        ("PRAGMA user_version=1;", f"PRAGMA user_version={SCHEMA_VERSION};"),
        (
            f"CHECK(building_count={N10_BUILDING_COUNT})",
            f"CHECK(building_count={FIXED_BUILDING_COUNT})",
        ),
        (
            f"CHECK(occurrence_count={N10_OCCURRENCE_COUNT})",
            f"CHECK(occurrence_count={contract.occurrence_count})",
        ),
    )
    for old, new in replacements:
        if schema.count(old) != 1:
            raise N100SemanticVisionError(
                f"frozen N10 schema template changed unexpectedly: {old}"
            )
        schema = schema.replace(old, new, 1)
    return (
        schema
        + """

CREATE TABLE runtime_events(
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES semantic_runs(run_id),
  event_type TEXT NOT NULL CHECK(length(trim(event_type))>0),
  detail_json TEXT NOT NULL CHECK(json_valid(detail_json)),
  created_at TEXT NOT NULL
);
CREATE TRIGGER runtime_events_insert BEFORE INSERT ON runtime_events
WHEN (SELECT status FROM semantic_runs WHERE run_id=NEW.run_id)<>'running'
BEGIN SELECT RAISE(ABORT,'runtime event requires running run'); END;
CREATE TRIGGER runtime_events_no_update BEFORE UPDATE ON runtime_events
BEGIN SELECT RAISE(ABORT,'runtime events are append-only'); END;
CREATE TRIGGER runtime_events_no_delete BEFORE DELETE ON runtime_events
BEGIN SELECT RAISE(ABORT,'runtime events are append-only'); END;
"""
    )


def _load_manifest(path: Path, contract: RuntimeContract) -> tuple[dict[str, Any], bytes]:
    if not path.is_file():
        raise FileNotFoundError(path)
    raw = path.read_bytes()
    actual_byte_sha = hashlib.sha256(raw).hexdigest()
    if actual_byte_sha != contract.manifest_byte_sha256:
        raise N100DataContractError(
            "N100 manifest byte SHA mismatch: "
            f"{actual_byte_sha} != {contract.manifest_byte_sha256}"
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise N100DataContractError("N100 manifest is not UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise N100DataContractError("N100 manifest root must be an object")
    if raw != (canonical_json(payload) + "\n").encode("utf-8"):
        raise N100DataContractError("N100 manifest is not canonical JSON plus LF")
    body = dict(payload)
    stored_self = body.pop("semantic_coverage_manifest_sha256", None)
    replayed_self = canonical_sha256(
        {"domain": SEMANTIC_COVERAGE_N100_MANIFEST_DOMAIN, "manifest": body}
    )
    if stored_self != contract.manifest_self_sha256 or replayed_self != stored_self:
        raise N100DataContractError("N100 manifest self SHA mismatch")
    identities = {
        "sample_seed": (payload.get("sample_seed"), contract.sample_seed),
        "sample_size_buildings": (
            payload.get("sample_size_buildings"),
            contract.building_count,
        ),
        "planned_occurrence_count": (
            payload.get("planned_occurrence_count"),
            contract.occurrence_count,
        ),
        "planned_unique_e1_pixel_count": (
            payload.get("planned_unique_e1_pixel_count"),
            contract.occurrence_count,
        ),
        "ordered_building_manifest_sha256": (
            payload.get("ordered_building_manifest_sha256"),
            contract.ordered_building_manifest_sha256,
        ),
        "ordered_occurrence_manifest_sha256": (
            payload.get("ordered_occurrence_manifest_sha256"),
            contract.ordered_occurrence_manifest_sha256,
        ),
    }
    mismatches = {
        key: {"actual": actual, "expected": expected}
        for key, (actual, expected) in identities.items()
        if actual != expected
    }
    if mismatches:
        raise N100DataContractError(
            "N100 manifest contract mismatch: " + canonical_json(mismatches)
        )
    return payload, raw


def _manifest_rows(
    payload: Mapping[str, Any], contract: RuntimeContract
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    wrappers = payload.get("selected_buildings")
    if not isinstance(wrappers, list) or len(wrappers) != FIXED_BUILDING_COUNT:
        raise N100DataContractError("N100 manifest must contain exactly 100 buildings")
    buildings: list[dict[str, Any]] = []
    occurrences: list[dict[str, Any]] = []
    input_rank = 0
    try:
        for building_rank, wrapper in enumerate(wrappers, 1):
            selected = wrapper["selected_building"]
            building = selected["building"]
            plan = selected["coverage_plan"]
            guard_name = selected.get(
                "sampling_stratum",
                selected.get("guard_name", building["population_stratum"]),
            )
            buildings.append(
                {
                    "building_rank": building_rank,
                    "selection_id": building["selection_id"],
                    "source": building["source"],
                    "source_building_id": building["source_building_id"],
                    "population_stratum": building["population_stratum"],
                    "guard_name": guard_name,
                    "qa_fallback": int(bool(building["qa_fallback"])),
                    "building_record_sha256": building["selection_record_sha256"],
                    "coverage_plan_record_sha256": selected[
                        "coverage_plan_record_sha256"
                    ],
                    "selected_building_record_sha256": wrapper[
                        "selected_building_record_sha256"
                    ],
                    "manifest_json": canonical_json(wrapper),
                }
            )
            for occurrence_wrapper in plan["selected_occurrences"]:
                input_rank += 1
                occurrence = occurrence_wrapper["occurrence"]
                candidate = occurrence["candidate"]
                occurrences.append(
                    {
                        "input_rank": input_rank,
                        "inference_id": f"semv100_{input_rank:06d}",
                        "selection_id": building["selection_id"],
                        "occurrence_rank": occurrence["occurrence_rank"],
                        "source": candidate["source"],
                        "source_building_id": candidate["source_building_id"],
                        "source_asset_id": candidate["source_asset_id"],
                        "candidate_id": candidate["candidate_id"],
                        "fetch_url": candidate["fetch_url"],
                        "expected_response_sha256": candidate[
                            "raw_response_sha256"
                        ],
                        "expected_e1_pixel_sha256": candidate[
                            "normalized_pixel_sha256"
                        ],
                        "expected_e1_width": candidate["normalized_width"],
                        "expected_e1_height": candidate["normalized_height"],
                        "e2_asset_record_sha256": candidate[
                            "e2_asset_record_sha256"
                        ],
                        "e2_relation_record_sha256": candidate[
                            "e2_building_relation_record_sha256"
                        ],
                        "e3_candidate_record_sha256": candidate[
                            "e3_candidate_record_sha256"
                        ],
                        "e3_ranking_record_sha256": candidate[
                            "e3_ranking_record_sha256"
                        ],
                        "e3_shortlist_record_sha256": candidate.get(
                            "e3_shortlist_item_record_sha256"
                        ),
                        "occurrence_record_sha256": occurrence_wrapper[
                            "occurrence_record_sha256"
                        ],
                        "manifest_json": canonical_json(occurrence_wrapper),
                    }
                )
    except (KeyError, TypeError) as exc:
        raise N100DataContractError("N100 manifest row structure is invalid") from exc
    if len(occurrences) != contract.occurrence_count:
        raise N100DataContractError("decoded N100 occurrence count mismatch")
    if len({row["selection_id"] for row in buildings}) != FIXED_BUILDING_COUNT:
        raise N100DataContractError("N100 building identities are not unique")
    if len({row["candidate_id"] for row in occurrences}) != len(occurrences):
        raise N100DataContractError("N100 candidate identities are not unique")
    if len({row["fetch_url"] for row in occurrences}) != len(occurrences):
        raise N100DataContractError("N100 fetch URLs are not unique")
    if len({row["expected_e1_pixel_sha256"] for row in occurrences}) != len(
        occurrences
    ):
        raise N100DataContractError(
            "identical expected E1 pixels require explicit reuse links; N100 v1 fails closed"
        )
    for row in occurrences:
        if row["source"] not in SOURCE_HOSTS:
            raise N100DataContractError(f"unsupported source: {row['source']!r}")
        parsed = urlsplit(str(row["fetch_url"]))
        if (
            parsed.scheme.casefold() != "https"
            or parsed.hostname != SOURCE_HOSTS[str(row["source"])]
        ):
            raise N100DataContractError(
                f"N100 fetch URL host mismatch: {row['inference_id']}"
            )
    return buildings, occurrences


def _verify_input(path: Path, record: Mapping[str, Any]) -> str:
    if not path.is_file():
        raise FileNotFoundError(path)
    if _sidecars(path):
        raise N100SemanticVisionError(f"SQLite input sidecars exist: {_sidecars(path)}")
    if path.stat().st_size != int(record["size_bytes"]):
        raise N100SemanticVisionError(f"input size mismatch: {path}")
    actual = file_sha256(path)
    if actual != record["byte_sha256"]:
        raise N100SemanticVisionError(f"input SHA mismatch: {path}")
    return actual


def load_n10_exclusions(
    path: Path,
    *,
    expected_sha256: str = FROZEN_N10_SIDECAR_SHA256,
) -> N10Exclusions:
    """Validate and read only the completed N10 exclusion identities."""

    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    if _sidecars(path):
        raise N100SemanticVisionError(f"N10 SQLite sidecars exist: {_sidecars(path)}")
    before_size = path.stat().st_size
    before_sha = file_sha256(path)
    if before_sha != expected_sha256:
        raise N100DataContractError(
            f"N10 sidecar SHA mismatch: {before_sha} != {expected_sha256}"
        )
    validate_n10_terminal_sidecar(path)
    connection = open_immutable(path)
    try:
        selection_ids = frozenset(
            str(row[0])
            for row in connection.execute(
                "SELECT selection_id FROM selected_buildings ORDER BY selection_id"
            )
        )
        derivative_pixels = frozenset(
            str(row[0])
            for row in connection.execute(
                "SELECT derivative_pixel_sha256 FROM vision_inputs "
                "WHERE derivative_pixel_sha256 IS NOT NULL "
                "ORDER BY derivative_pixel_sha256"
            )
        )
    finally:
        connection.close()
    if len(selection_ids) != N10_BUILDING_COUNT:
        raise N100DataContractError("N10 exclusion building count mismatch")
    if not derivative_pixels:
        raise N100DataContractError("N10 has no actual derivative-pixel exclusions")
    if (
        path.stat().st_size != before_size
        or file_sha256(path) != before_sha
        or _sidecars(path)
    ):
        raise N100SemanticVisionError("N10 sidecar changed during exclusion read")
    return N10Exclusions(before_sha, selection_ids, derivative_pixels)


def _contract_provenance(
    contract: RuntimeContract,
    exclusions: N10Exclusions,
    cache_dir: Path,
    *,
    retained_for_review: bool,
) -> dict[str, Any]:
    dependency = _dependency_manifest()
    dependency.update(
        {
            "n100_runtime_contract": contract.as_record(),
            "n10_exclusion_manifest_sha256": exclusions.manifest_sha256,
            "n10_sidecar_sha256": exclusions.sidecar_sha256,
            "retry_and_breaker_limits": {
                "http_circuit_threshold": HTTP_CIRCUIT_THRESHOLD,
                "max_total_input_tokens": MAX_TOTAL_INPUT_TOKENS,
                "max_total_output_tokens": MAX_TOTAL_OUTPUT_TOKENS,
                "max_total_tokens": MAX_TOTAL_TOKENS,
                "max_total_fetch_retries": MAX_TOTAL_FETCH_RETRIES,
                "max_total_vision_retries": MAX_TOTAL_VISION_RETRIES,
                "vision_circuit_threshold": VISION_CIRCUIT_THRESHOLD,
            },
            "materialization_cache": {
                "path": str(cache_dir.resolve()),
                "retained_for_review": retained_for_review,
                "write_contract": "atomic-exclusive-sha256-v1",
            },
        }
    )
    return dependency


def initialize_n100_sidecar(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    manifest_path: Path,
    manifest_raw: bytes,
    payload: Mapping[str, Any],
    contract: RuntimeContract,
    exclusions: N10Exclusions,
    e2_path: Path,
    e3_path: Path,
    model: str,
    reasoning: str,
    service_tier: str,
    cli_version: str | None,
    cache_dir: Path,
    retained_for_review: bool,
) -> None:
    buildings, occurrences = _manifest_rows(payload, contract)
    overlap = sorted(
        {row["selection_id"] for row in buildings} & exclusions.selection_ids
    )
    if overlap:
        raise N100DataContractError(
            "N100 building population overlaps N10: " + canonical_json(overlap)
        )
    dependency = _contract_provenance(
        contract,
        exclusions,
        cache_dir,
        retained_for_review=retained_for_review,
    )
    connection.executescript(_n100_schema(contract))
    run_values = {
        "run_id": run_id,
        "status": "initializing",
        "runner_version": RUNNER_VERSION,
        "schema_version": SCHEMA_VERSION,
        "manifest_path": str(manifest_path),
        "manifest_size": len(manifest_raw),
        "manifest_byte_sha256": contract.manifest_byte_sha256,
        "manifest_self_sha256": contract.manifest_self_sha256,
        "ordered_building_manifest_sha256": contract.ordered_building_manifest_sha256,
        "ordered_occurrence_manifest_sha256": contract.ordered_occurrence_manifest_sha256,
        "sample_seed": contract.sample_seed,
        "building_count": contract.building_count,
        "occurrence_count": contract.occurrence_count,
        "e2_path": str(e2_path),
        "e2_size": int(payload["e2_input"]["size_bytes"]),
        "e2_sha256_before": payload["e2_input"]["byte_sha256"],
        "e2_logical_sha256": payload["e2_input"]["logical_sha256"],
        "e2_run_id": payload["e2_input"]["run_id"],
        "e3_path": str(e3_path),
        "e3_size": int(payload["e3_input"]["size_bytes"]),
        "e3_sha256_before": payload["e3_input"]["byte_sha256"],
        "e3_logical_sha256": payload["e3_input"]["logical_sha256"],
        "e3_run_id": payload["e3_input"]["run_id"],
        "contract_version": CONTRACT_VERSION,
        "prompt_version": PROMPT_VERSION,
        "output_schema_sha256": _schema_sha256(),
        "transform_version": TRANSFORM_VERSION,
        "e1_contract_version": FINGERPRINT_CONTRACT_VERSION,
        "dependency_manifest_json": canonical_json(dependency),
        "dependency_manifest_sha256": canonical_sha256(dependency),
        "runtime_version": RUNTIME_VERSION,
        "retry_policy_version": RETRY_POLICY_VERSION,
        "batch_size": FIXED_BATCH_SIZE,
        "max_fetch_attempts": FIXED_FETCH_ATTEMPTS,
        "max_vision_attempts": FIXED_VISION_ATTEMPTS,
        "model": model,
        "reasoning": reasoning,
        "service_tier": service_tier,
        "image_detail": CLI_IMAGE_DETAIL,
        "cli_version": cli_version,
        "started_at": utc_now(),
        "metrics_json": canonical_json({"runtime_provenance": dependency}),
    }
    columns = tuple(run_values)
    connection.execute(
        f"INSERT INTO semantic_runs({','.join(columns)}) "
        f"VALUES({','.join('?' for _ in columns)})",
        tuple(run_values[column] for column in columns),
    )
    connection.executemany(
        "INSERT INTO selected_buildings VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                run_id,
                row["building_rank"],
                row["selection_id"],
                row["source"],
                row["source_building_id"],
                row["population_stratum"],
                row["guard_name"],
                row["qa_fallback"],
                row["building_record_sha256"],
                row["coverage_plan_record_sha256"],
                row["selected_building_record_sha256"],
                row["manifest_json"],
            )
            for row in buildings
        ],
    )
    connection.executemany(
        "INSERT INTO selected_occurrences VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                run_id,
                row["input_rank"],
                row["inference_id"],
                row["selection_id"],
                row["occurrence_rank"],
                row["source"],
                row["source_building_id"],
                row["source_asset_id"],
                row["candidate_id"],
                row["fetch_url"],
                row["expected_response_sha256"],
                row["expected_e1_pixel_sha256"],
                row["expected_e1_width"],
                row["expected_e1_height"],
                row["e2_asset_record_sha256"],
                row["e2_relation_record_sha256"],
                row["e3_candidate_record_sha256"],
                row["e3_ranking_record_sha256"],
                row["e3_shortlist_record_sha256"],
                row["occurrence_record_sha256"],
                row["manifest_json"],
            )
            for row in occurrences
        ],
    )
    connection.executemany(
        "INSERT INTO vision_inputs(run_id,inference_id,status) VALUES(?,?,'pending')",
        [(run_id, row["inference_id"]) for row in occurrences],
    )
    connection.execute(
        "UPDATE semantic_runs SET status='running' WHERE run_id=?", (run_id,)
    )
    connection.commit()


def _record_event(
    connection: sqlite3.Connection,
    run_id: str,
    event_type: str,
    detail: Mapping[str, Any],
) -> None:
    connection.execute(
        "INSERT INTO runtime_events(run_id,event_type,detail_json,created_at) "
        "VALUES(?,?,?,?)",
        (run_id, event_type, canonical_json(dict(detail)), utc_now()),
    )


def _pause(
    connection: sqlite3.Connection,
    run_id: str,
    reason: str,
    detail: Mapping[str, Any],
) -> None:
    _record_event(connection, run_id, reason, detail)
    connection.commit()
    raise N100RunPaused(reason, detail)


def _retry_count(connection: sqlite3.Connection, table: str, run_id: str) -> int:
    if table not in {"fetch_attempts", "vision_attempts"}:
        raise ValueError("unsupported retry table")
    return int(
        connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE run_id=? AND attempt_no>1",
            (run_id,),
        ).fetchone()[0]
    )


def _token_totals(connection: sqlite3.Connection, run_id: str) -> tuple[int, int]:
    row = connection.execute(
        "SELECT COALESCE(SUM(input_tokens),0),COALESCE(SUM(output_tokens),0) "
        "FROM vision_attempts WHERE run_id=?",
        (run_id,),
    ).fetchone()
    return int(row[0]), int(row[1])


def _enforce_model_envelope(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    contract: RuntimeContract,
    batch_no: int,
    after_attempt: bool,
) -> None:
    calls = int(
        connection.execute(
            "SELECT COUNT(*) FROM vision_attempts WHERE run_id=?", (run_id,)
        ).fetchone()[0]
    )
    input_tokens, output_tokens = _token_totals(connection, run_id)
    call_cap = contract.planned_batch_count + MAX_TOTAL_VISION_RETRIES
    exceeded = (
        calls > call_cap
        or input_tokens > MAX_TOTAL_INPUT_TOKENS
        or output_tokens > MAX_TOTAL_OUTPUT_TOKENS
        or input_tokens + output_tokens > MAX_TOTAL_TOKENS
    )
    exhausted = (
        calls >= call_cap
        or input_tokens >= MAX_TOTAL_INPUT_TOKENS
        or output_tokens >= MAX_TOTAL_OUTPUT_TOKENS
        or input_tokens + output_tokens >= MAX_TOTAL_TOKENS
    )
    if exceeded or (exhausted and not after_attempt):
        _pause(
            connection,
            run_id,
            "model_cost_envelope",
            {
                "after_attempt": after_attempt,
                "batch_no": batch_no,
                "call_cap": call_cap,
                "calls": calls,
                "input_token_cap": MAX_TOTAL_INPUT_TOKENS,
                "input_tokens": input_tokens,
                "output_token_cap": MAX_TOTAL_OUTPUT_TOKENS,
                "output_tokens": output_tokens,
                "total_token_cap": MAX_TOTAL_TOKENS,
                "total_tokens": input_tokens + output_tokens,
            },
        )


def _executor_exception_result(
    *,
    exc: Exception,
    prompt: str,
    image_paths: Sequence[Path],
    output_schema_path: Path,
    inference_ids: Sequence[str],
    codex_bin: Path,
    model: str,
    reasoning: str,
    service_tier: str,
    working_directory: Path,
    elapsed_seconds: float,
) -> VisionRuntimeResult:
    """Convert an unexpected executor exception into one durable failed attempt.

    ``run_codex_vision_batch`` normally returns typed failures, but its input
    validation or an injected/alternate executor can still raise.  Treat the
    invocation conservatively as a model attempt so retry, circuit, and call
    budgets cannot be bypassed by an exception path.
    """

    error_kind = type(exc).__name__
    error_message = str(exc)
    return VisionRuntimeResult(
        status="exec_error",
        records=(),
        final_assistant_text=None,
        usage=None,
        stdout="",
        stderr=f"{error_kind}: {error_message}",
        raw_events=(),
        non_json_stdout_lines=(),
        elapsed_seconds=max(0.0, elapsed_seconds),
        returncode=None,
        error_kind=error_kind,
        error_message=error_message,
        provenance=VisionRuntimeProvenance(
            runtime_version=RUNTIME_VERSION,
            codex_bin=str(codex_bin),
            model=model,
            reasoning=reasoning,
            service_tier=service_tier,
            cli_image_detail=CLI_IMAGE_DETAIL,
            sandbox=RUNTIME_SANDBOX,
            working_directory=str(working_directory),
            output_schema_path=str(output_schema_path),
            output_schema_sha256=hashlib.sha256(
                output_schema_path.read_bytes()
            ).hexdigest(),
            prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            image_paths=tuple(str(path) for path in image_paths),
            expected_asset_ids=tuple(inference_ids),
            timeout_seconds=600.0,
            command_without_prompt=("<executor raised before result>",),
        ),
    )


def _load_cached(cache_dir: Path, row: sqlite3.Row) -> bytes:
    path = cache_dir / f"{row['inference_id']}.jpg"
    if not path.is_file():
        raise N100DataContractError(
            f"ready input is missing durable derivative: {row['inference_id']}"
        )
    raw = path.read_bytes()
    try:
        actual = _derivative_identity(raw)
    except Exception as exc:
        raise N100DataContractError(
            f"durable derivative decode failed: {row['inference_id']}"
        ) from exc
    expected = (
        row["derivative_encoded_sha256"],
        row["derivative_pixel_sha256"],
        row["derivative_width"],
        row["derivative_height"],
        row["derivative_bytes"],
    )
    if actual != expected:
        raise N100DataContractError(
            f"durable derivative identity mismatch: {row['inference_id']}"
        )
    return raw


def _assert_derivative_allowed(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    inference_id: str,
    derivative_pixel_sha256: str,
    exclusions: N10Exclusions,
) -> None:
    if derivative_pixel_sha256 in exclusions.derivative_pixel_sha256s:
        raise N100DataContractError(
            f"N100 actual derivative overlaps N10: {inference_id}"
        )
    collision = connection.execute(
        "SELECT inference_id FROM vision_inputs WHERE run_id=? "
        "AND inference_id<>? AND derivative_pixel_sha256=? LIMIT 1",
        (run_id, inference_id, derivative_pixel_sha256),
    ).fetchone()
    if collision is not None:
        raise N100DataContractError(
            "identical N100 derivative pixels require explicit reuse links; "
            f"{inference_id} collides with {collision[0]}"
        )


def _prepare_input(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    row: sqlite3.Row,
    exclusions: N10Exclusions,
    cache_dir: Path,
    fetcher: Callable[[str, str], HttpPayload],
    rate_limiter: _GlobalRequestRateLimiter,
    circuit: _CircuitState,
    sleeper: Callable[[float], None],
    clock: Callable[[], float],
) -> tuple[bytes | None, int]:
    state = connection.execute(
        "SELECT * FROM vision_inputs WHERE run_id=? AND inference_id=?",
        (run_id, row["inference_id"]),
    ).fetchone()
    if state is None:
        raise N100DataContractError(f"missing Vision input: {row['inference_id']}")
    if state["status"] == "ready":
        raw = _load_cached(cache_dir, state)
        _assert_derivative_allowed(
            connection,
            run_id=run_id,
            inference_id=str(row["inference_id"]),
            derivative_pixel_sha256=str(state["derivative_pixel_sha256"]),
            exclusions=exclusions,
        )
        return raw, 0
    if state["status"] != "pending":
        return None, 0
    orphan = cache_dir / f"{row['inference_id']}.jpg"
    if orphan.exists():
        orphan.unlink()
    attempt_no = int(
        connection.execute(
            "SELECT COALESCE(MAX(attempt_no),0)+1 FROM fetch_attempts "
            "WHERE run_id=? AND inference_id=?",
            (run_id, row["inference_id"]),
        ).fetchone()[0]
    )
    requests_made = 0
    while attempt_no <= FIXED_FETCH_ATTEMPTS:
        if (
            attempt_no > 1
            and _retry_count(connection, "fetch_attempts", run_id)
            >= MAX_TOTAL_FETCH_RETRIES
        ):
            _pause(
                connection,
                run_id,
                "fetch_retry_cap",
                {
                    "inference_id": row["inference_id"],
                    "max_total_fetch_retries": MAX_TOTAL_FETCH_RETRIES,
                    "next_attempt_no": attempt_no,
                },
            )
        started_at = utc_now()
        started = clock()
        payload: HttpPayload | None = None
        actual_raw: str | None = None
        actual_e1: str | None = None
        attempt_recorded = False
        try:
            rate_limiter.wait()
            payload = fetcher(str(row["source"]), str(row["fetch_url"]))
            requests_made += int(payload.request_count)
            actual_raw = payload.raw_response_sha256
            fingerprint = fingerprint_bytes(payload.body)
            actual_e1 = fingerprint.pixel_sha256
            elapsed = max(0, round((clock() - started) * 1000))
            exact = (
                actual_raw == row["expected_response_sha256"]
                and actual_e1 == row["expected_e1_pixel_sha256"]
            )
            if not exact:
                outcome = (
                    "delivery_changed_pixel_stable"
                    if actual_e1 == row["expected_e1_pixel_sha256"]
                    else "source_changed"
                )
                _insert_fetch_attempt(
                    connection,
                    run_id=run_id,
                    row=row,
                    attempt_no=attempt_no,
                    started_at=started_at,
                    elapsed_ms=elapsed,
                    outcome=outcome,
                    payload=payload,
                    failure=None,
                    actual_response_sha256=actual_raw,
                    actual_e1_pixel_sha256=actual_e1,
                    retryable=False,
                    retry_after=None,
                    delay=None,
                    error_kind=outcome,
                    error_message="frozen response and E1 identities did not match exactly",
                )
                connection.execute(
                    "UPDATE vision_inputs SET status='fetch_failed',completed_at=?,"
                    "error_kind=?,error_message=? WHERE run_id=? AND inference_id=?",
                    (
                        utc_now(),
                        outcome,
                        "frozen response and E1 identities did not match exactly",
                        run_id,
                        row["inference_id"],
                    ),
                )
                connection.commit()
                circuit.consecutive_http_failures = 0
                return None, requests_made
            decoded = decode_source(payload.body)
            derivative = prepare_derivative(decoded, "long1024", 1024)
            _atomic_write_bytes(
                cache_dir / f"{row['inference_id']}.jpg",
                derivative.encoded_bytes,
            )
            _insert_fetch_attempt(
                connection,
                run_id=run_id,
                row=row,
                attempt_no=attempt_no,
                started_at=started_at,
                elapsed_ms=elapsed,
                outcome="exact_match",
                payload=payload,
                failure=None,
                actual_response_sha256=actual_raw,
                actual_e1_pixel_sha256=actual_e1,
                retryable=False,
                retry_after=None,
                delay=None,
                error_kind=None,
                error_message=None,
            )
            attempt_recorded = True
            connection.execute(
                """
                UPDATE vision_inputs SET status='ready',selected_fetch_attempt_no=?,
                  actual_response_sha256=?,actual_e1_pixel_sha256=?,
                  derivative_encoded_sha256=?,derivative_pixel_sha256=?,
                  derivative_width=?,derivative_height=?,derivative_bytes=?,
                  completed_at=NULL,error_kind=NULL,error_message=NULL
                WHERE run_id=? AND inference_id=?
                """,
                (
                    attempt_no,
                    actual_raw,
                    actual_e1,
                    derivative.encoded_sha256,
                    derivative.pixel_sha256,
                    derivative.width,
                    derivative.height,
                    len(derivative.encoded_bytes),
                    run_id,
                    row["inference_id"],
                ),
            )
            connection.commit()
            circuit.consecutive_http_failures = 0
            _assert_derivative_allowed(
                connection,
                run_id=run_id,
                inference_id=str(row["inference_id"]),
                derivative_pixel_sha256=derivative.pixel_sha256,
                exclusions=exclusions,
            )
            return derivative.encoded_bytes, requests_made
        except FetchFailure as exc:
            requests_made += int(exc.request_count)
            retryable = bool(exc.retryable)
            can_retry = retryable and attempt_no < FIXED_FETCH_ATTEMPTS
            retry_after = exc.retry_after_seconds
            delay = (
                max(float(retry_after or 0), min(60.0, float(2 ** (attempt_no - 1))))
                if can_retry
                else None
            )
            kind = str(exc.kind)
            if kind == "oversize":
                outcome = "oversize"
            elif kind in {"decode", "decode_failed", "invalid_dimensions"}:
                outcome = "decode_failed"
            elif kind in {"invalid_content_type", "empty_response"}:
                outcome = "invalid_content"
            else:
                outcome = "http_failed"
            _insert_fetch_attempt(
                connection,
                run_id=run_id,
                row=row,
                attempt_no=attempt_no,
                started_at=started_at,
                elapsed_ms=max(0, round(exc.elapsed_seconds * 1000)),
                outcome=outcome,
                payload=None,
                failure=exc,
                actual_response_sha256=exc.raw_response_sha256,
                actual_e1_pixel_sha256=None,
                retryable=can_retry,
                retry_after=retry_after,
                delay=delay,
                error_kind=kind,
                error_message=str(exc),
            )
            if not can_retry:
                connection.execute(
                    "UPDATE vision_inputs SET status='fetch_failed',completed_at=?,"
                    "error_kind=?,error_message=? WHERE run_id=? AND inference_id=?",
                    (
                        utc_now(),
                        kind,
                        str(exc)[:2000],
                        run_id,
                        row["inference_id"],
                    ),
                )
            connection.commit()
            if retryable:
                circuit.consecutive_http_failures += 1
            else:
                circuit.consecutive_http_failures = 0
            if circuit.consecutive_http_failures >= HTTP_CIRCUIT_THRESHOLD:
                _pause(
                    connection,
                    run_id,
                    "http_circuit_open",
                    {
                        "consecutive_retryable_failures": circuit.consecutive_http_failures,
                        "inference_id": row["inference_id"],
                        "last_error_kind": kind,
                    },
                )
            if not can_retry:
                return None, requests_made
            if delay is not None and delay > MAX_AUTOMATIC_COOLDOWN_SECONDS:
                _pause(
                    connection,
                    run_id,
                    "retry_after_cooldown",
                    {
                        "inference_id": row["inference_id"],
                        "retry_after_seconds": delay,
                    },
                )
            sleeper(float(delay or 0))
            attempt_no += 1
        except (N100RunPaused, N100DataContractError):
            raise
        except Exception as exc:
            # If SQLite work after the insert failed, roll that uncommitted
            # row back before recording the one durable outcome for this HTTP
            # attempt.  This preserves the unique attempt number and ensures
            # storage failures cannot create a duplicate-attempt insert.
            if attempt_recorded:
                connection.rollback()
            elapsed = max(0, round((clock() - started) * 1000))
            kind = type(exc).__name__
            _insert_fetch_attempt(
                connection,
                run_id=run_id,
                row=row,
                attempt_no=attempt_no,
                started_at=started_at,
                elapsed_ms=elapsed,
                outcome="decode_failed" if payload is not None else "http_failed",
                payload=payload,
                failure=None,
                actual_response_sha256=actual_raw,
                actual_e1_pixel_sha256=actual_e1,
                retryable=False,
                retry_after=None,
                delay=None,
                error_kind=kind,
                error_message=str(exc),
            )
            connection.execute(
                "UPDATE vision_inputs SET status='fetch_failed',completed_at=?,"
                "error_kind=?,error_message=? WHERE run_id=? AND inference_id=?",
                (utc_now(), kind, str(exc)[:2000], run_id, row["inference_id"]),
            )
            connection.commit()
            circuit.consecutive_http_failures = 0
            return None, requests_made
    return None, requests_made


def _cache_accounting(
    connection: sqlite3.Connection, run_id: str, cache_dir: Path
) -> tuple[bool, dict[str, Any]]:
    rows = list(
        connection.execute(
            "SELECT * FROM vision_inputs WHERE run_id=? "
            "AND derivative_encoded_sha256 IS NOT NULL ORDER BY inference_id",
            (run_id,),
        )
    )
    expected_names = {f"{row['inference_id']}.jpg" for row in rows}
    actual_names = {path.name for path in cache_dir.iterdir()}
    failures: list[str] = []
    if expected_names != actual_names:
        failures.append("missing=" + canonical_json(sorted(expected_names - actual_names)))
        failures.append(
            "unexpected=" + canonical_json(sorted(actual_names - expected_names))
        )
    for row in rows:
        try:
            _load_cached(cache_dir, row)
        except Exception as exc:
            failures.append(f"{row['inference_id']}:{type(exc).__name__}")
    return not failures, {
        "actual_entries": len(actual_names),
        "expected_jpegs": len(expected_names),
        "failures": failures,
    }


def _metrics(
    connection: sqlite3.Connection,
    run_id: str,
    contract: RuntimeContract,
    cache_dir: Path,
    retained_for_review: bool,
) -> dict[str, Any]:
    input_status = {
        str(row[0]): int(row[1])
        for row in connection.execute(
            "SELECT status,COUNT(*) FROM vision_inputs WHERE run_id=? "
            "GROUP BY status ORDER BY status",
            (run_id,),
        )
    }
    usage = connection.execute(
        "SELECT COUNT(*),SUM(status='success'),COALESCE(SUM(input_tokens),0),"
        "COALESCE(SUM(cached_input_tokens),0),COALESCE(SUM(output_tokens),0),"
        "COALESCE(SUM(elapsed_ms),0) FROM vision_attempts WHERE run_id=?",
        (run_id,),
    ).fetchone()
    event_counts = {
        str(row[0]): int(row[1])
        for row in connection.execute(
            "SELECT event_type,COUNT(*) FROM runtime_events WHERE run_id=? "
            "GROUP BY event_type ORDER BY event_type",
            (run_id,),
        )
    }
    return {
        "buildings": contract.building_count,
        "occurrences": contract.occurrence_count,
        "planned_batches": contract.planned_batch_count,
        "input_status": input_status,
        "fetch_attempts": int(
            connection.execute(
                "SELECT COUNT(*) FROM fetch_attempts WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        ),
        "fetch_retries": _retry_count(connection, "fetch_attempts", run_id),
        "downloaded_bytes": int(
            connection.execute(
                "SELECT COALESCE(SUM(response_bytes),0) FROM fetch_attempts "
                "WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
        ),
        "vision_attempts": int(usage[0]),
        "vision_retries": _retry_count(connection, "vision_attempts", run_id),
        "vision_call_cap": contract.planned_batch_count + MAX_TOTAL_VISION_RETRIES,
        "successful_vision_attempts": int(usage[1] or 0),
        "input_tokens": int(usage[2]),
        "cached_input_tokens": int(usage[3]),
        "output_tokens": int(usage[4]),
        "vision_elapsed_ms": int(usage[5]),
        "results": int(
            connection.execute(
                "SELECT COUNT(*) FROM semantic_results WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        ),
        "runtime_events": event_counts,
        "materialization_cache": {
            "path": str(cache_dir.resolve()),
            "retained_for_review": retained_for_review,
            "write_contract": "atomic-exclusive-sha256-v1",
        },
    }


def _logical_sha256(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    digest.update((LOGICAL_MANIFEST_VERSION + "\n").encode("ascii"))
    tables = N10_LOGICAL_TABLES + (
        (
            "runtime_events",
            "event_id",
            ("event_id", "event_type", "detail_json"),
        ),
    )
    for table, order_by, columns in tables:
        digest.update((table + "\0").encode("utf-8"))
        for row in connection.execute(
            f"SELECT {','.join(columns)} FROM {table} ORDER BY {order_by}"
        ):
            digest.update(
                canonical_json(dict(zip(columns, row))).encode("utf-8") + b"\n"
            )
    return digest.hexdigest()


def _render_report(
    status: str, logical_sha256: str, metrics: Mapping[str, Any]
) -> str:
    return "\n".join(
        [
            "# Cross-source semantic Vision N100",
            "",
            f"- Status: `{status}`",
            f"- Logical SHA-256: `{logical_sha256}`",
            f"- Buildings / occurrences: `{metrics['buildings']}` / `{metrics['occurrences']}`",
            f"- Input states: `{canonical_json(metrics['input_status'])}`",
            f"- Fetch attempts / retries: `{metrics['fetch_attempts']}` / `{metrics['fetch_retries']}`",
            f"- Downloaded bytes: `{metrics['downloaded_bytes']}`",
            f"- Vision attempts / retries / results: `{metrics['vision_attempts']}` / `{metrics['vision_retries']}` / `{metrics['results']}`",
            f"- Tokens input / cached / output: `{metrics['input_tokens']}` / `{metrics['cached_input_tokens']}` / `{metrics['output_tokens']}`",
            "",
            "This immutable N100 is calibration evidence. Hero and coverage rows are non-authoritative pixel-derived evidence.",
            "",
        ]
    )


def _report_binding(status: str, metrics: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        _render_report(status, REPORT_LOGICAL_PLACEHOLDER, metrics).encode("utf-8")
    ).hexdigest()


def _terminal_validation_rows(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    contract: RuntimeContract,
    exclusions: N10Exclusions,
    manifest_path: Path,
    e2_after: str,
    e3_after: str,
    cache_dir: Path,
) -> list[dict[str, Any]]:
    run = connection.execute(
        "SELECT * FROM semantic_runs WHERE run_id=?", (run_id,)
    ).fetchone()
    status_counts = {
        str(row[0]): int(row[1])
        for row in connection.execute(
            "SELECT status,COUNT(*) FROM vision_inputs WHERE run_id=? GROUP BY status",
            (run_id,),
        )
    }
    result_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM semantic_results WHERE run_id=?", (run_id,)
        ).fetchone()[0]
    )
    success_count = status_counts.get("success", 0)
    gaps = int(
        connection.execute(
            "SELECT COUNT(*) FROM (SELECT inference_id,MIN(attempt_no) lo,"
            "MAX(attempt_no) hi,COUNT(*) n FROM fetch_attempts WHERE run_id=? "
            "GROUP BY inference_id HAVING lo<>1 OR hi<>n)",
            (run_id,),
        ).fetchone()[0]
    )
    duplicate_derivatives = int(
        connection.execute(
            "SELECT COUNT(*) FROM (SELECT derivative_pixel_sha256 FROM vision_inputs "
            "WHERE run_id=? AND derivative_pixel_sha256 IS NOT NULL "
            "GROUP BY derivative_pixel_sha256 HAVING COUNT(*)>1)",
            (run_id,),
        ).fetchone()[0]
    )
    actual_pixels = {
        str(row[0])
        for row in connection.execute(
            "SELECT derivative_pixel_sha256 FROM vision_inputs WHERE run_id=? "
            "AND derivative_pixel_sha256 IS NOT NULL",
            (run_id,),
        )
    }
    n10_overlap = sorted(actual_pixels & exclusions.derivative_pixel_sha256s)
    cache_ok, cache_detail = _cache_accounting(connection, run_id, cache_dir)
    checks = [
        (
            "manifest_identity",
            file_sha256(manifest_path) == contract.manifest_byte_sha256,
            contract.manifest_byte_sha256,
            file_sha256(manifest_path),
            None,
        ),
        (
            "fixed_population_accounting",
            connection.execute("SELECT COUNT(*) FROM selected_buildings").fetchone()[0]
            == contract.building_count
            and connection.execute(
                "SELECT COUNT(*) FROM selected_occurrences"
            ).fetchone()[0]
            == contract.occurrence_count,
            f"{contract.building_count}/{contract.occurrence_count}",
            f"{connection.execute('SELECT COUNT(*) FROM selected_buildings').fetchone()[0]}/"
            f"{connection.execute('SELECT COUNT(*) FROM selected_occurrences').fetchone()[0]}",
            None,
        ),
        (
            "input_files_immutable",
            e2_after == run["e2_sha256_before"]
            and e3_after == run["e3_sha256_before"],
            f"{run['e2_sha256_before']}/{run['e3_sha256_before']}",
            f"{e2_after}/{e3_after}",
            None,
        ),
        ("n10_exclusion", not n10_overlap, "[]", canonical_json(n10_overlap), None),
        (
            "distinct_derivative_inputs",
            duplicate_derivatives == 0,
            "0",
            str(duplicate_derivatives),
            "N100 v1 has no implicit exact-pixel result reuse.",
        ),
        ("attempt_accounting", gaps == 0, "0", str(gaps), None),
        (
            "global_retry_caps",
            _retry_count(connection, "fetch_attempts", run_id)
            <= MAX_TOTAL_FETCH_RETRIES
            and _retry_count(connection, "vision_attempts", run_id)
            <= MAX_TOTAL_VISION_RETRIES
            and connection.execute(
                "SELECT COUNT(*) FROM vision_attempts WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            <= contract.planned_batch_count + MAX_TOTAL_VISION_RETRIES
            and _token_totals(connection, run_id)[0] <= MAX_TOTAL_INPUT_TOKENS
            and _token_totals(connection, run_id)[1] <= MAX_TOTAL_OUTPUT_TOKENS
            and sum(_token_totals(connection, run_id)) <= MAX_TOTAL_TOKENS,
            f"<={MAX_TOTAL_FETCH_RETRIES}/<={MAX_TOTAL_VISION_RETRIES}",
            f"{_retry_count(connection, 'fetch_attempts', run_id)}/"
            f"{_retry_count(connection, 'vision_attempts', run_id)}",
            None,
        ),
        (
            "result_accounting",
            result_count == success_count,
            str(success_count),
            str(result_count),
            None,
        ),
        (
            "no_pending_work",
            status_counts.get("pending", 0) + status_counts.get("ready", 0) == 0,
            "0",
            str(status_counts.get("pending", 0) + status_counts.get("ready", 0)),
            None,
        ),
        (
            "materialization_cache_integrity",
            cache_ok,
            canonical_json({"failures": [], "expected_jpegs": len(actual_pixels)}),
            canonical_json(cache_detail),
            None,
        ),
        (
            "sqlite_quick_check",
            connection.execute("PRAGMA quick_check").fetchone()[0] == "ok",
            "ok",
            connection.execute("PRAGMA quick_check").fetchone()[0],
            None,
        ),
        (
            "sqlite_integrity_check",
            connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
            "ok",
            connection.execute("PRAGMA integrity_check").fetchone()[0],
            None,
        ),
        (
            "foreign_key_check",
            not connection.execute("PRAGMA foreign_key_check").fetchall(),
            "0",
            str(len(connection.execute("PRAGMA foreign_key_check").fetchall())),
            None,
        ),
    ]
    rows = [
        {
            "name": name,
            "severity": "error",
            "passed": bool(passed),
            "expected": expected,
            "actual": actual,
            "detail": detail,
        }
        for name, passed, expected, actual, detail in checks
    ]
    rows.append(
        {
            "name": "n100_technical_gate",
            "severity": "warning",
            "passed": success_count == contract.occurrence_count,
            "expected": str(contract.occurrence_count),
            "actual": str(success_count),
            "detail": "A frozen N100 never replaces a failed input.",
        }
    )
    return rows


def _cleanup_cache(cache_dir: Path, inference_ids: Sequence[str]) -> None:
    if cache_dir.is_symlink():
        raise N100SemanticVisionError("N100 cache cannot be a symlink")
    resolved = cache_dir.resolve()
    for inference_id in inference_ids:
        if not re.fullmatch(r"semv100_\d{6}", inference_id):
            raise N100SemanticVisionError(
                f"unsafe N100 cache identifier: {inference_id!r}"
            )
        target = (resolved / f"{inference_id}.jpg").resolve()
        if target.parent != resolved:
            raise N100SemanticVisionError("N100 cache cleanup escaped its directory")
        try:
            target.unlink()
        except FileNotFoundError:
            pass
    try:
        resolved.rmdir()
    except OSError as exc:
        raise N100SemanticVisionError(
            f"N100 cache is not empty after exact cleanup: {resolved}"
        ) from exc


def validate_terminal_n100(
    path: Path,
    report_path: Path,
    contract: RuntimeContract,
) -> tuple[str, str, dict[str, Any]]:
    """Validate the runner-owned terminal invariants without network or writes."""

    if _sidecars(path):
        raise N100SemanticVisionError(f"terminal N100 has SQLite sidecars: {_sidecars(path)}")
    connection = open_immutable(path)
    try:
        if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise N100SemanticVisionError("N100 application_id mismatch")
        if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise N100SemanticVisionError("N100 user_version mismatch")
        run = connection.execute("SELECT * FROM semantic_runs").fetchone()
        if run is None or run["status"] not in {"complete", "complete_with_failures"}:
            raise N100SemanticVisionError("N100 sidecar is not terminal")
        if (
            run["building_count"] != contract.building_count
            or run["occurrence_count"] != contract.occurrence_count
            or run["manifest_byte_sha256"] != contract.manifest_byte_sha256
            or run["manifest_self_sha256"] != contract.manifest_self_sha256
            or run["sample_seed"] != contract.sample_seed
            or run["ordered_building_manifest_sha256"]
            != contract.ordered_building_manifest_sha256
            or run["ordered_occurrence_manifest_sha256"]
            != contract.ordered_occurrence_manifest_sha256
        ):
            raise N100SemanticVisionError("terminal N100 contract mismatch")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise N100SemanticVisionError("terminal N100 quick_check failed")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise N100SemanticVisionError("terminal N100 integrity_check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise N100SemanticVisionError("terminal N100 foreign_key_check failed")
        validations = {
            str(row[0]): (str(row[1]), int(row[2]))
            for row in connection.execute(
                "SELECT validation_name,severity,passed FROM validations"
            )
        }
        if any(validations.get(name) != ("error", 1) for name in REQUIRED_VALIDATIONS):
            raise N100SemanticVisionError("required N100 validations are absent or failed")
        logical = _logical_sha256(connection)
        if logical != run["logical_sha256"]:
            raise N100SemanticVisionError("terminal N100 logical SHA mismatch")
        metrics = json.loads(str(run["metrics_json"]))
        expected_report = _render_report(str(run["status"]), logical, metrics).encode(
            "utf-8"
        )
        if not report_path.is_file() or report_path.read_bytes() != expected_report:
            raise N100SemanticVisionError("terminal N100 report binding mismatch")
        return str(run["status"]), logical, metrics
    finally:
        connection.close()


def run_semantic_vision_n100(
    *,
    contract: RuntimeContract,
    manifest_path: Path,
    e2_path: Path,
    e3_path: Path,
    n10_sidecar_path: Path,
    output_db: Path,
    report_path: Path,
    codex_bin: Path,
    model: str = DEFAULT_MODEL,
    reasoning: str = DEFAULT_REASONING,
    service_tier: str = DEFAULT_SERVICE_TIER,
    cli_version: str | None = None,
    resume: bool = False,
    review_cache_dir: Path | None = None,
    expected_n10_sidecar_sha256: str = FROZEN_N10_SIDECAR_SHA256,
    fetcher: Callable[[str, str], HttpPayload] = network_fetch,
    executor: Callable[..., VisionRuntimeResult] = run_codex_vision_batch,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    n10_loader: Callable[..., N10Exclusions] = load_n10_exclusions,
) -> N100RunResult:
    """Run or exactly resume the frozen N100, stopping safely on breakers."""

    manifest_path = manifest_path.resolve()
    e2_path = e2_path.resolve()
    e3_path = e3_path.resolve()
    n10_sidecar_path = n10_sidecar_path.resolve()
    output_db = output_db.resolve()
    report_path = report_path.resolve()
    if len(
        {
            manifest_path,
            e2_path,
            e3_path,
            n10_sidecar_path,
            output_db,
            report_path,
        }
    ) != 6:
        raise ValueError("all N100 inputs, output, and report paths must be distinct")
    payload, manifest_raw = _load_manifest(manifest_path, contract)
    buildings, _ = _manifest_rows(payload, contract)
    e2_before = _verify_input(e2_path, payload["e2_input"])
    e3_before = _verify_input(e3_path, payload["e3_input"])
    exclusions = n10_loader(
        n10_sidecar_path, expected_sha256=expected_n10_sidecar_sha256
    )
    if exclusions.sidecar_sha256 != expected_n10_sidecar_sha256:
        raise N100DataContractError("N10 loader returned the wrong sidecar identity")
    manifest_n10 = payload.get("n10_semantic_sidecar_input")
    if not isinstance(manifest_n10, Mapping) or (
        manifest_n10.get("byte_sha256") != exclusions.sidecar_sha256
    ):
        raise N100DataContractError(
            "N100 manifest does not bind the supplied N10 semantic sidecar"
        )
    overlap = sorted(
        {str(row["selection_id"]) for row in buildings} & exclusions.selection_ids
    )
    if overlap:
        raise N100DataContractError(
            "N100 building population overlaps N10: " + canonical_json(overlap)
        )

    output_db.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(str(output_db) + ".lock")
    descriptor = _acquire_lock(lock_path)
    requests_made = 0
    vision_requests = 0
    try:
        if output_db.exists():
            if not resume:
                raise FileExistsError(f"immutable N100 output exists: {output_db}")
            status, logical, metrics = validate_terminal_n100(
                output_db, report_path, contract
            )
            return N100RunResult(
                output_db,
                report_path,
                status,
                logical,
                0,
                0,
                True,
                True,
                metrics,
            )
        if report_path.exists():
            raise FileExistsError(f"immutable N100 report exists: {report_path}")
        partial_db = output_db.with_name(output_db.name + ".partial")
        partial_report = report_path.with_name(report_path.name + ".partial")
        if partial_report.exists():
            raise FileExistsError(f"stale N100 partial report exists: {partial_report}")
        resumed = partial_db.exists()
        if resumed and not resume:
            raise FileExistsError(f"N100 partial exists; pass --resume: {partial_db}")
        if resume and not resumed:
            raise FileNotFoundError(f"N100 partial does not exist: {partial_db}")
        retained_for_review = review_cache_dir is not None
        cache_dir = (
            review_cache_dir.resolve()
            if review_cache_dir is not None
            else Path(str(partial_db) + ".inputs")
        )
        if cache_dir.exists() and not resumed:
            raise FileExistsError(f"N100 cache already exists: {cache_dir}")
        if resumed and not cache_dir.is_dir():
            raise N100DataContractError(f"N100 resume cache is missing: {cache_dir}")
        cache_dir.mkdir(parents=True, exist_ok=resumed)
        if resumed:
            recover_sqlite(partial_db)
        connection = sqlite3.connect(partial_db)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        try:
            expected_dependency = _contract_provenance(
                contract,
                exclusions,
                cache_dir,
                retained_for_review=retained_for_review,
            )
            if resumed:
                run = connection.execute("SELECT * FROM semantic_runs").fetchone()
                if run is None or run["status"] != "running":
                    raise N100DataContractError("N100 resume requires a running partial")
                expected = {
                    "runner_version": RUNNER_VERSION,
                    "schema_version": SCHEMA_VERSION,
                    "manifest_byte_sha256": contract.manifest_byte_sha256,
                    "manifest_self_sha256": contract.manifest_self_sha256,
                    "sample_seed": contract.sample_seed,
                    "building_count": contract.building_count,
                    "occurrence_count": contract.occurrence_count,
                    "e2_sha256_before": e2_before,
                    "e3_sha256_before": e3_before,
                    "contract_version": CONTRACT_VERSION,
                    "prompt_version": PROMPT_VERSION,
                    "transform_version": TRANSFORM_VERSION,
                    "runtime_version": RUNTIME_VERSION,
                    "retry_policy_version": RETRY_POLICY_VERSION,
                    "batch_size": FIXED_BATCH_SIZE,
                    "max_fetch_attempts": FIXED_FETCH_ATTEMPTS,
                    "max_vision_attempts": FIXED_VISION_ATTEMPTS,
                    "model": model,
                    "reasoning": reasoning,
                    "service_tier": service_tier,
                    "cli_version": cli_version,
                    "dependency_manifest_json": canonical_json(expected_dependency),
                    "dependency_manifest_sha256": canonical_sha256(expected_dependency),
                }
                mismatch = {
                    key: {"actual": run[key], "expected": value}
                    for key, value in expected.items()
                    if run[key] != value
                }
                if mismatch:
                    raise N100DataContractError(
                        "N100 resume contract mismatch: " + canonical_json(mismatch)
                    )
                run_id = str(run["run_id"])
                _clear_stale_atomic_temps(cache_dir)
            else:
                run_id = "semn100-" + hashlib.sha256(
                    (
                        contract.manifest_self_sha256
                        + "\0"
                        + model
                        + "\0"
                        + PROMPT_VERSION
                        + "\0"
                        + exclusions.manifest_sha256
                    ).encode("utf-8")
                ).hexdigest()[:24]
                initialize_n100_sidecar(
                    connection,
                    run_id=run_id,
                    manifest_path=manifest_path,
                    manifest_raw=manifest_raw,
                    payload=payload,
                    contract=contract,
                    exclusions=exclusions,
                    e2_path=e2_path,
                    e3_path=e3_path,
                    model=model,
                    reasoning=reasoning,
                    service_tier=service_tier,
                    cli_version=cli_version,
                    cache_dir=cache_dir,
                    retained_for_review=retained_for_review,
                )

            source_rows = list(
                connection.execute(
                    "SELECT o.* FROM selected_occurrences o WHERE o.run_id=? "
                    "ORDER BY o.input_rank",
                    (run_id,),
                )
            )
            circuit = _CircuitState()
            limiter = _GlobalRequestRateLimiter(
                DEFAULT_REQUESTS_PER_SECOND, clock, sleeper
            )

            # Phase one is deliberately complete before any model call.  This
            # makes the N10 derivative exclusion and N100 collision check a
            # true pre-inference gate rather than a late accounting check.
            for row in source_rows:
                _, made = _prepare_input(
                    connection,
                    run_id=run_id,
                    row=row,
                    exclusions=exclusions,
                    cache_dir=cache_dir,
                    fetcher=fetcher,
                    rate_limiter=limiter,
                    circuit=circuit,
                    sleeper=sleeper,
                    clock=clock,
                )
                requests_made += made

            schema_text = canonical_json(OUTPUT_SCHEMA)
            for batch_start in range(0, len(source_rows), FIXED_BATCH_SIZE):
                batch_no = batch_start // FIXED_BATCH_SIZE + 1
                batch = source_rows[batch_start : batch_start + FIXED_BATCH_SIZE]
                ready_rows: list[sqlite3.Row] = []
                for row in batch:
                    status = connection.execute(
                        "SELECT status FROM vision_inputs WHERE run_id=? AND inference_id=?",
                        (run_id, row["inference_id"]),
                    ).fetchone()[0]
                    if status == "ready":
                        ready_rows.append(row)
                if not ready_rows:
                    continue
                inference_ids = [str(row["inference_id"]) for row in ready_rows]
                attempts = list(
                    connection.execute(
                        "SELECT * FROM vision_attempts WHERE run_id=? AND batch_no=? "
                        "ORDER BY attempt_no",
                        (run_id, batch_no),
                    )
                )
                if any(
                    json.loads(str(attempt["inference_ids_json"])) != inference_ids
                    for attempt in attempts
                ):
                    raise N100DataContractError("N100 retry batch membership changed")
                if len(attempts) >= FIXED_VISION_ATTEMPTS:
                    connection.executemany(
                        "UPDATE vision_inputs SET status='vision_failed',completed_at=?,"
                        "error_kind='retry_exhausted',error_message='Vision retry budget exhausted' "
                        "WHERE run_id=? AND inference_id=? AND status='ready'",
                        [(utc_now(), run_id, value) for value in inference_ids],
                    )
                    connection.commit()
                    continue
                with tempfile.TemporaryDirectory(
                    prefix="semantic-vision-n100-"
                ) as temp_name:
                    temp_dir = Path(temp_name)
                    schema_path = temp_dir / "output.schema.json"
                    schema_path.write_text(
                        schema_text, encoding="utf-8", newline="\n"
                    )
                    image_paths: list[Path] = []
                    for row in ready_rows:
                        state = connection.execute(
                            "SELECT * FROM vision_inputs WHERE run_id=? AND inference_id=?",
                            (run_id, row["inference_id"]),
                        ).fetchone()
                        raw = _load_cached(cache_dir, state)
                        _assert_derivative_allowed(
                            connection,
                            run_id=run_id,
                            inference_id=str(row["inference_id"]),
                            derivative_pixel_sha256=str(
                                state["derivative_pixel_sha256"]
                            ),
                            exclusions=exclusions,
                        )
                        image_path = temp_dir / f"{row['inference_id']}.jpg"
                        image_path.write_bytes(raw)
                        image_paths.append(image_path)
                    succeeded = False
                    for attempt_no in range(
                        len(attempts) + 1, FIXED_VISION_ATTEMPTS + 1
                    ):
                        _enforce_model_envelope(
                            connection,
                            run_id=run_id,
                            contract=contract,
                            batch_no=batch_no,
                            after_attempt=False,
                        )
                        if (
                            attempt_no > 1
                            and _retry_count(connection, "vision_attempts", run_id)
                            >= MAX_TOTAL_VISION_RETRIES
                        ):
                            _pause(
                                connection,
                                run_id,
                                "vision_retry_cap",
                                {
                                    "batch_no": batch_no,
                                    "max_total_vision_retries": MAX_TOTAL_VISION_RETRIES,
                                    "next_attempt_no": attempt_no,
                                },
                            )
                        started_at = utc_now()
                        prompt = compose_prompt(inference_ids)
                        executor_started = clock()
                        vision_requests += 1
                        try:
                            result = executor(
                                prompt=prompt,
                                image_paths=image_paths,
                                output_schema_path=schema_path,
                                expected_asset_ids=inference_ids,
                                codex_bin=codex_bin,
                                model=model,
                                reasoning=reasoning,
                                service_tier=service_tier,
                                working_directory=temp_dir,
                                timeout_seconds=600,
                            )
                        except Exception as exc:
                            result = _executor_exception_result(
                                exc=exc,
                                prompt=prompt,
                                image_paths=image_paths,
                                output_schema_path=schema_path,
                                inference_ids=inference_ids,
                                codex_bin=codex_bin,
                                model=model,
                                reasoning=reasoning,
                                service_tier=service_tier,
                                working_directory=temp_dir,
                                elapsed_seconds=clock() - executor_started,
                            )
                        normalized: Sequence[Mapping[str, Any]] | None = None
                        error_kind: str | None = None
                        error_message: str | None = None
                        if result.ok:
                            try:
                                normalized = normalize_batch(
                                    result.records, inference_ids
                                )
                            except Exception as exc:
                                error_kind = "semantic_schema"
                                error_message = str(exc)
                        if not result.ok or normalized is None:
                            _write_vision_attempt(
                                connection,
                                run_id=run_id,
                                batch_no=batch_no,
                                attempt_no=attempt_no,
                                inference_ids=inference_ids,
                                result=result,
                                status="failed",
                                started_at=started_at,
                                error_kind=error_kind,
                                error_message=error_message,
                            )
                            connection.commit()
                            _enforce_model_envelope(
                                connection,
                                run_id=run_id,
                                contract=contract,
                                batch_no=batch_no,
                                after_attempt=True,
                            )
                            circuit.consecutive_vision_failures += 1
                            if (
                                circuit.consecutive_vision_failures
                                >= VISION_CIRCUIT_THRESHOLD
                            ):
                                _pause(
                                    connection,
                                    run_id,
                                    "vision_circuit_open",
                                    {
                                        "batch_no": batch_no,
                                        "consecutive_failures": circuit.consecutive_vision_failures,
                                    },
                                )
                            continue
                        attempt_id = _write_vision_attempt(
                            connection,
                            run_id=run_id,
                            batch_no=batch_no,
                            attempt_no=attempt_no,
                            inference_ids=inference_ids,
                            result=result,
                            status="success",
                            started_at=started_at,
                        )
                        _write_success_results(
                            connection,
                            run_id=run_id,
                            attempt_id=attempt_id,
                            rows=ready_rows,
                            raw_results=result.records,
                            normalized=normalized,
                        )
                        connection.commit()
                        _enforce_model_envelope(
                            connection,
                            run_id=run_id,
                            contract=contract,
                            batch_no=batch_no,
                            after_attempt=True,
                        )
                        circuit.consecutive_vision_failures = 0
                        succeeded = True
                        break
                    if not succeeded:
                        connection.executemany(
                            "UPDATE vision_inputs SET status='vision_failed',completed_at=?,"
                            "error_kind='retry_exhausted',error_message='Vision retry budget exhausted' "
                            "WHERE run_id=? AND inference_id=? AND status='ready'",
                            [(utc_now(), run_id, value) for value in inference_ids],
                        )
                        connection.commit()

            pending = int(
                connection.execute(
                    "SELECT COUNT(*) FROM vision_inputs WHERE run_id=? "
                    "AND status IN ('pending','ready')",
                    (run_id,),
                ).fetchone()[0]
            )
            if pending:
                raise N100SemanticVisionError(
                    f"N100 reached terminalization with {pending} pending/ready inputs"
                )
            _fill_unobserved_slots(connection, run_id)
            connection.commit()
            e2_after = _verify_input(e2_path, payload["e2_input"])
            e3_after = _verify_input(e3_path, payload["e3_input"])
            if (
                _sidecars(n10_sidecar_path)
                or file_sha256(n10_sidecar_path) != exclusions.sidecar_sha256
            ):
                raise N100SemanticVisionError("N10 sidecar changed during N100")
            validations = _terminal_validation_rows(
                connection,
                run_id=run_id,
                contract=contract,
                exclusions=exclusions,
                manifest_path=manifest_path,
                e2_after=e2_after,
                e3_after=e3_after,
                cache_dir=cache_dir,
            )
            hard_failures = [
                row
                for row in validations
                if row["severity"] == "error" and not row["passed"]
            ]
            if hard_failures:
                raise N100SemanticVisionError(
                    "N100 terminal validation failed: " + canonical_json(hard_failures)
                )
            metrics = _metrics(
                connection,
                run_id,
                contract,
                cache_dir,
                retained_for_review,
            )
            failures = contract.occurrence_count - int(
                metrics["input_status"].get("success", 0)
            )
            terminal_status = "complete_with_failures" if failures else "complete"
            binding = _report_binding(terminal_status, metrics)
            validations.append(
                {
                    "name": "report_binding",
                    "severity": "error",
                    "passed": True,
                    "expected": binding,
                    "actual": binding,
                    "detail": "SHA-256 of canonical report with logical placeholder.",
                }
            )
            connection.executemany(
                "INSERT INTO validations VALUES(?,?,?,?,?,?,?)",
                [
                    (
                        run_id,
                        row["name"],
                        row["severity"],
                        int(row["passed"]),
                        row["expected"],
                        row["actual"],
                        row["detail"],
                    )
                    for row in validations
                ],
            )
            logical = _logical_sha256(connection)
            report_raw = _render_report(
                terminal_status, logical, metrics
            ).encode("utf-8")
            connection.execute(
                "UPDATE semantic_runs SET status=?,e2_sha256_after=?,e3_sha256_after=?,"
                "completed_at=?,metrics_json=?,logical_sha256=?,error=NULL WHERE run_id=?",
                (
                    terminal_status,
                    e2_after,
                    e3_after,
                    utc_now(),
                    canonical_json(metrics),
                    logical,
                    run_id,
                ),
            )
            connection.commit()
            _atomic_write_bytes(partial_report, report_raw)
            materialized_ids = tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT inference_id FROM vision_inputs WHERE run_id=? "
                    "AND derivative_encoded_sha256 IS NOT NULL ORDER BY inference_id",
                    (run_id,),
                )
            )
        finally:
            connection.close()
        validate_terminal_n100(partial_db, partial_report, contract)
        _publish(partial_db, output_db, partial_report, report_path)
        validate_terminal_n100(output_db, report_path, contract)
        if not retained_for_review:
            _cleanup_cache(cache_dir, materialized_ids)
        return N100RunResult(
            output_db,
            report_path,
            terminal_status,
            logical,
            requests_made,
            vision_requests,
            resumed,
            False,
            metrics,
        )
    finally:
        _release_lock(lock_path, descriptor)


__all__ = [
    "DEFAULT_REQUESTS_PER_SECOND",
    "FIXED_BATCH_SIZE",
    "FIXED_BUILDING_COUNT",
    "FROZEN_N10_SIDECAR_SHA256",
    "HTTP_CIRCUIT_THRESHOLD",
    "MAX_OCCURRENCE_COUNT",
    "MAX_PLANNED_BATCHES",
    "MAX_TOTAL_FETCH_RETRIES",
    "MAX_TOTAL_INPUT_TOKENS",
    "MAX_TOTAL_OUTPUT_TOKENS",
    "MAX_TOTAL_TOKENS",
    "MAX_TOTAL_VISION_RETRIES",
    "N10Exclusions",
    "N100DataContractError",
    "N100RunPaused",
    "N100RunResult",
    "N100SemanticVisionError",
    "RuntimeContract",
    "VISION_CIRCUIT_THRESHOLD",
    "initialize_n100_sidecar",
    "load_n10_exclusions",
    "run_semantic_vision_n100",
    "validate_terminal_n100",
]
