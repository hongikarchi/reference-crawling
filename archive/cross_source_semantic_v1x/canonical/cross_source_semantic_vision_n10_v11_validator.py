"""Independent, read-only validator for semantic Vision N10 v1.1.

This module intentionally does not import the v1.1 runner or fetch adapter.  It
duplicates the published schema/state contract, replays manifest and semantic
derivations with the versioned prompt module, and opens every SQLite input with
``mode=ro&immutable=1`` only after rejecting WAL/SHM/journal files.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from canonical.cross_source_image_selection import canonical_json, canonical_sha256
from canonical.cross_source_semantic_vision_v11 import (
    CONTRACT_VERSION,
    OUTPUT_SCHEMA,
    PROMPT_TEMPLATE_SHA256,
    PROMPT_VERSION,
    TRANSFORM_VERSION,
    compose_prompt,
    derive_coverage_slots,
    derive_hero_decision,
    normalize_result,
)
from canonical.image_fingerprint import FINGERPRINT_CONTRACT_VERSION, fingerprint_bytes
from canonical.divisare_vision_runtime import (
    CLI_IMAGE_DETAIL,
    RUNTIME_SANDBOX,
    RUNTIME_VERSION,
)


APPLICATION_ID = 0x53564E31
SCHEMA_VERSION = 3
RUNNER_VERSION = "cross-source-semantic-vision-n10-v1.1-runner-v2.0.0"
RETRY_POLICY_VERSION = "cross-source-semantic-vision-n10-v1.1-retry-v2.0.0"
PUBLICATION_CONTRACT_VERSION = "semantic-two-file-recoverable-publication-v2"
LOGICAL_MANIFEST_VERSION = "cross-source-semantic-vision-n10-v1.1-logical-v1"
FIXED_BUILDING_COUNT = 10
FIXED_BATCH_SIZE = 5
FIXED_FETCH_ATTEMPTS = 3
FIXED_VISION_ATTEMPTS = 2
REPORT_LOGICAL_PLACEHOLDER = "0" * 64
TERMINAL_STATUSES = frozenset({"complete", "complete_with_failures"})
ALLOWED_HOSTS = {
    "architizer": "architizer-prod.imgix.net",
    "divisare": "images.divisare.com",
}
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
REQUIRED_TABLES = frozenset(
    {
        "semantic_runs", "selected_buildings", "selected_occurrences",
        "vision_inputs", "fetch_attempts", "vision_attempts",
        "vision_attempt_payloads", "semantic_results",
        "occurrence_result_links", "hero_candidate_decisions",
        "coverage_slot_assignments", "validations",
        "external_call_reservations", "external_call_completions",
        "runtime_events",
    }
)
REQUIRED_V11_TRIGGERS = frozenset(
    {
        "semantic_runs_v11_provenance_immutable",
        "reservations_insert", "reservations_model_cap",
        "reservations_no_update", "reservations_no_delete",
        "completions_insert", "completions_no_update", "completions_no_delete",
        "runtime_events_insert", "runtime_events_no_update", "runtime_events_no_delete",
        "vision_attempt_reservation",
    }
)


@dataclass(frozen=True)
class ValidationContract:
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
    expected_schema_sql_sha256: str
    expected_schema_objects_sha256: str
    expected_prompt_template_sha256: str = PROMPT_TEMPLATE_SHA256
    expected_prior_successful_derivative_count: int = 612


@dataclass(frozen=True)
class ValidationCheck:
    name: str
    severity: str
    passed: bool
    expected: Any
    actual: Any
    detail: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "severity": self.severity,
            "passed": self.passed,
            "expected": self.expected,
            "actual": self.actual,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ValidationResult:
    passed: bool
    sidecar_path: str
    sidecar_size: int
    sidecar_sha256: str
    run_id: str
    run_status: str
    logical_sha256: str
    counts: Mapping[str, int]
    tokens: Mapping[str, int]
    checks: tuple[ValidationCheck, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "checks": [value.as_dict() for value in self.checks],
            "counts": dict(self.counts),
            "failed_error_checks": [value.name for value in self.checks if value.severity == "error" and not value.passed],
            "llm_requests": 0,
            "logical_sha256": self.logical_sha256,
            "network_requests": 0,
            "passed": self.passed,
            "run_id": self.run_id,
            "run_status": self.run_status,
            "sidecar_path": self.sidecar_path,
            "sidecar_sha256": self.sidecar_sha256,
            "sidecar_size": self.sidecar_size,
            "tokens": dict(self.tokens),
            "vision_requests": 0,
        }


def _file_sha(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(chunk_size):
            digest.update(block)
    return digest.hexdigest()


def _sidecars(path: Path) -> tuple[str, ...]:
    return tuple(
        str(value)
        for value in (
            Path(str(path) + "-wal"),
            Path(str(path) + "-shm"),
            Path(str(path) + "-journal"),
        )
        if value.exists()
    )


def _open(path: Path) -> sqlite3.Connection:
    if _sidecars(path):
        raise ValueError(f"immutable SQLite input has sidecars: {_sidecars(path)}")
    connection = sqlite3.connect(
        f"file:{path.resolve().as_posix()}?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _check(
    checks: list[ValidationCheck],
    name: str,
    passed: bool,
    expected: Any,
    actual: Any,
    detail: Any = None,
    severity: str = "error",
) -> None:
    checks.append(ValidationCheck(name, severity, bool(passed), expected, actual, detail))


def _load_manifest(path: Path, contract: ValidationContract) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != contract.manifest_byte_sha256:
        raise ValueError("v1.1 manifest byte SHA mismatch")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict) or raw != (canonical_json(payload) + "\n").encode("utf-8"):
        raise ValueError("v1.1 manifest is not canonical JSON plus LF")
    body = dict(payload)
    stored = body.pop("semantic_coverage_manifest_sha256", None)
    replayed = canonical_sha256({"domain": contract.manifest_domain, "manifest": body})
    expected = {
        "self": (stored, contract.manifest_self_sha256),
        "replayed": (replayed, contract.manifest_self_sha256),
        "seed": (payload.get("sample_seed"), contract.sample_seed),
        "buildings": (payload.get("sample_size_buildings"), FIXED_BUILDING_COUNT),
        "occurrences": (payload.get("planned_occurrence_count"), contract.occurrence_count),
        "buildings_sha": (payload.get("ordered_building_manifest_sha256"), contract.ordered_building_manifest_sha256),
        "occurrences_sha": (payload.get("ordered_occurrence_manifest_sha256"), contract.ordered_occurrence_manifest_sha256),
        "urls_sha": (payload.get("ordered_fetch_url_manifest_sha256"), contract.ordered_fetch_url_manifest_sha256),
        "pixels_sha": (payload.get("ordered_e1_pixel_manifest_sha256"), contract.ordered_e1_pixel_manifest_sha256),
    }
    bad = {key: {"actual": actual, "expected": wanted} for key, (actual, wanted) in expected.items() if actual != wanted}
    if bad:
        raise ValueError("v1.1 manifest identity mismatch: " + canonical_json(bad))
    return payload, raw


def _manifest_rows(payload: Mapping[str, Any], contract: ValidationContract) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    buildings: list[dict[str, Any]] = []
    occurrences: list[dict[str, Any]] = []
    for building_rank, wrapper in enumerate(payload["selected_buildings"], 1):
        selected = wrapper["selected_building"]
        building = selected["building"]
        buildings.append(
            {
                "building_rank": building_rank,
                "selection_id": building["selection_id"],
                "source": building["source"],
                "source_building_id": building["source_building_id"],
                "population_stratum": building["population_stratum"],
                "guard_name": selected["lane"],
                "qa_fallback": int(bool(building["qa_fallback"])),
                "building_record_sha256": selected["building_record_sha256"],
                "coverage_plan_record_sha256": selected["coverage_record_sha256"],
                "selected_building_record_sha256": wrapper["selected_building_record_sha256"],
                "manifest_json": canonical_json(wrapper),
            }
        )
        for wrapped in selected["coverage_plan"]["selected_occurrences"]:
            occurrence = wrapped["occurrence"]
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
                    "occurrence_record_sha256": wrapped["occurrence_record_sha256"],
                    "manifest_json": canonical_json(wrapped),
                }
            )
    if len(buildings) != FIXED_BUILDING_COUNT or len(occurrences) != contract.occurrence_count:
        raise ValueError("v1.1 manifest row accounting mismatch")
    if len({row["fetch_url"] for row in occurrences}) != len(occurrences) or len({row["expected_e1_pixel_sha256"] for row in occurrences}) != len(occurrences):
        raise ValueError("v1.1 manifest selection is not identity-distinct")
    for row in occurrences:
        parsed = urlsplit(str(row["fetch_url"]))
        if parsed.scheme != "https" or parsed.hostname != ALLOWED_HOSTS[row["source"]]:
            raise ValueError("v1.1 manifest contains an invalid fetch host")
    return buildings, occurrences


def _row_mismatches(expected: Sequence[Mapping[str, Any]], actual: Sequence[Mapping[str, Any]], keys: Sequence[str]) -> list[dict[str, Any]]:
    left = {tuple(row[key] for key in keys): dict(row) for row in expected}
    right = {tuple(row[key] for key in keys): dict(row) for row in actual}
    return [
        {"identity": identity, "expected": left.get(identity), "actual": right.get(identity)}
        for identity in sorted(set(left) | set(right))
        if left.get(identity) != right.get(identity)
    ]


# This list is intentionally duplicated from the published v1.1 digest
# contract.  Importing it from the runner would make runner and validator
# repeat the same bug instead of independently detecting it.
_LOGICAL_TABLES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("semantic_runs", "run_id", (
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
        "publication_contract_version",
    )),
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

_COVERAGE_SLOTS = (
    "aerial_context", "construction_or_archive", "detail", "drawing_other",
    "drawing_plan", "drawing_section", "exterior_context", "exterior_overall",
    "interior", "model_or_render",
)


def _logical_sha256(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    digest.update((LOGICAL_MANIFEST_VERSION + "\n").encode("ascii"))
    for table, order_by, columns in _LOGICAL_TABLES:
        digest.update((table + "\0").encode("utf-8"))
        for row in connection.execute(
            f"SELECT {','.join(columns)} FROM {table} ORDER BY {order_by}"
        ):
            digest.update(canonical_json(dict(zip(columns, row))).encode("utf-8") + b"\n")
    return digest.hexdigest()


def _schema_objects_sha256(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    digest.update(b"cross-source-semantic-vision-n10-v1.1-schema-objects-v1\n")
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


def _render_report(status: str, logical: str, metrics: Mapping[str, Any]) -> str:
    return "\n".join(
        [
            "# Cross-source semantic Vision N10 v1.1", "",
            f"- Status: `{status}`", f"- Logical SHA-256: `{logical}`",
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


def _prior_derivatives(payload: Mapping[str, Any]) -> tuple[frozenset[str], int, tuple[str, ...]]:
    exclusions = payload["exclusions"]
    names = tuple(sorted(str(row["name"]) for row in exclusions["prior_run_inputs"]))
    records = exclusions["records"]["occurrence_records"]
    values = frozenset(
        str(row["derivative_pixel_sha256"])
        for row in records
        if row.get("derivative_pixel_sha256") is not None
    )
    declared = int(exclusions["prior_successful_derivative_pixel_count"])
    if len(values) != declared:
        raise ValueError("prior derivative exclusion ledger count mismatch")
    return values, declared, names


def _request_ledger_checks(
    connection: sqlite3.Connection,
    run_id: str,
    run: sqlite3.Row,
    occurrences: Sequence[Mapping[str, Any]],
) -> tuple[bool, dict[str, Any]]:
    occurrence_by_id = {str(row["inference_id"]): row for row in occurrences}
    inputs = {
        str(row["inference_id"]): row
        for row in connection.execute(
            "SELECT * FROM vision_inputs WHERE run_id=?", (run_id,)
        )
    }
    reservations = list(connection.execute(
        "SELECT * FROM external_call_reservations WHERE run_id=? ORDER BY reservation_id",
        (run_id,),
    ))
    completions = {
        int(row["reservation_id"]): row
        for row in connection.execute(
            "SELECT c.* FROM external_call_completions c JOIN external_call_reservations r USING(reservation_id) WHERE r.run_id=?",
            (run_id,),
        )
    }
    errors: list[str] = []
    for reservation in reservations:
        request_json = str(reservation["request_json"])
        try:
            request = json.loads(request_json)
        except json.JSONDecodeError as exc:
            errors.append(f"reservation {reservation['reservation_id']} invalid request JSON: {exc}")
            continue
        if canonical_json(request) != request_json:
            errors.append(f"reservation {reservation['reservation_id']} request is not canonical JSON")
        if hashlib.sha256(request_json.encode("utf-8")).hexdigest() != reservation["request_sha256"]:
            errors.append(f"reservation {reservation['reservation_id']} request SHA mismatch")
        if int(reservation["reservation_id"]) not in completions:
            errors.append(f"reservation {reservation['reservation_id']} is open")
        if reservation["call_kind"] == "http":
            source_row = occurrence_by_id.get(str(reservation["inference_id"]))
            expected = {
                "attempt_no": int(reservation["attempt_no"]),
                "hop_no": int(reservation["hop_no"]),
                "inference_id": str(reservation["inference_id"]),
                "request_url": request.get("request_url"),
                "source": None if source_row is None else source_row["source"],
            }
            if request != expected:
                errors.append(f"HTTP reservation {reservation['reservation_id']} request replay mismatch")
            if source_row is None:
                errors.append(f"HTTP reservation {reservation['reservation_id']} has no selected occurrence")
            elif int(reservation["hop_no"]) == 1 and request.get("request_url") != source_row["fetch_url"]:
                errors.append(f"HTTP reservation {reservation['reservation_id']} first-hop URL mismatch")
            expected_key = f"fetch:{reservation['inference_id']}:{reservation['attempt_no']}:{reservation['hop_no']}"
        else:
            ids = request.get("inference_ids")
            if not isinstance(ids, list) or not all(isinstance(value, str) for value in ids):
                errors.append(f"Vision reservation {reservation['reservation_id']} IDs malformed")
                ids = []
            replay_prompt = hashlib.sha256(compose_prompt(ids).encode("utf-8")).hexdigest()
            if request.get("prompt_sha256") != replay_prompt:
                errors.append(f"Vision reservation {reservation['reservation_id']} prompt replay mismatch")
            if request.get("batch_no") != reservation["batch_no"] or request.get("attempt_no") != reservation["attempt_no"]:
                errors.append(f"Vision reservation {reservation['reservation_id']} batch/attempt mismatch")
            if request.get("model") != run["model"] or request.get("reasoning") != run["reasoning"] or request.get("service_tier") != run["service_tier"]:
                errors.append(f"Vision reservation {reservation['reservation_id']} runtime envelope mismatch")
            if request.get("output_schema_sha256") != run["output_schema_sha256"]:
                errors.append(f"Vision reservation {reservation['reservation_id']} schema mismatch")
            ordered_inputs = request.get("ordered_inputs")
            expected_inputs = []
            for inference_id in ids:
                state = inputs.get(inference_id)
                if state is None:
                    expected_inputs.append(None)
                else:
                    expected_inputs.append(
                        {
                            "derivative_encoded_sha256": state["derivative_encoded_sha256"],
                            "derivative_pixel_sha256": state["derivative_pixel_sha256"],
                            "inference_id": inference_id,
                        }
                    )
            if ordered_inputs != expected_inputs or any(value is None for value in expected_inputs):
                errors.append(f"Vision reservation {reservation['reservation_id']} ordered derivative binding mismatch")
            expected_key = f"vision:{reservation['batch_no']}:{reservation['attempt_no']}"
        if reservation["logical_key"] != expected_key:
            errors.append(f"reservation {reservation['reservation_id']} logical key mismatch")

    used_http: list[int] = []
    for attempt in connection.execute(
        "SELECT * FROM fetch_attempts WHERE run_id=? ORDER BY inference_id,attempt_no", (run_id,)
    ):
        ids = json.loads(str(attempt["reservation_ids_json"]))
        source_row = occurrence_by_id.get(str(attempt["inference_id"]))
        if source_row is None or attempt["request_url"] != source_row["fetch_url"]:
            errors.append(f"fetch {attempt['inference_id']}/{attempt['attempt_no']} selected URL mismatch")
        if not ids:
            errors.append(f"fetch {attempt['inference_id']}/{attempt['attempt_no']} has no physical reservation")
            continue
        used_http.extend(int(value) for value in ids)
        linked = [row for row in reservations if int(row["reservation_id"]) in set(ids)]
        hops = [int(row["hop_no"]) for row in linked]
        if hops != list(range(1, len(hops) + 1)):
            errors.append(f"fetch {attempt['inference_id']}/{attempt['attempt_no']} hop sequence mismatch")
        if any(row["call_kind"] != "http" or row["inference_id"] != attempt["inference_id"] or row["attempt_no"] != attempt["attempt_no"] for row in linked):
            errors.append(f"fetch {attempt['inference_id']}/{attempt['attempt_no']} reservation link mismatch")
    expected_http = [int(row["reservation_id"]) for row in reservations if row["call_kind"] == "http"]
    if sorted(used_http) != sorted(expected_http) or len(used_http) != len(set(used_http)):
        errors.append("physical HTTP reservations are not linked exactly once")

    vision_reservations = {int(row["reservation_id"]): row for row in reservations if row["call_kind"] == "vision"}
    attempts = list(connection.execute("SELECT * FROM vision_attempts WHERE run_id=? ORDER BY attempt_id", (run_id,)))
    used_vision = [int(row["reservation_id"]) for row in attempts]
    if sorted(used_vision) != sorted(vision_reservations) or len(used_vision) != len(set(used_vision)):
        errors.append("Vision reservations and attempts are not one-to-one")
    for attempt in attempts:
        reservation = vision_reservations.get(int(attempt["reservation_id"]))
        if reservation is None:
            continue
        request = json.loads(str(reservation["request_json"]))
        ids = json.loads(str(attempt["inference_ids_json"]))
        if ids != request.get("inference_ids") or attempt["batch_no"] != reservation["batch_no"] or attempt["attempt_no"] != reservation["attempt_no"]:
            errors.append(f"Vision attempt {attempt['attempt_id']} reservation replay mismatch")
        if attempt["prompt_sha256"] != request.get("prompt_sha256") or attempt["output_schema_sha256"] != run["output_schema_sha256"]:
            errors.append(f"Vision attempt {attempt['attempt_id']} prompt/schema mismatch")
        envelope = {
            "model": run["model"], "reasoning": run["reasoning"],
            "service_tier": run["service_tier"], "runtime_version": run["runtime_version"],
            "cli_version": run["cli_version"], "image_detail": run["image_detail"],
            "sandbox": RUNTIME_SANDBOX, "codex_bin": request.get("codex_bin"),
        }
        if any(attempt[key] != value for key, value in envelope.items()):
            errors.append(f"Vision attempt {attempt['attempt_id']} runtime envelope mismatch")
        source_rows = [occurrence_by_id.get(str(value)) for value in ids]
        ranks = [int(value["input_rank"]) for value in source_rows if value is not None]
        if (
            any(value is None for value in source_rows)
            or ranks != sorted(ranks)
            or {(rank - 1) // FIXED_BATCH_SIZE + 1 for rank in ranks} != {int(attempt["batch_no"])}
        ):
            errors.append(f"Vision attempt {attempt['attempt_id']} fixed batch membership mismatch")
    by_batch: dict[int, list[sqlite3.Row]] = {}
    for attempt in attempts:
        by_batch.setdefault(int(attempt["batch_no"]), []).append(attempt)
    expected_batches: dict[int, list[str]] = {}
    for occurrence in occurrences:
        state = inputs.get(str(occurrence["inference_id"]))
        if state is None or state["status"] not in {"success", "vision_failed"}:
            continue
        batch_no = (int(occurrence["input_rank"]) - 1) // FIXED_BATCH_SIZE + 1
        expected_batches.setdefault(batch_no, []).append(str(occurrence["inference_id"]))
    if set(by_batch) != set(expected_batches):
        errors.append("Vision attempt batch identity set mismatch")
    for batch_no, values in by_batch.items():
        numbers = [int(value["attempt_no"]) for value in values]
        statuses = [str(value["status"]) for value in values]
        memberships = [json.loads(str(value["inference_ids_json"])) for value in values]
        if numbers != list(range(1, len(values) + 1)) or len(values) > FIXED_VISION_ATTEMPTS:
            errors.append(f"Vision batch {batch_no} retry chain mismatch")
        if any(status != "failed" for status in statuses[:-1]):
            errors.append(f"Vision batch {batch_no} has retry after success")
        if statuses and statuses[-1] == "failed" and len(values) != FIXED_VISION_ATTEMPTS:
            errors.append(f"Vision batch {batch_no} terminated before retry budget")
        if any(ids != expected_batches.get(batch_no) for ids in memberships):
            errors.append(f"Vision batch {batch_no} ordered membership mismatch")
        terminal_states = {str(inputs[value]["status"]) for value in expected_batches.get(batch_no, [])}
        expected_state = "success" if statuses and statuses[-1] == "success" else "vision_failed"
        if terminal_states != {expected_state}:
            errors.append(f"Vision batch {batch_no} terminal input state mismatch")
    return not errors, {
        "errors": errors,
        "http_reservations": len(expected_http),
        "vision_reservations": len(vision_reservations),
        "completions": len(completions),
    }


def _event_usage(event: Mapping[str, Any]) -> tuple[int, int, int] | None:
    usage: Any = None
    if event.get("type") == "turn.completed":
        usage = event.get("usage")
    elif event.get("type") == "response.completed" and isinstance(event.get("response"), Mapping):
        usage = event["response"].get("usage")
    if not isinstance(usage, Mapping):
        return None
    details = usage.get("input_tokens_details")
    nested = details.get("cached_tokens") if isinstance(details, Mapping) else None
    try:
        values = (
            int(usage.get("input_tokens") or 0),
            int(usage.get("cached_input_tokens", nested) or 0),
            int(usage.get("output_tokens") or 0),
        )
    except (TypeError, ValueError):
        return None
    return values if all(value >= 0 for value in values) else None


def _assistant_text(item: Any) -> str | None:
    if not isinstance(item, Mapping) or item.get("type") not in {"agent_message", "assistant_message"}:
        return None
    if isinstance(item.get("text"), str):
        return str(item["text"])
    content = item.get("content")
    if not isinstance(content, list):
        return None
    parts = [
        str(part["text"])
        for part in content
        if isinstance(part, Mapping)
        and part.get("type") in {"output_text", "input_text", "text"}
        and isinstance(part.get("text"), str)
    ]
    return "".join(parts) if parts else None


def _parse_stdout(raw: bytes) -> dict[str, Any]:
    text = raw.decode("utf-8")
    final_text: str | None = None
    usage: tuple[int, int, int] | None = None
    event_count = 0
    for raw_line in text.splitlines():
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, Mapping):
            continue
        event_count += 1
        if event.get("type") == "item.completed":
            candidate = _assistant_text(event.get("item"))
            if candidate is not None:
                final_text = candidate
        candidate_usage = _event_usage(event)
        if candidate_usage is not None:
            usage = candidate_usage
    results: list[dict[str, Any]] | None = None
    if final_text is not None:
        decoded = json.loads(final_text)
        if isinstance(decoded, Mapping) and isinstance(decoded.get("results"), list):
            values = decoded["results"]
            if all(isinstance(value, dict) for value in values):
                results = values
    return {"event_count": event_count, "results": results, "usage": usage}


def _payload_checks(
    connection: sqlite3.Connection, run_id: str
) -> tuple[bool, list[str], dict[int, dict[str, Any]]]:
    errors: list[str] = []
    attempts = {
        int(row["attempt_id"]): row
        for row in connection.execute(
            "SELECT * FROM vision_attempts WHERE run_id=?", (run_id,)
        )
    }
    rows = list(connection.execute(
        "SELECT p.* FROM vision_attempt_payloads p JOIN vision_attempts a USING(attempt_id) WHERE a.run_id=? ORDER BY p.attempt_id",
        (run_id,),
    ))
    parsed_payloads: dict[int, dict[str, Any]] = {}
    if {int(row["attempt_id"]) for row in rows} != set(attempts):
        errors.append("Vision attempt payload identity set mismatch")
    for row in rows:
        decoded_payload: dict[str, bytes] = {}
        for prefix in ("stdout", "stderr"):
            try:
                raw = gzip.decompress(bytes(row[f"{prefix}_gzip"]))
            except Exception as exc:
                errors.append(f"attempt {row['attempt_id']} {prefix} gzip invalid: {exc}")
                continue
            if len(raw) != int(row[f"{prefix}_bytes"]) or hashlib.sha256(raw).hexdigest() != row[f"{prefix}_sha256"]:
                errors.append(f"attempt {row['attempt_id']} {prefix} payload identity mismatch")
            decoded_payload[prefix] = raw
        attempt_id = int(row["attempt_id"])
        attempt = attempts.get(attempt_id)
        if attempt is None or "stdout" not in decoded_payload or "stderr" not in decoded_payload:
            continue
        if attempt["raw_events_sha256"] != row["stdout_sha256"]:
            errors.append(f"attempt {attempt_id} raw events SHA mismatch")
        try:
            stderr_text = decoded_payload["stderr"].decode("utf-8")
        except UnicodeDecodeError as exc:
            errors.append(f"attempt {attempt_id} stderr is not UTF-8: {exc}")
            stderr_text = ""
        if row["stderr_excerpt"] != (stderr_text[-8000:] or None):
            errors.append(f"attempt {attempt_id} stderr excerpt mismatch")
        try:
            parsed = _parse_stdout(decoded_payload["stdout"])
        except Exception as exc:
            errors.append(f"attempt {attempt_id} JSONL parse failed: {exc}")
            continue
        stored_usage = (
            attempt["input_tokens"], attempt["cached_input_tokens"], attempt["output_tokens"]
        )
        if parsed["usage"] is None:
            if attempt["status"] == "success" or any(value is not None for value in stored_usage):
                errors.append(f"attempt {attempt_id} usage lineage mismatch")
        elif tuple(int(value or 0) for value in stored_usage) != parsed["usage"]:
            errors.append(f"attempt {attempt_id} usage values mismatch")
        if attempt["status"] == "success":
            expected_ids = json.loads(str(attempt["inference_ids_json"]))
            actual_ids = [value.get("asset_id") for value in (parsed["results"] or [])]
            if actual_ids != expected_ids:
                errors.append(f"attempt {attempt_id} result IDs/order mismatch")
        parsed_payloads[attempt_id] = parsed
    return not errors, errors, parsed_payloads


def _semantic_replay_checks(
    connection: sqlite3.Connection,
    run_id: str,
    buildings: Sequence[Mapping[str, Any]],
    parsed_payloads: Mapping[int, Mapping[str, Any]],
) -> tuple[bool, dict[str, Any]]:
    errors: list[str] = []
    result_rows = list(connection.execute(
        "SELECT r.*,o.selection_id,o.input_rank,v.derivative_pixel_sha256 "
        "FROM semantic_results r JOIN selected_occurrences o USING(run_id,inference_id) "
        "JOIN vision_inputs v USING(run_id,inference_id) WHERE r.run_id=? ORDER BY o.input_rank",
        (run_id,),
    ))
    expected_links: list[dict[str, Any]] = []
    expected_heroes: list[dict[str, Any]] = []
    expected_slots: list[dict[str, Any]] = []
    slot_ranks: dict[tuple[str, str], int] = {}
    for row in result_rows:
        inference_id = str(row["inference_id"])
        try:
            raw = json.loads(str(row["raw_result_json"]))
            normalized = normalize_result(raw, inference_id)
        except Exception as exc:
            errors.append(f"result {inference_id} normalization failed: {exc}")
            continue
        event_results = parsed_payloads.get(int(row["attempt_id"]), {}).get("results") or []
        event_row = next(
            (value for value in event_results if value.get("asset_id") == inference_id), None
        )
        attempt_status = connection.execute(
            "SELECT status FROM vision_attempts WHERE attempt_id=?", (row["attempt_id"],)
        ).fetchone()
        if event_row is None or canonical_json(event_row) != row["raw_result_json"] or attempt_status is None or attempt_status[0] != "success":
            errors.append(f"result {inference_id} raw payload lineage mismatch")
        if canonical_json(normalized) != row["normalized_result_json"]:
            errors.append(f"result {inference_id} normalized JSON mismatch")
        scalar = {
            "in_scope": int(normalized["in_scope"]), "reject_reason": normalized["reject_reason"],
            "medium": normalized["medium"], "spatial_context": normalized["spatial_context"],
            "framing_scale": normalized["framing_scale"], "camera_angle": normalized["camera_angle"],
            "drawing_kind": normalized["drawing_kind"], "project_state": normalized["project_state"],
            "project_legibility": normalized["project_legibility"],
            "uncertain_axes_json": canonical_json(list(normalized["uncertain_axes"])),
            "resolution_insufficient": int(normalized["resolution_insufficient"]),
            "evidence": normalized["evidence"],
        }
        if any(row[key] != value for key, value in scalar.items()):
            errors.append(f"result {inference_id} scalar columns mismatch")
        result_body = {"attempt_id": int(row["attempt_id"]), "inference_id": inference_id, "normalized": normalized, "raw": raw}
        if row["record_sha256"] != canonical_sha256(result_body):
            errors.append(f"result {inference_id} record SHA mismatch")
        link = {"inference_id": inference_id, "result_inference_id": inference_id, "reuse_basis": "same_occurrence", "verified_input_pixel_sha256": row["derivative_pixel_sha256"]}
        expected_links.append({**link, "record_sha256": canonical_sha256(link)})
        tier, reasons = derive_hero_decision(normalized)
        hero = {"inference_id": inference_id, "tier": tier, "reasons": tuple(reasons), "authoritative": False}
        expected_heroes.append({"inference_id": inference_id, "tier": tier, "reasons_json": canonical_json(tuple(reasons)), "authoritative": 0, "record_sha256": canonical_sha256(hero)})
        for slot in derive_coverage_slots(normalized):
            key = (str(row["selection_id"]), slot)
            rank = slot_ranks.get(key, 0) + 1
            slot_ranks[key] = rank
            body = {"assignment_rank": rank, "inference_id": inference_id, "selection_id": key[0], "slot": slot, "state": "observed"}
            expected_slots.append({**body, "record_sha256": canonical_sha256(body)})
    for building in buildings:
        for slot in _COVERAGE_SLOTS:
            key = (str(building["selection_id"]), slot)
            if key in slot_ranks:
                continue
            body = {"assignment_rank": 0, "inference_id": None, "selection_id": key[0], "slot": slot, "state": "not_observed_in_sample"}
            expected_slots.append({**body, "record_sha256": canonical_sha256(body)})
    actual_links = [dict(row) for row in connection.execute("SELECT inference_id,result_inference_id,reuse_basis,verified_input_pixel_sha256,record_sha256 FROM occurrence_result_links WHERE run_id=? ORDER BY inference_id", (run_id,))]
    actual_heroes = [dict(row) for row in connection.execute("SELECT inference_id,tier,reasons_json,authoritative,record_sha256 FROM hero_candidate_decisions WHERE run_id=? ORDER BY inference_id", (run_id,))]
    actual_slots = [dict(row) for row in connection.execute("SELECT selection_id,slot,assignment_rank,state,inference_id,record_sha256 FROM coverage_slot_assignments WHERE run_id=? ORDER BY selection_id,slot,assignment_rank", (run_id,))]
    if sorted(expected_links, key=lambda row: row["inference_id"]) != actual_links:
        errors.append("occurrence result links replay mismatch")
    if sorted(expected_heroes, key=lambda row: row["inference_id"]) != actual_heroes:
        errors.append("hero decision replay mismatch")
    if sorted(expected_slots, key=lambda row: (row["selection_id"], row["slot"], row["assignment_rank"])) != actual_slots:
        errors.append("coverage slot replay mismatch")
    return not errors, {"errors": errors, "results": len(result_rows)}


def _cache_checks(
    connection: sqlite3.Connection, run_id: str, cache_dir: Path
) -> tuple[bool, dict[str, Any]]:
    import struct
    from canonical.divisare_vision_benchmark import decode_source

    rows = list(connection.execute(
        "SELECT * FROM vision_inputs WHERE run_id=? AND derivative_encoded_sha256 IS NOT NULL ORDER BY inference_id",
        (run_id,),
    ))
    expected_names = {f"{row['inference_id']}.jpg" for row in rows}
    actual_names = {path.name for path in cache_dir.glob("*.jpg") if path.is_file()} if cache_dir.is_dir() else set()
    errors: list[str] = []
    for row in rows:
        path = cache_dir / f"{row['inference_id']}.jpg"
        if not path.is_file():
            errors.append(f"missing {path.name}")
            continue
        raw = path.read_bytes()
        try:
            decoded = decode_source(raw)
            pixel = hashlib.sha256(b"RGB\0" + struct.pack(">II", decoded.width, decoded.height) + decoded.image.tobytes()).hexdigest()
            actual = (hashlib.sha256(raw).hexdigest(), pixel, decoded.width, decoded.height, len(raw))
        except Exception as exc:
            errors.append(f"{path.name} decode failed: {exc}")
            continue
        expected = (row["derivative_encoded_sha256"], row["derivative_pixel_sha256"], row["derivative_width"], row["derivative_height"], row["derivative_bytes"])
        if actual != expected:
            errors.append(f"{path.name} derivative identity mismatch")
    if actual_names != expected_names:
        errors.append("review cache filename set mismatch")
    return not errors, {"expected_files": len(expected_names), "actual_files": len(actual_names), "errors": errors}


def validate_semantic_n10_v11(
    sidecar_path: Path | str,
    report_path: Path | str,
    manifest_path: Path | str,
    e2_path: Path | str,
    e3_path: Path | str,
    review_cache_dir: Path | str,
    *,
    contract: ValidationContract,
) -> ValidationResult:
    """Independently validate one terminal v1.1 sidecar with zero requests."""

    sidecar = Path(sidecar_path).resolve()
    report = Path(report_path).resolve()
    manifest = Path(manifest_path).resolve()
    e2 = Path(e2_path).resolve()
    e3 = Path(e3_path).resolve()
    cache = Path(review_cache_dir).resolve()
    paths = (sidecar, manifest, e2, e3)
    if any(not value.is_file() for value in paths) or not report.is_file():
        missing = [str(value) for value in (*paths, report) if not value.is_file()]
        raise FileNotFoundError("missing validator inputs: " + canonical_json(missing))
    sidecars_before = {str(value): _sidecars(value) for value in paths}
    if any(sidecars_before.values()):
        raise ValueError("immutable validator input has SQLite sidecars: " + canonical_json(sidecars_before))
    before = {str(value): (value.stat().st_size, _file_sha(value)) for value in paths}
    payload, _ = _load_manifest(manifest, contract)
    buildings, occurrences = _manifest_rows(payload, contract)
    prior_pixels, prior_count, prior_names = _prior_derivatives(payload)
    checks: list[ValidationCheck] = []
    run_id = ""
    run_status = ""
    logical = ""
    counts: dict[str, int] = {}
    tokens: dict[str, int] = {}
    connection = _open(sidecar)
    try:
        app_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        _check(checks, "application_id", app_id == APPLICATION_ID, APPLICATION_ID, app_id)
        _check(checks, "user_version", user_version == SCHEMA_VERSION, SCHEMA_VERSION, user_version)
        tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        triggers = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        _check(checks, "required_tables", REQUIRED_TABLES.issubset(tables), sorted(REQUIRED_TABLES), sorted(tables))
        _check(checks, "required_v11_triggers", REQUIRED_V11_TRIGGERS.issubset(triggers), sorted(REQUIRED_V11_TRIGGERS), sorted(triggers))
        runs = connection.execute("SELECT * FROM semantic_runs").fetchall()
        _check(checks, "single_terminal_run", len(runs) == 1 and str(runs[0]["status"]) in TERMINAL_STATUSES if runs else False, "one terminal run", len(runs))
        if len(runs) != 1:
            raise ValueError("semantic sidecar must contain exactly one run")
        run = runs[0]
        run_id, run_status = str(run["run_id"]), str(run["status"])
        output_schema_sha = hashlib.sha256(canonical_json(OUTPUT_SCHEMA).encode("utf-8")).hexdigest()
        expected_run = {
            "runner_version": RUNNER_VERSION, "schema_version": SCHEMA_VERSION,
            "manifest_size": manifest.stat().st_size,
            "manifest_byte_sha256": contract.manifest_byte_sha256,
            "manifest_self_sha256": contract.manifest_self_sha256,
            "ordered_building_manifest_sha256": contract.ordered_building_manifest_sha256,
            "ordered_occurrence_manifest_sha256": contract.ordered_occurrence_manifest_sha256,
            "sample_seed": contract.sample_seed, "building_count": FIXED_BUILDING_COUNT,
            "occurrence_count": contract.occurrence_count,
            "e2_size": e2.stat().st_size, "e2_sha256_before": _file_sha(e2),
            "e3_size": e3.stat().st_size, "e3_sha256_before": _file_sha(e3),
            "contract_version": CONTRACT_VERSION, "prompt_version": PROMPT_VERSION,
            "output_schema_sha256": output_schema_sha, "transform_version": TRANSFORM_VERSION,
            "schema_sql_sha256": contract.expected_schema_sql_sha256,
            "schema_objects_sha256": contract.expected_schema_objects_sha256,
            "e1_contract_version": FINGERPRINT_CONTRACT_VERSION,
            "runtime_version": RUNTIME_VERSION,
            "retry_policy_version": RETRY_POLICY_VERSION, "batch_size": FIXED_BATCH_SIZE,
            "max_fetch_attempts": FIXED_FETCH_ATTEMPTS, "max_vision_attempts": FIXED_VISION_ATTEMPTS,
            "image_detail": CLI_IMAGE_DETAIL,
            "manifest_domain": contract.manifest_domain,
            "ordered_fetch_url_manifest_sha256": contract.ordered_fetch_url_manifest_sha256,
            "ordered_e1_pixel_manifest_sha256": contract.ordered_e1_pixel_manifest_sha256,
            "inference_id_prefix": contract.inference_id_prefix,
            "max_model_call_reservations": contract.max_model_call_reservations,
            "observed_input_token_stop_threshold": contract.observed_input_token_stop_threshold,
            "observed_output_token_stop_threshold": contract.observed_output_token_stop_threshold,
            "observed_total_token_stop_threshold": contract.observed_total_token_stop_threshold,
            "publication_contract_version": PUBLICATION_CONTRACT_VERSION,
        }
        mismatches = {key: {"expected": value, "actual": run[key]} for key, value in expected_run.items() if run[key] != value}
        _check(checks, "literal_runtime_contract", not mismatches, expected_run, {key: run[key] for key in expected_run}, mismatches)
        _check(checks, "prompt_template_pin", PROMPT_TEMPLATE_SHA256 == contract.expected_prompt_template_sha256, contract.expected_prompt_template_sha256, PROMPT_TEMPLATE_SHA256)
        cli_match = re.search(r"codex-cli\s+(\d+)\.(\d+)\.(\d+)", str(run["cli_version"] or ""))
        cli_tuple = tuple(int(value) for value in cli_match.groups()) if cli_match else ()
        _check(
            checks,
            "supported_codex_cli_version",
            bool(cli_tuple and cli_tuple >= (0, 146, 0)),
            ">= codex-cli 0.146.0",
            run["cli_version"],
        )
        _check(checks, "schema_sql_pin", run["schema_sql_sha256"] == contract.expected_schema_sql_sha256 and bool(re.fullmatch(r"[0-9a-f]{64}", contract.expected_schema_sql_sha256)), contract.expected_schema_sql_sha256, run["schema_sql_sha256"])
        actual_schema_objects_sha = _schema_objects_sha256(connection)
        _check(
            checks,
            "schema_objects_pin",
            actual_schema_objects_sha == contract.expected_schema_objects_sha256
            and run["schema_objects_sha256"] == contract.expected_schema_objects_sha256,
            contract.expected_schema_objects_sha256,
            {"sqlite_master": actual_schema_objects_sha, "stored": run["schema_objects_sha256"]},
        )

        actual_buildings = [dict(row) for row in connection.execute("SELECT building_rank,selection_id,source,source_building_id,population_stratum,guard_name,qa_fallback,building_record_sha256,coverage_plan_record_sha256,selected_building_record_sha256,manifest_json FROM selected_buildings WHERE run_id=? ORDER BY building_rank", (run_id,))]
        actual_occurrences = [dict(row) for row in connection.execute("SELECT input_rank,inference_id,selection_id,occurrence_rank,source,source_building_id,source_asset_id,candidate_id,fetch_url,expected_response_sha256,expected_e1_pixel_sha256,expected_e1_width,expected_e1_height,e2_asset_record_sha256,e2_relation_record_sha256,e3_candidate_record_sha256,e3_ranking_record_sha256,e3_shortlist_record_sha256,occurrence_record_sha256,manifest_json FROM selected_occurrences WHERE run_id=? ORDER BY input_rank", (run_id,))]
        _check(checks, "manifest_building_rows", actual_buildings == buildings, buildings, actual_buildings, _row_mismatches(buildings, actual_buildings, ("building_rank",)))
        _check(checks, "manifest_occurrence_rows", actual_occurrences == occurrences, occurrences, actual_occurrences, _row_mismatches(occurrences, actual_occurrences, ("input_rank",)))

        events = list(connection.execute("SELECT * FROM runtime_events WHERE run_id=? AND event_type='actual_derivative_exclusion_gate' ORDER BY event_id", (run_id,)))
        gate_detail = json.loads(str(events[0]["detail_json"])) if len(events) == 1 else {}
        derivatives = [str(row[0]) for row in connection.execute("SELECT derivative_pixel_sha256 FROM vision_inputs WHERE run_id=? AND derivative_pixel_sha256 IS NOT NULL ORDER BY inference_id", (run_id,))]
        gate_ok = (
            len(events) == 1 and prior_names == ("semantic_n100_v1", "semantic_n10_v1")
            and prior_count == contract.expected_prior_successful_derivative_count
            and len(derivatives) == len(set(derivatives)) and not (set(derivatives) & prior_pixels)
            and gate_detail == {
                "new_ready_derivative_count": len(derivatives),
                "prior_run_names": ["semantic_n10_v1", "semantic_n100_v1"],
                "prior_successful_derivative_count": prior_count,
                "vision_reservations_before_gate": 0,
            }
        )
        vision_before = 0
        if len(events) == 1:
            vision_before = int(connection.execute("SELECT COUNT(*) FROM external_call_reservations WHERE run_id=? AND call_kind='vision' AND reserved_at < ?", (run_id, events[0]["created_at"])).fetchone()[0])
        gate_ok = gate_ok and vision_before == 0
        _check(checks, "actual_derivative_exclusion_gate", gate_ok, {"prior_count": contract.expected_prior_successful_derivative_count, "prior_names": ["semantic_n10_v1", "semantic_n100_v1"], "vision_before": 0, "unique_disjoint": True}, {"event_count": len(events), "detail": gate_detail, "prior_count": prior_count, "prior_names": prior_names, "vision_before": vision_before, "derivatives": len(derivatives)})

        ledger_ok, ledger_detail = _request_ledger_checks(
            connection, run_id, run, occurrences
        )
        _check(checks, "independent_reservation_replay", ledger_ok, "all physical calls durably reserved/completed/replayed", ledger_detail)
        model_reservations = int(connection.execute("SELECT COUNT(*) FROM external_call_reservations WHERE run_id=? AND call_kind='vision'", (run_id,)).fetchone()[0])
        _check(checks, "independent_model_call_cap", model_reservations <= contract.max_model_call_reservations, contract.max_model_call_reservations, model_reservations)
        payload_ok, payload_errors, parsed_payloads = _payload_checks(connection, run_id)
        _check(checks, "attempt_payload_integrity", payload_ok, "valid deterministic gzip payload identities", payload_errors)
        semantic_ok, semantic_detail = _semantic_replay_checks(
            connection, run_id, buildings, parsed_payloads
        )
        _check(checks, "semantic_derivation_replay", semantic_ok, "normalized/result/link/hero/coverage replay", semantic_detail)
        cache_ok, cache_detail = _cache_checks(connection, run_id, cache)
        _check(checks, "review_cache_integrity", cache_ok, "exact retained derivative cache", cache_detail)

        input_status = {str(row[0]): int(row[1]) for row in connection.execute("SELECT status,COUNT(*) FROM vision_inputs WHERE run_id=? GROUP BY status ORDER BY status", (run_id,))}
        results = int(connection.execute("SELECT COUNT(*) FROM semantic_results WHERE run_id=?", (run_id,)).fetchone()[0])
        success = input_status.get("success", 0)
        pending = input_status.get("pending", 0) + input_status.get("ready", 0)
        _check(checks, "independent_result_accounting", results == success, success, results)
        _check(checks, "independent_no_pending", pending == 0, 0, pending)
        attempts = int(connection.execute("SELECT COUNT(*) FROM vision_attempts WHERE run_id=?", (run_id,)).fetchone()[0])
        fetch_attempts = int(connection.execute("SELECT COUNT(*) FROM fetch_attempts WHERE run_id=?", (run_id,)).fetchone()[0])
        usage = connection.execute("SELECT COALESCE(SUM(input_tokens),0),COALESCE(SUM(cached_input_tokens),0),COALESCE(SUM(output_tokens),0),SUM(input_tokens IS NULL OR output_tokens IS NULL) FROM vision_attempts WHERE run_id=?", (run_id,)).fetchone()
        tokens = {"input_tokens": int(usage[0]), "cached_input_tokens": int(usage[1]), "output_tokens": int(usage[2]), "unknown_usage_attempts": int(usage[3] or 0)}
        reservations = {str(row[0]): int(row[1]) for row in connection.execute("SELECT call_kind,COUNT(*) FROM external_call_reservations WHERE run_id=? GROUP BY call_kind", (run_id,))}
        downloaded = int(connection.execute("SELECT COALESCE(SUM(response_bytes),0) FROM fetch_attempts WHERE run_id=?", (run_id,)).fetchone()[0])
        metrics = json.loads(str(run["metrics_json"]))
        expected_metrics_core = {
            "buildings": FIXED_BUILDING_COUNT, "occurrences": contract.occurrence_count,
            "input_status": input_status, "fetch_attempts": fetch_attempts,
            "downloaded_bytes": downloaded, "vision_attempts": attempts,
            "successful_vision_attempts": int(connection.execute("SELECT COUNT(*) FROM vision_attempts WHERE run_id=? AND status='success'", (run_id,)).fetchone()[0]),
            "results": results, **tokens,
            "total_tokens_excluding_cached_double_count": tokens["input_tokens"] + tokens["output_tokens"],
            "physical_http_reservations": reservations.get("http", 0),
            "model_call_reservations": reservations.get("vision", 0),
            "model_call_reservation_cap": contract.max_model_call_reservations,
            "reservation_completions": int(connection.execute("SELECT COUNT(*) FROM external_call_completions c JOIN external_call_reservations r USING(reservation_id) WHERE r.run_id=?", (run_id,)).fetchone()[0]),
            "open_reservations": int(connection.execute("SELECT COUNT(*) FROM external_call_reservations r LEFT JOIN external_call_completions c USING(reservation_id) WHERE r.run_id=? AND c.reservation_id IS NULL", (run_id,)).fetchone()[0]),
        }
        metric_mismatches = {key: {"expected": value, "actual": metrics.get(key)} for key, value in expected_metrics_core.items() if metrics.get(key) != value}
        cache_provenance = metrics.get("materialization_cache", {})
        if cache_provenance.get("path") != str(cache) or cache_provenance.get("retained_for_review") is not True:
            metric_mismatches["materialization_cache"] = {"expected": {"path": str(cache), "retained_for_review": True}, "actual": cache_provenance}
        _check(checks, "metrics_replay", not metric_mismatches, expected_metrics_core, metrics, metric_mismatches)
        threshold_ok = (
            metrics.get("token_threshold_semantics") == "observed post-call stop threshold; unknown usage makes totals lower bounds"
            and int(run["observed_input_token_stop_threshold"]) == contract.observed_input_token_stop_threshold
            and int(run["observed_output_token_stop_threshold"]) == contract.observed_output_token_stop_threshold
            and int(run["observed_total_token_stop_threshold"]) == contract.observed_total_token_stop_threshold
        )
        _check(checks, "truthful_token_threshold", threshold_ok, "post-call observed lower-bound stop threshold", metrics.get("token_threshold_semantics"))

        stored_validations = {str(row[0]): (str(row[1]), int(row[2])) for row in connection.execute("SELECT validation_name,severity,passed FROM validations WHERE run_id=?", (run_id,))}
        missing_required = {name: stored_validations.get(name) for name in REQUIRED_VALIDATIONS if stored_validations.get(name) != ("error", 1)}
        _check(checks, "stored_required_validations", not missing_required, {name: ("error", 1) for name in REQUIRED_VALIDATIONS}, stored_validations, missing_required)
        quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
        _check(checks, "sqlite_quick_check", quick == "ok", "ok", quick)
        _check(checks, "sqlite_integrity_check", integrity == "ok", "ok", integrity)
        _check(checks, "foreign_key_check", not foreign, [], foreign)
        logical = _logical_sha256(connection)
        _check(checks, "independent_logical_sha256", logical == run["logical_sha256"], run["logical_sha256"], logical)
        report_expected = _render_report(run_status, logical, metrics).encode("utf-8")
        report_actual = report.read_bytes()
        _check(checks, "deterministic_report_binding", report_actual == report_expected, hashlib.sha256(report_expected).hexdigest(), hashlib.sha256(report_actual).hexdigest())
        counts = {
            "buildings": len(actual_buildings), "occurrences": len(actual_occurrences),
            "results": results, "fetch_attempts": fetch_attempts,
            "vision_attempts": attempts, "http_reservations": reservations.get("http", 0),
            "model_call_reservations": reservations.get("vision", 0),
        }
    finally:
        connection.close()
    after = {str(value): (value.stat().st_size, _file_sha(value)) for value in paths}
    sidecars_after = {str(value): _sidecars(value) for value in paths}
    _check(checks, "inputs_immutable", before == after, before, after)
    _check(checks, "no_sqlite_sidecars", not any(sidecars_after.values()), {}, sidecars_after)
    failed = [value for value in checks if value.severity == "error" and not value.passed]
    return ValidationResult(
        not failed, str(sidecar), sidecar.stat().st_size, _file_sha(sidecar),
        run_id, run_status, logical, counts, tokens, tuple(checks),
    )


__all__ = [
    "ValidationCheck", "ValidationContract", "ValidationResult",
    "validate_semantic_n10_v11",
]
