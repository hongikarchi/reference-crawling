"""Build the immutable Divisare metadata v2.3 review artifact.

The builder copies the exact v2.2 production database, applies three
versioned human-review manifests, and materializes every building-derived
surface affected by D2 identity decisions.  It never downloads or classifies
images; image work here is limited to regrouping existing URL/asset records.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from canonical.divisare_curated import normalize_identity_text  # noqa: E402
from canonical.divisare_curated_v2 import (  # noqa: E402
    ARTICLE_KIND_POLICY_VERSION,
    FACET_POLICY_VERSION,
    facet_status_v2,
)
from canonical.divisare_review_v23 import (  # noqa: E402
    BUILDER_VERSION,
    EXPECTED_PARENT_METADATA_VERSION,
    EXPECTED_PARENT_SCHEMA,
    EXPECTED_PARENT_SHA256,
    METADATA_VERSION,
    POLICY_VERSION,
    PRODUCTION_AREA_COUNTS,
    PRODUCTION_D2_COUNTS,
    SCHEMA_VERSION,
    LoadedManifest,
    canonical_json,
    load_area_manifest,
    load_d2_manifest,
    load_partial_manifest,
    resolve_identity_components,
    sha256_bytes,
    validate_area_guard,
    validate_d2_guard,
    validate_partial_guard,
)


DEFAULT_PARENT = ROOT / "data" / "curated" / "divisare_metadata_v2_2.db"
DEFAULT_PARTIAL = ROOT / "canonical" / "divisare_partial_text_decisions_v2.json"
DEFAULT_AREA = ROOT / "canonical" / "divisare_area_decisions_v1.json"
DEFAULT_D2 = ROOT / "canonical" / "divisare_d2_decisions_v1.json"
DEFAULT_OUTPUT = ROOT / "data" / "curated" / "divisare_metadata_v2_3.db"
DEFAULT_REPORT = ROOT / "data" / "reports" / "divisare_metadata_v2_3.md"

SCALAR_AXES = (
    "style",
    "structural_system",
    "roof_type",
    "facade_pattern",
    "facade_system",
)
EXCLUDED_FACET_AXES = (
    "country",
    "city",
    "project_year",
    "area_sqm",
    "country_candidate",
    "city_candidate",
)
SEARCH_TIER_RANK = {"hidden": 0, "secondary": 1, "primary": 2}

REQUIRED_PARENT_TABLES = {
    "metadata_reconciliation_lineage_v2_2",
    "metadata_reconciliation_validation_v2_2",
    "source_articles",
    "article_text_versions",
    "article_recrawl_evidence_v2_2",
    "article_metadata_resolution_v2_2",
    "buildings",
    "building_core_reconciled_v2_2",
    "building_attributes_v2",
    "active_building_membership_v2",
    "article_match_reviews_v2",
    "attribute_claims",
    "claim_evidence_v2",
    "building_facets_v2",
    "building_facet_claims_v2",
    "article_image_occurrences",
    "image_urls",
    "image_assets",
    "building_images_materialized_v2",
    "article_kind_resolution_v2",
    "article_architects",
    "article_tags",
}

ARTICLE_RESOLUTION_COLUMNS = (
    "article_id",
    "availability_status",
    "resolved_name",
    "resolved_name_normalized",
    "resolved_abstract",
    "location_country",
    "location_city",
    "project_year",
    "area_sqm",
    "area_candidate_sqm",
    "area_evidence_status",
    "area_unit_kind",
    "area_confidence",
    "parent_description_text_id",
    "name_source",
    "name_status",
    "abstract_source",
    "country_source",
    "country_status",
    "city_source",
    "city_status",
    "year_source",
    "year_status",
    "area_source",
    "area_status",
    "description_source",
    "description_status",
    "description_publishable",
    "area_evidence_json",
    "field_sources_json",
    "field_conflicts_json",
    "review_reasons_json",
    "metadata_needs_review",
    "reconciliation_status",
    "policy_version",
    "reconciled_at",
)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def open_readonly(path: Path) -> sqlite3.Connection:
    uri = "file:%s?mode=ro" % path.resolve().as_posix()
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


@contextmanager
def exclusive_build_lock(lock_path: Path, output_path: Path):
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(str(lock_path), flags)
    except FileExistsError as exc:
        raise RuntimeError("build lock exists: %s" % lock_path) from exc
    try:
        payload = canonical_json({"pid": os.getpid(), "output": str(output_path)})
        os.write(fd, payload.encode("utf-8"))
        os.close(fd)
        fd = -1
        yield
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def publish_no_clobber(
    *,
    temp_path: Path,
    output_path: Path,
    report_temp_path: Path,
    report_path: Path,
) -> None:
    published: List[Path] = []
    try:
        os.link(report_temp_path, report_path)
        published.append(report_path)
        os.link(temp_path, output_path)
        published.append(output_path)
    except FileExistsError as exc:
        for path in reversed(published):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise RuntimeError("an immutable output appeared during publication") from exc
    except Exception:
        for path in reversed(published):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        raise
    report_temp_path.unlink()
    temp_path.unlink()


def _quote_identifier(value: str) -> str:
    return '"%s"' % value.replace('"', '""')


def _update_typed_digest(digest: Any, value: Any) -> None:
    if value is None:
        marker, payload = b"N", b""
    elif isinstance(value, int):
        marker, payload = b"I", str(value).encode("ascii")
    elif isinstance(value, float):
        marker, payload = b"F", value.hex().encode("ascii")
    elif isinstance(value, str):
        marker, payload = b"T", value.encode("utf-8")
    elif isinstance(value, (bytes, bytearray, memoryview)):
        marker, payload = b"B", bytes(value)
    else:
        raise TypeError("unsupported SQLite value type: %s" % type(value).__name__)
    digest.update(marker)
    digest.update(len(payload).to_bytes(8, "big"))
    digest.update(payload)


def table_logical_sha256(conn: sqlite3.Connection, table: str) -> str:
    quoted = _quote_identifier(table)
    info = list(conn.execute("PRAGMA table_info(%s)" % quoted))
    if not info:
        raise RuntimeError("cannot hash missing table: %s" % table)
    columns = [str(row[1]) for row in info]
    primary = [
        str(row[1])
        for row in sorted(
            (row for row in info if int(row[5]) > 0),
            key=lambda row: int(row[5]),
        )
    ]
    select_sql = ",".join(_quote_identifier(value) for value in columns)
    if primary:
        order_sql = ",".join(_quote_identifier(value) for value in primary)
    else:
        order_sql = ",".join(
            "typeof(%s),quote(%s) COLLATE BINARY"
            % (_quote_identifier(value), _quote_identifier(value))
            for value in columns
        )
    digest = hashlib.sha256()
    _update_typed_digest(digest, table)
    for column in columns:
        _update_typed_digest(digest, column)
    for row in conn.execute(
        "SELECT %s FROM %s ORDER BY %s" % (select_sql, quoted, order_sql)
    ):
        digest.update(b"R")
        for value in row:
            _update_typed_digest(digest, value)
    return digest.hexdigest()


def user_table_hashes(conn: sqlite3.Connection) -> Dict[str, str]:
    tables = [
        str(row[0])
        for row in conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        )
    ]
    return {table: table_logical_sha256(conn, table) for table in tables}


def schema_objects(conn: sqlite3.Connection) -> Dict[Tuple[str, str], Optional[str]]:
    return {
        (str(row[0]), str(row[1])): row[2]
        for row in conn.execute(
            """
            SELECT type,name,sql FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
            ORDER BY type,name
            """
        )
    }


SCHEMA_SQL = """
CREATE TABLE metadata_review_lineage_v2_3 (
    lineage_id                 INTEGER PRIMARY KEY CHECK(lineage_id=1),
    parent_db_path             TEXT NOT NULL,
    parent_sha256              TEXT NOT NULL CHECK(length(parent_sha256)=64),
    parent_byte_size           INTEGER NOT NULL,
    parent_schema_version      INTEGER NOT NULL,
    parent_metadata_version    TEXT NOT NULL,
    partial_manifest_path      TEXT NOT NULL,
    partial_manifest_sha256    TEXT NOT NULL CHECK(length(partial_manifest_sha256)=64),
    partial_manifest_version   TEXT NOT NULL,
    area_manifest_path         TEXT NOT NULL,
    area_manifest_sha256       TEXT NOT NULL CHECK(length(area_manifest_sha256)=64),
    area_manifest_version      TEXT NOT NULL,
    d2_manifest_path           TEXT NOT NULL,
    d2_manifest_sha256         TEXT NOT NULL CHECK(length(d2_manifest_sha256)=64),
    d2_manifest_version        TEXT NOT NULL,
    builder_version            TEXT NOT NULL,
    policy_version             TEXT NOT NULL,
    metadata_version           TEXT NOT NULL,
    schema_version             INTEGER NOT NULL,
    frozen_at                  TEXT NOT NULL,
    decision_counts_json       TEXT NOT NULL CHECK(json_valid(decision_counts_json)),
    scope_json                 TEXT NOT NULL CHECK(json_valid(scope_json))
);

CREATE TABLE article_partial_text_decisions_v2_3 (
    article_id                 INTEGER PRIMARY KEY REFERENCES source_articles(article_id),
    parser_version             TEXT NOT NULL,
    prose_sha256               TEXT NOT NULL CHECK(length(prose_sha256)=64),
    decision                   TEXT NOT NULL CHECK(decision IN ('accept','reject','review')),
    reason_code                TEXT NOT NULL,
    note                       TEXT NOT NULL,
    decided_by                 TEXT NOT NULL,
    decided_at                 TEXT NOT NULL,
    decision_policy_version    TEXT NOT NULL,
    hash_guard_matched         INTEGER NOT NULL CHECK(hash_guard_matched=1)
);

CREATE TABLE article_area_decisions_v2_3 (
    article_id                 INTEGER PRIMARY KEY REFERENCES source_articles(article_id),
    decision_type              TEXT NOT NULL CHECK(decision_type IN (
      'accept_area','keep_scoped_candidate',
      'keep_null_multi_or_conflict','reject_non_area'
    )),
    resolved_area_sqm          REAL,
    candidate_area_sqm         REAL,
    area_scope                 TEXT NOT NULL,
    confidence                 REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
    closure_status             TEXT NOT NULL CHECK(closure_status IN (
      'final','open_external_text_review'
    )),
    rationale_code             TEXT NOT NULL,
    parser_version             TEXT NOT NULL,
    area_raw_sha256            TEXT,
    description_prose_sha256   TEXT,
    html_sha256                TEXT,
    evidence_json              TEXT NOT NULL CHECK(json_valid(evidence_json)),
    decision_policy_version    TEXT NOT NULL,
    hash_guard_matched         INTEGER NOT NULL CHECK(hash_guard_matched=1),
    CHECK(area_raw_sha256 IS NULL OR length(area_raw_sha256)=64),
    CHECK(description_prose_sha256 IS NULL OR length(description_prose_sha256)=64),
    CHECK(html_sha256 IS NULL OR length(html_sha256)=64)
);

CREATE TABLE article_d2_decisions_v2_3 (
    article_id_a               INTEGER NOT NULL REFERENCES source_articles(article_id),
    article_id_b               INTEGER NOT NULL REFERENCES source_articles(article_id),
    building_id_a_before       TEXT NOT NULL REFERENCES buildings(building_id),
    building_id_b_before       TEXT NOT NULL REFERENCES buildings(building_id),
    source_candidate_kind      TEXT NOT NULL,
    source_score               REAL NOT NULL,
    component_id               TEXT NOT NULL,
    building_pair_id           TEXT NOT NULL,
    decision                   TEXT NOT NULL CHECK(decision IN ('merge','reject','defer')),
    decision_id                TEXT NOT NULL UNIQUE,
    approved                   INTEGER NOT NULL CHECK(approved=1),
    reviewer                   TEXT NOT NULL,
    reviewed_at                TEXT NOT NULL,
    identity_scope             TEXT NOT NULL,
    relation_type              TEXT NOT NULL,
    related_project            INTEGER NOT NULL CHECK(related_project IN (0,1)),
    related_relation           TEXT,
    related_group_id           TEXT,
    reason_code                TEXT NOT NULL,
    note                       TEXT NOT NULL,
    evidence_families_json     TEXT NOT NULL CHECK(json_valid(evidence_families_json)),
    evidence_family_count      INTEGER NOT NULL,
    hard_conflicts_json        TEXT NOT NULL CHECK(json_valid(hard_conflicts_json)),
    evidence_json              TEXT NOT NULL CHECK(json_valid(evidence_json)),
    guards_json                TEXT NOT NULL CHECK(json_valid(guards_json)),
    decision_policy_version    TEXT NOT NULL,
    hash_guard_matched         INTEGER NOT NULL CHECK(hash_guard_matched=1),
    PRIMARY KEY(article_id_a,article_id_b),
    CHECK(article_id_a < article_id_b)
);

CREATE TABLE article_metadata_resolution_v2_3 (
    article_id                    INTEGER PRIMARY KEY REFERENCES source_articles(article_id),
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
    reconciled_at                 TEXT NOT NULL,
    parent_resolution_sha256      TEXT NOT NULL CHECK(length(parent_resolution_sha256)=64),
    partial_decision_version      TEXT,
    area_decision_version         TEXT
);

CREATE TABLE article_match_reviews_v2_3 (
    article_id_a               INTEGER NOT NULL REFERENCES source_articles(article_id),
    article_id_b               INTEGER NOT NULL REFERENCES source_articles(article_id),
    source_candidate_kind      TEXT NOT NULL,
    source_score               REAL NOT NULL,
    source_status              TEXT NOT NULL,
    source_signals_json        TEXT NOT NULL CHECK(json_valid(source_signals_json)),
    building_id_a              TEXT NOT NULL REFERENCES buildings(building_id),
    building_id_b              TEXT NOT NULL REFERENCES buildings(building_id),
    parent_decision_status     TEXT NOT NULL,
    decision_status            TEXT NOT NULL CHECK(decision_status IN (
      'confirmed','pending','rejected','deferred'
    )),
    decision_id                TEXT NOT NULL UNIQUE,
    recommendation             TEXT NOT NULL,
    decision_source            TEXT NOT NULL,
    decision_reason_json       TEXT NOT NULL CHECK(json_valid(decision_reason_json)),
    article_kind_context_json  TEXT NOT NULL CHECK(json_valid(article_kind_context_json)),
    decision_version           TEXT NOT NULL,
    decided_at                 TEXT,
    PRIMARY KEY(article_id_a,article_id_b),
    CHECK(article_id_a < article_id_b)
);

CREATE TABLE building_redirects_v2_3 (
    source_building_id         TEXT PRIMARY KEY REFERENCES buildings(building_id),
    target_building_id         TEXT NOT NULL REFERENCES buildings(building_id),
    redirect_kind              TEXT NOT NULL CHECK(redirect_kind IN (
      'inherited','manual_merge','composed'
    )),
    decision_ids_json          TEXT NOT NULL CHECK(json_valid(decision_ids_json)),
    reason_json                TEXT NOT NULL CHECK(json_valid(reason_json)),
    decision_version           TEXT NOT NULL,
    created_at                 TEXT NOT NULL,
    CHECK(source_building_id <> target_building_id)
);

CREATE TABLE active_building_membership_v2_3 (
    article_id                 INTEGER PRIMARY KEY REFERENCES source_articles(article_id),
    building_id                TEXT NOT NULL REFERENCES buildings(building_id),
    parent_building_id         TEXT NOT NULL REFERENCES buildings(building_id),
    source_building_id         TEXT NOT NULL REFERENCES buildings(building_id),
    source_article_role        TEXT NOT NULL,
    membership_confidence      REAL NOT NULL CHECK(membership_confidence BETWEEN 0 AND 1),
    decision_method            TEXT NOT NULL
);

CREATE TABLE building_facets_v2_3 (
    facet_v2_3_id              INTEGER PRIMARY KEY,
    building_id                TEXT NOT NULL REFERENCES buildings(building_id),
    axis                       TEXT NOT NULL,
    value                      TEXT NOT NULL,
    status                     TEXT NOT NULL CHECK(status IN ('candidate','confirmed','rejected')),
    role                       TEXT NOT NULL CHECK(role IN ('primary','secondary','facet')),
    confidence                 REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
    claim_count                INTEGER NOT NULL,
    article_count              INTEGER NOT NULL,
    direct_claim_count         INTEGER NOT NULL,
    supporting_claim_count     INTEGER NOT NULL,
    source_count               INTEGER NOT NULL,
    evidence_family_count      INTEGER NOT NULL,
    independence_group_count   INTEGER NOT NULL,
    max_priority               INTEGER NOT NULL,
    search_tier                TEXT NOT NULL CHECK(search_tier IN ('primary','secondary','hidden')),
    resolver_version           TEXT NOT NULL,
    parent_facet_ids_json      TEXT NOT NULL CHECK(json_valid(parent_facet_ids_json)),
    previous_statuses_json     TEXT NOT NULL CHECK(json_valid(previous_statuses_json)),
    status_changed             INTEGER NOT NULL CHECK(status_changed IN (0,1)),
    UNIQUE(building_id,axis,value)
);

CREATE TABLE building_facet_claims_v2_3 (
    facet_v2_3_id              INTEGER NOT NULL REFERENCES building_facets_v2_3(facet_v2_3_id),
    claim_id                   INTEGER NOT NULL REFERENCES attribute_claims(claim_id),
    weight                     REAL NOT NULL,
    evidence_family            TEXT NOT NULL,
    independence_key           TEXT NOT NULL,
    PRIMARY KEY(facet_v2_3_id,claim_id)
);

CREATE TABLE building_images_materialized_v2_3 (
    building_id                TEXT NOT NULL REFERENCES buildings(building_id),
    asset_key                  TEXT NOT NULL REFERENCES image_assets(asset_key),
    representative_url         TEXT NOT NULL,
    role_rank                  INTEGER NOT NULL,
    first_position             INTEGER NOT NULL,
    PRIMARY KEY(building_id,asset_key)
);

CREATE TABLE building_core_reconciled_v2_3 (
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
    area_candidates_json          TEXT NOT NULL CHECK(json_valid(area_candidates_json)),
    field_sources_json            TEXT NOT NULL CHECK(json_valid(field_sources_json)),
    core_conflicts_json           TEXT NOT NULL CHECK(json_valid(core_conflicts_json)),
    reconciliation_conflicts_json TEXT NOT NULL CHECK(json_valid(reconciliation_conflicts_json)),
    facet_conflicts_json          TEXT NOT NULL CHECK(json_valid(facet_conflicts_json)),
    article_kind_counts_json      TEXT NOT NULL CHECK(json_valid(article_kind_counts_json)),
    review_reasons_json           TEXT NOT NULL CHECK(json_valid(review_reasons_json)),
    metadata_needs_review         INTEGER NOT NULL CHECK(metadata_needs_review IN (0,1)),
    reconciliation_status         TEXT NOT NULL,
    identity_method               TEXT NOT NULL,
    resolution_version            TEXT NOT NULL,
    resolved_at                   TEXT NOT NULL
);

CREATE TABLE building_article_roles_v2_3 (
    article_id                 INTEGER PRIMARY KEY REFERENCES source_articles(article_id),
    building_id                TEXT NOT NULL REFERENCES buildings(building_id),
    parent_building_id         TEXT NOT NULL REFERENCES buildings(building_id),
    source_building_id         TEXT NOT NULL REFERENCES buildings(building_id),
    article_role               TEXT NOT NULL,
    article_kind               TEXT NOT NULL,
    article_kind_status        TEXT NOT NULL,
    role_confidence            REAL NOT NULL CHECK(role_confidence BETWEEN 0 AND 1),
    decision_method            TEXT NOT NULL,
    policy_version             TEXT NOT NULL
);

CREATE TABLE building_related_projects_v2_3 (
    article_id_a               INTEGER NOT NULL REFERENCES source_articles(article_id),
    article_id_b               INTEGER NOT NULL REFERENCES source_articles(article_id),
    building_id_a              TEXT NOT NULL REFERENCES buildings(building_id),
    building_id_b              TEXT NOT NULL REFERENCES buildings(building_id),
    relation_type              TEXT NOT NULL,
    related_group_id           TEXT,
    decision_id                TEXT NOT NULL REFERENCES article_d2_decisions_v2_3(decision_id),
    PRIMARY KEY(article_id_a,article_id_b),
    CHECK(article_id_a < article_id_b),
    CHECK(building_id_a <> building_id_b)
);

CREATE TABLE metadata_review_metrics_v2_3 (
    metric                     TEXT PRIMARY KEY,
    value_json                 TEXT NOT NULL CHECK(json_valid(value_json))
);

CREATE TABLE metadata_review_validation_v2_3 (
    check_name                 TEXT PRIMARY KEY,
    passed                     INTEGER NOT NULL CHECK(passed IN (0,1)),
    actual_json                TEXT NOT NULL CHECK(json_valid(actual_json)),
    expected_json              TEXT NOT NULL CHECK(json_valid(expected_json)),
    checked_at                 TEXT NOT NULL
);

CREATE INDEX idx_v23_match_status ON article_match_reviews_v2_3(decision_status,article_id_a);
CREATE INDEX idx_v23_redirect_target ON building_redirects_v2_3(target_building_id);
CREATE INDEX idx_v23_membership_building ON active_building_membership_v2_3(building_id,article_id);
CREATE INDEX idx_v23_facets_search ON building_facets_v2_3(axis,value,status,search_tier);
CREATE INDEX idx_v23_facets_building ON building_facets_v2_3(building_id,axis,status);
CREATE INDEX idx_v23_images_order ON building_images_materialized_v2_3(
  building_id,role_rank,first_position,asset_key
);
"""


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def validate_parent(
    conn: sqlite3.Connection,
    *,
    parent_sha256: str,
    expected_parent_sha256: str,
    compute_content_hashes: bool = True,
) -> Dict[str, Any]:
    if parent_sha256.casefold() != expected_parent_sha256.casefold():
        raise RuntimeError("parent SHA does not match the pinned v2.2 artifact")
    missing = sorted(REQUIRED_PARENT_TABLES - _table_names(conn))
    if missing:
        raise RuntimeError("parent DB is missing required tables: %s" % missing)
    if any(name.endswith("_v2_3") for name in _table_names(conn)):
        raise RuntimeError("parent already contains a v2.3 overlay")
    quick = conn.execute("PRAGMA quick_check").fetchone()[0]
    if quick != "ok":
        raise RuntimeError("parent quick_check failed: %s" % quick)
    user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if user_version != EXPECTED_PARENT_SCHEMA:
        raise RuntimeError("expected parent user_version 5, found %s" % user_version)
    lineage = conn.execute(
        "SELECT * FROM metadata_reconciliation_lineage_v2_2 WHERE lineage_id=1"
    ).fetchone()
    if lineage is None or lineage["metadata_version"] != EXPECTED_PARENT_METADATA_VERSION:
        raise RuntimeError("parent metadata lineage is not v2.2")
    failed = int(
        conn.execute(
            "SELECT COUNT(*) FROM metadata_reconciliation_validation_v2_2 WHERE passed<>1"
        ).fetchone()[0]
    )
    if failed:
        raise RuntimeError("parent contains failed v2.2 validation rows")
    counts = {
        "articles": int(conn.execute("SELECT COUNT(*) FROM source_articles").fetchone()[0]),
        "buildings": int(conn.execute("SELECT COUNT(*) FROM buildings").fetchone()[0]),
        "parent_active_buildings": int(
            conn.execute(
                "SELECT COUNT(*) FROM building_core_reconciled_v2_2 WHERE is_active=1"
            ).fetchone()[0]
        ),
        "parent_facets": int(
            conn.execute("SELECT COUNT(*) FROM building_facets_v2").fetchone()[0]
        ),
        "parent_images": int(
            conn.execute(
                "SELECT COUNT(*) FROM building_images_materialized_v2"
            ).fetchone()[0]
        ),
        "d2_pairs": int(
            conn.execute("SELECT COUNT(*) FROM article_match_reviews_v2").fetchone()[0]
        ),
    }
    pending_pairs = {
        (int(row[0]), int(row[1]))
        for row in conn.execute(
            """
            SELECT article_id_a,article_id_b FROM article_match_reviews_v2
            WHERE decision_status IN ('pending','deferred')
            ORDER BY article_id_a,article_id_b
            """
        )
    }
    partial_ids = {
        int(row[0])
        for row in conn.execute(
            "SELECT article_id FROM article_recrawl_evidence_v2_2 WHERE parse_status='partial'"
        )
    }
    area_review_ids = {
        int(row[0])
        for row in conn.execute(
            """
            SELECT article_id FROM article_metadata_resolution_v2_2
            WHERE COALESCE(json_extract(area_evidence_json,'$.needs_review'),0)=1
            """
        )
    }
    return {
        "counts": counts,
        "pending_pairs": pending_pairs,
        "partial_ids": partial_ids,
        "area_review_ids": area_review_ids,
        "table_hashes": user_table_hashes(conn) if compute_content_hashes else {},
        "schema_objects": schema_objects(conn),
        "lineage": dict(lineage),
    }


def _row_digest(row: Mapping[str, Any], columns: Sequence[str]) -> str:
    return sha256_bytes(
        canonical_json([row[column] for column in columns]).encode("utf-8")
    )


def _validated_partial_rows(
    conn: sqlite3.Connection,
    manifest: LoadedManifest,
    expected_ids: Iterable[int],
) -> List[Tuple[Any, ...]]:
    expected = {int(value) for value in expected_ids}
    actual = {int(value) for value in manifest.decisions}
    if expected != actual:
        raise RuntimeError(
            "partial decision ID set mismatch: missing=%s extra=%s"
            % (sorted(expected - actual), sorted(actual - expected))
        )
    rows: List[Tuple[Any, ...]] = []
    for article_id, decision in sorted(manifest.decisions.items()):
        evidence = conn.execute(
            """
            SELECT parser_version,details_json FROM article_recrawl_evidence_v2_2
            WHERE article_id=?
            """,
            (article_id,),
        ).fetchone()
        details = json.loads(evidence["details_json"] or "{}")
        validate_partial_guard(
            decision,
            parser_version=evidence["parser_version"],
            prose_sha256=details.get("prose_sha256"),
        )
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
                1,
            )
        )
    return rows


def materialize_partial_decisions(
    conn: sqlite3.Connection,
    manifest: LoadedManifest,
    expected_ids: Iterable[int],
) -> None:
    conn.executemany(
        """
        INSERT INTO article_partial_text_decisions_v2_3 VALUES (
          ?,?,?,?,?,?,?,?,?,?
        )
        """,
        _validated_partial_rows(conn, manifest, expected_ids),
    )


def _validated_area_rows(
    conn: sqlite3.Connection,
    manifest: LoadedManifest,
    expected_ids: Iterable[int],
) -> List[Tuple[Any, ...]]:
    expected = {int(value) for value in expected_ids}
    actual = {int(value) for value in manifest.decisions}
    if expected != actual:
        raise RuntimeError(
            "area decision ID set mismatch: missing=%s extra=%s"
            % (sorted(expected - actual), sorted(actual - expected))
        )
    rows: List[Tuple[Any, ...]] = []
    for article_id, decision in sorted(manifest.decisions.items()):
        evidence_row = conn.execute(
            """
            SELECT parser_version,recrawl_area_raw,description_prose,html_sha256
            FROM article_recrawl_evidence_v2_2 WHERE article_id=?
            """,
            (article_id,),
        ).fetchone()
        validate_area_guard(
            decision,
            parser_version=evidence_row["parser_version"],
            area_raw=evidence_row["recrawl_area_raw"],
            description_prose=evidence_row["description_prose"],
            html_sha256=evidence_row["html_sha256"],
        )
        evidence = decision["evidence"]
        rows.append(
            (
                article_id,
                decision["decision_type"],
                decision["resolved_area_sqm"],
                decision["candidate_area_sqm"],
                decision["area_scope"],
                decision["confidence"],
                decision["closure_status"],
                decision["rationale_code"],
                evidence["parser_version"],
                evidence.get("area_raw_sha256"),
                evidence.get("description_prose_sha256"),
                evidence.get("html_sha256"),
                canonical_json(evidence),
                decision["decision_policy_version"],
                1,
            )
        )
    return rows


def materialize_area_decisions(
    conn: sqlite3.Connection,
    manifest: LoadedManifest,
    expected_ids: Iterable[int],
) -> None:
    conn.executemany(
        """
        INSERT INTO article_area_decisions_v2_3 VALUES (
          ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
        )
        """,
        _validated_area_rows(conn, manifest, expected_ids),
    )


def _pending_component_ids(conn: sqlite3.Connection) -> Dict[int, str]:
    graph: Dict[int, set[int]] = defaultdict(set)
    for row in conn.execute(
        """
        SELECT article_id_a,article_id_b FROM article_match_reviews_v2
        WHERE decision_status IN ('pending','deferred')
        ORDER BY article_id_a,article_id_b
        """
    ):
        left, right = int(row[0]), int(row[1])
        graph[left].add(right)
        graph[right].add(left)
    output: Dict[int, str] = {}
    seen: set[int] = set()
    for start in sorted(graph):
        if start in seen:
            continue
        stack = [start]
        component: List[int] = []
        seen.add(start)
        while stack:
            current = stack.pop()
            component.append(current)
            for neighbor in sorted(graph[current], reverse=True):
                if neighbor not in seen:
                    seen.add(neighbor)
                    stack.append(neighbor)
        component_id = "d2c_%06d" % min(component)
        for article_id in component:
            output[article_id] = component_id
    return output


def _validated_d2_rows(
    conn: sqlite3.Connection,
    manifest: LoadedManifest,
) -> List[Tuple[Any, ...]]:
    rows = []
    component_ids = _pending_component_ids(conn)
    for pair, decision in sorted(manifest.decisions.items()):
        parent = conn.execute(
            """
            SELECT building_id_a,building_id_b,source_candidate_kind,source_score
            FROM article_match_reviews_v2
            WHERE article_id_a=? AND article_id_b=?
            """,
            pair,
        ).fetchone()
        if parent is None:
            raise RuntimeError("D2 pair is absent from the parent review table: %s" % (pair,))
        expected_pair_fields = {
            "building_id_a_before": str(parent["building_id_a"]),
            "building_id_b_before": str(parent["building_id_b"]),
            "source_candidate_kind": str(parent["source_candidate_kind"]),
        }
        for field, expected in expected_pair_fields.items():
            if str(decision.get(field) or "") != expected:
                raise RuntimeError("D2 pair %s %s guard failed" % (pair, field))
        if float(decision.get("source_score")) != float(parent["source_score"]):
            raise RuntimeError("D2 pair %s source_score guard failed" % (pair,))
        expected_building_pair = "|".join(
            sorted((str(parent["building_id_a"]), str(parent["building_id_b"])))
        )
        if decision.get("building_pair_id") != expected_building_pair:
            raise RuntimeError("D2 pair %s building_pair_id guard failed" % (pair,))
        if (
            component_ids.get(pair[0]) != decision.get("component_id")
            or component_ids.get(pair[1]) != decision.get("component_id")
        ):
            raise RuntimeError("D2 pair %s component_id guard failed" % (pair,))

        for side, article_id in (("article_a", pair[0]), ("article_b", pair[1])):
            evidence = conn.execute(
                """
                SELECT e.article_id,e.source_url,e.description_prose,
                       e.recrawl_abstract,e.parser_version,e.html_sha256,
                       e.snapshot_path,s.source_row_hash
                FROM article_recrawl_evidence_v2_2 e
                JOIN source_articles s ON s.article_id=e.article_id
                WHERE e.article_id=?
                """,
                (article_id,),
            ).fetchone()
            if evidence is None:
                raise RuntimeError("D2 guard article is missing: %s" % article_id)
            validate_d2_guard(
                decision,
                side=side,
                article_id=article_id,
                source_url=evidence["source_url"],
                parser_version=evidence["parser_version"],
                description_prose=evidence["description_prose"],
                recrawl_abstract=evidence["recrawl_abstract"],
                html_sha256=evidence["html_sha256"],
                source_row_hash=evidence["source_row_hash"],
                snapshot_path=evidence["snapshot_path"],
            )
        rows.append(
            (
                pair[0], pair[1], decision["building_id_a_before"],
                decision["building_id_b_before"], decision["source_candidate_kind"],
                decision["source_score"], decision["component_id"],
                decision["building_pair_id"], decision["decision"],
                decision["decision_id"],
                1, decision["reviewer"], decision["reviewed_at"],
                decision["identity_scope"], decision["relation_type"],
                int(decision["related_project"]), decision["related_relation"],
                decision["related_group_id"],
                decision["reason_code"], decision["note"],
                canonical_json(decision["evidence_families"]),
                decision["evidence_family_count"],
                canonical_json(decision["hard_conflicts"]),
                canonical_json(decision["evidence"]),
                canonical_json(decision["guards"]),
                decision["decision_policy_version"], 1,
            )
        )
    return rows


def materialize_d2_decisions(
    conn: sqlite3.Connection,
    manifest: LoadedManifest,
) -> None:
    conn.executemany(
        """
        INSERT INTO article_d2_decisions_v2_3(
          article_id_a,article_id_b,building_id_a_before,building_id_b_before,
          source_candidate_kind,source_score,component_id,building_pair_id,
          decision,decision_id,approved,reviewer,reviewed_at,identity_scope,
          relation_type,related_project,related_relation,related_group_id,
          reason_code,note,evidence_families_json,evidence_family_count,
          hard_conflicts_json,evidence_json,guards_json,
          decision_policy_version,hash_guard_matched
        ) VALUES (
          ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
        )
        """,
        _validated_d2_rows(conn, manifest),
    )


def _apply_article_decisions(
    parent: Mapping[str, Any],
    partial: Optional[Mapping[str, Any]],
    area: Optional[Mapping[str, Any]],
    frozen_at: str,
) -> Tuple[Any, ...]:
    values = {column: parent[column] for column in ARTICLE_RESOLUTION_COLUMNS}
    field_sources = json.loads(values["field_sources_json"] or "{}")
    conflicts = json.loads(values["field_conflicts_json"] or "{}")
    reasons = set(json.loads(values["review_reasons_json"] or "[]"))

    if partial is not None:
        reasons = {reason for reason in reasons if not str(reason).startswith("description_")}
        action = partial["decision"]
        if action == "accept":
            values["description_source"] = "recrawl"
            values["description_status"] = "manual_accept_fallback"
            values["description_publishable"] = 1
        elif action == "reject":
            values["description_source"] = "none"
            values["description_status"] = "manual_reject_fallback"
            values["description_publishable"] = 0
        else:
            values["description_source"] = "recrawl_candidate"
            values["description_status"] = "manual_review_fallback"
            values["description_publishable"] = 0
            reasons.add("description_manual_review_fallback")
        field_sources["description"] = {
            "source": values["description_source"],
            "status": values["description_status"],
            "publishable": bool(values["description_publishable"]),
            "decision": {
                "decision": action,
                "reason_code": partial["reason_code"],
                "policy_version": partial["decision_policy_version"],
            },
        }

    if area is not None:
        reasons = {reason for reason in reasons if not str(reason).startswith("area_")}
        conflicts.pop("area", None)
        decision_type = area["decision_type"]
        is_open = area["closure_status"] != "final"
        values["area_sqm"] = area["resolved_area_sqm"]
        values["area_candidate_sqm"] = area["candidate_area_sqm"]
        values["area_confidence"] = area["confidence"]
        if decision_type == "accept_area":
            evidence_status = "manual_accepted_generic"
            unit_kind = "sqm"
            area_source = "manual_review"
            area_status = "manual_accepted"
        elif decision_type == "keep_scoped_candidate":
            evidence_status = "manual_scoped_candidate"
            unit_kind = "candidate_scope"
            area_source = "manual_candidate"
            area_status = "manual_scoped_candidate"
        elif decision_type == "reject_non_area":
            evidence_status = "manual_rejected_non_area"
            unit_kind = "non_area"
            area_source = "manual_abstention"
            area_status = "manual_rejected_non_area"
        else:
            evidence_status = "manual_null_conflict"
            unit_kind = "ambiguous"
            area_source = "manual_abstention"
            area_status = "manual_null_conflict"
        if is_open:
            reasons.add("area_manual_open_external_text_review")
        parent_evidence = json.loads(values["area_evidence_json"] or "{}")
        manual_evidence = {
            "value_sqm": area["resolved_area_sqm"],
            "candidate_sqm": area["candidate_area_sqm"],
            "status": evidence_status,
            "unit_kind": unit_kind,
            "confidence": area["confidence"],
            "needs_review": is_open,
            "details": {
                "decision_type": decision_type,
                "closure_status": area["closure_status"],
                "area_scope": area["area_scope"],
                "rationale_code": area["rationale_code"],
                "policy_version": area["decision_policy_version"],
                "parent_parser_evidence": parent_evidence,
            },
        }
        values["area_evidence_status"] = evidence_status
        values["area_unit_kind"] = unit_kind
        values["area_source"] = area_source
        values["area_status"] = area_status
        values["area_evidence_json"] = canonical_json(manual_evidence)
        field_sources["area"] = {
            "source": area_source,
            "status": area_status,
            "decision": {
                "decision_type": decision_type,
                "area_scope": area["area_scope"],
                "closure_status": area["closure_status"],
                "policy_version": area["decision_policy_version"],
            },
        }

    values["field_sources_json"] = canonical_json(field_sources)
    values["field_conflicts_json"] = canonical_json(conflicts)
    values["review_reasons_json"] = canonical_json(sorted(reasons))
    values["metadata_needs_review"] = int(bool(reasons))
    if reasons:
        values["reconciliation_status"] = "review"
    elif any(
        values[column] is None
        for column in ("resolved_name", "location_country", "location_city", "project_year", "area_sqm")
    ):
        values["reconciliation_status"] = "complete_with_nulls"
    else:
        values["reconciliation_status"] = "complete"
    if partial is not None or area is not None:
        values["policy_version"] = POLICY_VERSION
        values["reconciled_at"] = frozen_at
    return tuple(values[column] for column in ARTICLE_RESOLUTION_COLUMNS)


def populate_article_resolutions(
    conn: sqlite3.Connection,
    *,
    partial_manifest: LoadedManifest,
    area_manifest: LoadedManifest,
    frozen_at: str,
) -> int:
    rows: List[Tuple[Any, ...]] = []
    for parent_row in conn.execute(
        "SELECT * FROM article_metadata_resolution_v2_2 ORDER BY article_id"
    ):
        parent = dict(parent_row)
        article_id = int(parent["article_id"])
        partial = partial_manifest.decisions.get(article_id)
        area = area_manifest.decisions.get(article_id)
        resolved = _apply_article_decisions(parent, partial, area, frozen_at)
        rows.append(
            resolved
            + (
                _row_digest(parent, ARTICLE_RESOLUTION_COLUMNS),
                partial_manifest.version if partial else None,
                area_manifest.version if area else None,
            )
        )
    placeholders = ",".join("?" for _ in range(len(ARTICLE_RESOLUTION_COLUMNS) + 3))
    conn.executemany(
        "INSERT INTO article_metadata_resolution_v2_3 VALUES (%s)" % placeholders,
        rows,
    )
    return len(rows)


def populate_match_reviews(
    conn: sqlite3.Connection,
    *,
    d2_manifest: LoadedManifest,
    frozen_at: str,
) -> Dict[str, int]:
    rows: List[Tuple[Any, ...]] = []
    counts: Dict[str, int] = defaultdict(int)
    for parent in conn.execute(
        "SELECT * FROM article_match_reviews_v2 ORDER BY article_id_a,article_id_b"
    ):
        pair = (int(parent["article_id_a"]), int(parent["article_id_b"]))
        decision = d2_manifest.decisions.get(pair)
        if decision is None:
            status = parent["decision_status"]
            decision_id = parent["decision_id"]
            recommendation = parent["recommendation"]
            source = parent["decision_source"]
            reason = json.loads(parent["decision_reason_json"] or "{}")
            decision_version = parent["decision_version"]
            decided_at = parent["decided_at"]
        else:
            action = decision["decision"]
            if action == "merge":
                status, recommendation = "confirmed", "merge"
            elif action == "reject":
                status, recommendation = "rejected", "keep_separate"
            else:
                status, recommendation = "deferred", "review_later"
            decision_id = decision["decision_id"]
            source = "versioned_manual_decision_v2_3"
            reason = {
                "manual": decision,
                "source_candidate_kind": parent["source_candidate_kind"],
            }
            decision_version = d2_manifest.version
            decided_at = decision["reviewed_at"]
        rows.append(
            (
                pair[0], pair[1], parent["source_candidate_kind"],
                parent["source_score"], parent["source_status"],
                parent["source_signals_json"], parent["building_id_a"],
                parent["building_id_b"], parent["decision_status"], status,
                decision_id, recommendation, source, canonical_json(reason),
                parent["article_kind_context_json"], decision_version,
                decided_at,
            )
        )
        counts[status] += 1
    conn.executemany(
        """
        INSERT INTO article_match_reviews_v2_3 VALUES (
          ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
        )
        """,
        rows,
    )
    counts["total"] = len(rows)
    return dict(counts)


def _terminal_parent_redirects(conn: sqlite3.Connection) -> Dict[str, str]:
    direct = {
        str(row["building_id"]): (
            str(row["redirect_to"]) if row["redirect_to"] is not None else str(row["building_id"])
        )
        for row in conn.execute(
            "SELECT building_id,redirect_to FROM building_core_reconciled_v2_2"
        )
    }

    def terminal(value: str) -> str:
        seen: set[str] = set()
        current = value
        while direct.get(current, current) != current:
            if current in seen:
                raise RuntimeError("parent building redirect cycle")
            seen.add(current)
            current = direct[current]
        return current

    return {building: terminal(building) for building in direct}


def populate_identity_and_membership(
    conn: sqlite3.Connection,
    *,
    d2_manifest: LoadedManifest,
    frozen_at: str,
) -> Tuple[Dict[str, str], Dict[str, str], Dict[str, Any]]:
    parent_rows = list(
        conn.execute(
            "SELECT * FROM active_building_membership_v2 ORDER BY article_id"
        )
    )
    article_to_parent = {
        int(row["article_id"]): str(row["building_id"]) for row in parent_rows
    }
    active_parent_buildings = sorted(set(article_to_parent.values()))
    component_map = resolve_identity_components(
        active_parent_buildings,
        article_to_parent,
        d2_manifest.decisions,
    )

    # Every inherited pending/deferred/rejected pair is also a component
    # barrier.  A manual merge may not collapse one transitively.
    for row in conn.execute(
        "SELECT article_id_a,article_id_b,decision_status FROM article_match_reviews_v2_3"
    ):
        if row["decision_status"] == "confirmed":
            continue
        left = component_map[article_to_parent[int(row["article_id_a"])]]
        right = component_map[article_to_parent[int(row["article_id_b"])]]
        if left == right:
            raise ValueError(
                "non-merge D2 pair collapsed through a merge component: %s/%s"
                % (row["article_id_a"], row["article_id_b"])
            )

    parent_terminal = _terminal_parent_redirects(conn)
    all_buildings = [
        str(row[0]) for row in conn.execute("SELECT building_id FROM buildings")
    ]
    final_by_building: Dict[str, str] = {}
    for building_id in all_buildings:
        parent_active = parent_terminal.get(building_id, building_id)
        final_by_building[building_id] = component_map.get(parent_active, parent_active)

    merge_decisions_by_component: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for pair, decision in sorted(d2_manifest.decisions.items()):
        if decision["decision"] != "merge":
            continue
        component = component_map[article_to_parent[pair[0]]]
        merge_decisions_by_component[component].append(decision)

    redirect_rows: List[Tuple[Any, ...]] = []
    for source in sorted(all_buildings):
        target = final_by_building[source]
        if source == target:
            continue
        inherited_target = parent_terminal.get(source, source)
        if inherited_target != source and target == inherited_target:
            kind = "inherited"
        elif inherited_target == source:
            kind = "manual_merge"
        else:
            kind = "composed"
        decisions = merge_decisions_by_component.get(target, [])
        redirect_rows.append(
            (
                source,
                target,
                kind,
                canonical_json(sorted(decision["decision_id"] for decision in decisions)),
                canonical_json(
                    {
                        "parent_terminal": inherited_target,
                        "survivor_policy": "minimum_stable_building_id",
                        "merge_pairs": sorted(
                            [
                                [decision["article_id_a"], decision["article_id_b"]]
                                for decision in decisions
                            ]
                        ),
                    }
                ),
                d2_manifest.version,
                frozen_at,
            )
        )
    conn.executemany(
        "INSERT INTO building_redirects_v2_3 VALUES (?,?,?,?,?,?,?)",
        redirect_rows,
    )

    membership_rows = []
    for row in parent_rows:
        parent_building = str(row["building_id"])
        membership_rows.append(
            (
                row["article_id"], component_map[parent_building], parent_building,
                row["source_building_id"], row["source_article_role"],
                row["membership_confidence"],
                (
                    "v2_3_manual_component"
                    if component_map[parent_building] != parent_building
                    else "v2_2_membership_preserved"
                ),
            )
        )
    conn.executemany(
        "INSERT INTO active_building_membership_v2_3 VALUES (?,?,?,?,?,?,?)",
        membership_rows,
    )

    relation_rows = []
    for pair, decision in sorted(d2_manifest.decisions.items()):
        if not decision["related_project"] or decision["decision"] == "merge":
            continue
        left = component_map[article_to_parent[pair[0]]]
        right = component_map[article_to_parent[pair[1]]]
        if left == right:
            raise ValueError("related projects cannot resolve to the same building")
        relation_rows.append(
            (
                pair[0], pair[1], left, right, decision["related_relation"],
                decision["related_group_id"], decision["decision_id"],
            )
        )
    conn.executemany(
        "INSERT INTO building_related_projects_v2_3 VALUES (?,?,?,?,?,?,?)",
        relation_rows,
    )
    return component_map, final_by_building, {
        "memberships": len(membership_rows),
        "redirects": len(redirect_rows),
        "relations": len(relation_rows),
        "active_buildings": len(set(component_map.values())),
    }


def populate_facets(
    conn: sqlite3.Connection,
    component_map: Mapping[str, str],
) -> Dict[str, int]:
    parent_sources: Dict[Tuple[str, str, str], Dict[str, set[Any]]] = defaultdict(
        lambda: {"ids": set(), "statuses": set()}
    )
    for row in conn.execute(
        "SELECT facet_v2_id,building_id,axis,value,status FROM building_facets_v2"
    ):
        building_id = component_map.get(str(row["building_id"]), str(row["building_id"]))
        key = (building_id, row["axis"], row["value"])
        parent_sources[key]["ids"].add(int(row["facet_v2_id"]))
        parent_sources[key]["statuses"].add(str(row["status"]))

    groups: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    placeholders = ",".join("?" for _ in EXCLUDED_FACET_AXES)
    query = """
        SELECT
          m.building_id,c.claim_id,c.article_id,c.axis,
          c.value_normalized AS value,c.confidence,c.source_ref,c.search_tier,
          COALESCE(json_extract(c.details_json,'$.priority'),0) AS priority,
          ce.mapping_kind,ce.evidence_family,ce.independence_key
        FROM active_building_membership_v2_3 m
        JOIN attribute_claims c ON c.article_id=m.article_id
        JOIN claim_evidence_v2 ce ON ce.claim_id=c.claim_id
        WHERE c.scope='building' AND c.polarity='positive'
          AND c.axis NOT IN (%s)
          AND ce.mapping_kind IN ('direct','supporting')
        ORDER BY m.building_id,c.axis,c.value_normalized,c.claim_id
    """ % placeholders
    for row in conn.execute(query, EXCLUDED_FACET_AXES):
        key = (str(row["building_id"]), str(row["axis"]), str(row["value"]))
        group = groups.setdefault(
            key,
            {
                "claims": [], "articles": set(), "direct_confidences": [],
                "supporting_keys": set(), "supporting_articles": set(),
                "all_keys": set(), "families": set(), "sources": set(),
                "confidence": 0.0, "max_priority": 0,
                "search_tier": "hidden", "direct_count": 0,
                "supporting_count": 0,
            },
        )
        confidence = float(row["confidence"])
        group["claims"].append(
            (
                int(row["claim_id"]), confidence, row["evidence_family"],
                row["independence_key"],
            )
        )
        group["articles"].add(int(row["article_id"]))
        group["all_keys"].add(row["independence_key"])
        group["families"].add(row["evidence_family"])
        group["sources"].add(row["source_ref"] or "claim:%s" % row["claim_id"])
        group["confidence"] = max(group["confidence"], confidence)
        group["max_priority"] = max(group["max_priority"], int(row["priority"] or 0))
        if SEARCH_TIER_RANK[row["search_tier"]] > SEARCH_TIER_RANK[group["search_tier"]]:
            group["search_tier"] = row["search_tier"]
        if row["mapping_kind"] == "direct":
            group["direct_count"] += 1
            group["direct_confidences"].append(confidence)
        else:
            group["supporting_count"] += 1
            group["supporting_keys"].add(row["independence_key"])
            group["supporting_articles"].add(int(row["article_id"]))

    facet_rows: List[List[Any]] = []
    claims_by_facet: Dict[int, Sequence[Tuple[Any, ...]]] = {}
    for facet_id, key in enumerate(sorted(groups), start=1):
        group = groups[key]
        status = facet_status_v2(
            group["direct_confidences"], group["supporting_keys"],
            group["confidence"],
            supporting_article_count=len(group["supporting_articles"]),
        )
        previous = parent_sources.get(key, {"ids": set(), "statuses": set()})
        previous_statuses = sorted(previous["statuses"])
        changed = int(bool(previous_statuses) and previous_statuses != [status])
        facet_rows.append(
            [
                facet_id, key[0], key[1], key[2], status, "facet",
                group["confidence"], len(group["claims"]), len(group["articles"]),
                group["direct_count"], group["supporting_count"],
                len(group["sources"]), len(group["families"]),
                len(group["all_keys"]), group["max_priority"],
                group["search_tier"], FACET_POLICY_VERSION,
                canonical_json(sorted(previous["ids"])),
                canonical_json(previous_statuses), changed,
            ]
        )
        claims_by_facet[facet_id] = group["claims"]

    # Multi-value axes have a primary only when exactly one value is confirmed.
    by_axis: Dict[Tuple[str, str], List[List[Any]]] = defaultdict(list)
    for row in facet_rows:
        if row[4] == "confirmed":
            by_axis[(row[1], row[2])].append(row)
    scalar_conflicts = 0
    for (_, axis), candidates in by_axis.items():
        if axis in ("program", "typology"):
            for candidate in candidates:
                candidate[5] = "secondary"
            if len(candidates) == 1:
                candidates[0][5] = "primary"
        elif axis in SCALAR_AXES:
            for candidate in candidates:
                candidate[5] = "secondary"
            direct = [candidate for candidate in candidates if int(candidate[9]) > 0]
            if len(direct) == 1:
                direct[0][5] = "primary"
            elif not direct and len(candidates) == 1:
                candidates[0][5] = "primary"
            elif len(candidates) > 1:
                scalar_conflicts += 1

    conn.executemany(
        "INSERT INTO building_facets_v2_3 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [tuple(row) for row in facet_rows],
    )
    link_rows = []
    for facet_id in sorted(claims_by_facet):
        for claim_id, weight, family, independence_key in claims_by_facet[facet_id]:
            link_rows.append((facet_id, claim_id, weight, family, independence_key))
    conn.executemany(
        "INSERT INTO building_facet_claims_v2_3 VALUES (?,?,?,?,?)",
        link_rows,
    )
    return {
        "facets": len(facet_rows),
        "facet_claim_links": len(link_rows),
        "confirmed": sum(row[4] == "confirmed" for row in facet_rows),
        "candidate": sum(row[4] == "candidate" for row in facet_rows),
        "scalar_conflicts": scalar_conflicts,
    }


def materialize_building_images(conn: sqlite3.Connection) -> int:
    conn.execute(
        """
        INSERT INTO building_images_materialized_v2_3(
          building_id,asset_key,representative_url,role_rank,first_position
        )
        WITH ranked AS (
          SELECT
            m.building_id,aio.asset_key,iu.url AS representative_url,
            CASE aio.role WHEN 'cover' THEN 0 ELSE 1 END AS role_rank,
            aio.position AS first_position,
            ROW_NUMBER() OVER (
              PARTITION BY m.building_id,aio.asset_key
              ORDER BY CASE aio.role WHEN 'cover' THEN 0 ELSE 1 END,
                       aio.position,iu.url_id
            ) AS rn
          FROM active_building_membership_v2_3 m
          JOIN article_image_occurrences aio ON aio.article_id=m.article_id
          JOIN image_urls iu ON iu.url_id=aio.url_id
        )
        SELECT building_id,asset_key,representative_url,role_rank,first_position
        FROM ranked WHERE rn=1
        ORDER BY building_id,role_rank,first_position,asset_key
        """
    )
    return int(
        conn.execute("SELECT COUNT(*) FROM building_images_materialized_v2_3").fetchone()[0]
    )


def _unique_values(
    rows: Sequence[Mapping[str, Any]],
    column: str,
    normalizer,
) -> List[Any]:
    output: Dict[Any, Any] = {}
    for row in rows:
        value = row[column]
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        key = normalizer(value)
        if key is None or key == "":
            continue
        output.setdefault(key, value)
    return list(output.values())


def _merge_json_conflicts(
    rows: Iterable[Mapping[str, Any]],
    column: str,
) -> Dict[str, Any]:
    merged: Dict[str, List[Any]] = {}
    for row in rows:
        payload = json.loads(row[column] or "{}")
        for key, value in payload.items():
            values = value if isinstance(value, list) else [value]
            target = merged.setdefault(str(key), [])
            for item in values:
                if item not in target:
                    target.append(item)
    return {key: value for key, value in sorted(merged.items())}


def populate_building_core(
    conn: sqlite3.Connection,
    *,
    final_by_building: Mapping[str, str],
    area_decision_ids: Iterable[int],
    frozen_at: str,
) -> Dict[str, int]:
    area_decision_ids = {int(value) for value in area_decision_ids}
    members: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    query = """
      SELECT
        m.article_id,m.building_id,m.parent_building_id,m.source_building_id,
        r.resolved_name,r.resolved_name_normalized,r.location_country,
        r.location_city,r.project_year,r.area_sqm,r.area_candidate_sqm,
        r.area_confidence,r.area_evidence_json,r.name_source,r.name_status,
        r.country_source,r.country_status,r.city_source,r.city_status,
        r.year_source,r.year_status,r.area_source,r.area_status,
        r.description_source,r.description_status,r.description_publishable,
        r.metadata_needs_review,r.review_reasons_json,
        e.description_quality,
        a.content_score,a.image_count,a.tag_count,a.description_ui_markers,
        ak.article_kind,ak.status AS article_kind_status,ak.confidence AS article_kind_confidence
      FROM active_building_membership_v2_3 m
      JOIN article_metadata_resolution_v2_3 r ON r.article_id=m.article_id
      JOIN article_recrawl_evidence_v2_2 e ON e.article_id=m.article_id
      JOIN source_articles a ON a.article_id=m.article_id
      JOIN article_kind_resolution_v2 ak ON ak.article_id=m.article_id
      ORDER BY m.building_id,m.article_id
    """
    for row in conn.execute(query):
        members[str(row["building_id"])].append(dict(row))

    parent_core = {
        str(row["building_id"]): dict(row)
        for row in conn.execute(
            "SELECT * FROM building_core_reconciled_v2_2 ORDER BY building_id"
        )
    }
    building_intrinsic_review = {
        str(row["building_id"]): bool(row["needs_review"])
        for row in conn.execute("SELECT building_id,needs_review FROM buildings")
    }

    confirmed_facets: Dict[Tuple[str, str], List[sqlite3.Row]] = defaultdict(list)
    primary_facets: Dict[Tuple[str, str], sqlite3.Row] = {}
    for row in conn.execute(
        """
        SELECT * FROM building_facets_v2_3
        WHERE status='confirmed' ORDER BY building_id,axis,value
        """
    ):
        key = (str(row["building_id"]), str(row["axis"]))
        confirmed_facets[key].append(row)
        if row["role"] == "primary":
            primary_facets[key] = row

    facet_conflicts: Dict[str, Dict[str, List[str]]] = defaultdict(dict)
    for (building_id, axis), facet_rows in confirmed_facets.items():
        if axis in SCALAR_AXES and len(facet_rows) > 1 and (building_id, axis) not in primary_facets:
            facet_conflicts[building_id][axis] = [str(row["value"]) for row in facet_rows]

    d2_review_buildings: set[str] = set()
    for row in conn.execute(
        """
        SELECT ma.building_id AS building_id_a,mb.building_id AS building_id_b
        FROM article_match_reviews_v2_3 mr
        JOIN active_building_membership_v2_3 ma ON ma.article_id=mr.article_id_a
        JOIN active_building_membership_v2_3 mb ON mb.article_id=mr.article_id_b
        WHERE mr.decision_status IN ('pending','deferred')
        """
    ):
        d2_review_buildings.add(str(row["building_id_a"]))
        d2_review_buildings.add(str(row["building_id_b"]))

    output: List[Tuple[Any, ...]] = []
    active_count = 0
    review_count = 0
    conflict_count = 0
    for building_id in sorted(parent_core):
        final_id = final_by_building.get(building_id, building_id)
        is_active = int(final_id == building_id)
        parent = parent_core[building_id]
        component_members = members.get(building_id, []) if is_active else []
        if not is_active:
            output.append(
                (
                    building_id, 0, final_id, 0, parent["primary_article_id"],
                    parent["name"], parent["name_normalized"],
                    parent["location_country"], parent["location_city"],
                    parent["location_resolution_method"],
                    parent["location_confidence"], parent["project_year"],
                    parent["year_kind"], parent["area_sqm"], "[]",
                    canonical_json({"all": "inactive_redirect_parent_preserved"}),
                    parent["core_conflicts_json"],
                    parent["reconciliation_conflicts_json"], "{}", "{}",
                    canonical_json(["inactive_redirect"]), 0,
                    "inactive_redirect", "terminal_redirect", POLICY_VERSION,
                    frozen_at,
                )
            )
            continue

        active_count += 1
        parent_component_ids = sorted(
            {str(member["parent_building_id"]) for member in component_members}
        )
        merged_component = len(parent_component_ids) > 1
        parent_component_rows = [parent_core[value] for value in parent_component_ids]

        if not merged_component:
            primary_article_id = int(parent["primary_article_id"])
            if primary_article_id not in {int(value["article_id"]) for value in component_members}:
                raise RuntimeError("parent primary is not an active member: %s" % building_id)
        else:
            primary_member = sorted(
                component_members,
                key=lambda row: (
                    -int(bool(row["description_publishable"])),
                    -(row["description_quality"] == "dom_prose_paragraphs"),
                    -float(row["content_score"] or 0.0),
                    -int(row["image_count"] or 0),
                    -int(row["tag_count"] or 0),
                    int(row["article_id"]),
                ),
            )[0]
            primary_article_id = int(primary_member["article_id"])
        primary = next(
            value for value in component_members if int(value["article_id"]) == primary_article_id
        )

        reconciliation_conflicts: Dict[str, Any] = {}
        core_conflicts = _merge_json_conflicts(parent_component_rows, "core_conflicts_json")
        impacted_area = any(int(member["article_id"]) in area_decision_ids for member in component_members)

        if merged_component:
            names = _unique_values(component_members, "resolved_name", normalize_identity_text)
            name = primary["resolved_name"] or (names[0] if len(names) == 1 else None)
            name_normalized = normalize_identity_text(name) if name else None
            if len(names) > 1:
                reconciliation_conflicts["name"] = sorted(str(value) for value in names)
                core_conflicts["name"] = sorted(str(value) for value in names)

            countries = _unique_values(
                component_members, "location_country", lambda value: str(value).casefold()
            )
            cities = _unique_values(component_members, "location_city", normalize_identity_text)
            if len(countries) <= 1 and len(cities) <= 1:
                country = countries[0] if countries else None
                city = cities[0] if cities else None
                if country is None and city is None:
                    location_method, location_confidence = "unresolved", 0.0
                elif country is None or city is None:
                    location_method, location_confidence = "v2_3_merge_partial_consensus", 0.8
                else:
                    location_method, location_confidence = "v2_3_merge_member_consensus", 0.95
            else:
                country = city = None
                location_method, location_confidence = "v2_3_merge_conflict_abstained", 0.0
                if len(countries) > 1:
                    reconciliation_conflicts["location_country"] = sorted(
                        str(value) for value in countries
                    )
                if len(cities) > 1:
                    reconciliation_conflicts["location_city"] = sorted(
                        str(value) for value in cities
                    )

            years = _unique_values(component_members, "project_year", int)
            if len(years) == 1:
                project_year, year_kind = int(years[0]), "v2_3_merge_consensus"
            elif not years:
                project_year, year_kind = None, "unknown"
            else:
                project_year, year_kind = None, "conflict_abstained"
                reconciliation_conflicts["project_year"] = sorted(int(value) for value in years)
        else:
            name, name_normalized = parent["name"], parent["name_normalized"]
            country, city = parent["location_country"], parent["location_city"]
            location_method = parent["location_resolution_method"]
            location_confidence = float(parent["location_confidence"])
            project_year, year_kind = parent["project_year"], parent["year_kind"]

        if merged_component or impacted_area:
            areas = _unique_values(
                component_members, "area_sqm", lambda value: round(float(value), 4)
            )
            core_conflicts.pop("area_sqm", None)
            if len(areas) == 1:
                area_sqm = float(areas[0])
            elif not areas:
                area_sqm = None
            else:
                area_sqm = None
                reconciliation_conflicts["area_sqm"] = sorted(float(value) for value in areas)
                core_conflicts["area_sqm"] = sorted(float(value) for value in areas)
        else:
            area_sqm = parent["area_sqm"]

        area_candidates = []
        for member in component_members:
            if member["area_candidate_sqm"] is None:
                continue
            evidence = json.loads(member["area_evidence_json"] or "{}")
            details = evidence.get("details", {}) if isinstance(evidence, dict) else {}
            area_candidates.append(
                {
                    "article_id": int(member["article_id"]),
                    "value_sqm": float(member["area_candidate_sqm"]),
                    "scope": details.get("area_scope") or details.get("scope"),
                    "confidence": float(member["area_confidence"]),
                    "status": member["area_status"],
                }
            )

        review_reasons: set[str] = set()
        for parent_id in parent_component_ids:
            if building_intrinsic_review.get(parent_id, False):
                review_reasons.add("intrinsic_building_review")
        for member in component_members:
            if member["metadata_needs_review"]:
                review_reasons.update(json.loads(member["review_reasons_json"] or "[]"))
            if member["article_kind_status"] == "ambiguous":
                review_reasons.add("article_kind_ambiguous")
        if building_id in d2_review_buildings:
            review_reasons.add("d2_deferred_or_pending")
        current_facet_conflicts = facet_conflicts.get(building_id, {})
        if current_facet_conflicts:
            review_reasons.add("facet_scalar_conflict")
        if reconciliation_conflicts:
            review_reasons.add("building_core_conflict")

        kind_counts: Dict[str, int] = defaultdict(int)
        for member in component_members:
            key = "%s:%s" % (member["article_kind"], member["article_kind_status"])
            kind_counts[key] += 1
        field_sources = {
            "name": {
                "source": primary["name_source"], "status": primary["name_status"]
            },
            "country": "article_member_consensus" if merged_component else "v2_2_parent_core",
            "city": "article_member_consensus" if merged_component else "v2_2_parent_core",
            "year": "article_member_consensus" if merged_component else "v2_2_parent_core",
            "area": (
                "article_member_consensus"
                if merged_component or impacted_area
                else "v2_2_parent_core"
            ),
            "description": {
                "source": primary["description_source"],
                "status": primary["description_status"],
            },
        }
        needs_review = int(bool(review_reasons))
        review_count += needs_review
        conflict_count += int(bool(reconciliation_conflicts))
        output.append(
            (
                building_id, 1, None, len(component_members), primary_article_id,
                name, name_normalized, country, city, location_method,
                location_confidence, project_year, year_kind, area_sqm,
                canonical_json(sorted(area_candidates, key=lambda value: value["article_id"])),
                canonical_json(field_sources), canonical_json(core_conflicts),
                canonical_json(reconciliation_conflicts),
                canonical_json(current_facet_conflicts),
                canonical_json(dict(sorted(kind_counts.items()))),
                canonical_json(sorted(review_reasons)), needs_review,
                "review" if needs_review else "complete",
                "manual_d2_component" if merged_component else "v2_2_component_preserved",
                POLICY_VERSION, frozen_at,
            )
        )
    conn.executemany(
        """
        INSERT INTO building_core_reconciled_v2_3 VALUES (
          ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
        )
        """,
        output,
    )
    return {
        "buildings": len(output),
        "active": active_count,
        "needs_review": review_count,
        "core_conflicts": conflict_count,
    }


def populate_article_roles(conn: sqlite3.Connection) -> int:
    primary = {
        str(row["building_id"]): int(row["primary_article_id"])
        for row in conn.execute(
            """
            SELECT building_id,primary_article_id FROM building_core_reconciled_v2_3
            WHERE is_active=1 AND primary_article_id IS NOT NULL
            """
        )
    }
    rows = []
    for row in conn.execute(
        """
        SELECT m.*,ak.article_kind,ak.status,ak.confidence
        FROM active_building_membership_v2_3 m
        JOIN article_kind_resolution_v2 ak ON ak.article_id=m.article_id
        ORDER BY m.building_id,m.article_id
        """
    ):
        article_id = int(row["article_id"])
        if article_id == primary[str(row["building_id"])]:
            role, method = "primary", "v2_3_stable_primary"
        elif row["article_kind"] != "project" and row["status"] == "confirmed":
            role, method = row["article_kind"], "confirmed_article_kind"
        else:
            role = "supporting_project"
            method = (
                "unconfirmed_article_kind_not_promoted"
                if row["article_kind"] != "project"
                else "cluster_membership"
            )
        rows.append(
            (
                article_id, row["building_id"], row["parent_building_id"],
                row["source_building_id"], role, row["article_kind"],
                row["status"], row["confidence"], method,
                ARTICLE_KIND_POLICY_VERSION,
            )
        )
    conn.executemany(
        "INSERT INTO building_article_roles_v2_3 VALUES (?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    return len(rows)


VIEWS_SQL = """
CREATE VIEW v_active_building_articles_v2_3 AS
SELECT article_id,building_id,parent_building_id,source_building_id,
       source_article_role,membership_confidence,decision_method
FROM active_building_membership_v2_3;

CREATE VIEW v_article_metadata_reconciled_v2_3 AS
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
    CASE WHEN r.description_source='recrawl_candidate'
         THEN e.description_prose ELSE NULL END AS description_candidate,
    parent_text.text AS historical_parent_description,
    CASE
      WHEN r.description_source='recrawl' THEN e.description_quality
      WHEN r.description_source='parent' THEN p.description_quality
      WHEN r.description_status='source_has_no_prose' THEN 'source_has_no_prose'
      ELSE 'unresolved'
    END AS description_quality,
    e.fetch_status,e.parse_status,e.http_status,e.snapshot_id,e.html_sha256,
    e.snapshot_path,e.parser_version,
    r.name_source,r.name_status,r.abstract_source,
    r.country_source,r.country_status,r.city_source,r.city_status,
    r.year_source,r.year_status,r.area_source,r.area_status,
    r.description_source,r.description_status,r.description_publishable,
    r.area_evidence_json,r.field_sources_json,r.field_conflicts_json,
    r.review_reasons_json,r.metadata_needs_review,r.reconciliation_status,
    r.policy_version,r.partial_decision_version,r.area_decision_version
FROM article_metadata_resolution_v2_3 r
JOIN source_articles p ON p.article_id=r.article_id
JOIN article_recrawl_evidence_v2_2 e ON e.article_id=r.article_id
LEFT JOIN article_text_versions parent_text
  ON parent_text.text_id=r.parent_description_text_id;

CREATE VIEW v_divisare_metadata_review_v2_3 AS
SELECT * FROM v_article_metadata_reconciled_v2_3
WHERE metadata_needs_review=1;

CREATE VIEW v_divisare_area_candidates_v2_3 AS
SELECT
  a.article_id,a.source_url,a.name,a.area_candidate_sqm,
  json_extract(a.area_evidence_json,'$.details.area_scope') AS area_scope,
  a.area_confidence,a.area_status,
  json_extract(a.area_evidence_json,'$.details.closure_status') AS closure_status
FROM v_article_metadata_reconciled_v2_3 a
WHERE a.area_candidate_sqm IS NOT NULL;

CREATE VIEW v_metadata_d2_review_v2_3 AS
SELECT
  mr.*,
  a.name AS name_a,b.name AS name_b,
  a.location_country AS country_a,b.location_country AS country_b,
  a.location_city AS city_a,b.location_city AS city_b,
  a.project_year AS year_a,b.project_year AS year_b,
  a.area_sqm AS area_sqm_a,b.area_sqm AS area_sqm_b,
  ma.building_id AS resolved_building_id_a,
  mb.building_id AS resolved_building_id_b
FROM article_match_reviews_v2_3 mr
JOIN v_article_metadata_reconciled_v2_3 a ON a.article_id=mr.article_id_a
JOIN v_article_metadata_reconciled_v2_3 b ON b.article_id=mr.article_id_b
JOIN active_building_membership_v2_3 ma ON ma.article_id=mr.article_id_a
JOIN active_building_membership_v2_3 mb ON mb.article_id=mr.article_id_b;

CREATE VIEW v_building_images_v2_3 AS
SELECT building_id,asset_key,representative_url,role_rank,first_position
FROM building_images_materialized_v2_3;

CREATE VIEW v_search_facets_v2_3 AS
SELECT building_id,axis,value,status,role,confidence,search_tier,
       evidence_family_count,independence_group_count
FROM building_facets_v2_3
WHERE search_tier<>'hidden' AND status IN ('confirmed','candidate');

CREATE VIEW v_divisare_buildings_export_v2_3 AS
SELECT
  b.building_id AS canonical_bld_id,
  core.primary_article_id AS primary_divisare_id,
  json_object(
    'divisare',
    json(COALESCE((
      SELECT json_group_array(article_id)
      FROM (
        SELECT article_id FROM active_building_membership_v2_3 m
        WHERE m.building_id=b.building_id ORDER BY article_id
      )
    ),'[]'))
  ) AS source_refs,
  core.name,
  core.location_city,
  core.location_country,
  core.location_resolution_method,
  core.location_confidence,
  core.project_year,
  core.year_kind,
  core.area_sqm,
  json(core.area_candidates_json) AS area_candidates,
  COALESCE((
    SELECT json_group_array(architect_id)
    FROM (
      SELECT DISTINCT aa.architect_id
      FROM active_building_membership_v2_3 m
      JOIN article_architects aa ON aa.article_id=m.article_id
      WHERE m.building_id=b.building_id AND aa.architect_id IS NOT NULL
      ORDER BY aa.architect_id
    )
  ),'[]') AS architect_canonical_ids,
  COALESCE((
    SELECT json_group_array(architect_name)
    FROM (
      SELECT DISTINCT aa.architect_name
      FROM active_building_membership_v2_3 m
      JOIN article_architects aa ON aa.article_id=m.article_id
      WHERE m.building_id=b.building_id AND aa.architect_name IS NOT NULL
      ORDER BY aa.architect_name
    )
  ),'[]') AS architect_names,
  (SELECT value FROM building_facets_v2_3 f
   WHERE f.building_id=b.building_id AND f.axis='program'
     AND f.status='confirmed' AND f.role='primary' LIMIT 1) AS program,
  COALESCE((
    SELECT json_group_array(value) FROM (
      SELECT value FROM building_facets_v2_3 f
      WHERE f.building_id=b.building_id AND f.axis='program'
        AND f.status='confirmed' ORDER BY value
    )
  ),'[]') AS programs,
  CAST((SELECT COUNT(*) FROM building_facets_v2_3 f
        WHERE f.building_id=b.building_id AND f.axis='program'
          AND f.status='confirmed')>1 AS INTEGER) AS mixed_use,
  (SELECT value FROM building_facets_v2_3 f
   WHERE f.building_id=b.building_id AND f.axis='typology'
     AND f.status='confirmed' AND f.role='primary' LIMIT 1) AS typology_primary,
  COALESCE((
    SELECT json_group_array(value) FROM (
      SELECT value FROM building_facets_v2_3 f
      WHERE f.building_id=b.building_id AND f.axis='typology'
        AND f.status='confirmed' ORDER BY value
    )
  ),'[]') AS typology_tags,
  CAST((SELECT COUNT(*) FROM building_facets_v2_3 f
        WHERE f.building_id=b.building_id AND f.axis='typology'
          AND f.status='confirmed')>1 AS INTEGER) AS multi_typology,
  COALESCE((
    SELECT json_group_array(value) FROM (
      SELECT value FROM building_facets_v2_3 f
      WHERE f.building_id=b.building_id AND f.axis='material'
        AND f.status='confirmed' ORDER BY confidence DESC,value
    )
  ),'[]') AS material_visual,
  COALESCE((
    SELECT json_group_array(value) FROM (
      SELECT value FROM building_facets_v2_3 f
      WHERE f.building_id=b.building_id AND f.axis='color'
        AND f.status='confirmed' ORDER BY confidence DESC,value
    )
  ),'[]') AS colors,
  (SELECT value FROM building_facets_v2_3 f
   WHERE f.building_id=b.building_id AND f.axis='style'
     AND f.status='confirmed' AND f.role='primary' LIMIT 1) AS style,
  (SELECT value FROM building_facets_v2_3 f
   WHERE f.building_id=b.building_id AND f.axis='structural_system'
     AND f.status='confirmed' AND f.role='primary' LIMIT 1) AS structural_system,
  COALESCE((
    SELECT json_group_array(value) FROM (
      SELECT value FROM building_facets_v2_3 f
      WHERE f.building_id=b.building_id AND f.axis='facade_material'
        AND f.status='confirmed' ORDER BY confidence DESC,value
    )
  ),'[]') AS facade_materials,
  (SELECT value FROM building_facets_v2_3 f
   WHERE f.building_id=b.building_id AND f.axis='facade_pattern'
     AND f.status='confirmed' AND f.role='primary' LIMIT 1) AS facade_pattern,
  (SELECT value FROM building_facets_v2_3 f
   WHERE f.building_id=b.building_id AND f.axis='facade_system'
     AND f.status='confirmed' AND f.role='primary' LIMIT 1) AS facade_system,
  (SELECT value FROM building_facets_v2_3 f
   WHERE f.building_id=b.building_id AND f.axis='roof_type'
     AND f.status='confirmed' AND f.role='primary' LIMIT 1) AS roof_type,
  COALESCE((
    SELECT json_group_array(value) FROM (
      SELECT value FROM building_facets_v2_3 f
      WHERE f.building_id=b.building_id AND f.axis='architectural_element'
        AND f.status='confirmed' AND f.search_tier<>'hidden'
      ORDER BY confidence DESC,value
    )
  ),'[]') AS architectural_elements,
  COALESCE((
    SELECT json_group_array(value) FROM (
      SELECT value FROM building_facets_v2_3 f
      WHERE f.building_id=b.building_id AND f.axis='site_context'
        AND f.status='confirmed' ORDER BY confidence DESC,value
    )
  ),'[]') AS site_contexts,
  COALESCE((
    SELECT json_group_array(value) FROM (
      SELECT value FROM building_facets_v2_3 f
      WHERE f.building_id=b.building_id AND f.axis='intervention_type'
        AND f.status='confirmed' ORDER BY confidence DESC,value
    )
  ),'[]') AS intervention_types,
  COALESCE((
    SELECT json_group_array(tag_slug) FROM (
      SELECT DISTINCT at.tag_slug
      FROM active_building_membership_v2_3 m
      JOIN article_tags at ON at.article_id=m.article_id
      WHERE m.building_id=b.building_id ORDER BY at.tag_slug
    )
  ),'[]') AS source_categories,
  (SELECT iu.url
   FROM article_image_occurrences aio JOIN image_urls iu ON iu.url_id=aio.url_id
   WHERE aio.article_id=core.primary_article_id AND aio.role='cover'
   ORDER BY aio.position,iu.url_id LIMIT 1) AS cover_image_url,
  COALESCE((
    SELECT json_group_array(representative_url) FROM (
      SELECT representative_url FROM building_images_materialized_v2_3 bi
      WHERE bi.building_id=b.building_id
      ORDER BY role_rank,first_position,asset_key
    )
  ),'[]') AS gallery_image_urls,
  article.description,
  article.description_quality,
  CASE WHEN article.description_source='parent'
       THEN primary_source.description_ui_markers ELSE 0 END AS description_ui_markers,
  article.description_candidate,
  article.description_status,
  article.availability_status,
  primary_kind.article_kind AS primary_article_kind,
  primary_kind.status AS primary_article_kind_status,
  core.article_kind_counts_json,
  b.cluster_confidence,
  core.metadata_needs_review AS needs_review,
  core.core_conflicts_json,
  core.facet_conflicts_json,
  core.reconciliation_conflicts_json,
  core.review_reasons_json,
  core.field_sources_json,
  COALESCE((
    SELECT json_group_array(json_object(
      'axis',axis,'value',value,'role',role,'confidence',confidence,
      'search_tier',search_tier
    )) FROM (
      SELECT axis,value,role,confidence,search_tier
      FROM building_facets_v2_3 f
      WHERE f.building_id=b.building_id AND f.status='confirmed'
      ORDER BY axis,confidence DESC,value
    )
  ),'[]') AS confirmed_facets_json,
  COALESCE((
    SELECT json_group_array(json_object(
      'axis',axis,'value',value,'role',role,'confidence',confidence,
      'search_tier',search_tier
    )) FROM (
      SELECT axis,value,role,confidence,search_tier
      FROM building_facets_v2_3 f
      WHERE f.building_id=b.building_id AND f.status='candidate'
      ORDER BY axis,confidence DESC,value
    )
  ),'[]') AS candidate_facets_json,
  COALESCE((
    SELECT json_group_array(json_object(
      'building_id',related_building,'relation',relation_type,
      'group_id',related_group_id
    )) FROM (
      SELECT DISTINCT CASE WHEN rp.building_id_a=b.building_id
                           THEN rp.building_id_b ELSE rp.building_id_a END AS related_building,
             rp.relation_type,rp.related_group_id
      FROM building_related_projects_v2_3 rp
      WHERE rp.building_id_a=b.building_id OR rp.building_id_b=b.building_id
      ORDER BY related_building,rp.relation_type
    )
  ),'[]') AS related_projects_json,
  'divisare-metadata-v2.3' AS metadata_version
FROM buildings b
JOIN building_core_reconciled_v2_3 core
  ON core.building_id=b.building_id AND core.is_active=1
JOIN v_article_metadata_reconciled_v2_3 article
  ON article.article_id=core.primary_article_id
JOIN source_articles primary_source
  ON primary_source.article_id=core.primary_article_id
JOIN article_kind_resolution_v2 primary_kind
  ON primary_kind.article_id=core.primary_article_id;
"""


def collect_metrics(conn: sqlite3.Connection) -> Dict[str, Any]:
    def grouped(table: str, column: str) -> Dict[str, int]:
        return {
            str(row["value"]): int(row["n"])
            for row in conn.execute(
                "SELECT %s AS value,COUNT(*) AS n FROM %s GROUP BY %s ORDER BY %s"
                % (column, table, column, column)
            )
        }

    return {
        "articles": int(conn.execute("SELECT COUNT(*) FROM source_articles").fetchone()[0]),
        "article_review": int(
            conn.execute(
                "SELECT COUNT(*) FROM article_metadata_resolution_v2_3 WHERE metadata_needs_review=1"
            ).fetchone()[0]
        ),
        "article_area": int(
            conn.execute(
                "SELECT COUNT(*) FROM article_metadata_resolution_v2_3 WHERE area_sqm IS NOT NULL"
            ).fetchone()[0]
        ),
        "article_area_candidates": int(
            conn.execute(
                "SELECT COUNT(*) FROM article_metadata_resolution_v2_3 WHERE area_candidate_sqm IS NOT NULL"
            ).fetchone()[0]
        ),
        "partial_decisions": grouped("article_partial_text_decisions_v2_3", "decision"),
        "area_decisions": grouped("article_area_decisions_v2_3", "decision_type"),
        "d2_decisions": grouped("article_d2_decisions_v2_3", "decision"),
        "d2_status": grouped("article_match_reviews_v2_3", "decision_status"),
        "active_buildings": int(
            conn.execute(
                "SELECT COUNT(*) FROM building_core_reconciled_v2_3 WHERE is_active=1"
            ).fetchone()[0]
        ),
        "building_review": int(
            conn.execute(
                """
                SELECT COUNT(*) FROM building_core_reconciled_v2_3
                WHERE is_active=1 AND metadata_needs_review=1
                """
            ).fetchone()[0]
        ),
        "redirects": int(
            conn.execute("SELECT COUNT(*) FROM building_redirects_v2_3").fetchone()[0]
        ),
        "facets": grouped("building_facets_v2_3", "status"),
        "building_images": int(
            conn.execute("SELECT COUNT(*) FROM building_images_materialized_v2_3").fetchone()[0]
        ),
        "relations": int(
            conn.execute("SELECT COUNT(*) FROM building_related_projects_v2_3").fetchone()[0]
        ),
    }


def validate_output(
    conn: sqlite3.Connection,
    *,
    parent: Mapping[str, Any],
    partial_manifest: LoadedManifest,
    area_manifest: LoadedManifest,
    d2_manifest: LoadedManifest,
    frozen_at: str,
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
    add("schema_version", int(conn.execute("PRAGMA user_version").fetchone()[0]), SCHEMA_VERSION)

    for table, expected_hash in sorted(parent["table_hashes"].items()):
        add(
            "preserved_table_%s" % table,
            table_logical_sha256(conn, table),
            expected_hash,
        )
    target_schema = schema_objects(conn)
    changed_schema = {
        "%s:%s" % key: {"expected": sql, "actual": target_schema.get(key)}
        for key, sql in parent["schema_objects"].items()
        if target_schema.get(key) != sql
    }
    add("preserved_schema_objects", changed_schema, {})

    article_count = int(parent["counts"]["articles"])
    building_count = int(parent["counts"]["buildings"])
    add(
        "article_resolution_complete",
        int(conn.execute("SELECT COUNT(*) FROM article_metadata_resolution_v2_3").fetchone()[0]),
        article_count,
    )
    add(
        "partial_decisions_complete",
        int(conn.execute("SELECT COUNT(*) FROM article_partial_text_decisions_v2_3").fetchone()[0]),
        len(partial_manifest.decisions),
    )
    add(
        "partial_guards",
        int(
            conn.execute(
                "SELECT COUNT(*) FROM article_partial_text_decisions_v2_3 WHERE hash_guard_matched<>1"
            ).fetchone()[0]
        ),
        0,
    )
    add(
        "partial_review_closed",
        int(
            conn.execute(
                "SELECT COUNT(*) FROM article_partial_text_decisions_v2_3 WHERE decision='review'"
            ).fetchone()[0]
        ),
        0,
    )
    add(
        "area_decisions_complete",
        int(conn.execute("SELECT COUNT(*) FROM article_area_decisions_v2_3").fetchone()[0]),
        len(area_manifest.decisions),
    )
    add(
        "area_guards",
        int(
            conn.execute(
                "SELECT COUNT(*) FROM article_area_decisions_v2_3 WHERE hash_guard_matched<>1"
            ).fetchone()[0]
        ),
        0,
    )
    add(
        "area_scoped_not_generic",
        int(
            conn.execute(
                """
                SELECT COUNT(*) FROM article_metadata_resolution_v2_3 r
                JOIN article_area_decisions_v2_3 d USING(article_id)
                WHERE d.decision_type='keep_scoped_candidate'
                  AND (r.area_sqm IS NOT NULL OR r.area_candidate_sqm IS NULL)
                """
            ).fetchone()[0]
        ),
        0,
    )
    add(
        "area_accept_only_generic",
        int(
            conn.execute(
                """
                SELECT COUNT(*) FROM article_metadata_resolution_v2_3 r
                JOIN article_area_decisions_v2_3 d USING(article_id)
                WHERE (d.decision_type='accept_area' AND r.area_sqm IS NULL)
                   OR (d.decision_type<>'accept_area' AND r.area_sqm IS NOT NULL)
                """
            ).fetchone()[0]
        ),
        0,
    )
    add(
        "area_open_review_exact",
        int(
            conn.execute(
                """
                SELECT COUNT(*) FROM article_metadata_resolution_v2_3 r
                JOIN article_area_decisions_v2_3 d USING(article_id)
                WHERE d.closure_status='open_external_text_review'
                  AND r.metadata_needs_review=1
                """
            ).fetchone()[0]
        ),
        int(area_manifest.counts.get("open_external_text_review", 0)),
    )

    affected_ids = set(partial_manifest.decisions) | set(area_manifest.decisions)
    placeholders = ",".join("?" for _ in affected_ids) or "NULL"
    columns = ",".join(ARTICLE_RESOLUTION_COLUMNS)
    params = tuple(sorted(affected_ids))
    unchanged_diff = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM (
              SELECT %s FROM article_metadata_resolution_v2_2
              WHERE article_id NOT IN (%s)
              EXCEPT
              SELECT %s FROM article_metadata_resolution_v2_3
              WHERE article_id NOT IN (%s)
            )
            """ % (columns, placeholders, columns, placeholders),
            params + params,
        ).fetchone()[0]
    )
    add("unaffected_article_values_exact", unchanged_diff, 0)

    add(
        "d2_decisions_complete",
        int(conn.execute("SELECT COUNT(*) FROM article_d2_decisions_v2_3").fetchone()[0]),
        len(d2_manifest.decisions),
    )
    add(
        "d2_guards",
        int(
            conn.execute(
                """
                SELECT COUNT(*) FROM article_d2_decisions_v2_3
                WHERE hash_guard_matched<>1
                """
            ).fetchone()[0]
        ),
        0,
    )
    for decision_type in ("merge", "reject", "defer"):
        add(
            "d2_%s_count" % decision_type,
            int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM article_d2_decisions_v2_3
                    WHERE decision=?
                    """,
                    (decision_type,),
                ).fetchone()[0]
            ),
            int(d2_manifest.counts[decision_type]),
        )
    add(
        "d2_review_snapshot_complete",
        int(conn.execute("SELECT COUNT(*) FROM article_match_reviews_v2_3").fetchone()[0]),
        int(parent["counts"]["d2_pairs"]),
    )
    add(
        "active_membership_complete",
        int(conn.execute("SELECT COUNT(*) FROM active_building_membership_v2_3").fetchone()[0]),
        article_count,
    )
    add(
        "active_membership_unique",
        int(
            conn.execute(
                """
                SELECT COUNT(*) FROM (
                  SELECT article_id FROM active_building_membership_v2_3
                  GROUP BY article_id HAVING COUNT(*)<>1
                )
                """
            ).fetchone()[0]
        ),
        0,
    )
    add(
        "redirects_terminal",
        int(
            conn.execute(
                """
                SELECT COUNT(*) FROM building_redirects_v2_3 r
                JOIN building_redirects_v2_3 n
                  ON n.source_building_id=r.target_building_id
                """
            ).fetchone()[0]
        ),
        0,
    )
    add(
        "confirmed_pairs_share_building",
        int(
            conn.execute(
                """
                SELECT COUNT(*) FROM article_match_reviews_v2_3 mr
                JOIN active_building_membership_v2_3 a ON a.article_id=mr.article_id_a
                JOIN active_building_membership_v2_3 b ON b.article_id=mr.article_id_b
                WHERE mr.decision_status='confirmed' AND a.building_id<>b.building_id
                """
            ).fetchone()[0]
        ),
        0,
    )
    add(
        "nonmerge_pairs_separate",
        int(
            conn.execute(
                """
                SELECT COUNT(*) FROM article_match_reviews_v2_3 mr
                JOIN active_building_membership_v2_3 a ON a.article_id=mr.article_id_a
                JOIN active_building_membership_v2_3 b ON b.article_id=mr.article_id_b
                WHERE mr.decision_status IN ('pending','deferred','rejected')
                  AND a.building_id=b.building_id
                """
            ).fetchone()[0]
        ),
        0,
    )
    add(
        "building_core_complete",
        int(conn.execute("SELECT COUNT(*) FROM building_core_reconciled_v2_3").fetchone()[0]),
        building_count,
    )
    active_buildings = int(
        conn.execute(
            "SELECT COUNT(*) FROM building_core_reconciled_v2_3 WHERE is_active=1"
        ).fetchone()[0]
    )
    add(
        "export_active_complete",
        int(conn.execute("SELECT COUNT(*) FROM v_divisare_buildings_export_v2_3").fetchone()[0]),
        active_buildings,
    )
    add(
        "primary_is_member",
        int(
            conn.execute(
                """
                SELECT COUNT(*) FROM building_core_reconciled_v2_3 c
                WHERE c.is_active=1 AND NOT EXISTS (
                  SELECT 1 FROM active_building_membership_v2_3 m
                  WHERE m.building_id=c.building_id AND m.article_id=c.primary_article_id
                )
                """
            ).fetchone()[0]
        ),
        0,
    )
    expected_images = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM (
              SELECT DISTINCT m.building_id,aio.asset_key
              FROM active_building_membership_v2_3 m
              JOIN article_image_occurrences aio ON aio.article_id=m.article_id
            )
            """
        ).fetchone()[0]
    )
    add(
        "building_images_complete",
        int(conn.execute("SELECT COUNT(*) FROM building_images_materialized_v2_3").fetchone()[0]),
        expected_images,
    )
    add(
        "facet_claim_links_complete",
        int(
            conn.execute(
                """
                SELECT COUNT(*) FROM building_facets_v2_3 f
                WHERE f.claim_count<>(
                  SELECT COUNT(*) FROM building_facet_claims_v2_3 l
                  WHERE l.facet_v2_3_id=f.facet_v2_3_id
                )
                """
            ).fetchone()[0]
        ),
        0,
    )
    add(
        "core_area_conflict_abstains",
        int(
            conn.execute(
                """
                SELECT COUNT(*) FROM building_core_reconciled_v2_3
                WHERE is_active=1 AND area_sqm IS NOT NULL
                  AND json_type(reconciliation_conflicts_json,'$.area_sqm') IS NOT NULL
                """
            ).fetchone()[0]
        ),
        0,
    )
    add(
        "validation_frozen_timestamp",
        conn.execute("SELECT frozen_at FROM metadata_review_lineage_v2_3").fetchone()[0],
        frozen_at,
    )

    conn.executemany(
        "INSERT INTO metadata_review_validation_v2_3 VALUES (?,?,?,?,?)",
        [
            (
                check["name"], int(check["passed"]),
                canonical_json(check["actual"]), canonical_json(check["expected"]),
                frozen_at,
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


V23_LOGICAL_TABLES = (
    "metadata_review_lineage_v2_3",
    "article_partial_text_decisions_v2_3",
    "article_area_decisions_v2_3",
    "article_d2_decisions_v2_3",
    "article_metadata_resolution_v2_3",
    "article_match_reviews_v2_3",
    "building_redirects_v2_3",
    "active_building_membership_v2_3",
    "building_facets_v2_3",
    "building_facet_claims_v2_3",
    "building_images_materialized_v2_3",
    "building_core_reconciled_v2_3",
    "building_article_roles_v2_3",
    "building_related_projects_v2_3",
    "metadata_review_metrics_v2_3",
    "metadata_review_validation_v2_3",
)


def logical_sha256(conn: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for table in V23_LOGICAL_TABLES:
        digest.update(table.encode("utf-8"))
        digest.update(table_logical_sha256(conn, table).encode("ascii"))
    return digest.hexdigest()


def write_report(
    path: Path,
    *,
    parent_path: Path,
    output_path: Path,
    parent_sha256: str,
    output_sha256: str,
    logical_sha: str,
    metrics: Mapping[str, Any],
    validation: Mapping[str, Any],
    elapsed_seconds: float,
) -> None:
    lines = [
        "# Divisare metadata v2.3 review report",
        "",
        "- Parent: `%s`" % parent_path,
        "- Parent SHA-256: `%s`" % parent_sha256,
        "- Output: `%s`" % output_path,
        "- Output SHA-256: `%s`" % output_sha256,
        "- Logical SHA-256: `%s`" % logical_sha,
        "- Builder: `%s`" % BUILDER_VERSION,
        "- Policy: `%s`" % POLICY_VERSION,
        "- Elapsed: `%.2f seconds`" % elapsed_seconds,
        "- Image download / pHash / Vision / vectors / cross-site work: `not run`",
        "",
        "## Metrics",
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
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _stable_artifact_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return resolved.name


def load_and_validate_inputs(
    *,
    parent_path: Path,
    partial_path: Path,
    area_path: Path,
    d2_path: Path,
    production_contract: bool,
    compute_content_hashes: bool,
) -> Dict[str, Any]:
    required = {
        "parent": parent_path,
        "partial": partial_path,
        "area": area_path,
        "D2": d2_path,
    }
    missing = ["%s=%s" % (name, path) for name, path in required.items() if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            "v2.3 requires all immutable inputs before building: %s"
            % ", ".join(missing)
        )

    parent_sha = file_sha256(parent_path)
    expected_parent_sha = EXPECTED_PARENT_SHA256 if production_contract else parent_sha
    conn = open_readonly(parent_path)
    try:
        parent = validate_parent(
            conn,
            parent_sha256=parent_sha,
            expected_parent_sha256=expected_parent_sha,
            compute_content_hashes=compute_content_hashes,
        )
        partial = load_partial_manifest(partial_path)
        area = load_area_manifest(
            area_path,
            expected_parent_sha256=parent_sha,
            expected_counts=PRODUCTION_AREA_COUNTS if production_contract else None,
        )
        d2 = load_d2_manifest(
            d2_path,
            expected_parent_sha256=parent_sha,
            expected_pairs=parent["pending_pairs"],
            expected_counts=PRODUCTION_D2_COUNTS if production_contract else None,
        )
        _validated_partial_rows(conn, partial, parent["partial_ids"])
        _validated_area_rows(conn, area, parent["area_review_ids"])
        _validated_d2_rows(conn, d2)
    finally:
        conn.close()

    return {
        "parent": parent,
        "parent_sha256": parent_sha,
        "parent_byte_size": parent_path.stat().st_size,
        "partial": partial,
        "area": area,
        "d2": d2,
    }


def _insert_lineage(
    conn: sqlite3.Connection,
    *,
    inputs: Mapping[str, Any],
    parent_path: Path,
    partial_path: Path,
    area_path: Path,
    d2_path: Path,
    frozen_at: str,
) -> None:
    partial: LoadedManifest = inputs["partial"]
    area: LoadedManifest = inputs["area"]
    d2: LoadedManifest = inputs["d2"]
    conn.execute(
        """
        INSERT INTO metadata_review_lineage_v2_3(
          lineage_id,parent_db_path,parent_sha256,parent_byte_size,
          parent_schema_version,parent_metadata_version,
          partial_manifest_path,partial_manifest_sha256,partial_manifest_version,
          area_manifest_path,area_manifest_sha256,area_manifest_version,
          d2_manifest_path,d2_manifest_sha256,d2_manifest_version,
          builder_version,policy_version,metadata_version,schema_version,
          frozen_at,decision_counts_json,scope_json
        ) VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            _stable_artifact_path(parent_path), inputs["parent_sha256"],
            inputs["parent_byte_size"], EXPECTED_PARENT_SCHEMA,
            EXPECTED_PARENT_METADATA_VERSION,
            _stable_artifact_path(partial_path), partial.sha256, partial.version,
            _stable_artifact_path(area_path), area.sha256, area.version,
            _stable_artifact_path(d2_path), d2.sha256, d2.version,
            BUILDER_VERSION, POLICY_VERSION, METADATA_VERSION, SCHEMA_VERSION,
            frozen_at,
            canonical_json(
                {
                    "partial": partial.counts,
                    "area": area.counts,
                    "d2": d2.counts,
                }
            ),
            canonical_json(
                {
                    "strategy": "single_overlay_after_three_frozen_manifests",
                    "parent_is_immutable": True,
                    "partial_text": "manual_manifest_only",
                    "area": "manual_manifest_only_no_image_numeric_inference",
                    "identity": "manual_d2_component_resolution",
                    "images": "existing_url_asset_regrouping_only",
                    "excluded": [
                        "image_download", "phash", "vision", "vectors",
                        "cross_site_deduplication",
                    ],
                }
            ),
        ),
    )


def _build_temp_artifact(
    *,
    temp_path: Path,
    parent_path: Path,
    partial_path: Path,
    area_path: Path,
    d2_path: Path,
    inputs: Mapping[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any], str]:
    shutil.copyfile(parent_path, temp_path)
    conn = sqlite3.connect(temp_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA synchronous=FULL")
        conn.executescript(SCHEMA_SQL)
        conn.execute("BEGIN IMMEDIATE")
        partial: LoadedManifest = inputs["partial"]
        area: LoadedManifest = inputs["area"]
        d2: LoadedManifest = inputs["d2"]
        frozen_at = str(d2.frozen_at)
        _insert_lineage(
            conn,
            inputs=inputs,
            parent_path=parent_path,
            partial_path=partial_path,
            area_path=area_path,
            d2_path=d2_path,
            frozen_at=frozen_at,
        )
        materialize_partial_decisions(
            conn, partial, inputs["parent"]["partial_ids"]
        )
        materialize_area_decisions(
            conn, area, inputs["parent"]["area_review_ids"]
        )
        materialize_d2_decisions(conn, d2)
        article_count = populate_article_resolutions(
            conn,
            partial_manifest=partial,
            area_manifest=area,
            frozen_at=frozen_at,
        )
        match_counts = populate_match_reviews(
            conn, d2_manifest=d2, frozen_at=frozen_at
        )
        component_map, final_by_building, identity_metrics = (
            populate_identity_and_membership(
                conn, d2_manifest=d2, frozen_at=frozen_at
            )
        )
        facet_metrics = populate_facets(conn, component_map)
        image_count = materialize_building_images(conn)
        core_metrics = populate_building_core(
            conn,
            final_by_building=final_by_building,
            area_decision_ids=area.decisions,
            frozen_at=frozen_at,
        )
        role_count = populate_article_roles(conn)
        conn.executescript(VIEWS_SQL)
        conn.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)

        metrics = collect_metrics(conn)
        metrics.update(
            {
                "article_resolution_rows": article_count,
                "article_role_rows": role_count,
                "materialized_image_rows": image_count,
                "match_status": match_counts,
                "identity": identity_metrics,
                "facet_materialization": facet_metrics,
                "building_core": core_metrics,
                "manifest_sha256": {
                    "partial": partial.sha256,
                    "area": area.sha256,
                    "d2": d2.sha256,
                },
            }
        )
        conn.executemany(
            "INSERT INTO metadata_review_metrics_v2_3 VALUES (?,?)",
            [
                (key, canonical_json(value))
                for key, value in sorted(metrics.items())
            ],
        )
        validation = validate_output(
            conn,
            parent=inputs["parent"],
            partial_manifest=partial,
            area_manifest=area,
            d2_manifest=d2,
            frozen_at=frozen_at,
        )
        if validation["failed"]:
            failed_names = [
                check["name"] for check in validation["checks"] if not check["passed"]
            ]
            raise RuntimeError(
                "v2.3 output validation failed: %s" % ", ".join(failed_names)
            )
        conn.commit()
        logical_sha = logical_sha256(conn)
        return metrics, validation, logical_sha
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def validate_only(
    *,
    parent_path: Path,
    partial_path: Path,
    area_path: Path,
    d2_path: Path,
    production_contract: bool = True,
) -> Dict[str, Any]:
    inputs = load_and_validate_inputs(
        parent_path=parent_path,
        partial_path=partial_path,
        area_path=area_path,
        d2_path=d2_path,
        production_contract=production_contract,
        compute_content_hashes=False,
    )
    return {
        "status": "validated",
        "parent_sha256": inputs["parent_sha256"],
        "parent_counts": inputs["parent"]["counts"],
        "partial_counts": inputs["partial"].counts,
        "area_counts": inputs["area"].counts,
        "d2_counts": inputs["d2"].counts,
        "output_created": False,
    }


def build_artifact(
    *,
    parent_path: Path,
    partial_path: Path,
    area_path: Path,
    d2_path: Path,
    output_path: Path,
    report_path: Path,
    production_contract: bool = True,
) -> Dict[str, Any]:
    start = time.monotonic()
    paths = [parent_path, partial_path, area_path, d2_path, output_path, report_path]
    (
        parent_path, partial_path, area_path, d2_path, output_path, report_path
    ) = [Path(path).resolve() for path in paths]
    if output_path.exists() or report_path.exists():
        raise FileExistsError("immutable output or report already exists")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = output_path.with_name(output_path.name + ".lock")
    temp_path = output_path.with_name(
        "%s.tmp.%s" % (output_path.name, os.getpid())
    )
    report_temp_path = report_path.with_name(
        "%s.tmp.%s" % (report_path.name, os.getpid())
    )
    if temp_path.exists() or report_temp_path.exists():
        raise FileExistsError("build staging path already exists")

    with exclusive_build_lock(lock_path, output_path):
        inputs = load_and_validate_inputs(
            parent_path=parent_path,
            partial_path=partial_path,
            area_path=area_path,
            d2_path=d2_path,
            production_contract=production_contract,
            compute_content_hashes=True,
        )
        try:
            metrics, validation, logical_sha = _build_temp_artifact(
                temp_path=temp_path,
                parent_path=parent_path,
                partial_path=partial_path,
                area_path=area_path,
                d2_path=d2_path,
                inputs=inputs,
            )
            if file_sha256(parent_path) != inputs["parent_sha256"]:
                raise RuntimeError("immutable parent changed during the v2.3 build")
            output_sha = file_sha256(temp_path)
            elapsed = time.monotonic() - start
            write_report(
                report_temp_path,
                parent_path=parent_path,
                output_path=output_path,
                parent_sha256=inputs["parent_sha256"],
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
        except Exception:
            for path in (temp_path, report_temp_path):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            raise
    return {
        "status": "built",
        "output": str(output_path),
        "report": str(report_path),
        "output_sha256": output_sha,
        "logical_sha256": logical_sha,
        "parent_sha256": inputs["parent_sha256"],
        "elapsed_seconds": round(elapsed, 3),
        "metrics": metrics,
        "validation": {"passed": validation["passed"], "failed": 0},
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", type=Path, default=DEFAULT_PARENT)
    parser.add_argument("--partial-manifest", type=Path, default=DEFAULT_PARTIAL)
    parser.add_argument("--area-manifest", type=Path, default=DEFAULT_AREA)
    parser.add_argument("--d2-manifest", type=Path, default=DEFAULT_D2)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="validate all manifests and guards without creating an output",
    )
    parser.add_argument(
        "--fixture-contract",
        action="store_true",
        help="test fixtures only: do not require the pinned production SHA/counts",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    kwargs = {
        "parent_path": args.parent,
        "partial_path": args.partial_manifest,
        "area_path": args.area_manifest,
        "d2_path": args.d2_manifest,
        "production_contract": not args.fixture_contract,
    }
    try:
        if args.validate_only:
            result = validate_only(**kwargs)
        else:
            result = build_artifact(
                **kwargs,
                output_path=args.output,
                report_path=args.report,
            )
    except Exception as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
