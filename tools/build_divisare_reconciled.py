"""Build the immutable Divisare metadata v2.2 reconciliation artifact.

The builder copies the complete metadata-v2.1 SQLite database, then adds a
read-only reconciliation overlay from the completed HTML-recrawl sidecar. It
does not fetch, hash, classify, or otherwise process images.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from collections import defaultdict
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from canonical.divisare_curated import (  # noqa: E402
    clean_scalar,
    normalize_identity_text,
)
from canonical.divisare_reconciliation import (  # noqa: E402
    METADATA_VERSION,
    POLICY_VERSION,
    SCHEMA_VERSION,
    parse_area_evidence,
    resolve_area,
    resolve_city,
    resolve_country,
    resolve_description,
    resolve_name,
    resolve_year,
)
from tools.build_divisare_curated_v2 import (  # noqa: E402
    exclusive_build_lock,
    file_sha256,
    json_dumps,
    open_readonly,
    publish_no_clobber,
)


BUILDER_VERSION = "divisare-metadata-reconciliation-builder-v1.1"
EXPECTED_PARENT_SCHEMA = 4
EXPECTED_PARENT_METADATA_VERSION = "divisare-metadata-v2.1"
EXPECTED_RECRAWL_SCHEMA = 1
EXPECTED_PARSER_VERSION = "divisare-html-metadata-v2.3"
PARTIAL_DECISION_SCHEMA_VERSION = 1
DEFAULT_PARTIAL_DECISIONS = (
    ROOT / "canonical" / "divisare_partial_text_decisions_v1.json"
)

AUTO_ACCEPTED_AREA_EVIDENCE_STATUSES = (
    "accepted",
    "accepted_converted",
    "accepted_dual_verified",
    "accepted_implicit_sqm",
    "accepted_reparsed",
    "computed_multiplier",
)
SAFE_AREA_RESIDUAL_QUALIFIER_TOKENS = (
    "housing_units_annotation",
    "volume_dimensions_annotation",
    "approximately",
    "approx",
    "surface",
    "circa",
    "gross",
    "house",
    "built",
    "area",
    "gfa",
    "app",
)

PRESERVED_TABLES = (
    "source_articles",
    "source_architects",
    "source_albums",
    "source_album_memberships",
    "source_tags",
    "article_tags",
    "attribute_claims",
    "claim_evidence_v2",
    "article_match_reviews_v2",
    "active_building_membership_v2",
    "building_attributes_v2",
    "building_facets_v2",
    "article_image_occurrences",
    "image_urls",
    "image_assets",
    "image_hashes",
    "image_classifications",
    "building_images_materialized_v2",
)


def _sql_literal(value: str) -> str:
    return "'%s'" % value.replace("'", "''")


def _sql_remove_literals(expression: str, values: Sequence[str]) -> str:
    for value in values:
        expression = "REPLACE(%s,%s,'')" % (expression, _sql_literal(value))
    return expression


_AREA_RESIDUE_JSON = (
    "COALESCE(json_extract(area_evidence_json,'$.details.residue'),'')"
)
_AREA_QUALIFIER_JSON = (
    "LOWER(COALESCE(json_extract(area_evidence_json,'$.details.qualifier'),''))"
)
_AREA_QUALIFIER_UNKNOWN_SQL = _sql_remove_literals(
    _AREA_QUALIFIER_JSON,
    SAFE_AREA_RESIDUAL_QUALIFIER_TOKENS + (",", " ", "\t", "\r", "\n"),
)
_AREA_RESIDUE_NON_PUNCTUATION_SQL = _sql_remove_literals(
    _AREA_RESIDUE_JSON,
    (
        " ", "\t", "\r", "\n", "(", ")", "[", "]", "{", "}",
        ".", ",", ";", ":", "/", "|", "\\", "~", "+", "-",
        "\u00b1", "\u2013", "\u2014", "\u2248",
    ),
)
_AUTO_ACCEPTED_AREA_STATUS_SQL = ",".join(
    _sql_literal(status) for status in AUTO_ACCEPTED_AREA_EVIDENCE_STATUSES
)
AUTO_AREA_RESIDUAL_UNKNOWN_SQL = """
    SELECT COUNT(*)
    FROM article_metadata_resolution_v2_2
    WHERE area_source='recrawl'
      AND (
        area_evidence_status NOT IN (%s)
        OR COALESCE(json_extract(area_evidence_json,'$.needs_review'),0)<>0
        OR COALESCE(json_extract(area_evidence_json,'$.details.reason'),'')<>''
        OR json_type(area_evidence_json,'$.details.scope') IS NOT NULL
        OR (
          TRIM(%s)<>''
          AND (
            (
              TRIM(%s)=''
              AND TRIM(%s)<>''
            )
            OR (
              TRIM(%s)<>''
              AND TRIM(%s)<>''
            )
          )
        )
      )
""" % (
    _AUTO_ACCEPTED_AREA_STATUS_SQL,
    _AREA_RESIDUE_JSON,
    _AREA_QUALIFIER_JSON,
    _AREA_RESIDUE_NON_PUNCTUATION_SQL,
    _AREA_QUALIFIER_JSON,
    _AREA_QUALIFIER_UNKNOWN_SQL,
)


SCHEMA_SQL = """
CREATE TABLE metadata_reconciliation_lineage_v2_2 (
    lineage_id                    INTEGER PRIMARY KEY CHECK(lineage_id=1),
    parent_db_path                TEXT NOT NULL,
    parent_sha256                 TEXT NOT NULL CHECK(length(parent_sha256)=64),
    parent_byte_size              INTEGER NOT NULL,
    parent_schema_version         INTEGER NOT NULL,
    parent_metadata_version       TEXT NOT NULL,
    recrawl_db_path               TEXT NOT NULL,
    recrawl_sha256                TEXT NOT NULL CHECK(length(recrawl_sha256)=64),
    recrawl_byte_size             INTEGER NOT NULL,
    recrawl_schema_version        INTEGER NOT NULL,
    recrawl_parent_sha256         TEXT NOT NULL CHECK(length(recrawl_parent_sha256)=64),
    crawler_version               TEXT NOT NULL,
    parser_version                TEXT NOT NULL,
    partial_decision_version      TEXT NOT NULL,
    partial_decision_file_path    TEXT NOT NULL,
    partial_decision_file_sha256  TEXT NOT NULL CHECK(length(partial_decision_file_sha256)=64),
    snapshot_root                 TEXT NOT NULL,
    builder_version               TEXT NOT NULL,
    reconciliation_policy_version TEXT NOT NULL,
    metadata_version              TEXT NOT NULL,
    schema_version                INTEGER NOT NULL,
    preserved_counts_json         TEXT NOT NULL CHECK(json_valid(preserved_counts_json)),
    scope_json                    TEXT NOT NULL CHECK(json_valid(scope_json)),
    reconciled_at                 TEXT NOT NULL
);

CREATE TABLE article_recrawl_evidence_v2_2 (
    article_id                    INTEGER PRIMARY KEY
        REFERENCES source_articles(article_id),
    source_url                    TEXT NOT NULL,
    priority                      INTEGER NOT NULL,
    reasons_json                  TEXT NOT NULL CHECK(json_valid(reasons_json)),
    fetch_status                  TEXT NOT NULL,
    parse_status                  TEXT NOT NULL,
    attempt_count                 INTEGER NOT NULL,
    http_status                   INTEGER,
    final_url                     TEXT,
    content_type                  TEXT,
    metadata_id                   INTEGER,
    snapshot_id                   INTEGER,
    html_sha256                   TEXT CHECK(html_sha256 IS NULL OR length(html_sha256)=64),
    snapshot_path                 TEXT,
    html_byte_size                INTEGER,
    response_headers_json         TEXT CHECK(
        response_headers_json IS NULL OR json_valid(response_headers_json)
    ),
    recrawl_name                  TEXT,
    recrawl_abstract              TEXT,
    recrawl_location_country      TEXT,
    recrawl_location_city         TEXT,
    recrawl_project_year          INTEGER,
    recrawl_area_sqm              REAL,
    recrawl_area_raw              TEXT,
    description_prose             TEXT,
    description_quality           TEXT,
    explicit_article_kind         TEXT,
    explicit_article_kind_raw     TEXT,
    parser_version                TEXT,
    details_json                  TEXT CHECK(details_json IS NULL OR json_valid(details_json)),
    fetched_at                    TEXT,
    parsed_at                     TEXT,
    updated_at                    TEXT NOT NULL,
    last_error                    TEXT
);

CREATE TABLE article_partial_text_decisions_v2_2 (
    article_id                    INTEGER PRIMARY KEY
        REFERENCES source_articles(article_id),
    parser_version                TEXT NOT NULL,
    prose_sha256                  TEXT NOT NULL CHECK(length(prose_sha256)=64),
    decision                      TEXT NOT NULL CHECK(decision IN ('accept','reject','review')),
    reason_code                   TEXT NOT NULL,
    note                          TEXT NOT NULL,
    decided_by                    TEXT NOT NULL,
    decided_at                    TEXT NOT NULL,
    decision_policy_version       TEXT NOT NULL,
    hash_guard_matched            INTEGER NOT NULL CHECK(hash_guard_matched IN (0,1))
);

CREATE TABLE article_metadata_resolution_v2_2 (
    article_id                    INTEGER PRIMARY KEY
        REFERENCES source_articles(article_id),
    availability_status           TEXT NOT NULL,
    resolved_name                 TEXT,
    resolved_name_normalized      TEXT,
    resolved_abstract             TEXT,
    location_country              TEXT,
    location_city                 TEXT,
    project_year                  INTEGER,
    area_sqm                      REAL,
    area_candidate_sqm            REAL,
    area_evidence_status          TEXT NOT NULL,
    area_unit_kind                TEXT NOT NULL,
    area_confidence               REAL NOT NULL CHECK(area_confidence BETWEEN 0 AND 1),
    parent_description_text_id    INTEGER REFERENCES article_text_versions(text_id),
    name_source                   TEXT NOT NULL,
    name_status                   TEXT NOT NULL,
    abstract_source               TEXT NOT NULL,
    country_source                TEXT NOT NULL,
    country_status                TEXT NOT NULL,
    city_source                   TEXT NOT NULL,
    city_status                   TEXT NOT NULL,
    year_source                   TEXT NOT NULL,
    year_status                   TEXT NOT NULL,
    area_source                   TEXT NOT NULL,
    area_status                   TEXT NOT NULL,
    description_source            TEXT NOT NULL,
    description_status            TEXT NOT NULL,
    description_publishable       INTEGER NOT NULL CHECK(description_publishable IN (0,1)),
    area_evidence_json            TEXT NOT NULL CHECK(json_valid(area_evidence_json)),
    field_sources_json            TEXT NOT NULL CHECK(json_valid(field_sources_json)),
    field_conflicts_json          TEXT NOT NULL CHECK(json_valid(field_conflicts_json)),
    review_reasons_json           TEXT NOT NULL CHECK(json_valid(review_reasons_json)),
    metadata_needs_review         INTEGER NOT NULL CHECK(metadata_needs_review IN (0,1)),
    reconciliation_status         TEXT NOT NULL,
    policy_version                TEXT NOT NULL,
    reconciled_at                 TEXT NOT NULL
);

CREATE TABLE building_core_reconciled_v2_2 (
    building_id                   TEXT PRIMARY KEY REFERENCES buildings(building_id),
    is_active                     INTEGER NOT NULL CHECK(is_active IN (0,1)),
    redirect_to                   TEXT REFERENCES buildings(building_id),
    article_count                 INTEGER NOT NULL,
    primary_article_id            INTEGER REFERENCES source_articles(article_id),
    name                          TEXT,
    name_normalized               TEXT,
    location_country              TEXT,
    location_city                 TEXT,
    location_resolution_method    TEXT NOT NULL,
    location_confidence           REAL NOT NULL CHECK(location_confidence BETWEEN 0 AND 1),
    project_year                  INTEGER,
    year_kind                     TEXT NOT NULL,
    area_sqm                      REAL,
    field_sources_json            TEXT NOT NULL CHECK(json_valid(field_sources_json)),
    core_conflicts_json           TEXT NOT NULL CHECK(json_valid(core_conflicts_json)),
    reconciliation_conflicts_json TEXT NOT NULL CHECK(json_valid(reconciliation_conflicts_json)),
    review_reasons_json           TEXT NOT NULL CHECK(json_valid(review_reasons_json)),
    metadata_needs_review         INTEGER NOT NULL CHECK(metadata_needs_review IN (0,1)),
    reconciliation_status         TEXT NOT NULL,
    resolution_version            TEXT NOT NULL,
    resolved_at                   TEXT NOT NULL
);

CREATE TABLE metadata_reconciliation_metrics_v2_2 (
    metric                        TEXT PRIMARY KEY,
    value_json                    TEXT NOT NULL CHECK(json_valid(value_json))
);

CREATE TABLE metadata_reconciliation_validation_v2_2 (
    check_name                    TEXT PRIMARY KEY,
    passed                        INTEGER NOT NULL CHECK(passed IN (0,1)),
    actual_json                   TEXT NOT NULL CHECK(json_valid(actual_json)),
    expected_json                 TEXT NOT NULL CHECK(json_valid(expected_json)),
    checked_at                    TEXT NOT NULL
);

CREATE INDEX idx_recrawl_evidence_status_v2_2
ON article_recrawl_evidence_v2_2(fetch_status,parse_status,article_id);
CREATE INDEX idx_article_resolution_review_v2_2
ON article_metadata_resolution_v2_2(metadata_needs_review,reconciliation_status,article_id);
CREATE INDEX idx_article_resolution_location_v2_2
ON article_metadata_resolution_v2_2(location_country,location_city);
CREATE INDEX idx_building_core_location_v2_2
ON building_core_reconciled_v2_2(location_country,location_city);
CREATE INDEX idx_building_core_review_v2_2
ON building_core_reconciled_v2_2(metadata_needs_review,reconciliation_status);
"""


VIEWS_SQL = """
CREATE VIEW v_article_metadata_reconciled_v2_2 AS
SELECT
    r.article_id,
    p.source_url,
    r.availability_status,
    r.resolved_name AS name,
    r.resolved_name_normalized AS name_normalized,
    r.resolved_abstract AS abstract,
    r.location_country,
    r.location_city,
    r.project_year,
    r.area_sqm,
    r.area_candidate_sqm,
    r.area_evidence_status,
    r.area_unit_kind,
    r.area_confidence,
    CASE
      WHEN r.description_publishable=0 THEN NULL
      WHEN r.description_source='recrawl' THEN e.description_prose
      WHEN r.description_source='parent' THEN parent_text.text
      ELSE NULL
    END AS description,
    CASE
      WHEN r.description_source='recrawl_candidate' THEN e.description_prose
      ELSE NULL
    END AS description_candidate,
    parent_text.text AS historical_parent_description,
    CASE
      WHEN r.description_source='recrawl' THEN e.description_quality
      WHEN r.description_source='parent' THEN p.description_quality
      WHEN r.description_status='source_has_no_prose' THEN 'source_has_no_prose'
      ELSE 'unresolved'
    END AS description_quality,
    e.fetch_status,
    e.parse_status,
    e.http_status,
    e.snapshot_id,
    e.html_sha256,
    e.snapshot_path,
    e.parser_version,
    r.name_source,
    r.name_status,
    r.abstract_source,
    r.country_source,
    r.country_status,
    r.city_source,
    r.city_status,
    r.year_source,
    r.year_status,
    r.area_source,
    r.area_status,
    r.description_source,
    r.description_status,
    r.description_publishable,
    r.area_evidence_json,
    r.field_sources_json,
    r.field_conflicts_json,
    r.review_reasons_json,
    r.metadata_needs_review,
    r.reconciliation_status,
    r.policy_version
FROM article_metadata_resolution_v2_2 r
JOIN source_articles p ON p.article_id=r.article_id
JOIN article_recrawl_evidence_v2_2 e ON e.article_id=r.article_id
LEFT JOIN article_text_versions parent_text
  ON parent_text.text_id=r.parent_description_text_id;

CREATE VIEW v_divisare_metadata_review_v2_2 AS
SELECT
    a.article_id,
    a.source_url,
    a.name,
    a.location_country,
    a.location_city,
    a.project_year,
    a.area_sqm,
    a.availability_status,
    a.fetch_status,
    a.parse_status,
    a.name_status,
    a.country_status,
    a.city_status,
    a.year_status,
    a.area_status,
    a.description_status,
    a.description_candidate,
    a.area_evidence_json,
    a.field_conflicts_json,
    a.review_reasons_json
FROM v_article_metadata_reconciled_v2_2 a
WHERE a.metadata_needs_review=1;

CREATE VIEW v_divisare_recrawl_status_v2_2 AS
SELECT
    e.article_id,
    e.source_url,
    e.fetch_status,
    e.parse_status,
    e.http_status,
    e.final_url,
    e.snapshot_id,
    e.html_sha256,
    e.snapshot_path,
    e.description_quality,
    e.recrawl_area_raw,
    e.recrawl_area_sqm,
    r.area_sqm AS resolved_area_sqm,
    r.area_candidate_sqm,
    r.area_evidence_status,
    r.area_unit_kind,
    r.area_confidence,
    r.area_status,
    r.description_status,
    r.metadata_needs_review,
    r.review_reasons_json
FROM article_recrawl_evidence_v2_2 e
JOIN article_metadata_resolution_v2_2 r ON r.article_id=e.article_id;

CREATE VIEW v_metadata_d2_review_v2_2 AS
SELECT
    d.*,
    a.name AS name_a,
    b.name AS name_b,
    a.location_country AS country_a,
    b.location_country AS country_b,
    a.location_city AS city_a,
    b.location_city AS city_b,
    a.project_year AS year_a,
    b.project_year AS year_b,
    a.area_sqm AS area_sqm_a,
    b.area_sqm AS area_sqm_b,
    a.description_status AS description_status_a,
    b.description_status AS description_status_b,
    a.html_sha256 AS html_sha256_a,
    b.html_sha256 AS html_sha256_b
FROM article_match_reviews_v2 d
JOIN v_article_metadata_reconciled_v2_2 a ON a.article_id=d.article_id_a
JOIN v_article_metadata_reconciled_v2_2 b ON b.article_id=d.article_id_b;

CREATE VIEW v_divisare_buildings_export_v2_2 AS
SELECT
    old.canonical_bld_id,
    core.primary_article_id AS primary_divisare_id,
    old.source_refs,
    core.name,
    core.location_city,
    core.location_country,
    core.location_resolution_method,
    core.location_confidence,
    core.project_year,
    core.year_kind,
    core.area_sqm,
    old.architect_canonical_ids,
    old.architect_names,
    old.program,
    old.programs,
    old.mixed_use,
    old.typology_primary,
    old.typology_tags,
    old.multi_typology,
    old.material_visual,
    old.colors,
    old.style,
    old.structural_system,
    old.facade_materials,
    old.facade_pattern,
    old.facade_system,
    old.roof_type,
    old.architectural_elements,
    old.site_contexts,
    old.intervention_types,
    old.source_categories,
    old.cover_image_url,
    old.gallery_image_urls,
    article.description,
    article.description_quality,
    CASE WHEN article.description_source='parent'
         THEN old.description_ui_markers ELSE 0 END AS description_ui_markers,
    article.description_candidate,
    article.description_status,
    article.availability_status,
    old.primary_article_kind,
    old.primary_article_kind_status,
    old.article_kind_counts_json,
    old.cluster_confidence,
    core.metadata_needs_review AS needs_review,
    core.core_conflicts_json,
    old.facet_conflicts_json,
    core.reconciliation_conflicts_json,
    core.review_reasons_json,
    core.field_sources_json,
    COALESCE((
      SELECT json_group_array(json_object(
        'axis',axis,'value',value,'role',role,'confidence',confidence,
        'search_tier',search_tier
      ))
      FROM (
        SELECT axis,value,role,confidence,search_tier
        FROM building_facets_v2 f
        WHERE f.building_id=old.canonical_bld_id AND f.status='confirmed'
        ORDER BY axis,confidence DESC,value
      )
    ), '[]') AS confirmed_facets_json,
    COALESCE((
      SELECT json_group_array(json_object(
        'axis',axis,'value',value,'role',role,'confidence',confidence,
        'search_tier',search_tier
      ))
      FROM (
        SELECT axis,value,role,confidence,search_tier
        FROM building_facets_v2 f
        WHERE f.building_id=old.canonical_bld_id AND f.status='candidate'
        ORDER BY axis,confidence DESC,value
      )
    ), '[]') AS candidate_facets_json,
    'divisare-metadata-v2.2' AS metadata_version
FROM v_divisare_buildings_export_v2 old
JOIN building_core_reconciled_v2_2 core
  ON core.building_id=old.canonical_bld_id AND core.is_active=1
JOIN v_article_metadata_reconciled_v2_2 article
  ON article.article_id=core.primary_article_id;
"""


def required_objects(conn: sqlite3.Connection, object_type: str) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type=?", (object_type,)
        )
    }


def _quote_identifier(value: str) -> str:
    return '"%s"' % value.replace('"', '""')


def _update_typed_digest(digest: Any, value: Any) -> None:
    if value is None:
        marker = b"N"
        payload = b""
    elif isinstance(value, int):
        marker = b"I"
        payload = str(value).encode("ascii")
    elif isinstance(value, float):
        marker = b"F"
        payload = value.hex().encode("ascii")
    elif isinstance(value, str):
        marker = b"T"
        payload = value.encode("utf-8")
    elif isinstance(value, (bytes, bytearray, memoryview)):
        marker = b"B"
        payload = bytes(value)
    else:
        raise TypeError("unsupported SQLite value type: %s" % type(value).__name__)
    digest.update(marker)
    digest.update(len(payload).to_bytes(8, "big"))
    digest.update(payload)


def table_logical_sha256(conn: sqlite3.Connection, table: str) -> str:
    """Hash one SQLite table independently of page layout and row insertion order."""

    quoted_table = _quote_identifier(table)
    info = list(conn.execute("PRAGMA table_info(%s)" % quoted_table))
    if not info:
        raise RuntimeError("cannot hash missing or columnless table: %s" % table)
    columns = [str(row[1]) for row in info]
    primary_key = [
        str(row[1])
        for row in sorted(
            (row for row in info if int(row[5]) > 0),
            key=lambda row: int(row[5]),
        )
    ]
    order_columns = primary_key or columns
    select_columns = ",".join(_quote_identifier(column) for column in columns)
    if primary_key:
        order_sql = ",".join(_quote_identifier(column) for column in order_columns)
    else:
        # Explicit type and quoted-value ordering avoids declared text collations
        # making differently-cased values compare equal.
        order_sql = ",".join(
            "typeof(%s),quote(%s) COLLATE BINARY"
            % (_quote_identifier(column), _quote_identifier(column))
            for column in order_columns
        )

    digest = hashlib.sha256()
    _update_typed_digest(digest, table)
    for column in columns:
        _update_typed_digest(digest, column)
    query = "SELECT %s FROM %s ORDER BY %s" % (
        select_columns,
        quoted_table,
        order_sql,
    )
    for row in conn.execute(query):
        digest.update(b"R")
        for value in row:
            _update_typed_digest(digest, value)
    return digest.hexdigest()


def preserved_table_logical_hashes(
    conn: sqlite3.Connection,
    tables: Optional[Sequence[str]] = None,
) -> Dict[str, str]:
    if tables is None:
        tables = tuple(
            str(row[0])
            for row in conn.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type='table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            )
        )
    return {
        table: table_logical_sha256(conn, table)
        for table in sorted(set(tables))
    }


def validate_parent(
    conn: sqlite3.Connection,
    *,
    parent_sha256: str,
) -> Dict[str, Any]:
    required = set(PRESERVED_TABLES) | {
        "artifact_lineage_v2",
        "article_text_versions",
        "buildings",
    }
    missing = sorted(required - required_objects(conn, "table"))
    if missing:
        raise RuntimeError("parent DB is missing required tables: %s" % missing)
    views = required_objects(conn, "view")
    for view in ("v_source_articles", "v_divisare_buildings_export_v2"):
        if view not in views:
            raise RuntimeError("parent DB is missing required view: %s" % view)
    quick_check = conn.execute("PRAGMA quick_check").fetchone()[0]
    if quick_check != "ok":
        raise RuntimeError("parent DB quick_check failed: %s" % quick_check)
    user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if user_version != EXPECTED_PARENT_SCHEMA:
        raise RuntimeError(
            "expected parent user_version %d, found %d"
            % (EXPECTED_PARENT_SCHEMA, user_version)
        )
    lineage = conn.execute(
        "SELECT * FROM artifact_lineage_v2 WHERE lineage_id=1"
    ).fetchone()
    if lineage is None:
        raise RuntimeError("parent DB has no artifact_lineage_v2")
    if lineage["metadata_version"] != EXPECTED_PARENT_METADATA_VERSION:
        raise RuntimeError(
            "expected parent metadata %s, found %s"
            % (EXPECTED_PARENT_METADATA_VERSION, lineage["metadata_version"])
        )
    counts = {
        table: int(conn.execute("SELECT COUNT(*) FROM [%s]" % table).fetchone()[0])
        for table in PRESERVED_TABLES
    }
    counts["active_export"] = int(
        conn.execute("SELECT COUNT(*) FROM v_divisare_buildings_export_v2").fetchone()[0]
    )
    content_hashes = preserved_table_logical_hashes(conn)
    return {
        "sha256": parent_sha256,
        "user_version": user_version,
        "lineage": dict(lineage),
        "counts": counts,
        "content_hashes": content_hashes,
    }


def validate_recrawl(
    conn: sqlite3.Connection,
    *,
    parent_sha256: str,
) -> Dict[str, Any]:
    required = {
        "recrawl_lineage",
        "recrawl_runs",
        "article_html_jobs",
        "article_html_snapshots",
        "article_metadata_versions",
    }
    missing = sorted(required - required_objects(conn, "table"))
    if missing:
        raise RuntimeError("recrawl DB is missing required tables: %s" % missing)
    quick_check = conn.execute("PRAGMA quick_check").fetchone()[0]
    if quick_check != "ok":
        raise RuntimeError("recrawl DB quick_check failed: %s" % quick_check)
    user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if user_version != EXPECTED_RECRAWL_SCHEMA:
        raise RuntimeError(
            "expected recrawl user_version %d, found %d"
            % (EXPECTED_RECRAWL_SCHEMA, user_version)
        )
    lineage = conn.execute("SELECT * FROM recrawl_lineage WHERE lineage_id=1").fetchone()
    if lineage is None:
        raise RuntimeError("recrawl DB has no recrawl_lineage")
    if lineage["parent_sha256"].casefold() != parent_sha256.casefold():
        raise RuntimeError("recrawl parent SHA does not match the supplied v2.1 DB")
    if lineage["parent_metadata_version"] != EXPECTED_PARENT_METADATA_VERSION:
        raise RuntimeError("recrawl lineage metadata version is not v2.1")
    if lineage["parser_version"] != EXPECTED_PARSER_VERSION:
        raise RuntimeError(
            "expected parser %s, found %s"
            % (EXPECTED_PARSER_VERSION, lineage["parser_version"])
        )

    fetch_status = {
        row["fetch_status"]: int(row["n"])
        for row in conn.execute(
            "SELECT fetch_status,COUNT(*) AS n FROM article_html_jobs GROUP BY fetch_status"
        )
    }
    parse_status = {
        row["parse_status"]: int(row["n"])
        for row in conn.execute(
            "SELECT parse_status,COUNT(*) AS n FROM article_html_jobs GROUP BY parse_status"
        )
    }
    invalid_fetch = set(fetch_status) - {"success", "not_modified", "not_found"}
    invalid_parse = set(parse_status) - {"success", "partial", "no_content", "skipped"}
    if invalid_fetch or invalid_parse:
        raise RuntimeError(
            "recrawl is not terminal: fetch=%s parse=%s"
            % (sorted(invalid_fetch), sorted(invalid_parse))
        )
    jobs = int(conn.execute("SELECT COUNT(*) FROM article_html_jobs").fetchone()[0])
    current_snapshots = int(
        conn.execute(
            "SELECT COUNT(*) FROM article_html_snapshots WHERE is_current=1"
        ).fetchone()[0]
    )
    current_metadata = int(
        conn.execute(
            "SELECT COUNT(*) FROM article_metadata_versions WHERE is_current=1"
        ).fetchone()[0]
    )
    fetched = fetch_status.get("success", 0) + fetch_status.get("not_modified", 0)
    if current_snapshots != fetched or current_metadata != fetched:
        raise RuntimeError(
            "current snapshot/metadata accounting mismatch: %d/%d expected %d"
            % (current_snapshots, current_metadata, fetched)
        )
    invalid_current = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM article_metadata_versions m
            JOIN article_html_jobs j ON j.article_id=m.article_id
            JOIN article_html_snapshots s ON s.snapshot_id=m.snapshot_id
            WHERE m.is_current=1
              AND (s.is_current<>1 OR s.article_id<>m.article_id
                   OR j.current_html_sha256<>s.html_sha256)
            """
        ).fetchone()[0]
    )
    if invalid_current:
        raise RuntimeError("recrawl has invalid current metadata links")
    completed_at = conn.execute(
        "SELECT MAX(COALESCE(parsed_at,updated_at)) FROM article_html_jobs"
    ).fetchone()[0]
    return {
        "user_version": user_version,
        "lineage": dict(lineage),
        "jobs": jobs,
        "current_snapshots": current_snapshots,
        "current_metadata": current_metadata,
        "fetch_status": fetch_status,
        "parse_status": parse_status,
        "completed_at": completed_at or lineage["created_at"],
    }


def validate_paths(paths: Sequence[Path]) -> None:
    resolved = [path.resolve() for path in paths]
    if len(set(resolved)) != len(resolved):
        raise ValueError("parent, recrawl, output, report, temp, and lock paths must differ")


def load_partial_text_decisions(
    path: Path,
) -> Tuple[str, str, Dict[int, Dict[str, Any]]]:
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    raw = path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("partial decision file is not valid UTF-8 JSON") from exc
    if payload.get("schema_version") != PARTIAL_DECISION_SCHEMA_VERSION:
        raise ValueError("unsupported partial decision schema_version")
    version = clean_scalar(payload.get("version"))
    decided_by = clean_scalar(payload.get("decided_by"))
    decided_at = clean_scalar(payload.get("decided_at"))
    if not version or not decided_by or not decided_at:
        raise ValueError("partial decision file is missing versioned provenance")
    decisions: Dict[int, Dict[str, Any]] = {}
    for item in payload.get("decisions", []):
        try:
            article_id = int(item["article_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("partial decision has an invalid article_id") from exc
        if article_id in decisions:
            raise ValueError("duplicate partial decision for article %d" % article_id)
        parser_version = clean_scalar(item.get("parser_version"))
        prose_sha256 = clean_scalar(item.get("prose_sha256"))
        decision = clean_scalar(item.get("decision"))
        reason_code = clean_scalar(item.get("reason_code"))
        note = clean_scalar(item.get("note"))
        if parser_version != EXPECTED_PARSER_VERSION:
            raise ValueError("partial decision parser guard is not v2.3")
        if not prose_sha256 or len(prose_sha256) != 64:
            raise ValueError("partial decision has an invalid prose SHA")
        if decision not in {"accept", "reject", "review"}:
            raise ValueError("partial decision must be accept, reject, or review")
        if not reason_code or not note:
            raise ValueError("partial decision is missing reason provenance")
        decisions[article_id] = {
            "article_id": article_id,
            "parser_version": parser_version,
            "prose_sha256": prose_sha256.casefold(),
            "decision": decision,
            "reason_code": reason_code,
            "note": note,
            "decided_by": decided_by,
            "decided_at": decided_at,
            "decision_policy_version": version,
        }
    return version, hashlib.sha256(raw).hexdigest(), decisions


def materialize_partial_text_decisions(
    conn: sqlite3.Connection,
    decisions: Mapping[int, Mapping[str, Any]],
) -> Dict[int, Dict[str, Any]]:
    applicable: Dict[int, Dict[str, Any]] = {}
    rows: List[Tuple[Any, ...]] = []
    partial_evidence = list(conn.execute(
        """
        SELECT article_id,parser_version,details_json
        FROM article_recrawl_evidence_v2_2
        WHERE parse_status='partial'
        ORDER BY article_id
        """
    ))
    partial_ids = {int(evidence["article_id"]) for evidence in partial_evidence}
    decision_ids = {int(article_id) for article_id in decisions}
    missing = sorted(partial_ids - decision_ids)
    extra = sorted(decision_ids - partial_ids)
    if missing or extra:
        raise RuntimeError(
            "partial decision ID set mismatch: missing=%s extra=%s"
            % (missing, extra)
        )

    for evidence in partial_evidence:
        article_id = int(evidence["article_id"])
        decision = decisions.get(article_id)
        assert decision is not None
        details = json.loads(evidence["details_json"] or "{}")
        prose_sha256 = clean_scalar(details.get("prose_sha256"))
        guard_matched = int(
            evidence["parser_version"] == decision["parser_version"]
            and prose_sha256 is not None
            and prose_sha256.casefold() == decision["prose_sha256"]
        )
        if not guard_matched:
            raise RuntimeError(
                "partial decision hash/parser guard failed for article %d" % article_id
            )
        applied = dict(decision)
        applied["hash_guard_matched"] = guard_matched
        applicable[article_id] = applied
        rows.append(
            (
                article_id,
                decision["parser_version"],
                decision["prose_sha256"],
                decision["decision"],
                decision["reason_code"],
                decision["note"],
                decision["decided_by"],
                decision["decided_at"],
                decision["decision_policy_version"],
                guard_matched,
            )
        )
    conn.executemany(
        """
        INSERT INTO article_partial_text_decisions_v2_2(
            article_id,parser_version,prose_sha256,decision,reason_code,note,
            decided_by,decided_at,decision_policy_version,hash_guard_matched
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        rows,
    )
    return applicable


def copy_recrawl_evidence(
    target: sqlite3.Connection,
    recrawl: sqlite3.Connection,
) -> int:
    query = """
        SELECT
            j.article_id,j.source_url,j.priority,j.reasons_json,
            j.fetch_status,j.parse_status,j.attempt_count,j.http_status,
            j.final_url,j.content_type,j.updated_at,j.last_error,
            m.metadata_id,m.name,m.abstract,m.location_country,m.location_city,
            m.project_year,m.area_sqm,m.area_raw,m.description_prose,
            m.description_quality,m.explicit_article_kind,
            m.explicit_article_kind_raw,m.parser_version,m.details_json,
            m.parsed_at,
            s.snapshot_id,s.html_sha256,s.snapshot_path,s.byte_size,
            s.response_headers_json,s.fetched_at
        FROM article_html_jobs j
        LEFT JOIN article_metadata_versions m
          ON m.article_id=j.article_id AND m.is_current=1
        LEFT JOIN article_html_snapshots s
          ON s.article_id=j.article_id AND s.is_current=1
        ORDER BY j.article_id
    """
    insert = """
        INSERT INTO article_recrawl_evidence_v2_2(
            article_id,source_url,priority,reasons_json,fetch_status,parse_status,
            attempt_count,http_status,final_url,content_type,metadata_id,
            snapshot_id,html_sha256,snapshot_path,html_byte_size,
            response_headers_json,recrawl_name,recrawl_abstract,
            recrawl_location_country,recrawl_location_city,
            recrawl_project_year,recrawl_area_sqm,recrawl_area_raw,
            description_prose,description_quality,explicit_article_kind,
            explicit_article_kind_raw,parser_version,details_json,fetched_at,
            parsed_at,updated_at,last_error
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """
    batch: List[Tuple[Any, ...]] = []
    copied = 0
    for row in recrawl.execute(query):
        batch.append(
            (
                row["article_id"], row["source_url"], row["priority"],
                row["reasons_json"], row["fetch_status"], row["parse_status"],
                row["attempt_count"], row["http_status"], row["final_url"],
                row["content_type"], row["metadata_id"], row["snapshot_id"],
                row["html_sha256"], row["snapshot_path"], row["byte_size"],
                row["response_headers_json"], row["name"], row["abstract"],
                row["location_country"], row["location_city"],
                row["project_year"], row["area_sqm"], row["area_raw"],
                row["description_prose"], row["description_quality"],
                row["explicit_article_kind"], row["explicit_article_kind_raw"],
                row["parser_version"], row["details_json"], row["fetched_at"],
                row["parsed_at"], row["updated_at"], row["last_error"],
            )
        )
        if len(batch) >= 250:
            target.executemany(insert, batch)
            copied += len(batch)
            batch.clear()
    if batch:
        target.executemany(insert, batch)
        copied += len(batch)
    return copied


def _looks_like_location_placeholder(
    name: Any,
    country: Any,
    city: Any,
) -> bool:
    display = clean_scalar(name)
    country_display = clean_scalar(country)
    city_display = clean_scalar(city)
    if not display or not country_display or not city_display:
        return False
    normalized = normalize_identity_text(display)
    return normalized in {
        normalize_identity_text("%s %s" % (city_display, country_display)),
        normalize_identity_text("%s %s" % (country_display, city_display)),
    }


def _resolution_conflict(value: Any) -> Any:
    if value is None:
        return None
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, (str, int, float, bool, list, tuple, dict)):
        return value
    return str(value)


def populate_article_resolutions(
    conn: sqlite3.Connection,
    *,
    reconciled_at: str,
    partial_decisions: Mapping[int, Mapping[str, Any]],
) -> Dict[str, int]:
    query = """
        SELECT
            p.article_id,p.name_raw,p.abstract_raw,p.location_country,
            p.location_city,p.project_year,p.area_sqm,
            p.description_quality AS parent_description_quality,
            tv.text_id AS parent_description_text_id,
            tv.text AS parent_description,
            e.*
        FROM source_articles p
        JOIN article_recrawl_evidence_v2_2 e ON e.article_id=p.article_id
        LEFT JOIN article_text_versions tv
          ON tv.article_id=p.article_id
         AND tv.text_kind='clean_description'
         AND tv.is_current=1
        ORDER BY p.article_id
    """
    insert = """
        INSERT INTO article_metadata_resolution_v2_2(
            article_id,availability_status,resolved_name,
            resolved_name_normalized,resolved_abstract,location_country,
            location_city,project_year,area_sqm,area_candidate_sqm,
            area_evidence_status,area_unit_kind,area_confidence,
            parent_description_text_id,
            name_source,name_status,abstract_source,country_source,
            country_status,city_source,city_status,year_source,year_status,
            area_source,area_status,description_source,description_status,
            description_publishable,area_evidence_json,field_sources_json,
            field_conflicts_json,review_reasons_json,metadata_needs_review,
            reconciliation_status,policy_version,reconciled_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """
    rows: List[Tuple[Any, ...]] = []
    counts: Dict[str, int] = defaultdict(int)
    for row in conn.execute(query):
        parent_name = row["name_raw"]
        invalid_parent_name = _looks_like_location_placeholder(
            parent_name,
            row["recrawl_location_country"],
            row["recrawl_location_city"],
        ) and not clean_scalar(row["recrawl_name"])
        name_result = resolve_name(
            None if invalid_parent_name else parent_name,
            row["recrawl_name"],
        )
        country_result = resolve_country(
            row["location_country"], row["recrawl_location_country"]
        )
        city_result = resolve_city(
            row["location_city"], row["recrawl_location_city"]
        )
        year_result = resolve_year(
            row["project_year"], row["recrawl_project_year"]
        )
        area_evidence = parse_area_evidence(
            row["recrawl_area_raw"], row["recrawl_area_sqm"]
        )
        area_result = resolve_area(
            row["area_sqm"],
            None if area_evidence.needs_review else area_evidence.value_sqm,
        )
        description_result = resolve_description(
            bool(row["parent_description"]),
            row["description_prose"],
            row["fetch_status"],
            row["parse_status"],
            row["description_quality"],
        )

        description_source = description_result.source
        description_status = description_result.status
        description_value = description_result.value
        description_needs_review = description_result.needs_review
        partial_decision = partial_decisions.get(int(row["article_id"]))
        if partial_decision is not None:
            decision = partial_decision["decision"]
            if decision == "accept":
                description_source = "recrawl"
                description_status = "manual_accept_fallback"
                description_value = row["description_prose"]
                description_needs_review = False
            elif decision == "reject":
                description_source = "none"
                description_status = "manual_reject_fallback"
                description_value = None
                description_needs_review = False
            else:
                description_source = "recrawl_candidate"
                description_status = "manual_review_fallback"
                description_value = row["description_prose"]
                description_needs_review = True

        abstract = clean_scalar(row["recrawl_abstract"])
        if abstract:
            abstract_source = "recrawl"
        else:
            abstract = clean_scalar(row["abstract_raw"])
            abstract_source = "parent" if abstract else "none"

        conflicts: Dict[str, Any] = {}
        review_reasons: set[str] = set()
        field_results = {
            "name": name_result,
            "country": country_result,
            "city": city_result,
            "year": year_result,
            "area": area_result,
        }
        for field, result in field_results.items():
            if result.conflict is not None:
                conflicts[field] = _resolution_conflict(result.conflict)
            if result.needs_review:
                review_reasons.add("%s_%s" % (field, result.status))
        if invalid_parent_name:
            conflicts["name_parent_invalid"] = parent_name
            review_reasons.add("name_location_placeholder")
        if area_evidence.needs_review:
            review_reasons.add("area_%s" % area_evidence.status)
        if description_needs_review:
            review_reasons.add("description_%s" % description_status)
        if row["fetch_status"] == "not_found":
            review_reasons.add("source_not_found")

        description_publishable = int(
            (
                description_source == "recrawl" and description_value is not None
            )
            or (
                description_source == "parent"
                and row["parent_description"] is not None
            )
        )
        field_sources = {
            field: {"source": result.source, "status": result.status}
            for field, result in field_results.items()
        }
        field_sources["area"]["evidence"] = {
            "status": area_evidence.status,
            "unit_kind": area_evidence.unit_kind,
            "confidence": area_evidence.confidence,
            "candidate_sqm": area_evidence.value_sqm,
        }
        field_sources["abstract"] = {"source": abstract_source}
        field_sources["description"] = {
            "source": description_source,
            "status": description_status,
            "publishable": bool(description_publishable),
        }
        if partial_decision is not None:
            field_sources["description"]["decision"] = {
                "decision": partial_decision["decision"],
                "reason_code": partial_decision["reason_code"],
                "policy_version": partial_decision["decision_policy_version"],
            }
        needs_review = int(bool(review_reasons))
        if needs_review:
            reconciliation_status = "review"
        elif any(result.source == "none" for result in field_results.values()):
            reconciliation_status = "complete_with_nulls"
        else:
            reconciliation_status = "complete"
        availability = (
            "source_unavailable" if row["fetch_status"] == "not_found" else "available"
        )
        resolved_name = name_result.value
        rows.append(
            (
                row["article_id"], availability, resolved_name,
                normalize_identity_text(resolved_name) if resolved_name else None,
                abstract, country_result.value, city_result.value,
                year_result.value, area_result.value,
                area_evidence.value_sqm, area_evidence.status,
                area_evidence.unit_kind, area_evidence.confidence,
                row["parent_description_text_id"], name_result.source,
                name_result.status, abstract_source, country_result.source,
                country_result.status, city_result.source, city_result.status,
                year_result.source, year_result.status, area_result.source,
                area_result.status, description_source,
                description_status, description_publishable,
                json_dumps(asdict(area_evidence)), json_dumps(field_sources),
                json_dumps(conflicts), json_dumps(sorted(review_reasons)),
                needs_review, reconciliation_status, POLICY_VERSION,
                reconciled_at,
            )
        )
        counts["articles"] += 1
        counts["needs_review"] += needs_review
        counts["description_publishable"] += description_publishable
        counts["area_resolved"] += int(area_result.value is not None)
        counts["area_quarantined"] += int(area_evidence.needs_review)
        counts["name_conflicts"] += int(name_result.status == "conflict")
        counts["country_conflicts"] += int(country_result.status == "conflict")
    conn.executemany(insert, rows)
    return dict(counts)


def _group_values(
    members: Sequence[sqlite3.Row],
    column: str,
    key,
) -> List[Any]:
    values: Dict[Any, Any] = {}
    for member in members:
        value = member[column]
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        normalized = key(value)
        if normalized is None or normalized == "":
            continue
        values.setdefault(normalized, value)
    return list(values.values())


def _merge_conflict(
    base: Dict[str, Any],
    key: str,
    values: Iterable[Any],
) -> None:
    merged = list(base.get(key, []))
    for value in values:
        if value not in merged:
            merged.append(value)
    base[key] = merged


def populate_building_core(
    conn: sqlite3.Connection,
    *,
    reconciled_at: str,
) -> Dict[str, int]:
    members: Dict[str, List[sqlite3.Row]] = defaultdict(list)
    for row in conn.execute(
        """
        SELECT m.building_id,m.article_id,
               r.resolved_name,r.resolved_name_normalized,
               r.location_country,r.location_city,r.project_year,r.area_sqm,
               r.name_source,r.name_status,r.country_source,r.country_status,
               r.city_source,r.city_status,r.year_source,r.year_status,
               r.area_source,r.area_status,r.description_source,
               r.description_status,r.metadata_needs_review,
               r.review_reasons_json
        FROM active_building_membership_v2 m
        JOIN article_metadata_resolution_v2_2 r ON r.article_id=m.article_id
        ORDER BY m.building_id,m.article_id
        """
    ):
        members[row["building_id"]].append(row)

    insert = """
        INSERT INTO building_core_reconciled_v2_2(
            building_id,is_active,redirect_to,article_count,primary_article_id,
            name,name_normalized,location_country,location_city,
            location_resolution_method,location_confidence,project_year,
            year_kind,area_sqm,field_sources_json,core_conflicts_json,
            reconciliation_conflicts_json,review_reasons_json,
            metadata_needs_review,reconciliation_status,resolution_version,
            resolved_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """
    output: List[Tuple[Any, ...]] = []
    counts: Dict[str, int] = defaultdict(int)
    for attrs in conn.execute(
        "SELECT * FROM building_attributes_v2 ORDER BY building_id"
    ):
        building_id = attrs["building_id"]
        building_members = members.get(building_id, [])
        existing_conflicts = json.loads(attrs["core_conflicts_json"] or "{}")
        reconciliation_conflicts: Dict[str, Any] = {}
        review_reasons: set[str] = set()
        if attrs["metadata_needs_review"]:
            review_reasons.add("parent_metadata_review")

        if not attrs["is_active"]:
            field_sources = {"all": "inactive_redirect_parent_preserved"}
            values = (
                attrs["name"], attrs["name_normalized"],
                attrs["location_country"], attrs["location_city"],
                attrs["location_resolution_method"], attrs["location_confidence"],
                attrs["project_year"], attrs["year_kind"], attrs["area_sqm"],
            )
        else:
            for member in building_members:
                if member["metadata_needs_review"]:
                    review_reasons.update(json.loads(member["review_reasons_json"]))
            primary = next(
                (
                    member for member in building_members
                    if int(member["article_id"]) == int(attrs["primary_article_id"])
                ),
                None,
            )

            names = _group_values(
                building_members, "resolved_name", normalize_identity_text
            )
            if primary is not None and primary["resolved_name"]:
                name = primary["resolved_name"]
            elif len(names) == 1:
                name = names[0]
            else:
                name = None
            if len(names) > 1:
                reconciliation_conflicts["name"] = sorted(str(v) for v in names)
                _merge_conflict(existing_conflicts, "name", names)
                review_reasons.add("building_name_conflict")
            name_normalized = normalize_identity_text(name) if name else None

            countries = _group_values(
                building_members, "location_country", lambda value: str(value).casefold()
            )
            cities = _group_values(
                building_members, "location_city", normalize_identity_text
            )
            if len(countries) == 1:
                country = countries[0]
            elif len(countries) == 0:
                country = resolve_country(attrs["location_country"], None).value
            else:
                country = resolve_country(attrs["location_country"], None).value
                reconciliation_conflicts["location_country"] = sorted(
                    str(v) for v in countries
                )
                _merge_conflict(existing_conflicts, "location_country", countries)
                review_reasons.add("building_country_conflict")
            if len(cities) == 1:
                city = cities[0]
            elif len(cities) == 0:
                city = resolve_city(attrs["location_city"], None).value
            else:
                city = resolve_city(attrs["location_city"], None).value
                reconciliation_conflicts["location_city"] = sorted(
                    str(v) for v in cities
                )
                _merge_conflict(existing_conflicts, "location_city", cities)
                review_reasons.add("building_city_conflict")

            if "location_country" in reconciliation_conflicts or "location_city" in reconciliation_conflicts:
                location_method = "reconciliation_conflict_parent_preserved"
                location_confidence = 0.0
            elif country != attrs["location_country"] or city != attrs["location_city"]:
                location_method = "recrawl_member_consensus"
                location_confidence = 0.95 if country and city else 0.8
            elif country is None and city is None:
                location_method = "unresolved"
                location_confidence = 0.0
            else:
                location_method = attrs["location_resolution_method"]
                location_confidence = float(attrs["location_confidence"])

            years = _group_values(building_members, "project_year", int)
            valid_parent_year = resolve_year(attrs["project_year"], None).value
            if len(years) == 1:
                project_year = int(years[0])
            elif len(years) == 0:
                project_year = valid_parent_year
            else:
                project_year = valid_parent_year
                reconciliation_conflicts["project_year"] = sorted(int(v) for v in years)
                _merge_conflict(existing_conflicts, "project_year", years)
                review_reasons.add("building_year_conflict")
            if "project_year" in reconciliation_conflicts:
                year_kind = "conflict_parent_preserved"
            elif project_year != attrs["project_year"]:
                year_kind = "recrawl_member_consensus" if project_year else "unknown"
            else:
                year_kind = attrs["year_kind"]

            areas = _group_values(
                building_members, "area_sqm", lambda value: round(float(value), 4)
            )
            valid_parent_area = resolve_area(attrs["area_sqm"], None).value
            if len(areas) == 1:
                area_sqm = float(areas[0])
            elif len(areas) == 0:
                area_sqm = valid_parent_area
            else:
                area_sqm = valid_parent_area
                reconciliation_conflicts["area_sqm"] = sorted(float(v) for v in areas)
                _merge_conflict(existing_conflicts, "area_sqm", areas)
                review_reasons.add("building_area_conflict")

            primary_sources = {
                "name": (
                    {"source": primary["name_source"], "status": primary["name_status"]}
                    if primary else {"source": "none"}
                ),
                "country": "article_member_consensus" if countries else "parent_building",
                "city": "article_member_consensus" if cities else "parent_building",
                "year": "article_member_consensus" if years else "parent_building",
                "area": "article_member_consensus" if areas else "parent_building",
                "description": (
                    {
                        "source": primary["description_source"],
                        "status": primary["description_status"],
                    }
                    if primary else {"source": "none"}
                ),
            }
            field_sources = primary_sources
            values = (
                name, name_normalized, country, city, location_method,
                location_confidence, project_year, year_kind, area_sqm,
            )

        needs_review = int(bool(review_reasons) or bool(reconciliation_conflicts))
        if needs_review:
            status = "review"
        elif not attrs["is_active"]:
            status = "inactive_redirect"
        else:
            status = "complete"
        output.append(
            (
                building_id, attrs["is_active"], attrs["redirect_to"],
                attrs["article_count"], attrs["primary_article_id"],
                values[0], values[1], values[2], values[3], values[4],
                values[5], values[6], values[7], values[8],
                json_dumps(field_sources), json_dumps(existing_conflicts),
                json_dumps(reconciliation_conflicts),
                json_dumps(sorted(review_reasons)), needs_review, status,
                POLICY_VERSION, reconciled_at,
            )
        )
        counts["buildings"] += 1
        counts["active"] += int(attrs["is_active"])
        counts["needs_review"] += needs_review
        counts["core_conflicts"] += int(bool(reconciliation_conflicts))
    conn.executemany(insert, output)
    return dict(counts)


def collect_metrics(
    conn: sqlite3.Connection,
    *,
    article_metrics: Mapping[str, int],
    building_metrics: Mapping[str, int],
    recrawl_metrics: Mapping[str, Any],
) -> Dict[str, Any]:
    metrics: Dict[str, Any] = {
        "article_population": dict(article_metrics),
        "building_population": dict(building_metrics),
        "fetch_status": dict(recrawl_metrics["fetch_status"]),
        "parse_status": dict(recrawl_metrics["parse_status"]),
    }
    metrics["article_coverage"] = dict(
        conn.execute(
            """
            SELECT
              COUNT(*) AS articles,
              SUM(resolved_name IS NOT NULL) AS name,
              SUM(location_country IS NOT NULL) AS country,
              SUM(location_city IS NOT NULL) AS city,
              SUM(project_year IS NOT NULL) AS year,
              SUM(area_sqm IS NOT NULL) AS area,
              SUM(description_publishable=1) AS description,
              SUM(metadata_needs_review=1) AS needs_review
            FROM article_metadata_resolution_v2_2
            """
        ).fetchone()
    )
    metrics["building_coverage"] = dict(
        conn.execute(
            """
            SELECT
              COUNT(*) AS buildings,
              SUM(name IS NOT NULL) AS name,
              SUM(location_country IS NOT NULL) AS country,
              SUM(location_city IS NOT NULL) AS city,
              SUM(project_year IS NOT NULL) AS year,
              SUM(area_sqm IS NOT NULL) AS area,
              SUM(metadata_needs_review=1) AS needs_review
            FROM building_core_reconciled_v2_2
            WHERE is_active=1
            """
        ).fetchone()
    )
    for label, column in (
        ("description_status", "description_status"),
        ("area_status", "area_status"),
        ("area_evidence_status", "area_evidence_status"),
        ("reconciliation_status", "reconciliation_status"),
    ):
        metrics[label] = {
            row["value"]: int(row["n"])
            for row in conn.execute(
                "SELECT %s AS value,COUNT(*) AS n "
                "FROM article_metadata_resolution_v2_2 GROUP BY %s ORDER BY %s"
                % (column, column, column)
            )
        }
    metrics["d2_status"] = {
        row["decision_status"]: int(row["n"])
        for row in conn.execute(
            """
            SELECT decision_status,COUNT(*) AS n
            FROM article_match_reviews_v2
            GROUP BY decision_status ORDER BY decision_status
            """
        )
    }
    metrics["taxonomy"] = {
        row["status"]: int(row["n"])
        for row in conn.execute(
            """
            SELECT status,COUNT(*) AS n
            FROM building_facets_v2
            GROUP BY status ORDER BY status
            """
        )
    }
    metrics["images_preserved"] = {
        table: int(conn.execute("SELECT COUNT(*) FROM [%s]" % table).fetchone()[0])
        for table in (
            "article_image_occurrences", "image_urls", "image_assets",
            "image_hashes", "image_classifications", "building_images_materialized_v2",
        )
    }
    return metrics


def store_metrics(conn: sqlite3.Connection, metrics: Mapping[str, Any]) -> None:
    conn.executemany(
        "INSERT INTO metadata_reconciliation_metrics_v2_2(metric,value_json) VALUES (?,?)",
        [(key, json_dumps(value)) for key, value in sorted(metrics.items())],
    )


def count_unknown_auto_area_residuals(conn: sqlite3.Connection) -> int:
    return int(conn.execute(AUTO_AREA_RESIDUAL_UNKNOWN_SQL).fetchone()[0])


def validate_output(
    conn: sqlite3.Connection,
    *,
    parent_counts: Mapping[str, int],
    parent_content_hashes: Mapping[str, str],
    recrawl_metrics: Mapping[str, Any],
    checked_at: str,
) -> Dict[str, Any]:
    checks: List[Dict[str, Any]] = []

    def add(name: str, actual: Any, expected: Any, passed: Optional[bool] = None) -> None:
        checks.append(
            {
                "name": name,
                "actual": actual,
                "expected": expected,
                "passed": bool(actual == expected if passed is None else passed),
            }
        )

    add("sqlite_integrity", conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
    add("foreign_keys", len(conn.execute("PRAGMA foreign_key_check").fetchall()), 0)
    add("schema_version", conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
    add(
        "preserved_parent_content_exact",
        preserved_table_logical_hashes(conn, tuple(parent_content_hashes)),
        dict(parent_content_hashes),
    )
    for table in PRESERVED_TABLES:
        add(
            "preserved_count_%s" % table,
            int(conn.execute("SELECT COUNT(*) FROM [%s]" % table).fetchone()[0]),
            int(parent_counts[table]),
        )
    article_count = int(parent_counts["source_articles"])
    add(
        "recrawl_job_scope",
        int(conn.execute("SELECT COUNT(*) FROM article_recrawl_evidence_v2_2").fetchone()[0]),
        article_count,
    )
    add(
        "article_resolution_complete",
        int(conn.execute("SELECT COUNT(*) FROM article_metadata_resolution_v2_2").fetchone()[0]),
        article_count,
    )
    add(
        "building_resolution_complete",
        int(conn.execute("SELECT COUNT(*) FROM building_core_reconciled_v2_2").fetchone()[0]),
        int(parent_counts["building_attributes_v2"]),
    )
    add(
        "active_export_complete",
        int(conn.execute("SELECT COUNT(*) FROM v_divisare_buildings_export_v2_2").fetchone()[0]),
        int(parent_counts["active_export"]),
    )
    add(
        "source_urls_match",
        int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM source_articles p
                JOIN article_recrawl_evidence_v2_2 e USING(article_id)
                WHERE p.source_url<>e.source_url
                """
            ).fetchone()[0]
        ),
        0,
    )
    add(
        "fetched_rows_have_current_evidence",
        int(
            conn.execute(
                """
                SELECT COUNT(*) FROM article_recrawl_evidence_v2_2
                WHERE fetch_status IN ('success','not_modified')
                  AND (metadata_id IS NULL OR snapshot_id IS NULL OR html_sha256 IS NULL)
                """
            ).fetchone()[0]
        ),
        0,
    )
    add(
        "not_found_rows_preserved_without_snapshot",
        int(
            conn.execute(
                """
                SELECT COUNT(*) FROM article_recrawl_evidence_v2_2
                WHERE fetch_status='not_found'
                  AND (metadata_id IS NOT NULL OR snapshot_id IS NOT NULL)
                """
            ).fetchone()[0]
        ),
        0,
    )
    add(
        "no_content_not_published_as_prose",
        int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM v_article_metadata_reconciled_v2_2
                WHERE parse_status='no_content' AND description IS NOT NULL
                """
            ).fetchone()[0]
        ),
        0,
    )
    direct_prose_count = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM article_recrawl_evidence_v2_2
            WHERE parse_status='success'
              AND description_quality='dom_prose_paragraphs'
              AND description_prose IS NOT NULL
            """
        ).fetchone()[0]
    )
    add(
        "direct_dom_prose_published",
        int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM v_article_metadata_reconciled_v2_2
                WHERE parse_status='success'
                  AND description_quality='dom_prose_paragraphs'
                  AND description IS NOT NULL
                """
            ).fetchone()[0]
        ),
        direct_prose_count,
    )
    add(
        "nonaccepted_partial_text_quarantined",
        int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM v_article_metadata_reconciled_v2_2 a
                JOIN article_partial_text_decisions_v2_2 d USING(article_id)
                WHERE d.decision<>'accept' AND a.description IS NOT NULL
                """
            ).fetchone()[0]
        ),
        0,
    )
    add(
        "accepted_partial_text_published",
        int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM v_article_metadata_reconciled_v2_2 a
                JOIN article_partial_text_decisions_v2_2 d USING(article_id)
                WHERE d.decision='accept' AND a.description IS NOT NULL
                """
            ).fetchone()[0]
        ),
        int(
            conn.execute(
                "SELECT COUNT(*) FROM article_partial_text_decisions_v2_2 WHERE decision='accept'"
            ).fetchone()[0]
        ),
    )
    partial_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM article_recrawl_evidence_v2_2 WHERE parse_status='partial'"
        ).fetchone()[0]
    )
    add(
        "partial_decisions_complete",
        int(conn.execute("SELECT COUNT(*) FROM article_partial_text_decisions_v2_2").fetchone()[0]),
        partial_count,
    )
    add(
        "partial_decision_guards",
        int(
            conn.execute(
                """
                SELECT COUNT(*) FROM article_partial_text_decisions_v2_2
                WHERE hash_guard_matched<>1
                """
            ).fetchone()[0]
        ),
        0,
    )
    add(
        "project_year_range",
        int(
            conn.execute(
                """
                SELECT COUNT(*) FROM article_metadata_resolution_v2_2
                WHERE project_year IS NOT NULL
                  AND (project_year<1000 OR project_year>2100)
                """
            ).fetchone()[0]
        ),
        0,
    )
    add(
        "review_area_evidence_not_adopted",
        int(
            conn.execute(
                """
                SELECT COUNT(*) FROM article_metadata_resolution_v2_2
                WHERE json_extract(area_evidence_json,'$.needs_review')=1
                  AND area_source='recrawl'
                """
            ).fetchone()[0]
        ),
        0,
    )
    add(
        "accepted_area_residual_classification_unknown",
        count_unknown_auto_area_residuals(conn),
        0,
    )
    add(
        "area_evidence_columns_match_json",
        int(
            conn.execute(
                """
                SELECT COUNT(*) FROM article_metadata_resolution_v2_2
                WHERE area_evidence_status<>json_extract(area_evidence_json,'$.status')
                   OR area_unit_kind<>json_extract(area_evidence_json,'$.unit_kind')
                   OR ABS(area_confidence-json_extract(area_evidence_json,'$.confidence'))>0.000001
                   OR CASE
                        WHEN area_candidate_sqm IS NULL
                         AND json_extract(area_evidence_json,'$.value_sqm') IS NULL THEN 0
                        WHEN area_candidate_sqm IS NULL
                          OR json_extract(area_evidence_json,'$.value_sqm') IS NULL THEN 1
                        WHEN ABS(
                          area_candidate_sqm-json_extract(area_evidence_json,'$.value_sqm')
                        )>0.000001 THEN 1
                        ELSE 0
                      END=1
                """
            ).fetchone()[0]
        ),
        0,
    )
    add(
        "location_delimiters_rejected",
        int(
            conn.execute(
                """
                SELECT COUNT(*) FROM article_metadata_resolution_v2_2
                WHERE TRIM(COALESCE(location_country,'')) IN ('-','--')
                   OR TRIM(COALESCE(location_city,'')) IN ('-','--')
                   OR location_country LIKE '- %'
                   OR location_country LIKE '% -'
                """
            ).fetchone()[0]
        ),
        0,
    )
    image_diff = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM (
              SELECT canonical_bld_id,cover_image_url,gallery_image_urls
              FROM v_divisare_buildings_export_v2
              EXCEPT
              SELECT canonical_bld_id,cover_image_url,gallery_image_urls
              FROM v_divisare_buildings_export_v2_2
            )
            """
        ).fetchone()[0]
    )
    add("image_urls_export_exact", image_diff, 0)
    source_ref_diff = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM (
              SELECT canonical_bld_id,source_refs
              FROM v_divisare_buildings_export_v2
              EXCEPT
              SELECT canonical_bld_id,source_refs
              FROM v_divisare_buildings_export_v2_2
            )
            """
        ).fetchone()[0]
    )
    add("source_refs_export_exact", source_ref_diff, 0)
    add(
        "recrawl_current_snapshot_count",
        int(
            conn.execute(
                "SELECT COUNT(*) FROM article_recrawl_evidence_v2_2 WHERE snapshot_id IS NOT NULL"
            ).fetchone()[0]
        ),
        int(recrawl_metrics["current_snapshots"]),
    )

    conn.executemany(
        """
        INSERT INTO metadata_reconciliation_validation_v2_2(
            check_name,passed,actual_json,expected_json,checked_at
        ) VALUES (?,?,?,?,?)
        """,
        [
            (
                check["name"], int(check["passed"]),
                json_dumps(check["actual"]), json_dumps(check["expected"]),
                checked_at,
            )
            for check in checks
        ],
    )
    failed = [check for check in checks if not check["passed"]]
    return {
        "passed": len(checks) - len(failed),
        "failed": len(failed),
        "checks": checks,
    }


def logical_sha256(conn: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    tables = (
        "metadata_reconciliation_lineage_v2_2",
        "article_recrawl_evidence_v2_2",
        "article_partial_text_decisions_v2_2",
        "article_metadata_resolution_v2_2",
        "building_core_reconciled_v2_2",
        "metadata_reconciliation_metrics_v2_2",
        "metadata_reconciliation_validation_v2_2",
    )
    for table in tables:
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(%s)" % table)]
        digest.update((table + "\n").encode("utf-8"))
        digest.update(("|".join(columns) + "\n").encode("utf-8"))
        order_column = columns[0]
        for row in conn.execute(
            "SELECT * FROM [%s] ORDER BY [%s]" % (table, order_column)
        ):
            digest.update(
                (json_dumps([row[column] for column in columns]) + "\n").encode("utf-8")
            )
    return digest.hexdigest()


def write_report(
    path: Path,
    *,
    parent_path: Path,
    recrawl_path: Path,
    output_path: Path,
    parent_sha256: str,
    recrawl_sha256: str,
    output_sha256: str,
    logical_sha: str,
    metrics: Mapping[str, Any],
    validation: Mapping[str, Any],
    elapsed_seconds: float,
) -> None:
    article = metrics["article_coverage"]
    building = metrics["building_coverage"]
    lines = [
        "# Divisare metadata v2.2 reconciliation report",
        "",
        "## Artifact",
        "",
        "- Builder: `%s`" % BUILDER_VERSION,
        "- Policy: `%s`" % POLICY_VERSION,
        "- Schema user version: `%s`" % SCHEMA_VERSION,
        "- Parent: `%s`" % parent_path,
        "- Parent SHA-256: `%s`" % parent_sha256,
        "- Recrawl sidecar: `%s`" % recrawl_path,
        "- Recrawl SHA-256: `%s`" % recrawl_sha256,
        "- Output: `%s`" % output_path,
        "- Output SHA-256: `%s`" % output_sha256,
        "- Reconciliation logical SHA-256: `%s`" % logical_sha,
        "- Elapsed: `%.2f seconds`" % elapsed_seconds,
        "- External API / LLM / Vision / Neon / R2 cost: `$0`",
        "",
        "## Coverage",
        "",
        "- Articles: `%s`" % article["articles"],
        "- Article name / country / city / year / area: `%s / %s / %s / %s / %s`"
        % (article["name"], article["country"], article["city"], article["year"], article["area"]),
        "- Publishable article descriptions: `%s`" % article["description"],
        "- Active buildings: `%s`" % building["buildings"],
        "- Building name / country / city / year / area: `%s / %s / %s / %s / %s`"
        % (building["name"], building["country"], building["city"], building["year"], building["area"]),
        "- Article / building review rows: `%s / %s`"
        % (article["needs_review"], building["needs_review"]),
        "- Validation: `%s passed / %s failed`"
        % (validation["passed"], validation["failed"]),
        "",
        "## Resolution Metrics",
        "",
        "```json",
        json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True),
        "```",
        "",
        "## Validation",
        "",
        "```json",
        json.dumps(validation["checks"], ensure_ascii=False, indent=2, sort_keys=True),
        "```",
        "",
        "## Scope Boundary",
        "",
        "- Recrawl direct DOM prose is publishable; fallback DOM text remains quarantined.",
        "- Valid no-prose pages do not inherit historical caption/credit residue.",
        "- Built Surface values are reparsed from raw units; ambiguous values remain review items.",
        "- Taxonomy, D2 decisions, architects, source tags, and image URLs are preserved.",
        "- No image download, pHash, image semantics, vector, cross-site merge, Neon, or R2 work ran.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _build_locked(
    *,
    parent_path: Path,
    recrawl_path: Path,
    output_path: Path,
    report_path: Path,
    partial_decisions_path: Path,
) -> Dict[str, Any]:
    started = time.monotonic()
    parent_path = parent_path.resolve()
    recrawl_path = recrawl_path.resolve()
    output_path = output_path.resolve()
    report_path = report_path.resolve()
    partial_decisions_path = partial_decisions_path.resolve()
    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    report_temp_path = report_path.with_suffix(report_path.suffix + ".tmp")
    lock_path = output_path.with_suffix(output_path.suffix + ".build.lock")
    validate_paths(
        (
            parent_path, recrawl_path, partial_decisions_path, output_path,
            report_path, temp_path, report_temp_path, lock_path,
        )
    )
    for input_path in (parent_path, recrawl_path, partial_decisions_path):
        if not input_path.exists():
            raise FileNotFoundError(input_path)
    for immutable_path in (output_path, report_path):
        if immutable_path.exists():
            raise FileExistsError("%s exists; artifacts are immutable" % immutable_path)
    for partial in (temp_path, report_temp_path):
        if partial.exists():
            raise FileExistsError("stale build temp exists: %s" % partial)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    parent_stat = parent_path.stat()
    recrawl_stat = recrawl_path.stat()
    parent_sha_before = file_sha256(parent_path)
    recrawl_sha_before = file_sha256(recrawl_path)
    partial_decision_version, partial_decision_sha, partial_decisions = (
        load_partial_text_decisions(partial_decisions_path)
    )
    parent_conn = open_readonly(parent_path)
    recrawl_conn = open_readonly(recrawl_path)
    parent = validate_parent(parent_conn, parent_sha256=parent_sha_before)
    recrawl = validate_recrawl(recrawl_conn, parent_sha256=parent_sha_before)
    if recrawl["jobs"] != parent["counts"]["source_articles"]:
        parent_conn.close()
        recrawl_conn.close()
        raise RuntimeError("recrawl job scope does not match parent articles")

    target: Optional[sqlite3.Connection] = None
    try:
        target = sqlite3.connect(temp_path)
        parent_conn.backup(target, pages=8192)
        parent_conn.close()
        target.row_factory = sqlite3.Row
        target.execute("PRAGMA foreign_keys=ON")
        target.execute("PRAGMA journal_mode=DELETE")
        target.execute("PRAGMA synchronous=NORMAL")
        target.execute("PRAGMA temp_store=FILE")
        target.executescript(SCHEMA_SQL)
        target.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
        reconciled_at = recrawl["completed_at"]
        target.execute(
            """
            INSERT INTO metadata_reconciliation_lineage_v2_2(
                lineage_id,parent_db_path,parent_sha256,parent_byte_size,
                parent_schema_version,parent_metadata_version,recrawl_db_path,
                recrawl_sha256,recrawl_byte_size,recrawl_schema_version,
                recrawl_parent_sha256,crawler_version,parser_version,
                partial_decision_version,partial_decision_file_path,
                partial_decision_file_sha256,snapshot_root,builder_version,
                reconciliation_policy_version,
                metadata_version,schema_version,preserved_counts_json,
                scope_json,reconciled_at
            ) VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(parent_path), parent_sha_before, parent_stat.st_size,
                parent["user_version"], EXPECTED_PARENT_METADATA_VERSION,
                str(recrawl_path), recrawl_sha_before, recrawl_stat.st_size,
                recrawl["user_version"], recrawl["lineage"]["parent_sha256"],
                recrawl["lineage"]["crawler_version"],
                recrawl["lineage"]["parser_version"],
                partial_decision_version, str(partial_decisions_path),
                partial_decision_sha,
                recrawl["lineage"]["snapshot_root"], BUILDER_VERSION,
                POLICY_VERSION, METADATA_VERSION, SCHEMA_VERSION,
                json_dumps(parent["counts"]),
                json_dumps(
                    {
                        "included": ["non_image_metadata", "taxonomy_preserved", "image_urls_preserved"],
                        "excluded": ["image_download", "phash", "vision", "vectors", "cross_site_merge", "neon", "r2"],
                    }
                ),
                reconciled_at,
            ),
        )
        evidence_count = copy_recrawl_evidence(target, recrawl_conn)
        recrawl_conn.close()
        if evidence_count != recrawl["jobs"]:
            raise RuntimeError("recrawl evidence copy was incomplete")
        applicable_partial_decisions = materialize_partial_text_decisions(
            target, partial_decisions
        )
        article_metrics = populate_article_resolutions(
            target,
            reconciled_at=reconciled_at,
            partial_decisions=applicable_partial_decisions,
        )
        building_metrics = populate_building_core(
            target, reconciled_at=reconciled_at
        )
        target.executescript(VIEWS_SQL)
        metrics = collect_metrics(
            target,
            article_metrics=article_metrics,
            building_metrics=building_metrics,
            recrawl_metrics=recrawl,
        )
        store_metrics(target, metrics)
        validation = validate_output(
            target,
            parent_counts=parent["counts"],
            parent_content_hashes=parent["content_hashes"],
            recrawl_metrics=recrawl,
            checked_at=reconciled_at,
        )
        if validation["failed"]:
            failed = [check["name"] for check in validation["checks"] if not check["passed"]]
            raise RuntimeError("reconciliation validation failed: %s" % failed)
        target.commit()
        target.execute("ANALYZE")
        target.execute("PRAGMA optimize")
        target.commit()
        logical_sha = logical_sha256(target)
        target.close()
        target = None

        parent_sha_after = file_sha256(parent_path)
        recrawl_sha_after = file_sha256(recrawl_path)
        if parent_sha_after != parent_sha_before:
            raise RuntimeError("parent DB changed during reconciliation")
        if recrawl_sha_after != recrawl_sha_before:
            raise RuntimeError("recrawl DB changed during reconciliation")
        output_sha = file_sha256(temp_path)
        elapsed = time.monotonic() - started
        write_report(
            report_temp_path,
            parent_path=parent_path,
            recrawl_path=recrawl_path,
            output_path=output_path,
            parent_sha256=parent_sha_before,
            recrawl_sha256=recrawl_sha_before,
            output_sha256=output_sha,
            logical_sha=logical_sha,
            metrics=metrics,
            validation=validation,
            elapsed_seconds=elapsed,
        )
        publish_no_clobber(
            temp_path=temp_path,
            output_path=output_path,
            report_temp_path=report_temp_path,
            report_path=report_path,
        )
        return {
            "output_db": str(output_path),
            "output_sha256": output_sha,
            "logical_sha256": logical_sha,
            "report": str(report_path),
            "elapsed_seconds": round(elapsed, 2),
            "metrics": metrics,
            "validation": {
                "passed": validation["passed"],
                "failed": validation["failed"],
            },
        }
    except Exception:
        if target is not None:
            target.close()
        for connection in (parent_conn, recrawl_conn):
            try:
                connection.close()
            except Exception:
                pass
        for partial in (temp_path, report_temp_path):
            try:
                partial.unlink()
            except FileNotFoundError:
                pass
        raise


def build(
    *,
    parent_path: Path,
    recrawl_path: Path,
    output_path: Path,
    report_path: Path,
    partial_decisions_path: Path = DEFAULT_PARTIAL_DECISIONS,
) -> Dict[str, Any]:
    output_path = output_path.resolve()
    lock_path = output_path.with_suffix(output_path.suffix + ".build.lock")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_build_lock(lock_path, output_path):
        return _build_locked(
            parent_path=parent_path,
            recrawl_path=recrawl_path,
            output_path=output_path,
            report_path=report_path,
            partial_decisions_path=partial_decisions_path,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--parent-db",
        type=Path,
        default=ROOT / "data" / "curated" / "divisare_metadata_v2_1.db",
    )
    parser.add_argument(
        "--recrawl-db",
        type=Path,
        default=ROOT / "data" / "enrichment" / "divisare_metadata_recrawl_v2_4.db",
    )
    parser.add_argument(
        "--output-db",
        type=Path,
        default=ROOT / "data" / "curated" / "divisare_metadata_v2_2.db",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=ROOT / "data" / "reports" / "divisare_metadata_v2_2.md",
    )
    parser.add_argument(
        "--partial-decisions",
        type=Path,
        default=DEFAULT_PARTIAL_DECISIONS,
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = build(
            parent_path=args.parent_db,
            recrawl_path=args.recrawl_db,
            output_path=args.output_db,
            report_path=args.report,
            partial_decisions_path=args.partial_decisions,
        )
    except Exception as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
