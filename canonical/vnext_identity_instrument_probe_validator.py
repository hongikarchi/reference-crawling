"""Independent validator for the WP2.6 identity-instrument calibration probe.

The validator intentionally does not import the probe producer, the WP2.5
producer, or the N100 producer.  It rebuilds the frozen controls from immutable
inputs, replays the pre-declared gate, and checks every durable barrier using
only Python's standard library.
"""

from __future__ import annotations

from collections import Counter
import binascii
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import struct
from typing import Any, Iterable, Mapping, Sequence
import zlib


VALIDATOR_VERSION = "vnext-identity-instrument-probe-independent-validator-v1"
SAMPLE_VERSION = "vnext-identity-instrument-probe-v1"
SAMPLE_SEED = "vnext-wp26-instrument-probe-v1-20260827"
REVIEW_VERSION = "vnext-identity-instrument-probe-wp25-instrument-v1"
ADJUDICATION_VERSION = "vnext-identity-instrument-probe-blind-adjudication-v1"
GATE_VERSION = "vnext-identity-instrument-probe-predeclared-gate-v1"
REVIEW_LABELS = frozenset({"same", "related", "different", "insufficient"})
GROUPS = ("P1", "P2", "P3", "D")
GROUP_QUOTAS = {"P1": 3, "P2": 4, "P3": 2, "D": 15}
TOTAL_SAMPLE = 24
MAX_ASSETS_PER_SIDE = 2
MAX_ASSET_OCCURRENCES = 96
MAX_LOGICAL_HTTP_FETCHES = 96
MAX_WIRE_HTTP_REQUESTS = 96
AXES = ("image", "name", "firm", "place", "year", "scope", "basis")
SAMPLE_INPUT_ROLES = frozenset(
    {
        "v4_candidates_final",
        "wp23_n100",
        "wp23_n100_manifest",
        "wp23_n100_locked",
        "wp25_sample",
        "wp25_manifest",
        "wp26_protocol",
        "wp26_order",
        "wp25_producer",
        "wp25_protocol",
        "n100_r1_completed_form",
        "n100_r2_completed_form",
        "wp25_r1_completed_form",
        "wp25_r2_completed_form",
    }
)
PREPARE_INPUT_ROLES = frozenset(
    {"e2_image_evidence", "e1_divisare", "e1_architizer"}
)

DEFAULT_ARTIFACT = Path("data/enrichment/vnext_identity_instrument_probe_v1.db")
DEFAULT_SAMPLE_MANIFEST = Path(
    "data/reports/vnext_identity_instrument_probe_v1_sample_manifest.json"
)
DEFAULT_FETCH_MANIFEST = Path(
    "data/reports/vnext_identity_instrument_probe_v1_fetch_manifest.json"
)
DEFAULT_FETCH_REPORT = Path(
    "data/reports/vnext_identity_instrument_probe_v1_fetch_report.json"
)
DEFAULT_FETCH_CAP_CORRECTION = Path(
    "data/reports/vnext_identity_instrument_probe_v1_fetch_cap_correction.json"
)
DEFAULT_SHEETS_ROOT = Path("data/reports/vnext_identity_instrument_probe_v1_blind")
DEFAULT_R1_FORM = Path(
    "data/reports/vnext_identity_instrument_probe_v1_reviewer_r1_draft.json"
)
DEFAULT_R2_FORM = Path(
    "data/reports/vnext_identity_instrument_probe_v1_reviewer_r2_draft.json"
)
DEFAULT_R1_LOCKED = Path(
    "data/reports/vnext_identity_instrument_probe_v1_reviewer_r1_locked.json"
)
DEFAULT_R1_COMPLETED = Path(
    "data/reports/vnext_identity_instrument_probe_v1_reviewer_r1_completed.json"
)
DEFAULT_R2_LOCKED = Path(
    "data/reports/vnext_identity_instrument_probe_v1_reviewer_r2_locked.json"
)
DEFAULT_R2_COMPLETED = Path(
    "data/reports/vnext_identity_instrument_probe_v1_reviewer_r2_completed.json"
)
DEFAULT_ADJUDICATION_PACKET = Path(
    "data/reports/vnext_identity_instrument_probe_v1_adjudication_packet.json"
)
DEFAULT_ADJUDICATION_FORM = Path(
    "data/reports/vnext_identity_instrument_probe_v1_adjudication_draft.json"
)
DEFAULT_ADJUDICATION_LOCKED = Path(
    "data/reports/vnext_identity_instrument_probe_v1_adjudication_locked.json"
)
DEFAULT_ADJUDICATION_COMPLETED = Path(
    "data/reports/vnext_identity_instrument_probe_v1_adjudication_completed.json"
)
DEFAULT_GOLD_REVEAL = Path(
    "data/reports/vnext_identity_instrument_probe_v1_gold_reveal.json"
)
DEFAULT_STAGE_REPORT = Path("data/reports/vnext_identity_instrument_probe_v1_report.json")
DEFAULT_MANIFEST = Path("data/reports/vnext_identity_instrument_probe_v1_manifest.json")
DEFAULT_REPORT = Path("data/reports/vnext_identity_instrument_probe_v1_validation.json")
DEFAULT_JOB_CARD = Path(
    ".claude/ops/jobs/20260827_vnext_wp26_instrument_calibration.md"
)
DEFAULT_ORDER = Path(
    ".claude/ops/jobs/20260827_vnext_wp26_instrument_calibration_order.md"
)
DEFAULT_PROTOCOL = Path("docs/vnext/IDENTITY_INSTRUMENT_PROBE_PROTOCOL.md")
DEFAULT_WP25_PRODUCER = Path("canonical/vnext_identity_band_precision.py")
DEFAULT_WP25_PROTOCOL = Path("docs/vnext/IDENTITY_BAND_PRECISION_PROTOCOL.md")
DEFAULT_V4 = Path("data/enrichment/vnext_identity_candidates_v4.db")
DEFAULT_N100 = Path("data/enrichment/vnext_identity_n100_v1.db")
DEFAULT_N100_MANIFEST = Path("data/reports/vnext_identity_n100_v1_manifest.json")
DEFAULT_N100_ADJUDICATION = Path(
    "data/reports/vnext_identity_n100_v1_adjudication_locked.json"
)
DEFAULT_WP25 = Path("data/enrichment/vnext_identity_band_precision_v1.db")
DEFAULT_WP25_MANIFEST = Path(
    "data/reports/vnext_identity_band_precision_v1_manifest.json"
)
DEFAULT_OUTPUT_CACHE = Path("data/enrichment/vnext_identity_instrument_probe_v1_cache")

FETCH_CAP_CORRECTION_VERSION = (
    "vnext-identity-instrument-probe-fetch-cap-correction-v1"
)
FETCH_CAP_CORRECTION_CAUSE = (
    "WP2.6 reused the pinned WP2.5 prepare writer, whose fetch manifest retained "
    "the WP2.5 literal 884 while WP2.6 enforcement, protocol, and terminal "
    "reporting used 96."
)
FETCH_CAPS = {
    "asset_occurrences": MAX_ASSET_OCCURRENCES,
    "logical_http_fetches": MAX_LOGICAL_HTTP_FETCHES,
    "wire_http_requests": MAX_WIRE_HTTP_REQUESTS,
    "redirects": 0,
    "retries": 0,
}
LEGACY_WP25_FETCH_CAPS = {
    "asset_occurrences": 884,
    "logical_http_fetches": 884,
    "wire_http_requests": 884,
    "redirects": 0,
    "retries": 0,
}
PROBE_PRODUCER_PATH = Path("canonical/vnext_identity_instrument_probe.py")
PROBE_PRODUCER_RUNTIME_SIZE = 109_175
PROBE_PRODUCER_RUNTIME_SHA256 = (
    "8e12aa23980ad17c1f7c724f8a99eb47f4e2844eee660a2d374a3436c032285e"
)

N100_FORM_PATHS = (
    Path("data/reports/vnext_identity_n100_v1_reviewer_r1_draft.json"),
    Path("data/reports/vnext_identity_n100_v1_reviewer_r2_draft.json"),
)
N100_LOCK_PATHS = (
    Path("data/reports/vnext_identity_n100_v1_reviewer_r1_locked.json"),
    Path("data/reports/vnext_identity_n100_v1_reviewer_r2_locked.json"),
)
WP25_FORM_PATHS = (
    Path("data/reports/vnext_identity_band_precision_v1_reviewer_r1_completed.json"),
    Path("data/reports/vnext_identity_band_precision_v1_reviewer_r2_completed.json"),
)
WP25_LOCK_PATHS = (
    Path("data/reports/vnext_identity_band_precision_v1_reviewer_r1_locked.json"),
    Path("data/reports/vnext_identity_band_precision_v1_reviewer_r2_locked.json"),
)

EXPECTED_INPUT_PATHS: dict[str, Path] = {
    "v4_candidates_final": DEFAULT_V4,
    "wp23_n100": DEFAULT_N100,
    "wp23_n100_manifest": DEFAULT_N100_MANIFEST,
    "wp23_n100_locked": DEFAULT_N100_ADJUDICATION,
    "wp25_sample": DEFAULT_WP25,
    "wp25_manifest": DEFAULT_WP25_MANIFEST,
    "wp26_protocol": DEFAULT_PROTOCOL,
    "wp26_order": DEFAULT_ORDER,
    "wp25_producer": DEFAULT_WP25_PRODUCER,
    "wp25_protocol": DEFAULT_WP25_PROTOCOL,
    "n100_r1_completed_form": N100_FORM_PATHS[0],
    "n100_r2_completed_form": N100_FORM_PATHS[1],
    "wp25_r1_completed_form": WP25_FORM_PATHS[0],
    "wp25_r2_completed_form": WP25_FORM_PATHS[1],
    "e2_image_evidence": Path(
        "data/enrichment/divisare_architizer_image_evidence_e2_full_v5.db"
    ),
    "e1_divisare": Path(
        "data/enrichment/divisare_image_fingerprints_e1_full_v1_2.db"
    ),
    "e1_architizer": Path(
        "data/enrichment/architizer_image_fingerprints_e1_full_v1_2.db"
    ),
}

FIXED_FILE_PINS: dict[Path, tuple[int, str]] = {
    DEFAULT_ORDER: (
        2_948,
        "e7a2446c2798e86ac196fa081a41bfb3c6882b0b6922672c7ab83d37634f38c1",
    ),
    DEFAULT_WP25_PRODUCER: (
        153_786,
        "fe0072b9440eb080f039990c65c6a882d635df1bf0021f5d6a98bf977a558bfa",
    ),
    DEFAULT_WP25_PROTOCOL: (
        2_952,
        "fdc4f91334c81115b001caa69923cbdbfeec025a956a27f29804f6cb8541f725",
    ),
    DEFAULT_V4: (
        792_211_456,
        "5ab133138c26ca6d6df44a0eeec46ad413f3deb06dd2c75b3f9f799f3752d713",
    ),
    DEFAULT_N100: (
        2_183_168,
        "ee62c63ebfbd118823caa2a99becd02ca974f73b453d743106bea82e21828b7b",
    ),
    DEFAULT_N100_MANIFEST: (
        3_579,
        "e4accff5634ce97712f5cb226e9a5557a75aef7699f6d532a6fcf514f2e3e9da",
    ),
    DEFAULT_N100_ADJUDICATION: (
        63_713,
        "c4fec6261cefb6a00c25baea9e36fccb9579e4832fbe52d80fd72fa36e9d3337",
    ),
    DEFAULT_WP25: (
        7_491_584,
        "8ada59fc36c026c15b54afb5815fae240e98eea3083a52ff1e933770ce4fa85a",
    ),
    DEFAULT_WP25_MANIFEST: (
        11_768,
        "a7b29eeb48f1dfcbae0c6831080011d9e4d706e02628e91a14ab9186049ff129",
    ),
}

FORM_TOP_KEYS = frozenset(
    {
        "allowed_labels",
        "form_contract_sha256",
        "items",
        "lane",
        "review_version",
        "reviewed_at",
        "reviewer",
        "run_id",
    }
)
FORM_ITEM_KEYS = frozenset(
    {
        "display_order",
        "hard_conflict_observed",
        "label",
        "opaque_review_id",
        "rationale",
        "sheet_path",
        "sheet_sha256",
    }
)
LOCK_TOP_KEYS = frozenset(
    {
        "items",
        "lane",
        "ordered_ledger_sha256",
        "review_version",
        "reviewed_at",
        "reviewer",
        "row_count",
        "run_id",
    }
)
LOCK_ITEM_KEYS = frozenset(
    {
        "hard_conflict_observed",
        "label",
        "lane",
        "opaque_review_id",
        "rationale",
        "reviewed_at",
        "reviewer",
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
        "proposal",
        "v3_proposal",
        "v4_proposal",
        "proposed_status",
        "score",
        "numeric_score",
        "auto_merge_eligible",
        "rank",
        "sample_rank",
        "group_seed_rank",
        "policy_flags",
        "candidate_flags",
        "flags",
        "flag_type",
        "seed_rank_sha256",
    }
)
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

FINAL_ARTIFACT_STAGES: dict[str, str] = {
    "sample_manifest": "freeze",
    "fetch_manifest": "prepare",
    "fetch_report": "prepare",
    "r1_blank_form": "prepare",
    "r1_completed_form": "lock_review",
    "r1_locked": "lock_review",
    "r2_blank_form": "prepare",
    "r2_completed_form": "lock_review",
    "r2_locked": "lock_review",
    "unblind_packet": "adjudication_prepare",
    "adjudication_blank_form": "adjudication_prepare",
    "adjudication_completed_form": "adjudication_lock",
    "adjudication_locked": "adjudication_lock",
    "final_report": "reveal_score",
    "final_manifest": "reveal_score",
}
FINAL_PATH_STAGES = {
    **FINAL_ARTIFACT_STAGES,
    "sheets_root": "prepare",
}

EXPECTED_DEFAULT_PATHS: dict[str, Path] = {
    "sample_manifest": DEFAULT_SAMPLE_MANIFEST,
    "fetch_manifest": DEFAULT_FETCH_MANIFEST,
    "fetch_report": DEFAULT_FETCH_REPORT,
    "sheets_root": DEFAULT_SHEETS_ROOT,
    "r1_blank_form": DEFAULT_R1_FORM,
    "r1_completed_form": DEFAULT_R1_COMPLETED,
    "r1_locked": DEFAULT_R1_LOCKED,
    "r2_blank_form": DEFAULT_R2_FORM,
    "r2_completed_form": DEFAULT_R2_COMPLETED,
    "r2_locked": DEFAULT_R2_LOCKED,
    "unblind_packet": DEFAULT_ADJUDICATION_PACKET,
    "adjudication_blank_form": DEFAULT_ADJUDICATION_FORM,
    "adjudication_completed_form": DEFAULT_ADJUDICATION_COMPLETED,
    "adjudication_locked": DEFAULT_ADJUDICATION_LOCKED,
    "final_report": DEFAULT_STAGE_REPORT,
    "final_manifest": DEFAULT_MANIFEST,
}

REQUIRED_TABLES = frozenset(
    {
        "runs",
        "input_pins",
        "quota_observations",
        "artifact_paths",
        "artifact_bindings",
        "final_payloads",
        "cache_pins",
        "population_frames",
        "excluded_pairs",
        "sample_pairs",
        "candidate_snapshots",
        "sample_assets",
        "fetch_assets",
        "fetch_attempts",
        "review_items",
        "blind_reviews",
        "review_locks",
        "review_lock_payloads",
        "unblind_artifacts",
        "unblind_payloads",
        "adjudications",
        "band_metrics",
        "state_events",
        "form_comparison",
        "control_commitments",
        "gate_contract",
        "probe_adjudication_items",
        "probe_adjudications",
        "probe_adjudication_lock",
        "probe_adjudication_lock_payload",
        "probe_scores",
        "probe_reveals",
        "probe_terminal",
    }
)


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    expected: Any
    actual: Any
    detail: str | None = None


@dataclass(frozen=True)
class ValidationResult:
    passed: bool
    phase: str
    run_id: str | None
    checks: tuple[Check, ...]
    metrics: Mapping[str, Any]
    report_path: Path | None

    @property
    def failed_check_names(self) -> tuple[str, ...]:
        return tuple(check.name for check in self.checks if not check.passed)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _record_sha(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(16 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _resolve(root: Path, value: str | Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _open_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        "file:" + path.resolve().as_posix() + "?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _rows(
    connection: sqlite3.Connection,
    sql: str,
    parameters: Sequence[Any] = (),
) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql, parameters)]


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
        raise ValueError("unsafe table name")
    return tuple(str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})"))


def _check(
    checks: list[Check],
    name: str,
    expected: Any,
    actual: Any,
    detail: str | None = None,
) -> None:
    checks.append(Check(name, expected == actual, expected, actual, detail))


def _json_value(raw: Any) -> Any:
    if raw is None or not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _no_sqlite_sidecars(path: Path) -> bool:
    return not any(
        Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")
    )


def _cache_tree(root: Path) -> tuple[int, int, str]:
    if root.exists() and not root.is_dir():
        raise ValueError(f"cache root is not a directory: {root}")
    entries: list[dict[str, Any]] = []
    total = 0
    if root.exists():
        for path in sorted(
            (candidate for candidate in root.rglob("*") if candidate.is_file()),
            key=lambda candidate: candidate.relative_to(root).as_posix(),
        ):
            size = path.stat().st_size
            total += size
            entries.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "sha256": _sha256_file(path),
                    "size": size,
                }
            )
    return (
        len(entries),
        total,
        hashlib.sha256(_canonical_json(entries).encode("utf-8")).hexdigest(),
    )


def stable_rank(group: str, candidate_id: str) -> str:
    """Return the exact pre-declared rank for a group/candidate pair."""

    return hashlib.sha256(
        f"{SAMPLE_VERSION}\0{SAMPLE_SEED}\0{group}\0{candidate_id}".encode("utf-8")
    ).hexdigest()


def _axis_counts(items: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    return {
        axis: sum(f"{axis}=" in str(item.get("rationale", "")) for item in items)
        for axis in AXES
    }


def _full_axis_count(items: Sequence[Mapping[str, Any]]) -> int:
    tokens = ("image=", "; name=", "; firm=", "; place=", "; year=", "; scope=", "; basis=")
    return sum(
        all(token in str(item.get("rationale", "")) for token in tokens)
        and all(
            str(item.get("rationale", "")).find(tokens[index])
            < str(item.get("rationale", "")).find(tokens[index + 1])
            for index in range(len(tokens) - 1)
        )
        for item in items
    )


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def recompute_step0(repo_root: Path) -> dict[str, Any]:
    """Recompute Step 0 from the immutable completed forms, locks and DB rows."""

    root = repo_root.resolve()
    result: dict[str, Any] = {}
    specifications = {
        "n100": (DEFAULT_N100, N100_FORM_PATHS, N100_LOCK_PATHS, 210),
        "wp25": (DEFAULT_WP25, WP25_FORM_PATHS, WP25_LOCK_PATHS, 442),
    }
    for name, (database_path, form_paths, lock_paths, expected_rows) in specifications.items():
        forms = [_load_json(_resolve(root, path)) for path in form_paths]
        locks = [_load_json(_resolve(root, path)) for path in lock_paths]
        form_items = [item for form in forms for item in form.get("items", [])]
        lock_items = [item for lock in locks for item in lock.get("items", [])]
        connection = _open_readonly(_resolve(root, database_path))
        try:
            blind_rows = _rows(
                connection,
                "SELECT lane,opaque_review_id,label,rationale,hard_conflict_observed,"
                "reviewer,reviewed_at FROM blind_reviews ORDER BY lane,opaque_review_id",
            )
            db_axis_columns = sorted(set(AXES) & set(_columns(connection, "blind_reviews")))
        finally:
            connection.close()
        db_by_key = {
            (str(row["lane"]), str(row["opaque_review_id"])): row for row in blind_rows
        }
        lock_db_matches = 0
        for item in lock_items:
            row = db_by_key.get((str(item.get("lane")), str(item.get("opaque_review_id"))))
            if row is not None and all(
                item.get(key) == row.get(key)
                for key in (
                    "lane",
                    "opaque_review_id",
                    "label",
                    "rationale",
                    "hard_conflict_observed",
                    "reviewer",
                    "reviewed_at",
                )
            ):
                lock_db_matches += 1
        lengths = [len(str(item.get("rationale", ""))) for item in lock_items]
        lengths_by_lane: dict[str, list[int]] = {}
        for item in lock_items:
            lengths_by_lane.setdefault(str(item.get("lane", "")), []).append(
                len(str(item.get("rationale", "")))
            )
        result[name] = {
            "expected_rows": expected_rows,
            "form_rows": len(form_items),
            "locked_rows": len(lock_items),
            "db_rows": len(blind_rows),
            "form_top_key_sets": [sorted(form) for form in forms],
            "form_item_key_sets": sorted(
                {tuple(sorted(item)) for item in form_items}
            ),
            "lock_top_key_sets": [sorted(lock) for lock in locks],
            "lock_item_key_sets": sorted(
                {tuple(sorted(item)) for item in lock_items}
            ),
            "axis_columns": db_axis_columns,
            "axis_counts": _axis_counts(lock_items),
            "full_ordered_axis_rows": _full_axis_count(lock_items),
            "labels": dict(sorted(Counter(str(item.get("label")) for item in lock_items).items())),
            "hard_conflicts": dict(
                sorted(
                    Counter(
                        str(item.get("hard_conflict_observed")) for item in lock_items
                    ).items()
                )
            ),
            "rationale_length_min": min(lengths) if lengths else None,
            "rationale_length_max": max(lengths) if lengths else None,
            "rationale_lengths_by_lane": {
                lane: {"min": min(values), "max": max(values)}
                for lane, values in sorted(lengths_by_lane.items())
                if values
            },
            "lock_db_matches": lock_db_matches,
        }
    return result


def expected_form_comparison(step0: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    n100 = step0["n100"]
    wp25 = step0["wp25"]
    rows = (
        (
            "top_level_json_keys",
            sorted(FORM_TOP_KEYS),
            sorted(FORM_TOP_KEYS),
            "same top-level form structure",
        ),
        (
            "item_json_keys",
            sorted(FORM_ITEM_KEYS),
            sorted(FORM_ITEM_KEYS),
            "same item form structure",
        ),
        (
            "axis_json_or_db_columns",
            [],
            [],
            "neither producer has separate axis columns",
        ),
        (
            "ordered_axis_tags",
            {"tagged": n100["full_ordered_axis_rows"], "rows": n100["locked_rows"]},
            {"tagged": wp25["full_ordered_axis_rows"], "rows": wp25["locked_rows"]},
            "N100 used ordered tags operationally; WP2.5 did not",
        ),
        (
            "rationale_lengths",
            n100["rationale_lengths_by_lane"],
            wp25["rationale_lengths_by_lane"],
            "WP2.5 rationale is materially shorter",
        ),
        (
            "actual_label_usage",
            n100["labels"],
            wp25["labels"],
            "WP2.5 did not exercise the intermediate labels",
        ),
        (
            "hard_conflict_payload",
            n100["hard_conflicts"],
            wp25["hard_conflicts"],
            "WP2.5 retained a weaker yes/no operational value",
        ),
        (
            "blank_completed_provenance",
            {"blank_distinct": False},
            {"blank_distinct": True},
            "WP2.5 preserves distinct blank and completed forms",
        ),
    )
    return {
        key: {"n100": left, "wp25": right, "finding": finding}
        for key, left, right, finding in rows
    }


def _walk_json(value: Any, path: str = "$") -> Iterable[tuple[str, str, Any]]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            yield child_path, str(key), child
            yield from _walk_json(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_json(child, f"{path}[{index}]")


def _blind_json_leaks(
    path: Path,
    candidate_ids: set[str],
    *,
    sample_ids: set[str] | None = None,
    gold_values: set[str] | None = None,
) -> list[str]:
    """Return paths/tokens that disclose fields hidden before gold reveal."""

    try:
        payload = _load_json(path)
    except (OSError, json.JSONDecodeError):
        return ["unreadable_json"]
    leaks: list[str] = []
    serialized = path.read_text(encoding="utf-8")
    for location, key, value in _walk_json(payload):
        if key.casefold() in HIDDEN_KEYS:
            leaks.append(location)
        if isinstance(value, str) and value in HIDDEN_VALUE_TOKENS:
            leaks.append(f"forbidden_value:{value}")
    for candidate_id in sorted(candidate_ids):
        if candidate_id and candidate_id in serialized:
            leaks.append("selected_candidate_value")
            break
    for sample_id in sorted(sample_ids or set()):
        if sample_id and sample_id in serialized:
            leaks.append("hidden_sample_value")
            break
    for gold in sorted(gold_values or set()):
        if gold and gold in serialized:
            leaks.append("hidden_gold_value")
            break
    return sorted(set(leaks))


def _quota_ledger_failures(
    rows: Sequence[Mapping[str, Any]], *, phase: str
) -> list[str]:
    failures: list[str] = []
    required = (
        "pre_freeze",
        "pre_prepare",
        "pre_r1_review",
        "pre_r2_review",
        "pre_adjudication_review",
        "pre_reveal_score",
    )
    seen: list[str] = []
    previous_time: datetime | None = None
    previous_phase_index = -1
    stopped = False
    for expected_no, row in enumerate(rows, 1):
        prefix = str(row.get("observation_no", expected_no))
        if row.get("observation_no") != expected_no:
            failures.append(f"{prefix}:observation_order")
        phase_name = str(row.get("phase", ""))
        if not phase_name:
            failures.append(f"{prefix}:phase")
        elif phase_name not in required:
            failures.append(f"{prefix}:unknown_phase")
        else:
            phase_index = required.index(phase_name)
            if phase_index < previous_phase_index or phase_index > previous_phase_index + 1:
                failures.append(f"{prefix}:phase_order")
            if seen and phase_name == "pre_freeze":
                failures.append(f"{prefix}:repeated_pre_freeze")
            previous_phase_index = phase_index
        seen.append(phase_name)
        try:
            observed = _iso(str(row.get("observed_at", "")))
            if previous_time is not None and observed < previous_time:
                failures.append(f"{prefix}:timestamp_order")
            previous_time = observed
        except ValueError:
            failures.append(f"{prefix}:timestamp")
        try:
            signals = json.loads(str(row.get("signals_json", "")))
        except json.JSONDecodeError:
            signals = None
        remaining_values: list[float] = []
        if not isinstance(signals, list) or not signals:
            failures.append(f"{prefix}:signals")
        else:
            for signal in signals:
                if not isinstance(signal, Mapping):
                    failures.append(f"{prefix}:signal_shape")
                    continue
                try:
                    remaining = float(signal["remaining_percent"])
                    if not 0 <= remaining <= 100:
                        raise ValueError
                    remaining_values.append(remaining)
                except (KeyError, TypeError, ValueError):
                    failures.append(f"{prefix}:signal_remaining")
                if signal.get("account_index") != row.get("account_index"):
                    failures.append(f"{prefix}:signal_account")
        try:
            basis = float(row.get("gate_basis_remaining_percent"))
        except (TypeError, ValueError):
            basis = -1.0
            failures.append(f"{prefix}:basis")
        if remaining_values and basis != min(remaining_values):
            failures.append(f"{prefix}:conservative_gate")
        expected_start = basis > 30
        expected_stop = basis <= 20
        if bool(row.get("start_allowed")) != expected_start:
            failures.append(f"{prefix}:start_gate")
        if bool(row.get("stop_required")) != expected_stop:
            failures.append(f"{prefix}:stop_gate")
        if basis <= 30:
            failures.append(f"{prefix}:wave_denied_at_or_below_30")
        if stopped:
            failures.append(f"{prefix}:activity_after_stop")
        stopped = stopped or expected_stop
    required_now = required[:1] if phase == "sample" else required
    missing = [item for item in required_now if item not in seen]
    if missing:
        failures.append("missing_wave_phases:" + ",".join(missing))
    order = [seen.index(item) for item in required_now if item in seen]
    if order != sorted(order):
        failures.append("wave_phase_order")
    if phase == "sample" and rows and float(rows[-1].get("gate_basis_remaining_percent", 0)) <= 30:
        failures.append("sample_started_at_or_below_30")
    return sorted(set(failures))


def gate_from_labels(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Replay the three immutable Step 2 predicates from revealed final labels."""

    by_group: dict[str, list[str]] = {group: [] for group in GROUPS}
    for row in rows:
        group = str(row.get("group", ""))
        label = str(row.get("probe_label", row.get("final_probe_label", "")))
        if group in by_group:
            by_group[group].append(label)
    p1_non_same = sum(label != "same" for label in by_group["P1"])
    p23_non_same = sum(
        label != "same" for group in ("P2", "P3") for label in by_group[group]
    )
    d_same = sum(label == "same" for label in by_group["D"])
    predicates = {
        "p1_non_same_at_least_2_of_3": p1_non_same >= 2,
        "p2_p3_non_same_at_least_3_of_6": p23_non_same >= 3,
        "d_same_at_least_13_of_15": d_same >= 13,
    }
    return {
        "counts": {
            "P1_non_same": p1_non_same,
            "P2_P3_non_same": p23_non_same,
            "D_same": d_same,
        },
        "predicates": predicates,
        "status": "pass" if all(predicates.values()) else "fail",
    }


def replay_control_universe(repo_root: Path) -> dict[str, Any]:
    """Rebuild P1/P2/P3 and the seed-selected D15 from frozen N100 gold."""

    root = repo_root.resolve()
    n100 = _open_readonly(_resolve(root, DEFAULT_N100))
    v4 = _open_readonly(_resolve(root, DEFAULT_V4))
    wp25 = _open_readonly(_resolve(root, DEFAULT_WP25))
    try:
        n100_rows = _rows(
            n100,
            "SELECT p.sample_id,p.candidate_id,p.cohort,p.stratum,p.sample_origin,"
            "COALESCE(a.final_gold_label,c.final_gold_label) AS frozen_gold_label,"
            "COALESCE(a.record_sha256,c.record_sha256) AS source_adjudication_record_sha256 "
            "FROM sample_pairs p "
            "LEFT JOIN adjudications a USING(run_id,sample_id) "
            "LEFT JOIN carry_forward_gold c USING(run_id,sample_id)",
        )
        flagged = {
            str(row[0])
            for row in v4.execute("SELECT DISTINCT candidate_id FROM candidate_policy_flags")
        }
        wp25_candidates = {
            str(row[0]) for row in wp25.execute("SELECT candidate_id FROM sample_pairs")
        }
    finally:
        n100.close()
        v4.close()
        wp25.close()

    if any(row["frozen_gold_label"] not in REVIEW_LABELS for row in n100_rows):
        raise ValueError("N100 gold join is incomplete or invalid")
    p1 = [
        row
        for row in n100_rows
        if row["stratum"] == "A_precision_core"
        and row["frozen_gold_label"] != "same"
        and row["candidate_id"] not in flagged
    ]
    p2 = [
        row
        for row in n100_rows
        if row["stratum"] == "B3_fanout_defer"
        and row["frozen_gold_label"] in {"related", "insufficient"}
    ]
    p3 = [
        row
        for row in n100_rows
        if row["stratum"] == "B2_phash_veto"
        and row["frozen_gold_label"] == "insufficient"
    ]
    d_universe = [
        row
        for row in n100_rows
        if row["stratum"] == "A_precision_core"
        and row["frozen_gold_label"] == "same"
    ]
    d = sorted(
        d_universe,
        key=lambda row: (stable_rank("D", str(row["candidate_id"])), row["candidate_id"]),
    )[: GROUP_QUOTAS["D"]]
    grouped = {"P1": p1, "P2": p2, "P3": p3, "D": d}
    selected: list[dict[str, Any]] = []
    for group, rows in grouped.items():
        for row in rows:
            selected.append(
                {
                    **row,
                    "group": group,
                    "seed_rank_sha256": stable_rank(group, str(row["candidate_id"])),
                }
            )
    for row in selected:
        row["freeze_rank_sha256"] = hashlib.sha256(
            f"{SAMPLE_VERSION}\0{SAMPLE_SEED}\0probe\0{row['candidate_id']}".encode("utf-8")
        ).hexdigest()
    selected.sort(key=lambda row: (row["freeze_rank_sha256"], row["candidate_id"]))
    candidate_ids = [str(row["candidate_id"]) for row in selected]
    sample_ids = [str(row["sample_id"]) for row in selected]
    return {
        "groups": grouped,
        "d_universe": d_universe,
        "selected": selected,
        "candidate_ids": candidate_ids,
        "sample_ids": sample_ids,
        "wp25_candidate_ids": wp25_candidates,
        "flagged_candidate_ids": flagged,
        "counts": {group: len(rows) for group, rows in grouped.items()},
        "gold_counts": {
            group: dict(
                sorted(Counter(str(row["frozen_gold_label"]) for row in rows).items())
            )
            for group, rows in grouped.items()
        },
        "d_universe_count": len(d_universe),
        "candidate_unique": len(candidate_ids) == len(set(candidate_ids)),
        "sample_unique": len(sample_ids) == len(set(sample_ids)),
        "wp25_overlap": sorted(set(candidate_ids) & wp25_candidates),
    }


def persisted_control_rows(run_id: str, expected: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for rank, control in enumerate(expected["selected"], 1):
        candidate_id = str(control["candidate_id"])
        opaque = "ctl_" + hashlib.sha256(
            f"{run_id}\0opaque-control\0{candidate_id}".encode("utf-8")
        ).hexdigest()[:32]
        sample_id = "probe_" + hashlib.sha256(
            f"{run_id}\0{opaque}".encode("utf-8")
        ).hexdigest()[:24]
        rows.append(
            {
                **control,
                "sample_rank": rank,
                "n100_sample_id": str(control["sample_id"]),
                "sample_id": sample_id,
                "opaque_control_id": opaque,
                "seed_rank_sha256": str(control["freeze_rank_sha256"]),
            }
        )
    return rows


def control_commitment(row: Mapping[str, Any]) -> str:
    return _record_sha(
        {
            "candidate_id": str(row["candidate_id"]),
            "group": str(row["group"]),
            "n100_sample_id": str(row.get("n100_sample_id", row["sample_id"])),
            "final_gold_label": str(row["frozen_gold_label"]),
            "adjudication_record_sha256": str(row["source_adjudication_record_sha256"]),
        }
    )


def source_record_commitment(row: Mapping[str, Any]) -> str:
    """Recompute the blinded commitment to the pinned N100 adjudication row."""

    return _record_sha(
        {
            "namespace": "wp26-source-record-commitment-v1",
            "candidate_id": str(row["candidate_id"]),
            "source_adjudication_record_sha256": str(
                row["source_adjudication_record_sha256"]
            ),
        }
    )


def _expected_persisted_gate_status(run_status: str) -> str:
    return run_status if run_status in {"pass", "fail"} else "declared"


def _terminal_status_matches(expected: str, actual: Any) -> bool:
    return expected in {"pass", "fail"} and isinstance(actual, str) and actual.casefold() == expected


def _resolved_report_binding_matches(
    root: Path,
    actual: Any,
    *,
    expected_path: Any,
    expected_sha256: Any,
) -> bool:
    return (
        isinstance(actual, Mapping)
        and set(actual) == {"path", "sha256"}
        and _is_sha256(actual.get("sha256"))
        and actual.get("sha256") == expected_sha256
        and _resolve(root, str(actual.get("path", "")))
        == _resolve(root, str(expected_path))
    )


def _logical_sha(connection: sqlite3.Connection, run_id: str) -> str:
    digest = hashlib.sha256((SAMPLE_VERSION + "\n").encode("utf-8"))
    tables: tuple[tuple[str, str], ...] = (
        ("input_pins", "role"),
        ("quota_observations", "observation_no"),
        ("population_frames", "stratum"),
        ("excluded_pairs", "stratum,candidate_id,exclusion_type"),
        ("sample_pairs", "sample_rank"),
        ("candidate_snapshots", "sample_id"),
    )
    for table, order in tables:
        digest.update((table + "\n").encode("utf-8"))
        sql = f"SELECT record_sha256 FROM {table} WHERE run_id=?"
        if table == "input_pins":
            sql += " AND role NOT LIKE 'e1_%' AND role<>'e2_image_evidence'"
        elif table == "quota_observations":
            sql += " AND phase='pre_freeze'"
        sql += f" ORDER BY {order}"
        for row in connection.execute(sql, (run_id,)):
            digest.update((str(row[0]) + "\n").encode("ascii"))
    return digest.hexdigest()


def _cache_relative_path(pixel_sha256: str, transform_version: str) -> Path:
    transform_key = hashlib.sha256(transform_version.encode("utf-8")).hexdigest()[:12]
    return Path(pixel_sha256[:2]) / f"{pixel_sha256}.{transform_key}.jpg"


def _validate_fetch(
    root: Path,
    connection: sqlite3.Connection,
    run: Mapping[str, Any],
    checks: list[Check],
    metrics: dict[str, Any],
) -> None:
    """Independently enforce the one-shot HTTP and cache-tree contract."""

    run_id = str(run["run_id"])
    assets = _rows(connection, "SELECT * FROM sample_assets WHERE run_id=?", (run_id,))
    fetches = _rows(connection, "SELECT * FROM fetch_assets WHERE run_id=?", (run_id,))
    attempts = _rows(connection, "SELECT * FROM fetch_attempts WHERE run_id=?", (run_id,))
    pins = _rows(connection, "SELECT * FROM cache_pins WHERE run_id=?", (run_id,))
    pin_by_role = {str(row["role"]): row for row in pins}

    per_side = Counter((str(row["sample_id"]), str(row["side"])) for row in assets)
    side_failures = [
        f"{sample}:{side}:{count}"
        for (sample, side), count in sorted(per_side.items())
        if count > MAX_ASSETS_PER_SIDE
    ]
    _check(checks, "assets_max_two_per_side", [], side_failures)
    occurrence_count = len(assets)
    _check(
        checks,
        "asset_occurrence_cap_and_run_count",
        {"within_cap": True, "run": occurrence_count},
        {
            "within_cap": occurrence_count <= MAX_ASSET_OCCURRENCES,
            "run": int(run.get("asset_occurrence_count", -1)),
        },
    )

    asset_keys = {str(row["asset_key"]) for row in assets}
    fetch_by_key = {str(row["asset_key"]): row for row in fetches}
    _check(checks, "asset_fetch_coverage", asset_keys, set(fetch_by_key))
    unique_count = len(fetch_by_key)
    _check(checks, "unique_asset_run_count", unique_count, int(run.get("unique_asset_count", -1)))

    attempts_by_key: dict[str, list[dict[str, Any]]] = {}
    for attempt in attempts:
        attempts_by_key.setdefault(str(attempt["asset_key"]), []).append(attempt)
    bijection_failures: list[str] = []
    status_failures: list[str] = []
    redirect_retry_failures: list[str] = []
    cache_failures: list[str] = []
    logical = 0
    wire = 0
    allowed_statuses = {
        "exact",
        "delivery_changed",
        "source_changed",
        "cache_hit",
        "failed",
    }
    for key, fetch in sorted(fetch_by_key.items()):
        status = str(fetch.get("status"))
        row_attempts = attempts_by_key.get(key, [])
        expected_attempts = 0 if status == "cache_hit" else 1
        if len(row_attempts) != expected_attempts:
            bijection_failures.append(f"{key}:{len(row_attempts)}!={expected_attempts}")
        if status != "cache_hit":
            logical += 1
        if status not in allowed_statuses:
            status_failures.append(f"{key}:non_terminal_or_unknown:{status}")
        expected_raw = fetch.get("expected_raw_sha256")
        expected_pixel = fetch.get("expected_pixel_sha256")
        actual_raw = fetch.get("actual_raw_sha256")
        actual_pixel = fetch.get("actual_pixel_sha256")
        if status == "exact" and not (
            actual_raw == expected_raw and actual_pixel == expected_pixel
        ):
            status_failures.append(f"{key}:exact_hash_semantics")
        elif status == "delivery_changed" and not (
            actual_raw not in (None, expected_raw) and actual_pixel == expected_pixel
        ):
            status_failures.append(f"{key}:delivery_changed_hash_semantics")
        elif status == "source_changed" and not (
            actual_raw not in (None, expected_raw)
            and actual_pixel not in (None, expected_pixel)
        ):
            status_failures.append(f"{key}:source_changed_hash_semantics")
        elif status == "failed" and (actual_raw is not None or actual_pixel is not None):
            status_failures.append(f"{key}:failed_pixel_without_raw")
        elif status == "cache_hit" and (
            int(fetch.get("request_count", -1)) != 0
            or fetch.get("actual_final_url") is not None
            or fetch.get("http_status") is not None
        ):
            status_failures.append(f"{key}:cache_hit_network_fields")

        if row_attempts:
            attempt = row_attempts[0]
            request_count = int(attempt.get("request_count", -1))
            wire += request_count
            try:
                redirects = json.loads(str(attempt.get("redirect_chain_json", "")))
            except json.JSONDecodeError:
                redirects = ["invalid"]
            if int(attempt.get("attempt_no", -1)) != 1:
                redirect_retry_failures.append(f"{key}:retry")
            if redirects != []:
                redirect_retry_failures.append(f"{key}:redirect")
            if request_count not in {0, 1} or request_count != int(fetch.get("request_count", -1)):
                redirect_retry_failures.append(f"{key}:request_count")
            if str(attempt.get("status")) != status:
                status_failures.append(f"{key}:attempt_status")
        role = fetch.get("cache_role")
        cache_path = fetch.get("cache_path")
        if status == "failed":
            if role is not None or cache_path is not None:
                cache_failures.append(f"{key}:failed_cache")
            continue
        if role not in pin_by_role or not isinstance(cache_path, str):
            cache_failures.append(f"{key}:cache_role")
            continue
        pin = pin_by_role[str(role)]
        if status == "cache_hit" and pin.get("access_mode") != "read_only":
            cache_failures.append(f"{key}:cache_hit_role")
        if status != "cache_hit" and pin.get("access_mode") != "write_only":
            cache_failures.append(f"{key}:cache_role")
        pin_root = _resolve(root, str(pin["path"]))
        resolved = _resolve(root, cache_path)
        pixel_for_path = expected_pixel if status == "cache_hit" else actual_pixel
        if not _is_sha256(pixel_for_path):
            cache_failures.append(f"{key}:pixel_sha")
            continue
        expected_path = (
            pin_root
            / _cache_relative_path(str(pixel_for_path), str(pin["transform_version"]))
        ).resolve()
        if resolved != expected_path or not resolved.is_relative_to(pin_root):
            cache_failures.append(f"{key}:cache_tree_path")
        if not resolved.is_file():
            cache_failures.append(f"{key}:cache_file")

    for key in sorted(set(attempts_by_key) - set(fetch_by_key)):
        bijection_failures.append(f"{key}:attempt_without_fetch")
    if logical != len(attempts):
        redirect_retry_failures.append("logical_attempt_mismatch")
    if logical != int(run.get("logical_http_fetches", -1)):
        redirect_retry_failures.append("logical_run_mismatch")
    if wire != int(run.get("wire_http_requests", -1)):
        redirect_retry_failures.append("wire_run_mismatch")
    if logical > MAX_LOGICAL_HTTP_FETCHES or wire > MAX_WIRE_HTTP_REQUESTS:
        redirect_retry_failures.append("hard_cap")
    if wire != logical:
        redirect_retry_failures.append("logical_wire_mismatch")
    _check(checks, "fetch_attempt_bijection", [], sorted(bijection_failures))
    _check(checks, "fetch_status_hash_semantics", [], sorted(status_failures))
    _check(checks, "fetch_redirects_retries_disabled", [], sorted(set(redirect_retry_failures)))
    _check(checks, "fetch_cache_role_path_tree_binding", [], sorted(set(cache_failures)))
    metrics["fetch"] = {
        "asset_occurrences": occurrence_count,
        "unique_assets": unique_count,
        "logical_http_fetches": logical,
        "wire_http_requests": wire,
        "statuses": dict(sorted(Counter(str(row["status"]) for row in fetches).items())),
    }


def _json_exact(actual: Any, expected: Any) -> bool:
    """Compare JSON values without Python's bool/int or float/int coercions."""

    try:
        return _canonical_json(actual) == _canonical_json(expected)
    except (TypeError, ValueError):
        return False


def _correction_file_binding_failures(
    root: Path,
    binding: Any,
    *,
    label: str,
    expected_path: Path,
    expected_size: int,
    expected_sha256: str,
) -> list[str]:
    failures: list[str] = []
    if not isinstance(binding, Mapping):
        return [f"{label}:mapping"]
    if set(binding) != {"path", "size_bytes", "sha256"}:
        failures.append(f"{label}:keys")
    try:
        bound_path = _resolve(root, str(binding.get("path")))
    except (OSError, ValueError):
        failures.append(f"{label}:path")
    else:
        if bound_path != expected_path.resolve():
            failures.append(f"{label}:path")
    if not _json_exact(binding.get("size_bytes"), expected_size):
        failures.append(f"{label}:size_bytes")
    if not _json_exact(binding.get("sha256"), expected_sha256):
        failures.append(f"{label}:sha256")
    return failures


def _fetch_cap_correction_failures(
    root: Path,
    *,
    correction_path: Path,
    fetch_manifest_path: Path,
    fetch_manifest: Mapping[str, Any],
    run_id: str,
    actual_counts: Mapping[str, int],
    artifact_path: Path,
    stage_report_path: Path,
    manifest_path: Path,
    terminal: Mapping[str, Any],
) -> list[str]:
    """Validate the one permitted immutable correction for the legacy 884 literal."""

    failures: list[str] = []
    if not correction_path.is_file():
        return ["correction:missing"]
    try:
        correction = _load_json(correction_path)
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return ["correction:invalid_json"]
    if not isinstance(correction, Mapping):
        return ["correction:mapping"]

    expected_top_keys = {
        "status",
        "correction_version",
        "run_id",
        "scope",
        "cause",
        "original_fetch_manifest",
        "corrected_contract",
        "corrected_projection",
        "terminal_bindings",
        "producer_bindings",
    }
    if set(correction) != expected_top_keys:
        failures.append("correction:top_keys")
    for key, expected in (
        ("status", "CORRECTION"),
        ("correction_version", FETCH_CAP_CORRECTION_VERSION),
        ("run_id", run_id),
        ("scope", "fetch_manifest.hard_caps"),
    ):
        if not _json_exact(correction.get(key), expected):
            failures.append(f"correction:{key}")

    cause = correction.get("cause")
    expected_cause = {
        "code": "reused_wp25_fetch_manifest_literal",
        "detail": FETCH_CAP_CORRECTION_CAUSE,
    }
    if not _json_exact(cause, expected_cause):
        failures.append("correction:cause")

    original = correction.get("original_fetch_manifest")
    if not isinstance(original, Mapping):
        failures.append("original_fetch_manifest:mapping")
    else:
        if set(original) != {"path", "size_bytes", "sha256", "recorded_hard_caps"}:
            failures.append("original_fetch_manifest:keys")
        try:
            original_path = _resolve(root, str(original.get("path")))
        except (OSError, ValueError):
            failures.append("original_fetch_manifest:path")
        else:
            if original_path != fetch_manifest_path.resolve():
                failures.append("original_fetch_manifest:path")
        if not _json_exact(original.get("size_bytes"), fetch_manifest_path.stat().st_size):
            failures.append("original_fetch_manifest:size_bytes")
        if not _json_exact(original.get("sha256"), _sha256_file(fetch_manifest_path)):
            failures.append("original_fetch_manifest:sha256")
        if not _json_exact(original.get("recorded_hard_caps"), LEGACY_WP25_FETCH_CAPS):
            failures.append("original_fetch_manifest:recorded_hard_caps")

    expected_contract = {
        "hard_caps": FETCH_CAPS,
        "actual_counts": dict(actual_counts),
        "within_hard_caps": all(
            int(actual_counts[key]) <= int(FETCH_CAPS[key])
            for key in FETCH_CAPS
        ),
    }
    if not _json_exact(correction.get("corrected_contract"), expected_contract):
        failures.append("correction:corrected_contract")

    corrected_manifest = dict(fetch_manifest)
    corrected_manifest["hard_caps"] = dict(FETCH_CAPS)
    expected_projection = {
        "canonicalization": "canonical-json-v1",
        "operation": "replace-hard_caps-only",
        "sha256": _record_sha(corrected_manifest),
    }
    if not _json_exact(correction.get("corrected_projection"), expected_projection):
        failures.append("correction:corrected_projection")

    terminal_bindings = correction.get("terminal_bindings")
    if not isinstance(terminal_bindings, Mapping):
        failures.append("terminal_bindings:mapping")
    else:
        if set(terminal_bindings) != {"database", "report", "manifest"}:
            failures.append("terminal_bindings:keys")
        for label, path in (
            ("database", artifact_path),
            ("report", stage_report_path),
            ("manifest", manifest_path),
        ):
            if not path.is_file():
                failures.append(f"terminal_bindings.{label}:file")
                continue
            failures.extend(
                _correction_file_binding_failures(
                    root,
                    terminal_bindings.get(label),
                    label=f"terminal_bindings.{label}",
                    expected_path=path,
                    expected_size=path.stat().st_size,
                    expected_sha256=_sha256_file(path),
                )
            )

    if str(terminal.get("run_id")) != run_id:
        failures.append("probe_terminal:run_id")
    for label, path, path_key, sha_key in (
        ("report", stage_report_path, "report_path", "report_sha256"),
        ("manifest", manifest_path, "manifest_path", "manifest_sha256"),
    ):
        try:
            terminal_path = _resolve(root, str(terminal.get(path_key)))
        except (OSError, ValueError):
            failures.append(f"probe_terminal:{label}_path")
        else:
            if terminal_path != path.resolve():
                failures.append(f"probe_terminal:{label}_path")
        if not path.is_file() or not _json_exact(terminal.get(sha_key), _sha256_file(path)):
            failures.append(f"probe_terminal:{label}_sha256")

    producer_bindings = correction.get("producer_bindings")
    producer_path = _resolve(root, PROBE_PRODUCER_PATH)
    if not isinstance(producer_bindings, Mapping):
        failures.append("producer_bindings:mapping")
    else:
        if set(producer_bindings) != {"runtime", "current"}:
            failures.append("producer_bindings:keys")
        failures.extend(
            _correction_file_binding_failures(
                root,
                producer_bindings.get("runtime"),
                label="producer_bindings.runtime",
                expected_path=producer_path,
                expected_size=PROBE_PRODUCER_RUNTIME_SIZE,
                expected_sha256=PROBE_PRODUCER_RUNTIME_SHA256,
            )
        )
        if not producer_path.is_file():
            failures.append("producer_bindings.current:file")
        else:
            failures.extend(
                _correction_file_binding_failures(
                    root,
                    producer_bindings.get("current"),
                    label="producer_bindings.current",
                    expected_path=producer_path,
                    expected_size=producer_path.stat().st_size,
                    expected_sha256=_sha256_file(producer_path),
                )
            )
    return sorted(set(failures))


def _validate_fetch_manifest_cap_contract(
    root: Path,
    connection: sqlite3.Connection,
    run: Mapping[str, Any],
    checks: list[Check],
    metrics: dict[str, Any],
    *,
    paths: Mapping[str, Path],
    artifact_path: Path,
    stage_report_path: Path,
    manifest_path: Path,
) -> None:
    """Require cap 96, with one exact overlay exception for the frozen 884 artifact."""

    failures: list[str] = []
    mode = "invalid"
    correction_sha256: str | None = None
    fetch_manifest_path = paths.get("fetch_manifest", _resolve(root, DEFAULT_FETCH_MANIFEST))
    if not fetch_manifest_path.is_file():
        failures.append("fetch_manifest:missing")
        fetch_manifest: Any = {}
    else:
        try:
            fetch_manifest = _load_json(fetch_manifest_path)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            failures.append("fetch_manifest:invalid_json")
            fetch_manifest = {}
    if not isinstance(fetch_manifest, Mapping):
        failures.append("fetch_manifest:mapping")
        fetch_manifest = {}

    run_id = str(run["run_id"])
    actual_counts = {
        "asset_occurrences": int(run.get("asset_occurrence_count", -1)),
        "logical_http_fetches": int(run.get("logical_http_fetches", -1)),
        "wire_http_requests": int(run.get("wire_http_requests", -1)),
        "redirects": 0,
        "retries": 0,
    }
    expected_manifest_counts = {
        "asset_occurrence_count": actual_counts["asset_occurrences"],
        "logical_http_fetches": actual_counts["logical_http_fetches"],
        "wire_http_requests": actual_counts["wire_http_requests"],
    }
    if not _json_exact(fetch_manifest.get("run_id"), run_id):
        failures.append("fetch_manifest:run_id")
    for key, expected in expected_manifest_counts.items():
        if not _json_exact(fetch_manifest.get(key), expected):
            failures.append(f"fetch_manifest:{key}")

    recorded_caps = fetch_manifest.get("hard_caps")
    if _json_exact(recorded_caps, FETCH_CAPS):
        mode = "native-96"
    elif _json_exact(recorded_caps, LEGACY_WP25_FETCH_CAPS):
        mode = "legacy-884-corrected"
        correction_path = _resolve(root, DEFAULT_FETCH_CAP_CORRECTION)
        terminals = _rows(
            connection,
            "SELECT * FROM probe_terminal WHERE run_id=?",
            (run_id,),
        )
        if len(terminals) != 1:
            failures.append("probe_terminal:row_count")
        else:
            failures.extend(
                _fetch_cap_correction_failures(
                    root,
                    correction_path=correction_path,
                    fetch_manifest_path=fetch_manifest_path,
                    fetch_manifest=fetch_manifest,
                    run_id=run_id,
                    actual_counts=actual_counts,
                    artifact_path=artifact_path,
                    stage_report_path=stage_report_path,
                    manifest_path=manifest_path,
                    terminal=terminals[0],
                )
            )
        if correction_path.is_file():
            correction_sha256 = _sha256_file(correction_path)
    else:
        failures.append("fetch_manifest:hard_caps")

    _check(
        checks,
        "fetch_manifest_hard_cap_contract_and_correction",
        [],
        sorted(set(failures)),
    )
    metrics["fetch_manifest_cap_contract"] = {
        "mode": mode,
        "recorded_hard_caps": recorded_caps,
        "enforced_hard_caps": dict(FETCH_CAPS),
        "actual_counts": actual_counts,
        "correction_sha256": correction_sha256,
    }


def _validate_step0(root: Path, checks: list[Check], metrics: dict[str, Any]) -> None:
    try:
        observed = recompute_step0(root)
    except (OSError, sqlite3.Error, json.JSONDecodeError, KeyError, ValueError) as exc:
        _check(checks, "step0_recomputed", "valid", f"{type(exc).__name__}:{exc}")
        return
    _check(checks, "step0_recomputed", "valid", "valid")
    n100 = observed["n100"]
    wp25 = observed["wp25"]
    _check(
        checks,
        "step0_form_top_schema_exact",
        [sorted(FORM_TOP_KEYS), sorted(FORM_TOP_KEYS)],
        n100["form_top_key_sets"],
    )
    _check(
        checks,
        "step0_wp25_form_top_schema_exact",
        [sorted(FORM_TOP_KEYS), sorted(FORM_TOP_KEYS)],
        wp25["form_top_key_sets"],
    )
    _check(
        checks,
        "step0_form_item_schema_exact",
        [tuple(sorted(FORM_ITEM_KEYS))],
        n100["form_item_key_sets"],
    )
    _check(
        checks,
        "step0_wp25_form_item_schema_exact",
        [tuple(sorted(FORM_ITEM_KEYS))],
        wp25["form_item_key_sets"],
    )
    _check(
        checks,
        "step0_lock_schema_exact",
        {
            "top": [sorted(LOCK_TOP_KEYS), sorted(LOCK_TOP_KEYS)],
            "item": [tuple(sorted(LOCK_ITEM_KEYS))],
        },
        {
            "top": n100["lock_top_key_sets"],
            "item": n100["lock_item_key_sets"],
        },
    )
    _check(
        checks,
        "step0_wp25_lock_schema_exact",
        {
            "top": [sorted(LOCK_TOP_KEYS), sorted(LOCK_TOP_KEYS)],
            "item": [tuple(sorted(LOCK_ITEM_KEYS))],
        },
        {
            "top": wp25["lock_top_key_sets"],
            "item": wp25["lock_item_key_sets"],
        },
    )
    _check(
        checks,
        "step0_row_counts",
        {"n100": [210, 210, 210], "wp25": [442, 442, 442]},
        {
            "n100": [n100["form_rows"], n100["locked_rows"], n100["db_rows"]],
            "wp25": [wp25["form_rows"], wp25["locked_rows"], wp25["db_rows"]],
        },
    )
    _check(
        checks,
        "step0_no_axis_columns",
        {"n100": [], "wp25": []},
        {"n100": n100["axis_columns"], "wp25": wp25["axis_columns"]},
    )
    _check(
        checks,
        "step0_n100_all_seven_ordered_axes",
        {**{axis: 210 for axis in AXES}, "full_ordered": 210},
        {**n100["axis_counts"], "full_ordered": n100["full_ordered_axis_rows"]},
    )
    _check(
        checks,
        "step0_wp25_zero_axis_tags",
        {**{axis: 0 for axis in AXES}, "full_ordered": 0},
        {**wp25["axis_counts"], "full_ordered": wp25["full_ordered_axis_rows"]},
    )
    _check(
        checks,
        "step0_actual_label_usage",
        {
            "n100": {"different": 24, "insufficient": 30, "related": 2, "same": 154},
            "wp25": {"different": 72, "same": 370},
        },
        {"n100": n100["labels"], "wp25": wp25["labels"]},
    )
    _check(
        checks,
        "step0_locked_payload_matches_sqlite",
        {"n100": 210, "wp25": 442},
        {"n100": n100["lock_db_matches"], "wp25": wp25["lock_db_matches"]},
    )
    _check(
        checks,
        "step0_rationale_length_ranges",
        {"n100": [175, 311], "wp25": [51, 144]},
        {
            "n100": [n100["rationale_length_min"], n100["rationale_length_max"]],
            "wp25": [wp25["rationale_length_min"], wp25["rationale_length_max"]],
        },
    )
    metrics["step0"] = observed


def _expected_input_roles(phase: str, run_status: str | None) -> frozenset[str]:
    """Return the exact pin set materialized at the persisted run barrier."""

    prepared_statuses = {
        "fetching",
        "review_ready",
        "reviews_partial",
        "reviews_locked",
        "unblinded",
        "adjudication_locked",
        "pass",
        "fail",
        "failed",
    }
    has_prepare_inputs = phase == "final" or run_status in prepared_statuses
    return SAMPLE_INPUT_ROLES | (PREPARE_INPUT_ROLES if has_prepare_inputs else frozenset())


def _validate_input_pins(
    root: Path,
    connection: sqlite3.Connection,
    run_id: str,
    checks: list[Check],
    metrics: dict[str, Any],
    *,
    phase: str,
    verify_full_hashes: bool,
) -> None:
    rows = _rows(connection, "SELECT * FROM input_pins WHERE run_id=?", (run_id,))
    status_row = connection.execute(
        "SELECT status FROM runs WHERE run_id=?", (run_id,)
    ).fetchone()
    expected_roles = _expected_input_roles(
        phase, str(status_row[0]) if status_row is not None else None
    )
    by_path = {
        _resolve(root, str(row["path"])): row for row in rows if row.get("path")
    }
    failures: list[str] = []
    actual_roles = {str(row.get("role")) for row in rows}
    if actual_roles != set(expected_roles):
        failures.append(
            "role_set:missing="
            + ",".join(sorted(set(expected_roles) - actual_roles))
            + ":extra="
            + ",".join(sorted(actual_roles - set(expected_roles)))
        )
    for row in rows:
        role = str(row.get("role"))
        expected_path = EXPECTED_INPUT_PATHS.get(role)
        if expected_path is not None and _resolve(root, str(row.get("path", ""))) != _resolve(
            root, expected_path
        ):
            failures.append(f"{role}:path_binding")
    current: list[dict[str, Any]] = []
    for relative, (expected_size, expected_sha) in FIXED_FILE_PINS.items():
        path = _resolve(root, relative)
        row = by_path.get(path)
        if not path.is_file():
            failures.append(f"{relative.as_posix()}:missing")
            continue
        size = path.stat().st_size
        digest = _sha256_file(path) if verify_full_hashes else expected_sha
        current.append({"path": relative.as_posix(), "size_bytes": size, "sha256": digest})
        if size != expected_size:
            failures.append(f"{relative.as_posix()}:size")
        if verify_full_hashes and digest != expected_sha:
            failures.append(f"{relative.as_posix()}:sha")
        if row is None:
            failures.append(f"{relative.as_posix()}:pin_missing")
            continue
        if int(row.get("size_bytes", -1)) != expected_size:
            failures.append(f"{relative.as_posix()}:pin_size")
        if row.get("sha256_before") != expected_sha:
            failures.append(f"{relative.as_posix()}:pin_before")
        if row.get("sha256_after") not in (None, expected_sha):
            failures.append(f"{relative.as_posix()}:pin_after")
    for row in rows:
        path = _resolve(root, str(row.get("path", "")))
        if not path.is_file():
            failures.append(f"{row.get('role')}:current_missing")
            continue
        if path.stat().st_size != int(row.get("size_bytes", -1)):
            failures.append(f"{row.get('role')}:current_size")
        before = row.get("sha256_before")
        after = row.get("sha256_after")
        if not _is_sha256(before) or after not in (None, before):
            failures.append(f"{row.get('role')}:immutable_sha")
        if verify_full_hashes and _sha256_file(path) != before:
            failures.append(f"{row.get('role')}:current_sha")
    _check(checks, "input_pins_current_and_immutable", [], sorted(set(failures)))
    metrics["input_pins"] = current


def _validate_cache_pins(
    root: Path,
    connection: sqlite3.Connection,
    run_id: str,
    checks: list[Check],
    metrics: dict[str, Any],
    *,
    verify_full_hashes: bool,
) -> None:
    rows = _rows(connection, "SELECT * FROM cache_pins WHERE run_id=?", (run_id,))
    failures: list[str] = []
    write_roles = [row for row in rows if row.get("access_mode") == "write_only"]
    read_roles = [row for row in rows if row.get("access_mode") == "read_only"]
    if len(write_roles) != 1:
        failures.append("write_role_count")
    elif _resolve(root, str(write_roles[0]["path"])) != _resolve(root, DEFAULT_OUTPUT_CACHE):
        failures.append("write_role_path")
    for row in rows:
        role = str(row.get("role"))
        mode = str(row.get("access_mode"))
        if mode not in {"read_only", "write_only"}:
            failures.append(f"{role}:access_mode")
            continue
        before = (
            int(row.get("file_count_before", -1)),
            int(row.get("total_bytes_before", -1)),
            str(row.get("tree_sha256_before", "")),
        )
        after = (
            int(row.get("file_count_after", -1)),
            int(row.get("total_bytes_after", -1)),
            str(row.get("tree_sha256_after", "")),
        )
        if not _is_sha256(before[2]) or not _is_sha256(after[2]):
            failures.append(f"{role}:tree_sha")
        if mode == "read_only" and before != after:
            failures.append(f"{role}:read_tree_changed")
        if mode == "write_only" and before[:2] != (0, 0):
            failures.append(f"{role}:write_tree_not_empty")
        if verify_full_hashes:
            try:
                current = _cache_tree(_resolve(root, str(row["path"])))
            except (OSError, ValueError) as exc:
                failures.append(f"{role}:tree:{type(exc).__name__}")
            else:
                if current != after:
                    failures.append(f"{role}:current_tree")
    if not read_roles:
        failures.append("read_roles_missing")
    _check(checks, "cache_pins_access_and_tree_immutability", [], sorted(set(failures)))
    metrics["cache_pins"] = [
        {
            "role": row.get("role"),
            "access_mode": row.get("access_mode"),
            "before": [
                row.get("file_count_before"),
                row.get("total_bytes_before"),
                row.get("tree_sha256_before"),
            ],
            "after": [
                row.get("file_count_after"),
                row.get("total_bytes_after"),
                row.get("tree_sha256_after"),
            ],
        }
        for row in rows
    ]


def _pre_reveal_db_leaks(
    connection: sqlite3.Connection,
    *,
    real_candidate_ids: set[str],
) -> list[str]:
    """Scan tables materialized before adjudication lock for scrub failures."""

    post_reveal_tables = {
        "probe_reveals",
        "probe_scores",
        "probe_terminal",
        "final_payloads",
    }
    aggregate_tables = {"population_frames", "gate_contract", "form_comparison"}
    forbidden_values = {
        "merge_candidate",
        "related_but_separate",
        "fanout_review_required",
        "phash_veto_review_required",
        "anchor_missing",
    }
    leaks: list[str] = []
    for table in sorted(_tables(connection) - post_reveal_tables):
        columns = _columns(connection, table)
        rows = _rows(connection, f"SELECT * FROM {table}")
        for index, row in enumerate(rows):
            serialized = _canonical_json(row)
            if any(candidate_id in serialized for candidate_id in real_candidate_ids):
                leaks.append(f"{table}[{index}]:real_candidate_id")
            for token in forbidden_values:
                if token in serialized:
                    leaks.append(f"{table}[{index}]:forbidden_value:{token}")
            if table not in aggregate_tables and "sample_id" in columns:
                for group in ("P1", "P2", "P3", "D"):
                    if any(value == group for value in row.values()):
                        leaks.append(f"{table}[{index}]:sample_group:{group}")
            for column in columns:
                folded = column.casefold()
                value = row.get(column)
                if (
                    ("gold" in folded and "commitment" not in folded)
                    or "proposal" in folded
                    or "score" in folded
                    or "policy_flag" in folded
                ) and value not in (None, "", "[]", "{}", 0):
                    leaks.append(f"{table}[{index}]:{column}")

    if "sample_pairs" in _tables(connection):
        for row in _rows(connection, "SELECT candidate_id,stratum FROM sample_pairs"):
            candidate = str(row.get("candidate_id", ""))
            if candidate in real_candidate_ids or not re.fullmatch(r"ctl_[0-9a-f]{24,64}", candidate):
                leaks.append("sample_pairs:opaque_control_id")
            if row.get("stratum") != "probe":
                leaks.append("sample_pairs:neutral_stratum")
    if "candidate_snapshots" in _tables(connection):
        allowed_candidate_keys = {
            "left_source",
            "left_source_building_id",
            "right_source",
            "right_source_building_id",
        }
        for row in _rows(connection, "SELECT * FROM candidate_snapshots"):
            try:
                candidate = json.loads(str(row.get("candidate_json", "")))
            except json.JSONDecodeError:
                candidate = None
            if not isinstance(candidate, Mapping) or set(candidate) != allowed_candidate_keys:
                leaks.append("candidate_snapshots:candidate_json_shape")
            for column in (
                "origins_json",
                "families_json",
                "conflicts_json",
                "policy_flags_json",
                "transition_json",
            ):
                if column in row and _json_value(row[column]) not in ({}, [], None):
                    leaks.append(f"candidate_snapshots:{column}")
    return sorted(set(leaks))


def _sample_manifest_leaks(
    path: Path,
    *,
    real_candidate_ids: set[str],
) -> list[str]:
    """Reject per-row group/gold/candidate disclosures while allowing aggregates."""

    try:
        payload = _load_json(path)
    except (OSError, json.JSONDecodeError):
        return ["unreadable_json"]
    samples = payload.get("samples") if isinstance(payload, Mapping) else None
    if not isinstance(samples, list):
        return ["$.samples"]
    leaks: list[str] = []
    forbidden_parts = ("candidate", "group", "gold_label", "proposal", "score", "flag")
    for index, sample in enumerate(samples):
        if not isinstance(sample, Mapping):
            leaks.append(f"$.samples[{index}]")
            continue
        for key, value in sample.items():
            folded = str(key).casefold()
            if any(part in folded for part in forbidden_parts) and "commitment" not in folded:
                leaks.append(f"$.samples[{index}].{key}")
            if isinstance(value, str) and value in {"P1", "P2", "P3", "D"}:
                leaks.append(f"$.samples[{index}]:group_value")
    serialized = path.read_text(encoding="utf-8")
    if any(candidate_id in serialized for candidate_id in real_candidate_ids):
        leaks.append("real_candidate_value")
    return sorted(set(leaks))


def _png_structure_leaks(path: Path) -> list[str]:
    """Validate a sheet as metadata-free RGB PNG, not as arbitrary bytes.

    Searching compressed IDAT bytes for short strings produces false positives:
    those bytes are a zlib stream, not text.  The pinned renderer emits only
    IHDR/IDAT/IEND.  This parser verifies that exact structure, every CRC, and
    the decoded scanline shape, thereby rejecting all text/private metadata
    channels without interpreting compressed pixels as plaintext.
    """

    try:
        raw = path.read_bytes()
    except OSError:
        return ["unreadable"]
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return ["png_signature"]
    position = 8
    chunks: list[tuple[bytes, bytes]] = []
    failures: list[str] = []
    while position < len(raw):
        if position + 12 > len(raw):
            failures.append("truncated_chunk_header")
            break
        length = struct.unpack(">I", raw[position : position + 4])[0]
        chunk_end = position + 12 + length
        if chunk_end > len(raw):
            failures.append("truncated_chunk")
            break
        chunk_type = raw[position + 4 : position + 8]
        data = raw[position + 8 : position + 8 + length]
        expected_crc = struct.unpack(">I", raw[position + 8 + length : chunk_end])[0]
        actual_crc = binascii.crc32(chunk_type + data) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            failures.append("crc:" + chunk_type.decode("ascii", errors="replace"))
        chunks.append((chunk_type, data))
        position = chunk_end
        if chunk_type == b"IEND":
            break
    if position != len(raw):
        failures.append("trailing_bytes")
    chunk_types = [chunk_type for chunk_type, _ in chunks]
    if not chunk_types or chunk_types[0] != b"IHDR" or chunk_types[-1:] != [b"IEND"]:
        failures.append("chunk_order")
    unexpected = sorted(
        {
            chunk_type.decode("ascii", errors="replace")
            for chunk_type in chunk_types
            if chunk_type not in {b"IHDR", b"IDAT", b"IEND"}
        }
    )
    failures.extend("unexpected_chunk:" + value for value in unexpected)
    if chunk_types.count(b"IHDR") != 1 or chunk_types.count(b"IEND") != 1:
        failures.append("singleton_chunks")
    idat_positions = [index for index, value in enumerate(chunk_types) if value == b"IDAT"]
    if not idat_positions or idat_positions != list(
        range(idat_positions[0], idat_positions[-1] + 1)
    ):
        failures.append("idat_order")
    ihdr = chunks[0][1] if chunks and chunks[0][0] == b"IHDR" else b""
    if len(ihdr) != 13:
        failures.append("ihdr")
    else:
        width, height, bit_depth, color_type, compression, filtering, interlace = (
            struct.unpack(">IIBBBBB", ihdr)
        )
        if not (
            width > 0
            and height > 0
            and bit_depth == 8
            and color_type == 2
            and compression == 0
            and filtering == 0
            and interlace == 0
        ):
            failures.append("ihdr_contract")
        else:
            try:
                pixels = zlib.decompress(
                    b"".join(data for chunk_type, data in chunks if chunk_type == b"IDAT")
                )
            except zlib.error:
                failures.append("idat_zlib")
            else:
                stride = 1 + width * 3
                if len(pixels) != height * stride:
                    failures.append("scanline_size")
                elif any(pixels[offset] > 4 for offset in range(0, len(pixels), stride)):
                    failures.append("scanline_filter")
    return sorted(set(failures))


def _sheet_structured_text_leaks(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    sample_id: str,
    opaque_review_id: str,
    tokens: Iterable[str],
) -> list[str]:
    """Scan only strings that the pinned renderer can place on the sheet."""

    row = connection.execute(
        "SELECT left_metadata_json,right_metadata_json FROM candidate_snapshots "
        "WHERE run_id=? AND sample_id=?",
        (run_id, sample_id),
    ).fetchone()
    if row is None:
        return ["snapshot_missing"]
    displayed: list[str] = [opaque_review_id]
    for column in ("left_metadata_json", "right_metadata_json"):
        try:
            metadata = json.loads(str(row[column]))
        except (json.JSONDecodeError, TypeError):
            return [column + ":json"]
        if not isinstance(metadata, Mapping):
            return [column + ":shape"]
        aliases = metadata.get("aliases_json", metadata.get("aliases", []))
        firms = metadata.get("firms_json", metadata.get("firms", []))
        for name, value in (("aliases", aliases), ("firms", firms)):
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    return [column + ":" + name + "_json"]
            if value is not None and not isinstance(value, list):
                return [column + ":" + name + "_shape"]
            displayed.extend(str(item) for item in (value or []))
        displayed.extend(
            str(value)
            for value in (
                metadata.get("source", "unknown"),
                metadata.get("name", "unknown"),
                metadata.get("country"),
                metadata.get("locality"),
                metadata.get("completion_year_min", metadata.get("year_min")),
                metadata.get("completion_year_max", metadata.get("year_max")),
            )
            if value is not None
        )
    joined = "\n".join(displayed)
    return sorted({token for token in tokens if len(token) >= 2 and token in joined})


def _validate_artifact_provenance(
    root: Path,
    connection: sqlite3.Connection,
    run_id: str,
    checks: list[Check],
    metrics: dict[str, Any],
    *,
    phase: str,
    verify_full_hashes: bool,
) -> dict[str, Path]:
    paths = _rows(connection, "SELECT * FROM artifact_paths WHERE run_id=?", (run_id,))
    bindings = _rows(
        connection, "SELECT * FROM artifact_bindings WHERE run_id=?", (run_id,)
    )
    path_by_role = {str(row["role"]): row for row in paths}
    binding_by_role = {str(row["role"]): row for row in bindings}
    required_paths = {"sample_manifest"} if phase == "sample" else set(FINAL_PATH_STAGES)
    required_bindings = (
        {"sample_manifest"} if phase == "sample" else set(FINAL_ARTIFACT_STAGES)
    )
    failures: list[str] = []
    resolved: dict[str, Path] = {}
    for role in sorted(required_paths):
        row = path_by_role.get(role)
        if row is None:
            failures.append(f"{role}:path_missing")
            continue
        if str(row.get("stage")) != FINAL_PATH_STAGES[role]:
            failures.append(f"{role}:stage")
        path = _resolve(root, str(row.get("path")))
        resolved[role] = path
        if role == "sheets_root":
            if not path.is_dir():
                failures.append(f"{role}:directory")
        elif not path.is_file():
            failures.append(f"{role}:file")
        expected_default = _resolve(root, EXPECTED_DEFAULT_PATHS[role])
        if path != expected_default:
            failures.append(f"{role}:non_default_path")
    for role in sorted(required_bindings):
        row = binding_by_role.get(role)
        if row is None:
            failures.append(f"{role}:binding_missing")
            continue
        path_row = path_by_role.get(role)
        if path_row is None or row.get("path") != path_row.get("path"):
            failures.append(f"{role}:path_binding")
        if str(row.get("stage")) != FINAL_ARTIFACT_STAGES[role]:
            failures.append(f"{role}:binding_stage")
        path = _resolve(root, str(row.get("path")))
        if path.is_file():
            if path.stat().st_size != int(row.get("size_bytes", -1)):
                failures.append(f"{role}:bytes")
            if not _is_sha256(row.get("sha256")):
                failures.append(f"{role}:sha_shape")
            elif verify_full_hashes and _sha256_file(path) != row.get("sha256"):
                failures.append(f"{role}:sha")
    if phase == "final":
        if set(path_by_role) != set(FINAL_PATH_STAGES):
            failures.append("path_role_set")
        if set(binding_by_role) != set(FINAL_ARTIFACT_STAGES):
            failures.append("binding_role_set")
    _check(checks, "artifact_paths_bindings_and_hashes", [], sorted(set(failures)))
    metrics["artifact_provenance"] = {
        "paths": sorted(path_by_role),
        "bindings": sorted(binding_by_role),
    }
    return resolved


def _validate_review_barriers(
    root: Path,
    connection: sqlite3.Connection,
    run: Mapping[str, Any],
    checks: list[Check],
    metrics: dict[str, Any],
    *,
    paths: Mapping[str, Path],
    expected_controls: Mapping[str, Any],
) -> None:
    run_id = str(run["run_id"])
    review_items = _rows(
        connection,
        "SELECT * FROM review_items WHERE run_id=? ORDER BY lane,display_order",
        (run_id,),
    )
    reviews = _rows(
        connection,
        "SELECT * FROM blind_reviews WHERE run_id=? ORDER BY lane,opaque_review_id",
        (run_id,),
    )
    locks = _rows(connection, "SELECT * FROM review_locks WHERE run_id=?", (run_id,))
    lock_payloads = _rows(
        connection, "SELECT * FROM review_lock_payloads WHERE run_id=?", (run_id,)
    )
    failures: list[str] = []
    if Counter(str(row["lane"]) for row in review_items) != {"r1": 24, "r2": 24}:
        failures.append("review_item_counts")
    if Counter(str(row["lane"]) for row in reviews) != {"r1": 24, "r2": 24}:
        failures.append("review_counts")
    if len(locks) != 2 or {str(row["lane"]) for row in locks} != {"r1", "r2"}:
        failures.append("review_lock_count")
    actors: dict[str, set[str]] = {"r1": set(), "r2": set()}
    review_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in reviews:
        lane = str(row["lane"])
        review_by_key[(lane, str(row["opaque_review_id"]))] = row
        actors.setdefault(lane, set()).add(str(row.get("reviewer", "")).strip().casefold())
        if row.get("label") not in REVIEW_LABELS:
            failures.append(f"{lane}:label")
        if not str(row.get("rationale", "")).strip():
            failures.append(f"{lane}:rationale")
        if not str(row.get("hard_conflict_observed", "")).strip():
            failures.append(f"{lane}:hard_conflict")
    if any(len(values) != 1 or "" in values for values in actors.values()):
        failures.append("review_actor_per_lane")
    if actors["r1"] & actors["r2"]:
        failures.append("review_actor_not_distinct")
    item_by_key = {
        (str(row["lane"]), str(row["opaque_review_id"])): row for row in review_items
    }
    if set(item_by_key) != set(review_by_key):
        failures.append("review_item_review_bijection")
    for item in review_items:
        sheet = _resolve(root, str(item["sheet_path"]))
        if not sheet.is_file() or _sha256_file(sheet) != str(item["sheet_sha256"]):
            failures.append(f"{item['lane']}:{item['opaque_review_id']}:sheet")

    payload_by_lane = {str(row["lane"]): row for row in lock_payloads}
    lock_times: list[datetime] = []
    for lock in locks:
        lane = str(lock["lane"])
        if int(lock.get("row_count", -1)) != TOTAL_SAMPLE:
            failures.append(f"{lane}:lock_rows")
        completed = _resolve(root, str(lock["completed_form_path"]))
        locked = _resolve(root, str(lock["locked_output_path"]))
        expected_completed = _resolve(
            root, DEFAULT_R1_COMPLETED if lane == "r1" else DEFAULT_R2_COMPLETED
        )
        expected_locked = _resolve(root, DEFAULT_R1_LOCKED if lane == "r1" else DEFAULT_R2_LOCKED)
        if completed != expected_completed or locked != expected_locked:
            failures.append(f"{lane}:default_paths")
        for path, sha_key in (
            (completed, "completed_form_sha256"),
            (locked, "locked_output_sha256"),
        ):
            if not path.is_file() or _sha256_file(path) != str(lock.get(sha_key)):
                failures.append(f"{lane}:{sha_key}")
        stored = payload_by_lane.get(lane)
        if stored is None:
            failures.append(f"{lane}:recovery_payload")
        else:
            try:
                payload = json.loads(str(stored["payload_json"]))
                locked_payload = _load_json(locked)
            except (OSError, json.JSONDecodeError):
                failures.append(f"{lane}:payload_json")
            else:
                if payload != locked_payload:
                    failures.append(f"{lane}:payload_file")
                if payload.get("row_count") != TOTAL_SAMPLE:
                    failures.append(f"{lane}:payload_rows")
                ledger = payload.get("items")
                if not isinstance(ledger, list) or len(ledger) != TOTAL_SAMPLE:
                    failures.append(f"{lane}:ledger_rows")
                else:
                    if _record_sha(ledger) != lock.get("ordered_ledger_sha256"):
                        failures.append(f"{lane}:ordered_ledger_sha")
        try:
            lock_times.append(_iso(str(lock["locked_at"])))
        except ValueError:
            failures.append(f"{lane}:locked_at")
    _check(checks, "dual_review_ledgers_and_recovery", [], sorted(set(failures)))

    unblind_rows = _rows(
        connection, "SELECT * FROM unblind_artifacts WHERE run_id=?", (run_id,)
    )
    unblind_payloads = _rows(
        connection, "SELECT * FROM unblind_payloads WHERE run_id=?", (run_id,)
    )
    adjudication_items = _rows(
        connection,
        "SELECT * FROM probe_adjudication_items WHERE run_id=? ORDER BY display_order",
        (run_id,),
    )
    adjudications = _rows(
        connection, "SELECT * FROM probe_adjudications WHERE run_id=?", (run_id,)
    )
    adjudication_locks = _rows(
        connection, "SELECT * FROM probe_adjudication_lock WHERE run_id=?", (run_id,)
    )
    adjudication_payloads = _rows(
        connection,
        "SELECT * FROM probe_adjudication_lock_payload WHERE run_id=?",
        (run_id,),
    )
    barrier_failures: list[str] = []
    if not (
        len(unblind_rows)
        == len(unblind_payloads)
        == len(adjudication_locks)
        == len(adjudication_payloads)
        == 1
    ):
        barrier_failures.append("barrier_singletons")
    if len(adjudication_items) != TOTAL_SAMPLE or len(adjudications) != TOTAL_SAMPLE:
        barrier_failures.append("adjudication_rows")
    if unblind_rows:
        unblind = unblind_rows[0]
        try:
            created = _iso(str(unblind["created_at"]))
            if len(lock_times) != 2 or max(lock_times) > created:
                barrier_failures.append("unblind_before_dual_lock")
        except ValueError:
            barrier_failures.append("unblind_time")
        for key, sha_key in (("packet_path", "packet_sha256"), ("form_path", "form_sha256")):
            path = _resolve(root, str(unblind[key]))
            if not path.is_file() or _sha256_file(path) != str(unblind[sha_key]):
                barrier_failures.append(f"unblind:{key}")
    if unblind_payloads and unblind_rows:
        stored = unblind_payloads[0]
        try:
            if json.loads(str(stored["packet_json"])) != _load_json(
                _resolve(root, str(unblind_rows[0]["packet_path"]))
            ):
                barrier_failures.append("unblind_packet_recovery")
            if json.loads(str(stored["form_json"])) != _load_json(
                _resolve(root, str(unblind_rows[0]["form_path"]))
            ):
                barrier_failures.append("unblind_form_recovery")
        except (OSError, json.JSONDecodeError):
            barrier_failures.append("unblind_recovery_json")
    reviewer_actor_values = actors["r1"] | actors["r2"]
    adjudicator_values = {
        str(row.get("adjudicator", "")).strip().casefold() for row in adjudications
    }
    if len(adjudicator_values) != 1 or "" in adjudicator_values:
        barrier_failures.append("adjudicator_actor")
    if adjudicator_values & reviewer_actor_values:
        barrier_failures.append("adjudicator_not_distinct")
    for row in adjudications:
        if row.get("r1_label") not in REVIEW_LABELS or row.get("r2_label") not in REVIEW_LABELS:
            barrier_failures.append(f"{row.get('opaque_probe_id')}:review_labels")
        if row.get("adjudicator_label") not in REVIEW_LABELS or row.get("final_probe_label") not in REVIEW_LABELS:
            barrier_failures.append(f"{row.get('opaque_probe_id')}:adjudicator_label")
        unresolved = bool(row.get("unresolved_disagreement"))
        expected_final = "insufficient" if unresolved else row.get("adjudicator_label")
        if row.get("final_probe_label") != expected_final:
            barrier_failures.append(f"{row.get('opaque_probe_id')}:final_label")
        if not str(row.get("rationale", "")).strip():
            barrier_failures.append(f"{row.get('opaque_probe_id')}:rationale")
    adjudication_lock_time: datetime | None = None
    if adjudication_locks:
        lock = adjudication_locks[0]
        if int(lock.get("row_count", -1)) != TOTAL_SAMPLE:
            barrier_failures.append("adjudication_lock_rows")
        completed = _resolve(root, str(lock["completed_form_path"]))
        locked = _resolve(root, str(lock["locked_output_path"]))
        if completed != _resolve(root, DEFAULT_ADJUDICATION_COMPLETED) or locked != _resolve(
            root, DEFAULT_ADJUDICATION_LOCKED
        ):
            barrier_failures.append("adjudication_default_paths")
        for path, sha_key in (
            (completed, "completed_form_sha256"),
            (locked, "locked_output_sha256"),
        ):
            if not path.is_file() or _sha256_file(path) != str(lock.get(sha_key)):
                barrier_failures.append(f"adjudication:{sha_key}")
        try:
            adjudication_lock_time = _iso(str(lock["locked_at"]))
            if unblind_rows and adjudication_lock_time < _iso(str(unblind_rows[0]["created_at"])):
                barrier_failures.append("adjudication_before_unblind")
        except ValueError:
            barrier_failures.append("adjudication_lock_time")
        if adjudication_payloads:
            try:
                payload = json.loads(str(adjudication_payloads[0]["payload_json"]))
                if payload != _load_json(locked):
                    barrier_failures.append("adjudication_recovery_payload")
                if _record_sha(payload.get("items", [])) != lock.get("ordered_ledger_sha256"):
                    barrier_failures.append("adjudication_ledger_sha")
            except (OSError, json.JSONDecodeError):
                barrier_failures.append("adjudication_payload_json")
    _check(checks, "lock_before_unblind_before_adjudication_lock", [], sorted(set(barrier_failures)))

    real_candidates = set(expected_controls["candidate_ids"])
    forbidden_tokens = set(real_candidates) | {
        "P1",
        "P2",
        "P3",
        "merge_candidate",
        "fanout_review_required",
        "phash_veto_review_required",
        "anchor_missing",
    }
    leak_failures: list[str] = []
    json_roles = (
        "r1_blank_form",
        "r1_completed_form",
        "r1_locked",
        "r2_blank_form",
        "r2_completed_form",
        "r2_locked",
        "unblind_packet",
        "adjudication_blank_form",
        "adjudication_completed_form",
        "adjudication_locked",
    )
    for role in json_roles:
        path = paths.get(role)
        if path is None:
            leak_failures.append(f"{role}:missing")
            continue
        leaks = _blind_json_leaks(path, real_candidates)
        if leaks:
            leak_failures.append(f"{role}:" + ",".join(leaks))
    for item in review_items:
        path = _resolve(root, str(item["sheet_path"]))
        leaks = _png_structure_leaks(path)
        leaks.extend(
            _sheet_structured_text_leaks(
                connection,
                run_id=run_id,
                sample_id=str(item["sample_id"]),
                opaque_review_id=str(item["opaque_review_id"]),
                tokens=forbidden_tokens,
            )
        )
        if leaks:
            leak_failures.append(
                f"sheet:{item['opaque_review_id']}:" + ",".join(sorted(set(leaks)))
            )
    _check(checks, "blind_deliverables_hidden_fields_zero", [], sorted(leak_failures))
    metrics["reviews"] = {
        "review_items": len(review_items),
        "review_rows": len(reviews),
        "review_locks": len(locks),
        "adjudication_rows": len(adjudications),
        "reviewer_disagreements": sum(
            row.get("r1_label") != row.get("r2_label") for row in adjudications
        ),
        "unresolved": sum(bool(row.get("unresolved_disagreement")) for row in adjudications),
        "adjudication_lock_time": (
            adjudication_lock_time.isoformat() if adjudication_lock_time else None
        ),
    }


def _validate_sample(
    root: Path,
    connection: sqlite3.Connection,
    run: Mapping[str, Any],
    checks: list[Check],
    metrics: dict[str, Any],
    *,
    sample_manifest_path: Path,
    expected: Mapping[str, Any],
) -> None:
    run_id = str(run["run_id"])
    _check(checks, "sample_version", SAMPLE_VERSION, run.get("sample_version"))
    _check(checks, "review_version", REVIEW_VERSION, run.get("review_version"))
    _check(
        checks,
        "adjudication_version",
        ADJUDICATION_VERSION,
        run.get("adjudication_version"),
    )
    _check(checks, "gate_version", GATE_VERSION, run.get("gate_version"))
    _check(checks, "sample_seed", SAMPLE_SEED, run.get("seed"))
    _check(checks, "sample_count", TOTAL_SAMPLE, int(run.get("sample_count", -1)))
    _check(
        checks,
        "gate_status_lifecycle",
        _expected_persisted_gate_status(str(run.get("status"))),
        run.get("gate_status"),
    )

    comparison_failures: list[str] = []
    step0_metrics = metrics.get("step0")
    expected_comparison = (
        expected_form_comparison(step0_metrics)
        if isinstance(step0_metrics, Mapping) and {"n100", "wp25"} <= set(step0_metrics)
        else {}
    )
    comparison_rows = _rows(
        connection, "SELECT * FROM form_comparison WHERE run_id=?", (run_id,)
    )
    comparison_by_key = {str(row["comparison_key"]): row for row in comparison_rows}
    if not expected_comparison or set(comparison_by_key) != set(expected_comparison):
        comparison_failures.append("key_set")
    for key, expected_row in expected_comparison.items():
        row = comparison_by_key.get(key)
        if row is None:
            continue
        if _json_value(row.get("n100_value_json")) != expected_row["n100"]:
            comparison_failures.append(f"{key}:n100")
        if _json_value(row.get("wp25_value_json")) != expected_row["wp25"]:
            comparison_failures.append(f"{key}:wp25")
        if row.get("finding") != expected_row["finding"]:
            comparison_failures.append(f"{key}:finding")
    _check(checks, "step0_sidecar_rows_match_independent_replay", [], comparison_failures)

    expected_counts = {"P1": 3, "P2": 4, "P3": 2, "D": 15}
    _check(checks, "control_universe_counts", expected_counts, expected["counts"])
    _check(checks, "d_universe_count", 66, expected["d_universe_count"])
    _check(
        checks,
        "control_gold_composition",
        {
            "P1": {"insufficient": 2, "related": 1},
            "P2": {"insufficient": 3, "related": 1},
            "P3": {"insufficient": 2},
            "D": {"same": 15},
        },
        expected["gold_counts"],
    )
    _check(
        checks,
        "controls_global_unique_and_wp25_disjoint",
        {"candidate_unique": True, "sample_unique": True, "overlap": []},
        {
            "candidate_unique": expected["candidate_unique"],
            "sample_unique": expected["sample_unique"],
            "overlap": expected["wp25_overlap"],
        },
    )

    pairs = _rows(
        connection, "SELECT * FROM sample_pairs WHERE run_id=? ORDER BY sample_rank", (run_id,)
    )
    by_sample = {str(row["sample_id"]): row for row in pairs}
    persisted_expected = persisted_control_rows(run_id, expected)
    expected_by_sample = {str(row["sample_id"]): row for row in persisted_expected}
    sample_failures: list[str] = []
    if len(pairs) != TOTAL_SAMPLE or set(by_sample) != set(expected_by_sample):
        sample_failures.append("exact_sample_set")
    if [int(row.get("sample_rank", -1)) for row in pairs] != list(range(1, TOTAL_SAMPLE + 1)):
        sample_failures.append("sample_rank_sequence")
    for sample_id, row in by_sample.items():
        expected_row = expected_by_sample.get(sample_id)
        if expected_row is None:
            continue
        if row.get("seed_rank_sha256") != expected_row["seed_rank_sha256"]:
            sample_failures.append(f"{sample_id}:seed_rank")
        if row.get("stratum") != "probe":
            sample_failures.append(f"{sample_id}:stratum")
        opaque = str(row.get("candidate_id", ""))
        if opaque != expected_row["opaque_control_id"]:
            sample_failures.append(f"{sample_id}:opaque_control")
    expected_order = [str(row["sample_id"]) for row in persisted_expected]
    if [str(row["sample_id"]) for row in pairs] != expected_order:
        sample_failures.append("seeded_shuffle_order")
    _check(checks, "sample_exact_membership_rank_and_no_replacement", [], sorted(sample_failures))

    commitments = _rows(
        connection, "SELECT * FROM control_commitments WHERE run_id=?", (run_id,)
    )
    commitment_by_sample = {str(row["sample_id"]): row for row in commitments}
    commitment_failures: list[str] = []
    if set(commitment_by_sample) != set(expected_by_sample):
        commitment_failures.append("commitment_set")
    for sample_id, expected_row in expected_by_sample.items():
        stored = commitment_by_sample.get(sample_id)
        if stored is None:
            continue
        if stored.get("gold_commitment_sha256") != control_commitment(expected_row):
            commitment_failures.append(f"{sample_id}:commitment")
        if stored.get("source_record_commitment_sha256") != source_record_commitment(
            expected_row
        ):
            commitment_failures.append(f"{sample_id}:source_record")
    _check(checks, "gold_commitments_exact_without_plaintext", [], sorted(commitment_failures))

    contract_rows = _rows(connection, "SELECT * FROM gate_contract WHERE run_id=?", (run_id,))
    contract_failures: list[str] = []
    expected_contract = {
        "p1_min_non_same": 2,
        "p23_min_non_same": 3,
        "d_min_same": 13,
        "p1_n": 3,
        "p23_n": 6,
        "d_n": 15,
    }
    contract_payload = {
        **expected_contract,
        "gate_version": GATE_VERSION,
        "predicate": (
            "P1 non-same >= 2/3 AND P2+P3 non-same >= 3/6 "
            "AND D same >= 13/15"
        ),
    }
    if len(contract_rows) != 1:
        contract_failures.append("row_count")
    else:
        row = contract_rows[0]
        actual_contract = {key: int(row.get(key, -1)) for key in expected_contract}
        if actual_contract != expected_contract:
            contract_failures.append("thresholds")
        if row.get("contract_sha256") != _record_sha(contract_payload):
            contract_failures.append("contract_sha")
        try:
            _iso(str(row["declared_at"]))
        except ValueError:
            contract_failures.append("declared_at")
    _check(checks, "gate_contract_predeclared_exact", [], contract_failures)

    logical = _logical_sha(connection, run_id)
    _check(checks, "sample_logical_sha_run", logical, run.get("sample_logical_sha256"))
    try:
        manifest = _load_json(sample_manifest_path)
    except (OSError, json.JSONDecodeError) as exc:
        _check(checks, "sample_manifest_payload", "valid", f"{type(exc).__name__}:{exc}")
        return
    _check(checks, "sample_manifest_payload", "valid", "valid")
    _check(checks, "sample_manifest_run_id", run_id, manifest.get("run_id"))
    _check(
        checks,
        "sample_manifest_logical_sha",
        logical,
        manifest.get("sample_logical_sha256"),
    )
    manifest_contract_failures: list[str] = []
    expected_manifest_scalars = {
        "status": "PASS",
        "seed": SAMPLE_SEED,
        "counts": {
            "total": TOTAL_SAMPLE,
            "groups": dict(GROUP_QUOTAS),
            "post_freeze_replacements": 0,
        },
        "network_requests": 0,
    }
    for key, value in expected_manifest_scalars.items():
        if manifest.get(key) != value:
            manifest_contract_failures.append(key)
    versions = manifest.get("versions")
    if not isinstance(versions, Mapping) or {
        "sample": versions.get("sample"),
        "review": versions.get("review"),
        "gate": versions.get("gate"),
    } != {"sample": SAMPLE_VERSION, "review": REVIEW_VERSION, "gate": GATE_VERSION}:
        manifest_contract_failures.append("versions")
    sidecar = manifest.get("sidecar")
    if not isinstance(sidecar, Mapping):
        manifest_contract_failures.append("sidecar")
    else:
        if _resolve(root, str(sidecar.get("path", ""))) != _resolve(root, DEFAULT_ARTIFACT):
            manifest_contract_failures.append("sidecar_path")
        if not isinstance(sidecar.get("size_bytes"), int) or int(sidecar["size_bytes"]) <= 0:
            manifest_contract_failures.append("sidecar_size")
        if not _is_sha256(sidecar.get("sha256")):
            manifest_contract_failures.append("sidecar_sha")
    expected_manifest_frames = {
        str(row["stratum"]): {
            "raw": int(row["raw_count"]),
            "flagged": int(row["flagged_count"]),
            "eligible": int(row["eligible_count"]),
            "quota": int(row["quota"]),
        }
        for row in _rows(
            connection,
            "SELECT * FROM population_frames WHERE run_id=? ORDER BY stratum",
            (run_id,),
        )
    }
    if manifest.get("population_frames") != expected_manifest_frames:
        manifest_contract_failures.append("population_frames")
    expected_manifest_step0 = [
        {
            "comparison_key": str(row["comparison_key"]),
            "n100": _json_value(row["n100_value_json"]),
            "wp25": _json_value(row["wp25_value_json"]),
            "finding": str(row["finding"]),
        }
        for row in _rows(
            connection,
            "SELECT * FROM form_comparison WHERE run_id=? ORDER BY comparison_key",
            (run_id,),
        )
    ]
    if manifest.get("step0_form_comparison") != expected_manifest_step0:
        manifest_contract_failures.append("step0_form_comparison")
    expected_manifest_pins = {
        str(row["role"]): {
            "path": str(row["path"]),
            "size_bytes": int(row["size_bytes"]),
            "sha256_before": str(row["sha256_before"]),
            "sha256_after": str(row["sha256_after"]),
            "usage": str(row["usage"]),
        }
        for row in _rows(
            connection,
            "SELECT * FROM input_pins WHERE run_id=? AND role NOT LIKE 'e1_%' "
            "AND role<>'e2_image_evidence' ORDER BY role",
            (run_id,),
        )
    }
    if manifest.get("input_pins") != expected_manifest_pins:
        manifest_contract_failures.append("input_pins")
    pre_freeze = connection.execute(
        "SELECT * FROM quota_observations WHERE run_id=? AND phase='pre_freeze'", (run_id,)
    ).fetchone()
    expected_manifest_quota = (
        {
            "observed_at": str(pre_freeze["observed_at"]),
            "remaining_percent": float(pre_freeze["gate_basis_remaining_percent"]),
            "limitation": str(pre_freeze["limitation"]),
        }
        if pre_freeze is not None
        else None
    )
    if manifest.get("quota_pre_freeze") != expected_manifest_quota:
        manifest_contract_failures.append("quota_pre_freeze")
    manifest_contract_failures.extend(
        "counter:" + value for value in _forbidden_counter_failures(manifest)
    )
    _check(
        checks,
        "sample_manifest_exact_contract",
        [],
        sorted(manifest_contract_failures),
    )
    manifest_samples = manifest.get("samples")
    manifest_failures: list[str] = []
    if not isinstance(manifest_samples, list) or len(manifest_samples) != TOTAL_SAMPLE:
        manifest_failures.append("sample_rows")
    else:
        manifest_by_sample = {
            str(row.get("sample_id")): row for row in manifest_samples if isinstance(row, Mapping)
        }
        if set(manifest_by_sample) != set(by_sample):
            manifest_failures.append("sample_set")
        for sample_id, row in manifest_by_sample.items():
            pair = by_sample.get(sample_id)
            commitment = commitment_by_sample.get(sample_id)
            if pair is None or commitment is None:
                continue
            if row.get("seed_rank_sha256") != pair.get("seed_rank_sha256"):
                manifest_failures.append(f"{sample_id}:rank")
            if int(row.get("sample_rank", -1)) != int(pair.get("sample_rank", -2)):
                manifest_failures.append(f"{sample_id}:sample_rank")
            commitment_value = row.get(
                "gold_commitment_sha256", row.get("control_commitment_sha256")
            )
            if commitment_value != commitment.get("gold_commitment_sha256"):
                manifest_failures.append(f"{sample_id}:commitment")
        if [str(row.get("sample_id")) for row in manifest_samples] != [
            str(row["sample_id"]) for row in pairs
        ]:
            manifest_failures.append("sample_order")
    _check(checks, "sample_manifest_db_binding", [], sorted(manifest_failures))
    _check(
        checks,
        "sample_manifest_pre_reveal_scrubbed",
        [],
        _sample_manifest_leaks(
            sample_manifest_path, real_candidate_ids=set(expected["candidate_ids"])
        ),
    )
    _check(
        checks,
        "pre_reveal_sidecar_scrubbed",
        [],
        _pre_reveal_db_leaks(
            connection, real_candidate_ids=set(expected["candidate_ids"])
        ),
    )
    metrics["sample"] = {
        "counts": expected["counts"],
        "gold_counts": expected["gold_counts"],
        "d_universe_count": expected["d_universe_count"],
        "sample_logical_sha256": logical,
    }


def _validate_state_events(
    connection: sqlite3.Connection,
    run_id: str,
    checks: list[Check],
) -> None:
    rows = _rows(
        connection, "SELECT * FROM state_events WHERE run_id=? ORDER BY event_no", (run_id,)
    )
    failures: list[str] = []
    if [int(row.get("event_no", -1)) for row in rows] != list(range(1, len(rows) + 1)):
        failures.append("event_numbers")
    previous_to: str | None = None
    previous_time: datetime | None = None
    statuses: list[str] = []
    criteria_declarations: list[int] = []
    for row in rows:
        from_status = str(row.get("from_status"))
        to_status = str(row.get("to_status"))
        if previous_to is not None and from_status != previous_to:
            failures.append(f"{row.get('event_no')}:chain")
        previous_to = to_status
        statuses.append(to_status)
        try:
            detail = _json_value(row.get("detail_json"))
        except (json.JSONDecodeError, TypeError):
            detail = None
        if isinstance(detail, Mapping) and detail.get("logical_stage") == "criteria_declared":
            criteria_declarations.append(len(statuses) - 1)
            if from_status != "sample_frozen" or to_status != "sample_frozen":
                failures.append(f"{row.get('event_no')}:criteria_declaration_transition")
        try:
            occurred = _iso(str(row.get("occurred_at")))
            if previous_time is not None and occurred < previous_time:
                failures.append(f"{row.get('event_no')}:time")
            previous_time = occurred
        except ValueError:
            failures.append(f"{row.get('event_no')}:timestamp")
    required = (
        "sample_frozen",
        "fetching",
        "review_ready",
        "reviews_partial",
        "reviews_locked",
        "unblinded",
        "adjudication_locked",
    )
    positions = []
    for status in required:
        if status not in statuses:
            failures.append(f"missing:{status}")
        else:
            positions.append(statuses.index(status))
    if positions != sorted(positions):
        failures.append("barrier_order")
    if len(criteria_declarations) != 1:
        failures.append("criteria_declaration_event")
    elif "fetching" in statuses and criteria_declarations[0] >= statuses.index("fetching"):
        failures.append("criteria_declared_after_fetch")
    terminal = [status for status in statuses if status in {"pass", "fail"}]
    if len(terminal) != 1 or not statuses or statuses[-1] != terminal[0]:
        failures.append("terminal_event")
    _check(checks, "state_event_chain_and_barrier_order", [], sorted(set(failures)))


def _forbidden_counter_failures(report: Mapping[str, Any]) -> list[str]:
    expected_zero_keys = {
        "paid_api_requests",
        "vision_requests",
        "llm_requests",
        "merge_executions",
        "vnext_id_issuances",
        "tier_promotions",
        "neon_writes",
        "r2_writes",
    }
    found: dict[str, Any] = {}
    for location, key, value in _walk_json(report):
        if key.casefold() in expected_zero_keys:
            found[key.casefold()] = value
    failures = [f"{key}:{value}" for key, value in sorted(found.items()) if value not in (0, 0.0)]
    missing = sorted(expected_zero_keys - set(found))
    if missing:
        failures.append("missing:" + ",".join(missing))
    return failures


def _validate_final(
    root: Path,
    connection: sqlite3.Connection,
    run: Mapping[str, Any],
    checks: list[Check],
    metrics: dict[str, Any],
    *,
    expected: Mapping[str, Any],
    report_path: Path,
    manifest_path: Path,
) -> None:
    run_id = str(run["run_id"])
    reveals = _rows(connection, "SELECT * FROM probe_reveals WHERE run_id=?", (run_id,))
    adjudications = _rows(
        connection, "SELECT * FROM probe_adjudications WHERE run_id=?", (run_id,)
    )
    reveal_by_sample = {str(row["sample_id"]): row for row in reveals}
    adjudication_by_sample = {str(row["sample_id"]): row for row in adjudications}
    expected_by_sample = {
        str(row["sample_id"]): row for row in persisted_control_rows(run_id, expected)
    }
    reveal_failures: list[str] = []
    if len(reveals) != TOTAL_SAMPLE or set(reveal_by_sample) != set(expected_by_sample):
        reveal_failures.append("reveal_set")
    for sample_id, expected_row in expected_by_sample.items():
        reveal = reveal_by_sample.get(sample_id)
        adjudication = adjudication_by_sample.get(sample_id)
        if reveal is None or adjudication is None:
            continue
        expected_values = {
            "candidate_id": expected_row["candidate_id"],
            "group_code": expected_row["group"],
            "n100_sample_id": expected_row["n100_sample_id"],
            "n100_gold_label": expected_row["frozen_gold_label"],
            "final_probe_label": adjudication["final_probe_label"],
            "commitment_sha256": control_commitment(expected_row),
        }
        for key, value in expected_values.items():
            if reveal.get(key) != value:
                reveal_failures.append(f"{sample_id}:{key}")
    _check(checks, "post_lock_n100_gold_join_and_commitment_open", [], sorted(reveal_failures))

    gate_rows = [
        {
            "group": row["group_code"],
            "probe_label": row["final_probe_label"],
        }
        for row in reveals
    ]
    expected_gate = gate_from_labels(gate_rows)
    scores = _rows(connection, "SELECT * FROM probe_scores WHERE run_id=?", (run_id,))
    score_failures: list[str] = []
    expected_score_values = {
        "P1": {
            "sample_n": 3,
            "probe_same": 3 - expected_gate["counts"]["P1_non_same"],
            "probe_non_same": expected_gate["counts"]["P1_non_same"],
            "threshold": 2,
            "passed": int(expected_gate["predicates"]["p1_non_same_at_least_2_of_3"]),
        },
        "P23": {
            "sample_n": 6,
            "probe_same": 6 - expected_gate["counts"]["P2_P3_non_same"],
            "probe_non_same": expected_gate["counts"]["P2_P3_non_same"],
            "threshold": 3,
            "passed": int(
                expected_gate["predicates"]["p2_p3_non_same_at_least_3_of_6"]
            ),
        },
        "D": {
            "sample_n": 15,
            "probe_same": expected_gate["counts"]["D_same"],
            "probe_non_same": 15 - expected_gate["counts"]["D_same"],
            "threshold": 13,
            "passed": int(expected_gate["predicates"]["d_same_at_least_13_of_15"]),
        },
    }
    score_by_group = {str(row["group_code"]): row for row in scores}
    if set(score_by_group) != set(expected_score_values):
        score_failures.append("score_groups")
    for group, expected_values in expected_score_values.items():
        row = score_by_group.get(group)
        if row is None:
            continue
        for key, value in expected_values.items():
            if int(row.get(key, -1)) != value:
                score_failures.append(f"{group}:{key}")
    _check(checks, "three_predeclared_gate_predicates_exact", [], sorted(score_failures))

    expected_status = expected_gate["status"]
    _check(checks, "terminal_run_status", expected_status, run.get("status"))
    _check(checks, "terminal_gate_status", expected_status, run.get("gate_status"))
    terminals = _rows(connection, "SELECT * FROM probe_terminal WHERE run_id=?", (run_id,))
    terminal_failures: list[str] = []
    if len(terminals) != 1:
        terminal_failures.append("terminal_row")
    else:
        terminal = terminals[0]
        if not _terminal_status_matches(expected_status, terminal.get("status")):
            terminal_failures.append("status")
        try:
            terminal_time = _iso(str(terminal.get("completed_at")))
            lock_row = connection.execute(
                "SELECT locked_at FROM probe_adjudication_lock WHERE run_id=?", (run_id,)
            ).fetchone()
            if lock_row is None or terminal_time < _iso(str(lock_row[0])):
                terminal_failures.append("completed_before_adjudication_lock")
        except ValueError:
            terminal_failures.append("completed_at")
        for path_key, sha_key, expected_path in (
            ("report_path", "report_sha256", report_path),
            ("manifest_path", "manifest_sha256", manifest_path),
        ):
            path = _resolve(root, str(terminal[path_key]))
            if path != expected_path.resolve() or not path.is_file():
                terminal_failures.append(path_key)
            elif _sha256_file(path) != terminal.get(sha_key):
                terminal_failures.append(sha_key)
    _check(checks, "terminal_artifact_hash_binding", [], terminal_failures)

    try:
        report = _load_json(report_path)
        manifest = _load_json(manifest_path)
    except (OSError, json.JSONDecodeError) as exc:
        _check(checks, "final_report_manifest_json", "valid", f"{type(exc).__name__}:{exc}")
        return
    _check(checks, "final_report_manifest_json", "valid", "valid")
    _check(checks, "report_status", expected_status, str(report.get("status", "")).casefold())
    report_gold = report.get("gold_comparison")
    report_failures: list[str] = []
    if not isinstance(report_gold, list) or len(report_gold) != TOTAL_SAMPLE:
        report_failures.append("gold_rows")
    else:
        if any(not isinstance(row, Mapping) for row in report_gold):
            report_failures.append("gold_shape")
        report_by_sample = {
            str(row.get("sample_id")): row for row in report_gold if isinstance(row, Mapping)
        }
        if set(report_by_sample) != set(reveal_by_sample):
            report_failures.append("gold_set")
        if all(isinstance(row, Mapping) for row in report_gold) and [
            str(row.get("sample_id")) for row in report_gold
        ] != sorted(report_by_sample):
            report_failures.append("gold_order")
        for sample_id, reveal in reveal_by_sample.items():
            row = report_by_sample.get(sample_id)
            if row is None:
                continue
            expected_report_values = {
                "sample_id": sample_id,
                "candidate_id": reveal["candidate_id"],
                "group": reveal["group_code"],
                "n100_sample_id": reveal["n100_sample_id"],
                "n100_gold_label": reveal["n100_gold_label"],
                "probe_label": reveal["final_probe_label"],
                "commitment_sha256": reveal["commitment_sha256"],
            }
            for key, value in expected_report_values.items():
                if row.get(key) != value:
                    report_failures.append(f"{sample_id}:{key}")
    report_gate = report.get("gate", report.get("gates"))
    if isinstance(report_gate, Mapping):
        if str(report_gate.get("status", "")).casefold() != expected_status:
            report_failures.append("gate_status")
        expected_predicates = {
            "p1_non_same": {
                "actual": expected_gate["counts"]["P1_non_same"],
                "threshold": 2,
                "passed": expected_gate["predicates"]["p1_non_same_at_least_2_of_3"],
            },
            "p23_non_same": {
                "actual": expected_gate["counts"]["P2_P3_non_same"],
                "threshold": 3,
                "passed": expected_gate["predicates"][
                    "p2_p3_non_same_at_least_3_of_6"
                ],
            },
            "d_same": {
                "actual": expected_gate["counts"]["D_same"],
                "threshold": 13,
                "passed": expected_gate["predicates"]["d_same_at_least_13_of_15"],
            },
        }
        if report_gate.get("predicates") != expected_predicates:
            report_failures.append("gate_predicates")
    else:
        report_failures.append("gate_payload")
    labels_by_group: dict[str, list[str]] = {group: [] for group in GROUPS}
    for reveal in reveals:
        group = str(reveal.get("group_code"))
        if group in labels_by_group:
            labels_by_group[group].append(str(reveal.get("final_probe_label")))
    expected_group_metrics: dict[str, Any] = {}
    for group, labels in labels_by_group.items():
        label_counts = Counter(labels)
        expected_group_metrics[group] = {
            "n": len(labels),
            "labels": dict(sorted(label_counts.items())),
            "same": label_counts["same"],
            "non_same": len(labels) - label_counts["same"],
        }
    if report.get("group_metrics") != expected_group_metrics:
        report_failures.append("group_metrics")
    expected_review = {
        "r1_rows": TOTAL_SAMPLE,
        "r2_rows": TOTAL_SAMPLE,
        "adjudicated_rows": TOTAL_SAMPLE,
        "disagreements": sum(
            row.get("r1_label") != row.get("r2_label") for row in adjudications
        ),
        "unresolved_disagreements": sum(
            bool(row.get("unresolved_disagreement")) for row in adjudications
        ),
    }
    if report.get("review") != expected_review:
        report_failures.append("review_metrics")
    fetch_statuses = Counter(
        str(row[0])
        for row in connection.execute(
            "SELECT status FROM fetch_assets WHERE run_id=?", (run_id,)
        )
    )
    expected_fetch = {
        "asset_occurrences": int(run.get("asset_occurrence_count", -1)),
        "unique_assets": int(run.get("unique_asset_count", -1)),
        "logical_http_fetches": int(run.get("logical_http_fetches", -1)),
        "wire_http_requests": int(run.get("wire_http_requests", -1)),
        "status_counts": dict(fetch_statuses),
        "redirects": 0,
        "retries": 0,
        "hard_caps": {
            "asset_occurrences": MAX_ASSET_OCCURRENCES,
            "logical_http_fetches": MAX_LOGICAL_HTTP_FETCHES,
            "wire_http_requests": MAX_WIRE_HTTP_REQUESTS,
        },
    }
    if report.get("fetch") != expected_fetch:
        report_failures.append("fetch_metrics")
    expected_step0 = [
        {
            "comparison_key": str(row["comparison_key"]),
            "n100": _json_value(row["n100_value_json"]),
            "wp25": _json_value(row["wp25_value_json"]),
            "finding": str(row["finding"]),
        }
        for row in _rows(
            connection,
            "SELECT * FROM form_comparison WHERE run_id=? ORDER BY comparison_key",
            (run_id,),
        )
    ]
    if report.get("step0_form_comparison") != expected_step0:
        report_failures.append("step0_form_comparison")
    expected_quota = [
        {
            "observation_no": int(row["observation_no"]),
            "phase": str(row["phase"]),
            "observed_at": str(row["observed_at"]),
            "remaining_percent": float(row["gate_basis_remaining_percent"]),
            "start_allowed": bool(row["start_allowed"]),
            "stop_required": bool(row["stop_required"]),
            "limitation": str(row["limitation"]),
        }
        for row in _rows(
            connection,
            "SELECT * FROM quota_observations WHERE run_id=? ORDER BY observation_no",
            (run_id,),
        )
    ]
    if report.get("quota_observations") != expected_quota:
        report_failures.append("quota_observations")
    if report.get("measurement_only") is not True:
        report_failures.append("measurement_only")
    if report.get("authorizes_tiering") is not False:
        report_failures.append("authorizes_tiering")
    if report.get("wp25_s1_s4_grades_valid") is not (expected_status == "pass"):
        report_failures.append("wp25_grade_validity")
    _check(checks, "report_gold_comparison_and_gate", [], sorted(report_failures))
    _check(
        checks,
        "forbidden_operation_counters_zero",
        [],
        sorted(
            ["report:" + value for value in _forbidden_counter_failures(report)]
            + ["manifest:" + value for value in _forbidden_counter_failures(manifest)]
        ),
    )
    # The manifest is provenance-only even though it is published after reveal.
    manifest_serialized = _canonical_json(manifest)
    manifest_gold_failures = []
    if "gold_comparison" in manifest_serialized or "n100_gold_label" in manifest_serialized:
        manifest_gold_failures.append("gold_payload")
    _check(checks, "final_manifest_contains_no_gold_payload", [], manifest_gold_failures)
    manifest_failures: list[str] = []
    terminal = terminals[0] if len(terminals) == 1 else None
    expected_manifest_scalars = {
        "status": expected_status.upper(),
        "run_id": run_id,
        "versions": {
            "runner": run.get("runner_version"),
            "sample": SAMPLE_VERSION,
            "review": REVIEW_VERSION,
            "adjudication": ADJUDICATION_VERSION,
            "gate": GATE_VERSION,
        },
        "sample_logical_sha256": run.get("sample_logical_sha256"),
        "counts": {
            "sample": TOTAL_SAMPLE,
            "r1": TOTAL_SAMPLE,
            "r2": TOTAL_SAMPLE,
            "adjudicated": TOTAL_SAMPLE,
            "post_freeze_replacements": 0,
        },
        "network": {
            "logical": int(run.get("logical_http_fetches", -1)),
            "wire": int(run.get("wire_http_requests", -1)),
            "redirects": 0,
            "retries": 0,
        },
    }
    for key, value in expected_manifest_scalars.items():
        if manifest.get(key) != value:
            manifest_failures.append(key)
    if terminal is not None:
        if manifest.get("completed_at") != terminal.get("completed_at"):
            manifest_failures.append("completed_at")
        if not _resolved_report_binding_matches(
            root,
            manifest.get("report"),
            expected_path=terminal.get("report_path"),
            expected_sha256=terminal.get("report_sha256"),
        ):
            manifest_failures.append("report_binding")
    expected_artifacts = {
        str(row["role"]): {
            "path": str(row["path"]),
            "size_bytes": int(row["size_bytes"]),
            "sha256": str(row["sha256"]),
            "stage": str(row["stage"]),
        }
        for row in _rows(
            connection,
            "SELECT * FROM artifact_bindings WHERE run_id=? AND role<>'final_manifest' "
            "ORDER BY role",
            (run_id,),
        )
    }
    if manifest.get("artifacts") != expected_artifacts:
        manifest_failures.append("artifacts")
    expected_manifest_pins = {
        str(row["role"]): {
            "path": str(row["path"]),
            "size_bytes": int(row["size_bytes"]),
            "sha256": str(row["sha256_before"]),
        }
        for row in _rows(
            connection, "SELECT * FROM input_pins WHERE run_id=? ORDER BY role", (run_id,)
        )
    }
    if manifest.get("input_pins") != expected_manifest_pins:
        manifest_failures.append("input_pins")
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
    expected_manifest_caches = [
        {key: row[key] for key in cache_keys}
        for row in _rows(
            connection, "SELECT * FROM cache_pins WHERE run_id=? ORDER BY role", (run_id,)
        )
    ]
    if manifest.get("cache_pins") != expected_manifest_caches:
        manifest_failures.append("cache_pins")
    _check(checks, "final_manifest_exact_provenance_payload", [], sorted(manifest_failures))
    metrics["gate"] = expected_gate


def _validate_record_hashes(
    connection: sqlite3.Connection,
    checks: list[Check],
) -> None:
    failures: list[str] = []
    checked = 0
    for table in sorted(_tables(connection)):
        columns = _columns(connection, table)
        if "record_sha256" not in columns:
            continue
        for index, row in enumerate(_rows(connection, f"SELECT * FROM {table}")):
            checked += 1
            actual = row.pop("record_sha256", None)
            candidates = {_record_sha(row)}
            if "run_id" in row:
                candidates.add(_record_sha({key: value for key, value in row.items() if key != "run_id"}))
            without_run = {key: value for key, value in row.items() if key != "run_id"}
            if table == "sample_pairs":
                candidates.add(
                    _record_sha(
                        {
                            "sample_rank": row["sample_rank"],
                            "sample_id": row["sample_id"],
                            "stratum": row["stratum"],
                            "candidate_id": row["candidate_id"],
                            "seed_rank": row["seed_rank_sha256"],
                        }
                    )
                )
            elif table == "candidate_snapshots":
                candidates.add(
                    _record_sha(
                        {
                            "sample_id": row["sample_id"],
                            "snapshot_sha256": row["snapshot_sha256"],
                        }
                    )
                )
            elif table == "population_frames":
                candidates.add(
                    _record_sha(
                        {
                            "stratum": row["stratum"],
                            "raw": row["raw_count"],
                            "flagged": row["flagged_count"],
                            "ex_related": row["ex_related_count"],
                            "prior": row["prior_review_count"],
                            "eligible": row["eligible_count"],
                            "quota": row["quota"],
                            "predicate_version": row["predicate_version"],
                        }
                    )
                )
            elif table == "quota_observations":
                candidates.add(
                    _record_sha(
                        {
                            "observation_no": row["observation_no"],
                            "phase": row["phase"],
                            "account_index": row["account_index"],
                            "observed_at": row["observed_at"],
                            "signals": _json_value(row["signals_json"]),
                            "gate_basis_remaining_percent": row[
                                "gate_basis_remaining_percent"
                            ],
                            "start_allowed": row["start_allowed"],
                            "stop_required": row["stop_required"],
                            "limitation": row["limitation"],
                        }
                    )
                )
            elif table == "fetch_assets":
                # Cache hits and lineage failures never enter the HTTP outcome
                # updater.  Their pinned WP2.5 record binds this exact initial
                # planning payload; request/outcome columns are independently
                # constrained by the fetch-accounting checks.
                if int(row.get("request_count", -1)) == 0 and row.get("status") in {
                    "cache_hit",
                    "failed",
                }:
                    candidates.add(
                        _record_sha(
                            {
                                key: row[key]
                                for key in (
                                    "asset_key",
                                    "source",
                                    "source_asset_id",
                                    "expected_url",
                                    "expected_raw_sha256",
                                    "expected_pixel_sha256",
                                    "status",
                                    "cache_role",
                                    "cache_path",
                                    "error_kind",
                                    "error_message",
                                )
                            }
                        )
                    )
            elif table == "fetch_attempts":
                candidates.add(
                    _record_sha(
                        {
                            "asset_key": row["asset_key"],
                            "attempt_no": row["attempt_no"],
                            "status": row["status"],
                            "request_count": row["request_count"],
                            "redirect_chain": _json_value(row["redirect_chain_json"]),
                            "started_at": row["started_at"],
                            "completed_at": row["completed_at"],
                            "error_kind": row["error_kind"],
                        }
                    )
                )
            elif table == "probe_reveals":
                candidates.add(
                    _record_sha(
                        {
                            "sample_id": row["sample_id"],
                            "candidate_id": row["candidate_id"],
                            "group": row["group_code"],
                            "n100_sample_id": row["n100_sample_id"],
                            "n100_gold_label": row["n100_gold_label"],
                            "final_probe_label": row["final_probe_label"],
                            "commitment_sha256": row["commitment_sha256"],
                        }
                    )
                )
            elif table == "form_comparison":
                candidates.add(
                    _record_sha(
                        {
                            "comparison_key": row["comparison_key"],
                            "n100": _json_value(row["n100_value_json"]),
                            "wp25": _json_value(row["wp25_value_json"]),
                            "finding": row["finding"],
                        }
                    )
                )
            elif table == "gate_contract":
                base = {
                    key: row[key]
                    for key in (
                        "p1_min_non_same",
                        "p23_min_non_same",
                        "d_min_same",
                        "p1_n",
                        "p23_n",
                        "d_n",
                    )
                }
                gate_payload = {
                    **base,
                    "gate_version": GATE_VERSION,
                    "predicate": (
                        "P1 non-same >= 2/3 AND P2+P3 non-same >= 3/6 "
                        "AND D same >= 13/15"
                    ),
                }
                candidates.add(
                    _record_sha(
                        {
                            **gate_payload,
                            "contract_sha256": row["contract_sha256"],
                            "declared_at": row["declared_at"],
                        }
                    )
                )
            elif table == "state_events":
                candidates.add(
                    _record_sha(
                        {
                            "event_no": row["event_no"],
                            "from_status": row["from_status"],
                            "to_status": row["to_status"],
                            "detail": _json_value(row["detail_json"]),
                            "occurred_at": row["occurred_at"],
                        }
                    )
                )
            elif table in {"review_lock_payloads", "unblind_payloads", "final_payloads"}:
                # Recovery payload hashes intentionally bind parsed canonical JSON.
                parsed = {
                    key: _json_value(value) if key.endswith("_json") else value
                    for key, value in without_run.items()
                }
                candidates.add(_record_sha(parsed))
            if actual not in candidates:
                failures.append(f"{table}[{index}]")
    _check(
        checks,
        "record_hashes_independently_recomputed",
        {"failures": [], "checked_positive": True},
        {"failures": failures, "checked_positive": checked > 0},
    )


def _validate_reuse_binding(root: Path, checks: list[Check]) -> None:
    producer = _resolve(root, Path("canonical/vnext_identity_instrument_probe.py"))
    failures: list[str] = []
    try:
        source = producer.read_text(encoding="utf-8")
    except OSError as exc:
        failures.append(f"producer:{type(exc).__name__}")
    else:
        required_source_tokens = (
            "types.FunctionType",
            "def _clone_function(",
            '"_logical_sha", "_blind_lane_items", "_form_payload",',
            '"_validate_completed_review_form", "_choose_assets"',
            '"prepare_review": _clone_function(wp25.prepare_review, globals_copy)',
            '"lock_review": _clone_function(wp25.lock_review, globals_copy)',
            'globals_copy["json"] = _ProbeJsonAdapter(opaque_by_source)',
            'globals_copy["_open_immutable"] = isolated_open',
            "MAX_ASSETS_PER_SIDE = 2",
            "DEFAULT_OUTPUT_CACHE",
        )
        for token in required_source_tokens:
            if token not in source:
                failures.append(f"missing:{token}")
        forbidden_redefinitions = (
            "def _choose_assets(",
            "def _render_blind_sheet(",
            "def _form_payload(",
        )
        for token in forbidden_redefinitions:
            if token in source:
                failures.append(f"redefined:{token}")
        forbidden_mutations = (
            "wp25.TOTAL_COUNT =",
            "wp25.MAX_ASSETS_PER_SIDE =",
            "wp25.MAX_ASSET_OCCURRENCES =",
            "wp25.MAX_LOGICAL_HTTP_FETCHES =",
            "wp25.MAX_WIRE_HTTP_REQUESTS =",
            "wp25.SAMPLE_VERSION =",
            "wp25.SEED =",
        )
        for token in forbidden_mutations:
            if token in source:
                failures.append(f"module_global_mutation:{token}")
    _check(checks, "wp25_builder_renderer_form_lock_reuse_binding", [], failures)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(child) for child in value]
    if isinstance(value, (set, frozenset)):
        converted = [_json_safe(child) for child in value]
        return sorted(converted, key=_canonical_json)
    return value


def _write_validation_report(
    path: Path,
    *,
    phase: str,
    run_id: str | None,
    checks: Sequence[Check],
    metrics: Mapping[str, Any],
) -> None:
    payload = {
        "validator_version": VALIDATOR_VERSION,
        "phase": phase,
        "run_id": run_id,
        "status": "PASS" if all(check.passed for check in checks) else "FAIL",
        "passed_checks": sum(check.passed for check in checks),
        "total_checks": len(checks),
        "checks": [
            {
                "name": check.name,
                "passed": check.passed,
                "expected": _json_safe(check.expected),
                "actual": _json_safe(check.actual),
                "detail": check.detail,
            }
            for check in checks
        ],
        "metrics": _json_safe(metrics),
    }
    raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() != raw:
        raise FileExistsError(f"refusing to clobber validation report: {path}")
    if not path.exists():
        path.write_bytes(raw)


def validate_identity_instrument_probe(
    *,
    repo_root: Path,
    artifact_path: Path = DEFAULT_ARTIFACT,
    sample_manifest_path: Path = DEFAULT_SAMPLE_MANIFEST,
    report_path: Path | None = DEFAULT_REPORT,
    stage_report_path: Path = DEFAULT_STAGE_REPORT,
    manifest_path: Path = DEFAULT_MANIFEST,
    phase: str = "final",
    verify_full_hashes: bool = True,
) -> ValidationResult:
    """Validate a frozen sample or a terminal WP2.6 probe without mutation."""

    if phase not in {"sample", "final"}:
        raise ValueError("phase must be sample or final")
    root = repo_root.resolve()
    artifact = _resolve(root, artifact_path)
    sample_manifest = _resolve(root, sample_manifest_path)
    stage_report = _resolve(root, stage_report_path)
    manifest = _resolve(root, manifest_path)
    validation_report = _resolve(root, report_path) if report_path is not None else None
    checks: list[Check] = []
    metrics: dict[str, Any] = {}
    if not artifact.is_file():
        _check(checks, "artifact_exists", True, False)
        return ValidationResult(False, phase, None, tuple(checks), metrics, None)
    _check(checks, "artifact_exists", True, True)
    _check(checks, "sqlite_sidecars_absent", True, _no_sqlite_sidecars(artifact))
    try:
        connection = _open_readonly(artifact)
    except sqlite3.Error as exc:
        _check(checks, "artifact_open_readonly", "success", f"{type(exc).__name__}:{exc}")
        return ValidationResult(False, phase, None, tuple(checks), metrics, None)
    run_id: str | None = None
    try:
        tables = _tables(connection)
        _check(checks, "required_tables", set(REQUIRED_TABLES), tables)
        if not REQUIRED_TABLES.issubset(tables):
            result = ValidationResult(False, phase, None, tuple(checks), metrics, None)
            return result
        runs = _rows(connection, "SELECT * FROM runs")
        _check(checks, "single_run", 1, len(runs))
        if len(runs) != 1:
            return ValidationResult(False, phase, None, tuple(checks), metrics, None)
        run = runs[0]
        run_id = str(run["run_id"])
        _validate_reuse_binding(root, checks)
        _validate_step0(root, checks, metrics)
        try:
            expected = replay_control_universe(root)
        except (OSError, sqlite3.Error, ValueError) as exc:
            _check(checks, "control_universe_replay", "valid", f"{type(exc).__name__}:{exc}")
            expected = None
        else:
            _check(checks, "control_universe_replay", "valid", "valid")
        _validate_input_pins(
            root,
            connection,
            run_id,
            checks,
            metrics,
            phase=phase,
            verify_full_hashes=verify_full_hashes,
        )
        quota_rows = _rows(
            connection,
            "SELECT * FROM quota_observations WHERE run_id=? ORDER BY observation_no",
            (run_id,),
        )
        _check(
            checks,
            "quota_phase_order_and_30_20_gates",
            [],
            _quota_ledger_failures(quota_rows, phase=phase),
        )
        metrics["quota_observations"] = quota_rows
        paths = _validate_artifact_provenance(
            root,
            connection,
            run_id,
            checks,
            metrics,
            phase=phase,
            verify_full_hashes=verify_full_hashes,
        )
        if expected is not None:
            _validate_sample(
                root,
                connection,
                run,
                checks,
                metrics,
                sample_manifest_path=sample_manifest,
                expected=expected,
            )
        _validate_record_hashes(connection, checks)
        if phase == "final":
            _validate_cache_pins(
                root,
                connection,
                run_id,
                checks,
                metrics,
                verify_full_hashes=verify_full_hashes,
            )
            _validate_fetch(root, connection, run, checks, metrics)
            _validate_fetch_manifest_cap_contract(
                root,
                connection,
                run,
                checks,
                metrics,
                paths=paths,
                artifact_path=artifact,
                stage_report_path=stage_report,
                manifest_path=manifest,
            )
            if expected is not None:
                _validate_review_barriers(
                    root,
                    connection,
                    run,
                    checks,
                    metrics,
                    paths=paths,
                    expected_controls=expected,
                )
                _validate_final(
                    root,
                    connection,
                    run,
                    checks,
                    metrics,
                    expected=expected,
                    report_path=stage_report,
                    manifest_path=manifest,
                )
            _validate_state_events(connection, run_id, checks)
    except (OSError, sqlite3.Error, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        _check(checks, "validator_internal_contract", "no exception", f"{type(exc).__name__}:{exc}")
    finally:
        connection.close()

    if validation_report is not None:
        _write_validation_report(
            validation_report,
            phase=phase,
            run_id=run_id,
            checks=checks,
            metrics=metrics,
        )
    return ValidationResult(
        all(check.passed for check in checks),
        phase,
        run_id,
        tuple(checks),
        metrics,
        validation_report,
    )
