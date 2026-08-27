"""Independent validator for the WP2.7 identity-instrument v2 probe.

This module deliberately uses only the Python standard library.  In
particular, it does not import the producer (or any earlier identity producer)
and independently restates the frozen observation, derivation, sample, gate,
and evidence contracts.
"""

from __future__ import annotations

import binascii
import hashlib
import json
import re
import sqlite3
import struct
import zlib
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


VALIDATOR_VERSION = "vnext-identity-instrument-v2-probe-independent-validator-v2"
APPLICATION_ID = 0x57503237
USER_VERSION = 1
RUNNER_VERSION = "vnext-identity-instrument-v2-probe-v1.0.0"
INSTRUMENT_VERSION = "vnext-identity-instrument-v2"
SAMPLE_VERSION = "vnext-identity-instrument-v2-probe-v1"
REVIEW_VERSION = "vnext-identity-instrument-v2-review-v1"
ADJUDICATION_VERSION = "vnext-identity-instrument-v2-axis-adjudication-v1"
GATE_VERSION = "vnext-identity-instrument-v2-predeclared-gate-v1"
SOURCE_SAMPLE_VERSION = "vnext-identity-instrument-probe-v1"
SOURCE_SEED = "vnext-wp26-instrument-probe-v1-20260827"
SOURCE_LOGICAL_SHA256 = "784a567458227a737c78b31320eb79fdfbca0e7cf5909d08bb87c38f12e2b208"
SOURCE_RUN_ID = "wp26-250a8cefe5396b87377ad55d"
TOTAL_SAMPLE = 24
SOURCE_SHEET_COUNT = 48
SOURCE_ASSET_COUNT = 93
MAX_ASSETS_PER_SIDE = 2
HTTP_CAP = 96
SOURCE_PACKET_SHA256 = "b1f0491852d68648405c032e1a8486ed41a939b18eb655cea1aab92ab07b39e5"
SOURCE_VISUAL_INVENTORY_SHA256 = (
    "0cdf2b6b42b9a4866a2e9ee0cb37e5efb9c045915ce83e67d350569749bbe99e"
)
PROTOCOL_SPEC = (
    7_411,
    "ea610cf3fdc86f1bc6c65852fde0c47fc5a4ec92fae48055ea545c183da7660a",
)
ORDER_SPEC = (
    4_508,
    "38618519893c15199ca38c817ceb6bc0a5e0f47e275544274d6d8310cd5c8599",
)

DEFAULT_ARTIFACT = Path("data/enrichment/vnext_identity_instrument_v2_probe_v1.db")
DEFAULT_REPORT = Path("data/reports/vnext_identity_instrument_v2_probe_v1_report.json")
DEFAULT_MANIFEST = Path("data/reports/vnext_identity_instrument_v2_probe_v1_manifest.json")
DEFAULT_VALIDATION_REPORT = Path(
    "data/reports/vnext_identity_instrument_v2_probe_v1_validation.json"
)
DEFAULT_SOURCE_DB = Path("data/enrichment/vnext_identity_instrument_probe_v1.db")
DEFAULT_SOURCE_REPORT = Path("data/reports/vnext_identity_instrument_probe_v1_report.json")
DEFAULT_SOURCE_MANIFEST = Path(
    "data/reports/vnext_identity_instrument_probe_v1_manifest.json"
)
DEFAULT_SOURCE_SAMPLE_MANIFEST = Path(
    "data/reports/vnext_identity_instrument_probe_v1_sample_manifest.json"
)
DEFAULT_FETCH_CORRECTION = Path(
    "data/reports/vnext_identity_instrument_probe_v1_fetch_cap_correction.json"
)
DEFAULT_SOURCE_FETCH_MANIFEST = Path(
    "data/reports/vnext_identity_instrument_probe_v1_fetch_manifest.json"
)
DEFAULT_SOURCE_PRODUCER = Path("canonical/vnext_identity_instrument_probe.py")
DEFAULT_SOURCE_ADJUDICATION_LOCKED = Path(
    "data/reports/vnext_identity_instrument_probe_v1_adjudication_locked.json"
)
DEFAULT_PROTOCOL = Path("docs/vnext/IDENTITY_INSTRUMENT_V2_PROTOCOL.md")
DEFAULT_ORDER = Path(".claude/ops/jobs/20260827_vnext_wp27_instrument_v2_order.md")

SOURCE_FILE_SPECS: dict[Path, tuple[int, str]] = {
    DEFAULT_SOURCE_DB: (
        716_800,
        "a0949e7d75069d869e665a10872fe13a3988897cfc810d36149508950594347a",
    ),
    DEFAULT_SOURCE_SAMPLE_MANIFEST: (
        20_523,
        "6e925f7f07c21fe29d66c0e92a498db81616b189a2b33d5cf2520281d20df06a",
    ),
    DEFAULT_SOURCE_REPORT: (
        19_671,
        "b1c04f394488624d248ce31965a6fbd01345bb5ffb51cfdfa2f9053e925980ab",
    ),
    DEFAULT_SOURCE_MANIFEST: (
        11_232,
        "162be33c250638248e3b03f80644adb883a7a562cba0a36b99fde350c801859f",
    ),
    DEFAULT_FETCH_CORRECTION: (
        2_523,
        "3a2540bb0ab3adf79cd119bfdd42f769c6a21bf8817ab4600e5d033e7c4301c5",
    ),
    DEFAULT_SOURCE_FETCH_MANIFEST: (
        45_145,
        "36c2faef5d925dcab5fbacb827e48a5c7336ebdce9cbf8b6f7ec5dc90139f12a",
    ),
    DEFAULT_SOURCE_PRODUCER: (
        122_514,
        "53ab52464aa0990e0748c1177544007ba97a8930a98d3e16263a72c5d3e93449",
    ),
    DEFAULT_SOURCE_ADJUDICATION_LOCKED: (
        13_415,
        "9bc4f38a4e18933e3ab77fc3e812ab29dd1bd7c4d79af30da760dab27c42a80a",
    ),
}

INPUT_PIN_SPECS: dict[str, tuple[Path, int, str, str]] = {
    "wp27_protocol": (DEFAULT_PROTOCOL, PROTOCOL_SPEC[0], PROTOCOL_SPEC[1], "instrument_contract"),
    "wp27_order": (DEFAULT_ORDER, ORDER_SPEC[0], ORDER_SPEC[1], "work_order"),
    "wp26_source_db": (
        DEFAULT_SOURCE_DB,
        *SOURCE_FILE_SPECS[DEFAULT_SOURCE_DB],
        "sample_evidence_and_reveal_source",
    ),
    "wp26_sample_manifest": (
        DEFAULT_SOURCE_SAMPLE_MANIFEST,
        *SOURCE_FILE_SPECS[DEFAULT_SOURCE_SAMPLE_MANIFEST],
        "sample_binding",
    ),
    "wp26_final_report": (
        DEFAULT_SOURCE_REPORT,
        *SOURCE_FILE_SPECS[DEFAULT_SOURCE_REPORT],
        "post_lock_contrast_source",
    ),
    "wp26_final_manifest": (
        DEFAULT_SOURCE_MANIFEST,
        *SOURCE_FILE_SPECS[DEFAULT_SOURCE_MANIFEST],
        "source_provenance",
    ),
    "wp26_fetch_manifest": (
        DEFAULT_SOURCE_FETCH_MANIFEST,
        *SOURCE_FILE_SPECS[DEFAULT_SOURCE_FETCH_MANIFEST],
        "inherited_fetch_ledger",
    ),
    "wp26_fetch_correction": (
        DEFAULT_FETCH_CORRECTION,
        *SOURCE_FILE_SPECS[DEFAULT_FETCH_CORRECTION],
        "terminal_fetch_cap_correction_overlay",
    ),
    "wp26_producer": (
        DEFAULT_SOURCE_PRODUCER,
        *SOURCE_FILE_SPECS[DEFAULT_SOURCE_PRODUCER],
        "source_producer_provenance",
    ),
    "wp26_adjudication_locked": (
        DEFAULT_SOURCE_ADJUDICATION_LOCKED,
        *SOURCE_FILE_SPECS[DEFAULT_SOURCE_ADJUDICATION_LOCKED],
        "source_axis_lock_provenance",
    ),
}

OBSERVATION_KEYS = (
    "image",
    "name",
    "firm",
    "place",
    "year",
    "scope",
    "basis",
)
CONTROLLED_VALUES: dict[str, frozenset[str]] = {
    "image": frozenset({"same_object", "different_objects", "inconclusive"}),
    "name": frozenset({"agree", "alias", "conflict", "unclear"}),
    "firm": frozenset({"agree", "overlap", "conflict", "unknown"}),
    "place": frozenset({"agree", "unclear", "conflict"}),
    "year": frozenset({"compatible", "conflict", "unclear"}),
    "scope": frozenset(
        {
            "same",
            "component_vs_umbrella",
            "phase_or_extension",
            "unclear",
            "unrelated",
        }
    ),
}
CONTROLLED_VALUE_ORDER: dict[str, tuple[str, ...]] = {
    "image": ("same_object", "different_objects", "inconclusive"),
    "name": ("agree", "alias", "conflict", "unclear"),
    "firm": ("agree", "overlap", "conflict", "unknown"),
    "place": ("agree", "unclear", "conflict"),
    "year": ("compatible", "conflict", "unclear"),
    "scope": (
        "same",
        "component_vs_umbrella",
        "phase_or_extension",
        "unclear",
        "unrelated",
    ),
}
DERIVED_LABELS = frozenset({"same", "related", "different", "insufficient"})
INSTRUMENT_FORBIDDEN_INPUT_FIELDS = frozenset(
    {
        "label",
        "allowed_labels",
        "final_label",
        "adjudicator_label",
        "rationale",
        "hard_conflict_observed",
        "derived_label",
        "derivation_rule",
    }
)

FORM_BINDING_KEYS = (
    "display_order",
    "opaque_review_id",
    "sheet_path",
    "sheet_sha256",
)
FORM_ITEM_KEYS = frozenset((*FORM_BINDING_KEYS, *OBSERVATION_KEYS))
REVIEW_FORM_TOP_KEYS = frozenset(
    {
        "run_id", "review_version", "lane", "instrument_contract_sha256",
        "instructions", "form_contract_sha256", "reviewer", "reviewed_at", "items",
    }
)
REVIEW_LOCK_TOP_KEYS = frozenset(
    {
        "run_id", "review_version", "lane", "instrument_contract_sha256", "reviewer",
        "instructions", "form_contract_sha256", "reviewed_at", "row_count",
        "ordered_ledger_sha256", "items",
    }
)
REVIEW_LOCK_ITEM_KEYS = frozenset(
    {"lane", "opaque_review_id", *OBSERVATION_KEYS, "reviewer", "reviewed_at"}
)
ADJUDICATION_FORM_TOP_KEYS = frozenset(
    {
        "run_id", "adjudication_version", "instrument_contract_sha256",
        "instructions", "form_contract_sha256", "adjudicator", "adjudicated_at", "items",
    }
)
ADJUDICATION_FORM_ITEM_KEYS = frozenset(
    {
        "display_order", "opaque_adjudication_id", "sheet_path", "sheet_sha256",
        *OBSERVATION_KEYS,
    }
)
ADJUDICATION_PACKET_TOP_KEYS = frozenset(
    {
        "run_id", "adjudication_version", "instrument_contract_sha256", "instructions",
        "row_count", "items", "packet_contract_sha256",
    }
)
ADJUDICATION_PACKET_ITEM_KEYS = frozenset(
    {"display_order", "opaque_adjudication_id", "sheet_path", "sheet_sha256", "reviews"}
)
PACKET_REVIEW_KEYS = frozenset(
    {"opaque_review_id", *OBSERVATION_KEYS, "reviewer", "reviewed_at"}
)
ADJUDICATION_LOCK_TOP_KEYS = frozenset(
    {
        "run_id", "adjudication_version", "instrument_contract_sha256", "adjudicator",
        "instructions", "form_contract_sha256", "packet_contract_sha256", "adjudicated_at",
        "row_count", "ordered_ledger_sha256", "items",
    }
)
ADJUDICATION_LOCK_ITEM_KEYS = frozenset(
    {"opaque_adjudication_id", *OBSERVATION_KEYS, "adjudicator", "adjudicated_at"}
)
FORBIDDEN_INPUT_KEYS = frozenset(
    {
        "label",
        "allowed_labels",
        "final_label",
        "final_probe_label",
        "adjudicator_label",
        "derived_label",
        "rule_no",
        "derivation_rule",
        "rationale",
        "hard_conflict_observed",
        "n100_gold_label",
        "v1_probe_label",
    }
)

GROUP_COUNTS = {"P1": 3, "P2": 4, "P3": 2, "D": 15}
GOLD_COUNTS = {"same": 15, "related": 2, "insufficient": 7}
GATE_THRESHOLDS = {
    "d_same": (13, 15),
    "p1_non_same": (2, 3),
    "gold_insufficient_to_insufficient": (4, 7),
    "gold_related_to_related": (1, 2),
}

STATE_SEQUENCE = (
    "instrument_frozen",
    "sample_frozen",
    "criteria_frozen",
    "review_ready",
    "reviews_partial",
    "reviews_locked",
    "adjudication_locked",
)
TERMINAL_STATES = frozenset({"pass", "fail"})

QUOTA_PHASES = (
    "pre_freeze",
    "pre_r1_review",
    "pre_r2_review",
    "pre_adjudication_review",
    "pre_reveal_score",
)

EXPECTED_COLUMNS: dict[str, tuple[str, ...]] = {
    "runs": (
        "run_id", "schema_version", "runner_version", "instrument_version",
        "sample_version", "review_version", "adjudication_version", "gate_version",
        "source_sample_version", "source_seed", "source_logical_sha256", "source_run_id",
        "source_packet_binding_sha256", "source_visual_inventory_sha256", "status",
        "sticky_stop", "sample_count", "sheet_count", "asset_occurrence_count",
        "unique_asset_count", "inherited_logical_http_fetches",
        "inherited_wire_http_requests", "incremental_logical_http_fetches",
        "incremental_wire_http_requests", "incremental_retries", "incremental_redirects",
        "incremental_renderer_substitutions", "incremental_cache_writes", "r1_status",
        "r2_status", "adjudication_status", "gate_status", "created_at", "completed_at",
        "record_sha256",
    ),
    "input_pins": (
        "run_id", "role", "path", "size_bytes", "sha256", "usage", "record_sha256",
    ),
    "instrument_contract": (
        "run_id", "contract_json", "contract_sha256", "protocol_path", "protocol_sha256",
        "order_path", "order_sha256", "frozen_at", "record_sha256",
    ),
    "source_samples": (
        "run_id", "sample_rank", "sample_id", "seed_rank_sha256",
        "control_commitment_sha256", "snapshot_sha256", "source_pair_record_sha256",
        "record_sha256",
    ),
    "source_sheets": (
        "run_id", "lane", "display_order", "opaque_review_id", "sample_id", "sheet_path",
        "sheet_sha256", "sheet_bytes", "source_record_sha256", "record_sha256",
    ),
    "source_assets": (
        "run_id", "sample_id", "side", "asset_order", "asset_key", "source",
        "source_building_id", "source_asset_id", "selection_reason", "source_record_sha256",
        "record_sha256",
    ),
    "source_fetch_assets": (
        "run_id", "asset_key", "source", "source_asset_id", "expected_url",
        "expected_raw_sha256", "expected_pixel_sha256", "status", "cache_role", "cache_path",
        "cache_file_sha256", "actual_final_url", "actual_raw_sha256", "actual_pixel_sha256",
        "http_status", "response_bytes", "request_count", "error_kind", "error_message",
        "source_record_sha256", "record_sha256",
    ),
    "source_fetch_attempts": (
        "run_id", "asset_key", "attempt_no", "status", "request_count",
        "redirect_chain_json", "started_at", "completed_at", "error_kind",
        "source_record_sha256", "record_sha256",
    ),
    "source_cache_pins": (
        "run_id", "role", "path", "transform_version", "access_mode", "file_count_before",
        "total_bytes_before", "tree_sha256_before", "file_count_after", "total_bytes_after",
        "tree_sha256_after", "source_record_sha256", "record_sha256",
    ),
    "quota_observations": (
        "run_id", "observation_no", "phase", "account_index", "observed_at", "source",
        "remaining_percent", "start_allowed", "stop_required", "limitation", "record_sha256",
    ),
    "gate_contract": (
        "run_id", "gate_json", "gate_sha256", "d_min_same", "d_n", "p1_min_non_same",
        "p1_n", "insufficient_min_exact", "insufficient_n", "related_min_exact", "related_n",
        "declared_at", "record_sha256",
    ),
    "artifact_paths": ("run_id", "role", "path", "stage", "record_sha256"),
    "artifact_bindings": (
        "run_id", "role", "path", "size_bytes", "sha256", "stage", "record_sha256",
    ),
    "json_payloads": ("run_id", "role", "payload_json", "record_sha256"),
    "artifacts": (
        "run_id", "role", "path", "stage", "size_bytes", "sha256", "payload_hex",
        "created_at", "record_sha256",
    ),
    "review_observations": (
        "run_id", "lane", "display_order", "opaque_review_id", "sample_id", "image", "name",
        "firm", "place", "year", "scope", "basis", "reviewer", "reviewed_at",
        "observation_sha256", "record_sha256",
    ),
    "review_locks": (
        "run_id", "lane", "reviewer", "row_count", "completed_form_path",
        "completed_form_sha256", "locked_output_path", "locked_output_sha256",
        "ordered_ledger_sha256", "locked_at", "record_sha256",
    ),
    "review_lock_payloads": ("run_id", "lane", "payload_json", "record_sha256"),
    "adjudication_items": (
        "run_id", "display_order", "opaque_adjudication_id", "sample_id", "sheet_path",
        "sheet_sha256", "r1_observation_sha256", "r2_observation_sha256", "record_sha256",
    ),
    "adjudicated_observations": (
        "run_id", "opaque_adjudication_id", "sample_id", "image", "name", "firm", "place",
        "year", "scope", "basis", "derived_label", "derivation_rule",
        "axis_adjustments_json", "adjudicator", "adjudicated_at", "observation_sha256",
        "record_sha256",
    ),
    "adjudication_lock": (
        "run_id", "adjudicator", "row_count", "completed_form_path", "completed_form_sha256",
        "locked_output_path", "locked_output_sha256", "ordered_ledger_sha256", "locked_at",
        "record_sha256",
    ),
    "adjudication_lock_payload": ("run_id", "payload_json", "record_sha256"),
    "control_reveals": (
        "run_id", "sample_id", "candidate_id", "group_code", "n100_sample_id",
        "n100_gold_label", "v1_probe_label", "v2_derived_label", "derivation_rule_no",
        "commitment_sha256", "revealed_at", "reveal_payload_sha256", "record_sha256",
    ),
    "gate_scores": (
        "run_id", "predicate", "actual", "denominator", "threshold", "passed",
        "record_sha256",
    ),
    "state_events": (
        "run_id", "event_no", "from_status", "to_status", "detail_json", "occurred_at",
        "record_sha256",
    ),
    "terminal": (
        "run_id", "status", "report_path", "report_sha256", "manifest_path",
        "manifest_sha256", "report_json", "manifest_json", "completed_at", "record_sha256",
    ),
}
REQUIRED_TABLES = frozenset(EXPECTED_COLUMNS)

INTEGER_COLUMNS = frozenset(
    {
        "schema_version", "sticky_stop", "sample_count", "sheet_count",
        "asset_occurrence_count", "unique_asset_count", "inherited_logical_http_fetches",
        "inherited_wire_http_requests", "incremental_logical_http_fetches",
        "incremental_wire_http_requests", "incremental_retries", "incremental_redirects",
        "incremental_renderer_substitutions", "incremental_cache_writes", "sample_rank",
        "display_order", "sheet_bytes", "asset_order", "http_status", "response_bytes",
        "request_count", "attempt_no", "file_count_before", "total_bytes_before",
        "file_count_after", "total_bytes_after", "observation_no", "account_index",
        "start_allowed", "stop_required", "d_min_same", "d_n", "p1_min_non_same",
        "p1_n", "insufficient_min_exact", "insufficient_n", "related_min_exact",
        "related_n", "size_bytes", "row_count", "derivation_rule", "derivation_rule_no",
        "actual", "denominator", "threshold", "passed", "event_no",
    }
)
EXPECTED_PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "runs": ("run_id",), "input_pins": ("run_id", "role"),
    "instrument_contract": ("run_id",), "source_samples": ("run_id", "sample_rank"),
    "source_sheets": ("run_id", "lane", "opaque_review_id"),
    "source_assets": ("run_id", "sample_id", "side", "asset_order"),
    "source_fetch_assets": ("run_id", "asset_key"),
    "source_fetch_attempts": ("run_id", "asset_key", "attempt_no"),
    "source_cache_pins": ("run_id", "role"),
    "quota_observations": ("run_id", "observation_no"), "gate_contract": ("run_id",),
    "artifact_paths": ("run_id", "role"), "artifact_bindings": ("run_id", "role"),
    "json_payloads": ("run_id", "role"), "artifacts": ("run_id", "role"),
    "review_observations": ("run_id", "lane", "opaque_review_id"),
    "review_locks": ("run_id", "lane"), "review_lock_payloads": ("run_id", "lane"),
    "adjudication_items": ("run_id", "opaque_adjudication_id"),
    "adjudicated_observations": ("run_id", "opaque_adjudication_id"),
    "adjudication_lock": ("run_id",), "adjudication_lock_payload": ("run_id",),
    "control_reveals": ("run_id", "sample_id"),
    "gate_scores": ("run_id", "predicate"), "state_events": ("run_id", "event_no"),
    "terminal": ("run_id",),
}

FINAL_ARTIFACT_ROLES = frozenset(
    {
        "sample_manifest", "fetch_manifest", "fetch_report", "r1_form", "r2_form",
        "r1_completed", "r1_locked", "r2_completed", "r2_locked",
        "adjudication_packet", "adjudication_form", "adjudication_completed",
        "adjudication_locked", "final_report", "final_manifest",
    }
)
FINAL_JSON_PAYLOAD_ROLES = frozenset(
    {
        "sample_manifest", "fetch_manifest", "fetch_report", "r1_form", "r2_form",
        "adjudication_packet", "adjudication_form",
    }
)

HIDDEN_KEYS = frozenset(
    {
        "candidate_id",
        "real_candidate_id",
        "group",
        "group_code",
        "stratum",
        "cohort",
        "n100_sample_id",
        "n100_gold_label",
        "gold",
        "gold_label",
        "final_gold_label",
        "frozen_gold_label",
        "v1_probe_label",
        "wp26_v1_label",
        "final_probe_label",
        "wp26_probe_label",
        "legacy_probe_label",
        "probe_label",
        "proposal",
        "score",
        "policy_flags",
        "flags",
        "sample_rank",
        "seed_rank_sha256",
        "three_way_comparison",
    }
) | FORBIDDEN_INPUT_KEYS
HIDDEN_VALUE_TOKENS = frozenset(
    {
        "P1",
        "P2",
        "P3",
        "merge_candidate",
        "related_but_separate",
        "fanout_review_required",
        "phash_veto_review_required",
        "anchor_missing",
    }
)


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    expected: Any
    actual: Any


@dataclass(frozen=True)
class ValidationResult:
    passed: bool
    checks: tuple[Check, ...]
    metrics: Mapping[str, Any]
    report_path: Path | None

    @property
    def failed_check_names(self) -> tuple[str, ...]:
        return tuple(check.name for check in self.checks if not check.passed)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _record_sha(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _resolve(root: Path, value: str | Path) -> Path:
    path = Path(value)
    return (path if path.is_absolute() else root / path).resolve()


def _open_readonly(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _rows(
    connection: sqlite3.Connection,
    query: str,
    parameters: Sequence[Any] = (),
) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(query, tuple(parameters))]


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")'))


def _json_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    return json.loads(str(value))


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _iso(value: Any) -> datetime:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("timezone-aware timestamp required")
    return parsed


def _check(checks: list[Check], name: str, expected: Any, actual: Any) -> None:
    checks.append(Check(name=name, passed=actual == expected, expected=expected, actual=actual))


def observation_failures(
    value: Mapping[str, Any],
    *,
    exact_keys: Iterable[str] = OBSERVATION_KEYS,
) -> list[str]:
    """Return fail-closed violations for one seven-field observation."""

    expected_keys = frozenset(exact_keys)
    actual_keys = frozenset(str(key) for key in value)
    failures: list[str] = []
    if actual_keys != expected_keys:
        missing = sorted(expected_keys - actual_keys)
        extra = sorted(actual_keys - expected_keys)
        if missing:
            failures.append("missing:" + ",".join(missing))
        if extra:
            failures.append("extra:" + ",".join(extra))
    for axis, allowed in CONTROLLED_VALUES.items():
        candidate = value.get(axis)
        if not isinstance(candidate, str) or candidate not in allowed:
            failures.append(f"{axis}:controlled_value")
    basis = value.get("basis")
    if not isinstance(basis, str) or not basis.strip():
        failures.append("basis:non_empty")
    elif basis != basis.strip():
        failures.append("basis:trimmed")
    return sorted(set(failures))


def form_item_failures(value: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    keys = frozenset(str(key) for key in value)
    if keys != FORM_ITEM_KEYS:
        missing = sorted(FORM_ITEM_KEYS - keys)
        extra = sorted(keys - FORM_ITEM_KEYS)
        if missing:
            failures.append("missing:" + ",".join(missing))
        if extra:
            failures.append("extra:" + ",".join(extra))
    for forbidden in sorted(keys & FORBIDDEN_INPUT_KEYS):
        failures.append("forbidden:" + forbidden)
    observation = {key: value.get(key) for key in OBSERVATION_KEYS}
    failures.extend(observation_failures(observation))
    if not isinstance(value.get("display_order"), int) or int(value["display_order"]) < 1:
        failures.append("display_order")
    if not isinstance(value.get("opaque_review_id"), str) or not value["opaque_review_id"]:
        failures.append("opaque_review_id")
    if not isinstance(value.get("sheet_path"), str) or not value["sheet_path"]:
        failures.append("sheet_path")
    if not _is_sha256(value.get("sheet_sha256")):
        failures.append("sheet_sha256")
    return sorted(set(failures))


def derive_label(value: Mapping[str, Any]) -> tuple[str, int]:
    """Apply the frozen first-match derivation rules to a valid observation."""

    failures = observation_failures({key: value.get(key) for key in OBSERVATION_KEYS})
    if failures:
        raise ValueError("invalid observation: " + ", ".join(failures))
    image = str(value["image"])
    scope = str(value["scope"])
    if image == "inconclusive":
        return "insufficient", 1
    if image == "different_objects" or scope == "unrelated":
        return "different", 2
    if scope in {"component_vs_umbrella", "phase_or_extension"}:
        return "related", 3
    if image == "same_object" and scope == "same":
        return "same", 4
    return "insufficient", 5


def derivation_truth_table() -> dict[tuple[str, str], tuple[str, int]]:
    """Return all 15 image-by-scope outcomes from the frozen ordered rules."""

    base = {
        "name": "unclear",
        "firm": "unknown",
        "place": "unclear",
        "year": "unclear",
        "basis": "truth-table fixture",
    }
    return {
        (image, scope): derive_label({**base, "image": image, "scope": scope})
        for image in sorted(CONTROLLED_VALUES["image"])
        for scope in sorted(CONTROLLED_VALUES["scope"])
    }


def _expected_instrument_contract() -> dict[str, Any]:
    """Restate Stage A without consulting the producer implementation."""

    return {
        "instrument_version": INSTRUMENT_VERSION,
        "observation_keys": list(OBSERVATION_KEYS),
        "controlled_values": {
            key: list(CONTROLLED_VALUE_ORDER[key]) for key in OBSERVATION_KEYS[:-1]
        },
        "basis": {"type": "trimmed_non_empty_text", "required": True},
        "forbidden_input_fields": sorted(INSTRUMENT_FORBIDDEN_INPUT_FIELDS),
        "instructions": {
            "image_inconclusive": (
                "Use image=inconclusive for drawings/diagrams only, publication pages/covers, "
                "or no image permitting comparison of building form; inability to compare is normal."
            ),
            "matching_photographs": (
                "Matching photographs prove a shared depicted object, not equal project scope."
            ),
            "umbrella_scope": (
                "Use scope=component_vs_umbrella when one name denotes a village, campus, "
                "complex, masterplan, park, festival, or other umbrella and the other one component."
            ),
        },
        "derivation_rules": [
            {"rule": 1, "when": "image=inconclusive", "label": "insufficient"},
            {
                "rule": 2,
                "when": "image=different_objects OR scope=unrelated",
                "label": "different",
            },
            {
                "rule": 3,
                "when": "scope IN (component_vs_umbrella,phase_or_extension)",
                "label": "related",
            },
            {
                "rule": 4,
                "when": "image=same_object AND scope=same",
                "label": "same",
            },
            {"rule": 5, "when": "otherwise", "label": "insufficient"},
        ],
        "source": {
            "run_id": SOURCE_RUN_ID,
            "sample_version": SOURCE_SAMPLE_VERSION,
            "seed": SOURCE_SEED,
            "logical_sha256": SOURCE_LOGICAL_SHA256,
            "packet_binding_sha256": SOURCE_PACKET_SHA256,
            "visual_inventory_sha256": SOURCE_VISUAL_INVENTORY_SHA256,
            "rows": TOTAL_SAMPLE,
            "sheets": SOURCE_SHEET_COUNT,
            "asset_occurrences": SOURCE_ASSET_COUNT,
        },
        "limits": {
            "max_assets_per_side": MAX_ASSETS_PER_SIDE,
            "max_asset_occurrences": HTTP_CAP,
            "incremental_http": 0,
            "incremental_wire": 0,
            "incremental_retries": 0,
            "incremental_redirects": 0,
            "incremental_renderer_substitutions": 0,
            "incremental_cache_writes": 0,
        },
    }


def _expected_axis_instructions() -> dict[str, Any]:
    return {
        "version": "vnext-identity-instrument-v2-axis-guidance-v1",
        "actor_contract": (
            "Record or adjudicate only the six controlled observations and mandatory basis; "
            "do not choose a label."
        ),
        "observation_keys": list(OBSERVATION_KEYS),
        "controlled_values": {
            key: list(CONTROLLED_VALUE_ORDER[key]) for key in OBSERVATION_KEYS[:-1]
        },
        "basis": {"required": True, "format": "trimmed_non_empty_text"},
        "image_inconclusive": {
            "value": "inconclusive",
            "use_when": (
                "One side has only drawings or diagrams, a publication page or cover, or no "
                "image permitting comparison of the building form."
            ),
            "comparison_unavailable_is_normal": True,
        },
        "scope_guidance": {
            "matching_photographs": (
                "Matching photographs establish a shared depicted object, not equal project scope."
            ),
            "umbrella": (
                "When one name denotes a village, campus, complex, masterplan, park, festival, "
                "or another umbrella and the other denotes one constituent object, use "
                "scope=component_vs_umbrella."
            ),
        },
    }


def _expected_gate_contract() -> dict[str, Any]:
    return {
        "gate_version": GATE_VERSION,
        "predicates": [
            {
                "code": "d_same",
                "denominator": 15,
                "threshold": 13,
                "condition": "group=D AND v2_derived_label=same",
            },
            {
                "code": "p1_non_same",
                "denominator": 3,
                "threshold": 2,
                "condition": "group=P1 AND v2_derived_label!=same",
            },
            {
                "code": "gold_insufficient_to_insufficient",
                "denominator": 7,
                "threshold": 4,
                "condition": (
                    "n100_gold_label=insufficient AND v2_derived_label=insufficient"
                ),
            },
            {
                "code": "gold_related_to_related",
                "denominator": 2,
                "threshold": 1,
                "condition": "n100_gold_label=related AND v2_derived_label=related",
            },
        ],
        "combiner": "all",
    }


def gate_from_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Independently replay the four immutable WP2.7 predicates."""

    groups = [str(row.get("group", row.get("group_code", ""))) for row in rows]
    gold = [str(row.get("gold", row.get("n100_gold_label", ""))) for row in rows]
    labels = [str(row.get("v2_label", row.get("final_v2_label", ""))) for row in rows]
    if len(rows) != TOTAL_SAMPLE or Counter(groups) != GROUP_COUNTS:
        raise ValueError("gate rows do not contain the frozen 24-row group composition")
    if Counter(gold) != GOLD_COUNTS:
        raise ValueError("gate rows do not contain the frozen N100 gold composition")
    if any(label not in DERIVED_LABELS for label in labels):
        raise ValueError("gate rows contain a non-vocabulary derived label")
    d_same = sum(
        str(row.get("group", row.get("group_code", ""))) == "D"
        and str(row.get("v2_label", row.get("final_v2_label", ""))) == "same"
        for row in rows
    )
    p1_non_same = sum(
        str(row.get("group", row.get("group_code", ""))) == "P1"
        and str(row.get("v2_label", row.get("final_v2_label", ""))) != "same"
        for row in rows
    )
    insufficient_correct = sum(
        str(row.get("gold", row.get("n100_gold_label", ""))) == "insufficient"
        and str(row.get("v2_label", row.get("final_v2_label", ""))) == "insufficient"
        for row in rows
    )
    related_correct = sum(
        str(row.get("gold", row.get("n100_gold_label", ""))) == "related"
        and str(row.get("v2_label", row.get("final_v2_label", ""))) == "related"
        for row in rows
    )
    counts = {
        "d_same": d_same,
        "p1_non_same": p1_non_same,
        "gold_insufficient_to_insufficient": insufficient_correct,
        "gold_related_to_related": related_correct,
    }
    predicates = {
        name: {
            "actual": counts[name],
            "threshold": threshold,
            "denominator": denominator,
            "passed": counts[name] >= threshold,
        }
        for name, (threshold, denominator) in GATE_THRESHOLDS.items()
    }
    return {
        "counts": counts,
        "predicates": predicates,
        "status": "pass" if all(item["passed"] for item in predicates.values()) else "fail",
    }


def _walk_json(value: Any, path: str = "$") -> Iterable[tuple[str, str, Any]]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            location = f"{path}.{key}"
            yield location, str(key), child
            yield from _walk_json(child, location)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_json(child, f"{path}[{index}]")


def blind_json_leaks(
    value: Any,
    *,
    candidate_ids: Iterable[str] = (),
    n100_sample_ids: Iterable[str] = (),
) -> list[str]:
    failures: list[str] = []
    serialized = _canonical_json(value)
    for location, key, child in _walk_json(value):
        folded = key.casefold()
        if folded in HIDDEN_KEYS:
            failures.append(location)
        if isinstance(child, str) and child in HIDDEN_VALUE_TOKENS:
            failures.append(f"{location}:forbidden_value:{child}")
    for token in (*candidate_ids, *n100_sample_ids):
        if token and token in serialized:
            failures.append("hidden_identifier_value")
            break
    return sorted(set(failures))


def _png_structure_failures(path: Path) -> list[str]:
    """Accept only metadata-free RGB PNGs with valid CRC and scanline shape."""

    try:
        raw = path.read_bytes()
    except OSError:
        return ["unreadable"]
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return ["signature"]
    position = 8
    chunks: list[tuple[bytes, bytes]] = []
    failures: list[str] = []
    while position < len(raw):
        if position + 12 > len(raw):
            failures.append("truncated_header")
            break
        length = struct.unpack(">I", raw[position : position + 4])[0]
        end = position + 12 + length
        if end > len(raw):
            failures.append("truncated_chunk")
            break
        kind = raw[position + 4 : position + 8]
        data = raw[position + 8 : position + 8 + length]
        expected_crc = struct.unpack(">I", raw[position + 8 + length : end])[0]
        if (binascii.crc32(kind + data) & 0xFFFFFFFF) != expected_crc:
            failures.append("crc:" + kind.decode("ascii", errors="replace"))
        chunks.append((kind, data))
        position = end
        if kind == b"IEND":
            break
    if position != len(raw):
        failures.append("trailing_bytes")
    kinds = [kind for kind, _ in chunks]
    if not kinds or kinds[0] != b"IHDR" or kinds[-1:] != [b"IEND"]:
        failures.append("chunk_order")
    for kind in kinds:
        if kind not in {b"IHDR", b"IDAT", b"IEND"}:
            failures.append("unexpected_chunk:" + kind.decode("ascii", errors="replace"))
    if len([kind for kind in kinds if kind == b"IHDR"]) != 1:
        failures.append("ihdr_count")
    if len([kind for kind in kinds if kind == b"IEND"]) != 1:
        failures.append("iend_count")
    ihdr = next((data for kind, data in chunks if kind == b"IHDR"), None)
    if ihdr is None or len(ihdr) != 13:
        failures.append("ihdr")
        return sorted(set(failures))
    width, height, depth, color, compression, filtering, interlace = struct.unpack(
        ">IIBBBBB", ihdr
    )
    if (depth, color, compression, filtering, interlace) != (8, 2, 0, 0, 0):
        failures.append("rgb_contract")
    compressed = b"".join(data for kind, data in chunks if kind == b"IDAT")
    try:
        decoded = zlib.decompress(compressed)
    except zlib.error:
        failures.append("idat_zlib")
    else:
        expected = height * (1 + width * 3)
        if len(decoded) != expected:
            failures.append("scanline_shape")
        elif any(decoded[row * (1 + width * 3)] > 4 for row in range(height)):
            failures.append("filter_type")
    return sorted(set(failures))


def quota_ledger_failures(
    rows: Sequence[Mapping[str, Any]],
    *,
    expected_phases: Sequence[str] = QUOTA_PHASES,
) -> list[str]:
    """Validate ordered quota gates and the sticky-stop boundary."""

    failures: list[str] = []
    previous_time: datetime | None = None
    previous_phase = -1
    stopped = False
    seen: list[str] = []
    latest_phase_authorized = True
    latest_phase = ""
    for expected_no, row in enumerate(rows, 1):
        prefix = str(expected_no)
        if int(row.get("observation_no", -1)) != expected_no:
            failures.append(prefix + ":observation_order")
        phase = str(row.get("phase", ""))
        if phase not in QUOTA_PHASES:
            failures.append(prefix + ":phase")
            phase_index = previous_phase
        else:
            phase_index = QUOTA_PHASES.index(phase)
            if phase_index < previous_phase or phase_index > previous_phase + 1:
                failures.append(prefix + ":phase_order")
            if phase_index != previous_phase:
                if latest_phase and not latest_phase_authorized:
                    failures.append(latest_phase + ":wave_not_authorized")
                seen.append(phase)
                previous_phase = phase_index
            latest_phase = phase
        try:
            observed = _iso(row.get("observed_at"))
            if previous_time is not None and observed < previous_time:
                failures.append(prefix + ":timestamp_order")
            previous_time = observed
        except (TypeError, ValueError):
            failures.append(prefix + ":timestamp")
        try:
            remaining = float(row.get("remaining_percent"))
            if not 0 <= remaining <= 100:
                raise ValueError
        except (TypeError, ValueError):
            remaining = -1.0
            failures.append(prefix + ":remaining_percent")
        expected_start = int(remaining > 30)
        expected_stop = int(remaining <= 20)
        if row.get("start_allowed") not in (0, 1) or int(row["start_allowed"]) != expected_start:
            failures.append(prefix + ":start_gate")
        if row.get("stop_required") not in (0, 1) or int(row["stop_required"]) != expected_stop:
            failures.append(prefix + ":stop_gate")
        latest_phase_authorized = bool(expected_start)
        if stopped:
            failures.append(prefix + ":activity_after_stop")
        stopped = stopped or bool(expected_stop)
        source = row.get("source")
        if not isinstance(source, str) or not source.strip() or source != source.strip():
            failures.append(prefix + ":source")
        limitation = row.get("limitation")
        if not isinstance(limitation, str) or limitation != limitation.strip():
            failures.append(prefix + ":limitation")
    if latest_phase and not latest_phase_authorized:
        failures.append(latest_phase + ":wave_not_authorized")
    if tuple(seen) != tuple(expected_phases):
        failures.append("phase_set")
    return sorted(set(failures))


def _sqlite_sidecars(path: Path) -> list[str]:
    return [
        candidate.name
        for suffix in ("-wal", "-shm", "-journal")
        if (candidate := Path(str(path) + suffix)).exists()
    ]


def _fetch_correction_failures(root: Path) -> list[str]:
    """Independently open WP2.6's no-clobber 884-to-96 correction overlay."""

    failures: list[str] = []
    try:
        correction = _load_json(_resolve(root, DEFAULT_FETCH_CORRECTION))
        fetch_path = _resolve(root, DEFAULT_SOURCE_FETCH_MANIFEST)
        fetch = _load_json(fetch_path)
    except (OSError, json.JSONDecodeError):
        return ["unreadable"]
    expected_scalars = {
        "status": "CORRECTION",
        "correction_version": "vnext-identity-instrument-probe-fetch-cap-correction-v1",
        "run_id": SOURCE_RUN_ID,
        "scope": "fetch_manifest.hard_caps",
    }
    for key, value in expected_scalars.items():
        if correction.get(key) != value:
            failures.append(key)
    expected_legacy_caps = {
        "asset_occurrences": 884,
        "logical_http_fetches": 884,
        "redirects": 0,
        "retries": 0,
        "wire_http_requests": 884,
    }
    original = correction.get("original_fetch_manifest")
    expected_original = {
        "path": DEFAULT_SOURCE_FETCH_MANIFEST.as_posix(),
        "size_bytes": fetch_path.stat().st_size,
        "sha256": _sha256_file(fetch_path),
        "recorded_hard_caps": expected_legacy_caps,
    }
    if original != expected_original:
        failures.append("original_fetch_manifest")
    expected_counts = {
        "asset_occurrences": 93,
        "logical_http_fetches": 17,
        "redirects": 0,
        "retries": 0,
        "wire_http_requests": 17,
    }
    expected_caps = {
        "asset_occurrences": 96,
        "logical_http_fetches": 96,
        "redirects": 0,
        "retries": 0,
        "wire_http_requests": 96,
    }
    if correction.get("corrected_contract") != {
        "actual_counts": expected_counts,
        "hard_caps": expected_caps,
        "within_hard_caps": True,
    }:
        failures.append("corrected_contract")
    corrected = dict(fetch)
    corrected["hard_caps"] = expected_caps
    if correction.get("corrected_projection") != {
        "canonicalization": "canonical-json-v1",
        "operation": "replace-hard_caps-only",
        "sha256": _record_sha(corrected),
    }:
        failures.append("corrected_projection")
    terminal = correction.get("terminal_bindings")
    expected_terminal_paths = {
        "database": DEFAULT_SOURCE_DB,
        "report": DEFAULT_SOURCE_REPORT,
        "manifest": DEFAULT_SOURCE_MANIFEST,
    }
    if not isinstance(terminal, Mapping):
        failures.append("terminal_bindings")
    else:
        for role, relative in expected_terminal_paths.items():
            path = _resolve(root, relative)
            expected = {
                "path": relative.as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
            if terminal.get(role) != expected:
                failures.append("terminal_bindings:" + role)
    producer = correction.get("producer_bindings")
    current_path = _resolve(root, DEFAULT_SOURCE_PRODUCER)
    if not isinstance(producer, Mapping):
        failures.append("producer_bindings")
    else:
        if producer.get("current") != {
            "path": DEFAULT_SOURCE_PRODUCER.as_posix(),
            "size_bytes": current_path.stat().st_size,
            "sha256": _sha256_file(current_path),
        }:
            failures.append("producer_current")
        if producer.get("runtime") != {
            "path": DEFAULT_SOURCE_PRODUCER.as_posix(),
            "size_bytes": 109_175,
            "sha256": "8e12aa23980ad17c1f7c724f8a99eb47f4e2844eee660a2d374a3436c032285e",
        }:
            failures.append("producer_runtime")
    return sorted(set(failures))


def _expected_inherited_fetch_manifest() -> dict[str, Any]:
    """Return provenance required in the terminal manifest, without reveal data."""

    native_caps = {
        "asset_occurrences": HTTP_CAP,
        "logical_http_fetches": HTTP_CAP,
        "wire_http_requests": HTTP_CAP,
        "redirects": 0,
        "retries": 0,
    }
    return {
        "asset_occurrences": SOURCE_ASSET_COUNT,
        "unique_assets": SOURCE_ASSET_COUNT,
        "status_counts": {"cache_hit": 76, "failed": 17},
        "logical_http_fetches": 17,
        "wire_http_requests": 17,
        "redirects": 0,
        "retries": 0,
        "source_correction": {
            "path": DEFAULT_FETCH_CORRECTION.as_posix(),
            "size_bytes": SOURCE_FILE_SPECS[DEFAULT_FETCH_CORRECTION][0],
            "sha256": SOURCE_FILE_SPECS[DEFAULT_FETCH_CORRECTION][1],
            "corrected_projection_sha256": (
                "439426d0706c7eac1b922af9fb8f042fb7bf3cb514cda809363daef7c0fc72b3"
            ),
            "native_hard_caps": native_caps,
        },
    }


def _source_snapshot(root: Path, *, verify_full_hashes: bool) -> dict[str, Any]:
    """Read and cross-check the frozen WP2.6 terminal source independently."""

    if verify_full_hashes:
        for relative, expected in SOURCE_FILE_SPECS.items():
            path = _resolve(root, relative)
            actual = (path.stat().st_size, _sha256_file(path))
            if actual != expected:
                raise ValueError(f"source pin mismatch: {relative.as_posix()}")
    source_db = _resolve(root, DEFAULT_SOURCE_DB)
    source_report = _load_json(_resolve(root, DEFAULT_SOURCE_REPORT))
    connection = _open_readonly(source_db)
    try:
        run_rows = _rows(connection, "SELECT * FROM runs")
        if len(run_rows) != 1:
            raise ValueError("WP2.6 source must contain exactly one run")
        run = run_rows[0]
        if run.get("run_id") != SOURCE_RUN_ID:
            raise ValueError("WP2.6 run id drift")
        if run.get("sample_version") != SOURCE_SAMPLE_VERSION:
            raise ValueError("WP2.6 sample version drift")
        if run.get("seed") != SOURCE_SEED:
            raise ValueError("WP2.6 seed drift")
        if run.get("sample_logical_sha256") != SOURCE_LOGICAL_SHA256:
            raise ValueError("WP2.6 sample logical SHA drift")
        samples = _rows(connection, "SELECT * FROM sample_pairs ORDER BY sample_rank")
        snapshots = _rows(connection, "SELECT * FROM candidate_snapshots ORDER BY sample_id")
        commitments = _rows(connection, "SELECT * FROM control_commitments ORDER BY sample_id")
        assets = _rows(
            connection,
            "SELECT * FROM sample_assets ORDER BY sample_id,side,asset_order",
        )
        sheets = _rows(
            connection,
            "SELECT * FROM review_items ORDER BY lane,display_order",
        )
        reveals = _rows(connection, "SELECT * FROM probe_reveals ORDER BY sample_id")
        fetch_assets = _rows(connection, "SELECT * FROM fetch_assets ORDER BY asset_key")
        fetch_attempts = _rows(
            connection,
            "SELECT * FROM fetch_attempts ORDER BY asset_key,attempt_no",
        )
        cache_pins = _rows(connection, "SELECT * FROM cache_pins ORDER BY role")
    finally:
        connection.close()
    if len(samples) != TOTAL_SAMPLE or [row["sample_rank"] for row in samples] != list(
        range(1, TOTAL_SAMPLE + 1)
    ):
        raise ValueError("WP2.6 sample rows/rank drift")
    if len(snapshots) != TOTAL_SAMPLE or len(commitments) != TOTAL_SAMPLE:
        raise ValueError("WP2.6 snapshot/commitment rows drift")
    if len(assets) != SOURCE_ASSET_COUNT:
        raise ValueError("WP2.6 source asset count drift")
    if len(sheets) != SOURCE_SHEET_COUNT:
        raise ValueError("WP2.6 source sheet count drift")
    if len(reveals) != TOTAL_SAMPLE:
        raise ValueError("WP2.6 reveal count drift")
    if Counter(str(row["status"]) for row in fetch_assets) != {
        "cache_hit": 76,
        "failed": 17,
    }:
        raise ValueError("WP2.6 fetch status drift")
    if (
        len(fetch_assets) != SOURCE_ASSET_COUNT
        or len(fetch_attempts) != 17
        or sum(int(row["request_count"]) for row in fetch_attempts) != 17
    ):
        raise ValueError("WP2.6 fetch/attempt count drift")
    if len(cache_pins) != 5:
        raise ValueError("WP2.6 cache pin count drift")
    packet_rows = [
        {
            key: row[key]
            for key in (
                "lane",
                "display_order",
                "opaque_review_id",
                "sample_id",
                "sheet_path",
                "sheet_sha256",
                "sheet_bytes",
            )
        }
        for row in sheets
    ]
    if _record_sha(packet_rows) != SOURCE_PACKET_SHA256:
        raise ValueError("WP2.6 authoritative packet binding drift")
    inventory_rows = [
        {
            "lane": row["lane"],
            "display_order": int(row["display_order"]),
            "path": row["sheet_path"],
            "sha256": row["sheet_sha256"],
            "bytes": int(row["sheet_bytes"]),
        }
        for row in sheets
    ]
    if _record_sha(inventory_rows) != SOURCE_VISUAL_INVENTORY_SHA256:
        raise ValueError("WP2.6 visual inventory binding drift")
    if sum(int(row["sheet_bytes"]) for row in sheets) != 24_517_369:
        raise ValueError("WP2.6 visual inventory byte count drift")
    if _fetch_correction_failures(root):
        raise ValueError("WP2.6 fetch correction binding drift")
    if any(
        count > MAX_ASSETS_PER_SIDE
        for count in Counter((row["sample_id"], row["side"]) for row in assets).values()
    ):
        raise ValueError("WP2.6 per-side asset cap drift")
    report_rows = source_report.get("gold_comparison")
    if not isinstance(report_rows, list) or len(report_rows) != TOTAL_SAMPLE:
        raise ValueError("WP2.6 report comparison drift")
    reveal_by_sample = {str(row["sample_id"]): row for row in reveals}
    report_by_sample = {str(row.get("sample_id")): row for row in report_rows}
    if set(reveal_by_sample) != set(report_by_sample):
        raise ValueError("WP2.6 report/DB sample set drift")
    for sample_id, reveal in reveal_by_sample.items():
        report = report_by_sample[sample_id]
        for report_key, db_key in (
            ("candidate_id", "candidate_id"),
            ("group", "group_code"),
            ("n100_sample_id", "n100_sample_id"),
            ("n100_gold_label", "n100_gold_label"),
            ("probe_label", "final_probe_label"),
            ("commitment_sha256", "commitment_sha256"),
        ):
            if report.get(report_key) != reveal.get(db_key):
                raise ValueError(f"WP2.6 report/DB mismatch: {sample_id}:{report_key}")
    if source_report.get("fetch") != {
        "asset_occurrences": 93,
        "hard_caps": {
            "asset_occurrences": 96,
            "logical_http_fetches": 96,
            "wire_http_requests": 96,
        },
        "logical_http_fetches": 17,
        "redirects": 0,
        "retries": 0,
        "status_counts": {"cache_hit": 76, "failed": 17},
        "unique_assets": 93,
        "wire_http_requests": 17,
    }:
        raise ValueError("WP2.6 report fetch contract drift")
    return {
        "run": run,
        "samples": samples,
        "snapshots": snapshots,
        "commitments": commitments,
        "assets": assets,
        "sheets": sheets,
        "reveals": reveals,
        "report_rows": report_rows,
        "fetch_assets": fetch_assets,
        "fetch_attempts": fetch_attempts,
        "cache_pins": cache_pins,
        "packet_rows": packet_rows,
        "inventory_rows": inventory_rows,
        "candidate_ids": {str(row["candidate_id"]) for row in reveals},
        "n100_sample_ids": {str(row["n100_sample_id"]) for row in reveals},
    }


def _validate_stage_a_contract_and_pins(
    root: Path,
    connection: sqlite3.Connection,
    checks: list[Check],
    *,
    verify_full_hashes: bool,
) -> None:
    failures: list[str] = []
    run_rows = _rows(connection, "SELECT * FROM runs")
    if len(run_rows) != 1:
        failures.append("runs:row_count")
        run: Mapping[str, Any] = {}
    else:
        run = run_rows[0]
    expected_run = {
        "schema_version": USER_VERSION,
        "runner_version": RUNNER_VERSION,
        "instrument_version": INSTRUMENT_VERSION,
        "sample_version": SAMPLE_VERSION,
        "review_version": REVIEW_VERSION,
        "adjudication_version": ADJUDICATION_VERSION,
        "gate_version": GATE_VERSION,
        "source_sample_version": SOURCE_SAMPLE_VERSION,
        "source_seed": SOURCE_SEED,
        "source_logical_sha256": SOURCE_LOGICAL_SHA256,
        "source_run_id": SOURCE_RUN_ID,
        "source_packet_binding_sha256": SOURCE_PACKET_SHA256,
        "source_visual_inventory_sha256": SOURCE_VISUAL_INVENTORY_SHA256,
        "sample_count": TOTAL_SAMPLE,
        "sheet_count": SOURCE_SHEET_COUNT,
        "asset_occurrence_count": SOURCE_ASSET_COUNT,
        "unique_asset_count": SOURCE_ASSET_COUNT,
        "inherited_logical_http_fetches": 17,
        "inherited_wire_http_requests": 17,
        "incremental_logical_http_fetches": 0,
        "incremental_wire_http_requests": 0,
        "incremental_retries": 0,
        "incremental_redirects": 0,
        "incremental_renderer_substitutions": 0,
        "incremental_cache_writes": 0,
        "sticky_stop": 0,
    }
    for key, expected in expected_run.items():
        if run.get(key) != expected:
            failures.append("runs:" + key)

    pin_rows = _rows(connection, "SELECT * FROM input_pins ORDER BY role")
    by_role = {str(row.get("role")): row for row in pin_rows}
    if set(by_role) != set(INPUT_PIN_SPECS) or len(pin_rows) != len(by_role):
        failures.append("input_pins:role_set")
    for role, (relative, size, digest, usage) in INPUT_PIN_SPECS.items():
        row = by_role.get(role)
        if row is None:
            continue
        expected_path = relative.as_posix()
        for key, expected in {
            "path": expected_path,
            "size_bytes": size,
            "sha256": digest,
            "usage": usage,
        }.items():
            if row.get(key) != expected:
                failures.append(f"input_pins:{role}:{key}")
        path = _resolve(root, expected_path)
        if not path.is_file():
            failures.append(f"input_pins:{role}:missing")
        elif verify_full_hashes and (
            path.stat().st_size != size or _sha256_file(path) != digest
        ):
            failures.append(f"input_pins:{role}:file_binding")

    contract_rows = _rows(connection, "SELECT * FROM instrument_contract")
    expected_contract = _expected_instrument_contract()
    expected_contract_json = _canonical_json(expected_contract)
    expected_contract_sha = _record_sha(expected_contract)
    if len(contract_rows) != 1:
        failures.append("instrument_contract:row_count")
    else:
        contract = contract_rows[0]
        expected_fields = {
            "contract_json": expected_contract_json,
            "contract_sha256": expected_contract_sha,
            "protocol_path": DEFAULT_PROTOCOL.as_posix(),
            "protocol_sha256": PROTOCOL_SPEC[1],
            "order_path": DEFAULT_ORDER.as_posix(),
            "order_sha256": ORDER_SPEC[1],
        }
        for key, expected in expected_fields.items():
            if contract.get(key) != expected:
                failures.append("instrument_contract:" + key)
        try:
            _iso(contract.get("frozen_at"))
        except (TypeError, ValueError):
            failures.append("instrument_contract:frozen_at")
    source_db_sha = SOURCE_FILE_SPECS[DEFAULT_SOURCE_DB][1]
    expected_run_id = "wp27-" + hashlib.sha256(
        f"{SAMPLE_VERSION}\0{expected_contract_sha}\0{source_db_sha}".encode("utf-8")
    ).hexdigest()[:24]
    if run.get("run_id") != expected_run_id:
        failures.append("runs:run_id")
    _check(checks, "stage_a_exact_instrument_contract_and_input_pins", [], sorted(set(failures)))


def _validate_source_pins(
    root: Path,
    connection: sqlite3.Connection,
    source: Mapping[str, Any],
    checks: list[Check],
    metrics: dict[str, Any],
) -> None:
    """Validate byte-for-byte sample, sheet, snapshot, commitment and asset reuse."""

    tables = _tables(connection)
    failures: list[str] = []
    source_samples = _rows(connection, "SELECT * FROM source_samples ORDER BY sample_rank")
    if len(source_samples) != TOTAL_SAMPLE:
        failures.append("sample_count")
    else:
        expected_samples = source["samples"]
        expected_snapshots = {row["sample_id"]: row for row in source["snapshots"]}
        expected_commitments = {row["sample_id"]: row for row in source["commitments"]}
        for actual, expected in zip(source_samples, expected_samples):
            sample_id = str(expected["sample_id"])
            for key in ("sample_rank", "sample_id", "seed_rank_sha256"):
                if actual.get(key) != expected.get(key):
                    failures.append(f"{sample_id}:{key}")
            commitment = expected_commitments[sample_id]
            if actual.get("control_commitment_sha256") != commitment.get(
                "gold_commitment_sha256"
            ):
                failures.append(f"{sample_id}:commitment")
            snapshot = expected_snapshots[sample_id]
            if actual.get("snapshot_sha256") != snapshot.get("snapshot_sha256"):
                failures.append(f"{sample_id}:snapshot_sha256")
            if actual.get("source_pair_record_sha256") != expected.get("record_sha256"):
                failures.append(f"{sample_id}:source_pair_record_sha256")
    sheet_rows = _rows(connection, "SELECT * FROM source_sheets ORDER BY lane,display_order")
    if len(sheet_rows) != SOURCE_SHEET_COUNT:
        failures.append("sheet_count")
    else:
        for actual, expected in zip(sheet_rows, source["sheets"]):
            for key in (
                "lane",
                "display_order",
                "opaque_review_id",
                "sample_id",
                "sheet_path",
                "sheet_sha256",
                "sheet_bytes",
            ):
                if key in actual and actual.get(key) != expected.get(key):
                    failures.append(f"sheet:{expected['lane']}:{expected['display_order']}:{key}")
            if actual.get("source_record_sha256") != expected.get("record_sha256"):
                failures.append(
                    f"sheet:{expected['lane']}:{expected['display_order']}:source_record_sha256"
                )
            path = _resolve(root, str(expected["sheet_path"]))
            if not path.is_file() or _sha256_file(path) != expected["sheet_sha256"]:
                failures.append(f"sheet:{expected['lane']}:{expected['display_order']}:file")
            elif _png_structure_failures(path):
                failures.append(f"sheet:{expected['lane']}:{expected['display_order']}:png")
    if "source_assets" not in tables:
        failures.append("source_assets:missing")
        asset_rows: list[dict[str, Any]] = []
    else:
        asset_rows = _rows(
            connection,
            "SELECT * FROM source_assets ORDER BY sample_id,side,asset_order",
        )
        if len(asset_rows) != SOURCE_ASSET_COUNT:
            failures.append("asset_count")
        else:
            compare_keys = (
                "sample_id",
                "side",
                "asset_order",
                "asset_key",
                "source",
                "source_building_id",
                "source_asset_id",
                "selection_reason",
            )
            for actual, expected in zip(asset_rows, source["assets"]):
                for key in compare_keys:
                    if actual.get(key) != expected.get(key):
                        failures.append(
                            f"asset:{expected['sample_id']}:{expected['side']}:"
                            f"{expected['asset_order']}:{key}"
                        )
                if actual.get("source_record_sha256") != expected.get("record_sha256"):
                    failures.append(
                        f"asset:{expected['sample_id']}:{expected['side']}:"
                        f"{expected['asset_order']}:source_record_sha256"
                    )
        per_side = Counter((row.get("sample_id"), row.get("side")) for row in asset_rows)
        if any(value > MAX_ASSETS_PER_SIDE for value in per_side.values()):
            failures.append("asset_per_side_cap")
    fetch_rows = _rows(
        connection,
        "SELECT * FROM source_fetch_assets ORDER BY asset_key",
    )
    fetch_keys = (
        "asset_key",
        "source",
        "source_asset_id",
        "expected_url",
        "expected_raw_sha256",
        "expected_pixel_sha256",
        "status",
        "cache_role",
        "cache_path",
        "actual_final_url",
        "actual_raw_sha256",
        "actual_pixel_sha256",
        "http_status",
        "response_bytes",
        "request_count",
        "error_kind",
        "error_message",
    )
    if len(fetch_rows) != SOURCE_ASSET_COUNT:
        failures.append("source_fetch_assets:count")
    else:
        for actual, expected in zip(fetch_rows, source["fetch_assets"]):
            asset_key = str(expected["asset_key"])
            for key in fetch_keys:
                if actual.get(key) != expected.get(key):
                    failures.append(f"source_fetch:{asset_key}:{key}")
            if actual.get("source_record_sha256") != expected.get("record_sha256"):
                failures.append(f"source_fetch:{asset_key}:source_record_sha256")
            cache_path = expected.get("cache_path")
            expected_cache_sha: str | None = None
            if cache_path:
                cache_file = _resolve(root, str(cache_path))
                if not cache_file.is_file():
                    failures.append(f"source_fetch:{asset_key}:cache_file")
                else:
                    expected_cache_sha = _sha256_file(cache_file)
            if actual.get("cache_file_sha256") != expected_cache_sha:
                failures.append(f"source_fetch:{asset_key}:cache_file_sha256")
    attempt_rows = _rows(
        connection,
        "SELECT * FROM source_fetch_attempts ORDER BY asset_key,attempt_no",
    )
    attempt_keys = (
        "asset_key",
        "attempt_no",
        "status",
        "request_count",
        "redirect_chain_json",
        "started_at",
        "completed_at",
        "error_kind",
    )
    if len(attempt_rows) != 17:
        failures.append("source_fetch_attempts:count")
    else:
        for actual, expected in zip(attempt_rows, source["fetch_attempts"]):
            key = f"{expected['asset_key']}:{expected['attempt_no']}"
            for field in attempt_keys:
                if actual.get(field) != expected.get(field):
                    failures.append(f"source_attempt:{key}:{field}")
            if actual.get("source_record_sha256") != expected.get("record_sha256"):
                failures.append(f"source_attempt:{key}:source_record_sha256")
    cache_rows = _rows(connection, "SELECT * FROM source_cache_pins ORDER BY role")
    cache_keys = (
        "role",
        "path",
        "transform_version",
        "access_mode",
        "file_count_before",
        "total_bytes_before",
        "tree_sha256_before",
        "file_count_after",
        "total_bytes_after",
        "tree_sha256_after",
    )
    if len(cache_rows) != len(source["cache_pins"]):
        failures.append("source_cache_pins:count")
    else:
        for actual, expected in zip(cache_rows, source["cache_pins"]):
            role = str(expected["role"])
            for field in cache_keys:
                if actual.get(field) != expected.get(field):
                    failures.append(f"source_cache:{role}:{field}")
            if actual.get("source_record_sha256") != expected.get("record_sha256"):
                failures.append(f"source_cache:{role}:source_record_sha256")
            # These are inherited WP2.6 provenance rows, not WP2.7 cache
            # operations. One historical role is correctly marked
            # ``write_only``; exact source-row replay above is authoritative.
            # WP2.7's zero-write guarantee is the unchanged snapshot itself.
            if (
                actual.get("file_count_before") != actual.get("file_count_after")
                or actual.get("total_bytes_before") != actual.get("total_bytes_after")
                or actual.get("tree_sha256_before") != actual.get("tree_sha256_after")
            ):
                failures.append(f"source_cache:{role}:stable_snapshot")
    packet_rows = [
        {
            key: row.get(key)
            for key in (
                "lane",
                "display_order",
                "opaque_review_id",
                "sample_id",
                "sheet_path",
                "sheet_sha256",
                "sheet_bytes",
            )
        }
        for row in sheet_rows
    ]
    if _record_sha(packet_rows) != SOURCE_PACKET_SHA256:
        failures.append("source_packet_binding_sha256")
    inventory_rows = [
        {
            "lane": row.get("lane"),
            "display_order": row.get("display_order"),
            "path": row.get("sheet_path"),
            "sha256": row.get("sheet_sha256"),
            "bytes": row.get("sheet_bytes"),
        }
        for row in sheet_rows
    ]
    if _record_sha(inventory_rows) != SOURCE_VISUAL_INVENTORY_SHA256:
        failures.append("source_visual_inventory_sha256")
    _check(checks, "wp26_exact_sample_snapshot_sheet_asset_reuse", [], sorted(set(failures)))
    metrics["source_reuse"] = {
        "samples": len(source_samples),
        "sheets": len(sheet_rows),
        "assets": len(asset_rows),
        "fetch_assets": len(fetch_rows),
        "fetch_attempts": len(attempt_rows),
        "cache_pins": len(cache_rows),
    }


def _artifact_map(
    root: Path,
    connection: sqlite3.Connection,
    *,
    verify_full_hashes: bool,
    phase: str,
) -> tuple[dict[str, Path], list[str]]:
    failures: list[str] = []
    result: dict[str, Path] = {}
    artifact_rows = _rows(connection, "SELECT * FROM artifacts ORDER BY role")
    path_rows = {
        str(row.get("role")): row
        for row in _rows(connection, "SELECT * FROM artifact_paths ORDER BY role")
    }
    binding_rows = {
        str(row.get("role")): row
        for row in _rows(connection, "SELECT * FROM artifact_bindings ORDER BY role")
    }
    payload_rows = {
        str(row.get("role")): row
        for row in _rows(connection, "SELECT * FROM json_payloads ORDER BY role")
    }
    artifact_roles = {str(row.get("role")) for row in artifact_rows}
    expected_roles = FINAL_ARTIFACT_ROLES if phase == "final" else frozenset({"sample_manifest"})
    expected_payload_roles = (
        FINAL_JSON_PAYLOAD_ROLES if phase == "final" else frozenset({"sample_manifest"})
    )
    if artifact_roles != expected_roles or len(artifact_rows) != len(artifact_roles):
        failures.append("artifacts:exact_role_set")
    if set(path_rows) != artifact_roles:
        failures.append("artifact_paths:role_set")
    if set(binding_rows) != artifact_roles:
        failures.append("artifact_bindings:role_set")
    resolved_paths: set[Path] = set()
    for row in artifact_rows:
        role = str(row.get("role", ""))
        path = _resolve(root, str(row.get("path", "")))
        if not role or role in result:
            failures.append("role:" + role)
            continue
        if "correction" in role.casefold():
            failures.append(role + ":v2_correction_overlay_forbidden")
        if path in resolved_paths:
            failures.append(role + ":path_alias")
        resolved_paths.add(path)
        result[role] = path
        path_row = path_rows.get(role)
        binding = binding_rows.get(role)
        for linked_name, linked in (("path", path_row), ("binding", binding)):
            if linked is None:
                continue
            for key in ("run_id", "role", "path", "stage"):
                if linked.get(key) != row.get(key):
                    failures.append(f"{role}:{linked_name}:{key}")
        if binding is not None:
            for key in ("size_bytes", "sha256"):
                if binding.get(key) != row.get(key):
                    failures.append(f"{role}:binding:{key}")
        try:
            raw = bytes.fromhex(str(row.get("payload_hex", "")))
        except ValueError:
            raw = b""
            failures.append(role + ":payload_hex")
        expected_size = int(row.get("size_bytes", -1))
        expected_sha = str(row.get("sha256", ""))
        if len(raw) != expected_size:
            failures.append(role + ":payload_size")
        if hashlib.sha256(raw).hexdigest() != expected_sha:
            failures.append(role + ":payload_sha256")
        try:
            _iso(row.get("created_at"))
        except (TypeError, ValueError):
            failures.append(role + ":created_at")
        if not path.is_file():
            failures.append(role + ":missing")
        elif verify_full_hashes:
            if int(row.get("size_bytes", -1)) != path.stat().st_size:
                failures.append(role + ":size")
            if row.get("sha256") != _sha256_file(path):
                failures.append(role + ":sha256")
            if path.read_bytes() != raw:
                failures.append(role + ":db_first_recovery_bytes")
        payload_row = payload_rows.get(role)
        if payload_row is not None:
            try:
                payload = _json_value(payload_row.get("payload_json"))
                decoded = json.loads(raw.decode("utf-8"))
            except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
                failures.append(role + ":json_payload")
            else:
                if not isinstance(payload, Mapping) or decoded != payload:
                    failures.append(role + ":json_payload_mismatch")
                if payload_row.get("payload_json") != _canonical_json(payload):
                    failures.append(role + ":json_payload_not_canonical")
    if set(payload_rows) != expected_payload_roles:
        failures.append("json_payloads:exact_role_set")
    return result, sorted(set(failures))


def _validate_actor_artifacts(
    root: Path,
    connection: sqlite3.Connection,
    source: Mapping[str, Any],
    checks: list[Check],
    *,
    paths: Mapping[str, Path],
) -> None:
    failures: list[str] = []
    candidate_ids = source["candidate_ids"]
    n100_ids = source["n100_sample_ids"]
    actor_roles = (
        "r1_form",
        "r2_form",
        "r1_completed",
        "r1_locked",
        "r2_completed",
        "r2_locked",
        "adjudication_packet",
        "adjudication_form",
        "adjudication_completed",
        "adjudication_locked",
    )
    payloads: dict[str, Mapping[str, Any]] = {}
    for role in actor_roles:
        path = paths.get(role)
        if path is None:
            failures.append(role + ":unbound")
            continue
        try:
            payload = _load_json(path)
        except (OSError, json.JSONDecodeError):
            failures.append(role + ":json")
            continue
        if not isinstance(payload, Mapping):
            failures.append(role + ":top_shape")
            continue
        payloads[role] = payload
        leaks = blind_json_leaks(
            payload, candidate_ids=candidate_ids, n100_sample_ids=n100_ids
        )
        if leaks:
            failures.append(role + ":leak:" + ",".join(leaks))

    run_rows = _rows(connection, "SELECT * FROM runs")
    run_id = str(run_rows[0].get("run_id", "")) if len(run_rows) == 1 else ""
    contract_rows = _rows(connection, "SELECT * FROM instrument_contract")
    instrument_sha = (
        str(contract_rows[0].get("contract_sha256", "")) if len(contract_rows) == 1 else ""
    )
    source_sheets = _rows(
        connection,
        "SELECT * FROM source_sheets ORDER BY lane,display_order",
    )
    sheets_by_lane = {
        lane: [row for row in source_sheets if row.get("lane") == lane]
        for lane in ("r1", "r2")
    }
    review_rows = _rows(
        connection,
        "SELECT * FROM review_observations ORDER BY lane,display_order",
    )
    reviews_by_lane = {
        lane: [row for row in review_rows if row.get("lane") == lane]
        for lane in ("r1", "r2")
    }
    artifact_rows = {
        str(row.get("role")): row
        for row in _rows(connection, "SELECT * FROM artifacts ORDER BY role")
    }
    instructions = _expected_axis_instructions()

    reviewers: dict[str, str] = {}
    for lane in ("r1", "r2"):
        blank_role = lane + "_form"
        completed_role = lane + "_completed"
        locked_role = lane + "_locked"
        frozen_rows = sheets_by_lane[lane]
        binding_items = [
            {
                key: row.get(key)
                for key in ("display_order", "opaque_review_id", "sheet_path", "sheet_sha256")
            }
            for row in frozen_rows
        ]
        contract = {
            "run_id": run_id,
            "review_version": REVIEW_VERSION,
            "lane": lane,
            "instrument_contract_sha256": instrument_sha,
            "instructions": instructions,
            "items": binding_items,
        }
        expected_contract_sha = _record_sha(contract)
        for role, blank in ((blank_role, True), (completed_role, False)):
            payload = payloads.get(role)
            if payload is None:
                continue
            if frozenset(payload) != REVIEW_FORM_TOP_KEYS:
                failures.append(role + ":top_keys")
                continue
            expected_top = {
                "run_id": run_id,
                "review_version": REVIEW_VERSION,
                "lane": lane,
                "instrument_contract_sha256": instrument_sha,
                "instructions": instructions,
                "form_contract_sha256": expected_contract_sha,
            }
            for key, expected in expected_top.items():
                if payload.get(key) != expected:
                    failures.append(f"{role}:{key}")
            reviewer = payload.get("reviewer")
            reviewed_at = payload.get("reviewed_at")
            if blank:
                if reviewer != "" or reviewed_at != "":
                    failures.append(role + ":blank_actor")
            else:
                if not isinstance(reviewer, str) or not reviewer.strip() or reviewer != reviewer.strip():
                    failures.append(role + ":reviewer")
                elif reviewer != " ".join(reviewer.split()):
                    failures.append(role + ":reviewer_normalization")
                else:
                    reviewers[lane] = reviewer
                try:
                    _iso(reviewed_at)
                except (TypeError, ValueError):
                    failures.append(role + ":reviewed_at")
            items = payload.get("items")
            if not isinstance(items, list) or len(items) != TOTAL_SAMPLE:
                failures.append(role + ":item_count")
                continue
            for index, (item, binding) in enumerate(zip(items, binding_items), 1):
                if not isinstance(item, Mapping) or frozenset(item) != FORM_ITEM_KEYS:
                    failures.append(f"{role}:{index}:keys")
                    continue
                for key, expected in binding.items():
                    if item.get(key) != expected:
                        failures.append(f"{role}:{index}:{key}")
                observation = {key: item.get(key) for key in OBSERVATION_KEYS}
                if blank:
                    if any(value != "" for value in observation.values()):
                        failures.append(f"{role}:{index}:blank_observation")
                else:
                    item_failures = observation_failures(observation)
                    if item_failures:
                        failures.append(f"{role}:{index}:" + ",".join(item_failures))

        locked_payload = payloads.get(locked_role)
        if locked_payload is not None:
            if frozenset(locked_payload) != REVIEW_LOCK_TOP_KEYS:
                failures.append(locked_role + ":top_keys")
            completed_payload = payloads.get(completed_role, {})
            expected_top = {
                "run_id": run_id,
                "review_version": REVIEW_VERSION,
                "lane": lane,
                "instrument_contract_sha256": instrument_sha,
                "instructions": instructions,
                "form_contract_sha256": expected_contract_sha,
                "reviewer": completed_payload.get("reviewer"),
                "reviewed_at": completed_payload.get("reviewed_at"),
                "row_count": TOTAL_SAMPLE,
            }
            for key, expected in expected_top.items():
                if locked_payload.get(key) != expected:
                    failures.append(f"{locked_role}:{key}")
            locked_items = locked_payload.get("items")
            if not isinstance(locked_items, list) or len(locked_items) != TOTAL_SAMPLE:
                failures.append(locked_role + ":item_count")
                locked_items = []
            completed_items = completed_payload.get("items")
            if not isinstance(completed_items, list):
                completed_items = []
            for index, (item, completed_item, binding) in enumerate(
                zip(locked_items, completed_items, binding_items), 1
            ):
                if not isinstance(item, Mapping) or frozenset(item) != REVIEW_LOCK_ITEM_KEYS:
                    failures.append(f"{locked_role}:{index}:keys")
                    continue
                expected_item = {
                    "lane": lane,
                    "opaque_review_id": binding["opaque_review_id"],
                    **{key: completed_item.get(key) for key in OBSERVATION_KEYS},
                    "reviewer": completed_payload.get("reviewer"),
                    "reviewed_at": completed_payload.get("reviewed_at"),
                }
                if dict(item) != expected_item:
                    failures.append(f"{locked_role}:{index}:payload")
            if locked_payload.get("ordered_ledger_sha256") != _record_sha(locked_items):
                failures.append(locked_role + ":ordered_ledger_sha256")
            payload_row = _rows(
                connection,
                "SELECT * FROM review_lock_payloads WHERE lane=?",
                (lane,),
            )
            if len(payload_row) != 1 or payload_row[0].get("payload_json") != _canonical_json(
                locked_payload
            ):
                failures.append(locked_role + ":db_payload")
            lock_rows = _rows(connection, "SELECT * FROM review_locks WHERE lane=?", (lane,))
            if len(lock_rows) != 1:
                failures.append(locked_role + ":db_lock_count")
            else:
                lock = lock_rows[0]
                completed_artifact = artifact_rows.get(completed_role, {})
                locked_artifact = artifact_rows.get(locked_role, {})
                expected_lock = {
                    "reviewer": completed_payload.get("reviewer"),
                    "row_count": TOTAL_SAMPLE,
                    "completed_form_path": completed_artifact.get("path"),
                    "completed_form_sha256": completed_artifact.get("sha256"),
                    "locked_output_path": locked_artifact.get("path"),
                    "locked_output_sha256": locked_artifact.get("sha256"),
                    "ordered_ledger_sha256": _record_sha(locked_items),
                }
                for key, expected in expected_lock.items():
                    if lock.get(key) != expected:
                        failures.append(f"{locked_role}:db_lock:{key}")

    if set(reviewers) != {"r1", "r2"} or reviewers.get("r1", "").casefold() == reviewers.get(
        "r2", ""
    ).casefold():
        failures.append("reviewers_distinct")

    adjudication_rows = _rows(
        connection,
        "SELECT * FROM adjudication_items ORDER BY display_order",
    )
    source_samples = _rows(connection, "SELECT * FROM source_samples ORDER BY sample_rank")
    packet = payloads.get("adjudication_packet")
    expected_adjudication_bindings: list[dict[str, Any]] = []
    for display_order, (sample, db_item) in enumerate(zip(source_samples, adjudication_rows), 1):
        sample_id = str(sample.get("sample_id", ""))
        opaque = "adj_" + hashlib.sha256(
            f"{ADJUDICATION_VERSION}\0{run_id}\0{sample_id}".encode("utf-8")
        ).hexdigest()[:24]
        r1_sheet = next(
            (row for row in sheets_by_lane["r1"] if row.get("sample_id") == sample_id),
            {},
        )
        binding = {
            "display_order": display_order,
            "opaque_adjudication_id": opaque,
            "sheet_path": r1_sheet.get("sheet_path"),
            "sheet_sha256": r1_sheet.get("sheet_sha256"),
        }
        expected_adjudication_bindings.append(binding)
        expected_db = {
            **binding,
            "sample_id": sample_id,
        }
        for key, expected in expected_db.items():
            if db_item.get(key) != expected:
                failures.append(f"adjudication_items:{display_order}:{key}")
        for lane in ("r1", "r2"):
            review = next(
                (row for row in reviews_by_lane[lane] if row.get("sample_id") == sample_id),
                {},
            )
            if db_item.get(f"{lane}_observation_sha256") != review.get("observation_sha256"):
                failures.append(f"adjudication_items:{display_order}:{lane}_sha")
    if len(adjudication_rows) != TOTAL_SAMPLE:
        failures.append("adjudication_items:count")

    if packet is not None:
        if frozenset(packet) != ADJUDICATION_PACKET_TOP_KEYS:
            failures.append("adjudication_packet:top_keys")
        for key, expected in {
            "run_id": run_id,
            "adjudication_version": ADJUDICATION_VERSION,
            "instrument_contract_sha256": instrument_sha,
            "instructions": instructions,
            "row_count": TOTAL_SAMPLE,
        }.items():
            if packet.get(key) != expected:
                failures.append("adjudication_packet:" + key)
        packet_items = packet.get("items")
        if not isinstance(packet_items, list) or len(packet_items) != TOTAL_SAMPLE:
            failures.append("adjudication_packet:item_count")
            packet_items = []
        for index, (item, binding, sample) in enumerate(
            zip(packet_items, expected_adjudication_bindings, source_samples), 1
        ):
            if not isinstance(item, Mapping) or frozenset(item) != ADJUDICATION_PACKET_ITEM_KEYS:
                failures.append(f"adjudication_packet:{index}:keys")
                continue
            for key, expected in binding.items():
                if item.get(key) != expected:
                    failures.append(f"adjudication_packet:{index}:{key}")
            nested = item.get("reviews")
            if not isinstance(nested, Mapping) or set(nested) != {"r1", "r2"}:
                failures.append(f"adjudication_packet:{index}:reviews")
                continue
            sample_id = str(sample.get("sample_id", ""))
            for lane in ("r1", "r2"):
                review = next(
                    (row for row in reviews_by_lane[lane] if row.get("sample_id") == sample_id),
                    {},
                )
                expected_review = {
                    "opaque_review_id": review.get("opaque_review_id"),
                    **{key: review.get(key) for key in OBSERVATION_KEYS},
                    "reviewer": review.get("reviewer"),
                    "reviewed_at": review.get("reviewed_at"),
                }
                if not isinstance(nested.get(lane), Mapping) or frozenset(
                    nested[lane]
                ) != PACKET_REVIEW_KEYS or dict(nested[lane]) != expected_review:
                    failures.append(f"adjudication_packet:{index}:{lane}")
        packet_contract = {
            key: packet.get(key)
            for key in (
                "run_id", "adjudication_version", "instrument_contract_sha256",
                "instructions", "row_count", "items",
            )
        }
        if packet.get("packet_contract_sha256") != _record_sha(packet_contract):
            failures.append("adjudication_packet:packet_contract_sha256")

    adjudication_contract = {
        "run_id": run_id,
        "adjudication_version": ADJUDICATION_VERSION,
        "instrument_contract_sha256": instrument_sha,
        "instructions": instructions,
        "items": expected_adjudication_bindings,
    }
    adjudicator = ""
    adjudicated_at = ""
    for role, blank in (("adjudication_form", True), ("adjudication_completed", False)):
        payload = payloads.get(role)
        if payload is None:
            continue
        if frozenset(payload) != ADJUDICATION_FORM_TOP_KEYS:
            failures.append(role + ":top_keys")
            continue
        for key, expected in {
            "run_id": run_id,
            "adjudication_version": ADJUDICATION_VERSION,
            "instrument_contract_sha256": instrument_sha,
            "instructions": instructions,
            "form_contract_sha256": _record_sha(adjudication_contract),
        }.items():
            if payload.get(key) != expected:
                failures.append(f"{role}:{key}")
        if blank:
            if payload.get("adjudicator") != "" or payload.get("adjudicated_at") != "":
                failures.append(role + ":blank_actor")
        else:
            raw_adjudicator = payload.get("adjudicator")
            raw_adjudicated_at = payload.get("adjudicated_at")
            adjudicator = raw_adjudicator if isinstance(raw_adjudicator, str) else ""
            adjudicated_at = raw_adjudicated_at if isinstance(raw_adjudicated_at, str) else ""
            if (
                not isinstance(raw_adjudicator, str)
                or not adjudicator.strip()
                or adjudicator != adjudicator.strip()
            ):
                failures.append(role + ":adjudicator")
            elif adjudicator != " ".join(adjudicator.split()):
                failures.append(role + ":adjudicator_normalization")
            try:
                _iso(adjudicated_at)
            except (TypeError, ValueError):
                failures.append(role + ":adjudicated_at")
        items = payload.get("items")
        if not isinstance(items, list) or len(items) != TOTAL_SAMPLE:
            failures.append(role + ":item_count")
            continue
        for index, (item, binding) in enumerate(zip(items, expected_adjudication_bindings), 1):
            if not isinstance(item, Mapping) or frozenset(item) != ADJUDICATION_FORM_ITEM_KEYS:
                failures.append(f"{role}:{index}:keys")
                continue
            for key, expected in binding.items():
                if item.get(key) != expected:
                    failures.append(f"{role}:{index}:{key}")
            observation = {key: item.get(key) for key in OBSERVATION_KEYS}
            if blank:
                if any(value != "" for value in observation.values()):
                    failures.append(f"{role}:{index}:blank_observation")
            else:
                item_failures = observation_failures(observation)
                if item_failures:
                    failures.append(f"{role}:{index}:" + ",".join(item_failures))

    locked = payloads.get("adjudication_locked")
    if locked is not None:
        if frozenset(locked) != ADJUDICATION_LOCK_TOP_KEYS:
            failures.append("adjudication_locked:top_keys")
        completed = payloads.get("adjudication_completed", {})
        expected_top = {
            "run_id": run_id,
            "adjudication_version": ADJUDICATION_VERSION,
            "instrument_contract_sha256": instrument_sha,
            "instructions": instructions,
            "form_contract_sha256": _record_sha(adjudication_contract),
            "packet_contract_sha256": (
                packet.get("packet_contract_sha256") if packet is not None else None
            ),
            "adjudicator": adjudicator,
            "adjudicated_at": adjudicated_at,
            "row_count": TOTAL_SAMPLE,
        }
        for key, expected in expected_top.items():
            if locked.get(key) != expected:
                failures.append("adjudication_locked:" + key)
        locked_items = locked.get("items")
        completed_items = completed.get("items") if isinstance(completed, Mapping) else None
        if not isinstance(locked_items, list) or len(locked_items) != TOTAL_SAMPLE:
            failures.append("adjudication_locked:item_count")
            locked_items = []
        if not isinstance(completed_items, list):
            completed_items = []
        for index, (item, completed_item, binding) in enumerate(
            zip(locked_items, completed_items, expected_adjudication_bindings), 1
        ):
            expected_item = {
                "opaque_adjudication_id": binding["opaque_adjudication_id"],
                **{key: completed_item.get(key) for key in OBSERVATION_KEYS},
                "adjudicator": adjudicator,
                "adjudicated_at": adjudicated_at,
            }
            if not isinstance(item, Mapping) or frozenset(item) != ADJUDICATION_LOCK_ITEM_KEYS:
                failures.append(f"adjudication_locked:{index}:keys")
            elif dict(item) != expected_item:
                failures.append(f"adjudication_locked:{index}:payload")
        ledger_sha = _record_sha(locked_items)
        if locked.get("ordered_ledger_sha256") != ledger_sha:
            failures.append("adjudication_locked:ordered_ledger_sha256")
        payload_row = _rows(connection, "SELECT * FROM adjudication_lock_payload")
        if len(payload_row) != 1 or payload_row[0].get("payload_json") != _canonical_json(locked):
            failures.append("adjudication_locked:db_payload")
        lock_rows = _rows(connection, "SELECT * FROM adjudication_lock")
        if len(lock_rows) != 1:
            failures.append("adjudication_locked:db_lock_count")
        else:
            lock = lock_rows[0]
            completed_artifact = artifact_rows.get("adjudication_completed", {})
            locked_artifact = artifact_rows.get("adjudication_locked", {})
            expected_lock = {
                "adjudicator": adjudicator,
                "row_count": TOTAL_SAMPLE,
                "completed_form_path": completed_artifact.get("path"),
                "completed_form_sha256": completed_artifact.get("sha256"),
                "locked_output_path": locked_artifact.get("path"),
                "locked_output_sha256": locked_artifact.get("sha256"),
                "ordered_ledger_sha256": ledger_sha,
            }
            for key, expected in expected_lock.items():
                if lock.get(key) != expected:
                    failures.append(f"adjudication_locked:db_lock:{key}")
    if not adjudicator or adjudicator.casefold() in {
        value.casefold() for value in reviewers.values()
    }:
        failures.append("adjudicator_distinct")
    _check(checks, "label_free_actor_artifacts_and_pre_reveal_scrub", [], sorted(set(failures)))


def _validate_stage_b_and_fetch_payloads(
    root: Path,
    artifact_path: Path,
    connection: sqlite3.Connection,
    checks: list[Check],
    *,
    paths: Mapping[str, Path],
    phase: str,
) -> None:
    failures: list[str] = []
    run_rows = _rows(connection, "SELECT * FROM runs")
    run = run_rows[0] if len(run_rows) == 1 else {}
    run_id = str(run.get("run_id", ""))
    contract_rows = _rows(connection, "SELECT * FROM instrument_contract")
    contract_sha = contract_rows[0].get("contract_sha256") if len(contract_rows) == 1 else None
    samples = [
        {
            key: row.get(key)
            for key in (
                "sample_rank", "sample_id", "seed_rank_sha256",
                "control_commitment_sha256", "snapshot_sha256",
            )
        }
        for row in _rows(connection, "SELECT * FROM source_samples ORDER BY sample_rank")
    ]
    sheets = [
        {
            key: row.get(key)
            for key in (
                "lane", "display_order", "opaque_review_id", "sample_id", "sheet_path",
                "sheet_sha256", "sheet_bytes",
            )
        }
        for row in _rows(connection, "SELECT * FROM source_sheets ORDER BY lane,display_order")
    ]
    expected_sample = {
        "status": "PASS",
        "run_id": run_id,
        "stage": "B",
        "versions": {
            "runner": RUNNER_VERSION,
            "instrument": INSTRUMENT_VERSION,
            "sample": SAMPLE_VERSION,
            "source_sample": SOURCE_SAMPLE_VERSION,
        },
        "source": {"seed": SOURCE_SEED, "logical_sha256": SOURCE_LOGICAL_SHA256},
        "source_run_id": SOURCE_RUN_ID,
        "source_packet_binding_sha256": SOURCE_PACKET_SHA256,
        "source_visual_inventory_sha256": SOURCE_VISUAL_INVENTORY_SHA256,
        "instrument_contract_sha256": contract_sha,
        "counts": {
            "samples": TOTAL_SAMPLE,
            "sheets": SOURCE_SHEET_COUNT,
            "asset_occurrences": SOURCE_ASSET_COUNT,
            "unique_assets": SOURCE_ASSET_COUNT,
            "post_freeze_replacements": 0,
        },
        "incremental_network": {"logical": 0, "wire": 0, "redirects": 0, "retries": 0},
        "incremental_cache_writes": 0,
        "incremental_renderer_substitutions": 0,
        "samples": samples,
        "sheets": sheets,
        "sidecar": {
            "path": artifact_path.resolve().relative_to(root.resolve()).as_posix(),
            "logical_sha256": _record_sha(samples),
        },
        "forbidden_operations": {
            "vision": 0,
            "llm": 0,
            "paid_api": 0,
            "merge": 0,
            "id_issuance": 0,
            "tier_promotion": 0,
            "neon_write": 0,
            "r2_write": 0,
        },
    }
    sample_path = paths.get("sample_manifest")
    try:
        sample_payload = _load_json(sample_path) if sample_path is not None else None
    except (OSError, json.JSONDecodeError):
        sample_payload = None
    if sample_payload != expected_sample:
        failures.append("sample_manifest")

    if phase == "final":
        fetch_rows = _rows(connection, "SELECT * FROM source_fetch_assets ORDER BY asset_key")
        correction = {
            "path": DEFAULT_FETCH_CORRECTION.as_posix(),
            "size_bytes": SOURCE_FILE_SPECS[DEFAULT_FETCH_CORRECTION][0],
            "sha256": SOURCE_FILE_SPECS[DEFAULT_FETCH_CORRECTION][1],
            "corrected_projection_sha256": (
                "439426d0706c7eac1b922af9fb8f042fb7bf3cb514cda809363daef7c0fc72b3"
            ),
            "native_hard_caps": {
                "asset_occurrences": HTTP_CAP,
                "logical_http_fetches": HTTP_CAP,
                "wire_http_requests": HTTP_CAP,
                "redirects": 0,
                "retries": 0,
            },
        }
        base = {
            "status": "PASS",
            "run_id": run_id,
            "evidence_reuse": "read_only_wp26",
            "counts": {
                "samples": TOTAL_SAMPLE,
                "sheets": SOURCE_SHEET_COUNT,
                "asset_occurrences": SOURCE_ASSET_COUNT,
                "unique_assets": SOURCE_ASSET_COUNT,
                "statuses": {"cache_hit": 76, "failed": 17},
            },
            "hard_caps": {
                "asset_occurrences": HTTP_CAP,
                "logical_http_fetches": HTTP_CAP,
                "wire_http_requests": HTTP_CAP,
                "redirects": 0,
                "retries": 0,
            },
            "source_generation_network": {
                "logical_http_fetches": 17,
                "wire_http_requests": 17,
                "redirects": 0,
                "retries": 0,
            },
            "source_correction": correction,
            "incremental_network": {
                "incremental_http_requests": 0,
                "logical_http_fetches": 0,
                "wire_http_requests": 0,
                "redirects": 0,
                "retries": 0,
            },
            "cache_writes": 0,
            "renderer_substitutions": 0,
        }
        expected_fetch_manifest = {
            **base,
            "assets": [
                {
                    key: row.get(key)
                    for key in (
                        "asset_key", "source", "source_asset_id", "status", "cache_role",
                        "cache_path", "cache_file_sha256", "request_count", "error_kind",
                    )
                }
                for row in fetch_rows
            ],
        }
        expected_fetch_report = {**base, "pixel_drift_count": 0}
        for role, expected in (
            ("fetch_manifest", expected_fetch_manifest),
            ("fetch_report", expected_fetch_report),
        ):
            try:
                payload = _load_json(paths[role])
            except (KeyError, OSError, json.JSONDecodeError):
                payload = None
            if payload != expected:
                failures.append(role)
    _check(checks, "stage_b_sample_fetch_native_96_and_correction_bindings", [], failures)


def _observation_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {key: row.get(key) for key in OBSERVATION_KEYS}


def _validate_observations(
    connection: sqlite3.Connection,
    checks: list[Check],
    metrics: dict[str, Any],
) -> None:
    review_rows = _rows(
        connection,
        "SELECT * FROM review_observations ORDER BY lane,opaque_review_id",
    )
    final_rows = _rows(
        connection,
        "SELECT * FROM adjudicated_observations ORDER BY sample_id",
    )
    failures: list[str] = []
    if Counter(str(row.get("lane")) for row in review_rows) != {"r1": 24, "r2": 24}:
        failures.append("review_counts")
    if len(final_rows) != TOTAL_SAMPLE:
        failures.append("adjudication_count")
    actors: dict[str, set[str]] = {"r1": set(), "r2": set()}
    source_sheet_by_key = {
        (str(row.get("lane")), str(row.get("opaque_review_id"))): row
        for row in _rows(connection, "SELECT * FROM source_sheets")
    }
    for row in review_rows:
        lane = str(row.get("lane"))
        actors.setdefault(lane, set()).add(str(row.get("reviewer", "")).strip().casefold())
        row_failures = observation_failures(_observation_from_row(row))
        if row_failures:
            failures.append(f"{lane}:{row.get('opaque_review_id')}:" + ",".join(row_failures))
        frozen = source_sheet_by_key.get((lane, str(row.get("opaque_review_id"))))
        if frozen is None:
            failures.append(f"{lane}:{row.get('opaque_review_id')}:source_binding")
        else:
            for key in ("display_order", "sample_id"):
                if row.get(key) != frozen.get(key):
                    failures.append(f"{lane}:{row.get('opaque_review_id')}:{key}")
        semantic = {
            "lane": lane,
            "opaque_review_id": row.get("opaque_review_id"),
            "sample_id": row.get("sample_id"),
            **_observation_from_row(row),
            "reviewer": row.get("reviewer"),
            "reviewed_at": row.get("reviewed_at"),
        }
        if row.get("observation_sha256") != _record_sha(semantic):
            failures.append(f"{lane}:{row.get('opaque_review_id')}:observation_sha256")
    if any(len(values) != 1 or "" in values for values in actors.values()):
        failures.append("review_actor_per_lane")
    if actors.get("r1", set()) & actors.get("r2", set()):
        failures.append("review_actors_distinct")
    adjudicators = {
        str(row.get("adjudicator", "")).strip().casefold() for row in final_rows
    }
    if len(adjudicators) != 1 or "" in adjudicators:
        failures.append("adjudicator_actor")
    if adjudicators & (actors.get("r1", set()) | actors.get("r2", set())):
        failures.append("adjudicator_distinct")
    derivations: dict[str, tuple[str, int]] = {}
    reviews_by_sample = {
        (str(row.get("sample_id")), str(row.get("lane"))): row for row in review_rows
    }
    for row in final_rows:
        sample_id = str(row.get("sample_id", ""))
        row_failures = observation_failures(_observation_from_row(row))
        if row_failures:
            failures.append(f"final:{sample_id}:" + ",".join(row_failures))
            continue
        expected_label, expected_rule = derive_label(_observation_from_row(row))
        if row.get("derived_label") != expected_label:
            failures.append(f"final:{sample_id}:derived_label")
        try:
            actual_rule = int(row.get("derivation_rule", -1))
        except (TypeError, ValueError):
            actual_rule = -1
        if actual_rule != expected_rule:
            failures.append(f"final:{sample_id}:derivation_rule")
        try:
            adjustments = _json_value(row.get("axis_adjustments_json"))
        except (TypeError, json.JSONDecodeError):
            adjustments = None
        expected_adjustments: dict[str, Any] = {}
        for key in OBSERVATION_KEYS:
            r1 = reviews_by_sample.get((sample_id, "r1"), {}).get(key)
            r2 = reviews_by_sample.get((sample_id, "r2"), {}).get(key)
            final = row.get(key)
            expected_adjustments[key] = {
                "r1": r1,
                "r2": r2,
                "final": final,
                "reviewers_disagreed": r1 != r2,
                "changed_from_r1": final != r1,
                "changed_from_r2": final != r2,
            }
        if adjustments != expected_adjustments or row.get("axis_adjustments_json") != _canonical_json(
            expected_adjustments
        ):
            failures.append(f"final:{sample_id}:axis_adjustments")
        semantic = {
            "opaque_adjudication_id": row.get("opaque_adjudication_id"),
            "sample_id": sample_id,
            **_observation_from_row(row),
            "derived_label": expected_label,
            "derivation_rule": expected_rule,
            "axis_adjustments": expected_adjustments,
            "adjudicator": row.get("adjudicator"),
            "adjudicated_at": row.get("adjudicated_at"),
        }
        if row.get("observation_sha256") != _record_sha(semantic):
            failures.append(f"final:{sample_id}:observation_sha256")
        derivations[sample_id] = (expected_label, expected_rule)
    _check(checks, "controlled_observations_axis_only_adjudication_and_derivation", [], sorted(set(failures)))
    metrics["derivations"] = {
        "rows": len(derivations),
        "labels": dict(Counter(label for label, _ in derivations.values())),
        "rules": dict(Counter(str(rule) for _, rule in derivations.values())),
    }


def _validate_state_and_quota(
    connection: sqlite3.Connection,
    checks: list[Check],
    *,
    phase: str,
) -> None:
    events = _rows(connection, "SELECT * FROM state_events ORDER BY event_no")
    failures: list[str] = []
    if [int(row.get("event_no", -1)) for row in events] != list(range(1, len(events) + 1)):
        failures.append("event_numbers")
    states = [str(row.get("to_status", row.get("state", ""))).casefold() for row in events]
    if phase == "sample":
        # Sample validation runs after Stage C and before forms are published.
        expected_states = STATE_SEQUENCE[:3]
    else:
        terminal_rows = _rows(connection, "SELECT * FROM terminal")
        terminal = (
            str(terminal_rows[0].get("status", "")).casefold()
            if len(terminal_rows) == 1
            else ""
        )
        if terminal not in TERMINAL_STATES:
            failures.append("terminal_row")
        expected_states = (*STATE_SEQUENCE, terminal)
    if tuple(states) != tuple(expected_states):
        failures.append("exact_state_sequence")
    for index, event in enumerate(events):
        expected_from = "none" if index == 0 else states[index - 1]
        if str(event.get("from_status", "")).casefold() != expected_from:
            failures.append(f"event:{index + 1}:from_status")
    run_rows = _rows(connection, "SELECT * FROM runs")
    if len(run_rows) != 1 or not states or str(run_rows[0].get("status", "")).casefold() != states[-1]:
        failures.append("runs_status")
    previous_time: datetime | None = None
    for index, event in enumerate(events, 1):
        try:
            occurred = _iso(event.get("occurred_at"))
            if previous_time is not None and occurred < previous_time:
                failures.append(f"event:{index}:timestamp")
            previous_time = occurred
        except (TypeError, ValueError):
            failures.append(f"event:{index}:timestamp")
    if len(events) >= 3:
        try:
            detail = _json_value(events[2].get("detail_json"))
        except (TypeError, json.JSONDecodeError):
            detail = None
        if not isinstance(detail, Mapping) or detail != {
            "gate_sha256": (
                _rows(connection, "SELECT gate_sha256 FROM gate_contract")[0]["gate_sha256"]
                if _rows(connection, "SELECT gate_sha256 FROM gate_contract")
                else None
            ),
            "pre_adjudication_count": 0,
            "pre_form_count": 0,
            "pre_review_count": 0,
            "stage": "C",
        }:
            failures.append("criteria_frozen_zero_counts")
    _check(checks, "state_sequence_stage_a_b_c_and_terminal", [], sorted(set(failures)))
    quota = _rows(connection, "SELECT * FROM quota_observations ORDER BY observation_no")
    expected_quota = QUOTA_PHASES if phase == "final" else QUOTA_PHASES[:1]
    _check(
        checks,
        "quota_order_thresholds_and_sticky_stop",
        [],
        quota_ledger_failures(quota, expected_phases=expected_quota),
    )


def _validate_stage_time_and_reveal_barriers(
    connection: sqlite3.Connection,
    checks: list[Check],
) -> None:
    failures: list[str] = []
    events = _rows(connection, "SELECT * FROM state_events ORDER BY event_no")
    event_times: dict[str, datetime] = {}
    for event in events:
        try:
            event_times[str(event.get("to_status"))] = _iso(event.get("occurred_at"))
        except (TypeError, ValueError):
            pass
    gate_rows = _rows(connection, "SELECT * FROM gate_contract")
    if len(gate_rows) != 1:
        failures.append("gate_contract:row_count")
        declared_at: datetime | None = None
    else:
        try:
            declared_at = _iso(gate_rows[0].get("declared_at"))
        except (TypeError, ValueError):
            declared_at = None
            failures.append("gate_contract:declared_at")
    criteria_at = event_times.get("criteria_frozen")
    if declared_at is None or criteria_at is None or declared_at > criteria_at:
        failures.append("gate_declared_before_criteria_event")

    artifacts = _rows(connection, "SELECT * FROM artifacts ORDER BY role")
    artifact_times: dict[str, datetime] = {}
    for artifact in artifacts:
        role = str(artifact.get("role"))
        try:
            artifact_times[role] = _iso(artifact.get("created_at"))
        except (TypeError, ValueError):
            failures.append(role + ":created_at")
    pre_reveal_roles = FINAL_ARTIFACT_ROLES - {"sample_manifest", "final_report", "final_manifest"}
    if criteria_at is not None:
        for role in sorted(pre_reveal_roles):
            when = artifact_times.get(role)
            if when is None or when < criteria_at:
                failures.append(role + ":before_criteria_frozen")

    for lane in ("r1", "r2"):
        form_at = artifact_times.get(lane + "_form")
        review_rows = _rows(
            connection,
            "SELECT reviewed_at FROM review_observations WHERE lane=?",
            (lane,),
        )
        for index, row in enumerate(review_rows, 1):
            try:
                reviewed_at = _iso(row.get("reviewed_at"))
            except (TypeError, ValueError):
                continue
            if criteria_at is None or reviewed_at < criteria_at:
                failures.append(f"{lane}:{index}:review_before_criteria")
            if form_at is None or reviewed_at < form_at:
                failures.append(f"{lane}:{index}:review_before_form")
        lock_rows = _rows(connection, "SELECT * FROM review_locks WHERE lane=?", (lane,))
        if len(lock_rows) == 1:
            try:
                locked_at = _iso(lock_rows[0].get("locked_at"))
                latest_review = max(_iso(row.get("reviewed_at")) for row in review_rows)
                if locked_at < latest_review:
                    failures.append(lane + ":lock_before_review")
            except (TypeError, ValueError):
                failures.append(lane + ":lock_timestamp")

    adjudication_form_at = artifact_times.get("adjudication_form")
    final_rows = _rows(connection, "SELECT * FROM adjudicated_observations")
    for index, row in enumerate(final_rows, 1):
        try:
            adjudicated_at = _iso(row.get("adjudicated_at"))
        except (TypeError, ValueError):
            continue
        if criteria_at is None or adjudicated_at < criteria_at:
            failures.append(f"adjudication:{index}:before_criteria")
        if adjudication_form_at is None or adjudicated_at < adjudication_form_at:
            failures.append(f"adjudication:{index}:before_form")
    adjudication_lock_rows = _rows(connection, "SELECT * FROM adjudication_lock")
    if len(adjudication_lock_rows) == 1 and final_rows:
        try:
            lock_at = _iso(adjudication_lock_rows[0].get("locked_at"))
            if lock_at < max(_iso(row.get("adjudicated_at")) for row in final_rows):
                failures.append("adjudication_lock_before_rows")
            locked_event = event_times.get("adjudication_locked")
            if locked_event is None or locked_event < lock_at:
                failures.append("adjudication_locked_event_before_lock")
        except (TypeError, ValueError):
            failures.append("adjudication_lock_timestamp")

    adjudication_locked_at = event_times.get("adjudication_locked")
    reveals = _rows(connection, "SELECT * FROM control_reveals ORDER BY sample_id")
    for row in reveals:
        sample_id = str(row.get("sample_id"))
        try:
            revealed_at = _iso(row.get("revealed_at"))
        except (TypeError, ValueError):
            failures.append(sample_id + ":revealed_at")
            continue
        if adjudication_locked_at is None or revealed_at < adjudication_locked_at:
            failures.append(sample_id + ":reveal_before_adjudication_lock")
        semantic_reveal = {
            "sample_id": sample_id,
            "candidate_id": row.get("candidate_id"),
            "group_code": row.get("group_code"),
            "n100_sample_id": row.get("n100_sample_id"),
            "n100_gold_label": row.get("n100_gold_label"),
            "v1_probe_label": row.get("v1_probe_label"),
            "v2_derived_label": row.get("v2_derived_label"),
            "derivation_rule_no": row.get("derivation_rule_no"),
            "control_commitment_sha256": row.get("commitment_sha256"),
            "revealed_at": row.get("revealed_at"),
        }
        if row.get("reveal_payload_sha256") != _record_sha(semantic_reveal):
            failures.append(sample_id + ":reveal_payload_sha256")
    if reveals:
        try:
            latest_reveal = max(_iso(row.get("revealed_at")) for row in reveals)
        except (TypeError, ValueError):
            latest_reveal = None
        if latest_reveal is not None:
            for role in ("final_report", "final_manifest"):
                if artifact_times.get(role) is None or artifact_times[role] < latest_reveal:
                    failures.append(role + ":before_reveal")
            terminal_rows = _rows(connection, "SELECT * FROM terminal")
            try:
                if _iso(terminal_rows[0].get("completed_at")) < latest_reveal:
                    failures.append("terminal_before_reveal")
            except (IndexError, TypeError, ValueError):
                failures.append("terminal:completed_at")

    quota_rows = _rows(connection, "SELECT * FROM quota_observations ORDER BY observation_no")
    quota_by_phase = {str(row.get("phase")): row for row in quota_rows}
    r2_times: list[datetime] = []
    for row in _rows(connection, "SELECT reviewed_at FROM review_observations WHERE lane='r2'"):
        try:
            r2_times.append(_iso(row.get("reviewed_at")))
        except (TypeError, ValueError):
            pass
    reveal_times: list[datetime] = []
    for row in reveals:
        try:
            reveal_times.append(_iso(row.get("revealed_at")))
        except (TypeError, ValueError):
            pass
    wave_times: dict[str, datetime | None] = {
        "pre_freeze": event_times.get("instrument_frozen"),
        "pre_r1_review": artifact_times.get("r1_form"),
        "pre_r2_review": min(r2_times) if r2_times else None,
        "pre_adjudication_review": artifact_times.get("adjudication_packet"),
        "pre_reveal_score": min(reveal_times) if reveal_times else None,
    }
    for phase, wave_at in wave_times.items():
        quota = quota_by_phase.get(phase)
        if quota is None or wave_at is None:
            failures.append(phase + ":quota_wave_binding")
            continue
        try:
            observed_at = _iso(quota.get("observed_at"))
        except (TypeError, ValueError):
            continue
        age_seconds = (wave_at - observed_at).total_seconds()
        if age_seconds < -300 or age_seconds > 24 * 60 * 60:
            failures.append(phase + ":quota_freshness")

    stop_rows = _rows(
        connection,
        "SELECT * FROM quota_observations WHERE stop_required=1 ORDER BY observation_no",
    )
    if stop_rows:
        try:
            stopped_at = _iso(stop_rows[0].get("observed_at"))
        except (TypeError, ValueError):
            stopped_at = None
        if stopped_at is not None:
            if any(when > stopped_at for when in event_times.values()):
                failures.append("state_after_sticky_stop")
            if any(when > stopped_at for when in artifact_times.values()):
                failures.append("artifact_after_sticky_stop")
    _check(checks, "criteria_reviewer_adjudication_reveal_time_barriers", [], sorted(set(failures)))


def _record_hash_failures(connection: sqlite3.Connection) -> list[str]:
    """Recompute the single canonical whole-row hash contract independently."""

    failures: list[str] = []
    checked = 0
    for table in sorted(_tables(connection)):
        columns = _columns(connection, table)
        if "record_sha256" not in columns:
            continue
        for index, row in enumerate(_rows(connection, f'SELECT * FROM "{table}"')):
            checked += 1
            actual = row.pop("record_sha256", None)
            if actual != _record_sha(row):
                failures.append(f"{table}[{index}]")
    if checked == 0:
        failures.append("no_record_hashes")
    return failures


def _validate_schema_triggers_hashes(
    artifact_path: Path,
    connection: sqlite3.Connection,
    checks: list[Check],
) -> None:
    actual_tables = _tables(connection)
    _check(checks, "required_schema_tables", [], sorted(REQUIRED_TABLES - actual_tables))
    column_failures = [
        f"{table}:{','.join(_columns(connection, table))}"
        for table, expected in EXPECTED_COLUMNS.items()
        if table in actual_tables and _columns(connection, table) != expected
    ]
    _check(checks, "exact_authoritative_table_columns", [], column_failures)
    shape_failures: list[str] = []
    for table, columns in EXPECTED_COLUMNS.items():
        if table not in actual_tables:
            continue
        info = [tuple(row) for row in connection.execute(f'PRAGMA table_info("{table}")')]
        by_name = {str(row[1]): row for row in info}
        for column in columns:
            expected_type = (
                "REAL" if column == "remaining_percent" else
                "INTEGER" if column in INTEGER_COLUMNS else "TEXT"
            )
            if str(by_name[column][2]).upper() != expected_type:
                shape_failures.append(f"{table}:{column}:type")
        actual_pk = tuple(
            str(row[1]) for row in sorted(info, key=lambda item: int(item[5])) if int(row[5]) > 0
        )
        if actual_pk != EXPECTED_PRIMARY_KEYS[table]:
            shape_failures.append(f"{table}:primary_key")
    _check(checks, "exact_column_affinities_and_primary_keys", [], shape_failures)
    application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    _check(checks, "sqlite_application_id", APPLICATION_ID, application_id)
    _check(checks, "sqlite_user_version", USER_VERSION, user_version)
    integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    _check(checks, "sqlite_integrity", "ok", integrity)
    foreign_key_failures = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
    _check(checks, "sqlite_foreign_key_check", [], foreign_key_failures)
    _check(checks, "sqlite_sidecars_absent", [], _sqlite_sidecars(artifact_path))
    _check(checks, "record_hashes_single_canonical_replay", [], _record_hash_failures(connection))
    hash_column_failures = [
        table
        for table in sorted(REQUIRED_TABLES & actual_tables)
        if "record_sha256" not in _columns(connection, table)
    ]
    _check(checks, "authoritative_tables_have_record_hash", [], hash_column_failures)
    frozen_tables = REQUIRED_TABLES - {"runs"}
    trigger_sql = "\n".join(
        str(row[0] or "")
        for row in connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
        )
    ).casefold()
    trigger_failures = []
    for table in sorted(frozen_tables):
        if table not in actual_tables:
            continue
        if f"before update on {table}" not in trigger_sql:
            trigger_failures.append(table + ":update")
        if f"before delete on {table}" not in trigger_sql:
            trigger_failures.append(table + ":delete")
    if "control_reveals" in actual_tables:
        normalized = re.sub(r"\s+", " ", trigger_sql)
        if not (
            "before insert on control_reveals" in normalized
            and "adjudication_locked" in normalized
            and "raise(abort" in normalized
        ):
            trigger_failures.append("control_reveals:insert_barrier")
    _check(checks, "immutable_table_update_delete_triggers", [], trigger_failures)


def _validate_operational_zero(
    connection: sqlite3.Connection,
    checks: list[Check],
    report: Mapping[str, Any] | None,
    manifest: Mapping[str, Any] | None,
) -> None:
    failures: list[str] = []
    run_rows = _rows(connection, "SELECT * FROM runs")
    if len(run_rows) != 1:
        failures.append("runs:row_count")
    else:
        run = run_rows[0]
        for key in (
            "incremental_logical_http_fetches",
            "incremental_wire_http_requests",
            "incremental_retries",
            "incremental_redirects",
            "incremental_renderer_substitutions",
            "incremental_cache_writes",
        ):
            if run.get(key) != 0:
                failures.append("runs:" + key)
    top_zero = (
        "vision_requests",
        "llm_requests",
        "paid_api_requests",
        "merge_executions",
        "vnext_id_issuances",
        "tier_promotions",
        "neon_writes",
        "r2_writes",
    )
    for label, payload in (("report", report), ("manifest", manifest)):
        if payload is None:
            continue
        for key in top_zero:
            if key not in payload or payload.get(key) != 0:
                failures.append(f"{label}:{key}")
    if report is not None:
        fetch = report.get("fetch")
        if not isinstance(fetch, Mapping):
            failures.append("report:fetch")
        else:
            if fetch.get("incremental_network") != {
                "logical_http_fetches": 0,
                "wire_http_requests": 0,
                "incremental_http_requests": 0,
                "redirects": 0,
                "retries": 0,
            }:
                failures.append("report:fetch:incremental_network")
            if fetch.get("cache_writes") != 0:
                failures.append("report:fetch:cache_writes")
            if fetch.get("renderer_substitutions") != 0:
                failures.append("report:fetch:renderer_substitutions")
    if manifest is not None:
        if manifest.get("network") != {
            "incremental_http_requests": 0,
            "logical_http_fetches": 0,
            "wire_http_requests": 0,
            "redirects": 0,
            "retries": 0,
        }:
            failures.append("manifest:network")
        if manifest.get("cache_writes") != 0:
            failures.append("manifest:cache_writes")
        if manifest.get("renderer_substitutions") != 0:
            failures.append("manifest:renderer_substitutions")
    # The v2 artifact must not contain active fetch/cache mutation ledgers.
    for table in ("fetch_attempts", "fetch_assets", "cache_writes"):
        if table in _tables(connection):
            count = int(connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])
            if count:
                failures.append(f"{table}:{count}")
    _check(checks, "incremental_http_cache_api_and_mutations_zero", [], sorted(set(failures)))


def _validate_reveal_gate_report(
    connection: sqlite3.Connection,
    source: Mapping[str, Any],
    checks: list[Check],
    metrics: dict[str, Any],
    *,
    root: Path,
    report_file: Path,
    manifest_file: Path,
    report: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> None:
    final_rows = _rows(connection, "SELECT * FROM adjudicated_observations ORDER BY sample_id")
    final_by_sample = {str(row.get("sample_id")): row for row in final_rows}
    review_rows = _rows(connection, "SELECT * FROM review_observations ORDER BY lane,sample_id")
    reviews_by_sample = {
        (str(row.get("sample_id")), str(row.get("lane"))): row for row in review_rows
    }
    source_reveals = {str(row["sample_id"]): row for row in source["reveals"]}
    reveals = _rows(connection, "SELECT * FROM control_reveals ORDER BY sample_id")
    reveal_by_sample = {str(row.get("sample_id")): row for row in reveals}
    failures: list[str] = []
    if len(reveals) != TOTAL_SAMPLE or set(reveal_by_sample) != set(source_reveals):
        failures.append("reveal_set")
    gate_rows: list[dict[str, Any]] = []
    expected_report_rows: list[dict[str, Any]] = []
    expected_axis_audit: list[dict[str, Any]] = []
    disagreements = {key: 0 for key in OBSERVATION_KEYS}
    for sample_id in sorted(source_reveals):
        expected = source_reveals[sample_id]
        actual = reveal_by_sample.get(sample_id)
        final = final_by_sample.get(sample_id)
        if actual is None or final is None:
            continue
        observation = _observation_from_row(final)
        try:
            v2_label, rule_no = derive_label(observation)
        except ValueError:
            continue
        expected_values = {
            "candidate_id": expected["candidate_id"],
            "group_code": expected["group_code"],
            "n100_sample_id": expected["n100_sample_id"],
            "n100_gold_label": expected["n100_gold_label"],
            "v1_probe_label": expected["final_probe_label"],
            "v2_derived_label": v2_label,
            "derivation_rule_no": rule_no,
            "commitment_sha256": expected["commitment_sha256"],
        }
        for key, value in expected_values.items():
            if actual.get(key) != value:
                failures.append(f"{sample_id}:{key}")
        gate_rows.append(
            {
                "group": expected["group_code"],
                "gold": expected["n100_gold_label"],
                "v2_label": v2_label,
            }
        )
        expected_report_rows.append(
            {
                "sample_id": sample_id,
                "candidate_id": expected["candidate_id"],
                "group": expected["group_code"],
                "n100_sample_id": expected["n100_sample_id"],
                "n100_gold_label": expected["n100_gold_label"],
                "v1_probe_label": expected["final_probe_label"],
                "v2_derived_label": v2_label,
                "derivation_rule_no": rule_no,
                "observations": observation,
            }
        )
        r1 = reviews_by_sample.get((sample_id, "r1"), {})
        r2 = reviews_by_sample.get((sample_id, "r2"), {})
        adjustments = {
            key: {
                "r1": r1.get(key),
                "r2": r2.get(key),
                "final": observation[key],
                "reviewers_disagreed": r1.get(key) != r2.get(key),
                "changed_from_r1": observation[key] != r1.get(key),
                "changed_from_r2": observation[key] != r2.get(key),
            }
            for key in OBSERVATION_KEYS
        }
        for key, audit in adjustments.items():
            disagreements[key] += int(bool(audit["reviewers_disagreed"]))
        expected_axis_audit.append(
            {
                "sample_id": sample_id,
                "r1": {key: r1.get(key) for key in OBSERVATION_KEYS},
                "r2": {key: r2.get(key) for key in OBSERVATION_KEYS},
                "final": observation,
                "adjustments": adjustments,
                "derivation_replay": {
                    "label": v2_label,
                    "rule_no": rule_no,
                    "matches_persisted": (
                        final.get("derived_label") == v2_label
                        and final.get("derivation_rule") == rule_no
                    ),
                },
            }
        )
    _check(checks, "post_lock_exact_n100_v1_v2_reveal", [], sorted(set(failures)))
    expected_gate = gate_from_rows(gate_rows)
    gate_contract_rows = _rows(connection, "SELECT * FROM gate_contract")
    contract_failures: list[str] = []
    expected_contract_values = {
        "d_min_same": 13,
        "d_n": 15,
        "p1_min_non_same": 2,
        "p1_n": 3,
        "insufficient_min_exact": 4,
        "insufficient_n": 7,
        "related_min_exact": 1,
        "related_n": 2,
    }
    if len(gate_contract_rows) != 1:
        contract_failures.append("row_count")
    else:
        contract = gate_contract_rows[0]
        expected_gate_payload = _expected_gate_contract()
        if contract.get("gate_json") != _canonical_json(expected_gate_payload):
            contract_failures.append("gate_json")
        if contract.get("gate_sha256") != _record_sha(expected_gate_payload):
            contract_failures.append("gate_sha256")
        for key, value in expected_contract_values.items():
            if int(contract.get(key, -1)) != value:
                contract_failures.append(key)
    _check(checks, "four_gate_contract_predeclared_exact", [], sorted(contract_failures))
    score_rows = _rows(connection, "SELECT * FROM gate_scores ORDER BY predicate")
    score_failures: list[str] = []
    by_name = {str(row.get("predicate")): row for row in score_rows}
    if set(by_name) != set(GATE_THRESHOLDS):
        score_failures.append("predicate_set")
    for name, expected in expected_gate["predicates"].items():
        row = by_name.get(name)
        if row is None:
            continue
        for key in ("actual", "threshold", "denominator"):
            if int(row.get(key, -1)) != int(expected[key]):
                score_failures.append(f"{name}:{key}")
        if bool(row.get("passed")) != bool(expected["passed"]):
            score_failures.append(f"{name}:passed")
    _check(checks, "four_gate_scores_independent_replay", [], sorted(score_failures))
    terminal_rows = _rows(connection, "SELECT * FROM terminal")
    terminal_status = (
        str(terminal_rows[0].get("status", "")).casefold() if len(terminal_rows) == 1 else ""
    )
    _check(checks, "terminal_status_matches_four_gate", expected_gate["status"], terminal_status)

    run_rows = _rows(connection, "SELECT * FROM runs")
    run = run_rows[0] if len(run_rows) == 1 else {}
    quota_rows = [
        {
            key: row.get(key)
            for key in (
                "observation_no", "phase", "account_index", "observed_at", "source",
                "remaining_percent", "start_allowed", "stop_required", "limitation",
            )
        }
        for row in _rows(connection, "SELECT * FROM quota_observations ORDER BY observation_no")
    ]
    correction_binding = {
        "path": DEFAULT_FETCH_CORRECTION.as_posix(),
        "size_bytes": SOURCE_FILE_SPECS[DEFAULT_FETCH_CORRECTION][0],
        "sha256": SOURCE_FILE_SPECS[DEFAULT_FETCH_CORRECTION][1],
        "correction_version": "vnext-identity-instrument-probe-fetch-cap-correction-v1",
        "corrected_projection_sha256": (
            "439426d0706c7eac1b922af9fb8f042fb7bf3cb514cda809363daef7c0fc72b3"
        ),
        "actual_counts": {
            "asset_occurrences": 93,
            "logical_http_fetches": 17,
            "wire_http_requests": 17,
            "redirects": 0,
            "retries": 0,
        },
        "hard_caps": {
            "asset_occurrences": 96,
            "logical_http_fetches": 96,
            "wire_http_requests": 96,
            "redirects": 0,
            "retries": 0,
        },
    }
    source_contract = {
        "run_id": SOURCE_RUN_ID,
        "sample_version": SOURCE_SAMPLE_VERSION,
        "seed": SOURCE_SEED,
        "logical_sha256": SOURCE_LOGICAL_SHA256,
        "packet_binding_sha256": SOURCE_PACKET_SHA256,
        "visual_inventory_sha256": SOURCE_VISUAL_INVENTORY_SHA256,
    }
    expected_report = {
        "status": expected_gate["status"],
        "run_id": run.get("run_id"),
        "completed_at": terminal_rows[0].get("completed_at") if len(terminal_rows) == 1 else None,
        "measurement_only": True,
        "authorizes_tiering": False,
        "authorizes_wp28": False,
        "instrument_version": INSTRUMENT_VERSION,
        "source_contract": source_contract,
        "gate": expected_gate,
        "three_way_comparison": expected_report_rows,
        "axis_adjustment_audit": expected_axis_audit,
        "axis_adjustment_summary": {
            "reviewer_disagreements": disagreements,
            "rows": TOTAL_SAMPLE,
        },
        "review": {
            "r1_rows": TOTAL_SAMPLE,
            "r2_rows": TOTAL_SAMPLE,
            "adjudicated_rows": TOTAL_SAMPLE,
            "actors_distinct": True,
        },
        "fetch": {
            "asset_occurrences": SOURCE_ASSET_COUNT,
            "unique_assets": SOURCE_ASSET_COUNT,
            "status_counts": {"cache_hit": 76, "failed": 17},
            "source_generation_network": {
                "logical_http_fetches": 17,
                "wire_http_requests": 17,
                "redirects": 0,
                "retries": 0,
            },
            "inherited_correction_overlay": correction_binding,
            "incremental_network": {
                "logical_http_fetches": 0,
                "wire_http_requests": 0,
                "incremental_http_requests": 0,
                "redirects": 0,
                "retries": 0,
            },
            "cache_writes": 0,
            "renderer_substitutions": 0,
            "hard_caps": {
                "asset_occurrences": HTTP_CAP,
                "logical_http_fetches": HTTP_CAP,
                "wire_http_requests": HTTP_CAP,
                "redirects": 0,
                "retries": 0,
            },
        },
        "quota_observations": quota_rows,
        "vision_requests": 0,
        "llm_requests": 0,
        "paid_api_requests": 0,
        "merge_executions": 0,
        "vnext_id_issuances": 0,
        "tier_promotions": 0,
        "neon_writes": 0,
        "r2_writes": 0,
    }
    report_failures: list[str] = []
    if dict(report) != expected_report:
        report_failures.append("exact_report_payload")
    _check(checks, "report_exact_three_way_rows_axes_and_gate", [], sorted(report_failures))

    input_pins = {
        str(row.get("role")): {
            key: row.get(key) for key in ("path", "size_bytes", "sha256", "usage")
        }
        for row in _rows(connection, "SELECT * FROM input_pins ORDER BY role")
    }
    artifact_bindings = {
        str(row.get("role")): {
            key: row.get(key) for key in ("path", "size_bytes", "sha256", "stage")
        }
        for row in _rows(
            connection,
            "SELECT * FROM artifact_bindings WHERE role<>'final_manifest' ORDER BY role",
        )
    }
    report_artifact = _rows(
        connection,
        "SELECT * FROM artifacts WHERE role='final_report'",
    )
    expected_manifest = {
        "status": expected_gate["status"],
        "run_id": run.get("run_id"),
        "completed_at": terminal_rows[0].get("completed_at") if len(terminal_rows) == 1 else None,
        "versions": {
            "runner": RUNNER_VERSION,
            "instrument": INSTRUMENT_VERSION,
            "sample": SAMPLE_VERSION,
            "review": REVIEW_VERSION,
            "adjudication": ADJUDICATION_VERSION,
            "gate": GATE_VERSION,
        },
        "source_contract": source_contract,
        "report": {
            "path": report_artifact[0].get("path") if len(report_artifact) == 1 else None,
            "sha256": report_artifact[0].get("sha256") if len(report_artifact) == 1 else None,
        },
        "input_pins": input_pins,
        "artifacts": artifact_bindings,
        "counts": {
            "sample": TOTAL_SAMPLE,
            "sheets": SOURCE_SHEET_COUNT,
            "asset_occurrences": SOURCE_ASSET_COUNT,
            "r1": TOTAL_SAMPLE,
            "r2": TOTAL_SAMPLE,
            "adjudicated": TOTAL_SAMPLE,
            "post_freeze_replacements": 0,
        },
        "inherited_fetch": _expected_inherited_fetch_manifest(),
        "network": {
            "incremental_http_requests": 0,
            "logical_http_fetches": 0,
            "wire_http_requests": 0,
            "redirects": 0,
            "retries": 0,
        },
        "cache_writes": 0,
        "renderer_substitutions": 0,
        "vision_requests": 0,
        "llm_requests": 0,
        "paid_api_requests": 0,
        "merge_executions": 0,
        "vnext_id_issuances": 0,
        "tier_promotions": 0,
        "neon_writes": 0,
        "r2_writes": 0,
    }
    manifest_serialized = _canonical_json(manifest)
    manifest_failures = []
    if dict(manifest) != expected_manifest:
        manifest_failures.append("exact_manifest_payload")
    for token in ("three_way_comparison", "n100_gold_label", "v1_probe_label"):
        if token in manifest_serialized:
            manifest_failures.append(token)
    _check(checks, "manifest_provenance_only_no_reveal_payload", [], manifest_failures)

    terminal_failures: list[str] = []
    if len(terminal_rows) != 1:
        terminal_failures.append("row_count")
    else:
        terminal = terminal_rows[0]
        report_raw = report_file.read_bytes()
        manifest_raw = manifest_file.read_bytes()
        expected_terminal = {
            "status": expected_gate["status"],
            "report_path": report_artifact[0].get("path") if len(report_artifact) == 1 else None,
            "report_sha256": hashlib.sha256(report_raw).hexdigest(),
            "manifest_path": (
                _rows(connection, "SELECT path FROM artifacts WHERE role='final_manifest'")[0].get(
                    "path"
                )
                if _rows(connection, "SELECT path FROM artifacts WHERE role='final_manifest'")
                else None
            ),
            "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
            "report_json": _canonical_json(report),
            "manifest_json": _canonical_json(manifest),
        }
        for key, expected in expected_terminal.items():
            if terminal.get(key) != expected:
                terminal_failures.append(key)
        if _resolve(root, str(terminal.get("report_path", ""))) != report_file.resolve():
            terminal_failures.append("report_path_external")
        if _resolve(root, str(terminal.get("manifest_path", ""))) != manifest_file.resolve():
            terminal_failures.append("manifest_path_external")
    _check(checks, "terminal_report_manifest_path_sha_embedded_json", [], terminal_failures)
    metrics["gate"] = expected_gate


def _write_validation_report(
    path: Path,
    *,
    checks: Sequence[Check],
    metrics: Mapping[str, Any],
) -> None:
    payload = {
        "status": "PASS" if all(check.passed for check in checks) else "FAIL",
        "validator_version": VALIDATOR_VERSION,
        "checks": [
            {
                "name": check.name,
                "passed": check.passed,
                "expected": check.expected,
                "actual": check.actual,
            }
            for check in checks
        ],
        "metrics": metrics,
    }
    raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() != raw:
        raise FileExistsError(f"refusing to clobber validation report: {path}")
    path.write_bytes(raw)


def validate_identity_instrument_v2_probe(
    *,
    repo_root: Path,
    artifact_path: Path = DEFAULT_ARTIFACT,
    report_path: Path = DEFAULT_REPORT,
    manifest_path: Path = DEFAULT_MANIFEST,
    validation_report_path: Path | None = DEFAULT_VALIDATION_REPORT,
    phase: str = "final",
    verify_full_hashes: bool = True,
) -> ValidationResult:
    """Validate a WP2.7 artifact without importing or executing its producer."""

    if phase not in {"sample", "final"}:
        raise ValueError("phase must be 'sample' or 'final'")
    root = repo_root.resolve()
    artifact = _resolve(root, artifact_path)
    report_file = _resolve(root, report_path)
    manifest_file = _resolve(root, manifest_path)
    output = _resolve(root, validation_report_path) if validation_report_path else None
    checks: list[Check] = []
    metrics: dict[str, Any] = {}
    if not artifact.is_file():
        _check(checks, "artifact_exists", True, False)
        if output is not None:
            _write_validation_report(output, checks=checks, metrics=metrics)
        return ValidationResult(False, tuple(checks), metrics, output)
    _check(checks, "artifact_exists", True, True)
    try:
        source = _source_snapshot(root, verify_full_hashes=verify_full_hashes)
    except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError) as exc:
        _check(checks, "wp26_source_replay", "valid", f"{type(exc).__name__}:{exc}")
        if output is not None:
            _write_validation_report(output, checks=checks, metrics=metrics)
        return ValidationResult(False, tuple(checks), metrics, output)
    _check(checks, "wp26_source_replay", "valid", "valid")
    connection: sqlite3.Connection | None = None
    try:
        connection = _open_readonly(artifact)
        _validate_schema_triggers_hashes(artifact, connection, checks)
        if not REQUIRED_TABLES <= _tables(connection):
            if output is not None:
                _write_validation_report(output, checks=checks, metrics=metrics)
            return ValidationResult(False, tuple(checks), metrics, output)
        if any(_columns(connection, table) != expected for table, expected in EXPECTED_COLUMNS.items()):
            if output is not None:
                _write_validation_report(output, checks=checks, metrics=metrics)
            return ValidationResult(False, tuple(checks), metrics, output)
        _validate_stage_a_contract_and_pins(
            root,
            connection,
            checks,
            verify_full_hashes=verify_full_hashes,
        )
        _validate_source_pins(root, connection, source, checks, metrics)
        _validate_state_and_quota(connection, checks, phase=phase)
        paths, artifact_failures = _artifact_map(
            root,
            connection,
            verify_full_hashes=verify_full_hashes,
            phase=phase,
        )
        _check(checks, "artifact_file_size_sha_bindings", [], artifact_failures)
        _validate_stage_b_and_fetch_payloads(
            root,
            artifact,
            connection,
            checks,
            paths=paths,
            phase=phase,
        )
        if phase == "sample":
            _validate_operational_zero(connection, checks, None, None)
        else:
            _validate_actor_artifacts(
                root, connection, source, checks, paths=paths
            )
            _validate_observations(connection, checks, metrics)
            _validate_stage_time_and_reveal_barriers(connection, checks)
            try:
                report = _load_json(report_file)
                manifest = _load_json(manifest_file)
            except (OSError, json.JSONDecodeError) as exc:
                _check(
                    checks,
                    "terminal_report_manifest_json",
                    "valid",
                    f"{type(exc).__name__}:{exc}",
                )
            else:
                _check(checks, "terminal_report_manifest_json", "valid", "valid")
                _validate_operational_zero(connection, checks, report, manifest)
                _validate_reveal_gate_report(
                    connection,
                    source,
                    checks,
                    metrics,
                    root=root,
                    report_file=report_file,
                    manifest_file=manifest_file,
                    report=report,
                    manifest=manifest,
                )
    except FileExistsError:
        raise
    except Exception as exc:
        _check(
            checks,
            "validator_execution",
            "completed",
            f"{type(exc).__name__}:{exc}",
        )
    finally:
        if connection is not None:
            connection.close()
    if output is not None:
        _write_validation_report(output, checks=checks, metrics=metrics)
    return ValidationResult(
        passed=all(check.passed for check in checks),
        checks=tuple(checks),
        metrics=metrics,
        report_path=output,
    )


__all__ = [
    "Check",
    "ValidationResult",
    "CONTROLLED_VALUES",
    "OBSERVATION_KEYS",
    "FORM_ITEM_KEYS",
    "GATE_THRESHOLDS",
    "derive_label",
    "derivation_truth_table",
    "observation_failures",
    "form_item_failures",
    "blind_json_leaks",
    "quota_ledger_failures",
    "gate_from_rows",
    "validate_identity_instrument_v2_probe",
]
