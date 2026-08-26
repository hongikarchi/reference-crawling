"""WP2.5 deterministic band-precision measurement and dual-blind review.

The immutable WP2.4 v4 candidate database is the only candidate-evidence
source.  This module writes a separate no-clobber sidecar and never merges a
pair, issues an ID, or writes Neon/R2.  HTTP is confined to the bounded
``prepare_review`` phase; Vision/LLM APIs are never called.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import sqlite3
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from canonical import vnext_identity_review as review_support
from canonical.cross_source_image_selection import canonical_json, canonical_sha256
from canonical.cross_source_semantic_fetch import FetchFailure, FetchPayload, fetch_once
from canonical.divisare_vision_benchmark import decode_source, prepare_derivative
from canonical.image_fingerprint import FingerprintError, fingerprint_bytes


SCHEMA_VERSION = 1
APPLICATION_ID = 0x57503235  # WP25
RUNNER_VERSION = "vnext-identity-band-precision-v1.0.0"
SAMPLE_VERSION = "vnext-identity-band-precision-v1"
REVIEW_VERSION = "vnext-identity-band-precision-dual-blind-v1"
ADJUDICATION_VERSION = "vnext-identity-band-precision-adjudication-v1"
GATE_VERSION = "vnext-identity-band-precision-predeclared-gate-v1"
SEED = "vnext-wp25-band-precision-v1-20260826"

STRATUM_QUOTAS: Mapping[str, int] = {"S1": 60, "S2": 60, "S3": 40, "S4": 25, "S5": 36}
EXPECTED_FRAMES: Mapping[str, Mapping[str, int]] = {
    "S1": {"raw": 2402, "flagged": 56, "ex_related": 97, "prior": 46, "eligible": 2203},
    "S2": {"raw": 1799, "flagged": 281, "ex_related": 0, "prior": 33, "eligible": 1485},
    "S3": {"raw": 899, "flagged": 417, "ex_related": 0, "prior": 18, "eligible": 464},
    "S4": {"raw": 119, "flagged": 21, "ex_related": 0, "prior": 1, "eligible": 97},
    "S5": {"raw": 36, "flagged": 0, "ex_related": 0, "prior": 0, "eligible": 36},
}
TOTAL_COUNT = sum(STRATUM_QUOTAS.values())
MAX_ASSETS_PER_SIDE = 2
MAX_ASSET_OCCURRENCES = TOTAL_COUNT * 2 * MAX_ASSETS_PER_SIDE
MAX_LOGICAL_HTTP_FETCHES = 884
MAX_WIRE_HTTP_REQUESTS = 884
MAX_WORKERS = 4

QUOTA_PHASES: tuple[str, ...] = (
    "pre_freeze",
    "pre_prepare",
    "pre_r1_review",
    "pre_r2_review",
    "pre_adjudication_review",
    "pre_finalize",
)
QUOTA_PHASE_INDEX: Mapping[str, int] = {
    phase: index for index, phase in enumerate(QUOTA_PHASES)
}
QUOTA_PHASE_RUN_STATUSES: Mapping[str, frozenset[str]] = {
    "pre_prepare": frozenset({"sample_frozen", "fetching", "review_ready"}),
    "pre_r1_review": frozenset({"review_ready"}),
    "pre_r2_review": frozenset({"reviews_partial"}),
    "pre_adjudication_review": frozenset({"reviews_locked"}),
    "pre_finalize": frozenset({"unblinded"}),
}
MAX_QUOTA_OBSERVATION_AGE = timedelta(hours=24)
MAX_QUOTA_FUTURE_SKEW = timedelta(minutes=5)

ALLOWED_LABELS = frozenset({"same", "related", "different", "insufficient"})
EXCLUSION_FLAGS = frozenset(
    {"fanout_review_required", "phash_veto_review_required", "anchor_missing"}
)
READABLE_ASSET_STATUSES = frozenset(
    {"cache_hit", "exact", "delivery_changed", "source_changed"}
)

DEFAULT_V4 = Path("data/enrichment/vnext_identity_candidates_v4.db")
DEFAULT_OUTPUT = Path("data/enrichment/vnext_identity_band_precision_v1.db")
DEFAULT_OUTPUT_CACHE = Path("data/enrichment/vnext_identity_band_precision_v1_cache")
DEFAULT_SAMPLE_MANIFEST = Path("data/reports/vnext_identity_band_precision_v1_sample_manifest.json")
DEFAULT_FETCH_MANIFEST = Path("data/reports/vnext_identity_band_precision_v1_fetch_manifest.json")
DEFAULT_FETCH_REPORT = Path("data/reports/vnext_identity_band_precision_v1_fetch_report.json")
DEFAULT_SHEETS_ROOT = Path("data/reports/vnext_identity_band_precision_v1_blind")
DEFAULT_R1_FORM = Path("data/reports/vnext_identity_band_precision_v1_reviewer_r1_draft.json")
DEFAULT_R2_FORM = Path("data/reports/vnext_identity_band_precision_v1_reviewer_r2_draft.json")
DEFAULT_R1_LOCKED = Path("data/reports/vnext_identity_band_precision_v1_reviewer_r1_locked.json")
DEFAULT_R2_LOCKED = Path("data/reports/vnext_identity_band_precision_v1_reviewer_r2_locked.json")
DEFAULT_ADJUDICATION_PACKET = Path("data/reports/vnext_identity_band_precision_v1_adjudication_packet.json")
DEFAULT_ADJUDICATION_FORM = Path("data/reports/vnext_identity_band_precision_v1_adjudication_draft.json")
DEFAULT_ADJUDICATION_LOCKED = Path("data/reports/vnext_identity_band_precision_v1_adjudication_locked.json")
DEFAULT_REPORT = Path("data/reports/vnext_identity_band_precision_v1_report.json")
DEFAULT_MANIFEST = Path("data/reports/vnext_identity_band_precision_v1_manifest.json")
DEFAULT_PROTOCOL = Path("docs/vnext/IDENTITY_BAND_PRECISION_PROTOCOL.md")
DEFAULT_CONTRACT = Path("docs/vnext/CONTRACT_IDENTITY.md")
DEFAULT_N100_PROTOCOL = Path("docs/vnext/IDENTITY_N100_PROTOCOL.md")
DEFAULT_ORDER = Path(".claude/ops/jobs/20260826_vnext_wp25_band_precision_order.md")
DEFAULT_V4_MANIFEST = Path("data/reports/vnext_identity_candidates_v4_manifest.json")
DEFAULT_V4_GATE = Path("data/reports/vnext_identity_candidates_v4_regression_gate.json")

V4_SPEC = (792_211_456, "5ab133138c26ca6d6df44a0eeec46ad413f3deb06dd2c75b3f9f799f3752d713")
V4_MANIFEST_SPEC = (140_345, "6fd7b5077803666e25fd28e4f44d8e62d4680db3ed5cbf43f319c5c5e7a3881d")
V4_GATE_SPEC = (3_676, "d8e37578b94cfef0891e2984b6fc781006ce552b46df6c361d3820fbb0f03242")
WP25_PROTOCOL_SPEC = (2_953, "88affb75d2f254c9c6eb8b5d3b708580100412caa80b069f6ae7716488b1120c")
IDENTITY_CONTRACT_SPEC = (9_879, "502e6258b9c7803ea58bc415ddbe73fbcc50faa0005d8b9aaaaaeab04838e3e8")
N100_PROTOCOL_SPEC = (2_735, "30e6c981ab257b39438769a49e4865309bb958b9db8fcdbf46530bf4d7390f1f")
WP25_ORDER_SPEC = (3_517, "4f4eab39bb9b08746ad8200418d48cc734a9b647456e2e7e810bb335aa684567")
PRIOR_REVIEW_SPECS: Mapping[str, tuple[Path, int, str]] = {
    "wp2_v1": (Path("data/enrichment/vnext_identity_candidates_v1.db"), 511_156_224, "0541b4c86bb2def88bbadaf53eec33c62d8f14e62800f1ceb8d96761f3aea5c4"),
    "wp21_v2": (Path("data/enrichment/vnext_identity_candidates_v2.db"), 581_292_032, "08e28e4e5a1ab5aba9ea5ddd53ca615f417458a6693cdfb1aefc822d57b5b0c4"),
    "wp22_v3": (Path("data/enrichment/vnext_identity_candidates_v3.db"), 580_710_400, "c71934cfd30807e523842133ffeb7e47d5a863d539feecd763d1086fd65d7a2f"),
    "wp23_n100": (Path("data/enrichment/vnext_identity_n100_v1.db"), 2_183_168, "ee62c63ebfbd118823caa2a99becd02ca974f73b453d743106bea82e21828b7b"),
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
    CacheSpec("wp1_p4b", Path("data/enrichment/vnext_p4b_pixel_cache_v1"), review_support.WP1_TRANSFORM_VERSION, 151, 26_909_641, "9bdc3b9b8b37808866d321420fc283a9270db4be021d561bfd756ed4f14983f6"),
    CacheSpec("wp2_v1_v2_review", Path("data/enrichment/vnext_identity_review_cache_v1"), review_support.TRANSFORM_VERSION, 73, 13_522_544, "b1356938dbbb4a011963cf78e3a3903e9e2ac4ba5d6f2616033f996353b65aa7"),
    CacheSpec("wp22_v3_review", Path("data/enrichment/vnext_identity_review_cache_v3"), review_support.TRANSFORM_VERSION, 35, 4_686_994, "4c99646c1ba084af1a54d44ee434e9b60997b8a78600d51e9b92193d159fe323"),
    CacheSpec("wp23_n100_review", Path("data/enrichment/vnext_identity_n100_cache_v1"), review_support.TRANSFORM_VERSION, 417, 79_402_638, "742bac35d70be5eaacc2895b0818c914f86eb9295d6042af8fa6f074d85f78b2"),
)


SCHEMA_SQL = """
CREATE TABLE runs(
 run_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL, runner_version TEXT NOT NULL,
 sample_version TEXT NOT NULL, review_version TEXT NOT NULL, adjudication_version TEXT NOT NULL,
 gate_version TEXT NOT NULL, seed TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('building','sample_frozen','fetching','review_ready','reviews_partial','reviews_locked','unblinded','pass','fail','failed')),
 sample_count INTEGER NOT NULL DEFAULT 0, asset_occurrence_count INTEGER NOT NULL DEFAULT 0,
 unique_asset_count INTEGER NOT NULL DEFAULT 0, logical_http_fetches INTEGER NOT NULL DEFAULT 0,
 wire_http_requests INTEGER NOT NULL DEFAULT 0, r1_status TEXT NOT NULL DEFAULT 'pending',
 r2_status TEXT NOT NULL DEFAULT 'pending', unblind_status TEXT NOT NULL DEFAULT 'pending',
 adjudication_status TEXT NOT NULL DEFAULT 'pending', gate_status TEXT NOT NULL DEFAULT 'pending',
 sample_logical_sha256 TEXT, created_at TEXT NOT NULL, completed_at TEXT,
 record_sha256 TEXT NOT NULL
);
CREATE TABLE input_pins(
 run_id TEXT NOT NULL REFERENCES runs(run_id), role TEXT NOT NULL, path TEXT NOT NULL,
 size_bytes INTEGER NOT NULL, sha256_before TEXT NOT NULL, sha256_after TEXT,
 usage TEXT NOT NULL, record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,role)
);
CREATE TABLE cache_pins(
 run_id TEXT NOT NULL REFERENCES runs(run_id), role TEXT NOT NULL, path TEXT NOT NULL,
 transform_version TEXT NOT NULL, access_mode TEXT NOT NULL CHECK(access_mode IN ('read_only','write_only')),
 file_count_before INTEGER NOT NULL, total_bytes_before INTEGER NOT NULL, tree_sha256_before TEXT NOT NULL,
 file_count_after INTEGER, total_bytes_after INTEGER, tree_sha256_after TEXT,
 record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,role)
);
CREATE TABLE population_frames(
 run_id TEXT NOT NULL REFERENCES runs(run_id), stratum TEXT NOT NULL,
 raw_count INTEGER NOT NULL, flagged_count INTEGER NOT NULL, ex_related_count INTEGER NOT NULL,
 prior_review_count INTEGER NOT NULL, eligible_count INTEGER NOT NULL, quota INTEGER NOT NULL,
 predicate_version TEXT NOT NULL, record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,stratum)
);
CREATE TABLE quota_observations(
 run_id TEXT NOT NULL REFERENCES runs(run_id), observation_no INTEGER NOT NULL,
 phase TEXT NOT NULL, account_index INTEGER NOT NULL, observed_at TEXT NOT NULL,
 signals_json TEXT NOT NULL, gate_basis_remaining_percent REAL NOT NULL,
 start_allowed INTEGER NOT NULL CHECK(start_allowed IN (0,1)),
 stop_required INTEGER NOT NULL CHECK(stop_required IN (0,1)),
 limitation TEXT NOT NULL, record_sha256 TEXT NOT NULL,
 PRIMARY KEY(run_id,observation_no)
);
CREATE TABLE artifact_paths(
 run_id TEXT NOT NULL REFERENCES runs(run_id), role TEXT NOT NULL, path TEXT NOT NULL,
 stage TEXT NOT NULL, record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,role)
);
CREATE TABLE artifact_bindings(
 run_id TEXT NOT NULL REFERENCES runs(run_id), role TEXT NOT NULL, path TEXT NOT NULL,
 size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, stage TEXT NOT NULL,
 record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,role)
);
CREATE TABLE final_payloads(
 run_id TEXT PRIMARY KEY REFERENCES runs(run_id), status TEXT NOT NULL,
 locked_adjudication_json TEXT NOT NULL, report_json TEXT NOT NULL,
 record_sha256 TEXT NOT NULL
);
CREATE TABLE excluded_pairs(
 run_id TEXT NOT NULL REFERENCES runs(run_id), candidate_id TEXT NOT NULL,
 stratum TEXT NOT NULL, exclusion_type TEXT NOT NULL, detail_json TEXT NOT NULL,
 record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,candidate_id,stratum,exclusion_type)
);
CREATE TABLE sample_pairs(
 run_id TEXT NOT NULL REFERENCES runs(run_id), sample_rank INTEGER NOT NULL,
 sample_id TEXT NOT NULL, stratum TEXT NOT NULL CHECK(stratum IN ('S1','S2','S3','S4','S5')),
 candidate_id TEXT NOT NULL, seed_rank_sha256 TEXT NOT NULL, record_sha256 TEXT NOT NULL,
 PRIMARY KEY(run_id,sample_rank), UNIQUE(run_id,sample_id), UNIQUE(run_id,candidate_id)
);
CREATE TABLE candidate_snapshots(
 run_id TEXT NOT NULL REFERENCES runs(run_id), sample_id TEXT NOT NULL,
 candidate_json TEXT NOT NULL, left_metadata_json TEXT NOT NULL, right_metadata_json TEXT NOT NULL,
 origins_json TEXT NOT NULL, families_json TEXT NOT NULL, conflicts_json TEXT NOT NULL,
 policy_flags_json TEXT NOT NULL, transition_json TEXT NOT NULL,
 snapshot_sha256 TEXT NOT NULL, record_sha256 TEXT NOT NULL,
 PRIMARY KEY(run_id,sample_id), FOREIGN KEY(run_id,sample_id) REFERENCES sample_pairs(run_id,sample_id)
);
CREATE TABLE sample_assets(
 run_id TEXT NOT NULL REFERENCES runs(run_id), sample_id TEXT NOT NULL,
 side TEXT NOT NULL CHECK(side IN ('left','right')), asset_order INTEGER NOT NULL,
 asset_key TEXT NOT NULL, source TEXT NOT NULL, source_building_id TEXT NOT NULL,
 source_asset_id TEXT NOT NULL, selection_reason TEXT NOT NULL, record_sha256 TEXT NOT NULL,
 PRIMARY KEY(run_id,sample_id,side,asset_order)
);
CREATE TABLE fetch_assets(
 run_id TEXT NOT NULL REFERENCES runs(run_id), asset_key TEXT NOT NULL, source TEXT NOT NULL,
 source_asset_id TEXT NOT NULL, expected_url TEXT, expected_raw_sha256 TEXT,
 expected_pixel_sha256 TEXT, status TEXT NOT NULL, cache_role TEXT, cache_path TEXT,
 actual_final_url TEXT, actual_raw_sha256 TEXT, actual_pixel_sha256 TEXT,
 http_status INTEGER, response_bytes INTEGER, request_count INTEGER NOT NULL DEFAULT 0,
 error_kind TEXT, error_message TEXT, record_sha256 TEXT NOT NULL,
 PRIMARY KEY(run_id,asset_key), UNIQUE(run_id,source,source_asset_id)
);
CREATE TABLE fetch_attempts(
 run_id TEXT NOT NULL REFERENCES runs(run_id), asset_key TEXT NOT NULL,
 attempt_no INTEGER NOT NULL CHECK(attempt_no=1), status TEXT NOT NULL,
 request_count INTEGER NOT NULL, redirect_chain_json TEXT NOT NULL,
 started_at TEXT NOT NULL, completed_at TEXT NOT NULL, error_kind TEXT,
 record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,asset_key,attempt_no)
);
CREATE TABLE review_items(
 run_id TEXT NOT NULL REFERENCES runs(run_id), lane TEXT NOT NULL CHECK(lane IN ('r1','r2')),
 display_order INTEGER NOT NULL, opaque_review_id TEXT NOT NULL, sample_id TEXT NOT NULL,
 sheet_path TEXT NOT NULL, sheet_sha256 TEXT NOT NULL, sheet_bytes INTEGER NOT NULL,
 record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,lane,opaque_review_id),
 UNIQUE(run_id,lane,display_order), UNIQUE(run_id,lane,sample_id)
);
CREATE TABLE blind_reviews(
 run_id TEXT NOT NULL REFERENCES runs(run_id), lane TEXT NOT NULL,
 opaque_review_id TEXT NOT NULL, sample_id TEXT NOT NULL, label TEXT NOT NULL,
 rationale TEXT NOT NULL, hard_conflict_observed TEXT NOT NULL, reviewer TEXT NOT NULL,
 reviewed_at TEXT NOT NULL, record_sha256 TEXT NOT NULL,
 PRIMARY KEY(run_id,lane,opaque_review_id)
);
CREATE TABLE review_locks(
 run_id TEXT NOT NULL REFERENCES runs(run_id), lane TEXT NOT NULL, reviewer TEXT NOT NULL,
 row_count INTEGER NOT NULL, completed_form_path TEXT NOT NULL, completed_form_sha256 TEXT NOT NULL,
 locked_output_path TEXT NOT NULL, locked_output_sha256 TEXT NOT NULL,
 ordered_ledger_sha256 TEXT NOT NULL, locked_at TEXT NOT NULL, record_sha256 TEXT NOT NULL,
 PRIMARY KEY(run_id,lane)
);
CREATE TABLE review_lock_payloads(
 run_id TEXT NOT NULL REFERENCES runs(run_id), lane TEXT NOT NULL,
 payload_json TEXT NOT NULL, record_sha256 TEXT NOT NULL,
 PRIMARY KEY(run_id,lane)
);
CREATE TABLE unblind_artifacts(
 run_id TEXT PRIMARY KEY REFERENCES runs(run_id), packet_path TEXT NOT NULL,
 packet_sha256 TEXT NOT NULL, form_path TEXT NOT NULL, form_sha256 TEXT NOT NULL,
 r1_ledger_sha256 TEXT NOT NULL, r2_ledger_sha256 TEXT NOT NULL,
 created_at TEXT NOT NULL, record_sha256 TEXT NOT NULL
);
CREATE TABLE unblind_payloads(
 run_id TEXT PRIMARY KEY REFERENCES runs(run_id), packet_json TEXT NOT NULL,
 form_json TEXT NOT NULL, record_sha256 TEXT NOT NULL
);
CREATE TABLE adjudications(
 run_id TEXT NOT NULL REFERENCES runs(run_id), sample_id TEXT NOT NULL,
 r1_label TEXT NOT NULL, r2_label TEXT NOT NULL, reviewer_agreement INTEGER NOT NULL,
 adjudicator_label TEXT NOT NULL, final_gold_label TEXT NOT NULL,
 unresolved_disagreement INTEGER NOT NULL CHECK(unresolved_disagreement IN (0,1)),
 rationale TEXT NOT NULL, adjudicator TEXT NOT NULL, adjudicated_at TEXT NOT NULL,
 record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,sample_id)
);
CREATE TABLE band_metrics(
 run_id TEXT NOT NULL REFERENCES runs(run_id), stratum TEXT NOT NULL,
 sample_n INTEGER NOT NULL, same_count INTEGER NOT NULL, related_count INTEGER NOT NULL,
 different_count INTEGER NOT NULL, insufficient_count INTEGER NOT NULL,
 precision REAL NOT NULL, cp95_low REAL NOT NULL, cp95_high REAL NOT NULL,
 error_count INTEGER NOT NULL, equivalent_errors_60 REAL NOT NULL,
 candidate_grade TEXT NOT NULL, bulk_excluded INTEGER NOT NULL CHECK(bulk_excluded IN (0,1)),
 status TEXT NOT NULL, record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,stratum)
);
CREATE TABLE state_events(
 run_id TEXT NOT NULL REFERENCES runs(run_id), event_no INTEGER NOT NULL,
 from_status TEXT NOT NULL, to_status TEXT NOT NULL, detail_json TEXT NOT NULL,
 occurred_at TEXT NOT NULL, record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,event_no)
);
CREATE TRIGGER sample_no_update BEFORE UPDATE ON sample_pairs BEGIN SELECT RAISE(ABORT,'sample is frozen'); END;
CREATE TRIGGER sample_no_delete BEFORE DELETE ON sample_pairs BEGIN SELECT RAISE(ABORT,'sample is frozen'); END;
CREATE TRIGGER sample_no_late_insert BEFORE INSERT ON sample_pairs
 WHEN (SELECT status FROM runs WHERE run_id=NEW.run_id)<>'building' BEGIN SELECT RAISE(ABORT,'sample is frozen'); END;
CREATE TRIGGER snapshot_no_update BEFORE UPDATE ON candidate_snapshots BEGIN SELECT RAISE(ABORT,'snapshot is frozen'); END;
CREATE TRIGGER snapshot_no_delete BEFORE DELETE ON candidate_snapshots BEGIN SELECT RAISE(ABORT,'snapshot is frozen'); END;
CREATE TRIGGER snapshot_no_late_insert BEFORE INSERT ON candidate_snapshots
 WHEN (SELECT status FROM runs WHERE run_id=NEW.run_id)<>'building' BEGIN SELECT RAISE(ABORT,'snapshot is frozen'); END;
CREATE TRIGGER population_no_update BEFORE UPDATE ON population_frames BEGIN SELECT RAISE(ABORT,'population is frozen'); END;
CREATE TRIGGER population_no_delete BEFORE DELETE ON population_frames BEGIN SELECT RAISE(ABORT,'population is frozen'); END;
CREATE TRIGGER population_no_late_insert BEFORE INSERT ON population_frames
 WHEN (SELECT status FROM runs WHERE run_id=NEW.run_id)<>'building' BEGIN SELECT RAISE(ABORT,'population is frozen'); END;
CREATE TRIGGER exclusion_no_update BEFORE UPDATE ON excluded_pairs BEGIN SELECT RAISE(ABORT,'exclusions are frozen'); END;
CREATE TRIGGER exclusion_no_delete BEFORE DELETE ON excluded_pairs BEGIN SELECT RAISE(ABORT,'exclusions are frozen'); END;
CREATE TRIGGER exclusion_no_late_insert BEFORE INSERT ON excluded_pairs
 WHEN (SELECT status FROM runs WHERE run_id=NEW.run_id)<>'building' BEGIN SELECT RAISE(ABORT,'exclusions are frozen'); END;
CREATE TRIGGER quota_no_update BEFORE UPDATE ON quota_observations BEGIN SELECT RAISE(ABORT,'quota ledger is append-only'); END;
CREATE TRIGGER quota_no_delete BEFORE DELETE ON quota_observations BEGIN SELECT RAISE(ABORT,'quota ledger is append-only'); END;
CREATE TRIGGER input_pins_update_only_building BEFORE UPDATE ON input_pins
 WHEN (SELECT status FROM runs WHERE run_id=OLD.run_id)<>'building'
 BEGIN SELECT RAISE(ABORT,'input pins are frozen'); END;
CREATE TRIGGER input_pins_no_delete BEFORE DELETE ON input_pins BEGIN SELECT RAISE(ABORT,'input pins are frozen'); END;
CREATE TRIGGER cache_pins_update_only_fetching BEFORE UPDATE ON cache_pins
 WHEN (SELECT status FROM runs WHERE run_id=OLD.run_id)<>'fetching'
 BEGIN SELECT RAISE(ABORT,'cache pins are frozen'); END;
CREATE TRIGGER cache_pins_no_delete BEFORE DELETE ON cache_pins BEGIN SELECT RAISE(ABORT,'cache pins are frozen'); END;
CREATE TRIGGER artifact_paths_no_update BEFORE UPDATE ON artifact_paths BEGIN SELECT RAISE(ABORT,'artifact paths are frozen'); END;
CREATE TRIGGER artifact_paths_no_delete BEFORE DELETE ON artifact_paths BEGIN SELECT RAISE(ABORT,'artifact paths are frozen'); END;
CREATE TRIGGER artifact_paths_no_terminal_insert BEFORE INSERT ON artifact_paths
 WHEN (SELECT status FROM runs WHERE run_id=NEW.run_id) IN ('pass','fail','failed')
 BEGIN SELECT RAISE(ABORT,'artifact paths are terminal'); END;
CREATE TRIGGER artifact_bindings_no_update BEFORE UPDATE ON artifact_bindings BEGIN SELECT RAISE(ABORT,'artifact bindings are frozen'); END;
CREATE TRIGGER artifact_bindings_no_delete BEFORE DELETE ON artifact_bindings BEGIN SELECT RAISE(ABORT,'artifact bindings are frozen'); END;
CREATE TRIGGER artifact_bindings_no_terminal_insert BEFORE INSERT ON artifact_bindings
 WHEN (SELECT status FROM runs WHERE run_id=NEW.run_id) IN ('pass','fail','failed')
 BEGIN SELECT RAISE(ABORT,'artifact bindings are terminal'); END;
CREATE TRIGGER final_payloads_no_update BEFORE UPDATE ON final_payloads BEGIN SELECT RAISE(ABORT,'final payload is frozen'); END;
CREATE TRIGGER final_payloads_no_delete BEFORE DELETE ON final_payloads BEGIN SELECT RAISE(ABORT,'final payload is frozen'); END;
CREATE TRIGGER final_payloads_only_unblinded BEFORE INSERT ON final_payloads
 WHEN (SELECT status FROM runs WHERE run_id=NEW.run_id)<>'unblinded'
 BEGIN SELECT RAISE(ABORT,'final payload state invalid'); END;
CREATE TRIGGER review_items_no_update BEFORE UPDATE ON review_items BEGIN SELECT RAISE(ABORT,'review items are frozen'); END;
CREATE TRIGGER review_items_no_delete BEFORE DELETE ON review_items BEGIN SELECT RAISE(ABORT,'review items are frozen'); END;
CREATE TRIGGER review_items_only_fetching BEFORE INSERT ON review_items
 WHEN (SELECT status FROM runs WHERE run_id=NEW.run_id)<>'fetching' BEGIN SELECT RAISE(ABORT,'review items are frozen'); END;
CREATE TRIGGER sample_assets_no_change BEFORE UPDATE ON sample_assets
 WHEN (SELECT status FROM runs WHERE run_id=NEW.run_id)<>'fetching' BEGIN SELECT RAISE(ABORT,'sample assets are frozen'); END;
CREATE TRIGGER sample_assets_no_delete BEFORE DELETE ON sample_assets
 WHEN (SELECT status FROM runs WHERE run_id=OLD.run_id)<>'fetching' BEGIN SELECT RAISE(ABORT,'sample assets are frozen'); END;
CREATE TRIGGER sample_assets_only_fetching BEFORE INSERT ON sample_assets
 WHEN (SELECT status FROM runs WHERE run_id=NEW.run_id)<>'fetching' BEGIN SELECT RAISE(ABORT,'sample assets are frozen'); END;
CREATE TRIGGER fetch_assets_no_change BEFORE UPDATE ON fetch_assets
 WHEN (SELECT status FROM runs WHERE run_id=NEW.run_id)<>'fetching' BEGIN SELECT RAISE(ABORT,'fetch assets are frozen'); END;
CREATE TRIGGER fetch_assets_no_delete BEFORE DELETE ON fetch_assets
 WHEN (SELECT status FROM runs WHERE run_id=OLD.run_id)<>'fetching' BEGIN SELECT RAISE(ABORT,'fetch assets are frozen'); END;
CREATE TRIGGER fetch_assets_only_fetching BEFORE INSERT ON fetch_assets
 WHEN (SELECT status FROM runs WHERE run_id=NEW.run_id)<>'fetching' BEGIN SELECT RAISE(ABORT,'fetch assets are frozen'); END;
CREATE TRIGGER fetch_attempts_update_guard BEFORE UPDATE ON fetch_attempts
 WHEN OLD.status<>'reserved' OR NEW.status='reserved'
   OR NEW.run_id<>OLD.run_id OR NEW.asset_key<>OLD.asset_key
   OR NEW.attempt_no<>OLD.attempt_no OR NEW.started_at<>OLD.started_at
 BEGIN SELECT RAISE(ABORT,'fetch attempt transition is invalid'); END;
CREATE TRIGGER fetch_attempts_no_delete BEFORE DELETE ON fetch_attempts BEGIN SELECT RAISE(ABORT,'fetch attempts are append-only'); END;
CREATE TRIGGER fetch_attempts_only_fetching BEFORE INSERT ON fetch_attempts
 WHEN (SELECT status FROM runs WHERE run_id=NEW.run_id)<>'fetching' BEGIN SELECT RAISE(ABORT,'fetch attempts are frozen'); END;
CREATE TRIGGER review_no_change_after_lock BEFORE UPDATE ON blind_reviews
 WHEN EXISTS(SELECT 1 FROM review_locks WHERE run_id=OLD.run_id AND lane=OLD.lane)
 BEGIN SELECT RAISE(ABORT,'review lane is locked'); END;
CREATE TRIGGER review_no_delete_after_lock BEFORE DELETE ON blind_reviews
 WHEN EXISTS(SELECT 1 FROM review_locks WHERE run_id=OLD.run_id AND lane=OLD.lane)
 BEGIN SELECT RAISE(ABORT,'review lane is locked'); END;
CREATE TRIGGER review_no_insert_after_lock BEFORE INSERT ON blind_reviews
 WHEN EXISTS(SELECT 1 FROM review_locks WHERE run_id=NEW.run_id AND lane=NEW.lane)
 BEGIN SELECT RAISE(ABORT,'review lane is locked'); END;
CREATE TRIGGER review_locks_no_update BEFORE UPDATE ON review_locks BEGIN SELECT RAISE(ABORT,'review locks are frozen'); END;
CREATE TRIGGER review_locks_no_delete BEFORE DELETE ON review_locks BEGIN SELECT RAISE(ABORT,'review locks are frozen'); END;
CREATE TRIGGER review_locks_only_reviewing BEFORE INSERT ON review_locks
 WHEN (SELECT status FROM runs WHERE run_id=NEW.run_id) NOT IN ('review_ready','reviews_partial')
 BEGIN SELECT RAISE(ABORT,'review lock state invalid'); END;
CREATE TRIGGER review_lock_payloads_no_update BEFORE UPDATE ON review_lock_payloads BEGIN SELECT RAISE(ABORT,'review lock payload is frozen'); END;
CREATE TRIGGER review_lock_payloads_no_delete BEFORE DELETE ON review_lock_payloads BEGIN SELECT RAISE(ABORT,'review lock payload is frozen'); END;
CREATE TRIGGER review_lock_payloads_only_reviewing BEFORE INSERT ON review_lock_payloads
 WHEN (SELECT status FROM runs WHERE run_id=NEW.run_id) NOT IN ('review_ready','reviews_partial')
 BEGIN SELECT RAISE(ABORT,'review lock payload state invalid'); END;
CREATE TRIGGER unblind_no_update BEFORE UPDATE ON unblind_artifacts BEGIN SELECT RAISE(ABORT,'unblind artifact is frozen'); END;
CREATE TRIGGER unblind_no_delete BEFORE DELETE ON unblind_artifacts BEGIN SELECT RAISE(ABORT,'unblind artifact is frozen'); END;
CREATE TRIGGER unblind_only_reviews_locked BEFORE INSERT ON unblind_artifacts
 WHEN (SELECT status FROM runs WHERE run_id=NEW.run_id)<>'reviews_locked'
 BEGIN SELECT RAISE(ABORT,'unblind artifact state invalid'); END;
CREATE TRIGGER unblind_payloads_no_update BEFORE UPDATE ON unblind_payloads BEGIN SELECT RAISE(ABORT,'unblind payload is frozen'); END;
CREATE TRIGGER unblind_payloads_no_delete BEFORE DELETE ON unblind_payloads BEGIN SELECT RAISE(ABORT,'unblind payload is frozen'); END;
CREATE TRIGGER unblind_payloads_only_reviews_locked BEFORE INSERT ON unblind_payloads
 WHEN (SELECT status FROM runs WHERE run_id=NEW.run_id)<>'reviews_locked'
 BEGIN SELECT RAISE(ABORT,'unblind payload state invalid'); END;
CREATE TRIGGER adjudication_no_update BEFORE UPDATE ON adjudications BEGIN SELECT RAISE(ABORT,'adjudication is frozen'); END;
CREATE TRIGGER adjudication_no_delete BEFORE DELETE ON adjudications BEGIN SELECT RAISE(ABORT,'adjudication is frozen'); END;
CREATE TRIGGER adjudication_only_unblinded BEFORE INSERT ON adjudications
 WHEN (SELECT status FROM runs WHERE run_id=NEW.run_id)<>'unblinded' BEGIN SELECT RAISE(ABORT,'adjudication state invalid'); END;
CREATE TRIGGER band_metrics_no_update BEFORE UPDATE ON band_metrics BEGIN SELECT RAISE(ABORT,'band metrics are frozen'); END;
CREATE TRIGGER band_metrics_no_delete BEFORE DELETE ON band_metrics BEGIN SELECT RAISE(ABORT,'band metrics are frozen'); END;
CREATE TRIGGER band_metrics_only_unblinded BEFORE INSERT ON band_metrics
 WHEN (SELECT status FROM runs WHERE run_id=NEW.run_id)<>'unblinded' BEGIN SELECT RAISE(ABORT,'band metrics state invalid'); END;
CREATE TRIGGER event_no_update BEFORE UPDATE ON state_events BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
CREATE TRIGGER event_no_delete BEFORE DELETE ON state_events BEGIN SELECT RAISE(ABORT,'events are append-only'); END;
"""


class IdentityBandPrecisionError(RuntimeError):
    """Raised when a frozen WP2.5 contract invariant fails."""


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
    asset_occurrences: int
    unique_assets: int
    logical_http_fetches: int
    wire_http_requests: int
    cache_hits: int
    sheet_count: int


@dataclass(frozen=True)
class FinalizeResult:
    artifact_path: Path
    run_id: str
    status: str
    s5_same_count: int
    disagreements: int
    unresolved_disagreements: int


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _quota_time(value: Any, *, now: datetime | None = None,
                require_fresh: bool = True) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise IdentityBandPrecisionError("quota observed_at is required")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IdentityBandPrecisionError(
            "quota observed_at must be ISO8601 with a timezone"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise IdentityBandPrecisionError(
            "quota observed_at must be ISO8601 with a timezone"
        )
    observed = parsed.astimezone(timezone.utc)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if require_fresh:
        if observed > current + MAX_QUOTA_FUTURE_SKEW:
            raise IdentityBandPrecisionError("quota observation is implausibly in the future")
        if current - observed > MAX_QUOTA_OBSERVATION_AGE:
            raise IdentityBandPrecisionError("quota observation is stale")
    return observed


def _validate_quota_ledger(connection: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    rows = connection.execute(
        "SELECT * FROM quota_observations WHERE run_id=? ORDER BY observation_no", (run_id,)
    ).fetchall()
    previous_phase = -1
    previous_time: datetime | None = None
    for position, row in enumerate(rows, 1):
        phase = str(row["phase"])
        index = QUOTA_PHASE_INDEX.get(phase)
        if (index is None or (position == 1 and phase != "pre_freeze")
                or (position > 1 and phase == "pre_freeze")):
            raise IdentityBandPrecisionError("quota ledger contains an invalid phase")
        if index < previous_phase or index > previous_phase + 1:
            raise IdentityBandPrecisionError("quota phase order is invalid")
        observed = _quota_time(row["observed_at"], require_fresh=False)
        if previous_time is not None and observed < previous_time:
            raise IdentityBandPrecisionError("quota observation times are not monotonic")
        previous_phase, previous_time = index, observed
    return rows


def _validate_next_quota_observation(connection: sqlite3.Connection, run_id: str,
                                     phase: str, observed_at: str,
                                     run_status: str) -> datetime:
    if phase not in QUOTA_PHASE_INDEX or phase == "pre_freeze":
        raise IdentityBandPrecisionError("quota phase is not an allowed runtime phase")
    allowed_statuses = QUOTA_PHASE_RUN_STATUSES[phase]
    if run_status not in allowed_statuses:
        raise IdentityBandPrecisionError(
            f"quota phase {phase} is invalid while run status is {run_status}"
        )
    observed = _quota_time(observed_at)
    rows = _validate_quota_ledger(connection, run_id)
    if not rows:
        raise IdentityBandPrecisionError("quota ledger must start with pre_freeze")
    latest = rows[-1]
    latest_index = QUOTA_PHASE_INDEX[str(latest["phase"])]
    requested_index = QUOTA_PHASE_INDEX[phase]
    if requested_index not in {latest_index, latest_index + 1}:
        raise IdentityBandPrecisionError("quota phase is out of order")
    latest_time = _quota_time(latest["observed_at"], require_fresh=False)
    if observed < latest_time:
        raise IdentityBandPrecisionError("quota observation times are not monotonic")
    return observed


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
    try:
        return value.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return value.resolve().as_posix()


def _resolve_text(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def _assert_no_sidecars(path: Path) -> None:
    found = [str(Path(str(path) + suffix)) for suffix in ("-wal", "-shm", "-journal") if Path(str(path) + suffix).exists()]
    if found:
        raise IdentityBandPrecisionError("forbidden SQLite sidecar(s): " + ", ".join(found))


def _open_immutable(path: Path) -> sqlite3.Connection:
    _assert_no_sidecars(path)
    connection = sqlite3.connect("file:" + path.resolve().as_posix() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _atomic_bytes(path: Path, raw: bytes, *, allow_identical: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if allow_identical and path.is_file() and path.read_bytes() == raw:
            return
        raise IdentityBandPrecisionError(f"refusing to clobber artifact: {path}")
    temporary = path.with_name(path.name + ".building")
    if temporary.exists():
        raise IdentityBandPrecisionError(f"stale temporary artifact: {temporary}")
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


def _atomic_json(path: Path, payload: Mapping[str, Any], *, allow_identical: bool = False) -> None:
    _atomic_bytes(path, _json_bytes(payload), allow_identical=allow_identical)


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _pin_file(root: Path, path: Path, usage: str, expected: tuple[int, str] | None = None) -> dict[str, Any]:
    absolute = _path(root, path)
    if not absolute.is_file():
        raise IdentityBandPrecisionError(f"pinned input is missing: {absolute}")
    _assert_no_sidecars(absolute)
    size, digest = absolute.stat().st_size, sha256_file(absolute)
    if expected is not None and (size, digest) != expected:
        raise IdentityBandPrecisionError(f"input byte pin mismatch: {path}")
    return {"path": _path_text(root, absolute), "size_bytes": size, "sha256_before": digest, "sha256_after": None, "usage": usage}


def _seed_rank(stratum: str, candidate_id: str) -> str:
    raw = f"{SAMPLE_VERSION}\0{SEED}\0{stratum}\0{candidate_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _insert_event(connection: sqlite3.Connection, run_id: str, before: str, after: str, detail: Mapping[str, Any]) -> None:
    event_no = int(connection.execute("SELECT coalesce(max(event_no),0)+1 FROM state_events WHERE run_id=?", (run_id,)).fetchone()[0])
    at = utc_now()
    payload = {"event_no": event_no, "from_status": before, "to_status": after, "detail": dict(detail), "occurred_at": at}
    connection.execute("INSERT INTO state_events VALUES(?,?,?,?,?,?,?)", (run_id, event_no, before, after, canonical_json(dict(detail)), at, _record(payload)))


def _refresh_run_record(connection: sqlite3.Connection, run_id: str) -> None:
    row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise IdentityBandPrecisionError("run row is missing")
    connection.execute("UPDATE runs SET record_sha256=? WHERE run_id=?", (_record({key: row[key] for key in row.keys() if key != "record_sha256"}), run_id))


def _register_artifact_path(connection: sqlite3.Connection, root: Path, run_id: str,
                            role: str, path: Path, stage: str) -> None:
    payload = {"role": role, "path": _path_text(root, path), "stage": stage}
    existing = connection.execute("SELECT * FROM artifact_paths WHERE run_id=? AND role=?", (run_id, role)).fetchone()
    if existing is None:
        connection.execute("INSERT INTO artifact_paths VALUES(?,?,?,?,?)", (run_id, role, payload["path"], stage, _record(payload)))
    elif str(existing["path"]) != payload["path"] or str(existing["stage"]) != stage:
        raise IdentityBandPrecisionError(f"artifact path changed for role {role}")


def _bind_artifact(connection: sqlite3.Connection, root: Path, run_id: str,
                   role: str, path: Path, stage: str) -> dict[str, Any]:
    if not path.is_file():
        raise IdentityBandPrecisionError(f"artifact to bind is missing: {role}")
    payload = {"role": role, "path": _path_text(root, path), "size_bytes": path.stat().st_size, "sha256": sha256_file(path), "stage": stage}
    existing = connection.execute("SELECT * FROM artifact_bindings WHERE run_id=? AND role=?", (run_id, role)).fetchone()
    if existing is None:
        connection.execute("INSERT INTO artifact_bindings VALUES(?,?,?,?,?,?,?)", (run_id, role, payload["path"], payload["size_bytes"], payload["sha256"], stage, _record(payload)))
    elif any(existing[key] != payload[key] for key in ("path", "size_bytes", "sha256", "stage")):
        raise IdentityBandPrecisionError(f"artifact binding drift: {role}")
    return payload


def _bind_artifact_values(connection: sqlite3.Connection, root: Path, run_id: str,
                          role: str, path: Path, stage: str,
                          size_bytes: int, sha256: str) -> dict[str, Any]:
    payload = {"role": role, "path": _path_text(root, path), "size_bytes": int(size_bytes), "sha256": sha256, "stage": stage}
    existing = connection.execute("SELECT * FROM artifact_bindings WHERE run_id=? AND role=?", (run_id, role)).fetchone()
    if existing is None:
        connection.execute("INSERT INTO artifact_bindings VALUES(?,?,?,?,?,?,?)", (run_id, role, payload["path"], payload["size_bytes"], sha256, stage, _record(payload)))
    elif any(existing[key] != payload[key] for key in ("path", "size_bytes", "sha256", "stage")):
        raise IdentityBandPrecisionError(f"artifact binding drift: {role}")
    return payload


def _logical_sha(connection: sqlite3.Connection, run_id: str) -> str:
    digest = hashlib.sha256((SAMPLE_VERSION + "\n").encode("utf-8"))
    # Output locations are operational provenance, not sample identity.  Keeping
    # artifact_paths outside this digest makes the frozen sample independent of
    # its publication path and invariant as later phase paths are registered.
    for table, order in (("input_pins", "role"), ("quota_observations", "observation_no"), ("population_frames", "stratum"), ("excluded_pairs", "stratum,candidate_id,exclusion_type"), ("sample_pairs", "sample_rank"), ("candidate_snapshots", "sample_id")):
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


def _prior_reviewed(root: Path, *, verify: bool) -> tuple[set[str], dict[str, dict[str, Any]]]:
    reviewed: set[str] = set()
    pins: dict[str, dict[str, Any]] = {}
    for role, (relative, size, digest) in PRIOR_REVIEW_SPECS.items():
        pin = _pin_file(root, relative, "prior_review_disjointness", (size, digest) if verify else None)
        pins[role] = pin
        connection = _open_immutable(_path(root, relative))
        try:
            table = "sample_pairs" if role == "wp23_n100" else "sample_neighbourhoods"
            reviewed.update(str(row[0]) for row in connection.execute(f"SELECT candidate_id FROM {table}"))
        finally:
            connection.close()
    if len(reviewed) != 135:
        raise IdentityBandPrecisionError(f"expected 135 prior-reviewed unique pairs, found {len(reviewed)}")
    return reviewed, pins


def _candidate_context(connection: sqlite3.Connection, candidate_id: str) -> dict[str, Any]:
    pair = connection.execute("SELECT * FROM candidate_pairs WHERE candidate_id=?", (candidate_id,)).fetchone()
    if pair is None:
        raise IdentityBandPrecisionError(f"candidate disappeared: {candidate_id}")
    pair_dict = dict(pair)
    def metadata(side: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM source_buildings WHERE source=? AND source_building_id=?", (pair[f"{side}_source"], pair[f"{side}_source_building_id"])).fetchone()
        if row is None:
            raise IdentityBandPrecisionError(f"missing {side} source metadata: {candidate_id}")
        return dict(row)
    origins = [str(row[0]) for row in connection.execute("SELECT origin FROM candidate_origins WHERE candidate_id=? ORDER BY origin", (candidate_id,))]
    families = [dict(row) for row in connection.execute("SELECT family,status,score,evidence_json,record_sha256 FROM evidence_families WHERE candidate_id=? ORDER BY family", (candidate_id,))]
    conflicts = [dict(row) for row in connection.execute("SELECT conflict_type,detail_json,record_sha256 FROM hard_conflicts WHERE candidate_id=? ORDER BY conflict_type", (candidate_id,))]
    flags = [dict(row) for row in connection.execute("SELECT flag_type,detail_json,record_sha256 FROM candidate_policy_flags WHERE candidate_id=? ORDER BY flag_type", (candidate_id,))]
    transition = connection.execute("SELECT * FROM candidate_transitions WHERE candidate_id=?", (candidate_id,)).fetchone()
    return {"candidate": pair_dict, "left": metadata("left"), "right": metadata("right"), "origins": origins, "families": families, "conflicts": conflicts, "policy_flags": flags, "transition": dict(transition) if transition else {}}


def _frame_rows(connection: sqlite3.Connection, reviewed: set[str]) -> tuple[dict[str, list[str]], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    flag_map: dict[str, set[str]] = {}
    for row in connection.execute("SELECT candidate_id,flag_type FROM candidate_policy_flags"):
        flag_map.setdefault(str(row[0]), set()).add(str(row[1]))
    band_rows = {str(row[0]): (str(row[1]), str(row[2])) for row in connection.execute("SELECT q.candidate_id,q.evidence_strength_band,t.v3_proposal FROM human_review_queue q JOIN candidate_transitions t USING(run_id,candidate_id)")}
    s4_raw = {str(row[0]) for row in connection.execute("SELECT candidate_id FROM candidate_transitions WHERE v3_proposal='related_but_separate' AND v4_proposal='merge_candidate'")}
    s5_raw = {str(row[0]) for row in connection.execute("SELECT t.candidate_id FROM candidate_transitions t JOIN hard_conflicts h USING(run_id,candidate_id) WHERE t.v3_hard_conflicts_json='[]' AND t.v4_proposal='reject' AND t.r1_changed=1 AND h.conflict_type='project_phase_incompatible'")}
    raw: dict[str, set[str]] = {
        "S1": {cid for cid, (band, _) in band_rows.items() if band == "name_firm_image_3plus"},
        "S2": {cid for cid, (band, _) in band_rows.items() if band == "other_anchor_3plus"},
        "S3": {cid for cid, (band, _) in band_rows.items() if band in {"anchor_2", "no_anchor_2plus"}},
        "S4": s4_raw,
        "S5": s5_raw,
    }
    eligible: dict[str, list[str]] = {}
    metrics: dict[str, dict[str, Any]] = {}
    exclusions: list[dict[str, Any]] = []
    for stratum in STRATUM_QUOTAS:
        flagged = {cid for cid in raw[stratum] if flag_map.get(cid, set()) & EXCLUSION_FLAGS}
        ex_related = set()
        if stratum in {"S1", "S2"}:
            ex_related = {cid for cid in raw[stratum] if band_rows[cid][1] == "related_but_separate"}
        after_policy = raw[stratum] - flagged - ex_related
        prior = after_policy & reviewed
        frame = sorted(after_policy - reviewed)
        eligible[stratum] = frame
        metrics[stratum] = {"raw": len(raw[stratum]), "flagged": len(flagged), "ex_related": len(ex_related - flagged), "prior": len(prior), "eligible": len(frame)}
        for cid in sorted(flagged):
            exclusions.append({"candidate_id": cid, "stratum": stratum, "type": "policy_flag", "detail": sorted(flag_map[cid] & EXCLUSION_FLAGS)})
        for cid in sorted(ex_related - flagged):
            exclusions.append({"candidate_id": cid, "stratum": stratum, "type": "ex_related", "detail": ["v3_related_but_separate"]})
        for cid in sorted(prior):
            exclusions.append({"candidate_id": cid, "stratum": stratum, "type": "prior_review", "detail": ["prior_review_union_135"]})
    return eligible, metrics, exclusions


def build_sample(*, repo_root: Path, v4_path: Path = DEFAULT_V4, output_path: Path = DEFAULT_OUTPUT,
                 manifest_path: Path = DEFAULT_SAMPLE_MANIFEST, protocol_path: Path = DEFAULT_PROTOCOL,
                 contract_path: Path = DEFAULT_CONTRACT, n100_protocol_path: Path = DEFAULT_N100_PROTOCOL,
                 order_path: Path = DEFAULT_ORDER, quota_remaining_percent: float,
                 quota_observed_at: str, quota_source: str, quota_account_index: int,
                 quota_limitation: str, verify_pins: bool = True) -> BuildResult:
    """Freeze the 221-row S1--S5 sample with network=0."""
    root = repo_root.resolve()
    if not 0 <= quota_remaining_percent <= 100:
        raise IdentityBandPrecisionError("quota remaining percent must be in 0..100")
    if quota_remaining_percent <= 30:
        raise IdentityBandPrecisionError("pre-freeze quota must be above the 30% new-wave gate")
    if not quota_observed_at.strip() or not quota_source.strip() or not quota_limitation.strip():
        raise IdentityBandPrecisionError("explicit quota observed-at/source/limitation are required")
    _quota_time(quota_observed_at)
    v4, output, manifest = _path(root, v4_path), _path(root, output_path), _path(root, manifest_path)
    if output.exists() and not manifest.exists():
        return recover_sample_manifest(repo_root=root, output_path=output, manifest_path=manifest)
    if output.exists() or manifest.exists():
        raise IdentityBandPrecisionError("refusing to clobber WP2.5 sample artifact")
    pins: dict[str, dict[str, Any]] = {
        "v4_candidates_final": _pin_file(root, v4, "immutable_candidate_population", V4_SPEC if verify_pins else None),
        "v4_manifest": _pin_file(root, DEFAULT_V4_MANIFEST, "v4_release_manifest", V4_MANIFEST_SPEC if verify_pins else None),
        "v4_regression_gate": _pin_file(root, DEFAULT_V4_GATE, "four_regression_gates", V4_GATE_SPEC if verify_pins else None),
        "wp25_protocol": _pin_file(root, protocol_path, "measurement_protocol", WP25_PROTOCOL_SPEC if verify_pins else None),
        "identity_contract": _pin_file(root, contract_path, "identity_contract_section_8", IDENTITY_CONTRACT_SPEC if verify_pins else None),
        "n100_protocol": _pin_file(root, n100_protocol_path, "dual_blind_overlay", N100_PROTOCOL_SPEC if verify_pins else None),
        "wp25_order": _pin_file(root, order_path, "job_order", WP25_ORDER_SPEC if verify_pins else None),
    }
    reviewed, prior_pins = _prior_reviewed(root, verify=verify_pins)
    pins.update(prior_pins)
    gate = json.loads(_path(root, DEFAULT_V4_GATE).read_text(encoding="utf-8"))
    manifest_v4 = json.loads(_path(root, DEFAULT_V4_MANIFEST).read_text(encoding="utf-8"))
    if gate.get("status") != "PASS" or int(gate.get("passed_gate_count", -1)) != 4 or int(gate.get("failed_gate_count", -1)) != 0:
        raise IdentityBandPrecisionError("v4 four-regression gate is not PASS")
    if manifest_v4.get("status") != "PASS" or manifest_v4.get("stage_a_artifact_sha256") != pins["v4_candidates_final"]["sha256_before"]:
        raise IdentityBandPrecisionError("v4 manifest does not bind the pinned DB")
    source = _open_immutable(v4)
    temporary = output.with_name(output.name + ".building")
    failed = output.with_name(output.name + ".building.failed")
    if temporary.exists() or failed.exists():
        source.close()
        raise IdentityBandPrecisionError("stale WP2.5 build artifact exists")
    database: sqlite3.Connection | None = None
    try:
        run_rows = source.execute("SELECT * FROM runs").fetchall()
        if len(run_rows) != 1 or str(run_rows[0]["status"]) not in {"stage_a_complete", "pass"}:
            raise IdentityBandPrecisionError("v4 is not a completed immutable Stage-A artifact")
        if int(run_rows[0]["merge_executions"]) or int(run_rows[0]["vnx_id_issuances"]):
            raise IdentityBandPrecisionError("v4 mutation counters are nonzero")
        frames, frame_metrics, exclusions = _frame_rows(source, reviewed)
        for stratum, expected in EXPECTED_FRAMES.items():
            actual = frame_metrics[stratum]
            for key in ("raw", "flagged", "ex_related", "prior", "eligible"):
                if actual[key] != expected[key]:
                    raise IdentityBandPrecisionError(f"{stratum} {key} drift: {actual[key]} != {expected[key]}")
            if actual["raw"] != actual["flagged"] + actual["ex_related"] + actual["prior"] + actual["eligible"]:
                raise IdentityBandPrecisionError(f"{stratum} population accounting invariant failed")
        selected: list[dict[str, str]] = []
        selected_ids: set[str] = set()
        for stratum, quota in STRATUM_QUOTAS.items():
            ranked = sorted(frames[stratum], key=lambda cid: (_seed_rank(stratum, cid), cid))
            if len(ranked) < quota:
                raise IdentityBandPrecisionError(f"{stratum} cannot fill its frozen quota")
            chosen = ranked if stratum == "S5" else ranked[:quota]
            if stratum == "S5" and len(chosen) != quota:
                raise IdentityBandPrecisionError("S5 must be the complete 36-row census")
            for candidate_id in chosen:
                if candidate_id in selected_ids:
                    raise IdentityBandPrecisionError(f"cross-stratum sample collision: {candidate_id}")
                selected_ids.add(candidate_id)
                selected.append({"stratum": stratum, "candidate_id": candidate_id, "seed_rank": _seed_rank(stratum, candidate_id)})
        selected.sort(key=lambda row: (row["stratum"], row["seed_rank"], row["candidate_id"]))
        if len(selected) != TOTAL_COUNT or len(selected_ids) != TOTAL_COUNT:
            raise IdentityBandPrecisionError("221-row sample accounting mismatch")

        output.parent.mkdir(parents=True, exist_ok=True)
        database = sqlite3.connect(temporary)
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA journal_mode=DELETE")
        database.execute("PRAGMA synchronous=FULL")
        database.execute("PRAGMA foreign_keys=ON")
        database.execute(f"PRAGMA application_id={APPLICATION_ID}")
        database.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        database.executescript(SCHEMA_SQL)
        run_id = "wp25-" + hashlib.sha256(f"{SAMPLE_VERSION}\0{SEED}\0{pins['v4_candidates_final']['sha256_before']}".encode()).hexdigest()[:24]
        created = utc_now()
        run_payload = {"run_id": run_id, "schema_version": SCHEMA_VERSION, "runner_version": RUNNER_VERSION, "sample_version": SAMPLE_VERSION, "review_version": REVIEW_VERSION, "adjudication_version": ADJUDICATION_VERSION, "gate_version": GATE_VERSION, "seed": SEED, "status": "building", "created_at": created}
        database.execute("INSERT INTO runs(run_id,schema_version,runner_version,sample_version,review_version,adjudication_version,gate_version,seed,status,created_at,record_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (run_id, SCHEMA_VERSION, RUNNER_VERSION, SAMPLE_VERSION, REVIEW_VERSION, ADJUDICATION_VERSION, GATE_VERSION, SEED, "building", created, _record(run_payload)))
        for role, pin in sorted(pins.items()):
            payload = {"role": role, **pin}
            database.execute("INSERT INTO input_pins VALUES(?,?,?,?,?,?,?,?)", (run_id, role, pin["path"], pin["size_bytes"], pin["sha256_before"], None, pin["usage"], _record(payload)))
        _register_artifact_path(database, root, run_id, "sample_manifest", manifest, "freeze")
        quota_signals = [{"source": quota_source.strip(), "account_index": quota_account_index, "observed_at": quota_observed_at.strip(), "remaining_percent": float(quota_remaining_percent)}]
        quota_payload = {"observation_no": 1, "phase": "pre_freeze", "account_index": quota_account_index, "observed_at": quota_observed_at.strip(), "signals": quota_signals, "gate_basis_remaining_percent": float(quota_remaining_percent), "start_allowed": 1, "stop_required": 0, "limitation": quota_limitation.strip()}
        database.execute("INSERT INTO quota_observations VALUES(?,?,?,?,?,?,?,?,?,?,?)", (run_id, 1, "pre_freeze", quota_account_index, quota_observed_at.strip(), canonical_json(quota_signals), float(quota_remaining_percent), 1, 0, quota_limitation.strip(), _record(quota_payload)))
        for stratum in STRATUM_QUOTAS:
            values = frame_metrics[stratum]
            payload = {"stratum": stratum, **values, "quota": STRATUM_QUOTAS[stratum], "predicate_version": "wp25-predeclared-frame-v1"}
            database.execute("INSERT INTO population_frames VALUES(?,?,?,?,?,?,?,?,?,?)", (run_id, stratum, values["raw"], values["flagged"], values["ex_related"], values["prior"], values["eligible"], STRATUM_QUOTAS[stratum], payload["predicate_version"], _record(payload)))
        for excluded in exclusions:
            payload = {"candidate_id": excluded["candidate_id"], "stratum": excluded["stratum"], "exclusion_type": excluded["type"], "detail": excluded["detail"]}
            database.execute("INSERT INTO excluded_pairs VALUES(?,?,?,?,?,?)", (run_id, excluded["candidate_id"], excluded["stratum"], excluded["type"], canonical_json(excluded["detail"]), _record(payload)))
        sample_manifest_rows: list[dict[str, Any]] = []
        for rank, spec in enumerate(selected, 1):
            sample_id = "bp_" + hashlib.sha256(f"{run_id}\0{spec['candidate_id']}".encode()).hexdigest()[:24]
            payload = {"sample_rank": rank, "sample_id": sample_id, **spec}
            database.execute("INSERT INTO sample_pairs VALUES(?,?,?,?,?,?,?)", (run_id, rank, sample_id, spec["stratum"], spec["candidate_id"], spec["seed_rank"], _record(payload)))
            context = _candidate_context(source, spec["candidate_id"])
            snapshot_sha = canonical_sha256(context)
            database.execute("INSERT INTO candidate_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (run_id, sample_id, canonical_json(context["candidate"]), canonical_json(context["left"]), canonical_json(context["right"]), canonical_json(context["origins"]), canonical_json(context["families"]), canonical_json(context["conflicts"]), canonical_json(context["policy_flags"]), canonical_json(context["transition"]), snapshot_sha, _record({"sample_id": sample_id, "snapshot_sha256": snapshot_sha})))
            sample_manifest_rows.append(payload)
        for role, pin in sorted(pins.items()):
            current = sha256_file(_resolve_text(root, pin["path"]))
            if current != pin["sha256_before"]:
                raise IdentityBandPrecisionError(f"input drift during freeze: {role}")
            pin["sha256_after"] = current
            final_pin_payload = {"role": role, **pin}
            database.execute("UPDATE input_pins SET sha256_after=?,record_sha256=? WHERE run_id=? AND role=?", (current, _record(final_pin_payload), run_id, role))
        logical = _logical_sha(database, run_id)
        database.execute("UPDATE runs SET status='sample_frozen',sample_count=?,sample_logical_sha256=? WHERE run_id=?", (TOTAL_COUNT, logical, run_id))
        _refresh_run_record(database, run_id)
        _insert_event(database, run_id, "building", "sample_frozen", {"sample_count": TOTAL_COUNT, "sample_logical_sha256": logical})
        database.commit()
        database.close(); database = None
        source.close()
        output_payload = {"status": "PASS", "run_id": run_id, "versions": {"runner": RUNNER_VERSION, "sample": SAMPLE_VERSION}, "seed": SEED, "counts": {"total": TOTAL_COUNT, "prior_review_union": 135, "strata": dict(STRATUM_QUOTAS)}, "population_frames": frame_metrics, "samples": sample_manifest_rows, "sample_logical_sha256": logical, "quota_pre_freeze": quota_payload, "sidecar": {"path": _path_text(root, output), "size_bytes": temporary.stat().st_size, "sha256": sha256_file(temporary)}, "input_pins": pins, "network_requests": 0, "vision_requests": 0, "llm_requests": 0, "merge_executions": 0, "vnext_id_issuances": 0, "post_freeze_replacements": 0}
        manifest_temporary = manifest.with_name(manifest.name + ".building")
        if manifest_temporary.exists():
            raise IdentityBandPrecisionError(f"stale temporary artifact: {manifest_temporary}")
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest_raw = (json.dumps(output_payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
        with manifest_temporary.open("xb") as handle:
            handle.write(manifest_raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
        os.replace(manifest_temporary, manifest)
        return BuildResult(output, manifest, run_id, TOTAL_COUNT, logical)
    except Exception:
        if database is not None:
            database.close()
        source.close()
        if temporary.exists() and not failed.exists():
            os.replace(temporary, failed)
        raise


def recover_sample_manifest(*, repo_root: Path, output_path: Path = DEFAULT_OUTPUT,
                            manifest_path: Path = DEFAULT_SAMPLE_MANIFEST) -> BuildResult:
    """Recover only the byte-identical manifest after an interrupted two-file publish."""
    root = repo_root.resolve(); output, manifest = _path(root, output_path), _path(root, manifest_path)
    if not output.is_file() or manifest.exists():
        raise IdentityBandPrecisionError("sample-manifest recovery requires DB present and manifest absent")
    connection = _open_immutable(output)
    try:
        run = connection.execute("SELECT * FROM runs").fetchall()
        if len(run) != 1 or str(run[0]["status"]) != "sample_frozen" or int(run[0]["sample_count"]) != TOTAL_COUNT:
            raise IdentityBandPrecisionError("orphan DB is not a pristine frozen sample")
        row = run[0]; run_id = str(row["run_id"])
        _verify_sample_inputs(connection, root, run_id)
        logical = _logical_sha(connection, run_id)
        if logical != str(row["sample_logical_sha256"]):
            raise IdentityBandPrecisionError("orphan DB logical SHA cannot be replayed")
        pins = {str(pin["role"]): {key: pin[key] for key in ("path", "size_bytes", "sha256_before", "sha256_after", "usage")} for pin in connection.execute("SELECT * FROM input_pins WHERE run_id=? ORDER BY role", (run_id,))}
        frames = {str(item["stratum"]): {"raw": int(item["raw_count"]), "flagged": int(item["flagged_count"]), "ex_related": int(item["ex_related_count"]), "prior": int(item["prior_review_count"]), "eligible": int(item["eligible_count"])} for item in connection.execute("SELECT * FROM population_frames WHERE run_id=? ORDER BY stratum", (run_id,))}
        samples = [{"sample_rank": int(item["sample_rank"]), "sample_id": str(item["sample_id"]), "stratum": str(item["stratum"]), "candidate_id": str(item["candidate_id"]), "seed_rank": str(item["seed_rank_sha256"])} for item in connection.execute("SELECT * FROM sample_pairs WHERE run_id=? ORDER BY sample_rank", (run_id,))]
        quota = connection.execute("SELECT * FROM quota_observations WHERE run_id=? AND phase='pre_freeze'", (run_id,)).fetchone()
        if quota is None:
            raise IdentityBandPrecisionError("orphan DB lacks pre-freeze quota evidence")
        quota_payload = {"observation_no": int(quota["observation_no"]), "phase": str(quota["phase"]), "account_index": int(quota["account_index"]), "observed_at": str(quota["observed_at"]), "signals": json.loads(str(quota["signals_json"])), "gate_basis_remaining_percent": float(quota["gate_basis_remaining_percent"]), "start_allowed": int(quota["start_allowed"]), "stop_required": int(quota["stop_required"]), "limitation": str(quota["limitation"])}
        payload = {"status": "PASS", "run_id": run_id, "versions": {"runner": RUNNER_VERSION, "sample": SAMPLE_VERSION}, "seed": SEED, "counts": {"total": TOTAL_COUNT, "prior_review_union": 135, "strata": dict(STRATUM_QUOTAS)}, "population_frames": frames, "samples": samples, "sample_logical_sha256": logical, "quota_pre_freeze": quota_payload, "sidecar": {"path": _path_text(root, output), "size_bytes": output.stat().st_size, "sha256": sha256_file(output)}, "input_pins": pins, "network_requests": 0, "vision_requests": 0, "llm_requests": 0, "merge_executions": 0, "vnext_id_issuances": 0, "post_freeze_replacements": 0}
    finally:
        connection.close()
    raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    staged = manifest.with_name(manifest.name + ".building")
    if staged.exists():
        if not staged.is_file() or staged.read_bytes() != raw:
            raise IdentityBandPrecisionError("staged manifest is not the byte-identical recovery payload")
        os.replace(staged, manifest)
    else:
        _atomic_bytes(manifest, raw)
    return BuildResult(output, manifest, run_id, TOTAL_COUNT, logical)


def record_quota_observation(*, repo_root: Path, artifact_path: Path = DEFAULT_OUTPUT,
                             phase: str, remaining_percent: float, source: str,
                             observed_at: str, account_index: int = 0,
                             limitation: str = "") -> dict[str, Any]:
    """Append a quota gate observation; fail closed before low-quota review waves."""
    if not phase.strip() or not source.strip() or not observed_at.strip():
        raise IdentityBandPrecisionError("quota phase/source/observed_at are required")
    if not 0 <= remaining_percent <= 100:
        raise IdentityBandPrecisionError("quota remaining_percent must be in 0..100")
    root = repo_root.resolve()
    artifact = _path(root, artifact_path)
    connection = sqlite3.connect(artifact)
    connection.row_factory = sqlite3.Row
    try:
        run = connection.execute("SELECT * FROM runs").fetchone()
        if run is None or str(run["status"]) in {"pass", "fail", "failed"}:
            raise IdentityBandPrecisionError("quota observation requires an active WP2.5 run")
        run_id = str(run["run_id"])
        normalized_phase = phase.strip()
        _validate_next_quota_observation(
            connection, run_id, normalized_phase, observed_at.strip(), str(run["status"])
        )
        number = int(connection.execute("SELECT coalesce(max(observation_no),0)+1 FROM quota_observations WHERE run_id=?", (run_id,)).fetchone()[0])
        stop_required = remaining_percent <= 20
        start_allowed = remaining_percent > 30
        signals = [{"source": source, "account_index": account_index, "observed_at": observed_at, "remaining_percent": remaining_percent}]
        payload = {"observation_no": number, "phase": normalized_phase, "account_index": account_index, "observed_at": observed_at.strip(), "signals": signals, "gate_basis_remaining_percent": float(remaining_percent), "start_allowed": int(start_allowed), "stop_required": int(stop_required), "limitation": limitation.strip()}
        connection.execute("INSERT INTO quota_observations VALUES(?,?,?,?,?,?,?,?,?,?,?)", (run_id, number, payload["phase"], account_index, payload["observed_at"], canonical_json(signals), float(remaining_percent), int(start_allowed), int(stop_required), payload["limitation"], _record(payload)))
        connection.commit()
        result = {"run_id": run_id, **payload}
        if stop_required:
            raise IdentityBandPrecisionError("weekly quota is at or below 20%; all review activity must stop")
        if not start_allowed:
            raise IdentityBandPrecisionError("weekly quota is at or below 30%; a new review wave may not start")
        return result
    finally:
        connection.close()


def cache_tree(root: Path) -> tuple[int, int, str]:
    rows: list[dict[str, Any]] = []
    total = 0
    if root.exists() and not root.is_dir():
        raise IdentityBandPrecisionError(f"cache root is not a directory: {root}")
    if root.exists():
        for path in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda item: item.relative_to(root).as_posix()):
            size = path.stat().st_size
            rows.append({"path": path.relative_to(root).as_posix(), "size": size, "sha256": sha256_file(path)})
            total += size
    return len(rows), total, hashlib.sha256(canonical_json(rows).encode("utf-8")).hexdigest()


def _cache_relative(pixel_sha256: str, transform_version: str) -> Path:
    transform_key = hashlib.sha256(transform_version.encode("utf-8")).hexdigest()[:12]
    return Path(pixel_sha256[:2]) / f"{pixel_sha256}.{transform_key}.jpg"


def _path_overlaps(left: Path, right: Path) -> bool:
    left, right = left.resolve(), right.resolve()
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def _verify_sample_inputs(connection: sqlite3.Connection, root: Path, run_id: str) -> None:
    for row in connection.execute("SELECT role,path,size_bytes,sha256_before,sha256_after FROM input_pins WHERE run_id=? ORDER BY role", (run_id,)):
        path = _resolve_text(root, str(row["path"]))
        if not path.is_file() or path.stat().st_size != int(row["size_bytes"]):
            raise IdentityBandPrecisionError(f"input size drift: {row['role']}")
        current = sha256_file(path)
        if current != str(row["sha256_before"]) or (row["sha256_after"] is not None and current != str(row["sha256_after"])):
            raise IdentityBandPrecisionError(f"input hash drift: {row['role']}")


def _pin_current_file(connection: sqlite3.Connection, root: Path, run_id: str, role: str,
                      path: Path, usage: str, expected: tuple[int, str] | None) -> dict[str, Any]:
    pin = _pin_file(root, path, usage, expected)
    pin["sha256_after"] = pin["sha256_before"]
    payload = {"role": role, **pin}
    connection.execute("INSERT INTO input_pins VALUES(?,?,?,?,?,?,?,?)", (run_id, role, pin["path"], pin["size_bytes"], pin["sha256_before"], pin["sha256_after"], usage, _record(payload)))
    return payload


def _pin_caches(connection: sqlite3.Connection, root: Path, run_id: str,
                read_caches: Sequence[CacheSpec], output_cache: Path,
                *, verify_expected: bool) -> list[dict[str, Any]]:
    pins: list[dict[str, Any]] = []
    for spec in read_caches:
        path = _path(root, spec.path)
        if _path_overlaps(path, output_cache):
            raise IdentityBandPrecisionError("read-only and write-only cache roots overlap")
        current = cache_tree(path)
        if verify_expected and current != (spec.file_count, spec.total_bytes, spec.tree_sha256):
            raise IdentityBandPrecisionError(f"read-cache pin mismatch: {spec.role}")
        payload = {"role": spec.role, "path": _path_text(root, path), "transform_version": spec.transform_version, "access_mode": "read_only", "file_count_before": current[0], "total_bytes_before": current[1], "tree_sha256_before": current[2], "file_count_after": current[0], "total_bytes_after": current[1], "tree_sha256_after": current[2]}
        connection.execute("INSERT INTO cache_pins VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (run_id, spec.role, payload["path"], spec.transform_version, "read_only", *current, *current, _record(payload)))
        pins.append(payload)
    if output_cache.exists():
        raise IdentityBandPrecisionError("write-only WP2.5 cache already exists")
    empty = cache_tree(output_cache)
    payload = {"role": "wp25_output", "path": _path_text(root, output_cache), "transform_version": review_support.TRANSFORM_VERSION, "access_mode": "write_only", "file_count_before": empty[0], "total_bytes_before": empty[1], "tree_sha256_before": empty[2], "file_count_after": None, "total_bytes_after": None, "tree_sha256_after": None}
    connection.execute("INSERT INTO cache_pins VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (run_id, "wp25_output", payload["path"], payload["transform_version"], "write_only", *empty, None, None, None, _record(payload)))
    pins.append(payload)
    return pins


def _choose_assets(v4: sqlite3.Connection, e2: sqlite3.Connection,
                   candidate: Mapping[str, Any]) -> dict[str, list[tuple[str, str]]]:
    selected: dict[str, list[tuple[str, str]]] = {"left": [], "right": []}
    priorities = {"exact_pixel": 0, "identical_phash": 1, "phash_le8": 2, "phash_9_16": 3}
    direct = [dict(row) for row in v4.execute("SELECT * FROM direct_image_evidence WHERE candidate_id=?", (str(candidate["candidate_id"]),))]
    direct.sort(key=lambda row: (priorities.get(str(row["evidence_kind"]), 99), int(row["phash_distance"] or 0), str(row["e2_evidence_id"])))
    for index, row in enumerate(direct, 1):
        for side, column in (("left", "left_source_asset_id"), ("right", "right_source_asset_id")):
            asset_id = str(row[column])
            if asset_id not in {value for value, _ in selected[side]} and len(selected[side]) < MAX_ASSETS_PER_SIDE:
                selected[side].append((asset_id, f"direct_evidence_{index}"))
        if all(len(values) >= MAX_ASSETS_PER_SIDE for values in selected.values()):
            break
    for side in ("left", "right"):
        source = str(candidate[f"{side}_source"])
        building_id = str(candidate[f"{side}_source_building_id"])
        existing = {value for value, _ in selected[side]}
        for asset_id in review_support._top_building_assets(e2, source, building_id):
            if len(selected[side]) >= MAX_ASSETS_PER_SIDE:
                break
            if asset_id not in existing:
                selected[side].append((asset_id, "building_representative"))
                existing.add(asset_id)
    return selected


def _cache_hit(root: Path, expected_pixel: str, read_caches: Sequence[CacheSpec]) -> tuple[str, Path] | None:
    for spec in read_caches:
        candidate = _path(root, spec.path) / _cache_relative(expected_pixel, spec.transform_version)
        if candidate.is_file():
            return spec.role, candidate
    return None


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
    redirects: tuple[Mapping[str, Any], ...]
    error_kind: str | None
    error_message: str | None
    body: bytes | None
    started_at: str
    completed_at: str


def _redirect_json(hops: Iterable[Any]) -> tuple[Mapping[str, Any], ...]:
    return tuple(dict(vars(hop)) if hasattr(hop, "__dict__") else {"value": str(hop)} for hop in hops)


def _fetch_one(row: Mapping[str, Any], fetcher: Callable[..., FetchPayload]) -> _FetchOutcome:
    started = utc_now()
    payload: FetchPayload | None = None
    try:
        payload = fetcher(str(row["source"]), str(row["expected_url"]), max_redirects=0)
        if int(payload.request_count) > 1 or payload.redirect_chain:
            raise IdentityBandPrecisionError("bounded fetcher followed a redirect or retried")
        fingerprint = fingerprint_bytes(payload.body)
        if fingerprint.response_sha256 != payload.raw_response_sha256:
            raise IdentityBandPrecisionError("fetch/fingerprint raw SHA mismatch")
        if fingerprint.pixel_sha256 == row["expected_pixel_sha256"]:
            status = "exact" if fingerprint.response_sha256 == row["expected_raw_sha256"] else "delivery_changed"
        else:
            status = "source_changed"
        return _FetchOutcome(str(row["asset_key"]), status, payload.final_url, fingerprint.response_sha256, fingerprint.pixel_sha256, int(payload.http_status), len(payload.body), int(payload.request_count), _redirect_json(payload.redirect_chain), None, None, payload.body, started, utc_now())
    except FetchFailure as exc:
        return _FetchOutcome(str(row["asset_key"]), "failed", exc.final_url, exc.raw_response_sha256, None, exc.http_status, exc.response_bytes, int(exc.request_count), _redirect_json(exc.redirect_chain), str(exc.kind), str(exc)[:2000], None, started, utc_now())
    except (FingerprintError, ValueError, OSError, IdentityBandPrecisionError) as exc:
        return _FetchOutcome(str(row["asset_key"]), "failed", getattr(payload, "final_url", None), getattr(payload, "raw_response_sha256", None), None, getattr(payload, "http_status", None), len(payload.body) if payload is not None else None, int(payload.request_count) if payload is not None else 0, _redirect_json(payload.redirect_chain) if payload is not None else (), "local_validation_failed", str(exc)[:2000], None, started, utc_now())
    except Exception as exc:  # A worker exception must become a durable failed outcome.
        return _FetchOutcome(str(row["asset_key"]), "failed", getattr(payload, "final_url", None), getattr(payload, "raw_response_sha256", None), None, getattr(payload, "http_status", None), len(payload.body) if payload is not None else None, int(getattr(payload, "request_count", 0) or 0), _redirect_json(getattr(payload, "redirect_chain", ())), "unexpected_fetch_exception", f"{type(exc).__name__}: {exc}"[:2000], None, started, utc_now())


def _materialize_derivative(output_cache: Path, pixel_sha: str, body: bytes) -> Path:
    target = output_cache / _cache_relative(pixel_sha, review_support.TRANSFORM_VERSION)
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
    return {"source": str(value.get("source", "unknown")), "name": str(value.get("name", "unknown")), "aliases": [str(item) for item in aliases or []], "firms": [str(item) for item in firms or []], "country": value.get("country"), "locality": value.get("locality"), "year_min": value.get("completion_year_min", value.get("year_min")), "year_max": value.get("completion_year_max", value.get("year_max"))}


def _render_blind_sheet(connection: sqlite3.Connection, root: Path, run_id: str,
                        sample_id: str, opaque: str, path: Path) -> tuple[str, int]:
    from PIL import Image, ImageDraw
    snapshot = connection.execute("SELECT left_metadata_json,right_metadata_json FROM candidate_snapshots WHERE run_id=? AND sample_id=?", (run_id, sample_id)).fetchone()
    if snapshot is None:
        raise IdentityBandPrecisionError("review snapshot is missing")
    left, right = _metadata_for_sheet(str(snapshot[0])), _metadata_for_sheet(str(snapshot[1]))
    canvas = Image.new("RGB", (1800, 1100), (244, 246, 248)); draw = ImageDraw.Draw(canvas)
    title, heading, body, small = review_support._font(34), review_support._font(25), review_support._font(20), review_support._font(16)
    draw.text((40, 28), f"Blind building identity review - {opaque}", font=title, fill=(20, 25, 30))
    draw.text((40, 74), "Judge the focal pair only. Selection, score, proposal, flags, band, and stratum are hidden.", font=small, fill=(75, 80, 88))
    for side, metadata, x in (("LEFT", left, 35), ("RIGHT", right, 915)):
        draw.rounded_rectangle((x, 110, x + 850, 1040), radius=12, fill="white", outline=(195, 200, 207), width=2)
        draw.text((x + 24, 132), f"{side} - {metadata['source']}", font=heading, fill=(25, 45, 70))
        ymin, ymax = metadata["year_min"], metadata["year_max"]
        year = ymin if ymin == ymax else f"{ymin} - {ymax}"
        info = f"Name: {metadata['name']}\nFirm: {'; '.join(metadata['firms'][:4]) or 'unknown'}\nLocation: {metadata['locality'] or 'unknown'}, {metadata['country'] or 'unknown'}\nYear: {year if ymin is not None or ymax is not None else 'unknown'}"
        review_support._draw_wrapped(draw, info, (x + 24, 178), 72, body, (35, 38, 43), 4)
        assets = connection.execute("SELECT s.asset_order,f.status,f.cache_path FROM sample_assets s JOIN fetch_assets f USING(run_id,asset_key) WHERE s.run_id=? AND s.sample_id=? AND s.side=? ORDER BY s.asset_order", (run_id, sample_id, side.casefold())).fetchall()
        for index, asset in enumerate(assets):
            ix, iy = x + 24 + index * 405, 385
            draw.rectangle((ix, iy, ix + 380, iy + 430), fill=(222, 225, 229), outline=(150, 155, 163), width=2)
            if asset["cache_path"]:
                cached = _resolve_text(root, str(asset["cache_path"]))
                if cached.is_file():
                    try:
                        canvas.paste(review_support._fit_image(cached.read_bytes(), 376, 390), (ix + 2, iy + 38))
                    except OSError:
                        pass
            status = str(asset["status"])
            label = {"cache_hit": "frozen cached image", "exact": "current image - exact", "delivery_changed": "current image - delivery changed", "source_changed": "current image - SOURCE CHANGED", "failed": "image unavailable"}.get(status, status)
            draw.text((ix + 8, iy + 8), label, font=small, fill=(155, 45, 35) if status in {"source_changed", "failed"} else (30, 95, 65))
        draw.text((x + 24, 850), "Aliases:", font=small, fill=(70, 75, 80))
        review_support._draw_wrapped(draw, "; ".join(metadata["aliases"][:5]) or "none", (x + 24, 875), 90, small, (70, 75, 80), 3)
    output = io.BytesIO(); canvas.save(output, format="PNG", optimize=False); raw = output.getvalue()
    _atomic_bytes(path, raw, allow_identical=True)
    return hashlib.sha256(raw).hexdigest(), len(raw)


def _blind_lane_items(connection: sqlite3.Connection, root: Path, run_id: str,
                      lane: str, sheets_root: Path) -> list[dict[str, Any]]:
    samples = [str(row[0]) for row in connection.execute("SELECT sample_id FROM sample_pairs WHERE run_id=?", (run_id,))]
    if len(samples) != TOTAL_COUNT:
        raise IdentityBandPrecisionError("review packet must contain exactly 221 rows")
    ranked: list[tuple[str, str, str]] = []
    for sample_id in samples:
        opaque = f"{lane}_" + hashlib.sha256(f"{REVIEW_VERSION}\0{run_id}\0{lane}\0{sample_id}".encode()).hexdigest()[:24]
        order = hashlib.sha256(f"{SEED}\0blind-order\0{lane}\0{sample_id}".encode()).hexdigest()
        ranked.append((order, opaque, sample_id))
    items: list[dict[str, Any]] = []
    for display_order, (_, opaque, sample_id) in enumerate(sorted(ranked), 1):
        sheet = sheets_root / lane / f"{display_order:03d}_{opaque}.png"
        digest, size = _render_blind_sheet(connection, root, run_id, sample_id, opaque, sheet)
        payload = {"lane": lane, "display_order": display_order, "opaque_review_id": opaque, "sample_id": sample_id, "sheet_path": _path_text(root, sheet), "sheet_sha256": digest, "sheet_bytes": size}
        existing = connection.execute("SELECT * FROM review_items WHERE run_id=? AND lane=? AND opaque_review_id=?", (run_id, lane, opaque)).fetchone()
        if existing is None:
            connection.execute("INSERT INTO review_items VALUES(?,?,?,?,?,?,?,?,?)", (run_id, lane, display_order, opaque, sample_id, payload["sheet_path"], digest, size, _record(payload)))
        elif any(existing[key] != payload[key] for key in ("display_order", "sample_id", "sheet_path", "sheet_sha256", "sheet_bytes")):
            raise IdentityBandPrecisionError("partial review-item ledger does not match deterministic replay")
        items.append({"display_order": display_order, "opaque_review_id": opaque, "sheet_path": payload["sheet_path"], "sheet_sha256": digest, "label": "", "rationale": "", "hard_conflict_observed": ""})
    return items


def _form_payload(run_id: str, lane: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    contract = {"review_version": REVIEW_VERSION, "run_id": run_id, "lane": lane, "items": [{key: item[key] for key in ("display_order", "opaque_review_id", "sheet_path", "sheet_sha256")} for item in items]}
    return {**contract, "form_contract_sha256": canonical_sha256(contract), "reviewer": "", "reviewed_at": "", "allowed_labels": sorted(ALLOWED_LABELS), "items": items}


def _fetch_asset_record(row: Mapping[str, Any], *, status: str,
                        cache_role: str | None, cache_path: str | None,
                        final_url: str | None, raw_sha256: str | None,
                        pixel_sha256: str | None, http_status: int | None,
                        response_bytes: int | None, request_count: int,
                        error_kind: str | None, error_message: str | None) -> dict[str, Any]:
    return {
        "asset_key": row["asset_key"], "source": row["source"],
        "source_asset_id": row["source_asset_id"],
        "expected_url": row["expected_url"],
        "expected_raw_sha256": row["expected_raw_sha256"],
        "expected_pixel_sha256": row["expected_pixel_sha256"],
        "status": status, "cache_role": cache_role, "cache_path": cache_path,
        "actual_final_url": final_url, "actual_raw_sha256": raw_sha256,
        "actual_pixel_sha256": pixel_sha256, "http_status": http_status,
        "response_bytes": response_bytes, "request_count": request_count,
        "error_kind": error_kind, "error_message": error_message,
    }


def _reserve_fetch_attempts(connection: sqlite3.Connection, run_id: str,
                            pending: Sequence[Mapping[str, Any]]) -> None:
    """Durably consume each one-shot logical/wire budget before network starts."""
    for item in pending:
        row = connection.execute(
            "SELECT * FROM fetch_assets WHERE run_id=? AND asset_key=?",
            (run_id, str(item["asset_key"])),
        ).fetchone()
        if row is None or str(row["status"]) != "pending":
            raise IdentityBandPrecisionError("HTTP reservation requires one pending asset")
        reserved_at = utc_now()
        updated = _fetch_asset_record(
            row, status="reserved", cache_role=None, cache_path=None,
            final_url=None, raw_sha256=None, pixel_sha256=None,
            http_status=None, response_bytes=None, request_count=1,
            error_kind="attempt_reserved", error_message="outcome not yet committed",
        )
        connection.execute(
            "UPDATE fetch_assets SET status='reserved',cache_role=NULL,cache_path=NULL,"
            "actual_final_url=NULL,actual_raw_sha256=NULL,actual_pixel_sha256=NULL,"
            "http_status=NULL,response_bytes=NULL,request_count=1,error_kind='attempt_reserved',"
            "error_message='outcome not yet committed',record_sha256=? "
            "WHERE run_id=? AND asset_key=?",
            (_record(updated), run_id, str(row["asset_key"])),
        )
        attempt = {
            "asset_key": str(row["asset_key"]), "attempt_no": 1,
            "status": "reserved", "request_count": 1, "redirect_chain": [],
            "started_at": reserved_at, "completed_at": reserved_at,
            "error_kind": "attempt_reserved",
        }
        connection.execute(
            "INSERT INTO fetch_attempts VALUES(?,?,?,?,?,?,?,?,?,?)",
            (run_id, str(row["asset_key"]), 1, "reserved", 1, "[]", reserved_at,
             reserved_at, "attempt_reserved", _record(attempt)),
        )
    connection.commit()


def _terminalize_reserved_fetches(connection: sqlite3.Connection, run_id: str,
                                  *, reason: str) -> int:
    """Close crash-ambiguous reservations without issuing another request."""
    rows = connection.execute(
        "SELECT f.*,a.started_at FROM fetch_assets f JOIN fetch_attempts a "
        "USING(run_id,asset_key) WHERE f.run_id=? AND f.status='reserved' "
        "AND a.status='reserved' ORDER BY f.asset_key", (run_id,)
    ).fetchall()
    for row in rows:
        completed = utc_now()
        message = reason[:2000]
        updated = _fetch_asset_record(
            row, status="failed", cache_role=None, cache_path=None,
            final_url=None, raw_sha256=None, pixel_sha256=None,
            http_status=None, response_bytes=None, request_count=1,
            error_kind="interrupted_unknown", error_message=message,
        )
        connection.execute(
            "UPDATE fetch_assets SET status='failed',cache_role=NULL,cache_path=NULL,"
            "actual_final_url=NULL,actual_raw_sha256=NULL,actual_pixel_sha256=NULL,"
            "http_status=NULL,response_bytes=NULL,request_count=1,"
            "error_kind='interrupted_unknown',error_message=?,record_sha256=? "
            "WHERE run_id=? AND asset_key=?",
            (message, _record(updated), run_id, str(row["asset_key"])),
        )
        attempt = {
            "asset_key": str(row["asset_key"]), "attempt_no": 1,
            "status": "failed", "request_count": 1, "redirect_chain": [],
            "started_at": str(row["started_at"]), "completed_at": completed,
            "error_kind": "interrupted_unknown",
        }
        connection.execute(
            "UPDATE fetch_attempts SET status='failed',request_count=1,"
            "redirect_chain_json='[]',completed_at=?,error_kind='interrupted_unknown',"
            "record_sha256=? WHERE run_id=? AND asset_key=? AND attempt_no=1",
            (completed, _record(attempt), run_id, str(row["asset_key"])),
        )
    if rows:
        connection.commit()
    return len(rows)


def _persist_fetch_outcome(connection: sqlite3.Connection, root: Path, run_id: str,
                           output_cache: Path, outcome: _FetchOutcome) -> None:
    """Materialize and commit one completed worker result before accepting another."""
    row = connection.execute("SELECT * FROM fetch_assets WHERE run_id=? AND asset_key=?", (run_id, outcome.asset_key)).fetchone()
    if row is None or str(row["status"]) != "reserved":
        raise IdentityBandPrecisionError("fetch outcome does not bind one reserved asset")
    status, error_kind, error_message = outcome.status, outcome.error_kind, outcome.error_message
    cache_role = cache_path = None
    if outcome.body is not None and outcome.pixel_sha256:
        try:
            materialized = _materialize_derivative(output_cache, outcome.pixel_sha256, outcome.body)
            cache_role, cache_path = "wp25_output", _path_text(root, materialized)
        except (OSError, ValueError) as exc:
            status, error_kind, error_message = "failed", "derivative_materialization_failed", str(exc)[:2000]
    updated = _fetch_asset_record(row, status=status, cache_role=cache_role,
                                  cache_path=cache_path, final_url=outcome.final_url,
                                  raw_sha256=outcome.raw_sha256,
                                  pixel_sha256=outcome.pixel_sha256,
                                  http_status=outcome.http_status,
                                  response_bytes=outcome.response_bytes,
                                  request_count=outcome.request_count,
                                  error_kind=error_kind, error_message=error_message)
    connection.execute("UPDATE fetch_assets SET status=?,cache_role=?,cache_path=?,actual_final_url=?,actual_raw_sha256=?,actual_pixel_sha256=?,http_status=?,response_bytes=?,request_count=?,error_kind=?,error_message=?,record_sha256=? WHERE run_id=? AND asset_key=?", (status, cache_role, cache_path, outcome.final_url, outcome.raw_sha256, outcome.pixel_sha256, outcome.http_status, outcome.response_bytes, outcome.request_count, error_kind, error_message, _record(updated), run_id, outcome.asset_key))
    attempt = connection.execute(
        "SELECT started_at FROM fetch_attempts WHERE run_id=? AND asset_key=? AND attempt_no=1",
        (run_id, outcome.asset_key),
    ).fetchone()
    if attempt is None:
        raise IdentityBandPrecisionError("durable HTTP reservation is missing")
    attempt_payload = {"asset_key": outcome.asset_key, "attempt_no": 1, "status": status, "request_count": outcome.request_count, "redirect_chain": list(outcome.redirects), "started_at": str(attempt["started_at"]), "completed_at": outcome.completed_at, "error_kind": error_kind}
    connection.execute(
        "UPDATE fetch_attempts SET status=?,request_count=?,redirect_chain_json=?,"
        "completed_at=?,error_kind=?,record_sha256=? "
        "WHERE run_id=? AND asset_key=? AND attempt_no=1",
        (status, outcome.request_count, canonical_json(list(outcome.redirects)),
         outcome.completed_at, error_kind, _record(attempt_payload), run_id,
         outcome.asset_key),
    )
    connection.commit()


def prepare_review(*, repo_root: Path, artifact_path: Path = DEFAULT_OUTPUT,
                   output_cache_path: Path = DEFAULT_OUTPUT_CACHE,
                   sheets_root: Path = DEFAULT_SHEETS_ROOT,
                   r1_form_path: Path = DEFAULT_R1_FORM,
                   r2_form_path: Path = DEFAULT_R2_FORM,
                   fetch_manifest_path: Path = DEFAULT_FETCH_MANIFEST,
                   fetch_report_path: Path = DEFAULT_FETCH_REPORT,
                   e2_spec: tuple[Path, int, str] | None = None,
                   e1_specs: Mapping[str, tuple[Path, int, str]] | None = None,
                   read_caches: Sequence[CacheSpec] = DEFAULT_READ_CACHES,
                   workers: int = 4, fetcher: Callable[..., FetchPayload] = fetch_once,
                   verify_source_pins: bool = True,
                   verify_cache_pins: bool = True) -> PrepareResult:
    """Perform one globally deduplicated, redirect-0/retry-0 pixel wave."""
    if not 1 <= workers <= MAX_WORKERS:
        raise IdentityBandPrecisionError("workers must be in 1..4")
    root = repo_root.resolve()
    artifact, output_cache = _path(root, artifact_path), _path(root, output_cache_path)
    sheet_root, r1_form, r2_form = _path(root, sheets_root), _path(root, r1_form_path), _path(root, r2_form_path)
    fetch_manifest, fetch_report = _path(root, fetch_manifest_path), _path(root, fetch_report_path)
    if not artifact.is_file():
        raise IdentityBandPrecisionError("WP2.5 sample sidecar is missing")
    e2_config = e2_spec or review_support.E2_SPEC
    e1_config = dict(e1_specs or review_support.E1_SPECS)
    database = sqlite3.connect(artifact); database.row_factory = sqlite3.Row
    database.execute("PRAGMA foreign_keys=ON")
    v4: sqlite3.Connection | None = None
    e2: sqlite3.Connection | None = None
    e1: dict[str, sqlite3.Connection] = {}
    run_id = ""
    try:
        run = database.execute("SELECT * FROM runs").fetchall()
        if len(run) != 1 or str(run[0]["status"]) not in {"sample_frozen", "fetching", "review_ready"} or int(run[0]["sample_count"]) != TOTAL_COUNT:
            raise IdentityBandPrecisionError("prepare requires a frozen, fetching, or review-ready 221-row run")
        initial = str(run[0]["status"]) == "sample_frozen"
        recovering_ready = str(run[0]["status"]) == "review_ready"
        if initial:
            for target in (output_cache, sheet_root, r1_form, r2_form, fetch_manifest, fetch_report):
                if target.exists():
                    raise IdentityBandPrecisionError(f"refusing to clobber initial prepare artifact: {target}")
        run_id = str(run[0]["run_id"])
        _verify_sample_inputs(database, root, run_id)
        if _logical_sha(database, run_id) != str(run[0]["sample_logical_sha256"]):
            raise IdentityBandPrecisionError("frozen sample logical SHA drift")
        _require_global_not_stopped(database, run_id)
        _require_quota_phase(database, run_id, "pre_prepare")
        for role, path in (("fetch_manifest", fetch_manifest), ("fetch_report", fetch_report),
                           ("r1_blank_form", r1_form), ("r2_blank_form", r2_form),
                           ("sheets_root", sheet_root)):
            _register_artifact_path(database, root, run_id, role, path, "prepare")
        reserved_on_entry = int(database.execute(
            "SELECT count(*) FROM fetch_attempts WHERE run_id=? AND status='reserved'",
            (run_id,),
        ).fetchone()[0])
        if reserved_on_entry:
            closed = _terminalize_reserved_fetches(
                database, run_id,
                reason="process ended before the reserved HTTP outcome was committed",
            )
            failure = {"status": "FAIL", "run_id": run_id, "phase": "prepare",
                       "error_kind": "interrupted_http_outcome",
                       "error_message": f"closed {closed} ambiguous durable HTTP reservation(s) without retry",
                       "structural_failure": True, "retried_requests": 0}
            database.execute("UPDATE runs SET status='failed' WHERE run_id=?", (run_id,))
            _refresh_run_record(database, run_id)
            _insert_event(database, run_id, "fetching", "failed", failure)
            database.commit()
            if not fetch_report.exists():
                _atomic_json(fetch_report, failure)
            raise IdentityBandPrecisionError(failure["error_message"])
        v4_pin = database.execute("SELECT path,sha256_before FROM input_pins WHERE run_id=? AND role='v4_candidates_final'", (run_id,)).fetchone()
        v4_path = _resolve_text(root, str(v4_pin["path"])); v4_before = sha256_file(v4_path)
        if v4_before != str(v4_pin["sha256_before"]):
            raise IdentityBandPrecisionError("v4 drift before prepare")
        v4 = _open_immutable(v4_path)
        if initial:
            database.execute("UPDATE runs SET status='fetching' WHERE run_id=?", (run_id,)); _refresh_run_record(database, run_id)
            _insert_event(database, run_id, "sample_frozen", "fetching", {"workers": workers})
            e2_path, e2_size, e2_sha = e2_config
            _pin_current_file(database, root, run_id, "e2_image_evidence", e2_path, "asset_selection_and_frozen_lineage", (e2_size, e2_sha) if verify_source_pins else None)
            e2 = _open_immutable(_path(root, e2_path))
            for source, (path, size, digest) in sorted(e1_config.items()):
                _pin_current_file(database, root, run_id, f"e1_{source}", path, "frozen_fetch_lineage", (size, digest) if verify_source_pins else None)
                e1[source] = _open_immutable(_path(root, path))
            _pin_caches(database, root, run_id, read_caches, output_cache, verify_expected=verify_cache_pins)
            samples = database.execute("SELECT p.sample_id,s.candidate_json FROM sample_pairs p JOIN candidate_snapshots s USING(run_id,sample_id) WHERE p.run_id=? ORDER BY p.sample_rank", (run_id,)).fetchall()
            if len(samples) != TOTAL_COUNT:
                raise IdentityBandPrecisionError("prepare sample count drift")
            occurrences = 0
            for sample in samples:
                candidate = json.loads(str(sample["candidate_json"])); chosen = _choose_assets(v4, e2, candidate)
                for side in ("left", "right"):
                    source = str(candidate[f"{side}_source"]); building_id = str(candidate[f"{side}_source_building_id"])
                    if source not in e1:
                        raise IdentityBandPrecisionError(f"no E1 source pin for {source}")
                    for order, (asset_id, reason) in enumerate(chosen[side], 1):
                        occurrences += 1
                        if occurrences > MAX_ASSET_OCCURRENCES:
                            raise IdentityBandPrecisionError("asset occurrence cap 884 exceeded before HTTP")
                        asset_key = hashlib.sha256(f"{source}\0{asset_id}".encode()).hexdigest()
                        e2_asset, e1_asset = review_support._asset_row(e2, source, asset_id), review_support._e1_row(e1[source], asset_id)
                        status, error_kind, error_message = "pending", None, None
                        expected_url = expected_raw = expected_pixel = cache_role = cache_path = None
                        if e2_asset is None or e1_asset is None:
                            status, error_kind, error_message = "failed", "missing_frozen_lineage", "E1/E2 asset lineage is unavailable"
                        else:
                            expected_url, expected_raw, expected_pixel = e1_asset["fetch_url"], e1_asset["raw_response_sha256"], e1_asset["normalized_pixel_sha256"]
                            if (str(e1_asset["status"]), str(expected_raw), str(expected_pixel)) != ("success", str(e2_asset["raw_response_sha256"]), str(e2_asset["normalized_pixel_sha256"])):
                                raise IdentityBandPrecisionError(f"E1/E2 lineage mismatch: {source}/{asset_id}")
                            hit = _cache_hit(root, str(expected_pixel), read_caches)
                            if hit is not None:
                                cache_role, cached = hit; cache_path = _path_text(root, cached); status = "cache_hit"
                        fetch_payload = {"asset_key": asset_key, "source": source, "source_asset_id": asset_id, "expected_url": expected_url, "expected_raw_sha256": expected_raw, "expected_pixel_sha256": expected_pixel, "status": status, "cache_role": cache_role, "cache_path": cache_path, "error_kind": error_kind, "error_message": error_message}
                        existing = database.execute("SELECT * FROM fetch_assets WHERE run_id=? AND asset_key=?", (run_id, asset_key)).fetchone()
                        if existing is None:
                            database.execute("INSERT INTO fetch_assets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (run_id, asset_key, source, asset_id, expected_url, expected_raw, expected_pixel, status, cache_role, cache_path, None, None, None, None, None, 0, error_kind, error_message, _record(fetch_payload)))
                        else:
                            for key, value in (("source", source), ("source_asset_id", asset_id), ("expected_url", expected_url), ("expected_raw_sha256", expected_raw), ("expected_pixel_sha256", expected_pixel)):
                                if existing[key] != value:
                                    raise IdentityBandPrecisionError(f"global asset lineage collision: {asset_key}/{key}")
                        occurrence_payload = {"sample_id": str(sample["sample_id"]), "side": side, "asset_order": order, "asset_key": asset_key, "source": source, "source_building_id": building_id, "source_asset_id": asset_id, "selection_reason": reason}
                        database.execute("INSERT INTO sample_assets VALUES(?,?,?,?,?,?,?,?,?,?)", (run_id, str(sample["sample_id"]), side, order, asset_key, source, building_id, asset_id, reason, _record(occurrence_payload)))
            database.commit()
        else:
            occurrences = int(database.execute("SELECT count(*) FROM sample_assets WHERE run_id=?", (run_id,)).fetchone()[0])
            if occurrences > MAX_ASSET_OCCURRENCES or occurrences == 0:
                raise IdentityBandPrecisionError("resumed asset-plan accounting is invalid")
            if not database.execute("SELECT 1 FROM cache_pins WHERE run_id=? AND role='wp25_output'", (run_id,)).fetchone():
                raise IdentityBandPrecisionError("resumed prepare lacks cache pins")
        pending = [dict(row) for row in database.execute("SELECT * FROM fetch_assets WHERE run_id=? AND status='pending' ORDER BY asset_key", (run_id,))]
        existing_logical = int(database.execute("SELECT count(*) FROM fetch_attempts WHERE run_id=?", (run_id,)).fetchone()[0])
        existing_wire = int(database.execute("SELECT coalesce(sum(request_count),0) FROM fetch_attempts WHERE run_id=?", (run_id,)).fetchone()[0])
        if len(pending) + existing_logical > MAX_LOGICAL_HTTP_FETCHES:
            raise IdentityBandPrecisionError("logical HTTP cap 884 exceeded before network")
        if recovering_ready and pending:
            raise IdentityBandPrecisionError("review-ready recovery cannot contain pending HTTP rows")
        database.commit()
        if pending:
            _reserve_fetch_attempts(database, run_id, pending)
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(_fetch_one, row, fetcher) for row in pending]
                for future in as_completed(futures):
                    outcome = future.result()
                    current_wire = int(database.execute("SELECT coalesce(sum(request_count),0) FROM fetch_attempts WHERE run_id=?", (run_id,)).fetchone()[0])
                    if outcome.request_count > 1 or outcome.redirects or current_wire > MAX_WIRE_HTTP_REQUESTS:
                        raise IdentityBandPrecisionError("redirect/retry/wire cap invariant failed")
                    _persist_fetch_outcome(database, root, run_id, output_cache, outcome)
        logical_total = int(database.execute("SELECT count(*) FROM fetch_attempts WHERE run_id=?", (run_id,)).fetchone()[0])
        wire = int(database.execute("SELECT coalesce(sum(request_count),0) FROM fetch_attempts WHERE run_id=?", (run_id,)).fetchone()[0])
        if logical_total > MAX_LOGICAL_HTTP_FETCHES or wire > MAX_WIRE_HTTP_REQUESTS:
            raise IdentityBandPrecisionError("terminal HTTP accounting exceeds hard caps")

        for spec in read_caches:
            current = cache_tree(_path(root, spec.path))
            before = database.execute("SELECT file_count_before,total_bytes_before,tree_sha256_before FROM cache_pins WHERE run_id=? AND role=?", (run_id, spec.role)).fetchone()
            if current != (int(before[0]), int(before[1]), str(before[2])):
                raise IdentityBandPrecisionError(f"read-cache drift during prepare: {spec.role}")
        output_tree = cache_tree(output_cache)
        output_pin = database.execute("SELECT * FROM cache_pins WHERE run_id=? AND role='wp25_output'", (run_id,)).fetchone()
        output_payload = {"role": "wp25_output", "path": output_pin["path"], "transform_version": output_pin["transform_version"], "access_mode": "write_only", "file_count_before": output_pin["file_count_before"], "total_bytes_before": output_pin["total_bytes_before"], "tree_sha256_before": output_pin["tree_sha256_before"], "file_count_after": output_tree[0], "total_bytes_after": output_tree[1], "tree_sha256_after": output_tree[2]}
        database.execute("UPDATE cache_pins SET file_count_after=?,total_bytes_after=?,tree_sha256_after=?,record_sha256=? WHERE run_id=? AND role='wp25_output'", (*output_tree, _record(output_payload), run_id))
        forms = {lane: _form_payload(run_id, lane, _blind_lane_items(database, root, run_id, lane, sheet_root)) for lane in ("r1", "r2")}
        if {item["opaque_review_id"] for item in forms["r1"]["items"]} & {item["opaque_review_id"] for item in forms["r2"]["items"]}:
            raise IdentityBandPrecisionError("review opaque namespaces overlap")
        _atomic_json(r1_form, forms["r1"], allow_identical=True); _atomic_json(r2_form, forms["r2"], allow_identical=True)
        _verify_sample_inputs(database, root, run_id)
        if sha256_file(v4_path) != v4_before:
            raise IdentityBandPrecisionError("v4 drift during prepare")
        unique_assets = int(database.execute("SELECT count(*) FROM fetch_assets WHERE run_id=?", (run_id,)).fetchone()[0])
        if not recovering_ready:
            database.execute("UPDATE runs SET status='review_ready',asset_occurrence_count=?,unique_asset_count=?,logical_http_fetches=?,wire_http_requests=? WHERE run_id=?", (occurrences, unique_assets, logical_total, wire, run_id))
            _refresh_run_record(database, run_id); _insert_event(database, run_id, "fetching", "review_ready", {"rows_per_lane": TOTAL_COUNT, "asset_occurrences": occurrences, "unique_assets": unique_assets, "logical_http_fetches": logical_total, "wire_http_requests": wire})
        database.commit()
        fetch_rows = [dict(row) for row in database.execute("SELECT asset_key,source,source_asset_id,status,cache_role,cache_path,request_count,error_kind FROM fetch_assets WHERE run_id=? ORDER BY asset_key", (run_id,))]
        cache_rows = [dict(row) for row in database.execute("SELECT role,path,transform_version,access_mode,file_count_before,total_bytes_before,tree_sha256_before,file_count_after,total_bytes_after,tree_sha256_after FROM cache_pins WHERE run_id=? ORDER BY role", (run_id,))]
        _atomic_json(fetch_manifest, {"status": "PASS", "run_id": run_id, "review_version": REVIEW_VERSION, "rows_per_lane": TOTAL_COUNT, "asset_occurrence_count": occurrences, "unique_asset_count": unique_assets, "logical_http_fetches": logical_total, "wire_http_requests": wire, "hard_caps": {"asset_occurrences": 884, "logical_http_fetches": 884, "wire_http_requests": 884, "redirects": 0, "retries": 0}, "v4_sha256_before": v4_before, "v4_sha256_after": sha256_file(v4_path), "cache_pins": cache_rows, "fetch_assets": fetch_rows, "forms": {"r1": {"path": _path_text(root, r1_form), "sha256": sha256_file(r1_form)}, "r2": {"path": _path_text(root, r2_form), "sha256": sha256_file(r2_form)}}, "sheet_count": TOTAL_COUNT * 2, "vision_requests": 0, "llm_requests": 0}, allow_identical=True)
        _atomic_json(fetch_report, {"status": "PASS", "run_id": run_id, "counts": dict(Counter(row["status"] for row in fetch_rows)), "network": {"logical": logical_total, "wire": wire, "redirects": 0, "retries": 0}, "pixel_drift_count": sum(row["status"] == "source_changed" for row in fetch_rows)}, allow_identical=True)
        return PrepareResult(artifact, run_id, occurrences, unique_assets, logical_total, wire, sum(row["status"] == "cache_hit" for row in fetch_rows), TOTAL_COUNT * 2)
    except BaseException as exc:
        database.rollback()
        if run_id:
            try:
                current = database.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
                if current and str(current[0]) == "fetching":
                    _terminalize_reserved_fetches(
                        database, run_id,
                        reason=f"{type(exc).__name__}: {exc}",
                    )
                    database.execute("UPDATE runs SET status='failed' WHERE run_id=?", (run_id,)); _refresh_run_record(database, run_id)
                    failure = {"status": "FAIL", "run_id": run_id, "phase": "prepare", "error_kind": type(exc).__name__, "error_message": str(exc)[:2000], "structural_failure": True}
                    _insert_event(database, run_id, str(current[0]), "failed", failure); database.commit()
                    if not fetch_report.exists():
                        _atomic_json(fetch_report, failure)
            except sqlite3.Error:
                database.rollback()
        raise
    finally:
        for connection in e1.values(): connection.close()
        if e2 is not None: e2.close()
        if v4 is not None: v4.close()
        database.close()


def _normalized_actor(value: Any) -> str:
    actor = " ".join(str(value or "").split()).casefold()
    if not actor:
        raise IdentityBandPrecisionError("reviewer/adjudicator identity is required")
    return actor


def _validate_completed_review_form(connection: sqlite3.Connection, root: Path,
                                    run_id: str, lane: str,
                                    form: Mapping[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
    if form.get("review_version") != REVIEW_VERSION or form.get("run_id") != run_id or form.get("lane") != lane:
        raise IdentityBandPrecisionError("review form run/version/lane mismatch")
    reviewer = str(form.get("reviewer", "")).strip(); _normalized_actor(reviewer)
    reviewed_at = str(form.get("reviewed_at", "")).strip() or utc_now()
    raw_items = form.get("items")
    if not isinstance(raw_items, list) or len(raw_items) != TOTAL_COUNT:
        raise IdentityBandPrecisionError("completed reviewer form must contain 221 rows")
    expected = {str(row["opaque_review_id"]): dict(row) for row in connection.execute("SELECT * FROM review_items WHERE run_id=? AND lane=?", (run_id, lane))}
    if len(expected) != TOTAL_COUNT:
        raise IdentityBandPrecisionError("review-item ledger is incomplete")
    seen: set[str] = set(); validated: list[dict[str, Any]] = []; contract_items: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, Mapping):
            raise IdentityBandPrecisionError("review row must be an object")
        opaque = str(item.get("opaque_review_id", ""))
        if opaque in seen or opaque not in expected:
            raise IdentityBandPrecisionError("unknown or duplicate opaque review ID")
        seen.add(opaque); frozen = expected[opaque]
        if int(item.get("display_order", -1)) != int(frozen["display_order"]) or item.get("sheet_path") != frozen["sheet_path"] or item.get("sheet_sha256") != frozen["sheet_sha256"]:
            raise IdentityBandPrecisionError("review sheet binding changed")
        sheet = _resolve_text(root, str(frozen["sheet_path"]))
        if not sheet.is_file() or sha256_file(sheet) != str(frozen["sheet_sha256"]):
            raise IdentityBandPrecisionError("review sheet missing or changed")
        label, rationale = str(item.get("label", "")).strip(), str(item.get("rationale", "")).strip()
        hard_conflict = str(item.get("hard_conflict_observed", "")).strip()
        if label not in ALLOWED_LABELS or not rationale or not hard_conflict:
            raise IdentityBandPrecisionError("review row is incomplete or invalid")
        contract_items.append({"display_order": int(frozen["display_order"]), "opaque_review_id": opaque, "sheet_path": str(frozen["sheet_path"]), "sheet_sha256": str(frozen["sheet_sha256"])})
        validated.append({"display_order": int(frozen["display_order"]), "opaque_review_id": opaque, "sample_id": str(frozen["sample_id"]), "label": label, "rationale": rationale, "hard_conflict_observed": hard_conflict})
    if seen != set(expected):
        raise IdentityBandPrecisionError("review form omits frozen rows")
    contract_items.sort(key=lambda item: item["display_order"])
    contract = {"review_version": REVIEW_VERSION, "run_id": run_id, "lane": lane, "items": contract_items}
    if form.get("form_contract_sha256") != canonical_sha256(contract):
        raise IdentityBandPrecisionError("review form contract SHA mismatch")
    return reviewer, reviewed_at, sorted(validated, key=lambda item: item["display_order"])


def _require_quota_phase(connection: sqlite3.Connection, run_id: str, phase: str) -> None:
    rows = _validate_quota_ledger(connection, run_id)
    row = connection.execute("SELECT * FROM quota_observations WHERE run_id=? AND phase=? ORDER BY observation_no DESC LIMIT 1", (run_id, phase)).fetchone()
    if (row is None or not rows or str(rows[-1]["phase"]) != phase
            or not int(row["start_allowed"]) or int(row["stop_required"])):
        raise IdentityBandPrecisionError(f"missing/pending quota gate for {phase}")
    _quota_time(row["observed_at"])


def _require_global_not_stopped(connection: sqlite3.Connection, run_id: str) -> None:
    rows = _validate_quota_ledger(connection, run_id)
    row = rows[-1] if rows else None
    if row is None or int(row["stop_required"]):
        raise IdentityBandPrecisionError("latest global quota observation requires all review activity to stop")


def lock_review(*, repo_root: Path, lane: str, completed_form_path: Path,
                locked_output_path: Path, artifact_path: Path = DEFAULT_OUTPUT) -> Path:
    """Freeze one complete reviewer ledger without exposing sample IDs."""
    if lane not in {"r1", "r2"}:
        raise IdentityBandPrecisionError("lane must be r1 or r2")
    root = repo_root.resolve(); artifact = _path(root, artifact_path)
    completed, locked = _path(root, completed_form_path), _path(root, locked_output_path)
    if not completed.is_file():
        raise IdentityBandPrecisionError("completed review form is missing")
    if completed.resolve() == locked.resolve():
        raise IdentityBandPrecisionError("completed and locked review paths must be distinct")
    form = json.loads(completed.read_text(encoding="utf-8"))
    connection = sqlite3.connect(artifact); connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        run = connection.execute("SELECT * FROM runs").fetchone()
        if run is None:
            raise IdentityBandPrecisionError("review run is missing")
        run_id = str(run["run_id"])
        blank_form, blank_stage = _artifact_path_row(connection, root, run_id, f"{lane}_blank_form")
        if blank_form.resolve() == completed.resolve():
            raise IdentityBandPrecisionError("completed reviewer form must be distinct from its blank form")
        if not blank_form.is_file() or sha256_file(blank_form) == sha256_file(completed):
            raise IdentityBandPrecisionError("completed reviewer form bytes must differ from its blank form")
        existing_lock = connection.execute("SELECT * FROM review_locks WHERE run_id=? AND lane=?", (run_id, lane)).fetchone()
        if existing_lock is not None:
            bound = _resolve_text(root, str(existing_lock["locked_output_path"]))
            stored = connection.execute("SELECT * FROM review_lock_payloads WHERE run_id=? AND lane=?", (run_id, lane)).fetchone()
            if stored is None:
                raise IdentityBandPrecisionError("review-lock recovery payload is missing")
            payload = json.loads(str(stored["payload_json"])); raw = _json_bytes(payload)
            expected_sha = hashlib.sha256(raw).hexdigest()
            if (bound.resolve() != locked.resolve()
                    or _resolve_text(root, str(existing_lock["completed_form_path"])).resolve() != completed.resolve()
                    or sha256_file(completed) != str(existing_lock["completed_form_sha256"])
                    or expected_sha != str(existing_lock["locked_output_sha256"])):
                raise IdentityBandPrecisionError("idempotent review-lock recovery binding mismatch")
            for role, expected_path in ((f"{lane}_blank_form", blank_form),
                                        (f"{lane}_completed_form", completed)):
                binding = connection.execute(
                    "SELECT * FROM artifact_bindings WHERE run_id=? AND role=?", (run_id, role)
                ).fetchone()
                if (binding is None
                        or _resolve_text(root, str(binding["path"])).resolve() != expected_path.resolve()
                        or not expected_path.is_file()
                        or expected_path.stat().st_size != int(binding["size_bytes"])
                        or sha256_file(expected_path) != str(binding["sha256"])):
                    raise IdentityBandPrecisionError(f"review form binding mismatch: {role}")
            locked_binding = connection.execute(
                "SELECT * FROM artifact_bindings WHERE run_id=? AND role=?",
                (run_id, f"{lane}_locked")
            ).fetchone()
            if (locked_binding is None
                    or _resolve_text(root, str(locked_binding["path"])).resolve() != bound.resolve()
                    or int(locked_binding["size_bytes"]) != len(raw)
                    or str(locked_binding["sha256"]) != expected_sha):
                raise IdentityBandPrecisionError("review locked-output binding mismatch")
            _atomic_bytes(bound, raw, allow_identical=True)
            if sha256_file(bound) != expected_sha:
                raise IdentityBandPrecisionError("recovered reviewer lock SHA mismatch")
            return bound
        _require_global_not_stopped(connection, run_id)
        _require_quota_phase(connection, run_id, f"pre_{lane}_review")
        if str(run["status"]) not in {"review_ready", "reviews_partial"}:
            raise IdentityBandPrecisionError("review lock requires review_ready/reviews_partial")
        reviewer, reviewed_at, rows = _validate_completed_review_form(connection, root, run_id, lane, form)
        other = connection.execute("SELECT reviewer FROM review_locks WHERE run_id=? AND lane<>?", (run_id, lane)).fetchone()
        if other is not None and _normalized_actor(other[0]) == _normalized_actor(reviewer):
            raise IdentityBandPrecisionError("reviewers must be distinct")
        ledger: list[dict[str, Any]] = []
        for row in rows:
            payload = {"lane": lane, "opaque_review_id": row["opaque_review_id"], "sample_id": row["sample_id"], "label": row["label"], "rationale": row["rationale"], "hard_conflict_observed": row["hard_conflict_observed"], "reviewer": reviewer, "reviewed_at": reviewed_at}
            connection.execute("INSERT INTO blind_reviews VALUES(?,?,?,?,?,?,?,?,?,?)", (run_id, lane, row["opaque_review_id"], row["sample_id"], row["label"], row["rationale"], row["hard_conflict_observed"], reviewer, reviewed_at, _record(payload)))
            ledger.append({key: value for key, value in payload.items() if key != "sample_id"})
        ledger_sha = canonical_sha256(ledger)
        locked_payload = {"review_version": REVIEW_VERSION, "run_id": run_id, "lane": lane, "reviewer": reviewer, "reviewed_at": reviewed_at, "row_count": len(rows), "ordered_ledger_sha256": ledger_sha, "items": ledger}
        locked_json = canonical_json(locked_payload); locked_raw = _json_bytes(locked_payload)
        locked_sha = hashlib.sha256(locked_raw).hexdigest(); locked_at = utc_now()
        _bind_artifact(connection, root, run_id, f"{lane}_blank_form", blank_form, blank_stage)
        _register_artifact_path(connection, root, run_id, f"{lane}_completed_form", completed, "lock_review")
        _bind_artifact(connection, root, run_id, f"{lane}_completed_form", completed, "lock_review")
        _register_artifact_path(connection, root, run_id, f"{lane}_locked", locked, "lock_review")
        stored_payload = {"lane": lane, "payload_json": locked_json}
        connection.execute("INSERT INTO review_lock_payloads VALUES(?,?,?,?)",
                           (run_id, lane, locked_json, _record(stored_payload)))
        lock_payload = {"lane": lane, "reviewer": reviewer, "row_count": len(rows), "completed_form_path": _path_text(root, completed), "completed_form_sha256": sha256_file(completed), "locked_output_path": _path_text(root, locked), "locked_output_sha256": locked_sha, "ordered_ledger_sha256": ledger_sha, "locked_at": locked_at}
        connection.execute("INSERT INTO review_locks VALUES(?,?,?,?,?,?,?,?,?,?,?)", (run_id, lane, reviewer, len(rows), lock_payload["completed_form_path"], lock_payload["completed_form_sha256"], lock_payload["locked_output_path"], lock_payload["locked_output_sha256"], ledger_sha, locked_at, _record(lock_payload)))
        _bind_artifact_values(connection, root, run_id, f"{lane}_locked", locked,
                              "lock_review", len(locked_raw), locked_sha)
        count = int(connection.execute("SELECT count(*) FROM review_locks WHERE run_id=?", (run_id,)).fetchone()[0])
        before, after = str(run["status"]), "reviews_locked" if count == 2 else "reviews_partial"
        connection.execute(f"UPDATE runs SET status=?,{lane}_status='locked' WHERE run_id=?", (after, run_id)); _refresh_run_record(connection, run_id)
        _insert_event(connection, run_id, before, after, {"lane": lane, "row_count": len(rows), "ordered_ledger_sha256": ledger_sha})
        connection.commit()
        _atomic_bytes(locked, locked_raw, allow_identical=True)
        if sha256_file(locked) != locked_sha:
            raise IdentityBandPrecisionError("published reviewer lock SHA mismatch")
        return locked
    except Exception:
        connection.rollback(); raise
    finally:
        connection.close()


def unblind_reviews(*, repo_root: Path, artifact_path: Path = DEFAULT_OUTPUT,
                    packet_path: Path = DEFAULT_ADJUDICATION_PACKET,
                    adjudication_form_path: Path = DEFAULT_ADJUDICATION_FORM) -> tuple[Path, Path]:
    """Cross the unblind barrier only after both immutable 221-row locks."""
    root = repo_root.resolve(); artifact = _path(root, artifact_path)
    packet, form_path = _path(root, packet_path), _path(root, adjudication_form_path)
    if packet.resolve() == form_path.resolve():
        raise IdentityBandPrecisionError("unblind packet and blank form paths must be distinct")
    connection = sqlite3.connect(artifact); connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        run = connection.execute("SELECT * FROM runs").fetchone()
        if run is None:
            raise IdentityBandPrecisionError("review run is missing")
        run_id = str(run["run_id"])
        existing_unblind = connection.execute("SELECT * FROM unblind_artifacts WHERE run_id=?", (run_id,)).fetchone()
        if existing_unblind is not None:
            bound_packet = _resolve_text(root, str(existing_unblind["packet_path"])); bound_form = _resolve_text(root, str(existing_unblind["form_path"]))
            stored = connection.execute("SELECT * FROM unblind_payloads WHERE run_id=?", (run_id,)).fetchone()
            if stored is None:
                raise IdentityBandPrecisionError("unblind recovery payload is missing")
            packet_payload = json.loads(str(stored["packet_json"])); form_payload = json.loads(str(stored["form_json"]))
            packet_raw, form_raw = _json_bytes(packet_payload), _json_bytes(form_payload)
            packet_sha, form_sha = hashlib.sha256(packet_raw).hexdigest(), hashlib.sha256(form_raw).hexdigest()
            if (bound_packet.resolve() != packet.resolve() or bound_form.resolve() != form_path.resolve()
                    or packet_sha != str(existing_unblind["packet_sha256"])
                    or form_sha != str(existing_unblind["form_sha256"])):
                raise IdentityBandPrecisionError("idempotent unblind recovery binding mismatch")
            for role, path, raw, digest in (
                    ("unblind_packet", bound_packet, packet_raw, packet_sha),
                    ("adjudication_blank_form", bound_form, form_raw, form_sha)):
                binding = connection.execute(
                    "SELECT * FROM artifact_bindings WHERE run_id=? AND role=?", (run_id, role)
                ).fetchone()
                if (binding is None
                        or _resolve_text(root, str(binding["path"])).resolve() != path.resolve()
                        or int(binding["size_bytes"]) != len(raw)
                        or str(binding["sha256"]) != digest):
                    raise IdentityBandPrecisionError(f"unblind artifact binding mismatch: {role}")
            _atomic_bytes(bound_packet, packet_raw, allow_identical=True)
            _atomic_bytes(bound_form, form_raw, allow_identical=True)
            if sha256_file(bound_packet) != packet_sha or sha256_file(bound_form) != form_sha:
                raise IdentityBandPrecisionError("recovered unblind artifact SHA mismatch")
            return bound_packet, bound_form
        _require_global_not_stopped(connection, run_id)
        _require_quota_phase(connection, run_id, "pre_adjudication_review")
        if str(run["status"]) != "reviews_locked":
            raise IdentityBandPrecisionError("both reviewer lanes must be locked")
        locks = connection.execute("SELECT * FROM review_locks WHERE run_id=? ORDER BY lane", (run_id,)).fetchall()
        if len(locks) != 2 or {str(row["lane"]) for row in locks} != {"r1", "r2"} or any(int(row["row_count"]) != TOTAL_COUNT for row in locks):
            raise IdentityBandPrecisionError("two complete reviewer locks are required")
        if _normalized_actor(locks[0]["reviewer"]) == _normalized_actor(locks[1]["reviewer"]):
            raise IdentityBandPrecisionError("reviewers are not independent")
        for lock in locks:
            path = _resolve_text(root, str(lock["locked_output_path"]))
            if not path.is_file() or sha256_file(path) != str(lock["locked_output_sha256"]):
                raise IdentityBandPrecisionError("locked reviewer ledger missing or changed")
        if int(connection.execute("SELECT count(*) FROM blind_reviews WHERE run_id=?", (run_id,)).fetchone()[0]) != TOTAL_COUNT * 2:
            raise IdentityBandPrecisionError("blind-review rows are incomplete")
        samples = connection.execute("SELECT p.sample_rank,p.sample_id,p.stratum,p.candidate_id,s.* FROM sample_pairs p JOIN candidate_snapshots s USING(run_id,sample_id) WHERE p.run_id=? ORDER BY p.sample_rank", (run_id,)).fetchall()
        items: list[dict[str, Any]] = []; form_items: list[dict[str, Any]] = []
        for sample in samples:
            reviews = {str(row["lane"]): dict(row) for row in connection.execute("SELECT * FROM blind_reviews WHERE run_id=? AND sample_id=? ORDER BY lane", (run_id, sample["sample_id"]))}
            if set(reviews) != {"r1", "r2"}:
                raise IdentityBandPrecisionError("sample is missing a reviewer lane")
            items.append({"sample_rank": int(sample["sample_rank"]), "sample_id": str(sample["sample_id"]), "candidate_id": str(sample["candidate_id"]), "stratum": str(sample["stratum"]), "candidate": json.loads(str(sample["candidate_json"])), "left": json.loads(str(sample["left_metadata_json"])), "right": json.loads(str(sample["right_metadata_json"])), "families": json.loads(str(sample["families_json"])), "conflicts": json.loads(str(sample["conflicts_json"])), "policy_flags": json.loads(str(sample["policy_flags_json"])), "transition": json.loads(str(sample["transition_json"])), "reviews": {lane: {key: str(reviews[lane][key]) for key in ("opaque_review_id", "label", "rationale", "hard_conflict_observed", "reviewer")} for lane in ("r1", "r2")}})
            form_items.append({"sample_rank": int(sample["sample_rank"]), "sample_id": str(sample["sample_id"]), "candidate_id": str(sample["candidate_id"]), "r1_label": str(reviews["r1"]["label"]), "r2_label": str(reviews["r2"]["label"]), "adjudicator_label": "", "unresolved_disagreement": False, "rationale": ""})
        if len(items) != TOTAL_COUNT:
            raise IdentityBandPrecisionError("unblind packet must contain 221 rows")
        packet_payload = {"adjudication_version": ADJUDICATION_VERSION, "run_id": run_id, "row_count": TOTAL_COUNT, "items": items}
        contract = {"adjudication_version": ADJUDICATION_VERSION, "run_id": run_id, "items": [{key: item[key] for key in ("sample_rank", "sample_id", "candidate_id", "r1_label", "r2_label")} for item in form_items]}
        form_payload = {**contract, "form_contract_sha256": canonical_sha256(contract), "adjudicator": "", "adjudicated_at": "", "allowed_labels": sorted(ALLOWED_LABELS), "items": form_items}
        packet_json, form_json = canonical_json(packet_payload), canonical_json(form_payload)
        packet_raw, form_raw = _json_bytes(packet_payload), _json_bytes(form_payload)
        packet_sha, form_sha = hashlib.sha256(packet_raw).hexdigest(), hashlib.sha256(form_raw).hexdigest()
        _register_artifact_path(connection, root, run_id, "unblind_packet", packet, "unblind")
        _register_artifact_path(connection, root, run_id, "adjudication_blank_form", form_path, "unblind")
        payload_record = {"packet_json": packet_json, "form_json": form_json}
        connection.execute("INSERT INTO unblind_payloads VALUES(?,?,?,?)",
                           (run_id, packet_json, form_json, _record(payload_record)))
        created = utc_now()
        lock_map = {str(row["lane"]): row for row in locks}
        payload = {"packet_path": _path_text(root, packet), "packet_sha256": packet_sha, "form_path": _path_text(root, form_path), "form_sha256": form_sha, "r1_ledger_sha256": str(lock_map["r1"]["ordered_ledger_sha256"]), "r2_ledger_sha256": str(lock_map["r2"]["ordered_ledger_sha256"]), "created_at": created}
        connection.execute("INSERT INTO unblind_artifacts VALUES(?,?,?,?,?,?,?,?,?)", (run_id, payload["packet_path"], payload["packet_sha256"], payload["form_path"], payload["form_sha256"], payload["r1_ledger_sha256"], payload["r2_ledger_sha256"], created, _record(payload)))
        _bind_artifact_values(connection, root, run_id, "unblind_packet", packet,
                              "unblind", len(packet_raw), packet_sha)
        _bind_artifact_values(connection, root, run_id, "adjudication_blank_form", form_path,
                              "unblind", len(form_raw), form_sha)
        connection.execute("UPDATE runs SET status='unblinded',unblind_status='complete' WHERE run_id=?", (run_id,)); _refresh_run_record(connection, run_id)
        _insert_event(connection, run_id, "reviews_locked", "unblinded", {"rows": TOTAL_COUNT}); connection.commit()
        _atomic_bytes(packet, packet_raw, allow_identical=True)
        _atomic_bytes(form_path, form_raw, allow_identical=True)
        if sha256_file(packet) != packet_sha or sha256_file(form_path) != form_sha:
            raise IdentityBandPrecisionError("published unblind artifact SHA mismatch")
        return packet, form_path
    except Exception:
        connection.rollback(); raise
    finally:
        connection.close()


def clopper_pearson(successes: int, trials: int, alpha: float = 0.05) -> tuple[float, float]:
    """Two-sided exact binomial interval without a SciPy dependency."""
    if trials < 1 or not 0 <= successes <= trials or not 0 < alpha < 1:
        raise IdentityBandPrecisionError("invalid Clopper-Pearson arguments")
    def at_least(k: int, probability: float) -> float:
        return sum(math.comb(trials, value) * probability**value * (1 - probability) ** (trials - value) for value in range(k, trials + 1))
    def at_most(k: int, probability: float) -> float:
        return sum(math.comb(trials, value) * probability**value * (1 - probability) ** (trials - value) for value in range(k + 1))
    if successes == 0:
        lower = 0.0
    else:
        low, high = 0.0, 1.0
        for _ in range(100):
            middle = (low + high) / 2
            if at_least(successes, middle) < alpha / 2: low = middle
            else: high = middle
        lower = (low + high) / 2
    if successes == trials:
        upper = 1.0
    else:
        low, high = 0.0, 1.0
        for _ in range(100):
            middle = (low + high) / 2
            if at_most(successes, middle) > alpha / 2: low = middle
            else: high = middle
        upper = (low + high) / 2
    return lower, upper


def _validate_adjudication_form(connection: sqlite3.Connection, run_id: str,
                                form: Mapping[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
    if form.get("adjudication_version") != ADJUDICATION_VERSION or form.get("run_id") != run_id:
        raise IdentityBandPrecisionError("adjudication form run/version mismatch")
    adjudicator = str(form.get("adjudicator", "")).strip(); normalized = _normalized_actor(adjudicator)
    reviewers = {_normalized_actor(row[0]) for row in connection.execute("SELECT reviewer FROM review_locks WHERE run_id=?", (run_id,))}
    if len(reviewers) != 2 or normalized in reviewers:
        raise IdentityBandPrecisionError("adjudicator must be distinct from both reviewers")
    adjudicated_at = str(form.get("adjudicated_at", "")).strip() or utc_now()
    expected: dict[str, dict[str, Any]] = {}
    for row in connection.execute("SELECT p.sample_rank,p.sample_id,p.candidate_id,max(CASE WHEN b.lane='r1' THEN b.label END) r1_label,max(CASE WHEN b.lane='r2' THEN b.label END) r2_label FROM sample_pairs p JOIN blind_reviews b USING(run_id,sample_id) WHERE p.run_id=? GROUP BY p.sample_id ORDER BY p.sample_rank", (run_id,)):
        expected[str(row["sample_id"])] = dict(row)
    raw_items = form.get("items")
    if not isinstance(raw_items, list) or len(raw_items) != TOTAL_COUNT:
        raise IdentityBandPrecisionError("adjudication form must contain 221 rows")
    seen: set[str] = set(); rows: list[dict[str, Any]] = []; contract_items: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, Mapping):
            raise IdentityBandPrecisionError("adjudication row must be an object")
        sample_id = str(item.get("sample_id", ""))
        if sample_id in seen or sample_id not in expected:
            raise IdentityBandPrecisionError("unknown or duplicate adjudication sample")
        seen.add(sample_id); frozen = expected[sample_id]
        if int(item.get("sample_rank", -1)) != int(frozen["sample_rank"]) or item.get("candidate_id") != frozen["candidate_id"] or item.get("r1_label") != frozen["r1_label"] or item.get("r2_label") != frozen["r2_label"]:
            raise IdentityBandPrecisionError("adjudication form binding changed")
        label, rationale = str(item.get("adjudicator_label", "")).strip(), str(item.get("rationale", "")).strip()
        unresolved_raw = item.get("unresolved_disagreement")
        if not isinstance(unresolved_raw, bool) or label not in ALLOWED_LABELS or not rationale:
            raise IdentityBandPrecisionError("adjudication row is incomplete or invalid")
        agreement = frozen["r1_label"] == frozen["r2_label"]
        if agreement and unresolved_raw:
            raise IdentityBandPrecisionError("an agreeing pair cannot be unresolved")
        final_label = "insufficient" if (not agreement and unresolved_raw) else label
        contract_items.append({"sample_rank": int(frozen["sample_rank"]), "sample_id": sample_id, "candidate_id": str(frozen["candidate_id"]), "r1_label": str(frozen["r1_label"]), "r2_label": str(frozen["r2_label"])})
        rows.append({"sample_rank": int(frozen["sample_rank"]), "sample_id": sample_id, "candidate_id": str(frozen["candidate_id"]), "r1_label": str(frozen["r1_label"]), "r2_label": str(frozen["r2_label"]), "reviewer_agreement": int(agreement), "adjudicator_label": label, "final_gold_label": final_label, "unresolved_disagreement": int(unresolved_raw), "rationale": rationale})
    if seen != set(expected):
        raise IdentityBandPrecisionError("adjudication form omits frozen rows")
    contract_items.sort(key=lambda item: item["sample_rank"])
    contract = {"adjudication_version": ADJUDICATION_VERSION, "run_id": run_id, "items": contract_items}
    if form.get("form_contract_sha256") != canonical_sha256(contract):
        raise IdentityBandPrecisionError("adjudication form contract SHA mismatch")
    return adjudicator, adjudicated_at, sorted(rows, key=lambda item: item["sample_rank"])


def _grade(stratum: str, labels: Counter[str]) -> dict[str, Any]:
    n = STRATUM_QUOTAS[stratum]; same = labels["same"]; errors = n - same
    precision = same / n; low, high = clopper_pearson(same, n); equivalent = errors * 60.0 / n
    if stratum == "S5":
        grade = "r1_rule_guard"
    elif equivalent <= 1:
        grade = "bulk_candidate"
    elif equivalent < 5:
        grade = "agent_first_pass"
    else:
        grade = "direct_review"
    return {"stratum": stratum, "sample_n": n, "label_counts": {label: labels[label] for label in sorted(ALLOWED_LABELS)}, "same_count": same, "related_count": labels["related"], "different_count": labels["different"], "insufficient_count": labels["insufficient"], "precision": precision, "clopper_pearson_95": {"low": low, "high": high}, "error_count": errors, "equivalent_errors_60": equivalent, "candidate_grade": grade, "bulk_excluded": bool(labels["different"]), "status": "FAIL" if stratum == "S5" and same else "PASS"}


FINAL_ARTIFACT_ROLES = frozenset({
    "sample_manifest", "fetch_manifest", "fetch_report",
    "r1_blank_form", "r1_completed_form", "r1_locked",
    "r2_blank_form", "r2_completed_form", "r2_locked",
    "unblind_packet", "adjudication_blank_form", "completed_adjudication",
    "locked_adjudication", "final_report",
})


def _quota_payload(connection: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    rows = [dict(row) for row in connection.execute(
        "SELECT observation_no,phase,account_index,observed_at,signals_json,"
        "gate_basis_remaining_percent,start_allowed,stop_required,limitation "
        "FROM quota_observations WHERE run_id=? ORDER BY observation_no", (run_id,))]
    for row in rows:
        row["signals"] = json.loads(row.pop("signals_json"))
    return rows


def _artifact_path_row(connection: sqlite3.Connection, root: Path, run_id: str,
                       role: str) -> tuple[Path, str]:
    row = connection.execute(
        "SELECT path,stage FROM artifact_paths WHERE run_id=? AND role=?", (run_id, role)
    ).fetchone()
    if row is None:
        raise IdentityBandPrecisionError(f"artifact path is missing for role {role}")
    return _resolve_text(root, str(row["path"])), str(row["stage"])


def _final_manifest_payload(connection: sqlite3.Connection, root: Path, artifact: Path,
                            run_id: str, manifest: Path,
                            quota: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if run is None or str(run["status"]) not in {"pass", "fail"}:
        raise IdentityBandPrecisionError("final manifest requires a terminal run")
    manifest_path, _ = _artifact_path_row(connection, root, run_id, "final_manifest")
    if manifest_path.resolve() != manifest.resolve():
        raise IdentityBandPrecisionError("final manifest recovery path changed")
    binding_rows = [dict(row) for row in connection.execute(
        "SELECT role,path,size_bytes,sha256,stage FROM artifact_bindings "
        "WHERE run_id=? ORDER BY role", (run_id,))]
    roles = {str(row["role"]) for row in binding_rows}
    if not FINAL_ARTIFACT_ROLES.issubset(roles):
        missing = sorted(FINAL_ARTIFACT_ROLES - roles)
        raise IdentityBandPrecisionError(f"final artifact provenance is incomplete: {missing}")
    cache_pins = [dict(row) for row in connection.execute(
        "SELECT role,path,transform_version,access_mode,file_count_before,total_bytes_before,"
        "tree_sha256_before,file_count_after,total_bytes_after,tree_sha256_after "
        "FROM cache_pins WHERE run_id=? ORDER BY role", (run_id,))]
    status = str(run["status"]).upper()
    artifact_map = {str(row["role"]): {
        "path": str(row["path"]), "size_bytes": int(row["size_bytes"]),
        "sha256": str(row["sha256"]), "stage": str(row["stage"]),
    } for row in binding_rows}
    return {
        "status": status,
        "run_id": run_id,
        "versions": {"runner": RUNNER_VERSION, "sample": SAMPLE_VERSION,
                     "review": REVIEW_VERSION, "adjudication": ADJUDICATION_VERSION,
                     "gate": GATE_VERSION},
        "sidecar": {"path": _path_text(root, artifact),
                    "size_bytes": artifact.stat().st_size,
                    "sha256": sha256_file(artifact)},
        "manifest_path": _path_text(root, manifest),
        "sample_logical_sha256": str(run["sample_logical_sha256"]),
        "cache_pins": cache_pins,
        "artifacts": artifact_map,
        "artifact_provenance_sha256": canonical_sha256(artifact_map),
        "review_rows": {"r1": TOTAL_COUNT, "r2": TOTAL_COUNT, "adjudicated": TOTAL_COUNT},
        "quota_observations": list(quota),
        "network_requests": int(run["wire_http_requests"]),
        "vision_requests": 0,
        "llm_requests": 0,
        "merge_executions": 0,
        "vnext_id_issuances": 0,
        "measurement_only": True,
        "human_decision_required": True,
    }


def _terminal_result(connection: sqlite3.Connection, artifact: Path,
                     run_id: str) -> FinalizeResult:
    run = connection.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if run is None or str(run["status"]) not in {"pass", "fail"}:
        raise IdentityBandPrecisionError("terminal result is unavailable")
    s5 = connection.execute(
        "SELECT same_count FROM band_metrics WHERE run_id=? AND stratum='S5'", (run_id,)
    ).fetchone()
    if s5 is None:
        raise IdentityBandPrecisionError("terminal S5 metric is missing")
    disagreements = int(connection.execute(
        "SELECT count(*) FROM adjudications WHERE run_id=? AND reviewer_agreement=0", (run_id,)
    ).fetchone()[0])
    unresolved = int(connection.execute(
        "SELECT count(*) FROM adjudications WHERE run_id=? AND unresolved_disagreement=1", (run_id,)
    ).fetchone()[0])
    return FinalizeResult(artifact, run_id, str(run["status"]).upper(), int(s5[0]),
                          disagreements, unresolved)


def _publish_terminal_outputs(connection: sqlite3.Connection, root: Path, artifact: Path,
                              run_id: str, completed: Path, locked: Path,
                              report: Path, manifest: Path) -> FinalizeResult:
    stored = connection.execute("SELECT * FROM final_payloads WHERE run_id=?", (run_id,)).fetchone()
    if stored is None:
        raise IdentityBandPrecisionError("terminal recovery payload is missing")
    locked_payload = json.loads(str(stored["locked_adjudication_json"]))
    report_payload = json.loads(str(stored["report_json"]))
    recoverable = {
        "locked_adjudication": (locked, _json_bytes(locked_payload)),
        "final_report": (report, _json_bytes(report_payload)),
    }
    bindings = {str(row["role"]): row for row in connection.execute(
        "SELECT * FROM artifact_bindings WHERE run_id=?", (run_id,))}
    if not FINAL_ARTIFACT_ROLES.issubset(bindings):
        raise IdentityBandPrecisionError("terminal artifact bindings are incomplete")
    completed_binding = bindings["completed_adjudication"]
    blank_binding = bindings["adjudication_blank_form"]
    if (_resolve_text(root, str(completed_binding["path"])).resolve() != completed.resolve()
            or _resolve_text(root, str(blank_binding["path"])).resolve() == completed.resolve()
            or str(blank_binding["sha256"]) == str(completed_binding["sha256"])):
        raise IdentityBandPrecisionError("completed adjudication recovery binding mismatch")
    for role, binding in bindings.items():
        path = _resolve_text(root, str(binding["path"])); expected_size = int(binding["size_bytes"])
        expected_sha = str(binding["sha256"])
        if role in recoverable:
            target, raw = recoverable[role]
            if path.resolve() != target.resolve() or len(raw) != expected_size or hashlib.sha256(raw).hexdigest() != expected_sha:
                raise IdentityBandPrecisionError(f"terminal payload binding mismatch: {role}")
            continue
        if not path.is_file() or path.stat().st_size != expected_size or sha256_file(path) != expected_sha:
            raise IdentityBandPrecisionError(f"bound final artifact missing or changed: {role}")
    for role in ("locked_adjudication", "final_report"):
        target, raw = recoverable[role]
        _atomic_bytes(target, raw, allow_identical=True)
        if sha256_file(target) != str(bindings[role]["sha256"]):
            raise IdentityBandPrecisionError(f"recovered final artifact SHA mismatch: {role}")
    quota = _quota_payload(connection, run_id)
    manifest_payload = _final_manifest_payload(connection, root, artifact, run_id, manifest, quota)
    _atomic_json(manifest, manifest_payload, allow_identical=True)
    return _terminal_result(connection, artifact, run_id)


def finalize_review(*, repo_root: Path, completed_adjudication_path: Path,
                    artifact_path: Path = DEFAULT_OUTPUT,
                    locked_adjudication_path: Path = DEFAULT_ADJUDICATION_LOCKED,
                    report_path: Path = DEFAULT_REPORT,
                    manifest_path: Path = DEFAULT_MANIFEST) -> FinalizeResult:
    """Freeze gold and compute the immutable band grades and S5 rule gate."""
    root = repo_root.resolve(); artifact = _path(root, artifact_path)
    completed, locked = _path(root, completed_adjudication_path), _path(root, locked_adjudication_path)
    report, manifest = _path(root, report_path), _path(root, manifest_path)
    if not completed.is_file():
        raise IdentityBandPrecisionError("completed adjudication form is missing")
    if len({path.resolve() for path in (completed, locked, report, manifest)}) != 4:
        raise IdentityBandPrecisionError("completed, locked, report, and manifest paths must be distinct")
    connection = sqlite3.connect(artifact); connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        run = connection.execute("SELECT * FROM runs").fetchone()
        if run is None or str(run["status"]) not in {"unblinded", "pass", "fail"}:
            raise IdentityBandPrecisionError("finalize requires unblinded or terminal state")
        run_id = str(run["run_id"])
        if str(run["status"]) in {"pass", "fail"}:
            return _publish_terminal_outputs(connection, root, artifact, run_id, completed,
                                             locked, report, manifest)
        _require_global_not_stopped(connection, run_id)
        _require_quota_phase(connection, run_id, "pre_finalize")
        _verify_sample_inputs(connection, root, run_id)
        if _logical_sha(connection, run_id) != str(run["sample_logical_sha256"]):
            raise IdentityBandPrecisionError("frozen sample logical SHA drift")
        for pin in connection.execute("SELECT * FROM cache_pins WHERE run_id=?", (run_id,)):
            expected = (int(pin["file_count_after"]), int(pin["total_bytes_after"]), str(pin["tree_sha256_after"]))
            if cache_tree(_resolve_text(root, str(pin["path"]))) != expected:
                raise IdentityBandPrecisionError(f"cache drift at finalization: {pin['role']}")
        missing_sheets = 0
        for item in connection.execute("SELECT sheet_path,sheet_sha256 FROM review_items WHERE run_id=?", (run_id,)):
            path = _resolve_text(root, str(item["sheet_path"]))
            missing_sheets += int(not path.is_file() or sha256_file(path) != str(item["sheet_sha256"]))
        if missing_sheets:
            raise IdentityBandPrecisionError("review sheets are incomplete or changed")
        blank = connection.execute(
            "SELECT form_path,form_sha256 FROM unblind_artifacts WHERE run_id=?", (run_id,)
        ).fetchone()
        if (blank is None
                or _resolve_text(root, str(blank["form_path"])).resolve() == completed.resolve()
                or str(blank["form_sha256"]) == sha256_file(completed)):
            raise IdentityBandPrecisionError("completed adjudication must be distinct from the blank form")
        form = json.loads(completed.read_text(encoding="utf-8"))
        adjudicator, adjudicated_at, rows = _validate_adjudication_form(connection, run_id, form)
        locked_rows: list[dict[str, Any]] = []
        for row in rows:
            payload = {"sample_id": row["sample_id"], "r1_label": row["r1_label"], "r2_label": row["r2_label"], "reviewer_agreement": row["reviewer_agreement"], "adjudicator_label": row["adjudicator_label"], "final_gold_label": row["final_gold_label"], "unresolved_disagreement": row["unresolved_disagreement"], "rationale": row["rationale"], "adjudicator": adjudicator, "adjudicated_at": adjudicated_at}
            connection.execute("INSERT INTO adjudications VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (run_id, row["sample_id"], row["r1_label"], row["r2_label"], row["reviewer_agreement"], row["adjudicator_label"], row["final_gold_label"], row["unresolved_disagreement"], row["rationale"], adjudicator, adjudicated_at, _record(payload)))
            locked_rows.append({"candidate_id": row["candidate_id"], **payload})
        locked_payload = {"adjudication_version": ADJUDICATION_VERSION, "run_id": run_id, "adjudicator": adjudicator, "adjudicated_at": adjudicated_at, "row_count": TOTAL_COUNT, "items": locked_rows}
        metrics: dict[str, dict[str, Any]] = {}
        for stratum in STRATUM_QUOTAS:
            labels = Counter(str(row[0]) for row in connection.execute("SELECT a.final_gold_label FROM sample_pairs p JOIN adjudications a USING(run_id,sample_id) WHERE p.run_id=? AND p.stratum=?", (run_id, stratum)))
            if sum(labels.values()) != STRATUM_QUOTAS[stratum]:
                raise IdentityBandPrecisionError(f"{stratum} gold is incomplete")
            metric = _grade(stratum, labels); metrics[stratum] = metric
            connection.execute("INSERT INTO band_metrics VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (run_id, stratum, metric["sample_n"], metric["same_count"], metric["related_count"], metric["different_count"], metric["insufficient_count"], metric["precision"], metric["clopper_pearson_95"]["low"], metric["clopper_pearson_95"]["high"], metric["error_count"], metric["equivalent_errors_60"], metric["candidate_grade"], int(metric["bulk_excluded"]), metric["status"], _record(metric)))
        s5_same = metrics["S5"]["same_count"]; status = "FAIL" if s5_same else "PASS"
        disagreements = int(connection.execute("SELECT count(*) FROM adjudications WHERE run_id=? AND reviewer_agreement=0", (run_id,)).fetchone()[0])
        unresolved = int(connection.execute("SELECT count(*) FROM adjudications WHERE run_id=? AND unresolved_disagreement=1", (run_id,)).fetchone()[0])
        quota = _quota_payload(connection, run_id)
        fetch_counts = dict(Counter(str(row[0]) for row in connection.execute("SELECT status FROM fetch_assets WHERE run_id=?", (run_id,))))
        evidence_rows = [{"candidate_id": str(row["candidate_id"]), "stratum": str(row["stratum"]), "final_gold_label": str(row["final_gold_label"]), "r1_label": str(row["r1_label"]), "r2_label": str(row["r2_label"]), "unresolved_disagreement": bool(row["unresolved_disagreement"]), "adjudicator_rationale": str(row["rationale"])} for row in connection.execute("SELECT p.candidate_id,p.stratum,a.* FROM sample_pairs p JOIN adjudications a USING(run_id,sample_id) WHERE p.run_id=? ORDER BY p.sample_rank", (run_id,))]
        report_payload = {"status": status, "run_id": run_id, "measurement_only": True, "authorizes_tiering": False, "human_decision_required": True, "bands": metrics, "s5_rule_gate": {"status": metrics["S5"]["status"], "same_count": s5_same, "r1_six_year_rule_fail": bool(s5_same), "hold_s1_start": bool(s5_same)}, "review": {"r1_rows": TOTAL_COUNT, "r2_rows": TOTAL_COUNT, "adjudicated_rows": TOTAL_COUNT, "disagreements": disagreements, "unresolved_disagreements": unresolved}, "evidence_memo": evidence_rows, "fetch": {"asset_occurrences": int(run["asset_occurrence_count"]), "unique_assets": int(run["unique_asset_count"]), "logical_http_fetches": int(run["logical_http_fetches"]), "wire_http_requests": int(run["wire_http_requests"]), "status_counts": fetch_counts, "redirects": 0, "retries": 0}, "quota_observations": quota, "vision_requests": 0, "llm_requests": 0, "merge_executions": 0, "vnext_id_issuances": 0}
        for role in ("sample_manifest", "fetch_manifest", "fetch_report",
                     "r1_blank_form", "r1_completed_form", "r1_locked",
                     "r2_blank_form", "r2_completed_form", "r2_locked",
                     "unblind_packet", "adjudication_blank_form"):
            path, stage = _artifact_path_row(connection, root, run_id, role)
            _bind_artifact(connection, root, run_id, role, path, stage)
        _register_artifact_path(connection, root, run_id, "completed_adjudication", completed, "finalize")
        _register_artifact_path(connection, root, run_id, "locked_adjudication", locked, "finalize")
        _register_artifact_path(connection, root, run_id, "final_report", report, "finalize")
        _register_artifact_path(connection, root, run_id, "final_manifest", manifest, "finalize")
        _bind_artifact(connection, root, run_id, "completed_adjudication", completed, "finalize")
        locked_raw, report_raw = _json_bytes(locked_payload), _json_bytes(report_payload)
        _bind_artifact_values(connection, root, run_id, "locked_adjudication", locked,
                              "finalize", len(locked_raw), hashlib.sha256(locked_raw).hexdigest())
        _bind_artifact_values(connection, root, run_id, "final_report", report,
                              "finalize", len(report_raw), hashlib.sha256(report_raw).hexdigest())
        locked_json, report_json = canonical_json(locked_payload), canonical_json(report_payload)
        final_stored = {"status": status, "locked_adjudication_json": locked_json,
                        "report_json": report_json}
        connection.execute("INSERT INTO final_payloads VALUES(?,?,?,?,?)",
                           (run_id, status, locked_json, report_json, _record(final_stored)))
        connection.execute("UPDATE runs SET status=?,adjudication_status='complete',gate_status=?,completed_at=? WHERE run_id=?", (status.casefold(), status.casefold(), utc_now(), run_id)); _refresh_run_record(connection, run_id)
        _insert_event(connection, run_id, "unblinded", status.casefold(), {"s5_same_count": s5_same, "bands": metrics}); connection.commit()
        return _publish_terminal_outputs(connection, root, artifact, run_id, completed,
                                         locked, report, manifest)
    except Exception:
        connection.rollback(); raise
    finally:
        connection.close()


__all__ = ["BuildResult", "CacheSpec", "FinalizeResult", "IdentityBandPrecisionError", "PrepareResult", "build_sample", "cache_tree", "clopper_pearson", "finalize_review", "lock_review", "prepare_review", "record_quota_observation", "unblind_reviews"]
