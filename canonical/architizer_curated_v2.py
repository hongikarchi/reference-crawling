"""Schema contract for the final source-specific Architizer curated v2 DB.

The v2 materializer deliberately extends, rather than replaces, the complete
curated-v1.3 SQLite contract.  Existing tables/views are rebuilt by the v1.3
pipeline from reconciled effective source rows.  The tables below preserve the
new reconciliation and structured A+Awards evidence without forcing product or
brand records into the project/firm corpus.
"""

from __future__ import annotations


SCHEMA_VERSION = "architizer-curated-schema-v2.0"
MATERIALIZER_VERSION = "architizer-curated-v2-materializer-v0.6"
MATERIALIZATION_POLICY_VERSION = "architizer-curated-v2-policy-v0.2"
MATERIALIZATION_SELECTION_VERSION = "architizer-curated-v2-selection-v1"
READY_VERSION = "architizer-curated-v2-ready-v1"


EXTENSION_DDL = r"""
PRAGMA foreign_keys=ON;
PRAGMA journal_mode=DELETE;
PRAGMA synchronous=FULL;
PRAGMA trusted_schema=OFF;

CREATE TABLE curated_v2_runs (
    materialization_id TEXT PRIMARY KEY,
    base_build_id TEXT NOT NULL REFERENCES build_runs(build_id),
    materializer_version TEXT NOT NULL,
    schema_version TEXT NOT NULL CHECK (schema_version='architizer-curated-schema-v2.0'),
    policy_version TEXT NOT NULL,
    selection_version TEXT NOT NULL,
    build_mode TEXT NOT NULL CHECK (build_mode IN ('N10','N100','full')),
    project_limit INTEGER,
    award_limit INTEGER,
    is_full_materialization INTEGER NOT NULL CHECK (is_full_materialization IN (0,1)),
    deterministic_timestamp TEXT NOT NULL,
    reconciliation_id TEXT NOT NULL,
    selected_project_count INTEGER NOT NULL CHECK (selected_project_count >= 0),
    selected_firm_count INTEGER NOT NULL CHECK (selected_firm_count >= 0),
    selected_award_count INTEGER NOT NULL CHECK (selected_award_count >= 0),
    validation_json TEXT NOT NULL CHECK (json_valid(validation_json)),
    CHECK (
        (build_mode='full' AND project_limit IS NULL AND award_limit IS NULL
         AND is_full_materialization=1)
        OR
        (build_mode IN ('N10','N100') AND project_limit IS NOT NULL
         AND award_limit IS NOT NULL AND is_full_materialization=0)
    )
) STRICT;

CREATE TABLE curated_v2_input_snapshots (
    materialization_id TEXT NOT NULL REFERENCES curated_v2_runs(materialization_id),
    input_role TEXT NOT NULL CHECK (
        input_role IN (
            'legacy_raw','curated_v1_3','reconciliation_plan',
            'reconciliation_report','reconciliation_ready','structured_awards_v2',
            'structured_awards_ready'
        )
    ),
    path_label TEXT NOT NULL,
    sha256_before TEXT NOT NULL,
    sha256_after TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    is_sqlite INTEGER NOT NULL CHECK (is_sqlite IN (0,1)),
    query_only INTEGER CHECK (query_only IN (0,1)),
    quick_check TEXT,
    integrity_check TEXT,
    foreign_key_violations INTEGER,
    lineage_json TEXT NOT NULL CHECK (json_valid(lineage_json)),
    PRIMARY KEY (materialization_id,input_role),
    CHECK (
        (is_sqlite=1 AND query_only=1 AND quick_check='ok'
         AND integrity_check='ok' AND foreign_key_violations=0)
        OR
        (is_sqlite=0 AND query_only IS NULL AND quick_check IS NULL
         AND integrity_check IS NULL AND foreign_key_violations IS NULL)
    )
) WITHOUT ROWID, STRICT;

CREATE TABLE v2_reconciliation_runs (
    reconciliation_id TEXT PRIMARY KEY,
    materialization_id TEXT NOT NULL REFERENCES curated_v2_runs(materialization_id),
    tool_version TEXT NOT NULL,
    plan_schema_version TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    baseline_schema_version TEXT NOT NULL,
    final_target_schema_version TEXT NOT NULL,
    artifact_kind TEXT NOT NULL,
    publication_eligibility TEXT NOT NULL,
    project_limit INTEGER,
    firm_limit INTEGER,
    selected_project_count INTEGER NOT NULL,
    selected_firm_count INTEGER NOT NULL,
    pending_target_count INTEGER NOT NULL,
    deterministic_cutoff TEXT NOT NULL,
    validation_json TEXT NOT NULL CHECK (json_valid(validation_json))
) STRICT;

CREATE TABLE v2_reconciliation_input_snapshots (
    reconciliation_id TEXT NOT NULL REFERENCES v2_reconciliation_runs(reconciliation_id),
    input_role TEXT NOT NULL,
    path_label TEXT NOT NULL,
    sha256_before TEXT NOT NULL,
    sha256_after TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    query_only INTEGER NOT NULL,
    quick_check TEXT NOT NULL,
    foreign_key_violations INTEGER NOT NULL,
    lineage_json TEXT NOT NULL CHECK (json_valid(lineage_json)),
    PRIMARY KEY (reconciliation_id,input_role)
) WITHOUT ROWID, STRICT;

CREATE TABLE v2_reconciliation_trusted_manifest (
    reconciliation_id TEXT PRIMARY KEY REFERENCES v2_reconciliation_runs(reconciliation_id),
    manifest_version TEXT NOT NULL,
    path_label TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    manifest_json TEXT NOT NULL CHECK (json_valid(manifest_json))
) WITHOUT ROWID, STRICT;

CREATE TABLE v2_reconciliation_baseline_contract (
    object_type TEXT NOT NULL,
    object_name TEXT NOT NULL,
    sql_sha256 TEXT NOT NULL,
    sql_text TEXT NOT NULL,
    columns_json TEXT NOT NULL CHECK (json_valid(columns_json)),
    PRIMARY KEY (object_type,object_name)
) WITHOUT ROWID, STRICT;

CREATE TABLE v2_reconciliation_entities (
    entity_key TEXT PRIMARY KEY,
    reconciliation_id TEXT NOT NULL REFERENCES v2_reconciliation_runs(reconciliation_id),
    entity_type TEXT NOT NULL CHECK (entity_type IN ('project','firm')),
    source_url TEXT NOT NULL UNIQUE,
    source_slug TEXT NOT NULL,
    origin TEXT NOT NULL,
    baseline_present INTEGER NOT NULL CHECK (baseline_present IN (0,1)),
    baseline_identity_key TEXT,
    baseline_acceptance_status TEXT,
    last_good_version_id INTEGER,
    snapshot_sha256 TEXT,
    parser_version TEXT,
    metadata_version TEXT,
    identity_status TEXT NOT NULL,
    inclusion_status TEXT NOT NULL CHECK (inclusion_status IN ('included','qa_only')),
    identity_evidence_json TEXT NOT NULL CHECK (json_valid(identity_evidence_json)),
    effective_fields_json TEXT NOT NULL CHECK (json_valid(effective_fields_json))
) STRICT;

CREATE TABLE v2_reconciliation_entity_aliases (
    entity_key TEXT NOT NULL REFERENCES v2_reconciliation_entities(entity_key),
    target_url TEXT NOT NULL,
    final_url TEXT,
    metadata_version_id INTEGER,
    alias_kind TEXT NOT NULL,
    evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json)),
    PRIMARY KEY (entity_key,target_url)
) WITHOUT ROWID, STRICT;

CREATE TABLE v2_reconciliation_target_reasons (
    target_url TEXT NOT NULL,
    entity_key TEXT REFERENCES v2_reconciliation_entities(entity_key),
    target_entity_type TEXT NOT NULL CHECK (target_entity_type IN ('project','firm')),
    target_status TEXT NOT NULL,
    target_retryable INTEGER NOT NULL CHECK (target_retryable IN (0,1)),
    last_good_version_id INTEGER,
    reason TEXT NOT NULL,
    discovery_source TEXT NOT NULL,
    priority INTEGER NOT NULL,
    source_lastmod TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    input_lineage_json TEXT NOT NULL CHECK (json_valid(input_lineage_json)),
    PRIMARY KEY (target_url,reason,discovery_source)
) WITHOUT ROWID, STRICT;

CREATE TABLE v2_reconciliation_field_candidates (
    candidate_id TEXT PRIMARY KEY,
    entity_key TEXT NOT NULL REFERENCES v2_reconciliation_entities(entity_key),
    field_name TEXT NOT NULL,
    source_role TEXT NOT NULL,
    value_json TEXT CHECK (value_json IS NULL OR json_valid(value_json)),
    status TEXT NOT NULL,
    quality TEXT NOT NULL,
    metadata_version_id INTEGER,
    source_locator_json TEXT NOT NULL CHECK (json_valid(source_locator_json)),
    UNIQUE (entity_key,field_name,source_role)
) STRICT;

CREATE TABLE v2_reconciliation_field_decisions (
    entity_key TEXT NOT NULL REFERENCES v2_reconciliation_entities(entity_key),
    field_name TEXT NOT NULL,
    effective_value_json TEXT CHECK (effective_value_json IS NULL OR json_valid(effective_value_json)),
    decision_kind TEXT NOT NULL,
    decision_status TEXT NOT NULL,
    selected_candidate_id TEXT REFERENCES v2_reconciliation_field_candidates(candidate_id),
    baseline_candidate_id TEXT REFERENCES v2_reconciliation_field_candidates(candidate_id),
    recrawl_candidate_id TEXT REFERENCES v2_reconciliation_field_candidates(candidate_id),
    rule_id TEXT NOT NULL,
    PRIMARY KEY (entity_key,field_name)
) WITHOUT ROWID, STRICT;

CREATE TABLE v2_reconciliation_field_lineage (
    entity_key TEXT NOT NULL,
    field_name TEXT NOT NULL,
    candidate_id TEXT NOT NULL REFERENCES v2_reconciliation_field_candidates(candidate_id),
    lineage_role TEXT NOT NULL,
    PRIMARY KEY (entity_key,field_name,candidate_id),
    FOREIGN KEY (entity_key,field_name)
        REFERENCES v2_reconciliation_field_decisions(entity_key,field_name)
) WITHOUT ROWID, STRICT;

CREATE TABLE v2_reconciliation_field_conflicts (
    conflict_id TEXT PRIMARY KEY,
    entity_key TEXT NOT NULL REFERENCES v2_reconciliation_entities(entity_key),
    field_name TEXT NOT NULL,
    conflict_kind TEXT NOT NULL,
    baseline_value_json TEXT CHECK (baseline_value_json IS NULL OR json_valid(baseline_value_json)),
    recrawl_value_json TEXT CHECK (recrawl_value_json IS NULL OR json_valid(recrawl_value_json)),
    disposition TEXT NOT NULL,
    evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json)),
    rule_id TEXT NOT NULL
) STRICT;

CREATE TABLE v2_reconciliation_qa_issues (
    qa_issue_id TEXT PRIMARY KEY,
    entity_key TEXT NOT NULL REFERENCES v2_reconciliation_entities(entity_key),
    issue_code TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL,
    details_json TEXT NOT NULL CHECK (json_valid(details_json)),
    UNIQUE (entity_key,issue_code)
) STRICT;

CREATE TABLE v2_reconciliation_metrics (
    metric_name TEXT PRIMARY KEY,
    metric_value_json TEXT NOT NULL CHECK (json_valid(metric_value_json))
) STRICT;

CREATE TABLE v2_structured_award_lineage (
    id INTEGER PRIMARY KEY CHECK (id=1),
    source_input_lineage_json TEXT NOT NULL CHECK (json_valid(source_input_lineage_json)),
    source_build_manifest_json TEXT NOT NULL CHECK (json_valid(source_build_manifest_json)),
    source_schema_meta_json TEXT NOT NULL CHECK (json_valid(source_schema_meta_json))
) STRICT;

CREATE TABLE v2_structured_award_pages (
    source_page_id INTEGER PRIMARY KEY,
    page_kind TEXT NOT NULL,
    award_year INTEGER NOT NULL,
    award_track TEXT,
    requested_url TEXT NOT NULL,
    final_url TEXT NOT NULL,
    final_url_policy TEXT NOT NULL,
    http_status INTEGER NOT NULL,
    content_type TEXT NOT NULL,
    response_bytes INTEGER NOT NULL,
    snapshot_content_sha256 TEXT NOT NULL,
    snapshot_gzip_sha256 TEXT NOT NULL,
    snapshot_gzip_path TEXT NOT NULL,
    snapshot_gzip_bytes INTEGER NOT NULL,
    parser_version TEXT NOT NULL,
    parsed_at TEXT NOT NULL,
    parse_status TEXT NOT NULL,
    source_record_count INTEGER NOT NULL,
    source_selected_record_count INTEGER NOT NULL,
    materialized_record_count INTEGER NOT NULL,
    status_counts_json TEXT NOT NULL CHECK (json_valid(status_counts_json)),
    duplicate_attribution_ids_json TEXT NOT NULL CHECK (json_valid(duplicate_attribution_ids_json))
) STRICT;

CREATE TABLE v2_structured_award_attributions (
    source_attribution_id INTEGER PRIMARY KEY,
    source_page_id INTEGER NOT NULL REFERENCES v2_structured_award_pages(source_page_id),
    materialization_order INTEGER NOT NULL UNIQUE,
    source_selection_order INTEGER NOT NULL UNIQUE,
    source_group_ordinal INTEGER NOT NULL,
    source_card_ordinal INTEGER NOT NULL,
    award_year INTEGER NOT NULL,
    award_track TEXT NOT NULL,
    attribution_pk INTEGER,
    attribution_global_id TEXT,
    category_raw TEXT,
    category_path_json TEXT NOT NULL CHECK (json_valid(category_path_json)),
    subject_kind TEXT,
    subject_slug TEXT,
    subject_name TEXT,
    subject_url TEXT,
    description_raw TEXT,
    image_url_resolved TEXT,
    parse_status TEXT NOT NULL,
    missing_json TEXT NOT NULL CHECK (json_valid(missing_json)),
    conflicts_json TEXT NOT NULL CHECK (json_valid(conflicts_json)),
    warnings_json TEXT NOT NULL CHECK (json_valid(warnings_json)),
    raw_attributes_json TEXT NOT NULL CHECK (json_valid(raw_attributes_json)),
    dom_values_json TEXT NOT NULL CHECK (json_valid(dom_values_json)),
    source_url TEXT NOT NULL,
    UNIQUE (source_page_id,source_group_ordinal,source_card_ordinal)
) STRICT;

CREATE TABLE v2_structured_award_tiers (
    source_attribution_id INTEGER NOT NULL REFERENCES v2_structured_award_attributions(source_attribution_id),
    position INTEGER NOT NULL,
    normalized_tier TEXT,
    raw_attribute_label TEXT,
    raw_dom_label TEXT,
    parse_status TEXT NOT NULL,
    PRIMARY KEY (source_attribution_id,position)
) WITHOUT ROWID, STRICT;

CREATE TABLE v2_structured_award_companies (
    source_attribution_id INTEGER NOT NULL REFERENCES v2_structured_award_attributions(source_attribution_id),
    position INTEGER NOT NULL,
    entity_kind TEXT,
    slug TEXT,
    name TEXT,
    url TEXT,
    attribute_observation_json TEXT NOT NULL CHECK (json_valid(attribute_observation_json)),
    dom_observation_json TEXT NOT NULL CHECK (json_valid(dom_observation_json)),
    parse_status TEXT NOT NULL,
    PRIMARY KEY (source_attribution_id,position)
) WITHOUT ROWID, STRICT;

CREATE TABLE v2_structured_award_projection_policy (
    entity_kind TEXT PRIMARY KEY,
    preserve_in_source_corpus INTEGER NOT NULL,
    corpus_role TEXT NOT NULL,
    project_firm_curated_projection TEXT NOT NULL,
    policy_version TEXT NOT NULL
) STRICT;

CREATE TABLE v2_structured_award_entity_links (
    source_attribution_id INTEGER NOT NULL REFERENCES v2_structured_award_attributions(source_attribution_id),
    relation_role TEXT NOT NULL CHECK (relation_role IN ('subject','company')),
    position INTEGER NOT NULL,
    entity_kind TEXT,
    raw_slug TEXT,
    raw_url TEXT,
    resolved_source_project_id INTEGER REFERENCES source_projects(source_project_id),
    resolved_source_firm_slug TEXT REFERENCES source_firms(source_firm_slug),
    link_status TEXT NOT NULL CHECK (
        link_status IN ('resolved','unresolved','not_in_materialization_subset','source_only','missing_identity')
    ),
    evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json)),
    PRIMARY KEY (source_attribution_id,relation_role,position),
    CHECK (resolved_source_project_id IS NULL OR resolved_source_firm_slug IS NULL)
) WITHOUT ROWID, STRICT;

CREATE TABLE v2_structured_award_base_projections (
    source_attribution_id INTEGER NOT NULL REFERENCES v2_structured_award_attributions(source_attribution_id),
    tier_position INTEGER NOT NULL,
    projection_key TEXT,
    projected_source_award_id INTEGER REFERENCES source_awards(source_award_id),
    projection_status TEXT NOT NULL CHECK (
        projection_status IN (
            'projected','not_in_materialization_subset','source_only',
            'conflict_or_partial','missing_identity'
        )
    ),
    policy_version TEXT NOT NULL,
    evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json)),
    PRIMARY KEY (source_attribution_id,tier_position),
    UNIQUE (projection_key)
) WITHOUT ROWID, STRICT;

CREATE TABLE curated_v2_metrics (
    metric_name TEXT PRIMARY KEY,
    metric_value_json TEXT NOT NULL CHECK (json_valid(metric_value_json))
) STRICT;

CREATE INDEX idx_v2_reconciliation_entities_type
ON v2_reconciliation_entities(entity_type,inclusion_status,source_slug);
CREATE INDEX idx_v2_reconciliation_conflicts
ON v2_reconciliation_field_conflicts(conflict_kind,field_name);
CREATE INDEX idx_v2_reconciliation_target_reasons
ON v2_reconciliation_target_reasons(reason,target_entity_type,target_status);
CREATE INDEX idx_v2_awards_track
ON v2_structured_award_attributions(award_year,award_track,attribution_pk);
CREATE INDEX idx_v2_awards_subject
ON v2_structured_award_attributions(subject_kind,subject_slug);
CREATE INDEX idx_v2_award_links
ON v2_structured_award_entity_links(entity_kind,raw_slug,link_status);
CREATE INDEX idx_v2_award_base_projection
ON v2_structured_award_base_projections(projection_status,projected_source_award_id);

CREATE VIEW v_curated_v2_project_reconciliation AS
SELECT
    p.*,
    e.entity_key AS reconciliation_entity_key,
    e.origin AS reconciliation_origin,
    e.last_good_version_id,
    e.snapshot_sha256,
    e.parser_version AS reconciliation_parser_version,
    e.metadata_version,
    e.identity_status AS reconciliation_identity_status
FROM source_projects p
LEFT JOIN v2_reconciliation_entities e
  ON e.entity_type='project' AND e.source_url=p.source_url;

CREATE VIEW v_curated_v2_firm_reconciliation AS
SELECT
    f.*,
    e.entity_key AS reconciliation_entity_key,
    e.origin AS reconciliation_origin,
    e.last_good_version_id,
    e.snapshot_sha256,
    e.parser_version AS reconciliation_parser_version,
    e.metadata_version,
    e.identity_status AS reconciliation_identity_status
FROM source_firms f
LEFT JOIN v2_reconciliation_entities e
  ON e.entity_type='firm' AND e.source_slug=f.source_firm_slug;

CREATE VIEW v_curated_v2_structured_awards AS
SELECT
    a.*,
    l.link_status AS subject_link_status,
    l.resolved_source_project_id,
    l.resolved_source_firm_slug
FROM v2_structured_award_attributions a
LEFT JOIN v2_structured_award_entity_links l
  ON l.source_attribution_id=a.source_attribution_id
 AND l.relation_role='subject' AND l.position=0;
"""


__all__ = [
    "EXTENSION_DDL",
    "MATERIALIZATION_POLICY_VERSION",
    "MATERIALIZATION_SELECTION_VERSION",
    "MATERIALIZER_VERSION",
    "READY_VERSION",
    "SCHEMA_VERSION",
]
