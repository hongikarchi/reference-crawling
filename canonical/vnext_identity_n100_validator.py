"""Independent validator for the WP2.3 identity N100 overlay.

The validator deliberately does not import :mod:`vnext_identity_n100`.  Its
protocol constants, sampling predicates, statistical calculations, and row
hash replay live here so a producer defect cannot validate itself.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


VALIDATOR_VERSION = "vnext-identity-n100-independent-validator-v1"
APPLICATION_ID = 0x4E313030
SCHEMA_VERSION = 1
RUNNER_VERSION = "vnext-identity-n100-v1.0.0"
SAMPLE_VERSION = "vnext-identity-wp23-n100-sample-v1"
REVIEW_VERSION = "vnext-identity-wp23-dual-blind-v1"
ADJUDICATION_VERSION = "vnext-identity-wp23-adjudication-v1"
GATE_VERSION = "vnext-identity-wp23-core-a70-gate-v1"

DEFAULT_ARTIFACT = Path("data/enrichment/vnext_identity_n100_v1.db")
DEFAULT_SAMPLE_MANIFEST = Path("data/reports/vnext_identity_n100_v1_sample_manifest.json")
DEFAULT_FETCH_MANIFEST = Path("data/reports/vnext_identity_n100_v1_fetch_manifest.json")
DEFAULT_FETCH_REPORT = Path("data/reports/vnext_identity_n100_v1_fetch_report.json")
DEFAULT_SHEETS_ROOT = Path("data/reports/vnext_identity_n100_v1_blind")
DEFAULT_R1_FORM = Path("data/reports/vnext_identity_n100_v1_reviewer_r1_draft.json")
DEFAULT_R2_FORM = Path("data/reports/vnext_identity_n100_v1_reviewer_r2_draft.json")
DEFAULT_ADJUDICATION_PACKET = Path(
    "data/reports/vnext_identity_n100_v1_adjudication_packet.json"
)
DEFAULT_ADJUDICATION_FORM = Path(
    "data/reports/vnext_identity_n100_v1_adjudication_draft.json"
)
DEFAULT_MANIFEST = Path("data/reports/vnext_identity_n100_v1_manifest.json")
DEFAULT_STAGE_REPORT = Path("data/reports/vnext_identity_n100_v1_report.json")
DEFAULT_GATE_REPORT = Path("data/reports/vnext_identity_n100_v1_gate.json")
DEFAULT_ADJUDICATION_LOCKED = Path(
    "data/reports/vnext_identity_n100_v1_adjudication_locked.json"
)
DEFAULT_REPORT = Path("data/reports/vnext_identity_n100_v1_validation.json")
DEFAULT_V3 = Path("data/enrichment/vnext_identity_candidates_v3.db")
DEFAULT_PROTOCOL = Path("docs/vnext/IDENTITY_N100_PROTOCOL.md")
DEFAULT_ORDER = Path(".claude/ops/jobs/20260826_vnext_wp23_identity_n100_order.md")
DEFAULT_OUTPUT_CACHE = Path("data/enrichment/vnext_identity_n100_cache_v1")

V3_SIZE_BYTES = 580_710_400
V3_SHA256 = "c71934cfd30807e523842133ffeb7e47d5a863d539feecd763d1086fd65d7a2f"
SAMPLE_SEED = "vnext-wp23-identity-n100-v1-20260826"

A_FULL_POPULATION = 4_169
A_REVIEWED_ELIGIBLE = 19
A_FRAME_POPULATION = 4_150
A_SAMPLE_COUNT = 70
B_SAMPLE_COUNT = 40
TOTAL_SAMPLE_COUNT = 110
FRESH_SAMPLE_COUNT = 105
CARRY_FORWARD_COUNT = 5
REVIEW_ROWS_PER_REVIEWER = 105
TOTAL_DUAL_REVIEW_ROWS = 210
FRESH_ADJUDICATION_COUNT = 105
PRIOR_FOCAL_REVIEW_COUNT = 30

REVIEW_LABELS = frozenset({"same", "related", "different", "insufficient"})
GOLD_DECISIONS = {
    "same": "merge",
    "related": "related_but_separate",
    "different": "reject",
    "insufficient": "defer",
}
REVIEWER_SLOTS = ("reviewer_a", "reviewer_b")
LANES = ("r1", "r2")
MAX_ASSETS_PER_SIDE = 2
MAX_ASSET_OCCURRENCES = 420
MAX_LOGICAL_HTTP_FETCHES = 420
MAX_WIRE_HTTP_REQUESTS = 500
READABLE_ASSET_STATUSES = frozenset(
    {"cache_hit", "exact", "delivery_changed", "source_changed"}
)
TERMINAL_ASSET_STATUSES = READABLE_ASSET_STATUSES | {"failed"}

PRIOR_DB_SPECS: Mapping[str, tuple[Path, int, str]] = {
    "wp2_v1_review_db": (
        Path("data/enrichment/vnext_identity_candidates_v1.db"),
        511_156_224,
        "0541b4c86bb2def88bbadaf53eec33c62d8f14e62800f1ceb8d96761f3aea5c4",
    ),
    "wp21_v2_review_db": (
        Path("data/enrichment/vnext_identity_candidates_v2.db"),
        581_292_032,
        "08e28e4e5a1ab5aba9ea5ddd53ca615f417458a6693cdfb1aefc822d57b5b0c4",
    ),
}

READ_CACHE_SPECS: Mapping[str, tuple[Path, int, int, str]] = {
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
}

SOURCE_DB_SPECS: Mapping[str, tuple[Path, int, str]] = {
    "e2_image_evidence": (
        Path("data/enrichment/divisare_architizer_image_evidence_e2_full_v5.db"),
        10_164_682_752,
        "4728f9f015d7fba303923df40f550c59835a7e2a5c1316eef03c499ee3ca7b19",
    ),
    "e1_divisare": (
        Path("data/enrichment/divisare_image_fingerprints_e1_full_v1_2.db"),
        2_646_114_304,
        "869a79fee9fd65ddeffa299fef4dd9e2ba15a9c7c7170964b03fee1f4c96a819",
    ),
    "e1_architizer": (
        Path("data/enrichment/architizer_image_fingerprints_e1_full_v1_2.db"),
        4_373_962_752,
        "58aecdcda936f7327ef7bb4bf3fe21a39ad070e784ab7061e989b62c2dcfe937",
    ),
}

WP1_TRANSFORM_VERSION = "vnext-p4b-shared-1024-jpeg-q92-s0-v1"
REVIEW_TRANSFORM_VERSION = "vnext-identity-review-1024-jpeg-q92-s0-v1"

REQUIRED_TABLES = frozenset(
    {
        "runs",
        "input_pins",
        "cache_pins",
        "review_exclusions",
        "carry_forward_gold",
        "population_cells",
        "sample_pairs",
        "candidate_snapshots",
        "sample_assets",
        "fetch_assets",
        "fetch_attempts",
        "review_items",
        "blind_reviews",
        "review_locks",
        "unblind_artifacts",
        "adjudications",
        "metrics",
        "state_events",
    }
)

CARRY_FORWARD: Mapping[str, tuple[str, ...]] = {
    "B1_year_gate": ("idc_bc3502139c26bf851b5be4d3de0870db",),
    "B2_phash_veto": ("idc_7e6d26b574f1d2bd73240aa26c188933",),
    "B3_fanout_defer": (
        "idc_681fba1ce7f631d10ddc1e5054e36134",
        "idc_89752449d8e8cb76e5728f71c352c410",
        "idc_32aae04217168527d72cde93a966437f",
    ),
    "B4_high_name_reject": (),
}

B_FULL_COUNTS = {
    "B1_year_gate": 86,
    "B2_phash_veto": 734,
    "B3_fanout_defer": 223,
    "B4_high_name_reject": 3_195,
}
B_FRESH_COUNTS = {
    "B1_year_gate": 84,
    "B2_phash_veto": 732,
    "B3_fanout_defer": 220,
    "B4_high_name_reject": 3_195,
}
B_FRESH_SAMPLE_COUNTS = {
    "B1_year_gate": 9,
    "B2_phash_veto": 9,
    "B3_fanout_defer": 7,
    "B4_high_name_reject": 10,
}
B_TOTAL_SAMPLE_COUNTS = {
    "B1_year_gate": 10,
    "B2_phash_veto": 10,
    "B3_fanout_defer": 10,
    "B4_high_name_reject": 10,
}

# Hierarchical Hamilton allocation: anchor type first, auxiliary-positive
# combination second.  Full vocabulary names are used to prevent alias drift.
A_QUOTAS: Mapping[tuple[str, str], int] = {
    ("both", "none"): 1,
    ("both", "architect_firm_identity"): 2,
    ("both", "architect_firm_identity+location_site_identity"): 0,
    ("both", "architect_firm_identity+location_site_identity+year_phase_compatibility"): 2,
    ("both", "architect_firm_identity+year_phase_compatibility"): 34,
    ("both", "location_site_identity+year_phase_compatibility"): 0,
    ("both", "year_phase_compatibility"): 6,
    ("image", "architect_firm_identity"): 2,
    ("image", "architect_firm_identity+location_site_identity"): 0,
    ("image", "architect_firm_identity+location_site_identity+year_phase_compatibility"): 1,
    ("image", "architect_firm_identity+year_phase_compatibility"): 17,
    ("image", "location_site_identity+year_phase_compatibility"): 0,
    ("image", "year_phase_compatibility"): 4,
    ("name", "architect_firm_identity"): 0,
    ("name", "architect_firm_identity+location_site_identity"): 0,
    ("name", "architect_firm_identity+year_phase_compatibility"): 1,
    ("name", "year_phase_compatibility"): 0,
}

A_FRAME_COUNTS: Mapping[tuple[str, str], int] = {
    ("both", "none"): 47,
    ("both", "architect_firm_identity"): 112,
    ("both", "architect_firm_identity+location_site_identity"): 2,
    ("both", "architect_firm_identity+location_site_identity+year_phase_compatibility"): 116,
    ("both", "architect_firm_identity+year_phase_compatibility"): 2_014,
    ("both", "location_site_identity+year_phase_compatibility"): 22,
    ("both", "year_phase_compatibility"): 374,
    ("image", "architect_firm_identity"): 97,
    ("image", "architect_firm_identity+location_site_identity"): 2,
    ("image", "architect_firm_identity+location_site_identity+year_phase_compatibility"): 60,
    ("image", "architect_firm_identity+year_phase_compatibility"): 1_029,
    ("image", "location_site_identity+year_phase_compatibility"): 8,
    ("image", "year_phase_compatibility"): 240,
    ("name", "architect_firm_identity"): 6,
    ("name", "architect_firm_identity+location_site_identity"): 1,
    ("name", "architect_firm_identity+year_phase_compatibility"): 12,
    ("name", "year_phase_compatibility"): 8,
}


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    expected: Any
    actual: Any
    detail: str | None = None


@dataclass(frozen=True)
class ValidationResult:
    artifact_path: Path
    report_path: Path | None
    run_id: str | None
    checks: tuple[Check, ...]
    metrics: Mapping[str, Any]
    phase: str = "final"

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(row.passed for row in self.checks)

    @property
    def failed_check_names(self) -> tuple[str, ...]:
        return tuple(row.name for row in self.checks if not row.passed)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _record_sha(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _artifact_json_sha(payload: Mapping[str, Any]) -> str:
    """Hash the exact pretty-printed JSON byte contract used for artifacts."""

    raw = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _blank_reviewer_form_payload(
    run_id: str, lane: str, lane_items: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    items = [
        {
            "display_order": int(item["display_order"]),
            "opaque_review_id": str(item["opaque_review_id"]),
            "sheet_path": str(item["sheet_path"]),
            "sheet_sha256": str(item["sheet_sha256"]),
            "label": "",
            "rationale": "",
            "hard_conflict_observed": "",
        }
        for item in sorted(lane_items, key=lambda row: int(row["display_order"]))
    ]
    contract = {
        "review_version": REVIEW_VERSION,
        "run_id": run_id,
        "lane": lane,
        "items": [
            {
                "display_order": item["display_order"],
                "opaque_review_id": item["opaque_review_id"],
                "sheet_path": item["sheet_path"],
                "sheet_sha256": item["sheet_sha256"],
            }
            for item in items
        ],
    }
    return {
        **contract,
        "form_contract_sha256": _record_sha(contract),
        "reviewer": "",
        "reviewed_at": "",
        "allowed_labels": sorted(REVIEW_LABELS),
        "items": items,
    }


def _blank_adjudication_form_payload(
    run_id: str,
    samples: Sequence[Mapping[str, Any]],
    reviews: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    review_by_key = {
        (str(row["sample_id"]), str(row["lane"])): row for row in reviews
    }
    items: list[dict[str, Any]] = []
    for sample in sorted(samples, key=lambda row: int(row["sample_rank"])):
        sample_id = str(sample["sample_id"])
        items.append(
            {
                "sample_rank": int(sample["sample_rank"]),
                "sample_id": sample_id,
                "candidate_id": str(sample["candidate_id"]),
                "r1_label": str(review_by_key[(sample_id, "r1")]["label"]),
                "r2_label": str(review_by_key[(sample_id, "r2")]["label"]),
                "adjudicator_label": "",
                "unresolved_disagreement": False,
                "rationale": "",
            }
        )
    contract = {
        "adjudication_version": ADJUDICATION_VERSION,
        "run_id": run_id,
        "items": [
            {
                "sample_rank": item["sample_rank"],
                "sample_id": item["sample_id"],
                "candidate_id": item["candidate_id"],
                "r1_label": item["r1_label"],
                "r2_label": item["r2_label"],
            }
            for item in items
        ],
    }
    return {
        **contract,
        "form_contract_sha256": _record_sha(contract),
        "adjudicator": "",
        "adjudicated_at": "",
        "allowed_labels": sorted(REVIEW_LABELS),
        "items": items,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(16 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_rank(lane: str, candidate_id: str) -> str:
    return hashlib.sha256(
        "\0".join((SAMPLE_VERSION, SAMPLE_SEED, lane, candidate_id)).encode("utf-8")
    ).hexdigest()


def _open_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        "file:" + path.resolve().as_posix() + "?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _resolve_repo_path(root: Path, value: str | Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _no_sqlite_sidecars(path: Path) -> bool:
    return not any(
        Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")
    )


def _json_value(raw: Any) -> Any:
    if raw is None or not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _cache_tree(root: Path) -> tuple[int, int, str]:
    """Independently hash a cache tree using the pinned path/byte protocol."""

    if root.exists() and not root.is_dir():
        raise ValueError(f"cache root is not a directory: {root}")
    rows: list[dict[str, Any]] = []
    total = 0
    if root.exists():
        paths = sorted(
            (path for path in root.rglob("*") if path.is_file()),
            key=lambda path: path.relative_to(root).as_posix(),
        )
        for path in paths:
            size = path.stat().st_size
            rows.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "sha256": _sha256_file(path),
                    "size": size,
                }
            )
            total += size
    return len(rows), total, hashlib.sha256(
        _canonical_json(rows).encode("utf-8")
    ).hexdigest()


def _cache_asset_path(root: Path, pixel_sha256: str, transform_version: str) -> Path:
    transform_key = hashlib.sha256(transform_version.encode("utf-8")).hexdigest()[:12]
    return root / pixel_sha256[:2] / f"{pixel_sha256}.{transform_key}.jpg"


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _trigger_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
    }


def _iso_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _rows(connection: sqlite3.Connection, sql: str, values: Sequence[Any] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql, values)]


def _sample_logical_sha(connection: sqlite3.Connection, run_id: str) -> str:
    digest = hashlib.sha256()
    digest.update((SAMPLE_VERSION + "\n").encode("utf-8"))
    for table, order in (
        ("input_pins", "role"),
        ("review_exclusions", "candidate_id,prior_version"),
        ("carry_forward_gold", "candidate_id"),
        ("population_cells", "cohort,stratum,anchor_type,auxiliary_mask"),
        ("sample_pairs", "sample_rank"),
        ("candidate_snapshots", "sample_id"),
    ):
        digest.update((table + "\n").encode("utf-8"))
        if table == "input_pins":
            query = (
                "SELECT record_sha256 FROM input_pins WHERE run_id=? AND role IN "
                "('v3_candidates_final','n100_protocol','wp23_order') ORDER BY role"
            )
        else:
            query = f"SELECT record_sha256 FROM {table} WHERE run_id=? ORDER BY {order}"
        for row in connection.execute(query, (run_id,)):
            digest.update((str(row[0]) + "\n").encode("ascii"))
    return digest.hexdigest()


def _check(
    checks: list[Check], name: str, expected: Any, actual: Any, detail: str | None = None
) -> None:
    checks.append(Check(name, expected == actual, expected, actual, detail))


def _without_row_keys(row: Mapping[str, Any], *keys: str) -> dict[str, Any]:
    excluded = {"run_id", "record_sha256", *keys}
    return {key: value for key, value in row.items() if key not in excluded}


def _record_payload(table: str, row: Mapping[str, Any]) -> dict[str, Any]:
    """Reconstruct producer record payloads without importing producer code."""

    if table == "runs":
        return {key: value for key, value in row.items() if key != "record_sha256"}
    if table == "review_exclusions":
        return {
            "candidate_id": row["candidate_id"],
            "prior_version": row["prior_version"],
            "opaque_review_id": row["prior_opaque_review_id"],
            "carry_forward": row["carry_forward"],
        }
    if table == "candidate_snapshots":
        return {
            "sample_id": row["sample_id"],
            "snapshot_sha256": row["snapshot_sha256"],
        }
    if table == "state_events":
        return {
            "event_no": row["event_no"],
            "from_status": row["from_status"],
            "to_status": row["to_status"],
            "detail": _json_value(row["detail_json"]),
            "occurred_at": row["occurred_at"],
        }
    if table == "metrics":
        # Metrics are semantic JSON values; hash their parsed value rather than
        # accepting whitespace or key-order changes in the stored JSON text.
        return {
            "phase": row["phase"],
            "metric": row["metric"],
            "value": _json_value(row["value_json"]),
        }
    if table == "fetch_attempts":
        payload = _without_row_keys(row)
        payload["redirect_chain"] = _json_value(payload.pop("redirect_chain_json"))
        return payload
    return _without_row_keys(row)


def _record_sha_failures(
    connection: sqlite3.Connection, tables: Iterable[str]
) -> list[str]:
    failures: list[str] = []
    present = _table_names(connection)
    for table in sorted(set(tables) & present):
        columns = {
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if "record_sha256" not in columns:
            continue
        attempted_assets: set[str] = set()
        if table == "fetch_assets" and "fetch_attempts" in present:
            attempted_assets = {
                str(row[0]) for row in connection.execute("SELECT asset_key FROM fetch_attempts")
            }
        for index, raw in enumerate(connection.execute(f"SELECT * FROM {table}"), 1):
            row = dict(raw)
            try:
                if table == "fetch_assets" and str(row["asset_key"]) not in attempted_assets:
                    payload = {
                        key: row[key]
                        for key in (
                            "asset_key", "source", "source_asset_id", "expected_url",
                            "expected_raw_sha256", "expected_pixel_sha256", "status",
                            "cache_role", "cache_path", "error_kind", "error_message",
                        )
                    }
                else:
                    payload = _record_payload(table, row)
                actual = _record_sha(payload)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                failures.append(f"{table}[{index}]:payload:{type(exc).__name__}")
                continue
            if actual != row["record_sha256"]:
                identity = row.get("sample_id") or row.get("candidate_id") or row.get("role") or index
                failures.append(f"{table}[{identity}]")
    return failures


def _strong_image_count(row: Mapping[str, Any]) -> int:
    return sum(
        int(row.get(column, 0) or 0)
        for column in (
            "exact_asset_pair_count",
            "identical_phash_pair_count",
            "phash_le8_pair_count",
        )
    )


def anchor_type(row: Mapping[str, Any]) -> str:
    name = (
        bool(row.get("name_pos"))
        if "name_pos" in row
        else float(row.get("name_strict_score", 0.0) or 0.0) >= 90.0
    )
    image = (
        bool(row.get("image_pos"))
        if "image_pos" in row
        else _strong_image_count(row) > 0
    )
    if name and image:
        return "both"
    if name:
        return "name"
    if image:
        return "image"
    return "none"


def auxiliary_family_key(statuses: Mapping[str, str]) -> str:
    positives = tuple(
        family
        for family in (
            "architect_firm_identity",
            "location_site_identity",
            "source_url_external_stable_id",
            "year_phase_compatibility",
        )
        if statuses.get(family) == "positive"
    )
    return "+".join(positives) or "none"


def is_b1(row: Mapping[str, Any], conflicts: Sequence[str]) -> bool:
    return (
        row.get("proposal") == "reject"
        and int(row.get("positive_family_count", 0) or 0) >= 2
        and anchor_type(row) != "none"
        and tuple(sorted(conflicts)) == ("matcher_architect_year_gate_block",)
    )


def is_b2(row: Mapping[str, Any], conflicts: Sequence[str]) -> bool:
    return (
        row.get("proposal") == "reject"
        and int(row.get("positive_family_count", 0) or 0) >= 2
        and anchor_type(row) in {"name", "both"}
        and float(row.get("name_strict_score", 0.0) or 0.0) >= 90.0
        and tuple(sorted(conflicts)) == ("verified_phash_veto",)
    )


def is_b3(row: Mapping[str, Any], flags: Sequence[str]) -> bool:
    return row.get("proposal") == "defer" and "fanout_conflict" in flags


def is_b4(
    row: Mapping[str, Any], conflicts: Sequence[str], *, assigned_upstream: bool = False
) -> bool:
    return (
        not assigned_upstream
        and row.get("proposal") == "reject"
        and float(row.get("name_strict_score", 0.0) or 0.0) >= 90.0
    )


def _hamilton(populations: Mapping[str, int], seats: int) -> dict[str, int]:
    if seats < 0 or not populations or any(value < 0 for value in populations.values()):
        raise ValueError("invalid Hamilton inputs")
    total = sum(populations.values())
    if total == 0 or total < seats:
        raise ValueError("Hamilton population cannot fill seats")
    quotas = {key: seats * value // total for key, value in populations.items()}
    remainder = seats - sum(quotas.values())
    order = sorted(
        populations,
        key=lambda key: (-(seats * populations[key] % total), key),
    )
    for key in order[:remainder]:
        quotas[key] += 1
    return dict(sorted(quotas.items()))


def _hierarchical_hamilton(
    populations: Mapping[tuple[str, str], int], seats: int
) -> dict[tuple[str, str], int]:
    anchors: dict[str, int] = {}
    for (anchor, _mask), population in populations.items():
        anchors[anchor] = anchors.get(anchor, 0) + population
    anchor_quotas = _hamilton(anchors, seats)
    result: dict[tuple[str, str], int] = {}
    for anchor in sorted(anchors):
        children = {
            mask: population
            for (cell_anchor, mask), population in populations.items()
            if cell_anchor == anchor
        }
        for mask, quota in _hamilton(children, anchor_quotas[anchor]).items():
            result[(anchor, mask)] = quota
    return dict(sorted(result.items()))


def _candidate_view(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    query = """
    WITH fam AS (
      SELECT run_id,candidate_id,
        max(CASE WHEN family='name_alias_identity' AND status='positive' THEN 1 ELSE 0 END) name_pos,
        max(CASE WHEN family='verified_pixel_image_overlap' AND status='positive' THEN 1 ELSE 0 END) image_pos,
        max(CASE WHEN family='architect_firm_identity' AND status='positive' THEN 1 ELSE 0 END) firm_pos,
        max(CASE WHEN family='location_site_identity' AND status='positive' THEN 1 ELSE 0 END) location_pos,
        max(CASE WHEN family='year_phase_compatibility' AND status='positive' THEN 1 ELSE 0 END) year_pos,
        max(CASE WHEN family='source_url_external_stable_id' AND status='positive' THEN 1 ELSE 0 END) stable_pos
      FROM evidence_families GROUP BY run_id,candidate_id
    )
    SELECT c.*,fam.name_pos,fam.image_pos,fam.firm_pos,fam.location_pos,
      fam.year_pos,fam.stable_pos
    FROM candidate_pairs c JOIN fam USING(run_id,candidate_id)
    ORDER BY c.candidate_id
    """
    return [dict(row) for row in connection.execute(query)]


def _reviewed_rows(connection: sqlite3.Connection) -> list[dict[str, str]]:
    rows = [
        {
            "candidate_id": str(row[0]),
            "prior_version": str(row[1]),
            "opaque_review_id": str(row[2]),
        }
        for row in connection.execute(
            "SELECT candidate_id,prior_version,prior_opaque_review_id "
            "FROM prior_review_exclusions ORDER BY candidate_id,prior_version"
        )
    ]
    rows.extend(
        {
            "candidate_id": str(row[0]),
            "prior_version": "v3",
            "opaque_review_id": str(row[1]),
        }
        for row in connection.execute(
            "SELECT candidate_id,opaque_review_id FROM sample_neighbourhoods "
            "ORDER BY candidate_id"
        )
    )
    return rows


def _conflict_sets(connection: sqlite3.Connection) -> dict[str, frozenset[str]]:
    result: dict[str, set[str]] = {}
    for candidate_id, conflict_type in connection.execute(
        "SELECT candidate_id,conflict_type FROM hard_conflicts"
    ):
        result.setdefault(str(candidate_id), set()).add(str(conflict_type))
    return {key: frozenset(value) for key, value in result.items()}


def _fanout_flags(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT candidate_id FROM candidate_policy_flags "
            "WHERE flag_type='fanout_conflict'"
        )
    }


def _auxiliary_from_view(row: Mapping[str, Any]) -> str:
    statuses = {
        "architect_firm_identity": "positive" if row.get("firm_pos") else "unknown",
        "location_site_identity": "positive" if row.get("location_pos") else "unknown",
        "source_url_external_stable_id": "positive" if row.get("stable_pos") else "unknown",
        "year_phase_compatibility": "positive" if row.get("year_pos") else "unknown",
    }
    return auxiliary_family_key(statuses)


def _replay_sample(connection: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    candidates = _candidate_view(connection)
    by_id = {str(row["candidate_id"]): row for row in candidates}
    reviewed_rows = _reviewed_rows(connection)
    reviewed = {row["candidate_id"] for row in reviewed_rows}
    conflicts = _conflict_sets(connection)
    fanout_flags = _fanout_flags(connection)

    a_full = [
        row
        for row in candidates
        if row["proposal"] == "merge_candidate" and int(row["auto_merge_eligible"]) == 1
    ]
    a_frame = [row for row in a_full if str(row["candidate_id"]) not in reviewed]
    cells: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in a_frame:
        cell = (anchor_type(row), _auxiliary_from_view(row))
        cells.setdefault(cell, []).append(row)
    populations = {cell: len(rows) for cell, rows in cells.items()}
    quotas = _hierarchical_hamilton(populations, A_SAMPLE_COUNT)

    expected: list[dict[str, Any]] = []
    for (anchor, mask), rows in sorted(cells.items()):
        lane = f"A/{anchor}/{mask}"
        ranked = sorted(
            rows,
            key=lambda row: (
                _stable_rank(lane, str(row["candidate_id"])),
                str(row["candidate_id"]),
            ),
        )
        for row in ranked[: quotas[(anchor, mask)]]:
            candidate_id = str(row["candidate_id"])
            expected.append(
                {
                    "cohort": "A",
                    "stratum": "A_precision_core",
                    "sample_origin": "fresh",
                    "candidate_id": candidate_id,
                    "anchor_type": anchor,
                    "auxiliary_mask": mask,
                    "seed_rank_sha256": _stable_rank(lane, candidate_id),
                    "gate_scope": 1,
                }
            )

    b1 = [row for row in candidates if is_b1(row, conflicts.get(str(row["candidate_id"]), ())) ]
    b2 = [row for row in candidates if is_b2(row, conflicts.get(str(row["candidate_id"]), ())) ]
    b3 = [
        row
        for row in candidates
        if row["proposal"] == "defer"
        and str(row["candidate_id"]) in fanout_flags
        and conflicts.get(str(row["candidate_id"]), frozenset()) == {"fanout_conflict"}
    ]
    upstream = {
        str(row["candidate_id"])
        for rows in (b1, b2, b3)
        for row in rows
    }
    b4 = [
        row
        for row in candidates
        if is_b4(
            row,
            conflicts.get(str(row["candidate_id"]), ()),
            assigned_upstream=str(row["candidate_id"]) in upstream,
        )
    ]
    pools = {
        "B1_year_gate": b1,
        "B2_phash_veto": b2,
        "B3_fanout_defer": b3,
        "B4_high_name_reject": b4,
    }
    already = {row["candidate_id"] for row in expected}
    for stratum in (
        "B1_year_gate",
        "B2_phash_veto",
        "B3_fanout_defer",
        "B4_high_name_reject",
    ):
        pool_ids = {str(row["candidate_id"]) for row in pools[stratum]}
        if not set(CARRY_FORWARD[stratum]) <= pool_ids:
            raise ValueError(f"carry-forward predicate mismatch: {stratum}")
        for candidate_id in CARRY_FORWARD[stratum]:
            lane = f"{stratum}/carry"
            expected.append(
                {
                    "cohort": "B",
                    "stratum": stratum,
                    "sample_origin": "carry_forward",
                    "candidate_id": candidate_id,
                    "anchor_type": "",
                    "auxiliary_mask": "",
                    "seed_rank_sha256": _stable_rank(lane, candidate_id),
                    "gate_scope": 0,
                }
            )
            already.add(candidate_id)
        lane = f"{stratum}/fresh"
        fresh = [
            row
            for row in pools[stratum]
            if str(row["candidate_id"]) not in reviewed
            and str(row["candidate_id"]) not in already
        ]
        fresh.sort(
            key=lambda row: (
                _stable_rank(lane, str(row["candidate_id"])),
                str(row["candidate_id"]),
            )
        )
        for row in fresh[: B_FRESH_SAMPLE_COUNTS[stratum]]:
            candidate_id = str(row["candidate_id"])
            expected.append(
                {
                    "cohort": "B",
                    "stratum": stratum,
                    "sample_origin": "fresh",
                    "candidate_id": candidate_id,
                    "anchor_type": "",
                    "auxiliary_mask": "",
                    "seed_rank_sha256": _stable_rank(lane, candidate_id),
                    "gate_scope": 0,
                }
            )
            already.add(candidate_id)

    expected.sort(
        key=lambda row: (
            0 if row["cohort"] == "A" else 1,
            row["stratum"],
            row["anchor_type"],
            row["auxiliary_mask"],
            row["seed_rank_sha256"],
            row["candidate_id"],
        )
    )
    for rank, row in enumerate(expected, 1):
        row["sample_rank"] = rank
        row["sample_id"] = "n100_" + hashlib.sha256(
            f"{run_id}\0{row['candidate_id']}".encode("utf-8")
        ).hexdigest()[:24]
    return {
        "by_id": by_id,
        "reviewed_rows": reviewed_rows,
        "reviewed": reviewed,
        "a_full_count": len(a_full),
        "a_frame_count": len(a_frame),
        "a_populations": populations,
        "a_quotas": quotas,
        "b_pools": pools,
        "b_fresh_counts": {
            stratum: sum(str(row["candidate_id"]) not in reviewed for row in rows)
            for stratum, rows in pools.items()
        },
        "expected_samples": expected,
    }


def _expected_snapshot(
    v3: sqlite3.Connection, candidate_id: str
) -> tuple[dict[str, Any], str]:
    pair_row = v3.execute(
        "SELECT * FROM candidate_pairs WHERE candidate_id=?", (candidate_id,)
    ).fetchone()
    if pair_row is None:
        raise ValueError(f"candidate disappeared from v3: {candidate_id}")
    pair = dict(pair_row)
    left_row = v3.execute(
        "SELECT * FROM source_buildings WHERE source=? AND source_building_id=?",
        (pair["left_source"], pair["left_source_building_id"]),
    ).fetchone()
    right_row = v3.execute(
        "SELECT * FROM source_buildings WHERE source=? AND source_building_id=?",
        (pair["right_source"], pair["right_source_building_id"]),
    ).fetchone()
    if left_row is None or right_row is None:
        raise ValueError(f"candidate endpoint disappeared from v3: {candidate_id}")
    snapshot = {
        "candidate": pair,
        "left": dict(left_row),
        "right": dict(right_row),
        "origins": [
            str(row[0])
            for row in v3.execute(
                "SELECT origin FROM candidate_origins WHERE candidate_id=? ORDER BY origin",
                (candidate_id,),
            )
        ],
        "families": _rows(
            v3,
            "SELECT family,status,score,evidence_json,record_sha256 "
            "FROM evidence_families WHERE candidate_id=? ORDER BY family",
            (candidate_id,),
        ),
        "conflicts": _rows(
            v3,
            "SELECT conflict_type,detail_json,record_sha256 FROM hard_conflicts "
            "WHERE candidate_id=? ORDER BY conflict_type",
            (candidate_id,),
        ),
        "policy_flags": _rows(
            v3,
            "SELECT flag_type,detail_json,record_sha256 FROM candidate_policy_flags "
            "WHERE candidate_id=? ORDER BY flag_type",
            (candidate_id,),
        ),
    }
    digest = hashlib.sha256(_canonical_json(snapshot).encode("utf-8")).hexdigest()
    return snapshot, digest


def _expected_carry_gold(
    v3: sqlite3.Connection, candidate_id: str
) -> dict[str, Any]:
    row = v3.execute(
        """
        SELECT s.run_id,s.opaque_review_id,b.reviewer,
               b.record_sha256 blind_review_record_sha256,
               a.adjudicator,a.final_gold_label,a.mapped_decision,
               a.record_sha256 adjudication_record_sha256
        FROM sample_neighbourhoods s
        JOIN blind_reviews b USING(run_id,opaque_review_id)
        JOIN adjudications a USING(run_id,opaque_review_id)
        WHERE s.candidate_id=?
        """,
        (candidate_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"missing v3 carry-forward gold: {candidate_id}")
    return dict(row)


def _validate_sample_stage(
    sidecar: sqlite3.Connection,
    v3: sqlite3.Connection,
    run: Mapping[str, Any],
    checks: list[Check],
    metrics: dict[str, Any],
) -> dict[str, Any]:
    run_id = str(run["run_id"])
    replay = _replay_sample(v3, run_id)
    _check(checks, "prior_focal_review_count", PRIOR_FOCAL_REVIEW_COUNT, len(replay["reviewed_rows"]))
    _check(checks, "prior_focal_candidate_unique_count", PRIOR_FOCAL_REVIEW_COUNT, len(replay["reviewed"]))
    _check(checks, "core_full_population", A_FULL_POPULATION, replay["a_full_count"])
    _check(checks, "core_frame_population", A_FRAME_POPULATION, replay["a_frame_count"])
    _check(checks, "core_reviewed_eligible_count", A_REVIEWED_ELIGIBLE, replay["a_full_count"] - replay["a_frame_count"])
    _check(checks, "core_population_cells", dict(A_FRAME_COUNTS), replay["a_populations"])
    _check(checks, "core_hamilton_quotas", dict(A_QUOTAS), replay["a_quotas"])
    b_full = {key: len(rows) for key, rows in replay["b_pools"].items()}
    _check(checks, "diagnostic_full_populations", B_FULL_COUNTS, b_full)
    _check(checks, "diagnostic_fresh_populations", B_FRESH_COUNTS, replay["b_fresh_counts"])

    expected_exclusions = sorted(
        (
            row["candidate_id"],
            row["prior_version"],
            row["opaque_review_id"],
            int(row["candidate_id"] in {value for values in CARRY_FORWARD.values() for value in values}),
        )
        for row in replay["reviewed_rows"]
    )
    actual_exclusions = [
        (
            str(row[0]),
            str(row[1]),
            str(row[2]),
            int(row[3]),
        )
        for row in sidecar.execute(
            "SELECT candidate_id,prior_version,prior_opaque_review_id,carry_forward "
            "FROM review_exclusions WHERE run_id=? "
            "ORDER BY candidate_id,prior_version",
            (run_id,),
        )
    ]
    _check(checks, "review_exclusion_ledger_exact", expected_exclusions, actual_exclusions)

    expected_cells: list[tuple[Any, ...]] = []
    for (anchor, mask), count in sorted(replay["a_populations"].items()):
        expected_cells.append(
            (
                "A", "A_precision_core", anchor, mask, count, count,
                replay["a_quotas"][(anchor, mask)],
                "v3-eligible-hierarchical-hamilton-v1",
            )
        )
    for stratum in sorted(replay["b_pools"]):
        expected_cells.append(
            (
                "B", stratum, "", "", len(replay["b_pools"][stratum]),
                replay["b_fresh_counts"][stratum], B_TOTAL_SAMPLE_COUNTS[stratum],
                "wp23-clean-mechanism-predicates-v1",
            )
        )
    actual_cells = [
        tuple(row)
        for row in sidecar.execute(
            "SELECT cohort,stratum,anchor_type,auxiliary_mask,population_count,"
            "fresh_population_count,quota,predicate_version FROM population_cells "
            "WHERE run_id=? ORDER BY cohort,stratum,anchor_type,auxiliary_mask",
            (run_id,),
        )
    ]
    _check(checks, "population_ledger_exact", expected_cells, actual_cells)

    sample_fields = (
        "sample_rank", "sample_id", "cohort", "stratum", "sample_origin",
        "candidate_id", "anchor_type", "auxiliary_mask", "seed_rank_sha256",
        "gate_scope",
    )
    expected_samples = [tuple(row[field] for field in sample_fields) for row in replay["expected_samples"]]
    actual_sample_rows = _rows(
        sidecar,
        "SELECT * FROM sample_pairs WHERE run_id=? ORDER BY sample_rank",
        (run_id,),
    )
    actual_samples = [tuple(row[field] for field in sample_fields) for row in actual_sample_rows]
    _check(checks, "sample_pairs_seed_replay_exact", expected_samples, actual_samples)
    _check(checks, "sample_pair_count", TOTAL_SAMPLE_COUNT, len(actual_sample_rows))
    _check(checks, "sample_candidate_id_unique", TOTAL_SAMPLE_COUNT, len({row["candidate_id"] for row in actual_sample_rows}))
    _check(checks, "sample_id_unique", TOTAL_SAMPLE_COUNT, len({row["sample_id"] for row in actual_sample_rows}))
    _check(checks, "fresh_sample_count", FRESH_SAMPLE_COUNT, sum(row["sample_origin"] == "fresh" for row in actual_sample_rows))
    _check(checks, "carry_forward_sample_count", CARRY_FORWARD_COUNT, sum(row["sample_origin"] == "carry_forward" for row in actual_sample_rows))

    snapshot_failures: list[str] = []
    endpoints: list[tuple[str, str]] = []
    for sample in actual_sample_rows:
        candidate_id = str(sample["candidate_id"])
        snapshot_row = sidecar.execute(
            "SELECT * FROM candidate_snapshots WHERE run_id=? AND sample_id=?",
            (run_id, sample["sample_id"]),
        ).fetchone()
        if snapshot_row is None:
            snapshot_failures.append(f"{candidate_id}:missing")
            continue
        stored = dict(snapshot_row)
        try:
            expected_snapshot, expected_sha = _expected_snapshot(v3, candidate_id)
            actual_snapshot = {
                "candidate": json.loads(stored["candidate_json"]),
                "left": json.loads(stored["left_metadata_json"]),
                "right": json.loads(stored["right_metadata_json"]),
                "origins": json.loads(stored["origins_json"]),
                "families": json.loads(stored["families_json"]),
                "conflicts": json.loads(stored["conflicts_json"]),
                "policy_flags": json.loads(stored["policy_flags_json"]),
            }
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            snapshot_failures.append(f"{candidate_id}:{type(exc).__name__}")
            continue
        if actual_snapshot != expected_snapshot or stored["snapshot_sha256"] != expected_sha:
            snapshot_failures.append(candidate_id)
        candidate = expected_snapshot["candidate"]
        endpoints.extend(
            [
                (str(candidate["left_source"]), str(candidate["left_source_building_id"])),
                (str(candidate["right_source"]), str(candidate["right_source_building_id"])),
            ]
        )
    _check(checks, "candidate_snapshots_exact_v3_replay", [], snapshot_failures)
    _check(
        checks,
        "candidate_snapshot_count",
        TOTAL_SAMPLE_COUNT,
        int(
            sidecar.execute(
                "SELECT count(*) FROM candidate_snapshots WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        ),
    )
    endpoint_counts: dict[tuple[str, str], int] = {}
    for endpoint in endpoints:
        endpoint_counts[endpoint] = endpoint_counts.get(endpoint, 0) + 1
    metrics["endpoint_overlap"] = {
        "distinct_endpoints": len(endpoint_counts),
        "reused_endpoint_count": sum(value > 1 for value in endpoint_counts.values()),
        "maximum_candidate_occurrences": max(endpoint_counts.values(), default=0),
        "pass_condition": False,
    }

    carry_failures: list[str] = []
    carry_rows = _rows(
        sidecar,
        "SELECT * FROM carry_forward_gold WHERE run_id=? ORDER BY candidate_id",
        (run_id,),
    )
    carry_samples = {
        str(row["candidate_id"]): row
        for row in actual_sample_rows
        if row["sample_origin"] == "carry_forward"
    }
    for carry in carry_rows:
        candidate_id = str(carry["candidate_id"])
        sample = carry_samples.get(candidate_id)
        if sample is None or carry["sample_id"] != sample["sample_id"]:
            carry_failures.append(f"{candidate_id}:sample")
            continue
        expected = _expected_carry_gold(v3, candidate_id)
        comparisons = {
            "prior_run_id": expected["run_id"],
            "prior_opaque_review_id": expected["opaque_review_id"],
            "reviewer": expected["reviewer"],
            "adjudicator": expected["adjudicator"],
            "final_gold_label": expected["final_gold_label"],
            "mapped_decision": expected["mapped_decision"],
            "blind_review_record_sha256": expected["blind_review_record_sha256"],
            "adjudication_record_sha256": expected["adjudication_record_sha256"],
            "source_db_sha256": V3_SHA256,
        }
        if any(carry[key] != value for key, value in comparisons.items()):
            carry_failures.append(candidate_id)
        if GOLD_DECISIONS.get(str(carry["final_gold_label"])) != carry["mapped_decision"]:
            carry_failures.append(f"{candidate_id}:mapping")
    _check(checks, "carry_forward_gold_count", CARRY_FORWARD_COUNT, len(carry_rows))
    _check(checks, "carry_forward_gold_v3_provenance", [], carry_failures)

    logical = _sample_logical_sha(sidecar, run_id)
    _check(checks, "sample_logical_sha256_replay", logical, run["sample_logical_sha256"])
    metrics["sample_logical_sha256"] = logical
    metrics["sample_counts"] = {
        "A": sum(row["cohort"] == "A" for row in actual_sample_rows),
        "B": sum(row["cohort"] == "B" for row in actual_sample_rows),
        "fresh": sum(row["sample_origin"] == "fresh" for row in actual_sample_rows),
        "carry_forward": sum(row["sample_origin"] == "carry_forward" for row in actual_sample_rows),
    }
    return {"replay": replay, "samples": actual_sample_rows, "carry": carry_rows}


def _validate_sample_boundary(
    root: Path,
    sidecar: sqlite3.Connection,
    run: Mapping[str, Any],
    checks: list[Check],
    metrics: dict[str, Any],
    *,
    verify_full_hashes: bool,
) -> None:
    run_id = str(run["run_id"])
    _check(checks, "sample_run_status", "sample_frozen", run["status"])
    _check(checks, "sample_run_completed_at", None, run["completed_at"])
    _check(
        checks,
        "sample_run_downstream_statuses",
        ("pending", "pending", "pending", "pending", "pending"),
        (
            run["r1_status"],
            run["r2_status"],
            run["unblind_status"],
            run["adjudication_status"],
            run["gate_status"],
        ),
    )
    _check(
        checks,
        "sample_run_network_and_asset_counters",
        (0, 0, 0, 0),
        (
            int(run["asset_occurrence_count"]),
            int(run["unique_asset_count"]),
            int(run["logical_http_fetches"]),
            int(run["wire_http_requests"]),
        ),
    )

    pin_rows = _rows(
        sidecar,
        "SELECT * FROM input_pins WHERE run_id=? ORDER BY role",
        (run_id,),
    )
    pin_by_role = {str(row["role"]): row for row in pin_rows}
    expected_roles = {"v3_candidates_final", "n100_protocol", "wp23_order"}
    _check(checks, "sample_input_pin_roles_exact", expected_roles, set(pin_by_role))
    expected_paths = {
        "v3_candidates_final": DEFAULT_V3,
        "n100_protocol": DEFAULT_PROTOCOL,
        "wp23_order": DEFAULT_ORDER,
    }
    expected_usage = {
        "v3_candidates_final": "immutable_candidate_population",
        "n100_protocol": "review_protocol",
        "wp23_order": "job_order",
    }
    pin_failures: list[str] = []
    for role, row in pin_by_role.items():
        path = _resolve_repo_path(root, str(row["path"]))
        if (
            role not in expected_paths
            or path != _resolve_repo_path(root, expected_paths[role])
            or row["usage"] != expected_usage[role]
            or not path.is_file()
            or not _no_sqlite_sidecars(path)
            or int(row["size_bytes"]) != path.stat().st_size
            or row["sha256_before"] != row["sha256_after"]
        ):
            pin_failures.append(f"{role}:metadata")
            continue
        current = _sha256_file(path) if verify_full_hashes else row["sha256_before"]
        if current != row["sha256_before"]:
            pin_failures.append(f"{role}:sha256")
        if role == "v3_candidates_final" and (
            int(row["size_bytes"]), str(row["sha256_before"])
        ) != (V3_SIZE_BYTES, V3_SHA256):
            pin_failures.append(f"{role}:fixed_pin")
    _check(checks, "sample_input_pins_current_and_immutable", [], pin_failures)

    downstream_tables = (
        "cache_pins",
        "sample_assets",
        "fetch_assets",
        "fetch_attempts",
        "review_items",
        "blind_reviews",
        "review_locks",
        "unblind_artifacts",
        "adjudications",
        "metrics",
    )
    downstream_counts = {
        table: int(
            sidecar.execute(
                f"SELECT count(*) FROM {table} WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        )
        for table in downstream_tables
    }
    _check(
        checks,
        "sample_downstream_tables_empty",
        {table: 0 for table in downstream_tables},
        downstream_counts,
    )
    metrics["sample_boundary"] = {
        "input_pin_count": len(pin_rows),
        "downstream_counts": downstream_counts,
    }


def _top_building_assets(
    e2: sqlite3.Connection, source: str, building_id: str
) -> list[str]:
    return [
        str(row[0])
        for row in e2.execute(
            "SELECT ba.source_asset_id,a.original_width,a.original_height "
            "FROM building_assets ba JOIN assets a "
            "ON a.run_id=ba.run_id AND a.source=ba.source "
            "AND a.source_asset_id=ba.source_asset_id "
            "WHERE ba.run_id=(SELECT run_id FROM e2_runs LIMIT 1) "
            "AND ba.source=? AND ba.source_building_id=? "
            "AND a.fingerprint_status='success' "
            "ORDER BY (coalesce(a.original_width,0)*coalesce(a.original_height,0)) DESC,"
            "min(coalesce(a.original_width,0),coalesce(a.original_height,0)) DESC,"
            "ba.source_asset_id",
            (source, building_id),
        )
    ]


def _expected_assets_for_candidate(
    v3: sqlite3.Connection,
    e2: sqlite3.Connection,
    candidate: Mapping[str, Any],
) -> dict[str, list[tuple[str, str]]]:
    selected: dict[str, list[tuple[str, str]]] = {"left": [], "right": []}
    priorities = {"exact_pixel": 0, "identical_phash": 1, "phash_le8": 2, "phash_9_16": 3}
    direct = _rows(
        v3,
        "SELECT * FROM direct_image_evidence WHERE candidate_id=?",
        (candidate["candidate_id"],),
    )
    direct.sort(
        key=lambda row: (
            priorities.get(str(row["evidence_kind"]), 99),
            int(row["phash_distance"] or 0),
            str(row["e2_evidence_id"]),
        )
    )
    for index, row in enumerate(direct, 1):
        for side, column in (
            ("left", "left_source_asset_id"),
            ("right", "right_source_asset_id"),
        ):
            asset_id = str(row[column])
            if (
                asset_id not in {value for value, _ in selected[side]}
                and len(selected[side]) < MAX_ASSETS_PER_SIDE
            ):
                selected[side].append((asset_id, f"direct_evidence_{index}"))
        if all(len(values) >= MAX_ASSETS_PER_SIDE for values in selected.values()):
            break
    for side in ("left", "right"):
        if len(selected[side]) >= MAX_ASSETS_PER_SIDE:
            continue
        source = str(candidate[f"{side}_source"])
        building_id = str(candidate[f"{side}_source_building_id"])
        existing = {value for value, _ in selected[side]}
        for asset_id in _top_building_assets(e2, source, building_id):
            if len(selected[side]) >= MAX_ASSETS_PER_SIDE:
                break
            if asset_id in existing:
                continue
            selected[side].append((asset_id, "building_representative"))
            existing.add(asset_id)
            if len(selected[side]) >= MAX_ASSETS_PER_SIDE:
                break
    return selected


def _e2_asset(
    connection: sqlite3.Connection, source: str, asset_id: str
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT source,source_asset_id,fingerprint_status,canonical_url,fetch_url,final_url,"
        "raw_response_sha256,normalized_pixel_sha256,phash_hex,original_width,"
        "original_height,source_record_sha256 FROM assets "
        "WHERE run_id=(SELECT run_id FROM e2_runs LIMIT 1) "
        "AND source=? AND source_asset_id=?",
        (source, asset_id),
    ).fetchone()


def _e1_asset(connection: sqlite3.Connection, asset_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT sa.run_id,sa.fetch_url,sa.source_record_sha256,f.status,"
        "f.raw_response_sha256,f.normalized_pixel_sha256,f.selected_attempt_no "
        "FROM source_assets sa JOIN fingerprints f USING(run_id,source_asset_id) "
        "WHERE sa.run_id=(SELECT run_id FROM fingerprint_runs LIMIT 1) "
        "AND sa.source_asset_id=?",
        (asset_id,),
    ).fetchone()


def _validate_fetch_and_review_items(
    root: Path,
    sidecar: sqlite3.Connection,
    v3: sqlite3.Connection,
    run: Mapping[str, Any],
    sample_state: Mapping[str, Any],
    checks: list[Check],
    metrics: dict[str, Any],
    *,
    verify_full_hashes: bool,
) -> None:
    run_id = str(run["run_id"])
    samples = list(sample_state["samples"])
    fresh = {str(row["sample_id"]): row for row in samples if row["sample_origin"] == "fresh"}
    carry_ids = {str(row["sample_id"]) for row in samples if row["sample_origin"] == "carry_forward"}

    pin_rows = _rows(sidecar, "SELECT * FROM input_pins WHERE run_id=? ORDER BY role", (run_id,))
    pin_by_role = {str(row["role"]): row for row in pin_rows}
    expected_base_roles = {"v3_candidates_final", "n100_protocol", "wp23_order"}
    expected_roles = expected_base_roles | set(SOURCE_DB_SPECS)
    _check(checks, "input_pin_roles_exact", expected_roles, set(pin_by_role))
    pin_failures: list[str] = []
    base_pin_paths = {
        "v3_candidates_final": DEFAULT_V3,
        "n100_protocol": DEFAULT_PROTOCOL,
        "wp23_order": DEFAULT_ORDER,
    }
    base_pin_usage = {
        "v3_candidates_final": "immutable_candidate_population",
        "n100_protocol": "review_protocol",
        "wp23_order": "job_order",
    }
    for role, row in pin_by_role.items():
        path = _resolve_repo_path(root, str(row["path"]))
        if not path.is_file() or not _no_sqlite_sidecars(path):
            pin_failures.append(f"{role}:missing_or_sidecar")
            continue
        if int(row["size_bytes"]) != path.stat().st_size:
            pin_failures.append(f"{role}:size")
        if row["sha256_before"] != row["sha256_after"]:
            pin_failures.append(f"{role}:before_after")
        current = _sha256_file(path) if verify_full_hashes else str(row["sha256_before"])
        if current != row["sha256_before"]:
            pin_failures.append(f"{role}:current_sha")
        if role in base_pin_paths and (
            path != _resolve_repo_path(root, base_pin_paths[role])
            or row["usage"] != base_pin_usage[role]
        ):
            pin_failures.append(f"{role}:path_or_usage")
        if role == "v3_candidates_final" and (
            int(row["size_bytes"]), str(row["sha256_before"])
        ) != (V3_SIZE_BYTES, V3_SHA256):
            pin_failures.append(f"{role}:fixed_pin")
        if role in SOURCE_DB_SPECS:
            expected_path, expected_size, expected_sha = SOURCE_DB_SPECS[role]
            if (
                _resolve_repo_path(root, expected_path) != path
                or int(row["size_bytes"]) != expected_size
                or row["sha256_before"] != expected_sha
            ):
                pin_failures.append(f"{role}:protocol_pin")
    _check(checks, "input_pins_current_and_immutable", [], pin_failures)

    source_connections: dict[str, sqlite3.Connection] = {}
    e2: sqlite3.Connection | None = None
    try:
        if "e2_image_evidence" in pin_by_role:
            e2 = _open_readonly(_resolve_repo_path(root, str(pin_by_role["e2_image_evidence"]["path"])))
        for role, row in pin_by_role.items():
            if role.startswith("e1_"):
                source_connections[role.removeprefix("e1_")] = _open_readonly(
                    _resolve_repo_path(root, str(row["path"]))
                )

        asset_rows = _rows(
            sidecar,
            "SELECT * FROM sample_assets WHERE run_id=? ORDER BY sample_id,side,asset_order",
            (run_id,),
        )
        _check(checks, "asset_occurrence_count_run_row", len(asset_rows), int(run["asset_occurrence_count"]))
        _check(checks, "asset_occurrence_hard_cap", True, len(asset_rows) <= MAX_ASSET_OCCURRENCES)
        _check(checks, "carry_forward_has_no_new_assets", set(), {str(row["sample_id"]) for row in asset_rows} & carry_ids)
        _check(checks, "assets_only_for_fresh_samples", set(), {str(row["sample_id"]) for row in asset_rows} - set(fresh))

        grouping: dict[tuple[str, str], list[int]] = {}
        occurrence_failures: list[str] = []
        actual_by_sample: dict[str, dict[str, list[tuple[str, str]]]] = {}
        for row in asset_rows:
            sample_id = str(row["sample_id"])
            side = str(row["side"])
            grouping.setdefault((sample_id, side), []).append(int(row["asset_order"]))
            actual_by_sample.setdefault(sample_id, {"left": [], "right": []})[side].append(
                (str(row["source_asset_id"]), str(row["selection_reason"]))
            )
            sample = fresh.get(sample_id)
            if sample is None:
                continue
            candidate = sample_state["replay"]["by_id"][str(sample["candidate_id"])]
            expected_source = str(candidate[f"{side}_source"])
            expected_building = str(candidate[f"{side}_source_building_id"])
            expected_key = hashlib.sha256(
                f"{row['source']}\0{row['source_asset_id']}".encode("utf-8")
            ).hexdigest()
            if (
                row["source"] != expected_source
                or row["source_building_id"] != expected_building
                or row["asset_key"] != expected_key
            ):
                occurrence_failures.append(f"{sample_id}/{side}/{row['asset_order']}")
        for key, orders in grouping.items():
            if orders != list(range(1, len(orders) + 1)) or len(orders) > MAX_ASSETS_PER_SIDE:
                occurrence_failures.append(f"{key}:order")
        _check(checks, "sample_asset_endpoint_key_and_order", [], occurrence_failures)

        selection_failures: list[str] = []
        if e2 is None:
            selection_failures.append("missing_e2")
        else:
            for sample_id, sample in fresh.items():
                candidate = sample_state["replay"]["by_id"][str(sample["candidate_id"])]
                expected = _expected_assets_for_candidate(v3, e2, candidate)
                actual = actual_by_sample.get(sample_id, {"left": [], "right": []})
                if actual != expected:
                    selection_failures.append(sample_id)
        _check(checks, "sample_asset_selection_replay", [], selection_failures)

        fetch_rows = _rows(
            sidecar,
            "SELECT * FROM fetch_assets WHERE run_id=? ORDER BY asset_key",
            (run_id,),
        )
        fetch_by_key = {str(row["asset_key"]): row for row in fetch_rows}
        occurrence_keys = {str(row["asset_key"]) for row in asset_rows}
        _check(checks, "global_asset_dedup_exact", occurrence_keys, set(fetch_by_key))
        _check(checks, "unique_asset_count_run_row", len(fetch_rows), int(run["unique_asset_count"]))
        _check(checks, "fetch_asset_source_id_unique", len(fetch_rows), len({(row["source"], row["source_asset_id"]) for row in fetch_rows}))
        _check(checks, "fetch_assets_all_terminal", True, all(str(row["status"]) in TERMINAL_ASSET_STATUSES for row in fetch_rows))

        lineage_failures: list[str] = []
        cache_path_failures: list[str] = []
        for row in fetch_rows:
            source = str(row["source"])
            asset_id = str(row["source_asset_id"])
            expected_key = hashlib.sha256(f"{source}\0{asset_id}".encode()).hexdigest()
            if row["asset_key"] != expected_key:
                lineage_failures.append(f"{row['asset_key']}:key")
            e2_row = _e2_asset(e2, source, asset_id) if e2 is not None else None
            e1_connection = source_connections.get(source)
            e1_row = _e1_asset(e1_connection, asset_id) if e1_connection is not None else None
            if e2_row is None or e1_row is None:
                if row["status"] != "failed" or row["error_kind"] != "missing_frozen_lineage":
                    lineage_failures.append(f"{row['asset_key']}:missing_lineage_status")
            else:
                expected_lineage = (
                    e1_row["fetch_url"], e1_row["raw_response_sha256"],
                    e1_row["normalized_pixel_sha256"],
                )
                actual_lineage = (
                    row["expected_url"], row["expected_raw_sha256"],
                    row["expected_pixel_sha256"],
                )
                if str(e1_row["status"]) != "success" or expected_lineage != actual_lineage:
                    lineage_failures.append(f"{row['asset_key']}:e1")
                if (
                    str(e2_row["raw_response_sha256"]),
                    str(e2_row["normalized_pixel_sha256"]),
                ) != (str(row["expected_raw_sha256"]), str(row["expected_pixel_sha256"])):
                    lineage_failures.append(f"{row['asset_key']}:e2")
            if row["cache_path"] is not None:
                cache_path = _resolve_repo_path(root, str(row["cache_path"])); role = str(row["cache_role"])
                cache_pin = None
                if role in READ_CACHE_SPECS:
                    cache_pin = _resolve_repo_path(root, READ_CACHE_SPECS[role][0])
                    transform_version = (
                        WP1_TRANSFORM_VERSION if role == "wp1_p4b" else REVIEW_TRANSFORM_VERSION
                    )
                    pixel_sha = str(row["expected_pixel_sha256"] or "")
                elif role == "wp23_n100_output":
                    cache_pin = _resolve_repo_path(root, DEFAULT_OUTPUT_CACHE)
                    transform_version = REVIEW_TRANSFORM_VERSION
                    pixel_sha = str(row["actual_pixel_sha256"] or "")
                else:
                    transform_version = ""
                    pixel_sha = ""
                try:
                    expected_cache_path = (
                        _cache_asset_path(cache_pin, pixel_sha, transform_version)
                        if cache_pin is not None and pixel_sha and transform_version
                        else None
                    )
                    if (
                        not cache_path.is_file()
                        or cache_pin is None
                        or not cache_path.is_relative_to(cache_pin)
                        or cache_path != expected_cache_path
                    ):
                        cache_path_failures.append(str(row["asset_key"]))
                except ValueError:
                    cache_path_failures.append(str(row["asset_key"]))
            elif row["status"] in READABLE_ASSET_STATUSES:
                cache_path_failures.append(f"{row['asset_key']}:missing")
        _check(checks, "fetch_lineage_replay", [], lineage_failures)
        _check(checks, "fetch_cache_paths_bound", [], cache_path_failures)

        attempts = _rows(
            sidecar,
            "SELECT * FROM fetch_attempts WHERE run_id=? ORDER BY asset_key,attempt_no",
            (run_id,),
        )
        attempted_ids = {str(row["asset_key"]) for row in attempts}
        attempt_failures: list[str] = []
        for row in attempts:
            fetch = fetch_by_key.get(str(row["asset_key"]))
            try:
                redirects = json.loads(str(row["redirect_chain_json"]))
            except json.JSONDecodeError:
                redirects = ["invalid"]
            if (
                fetch is None
                or int(row["attempt_no"]) != 1
                or int(row["request_count"]) not in {0, 1}
                or redirects != []
                or fetch["status"] != row["status"]
                or int(fetch["request_count"]) != int(row["request_count"])
            ):
                attempt_failures.append(str(row["asset_key"]))
            try:
                if _iso_timestamp(str(row["started_at"])) > _iso_timestamp(str(row["completed_at"])):
                    attempt_failures.append(f"{row['asset_key']}:time")
            except ValueError:
                attempt_failures.append(f"{row['asset_key']}:timestamp")
        non_attempted_bad = [
            str(row["asset_key"])
            for row in fetch_rows
            if row["asset_key"] not in attempted_ids
            and not (
                (row["status"] == "cache_hit" and int(row["request_count"]) == 0)
                or (row["status"] == "failed" and row["error_kind"] == "missing_frozen_lineage" and int(row["request_count"]) == 0)
            )
        ]
        logical = len(attempts)
        wire = sum(int(row["request_count"]) for row in attempts)
        _check(checks, "fetch_attempts_single_no_redirect", [], attempt_failures)
        _check(checks, "fetch_non_attempted_terminal_reason", [], non_attempted_bad)
        _check(checks, "logical_http_count_run_row", logical, int(run["logical_http_fetches"]))
        _check(checks, "wire_http_count_run_row", wire, int(run["wire_http_requests"]))
        _check(checks, "logical_http_cap_420", True, logical <= MAX_LOGICAL_HTTP_FETCHES)
        _check(checks, "wire_http_cap_420_and_hard_500", True, wire <= MAX_LOGICAL_HTTP_FETCHES <= MAX_WIRE_HTTP_REQUESTS)
        metrics["network"] = {"logical_http_fetches": logical, "wire_http_requests": wire, "hard_wire_cap": MAX_WIRE_HTTP_REQUESTS}

        cache_rows = _rows(sidecar, "SELECT * FROM cache_pins WHERE run_id=? ORDER BY role", (run_id,))
        cache_by_role = {str(row["role"]): row for row in cache_rows}
        _check(checks, "cache_pin_roles_exact", set(READ_CACHE_SPECS) | {"wp23_n100_output"}, set(cache_by_role))
        cache_failures: list[str] = []
        for role, row in cache_by_role.items():
            path = _resolve_repo_path(root, str(row["path"])); current = _cache_tree(path)
            after = (int(row["file_count_after"]), int(row["total_bytes_after"]), str(row["tree_sha256_after"]))
            if current != after:
                cache_failures.append(f"{role}:current")
            if role in READ_CACHE_SPECS:
                expected_path, expected_count, expected_bytes, expected_tree = READ_CACHE_SPECS[role]
                expected_transform = (
                    WP1_TRANSFORM_VERSION if role == "wp1_p4b" else REVIEW_TRANSFORM_VERSION
                )
                before = (int(row["file_count_before"]), int(row["total_bytes_before"]), str(row["tree_sha256_before"]))
                if (
                    row["access_mode"] != "read_only"
                    or row["transform_version"] != expected_transform
                    or path != _resolve_repo_path(root, expected_path)
                    or before != (expected_count, expected_bytes, expected_tree)
                    or after != before
                ):
                    cache_failures.append(f"{role}:readonly_pin")
            elif (
                role != "wp23_n100_output"
                or row["access_mode"] != "write_only"
                or row["transform_version"] != REVIEW_TRANSFORM_VERSION
                or path != _resolve_repo_path(root, DEFAULT_OUTPUT_CACHE)
                or int(row["file_count_before"]) != 0
                or int(row["total_bytes_before"]) != 0
                or row["tree_sha256_before"]
                != hashlib.sha256(b"[]").hexdigest()
            ):
                cache_failures.append(f"{role}:output_pin")
        _check(checks, "cache_before_after_and_current_tree", [], cache_failures)

        review_rows = _rows(
            sidecar,
            "SELECT * FROM review_items WHERE run_id=? ORDER BY lane,display_order",
            (run_id,),
        )
        review_failures: list[str] = []
        opaque_by_lane: dict[str, set[str]] = {lane: set() for lane in LANES}
        for lane in LANES:
            ranked: list[tuple[str, str, str]] = []
            for sample_id in fresh:
                digest = hashlib.sha256(
                    f"{REVIEW_VERSION}\0{run_id}\0{lane}\0{sample_id}".encode()
                ).hexdigest()
                opaque = f"{lane}_{digest[:24]}"
                order = hashlib.sha256(
                    f"{SAMPLE_SEED}\0blind-order\0{lane}\0{sample_id}".encode()
                ).hexdigest()
                ranked.append((order, opaque, sample_id))
            ranked.sort()
            actual_lane = [row for row in review_rows if row["lane"] == lane]
            expected_lane = [
                (index, opaque, sample_id)
                for index, (_order, opaque, sample_id) in enumerate(ranked, 1)
            ]
            actual_binding = [
                (int(row["display_order"]), str(row["opaque_review_id"]), str(row["sample_id"]))
                for row in actual_lane
            ]
            if actual_binding != expected_lane:
                review_failures.append(f"{lane}:binding")
            for row in actual_lane:
                opaque_by_lane[lane].add(str(row["opaque_review_id"])); path = _resolve_repo_path(root, str(row["sheet_path"]))
                if (
                    not path.is_file()
                    or int(row["sheet_bytes"]) != path.stat().st_size
                    or (verify_full_hashes and _sha256_file(path) != row["sheet_sha256"])
                ):
                    review_failures.append(f"{lane}:{row['opaque_review_id']}:sheet")
        _check(checks, "review_item_count_per_lane", {"r1": REVIEW_ROWS_PER_REVIEWER, "r2": REVIEW_ROWS_PER_REVIEWER}, {lane: sum(row["lane"] == lane for row in review_rows) for lane in LANES})
        _check(checks, "review_items_fresh_only", set(), {str(row["sample_id"]) for row in review_rows} - set(fresh))
        _check(checks, "review_opaque_namespaces_disjoint", set(), opaque_by_lane["r1"] & opaque_by_lane["r2"])
        _check(checks, "review_item_binding_and_sheet_hash", [], review_failures)
    finally:
        for connection in source_connections.values():
            connection.close()
        if e2 is not None:
            e2.close()


def _normalized_actor(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _validate_review_barrier(
    root: Path,
    sidecar: sqlite3.Connection,
    run: Mapping[str, Any],
    sample_state: Mapping[str, Any],
    checks: list[Check],
    metrics: dict[str, Any],
    *,
    verify_full_hashes: bool,
) -> dict[str, Any]:
    run_id = str(run["run_id"])
    samples = list(sample_state["samples"])
    fresh_ids = {str(row["sample_id"]) for row in samples if row["sample_origin"] == "fresh"}
    carry_ids = {str(row["sample_id"]) for row in samples if row["sample_origin"] == "carry_forward"}
    review_items = _rows(
        sidecar,
        "SELECT * FROM review_items WHERE run_id=? ORDER BY lane,display_order",
        (run_id,),
    )
    item_by_key = {
        (str(row["lane"]), str(row["opaque_review_id"])): row for row in review_items
    }
    reviews = _rows(
        sidecar,
        "SELECT * FROM blind_reviews WHERE run_id=? ORDER BY lane,opaque_review_id",
        (run_id,),
    )
    _check(checks, "dual_blind_review_row_count", TOTAL_DUAL_REVIEW_ROWS, len(reviews))
    _check(checks, "blind_reviews_fresh_only", set(), {str(row["sample_id"]) for row in reviews} - fresh_ids)
    _check(checks, "carry_forward_has_no_new_blind_review", set(), {str(row["sample_id"]) for row in reviews} & carry_ids)
    review_failures: list[str] = []
    for row in reviews:
        key = (str(row["lane"]), str(row["opaque_review_id"]))
        item = item_by_key.get(key)
        if (
            item is None
            or item["sample_id"] != row["sample_id"]
            or row["label"] not in REVIEW_LABELS
            or not str(row["rationale"]).strip()
            or not str(row["hard_conflict_observed"]).strip()
            or not _normalized_actor(row["reviewer"])
        ):
            review_failures.append("/".join(key))
        try:
            _iso_timestamp(str(row["reviewed_at"]))
        except ValueError:
            review_failures.append("/".join(key) + ":timestamp")
    _check(checks, "blind_review_bindings_and_completeness", [], review_failures)
    _check(
        checks,
        "blind_review_sample_lane_unique",
        TOTAL_DUAL_REVIEW_ROWS,
        len({(row["sample_id"], row["lane"]) for row in reviews}),
    )

    locks = _rows(
        sidecar,
        "SELECT * FROM review_locks WHERE run_id=? ORDER BY lane",
        (run_id,),
    )
    lock_by_lane = {str(row["lane"]): row for row in locks}
    _check(checks, "dual_review_lock_lanes", set(LANES), set(lock_by_lane))
    actors = {_normalized_actor(row["reviewer"]) for row in locks}
    _check(checks, "reviewer_identities_distinct", 2, len(actors))
    lock_failures: list[str] = []
    lock_times: list[datetime] = []
    for lane in LANES:
        lock = lock_by_lane.get(lane)
        if lock is None:
            continue
        lane_items = sorted(
            (row for row in review_items if row["lane"] == lane),
            key=lambda row: int(row["display_order"]),
        )
        lane_reviews = {
            str(row["opaque_review_id"]): row for row in reviews if row["lane"] == lane
        }
        ledger_rows: list[dict[str, Any]] = []
        reviewed_at_values: set[str] = set()
        for item in lane_items:
            review = lane_reviews.get(str(item["opaque_review_id"]))
            if review is None:
                continue
            reviewed_at_values.add(str(review["reviewed_at"]))
            ledger_rows.append(
                {
                    "lane": lane,
                    "opaque_review_id": str(review["opaque_review_id"]),
                    "label": str(review["label"]),
                    "rationale": str(review["rationale"]),
                    "hard_conflict_observed": str(review["hard_conflict_observed"]),
                    "reviewer": str(review["reviewer"]),
                    "reviewed_at": str(review["reviewed_at"]),
                }
            )
        ledger_sha = hashlib.sha256(_canonical_json(ledger_rows).encode("utf-8")).hexdigest()
        if (
            int(lock["row_count"]) != REVIEW_ROWS_PER_REVIEWER
            or len(ledger_rows) != REVIEW_ROWS_PER_REVIEWER
            or len(reviewed_at_values) != 1
            or _normalized_actor(lock["reviewer"]) not in actors
            or any(_normalized_actor(row["reviewer"]) != _normalized_actor(lock["reviewer"]) for row in lane_reviews.values())
            or lock["ordered_ledger_sha256"] != ledger_sha
        ):
            lock_failures.append(f"{lane}:ledger")
        completed = _resolve_repo_path(root, str(lock["completed_form_path"]))
        locked = _resolve_repo_path(root, str(lock["locked_output_path"]))
        for role, path, digest in (
            ("completed", completed, lock["completed_form_sha256"]),
            ("locked", locked, lock["locked_output_sha256"]),
        ):
            if (
                not path.is_file()
                or (verify_full_hashes and _sha256_file(path) != digest)
            ):
                lock_failures.append(f"{lane}:{role}_file")
        if locked.is_file():
            try:
                locked_payload = json.loads(locked.read_text(encoding="utf-8"))
                expected_locked = {
                    "review_version": REVIEW_VERSION,
                    "run_id": run_id,
                    "lane": lane,
                    "reviewer": str(lock["reviewer"]),
                    "reviewed_at": next(iter(reviewed_at_values), ""),
                    "row_count": REVIEW_ROWS_PER_REVIEWER,
                    "ordered_ledger_sha256": ledger_sha,
                    "items": ledger_rows,
                }
                if locked_payload != expected_locked:
                    lock_failures.append(f"{lane}:locked_payload")
            except (OSError, json.JSONDecodeError):
                lock_failures.append(f"{lane}:locked_json")
        if completed.is_file():
            try:
                completed_payload = json.loads(completed.read_text(encoding="utf-8"))
                contract_items = [
                    {
                        "display_order": int(item["display_order"]),
                        "opaque_review_id": str(item["opaque_review_id"]),
                        "sheet_path": str(item["sheet_path"]),
                        "sheet_sha256": str(item["sheet_sha256"]),
                    }
                    for item in lane_items
                ]
                contract = {
                    "review_version": REVIEW_VERSION,
                    "run_id": run_id,
                    "lane": lane,
                    "items": contract_items,
                }
                if (
                    completed_payload.get("form_contract_sha256")
                    != hashlib.sha256(_canonical_json(contract).encode()).hexdigest()
                    or _normalized_actor(completed_payload.get("reviewer"))
                    != _normalized_actor(lock["reviewer"])
                    or len(completed_payload.get("items", [])) != REVIEW_ROWS_PER_REVIEWER
                ):
                    lock_failures.append(f"{lane}:completed_contract")
                completed_items = {
                    str(item.get("opaque_review_id", "")): item
                    for item in completed_payload.get("items", [])
                    if isinstance(item, Mapping)
                }
                for opaque, review in lane_reviews.items():
                    item = completed_items.get(opaque)
                    if item is None or any(
                        str(item.get(key, "")).strip() != str(review[column]).strip()
                        for key, column in (
                            ("label", "label"),
                            ("rationale", "rationale"),
                            ("hard_conflict_observed", "hard_conflict_observed"),
                        )
                    ):
                        lock_failures.append(f"{lane}:completed_binding:{opaque}")
            except (OSError, json.JSONDecodeError):
                lock_failures.append(f"{lane}:completed_json")
        try:
            lock_times.append(_iso_timestamp(str(lock["locked_at"])))
        except ValueError:
            lock_failures.append(f"{lane}:locked_at")
    _check(checks, "review_lock_ledgers_and_files", [], lock_failures)

    unblind_rows = _rows(sidecar, "SELECT * FROM unblind_artifacts WHERE run_id=?", (run_id,))
    _check(checks, "single_unblind_artifact", 1, len(unblind_rows))
    unblind_failures: list[str] = []
    if unblind_rows:
        unblind = unblind_rows[0]
        if (
            unblind["r1_ledger_sha256"] != lock_by_lane.get("r1", {}).get("ordered_ledger_sha256")
            or unblind["r2_ledger_sha256"] != lock_by_lane.get("r2", {}).get("ordered_ledger_sha256")
        ):
            unblind_failures.append("ledger_binding")
        try:
            unblind_time = _iso_timestamp(str(unblind["created_at"]))
            if len(lock_times) != 2 or unblind_time < max(lock_times):
                unblind_failures.append("created_before_dual_lock")
        except ValueError:
            unblind_failures.append("timestamp")
        packet_path = _resolve_repo_path(root, str(unblind["packet_path"]))
        form_path = _resolve_repo_path(root, str(unblind["form_path"]))
        if (
            not packet_path.is_file()
            or (
                verify_full_hashes
                and _sha256_file(packet_path) != unblind["packet_sha256"]
            )
        ):
            unblind_failures.append("packet_hash")
        expected_fresh = sorted(
            (row for row in samples if row["sample_origin"] == "fresh"),
            key=lambda row: int(row["sample_rank"]),
        )
        try:
            blank_form = _blank_adjudication_form_payload(
                run_id, expected_fresh, reviews
            )
            if _artifact_json_sha(blank_form) != unblind["form_sha256"]:
                unblind_failures.append("form_origin_hash")
        except (KeyError, TypeError, ValueError):
            unblind_failures.append("form_origin_replay")
        if not form_path.is_file():
            unblind_failures.append("form_missing")
        try:
            packet = json.loads(packet_path.read_text(encoding="utf-8"))
            form = json.loads(form_path.read_text(encoding="utf-8"))
            if (
                packet.get("run_id") != run_id
                or packet.get("adjudication_version") != ADJUDICATION_VERSION
                or packet.get("fresh_row_count") != FRESH_SAMPLE_COUNT
                or packet.get("carry_forward_rows") != CARRY_FORWARD_COUNT
                or len(packet.get("items", [])) != FRESH_SAMPLE_COUNT
                or form.get("run_id") != run_id
                or form.get("adjudication_version") != ADJUDICATION_VERSION
                or len(form.get("items", [])) != FRESH_SAMPLE_COUNT
            ):
                unblind_failures.append("packet_or_form_contract")
            packet_items = {
                str(item.get("sample_id", "")): item
                for item in packet.get("items", [])
                if isinstance(item, Mapping)
            }
            form_items = {
                str(item.get("sample_id", "")): item
                for item in form.get("items", [])
                if isinstance(item, Mapping)
            }
            form_contract_items: list[dict[str, Any]] = []
            for sample in expected_fresh:
                sample_id = str(sample["sample_id"])
                packet_item = packet_items.get(sample_id)
                form_item = form_items.get(sample_id)
                lane_reviews = {
                    lane: next(
                        (
                            row
                            for row in reviews
                            if row["sample_id"] == sample_id and row["lane"] == lane
                        ),
                        None,
                    )
                    for lane in LANES
                }
                if (
                    packet_item is None
                    or form_item is None
                    or packet_item.get("candidate_id") != sample["candidate_id"]
                    or packet_item.get("cohort") != sample["cohort"]
                    or packet_item.get("stratum") != sample["stratum"]
                    or any(lane_reviews[lane] is None for lane in LANES)
                    or any(
                        packet_item.get("reviews", {}).get(lane, {}).get("label")
                        != lane_reviews[lane]["label"]
                        for lane in LANES
                        if lane_reviews[lane] is not None
                    )
                    or form_item.get("candidate_id") != sample["candidate_id"]
                    or form_item.get("r1_label")
                    != (lane_reviews["r1"]["label"] if lane_reviews["r1"] else None)
                    or form_item.get("r2_label")
                    != (lane_reviews["r2"]["label"] if lane_reviews["r2"] else None)
                ):
                    unblind_failures.append(f"unblind_binding:{sample_id}")
                form_contract_items.append(
                    {
                        "sample_rank": sample["sample_rank"],
                        "sample_id": sample_id,
                        "candidate_id": sample["candidate_id"],
                        "r1_label": lane_reviews["r1"]["label"] if lane_reviews["r1"] else None,
                        "r2_label": lane_reviews["r2"]["label"] if lane_reviews["r2"] else None,
                    }
                )
            form_contract = {
                "adjudication_version": ADJUDICATION_VERSION,
                "run_id": run_id,
                "items": form_contract_items,
            }
            if form.get("form_contract_sha256") != hashlib.sha256(
                _canonical_json(form_contract).encode("utf-8")
            ).hexdigest():
                unblind_failures.append("adjudication_form_contract_sha")
        except (OSError, json.JSONDecodeError):
            unblind_failures.append("packet_or_form_json")
    _check(checks, "dual_locks_before_unblind_and_artifact_hashes", [], unblind_failures)
    metrics["review_barrier"] = {
        "fresh_rows_per_lane": {lane: sum(row["lane"] == lane for row in reviews) for lane in LANES},
        "carry_provenance_rows": len(sample_state["carry"]),
        "locks": len(locks),
    }
    return {"reviews": reviews, "locks": locks, "unblind": unblind_rows[0] if unblind_rows else None}


def _label_counts(values: Iterable[str]) -> dict[str, int]:
    counts = {label: 0 for label in sorted(REVIEW_LABELS)}
    for value in values:
        if value in counts:
            counts[value] += 1
    return counts


def _replay_diagnostic_b(
    connection: sqlite3.Connection, run_id: str
) -> dict[str, Any]:
    """Recompute the complete B40 descriptive diagnostic from normalized rows."""

    diagnostic: dict[str, Any] = {}
    for stratum in sorted(B_FRESH_SAMPLE_COUNTS):
        fresh_values = [
            str(row[0])
            for row in connection.execute(
                "SELECT a.final_gold_label FROM sample_pairs p "
                "JOIN adjudications a USING(run_id,sample_id) "
                "WHERE p.run_id=? AND p.stratum=? AND p.sample_origin='fresh'",
                (run_id, stratum),
            )
        ]
        carry_values = [
            str(row[0])
            for row in connection.execute(
                "SELECT c.final_gold_label FROM sample_pairs p "
                "JOIN carry_forward_gold c USING(run_id,sample_id) "
                "WHERE p.run_id=? AND p.stratum=?",
                (run_id, stratum),
            )
        ]
        fresh_counts = _label_counts(fresh_values)
        carry_counts = _label_counts(carry_values)
        combined_counts = {
            label: fresh_counts[label] + carry_counts[label]
            for label in sorted(REVIEW_LABELS)
        }
        evidence_memo: list[dict[str, Any]] = []
        fresh_evidence = _rows(
            connection,
            "SELECT p.candidate_id,a.final_gold_label,s.families_json,s.conflicts_json,"
            "a.rationale adjudicator_rationale,"
            "max(CASE WHEN b.lane='r1' THEN b.rationale END) r1_rationale,"
            "max(CASE WHEN b.lane='r2' THEN b.rationale END) r2_rationale "
            "FROM sample_pairs p JOIN adjudications a USING(run_id,sample_id) "
            "JOIN candidate_snapshots s USING(run_id,sample_id) "
            "JOIN blind_reviews b USING(run_id,sample_id) "
            "WHERE p.run_id=? AND p.stratum=? AND p.sample_origin='fresh' "
            "GROUP BY p.sample_id ORDER BY p.sample_rank",
            (run_id, stratum),
        )
        for evidence in fresh_evidence:
            families = json.loads(str(evidence["families_json"]))
            evidence_memo.append(
                {
                    "sample_origin": "fresh",
                    "candidate_id": str(evidence["candidate_id"]),
                    "final_gold_label": str(evidence["final_gold_label"]),
                    "hard_conflicts": json.loads(str(evidence["conflicts_json"])),
                    "positive_families": [
                        row
                        for row in families
                        if str(row.get("status")) == "positive"
                    ],
                    "reviewer_rationales": {
                        "r1": str(evidence["r1_rationale"]),
                        "r2": str(evidence["r2_rationale"]),
                    },
                    "adjudicator_rationale": str(
                        evidence["adjudicator_rationale"]
                    ),
                }
            )
        carry_evidence = _rows(
            connection,
            "SELECT p.candidate_id,c.final_gold_label,s.families_json,s.conflicts_json,"
            "c.reviewer_rationale,c.adjudicator_rationale,c.prior_run_id,"
            "c.prior_opaque_review_id,c.blind_review_record_sha256,"
            "c.adjudication_record_sha256 "
            "FROM sample_pairs p JOIN carry_forward_gold c USING(run_id,sample_id) "
            "JOIN candidate_snapshots s USING(run_id,sample_id) "
            "WHERE p.run_id=? AND p.stratum=? ORDER BY p.sample_rank",
            (run_id, stratum),
        )
        for evidence in carry_evidence:
            families = json.loads(str(evidence["families_json"]))
            evidence_memo.append(
                {
                    "sample_origin": "carry_forward",
                    "candidate_id": str(evidence["candidate_id"]),
                    "final_gold_label": str(evidence["final_gold_label"]),
                    "hard_conflicts": json.loads(str(evidence["conflicts_json"])),
                    "positive_families": [
                        row
                        for row in families
                        if str(row.get("status")) == "positive"
                    ],
                    "reviewer_rationales": {
                        "v3_single_reviewer": str(evidence["reviewer_rationale"]),
                        "second_reviewer": None,
                    },
                    "adjudicator_rationale": str(
                        evidence["adjudicator_rationale"]
                    ),
                    "provenance": {
                        "prior_run_id": str(evidence["prior_run_id"]),
                        "prior_opaque_review_id": str(
                            evidence["prior_opaque_review_id"]
                        ),
                        "blind_review_record_sha256": str(
                            evidence["blind_review_record_sha256"]
                        ),
                        "adjudication_record_sha256": str(
                            evidence["adjudication_record_sha256"]
                        ),
                    },
                }
            )
        fresh_denominator = B_FRESH_SAMPLE_COUNTS[stratum]
        carry_denominator = len(CARRY_FORWARD[stratum])
        combined_denominator = fresh_denominator + carry_denominator
        diagnostic[stratum] = {
            "fresh": {
                "denominator": fresh_denominator,
                "label_counts": fresh_counts,
                "same_rate": fresh_counts["same"] / fresh_denominator,
                "fresh_false_reject_rate": fresh_counts["same"]
                / fresh_denominator,
            },
            "carry_forward": {
                "denominator": carry_denominator,
                "label_counts": carry_counts,
                "provenance_only": True,
                "carry_forward_confirmation": carry_denominator
                == sum(carry_counts.values()),
            },
            "combined_descriptive": {
                "denominator": combined_denominator,
                "label_counts": combined_counts,
                "same_rate": combined_counts["same"] / combined_denominator,
                "inferential": False,
            },
            "evidence_memo": evidence_memo,
        }
    return diagnostic


def _validate_adjudication_and_gate(
    root: Path,
    artifact: Path,
    sidecar: sqlite3.Connection,
    run: Mapping[str, Any],
    sample_state: Mapping[str, Any],
    review_state: Mapping[str, Any],
    checks: list[Check],
    metrics: dict[str, Any],
    *,
    verify_full_hashes: bool,
) -> None:
    run_id = str(run["run_id"])
    samples = list(sample_state["samples"])
    sample_by_id = {str(row["sample_id"]): row for row in samples}
    fresh_ids = {sample_id for sample_id, row in sample_by_id.items() if row["sample_origin"] == "fresh"}
    carry_ids = {sample_id for sample_id, row in sample_by_id.items() if row["sample_origin"] == "carry_forward"}
    reviews = list(review_state["reviews"])
    review_by_sample_lane = {
        (str(row["sample_id"]), str(row["lane"])): row for row in reviews
    }
    adjudications = _rows(
        sidecar,
        "SELECT * FROM adjudications WHERE run_id=? ORDER BY sample_id",
        (run_id,),
    )
    _check(checks, "fresh_adjudication_count", FRESH_ADJUDICATION_COUNT, len(adjudications))
    _check(checks, "adjudications_fresh_only", set(), {str(row["sample_id"]) for row in adjudications} - fresh_ids)
    _check(checks, "carry_forward_has_no_new_adjudication", set(), {str(row["sample_id"]) for row in adjudications} & carry_ids)
    adjudication_failures: list[str] = []
    adjudicator_actors: set[str] = set()
    adjudicated_times: set[str] = set()
    for row in adjudications:
        sample_id = str(row["sample_id"])
        r1 = review_by_sample_lane.get((sample_id, "r1"))
        r2 = review_by_sample_lane.get((sample_id, "r2"))
        agreement = r1 is not None and r2 is not None and r1["label"] == r2["label"]
        unresolved = bool(row["unresolved_disagreement"])
        expected_final = "insufficient" if (not agreement and unresolved) else row["adjudicator_label"]
        adjudicator_actors.add(_normalized_actor(row["adjudicator"]))
        adjudicated_times.add(str(row["adjudicated_at"]))
        if (
            r1 is None
            or r2 is None
            or row["r1_label"] != r1["label"]
            or row["r2_label"] != r2["label"]
            or int(row["reviewer_agreement"]) != int(agreement)
            or row["adjudicator_label"] not in REVIEW_LABELS
            or row["final_gold_label"] not in REVIEW_LABELS
            or (agreement and unresolved)
            or row["final_gold_label"] != expected_final
            or not str(row["rationale"]).strip()
            or not _normalized_actor(row["adjudicator"])
        ):
            adjudication_failures.append(sample_id)
        try:
            _iso_timestamp(str(row["adjudicated_at"]))
        except ValueError:
            adjudication_failures.append(f"{sample_id}:timestamp")
    reviewers = {_normalized_actor(row["reviewer"]) for row in review_state["locks"]}
    if len(adjudicator_actors) != 1 or not adjudicator_actors.isdisjoint(reviewers):
        adjudication_failures.append("actor_independence")
    _check(checks, "adjudication_bindings_resolution_and_actor", [], adjudication_failures)
    _check(checks, "adjudication_sample_unique", FRESH_ADJUDICATION_COUNT, len({row["sample_id"] for row in adjudications}))

    adjudication_by_id = {str(row["sample_id"]): row for row in adjudications}
    core_samples = sorted(
        (row for row in samples if row["cohort"] == "A"),
        key=lambda row: int(row["sample_rank"]),
    )
    core_labels = [
        str(adjudication_by_id.get(str(row["sample_id"]), {}).get("final_gold_label", ""))
        for row in core_samples
    ]
    label_counts = _label_counts(core_labels)
    false_merge = label_counts["different"]
    same_count = label_counts["same"]
    hard_conflicts = 0
    for sample in core_samples:
        snapshot = sidecar.execute(
            "SELECT conflicts_json FROM candidate_snapshots WHERE run_id=? AND sample_id=?",
            (run_id, sample["sample_id"]),
        ).fetchone()
        if snapshot is not None and json.loads(str(snapshot[0])):
            hard_conflicts += 1
    review_item_rows = sidecar.execute(
        "SELECT sheet_path,sheet_sha256 FROM review_items WHERE run_id=?", (run_id,)
    ).fetchall()
    missing_sheets = max(0, TOTAL_DUAL_REVIEW_ROWS - len(review_item_rows))
    for row in review_item_rows:
        path = _resolve_repo_path(root, str(row[0]))
        if not path.is_file() or (verify_full_hashes and _sha256_file(path) != row[1]):
            missing_sheets += 1
    missing_reviews = max(0, TOTAL_DUAL_REVIEW_ROWS - len(reviews))
    missing_adjudications = max(0, FRESH_ADJUDICATION_COUNT - len(adjudications))
    missing_carry = max(0, CARRY_FORWARD_COUNT - len(sample_state["carry"]))
    unreviewable = missing_sheets + missing_reviews + missing_adjudications + missing_carry
    precision = same_count / A_SAMPLE_COUNT
    cp_low, cp_high = clopper_pearson_95(same_count, A_SAMPLE_COUNT)
    gate_pass = false_merge == 0 and hard_conflicts == 0 and unreviewable == 0
    status = "PASS" if gate_pass else "FAIL"
    expected_gate = {
        "status": status,
        "run_id": run_id,
        "gate_version": GATE_VERSION,
        "core_a_denominator": A_SAMPLE_COUNT,
        "core_label_counts": label_counts,
        "false_merge_count": false_merge,
        "hard_conflict_violation_count": hard_conflicts,
        "unreviewable_count": unreviewable,
        "exact_identity_precision": precision,
        "clopper_pearson_95": {"low": cp_low, "high": cp_high},
        "establishes_99_percent_precision": False,
        "authorizes_auto_merge": False,
        "human_approval_required": True,
    }
    diagnostic = _replay_diagnostic_b(sidecar, run_id)
    expected_report = {
        "status": status,
        "run_id": run_id,
        "review_completion": {
            "r1_fresh": f"{FRESH_SAMPLE_COUNT}/{FRESH_SAMPLE_COUNT}",
            "r2_fresh": f"{FRESH_SAMPLE_COUNT}/{FRESH_SAMPLE_COUNT}",
            "fresh_adjudication": f"{FRESH_ADJUDICATION_COUNT}/{FRESH_ADJUDICATION_COUNT}",
            "carry_provenance": f"{CARRY_FORWARD_COUNT}/{CARRY_FORWARD_COUNT}",
        },
        "core_gate": expected_gate,
        "diagnostic_b": diagnostic,
        "disagreements": sum(not bool(row["reviewer_agreement"]) for row in adjudications),
        "unresolved_disagreements": sum(bool(row["unresolved_disagreement"]) for row in adjudications),
    }
    external_failures: list[str] = []
    gate_path = root / DEFAULT_GATE_REPORT
    stage_report_path = root / DEFAULT_STAGE_REPORT
    for role, path, expected in (
        ("gate", gate_path, expected_gate),
        ("report", stage_report_path, expected_report),
    ):
        try:
            if json.loads(path.read_text(encoding="utf-8")) != expected:
                external_failures.append(f"{role}:payload")
        except (OSError, json.JSONDecodeError):
            external_failures.append(f"{role}:missing_or_json")
    _check(checks, "independent_a70_gate_and_b_diagnostics", [], external_failures)

    metric_rows = {
        (str(row["phase"]), str(row["metric"])): _json_value(row["value_json"])
        for row in _rows(sidecar, "SELECT * FROM metrics WHERE run_id=?", (run_id,))
    }
    expected_metrics = {
        ("gate", "core_a"): expected_gate,
        ("diagnostic", "b_fresh_and_carry"): diagnostic,
        ("completion", "review_rows"): {
            "r1": FRESH_SAMPLE_COUNT,
            "r2": FRESH_SAMPLE_COUNT,
            "adjudicated": FRESH_ADJUDICATION_COUNT,
            "carry": CARRY_FORWARD_COUNT,
        },
    }
    _check(checks, "metrics_semantic_replay", expected_metrics, metric_rows)

    expected_terminal = status.casefold()
    run_status = {
        "status": run["status"],
        "r1_status": run["r1_status"],
        "r2_status": run["r2_status"],
        "unblind_status": run["unblind_status"],
        "adjudication_status": run["adjudication_status"],
        "gate_status": run["gate_status"],
    }
    _check(
        checks,
        "run_terminal_statuses",
        {
            "status": expected_terminal,
            "r1_status": "locked",
            "r2_status": "locked",
            "unblind_status": "complete",
            "adjudication_status": "complete",
            "gate_status": expected_terminal,
        },
        run_status,
    )
    _check(checks, "core_gate_different_only_fail_rule", expected_terminal, run["status"])
    metrics["gate"] = expected_gate
    metrics["diagnostic_b"] = diagnostic
    metrics["related_excluded_from_precision_numerator"] = label_counts["related"]

    locked_path = root / DEFAULT_ADJUDICATION_LOCKED
    manifest_path = root / DEFAULT_MANIFEST
    manifest_failures: list[str] = []
    if len(adjudicator_actors) == 1 and len(adjudicated_times) == 1:
        ordered_adjudications = sorted(
            adjudications,
            key=lambda row: int(sample_by_id[str(row["sample_id"])]["sample_rank"]),
        )
        locked_items = []
        for row in ordered_adjudications:
            sample = sample_by_id[str(row["sample_id"])]
            locked_items.append(
                {
                    "candidate_id": sample["candidate_id"],
                    "sample_id": row["sample_id"],
                    "r1_label": row["r1_label"],
                    "r2_label": row["r2_label"],
                    "reviewer_agreement": row["reviewer_agreement"],
                    "adjudicator_label": row["adjudicator_label"],
                    "final_gold_label": row["final_gold_label"],
                    "unresolved_disagreement": row["unresolved_disagreement"],
                    "rationale": row["rationale"],
                    "adjudicator": row["adjudicator"],
                    "adjudicated_at": row["adjudicated_at"],
                }
            )
        expected_locked = {
            "adjudication_version": ADJUDICATION_VERSION,
            "run_id": run_id,
            "adjudicator": next(iter(adjudicator_actors)),
            "adjudicated_at": next(iter(adjudicated_times)),
            "fresh_row_count": FRESH_ADJUDICATION_COUNT,
            "items": locked_items,
        }
        try:
            actual_locked = json.loads(locked_path.read_text(encoding="utf-8"))
            # Preserve display spelling of the adjudicator in the top-level key.
            expected_locked["adjudicator"] = ordered_adjudications[0]["adjudicator"] if ordered_adjudications else ""
            if actual_locked != expected_locked:
                manifest_failures.append("locked_adjudication_payload")
        except (OSError, json.JSONDecodeError):
            manifest_failures.append("locked_adjudication_missing")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        cache_rows = [
            dict(row)
            for row in sidecar.execute(
                "SELECT role,path,access_mode,file_count_before,total_bytes_before,"
                "tree_sha256_before,file_count_after,total_bytes_after,tree_sha256_after "
                "FROM cache_pins WHERE run_id=? ORDER BY role",
                (run_id,),
            )
        ]
        expected_manifest = {
            "status": status,
            "run_id": run_id,
            "versions": {
                "runner": RUNNER_VERSION,
                "sample": SAMPLE_VERSION,
                "review": REVIEW_VERSION,
                "adjudication": ADJUDICATION_VERSION,
                "gate": GATE_VERSION,
            },
            "sidecar": {
                "path": artifact.relative_to(root).as_posix() if artifact.is_relative_to(root) else artifact.as_posix(),
                "size_bytes": artifact.stat().st_size,
                "sha256": _sha256_file(artifact) if verify_full_hashes else manifest.get("sidecar", {}).get("sha256"),
            },
            "v3_immutable": {
                "path": DEFAULT_V3.as_posix(),
                "sha256_before": V3_SHA256,
                "sha256_after": V3_SHA256,
            },
            "sample_logical_sha256": run["sample_logical_sha256"],
            "cache_pins": cache_rows,
            "artifacts": {
                "locked_adjudication": {
                    "path": DEFAULT_ADJUDICATION_LOCKED.as_posix(),
                    "sha256": _sha256_file(locked_path) if locked_path.is_file() else None,
                },
                "gate": {
                    "path": DEFAULT_GATE_REPORT.as_posix(),
                    "sha256": _sha256_file(gate_path) if gate_path.is_file() else None,
                },
                "report": {
                    "path": DEFAULT_STAGE_REPORT.as_posix(),
                    "sha256": _sha256_file(stage_report_path) if stage_report_path.is_file() else None,
                },
            },
            "review_rows": {
                "r1": FRESH_SAMPLE_COUNT,
                "r2": FRESH_SAMPLE_COUNT,
                "adjudicated": FRESH_ADJUDICATION_COUNT,
                "carry": CARRY_FORWARD_COUNT,
            },
            "network_requests": int(run["wire_http_requests"]),
            "vision_requests": 0,
            "llm_requests": 0,
            "merge_executions": 0,
            "vnext_id_issuances": 0,
            "human_approval_required": True,
        }
        if manifest != expected_manifest:
            manifest_failures.append("manifest_payload_or_hash")
    except (OSError, json.JSONDecodeError, ValueError):
        manifest_failures.append("manifest_missing_or_json")
    _check(checks, "final_artifact_manifest_hashes", [], manifest_failures)


def _binomial_cdf(successes: int, trials: int, probability: float) -> float:
    return sum(
        math.comb(trials, k)
        * probability**k
        * (1.0 - probability) ** (trials - k)
        for k in range(successes + 1)
    )


def _binomial_sf_at_least(successes: int, trials: int, probability: float) -> float:
    return sum(
        math.comb(trials, k)
        * probability**k
        * (1.0 - probability) ** (trials - k)
        for k in range(successes, trials + 1)
    )


def clopper_pearson_95(successes: int, trials: int) -> tuple[float, float]:
    """Return an exact equal-tailed two-sided 95% binomial interval."""

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


def _json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return {"__bytes_hex__": value.hex()}
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_json_safe(item) for item in value]
        return sorted(items, key=_canonical_json)
    if isinstance(value, Mapping):
        entries: list[tuple[str, str, Any]] = []
        for key, item in value.items():
            safe_key = _json_safe(key)
            canonical_key = _canonical_json(safe_key)
            display_key = (
                key
                if isinstance(key, str)
                else f"@{type(key).__name__}:{canonical_key}"
            )
            safe_item = _json_safe(item)
            entries.append(
                (str(display_key), canonical_key + "\0" + _canonical_json(safe_item), safe_item)
            )
        entries.sort(key=lambda row: (row[0], row[1]))
        result: dict[str, Any] = {}
        for display_key, _sort_key, safe_item in entries:
            unique_key = display_key
            suffix = 2
            while unique_key in result:
                unique_key = f"{display_key}#{suffix}"
                suffix += 1
            result[unique_key] = safe_item
        return result
    return value


def _validate_state_events(
    connection: sqlite3.Connection,
    run: Mapping[str, Any],
    checks: list[Check],
    *,
    phase: str = "final",
) -> None:
    run_id = str(run["run_id"])
    events = _rows(
        connection,
        "SELECT * FROM state_events WHERE run_id=? ORDER BY event_no",
        (run_id,),
    )
    expected_statuses = (
        ["building", "sample_frozen"]
        if phase == "sample"
        else [
            "building",
            "sample_frozen",
            "fetching",
            "review_ready",
            "reviews_partial",
            "reviews_locked",
            "unblinded",
            str(run["status"]),
        ]
    )
    actual_statuses = [str(events[0]["from_status"])] if events else []
    actual_statuses.extend(str(row["to_status"]) for row in events)
    _check(checks, "state_transition_chain", expected_statuses, actual_statuses)
    _check(checks, "state_event_numbers_contiguous", list(range(1, len(events) + 1)), [int(row["event_no"]) for row in events])
    timestamp_failures: list[int] = []
    previous: datetime | None = None
    for row in events:
        try:
            current = _iso_timestamp(str(row["occurred_at"]))
            if previous is not None and current < previous:
                timestamp_failures.append(int(row["event_no"]))
            previous = current
        except ValueError:
            timestamp_failures.append(int(row["event_no"]))
    _check(checks, "state_event_timestamps_monotonic", [], timestamp_failures)


def _validate_stage_manifests(
    root: Path,
    connection: sqlite3.Connection,
    run: Mapping[str, Any],
    sample_state: Mapping[str, Any],
    metrics: Mapping[str, Any],
    checks: list[Check],
    *,
    verify_full_hashes: bool,
    phase: str = "final",
) -> None:
    run_id = str(run["run_id"])
    failures: list[str] = []
    try:
        sample_manifest = json.loads(
            (root / DEFAULT_SAMPLE_MANIFEST).read_text(encoding="utf-8")
        )
        input_rows = {
            str(row["role"]): row
            for row in _rows(
                connection,
                "SELECT * FROM input_pins WHERE run_id=? AND role IN "
                "('v3_candidates_final','n100_protocol','wp23_order') ORDER BY role",
                (run_id,),
            )
        }
        sample_rows = [
            {
                "sample_rank": row["sample_rank"],
                "sample_id": row["sample_id"],
                "cohort": row["cohort"],
                "stratum": row["stratum"],
                "sample_origin": row["sample_origin"],
                "candidate_id": row["candidate_id"],
                "anchor_type": row["anchor_type"],
                "auxiliary_mask": row["auxiliary_mask"],
                "seed_rank_sha256": row["seed_rank_sha256"],
            }
            for row in sample_state["samples"]
        ]
        replay = sample_state["replay"]
        expected_without_sample_bytes = {
            "status": "PASS",
            "run_id": run_id,
            "runner_version": RUNNER_VERSION,
            "sample_version": SAMPLE_VERSION,
            "seed": SAMPLE_SEED,
            "counts": {
                "total": TOTAL_SAMPLE_COUNT,
                "core_a": A_SAMPLE_COUNT,
                "diagnostic_b": B_SAMPLE_COUNT,
                "carry_forward": CARRY_FORWARD_COUNT,
                "fresh_diagnostic": sum(B_FRESH_SAMPLE_COUNTS.values()),
                "fresh_review_rows_per_lane": FRESH_SAMPLE_COUNT,
                "prior_review_exclusions": PRIOR_FOCAL_REVIEW_COUNT,
                "core_population_after_exclusion": replay["a_frame_count"],
            },
            "hierarchical_hamilton_allocations": [
                {"anchor_type": key[0], "auxiliary_mask": key[1], "quota": value}
                for key, value in sorted(replay["a_quotas"].items())
            ],
            "diagnostic_populations": {
                key: {
                    "full": len(value),
                    "fresh": replay["b_fresh_counts"][key],
                    "carry_forward": len(CARRY_FORWARD[key]),
                    "fresh_quota": B_FRESH_SAMPLE_COUNTS[key],
                }
                for key, value in sorted(replay["b_pools"].items())
            },
            "samples": sample_rows,
            "endpoint_reuse_diagnostic": {
                "distinct_endpoints": metrics["endpoint_overlap"]["distinct_endpoints"],
                "reused_endpoint_count": metrics["endpoint_overlap"]["reused_endpoint_count"],
                "maximum_candidate_occurrences": metrics["endpoint_overlap"]["maximum_candidate_occurrences"],
                "selection_constraint": False,
            },
            "sample_logical_sha256": run["sample_logical_sha256"],
            "input_pins": {
                role: {
                    "path": row["path"],
                    "size_bytes": row["size_bytes"],
                    "sha256_before": row["sha256_before"],
                    "sha256_after": row["sha256_after"],
                    "usage": row["usage"],
                }
                for role, row in sorted(input_rows.items())
            },
            "network_requests": 0,
            "vision_requests": 0,
            "llm_requests": 0,
            "merge_executions": 0,
            "vnx_id_issuances": 0,
        }
        actual_without_sample_bytes = {
            key: value
            for key, value in sample_manifest.items()
            if key not in {"sidecar_sample_bytes", "sidecar_sample_sha256"}
        }
        if actual_without_sample_bytes != expected_without_sample_bytes:
            failures.append("sample_manifest_payload")
        if (
            not isinstance(sample_manifest.get("sidecar_sample_bytes"), int)
            or int(sample_manifest["sidecar_sample_bytes"]) <= 0
            or not isinstance(sample_manifest.get("sidecar_sample_sha256"), str)
            or len(sample_manifest["sidecar_sample_sha256"]) != 64
        ):
            failures.append("sample_manifest_freeze_pin_shape")
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        failures.append("sample_manifest_missing_or_invalid")

    if phase == "sample":
        _check(checks, "sample_manifest_replay", [], failures)
        return

    try:
        fetch_manifest = json.loads(
            (root / DEFAULT_FETCH_MANIFEST).read_text(encoding="utf-8")
        )
        fetch_report = json.loads(
            (root / DEFAULT_FETCH_REPORT).read_text(encoding="utf-8")
        )
        fetch_rows = _rows(
            connection,
            "SELECT asset_key,source,source_asset_id,status,cache_role,cache_path,"
            "request_count,error_kind FROM fetch_assets WHERE run_id=? ORDER BY asset_key",
            (run_id,),
        )
        cache_rows = _rows(
            connection,
            "SELECT role,path,transform_version,access_mode,file_count_before,total_bytes_before,"
            "tree_sha256_before,file_count_after,total_bytes_after,tree_sha256_after "
            "FROM cache_pins WHERE run_id=? ORDER BY role",
            (run_id,),
        )
        review_items = _rows(
            connection,
            "SELECT lane,display_order,opaque_review_id,sheet_path,sheet_sha256 "
            "FROM review_items WHERE run_id=? ORDER BY lane,display_order",
            (run_id,),
        )
        forms: dict[str, dict[str, Any]] = {}
        for lane, relative in (("r1", DEFAULT_R1_FORM), ("r2", DEFAULT_R2_FORM)):
            blank_payload = _blank_reviewer_form_payload(
                run_id,
                lane,
                [row for row in review_items if row["lane"] == lane],
            )
            forms[lane] = {
                "path": relative.as_posix(),
                "sha256": _artifact_json_sha(blank_payload),
            }
        expected_fetch_manifest = {
            "status": "PASS",
            "run_id": run_id,
            "review_version": REVIEW_VERSION,
            "fresh_review_rows_per_lane": FRESH_SAMPLE_COUNT,
            "carry_forward_provenance_rows": CARRY_FORWARD_COUNT,
            "asset_occurrence_count": int(run["asset_occurrence_count"]),
            "unique_asset_count": int(run["unique_asset_count"]),
            "logical_http_fetches": int(run["logical_http_fetches"]),
            "wire_http_requests": int(run["wire_http_requests"]),
            "hard_caps": {
                "asset_occurrences": MAX_ASSET_OCCURRENCES,
                "logical_http_fetches": MAX_LOGICAL_HTTP_FETCHES,
                "wire_http_requests": MAX_WIRE_HTTP_REQUESTS,
                "redirects": 0,
                "retries": 0,
            },
            "v3_sha256_before": V3_SHA256,
            "v3_sha256_after": V3_SHA256,
            "cache_pins": cache_rows,
            "fetch_assets": fetch_rows,
            "forms": forms,
            "sheet_count": TOTAL_DUAL_REVIEW_ROWS,
            "vision_requests": 0,
            "llm_requests": 0,
        }
        expected_fetch_report = {
            "status": "PASS",
            "run_id": run_id,
            "counts": dict(sorted({
                status: sum(row["status"] == status for row in fetch_rows)
                for status in {str(row["status"]) for row in fetch_rows}
            }.items())),
            "review_completion_contract": {
                "r1_fresh": f"{FRESH_SAMPLE_COUNT}/{FRESH_SAMPLE_COUNT}",
                "r2_fresh": f"{FRESH_SAMPLE_COUNT}/{FRESH_SAMPLE_COUNT}",
                "carry_provenance": f"{CARRY_FORWARD_COUNT}/{CARRY_FORWARD_COUNT}",
            },
            "network": {
                "logical": int(run["logical_http_fetches"]),
                "wire": int(run["wire_http_requests"]),
                "max_redirects": 0,
                "retries": 0,
            },
        }
        if fetch_manifest != expected_fetch_manifest:
            failures.append("fetch_manifest_payload_or_form_hash")
        if fetch_report != expected_fetch_report:
            failures.append("fetch_report_payload")
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        failures.append("fetch_artifact_missing_or_invalid")
    _check(checks, "sample_and_fetch_manifests_replay", [], failures)


def _write_report(result: ValidationResult, path: Path) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to clobber validation report: {path}")
    payload = {
        "status": "PASS" if result.passed else "FAIL",
        "phase": result.phase,
        "validator_version": VALIDATOR_VERSION,
        "artifact_path": str(result.artifact_path),
        "run_id": result.run_id,
        "passed_checks": sum(row.passed for row in result.checks),
        "total_checks": len(result.checks),
        "failed_checks": list(result.failed_check_names),
        "checks": [
            {
                "name": row.name,
                "passed": row.passed,
                "expected": _json_safe(row.expected),
                "actual": _json_safe(row.actual),
                "detail": row.detail,
            }
            for row in result.checks
        ],
        "metrics": _json_safe(result.metrics),
        "validated_at": datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".building")
    if temporary.exists():
        raise FileExistsError(f"stale validation report temporary: {temporary}")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def validate_identity_n100(
    *,
    repo_root: Path,
    artifact_path: Path = DEFAULT_ARTIFACT,
    report_path: Path | None = DEFAULT_REPORT,
    verify_full_hashes: bool = True,
    phase: str = "final",
) -> ValidationResult:
    """Independently validate either the sample freeze or terminal N100 state."""

    if phase not in {"sample", "final"}:
        raise ValueError("phase must be 'sample' or 'final'")

    root = repo_root.resolve()
    artifact = artifact_path if artifact_path.is_absolute() else root / artifact_path
    report = None if report_path is None else (
        report_path if report_path.is_absolute() else root / report_path
    )
    checks: list[Check] = []
    metrics: dict[str, Any] = {}
    metrics["phase"] = phase
    run_id: str | None = None

    v3_path = root / DEFAULT_V3
    _check(checks, "v3_artifact_exists", True, v3_path.is_file())
    if v3_path.is_file():
        _check(checks, "v3_artifact_size_immutable", V3_SIZE_BYTES, v3_path.stat().st_size)
        _check(
            checks,
            "v3_artifact_sha256_immutable",
            V3_SHA256,
            _sha256_file(v3_path) if verify_full_hashes else V3_SHA256,
        )
    _check(checks, "artifact_exists", True, artifact.is_file())
    if artifact.is_file():
        _check(checks, "artifact_has_no_sqlite_sidecars", True, _no_sqlite_sidecars(artifact))
        no_clobber_targets = (
            artifact,
            root / DEFAULT_SAMPLE_MANIFEST,
            root / DEFAULT_FETCH_MANIFEST,
            root / DEFAULT_FETCH_REPORT,
            root / DEFAULT_R1_FORM,
            root / DEFAULT_R2_FORM,
            root / DEFAULT_ADJUDICATION_PACKET,
            root / DEFAULT_ADJUDICATION_FORM,
            root / DEFAULT_ADJUDICATION_LOCKED,
            root / DEFAULT_GATE_REPORT,
            root / DEFAULT_STAGE_REPORT,
            root / DEFAULT_MANIFEST,
        )
        stale_temporaries = sorted(
            str(candidate)
            for target in no_clobber_targets
            for candidate in (
                target.with_name(target.name + ".building"),
                target.with_name(target.name + ".building.failed"),
            )
            if candidate.exists()
        )
        _check(checks, "no_stale_no_clobber_temporaries", [], stale_temporaries)
        sidecar: sqlite3.Connection | None = None
        v3: sqlite3.Connection | None = None
        try:
            sidecar = _open_readonly(artifact)
            application_id = int(sidecar.execute("PRAGMA application_id").fetchone()[0])
            user_version = int(sidecar.execute("PRAGMA user_version").fetchone()[0])
            _check(checks, "sqlite_application_id", APPLICATION_ID, application_id)
            _check(checks, "sqlite_schema_version", SCHEMA_VERSION, user_version)
            tables = _table_names(sidecar)
            _check(checks, "required_tables_present", set(), set(REQUIRED_TABLES) - tables)
            quick = [str(row[0]) for row in sidecar.execute("PRAGMA quick_check")]
            _check(checks, "sqlite_quick_check", ["ok"], quick)
            foreign = [tuple(row) for row in sidecar.execute("PRAGMA foreign_key_check")]
            _check(checks, "sqlite_foreign_key_check", [], foreign)
            run_rows = _rows(sidecar, "SELECT * FROM runs")
            _check(checks, "single_run_row", 1, len(run_rows))
            if len(run_rows) != 1:
                raise ValueError("N100 sidecar must contain exactly one run")
            run = run_rows[0]
            run_id = str(run["run_id"])
            expected_run_id = "wp23-" + hashlib.sha256(
                f"{SAMPLE_VERSION}\0{SAMPLE_SEED}\0{V3_SHA256}".encode("utf-8")
            ).hexdigest()[:24]
            _check(checks, "run_id_derivation", expected_run_id, run_id)
            versions = {
                "schema_version": run["schema_version"],
                "runner_version": run["runner_version"],
                "sample_version": run["sample_version"],
                "review_version": run["review_version"],
                "adjudication_version": run["adjudication_version"],
                "gate_version": run["gate_version"],
                "seed": run["seed"],
            }
            _check(
                checks,
                "run_protocol_versions_and_seed",
                {
                    "schema_version": SCHEMA_VERSION,
                    "runner_version": RUNNER_VERSION,
                    "sample_version": SAMPLE_VERSION,
                    "review_version": REVIEW_VERSION,
                    "adjudication_version": ADJUDICATION_VERSION,
                    "gate_version": GATE_VERSION,
                    "seed": SAMPLE_SEED,
                },
                versions,
            )
            _check(
                checks,
                "run_sample_accounting",
                (TOTAL_SAMPLE_COUNT, A_SAMPLE_COUNT, B_SAMPLE_COUNT),
                (int(run["sample_count"]), int(run["core_count"]), int(run["diagnostic_count"])),
            )
            if phase == "final":
                _check(checks, "run_is_terminal", True, str(run["status"]) in {"pass", "fail"})
                _check(checks, "run_completed_at_present", True, bool(run["completed_at"]))

            if not v3_path.is_file():
                raise ValueError("v3 artifact unavailable for independent replay")
            v3 = _open_readonly(v3_path)
            v3_runs = _rows(v3, "SELECT run_id,status FROM runs")
            _check(
                checks,
                "v3_single_final_pass_run",
                (1, ["pass"]),
                (len(v3_runs), [str(row["status"]) for row in v3_runs]),
            )
            sample_state = _validate_sample_stage(sidecar, v3, run, checks, metrics)
            if phase == "sample":
                _validate_sample_boundary(
                    root,
                    sidecar,
                    run,
                    checks,
                    metrics,
                    verify_full_hashes=verify_full_hashes,
                )
                _validate_stage_manifests(
                    root,
                    sidecar,
                    run,
                    sample_state,
                    metrics,
                    checks,
                    verify_full_hashes=verify_full_hashes,
                    phase="sample",
                )
                _validate_state_events(sidecar, run, checks, phase="sample")
            else:
                _validate_fetch_and_review_items(
                    root,
                    sidecar,
                    v3,
                    run,
                    sample_state,
                    checks,
                    metrics,
                    verify_full_hashes=verify_full_hashes,
                )
                _validate_stage_manifests(
                    root,
                    sidecar,
                    run,
                    sample_state,
                    metrics,
                    checks,
                    verify_full_hashes=verify_full_hashes,
                    phase="final",
                )
                review_state = _validate_review_barrier(
                    root,
                    sidecar,
                    run,
                    sample_state,
                    checks,
                    metrics,
                    verify_full_hashes=verify_full_hashes,
                )
                _validate_adjudication_and_gate(
                    root,
                    artifact,
                    sidecar,
                    run,
                    sample_state,
                    review_state,
                    checks,
                    metrics,
                    verify_full_hashes=verify_full_hashes,
                )
                _validate_state_events(sidecar, run, checks, phase="final")
            required_triggers = {
                "sample_pairs_no_update", "sample_pairs_no_delete",
                "snapshots_no_update", "snapshots_no_delete",
                "sample_pairs_insert_after_freeze", "snapshots_insert_after_freeze",
                "exclusions_no_update", "exclusions_no_delete",
                "carry_gold_no_update", "carry_gold_no_delete",
                "review_items_no_update", "review_items_no_delete",
                "review_items_insert_after_ready", "sample_assets_change_after_ready",
                "sample_assets_delete_after_ready", "fetch_assets_change_after_ready",
                "fetch_assets_delete_after_ready", "blind_reviews_change_after_lock",
                "blind_reviews_delete_after_lock", "blind_reviews_insert_after_lock",
                "state_events_no_update", "state_events_no_delete",
            }
            _check(checks, "freeze_and_lock_triggers_present", set(), required_triggers - _trigger_names(sidecar))
            record_failures = _record_sha_failures(sidecar, REQUIRED_TABLES)
            _check(checks, "all_table_record_sha256_replay", [], record_failures)
            metrics["record_sha_rows"] = {
                table: int(sidecar.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
                for table in sorted(REQUIRED_TABLES)
            }
        except (sqlite3.Error, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            _check(
                checks,
                "validator_completed_without_exception",
                "no exception",
                f"{type(exc).__name__}: {exc}",
            )
        finally:
            if v3 is not None:
                v3.close()
            if sidecar is not None:
                sidecar.close()

    result = ValidationResult(artifact, report, run_id, tuple(checks), metrics, phase)
    if report is not None:
        _write_report(result, report)
    return result
