"""Independent read-only validator for the frozen semantic Vision N10 sidecar.

This module deliberately does not import or call the runtime sidecar/runner.
It opens the result and its SQLite inputs immutable, replays the manifest and
semantic derivations, and independently recomputes the logical digest.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
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


# These values are intentionally literal rather than imported from the runner.
APPLICATION_ID = 0x53564E31
SCHEMA_VERSION = 1
FIXED_BUILDING_COUNT = 10
FIXED_OCCURRENCE_COUNT = 57
FIXED_BATCH_SIZE = 5
FIXED_FETCH_ATTEMPTS = 3
FIXED_VISION_ATTEMPTS = 2
FROZEN_SAMPLE_SEED = "archibe-semantic-coverage-n10-v1"
FROZEN_MANIFEST_BYTE_SHA256 = (
    "81fa13340e584e6d874ab7145a9d003ec57093db5a4dbe41f206c6e7ac85ce1f"
)
FROZEN_MANIFEST_SELF_SHA256 = (
    "bf5ac74479ac305e11dc5aa17f17d02102a7eb2499d15680384d21848801ab5b"
)
MANIFEST_DOMAIN = "archibe-cross-source-semantic-coverage-manifest-v1"
LOGICAL_MANIFEST_VERSION = "cross-source-semantic-vision-logical-v1"
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
    "attempt_accounting",
    "result_accounting",
    "payload_integrity",
    "semantic_derivations",
    "no_pending_work",
    "sqlite_quick_check",
    "sqlite_integrity_check",
    "foreign_key_check",
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
    }
)


class SemanticVisionValidationError(RuntimeError):
    """The artifact could not be safely interpreted as a semantic sidecar."""


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
class SemanticVisionValidationResult:
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
            "checks": [check.as_dict() for check in self.checks],
            "counts": dict(self.counts),
            "failed_error_checks": [
                check.name
                for check in self.checks
                if check.severity == "error" and not check.passed
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


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _fetch_attempt_replay_errors(
    rows: Sequence[Mapping[str, Any]],
    *,
    input_status: str,
    selected_attempt_no: int | None,
) -> list[dict[str, Any]]:
    """Validate append-only fetch retry history, including resumable checkpoints.

    A crash may leave a pending input after a committed retryable failure, or a
    ready input after its exact fetch was committed. Terminal validation rejects
    those input states elsewhere, but their attempt history is still a valid
    resume point and must not be described as corrupt merely because its last
    attempt is retryable.
    """

    errors: list[dict[str, Any]] = []
    numbers = [row.get("attempt_no") for row in rows]
    if numbers != list(range(1, len(rows) + 1)):
        errors.append({"error": "attempt sequence", "actual": numbers})
    if len(rows) > FIXED_FETCH_ATTEMPTS:
        errors.append(
            {
                "error": "fetch retry budget exceeded",
                "actual": len(rows),
                "maximum": FIXED_FETCH_ATTEMPTS,
            }
        )

    for index, row in enumerate(rows):
        attempt_no = row.get("attempt_no")
        retryable = row.get("retryable")
        delay = row.get("scheduled_delay_seconds")
        if retryable not in {0, 1}:
            errors.append(
                {
                    "attempt": attempt_no,
                    "error": "invalid retryable flag",
                    "actual": retryable,
                }
            )
            continue
        if retryable == 1:
            if not isinstance(attempt_no, int) or attempt_no >= FIXED_FETCH_ATTEMPTS:
                errors.append(
                    {
                        "attempt": attempt_no,
                        "error": "retryable attempt exhausts fetch budget",
                    }
                )
            if (
                not isinstance(delay, (int, float))
                or isinstance(delay, bool)
                or delay <= 0
            ):
                errors.append(
                    {
                        "attempt": attempt_no,
                        "error": "retryable attempt lacks positive scheduled delay",
                        "actual": delay,
                    }
                )
        elif delay is not None:
            errors.append(
                {
                    "attempt": attempt_no,
                    "error": "non-retryable attempt has scheduled delay",
                    "actual": delay,
                }
            )
        if index < len(rows) - 1 and retryable != 1:
            errors.append(
                {
                    "attempt": attempt_no,
                    "error": "non-retryable attempt followed by another",
                }
            )

    final = rows[-1] if rows else None
    final_retryable = final.get("retryable") if final is not None else None
    final_attempt_no = final.get("attempt_no") if final is not None else None
    final_outcome = final.get("outcome") if final is not None else None
    if input_status == "pending":
        if selected_attempt_no is not None:
            errors.append({"error": "pending input selected a fetch attempt"})
        if rows and final_retryable != 1:
            errors.append(
                {
                    "attempt": final_attempt_no,
                    "error": "pending checkpoint is not retryable",
                }
            )
    elif input_status in {"ready", "success", "vision_failed"}:
        if final is None:
            errors.append({"error": "prepared input has no fetch attempt"})
        elif (
            selected_attempt_no != final_attempt_no
            or final_outcome != "exact_match"
            or final_retryable != 0
        ):
            errors.append(
                {
                    "error": "prepared input must select its final exact attempt",
                    "selected": selected_attempt_no,
                    "final_attempt": final_attempt_no,
                    "final_outcome": final_outcome,
                }
            )
    elif input_status == "fetch_failed":
        if selected_attempt_no is not None:
            errors.append({"error": "fetch-failed input selected an attempt"})
        if final is None:
            errors.append({"error": "fetch-failed input has no fetch attempt"})
        elif final_retryable != 0:
            errors.append(
                {
                    "attempt": final_attempt_no,
                    "error": "terminal fetch failure remains retryable",
                }
            )
    else:
        errors.append({"error": "unknown input status", "actual": input_status})
    return errors


def _vision_attempt_replay_errors(
    rows: Sequence[Mapping[str, Any]],
    *,
    terminal_run: bool,
) -> list[dict[str, Any]]:
    """Validate one batch's retry chain without assuming wall-clock timing."""

    errors: list[dict[str, Any]] = []
    numbers = [row.get("attempt_no") for row in rows]
    if numbers != list(range(1, len(rows) + 1)):
        errors.append({"error": "attempt numbering", "actual": numbers})
    if len(rows) > FIXED_VISION_ATTEMPTS:
        errors.append(
            {
                "error": "Vision retry budget exceeded",
                "actual": len(rows),
                "maximum": FIXED_VISION_ATTEMPTS,
            }
        )
    statuses = [row.get("status") for row in rows]
    for index, status in enumerate(statuses):
        if status not in {"failed", "success"}:
            errors.append(
                {
                    "attempt": numbers[index],
                    "error": "invalid Vision attempt status",
                    "actual": status,
                }
            )
        if index < len(statuses) - 1 and status != "failed":
            errors.append(
                {
                    "attempt": numbers[index],
                    "error": "successful Vision attempt followed by another",
                }
            )
    if terminal_run and statuses and statuses[-1] == "failed" and len(rows) < FIXED_VISION_ATTEMPTS:
        errors.append(
            {
                "error": "terminal Vision retry chain stopped before budget exhaustion",
                "actual": len(rows),
                "maximum": FIXED_VISION_ATTEMPTS,
            }
        )
    return errors


def _event_usage(event: Mapping[str, Any]) -> tuple[int, int, int] | None:
    usage: Any = None
    if event.get("type") == "turn.completed":
        usage = event.get("usage")
    elif event.get("type") == "response.completed":
        response = event.get("response")
        if isinstance(response, Mapping):
            usage = response.get("usage")
    if not isinstance(usage, Mapping):
        return None
    details = usage.get("input_tokens_details")
    nested_cached = (
        details.get("cached_tokens") if isinstance(details, Mapping) else None
    )
    try:
        values = (
            int(usage.get("input_tokens") or 0),
            int(usage.get("cached_input_tokens", nested_cached) or 0),
            int(usage.get("output_tokens") or 0),
        )
    except (TypeError, ValueError):
        return None
    return values if all(value >= 0 for value in values) else None


def _event_assistant_text(item: Any) -> str | None:
    if not isinstance(item, Mapping):
        return None
    if item.get("type") not in {"agent_message", "assistant_message"}:
        return None
    if isinstance(item.get("text"), str):
        return str(item["text"])
    content = item.get("content")
    if not isinstance(content, list):
        return None
    parts = [
        part["text"]
        for part in content
        if isinstance(part, Mapping)
        and part.get("type") in {"output_text", "input_text", "text"}
        and isinstance(part.get("text"), str)
    ]
    return "".join(parts) if parts else None


def _parse_stdout_events(stdout: bytes) -> dict[str, Any]:
    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("stdout payload is not UTF-8") from exc
    final_text: str | None = None
    usage: tuple[int, int, int] | None = None
    non_json_lines = 0
    event_count = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            non_json_lines += 1
            continue
        event_count += 1
        if not isinstance(event, Mapping):
            continue
        if event.get("type") == "item.completed":
            candidate = _event_assistant_text(event.get("item"))
            if candidate is not None:
                final_text = candidate
        candidate_usage = _event_usage(event)
        if candidate_usage is not None:
            usage = candidate_usage
    results: list[dict[str, Any]] | None = None
    results_error: str | None = None
    if final_text is not None:
        try:
            decoded = json.loads(final_text)
            if not isinstance(decoded, Mapping) or not isinstance(
                decoded.get("results"), list
            ):
                raise ValueError("final assistant text has no results array")
            candidate_results = decoded["results"]
            if not all(isinstance(row, dict) for row in candidate_results):
                raise ValueError("final assistant results contain a non-object")
            results = candidate_results
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            results_error = str(exc)
    return {
        "event_count": event_count,
        "final_text": final_text,
        "non_json_lines": non_json_lines,
        "results": results,
        "results_error": results_error,
        "usage": usage,
    }


def _file_sha256(path: Path, *, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest()


def _sqlite_sidecars(path: Path) -> tuple[str, ...]:
    return tuple(
        str(candidate)
        for candidate in (
            Path(str(path) + "-wal"),
            Path(str(path) + "-shm"),
            Path(str(path) + "-journal"),
        )
        if candidate.exists()
    )


def _open_immutable(path: Path) -> sqlite3.Connection:
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
    *,
    severity: str = "error",
    detail: Any = None,
) -> None:
    checks.append(
        ValidationCheck(name, severity, bool(passed), expected, actual, detail)
    )


def _load_frozen_manifest(path: Path) -> tuple[dict[str, Any], bytes, str, str]:
    if not path.is_file():
        raise FileNotFoundError(path)
    raw = path.read_bytes()
    byte_sha = hashlib.sha256(raw).hexdigest()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SemanticVisionValidationError("manifest is not UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise SemanticVisionValidationError("manifest root must be an object")
    if raw != (_canonical_json(payload) + "\n").encode("utf-8"):
        raise SemanticVisionValidationError(
            "manifest is not canonical JSON followed by one LF"
        )
    body = dict(payload)
    stored_self = body.pop("semantic_coverage_manifest_sha256", None)
    replayed_self = _canonical_sha256(
        {"domain": MANIFEST_DOMAIN, "manifest": body}
    )
    if byte_sha != FROZEN_MANIFEST_BYTE_SHA256:
        raise SemanticVisionValidationError(
            f"frozen manifest byte SHA mismatch: {byte_sha}"
        )
    if stored_self != FROZEN_MANIFEST_SELF_SHA256 or replayed_self != stored_self:
        raise SemanticVisionValidationError("frozen manifest self SHA mismatch")
    if payload.get("sample_seed") != FROZEN_SAMPLE_SEED:
        raise SemanticVisionValidationError("frozen manifest sample seed mismatch")
    if payload.get("sample_size_buildings") != FIXED_BUILDING_COUNT:
        raise SemanticVisionValidationError("frozen manifest building count mismatch")
    if payload.get("planned_occurrence_count") != FIXED_OCCURRENCE_COUNT:
        raise SemanticVisionValidationError("frozen manifest occurrence count mismatch")
    if payload.get("planned_unique_e1_pixel_count") != FIXED_OCCURRENCE_COUNT:
        raise SemanticVisionValidationError("frozen manifest E1 identity count mismatch")
    return payload, raw, byte_sha, replayed_self


def _expected_manifest_rows(
    payload: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    buildings: list[dict[str, Any]] = []
    occurrences: list[dict[str, Any]] = []
    input_rank = 0
    selected_buildings = payload.get("selected_buildings")
    if not isinstance(selected_buildings, list):
        raise SemanticVisionValidationError("selected_buildings must be a list")
    for building_rank, wrapper in enumerate(selected_buildings, 1):
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
                "guard_name": selected["guard_name"],
                "qa_fallback": int(bool(building["qa_fallback"])),
                "building_record_sha256": building["selection_record_sha256"],
                "coverage_plan_record_sha256": selected[
                    "coverage_plan_record_sha256"
                ],
                "selected_building_record_sha256": wrapper[
                    "selected_building_record_sha256"
                ],
                "manifest_json": _canonical_json(wrapper),
            }
        )
        for occurrence_wrapper in plan["selected_occurrences"]:
            input_rank += 1
            occurrence = occurrence_wrapper["occurrence"]
            candidate = occurrence["candidate"]
            source = candidate["source"]
            fetch_url = candidate["fetch_url"]
            parsed = urlsplit(fetch_url)
            if (
                source not in ALLOWED_HOSTS
                or parsed.scheme.casefold() != "https"
                or parsed.hostname != ALLOWED_HOSTS[source]
            ):
                raise SemanticVisionValidationError(
                    f"manifest fetch host mismatch at input rank {input_rank}"
                )
            occurrences.append(
                {
                    "input_rank": input_rank,
                    "inference_id": f"semv_{input_rank:06d}",
                    "selection_id": building["selection_id"],
                    "occurrence_rank": occurrence["occurrence_rank"],
                    "source": source,
                    "source_building_id": candidate["source_building_id"],
                    "source_asset_id": candidate["source_asset_id"],
                    "candidate_id": candidate["candidate_id"],
                    "fetch_url": fetch_url,
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
                    "e3_shortlist_record_sha256": candidate[
                        "e3_shortlist_item_record_sha256"
                    ],
                    "occurrence_record_sha256": occurrence_wrapper[
                        "occurrence_record_sha256"
                    ],
                    "manifest_json": _canonical_json(occurrence_wrapper),
                }
            )
    if len(buildings) != FIXED_BUILDING_COUNT:
        raise SemanticVisionValidationError("manifest does not decode to ten buildings")
    if len(occurrences) != FIXED_OCCURRENCE_COUNT:
        raise SemanticVisionValidationError(
            "manifest does not decode to 57 occurrences"
        )
    if len({row["fetch_url"] for row in occurrences}) != FIXED_OCCURRENCE_COUNT:
        raise SemanticVisionValidationError("manifest fetch URLs are not unique")
    if (
        len({row["expected_e1_pixel_sha256"] for row in occurrences})
        != FIXED_OCCURRENCE_COUNT
    ):
        raise SemanticVisionValidationError("manifest E1 pixel identities are not unique")
    return buildings, occurrences


def _row_mismatches(
    expected: Sequence[Mapping[str, Any]],
    actual: Sequence[Mapping[str, Any]],
    *,
    keys: Sequence[str],
) -> list[dict[str, Any]]:
    expected_map = {tuple(row[key] for key in keys): dict(row) for row in expected}
    actual_map = {tuple(row[key] for key in keys): dict(row) for row in actual}
    mismatches: list[dict[str, Any]] = []
    for identity in sorted(set(expected_map) | set(actual_map)):
        wanted = expected_map.get(identity)
        found = actual_map.get(identity)
        if wanted != found:
            mismatches.append(
                {"identity": identity, "expected": wanted, "actual": found}
            )
    return mismatches


_LOGICAL_TABLES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "semantic_runs",
        "run_id",
        (
            "run_id", "runner_version", "schema_version", "manifest_size",
            "manifest_byte_sha256", "manifest_self_sha256",
            "ordered_building_manifest_sha256",
            "ordered_occurrence_manifest_sha256", "sample_seed",
            "building_count", "occurrence_count", "e2_size",
            "e2_sha256_before", "e2_logical_sha256", "e2_run_id", "e3_size",
            "e3_sha256_before", "e3_logical_sha256", "e3_run_id",
            "contract_version", "prompt_version", "output_schema_sha256",
            "transform_version", "e1_contract_version",
            "dependency_manifest_json", "dependency_manifest_sha256",
            "runtime_version", "retry_policy_version", "batch_size",
            "max_fetch_attempts", "max_vision_attempts", "model", "reasoning",
            "service_tier", "image_detail", "cli_version",
        ),
    ),
    (
        "selected_buildings",
        "building_rank",
        (
            "building_rank", "selection_id", "source", "source_building_id",
            "population_stratum", "guard_name", "qa_fallback",
            "building_record_sha256", "coverage_plan_record_sha256",
            "selected_building_record_sha256", "manifest_json",
        ),
    ),
    (
        "selected_occurrences",
        "input_rank",
        (
            "input_rank", "inference_id", "selection_id", "occurrence_rank",
            "source", "source_building_id", "source_asset_id", "candidate_id",
            "fetch_url", "expected_response_sha256", "expected_e1_pixel_sha256",
            "expected_e1_width", "expected_e1_height", "e2_asset_record_sha256",
            "e2_relation_record_sha256", "e3_candidate_record_sha256",
            "e3_ranking_record_sha256", "e3_shortlist_record_sha256",
            "occurrence_record_sha256", "manifest_json",
        ),
    ),
    (
        "vision_inputs", "inference_id",
        (
            "inference_id", "status", "selected_fetch_attempt_no",
            "actual_response_sha256", "actual_e1_pixel_sha256",
            "derivative_encoded_sha256", "derivative_pixel_sha256",
            "derivative_width", "derivative_height", "derivative_bytes",
            "error_kind", "error_message",
        ),
    ),
    (
        "fetch_attempts", "inference_id,attempt_no",
        (
            "inference_id", "attempt_no", "request_url", "final_url",
            "elapsed_ms", "outcome", "http_status", "content_type",
            "response_bytes", "expected_response_sha256",
            "actual_response_sha256", "expected_e1_pixel_sha256",
            "actual_e1_pixel_sha256", "retryable", "retry_after_seconds",
            "scheduled_delay_seconds", "error_kind", "error_message",
        ),
    ),
    (
        "vision_attempts", "attempt_id",
        (
            "attempt_id", "batch_no", "attempt_no", "inference_ids_json",
            "status", "model", "reasoning", "service_tier", "runtime_version",
            "cli_version", "codex_bin", "image_detail", "sandbox",
            "prompt_sha256", "output_schema_sha256", "elapsed_ms",
            "input_tokens", "cached_input_tokens", "output_tokens",
            "raw_events_sha256", "error_kind", "error_message",
        ),
    ),
    (
        "vision_attempt_payloads", "attempt_id",
        (
            "attempt_id", "codec", "stdout_bytes", "stdout_sha256",
            "stderr_bytes", "stderr_sha256", "stderr_excerpt",
        ),
    ),
    (
        "semantic_results", "inference_id",
        (
            "inference_id", "attempt_id", "raw_result_json",
            "normalized_result_json", "in_scope", "reject_reason", "medium",
            "spatial_context", "framing_scale", "camera_angle", "drawing_kind",
            "project_state", "project_legibility", "uncertain_axes_json",
            "resolution_insufficient", "evidence", "record_sha256",
        ),
    ),
    (
        "occurrence_result_links", "inference_id",
        (
            "inference_id", "result_inference_id", "reuse_basis",
            "verified_input_pixel_sha256", "record_sha256",
        ),
    ),
    (
        "hero_candidate_decisions", "inference_id",
        (
            "inference_id", "tier", "reasons_json", "authoritative",
            "record_sha256",
        ),
    ),
    (
        "coverage_slot_assignments", "selection_id,slot,assignment_rank",
        (
            "selection_id", "slot", "assignment_rank", "state",
            "inference_id", "record_sha256",
        ),
    ),
    (
        "validations", "validation_name",
        (
            "validation_name", "severity", "passed", "expected", "actual",
            "detail",
        ),
    ),
)


def independent_logical_sha256(connection: sqlite3.Connection) -> str:
    """Recompute the runner's logical digest without importing runner code."""

    digest = hashlib.sha256()
    digest.update((LOGICAL_MANIFEST_VERSION + "\n").encode("ascii"))
    for table, order_by, columns in _LOGICAL_TABLES:
        digest.update((table + "\0").encode("utf-8"))
        for row in connection.execute(
            f"SELECT {','.join(columns)} FROM {table} ORDER BY {order_by}"
        ):
            digest.update(
                _canonical_json(dict(zip(columns, row))).encode("utf-8") + b"\n"
            )
    return digest.hexdigest()


def _validate_input_artifact(
    checks: list[ValidationCheck],
    *,
    label: str,
    path: Path,
    manifest_record: Mapping[str, Any],
    run: sqlite3.Row,
) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    sidecars = _sqlite_sidecars(path)
    actual_size = path.stat().st_size
    actual_sha = _file_sha256(path)
    artifact = _open_immutable(path)
    try:
        actual_application_id = int(
            artifact.execute("PRAGMA application_id").fetchone()[0]
        )
        actual_user_version = int(
            artifact.execute("PRAGMA user_version").fetchone()[0]
        )
    finally:
        artifact.close()
    prefix = label.casefold()
    expected_size = int(manifest_record["size_bytes"])
    expected_sha = str(manifest_record["byte_sha256"])
    _check(checks, f"{prefix}_sidecars_absent", not sidecars, [], list(sidecars))
    _check(checks, f"{prefix}_size", actual_size == expected_size, expected_size, actual_size)
    _check(checks, f"{prefix}_byte_sha256", actual_sha == expected_sha, expected_sha, actual_sha)
    _check(
        checks,
        f"{prefix}_sqlite_identity",
        (
            actual_application_id,
            actual_user_version,
        )
        == (
            int(manifest_record["application_id"]),
            int(manifest_record["user_version"]),
        ),
        [
            int(manifest_record["application_id"]),
            int(manifest_record["user_version"]),
        ],
        [actual_application_id, actual_user_version],
    )
    recorded_path = Path(str(run[f"{prefix}_path"])).resolve()
    _check(checks, f"{prefix}_path", path.resolve() == recorded_path, str(path.resolve()), str(recorded_path))
    recorded = {
        "size": run[f"{prefix}_size"],
        "before": run[f"{prefix}_sha256_before"],
        "after": run[f"{prefix}_sha256_after"],
        "logical": run[f"{prefix}_logical_sha256"],
        "run_id": run[f"{prefix}_run_id"],
    }
    wanted = {
        "size": expected_size,
        "before": expected_sha,
        "after": expected_sha,
        "logical": manifest_record["logical_sha256"],
        "run_id": manifest_record["run_id"],
    }
    _check(checks, f"{prefix}_run_lineage", recorded == wanted, wanted, recorded)
    return {
        "path": str(path.resolve()),
        "size": actual_size,
        "sha256": actual_sha,
        "sidecars": list(sidecars),
        "application_id": actual_application_id,
        "user_version": actual_user_version,
    }


def validate_semantic_vision_sidecar(
    sidecar_path: Path,
    *,
    manifest_path: Path,
    e2_path: Path,
    e3_path: Path,
) -> SemanticVisionValidationResult:
    """Perform a complete independent, immutable, zero-network validation."""

    sidecar_path = sidecar_path.resolve()
    manifest_path = manifest_path.resolve()
    e2_path = e2_path.resolve()
    e3_path = e3_path.resolve()
    if len({sidecar_path, manifest_path, e2_path, e3_path}) != 4:
        raise ValueError("sidecar, manifest, E2, and E3 paths must be distinct")
    if not sidecar_path.is_file():
        raise FileNotFoundError(sidecar_path)
    sidecars = _sqlite_sidecars(sidecar_path)
    if sidecars:
        raise SemanticVisionValidationError(
            f"semantic SQLite sidecars are present: {sidecars}"
        )

    payload, manifest_raw, manifest_byte_sha, manifest_self_sha = (
        _load_frozen_manifest(manifest_path)
    )
    expected_buildings, expected_occurrences = _expected_manifest_rows(payload)
    sidecar_size = sidecar_path.stat().st_size
    sidecar_sha = _file_sha256(sidecar_path)
    checks: list[ValidationCheck] = []
    counts: dict[str, Any] = {}
    tokens: dict[str, int] = {}

    connection = _open_immutable(sidecar_path)
    try:
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        _check(checks, "application_id", application_id == APPLICATION_ID, APPLICATION_ID, application_id)
        _check(checks, "user_version", user_version == SCHEMA_VERSION, SCHEMA_VERSION, user_version)

        run_rows = connection.execute("SELECT * FROM semantic_runs").fetchall()
        if len(run_rows) != 1:
            raise SemanticVisionValidationError(
                f"expected exactly one semantic run, found {len(run_rows)}"
            )
        run = run_rows[0]
        run_id = str(run["run_id"])
        run_status = str(run["status"])
        actual_triggers = frozenset(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' ORDER BY name"
            )
        )
        _check(
            checks,
            "terminal_trigger_set",
            actual_triggers == REQUIRED_TRIGGERS,
            sorted(REQUIRED_TRIGGERS),
            sorted(actual_triggers),
        )
        _check(
            checks,
            "terminal_run_status",
            run_status in {"complete", "complete_with_failures"},
            ["complete", "complete_with_failures"],
            run_status,
        )
        _check(checks, "schema_version_column", run["schema_version"] == SCHEMA_VERSION, SCHEMA_VERSION, run["schema_version"])
        _check(
            checks,
            "fixed_runtime_limits",
            (
                run["building_count"], run["occurrence_count"], run["batch_size"],
                run["max_fetch_attempts"], run["max_vision_attempts"],
            )
            == (
                FIXED_BUILDING_COUNT, FIXED_OCCURRENCE_COUNT, FIXED_BATCH_SIZE,
                FIXED_FETCH_ATTEMPTS, FIXED_VISION_ATTEMPTS,
            ),
            [
                FIXED_BUILDING_COUNT, FIXED_OCCURRENCE_COUNT, FIXED_BATCH_SIZE,
                FIXED_FETCH_ATTEMPTS, FIXED_VISION_ATTEMPTS,
            ],
            [
                run["building_count"], run["occurrence_count"], run["batch_size"],
                run["max_fetch_attempts"], run["max_vision_attempts"],
            ],
        )
        schema_sha = _canonical_sha256(OUTPUT_SCHEMA)
        contract_actual = {
            "contract_version": run["contract_version"],
            "prompt_version": run["prompt_version"],
            "output_schema_sha256": run["output_schema_sha256"],
            "transform_version": run["transform_version"],
        }
        contract_expected = {
            "contract_version": CONTRACT_VERSION,
            "prompt_version": PROMPT_VERSION,
            "output_schema_sha256": schema_sha,
            "transform_version": TRANSFORM_VERSION,
        }
        _check(checks, "semantic_contract", contract_actual == contract_expected, contract_expected, contract_actual)

        manifest_actual = {
            "path": str(Path(str(run["manifest_path"])).resolve()),
            "size": run["manifest_size"],
            "byte_sha256": run["manifest_byte_sha256"],
            "self_sha256": run["manifest_self_sha256"],
            "sample_seed": run["sample_seed"],
            "building_manifest": run["ordered_building_manifest_sha256"],
            "occurrence_manifest": run["ordered_occurrence_manifest_sha256"],
        }
        manifest_expected = {
            "path": str(manifest_path),
            "size": len(manifest_raw),
            "byte_sha256": manifest_byte_sha,
            "self_sha256": manifest_self_sha,
            "sample_seed": FROZEN_SAMPLE_SEED,
            "building_manifest": payload["ordered_building_manifest_sha256"],
            "occurrence_manifest": payload["ordered_occurrence_manifest_sha256"],
        }
        _check(checks, "manifest_identity", manifest_actual == manifest_expected, manifest_expected, manifest_actual)

        input_details = {
            "e2": _validate_input_artifact(
                checks,
                label="e2",
                path=e2_path,
                manifest_record=payload["e2_input"],
                run=run,
            ),
            "e3": _validate_input_artifact(
                checks,
                label="e3",
                path=e3_path,
                manifest_record=payload["e3_input"],
                run=run,
            ),
        }
        counts["inputs"] = input_details

        building_columns = tuple(expected_buildings[0])
        actual_buildings = [
            dict(row)
            for row in connection.execute(
                f"SELECT {','.join(building_columns)} FROM selected_buildings WHERE run_id=? ORDER BY building_rank",
                (run_id,),
            )
        ]
        occurrence_columns = tuple(expected_occurrences[0])
        actual_occurrences = [
            dict(row)
            for row in connection.execute(
                f"SELECT {','.join(occurrence_columns)} FROM selected_occurrences WHERE run_id=? ORDER BY input_rank",
                (run_id,),
            )
        ]
        building_mismatches = _row_mismatches(
            expected_buildings, actual_buildings, keys=("building_rank",)
        )
        occurrence_mismatches = _row_mismatches(
            expected_occurrences, actual_occurrences, keys=("input_rank",)
        )
        _check(checks, "manifest_building_rows", not building_mismatches, 0, len(building_mismatches), detail=building_mismatches)
        _check(checks, "manifest_occurrence_rows", not occurrence_mismatches, 0, len(occurrence_mismatches), detail=occurrence_mismatches)
        counts["buildings"] = len(actual_buildings)
        counts["occurrences"] = len(actual_occurrences)

        occurrence_by_id = {row["inference_id"]: row for row in actual_occurrences}
        input_rows = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM vision_inputs WHERE run_id=? ORDER BY inference_id",
                (run_id,),
            )
        ]
        input_by_id = {row["inference_id"]: row for row in input_rows}
        input_status: dict[str, int] = {}
        for row in input_rows:
            input_status[row["status"]] = input_status.get(row["status"], 0) + 1
        counts["input_status"] = dict(sorted(input_status.items()))
        _check(checks, "vision_input_exact_population", set(input_by_id) == set(occurrence_by_id) and len(input_rows) == FIXED_OCCURRENCE_COUNT, FIXED_OCCURRENCE_COUNT, len(input_rows))
        _check(checks, "no_pending_or_ready", input_status.get("pending", 0) + input_status.get("ready", 0) == 0, 0, input_status.get("pending", 0) + input_status.get("ready", 0))

        fetch_rows = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM fetch_attempts WHERE run_id=? ORDER BY inference_id,attempt_no",
                (run_id,),
            )
        ]
        fetch_by_id: dict[str, list[dict[str, Any]]] = {}
        fetch_bad: list[dict[str, Any]] = []
        for row in fetch_rows:
            inference_id = row["inference_id"]
            source = occurrence_by_id.get(inference_id)
            if source is None:
                fetch_bad.append({"inference_id": inference_id, "error": "unknown inference"})
                continue
            fetch_by_id.setdefault(inference_id, []).append(row)
            if row["request_url"] != source["fetch_url"]:
                fetch_bad.append({"inference_id": inference_id, "attempt": row["attempt_no"], "error": "request URL mismatch"})
            if row["expected_response_sha256"] != source["expected_response_sha256"] or row["expected_e1_pixel_sha256"] != source["expected_e1_pixel_sha256"]:
                fetch_bad.append({"inference_id": inference_id, "attempt": row["attempt_no"], "error": "expected identity mismatch"})
            if row["final_url"] is not None:
                parsed_final = urlsplit(row["final_url"])
                if (
                    parsed_final.scheme.casefold() != "https"
                    or parsed_final.hostname != ALLOWED_HOSTS[source["source"]]
                ):
                    fetch_bad.append(
                        {
                            "inference_id": inference_id,
                            "attempt": row["attempt_no"],
                            "error": "final URL source host mismatch",
                            "final_url": row["final_url"],
                        }
                    )
            outcome = row["outcome"]
            if outcome == "exact_match":
                valid = (
                    row["actual_response_sha256"] == row["expected_response_sha256"]
                    and row["actual_e1_pixel_sha256"] == row["expected_e1_pixel_sha256"]
                    and row["http_status"] == 200
                    and isinstance(row["response_bytes"], int)
                    and row["response_bytes"] > 0
                    and isinstance(row["content_type"], str)
                    and row["content_type"].casefold().startswith("image/")
                    and row["retryable"] == 0
                    and row["error_kind"] is None
                )
            elif outcome == "delivery_changed_pixel_stable":
                valid = (
                    row["actual_response_sha256"] is not None
                    and row["actual_response_sha256"] != row["expected_response_sha256"]
                    and row["actual_e1_pixel_sha256"] == row["expected_e1_pixel_sha256"]
                    and row["retryable"] == 0
                    and row["error_kind"] == outcome
                )
            elif outcome == "source_changed":
                valid = (
                    row["actual_e1_pixel_sha256"] is not None
                    and row["actual_e1_pixel_sha256"] != row["expected_e1_pixel_sha256"]
                    and row["retryable"] == 0
                    and row["error_kind"] == outcome
                )
            else:
                valid = outcome in {"http_failed", "invalid_content", "decode_failed", "oversize"} and isinstance(row["error_kind"], str) and bool(row["error_kind"])
            if not valid:
                fetch_bad.append({"inference_id": inference_id, "attempt": row["attempt_no"], "error": "outcome/hash logic", "outcome": outcome})
        for inference_id, input_row in input_by_id.items():
            attempts = fetch_by_id.get(inference_id, [])
            selected_no = input_row["selected_fetch_attempt_no"]
            fetch_bad.extend(
                {"inference_id": inference_id, **error}
                for error in _fetch_attempt_replay_errors(
                    attempts,
                    input_status=str(input_row["status"]),
                    selected_attempt_no=selected_no,
                )
            )
            selected = next((row for row in attempts if row["attempt_no"] == selected_no), None)
            if input_row["status"] in {"success", "vision_failed"}:
                valid = (
                    selected is not None
                    and selected["outcome"] == "exact_match"
                    and input_row["actual_response_sha256"] == occurrence_by_id[inference_id]["expected_response_sha256"]
                    and input_row["actual_e1_pixel_sha256"] == occurrence_by_id[inference_id]["expected_e1_pixel_sha256"]
                    and all(input_row[key] is not None for key in (
                        "derivative_encoded_sha256", "derivative_pixel_sha256",
                        "derivative_width", "derivative_height", "derivative_bytes",
                    ))
                )
            elif input_row["status"] == "fetch_failed":
                valid = (
                    bool(attempts)
                    and selected_no is None
                    and input_row["derivative_encoded_sha256"] is None
                    and bool(input_row["error_kind"])
                )
            else:
                valid = False
            if not valid:
                fetch_bad.append({"inference_id": inference_id, "error": "input terminal/fetch linkage", "status": input_row["status"]})
        _check(checks, "fetch_outcome_and_pixel_logic", not fetch_bad, 0, len(fetch_bad), detail=fetch_bad)
        counts["fetch_attempts"] = len(fetch_rows)
        counts["fetch_outcomes"] = dict(sorted({name: sum(row["outcome"] == name for row in fetch_rows) for name in {row["outcome"] for row in fetch_rows}}.items()))
        counts["downloaded_bytes"] = sum(int(row["response_bytes"] or 0) for row in fetch_rows)

        attempt_rows = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM vision_attempts WHERE run_id=? ORDER BY attempt_id",
                (run_id,),
            )
        ]
        attempt_by_id = {row["attempt_id"]: row for row in attempt_rows}
        attempt_rows_by_batch: dict[int, list[dict[str, Any]]] = {}
        attempt_lists_by_batch: dict[int, tuple[str, ...]] = {}
        attempt_bad: list[dict[str, Any]] = []
        for row in attempt_rows:
            try:
                ids_value = json.loads(row["inference_ids_json"])
            except json.JSONDecodeError:
                ids_value = None
            if not isinstance(ids_value, list) or not ids_value or any(not isinstance(value, str) for value in ids_value) or len(ids_value) != len(set(ids_value)) or len(ids_value) > FIXED_BATCH_SIZE:
                attempt_bad.append({"attempt_id": row["attempt_id"], "error": "invalid inference_ids_json"})
                continue
            ids = tuple(ids_value)
            sources = [occurrence_by_id.get(value) for value in ids]
            if any(source is None for source in sources):
                attempt_bad.append({"attempt_id": row["attempt_id"], "error": "unknown inference ID"})
                continue
            ranks = [int(source["input_rank"]) for source in sources if source is not None]
            expected_batch = {(rank - 1) // FIXED_BATCH_SIZE + 1 for rank in ranks}
            if ranks != sorted(ranks) or expected_batch != {row["batch_no"]}:
                attempt_bad.append({"attempt_id": row["attempt_id"], "error": "batch membership/order", "ranks": ranks})
            if row["prompt_sha256"] != hashlib.sha256(compose_prompt(ids).encode("utf-8")).hexdigest():
                attempt_bad.append({"attempt_id": row["attempt_id"], "error": "prompt SHA mismatch"})
            if row["output_schema_sha256"] != schema_sha:
                attempt_bad.append({"attempt_id": row["attempt_id"], "error": "output schema SHA mismatch"})
            for key in ("model", "reasoning", "service_tier", "runtime_version", "cli_version", "image_detail"):
                if row[key] != run[key]:
                    attempt_bad.append({"attempt_id": row["attempt_id"], "error": f"{key} lineage mismatch"})
            attempt_rows_by_batch.setdefault(row["batch_no"], []).append(row)
            previous_ids = attempt_lists_by_batch.setdefault(row["batch_no"], ids)
            if previous_ids != ids:
                attempt_bad.append({"attempt_id": row["attempt_id"], "error": "retry batch IDs changed"})
        for batch_no, rows in attempt_rows_by_batch.items():
            attempt_bad.extend(
                {"batch_no": batch_no, **error}
                for error in _vision_attempt_replay_errors(
                    rows,
                    terminal_run=run_status in {"complete", "complete_with_failures"},
                )
            )
        _check(checks, "vision_batch_exact_ids", not attempt_bad, 0, len(attempt_bad), detail=attempt_bad)

        payload_rows = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM vision_attempt_payloads ORDER BY attempt_id"
            )
        ]
        payload_bad: list[dict[str, Any]] = []
        parsed_payloads: dict[int, dict[str, Any]] = {}
        if {row["attempt_id"] for row in payload_rows} != set(attempt_by_id):
            payload_bad.append({"error": "payload/attempt identity set mismatch"})
        for row in payload_rows:
            try:
                stdout = gzip.decompress(row["stdout_gzip"])
                stderr = gzip.decompress(row["stderr_gzip"])
            except (OSError, EOFError) as exc:
                payload_bad.append({"attempt_id": row["attempt_id"], "error": f"gzip: {exc}"})
                continue
            stdout_sha = hashlib.sha256(stdout).hexdigest()
            stderr_sha = hashlib.sha256(stderr).hexdigest()
            valid = (
                row["codec"] == "gzip"
                and len(stdout) == row["stdout_bytes"]
                and stdout_sha == row["stdout_sha256"]
                and len(stderr) == row["stderr_bytes"]
                and stderr_sha == row["stderr_sha256"]
                and row["attempt_id"] in attempt_by_id
                and attempt_by_id[row["attempt_id"]]["raw_events_sha256"] == stdout_sha
            )
            if not valid:
                payload_bad.append({"attempt_id": row["attempt_id"], "error": "payload length/SHA linkage"})
                continue
            attempt = attempt_by_id[row["attempt_id"]]
            try:
                parsed_events = _parse_stdout_events(stdout)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                payload_bad.append(
                    {
                        "attempt_id": row["attempt_id"],
                        "error": f"stdout JSONL parse: {exc}",
                    }
                )
                continue
            stored_usage = (
                attempt["input_tokens"],
                attempt["cached_input_tokens"],
                attempt["output_tokens"],
            )
            parsed_usage = parsed_events["usage"]
            usage_valid = (
                all(value is None for value in stored_usage)
                if parsed_usage is None
                else tuple(int(value or 0) for value in stored_usage)
                == parsed_usage
                and all(value is not None for value in stored_usage)
            )
            if not usage_valid:
                payload_bad.append(
                    {
                        "attempt_id": row["attempt_id"],
                        "error": "usage event/token columns mismatch",
                        "event_usage": parsed_usage,
                        "stored_usage": stored_usage,
                    }
                )
            if attempt["status"] == "success":
                result_records = parsed_events["results"]
                expected_ids = json.loads(attempt["inference_ids_json"])
                actual_ids = (
                    [result.get("asset_id") for result in result_records]
                    if isinstance(result_records, list)
                    else None
                )
                if actual_ids != expected_ids:
                    payload_bad.append(
                        {
                            "attempt_id": row["attempt_id"],
                            "error": "final assistant result IDs/order mismatch",
                            "expected": expected_ids,
                            "actual": actual_ids,
                            "parse_error": parsed_events["results_error"],
                        }
                    )
                if parsed_usage is None:
                    payload_bad.append(
                        {
                            "attempt_id": row["attempt_id"],
                            "error": "successful attempt has no usage event",
                        }
                    )
            parsed_payloads[row["attempt_id"]] = parsed_events
        _check(checks, "gzip_payload_integrity", not payload_bad, 0, len(payload_bad), detail=payload_bad)

        result_rows = [
            dict(row)
            for row in connection.execute(
                "SELECT r.*,o.selection_id,o.input_rank FROM semantic_results r JOIN selected_occurrences o USING(run_id,inference_id) WHERE r.run_id=? ORDER BY o.input_rank",
                (run_id,),
            )
        ]
        normalized_by_id: dict[str, dict[str, Any]] = {}
        result_bad: list[dict[str, Any]] = []
        for row in result_rows:
            inference_id = row["inference_id"]
            attempt = attempt_by_id.get(row["attempt_id"])
            try:
                raw_result = json.loads(row["raw_result_json"])
                normalized_stored = json.loads(row["normalized_result_json"])
                normalized = normalize_result(raw_result, inference_id)
            except Exception as exc:
                result_bad.append({"inference_id": inference_id, "error": f"normalize: {exc}"})
                continue
            normalized_by_id[inference_id] = normalized
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
                "uncertain_axes_json": _canonical_json(normalized["uncertain_axes"]),
                "resolution_insufficient": int(normalized["resolution_insufficient"]),
                "evidence": normalized["evidence"],
            }
            actual_columns = {key: row[key] for key in expected_columns}
            result_body = {
                "attempt_id": row["attempt_id"],
                "inference_id": inference_id,
                "normalized": normalized,
                "raw": raw_result,
            }
            attempt_ids = json.loads(attempt["inference_ids_json"]) if attempt else []
            event_results = (
                parsed_payloads.get(row["attempt_id"], {}).get("results")
                if attempt is not None
                else None
            )
            event_result = next(
                (
                    candidate
                    for candidate in (event_results or [])
                    if candidate.get("asset_id") == inference_id
                ),
                None,
            )
            valid = (
                _canonical_json(raw_result) == row["raw_result_json"]
                and event_result is not None
                and _canonical_json(event_result) == row["raw_result_json"]
                and _canonical_json(normalized) == row["normalized_result_json"]
                and normalized_stored == json.loads(_canonical_json(normalized))
                and actual_columns == expected_columns
                and row["record_sha256"] == _canonical_sha256(result_body)
                and attempt is not None
                and attempt["status"] == "success"
                and inference_id in attempt_ids
            )
            if not valid:
                result_bad.append({"inference_id": inference_id, "error": "raw/normalized/result lineage mismatch"})
        success_ids = {row["inference_id"] for row in input_rows if row["status"] == "success"}
        result_ids = {row["inference_id"] for row in result_rows}
        if result_ids != success_ids:
            result_bad.append({"error": "success/result identity mismatch", "success": sorted(success_ids), "results": sorted(result_ids)})
        for attempt_id, attempt in attempt_by_id.items():
            linked = {row["inference_id"] for row in result_rows if row["attempt_id"] == attempt_id}
            ids = set(json.loads(attempt["inference_ids_json"]))
            if (attempt["status"] == "success" and linked != ids) or (attempt["status"] == "failed" and linked):
                result_bad.append({"attempt_id": attempt_id, "error": "attempt/result accounting", "expected": sorted(ids) if attempt["status"] == "success" else [], "actual": sorted(linked)})
        _check(checks, "raw_to_normalized_results", not result_bad, 0, len(result_bad), detail=result_bad)

        expected_links: list[dict[str, Any]] = []
        expected_heroes: list[dict[str, Any]] = []
        expected_slots: list[dict[str, Any]] = []
        slot_ranks: dict[tuple[str, str], int] = {}
        for occurrence in actual_occurrences:
            inference_id = occurrence["inference_id"]
            if inference_id not in normalized_by_id:
                continue
            normalized = normalized_by_id[inference_id]
            input_row = input_by_id[inference_id]
            link_body = {
                "inference_id": inference_id,
                "result_inference_id": inference_id,
                "reuse_basis": "same_occurrence",
                "verified_input_pixel_sha256": input_row["derivative_pixel_sha256"],
            }
            expected_links.append({**link_body, "record_sha256": _canonical_sha256(link_body)})
            tier, reasons = derive_hero_decision(normalized)
            hero_body = {
                "inference_id": inference_id,
                "tier": tier,
                "reasons": reasons,
                "authoritative": False,
            }
            expected_heroes.append(
                {
                    "inference_id": inference_id,
                    "tier": tier,
                    "reasons_json": _canonical_json(reasons),
                    "authoritative": 0,
                    "record_sha256": _canonical_sha256(hero_body),
                }
            )
            for slot in derive_coverage_slots(normalized):
                key = (occurrence["selection_id"], slot)
                rank = slot_ranks.get(key, 0) + 1
                slot_ranks[key] = rank
                body = {
                    "assignment_rank": rank,
                    "inference_id": inference_id,
                    "selection_id": occurrence["selection_id"],
                    "slot": slot,
                    "state": "observed",
                }
                expected_slots.append({**body, "record_sha256": _canonical_sha256(body)})
        for building in actual_buildings:
            for slot in COVERAGE_SLOTS:
                key = (building["selection_id"], slot)
                if key in slot_ranks:
                    continue
                body = {
                    "assignment_rank": 0,
                    "inference_id": None,
                    "selection_id": building["selection_id"],
                    "slot": slot,
                    "state": "not_observed_in_sample",
                }
                expected_slots.append({**body, "record_sha256": _canonical_sha256(body)})

        actual_links = [dict(row) for row in connection.execute("SELECT inference_id,result_inference_id,reuse_basis,verified_input_pixel_sha256,record_sha256 FROM occurrence_result_links WHERE run_id=? ORDER BY inference_id", (run_id,))]
        actual_heroes = [dict(row) for row in connection.execute("SELECT inference_id,tier,reasons_json,authoritative,record_sha256 FROM hero_candidate_decisions WHERE run_id=? ORDER BY inference_id", (run_id,))]
        actual_slots = [dict(row) for row in connection.execute("SELECT selection_id,slot,assignment_rank,state,inference_id,record_sha256 FROM coverage_slot_assignments WHERE run_id=? ORDER BY selection_id,slot,assignment_rank", (run_id,))]
        link_mismatches = _row_mismatches(expected_links, actual_links, keys=("inference_id",))
        hero_mismatches = _row_mismatches(expected_heroes, actual_heroes, keys=("inference_id",))
        slot_mismatches = _row_mismatches(expected_slots, actual_slots, keys=("selection_id", "slot", "assignment_rank"))
        _check(checks, "result_link_replay", not link_mismatches, 0, len(link_mismatches), detail=link_mismatches)
        _check(checks, "hero_derivation_replay", not hero_mismatches, 0, len(hero_mismatches), detail=hero_mismatches)
        _check(checks, "coverage_derivation_replay", not slot_mismatches, 0, len(slot_mismatches), detail=slot_mismatches)

        counts["vision_attempts"] = len(attempt_rows)
        counts["successful_vision_attempts"] = sum(row["status"] == "success" for row in attempt_rows)
        counts["results"] = len(result_rows)
        counts["result_links"] = len(actual_links)
        counts["hero_decisions"] = len(actual_heroes)
        counts["coverage_assignments"] = len(actual_slots)
        tokens = {
            "input_tokens": sum(int(row["input_tokens"] or 0) for row in attempt_rows),
            "cached_input_tokens": sum(int(row["cached_input_tokens"] or 0) for row in attempt_rows),
            "output_tokens": sum(int(row["output_tokens"] or 0) for row in attempt_rows),
            "vision_elapsed_ms": sum(int(row["elapsed_ms"] or 0) for row in attempt_rows),
        }
        recomputed_metrics = {
            "buildings": FIXED_BUILDING_COUNT,
            "occurrences": FIXED_OCCURRENCE_COUNT,
            "input_status": dict(sorted(input_status.items())),
            "fetch_attempts": len(fetch_rows),
            "downloaded_bytes": counts["downloaded_bytes"],
            "vision_attempts": len(attempt_rows),
            "successful_vision_attempts": counts["successful_vision_attempts"],
            **tokens,
            "results": len(result_rows),
        }
        try:
            stored_metrics = json.loads(run["metrics_json"])
        except (TypeError, json.JSONDecodeError):
            stored_metrics = None
        _check(checks, "token_and_metrics_aggregate", stored_metrics == recomputed_metrics, recomputed_metrics, stored_metrics)

        stored_validations = {
            row["validation_name"]: dict(row)
            for row in connection.execute(
                "SELECT validation_name,severity,passed,expected,actual,detail FROM validations WHERE run_id=? ORDER BY validation_name",
                (run_id,),
            )
        }
        invalid_required = [
            name
            for name in REQUIRED_STORED_VALIDATIONS
            if name not in stored_validations
            or stored_validations[name]["severity"] != "error"
            or stored_validations[name]["passed"] != 1
        ]
        _check(checks, "stored_required_validations", not invalid_required, [], invalid_required)
        counts["stored_validations"] = len(stored_validations)

        quick = connection.execute("PRAGMA quick_check").fetchone()[0]
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
        _check(checks, "sqlite_quick_check", quick == "ok", "ok", quick)
        _check(checks, "sqlite_integrity_check", integrity == "ok", "ok", integrity)
        _check(checks, "foreign_key_check", not foreign_keys, [], foreign_keys)

        expected_status = "complete" if input_status.get("success", 0) == FIXED_OCCURRENCE_COUNT else "complete_with_failures"
        _check(checks, "status_matches_terminal_accounting", run_status == expected_status, expected_status, run_status)
        input_hashes_stable = (
            run["e2_sha256_after"] == run["e2_sha256_before"]
            and run["e3_sha256_after"] == run["e3_sha256_before"]
        )
        _check(
            checks,
            "terminal_input_hashes",
            input_hashes_stable,
            True,
            input_hashes_stable,
        )
        _check(checks, "terminal_completion", bool(run["completed_at"]), "non-empty", run["completed_at"])
        _check(checks, "terminal_error_clear", run["error"] is None, None, run["error"])

        logical = independent_logical_sha256(connection)
        _check(checks, "independent_logical_sha256", logical == run["logical_sha256"], run["logical_sha256"], logical)
    finally:
        connection.close()

    sidecar_size_after = sidecar_path.stat().st_size
    sidecar_sha_after = _file_sha256(sidecar_path)
    sidecars_after = _sqlite_sidecars(sidecar_path)
    _check(
        checks,
        "sidecar_read_only_stability",
        (
            sidecar_size_after == sidecar_size
            and sidecar_sha_after == sidecar_sha
            and not sidecars_after
        ),
        {
            "size": sidecar_size,
            "sha256": sidecar_sha,
            "sqlite_sidecars": [],
        },
        {
            "size": sidecar_size_after,
            "sha256": sidecar_sha_after,
            "sqlite_sidecars": list(sidecars_after),
        },
    )
    passed = all(check.passed for check in checks if check.severity == "error")
    return SemanticVisionValidationResult(
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


validate_semantic_vision_n10 = validate_semantic_vision_sidecar


__all__ = [
    "APPLICATION_ID",
    "SCHEMA_VERSION",
    "SemanticVisionValidationError",
    "SemanticVisionValidationResult",
    "ValidationCheck",
    "independent_logical_sha256",
    "validate_semantic_vision_n10",
    "validate_semantic_vision_sidecar",
]
