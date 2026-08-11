"""SQLite contract for the offline E2 cross-source image-evidence artifact.

This module deliberately stores *evidence*, not decisions.  It has no table for
representative-image selection, Vision work queues, final entity matches, or
merge decisions.  Those policy choices belong to a later, separately reviewed
stage.

The E2 builder writes through one SQLite connection in WAL mode.  A completed
artifact is checkpointed back to DELETE journal mode and can then be opened
with SQLite's immutable read-only flag.  Source and E1 databases are inputs;
their byte hashes are recorded in :data:`e2_inputs` and are never attached for
writing by this module.
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
APPLICATION_ID: Final = int.from_bytes(b"E2IE", "big")

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
    "e2_runs",
    "e2_inputs",
    "source_projects",
    "source_buildings",
    "source_project_buildings",
    "assets",
    "project_asset_occurrences",
    "project_assets",
    "building_assets",
    "exact_pixel_clusters",
    "exact_pixel_cluster_members",
    "phash_nodes",
    "phash_node_members",
    "phash_candidates",
    "phash_edges",
    "metadata_building_pairs",
    "cross_source_project_image_evidence",
    "candidate_image_evidence",
    "cross_source_building_candidates",
    "e2_metrics",
    "e2_validations",
    "smoke_manifests",
    "smoke_manifest_items",
    "build_checkpoints",
)

INDEX_NAMES: Final = (
    "idx_e2_inputs_source_role",
    "idx_source_projects_name",
    "idx_source_buildings_name",
    "idx_source_project_buildings_building",
    "idx_assets_pixel_sha",
    "idx_assets_phash",
    "idx_assets_status",
    "idx_project_occurrences_asset",
    "idx_project_occurrences_project_role",
    "idx_project_assets_asset",
    "idx_building_assets_asset",
    "idx_exact_members_asset",
    "idx_phash_members_asset",
    "idx_phash_candidates_pair",
    "idx_phash_edges_distance",
    "idx_metadata_pairs_buildings",
    "idx_project_image_evidence_projects",
    "idx_candidate_image_evidence_assets",
    "idx_building_candidates_buildings",
    "idx_e2_metrics_name",
    "idx_e2_validations_failed",
    "idx_smoke_items_entity",
)


def _sha_check(column: str, *, nullable: bool = False) -> str:
    valid = (
        f"length({column})=64 AND {column}=lower({column}) "
        f"AND {column} NOT GLOB '*[^0-9a-f]*'"
    )
    return f"({column} IS NULL OR ({valid}))" if nullable else f"({valid})"


SIDECAR_SCHEMA = f"""
PRAGMA foreign_keys=ON;
PRAGMA application_id={APPLICATION_ID};
PRAGMA user_version={SCHEMA_VERSION};

CREATE TABLE e2_runs (
    run_id                              TEXT PRIMARY KEY
                                                CHECK(length(trim(run_id)) > 0),
    contract_version                    TEXT NOT NULL
                                                CHECK(length(trim(contract_version)) > 0),
    builder_version                     TEXT NOT NULL
                                                CHECK(length(trim(builder_version)) > 0),
    selection_mode                      TEXT NOT NULL
                                                CHECK(selection_mode IN ('sample','full')),
    sample_size                         INTEGER CHECK(sample_size IS NULL OR sample_size > 0),
    sample_seed                         TEXT,
    ordered_selection_manifest_sha256   TEXT
                                                CHECK({_sha_check('ordered_selection_manifest_sha256', nullable=True)}),
    config_json                         TEXT NOT NULL DEFAULT '{{}}'
                                                CHECK(json_valid(config_json)),
    status                              TEXT NOT NULL DEFAULT 'building'
                                                CHECK(status IN ('building','complete','failed_validation')),
    started_at                          TEXT NOT NULL
                                                CHECK(length(trim(started_at)) > 0),
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

CREATE TRIGGER e2_runs_single_run
BEFORE INSERT ON e2_runs
WHEN EXISTS (SELECT 1 FROM e2_runs)
BEGIN
    SELECT RAISE(ABORT, 'an E2 sidecar contains exactly one run');
END;

CREATE TRIGGER e2_runs_provenance_immutable
BEFORE UPDATE OF run_id,contract_version,builder_version,selection_mode,
                 sample_size,sample_seed,config_json,started_at
ON e2_runs
BEGIN
    SELECT RAISE(ABORT, 'E2 run provenance is immutable');
END;

CREATE TRIGGER e2_runs_status_transition
BEFORE UPDATE OF status ON e2_runs
WHEN NEW.status<>OLD.status
 AND NOT (OLD.status='building'
          AND NEW.status IN ('complete','failed_validation'))
BEGIN
    SELECT RAISE(ABORT, 'invalid E2 run status transition');
END;

CREATE TRIGGER e2_runs_terminal_immutable
BEFORE UPDATE ON e2_runs
WHEN OLD.status IN ('complete','failed_validation')
BEGIN
    SELECT RAISE(ABORT, 'terminal E2 run is immutable');
END;

CREATE TRIGGER e2_runs_complete_requires_validations
BEFORE UPDATE OF status ON e2_runs
WHEN NEW.status='complete'
 AND (NOT EXISTS (
        SELECT 1 FROM e2_validations v
        WHERE v.run_id=OLD.run_id AND v.severity='error'
      )
      OR EXISTS (
        SELECT 1 FROM e2_validations v
        WHERE v.run_id=OLD.run_id AND v.severity='error' AND v.passed=0
      ))
BEGIN
    SELECT RAISE(ABORT, 'complete E2 run requires passing error validations');
END;

CREATE TRIGGER e2_runs_failed_requires_validation
BEFORE UPDATE OF status ON e2_runs
WHEN NEW.status='failed_validation'
 AND NOT EXISTS (
        SELECT 1 FROM e2_validations v
        WHERE v.run_id=OLD.run_id AND v.severity='error' AND v.passed=0
     )
BEGIN
    SELECT RAISE(ABORT, 'failed_validation requires a failed error validation');
END;

CREATE TRIGGER e2_runs_immutable_delete
BEFORE DELETE ON e2_runs
BEGIN
    SELECT RAISE(ABORT, 'E2 runs are immutable');
END;

CREATE TABLE e2_inputs (
    run_id                  TEXT NOT NULL REFERENCES e2_runs(run_id)
                                    ON UPDATE RESTRICT ON DELETE RESTRICT,
    input_name              TEXT NOT NULL CHECK(length(trim(input_name)) > 0),
    source                  TEXT NOT NULL
                                    CHECK(source IN ('divisare','architizer','shared')),
    input_role              TEXT NOT NULL
                                    CHECK(input_role IN
                                      ('source_db','e1_sidecar','configuration','other')),
    file_path               TEXT NOT NULL CHECK(length(trim(file_path)) > 0),
    size_bytes              INTEGER NOT NULL CHECK(size_bytes >= 0),
    sha256_before           TEXT NOT NULL CHECK({_sha_check('sha256_before')}),
    sha256_after            TEXT CHECK({_sha_check('sha256_after', nullable=True)}),
    application_id          INTEGER,
    user_version            INTEGER,
    schema_manifest_sha256  TEXT CHECK({_sha_check('schema_manifest_sha256', nullable=True)}),
    recorded_at             TEXT NOT NULL CHECK(length(trim(recorded_at)) > 0),
    detail_json             TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(detail_json)),
    PRIMARY KEY(run_id, input_name),
    UNIQUE(run_id, file_path),
    CHECK(sha256_after IS NULL OR sha256_after=sha256_before)
);

CREATE TABLE source_projects (
    run_id                  TEXT NOT NULL REFERENCES e2_runs(run_id)
                                    ON UPDATE RESTRICT ON DELETE RESTRICT,
    source                  TEXT NOT NULL CHECK(source IN ('divisare','architizer')),
    source_project_id       TEXT NOT NULL CHECK(length(trim(source_project_id)) > 0),
    canonical_url           TEXT,
    slug                    TEXT,
    global_id               TEXT,
    name                    TEXT,
    normalized_name         TEXT,
    country                 TEXT,
    region                  TEXT,
    locality                TEXT,
    completion_year_min     INTEGER,
    completion_year_max     INTEGER,
    source_record_sha256    TEXT NOT NULL CHECK({_sha_check('source_record_sha256')}),
    metadata_json           TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(metadata_json)),
    PRIMARY KEY(run_id, source, source_project_id),
    CHECK(completion_year_min IS NULL OR completion_year_max IS NULL
          OR completion_year_min<=completion_year_max)
);

CREATE TABLE source_buildings (
    run_id                  TEXT NOT NULL REFERENCES e2_runs(run_id)
                                    ON UPDATE RESTRICT ON DELETE RESTRICT,
    source                  TEXT NOT NULL CHECK(source IN ('divisare','architizer')),
    source_building_id      TEXT NOT NULL CHECK(length(trim(source_building_id)) > 0),
    canonical_url           TEXT,
    slug                    TEXT,
    global_id               TEXT,
    name                    TEXT,
    normalized_name         TEXT,
    country                 TEXT,
    region                  TEXT,
    locality                TEXT,
    completion_year_min     INTEGER,
    completion_year_max     INTEGER,
    source_record_sha256    TEXT NOT NULL CHECK({_sha_check('source_record_sha256')}),
    metadata_json           TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(metadata_json)),
    PRIMARY KEY(run_id, source, source_building_id),
    CHECK(completion_year_min IS NULL OR completion_year_max IS NULL
          OR completion_year_min<=completion_year_max)
);

CREATE TABLE source_project_buildings (
    run_id                  TEXT NOT NULL,
    source                  TEXT NOT NULL CHECK(source IN ('divisare','architizer')),
    source_project_id       TEXT NOT NULL,
    source_building_id      TEXT NOT NULL,
    membership_reason       TEXT NOT NULL CHECK(length(trim(membership_reason)) > 0),
    membership_ordinal      INTEGER CHECK(membership_ordinal IS NULL
                                           OR membership_ordinal >= 0),
    source_record_sha256    TEXT NOT NULL CHECK({_sha_check('source_record_sha256')}),
    detail_json             TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(detail_json)),
    PRIMARY KEY(run_id, source, source_project_id, source_building_id),
    FOREIGN KEY(run_id, source, source_project_id)
        REFERENCES source_projects(run_id, source, source_project_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY(run_id, source, source_building_id)
        REFERENCES source_buildings(run_id, source, source_building_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE assets (
    run_id                      TEXT NOT NULL REFERENCES e2_runs(run_id)
                                        ON UPDATE RESTRICT ON DELETE RESTRICT,
    source                      TEXT NOT NULL
                                        CHECK(source IN ('divisare','architizer')),
    source_asset_id             TEXT NOT NULL
                                        CHECK(length(trim(source_asset_id)) > 0),
    e1_run_id                   TEXT NOT NULL CHECK(length(trim(e1_run_id)) > 0),
    fingerprint_status          TEXT NOT NULL
                                        CHECK(fingerprint_status IN
                                          ('success','failed','skipped','excluded')),
    canonical_url               TEXT,
    fetch_url                   TEXT,
    final_url                   TEXT,
    raw_response_sha256         TEXT CHECK({_sha_check('raw_response_sha256', nullable=True)}),
    normalized_pixel_sha256     TEXT CHECK({_sha_check('normalized_pixel_sha256', nullable=True)}),
    phash_hex                   TEXT CHECK({_sha_check('phash_hex', nullable=True)}),
    original_width              INTEGER CHECK(original_width IS NULL OR original_width > 0),
    original_height             INTEGER CHECK(original_height IS NULL OR original_height > 0),
    normalized_width            INTEGER CHECK(normalized_width IS NULL OR normalized_width > 0),
    normalized_height           INTEGER CHECK(normalized_height IS NULL OR normalized_height > 0),
    source_record_sha256        TEXT NOT NULL CHECK({_sha_check('source_record_sha256')}),
    provenance_json             TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(provenance_json)),
    error_kind                  TEXT,
    error_message               TEXT,
    PRIMARY KEY(run_id, source, source_asset_id),
    CHECK((fingerprint_status='success'
           AND raw_response_sha256 IS NOT NULL
           AND normalized_pixel_sha256 IS NOT NULL
           AND phash_hex IS NOT NULL)
       OR (fingerprint_status<>'success'
           AND normalized_pixel_sha256 IS NULL
           AND phash_hex IS NULL))
);

CREATE TABLE project_asset_occurrences (
    run_id                  TEXT NOT NULL REFERENCES e2_runs(run_id)
                                    ON UPDATE RESTRICT ON DELETE RESTRICT,
    source                  TEXT NOT NULL CHECK(source IN ('divisare','architizer')),
    occurrence_id           TEXT NOT NULL CHECK(length(trim(occurrence_id)) > 0),
    source_project_id       TEXT NOT NULL,
    raw_asset_key           TEXT NOT NULL CHECK(length(trim(raw_asset_key)) > 0),
    source_asset_id         TEXT,
    resolution_status       TEXT NOT NULL
                                    CHECK(resolution_status IN
                                      ('linked','excluded','missing','malformed')),
    role                    TEXT NOT NULL CHECK(length(trim(role)) > 0),
    ordinal                 INTEGER CHECK(ordinal IS NULL OR ordinal >= 0),
    occurrence_url          TEXT,
    source_record_sha256    TEXT NOT NULL CHECK({_sha_check('source_record_sha256')}),
    detail_json             TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(detail_json)),
    PRIMARY KEY(run_id, source, occurrence_id),
    FOREIGN KEY(run_id, source, source_project_id)
        REFERENCES source_projects(run_id, source, source_project_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY(run_id, source, source_asset_id)
        REFERENCES assets(run_id, source, source_asset_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CHECK((resolution_status='linked' AND source_asset_id IS NOT NULL)
       OR (resolution_status<>'linked' AND source_asset_id IS NULL))
);

CREATE TABLE project_assets (
    run_id                  TEXT NOT NULL,
    source                  TEXT NOT NULL CHECK(source IN ('divisare','architizer')),
    source_project_id       TEXT NOT NULL,
    source_asset_id         TEXT NOT NULL,
    occurrence_count        INTEGER NOT NULL CHECK(occurrence_count > 0),
    roles_json              TEXT NOT NULL CHECK(json_valid(roles_json)),
    first_ordinal           INTEGER CHECK(first_ordinal IS NULL OR first_ordinal >= 0),
    relation_record_sha256  TEXT NOT NULL CHECK({_sha_check('relation_record_sha256')}),
    PRIMARY KEY(run_id, source, source_project_id, source_asset_id),
    FOREIGN KEY(run_id, source, source_project_id)
        REFERENCES source_projects(run_id, source, source_project_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY(run_id, source, source_asset_id)
        REFERENCES assets(run_id, source, source_asset_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE building_assets (
    run_id                  TEXT NOT NULL,
    source                  TEXT NOT NULL CHECK(source IN ('divisare','architizer')),
    source_building_id      TEXT NOT NULL,
    source_asset_id         TEXT NOT NULL,
    project_count           INTEGER NOT NULL CHECK(project_count > 0),
    occurrence_count        INTEGER NOT NULL CHECK(occurrence_count > 0),
    roles_json              TEXT NOT NULL CHECK(json_valid(roles_json)),
    relation_record_sha256  TEXT NOT NULL CHECK({_sha_check('relation_record_sha256')}),
    PRIMARY KEY(run_id, source, source_building_id, source_asset_id),
    FOREIGN KEY(run_id, source, source_building_id)
        REFERENCES source_buildings(run_id, source, source_building_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY(run_id, source, source_asset_id)
        REFERENCES assets(run_id, source, source_asset_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE exact_pixel_clusters (
    run_id                      TEXT NOT NULL REFERENCES e2_runs(run_id)
                                        ON UPDATE RESTRICT ON DELETE RESTRICT,
    cluster_id                  TEXT NOT NULL CHECK(length(trim(cluster_id)) > 0),
    normalized_pixel_sha256     TEXT NOT NULL CHECK({_sha_check('normalized_pixel_sha256')}),
    member_count                INTEGER NOT NULL CHECK(member_count > 0),
    source_count                INTEGER NOT NULL CHECK(source_count BETWEEN 1 AND 2),
    project_count               INTEGER NOT NULL CHECK(project_count >= 0),
    building_count              INTEGER NOT NULL CHECK(building_count >= 0),
    is_cross_source             INTEGER NOT NULL CHECK(is_cross_source IN (0,1)),
    PRIMARY KEY(run_id, cluster_id),
    UNIQUE(run_id, normalized_pixel_sha256),
    CHECK(is_cross_source=(source_count=2))
);

CREATE TABLE exact_pixel_cluster_members (
    run_id                  TEXT NOT NULL,
    cluster_id              TEXT NOT NULL,
    source                  TEXT NOT NULL CHECK(source IN ('divisare','architizer')),
    source_asset_id         TEXT NOT NULL,
    PRIMARY KEY(run_id, cluster_id, source, source_asset_id),
    UNIQUE(run_id, source, source_asset_id),
    FOREIGN KEY(run_id, cluster_id)
        REFERENCES exact_pixel_clusters(run_id, cluster_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY(run_id, source, source_asset_id)
        REFERENCES assets(run_id, source, source_asset_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE phash_nodes (
    run_id                  TEXT NOT NULL REFERENCES e2_runs(run_id)
                                    ON UPDATE RESTRICT ON DELETE RESTRICT,
    node_id                 TEXT NOT NULL CHECK(length(trim(node_id)) > 0),
    phash_hex               TEXT NOT NULL CHECK({_sha_check('phash_hex')}),
    member_count            INTEGER NOT NULL CHECK(member_count > 0),
    source_count            INTEGER NOT NULL CHECK(source_count BETWEEN 1 AND 2),
    is_cross_source         INTEGER NOT NULL CHECK(is_cross_source IN (0,1)),
    PRIMARY KEY(run_id, node_id),
    UNIQUE(run_id, phash_hex),
    CHECK(is_cross_source=(source_count=2))
);

CREATE TABLE phash_node_members (
    run_id                  TEXT NOT NULL,
    node_id                 TEXT NOT NULL,
    source                  TEXT NOT NULL CHECK(source IN ('divisare','architizer')),
    source_asset_id         TEXT NOT NULL,
    PRIMARY KEY(run_id, node_id, source, source_asset_id),
    UNIQUE(run_id, source, source_asset_id),
    FOREIGN KEY(run_id, node_id)
        REFERENCES phash_nodes(run_id, node_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY(run_id, source, source_asset_id)
        REFERENCES assets(run_id, source, source_asset_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE phash_candidates (
    run_id                  TEXT NOT NULL REFERENCES e2_runs(run_id)
                                    ON UPDATE RESTRICT ON DELETE RESTRICT,
    candidate_id            TEXT NOT NULL CHECK(length(trim(candidate_id)) > 0),
    left_node_id            TEXT NOT NULL,
    right_node_id           TEXT NOT NULL,
    candidate_scope         TEXT NOT NULL
                                    CHECK(candidate_scope IN
                                      ('global_le8','metadata_le16')),
    shared_band_count       INTEGER NOT NULL CHECK(shared_band_count >= 0),
    recomputed_distance     INTEGER NOT NULL
                                    CHECK(recomputed_distance BETWEEN 0 AND 256),
    passed_threshold        INTEGER NOT NULL CHECK(passed_threshold IN (0,1)),
    candidate_record_sha256 TEXT NOT NULL CHECK({_sha_check('candidate_record_sha256')}),
    detail_json             TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(detail_json)),
    PRIMARY KEY(run_id, candidate_id),
    UNIQUE(run_id, left_node_id, right_node_id, candidate_scope),
    FOREIGN KEY(run_id, left_node_id)
        REFERENCES phash_nodes(run_id, node_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY(run_id, right_node_id)
        REFERENCES phash_nodes(run_id, node_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CHECK(left_node_id<right_node_id),
    CHECK(passed_threshold=(
      CASE candidate_scope
        WHEN 'global_le8' THEN recomputed_distance<=8
        ELSE recomputed_distance<=16
      END
    ))
);

CREATE TABLE phash_edges (
    run_id                  TEXT NOT NULL REFERENCES e2_runs(run_id)
                                    ON UPDATE RESTRICT ON DELETE RESTRICT,
    edge_id                 TEXT NOT NULL CHECK(length(trim(edge_id)) > 0),
    left_node_id            TEXT NOT NULL,
    right_node_id           TEXT NOT NULL,
    hamming_distance        INTEGER NOT NULL
                                    CHECK(hamming_distance BETWEEN 1 AND 16),
    edge_scope              TEXT NOT NULL
                                    CHECK(edge_scope IN ('global_le8','metadata_9_16')),
    candidate_id            TEXT NOT NULL,
    edge_record_sha256      TEXT NOT NULL CHECK({_sha_check('edge_record_sha256')}),
    detail_json             TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(detail_json)),
    PRIMARY KEY(run_id, edge_id),
    UNIQUE(run_id, left_node_id, right_node_id, edge_scope),
    FOREIGN KEY(run_id, left_node_id)
        REFERENCES phash_nodes(run_id, node_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY(run_id, right_node_id)
        REFERENCES phash_nodes(run_id, node_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY(run_id, candidate_id)
        REFERENCES phash_candidates(run_id, candidate_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CHECK(left_node_id<right_node_id),
    CHECK((edge_scope='global_le8' AND hamming_distance<=8)
       OR (edge_scope='metadata_9_16' AND hamming_distance BETWEEN 9 AND 16))
);

CREATE TABLE metadata_building_pairs (
    run_id                      TEXT NOT NULL REFERENCES e2_runs(run_id)
                                        ON UPDATE RESTRICT ON DELETE RESTRICT,
    metadata_pair_id            TEXT NOT NULL CHECK(length(trim(metadata_pair_id)) > 0),
    left_source                 TEXT NOT NULL CHECK(left_source IN ('divisare','architizer')),
    left_source_building_id     TEXT NOT NULL,
    right_source                TEXT NOT NULL CHECK(right_source IN ('divisare','architizer')),
    right_source_building_id    TEXT NOT NULL,
    blocker_version             TEXT NOT NULL CHECK(length(trim(blocker_version)) > 0),
    discovery_reason            TEXT NOT NULL CHECK(length(trim(discovery_reason)) > 0),
    normalized_name_equal       INTEGER CHECK(normalized_name_equal IN (0,1)),
    country_equal               INTEGER CHECK(country_equal IN (0,1)),
    locality_equal              INTEGER CHECK(locality_equal IN (0,1)),
    year_overlap                INTEGER CHECK(year_overlap IN (0,1)),
    metadata_record_sha256      TEXT NOT NULL CHECK({_sha_check('metadata_record_sha256')}),
    evidence_json               TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(evidence_json)),
    PRIMARY KEY(run_id, metadata_pair_id),
    UNIQUE(run_id,left_source,left_source_building_id,
                  right_source,right_source_building_id),
    FOREIGN KEY(run_id,left_source,left_source_building_id)
        REFERENCES source_buildings(run_id,source,source_building_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY(run_id,right_source,right_source_building_id)
        REFERENCES source_buildings(run_id,source,source_building_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CHECK(left_source<>right_source)
);

CREATE TABLE cross_source_project_image_evidence (
    run_id                      TEXT NOT NULL REFERENCES e2_runs(run_id)
                                        ON UPDATE RESTRICT ON DELETE RESTRICT,
    project_pair_id             TEXT NOT NULL CHECK(length(trim(project_pair_id)) > 0),
    left_source                 TEXT NOT NULL CHECK(left_source IN ('divisare','architizer')),
    left_source_project_id      TEXT NOT NULL,
    right_source                TEXT NOT NULL CHECK(right_source IN ('divisare','architizer')),
    right_source_project_id     TEXT NOT NULL,
    exact_asset_pair_count      INTEGER NOT NULL DEFAULT 0 CHECK(exact_asset_pair_count >= 0),
    identical_phash_pair_count  INTEGER NOT NULL DEFAULT 0 CHECK(identical_phash_pair_count >= 0),
    phash_le8_pair_count        INTEGER NOT NULL DEFAULT 0 CHECK(phash_le8_pair_count >= 0),
    phash_9_16_pair_count       INTEGER NOT NULL DEFAULT 0 CHECK(phash_9_16_pair_count >= 0),
    min_phash_distance          INTEGER CHECK(min_phash_distance BETWEEN 0 AND 16),
    evidence_record_sha256      TEXT NOT NULL CHECK({_sha_check('evidence_record_sha256')}),
    evidence_json               TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(evidence_json)),
    PRIMARY KEY(run_id, project_pair_id),
    UNIQUE(run_id,left_source,left_source_project_id,
                  right_source,right_source_project_id),
    FOREIGN KEY(run_id,left_source,left_source_project_id)
        REFERENCES source_projects(run_id,source,source_project_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY(run_id,right_source,right_source_project_id)
        REFERENCES source_projects(run_id,source,source_project_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CHECK(left_source<>right_source),
    CHECK(exact_asset_pair_count+identical_phash_pair_count+
          phash_le8_pair_count+phash_9_16_pair_count>0)
);

CREATE TABLE cross_source_building_candidates (
    run_id                      TEXT NOT NULL REFERENCES e2_runs(run_id)
                                        ON UPDATE RESTRICT ON DELETE RESTRICT,
    building_candidate_id       TEXT NOT NULL
                                        CHECK(length(trim(building_candidate_id)) > 0),
    left_source                 TEXT NOT NULL CHECK(left_source IN ('divisare','architizer')),
    left_source_building_id     TEXT NOT NULL,
    right_source                TEXT NOT NULL CHECK(right_source IN ('divisare','architizer')),
    right_source_building_id    TEXT NOT NULL,
    metadata_pair_id            TEXT,
    exact_asset_pair_count      INTEGER NOT NULL DEFAULT 0 CHECK(exact_asset_pair_count >= 0),
    identical_phash_pair_count  INTEGER NOT NULL DEFAULT 0 CHECK(identical_phash_pair_count >= 0),
    phash_le8_pair_count        INTEGER NOT NULL DEFAULT 0 CHECK(phash_le8_pair_count >= 0),
    phash_9_16_pair_count       INTEGER NOT NULL DEFAULT 0 CHECK(phash_9_16_pair_count >= 0),
    min_phash_distance          INTEGER CHECK(min_phash_distance BETWEEN 0 AND 16),
    discovery_basis_json        TEXT NOT NULL CHECK(json_valid(discovery_basis_json)),
    candidate_record_sha256     TEXT NOT NULL CHECK({_sha_check('candidate_record_sha256')}),
    PRIMARY KEY(run_id, building_candidate_id),
    UNIQUE(run_id,left_source,left_source_building_id,
                  right_source,right_source_building_id),
    FOREIGN KEY(run_id,left_source,left_source_building_id)
        REFERENCES source_buildings(run_id,source,source_building_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY(run_id,right_source,right_source_building_id)
        REFERENCES source_buildings(run_id,source,source_building_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY(run_id,metadata_pair_id)
        REFERENCES metadata_building_pairs(run_id,metadata_pair_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CHECK(left_source<>right_source),
    CHECK(metadata_pair_id IS NOT NULL OR
          exact_asset_pair_count+identical_phash_pair_count+
          phash_le8_pair_count+phash_9_16_pair_count>0)
);

CREATE TABLE candidate_image_evidence (
    run_id                  TEXT NOT NULL REFERENCES e2_runs(run_id)
                                    ON UPDATE RESTRICT ON DELETE RESTRICT,
    evidence_id             TEXT NOT NULL CHECK(length(trim(evidence_id)) > 0),
    building_candidate_id   TEXT NOT NULL,
    project_pair_id         TEXT,
    left_source             TEXT NOT NULL CHECK(left_source IN ('divisare','architizer')),
    left_source_asset_id    TEXT NOT NULL,
    right_source            TEXT NOT NULL CHECK(right_source IN ('divisare','architizer')),
    right_source_asset_id   TEXT NOT NULL,
    evidence_kind           TEXT NOT NULL
                                    CHECK(evidence_kind IN
                                      ('exact_pixel','identical_phash',
                                       'phash_le8','phash_9_16')),
    exact_cluster_id        TEXT,
    phash_edge_id           TEXT,
    phash_distance          INTEGER CHECK(phash_distance BETWEEN 0 AND 16),
    direct_evidence         INTEGER NOT NULL DEFAULT 1 CHECK(direct_evidence=1),
    evidence_record_sha256  TEXT NOT NULL CHECK({_sha_check('evidence_record_sha256')}),
    detail_json             TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(detail_json)),
    PRIMARY KEY(run_id, evidence_id),
    UNIQUE(run_id,building_candidate_id,left_source,left_source_asset_id,
                  right_source,right_source_asset_id,evidence_kind),
    FOREIGN KEY(run_id,building_candidate_id)
        REFERENCES cross_source_building_candidates(run_id,building_candidate_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY(run_id,project_pair_id)
        REFERENCES cross_source_project_image_evidence(run_id,project_pair_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY(run_id,left_source,left_source_asset_id)
        REFERENCES assets(run_id,source,source_asset_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY(run_id,right_source,right_source_asset_id)
        REFERENCES assets(run_id,source,source_asset_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY(run_id,exact_cluster_id)
        REFERENCES exact_pixel_clusters(run_id,cluster_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY(run_id,phash_edge_id)
        REFERENCES phash_edges(run_id,edge_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CHECK(left_source<>right_source),
    CHECK((evidence_kind='exact_pixel'
           AND exact_cluster_id IS NOT NULL AND phash_edge_id IS NULL
           AND phash_distance IS NULL)
       OR (evidence_kind='identical_phash'
           AND exact_cluster_id IS NULL AND phash_edge_id IS NULL
           AND phash_distance=0)
       OR (evidence_kind IN ('phash_le8','phash_9_16')
           AND exact_cluster_id IS NULL AND phash_edge_id IS NOT NULL
           AND phash_distance IS NOT NULL)),
    CHECK((evidence_kind='phash_le8' AND phash_distance BETWEEN 1 AND 8)
       OR (evidence_kind='phash_9_16' AND phash_distance BETWEEN 9 AND 16)
       OR evidence_kind IN ('exact_pixel','identical_phash'))
);

CREATE TABLE e2_metrics (
    run_id              TEXT NOT NULL REFERENCES e2_runs(run_id)
                                ON UPDATE RESTRICT ON DELETE RESTRICT,
    phase               TEXT NOT NULL CHECK(length(trim(phase)) > 0),
    metric_name         TEXT NOT NULL CHECK(length(trim(metric_name)) > 0),
    stratum_json        TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(stratum_json)),
    value_integer       INTEGER,
    value_real          REAL,
    value_text          TEXT,
    recorded_at         TEXT NOT NULL CHECK(length(trim(recorded_at)) > 0),
    PRIMARY KEY(run_id, phase, metric_name, stratum_json),
    CHECK((value_integer IS NOT NULL)+(value_real IS NOT NULL)+
          (value_text IS NOT NULL)=1)
);

CREATE TABLE e2_validations (
    run_id              TEXT NOT NULL REFERENCES e2_runs(run_id)
                                ON UPDATE RESTRICT ON DELETE RESTRICT,
    validation_name     TEXT NOT NULL CHECK(length(trim(validation_name)) > 0),
    severity            TEXT NOT NULL CHECK(severity IN ('error','warning','info')),
    passed              INTEGER NOT NULL CHECK(passed IN (0,1)),
    expected            TEXT,
    actual              TEXT,
    detail_json         TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(detail_json)),
    recorded_at         TEXT NOT NULL CHECK(length(trim(recorded_at)) > 0),
    PRIMARY KEY(run_id, validation_name)
);

CREATE TABLE smoke_manifests (
    run_id                  TEXT NOT NULL REFERENCES e2_runs(run_id)
                                    ON UPDATE RESTRICT ON DELETE RESTRICT,
    manifest_name           TEXT NOT NULL CHECK(length(trim(manifest_name)) > 0),
    sample_size             INTEGER NOT NULL CHECK(sample_size > 0),
    sample_seed             TEXT NOT NULL CHECK(length(trim(sample_seed)) > 0),
    selection_version       TEXT NOT NULL CHECK(length(trim(selection_version)) > 0),
    ordered_manifest_sha256 TEXT NOT NULL CHECK({_sha_check('ordered_manifest_sha256')}),
    selection_scope_json    TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(selection_scope_json)),
    created_at              TEXT NOT NULL CHECK(length(trim(created_at)) > 0),
    PRIMARY KEY(run_id, manifest_name)
);

CREATE TABLE smoke_manifest_items (
    run_id              TEXT NOT NULL,
    manifest_name       TEXT NOT NULL,
    selection_rank      INTEGER NOT NULL CHECK(selection_rank > 0),
    entity_kind         TEXT NOT NULL CHECK(entity_kind IN ('project','building','asset')),
    source              TEXT NOT NULL CHECK(source IN ('divisare','architizer')),
    source_entity_id    TEXT NOT NULL CHECK(length(trim(source_entity_id)) > 0),
    stratum             TEXT NOT NULL CHECK(length(trim(stratum)) > 0),
    score_sha256        TEXT NOT NULL CHECK({_sha_check('score_sha256')}),
    item_record_sha256  TEXT NOT NULL CHECK({_sha_check('item_record_sha256')}),
    detail_json         TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(detail_json)),
    PRIMARY KEY(run_id, manifest_name, selection_rank),
    UNIQUE(run_id, manifest_name, entity_kind, source, source_entity_id),
    FOREIGN KEY(run_id,manifest_name)
        REFERENCES smoke_manifests(run_id,manifest_name)
        ON UPDATE RESTRICT ON DELETE RESTRICT
);

CREATE TABLE build_checkpoints (
    run_id              TEXT NOT NULL REFERENCES e2_runs(run_id)
                                ON UPDATE RESTRICT ON DELETE RESTRICT,
    phase               TEXT NOT NULL CHECK(length(trim(phase)) > 0),
    cursor_json         TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(cursor_json)),
    completed_rows      INTEGER NOT NULL DEFAULT 0 CHECK(completed_rows >= 0),
    phase_complete      INTEGER NOT NULL DEFAULT 0 CHECK(phase_complete IN (0,1)),
    updated_at          TEXT NOT NULL CHECK(length(trim(updated_at)) > 0),
    PRIMARY KEY(run_id, phase)
);

CREATE INDEX idx_e2_inputs_source_role
    ON e2_inputs(run_id,source,input_role);
CREATE INDEX idx_source_projects_name
    ON source_projects(run_id,source,normalized_name);
CREATE INDEX idx_source_buildings_name
    ON source_buildings(run_id,source,normalized_name);
CREATE INDEX idx_source_project_buildings_building
    ON source_project_buildings(run_id,source,source_building_id);
CREATE INDEX idx_assets_pixel_sha
    ON assets(run_id,normalized_pixel_sha256)
    WHERE normalized_pixel_sha256 IS NOT NULL;
CREATE INDEX idx_assets_phash
    ON assets(run_id,phash_hex) WHERE phash_hex IS NOT NULL;
CREATE INDEX idx_assets_status
    ON assets(run_id,source,fingerprint_status);
CREATE INDEX idx_project_occurrences_asset
    ON project_asset_occurrences(run_id,source,source_asset_id)
    WHERE source_asset_id IS NOT NULL;
CREATE INDEX idx_project_occurrences_project_role
    ON project_asset_occurrences(run_id,source,source_project_id,role,ordinal);
CREATE INDEX idx_project_assets_asset
    ON project_assets(run_id,source,source_asset_id);
CREATE INDEX idx_building_assets_asset
    ON building_assets(run_id,source,source_asset_id);
CREATE INDEX idx_exact_members_asset
    ON exact_pixel_cluster_members(run_id,source,source_asset_id);
CREATE INDEX idx_phash_members_asset
    ON phash_node_members(run_id,source,source_asset_id);
CREATE INDEX idx_phash_candidates_pair
    ON phash_candidates(run_id,left_node_id,right_node_id);
CREATE INDEX idx_phash_edges_distance
    ON phash_edges(run_id,hamming_distance,left_node_id,right_node_id);
CREATE INDEX idx_metadata_pairs_buildings
    ON metadata_building_pairs(run_id,left_source,left_source_building_id,
                                      right_source,right_source_building_id);
CREATE INDEX idx_project_image_evidence_projects
    ON cross_source_project_image_evidence(
      run_id,left_source,left_source_project_id,right_source,right_source_project_id
    );
CREATE INDEX idx_candidate_image_evidence_assets
    ON candidate_image_evidence(
      run_id,left_source,left_source_asset_id,right_source,right_source_asset_id
    );
CREATE INDEX idx_building_candidates_buildings
    ON cross_source_building_candidates(
      run_id,left_source,left_source_building_id,right_source,right_source_building_id
    );
CREATE INDEX idx_e2_metrics_name
    ON e2_metrics(run_id,metric_name,phase);
CREATE INDEX idx_e2_validations_failed
    ON e2_validations(run_id,severity,validation_name) WHERE passed=0;
CREATE INDEX idx_smoke_items_entity
    ON smoke_manifest_items(run_id,manifest_name,entity_kind,source,source_entity_id);
"""


_TERMINAL_GUARD_TABLES: Final = tuple(
    table for table in TABLE_NAMES if table != "e2_runs"
)


def _terminal_guard_schema() -> str:
    statements: list[str] = []
    for table in _TERMINAL_GUARD_TABLES:
        statements.extend(
            (
                f"""CREATE TRIGGER {table}_terminal_insert_guard
                    BEFORE INSERT ON {table}
                    WHEN coalesce((SELECT status FROM e2_runs
                                   WHERE run_id=NEW.run_id),'')<>'building'
                    BEGIN
                      SELECT RAISE(ABORT, 'terminal E2 evidence is immutable');
                    END;""",
                f"""CREATE TRIGGER {table}_terminal_update_guard
                    BEFORE UPDATE ON {table}
                    WHEN coalesce((SELECT status FROM e2_runs
                                   WHERE run_id=OLD.run_id),'')<>'building'
                    BEGIN
                      SELECT RAISE(ABORT, 'terminal E2 evidence is immutable');
                    END;""",
                f"""CREATE TRIGGER {table}_terminal_delete_guard
                    BEFORE DELETE ON {table}
                    WHEN coalesce((SELECT status FROM e2_runs
                                   WHERE run_id=OLD.run_id),'')<>'building'
                    BEGIN
                      SELECT RAISE(ABORT, 'terminal E2 evidence is immutable');
                    END;""",
            )
        )
    return "\n".join(statements)


TERMINAL_GUARD_SCHEMA: Final = _terminal_guard_schema()
TRIGGER_NAMES: Final = (
    "e2_runs_single_run",
    "e2_runs_provenance_immutable",
    "e2_runs_status_transition",
    "e2_runs_terminal_immutable",
    "e2_runs_complete_requires_validations",
    "e2_runs_failed_requires_validation",
    "e2_runs_immutable_delete",
    *(
        f"{table}_terminal_{operation}_guard"
        for table in _TERMINAL_GUARD_TABLES
        for operation in ("insert", "update", "delete")
    ),
)

# This is an exact v1 evidence schema allow-list, not a migration map.  E2 v1
# artifacts may contain no policy tables, views, ad-hoc tables, or extra
# columns.  A schema change requires a new immutable E2 schema version.
EXPECTED_TABLE_COLUMNS: Final = {
    "e2_runs": (
        "run_id", "contract_version", "builder_version", "selection_mode",
        "sample_size", "sample_seed", "ordered_selection_manifest_sha256",
        "config_json", "status", "started_at", "completed_at", "error",
    ),
    "e2_inputs": (
        "run_id", "input_name", "source", "input_role", "file_path",
        "size_bytes", "sha256_before", "sha256_after", "application_id",
        "user_version", "schema_manifest_sha256", "recorded_at", "detail_json",
    ),
    "source_projects": (
        "run_id", "source", "source_project_id", "canonical_url", "slug",
        "global_id", "name", "normalized_name", "country", "region",
        "locality", "completion_year_min", "completion_year_max",
        "source_record_sha256", "metadata_json",
    ),
    "source_buildings": (
        "run_id", "source", "source_building_id", "canonical_url", "slug",
        "global_id", "name", "normalized_name", "country", "region",
        "locality", "completion_year_min", "completion_year_max",
        "source_record_sha256", "metadata_json",
    ),
    "source_project_buildings": (
        "run_id", "source", "source_project_id", "source_building_id",
        "membership_reason", "membership_ordinal", "source_record_sha256",
        "detail_json",
    ),
    "assets": (
        "run_id", "source", "source_asset_id", "e1_run_id",
        "fingerprint_status", "canonical_url", "fetch_url", "final_url",
        "raw_response_sha256", "normalized_pixel_sha256", "phash_hex",
        "original_width", "original_height", "normalized_width",
        "normalized_height", "source_record_sha256", "provenance_json",
        "error_kind", "error_message",
    ),
    "project_asset_occurrences": (
        "run_id", "source", "occurrence_id", "source_project_id",
        "raw_asset_key", "source_asset_id", "resolution_status", "role",
        "ordinal", "occurrence_url", "source_record_sha256", "detail_json",
    ),
    "project_assets": (
        "run_id", "source", "source_project_id", "source_asset_id",
        "occurrence_count", "roles_json", "first_ordinal",
        "relation_record_sha256",
    ),
    "building_assets": (
        "run_id", "source", "source_building_id", "source_asset_id",
        "project_count", "occurrence_count", "roles_json",
        "relation_record_sha256",
    ),
    "exact_pixel_clusters": (
        "run_id", "cluster_id", "normalized_pixel_sha256", "member_count",
        "source_count", "project_count", "building_count", "is_cross_source",
    ),
    "exact_pixel_cluster_members": (
        "run_id", "cluster_id", "source", "source_asset_id",
    ),
    "phash_nodes": (
        "run_id", "node_id", "phash_hex", "member_count", "source_count",
        "is_cross_source",
    ),
    "phash_node_members": (
        "run_id", "node_id", "source", "source_asset_id",
    ),
    "phash_candidates": (
        "run_id", "candidate_id", "left_node_id", "right_node_id",
        "candidate_scope", "shared_band_count", "recomputed_distance",
        "passed_threshold", "candidate_record_sha256", "detail_json",
    ),
    "phash_edges": (
        "run_id", "edge_id", "left_node_id", "right_node_id",
        "hamming_distance", "edge_scope", "candidate_id",
        "edge_record_sha256", "detail_json",
    ),
    "metadata_building_pairs": (
        "run_id", "metadata_pair_id", "left_source", "left_source_building_id",
        "right_source", "right_source_building_id", "blocker_version",
        "discovery_reason", "normalized_name_equal", "country_equal",
        "locality_equal", "year_overlap", "metadata_record_sha256",
        "evidence_json",
    ),
    "cross_source_project_image_evidence": (
        "run_id", "project_pair_id", "left_source", "left_source_project_id",
        "right_source", "right_source_project_id", "exact_asset_pair_count",
        "identical_phash_pair_count", "phash_le8_pair_count",
        "phash_9_16_pair_count", "min_phash_distance",
        "evidence_record_sha256", "evidence_json",
    ),
    "candidate_image_evidence": (
        "run_id", "evidence_id", "building_candidate_id", "project_pair_id",
        "left_source", "left_source_asset_id", "right_source",
        "right_source_asset_id", "evidence_kind", "exact_cluster_id",
        "phash_edge_id", "phash_distance", "direct_evidence",
        "evidence_record_sha256", "detail_json",
    ),
    "cross_source_building_candidates": (
        "run_id", "building_candidate_id", "left_source",
        "left_source_building_id", "right_source", "right_source_building_id",
        "metadata_pair_id", "exact_asset_pair_count",
        "identical_phash_pair_count", "phash_le8_pair_count",
        "phash_9_16_pair_count", "min_phash_distance",
        "discovery_basis_json", "candidate_record_sha256",
    ),
    "e2_metrics": (
        "run_id", "phase", "metric_name", "stratum_json", "value_integer",
        "value_real", "value_text", "recorded_at",
    ),
    "e2_validations": (
        "run_id", "validation_name", "severity", "passed", "expected",
        "actual", "detail_json", "recorded_at",
    ),
    "smoke_manifests": (
        "run_id", "manifest_name", "sample_size", "sample_seed",
        "selection_version", "ordered_manifest_sha256", "selection_scope_json",
        "created_at",
    ),
    "smoke_manifest_items": (
        "run_id", "manifest_name", "selection_rank", "entity_kind", "source",
        "source_entity_id", "stratum", "score_sha256", "item_record_sha256",
        "detail_json",
    ),
    "build_checkpoints": (
        "run_id", "phase", "cursor_json", "completed_rows", "phase_complete",
        "updated_at",
    ),
}


class SidecarSchemaError(RuntimeError):
    """Raised when a file is not the expected E2 evidence schema."""


class BuildLockError(RuntimeError):
    """Raised when the advisory E2 build lock cannot be acquired safely."""


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
    """Owned O_EXCL lockfile; release only removes the caller's token."""

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
        raise BuildLockError(f"invalid E2 lockfile; manual inspection required: {path}") from exc
    if not isinstance(payload, dict):
        raise BuildLockError(f"invalid E2 lockfile; manual inspection required: {path}")
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
        raise BuildLockError(f"invalid E2 lockfile; manual inspection required: {path}")
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
    """Acquire an O_EXCL lock, recovering only a dead same-host owner.

    A foreign-host, malformed, or live-process lock is never removed.  When a
    same-host PID is definitely absent, its immutable lockfile is renamed to a
    unique tombstone before retrying acquisition, avoiding an unlink race
    between concurrent recovery contenders.
    """

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
                    "E2 build lock is held: "
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
                raise BuildLockError(
                    f"E2 lock changed during stale recovery: {tombstone}"
                )
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
    forbidden = sorted(
        set(FORBIDDEN_POLICY_TABLE_NAMES) & (tables | views)
    )
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
        raise SidecarSchemaError(f"missing E2 tables: {', '.join(missing_tables)}")
    if forbidden:
        raise SidecarSchemaError(
            f"forbidden policy schema objects in E2 evidence DB: {', '.join(forbidden)}"
        )
    if unexpected_tables:
        raise SidecarSchemaError(
            f"unexpected E2 tables: {', '.join(unexpected_tables)}"
        )
    if views:
        raise SidecarSchemaError(f"E2 evidence DB must not contain views: {', '.join(sorted(views))}")
    if missing_triggers:
        raise SidecarSchemaError(f"missing E2 triggers: {', '.join(missing_triggers)}")
    if unexpected_triggers:
        raise SidecarSchemaError(
            f"unexpected E2 triggers: {', '.join(unexpected_triggers)}"
        )
    if missing_indexes:
        raise SidecarSchemaError(f"missing E2 indexes: {', '.join(missing_indexes)}")
    if unexpected_indexes:
        raise SidecarSchemaError(
            f"unexpected E2 indexes: {', '.join(unexpected_indexes)}"
        )
    if column_mismatches:
        raise SidecarSchemaError(
            "E2 table column contract mismatch: "
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
    """Create a new E2 sidecar in WAL mode without clobbering any file."""

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
    """Open and identify an E2 artifact.

    ``immutable`` defaults to true for read-only access.  Immutable access is
    refused while a WAL, SHM, or rollback journal exists so uncheckpointed
    pages can never be silently ignored.
    """

    target = Path(path).resolve()
    if not target.is_file():
        raise FileNotFoundError(f"E2 sidecar does not exist: {target}")
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
            rows = connection.execute("SELECT status FROM e2_runs").fetchall()
            if rows and any(str(row[0]) != "building" for row in rows):
                raise SidecarSchemaError("terminal E2 sidecar cannot be opened writable")
    except Exception:
        connection.close()
        raise
    return connection


def recover_sidecar(path: Path | str, *, switch_to_delete: bool = False) -> None:
    """Perform writable SQLite recovery and optionally remove WAL mode.

    The immediate transaction proves no live SQLite writer owns the database.
    No application row is changed.
    """

    target = Path(path).resolve()
    if not target.is_file():
        raise FileNotFoundError(f"E2 sidecar does not exist: {target}")
    connection = sqlite3.connect(target)
    try:
        _configure(connection, readonly=False)
        _assert_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("SELECT count(*) FROM e2_runs").fetchone()
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
    if sqlite_sidecar_paths(target):
        raise SidecarSchemaError(
            "SQLite sidecars remain after recovery: "
            + ", ".join(str(item) for item in sqlite_sidecar_paths(target))
        )
    connection = open_sidecar(target, readonly=True, immutable=True)
    try:
        rows = connection.execute("SELECT status FROM e2_runs").fetchall()
        if len(rows) != 1 or str(rows[0][0]) not in TERMINAL_STATUSES:
            raise SidecarSchemaError("immutable publication requires one terminal E2 run")
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
    """Set a terminal status, checkpoint WAL, and restore DELETE journal mode.

    The supplied connection is consumed and, by default, closed.  The schema
    triggers ensure ``complete`` has no failed error validation and
    ``failed_validation`` has at least one.
    """

    if status not in TERMINAL_STATUSES:
        raise ValueError("status must be 'complete' or 'failed_validation'")
    _assert_schema(connection)
    db_rows = connection.execute("PRAGMA database_list").fetchall()
    main_path = next(Path(str(row[2])).resolve() for row in db_rows if str(row[1]) == "main")
    try:
        row = connection.execute("SELECT run_id,status FROM e2_runs").fetchone()
        if row is None or str(row[1]) != "building":
            raise SidecarSchemaError("finalization requires exactly one building E2 run")
        connection.execute(
            """UPDATE e2_runs SET status=?,completed_at=?,error=? WHERE run_id=?""",
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
    """Run physical, relational, and evidence-contract checks read-only."""

    target = Path(path).resolve()
    connection = open_sidecar(target, readonly=True, immutable=immutable)
    try:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        integrity_check = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_key_violations = sum(
            1 for _ in connection.execute("PRAGMA foreign_key_check")
        )
        semantic_queries = (
            ("single_run_count_mismatch", "SELECT abs(count(*)-1) FROM e2_runs"),
            (
                "evidence_only_config_mismatch",
                """SELECT count(*) FROM e2_runs
                   WHERE coalesce(json_type(config_json,'$.representative_selection'),
                                  'missing')<>'false'
                      OR coalesce(json_type(config_json,'$.network_requests'),
                                  'missing') NOT IN ('integer','real')
                      OR json_extract(config_json,'$.network_requests')<>0
                      OR coalesce(json_type(config_json,'$.vision_requests'),
                                  'missing') NOT IN ('integer','real')
                      OR json_extract(config_json,'$.vision_requests')<>0""",
            ),
            (
                "evidence_only_metric_mismatch",
                """SELECT count(*) FROM e2_runs r
                   WHERE (SELECT count(*) FROM e2_metrics m
                          WHERE m.run_id=r.run_id
                            AND m.metric_name IN (
                              'network_requests','vision_requests','llm_requests'
                            ))<>3
                      OR EXISTS(
                        SELECT 1 FROM e2_metrics m
                        WHERE m.run_id=r.run_id
                          AND m.metric_name IN (
                            'network_requests','vision_requests','llm_requests'
                          )
                          AND (m.phase<>'validation' OR m.stratum_json<>'{}'
                               OR m.value_integer IS NOT 0
                               OR m.value_real IS NOT NULL
                               OR m.value_text IS NOT NULL)
                      )""",
            ),
            (
                "terminal_input_sha_mismatch",
                """SELECT count(*) FROM e2_inputs i JOIN e2_runs r USING(run_id)
                   WHERE r.status<>'building'
                     AND (i.sha256_after IS NULL OR i.sha256_after<>i.sha256_before)""",
            ),
            (
                "successful_asset_hash_mismatch",
                """SELECT count(*) FROM assets
                   WHERE fingerprint_status='success'
                     AND (raw_response_sha256 IS NULL
                          OR normalized_pixel_sha256 IS NULL OR phash_hex IS NULL)""",
            ),
            (
                "non_success_asset_hash_present",
                """SELECT count(*) FROM assets
                   WHERE fingerprint_status<>'success'
                     AND (normalized_pixel_sha256 IS NOT NULL OR phash_hex IS NOT NULL)""",
            ),
            (
                "exact_cluster_count_mismatch",
                """SELECT count(*) FROM exact_pixel_clusters c
                   WHERE c.member_count<>(SELECT count(*) FROM exact_pixel_cluster_members m
                                           WHERE m.run_id=c.run_id
                                             AND m.cluster_id=c.cluster_id)
                      OR c.source_count<>(SELECT count(DISTINCT source)
                                          FROM exact_pixel_cluster_members m
                                          WHERE m.run_id=c.run_id
                                            AND m.cluster_id=c.cluster_id)""",
            ),
            (
                "exact_cluster_hash_mismatch",
                """SELECT count(*)
                   FROM exact_pixel_cluster_members m
                   JOIN exact_pixel_clusters c USING(run_id,cluster_id)
                   JOIN assets a ON a.run_id=m.run_id AND a.source=m.source
                                AND a.source_asset_id=m.source_asset_id
                   WHERE a.normalized_pixel_sha256<>c.normalized_pixel_sha256""",
            ),
            (
                "phash_node_count_mismatch",
                """SELECT count(*) FROM phash_nodes n
                   WHERE n.member_count<>(SELECT count(*) FROM phash_node_members m
                                           WHERE m.run_id=n.run_id AND m.node_id=n.node_id)
                      OR n.source_count<>(SELECT count(DISTINCT source)
                                          FROM phash_node_members m
                                          WHERE m.run_id=n.run_id AND m.node_id=n.node_id)""",
            ),
            (
                "phash_node_hash_mismatch",
                """SELECT count(*)
                   FROM phash_node_members m
                   JOIN phash_nodes n USING(run_id,node_id)
                   JOIN assets a ON a.run_id=m.run_id AND a.source=m.source
                                AND a.source_asset_id=m.source_asset_id
                   WHERE a.phash_hex<>n.phash_hex""",
            ),
            (
                "complete_run_failed_error_validation",
                """SELECT count(*) FROM e2_runs r
                   WHERE r.status='complete' AND EXISTS(
                     SELECT 1 FROM e2_validations v WHERE v.run_id=r.run_id
                       AND v.severity='error' AND v.passed=0)""",
            ),
            (
                "failed_run_without_failed_error_validation",
                """SELECT count(*) FROM e2_runs r
                   WHERE r.status='failed_validation' AND NOT EXISTS(
                     SELECT 1 FROM e2_validations v WHERE v.run_id=r.run_id
                       AND v.severity='error' AND v.passed=0)""",
            ),
            (
                "smoke_manifest_count_mismatch",
                """SELECT count(*) FROM smoke_manifests m
                   WHERE m.sample_size<>(SELECT count(*) FROM smoke_manifest_items i
                                         WHERE i.run_id=m.run_id
                                           AND i.manifest_name=m.manifest_name)""",
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
        statuses = [str(row[0]) for row in connection.execute("SELECT status FROM e2_runs")]
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
