"""WP2.3 deterministic identity N100 sampling and blind-review sidecar.

The final WP2.2 v3 candidate database is an immutable input.  This module
stores every N100 sample, HTTP, cache, review, and adjudication row in a
separate no-clobber SQLite sidecar.  It never executes a merge, issues a vNext
ID, or calls Vision/LLM APIs.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import sqlite3
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from canonical.cross_source_image_selection import canonical_json, canonical_sha256
from canonical.cross_source_semantic_fetch import FetchFailure, FetchPayload, fetch_once
from canonical.divisare_vision_benchmark import decode_source, prepare_derivative
from canonical.image_fingerprint import FingerprintError, fingerprint_bytes
from canonical import vnext_identity_review as n10_review


SCHEMA_VERSION = 1
APPLICATION_ID = 0x4E313030  # N100
RUNNER_VERSION = "vnext-identity-n100-v1.0.0"
SAMPLE_VERSION = "vnext-identity-wp23-n100-sample-v1"
REVIEW_VERSION = "vnext-identity-wp23-dual-blind-v1"
ADJUDICATION_VERSION = "vnext-identity-wp23-adjudication-v1"
GATE_VERSION = "vnext-identity-wp23-core-a70-gate-v1"
SEED = "vnext-wp23-identity-n100-v1-20260826"

CORE_COUNT = 70
DIAGNOSTIC_COUNT = 40
TOTAL_COUNT = 110
FRESH_REVIEW_COUNT = 105
MAX_ASSETS_PER_SIDE = 2
MAX_ASSET_OCCURRENCES = FRESH_REVIEW_COUNT * 2 * MAX_ASSETS_PER_SIDE
MAX_LOGICAL_HTTP_FETCHES = 420
MAX_WIRE_HTTP_REQUESTS = 500
MAX_WORKERS = 4

DEFAULT_V3 = Path("data/enrichment/vnext_identity_candidates_v3.db")
DEFAULT_OUTPUT = Path("data/enrichment/vnext_identity_n100_v1.db")
DEFAULT_OUTPUT_CACHE = Path("data/enrichment/vnext_identity_n100_cache_v1")
DEFAULT_SAMPLE_MANIFEST = Path("data/reports/vnext_identity_n100_v1_sample_manifest.json")
DEFAULT_FETCH_MANIFEST = Path("data/reports/vnext_identity_n100_v1_fetch_manifest.json")
DEFAULT_FETCH_REPORT = Path("data/reports/vnext_identity_n100_v1_fetch_report.json")
DEFAULT_SHEETS_ROOT = Path("data/reports/vnext_identity_n100_v1_blind")
DEFAULT_R1_FORM = Path("data/reports/vnext_identity_n100_v1_reviewer_r1_draft.json")
DEFAULT_R2_FORM = Path("data/reports/vnext_identity_n100_v1_reviewer_r2_draft.json")
DEFAULT_R1_LOCKED = Path("data/reports/vnext_identity_n100_v1_reviewer_r1_locked.json")
DEFAULT_R2_LOCKED = Path("data/reports/vnext_identity_n100_v1_reviewer_r2_locked.json")
DEFAULT_ADJUDICATION_PACKET = Path("data/reports/vnext_identity_n100_v1_adjudication_packet.json")
DEFAULT_ADJUDICATION_FORM = Path("data/reports/vnext_identity_n100_v1_adjudication_draft.json")
DEFAULT_ADJUDICATION_LOCKED = Path("data/reports/vnext_identity_n100_v1_adjudication_locked.json")
DEFAULT_GATE = Path("data/reports/vnext_identity_n100_v1_gate.json")
DEFAULT_REPORT = Path("data/reports/vnext_identity_n100_v1_report.json")
DEFAULT_MANIFEST = Path("data/reports/vnext_identity_n100_v1_manifest.json")
DEFAULT_PROTOCOL = Path("docs/vnext/IDENTITY_N100_PROTOCOL.md")
DEFAULT_ORDER = Path(".claude/ops/jobs/20260826_vnext_wp23_identity_n100_order.md")

V3_FINAL_SPEC = (
    580_710_400,
    "c71934cfd30807e523842133ffeb7e47d5a863d539feecd763d1086fd65d7a2f",
)
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


@dataclass(frozen=True)
class CacheSpec:
    role: str
    path: Path
    transform_version: str
    file_count: int
    total_bytes: int
    tree_sha256: str


DEFAULT_READ_CACHES: tuple[CacheSpec, ...] = (
    CacheSpec(
        "wp1_p4b",
        Path("data/enrichment/vnext_p4b_pixel_cache_v1"),
        n10_review.WP1_TRANSFORM_VERSION,
        151,
        26_909_641,
        "9bdc3b9b8b37808866d321420fc283a9270db4be021d561bfd756ed4f14983f6",
    ),
    CacheSpec(
        "wp2_v1_v2_review",
        Path("data/enrichment/vnext_identity_review_cache_v1"),
        n10_review.TRANSFORM_VERSION,
        73,
        13_522_544,
        "b1356938dbbb4a011963cf78e3a3903e9e2ac4ba5d6f2616033f996353b65aa7",
    ),
    CacheSpec(
        "wp22_v3_review",
        Path("data/enrichment/vnext_identity_review_cache_v3"),
        n10_review.TRANSFORM_VERSION,
        35,
        4_686_994,
        "4c99646c1ba084af1a54d44ee434e9b60997b8a78600d51e9b92193d159fe323",
    ),
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
FRESH_QUOTAS: Mapping[str, int] = {
    "B1_year_gate": 9,
    "B2_phash_veto": 9,
    "B3_fanout_defer": 7,
    "B4_high_name_reject": 10,
}

ALLOWED_LABELS = frozenset({"same", "related", "different", "insufficient"})
READABLE_ASSET_STATUSES = frozenset(
    {"cache_hit", "exact", "delivery_changed", "source_changed"}
)
TERMINAL_ASSET_STATUSES = READABLE_ASSET_STATUSES | {"failed"}


SCHEMA_SQL = """
CREATE TABLE runs(
  run_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL,
  runner_version TEXT NOT NULL, sample_version TEXT NOT NULL,
  review_version TEXT NOT NULL, adjudication_version TEXT NOT NULL,
  gate_version TEXT NOT NULL, seed TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN (
    'building','sample_frozen','fetching','review_ready','reviews_partial',
    'reviews_locked','unblinded','pass','fail','failed')),
  sample_count INTEGER NOT NULL DEFAULT 0, core_count INTEGER NOT NULL DEFAULT 0,
  diagnostic_count INTEGER NOT NULL DEFAULT 0,
  asset_occurrence_count INTEGER NOT NULL DEFAULT 0,
  unique_asset_count INTEGER NOT NULL DEFAULT 0,
  logical_http_fetches INTEGER NOT NULL DEFAULT 0,
  wire_http_requests INTEGER NOT NULL DEFAULT 0,
  r1_status TEXT NOT NULL DEFAULT 'pending',
  r2_status TEXT NOT NULL DEFAULT 'pending',
  unblind_status TEXT NOT NULL DEFAULT 'pending',
  adjudication_status TEXT NOT NULL DEFAULT 'pending',
  gate_status TEXT NOT NULL DEFAULT 'pending',
  sample_logical_sha256 TEXT, created_at TEXT NOT NULL, completed_at TEXT,
  record_sha256 TEXT NOT NULL
);
CREATE TABLE input_pins(
  run_id TEXT NOT NULL REFERENCES runs(run_id), role TEXT NOT NULL,
  path TEXT NOT NULL, size_bytes INTEGER NOT NULL, sha256_before TEXT NOT NULL,
  sha256_after TEXT, usage TEXT NOT NULL, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,role)
);
CREATE TABLE cache_pins(
  run_id TEXT NOT NULL REFERENCES runs(run_id), role TEXT NOT NULL,
  path TEXT NOT NULL, transform_version TEXT NOT NULL,
  access_mode TEXT NOT NULL CHECK(access_mode IN ('read_only','write_only')),
  file_count_before INTEGER NOT NULL, total_bytes_before INTEGER NOT NULL,
  tree_sha256_before TEXT NOT NULL, file_count_after INTEGER,
  total_bytes_after INTEGER, tree_sha256_after TEXT, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,role)
);
CREATE TABLE review_exclusions(
  run_id TEXT NOT NULL REFERENCES runs(run_id), candidate_id TEXT NOT NULL,
  prior_version TEXT NOT NULL, prior_opaque_review_id TEXT NOT NULL,
  carry_forward INTEGER NOT NULL CHECK(carry_forward IN (0,1)),
  record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,candidate_id,prior_version)
);
CREATE TABLE carry_forward_gold(
  run_id TEXT NOT NULL REFERENCES runs(run_id), sample_id TEXT NOT NULL,
  candidate_id TEXT NOT NULL, prior_run_id TEXT NOT NULL,
  prior_opaque_review_id TEXT NOT NULL, reviewer TEXT NOT NULL,
  adjudicator TEXT NOT NULL, final_gold_label TEXT NOT NULL,
  mapped_decision TEXT NOT NULL, blind_review_record_sha256 TEXT NOT NULL,
  reviewer_rationale TEXT NOT NULL, adjudicator_rationale TEXT NOT NULL,
  adjudication_record_sha256 TEXT NOT NULL, source_db_sha256 TEXT NOT NULL,
  record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,sample_id),
  UNIQUE(run_id,candidate_id)
);
CREATE TABLE population_cells(
  run_id TEXT NOT NULL REFERENCES runs(run_id), cohort TEXT NOT NULL,
  stratum TEXT NOT NULL, anchor_type TEXT NOT NULL DEFAULT '',
  auxiliary_mask TEXT NOT NULL DEFAULT '', population_count INTEGER NOT NULL,
  fresh_population_count INTEGER NOT NULL, quota INTEGER NOT NULL,
  predicate_version TEXT NOT NULL, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,cohort,stratum,anchor_type,auxiliary_mask)
);
CREATE TABLE sample_pairs(
  run_id TEXT NOT NULL REFERENCES runs(run_id), sample_rank INTEGER NOT NULL,
  sample_id TEXT NOT NULL, cohort TEXT NOT NULL CHECK(cohort IN ('A','B')),
  stratum TEXT NOT NULL, sample_origin TEXT NOT NULL
    CHECK(sample_origin IN ('fresh','carry_forward')),
  candidate_id TEXT NOT NULL, anchor_type TEXT NOT NULL DEFAULT '',
  auxiliary_mask TEXT NOT NULL DEFAULT '', seed_rank_sha256 TEXT NOT NULL,
  gate_scope INTEGER NOT NULL CHECK(gate_scope IN (0,1)),
  record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,sample_rank),
  UNIQUE(run_id,sample_id), UNIQUE(run_id,candidate_id)
);
CREATE TABLE candidate_snapshots(
  run_id TEXT NOT NULL REFERENCES runs(run_id), sample_id TEXT NOT NULL,
  candidate_json TEXT NOT NULL, left_metadata_json TEXT NOT NULL,
  right_metadata_json TEXT NOT NULL, origins_json TEXT NOT NULL,
  families_json TEXT NOT NULL, conflicts_json TEXT NOT NULL,
  policy_flags_json TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL,
  record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,sample_id),
  FOREIGN KEY(run_id,sample_id) REFERENCES sample_pairs(run_id,sample_id)
);
CREATE TABLE sample_assets(
  run_id TEXT NOT NULL REFERENCES runs(run_id), sample_id TEXT NOT NULL,
  side TEXT NOT NULL CHECK(side IN ('left','right')), asset_order INTEGER NOT NULL,
  asset_key TEXT NOT NULL, source TEXT NOT NULL, source_building_id TEXT NOT NULL,
  source_asset_id TEXT NOT NULL, selection_reason TEXT NOT NULL,
  record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,sample_id,side,asset_order),
  FOREIGN KEY(run_id,sample_id) REFERENCES sample_pairs(run_id,sample_id)
);
CREATE TABLE fetch_assets(
  run_id TEXT NOT NULL REFERENCES runs(run_id), asset_key TEXT NOT NULL,
  source TEXT NOT NULL, source_asset_id TEXT NOT NULL, expected_url TEXT,
  expected_raw_sha256 TEXT, expected_pixel_sha256 TEXT,
  status TEXT NOT NULL, cache_role TEXT, cache_path TEXT,
  actual_final_url TEXT, actual_raw_sha256 TEXT, actual_pixel_sha256 TEXT,
  http_status INTEGER, response_bytes INTEGER, request_count INTEGER NOT NULL DEFAULT 0,
  error_kind TEXT, error_message TEXT, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,asset_key), UNIQUE(run_id,source,source_asset_id)
);
CREATE TABLE fetch_attempts(
  run_id TEXT NOT NULL REFERENCES runs(run_id), asset_key TEXT NOT NULL,
  attempt_no INTEGER NOT NULL CHECK(attempt_no=1), status TEXT NOT NULL,
  request_count INTEGER NOT NULL, redirect_chain_json TEXT NOT NULL,
  started_at TEXT NOT NULL, completed_at TEXT NOT NULL,
  error_kind TEXT, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,asset_key,attempt_no)
);
CREATE TABLE review_items(
  run_id TEXT NOT NULL REFERENCES runs(run_id), lane TEXT NOT NULL
    CHECK(lane IN ('r1','r2')), display_order INTEGER NOT NULL,
  opaque_review_id TEXT NOT NULL, sample_id TEXT NOT NULL, sheet_path TEXT NOT NULL,
  sheet_sha256 TEXT NOT NULL, sheet_bytes INTEGER NOT NULL,
  record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,lane,opaque_review_id),
  UNIQUE(run_id,lane,display_order), UNIQUE(run_id,lane,sample_id)
);
CREATE TABLE blind_reviews(
  run_id TEXT NOT NULL REFERENCES runs(run_id), lane TEXT NOT NULL,
  opaque_review_id TEXT NOT NULL, sample_id TEXT NOT NULL, label TEXT NOT NULL,
  rationale TEXT NOT NULL, hard_conflict_observed TEXT NOT NULL,
  reviewer TEXT NOT NULL, reviewed_at TEXT NOT NULL, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,lane,opaque_review_id)
);
CREATE TABLE review_locks(
  run_id TEXT NOT NULL REFERENCES runs(run_id), lane TEXT NOT NULL,
  reviewer TEXT NOT NULL, row_count INTEGER NOT NULL,
  completed_form_path TEXT NOT NULL, completed_form_sha256 TEXT NOT NULL,
  locked_output_path TEXT NOT NULL, locked_output_sha256 TEXT NOT NULL,
  ordered_ledger_sha256 TEXT NOT NULL, locked_at TEXT NOT NULL,
  record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,lane)
);
CREATE TABLE unblind_artifacts(
  run_id TEXT PRIMARY KEY REFERENCES runs(run_id), packet_path TEXT NOT NULL,
  packet_sha256 TEXT NOT NULL, form_path TEXT NOT NULL, form_sha256 TEXT NOT NULL,
  r1_ledger_sha256 TEXT NOT NULL, r2_ledger_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL, record_sha256 TEXT NOT NULL
);
CREATE TABLE adjudications(
  run_id TEXT NOT NULL REFERENCES runs(run_id), sample_id TEXT NOT NULL,
  r1_label TEXT NOT NULL, r2_label TEXT NOT NULL, reviewer_agreement INTEGER NOT NULL,
  adjudicator_label TEXT NOT NULL, final_gold_label TEXT NOT NULL,
  unresolved_disagreement INTEGER NOT NULL CHECK(unresolved_disagreement IN (0,1)),
  rationale TEXT NOT NULL, adjudicator TEXT NOT NULL, adjudicated_at TEXT NOT NULL,
  record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,sample_id)
);
CREATE TABLE metrics(
  run_id TEXT NOT NULL REFERENCES runs(run_id), phase TEXT NOT NULL,
  metric TEXT NOT NULL, value_json TEXT NOT NULL, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,phase,metric)
);
CREATE TABLE state_events(
  run_id TEXT NOT NULL REFERENCES runs(run_id), event_no INTEGER NOT NULL,
  from_status TEXT NOT NULL, to_status TEXT NOT NULL, detail_json TEXT NOT NULL,
  occurred_at TEXT NOT NULL, record_sha256 TEXT NOT NULL,
  PRIMARY KEY(run_id,event_no)
);
CREATE TRIGGER sample_pairs_no_update BEFORE UPDATE ON sample_pairs
BEGIN SELECT RAISE(ABORT,'sample_pairs are frozen'); END;
CREATE TRIGGER sample_pairs_no_delete BEFORE DELETE ON sample_pairs
BEGIN SELECT RAISE(ABORT,'sample_pairs are frozen'); END;
CREATE TRIGGER snapshots_no_update BEFORE UPDATE ON candidate_snapshots
BEGIN SELECT RAISE(ABORT,'candidate snapshots are frozen'); END;
CREATE TRIGGER snapshots_no_delete BEFORE DELETE ON candidate_snapshots
BEGIN SELECT RAISE(ABORT,'candidate snapshots are frozen'); END;
CREATE TRIGGER sample_pairs_insert_after_freeze BEFORE INSERT ON sample_pairs
WHEN (SELECT status FROM runs WHERE run_id=NEW.run_id) <> 'building'
BEGIN SELECT RAISE(ABORT,'sample_pairs are frozen'); END;
CREATE TRIGGER snapshots_insert_after_freeze BEFORE INSERT ON candidate_snapshots
WHEN (SELECT status FROM runs WHERE run_id=NEW.run_id) <> 'building'
BEGIN SELECT RAISE(ABORT,'candidate snapshots are frozen'); END;
CREATE TRIGGER exclusions_no_update BEFORE UPDATE ON review_exclusions
BEGIN SELECT RAISE(ABORT,'review exclusions are frozen'); END;
CREATE TRIGGER exclusions_no_delete BEFORE DELETE ON review_exclusions
BEGIN SELECT RAISE(ABORT,'review exclusions are frozen'); END;
CREATE TRIGGER exclusions_insert_after_freeze BEFORE INSERT ON review_exclusions
WHEN (SELECT status FROM runs WHERE run_id=NEW.run_id) <> 'building'
BEGIN SELECT RAISE(ABORT,'review exclusions are frozen'); END;
CREATE TRIGGER population_no_update BEFORE UPDATE ON population_cells
BEGIN SELECT RAISE(ABORT,'population cells are frozen'); END;
CREATE TRIGGER population_no_delete BEFORE DELETE ON population_cells
BEGIN SELECT RAISE(ABORT,'population cells are frozen'); END;
CREATE TRIGGER population_insert_after_freeze BEFORE INSERT ON population_cells
WHEN (SELECT status FROM runs WHERE run_id=NEW.run_id) <> 'building'
BEGIN SELECT RAISE(ABORT,'population cells are frozen'); END;
CREATE TRIGGER carry_gold_no_update BEFORE UPDATE ON carry_forward_gold
BEGIN SELECT RAISE(ABORT,'carry-forward gold is frozen'); END;
CREATE TRIGGER carry_gold_no_delete BEFORE DELETE ON carry_forward_gold
BEGIN SELECT RAISE(ABORT,'carry-forward gold is frozen'); END;
CREATE TRIGGER carry_gold_insert_after_freeze BEFORE INSERT ON carry_forward_gold
WHEN (SELECT status FROM runs WHERE run_id=NEW.run_id) <> 'building'
BEGIN SELECT RAISE(ABORT,'carry-forward gold is frozen'); END;
CREATE TRIGGER review_items_no_update BEFORE UPDATE ON review_items
BEGIN SELECT RAISE(ABORT,'review items are frozen'); END;
CREATE TRIGGER review_items_no_delete BEFORE DELETE ON review_items
BEGIN SELECT RAISE(ABORT,'review items are frozen'); END;
CREATE TRIGGER review_items_insert_after_ready BEFORE INSERT ON review_items
WHEN (SELECT status FROM runs WHERE run_id=NEW.run_id) <> 'fetching'
BEGIN SELECT RAISE(ABORT,'review items are frozen'); END;
CREATE TRIGGER sample_assets_change_after_ready BEFORE UPDATE ON sample_assets
WHEN (SELECT status FROM runs WHERE run_id=NEW.run_id) <> 'fetching'
BEGIN SELECT RAISE(ABORT,'sample assets are frozen'); END;
CREATE TRIGGER sample_assets_delete_after_ready BEFORE DELETE ON sample_assets
WHEN (SELECT status FROM runs WHERE run_id=OLD.run_id) <> 'fetching'
BEGIN SELECT RAISE(ABORT,'sample assets are frozen'); END;
CREATE TRIGGER sample_assets_insert_after_ready BEFORE INSERT ON sample_assets
WHEN (SELECT status FROM runs WHERE run_id=NEW.run_id) <> 'fetching'
BEGIN SELECT RAISE(ABORT,'sample assets are frozen'); END;
CREATE TRIGGER fetch_assets_change_after_ready BEFORE UPDATE ON fetch_assets
WHEN (SELECT status FROM runs WHERE run_id=NEW.run_id) <> 'fetching'
BEGIN SELECT RAISE(ABORT,'fetch assets are frozen'); END;
CREATE TRIGGER fetch_assets_delete_after_ready BEFORE DELETE ON fetch_assets
WHEN (SELECT status FROM runs WHERE run_id=OLD.run_id) <> 'fetching'
BEGIN SELECT RAISE(ABORT,'fetch assets are frozen'); END;
CREATE TRIGGER fetch_assets_insert_after_ready BEFORE INSERT ON fetch_assets
WHEN (SELECT status FROM runs WHERE run_id=NEW.run_id) <> 'fetching'
BEGIN SELECT RAISE(ABORT,'fetch assets are frozen'); END;
CREATE TRIGGER blind_reviews_change_after_lock BEFORE UPDATE ON blind_reviews
WHEN EXISTS(SELECT 1 FROM review_locks WHERE run_id=OLD.run_id AND lane=OLD.lane)
BEGIN SELECT RAISE(ABORT,'blind-review lane is locked'); END;
CREATE TRIGGER blind_reviews_delete_after_lock BEFORE DELETE ON blind_reviews
WHEN EXISTS(SELECT 1 FROM review_locks WHERE run_id=OLD.run_id AND lane=OLD.lane)
BEGIN SELECT RAISE(ABORT,'blind-review lane is locked'); END;
CREATE TRIGGER blind_reviews_insert_after_lock BEFORE INSERT ON blind_reviews
WHEN EXISTS(SELECT 1 FROM review_locks WHERE run_id=NEW.run_id AND lane=NEW.lane)
BEGIN SELECT RAISE(ABORT,'blind-review lane is locked'); END;
CREATE TRIGGER adjudications_no_update BEFORE UPDATE ON adjudications
BEGIN SELECT RAISE(ABORT,'adjudications are frozen'); END;
CREATE TRIGGER adjudications_no_delete BEFORE DELETE ON adjudications
BEGIN SELECT RAISE(ABORT,'adjudications are frozen'); END;
CREATE TRIGGER state_events_no_update BEFORE UPDATE ON state_events
BEGIN SELECT RAISE(ABORT,'state events are append-only'); END;
CREATE TRIGGER state_events_no_delete BEFORE DELETE ON state_events
BEGIN SELECT RAISE(ABORT,'state events are append-only'); END;
"""


class IdentityN100Error(RuntimeError):
    """Raised when an N100 contract or no-clobber invariant fails."""


@dataclass(frozen=True)
class BuildResult:
    output_path: Path
    manifest_path: Path
    run_id: str
    sample_count: int
    logical_sha256: str


@dataclass(frozen=True)
class PrepareResult:
    artifact_path: Path
    run_id: str
    asset_occurrence_count: int
    unique_asset_count: int
    logical_http_fetches: int
    wire_http_requests: int
    cache_hits: int
    sheet_count: int


@dataclass(frozen=True)
class FinalizeResult:
    artifact_path: Path
    run_id: str
    status: str
    false_merge_count: int
    hard_conflict_violation_count: int
    unreviewable_count: int
    precision: float
    cp95_low: float
    cp95_high: float


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(16 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _record(payload: Mapping[str, Any]) -> str:
    return canonical_sha256(dict(payload))


def _path(root: Path, value: Path) -> Path:
    return value if value.is_absolute() else root / value


def _path_text(root: Path, value: Path) -> str:
    resolved = value.resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def _resolve_text(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def _assert_no_sidecars(path: Path) -> None:
    sidecars = [Path(str(path) + suffix) for suffix in ("-wal", "-shm", "-journal")]
    present = [str(item) for item in sidecars if item.exists()]
    if present:
        raise IdentityN100Error("forbidden SQLite sidecar(s): " + ", ".join(present))


def _open_immutable(path: Path) -> sqlite3.Connection:
    _assert_no_sidecars(path)
    connection = sqlite3.connect(
        "file:" + path.resolve().as_posix() + "?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _atomic_bytes(path: Path, raw: bytes, *, allow_identical: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if allow_identical and path.is_file() and path.read_bytes() == raw:
            return
        raise IdentityN100Error(f"refusing to clobber artifact: {path}")
    temporary = path.with_name(path.name + ".building")
    if temporary.exists():
        raise IdentityN100Error(f"stale temporary artifact: {temporary}")
    try:
        with temporary.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    _atomic_bytes(path, raw)


def cache_tree(root: Path) -> tuple[int, int, str]:
    rows: list[dict[str, Any]] = []
    total = 0
    if root.exists() and not root.is_dir():
        raise IdentityN100Error(f"cache root is not a directory: {root}")
    if root.exists():
        paths = sorted(
            (path for path in root.rglob("*") if path.is_file()),
            key=lambda path: path.relative_to(root).as_posix(),
        )
        for path in paths:
            relative = path.relative_to(root).as_posix()
            size = path.stat().st_size
            rows.append({"path": relative, "sha256": sha256_file(path), "size": size})
            total += size
    tree = hashlib.sha256(canonical_json(rows).encode("utf-8")).hexdigest()
    return len(rows), total, tree


def _pin_file(root: Path, path: Path, usage: str) -> dict[str, Any]:
    absolute = _path(root, path)
    if not absolute.is_file():
        raise IdentityN100Error(f"pinned input is missing: {absolute}")
    return {
        "path": _path_text(root, absolute),
        "size_bytes": absolute.stat().st_size,
        "sha256_before": sha256_file(absolute),
        "sha256_after": None,
        "usage": usage,
    }


def _seed_rank(seed: str, domain: str, candidate_id: str) -> str:
    raw = f"{SAMPLE_VERSION}\0{seed}\0{domain}\0{candidate_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def hamilton_apportion(populations: Mapping[str, int], seats: int) -> dict[str, int]:
    """Hamilton/largest-remainder apportionment with lexical tie-breaking."""

    if seats < 0 or not populations or any(value < 0 for value in populations.values()):
        raise IdentityN100Error("invalid Hamilton inputs")
    total = sum(populations.values())
    if total < seats or total == 0:
        raise IdentityN100Error("Hamilton population cannot fill requested seats")
    quotas = {key: seats * value // total for key, value in populations.items()}
    remaining = seats - sum(quotas.values())
    ranked = sorted(
        populations,
        key=lambda key: (-(seats * populations[key] % total), key),
    )
    for key in ranked[:remaining]:
        quotas[key] += 1
    return dict(sorted(quotas.items()))


def hierarchical_hamilton(
    populations: Mapping[tuple[str, str], int], seats: int
) -> dict[tuple[str, str], int]:
    anchor_populations: dict[str, int] = defaultdict(int)
    for (anchor, _mask), count in populations.items():
        anchor_populations[anchor] += count
    anchor_seats = hamilton_apportion(anchor_populations, seats)
    result: dict[tuple[str, str], int] = {}
    for anchor in sorted(anchor_populations):
        children = {
            mask: count
            for (cell_anchor, mask), count in populations.items()
            if cell_anchor == anchor
        }
        child_seats = hamilton_apportion(children, anchor_seats[anchor])
        for mask, quota in child_seats.items():
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
            "SELECT candidate_id,opaque_review_id FROM sample_neighbourhoods ORDER BY candidate_id"
        )
    )
    return rows


def _conflict_sets(connection: sqlite3.Connection) -> dict[str, frozenset[str]]:
    values: dict[str, set[str]] = defaultdict(set)
    for row in connection.execute("SELECT candidate_id,conflict_type FROM hard_conflicts"):
        values[str(row[0])].add(str(row[1]))
    return {key: frozenset(value) for key, value in values.items()}


def _fanout_flags(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT candidate_id FROM candidate_policy_flags WHERE flag_type='fanout_conflict'"
        )
    }


def _anchor_and_mask(row: Mapping[str, Any]) -> tuple[str, str]:
    name = bool(row["name_pos"])
    image = bool(row["image_pos"])
    if not (name or image):
        raise IdentityN100Error("eligible candidate has no anchor")
    anchor = "both" if name and image else ("name" if name else "image")
    auxiliary = []
    for column, label in (
        ("firm_pos", "architect_firm_identity"),
        ("location_pos", "location_site_identity"),
        ("stable_pos", "source_url_external_stable_id"),
        ("year_pos", "year_phase_compatibility"),
    ):
        if row[column]:
            auxiliary.append(label)
    return anchor, "+".join(sorted(auxiliary)) or "none"


def _b_pool(
    stratum: str,
    rows: Sequence[Mapping[str, Any]],
    conflicts: Mapping[str, frozenset[str]],
    fanout_flags: set[str],
    b1_ids: set[str] | None = None,
    b2_ids: set[str] | None = None,
    b3_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        candidate_id = str(row["candidate_id"])
        conflict_set = conflicts.get(candidate_id, frozenset())
        anchor = bool(row["name_pos"] or row["image_pos"])
        if stratum == "B1_year_gate":
            include = (
                row["proposal"] == "reject"
                and int(row["positive_family_count"]) >= 2
                and anchor
                and conflict_set == {"matcher_architect_year_gate_block"}
            )
        elif stratum == "B2_phash_veto":
            include = (
                row["proposal"] == "reject"
                and int(row["positive_family_count"]) >= 2
                and bool(row["name_pos"])
                and float(row["name_strict_score"]) >= 90.0
                and conflict_set == {"verified_phash_veto"}
            )
        elif stratum == "B3_fanout_defer":
            include = (
                row["proposal"] == "defer"
                and candidate_id in fanout_flags
                and conflict_set == {"fanout_conflict"}
            )
        elif stratum == "B4_high_name_reject":
            excluded = (b1_ids or set()) | (b2_ids or set()) | (b3_ids or set())
            include = (
                row["proposal"] == "reject"
                and float(row["name_strict_score"]) >= 90.0
                and candidate_id not in excluded
            )
        else:
            raise IdentityN100Error(f"unknown diagnostic stratum: {stratum}")
        if include:
            selected.append(row)
    return selected


def _logical_sha(connection: sqlite3.Connection, run_id: str) -> str:
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
        sql = f"SELECT record_sha256 FROM {table} WHERE run_id=?"
        parameters: tuple[Any, ...] = (run_id,)
        if table == "input_pins":
            sql += " AND role IN ('v3_candidates_final','n100_protocol','wp23_order')"
        sql += f" ORDER BY {order}"
        for row in connection.execute(sql, parameters):
            digest.update((str(row[0]) + "\n").encode("ascii"))
    return digest.hexdigest()


def _refresh_run_record(connection: sqlite3.Connection, run_id: str) -> None:
    """Keep runs.record_sha256 equal to the current row, excluding itself."""

    row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise IdentityN100Error("run row is missing")
    payload = {key: row[key] for key in row.keys() if key != "record_sha256"}
    connection.execute(
        "UPDATE runs SET record_sha256=? WHERE run_id=?", (_record(payload), run_id)
    )


def _carry_gold(connection: sqlite3.Connection, candidate_id: str) -> dict[str, str]:
    row = connection.execute(
        """
        SELECT s.run_id,s.opaque_review_id,b.reviewer,b.rationale reviewer_rationale,
               b.record_sha256 blind_record_sha256,
               a.adjudicator,a.final_gold_label,a.mapped_decision,
               a.rationale adjudicator_rationale,
               a.record_sha256 adjudication_record_sha256
        FROM sample_neighbourhoods s
        JOIN blind_reviews b USING(run_id,opaque_review_id)
        JOIN adjudications a USING(run_id,opaque_review_id)
        WHERE s.candidate_id=?
        """,
        (candidate_id,),
    ).fetchone()
    if row is None:
        raise IdentityN100Error(f"carry-forward gold is missing: {candidate_id}")
    return {key: str(row[key]) for key in row.keys()}


def _insert_event(
    connection: sqlite3.Connection,
    run_id: str,
    from_status: str,
    to_status: str,
    detail: Mapping[str, Any],
) -> None:
    event_no = int(
        connection.execute(
            "SELECT coalesce(max(event_no),0)+1 FROM state_events WHERE run_id=?", (run_id,)
        ).fetchone()[0]
    )
    occurred_at = utc_now()
    payload = {
        "event_no": event_no,
        "from_status": from_status,
        "to_status": to_status,
        "detail": dict(detail),
        "occurred_at": occurred_at,
    }
    connection.execute(
        "INSERT INTO state_events VALUES(?,?,?,?,?,?,?)",
        (
            run_id,
            event_no,
            from_status,
            to_status,
            canonical_json(dict(detail)),
            occurred_at,
            _record(payload),
        ),
    )


def _metadata_row(
    connection: sqlite3.Connection, source: str, source_building_id: str
) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM source_buildings WHERE source=? AND source_building_id=?",
        (source, source_building_id),
    ).fetchone()
    if row is None:
        raise IdentityN100Error(f"missing source building: {source}/{source_building_id}")
    return dict(row)


def build_sample(
    *,
    repo_root: Path,
    v3_path: Path = DEFAULT_V3,
    output_path: Path = DEFAULT_OUTPUT,
    manifest_path: Path = DEFAULT_SAMPLE_MANIFEST,
    protocol_path: Path = DEFAULT_PROTOCOL,
    order_path: Path = DEFAULT_ORDER,
    seed: str = SEED,
    verify_v3_pin: bool = True,
) -> BuildResult:
    """Freeze A70+B40 without touching v3 or using network."""

    root = repo_root.resolve()
    v3 = _path(root, v3_path)
    output = _path(root, output_path)
    manifest = _path(root, manifest_path)
    protocol = _path(root, protocol_path)
    order = _path(root, order_path)
    if output.exists() or manifest.exists():
        raise IdentityN100Error("refusing to clobber N100 sample artifact")
    if not v3.is_file():
        raise IdentityN100Error("v3 input is missing")
    _assert_no_sidecars(v3)
    v3_size = v3.stat().st_size
    v3_sha = sha256_file(v3)
    if verify_v3_pin and (v3_size, v3_sha) != V3_FINAL_SPEC:
        raise IdentityN100Error("v3 final byte pin mismatch")

    pins = {
        "v3_candidates_final": _pin_file(root, v3, "immutable_candidate_population"),
        "n100_protocol": _pin_file(root, protocol, "review_protocol"),
        "wp23_order": _pin_file(root, order, "job_order"),
    }
    run_id = "wp23-" + hashlib.sha256(
        f"{SAMPLE_VERSION}\0{seed}\0{v3_sha}".encode("utf-8")
    ).hexdigest()[:24]
    v3_db = _open_immutable(v3)
    temporary = output.with_name(output.name + ".building")
    failed = output.with_name(output.name + ".building.failed")
    if temporary.exists() or failed.exists():
        v3_db.close()
        raise IdentityN100Error("stale N100 build artifact exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    database: sqlite3.Connection | None = None
    try:
        run_rows = v3_db.execute("SELECT run_id,status FROM runs").fetchall()
        if len(run_rows) != 1 or str(run_rows[0]["status"]) != "pass":
            raise IdentityN100Error("v3 is not the final passed artifact")
        candidates = _candidate_view(v3_db)
        by_id = {str(row["candidate_id"]): row for row in candidates}
        conflicts = _conflict_sets(v3_db)
        fanout_flags = _fanout_flags(v3_db)
        reviewed_rows = _reviewed_rows(v3_db)
        reviewed = {row["candidate_id"] for row in reviewed_rows}
        if len(reviewed) != 30:
            raise IdentityN100Error(f"expected 30 prior focal reviews, found {len(reviewed)}")
        carry_ids = {candidate_id for values in CARRY_FORWARD.values() for candidate_id in values}
        if not carry_ids <= reviewed:
            raise IdentityN100Error("carry-forward pair is not in prior-review ledger")

        core_rows = [
            row
            for row in candidates
            if row["proposal"] == "merge_candidate"
            and int(row["auto_merge_eligible"]) == 1
            and str(row["candidate_id"]) not in reviewed
        ]
        cells: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in core_rows:
            cells[_anchor_and_mask(row)].append(row)
        populations = {key: len(value) for key, value in cells.items()}
        allocations = hierarchical_hamilton(populations, CORE_COUNT)
        selected_specs: list[dict[str, Any]] = []
        for cell in sorted(cells):
            anchor, mask = cell
            ranked = sorted(
                cells[cell],
                key=lambda row: (
                    _seed_rank(seed, f"A/{anchor}/{mask}", str(row["candidate_id"])),
                    str(row["candidate_id"]),
                ),
            )
            for row in ranked[: allocations[cell]]:
                candidate_id = str(row["candidate_id"])
                selected_specs.append(
                    {
                        "cohort": "A",
                        "stratum": "A_precision_core",
                        "origin": "fresh",
                        "candidate_id": candidate_id,
                        "anchor": anchor,
                        "mask": mask,
                        "seed_rank": _seed_rank(seed, f"A/{anchor}/{mask}", candidate_id),
                        "gate_scope": 1,
                    }
                )

        b1 = _b_pool("B1_year_gate", candidates, conflicts, fanout_flags)
        b2 = _b_pool("B2_phash_veto", candidates, conflicts, fanout_flags)
        b3 = _b_pool("B3_fanout_defer", candidates, conflicts, fanout_flags)
        b1_ids = {str(row["candidate_id"]) for row in b1}
        b2_ids = {str(row["candidate_id"]) for row in b2}
        b3_ids = {str(row["candidate_id"]) for row in b3}
        b4 = _b_pool(
            "B4_high_name_reject",
            candidates,
            conflicts,
            fanout_flags,
            b1_ids,
            b2_ids,
            b3_ids,
        )
        pools = {
            "B1_year_gate": b1,
            "B2_phash_veto": b2,
            "B3_fanout_defer": b3,
            "B4_high_name_reject": b4,
        }
        already_selected = {str(row["candidate_id"]) for row in selected_specs}
        for stratum in ("B1_year_gate", "B2_phash_veto", "B3_fanout_defer", "B4_high_name_reject"):
            pool_ids = {str(row["candidate_id"]) for row in pools[stratum]}
            required = set(CARRY_FORWARD[stratum])
            if not required <= pool_ids:
                raise IdentityN100Error(f"carry-forward pair fails {stratum} predicate")
            for candidate_id in CARRY_FORWARD[stratum]:
                selected_specs.append(
                    {
                        "cohort": "B",
                        "stratum": stratum,
                        "origin": "carry_forward",
                        "candidate_id": candidate_id,
                        "anchor": "",
                        "mask": "",
                        "seed_rank": _seed_rank(seed, f"{stratum}/carry", candidate_id),
                        "gate_scope": 0,
                    }
                )
                already_selected.add(candidate_id)
            fresh = [
                row
                for row in pools[stratum]
                if str(row["candidate_id"]) not in reviewed
                and str(row["candidate_id"]) not in already_selected
            ]
            fresh.sort(
                key=lambda row: (
                    _seed_rank(seed, f"{stratum}/fresh", str(row["candidate_id"])),
                    str(row["candidate_id"]),
                )
            )
            quota = FRESH_QUOTAS[stratum]
            if len(fresh) < quota:
                raise IdentityN100Error(f"insufficient fresh pool for {stratum}")
            for row in fresh[:quota]:
                candidate_id = str(row["candidate_id"])
                selected_specs.append(
                    {
                        "cohort": "B",
                        "stratum": stratum,
                        "origin": "fresh",
                        "candidate_id": candidate_id,
                        "anchor": "",
                        "mask": "",
                        "seed_rank": _seed_rank(seed, f"{stratum}/fresh", candidate_id),
                        "gate_scope": 0,
                    }
                )
                already_selected.add(candidate_id)

        selected_specs.sort(
            key=lambda row: (
                0 if row["cohort"] == "A" else 1,
                row["stratum"],
                row["anchor"],
                row["mask"],
                row["seed_rank"],
                row["candidate_id"],
            )
        )
        if len(selected_specs) != TOTAL_COUNT or len(already_selected) != TOTAL_COUNT:
            raise IdentityN100Error("N100 sample accounting mismatch")
        if sum(row["cohort"] == "A" for row in selected_specs) != CORE_COUNT:
            raise IdentityN100Error("core A70 accounting mismatch")
        if sum(row["origin"] == "carry_forward" for row in selected_specs) != 5:
            raise IdentityN100Error("carry-forward accounting mismatch")

        database = sqlite3.connect(temporary)
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA journal_mode=DELETE")
        database.execute("PRAGMA synchronous=FULL")
        database.execute("PRAGMA foreign_keys=ON")
        database.execute(f"PRAGMA application_id={APPLICATION_ID}")
        database.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        database.executescript(SCHEMA_SQL)
        created_at = utc_now()
        run_payload = {
            "run_id": run_id,
            "schema_version": SCHEMA_VERSION,
            "runner_version": RUNNER_VERSION,
            "sample_version": SAMPLE_VERSION,
            "review_version": REVIEW_VERSION,
            "adjudication_version": ADJUDICATION_VERSION,
            "gate_version": GATE_VERSION,
            "seed": seed,
            "status": "building",
            "created_at": created_at,
        }
        database.execute(
            "INSERT INTO runs(run_id,schema_version,runner_version,sample_version,review_version,"
            "adjudication_version,gate_version,seed,status,created_at,record_sha256) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                SCHEMA_VERSION,
                RUNNER_VERSION,
                SAMPLE_VERSION,
                REVIEW_VERSION,
                ADJUDICATION_VERSION,
                GATE_VERSION,
                seed,
                "building",
                created_at,
                _record(run_payload),
            ),
        )
        for role, pin in sorted(pins.items()):
            payload = {"role": role, **pin}
            database.execute(
                "INSERT INTO input_pins VALUES(?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    role,
                    pin["path"],
                    pin["size_bytes"],
                    pin["sha256_before"],
                    None,
                    pin["usage"],
                    _record(payload),
                ),
            )
        for row in reviewed_rows:
            payload = {
                **row,
                "carry_forward": int(row["candidate_id"] in carry_ids),
            }
            database.execute(
                "INSERT INTO review_exclusions VALUES(?,?,?,?,?,?)",
                (
                    run_id,
                    row["candidate_id"],
                    row["prior_version"],
                    row["opaque_review_id"],
                    payload["carry_forward"],
                    _record(payload),
                ),
            )
        for (anchor, mask), count in sorted(populations.items()):
            payload = {
                "cohort": "A",
                "stratum": "A_precision_core",
                "anchor_type": anchor,
                "auxiliary_mask": mask,
                "population_count": count,
                "fresh_population_count": count,
                "quota": allocations[(anchor, mask)],
                "predicate_version": "v3-eligible-hierarchical-hamilton-v1",
            }
            database.execute(
                "INSERT INTO population_cells VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    "A",
                    "A_precision_core",
                    anchor,
                    mask,
                    count,
                    count,
                    allocations[(anchor, mask)],
                    payload["predicate_version"],
                    _record(payload),
                ),
            )
        for stratum, pool in pools.items():
            fresh_count = sum(str(row["candidate_id"]) not in reviewed for row in pool)
            quota = len(CARRY_FORWARD[stratum]) + FRESH_QUOTAS[stratum]
            payload = {
                "cohort": "B",
                "stratum": stratum,
                "anchor_type": "",
                "auxiliary_mask": "",
                "population_count": len(pool),
                "fresh_population_count": fresh_count,
                "quota": quota,
                "predicate_version": "wp23-clean-mechanism-predicates-v1",
            }
            database.execute(
                "INSERT INTO population_cells VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    "B",
                    stratum,
                    "",
                    "",
                    len(pool),
                    fresh_count,
                    quota,
                    payload["predicate_version"],
                    _record(payload),
                ),
            )
        for rank, spec in enumerate(selected_specs, 1):
            candidate_id = spec["candidate_id"]
            row = by_id[candidate_id]
            sample_id = "n100_" + hashlib.sha256(
                f"{run_id}\0{candidate_id}".encode("utf-8")
            ).hexdigest()[:24]
            sample_payload = {
                "sample_rank": rank,
                "sample_id": sample_id,
                "cohort": spec["cohort"],
                "stratum": spec["stratum"],
                "sample_origin": spec["origin"],
                "candidate_id": candidate_id,
                "anchor_type": spec["anchor"],
                "auxiliary_mask": spec["mask"],
                "seed_rank_sha256": spec["seed_rank"],
                "gate_scope": spec["gate_scope"],
            }
            database.execute(
                "INSERT INTO sample_pairs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    rank,
                    sample_id,
                    spec["cohort"],
                    spec["stratum"],
                    spec["origin"],
                    candidate_id,
                    spec["anchor"],
                    spec["mask"],
                    spec["seed_rank"],
                    spec["gate_scope"],
                    _record(sample_payload),
                ),
            )
            pair = v3_db.execute(
                "SELECT * FROM candidate_pairs WHERE candidate_id=?", (candidate_id,)
            ).fetchone()
            if pair is None:
                raise IdentityN100Error("selected candidate disappeared")
            pair_dict = dict(pair)
            left = _metadata_row(
                v3_db, str(pair["left_source"]), str(pair["left_source_building_id"])
            )
            right = _metadata_row(
                v3_db, str(pair["right_source"]), str(pair["right_source_building_id"])
            )
            origins = [
                str(item[0])
                for item in v3_db.execute(
                    "SELECT origin FROM candidate_origins WHERE candidate_id=? ORDER BY origin",
                    (candidate_id,),
                )
            ]
            families = [
                dict(item)
                for item in v3_db.execute(
                    "SELECT family,status,score,evidence_json,record_sha256 "
                    "FROM evidence_families WHERE candidate_id=? ORDER BY family",
                    (candidate_id,),
                )
            ]
            conflict_rows = [
                dict(item)
                for item in v3_db.execute(
                    "SELECT conflict_type,detail_json,record_sha256 FROM hard_conflicts "
                    "WHERE candidate_id=? ORDER BY conflict_type",
                    (candidate_id,),
                )
            ]
            flags = [
                dict(item)
                for item in v3_db.execute(
                    "SELECT flag_type,detail_json,record_sha256 FROM candidate_policy_flags "
                    "WHERE candidate_id=? ORDER BY flag_type",
                    (candidate_id,),
                )
            ]
            snapshot = {
                "candidate": pair_dict,
                "left": left,
                "right": right,
                "origins": origins,
                "families": families,
                "conflicts": conflict_rows,
                "policy_flags": flags,
            }
            snapshot_sha = canonical_sha256(snapshot)
            snapshot_payload = {"sample_id": sample_id, "snapshot_sha256": snapshot_sha}
            database.execute(
                "INSERT INTO candidate_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    sample_id,
                    canonical_json(pair_dict),
                    canonical_json(left),
                    canonical_json(right),
                    canonical_json(origins),
                    canonical_json(families),
                    canonical_json(conflict_rows),
                    canonical_json(flags),
                    snapshot_sha,
                    _record(snapshot_payload),
                ),
            )
            if spec["origin"] == "carry_forward":
                gold = _carry_gold(v3_db, candidate_id)
                carry_payload = {
                    "sample_id": sample_id,
                    "candidate_id": candidate_id,
                    "prior_run_id": gold["run_id"],
                    "prior_opaque_review_id": gold["opaque_review_id"],
                    "reviewer": gold["reviewer"],
                    "adjudicator": gold["adjudicator"],
                    "final_gold_label": gold["final_gold_label"],
                    "mapped_decision": gold["mapped_decision"],
                    "blind_review_record_sha256": gold["blind_record_sha256"],
                    "reviewer_rationale": gold["reviewer_rationale"],
                    "adjudicator_rationale": gold["adjudicator_rationale"],
                    "adjudication_record_sha256": gold["adjudication_record_sha256"],
                    "source_db_sha256": v3_sha,
                }
                database.execute(
                    "INSERT INTO carry_forward_gold VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        sample_id,
                        candidate_id,
                        gold["run_id"],
                        gold["opaque_review_id"],
                        gold["reviewer"],
                        gold["adjudicator"],
                        gold["final_gold_label"],
                        gold["mapped_decision"],
                        gold["blind_record_sha256"],
                        gold["reviewer_rationale"],
                        gold["adjudicator_rationale"],
                        gold["adjudication_record_sha256"],
                        v3_sha,
                        _record(carry_payload),
                    ),
                )
        for role, pin in sorted(pins.items()):
            current = sha256_file(_resolve_text(root, pin["path"]))
            if current != pin["sha256_before"]:
                raise IdentityN100Error(f"input drift during sample freeze: {role}")
            pin["sha256_after"] = current
            payload = {"role": role, **pin, "sha256_after": current}
            database.execute(
                "UPDATE input_pins SET sha256_after=?,record_sha256=? WHERE run_id=? AND role=?",
                (current, _record(payload), run_id, role),
            )
        logical = _logical_sha(database, run_id)
        database.execute(
            "UPDATE runs SET status='sample_frozen',sample_count=?,core_count=?,"
            "diagnostic_count=?,sample_logical_sha256=? WHERE run_id=?",
            (TOTAL_COUNT, CORE_COUNT, DIAGNOSTIC_COUNT, logical, run_id),
        )
        _refresh_run_record(database, run_id)
        _insert_event(
            database,
            run_id,
            "building",
            "sample_frozen",
            {"sample_count": TOTAL_COUNT, "logical_sha256": logical},
        )
        database.commit()
        database.execute("PRAGMA optimize")
        database.close()
        database = None
        v3_db.close()
        os.replace(temporary, output)
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
            for row in selected_specs_from_db(output)
        ]
        endpoint_counts: Counter[tuple[str, str]] = Counter()
        for sample in sample_rows:
            candidate = by_id[sample["candidate_id"]]
            endpoint_counts[(str(candidate["left_source"]), str(candidate["left_source_building_id"]))] += 1
            endpoint_counts[(str(candidate["right_source"]), str(candidate["right_source_building_id"]))] += 1
        manifest_payload = {
            "status": "PASS",
            "run_id": run_id,
            "runner_version": RUNNER_VERSION,
            "sample_version": SAMPLE_VERSION,
            "seed": seed,
            "counts": {
                "total": TOTAL_COUNT,
                "core_a": CORE_COUNT,
                "diagnostic_b": DIAGNOSTIC_COUNT,
                "carry_forward": 5,
                "fresh_diagnostic": 35,
                "fresh_review_rows_per_lane": FRESH_REVIEW_COUNT,
                "prior_review_exclusions": 30,
                "core_population_after_exclusion": len(core_rows),
            },
            "hierarchical_hamilton_allocations": [
                {"anchor_type": key[0], "auxiliary_mask": key[1], "quota": value}
                for key, value in sorted(allocations.items())
            ],
            "diagnostic_populations": {
                key: {
                    "full": len(value),
                    "fresh": sum(str(row["candidate_id"]) not in reviewed for row in value),
                    "carry_forward": len(CARRY_FORWARD[key]),
                    "fresh_quota": FRESH_QUOTAS[key],
                }
                for key, value in sorted(pools.items())
            },
            "samples": sample_rows,
            "endpoint_reuse_diagnostic": {
                "distinct_endpoints": len(endpoint_counts),
                "reused_endpoint_count": sum(value > 1 for value in endpoint_counts.values()),
                "maximum_candidate_occurrences": max(endpoint_counts.values(), default=0),
                "selection_constraint": False,
            },
            "sample_logical_sha256": logical,
            "sidecar_sample_bytes": output.stat().st_size,
            "sidecar_sample_sha256": sha256_file(output),
            "input_pins": pins,
            "network_requests": 0,
            "vision_requests": 0,
            "llm_requests": 0,
            "merge_executions": 0,
            "vnx_id_issuances": 0,
        }
        _atomic_json(manifest, manifest_payload)
        return BuildResult(output, manifest, run_id, TOTAL_COUNT, logical)
    except Exception:
        if database is not None:
            database.close()
        v3_db.close()
        if temporary.exists() and not failed.exists():
            os.replace(temporary, failed)
        raise


def selected_specs_from_db(path: Path) -> list[dict[str, Any]]:
    connection = _open_immutable(path)
    try:
        return [dict(row) for row in connection.execute("SELECT * FROM sample_pairs ORDER BY sample_rank")]
    finally:
        connection.close()


@dataclass(frozen=True)
class _FetchOutcome:
    asset_key: str
    status: str
    final_url: str | None
    raw_sha256: str | None
    pixel_sha256: str | None
    http_status: int | None
    response_bytes: int | None
    request_count: int
    redirect_chain: tuple[Mapping[str, Any], ...]
    error_kind: str | None
    error_message: str | None
    body: bytes | None
    started_at: str
    completed_at: str


def _cache_relative(pixel_sha256: str, transform_version: str) -> Path:
    transform_key = hashlib.sha256(transform_version.encode("utf-8")).hexdigest()[:12]
    return Path(pixel_sha256[:2]) / f"{pixel_sha256}.{transform_key}.jpg"


def _path_overlaps(left: Path, right: Path) -> bool:
    left_resolved, right_resolved = left.resolve(), right.resolve()
    try:
        left_resolved.relative_to(right_resolved)
        return True
    except ValueError:
        pass
    try:
        right_resolved.relative_to(left_resolved)
        return True
    except ValueError:
        return False


def _pin_current_file(
    database: sqlite3.Connection,
    root: Path,
    run_id: str,
    role: str,
    path: Path,
    usage: str,
    expected: tuple[int, str] | None,
) -> dict[str, Any]:
    absolute = _path(root, path)
    if not absolute.is_file():
        raise IdentityN100Error(f"missing pinned input: {absolute}")
    _assert_no_sidecars(absolute)
    size, digest = absolute.stat().st_size, sha256_file(absolute)
    if expected is not None and (size, digest) != expected:
        raise IdentityN100Error(f"input pin mismatch: {role}")
    payload = {
        "role": role,
        "path": _path_text(root, absolute),
        "size_bytes": size,
        "sha256_before": digest,
        "sha256_after": digest,
        "usage": usage,
    }
    database.execute(
        "INSERT INTO input_pins VALUES(?,?,?,?,?,?,?,?)",
        (
            run_id,
            role,
            payload["path"],
            size,
            digest,
            digest,
            usage,
            _record(payload),
        ),
    )
    return payload


def _verify_sample_inputs(database: sqlite3.Connection, root: Path, run_id: str) -> None:
    for row in database.execute(
        "SELECT role,path,size_bytes,sha256_before,sha256_after FROM input_pins "
        "WHERE run_id=? ORDER BY role",
        (run_id,),
    ):
        path = _resolve_text(root, str(row["path"]))
        if not path.is_file() or path.stat().st_size != int(row["size_bytes"]):
            raise IdentityN100Error(f"sample input size drift: {row['role']}")
        current = sha256_file(path)
        if current != str(row["sha256_before"]) or (
            row["sha256_after"] is not None and current != str(row["sha256_after"])
        ):
            raise IdentityN100Error(f"sample input hash drift: {row['role']}")


def _pin_read_caches(
    database: sqlite3.Connection,
    root: Path,
    run_id: str,
    read_caches: Sequence[CacheSpec],
    output_cache: Path,
    *,
    verify_expected: bool,
) -> list[dict[str, Any]]:
    pins: list[dict[str, Any]] = []
    for spec in read_caches:
        absolute = _path(root, spec.path)
        if _path_overlaps(absolute, output_cache):
            raise IdentityN100Error("read-only and write-only cache roots overlap")
        count, total, tree = cache_tree(absolute)
        if verify_expected and (count, total, tree) != (
            spec.file_count,
            spec.total_bytes,
            spec.tree_sha256,
        ):
            raise IdentityN100Error(f"read-cache pin mismatch: {spec.role}")
        payload = {
            "role": spec.role,
            "path": _path_text(root, absolute),
            "transform_version": spec.transform_version,
            "access_mode": "read_only",
            "file_count_before": count,
            "total_bytes_before": total,
            "tree_sha256_before": tree,
            "file_count_after": count,
            "total_bytes_after": total,
            "tree_sha256_after": tree,
        }
        database.execute(
            "INSERT INTO cache_pins VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                spec.role,
                payload["path"],
                spec.transform_version,
                "read_only",
                count,
                total,
                tree,
                count,
                total,
                tree,
                _record(payload),
            ),
        )
        pins.append(payload)
    empty_tree = cache_tree(output_cache)
    if output_cache.exists():
        raise IdentityN100Error("write-only N100 cache already exists")
    output_payload = {
        "role": "wp23_n100_output",
        "path": _path_text(root, output_cache),
        "transform_version": n10_review.TRANSFORM_VERSION,
        "access_mode": "write_only",
        "file_count_before": empty_tree[0],
        "total_bytes_before": empty_tree[1],
        "tree_sha256_before": empty_tree[2],
        "file_count_after": None,
        "total_bytes_after": None,
        "tree_sha256_after": None,
    }
    database.execute(
        "INSERT INTO cache_pins VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            "wp23_n100_output",
            output_payload["path"],
            n10_review.TRANSFORM_VERSION,
            "write_only",
            *empty_tree,
            None,
            None,
            None,
            _record(output_payload),
        ),
    )
    pins.append(output_payload)
    return pins


def _choose_assets(
    v3: sqlite3.Connection,
    e2: sqlite3.Connection,
    candidate: Mapping[str, Any],
) -> dict[str, list[tuple[str, str]]]:
    candidate_id = str(candidate["candidate_id"])
    selected: dict[str, list[tuple[str, str]]] = {"left": [], "right": []}
    priorities = {"exact_pixel": 0, "identical_phash": 1, "phash_le8": 2, "phash_9_16": 3}
    direct = [
        dict(row)
        for row in v3.execute(
            "SELECT * FROM direct_image_evidence WHERE candidate_id=?", (candidate_id,)
        )
    ]
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
                asset_id not in {value for value, _reason in selected[side]}
                and len(selected[side]) < MAX_ASSETS_PER_SIDE
            ):
                selected[side].append((asset_id, f"direct_evidence_{index}"))
        if all(len(value) >= MAX_ASSETS_PER_SIDE for value in selected.values()):
            break
    for side in ("left", "right"):
        if len(selected[side]) >= MAX_ASSETS_PER_SIDE:
            continue
        source = str(candidate[f"{side}_source"])
        building_id = str(candidate[f"{side}_source_building_id"])
        existing = {value for value, _reason in selected[side]}
        for asset_id in n10_review._top_building_assets(e2, source, building_id):
            if len(selected[side]) >= MAX_ASSETS_PER_SIDE:
                break
            if asset_id in existing:
                continue
            selected[side].append((asset_id, "building_representative"))
            existing.add(asset_id)
            if len(selected[side]) >= MAX_ASSETS_PER_SIDE:
                break
    return selected


def _cache_hit(
    root: Path,
    expected_pixel_sha256: str,
    read_caches: Sequence[CacheSpec],
) -> tuple[str, Path] | None:
    for spec in read_caches:
        candidate = _path(root, spec.path) / _cache_relative(
            expected_pixel_sha256, spec.transform_version
        )
        if candidate.is_file():
            return spec.role, candidate
    return None


def _redirect_json(hops: Iterable[Any]) -> tuple[Mapping[str, Any], ...]:
    values: list[Mapping[str, Any]] = []
    for hop in hops:
        if hasattr(hop, "__dict__"):
            values.append(dict(vars(hop)))
        else:
            values.append({"value": str(hop)})
    return tuple(values)


def _fetch_one(
    row: Mapping[str, Any],
    fetcher: Callable[..., FetchPayload],
) -> _FetchOutcome:
    started_at = utc_now()
    payload: FetchPayload | None = None
    try:
        payload = fetcher(str(row["source"]), str(row["expected_url"]), max_redirects=0)
        if int(payload.request_count) > 1 or payload.redirect_chain:
            raise IdentityN100Error("bounded fetcher followed a redirect")
        fingerprint = fingerprint_bytes(payload.body)
        if fingerprint.response_sha256 != payload.raw_response_sha256:
            raise IdentityN100Error("fetch/fingerprint raw SHA mismatch")
        if fingerprint.pixel_sha256 == row["expected_pixel_sha256"]:
            status = (
                "exact"
                if fingerprint.response_sha256 == row["expected_raw_sha256"]
                else "delivery_changed"
            )
        else:
            status = "source_changed"
        return _FetchOutcome(
            str(row["asset_key"]),
            status,
            payload.final_url,
            fingerprint.response_sha256,
            fingerprint.pixel_sha256,
            int(payload.http_status),
            len(payload.body),
            int(payload.request_count),
            _redirect_json(payload.redirect_chain),
            None,
            None,
            payload.body,
            started_at,
            utc_now(),
        )
    except FetchFailure as exc:
        return _FetchOutcome(
            str(row["asset_key"]),
            "failed",
            exc.final_url,
            exc.raw_response_sha256,
            None,
            exc.http_status,
            exc.response_bytes,
            int(exc.request_count),
            _redirect_json(exc.redirect_chain),
            str(exc.kind),
            str(exc)[:2000],
            None,
            started_at,
            utc_now(),
        )
    except (FingerprintError, ValueError, OSError, IdentityN100Error) as exc:
        return _FetchOutcome(
            str(row["asset_key"]),
            "failed",
            getattr(payload, "final_url", None),
            getattr(payload, "raw_response_sha256", None),
            None,
            getattr(payload, "http_status", None),
            len(payload.body) if payload is not None else None,
            int(payload.request_count) if payload is not None else 0,
            _redirect_json(payload.redirect_chain) if payload is not None else (),
            "local_validation_failed",
            str(exc)[:2000],
            None,
            started_at,
            utc_now(),
        )


def _materialize_derivative(output_cache: Path, pixel_sha256: str, body: bytes) -> Path:
    target = output_cache / _cache_relative(pixel_sha256, n10_review.TRANSFORM_VERSION)
    derivative = prepare_derivative(decode_source(body), "vnext_identity_review", 1024)
    _atomic_bytes(target, derivative.encoded_bytes, allow_identical=True)
    return target


def _metadata_for_sheet(raw: str) -> dict[str, Any]:
    value = json.loads(raw)
    aliases = value.get("aliases_json", value.get("aliases", []))
    firms = value.get("firms_json", value.get("firms", []))
    if isinstance(aliases, str):
        aliases = json.loads(aliases)
    if isinstance(firms, str):
        firms = json.loads(firms)
    return {
        "source": str(value.get("source", "unknown")),
        "name": str(value.get("name", "unknown")),
        "aliases": [str(item) for item in aliases or []],
        "firms": [str(item) for item in firms or []],
        "country": value.get("country"),
        "locality": value.get("locality"),
        "year_min": value.get("completion_year_min", value.get("year_min")),
        "year_max": value.get("completion_year_max", value.get("year_max")),
    }


def _render_blind_sheet(
    database: sqlite3.Connection,
    root: Path,
    run_id: str,
    sample_id: str,
    opaque_review_id: str,
    path: Path,
) -> tuple[str, int]:
    """Render only focal metadata/images; no neighbourhood or selection fields."""

    from PIL import Image, ImageDraw

    snapshot = database.execute(
        "SELECT left_metadata_json,right_metadata_json FROM candidate_snapshots "
        "WHERE run_id=? AND sample_id=?",
        (run_id, sample_id),
    ).fetchone()
    if snapshot is None:
        raise IdentityN100Error("review snapshot is missing")
    left = _metadata_for_sheet(str(snapshot["left_metadata_json"]))
    right = _metadata_for_sheet(str(snapshot["right_metadata_json"]))
    canvas = Image.new("RGB", (1800, 1100), (244, 246, 248))
    draw = ImageDraw.Draw(canvas)
    title_font = n10_review._font(34)
    heading_font = n10_review._font(25)
    body_font = n10_review._font(20)
    small_font = n10_review._font(16)
    draw.text(
        (40, 28),
        f"Blind building identity review - {opaque_review_id}",
        font=title_font,
        fill=(20, 25, 30),
    )
    draw.text(
        (40, 74),
        "Judge the focal pair only. Selection, score, proposal, and strata are hidden.",
        font=small_font,
        fill=(75, 80, 88),
    )
    for side, metadata, x in (("LEFT", left, 35), ("RIGHT", right, 915)):
        draw.rounded_rectangle(
            (x, 110, x + 850, 1040), radius=12, fill="white", outline=(195, 200, 207), width=2
        )
        draw.text((x + 24, 132), f"{side} - {metadata['source']}", font=heading_font, fill=(25, 45, 70))
        year_min, year_max = metadata["year_min"], metadata["year_max"]
        year = year_min if year_min == year_max else f"{year_min} - {year_max}"
        info = (
            f"Name: {metadata['name']}\n"
            f"Firm: {'; '.join(metadata['firms'][:4]) or 'unknown'}\n"
            f"Location: {metadata['locality'] or 'unknown'}, {metadata['country'] or 'unknown'}\n"
            f"Year: {year if year_min is not None or year_max is not None else 'unknown'}"
        )
        n10_review._draw_wrapped(draw, info, (x + 24, 178), 72, body_font, (35, 38, 43), 4)
        assets = database.execute(
            "SELECT s.asset_order,f.status,f.cache_path FROM sample_assets s "
            "JOIN fetch_assets f USING(run_id,asset_key) "
            "WHERE s.run_id=? AND s.sample_id=? AND s.side=? ORDER BY s.asset_order",
            (run_id, sample_id, side.casefold()),
        ).fetchall()
        for index, asset in enumerate(assets):
            image_x, image_y = x + 24 + index * 405, 385
            draw.rectangle(
                (image_x, image_y, image_x + 380, image_y + 430),
                fill=(222, 225, 229),
                outline=(150, 155, 163),
                width=2,
            )
            cache_path = asset["cache_path"]
            if cache_path:
                cached = _resolve_text(root, str(cache_path))
                if cached.is_file():
                    try:
                        canvas.paste(n10_review._fit_image(cached.read_bytes(), 376, 390), (image_x + 2, image_y + 38))
                    except OSError:
                        pass
            status = str(asset["status"])
            label = {
                "cache_hit": "frozen cached image",
                "exact": "current image - exact",
                "delivery_changed": "current image - delivery changed",
                "source_changed": "current image - SOURCE CHANGED",
                "failed": "image unavailable",
            }.get(status, status)
            color = (155, 45, 35) if status in {"source_changed", "failed"} else (30, 95, 65)
            draw.text((image_x + 8, image_y + 8), label, font=small_font, fill=color)
        draw.text((x + 24, 850), "Aliases:", font=small_font, fill=(70, 75, 80))
        n10_review._draw_wrapped(
            draw,
            "; ".join(metadata["aliases"][:5]) or "none",
            (x + 24, 875),
            90,
            small_font,
            (70, 75, 80),
            3,
        )
    output = io.BytesIO()
    canvas.save(output, format="PNG", optimize=False)
    raw = output.getvalue()
    _atomic_bytes(path, raw)
    return hashlib.sha256(raw).hexdigest(), len(raw)


def _blind_lane_items(
    database: sqlite3.Connection,
    root: Path,
    run_id: str,
    lane: str,
    sheets_root: Path,
) -> list[dict[str, Any]]:
    fresh = database.execute(
        "SELECT sample_id FROM sample_pairs WHERE run_id=? AND sample_origin='fresh'",
        (run_id,),
    ).fetchall()
    if len(fresh) != FRESH_REVIEW_COUNT:
        raise IdentityN100Error("review packet must contain exactly 105 fresh rows")
    ranked: list[tuple[str, str, str]] = []
    for row in fresh:
        sample_id = str(row["sample_id"])
        digest = hashlib.sha256(f"{REVIEW_VERSION}\0{run_id}\0{lane}\0{sample_id}".encode()).hexdigest()
        opaque = f"{lane}_{digest[:24]}"
        order = hashlib.sha256(f"{SEED}\0blind-order\0{lane}\0{sample_id}".encode()).hexdigest()
        ranked.append((order, opaque, sample_id))
    ranked.sort()
    items: list[dict[str, Any]] = []
    for display_order, (_order, opaque, sample_id) in enumerate(ranked, 1):
        sheet = sheets_root / lane / f"{display_order:03d}_{opaque}.png"
        digest, size = _render_blind_sheet(database, root, run_id, sample_id, opaque, sheet)
        payload = {
            "lane": lane,
            "display_order": display_order,
            "opaque_review_id": opaque,
            "sample_id": sample_id,
            "sheet_path": _path_text(root, sheet),
            "sheet_sha256": digest,
            "sheet_bytes": size,
        }
        database.execute(
            "INSERT INTO review_items VALUES(?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                lane,
                display_order,
                opaque,
                sample_id,
                payload["sheet_path"],
                digest,
                size,
                _record(payload),
            ),
        )
        items.append(
            {
                "display_order": display_order,
                "opaque_review_id": opaque,
                "sheet_path": payload["sheet_path"],
                "sheet_sha256": digest,
                "label": "",
                "rationale": "",
                "hard_conflict_observed": "",
            }
        )
    return items


def _form_payload(run_id: str, lane: str, items: list[dict[str, Any]]) -> dict[str, Any]:
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
        "form_contract_sha256": canonical_sha256(contract),
        "reviewer": "",
        "reviewed_at": "",
        "allowed_labels": sorted(ALLOWED_LABELS),
        "items": items,
    }


def prepare_review(
    *,
    repo_root: Path,
    artifact_path: Path = DEFAULT_OUTPUT,
    output_cache_path: Path = DEFAULT_OUTPUT_CACHE,
    sheets_root: Path = DEFAULT_SHEETS_ROOT,
    r1_form_path: Path = DEFAULT_R1_FORM,
    r2_form_path: Path = DEFAULT_R2_FORM,
    fetch_manifest_path: Path = DEFAULT_FETCH_MANIFEST,
    fetch_report_path: Path = DEFAULT_FETCH_REPORT,
    e2_spec: tuple[Path, int, str] | None = None,
    e1_specs: Mapping[str, tuple[Path, int, str]] | None = None,
    read_caches: Sequence[CacheSpec] = DEFAULT_READ_CACHES,
    workers: int = 4,
    fetcher: Callable[..., FetchPayload] = fetch_once,
    verify_source_pins: bool = True,
    verify_cache_pins: bool = True,
) -> PrepareResult:
    """Prepare 105-row dual-blind packets with one globally deduplicated fetch wave."""

    if workers < 1 or workers > MAX_WORKERS:
        raise IdentityN100Error("workers must be in the inclusive range 1..4")
    root = repo_root.resolve()
    artifact = _path(root, artifact_path)
    output_cache = _path(root, output_cache_path)
    sheet_root = _path(root, sheets_root)
    r1_form = _path(root, r1_form_path)
    r2_form = _path(root, r2_form_path)
    fetch_manifest = _path(root, fetch_manifest_path)
    fetch_report = _path(root, fetch_report_path)
    if not artifact.is_file():
        raise IdentityN100Error("N100 sample sidecar is missing")
    _assert_no_sidecars(artifact)
    for target in (output_cache, sheet_root, r1_form, r2_form, fetch_manifest, fetch_report):
        if target.exists():
            raise IdentityN100Error(f"refusing to clobber prepare artifact: {target}")

    e2_config = e2_spec or n10_review.E2_SPEC
    e1_config = dict(e1_specs or n10_review.E1_SPECS)
    database = sqlite3.connect(artifact)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA foreign_keys=ON")
    v3: sqlite3.Connection | None = None
    e2: sqlite3.Connection | None = None
    e1: dict[str, sqlite3.Connection] = {}
    run_id = ""
    v3_before = ""
    try:
        run = database.execute("SELECT * FROM runs").fetchall()
        if len(run) != 1 or str(run[0]["status"]) != "sample_frozen":
            raise IdentityN100Error("prepare requires one sample_frozen run")
        run_id = str(run[0]["run_id"])
        if int(run[0]["sample_count"]) != TOTAL_COUNT:
            raise IdentityN100Error("sample count is not 110")
        _verify_sample_inputs(database, root, run_id)
        v3_pin = database.execute(
            "SELECT path,sha256_before FROM input_pins WHERE run_id=? AND role='v3_candidates_final'",
            (run_id,),
        ).fetchone()
        if v3_pin is None:
            raise IdentityN100Error("v3 source pin is missing")
        v3_path = _resolve_text(root, str(v3_pin["path"]))
        v3_before = sha256_file(v3_path)
        if v3_before != str(v3_pin["sha256_before"]):
            raise IdentityN100Error("v3 source drift before prepare")
        v3 = _open_immutable(v3_path)

        database.execute("UPDATE runs SET status='fetching' WHERE run_id=?", (run_id,))
        _refresh_run_record(database, run_id)
        _insert_event(database, run_id, "sample_frozen", "fetching", {"workers": workers})
        e2_path, e2_size, e2_sha = e2_config
        _pin_current_file(
            database,
            root,
            run_id,
            "e2_image_evidence",
            e2_path,
            "asset_selection_and_frozen_lineage",
            (e2_size, e2_sha) if verify_source_pins else None,
        )
        e2 = _open_immutable(_path(root, e2_path))
        for source, spec in sorted(e1_config.items()):
            path, size, digest = spec
            _pin_current_file(
                database,
                root,
                run_id,
                f"e1_{source}",
                path,
                "frozen_fetch_lineage",
                (size, digest) if verify_source_pins else None,
            )
            e1[source] = _open_immutable(_path(root, path))
        cache_pins = _pin_read_caches(
            database,
            root,
            run_id,
            read_caches,
            output_cache,
            verify_expected=verify_cache_pins,
        )

        fresh_samples = database.execute(
            "SELECT p.sample_id,p.candidate_id,s.candidate_json FROM sample_pairs p "
            "JOIN candidate_snapshots s USING(run_id,sample_id) "
            "WHERE p.run_id=? AND p.sample_origin='fresh' ORDER BY p.sample_rank",
            (run_id,),
        ).fetchall()
        if len(fresh_samples) != FRESH_REVIEW_COUNT:
            raise IdentityN100Error("prepare requires exactly 105 fresh samples")
        occurrence_count = 0
        for sample in fresh_samples:
            candidate = json.loads(str(sample["candidate_json"]))
            chosen = _choose_assets(v3, e2, candidate)
            for side in ("left", "right"):
                source = str(candidate[f"{side}_source"])
                building_id = str(candidate[f"{side}_source_building_id"])
                if source not in e1:
                    raise IdentityN100Error(f"no E1 source pin for {source}")
                for asset_order, (asset_id, reason) in enumerate(chosen[side], 1):
                    occurrence_count += 1
                    if occurrence_count > MAX_ASSET_OCCURRENCES:
                        raise IdentityN100Error("asset occurrence hard cap exceeded before HTTP")
                    asset_key = hashlib.sha256(f"{source}\0{asset_id}".encode()).hexdigest()
                    e2_asset = n10_review._asset_row(e2, source, asset_id)
                    e1_asset = n10_review._e1_row(e1[source], asset_id)
                    status = "pending"
                    error_kind = error_message = None
                    expected_url = expected_raw = expected_pixel = None
                    cache_role = cache_path = None
                    if e2_asset is None or e1_asset is None:
                        status = "failed"
                        error_kind = "missing_frozen_lineage"
                        error_message = "E1/E2 asset lineage is unavailable"
                    else:
                        expected_url = e1_asset["fetch_url"]
                        expected_raw = e1_asset["raw_response_sha256"]
                        expected_pixel = e1_asset["normalized_pixel_sha256"]
                        if (
                            str(e1_asset["status"]),
                            str(expected_raw),
                            str(expected_pixel),
                        ) != (
                            "success",
                            str(e2_asset["raw_response_sha256"]),
                            str(e2_asset["normalized_pixel_sha256"]),
                        ):
                            raise IdentityN100Error(f"E1/E2 lineage mismatch: {source}/{asset_id}")
                        hit = _cache_hit(root, str(expected_pixel), read_caches)
                        if hit is not None:
                            cache_role, cached = hit
                            cache_path = _path_text(root, cached)
                            status = "cache_hit"
                    fetch_payload = {
                        "asset_key": asset_key,
                        "source": source,
                        "source_asset_id": asset_id,
                        "expected_url": expected_url,
                        "expected_raw_sha256": expected_raw,
                        "expected_pixel_sha256": expected_pixel,
                        "status": status,
                        "cache_role": cache_role,
                        "cache_path": cache_path,
                        "error_kind": error_kind,
                        "error_message": error_message,
                    }
                    existing = database.execute(
                        "SELECT * FROM fetch_assets WHERE run_id=? AND asset_key=?",
                        (run_id, asset_key),
                    ).fetchone()
                    if existing is None:
                        database.execute(
                            "INSERT INTO fetch_assets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                run_id,
                                asset_key,
                                source,
                                asset_id,
                                expected_url,
                                expected_raw,
                                expected_pixel,
                                status,
                                cache_role,
                                cache_path,
                                None,
                                None,
                                None,
                                None,
                                None,
                                0,
                                error_kind,
                                error_message,
                                _record(fetch_payload),
                            ),
                        )
                    else:
                        for key, value in (
                            ("source", source),
                            ("source_asset_id", asset_id),
                            ("expected_url", expected_url),
                            ("expected_raw_sha256", expected_raw),
                            ("expected_pixel_sha256", expected_pixel),
                        ):
                            if existing[key] != value:
                                raise IdentityN100Error(f"global asset lineage collision: {asset_key}/{key}")
                    occurrence_payload = {
                        "sample_id": str(sample["sample_id"]),
                        "side": side,
                        "asset_order": asset_order,
                        "asset_key": asset_key,
                        "source": source,
                        "source_building_id": building_id,
                        "source_asset_id": asset_id,
                        "selection_reason": reason,
                    }
                    database.execute(
                        "INSERT INTO sample_assets VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            run_id,
                            str(sample["sample_id"]),
                            side,
                            asset_order,
                            asset_key,
                            source,
                            building_id,
                            asset_id,
                            reason,
                            _record(occurrence_payload),
                        ),
                    )

        pending = [
            dict(row)
            for row in database.execute(
                "SELECT * FROM fetch_assets WHERE run_id=? AND status='pending' ORDER BY asset_key",
                (run_id,),
            )
        ]
        if len(pending) > MAX_LOGICAL_HTTP_FETCHES:
            raise IdentityN100Error("logical HTTP hard cap exceeded before network")
        database.commit()

        outcomes: list[_FetchOutcome] = []
        if pending:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(_fetch_one, row, fetcher): row["asset_key"] for row in pending}
                for future in as_completed(futures):
                    outcomes.append(future.result())
        outcomes.sort(key=lambda item: item.asset_key)
        wire_requests = sum(item.request_count for item in outcomes)
        if any(item.request_count > 1 or item.redirect_chain for item in outcomes):
            raise IdentityN100Error("redirect/retry invariant violated")
        if wire_requests > MAX_WIRE_HTTP_REQUESTS:
            raise IdentityN100Error("wire HTTP hard cap exceeded")
        for outcome in outcomes:
            row = database.execute(
                "SELECT * FROM fetch_assets WHERE run_id=? AND asset_key=?",
                (run_id, outcome.asset_key),
            ).fetchone()
            status = outcome.status
            cache_role = None
            cache_path = None
            error_kind, error_message = outcome.error_kind, outcome.error_message
            if outcome.body is not None and outcome.pixel_sha256:
                try:
                    materialized = _materialize_derivative(output_cache, outcome.pixel_sha256, outcome.body)
                    cache_role = "wp23_n100_output"
                    cache_path = _path_text(root, materialized)
                except (OSError, ValueError) as exc:
                    status = "failed"
                    error_kind = "derivative_materialization_failed"
                    error_message = str(exc)[:2000]
            updated = {
                "asset_key": outcome.asset_key,
                "source": row["source"],
                "source_asset_id": row["source_asset_id"],
                "expected_url": row["expected_url"],
                "expected_raw_sha256": row["expected_raw_sha256"],
                "expected_pixel_sha256": row["expected_pixel_sha256"],
                "status": status,
                "cache_role": cache_role,
                "cache_path": cache_path,
                "actual_final_url": outcome.final_url,
                "actual_raw_sha256": outcome.raw_sha256,
                "actual_pixel_sha256": outcome.pixel_sha256,
                "http_status": outcome.http_status,
                "response_bytes": outcome.response_bytes,
                "request_count": outcome.request_count,
                "error_kind": error_kind,
                "error_message": error_message,
            }
            database.execute(
                "UPDATE fetch_assets SET status=?,cache_role=?,cache_path=?,actual_final_url=?,"
                "actual_raw_sha256=?,actual_pixel_sha256=?,http_status=?,response_bytes=?,"
                "request_count=?,error_kind=?,error_message=?,record_sha256=? "
                "WHERE run_id=? AND asset_key=?",
                (
                    status,
                    cache_role,
                    cache_path,
                    outcome.final_url,
                    outcome.raw_sha256,
                    outcome.pixel_sha256,
                    outcome.http_status,
                    outcome.response_bytes,
                    outcome.request_count,
                    error_kind,
                    error_message,
                    _record(updated),
                    run_id,
                    outcome.asset_key,
                ),
            )
            attempt_payload = {
                "asset_key": outcome.asset_key,
                "attempt_no": 1,
                "status": status,
                "request_count": outcome.request_count,
                "redirect_chain": list(outcome.redirect_chain),
                "started_at": outcome.started_at,
                "completed_at": outcome.completed_at,
                "error_kind": error_kind,
            }
            database.execute(
                "INSERT INTO fetch_attempts VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    outcome.asset_key,
                    1,
                    status,
                    outcome.request_count,
                    canonical_json(list(outcome.redirect_chain)),
                    outcome.started_at,
                    outcome.completed_at,
                    error_kind,
                    _record(attempt_payload),
                ),
            )

        for spec in read_caches:
            current = cache_tree(_path(root, spec.path))
            before = database.execute(
                "SELECT file_count_before,total_bytes_before,tree_sha256_before FROM cache_pins "
                "WHERE run_id=? AND role=?",
                (run_id, spec.role),
            ).fetchone()
            if current != (int(before[0]), int(before[1]), str(before[2])):
                raise IdentityN100Error(f"read-cache drift during prepare: {spec.role}")
            cache_payload = {
                "role": spec.role,
                "path": _path_text(root, _path(root, spec.path)),
                "transform_version": spec.transform_version,
                "access_mode": "read_only",
                "file_count_before": current[0],
                "total_bytes_before": current[1],
                "tree_sha256_before": current[2],
                "file_count_after": current[0],
                "total_bytes_after": current[1],
                "tree_sha256_after": current[2],
            }
            database.execute(
                "UPDATE cache_pins SET file_count_after=?,total_bytes_after=?,tree_sha256_after=?,"
                "record_sha256=? WHERE run_id=? AND role=?",
                (current[0], current[1], current[2], _record(cache_payload), run_id, spec.role),
            )
        output_tree = cache_tree(output_cache)
        output_pin = database.execute(
            "SELECT * FROM cache_pins WHERE run_id=? AND role='wp23_n100_output'", (run_id,)
        ).fetchone()
        output_payload = {
            "role": "wp23_n100_output",
            "path": output_pin["path"],
            "transform_version": output_pin["transform_version"],
            "access_mode": "write_only",
            "file_count_before": 0,
            "total_bytes_before": 0,
            "tree_sha256_before": output_pin["tree_sha256_before"],
            "file_count_after": output_tree[0],
            "total_bytes_after": output_tree[1],
            "tree_sha256_after": output_tree[2],
        }
        database.execute(
            "UPDATE cache_pins SET file_count_after=?,total_bytes_after=?,tree_sha256_after=?,"
            "record_sha256=? WHERE run_id=? AND role='wp23_n100_output'",
            (output_tree[0], output_tree[1], output_tree[2], _record(output_payload), run_id),
        )

        lane_forms: dict[str, dict[str, Any]] = {}
        for lane in ("r1", "r2"):
            lane_forms[lane] = _form_payload(
                run_id, lane, _blind_lane_items(database, root, run_id, lane, sheet_root)
            )
        if {
            item["opaque_review_id"] for item in lane_forms["r1"]["items"]
        } & {item["opaque_review_id"] for item in lane_forms["r2"]["items"]}:
            raise IdentityN100Error("reviewer opaque-ID namespaces overlap")
        _atomic_json(r1_form, lane_forms["r1"])
        _atomic_json(r2_form, lane_forms["r2"])
        _verify_sample_inputs(database, root, run_id)
        if sha256_file(v3_path) != v3_before:
            raise IdentityN100Error("v3 source drift during prepare")
        unique_assets = int(
            database.execute("SELECT count(*) FROM fetch_assets WHERE run_id=?", (run_id,)).fetchone()[0]
        )
        database.execute(
            "UPDATE runs SET status='review_ready',asset_occurrence_count=?,unique_asset_count=?,"
            "logical_http_fetches=?,wire_http_requests=? WHERE run_id=?",
            (occurrence_count, unique_assets, len(pending), wire_requests, run_id),
        )
        _refresh_run_record(database, run_id)
        _insert_event(
            database,
            run_id,
            "fetching",
            "review_ready",
            {
                "fresh_rows_per_lane": FRESH_REVIEW_COUNT,
                "asset_occurrences": occurrence_count,
                "unique_assets": unique_assets,
                "logical_http_fetches": len(pending),
                "wire_http_requests": wire_requests,
            },
        )
        database.commit()

        fetch_rows = [
            dict(row)
            for row in database.execute(
                "SELECT asset_key,source,source_asset_id,status,cache_role,cache_path,request_count,"
                "error_kind FROM fetch_assets WHERE run_id=? ORDER BY asset_key",
                (run_id,),
            )
        ]
        cache_rows = [
            dict(row)
            for row in database.execute(
                "SELECT role,path,transform_version,access_mode,file_count_before,total_bytes_before,"
                "tree_sha256_before,file_count_after,total_bytes_after,tree_sha256_after "
                "FROM cache_pins WHERE run_id=? ORDER BY role",
                (run_id,),
            )
        ]
        manifest_payload = {
            "status": "PASS",
            "run_id": run_id,
            "review_version": REVIEW_VERSION,
            "fresh_review_rows_per_lane": FRESH_REVIEW_COUNT,
            "carry_forward_provenance_rows": 5,
            "asset_occurrence_count": occurrence_count,
            "unique_asset_count": unique_assets,
            "logical_http_fetches": len(pending),
            "wire_http_requests": wire_requests,
            "hard_caps": {
                "asset_occurrences": MAX_ASSET_OCCURRENCES,
                "logical_http_fetches": MAX_LOGICAL_HTTP_FETCHES,
                "wire_http_requests": MAX_WIRE_HTTP_REQUESTS,
                "redirects": 0,
                "retries": 0,
            },
            "v3_sha256_before": v3_before,
            "v3_sha256_after": sha256_file(v3_path),
            "cache_pins": cache_rows,
            "fetch_assets": fetch_rows,
            "forms": {
                "r1": {"path": _path_text(root, r1_form), "sha256": sha256_file(r1_form)},
                "r2": {"path": _path_text(root, r2_form), "sha256": sha256_file(r2_form)},
            },
            "sheet_count": FRESH_REVIEW_COUNT * 2,
            "vision_requests": 0,
            "llm_requests": 0,
        }
        report_payload = {
            "status": "PASS",
            "run_id": run_id,
            "counts": dict(Counter(row["status"] for row in fetch_rows)),
            "review_completion_contract": {
                "r1_fresh": f"{FRESH_REVIEW_COUNT}/{FRESH_REVIEW_COUNT}",
                "r2_fresh": f"{FRESH_REVIEW_COUNT}/{FRESH_REVIEW_COUNT}",
                "carry_provenance": "5/5",
            },
            "network": {
                "logical": len(pending),
                "wire": wire_requests,
                "max_redirects": 0,
                "retries": 0,
            },
        }
        _atomic_json(fetch_manifest, manifest_payload)
        _atomic_json(fetch_report, report_payload)
        return PrepareResult(
            artifact,
            run_id,
            occurrence_count,
            unique_assets,
            len(pending),
            wire_requests,
            sum(row["status"] == "cache_hit" for row in fetch_rows),
            FRESH_REVIEW_COUNT * 2,
        )
    except Exception:
        database.rollback()
        if run_id:
            try:
                current = database.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
                if current is not None and str(current[0]) not in {"pass", "fail", "failed"}:
                    database.execute("UPDATE runs SET status='failed' WHERE run_id=?", (run_id,))
                    _refresh_run_record(database, run_id)
                    _insert_event(database, run_id, str(current[0]), "failed", {"phase": "prepare"})
                    database.commit()
            except sqlite3.Error:
                database.rollback()
        raise
    finally:
        for connection in e1.values():
            connection.close()
        if e2 is not None:
            e2.close()
        if v3 is not None:
            v3.close()
        database.close()


def _normalized_actor(value: Any) -> str:
    normalized = " ".join(str(value or "").split()).casefold()
    if not normalized:
        raise IdentityN100Error("reviewer/adjudicator identity is required")
    return normalized


def _validate_completed_review_form(
    database: sqlite3.Connection,
    root: Path,
    run_id: str,
    lane: str,
    form: Mapping[str, Any],
) -> tuple[str, str, list[dict[str, Any]]]:
    if form.get("review_version") != REVIEW_VERSION or form.get("run_id") != run_id:
        raise IdentityN100Error("review form run/version mismatch")
    if form.get("lane") != lane:
        raise IdentityN100Error("review form lane mismatch")
    reviewer = str(form.get("reviewer", "")).strip()
    _normalized_actor(reviewer)
    reviewed_at = str(form.get("reviewed_at", "")).strip() or utc_now()
    raw_items = form.get("items")
    if not isinstance(raw_items, list) or len(raw_items) != FRESH_REVIEW_COUNT:
        raise IdentityN100Error("completed reviewer form must contain 105 rows")
    expected = {
        str(row["opaque_review_id"]): dict(row)
        for row in database.execute(
            "SELECT * FROM review_items WHERE run_id=? AND lane=?", (run_id, lane)
        )
    }
    if len(expected) != FRESH_REVIEW_COUNT:
        raise IdentityN100Error("review item ledger is incomplete")
    seen: set[str] = set()
    validated: list[dict[str, Any]] = []
    contract_items: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, Mapping):
            raise IdentityN100Error("review row must be an object")
        opaque = str(item.get("opaque_review_id", ""))
        if not opaque or opaque in seen or opaque not in expected:
            raise IdentityN100Error("unknown or duplicate opaque review ID")
        seen.add(opaque)
        frozen = expected[opaque]
        if int(item.get("display_order", -1)) != int(frozen["display_order"]):
            raise IdentityN100Error("review display order changed")
        if item.get("sheet_path") != frozen["sheet_path"] or item.get("sheet_sha256") != frozen["sheet_sha256"]:
            raise IdentityN100Error("review sheet binding changed")
        sheet = _resolve_text(root, str(frozen["sheet_path"]))
        if not sheet.is_file() or sha256_file(sheet) != str(frozen["sheet_sha256"]):
            raise IdentityN100Error("review sheet missing or changed")
        label = str(item.get("label", "")).strip()
        rationale = str(item.get("rationale", "")).strip()
        hard_conflict = str(item.get("hard_conflict_observed", "")).strip()
        if label not in ALLOWED_LABELS or not rationale or not hard_conflict:
            raise IdentityN100Error("review row is incomplete or has an invalid label")
        contract_items.append(
            {
                "display_order": int(frozen["display_order"]),
                "opaque_review_id": opaque,
                "sheet_path": str(frozen["sheet_path"]),
                "sheet_sha256": str(frozen["sheet_sha256"]),
            }
        )
        validated.append(
            {
                "display_order": int(frozen["display_order"]),
                "opaque_review_id": opaque,
                "sample_id": str(frozen["sample_id"]),
                "label": label,
                "rationale": rationale,
                "hard_conflict_observed": hard_conflict,
            }
        )
    if seen != set(expected):
        raise IdentityN100Error("review form omits frozen rows")
    contract_items.sort(key=lambda item: item["display_order"])
    contract = {
        "review_version": REVIEW_VERSION,
        "run_id": run_id,
        "lane": lane,
        "items": contract_items,
    }
    if form.get("form_contract_sha256") != canonical_sha256(contract):
        raise IdentityN100Error("review form contract SHA mismatch")
    validated.sort(key=lambda item: item["display_order"])
    return reviewer, reviewed_at, validated


def lock_review(
    *,
    repo_root: Path,
    lane: str,
    completed_form_path: Path,
    locked_output_path: Path,
    artifact_path: Path = DEFAULT_OUTPUT,
) -> Path:
    """Validate and append one complete blind ledger; never unblind here."""

    if lane not in {"r1", "r2"}:
        raise IdentityN100Error("lane must be r1 or r2")
    root = repo_root.resolve()
    artifact = _path(root, artifact_path)
    completed = _path(root, completed_form_path)
    locked = _path(root, locked_output_path)
    if not completed.is_file():
        raise IdentityN100Error("completed review form is missing")
    if locked.exists():
        raise IdentityN100Error("refusing to clobber locked review ledger")
    form = json.loads(completed.read_text(encoding="utf-8"))
    database = sqlite3.connect(artifact)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA foreign_keys=ON")
    try:
        run = database.execute("SELECT * FROM runs").fetchone()
        if run is None or str(run["status"]) not in {"review_ready", "reviews_partial"}:
            raise IdentityN100Error("review lock requires review_ready/reviews_partial state")
        run_id = str(run["run_id"])
        if database.execute(
            "SELECT 1 FROM review_locks WHERE run_id=? AND lane=?", (run_id, lane)
        ).fetchone():
            raise IdentityN100Error("review lane is already locked")
        reviewer, reviewed_at, rows = _validate_completed_review_form(
            database, root, run_id, lane, form
        )
        other = database.execute(
            "SELECT reviewer FROM review_locks WHERE run_id=? AND lane<>?", (run_id, lane)
        ).fetchone()
        if other is not None and _normalized_actor(other[0]) == _normalized_actor(reviewer):
            raise IdentityN100Error("the two reviewer lanes require distinct reviewers")
        ledger_rows = []
        for row in rows:
            payload = {
                "lane": lane,
                "opaque_review_id": row["opaque_review_id"],
                "sample_id": row["sample_id"],
                "label": row["label"],
                "rationale": row["rationale"],
                "hard_conflict_observed": row["hard_conflict_observed"],
                "reviewer": reviewer,
                "reviewed_at": reviewed_at,
            }
            database.execute(
                "INSERT INTO blind_reviews VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    lane,
                    row["opaque_review_id"],
                    row["sample_id"],
                    row["label"],
                    row["rationale"],
                    row["hard_conflict_observed"],
                    reviewer,
                    reviewed_at,
                    _record(payload),
                ),
            )
            ledger_rows.append({key: value for key, value in payload.items() if key != "sample_id"})
        ordered_ledger_sha = canonical_sha256(ledger_rows)
        locked_payload = {
            "review_version": REVIEW_VERSION,
            "run_id": run_id,
            "lane": lane,
            "reviewer": reviewer,
            "reviewed_at": reviewed_at,
            "row_count": len(rows),
            "ordered_ledger_sha256": ordered_ledger_sha,
            "items": ledger_rows,
        }
        _atomic_json(locked, locked_payload)
        locked_at = utc_now()
        lock_record = {
            "lane": lane,
            "reviewer": reviewer,
            "row_count": len(rows),
            "completed_form_path": _path_text(root, completed),
            "completed_form_sha256": sha256_file(completed),
            "locked_output_path": _path_text(root, locked),
            "locked_output_sha256": sha256_file(locked),
            "ordered_ledger_sha256": ordered_ledger_sha,
            "locked_at": locked_at,
        }
        database.execute(
            "INSERT INTO review_locks VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                lane,
                reviewer,
                len(rows),
                lock_record["completed_form_path"],
                lock_record["completed_form_sha256"],
                lock_record["locked_output_path"],
                lock_record["locked_output_sha256"],
                ordered_ledger_sha,
                locked_at,
                _record(lock_record),
            ),
        )
        lock_count = int(
            database.execute("SELECT count(*) FROM review_locks WHERE run_id=?", (run_id,)).fetchone()[0]
        )
        from_status = str(run["status"])
        to_status = "reviews_locked" if lock_count == 2 else "reviews_partial"
        database.execute(
            f"UPDATE runs SET status=?,{lane}_status='locked' WHERE run_id=?",
            (to_status, run_id),
        )
        _refresh_run_record(database, run_id)
        _insert_event(
            database,
            run_id,
            from_status,
            to_status,
            {"lane": lane, "row_count": len(rows), "ordered_ledger_sha256": ordered_ledger_sha},
        )
        database.commit()
        return locked
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def unblind_reviews(
    *,
    repo_root: Path,
    artifact_path: Path = DEFAULT_OUTPUT,
    packet_path: Path = DEFAULT_ADJUDICATION_PACKET,
    adjudication_form_path: Path = DEFAULT_ADJUDICATION_FORM,
) -> tuple[Path, Path]:
    """Cross the unblind barrier only after both independent 105-row locks."""

    root = repo_root.resolve()
    artifact = _path(root, artifact_path)
    packet = _path(root, packet_path)
    form_path = _path(root, adjudication_form_path)
    if packet.exists() or form_path.exists():
        raise IdentityN100Error("refusing to clobber unblind artifact")
    database = sqlite3.connect(artifact)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA foreign_keys=ON")
    try:
        run = database.execute("SELECT * FROM runs").fetchone()
        if run is None or str(run["status"]) != "reviews_locked":
            raise IdentityN100Error("both review lanes must be locked before unblind")
        run_id = str(run["run_id"])
        locks = database.execute(
            "SELECT * FROM review_locks WHERE run_id=? ORDER BY lane", (run_id,)
        ).fetchall()
        if len(locks) != 2 or {str(row["lane"]) for row in locks} != {"r1", "r2"}:
            raise IdentityN100Error("both reviewer locks are required")
        if any(int(row["row_count"]) != FRESH_REVIEW_COUNT for row in locks):
            raise IdentityN100Error("review lock row-count mismatch")
        if _normalized_actor(locks[0]["reviewer"]) == _normalized_actor(locks[1]["reviewer"]):
            raise IdentityN100Error("reviewers are not independent")
        for lock in locks:
            locked_path = _resolve_text(root, str(lock["locked_output_path"]))
            if not locked_path.is_file() or sha256_file(locked_path) != str(lock["locked_output_sha256"]):
                raise IdentityN100Error("locked reviewer ledger missing or changed")
        if int(
            database.execute("SELECT count(*) FROM blind_reviews WHERE run_id=?", (run_id,)).fetchone()[0]
        ) != FRESH_REVIEW_COUNT * 2:
            raise IdentityN100Error("blind review rows are incomplete")
        samples = database.execute(
            "SELECT p.sample_rank,p.sample_id,p.cohort,p.stratum,p.candidate_id,"
            "s.candidate_json,s.left_metadata_json,s.right_metadata_json,s.families_json,"
            "s.conflicts_json,s.policy_flags_json FROM sample_pairs p "
            "JOIN candidate_snapshots s USING(run_id,sample_id) "
            "WHERE p.run_id=? AND p.sample_origin='fresh' ORDER BY p.sample_rank",
            (run_id,),
        ).fetchall()
        items: list[dict[str, Any]] = []
        form_items: list[dict[str, Any]] = []
        for sample in samples:
            reviews = {
                str(row["lane"]): dict(row)
                for row in database.execute(
                    "SELECT b.*,i.opaque_review_id FROM blind_reviews b JOIN review_items i "
                    "USING(run_id,lane,opaque_review_id,sample_id) "
                    "WHERE b.run_id=? AND b.sample_id=? ORDER BY b.lane",
                    (run_id, sample["sample_id"]),
                )
            }
            if set(reviews) != {"r1", "r2"}:
                raise IdentityN100Error("sample is missing a reviewer lane")
            item = {
                "sample_rank": int(sample["sample_rank"]),
                "sample_id": str(sample["sample_id"]),
                "candidate_id": str(sample["candidate_id"]),
                "cohort": str(sample["cohort"]),
                "stratum": str(sample["stratum"]),
                "candidate": json.loads(str(sample["candidate_json"])),
                "left": json.loads(str(sample["left_metadata_json"])),
                "right": json.loads(str(sample["right_metadata_json"])),
                "families": json.loads(str(sample["families_json"])),
                "conflicts": json.loads(str(sample["conflicts_json"])),
                "policy_flags": json.loads(str(sample["policy_flags_json"])),
                "reviews": {
                    lane: {
                        "opaque_review_id": str(reviews[lane]["opaque_review_id"]),
                        "label": str(reviews[lane]["label"]),
                        "rationale": str(reviews[lane]["rationale"]),
                        "hard_conflict_observed": str(reviews[lane]["hard_conflict_observed"]),
                        "reviewer": str(reviews[lane]["reviewer"]),
                    }
                    for lane in ("r1", "r2")
                },
            }
            items.append(item)
            form_items.append(
                {
                    "sample_rank": int(sample["sample_rank"]),
                    "sample_id": str(sample["sample_id"]),
                    "candidate_id": str(sample["candidate_id"]),
                    "r1_label": str(reviews["r1"]["label"]),
                    "r2_label": str(reviews["r2"]["label"]),
                    "adjudicator_label": "",
                    "unresolved_disagreement": False,
                    "rationale": "",
                }
            )
        if len(items) != FRESH_REVIEW_COUNT:
            raise IdentityN100Error("unblind packet must contain 105 fresh rows")
        packet_payload = {
            "adjudication_version": ADJUDICATION_VERSION,
            "run_id": run_id,
            "fresh_row_count": FRESH_REVIEW_COUNT,
            "carry_forward_rows": 5,
            "items": items,
        }
        form_contract = {
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
                for item in form_items
            ],
        }
        form_payload = {
            **form_contract,
            "form_contract_sha256": canonical_sha256(form_contract),
            "adjudicator": "",
            "adjudicated_at": "",
            "allowed_labels": sorted(ALLOWED_LABELS),
            "items": form_items,
        }
        _atomic_json(packet, packet_payload)
        _atomic_json(form_path, form_payload)
        created_at = utc_now()
        artifact_payload = {
            "packet_path": _path_text(root, packet),
            "packet_sha256": sha256_file(packet),
            "form_path": _path_text(root, form_path),
            "form_sha256": sha256_file(form_path),
            "r1_ledger_sha256": str(next(row for row in locks if row["lane"] == "r1")["ordered_ledger_sha256"]),
            "r2_ledger_sha256": str(next(row for row in locks if row["lane"] == "r2")["ordered_ledger_sha256"]),
            "created_at": created_at,
        }
        database.execute(
            "INSERT INTO unblind_artifacts VALUES(?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                artifact_payload["packet_path"],
                artifact_payload["packet_sha256"],
                artifact_payload["form_path"],
                artifact_payload["form_sha256"],
                artifact_payload["r1_ledger_sha256"],
                artifact_payload["r2_ledger_sha256"],
                created_at,
                _record(artifact_payload),
            ),
        )
        database.execute(
            "UPDATE runs SET status='unblinded',unblind_status='complete' WHERE run_id=?",
            (run_id,),
        )
        _refresh_run_record(database, run_id)
        _insert_event(
            database,
            run_id,
            "reviews_locked",
            "unblinded",
            {"fresh_rows": FRESH_REVIEW_COUNT, "carry_provenance_rows": 5},
        )
        database.commit()
        return packet, form_path
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def clopper_pearson(successes: int, trials: int, alpha: float = 0.05) -> tuple[float, float]:
    """Two-sided exact binomial interval without a SciPy runtime dependency."""

    if trials < 1 or successes < 0 or successes > trials or not (0.0 < alpha < 1.0):
        raise IdentityN100Error("invalid Clopper-Pearson arguments")

    def probability_at_least(k: int, probability: float) -> float:
        return sum(
            math.comb(trials, value)
            * probability**value
            * (1.0 - probability) ** (trials - value)
            for value in range(k, trials + 1)
        )

    def probability_at_most(k: int, probability: float) -> float:
        return sum(
            math.comb(trials, value)
            * probability**value
            * (1.0 - probability) ** (trials - value)
            for value in range(0, k + 1)
        )

    if successes == 0:
        lower = 0.0
    else:
        low, high = 0.0, 1.0
        target = alpha / 2.0
        for _ in range(100):
            middle = (low + high) / 2.0
            if probability_at_least(successes, middle) < target:
                low = middle
            else:
                high = middle
        lower = (low + high) / 2.0
    if successes == trials:
        upper = 1.0
    else:
        low, high = 0.0, 1.0
        target = alpha / 2.0
        for _ in range(100):
            middle = (low + high) / 2.0
            if probability_at_most(successes, middle) > target:
                low = middle
            else:
                high = middle
        upper = (low + high) / 2.0
    return lower, upper


def _insert_metric(
    database: sqlite3.Connection,
    run_id: str,
    phase: str,
    metric: str,
    value: Any,
) -> None:
    payload = {"phase": phase, "metric": metric, "value": value}
    database.execute(
        "INSERT INTO metrics VALUES(?,?,?,?,?)",
        (run_id, phase, metric, canonical_json(value), _record(payload)),
    )


def _validate_adjudication_form(
    database: sqlite3.Connection,
    run_id: str,
    form: Mapping[str, Any],
) -> tuple[str, str, list[dict[str, Any]]]:
    if form.get("adjudication_version") != ADJUDICATION_VERSION or form.get("run_id") != run_id:
        raise IdentityN100Error("adjudication form run/version mismatch")
    adjudicator = str(form.get("adjudicator", "")).strip()
    normalized_adjudicator = _normalized_actor(adjudicator)
    reviewers = {
        _normalized_actor(row[0])
        for row in database.execute("SELECT reviewer FROM review_locks WHERE run_id=?", (run_id,))
    }
    if normalized_adjudicator in reviewers or len(reviewers) != 2:
        raise IdentityN100Error("adjudicator must be distinct from both reviewers")
    adjudicated_at = str(form.get("adjudicated_at", "")).strip() or utc_now()
    expected: dict[str, dict[str, Any]] = {}
    for row in database.execute(
        "SELECT p.sample_rank,p.sample_id,p.candidate_id,"
        "max(CASE WHEN b.lane='r1' THEN b.label END) r1_label,"
        "max(CASE WHEN b.lane='r2' THEN b.label END) r2_label "
        "FROM sample_pairs p JOIN blind_reviews b USING(run_id,sample_id) "
        "WHERE p.run_id=? AND p.sample_origin='fresh' GROUP BY p.sample_id ORDER BY p.sample_rank",
        (run_id,),
    ):
        expected[str(row["sample_id"])] = dict(row)
    raw_items = form.get("items")
    if not isinstance(raw_items, list) or len(raw_items) != FRESH_REVIEW_COUNT:
        raise IdentityN100Error("adjudication form must contain 105 fresh rows")
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    contract_items: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, Mapping):
            raise IdentityN100Error("adjudication row must be an object")
        sample_id = str(item.get("sample_id", ""))
        if not sample_id or sample_id in seen or sample_id not in expected:
            raise IdentityN100Error("unknown or duplicate adjudication sample")
        seen.add(sample_id)
        frozen = expected[sample_id]
        if (
            int(item.get("sample_rank", -1)) != int(frozen["sample_rank"])
            or item.get("candidate_id") != frozen["candidate_id"]
            or item.get("r1_label") != frozen["r1_label"]
            or item.get("r2_label") != frozen["r2_label"]
        ):
            raise IdentityN100Error("adjudication form binding changed")
        label = str(item.get("adjudicator_label", "")).strip()
        rationale = str(item.get("rationale", "")).strip()
        unresolved_raw = item.get("unresolved_disagreement")
        if not isinstance(unresolved_raw, bool):
            raise IdentityN100Error("unresolved_disagreement must be boolean")
        unresolved = bool(unresolved_raw)
        agreement = frozen["r1_label"] == frozen["r2_label"]
        if label not in ALLOWED_LABELS or not rationale:
            raise IdentityN100Error("adjudication row is incomplete or invalid")
        if agreement and unresolved:
            raise IdentityN100Error("an agreeing reviewer pair cannot be unresolved")
        final_label = "insufficient" if (not agreement and unresolved) else label
        contract_items.append(
            {
                "sample_rank": int(frozen["sample_rank"]),
                "sample_id": sample_id,
                "candidate_id": str(frozen["candidate_id"]),
                "r1_label": str(frozen["r1_label"]),
                "r2_label": str(frozen["r2_label"]),
            }
        )
        rows.append(
            {
                "sample_rank": int(frozen["sample_rank"]),
                "sample_id": sample_id,
                "candidate_id": str(frozen["candidate_id"]),
                "r1_label": str(frozen["r1_label"]),
                "r2_label": str(frozen["r2_label"]),
                "reviewer_agreement": int(agreement),
                "adjudicator_label": label,
                "final_gold_label": final_label,
                "unresolved_disagreement": int(unresolved),
                "rationale": rationale,
            }
        )
    if seen != set(expected):
        raise IdentityN100Error("adjudication form omits frozen rows")
    contract_items.sort(key=lambda item: item["sample_rank"])
    contract = {
        "adjudication_version": ADJUDICATION_VERSION,
        "run_id": run_id,
        "items": contract_items,
    }
    if form.get("form_contract_sha256") != canonical_sha256(contract):
        raise IdentityN100Error("adjudication form contract SHA mismatch")
    rows.sort(key=lambda item: item["sample_rank"])
    return adjudicator, adjudicated_at, rows


def finalize_review(
    *,
    repo_root: Path,
    completed_adjudication_path: Path,
    artifact_path: Path = DEFAULT_OUTPUT,
    locked_adjudication_path: Path = DEFAULT_ADJUDICATION_LOCKED,
    gate_path: Path = DEFAULT_GATE,
    report_path: Path = DEFAULT_REPORT,
    manifest_path: Path = DEFAULT_MANIFEST,
) -> FinalizeResult:
    """Freeze fresh gold, add inherited carry gold, and evaluate the A70 gate."""

    root = repo_root.resolve()
    artifact = _path(root, artifact_path)
    completed = _path(root, completed_adjudication_path)
    locked = _path(root, locked_adjudication_path)
    gate_output = _path(root, gate_path)
    report_output = _path(root, report_path)
    manifest_output = _path(root, manifest_path)
    if not completed.is_file():
        raise IdentityN100Error("completed adjudication form is missing")
    for target in (locked, gate_output, report_output, manifest_output):
        if target.exists():
            raise IdentityN100Error(f"refusing to clobber final artifact: {target}")
    form = json.loads(completed.read_text(encoding="utf-8"))
    database = sqlite3.connect(artifact)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA foreign_keys=ON")
    try:
        run = database.execute("SELECT * FROM runs").fetchone()
        if run is None or str(run["status"]) != "unblinded":
            raise IdentityN100Error("finalization requires unblinded state")
        run_id = str(run["run_id"])
        _verify_sample_inputs(database, root, run_id)
        if _logical_sha(database, run_id) != str(run["sample_logical_sha256"]):
            raise IdentityN100Error("frozen sample logical SHA drift")
        v3_pin = database.execute(
            "SELECT path,sha256_before FROM input_pins WHERE run_id=? AND role='v3_candidates_final'",
            (run_id,),
        ).fetchone()
        v3_path = _resolve_text(root, str(v3_pin["path"]))
        v3_before = sha256_file(v3_path)
        if v3_before != str(v3_pin["sha256_before"]):
            raise IdentityN100Error("v3 source drift at finalization")
        cache_rows = [
            dict(row)
            for row in database.execute(
                "SELECT role,path,access_mode,file_count_before,total_bytes_before,tree_sha256_before,"
                "file_count_after,total_bytes_after,tree_sha256_after FROM cache_pins "
                "WHERE run_id=? ORDER BY role",
                (run_id,),
            )
        ]
        for pin in cache_rows:
            current = cache_tree(_resolve_text(root, str(pin["path"])))
            expected = (
                int(pin["file_count_after"]),
                int(pin["total_bytes_after"]),
                str(pin["tree_sha256_after"]),
            )
            if current != expected:
                raise IdentityN100Error(f"cache drift at finalization: {pin['role']}")
        adjudicator, adjudicated_at, rows = _validate_adjudication_form(database, run_id, form)
        locked_rows: list[dict[str, Any]] = []
        for row in rows:
            payload = {
                "sample_id": row["sample_id"],
                "r1_label": row["r1_label"],
                "r2_label": row["r2_label"],
                "reviewer_agreement": row["reviewer_agreement"],
                "adjudicator_label": row["adjudicator_label"],
                "final_gold_label": row["final_gold_label"],
                "unresolved_disagreement": row["unresolved_disagreement"],
                "rationale": row["rationale"],
                "adjudicator": adjudicator,
                "adjudicated_at": adjudicated_at,
            }
            database.execute(
                "INSERT INTO adjudications VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    row["sample_id"],
                    row["r1_label"],
                    row["r2_label"],
                    row["reviewer_agreement"],
                    row["adjudicator_label"],
                    row["final_gold_label"],
                    row["unresolved_disagreement"],
                    row["rationale"],
                    adjudicator,
                    adjudicated_at,
                    _record(payload),
                ),
            )
            locked_rows.append({"candidate_id": row["candidate_id"], **payload})
        locked_payload = {
            "adjudication_version": ADJUDICATION_VERSION,
            "run_id": run_id,
            "adjudicator": adjudicator,
            "adjudicated_at": adjudicated_at,
            "fresh_row_count": len(locked_rows),
            "items": locked_rows,
        }
        _atomic_json(locked, locked_payload)

        core_rows = database.execute(
            "SELECT p.sample_id,p.candidate_id,a.final_gold_label,s.conflicts_json "
            "FROM sample_pairs p JOIN adjudications a USING(run_id,sample_id) "
            "JOIN candidate_snapshots s USING(run_id,sample_id) "
            "WHERE p.run_id=? AND p.cohort='A' ORDER BY p.sample_rank",
            (run_id,),
        ).fetchall()
        if len(core_rows) != CORE_COUNT:
            raise IdentityN100Error("A70 final gold is incomplete")
        core_labels = Counter(str(row["final_gold_label"]) for row in core_rows)
        false_merge_count = core_labels["different"]
        hard_conflict_violation_count = sum(
            bool(json.loads(str(row["conflicts_json"]))) for row in core_rows
        )
        same_count = core_labels["same"]
        precision = same_count / CORE_COUNT
        cp_low, cp_high = clopper_pearson(same_count, CORE_COUNT)

        missing_sheets = 0
        for item in database.execute(
            "SELECT sheet_path,sheet_sha256 FROM review_items WHERE run_id=?", (run_id,)
        ):
            sheet = _resolve_text(root, str(item["sheet_path"]))
            if not sheet.is_file() or sha256_file(sheet) != str(item["sheet_sha256"]):
                missing_sheets += 1
        missing_reviews = FRESH_REVIEW_COUNT * 2 - int(
            database.execute("SELECT count(*) FROM blind_reviews WHERE run_id=?", (run_id,)).fetchone()[0]
        )
        missing_adjudications = FRESH_REVIEW_COUNT - int(
            database.execute("SELECT count(*) FROM adjudications WHERE run_id=?", (run_id,)).fetchone()[0]
        )
        missing_carry = 5 - int(
            database.execute("SELECT count(*) FROM carry_forward_gold WHERE run_id=?", (run_id,)).fetchone()[0]
        )
        unreviewable_count = max(0, missing_sheets) + max(0, missing_reviews) + max(0, missing_adjudications) + max(0, missing_carry)

        diagnostic: dict[str, Any] = {}
        for stratum in sorted(FRESH_QUOTAS):
            fresh_labels = Counter(
                str(row[0])
                for row in database.execute(
                    "SELECT a.final_gold_label FROM sample_pairs p JOIN adjudications a USING(run_id,sample_id) "
                    "WHERE p.run_id=? AND p.stratum=? AND p.sample_origin='fresh'",
                    (run_id, stratum),
                )
            )
            carry_labels = Counter(
                str(row[0])
                for row in database.execute(
                    "SELECT c.final_gold_label FROM sample_pairs p JOIN carry_forward_gold c USING(run_id,sample_id) "
                    "WHERE p.run_id=? AND p.stratum=?",
                    (run_id, stratum),
                )
            )
            fresh_denominator = FRESH_QUOTAS[stratum]
            evidence_memo: list[dict[str, Any]] = []
            for evidence in database.execute(
                "SELECT p.sample_origin,p.candidate_id,a.final_gold_label,s.families_json,s.conflicts_json,"
                "a.rationale adjudicator_rationale,"
                "max(CASE WHEN b.lane='r1' THEN b.rationale END) r1_rationale,"
                "max(CASE WHEN b.lane='r2' THEN b.rationale END) r2_rationale "
                "FROM sample_pairs p JOIN adjudications a USING(run_id,sample_id) "
                "JOIN candidate_snapshots s USING(run_id,sample_id) "
                "JOIN blind_reviews b USING(run_id,sample_id) "
                "WHERE p.run_id=? AND p.stratum=? AND p.sample_origin='fresh' "
                "GROUP BY p.sample_id ORDER BY p.sample_rank",
                (run_id, stratum),
            ):
                families = json.loads(str(evidence["families_json"]))
                evidence_memo.append(
                    {
                        "sample_origin": "fresh",
                        "candidate_id": str(evidence["candidate_id"]),
                        "final_gold_label": str(evidence["final_gold_label"]),
                        "hard_conflicts": json.loads(str(evidence["conflicts_json"])),
                        "positive_families": [
                            row for row in families if str(row.get("status")) == "positive"
                        ],
                        "reviewer_rationales": {
                            "r1": str(evidence["r1_rationale"]),
                            "r2": str(evidence["r2_rationale"]),
                        },
                        "adjudicator_rationale": str(evidence["adjudicator_rationale"]),
                    }
                )
            for evidence in database.execute(
                "SELECT p.candidate_id,c.final_gold_label,s.families_json,s.conflicts_json,"
                "c.reviewer_rationale,c.adjudicator_rationale,c.prior_run_id,"
                "c.prior_opaque_review_id,c.blind_review_record_sha256,c.adjudication_record_sha256 "
                "FROM sample_pairs p JOIN carry_forward_gold c USING(run_id,sample_id) "
                "JOIN candidate_snapshots s USING(run_id,sample_id) "
                "WHERE p.run_id=? AND p.stratum=? ORDER BY p.sample_rank",
                (run_id, stratum),
            ):
                families = json.loads(str(evidence["families_json"]))
                evidence_memo.append(
                    {
                        "sample_origin": "carry_forward",
                        "candidate_id": str(evidence["candidate_id"]),
                        "final_gold_label": str(evidence["final_gold_label"]),
                        "hard_conflicts": json.loads(str(evidence["conflicts_json"])),
                        "positive_families": [
                            row for row in families if str(row.get("status")) == "positive"
                        ],
                        "reviewer_rationales": {
                            "v3_single_reviewer": str(evidence["reviewer_rationale"]),
                            "second_reviewer": None,
                        },
                        "adjudicator_rationale": str(evidence["adjudicator_rationale"]),
                        "provenance": {
                            "prior_run_id": str(evidence["prior_run_id"]),
                            "prior_opaque_review_id": str(evidence["prior_opaque_review_id"]),
                            "blind_review_record_sha256": str(evidence["blind_review_record_sha256"]),
                            "adjudication_record_sha256": str(evidence["adjudication_record_sha256"]),
                        },
                    }
                )
            combined_labels = fresh_labels + carry_labels
            combined_denominator = fresh_denominator + len(CARRY_FORWARD[stratum])
            diagnostic[stratum] = {
                "fresh": {
                    "denominator": fresh_denominator,
                    "label_counts": {label: fresh_labels[label] for label in sorted(ALLOWED_LABELS)},
                    "same_rate": fresh_labels["same"] / fresh_denominator,
                    "fresh_false_reject_rate": fresh_labels["same"] / fresh_denominator,
                },
                "carry_forward": {
                    "denominator": len(CARRY_FORWARD[stratum]),
                    "label_counts": {label: carry_labels[label] for label in sorted(ALLOWED_LABELS)},
                    "provenance_only": True,
                    "carry_forward_confirmation": len(CARRY_FORWARD[stratum]) == sum(carry_labels.values()),
                },
                "combined_descriptive": {
                    "denominator": combined_denominator,
                    "label_counts": {label: combined_labels[label] for label in sorted(ALLOWED_LABELS)},
                    "same_rate": combined_labels["same"] / combined_denominator,
                    "inferential": False,
                },
                "evidence_memo": evidence_memo,
            }
        gate_pass = (
            false_merge_count == 0
            and hard_conflict_violation_count == 0
            and unreviewable_count == 0
        )
        status = "PASS" if gate_pass else "FAIL"
        gate_payload = {
            "status": status,
            "run_id": run_id,
            "gate_version": GATE_VERSION,
            "core_a_denominator": CORE_COUNT,
            "core_label_counts": {label: core_labels[label] for label in sorted(ALLOWED_LABELS)},
            "false_merge_count": false_merge_count,
            "hard_conflict_violation_count": hard_conflict_violation_count,
            "unreviewable_count": unreviewable_count,
            "exact_identity_precision": precision,
            "clopper_pearson_95": {"low": cp_low, "high": cp_high},
            "establishes_99_percent_precision": False,
            "authorizes_auto_merge": False,
            "human_approval_required": True,
        }
        report_payload = {
            "status": status,
            "run_id": run_id,
            "review_completion": {
                "r1_fresh": f"{FRESH_REVIEW_COUNT}/{FRESH_REVIEW_COUNT}",
                "r2_fresh": f"{FRESH_REVIEW_COUNT}/{FRESH_REVIEW_COUNT}",
                "fresh_adjudication": f"{FRESH_REVIEW_COUNT}/{FRESH_REVIEW_COUNT}",
                "carry_provenance": "5/5",
            },
            "core_gate": gate_payload,
            "diagnostic_b": diagnostic,
            "disagreements": int(
                database.execute(
                    "SELECT count(*) FROM adjudications WHERE run_id=? AND reviewer_agreement=0",
                    (run_id,),
                ).fetchone()[0]
            ),
            "unresolved_disagreements": int(
                database.execute(
                    "SELECT count(*) FROM adjudications WHERE run_id=? AND unresolved_disagreement=1",
                    (run_id,),
                ).fetchone()[0]
            ),
        }
        _insert_metric(database, run_id, "gate", "core_a", gate_payload)
        _insert_metric(database, run_id, "diagnostic", "b_fresh_and_carry", diagnostic)
        _insert_metric(
            database,
            run_id,
            "completion",
            "review_rows",
            {"r1": FRESH_REVIEW_COUNT, "r2": FRESH_REVIEW_COUNT, "adjudicated": FRESH_REVIEW_COUNT, "carry": 5},
        )
        database.execute(
            "UPDATE runs SET status=?,adjudication_status='complete',gate_status=?,completed_at=? WHERE run_id=?",
            (status.casefold(), status.casefold(), utc_now(), run_id),
        )
        _refresh_run_record(database, run_id)
        _insert_event(database, run_id, "unblinded", status.casefold(), gate_payload)
        if sha256_file(v3_path) != v3_before:
            raise IdentityN100Error("v3 source drift during finalization")
        database.commit()

        _atomic_json(gate_output, gate_payload)
        _atomic_json(report_output, report_payload)
        manifest_payload = {
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
                "path": _path_text(root, artifact),
                "size_bytes": artifact.stat().st_size,
                "sha256": sha256_file(artifact),
            },
            "v3_immutable": {"path": _path_text(root, v3_path), "sha256_before": v3_before, "sha256_after": v3_before},
            "sample_logical_sha256": str(run["sample_logical_sha256"]),
            "cache_pins": cache_rows,
            "artifacts": {
                "locked_adjudication": {"path": _path_text(root, locked), "sha256": sha256_file(locked)},
                "gate": {"path": _path_text(root, gate_output), "sha256": sha256_file(gate_output)},
                "report": {"path": _path_text(root, report_output), "sha256": sha256_file(report_output)},
            },
            "review_rows": {"r1": FRESH_REVIEW_COUNT, "r2": FRESH_REVIEW_COUNT, "adjudicated": FRESH_REVIEW_COUNT, "carry": 5},
            "network_requests": int(run["wire_http_requests"]),
            "vision_requests": 0,
            "llm_requests": 0,
            "merge_executions": 0,
            "vnext_id_issuances": 0,
            "human_approval_required": True,
        }
        _atomic_json(manifest_output, manifest_payload)
        return FinalizeResult(
            artifact,
            run_id,
            status,
            false_merge_count,
            hard_conflict_violation_count,
            unreviewable_count,
            precision,
            cp_low,
            cp_high,
        )
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


__all__ = [
    "BuildResult",
    "CacheSpec",
    "FinalizeResult",
    "IdentityN100Error",
    "PrepareResult",
    "build_sample",
    "clopper_pearson",
    "finalize_review",
    "hamilton_apportion",
    "hierarchical_hamilton",
    "lock_review",
    "prepare_review",
    "selected_specs_from_db",
    "unblind_reviews",
]
