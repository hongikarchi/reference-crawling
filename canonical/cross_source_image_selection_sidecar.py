"""Immutable SQLite contract for offline E3 image-policy comparison.

The artifact produced by this module is deliberately *candidate only*.  It may
compare deterministic ranking policies and persist shortlists or queue-cost
estimates, but it never declares an authoritative representative image and it
never contains executable Vision work.  The accepted E2 evidence artifact is
an immutable input whose byte and logical hashes are bound into every run.

Builders use one SQLite writer in WAL mode.  Terminal artifacts are
checkpointed back to DELETE journal mode before immutable read-only access.
"""

from __future__ import annotations

import errno
import json
import os
import socket
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final


SCHEMA_VERSION: Final = 1
APPLICATION_ID: Final = int.from_bytes(b"E3IS", "big")

RUN_STATUSES: Final = frozenset({"building", "complete", "failed_validation"})
TERMINAL_STATUSES: Final = frozenset({"complete", "failed_validation"})

FORBIDDEN_POLICY_TABLE_NAMES: Final = (
    "representatives",
    "representative_images",
    "vision_queue",
    "vision_tasks",
    "final_matches",
    "merge_decisions",
)

TABLE_NAMES: Final = (
    "selection_runs",
    "selection_inputs",
    "policy_definitions",
    "population_strata",
    "selected_buildings",
    "image_candidates",
    "policy_rankings",
    "shortlist_items",
    "queue_estimates",
    "selection_metrics",
    "selection_validations",
    "build_checkpoints",
)

INDEX_NAMES: Final = (
    "idx_selection_inputs_role",
    "idx_policy_definitions_enabled",
    "idx_population_strata_key",
    "idx_selected_buildings_entity",
    "idx_image_candidates_building",
    "idx_image_candidates_asset",
    "idx_image_candidates_exact",
    "idx_image_candidates_phash",
    "idx_policy_rankings_order",
    "idx_policy_rankings_selected",
    "idx_shortlist_items_candidate",
    "idx_queue_estimates_policy",
    "idx_selection_metrics_name",
    "idx_selection_validations_failed",
)


def _sha_check(column: str, *, nullable: bool = False) -> str:
    valid = (
        f"length({column})=64 AND {column}=lower({column}) "
        f"AND {column} NOT GLOB '*[^0-9a-f]*'"
    )
    return f"({column} IS NULL OR ({valid}))" if nullable else f"({valid})"


SIDECAR_SCHEMA: Final = f"""
PRAGMA foreign_keys=ON;
PRAGMA application_id={APPLICATION_ID};
PRAGMA user_version={SCHEMA_VERSION};

CREATE TABLE selection_runs (
    run_id                              TEXT PRIMARY KEY
                                                CHECK(length(trim(run_id)) > 0),
    contract_version                    TEXT NOT NULL
                                                CHECK(length(trim(contract_version)) > 0),
    builder_version                     TEXT NOT NULL
                                                CHECK(length(trim(builder_version)) > 0),
    e2_artifact_path                    TEXT NOT NULL
                                                CHECK(length(trim(e2_artifact_path)) > 0),
    e2_size_bytes                       INTEGER NOT NULL CHECK(e2_size_bytes > 0),
    e2_byte_sha256                      TEXT NOT NULL CHECK({_sha_check('e2_byte_sha256')}),
    e2_logical_sha256                   TEXT NOT NULL CHECK({_sha_check('e2_logical_sha256')}),
    policy_set_sha256                   TEXT NOT NULL CHECK({_sha_check('policy_set_sha256')}),
    selection_mode                      TEXT NOT NULL
                                                CHECK(selection_mode IN ('sample','full')),
    sample_size                         INTEGER CHECK(sample_size IS NULL OR sample_size > 0),
    sample_seed                         TEXT,
    shortlist_size                      INTEGER NOT NULL CHECK(shortlist_size > 0),
    ordered_selection_manifest_sha256   TEXT
                                                CHECK({_sha_check('ordered_selection_manifest_sha256', nullable=True)}),
    config_json                         TEXT NOT NULL DEFAULT '{{}}'
                                                CHECK(json_valid(config_json)
                                                      AND json_type(config_json)='object'),
    network_requests                    INTEGER NOT NULL DEFAULT 0 CHECK(network_requests=0),
    vision_requests                     INTEGER NOT NULL DEFAULT 0 CHECK(vision_requests=0),
    llm_requests                        INTEGER NOT NULL DEFAULT 0 CHECK(llm_requests=0),
    authoritative                       INTEGER NOT NULL DEFAULT 0 CHECK(authoritative=0),
    artifact_scope                      TEXT NOT NULL DEFAULT 'candidate_only'
                                                CHECK(artifact_scope='candidate_only'),
    status                              TEXT NOT NULL DEFAULT 'building'
                                                CHECK(status IN
                                                  ('building','complete','failed_validation')),
    started_at                          TEXT NOT NULL CHECK(length(trim(started_at)) > 0),
    completed_at                        TEXT,
    error                               TEXT,
    CHECK((selection_mode='sample' AND sample_size IS NOT NULL
                                      AND sample_seed IS NOT NULL
                                      AND length(trim(sample_seed)) > 0)
       OR (selection_mode='full' AND sample_size IS NULL AND sample_seed IS NULL)),
    CHECK((status='building' AND completed_at IS NULL)
       OR (status IN ('complete','failed_validation')
           AND completed_at IS NOT NULL AND length(trim(completed_at)) > 0)),
    CHECK(status<>'complete' OR ordered_selection_manifest_sha256 IS NOT NULL)
);

CREATE TRIGGER selection_runs_single_run
BEFORE INSERT ON selection_runs
WHEN EXISTS (SELECT 1 FROM selection_runs)
BEGIN
    SELECT RAISE(ABORT, 'an E3 selection sidecar contains exactly one run');
END;

CREATE TRIGGER selection_runs_provenance_immutable
BEFORE UPDATE OF run_id,contract_version,builder_version,e2_artifact_path,
                 e2_size_bytes,e2_byte_sha256,e2_logical_sha256,
                 policy_set_sha256,selection_mode,sample_size,sample_seed,
                 shortlist_size,config_json,network_requests,vision_requests,
                 llm_requests,authoritative,artifact_scope,started_at
ON selection_runs
BEGIN
    SELECT RAISE(ABORT, 'E3 selection run provenance is immutable');
END;

CREATE TRIGGER selection_runs_status_transition
BEFORE UPDATE OF status ON selection_runs
WHEN NEW.status<>OLD.status
 AND NOT (OLD.status='building'
          AND NEW.status IN ('complete','failed_validation'))
BEGIN
    SELECT RAISE(ABORT, 'invalid E3 selection run status transition');
END;

CREATE TRIGGER selection_runs_terminal_immutable
BEFORE UPDATE ON selection_runs
WHEN OLD.status IN ('complete','failed_validation')
BEGIN
    SELECT RAISE(ABORT, 'terminal E3 selection run is immutable');
END;

CREATE TRIGGER selection_runs_complete_requires_validations
BEFORE UPDATE OF status ON selection_runs
WHEN NEW.status='complete'
 AND (NOT EXISTS (
        SELECT 1 FROM selection_validations v
        WHERE v.run_id=OLD.run_id AND v.severity='error'
      )
      OR EXISTS (
        SELECT 1 FROM selection_validations v
        WHERE v.run_id=OLD.run_id AND v.severity='error' AND v.passed=0
      ))
BEGIN
    SELECT RAISE(ABORT, 'complete E3 run requires passing error validations');
END;

CREATE TRIGGER selection_runs_failed_requires_validation
BEFORE UPDATE OF status ON selection_runs
WHEN NEW.status='failed_validation'
 AND NOT EXISTS (
       SELECT 1 FROM selection_validations v
       WHERE v.run_id=OLD.run_id AND v.severity='error' AND v.passed=0
     )
BEGIN
    SELECT RAISE(ABORT, 'failed_validation requires a failed error validation');
END;

CREATE TRIGGER selection_runs_immutable_delete
BEFORE DELETE ON selection_runs
BEGIN
    SELECT RAISE(ABORT, 'E3 selection runs are immutable');
END;

CREATE TABLE selection_inputs (
    run_id                  TEXT NOT NULL REFERENCES selection_runs(run_id)
                                    ON UPDATE RESTRICT ON DELETE RESTRICT,
    input_name              TEXT NOT NULL CHECK(length(trim(input_name)) > 0),
    input_role              TEXT NOT NULL
                                    CHECK(input_role IN
                                      ('e2_evidence','policy_spec','auxiliary')),
    file_path               TEXT NOT NULL CHECK(length(trim(file_path)) > 0),
    size_bytes              INTEGER NOT NULL CHECK(size_bytes >= 0),
    sha256_before           TEXT NOT NULL CHECK({_sha_check('sha256_before')}),
    sha256_after            TEXT CHECK({_sha_check('sha256_after', nullable=True)}),
    logical_sha256          TEXT CHECK({_sha_check('logical_sha256', nullable=True)}),
    application_id          INTEGER,
    user_version            INTEGER,
    schema_manifest_sha256  TEXT CHECK({_sha_check('schema_manifest_sha256', nullable=True)}),
    recorded_at             TEXT NOT NULL CHECK(length(trim(recorded_at)) > 0),
    detail_json             TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(detail_json)),
    PRIMARY KEY(run_id,input_name),
    UNIQUE(run_id,file_path),
    CHECK(sha256_after IS NULL OR sha256_after=sha256_before),
    CHECK(input_role<>'e2_evidence' OR logical_sha256 IS NOT NULL)
);

CREATE TABLE policy_definitions (
    run_id                  TEXT NOT NULL REFERENCES selection_runs(run_id)
                                    ON UPDATE RESTRICT ON DELETE RESTRICT,
    policy_id               TEXT NOT NULL CHECK(length(trim(policy_id)) > 0),
    policy_version          TEXT NOT NULL CHECK(length(trim(policy_version)) > 0),
    policy_name             TEXT NOT NULL CHECK(length(trim(policy_name)) > 0),
    description             TEXT,
    shortlist_size          INTEGER NOT NULL CHECK(shortlist_size > 0),
    enabled                 INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    definition_json         TEXT NOT NULL CHECK(json_valid(definition_json)
                                                AND json_type(definition_json)='object'),
    policy_config_sha256    TEXT NOT NULL CHECK({_sha_check('policy_config_sha256')}),
    policy_record_sha256    TEXT NOT NULL CHECK({_sha_check('policy_record_sha256')}),
    created_at              TEXT NOT NULL CHECK(length(trim(created_at)) > 0),
    PRIMARY KEY(run_id,policy_id),
    UNIQUE(run_id,policy_config_sha256)
);

CREATE TABLE population_strata (
    run_id                      TEXT NOT NULL REFERENCES selection_runs(run_id)
                                        ON UPDATE RESTRICT ON DELETE RESTRICT,
    stratum_id                  TEXT NOT NULL CHECK(length(trim(stratum_id)) > 0),
    stratum_key                 TEXT NOT NULL CHECK(length(trim(stratum_key)) > 0),
    stratum_json                TEXT NOT NULL CHECK(json_valid(stratum_json)
                                                    AND json_type(stratum_json)='object'),
    population_count            INTEGER NOT NULL CHECK(population_count >= 0),
    eligible_count              INTEGER NOT NULL CHECK(eligible_count >= 0),
    selected_building_count     INTEGER NOT NULL CHECK(selected_building_count >= 0),
    selected_candidate_count    INTEGER NOT NULL CHECK(selected_candidate_count >= 0),
    stratum_record_sha256       TEXT NOT NULL CHECK({_sha_check('stratum_record_sha256')}),
    PRIMARY KEY(run_id,stratum_id),
    UNIQUE(run_id,stratum_key),
    CHECK(population_count>=eligible_count),
    CHECK(population_count>=selected_building_count)
);

CREATE TABLE selected_buildings (
    run_id                      TEXT NOT NULL REFERENCES selection_runs(run_id)
                                        ON UPDATE RESTRICT ON DELETE RESTRICT,
    selection_id               TEXT NOT NULL CHECK(length(trim(selection_id)) > 0),
    selection_rank             INTEGER NOT NULL CHECK(selection_rank > 0),
    stratum_id                  TEXT NOT NULL,
    source                      TEXT NOT NULL
                                        CHECK(source IN ('divisare','architizer')),
    entity_type                 TEXT NOT NULL CHECK(entity_type IN ('building','project')),
    source_entity_id            TEXT NOT NULL CHECK(length(trim(source_entity_id)) > 0),
    source_building_id          TEXT,
    source_project_id           TEXT,
    name                        TEXT,
    normalized_name             TEXT,
    selection_reason            TEXT NOT NULL CHECK(length(trim(selection_reason)) > 0),
    e2_source_record_sha256     TEXT NOT NULL CHECK({_sha_check('e2_source_record_sha256')}),
    e2_relation_record_sha256   TEXT CHECK({_sha_check('e2_relation_record_sha256', nullable=True)}),
    selection_record_sha256     TEXT NOT NULL CHECK({_sha_check('selection_record_sha256')}),
    detail_json                 TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(detail_json)),
    PRIMARY KEY(run_id,selection_id),
    UNIQUE(run_id,selection_rank),
    UNIQUE(run_id,source,entity_type,source_entity_id),
    FOREIGN KEY(run_id,stratum_id)
        REFERENCES population_strata(run_id,stratum_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CHECK((entity_type='building' AND source_building_id=source_entity_id)
       OR (entity_type='project' AND source_project_id=source_entity_id))
);

CREATE TABLE image_candidates (
    run_id                          TEXT NOT NULL REFERENCES selection_runs(run_id)
                                            ON UPDATE RESTRICT ON DELETE RESTRICT,
    candidate_id                    TEXT NOT NULL CHECK(length(trim(candidate_id)) > 0),
    selection_id                    TEXT NOT NULL,
    source                          TEXT NOT NULL
                                            CHECK(source IN ('divisare','architizer')),
    source_building_id              TEXT,
    source_project_id               TEXT,
    source_asset_id                 TEXT NOT NULL CHECK(length(trim(source_asset_id)) > 0),
    fingerprint_status              TEXT NOT NULL
                                            CHECK(fingerprint_status IN
                                              ('success','failed','skipped','excluded')),
    canonical_url                   TEXT,
    fetch_url                       TEXT,
    final_url                       TEXT,
    roles_json                      TEXT NOT NULL CHECK(json_valid(roles_json)
                                                        AND json_type(roles_json)='array'),
    primary_role                    TEXT NOT NULL CHECK(length(trim(primary_role)) > 0),
    role_rank                       INTEGER NOT NULL CHECK(role_rank >= 0),
    source_ordinal                  INTEGER CHECK(source_ordinal IS NULL OR source_ordinal >= 0),
    ordinal_is_derived              INTEGER NOT NULL DEFAULT 0 CHECK(ordinal_is_derived IN (0,1)),
    original_width                  INTEGER CHECK(original_width IS NULL OR original_width > 0),
    original_height                 INTEGER CHECK(original_height IS NULL OR original_height > 0),
    normalized_width                INTEGER CHECK(normalized_width IS NULL OR normalized_width > 0),
    normalized_height               INTEGER CHECK(normalized_height IS NULL OR normalized_height > 0),
    quality_flags_json              TEXT NOT NULL DEFAULT '[]'
                                            CHECK(json_valid(quality_flags_json)
                                                  AND json_type(quality_flags_json)='array'),
    low_information                 INTEGER NOT NULL DEFAULT 0 CHECK(low_information IN (0,1)),
    normalized_pixel_sha256         TEXT CHECK({_sha_check('normalized_pixel_sha256', nullable=True)}),
    exact_cluster_id                TEXT,
    phash_node_id                   TEXT,
    source_record_sha256            TEXT NOT NULL CHECK({_sha_check('source_record_sha256')}),
    occurrence_record_sha256        TEXT CHECK({_sha_check('occurrence_record_sha256', nullable=True)}),
    project_relation_record_sha256  TEXT CHECK({_sha_check('project_relation_record_sha256', nullable=True)}),
    building_relation_record_sha256 TEXT NOT NULL CHECK({_sha_check('building_relation_record_sha256')}),
    candidate_record_sha256         TEXT NOT NULL CHECK({_sha_check('candidate_record_sha256')}),
    detail_json                     TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(detail_json)),
    PRIMARY KEY(run_id,candidate_id),
    UNIQUE(run_id,selection_id,candidate_id),
    UNIQUE(run_id,selection_id,source,source_asset_id),
    FOREIGN KEY(run_id,selection_id)
        REFERENCES selected_buildings(run_id,selection_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CHECK((fingerprint_status='success'
           AND normalized_pixel_sha256 IS NOT NULL
           AND phash_node_id IS NOT NULL)
       OR (fingerprint_status<>'success'
           AND normalized_pixel_sha256 IS NULL
           AND phash_node_id IS NULL))
);

CREATE TABLE policy_rankings (
    run_id                      TEXT NOT NULL REFERENCES selection_runs(run_id)
                                        ON UPDATE RESTRICT ON DELETE RESTRICT,
    policy_id                   TEXT NOT NULL,
    policy_version              TEXT NOT NULL CHECK(length(trim(policy_version)) > 0),
    policy_config_sha256        TEXT NOT NULL CHECK({_sha_check('policy_config_sha256')}),
    selection_id                TEXT NOT NULL,
    candidate_id                TEXT NOT NULL,
    ranking_state               TEXT NOT NULL
                                        CHECK(ranking_state IN
                                          ('ranked','shortlisted','suppressed','ineligible')),
    editorial_rank              INTEGER CHECK(editorial_rank IS NULL OR editorial_rank > 0),
    shortlist_rank              INTEGER CHECK(shortlist_rank IS NULL OR shortlist_rank > 0),
    selected                    INTEGER NOT NULL CHECK(selected IN (0,1)),
    qa_fallback                 INTEGER NOT NULL DEFAULT 0 CHECK(qa_fallback IN (0,1)),
    hard_risk                   INTEGER NOT NULL DEFAULT 0 CHECK(hard_risk IN (0,1)),
    rank_tuple_json             TEXT NOT NULL CHECK(json_valid(rank_tuple_json)
                                                    AND json_type(rank_tuple_json)='array'),
    component_scores_json       TEXT NOT NULL CHECK(json_valid(component_scores_json)
                                                    AND json_type(component_scores_json)='object'),
    reasons_json                TEXT NOT NULL CHECK(json_valid(reasons_json)
                                                    AND json_type(reasons_json)='array'),
    suppressed_by_candidate_id  TEXT,
    suppression_reason          TEXT,
    fallback_reason             TEXT,
    ranking_record_sha256       TEXT NOT NULL CHECK({_sha_check('ranking_record_sha256')}),
    detail_json                 TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(detail_json)),
    PRIMARY KEY(run_id,policy_id,selection_id,candidate_id),
    FOREIGN KEY(run_id,policy_id)
        REFERENCES policy_definitions(run_id,policy_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY(run_id,selection_id,candidate_id)
        REFERENCES image_candidates(run_id,selection_id,candidate_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY(run_id,suppressed_by_candidate_id)
        REFERENCES image_candidates(run_id,candidate_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CHECK((selected=1 AND ranking_state='shortlisted' AND shortlist_rank IS NOT NULL)
       OR (selected=0 AND ranking_state<>'shortlisted' AND shortlist_rank IS NULL)),
    CHECK(ranking_state<>'suppressed' OR suppression_reason IS NOT NULL),
    CHECK(suppressed_by_candidate_id IS NULL OR selected=0)
);

CREATE TABLE shortlist_items (
    run_id                  TEXT NOT NULL REFERENCES selection_runs(run_id)
                                    ON UPDATE RESTRICT ON DELETE RESTRICT,
    policy_id               TEXT NOT NULL,
    selection_id            TEXT NOT NULL,
    shortlist_rank          INTEGER NOT NULL CHECK(shortlist_rank > 0),
    candidate_id            TEXT NOT NULL,
    shortlist_state         TEXT NOT NULL
                                    CHECK(shortlist_state IN ('primary','qa_fallback')),
    authoritative           INTEGER NOT NULL DEFAULT 0 CHECK(authoritative=0),
    item_record_sha256      TEXT NOT NULL CHECK({_sha_check('item_record_sha256')}),
    rationale_json          TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(rationale_json)),
    PRIMARY KEY(run_id,policy_id,selection_id,shortlist_rank),
    UNIQUE(run_id,policy_id,selection_id,candidate_id),
    FOREIGN KEY(run_id,policy_id,selection_id,candidate_id)
        REFERENCES policy_rankings(run_id,policy_id,selection_id,candidate_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE queue_estimates (
    run_id                      TEXT NOT NULL REFERENCES selection_runs(run_id)
                                        ON UPDATE RESTRICT ON DELETE RESTRICT,
    estimate_id                 TEXT NOT NULL CHECK(length(trim(estimate_id)) > 0),
    policy_id                   TEXT NOT NULL,
    stratum_id                  TEXT,
    queue_unit                  TEXT NOT NULL
                                        CHECK(queue_unit IN
                                          ('shortlist_item','selected_entity',
                                           'exact_unique_asset','all_candidate_asset','other')),
    population_count            INTEGER NOT NULL CHECK(population_count >= 0),
    estimated_queue_items       INTEGER NOT NULL CHECK(estimated_queue_items >= 0),
    tokens_per_item_low         REAL CHECK(tokens_per_item_low IS NULL OR tokens_per_item_low >= 0),
    tokens_per_item_point       REAL CHECK(tokens_per_item_point IS NULL OR tokens_per_item_point >= 0),
    tokens_per_item_high        REAL CHECK(tokens_per_item_high IS NULL OR tokens_per_item_high >= 0),
    projected_input_tokens      INTEGER CHECK(projected_input_tokens IS NULL OR projected_input_tokens >= 0),
    projected_output_tokens     INTEGER CHECK(projected_output_tokens IS NULL OR projected_output_tokens >= 0),
    projected_total_tokens      INTEGER CHECK(projected_total_tokens IS NULL OR projected_total_tokens >= 0),
    estimated_calls             INTEGER CHECK(estimated_calls IS NULL OR estimated_calls >= 0),
    retry_factor                REAL NOT NULL DEFAULT 1.0 CHECK(retry_factor >= 1.0),
    estimated_cost_usd          REAL CHECK(estimated_cost_usd IS NULL OR estimated_cost_usd >= 0),
    pricing_snapshot_json       TEXT NOT NULL DEFAULT '{{}}'
                                        CHECK(json_valid(pricing_snapshot_json)
                                              AND json_type(pricing_snapshot_json)='object'),
    quota_basis                 TEXT,
    projected_quota_percent     REAL CHECK(projected_quota_percent IS NULL
                                            OR projected_quota_percent >= 0),
    requests_executed           INTEGER NOT NULL DEFAULT 0 CHECK(requests_executed=0),
    authoritative               INTEGER NOT NULL DEFAULT 0 CHECK(authoritative=0),
    estimate_record_sha256      TEXT NOT NULL CHECK({_sha_check('estimate_record_sha256')}),
    detail_json                 TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(detail_json)),
    created_at                  TEXT NOT NULL CHECK(length(trim(created_at)) > 0),
    PRIMARY KEY(run_id,estimate_id),
    FOREIGN KEY(run_id,policy_id)
        REFERENCES policy_definitions(run_id,policy_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY(run_id,stratum_id)
        REFERENCES population_strata(run_id,stratum_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CHECK(tokens_per_item_low IS NULL OR tokens_per_item_high IS NULL
          OR tokens_per_item_low<=tokens_per_item_high)
);

CREATE TABLE selection_metrics (
    run_id              TEXT NOT NULL REFERENCES selection_runs(run_id)
                                ON UPDATE RESTRICT ON DELETE RESTRICT,
    phase               TEXT NOT NULL CHECK(length(trim(phase)) > 0),
    metric_name         TEXT NOT NULL CHECK(length(trim(metric_name)) > 0),
    stratum_json        TEXT NOT NULL DEFAULT '{{}}'
                                CHECK(json_valid(stratum_json)
                                      AND json_type(stratum_json)='object'),
    value_integer       INTEGER,
    value_real          REAL,
    value_text          TEXT,
    recorded_at         TEXT NOT NULL CHECK(length(trim(recorded_at)) > 0),
    PRIMARY KEY(run_id,phase,metric_name,stratum_json),
    CHECK((value_integer IS NOT NULL)+(value_real IS NOT NULL)+(value_text IS NOT NULL)=1)
);

CREATE TABLE selection_validations (
    run_id              TEXT NOT NULL REFERENCES selection_runs(run_id)
                                ON UPDATE RESTRICT ON DELETE RESTRICT,
    validation_name     TEXT NOT NULL CHECK(length(trim(validation_name)) > 0),
    severity            TEXT NOT NULL CHECK(severity IN ('error','warning','info')),
    passed              INTEGER NOT NULL CHECK(passed IN (0,1)),
    expected            TEXT,
    actual              TEXT,
    detail_json         TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(detail_json)),
    recorded_at         TEXT NOT NULL CHECK(length(trim(recorded_at)) > 0),
    PRIMARY KEY(run_id,validation_name)
);

CREATE TABLE build_checkpoints (
    run_id              TEXT NOT NULL REFERENCES selection_runs(run_id)
                                ON UPDATE RESTRICT ON DELETE RESTRICT,
    phase               TEXT NOT NULL CHECK(length(trim(phase)) > 0),
    cursor_json         TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(cursor_json)),
    completed_rows      INTEGER NOT NULL DEFAULT 0 CHECK(completed_rows >= 0),
    phase_complete      INTEGER NOT NULL DEFAULT 0 CHECK(phase_complete IN (0,1)),
    updated_at          TEXT NOT NULL CHECK(length(trim(updated_at)) > 0),
    PRIMARY KEY(run_id,phase)
);

CREATE INDEX idx_selection_inputs_role
    ON selection_inputs(run_id,input_role);
CREATE INDEX idx_policy_definitions_enabled
    ON policy_definitions(run_id,enabled,policy_id);
CREATE INDEX idx_population_strata_key
    ON population_strata(run_id,stratum_key);
CREATE INDEX idx_selected_buildings_entity
    ON selected_buildings(run_id,source,entity_type,source_entity_id);
CREATE INDEX idx_image_candidates_building
    ON image_candidates(run_id,selection_id,role_rank,source_ordinal);
CREATE INDEX idx_image_candidates_asset
    ON image_candidates(run_id,source,source_asset_id);
CREATE INDEX idx_image_candidates_exact
    ON image_candidates(run_id,exact_cluster_id) WHERE exact_cluster_id IS NOT NULL;
CREATE INDEX idx_image_candidates_phash
    ON image_candidates(run_id,phash_node_id) WHERE phash_node_id IS NOT NULL;
CREATE INDEX idx_policy_rankings_order
    ON policy_rankings(run_id,policy_id,selection_id,editorial_rank,candidate_id);
CREATE INDEX idx_policy_rankings_selected
    ON policy_rankings(run_id,policy_id,selection_id,shortlist_rank)
    WHERE selected=1;
CREATE INDEX idx_shortlist_items_candidate
    ON shortlist_items(run_id,candidate_id,policy_id);
CREATE INDEX idx_queue_estimates_policy
    ON queue_estimates(run_id,policy_id,queue_unit);
CREATE INDEX idx_selection_metrics_name
    ON selection_metrics(run_id,metric_name,phase);
CREATE INDEX idx_selection_validations_failed
    ON selection_validations(run_id,severity,validation_name) WHERE passed=0;
"""


_TERMINAL_GUARD_TABLES: Final = tuple(
    table for table in TABLE_NAMES if table != "selection_runs"
)


def _terminal_guard_schema() -> str:
    statements: list[str] = []
    for table in _TERMINAL_GUARD_TABLES:
        statements.extend(
            (
                f"""CREATE TRIGGER {table}_terminal_insert_guard
                    BEFORE INSERT ON {table}
                    WHEN coalesce((SELECT status FROM selection_runs
                                   WHERE run_id=NEW.run_id),'')<>'building'
                    BEGIN
                      SELECT RAISE(ABORT, 'terminal E3 selection is immutable');
                    END;""",
                f"""CREATE TRIGGER {table}_terminal_update_guard
                    BEFORE UPDATE ON {table}
                    WHEN coalesce((SELECT status FROM selection_runs
                                   WHERE run_id=OLD.run_id),'')<>'building'
                    BEGIN
                      SELECT RAISE(ABORT, 'terminal E3 selection is immutable');
                    END;""",
                f"""CREATE TRIGGER {table}_terminal_delete_guard
                    BEFORE DELETE ON {table}
                    WHEN coalesce((SELECT status FROM selection_runs
                                   WHERE run_id=OLD.run_id),'')<>'building'
                    BEGIN
                      SELECT RAISE(ABORT, 'terminal E3 selection is immutable');
                    END;""",
            )
        )
    return "\n".join(statements)


TERMINAL_GUARD_SCHEMA: Final = _terminal_guard_schema()
TRIGGER_NAMES: Final = (
    "selection_runs_single_run",
    "selection_runs_provenance_immutable",
    "selection_runs_status_transition",
    "selection_runs_terminal_immutable",
    "selection_runs_complete_requires_validations",
    "selection_runs_failed_requires_validation",
    "selection_runs_immutable_delete",
    *(
        f"{table}_terminal_{operation}_guard"
        for table in _TERMINAL_GUARD_TABLES
        for operation in ("insert", "update", "delete")
    ),
)


EXPECTED_TABLE_COLUMNS: Final = {
    "selection_runs": (
        "run_id", "contract_version", "builder_version", "e2_artifact_path",
        "e2_size_bytes", "e2_byte_sha256", "e2_logical_sha256",
        "policy_set_sha256", "selection_mode", "sample_size", "sample_seed",
        "shortlist_size", "ordered_selection_manifest_sha256", "config_json",
        "network_requests", "vision_requests", "llm_requests", "authoritative",
        "artifact_scope", "status", "started_at", "completed_at", "error",
    ),
    "selection_inputs": (
        "run_id", "input_name", "input_role", "file_path", "size_bytes",
        "sha256_before", "sha256_after", "logical_sha256", "application_id",
        "user_version", "schema_manifest_sha256", "recorded_at", "detail_json",
    ),
    "policy_definitions": (
        "run_id", "policy_id", "policy_version", "policy_name", "description",
        "shortlist_size", "enabled", "definition_json", "policy_config_sha256",
        "policy_record_sha256", "created_at",
    ),
    "population_strata": (
        "run_id", "stratum_id", "stratum_key", "stratum_json",
        "population_count", "eligible_count", "selected_building_count",
        "selected_candidate_count", "stratum_record_sha256",
    ),
    "selected_buildings": (
        "run_id", "selection_id", "selection_rank", "stratum_id", "source",
        "entity_type", "source_entity_id", "source_building_id",
        "source_project_id", "name", "normalized_name", "selection_reason",
        "e2_source_record_sha256", "e2_relation_record_sha256",
        "selection_record_sha256", "detail_json",
    ),
    "image_candidates": (
        "run_id", "candidate_id", "selection_id", "source",
        "source_building_id", "source_project_id", "source_asset_id",
        "fingerprint_status", "canonical_url", "fetch_url", "final_url",
        "roles_json", "primary_role", "role_rank", "source_ordinal",
        "ordinal_is_derived", "original_width", "original_height",
        "normalized_width", "normalized_height", "quality_flags_json",
        "low_information", "normalized_pixel_sha256", "exact_cluster_id",
        "phash_node_id", "source_record_sha256", "occurrence_record_sha256",
        "project_relation_record_sha256", "building_relation_record_sha256",
        "candidate_record_sha256", "detail_json",
    ),
    "policy_rankings": (
        "run_id", "policy_id", "policy_version", "policy_config_sha256", "selection_id",
        "candidate_id", "ranking_state", "editorial_rank", "shortlist_rank",
        "selected", "qa_fallback", "hard_risk", "rank_tuple_json",
        "component_scores_json", "reasons_json", "suppressed_by_candidate_id",
        "suppression_reason", "fallback_reason", "ranking_record_sha256",
        "detail_json",
    ),
    "shortlist_items": (
        "run_id", "policy_id", "selection_id", "shortlist_rank",
        "candidate_id", "shortlist_state", "authoritative",
        "item_record_sha256", "rationale_json",
    ),
    "queue_estimates": (
        "run_id", "estimate_id", "policy_id", "stratum_id", "queue_unit",
        "population_count", "estimated_queue_items", "tokens_per_item_low",
        "tokens_per_item_point", "tokens_per_item_high",
        "projected_input_tokens", "projected_output_tokens",
        "projected_total_tokens", "estimated_calls", "retry_factor",
        "estimated_cost_usd", "pricing_snapshot_json", "quota_basis",
        "projected_quota_percent", "requests_executed", "authoritative",
        "estimate_record_sha256", "detail_json", "created_at",
    ),
    "selection_metrics": (
        "run_id", "phase", "metric_name", "stratum_json", "value_integer",
        "value_real", "value_text", "recorded_at",
    ),
    "selection_validations": (
        "run_id", "validation_name", "severity", "passed", "expected",
        "actual", "detail_json", "recorded_at",
    ),
    "build_checkpoints": (
        "run_id", "phase", "cursor_json", "completed_rows", "phase_complete",
        "updated_at",
    ),
}


class SidecarSchemaError(RuntimeError):
    """Raised when a file is not the expected E3 selection schema."""


class BuildLockError(RuntimeError):
    """Raised when the advisory E3 build lock cannot be acquired safely."""


@dataclass(frozen=True)
class SidecarValidation:
    quick_check: str
    integrity_check: str
    foreign_key_violations: int
    semantic_violations: tuple[tuple[str, int], ...]
    table_counts: tuple[tuple[str, int], ...]
    run_status: str | None
    sqlite_sidecars: tuple[str, ...]

    @property
    def structurally_valid(self) -> bool:
        return (
            self.quick_check == "ok"
            and self.integrity_check == "ok"
            and self.foreign_key_violations == 0
            and all(count == 0 for _, count in self.semantic_violations)
        )

    @property
    def passed(self) -> bool:
        return self.structurally_valid and self.run_status == "complete"


@dataclass
class BuildLock:
    """Owned O_EXCL lockfile; release removes only the caller's token."""

    path: Path
    token: str
    hostname: str
    pid: int
    _released: bool = False

    def release(self) -> None:
        if self._released:
            return
        try:
            payload = _read_lock_payload(self.path)
        except FileNotFoundError:
            self._released = True
            return
        if (
            payload.get("token") != self.token
            or payload.get("hostname") != self.hostname
            or payload.get("pid") != self.pid
        ):
            raise BuildLockError(
                f"refusing to remove a lock owned by another process: {self.path}"
            )
        self.path.unlink()
        self._released = True

    def __enter__(self) -> "BuildLock":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def lock_path_for(path: Path | str) -> Path:
    """Return the conventional advisory-lock path for a sidecar."""

    return Path(str(Path(path).resolve()) + ".lock")


def _canonical_lock_bytes(payload: dict[str, object]) -> bytes:
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("ascii")


def _read_lock_payload(path: Path) -> dict[str, object]:
    raw = path.read_bytes()
    try:
        payload = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BuildLockError(
            f"invalid E3 lockfile; manual inspection required: {path}"
        ) from exc
    if not isinstance(payload, dict):
        raise BuildLockError(f"invalid E3 lockfile; manual inspection required: {path}")
    hostname = payload.get("hostname")
    pid = payload.get("pid")
    token = payload.get("token")
    if (
        not isinstance(hostname, str)
        or not hostname
        or isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 0
        or not isinstance(token, str)
        or not token
    ):
        raise BuildLockError(f"invalid E3 lockfile; manual inspection required: {path}")
    return payload


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        return True
    return True


def acquire_build_lock(path: Path | str) -> BuildLock:
    """Acquire an O_EXCL lock, recovering only a dead same-host owner."""

    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    hostname = socket.gethostname()
    pid = os.getpid()
    token = uuid.uuid4().hex
    payload = {
        "created_at": _utc_now(),
        "hostname": hostname,
        "pid": pid,
        "token": token,
    }
    encoded = _canonical_lock_bytes(payload)

    while True:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                target,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            os.write(descriptor, encoded)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            return BuildLock(target, token, hostname, pid)
        except FileExistsError:
            existing = _read_lock_payload(target)
            owner_host = str(existing["hostname"])
            owner_pid = int(existing["pid"])
            if owner_host != hostname or _pid_is_alive(owner_pid):
                raise BuildLockError(
                    "E3 build lock is held: "
                    f"{target} (host={owner_host!r}, pid={owner_pid})"
                )
            tombstone = target.with_name(
                f"{target.name}.stale-{owner_pid}-{uuid.uuid4().hex}"
            )
            try:
                target.rename(tombstone)
            except FileNotFoundError:
                continue
            moved = _read_lock_payload(tombstone)
            if moved != existing:
                raise BuildLockError(f"E3 lock changed during recovery: {tombstone}")
            tombstone.unlink()
            continue
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass
            raise


def _configure(connection: sqlite3.Connection, *, readonly: bool) -> None:
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    if readonly:
        connection.execute("PRAGMA query_only=ON")


def _schema_objects(connection: sqlite3.Connection, kind: str) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type=?", (kind,)
        )
    }


def _assert_schema(connection: sqlite3.Connection) -> None:
    application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
    schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    tables = _schema_objects(connection, "table")
    views = _schema_objects(connection, "view")
    triggers = _schema_objects(connection, "trigger")
    indexes = {
        name
        for name in _schema_objects(connection, "index")
        if not name.startswith("sqlite_")
    }
    missing_tables = sorted(set(TABLE_NAMES) - tables)
    unexpected_tables = sorted(tables - set(TABLE_NAMES))
    missing_triggers = sorted(set(TRIGGER_NAMES) - triggers)
    unexpected_triggers = sorted(triggers - set(TRIGGER_NAMES))
    missing_indexes = sorted(set(INDEX_NAMES) - indexes)
    unexpected_indexes = sorted(indexes - set(INDEX_NAMES))
    forbidden = sorted(set(FORBIDDEN_POLICY_TABLE_NAMES) & (tables | views))
    column_mismatches = {
        table: {
            "expected": EXPECTED_TABLE_COLUMNS[table],
            "actual": tuple(
                str(row[1])
                for row in connection.execute(f'PRAGMA table_info("{table}")')
            ),
        }
        for table in TABLE_NAMES
        if table in tables
        and tuple(
            str(row[1])
            for row in connection.execute(f'PRAGMA table_info("{table}")')
        )
        != EXPECTED_TABLE_COLUMNS[table]
    }
    if application_id != APPLICATION_ID:
        raise SidecarSchemaError(
            f"application_id mismatch: expected {APPLICATION_ID}, got {application_id}"
        )
    if schema_version != SCHEMA_VERSION:
        raise SidecarSchemaError(
            f"schema version mismatch: expected {SCHEMA_VERSION}, got {schema_version}"
        )
    if missing_tables:
        raise SidecarSchemaError(f"missing E3 tables: {', '.join(missing_tables)}")
    if forbidden:
        raise SidecarSchemaError(
            "forbidden authoritative/Vision schema objects: " + ", ".join(forbidden)
        )
    if unexpected_tables:
        raise SidecarSchemaError(f"unexpected E3 tables: {', '.join(unexpected_tables)}")
    if views:
        raise SidecarSchemaError(
            "E3 selection DB must not contain views: " + ", ".join(sorted(views))
        )
    if missing_triggers:
        raise SidecarSchemaError(f"missing E3 triggers: {', '.join(missing_triggers)}")
    if unexpected_triggers:
        raise SidecarSchemaError(
            f"unexpected E3 triggers: {', '.join(unexpected_triggers)}"
        )
    if missing_indexes:
        raise SidecarSchemaError(f"missing E3 indexes: {', '.join(missing_indexes)}")
    if unexpected_indexes:
        raise SidecarSchemaError(
            f"unexpected E3 indexes: {', '.join(unexpected_indexes)}"
        )
    if column_mismatches:
        raise SidecarSchemaError(
            "E3 table column contract mismatch: "
            + json.dumps(column_mismatches, sort_keys=True)
        )


def sqlite_sidecar_paths(path: Path | str) -> tuple[Path, ...]:
    target = Path(path).resolve()
    return tuple(
        Path(str(target) + suffix)
        for suffix in ("-wal", "-shm", "-journal")
        if Path(str(target) + suffix).exists()
    )


def initialize_sidecar(path: Path | str) -> sqlite3.Connection:
    """Create a new E3 sidecar in WAL mode without clobbering any file."""

    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor: int | None = None
    try:
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        descriptor = None
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise

    connection = sqlite3.connect(target)
    try:
        _configure(connection, readonly=False)
        journal_mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0])
        if journal_mode.casefold() != "wal":
            raise SidecarSchemaError(f"could not enable WAL mode: {journal_mode}")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.executescript(SIDECAR_SCHEMA)
        connection.executescript(TERMINAL_GUARD_SCHEMA)
        connection.commit()
        _assert_schema(connection)
    except Exception:
        connection.close()
        raise
    return connection


def open_sidecar(
    path: Path | str,
    *,
    readonly: bool = True,
    immutable: bool | None = None,
) -> sqlite3.Connection:
    """Open and identify an E3 artifact, optionally with immutable access."""

    target = Path(path).resolve()
    if not target.is_file():
        raise FileNotFoundError(f"E3 sidecar does not exist: {target}")
    use_immutable = readonly if immutable is None else bool(immutable)
    if use_immutable and not readonly:
        raise ValueError("immutable access must be read-only")
    if readonly:
        if use_immutable:
            sidecars = sqlite_sidecar_paths(target)
            if sidecars:
                raise SidecarSchemaError(
                    "immutable open refused until SQLite recovery/checkpoint: "
                    + ", ".join(str(item) for item in sidecars)
                )
        suffix = "?mode=ro&immutable=1" if use_immutable else "?mode=ro"
        connection = sqlite3.connect(f"file:{target.as_posix()}{suffix}", uri=True)
    else:
        connection = sqlite3.connect(target)
    try:
        _configure(connection, readonly=readonly)
        _assert_schema(connection)
        if not readonly:
            statuses = [str(row[0]) for row in connection.execute(
                "SELECT status FROM selection_runs"
            )]
            if statuses and any(status != "building" for status in statuses):
                raise SidecarSchemaError("terminal E3 sidecar cannot be opened writable")
    except Exception:
        connection.close()
        raise
    return connection


def recover_sidecar(path: Path | str, *, switch_to_delete: bool = False) -> None:
    """Perform writable SQLite recovery without changing application rows."""

    target = Path(path).resolve()
    if not target.is_file():
        raise FileNotFoundError(f"E3 sidecar does not exist: {target}")
    connection = sqlite3.connect(target)
    try:
        _configure(connection, readonly=False)
        _assert_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("SELECT count(*) FROM selection_runs").fetchone()
        connection.commit()
        mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).casefold()
        if mode == "wal":
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if switch_to_delete:
            changed = str(connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0])
            if changed.casefold() != "delete":
                raise SidecarSchemaError(f"could not restore DELETE journal mode: {changed}")
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def prepare_immutable_sidecar(path: Path | str) -> None:
    """Recover/checkpoint a terminal artifact and prove immutable-open safety."""

    target = Path(path).resolve()
    recover_sidecar(target, switch_to_delete=True)
    sidecars = sqlite_sidecar_paths(target)
    if sidecars:
        raise SidecarSchemaError(
            "SQLite sidecars remain after recovery: "
            + ", ".join(str(item) for item in sidecars)
        )
    connection = open_sidecar(target, readonly=True, immutable=True)
    try:
        statuses = [str(row[0]) for row in connection.execute(
            "SELECT status FROM selection_runs"
        )]
        if len(statuses) != 1 or statuses[0] not in TERMINAL_STATUSES:
            raise SidecarSchemaError(
                "immutable publication requires one terminal E3 selection run"
            )
    finally:
        connection.close()


def finalize_sidecar(
    connection: sqlite3.Connection,
    *,
    status: str,
    completed_at: str | None = None,
    error: str | None = None,
    close: bool = True,
) -> Path:
    """Set a terminal status, checkpoint WAL, and restore DELETE mode."""

    if status not in TERMINAL_STATUSES:
        raise ValueError("status must be 'complete' or 'failed_validation'")
    _assert_schema(connection)
    db_rows = connection.execute("PRAGMA database_list").fetchall()
    main_path = next(
        Path(str(row[2])).resolve() for row in db_rows if str(row[1]) == "main"
    )
    try:
        row = connection.execute(
            "SELECT run_id,status FROM selection_runs"
        ).fetchone()
        if row is None or str(row[1]) != "building":
            raise SidecarSchemaError(
                "finalization requires exactly one building E3 selection run"
            )
        connection.execute(
            "UPDATE selection_runs SET status=?,completed_at=?,error=? WHERE run_id=?",
            (status, completed_at or _utc_now(), error, str(row[0])),
        )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        mode = str(connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0])
        if mode.casefold() != "delete":
            raise SidecarSchemaError(f"could not restore DELETE journal mode: {mode}")
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        if close:
            connection.close()
    if close and sqlite_sidecar_paths(main_path):
        raise SidecarSchemaError(
            "SQLite sidecars remain after finalization: "
            + ", ".join(str(item) for item in sqlite_sidecar_paths(main_path))
        )
    return main_path


def validate_sidecar(
    path: Path | str,
    *,
    immutable: bool = True,
) -> SidecarValidation:
    """Run physical, relational, and candidate-only contract checks read-only."""

    target = Path(path).resolve()
    connection = open_sidecar(target, readonly=True, immutable=immutable)
    try:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        integrity_check = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_key_violations = sum(
            1 for _ in connection.execute("PRAGMA foreign_key_check")
        )
        semantic_queries = (
            ("single_run_count_mismatch", "SELECT abs(count(*)-1) FROM selection_runs"),
            (
                "candidate_only_run_contract_mismatch",
                """SELECT count(*) FROM selection_runs
                   WHERE network_requests<>0 OR vision_requests<>0 OR llm_requests<>0
                      OR authoritative<>0 OR artifact_scope<>'candidate_only'
                      OR coalesce(json_type(config_json,'$.network_requests'),'missing')
                           NOT IN ('integer','real')
                      OR json_extract(config_json,'$.network_requests')<>0
                      OR coalesce(json_type(config_json,'$.vision_requests'),'missing')
                           NOT IN ('integer','real')
                      OR json_extract(config_json,'$.vision_requests')<>0
                      OR coalesce(json_type(config_json,'$.llm_requests'),'missing')
                           NOT IN ('integer','real')
                      OR json_extract(config_json,'$.llm_requests')<>0
                      OR coalesce(json_type(config_json,'$.authoritative'),'missing')
                           NOT IN ('integer','real')
                      OR json_extract(config_json,'$.authoritative')<>0""",
            ),
            (
                "request_metric_mismatch",
                """SELECT count(*) FROM selection_runs r
                   WHERE (SELECT count(*) FROM selection_metrics m
                          WHERE m.run_id=r.run_id AND m.phase='validation'
                            AND m.metric_name IN
                              ('network_requests','vision_requests','llm_requests')
                            AND m.stratum_json='{}' AND m.value_integer=0
                            AND m.value_real IS NULL AND m.value_text IS NULL)<>3""",
            ),
            (
                "e2_input_count_mismatch",
                """SELECT count(*) FROM selection_runs r
                   WHERE (SELECT count(*) FROM selection_inputs i
                          WHERE i.run_id=r.run_id AND i.input_role='e2_evidence')<>1""",
            ),
            (
                "e2_input_lineage_mismatch",
                """SELECT count(*) FROM selection_runs r
                   JOIN selection_inputs i ON i.run_id=r.run_id
                                          AND i.input_role='e2_evidence'
                   WHERE i.file_path<>r.e2_artifact_path
                      OR i.size_bytes<>r.e2_size_bytes
                      OR i.sha256_before<>r.e2_byte_sha256
                      OR i.logical_sha256<>r.e2_logical_sha256""",
            ),
            (
                "terminal_input_sha_mismatch",
                """SELECT count(*) FROM selection_inputs i
                   JOIN selection_runs r USING(run_id)
                   WHERE r.status<>'building'
                     AND (i.sha256_after IS NULL OR i.sha256_after<>i.sha256_before)""",
            ),
            (
                "policy_set_count_mismatch",
                """SELECT count(*) FROM selection_runs r
                   WHERE NOT EXISTS(SELECT 1 FROM policy_definitions p
                                    WHERE p.run_id=r.run_id AND p.enabled=1)""",
            ),
            (
                "sample_selection_count_mismatch",
                """SELECT count(*) FROM selection_runs r
                   WHERE r.selection_mode='sample'
                     AND r.sample_size<>(SELECT count(*) FROM selected_buildings b
                                        WHERE b.run_id=r.run_id)""",
            ),
            (
                "stratum_selected_count_mismatch",
                """SELECT count(*) FROM population_strata s
                   WHERE s.selected_building_count<>(
                           SELECT count(*) FROM selected_buildings b
                           WHERE b.run_id=s.run_id AND b.stratum_id=s.stratum_id)
                      OR s.selected_candidate_count<>(
                           SELECT count(*) FROM image_candidates c
                           JOIN selected_buildings b
                             ON b.run_id=c.run_id AND b.selection_id=c.selection_id
                           WHERE b.run_id=s.run_id AND b.stratum_id=s.stratum_id)""",
            ),
            (
                "candidate_entity_mismatch",
                """SELECT count(*) FROM image_candidates c
                   JOIN selected_buildings b
                     ON b.run_id=c.run_id AND b.selection_id=c.selection_id
                   WHERE c.source<>b.source
                      OR (b.entity_type='building'
                          AND c.source_building_id<>b.source_building_id)
                      OR (b.entity_type='project'
                          AND c.source_project_id<>b.source_project_id)""",
            ),
            (
                "candidate_hash_status_mismatch",
                """SELECT count(*) FROM image_candidates
                   WHERE (fingerprint_status='success'
                          AND (normalized_pixel_sha256 IS NULL OR phash_node_id IS NULL))
                      OR (fingerprint_status<>'success'
                          AND (normalized_pixel_sha256 IS NOT NULL
                               OR phash_node_id IS NOT NULL))""",
            ),
            (
                "ranking_policy_version_mismatch",
                """SELECT count(*) FROM policy_rankings r
                   JOIN policy_definitions p USING(run_id,policy_id)
                   WHERE r.policy_version<>p.policy_version
                      OR r.policy_config_sha256<>p.policy_config_sha256""",
            ),
            (
                "shortlist_ranking_mismatch",
                """SELECT count(*) FROM shortlist_items s
                   JOIN policy_rankings r
                     ON r.run_id=s.run_id AND r.policy_id=s.policy_id
                    AND r.selection_id=s.selection_id
                    AND r.candidate_id=s.candidate_id
                   WHERE r.selected<>1 OR r.ranking_state<>'shortlisted'
                      OR r.shortlist_rank<>s.shortlist_rank
                      OR (s.shortlist_state='qa_fallback' AND r.qa_fallback<>1)
                      OR (s.shortlist_state='primary' AND r.qa_fallback<>0)""",
            ),
            (
                "shortlist_selection_count_mismatch",
                """SELECT count(*) FROM policy_rankings r
                   WHERE r.selected<>(CASE WHEN EXISTS(
                     SELECT 1 FROM shortlist_items s
                     WHERE s.run_id=r.run_id AND s.policy_id=r.policy_id
                       AND s.selection_id=r.selection_id
                       AND s.candidate_id=r.candidate_id
                   ) THEN 1 ELSE 0 END)""",
            ),
            (
                "shortlist_limit_exceeded",
                """SELECT count(*) FROM shortlist_items s
                   JOIN selection_runs r USING(run_id)
                   JOIN policy_definitions p USING(run_id,policy_id)
                   WHERE s.shortlist_rank>min(r.shortlist_size,p.shortlist_size)""",
            ),
            (
                "shortlist_rank_gap",
                """SELECT count(*) FROM (
                     SELECT run_id,policy_id,selection_id,count(*) AS n,max(shortlist_rank) AS m
                     FROM shortlist_items
                     GROUP BY run_id,policy_id,selection_id
                     HAVING n<>m
                   )""",
            ),
            (
                "authoritative_row_mismatch",
                """SELECT (SELECT count(*) FROM shortlist_items WHERE authoritative<>0)
                         +(SELECT count(*) FROM queue_estimates WHERE authoritative<>0)""",
            ),
            (
                "queue_request_mismatch",
                "SELECT count(*) FROM queue_estimates WHERE requests_executed<>0",
            ),
            (
                "complete_run_failed_error_validation",
                """SELECT count(*) FROM selection_runs r
                   WHERE r.status='complete' AND EXISTS(
                     SELECT 1 FROM selection_validations v
                     WHERE v.run_id=r.run_id AND v.severity='error' AND v.passed=0)""",
            ),
            (
                "failed_run_without_failed_error_validation",
                """SELECT count(*) FROM selection_runs r
                   WHERE r.status='failed_validation' AND NOT EXISTS(
                     SELECT 1 FROM selection_validations v
                     WHERE v.run_id=r.run_id AND v.severity='error' AND v.passed=0)""",
            ),
        )
        semantic = tuple(
            (name, int(connection.execute(query).fetchone()[0]))
            for name, query in semantic_queries
        )
        counts = tuple(
            (
                table,
                int(connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]),
            )
            for table in TABLE_NAMES
        )
        statuses = [
            str(row[0]) for row in connection.execute("SELECT status FROM selection_runs")
        ]
        return SidecarValidation(
            quick_check=quick_check,
            integrity_check=integrity_check,
            foreign_key_violations=foreign_key_violations,
            semantic_violations=semantic,
            table_counts=counts,
            run_status=statuses[0] if len(statuses) == 1 else None,
            sqlite_sidecars=tuple(str(item) for item in sqlite_sidecar_paths(target)),
        )
    finally:
        connection.close()


__all__ = [
    "APPLICATION_ID",
    "BuildLock",
    "BuildLockError",
    "EXPECTED_TABLE_COLUMNS",
    "FORBIDDEN_POLICY_TABLE_NAMES",
    "INDEX_NAMES",
    "RUN_STATUSES",
    "SCHEMA_VERSION",
    "SIDECAR_SCHEMA",
    "SidecarSchemaError",
    "SidecarValidation",
    "TABLE_NAMES",
    "TERMINAL_STATUSES",
    "TRIGGER_NAMES",
    "acquire_build_lock",
    "finalize_sidecar",
    "initialize_sidecar",
    "lock_path_for",
    "open_sidecar",
    "prepare_immutable_sidecar",
    "recover_sidecar",
    "sqlite_sidecar_paths",
    "validate_sidecar",
]
