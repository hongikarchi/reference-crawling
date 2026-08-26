"""Independent validator for the WP2.5 identity band-precision overlay.

This module deliberately does not import the WP2.5 producer or the WP2.3 N100
implementation.  It reconstructs the sampling frames from the immutable v4
candidate database and derives terminal review statistics from the sidecar.
Only Python's standard library is used.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable, Mapping, Sequence


VALIDATOR_VERSION = "vnext-identity-band-precision-independent-validator-v2"
SAMPLE_VERSION = "vnext-identity-band-precision-v1"
SAMPLE_SEED = "vnext-wp25-band-precision-v1-20260826"
REVIEW_VERSION = "vnext-identity-band-precision-dual-blind-v1"
ADJUDICATION_VERSION = "vnext-identity-band-precision-adjudication-v1"
GATE_VERSION = "vnext-identity-band-precision-predeclared-gate-v1"
REVIEW_LABELS = frozenset({"same", "related", "different", "insufficient"})
STRATA = ("S1", "S2", "S3", "S4", "S5")
QUOTAS = {"S1": 60, "S2": 60, "S3": 40, "S4": 25, "S5": 36}
FRAME_COUNTS = {"S1": 2203, "S2": 1485, "S3": 464, "S4": 97, "S5": 36}
RAW_COUNTS = {"S1": 2402, "S2": 1799, "S3": 899, "S4": 119, "S5": 36}
FLAG_COUNTS = {
    "fanout_review_required": 236,
    "phash_veto_review_required": 229,
    "anchor_missing": 289,
}
TOTAL_FLAGGED = 754
PRIOR_REVIEW_UNION = 135
TOTAL_SAMPLE = 221
MAX_ASSETS_PER_SIDE = 2
MAX_ASSET_OCCURRENCES = 884
MAX_LOGICAL_HTTP_FETCHES = 884
MAX_WIRE_HTTP_REQUESTS = 884

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
    "unblind_packet": "unblind",
    "adjudication_blank_form": "unblind",
    "completed_adjudication": "finalize",
    "locked_adjudication": "finalize",
    "final_report": "finalize",
}
FINAL_PATH_STAGES: dict[str, str] = {
    **FINAL_ARTIFACT_STAGES,
    "sheets_root": "prepare",
    "final_manifest": "finalize",
}

DEFAULT_ARTIFACT = Path("data/enrichment/vnext_identity_band_precision_v1.db")
DEFAULT_SAMPLE_MANIFEST = Path(
    "data/reports/vnext_identity_band_precision_v1_sample_manifest.json"
)
DEFAULT_FETCH_MANIFEST = Path(
    "data/reports/vnext_identity_band_precision_v1_fetch_manifest.json"
)
DEFAULT_FETCH_REPORT = Path(
    "data/reports/vnext_identity_band_precision_v1_fetch_report.json"
)
DEFAULT_SHEETS_ROOT = Path("data/reports/vnext_identity_band_precision_v1_blind")
DEFAULT_R1_FORM = Path(
    "data/reports/vnext_identity_band_precision_v1_reviewer_r1_draft.json"
)
DEFAULT_R2_FORM = Path(
    "data/reports/vnext_identity_band_precision_v1_reviewer_r2_draft.json"
)
DEFAULT_R1_LOCKED = Path(
    "data/reports/vnext_identity_band_precision_v1_reviewer_r1_locked.json"
)
DEFAULT_R2_LOCKED = Path(
    "data/reports/vnext_identity_band_precision_v1_reviewer_r2_locked.json"
)
DEFAULT_ADJUDICATION_PACKET = Path(
    "data/reports/vnext_identity_band_precision_v1_adjudication_packet.json"
)
DEFAULT_ADJUDICATION_FORM = Path(
    "data/reports/vnext_identity_band_precision_v1_adjudication_draft.json"
)
DEFAULT_ADJUDICATION_LOCKED = Path(
    "data/reports/vnext_identity_band_precision_v1_adjudication_locked.json"
)
DEFAULT_STAGE_REPORT = Path("data/reports/vnext_identity_band_precision_v1_report.json")
DEFAULT_MANIFEST = Path("data/reports/vnext_identity_band_precision_v1_manifest.json")
DEFAULT_REPORT = Path(
    "data/reports/vnext_identity_band_precision_v1_validation.json"
)
DEFAULT_JOB_CARD = Path(".claude/ops/jobs/20260826_vnext_wp25_band_precision.md")
DEFAULT_V4 = Path("data/enrichment/vnext_identity_candidates_v4.db")
DEFAULT_V4_MANIFEST = Path(
    "data/reports/vnext_identity_candidates_v4_manifest.json"
)
DEFAULT_V4_GATE = Path(
    "data/reports/vnext_identity_candidates_v4_regression_gate.json"
)
DEFAULT_V1 = Path("data/enrichment/vnext_identity_candidates_v1.db")
DEFAULT_V2 = Path("data/enrichment/vnext_identity_candidates_v2.db")
DEFAULT_V3 = Path("data/enrichment/vnext_identity_candidates_v3.db")
DEFAULT_N100 = Path("data/enrichment/vnext_identity_n100_v1.db")
DEFAULT_CONTRACT = Path("docs/vnext/CONTRACT_IDENTITY.md")
DEFAULT_N100_PROTOCOL = Path("docs/vnext/IDENTITY_N100_PROTOCOL.md")
DEFAULT_PROTOCOL = Path("docs/vnext/IDENTITY_BAND_PRECISION_PROTOCOL.md")
DEFAULT_ORDER = Path(
    ".claude/ops/jobs/20260826_vnext_wp25_band_precision_order.md"
)
DEFAULT_OUTPUT_CACHE = Path(
    "data/enrichment/vnext_identity_band_precision_v1_cache"
)

FIXED_FILE_PINS: dict[Path, tuple[int, str]] = {
    DEFAULT_ORDER: (
        3517,
        "4f4eab39bb9b08746ad8200418d48cc734a9b647456e2e7e810bb335aa684567",
    ),
    DEFAULT_V4: (
        792_211_456,
        "5ab133138c26ca6d6df44a0eeec46ad413f3deb06dd2c75b3f9f799f3752d713",
    ),
    DEFAULT_V4_MANIFEST: (
        140_345,
        "6fd7b5077803666e25fd28e4f44d8e62d4680db3ed5cbf43f319c5c5e7a3881d",
    ),
    DEFAULT_V4_GATE: (
        3_676,
        "d8e37578b94cfef0891e2984b6fc781006ce552b46df6c361d3820fbb0f03242",
    ),
    DEFAULT_V1: (
        511_156_224,
        "0541b4c86bb2def88bbadaf53eec33c62d8f14e62800f1ceb8d96761f3aea5c4",
    ),
    DEFAULT_V2: (
        581_292_032,
        "08e28e4e5a1ab5aba9ea5ddd53ca615f417458a6693cdfb1aefc822d57b5b0c4",
    ),
    DEFAULT_V3: (
        580_710_400,
        "c71934cfd30807e523842133ffeb7e47d5a863d539feecd763d1086fd65d7a2f",
    ),
    DEFAULT_N100: (
        2_183_168,
        "ee62c63ebfbd118823caa2a99becd02ca974f73b453d743106bea82e21828b7b",
    ),
    DEFAULT_CONTRACT: (
        9_879,
        "502e6258b9c7803ea58bc415ddbe73fbcc50faa0005d8b9aaaaaeab04838e3e8",
    ),
    DEFAULT_N100_PROTOCOL: (
        2_735,
        "30e6c981ab257b39438769a49e4865309bb958b9db8fcdbf46530bf4d7390f1f",
    ),
    DEFAULT_PROTOCOL: (
        2_953,
        "88affb75d2f254c9c6eb8b5d3b708580100412caa80b069f6ae7716488b1120c",
    ),
}

READ_CACHE_PINS: dict[str, tuple[Path, int, int, str]] = {
    "wp1_p4b": (
        Path("data/enrichment/vnext_p4b_pixel_cache_v1"),
        151,
        26_909_641,
        "9bdc3b9b8b37808866d321420fc283a9270db4be021d561bfd756ed4f14983f6",
    ),
    "wp2_v1_v2_review": (
        Path("data/enrichment/vnext_identity_review_cache_v1"),
        73,
        13_522_544,
        "b1356938dbbb4a011963cf78e3a3903e9e2ac4ba5d6f2616033f996353b65aa7",
    ),
    "wp22_v3_review": (
        Path("data/enrichment/vnext_identity_review_cache_v3"),
        35,
        4_686_994,
        "4c99646c1ba084af1a54d44ee434e9b60997b8a78600d51e9b92193d159fe323",
    ),
    "wp23_n100_review": (
        Path("data/enrichment/vnext_identity_n100_cache_v1"),
        417,
        79_402_638,
        "742bac35d70be5eaacc2895b0818c914f86eb9295d6042af8fa6f074d85f78b2",
    ),
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
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _record_sha(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(16 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _open_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        "file:" + path.resolve().as_posix() + "?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _resolve(root: Path, value: str | Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _cache_relative_path(pixel_sha256: str, transform_version: str) -> Path:
    transform_key = hashlib.sha256(transform_version.encode("utf-8")).hexdigest()[:12]
    return Path(pixel_sha256[:2]) / f"{pixel_sha256}.{transform_key}.jpg"


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
    rows: list[dict[str, Any]] = []
    total = 0
    if root.exists():
        for path in sorted(
            (item for item in root.rglob("*") if item.is_file()),
            key=lambda item: item.relative_to(root).as_posix(),
        ):
            size = path.stat().st_size
            total += size
            rows.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "sha256": _sha256_file(path),
                    "size": size,
                }
            )
    return len(rows), total, hashlib.sha256(
        _canonical_json(rows).encode("utf-8")
    ).hexdigest()


def stable_rank(stratum: str, candidate_id: str) -> str:
    """Return the pre-declared sample rank for a stratum/candidate pair."""

    return hashlib.sha256(
        "\0".join((SAMPLE_VERSION, SAMPLE_SEED, stratum, candidate_id)).encode("utf-8")
    ).hexdigest()


def _prior_review_rows(root: Path) -> tuple[list[tuple[str, str, str]], set[str]]:
    rows: list[tuple[str, str, str]] = []
    for version, relative in (
        ("v1", DEFAULT_V1),
        ("v2", DEFAULT_V2),
        ("v3", DEFAULT_V3),
    ):
        with _open_readonly(_resolve(root, relative)) as connection:
            rows.extend(
                (str(row[0]), version, str(row[1]))
                for row in connection.execute(
                    "SELECT candidate_id,opaque_review_id FROM sample_neighbourhoods "
                    "ORDER BY candidate_id"
                )
            )
    with _open_readonly(_resolve(root, DEFAULT_N100)) as connection:
        rows.extend(
            (str(row[0]), "n100", str(row[1]))
            for row in connection.execute(
                "SELECT candidate_id,sample_id FROM sample_pairs ORDER BY candidate_id"
            )
        )
    return rows, {row[0] for row in rows}


def _frame_replay(v4: sqlite3.Connection, prior_ids: set[str]) -> dict[str, Any]:
    queue = {
        str(row["candidate_id"]): dict(row)
        for row in v4.execute("SELECT * FROM human_review_queue ORDER BY candidate_id")
    }
    flags_by_type: dict[str, set[str]] = defaultdict(set)
    for candidate_id, flag_type in v4.execute(
        "SELECT candidate_id,flag_type FROM candidate_policy_flags"
    ):
        flags_by_type[str(flag_type)].add(str(candidate_id))
    flagged = set().union(*flags_by_type.values()) if flags_by_type else set()
    transitions = {
        str(row["candidate_id"]): dict(row)
        for row in v4.execute("SELECT * FROM candidate_transitions ORDER BY candidate_id")
    }
    ex_related = {
        candidate_id
        for candidate_id, row in transitions.items()
        if row["v3_proposal"] == "related_but_separate"
    }

    raw: dict[str, set[str]] = {
        "S1": {
            candidate_id
            for candidate_id, row in queue.items()
            if row["evidence_strength_band"] == "name_firm_image_3plus"
        },
        "S2": {
            candidate_id
            for candidate_id, row in queue.items()
            if row["evidence_strength_band"] == "other_anchor_3plus"
        },
        "S3": {
            candidate_id
            for candidate_id, row in queue.items()
            if row["evidence_strength_band"] in {"anchor_2", "no_anchor_2plus"}
        },
        "S4": {
            candidate_id
            for candidate_id, row in transitions.items()
            if row["v3_proposal"] == "related_but_separate"
            and row["v4_proposal"] == "merge_candidate"
        },
        "S5": set(),
    }
    project_phase_conflicts = {
        str(row[0])
        for row in v4.execute(
            "SELECT candidate_id FROM hard_conflicts "
            "WHERE conflict_type='project_phase_incompatible'"
        )
    }
    for candidate_id, row in transitions.items():
        try:
            old_conflicts = json.loads(str(row["v3_hard_conflicts_json"]))
        except json.JSONDecodeError:
            continue
        if (
            row["v4_proposal"] == "reject"
            and old_conflicts == []
            and int(row["r1_changed"]) == 1
            and candidate_id in project_phase_conflicts
        ):
            raw["S5"].add(candidate_id)

    frames: dict[str, set[str]] = {}
    exclusions: dict[str, dict[str, set[str]]] = {}
    for stratum in STRATA:
        raw_ids = raw[stratum]
        flagged_ids = raw_ids & flagged
        after_flags = raw_ids - flagged
        related_ids = (
            after_flags & ex_related if stratum in {"S1", "S2"} else set()
        )
        after_related = after_flags - related_ids
        prior = after_related & prior_ids
        eligible = after_related - prior
        frames[stratum] = eligible
        exclusions[stratum] = {
            "flagged": flagged_ids,
            "ex_related": related_ids,
            "prior_reviewed": prior,
        }

    selected: dict[str, list[tuple[str, str]]] = {}
    for stratum in STRATA:
        ranked = sorted(
            ((stable_rank(stratum, candidate_id), candidate_id) for candidate_id in frames[stratum]),
            key=lambda item: (item[0], item[1]),
        )
        if stratum == "S5":
            selected[stratum] = ranked
        else:
            selected[stratum] = ranked[: QUOTAS[stratum]]

    return {
        "queue": queue,
        "transitions": transitions,
        "flags_by_type": {key: set(value) for key, value in flags_by_type.items()},
        "flagged": flagged,
        "ex_related": ex_related,
        "raw": raw,
        "frames": frames,
        "exclusions": exclusions,
        "selected": selected,
    }


def _expected_snapshot(
    v4: sqlite3.Connection, candidate_id: str
) -> tuple[dict[str, Any], str]:
    run_row = v4.execute("SELECT run_id FROM runs").fetchone()
    if run_row is None:
        raise ValueError("v4 run is absent")
    run_id = str(run_row[0])
    pair_row = v4.execute(
        "SELECT * FROM candidate_pairs WHERE run_id=? AND candidate_id=?",
        (run_id, candidate_id),
    ).fetchone()
    if pair_row is None:
        raise ValueError(f"candidate is absent from v4: {candidate_id}")
    pair = dict(pair_row)
    left = v4.execute(
        "SELECT * FROM source_buildings WHERE run_id=? AND source=? AND source_building_id=?",
        (run_id, pair["left_source"], pair["left_source_building_id"]),
    ).fetchone()
    right = v4.execute(
        "SELECT * FROM source_buildings WHERE run_id=? AND source=? AND source_building_id=?",
        (run_id, pair["right_source"], pair["right_source_building_id"]),
    ).fetchone()
    if left is None or right is None:
        raise ValueError(f"candidate endpoint is absent from v4: {candidate_id}")
    snapshot = {
        "candidate": pair,
        "left": dict(left),
        "right": dict(right),
        "origins": [
            str(row[0])
            for row in v4.execute(
                "SELECT origin FROM candidate_origins WHERE run_id=? AND candidate_id=? ORDER BY origin",
                (run_id, candidate_id),
            )
        ],
        "families": _rows(
            v4,
            "SELECT family,status,score,evidence_json,record_sha256 "
            "FROM evidence_families WHERE run_id=? AND candidate_id=? ORDER BY family",
            (run_id, candidate_id),
        ),
        "conflicts": _rows(
            v4,
            "SELECT conflict_type,detail_json,record_sha256 FROM hard_conflicts "
            "WHERE run_id=? AND candidate_id=? ORDER BY conflict_type",
            (run_id, candidate_id),
        ),
        "policy_flags": _rows(
            v4,
            "SELECT flag_type,detail_json,record_sha256 FROM candidate_policy_flags "
            "WHERE run_id=? AND candidate_id=? ORDER BY flag_type",
            (run_id, candidate_id),
        ),
        "transition": {},
    }
    transition = v4.execute(
        "SELECT * FROM candidate_transitions WHERE run_id=? AND candidate_id=?",
        (run_id, candidate_id),
    ).fetchone()
    if transition is not None:
        snapshot["transition"] = dict(transition)
    return snapshot, hashlib.sha256(
        _canonical_json(snapshot).encode("utf-8")
    ).hexdigest()


def _sample_logical_sha(connection: sqlite3.Connection, run_id: str) -> str:
    digest = hashlib.sha256()
    digest.update((SAMPLE_VERSION + "\n").encode("utf-8"))
    for table, order in (
        ("input_pins", "role"),
        ("quota_observations", "observation_no"),
        ("population_frames", "stratum"),
        ("excluded_pairs", "stratum,candidate_id,exclusion_type"),
        ("sample_pairs", "sample_rank"),
        ("candidate_snapshots", "sample_id"),
    ):
        digest.update((table + "\n").encode("utf-8"))
        query = f"SELECT record_sha256 FROM {table} WHERE run_id=?"
        if table == "input_pins":
            query += " AND role NOT LIKE 'e1_%' AND role<>'e2_image_evidence'"
        elif table == "quota_observations":
            query += " AND phase='pre_freeze'"
        query += f" ORDER BY {order}"
        for row in connection.execute(query, (run_id,)):
            digest.update((str(row[0]) + "\n").encode("ascii"))
    return digest.hexdigest()


def _validate_record_hashes(
    connection: sqlite3.Connection, tables: Iterable[str]
) -> list[str]:
    failures: list[str] = []
    present = _tables(connection)
    attempted_assets = (
        {
            str(row[0])
            for row in connection.execute("SELECT asset_key FROM fetch_attempts")
        }
        if "fetch_attempts" in present
        else set()
    )
    for table in sorted(set(tables) & present):
        columns = [str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")]
        if "record_sha256" not in columns:
            continue
        for index, raw in enumerate(connection.execute(f"SELECT * FROM {table}"), 1):
            original = dict(raw)
            stored = str(original["record_sha256"])
            row = {key: value for key, value in original.items() if key != "record_sha256"}
            if table != "runs":
                row.pop("run_id", None)
            if table == "candidate_snapshots":
                payload: Mapping[str, Any] = {
                    "sample_id": row["sample_id"],
                    "snapshot_sha256": row["snapshot_sha256"],
                }
            elif table == "population_frames":
                payload = {
                    "stratum": row["stratum"],
                    "raw": row["raw_count"],
                    "flagged": row["flagged_count"],
                    "ex_related": row["ex_related_count"],
                    "prior": row["prior_review_count"],
                    "eligible": row["eligible_count"],
                    "quota": row["quota"],
                    "predicate_version": row["predicate_version"],
                }
            elif table == "excluded_pairs":
                payload = {
                    "candidate_id": row["candidate_id"],
                    "stratum": row["stratum"],
                    "exclusion_type": row["exclusion_type"],
                    "detail": _json_value(row["detail_json"]),
                }
            elif table == "sample_pairs":
                payload = {
                    "sample_rank": row["sample_rank"],
                    "sample_id": row["sample_id"],
                    "stratum": row["stratum"],
                    "candidate_id": row["candidate_id"],
                    "seed_rank": row["seed_rank_sha256"],
                }
            elif table == "quota_observations":
                payload = {
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
            elif table == "fetch_assets" and str(row["asset_key"]) not in attempted_assets:
                payload = {
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
            elif table == "band_metrics":
                payload = {
                    "stratum": row["stratum"],
                    "sample_n": row["sample_n"],
                    "label_counts": {
                        label: row[f"{label}_count"] for label in sorted(REVIEW_LABELS)
                    },
                    "same_count": row["same_count"],
                    "related_count": row["related_count"],
                    "different_count": row["different_count"],
                    "insufficient_count": row["insufficient_count"],
                    "precision": row["precision"],
                    "clopper_pearson_95": {
                        "low": row["cp95_low"],
                        "high": row["cp95_high"],
                    },
                    "error_count": row["error_count"],
                    "equivalent_errors_60": row["equivalent_errors_60"],
                    "candidate_grade": row["candidate_grade"],
                    "bulk_excluded": bool(row["bulk_excluded"]),
                    "status": row["status"],
                }
            elif table == "state_events" and "detail_json" in row:
                payload = {
                    **{key: value for key, value in row.items() if key != "detail_json"},
                    "detail": _json_value(row["detail_json"]),
                }
            elif table == "fetch_attempts" and "redirect_chain_json" in row:
                payload = {
                    **{
                        key: value
                        for key, value in row.items()
                        if key != "redirect_chain_json"
                    },
                    "redirect_chain": _json_value(row["redirect_chain_json"]),
                }
            else:
                payload = row
            if _record_sha(payload) != stored:
                identity = row.get("sample_id") or row.get("candidate_id") or row.get("role") or index
                failures.append(f"{table}[{identity}]")
    return failures


def _validate_input_pins(
    root: Path,
    connection: sqlite3.Connection,
    run_id: str,
    checks: list[Check],
    *,
    verify_full_hashes: bool,
) -> None:
    rows = _rows(
        connection, "SELECT * FROM input_pins WHERE run_id=? ORDER BY role", (run_id,)
    )
    expected_roles = {
        "v4_candidates_final",
        "v4_manifest",
        "v4_regression_gate",
        "wp25_protocol",
        "identity_contract",
        "n100_protocol",
        "wp25_order",
        "wp2_v1",
        "wp21_v2",
        "wp22_v3",
        "wp23_n100",
    }
    actual_roles = {str(row["role"]) for row in rows}
    allowed_late_roles = {"e1_divisare", "e1_architizer", "e2_image_evidence"}
    _check(
        checks,
        "input_pin_roles_contract",
        ([], []),
        (
            sorted(expected_roles - actual_roles),
            sorted(actual_roles - expected_roles - allowed_late_roles),
        ),
    )
    by_path = {_resolve(root, str(row["path"])): row for row in rows}
    required_paths = {_resolve(root, path) for path in FIXED_FILE_PINS}
    required_paths.add(_resolve(root, DEFAULT_PROTOCOL))
    _check(
        checks,
        "input_pin_required_paths_present",
        [],
        sorted(str(path) for path in required_paths - set(by_path)),
    )
    failures: list[str] = []
    for path, row in by_path.items():
        if (
            not path.is_file()
            or not _no_sqlite_sidecars(path)
            or int(row["size_bytes"]) != path.stat().st_size
            or row["sha256_before"] != row["sha256_after"]
        ):
            failures.append(f"{row['role']}:metadata")
            continue
        if verify_full_hashes and _sha256_file(path) != str(row["sha256_before"]):
            failures.append(f"{row['role']}:current_sha")
        relative = path.relative_to(root.resolve()) if path.is_relative_to(root.resolve()) else None
        if relative in FIXED_FILE_PINS:
            expected = FIXED_FILE_PINS[relative]
            actual = (int(row["size_bytes"]), str(row["sha256_before"]))
            if actual != expected:
                failures.append(f"{row['role']}:fixed_pin")
    _check(checks, "input_pins_current_immutable_and_fixed", [], failures)


def _quota_ledger_failures(
    quota_rows: Sequence[Mapping[str, Any]], *, phase: str
) -> list[str]:
    """Validate dynamic quota evidence without pinning a transient percentage.

    The recorded gate must be the conservative minimum of its evidence signals.
    Wave boundaries remain fixed by protocol even though account, timestamp,
    source, and remaining percentage are runtime observations.
    """

    failures: list[str] = []
    if not quota_rows:
        return ["missing_pre_freeze"]

    required_sequence = (
        "pre_freeze",
        "pre_prepare",
        "pre_r1_review",
        "pre_r2_review",
        "pre_adjudication_review",
        "pre_finalize",
    )
    starting_wave_phases = set(required_sequence)
    observed_times: list[datetime] = []
    phase_positions: dict[str, list[int]] = defaultdict(list)

    for expected_no, row in enumerate(quota_rows, 1):
        identity = str(row.get("phase", ""))
        try:
            observation_no = int(row["observation_no"])
            account_index = int(row["account_index"])
            observed_at = str(row["observed_at"])
            gate = float(row["gate_basis_remaining_percent"])
            start_allowed = int(row["start_allowed"])
            stop_required = int(row["stop_required"])
            limitation = str(row["limitation"])
            signals = _json_value(row["signals_json"])
        except (KeyError, TypeError, ValueError):
            failures.append(f"{expected_no}:payload")
            continue

        if observation_no != expected_no:
            failures.append(f"{expected_no}:sequence")
        if account_index < 0:
            failures.append(f"{expected_no}:account")
        if not identity.strip():
            failures.append(f"{expected_no}:phase")
        else:
            phase_positions[identity].append(expected_no)
        if not limitation.strip():
            failures.append(f"{expected_no}:limitation")
        if not 0.0 <= gate <= 100.0:
            failures.append(f"{expected_no}:gate_range")
        try:
            observed_times.append(_iso(observed_at))
        except ValueError:
            failures.append(f"{expected_no}:observed_at")

        signal_values: list[float] = []
        if not isinstance(signals, list) or not signals:
            failures.append(f"{expected_no}:signals")
        else:
            for signal_no, signal in enumerate(signals, 1):
                if not isinstance(signal, Mapping):
                    failures.append(f"{expected_no}:signal_{signal_no}:mapping")
                    continue
                try:
                    source = str(signal["source"])
                    signal_account = int(signal["account_index"])
                    signal_observed_at = str(signal["observed_at"])
                    remaining = float(signal["remaining_percent"])
                except (KeyError, TypeError, ValueError):
                    failures.append(f"{expected_no}:signal_{signal_no}:payload")
                    continue
                if not source.strip():
                    failures.append(f"{expected_no}:signal_{signal_no}:source")
                if signal_account != account_index:
                    failures.append(f"{expected_no}:signal_{signal_no}:account")
                if not 0.0 <= remaining <= 100.0:
                    failures.append(f"{expected_no}:signal_{signal_no}:range")
                try:
                    _iso(signal_observed_at)
                except ValueError:
                    failures.append(f"{expected_no}:signal_{signal_no}:observed_at")
                signal_values.append(remaining)
        if signal_values and not math.isclose(
            gate, min(signal_values), rel_tol=0.0, abs_tol=1e-12
        ):
            failures.append(f"{expected_no}:conservative_gate")
        if stop_required != int(gate <= 20.0):
            failures.append(f"{expected_no}:stop_gate")
        if identity in starting_wave_phases and start_allowed != int(gate > 30.0):
            failures.append(f"{expected_no}:start_gate")

    first = quota_rows[0]
    try:
        if (
            int(first["observation_no"]) != 1
            or str(first["phase"]) != "pre_freeze"
            or float(first["gate_basis_remaining_percent"]) <= 30.0
            or int(first["start_allowed"]) != 1
            or int(first["stop_required"]) != 0
        ):
            failures.append("pre_freeze_payload")
    except (KeyError, TypeError, ValueError):
        failures.append("pre_freeze_payload")

    try:
        if observed_times != sorted(observed_times):
            failures.append("observed_at_order")
    except TypeError:
        failures.append("observed_at_timezone_mismatch")

    if phase == "sample":
        if len(quota_rows) != 1:
            failures.append("sample_observation_count")
    else:
        missing = [name for name in required_sequence if name not in phase_positions]
        if missing:
            failures.append("missing_final_wave_phases:" + ",".join(missing))
        positions = [phase_positions[name][0] for name in required_sequence if name in phase_positions]
        if positions != sorted(positions):
            failures.append("wave_phase_order")
        for wave_phase in required_sequence:
            if wave_phase not in phase_positions:
                continue
            row = quota_rows[phase_positions[wave_phase][-1] - 1]
            if not int(row["start_allowed"]) or int(row["stop_required"]):
                failures.append(f"{wave_phase}:latest_gate_denies")
        if any(int(row["stop_required"]) for row in quota_rows):
            failures.append("terminal_run_after_stop_required")
    return failures


def _validate_artifact_provenance(
    root: Path,
    sidecar: sqlite3.Connection,
    run_id: str,
    checks: list[Check],
    *,
    phase: str,
    verify_full_hashes: bool,
    explicit_paths: Mapping[str, Path],
) -> dict[str, dict[str, Any]]:
    """Recompute the append-only path and byte-binding provenance contract."""

    path_rows = _rows(
        sidecar,
        "SELECT * FROM artifact_paths WHERE run_id=? ORDER BY role",
        (run_id,),
    )
    binding_rows = _rows(
        sidecar,
        "SELECT * FROM artifact_bindings WHERE run_id=? ORDER BY role",
        (run_id,),
    )
    paths = {str(row["role"]): row for row in path_rows}
    bindings = {str(row["role"]): row for row in binding_rows}
    expected_paths = (
        {"sample_manifest": "freeze"} if phase == "sample" else FINAL_PATH_STAGES
    )
    expected_bindings = {} if phase == "sample" else FINAL_ARTIFACT_STAGES
    failures: list[str] = []

    if set(paths) != set(expected_paths):
        failures.append(
            "path_roles:missing="
            + ",".join(sorted(set(expected_paths) - set(paths)))
            + ":extra="
            + ",".join(sorted(set(paths) - set(expected_paths)))
        )
    if set(bindings) != set(expected_bindings):
        failures.append(
            "binding_roles:missing="
            + ",".join(sorted(set(expected_bindings) - set(bindings)))
            + ":extra="
            + ",".join(sorted(set(bindings) - set(expected_bindings)))
        )

    resolved_roles: dict[Path, list[str]] = defaultdict(list)
    for role, expected_stage in expected_paths.items():
        row = paths.get(role)
        if row is None:
            continue
        stored_text = str(row["path"])
        stored = Path(stored_text)
        resolved = _resolve(root, stored)
        resolved_roles[resolved].append(role)
        if stored.is_absolute() or not resolved.is_relative_to(root):
            failures.append(f"{role}:path_scope")
        if str(row["stage"]) != expected_stage:
            failures.append(f"{role}:path_stage")
        expected_explicit = explicit_paths.get(role)
        if expected_explicit is not None and resolved != _resolve(root, expected_explicit):
            failures.append(f"{role}:cli_path_binding")
        if role == "sheets_root":
            if phase == "final" and not resolved.is_dir():
                failures.append(f"{role}:missing_directory")
        elif not resolved.is_file() or resolved.stat().st_size == 0:
            failures.append(f"{role}:missing_file")
    for resolved, roles in resolved_roles.items():
        if len(roles) > 1:
            failures.append("path_collision:" + ",".join(sorted(roles)))

    for role, expected_stage in expected_bindings.items():
        row = bindings.get(role)
        path_row = paths.get(role)
        if row is None or path_row is None:
            continue
        path = _resolve(root, str(row["path"]))
        if (
            str(row["path"]) != str(path_row["path"])
            or str(row["stage"]) != expected_stage
            or str(row["stage"]) != str(path_row["stage"])
        ):
            failures.append(f"{role}:path_stage_binding")
            continue
        if (
            not path.is_file()
            or int(row["size_bytes"]) <= 0
            or path.stat().st_size != int(row["size_bytes"])
            or not re.fullmatch(r"[0-9a-f]{64}", str(row["sha256"]))
        ):
            failures.append(f"{role}:file_metadata")
            continue
        if verify_full_hashes and _sha256_file(path) != str(row["sha256"]):
            failures.append(f"{role}:file_sha256")

    payload_counts = {
        table: int(
            sidecar.execute(
                f"SELECT count(*) FROM {table} WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        )
        for table in ("review_lock_payloads", "unblind_payloads", "final_payloads")
    }
    expected_payload_counts = (
        {"review_lock_payloads": 0, "unblind_payloads": 0, "final_payloads": 0}
        if phase == "sample"
        else {"review_lock_payloads": 2, "unblind_payloads": 1, "final_payloads": 1}
    )
    if payload_counts != expected_payload_counts:
        failures.append("recovery_payload_counts")
    _check(checks, "artifact_paths_bindings_and_recovery_counts", [], failures)
    return {"paths": paths, "bindings": bindings}


def _validate_sample(
    root: Path,
    sidecar: sqlite3.Connection,
    v4: sqlite3.Connection,
    run: Mapping[str, Any],
    checks: list[Check],
    metrics: dict[str, Any],
    *,
    phase: str,
    verify_full_hashes: bool,
    sample_manifest_path: Path,
) -> dict[str, Any]:
    run_id = str(run["run_id"])
    _validate_input_pins(
        root, sidecar, run_id, checks, verify_full_hashes=verify_full_hashes
    )

    quota_rows = _rows(
        sidecar,
        "SELECT * FROM quota_observations WHERE run_id=? ORDER BY observation_no",
        (run_id,),
    )
    quota_failures = _quota_ledger_failures(quota_rows, phase=phase)
    _check(checks, "quota_observation_ledger_and_thresholds", [], quota_failures)

    v4_run = v4.execute("SELECT * FROM runs").fetchall()
    _check(checks, "v4_single_run", 1, len(v4_run))
    if v4_run:
        _check(checks, "v4_status_stage_a_complete", "stage_a_complete", v4_run[0]["status"])
        _check(checks, "v4_zero_merge_execution", 0, int(v4_run[0]["merge_executions"]))
        _check(checks, "v4_zero_id_issuance", 0, int(v4_run[0]["vnx_id_issuances"]))
    release_failures: list[str] = []
    try:
        v4_manifest = json.loads(
            _resolve(root, DEFAULT_V4_MANIFEST).read_text(encoding="utf-8")
        )
        if v4_manifest.get("status") != "PASS":
            release_failures.append("manifest_status")
        if v4_manifest.get("stage_a_artifact_sha256") != FIXED_FILE_PINS[DEFAULT_V4][1]:
            release_failures.append("manifest_db_binding")
    except (OSError, json.JSONDecodeError):
        release_failures.append("manifest_json")
    try:
        v4_gate = json.loads(_resolve(root, DEFAULT_V4_GATE).read_text(encoding="utf-8"))
        if (
            v4_gate.get("status") != "PASS"
            or int(v4_gate.get("passed_gate_count", -1)) != 4
            or int(v4_gate.get("failed_gate_count", -1)) != 0
        ):
            release_failures.append("regression_gate")
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        release_failures.append("gate_json")
    _check(checks, "v4_manifest_and_four_regression_gate", [], release_failures)

    prior_rows, prior_ids = _prior_review_rows(root)
    replay = _frame_replay(v4, prior_ids)
    _check(checks, "prior_review_row_count", 140, len(prior_rows))
    _check(checks, "prior_review_union_count", PRIOR_REVIEW_UNION, len(prior_ids))
    _check(
        checks,
        "flag_type_counts",
        FLAG_COUNTS,
        {key: len(replay["flags_by_type"].get(key, set())) for key in FLAG_COUNTS},
    )
    _check(checks, "flag_unique_count", TOTAL_FLAGGED, len(replay["flagged"]))
    _check(
        checks,
        "flag_sets_disjoint",
        sum(FLAG_COUNTS.values()),
        sum(len(value) for value in replay["flags_by_type"].values()),
    )
    actual_raw = {key: len(value) for key, value in replay["raw"].items()}
    actual_frames = {key: len(value) for key, value in replay["frames"].items()}
    _check(checks, "raw_population_counts", RAW_COUNTS, actual_raw)
    _check(checks, "eligible_frame_counts", FRAME_COUNTS, actual_frames)

    # S4's raw transition universe is reported separately from the global
    # exclusion frame; this guards against silently sampling all 119 rows.
    s4_exclusions = replay["exclusions"]["S4"]
    _check(checks, "s4_flagged_count", 21, len(s4_exclusions["flagged"]))
    _check(checks, "s4_after_flag_count", 98, RAW_COUNTS["S4"] - len(s4_exclusions["flagged"]))
    _check(checks, "s4_prior_review_count", 1, len(s4_exclusions["prior_reviewed"]))
    _check(checks, "s4_final_frame_count", 97, len(replay["frames"]["S4"]))

    frame_rows = {
        str(row["stratum"]): row
        for row in sidecar.execute(
            "SELECT * FROM population_frames WHERE run_id=?", (run_id,)
        )
    }
    _check(checks, "population_frame_strata_exact", set(STRATA), set(frame_rows))
    frame_failures: list[str] = []
    expected_frame_parts = {
        "S1": (56, 97, 46),
        "S2": (281, 0, 33),
        "S3": (417, 0, 18),
        "S4": (21, 0, 1),
        "S5": (0, 0, 0),
    }
    for stratum in STRATA:
        row = frame_rows.get(stratum)
        if row is None:
            continue
        flagged_count, ex_related_count, prior_count = expected_frame_parts[stratum]
        if (
            int(row["raw_count"]) != RAW_COUNTS[stratum]
            or int(row["flagged_count"]) != flagged_count
            or int(row["ex_related_count"]) != ex_related_count
            or int(row["prior_review_count"]) != prior_count
            or int(row["eligible_count"]) != FRAME_COUNTS[stratum]
            or int(row["quota"]) != QUOTAS[stratum]
            or int(row["raw_count"])
            != int(row["flagged_count"])
            + int(row["ex_related_count"])
            + int(row["prior_review_count"])
            + int(row["eligible_count"])
        ):
            frame_failures.append(stratum)
    _check(checks, "population_frame_ledger_counts", [], frame_failures)

    expected_exclusions: list[tuple[str, str, str, Any]] = []
    for stratum in STRATA:
        for candidate_id in sorted(replay["exclusions"][stratum]["flagged"]):
            expected_exclusions.append(
                (
                    stratum,
                    candidate_id,
                    "policy_flag",
                    sorted(
                        flag
                        for flag, values in replay["flags_by_type"].items()
                        if candidate_id in values
                    ),
                )
            )
        for candidate_id in sorted(replay["exclusions"][stratum]["ex_related"]):
            expected_exclusions.append(
                (stratum, candidate_id, "ex_related", ["v3_related_but_separate"])
            )
        for candidate_id in sorted(replay["exclusions"][stratum]["prior_reviewed"]):
            expected_exclusions.append(
                (stratum, candidate_id, "prior_review", ["prior_review_union_135"])
            )
    actual_exclusions = [
        (
            str(row["stratum"]),
            str(row["candidate_id"]),
            str(row["exclusion_type"]),
            _json_value(row["detail_json"]),
        )
        for row in sidecar.execute(
            "SELECT * FROM excluded_pairs WHERE run_id=? "
            "ORDER BY stratum,candidate_id,exclusion_type",
            (run_id,),
        )
    ]
    _check(
        checks,
        "exclusion_ledger_exact_replay",
        sorted(expected_exclusions),
        actual_exclusions,
    )

    expected: list[tuple[str, str, str]] = []
    for stratum in STRATA:
        expected.extend(
            (stratum, candidate_id, rank)
            for rank, candidate_id in replay["selected"][stratum]
        )
    actual_samples = _rows(
        sidecar,
        "SELECT * FROM sample_pairs WHERE run_id=? ORDER BY sample_rank",
        (run_id,),
    )
    actual = [
        (str(row["stratum"]), str(row["candidate_id"]), str(row["seed_rank_sha256"]))
        for row in actual_samples
    ]
    _check(checks, "sample_seed_replay_exact", expected, actual)
    _check(checks, "sample_count", TOTAL_SAMPLE, len(actual_samples))
    _check(
        checks,
        "sample_stratum_counts",
        QUOTAS,
        dict(sorted(Counter(str(row["stratum"]) for row in actual_samples).items())),
    )
    _check(
        checks,
        "sample_candidate_ids_globally_unique",
        TOTAL_SAMPLE,
        len({str(row["candidate_id"]) for row in actual_samples}),
    )
    _check(
        checks,
        "sample_ids_globally_unique",
        TOTAL_SAMPLE,
        len({str(row["sample_id"]) for row in actual_samples}),
    )
    expected_sample_ids = {
        str(row["candidate_id"]): "bp_"
        + hashlib.sha256(
            f"{run_id}\0{row['candidate_id']}".encode("utf-8")
        ).hexdigest()[:24]
        for row in actual_samples
    }
    _check(
        checks,
        "sample_ids_deterministic",
        [],
        sorted(
            str(row["candidate_id"])
            for row in actual_samples
            if str(row["sample_id"])
            != expected_sample_ids[str(row["candidate_id"])]
        ),
    )
    _check(
        checks,
        "sample_prior_review_disjoint",
        [],
        sorted({str(row["candidate_id"]) for row in actual_samples} & prior_ids),
    )
    _check(
        checks,
        "sample_flag_disjoint",
        [],
        sorted({str(row["candidate_id"]) for row in actual_samples} & replay["flagged"]),
    )

    snapshot_failures: list[str] = []
    for sample in actual_samples:
        stored_row = sidecar.execute(
            "SELECT * FROM candidate_snapshots WHERE run_id=? AND sample_id=?",
            (run_id, sample["sample_id"]),
        ).fetchone()
        if stored_row is None:
            snapshot_failures.append(f"{sample['candidate_id']}:missing")
            continue
        stored = dict(stored_row)
        try:
            snapshot, digest = _expected_snapshot(v4, str(sample["candidate_id"]))
            actual_snapshot = {
                "candidate": json.loads(str(stored["candidate_json"])),
                "left": json.loads(str(stored["left_metadata_json"])),
                "right": json.loads(str(stored["right_metadata_json"])),
                "origins": json.loads(str(stored["origins_json"])),
                "families": json.loads(str(stored["families_json"])),
                "conflicts": json.loads(str(stored["conflicts_json"])),
                "policy_flags": json.loads(str(stored["policy_flags_json"])),
                "transition": json.loads(str(stored["transition_json"])),
            }
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            snapshot_failures.append(f"{sample['candidate_id']}:{type(exc).__name__}")
            continue
        if actual_snapshot != snapshot or stored["snapshot_sha256"] != digest:
            snapshot_failures.append(str(sample["candidate_id"]))
    _check(checks, "candidate_snapshots_exact_v4_replay", [], snapshot_failures)
    _check(
        checks,
        "candidate_snapshot_count",
        TOTAL_SAMPLE,
        int(
            sidecar.execute(
                "SELECT count(*) FROM candidate_snapshots WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        ),
    )

    logical = _sample_logical_sha(sidecar, run_id)
    _check(checks, "sample_logical_sha256_replay", logical, run["sample_logical_sha256"])
    _check(
        checks,
        "sample_record_sha256s",
        [],
        _validate_record_hashes(
            sidecar,
            (
                "runs",
                "input_pins",
                "quota_observations",
                "artifact_paths",
                "artifact_bindings",
                "review_lock_payloads",
                "unblind_payloads",
                "final_payloads",
                "population_frames",
                "excluded_pairs",
                "sample_pairs",
                "candidate_snapshots",
            ),
        ),
    )

    sample_manifest = _resolve(root, sample_manifest_path)
    manifest_failures: list[str] = []
    try:
        manifest = json.loads(sample_manifest.read_text(encoding="utf-8"))
        if manifest.get("run_id") != run_id:
            manifest_failures.append("run_id")
        manifest_sample_version = manifest.get("sample_version")
        if manifest_sample_version is None and isinstance(manifest.get("versions"), Mapping):
            manifest_sample_version = manifest["versions"].get("sample")
        if manifest_sample_version != SAMPLE_VERSION:
            manifest_failures.append("sample_version")
        if manifest.get("seed") != SAMPLE_SEED:
            manifest_failures.append("seed")
        if manifest.get("sample_logical_sha256") != logical:
            manifest_failures.append("logical_sha")
        samples = manifest.get("samples")
        if not isinstance(samples, list) or len(samples) != TOTAL_SAMPLE:
            manifest_failures.append("samples")
        else:
            manifest_sample_view = [
                (
                    str(row.get("stratum")),
                    str(row.get("candidate_id")),
                    str(row.get("seed_rank")),
                    str(row.get("sample_id")),
                    int(row.get("sample_rank", -1)),
                )
                for row in samples
            ]
            db_sample_view = [
                (
                    str(row["stratum"]),
                    str(row["candidate_id"]),
                    str(row["seed_rank_sha256"]),
                    str(row["sample_id"]),
                    int(row["sample_rank"]),
                )
                for row in actual_samples
            ]
            if manifest_sample_view != db_sample_view:
                manifest_failures.append("sample_rows")
        if manifest.get("population_frames") != {
            "S1": {"raw": 2402, "flagged": 56, "ex_related": 97, "prior": 46, "eligible": 2203},
            "S2": {"raw": 1799, "flagged": 281, "ex_related": 0, "prior": 33, "eligible": 1485},
            "S3": {"raw": 899, "flagged": 417, "ex_related": 0, "prior": 18, "eligible": 464},
            "S4": {"raw": 119, "flagged": 21, "ex_related": 0, "prior": 1, "eligible": 97},
            "S5": {"raw": 36, "flagged": 0, "ex_related": 0, "prior": 0, "eligible": 36},
        }:
            manifest_failures.append("population_frames")
        quota_manifest = manifest.get("quota_pre_freeze")
        if not isinstance(quota_manifest, Mapping) or not quota_rows:
            manifest_failures.append("quota_pre_freeze")
        else:
            first = quota_rows[0]
            expected_quota_manifest = {
                "observation_no": int(first["observation_no"]),
                "phase": str(first["phase"]),
                "account_index": int(first["account_index"]),
                "observed_at": str(first["observed_at"]),
                "signals": _json_value(first["signals_json"]),
                "gate_basis_remaining_percent": float(first["gate_basis_remaining_percent"]),
                "start_allowed": int(first["start_allowed"]),
                "stop_required": int(first["stop_required"]),
                "limitation": str(first["limitation"]),
            }
            if dict(quota_manifest) != expected_quota_manifest:
                manifest_failures.append("quota_binding")
        if any(
            int(manifest.get(key, -1)) != 0
            for key in (
                "network_requests",
                "vision_requests",
                "llm_requests",
                "merge_executions",
                "vnext_id_issuances",
                "post_freeze_replacements",
            )
        ):
            manifest_failures.append("zero_mutation_and_cost_counters")
        if phase == "sample":
            sidecar_entry = manifest.get("sidecar", {})
            database_file = Path(str(sidecar.execute("PRAGMA database_list").fetchone()[2]))
            if (
                not isinstance(sidecar_entry, Mapping)
                or int(sidecar_entry.get("size_bytes", -1)) != database_file.stat().st_size
                or (
                    verify_full_hashes
                    and sidecar_entry.get("sha256") != _sha256_file(database_file)
                )
            ):
                manifest_failures.append("sidecar_hash")
    except (OSError, json.JSONDecodeError):
        manifest_failures.append("missing_or_json")
    _check(checks, "sample_manifest_semantics", [], manifest_failures)

    if phase == "sample":
        _check(checks, "sample_run_status", "sample_frozen", run["status"])
        _check(
            checks,
            "sample_run_counters_zero",
            (0, 0, 0, 0),
            tuple(
                int(run[key])
                for key in (
                    "asset_occurrence_count",
                    "unique_asset_count",
                    "logical_http_fetches",
                    "wire_http_requests",
                )
            ),
        )
        downstream = (
            "cache_pins",
            "artifact_bindings",
            "review_lock_payloads",
            "sample_assets",
            "fetch_assets",
            "fetch_attempts",
            "review_items",
            "blind_reviews",
            "review_locks",
            "unblind_artifacts",
            "unblind_payloads",
            "adjudications",
            "band_metrics",
            "final_payloads",
        )
        counts = {
            table: int(
                sidecar.execute(
                    f"SELECT count(*) FROM {table} WHERE run_id=?", (run_id,)
                ).fetchone()[0]
            )
            for table in downstream
        }
        _check(checks, "sample_downstream_tables_empty", {key: 0 for key in downstream}, counts)

    metrics["sample"] = {
        "raw_counts": actual_raw,
        "eligible_counts": actual_frames,
        "selected_counts": QUOTAS,
        "flag_count": len(replay["flagged"]),
        "prior_review_union": len(prior_ids),
        "sample_logical_sha256": logical,
    }
    return {"replay": replay, "samples": actual_samples, "prior_ids": prior_ids}


def _binomial_cdf(successes: int, trials: int, probability: float) -> float:
    return sum(
        math.comb(trials, value)
        * probability**value
        * (1.0 - probability) ** (trials - value)
        for value in range(successes + 1)
    )


def _binomial_sf_at_least(successes: int, trials: int, probability: float) -> float:
    return sum(
        math.comb(trials, value)
        * probability**value
        * (1.0 - probability) ** (trials - value)
        for value in range(successes, trials + 1)
    )


def clopper_pearson_95(successes: int, trials: int) -> tuple[float, float]:
    """Return the exact equal-tailed two-sided 95% binomial interval."""

    if trials <= 0 or successes < 0 or successes > trials:
        raise ValueError("invalid binomial counts")
    tail = 0.025
    if successes == 0:
        lower = 0.0
    else:
        lo, hi = 0.0, 1.0
        for _ in range(100):
            mid = (lo + hi) / 2.0
            if _binomial_sf_at_least(successes, trials, mid) < tail:
                lo = mid
            else:
                hi = mid
        lower = (lo + hi) / 2.0
    if successes == trials:
        upper = 1.0
    else:
        lo, hi = 0.0, 1.0
        for _ in range(100):
            mid = (lo + hi) / 2.0
            if _binomial_cdf(successes, trials, mid) > tail:
                lo = mid
            else:
                hi = mid
        upper = (lo + hi) / 2.0
    return lower, upper


def candidate_grade(error_count: int, sample_count: int) -> tuple[float, str]:
    """Apply the pre-declared exact (not rounded) n=60 equivalent grade."""

    if sample_count <= 0 or error_count < 0 or error_count > sample_count:
        raise ValueError("invalid error/sample count")
    equivalent = error_count * 60.0 / sample_count
    if equivalent <= 1.0:
        grade = "bulk_candidate"
    elif equivalent < 5.0:
        grade = "agent_first_pass"
    else:
        grade = "direct_review"
    return equivalent, grade


_BANNED_BLIND_KEYS = frozenset(
    {
        "candidate_id",
        "sample_id",
        "proposal",
        "auto_merge_eligible",
        "score",
        "policy_flags",
        "policy_flags_json",
        "evidence_strength_band",
        "band",
        "stratum",
        "seed_rank_sha256",
        "rank",
    }
)


def _blind_json_leaks(path: Path, candidate_ids: set[str]) -> list[str]:
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ["missing_or_json"]
    leaks: list[str] = []

    def visit(value: Any, location: str) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if str(key) in _BANNED_BLIND_KEYS:
                    leaks.append(f"{location}.{key}")
                visit(item, f"{location}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f"{location}[{index}]")

    visit(payload, "$")
    if re.search(r"\bidc_[0-9a-f]{32,}\b", raw):
        leaks.append("candidate_id_value")
    if any(candidate_id in raw for candidate_id in candidate_ids):
        leaks.append("selected_candidate_value")
    for forbidden in (
        "fanout_review_required",
        "phash_veto_review_required",
        "anchor_missing",
        "name_firm_image_3plus",
        "other_anchor_3plus",
        "no_anchor_2plus",
        "merge_candidate",
    ):
        if forbidden in raw:
            leaks.append(f"forbidden_value:{forbidden}")
    return sorted(set(leaks))


def _validate_cache_pins(
    root: Path,
    sidecar: sqlite3.Connection,
    run_id: str,
    checks: list[Check],
    *,
    verify_full_hashes: bool,
) -> None:
    rows = _rows(sidecar, "SELECT * FROM cache_pins WHERE run_id=? ORDER BY role", (run_id,))
    by_role = {str(row["role"]): row for row in rows}
    _check(
        checks,
        "read_cache_pin_roles_present",
        [],
        sorted(set(READ_CACHE_PINS) - set(by_role)),
    )
    failures: list[str] = []
    for role, (relative, count, size, tree) in READ_CACHE_PINS.items():
        row = by_role.get(role)
        if row is None:
            continue
        if (
            _resolve(root, str(row["path"])) != _resolve(root, relative)
            or str(row["access_mode"]) != "read_only"
            or (
                int(row["file_count_before"]),
                int(row["total_bytes_before"]),
                str(row["tree_sha256_before"]),
            )
            != (count, size, tree)
            or (
                int(row["file_count_after"]),
                int(row["total_bytes_after"]),
                str(row["tree_sha256_after"]),
            )
            != (count, size, tree)
        ):
            failures.append(f"{role}:pin")
            continue
        if verify_full_hashes and _cache_tree(_resolve(root, relative)) != (count, size, tree):
            failures.append(f"{role}:current_tree")
    output_rows = [row for row in rows if str(row["role"]) == "wp25_output"]
    if len(output_rows) != 1:
        failures.append("wp25_output:count")
    else:
        output = output_rows[0]
        if str(output["access_mode"]) not in {"write_only", "output"}:
            failures.append("wp25_output:access_mode")
        output_root = _resolve(root, str(output["path"]))
        if any(
            output_root == _resolve(root, relative)
            or output_root in _resolve(root, relative).parents
            or _resolve(root, relative) in output_root.parents
            for relative, _count, _size, _tree in READ_CACHE_PINS.values()
        ):
            failures.append("wp25_output:overlaps_read_cache")
        expected_after = (
            int(output["file_count_after"]),
            int(output["total_bytes_after"]),
            str(output["tree_sha256_after"]),
        )
        if verify_full_hashes and _cache_tree(output_root) != expected_after:
            failures.append("wp25_output:current_tree")
    _check(checks, "cache_pins_and_trees", [], failures)


def _validate_fetch(
    root: Path,
    sidecar: sqlite3.Connection,
    run: Mapping[str, Any],
    checks: list[Check],
    metrics: dict[str, Any],
) -> None:
    run_id = str(run["run_id"])
    assets = _rows(sidecar, "SELECT * FROM sample_assets WHERE run_id=?", (run_id,))
    fetches = _rows(sidecar, "SELECT * FROM fetch_assets WHERE run_id=?", (run_id,))
    attempts = _rows(sidecar, "SELECT * FROM fetch_attempts WHERE run_id=?", (run_id,))
    fetch_key_counts = Counter(str(row["asset_key"]) for row in fetches)
    by_key = {str(row["asset_key"]): row for row in fetches}
    counts = Counter((str(row["sample_id"]), str(row["side"])) for row in assets)
    _check(
        checks,
        "at_most_two_assets_per_side",
        [],
        sorted(f"{sample}:{side}:{count}" for (sample, side), count in counts.items() if count > MAX_ASSETS_PER_SIDE),
    )
    _check(checks, "asset_occurrences_at_most_884", True, len(assets) <= MAX_ASSET_OCCURRENCES)
    _check(checks, "sample_asset_count_matches_run", int(run["asset_occurrence_count"]), len(assets))
    _check(checks, "unique_asset_count_matches_run", int(run["unique_asset_count"]), len(fetches))
    sample_asset_keys = {str(row["asset_key"]) for row in assets}
    _check(
        checks,
        "all_sample_assets_have_fetch_rows",
        [],
        sorted(sample_asset_keys - set(by_key)),
    )
    _check(
        checks,
        "fetch_rows_are_unique_and_sample_bound",
        [],
        sorted(
            [f"{key}:duplicate" for key, count in fetch_key_counts.items() if count != 1]
            + [f"{key}:orphan" for key in set(by_key) - sample_asset_keys]
        ),
    )

    attempts_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in attempts:
        attempts_by_key[str(row["asset_key"])].append(row)
    attempt_keys = Counter({key: len(rows) for key, rows in attempts_by_key.items()})
    attempt_failures: list[str] = []
    for row in attempts:
        key = str(row["asset_key"])
        if int(row["attempt_no"]) != 1:
            attempt_failures.append(f"{key}:retry")
        if int(row["request_count"]) not in {0, 1}:
            attempt_failures.append(f"{key}:request_count")
        if _json_value(row["redirect_chain_json"]) != []:
            attempt_failures.append(f"{key}:redirect")
        if not str(row["started_at"]).strip() or not str(row["completed_at"]).strip():
            attempt_failures.append(f"{key}:timestamps")
        fetch_row = by_key.get(key)
        if fetch_row is None:
            attempt_failures.append(f"{key}:missing_fetch_asset")
        elif (
            int(fetch_row["request_count"]) != int(row["request_count"])
            or str(fetch_row["status"]) != str(row["status"])
            or str(fetch_row.get("error_kind") or "") != str(row.get("error_kind") or "")
        ):
            attempt_failures.append(f"{key}:fetch_binding")
    if any(value > 1 for value in attempt_keys.values()):
        attempt_failures.append("duplicate_logical_attempt")

    bijection_failures: list[str] = []
    for key, row in by_key.items():
        status = str(row["status"])
        prefetch_failure = status == "failed" and str(row.get("error_kind") or "") == "missing_frozen_lineage"
        expected_attempts = 0 if status == "cache_hit" or prefetch_failure else 1
        actual_attempts = len(attempts_by_key.get(key, []))
        if actual_attempts != expected_attempts:
            bijection_failures.append(f"{key}:{actual_attempts}!={expected_attempts}")
    for key in set(attempts_by_key) - set(by_key):
        bijection_failures.append(f"{key}:attempt_without_fetch")
    _check(checks, "fetch_attempt_bijection", [], sorted(set(bijection_failures)))

    logical = len(attempts)
    wire = sum(int(row["request_count"]) for row in attempts)
    if wire != sum(int(row["request_count"]) for row in fetches):
        attempt_failures.append("fetch_request_sum")
    if logical != wire:
        attempt_failures.append(f"logical_wire_mismatch:{logical}!={wire}")
    _check(checks, "fetch_redirects_retries_disabled", [], attempt_failures)
    _check(checks, "logical_http_fetches_at_most_884", True, logical <= MAX_LOGICAL_HTTP_FETCHES)
    _check(checks, "wire_http_requests_at_most_884", True, wire <= MAX_WIRE_HTTP_REQUESTS)
    _check(checks, "logical_http_counter", int(run["logical_http_fetches"]), logical)
    _check(checks, "wire_http_counter", int(run["wire_http_requests"]), wire)

    cache_rows = _rows(sidecar, "SELECT * FROM cache_pins WHERE run_id=?", (run_id,))
    cache_role_counts = Counter(str(row["role"]) for row in cache_rows)
    cache_by_role = {str(row["role"]): row for row in cache_rows}
    cache_failures = [
        f"{role}:duplicate_pin" for role, count in cache_role_counts.items() if count != 1
    ]
    status_failures: list[str] = []
    terminal = {"exact", "delivery_changed", "source_changed", "cache_hit", "failed"}
    for key, row in by_key.items():
        status = str(row["status"])
        if status not in terminal:
            status_failures.append(f"{key}:non_terminal_or_unknown:{status}")
        expected_url = row.get("expected_url")
        expected_raw = row.get("expected_raw_sha256")
        expected_pixel = row.get("expected_pixel_sha256")
        actual_raw = row.get("actual_raw_sha256")
        actual_pixel = row.get("actual_pixel_sha256")
        request_count = int(row["request_count"])
        error_kind = str(row.get("error_kind") or "")
        error_message = str(row.get("error_message") or "")

        if status in {"exact", "delivery_changed", "source_changed", "cache_hit"}:
            if not str(expected_url or "").strip():
                status_failures.append(f"{key}:expected_url")
            if not _is_sha256(expected_raw):
                status_failures.append(f"{key}:expected_raw_sha256")
            if not _is_sha256(expected_pixel):
                status_failures.append(f"{key}:expected_pixel_sha256")

        if status in {"exact", "delivery_changed", "source_changed"}:
            if request_count != 1:
                status_failures.append(f"{key}:network_request_count")
            if not _is_sha256(actual_raw):
                status_failures.append(f"{key}:actual_raw_sha256")
            if not _is_sha256(actual_pixel):
                status_failures.append(f"{key}:actual_pixel_sha256")
            if not str(row.get("actual_final_url") or "").strip():
                status_failures.append(f"{key}:actual_final_url")
            http_status = row.get("http_status")
            if http_status is None or not 200 <= int(http_status) < 300:
                status_failures.append(f"{key}:http_status")
            response_bytes = row.get("response_bytes")
            if response_bytes is None or int(response_bytes) <= 0:
                status_failures.append(f"{key}:response_bytes")
            if error_kind or error_message:
                status_failures.append(f"{key}:unexpected_error")
            if status == "exact" and (
                actual_raw != expected_raw or actual_pixel != expected_pixel
            ):
                status_failures.append(f"{key}:exact_hash_semantics")
            elif status == "delivery_changed" and (
                actual_raw == expected_raw or actual_pixel != expected_pixel
            ):
                status_failures.append(f"{key}:delivery_changed_hash_semantics")
            elif status == "source_changed" and (
                actual_raw == expected_raw or actual_pixel == expected_pixel
            ):
                status_failures.append(f"{key}:source_changed_hash_semantics")
        elif status == "cache_hit":
            if request_count != 0:
                status_failures.append(f"{key}:cache_hit_request_count")
            if any(
                row.get(field) is not None
                for field in (
                    "actual_final_url",
                    "actual_raw_sha256",
                    "actual_pixel_sha256",
                    "http_status",
                    "response_bytes",
                )
            ):
                status_failures.append(f"{key}:cache_hit_network_evidence")
            if error_kind or error_message:
                status_failures.append(f"{key}:cache_hit_error")
        elif status == "failed":
            if row.get("cache_role") is not None or row.get("cache_path") is not None:
                status_failures.append(f"{key}:failed_cache")
            if not error_kind or not error_message:
                status_failures.append(f"{key}:failed_error_evidence")
            if actual_raw is not None and not _is_sha256(actual_raw):
                status_failures.append(f"{key}:failed_actual_raw_sha256")
            if actual_pixel is not None and not _is_sha256(actual_pixel):
                status_failures.append(f"{key}:failed_actual_pixel_sha256")
            if actual_pixel is not None and actual_raw is None:
                status_failures.append(f"{key}:failed_pixel_without_raw")
            if error_kind == "missing_frozen_lineage":
                if request_count != 0 or any(
                    row.get(field) is not None
                    for field in (
                        "expected_url",
                        "expected_raw_sha256",
                        "expected_pixel_sha256",
                        "actual_final_url",
                        "actual_raw_sha256",
                        "actual_pixel_sha256",
                        "http_status",
                        "response_bytes",
                    )
                ):
                    status_failures.append(f"{key}:prefetch_failure_semantics")
            elif (
                not str(expected_url or "").strip()
                or not _is_sha256(expected_raw)
                or not _is_sha256(expected_pixel)
            ):
                status_failures.append(f"{key}:failed_frozen_lineage")
        elif status == "interrupted":
            if row.get("cache_role") is not None or row.get("cache_path") is not None:
                status_failures.append(f"{key}:interrupted_cache")
            if not error_kind or not error_message:
                status_failures.append(f"{key}:interrupted_error_evidence")
            if actual_raw is not None and not _is_sha256(actual_raw):
                status_failures.append(f"{key}:interrupted_actual_raw_sha256")
            if actual_pixel is not None:
                status_failures.append(f"{key}:interrupted_pixel_sha256")
            if (
                not str(expected_url or "").strip()
                or not _is_sha256(expected_raw)
                or not _is_sha256(expected_pixel)
            ):
                status_failures.append(f"{key}:interrupted_frozen_lineage")

        role = str(row.get("cache_role") or "")
        cache_path = str(row.get("cache_path") or "")
        if status in {"exact", "delivery_changed", "source_changed"}:
            expected_role = "wp25_output"
            pixel_for_path = actual_pixel
            expected_access = {"write_only", "output"}
        elif status == "cache_hit":
            expected_role = role
            pixel_for_path = expected_pixel
            expected_access = {"read_only"}
            if role not in READ_CACHE_PINS:
                cache_failures.append(f"{key}:read_cache_role:{role or 'missing'}")
        else:
            expected_role = ""
            pixel_for_path = None
            expected_access = set()
            if role or cache_path:
                cache_failures.append(f"{key}:unexpected_cache_binding")

        if expected_role:
            if role != expected_role:
                cache_failures.append(f"{key}:cache_role:{role or 'missing'}!={expected_role}")
            pin = cache_by_role.get(role)
            if pin is None:
                cache_failures.append(f"{key}:missing_cache_pin:{role or 'missing'}")
            elif str(pin["access_mode"]) not in expected_access:
                cache_failures.append(f"{key}:cache_access_mode")
            elif not _is_sha256(pixel_for_path) or not cache_path:
                cache_failures.append(f"{key}:cache_path_inputs")
            else:
                pin_root = _resolve(root, str(pin["path"]))
                resolved_path = _resolve(root, cache_path)
                expected_path = (
                    pin_root
                    / _cache_relative_path(str(pixel_for_path), str(pin["transform_version"]))
                ).resolve()
                if (
                    not resolved_path.is_relative_to(pin_root)
                    or resolved_path != expected_path
                ):
                    cache_failures.append(f"{key}:cache_tree_path")
                elif not resolved_path.is_file():
                    cache_failures.append(f"{key}:missing_cache")

    _check(checks, "fetch_status_hash_semantics", [], sorted(set(status_failures)))
    _check(checks, "fetch_cache_role_path_tree_binding", [], sorted(set(cache_failures)))
    _check(
        checks,
        "fetch_ledger_terminal_and_cache_complete",
        [],
        sorted(set(status_failures + cache_failures)),
    )
    metrics["fetch"] = {
        "asset_occurrences": len(assets),
        "unique_assets": len(fetches),
        "logical_attempts": logical,
        "wire_requests": wire,
        "status_counts": dict(sorted(Counter(str(row["status"]) for row in fetches).items())),
    }


def _validate_state_events(
    sidecar: sqlite3.Connection,
    run: Mapping[str, Any],
    phase: str,
    checks: list[Check],
) -> None:
    rows = _rows(
        sidecar,
        "SELECT * FROM state_events WHERE run_id=? ORDER BY event_no",
        (str(run["run_id"]),),
    )
    failures: list[str] = []
    for expected_no, row in enumerate(rows, 1):
        if int(row["event_no"]) != expected_no:
            failures.append(f"{expected_no}:sequence")
        if not isinstance(_json_value(row["detail_json"]), Mapping):
            failures.append(f"{expected_no}:detail")
        if expected_no > 1 and row["from_status"] != rows[expected_no - 2]["to_status"]:
            failures.append(f"{expected_no}:chain")
        if expected_no > 1 and _iso(str(row["occurred_at"])) < _iso(
            str(rows[expected_no - 2]["occurred_at"])
        ):
            failures.append(f"{expected_no}:timestamp")
    if phase == "sample":
        expected_edges = [("building", "sample_frozen")]
    else:
        terminal = "fail" if str(run["status"]).casefold() == "fail" else "pass"
        expected_edges = [
            ("building", "sample_frozen"),
            ("sample_frozen", "fetching"),
            ("fetching", "review_ready"),
            ("review_ready", "reviews_partial"),
            ("reviews_partial", "reviews_locked"),
            ("reviews_locked", "unblinded"),
            ("unblinded", terminal),
        ]
    actual_edges = [
        (str(row["from_status"]), str(row["to_status"])) for row in rows
    ]
    if actual_edges != expected_edges:
        failures.append("edge_sequence")
    _check(checks, "state_event_chain_and_barrier", [], failures)


def _validate_reviews(
    root: Path,
    sidecar: sqlite3.Connection,
    run: Mapping[str, Any],
    samples: Sequence[Mapping[str, Any]],
    checks: list[Check],
    metrics: dict[str, Any],
) -> list[dict[str, Any]]:
    run_id = str(run["run_id"])
    sample_ids = {str(row["sample_id"]) for row in samples}
    candidate_ids = {str(row["candidate_id"]) for row in samples}
    items = _rows(
        sidecar,
        "SELECT * FROM review_items WHERE run_id=? ORDER BY lane,display_order",
        (run_id,),
    )
    reviews = _rows(
        sidecar,
        "SELECT * FROM blind_reviews WHERE run_id=? ORDER BY lane,opaque_review_id",
        (run_id,),
    )
    locks = _rows(sidecar, "SELECT * FROM review_locks WHERE run_id=? ORDER BY lane", (run_id,))
    unblind = _rows(sidecar, "SELECT * FROM unblind_artifacts WHERE run_id=?", (run_id,))
    _check(
        checks,
        "review_item_counts_per_lane",
        {"r1": TOTAL_SAMPLE, "r2": TOTAL_SAMPLE},
        dict(sorted(Counter(str(row["lane"]) for row in items).items())),
    )
    _check(
        checks,
        "blind_review_counts_per_lane",
        {"r1": TOTAL_SAMPLE, "r2": TOTAL_SAMPLE},
        dict(sorted(Counter(str(row["lane"]) for row in reviews).items())),
    )
    item_failures: list[str] = []
    lane_sample_sequences: dict[str, list[str]] = {}
    for lane in ("r1", "r2"):
        lane_rows = [row for row in items if row["lane"] == lane]
        lane_sample_sequences[lane] = [str(row["sample_id"]) for row in lane_rows]
        if {str(row["sample_id"]) for row in lane_rows} != sample_ids:
            item_failures.append(f"{lane}:sample_coverage")
        if [int(row["display_order"]) for row in lane_rows] != list(range(1, TOTAL_SAMPLE + 1)):
            item_failures.append(f"{lane}:display_order")
        for row in lane_rows:
            path = _resolve(root, str(row["sheet_path"]))
            if (
                not path.is_file()
                or path.stat().st_size != int(row["sheet_bytes"])
                or _sha256_file(path) != str(row["sheet_sha256"])
                or "idc_" in str(row["sheet_path"])
            ):
                item_failures.append(f"{lane}:{row['opaque_review_id']}:sheet")
        expected_order: list[tuple[str, str, str]] = []
        for sample_id in sample_ids:
            opaque = f"{lane}_" + hashlib.sha256(
                f"{REVIEW_VERSION}\0{run_id}\0{lane}\0{sample_id}".encode("utf-8")
            ).hexdigest()[:24]
            order_key = hashlib.sha256(
                f"{SAMPLE_SEED}\0blind-order\0{lane}\0{sample_id}".encode("utf-8")
            ).hexdigest()
            expected_order.append((order_key, opaque, sample_id))
        expected_lane = [
            (display_order, opaque, sample_id)
            for display_order, (_key, opaque, sample_id) in enumerate(
                sorted(expected_order), 1
            )
        ]
        actual_lane = [
            (
                int(row["display_order"]),
                str(row["opaque_review_id"]),
                str(row["sample_id"]),
            )
            for row in lane_rows
        ]
        if actual_lane != expected_lane:
            item_failures.append(f"{lane}:deterministic_shuffle_or_opaque")
    r1_opaque = {str(row["opaque_review_id"]) for row in items if row["lane"] == "r1"}
    r2_opaque = {str(row["opaque_review_id"]) for row in items if row["lane"] == "r2"}
    if r1_opaque & r2_opaque:
        item_failures.append("opaque_ids_not_lane_isolated")
    if lane_sample_sequences.get("r1") == lane_sample_sequences.get("r2"):
        item_failures.append("lane_orders_not_independent")
    _check(checks, "review_items_complete_isolated_and_hashed", [], item_failures)

    review_failures: list[str] = []
    for row in reviews:
        if row["label"] not in REVIEW_LABELS:
            review_failures.append(f"{row['lane']}:{row['opaque_review_id']}:label")
        if not str(row["rationale"]).strip():
            review_failures.append(f"{row['lane']}:{row['opaque_review_id']}:rationale")
    reviewer_sets = {
        lane: {str(row["reviewer"]).strip().casefold() for row in reviews if row["lane"] == lane}
        for lane in ("r1", "r2")
    }
    if any(len(value) != 1 for value in reviewer_sets.values()):
        review_failures.append("multiple_actor_within_lane")
    reviewer_actors = {next(iter(value)) for value in reviewer_sets.values() if len(value) == 1}
    if len(reviewer_actors) != 2:
        review_failures.append("reviewer_actors_not_independent")
    _check(checks, "blind_reviews_labels_rationales_and_actor_isolation", [], review_failures)

    lock_failures: list[str] = []
    by_lane_lock = {str(row["lane"]): row for row in locks}
    if set(by_lane_lock) != {"r1", "r2"}:
        lock_failures.append("lock_rows")
    for lane, row in by_lane_lock.items():
        if int(row["row_count"]) != TOTAL_SAMPLE:
            lock_failures.append(f"{lane}:row_count")
        for path_key, sha_key in (
            ("completed_form_path", "completed_form_sha256"),
            ("locked_output_path", "locked_output_sha256"),
        ):
            path = _resolve(root, str(row[path_key]))
            if not path.is_file() or _sha256_file(path) != str(row[sha_key]):
                lock_failures.append(f"{lane}:{path_key}")
        try:
            locked_payload = json.loads(
                _resolve(root, str(row["locked_output_path"])).read_text(
                    encoding="utf-8"
                )
            )
            locked_items = locked_payload.get("items")
            if (
                locked_payload.get("review_version") != REVIEW_VERSION
                or locked_payload.get("run_id") != run_id
                or locked_payload.get("lane") != lane
                or locked_payload.get("reviewer") != row["reviewer"]
                or int(locked_payload.get("row_count", -1)) != TOTAL_SAMPLE
                or not isinstance(locked_items, list)
                or len(locked_items) != TOTAL_SAMPLE
                or _record_sha(locked_items) != row["ordered_ledger_sha256"]
                or locked_payload.get("ordered_ledger_sha256")
                != row["ordered_ledger_sha256"]
            ):
                lock_failures.append(f"{lane}:locked_payload")
            else:
                review_map = {
                    str(review["opaque_review_id"]): review
                    for review in reviews
                    if review["lane"] == lane
                }
                for item in locked_items:
                    opaque = str(item.get("opaque_review_id", ""))
                    review = review_map.get(opaque)
                    if review is None or any(
                        item.get(key) != review[key]
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
                        lock_failures.append(f"{lane}:ledger_db_binding")
                        break
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            lock_failures.append(f"{lane}:locked_json")
    if len(unblind) != 1:
        lock_failures.append("unblind_row_count")
    else:
        unblind_at = _iso(str(unblind[0]["created_at"]))
        for lane, row in by_lane_lock.items():
            if _iso(str(row["locked_at"])) > unblind_at:
                lock_failures.append(f"{lane}:lock_after_unblind")
            if str(unblind[0][f"{lane}_ledger_sha256"]) != str(row["ordered_ledger_sha256"]):
                lock_failures.append(f"{lane}:ledger_binding")
        for path_key, sha_key in (
            ("packet_path", "packet_sha256"),
            ("form_path", "form_sha256"),
        ):
            path = _resolve(root, str(unblind[0][path_key]))
            if not path.is_file() or _sha256_file(path) != str(unblind[0][sha_key]):
                lock_failures.append(f"unblind:{path_key}")
        try:
            packet = json.loads(
                _resolve(root, str(unblind[0]["packet_path"])).read_text(
                    encoding="utf-8"
                )
            )
            form = json.loads(
                _resolve(root, str(unblind[0]["form_path"])).read_text(
                    encoding="utf-8"
                )
            )
            packet_items = packet.get("items")
            form_items = form.get("items")
            if (
                packet.get("adjudication_version") != ADJUDICATION_VERSION
                or packet.get("run_id") != run_id
                or int(packet.get("row_count", -1)) != TOTAL_SAMPLE
                or not isinstance(packet_items, list)
                or len(packet_items) != TOTAL_SAMPLE
                or form.get("adjudication_version") != ADJUDICATION_VERSION
                or form.get("run_id") != run_id
                or not isinstance(form_items, list)
                or len(form_items) != TOTAL_SAMPLE
            ):
                lock_failures.append("unblind:payload_contract")
            else:
                sample_order = {
                    str(sample["sample_id"]): (
                        int(sample["sample_rank"]),
                        str(sample["candidate_id"]),
                    )
                    for sample in samples
                }
                if [str(item.get("sample_id")) for item in packet_items] != [
                    str(sample["sample_id"]) for sample in samples
                ]:
                    lock_failures.append("unblind:packet_order")
                contract_items: list[dict[str, Any]] = []
                for item in form_items:
                    sample_id = str(item.get("sample_id", ""))
                    expected = sample_order.get(sample_id)
                    if (
                        expected is None
                        or int(item.get("sample_rank", -1)) != expected[0]
                        or item.get("candidate_id") != expected[1]
                    ):
                        lock_failures.append("unblind:form_binding")
                        break
                    contract_items.append(
                        {
                            key: item[key]
                            for key in (
                                "sample_rank",
                                "sample_id",
                                "candidate_id",
                                "r1_label",
                                "r2_label",
                            )
                        }
                    )
                contract = {
                    "adjudication_version": ADJUDICATION_VERSION,
                    "run_id": run_id,
                    "items": contract_items,
                }
                if form.get("form_contract_sha256") != _record_sha(contract):
                    lock_failures.append("unblind:form_contract_sha")
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            lock_failures.append("unblind:json")
    _check(checks, "lock_before_unblind_and_hash_bindings", [], lock_failures)

    recovery_failures: list[str] = []
    lock_payload_rows = _rows(
        sidecar,
        "SELECT * FROM review_lock_payloads WHERE run_id=? ORDER BY lane",
        (run_id,),
    )
    lock_payload_by_lane = {str(row["lane"]): row for row in lock_payload_rows}
    if set(lock_payload_by_lane) != {"r1", "r2"}:
        recovery_failures.append("review_lock_payload_roles")
    for lane in ("r1", "r2"):
        payload_row = lock_payload_by_lane.get(lane)
        lock_row = by_lane_lock.get(lane)
        if payload_row is None or lock_row is None:
            continue
        try:
            stored_payload = json.loads(str(payload_row["payload_json"]))
            file_payload = json.loads(
                _resolve(root, str(lock_row["locked_output_path"])).read_text(
                    encoding="utf-8"
                )
            )
            if stored_payload != file_payload:
                recovery_failures.append(f"{lane}:payload_file_binding")
            if (
                stored_payload.get("lane") != lane
                or stored_payload.get("run_id") != run_id
                or stored_payload.get("ordered_ledger_sha256")
                != lock_row["ordered_ledger_sha256"]
                or int(stored_payload.get("row_count", -1)) != TOTAL_SAMPLE
            ):
                recovery_failures.append(f"{lane}:payload_contract")
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            recovery_failures.append(f"{lane}:payload_json")

    unblind_payload_rows = _rows(
        sidecar,
        "SELECT * FROM unblind_payloads WHERE run_id=?",
        (run_id,),
    )
    if len(unblind_payload_rows) != 1 or len(unblind) != 1:
        recovery_failures.append("unblind_payload_count")
    else:
        try:
            stored_packet = json.loads(str(unblind_payload_rows[0]["packet_json"]))
            stored_form = json.loads(str(unblind_payload_rows[0]["form_json"]))
            file_packet = json.loads(
                _resolve(root, str(unblind[0]["packet_path"])).read_text(
                    encoding="utf-8"
                )
            )
            file_form = json.loads(
                _resolve(root, str(unblind[0]["form_path"])).read_text(
                    encoding="utf-8"
                )
            )
            if stored_packet != file_packet:
                recovery_failures.append("unblind:packet_payload_file_binding")
            if stored_form != file_form:
                recovery_failures.append("unblind:form_payload_file_binding")
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            recovery_failures.append("unblind:recovery_payload_json")
    _check(
        checks,
        "recovery_payloads_bind_locked_and_unblind_artifacts",
        [],
        recovery_failures,
    )

    blind_files = sorted(
        {
            _resolve(root, str(row[key]))
            for row in locks
            for key in ("completed_form_path", "locked_output_path")
        },
        key=str,
    )
    leakage = {
        str(path): _blind_json_leaks(path, candidate_ids) for path in blind_files
    }
    _check(checks, "blind_deliverables_hide_policy_and_identity", {}, {key: value for key, value in leakage.items() if value})

    adjudications = _rows(
        sidecar,
        "SELECT * FROM adjudications WHERE run_id=? ORDER BY sample_id",
        (run_id,),
    )
    _check(checks, "adjudication_count", TOTAL_SAMPLE, len(adjudications))
    review_by_sample_lane = {
        (str(row["sample_id"]), str(row["lane"])): row for row in reviews
    }
    adjudication_failures: list[str] = []
    adjudicator_actors: set[str] = set()
    for row in adjudications:
        sample_id = str(row["sample_id"])
        r1 = review_by_sample_lane.get((sample_id, "r1"))
        r2 = review_by_sample_lane.get((sample_id, "r2"))
        if r1 is None or r2 is None:
            adjudication_failures.append(f"{sample_id}:reviews")
            continue
        if row["r1_label"] != r1["label"] or row["r2_label"] != r2["label"]:
            adjudication_failures.append(f"{sample_id}:review_binding")
        agreement = int(r1["label"] == r2["label"])
        if int(row["reviewer_agreement"]) != agreement:
            adjudication_failures.append(f"{sample_id}:agreement")
        if row["adjudicator_label"] not in REVIEW_LABELS or row["final_gold_label"] not in REVIEW_LABELS:
            adjudication_failures.append(f"{sample_id}:label")
        if int(row["unresolved_disagreement"]) and row["final_gold_label"] != "insufficient":
            adjudication_failures.append(f"{sample_id}:unresolved_not_insufficient")
        if not int(row["unresolved_disagreement"]) and row["final_gold_label"] != row["adjudicator_label"]:
            adjudication_failures.append(f"{sample_id}:final_not_adjudicator")
        if not str(row["rationale"]).strip():
            adjudication_failures.append(f"{sample_id}:rationale")
        adjudicator_actors.add(str(row["adjudicator"]).strip().casefold())
    if len(adjudicator_actors) != 1:
        adjudication_failures.append("adjudicator_actor_count")
    if reviewer_actors & adjudicator_actors:
        adjudication_failures.append("adjudicator_not_independent")
    _check(checks, "adjudication_bindings_labels_and_actor_isolation", [], adjudication_failures)
    metrics["review"] = {
        "reviewer_actors": sorted(reviewer_actors),
        "adjudicator_actors": sorted(adjudicator_actors),
        "reviewer_disagreements": sum(
            int(row["r1_label"] != row["r2_label"]) for row in adjudications
        ),
        "unresolved_disagreements": sum(
            int(row["unresolved_disagreement"]) for row in adjudications
        ),
    }
    return adjudications


def _metric_field(row: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in row:
            return row[name]
    raise KeyError(names[0])


def _validate_metrics(
    sidecar: sqlite3.Connection,
    run: Mapping[str, Any],
    samples: Sequence[Mapping[str, Any]],
    adjudications: Sequence[Mapping[str, Any]],
    checks: list[Check],
    metrics: dict[str, Any],
) -> None:
    run_id = str(run["run_id"])
    sample_stratum = {str(row["sample_id"]): str(row["stratum"]) for row in samples}
    gold: dict[str, list[str]] = defaultdict(list)
    for row in adjudications:
        gold[sample_stratum[str(row["sample_id"])]].append(str(row["final_gold_label"]))
    stored = {
        str(row["stratum"]): dict(row)
        for row in sidecar.execute("SELECT * FROM band_metrics WHERE run_id=?", (run_id,))
    }
    _check(checks, "band_metric_strata_exact", set(STRATA), set(stored))
    failures: list[str] = []
    derived: dict[str, Any] = {}
    for stratum in STRATA:
        labels = gold.get(stratum, [])
        counts = Counter(labels)
        n = len(labels)
        same = counts["same"]
        low, high = clopper_pearson_95(same, n)
        error_count = n - same
        if stratum == "S5":
            equivalent = error_count * 60.0 / n
            grade = "r1_rule_guard"
            bulk_excluded = counts["different"] > 0
            status = "FAIL" if same else "PASS"
        else:
            equivalent, grade = candidate_grade(error_count, n)
            bulk_excluded = counts["different"] > 0
            status = "PASS"
        expected = {
            "sample_n": n,
            "same_count": counts["same"],
            "related_count": counts["related"],
            "different_count": counts["different"],
            "insufficient_count": counts["insufficient"],
            "precision": same / n,
            "cp95_low": low,
            "cp95_high": high,
            "error_count": error_count,
            "equivalent_errors_60": equivalent,
            "candidate_grade": grade,
            "bulk_excluded": int(bulk_excluded),
            "status": status,
        }
        derived[stratum] = expected
        row = stored.get(stratum)
        if row is None:
            continue
        for key, value in expected.items():
            try:
                actual = _metric_field(row, key)
            except KeyError:
                failures.append(f"{stratum}:{key}:missing")
                continue
            if isinstance(value, float):
                if actual is None or not math.isclose(float(actual), value, rel_tol=0.0, abs_tol=1e-12):
                    failures.append(f"{stratum}:{key}")
            elif value != actual:
                failures.append(f"{stratum}:{key}")
    _check(checks, "band_metrics_independent_recalculation", [], failures)
    s5_same = derived.get("S5", {}).get("same_count", 0)
    expected_gate = "fail" if s5_same else "pass"
    _check(checks, "s5_rule_gap_gate", expected_gate, str(run["gate_status"]).casefold())
    _check(checks, "terminal_run_status_matches_s5", expected_gate, str(run["status"]).casefold())
    metrics["bands"] = derived
    metrics["s5_same_count"] = s5_same


def _validate_final_artifacts(
    root: Path,
    artifact: Path,
    sidecar: sqlite3.Connection,
    run: Mapping[str, Any],
    samples: Sequence[Mapping[str, Any]],
    adjudications: Sequence[Mapping[str, Any]],
    checks: list[Check],
    metrics: Mapping[str, Any],
    *,
    fetch_manifest_path: Path,
    fetch_report_path: Path,
    locked_adjudication_path: Path,
    stage_report_path: Path,
    manifest_path: Path,
    job_card_path: Path,
) -> None:
    run_id = str(run["run_id"])
    failures: list[str] = []
    quota: list[dict[str, Any]] = []
    for relative in (
        fetch_manifest_path,
        fetch_report_path,
        locked_adjudication_path,
        stage_report_path,
        manifest_path,
        job_card_path,
    ):
        path = _resolve(root, relative)
        if not path.is_file() or path.stat().st_size == 0:
            failures.append(f"{relative}:missing")
    try:
        quota = _rows(
            sidecar,
            "SELECT observation_no,phase,account_index,observed_at,signals_json,"
            "gate_basis_remaining_percent,start_allowed,stop_required,limitation "
            "FROM quota_observations WHERE run_id=? ORDER BY observation_no",
            (run_id,),
        )
        for row in quota:
            row["signals"] = _json_value(row.pop("signals_json"))
        bands: dict[str, Any] = {}
        for stratum, value in metrics["bands"].items():
            bands[stratum] = {
                "stratum": stratum,
                "sample_n": value["sample_n"],
                "label_counts": {
                    label: value[f"{label}_count"] for label in sorted(REVIEW_LABELS)
                },
                "same_count": value["same_count"],
                "related_count": value["related_count"],
                "different_count": value["different_count"],
                "insufficient_count": value["insufficient_count"],
                "precision": value["precision"],
                "clopper_pearson_95": {
                    "low": value["cp95_low"],
                    "high": value["cp95_high"],
                },
                "error_count": value["error_count"],
                "equivalent_errors_60": value["equivalent_errors_60"],
                "candidate_grade": value["candidate_grade"],
                "bulk_excluded": bool(value["bulk_excluded"]),
                "status": value["status"],
            }
        adjudication_by_sample = {
            str(row["sample_id"]): row for row in adjudications
        }
        evidence_memo: list[dict[str, Any]] = []
        for sample in samples:
            adjudication = adjudication_by_sample[str(sample["sample_id"])]
            evidence_memo.append(
                {
                    "candidate_id": str(sample["candidate_id"]),
                    "stratum": str(sample["stratum"]),
                    "final_gold_label": str(adjudication["final_gold_label"]),
                    "r1_label": str(adjudication["r1_label"]),
                    "r2_label": str(adjudication["r2_label"]),
                    "unresolved_disagreement": bool(
                        adjudication["unresolved_disagreement"]
                    ),
                    "adjudicator_rationale": str(adjudication["rationale"]),
                }
            )
        s5_same = int(metrics["s5_same_count"])
        terminal_status = "FAIL" if s5_same else "PASS"
        expected_report = {
            "status": terminal_status,
            "run_id": run_id,
            "measurement_only": True,
            "authorizes_tiering": False,
            "human_decision_required": True,
            "bands": bands,
            "s5_rule_gate": {
                "status": bands["S5"]["status"],
                "same_count": s5_same,
                "r1_six_year_rule_fail": bool(s5_same),
                "hold_s1_start": bool(s5_same),
            },
            "review": {
                "r1_rows": TOTAL_SAMPLE,
                "r2_rows": TOTAL_SAMPLE,
                "adjudicated_rows": TOTAL_SAMPLE,
                "disagreements": metrics["review"]["reviewer_disagreements"],
                "unresolved_disagreements": metrics["review"][
                    "unresolved_disagreements"
                ],
            },
            "evidence_memo": evidence_memo,
            "fetch": {
                "asset_occurrences": metrics["fetch"]["asset_occurrences"],
                "unique_assets": metrics["fetch"]["unique_assets"],
                "logical_http_fetches": metrics["fetch"]["logical_attempts"],
                "wire_http_requests": metrics["fetch"]["wire_requests"],
                "status_counts": metrics["fetch"]["status_counts"],
                "redirects": 0,
                "retries": 0,
            },
            "quota_observations": quota,
            "vision_requests": 0,
            "llm_requests": 0,
            "merge_executions": 0,
            "vnext_id_issuances": 0,
        }
        report_file = _resolve(root, stage_report_path)
        report = json.loads(report_file.read_text(encoding="utf-8"))
        if report != expected_report:
            failures.append("report:payload")

        ordered_samples = sorted(samples, key=lambda row: int(row["sample_rank"]))
        adjudicator_actors = {str(row["adjudicator"]) for row in adjudications}
        adjudicated_times = {str(row["adjudicated_at"]) for row in adjudications}
        if len(adjudicator_actors) != 1 or len(adjudicated_times) != 1:
            failures.append("final_payload:adjudicator_or_time_not_singular")
            adjudicator = ""
            adjudicated_at = ""
        else:
            adjudicator = next(iter(adjudicator_actors))
            adjudicated_at = next(iter(adjudicated_times))
        expected_locked_items: list[dict[str, Any]] = []
        for sample in ordered_samples:
            adjudication = adjudication_by_sample[str(sample["sample_id"])]
            expected_locked_items.append(
                {
                    "candidate_id": str(sample["candidate_id"]),
                    "sample_id": str(sample["sample_id"]),
                    "r1_label": str(adjudication["r1_label"]),
                    "r2_label": str(adjudication["r2_label"]),
                    "reviewer_agreement": int(adjudication["reviewer_agreement"]),
                    "adjudicator_label": str(adjudication["adjudicator_label"]),
                    "final_gold_label": str(adjudication["final_gold_label"]),
                    "unresolved_disagreement": int(
                        adjudication["unresolved_disagreement"]
                    ),
                    "rationale": str(adjudication["rationale"]),
                    "adjudicator": str(adjudication["adjudicator"]),
                    "adjudicated_at": str(adjudication["adjudicated_at"]),
                }
            )
        expected_locked = {
            "adjudication_version": ADJUDICATION_VERSION,
            "run_id": run_id,
            "adjudicator": adjudicator,
            "adjudicated_at": adjudicated_at,
            "row_count": TOTAL_SAMPLE,
            "items": expected_locked_items,
        }
        locked_file = _resolve(root, locked_adjudication_path)
        locked_payload = json.loads(locked_file.read_text(encoding="utf-8"))
        if locked_payload != expected_locked:
            failures.append("locked_adjudication:payload")

        final_payload_rows = _rows(
            sidecar,
            "SELECT * FROM final_payloads WHERE run_id=?",
            (run_id,),
        )
        if len(final_payload_rows) != 1:
            failures.append("final_payload:row_count")
        else:
            final_payload = final_payload_rows[0]
            try:
                stored_locked = json.loads(
                    str(final_payload["locked_adjudication_json"])
                )
                stored_report = json.loads(str(final_payload["report_json"]))
                if str(final_payload["status"]) != terminal_status:
                    failures.append("final_payload:status")
                if stored_locked != expected_locked or stored_locked != locked_payload:
                    failures.append("final_payload:locked_binding")
                if stored_report != expected_report or stored_report != report:
                    failures.append("final_payload:report_binding")
            except (json.JSONDecodeError, TypeError, ValueError):
                failures.append("final_payload:json")

        cache_pins = _rows(
            sidecar,
            "SELECT role,path,transform_version,access_mode,file_count_before,"
            "total_bytes_before,tree_sha256_before,file_count_after,total_bytes_after,"
            "tree_sha256_after FROM cache_pins WHERE run_id=? ORDER BY role",
            (run_id,),
        )
        binding_rows = _rows(
            sidecar,
            "SELECT role,path,size_bytes,sha256,stage FROM artifact_bindings "
            "WHERE run_id=? ORDER BY role",
            (run_id,),
        )
        artifact_paths = {
            str(row["role"]): row
            for row in _rows(
                sidecar,
                "SELECT role,path,stage FROM artifact_paths WHERE run_id=? ORDER BY role",
                (run_id,),
            )
        }
        final_manifest_row = artifact_paths.get("final_manifest")
        if final_manifest_row is None:
            raise KeyError("final_manifest")
        manifest_artifacts = {
            str(row["role"]): {
                "path": str(row["path"]),
                "size_bytes": int(row["size_bytes"]),
                "sha256": str(row["sha256"]),
                "stage": str(row["stage"]),
            }
            for row in binding_rows
        }
        expected_manifest = {
            "status": terminal_status,
            "run_id": run_id,
            "versions": {
                "runner": "vnext-identity-band-precision-v1.0.0",
                "sample": SAMPLE_VERSION,
                "review": REVIEW_VERSION,
                "adjudication": ADJUDICATION_VERSION,
                "gate": GATE_VERSION,
            },
            "sidecar": {
                "path": artifact.resolve().relative_to(root).as_posix(),
                "size_bytes": artifact.stat().st_size,
                "sha256": _sha256_file(artifact),
            },
            "manifest_path": str(final_manifest_row["path"]),
            "sample_logical_sha256": str(run["sample_logical_sha256"]),
            "cache_pins": cache_pins,
            "artifacts": manifest_artifacts,
            "artifact_provenance_sha256": _record_sha(manifest_artifacts),
            "review_rows": {
                "r1": TOTAL_SAMPLE,
                "r2": TOTAL_SAMPLE,
                "adjudicated": TOTAL_SAMPLE,
            },
            "quota_observations": quota,
            "network_requests": metrics["fetch"]["wire_requests"],
            "vision_requests": 0,
            "llm_requests": 0,
            "merge_executions": 0,
            "vnext_id_issuances": 0,
            "measurement_only": True,
            "human_decision_required": True,
        }
        manifest = json.loads(
            _resolve(root, manifest_path).read_text(encoding="utf-8")
        )
        if manifest != expected_manifest:
            failures.append("manifest:payload_or_hash")
    except (OSError, json.JSONDecodeError, TypeError, ValueError, KeyError):
        failures.append("manifest_or_report:json")
    try:
        job_text = _resolve(root, job_card_path).read_text(encoding="utf-8")
        gate = float(quota[0]["gate_basis_remaining_percent"]) if quota else -1.0
        quota_tokens = {f"{gate:g}%", f"{gate:.1f}%"}
        if not any(token in job_text for token in quota_tokens):
            failures.append("job_card:dynamic_quota")
        for token in ("S1", "S5", "Clopper", "fetch"):
            if token.casefold() not in job_text.casefold():
                failures.append(f"job_card:{token}")
    except OSError:
        failures.append("job_card:read")
    _check(checks, "terminal_manifest_report_and_job_card", [], failures)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((_json_safe(item) for item in value), key=_canonical_json)
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    return value


def _write_report(result: ValidationResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "validator_version": VALIDATOR_VERSION,
        "status": "PASS" if result.passed else "FAIL",
        "phase": result.phase,
        "run_id": result.run_id,
        "passed_check_count": sum(check.passed for check in result.checks),
        "total_check_count": len(result.checks),
        "failed_checks": list(result.failed_check_names),
        "checks": [
            {
                "name": check.name,
                "passed": check.passed,
                "expected": _json_safe(check.expected),
                "actual": _json_safe(check.actual),
                "detail": check.detail,
            }
            for check in result.checks
        ],
        "metrics": _json_safe(result.metrics),
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def validate_identity_band_precision(
    *,
    repo_root: Path,
    artifact_path: Path = DEFAULT_ARTIFACT,
    report_path: Path | None = DEFAULT_REPORT,
    sample_manifest_path: Path = DEFAULT_SAMPLE_MANIFEST,
    fetch_manifest_path: Path = DEFAULT_FETCH_MANIFEST,
    fetch_report_path: Path = DEFAULT_FETCH_REPORT,
    locked_adjudication_path: Path = DEFAULT_ADJUDICATION_LOCKED,
    stage_report_path: Path = DEFAULT_STAGE_REPORT,
    manifest_path: Path = DEFAULT_MANIFEST,
    job_card_path: Path = DEFAULT_JOB_CARD,
    verify_full_hashes: bool = True,
    phase: str = "final",
) -> ValidationResult:
    """Independently validate the frozen sample or terminal WP2.5 run."""

    if phase not in {"sample", "final"}:
        raise ValueError("phase must be 'sample' or 'final'")
    root = repo_root.resolve()
    artifact = _resolve(root, artifact_path)
    checks: list[Check] = []
    metrics: dict[str, Any] = {"validator_version": VALIDATOR_VERSION}
    run_id: str | None = None
    if not artifact.is_file():
        _check(checks, "artifact_exists", True, False)
        result = ValidationResult(False, phase, None, tuple(checks), metrics, report_path)
        if report_path is not None:
            resolved_report = _resolve(root, report_path)
            result = ValidationResult(False, phase, None, tuple(checks), metrics, resolved_report)
            _write_report(result, resolved_report)
        return result

    _check(checks, "artifact_has_no_sqlite_sidecars", True, _no_sqlite_sidecars(artifact))
    try:
        with _open_readonly(artifact) as sidecar:
            _check(
                checks,
                "sqlite_application_id",
                0x57503235,
                int(sidecar.execute("PRAGMA application_id").fetchone()[0]),
            )
            _check(
                checks,
                "sqlite_user_version",
                1,
                int(sidecar.execute("PRAGMA user_version").fetchone()[0]),
            )
            _check(
                checks,
                "sqlite_foreign_key_check",
                [],
                [tuple(row) for row in sidecar.execute("PRAGMA foreign_key_check")],
            )
            present = _tables(sidecar)
            _check(checks, "required_tables", set(REQUIRED_TABLES), present & set(REQUIRED_TABLES))
            missing = REQUIRED_TABLES - present
            if missing:
                raise ValueError("missing tables: " + ",".join(sorted(missing)))
            runs = _rows(sidecar, "SELECT * FROM runs")
            _check(checks, "single_run", 1, len(runs))
            if len(runs) != 1:
                raise ValueError("sidecar must contain exactly one run")
            run = runs[0]
            run_id = str(run["run_id"])
            _check(
                checks,
                "run_versions",
                (
                    1,
                    "vnext-identity-band-precision-v1.0.0",
                    REVIEW_VERSION,
                    ADJUDICATION_VERSION,
                    GATE_VERSION,
                ),
                (
                    int(run["schema_version"]),
                    str(run["runner_version"]),
                    str(run["review_version"]),
                    str(run["adjudication_version"]),
                    str(run["gate_version"]),
                ),
            )
            _check(checks, "sample_version", SAMPLE_VERSION, run["sample_version"])
            _check(checks, "sample_seed", SAMPLE_SEED, run["seed"])
            _check(checks, "run_sample_count", TOTAL_SAMPLE, int(run["sample_count"]))
            _validate_artifact_provenance(
                root,
                sidecar,
                run_id,
                checks,
                phase=phase,
                verify_full_hashes=verify_full_hashes,
                explicit_paths={
                    "sample_manifest": sample_manifest_path,
                    "fetch_manifest": fetch_manifest_path,
                    "fetch_report": fetch_report_path,
                    "locked_adjudication": locked_adjudication_path,
                    "final_report": stage_report_path,
                    "final_manifest": manifest_path,
                },
            )
            with _open_readonly(_resolve(root, DEFAULT_V4)) as v4:
                sample_state = _validate_sample(
                    root,
                    sidecar,
                    v4,
                    run,
                    checks,
                    metrics,
                    phase=phase,
                    verify_full_hashes=verify_full_hashes,
                    sample_manifest_path=sample_manifest_path,
                )
            _validate_state_events(sidecar, run, phase, checks)
            if phase == "final":
                _validate_cache_pins(
                    root,
                    sidecar,
                    run_id,
                    checks,
                    verify_full_hashes=verify_full_hashes,
                )
                _validate_fetch(root, sidecar, run, checks, metrics)
                adjudications = _validate_reviews(
                    root,
                    sidecar,
                    run,
                    sample_state["samples"],
                    checks,
                    metrics,
                )
                _validate_metrics(
                    sidecar,
                    run,
                    sample_state["samples"],
                    adjudications,
                    checks,
                    metrics,
                )
                _check(
                    checks,
                    "all_record_sha256s",
                    [],
                    _validate_record_hashes(sidecar, REQUIRED_TABLES),
                )
                _validate_final_artifacts(
                    root,
                    artifact,
                    sidecar,
                    run,
                    sample_state["samples"],
                    adjudications,
                    checks,
                    metrics,
                    fetch_manifest_path=fetch_manifest_path,
                    fetch_report_path=fetch_report_path,
                    locked_adjudication_path=locked_adjudication_path,
                    stage_report_path=stage_report_path,
                    manifest_path=manifest_path,
                    job_card_path=job_card_path,
                )
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        checks.append(
            Check(
                "validator_completed_without_exception",
                False,
                "no exception",
                f"{type(exc).__name__}: {exc}",
            )
        )

    resolved_report = None if report_path is None else _resolve(root, report_path)
    result = ValidationResult(
        all(check.passed for check in checks),
        phase,
        run_id,
        tuple(checks),
        metrics,
        resolved_report,
    )
    if resolved_report is not None:
        _write_report(result, resolved_report)
    return result
