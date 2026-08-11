"""Immutable runtime sidecar for the fixed cross-source semantic Vision N10.

The source manifest, E2, and E3 artifacts are inputs only.  Verified Vision
derivatives are atomically materialized in a bounded run-local spool so a
crash never forces an already accepted input to consume another fetch attempt;
unless explicitly retained for blind review, the spool is removed after the
terminal DB and report are published and verified.  Image bytes are never
stored in SQLite.  Every HTTP and model attempt is committed before a retry.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import sqlite3
import struct
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

from canonical.cross_source_image_selection import canonical_json, canonical_sha256
from canonical.cross_source_semantic_coverage import (
    DEFAULT_SAMPLE_SEED,
    SEMANTIC_COVERAGE_MANIFEST_DOMAIN,
)
from canonical.cross_source_semantic_coverage_sources import ArtifactSpec
from canonical.cross_source_semantic_coverage_validator import (
    validate_semantic_coverage_manifest,
)
from canonical.cross_source_semantic_vision import (
    CONTRACT_VERSION,
    OUTPUT_SCHEMA,
    PROMPT_VERSION,
    TRANSFORM_VERSION,
    compose_prompt,
    derive_coverage_slots,
    derive_hero_decision,
    normalize_batch,
)
from canonical.cross_source_semantic_fetch import (
    FETCH_CONTRACT_VERSION,
    SOURCE_HOSTS,
    FetchFailure,
    FetchPayload as HttpPayload,
    fetch_once as network_fetch,
)
from canonical.divisare_vision_benchmark import decode_source, prepare_derivative
from canonical.divisare_vision_runtime import (
    CLI_IMAGE_DETAIL,
    DEFAULT_MODEL,
    DEFAULT_REASONING,
    DEFAULT_SERVICE_TIER,
    RUNTIME_VERSION,
    VisionRuntimeResult,
    run_codex_vision_batch,
)
from canonical.image_fingerprint import (
    DEFAULT_MAX_INPUT_BYTES,
    FINGERPRINT_CONTRACT_VERSION,
    dependency_versions,
    fingerprint_bytes,
)


APPLICATION_ID = 0x53564E31
SCHEMA_VERSION = 1
RUNNER_VERSION = "cross-source-semantic-vision-n10-runner-v1.1.0"
RETRY_POLICY_VERSION = "cross-source-semantic-vision-n10-retry-v1.1.0"
LOGICAL_MANIFEST_VERSION = "cross-source-semantic-vision-logical-v1"
FIXED_BUILDING_COUNT = 10
FIXED_OCCURRENCE_COUNT = 57
FIXED_BATCH_SIZE = 5
FIXED_FETCH_ATTEMPTS = 3
FIXED_VISION_ATTEMPTS = 2
DEFAULT_REQUESTS_PER_SECOND = 2.0
MAX_STDOUT_CAPTURE_BYTES = 8 * 1024 * 1024
MAX_STDERR_CAPTURE_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = DEFAULT_MAX_INPUT_BYTES
FROZEN_MANIFEST_BYTE_SHA256 = (
    "81fa13340e584e6d874ab7145a9d003ec57093db5a4dbe41f206c6e7ac85ce1f"
)
FROZEN_MANIFEST_SELF_SHA256 = (
    "bf5ac74479ac305e11dc5aa17f17d02102a7eb2499d15680384d21848801ab5b"
)
ALLOWED_HOSTS = dict(SOURCE_HOSTS)
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
LEGACY_REQUIRED_VALIDATIONS = (
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
REQUIRED_VALIDATIONS = LEGACY_REQUIRED_VALIDATIONS + (
    "materialization_cache_integrity",
    "report_binding",
)

_REPORT_LOGICAL_PLACEHOLDER = "0" * 64
_SENSITIVE_CAPTURE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s\"'\\,;]+"),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
            r"password|cookie)[\"']?\s*[:=]\s*[\"']?)([^\"'\s,;}]+)"
        ),
        r"\1[REDACTED]",
    ),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "[REDACTED_OPENAI_KEY]"),
    (
        re.compile(
            r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?"
            r"-----END [^-\r\n]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        "[REDACTED_PRIVATE_KEY]",
    ),
)


class SemanticVisionError(RuntimeError):
    """A fail-closed provenance, fetch, inference, or publication error."""


@dataclass(frozen=True)
class RunResult:
    output_db: Path
    report_path: Path
    status: str
    logical_sha256: str
    requests_made: int
    vision_requests: int
    resumed: bool
    already_complete: bool
    metrics: Mapping[str, Any]


@dataclass
class _GlobalRequestRateLimiter:
    """Process-local limiter shared by every source and fetch attempt."""

    requests_per_second: float
    clock: Callable[[], float]
    sleeper: Callable[[float], None]
    _next_allowed: float | None = None

    def wait(self) -> None:
        if self.requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        interval = 1.0 / self.requests_per_second
        now = self.clock()
        if self._next_allowed is not None and now < self._next_allowed:
            self.sleeper(self._next_allowed - now)
            now = self.clock()
        self._next_allowed = max(now, self._next_allowed or now) + interval


SIDECAR_SCHEMA = f"""
PRAGMA foreign_keys=ON;
PRAGMA application_id={APPLICATION_ID};
PRAGMA user_version={SCHEMA_VERSION};

CREATE TABLE semantic_runs(
  run_id TEXT PRIMARY KEY CHECK(length(trim(run_id))>0),
  status TEXT NOT NULL CHECK(status IN
    ('initializing','running','complete','complete_with_failures','failed_validation','failed')),
  runner_version TEXT NOT NULL,
  schema_version INTEGER NOT NULL,
  manifest_path TEXT NOT NULL,
  manifest_size INTEGER NOT NULL,
  manifest_byte_sha256 TEXT NOT NULL CHECK(length(manifest_byte_sha256)=64),
  manifest_self_sha256 TEXT NOT NULL CHECK(length(manifest_self_sha256)=64),
  ordered_building_manifest_sha256 TEXT NOT NULL CHECK(length(ordered_building_manifest_sha256)=64),
  ordered_occurrence_manifest_sha256 TEXT NOT NULL CHECK(length(ordered_occurrence_manifest_sha256)=64),
  sample_seed TEXT NOT NULL,
  building_count INTEGER NOT NULL CHECK(building_count={FIXED_BUILDING_COUNT}),
  occurrence_count INTEGER NOT NULL CHECK(occurrence_count={FIXED_OCCURRENCE_COUNT}),
  e2_path TEXT NOT NULL,
  e2_size INTEGER NOT NULL,
  e2_sha256_before TEXT NOT NULL CHECK(length(e2_sha256_before)=64),
  e2_sha256_after TEXT CHECK(e2_sha256_after IS NULL OR length(e2_sha256_after)=64),
  e2_logical_sha256 TEXT NOT NULL CHECK(length(e2_logical_sha256)=64),
  e2_run_id TEXT NOT NULL,
  e3_path TEXT NOT NULL,
  e3_size INTEGER NOT NULL,
  e3_sha256_before TEXT NOT NULL CHECK(length(e3_sha256_before)=64),
  e3_sha256_after TEXT CHECK(e3_sha256_after IS NULL OR length(e3_sha256_after)=64),
  e3_logical_sha256 TEXT NOT NULL CHECK(length(e3_logical_sha256)=64),
  e3_run_id TEXT NOT NULL,
  contract_version TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  output_schema_sha256 TEXT NOT NULL CHECK(length(output_schema_sha256)=64),
  transform_version TEXT NOT NULL,
  e1_contract_version TEXT NOT NULL,
  dependency_manifest_json TEXT NOT NULL CHECK(json_valid(dependency_manifest_json)),
  dependency_manifest_sha256 TEXT NOT NULL CHECK(length(dependency_manifest_sha256)=64),
  runtime_version TEXT NOT NULL,
  retry_policy_version TEXT NOT NULL,
  batch_size INTEGER NOT NULL CHECK(batch_size={FIXED_BATCH_SIZE}),
  max_fetch_attempts INTEGER NOT NULL CHECK(max_fetch_attempts={FIXED_FETCH_ATTEMPTS}),
  max_vision_attempts INTEGER NOT NULL CHECK(max_vision_attempts={FIXED_VISION_ATTEMPTS}),
  model TEXT NOT NULL,
  reasoning TEXT NOT NULL,
  service_tier TEXT NOT NULL,
  image_detail TEXT NOT NULL,
  cli_version TEXT,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  metrics_json TEXT CHECK(metrics_json IS NULL OR json_valid(metrics_json)),
  logical_sha256 TEXT CHECK(logical_sha256 IS NULL OR length(logical_sha256)=64),
  error TEXT,
  CHECK((status IN ('initializing','running') AND completed_at IS NULL
         AND e2_sha256_after IS NULL AND e3_sha256_after IS NULL)
     OR (status NOT IN ('initializing','running') AND completed_at IS NOT NULL
         AND e2_sha256_after=e2_sha256_before AND e3_sha256_after=e3_sha256_before))
);

CREATE TRIGGER semantic_runs_single BEFORE INSERT ON semantic_runs
WHEN EXISTS(SELECT 1 FROM semantic_runs) BEGIN
  SELECT RAISE(ABORT,'one semantic run per sidecar'); END;
CREATE TRIGGER semantic_runs_provenance_immutable BEFORE UPDATE OF
  run_id,runner_version,schema_version,manifest_path,manifest_size,
  manifest_byte_sha256,manifest_self_sha256,ordered_building_manifest_sha256,
  ordered_occurrence_manifest_sha256,sample_seed,building_count,occurrence_count,
  e2_path,e2_size,e2_sha256_before,e2_logical_sha256,e2_run_id,
  e3_path,e3_size,e3_sha256_before,e3_logical_sha256,e3_run_id,
  contract_version,prompt_version,output_schema_sha256,transform_version,
  e1_contract_version,dependency_manifest_json,dependency_manifest_sha256,
  runtime_version,retry_policy_version,batch_size,max_fetch_attempts,
  max_vision_attempts,model,reasoning,service_tier,image_detail,cli_version,started_at
ON semantic_runs BEGIN SELECT RAISE(ABORT,'run provenance is immutable'); END;
CREATE TRIGGER semantic_runs_status_transition BEFORE UPDATE OF status ON semantic_runs
WHEN NOT ((OLD.status='initializing' AND NEW.status='running') OR
          (OLD.status='running' AND NEW.status IN
           ('complete','complete_with_failures','failed_validation','failed')))
BEGIN SELECT RAISE(ABORT,'invalid semantic run status transition'); END;
CREATE TRIGGER semantic_runs_terminal_immutable BEFORE UPDATE ON semantic_runs
WHEN OLD.status NOT IN ('initializing','running')
BEGIN SELECT RAISE(ABORT,'terminal semantic run is immutable'); END;
CREATE TRIGGER semantic_runs_no_delete BEFORE DELETE ON semantic_runs
BEGIN SELECT RAISE(ABORT,'semantic run is immutable'); END;

CREATE TABLE selected_buildings(
  run_id TEXT NOT NULL REFERENCES semantic_runs(run_id),
  building_rank INTEGER NOT NULL CHECK(building_rank>=1),
  selection_id TEXT NOT NULL,
  source TEXT NOT NULL CHECK(source IN ('architizer','divisare')),
  source_building_id TEXT NOT NULL,
  population_stratum TEXT NOT NULL,
  guard_name TEXT NOT NULL,
  qa_fallback INTEGER NOT NULL CHECK(qa_fallback IN (0,1)),
  building_record_sha256 TEXT NOT NULL CHECK(length(building_record_sha256)=64),
  coverage_plan_record_sha256 TEXT NOT NULL CHECK(length(coverage_plan_record_sha256)=64),
  selected_building_record_sha256 TEXT NOT NULL CHECK(length(selected_building_record_sha256)=64),
  manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json)),
  PRIMARY KEY(run_id,selection_id), UNIQUE(run_id,building_rank)
);

CREATE TABLE selected_occurrences(
  run_id TEXT NOT NULL,
  input_rank INTEGER NOT NULL CHECK(input_rank>=1),
  inference_id TEXT NOT NULL,
  selection_id TEXT NOT NULL,
  occurrence_rank INTEGER NOT NULL CHECK(occurrence_rank>=1),
  source TEXT NOT NULL CHECK(source IN ('architizer','divisare')),
  source_building_id TEXT NOT NULL,
  source_asset_id TEXT NOT NULL,
  candidate_id TEXT NOT NULL,
  fetch_url TEXT NOT NULL,
  expected_response_sha256 TEXT NOT NULL CHECK(length(expected_response_sha256)=64),
  expected_e1_pixel_sha256 TEXT NOT NULL CHECK(length(expected_e1_pixel_sha256)=64),
  expected_e1_width INTEGER NOT NULL CHECK(expected_e1_width>0),
  expected_e1_height INTEGER NOT NULL CHECK(expected_e1_height>0),
  e2_asset_record_sha256 TEXT NOT NULL CHECK(length(e2_asset_record_sha256)=64),
  e2_relation_record_sha256 TEXT NOT NULL CHECK(length(e2_relation_record_sha256)=64),
  e3_candidate_record_sha256 TEXT NOT NULL CHECK(length(e3_candidate_record_sha256)=64),
  e3_ranking_record_sha256 TEXT NOT NULL CHECK(length(e3_ranking_record_sha256)=64),
  e3_shortlist_record_sha256 TEXT CHECK(e3_shortlist_record_sha256 IS NULL OR length(e3_shortlist_record_sha256)=64),
  occurrence_record_sha256 TEXT NOT NULL CHECK(length(occurrence_record_sha256)=64),
  manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json)),
  PRIMARY KEY(run_id,inference_id),
  UNIQUE(run_id,input_rank), UNIQUE(run_id,candidate_id),
  UNIQUE(run_id,selection_id,occurrence_rank),
  FOREIGN KEY(run_id,selection_id) REFERENCES selected_buildings(run_id,selection_id)
);

CREATE TABLE vision_inputs(
  run_id TEXT NOT NULL,
  inference_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN
    ('pending','ready','success','fetch_failed','vision_failed')),
  selected_fetch_attempt_no INTEGER,
  actual_response_sha256 TEXT CHECK(actual_response_sha256 IS NULL OR length(actual_response_sha256)=64),
  actual_e1_pixel_sha256 TEXT CHECK(actual_e1_pixel_sha256 IS NULL OR length(actual_e1_pixel_sha256)=64),
  derivative_encoded_sha256 TEXT CHECK(derivative_encoded_sha256 IS NULL OR length(derivative_encoded_sha256)=64),
  derivative_pixel_sha256 TEXT CHECK(derivative_pixel_sha256 IS NULL OR length(derivative_pixel_sha256)=64),
  derivative_width INTEGER CHECK(derivative_width IS NULL OR derivative_width>0),
  derivative_height INTEGER CHECK(derivative_height IS NULL OR derivative_height>0),
  derivative_bytes INTEGER CHECK(derivative_bytes IS NULL OR derivative_bytes>0),
  completed_at TEXT,
  error_kind TEXT,
  error_message TEXT,
  PRIMARY KEY(run_id,inference_id),
  FOREIGN KEY(run_id,inference_id) REFERENCES selected_occurrences(run_id,inference_id),
  FOREIGN KEY(run_id,inference_id,selected_fetch_attempt_no)
    REFERENCES fetch_attempts(run_id,inference_id,attempt_no),
  CHECK((status='pending' AND selected_fetch_attempt_no IS NULL AND completed_at IS NULL)
     OR (status='ready' AND selected_fetch_attempt_no IS NOT NULL
         AND derivative_encoded_sha256 IS NOT NULL AND completed_at IS NULL)
     OR (status='success' AND selected_fetch_attempt_no IS NOT NULL
         AND derivative_encoded_sha256 IS NOT NULL AND completed_at IS NOT NULL
         AND error_kind IS NULL)
     OR (status IN ('fetch_failed','vision_failed') AND completed_at IS NOT NULL
         AND error_kind IS NOT NULL))
);

CREATE TABLE fetch_attempts(
  run_id TEXT NOT NULL,
  inference_id TEXT NOT NULL,
  attempt_no INTEGER NOT NULL CHECK(attempt_no>=1 AND attempt_no<={FIXED_FETCH_ATTEMPTS}),
  request_url TEXT NOT NULL,
  final_url TEXT,
  started_at TEXT NOT NULL,
  completed_at TEXT NOT NULL,
  elapsed_ms INTEGER NOT NULL CHECK(elapsed_ms>=0),
  outcome TEXT NOT NULL CHECK(outcome IN
    ('exact_match','delivery_changed_pixel_stable','source_changed','http_failed',
     'invalid_content','decode_failed','oversize')),
  http_status INTEGER CHECK(http_status IS NULL OR http_status BETWEEN 100 AND 599),
  content_type TEXT,
  response_bytes INTEGER CHECK(response_bytes IS NULL OR response_bytes>=0),
  expected_response_sha256 TEXT NOT NULL CHECK(length(expected_response_sha256)=64),
  actual_response_sha256 TEXT CHECK(actual_response_sha256 IS NULL OR length(actual_response_sha256)=64),
  expected_e1_pixel_sha256 TEXT NOT NULL CHECK(length(expected_e1_pixel_sha256)=64),
  actual_e1_pixel_sha256 TEXT CHECK(actual_e1_pixel_sha256 IS NULL OR length(actual_e1_pixel_sha256)=64),
  retryable INTEGER NOT NULL CHECK(retryable IN (0,1)),
  retry_after_seconds REAL CHECK(retry_after_seconds IS NULL OR retry_after_seconds>=0),
  scheduled_delay_seconds REAL CHECK(scheduled_delay_seconds IS NULL OR scheduled_delay_seconds>=0),
  error_kind TEXT,
  error_message TEXT,
  PRIMARY KEY(run_id,inference_id,attempt_no),
  FOREIGN KEY(run_id,inference_id) REFERENCES selected_occurrences(run_id,inference_id)
);

CREATE TABLE vision_attempts(
  attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES semantic_runs(run_id),
  batch_no INTEGER NOT NULL CHECK(batch_no>=1),
  attempt_no INTEGER NOT NULL CHECK(attempt_no>=1 AND attempt_no<={FIXED_VISION_ATTEMPTS}),
  inference_ids_json TEXT NOT NULL CHECK(json_valid(inference_ids_json)),
  status TEXT NOT NULL CHECK(status IN ('success','failed')),
  model TEXT NOT NULL, reasoning TEXT NOT NULL, service_tier TEXT NOT NULL,
  runtime_version TEXT NOT NULL, cli_version TEXT, codex_bin TEXT NOT NULL,
  image_detail TEXT NOT NULL, sandbox TEXT NOT NULL,
  prompt_sha256 TEXT NOT NULL CHECK(length(prompt_sha256)=64),
  output_schema_sha256 TEXT NOT NULL CHECK(length(output_schema_sha256)=64),
  started_at TEXT NOT NULL, completed_at TEXT NOT NULL,
  elapsed_ms INTEGER NOT NULL CHECK(elapsed_ms>=0),
  input_tokens INTEGER CHECK(input_tokens IS NULL OR input_tokens>=0),
  cached_input_tokens INTEGER CHECK(cached_input_tokens IS NULL OR cached_input_tokens>=0),
  output_tokens INTEGER CHECK(output_tokens IS NULL OR output_tokens>=0),
  raw_events_sha256 TEXT CHECK(raw_events_sha256 IS NULL OR length(raw_events_sha256)=64),
  error_kind TEXT, error_message TEXT,
  UNIQUE(run_id,batch_no,attempt_no)
);

CREATE TABLE vision_attempt_payloads(
  attempt_id INTEGER PRIMARY KEY REFERENCES vision_attempts(attempt_id),
  codec TEXT NOT NULL CHECK(codec='gzip'),
  stdout_gzip BLOB NOT NULL,
  stdout_bytes INTEGER NOT NULL CHECK(stdout_bytes>=0),
  stdout_sha256 TEXT NOT NULL CHECK(length(stdout_sha256)=64),
  stderr_gzip BLOB NOT NULL,
  stderr_bytes INTEGER NOT NULL CHECK(stderr_bytes>=0),
  stderr_sha256 TEXT NOT NULL CHECK(length(stderr_sha256)=64),
  stderr_excerpt TEXT
);

CREATE TABLE semantic_results(
  run_id TEXT NOT NULL,
  inference_id TEXT NOT NULL,
  attempt_id INTEGER NOT NULL REFERENCES vision_attempts(attempt_id),
  raw_result_json TEXT NOT NULL CHECK(json_valid(raw_result_json)),
  normalized_result_json TEXT NOT NULL CHECK(json_valid(normalized_result_json)),
  in_scope INTEGER NOT NULL CHECK(in_scope IN (0,1)), reject_reason TEXT NOT NULL,
  medium TEXT NOT NULL, spatial_context TEXT NOT NULL, framing_scale TEXT NOT NULL,
  camera_angle TEXT NOT NULL, drawing_kind TEXT NOT NULL, project_state TEXT NOT NULL,
  project_legibility TEXT NOT NULL, uncertain_axes_json TEXT NOT NULL CHECK(json_valid(uncertain_axes_json)),
  resolution_insufficient INTEGER NOT NULL CHECK(resolution_insufficient IN (0,1)),
  evidence TEXT NOT NULL, record_sha256 TEXT NOT NULL CHECK(length(record_sha256)=64),
  PRIMARY KEY(run_id,inference_id),
  FOREIGN KEY(run_id,inference_id) REFERENCES selected_occurrences(run_id,inference_id)
);

CREATE TABLE occurrence_result_links(
  run_id TEXT NOT NULL, inference_id TEXT NOT NULL,
  result_inference_id TEXT NOT NULL, reuse_basis TEXT NOT NULL CHECK(reuse_basis='same_occurrence'),
  verified_input_pixel_sha256 TEXT NOT NULL CHECK(length(verified_input_pixel_sha256)=64),
  record_sha256 TEXT NOT NULL CHECK(length(record_sha256)=64),
  PRIMARY KEY(run_id,inference_id),
  FOREIGN KEY(run_id,inference_id) REFERENCES selected_occurrences(run_id,inference_id),
  FOREIGN KEY(run_id,result_inference_id) REFERENCES semantic_results(run_id,inference_id)
);

CREATE TABLE hero_candidate_decisions(
  run_id TEXT NOT NULL, inference_id TEXT NOT NULL,
  tier TEXT NOT NULL CHECK(tier IN ('preferred','eligible','fallback','qa_only','archive_only','rejected')),
  reasons_json TEXT NOT NULL CHECK(json_valid(reasons_json)), authoritative INTEGER NOT NULL CHECK(authoritative=0),
  record_sha256 TEXT NOT NULL CHECK(length(record_sha256)=64),
  PRIMARY KEY(run_id,inference_id),
  FOREIGN KEY(run_id,inference_id) REFERENCES semantic_results(run_id,inference_id)
);

CREATE TABLE coverage_slot_assignments(
  run_id TEXT NOT NULL, selection_id TEXT NOT NULL, slot TEXT NOT NULL,
  assignment_rank INTEGER NOT NULL CHECK(assignment_rank>=0),
  state TEXT NOT NULL CHECK(state IN ('observed','not_observed_in_sample')),
  inference_id TEXT,
  record_sha256 TEXT NOT NULL CHECK(length(record_sha256)=64),
  PRIMARY KEY(run_id,selection_id,slot,assignment_rank),
  FOREIGN KEY(run_id,selection_id) REFERENCES selected_buildings(run_id,selection_id),
  FOREIGN KEY(run_id,inference_id) REFERENCES semantic_results(run_id,inference_id),
  CHECK((state='observed' AND assignment_rank>=1 AND inference_id IS NOT NULL)
     OR (state='not_observed_in_sample' AND assignment_rank=0 AND inference_id IS NULL))
);

CREATE TABLE validations(
  run_id TEXT NOT NULL REFERENCES semantic_runs(run_id),
  validation_name TEXT NOT NULL, severity TEXT NOT NULL CHECK(severity IN ('info','warning','error')),
  passed INTEGER NOT NULL CHECK(passed IN (0,1)), expected TEXT, actual TEXT NOT NULL, detail TEXT,
  PRIMARY KEY(run_id,validation_name)
);

CREATE TRIGGER selected_buildings_insert BEFORE INSERT ON selected_buildings
WHEN (SELECT status FROM semantic_runs WHERE run_id=NEW.run_id)<>'initializing'
BEGIN SELECT RAISE(ABORT,'building initialization is closed'); END;
CREATE TRIGGER selected_buildings_no_update BEFORE UPDATE ON selected_buildings
BEGIN SELECT RAISE(ABORT,'selected building is immutable'); END;
CREATE TRIGGER selected_buildings_no_delete BEFORE DELETE ON selected_buildings
BEGIN SELECT RAISE(ABORT,'selected building is immutable'); END;
CREATE TRIGGER selected_occurrences_insert BEFORE INSERT ON selected_occurrences
WHEN (SELECT status FROM semantic_runs WHERE run_id=NEW.run_id)<>'initializing'
BEGIN SELECT RAISE(ABORT,'occurrence initialization is closed'); END;
CREATE TRIGGER selected_occurrences_no_update BEFORE UPDATE ON selected_occurrences
BEGIN SELECT RAISE(ABORT,'selected occurrence is immutable'); END;
CREATE TRIGGER selected_occurrences_no_delete BEFORE DELETE ON selected_occurrences
BEGIN SELECT RAISE(ABORT,'selected occurrence is immutable'); END;
CREATE TRIGGER vision_inputs_insert BEFORE INSERT ON vision_inputs
WHEN NEW.status<>'pending' OR (SELECT status FROM semantic_runs WHERE run_id=NEW.run_id)<>'initializing'
BEGIN SELECT RAISE(ABORT,'vision inputs initialize pending only'); END;
CREATE TRIGGER vision_inputs_update BEFORE UPDATE ON vision_inputs
WHEN (SELECT status FROM semantic_runs WHERE run_id=OLD.run_id)<>'running'
BEGIN SELECT RAISE(ABORT,'vision input update requires running run'); END;
CREATE TRIGGER vision_inputs_transition BEFORE UPDATE OF status ON vision_inputs
WHEN NOT ((OLD.status='pending' AND NEW.status IN ('ready','fetch_failed')) OR
          (OLD.status='ready' AND NEW.status IN ('ready','success','fetch_failed','vision_failed')))
BEGIN SELECT RAISE(ABORT,'invalid vision input status transition'); END;
CREATE TRIGGER vision_inputs_no_delete BEFORE DELETE ON vision_inputs
BEGIN SELECT RAISE(ABORT,'vision inputs are immutable'); END;
CREATE TRIGGER fetch_attempts_insert BEFORE INSERT ON fetch_attempts
WHEN (SELECT status FROM semantic_runs WHERE run_id=NEW.run_id)<>'running'
BEGIN SELECT RAISE(ABORT,'fetch attempt requires running run'); END;
CREATE TRIGGER fetch_attempts_no_update BEFORE UPDATE ON fetch_attempts
BEGIN SELECT RAISE(ABORT,'fetch attempts are append-only'); END;
CREATE TRIGGER fetch_attempts_no_delete BEFORE DELETE ON fetch_attempts
BEGIN SELECT RAISE(ABORT,'fetch attempts are append-only'); END;
CREATE TRIGGER vision_attempts_insert BEFORE INSERT ON vision_attempts
WHEN (SELECT status FROM semantic_runs WHERE run_id=NEW.run_id)<>'running'
BEGIN SELECT RAISE(ABORT,'vision attempt requires running run'); END;
CREATE TRIGGER vision_attempts_no_update BEFORE UPDATE ON vision_attempts
BEGIN SELECT RAISE(ABORT,'vision attempts are append-only'); END;
CREATE TRIGGER vision_attempts_no_delete BEFORE DELETE ON vision_attempts
BEGIN SELECT RAISE(ABORT,'vision attempts are append-only'); END;
CREATE TRIGGER payloads_insert BEFORE INSERT ON vision_attempt_payloads
WHEN (SELECT r.status FROM semantic_runs r JOIN vision_attempts a ON a.run_id=r.run_id
      WHERE a.attempt_id=NEW.attempt_id)<>'running'
BEGIN SELECT RAISE(ABORT,'attempt payload requires running run'); END;
CREATE TRIGGER payloads_no_update BEFORE UPDATE ON vision_attempt_payloads
BEGIN SELECT RAISE(ABORT,'attempt payloads are immutable'); END;
CREATE TRIGGER payloads_no_delete BEFORE DELETE ON vision_attempt_payloads
BEGIN SELECT RAISE(ABORT,'attempt payloads are immutable'); END;
CREATE TRIGGER semantic_results_insert BEFORE INSERT ON semantic_results
WHEN (SELECT status FROM semantic_runs WHERE run_id=NEW.run_id)<>'running'
BEGIN SELECT RAISE(ABORT,'semantic result requires running run'); END;
CREATE TRIGGER semantic_results_no_update BEFORE UPDATE ON semantic_results
BEGIN SELECT RAISE(ABORT,'semantic results are immutable'); END;
CREATE TRIGGER semantic_results_no_delete BEFORE DELETE ON semantic_results
BEGIN SELECT RAISE(ABORT,'semantic results are immutable'); END;
CREATE TRIGGER links_insert BEFORE INSERT ON occurrence_result_links
WHEN (SELECT status FROM semantic_runs WHERE run_id=NEW.run_id)<>'running'
BEGIN SELECT RAISE(ABORT,'result link requires running run'); END;
CREATE TRIGGER links_no_update BEFORE UPDATE ON occurrence_result_links
BEGIN SELECT RAISE(ABORT,'result links are immutable'); END;
CREATE TRIGGER links_no_delete BEFORE DELETE ON occurrence_result_links
BEGIN SELECT RAISE(ABORT,'result links are immutable'); END;
CREATE TRIGGER hero_insert BEFORE INSERT ON hero_candidate_decisions
WHEN (SELECT status FROM semantic_runs WHERE run_id=NEW.run_id)<>'running'
BEGIN SELECT RAISE(ABORT,'hero decision requires running run'); END;
CREATE TRIGGER hero_no_update BEFORE UPDATE ON hero_candidate_decisions
BEGIN SELECT RAISE(ABORT,'hero decisions are immutable'); END;
CREATE TRIGGER hero_no_delete BEFORE DELETE ON hero_candidate_decisions
BEGIN SELECT RAISE(ABORT,'hero decisions are immutable'); END;
CREATE TRIGGER slots_insert BEFORE INSERT ON coverage_slot_assignments
WHEN (SELECT status FROM semantic_runs WHERE run_id=NEW.run_id)<>'running'
BEGIN SELECT RAISE(ABORT,'coverage assignment requires running run'); END;
CREATE TRIGGER slots_no_update BEFORE UPDATE ON coverage_slot_assignments
BEGIN SELECT RAISE(ABORT,'coverage assignments are immutable'); END;
CREATE TRIGGER slots_no_delete BEFORE DELETE ON coverage_slot_assignments
BEGIN SELECT RAISE(ABORT,'coverage assignments are immutable'); END;
CREATE TRIGGER validations_write BEFORE INSERT ON validations
WHEN (SELECT status FROM semantic_runs WHERE run_id=NEW.run_id) NOT IN ('initializing','running')
BEGIN SELECT RAISE(ABORT,'validation write requires nonterminal run'); END;
CREATE TRIGGER validations_no_update BEFORE UPDATE ON validations
BEGIN SELECT RAISE(ABORT,'validations are immutable'); END;
CREATE TRIGGER validations_no_delete BEFORE DELETE ON validations
BEGIN SELECT RAISE(ABORT,'validations are immutable'); END;

CREATE INDEX idx_occurrence_building ON selected_occurrences(run_id,selection_id,occurrence_rank);
CREATE INDEX idx_inputs_status ON vision_inputs(run_id,status,inference_id);
CREATE INDEX idx_fetch_outcome ON fetch_attempts(run_id,outcome,inference_id);
CREATE INDEX idx_attempt_batch ON vision_attempts(run_id,batch_no,attempt_no);
CREATE INDEX idx_results_axes ON semantic_results(run_id,medium,spatial_context,framing_scale);
CREATE INDEX idx_slots ON coverage_slot_assignments(run_id,slot,state);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def file_sha256(path: Path, *, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest()


def _gzip_bytes(value: str) -> bytes:
    return gzip.compress(value.encode("utf-8"), compresslevel=9, mtime=0)


def _sanitize_capture(value: str | None, *, label: str, max_bytes: int) -> str:
    sanitized = value or ""
    for pattern, replacement in _SENSITIVE_CAPTURE_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    encoded = sanitized.encode("utf-8")
    if len(encoded) > max_bytes:
        raise SemanticVisionError(
            f"{label} capture exceeds bounded limit: {len(encoded)} > {max_bytes} bytes"
        )
    return sanitized


def _atomic_write_bytes(path: Path, raw: bytes) -> None:
    """Publish bytes without overwriting an existing path.

    A same-content existing file is accepted, which makes crash recovery
    idempotent.  A different existing file is always a fail-closed error.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    expected_sha = hashlib.sha256(raw).hexdigest()
    if path.exists():
        if path.stat().st_size != len(raw) or file_sha256(path) != expected_sha:
            raise SemanticVisionError(f"durable file mismatch: {path.name}")
        return
    descriptor, temp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp_path, path)
        except FileExistsError:
            if path.stat().st_size != len(raw) or file_sha256(path) != expected_sha:
                raise SemanticVisionError(f"durable file mismatch: {path.name}")
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _schema_sha256() -> str:
    return canonical_sha256(OUTPUT_SCHEMA)


def _sidecars(path: Path) -> tuple[Path, ...]:
    return tuple(
        candidate
        for candidate in (
            Path(str(path) + "-wal"),
            Path(str(path) + "-shm"),
            Path(str(path) + "-journal"),
        )
        if candidate.exists()
    )


def recover_sqlite(path: Path) -> None:
    if not path.exists():
        return
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA quick_check").fetchone()
    finally:
        connection.close()


def open_immutable(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _load_manifest(
    path: Path,
    *,
    expected_byte_sha256: str,
    expected_self_sha256: str,
) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()
    byte_sha = hashlib.sha256(raw).hexdigest()
    if byte_sha != expected_byte_sha256:
        raise SemanticVisionError(
            f"manifest byte SHA mismatch: expected {expected_byte_sha256}, got {byte_sha}"
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SemanticVisionError("manifest is not valid UTF-8 JSON") from exc
    if raw != (canonical_json(payload) + "\n").encode("utf-8"):
        raise SemanticVisionError("manifest is not canonical JSON followed by one LF")
    body = dict(payload)
    actual_self = body.pop("semantic_coverage_manifest_sha256", None)
    replayed = canonical_sha256(
        {"domain": SEMANTIC_COVERAGE_MANIFEST_DOMAIN, "manifest": body}
    )
    if actual_self != expected_self_sha256 or replayed != expected_self_sha256:
        raise SemanticVisionError("manifest self SHA mismatch")
    if payload.get("sample_seed") != DEFAULT_SAMPLE_SEED:
        raise SemanticVisionError("manifest sample seed is not the frozen N10 seed")
    if payload.get("sample_size_buildings") != FIXED_BUILDING_COUNT:
        raise SemanticVisionError("manifest is not fixed N10")
    if payload.get("planned_occurrence_count") != FIXED_OCCURRENCE_COUNT:
        raise SemanticVisionError("manifest does not contain 57 occurrences")
    if payload.get("planned_unique_e1_pixel_count") != FIXED_OCCURRENCE_COUNT:
        raise SemanticVisionError("N10 requires 57 unique E1 pixel identities")
    return payload, raw


def _manifest_rows(payload: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    buildings: list[dict[str, Any]] = []
    occurrences: list[dict[str, Any]] = []
    input_rank = 0
    for building_rank, wrapper in enumerate(payload["selected_buildings"], 1):
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
                "coverage_plan_record_sha256": selected["coverage_plan_record_sha256"],
                "selected_building_record_sha256": wrapper["selected_building_record_sha256"],
                "manifest_json": canonical_json(wrapper),
            }
        )
        for occurrence_wrapper in plan["selected_occurrences"]:
            input_rank += 1
            occurrence = occurrence_wrapper["occurrence"]
            candidate = occurrence["candidate"]
            occurrences.append(
                {
                    "input_rank": input_rank,
                    "inference_id": f"semv_{input_rank:06d}",
                    "selection_id": building["selection_id"],
                    "occurrence_rank": occurrence["occurrence_rank"],
                    "source": candidate["source"],
                    "source_building_id": candidate["source_building_id"],
                    "source_asset_id": candidate["source_asset_id"],
                    "candidate_id": candidate["candidate_id"],
                    "fetch_url": candidate["fetch_url"],
                    "expected_response_sha256": candidate["raw_response_sha256"],
                    "expected_e1_pixel_sha256": candidate["normalized_pixel_sha256"],
                    "expected_e1_width": candidate["normalized_width"],
                    "expected_e1_height": candidate["normalized_height"],
                    "e2_asset_record_sha256": candidate["e2_asset_record_sha256"],
                    "e2_relation_record_sha256": candidate[
                        "e2_building_relation_record_sha256"
                    ],
                    "e3_candidate_record_sha256": candidate["e3_candidate_record_sha256"],
                    "e3_ranking_record_sha256": candidate["e3_ranking_record_sha256"],
                    "e3_shortlist_record_sha256": candidate[
                        "e3_shortlist_item_record_sha256"
                    ],
                    "occurrence_record_sha256": occurrence_wrapper[
                        "occurrence_record_sha256"
                    ],
                    "manifest_json": canonical_json(occurrence_wrapper),
                }
            )
    if len(buildings) != FIXED_BUILDING_COUNT or len(occurrences) != FIXED_OCCURRENCE_COUNT:
        raise SemanticVisionError("decoded manifest row accounting mismatch")
    if len({row["fetch_url"] for row in occurrences}) != FIXED_OCCURRENCE_COUNT:
        raise SemanticVisionError("fixed N10 unexpectedly contains duplicate fetch URLs")
    if len({row["expected_e1_pixel_sha256"] for row in occurrences}) != FIXED_OCCURRENCE_COUNT:
        raise SemanticVisionError("fixed N10 unexpectedly contains duplicate E1 pixels")
    for row in occurrences:
        parsed = urlsplit(row["fetch_url"])
        if parsed.scheme.casefold() != "https" or parsed.hostname != ALLOWED_HOSTS[row["source"]]:
            raise SemanticVisionError(f"frozen fetch URL host mismatch: {row['inference_id']}")
    return buildings, occurrences


def _artifact_spec(name: str, path: Path, record: Mapping[str, Any]) -> ArtifactSpec:
    return ArtifactSpec(
        name=name,
        path=path,
        expected_size=int(record["size_bytes"]),
        expected_sha256=str(record["byte_sha256"]),
        expected_logical_sha256=str(record["logical_sha256"]),
        expected_run_id=str(record["run_id"]),
        expected_application_id=int(record["application_id"]),
        expected_user_version=int(record["user_version"]),
    )


def _verify_artifact(path: Path, record: Mapping[str, Any]) -> str:
    if not path.is_file():
        raise FileNotFoundError(path)
    if path.stat().st_size != int(record["size_bytes"]):
        raise SemanticVisionError(f"input size mismatch: {path}")
    if _sidecars(path):
        raise SemanticVisionError(f"input SQLite sidecars present: {_sidecars(path)}")
    actual = file_sha256(path)
    if actual != record["byte_sha256"]:
        raise SemanticVisionError(f"input SHA mismatch: {path}")
    return actual


def _dependency_manifest() -> dict[str, Any]:
    return {
        "e1": dependency_versions(),
        "e1_contract": FINGERPRINT_CONTRACT_VERSION,
        "fetch_contract": FETCH_CONTRACT_VERSION,
        "semantic_contract": CONTRACT_VERSION,
        "transform": TRANSFORM_VERSION,
    }


def _materialization_provenance(
    directory: Path | None,
    *,
    retained_for_review: bool,
) -> dict[str, Any]:
    return {
        "path": str(directory.resolve()) if directory is not None else None,
        "retained_for_review": bool(retained_for_review),
        "write_contract": "atomic-exclusive-sha256-v1",
    }


def initialize_sidecar(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    manifest_path: Path,
    manifest_raw: bytes,
    payload: Mapping[str, Any],
    e2_path: Path,
    e3_path: Path,
    model: str,
    reasoning: str,
    service_tier: str,
    cli_version: str | None,
    materialization_cache_dir: Path | None = None,
    retain_review_cache: bool = False,
) -> None:
    buildings, occurrences = _manifest_rows(payload)
    dependency = _dependency_manifest()
    connection.executescript(SIDECAR_SCHEMA)
    run_values = {
        "run_id": run_id,
        "status": "initializing",
        "runner_version": RUNNER_VERSION,
        "schema_version": SCHEMA_VERSION,
        "manifest_path": str(manifest_path),
        "manifest_size": len(manifest_raw),
        "manifest_byte_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "manifest_self_sha256": payload["semantic_coverage_manifest_sha256"],
        "ordered_building_manifest_sha256": payload["ordered_building_manifest_sha256"],
        "ordered_occurrence_manifest_sha256": payload["ordered_occurrence_manifest_sha256"],
        "sample_seed": payload["sample_seed"],
        "building_count": FIXED_BUILDING_COUNT,
        "occurrence_count": FIXED_OCCURRENCE_COUNT,
        "e2_path": str(e2_path),
        "e2_size": int(payload["e2_input"]["size_bytes"]),
        "e2_sha256_before": payload["e2_input"]["byte_sha256"],
        "e2_logical_sha256": payload["e2_input"]["logical_sha256"],
        "e2_run_id": payload["e2_input"]["run_id"],
        "e3_path": str(e3_path),
        "e3_size": int(payload["e3_input"]["size_bytes"]),
        "e3_sha256_before": payload["e3_input"]["byte_sha256"],
        "e3_logical_sha256": payload["e3_input"]["logical_sha256"],
        "e3_run_id": payload["e3_input"]["run_id"],
        "contract_version": CONTRACT_VERSION,
        "prompt_version": PROMPT_VERSION,
        "output_schema_sha256": _schema_sha256(),
        "transform_version": TRANSFORM_VERSION,
        "e1_contract_version": FINGERPRINT_CONTRACT_VERSION,
        "dependency_manifest_json": canonical_json(dependency),
        "dependency_manifest_sha256": canonical_sha256(dependency),
        "runtime_version": RUNTIME_VERSION,
        "retry_policy_version": RETRY_POLICY_VERSION,
        "batch_size": FIXED_BATCH_SIZE,
        "max_fetch_attempts": FIXED_FETCH_ATTEMPTS,
        "max_vision_attempts": FIXED_VISION_ATTEMPTS,
        "model": model,
        "reasoning": reasoning,
        "service_tier": service_tier,
        "image_detail": CLI_IMAGE_DETAIL,
        "cli_version": cli_version,
        "started_at": utc_now(),
        "metrics_json": canonical_json(
            {
                "runtime_provenance": {
                    "materialization_cache": _materialization_provenance(
                        materialization_cache_dir,
                        retained_for_review=retain_review_cache,
                    )
                }
            }
        ),
    }
    columns = tuple(run_values)
    connection.execute(
        f"INSERT INTO semantic_runs({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
        tuple(run_values[column] for column in columns),
    )
    connection.executemany(
        "INSERT INTO selected_buildings VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                run_id,
                row["building_rank"],
                row["selection_id"],
                row["source"],
                row["source_building_id"],
                row["population_stratum"],
                row["guard_name"],
                row["qa_fallback"],
                row["building_record_sha256"],
                row["coverage_plan_record_sha256"],
                row["selected_building_record_sha256"],
                row["manifest_json"],
            )
            for row in buildings
        ],
    )
    connection.executemany(
        "INSERT INTO selected_occurrences VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                run_id,
                row["input_rank"],
                row["inference_id"],
                row["selection_id"],
                row["occurrence_rank"],
                row["source"],
                row["source_building_id"],
                row["source_asset_id"],
                row["candidate_id"],
                row["fetch_url"],
                row["expected_response_sha256"],
                row["expected_e1_pixel_sha256"],
                row["expected_e1_width"],
                row["expected_e1_height"],
                row["e2_asset_record_sha256"],
                row["e2_relation_record_sha256"],
                row["e3_candidate_record_sha256"],
                row["e3_ranking_record_sha256"],
                row["e3_shortlist_record_sha256"],
                row["occurrence_record_sha256"],
                row["manifest_json"],
            )
            for row in occurrences
        ],
    )
    connection.executemany(
        "INSERT INTO vision_inputs(run_id,inference_id,status) VALUES(?,?,'pending')",
        [(run_id, row["inference_id"]) for row in occurrences],
    )
    connection.execute("UPDATE semantic_runs SET status='running' WHERE run_id=?", (run_id,))
    connection.commit()


def _next_fetch_attempt(connection: sqlite3.Connection, run_id: str, inference_id: str) -> int:
    return int(
        connection.execute(
            "SELECT COALESCE(MAX(attempt_no),0)+1 FROM fetch_attempts WHERE run_id=? AND inference_id=?",
            (run_id, inference_id),
        ).fetchone()[0]
    )


def _insert_fetch_attempt(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    row: sqlite3.Row,
    attempt_no: int,
    started_at: str,
    elapsed_ms: int,
    outcome: str,
    payload: HttpPayload | None,
    failure: FetchFailure | None,
    actual_response_sha256: str | None,
    actual_e1_pixel_sha256: str | None,
    retryable: bool,
    retry_after: float | None,
    delay: float | None,
    error_kind: str | None,
    error_message: str | None,
) -> None:
    connection.execute(
        """
        INSERT INTO fetch_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            row["inference_id"],
            attempt_no,
            row["fetch_url"],
            payload.final_url if payload else (failure.final_url if failure else None),
            started_at,
            utc_now(),
            elapsed_ms,
            outcome,
            payload.http_status if payload else (failure.http_status if failure else None),
            payload.content_type if payload else (failure.content_type if failure else None),
            len(payload.body) if payload else (failure.response_bytes if failure else None),
            row["expected_response_sha256"],
            actual_response_sha256,
            row["expected_e1_pixel_sha256"],
            actual_e1_pixel_sha256,
            int(retryable),
            retry_after,
            delay,
            error_kind,
            (error_message or "")[:2000] or None,
        ),
    )


def _derivative_identity(raw: bytes) -> tuple[str, str, int, int, int]:
    decoded = decode_source(raw)
    pixel_sha = hashlib.sha256(
        b"RGB\0"
        + struct.pack(">II", decoded.width, decoded.height)
        + decoded.image.tobytes()
    ).hexdigest()
    return (
        hashlib.sha256(raw).hexdigest(),
        pixel_sha,
        decoded.width,
        decoded.height,
        len(raw),
    )


def _retain_review_input(directory: Path | None, inference_id: str, raw: bytes) -> None:
    if directory is None:
        raise SemanticVisionError("durable derivative cache is required")
    _atomic_write_bytes(directory / f"{inference_id}.jpg", raw)


def _load_materialized_input(
    directory: Path | None,
    row: sqlite3.Row,
) -> bytes:
    if directory is None:
        raise SemanticVisionError("ready input has no durable derivative cache")
    path = directory / f"{row['inference_id']}.jpg"
    if not path.is_file():
        raise SemanticVisionError(
            f"ready input is missing durable derivative: {row['inference_id']}"
        )
    raw = path.read_bytes()
    try:
        actual = _derivative_identity(raw)
    except Exception as exc:
        raise SemanticVisionError(
            f"durable derivative identity mismatch: {row['inference_id']}"
        ) from exc
    expected = (
        row["derivative_encoded_sha256"],
        row["derivative_pixel_sha256"],
        row["derivative_width"],
        row["derivative_height"],
        row["derivative_bytes"],
    )
    if actual != expected:
        raise SemanticVisionError(
            f"durable derivative identity mismatch: {row['inference_id']}"
        )
    return raw


def _materialization_cache_accounting(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    directory: Path,
) -> tuple[bool, dict[str, Any]]:
    expected_rows = list(
        connection.execute(
            """
            SELECT inference_id,derivative_encoded_sha256,derivative_pixel_sha256,
                   derivative_width,derivative_height,derivative_bytes
            FROM vision_inputs
            WHERE run_id=? AND derivative_encoded_sha256 IS NOT NULL
            ORDER BY inference_id
            """,
            (run_id,),
        )
    )
    expected_names = {f"{row['inference_id']}.jpg" for row in expected_rows}
    actual_names = {path.name for path in directory.iterdir()}
    failures: list[str] = []
    if expected_names != actual_names:
        failures.append(
            "missing=" + canonical_json(sorted(expected_names - actual_names))
        )
        failures.append(
            "unexpected=" + canonical_json(sorted(actual_names - expected_names))
        )
    for row in expected_rows:
        path = directory / f"{row['inference_id']}.jpg"
        if not path.is_file():
            continue
        try:
            actual = _derivative_identity(path.read_bytes())
        except Exception as exc:
            failures.append(f"{row['inference_id']}:decode:{type(exc).__name__}")
            continue
        expected = (
            row["derivative_encoded_sha256"],
            row["derivative_pixel_sha256"],
            row["derivative_width"],
            row["derivative_height"],
            row["derivative_bytes"],
        )
        if actual != expected:
            failures.append(f"{row['inference_id']}:identity")
    detail = {
        "actual_entries": len(actual_names),
        "expected_jpegs": len(expected_names),
        "failures": failures,
    }
    return not failures, detail


def _clear_stale_atomic_temps(directory: Path) -> None:
    for path in directory.glob(".*.tmp"):
        if path.is_file():
            path.unlink()


def _cleanup_internal_materializations(
    directory: Path,
    inference_ids: Sequence[str],
) -> None:
    """Remove only the exact opaque files owned by this run, then the empty dir."""

    if directory.is_symlink():
        raise SemanticVisionError("internal materialization cache cannot be a symlink")
    resolved = directory.resolve()
    for inference_id in inference_ids:
        if not re.fullmatch(r"semv_\d{6}", inference_id):
            raise SemanticVisionError(
                f"unsafe materialization cleanup identifier: {inference_id!r}"
            )
        target = (resolved / f"{inference_id}.jpg").resolve()
        if target.parent != resolved:
            raise SemanticVisionError("materialization cleanup escaped cache directory")
        try:
            target.unlink()
        except FileNotFoundError:
            pass
    try:
        resolved.rmdir()
    except OSError as exc:
        raise SemanticVisionError(
            f"internal materialization cache is not empty after exact cleanup: {resolved}"
        ) from exc


def _prepare_input(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    row: sqlite3.Row,
    fetcher: Callable[[str, str], HttpPayload],
    sleeper: Callable[[float], None],
    clock: Callable[[], float],
    review_cache_dir: Path | None,
    rate_limiter: _GlobalRequestRateLimiter | None = None,
) -> tuple[bytes | None, int]:
    attempts_made = 0
    input_state = connection.execute(
        "SELECT * FROM vision_inputs WHERE run_id=? AND inference_id=?",
        (run_id, row["inference_id"]),
    ).fetchone()
    if input_state is None:
        raise SemanticVisionError(f"vision input is missing: {row['inference_id']}")
    if input_state["status"] == "ready":
        return _load_materialized_input(review_cache_dir, input_state), 0
    if input_state["status"] != "pending":
        return None, 0
    if review_cache_dir is not None:
        orphan = review_cache_dir / f"{row['inference_id']}.jpg"
        if orphan.exists():
            orphan.unlink()
    attempt_no = _next_fetch_attempt(connection, run_id, row["inference_id"])
    while attempt_no <= FIXED_FETCH_ATTEMPTS:
        started_at = utc_now()
        started = clock()
        payload: HttpPayload | None = None
        actual_raw: str | None = None
        actual_pixel: str | None = None
        try:
            if rate_limiter is not None:
                rate_limiter.wait()
            payload = fetcher(row["source"], row["fetch_url"])
            attempts_made += int(payload.request_count)
            actual_raw = payload.raw_response_sha256
            fingerprint = fingerprint_bytes(payload.body)
            actual_pixel = fingerprint.pixel_sha256
            if actual_raw == row["expected_response_sha256"] and actual_pixel == row["expected_e1_pixel_sha256"]:
                outcome = "exact_match"
            elif actual_pixel == row["expected_e1_pixel_sha256"]:
                outcome = "delivery_changed_pixel_stable"
            else:
                outcome = "source_changed"
            if outcome != "exact_match":
                elapsed = max(0, round((clock() - started) * 1000))
                _insert_fetch_attempt(
                    connection, run_id=run_id, row=row, attempt_no=attempt_no,
                    started_at=started_at, elapsed_ms=elapsed, outcome=outcome,
                    payload=payload, failure=None, actual_response_sha256=actual_raw,
                    actual_e1_pixel_sha256=actual_pixel, retryable=False,
                    retry_after=None, delay=None, error_kind=outcome,
                    error_message="frozen E1 identity did not match exactly",
                )
                connection.execute(
                    "UPDATE vision_inputs SET status='fetch_failed',completed_at=?,error_kind=?,error_message=? WHERE run_id=? AND inference_id=?",
                    (utc_now(), outcome, "frozen E1 identity did not match exactly", run_id, row["inference_id"]),
                )
                connection.commit()
                return None, attempts_made
            decoded = decode_source(payload.body)
            derivative = prepare_derivative(decoded, "long1024", 1024)
            elapsed = max(0, round((clock() - started) * 1000))
            _insert_fetch_attempt(
                connection, run_id=run_id, row=row, attempt_no=attempt_no,
                started_at=started_at, elapsed_ms=elapsed, outcome="exact_match",
                payload=payload, failure=None, actual_response_sha256=actual_raw,
                actual_e1_pixel_sha256=actual_pixel, retryable=False,
                retry_after=None, delay=None, error_kind=None, error_message=None,
            )
            retained = connection.execute(
                "SELECT derivative_encoded_sha256,derivative_pixel_sha256,derivative_width,derivative_height,derivative_bytes FROM vision_inputs WHERE run_id=? AND inference_id=?",
                (run_id, row["inference_id"]),
            ).fetchone()
            actual_derivative = (
                derivative.encoded_sha256,
                derivative.pixel_sha256,
                derivative.width,
                derivative.height,
                len(derivative.encoded_bytes),
            )
            if retained[0] is not None and tuple(retained) != actual_derivative:
                raise SemanticVisionError(f"resume derivative mismatch: {row['inference_id']}")
            _retain_review_input(
                review_cache_dir,
                row["inference_id"],
                derivative.encoded_bytes,
            )
            connection.execute(
                """
                UPDATE vision_inputs SET status='ready',selected_fetch_attempt_no=?,
                  actual_response_sha256=?,actual_e1_pixel_sha256=?,
                  derivative_encoded_sha256=?,derivative_pixel_sha256=?,
                  derivative_width=?,derivative_height=?,derivative_bytes=?,
                  completed_at=NULL,error_kind=NULL,error_message=NULL
                WHERE run_id=? AND inference_id=?
                """,
                (
                    attempt_no, actual_raw, actual_pixel, derivative.encoded_sha256,
                    derivative.pixel_sha256, derivative.width, derivative.height,
                    len(derivative.encoded_bytes), run_id, row["inference_id"],
                ),
            )
            connection.commit()
            return derivative.encoded_bytes, attempts_made
        except FetchFailure as exc:
            attempts_made += int(exc.request_count)
            retryable = bool(getattr(exc, "retryable", False))
            retry_after = getattr(exc, "retry_after_seconds", None)
            kind = str(getattr(exc, "kind", exc.__class__.__name__))
            if kind == "oversize":
                outcome = "oversize"
            elif kind in {
                "decode",
                "decode_failed",
                "invalid_dimensions",
                "transform_not_applied",
            }:
                outcome = "decode_failed"
            elif kind in {"invalid_content_type", "empty_response"}:
                outcome = "invalid_content"
            else:
                outcome = "http_failed"
            can_retry = retryable and attempt_no < FIXED_FETCH_ATTEMPTS
            delay = max(float(retry_after or 0), min(60.0, float(2 ** (attempt_no - 1)))) if can_retry else None
            elapsed = max(0, round(float(exc.elapsed_seconds) * 1000))
            _insert_fetch_attempt(
                connection, run_id=run_id, row=row, attempt_no=attempt_no,
                started_at=started_at, elapsed_ms=elapsed, outcome=outcome,
                payload=None, failure=exc,
                actual_response_sha256=exc.raw_response_sha256,
                actual_e1_pixel_sha256=actual_pixel, retryable=can_retry,
                retry_after=retry_after, delay=delay, error_kind=kind,
                error_message=str(exc),
            )
            if not can_retry:
                connection.execute(
                    "UPDATE vision_inputs SET status='fetch_failed',completed_at=?,error_kind=?,error_message=? WHERE run_id=? AND inference_id=?",
                    (utc_now(), kind, str(exc)[:2000], run_id, row["inference_id"]),
                )
            connection.commit()
            if not can_retry:
                    return None, attempts_made
            sleeper(float(delay))
            attempt_no += 1
        except SemanticVisionError:
            raise
        except Exception as exc:
            kind = str(getattr(exc, "kind", exc.__class__.__name__))
            outcome = "decode_failed" if payload is not None else "http_failed"
            elapsed = max(0, round((clock() - started) * 1000))
            _insert_fetch_attempt(
                connection, run_id=run_id, row=row, attempt_no=attempt_no,
                started_at=started_at, elapsed_ms=elapsed, outcome=outcome,
                payload=payload, failure=None, actual_response_sha256=actual_raw,
                actual_e1_pixel_sha256=actual_pixel, retryable=False,
                retry_after=None, delay=None, error_kind=kind,
                error_message=str(exc),
            )
            connection.execute(
                "UPDATE vision_inputs SET status='fetch_failed',completed_at=?,error_kind=?,error_message=? WHERE run_id=? AND inference_id=?",
                (utc_now(), kind, str(exc)[:2000], run_id, row["inference_id"]),
            )
            connection.commit()
            return None, attempts_made
    connection.execute(
        "UPDATE vision_inputs SET status='fetch_failed',completed_at=?,error_kind='retry_exhausted',error_message='fetch or rematerialization retry budget exhausted' WHERE run_id=? AND inference_id=? AND status IN ('pending','ready')",
        (utc_now(), run_id, row["inference_id"]),
    )
    connection.commit()
    return None, attempts_made


def _write_vision_attempt(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    batch_no: int,
    attempt_no: int,
    inference_ids: Sequence[str],
    result: VisionRuntimeResult,
    status: str,
    started_at: str,
    error_kind: str | None = None,
    error_message: str | None = None,
) -> int:
    usage = result.usage
    stdout = _sanitize_capture(
        result.stdout,
        label="Vision stdout",
        max_bytes=MAX_STDOUT_CAPTURE_BYTES,
    )
    stderr = _sanitize_capture(
        result.stderr,
        label="Vision stderr",
        max_bytes=MAX_STDERR_CAPTURE_BYTES,
    )
    stored_error = _sanitize_capture(
        error_message or result.error_message,
        label="Vision error",
        max_bytes=2000,
    )
    cli_version = connection.execute(
        "SELECT cli_version FROM semantic_runs WHERE run_id=?", (run_id,)
    ).fetchone()[0]
    connection.execute(
        """
        INSERT INTO vision_attempts(
          run_id,batch_no,attempt_no,inference_ids_json,status,model,reasoning,
          service_tier,runtime_version,cli_version,codex_bin,image_detail,sandbox,
          prompt_sha256,output_schema_sha256,started_at,completed_at,elapsed_ms,
          input_tokens,cached_input_tokens,output_tokens,raw_events_sha256,
          error_kind,error_message)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id, batch_no, attempt_no, canonical_json(list(inference_ids)), status,
            result.provenance.model, result.provenance.reasoning,
            result.provenance.service_tier, result.provenance.runtime_version,
            cli_version, result.provenance.codex_bin,
            result.provenance.cli_image_detail, result.provenance.sandbox,
            result.provenance.prompt_sha256, result.provenance.output_schema_sha256,
            started_at, utc_now(), round(result.elapsed_seconds * 1000),
            usage.input_tokens if usage else None,
            usage.cached_input_tokens if usage else None,
            usage.output_tokens if usage else None,
            hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
            error_kind or result.error_kind,
            stored_error or None,
        ),
    )
    attempt_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
    connection.execute(
        "INSERT INTO vision_attempt_payloads VALUES(?,?,?,?,?,?,?,?,?)",
        (
            attempt_id, "gzip", _gzip_bytes(stdout), len(stdout.encode("utf-8")),
            hashlib.sha256(stdout.encode("utf-8")).hexdigest(), _gzip_bytes(stderr),
            len(stderr.encode("utf-8")), hashlib.sha256(stderr.encode("utf-8")).hexdigest(),
            stderr[-8000:] or None,
        ),
    )
    return attempt_id


def _write_success_results(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    attempt_id: int,
    rows: Sequence[sqlite3.Row],
    raw_results: Sequence[Mapping[str, Any]],
    normalized: Sequence[Mapping[str, Any]],
) -> None:
    for source_row, raw, result in zip(rows, raw_results, normalized):
        result_body = {
            "attempt_id": attempt_id,
            "inference_id": source_row["inference_id"],
            "normalized": result,
            "raw": raw,
        }
        connection.execute(
            """
            INSERT INTO semantic_results VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id, source_row["inference_id"], attempt_id, canonical_json(raw),
                canonical_json(result), int(result["in_scope"]), result["reject_reason"],
                result["medium"], result["spatial_context"], result["framing_scale"],
                result["camera_angle"], result["drawing_kind"], result["project_state"],
                result["project_legibility"], canonical_json(result["uncertain_axes"]),
                int(result["resolution_insufficient"]), result["evidence"],
                canonical_sha256(result_body),
            ),
        )
        derivative_sha = connection.execute(
            "SELECT derivative_pixel_sha256 FROM vision_inputs WHERE run_id=? AND inference_id=?",
            (run_id, source_row["inference_id"]),
        ).fetchone()[0]
        link = {
            "inference_id": source_row["inference_id"],
            "result_inference_id": source_row["inference_id"],
            "reuse_basis": "same_occurrence",
            "verified_input_pixel_sha256": derivative_sha,
        }
        connection.execute(
            "INSERT INTO occurrence_result_links VALUES(?,?,?,?,?,?)",
            (run_id, *link.values(), canonical_sha256(link)),
        )
        tier, reasons = derive_hero_decision(result)
        hero = {
            "inference_id": source_row["inference_id"],
            "tier": tier,
            "reasons": reasons,
            "authoritative": False,
        }
        connection.execute(
            "INSERT INTO hero_candidate_decisions VALUES(?,?,?,?,?,?)",
            (
                run_id, source_row["inference_id"], tier, canonical_json(reasons),
                0, canonical_sha256(hero),
            ),
        )
        for slot in derive_coverage_slots(result):
            rank = int(
                connection.execute(
                    "SELECT COUNT(*)+1 FROM coverage_slot_assignments WHERE run_id=? AND selection_id=? AND slot=? AND state='observed'",
                    (run_id, source_row["selection_id"], slot),
                ).fetchone()[0]
            )
            slot_body = {
                "assignment_rank": rank,
                "inference_id": source_row["inference_id"],
                "selection_id": source_row["selection_id"],
                "slot": slot,
                "state": "observed",
            }
            connection.execute(
                "INSERT INTO coverage_slot_assignments VALUES(?,?,?,?,?,?,?)",
                (
                    run_id, source_row["selection_id"], slot, rank, "observed",
                    source_row["inference_id"], canonical_sha256(slot_body),
                ),
            )
        connection.execute(
            "UPDATE vision_inputs SET status='success',completed_at=?,error_kind=NULL,error_message=NULL WHERE run_id=? AND inference_id=?",
            (utc_now(), run_id, source_row["inference_id"]),
        )


def _fill_unobserved_slots(connection: sqlite3.Connection, run_id: str) -> None:
    for building in connection.execute(
        "SELECT selection_id FROM selected_buildings WHERE run_id=? ORDER BY building_rank",
        (run_id,),
    ):
        for slot in COVERAGE_SLOTS:
            present = connection.execute(
                "SELECT 1 FROM coverage_slot_assignments WHERE run_id=? AND selection_id=? AND slot=? LIMIT 1",
                (run_id, building[0], slot),
            ).fetchone()
            if present:
                continue
            body = {
                "assignment_rank": 0,
                "inference_id": None,
                "selection_id": building[0],
                "slot": slot,
                "state": "not_observed_in_sample",
            }
            connection.execute(
                "INSERT INTO coverage_slot_assignments VALUES(?,?,?,?,?,?,?)",
                (run_id, building[0], slot, 0, "not_observed_in_sample", None, canonical_sha256(body)),
            )


def _metrics(connection: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    by_status = {
        row[0]: int(row[1])
        for row in connection.execute(
            "SELECT status,COUNT(*) FROM vision_inputs WHERE run_id=? GROUP BY status ORDER BY status",
            (run_id,),
        )
    }
    usage = connection.execute(
        """
        SELECT COUNT(*),SUM(status='success'),COALESCE(SUM(input_tokens),0),
               COALESCE(SUM(cached_input_tokens),0),COALESCE(SUM(output_tokens),0),
               COALESCE(SUM(elapsed_ms),0)
        FROM vision_attempts WHERE run_id=?
        """,
        (run_id,),
    ).fetchone()
    return {
        "buildings": FIXED_BUILDING_COUNT,
        "occurrences": FIXED_OCCURRENCE_COUNT,
        "input_status": by_status,
        "fetch_attempts": int(connection.execute("SELECT COUNT(*) FROM fetch_attempts WHERE run_id=?", (run_id,)).fetchone()[0]),
        "downloaded_bytes": int(connection.execute("SELECT COALESCE(SUM(response_bytes),0) FROM fetch_attempts WHERE run_id=?", (run_id,)).fetchone()[0]),
        "vision_attempts": int(usage[0]),
        "successful_vision_attempts": int(usage[1] or 0),
        "input_tokens": int(usage[2]),
        "cached_input_tokens": int(usage[3]),
        "output_tokens": int(usage[4]),
        "vision_elapsed_ms": int(usage[5]),
        "results": int(connection.execute("SELECT COUNT(*) FROM semantic_results WHERE run_id=?", (run_id,)).fetchone()[0]),
    }


def _validation_rows(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    manifest_path: Path,
    e2_after: str,
    e3_after: str,
    materialization_cache_dir: Path,
) -> list[dict[str, Any]]:
    run = connection.execute("SELECT * FROM semantic_runs WHERE run_id=?", (run_id,)).fetchone()
    input_status = {
        row[0]: int(row[1])
        for row in connection.execute(
            "SELECT status,COUNT(*) FROM vision_inputs WHERE run_id=? GROUP BY status", (run_id,)
        )
    }
    payload_bad = 0
    for row in connection.execute(
        "SELECT stdout_gzip,stdout_bytes,stdout_sha256,stderr_gzip,stderr_bytes,stderr_sha256 FROM vision_attempt_payloads"
    ):
        try:
            stdout = gzip.decompress(row[0])
            stderr = gzip.decompress(row[3])
            payload_bad += int(len(stdout) != row[1] or hashlib.sha256(stdout).hexdigest() != row[2])
            payload_bad += int(len(stderr) != row[4] or hashlib.sha256(stderr).hexdigest() != row[5])
        except (OSError, EOFError):
            payload_bad += 1
    fetch_gap = int(
        connection.execute(
            "SELECT COUNT(*) FROM (SELECT inference_id,MIN(attempt_no) lo,MAX(attempt_no) hi,COUNT(*) n FROM fetch_attempts GROUP BY inference_id HAVING lo<>1 OR hi<>n)"
        ).fetchone()[0]
    )
    result_count = int(connection.execute("SELECT COUNT(*) FROM semantic_results").fetchone()[0])
    success_count = input_status.get("success", 0)
    derivation_bad = int(
        connection.execute(
            """
            SELECT COUNT(*) FROM semantic_results r
            LEFT JOIN occurrence_result_links l ON l.run_id=r.run_id AND l.result_inference_id=r.inference_id
            LEFT JOIN hero_candidate_decisions h ON h.run_id=r.run_id AND h.inference_id=r.inference_id
            WHERE l.inference_id IS NULL OR h.inference_id IS NULL
            """
        ).fetchone()[0]
    )
    cache_ok, cache_detail = _materialization_cache_accounting(
        connection,
        run_id=run_id,
        directory=materialization_cache_dir,
    )
    checks = [
        ("manifest_identity", "error", file_sha256(manifest_path) == run["manifest_byte_sha256"], run["manifest_byte_sha256"], file_sha256(manifest_path), None),
        ("fixed_population_accounting", "error", connection.execute("SELECT COUNT(*) FROM selected_buildings").fetchone()[0] == FIXED_BUILDING_COUNT and connection.execute("SELECT COUNT(*) FROM selected_occurrences").fetchone()[0] == FIXED_OCCURRENCE_COUNT, f"{FIXED_BUILDING_COUNT}/{FIXED_OCCURRENCE_COUNT}", f"{connection.execute('SELECT COUNT(*) FROM selected_buildings').fetchone()[0]}/{connection.execute('SELECT COUNT(*) FROM selected_occurrences').fetchone()[0]}", None),
        ("input_files_immutable", "error", e2_after == run["e2_sha256_before"] and e3_after == run["e3_sha256_before"], f"{run['e2_sha256_before']}/{run['e3_sha256_before']}", f"{e2_after}/{e3_after}", None),
        ("attempt_accounting", "error", fetch_gap == 0, "0 attempt-number gaps", str(fetch_gap), None),
        ("result_accounting", "error", result_count == success_count, str(success_count), str(result_count), None),
        ("payload_integrity", "error", payload_bad == 0, "0", str(payload_bad), None),
        ("semantic_derivations", "error", derivation_bad == 0, "0", str(derivation_bad), None),
        ("no_pending_work", "error", input_status.get("pending", 0) == 0 and input_status.get("ready", 0) == 0, "0", str(input_status.get("pending", 0) + input_status.get("ready", 0)), None),
        ("sqlite_quick_check", "error", connection.execute("PRAGMA quick_check").fetchone()[0] == "ok", "ok", connection.execute("PRAGMA quick_check").fetchone()[0], None),
        ("sqlite_integrity_check", "error", connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "ok", connection.execute("PRAGMA integrity_check").fetchone()[0], None),
        ("foreign_key_check", "error", len(connection.execute("PRAGMA foreign_key_check").fetchall()) == 0, "0", str(len(connection.execute("PRAGMA foreign_key_check").fetchall())), None),
        (
            "materialization_cache_integrity",
            "error",
            cache_ok,
            canonical_json({"failures": [], "expected_jpegs": cache_detail["expected_jpegs"]}),
            canonical_json(cache_detail),
            "Every derivative-bearing input must have exactly one SHA-verified durable JPEG.",
        ),
        ("n10_technical_gate", "warning", success_count == FIXED_OCCURRENCE_COUNT, str(FIXED_OCCURRENCE_COUNT), str(success_count), "A frozen N10 never replaces a failed image."),
    ]
    return [
        {"name": name, "severity": severity, "passed": bool(passed), "expected": expected, "actual": actual, "detail": detail}
        for name, severity, passed, expected, actual, detail in checks
    ]


_LOGICAL_TABLES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("semantic_runs", "run_id", (
        "run_id","runner_version","schema_version","manifest_size","manifest_byte_sha256",
        "manifest_self_sha256","ordered_building_manifest_sha256","ordered_occurrence_manifest_sha256",
        "sample_seed","building_count","occurrence_count","e2_size","e2_sha256_before",
        "e2_logical_sha256","e2_run_id","e3_size","e3_sha256_before","e3_logical_sha256",
        "e3_run_id","contract_version","prompt_version","output_schema_sha256","transform_version",
        "e1_contract_version","dependency_manifest_json","dependency_manifest_sha256","runtime_version",
        "retry_policy_version","batch_size","max_fetch_attempts","max_vision_attempts","model",
        "reasoning","service_tier","image_detail","cli_version")),
    ("selected_buildings", "building_rank", ("building_rank","selection_id","source","source_building_id","population_stratum","guard_name","qa_fallback","building_record_sha256","coverage_plan_record_sha256","selected_building_record_sha256","manifest_json")),
    ("selected_occurrences", "input_rank", ("input_rank","inference_id","selection_id","occurrence_rank","source","source_building_id","source_asset_id","candidate_id","fetch_url","expected_response_sha256","expected_e1_pixel_sha256","expected_e1_width","expected_e1_height","e2_asset_record_sha256","e2_relation_record_sha256","e3_candidate_record_sha256","e3_ranking_record_sha256","e3_shortlist_record_sha256","occurrence_record_sha256","manifest_json")),
    ("vision_inputs", "inference_id", ("inference_id","status","selected_fetch_attempt_no","actual_response_sha256","actual_e1_pixel_sha256","derivative_encoded_sha256","derivative_pixel_sha256","derivative_width","derivative_height","derivative_bytes","error_kind","error_message")),
    ("fetch_attempts", "inference_id,attempt_no", ("inference_id","attempt_no","request_url","final_url","elapsed_ms","outcome","http_status","content_type","response_bytes","expected_response_sha256","actual_response_sha256","expected_e1_pixel_sha256","actual_e1_pixel_sha256","retryable","retry_after_seconds","scheduled_delay_seconds","error_kind","error_message")),
    ("vision_attempts", "attempt_id", ("attempt_id","batch_no","attempt_no","inference_ids_json","status","model","reasoning","service_tier","runtime_version","cli_version","codex_bin","image_detail","sandbox","prompt_sha256","output_schema_sha256","elapsed_ms","input_tokens","cached_input_tokens","output_tokens","raw_events_sha256","error_kind","error_message")),
    ("vision_attempt_payloads", "attempt_id", ("attempt_id","codec","stdout_bytes","stdout_sha256","stderr_bytes","stderr_sha256","stderr_excerpt")),
    ("semantic_results", "inference_id", ("inference_id","attempt_id","raw_result_json","normalized_result_json","in_scope","reject_reason","medium","spatial_context","framing_scale","camera_angle","drawing_kind","project_state","project_legibility","uncertain_axes_json","resolution_insufficient","evidence","record_sha256")),
    ("occurrence_result_links", "inference_id", ("inference_id","result_inference_id","reuse_basis","verified_input_pixel_sha256","record_sha256")),
    ("hero_candidate_decisions", "inference_id", ("inference_id","tier","reasons_json","authoritative","record_sha256")),
    ("coverage_slot_assignments", "selection_id,slot,assignment_rank", ("selection_id","slot","assignment_rank","state","inference_id","record_sha256")),
    ("validations", "validation_name", ("validation_name","severity","passed","expected","actual","detail")),
)


def logical_sha256(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    digest.update((LOGICAL_MANIFEST_VERSION + "\n").encode("ascii"))
    for table, order_by, columns in _LOGICAL_TABLES:
        digest.update((table + "\0").encode("utf-8"))
        for row in connection.execute(
            f"SELECT {','.join(columns)} FROM {table} ORDER BY {order_by}"
        ):
            digest.update(canonical_json(dict(zip(columns, row))).encode("utf-8") + b"\n")
    return digest.hexdigest()


def validate_terminal_sidecar(path: Path) -> tuple[bool, str, dict[str, Any]]:
    if _sidecars(path):
        raise SemanticVisionError(f"sidecar files remain beside terminal DB: {_sidecars(path)}")
    connection = open_immutable(path)
    try:
        if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise SemanticVisionError("semantic sidecar application_id mismatch")
        if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise SemanticVisionError("semantic sidecar user_version mismatch")
        run = connection.execute("SELECT * FROM semantic_runs").fetchone()
        if run is None or run["status"] not in {"complete", "complete_with_failures"}:
            raise SemanticVisionError("semantic sidecar is not publishable terminal state")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise SemanticVisionError("semantic sidecar quick_check failed")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SemanticVisionError("semantic sidecar integrity_check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise SemanticVisionError("semantic sidecar foreign_key_check failed")
        required = {
            row[0]: (row[1], row[2])
            for row in connection.execute("SELECT validation_name,severity,passed FROM validations")
        }
        required_names = (
            LEGACY_REQUIRED_VALIDATIONS
            if str(run["runner_version"]).endswith("v1.0.0")
            else REQUIRED_VALIDATIONS
        )
        if any(required.get(name) != ("error", 1) for name in required_names):
            raise SemanticVisionError("required semantic validations are absent or failed")
        logical = logical_sha256(connection)
        if logical != run["logical_sha256"]:
            raise SemanticVisionError("semantic logical SHA mismatch")
        return True, str(run["status"]), json.loads(run["metrics_json"])
    finally:
        connection.close()


def _acquire_lock(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.fstat(descriptor).st_size < 1:
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, b"1")
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError) as exc:
        os.close(descriptor)
        raise SemanticVisionError(f"semantic runner lock is held: {path}") from exc
    return descriptor


def _release_lock(path: Path, descriptor: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
    # The lock inode is intentionally persistent.  Unlinking after unlock can
    # race with a second holder and permits a third process to lock a new inode.


def _publish(partial_db: Path, output_db: Path, partial_report: Path, report_path: Path) -> None:
    linked_db = False
    linked_report = False
    try:
        os.link(partial_db, output_db)
        linked_db = True
        os.link(partial_report, report_path)
        linked_report = True
    except BaseException as exc:
        rollback_errors: list[str] = []
        for linked, path in ((linked_report, report_path), (linked_db, output_db)):
            if not linked:
                continue
            try:
                path.unlink()
            except OSError as rollback_exc:
                rollback_errors.append(f"{path}: {rollback_exc}")
        if rollback_errors:
            raise SemanticVisionError(
                "semantic publication failed and rollback was incomplete: "
                + "; ".join(rollback_errors)
            ) from exc
        if isinstance(exc, FileExistsError):
            raise FileExistsError(
                "immutable semantic output or report already exists"
            ) from exc
        raise
    partial_db.unlink()
    partial_report.unlink()


def _render_report(status: str, logical: str, metrics: Mapping[str, Any]) -> str:
    return "\n".join(
        [
            "# Cross-source semantic Vision N10",
            "",
            f"- Status: `{status}`",
            f"- Logical SHA-256: `{logical}`",
            f"- Buildings / occurrences: `{FIXED_BUILDING_COUNT}` / `{FIXED_OCCURRENCE_COUNT}`",
            f"- Input states: `{canonical_json(metrics['input_status'])}`",
            f"- Fetch attempts / downloaded bytes: `{metrics['fetch_attempts']}` / `{metrics['downloaded_bytes']}`",
            f"- Vision attempts / results: `{metrics['vision_attempts']}` / `{metrics['results']}`",
            f"- Tokens input / cached / output: `{metrics['input_tokens']}` / `{metrics['cached_input_tokens']}` / `{metrics['output_tokens']}`",
            "",
            "The sample is frozen. Failed or changed images were not replaced. Hero and coverage rows are non-authoritative pixel-derived evidence.",
            "",
        ]
    )


def _report_binding_sha256(status: str, metrics: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        _render_report(status, _REPORT_LOGICAL_PLACEHOLDER, metrics).encode("utf-8")
    ).hexdigest()


def _verify_report_binding(
    connection: sqlite3.Connection,
    report_path: Path,
) -> tuple[str, int]:
    run = connection.execute(
        "SELECT runner_version,status,logical_sha256,metrics_json FROM semantic_runs"
    ).fetchone()
    if run is None:
        raise SemanticVisionError("semantic run is missing for report verification")
    validation = connection.execute(
        "SELECT severity,passed,expected,actual FROM validations "
        "WHERE validation_name='report_binding'"
    ).fetchone()
    if str(run["runner_version"]).endswith("v1.0.0"):
        if not report_path.is_file():
            raise SemanticVisionError("completed DB exists without its report")
        raw = report_path.read_bytes()
        return hashlib.sha256(raw).hexdigest(), len(raw)
    metrics = json.loads(run["metrics_json"])
    expected_binding = _report_binding_sha256(str(run["status"]), metrics)
    if (
        validation is None
        or (validation["severity"], validation["passed"]) != ("error", 1)
        or validation["expected"] != expected_binding
        or validation["actual"] != expected_binding
    ):
        raise SemanticVisionError("semantic report binding validation mismatch")
    expected_raw = _render_report(
        str(run["status"]), str(run["logical_sha256"]), metrics
    ).encode("utf-8")
    if not report_path.is_file():
        raise SemanticVisionError("completed DB exists without its report")
    actual_raw = report_path.read_bytes()
    if actual_raw != expected_raw:
        raise SemanticVisionError("semantic report content does not match bound DB state")
    actual_sha = hashlib.sha256(actual_raw).hexdigest()
    if (
        metrics.get("report_sha256") != actual_sha
        or metrics.get("report_size_bytes") != len(actual_raw)
    ):
        raise SemanticVisionError("semantic report SHA/size binding mismatch")
    return actual_sha, len(actual_raw)


def _verify_retained_review_cache(
    connection: sqlite3.Connection,
    requested_directory: Path | None,
) -> None:
    run = connection.execute(
        "SELECT run_id,runner_version,metrics_json FROM semantic_runs"
    ).fetchone()
    if run is None:
        raise SemanticVisionError("semantic run is missing for cache verification")
    metrics = json.loads(run["metrics_json"] or "{}")
    cache = metrics.get("materialization_cache")
    if not isinstance(cache, Mapping):
        if str(run["runner_version"]).endswith("v1.0.0"):
            return
        raise SemanticVisionError("materialization-cache provenance is missing")
    retained = bool(cache.get("retained_for_review"))
    stored_path = cache.get("path")
    if not retained:
        if requested_directory is not None:
            raise SemanticVisionError("completed run did not retain a review cache")
        return
    if not isinstance(stored_path, str) or not stored_path:
        raise SemanticVisionError("retained review-cache path provenance is missing")
    directory = Path(stored_path)
    if requested_directory is not None and requested_directory.resolve() != directory:
        raise SemanticVisionError("completed review-cache path does not match provenance")
    if not directory.is_dir():
        raise SemanticVisionError(f"retained review cache is missing: {directory}")
    passed, detail = _materialization_cache_accounting(
        connection,
        run_id=str(run["run_id"]),
        directory=directory,
    )
    if not passed:
        raise SemanticVisionError(
            "retained review-cache accounting failed: " + canonical_json(detail)
        )


def run_semantic_vision_n10(
    *,
    manifest_path: Path,
    e2_path: Path,
    e3_path: Path,
    output_db: Path,
    report_path: Path,
    codex_bin: Path,
    model: str = DEFAULT_MODEL,
    reasoning: str = DEFAULT_REASONING,
    service_tier: str = DEFAULT_SERVICE_TIER,
    cli_version: str | None = None,
    resume: bool = False,
    review_cache_dir: Path | None = None,
    expected_manifest_byte_sha256: str = FROZEN_MANIFEST_BYTE_SHA256,
    expected_manifest_self_sha256: str = FROZEN_MANIFEST_SELF_SHA256,
    fetcher: Callable[[str, str], HttpPayload] = network_fetch,
    executor: Callable[..., VisionRuntimeResult] = run_codex_vision_batch,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    preflight_validator: Callable[..., Any] = validate_semantic_coverage_manifest,
) -> RunResult:
    manifest_path = manifest_path.resolve()
    e2_path = e2_path.resolve()
    e3_path = e3_path.resolve()
    output_db = output_db.resolve()
    report_path = report_path.resolve()
    if len({manifest_path, e2_path, e3_path, output_db, report_path}) != 5:
        raise ValueError("manifest, inputs, output, and report must be distinct")
    payload, manifest_raw = _load_manifest(
        manifest_path,
        expected_byte_sha256=expected_manifest_byte_sha256,
        expected_self_sha256=expected_manifest_self_sha256,
    )
    e2_spec = _artifact_spec("e2_evidence", e2_path, payload["e2_input"])
    e3_spec = _artifact_spec("e3_selection", e3_path, payload["e3_input"])
    preflight = preflight_validator(
        manifest_path,
        e2_spec=e2_spec,
        e3_spec=e3_spec,
        expected_sample_size=FIXED_BUILDING_COUNT,
        expected_sample_seed=DEFAULT_SAMPLE_SEED,
        expected_max_images_per_building=6,
    )
    if not bool(getattr(preflight, "passed", False)):
        raise SemanticVisionError("independent semantic-coverage preflight failed")
    _verify_artifact(e2_path, payload["e2_input"])
    _verify_artifact(e3_path, payload["e3_input"])

    output_db.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(str(output_db) + ".lock")
    descriptor = _acquire_lock(lock_path)
    requests_made = 0
    vision_requests = 0
    try:
        if output_db.exists():
            if not resume:
                raise FileExistsError(f"immutable semantic output exists: {output_db}")
            if not report_path.exists():
                raise SemanticVisionError("completed DB exists without its report")
            _, status, metrics = validate_terminal_sidecar(output_db)
            completed = open_immutable(output_db)
            try:
                _verify_report_binding(completed, report_path)
                _verify_retained_review_cache(completed, review_cache_dir)
                completed_logical = str(
                    completed.execute(
                        "SELECT logical_sha256 FROM semantic_runs"
                    ).fetchone()[0]
                )
            finally:
                completed.close()
            return RunResult(
                output_db,
                report_path,
                status,
                completed_logical,
                0,
                0,
                True,
                True,
                metrics,
            )
        if report_path.exists():
            raise FileExistsError(f"immutable semantic report exists: {report_path}")
        partial_db = output_db.with_name(output_db.name + ".partial")
        partial_report = report_path.with_name(report_path.name + ".partial")
        if partial_report.exists():
            raise FileExistsError(f"stale partial semantic report exists: {partial_report}")
        resumed = partial_db.exists()
        if resumed and not resume:
            raise FileExistsError(f"partial semantic DB exists; pass --resume: {partial_db}")
        if not resumed and resume:
            raise FileNotFoundError(f"partial semantic DB does not exist: {partial_db}")
        retain_review_cache = review_cache_dir is not None
        materialization_cache_dir = (
            review_cache_dir.resolve()
            if review_cache_dir is not None
            else Path(str(partial_db) + ".inputs")
        )
        if materialization_cache_dir.exists() and not resumed:
            raise FileExistsError(
                f"derivative materialization cache already exists: {materialization_cache_dir}"
            )
        if resumed and not materialization_cache_dir.is_dir():
            raise SemanticVisionError(
                f"resume derivative materialization cache is missing: {materialization_cache_dir}"
            )
        materialization_cache_dir.mkdir(parents=True, exist_ok=resumed)
        if resumed:
            recover_sqlite(partial_db)
        connection = sqlite3.connect(partial_db)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        try:
            if resumed:
                run = connection.execute("SELECT * FROM semantic_runs").fetchone()
                if run is None or run["status"] != "running":
                    raise SemanticVisionError("resume requires one running semantic run")
                expected = {
                    "manifest_byte_sha256": expected_manifest_byte_sha256,
                    "manifest_self_sha256": expected_manifest_self_sha256,
                    "e2_sha256_before": payload["e2_input"]["byte_sha256"],
                    "e3_sha256_before": payload["e3_input"]["byte_sha256"],
                    "contract_version": CONTRACT_VERSION,
                    "prompt_version": PROMPT_VERSION,
                    "output_schema_sha256": _schema_sha256(),
                    "transform_version": TRANSFORM_VERSION,
                    "runtime_version": RUNTIME_VERSION,
                    "model": model,
                    "reasoning": reasoning,
                    "service_tier": service_tier,
                    "cli_version": cli_version,
                }
                mismatches = {key: {"actual": run[key], "expected": value} for key, value in expected.items() if run[key] != value}
                if mismatches:
                    raise SemanticVisionError("resume contract mismatch: " + canonical_json(mismatches))
                runtime_provenance = json.loads(run["metrics_json"] or "{}")
                expected_cache_provenance = _materialization_provenance(
                    materialization_cache_dir,
                    retained_for_review=retain_review_cache,
                )
                if runtime_provenance.get("runtime_provenance", {}).get(
                    "materialization_cache"
                ) != expected_cache_provenance:
                    raise SemanticVisionError(
                        "resume materialization-cache provenance mismatch"
                    )
                run_id = str(run["run_id"])
                _clear_stale_atomic_temps(materialization_cache_dir)
            else:
                run_id = "semn10-" + hashlib.sha256(
                    (expected_manifest_self_sha256 + "\0" + model + "\0" + PROMPT_VERSION).encode("utf-8")
                ).hexdigest()[:24]
                initialize_sidecar(
                    connection,
                    run_id=run_id,
                    manifest_path=manifest_path,
                    manifest_raw=manifest_raw,
                    payload=payload,
                    e2_path=e2_path,
                    e3_path=e3_path,
                    model=model,
                    reasoning=reasoning,
                    service_tier=service_tier,
                    cli_version=cli_version,
                    materialization_cache_dir=materialization_cache_dir,
                    retain_review_cache=retain_review_cache,
                )

            source_rows = list(
                connection.execute(
                    """
                    SELECT o.*,i.status FROM selected_occurrences o
                    JOIN vision_inputs i USING(run_id,inference_id)
                    WHERE o.run_id=? ORDER BY o.input_rank
                    """,
                    (run_id,),
                )
            )
            schema_text = canonical_json(OUTPUT_SCHEMA)
            rate_limiter = _GlobalRequestRateLimiter(
                DEFAULT_REQUESTS_PER_SECOND,
                clock,
                sleeper,
            )
            for batch_start in range(0, len(source_rows), FIXED_BATCH_SIZE):
                batch_no = batch_start // FIXED_BATCH_SIZE + 1
                batch = source_rows[batch_start : batch_start + FIXED_BATCH_SIZE]
                outstanding = [
                    row for row in batch
                    if connection.execute("SELECT status FROM vision_inputs WHERE run_id=? AND inference_id=?", (run_id, row["inference_id"])).fetchone()[0] in {"pending", "ready"}
                ]
                if not outstanding:
                    continue
                with tempfile.TemporaryDirectory(prefix="semantic-vision-n10-") as temp_name:
                    temp_dir = Path(temp_name)
                    schema_path = temp_dir / "output.schema.json"
                    schema_path.write_text(schema_text, encoding="utf-8", newline="\n")
                    ready_rows: list[sqlite3.Row] = []
                    image_paths: list[Path] = []
                    for row in outstanding:
                        encoded, made = _prepare_input(
                            connection, run_id=run_id, row=row, fetcher=fetcher,
                            sleeper=sleeper, clock=clock,
                            review_cache_dir=materialization_cache_dir,
                            rate_limiter=rate_limiter,
                        )
                        requests_made += made
                        if encoded is None:
                            continue
                        image_path = temp_dir / f"{row['inference_id']}.jpg"
                        image_path.write_bytes(encoded)
                        ready_rows.append(row)
                        image_paths.append(image_path)
                    if not ready_rows:
                        continue
                    inference_ids = [row["inference_id"] for row in ready_rows]
                    existing_attempts = int(
                        connection.execute("SELECT COUNT(*) FROM vision_attempts WHERE run_id=? AND batch_no=?", (run_id, batch_no)).fetchone()[0]
                    )
                    success = False
                    for attempt_no in range(existing_attempts + 1, FIXED_VISION_ATTEMPTS + 1):
                        started_at = utc_now()
                        result = executor(
                            prompt=compose_prompt(inference_ids), image_paths=image_paths,
                            output_schema_path=schema_path, expected_asset_ids=inference_ids,
                            codex_bin=codex_bin, model=model, reasoning=reasoning,
                            service_tier=service_tier, working_directory=temp_dir,
                            timeout_seconds=600,
                        )
                        vision_requests += 1
                        if not result.ok:
                            _write_vision_attempt(
                                connection, run_id=run_id, batch_no=batch_no,
                                attempt_no=attempt_no, inference_ids=inference_ids,
                                result=result, status="failed", started_at=started_at,
                            )
                            connection.commit()
                            continue
                        try:
                            normalized = normalize_batch(result.records, inference_ids)
                        except Exception as exc:
                            _write_vision_attempt(
                                connection, run_id=run_id, batch_no=batch_no,
                                attempt_no=attempt_no, inference_ids=inference_ids,
                                result=result, status="failed", started_at=started_at,
                                error_kind="semantic_schema", error_message=str(exc),
                            )
                            connection.commit()
                            continue
                        attempt_id = _write_vision_attempt(
                            connection, run_id=run_id, batch_no=batch_no,
                            attempt_no=attempt_no, inference_ids=inference_ids,
                            result=result, status="success", started_at=started_at,
                        )
                        _write_success_results(
                            connection, run_id=run_id, attempt_id=attempt_id,
                            rows=ready_rows, raw_results=result.records,
                            normalized=normalized,
                        )
                        connection.commit()
                        success = True
                        break
                    if not success:
                        connection.executemany(
                            "UPDATE vision_inputs SET status='vision_failed',completed_at=?,error_kind='retry_exhausted',error_message='Vision retry budget exhausted' WHERE run_id=? AND inference_id=? AND status='ready'",
                            [(utc_now(), run_id, row["inference_id"]) for row in ready_rows],
                        )
                        connection.commit()

            _fill_unobserved_slots(connection, run_id)
            connection.commit()
            e2_after = _verify_artifact(e2_path, payload["e2_input"])
            e3_after = _verify_artifact(e3_path, payload["e3_input"])
            validation_rows = _validation_rows(
                connection, run_id=run_id, manifest_path=manifest_path,
                e2_after=e2_after, e3_after=e3_after,
                materialization_cache_dir=materialization_cache_dir,
            )
            metrics = _metrics(connection, run_id)
            cache_ok, cache_detail = _materialization_cache_accounting(
                connection,
                run_id=run_id,
                directory=materialization_cache_dir,
            )
            materialized_inference_ids = tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT inference_id FROM vision_inputs "
                    "WHERE run_id=? AND derivative_encoded_sha256 IS NOT NULL "
                    "ORDER BY inference_id",
                    (run_id,),
                )
            )
            metrics["materialization_cache"] = {
                "accounting": cache_detail,
                "path": str(materialization_cache_dir),
                "retained_for_review": retain_review_cache,
                "write_contract": "atomic-exclusive-sha256-v1",
            }
            hard_failures = [
                row
                for row in validation_rows
                if row["severity"] == "error" and not row["passed"]
            ]
            failures = FIXED_OCCURRENCE_COUNT - int(
                metrics["input_status"].get("success", 0)
            )
            terminal_status = (
                "failed_validation"
                if hard_failures
                else ("complete_with_failures" if failures else "complete")
            )
            report_binding = _report_binding_sha256(terminal_status, metrics)
            validation_rows.append(
                {
                    "name": "report_binding",
                    "severity": "error",
                    "passed": True,
                    "expected": report_binding,
                    "actual": report_binding,
                    "detail": "SHA-256 of canonical report with logical-SHA placeholder.",
                }
            )
            connection.executemany(
                "INSERT INTO validations VALUES(?,?,?,?,?,?,?)",
                [
                    (run_id, row["name"], row["severity"], int(row["passed"]), row["expected"], row["actual"], row["detail"])
                    for row in validation_rows
                ],
            )
            logical = logical_sha256(connection)
            report_raw = _render_report(terminal_status, logical, metrics).encode("utf-8")
            metrics["report_sha256"] = hashlib.sha256(report_raw).hexdigest()
            metrics["report_size_bytes"] = len(report_raw)
            if (
                _render_report(terminal_status, logical, metrics).encode("utf-8")
                != report_raw
            ):
                raise SemanticVisionError("report renderer introduced a hash-binding cycle")
            connection.execute(
                """
                UPDATE semantic_runs SET status=?,e2_sha256_after=?,e3_sha256_after=?,
                  completed_at=?,metrics_json=?,logical_sha256=?,error=? WHERE run_id=?
                """,
                (
                    terminal_status, e2_after, e3_after, utc_now(), canonical_json(metrics),
                    logical, canonical_json(hard_failures) if hard_failures else None, run_id,
                ),
            )
            connection.commit()
            if hard_failures:
                raise SemanticVisionError("semantic sidecar validation failed: " + canonical_json(hard_failures))
            _atomic_write_bytes(partial_report, report_raw)
        finally:
            connection.close()
        validate_terminal_sidecar(partial_db)
        partial_connection = open_immutable(partial_db)
        try:
            _verify_report_binding(partial_connection, partial_report)
        finally:
            partial_connection.close()
        _publish(partial_db, output_db, partial_report, report_path)
        validate_terminal_sidecar(output_db)
        published_connection = open_immutable(output_db)
        try:
            _verify_report_binding(published_connection, report_path)
        finally:
            published_connection.close()
        if not retain_review_cache:
            _cleanup_internal_materializations(
                materialization_cache_dir,
                materialized_inference_ids,
            )
        return RunResult(output_db, report_path, terminal_status, logical, requests_made, vision_requests, resumed, False, metrics)
    finally:
        _release_lock(lock_path, descriptor)


__all__ = [
    "APPLICATION_ID",
    "FIXED_BATCH_SIZE",
    "FIXED_BUILDING_COUNT",
    "FIXED_OCCURRENCE_COUNT",
    "FROZEN_MANIFEST_BYTE_SHA256",
    "FROZEN_MANIFEST_SELF_SHA256",
    "HttpPayload",
    "RunResult",
    "SemanticVisionError",
    "SIDECAR_SCHEMA",
    "logical_sha256",
    "network_fetch",
    "run_semantic_vision_n10",
    "validate_terminal_sidecar",
]
