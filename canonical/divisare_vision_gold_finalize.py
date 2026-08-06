"""Freeze a reviewed, hash-verified Divisare Vision N100 gold manifest."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from canonical.divisare_vision_gold import (
    CANDIDATE_MANIFEST_VERSION,
    CLASSES,
    FINAL_CELL_QUOTAS,
    GENERATION_GROUPS,
    GOLD_MANIFEST_VERSION,
    PHASH_VERSION as CANDIDATE_PHASH_VERSION,
    PIXEL_HASH_VERSION as CANDIDATE_PIXEL_HASH_VERSION,
    REVIEWED_POOL_VERSION,
    SOURCE_PROFILE,
    validate_candidate_manifest,
)
from canonical.divisare_vision_probe import (
    IDENTITY_PROFILE as PROBE_IDENTITY_PROFILE,
    NORMALIZED_LONG_EDGE,
    SOURCE_LONG_EDGE,
    PHASH_VERSION as PROBE_PHASH_VERSION,
    PIXEL_HASH_VERSION as PROBE_PIXEL_HASH_VERSION,
    PROBE_VERSION,
    ProbeConfig,
    _logical_sha256 as probe_logical_sha256,
)


FINALIZER_VERSION = "divisare-vision-gold-finalizer-v1.0.0"
SELECTION_POLICY_VERSION = "divisare-vision-gold-selection-v1.0.0"
EXPECTED_CANDIDATE_COUNT = 560
EXPECTED_GOLD_COUNT = 100
MAX_NOTES_LENGTH = 4_000
MAX_SEARCH_STATES = 250_000

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAMPLE_ID_RE = re.compile(r"^sample-[0-9]{4}$")

CELL_ORDER = tuple(
    (label, generation, clarity)
    for label in CLASSES
    for generation, clarity in FINAL_CELL_QUOTAS
)
CELL_QUOTAS = {
    (label, generation, clarity): quota
    for label in CLASSES
    for (generation, clarity), quota in FINAL_CELL_QUOTAS.items()
}

HASH_EVIDENCE_FIELDS = (
    "probe_status",
    "probe_final_url",
    "http_status",
    "response_mime",
    "response_bytes",
    "content_sha256",
    "original_format",
    "original_mode",
    "original_width",
    "original_height",
    "frame_count",
    "exif_orientation",
    "orientation_applied",
    "oriented_width",
    "oriented_height",
    "alpha_composited",
    "icc_profile_present",
    "color_normalization",
    "normalized_width",
    "normalized_height",
    "pixel_sha256",
    "phash_256",
    "exact_duplicate_group",
    "is_exact_pixel_duplicate",
    "duplicate_of",
    "auto_exclude_exact_duplicate",
    "phash_le8_matches",
    "has_phash_le8_candidate",
    "probe_attempt_count",
    "probe_elapsed_ms",
    "probe_completed_at",
    "probe_error_kind",
    "probe_error_message",
)

REVIEW_IDENTITY_FIELDS = (
    "asset_key",
    "article_id",
    "building_id",
    "request_url",
    "review_url",
    "generation_group",
    "url_generation",
    "content_sha256",
    "pixel_sha256",
    "phash_256",
)

REVIEW_DECISION_FIELDS = frozenset(
    {
        "candidate_id",
        *REVIEW_IDENTITY_FIELDS,
        "delivery_lane",
        "duplicate_of",
        "disposition",
        "gold_label",
        "clarity",
        "acceptable_labels",
        "high_res_viewed",
        "notes",
        "reviewed_at",
    }
)

DISCOVERY_HINT_FIELDS = frozenset(
    {
        "weak_hints",
        "discovery_class",
        "discovery_score",
        "discovery_reasons",
        "filename_hints",
        "article_hints",
        "album_priors",
        "source_url",
        "original_filename",
        "project_name",
        "article_title",
        "article_kind",
        "kind_status",
        "country",
        "role",
        "position",
        "stable_order",
        "country_cap_fallback",
    }
)

SOURCE_IDENTITY_FIELDS = frozenset(
    {
        "candidate_id",
        "candidate_rank",
        "asset_key",
        "article_id",
        "building_id",
        "request_url",
        "review_url",
        "generation_group",
        "url_generation",
    }
)
HUMAN_REVIEW_FIELDS = frozenset(
    {
        "disposition",
        "gold_label",
        "clarity",
        "acceptable_labels",
        "high_res_viewed",
        "notes",
        "reviewed_at",
    }
)
PROBE_ATTEMPT_FIELDS = frozenset(
    {
        "candidate_id",
        "attempt_no",
        "started_at",
        "elapsed_ms",
        "outcome",
        "final_url",
        "http_status",
        "response_mime",
        "response_bytes",
        "content_sha256",
        "error_kind",
        "error_message",
    }
)


class GoldQuotaError(ValueError):
    """Raised when reviewed, canonical candidates cannot satisfy a quota cell."""

    def __init__(self, shortfalls: Mapping[str, Mapping[str, int]]) -> None:
        self.shortfalls = {key: dict(value) for key, value in shortfalls.items()}
        details = "; ".join(
            "%s required=%d eligible=%d selected=%d"
            % (
                key,
                value["required"],
                value["eligible"],
                value["selected"],
            )
            for key, value in sorted(self.shortfalls.items())
        )
        super().__init__("gold quota shortfall: " + details)


def _reject_json_constant(value: str) -> None:
    raise ValueError("non-finite JSON number is forbidden: %s" % value)


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError("duplicate JSON object key: %s" % key)
        output[key] = value
    return output


def parse_json_strict(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("%s is not valid UTF-8 JSON" % label) from exc
    if not isinstance(value, dict):
        raise ValueError("%s must be a JSON object" % label)
    return value


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_value(value: Any) -> str:
    return _sha256_bytes(canonical_json_bytes(value))


def _without_field(payload: Mapping[str, Any], field: str) -> dict[str, Any]:
    value = dict(payload)
    value.pop(field, None)
    return value


def candidate_manifest_sha256(payload: Mapping[str, Any]) -> str:
    return _sha256_value(_without_field(payload, "manifest_sha256"))


def reviewed_pool_sha256(payload: Mapping[str, Any]) -> str:
    return _sha256_value(_without_field(payload, "reviewed_pool_sha256"))


def gold_manifest_sha256(payload: Mapping[str, Any]) -> str:
    return _sha256_value(_without_field(payload, "gold_manifest_sha256"))


def _logical_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    provenance = copy.deepcopy(dict(payload["provenance"]))
    provenance.pop("candidate_manifest_file_sha256", None)
    provenance.pop("reviewed_pool_file_sha256", None)
    return {
        "manifest_version": payload["manifest_version"],
        "finalizer_version": payload["finalizer_version"],
        "provenance": provenance,
        "selection_policy": payload["selection_policy"],
        "selection_metrics": payload["selection_metrics"],
        "samples": payload["samples"],
    }


def gold_logical_sha256(payload: Mapping[str, Any]) -> str:
    return _sha256_value(_logical_payload(payload))


def _require_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("%s must be an object" % name)
    return value


def _require_sha(value: Any, name: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ValueError("%s must be 64 lowercase hexadecimal characters" % name)
    return value


def _require_nonempty_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("%s must be a non-empty string" % name)
    return value


def _find_discovery_hint(value: Any, path: str = "sample") -> str | None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in DISCOVERY_HINT_FIELDS:
                return "%s.%s" % (path, key)
            found = _find_discovery_hint(item, "%s.%s" % (path, key))
            if found is not None:
                return found
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found = _find_discovery_hint(item, "%s[%d]" % (path, index))
            if found is not None:
                return found
    return None


def phash_distance(left: str, right: str) -> int:
    _require_sha(left, "left pHash")
    _require_sha(right, "right pHash")
    return (int(left, 16) ^ int(right, 16)).bit_count()


def _expected_duplicate_evidence(
    candidates: Sequence[Mapping[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, tuple[str | None, str | None]],
    dict[str, list[dict[str, Any]]],
]:
    ordered = sorted(candidates, key=lambda row: int(row["candidate_rank"]))
    exact_buckets: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in ordered:
        exact_buckets[str(row["pixel_sha256"])].append(row)

    exact_groups: list[dict[str, Any]] = []
    duplicate_status: dict[str, tuple[str | None, str | None]] = {
        str(row["candidate_id"]): (None, None) for row in ordered
    }
    duplicate_sets = sorted(
        (rows for rows in exact_buckets.values() if len(rows) > 1),
        key=lambda rows: int(rows[0]["candidate_rank"]),
    )
    for index, rows in enumerate(duplicate_sets, 1):
        group_id = "exact-pixel-%04d" % index
        representative = str(rows[0]["candidate_id"])
        members = [str(row["candidate_id"]) for row in rows]
        exact_groups.append(
            {
                "group_id": group_id,
                "pixel_sha256": rows[0]["pixel_sha256"],
                "representative_candidate_id": representative,
                "member_candidate_ids": members,
                "member_count": len(rows),
            }
        )
        for candidate_id in members:
            duplicate_status[candidate_id] = (
                group_id,
                None if candidate_id == representative else representative,
            )

    duplicate_pairs: list[dict[str, Any]] = []
    audit_pairs: list[dict[str, Any]] = []
    matches: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for left_index, left in enumerate(ordered):
        for right in ordered[left_index + 1 :]:
            distance = phash_distance(str(left["phash_256"]), str(right["phash_256"]))
            if distance > 16:
                continue
            pair = {
                "candidate_id_a": left["candidate_id"],
                "candidate_id_b": right["candidate_id"],
                "phash_distance": distance,
                "exact_pixel_duplicate": left["pixel_sha256"] == right["pixel_sha256"],
            }
            if distance <= 8:
                duplicate_pairs.append(pair)
                matches[str(left["candidate_id"])].append(
                    {"candidate_id": right["candidate_id"], "distance": distance}
                )
                matches[str(right["candidate_id"])].append(
                    {"candidate_id": left["candidate_id"], "distance": distance}
                )
            else:
                audit_pairs.append(pair)

    pair_order = lambda row: (  # noqa: E731
        int(row["phash_distance"]),
        str(row["candidate_id_a"]),
        str(row["candidate_id_b"]),
    )
    duplicate_pairs.sort(key=pair_order)
    audit_pairs.sort(key=pair_order)
    for values in matches.values():
        values.sort(key=lambda row: (int(row["distance"]), str(row["candidate_id"])))
    return exact_groups, duplicate_pairs, audit_pairs, duplicate_status, dict(matches)


def _validate_probe_attempts(
    payload: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]]
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    raw_attempts = payload.get("probe_attempts")
    if not isinstance(raw_attempts, list):
        raise ValueError("probe_attempts must be a list")
    by_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    candidate_by_id = {str(row["candidate_id"]): row for row in candidates}
    seen: set[tuple[str, int]] = set()
    prior_order: tuple[str, int] | None = None
    for raw in raw_attempts:
        attempt = dict(_require_mapping(raw, "probe attempt"))
        candidate_id = str(attempt.get("candidate_id") or "")
        if candidate_id not in candidate_by_id:
            raise ValueError("probe attempt has unknown candidate_id: %s" % candidate_id)
        attempt_no = attempt.get("attempt_no")
        if not isinstance(attempt_no, int) or isinstance(attempt_no, bool) or attempt_no < 1:
            raise ValueError("probe attempt_no must be a positive integer")
        identity = (candidate_id, attempt_no)
        if identity in seen:
            raise ValueError("duplicate probe attempt: %s/%d" % identity)
        if prior_order is not None and identity <= prior_order:
            raise ValueError("probe_attempts must be ordered by candidate_id and attempt_no")
        prior_order = identity
        seen.add(identity)
        if attempt.get("outcome") not in ("success", "failed"):
            raise ValueError("probe attempt outcome must be success or failed")
        if not isinstance(attempt.get("elapsed_ms"), int) or attempt["elapsed_ms"] < 0:
            raise ValueError("probe attempt elapsed_ms must be non-negative")
        _require_nonempty_string(attempt.get("started_at"), "probe attempt started_at")
        if attempt["outcome"] == "success":
            if attempt.get("error_kind") is not None or attempt.get("error_message") is not None:
                raise ValueError("successful probe attempt carries an error: %s" % candidate_id)
        else:
            _require_nonempty_string(
                attempt.get("error_kind"), "failed probe attempt error_kind"
            )
            _require_nonempty_string(
                attempt.get("error_message"), "failed probe attempt error_message"
            )
        by_id[candidate_id].append(attempt)

    for candidate_id, candidate in candidate_by_id.items():
        attempts = by_id.get(candidate_id, [])
        expected_count = candidate.get("probe_attempt_count")
        if not isinstance(expected_count, int) or isinstance(expected_count, bool) or expected_count < 1:
            raise ValueError("invalid probe_attempt_count for %s" % candidate_id)
        max_attempts = _require_mapping(
            payload.get("probe_contract"), "probe_contract"
        ).get("max_attempts")
        if isinstance(max_attempts, int) and expected_count > max_attempts:
            raise ValueError("probe_attempt_count exceeds contract for %s" % candidate_id)
        if len(attempts) != expected_count:
            raise ValueError("probe attempt count mismatch for %s" % candidate_id)
        if [row["attempt_no"] for row in attempts] != list(range(1, len(attempts) + 1)):
            raise ValueError("probe attempts are not contiguous for %s" % candidate_id)
        status = candidate.get("probe_status")
        if status == "success":
            if (
                any(row["outcome"] != "failed" for row in attempts[:-1])
                or attempts[-1]["outcome"] != "success"
            ):
                raise ValueError(
                    "successful candidate has invalid retry history: %s" % candidate_id
                )
        elif status == "failed":
            if any(row["outcome"] != "failed" for row in attempts):
                raise ValueError(
                    "failed candidate has invalid retry history: %s" % candidate_id
                )
        else:
            raise ValueError("candidate has non-terminal probe status: %s" % candidate_id)

        terminal = attempts[-1]
        for field in (
            "final_url",
            "http_status",
            "response_mime",
            "response_bytes",
            "content_sha256",
        ):
            candidate_field = "probe_final_url" if field == "final_url" else field
            if terminal.get(field) != candidate.get(candidate_field):
                raise ValueError(
                    "terminal probe attempt mismatch for %s: %s"
                    % (candidate_id, field)
                )
        if status == "success":
            if terminal.get("error_kind") is not None or terminal.get("error_message") is not None:
                raise ValueError("successful probe attempt carries an error: %s" % candidate_id)
        else:
            for field, candidate_field in (
                ("error_kind", "probe_error_kind"),
                ("error_message", "probe_error_message"),
            ):
                if terminal.get(field) != candidate.get(candidate_field):
                    raise ValueError(
                        "terminal probe attempt mismatch for %s: %s"
                        % (candidate_id, field)
                    )

    statuses = Counter(str(row.get("probe_status")) for row in candidates)
    errors = Counter(
        str(row.get("probe_error_kind"))
        for row in candidates
        if row.get("probe_status") == "failed"
    )
    metrics = {
        "candidate_count": len(candidates),
        "success_count": statuses.get("success", 0),
        "failure_count": statuses.get("failed", 0),
        "pending_count": statuses.get("pending", 0),
        "attempt_count": len(raw_attempts),
        "successful_probe_attempts": sum(
            row.get("outcome") == "success" for row in raw_attempts
        ),
        "http_2xx_attempts": sum(
            row.get("http_status") is not None
            and 200 <= int(row["http_status"]) <= 299
            for row in raw_attempts
        ),
        "failed_attempts": sum(row.get("outcome") == "failed" for row in raw_attempts),
        "downloaded_bytes": sum(int(row.get("response_bytes") or 0) for row in raw_attempts),
        "errors_by_kind": dict(sorted(errors.items())),
    }
    return dict(by_id), metrics


def _reconstructed_probe_results(
    candidates: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "candidate_rank": candidate["candidate_rank"],
            "candidate_id": candidate["candidate_id"],
            "asset_key": candidate["asset_key"],
            "request_url": candidate["request_url"],
            "status": candidate["probe_status"],
            "attempt_count": candidate["probe_attempt_count"],
            "elapsed_ms": candidate["probe_elapsed_ms"],
            "final_url": candidate["probe_final_url"],
            "http_status": candidate["http_status"],
            "response_mime": candidate["response_mime"],
            "response_bytes": candidate["response_bytes"],
            "content_sha256": candidate["content_sha256"],
            "original_format": candidate["original_format"],
            "original_mode": candidate["original_mode"],
            "original_width": candidate["original_width"],
            "original_height": candidate["original_height"],
            "frame_count": candidate["frame_count"],
            "exif_orientation": candidate["exif_orientation"],
            "orientation_applied": candidate["orientation_applied"],
            "oriented_width": candidate["oriented_width"],
            "oriented_height": candidate["oriented_height"],
            "alpha_composited": candidate["alpha_composited"],
            "icc_profile_present": candidate["icc_profile_present"],
            "color_normalization": candidate["color_normalization"],
            "normalized_width": candidate["normalized_width"],
            "normalized_height": candidate["normalized_height"],
            "pixel_sha256": candidate["pixel_sha256"],
            "phash_256": candidate["phash_256"],
            "error_kind": candidate["probe_error_kind"],
        }
        for candidate in candidates
    ]


def validate_enriched_candidate_manifest(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    canonical_json_bytes(payload)
    validate_candidate_manifest(payload)
    declared_sha = _require_sha(payload.get("manifest_sha256"), "candidate manifest SHA")
    if declared_sha != candidate_manifest_sha256(payload):
        raise ValueError("candidate manifest SHA mismatch under strict canonical JSON")

    candidates_raw = payload.get("candidates")
    if not isinstance(candidates_raw, list) or len(candidates_raw) != EXPECTED_CANDIDATE_COUNT:
        raise ValueError("enriched candidate manifest must contain exactly 560 candidates")
    candidates = [dict(_require_mapping(row, "candidate")) for row in candidates_raw]

    probe_contract = _require_mapping(payload.get("probe_contract"), "probe_contract")
    contract = _require_mapping(payload.get("contract"), "candidate contract")
    required_probe_contract = {
        "probe_version": PROBE_VERSION,
        "identity_profile": PROBE_IDENTITY_PROFILE,
        "pixel_hash_version": PROBE_PIXEL_HASH_VERSION,
        "phash_version": PROBE_PHASH_VERSION,
        "source_request_profile": SOURCE_PROFILE,
        "normalized_long_edge": NORMALIZED_LONG_EDGE,
        "images_persisted": False,
    }
    for field, expected in required_probe_contract.items():
        if probe_contract.get(field) != expected:
            raise ValueError("probe contract mismatch: %s" % field)
    if contract.get("pixel_hash_version") != probe_contract.get("pixel_hash_version"):
        raise ValueError("candidate/probe pixel_hash_version mismatch")
    if contract.get("phash_version") != probe_contract.get("phash_version"):
        raise ValueError("candidate/probe phash_version mismatch")
    if contract.get("identity_profile") != probe_contract.get("identity_profile"):
        raise ValueError("candidate/probe identity normalization mismatch")
    if contract.get("pixel_hash_version") != CANDIDATE_PIXEL_HASH_VERSION:
        raise ValueError("candidate pixel hash contract mismatch")
    if contract.get("phash_version") != CANDIDATE_PHASH_VERSION:
        raise ValueError("candidate pHash contract mismatch")
    for field in ("input_manifest_sha256", "input_manifest_file_sha256", "logical_sha256"):
        _require_sha(probe_contract.get(field), "probe_contract.%s" % field)
    for field in ("started_at", "completed_at"):
        _require_nonempty_string(probe_contract.get(field), "probe_contract.%s" % field)
    runtime_versions = _require_mapping(
        probe_contract.get("runtime_versions"), "probe_contract.runtime_versions"
    )
    if set(runtime_versions) != {"python", "pillow", "imagehash", "numpy"}:
        raise ValueError("probe runtime_versions contract mismatch")
    for field, value in runtime_versions.items():
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError("invalid probe runtime version: %s" % field)
    max_attempts = probe_contract.get("max_attempts")
    if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts < 1:
        raise ValueError("probe_contract.max_attempts must be positive")
    config_values = {
        "workers": probe_contract.get("workers"),
        "max_bytes": probe_contract.get("max_bytes"),
        "connect_timeout": probe_contract.get("connect_timeout"),
        "read_timeout": probe_contract.get("read_timeout"),
        "max_attempts": max_attempts,
    }
    for field in ("workers", "max_bytes", "max_attempts"):
        if not isinstance(config_values[field], int) or isinstance(config_values[field], bool):
            raise ValueError("probe contract config must be integer: %s" % field)
    for field in ("connect_timeout", "read_timeout"):
        if not isinstance(config_values[field], (int, float)) or isinstance(
            config_values[field], bool
        ):
            raise ValueError("probe contract timeout is invalid: %s" % field)
    config = ProbeConfig(**config_values)
    config.validate()
    _require_nonempty_string(
        probe_contract.get("input_manifest_filename"),
        "probe_contract.input_manifest_filename",
    )

    for rank, candidate in enumerate(candidates, 1):
        candidate_id = str(candidate["candidate_id"])
        if candidate.get("candidate_rank") != rank:
            raise ValueError("enriched candidate ranks must be contiguous")
        status = candidate.get("probe_status")
        if status not in ("success", "failed"):
            raise ValueError("candidate has non-terminal probe status: %s" % candidate_id)
        _require_nonempty_string(
            candidate.get("probe_completed_at"), "%s probe_completed_at" % candidate_id
        )
        if not isinstance(candidate.get("probe_elapsed_ms"), int) or candidate["probe_elapsed_ms"] < 0:
            raise ValueError("candidate has invalid probe_elapsed_ms: %s" % candidate_id)
        probe_final_url = candidate.get("probe_final_url")
        if probe_final_url is not None and (
            not isinstance(probe_final_url, str) or not probe_final_url.strip()
        ):
            raise ValueError("candidate has invalid probe_final_url: %s" % candidate_id)
        response_mime = candidate.get("response_mime")
        if response_mime is not None and (
            not isinstance(response_mime, str) or not response_mime.strip()
        ):
            raise ValueError("candidate has invalid response_mime: %s" % candidate_id)
        http_status = candidate.get("http_status")
        if http_status is not None and (
            not isinstance(http_status, int)
            or isinstance(http_status, bool)
            or not 100 <= http_status <= 599
        ):
            raise ValueError("candidate has invalid HTTP status: %s" % candidate_id)
        response_bytes = candidate.get("response_bytes")
        if response_bytes is not None and (
            not isinstance(response_bytes, int)
            or isinstance(response_bytes, bool)
            or response_bytes < 0
        ):
            raise ValueError("candidate has invalid response_bytes: %s" % candidate_id)

        if status == "success":
            if http_status is None or not 200 <= http_status <= 299:
                raise ValueError(
                    "candidate has invalid successful HTTP status: %s" % candidate_id
                )
            if response_bytes is None or response_bytes <= 0:
                raise ValueError("candidate has invalid response_bytes: %s" % candidate_id)
            for field in ("content_sha256", "pixel_sha256", "phash_256"):
                _require_sha(candidate.get(field), "%s %s" % (candidate_id, field))
            for field in (
                "original_width",
                "original_height",
                "oriented_width",
                "oriented_height",
                "normalized_width",
                "normalized_height",
                "frame_count",
            ):
                value = candidate.get(field)
                if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                    raise ValueError("candidate has invalid %s: %s" % (field, candidate_id))
            if max(candidate["original_width"], candidate["original_height"]) > SOURCE_LONG_EDGE:
                raise ValueError("candidate source dimensions exceed contract: %s" % candidate_id)
            if max(candidate["normalized_width"], candidate["normalized_height"]) > NORMALIZED_LONG_EDGE:
                raise ValueError("candidate normalized dimensions exceed contract: %s" % candidate_id)
            expected_normalized_edge = min(
                max(candidate["oriented_width"], candidate["oriented_height"]),
                NORMALIZED_LONG_EDGE,
            )
            if max(candidate["normalized_width"], candidate["normalized_height"]) != expected_normalized_edge:
                raise ValueError("candidate normalized long edge mismatch: %s" % candidate_id)
            for field in ("orientation_applied", "alpha_composited", "icc_profile_present"):
                if not isinstance(candidate.get(field), bool):
                    raise ValueError("candidate has invalid boolean %s: %s" % (field, candidate_id))
            for field in ("original_format", "original_mode", "color_normalization"):
                _require_nonempty_string(candidate.get(field), "%s %s" % (candidate_id, field))
            if candidate.get("probe_error_kind") is not None or candidate.get("probe_error_message") is not None:
                raise ValueError("successful candidate carries probe error: %s" % candidate_id)
        else:
            _require_nonempty_string(
                candidate.get("probe_error_kind"), "%s probe_error_kind" % candidate_id
            )
            _require_nonempty_string(
                candidate.get("probe_error_message"),
                "%s probe_error_message" % candidate_id,
            )
            failed_null_fields = (
                "content_sha256",
                "original_format",
                "original_mode",
                "original_width",
                "original_height",
                "frame_count",
                "exif_orientation",
                "orientation_applied",
                "oriented_width",
                "oriented_height",
                "alpha_composited",
                "icc_profile_present",
                "color_normalization",
                "normalized_width",
                "normalized_height",
                "pixel_sha256",
                "phash_256",
            )
            for field in failed_null_fields:
                if candidate.get(field) is not None:
                    raise ValueError(
                        "failed candidate carries image evidence: %s/%s"
                        % (candidate_id, field)
                    )

    successful_candidates = [
        candidate for candidate in candidates if candidate["probe_status"] == "success"
    ]
    exact, pairs, audit, duplicate_status, matches = _expected_duplicate_evidence(
        successful_candidates
    )
    if payload.get("exact_pixel_duplicate_groups") != exact:
        raise ValueError("exact pixel duplicate evidence mismatch")
    if payload.get("phash_duplicate_pairs_le_8") != pairs:
        raise ValueError("pHash <=8 duplicate evidence mismatch")
    if payload.get("phash_audit_pairs_9_16") != audit:
        raise ValueError("pHash 9..16 audit evidence mismatch")
    for candidate in candidates:
        candidate_id = str(candidate["candidate_id"])
        expected_group, expected_duplicate_of = duplicate_status.get(
            candidate_id, (None, None)
        )
        if candidate.get("exact_duplicate_group") != expected_group:
            raise ValueError("candidate exact duplicate group mismatch: %s" % candidate_id)
        if candidate.get("duplicate_of") != expected_duplicate_of:
            raise ValueError("candidate duplicate_of mismatch: %s" % candidate_id)
        if candidate.get("is_exact_pixel_duplicate") is not (expected_group is not None):
            raise ValueError("candidate exact duplicate boolean mismatch: %s" % candidate_id)
        if candidate.get("auto_exclude_exact_duplicate") is not (
            expected_duplicate_of is not None
        ):
            raise ValueError("candidate auto-exclude duplicate mismatch: %s" % candidate_id)
        if candidate.get("phash_le8_matches") != matches.get(candidate_id, []):
            raise ValueError("candidate pHash match evidence mismatch: %s" % candidate_id)
        if candidate.get("has_phash_le8_candidate") is not bool(matches.get(candidate_id, [])):
            raise ValueError("candidate pHash match boolean mismatch: %s" % candidate_id)

    _, expected_metrics = _validate_probe_attempts(payload, candidates)
    if probe_contract.get("metrics") != expected_metrics:
        raise ValueError("probe metrics mismatch")

    reconstructed_input = copy.deepcopy(dict(payload))
    reconstructed_input.pop("manifest_sha256", None)
    for field in (
        "probe_contract",
        "exact_pixel_duplicate_groups",
        "phash_duplicate_pairs_le_8",
        "phash_audit_pairs_9_16",
        "probe_attempts",
    ):
        reconstructed_input.pop(field, None)
    for candidate in reconstructed_input["candidates"]:
        for field in HASH_EVIDENCE_FIELDS:
            candidate.pop(field, None)
    reconstructed_input["manifest_sha256"] = probe_contract["input_manifest_sha256"]
    validate_candidate_manifest(reconstructed_input)
    if candidate_manifest_sha256(reconstructed_input) != probe_contract["input_manifest_sha256"]:
        raise ValueError("probe input manifest reconstruction SHA mismatch")
    reconstructed_results = _reconstructed_probe_results(candidates)
    expected_logical = probe_logical_sha256(
        manifest=reconstructed_input,
        manifest_file_sha=probe_contract["input_manifest_file_sha256"],
        config=config,
        runtime_versions=runtime_versions,
        results=reconstructed_results,
        exact_groups=exact,
        duplicate_pairs=pairs,
        audit_pairs=audit,
    )
    if probe_contract.get("logical_sha256") != expected_logical:
        raise ValueError("probe logical SHA mismatch")
    return candidates


def _validate_review_decision(
    raw: Mapping[str, Any], candidate: Mapping[str, Any]
) -> dict[str, Any]:
    unknown = set(raw) - REVIEW_DECISION_FIELDS
    if unknown:
        raise ValueError("review decision contains unsupported fields: " + ", ".join(sorted(unknown)))
    leaked = DISCOVERY_HINT_FIELDS.intersection(raw)
    if leaked:
        raise ValueError("review decision contains discovery hints: " + ", ".join(sorted(leaked)))
    candidate_id = str(candidate["candidate_id"])
    if raw.get("candidate_id") != candidate_id:
        raise ValueError("review candidate order/identity mismatch: %s" % candidate_id)
    for field in REVIEW_IDENTITY_FIELDS:
        if raw.get(field) != candidate.get(field):
            raise ValueError("review identity mismatch for %s: %s" % (candidate_id, field))
    expected_duplicate = candidate.get("duplicate_of")
    if expected_duplicate is not None:
        if raw.get("duplicate_of") != expected_duplicate:
            raise ValueError("review duplicate identity mismatch: %s" % candidate_id)
    elif raw.get("duplicate_of") is not None:
        raise ValueError("review duplicate identity mismatch: %s" % candidate_id)
    if "delivery_lane" in raw or "delivery_lane" in candidate:
        if raw.get("delivery_lane") != candidate.get("delivery_lane"):
            raise ValueError("review delivery_lane mismatch: %s" % candidate_id)

    disposition = raw.get("disposition")
    if disposition not in ("include", "exclude"):
        raise ValueError("invalid review disposition: %s" % candidate_id)
    notes = raw.get("notes")
    if not isinstance(notes, str) or notes != notes.strip() or len(notes) > MAX_NOTES_LENGTH:
        raise ValueError("invalid reviewer notes: %s" % candidate_id)
    if not isinstance(raw.get("high_res_viewed"), bool):
        raise ValueError("invalid high_res_viewed flag: %s" % candidate_id)
    _require_nonempty_string(raw.get("reviewed_at"), "%s reviewed_at" % candidate_id)
    acceptable = raw.get("acceptable_labels")
    if not isinstance(acceptable, list) or any(label not in CLASSES for label in acceptable):
        raise ValueError("invalid acceptable_labels: %s" % candidate_id)
    if len(acceptable) != len(set(acceptable)):
        raise ValueError("duplicate acceptable_labels: %s" % candidate_id)
    if acceptable != sorted(acceptable, key=CLASSES.index):
        raise ValueError("acceptable_labels are not in canonical order: %s" % candidate_id)

    gold_label = raw.get("gold_label")
    clarity = raw.get("clarity")
    if disposition == "exclude":
        if gold_label is not None or clarity is not None or acceptable:
            raise ValueError("excluded review carries label evidence: %s" % candidate_id)
    else:
        if gold_label not in CLASSES or clarity not in ("clear", "boundary"):
            raise ValueError("included review has invalid gold label/clarity: %s" % candidate_id)
        if gold_label not in acceptable:
            raise ValueError("acceptable_labels omit gold label: %s" % candidate_id)
        if clarity == "clear" and acceptable != [gold_label]:
            raise ValueError("clear review accepts multiple labels: %s" % candidate_id)
        if clarity == "boundary" and len(acceptable) < 2:
            raise ValueError("boundary review needs at least two labels: %s" % candidate_id)
    return dict(raw)


def validate_reviewed_pool(
    payload: Mapping[str, Any], candidate_manifest: Mapping[str, Any]
) -> list[dict[str, Any]]:
    canonical_json_bytes(payload)
    if payload.get("manifest_version") != REVIEWED_POOL_VERSION:
        raise ValueError("reviewed pool manifest_version mismatch")
    declared = _require_sha(payload.get("reviewed_pool_sha256"), "reviewed pool SHA")
    if declared != reviewed_pool_sha256(payload):
        raise ValueError("reviewed pool SHA mismatch")
    if payload.get("candidate_manifest_version") != CANDIDATE_MANIFEST_VERSION:
        raise ValueError("reviewed pool candidate manifest version mismatch")
    if payload.get("candidate_manifest_sha256") != candidate_manifest.get("manifest_sha256"):
        raise ValueError("reviewed pool is bound to a different candidate manifest")
    if payload.get("source_db_sha256") != candidate_manifest.get("source_db_sha256"):
        raise ValueError("reviewed pool source DB SHA mismatch")
    if payload.get("contract") != candidate_manifest.get("contract"):
        raise ValueError("reviewed pool candidate contract mismatch")
    reviewer = _require_nonempty_string(payload.get("reviewer"), "reviewer")
    if reviewer != reviewer.strip():
        raise ValueError("reviewer must not contain surrounding whitespace")
    _require_nonempty_string(payload.get("exported_at"), "reviewed pool exported_at")

    candidates = candidate_manifest["candidates"]
    raw_decisions = payload.get("decisions")
    if not isinstance(raw_decisions, list) or len(raw_decisions) != EXPECTED_CANDIDATE_COUNT:
        raise ValueError("reviewed pool must contain all 560 decisions")
    if payload.get("total_candidates") != EXPECTED_CANDIDATE_COUNT:
        raise ValueError("reviewed pool total_candidates mismatch")
    if payload.get("decided_count") != EXPECTED_CANDIDATE_COUNT or payload.get("complete") is not True:
        raise ValueError("reviewed pool is incomplete")

    decisions = [
        _validate_review_decision(_require_mapping(raw, "review decision"), candidate)
        for raw, candidate in zip(raw_decisions, candidates)
    ]
    for candidate, decision in zip(candidates, decisions):
        if (
            candidate.get("probe_status") == "failed"
            and decision["disposition"] != "exclude"
        ):
            raise ValueError(
                "failed probe candidate must be excluded: %s"
                % candidate["candidate_id"]
            )
    included = sum(row["disposition"] == "include" for row in decisions)
    excluded = len(decisions) - included
    if payload.get("included_count") != included or payload.get("excluded_count") != excluded:
        raise ValueError("reviewed pool inclusion counts mismatch")
    return decisions


def _cell_key(label: str, generation: str, clarity: str) -> tuple[str, str, str]:
    return label, generation, clarity


def _cell_name(cell: tuple[str, str, str]) -> str:
    return "/".join(cell)


def _build_conflicts(candidates: Sequence[Mapping[str, Any]]) -> dict[str, set[str]]:
    conflicts: dict[str, set[str]] = {
        str(row["candidate_id"]): set() for row in candidates
    }
    for index, left in enumerate(candidates):
        left_id = str(left["candidate_id"])
        for right in candidates[index + 1 :]:
            right_id = str(right["candidate_id"])
            if (
                left["pixel_sha256"] == right["pixel_sha256"]
                or phash_distance(str(left["phash_256"]), str(right["phash_256"])) <= 8
            ):
                conflicts[left_id].add(right_id)
                conflicts[right_id].add(left_id)
    return conflicts


def _select_candidates(
    candidates: Sequence[Mapping[str, Any]], decisions: Sequence[Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    decision_by_id = {str(row["candidate_id"]): row for row in decisions}
    pools: dict[tuple[str, str, str], list[dict[str, Any]]] = {
        cell: [] for cell in CELL_ORDER
    }
    reviewed_included: Counter[tuple[str, str, str]] = Counter()
    duplicate_nonrepresentatives: Counter[tuple[str, str, str]] = Counter()
    for raw_candidate in candidates:
        candidate = dict(raw_candidate)
        decision = decision_by_id[str(candidate["candidate_id"])]
        if candidate.get("probe_status") != "success":
            continue
        if decision["disposition"] != "include":
            continue
        cell = _cell_key(
            str(decision["gold_label"]),
            str(candidate["generation_group"]),
            str(decision["clarity"]),
        )
        reviewed_included[cell] += 1
        if candidate.get("duplicate_of") is not None:
            duplicate_nonrepresentatives[cell] += 1
            continue
        pools[cell].append(candidate)
    for values in pools.values():
        values.sort(key=lambda row: (int(row["candidate_rank"]), str(row["candidate_id"])))

    pre_shortfalls: dict[str, dict[str, int]] = {}
    for cell in CELL_ORDER:
        quota = CELL_QUOTAS[cell]
        eligible = len(pools[cell])
        if eligible < quota:
            pre_shortfalls[_cell_name(cell)] = {
                "required": quota,
                "eligible": eligible,
                "selected": 0,
            }
    if pre_shortfalls:
        raise GoldQuotaError(pre_shortfalls)

    eligible = [row for cell in CELL_ORDER for row in pools[cell]]
    candidate_cell = {
        str(row["candidate_id"]): cell for cell in CELL_ORDER for row in pools[cell]
    }
    by_id = {str(row["candidate_id"]): row for row in eligible}
    conflicts = _build_conflicts(eligible)
    remaining = dict(CELL_QUOTAS)
    selected: list[str] = []
    selected_set: set[str] = set()
    skipped: set[str] = set()
    state_count = 0
    best: tuple[str, ...] = ()

    def search() -> tuple[str, ...] | None:
        nonlocal state_count, best
        state_count += 1
        if state_count > MAX_SEARCH_STATES:
            raise RuntimeError(
                "gold duplicate-constraint search exceeded %d deterministic states"
                % MAX_SEARCH_STATES
            )
        if len(selected) > len(best):
            best = tuple(selected)
        if not any(remaining.values()):
            return tuple(selected)

        choices: list[
            tuple[int, int, int, tuple[str, str, str], list[dict[str, Any]]]
        ] = []
        for order, cell in enumerate(CELL_ORDER):
            need = remaining[cell]
            if need <= 0:
                continue
            available = [
                row
                for row in pools[cell]
                if str(row["candidate_id"]) not in skipped
                and str(row["candidate_id"]) not in selected_set
                and not (conflicts[str(row["candidate_id"])] & selected_set)
            ]
            if len(available) < need:
                return None
            choices.append((len(available) - need, len(available), order, cell, available))
        _, _, _, cell, available = min(choices, key=lambda value: value[:3])
        candidate_id = str(available[0]["candidate_id"])

        selected.append(candidate_id)
        selected_set.add(candidate_id)
        remaining[cell] -= 1
        result = search()
        if result is not None:
            return result
        remaining[cell] += 1
        selected_set.remove(candidate_id)
        selected.pop()

        skipped.add(candidate_id)
        result = search()
        skipped.remove(candidate_id)
        return result

    result = search()
    if result is None:
        best_counts = Counter(candidate_cell[candidate_id] for candidate_id in best)
        shortfalls = {
            _cell_name(cell): {
                "required": CELL_QUOTAS[cell],
                "eligible": len(pools[cell]),
                "selected": best_counts.get(cell, 0),
            }
            for cell in CELL_ORDER
            if best_counts.get(cell, 0) < CELL_QUOTAS[cell]
        }
        raise GoldQuotaError(shortfalls)

    selected_rows = [by_id[candidate_id] for candidate_id in result]
    selected_rows.sort(key=lambda row: (int(row["candidate_rank"]), str(row["candidate_id"])))
    metrics = {
        "reviewed_included_count": sum(reviewed_included.values()),
        "canonical_exact_duplicate_nonrepresentative_count": sum(
            duplicate_nonrepresentatives.values()
        ),
        "eligible_count": len(eligible),
        "selected_count": len(selected_rows),
        "search_state_count": state_count,
        "cells": {
            _cell_name(cell): {
                "required": CELL_QUOTAS[cell],
                "reviewed_included": reviewed_included.get(cell, 0),
                "exact_duplicate_nonrepresentatives": duplicate_nonrepresentatives.get(cell, 0),
                "eligible": len(pools[cell]),
                "selected": sum(
                    candidate_cell[str(row["candidate_id"])] == cell for row in selected_rows
                ),
            }
            for cell in CELL_ORDER
        },
    }
    return selected_rows, metrics


def _selected_sample(
    *,
    sample_rank: int,
    candidate: Mapping[str, Any],
    decision: Mapping[str, Any],
    attempts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    evidence = {field: copy.deepcopy(candidate.get(field)) for field in HASH_EVIDENCE_FIELDS}
    evidence["probe_attempts"] = [copy.deepcopy(dict(row)) for row in attempts]
    return {
        "sample_id": "sample-%04d" % sample_rank,
        "sample_rank": sample_rank,
        "source_identity": {
            "candidate_id": candidate["candidate_id"],
            "candidate_rank": candidate["candidate_rank"],
            "asset_key": candidate["asset_key"],
            "article_id": candidate["article_id"],
            "building_id": candidate["building_id"],
            "request_url": candidate["request_url"],
            "review_url": candidate["review_url"],
            "generation_group": candidate["generation_group"],
            "url_generation": candidate["url_generation"],
        },
        "image_evidence": evidence,
        "human_review": {
            "disposition": decision["disposition"],
            "gold_label": decision["gold_label"],
            "clarity": decision["clarity"],
            "acceptable_labels": list(decision["acceptable_labels"]),
            "high_res_viewed": decision["high_res_viewed"],
            "notes": decision["notes"],
            "reviewed_at": decision["reviewed_at"],
        },
    }


def build_gold_manifest(
    *,
    candidate_manifest: Mapping[str, Any],
    reviewed_pool: Mapping[str, Any],
    candidate_manifest_file_sha256: str,
    reviewed_pool_file_sha256: str,
) -> dict[str, Any]:
    _require_sha(candidate_manifest_file_sha256, "candidate manifest file SHA")
    _require_sha(reviewed_pool_file_sha256, "reviewed pool file SHA")
    candidates = validate_enriched_candidate_manifest(candidate_manifest)
    decisions = validate_reviewed_pool(reviewed_pool, candidate_manifest)
    selected, metrics = _select_candidates(candidates, decisions)
    decisions_by_id = {str(row["candidate_id"]): row for row in decisions}
    attempts_by_id, _ = _validate_probe_attempts(candidate_manifest, candidates)

    samples = [
        _selected_sample(
            sample_rank=index,
            candidate=candidate,
            decision=decisions_by_id[str(candidate["candidate_id"])],
            attempts=attempts_by_id[str(candidate["candidate_id"])],
        )
        for index, candidate in enumerate(selected, 1)
    ]
    output: dict[str, Any] = {
        "manifest_version": GOLD_MANIFEST_VERSION,
        "finalizer_version": FINALIZER_VERSION,
        "provenance": {
            "source_db_filename": candidate_manifest["source_db_filename"],
            "source_db_sha256": candidate_manifest["source_db_sha256"],
            "candidate_manifest_version": candidate_manifest["manifest_version"],
            "candidate_manifest_sha256": candidate_manifest["manifest_sha256"],
            "candidate_manifest_file_sha256": candidate_manifest_file_sha256,
            "selection_input_manifest_sha256": candidate_manifest["probe_contract"][
                "input_manifest_sha256"
            ],
            "selection_contract": copy.deepcopy(candidate_manifest["contract"]),
            "probe_contract": copy.deepcopy(candidate_manifest["probe_contract"]),
            "reviewed_pool_version": reviewed_pool["manifest_version"],
            "reviewed_pool_sha256": reviewed_pool["reviewed_pool_sha256"],
            "reviewed_pool_file_sha256": reviewed_pool_file_sha256,
            "reviewer": reviewed_pool["reviewer"],
            "review_exported_at": reviewed_pool["exported_at"],
        },
        "selection_policy": {
            "policy_version": SELECTION_POLICY_VERSION,
            "class_order": list(CLASSES),
            "cell_order": [_cell_name(cell) for cell in CELL_ORDER],
            "cell_quotas": {
                _cell_name(cell): CELL_QUOTAS[cell] for cell in CELL_ORDER
            },
            "candidate_tie_break": "candidate_rank_then_candidate_id",
            "exact_pixel_policy": "canonical_representative_only",
            "phash_policy": "selected_pair_hamming_distance_gt_8",
            "phash_bits": 256,
        },
        "selection_metrics": metrics,
        "samples": samples,
    }
    output["logical_sha256"] = gold_logical_sha256(output)
    output["gold_manifest_sha256"] = gold_manifest_sha256(output)
    validate_gold_manifest(output)
    return output


def validate_gold_manifest(payload: Mapping[str, Any]) -> None:
    canonical_json_bytes(payload)
    if payload.get("manifest_version") != GOLD_MANIFEST_VERSION:
        raise ValueError("gold manifest version mismatch")
    if payload.get("finalizer_version") != FINALIZER_VERSION:
        raise ValueError("gold finalizer version mismatch")
    declared_logical = _require_sha(payload.get("logical_sha256"), "gold logical SHA")
    if declared_logical != gold_logical_sha256(payload):
        raise ValueError("gold logical SHA mismatch")
    declared_self = _require_sha(payload.get("gold_manifest_sha256"), "gold manifest SHA")
    if declared_self != gold_manifest_sha256(payload):
        raise ValueError("gold manifest SHA mismatch")
    provenance = _require_mapping(payload.get("provenance"), "gold provenance")
    for field in (
        "source_db_sha256",
        "candidate_manifest_sha256",
        "candidate_manifest_file_sha256",
        "selection_input_manifest_sha256",
        "reviewed_pool_sha256",
        "reviewed_pool_file_sha256",
    ):
        _require_sha(provenance.get(field), "provenance.%s" % field)
    if provenance.get("candidate_manifest_version") != CANDIDATE_MANIFEST_VERSION:
        raise ValueError("gold candidate manifest version mismatch")
    if provenance.get("reviewed_pool_version") != REVIEWED_POOL_VERSION:
        raise ValueError("gold reviewed pool version mismatch")

    expected_policy = {
        "policy_version": SELECTION_POLICY_VERSION,
        "class_order": list(CLASSES),
        "cell_order": [_cell_name(cell) for cell in CELL_ORDER],
        "cell_quotas": {_cell_name(cell): CELL_QUOTAS[cell] for cell in CELL_ORDER},
        "candidate_tie_break": "candidate_rank_then_candidate_id",
        "exact_pixel_policy": "canonical_representative_only",
        "phash_policy": "selected_pair_hamming_distance_gt_8",
        "phash_bits": 256,
    }
    if payload.get("selection_policy") != expected_policy:
        raise ValueError("gold selection policy mismatch")

    samples = payload.get("samples")
    if not isinstance(samples, list) or len(samples) != EXPECTED_GOLD_COUNT:
        raise ValueError("gold manifest must contain exactly 100 samples")
    seen_sample_ids: set[str] = set()
    seen_candidates: set[str] = set()
    seen_assets: set[str] = set()
    seen_articles: set[str] = set()
    seen_buildings: set[str] = set()
    counts: Counter[tuple[str, str, str]] = Counter()
    hashes: list[tuple[str, str, str]] = []
    prior_candidate_rank = 0
    for rank, raw in enumerate(samples, 1):
        sample = _require_mapping(raw, "gold sample")
        leaked_path = _find_discovery_hint(sample)
        if leaked_path is not None:
            raise ValueError("gold sample leaks discovery hint at %s" % leaked_path)
        sample_id = sample.get("sample_id")
        if (
            not isinstance(sample_id, str)
            or not SAMPLE_ID_RE.fullmatch(sample_id)
            or sample_id != "sample-%04d" % rank
            or sample_id in seen_sample_ids
        ):
            raise ValueError("gold sample ID/rank mismatch at %d" % rank)
        if sample.get("sample_rank") != rank:
            raise ValueError("gold sample_rank mismatch at %d" % rank)
        seen_sample_ids.add(sample_id)
        source = _require_mapping(sample.get("source_identity"), "sample source_identity")
        evidence = _require_mapping(sample.get("image_evidence"), "sample image_evidence")
        review = _require_mapping(sample.get("human_review"), "sample human_review")
        if set(source) != SOURCE_IDENTITY_FIELDS:
            raise ValueError("gold source_identity schema mismatch: %s" % sample_id)
        if set(evidence) != set(HASH_EVIDENCE_FIELDS) | {"probe_attempts"}:
            raise ValueError("gold image_evidence schema mismatch: %s" % sample_id)
        if set(review) != HUMAN_REVIEW_FIELDS:
            raise ValueError("gold human_review schema mismatch: %s" % sample_id)
        candidate_id = _require_nonempty_string(source.get("candidate_id"), "candidate_id")
        asset_key = _require_nonempty_string(source.get("asset_key"), "asset_key")
        article_key = str(source.get("article_id"))
        building_key = str(source.get("building_id"))
        if (
            candidate_id in seen_candidates
            or asset_key in seen_assets
            or article_key in seen_articles
            or building_key in seen_buildings
        ):
            raise ValueError("gold sample source identities must be unique")
        seen_candidates.add(candidate_id)
        seen_assets.add(asset_key)
        seen_articles.add(article_key)
        seen_buildings.add(building_key)
        candidate_rank = source.get("candidate_rank")
        if (
            not isinstance(candidate_rank, int)
            or isinstance(candidate_rank, bool)
            or candidate_rank <= prior_candidate_rank
        ):
            raise ValueError("gold candidates are not in strict source-rank order")
        prior_candidate_rank = candidate_rank
        if source.get("generation_group") not in GENERATION_GROUPS:
            raise ValueError("gold source generation is invalid: %s" % sample_id)
        expected_generation = (
            "modern"
            if source.get("url_generation") == "cloudinary_public_id"
            else "legacy"
        )
        if source.get("generation_group") != expected_generation:
            raise ValueError("gold source generation identity mismatch: %s" % sample_id)
        for field in ("request_url", "review_url"):
            parts = urlsplit(str(source.get(field) or ""))
            if parts.scheme != "https" or parts.hostname != "images.divisare.com":
                raise ValueError("gold source URL is invalid: %s/%s" % (sample_id, field))
        if evidence.get("probe_status") != "success" or evidence.get("duplicate_of") is not None:
            raise ValueError("gold sample is not a canonical successful image: %s" % sample_id)
        if evidence.get("auto_exclude_exact_duplicate") is not False:
            raise ValueError("gold sample carries auto-exclusion: %s" % sample_id)
        content_sha = _require_sha(
            evidence.get("content_sha256"), "%s content SHA" % sample_id
        )
        pixel_sha = _require_sha(evidence.get("pixel_sha256"), "%s pixel SHA" % sample_id)
        phash = _require_sha(evidence.get("phash_256"), "%s pHash" % sample_id)
        hashes.append((sample_id, pixel_sha, phash))
        attempts = evidence.get("probe_attempts")
        if not isinstance(attempts, list) or len(attempts) != evidence.get(
            "probe_attempt_count"
        ):
            raise ValueError("gold probe attempt accounting mismatch: %s" % sample_id)
        for attempt_no, raw_attempt in enumerate(attempts, 1):
            attempt = _require_mapping(raw_attempt, "gold probe attempt")
            if set(attempt) != PROBE_ATTEMPT_FIELDS:
                raise ValueError("gold probe attempt schema mismatch: %s" % sample_id)
            if (
                attempt.get("candidate_id") != candidate_id
                or attempt.get("attempt_no") != attempt_no
            ):
                raise ValueError("gold probe attempt identity mismatch: %s" % sample_id)
        success_attempt = attempts[-1]
        if (
            success_attempt.get("outcome") != "success"
            or success_attempt.get("content_sha256") != content_sha
            or success_attempt.get("http_status") != evidence.get("http_status")
            or success_attempt.get("response_bytes") != evidence.get("response_bytes")
        ):
            raise ValueError("gold successful probe attempt mismatch: %s" % sample_id)
        if review.get("disposition") != "include":
            raise ValueError("gold sample must be human-included: %s" % sample_id)
        label = review.get("gold_label")
        generation = source.get("generation_group")
        clarity = review.get("clarity")
        cell = (str(label), str(generation), str(clarity))
        if cell not in CELL_QUOTAS:
            raise ValueError("gold sample has unsupported quota cell: %s" % sample_id)
        acceptable = review.get("acceptable_labels")
        if not isinstance(acceptable, list) or label not in acceptable:
            raise ValueError("gold acceptable labels omit primary label: %s" % sample_id)
        if acceptable != sorted(set(acceptable), key=CLASSES.index):
            raise ValueError("gold acceptable labels are not canonical: %s" % sample_id)
        if clarity == "clear" and acceptable != [label]:
            raise ValueError("gold clear sample accepts multiple labels: %s" % sample_id)
        if clarity == "boundary" and len(acceptable) < 2:
            raise ValueError("gold boundary sample needs multiple labels: %s" % sample_id)
        if not isinstance(review.get("high_res_viewed"), bool):
            raise ValueError("gold high_res_viewed is invalid: %s" % sample_id)
        notes = review.get("notes")
        if not isinstance(notes, str) or notes != notes.strip() or len(notes) > MAX_NOTES_LENGTH:
            raise ValueError("gold reviewer notes are invalid: %s" % sample_id)
        _require_nonempty_string(review.get("reviewed_at"), "%s reviewed_at" % sample_id)
        counts[cell] += 1
    for cell, quota in CELL_QUOTAS.items():
        if counts.get(cell, 0) != quota:
            raise ValueError("gold quota mismatch for %s" % _cell_name(cell))

    metrics = _require_mapping(payload.get("selection_metrics"), "selection_metrics")
    if metrics.get("selected_count") != EXPECTED_GOLD_COUNT:
        raise ValueError("gold selection_metrics selected_count mismatch")
    for field in (
        "reviewed_included_count",
        "canonical_exact_duplicate_nonrepresentative_count",
        "eligible_count",
        "search_state_count",
    ):
        if not isinstance(metrics.get(field), int) or isinstance(metrics.get(field), bool):
            raise ValueError("gold selection metric is invalid: %s" % field)
    metric_cells = _require_mapping(metrics.get("cells"), "selection_metrics.cells")
    if set(metric_cells) != {_cell_name(cell) for cell in CELL_ORDER}:
        raise ValueError("gold selection metric cells mismatch")
    reviewed_total = duplicate_total = eligible_total = selected_total = 0
    for cell in CELL_ORDER:
        values = _require_mapping(
            metric_cells[_cell_name(cell)], "selection metric cell %s" % _cell_name(cell)
        )
        if set(values) != {
            "required",
            "reviewed_included",
            "exact_duplicate_nonrepresentatives",
            "eligible",
            "selected",
        }:
            raise ValueError("gold selection metric cell schema mismatch")
        if any(
            not isinstance(values[field], int)
            or isinstance(values[field], bool)
            or values[field] < 0
            for field in values
        ):
            raise ValueError("gold selection metric cell contains invalid counts")
        if (
            values["required"] != CELL_QUOTAS[cell]
            or values["selected"] != counts[cell]
            or values["reviewed_included"]
            != values["eligible"] + values["exact_duplicate_nonrepresentatives"]
            or values["eligible"] < values["selected"]
        ):
            raise ValueError("gold selection metric cell accounting mismatch")
        reviewed_total += values["reviewed_included"]
        duplicate_total += values["exact_duplicate_nonrepresentatives"]
        eligible_total += values["eligible"]
        selected_total += values["selected"]
    if (
        metrics["reviewed_included_count"] != reviewed_total
        or metrics["canonical_exact_duplicate_nonrepresentative_count"] != duplicate_total
        or metrics["eligible_count"] != eligible_total
        or metrics["selected_count"] != selected_total
        or metrics["search_state_count"] < 1
    ):
        raise ValueError("gold selection metrics accounting mismatch")
    for index, (left_id, left_pixel, left_phash) in enumerate(hashes):
        for right_id, right_pixel, right_phash in hashes[index + 1 :]:
            if left_pixel == right_pixel:
                raise ValueError("gold exact pixel duplicate: %s/%s" % (left_id, right_id))
            distance = phash_distance(left_phash, right_phash)
            if distance <= 8:
                raise ValueError(
                    "gold pHash duplicate distance %d: %s/%s"
                    % (distance, left_id, right_id)
                )


def write_json_no_clobber(path: Path, payload: Mapping[str, Any]) -> None:
    if path.exists():
        raise FileExistsError("immutable gold manifest already exists: %s" % path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".partial", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(canonical_json_bytes(payload) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp, path)
        except FileExistsError as exc:
            raise FileExistsError("immutable gold manifest already exists: %s" % path) from exc
    finally:
        temp.unlink(missing_ok=True)


def finalize_gold_files(
    *, candidate_manifest_path: Path, reviewed_pool_path: Path, output_path: Path
) -> dict[str, Any]:
    candidate_manifest_path = candidate_manifest_path.resolve()
    reviewed_pool_path = reviewed_pool_path.resolve()
    output_path = output_path.resolve()
    if len({candidate_manifest_path, reviewed_pool_path, output_path}) != 3:
        raise ValueError("candidate, reviewed-pool, and output paths must be distinct")
    if output_path.exists():
        raise FileExistsError("immutable gold manifest already exists: %s" % output_path)
    candidate_raw = candidate_manifest_path.read_bytes()
    review_raw = reviewed_pool_path.read_bytes()
    candidate = parse_json_strict(candidate_raw, label="candidate manifest")
    review = parse_json_strict(review_raw, label="reviewed pool")
    payload = build_gold_manifest(
        candidate_manifest=candidate,
        reviewed_pool=review,
        candidate_manifest_file_sha256=_sha256_bytes(candidate_raw),
        reviewed_pool_file_sha256=_sha256_bytes(review_raw),
    )
    if candidate_manifest_path.read_bytes() != candidate_raw:
        raise RuntimeError("candidate manifest changed during finalization")
    if reviewed_pool_path.read_bytes() != review_raw:
        raise RuntimeError("reviewed pool changed during finalization")
    write_json_no_clobber(output_path, payload)
    return payload
