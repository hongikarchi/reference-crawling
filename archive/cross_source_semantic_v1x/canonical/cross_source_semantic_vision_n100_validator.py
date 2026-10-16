"""Independent, immutable validator for the frozen semantic Vision N100.

This module deliberately does not import the N100 runner.  All manifest pins,
runtime limits, schema identifiers, trigger names, logical-digest columns, and
report rendering rules are literal.  It performs no network or model work.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
import struct
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from canonical.cross_source_semantic_vision import (
    CONTRACT_VERSION,
    OUTPUT_SCHEMA,
    PROMPT_VERSION,
    TRANSFORM_VERSION,
    compose_prompt,
    derive_coverage_slots,
    derive_hero_decision,
    normalize_result,
)
from canonical.cross_source_semantic_coverage_n100 import PriorN10Spec
from canonical.cross_source_semantic_coverage_n100_validator import (
    validate_semantic_coverage_n100_manifest,
)
from canonical.cross_source_semantic_coverage_sources import ArtifactSpec


APPLICATION_ID = 0x53564E31
SCHEMA_VERSION = 2
RUNNER_VERSION = "cross-source-semantic-vision-n100-runner-v1.0.0"
RETRY_POLICY_VERSION = "cross-source-semantic-vision-n100-retry-v1.0.0"
LOGICAL_MANIFEST_VERSION = "cross-source-semantic-vision-n100-logical-v1"
MANIFEST_DOMAIN = "archibe-cross-source-semantic-coverage-n100-manifest-v1"
FROZEN_MANIFEST_BYTE_SHA256 = (
    "37d917dc2c5b7c89308641b97035255e1fab5e469a0fc86935bf716559ead540"
)
FROZEN_MANIFEST_SELF_SHA256 = (
    "9d34f68ceba5dc561169ed5c3b6fecfc9284dc36fb6ffd544be9198f9faf80ec"
)
FROZEN_SAMPLE_SEED = "archibe-semantic-coverage-n100-v1"
FROZEN_BUILDING_COUNT = 100
FROZEN_OCCURRENCE_COUNT = 562
FROZEN_ORDERED_BUILDING_SHA256 = (
    "edfc89773225828378c56e7cb7c9a9ef8bb5768ed91c50dc03ae464ec6299802"
)
FROZEN_ORDERED_OCCURRENCE_SHA256 = (
    "f8492352487b33540dd25861f299f0ea55079f4fd9395899257d52de7c487e8b"
)
FROZEN_ORDERED_FETCH_URL_SHA256 = (
    "c43fd188322ace59378bb23d0e0b14bca4da172c3109b25273451a60d69e71a7"
)
FROZEN_ORDERED_E1_PIXEL_SHA256 = (
    "9d1332006d72a59b40e1d3c1a66e5abe8cd0f719207e7be0ef765058b4654b62"
)
SELECTION_VERSION = "archibe-cross-source-semantic-coverage-n100-v1"
E1_CONTRACT_VERSION = "archibe-e1-fingerprint-v1"
FIXED_BATCH_SIZE = 5
FIXED_FETCH_ATTEMPTS = 3
FIXED_VISION_ATTEMPTS = 2
PLANNED_BATCH_COUNT = 113
MAX_TOTAL_FETCH_RETRIES = 30
MAX_TOTAL_VISION_RETRIES = 12
MAX_TOTAL_MODEL_CALLS = 125
MAX_TOTAL_INPUT_TOKENS = 2_600_000
MAX_TOTAL_OUTPUT_TOKENS = 100_000
MAX_TOTAL_TOKENS = 2_700_000
HTTP_CIRCUIT_THRESHOLD = 5
VISION_CIRCUIT_THRESHOLD = 3
FROZEN_N10_SHA256 = (
    "30cfdce39b8ac0ecc0d1de0b52f05a5f1f5d7bec7390c03439096118e08ee31a"
)
REPORT_LOGICAL_PLACEHOLDER = "0" * 64
ALLOWED_HOSTS = {
    "architizer": "architizer-prod.imgix.net",
    "divisare": "images.divisare.com",
}
COVERAGE_SLOTS = (
    "aerial_context",
    "construction_or_archive",
    "detail",
    "drawing_other",
    "drawing_plan",
    "drawing_section",
    "exterior_context",
    "exterior_overall",
    "interior",
    "model_or_render",
)
REQUIRED_STORED_VALIDATIONS = (
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
REQUIRED_TRIGGERS = frozenset(
    {
        "semantic_runs_single",
        "semantic_runs_provenance_immutable",
        "semantic_runs_status_transition",
        "semantic_runs_terminal_immutable",
        "semantic_runs_no_delete",
        "selected_buildings_insert",
        "selected_buildings_no_update",
        "selected_buildings_no_delete",
        "selected_occurrences_insert",
        "selected_occurrences_no_update",
        "selected_occurrences_no_delete",
        "vision_inputs_insert",
        "vision_inputs_update",
        "vision_inputs_transition",
        "vision_inputs_no_delete",
        "fetch_attempts_insert",
        "fetch_attempts_no_update",
        "fetch_attempts_no_delete",
        "vision_attempts_insert",
        "vision_attempts_no_update",
        "vision_attempts_no_delete",
        "payloads_insert",
        "payloads_no_update",
        "payloads_no_delete",
        "semantic_results_insert",
        "semantic_results_no_update",
        "semantic_results_no_delete",
        "links_insert",
        "links_no_update",
        "links_no_delete",
        "hero_insert",
        "hero_no_update",
        "hero_no_delete",
        "slots_insert",
        "slots_no_update",
        "slots_no_delete",
        "validations_write",
        "validations_no_update",
        "validations_no_delete",
        "runtime_events_insert",
        "runtime_events_no_update",
        "runtime_events_no_delete",
    }
)
TRIGGER_SQL_SHA256 = (
    "e9ba0cc3bb0060d9ea5be0ebec260a3a3dfaf09a6c44591151d8399b60ab7330"
)

# Exact, literal schema surface.  The validator intentionally does not import
# the runtime schema or runner constants, so widening a table is visible here.
EXPECTED_TABLE_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "coverage_slot_assignments": (
        "run_id", "selection_id", "slot", "assignment_rank", "state",
        "inference_id", "record_sha256",
    ),
    "fetch_attempts": (
        "run_id", "inference_id", "attempt_no", "request_url", "final_url",
        "started_at", "completed_at", "elapsed_ms", "outcome", "http_status",
        "content_type", "response_bytes", "expected_response_sha256",
        "actual_response_sha256", "expected_e1_pixel_sha256",
        "actual_e1_pixel_sha256", "retryable", "retry_after_seconds",
        "scheduled_delay_seconds", "error_kind", "error_message",
    ),
    "hero_candidate_decisions": (
        "run_id", "inference_id", "tier", "reasons_json", "authoritative",
        "record_sha256",
    ),
    "occurrence_result_links": (
        "run_id", "inference_id", "result_inference_id", "reuse_basis",
        "verified_input_pixel_sha256", "record_sha256",
    ),
    "runtime_events": (
        "event_id", "run_id", "event_type", "detail_json", "created_at",
    ),
    "selected_buildings": (
        "run_id", "building_rank", "selection_id", "source",
        "source_building_id", "population_stratum", "guard_name", "qa_fallback",
        "building_record_sha256", "coverage_plan_record_sha256",
        "selected_building_record_sha256", "manifest_json",
    ),
    "selected_occurrences": (
        "run_id", "input_rank", "inference_id", "selection_id",
        "occurrence_rank", "source", "source_building_id", "source_asset_id",
        "candidate_id", "fetch_url", "expected_response_sha256",
        "expected_e1_pixel_sha256", "expected_e1_width", "expected_e1_height",
        "e2_asset_record_sha256", "e2_relation_record_sha256",
        "e3_candidate_record_sha256", "e3_ranking_record_sha256",
        "e3_shortlist_record_sha256", "occurrence_record_sha256", "manifest_json",
    ),
    "semantic_results": (
        "run_id", "inference_id", "attempt_id", "raw_result_json",
        "normalized_result_json", "in_scope", "reject_reason", "medium",
        "spatial_context", "framing_scale", "camera_angle", "drawing_kind",
        "project_state", "project_legibility", "uncertain_axes_json",
        "resolution_insufficient", "evidence", "record_sha256",
    ),
    "semantic_runs": (
        "run_id", "status", "runner_version", "schema_version", "manifest_path",
        "manifest_size", "manifest_byte_sha256", "manifest_self_sha256",
        "ordered_building_manifest_sha256", "ordered_occurrence_manifest_sha256",
        "sample_seed", "building_count", "occurrence_count", "e2_path", "e2_size",
        "e2_sha256_before", "e2_sha256_after", "e2_logical_sha256", "e2_run_id",
        "e3_path", "e3_size", "e3_sha256_before", "e3_sha256_after",
        "e3_logical_sha256", "e3_run_id", "contract_version", "prompt_version",
        "output_schema_sha256", "transform_version", "e1_contract_version",
        "dependency_manifest_json", "dependency_manifest_sha256", "runtime_version",
        "retry_policy_version", "batch_size", "max_fetch_attempts",
        "max_vision_attempts", "model", "reasoning", "service_tier", "image_detail",
        "cli_version", "started_at", "completed_at", "metrics_json",
        "logical_sha256", "error",
    ),
    "validations": (
        "run_id", "validation_name", "severity", "passed", "expected", "actual",
        "detail",
    ),
    "vision_attempt_payloads": (
        "attempt_id", "codec", "stdout_gzip", "stdout_bytes", "stdout_sha256",
        "stderr_gzip", "stderr_bytes", "stderr_sha256", "stderr_excerpt",
    ),
    "vision_attempts": (
        "attempt_id", "run_id", "batch_no", "attempt_no", "inference_ids_json",
        "status", "model", "reasoning", "service_tier", "runtime_version",
        "cli_version", "codex_bin", "image_detail", "sandbox", "prompt_sha256",
        "output_schema_sha256", "started_at", "completed_at", "elapsed_ms",
        "input_tokens", "cached_input_tokens", "output_tokens", "raw_events_sha256",
        "error_kind", "error_message",
    ),
    "vision_inputs": (
        "run_id", "inference_id", "status", "selected_fetch_attempt_no",
        "actual_response_sha256", "actual_e1_pixel_sha256",
        "derivative_encoded_sha256", "derivative_pixel_sha256", "derivative_width",
        "derivative_height", "derivative_bytes", "completed_at", "error_kind",
        "error_message",
    ),
}


class N100ValidationError(RuntimeError):
    pass


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
class N100ValidationResult:
    passed: bool
    sidecar_path: str
    sidecar_size: int
    sidecar_sha256: str
    run_id: str
    run_status: str
    logical_sha256: str
    counts: Mapping[str, Any]
    tokens: Mapping[str, int]
    checks: tuple[ValidationCheck, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "checks": [value.as_dict() for value in self.checks],
            "counts": dict(self.counts),
            "failed_error_checks": [
                value.name
                for value in self.checks
                if value.severity == "error" and not value.passed
            ],
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


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _file_sha(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk_size), b""):
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
    connection = sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
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
    checks.append(
        ValidationCheck(name, severity, bool(passed), expected, actual, detail)
    )


def _load_manifest(path: Path) -> tuple[dict[str, Any], bytes]:
    if not path.is_file():
        raise FileNotFoundError(path)
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != FROZEN_MANIFEST_BYTE_SHA256:
        raise N100ValidationError("frozen N100 manifest byte SHA mismatch")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise N100ValidationError("N100 manifest is not UTF-8 JSON") from exc
    if not isinstance(payload, dict) or raw != (_json(payload) + "\n").encode("utf-8"):
        raise N100ValidationError("N100 manifest is not canonical JSON plus LF")
    body = dict(payload)
    stored = body.pop("semantic_coverage_manifest_sha256", None)
    replayed = _sha({"domain": MANIFEST_DOMAIN, "manifest": body})
    pins = (
        stored == FROZEN_MANIFEST_SELF_SHA256,
        replayed == FROZEN_MANIFEST_SELF_SHA256,
        payload.get("sample_seed") == FROZEN_SAMPLE_SEED,
        payload.get("sample_size_buildings") == FROZEN_BUILDING_COUNT,
        payload.get("planned_occurrence_count") == FROZEN_OCCURRENCE_COUNT,
        payload.get("planned_unique_e1_pixel_count") == FROZEN_OCCURRENCE_COUNT,
        payload.get("ordered_building_manifest_sha256")
        == FROZEN_ORDERED_BUILDING_SHA256,
        payload.get("ordered_occurrence_manifest_sha256")
        == FROZEN_ORDERED_OCCURRENCE_SHA256,
        payload.get("ordered_fetch_url_manifest_sha256")
        == FROZEN_ORDERED_FETCH_URL_SHA256,
        payload.get("ordered_e1_pixel_manifest_sha256")
        == FROZEN_ORDERED_E1_PIXEL_SHA256,
    )
    if not all(pins):
        raise N100ValidationError("frozen N100 manifest literal pins mismatch")
    return payload, raw


def _manifest_rows(
    payload: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    buildings: list[dict[str, Any]] = []
    occurrences: list[dict[str, Any]] = []
    input_rank = 0
    for building_rank, wrapper in enumerate(payload["selected_buildings"], 1):
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
                "guard_name": selected.get(
                    "sampling_stratum",
                    selected.get("guard_name", building["population_stratum"]),
                ),
                "qa_fallback": int(bool(building["qa_fallback"])),
                "building_record_sha256": building["selection_record_sha256"],
                "coverage_plan_record_sha256": selected[
                    "coverage_plan_record_sha256"
                ],
                "selected_building_record_sha256": wrapper[
                    "selected_building_record_sha256"
                ],
                "manifest_json": _json(wrapper),
            }
        )
        for occurrence_wrapper in plan["selected_occurrences"]:
            input_rank += 1
            occurrence = occurrence_wrapper["occurrence"]
            candidate = occurrence["candidate"]
            source = str(candidate["source"])
            parsed = urlsplit(str(candidate["fetch_url"]))
            if (
                source not in ALLOWED_HOSTS
                or parsed.scheme.casefold() != "https"
                or parsed.hostname != ALLOWED_HOSTS[source]
            ):
                raise N100ValidationError(f"manifest URL host mismatch: {input_rank}")
            occurrences.append(
                {
                    "input_rank": input_rank,
                    "inference_id": f"semv100_{input_rank:06d}",
                    "selection_id": building["selection_id"],
                    "occurrence_rank": occurrence["occurrence_rank"],
                    "source": source,
                    "source_building_id": candidate["source_building_id"],
                    "source_asset_id": candidate["source_asset_id"],
                    "candidate_id": candidate["candidate_id"],
                    "fetch_url": candidate["fetch_url"],
                    "expected_response_sha256": candidate["raw_response_sha256"],
                    "expected_e1_pixel_sha256": candidate[
                        "normalized_pixel_sha256"
                    ],
                    "expected_e1_width": candidate["normalized_width"],
                    "expected_e1_height": candidate["normalized_height"],
                    "e2_asset_record_sha256": candidate["e2_asset_record_sha256"],
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
                    "manifest_json": _json(occurrence_wrapper),
                }
            )
    if len(buildings) != FROZEN_BUILDING_COUNT or len(occurrences) != FROZEN_OCCURRENCE_COUNT:
        raise N100ValidationError("decoded frozen N100 population mismatch")
    if len({row["fetch_url"] for row in occurrences}) != FROZEN_OCCURRENCE_COUNT:
        raise N100ValidationError("N100 manifest URLs are not unique")
    if len({row["expected_e1_pixel_sha256"] for row in occurrences}) != FROZEN_OCCURRENCE_COUNT:
        raise N100ValidationError("N100 manifest E1 pixels are not unique")
    replayed_ordered = {
        "buildings": _sha(
            {
                "ordered_buildings": [
                    {
                        "rank": row["building_rank"],
                        "record_sha256": row["selected_building_record_sha256"],
                    }
                    for row in buildings
                ],
                "version": SELECTION_VERSION,
            }
        ),
        "occurrences": _sha(
            {
                "ordered_occurrences": [
                    {
                        "candidate_id": row["candidate_id"],
                        "occurrence_record_sha256": row["occurrence_record_sha256"],
                        "selection_id": row["selection_id"],
                    }
                    for row in occurrences
                ],
                "version": SELECTION_VERSION,
            }
        ),
        "urls": _sha(
            {
                "ordered_fetch_urls": [
                    {
                        "fetch_url": row["fetch_url"],
                        "input_rank": row["input_rank"],
                        "source": row["source"],
                    }
                    for row in occurrences
                ],
                "version": SELECTION_VERSION,
            }
        ),
        "e1_pixels": _sha(
            {
                "ordered_e1_pixels": [
                    {
                        "input_rank": row["input_rank"],
                        "normalized_pixel_sha256": row["expected_e1_pixel_sha256"],
                        "source": row["source"],
                    }
                    for row in occurrences
                ],
                "version": SELECTION_VERSION,
            }
        ),
    }
    stored_ordered = {
        "buildings": payload.get("ordered_building_manifest_sha256"),
        "occurrences": payload.get("ordered_occurrence_manifest_sha256"),
        "urls": payload.get("ordered_fetch_url_manifest_sha256"),
        "e1_pixels": payload.get("ordered_e1_pixel_manifest_sha256"),
    }
    if replayed_ordered != stored_ordered:
        raise N100ValidationError(
            "N100 ordered manifest SHA replay mismatch: "
            + _json({"actual": stored_ordered, "expected": replayed_ordered})
        )
    return buildings, occurrences


def _mismatches(
    expected: Sequence[Mapping[str, Any]],
    actual: Sequence[Mapping[str, Any]],
    keys: Sequence[str],
) -> list[dict[str, Any]]:
    left = {tuple(row[key] for key in keys): dict(row) for row in expected}
    right = {tuple(row[key] for key in keys): dict(row) for row in actual}
    return [
        {"identity": identity, "expected": left.get(identity), "actual": right.get(identity)}
        for identity in sorted(set(left) | set(right))
        if left.get(identity) != right.get(identity)
    ]


def _event_usage(event: Mapping[str, Any]) -> tuple[int, int, int] | None:
    usage: Any = None
    if event.get("type") == "turn.completed":
        usage = event.get("usage")
    elif event.get("type") == "response.completed" and isinstance(
        event.get("response"), Mapping
    ):
        usage = event["response"].get("usage")
    if not isinstance(usage, Mapping):
        return None
    details = usage.get("input_tokens_details")
    nested = details.get("cached_tokens") if isinstance(details, Mapping) else None
    try:
        result = (
            int(usage.get("input_tokens") or 0),
            int(usage.get("cached_input_tokens", nested) or 0),
            int(usage.get("output_tokens") or 0),
        )
    except (TypeError, ValueError):
        return None
    return result if all(value >= 0 for value in result) else None


def _assistant_text(item: Any) -> str | None:
    if not isinstance(item, Mapping) or item.get("type") not in {
        "agent_message",
        "assistant_message",
    }:
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
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Vision stdout is not UTF-8") from exc
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


def _derivative_identity(raw: bytes) -> tuple[str, str, int, int, int]:
    from PIL import Image
    from io import BytesIO

    with Image.open(BytesIO(raw)) as image:
        converted = image.convert("RGB")
        converted.load()
        width, height = converted.size
        pixel_sha = hashlib.sha256(
            b"RGB\0" + struct.pack(">II", width, height) + converted.tobytes()
        ).hexdigest()
    return hashlib.sha256(raw).hexdigest(), pixel_sha, width, height, len(raw)


def _validate_artifact(
    checks: list[ValidationCheck],
    label: str,
    path: Path,
    record: Mapping[str, Any],
    run: sqlite3.Row,
) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    sidecars = _sidecars(path)
    size = path.stat().st_size
    byte_sha = _file_sha(path)
    connection = _open(path)
    try:
        app = int(connection.execute("PRAGMA application_id").fetchone()[0])
        user = int(connection.execute("PRAGMA user_version").fetchone()[0])
        quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        table = "e2_runs" if label == "e2" else "selection_runs"
        metric_table = "e2_metrics" if label == "e2" else "selection_metrics"
        source_runs = connection.execute(f"SELECT * FROM {table}").fetchall()
        source_run = source_runs[0] if len(source_runs) == 1 else None
        source_run_id = str(source_run["run_id"]) if source_run is not None else None
        logical_rows = (
            connection.execute(
                f"SELECT value_text FROM {metric_table} WHERE run_id=? "
                "AND phase='validation' AND metric_name='output_logical_sha256' "
                "AND stratum_json='{}'",
                (source_run_id,),
            ).fetchall()
            if source_run_id is not None
            else []
        )
        source_logical = (
            str(logical_rows[0][0])
            if len(logical_rows) == 1 and logical_rows[0][0] is not None
            else None
        )
        source_contract = (
            str(source_run["contract_version"]) if source_run is not None else None
        )
        source_builder = (
            str(source_run["builder_version"]) if source_run is not None else None
        )
        source_status = str(source_run["status"]) if source_run is not None else None
        source_mode = (
            str(source_run["selection_mode"]) if source_run is not None else None
        )
        offline = True
        if label == "e3" and source_run is not None:
            offline = (
                int(source_run["network_requests"]) == 0
                and int(source_run["vision_requests"]) == 0
                and int(source_run["llm_requests"]) == 0
                and str(source_run["artifact_scope"]) == "candidate_only"
            )
    finally:
        connection.close()
    expected = {
        "size": int(record["size_bytes"]),
        "byte_sha": str(record["byte_sha256"]),
        "application_id": int(record["application_id"]),
        "user_version": int(record["user_version"]),
        "run_id": str(record["run_id"]),
        "logical_sha": str(record["logical_sha256"]),
        "contract_version": str(record["contract_version"]),
        "builder_version": str(record["builder_version"]),
        "status": "complete",
        "selection_mode": "full",
        "path": str(path.resolve()),
    }
    actual = {
        "size": size,
        "byte_sha": byte_sha,
        "application_id": app,
        "user_version": user,
        "run_id": source_run_id,
        "logical_sha": source_logical,
        "contract_version": source_contract,
        "builder_version": source_builder,
        "status": source_status,
        "selection_mode": source_mode,
        "path": str(path.resolve()),
    }
    run_prefix = label
    run_values = {
        "size": int(run[f"{run_prefix}_size"]),
        "byte_sha": str(run[f"{run_prefix}_sha256_before"]),
        "byte_sha_after": str(run[f"{run_prefix}_sha256_after"]),
        "run_id": str(run[f"{run_prefix}_run_id"]),
        "logical_sha": str(run[f"{run_prefix}_logical_sha256"]),
        "path": str(Path(str(run[f"{run_prefix}_path"])).resolve()),
    }
    _check(
        checks,
        f"{label}_lineage",
        actual == expected
        and not sidecars
        and quick == "ok"
        and integrity == "ok"
        and foreign == 0
        and offline
        and str(Path(str(record["path"])).resolve()) == expected["path"]
        and run_values["byte_sha_after"] == expected["byte_sha"]
        and all(
            run_values[key] == expected[key]
            for key in ("size", "byte_sha", "run_id", "logical_sha", "path")
        ),
        expected,
        {**actual, "offline": offline, "sidecars": sidecars, "quick": quick, "integrity": integrity, "fk": foreign, "run": run_values},
    )
    return actual


def _independent_manifest_replay(
    *,
    payload: Mapping[str, Any],
    manifest_path: Path,
    e2_path: Path,
    e3_path: Path,
    n10_manifest_path: Path,
    n10_sidecar_path: Path,
) -> dict[str, Any]:
    """Replay the frozen selection without importing the Vision runner."""

    e2 = payload["e2_input"]
    e3 = payload["e3_input"]
    n10_manifest = payload["n10_manifest_input"]
    n10_sidecar = payload["n10_semantic_sidecar_input"]
    e2_spec = ArtifactSpec(
        "e2_evidence",
        e2_path,
        int(e2["size_bytes"]),
        str(e2["byte_sha256"]),
        str(e2["logical_sha256"]),
        str(e2["run_id"]),
        int(e2["application_id"]),
        int(e2["user_version"]),
    )
    e3_spec = ArtifactSpec(
        "e3_selection",
        e3_path,
        int(e3["size_bytes"]),
        str(e3["byte_sha256"]),
        str(e3["logical_sha256"]),
        str(e3["run_id"]),
        int(e3["application_id"]),
        int(e3["user_version"]),
    )
    n10_spec = PriorN10Spec(
        manifest_path=n10_manifest_path,
        sidecar_path=n10_sidecar_path,
        expected_manifest_size=int(n10_manifest["size_bytes"]),
        expected_manifest_byte_sha256=str(n10_manifest["byte_sha256"]),
        expected_manifest_self_sha256=str(n10_manifest["manifest_self_sha256"]),
        expected_sidecar_size=int(n10_sidecar["size_bytes"]),
        expected_sidecar_byte_sha256=str(n10_sidecar["byte_sha256"]),
        expected_sidecar_logical_sha256=str(n10_sidecar["logical_sha256"]),
        expected_run_id=str(n10_sidecar["run_id"]),
        expected_application_id=int(n10_sidecar["application_id"]),
        expected_user_version=int(n10_sidecar["user_version"]),
    )
    replay = validate_semantic_coverage_n100_manifest(
        manifest_path,
        e2_spec=e2_spec,
        e3_spec=e3_spec,
        n10_spec=n10_spec,
        expected_seed=FROZEN_SAMPLE_SEED,
    )
    return replay.as_dict()


def validate_frozen_n100_manifest_preflight(
    manifest_path: Path,
    *,
    e2_path: Path,
    e3_path: Path,
    n10_manifest_path: Path,
    n10_sidecar_path: Path,
) -> dict[str, Any]:
    """Pin and independently replay the production manifest before any fetch."""

    resolved = tuple(
        value.resolve()
        for value in (
            manifest_path,
            e2_path,
            e3_path,
            n10_manifest_path,
            n10_sidecar_path,
        )
    )
    if len(set(resolved)) != len(resolved):
        raise ValueError("all N100 preflight input paths must be distinct")
    manifest_path, e2_path, e3_path, n10_manifest_path, n10_sidecar_path = resolved
    payload, _ = _load_manifest(manifest_path)
    _manifest_rows(payload)
    result = _independent_manifest_replay(
        payload=payload,
        manifest_path=manifest_path,
        e2_path=e2_path,
        e3_path=e3_path,
        n10_manifest_path=n10_manifest_path,
        n10_sidecar_path=n10_sidecar_path,
    )
    if result.get("passed") is not True:
        raise N100ValidationError(
            "independent frozen N100 manifest replay failed: " + _json(result)
        )
    return result


def _load_n10(
    checks: list[ValidationCheck], path: Path, manifest_record: Mapping[str, Any]
) -> tuple[set[str], set[str], dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    sidecars = _sidecars(path)
    byte_sha = _file_sha(path)
    connection = _open(path)
    try:
        app = int(connection.execute("PRAGMA application_id").fetchone()[0])
        user = int(connection.execute("PRAGMA user_version").fetchone()[0])
        run = connection.execute("SELECT * FROM semantic_runs").fetchone()
        buildings = {
            str(row[0])
            for row in connection.execute("SELECT selection_id FROM selected_buildings")
        }
        pixels = {
            str(row[0])
            for row in connection.execute(
                "SELECT derivative_pixel_sha256 FROM vision_inputs "
                "WHERE derivative_pixel_sha256 IS NOT NULL"
            )
        }
        quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    finally:
        connection.close()
    actual = {
        "application_id": app,
        "byte_sha256": byte_sha,
        "logical_sha256": str(run["logical_sha256"]) if run else None,
        "run_id": str(run["run_id"]) if run else None,
        "size_bytes": path.stat().st_size,
        "status": str(run["status"]) if run else None,
        "user_version": user,
    }
    expected = {
        key: manifest_record[key]
        for key in actual
    }
    _check(
        checks,
        "n10_lineage",
        actual == expected
        and byte_sha == FROZEN_N10_SHA256
        and not sidecars
        and quick == "ok"
        and integrity == "ok"
        and foreign == 0
        and len(buildings) == 10
        and len(pixels) == 57,
        expected,
        {**actual, "buildings": len(buildings), "pixels": len(pixels), "sidecars": sidecars, "quick": quick, "integrity": integrity, "fk": foreign},
    )
    return buildings, pixels, actual


LOGICAL_TABLES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("semantic_runs", "run_id", ("run_id","runner_version","schema_version","manifest_size","manifest_byte_sha256","manifest_self_sha256","ordered_building_manifest_sha256","ordered_occurrence_manifest_sha256","sample_seed","building_count","occurrence_count","e2_size","e2_sha256_before","e2_logical_sha256","e2_run_id","e3_size","e3_sha256_before","e3_logical_sha256","e3_run_id","contract_version","prompt_version","output_schema_sha256","transform_version","e1_contract_version","dependency_manifest_json","dependency_manifest_sha256","runtime_version","retry_policy_version","batch_size","max_fetch_attempts","max_vision_attempts","model","reasoning","service_tier","image_detail","cli_version")),
    ("selected_buildings", "building_rank", ("building_rank","selection_id","source","source_building_id","population_stratum","guard_name","qa_fallback","building_record_sha256","coverage_plan_record_sha256","selected_building_record_sha256","manifest_json")),
    ("selected_occurrences", "input_rank", ("input_rank","inference_id","selection_id","occurrence_rank","source","source_building_id","source_asset_id","candidate_id","fetch_url","expected_response_sha256","expected_e1_pixel_sha256","expected_e1_width","expected_e1_height","e2_asset_record_sha256","e2_relation_record_sha256","e3_candidate_record_sha256","e3_ranking_record_sha256","e3_shortlist_record_sha256","occurrence_record_sha256","manifest_json")),
    ("vision_inputs", "inference_id", ("inference_id","status","selected_fetch_attempt_no","actual_response_sha256","actual_e1_pixel_sha256","derivative_encoded_sha256","derivative_pixel_sha256","derivative_width","derivative_height","derivative_bytes","error_kind","error_message")),
    ("fetch_attempts", "inference_id,attempt_no", ("inference_id","attempt_no","request_url","final_url","elapsed_ms","outcome","http_status","content_type","response_bytes","expected_response_sha256","actual_response_sha256","expected_e1_pixel_sha256","actual_e1_pixel_sha256","retryable","retry_after_seconds","scheduled_delay_seconds","error_kind","error_message")),
    ("vision_attempts", "attempt_id", ("attempt_id","batch_no","attempt_no","inference_ids_json","status","model","reasoning","service_tier","runtime_version","cli_version","codex_bin","image_detail","sandbox","prompt_sha256","output_schema_sha256","elapsed_ms","input_tokens","cached_input_tokens","output_tokens","raw_events_sha256","error_kind","error_message")),
    ("vision_attempt_payloads", "attempt_id", ("attempt_id","codec","stdout_bytes","stdout_sha256","stderr_bytes","stderr_sha256","stderr_excerpt")),
    ("semantic_results", "inference_id", ("inference_id","attempt_id","raw_result_json","normalized_result_json","in_scope","reject_reason","medium","spatial_context","framing_scale","camera_angle","drawing_kind","project_state","project_legibility","uncertain_axes_json","resolution_insufficient","evidence","record_sha256")),
    ("occurrence_result_links", "inference_id", ("inference_id","result_inference_id","reuse_basis","verified_input_pixel_sha256","record_sha256")),
    ("hero_candidate_decisions", "inference_id", ("inference_id","tier","reasons_json","authoritative","record_sha256")),
    ("coverage_slot_assignments", "selection_id,slot,assignment_rank", ("selection_id","slot","assignment_rank","state","inference_id","record_sha256")),
    ("validations", "validation_name", ("validation_name","severity","passed","expected","actual","detail")),
    ("runtime_events", "event_id", ("event_id","event_type","detail_json")),
)


def independent_logical_sha256(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    digest.update((LOGICAL_MANIFEST_VERSION + "\n").encode("ascii"))
    for table, order_by, columns in LOGICAL_TABLES:
        digest.update((table + "\0").encode("utf-8"))
        for row in connection.execute(
            f"SELECT {','.join(columns)} FROM {table} ORDER BY {order_by}"
        ):
            digest.update(_json(dict(zip(columns, row))).encode("utf-8") + b"\n")
    return digest.hexdigest()


def _render_report(status: str, logical: str, metrics: Mapping[str, Any]) -> str:
    return "\n".join(
        [
            "# Cross-source semantic Vision N100",
            "",
            f"- Status: `{status}`",
            f"- Logical SHA-256: `{logical}`",
            f"- Buildings / occurrences: `{metrics['buildings']}` / `{metrics['occurrences']}`",
            f"- Input states: `{_json(metrics['input_status'])}`",
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


def validate_semantic_vision_n100(
    sidecar_path: Path,
    *,
    report_path: Path,
    manifest_path: Path,
    e2_path: Path,
    e3_path: Path,
    n10_manifest_path: Path,
    n10_sidecar_path: Path,
    review_cache_dir: Path,
) -> N100ValidationResult:
    paths = tuple(
        value.resolve()
        for value in (
            sidecar_path,
            report_path,
            manifest_path,
            e2_path,
            e3_path,
            n10_manifest_path,
            n10_sidecar_path,
            review_cache_dir,
        )
    )
    (
        sidecar_path,
        report_path,
        manifest_path,
        e2_path,
        e3_path,
        n10_manifest_path,
        n10_sidecar_path,
        review_cache_dir,
    ) = paths
    if len(set(paths)) != len(paths):
        raise ValueError("all N100 validator paths must be distinct")
    if not sidecar_path.is_file():
        raise FileNotFoundError(sidecar_path)
    sidecars = _sidecars(sidecar_path)
    if sidecars:
        raise N100ValidationError(f"N100 sidecar has SQLite sidecars: {sidecars}")
    payload, manifest_raw = _load_manifest(manifest_path)
    expected_buildings, expected_occurrences = _manifest_rows(payload)
    sidecar_size = sidecar_path.stat().st_size
    sidecar_sha = _file_sha(sidecar_path)
    immutable_before = {
        "sidecar": (sidecar_size, sidecar_sha, _sidecars(sidecar_path)),
        "manifest": (manifest_path.stat().st_size, _file_sha(manifest_path)),
        "e2": (e2_path.stat().st_size, _file_sha(e2_path)),
        "e3": (e3_path.stat().st_size, _file_sha(e3_path)),
        "n10_manifest": (
            n10_manifest_path.stat().st_size,
            _file_sha(n10_manifest_path),
        ),
        "n10": (n10_sidecar_path.stat().st_size, _file_sha(n10_sidecar_path)),
    }
    checks: list[ValidationCheck] = []
    counts: dict[str, Any] = {}
    tokens: dict[str, int] = {}
    publish_leftovers = tuple(
        str(value)
        for value in (
            sidecar_path.with_name(sidecar_path.name + ".partial"),
            Path(str(sidecar_path.with_name(sidecar_path.name + ".partial")) + ".inputs"),
            report_path.with_name(report_path.name + ".partial"),
        )
        if value.exists()
    )
    _check(
        checks,
        "terminal_publish_cleanup",
        not publish_leftovers,
        [],
        list(publish_leftovers),
    )
    lock_path = Path(str(sidecar_path) + ".lock")
    lock_bytes = lock_path.read_bytes() if lock_path.is_file() else None
    _check(
        checks,
        "persistent_advisory_lock_inode",
        lock_bytes == b"1",
        "31",
        lock_bytes.hex() if lock_bytes is not None else None,
    )
    n10_buildings, n10_pixels, _ = _load_n10(
        checks, n10_sidecar_path, payload["n10_semantic_sidecar_input"]
    )
    try:
        manifest_replay = _independent_manifest_replay(
            payload=payload,
            manifest_path=manifest_path,
            e2_path=e2_path,
            e3_path=e3_path,
            n10_manifest_path=n10_manifest_path,
            n10_sidecar_path=n10_sidecar_path,
        )
    except Exception as exc:
        manifest_replay = {
            "error": str(exc),
            "error_type": type(exc).__name__,
            "passed": False,
        }
    _check(
        checks,
        "independent_manifest_replay",
        manifest_replay.get("passed") is True,
        True,
        manifest_replay.get("passed"),
        manifest_replay,
    )

    connection = _open(sidecar_path)
    try:
        app = int(connection.execute("PRAGMA application_id").fetchone()[0])
        user = int(connection.execute("PRAGMA user_version").fetchone()[0])
        _check(checks, "application_id", app == APPLICATION_ID, APPLICATION_ID, app)
        _check(checks, "user_version", user == SCHEMA_VERSION, SCHEMA_VERSION, user)
        actual_schema = {
            str(row[0]): tuple(
                str(value[1])
                for value in connection.execute(f"PRAGMA table_info({row[0]})")
            )
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        }
        _check(
            checks,
            "exact_table_schema",
            actual_schema == EXPECTED_TABLE_COLUMNS,
            EXPECTED_TABLE_COLUMNS,
            actual_schema,
        )
        run_rows = connection.execute("SELECT * FROM semantic_runs").fetchall()
        if len(run_rows) != 1:
            raise N100ValidationError(f"expected one N100 run, found {len(run_rows)}")
        run = run_rows[0]
        run_id = str(run["run_id"])
        run_status = str(run["status"])
        trigger_rows = [
            {"name": str(row[0]), "sql": str(row[1])}
            for row in connection.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
                "ORDER BY name"
            )
        ]
        triggers = frozenset(row["name"] for row in trigger_rows)
        _check(
            checks,
            "trigger_set",
            triggers == REQUIRED_TRIGGERS,
            sorted(REQUIRED_TRIGGERS),
            sorted(triggers),
        )
        _check(
            checks,
            "trigger_sql_sha256",
            _sha(trigger_rows) == TRIGGER_SQL_SHA256,
            TRIGGER_SQL_SHA256,
            _sha(trigger_rows),
        )
        _check(
            checks,
            "terminal_status",
            run_status in {"complete", "complete_with_failures"}
            and run["completed_at"] is not None
            and run["error"] is None,
            {
                "completed_at": "non-null",
                "error": None,
                "status": ["complete", "complete_with_failures"],
            },
            {
                "completed_at": run["completed_at"],
                "error": run["error"],
                "status": run_status,
            },
        )
        runtime_actual = {
            "runner_version": run["runner_version"],
            "schema_version": run["schema_version"],
            "retry_policy_version": run["retry_policy_version"],
            "building_count": run["building_count"],
            "occurrence_count": run["occurrence_count"],
            "batch_size": run["batch_size"],
            "max_fetch_attempts": run["max_fetch_attempts"],
            "max_vision_attempts": run["max_vision_attempts"],
        }
        runtime_expected = {
            "runner_version": RUNNER_VERSION,
            "schema_version": SCHEMA_VERSION,
            "retry_policy_version": RETRY_POLICY_VERSION,
            "building_count": FROZEN_BUILDING_COUNT,
            "occurrence_count": FROZEN_OCCURRENCE_COUNT,
            "batch_size": FIXED_BATCH_SIZE,
            "max_fetch_attempts": FIXED_FETCH_ATTEMPTS,
            "max_vision_attempts": FIXED_VISION_ATTEMPTS,
        }
        _check(
            checks,
            "runtime_limits",
            runtime_actual == runtime_expected,
            runtime_expected,
            runtime_actual,
        )
        contract_actual = {
            "contract_version": run["contract_version"],
            "prompt_version": run["prompt_version"],
            "output_schema_sha256": run["output_schema_sha256"],
            "transform_version": run["transform_version"],
            "e1_contract_version": run["e1_contract_version"],
        }
        contract_expected = {
            "contract_version": CONTRACT_VERSION,
            "prompt_version": PROMPT_VERSION,
            "output_schema_sha256": _sha(OUTPUT_SCHEMA),
            "transform_version": TRANSFORM_VERSION,
            "e1_contract_version": E1_CONTRACT_VERSION,
        }
        _check(
            checks,
            "semantic_contract",
            contract_actual == contract_expected,
            contract_expected,
            contract_actual,
        )
        manifest_actual = {
            "size": run["manifest_size"],
            "byte": run["manifest_byte_sha256"],
            "self": run["manifest_self_sha256"],
            "seed": run["sample_seed"],
            "buildings": run["ordered_building_manifest_sha256"],
            "occurrences": run["ordered_occurrence_manifest_sha256"],
        }
        manifest_expected = {
            "size": len(manifest_raw),
            "byte": FROZEN_MANIFEST_BYTE_SHA256,
            "self": FROZEN_MANIFEST_SELF_SHA256,
            "seed": FROZEN_SAMPLE_SEED,
            "buildings": FROZEN_ORDERED_BUILDING_SHA256,
            "occurrences": FROZEN_ORDERED_OCCURRENCE_SHA256,
        }
        _check(
            checks,
            "manifest_lineage",
            manifest_actual == manifest_expected,
            manifest_expected,
            manifest_actual,
        )
        dependency = json.loads(str(run["dependency_manifest_json"]))
        dependency_contract = dependency.get("n100_runtime_contract", {})
        retry_limits = dependency.get("retry_and_breaker_limits", {})
        dependency_expected_contract = {
            "building_count": FROZEN_BUILDING_COUNT,
            "manifest_byte_sha256": FROZEN_MANIFEST_BYTE_SHA256,
            "manifest_self_sha256": FROZEN_MANIFEST_SELF_SHA256,
            "occurrence_count": FROZEN_OCCURRENCE_COUNT,
            "ordered_building_manifest_sha256": FROZEN_ORDERED_BUILDING_SHA256,
            "ordered_occurrence_manifest_sha256": FROZEN_ORDERED_OCCURRENCE_SHA256,
            "sample_seed": FROZEN_SAMPLE_SEED,
        }
        dependency_limits = {
            "max_total_fetch_retries": MAX_TOTAL_FETCH_RETRIES,
            "max_total_vision_retries": MAX_TOTAL_VISION_RETRIES,
            "max_total_input_tokens": MAX_TOTAL_INPUT_TOKENS,
            "max_total_output_tokens": MAX_TOTAL_OUTPUT_TOKENS,
            "max_total_tokens": MAX_TOTAL_TOKENS,
            "http_circuit_threshold": HTTP_CIRCUIT_THRESHOLD,
            "vision_circuit_threshold": VISION_CIRCUIT_THRESHOLD,
        }
        _check(
            checks,
            "dependency_contract",
            dependency_contract == dependency_expected_contract
            and all(retry_limits.get(key) == value for key, value in dependency_limits.items())
            and dependency.get("n10_sidecar_sha256") == FROZEN_N10_SHA256
            and run["dependency_manifest_sha256"] == _sha(dependency),
            {"contract": dependency_expected_contract, "limits": dependency_limits, "n10": FROZEN_N10_SHA256},
            {"contract": dependency_contract, "limits": retry_limits, "n10": dependency.get("n10_sidecar_sha256")},
        )
        counts["inputs"] = {
            "e2": _validate_artifact(checks, "e2", e2_path, payload["e2_input"], run),
            "e3": _validate_artifact(checks, "e3", e3_path, payload["e3_input"], run),
        }
        building_columns = tuple(expected_buildings[0])
        actual_buildings = [
            dict(row)
            for row in connection.execute(
                f"SELECT {','.join(building_columns)} FROM selected_buildings "
                "WHERE run_id=? ORDER BY building_rank",
                (run_id,),
            )
        ]
        occurrence_columns = tuple(expected_occurrences[0])
        actual_occurrences = [
            dict(row)
            for row in connection.execute(
                f"SELECT {','.join(occurrence_columns)} FROM selected_occurrences "
                "WHERE run_id=? ORDER BY input_rank",
                (run_id,),
            )
        ]
        building_bad = _mismatches(expected_buildings, actual_buildings, ("building_rank",))
        occurrence_bad = _mismatches(expected_occurrences, actual_occurrences, ("input_rank",))
        _check(checks, "manifest_building_rows", not building_bad, 0, len(building_bad), building_bad)
        _check(checks, "manifest_occurrence_rows", not occurrence_bad, 0, len(occurrence_bad), occurrence_bad)
        sidecar_building_ids = {str(row["selection_id"]) for row in actual_buildings}
        _check(
            checks,
            "n10_building_disjoint",
            not (sidecar_building_ids & n10_buildings),
            [],
            sorted(sidecar_building_ids & n10_buildings),
        )
        counts["buildings"] = len(actual_buildings)
        counts["occurrences"] = len(actual_occurrences)
        occurrence_by_id = {str(row["inference_id"]): row for row in actual_occurrences}

        input_rows = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM vision_inputs WHERE run_id=? ORDER BY inference_id",
                (run_id,),
            )
        ]
        input_by_id = {str(row["inference_id"]): row for row in input_rows}
        status_counts = Counter(str(row["status"]) for row in input_rows)
        derivative_pixels = {
            str(row["derivative_pixel_sha256"])
            for row in input_rows
            if row["derivative_pixel_sha256"] is not None
        }
        _check(
            checks,
            "vision_input_population",
            set(input_by_id) == set(occurrence_by_id)
            and len(input_rows) == FROZEN_OCCURRENCE_COUNT,
            FROZEN_OCCURRENCE_COUNT,
            len(input_rows),
        )
        _check(
            checks,
            "no_pending_ready",
            status_counts["pending"] + status_counts["ready"] == 0,
            0,
            status_counts["pending"] + status_counts["ready"],
        )
        duplicate_count = sum(
            count > 1
            for count in Counter(
                str(row["derivative_pixel_sha256"])
                for row in input_rows
                if row["derivative_pixel_sha256"] is not None
            ).values()
        )
        _check(checks, "distinct_derivative_pixels", duplicate_count == 0, 0, duplicate_count)
        _check(
            checks,
            "n10_derivative_disjoint",
            not (derivative_pixels & n10_pixels),
            [],
            sorted(derivative_pixels & n10_pixels),
        )
        counts["input_status"] = dict(sorted(status_counts.items()))

        fetch_rows = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM fetch_attempts WHERE run_id=? "
                "ORDER BY inference_id,attempt_no",
                (run_id,),
            )
        ]
        fetch_by_id: dict[str, list[dict[str, Any]]] = {}
        fetch_bad: list[dict[str, Any]] = []
        for row in fetch_rows:
            inference_id = str(row["inference_id"])
            occurrence = occurrence_by_id.get(inference_id)
            fetch_by_id.setdefault(inference_id, []).append(row)
            if occurrence is None:
                fetch_bad.append({"id": inference_id, "error": "orphan attempt"})
                continue
            parsed_final = urlsplit(str(row["final_url"])) if row["final_url"] else None
            if (
                row["request_url"] != occurrence["fetch_url"]
                or row["expected_response_sha256"] != occurrence["expected_response_sha256"]
                or row["expected_e1_pixel_sha256"] != occurrence["expected_e1_pixel_sha256"]
                or (
                    parsed_final is not None
                    and (
                        parsed_final.scheme.casefold() != "https"
                        or parsed_final.hostname != ALLOWED_HOSTS[str(occurrence["source"])]
                    )
                )
            ):
                fetch_bad.append({"id": inference_id, "attempt": row["attempt_no"], "error": "lineage"})
            if row["outcome"] == "exact_match" and not (
                row["actual_response_sha256"] == row["expected_response_sha256"]
                and row["actual_e1_pixel_sha256"] == row["expected_e1_pixel_sha256"]
                and row["http_status"] == 200
                and row["retryable"] == 0
                and row["error_kind"] is None
            ):
                fetch_bad.append({"id": inference_id, "attempt": row["attempt_no"], "error": "false exact"})
        for inference_id, input_row in input_by_id.items():
            attempts = fetch_by_id.get(inference_id, [])
            numbers = [int(value["attempt_no"]) for value in attempts]
            if numbers != list(range(1, len(attempts) + 1)) or len(attempts) > FIXED_FETCH_ATTEMPTS:
                fetch_bad.append({"id": inference_id, "error": "attempt sequence", "actual": numbers})
            for value in attempts[:-1]:
                if value["retryable"] != 1 or not value["scheduled_delay_seconds"]:
                    fetch_bad.append({"id": inference_id, "error": "invalid intermediate retry"})
            final = attempts[-1] if attempts else None
            status = str(input_row["status"])
            if status in {"success", "vision_failed"}:
                if not final or not (
                    final["outcome"] == "exact_match"
                    and input_row["selected_fetch_attempt_no"] == final["attempt_no"]
                    and input_row["actual_response_sha256"] == occurrence_by_id[inference_id]["expected_response_sha256"]
                    and input_row["actual_e1_pixel_sha256"] == occurrence_by_id[inference_id]["expected_e1_pixel_sha256"]
                    and input_row["derivative_pixel_sha256"] is not None
                ):
                    fetch_bad.append({"id": inference_id, "error": "prepared linkage"})
            elif status == "fetch_failed":
                if not final or final["retryable"] != 0 or input_row["selected_fetch_attempt_no"] is not None:
                    fetch_bad.append({"id": inference_id, "error": "failed linkage"})
            else:
                fetch_bad.append({"id": inference_id, "error": "nonterminal status", "status": status})
        fetch_retries = sum(int(row["attempt_no"]) > 1 for row in fetch_rows)
        _check(checks, "fetch_attempt_replay", not fetch_bad, 0, len(fetch_bad), fetch_bad)
        _check(checks, "fetch_retry_cap", fetch_retries <= MAX_TOTAL_FETCH_RETRIES, MAX_TOTAL_FETCH_RETRIES, fetch_retries)
        counts["fetch_attempts"] = len(fetch_rows)
        counts["downloaded_bytes"] = sum(int(row["response_bytes"] or 0) for row in fetch_rows)

        attempt_rows = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM vision_attempts WHERE run_id=? ORDER BY attempt_id",
                (run_id,),
            )
        ]
        attempt_by_id = {int(row["attempt_id"]): row for row in attempt_rows}
        by_batch: dict[int, list[dict[str, Any]]] = {}
        attempt_bad: list[dict[str, Any]] = []
        schema_sha = _sha(OUTPUT_SCHEMA)
        for row in attempt_rows:
            try:
                ids = json.loads(str(row["inference_ids_json"]))
            except json.JSONDecodeError:
                ids = None
            if not isinstance(ids, list) or not ids or len(ids) > FIXED_BATCH_SIZE or len(ids) != len(set(ids)):
                attempt_bad.append({"attempt": row["attempt_id"], "error": "IDs"})
                continue
            source_rows = [occurrence_by_id.get(str(value)) for value in ids]
            if any(value is None for value in source_rows):
                attempt_bad.append({"attempt": row["attempt_id"], "error": "orphan ID"})
                continue
            ranks = [int(value["input_rank"]) for value in source_rows if value is not None]
            if ranks != sorted(ranks) or {(rank - 1) // FIXED_BATCH_SIZE + 1 for rank in ranks} != {int(row["batch_no"])}:
                attempt_bad.append({"attempt": row["attempt_id"], "error": "batch membership"})
            if row["prompt_sha256"] != hashlib.sha256(compose_prompt(ids).encode("utf-8")).hexdigest() or row["output_schema_sha256"] != schema_sha:
                attempt_bad.append({"attempt": row["attempt_id"], "error": "prompt/schema"})
            for key in ("model", "reasoning", "service_tier", "runtime_version", "cli_version", "image_detail"):
                if row[key] != run[key]:
                    attempt_bad.append({"attempt": row["attempt_id"], "error": key})
            by_batch.setdefault(int(row["batch_no"]), []).append(row)
        for batch_no, values in by_batch.items():
            numbers = [int(value["attempt_no"]) for value in values]
            statuses = [str(value["status"]) for value in values]
            ids = [str(value["inference_ids_json"]) for value in values]
            if numbers != list(range(1, len(values) + 1)) or len(values) > FIXED_VISION_ATTEMPTS or len(set(ids)) != 1:
                attempt_bad.append({"batch": batch_no, "error": "retry chain"})
            if any(status != "failed" for status in statuses[:-1]):
                attempt_bad.append({"batch": batch_no, "error": "success followed"})
            if statuses and statuses[-1] == "failed" and len(values) != FIXED_VISION_ATTEMPTS:
                attempt_bad.append({"batch": batch_no, "error": "early terminal failure"})
        expected_batches: dict[int, list[str]] = {}
        for input_row in input_rows:
            if input_row["status"] not in {"success", "vision_failed"}:
                continue
            occurrence = occurrence_by_id[str(input_row["inference_id"])]
            batch_no = (int(occurrence["input_rank"]) - 1) // FIXED_BATCH_SIZE + 1
            expected_batches.setdefault(batch_no, []).append(
                str(input_row["inference_id"])
            )
        if set(by_batch) != set(expected_batches):
            attempt_bad.append(
                {
                    "error": "batch identity set",
                    "actual": sorted(by_batch),
                    "expected": sorted(expected_batches),
                }
            )
        for batch_no, expected_ids in expected_batches.items():
            values = by_batch.get(batch_no, [])
            if not values:
                continue
            for value in values:
                if json.loads(str(value["inference_ids_json"])) != expected_ids:
                    attempt_bad.append(
                        {"batch": batch_no, "error": "incomplete batch membership"}
                    )
            final_status = str(values[-1]["status"])
            terminal_input_states = {
                str(input_by_id[inference_id]["status"])
                for inference_id in expected_ids
            }
            expected_input_state = (
                "success" if final_status == "success" else "vision_failed"
            )
            if terminal_input_states != {expected_input_state}:
                attempt_bad.append(
                    {
                        "batch": batch_no,
                        "error": "attempt/input terminal state",
                        "attempt": final_status,
                        "inputs": sorted(terminal_input_states),
                    }
                )
        vision_retries = sum(int(row["attempt_no"]) > 1 for row in attempt_rows)
        _check(checks, "vision_attempt_replay", not attempt_bad, 0, len(attempt_bad), attempt_bad)
        _check(
            checks,
            "vision_call_retry_caps",
            len(attempt_rows) <= MAX_TOTAL_MODEL_CALLS and vision_retries <= MAX_TOTAL_VISION_RETRIES,
            {"calls": MAX_TOTAL_MODEL_CALLS, "retries": MAX_TOTAL_VISION_RETRIES},
            {"calls": len(attempt_rows), "retries": vision_retries},
        )

        payload_rows = [dict(row) for row in connection.execute("SELECT * FROM vision_attempt_payloads")]
        payload_bad: list[dict[str, Any]] = []
        parsed_payloads: dict[int, dict[str, Any]] = {}
        if {int(row["attempt_id"]) for row in payload_rows} != set(attempt_by_id):
            payload_bad.append({"error": "payload identity set"})
        for row in payload_rows:
            attempt_id = int(row["attempt_id"])
            try:
                stdout = gzip.decompress(row["stdout_gzip"])
                stderr = gzip.decompress(row["stderr_gzip"])
            except (OSError, EOFError) as exc:
                payload_bad.append({"attempt": attempt_id, "error": str(exc)})
                continue
            if not (
                row["codec"] == "gzip"
                and len(stdout) == row["stdout_bytes"]
                and hashlib.sha256(stdout).hexdigest() == row["stdout_sha256"]
                and len(stderr) == row["stderr_bytes"]
                and hashlib.sha256(stderr).hexdigest() == row["stderr_sha256"]
                and attempt_id in attempt_by_id
                and attempt_by_id[attempt_id]["raw_events_sha256"] == row["stdout_sha256"]
                and row["stderr_excerpt"]
                == (stderr.decode("utf-8")[-8000:] or None)
            ):
                payload_bad.append({"attempt": attempt_id, "error": "gzip/SHA"})
                continue
            try:
                parsed = _parse_stdout(stdout)
            except Exception as exc:
                payload_bad.append({"attempt": attempt_id, "error": f"JSONL:{exc}"})
                continue
            attempt = attempt_by_id[attempt_id]
            stored_usage = (
                attempt["input_tokens"],
                attempt["cached_input_tokens"],
                attempt["output_tokens"],
            )
            if parsed["usage"] is None:
                if attempt["status"] == "success" or any(value is not None for value in stored_usage):
                    payload_bad.append({"attempt": attempt_id, "error": "usage"})
            elif tuple(int(value or 0) for value in stored_usage) != parsed["usage"]:
                payload_bad.append({"attempt": attempt_id, "error": "usage mismatch"})
            if attempt["status"] == "success":
                expected_ids = json.loads(str(attempt["inference_ids_json"]))
                actual_ids = [value.get("asset_id") for value in (parsed["results"] or [])]
                if actual_ids != expected_ids:
                    payload_bad.append({"attempt": attempt_id, "error": "result IDs"})
            parsed_payloads[attempt_id] = parsed
        _check(checks, "raw_jsonl_payloads", not payload_bad, 0, len(payload_bad), payload_bad)

        result_rows = [
            dict(row)
            for row in connection.execute(
                "SELECT r.*,o.selection_id,o.input_rank FROM semantic_results r "
                "JOIN selected_occurrences o USING(run_id,inference_id) "
                "WHERE r.run_id=? ORDER BY o.input_rank",
                (run_id,),
            )
        ]
        normalized_by_id: dict[str, dict[str, Any]] = {}
        result_bad: list[dict[str, Any]] = []
        for row in result_rows:
            inference_id = str(row["inference_id"])
            try:
                raw_result = json.loads(str(row["raw_result_json"]))
                normalized = normalize_result(raw_result, inference_id)
            except Exception as exc:
                result_bad.append({"id": inference_id, "error": str(exc)})
                continue
            normalized_by_id[inference_id] = normalized
            attempt = attempt_by_id.get(int(row["attempt_id"]))
            event_results = parsed_payloads.get(int(row["attempt_id"]), {}).get("results") or []
            event_row = next((value for value in event_results if value.get("asset_id") == inference_id), None)
            expected_columns = {
                "in_scope": int(normalized["in_scope"]),
                "reject_reason": normalized["reject_reason"],
                "medium": normalized["medium"],
                "spatial_context": normalized["spatial_context"],
                "framing_scale": normalized["framing_scale"],
                "camera_angle": normalized["camera_angle"],
                "drawing_kind": normalized["drawing_kind"],
                "project_state": normalized["project_state"],
                "project_legibility": normalized["project_legibility"],
                "uncertain_axes_json": _json(normalized["uncertain_axes"]),
                "resolution_insufficient": int(normalized["resolution_insufficient"]),
                "evidence": normalized["evidence"],
            }
            record_body = {"attempt_id": row["attempt_id"], "inference_id": inference_id, "normalized": normalized, "raw": raw_result}
            if not (
                event_row is not None
                and _json(event_row) == row["raw_result_json"]
                and _json(normalized) == row["normalized_result_json"]
                and all(row[key] == value for key, value in expected_columns.items())
                and row["record_sha256"] == _sha(record_body)
                and attempt is not None
                and attempt["status"] == "success"
            ):
                result_bad.append({"id": inference_id, "error": "result lineage"})
        success_ids = {str(row["inference_id"]) for row in input_rows if row["status"] == "success"}
        result_ids = {str(row["inference_id"]) for row in result_rows}
        if success_ids != result_ids:
            result_bad.append({"error": "success/result set", "missing": sorted(success_ids - result_ids), "orphan": sorted(result_ids - success_ids)})
        _check(checks, "raw_semantic_results", not result_bad, 0, len(result_bad), result_bad)

        expected_links: list[dict[str, Any]] = []
        expected_heroes: list[dict[str, Any]] = []
        expected_slots: list[dict[str, Any]] = []
        slot_ranks: dict[tuple[str, str], int] = {}
        for occurrence in actual_occurrences:
            inference_id = str(occurrence["inference_id"])
            if inference_id not in normalized_by_id:
                continue
            normalized = normalized_by_id[inference_id]
            link = {
                "inference_id": inference_id,
                "result_inference_id": inference_id,
                "reuse_basis": "same_occurrence",
                "verified_input_pixel_sha256": input_by_id[inference_id]["derivative_pixel_sha256"],
            }
            expected_links.append({**link, "record_sha256": _sha(link)})
            tier, reasons = derive_hero_decision(normalized)
            hero = {"inference_id": inference_id, "tier": tier, "reasons": reasons, "authoritative": False}
            expected_heroes.append({"inference_id": inference_id, "tier": tier, "reasons_json": _json(reasons), "authoritative": 0, "record_sha256": _sha(hero)})
            for slot in derive_coverage_slots(normalized):
                key = (str(occurrence["selection_id"]), slot)
                rank = slot_ranks.get(key, 0) + 1
                slot_ranks[key] = rank
                body = {"assignment_rank": rank, "inference_id": inference_id, "selection_id": key[0], "slot": slot, "state": "observed"}
                expected_slots.append({**body, "record_sha256": _sha(body)})
        for building in actual_buildings:
            for slot in COVERAGE_SLOTS:
                key = (str(building["selection_id"]), slot)
                if key in slot_ranks:
                    continue
                body = {"assignment_rank": 0, "inference_id": None, "selection_id": key[0], "slot": slot, "state": "not_observed_in_sample"}
                expected_slots.append({**body, "record_sha256": _sha(body)})
        actual_links = [dict(row) for row in connection.execute("SELECT inference_id,result_inference_id,reuse_basis,verified_input_pixel_sha256,record_sha256 FROM occurrence_result_links WHERE run_id=? ORDER BY inference_id", (run_id,))]
        actual_heroes = [dict(row) for row in connection.execute("SELECT inference_id,tier,reasons_json,authoritative,record_sha256 FROM hero_candidate_decisions WHERE run_id=? ORDER BY inference_id", (run_id,))]
        actual_slots = [dict(row) for row in connection.execute("SELECT selection_id,slot,assignment_rank,state,inference_id,record_sha256 FROM coverage_slot_assignments WHERE run_id=? ORDER BY selection_id,slot,assignment_rank", (run_id,))]
        link_bad = _mismatches(expected_links, actual_links, ("inference_id",))
        hero_bad = _mismatches(expected_heroes, actual_heroes, ("inference_id",))
        slot_bad = _mismatches(expected_slots, actual_slots, ("selection_id", "slot", "assignment_rank"))
        _check(checks, "result_links", not link_bad, 0, len(link_bad), link_bad)
        _check(checks, "hero_replay", not hero_bad, 0, len(hero_bad), hero_bad)
        _check(checks, "coverage_replay", not slot_bad, 0, len(slot_bad), slot_bad)

        input_tokens = sum(int(row["input_tokens"] or 0) for row in attempt_rows)
        cached_tokens = sum(int(row["cached_input_tokens"] or 0) for row in attempt_rows)
        output_tokens = sum(int(row["output_tokens"] or 0) for row in attempt_rows)
        tokens = {
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_tokens,
            "output_tokens": output_tokens,
            "vision_elapsed_ms": sum(int(row["elapsed_ms"] or 0) for row in attempt_rows),
        }
        _check(
            checks,
            "token_envelope",
            input_tokens <= MAX_TOTAL_INPUT_TOKENS
            and output_tokens <= MAX_TOTAL_OUTPUT_TOKENS
            and input_tokens + output_tokens <= MAX_TOTAL_TOKENS,
            {"input": MAX_TOTAL_INPUT_TOKENS, "output": MAX_TOTAL_OUTPUT_TOKENS, "total": MAX_TOTAL_TOKENS},
            {"input": input_tokens, "output": output_tokens, "total": input_tokens + output_tokens},
        )
        event_counts = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                "SELECT event_type,COUNT(*) FROM runtime_events WHERE run_id=? GROUP BY event_type ORDER BY event_type",
                (run_id,),
            )
        }
        try:
            stored_metrics = json.loads(str(run["metrics_json"]))
        except json.JSONDecodeError:
            stored_metrics = None
        cache_provenance = dependency.get("materialization_cache")
        recomputed_metrics = {
            "buildings": FROZEN_BUILDING_COUNT,
            "occurrences": FROZEN_OCCURRENCE_COUNT,
            "planned_batches": PLANNED_BATCH_COUNT,
            "input_status": dict(sorted(status_counts.items())),
            "fetch_attempts": len(fetch_rows),
            "fetch_retries": fetch_retries,
            "downloaded_bytes": counts["downloaded_bytes"],
            "vision_attempts": len(attempt_rows),
            "vision_retries": vision_retries,
            "vision_call_cap": MAX_TOTAL_MODEL_CALLS,
            "successful_vision_attempts": sum(row["status"] == "success" for row in attempt_rows),
            **tokens,
            "results": len(result_rows),
            "runtime_events": event_counts,
            "materialization_cache": cache_provenance,
        }
        _check(checks, "metrics_replay", stored_metrics == recomputed_metrics, recomputed_metrics, stored_metrics)
        counts.update({
            "vision_attempts": len(attempt_rows),
            "vision_retries": vision_retries,
            "results": len(result_rows),
            "hero_decisions": len(actual_heroes),
            "coverage_assignments": len(actual_slots),
            "runtime_events": event_counts,
        })

        cache_bad: list[str] = []
        expected_cache_names = {
            f"{row['inference_id']}.jpg"
            for row in input_rows
            if row["derivative_encoded_sha256"] is not None
        }
        if not isinstance(cache_provenance, Mapping) or not cache_provenance.get("retained_for_review") or Path(str(cache_provenance.get("path"))).resolve() != review_cache_dir:
            cache_bad.append("retained review-cache provenance mismatch")
        if not review_cache_dir.is_dir():
            cache_bad.append("review cache directory missing")
            actual_cache_names: set[str] = set()
        else:
            actual_cache_names = {value.name for value in review_cache_dir.iterdir()}
        if actual_cache_names != expected_cache_names:
            cache_bad.append("review cache name accounting")
        for input_row in input_rows:
            if input_row["derivative_encoded_sha256"] is None:
                continue
            cache_path = review_cache_dir / f"{input_row['inference_id']}.jpg"
            if not cache_path.is_file():
                continue
            try:
                actual_identity = _derivative_identity(cache_path.read_bytes())
            except Exception as exc:
                cache_bad.append(f"{input_row['inference_id']}:decode:{type(exc).__name__}")
                continue
            expected_identity = (
                input_row["derivative_encoded_sha256"],
                input_row["derivative_pixel_sha256"],
                input_row["derivative_width"],
                input_row["derivative_height"],
                input_row["derivative_bytes"],
            )
            if actual_identity != expected_identity:
                cache_bad.append(f"{input_row['inference_id']}:identity")
        _check(checks, "review_cache_integrity", not cache_bad, 0, len(cache_bad), cache_bad)

        stored_validations = {
            str(row["validation_name"]): dict(row)
            for row in connection.execute(
                "SELECT validation_name,severity,passed,expected,actual,detail "
                "FROM validations WHERE run_id=?",
                (run_id,),
            )
        }
        missing_validations = [
            name
            for name in REQUIRED_STORED_VALIDATIONS
            if name not in stored_validations
            or stored_validations[name]["severity"] != "error"
            or stored_validations[name]["passed"] != 1
        ]
        _check(checks, "stored_validations", not missing_validations, [], missing_validations)
        expected_binding = _report_binding(run_status, recomputed_metrics)
        binding = stored_validations.get("report_binding")
        _check(
            checks,
            "report_binding_row",
            binding is not None
            and binding["expected"] == expected_binding
            and binding["actual"] == expected_binding,
            expected_binding,
            binding,
        )
        logical = independent_logical_sha256(connection)
        _check(checks, "logical_sha256", logical == run["logical_sha256"], run["logical_sha256"], logical)
        expected_report = _render_report(run_status, logical, recomputed_metrics).encode("utf-8")
        report_actual = report_path.read_bytes() if report_path.is_file() else None
        _check(checks, "report_bytes", report_actual == expected_report, hashlib.sha256(expected_report).hexdigest(), hashlib.sha256(report_actual).hexdigest() if report_actual is not None else None)
        expected_status = "complete" if status_counts["success"] == FROZEN_OCCURRENCE_COUNT else "complete_with_failures"
        _check(checks, "status_accounting", run_status == expected_status, expected_status, run_status)
        quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
        _check(checks, "sqlite_quick_check", quick == "ok", "ok", quick)
        _check(checks, "sqlite_integrity_check", integrity == "ok", "ok", integrity)
        _check(checks, "foreign_key_check", not foreign, [], foreign)
    finally:
        connection.close()

    immutable_after = {
        "sidecar": (sidecar_path.stat().st_size, _file_sha(sidecar_path), _sidecars(sidecar_path)),
        "manifest": (manifest_path.stat().st_size, _file_sha(manifest_path)),
        "e2": (e2_path.stat().st_size, _file_sha(e2_path)),
        "e3": (e3_path.stat().st_size, _file_sha(e3_path)),
        "n10_manifest": (
            n10_manifest_path.stat().st_size,
            _file_sha(n10_manifest_path),
        ),
        "n10": (n10_sidecar_path.stat().st_size, _file_sha(n10_sidecar_path)),
    }
    _check(checks, "all_inputs_immutable", immutable_after == immutable_before, immutable_before, immutable_after)
    publish_leftovers_after = tuple(
        str(value)
        for value in (
            sidecar_path.with_name(sidecar_path.name + ".partial"),
            Path(str(sidecar_path.with_name(sidecar_path.name + ".partial")) + ".inputs"),
            report_path.with_name(report_path.name + ".partial"),
        )
        if value.exists()
    )
    _check(
        checks,
        "terminal_publish_cleanup_after_validation",
        not publish_leftovers_after,
        [],
        list(publish_leftovers_after),
    )
    passed = all(value.passed for value in checks if value.severity == "error")
    return N100ValidationResult(
        passed=passed,
        sidecar_path=str(sidecar_path),
        sidecar_size=sidecar_size,
        sidecar_sha256=sidecar_sha,
        run_id=run_id,
        run_status=run_status,
        logical_sha256=logical,
        counts=counts,
        tokens=tokens,
        checks=tuple(checks),
    )


__all__ = [
    "N100ValidationError",
    "N100ValidationResult",
    "ValidationCheck",
    "independent_logical_sha256",
    "validate_frozen_n100_manifest_preflight",
    "validate_semantic_vision_n100",
]
