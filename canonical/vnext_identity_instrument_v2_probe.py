"""WP2.7 identity-instrument v2 contrast producer.

The producer replays the immutable WP2.6 sample, sheets, and asset lineage.
It never fetches or renders evidence.  Reviewers and the adjudicator enter only
six controlled observations plus a free-text basis; labels are derived by the
producer from the final locked observations.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping, Sequence

from canonical.cross_source_image_selection import canonical_json, canonical_sha256


SCHEMA_VERSION = 1
APPLICATION_ID = 0x57503237  # WP27
RUNNER_VERSION = "vnext-identity-instrument-v2-probe-v1.0.0"
INSTRUMENT_VERSION = "vnext-identity-instrument-v2"
SAMPLE_VERSION = "vnext-identity-instrument-v2-probe-v1"
REVIEW_VERSION = "vnext-identity-instrument-v2-review-v1"
ADJUDICATION_VERSION = "vnext-identity-instrument-v2-axis-adjudication-v1"
GATE_VERSION = "vnext-identity-instrument-v2-predeclared-gate-v1"

SOURCE_SAMPLE_VERSION = "vnext-identity-instrument-probe-v1"
SOURCE_SEED = "vnext-wp26-instrument-probe-v1-20260827"
SOURCE_LOGICAL_SHA256 = (
    "784a567458227a737c78b31320eb79fdfbca0e7cf5909d08bb87c38f12e2b208"
)
SOURCE_RUN_ID = "wp26-250a8cefe5396b87377ad55d"
SOURCE_PACKET_BINDING_SHA256 = (
    "b1f0491852d68648405c032e1a8486ed41a939b18eb655cea1aab92ab07b39e5"
)
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
TOTAL_COUNT = 24
SOURCE_SHEET_COUNT = 48
SOURCE_ASSET_OCCURRENCES = 93
MAX_ASSETS_PER_SIDE = 2
MAX_ASSET_OCCURRENCES = 96
MAX_LOGICAL_HTTP_FETCHES = 96
MAX_WIRE_HTTP_REQUESTS = 96

OBSERVATION_KEYS = ("image", "name", "firm", "place", "year", "scope", "basis")
CONTROLLED_KEYS = OBSERVATION_KEYS[:-1]
CONTROLLED_VALUES: Mapping[str, tuple[str, ...]] = {
    "image": ("same_object", "different_objects", "inconclusive"),
    "name": ("agree", "alias", "conflict", "unclear"),
    "firm": ("agree", "overlap", "conflict", "unknown"),
    "place": ("agree", "unclear", "conflict"),
    "year": ("compatible", "conflict", "unclear"),
    "scope": (
        "same", "component_vs_umbrella", "phase_or_extension", "unclear", "unrelated"
    ),
}
FORBIDDEN_INPUT_FIELDS = frozenset(
    {"label", "allowed_labels", "final_label", "adjudicator_label", "rationale",
     "hard_conflict_observed", "derived_label", "derivation_rule"}
)
ALLOWED_LABELS = frozenset({"same", "related", "different", "insufficient"})

QUOTA_PHASES = (
    "pre_freeze", "pre_r1_review", "pre_r2_review",
    "pre_adjudication_review", "pre_reveal_score",
)
QUOTA_INDEX = {phase: index for index, phase in enumerate(QUOTA_PHASES)}
MAX_QUOTA_AGE = timedelta(hours=24)
MAX_QUOTA_FUTURE_SKEW = timedelta(minutes=5)

STATE_SEQUENCE = (
    "instrument_frozen", "sample_frozen", "criteria_frozen", "review_ready",
    "reviews_partial", "reviews_locked", "adjudication_locked", "pass", "fail",
)

DEFAULT_PROTOCOL = Path("docs/vnext/IDENTITY_INSTRUMENT_V2_PROTOCOL.md")
DEFAULT_ORDER = Path(".claude/ops/jobs/20260827_vnext_wp27_instrument_v2_order.md")
DEFAULT_SOURCE_DB = Path("data/enrichment/vnext_identity_instrument_probe_v1.db")
DEFAULT_SOURCE_SAMPLE_MANIFEST = Path(
    "data/reports/vnext_identity_instrument_probe_v1_sample_manifest.json"
)
DEFAULT_SOURCE_REPORT = Path("data/reports/vnext_identity_instrument_probe_v1_report.json")
DEFAULT_SOURCE_MANIFEST = Path("data/reports/vnext_identity_instrument_probe_v1_manifest.json")
DEFAULT_SOURCE_FETCH_MANIFEST = Path(
    "data/reports/vnext_identity_instrument_probe_v1_fetch_manifest.json"
)
DEFAULT_SOURCE_FETCH_CORRECTION = Path(
    "data/reports/vnext_identity_instrument_probe_v1_fetch_cap_correction.json"
)
DEFAULT_SOURCE_PRODUCER = Path("canonical/vnext_identity_instrument_probe.py")
DEFAULT_SOURCE_ADJUDICATION_LOCKED = Path(
    "data/reports/vnext_identity_instrument_probe_v1_adjudication_locked.json"
)

FIXED_INPUT_SPECS: Mapping[str, tuple[int, str]] = {
    "wp27_protocol": PROTOCOL_SPEC,
    "wp27_order": ORDER_SPEC,
    "wp26_source_db": (
        716_800,
        "a0949e7d75069d869e665a10872fe13a3988897cfc810d36149508950594347a",
    ),
    "wp26_sample_manifest": (
        20_523,
        "6e925f7f07c21fe29d66c0e92a498db81616b189a2b33d5cf2520281d20df06a",
    ),
    "wp26_final_report": (
        19_671,
        "b1c04f394488624d248ce31965a6fbd01345bb5ffb51cfdfa2f9053e925980ab",
    ),
    "wp26_final_manifest": (
        11_232,
        "162be33c250638248e3b03f80644adb883a7a562cba0a36b99fde350c801859f",
    ),
    "wp26_fetch_manifest": (
        45_145,
        "36c2faef5d925dcab5fbacb827e48a5c7336ebdce9cbf8b6f7ec5dc90139f12a",
    ),
    "wp26_fetch_correction": (
        2_523,
        "3a2540bb0ab3adf79cd119bfdd42f769c6a21bf8817ab4600e5d033e7c4301c5",
    ),
    "wp26_producer": (
        122_514,
        "53ab52464aa0990e0748c1177544007ba97a8930a98d3e16263a72c5d3e93449",
    ),
    "wp26_adjudication_locked": (
        13_415,
        "9bc4f38a4e18933e3ab77fc3e812ab29dd1bd7c4d79af30da760dab27c42a80a",
    ),
}

DEFAULT_OUTPUT = Path("data/enrichment/vnext_identity_instrument_v2_probe_v1.db")
DEFAULT_SAMPLE_MANIFEST = Path(
    "data/reports/vnext_identity_instrument_v2_probe_v1_sample_manifest.json"
)
DEFAULT_FETCH_MANIFEST = Path(
    "data/reports/vnext_identity_instrument_v2_probe_v1_fetch_manifest.json"
)
DEFAULT_FETCH_REPORT = Path(
    "data/reports/vnext_identity_instrument_v2_probe_v1_fetch_report.json"
)
DEFAULT_R1_FORM = Path(
    "data/reports/vnext_identity_instrument_v2_probe_v1_reviewer_r1_draft.json"
)
DEFAULT_R2_FORM = Path(
    "data/reports/vnext_identity_instrument_v2_probe_v1_reviewer_r2_draft.json"
)
DEFAULT_R1_LOCKED = Path(
    "data/reports/vnext_identity_instrument_v2_probe_v1_reviewer_r1_locked.json"
)
DEFAULT_R2_LOCKED = Path(
    "data/reports/vnext_identity_instrument_v2_probe_v1_reviewer_r2_locked.json"
)
DEFAULT_ADJUDICATION_PACKET = Path(
    "data/reports/vnext_identity_instrument_v2_probe_v1_adjudication_packet.json"
)
DEFAULT_ADJUDICATION_FORM = Path(
    "data/reports/vnext_identity_instrument_v2_probe_v1_adjudication_draft.json"
)
DEFAULT_ADJUDICATION_LOCKED = Path(
    "data/reports/vnext_identity_instrument_v2_probe_v1_adjudication_locked.json"
)
DEFAULT_REPORT = Path("data/reports/vnext_identity_instrument_v2_probe_v1_report.json")
DEFAULT_MANIFEST = Path("data/reports/vnext_identity_instrument_v2_probe_v1_manifest.json")


class IdentityInstrumentV2Error(RuntimeError):
    """A fail-closed WP2.7 contract violation."""


ERROR_BASE = IdentityInstrumentV2Error


@dataclass(frozen=True)
class FreezeResult:
    artifact_path: Path
    run_id: str
    status: str


@dataclass(frozen=True)
class SampleResult:
    artifact_path: Path
    manifest_path: Path
    run_id: str
    sample_count: int
    sheet_count: int
    asset_occurrence_count: int


@dataclass(frozen=True)
class PrepareResult:
    artifact_path: Path
    run_id: str
    form_paths: tuple[Path, Path]


@dataclass(frozen=True)
class RevealResult:
    artifact_path: Path
    report_path: Path
    manifest_path: Path
    run_id: str
    status: str


SCHEMA_SQL = r"""
CREATE TABLE runs(
 run_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL, runner_version TEXT NOT NULL,
 instrument_version TEXT NOT NULL, sample_version TEXT NOT NULL,
 review_version TEXT NOT NULL, adjudication_version TEXT NOT NULL,
 gate_version TEXT NOT NULL, source_sample_version TEXT NOT NULL,
 source_seed TEXT NOT NULL, source_logical_sha256 TEXT NOT NULL,
 source_run_id TEXT NOT NULL, source_packet_binding_sha256 TEXT NOT NULL,
 source_visual_inventory_sha256 TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('instrument_frozen','sample_frozen',
  'criteria_frozen','review_ready','reviews_partial','reviews_locked',
  'adjudication_locked','pass','fail')),
 sticky_stop INTEGER NOT NULL DEFAULT 0 CHECK(sticky_stop IN (0,1)),
 sample_count INTEGER NOT NULL DEFAULT 0, sheet_count INTEGER NOT NULL DEFAULT 0,
 asset_occurrence_count INTEGER NOT NULL DEFAULT 0,
 unique_asset_count INTEGER NOT NULL DEFAULT 0,
 inherited_logical_http_fetches INTEGER NOT NULL DEFAULT 0,
 inherited_wire_http_requests INTEGER NOT NULL DEFAULT 0,
 incremental_logical_http_fetches INTEGER NOT NULL DEFAULT 0 CHECK(incremental_logical_http_fetches=0),
 incremental_wire_http_requests INTEGER NOT NULL DEFAULT 0 CHECK(incremental_wire_http_requests=0),
 incremental_retries INTEGER NOT NULL DEFAULT 0 CHECK(incremental_retries=0),
 incremental_redirects INTEGER NOT NULL DEFAULT 0 CHECK(incremental_redirects=0),
 incremental_renderer_substitutions INTEGER NOT NULL DEFAULT 0 CHECK(incremental_renderer_substitutions=0),
 incremental_cache_writes INTEGER NOT NULL DEFAULT 0 CHECK(incremental_cache_writes=0),
 r1_status TEXT NOT NULL DEFAULT 'pending', r2_status TEXT NOT NULL DEFAULT 'pending',
 adjudication_status TEXT NOT NULL DEFAULT 'pending',
 gate_status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
 completed_at TEXT, record_sha256 TEXT NOT NULL
);
CREATE TABLE input_pins(
 run_id TEXT NOT NULL REFERENCES runs(run_id), role TEXT NOT NULL, path TEXT NOT NULL,
 size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, usage TEXT NOT NULL,
 record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,role)
);
CREATE TABLE instrument_contract(
 run_id TEXT PRIMARY KEY REFERENCES runs(run_id), contract_json TEXT NOT NULL,
 contract_sha256 TEXT NOT NULL, protocol_path TEXT NOT NULL,
 protocol_sha256 TEXT NOT NULL, order_path TEXT NOT NULL, order_sha256 TEXT NOT NULL,
 frozen_at TEXT NOT NULL, record_sha256 TEXT NOT NULL
);
CREATE TABLE source_samples(
 run_id TEXT NOT NULL REFERENCES runs(run_id), sample_rank INTEGER NOT NULL,
 sample_id TEXT NOT NULL, seed_rank_sha256 TEXT NOT NULL,
 control_commitment_sha256 TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL,
 source_pair_record_sha256 TEXT NOT NULL, record_sha256 TEXT NOT NULL,
 PRIMARY KEY(run_id,sample_rank), UNIQUE(run_id,sample_id)
);
CREATE TABLE source_sheets(
 run_id TEXT NOT NULL REFERENCES runs(run_id), lane TEXT NOT NULL CHECK(lane IN ('r1','r2')),
 display_order INTEGER NOT NULL, opaque_review_id TEXT NOT NULL, sample_id TEXT NOT NULL,
 sheet_path TEXT NOT NULL, sheet_sha256 TEXT NOT NULL, sheet_bytes INTEGER NOT NULL,
 source_record_sha256 TEXT NOT NULL, record_sha256 TEXT NOT NULL,
 PRIMARY KEY(run_id,lane,opaque_review_id), UNIQUE(run_id,lane,display_order),
 UNIQUE(run_id,lane,sample_id)
);
CREATE TABLE source_assets(
 run_id TEXT NOT NULL REFERENCES runs(run_id), sample_id TEXT NOT NULL,
 side TEXT NOT NULL CHECK(side IN ('left','right')), asset_order INTEGER NOT NULL CHECK(asset_order BETWEEN 1 AND 2),
 asset_key TEXT NOT NULL, source TEXT NOT NULL, source_building_id TEXT NOT NULL,
 source_asset_id TEXT NOT NULL, selection_reason TEXT NOT NULL,
 source_record_sha256 TEXT NOT NULL, record_sha256 TEXT NOT NULL,
 PRIMARY KEY(run_id,sample_id,side,asset_order)
);
CREATE TABLE source_fetch_assets(
 run_id TEXT NOT NULL REFERENCES runs(run_id), asset_key TEXT NOT NULL,
 source TEXT NOT NULL, source_asset_id TEXT NOT NULL, expected_url TEXT,
 expected_raw_sha256 TEXT, expected_pixel_sha256 TEXT, status TEXT NOT NULL,
 cache_role TEXT, cache_path TEXT, cache_file_sha256 TEXT,
 actual_final_url TEXT, actual_raw_sha256 TEXT, actual_pixel_sha256 TEXT,
 http_status INTEGER, response_bytes INTEGER, request_count INTEGER NOT NULL,
 error_kind TEXT, error_message TEXT, source_record_sha256 TEXT NOT NULL,
 record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,asset_key)
);
CREATE TABLE source_fetch_attempts(
 run_id TEXT NOT NULL REFERENCES runs(run_id), asset_key TEXT NOT NULL,
 attempt_no INTEGER NOT NULL, status TEXT NOT NULL, request_count INTEGER NOT NULL,
 redirect_chain_json TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT NOT NULL,
 error_kind TEXT, source_record_sha256 TEXT NOT NULL, record_sha256 TEXT NOT NULL,
 PRIMARY KEY(run_id,asset_key,attempt_no)
);
CREATE TABLE source_cache_pins(
 run_id TEXT NOT NULL REFERENCES runs(run_id), role TEXT NOT NULL, path TEXT NOT NULL,
 transform_version TEXT NOT NULL, access_mode TEXT NOT NULL,
 file_count_before INTEGER NOT NULL, total_bytes_before INTEGER NOT NULL,
 tree_sha256_before TEXT NOT NULL, file_count_after INTEGER,
 total_bytes_after INTEGER, tree_sha256_after TEXT,
 source_record_sha256 TEXT NOT NULL, record_sha256 TEXT NOT NULL,
 PRIMARY KEY(run_id,role)
);
CREATE TABLE quota_observations(
 run_id TEXT NOT NULL REFERENCES runs(run_id), observation_no INTEGER NOT NULL,
 phase TEXT NOT NULL, account_index INTEGER NOT NULL, observed_at TEXT NOT NULL,
 source TEXT NOT NULL, remaining_percent REAL NOT NULL,
 start_allowed INTEGER NOT NULL CHECK(start_allowed IN (0,1)),
 stop_required INTEGER NOT NULL CHECK(stop_required IN (0,1)),
 limitation TEXT NOT NULL, record_sha256 TEXT NOT NULL,
 PRIMARY KEY(run_id,observation_no)
);
CREATE TABLE gate_contract(
 run_id TEXT PRIMARY KEY REFERENCES runs(run_id), gate_json TEXT NOT NULL,
 gate_sha256 TEXT NOT NULL,
 d_min_same INTEGER NOT NULL CHECK(d_min_same=13), d_n INTEGER NOT NULL CHECK(d_n=15),
 p1_min_non_same INTEGER NOT NULL CHECK(p1_min_non_same=2), p1_n INTEGER NOT NULL CHECK(p1_n=3),
 insufficient_min_exact INTEGER NOT NULL CHECK(insufficient_min_exact=4),
 insufficient_n INTEGER NOT NULL CHECK(insufficient_n=7),
 related_min_exact INTEGER NOT NULL CHECK(related_min_exact=1),
 related_n INTEGER NOT NULL CHECK(related_n=2),
 declared_at TEXT NOT NULL, record_sha256 TEXT NOT NULL
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
CREATE TABLE json_payloads(
 run_id TEXT NOT NULL REFERENCES runs(run_id), role TEXT NOT NULL,
 payload_json TEXT NOT NULL, record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,role)
);
CREATE TABLE artifacts(
 run_id TEXT NOT NULL REFERENCES runs(run_id), role TEXT NOT NULL, path TEXT NOT NULL,
 stage TEXT NOT NULL, size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL,
 payload_hex TEXT NOT NULL, created_at TEXT NOT NULL, record_sha256 TEXT NOT NULL,
 PRIMARY KEY(run_id,role)
);
CREATE TABLE review_observations(
 run_id TEXT NOT NULL REFERENCES runs(run_id), lane TEXT NOT NULL CHECK(lane IN ('r1','r2')),
 display_order INTEGER NOT NULL, opaque_review_id TEXT NOT NULL, sample_id TEXT NOT NULL,
 image TEXT NOT NULL, name TEXT NOT NULL, firm TEXT NOT NULL, place TEXT NOT NULL,
 year TEXT NOT NULL, scope TEXT NOT NULL, basis TEXT NOT NULL,
 reviewer TEXT NOT NULL, reviewed_at TEXT NOT NULL, observation_sha256 TEXT NOT NULL,
 record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,lane,opaque_review_id),
 UNIQUE(run_id,lane,sample_id)
);
CREATE TABLE review_locks(
 run_id TEXT NOT NULL REFERENCES runs(run_id), lane TEXT NOT NULL,
 reviewer TEXT NOT NULL, row_count INTEGER NOT NULL,
 completed_form_path TEXT NOT NULL, completed_form_sha256 TEXT NOT NULL,
 locked_output_path TEXT NOT NULL, locked_output_sha256 TEXT NOT NULL,
 ordered_ledger_sha256 TEXT NOT NULL, locked_at TEXT NOT NULL,
 record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,lane)
);
CREATE TABLE review_lock_payloads(
 run_id TEXT NOT NULL REFERENCES runs(run_id), lane TEXT NOT NULL,
 payload_json TEXT NOT NULL, record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,lane)
);
CREATE TABLE adjudication_items(
 run_id TEXT NOT NULL REFERENCES runs(run_id), display_order INTEGER NOT NULL,
 opaque_adjudication_id TEXT NOT NULL, sample_id TEXT NOT NULL,
 sheet_path TEXT NOT NULL, sheet_sha256 TEXT NOT NULL,
 r1_observation_sha256 TEXT NOT NULL, r2_observation_sha256 TEXT NOT NULL,
 record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,opaque_adjudication_id),
 UNIQUE(run_id,display_order), UNIQUE(run_id,sample_id)
);
CREATE TABLE adjudicated_observations(
 run_id TEXT NOT NULL REFERENCES runs(run_id), opaque_adjudication_id TEXT NOT NULL,
 sample_id TEXT NOT NULL, image TEXT NOT NULL, name TEXT NOT NULL, firm TEXT NOT NULL,
 place TEXT NOT NULL, year TEXT NOT NULL, scope TEXT NOT NULL, basis TEXT NOT NULL,
 derived_label TEXT NOT NULL, derivation_rule INTEGER NOT NULL CHECK(derivation_rule BETWEEN 1 AND 5),
 axis_adjustments_json TEXT NOT NULL, adjudicator TEXT NOT NULL,
 adjudicated_at TEXT NOT NULL, observation_sha256 TEXT NOT NULL,
 record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,opaque_adjudication_id),
 UNIQUE(run_id,sample_id)
);
CREATE TABLE adjudication_lock(
 run_id TEXT PRIMARY KEY REFERENCES runs(run_id), adjudicator TEXT NOT NULL,
 row_count INTEGER NOT NULL, completed_form_path TEXT NOT NULL,
 completed_form_sha256 TEXT NOT NULL, locked_output_path TEXT NOT NULL,
 locked_output_sha256 TEXT NOT NULL, ordered_ledger_sha256 TEXT NOT NULL,
 locked_at TEXT NOT NULL, record_sha256 TEXT NOT NULL
);
CREATE TABLE adjudication_lock_payload(
 run_id TEXT PRIMARY KEY REFERENCES runs(run_id), payload_json TEXT NOT NULL,
 record_sha256 TEXT NOT NULL
);
CREATE TABLE control_reveals(
 run_id TEXT NOT NULL REFERENCES runs(run_id), sample_id TEXT NOT NULL,
 candidate_id TEXT NOT NULL, group_code TEXT NOT NULL,
 n100_sample_id TEXT NOT NULL, n100_gold_label TEXT NOT NULL,
 v1_probe_label TEXT NOT NULL, v2_derived_label TEXT NOT NULL,
 derivation_rule_no INTEGER NOT NULL, commitment_sha256 TEXT NOT NULL,
 revealed_at TEXT NOT NULL, reveal_payload_sha256 TEXT NOT NULL,
 record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,sample_id)
);
CREATE TABLE gate_scores(
 run_id TEXT NOT NULL REFERENCES runs(run_id), predicate TEXT NOT NULL,
 actual INTEGER NOT NULL, denominator INTEGER NOT NULL, threshold INTEGER NOT NULL,
 passed INTEGER NOT NULL CHECK(passed IN (0,1)), record_sha256 TEXT NOT NULL,
 PRIMARY KEY(run_id,predicate)
);
CREATE TABLE state_events(
 run_id TEXT NOT NULL REFERENCES runs(run_id), event_no INTEGER NOT NULL,
 from_status TEXT NOT NULL, to_status TEXT NOT NULL, detail_json TEXT NOT NULL,
 occurred_at TEXT NOT NULL, record_sha256 TEXT NOT NULL,
 PRIMARY KEY(run_id,event_no)
);
CREATE TABLE terminal(
 run_id TEXT PRIMARY KEY REFERENCES runs(run_id), status TEXT NOT NULL,
 report_path TEXT NOT NULL, report_sha256 TEXT NOT NULL,
 manifest_path TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
 report_json TEXT NOT NULL, manifest_json TEXT NOT NULL,
 completed_at TEXT NOT NULL, record_sha256 TEXT NOT NULL
);

CREATE TRIGGER source_samples_no_update BEFORE UPDATE ON source_samples BEGIN SELECT RAISE(ABORT,'source samples frozen'); END;
CREATE TRIGGER source_samples_no_delete BEFORE DELETE ON source_samples BEGIN SELECT RAISE(ABORT,'source samples frozen'); END;
CREATE TRIGGER source_sheets_no_update BEFORE UPDATE ON source_sheets BEGIN SELECT RAISE(ABORT,'source sheets frozen'); END;
CREATE TRIGGER source_sheets_no_delete BEFORE DELETE ON source_sheets BEGIN SELECT RAISE(ABORT,'source sheets frozen'); END;
CREATE TRIGGER source_assets_no_update BEFORE UPDATE ON source_assets BEGIN SELECT RAISE(ABORT,'source assets frozen'); END;
CREATE TRIGGER source_assets_no_delete BEFORE DELETE ON source_assets BEGIN SELECT RAISE(ABORT,'source assets frozen'); END;
CREATE TRIGGER source_fetch_assets_no_update BEFORE UPDATE ON source_fetch_assets BEGIN SELECT RAISE(ABORT,'source fetch frozen'); END;
CREATE TRIGGER source_fetch_assets_no_delete BEFORE DELETE ON source_fetch_assets BEGIN SELECT RAISE(ABORT,'source fetch frozen'); END;
CREATE TRIGGER source_fetch_attempts_no_update BEFORE UPDATE ON source_fetch_attempts BEGIN SELECT RAISE(ABORT,'source attempts frozen'); END;
CREATE TRIGGER source_fetch_attempts_no_delete BEFORE DELETE ON source_fetch_attempts BEGIN SELECT RAISE(ABORT,'source attempts frozen'); END;
CREATE TRIGGER source_cache_no_update BEFORE UPDATE ON source_cache_pins BEGIN SELECT RAISE(ABORT,'source caches frozen'); END;
CREATE TRIGGER source_cache_no_delete BEFORE DELETE ON source_cache_pins BEGIN SELECT RAISE(ABORT,'source caches frozen'); END;
CREATE TRIGGER quota_no_update BEFORE UPDATE ON quota_observations BEGIN SELECT RAISE(ABORT,'quota append only'); END;
CREATE TRIGGER quota_no_delete BEFORE DELETE ON quota_observations BEGIN SELECT RAISE(ABORT,'quota append only'); END;
CREATE TRIGGER instrument_no_update BEFORE UPDATE ON instrument_contract BEGIN SELECT RAISE(ABORT,'instrument frozen'); END;
CREATE TRIGGER instrument_no_delete BEFORE DELETE ON instrument_contract BEGIN SELECT RAISE(ABORT,'instrument frozen'); END;
CREATE TRIGGER input_pins_no_update BEFORE UPDATE ON input_pins BEGIN SELECT RAISE(ABORT,'input pins frozen'); END;
CREATE TRIGGER input_pins_no_delete BEFORE DELETE ON input_pins BEGIN SELECT RAISE(ABORT,'input pins frozen'); END;
CREATE TRIGGER gate_no_update BEFORE UPDATE ON gate_contract BEGIN SELECT RAISE(ABORT,'gate frozen'); END;
CREATE TRIGGER gate_no_delete BEFORE DELETE ON gate_contract BEGIN SELECT RAISE(ABORT,'gate frozen'); END;
CREATE TRIGGER paths_no_update BEFORE UPDATE ON artifact_paths BEGIN SELECT RAISE(ABORT,'artifact path frozen'); END;
CREATE TRIGGER paths_no_delete BEFORE DELETE ON artifact_paths BEGIN SELECT RAISE(ABORT,'artifact path frozen'); END;
CREATE TRIGGER bindings_no_update BEFORE UPDATE ON artifact_bindings BEGIN SELECT RAISE(ABORT,'artifact binding frozen'); END;
CREATE TRIGGER bindings_no_delete BEFORE DELETE ON artifact_bindings BEGIN SELECT RAISE(ABORT,'artifact binding frozen'); END;
CREATE TRIGGER json_payloads_no_update BEFORE UPDATE ON json_payloads BEGIN SELECT RAISE(ABORT,'JSON payload frozen'); END;
CREATE TRIGGER json_payloads_no_delete BEFORE DELETE ON json_payloads BEGIN SELECT RAISE(ABORT,'JSON payload frozen'); END;
CREATE TRIGGER artifacts_no_update BEFORE UPDATE ON artifacts BEGIN SELECT RAISE(ABORT,'artifact frozen'); END;
CREATE TRIGGER artifacts_no_delete BEFORE DELETE ON artifacts BEGIN SELECT RAISE(ABORT,'artifact frozen'); END;
CREATE TRIGGER reviews_no_update BEFORE UPDATE ON review_observations BEGIN SELECT RAISE(ABORT,'review frozen'); END;
CREATE TRIGGER reviews_no_delete BEFORE DELETE ON review_observations BEGIN SELECT RAISE(ABORT,'review frozen'); END;
CREATE TRIGGER review_locks_no_update BEFORE UPDATE ON review_locks BEGIN SELECT RAISE(ABORT,'review lock frozen'); END;
CREATE TRIGGER review_locks_no_delete BEFORE DELETE ON review_locks BEGIN SELECT RAISE(ABORT,'review lock frozen'); END;
CREATE TRIGGER review_lock_payloads_no_update BEFORE UPDATE ON review_lock_payloads BEGIN SELECT RAISE(ABORT,'review lock payload frozen'); END;
CREATE TRIGGER review_lock_payloads_no_delete BEFORE DELETE ON review_lock_payloads BEGIN SELECT RAISE(ABORT,'review lock payload frozen'); END;
CREATE TRIGGER adjudication_items_no_update BEFORE UPDATE ON adjudication_items BEGIN SELECT RAISE(ABORT,'adjudication item frozen'); END;
CREATE TRIGGER adjudication_items_no_delete BEFORE DELETE ON adjudication_items BEGIN SELECT RAISE(ABORT,'adjudication item frozen'); END;
CREATE TRIGGER adjudications_no_update BEFORE UPDATE ON adjudicated_observations BEGIN SELECT RAISE(ABORT,'adjudication frozen'); END;
CREATE TRIGGER adjudications_no_delete BEFORE DELETE ON adjudicated_observations BEGIN SELECT RAISE(ABORT,'adjudication frozen'); END;
CREATE TRIGGER adjudication_lock_no_update BEFORE UPDATE ON adjudication_lock BEGIN SELECT RAISE(ABORT,'adjudication lock frozen'); END;
CREATE TRIGGER adjudication_lock_no_delete BEFORE DELETE ON adjudication_lock BEGIN SELECT RAISE(ABORT,'adjudication lock frozen'); END;
CREATE TRIGGER adjudication_lock_payload_no_update BEFORE UPDATE ON adjudication_lock_payload BEGIN SELECT RAISE(ABORT,'adjudication lock payload frozen'); END;
CREATE TRIGGER adjudication_lock_payload_no_delete BEFORE DELETE ON adjudication_lock_payload BEGIN SELECT RAISE(ABORT,'adjudication lock payload frozen'); END;
CREATE TRIGGER reveals_only_locked BEFORE INSERT ON control_reveals
 WHEN (SELECT status FROM runs WHERE run_id=NEW.run_id)<>'adjudication_locked'
 BEGIN SELECT RAISE(ABORT,'reveal before adjudication lock'); END;
CREATE TRIGGER reveals_no_update BEFORE UPDATE ON control_reveals BEGIN SELECT RAISE(ABORT,'reveal frozen'); END;
CREATE TRIGGER reveals_no_delete BEFORE DELETE ON control_reveals BEGIN SELECT RAISE(ABORT,'reveal frozen'); END;
CREATE TRIGGER scores_no_update BEFORE UPDATE ON gate_scores BEGIN SELECT RAISE(ABORT,'score frozen'); END;
CREATE TRIGGER scores_no_delete BEFORE DELETE ON gate_scores BEGIN SELECT RAISE(ABORT,'score frozen'); END;
CREATE TRIGGER events_no_update BEFORE UPDATE ON state_events BEGIN SELECT RAISE(ABORT,'events append only'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON state_events BEGIN SELECT RAISE(ABORT,'events append only'); END;
CREATE TRIGGER terminal_no_update BEFORE UPDATE ON terminal BEGIN SELECT RAISE(ABORT,'terminal frozen'); END;
CREATE TRIGGER terminal_no_delete BEFORE DELETE ON terminal BEGIN SELECT RAISE(ABORT,'terminal frozen'); END;
"""


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


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _record(payload: Mapping[str, Any]) -> str:
    return canonical_sha256(dict(payload))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(16 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_bytes(path: Path, raw: bytes, *, allow_identical: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if allow_identical and path.is_file() and path.read_bytes() == raw:
            return
        raise IdentityInstrumentV2Error(f"refusing to clobber artifact: {path}")
    temporary = path.with_name(path.name + ".building")
    if temporary.exists():
        raise IdentityInstrumentV2Error(f"stale temporary artifact: {temporary}")
    try:
        with temporary.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        if temporary.exists():
            failed = path.with_name(path.name + ".building.failed")
            if not failed.exists():
                os.replace(temporary, failed)
        raise


def _atomic_json(path: Path, payload: Mapping[str, Any], *, allow_identical: bool = False) -> None:
    _atomic_bytes(path, _json_bytes(payload), allow_identical=allow_identical)


def _load_json(path: Path, label: str) -> Mapping[str, Any]:
    if not path.is_file():
        raise IdentityInstrumentV2Error(f"{label} is missing")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IdentityInstrumentV2Error(f"{label} is not valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise IdentityInstrumentV2Error(f"{label} must be a JSON object")
    return payload


def _assert_no_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        if Path(str(path) + suffix).exists():
            raise IdentityInstrumentV2Error(f"mutable SQLite sidecar exists: {path}{suffix}")


def _open_ro(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise IdentityInstrumentV2Error(f"source DB is missing: {path}")
    _assert_no_sidecars(path)
    connection = sqlite3.connect(
        "file:" + path.resolve().as_posix() + "?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _connect(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise IdentityInstrumentV2Error(f"WP2.7 artifact is missing: {path}")
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _quota_time(value: Any, *, require_fresh: bool = True,
                now: datetime | None = None) -> datetime:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IdentityInstrumentV2Error("quota observed_at must be timezone ISO8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise IdentityInstrumentV2Error("quota observed_at must include a timezone")
    observed = parsed.astimezone(timezone.utc)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if require_fresh and observed > current + MAX_QUOTA_FUTURE_SKEW:
        raise IdentityInstrumentV2Error("quota observation is implausibly in the future")
    if require_fresh and current - observed > MAX_QUOTA_AGE:
        raise IdentityInstrumentV2Error("quota observation is stale")
    return observed


def _one_run(connection: sqlite3.Connection) -> sqlite3.Row:
    rows = connection.execute("SELECT * FROM runs").fetchall()
    if len(rows) != 1:
        raise IdentityInstrumentV2Error("artifact must contain exactly one run")
    return rows[0]


def _refresh_run_record(connection: sqlite3.Connection, run_id: str) -> None:
    row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise IdentityInstrumentV2Error("run disappeared")
    payload = {key: row[key] for key in row.keys() if key != "record_sha256"}
    connection.execute("UPDATE runs SET record_sha256=? WHERE run_id=?", (_record(payload), run_id))


def _insert_event(connection: sqlite3.Connection, run_id: str, from_status: str,
                  to_status: str, detail: Mapping[str, Any]) -> None:
    event_no = int(connection.execute(
        "SELECT coalesce(max(event_no),0)+1 FROM state_events WHERE run_id=?", (run_id,)
    ).fetchone()[0])
    occurred_at = utc_now()
    detail_json = canonical_json(dict(detail))
    payload = {"run_id": run_id, "event_no": event_no, "from_status": from_status,
               "to_status": to_status, "detail_json": detail_json,
               "occurred_at": occurred_at}
    connection.execute("INSERT INTO state_events VALUES(?,?,?,?,?,?,?)",
                       (run_id, event_no, from_status, to_status,
                        detail_json, occurred_at, _record(payload)))


def _file_pin(root: Path, path: Path, usage: str) -> dict[str, Any]:
    absolute = _path(root, path)
    if not absolute.is_file():
        raise IdentityInstrumentV2Error(f"pinned input is missing: {absolute}")
    return {"path": _path_text(root, absolute), "size_bytes": absolute.stat().st_size,
            "sha256": sha256_file(absolute), "usage": usage}


def _insert_pin(connection: sqlite3.Connection, run_id: str, role: str,
                pin: Mapping[str, Any]) -> None:
    payload = {"run_id": run_id, "role": role, **dict(pin)}
    connection.execute("INSERT INTO input_pins VALUES(?,?,?,?,?,?,?)",
                       (run_id, role, pin["path"], pin["size_bytes"], pin["sha256"],
                        pin["usage"], _record(payload)))


def _verify_pins(connection: sqlite3.Connection, root: Path, run_id: str) -> None:
    for row in connection.execute("SELECT * FROM input_pins WHERE run_id=?", (run_id,)):
        path = _resolve_text(root, str(row["path"]))
        if (not path.is_file() or path.stat().st_size != int(row["size_bytes"])
                or sha256_file(path) != str(row["sha256"])):
            raise IdentityInstrumentV2Error(f"pinned input changed: {row['role']}")


def _register_path(connection: sqlite3.Connection, root: Path, run_id: str,
                   role: str, path: Path, stage: str) -> None:
    text = _path_text(root, path)
    payload = {"run_id": run_id, "role": role, "path": text, "stage": stage}
    connection.execute("INSERT INTO artifact_paths VALUES(?,?,?,?,?)",
                       (run_id, role, text, stage, _record(payload)))


def _bind_values(connection: sqlite3.Connection, root: Path, run_id: str,
                 role: str, path: Path, stage: str, raw: bytes) -> None:
    text = _path_text(root, path)
    digest = hashlib.sha256(raw).hexdigest()
    payload = {"run_id": run_id, "role": role, "path": text, "size_bytes": len(raw),
               "sha256": digest, "stage": stage}
    connection.execute("INSERT INTO artifact_bindings VALUES(?,?,?,?,?,?,?)",
                       (run_id, role, text, len(raw), digest, stage, _record(payload)))
    created_at = utc_now()
    artifact_row = {
        "run_id": run_id, "role": role, "path": text, "stage": stage,
        "size_bytes": len(raw), "sha256": digest, "payload_hex": raw.hex(),
        "created_at": created_at,
    }
    connection.execute(
        "INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?)",
        (*artifact_row.values(), _record(artifact_row)),
    )


def _store_json_payload(connection: sqlite3.Connection, run_id: str,
                        role: str, payload: Mapping[str, Any]) -> bytes:
    raw = _json_bytes(payload)
    text = canonical_json(dict(payload))
    row = {"run_id": run_id, "role": role, "payload_json": text}
    connection.execute("INSERT INTO json_payloads VALUES(?,?,?,?)",
                       (run_id, role, text, _record(row)))
    return raw


def _recover_artifact(connection: sqlite3.Connection, root: Path, run_id: str,
                      role: str, expected_path: Path) -> bytes:
    """Restore one exact external artifact from the immutable SQLite byte copy."""
    row = connection.execute(
        "SELECT * FROM artifacts WHERE run_id=? AND role=?", (run_id, role)
    ).fetchone()
    if row is None:
        raise IdentityInstrumentV2Error(f"stored artifact is missing: {role}")
    bound_path = _resolve_text(root, str(row["path"]))
    if bound_path.resolve() != expected_path.resolve():
        raise IdentityInstrumentV2Error(f"artifact recovery path changed: {role}")
    try:
        raw = bytes.fromhex(str(row["payload_hex"]))
    except ValueError as exc:
        raise IdentityInstrumentV2Error(f"stored artifact bytes are invalid: {role}") from exc
    if len(raw) != int(row["size_bytes"]) or hashlib.sha256(raw).hexdigest() != str(row["sha256"]):
        raise IdentityInstrumentV2Error(f"stored artifact binding changed: {role}")
    _atomic_bytes(expected_path, raw, allow_identical=True)
    return raw


def _payload_from_db(connection: sqlite3.Connection, run_id: str, role: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT payload_json FROM json_payloads WHERE run_id=? AND role=?", (run_id, role)
    ).fetchone()
    if row is None:
        raise IdentityInstrumentV2Error(f"stored JSON payload is missing: {role}")
    payload = json.loads(str(row[0]))
    if not isinstance(payload, dict):
        raise IdentityInstrumentV2Error(f"stored JSON payload is invalid: {role}")
    return payload


def _instrument_payload() -> dict[str, Any]:
    return {
        "instrument_version": INSTRUMENT_VERSION,
        "observation_keys": list(OBSERVATION_KEYS),
        "controlled_values": {key: list(CONTROLLED_VALUES[key]) for key in CONTROLLED_KEYS},
        "basis": {"type": "trimmed_non_empty_text", "required": True},
        "forbidden_input_fields": sorted(FORBIDDEN_INPUT_FIELDS),
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
            {"rule": 2, "when": "image=different_objects OR scope=unrelated", "label": "different"},
            {"rule": 3, "when": "scope IN (component_vs_umbrella,phase_or_extension)", "label": "related"},
            {"rule": 4, "when": "image=same_object AND scope=same", "label": "same"},
            {"rule": 5, "when": "otherwise", "label": "insufficient"},
        ],
        "source": {"run_id": SOURCE_RUN_ID, "sample_version": SOURCE_SAMPLE_VERSION,
                   "seed": SOURCE_SEED, "logical_sha256": SOURCE_LOGICAL_SHA256,
                   "packet_binding_sha256": SOURCE_PACKET_BINDING_SHA256,
                   "visual_inventory_sha256": SOURCE_VISUAL_INVENTORY_SHA256,
                   "rows": TOTAL_COUNT, "sheets": SOURCE_SHEET_COUNT,
                   "asset_occurrences": SOURCE_ASSET_OCCURRENCES},
        "limits": {"max_assets_per_side": MAX_ASSETS_PER_SIDE,
                   "max_asset_occurrences": MAX_ASSET_OCCURRENCES,
                   "incremental_http": 0, "incremental_wire": 0,
                   "incremental_retries": 0, "incremental_redirects": 0,
                   "incremental_renderer_substitutions": 0,
                   "incremental_cache_writes": 0},
    }


def _axis_instructions() -> dict[str, Any]:
    """Return the immutable, label-free guidance shipped to every actor."""
    return {
        "version": "vnext-identity-instrument-v2-axis-guidance-v1",
        "actor_contract": (
            "Record or adjudicate only the six controlled observations and mandatory basis; "
            "do not choose a label."
        ),
        "observation_keys": list(OBSERVATION_KEYS),
        "controlled_values": {
            key: list(CONTROLLED_VALUES[key]) for key in CONTROLLED_KEYS
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


def validate_observation(value: Mapping[str, Any]) -> dict[str, str]:
    if set(value) != set(OBSERVATION_KEYS):
        missing = sorted(set(OBSERVATION_KEYS) - set(value))
        extra = sorted(set(value) - set(OBSERVATION_KEYS))
        raise IdentityInstrumentV2Error(
            f"observation keys must be exact; missing={missing}, extra={extra}"
        )
    result: dict[str, str] = {}
    for key in CONTROLLED_KEYS:
        raw = value[key]
        if not isinstance(raw, str) or raw not in CONTROLLED_VALUES[key]:
            raise IdentityInstrumentV2Error(f"invalid controlled observation: {key}")
        result[key] = raw
    basis = value["basis"]
    if not isinstance(basis, str) or not basis.strip() or basis != basis.strip():
        raise IdentityInstrumentV2Error("basis must be trimmed non-empty text")
    result["basis"] = basis
    return result


def derive_label(observation: Mapping[str, Any]) -> tuple[str, int]:
    value = validate_observation(observation)
    if value["image"] == "inconclusive":
        return "insufficient", 1
    if value["image"] == "different_objects" or value["scope"] == "unrelated":
        return "different", 2
    if value["scope"] in {"component_vs_umbrella", "phase_or_extension"}:
        return "related", 3
    if value["image"] == "same_object" and value["scope"] == "same":
        return "same", 4
    return "insufficient", 5


def _gate_payload() -> dict[str, Any]:
    return {
        "gate_version": GATE_VERSION,
        "predicates": [
            {"code": "d_same", "denominator": 15, "threshold": 13,
             "condition": "group=D AND v2_derived_label=same"},
            {"code": "p1_non_same", "denominator": 3, "threshold": 2,
             "condition": "group=P1 AND v2_derived_label!=same"},
            {"code": "gold_insufficient_to_insufficient", "denominator": 7, "threshold": 4,
             "condition": "n100_gold_label=insufficient AND v2_derived_label=insufficient"},
            {"code": "gold_related_to_related", "denominator": 2, "threshold": 1,
             "condition": "n100_gold_label=related AND v2_derived_label=related"},
        ],
        "combiner": "all",
    }


def _require_not_stopped(connection: sqlite3.Connection, run_id: str) -> None:
    run = connection.execute("SELECT sticky_stop FROM runs WHERE run_id=?", (run_id,)).fetchone()
    any_stop = connection.execute(
        "SELECT 1 FROM quota_observations WHERE run_id=? AND stop_required=1 LIMIT 1", (run_id,)
    ).fetchone()
    if run is None or int(run[0]) or any_stop is not None:
        raise IdentityInstrumentV2Error("sticky quota stop forbids further activity")


def _quota_rows(connection: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    rows = connection.execute(
        "SELECT * FROM quota_observations WHERE run_id=? ORDER BY observation_no", (run_id,)
    ).fetchall()
    previous_index = -1
    previous_time: datetime | None = None
    for position, row in enumerate(rows):
        phase = str(row["phase"])
        index = QUOTA_INDEX.get(phase)
        if index is None or (position == 0 and phase != "pre_freeze"):
            raise IdentityInstrumentV2Error("quota phase sequence is invalid")
        if index < previous_index or index > previous_index + 1:
            raise IdentityInstrumentV2Error("quota phase order is invalid")
        observed = _quota_time(row["observed_at"], require_fresh=False)
        if previous_time is not None and observed < previous_time:
            raise IdentityInstrumentV2Error("quota observation times are not monotonic")
        previous_index, previous_time = index, observed
    return rows


def _require_quota(connection: sqlite3.Connection, run_id: str, phase: str) -> None:
    _require_not_stopped(connection, run_id)
    rows = _quota_rows(connection, run_id)
    if not rows or str(rows[-1]["phase"]) != phase:
        raise IdentityInstrumentV2Error(f"quota gate missing for {phase}")
    row = rows[-1]
    _quota_time(row["observed_at"])
    if not int(row["start_allowed"]) or int(row["stop_required"]):
        raise IdentityInstrumentV2Error(f"quota gate denied for {phase}")


def freeze_instrument(*, repo_root: Path, quota_remaining_percent: float,
                      quota_observed_at: str, quota_source: str,
                      quota_account_index: int = 0, quota_limitation: str = "",
                      protocol_path: Path = DEFAULT_PROTOCOL,
                      order_path: Path = DEFAULT_ORDER,
                      source_db_path: Path = DEFAULT_SOURCE_DB,
                      source_sample_manifest_path: Path = DEFAULT_SOURCE_SAMPLE_MANIFEST,
                      source_report_path: Path = DEFAULT_SOURCE_REPORT,
                      source_manifest_path: Path = DEFAULT_SOURCE_MANIFEST,
                      source_fetch_manifest_path: Path = DEFAULT_SOURCE_FETCH_MANIFEST,
                      source_fetch_correction_path: Path = DEFAULT_SOURCE_FETCH_CORRECTION,
                      source_producer_path: Path = DEFAULT_SOURCE_PRODUCER,
                      source_adjudication_locked_path: Path = DEFAULT_SOURCE_ADJUDICATION_LOCKED,
                      output_path: Path = DEFAULT_OUTPUT,
                      verify_fixed_pins: bool = True) -> FreezeResult:
    """Stage A: atomically freeze the v2 instrument and all source file pins."""
    root = repo_root.resolve()
    output = _path(root, output_path)
    _quota_time(quota_observed_at)
    if not 0 <= quota_remaining_percent <= 100 or quota_remaining_percent <= 30:
        raise IdentityInstrumentV2Error("pre-freeze quota must be greater than 30%")
    if not quota_source.strip():
        raise IdentityInstrumentV2Error("quota source is required")
    pins = {
        "wp27_protocol": _file_pin(root, protocol_path, "instrument_contract"),
        "wp27_order": _file_pin(root, order_path, "work_order"),
        "wp26_source_db": _file_pin(root, source_db_path, "sample_evidence_and_reveal_source"),
        "wp26_sample_manifest": _file_pin(root, source_sample_manifest_path, "sample_binding"),
        "wp26_final_report": _file_pin(root, source_report_path, "post_lock_contrast_source"),
        "wp26_final_manifest": _file_pin(root, source_manifest_path, "source_provenance"),
        "wp26_fetch_manifest": _file_pin(root, source_fetch_manifest_path, "inherited_fetch_ledger"),
        "wp26_fetch_correction": _file_pin(
            root, source_fetch_correction_path, "terminal_fetch_cap_correction_overlay"
        ),
        "wp26_producer": _file_pin(root, source_producer_path, "source_producer_provenance"),
        "wp26_adjudication_locked": _file_pin(
            root, source_adjudication_locked_path, "source_axis_lock_provenance"
        ),
    }
    if verify_fixed_pins:
        mismatches = [
            role for role, expected in FIXED_INPUT_SPECS.items()
            if (int(pins[role]["size_bytes"]), str(pins[role]["sha256"])) != expected
        ]
        if mismatches:
            raise IdentityInstrumentV2Error(
                "fixed input pin mismatch: " + ", ".join(sorted(mismatches))
            )
    contract = _instrument_payload()
    contract_sha = canonical_sha256(contract)
    run_id = "wp27-" + hashlib.sha256(
        f"{SAMPLE_VERSION}\0{contract_sha}\0{pins['wp26_source_db']['sha256']}".encode("utf-8")
    ).hexdigest()[:24]
    if output.exists():
        connection = _connect(output)
        try:
            run = _one_run(connection)
            _require_not_stopped(connection, str(run["run_id"]))
            row = connection.execute("SELECT * FROM instrument_contract").fetchone()
            if (str(run["run_id"]) != run_id or row is None
                    or str(row["contract_sha256"]) != contract_sha):
                raise IdentityInstrumentV2Error("instrument-freeze recovery mismatch")
            _verify_pins(connection, root, run_id)
            return FreezeResult(output, run_id, str(run["status"]))
        finally:
            connection.close()
    temporary = output.with_name(output.name + ".building")
    failed = output.with_name(output.name + ".building.failed")
    if temporary.exists() or failed.exists():
        raise IdentityInstrumentV2Error("stale instrument build artifact exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(temporary)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        connection.executescript(SCHEMA_SQL)
        created = utc_now()
        run_payload = {
            "run_id": run_id, "schema_version": SCHEMA_VERSION,
            "runner_version": RUNNER_VERSION, "instrument_version": INSTRUMENT_VERSION,
            "sample_version": SAMPLE_VERSION, "review_version": REVIEW_VERSION,
            "adjudication_version": ADJUDICATION_VERSION, "gate_version": GATE_VERSION,
            "source_sample_version": SOURCE_SAMPLE_VERSION, "source_seed": SOURCE_SEED,
            "source_logical_sha256": SOURCE_LOGICAL_SHA256,
            "source_run_id": SOURCE_RUN_ID,
            "source_packet_binding_sha256": SOURCE_PACKET_BINDING_SHA256,
            "source_visual_inventory_sha256": SOURCE_VISUAL_INVENTORY_SHA256,
            "status": "instrument_frozen", "created_at": created,
        }
        connection.execute(
            "INSERT INTO runs(run_id,schema_version,runner_version,instrument_version,"
            "sample_version,review_version,adjudication_version,gate_version,"
            "source_sample_version,source_seed,source_logical_sha256,source_run_id,"
            "source_packet_binding_sha256,source_visual_inventory_sha256,status,created_at,record_sha256) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, SCHEMA_VERSION, RUNNER_VERSION, INSTRUMENT_VERSION, SAMPLE_VERSION,
             REVIEW_VERSION, ADJUDICATION_VERSION, GATE_VERSION, SOURCE_SAMPLE_VERSION,
             SOURCE_SEED, SOURCE_LOGICAL_SHA256, SOURCE_RUN_ID,
             SOURCE_PACKET_BINDING_SHA256, SOURCE_VISUAL_INVENTORY_SHA256,
             "instrument_frozen", created,
             _record(run_payload)),
        )
        for role, pin in sorted(pins.items()):
            _insert_pin(connection, run_id, role, pin)
        frozen = utc_now()
        protocol_pin, order_pin = pins["wp27_protocol"], pins["wp27_order"]
        contract_json = canonical_json(contract)
        row_payload = {
            "run_id": run_id, "contract_json": contract_json, "contract_sha256": contract_sha,
            "protocol_path": protocol_pin["path"], "protocol_sha256": protocol_pin["sha256"],
            "order_path": order_pin["path"], "order_sha256": order_pin["sha256"],
            "frozen_at": frozen,
        }
        connection.execute("INSERT INTO instrument_contract VALUES(?,?,?,?,?,?,?,?,?)",
                           (run_id, contract_json, contract_sha,
                            protocol_pin["path"], protocol_pin["sha256"], order_pin["path"],
                            order_pin["sha256"], frozen, _record(row_payload)))
        quota_payload = {
            "run_id": run_id, "observation_no": 1, "phase": "pre_freeze",
            "account_index": quota_account_index, "observed_at": quota_observed_at.strip(),
            "source": quota_source.strip(), "remaining_percent": float(quota_remaining_percent),
            "start_allowed": 1, "stop_required": 0,
            "limitation": quota_limitation.strip(),
        }
        connection.execute("INSERT INTO quota_observations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (run_id, 1, "pre_freeze", quota_account_index,
                            quota_observed_at.strip(), quota_source.strip(),
                            float(quota_remaining_percent), 1, 0, quota_limitation.strip(),
                            _record(quota_payload)))
        _insert_event(connection, run_id, "none", "instrument_frozen",
                      {"stage": "A", "instrument_contract_sha256": contract_sha})
        _refresh_run_record(connection, run_id)
        connection.commit()
        connection.close(); connection = None
        os.replace(temporary, output)
        return FreezeResult(output, run_id, "instrument_frozen")
    except Exception:
        if connection is not None:
            connection.close()
        if temporary.exists() and not failed.exists():
            os.replace(temporary, failed)
        raise


def _sample_manifest_payload(connection: sqlite3.Connection, root: Path,
                             run_id: str, artifact: Path) -> dict[str, Any]:
    samples = [{key: row[key] for key in (
        "sample_rank", "sample_id", "seed_rank_sha256",
        "control_commitment_sha256", "snapshot_sha256")}
        for row in connection.execute(
            "SELECT * FROM source_samples WHERE run_id=? ORDER BY sample_rank", (run_id,))]
    sheets = [{key: row[key] for key in (
        "lane", "display_order", "opaque_review_id", "sample_id",
        "sheet_path", "sheet_sha256", "sheet_bytes")}
        for row in connection.execute(
            "SELECT * FROM source_sheets WHERE run_id=? ORDER BY lane,display_order", (run_id,))]
    run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    instrument = connection.execute(
        "SELECT contract_sha256 FROM instrument_contract WHERE run_id=?", (run_id,)
    ).fetchone()
    return {
        "status": "PASS", "run_id": run_id, "stage": "B",
        "versions": {"runner": RUNNER_VERSION, "instrument": INSTRUMENT_VERSION,
                     "sample": SAMPLE_VERSION, "source_sample": SOURCE_SAMPLE_VERSION},
        "source": {"seed": SOURCE_SEED, "logical_sha256": SOURCE_LOGICAL_SHA256},
        "source_run_id": SOURCE_RUN_ID,
        "source_packet_binding_sha256": SOURCE_PACKET_BINDING_SHA256,
        "source_visual_inventory_sha256": SOURCE_VISUAL_INVENTORY_SHA256,
        "instrument_contract_sha256": str(instrument[0]),
        "counts": {"samples": int(run["sample_count"]), "sheets": int(run["sheet_count"]),
                   "asset_occurrences": int(run["asset_occurrence_count"]),
                   "unique_assets": int(run["unique_asset_count"]),
                   "post_freeze_replacements": 0},
        "incremental_network": {"logical": 0, "wire": 0, "redirects": 0, "retries": 0},
        "incremental_cache_writes": 0, "incremental_renderer_substitutions": 0,
        "samples": samples, "sheets": sheets,
        "sidecar": {"path": _path_text(root, artifact),
                    "logical_sha256": canonical_sha256(samples)},
        "forbidden_operations": {"vision": 0, "llm": 0, "paid_api": 0,
                                 "merge": 0, "id_issuance": 0, "tier_promotion": 0,
                                 "neon_write": 0, "r2_write": 0},
    }


def freeze_sample(*, repo_root: Path, artifact_path: Path = DEFAULT_OUTPUT,
                  sample_manifest_path: Path = DEFAULT_SAMPLE_MANIFEST,
                  enforce_source_contract: bool = True) -> SampleResult:
    """Stage B: copy only blind-safe WP2.6 sample, sheet, and asset lineage."""
    root = repo_root.resolve(); artifact = _path(root, artifact_path)
    manifest_path = _path(root, sample_manifest_path)
    connection = _connect(artifact)
    source: sqlite3.Connection | None = None
    try:
        run = _one_run(connection); run_id, status = str(run["run_id"]), str(run["status"])
        _require_not_stopped(connection, run_id)
        existing_manifest = connection.execute(
            "SELECT 1 FROM artifacts WHERE run_id=? AND role='sample_manifest'", (run_id,)
        ).fetchone()
        if existing_manifest is not None:
            _recover_artifact(connection, root, run_id, "sample_manifest", manifest_path)
            return SampleResult(artifact, manifest_path, run_id, TOTAL_COUNT,
                                SOURCE_SHEET_COUNT, SOURCE_ASSET_OCCURRENCES)
        if status != "instrument_frozen":
            raise IdentityInstrumentV2Error("sample freeze requires instrument_frozen")
        if manifest_path.exists():
            raise IdentityInstrumentV2Error("refusing to clobber sample manifest")
        _verify_pins(connection, root, run_id)
        pin = connection.execute(
            "SELECT path FROM input_pins WHERE run_id=? AND role='wp26_source_db'", (run_id,)
        ).fetchone()
        source_path = _resolve_text(root, str(pin[0]))
        source = _open_ro(source_path)
        source_run = source.execute("SELECT * FROM runs").fetchone()
        if source_run is None:
            raise IdentityInstrumentV2Error("WP2.6 source run is missing")
        if enforce_source_contract and (
            str(source_run["run_id"]) != SOURCE_RUN_ID
            or str(source_run["sample_version"]) != SOURCE_SAMPLE_VERSION
            or str(source_run["seed"]) != SOURCE_SEED
            or str(source_run["sample_logical_sha256"]) != SOURCE_LOGICAL_SHA256
        ):
            raise IdentityInstrumentV2Error("WP2.6 source sample contract mismatch")
        if int(source_run["sample_count"]) != TOTAL_COUNT:
            raise IdentityInstrumentV2Error("WP2.6 source sample must contain 24 rows")
        sample_rows = source.execute(
            "SELECT p.*,s.snapshot_sha256,c.gold_commitment_sha256 "
            "FROM sample_pairs p JOIN candidate_snapshots s USING(run_id,sample_id) "
            "JOIN control_commitments c USING(run_id,sample_id) "
            "ORDER BY p.sample_rank"
        ).fetchall()
        sheet_rows = source.execute("SELECT * FROM review_items ORDER BY lane,display_order").fetchall()
        asset_rows = source.execute(
            "SELECT * FROM sample_assets ORDER BY sample_id,side,asset_order"
        ).fetchall()
        fetch_rows = source.execute("SELECT * FROM fetch_assets ORDER BY asset_key").fetchall()
        attempt_rows = source.execute(
            "SELECT * FROM fetch_attempts ORDER BY asset_key,attempt_no"
        ).fetchall()
        cache_rows = source.execute("SELECT * FROM cache_pins ORDER BY role").fetchall()
        if enforce_source_contract:
            fetch_statuses = Counter(str(row["status"]) for row in fetch_rows)
            if (len(fetch_rows) != 93 or len(attempt_rows) != 17 or len(cache_rows) != 5
                    or fetch_statuses != Counter({"cache_hit": 76, "failed": 17})
                    or sum(int(row["request_count"]) for row in attempt_rows) != 17):
                raise IdentityInstrumentV2Error(
                    "WP2.6 fetch/attempt/cache lineage must replay as 93/17/5"
                )
        if len(sample_rows) != TOTAL_COUNT or len(sheet_rows) != SOURCE_SHEET_COUNT:
            raise IdentityInstrumentV2Error("source sample/sheet count mismatch")
        packet_rows = [
            {key: row[key] for key in (
                "lane", "display_order", "opaque_review_id", "sample_id",
                "sheet_path", "sheet_sha256", "sheet_bytes",
            )}
            for row in sheet_rows
        ]
        packet_sha = canonical_sha256(packet_rows)
        if enforce_source_contract and packet_sha != SOURCE_PACKET_BINDING_SHA256:
            raise IdentityInstrumentV2Error("authoritative WP2.6 packet binding changed")
        visual_inventory = [
            {
                "lane": row["lane"], "display_order": row["display_order"],
                "path": row["sheet_path"], "sha256": row["sheet_sha256"],
                "bytes": row["sheet_bytes"],
            }
            for row in sheet_rows
        ]
        visual_inventory_sha = canonical_sha256(visual_inventory)
        if enforce_source_contract and (
            visual_inventory_sha != SOURCE_VISUAL_INVENTORY_SHA256
            or sum(int(row["sheet_bytes"]) for row in sheet_rows) != 24_517_369
        ):
            raise IdentityInstrumentV2Error("WP2.6 visual inventory changed")
        if len(asset_rows) != SOURCE_ASSET_OCCURRENCES or len(asset_rows) > MAX_ASSET_OCCURRENCES:
            raise IdentityInstrumentV2Error("source asset occurrence count mismatch")
        per_side = Counter((str(row["sample_id"]), str(row["side"])) for row in asset_rows)
        if not per_side or max(per_side.values()) > MAX_ASSETS_PER_SIDE:
            raise IdentityInstrumentV2Error("source per-side asset cap exceeded")
        sample_ids = {str(row["sample_id"]) for row in sample_rows}
        if len(sample_ids) != TOTAL_COUNT:
            raise IdentityInstrumentV2Error("source sample IDs are not unique")
        for row in sample_rows:
            payload = {
                "run_id": run_id, "sample_rank": int(row["sample_rank"]),
                "sample_id": str(row["sample_id"]),
                "seed_rank_sha256": str(row["seed_rank_sha256"]),
                "control_commitment_sha256": str(row["gold_commitment_sha256"]),
                "snapshot_sha256": str(row["snapshot_sha256"]),
                "source_pair_record_sha256": str(row["record_sha256"]),
            }
            connection.execute("INSERT INTO source_samples VALUES(?,?,?,?,?,?,?,?)",
                               (*payload.values(), _record(payload)))
        for row in sheet_rows:
            sheet = _resolve_text(root, str(row["sheet_path"]))
            if (not sheet.is_file() or sheet.stat().st_size != int(row["sheet_bytes"])
                    or sha256_file(sheet) != str(row["sheet_sha256"])):
                raise IdentityInstrumentV2Error("frozen WP2.6 sheet missing or changed")
            payload = {"run_id": run_id, **{key: row[key] for key in (
                "lane", "display_order", "opaque_review_id", "sample_id", "sheet_path",
                "sheet_sha256", "sheet_bytes")}}
            payload = {**payload, "source_record_sha256": str(row["record_sha256"])}
            connection.execute("INSERT INTO source_sheets VALUES(?,?,?,?,?,?,?,?,?,?)",
                               (*payload.values(), _record(payload)))
        for row in asset_rows:
            payload = {"run_id": run_id, **{key: row[key] for key in (
                "sample_id", "side", "asset_order", "asset_key", "source",
                "source_building_id", "source_asset_id", "selection_reason")}}
            payload = {**payload, "source_record_sha256": str(row["record_sha256"])}
            connection.execute("INSERT INTO source_assets VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                               (*payload.values(), _record(payload)))
        for row in fetch_rows:
            cache_sha = None
            if row["cache_path"]:
                cache = _resolve_text(root, str(row["cache_path"]))
                if not cache.is_file():
                    raise IdentityInstrumentV2Error("source cache file is missing")
                cache_sha = sha256_file(cache)
            keys = (
                "asset_key", "source", "source_asset_id", "expected_url",
                "expected_raw_sha256", "expected_pixel_sha256", "status", "cache_role",
                "cache_path", "actual_final_url", "actual_raw_sha256", "actual_pixel_sha256",
                "http_status", "response_bytes", "request_count", "error_kind", "error_message",
            )
            values = [row[key] for key in keys]
            values.insert(9, cache_sha)
            fetch_columns = (
                "asset_key", "source", "source_asset_id", "expected_url",
                "expected_raw_sha256", "expected_pixel_sha256", "status", "cache_role",
                "cache_path", "cache_file_sha256", "actual_final_url", "actual_raw_sha256",
                "actual_pixel_sha256", "http_status", "response_bytes", "request_count",
                "error_kind", "error_message",
            )
            payload = {"run_id": run_id, **dict(zip(fetch_columns, values)),
                       "source_record_sha256": str(row["record_sha256"])}
            connection.execute(
                "INSERT INTO source_fetch_assets VALUES(" + ",".join("?" for _ in range(21)) + ")",
                (*payload.values(), _record(payload)),
            )
        for row in attempt_rows:
            payload = {"run_id": run_id, **{key: row[key] for key in (
                "asset_key", "attempt_no", "status", "request_count", "redirect_chain_json",
                "started_at", "completed_at", "error_kind")}}
            payload = {**payload, "source_record_sha256": str(row["record_sha256"])}
            connection.execute("INSERT INTO source_fetch_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                               (*payload.values(), _record(payload)))
        for row in cache_rows:
            keys = ("role", "path", "transform_version", "access_mode", "file_count_before",
                    "total_bytes_before", "tree_sha256_before", "file_count_after",
                    "total_bytes_after", "tree_sha256_after")
            payload = {"run_id": run_id, **{key: row[key] for key in keys}}
            payload["source_record_sha256"] = str(row["record_sha256"])
            connection.execute("INSERT INTO source_cache_pins VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (*payload.values(), _record(payload)))
        inherited_logical = len(attempt_rows)
        inherited_wire = sum(int(row["request_count"]) for row in attempt_rows)
        if inherited_logical > MAX_LOGICAL_HTTP_FETCHES or inherited_wire > MAX_WIRE_HTTP_REQUESTS:
            raise IdentityInstrumentV2Error("inherited fetch lineage exceeds 96 cap")
        connection.execute(
            "UPDATE runs SET status='sample_frozen',sample_count=?,sheet_count=?,"
            "asset_occurrence_count=?,unique_asset_count=?,inherited_logical_http_fetches=?,"
            "inherited_wire_http_requests=? WHERE run_id=?",
            (len(sample_rows), len(sheet_rows), len(asset_rows), len(fetch_rows),
             inherited_logical, inherited_wire, run_id),
        )
        _refresh_run_record(connection, run_id)
        _insert_event(connection, run_id, "instrument_frozen", "sample_frozen",
                      {"stage": "B", "samples": len(sample_rows), "sheets": len(sheet_rows),
                       "asset_occurrences": len(asset_rows),
                       "source_packet_binding_sha256": packet_sha,
                       "source_visual_inventory_sha256": visual_inventory_sha,
                       "incremental_http": 0, "incremental_renderer_substitutions": 0})
        payload = _sample_manifest_payload(connection, root, run_id, artifact)
        raw = _json_bytes(payload)
        _register_path(connection, root, run_id, "sample_manifest", manifest_path, "sample_freeze")
        _bind_values(connection, root, run_id, "sample_manifest", manifest_path,
                     "sample_freeze", raw)
        _store_json_payload(connection, run_id, "sample_manifest", payload)
        connection.commit()
        _atomic_bytes(manifest_path, raw)
        return SampleResult(artifact, manifest_path, run_id, len(sample_rows),
                            len(sheet_rows), len(asset_rows))
    except Exception:
        connection.rollback()
        raise
    finally:
        if source is not None:
            source.close()
        connection.close()


def declare_gate(*, repo_root: Path, artifact_path: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    """Stage C: freeze all four predicates before any reviewer form exists."""
    root = repo_root.resolve(); artifact = _path(root, artifact_path)
    connection = _connect(artifact)
    try:
        run = _one_run(connection); run_id, status = str(run["run_id"]), str(run["status"])
        _require_not_stopped(connection, run_id)
        gate = _gate_payload(); gate_sha = canonical_sha256(gate)
        existing = connection.execute("SELECT * FROM gate_contract WHERE run_id=?", (run_id,)).fetchone()
        if existing is not None:
            if str(existing["gate_sha256"]) != gate_sha:
                raise IdentityInstrumentV2Error("gate declaration recovery mismatch")
            return {**gate, "gate_sha256": gate_sha, "declared_at": str(existing["declared_at"])}
        if status != "sample_frozen":
            raise IdentityInstrumentV2Error("gate declaration requires sample_frozen")
        form_roles = (
            "r1_form", "r2_form", "r1_blank_form", "r2_blank_form",
            "adjudication_packet", "adjudication_form", "adjudication_blank_form",
        )
        placeholders = ",".join("?" for _ in form_roles)
        form_count = int(connection.execute(
            f"SELECT count(*) FROM artifacts WHERE role IN ({placeholders})", form_roles
        ).fetchone()[0])
        review_count = sum(int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
                           for table in ("review_observations", "review_locks", "review_lock_payloads"))
        adjudication_count = sum(
            int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            for table in (
                "adjudication_items", "adjudicated_observations", "adjudication_lock",
                "adjudication_lock_payload", "control_reveals", "gate_scores",
            )
        )
        if form_count or review_count or adjudication_count:
            raise IdentityInstrumentV2Error("gate declaration is too late")
        declared = utc_now()
        gate_json = canonical_json(gate)
        payload = {
            "run_id": run_id, "gate_json": gate_json, "gate_sha256": gate_sha,
            "d_min_same": 13, "d_n": 15, "p1_min_non_same": 2, "p1_n": 3,
            "insufficient_min_exact": 4, "insufficient_n": 7,
            "related_min_exact": 1, "related_n": 2, "declared_at": declared,
        }
        connection.execute(
            "INSERT INTO gate_contract VALUES(" + ",".join("?" for _ in range(13)) + ")",
            (*payload.values(), _record(payload)),
        )
        connection.execute("UPDATE runs SET status='criteria_frozen',gate_status='declared' WHERE run_id=?",
                           (run_id,))
        _refresh_run_record(connection, run_id)
        _insert_event(connection, run_id, "sample_frozen", "criteria_frozen",
                      {"stage": "C", "gate_sha256": gate_sha,
                       "pre_form_count": form_count, "pre_review_count": review_count,
                       "pre_adjudication_count": adjudication_count})
        connection.commit()
        return {**gate, "gate_sha256": gate_sha, "declared_at": declared}
    except Exception:
        connection.rollback(); raise
    finally:
        connection.close()


def record_quota_observation(*, repo_root: Path, phase: str, remaining_percent: float,
                             source: str, observed_at: str, account_index: int = 0,
                             limitation: str = "", artifact_path: Path = DEFAULT_OUTPUT
                             ) -> dict[str, Any]:
    root = repo_root.resolve(); artifact = _path(root, artifact_path)
    if phase not in QUOTA_INDEX or phase == "pre_freeze":
        raise IdentityInstrumentV2Error("invalid runtime quota phase")
    if not 0 <= remaining_percent <= 100 or not source.strip():
        raise IdentityInstrumentV2Error("valid quota percentage and source are required")
    observed = _quota_time(observed_at)
    connection = _connect(artifact)
    try:
        run = _one_run(connection); run_id = str(run["run_id"])
        _require_not_stopped(connection, run_id)
        status_by_phase = {
            "pre_r1_review": {"criteria_frozen"},
            "pre_r2_review": {"reviews_partial"},
            "pre_adjudication_review": {"reviews_locked"},
            "pre_reveal_score": {"adjudication_locked"},
        }
        if str(run["status"]) not in status_by_phase[phase]:
            raise IdentityInstrumentV2Error(f"quota phase {phase} invalid in current state")
        rows = _quota_rows(connection, run_id)
        latest = rows[-1]
        latest_index, requested_index = QUOTA_INDEX[str(latest["phase"])], QUOTA_INDEX[phase]
        if requested_index not in {latest_index, latest_index + 1}:
            raise IdentityInstrumentV2Error("quota phase is out of order")
        if observed < _quota_time(latest["observed_at"], require_fresh=False):
            raise IdentityInstrumentV2Error("quota observation times are not monotonic")
        number = int(latest["observation_no"]) + 1
        stop, allowed = remaining_percent <= 20, remaining_percent > 30
        payload = {"run_id": run_id, "observation_no": number, "phase": phase,
                   "account_index": account_index,
                   "observed_at": observed_at.strip(), "source": source.strip(),
                   "remaining_percent": float(remaining_percent),
                   "start_allowed": int(allowed), "stop_required": int(stop),
                   "limitation": limitation.strip()}
        connection.execute("INSERT INTO quota_observations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (run_id, number, phase, account_index, observed_at.strip(), source.strip(),
                            float(remaining_percent), int(allowed), int(stop), limitation.strip(),
                            _record(payload)))
        if stop:
            connection.execute("UPDATE runs SET sticky_stop=1 WHERE run_id=?", (run_id,))
            _refresh_run_record(connection, run_id)
        connection.commit()
        if stop:
            raise IdentityInstrumentV2Error("weekly quota is at or below 20%; sticky stop set")
        if not allowed:
            raise IdentityInstrumentV2Error("weekly quota is at or below 30%; wave denied")
        return {"run_id": run_id, **payload}
    finally:
        connection.close()


REVIEW_FORM_TOP_KEYS = frozenset({
    "run_id", "review_version", "lane", "instrument_contract_sha256",
    "instructions", "form_contract_sha256", "reviewer", "reviewed_at", "items",
})
REVIEW_FORM_ITEM_KEYS = frozenset({
    "display_order", "opaque_review_id", "sheet_path", "sheet_sha256", *OBSERVATION_KEYS,
})


def _review_form_payload(connection: sqlite3.Connection, run_id: str, lane: str) -> dict[str, Any]:
    instrument_sha = str(connection.execute(
        "SELECT contract_sha256 FROM instrument_contract WHERE run_id=?", (run_id,)
    ).fetchone()[0])
    items = []
    bindings = []
    for row in connection.execute(
            "SELECT * FROM source_sheets WHERE run_id=? AND lane=? ORDER BY display_order",
            (run_id, lane)):
        binding = {key: row[key] for key in (
            "display_order", "opaque_review_id", "sheet_path", "sheet_sha256")}
        bindings.append(binding)
        items.append({**binding, **{key: "" for key in OBSERVATION_KEYS}})
    if len(items) != TOTAL_COUNT:
        raise IdentityInstrumentV2Error("review form requires 24 frozen sheets")
    instructions = _axis_instructions()
    contract = {"run_id": run_id, "review_version": REVIEW_VERSION, "lane": lane,
                "instrument_contract_sha256": instrument_sha,
                "instructions": instructions, "items": bindings}
    return {**contract, "form_contract_sha256": canonical_sha256(contract),
            "reviewer": "", "reviewed_at": "", "items": items}


def _source_fetch_payloads(connection: sqlite3.Connection, run_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    statuses = dict(sorted(Counter(str(row[0]) for row in connection.execute(
        "SELECT status FROM source_fetch_assets WHERE run_id=?", (run_id,))).items()))
    correction = connection.execute(
        "SELECT path,size_bytes,sha256 FROM input_pins WHERE run_id=? "
        "AND role='wp26_fetch_correction'", (run_id,)
    ).fetchone()
    correction_binding = {
        "path": str(correction["path"]), "size_bytes": int(correction["size_bytes"]),
        "sha256": str(correction["sha256"]),
        "corrected_projection_sha256":
            "439426d0706c7eac1b922af9fb8f042fb7bf3cb514cda809363daef7c0fc72b3",
        "native_hard_caps": {"asset_occurrences": 96, "logical_http_fetches": 96,
                             "wire_http_requests": 96, "redirects": 0, "retries": 0},
    }
    base = {
        "status": "PASS", "run_id": run_id, "evidence_reuse": "read_only_wp26",
        "counts": {"samples": int(run["sample_count"]), "sheets": int(run["sheet_count"]),
                   "asset_occurrences": int(run["asset_occurrence_count"]),
                   "unique_assets": int(run["unique_asset_count"]), "statuses": statuses},
        "hard_caps": {"asset_occurrences": 96, "logical_http_fetches": 96,
                      "wire_http_requests": 96, "redirects": 0, "retries": 0},
        "source_generation_network": {
            "logical_http_fetches": int(run["inherited_logical_http_fetches"]),
            "wire_http_requests": int(run["inherited_wire_http_requests"]),
            "redirects": 0, "retries": 0,
        },
        "source_correction": correction_binding,
        "incremental_network": {"incremental_http_requests": 0,
                                "logical_http_fetches": 0, "wire_http_requests": 0,
                                "redirects": 0, "retries": 0},
        "cache_writes": 0, "renderer_substitutions": 0,
    }
    manifest = {**base, "assets": [{key: row[key] for key in (
        "asset_key", "source", "source_asset_id", "status", "cache_role", "cache_path",
        "cache_file_sha256", "request_count", "error_kind")}
        for row in connection.execute(
            "SELECT * FROM source_fetch_assets WHERE run_id=? ORDER BY asset_key", (run_id,))]}
    report = {**base, "pixel_drift_count": statuses.get("source_changed", 0)}
    return manifest, report


def prepare_review(*, repo_root: Path, artifact_path: Path = DEFAULT_OUTPUT,
                   r1_form_path: Path = DEFAULT_R1_FORM,
                   r2_form_path: Path = DEFAULT_R2_FORM,
                   fetch_manifest_path: Path = DEFAULT_FETCH_MANIFEST,
                   fetch_report_path: Path = DEFAULT_FETCH_REPORT) -> PrepareResult:
    """Publish only v2 forms over the byte-pinned WP2.6 sheets; HTTP is impossible."""
    root = repo_root.resolve(); artifact = _path(root, artifact_path)
    targets = {
        "r1_form": _path(root, r1_form_path),
        "r2_form": _path(root, r2_form_path),
        "fetch_manifest": _path(root, fetch_manifest_path),
        "fetch_report": _path(root, fetch_report_path),
    }
    if len({path.resolve() for path in targets.values()}) != len(targets):
        raise IdentityInstrumentV2Error("review output paths must be distinct")
    connection = _connect(artifact)
    try:
        run = _one_run(connection); run_id, status = str(run["run_id"]), str(run["status"])
        _require_not_stopped(connection, run_id)
        if connection.execute(
            "SELECT count(*) FROM artifacts WHERE run_id=? AND role IN "
            "('r1_form','r2_form','fetch_manifest','fetch_report')", (run_id,)
        ).fetchone()[0] == 4:
            for role, path in targets.items():
                _recover_artifact(connection, root, run_id, role, path)
            return PrepareResult(artifact, run_id, (targets["r1_form"], targets["r2_form"]))
        if status != "criteria_frozen":
            raise IdentityInstrumentV2Error("prepare requires criteria_frozen")
        _require_quota(connection, run_id, "pre_r1_review")
        if any(path.exists() for path in targets.values()):
            raise IdentityInstrumentV2Error("refusing to clobber initial review artifact")
        gate = connection.execute("SELECT 1 FROM gate_contract WHERE run_id=?", (run_id,)).fetchone()
        if gate is None:
            raise IdentityInstrumentV2Error("review forms require frozen criteria")
        payloads = {"r1_form": _review_form_payload(connection, run_id, "r1"),
                    "r2_form": _review_form_payload(connection, run_id, "r2")}
        payloads["fetch_manifest"], payloads["fetch_report"] = _source_fetch_payloads(connection, run_id)
        raws: dict[str, bytes] = {}
        for role, payload in payloads.items():
            path = targets[role]
            _register_path(connection, root, run_id, role, path, "prepare_review")
            raw = _store_json_payload(connection, run_id, role, payload)
            _bind_values(connection, root, run_id, role, path, "prepare_review", raw)
            raws[role] = raw
        connection.execute("UPDATE runs SET status='review_ready' WHERE run_id=?", (run_id,))
        _refresh_run_record(connection, run_id)
        _insert_event(connection, run_id, "criteria_frozen", "review_ready",
                      {"forms": 2, "source_sheets": SOURCE_SHEET_COUNT,
                       "incremental_http": 0, "incremental_cache_writes": 0,
                       "incremental_renderer_substitutions": 0})
        connection.commit()
        for role, path in targets.items():
            _atomic_bytes(path, raws[role])
        return PrepareResult(artifact, run_id, (targets["r1_form"], targets["r2_form"]))
    except Exception:
        connection.rollback(); raise
    finally:
        connection.close()


def _actor(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise IdentityInstrumentV2Error(f"{label} is required")
    if value != value.strip() or value != " ".join(value.split()):
        raise IdentityInstrumentV2Error(f"{label} must be normalized text")
    return value


def _actor_key(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _require_at_or_after_criteria(connection: sqlite3.Connection, run_id: str,
                                  value: str, label: str) -> None:
    row = connection.execute(
        "SELECT declared_at FROM gate_contract WHERE run_id=?", (run_id,)
    ).fetchone()
    if row is None:
        raise IdentityInstrumentV2Error(f"{label} requires frozen criteria")
    if _quota_time(value, require_fresh=False) < _quota_time(row[0], require_fresh=False):
        raise IdentityInstrumentV2Error(f"{label} predates criteria_frozen")


def _require_at_or_after_artifact(connection: sqlite3.Connection, run_id: str,
                                  role: str, value: str, label: str) -> None:
    row = connection.execute(
        "SELECT created_at FROM artifacts WHERE run_id=? AND role=?", (run_id, role)
    ).fetchone()
    if row is None or _quota_time(value, require_fresh=False) < _quota_time(
        row[0], require_fresh=False
    ):
        raise IdentityInstrumentV2Error(f"{label} predates {role}")


def _completed_review(connection: sqlite3.Connection, root: Path, run_id: str,
                      lane: str, form: Mapping[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
    if set(form) != REVIEW_FORM_TOP_KEYS or any(key in form for key in FORBIDDEN_INPUT_FIELDS):
        raise IdentityInstrumentV2Error("review form top-level keys are not exact")
    if (form["run_id"] != run_id or form["review_version"] != REVIEW_VERSION
            or form["lane"] != lane):
        raise IdentityInstrumentV2Error("review form version/run/lane mismatch")
    instrument_sha = str(connection.execute(
        "SELECT contract_sha256 FROM instrument_contract WHERE run_id=?", (run_id,)
    ).fetchone()[0])
    if form["instrument_contract_sha256"] != instrument_sha:
        raise IdentityInstrumentV2Error("review instrument contract changed")
    if form["instructions"] != _axis_instructions():
        raise IdentityInstrumentV2Error("review axis instructions changed")
    reviewer = _actor(form["reviewer"], "reviewer")
    reviewed_at = str(form["reviewed_at"] or "").strip()
    _require_at_or_after_criteria(connection, run_id, reviewed_at, "reviewed_at")
    _require_at_or_after_artifact(connection, run_id, f"{lane}_form", reviewed_at, "reviewed_at")
    raw_items = form["items"]
    if not isinstance(raw_items, list) or len(raw_items) != TOTAL_COUNT:
        raise IdentityInstrumentV2Error("completed review form must contain 24 rows")
    expected = {str(row["opaque_review_id"]): row for row in connection.execute(
        "SELECT * FROM source_sheets WHERE run_id=? AND lane=?", (run_id, lane))}
    seen: set[str] = set(); validated = []; contract_items = []
    for item in raw_items:
        if not isinstance(item, Mapping) or set(item) != REVIEW_FORM_ITEM_KEYS:
            raise IdentityInstrumentV2Error("review item keys are not exact")
        if any(key in item for key in FORBIDDEN_INPUT_FIELDS):
            raise IdentityInstrumentV2Error("review item contains a forbidden label field")
        opaque = str(item["opaque_review_id"]); frozen = expected.get(opaque)
        if frozen is None or opaque in seen:
            raise IdentityInstrumentV2Error("unknown or duplicate review ID")
        seen.add(opaque)
        for key in ("display_order", "sheet_path", "sheet_sha256"):
            if item[key] != frozen[key]:
                raise IdentityInstrumentV2Error("review sheet binding changed")
        sheet = _resolve_text(root, str(frozen["sheet_path"]))
        if not sheet.is_file() or sha256_file(sheet) != str(frozen["sheet_sha256"]):
            raise IdentityInstrumentV2Error("review sheet missing or changed")
        observation = validate_observation({key: item[key] for key in OBSERVATION_KEYS})
        binding = {key: item[key] for key in (
            "display_order", "opaque_review_id", "sheet_path", "sheet_sha256")}
        contract_items.append(binding)
        validated.append({**binding, "sample_id": str(frozen["sample_id"]), **observation})
    if seen != set(expected):
        raise IdentityInstrumentV2Error("review form omits frozen rows")
    contract_items.sort(key=lambda row: int(row["display_order"]))
    contract = {"run_id": run_id, "review_version": REVIEW_VERSION, "lane": lane,
                "instrument_contract_sha256": instrument_sha,
                "instructions": _axis_instructions(), "items": contract_items}
    if form["form_contract_sha256"] != canonical_sha256(contract):
        raise IdentityInstrumentV2Error("review form contract SHA mismatch")
    return reviewer, reviewed_at, sorted(validated, key=lambda row: int(row["display_order"]))


def lock_review(*, repo_root: Path, lane: str, completed_form_path: Path,
                locked_output_path: Path, artifact_path: Path = DEFAULT_OUTPUT) -> Path:
    if lane not in {"r1", "r2"}:
        raise IdentityInstrumentV2Error("lane must be r1 or r2")
    root = repo_root.resolve(); artifact = _path(root, artifact_path)
    completed, locked = _path(root, completed_form_path), _path(root, locked_output_path)
    if completed.resolve() == locked.resolve():
        raise IdentityInstrumentV2Error("completed/locked review paths are invalid")
    connection = _connect(artifact)
    try:
        run = _one_run(connection); run_id, status = str(run["run_id"]), str(run["status"])
        _require_not_stopped(connection, run_id)
        existing = connection.execute(
            "SELECT * FROM review_locks WHERE run_id=? AND lane=?", (run_id, lane)
        ).fetchone()
        if existing is not None:
            if (_resolve_text(root, str(existing["completed_form_path"])).resolve() != completed.resolve()
                    or _resolve_text(root, str(existing["locked_output_path"])).resolve() != locked.resolve()):
                raise IdentityInstrumentV2Error("review-lock recovery mismatch")
            _recover_artifact(connection, root, run_id, f"{lane}_completed", completed)
            _recover_artifact(connection, root, run_id, f"{lane}_locked", locked)
            return locked
        expected_status = "review_ready" if lane == "r1" else "reviews_partial"
        if status != expected_status:
            raise IdentityInstrumentV2Error(f"{lane} lock requires {expected_status}")
        if locked.exists():
            raise IdentityInstrumentV2Error("refusing to clobber review lock")
        if not completed.is_file():
            raise IdentityInstrumentV2Error("completed review form is missing")
        form = _load_json(completed, "completed review form")
        _require_quota(connection, run_id, f"pre_{lane}_review")
        blank_binding = connection.execute(
            "SELECT * FROM artifact_bindings WHERE run_id=? AND role=?", (run_id, f"{lane}_form")
        ).fetchone()
        blank = _resolve_text(root, str(blank_binding["path"])) if blank_binding else None
        if (blank is None or not blank.is_file() or blank.resolve() == completed.resolve()
                or sha256_file(blank) == sha256_file(completed)):
            raise IdentityInstrumentV2Error("completed form must differ from blank form")
        reviewer, reviewed_at, rows = _completed_review(connection, root, run_id, lane, form)
        other = connection.execute("SELECT reviewer FROM review_locks WHERE run_id=?", (run_id,)).fetchone()
        if other is not None and _actor_key(other[0]) == _actor_key(reviewer):
            raise IdentityInstrumentV2Error("reviewers must be distinct")
        ledger = []
        for row in rows:
            observation = {key: row[key] for key in OBSERVATION_KEYS}
            semantic_observation = {"lane": lane, "opaque_review_id": row["opaque_review_id"],
                                    "sample_id": row["sample_id"], **observation,
                                    "reviewer": reviewer, "reviewed_at": reviewed_at}
            observation_sha = canonical_sha256(semantic_observation)
            observation_payload = {
                "run_id": run_id, "lane": lane, "display_order": row["display_order"],
                "opaque_review_id": row["opaque_review_id"], "sample_id": row["sample_id"],
                **observation, "reviewer": reviewer, "reviewed_at": reviewed_at,
                "observation_sha256": observation_sha,
            }
            connection.execute(
                "INSERT INTO review_observations VALUES(" + ",".join("?" for _ in range(16)) + ")",
                (*observation_payload.values(), _record(observation_payload)),
            )
            ledger.append({"lane": lane, "opaque_review_id": row["opaque_review_id"],
                           **observation, "reviewer": reviewer, "reviewed_at": reviewed_at})
        ordered_sha = canonical_sha256(ledger)
        instrument_sha = str(connection.execute(
            "SELECT contract_sha256 FROM instrument_contract WHERE run_id=?", (run_id,)
        ).fetchone()[0])
        locked_payload = {"run_id": run_id, "review_version": REVIEW_VERSION, "lane": lane,
                          "instrument_contract_sha256": instrument_sha,
                          "instructions": _axis_instructions(),
                          "form_contract_sha256": str(form["form_contract_sha256"]),
                          "reviewer": reviewer, "reviewed_at": reviewed_at,
                          "row_count": TOTAL_COUNT, "ordered_ledger_sha256": ordered_sha,
                          "items": ledger}
        raw = _json_bytes(locked_payload); digest = hashlib.sha256(raw).hexdigest()
        completed_sha = sha256_file(completed); locked_at = utc_now()
        if _quota_time(reviewed_at, require_fresh=False) > _quota_time(
            locked_at, require_fresh=False
        ):
            raise IdentityInstrumentV2Error("reviewed_at cannot follow locked_at")
        completed_role, locked_role = f"{lane}_completed", f"{lane}_locked"
        _register_path(connection, root, run_id, completed_role, completed, "lock_review")
        _bind_values(connection, root, run_id, completed_role, completed, "lock_review",
                     completed.read_bytes())
        _register_path(connection, root, run_id, locked_role, locked, "lock_review")
        _bind_values(connection, root, run_id, locked_role, locked, "lock_review", raw)
        locked_payload_json = canonical_json(locked_payload)
        lock_payload_row = {"run_id": run_id, "lane": lane,
                            "payload_json": locked_payload_json}
        connection.execute("INSERT INTO review_lock_payloads VALUES(?,?,?,?)",
                           (*lock_payload_row.values(), _record(lock_payload_row)))
        lock_payload = {"run_id": run_id, "lane": lane, "reviewer": reviewer,
                        "row_count": TOTAL_COUNT,
                        "completed_form_path": _path_text(root, completed),
                        "completed_form_sha256": completed_sha,
                        "locked_output_path": _path_text(root, locked),
                        "locked_output_sha256": digest,
                        "ordered_ledger_sha256": ordered_sha, "locked_at": locked_at}
        _require_at_or_after_criteria(connection, run_id, locked_at, "locked_at")
        connection.execute("INSERT INTO review_locks VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (*lock_payload.values(), _record(lock_payload)))
        after = "reviews_partial" if lane == "r1" else "reviews_locked"
        connection.execute(f"UPDATE runs SET status=?,{lane}_status='locked' WHERE run_id=?",
                           (after, run_id))
        _refresh_run_record(connection, run_id)
        _insert_event(connection, run_id, status, after,
                      {"lane": lane, "rows": TOTAL_COUNT, "ledger_sha256": ordered_sha})
        connection.commit()
        _atomic_bytes(locked, raw)
        return locked
    except Exception:
        connection.rollback(); raise
    finally:
        connection.close()


ADJUDICATION_FORM_TOP_KEYS = frozenset({
    "run_id", "adjudication_version", "instrument_contract_sha256",
    "instructions", "form_contract_sha256", "adjudicator", "adjudicated_at", "items",
})
ADJUDICATION_FORM_ITEM_KEYS = frozenset({
    "display_order", "opaque_adjudication_id", "sheet_path", "sheet_sha256", *OBSERVATION_KEYS,
})


def _review_value(row: sqlite3.Row) -> dict[str, Any]:
    return {"opaque_review_id": str(row["opaque_review_id"]),
            **{key: str(row[key]) for key in OBSERVATION_KEYS},
            "reviewer": str(row["reviewer"]), "reviewed_at": str(row["reviewed_at"])}


def _adjudication_payloads(connection: sqlite3.Connection, run_id: str
                           ) -> tuple[dict[str, Any], dict[str, Any]]:
    instrument_sha = str(connection.execute(
        "SELECT contract_sha256 FROM instrument_contract WHERE run_id=?", (run_id,)
    ).fetchone()[0])
    packet_items, form_items, contract_items = [], [], []
    samples = connection.execute(
        "SELECT sample_id,sample_rank FROM source_samples WHERE run_id=? ORDER BY sample_rank", (run_id,)
    ).fetchall()
    for display_order, sample in enumerate(samples, 1):
        sample_id = str(sample["sample_id"])
        reviews = {str(row["lane"]): row for row in connection.execute(
            "SELECT * FROM review_observations WHERE run_id=? AND sample_id=? ORDER BY lane",
            (run_id, sample_id))}
        if set(reviews) != {"r1", "r2"}:
            raise IdentityInstrumentV2Error("adjudication row lacks two reviews")
        sheet = connection.execute(
            "SELECT sheet_path,sheet_sha256 FROM source_sheets "
            "WHERE run_id=? AND lane='r1' AND sample_id=?", (run_id, sample_id)
        ).fetchone()
        opaque = "adj_" + hashlib.sha256(
            f"{ADJUDICATION_VERSION}\0{run_id}\0{sample_id}".encode("utf-8")
        ).hexdigest()[:24]
        binding = {"display_order": display_order, "opaque_adjudication_id": opaque,
                   "sheet_path": str(sheet["sheet_path"]),
                   "sheet_sha256": str(sheet["sheet_sha256"])}
        packet_items.append({**binding,
                             "reviews": {lane: _review_value(reviews[lane]) for lane in ("r1", "r2")}})
        form_items.append({**binding, **{key: "" for key in OBSERVATION_KEYS}})
        contract_items.append(binding)
        payload = {"run_id": run_id, "display_order": display_order,
                   "opaque_adjudication_id": opaque, "sample_id": sample_id,
                   "sheet_path": binding["sheet_path"],
                   "sheet_sha256": binding["sheet_sha256"],
                   "r1_observation_sha256": str(reviews["r1"]["observation_sha256"]),
                   "r2_observation_sha256": str(reviews["r2"]["observation_sha256"])}
        connection.execute("INSERT INTO adjudication_items VALUES(?,?,?,?,?,?,?,?,?)",
                           (*payload.values(), _record(payload)))
    instructions = _axis_instructions()
    contract = {"run_id": run_id, "adjudication_version": ADJUDICATION_VERSION,
                "instrument_contract_sha256": instrument_sha,
                "instructions": instructions, "items": contract_items}
    packet_contract = {
        "run_id": run_id, "adjudication_version": ADJUDICATION_VERSION,
        "instrument_contract_sha256": instrument_sha, "instructions": instructions,
        "row_count": TOTAL_COUNT, "items": packet_items,
    }
    packet = {**packet_contract,
              "packet_contract_sha256": canonical_sha256(packet_contract)}
    form = {**contract, "form_contract_sha256": canonical_sha256(contract),
            "adjudicator": "", "adjudicated_at": "", "items": form_items}
    return packet, form


def prepare_adjudication(*, repo_root: Path, artifact_path: Path = DEFAULT_OUTPUT,
                         packet_path: Path = DEFAULT_ADJUDICATION_PACKET,
                         adjudication_form_path: Path = DEFAULT_ADJUDICATION_FORM
                         ) -> tuple[Path, Path]:
    root = repo_root.resolve(); artifact = _path(root, artifact_path)
    packet, form_path = _path(root, packet_path), _path(root, adjudication_form_path)
    if packet.resolve() == form_path.resolve():
        raise IdentityInstrumentV2Error("adjudication packet/form paths must differ")
    connection = _connect(artifact)
    try:
        run = _one_run(connection); run_id, status = str(run["run_id"]), str(run["status"])
        _require_not_stopped(connection, run_id)
        existing = connection.execute(
            "SELECT 1 FROM json_payloads WHERE run_id=? AND role='adjudication_packet'", (run_id,)
        ).fetchone()
        if existing is not None:
            for role, path in (("adjudication_packet", packet), ("adjudication_form", form_path)):
                _recover_artifact(connection, root, run_id, role, path)
            return packet, form_path
        if status != "reviews_locked":
            raise IdentityInstrumentV2Error("adjudication prepare requires reviews_locked")
        _require_quota(connection, run_id, "pre_adjudication_review")
        if packet.exists() or form_path.exists():
            raise IdentityInstrumentV2Error("refusing to clobber adjudication artifacts")
        payloads = dict(zip(("adjudication_packet", "adjudication_form"),
                            _adjudication_payloads(connection, run_id)))
        raws = {}
        for role, path in (("adjudication_packet", packet), ("adjudication_form", form_path)):
            _register_path(connection, root, run_id, role, path, "prepare_adjudication")
            raws[role] = _store_json_payload(connection, run_id, role, payloads[role])
            _bind_values(connection, root, run_id, role, path, "prepare_adjudication", raws[role])
        connection.commit()
        _atomic_bytes(packet, raws["adjudication_packet"])
        _atomic_bytes(form_path, raws["adjudication_form"])
        return packet, form_path
    except Exception:
        connection.rollback(); raise
    finally:
        connection.close()


def _completed_adjudication(connection: sqlite3.Connection, root: Path, run_id: str,
                            form: Mapping[str, Any]
                            ) -> tuple[str, str, list[dict[str, Any]]]:
    if set(form) != ADJUDICATION_FORM_TOP_KEYS or any(
            key in form for key in FORBIDDEN_INPUT_FIELDS):
        raise IdentityInstrumentV2Error("adjudication form top-level keys are not exact")
    if form["run_id"] != run_id or form["adjudication_version"] != ADJUDICATION_VERSION:
        raise IdentityInstrumentV2Error("adjudication version/run mismatch")
    instrument_sha = str(connection.execute(
        "SELECT contract_sha256 FROM instrument_contract WHERE run_id=?", (run_id,)
    ).fetchone()[0])
    if form["instrument_contract_sha256"] != instrument_sha:
        raise IdentityInstrumentV2Error("adjudication instrument contract changed")
    if form["instructions"] != _axis_instructions():
        raise IdentityInstrumentV2Error("adjudication axis instructions changed")
    adjudicator = _actor(form["adjudicator"], "adjudicator")
    adjudicated_at = str(form["adjudicated_at"] or "").strip()
    _require_at_or_after_criteria(connection, run_id, adjudicated_at, "adjudicated_at")
    _require_at_or_after_artifact(
        connection, run_id, "adjudication_form", adjudicated_at, "adjudicated_at"
    )
    raw_items = form["items"]
    if not isinstance(raw_items, list) or len(raw_items) != TOTAL_COUNT:
        raise IdentityInstrumentV2Error("completed adjudication must contain 24 rows")
    expected = {str(row["opaque_adjudication_id"]): row for row in connection.execute(
        "SELECT * FROM adjudication_items WHERE run_id=?", (run_id,))}
    seen: set[str] = set(); rows = []; contract_items = []
    for item in raw_items:
        if not isinstance(item, Mapping) or set(item) != ADJUDICATION_FORM_ITEM_KEYS:
            raise IdentityInstrumentV2Error("adjudication item keys are not exact")
        if any(key in item for key in FORBIDDEN_INPUT_FIELDS):
            raise IdentityInstrumentV2Error("adjudication item contains a forbidden label field")
        opaque = str(item["opaque_adjudication_id"]); frozen = expected.get(opaque)
        if frozen is None or opaque in seen:
            raise IdentityInstrumentV2Error("unknown or duplicate adjudication ID")
        seen.add(opaque)
        for key in ("display_order", "sheet_path", "sheet_sha256"):
            if item[key] != frozen[key]:
                raise IdentityInstrumentV2Error("adjudication sheet binding changed")
        sheet = _resolve_text(root, str(frozen["sheet_path"]))
        if not sheet.is_file() or sha256_file(sheet) != str(frozen["sheet_sha256"]):
            raise IdentityInstrumentV2Error("adjudication sheet missing or changed")
        observation = validate_observation({key: item[key] for key in OBSERVATION_KEYS})
        binding = {key: item[key] for key in (
            "display_order", "opaque_adjudication_id", "sheet_path", "sheet_sha256")}
        contract_items.append(binding)
        rows.append({**binding, "sample_id": str(frozen["sample_id"]), **observation})
    if seen != set(expected):
        raise IdentityInstrumentV2Error("adjudication form omits frozen rows")
    contract_items.sort(key=lambda row: int(row["display_order"]))
    contract = {"run_id": run_id, "adjudication_version": ADJUDICATION_VERSION,
                "instrument_contract_sha256": instrument_sha,
                "instructions": _axis_instructions(), "items": contract_items}
    if form["form_contract_sha256"] != canonical_sha256(contract):
        raise IdentityInstrumentV2Error("adjudication form contract SHA mismatch")
    return adjudicator, adjudicated_at, sorted(rows, key=lambda row: int(row["display_order"]))


def _adjustments(connection: sqlite3.Connection, run_id: str, sample_id: str,
                 final: Mapping[str, str]) -> dict[str, Any]:
    reviews = {str(row["lane"]): row for row in connection.execute(
        "SELECT * FROM review_observations WHERE run_id=? AND sample_id=?", (run_id, sample_id))}
    result = {}
    for key in OBSERVATION_KEYS:
        r1, r2, chosen = str(reviews["r1"][key]), str(reviews["r2"][key]), final[key]
        result[key] = {"r1": r1, "r2": r2, "final": chosen,
                       "reviewers_disagreed": r1 != r2,
                       "changed_from_r1": chosen != r1, "changed_from_r2": chosen != r2}
    return result


def lock_adjudication(*, repo_root: Path, completed_form_path: Path,
                      locked_output_path: Path = DEFAULT_ADJUDICATION_LOCKED,
                      artifact_path: Path = DEFAULT_OUTPUT) -> Path:
    root = repo_root.resolve(); artifact = _path(root, artifact_path)
    completed, locked = _path(root, completed_form_path), _path(root, locked_output_path)
    if completed.resolve() == locked.resolve():
        raise IdentityInstrumentV2Error("completed/locked adjudication paths are invalid")
    connection = _connect(artifact)
    try:
        run = _one_run(connection); run_id, status = str(run["run_id"]), str(run["status"])
        _require_not_stopped(connection, run_id)
        existing = connection.execute("SELECT * FROM adjudication_lock WHERE run_id=?", (run_id,)).fetchone()
        if existing is not None:
            if (_resolve_text(root, str(existing["completed_form_path"])).resolve() != completed.resolve()
                    or _resolve_text(root, str(existing["locked_output_path"])).resolve() != locked.resolve()):
                raise IdentityInstrumentV2Error("adjudication-lock recovery mismatch")
            _recover_artifact(connection, root, run_id, "adjudication_completed", completed)
            _recover_artifact(connection, root, run_id, "adjudication_locked", locked)
            return locked
        if status != "reviews_locked":
            raise IdentityInstrumentV2Error("adjudication lock requires reviews_locked")
        if locked.exists():
            raise IdentityInstrumentV2Error("refusing to clobber adjudication lock")
        if not completed.is_file():
            raise IdentityInstrumentV2Error("completed adjudication form is missing")
        form = _load_json(completed, "completed adjudication form")
        blank_binding = connection.execute(
            "SELECT * FROM artifact_bindings WHERE run_id=? AND role='adjudication_form'",
            (run_id,),
        ).fetchone()
        blank = _resolve_text(root, str(blank_binding["path"])) if blank_binding else None
        if (blank is None or not blank.is_file() or blank.resolve() == completed.resolve()
                or sha256_file(blank) == sha256_file(completed)):
            raise IdentityInstrumentV2Error("completed adjudication must differ from blank")
        adjudicator, adjudicated_at, rows = _completed_adjudication(
            connection, root, run_id, form)
        reviewers = [str(row[0]) for row in connection.execute(
            "SELECT reviewer FROM review_locks WHERE run_id=?", (run_id,))]
        if len(reviewers) != 2 or any(_actor_key(name) == _actor_key(adjudicator) for name in reviewers):
            raise IdentityInstrumentV2Error("adjudicator must be distinct from both reviewers")
        ledger = []
        for row in rows:
            observation = {key: row[key] for key in OBSERVATION_KEYS}
            label, rule = derive_label(observation)
            adjustments = _adjustments(connection, run_id, row["sample_id"], observation)
            semantic_observation = {"opaque_adjudication_id": row["opaque_adjudication_id"],
                                    "sample_id": row["sample_id"], **observation,
                                    "derived_label": label, "derivation_rule": rule,
                                    "axis_adjustments": adjustments,
                                    "adjudicator": adjudicator,
                                    "adjudicated_at": adjudicated_at}
            observation_sha = canonical_sha256(semantic_observation)
            adjustments_json = canonical_json(adjustments)
            observation_payload = {
                "run_id": run_id, "opaque_adjudication_id": row["opaque_adjudication_id"],
                "sample_id": row["sample_id"], **observation, "derived_label": label,
                "derivation_rule": rule, "axis_adjustments_json": adjustments_json,
                "adjudicator": adjudicator, "adjudicated_at": adjudicated_at,
                "observation_sha256": observation_sha,
            }
            connection.execute(
                "INSERT INTO adjudicated_observations VALUES(" + ",".join("?" for _ in range(17)) + ")",
                (*observation_payload.values(), _record(observation_payload)),
            )
            ledger.append({"opaque_adjudication_id": row["opaque_adjudication_id"],
                           **observation, "adjudicator": adjudicator,
                           "adjudicated_at": adjudicated_at})
        ordered_sha = canonical_sha256(ledger)
        instrument_sha = str(connection.execute(
            "SELECT contract_sha256 FROM instrument_contract WHERE run_id=?", (run_id,)
        ).fetchone()[0])
        locked_payload = {"run_id": run_id, "adjudication_version": ADJUDICATION_VERSION,
                          "instrument_contract_sha256": instrument_sha,
                          "instructions": _axis_instructions(),
                          "form_contract_sha256": str(form["form_contract_sha256"]),
                          "packet_contract_sha256": str(_payload_from_db(
                              connection, run_id, "adjudication_packet"
                          )["packet_contract_sha256"]),
                          "adjudicator": adjudicator, "adjudicated_at": adjudicated_at,
                          "row_count": TOTAL_COUNT, "ordered_ledger_sha256": ordered_sha,
                          "items": ledger}
        raw = _json_bytes(locked_payload); digest = hashlib.sha256(raw).hexdigest()
        completed_sha, locked_at = sha256_file(completed), utc_now()
        if _quota_time(adjudicated_at, require_fresh=False) > _quota_time(
            locked_at, require_fresh=False
        ):
            raise IdentityInstrumentV2Error("adjudicated_at cannot follow locked_at")
        _register_path(connection, root, run_id, "adjudication_completed", completed,
                       "lock_adjudication")
        _bind_values(connection, root, run_id, "adjudication_completed", completed,
                     "lock_adjudication", completed.read_bytes())
        _register_path(connection, root, run_id, "adjudication_locked", locked,
                       "lock_adjudication")
        _bind_values(connection, root, run_id, "adjudication_locked", locked,
                     "lock_adjudication", raw)
        locked_payload_json = canonical_json(locked_payload)
        lock_payload_row = {"run_id": run_id, "payload_json": locked_payload_json}
        connection.execute("INSERT INTO adjudication_lock_payload VALUES(?,?,?)",
                           (*lock_payload_row.values(), _record(lock_payload_row)))
        lock_payload = {"run_id": run_id, "adjudicator": adjudicator,
                        "row_count": TOTAL_COUNT,
                        "completed_form_path": _path_text(root, completed),
                        "completed_form_sha256": completed_sha,
                        "locked_output_path": _path_text(root, locked),
                        "locked_output_sha256": digest,
                        "ordered_ledger_sha256": ordered_sha, "locked_at": locked_at}
        _require_at_or_after_criteria(connection, run_id, locked_at, "locked_at")
        connection.execute("INSERT INTO adjudication_lock VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (*lock_payload.values(), _record(lock_payload)))
        connection.execute(
            "UPDATE runs SET status='adjudication_locked',adjudication_status='locked' WHERE run_id=?",
            (run_id,))
        _refresh_run_record(connection, run_id)
        _insert_event(connection, run_id, "reviews_locked", "adjudication_locked",
                      {"rows": TOTAL_COUNT, "ledger_sha256": ordered_sha,
                       "gold_revealed": False, "wp26_v1_labels_revealed": False})
        connection.commit()
        _atomic_bytes(locked, raw)
        return locked
    except Exception:
        connection.rollback(); raise
    finally:
        connection.close()


def _score_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    actual = {
        "d_same": sum(row["group_code"] == "D" and row["v2_derived_label"] == "same" for row in rows),
        "p1_non_same": sum(row["group_code"] == "P1" and row["v2_derived_label"] != "same" for row in rows),
        "gold_insufficient_to_insufficient": sum(
            row["n100_gold_label"] == "insufficient"
            and row["v2_derived_label"] == "insufficient" for row in rows
        ),
        "gold_related_to_related": sum(
            row["n100_gold_label"] == "related"
            and row["v2_derived_label"] == "related" for row in rows
        ),
    }
    denominators = {"d_same": 15, "p1_non_same": 3,
                    "gold_insufficient_to_insufficient": 7,
                    "gold_related_to_related": 2}
    thresholds = {"d_same": 13, "p1_non_same": 2,
                  "gold_insufficient_to_insufficient": 4,
                  "gold_related_to_related": 1}
    predicates = {code: {"actual": int(actual[code]), "denominator": denominators[code],
                         "threshold": thresholds[code],
                         "passed": int(actual[code]) >= thresholds[code]}
                  for code in thresholds}
    return {"counts": {key: int(value) for key, value in actual.items()},
            "predicates": predicates,
            "status": "pass" if all(item["passed"] for item in predicates.values()) else "fail"}


def _quota_report(connection: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    return [{key: row[key] for key in (
        "observation_no", "phase", "account_index", "observed_at", "source",
        "remaining_percent", "start_allowed", "stop_required", "limitation")}
        for row in connection.execute(
            "SELECT * FROM quota_observations WHERE run_id=? ORDER BY observation_no", (run_id,))]


def _contrast_report_rows(connection: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    result = []
    for reveal in connection.execute(
            "SELECT * FROM control_reveals WHERE run_id=? ORDER BY sample_id", (run_id,)):
        final = connection.execute(
            "SELECT * FROM adjudicated_observations WHERE run_id=? AND sample_id=?",
            (run_id, reveal["sample_id"])).fetchone()
        observations = {key: str(final[key]) for key in OBSERVATION_KEYS}
        result.append({
            "sample_id": str(reveal["sample_id"]), "candidate_id": str(reveal["candidate_id"]),
            "group": str(reveal["group_code"]), "n100_sample_id": str(reveal["n100_sample_id"]),
            "n100_gold_label": str(reveal["n100_gold_label"]),
            "v1_probe_label": str(reveal["v1_probe_label"]),
            "v2_derived_label": str(reveal["v2_derived_label"]),
            "derivation_rule_no": int(reveal["derivation_rule_no"]),
            "observations": observations,
        })
    return result


def _axis_adjustment_audit(connection: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    result = []
    for final in connection.execute(
        "SELECT * FROM adjudicated_observations WHERE run_id=? ORDER BY sample_id", (run_id,)
    ):
        reviews = {str(row["lane"]): row for row in connection.execute(
            "SELECT * FROM review_observations WHERE run_id=? AND sample_id=? ORDER BY lane",
            (run_id, final["sample_id"]))}
        observations = {key: str(final[key]) for key in OBSERVATION_KEYS}
        label, rule = derive_label(observations)
        result.append({
            "sample_id": str(final["sample_id"]),
            "r1": {key: str(reviews["r1"][key]) for key in OBSERVATION_KEYS},
            "r2": {key: str(reviews["r2"][key]) for key in OBSERVATION_KEYS},
            "final": observations,
            "adjustments": json.loads(str(final["axis_adjustments_json"])),
            "derivation_replay": {"label": label, "rule_no": rule,
                                  "matches_persisted": label == str(final["derived_label"])
                                  and rule == int(final["derivation_rule"])},
        })
    return result


def _report_payload(connection: sqlite3.Connection, run_id: str, status: str,
                    completed_at: str) -> dict[str, Any]:
    run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    scores = {str(row["predicate"]): {"actual": int(row["actual"]),
              "denominator": int(row["denominator"]), "threshold": int(row["threshold"]),
              "passed": bool(row["passed"])} for row in connection.execute(
                  "SELECT * FROM gate_scores WHERE run_id=? ORDER BY predicate", (run_id,))}
    counts = {key: int(value["actual"]) for key, value in scores.items()}
    disagreements = {key: 0 for key in OBSERVATION_KEYS}
    for row in connection.execute(
            "SELECT axis_adjustments_json FROM adjudicated_observations WHERE run_id=?", (run_id,)):
        for key, value in json.loads(str(row[0])).items():
            disagreements[key] += int(bool(value["reviewers_disagreed"]))
    status_counts = dict(sorted(Counter(str(row[0]) for row in connection.execute(
        "SELECT status FROM source_fetch_assets WHERE run_id=?", (run_id,))).items()))
    correction = connection.execute(
        "SELECT path,size_bytes,sha256 FROM input_pins "
        "WHERE run_id=? AND role='wp26_fetch_correction'", (run_id,)
    ).fetchone()
    correction_binding = {
        "path": str(correction["path"]), "size_bytes": int(correction["size_bytes"]),
        "sha256": str(correction["sha256"]),
        "correction_version": "vnext-identity-instrument-probe-fetch-cap-correction-v1",
        "corrected_projection_sha256":
            "439426d0706c7eac1b922af9fb8f042fb7bf3cb514cda809363daef7c0fc72b3",
        "actual_counts": {"asset_occurrences": 93, "logical_http_fetches": 17,
                          "wire_http_requests": 17, "redirects": 0, "retries": 0},
        "hard_caps": {"asset_occurrences": 96, "logical_http_fetches": 96,
                      "wire_http_requests": 96, "redirects": 0, "retries": 0},
    }
    return {
        "status": status, "run_id": run_id, "completed_at": completed_at,
        "measurement_only": True, "authorizes_tiering": False,
        "authorizes_wp28": False, "instrument_version": INSTRUMENT_VERSION,
        "source_contract": {"run_id": SOURCE_RUN_ID,
                            "sample_version": SOURCE_SAMPLE_VERSION, "seed": SOURCE_SEED,
                            "logical_sha256": SOURCE_LOGICAL_SHA256,
                            "packet_binding_sha256": SOURCE_PACKET_BINDING_SHA256,
                            "visual_inventory_sha256": SOURCE_VISUAL_INVENTORY_SHA256},
        "gate": {"counts": counts, "predicates": scores, "status": status},
        "three_way_comparison": _contrast_report_rows(connection, run_id),
        "axis_adjustment_audit": _axis_adjustment_audit(connection, run_id),
        "axis_adjustment_summary": {"reviewer_disagreements": disagreements,
                                    "rows": TOTAL_COUNT},
        "review": {"r1_rows": TOTAL_COUNT, "r2_rows": TOTAL_COUNT,
                   "adjudicated_rows": TOTAL_COUNT, "actors_distinct": True},
        "fetch": {"asset_occurrences": int(run["asset_occurrence_count"]),
                  "unique_assets": int(run["unique_asset_count"]),
                   "status_counts": status_counts,
                   "source_generation_network": {
                       "logical_http_fetches": int(run["inherited_logical_http_fetches"]),
                       "wire_http_requests": int(run["inherited_wire_http_requests"]),
                       "redirects": 0, "retries": 0},
                   "inherited_correction_overlay": correction_binding,
                   "incremental_network": {"logical_http_fetches": 0,
                                           "wire_http_requests": 0,
                                           "incremental_http_requests": 0,
                                           "redirects": 0, "retries": 0},
                   "cache_writes": 0, "renderer_substitutions": 0,
                   "hard_caps": {"asset_occurrences": 96, "logical_http_fetches": 96,
                                 "wire_http_requests": 96, "redirects": 0,
                                 "retries": 0}},
        "quota_observations": _quota_report(connection, run_id),
        "vision_requests": 0, "llm_requests": 0, "paid_api_requests": 0,
        "merge_executions": 0, "vnext_id_issuances": 0, "tier_promotions": 0,
        "neon_writes": 0, "r2_writes": 0,
    }


def _manifest_payload(connection: sqlite3.Connection, root: Path, run_id: str, status: str,
                      completed_at: str, report_path: Path, report_sha: str) -> dict[str, Any]:
    pins = {str(row["role"]): {key: row[key] for key in ("path", "size_bytes", "sha256", "usage")}
            for row in connection.execute(
                "SELECT * FROM input_pins WHERE run_id=? ORDER BY role", (run_id,))}
    artifacts = {str(row["role"]): {key: row[key] for key in (
        "path", "size_bytes", "sha256", "stage")}
        for row in connection.execute(
            "SELECT * FROM artifact_bindings WHERE run_id=? AND role<>'final_manifest' ORDER BY role",
            (run_id,))}
    run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    statuses = dict(sorted(Counter(str(row[0]) for row in connection.execute(
        "SELECT status FROM source_fetch_assets WHERE run_id=?", (run_id,))).items()))
    correction_pin = pins["wp26_fetch_correction"]
    return {
        "status": status, "run_id": run_id, "completed_at": completed_at,
        "versions": {"runner": RUNNER_VERSION, "instrument": INSTRUMENT_VERSION,
                     "sample": SAMPLE_VERSION, "review": REVIEW_VERSION,
                     "adjudication": ADJUDICATION_VERSION, "gate": GATE_VERSION},
        "source_contract": {"run_id": SOURCE_RUN_ID,
                            "sample_version": SOURCE_SAMPLE_VERSION, "seed": SOURCE_SEED,
                            "logical_sha256": SOURCE_LOGICAL_SHA256,
                            "packet_binding_sha256": SOURCE_PACKET_BINDING_SHA256,
                            "visual_inventory_sha256": SOURCE_VISUAL_INVENTORY_SHA256},
        "report": {"path": _path_text(root, report_path), "sha256": report_sha},
        "input_pins": pins, "artifacts": artifacts,
        "counts": {"sample": TOTAL_COUNT, "sheets": SOURCE_SHEET_COUNT,
                    "asset_occurrences": SOURCE_ASSET_OCCURRENCES,
                   "r1": TOTAL_COUNT, "r2": TOTAL_COUNT, "adjudicated": TOTAL_COUNT,
                    "post_freeze_replacements": 0},
        "inherited_fetch": {
            "asset_occurrences": SOURCE_ASSET_OCCURRENCES,
            "unique_assets": int(run["unique_asset_count"]), "status_counts": statuses,
            "logical_http_fetches": int(run["inherited_logical_http_fetches"]),
            "wire_http_requests": int(run["inherited_wire_http_requests"]),
            "redirects": 0, "retries": 0,
            "source_correction": {
                "path": correction_pin["path"], "size_bytes": correction_pin["size_bytes"],
                "sha256": correction_pin["sha256"],
                "corrected_projection_sha256":
                    "439426d0706c7eac1b922af9fb8f042fb7bf3cb514cda809363daef7c0fc72b3",
                "native_hard_caps": {"asset_occurrences": 96,
                                     "logical_http_fetches": 96,
                                     "wire_http_requests": 96,
                                     "redirects": 0, "retries": 0},
            },
        },
        "network": {"incremental_http_requests": 0, "logical_http_fetches": 0,
                    "wire_http_requests": 0, "redirects": 0, "retries": 0},
        "cache_writes": 0, "renderer_substitutions": 0,
        "vision_requests": 0, "llm_requests": 0,
        "paid_api_requests": 0, "merge_executions": 0, "vnext_id_issuances": 0,
        "tier_promotions": 0, "neon_writes": 0, "r2_writes": 0,
    }


def reveal_score(*, repo_root: Path, artifact_path: Path = DEFAULT_OUTPUT,
                 report_path: Path = DEFAULT_REPORT,
                 manifest_path: Path = DEFAULT_MANIFEST) -> RevealResult:
    """Reveal WP2.6 labels only after all three axis ledgers are immutable."""
    root = repo_root.resolve(); artifact = _path(root, artifact_path)
    report, manifest = _path(root, report_path), _path(root, manifest_path)
    if report.resolve() == manifest.resolve():
        raise IdentityInstrumentV2Error("report and manifest paths must differ")
    connection = _connect(artifact); source: sqlite3.Connection | None = None
    try:
        run = _one_run(connection); run_id, run_status = str(run["run_id"]), str(run["status"])
        terminal = connection.execute("SELECT * FROM terminal WHERE run_id=?", (run_id,)).fetchone()
        if terminal is not None:
            if run_status not in {"pass", "fail"}:
                raise IdentityInstrumentV2Error("terminal/run status mismatch")
            if (_resolve_text(root, str(terminal["report_path"])).resolve() != report.resolve()
                    or _resolve_text(root, str(terminal["manifest_path"])).resolve() != manifest.resolve()):
                raise IdentityInstrumentV2Error("terminal recovery path changed")
            report_payload = json.loads(str(terminal["report_json"]))
            manifest_payload = json.loads(str(terminal["manifest_json"]))
            report_raw, manifest_raw = _json_bytes(report_payload), _json_bytes(manifest_payload)
            if (hashlib.sha256(report_raw).hexdigest() != str(terminal["report_sha256"])
                    or hashlib.sha256(manifest_raw).hexdigest() != str(terminal["manifest_sha256"])):
                raise IdentityInstrumentV2Error("terminal recovery payload changed")
            _atomic_bytes(report, report_raw, allow_identical=True)
            _atomic_bytes(manifest, manifest_raw, allow_identical=True)
            return RevealResult(artifact, report, manifest, run_id, run_status.upper())
        if run_status != "adjudication_locked":
            raise IdentityInstrumentV2Error("reveal requires adjudication_locked")
        _require_quota(connection, run_id, "pre_reveal_score")
        if report.exists() or manifest.exists():
            raise IdentityInstrumentV2Error("refusing to clobber terminal report/manifest")
        _verify_pins(connection, root, run_id)
        pin = connection.execute(
            "SELECT path FROM input_pins WHERE run_id=? AND role='wp26_source_db'", (run_id,)
        ).fetchone()
        source = _open_ro(_resolve_text(root, str(pin[0])))
        reveals = {str(row["sample_id"]): row for row in source.execute(
            "SELECT * FROM probe_reveals ORDER BY sample_id")}
        samples = connection.execute(
            "SELECT * FROM source_samples WHERE run_id=? ORDER BY sample_id", (run_id,)
        ).fetchall()
        locked_event = connection.execute(
            "SELECT occurred_at FROM state_events WHERE run_id=? "
            "AND to_status='adjudication_locked'", (run_id,)
        ).fetchone()
        if locked_event is None:
            raise IdentityInstrumentV2Error("adjudication_locked event is missing")
        if len(reveals) != TOTAL_COUNT or len(samples) != TOTAL_COUNT:
            raise IdentityInstrumentV2Error("source reveal cardinality mismatch")
        contrast: list[dict[str, Any]] = []
        for sample in samples:
            sample_id = str(sample["sample_id"]); reveal = reveals.get(sample_id)
            final = connection.execute(
                "SELECT * FROM adjudicated_observations WHERE run_id=? AND sample_id=?",
                (run_id, sample_id)).fetchone()
            if reveal is None or final is None:
                raise IdentityInstrumentV2Error("source reveal/final observation missing")
            if str(reveal["commitment_sha256"]) != str(sample["control_commitment_sha256"]):
                raise IdentityInstrumentV2Error("control commitment mismatch at reveal")
            observation = {key: str(final[key]) for key in OBSERVATION_KEYS}
            label, rule = derive_label(observation)
            if label != str(final["derived_label"]) or rule != int(final["derivation_rule"]):
                raise IdentityInstrumentV2Error("stored v2 derivation mismatch")
            revealed_at = utc_now()
            if _quota_time(revealed_at, require_fresh=False) < _quota_time(
                locked_event[0], require_fresh=False
            ):
                raise IdentityInstrumentV2Error("control reveal predates adjudication lock")
            semantic_reveal = {
                "sample_id": sample_id, "candidate_id": str(reveal["candidate_id"]),
                "group_code": str(reveal["group_code"]),
                "n100_sample_id": str(reveal["n100_sample_id"]),
                "n100_gold_label": str(reveal["n100_gold_label"]),
                "v1_probe_label": str(reveal["final_probe_label"]),
                "v2_derived_label": label, "derivation_rule_no": rule,
                "control_commitment_sha256": str(reveal["commitment_sha256"]),
                "revealed_at": revealed_at,
            }
            payload = {
                "run_id": run_id, "sample_id": sample_id,
                "candidate_id": semantic_reveal["candidate_id"],
                "group_code": semantic_reveal["group_code"],
                "n100_sample_id": semantic_reveal["n100_sample_id"],
                "n100_gold_label": semantic_reveal["n100_gold_label"],
                "v1_probe_label": semantic_reveal["v1_probe_label"],
                "v2_derived_label": label, "derivation_rule_no": rule,
                "commitment_sha256": semantic_reveal["control_commitment_sha256"],
                "revealed_at": revealed_at,
                "reveal_payload_sha256": canonical_sha256(semantic_reveal),
            }
            connection.execute(
                "INSERT INTO control_reveals VALUES(" + ",".join("?" for _ in range(13)) + ")",
                (*payload.values(), _record(payload)),
            )
            contrast.append(payload)
        gold_counts = Counter(row["n100_gold_label"] for row in contrast)
        group_counts = Counter(row["group_code"] for row in contrast)
        if gold_counts != Counter({"same": 15, "insufficient": 7, "related": 2}):
            raise IdentityInstrumentV2Error("revealed N100 gold composition changed")
        if group_counts != Counter({"D": 15, "P2": 4, "P1": 3, "P3": 2}):
            raise IdentityInstrumentV2Error("revealed control group composition changed")
        gate = _score_rows(contrast); status = str(gate["status"])
        for code, item in gate["predicates"].items():
            payload = {"run_id": run_id, "predicate": code, "actual": item["actual"],
                       "denominator": item["denominator"], "threshold": item["threshold"],
                       "passed": int(item["passed"])}
            connection.execute("INSERT INTO gate_scores VALUES(?,?,?,?,?,?,?)",
                               (*payload.values(), _record(payload)))
        completed = utc_now()
        report_payload = _report_payload(connection, run_id, status, completed)
        report_raw = _json_bytes(report_payload); report_sha = hashlib.sha256(report_raw).hexdigest()
        _register_path(connection, root, run_id, "final_report", report, "reveal_score")
        _bind_values(connection, root, run_id, "final_report", report, "reveal_score", report_raw)
        manifest_payload = _manifest_payload(
            connection, root, run_id, status, completed, report, report_sha
        )
        manifest_raw = _json_bytes(manifest_payload); manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
        _register_path(connection, root, run_id, "final_manifest", manifest, "reveal_score")
        _bind_values(connection, root, run_id, "final_manifest", manifest, "reveal_score", manifest_raw)
        terminal_payload = {"run_id": run_id, "status": status,
                            "report_path": _path_text(root, report),
                            "report_sha256": report_sha, "manifest_path": _path_text(root, manifest),
                            "manifest_sha256": manifest_sha, "report_json": canonical_json(report_payload),
                            "manifest_json": canonical_json(manifest_payload), "completed_at": completed}
        connection.execute(
            "INSERT INTO terminal VALUES(?,?,?,?,?,?,?,?,?,?)",
            (*terminal_payload.values(), _record(terminal_payload)),
        )
        connection.execute("UPDATE runs SET status=?,completed_at=? WHERE run_id=?",
                           (status.casefold(), completed, run_id))
        _refresh_run_record(connection, run_id)
        _insert_event(connection, run_id, "adjudication_locked", status.casefold(),
                      {"gold_revealed": True, "wp26_v1_labels_revealed": True,
                       "gate": gate})
        connection.commit()
        _atomic_bytes(report, report_raw)
        _atomic_bytes(manifest, manifest_raw)
        return RevealResult(artifact, report, manifest, run_id, status)
    except Exception:
        connection.rollback(); raise
    finally:
        if source is not None:
            source.close()
        connection.close()
