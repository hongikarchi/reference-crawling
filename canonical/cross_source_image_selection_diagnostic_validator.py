"""Independent replay validator for E3 diagnostic selection manifests."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from canonical.cross_source_image_selection import canonical_json, canonical_sha256
from canonical.cross_source_image_selection_diagnostic import (
    E3_DIAGNOSTIC_MANIFEST_DOMAIN,
    EVIDENCE_KINDS,
    build_diagnostic_sample_plan,
)
from canonical.cross_source_image_selection_sources import (
    E2ArtifactSpec,
    open_e2_selection_sources,
)


@dataclass(frozen=True)
class DiagnosticValidationCheck:
    name: str
    passed: bool
    expected: str
    actual: str


@dataclass(frozen=True)
class DiagnosticValidationResult:
    manifest_path: Path
    passed: bool
    checks: tuple[DiagnosticValidationCheck, ...]
    manifest_size_bytes: int
    manifest_byte_sha256: str
    diagnostic_manifest_sha256: str | None
    inventory_count: int
    selected_count: int
    inventory_manifest_sha256: str | None
    ordered_selection_manifest_sha256: str | None
    e2_byte_sha256: str | None
    e2_logical_sha256: str | None

    @property
    def failed_check_names(self) -> tuple[str, ...]:
        return tuple(value.name for value in self.checks if not value.passed)

    def as_dict(self) -> dict[str, Any]:
        return {
            "checks": [
                {
                    "actual": value.actual,
                    "expected": value.expected,
                    "name": value.name,
                    "passed": value.passed,
                }
                for value in self.checks
            ],
            "diagnostic_manifest_sha256": self.diagnostic_manifest_sha256,
            "e2_byte_sha256": self.e2_byte_sha256,
            "e2_logical_sha256": self.e2_logical_sha256,
            "failed_check_names": list(self.failed_check_names),
            "inventory_count": self.inventory_count,
            "inventory_manifest_sha256": self.inventory_manifest_sha256,
            "llm_requests": 0,
            "manifest_byte_sha256": self.manifest_byte_sha256,
            "manifest_path": str(self.manifest_path),
            "manifest_size_bytes": self.manifest_size_bytes,
            "network_requests": 0,
            "ordered_selection_manifest_sha256": (
                self.ordered_selection_manifest_sha256
            ),
            "passed": self.passed,
            "selected_count": self.selected_count,
            "vision_requests": 0,
        }


def _sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return canonical_json(value)
    except (TypeError, ValueError):
        return repr(value)


def _safe_sha256(value: Any) -> str:
    try:
        return canonical_sha256(value)
    except (TypeError, ValueError) as exc:
        return f"invalid-canonical-value:{type(exc).__name__}:{exc}"


def _check(
    checks: list[DiagnosticValidationCheck],
    name: str,
    passed: bool,
    expected: Any,
    actual: Any,
) -> None:
    checks.append(
        DiagnosticValidationCheck(
            name=name,
            passed=bool(passed),
            expected=_text(expected),
            actual=_text(actual),
        )
    )


def _expected_payload(
    *,
    plan: Any,
    e2_path: Path,
    e2_size_bytes: int,
    e2_byte_sha256: str,
    e2_logical_sha256: str,
) -> dict[str, Any]:
    # Deliberately reconstruct the manifest body here instead of importing the
    # planner's write function.  The validator and writer only share the
    # versioned data model and canonical hashing primitive.
    payload = plan.as_manifest()
    payload["e2_input"] = {
        "byte_sha256": e2_byte_sha256,
        "logical_sha256": e2_logical_sha256,
        "path": str(e2_path.resolve()),
        "size_bytes": e2_size_bytes,
    }
    payload["diagnostic_manifest_sha256"] = canonical_sha256(
        {
            "domain": E3_DIAGNOSTIC_MANIFEST_DOMAIN,
            "manifest": payload,
        }
    )
    return payload


def validate_diagnostic_manifest(
    manifest_path: Path | str,
    *,
    e2_spec: E2ArtifactSpec,
    expected_sample_size: int,
    expected_sample_seed: str,
    batch_size: int = 1_000,
) -> DiagnosticValidationResult:
    """Replay E2 evidence discovery and compare an entire canonical manifest.

    The expected sample size and seed are caller-owned acceptance parameters;
    they are never adopted from the manifest under test.
    """

    path = Path(manifest_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    size_before = path.stat().st_size
    sha_before = _sha256_file(path)
    raw = path.read_bytes()
    checks: list[DiagnosticValidationCheck] = []
    try:
        decoded = raw.decode("utf-8")
        payload = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _check(checks, "valid_utf8_json", False, "canonical UTF-8 JSON", str(exc))
        return DiagnosticValidationResult(
            manifest_path=path,
            passed=False,
            checks=tuple(checks),
            manifest_size_bytes=size_before,
            manifest_byte_sha256=sha_before,
            diagnostic_manifest_sha256=None,
            inventory_count=0,
            selected_count=0,
            inventory_manifest_sha256=None,
            ordered_selection_manifest_sha256=None,
            e2_byte_sha256=None,
            e2_logical_sha256=None,
        )
    if not isinstance(payload, dict):
        _check(checks, "json_object", False, "object", type(payload).__name__)
        return DiagnosticValidationResult(
            manifest_path=path,
            passed=False,
            checks=tuple(checks),
            manifest_size_bytes=size_before,
            manifest_byte_sha256=sha_before,
            diagnostic_manifest_sha256=None,
            inventory_count=0,
            selected_count=0,
            inventory_manifest_sha256=None,
            ordered_selection_manifest_sha256=None,
            e2_byte_sha256=None,
            e2_logical_sha256=None,
        )

    _check(checks, "valid_utf8_json", True, "valid", "valid")
    try:
        expected_canonical_bytes = (canonical_json(payload) + "\n").encode(
            "utf-8"
        )
        canonical_digest = hashlib.sha256(expected_canonical_bytes).hexdigest()
    except (TypeError, ValueError) as exc:
        expected_canonical_bytes = b""
        canonical_digest = f"valid canonical JSON ({type(exc).__name__}: {exc})"
    _check(
        checks,
        "canonical_json_bytes",
        raw == expected_canonical_bytes,
        canonical_digest,
        sha_before,
    )

    with open_e2_selection_sources(e2_spec, batch_size=batch_size) as source:
        plan = build_diagnostic_sample_plan(
            source,
            sample_size=expected_sample_size,
            seed=expected_sample_seed,
        )
        expected = _expected_payload(
            plan=plan,
            e2_path=source.path,
            e2_size_bytes=source.lineage.artifact_size,
            e2_byte_sha256=source.lineage.artifact_sha256,
            e2_logical_sha256=source.lineage.stored_logical_sha256,
        )
        e2_byte_sha = source.lineage.artifact_sha256
        e2_logical_sha = source.lineage.stored_logical_sha256

    actual_manifest_sha = payload.get("diagnostic_manifest_sha256")
    payload_without_sha = dict(payload)
    payload_without_sha.pop("diagnostic_manifest_sha256", None)
    independently_hashed = _safe_sha256(
        {
            "domain": E3_DIAGNOSTIC_MANIFEST_DOMAIN,
            "manifest": payload_without_sha,
        }
    )
    _check(
        checks,
        "diagnostic_manifest_sha256",
        actual_manifest_sha == independently_hashed,
        independently_hashed,
        actual_manifest_sha,
    )
    _check(
        checks,
        "e2_input_lineage",
        payload.get("e2_input") == expected["e2_input"],
        expected["e2_input"],
        payload.get("e2_input"),
    )
    _check(
        checks,
        "acceptance_sample_parameters",
        payload.get("sample_size") == expected_sample_size
        and payload.get("sample_seed") == expected_sample_seed,
        {"sample_seed": expected_sample_seed, "sample_size": expected_sample_size},
        {
            "sample_seed": payload.get("sample_seed"),
            "sample_size": payload.get("sample_size"),
        },
    )
    _check(
        checks,
        "inventory_manifest_replay",
        payload.get("inventory_manifest_sha256")
        == plan.inventory_manifest_sha256,
        plan.inventory_manifest_sha256,
        payload.get("inventory_manifest_sha256"),
    )
    _check(
        checks,
        "ordered_selection_manifest_replay",
        payload.get("ordered_selection_manifest_sha256")
        == plan.ordered_selection_manifest_sha256,
        plan.ordered_selection_manifest_sha256,
        payload.get("ordered_selection_manifest_sha256"),
    )
    actual_selected = payload.get("selected")
    expected_selected = expected["selected"]
    _check(
        checks,
        "selected_order_and_records",
        actual_selected == expected_selected,
        _safe_sha256(expected_selected),
        _safe_sha256(actual_selected),
    )
    _check(
        checks,
        "suppression_kind_and_count_replay",
        payload.get("population_by_source_and_kind")
        == expected["population_by_source_and_kind"]
        and payload.get("selected_by_source_and_kind")
        == expected["selected_by_source_and_kind"],
        {
            "population": expected["population_by_source_and_kind"],
            "selected": expected["selected_by_source_and_kind"],
        },
        {
            "population": payload.get("population_by_source_and_kind"),
            "selected": payload.get("selected_by_source_and_kind"),
        },
    )
    actual_kinds = {
        kind
        for row in (actual_selected if isinstance(actual_selected, list) else [])
        if isinstance(row, dict)
        for kind in row.get("diagnostic_record", {}).get("evidence_kinds", [])
    }
    _check(
        checks,
        "required_evidence_coverage",
        actual_kinds == set(EVIDENCE_KINDS),
        list(EVIDENCE_KINDS),
        sorted(actual_kinds),
    )
    zero_request = {
        "llm_requests": payload.get("llm_requests"),
        "network_requests": payload.get("network_requests"),
        "vision_requests": payload.get("vision_requests"),
    }
    _check(
        checks,
        "zero_request_flags",
        all(value == 0 for value in zero_request.values()),
        {name: 0 for name in zero_request},
        zero_request,
    )
    safety_flags = {
        "authoritative": payload.get("authoritative"),
        "creates_final_representative": payload.get(
            "creates_final_representative"
        ),
        "creates_vision_tasks": payload.get("creates_vision_tasks"),
        "selection_mode": payload.get("selection_mode"),
    }
    _check(
        checks,
        "candidate_only_safety_flags",
        safety_flags
        == {
            "authoritative": False,
            "creates_final_representative": False,
            "creates_vision_tasks": False,
            "selection_mode": "diagnostic_sample",
        },
        {
            "authoritative": False,
            "creates_final_representative": False,
            "creates_vision_tasks": False,
            "selection_mode": "diagnostic_sample",
        },
        safety_flags,
    )
    _check(
        checks,
        "entire_manifest_replay",
        payload == expected,
        _safe_sha256(expected),
        _safe_sha256(payload),
    )

    size_after = path.stat().st_size
    sha_after = _sha256_file(path)
    _check(
        checks,
        "manifest_read_only_unchanged",
        (size_before, sha_before) == (size_after, sha_after),
        {"sha256": sha_before, "size_bytes": size_before},
        {"sha256": sha_after, "size_bytes": size_after},
    )
    return DiagnosticValidationResult(
        manifest_path=path,
        passed=all(value.passed for value in checks),
        checks=tuple(checks),
        manifest_size_bytes=size_after,
        manifest_byte_sha256=sha_after,
        diagnostic_manifest_sha256=(
            str(actual_manifest_sha) if actual_manifest_sha is not None else None
        ),
        inventory_count=len(plan.inventory),
        selected_count=len(plan.selected),
        inventory_manifest_sha256=plan.inventory_manifest_sha256,
        ordered_selection_manifest_sha256=(
            plan.ordered_selection_manifest_sha256
        ),
        e2_byte_sha256=e2_byte_sha,
        e2_logical_sha256=e2_logical_sha,
    )


__all__ = [
    "DiagnosticValidationCheck",
    "DiagnosticValidationResult",
    "validate_diagnostic_manifest",
]
