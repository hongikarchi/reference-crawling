"""Crash-safe, reservation-ledgered semantic Vision N10 v1.2 runner.

This module is deliberately separate from the frozen N10 v1, N10 v1.1, and
N100 implementations.  Their schemas, validators, logical digests, and
immutable artifacts must remain byte compatible.  The v1.2 runner is a pinned
fork of the hardened v1.1 state machine; its schema/version namespace is
independent so a new paid smoke cannot widen legacy interpretation.

External calls follow a conservative two-step protocol:

1. append and commit ``external_call_reservations``;
2. invoke HTTP/Codex, then append completion and attempt evidence.

Any reservation without durable attempt evidence is consumed on resume as
``interrupted_unknown``.  It is never silently repeated under the same retry or
call budget.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

from canonical.cross_source_image_selection import canonical_json, canonical_sha256
from canonical.cross_source_semantic_fetch import (
    FetchFailure,
    FetchPayload,
    SOURCE_HOSTS,
)
from canonical.cross_source_semantic_fetch_v12 import (
    FETCH_CONTRACT_VERSION,
    PhysicalRequestCompletion,
    fetch_once_reserved,
)
from canonical.cross_source_semantic_vision_sidecar import (
    APPLICATION_ID,
    SIDECAR_SCHEMA as LEGACY_SCHEMA,
    _GlobalRequestRateLimiter,
    _acquire_lock,
    _atomic_write_bytes,
    _clear_stale_atomic_temps,
    _derivative_identity,
    _release_lock,
    _sanitize_capture,
    file_sha256,
    open_immutable,
    recover_sqlite,
    utc_now,
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
from canonical.image_fingerprint import FINGERPRINT_CONTRACT_VERSION, fingerprint_bytes


SCHEMA_VERSION = 4
RUNNER_VERSION = "cross-source-semantic-vision-n10-v1.2-runner-v2.0.0"
RETRY_POLICY_VERSION = "cross-source-semantic-vision-n10-v1.2-retry-v2.0.0"
PUBLICATION_CONTRACT_VERSION = "semantic-two-file-recoverable-publication-v2"
LOGICAL_MANIFEST_VERSION = "cross-source-semantic-vision-n10-v1.2-logical-v1"
FIXED_BUILDING_COUNT = 10
FIXED_BATCH_SIZE = 5
FIXED_FETCH_ATTEMPTS = 3
FIXED_VISION_ATTEMPTS = 2
DEFAULT_REQUESTS_PER_SECOND = 2.0
HTTP_CIRCUIT_THRESHOLD = 5
REPORT_LOGICAL_PLACEHOLDER = "0" * 64
MAX_STDOUT_CAPTURE_BYTES = 8 * 1024 * 1024
MAX_STDERR_CAPTURE_BYTES = 1024 * 1024
TERMINAL_STATUSES = frozenset({"complete", "complete_with_failures"})

REQUIRED_VALIDATIONS = (
    "manifest_identity",
    "fixed_population_accounting",
    "input_files_immutable",
    "actual_derivative_exclusion_gate",
    "reservation_accounting",
    "model_call_reservation_cap",
    "observed_token_threshold_accounting",
    "attempt_accounting",
    "result_accounting",
    "no_pending_work",
    "materialization_cache_integrity",
    "sqlite_quick_check",
    "sqlite_integrity_check",
    "foreign_key_check",
    "report_binding",
)


class N10V12Error(RuntimeError):
    """Fail-closed v1.2 provenance, state, or publication error."""


class N10V12Paused(N10V12Error):
    """A resumable circuit or observed-cost threshold stopped new calls."""

    def __init__(self, reason: str, detail: Mapping[str, Any]) -> None:
        super().__init__(f"{reason}: {canonical_json(dict(detail))}")
        self.reason = reason
        self.detail = dict(detail)


@dataclass(frozen=True)
class PopulationContract:
    """Literal identity and approved envelope of one new disjoint N10."""

    manifest_domain: str
    manifest_byte_sha256: str
    manifest_self_sha256: str
    sample_seed: str
    occurrence_count: int
    ordered_building_manifest_sha256: str
    ordered_occurrence_manifest_sha256: str
    ordered_fetch_url_manifest_sha256: str
    ordered_e1_pixel_manifest_sha256: str
    inference_id_prefix: str
    max_model_call_reservations: int
    observed_input_token_stop_threshold: int
    observed_output_token_stop_threshold: int
    observed_total_token_stop_threshold: int
    prior_run_names: tuple[str, ...]
    prior_successful_derivative_count: int
    max_redirects: int = 5

    def __post_init__(self) -> None:
        for name in (
            "manifest_byte_sha256",
            "manifest_self_sha256",
            "ordered_building_manifest_sha256",
            "ordered_occurrence_manifest_sha256",
            "ordered_fetch_url_manifest_sha256",
            "ordered_e1_pixel_manifest_sha256",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError(f"{name} must be lowercase SHA-256 hex")
        if not self.manifest_domain or not self.sample_seed:
            raise ValueError("manifest domain and sample seed are required")
        if (
            not self.prior_run_names
            or len(set(self.prior_run_names)) != len(self.prior_run_names)
            or any(not isinstance(value, str) or not value for value in self.prior_run_names)
        ):
            raise ValueError("prior_run_names must contain distinct non-empty names")
        if self.prior_successful_derivative_count < 0:
            raise ValueError("prior_successful_derivative_count must be non-negative")
        if not re.fullmatch(r"[a-z][a-z0-9_]*_", self.inference_id_prefix):
            raise ValueError("inference_id_prefix must be opaque lowercase prefix ending in _")
        if self.occurrence_count < FIXED_BUILDING_COUNT:
            raise ValueError("N10 must contain at least one occurrence per building")
        planned = math.ceil(self.occurrence_count / FIXED_BATCH_SIZE)
        if not planned <= self.max_model_call_reservations <= planned * FIXED_VISION_ATTEMPTS:
            raise ValueError("model-call reservation cap must cover planned calls but not exceed retry budget")
        for name in (
            "observed_input_token_stop_threshold",
            "observed_output_token_stop_threshold",
            "observed_total_token_stop_threshold",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if not 0 <= self.max_redirects <= 5:
            raise ValueError("max_redirects must be between zero and five")

    @property
    def planned_batch_count(self) -> int:
        return math.ceil(self.occurrence_count / FIXED_BATCH_SIZE)


@dataclass(frozen=True)
class SemanticContract:
    """Versioned prompt/schema and deterministic interpretation callbacks."""

    contract_version: str
    prompt_version: str
    transform_version: str
    output_schema: Mapping[str, Any]
    compose_prompt: Callable[[Sequence[str]], str]
    normalize_batch: Callable[[Sequence[Mapping[str, Any]], Sequence[str]], Sequence[Mapping[str, Any]]]
    derive_hero_decision: Callable[[Mapping[str, Any]], tuple[str, Sequence[str]]]
    derive_coverage_slots: Callable[[Mapping[str, Any]], Sequence[str]]
    coverage_slots: Sequence[str]

    @property
    def output_schema_sha256(self) -> str:
        return hashlib.sha256(canonical_json(self.output_schema).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class N10V12RunResult:
    output_db: Path
    report_path: Path
    status: str
    logical_sha256: str
    physical_http_reservations: int
    model_call_reservations: int
    resumed: bool
    already_complete: bool
    metrics: Mapping[str, Any]


def _schema(contract: PopulationContract) -> str:
    schema = LEGACY_SCHEMA
    replacements = (
        ("PRAGMA user_version=1;", f"PRAGMA user_version={SCHEMA_VERSION};"),
        ("CHECK(occurrence_count=57)", f"CHECK(occurrence_count={contract.occurrence_count})"),
        (
            "  cli_version TEXT,\n  started_at TEXT NOT NULL,",
            "  cli_version TEXT,\n"
            "  schema_sql_sha256 TEXT NOT NULL CHECK(length(schema_sql_sha256)=64),\n"
            "  schema_objects_sha256 TEXT NOT NULL CHECK(length(schema_objects_sha256)=64),\n"
            "  manifest_domain TEXT NOT NULL,\n"
            "  ordered_fetch_url_manifest_sha256 TEXT NOT NULL CHECK(length(ordered_fetch_url_manifest_sha256)=64),\n"
            "  ordered_e1_pixel_manifest_sha256 TEXT NOT NULL CHECK(length(ordered_e1_pixel_manifest_sha256)=64),\n"
            "  inference_id_prefix TEXT NOT NULL,\n"
            "  max_model_call_reservations INTEGER NOT NULL CHECK(max_model_call_reservations>0),\n"
            "  observed_input_token_stop_threshold INTEGER NOT NULL CHECK(observed_input_token_stop_threshold>0),\n"
            "  observed_output_token_stop_threshold INTEGER NOT NULL CHECK(observed_output_token_stop_threshold>0),\n"
            "  observed_total_token_stop_threshold INTEGER NOT NULL CHECK(observed_total_token_stop_threshold>0),\n"
            "  prior_run_names_json TEXT NOT NULL CHECK(json_valid(prior_run_names_json)),\n"
            "  prior_successful_derivative_count INTEGER NOT NULL CHECK(prior_successful_derivative_count>=0),\n"
            "  publication_contract_version TEXT NOT NULL,\n"
            "  started_at TEXT NOT NULL,",
        ),
    )
    for old, new in replacements:
        if schema.count(old) != 1:
            raise N10V12Error(f"legacy schema template changed unexpectedly: {old}")
        schema = schema.replace(old, new, 1)
    return schema + """

CREATE TABLE external_call_reservations(
  reservation_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES semantic_runs(run_id),
  call_kind TEXT NOT NULL CHECK(call_kind IN ('http','vision')),
  logical_key TEXT NOT NULL CHECK(length(trim(logical_key))>0),
  inference_id TEXT,
  batch_no INTEGER CHECK(batch_no IS NULL OR batch_no>=1),
  attempt_no INTEGER NOT NULL CHECK(attempt_no>=1),
  hop_no INTEGER CHECK(hop_no IS NULL OR hop_no>=1),
  request_json TEXT NOT NULL CHECK(json_valid(request_json)),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  reserved_at TEXT NOT NULL,
  UNIQUE(run_id,call_kind,logical_key),
  CHECK((call_kind='http' AND inference_id IS NOT NULL AND batch_no IS NULL AND hop_no IS NOT NULL)
     OR (call_kind='vision' AND inference_id IS NULL AND batch_no IS NOT NULL AND hop_no IS NULL))
);
CREATE TABLE external_call_completions(
  reservation_id INTEGER PRIMARY KEY REFERENCES external_call_reservations(reservation_id),
  outcome TEXT NOT NULL CHECK(outcome IN ('response_received','completed','exception','interrupted_unknown')),
  http_status INTEGER CHECK(http_status IS NULL OR http_status BETWEEN 100 AND 599),
  elapsed_ms INTEGER NOT NULL CHECK(elapsed_ms>=0),
  error_kind TEXT,
  error_message TEXT,
  completed_at TEXT NOT NULL
);
CREATE TABLE runtime_events(
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES semantic_runs(run_id),
  event_type TEXT NOT NULL CHECK(length(trim(event_type))>0),
  detail_json TEXT NOT NULL CHECK(json_valid(detail_json)),
  created_at TEXT NOT NULL
);
ALTER TABLE fetch_attempts ADD COLUMN reservation_ids_json TEXT CHECK(reservation_ids_json IS NULL OR json_valid(reservation_ids_json));
ALTER TABLE vision_attempts ADD COLUMN reservation_id INTEGER REFERENCES external_call_reservations(reservation_id);

CREATE TRIGGER semantic_runs_v12_provenance_immutable BEFORE UPDATE OF
  schema_sql_sha256,schema_objects_sha256,manifest_domain,ordered_fetch_url_manifest_sha256,ordered_e1_pixel_manifest_sha256,
  inference_id_prefix,max_model_call_reservations,
  observed_input_token_stop_threshold,observed_output_token_stop_threshold,
  observed_total_token_stop_threshold,prior_run_names_json,
  prior_successful_derivative_count,publication_contract_version
ON semantic_runs BEGIN SELECT RAISE(ABORT,'v1.2 run provenance is immutable'); END;
CREATE TRIGGER reservations_insert BEFORE INSERT ON external_call_reservations
WHEN (SELECT status FROM semantic_runs WHERE run_id=NEW.run_id)<>'running'
BEGIN SELECT RAISE(ABORT,'external call reservation requires running run'); END;
CREATE TRIGGER reservations_model_cap BEFORE INSERT ON external_call_reservations
WHEN NEW.call_kind='vision' AND
 (SELECT COUNT(*) FROM external_call_reservations WHERE run_id=NEW.run_id AND call_kind='vision') >=
 (SELECT max_model_call_reservations FROM semantic_runs WHERE run_id=NEW.run_id)
BEGIN SELECT RAISE(ABORT,'model call reservation cap exhausted'); END;
CREATE TRIGGER reservations_no_update BEFORE UPDATE ON external_call_reservations
BEGIN SELECT RAISE(ABORT,'external call reservations are append-only'); END;
CREATE TRIGGER reservations_no_delete BEFORE DELETE ON external_call_reservations
BEGIN SELECT RAISE(ABORT,'external call reservations are append-only'); END;
CREATE TRIGGER completions_insert BEFORE INSERT ON external_call_completions
WHEN (SELECT status FROM semantic_runs r JOIN external_call_reservations x ON x.run_id=r.run_id
      WHERE x.reservation_id=NEW.reservation_id)<>'running'
BEGIN SELECT RAISE(ABORT,'external call completion requires running run'); END;
CREATE TRIGGER completions_no_update BEFORE UPDATE ON external_call_completions
BEGIN SELECT RAISE(ABORT,'external call completions are append-only'); END;
CREATE TRIGGER completions_no_delete BEFORE DELETE ON external_call_completions
BEGIN SELECT RAISE(ABORT,'external call completions are append-only'); END;
CREATE TRIGGER runtime_events_insert BEFORE INSERT ON runtime_events
WHEN (SELECT status FROM semantic_runs WHERE run_id=NEW.run_id)<>'running'
BEGIN SELECT RAISE(ABORT,'runtime event requires running run'); END;
CREATE TRIGGER runtime_events_no_update BEFORE UPDATE ON runtime_events
BEGIN SELECT RAISE(ABORT,'runtime events are append-only'); END;
CREATE TRIGGER runtime_events_no_delete BEFORE DELETE ON runtime_events
BEGIN SELECT RAISE(ABORT,'runtime events are append-only'); END;
CREATE TRIGGER vision_attempt_reservation BEFORE INSERT ON vision_attempts
WHEN NEW.reservation_id IS NULL OR NOT EXISTS(
  SELECT 1 FROM external_call_reservations r JOIN external_call_completions c USING(reservation_id)
  WHERE r.reservation_id=NEW.reservation_id AND r.run_id=NEW.run_id AND r.call_kind='vision'
    AND r.batch_no=NEW.batch_no AND r.attempt_no=NEW.attempt_no)
BEGIN SELECT RAISE(ABORT,'Vision attempt requires matching completed reservation'); END;
"""


def schema_sql_sha256(contract: PopulationContract) -> str:
    return hashlib.sha256(_schema(contract).encode("utf-8")).hexdigest()


def schema_objects_sha256(connection: sqlite3.Connection) -> str:
    """Digest every persisted, non-internal schema object and its exact SQL."""

    digest = hashlib.sha256()
    digest.update(b"cross-source-semantic-vision-n10-v1.2-schema-objects-v1\n")
    for row in connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ):
        digest.update(
            canonical_json(
                {"type": row[0], "name": row[1], "tbl_name": row[2], "sql": row[3]}
            ).encode("utf-8")
            + b"\n"
        )
    return digest.hexdigest()


def schema_objects_sha256_for_contract(contract: PopulationContract) -> str:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(_schema(contract))
        return schema_objects_sha256(connection)
    finally:
        connection.close()


def _sidecars(path: Path) -> tuple[Path, ...]:
    return tuple(
        candidate
        for candidate in (
            Path(str(path) + "-wal"),
            Path(str(path) + "-shm"),
            Path(str(path) + "-journal"),
        )
        if candidate.exists()
    )


def _load_manifest(path: Path, contract: PopulationContract) -> tuple[dict[str, Any], bytes]:
    if not path.is_file():
        raise FileNotFoundError(path)
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != contract.manifest_byte_sha256:
        raise N10V12Error("v1.2 manifest byte SHA mismatch")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise N10V12Error("v1.2 manifest is not UTF-8 JSON") from exc
    if not isinstance(payload, dict) or raw != (canonical_json(payload) + "\n").encode("utf-8"):
        raise N10V12Error("v1.2 manifest is not canonical JSON plus LF")
    body = dict(payload)
    stored = body.pop("semantic_coverage_manifest_sha256", None)
    replayed = canonical_sha256({"domain": contract.manifest_domain, "manifest": body})
    identities = {
        "self": (stored, contract.manifest_self_sha256),
        "replayed_self": (replayed, contract.manifest_self_sha256),
        "seed": (payload.get("sample_seed"), contract.sample_seed),
        "building_count": (payload.get("sample_size_buildings"), FIXED_BUILDING_COUNT),
        "occurrence_count": (payload.get("planned_occurrence_count"), contract.occurrence_count),
        "ordered_buildings": (payload.get("ordered_building_manifest_sha256"), contract.ordered_building_manifest_sha256),
        "ordered_occurrences": (payload.get("ordered_occurrence_manifest_sha256"), contract.ordered_occurrence_manifest_sha256),
        "ordered_urls": (payload.get("ordered_fetch_url_manifest_sha256"), contract.ordered_fetch_url_manifest_sha256),
        "ordered_pixels": (payload.get("ordered_e1_pixel_manifest_sha256"), contract.ordered_e1_pixel_manifest_sha256),
    }
    bad = {name: {"actual": actual, "expected": expected} for name, (actual, expected) in identities.items() if actual != expected}
    if bad:
        raise N10V12Error("v1.2 manifest identity mismatch: " + canonical_json(bad))
    return payload, raw


def _manifest_rows(
    payload: Mapping[str, Any], contract: PopulationContract
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    buildings: list[dict[str, Any]] = []
    occurrences: list[dict[str, Any]] = []
    try:
        wrappers = payload["selected_buildings"]
        for building_rank, wrapper in enumerate(wrappers, 1):
            selected = wrapper["selected_building"]
            building = selected["building"]
            plan = selected["coverage_plan"]
            buildings.append(
                {
                    "building_rank": building_rank,
                    "selection_id": building["selection_id"],
                    "source": building["source"],
                    "source_building_id": building["source_building_id"],
                    "population_stratum": building["population_stratum"],
                    # The v1.2 planner replaced legacy one-guard-per-row
                    # selection with two explicit lanes.  The inherited table
                    # column keeps its historical name, but stores the exact
                    # lane value rather than inventing a guard.
                    "guard_name": selected["lane"],
                    "qa_fallback": int(bool(building["qa_fallback"])),
                    "building_record_sha256": selected["building_record_sha256"],
                    # Likewise this inherited column contains the v1.2
                    # BuildingCoveragePlan record SHA exposed by the planner.
                    "coverage_plan_record_sha256": selected["coverage_record_sha256"],
                    "selected_building_record_sha256": wrapper["selected_building_record_sha256"],
                    "manifest_json": canonical_json(wrapper),
                }
            )
            for occurrence_wrapper in plan["selected_occurrences"]:
                occurrence = occurrence_wrapper["occurrence"]
                candidate = occurrence["candidate"]
                rank = len(occurrences) + 1
                occurrences.append(
                    {
                        "input_rank": rank,
                        "inference_id": f"{contract.inference_id_prefix}{rank:06d}",
                        "selection_id": building["selection_id"],
                        "occurrence_rank": occurrence["occurrence_rank"],
                        "source": candidate["source"],
                        "source_building_id": candidate["source_building_id"],
                        "source_asset_id": candidate["source_asset_id"],
                        "candidate_id": candidate["candidate_id"],
                        "fetch_url": candidate["fetch_url"],
                        "expected_response_sha256": candidate["raw_response_sha256"],
                        "expected_e1_pixel_sha256": candidate["normalized_pixel_sha256"],
                        "expected_e1_width": candidate["normalized_width"],
                        "expected_e1_height": candidate["normalized_height"],
                        "e2_asset_record_sha256": candidate["e2_asset_record_sha256"],
                        "e2_relation_record_sha256": candidate["e2_building_relation_record_sha256"],
                        "e3_candidate_record_sha256": candidate["e3_candidate_record_sha256"],
                        "e3_ranking_record_sha256": candidate["e3_ranking_record_sha256"],
                        "e3_shortlist_record_sha256": candidate["e3_shortlist_item_record_sha256"],
                        "occurrence_record_sha256": occurrence_wrapper["occurrence_record_sha256"],
                        "manifest_json": canonical_json(occurrence_wrapper),
                    }
                )
    except (KeyError, TypeError) as exc:
        raise N10V12Error("v1.2 manifest row structure is malformed") from exc
    if len(buildings) != FIXED_BUILDING_COUNT or len(occurrences) != contract.occurrence_count:
        raise N10V12Error("v1.2 decoded manifest accounting mismatch")
    if len({row["fetch_url"] for row in occurrences}) != len(occurrences):
        raise N10V12Error("v1.2 manifest contains duplicate fetch URLs")
    if len({row["expected_e1_pixel_sha256"] for row in occurrences}) != len(occurrences):
        raise N10V12Error("v1.2 manifest contains duplicate E1 pixels")
    for row in occurrences:
        parsed = urlsplit(str(row["fetch_url"]))
        if parsed.scheme.casefold() != "https" or parsed.hostname != SOURCE_HOSTS.get(str(row["source"])):
            raise N10V12Error(f"v1.2 fetch host mismatch: {row['inference_id']}")
    return buildings, occurrences


def _prior_successful_derivative_pixels(
    payload: Mapping[str, Any], contract: PopulationContract
) -> frozenset[str]:
    """Return the manifest-bound successful derivative exclusions.

    The planner combines every immutable prior semantic-run ledger named by
    the population contract.  Failed ``source_changed`` inputs intentionally
    have no accepted derivative and therefore contribute expected-E1 identity
    only, not a fabricated runtime derivative.
    """

    try:
        exclusions = payload["exclusions"]
        records = exclusions["records"]["occurrence_records"]
        run_inputs = exclusions["prior_run_inputs"]
        declared_count = int(exclusions["prior_successful_derivative_pixel_count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise N10V12Error("v1.2 manifest lacks prior derivative exclusion ledger") from exc
    if not isinstance(records, list) or not isinstance(run_inputs, list):
        raise N10V12Error("v1.2 prior derivative exclusion ledger is malformed")
    prior_names = {
        str(row.get("name")) for row in run_inputs if isinstance(row, Mapping)
    }
    expected_names = set(contract.prior_run_names)
    if prior_names != expected_names or len(run_inputs) != len(expected_names):
        raise N10V12Error(
            "v1.2 runtime prior semantic ledger set mismatch: "
            + canonical_json(
                {"actual": sorted(prior_names), "expected": sorted(expected_names)}
            )
        )
    values: set[str] = set()
    for row in records:
        if not isinstance(row, Mapping):
            raise N10V12Error("v1.2 prior occurrence exclusion row is malformed")
        value = row.get("derivative_pixel_sha256")
        if value is None:
            continue
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise N10V12Error("v1.2 prior derivative exclusion SHA is malformed")
        values.add(value)
    if (
        len(values) != declared_count
        or declared_count != contract.prior_successful_derivative_count
    ):
        raise N10V12Error(
            "v1.2 prior derivative exclusion accounting mismatch: "
            + canonical_json(
                {
                    "actual_unique": len(values),
                    "declared": declared_count,
                    "expected": contract.prior_successful_derivative_count,
                }
            )
        )
    return frozenset(values)


def _verify_input(path: Path, record: Mapping[str, Any]) -> str:
    if not path.is_file() or path.stat().st_size != int(record["size_bytes"]):
        raise N10V12Error(f"input path or size mismatch: {path}")
    if _sidecars(path):
        raise N10V12Error(f"input SQLite sidecars present: {_sidecars(path)}")
    actual = file_sha256(path)
    if actual != record["byte_sha256"]:
        raise N10V12Error(f"input SHA mismatch: {path}")
    return actual


def _dependency_manifest(semantics: SemanticContract) -> dict[str, Any]:
    return {
        "e1_contract": FINGERPRINT_CONTRACT_VERSION,
        "fetch_contract": FETCH_CONTRACT_VERSION,
        "semantic_contract": semantics.contract_version,
        "semantic_prompt": semantics.prompt_version,
        "transform": semantics.transform_version,
        "publication_contract": PUBLICATION_CONTRACT_VERSION,
    }


def _cache_provenance(path: Path, retained_for_review: bool) -> dict[str, Any]:
    return {
        "path": str(path.resolve()),
        "retained_for_review": bool(retained_for_review),
        "write_contract": "atomic-exclusive-sha256-v1",
    }


def _ensure_schema(connection: sqlite3.Connection, contract: PopulationContract) -> None:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if not tables:
        connection.executescript(_schema(contract))
        connection.commit()
    required = {
        "semantic_runs",
        "selected_buildings",
        "selected_occurrences",
        "vision_inputs",
        "fetch_attempts",
        "vision_attempts",
        "vision_attempt_payloads",
        "semantic_results",
        "occurrence_result_links",
        "hero_candidate_decisions",
        "coverage_slot_assignments",
        "validations",
        "external_call_reservations",
        "external_call_completions",
        "runtime_events",
    }
    actual = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if not required.issubset(actual):
        raise N10V12Error(f"v1.2 partial schema is incomplete: {sorted(required - actual)}")
    if int(connection.execute("PRAGMA application_id").fetchone()[0]) != APPLICATION_ID:
        raise N10V12Error("v1.2 partial application_id mismatch")
    if int(connection.execute("PRAGMA user_version").fetchone()[0]) != SCHEMA_VERSION:
        raise N10V12Error("v1.2 partial schema version mismatch")


def _initialize(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    manifest_path: Path,
    manifest_raw: bytes,
    payload: Mapping[str, Any],
    population: PopulationContract,
    semantics: SemanticContract,
    e2_path: Path,
    e3_path: Path,
    model: str,
    reasoning: str,
    service_tier: str,
    cli_version: str | None,
    cache_dir: Path,
    retained_for_review: bool,
) -> None:
    _ensure_schema(connection, population)
    buildings, occurrences = _manifest_rows(payload, population)
    dependency = _dependency_manifest(semantics)
    existing = connection.execute("SELECT * FROM semantic_runs").fetchall()
    if len(existing) > 1:
        raise N10V12Error("v1.2 sidecar contains multiple runs")
    if existing:
        run = existing[0]
        if run["run_id"] != run_id or run["status"] != "initializing":
            raise N10V12Error("initialization recovery requires its exact initializing run")
    else:
        values = {
            "run_id": run_id,
            "status": "initializing",
            "runner_version": RUNNER_VERSION,
            "schema_version": SCHEMA_VERSION,
            "manifest_path": str(manifest_path),
            "manifest_size": len(manifest_raw),
            "manifest_byte_sha256": population.manifest_byte_sha256,
            "manifest_self_sha256": population.manifest_self_sha256,
            "ordered_building_manifest_sha256": population.ordered_building_manifest_sha256,
            "ordered_occurrence_manifest_sha256": population.ordered_occurrence_manifest_sha256,
            "sample_seed": population.sample_seed,
            "building_count": FIXED_BUILDING_COUNT,
            "occurrence_count": population.occurrence_count,
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
            "contract_version": semantics.contract_version,
            "prompt_version": semantics.prompt_version,
            "output_schema_sha256": semantics.output_schema_sha256,
            "transform_version": semantics.transform_version,
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
            "schema_sql_sha256": schema_sql_sha256(population),
            "schema_objects_sha256": schema_objects_sha256(connection),
            "manifest_domain": population.manifest_domain,
            "ordered_fetch_url_manifest_sha256": population.ordered_fetch_url_manifest_sha256,
            "ordered_e1_pixel_manifest_sha256": population.ordered_e1_pixel_manifest_sha256,
            "inference_id_prefix": population.inference_id_prefix,
            "max_model_call_reservations": population.max_model_call_reservations,
            "observed_input_token_stop_threshold": population.observed_input_token_stop_threshold,
            "observed_output_token_stop_threshold": population.observed_output_token_stop_threshold,
            "observed_total_token_stop_threshold": population.observed_total_token_stop_threshold,
            "prior_run_names_json": canonical_json(sorted(population.prior_run_names)),
            "prior_successful_derivative_count": population.prior_successful_derivative_count,
            "publication_contract_version": PUBLICATION_CONTRACT_VERSION,
            "started_at": utc_now(),
            "metrics_json": canonical_json(
                {
                    "runtime_provenance": {
                        "cache": _cache_provenance(cache_dir, retained_for_review),
                        "schema_sql_sha256": schema_sql_sha256(population),
                    }
                }
            ),
        }
        columns = tuple(values)
        connection.execute(
            f"INSERT INTO semantic_runs({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
            tuple(values[name] for name in columns),
        )
    connection.executemany(
        "INSERT OR IGNORE INTO selected_buildings VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
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
        "INSERT OR IGNORE INTO selected_occurrences VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
        "INSERT OR IGNORE INTO vision_inputs(run_id,inference_id,status) VALUES(?,?,'pending')",
        [(run_id, row["inference_id"]) for row in occurrences],
    )
    # ``INSERT OR IGNORE`` is needed so a crash during initialization can be
    # resumed.  It must not, however, turn a conflicting partial row into an
    # accepted row merely because the aggregate counts happen to match.
    stored_buildings = [
        dict(row)
        for row in connection.execute(
            "SELECT building_rank,selection_id,source,source_building_id,"
            "population_stratum,guard_name,qa_fallback,building_record_sha256,"
            "coverage_plan_record_sha256,selected_building_record_sha256,manifest_json "
            "FROM selected_buildings WHERE run_id=? ORDER BY building_rank",
            (run_id,),
        )
    ]
    stored_occurrences = [
        dict(row)
        for row in connection.execute(
            "SELECT input_rank,inference_id,selection_id,occurrence_rank,source,"
            "source_building_id,source_asset_id,candidate_id,fetch_url,"
            "expected_response_sha256,expected_e1_pixel_sha256,expected_e1_width,"
            "expected_e1_height,e2_asset_record_sha256,e2_relation_record_sha256,"
            "e3_candidate_record_sha256,e3_ranking_record_sha256,"
            "e3_shortlist_record_sha256,occurrence_record_sha256,manifest_json "
            "FROM selected_occurrences WHERE run_id=? ORDER BY input_rank",
            (run_id,),
        )
    ]
    if stored_buildings != buildings or stored_occurrences != occurrences:
        raise N10V12Error("initialization recovery rows differ from pinned manifest")
    stored_inputs = [
        (str(row[0]), str(row[1]))
        for row in connection.execute(
            "SELECT inference_id,status FROM vision_inputs WHERE run_id=? ORDER BY inference_id",
            (run_id,),
        )
    ]
    expected_inputs = sorted((str(row["inference_id"]), "pending") for row in occurrences)
    if stored_inputs != expected_inputs:
        raise N10V12Error("initialization recovery input state differs from pinned manifest")
    counts = (
        int(connection.execute("SELECT COUNT(*) FROM selected_buildings").fetchone()[0]),
        int(connection.execute("SELECT COUNT(*) FROM selected_occurrences").fetchone()[0]),
        int(connection.execute("SELECT COUNT(*) FROM vision_inputs").fetchone()[0]),
    )
    if counts != (FIXED_BUILDING_COUNT, population.occurrence_count, population.occurrence_count):
        raise N10V12Error(f"initialization accounting mismatch: {counts}")
    connection.execute("UPDATE semantic_runs SET status='running' WHERE run_id=?", (run_id,))
    connection.commit()


def _record_event(
    connection: sqlite3.Connection, run_id: str, event_type: str, detail: Mapping[str, Any]
) -> None:
    connection.execute(
        "INSERT INTO runtime_events(run_id,event_type,detail_json,created_at) VALUES(?,?,?,?)",
        (run_id, event_type, canonical_json(dict(detail)), utc_now()),
    )


def _pause(
    connection: sqlite3.Connection, run_id: str, reason: str, detail: Mapping[str, Any]
) -> None:
    _record_event(connection, run_id, reason, detail)
    connection.commit()
    raise N10V12Paused(reason, detail)


def _reserve_call(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    kind: str,
    logical_key: str,
    inference_id: str | None,
    batch_no: int | None,
    attempt_no: int,
    hop_no: int | None,
    request: Mapping[str, Any],
) -> int:
    request_json = canonical_json(dict(request))
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            """
            INSERT INTO external_call_reservations(
              run_id,call_kind,logical_key,inference_id,batch_no,attempt_no,hop_no,
              request_json,request_sha256,reserved_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                kind,
                logical_key,
                inference_id,
                batch_no,
                attempt_no,
                hop_no,
                request_json,
                hashlib.sha256(request_json.encode("utf-8")).hexdigest(),
                utc_now(),
            ),
        )
        reservation_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        connection.commit()
        return reservation_id
    except BaseException:
        connection.rollback()
        raise


def _complete_call(
    connection: sqlite3.Connection,
    *,
    reservation_id: int,
    outcome: str,
    elapsed_ms: int,
    http_status: int | None = None,
    error_kind: str | None = None,
    error_message: str | None = None,
    commit: bool = True,
) -> None:
    connection.execute(
        """
        INSERT INTO external_call_completions(
          reservation_id,outcome,http_status,elapsed_ms,error_kind,error_message,completed_at)
        VALUES(?,?,?,?,?,?,?)
        """,
        (
            reservation_id,
            outcome,
            http_status,
            max(0, int(elapsed_ms)),
            error_kind,
            (error_message or "")[:2000] or None,
            utc_now(),
        ),
    )
    if commit:
        connection.commit()


class _HttpCallbacks:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        row: sqlite3.Row,
        attempt_no: int,
        limiter: _GlobalRequestRateLimiter,
    ) -> None:
        self.connection = connection
        self.run_id = run_id
        self.row = row
        self.attempt_no = attempt_no
        self.limiter = limiter
        self.reservation_ids: list[int] = []

    def reserve(self, request_url: str, hop_no: int) -> int:
        if hop_no != len(self.reservation_ids) + 1:
            raise N10V12Error("physical HTTP hop sequence changed")
        self.limiter.wait()
        request = {
            "attempt_no": self.attempt_no,
            "hop_no": hop_no,
            "inference_id": str(self.row["inference_id"]),
            "request_url": request_url,
            "source": str(self.row["source"]),
        }
        reservation_id = _reserve_call(
            self.connection,
            run_id=self.run_id,
            kind="http",
            logical_key=f"fetch:{self.row['inference_id']}:{self.attempt_no}:{hop_no}",
            inference_id=str(self.row["inference_id"]),
            batch_no=None,
            attempt_no=self.attempt_no,
            hop_no=hop_no,
            request=request,
        )
        self.reservation_ids.append(reservation_id)
        return reservation_id

    def complete(self, completion: PhysicalRequestCompletion) -> None:
        _complete_call(
            self.connection,
            reservation_id=completion.reservation_id,
            outcome=completion.outcome,
            http_status=completion.http_status,
            elapsed_ms=round(completion.elapsed_seconds * 1000),
            error_kind=completion.error_kind,
            error_message=completion.error_message,
        )


def _insert_fetch_attempt(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    row: Mapping[str, Any],
    attempt_no: int,
    started_at: str,
    elapsed_ms: int,
    outcome: str,
    reservation_ids: Sequence[int],
    payload: FetchPayload | None = None,
    failure: FetchFailure | None = None,
    actual_response_sha256: str | None = None,
    actual_e1_pixel_sha256: str | None = None,
    retryable: bool = False,
    retry_after: float | None = None,
    delay: float | None = None,
    error_kind: str | None = None,
    error_message: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO fetch_attempts(
          run_id,inference_id,attempt_no,request_url,final_url,started_at,
          completed_at,elapsed_ms,outcome,http_status,content_type,response_bytes,
          expected_response_sha256,actual_response_sha256,
          expected_e1_pixel_sha256,actual_e1_pixel_sha256,retryable,
          retry_after_seconds,scheduled_delay_seconds,error_kind,error_message,
          reservation_ids_json)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            row["inference_id"],
            attempt_no,
            row["fetch_url"],
            payload.final_url if payload else (failure.final_url if failure else None),
            started_at,
            utc_now(),
            max(0, int(elapsed_ms)),
            outcome,
            payload.http_status if payload else (failure.http_status if failure else None),
            payload.content_type if payload else (failure.content_type if failure else None),
            len(payload.body) if payload else (failure.response_bytes if failure else None),
            row["expected_response_sha256"],
            actual_response_sha256,
            row["expected_e1_pixel_sha256"],
            actual_e1_pixel_sha256,
            int(retryable),
            retry_after,
            delay,
            error_kind,
            (error_message or "")[:2000] or None,
            canonical_json(list(reservation_ids)),
        ),
    )


def _load_cached(cache_dir: Path, state: sqlite3.Row) -> bytes:
    path = cache_dir / f"{state['inference_id']}.jpg"
    if not path.is_file():
        raise N10V12Error(f"durable v1.2 derivative is missing: {state['inference_id']}")
    raw = path.read_bytes()
    actual = _derivative_identity(raw)
    expected = (
        state["derivative_encoded_sha256"],
        state["derivative_pixel_sha256"],
        state["derivative_width"],
        state["derivative_height"],
        state["derivative_bytes"],
    )
    if actual != expected:
        raise N10V12Error(f"durable v1.2 derivative identity mismatch: {state['inference_id']}")
    return raw


def _close_missing_completions(
    connection: sqlite3.Connection, reservation_ids: Sequence[int]
) -> None:
    for reservation_id in reservation_ids:
        exists = connection.execute(
            "SELECT 1 FROM external_call_completions WHERE reservation_id=?",
            (reservation_id,),
        ).fetchone()
        if exists is None:
            _complete_call(
                connection,
                reservation_id=reservation_id,
                outcome="interrupted_unknown",
                elapsed_ms=0,
                error_kind="interrupted_unknown",
                error_message="external call outcome was not durably published",
                commit=False,
            )


def _prepare_input(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    row: sqlite3.Row,
    population: PopulationContract,
    cache_dir: Path,
    fetcher: Callable[..., FetchPayload],
    limiter: _GlobalRequestRateLimiter,
    sleeper: Callable[[float], None],
    clock: Callable[[], float],
    circuit: list[int],
) -> bytes | None:
    state = connection.execute(
        "SELECT * FROM vision_inputs WHERE run_id=? AND inference_id=?",
        (run_id, row["inference_id"]),
    ).fetchone()
    if state is None:
        raise N10V12Error(f"missing v1.2 Vision input: {row['inference_id']}")
    if state["status"] == "ready":
        return _load_cached(cache_dir, state)
    if state["status"] != "pending":
        return None
    orphan = cache_dir / f"{row['inference_id']}.jpg"
    if orphan.exists():
        orphan.unlink()
    attempt_no = int(
        connection.execute(
            "SELECT COALESCE(MAX(attempt_no),0)+1 FROM fetch_attempts WHERE run_id=? AND inference_id=?",
            (run_id, row["inference_id"]),
        ).fetchone()[0]
    )
    while attempt_no <= FIXED_FETCH_ATTEMPTS:
        started_at = utc_now()
        started = clock()
        callbacks = _HttpCallbacks(
            connection,
            run_id=run_id,
            row=row,
            attempt_no=attempt_no,
            limiter=limiter,
        )
        payload: FetchPayload | None = None
        actual_raw: str | None = None
        actual_e1: str | None = None
        try:
            payload = fetcher(
                str(row["source"]),
                str(row["fetch_url"]),
                callbacks=callbacks,
                max_redirects=population.max_redirects,
                clock=clock,
            )
            actual_raw = payload.raw_response_sha256
            actual_e1 = fingerprint_bytes(payload.body).pixel_sha256
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
                    reservation_ids=callbacks.reservation_ids,
                    payload=payload,
                    actual_response_sha256=actual_raw,
                    actual_e1_pixel_sha256=actual_e1,
                    error_kind=outcome,
                    error_message="frozen response and E1 identities did not match exactly",
                )
                connection.execute(
                    "UPDATE vision_inputs SET status='fetch_failed',completed_at=?,error_kind=?,error_message=? WHERE run_id=? AND inference_id=?",
                    (utc_now(), outcome, "frozen response and E1 identities did not match exactly", run_id, row["inference_id"]),
                )
                connection.commit()
                circuit[0] = 0
                return None
            decoded = decode_source(payload.body)
            derivative = prepare_derivative(decoded, "long1024", 1024)
            _atomic_write_bytes(orphan, derivative.encoded_bytes)
            _insert_fetch_attempt(
                connection,
                run_id=run_id,
                row=row,
                attempt_no=attempt_no,
                started_at=started_at,
                elapsed_ms=elapsed,
                outcome="exact_match",
                reservation_ids=callbacks.reservation_ids,
                payload=payload,
                actual_response_sha256=actual_raw,
                actual_e1_pixel_sha256=actual_e1,
            )
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
            circuit[0] = 0
            return derivative.encoded_bytes
        except FetchFailure as exc:
            can_retry = bool(exc.retryable) and attempt_no < FIXED_FETCH_ATTEMPTS
            delay = (
                max(float(exc.retry_after_seconds or 0), min(60.0, float(2 ** (attempt_no - 1))))
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
            _close_missing_completions(connection, callbacks.reservation_ids)
            _insert_fetch_attempt(
                connection,
                run_id=run_id,
                row=row,
                attempt_no=attempt_no,
                started_at=started_at,
                elapsed_ms=round(exc.elapsed_seconds * 1000),
                outcome=outcome,
                reservation_ids=callbacks.reservation_ids,
                failure=exc,
                actual_response_sha256=exc.raw_response_sha256,
                retryable=can_retry,
                retry_after=exc.retry_after_seconds,
                delay=delay,
                error_kind=kind,
                error_message=str(exc),
            )
            if not can_retry:
                connection.execute(
                    "UPDATE vision_inputs SET status='fetch_failed',completed_at=?,error_kind=?,error_message=? WHERE run_id=? AND inference_id=?",
                    (utc_now(), kind, str(exc)[:2000], run_id, row["inference_id"]),
                )
            connection.commit()
            circuit[0] = circuit[0] + 1 if exc.retryable else 0
            if circuit[0] >= HTTP_CIRCUIT_THRESHOLD:
                _pause(
                    connection,
                    run_id,
                    "http_circuit_open",
                    {"consecutive_retryable_failures": circuit[0], "inference_id": row["inference_id"]},
                )
            if not can_retry:
                return None
            sleeper(float(delay or 0))
            attempt_no += 1
        except N10V12Paused:
            raise
        except Exception as exc:
            _close_missing_completions(connection, callbacks.reservation_ids)
            _insert_fetch_attempt(
                connection,
                run_id=run_id,
                row=row,
                attempt_no=attempt_no,
                started_at=started_at,
                elapsed_ms=max(0, round((clock() - started) * 1000)),
                outcome="decode_failed" if payload is not None else "http_failed",
                reservation_ids=callbacks.reservation_ids,
                payload=payload,
                actual_response_sha256=actual_raw,
                actual_e1_pixel_sha256=actual_e1,
                error_kind=type(exc).__name__,
                error_message=str(exc),
            )
            connection.execute(
                "UPDATE vision_inputs SET status='fetch_failed',completed_at=?,error_kind=?,error_message=? WHERE run_id=? AND inference_id=?",
                (utc_now(), type(exc).__name__, str(exc)[:2000], run_id, row["inference_id"]),
            )
            connection.commit()
            return None
    return None


def _actual_derivative_exclusion_gate(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    population: PopulationContract,
    prior_derivative_pixels: frozenset[str],
) -> None:
    rows = list(
        connection.execute(
            "SELECT inference_id,status,derivative_pixel_sha256 FROM vision_inputs "
            "WHERE run_id=? ORDER BY inference_id",
            (run_id,),
        )
    )
    ready = [row for row in rows if row["status"] == "ready"]
    values = [str(row["derivative_pixel_sha256"]) for row in ready]
    collisions = sorted(set(values) & prior_derivative_pixels)
    duplicates = sorted(value for value in set(values) if values.count(value) > 1)
    if collisions or duplicates:
        raise N10V12Error(
            "v1.2 actual derivative exclusion gate failed: "
            + canonical_json(
                {
                    "duplicates_within_new_n10": duplicates,
                    "prior_n10_n100_derivative_collisions": collisions,
                }
            )
        )
    _record_event(
        connection,
        run_id,
        "actual_derivative_exclusion_gate",
        {
            "new_ready_derivative_count": len(values),
            "prior_successful_derivative_count": len(prior_derivative_pixels),
            "prior_run_names": sorted(population.prior_run_names),
            "vision_reservations_before_gate": int(
                connection.execute(
                    "SELECT COUNT(*) FROM external_call_reservations WHERE run_id=? AND call_kind='vision'",
                    (run_id,),
                ).fetchone()[0]
            ),
        },
    )
    if connection.execute(
        "SELECT COUNT(*) FROM external_call_reservations WHERE run_id=? AND call_kind='vision'",
        (run_id,),
    ).fetchone()[0]:
        raise N10V12Error("actual derivative exclusion gate ran after Vision reservation")
    connection.commit()


def _executor_exception_result(
    *,
    exc: Exception,
    prompt: str,
    image_paths: Sequence[Path],
    schema_path: Path,
    inference_ids: Sequence[str],
    codex_bin: Path,
    model: str,
    reasoning: str,
    service_tier: str,
    working_directory: Path,
    elapsed_seconds: float,
) -> VisionRuntimeResult:
    error_kind = type(exc).__name__
    error_message = str(exc) or error_kind
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
            output_schema_path=str(schema_path),
            output_schema_sha256=hashlib.sha256(schema_path.read_bytes()).hexdigest(),
            prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            image_paths=tuple(str(path) for path in image_paths),
            expected_asset_ids=tuple(inference_ids),
            timeout_seconds=600.0,
            command_without_prompt=("<executor raised before result>",),
        ),
    )


def _write_vision_attempt(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    batch_no: int,
    attempt_no: int,
    reservation_id: int,
    inference_ids: Sequence[str],
    result: VisionRuntimeResult,
    status: str,
    started_at: str,
    error_kind: str | None = None,
    error_message: str | None = None,
) -> int:
    stdout = _sanitize_capture(result.stdout, label="Vision stdout", max_bytes=MAX_STDOUT_CAPTURE_BYTES)
    stderr = _sanitize_capture(result.stderr, label="Vision stderr", max_bytes=MAX_STDERR_CAPTURE_BYTES)
    stored_error = _sanitize_capture(
        error_message or result.error_message,
        label="Vision error",
        max_bytes=2000,
    )
    usage = result.usage
    cli_version = connection.execute(
        "SELECT cli_version FROM semantic_runs WHERE run_id=?", (run_id,)
    ).fetchone()[0]
    connection.execute(
        """
        INSERT INTO vision_attempts(
          run_id,batch_no,attempt_no,inference_ids_json,status,model,reasoning,
          service_tier,runtime_version,cli_version,codex_bin,image_detail,sandbox,
          prompt_sha256,output_schema_sha256,started_at,completed_at,elapsed_ms,
          input_tokens,cached_input_tokens,output_tokens,raw_events_sha256,
          error_kind,error_message,reservation_id)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            batch_no,
            attempt_no,
            canonical_json(list(inference_ids)),
            status,
            result.provenance.model,
            result.provenance.reasoning,
            result.provenance.service_tier,
            result.provenance.runtime_version,
            cli_version,
            result.provenance.codex_bin,
            result.provenance.cli_image_detail,
            result.provenance.sandbox,
            result.provenance.prompt_sha256,
            result.provenance.output_schema_sha256,
            started_at,
            utc_now(),
            max(0, round(result.elapsed_seconds * 1000)),
            usage.input_tokens if usage else None,
            usage.cached_input_tokens if usage else None,
            usage.output_tokens if usage else None,
            hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
            error_kind or result.error_kind,
            stored_error or None,
            reservation_id,
        ),
    )
    attempt_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
    raw_stdout = stdout.encode("utf-8")
    raw_stderr = stderr.encode("utf-8")
    connection.execute(
        "INSERT INTO vision_attempt_payloads VALUES(?,?,?,?,?,?,?,?,?)",
        (
            attempt_id,
            "gzip",
            gzip.compress(raw_stdout, mtime=0),
            len(raw_stdout),
            hashlib.sha256(raw_stdout).hexdigest(),
            gzip.compress(raw_stderr, mtime=0),
            len(raw_stderr),
            hashlib.sha256(raw_stderr).hexdigest(),
            stderr[-8000:] or None,
        ),
    )
    return attempt_id


def _write_results(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    attempt_id: int,
    rows: Sequence[sqlite3.Row],
    raw_results: Sequence[Mapping[str, Any]],
    normalized: Sequence[Mapping[str, Any]],
    semantics: SemanticContract,
) -> None:
    for source_row, raw, result in zip(rows, raw_results, normalized, strict=True):
        result_body = {
            "attempt_id": attempt_id,
            "inference_id": source_row["inference_id"],
            "normalized": result,
            "raw": raw,
        }
        connection.execute(
            "INSERT INTO semantic_results VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                source_row["inference_id"],
                attempt_id,
                canonical_json(raw),
                canonical_json(result),
                int(result["in_scope"]),
                result["reject_reason"],
                result["medium"],
                result["spatial_context"],
                result["framing_scale"],
                result["camera_angle"],
                result["drawing_kind"],
                result["project_state"],
                result["project_legibility"],
                canonical_json(list(result["uncertain_axes"])),
                int(result["resolution_insufficient"]),
                result["evidence"],
                canonical_sha256(result_body),
            ),
        )
        derivative_sha = connection.execute(
            "SELECT derivative_pixel_sha256 FROM vision_inputs WHERE run_id=? AND inference_id=?",
            (run_id, source_row["inference_id"]),
        ).fetchone()[0]
        link = {
            "inference_id": source_row["inference_id"],
            "result_inference_id": source_row["inference_id"],
            "reuse_basis": "same_occurrence",
            "verified_input_pixel_sha256": derivative_sha,
        }
        connection.execute(
            "INSERT INTO occurrence_result_links VALUES(?,?,?,?,?,?)",
            (run_id, *link.values(), canonical_sha256(link)),
        )
        tier, reasons = semantics.derive_hero_decision(result)
        reasons = tuple(reasons)
        hero = {
            "inference_id": source_row["inference_id"],
            "tier": tier,
            "reasons": reasons,
            "authoritative": False,
        }
        connection.execute(
            "INSERT INTO hero_candidate_decisions VALUES(?,?,?,?,?,?)",
            (run_id, source_row["inference_id"], tier, canonical_json(reasons), 0, canonical_sha256(hero)),
        )
        for slot in semantics.derive_coverage_slots(result):
            rank = int(
                connection.execute(
                    "SELECT COUNT(*)+1 FROM coverage_slot_assignments WHERE run_id=? AND selection_id=? AND slot=? AND state='observed'",
                    (run_id, source_row["selection_id"], slot),
                ).fetchone()[0]
            )
            body = {
                "assignment_rank": rank,
                "inference_id": source_row["inference_id"],
                "selection_id": source_row["selection_id"],
                "slot": slot,
                "state": "observed",
            }
            connection.execute(
                "INSERT INTO coverage_slot_assignments VALUES(?,?,?,?,?,?,?)",
                (run_id, source_row["selection_id"], slot, rank, "observed", source_row["inference_id"], canonical_sha256(body)),
            )
        connection.execute(
            "UPDATE vision_inputs SET status='success',completed_at=?,error_kind=NULL,error_message=NULL WHERE run_id=? AND inference_id=?",
            (utc_now(), run_id, source_row["inference_id"]),
        )


def _observed_usage(connection: sqlite3.Connection, run_id: str) -> dict[str, int]:
    row = connection.execute(
        "SELECT COALESCE(SUM(input_tokens),0),COALESCE(SUM(cached_input_tokens),0),"
        "COALESCE(SUM(output_tokens),0),SUM(input_tokens IS NULL OR output_tokens IS NULL) "
        "FROM vision_attempts WHERE run_id=?",
        (run_id,),
    ).fetchone()
    return {
        "input_tokens": int(row[0]),
        "cached_input_tokens": int(row[1]),
        "output_tokens": int(row[2]),
        "unknown_usage_attempts": int(row[3] or 0),
    }


def _enforce_observed_token_stop(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    population: PopulationContract,
    before_new_call: bool,
) -> None:
    usage = _observed_usage(connection, run_id)
    total = usage["input_tokens"] + usage["output_tokens"]
    reached = (
        usage["input_tokens"] >= population.observed_input_token_stop_threshold
        or usage["output_tokens"] >= population.observed_output_token_stop_threshold
        or total >= population.observed_total_token_stop_threshold
        or usage["unknown_usage_attempts"] > 0
    )
    # This is truthfully an observed stop threshold, never a server-enforced
    # hard ceiling.  A completed call may cross it.  Unknown usage makes the
    # observed total a lower bound and therefore also stops new paid calls.
    if before_new_call and reached:
        _pause(
            connection,
            run_id,
            "observed_token_stop_threshold",
            {
                **usage,
                "observed_total_tokens": total,
                "thresholds": {
                    "input": population.observed_input_token_stop_threshold,
                    "output": population.observed_output_token_stop_threshold,
                    "total": population.observed_total_token_stop_threshold,
                },
                "semantics": "post-call observed lower-bound stop; not a hard pre-call cap",
            },
        )


def _reconcile_open_reservations(
    connection: sqlite3.Connection,
    *,
    run_id: str,
) -> dict[str, int]:
    """Close crash-open calls and consume their exact attempt/call budgets."""

    open_rows = list(
        connection.execute(
            """
            SELECT r.* FROM external_call_reservations r
            LEFT JOIN external_call_completions c USING(reservation_id)
            WHERE r.run_id=? AND c.reservation_id IS NULL ORDER BY r.reservation_id
            """,
            (run_id,),
        )
    )
    for reservation in open_rows:
        _complete_call(
            connection,
            reservation_id=int(reservation["reservation_id"]),
            outcome="interrupted_unknown",
            elapsed_ms=0,
            error_kind="interrupted_unknown",
            error_message="process ended after durable reservation but before durable completion",
            commit=False,
        )
    http_groups: dict[tuple[str, int], list[sqlite3.Row]] = {}
    for reservation in connection.execute(
        """
        SELECT r.* FROM external_call_reservations r
        WHERE r.run_id=? AND r.call_kind='http'
          AND NOT EXISTS(
            SELECT 1 FROM fetch_attempts f
            WHERE f.run_id=r.run_id AND f.inference_id=r.inference_id
              AND f.attempt_no=r.attempt_no)
        ORDER BY r.reservation_id
        """,
        (run_id,),
    ):
        http_groups.setdefault(
            (str(reservation["inference_id"]), int(reservation["attempt_no"])), []
        ).append(reservation)
    for (inference_id, attempt_no), reservations in http_groups.items():
        row = connection.execute(
            "SELECT * FROM selected_occurrences WHERE run_id=? AND inference_id=?",
            (run_id, inference_id),
        ).fetchone()
        if row is None:
            raise N10V12Error("open HTTP reservation has no selected occurrence")
        can_retry = attempt_no < FIXED_FETCH_ATTEMPTS
        _insert_fetch_attempt(
            connection,
            run_id=run_id,
            row=row,
            attempt_no=attempt_no,
            started_at=str(reservations[0]["reserved_at"]),
            elapsed_ms=0,
            outcome="http_failed",
            reservation_ids=[int(value["reservation_id"]) for value in reservations],
            retryable=can_retry,
            delay=1.0 if can_retry else None,
            error_kind="interrupted_unknown",
            error_message="logical fetch may have executed before process interruption",
        )
        if not can_retry:
            connection.execute(
                "UPDATE vision_inputs SET status='fetch_failed',completed_at=?,error_kind='interrupted_unknown',error_message=? WHERE run_id=? AND inference_id=? AND status='pending'",
                (utc_now(), "fetch retry budget consumed by interrupted external call", run_id, inference_id),
            )
    vision_rows = list(
        connection.execute(
            """
            SELECT r.* FROM external_call_reservations r
            WHERE r.run_id=? AND r.call_kind='vision'
              AND NOT EXISTS(SELECT 1 FROM vision_attempts a WHERE a.reservation_id=r.reservation_id)
            ORDER BY r.reservation_id
            """,
            (run_id,),
        )
    )
    for reservation in vision_rows:
        request = json.loads(str(reservation["request_json"]))
        inference_ids = request.get("inference_ids")
        if not isinstance(inference_ids, list) or not all(isinstance(value, str) for value in inference_ids):
            raise N10V12Error("open Vision reservation request is malformed")
        run = connection.execute("SELECT * FROM semantic_runs WHERE run_id=?", (run_id,)).fetchone()
        empty = b""
        connection.execute(
            """
            INSERT INTO vision_attempts(
              run_id,batch_no,attempt_no,inference_ids_json,status,model,reasoning,
              service_tier,runtime_version,cli_version,codex_bin,image_detail,sandbox,
              prompt_sha256,output_schema_sha256,started_at,completed_at,elapsed_ms,
              input_tokens,cached_input_tokens,output_tokens,raw_events_sha256,
              error_kind,error_message,reservation_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                reservation["batch_no"],
                reservation["attempt_no"],
                canonical_json(inference_ids),
                "failed",
                run["model"],
                run["reasoning"],
                run["service_tier"],
                run["runtime_version"],
                run["cli_version"],
                str(request.get("codex_bin", "<interrupted>")),
                run["image_detail"],
                RUNTIME_SANDBOX,
                str(request["prompt_sha256"]),
                run["output_schema_sha256"],
                reservation["reserved_at"],
                utc_now(),
                0,
                None,
                None,
                None,
                hashlib.sha256(empty).hexdigest(),
                "interrupted_unknown",
                "Vision call may have executed before process interruption",
                reservation["reservation_id"],
            ),
        )
        attempt_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        connection.execute(
            "INSERT INTO vision_attempt_payloads VALUES(?,?,?,?,?,?,?,?,?)",
            (
                attempt_id,
                "gzip",
                gzip.compress(empty, mtime=0),
                0,
                hashlib.sha256(empty).hexdigest(),
                gzip.compress(empty, mtime=0),
                0,
                hashlib.sha256(empty).hexdigest(),
                "interrupted_unknown",
            ),
        )
    if open_rows or http_groups or vision_rows:
        _record_event(
            connection,
            run_id,
            "interrupted_external_calls_reconciled",
            {
                "open_completions_closed": len(open_rows),
                "http_attempts_consumed": len(http_groups),
                "vision_attempts_consumed": len(vision_rows),
            },
        )
    connection.commit()
    return {
        "open_completions_closed": len(open_rows),
        "http_attempts_consumed": len(http_groups),
        "vision_attempts_consumed": len(vision_rows),
    }


def _finalize_slots(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    semantics: SemanticContract,
) -> None:
    for building in connection.execute(
        "SELECT selection_id FROM selected_buildings WHERE run_id=? ORDER BY building_rank",
        (run_id,),
    ):
        for slot in semantics.coverage_slots:
            exists = connection.execute(
                "SELECT 1 FROM coverage_slot_assignments WHERE run_id=? AND selection_id=? AND slot=? LIMIT 1",
                (run_id, building["selection_id"], slot),
            ).fetchone()
            if exists is not None:
                continue
            body = {
                "assignment_rank": 0,
                "inference_id": None,
                "selection_id": building["selection_id"],
                "slot": slot,
                "state": "not_observed_in_sample",
            }
            connection.execute(
                "INSERT INTO coverage_slot_assignments VALUES(?,?,?,?,?,?,?)",
                (run_id, building["selection_id"], slot, 0, "not_observed_in_sample", None, canonical_sha256(body)),
            )


def _metrics(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    population: PopulationContract,
    cache_dir: Path,
    retained_for_review: bool,
) -> dict[str, Any]:
    status = {
        str(row[0]): int(row[1])
        for row in connection.execute(
            "SELECT status,COUNT(*) FROM vision_inputs WHERE run_id=? GROUP BY status ORDER BY status",
            (run_id,),
        )
    }
    usage = _observed_usage(connection, run_id)
    reservations = {
        str(row[0]): int(row[1])
        for row in connection.execute(
            "SELECT call_kind,COUNT(*) FROM external_call_reservations WHERE run_id=? GROUP BY call_kind ORDER BY call_kind",
            (run_id,),
        )
    }
    completions = int(
        connection.execute(
            "SELECT COUNT(*) FROM external_call_completions c JOIN external_call_reservations r USING(reservation_id) WHERE r.run_id=?",
            (run_id,),
        ).fetchone()[0]
    )
    open_reservations = int(
        connection.execute(
            "SELECT COUNT(*) FROM external_call_reservations r LEFT JOIN external_call_completions c USING(reservation_id) WHERE r.run_id=? AND c.reservation_id IS NULL",
            (run_id,),
        ).fetchone()[0]
    )
    downloaded = int(
        connection.execute(
            "SELECT COALESCE(SUM(response_bytes),0) FROM fetch_attempts WHERE run_id=?",
            (run_id,),
        ).fetchone()[0]
    )
    return {
        "buildings": FIXED_BUILDING_COUNT,
        "occurrences": population.occurrence_count,
        "input_status": status,
        "fetch_attempts": int(connection.execute("SELECT COUNT(*) FROM fetch_attempts WHERE run_id=?", (run_id,)).fetchone()[0]),
        "downloaded_bytes": downloaded,
        "vision_attempts": int(connection.execute("SELECT COUNT(*) FROM vision_attempts WHERE run_id=?", (run_id,)).fetchone()[0]),
        "successful_vision_attempts": int(connection.execute("SELECT COUNT(*) FROM vision_attempts WHERE run_id=? AND status='success'", (run_id,)).fetchone()[0]),
        "results": int(connection.execute("SELECT COUNT(*) FROM semantic_results WHERE run_id=?", (run_id,)).fetchone()[0]),
        **usage,
        "total_tokens_excluding_cached_double_count": usage["input_tokens"] + usage["output_tokens"],
        "token_threshold_semantics": "observed post-call stop threshold; unknown usage makes totals lower bounds",
        "physical_http_reservations": reservations.get("http", 0),
        "model_call_reservations": reservations.get("vision", 0),
        "model_call_reservation_cap": population.max_model_call_reservations,
        "reservation_completions": completions,
        "open_reservations": open_reservations,
        "materialization_cache": _cache_provenance(cache_dir, retained_for_review),
    }


_LOGICAL_TABLES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "semantic_runs",
        "run_id",
        (
            "run_id", "runner_version", "schema_version", "manifest_size",
            "manifest_byte_sha256", "manifest_self_sha256",
            "ordered_building_manifest_sha256", "ordered_occurrence_manifest_sha256",
            "sample_seed", "building_count", "occurrence_count", "e2_size",
            "e2_sha256_before", "e2_logical_sha256", "e2_run_id", "e3_size",
            "e3_sha256_before", "e3_logical_sha256", "e3_run_id",
            "contract_version", "prompt_version", "output_schema_sha256",
            "transform_version", "e1_contract_version", "dependency_manifest_json",
            "dependency_manifest_sha256", "runtime_version", "retry_policy_version",
            "batch_size", "max_fetch_attempts", "max_vision_attempts", "model",
            "reasoning", "service_tier", "image_detail", "cli_version",
            "schema_sql_sha256", "schema_objects_sha256", "manifest_domain", "ordered_fetch_url_manifest_sha256",
            "ordered_e1_pixel_manifest_sha256", "inference_id_prefix",
            "max_model_call_reservations", "observed_input_token_stop_threshold",
            "observed_output_token_stop_threshold", "observed_total_token_stop_threshold",
            "prior_run_names_json", "prior_successful_derivative_count",
            "publication_contract_version",
        ),
    ),
    ("selected_buildings", "building_rank", ("building_rank","selection_id","source","source_building_id","population_stratum","guard_name","qa_fallback","building_record_sha256","coverage_plan_record_sha256","selected_building_record_sha256","manifest_json")),
    ("selected_occurrences", "input_rank", ("input_rank","inference_id","selection_id","occurrence_rank","source","source_building_id","source_asset_id","candidate_id","fetch_url","expected_response_sha256","expected_e1_pixel_sha256","expected_e1_width","expected_e1_height","e2_asset_record_sha256","e2_relation_record_sha256","e3_candidate_record_sha256","e3_ranking_record_sha256","e3_shortlist_record_sha256","occurrence_record_sha256","manifest_json")),
    ("vision_inputs", "inference_id", ("inference_id","status","selected_fetch_attempt_no","actual_response_sha256","actual_e1_pixel_sha256","derivative_encoded_sha256","derivative_pixel_sha256","derivative_width","derivative_height","derivative_bytes","error_kind","error_message")),
    ("external_call_reservations", "reservation_id", ("reservation_id","call_kind","logical_key","inference_id","batch_no","attempt_no","hop_no","request_json","request_sha256")),
    ("external_call_completions", "reservation_id", ("reservation_id","outcome","http_status","elapsed_ms","error_kind","error_message")),
    ("fetch_attempts", "inference_id,attempt_no", ("inference_id","attempt_no","request_url","final_url","elapsed_ms","outcome","http_status","content_type","response_bytes","expected_response_sha256","actual_response_sha256","expected_e1_pixel_sha256","actual_e1_pixel_sha256","retryable","retry_after_seconds","scheduled_delay_seconds","error_kind","error_message","reservation_ids_json")),
    ("vision_attempts", "attempt_id", ("attempt_id","batch_no","attempt_no","inference_ids_json","status","model","reasoning","service_tier","runtime_version","cli_version","codex_bin","image_detail","sandbox","prompt_sha256","output_schema_sha256","elapsed_ms","input_tokens","cached_input_tokens","output_tokens","raw_events_sha256","error_kind","error_message","reservation_id")),
    ("vision_attempt_payloads", "attempt_id", ("attempt_id","codec","stdout_bytes","stdout_sha256","stderr_bytes","stderr_sha256","stderr_excerpt")),
    ("semantic_results", "inference_id", ("inference_id","attempt_id","raw_result_json","normalized_result_json","in_scope","reject_reason","medium","spatial_context","framing_scale","camera_angle","drawing_kind","project_state","project_legibility","uncertain_axes_json","resolution_insufficient","evidence","record_sha256")),
    ("occurrence_result_links", "inference_id", ("inference_id","result_inference_id","reuse_basis","verified_input_pixel_sha256","record_sha256")),
    ("hero_candidate_decisions", "inference_id", ("inference_id","tier","reasons_json","authoritative","record_sha256")),
    ("coverage_slot_assignments", "selection_id,slot,assignment_rank", ("selection_id","slot","assignment_rank","state","inference_id","record_sha256")),
    ("runtime_events", "event_id", ("event_id","event_type","detail_json")),
    ("validations", "validation_name", ("validation_name","severity","passed","expected","actual","detail")),
)


def logical_sha256(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    digest.update((LOGICAL_MANIFEST_VERSION + "\n").encode("ascii"))
    for table, order_by, columns in _LOGICAL_TABLES:
        digest.update((table + "\0").encode("utf-8"))
        for row in connection.execute(
            f"SELECT {','.join(columns)} FROM {table} ORDER BY {order_by}"
        ):
            digest.update(canonical_json(dict(zip(columns, row))).encode("utf-8") + b"\n")
    return digest.hexdigest()


def _render_report(status: str, logical: str, metrics: Mapping[str, Any]) -> str:
    return "\n".join(
        [
            "# Cross-source semantic Vision N10 v1.2",
            "",
            f"- Status: `{status}`",
            f"- Logical SHA-256: `{logical}`",
            f"- Buildings / occurrences: `{metrics['buildings']}` / `{metrics['occurrences']}`",
            f"- Input states: `{canonical_json(metrics['input_status'])}`",
            f"- Physical HTTP / model call reservations: `{metrics['physical_http_reservations']}` / `{metrics['model_call_reservations']}`",
            f"- Fetch attempts / downloaded bytes: `{metrics['fetch_attempts']}` / `{metrics['downloaded_bytes']}`",
            f"- Vision attempts / results: `{metrics['vision_attempts']}` / `{metrics['results']}`",
            f"- Observed tokens input / cached / output: `{metrics['input_tokens']}` / `{metrics['cached_input_tokens']}` / `{metrics['output_tokens']}`",
            "",
            "Token thresholds are post-call observed stop thresholds, not server-enforced hard ceilings. Unknown usage makes totals lower bounds.",
            "The sample is frozen; failed or changed images were not replaced. Hero and coverage rows are non-authoritative pixel-derived evidence.",
            "",
        ]
    )


def _report_binding(status: str, metrics: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        _render_report(status, REPORT_LOGICAL_PLACEHOLDER, metrics).encode("utf-8")
    ).hexdigest()


def _cache_accounting(
    connection: sqlite3.Connection, *, run_id: str, cache_dir: Path
) -> tuple[bool, dict[str, Any]]:
    rows = list(
        connection.execute(
            "SELECT * FROM vision_inputs WHERE run_id=? AND derivative_encoded_sha256 IS NOT NULL ORDER BY inference_id",
            (run_id,),
        )
    )
    expected = {f"{row['inference_id']}.jpg" for row in rows}
    actual = {path.name for path in cache_dir.glob("*.jpg") if path.is_file()}
    mismatches: list[str] = []
    for row in rows:
        try:
            _load_cached(cache_dir, row)
        except Exception as exc:
            mismatches.append(f"{row['inference_id']}: {exc}")
    detail = {
        "expected_files": len(expected),
        "actual_files": len(actual),
        "missing": sorted(expected - actual),
        "unexpected": sorted(actual - expected),
        "identity_mismatches": mismatches,
    }
    return expected == actual and not mismatches, detail


def _validation_rows(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    population: PopulationContract,
    manifest_path: Path,
    e2_after: str,
    e3_after: str,
    cache_dir: Path,
) -> list[dict[str, Any]]:
    run = connection.execute("SELECT * FROM semantic_runs WHERE run_id=?", (run_id,)).fetchone()
    reservations = int(
        connection.execute(
            "SELECT COUNT(*) FROM external_call_reservations WHERE run_id=?", (run_id,)
        ).fetchone()[0]
    )
    completions = int(
        connection.execute(
            "SELECT COUNT(*) FROM external_call_completions c JOIN external_call_reservations r USING(reservation_id) WHERE r.run_id=?",
            (run_id,),
        ).fetchone()[0]
    )
    model_calls = int(
        connection.execute(
            "SELECT COUNT(*) FROM external_call_reservations WHERE run_id=? AND call_kind='vision'",
            (run_id,),
        ).fetchone()[0]
    )
    inputs = {
        str(row[0]): int(row[1])
        for row in connection.execute(
            "SELECT status,COUNT(*) FROM vision_inputs WHERE run_id=? GROUP BY status",
            (run_id,),
        )
    }
    successes = inputs.get("success", 0)
    results = int(connection.execute("SELECT COUNT(*) FROM semantic_results WHERE run_id=?", (run_id,)).fetchone()[0])
    cache_ok, cache_detail = _cache_accounting(connection, run_id=run_id, cache_dir=cache_dir)
    usage = _observed_usage(connection, run_id)
    threshold_detail = {
        **usage,
        "semantics": "observed post-call stop threshold; unknown usage is a lower-bound warning",
        "thresholds": {
            "input": population.observed_input_token_stop_threshold,
            "output": population.observed_output_token_stop_threshold,
            "total": population.observed_total_token_stop_threshold,
        },
    }
    gate_rows = list(
        connection.execute(
            "SELECT detail_json FROM runtime_events WHERE run_id=? "
            "AND event_type='actual_derivative_exclusion_gate' ORDER BY event_id",
            (run_id,),
        )
    )
    try:
        gate_detail = json.loads(str(gate_rows[0][0])) if len(gate_rows) == 1 else {}
    except json.JSONDecodeError:
        gate_detail = {}
    gate_ok = (
        len(gate_rows) == 1
        and gate_detail.get("vision_reservations_before_gate") == 0
        and gate_detail.get("prior_run_names") == sorted(population.prior_run_names)
        and gate_detail.get("prior_successful_derivative_count")
        == population.prior_successful_derivative_count
    )
    checks = [
        ("manifest_identity", file_sha256(manifest_path) == population.manifest_byte_sha256, population.manifest_byte_sha256, file_sha256(manifest_path), None),
        ("fixed_population_accounting", int(run["building_count"]) == FIXED_BUILDING_COUNT and int(run["occurrence_count"]) == population.occurrence_count, f"{FIXED_BUILDING_COUNT}/{population.occurrence_count}", f"{run['building_count']}/{run['occurrence_count']}", None),
        ("input_files_immutable", e2_after == run["e2_sha256_before"] and e3_after == run["e3_sha256_before"], "unchanged", f"{e2_after}/{e3_after}", None),
        ("actual_derivative_exclusion_gate", gate_ok, "one committed pre-Vision gate", len(gate_rows), gate_detail),
        ("reservation_accounting", reservations == completions, reservations, completions, None),
        ("model_call_reservation_cap", model_calls <= population.max_model_call_reservations, population.max_model_call_reservations, model_calls, None),
        ("observed_token_threshold_accounting", True, "observed lower-bound accounting", threshold_detail, threshold_detail),
        ("attempt_accounting", int(connection.execute("SELECT COUNT(*) FROM vision_attempts WHERE run_id=?", (run_id,)).fetchone()[0]) == model_calls, model_calls, int(connection.execute("SELECT COUNT(*) FROM vision_attempts WHERE run_id=?", (run_id,)).fetchone()[0]), None),
        ("result_accounting", results == successes, successes, results, None),
        ("no_pending_work", not any(inputs.get(value, 0) for value in ("pending", "ready")), 0, inputs.get("pending", 0) + inputs.get("ready", 0), None),
        ("materialization_cache_integrity", cache_ok, True, cache_ok, cache_detail),
        ("sqlite_quick_check", connection.execute("PRAGMA quick_check").fetchone()[0] == "ok", "ok", connection.execute("PRAGMA quick_check").fetchone()[0], None),
        ("sqlite_integrity_check", connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "ok", connection.execute("PRAGMA integrity_check").fetchone()[0], None),
        ("foreign_key_check", not connection.execute("PRAGMA foreign_key_check").fetchall(), [], [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")], None),
    ]
    return [
        {"name": name, "severity": "error", "passed": bool(passed), "expected": canonical_json(expected) if not isinstance(expected, str) else expected, "actual": canonical_json(actual) if not isinstance(actual, str) else actual, "detail": canonical_json(detail) if detail is not None else None}
        for name, passed, expected, actual, detail in checks
    ]


def _publish_link_recoverable(
    partial_db: Path,
    output_db: Path,
    partial_report: Path,
    report_path: Path,
) -> None:
    """Idempotently hard-link a validated terminal pair without clobbering."""

    expected_db = (partial_db.stat().st_size, file_sha256(partial_db))
    expected_report = (partial_report.stat().st_size, file_sha256(partial_report))
    if output_db.exists():
        if (output_db.stat().st_size, file_sha256(output_db)) != expected_db:
            raise N10V12Error("published DB conflicts with terminal partial DB")
    else:
        os.link(partial_db, output_db)
    if report_path.exists():
        if (report_path.stat().st_size, file_sha256(report_path)) != expected_report:
            raise N10V12Error("published report conflicts with terminal partial report")
    else:
        os.link(partial_report, report_path)
    # Only remove the private names after both public names are proven exact.
    partial_db.unlink(missing_ok=True)
    partial_report.unlink(missing_ok=True)


def validate_terminal_v12(
    path: Path,
    report_path: Path,
    population: PopulationContract,
    semantics: SemanticContract,
) -> tuple[str, str, dict[str, Any]]:
    if _sidecars(path):
        raise N10V12Error(f"terminal v1.2 DB has SQLite sidecars: {_sidecars(path)}")
    connection = open_immutable(path)
    try:
        if int(connection.execute("PRAGMA application_id").fetchone()[0]) != APPLICATION_ID:
            raise N10V12Error("terminal v1.2 application_id mismatch")
        if int(connection.execute("PRAGMA user_version").fetchone()[0]) != SCHEMA_VERSION:
            raise N10V12Error("terminal v1.2 schema version mismatch")
        runs = connection.execute("SELECT * FROM semantic_runs").fetchall()
        if len(runs) != 1 or str(runs[0]["status"]) not in TERMINAL_STATUSES:
            raise N10V12Error("terminal v1.2 contains no publishable run")
        run = runs[0]
        expected = {
            "runner_version": RUNNER_VERSION,
            "schema_version": SCHEMA_VERSION,
            "manifest_domain": population.manifest_domain,
            "manifest_byte_sha256": population.manifest_byte_sha256,
            "manifest_self_sha256": population.manifest_self_sha256,
            "sample_seed": population.sample_seed,
            "building_count": FIXED_BUILDING_COUNT,
            "occurrence_count": population.occurrence_count,
            "contract_version": semantics.contract_version,
            "prompt_version": semantics.prompt_version,
            "output_schema_sha256": semantics.output_schema_sha256,
            "schema_sql_sha256": schema_sql_sha256(population),
            "schema_objects_sha256": schema_objects_sha256(connection),
            "max_model_call_reservations": population.max_model_call_reservations,
            "prior_run_names_json": canonical_json(sorted(population.prior_run_names)),
            "prior_successful_derivative_count": population.prior_successful_derivative_count,
            "publication_contract_version": PUBLICATION_CONTRACT_VERSION,
        }
        mismatch = {key: {"actual": run[key], "expected": value} for key, value in expected.items() if run[key] != value}
        if mismatch:
            raise N10V12Error("terminal v1.2 contract mismatch: " + canonical_json(mismatch))
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok" or connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or connection.execute("PRAGMA foreign_key_check").fetchall():
            raise N10V12Error("terminal v1.2 SQLite integrity failed")
        stored = {str(row[0]): (str(row[1]), int(row[2])) for row in connection.execute("SELECT validation_name,severity,passed FROM validations")}
        if any(stored.get(name) != ("error", 1) for name in REQUIRED_VALIDATIONS):
            raise N10V12Error("terminal v1.2 required validation absent or failed")
        logical = logical_sha256(connection)
        if logical != run["logical_sha256"]:
            raise N10V12Error("terminal v1.2 logical SHA mismatch")
        metrics = json.loads(str(run["metrics_json"]))
        expected_report = _render_report(str(run["status"]), logical, metrics).encode("utf-8")
        if not report_path.is_file() or report_path.read_bytes() != expected_report:
            raise N10V12Error("terminal v1.2 report binding mismatch")
        return str(run["status"]), logical, metrics
    finally:
        connection.close()


def _recreate_terminal_report(
    db_path: Path,
    report_path: Path,
    population: PopulationContract,
    semantics: SemanticContract,
) -> None:
    """Recover a missing private/public report from an already terminal DB."""

    if _sidecars(db_path):
        raise N10V12Error("cannot recreate report while terminal DB has SQLite sidecars")
    connection = open_immutable(db_path)
    try:
        run = connection.execute("SELECT * FROM semantic_runs").fetchone()
        if run is None or str(run["status"]) not in TERMINAL_STATUSES:
            raise N10V12Error("report recovery requires a terminal v1.2 DB")
        if (
            int(run["schema_version"]) != SCHEMA_VERSION
            or run["manifest_byte_sha256"] != population.manifest_byte_sha256
            or run["prompt_version"] != semantics.prompt_version
            or run["output_schema_sha256"] != semantics.output_schema_sha256
        ):
            raise N10V12Error("report recovery DB contract mismatch")
        logical = logical_sha256(connection)
        if logical != run["logical_sha256"]:
            raise N10V12Error("report recovery logical SHA mismatch")
        metrics = json.loads(str(run["metrics_json"]))
        raw = _render_report(str(run["status"]), logical, metrics).encode("utf-8")
    finally:
        connection.close()
    _atomic_write_bytes(report_path, raw)


def _cleanup_private_cache(cache_dir: Path, inference_ids: Sequence[str]) -> None:
    expected = {f"{value}.jpg" for value in inference_ids}
    actual = {path.name for path in cache_dir.iterdir() if path.is_file()}
    if actual != expected:
        raise N10V12Error(
            "private cache cleanup accounting mismatch: "
            + canonical_json({"expected": sorted(expected), "actual": sorted(actual)})
        )
    for name in sorted(expected):
        (cache_dir / name).unlink()
    cache_dir.rmdir()


def run_semantic_vision_n10_v12(
    *,
    population: PopulationContract,
    semantics: SemanticContract,
    manifest_path: Path,
    e2_path: Path,
    e3_path: Path,
    output_db: Path,
    report_path: Path,
    codex_bin: Path,
    model: str = DEFAULT_MODEL,
    reasoning: str = DEFAULT_REASONING,
    service_tier: str = DEFAULT_SERVICE_TIER,
    cli_version: str | None = None,
    resume: bool = False,
    review_cache_dir: Path | None = None,
    fetcher: Callable[..., FetchPayload] = fetch_once_reserved,
    executor: Callable[..., VisionRuntimeResult] = run_codex_vision_batch,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    preflight_validator: Callable[[Path], Any] | None = None,
) -> N10V12RunResult:
    """Run or exactly resume one pinned, disjoint semantic N10 v1.2."""

    manifest_path = manifest_path.resolve()
    e2_path = e2_path.resolve()
    e3_path = e3_path.resolve()
    output_db = output_db.resolve()
    report_path = report_path.resolve()
    if len({manifest_path, e2_path, e3_path, output_db, report_path}) != 5:
        raise ValueError("v1.2 manifest, inputs, output, and report must be distinct")
    payload, manifest_raw = _load_manifest(manifest_path, population)
    _manifest_rows(payload, population)
    prior_derivative_pixels = _prior_successful_derivative_pixels(payload, population)
    if preflight_validator is not None:
        preflight = preflight_validator(manifest_path)
        if not bool(getattr(preflight, "passed", False)):
            raise N10V12Error("independent v1.2 manifest preflight failed")
    e2_before = _verify_input(e2_path, payload["e2_input"])
    e3_before = _verify_input(e3_path, payload["e3_input"])

    output_db.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    partial_db = output_db.with_name(output_db.name + ".partial")
    partial_report = report_path.with_name(report_path.name + ".partial")
    retained_for_review = review_cache_dir is not None
    cache_dir = (
        review_cache_dir.resolve()
        if review_cache_dir is not None
        else Path(str(partial_db) + ".inputs")
    )
    descriptor = _acquire_lock(Path(str(output_db) + ".lock"))
    resumed = False
    try:
        # Recover a crash between the two public hard links.  No external call
        # is possible before this state classification completes.
        if output_db.exists():
            if report_path.exists():
                if not resume:
                    raise FileExistsError(f"immutable v1.2 output exists: {output_db}")
                status, logical, metrics = validate_terminal_v12(
                    output_db, report_path, population, semantics
                )
                for private_path, public_path in (
                    (partial_db, output_db),
                    (partial_report, report_path),
                ):
                    if not private_path.exists():
                        continue
                    if (private_path.stat().st_size, file_sha256(private_path)) != (
                        public_path.stat().st_size,
                        file_sha256(public_path),
                    ):
                        raise N10V12Error(
                            f"completed publication has conflicting private path: {private_path}"
                        )
                    private_path.unlink()
                cache_record = metrics.get("materialization_cache", {})
                if (
                    isinstance(cache_record, Mapping)
                    and cache_record.get("retained_for_review") is False
                    and cache_dir.is_dir()
                ):
                    completed = open_immutable(output_db)
                    try:
                        ids = tuple(
                            str(row[0])
                            for row in completed.execute(
                                "SELECT inference_id FROM vision_inputs WHERE derivative_encoded_sha256 IS NOT NULL ORDER BY inference_id"
                            )
                        )
                    finally:
                        completed.close()
                    _cleanup_private_cache(cache_dir, ids)
                return N10V12RunResult(output_db, report_path, status, logical, 0, 0, True, True, metrics)
            if not resume or not partial_db.exists():
                raise N10V12Error("published DB exists without report/recovery partial")
            if (output_db.stat().st_size, file_sha256(output_db)) != (
                partial_db.stat().st_size,
                file_sha256(partial_db),
            ):
                raise N10V12Error("published DB and recovery partial differ")
            _recreate_terminal_report(partial_db, partial_report, population, semantics)
            _publish_link_recoverable(partial_db, output_db, partial_report, report_path)
            status, logical, metrics = validate_terminal_v12(output_db, report_path, population, semantics)
            return N10V12RunResult(output_db, report_path, status, logical, 0, 0, True, True, metrics)
        if report_path.exists():
            raise N10V12Error("published report exists without its immutable DB")

        if partial_db.exists():
            if not resume:
                raise FileExistsError(f"v1.2 partial exists; pass --resume: {partial_db}")
            resumed = True
            recover_sqlite(partial_db)
            probe = sqlite3.connect(partial_db)
            probe.row_factory = sqlite3.Row
            try:
                if int(probe.execute("PRAGMA user_version").fetchone()[0]) not in {0, SCHEMA_VERSION}:
                    raise N10V12Error("partial DB is not a v1.2 schema")
                run = probe.execute("SELECT * FROM semantic_runs").fetchone() if probe.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='semantic_runs'").fetchone()[0] else None
            finally:
                probe.close()
            if run is not None and str(run["status"]) in TERMINAL_STATUSES:
                _recreate_terminal_report(partial_db, partial_report, population, semantics)
                validate_terminal_v12(partial_db, partial_report, population, semantics)
                _publish_link_recoverable(partial_db, output_db, partial_report, report_path)
                status, logical, metrics = validate_terminal_v12(output_db, report_path, population, semantics)
                return N10V12RunResult(output_db, report_path, status, logical, 0, 0, True, True, metrics)
            if partial_report.exists():
                raise N10V12Error("running/initializing partial DB has a stale partial report")
        elif resume:
            raise FileNotFoundError(f"v1.2 partial does not exist: {partial_db}")
        else:
            # Exclusive creation makes a crash-before-first-run-row visible as
            # an initialization partial which --resume can finish.
            with partial_db.open("xb"):
                pass

        connection = sqlite3.connect(partial_db)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        materialized_ids: tuple[str, ...] = ()
        try:
            _ensure_schema(connection, population)
            run = connection.execute("SELECT * FROM semantic_runs").fetchone()
            run_id = "semn10v12-" + hashlib.sha256(
                (
                    population.manifest_self_sha256
                    + "\0"
                    + model
                    + "\0"
                    + semantics.prompt_version
                ).encode("utf-8")
            ).hexdigest()[:24]
            if run is None or str(run["status"]) == "initializing":
                cache_dir.mkdir(parents=True, exist_ok=True)
                _initialize(
                    connection,
                    run_id=run_id,
                    manifest_path=manifest_path,
                    manifest_raw=manifest_raw,
                    payload=payload,
                    population=population,
                    semantics=semantics,
                    e2_path=e2_path,
                    e3_path=e3_path,
                    model=model,
                    reasoning=reasoning,
                    service_tier=service_tier,
                    cli_version=cli_version,
                    cache_dir=cache_dir,
                    retained_for_review=retained_for_review,
                )
                run = connection.execute("SELECT * FROM semantic_runs").fetchone()
            if run is None or str(run["status"]) != "running" or str(run["run_id"]) != run_id:
                raise N10V12Error("v1.2 resume requires its exact running run")
            expected = {
                "runner_version": RUNNER_VERSION,
                "schema_version": SCHEMA_VERSION,
                "manifest_byte_sha256": population.manifest_byte_sha256,
                "manifest_self_sha256": population.manifest_self_sha256,
                "e2_sha256_before": e2_before,
                "e3_sha256_before": e3_before,
                "contract_version": semantics.contract_version,
                "prompt_version": semantics.prompt_version,
                "output_schema_sha256": semantics.output_schema_sha256,
                "model": model,
                "reasoning": reasoning,
                "service_tier": service_tier,
                "cli_version": cli_version,
                "max_model_call_reservations": population.max_model_call_reservations,
                "prior_run_names_json": canonical_json(sorted(population.prior_run_names)),
                "prior_successful_derivative_count": population.prior_successful_derivative_count,
            }
            mismatch = {key: {"actual": run[key], "expected": value} for key, value in expected.items() if run[key] != value}
            if mismatch:
                raise N10V12Error("v1.2 resume contract mismatch: " + canonical_json(mismatch))
            if not cache_dir.is_dir():
                raise N10V12Error("v1.2 running resume cache is missing")
            _clear_stale_atomic_temps(cache_dir)
            _reconcile_open_reservations(connection, run_id=run_id)

            source_rows = list(
                connection.execute(
                    "SELECT o.* FROM selected_occurrences o WHERE o.run_id=? ORDER BY o.input_rank",
                    (run_id,),
                )
            )
            limiter = _GlobalRequestRateLimiter(DEFAULT_REQUESTS_PER_SECOND, clock, sleeper)
            circuit = [0]
            # All fixed inputs are materialized before the first model call.
            for row in source_rows:
                _prepare_input(
                    connection,
                    run_id=run_id,
                    row=row,
                    population=population,
                    cache_dir=cache_dir,
                    fetcher=fetcher,
                    limiter=limiter,
                    sleeper=sleeper,
                    clock=clock,
                    circuit=circuit,
                )
            existing_gate = connection.execute(
                "SELECT 1 FROM runtime_events WHERE run_id=? AND event_type='actual_derivative_exclusion_gate'",
                (run_id,),
            ).fetchone()
            if existing_gate is None:
                _actual_derivative_exclusion_gate(
                    connection,
                    run_id=run_id,
                    population=population,
                    prior_derivative_pixels=prior_derivative_pixels,
                )

            schema_text = canonical_json(semantics.output_schema)
            for batch_start in range(0, len(source_rows), FIXED_BATCH_SIZE):
                batch_no = batch_start // FIXED_BATCH_SIZE + 1
                batch = source_rows[batch_start : batch_start + FIXED_BATCH_SIZE]
                ready_rows = [
                    row
                    for row in batch
                    if connection.execute(
                        "SELECT status FROM vision_inputs WHERE run_id=? AND inference_id=?",
                        (run_id, row["inference_id"]),
                    ).fetchone()[0]
                    == "ready"
                ]
                if not ready_rows:
                    continue
                inference_ids = [str(row["inference_id"]) for row in ready_rows]
                attempts = list(
                    connection.execute(
                        "SELECT * FROM vision_attempts WHERE run_id=? AND batch_no=? ORDER BY attempt_no",
                        (run_id, batch_no),
                    )
                )
                if any(json.loads(str(row["inference_ids_json"])) != inference_ids for row in attempts):
                    raise N10V12Error("v1.2 retry batch membership changed")
                if len(attempts) >= FIXED_VISION_ATTEMPTS:
                    connection.executemany(
                        "UPDATE vision_inputs SET status='vision_failed',completed_at=?,error_kind='retry_exhausted',error_message='Vision retry budget exhausted' WHERE run_id=? AND inference_id=? AND status='ready'",
                        [(utc_now(), run_id, value) for value in inference_ids],
                    )
                    connection.commit()
                    continue
                with tempfile.TemporaryDirectory(prefix="semantic-vision-n10-v12-") as temp_name:
                    temp_dir = Path(temp_name)
                    schema_path = temp_dir / "output.schema.json"
                    schema_path.write_text(schema_text, encoding="utf-8", newline="\n")
                    image_paths: list[Path] = []
                    for row in ready_rows:
                        state = connection.execute(
                            "SELECT * FROM vision_inputs WHERE run_id=? AND inference_id=?",
                            (run_id, row["inference_id"]),
                        ).fetchone()
                        path = temp_dir / f"{row['inference_id']}.jpg"
                        path.write_bytes(_load_cached(cache_dir, state))
                        image_paths.append(path)
                    succeeded = False
                    for attempt_no in range(len(attempts) + 1, FIXED_VISION_ATTEMPTS + 1):
                        _enforce_observed_token_stop(
                            connection,
                            run_id=run_id,
                            population=population,
                            before_new_call=True,
                        )
                        prompt = semantics.compose_prompt(inference_ids)
                        ordered_inputs = []
                        for ready_row in ready_rows:
                            input_state = connection.execute(
                                "SELECT derivative_encoded_sha256,derivative_pixel_sha256 "
                                "FROM vision_inputs WHERE run_id=? AND inference_id=?",
                                (run_id, ready_row["inference_id"]),
                            ).fetchone()
                            if input_state is None or not all(input_state):
                                raise N10V12Error("Vision reservation input identity is missing")
                            ordered_inputs.append(
                                {
                                    "derivative_encoded_sha256": input_state[0],
                                    "derivative_pixel_sha256": input_state[1],
                                    "inference_id": ready_row["inference_id"],
                                }
                            )
                        request = {
                            "batch_no": batch_no,
                            "attempt_no": attempt_no,
                            "codex_bin": str(codex_bin),
                            "inference_ids": inference_ids,
                            "model": model,
                            "ordered_inputs": ordered_inputs,
                            "output_schema_sha256": semantics.output_schema_sha256,
                            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                            "reasoning": reasoning,
                            "service_tier": service_tier,
                        }
                        try:
                            reservation_id = _reserve_call(
                                connection,
                                run_id=run_id,
                                kind="vision",
                                logical_key=f"vision:{batch_no}:{attempt_no}",
                                inference_id=None,
                                batch_no=batch_no,
                                attempt_no=attempt_no,
                                hop_no=None,
                                request=request,
                            )
                        except sqlite3.IntegrityError as exc:
                            _pause(
                                connection,
                                run_id,
                                "model_call_reservation_cap",
                                {"batch_no": batch_no, "attempt_no": attempt_no, "error": str(exc)},
                            )
                        started_at = utc_now()
                        executor_started = clock()
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
                                schema_path=schema_path,
                                inference_ids=inference_ids,
                                codex_bin=codex_bin,
                                model=model,
                                reasoning=reasoning,
                                service_tier=service_tier,
                                working_directory=temp_dir,
                                elapsed_seconds=clock() - executor_started,
                            )
                        normalized: Sequence[Mapping[str, Any]] | None = None
                        schema_error: str | None = None
                        if result.ok:
                            try:
                                normalized = semantics.normalize_batch(result.records, inference_ids)
                            except Exception as exc:
                                schema_error = str(exc)
                        _complete_call(
                            connection,
                            reservation_id=reservation_id,
                            outcome="completed" if result.ok else "exception",
                            elapsed_ms=round(result.elapsed_seconds * 1000),
                            error_kind=result.error_kind,
                            error_message=result.error_message,
                            commit=False,
                        )
                        if not result.ok or normalized is None:
                            _write_vision_attempt(
                                connection,
                                run_id=run_id,
                                batch_no=batch_no,
                                attempt_no=attempt_no,
                                reservation_id=reservation_id,
                                inference_ids=inference_ids,
                                result=result,
                                status="failed",
                                started_at=started_at,
                                error_kind="semantic_schema" if schema_error else None,
                                error_message=schema_error,
                            )
                            connection.commit()
                            continue
                        attempt_id = _write_vision_attempt(
                            connection,
                            run_id=run_id,
                            batch_no=batch_no,
                            attempt_no=attempt_no,
                            reservation_id=reservation_id,
                            inference_ids=inference_ids,
                            result=result,
                            status="success",
                            started_at=started_at,
                        )
                        _write_results(
                            connection,
                            run_id=run_id,
                            attempt_id=attempt_id,
                            rows=ready_rows,
                            raw_results=result.records,
                            normalized=normalized,
                            semantics=semantics,
                        )
                        connection.commit()
                        succeeded = True
                        break
                    if not succeeded:
                        connection.executemany(
                            "UPDATE vision_inputs SET status='vision_failed',completed_at=?,error_kind='retry_exhausted',error_message='Vision retry budget exhausted' WHERE run_id=? AND inference_id=? AND status='ready'",
                            [(utc_now(), run_id, value) for value in inference_ids],
                        )
                        connection.commit()

            pending = int(connection.execute("SELECT COUNT(*) FROM vision_inputs WHERE run_id=? AND status IN ('pending','ready')", (run_id,)).fetchone()[0])
            if pending:
                raise N10V12Error(f"v1.2 terminalization has {pending} pending/ready inputs")
            _finalize_slots(connection, run_id=run_id, semantics=semantics)
            connection.commit()
            e2_after = _verify_input(e2_path, payload["e2_input"])
            e3_after = _verify_input(e3_path, payload["e3_input"])
            validations = _validation_rows(
                connection,
                run_id=run_id,
                population=population,
                manifest_path=manifest_path,
                e2_after=e2_after,
                e3_after=e3_after,
                cache_dir=cache_dir,
            )
            hard_failures = [row for row in validations if not row["passed"]]
            if hard_failures:
                raise N10V12Error("v1.2 terminal validation failed: " + canonical_json(hard_failures))
            metrics = _metrics(
                connection,
                run_id=run_id,
                population=population,
                cache_dir=cache_dir,
                retained_for_review=retained_for_review,
            )
            failures = population.occurrence_count - int(metrics["input_status"].get("success", 0))
            terminal_status = "complete_with_failures" if failures else "complete"
            binding = _report_binding(terminal_status, metrics)
            validations.append(
                {"name": "report_binding", "severity": "error", "passed": True, "expected": binding, "actual": binding, "detail": "canonical report with logical placeholder"}
            )
            connection.executemany(
                "INSERT INTO validations VALUES(?,?,?,?,?,?,?)",
                [(run_id, row["name"], row["severity"], int(row["passed"]), row["expected"], row["actual"], row["detail"]) for row in validations],
            )
            logical = logical_sha256(connection)
            report_raw = _render_report(terminal_status, logical, metrics).encode("utf-8")
            connection.execute(
                "UPDATE semantic_runs SET status=?,e2_sha256_after=?,e3_sha256_after=?,completed_at=?,metrics_json=?,logical_sha256=?,error=NULL WHERE run_id=?",
                (terminal_status, e2_after, e3_after, utc_now(), canonical_json(metrics), logical, run_id),
            )
            connection.commit()
            _atomic_write_bytes(partial_report, report_raw)
            materialized_ids = tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT inference_id FROM vision_inputs WHERE run_id=? AND derivative_encoded_sha256 IS NOT NULL ORDER BY inference_id",
                    (run_id,),
                )
            )
        finally:
            connection.close()
        validate_terminal_v12(partial_db, partial_report, population, semantics)
        _publish_link_recoverable(partial_db, output_db, partial_report, report_path)
        status, logical, metrics = validate_terminal_v12(output_db, report_path, population, semantics)
        if not retained_for_review:
            _cleanup_private_cache(cache_dir, materialized_ids)
        return N10V12RunResult(
            output_db,
            report_path,
            status,
            logical,
            int(metrics["physical_http_reservations"]),
            int(metrics["model_call_reservations"]),
            resumed,
            False,
            metrics,
        )
    finally:
        _release_lock(Path(str(output_db) + ".lock"), descriptor)


__all__ = [
    "APPLICATION_ID",
    "DEFAULT_REQUESTS_PER_SECOND",
    "FIXED_BATCH_SIZE",
    "FIXED_BUILDING_COUNT",
    "FIXED_FETCH_ATTEMPTS",
    "FIXED_VISION_ATTEMPTS",
    "LOGICAL_MANIFEST_VERSION",
    "N10V12Error",
    "N10V12Paused",
    "N10V12RunResult",
    "PUBLICATION_CONTRACT_VERSION",
    "PopulationContract",
    "REQUIRED_VALIDATIONS",
    "RETRY_POLICY_VERSION",
    "RUNNER_VERSION",
    "SCHEMA_VERSION",
    "SemanticContract",
    "logical_sha256",
    "run_semantic_vision_n10_v12",
    "schema_objects_sha256",
    "schema_objects_sha256_for_contract",
    "schema_sql_sha256",
    "validate_terminal_v12",
]
