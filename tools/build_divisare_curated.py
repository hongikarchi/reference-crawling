#!/usr/bin/env python3
"""Build a Divisare-only normalized and auditable SQLite database.

The raw crawler database is opened read-only and never mutated.  This builder
creates a separate article -> building cluster -> resolved facet database.  It
does not call an LLM, download images, compute embeddings, write to Neon, or
reuse the known-incomplete positional pHash cache.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Optional


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from canonical.divisare_curated import (  # noqa: E402
    ASSET_KEY_VERSION,
    CLUSTER_VERSION,
    RESOLVER_VERSION,
    TAXONOMY_VERSION,
    TEXT_PROCESSOR_VERSION,
    URL_HINT_VERSION,
    clean_description,
    clean_location,
    clean_scalar,
    confidence_class,
    divisare_asset_identity,
    filename_media_hints,
    is_generic_building_name,
    mappings_for_tag,
    normalize_country,
    normalize_identity_text,
    parse_json_list,
    search_tier_rank,
)


BUILDER_VERSION = "divisare-curated-builder-v1.5"
SCHEMA_VERSION = 2
PHASH_ALGORITHM = "phash-256"
PHASH_ALGORITHM_VERSION = "imagehash-phash-hash_size_16-v1"
REQUIRED_SOURCE_TABLES = {
    "divisare_architects",
    "divisare_projects",
    "divisare_tags",
    "divisare_albums",
    "divisare_album_membership",
    "pending_tags",
}
REQUIRED_SOURCE_COLUMNS = {
    "divisare_projects": {
        "id",
        "slug",
        "name",
        "architect_ids",
        "architect_names",
        "location_country",
        "location_city",
        "project_year",
        "area_sqm",
        "description",
        "tag_slugs",
        "cover_image_url",
        "gallery_urls",
    },
    "divisare_architects": {"id", "slug", "name"},
    "divisare_album_membership": {
        "album_slug",
        "child_slug",
        "child_name",
        "child_url",
    },
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_row_hash(row: sqlite3.Row) -> str:
    payload = {key: row[key] for key in row.keys()}
    return hashlib.sha256(json_dumps(payload).encode("utf-8", "replace")).hexdigest()


def open_source(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def validate_source(conn: sqlite3.Connection) -> dict[str, Any]:
    if conn.execute("SELECT json_valid('{}')").fetchone()[0] != 1:
        raise RuntimeError("SQLite JSON1 support is required")
    quick_check = conn.execute("PRAGMA quick_check").fetchone()[0]
    if quick_check != "ok":
        raise RuntimeError(f"source DB quick_check failed: {quick_check}")
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    missing = sorted(REQUIRED_SOURCE_TABLES - tables)
    if missing:
        raise RuntimeError(f"source DB missing required tables: {missing}")
    for table, required_columns in REQUIRED_SOURCE_COLUMNS.items():
        columns = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})")
        }
        missing_columns = sorted(required_columns - columns)
        if missing_columns:
            raise RuntimeError(
                f"source DB table {table} missing columns: {missing_columns}"
            )
    counts = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in sorted(REQUIRED_SOURCE_TABLES)
    }
    counts["quick_check"] = quick_check
    return counts


def nonregenerable_state(path: Path) -> dict[str, int]:
    """Return enrichment/manual state that a raw rebuild must not discard."""

    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        version_params = {
            "builder_version": BUILDER_VERSION,
            "schema_version": SCHEMA_VERSION,
            "taxonomy_version": TAXONOMY_VERSION,
            "text_processor_version": TEXT_PROCESSOR_VERSION,
            "asset_key_version": ASSET_KEY_VERSION,
            "cluster_version": CLUSTER_VERSION,
            "resolver_version": RESOLVER_VERSION,
            "url_hint_version": URL_HINT_VERSION,
        }
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        checks = {
            "downstream_or_unknown_build_runs": (
                "build_runs",
                """
                SELECT CASE
                  WHEN COUNT(*)=1
                   AND SUM(
                     CASE WHEN
                       status='complete'
                       AND builder_version=:builder_version
                       AND schema_version=:schema_version
                       AND taxonomy_version=:taxonomy_version
                       AND text_processor_version=:text_processor_version
                       AND asset_key_version=:asset_key_version
                       AND cluster_version=:cluster_version
                       AND resolver_version=:resolver_version
                     THEN 1 ELSE 0 END
                   )=1
                  THEN 0
                  ELSE COUNT(*) + 1
                END
                FROM build_runs
                """,
            ),
            "image_hash_work": (
                "image_hashes",
                """
                SELECT COUNT(*) FROM image_hashes
                WHERE status<>'pending'
                   OR attempt_count<>0
                   OR hash_bits IS NOT NULL
                   OR hash_hex IS NOT NULL
                   OR last_error IS NOT NULL
                   OR computed_at IS NOT NULL
                """,
            ),
            "image_fetch_work": (
                "image_assets",
                """
                SELECT COUNT(*) FROM image_assets
                WHERE fetch_status<>'pending'
                   OR mime_type IS NOT NULL
                   OR width IS NOT NULL
                   OR height IS NOT NULL
                   OR byte_size IS NOT NULL
                   OR content_sha256 IS NOT NULL
                   OR last_fetch_error IS NOT NULL
                   OR fetched_at IS NOT NULL
                """,
            ),
            "custom_image_url_hints": (
                "image_url_hints",
                """
                SELECT COUNT(*) FROM image_url_hints
                WHERE rule_version<>:url_hint_version
                """,
            ),
            "image_classifications": (
                "image_classifications",
                "SELECT COUNT(*) FROM image_classifications",
            ),
            "image_hash_bands": (
                "image_hash_bands",
                "SELECT COUNT(*) FROM image_hash_bands",
            ),
            "image_match_candidates": (
                "image_match_candidates",
                "SELECT COUNT(*) FROM image_match_candidates",
            ),
            "model_text_enrichment": (
                "article_text_versions",
                """
                SELECT COUNT(*) FROM article_text_versions
                WHERE NOT (
                  (
                    text_kind='raw_description'
                    AND processor_version='raw-v1'
                  )
                  OR (
                    text_kind='clean_description'
                    AND processor_version=:text_processor_version
                  )
                )
                """,
            ),
            "derived_attribute_claims": (
                "attribute_claims",
                """
                SELECT COUNT(*) FROM attribute_claims
                WHERE NOT (
                  (
                    evidence_kind='source_tag'
                    AND extractor_version=:taxonomy_version
                  )
                  OR (
                    evidence_kind='structured_field'
                    AND extractor_version=:builder_version
                  )
                )
                """,
            ),
            "derived_location_claims": (
                "location_claims",
                """
                SELECT COUNT(*) FROM location_claims
                WHERE source_kind NOT IN (
                  'structured','city_tag','house_country_tag','regional_tag'
                )
                """,
            ),
            "custom_tag_crosswalk": (
                "tag_crosswalk",
                """
                SELECT COUNT(*) FROM tag_crosswalk
                WHERE mapping_version<>:taxonomy_version
                """,
            ),
            "manual_article_matches": (
                "article_match_candidates",
                """
                SELECT COUNT(*) FROM article_match_candidates
                WHERE status IN ('confirmed','rejected')
                   OR cluster_version<>:cluster_version
                """,
            ),
            "manual_qa_decisions": (
                "qa_issues",
                """
                SELECT COUNT(*) FROM qa_issues
                WHERE status IN ('resolved','ignored')
                """,
            ),
            "manual_or_custom_facets": (
                "building_facets",
                """
                WITH evidence AS (
                  SELECT
                    f.facet_id,
                    f.status,
                    f.resolver_version,
                    MAX(
                      CASE
                        WHEN json_extract(c.details_json,'$.mapping_kind')='direct'
                        THEN c.confidence
                      END
                    ) AS max_direct_confidence,
                    COUNT(
                      DISTINCT CASE
                        WHEN json_extract(c.details_json,'$.mapping_kind')='supporting'
                        THEN c.source_ref
                      END
                    ) AS supporting_sources,
                    MAX(c.confidence) AS confidence
                  FROM building_facets f
                  LEFT JOIN building_facet_claims fc ON fc.facet_id=f.facet_id
                  LEFT JOIN attribute_claims c ON c.claim_id=fc.claim_id
                  GROUP BY f.facet_id
                )
                SELECT COUNT(*)
                FROM evidence
                WHERE status='rejected'
                   OR resolver_version<>:resolver_version
                   OR status<>CASE
                     WHEN max_direct_confidence>=0.85
                       OR (
                         max_direct_confidence IS NULL
                         AND supporting_sources>=2
                         AND confidence>=0.75
                       )
                     THEN 'confirmed'
                     ELSE 'candidate'
                   END
                """,
            ),
            "manual_building_redirects": (
                "buildings",
                """
                SELECT COUNT(*) FROM buildings
                WHERE redirect_to IS NOT NULL
                   OR resolution_version<>:resolver_version
                """,
            ),
            "custom_building_memberships": (
                "building_articles",
                """
                SELECT COUNT(*) FROM building_articles
                WHERE decision_method NOT IN ('singleton','strict_signature_v1')
                """,
            ),
            "custom_cluster_events": (
                "cluster_events",
                """
                SELECT COUNT(*) FROM cluster_events
                WHERE cluster_version<>:cluster_version
                """,
            ),
        }
        result = {
            name: int(conn.execute(sql, version_params).fetchone()[0])
            for name, (table, sql) in checks.items()
            if table in tables
        }
        conn.close()
        return {name: count for name, count in result.items() if count}
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(
            f"existing output is not a readable curated SQLite DB: {path}"
        ) from exc


def validate_build_paths(
    source_path: Path,
    output_path: Path,
    report_path: Path,
    temp_path: Path,
    report_temp_path: Path,
    lock_path: Optional[Path] = None,
) -> None:
    paths = {
        "source": source_path,
        "output": output_path,
        "report": report_path,
        "output_temp": temp_path,
        "report_temp": report_temp_path,
    }
    if lock_path is not None:
        paths["build_lock"] = lock_path
    normalized: dict[str, str] = {}
    for label, path in paths.items():
        key = os.path.normcase(str(path.resolve()))
        if key in normalized:
            raise ValueError(
                f"path collision: {label} and {normalized[key]} both resolve to {path}"
            )
        normalized[key] = label


@contextmanager
def exclusive_build_lock(lock_path: Path, output_path: Path):
    """Prevent concurrent builders from targeting the same output path."""

    try:
        fd = os.open(
            str(lock_path),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
    except FileExistsError as exc:
        raise RuntimeError(
            f"build lock already exists for {output_path}: {lock_path}"
        ) from exc
    try:
        payload = json_dumps(
            {
                "pid": os.getpid(),
                "created_at": utc_now(),
                "output_db": str(output_path),
            }
        ).encode("utf-8")
        os.write(fd, payload)
        yield
    finally:
        os.close(fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


SCHEMA_SQL = """
PRAGMA foreign_keys=ON;
PRAGMA user_version=2;

CREATE TABLE build_runs (
    run_id                  INTEGER PRIMARY KEY,
    started_at              TEXT NOT NULL,
    completed_at            TEXT,
    status                  TEXT NOT NULL CHECK(status IN ('running','complete','failed')),
    builder_version         TEXT NOT NULL,
    schema_version          INTEGER NOT NULL,
    taxonomy_version        TEXT NOT NULL,
    text_processor_version  TEXT NOT NULL,
    asset_key_version       TEXT NOT NULL,
    cluster_version         TEXT NOT NULL,
    resolver_version        TEXT NOT NULL,
    source_db_path          TEXT NOT NULL,
    output_db_path          TEXT NOT NULL,
    limit_rows              INTEGER,
    metrics_json            TEXT CHECK(metrics_json IS NULL OR json_valid(metrics_json)),
    error                   TEXT
);

CREATE TABLE source_snapshots (
    snapshot_id             INTEGER PRIMARY KEY,
    run_id                  INTEGER NOT NULL REFERENCES build_runs(run_id),
    source_db_path          TEXT NOT NULL,
    byte_size               INTEGER NOT NULL,
    modified_at             TEXT NOT NULL,
    sha256                  TEXT,
    sqlite_quick_check      TEXT NOT NULL,
    source_counts_json      TEXT NOT NULL CHECK(json_valid(source_counts_json)),
    captured_at             TEXT NOT NULL
);

CREATE TABLE source_albums (
    album_slug              TEXT PRIMARY KEY,
    name                    TEXT NOT NULL,
    kind                    TEXT NOT NULL,
    child_count             INTEGER NOT NULL DEFAULT 0,
    fetched_at              TEXT
);

CREATE TABLE source_album_memberships (
    album_slug              TEXT NOT NULL REFERENCES source_albums(album_slug),
    child_slug              TEXT NOT NULL,
    child_name              TEXT NOT NULL,
    child_url               TEXT NOT NULL,
    PRIMARY KEY(album_slug, child_slug)
);

CREATE TABLE source_tags (
    tag_slug                TEXT PRIMARY KEY,
    label                   TEXT NOT NULL,
    album_slug              TEXT,
    child_url               TEXT,
    landing_status          TEXT,
    curated                 INTEGER CHECK(curated IS NULL OR curated IN (0,1)),
    project_count_seen      INTEGER,
    used_article_count      INTEGER NOT NULL DEFAULT 0,
    fetched_at              TEXT,
    CHECK(album_slug IS NULL OR album_slug <> '')
);

CREATE TABLE source_architects (
    architect_id            INTEGER PRIMARY KEY,
    slug                    TEXT,
    name                    TEXT NOT NULL,
    description             TEXT,
    country_raw             TEXT,
    city_raw                TEXT,
    country_normalized      TEXT,
    website                 TEXT,
    phone                   TEXT,
    project_count_seen      INTEGER,
    record_source           TEXT NOT NULL DEFAULT 'architect_index'
        CHECK(record_source IN (
          'architect_index',
          'project_reference_aligned',
          'project_reference_unresolved'
        )),
    name_confidence         REAL NOT NULL DEFAULT 1.0
        CHECK(name_confidence BETWEEN 0 AND 1),
    identity_notes          TEXT,
    fetched_at              TEXT
);

CREATE TABLE source_articles (
    article_id              INTEGER PRIMARY KEY,
    snapshot_id             INTEGER NOT NULL REFERENCES source_snapshots(snapshot_id),
    slug                    TEXT NOT NULL,
    source_url              TEXT NOT NULL UNIQUE,
    name_raw                TEXT NOT NULL,
    name_normalized         TEXT NOT NULL,
    abstract_raw            TEXT,
    location_country_raw    TEXT,
    location_country        TEXT,
    location_city_raw       TEXT,
    location_city           TEXT,
    project_year            INTEGER,
    area_sqm                REAL,
    article_kind            TEXT NOT NULL DEFAULT 'project',
    description_quality     TEXT NOT NULL,
    description_ui_markers  INTEGER NOT NULL DEFAULT 0,
    source_row_hash         TEXT NOT NULL,
    tag_count               INTEGER NOT NULL DEFAULT 0,
    image_count             INTEGER NOT NULL DEFAULT 0,
    content_score           REAL NOT NULL DEFAULT 0,
    fetched_at              TEXT
);

CREATE TABLE article_text_versions (
    text_id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id              INTEGER NOT NULL REFERENCES source_articles(article_id),
    text_kind               TEXT NOT NULL,
    text                    TEXT NOT NULL,
    language                TEXT,
    quality_status          TEXT NOT NULL,
    processor_version       TEXT NOT NULL,
    is_current              INTEGER NOT NULL CHECK(is_current IN (0,1)),
    checksum                TEXT NOT NULL,
    run_id                  INTEGER NOT NULL REFERENCES build_runs(run_id),
    UNIQUE(article_id, text_kind, processor_version)
);

CREATE UNIQUE INDEX uq_article_text_current
ON article_text_versions(article_id, text_kind)
WHERE is_current=1;

CREATE TABLE article_architects (
    article_id              INTEGER NOT NULL REFERENCES source_articles(article_id),
    position                INTEGER NOT NULL,
    architect_id            INTEGER NOT NULL REFERENCES source_architects(architect_id),
    architect_name          TEXT NOT NULL,
    role                    TEXT NOT NULL DEFAULT 'designer',
    PRIMARY KEY(article_id, position)
);

CREATE TABLE article_attributions (
    attribution_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id              INTEGER NOT NULL REFERENCES source_articles(article_id),
    role                    TEXT NOT NULL,
    name                    TEXT NOT NULL,
    source_order            INTEGER NOT NULL,
    identity_relevance      TEXT NOT NULL DEFAULT 'provenance_only',
    UNIQUE(article_id, role, name)
);

CREATE TABLE article_tags (
    article_id              INTEGER NOT NULL REFERENCES source_articles(article_id),
    tag_slug                TEXT NOT NULL REFERENCES source_tags(tag_slug),
    ordinal                 INTEGER NOT NULL,
    PRIMARY KEY(article_id, tag_slug),
    UNIQUE(article_id, ordinal)
);

CREATE TABLE controlled_terms (
    axis                    TEXT NOT NULL,
    value                   TEXT NOT NULL,
    label                   TEXT NOT NULL,
    is_searchable           INTEGER NOT NULL CHECK(is_searchable IN (0,1)),
    vocab_version           TEXT NOT NULL,
    PRIMARY KEY(axis, value, vocab_version)
);

CREATE TABLE tag_crosswalk (
    mapping_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tag_slug                TEXT NOT NULL REFERENCES source_tags(tag_slug),
    album_slug              TEXT NOT NULL,
    target_scope            TEXT NOT NULL CHECK(target_scope IN ('building','article')),
    target_axis             TEXT NOT NULL,
    target_value            TEXT NOT NULL,
    mapping_kind            TEXT NOT NULL CHECK(mapping_kind IN ('direct','supporting','editorial','exclusion')),
    base_confidence         REAL NOT NULL CHECK(base_confidence BETWEEN 0 AND 1),
    priority                INTEGER NOT NULL,
    search_tier             TEXT NOT NULL CHECK(search_tier IN ('primary','secondary','hidden')),
    enabled                 INTEGER NOT NULL CHECK(enabled IN (0,1)),
    mapping_version         TEXT NOT NULL,
    notes                   TEXT,
    UNIQUE(tag_slug, target_scope, target_axis, target_value, mapping_version)
);

CREATE TABLE image_assets (
    asset_key               TEXT PRIMARY KEY,
    provider                TEXT NOT NULL DEFAULT 'divisare',
    public_id               TEXT,
    original_filename       TEXT,
    url_generation          TEXT NOT NULL,
    first_seen_article_id   INTEGER REFERENCES source_articles(article_id),
    mime_type               TEXT,
    width                   INTEGER,
    height                  INTEGER,
    byte_size               INTEGER,
    content_sha256          TEXT,
    fetch_status            TEXT NOT NULL DEFAULT 'pending'
        CHECK(fetch_status IN ('pending','success','failed','skipped')),
    last_fetch_error        TEXT,
    fetched_at              TEXT
);

CREATE TABLE image_urls (
    url_id                  INTEGER PRIMARY KEY,
    asset_key               TEXT NOT NULL REFERENCES image_assets(asset_key),
    url                     TEXT NOT NULL UNIQUE,
    transform_signature     TEXT,
    url_generation          TEXT NOT NULL,
    UNIQUE(url_id, asset_key)
);

CREATE TABLE source_image_occurrences (
    article_id              INTEGER NOT NULL REFERENCES source_articles(article_id),
    role                    TEXT NOT NULL CHECK(role IN ('cover','gallery')),
    position                INTEGER NOT NULL,
    raw_url                 TEXT NOT NULL,
    parse_status            TEXT NOT NULL CHECK(parse_status IN ('parsed','malformed')),
    parse_error             TEXT,
    asset_key               TEXT REFERENCES image_assets(asset_key),
    PRIMARY KEY(article_id, role, position),
    CHECK(
      (parse_status='parsed' AND asset_key IS NOT NULL AND parse_error IS NULL)
      OR
      (parse_status='malformed' AND asset_key IS NULL AND parse_error IS NOT NULL)
    )
);

CREATE TABLE article_image_occurrences (
    article_id              INTEGER NOT NULL REFERENCES source_articles(article_id),
    role                    TEXT NOT NULL CHECK(role IN ('cover','gallery')),
    position                INTEGER NOT NULL,
    asset_key               TEXT NOT NULL REFERENCES image_assets(asset_key),
    url_id                  INTEGER NOT NULL REFERENCES image_urls(url_id),
    PRIMARY KEY(article_id, role, position),
    FOREIGN KEY(article_id, role, position)
      REFERENCES source_image_occurrences(article_id, role, position),
    FOREIGN KEY(url_id, asset_key)
      REFERENCES image_urls(url_id, asset_key)
);

CREATE TABLE image_url_hints (
    asset_key               TEXT NOT NULL REFERENCES image_assets(asset_key),
    url_id                  INTEGER NOT NULL REFERENCES image_urls(url_id),
    hint                    TEXT NOT NULL,
    confidence              REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
    rule_version            TEXT NOT NULL,
    PRIMARY KEY(asset_key, url_id, hint, rule_version)
);

CREATE TABLE image_hashes (
    asset_key               TEXT NOT NULL REFERENCES image_assets(asset_key),
    algorithm               TEXT NOT NULL,
    algorithm_version       TEXT NOT NULL,
    hash_bits               INTEGER,
    hash_hex                TEXT,
    status                  TEXT NOT NULL CHECK(status IN ('pending','success','failed','skipped')),
    attempt_count           INTEGER NOT NULL DEFAULT 0,
    last_error              TEXT,
    computed_at             TEXT,
    run_id                  INTEGER REFERENCES build_runs(run_id),
    PRIMARY KEY(asset_key, algorithm, algorithm_version),
    CHECK(status <> 'success' OR (hash_bits=256 AND length(hash_hex)=64))
);

CREATE TABLE image_hash_bands (
    asset_key               TEXT NOT NULL REFERENCES image_assets(asset_key),
    algorithm_version       TEXT NOT NULL,
    band_index              INTEGER NOT NULL,
    band_value              TEXT NOT NULL,
    PRIMARY KEY(asset_key, algorithm_version, band_index)
);

CREATE TABLE image_classifications (
    asset_key               TEXT NOT NULL REFERENCES image_assets(asset_key),
    axis                    TEXT NOT NULL,
    value                   TEXT NOT NULL,
    confidence              REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
    source_kind             TEXT NOT NULL,
    model_version           TEXT NOT NULL,
    status                  TEXT NOT NULL CHECK(status IN ('candidate','confirmed','rejected')),
    run_id                  INTEGER REFERENCES build_runs(run_id),
    PRIMARY KEY(asset_key, axis, value, model_version)
);

CREATE TABLE image_match_candidates (
    asset_key_a             TEXT NOT NULL REFERENCES image_assets(asset_key),
    asset_key_b             TEXT NOT NULL REFERENCES image_assets(asset_key),
    algorithm_version       TEXT NOT NULL,
    hamming_distance        INTEGER NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'open',
    PRIMARY KEY(asset_key_a, asset_key_b, algorithm_version),
    CHECK(asset_key_a < asset_key_b)
);

CREATE TABLE attribute_claims (
    claim_id                INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id              INTEGER NOT NULL REFERENCES source_articles(article_id),
    image_asset_key         TEXT REFERENCES image_assets(asset_key),
    scope                   TEXT NOT NULL CHECK(scope IN ('article','building','image')),
    axis                    TEXT NOT NULL,
    value_raw               TEXT,
    value_normalized        TEXT NOT NULL,
    evidence_kind           TEXT NOT NULL,
    source_ref              TEXT,
    source_tag_slug         TEXT REFERENCES source_tags(tag_slug),
    mapping_id              INTEGER REFERENCES tag_crosswalk(mapping_id),
    binding                 TEXT NOT NULL DEFAULT 'atomic',
    confidence              REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
    confidence_class        TEXT NOT NULL CHECK(confidence_class IN ('high','medium','low')),
    polarity                TEXT NOT NULL DEFAULT 'positive'
        CHECK(polarity IN ('positive','negative')),
    search_tier             TEXT NOT NULL CHECK(search_tier IN ('primary','secondary','hidden')),
    extractor_version       TEXT NOT NULL,
    run_id                  INTEGER NOT NULL REFERENCES build_runs(run_id),
    details_json            TEXT CHECK(details_json IS NULL OR json_valid(details_json)),
    UNIQUE(article_id, scope, axis, value_normalized, evidence_kind, source_ref, extractor_version),
    CHECK(
      (scope='image' AND image_asset_key IS NOT NULL)
      OR
      (scope<>'image' AND image_asset_key IS NULL)
    )
);

CREATE TABLE location_claims (
    location_claim_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id              INTEGER NOT NULL REFERENCES source_articles(article_id),
    source_kind             TEXT NOT NULL,
    source_ref              TEXT,
    raw_text                TEXT,
    country_name            TEXT,
    region_name             TEXT,
    city_name               TEXT,
    confidence              REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
    run_id                  INTEGER NOT NULL REFERENCES build_runs(run_id)
);

CREATE TABLE article_match_candidates (
    article_id_a            INTEGER NOT NULL REFERENCES source_articles(article_id),
    article_id_b            INTEGER NOT NULL REFERENCES source_articles(article_id),
    candidate_kind          TEXT NOT NULL,
    score                   REAL NOT NULL CHECK(score BETWEEN 0 AND 1),
    signals_json            TEXT NOT NULL CHECK(json_valid(signals_json)),
    status                  TEXT NOT NULL CHECK(status IN ('auto_clustered','open','rejected','confirmed')),
    cluster_version         TEXT NOT NULL,
    created_run_id          INTEGER NOT NULL REFERENCES build_runs(run_id),
    PRIMARY KEY(article_id_a, article_id_b, cluster_version),
    CHECK(article_id_a < article_id_b)
);

CREATE TABLE buildings (
    building_id             TEXT PRIMARY KEY,
    primary_article_id      INTEGER NOT NULL REFERENCES source_articles(article_id),
    name                    TEXT NOT NULL,
    name_normalized         TEXT NOT NULL,
    location_country        TEXT,
    location_city           TEXT,
    location_resolution_method TEXT NOT NULL,
    location_confidence     REAL NOT NULL CHECK(location_confidence BETWEEN 0 AND 1),
    project_year            INTEGER,
    year_kind               TEXT NOT NULL,
    area_sqm                REAL,
    program                 TEXT,
    program_confidence      REAL,
    typology_primary        TEXT,
    typology_confidence     REAL,
    style                   TEXT,
    structural_system       TEXT,
    roof_type               TEXT,
    facade_pattern          TEXT,
    facade_system           TEXT,
    description_text_id     INTEGER REFERENCES article_text_versions(text_id),
    cluster_status          TEXT NOT NULL,
    cluster_confidence      REAL NOT NULL CHECK(cluster_confidence BETWEEN 0 AND 1),
    article_count           INTEGER NOT NULL,
    needs_review            INTEGER NOT NULL CHECK(needs_review IN (0,1)),
    resolution_version      TEXT NOT NULL,
    redirect_to             TEXT REFERENCES buildings(building_id),
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);

CREATE TABLE building_articles (
    article_id              INTEGER PRIMARY KEY REFERENCES source_articles(article_id),
    building_id             TEXT NOT NULL REFERENCES buildings(building_id),
    article_role            TEXT NOT NULL,
    membership_confidence   REAL NOT NULL CHECK(membership_confidence BETWEEN 0 AND 1),
    decision_method         TEXT NOT NULL,
    linked_at               TEXT NOT NULL
);

CREATE TABLE cluster_events (
    event_id                INTEGER PRIMARY KEY AUTOINCREMENT,
    building_id             TEXT NOT NULL REFERENCES buildings(building_id),
    event_type              TEXT NOT NULL,
    article_ids_json        TEXT NOT NULL CHECK(json_valid(article_ids_json)),
    reason_json             TEXT NOT NULL CHECK(json_valid(reason_json)),
    cluster_version         TEXT NOT NULL,
    run_id                  INTEGER NOT NULL REFERENCES build_runs(run_id),
    created_at              TEXT NOT NULL
);

CREATE TABLE building_facets (
    facet_id                INTEGER PRIMARY KEY AUTOINCREMENT,
    building_id             TEXT NOT NULL REFERENCES buildings(building_id),
    axis                    TEXT NOT NULL,
    value                   TEXT NOT NULL,
    status                  TEXT NOT NULL CHECK(status IN ('candidate','confirmed','rejected')),
    role                    TEXT NOT NULL CHECK(role IN ('primary','secondary','facet')),
    confidence              REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
    claim_count             INTEGER NOT NULL,
    article_count           INTEGER NOT NULL,
    direct_claim_count      INTEGER NOT NULL,
    supporting_claim_count  INTEGER NOT NULL,
    source_count            INTEGER NOT NULL,
    max_priority            INTEGER NOT NULL,
    search_tier             TEXT NOT NULL CHECK(search_tier IN ('primary','secondary','hidden')),
    resolver_version        TEXT NOT NULL,
    UNIQUE(building_id, axis, value)
);

CREATE TABLE building_facet_claims (
    facet_id                INTEGER NOT NULL REFERENCES building_facets(facet_id),
    claim_id                INTEGER NOT NULL REFERENCES attribute_claims(claim_id),
    weight                  REAL NOT NULL,
    PRIMARY KEY(facet_id, claim_id)
);

CREATE TABLE qa_issues (
    issue_id                INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type             TEXT NOT NULL,
    entity_key              TEXT NOT NULL,
    check_code              TEXT NOT NULL,
    severity                TEXT NOT NULL CHECK(severity IN ('info','warning','error')),
    status                  TEXT NOT NULL CHECK(status IN ('open','resolved','ignored')),
    details_json            TEXT CHECK(details_json IS NULL OR json_valid(details_json)),
    detected_run_id         INTEGER NOT NULL REFERENCES build_runs(run_id),
    resolved_run_id         INTEGER REFERENCES build_runs(run_id),
    created_at              TEXT NOT NULL,
    resolved_at             TEXT,
    UNIQUE(entity_type, entity_key, check_code, detected_run_id)
);

CREATE TABLE build_metrics (
    run_id                  INTEGER NOT NULL REFERENCES build_runs(run_id),
    metric                  TEXT NOT NULL,
    value                   REAL NOT NULL,
    details_json            TEXT CHECK(details_json IS NULL OR json_valid(details_json)),
    PRIMARY KEY(run_id, metric)
);

CREATE INDEX idx_album_membership_child
ON source_album_memberships(child_slug, album_slug);
CREATE INDEX idx_source_tags_album
ON source_tags(album_slug, tag_slug);
CREATE INDEX idx_articles_identity
ON source_articles(name_normalized, location_country, location_city, project_year);
CREATE INDEX idx_articles_location
ON source_articles(location_country, location_city);
CREATE INDEX idx_article_architect_id
ON article_architects(architect_id, article_id);
CREATE INDEX idx_article_architect_name
ON article_architects(architect_name, article_id);
CREATE INDEX idx_article_tags_reverse
ON article_tags(tag_slug, article_id);
CREATE INDEX idx_crosswalk_lookup
ON tag_crosswalk(tag_slug, enabled, mapping_version);
CREATE INDEX idx_claims_article_axis
ON attribute_claims(article_id, scope, axis);
CREATE INDEX idx_claims_value
ON attribute_claims(axis, value_normalized, confidence);
CREATE INDEX idx_location_claims_article
ON location_claims(article_id, source_kind);
CREATE INDEX idx_image_urls_asset
ON image_urls(asset_key);
CREATE INDEX idx_source_occurrences_asset
ON source_image_occurrences(asset_key, article_id);
CREATE INDEX idx_occurrences_asset
ON article_image_occurrences(asset_key, article_id);
CREATE INDEX idx_occurrences_article
ON article_image_occurrences(article_id, role, position);
CREATE INDEX idx_image_hash_work
ON image_hashes(algorithm, algorithm_version, status);
CREATE INDEX idx_hash_band_lookup
ON image_hash_bands(algorithm_version, band_index, band_value);
CREATE INDEX idx_image_classification_lookup
ON image_classifications(axis, value, status);
CREATE INDEX idx_match_candidates_status
ON article_match_candidates(status, score DESC);
CREATE INDEX idx_buildings_location
ON buildings(location_country, location_city, project_year);
CREATE INDEX idx_buildings_name
ON buildings(name_normalized);
CREATE INDEX idx_building_articles_building
ON building_articles(building_id, article_role);
CREATE INDEX idx_building_facets_search
ON building_facets(axis, value, status, search_tier);
CREATE INDEX idx_qa_open
ON qa_issues(status, severity, check_code);
"""


VIEWS_SQL = """
CREATE VIEW v_source_articles AS
SELECT
    a.*,
    raw_text.text AS description_raw,
    clean_text.text AS description_clean
FROM source_articles a
LEFT JOIN article_text_versions raw_text
  ON raw_text.article_id=a.article_id
 AND raw_text.text_kind='raw_description'
 AND raw_text.is_current=1
LEFT JOIN article_text_versions clean_text
  ON clean_text.article_id=a.article_id
 AND clean_text.text_kind='clean_description'
 AND clean_text.is_current=1;

CREATE VIEW v_article_tag_axes AS
SELECT
    at.article_id,
    at.ordinal,
    st.album_slug,
    at.tag_slug,
    st.label,
    cw.target_scope,
    cw.target_axis,
    cw.target_value,
    cw.mapping_kind,
    cw.base_confidence,
    cw.search_tier,
    cw.mapping_version
FROM article_tags at
JOIN source_tags st ON st.tag_slug=at.tag_slug
LEFT JOIN tag_crosswalk cw
  ON cw.tag_slug=at.tag_slug
 AND cw.enabled=1;

CREATE VIEW v_unmapped_tags AS
SELECT
    st.album_slug,
    st.tag_slug,
    st.label,
    st.used_article_count
FROM source_tags st
WHERE st.used_article_count > 0
  AND NOT EXISTS (
      SELECT 1 FROM tag_crosswalk cw
      WHERE cw.tag_slug=st.tag_slug AND cw.enabled=1
  )
ORDER BY st.used_article_count DESC, st.tag_slug;

CREATE VIEW v_tags_without_normalized_semantics AS
SELECT
    st.album_slug,
    st.tag_slug,
    st.label,
    st.used_article_count
FROM source_tags st
WHERE st.used_article_count > 0
  AND NOT EXISTS (
      SELECT 1
      FROM tag_crosswalk cw
      WHERE cw.tag_slug=st.tag_slug
        AND cw.enabled=1
        AND cw.mapping_kind IN ('direct','supporting')
        AND cw.target_axis NOT IN ('source_typology','source_topic')
  )
ORDER BY st.used_article_count DESC, st.tag_slug;

CREATE VIEW v_tags_without_building_projection AS
SELECT
    st.album_slug,
    st.tag_slug,
    st.label,
    st.used_article_count
FROM source_tags st
WHERE st.used_article_count > 0
  AND NOT EXISTS (
      SELECT 1
      FROM tag_crosswalk cw
      WHERE cw.tag_slug=st.tag_slug
        AND cw.enabled=1
        AND cw.target_scope='building'
        AND cw.mapping_kind IN ('direct','supporting')
        AND cw.target_axis NOT IN ('source_typology','source_topic')
  )
ORDER BY st.used_article_count DESC, st.tag_slug;

CREATE VIEW v_unmapped_building_taxonomy_tags AS
SELECT * FROM v_tags_without_building_projection;

CREATE VIEW v_article_content_hints AS
SELECT
    article_id,
    value_normalized AS content_hint,
    MAX(confidence) AS confidence,
    GROUP_CONCAT(DISTINCT source_tag_slug) AS source_tags
FROM attribute_claims
WHERE scope='article'
  AND axis='content_hint'
  AND polarity='positive'
GROUP BY article_id, value_normalized;

CREATE VIEW v_phash_work_queue AS
SELECT
    ia.asset_key,
    MIN(iu.url) AS candidate_url,
    ia.original_filename,
    ih.attempt_count,
    ih.last_error
FROM image_assets ia
JOIN image_urls iu ON iu.asset_key=ia.asset_key
JOIN image_hashes ih
  ON ih.asset_key=ia.asset_key
 AND ih.algorithm='phash-256'
WHERE ih.status IN ('pending','failed')
GROUP BY ia.asset_key, ia.original_filename, ih.attempt_count, ih.last_error;

CREATE VIEW v_image_classification_queue AS
SELECT
    ia.asset_key,
    MIN(iu.url) AS candidate_url,
    MAX(CASE WHEN iuh.hint IS NOT NULL THEN 1 ELSE 0 END) AS has_filename_hint,
    MAX(CASE WHEN ach.article_id IS NOT NULL THEN 1 ELSE 0 END) AS has_article_content_hint,
    CASE
      WHEN MAX(CASE WHEN iuh.hint IS NOT NULL THEN 1 ELSE 0 END)=1 THEN 100
      WHEN MAX(CASE WHEN ach.article_id IS NOT NULL THEN 1 ELSE 0 END)=1 THEN 80
      ELSE 10
    END AS priority
FROM image_assets ia
JOIN image_urls iu ON iu.asset_key=ia.asset_key
LEFT JOIN image_url_hints iuh ON iuh.asset_key=ia.asset_key
LEFT JOIN article_image_occurrences aio ON aio.asset_key=ia.asset_key
LEFT JOIN v_article_content_hints ach ON ach.article_id=aio.article_id
WHERE NOT EXISTS (
    SELECT 1 FROM image_classifications ic
    WHERE ic.asset_key=ia.asset_key AND ic.axis='media_type' AND ic.status='confirmed'
)
GROUP BY ia.asset_key;

CREATE VIEW v_building_articles AS
SELECT
    b.building_id,
    b.name AS building_name,
    ba.article_id,
    ba.article_role,
    ba.membership_confidence,
    a.name_raw AS article_name,
    a.source_url
FROM buildings b
JOIN building_articles ba ON ba.building_id=b.building_id
JOIN source_articles a ON a.article_id=ba.article_id;

CREATE VIEW v_building_images AS
SELECT
    ba.building_id,
    aio.asset_key,
    MIN(CASE WHEN aio.role='cover' THEN 0 ELSE 1 END) AS role_rank,
    MIN(aio.position) AS first_position,
    MIN(iu.url) AS representative_url,
    COUNT(DISTINCT aio.article_id) AS article_count
FROM building_articles ba
JOIN article_image_occurrences aio ON aio.article_id=ba.article_id
JOIN image_urls iu ON iu.url_id=aio.url_id
GROUP BY ba.building_id, aio.asset_key;

CREATE VIEW v_dedup_review_queue AS
SELECT
    c.*,
    a.name_raw AS name_a,
    b.name_raw AS name_b,
    a.location_country AS country_a,
    b.location_country AS country_b,
    a.location_city AS city_a,
    b.location_city AS city_b,
    a.project_year AS year_a,
    b.project_year AS year_b
FROM article_match_candidates c
JOIN source_articles a ON a.article_id=c.article_id_a
JOIN source_articles b ON b.article_id=c.article_id_b
WHERE c.status='open'
ORDER BY c.score DESC, c.article_id_a, c.article_id_b;

CREATE VIEW v_search_facets AS
SELECT
    building_id,
    axis,
    value,
    role,
    confidence,
    claim_count,
    article_count,
    direct_claim_count,
    supporting_claim_count,
    source_count,
    search_tier
FROM building_facets
WHERE status IN ('confirmed','candidate')
  AND search_tier <> 'hidden';

CREATE VIEW v_qa_open AS
SELECT *
FROM qa_issues
WHERE status='open'
ORDER BY
  CASE severity WHEN 'error' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
  check_code,
  entity_key;

CREATE VIEW v_building_completeness AS
SELECT
    b.building_id,
    b.name,
    b.article_count,
    (b.location_country IS NOT NULL) AS has_country,
    (b.location_city IS NOT NULL) AS has_city,
    (b.project_year IS NOT NULL) AS has_year,
    (b.program IS NOT NULL) AS has_program,
    (b.typology_primary IS NOT NULL) AS has_typology,
    (b.description_text_id IS NOT NULL) AS has_description,
    EXISTS(SELECT 1 FROM v_building_images bi WHERE bi.building_id=b.building_id) AS has_images,
    b.needs_review
FROM buildings b;

CREATE VIEW v_divisare_buildings_export AS
SELECT
    b.building_id AS canonical_bld_id,
    b.primary_article_id AS primary_divisare_id,
    json_object(
      'divisare',
      json((
        SELECT json_group_array(article_id)
        FROM (
          SELECT ba2.article_id
          FROM building_articles ba2
          WHERE ba2.building_id=b.building_id
          ORDER BY ba2.article_id
        )
      ))
    ) AS source_refs,
    b.name,
    b.location_city,
    b.location_country,
    b.location_resolution_method,
    b.location_confidence,
    b.project_year,
    COALESCE((
      SELECT json_group_array(architect_id)
      FROM (
        SELECT DISTINCT aa.architect_id
        FROM building_articles ba3
        JOIN article_architects aa ON aa.article_id=ba3.article_id
        WHERE ba3.building_id=b.building_id
          AND aa.architect_id IS NOT NULL
        ORDER BY aa.architect_id
      )
    ), '[]') AS architect_canonical_ids,
    COALESCE((
      SELECT json_group_array(architect_name)
      FROM (
        SELECT DISTINCT aa.architect_name
        FROM building_articles ba4
        JOIN article_architects aa ON aa.article_id=ba4.article_id
        WHERE ba4.building_id=b.building_id
          AND aa.architect_name IS NOT NULL
        ORDER BY aa.architect_name
      )
    ), '[]') AS architect_names,
    b.program,
    b.typology_primary,
    COALESCE((
      SELECT json_group_array(value)
      FROM (
        SELECT value
        FROM building_facets f
        WHERE f.building_id=b.building_id
          AND f.axis='typology'
          AND f.status='confirmed'
        ORDER BY f.confidence DESC, f.value
      )
    ), '[]') AS typology_tags,
    COALESCE((
      SELECT json_group_array(value)
      FROM (
        SELECT value
        FROM building_facets f
        WHERE f.building_id=b.building_id
          AND f.axis='material'
          AND f.status='confirmed'
        ORDER BY f.confidence DESC, f.value
      )
    ), '[]') AS material_visual,
    COALESCE((
      SELECT json_group_array(value)
      FROM (
        SELECT value
        FROM building_facets f
        WHERE f.building_id=b.building_id
          AND f.axis='color'
          AND f.status='confirmed'
        ORDER BY f.confidence DESC, f.value
      )
    ), '[]') AS colors,
    b.style,
    b.structural_system,
    COALESCE((
      SELECT json_group_array(value)
      FROM (
        SELECT value
        FROM building_facets f
        WHERE f.building_id=b.building_id
          AND f.axis='facade_material'
          AND f.status='confirmed'
        ORDER BY f.confidence DESC, f.value
      )
    ), '[]') AS facade_materials,
    b.facade_pattern,
    b.facade_system,
    b.roof_type,
    COALESCE((
      SELECT json_group_array(value)
      FROM (
        SELECT value
        FROM building_facets f
        WHERE f.building_id=b.building_id AND f.axis='architectural_element'
          AND f.status='confirmed'
          AND f.search_tier <> 'hidden'
        ORDER BY f.confidence DESC, f.value
      )
    ), '[]') AS architectural_elements,
    COALESCE((
      SELECT json_group_array(value)
      FROM (
        SELECT value
        FROM building_facets f
        WHERE f.building_id=b.building_id
          AND f.axis='site_context'
          AND f.status='confirmed'
        ORDER BY f.confidence DESC, f.value
      )
    ), '[]') AS site_contexts,
    COALESCE((
      SELECT json_group_array(value)
      FROM (
        SELECT value
        FROM building_facets f
        WHERE f.building_id=b.building_id
          AND f.axis='intervention_type'
          AND f.status='confirmed'
        ORDER BY f.confidence DESC, f.value
      )
    ), '[]') AS intervention_types,
    COALESCE((
      SELECT json_group_array(tag_slug)
      FROM (
        SELECT DISTINCT at.tag_slug
        FROM building_articles ba5
        JOIN article_tags at ON at.article_id=ba5.article_id
        WHERE ba5.building_id=b.building_id
        ORDER BY at.tag_slug
      )
    ), '[]') AS source_categories,
    (
      SELECT iu.url
      FROM article_image_occurrences aio
      JOIN image_urls iu ON iu.url_id=aio.url_id
      WHERE aio.article_id=b.primary_article_id AND aio.role='cover'
      ORDER BY aio.position
      LIMIT 1
    ) AS cover_image_url,
    COALESCE((
      SELECT json_group_array(representative_url)
      FROM (
        SELECT bi.representative_url
        FROM v_building_images bi
        WHERE bi.building_id=b.building_id
        ORDER BY bi.role_rank, bi.first_position, bi.asset_key
      )
    ), '[]') AS gallery_image_urls,
    tv.text AS description,
    pa.description_quality,
    pa.description_ui_markers,
    b.cluster_confidence,
    b.needs_review
FROM buildings b
JOIN source_articles pa ON pa.article_id=b.primary_article_id
LEFT JOIN article_text_versions tv ON tv.text_id=b.description_text_id
WHERE b.redirect_to IS NULL;
"""


class UnionFind:
    def __init__(self, values: Iterable[int]):
        self.parent = {value: value for value in values}
        self.rank = {value: 0 for value in values}

    def find(self, value: int) -> int:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: int, right: int) -> None:
        root_left = self.find(left)
        root_right = self.find(right)
        if root_left == root_right:
            return
        if self.rank[root_left] < self.rank[root_right]:
            root_left, root_right = root_right, root_left
        self.parent[root_right] = root_left
        if self.rank[root_left] == self.rank[root_right]:
            self.rank[root_left] += 1


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


def insert_text(
    conn: sqlite3.Connection,
    *,
    article_id: int,
    text_kind: str,
    text: Optional[str],
    quality_status: str,
    processor_version: str,
    run_id: int,
) -> Optional[int]:
    if not text:
        return None
    checksum = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
    cursor = conn.execute(
        """
        INSERT INTO article_text_versions(
            article_id, text_kind, text, quality_status, processor_version,
            is_current, checksum, run_id
        ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            article_id,
            text_kind,
            text,
            quality_status,
            processor_version,
            checksum,
            run_id,
        ),
    )
    return int(cursor.lastrowid)


def import_source_taxonomy(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
) -> None:
    target.executemany(
        """
        INSERT INTO source_albums(album_slug,name,kind,child_count,fetched_at)
        VALUES (?,?,?,?,?)
        """,
        [
            (r["slug"], r["name"], r["kind"], r["child_count"], r["fetched_at"])
            for r in source.execute(
                "SELECT slug,name,kind,child_count,fetched_at FROM divisare_albums"
            )
        ],
    )
    target.executemany(
        """
        INSERT INTO source_album_memberships(album_slug,child_slug,child_name,child_url)
        VALUES (?,?,?,?)
        """,
        [
            (r["album_slug"], r["child_slug"], r["child_name"], r["child_url"])
            for r in source.execute(
                """
                SELECT album_slug,child_slug,child_name,child_url
                FROM divisare_album_membership
                """
            )
        ],
    )

    landing = {
        r["slug"]: dict(r)
        for r in source.execute(
            """
            SELECT p.slug, p.status AS landing_status, t.name, t.curated,
                   t.project_count_seen, t.fetched_at
            FROM pending_tags p
            LEFT JOIN divisare_tags t ON t.slug=p.slug
            """
        )
    }
    tag_members = source.execute(
        """
        SELECT m.child_slug, m.child_name, m.child_url, m.album_slug
        FROM divisare_album_membership m
        JOIN divisare_albums a ON a.slug=m.album_slug
        WHERE a.kind='tag_album'
        ORDER BY m.album_slug,m.child_slug
        """
    ).fetchall()
    for row in tag_members:
        page = landing.get(row["child_slug"], {})
        target.execute(
            """
            INSERT INTO source_tags(
                tag_slug,label,album_slug,child_url,landing_status,curated,
                project_count_seen,fetched_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                row["child_slug"],
                row["child_name"],
                row["album_slug"],
                row["child_url"],
                page.get("landing_status"),
                page.get("curated"),
                page.get("project_count_seen"),
                page.get("fetched_at"),
            ),
        )


def import_architects(source: sqlite3.Connection, target: sqlite3.Connection) -> None:
    target.executemany(
        """
        INSERT INTO source_architects(
            architect_id,slug,name,description,country_raw,city_raw,
            country_normalized,website,phone,project_count_seen,fetched_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (
                r["id"],
                r["slug"],
                r["name"],
                r["description"],
                r["country"],
                r["city"],
                normalize_country(r["country"]),
                r["website"],
                r["phone"],
                r["project_count_seen"],
                r["fetched_at"],
            )
            for r in source.execute(
                """
                SELECT id,slug,name,description,country,city,website,phone,
                       project_count_seen,fetched_at
                FROM divisare_architects
                """
            )
        ],
    )
    known_ids = {
        int(row["architect_id"])
        for row in target.execute("SELECT architect_id FROM source_architects")
    }
    reference_counts: Counter[int] = Counter()
    aligned_candidates: dict[int, dict[str, str]] = defaultdict(dict)
    for row in source.execute(
        "SELECT architect_ids,architect_names FROM divisare_projects"
    ):
        architect_ids = [
            int(value)
            for value in parse_json_list(row["architect_ids"])
            if str(value).strip().isdigit()
        ]
        architect_names = [
            value
            for value in (
                clean_scalar(item)
                for item in parse_json_list(row["architect_names"])
            )
            if value
        ]
        reference_counts.update(architect_ids)
        if len(architect_ids) != len(architect_names):
            continue
        for architect_id, architect_name in zip(architect_ids, architect_names):
            normalized = normalize_identity_text(architect_name)
            if normalized:
                aligned_candidates[architect_id][normalized] = architect_name

    for architect_id in sorted(set(reference_counts) - known_ids):
        candidates = aligned_candidates.get(architect_id, {})
        if len(candidates) == 1:
            name = next(iter(candidates.values()))
            record_source = "project_reference_aligned"
            name_confidence = 0.8
        else:
            name = f"Divisare architect {architect_id}"
            record_source = "project_reference_unresolved"
            name_confidence = 0.0
        target.execute(
            """
            INSERT INTO source_architects(
                architect_id,name,project_count_seen,record_source,
                name_confidence,identity_notes
            ) VALUES (?,?,?,?,?,?)
            """,
            (
                architect_id,
                name,
                reference_counts[architect_id],
                record_source,
                name_confidence,
                json_dumps(
                    {
                        "reason": "referenced by project but absent from architect index",
                        "aligned_name_candidates": sorted(candidates.values()),
                    }
                ),
            ),
        )


def ensure_source_tag(target: sqlite3.Connection, tag_slug: str) -> None:
    target.execute(
        """
        INSERT OR IGNORE INTO source_tags(tag_slug,label,album_slug,landing_status)
        VALUES (?,? ,NULL,'not_in_album_inventory')
        """,
        (tag_slug, tag_slug.replace("-", " ").title()),
    )


def insert_image_occurrence(
    target: sqlite3.Connection,
    *,
    article_id: int,
    role: str,
    position: int,
    url: str,
    next_url_id: list[int],
) -> bool:
    identity = divisare_asset_identity(url)
    if identity is None:
        target.execute(
            """
            INSERT INTO source_image_occurrences(
                article_id,role,position,raw_url,parse_status,parse_error
            ) VALUES (?,?,?,?,'malformed','unsupported_divisare_url')
            """,
            (article_id, role, position, url),
        )
        return False
    target.execute(
        """
        INSERT OR IGNORE INTO image_assets(
            asset_key,public_id,original_filename,url_generation,first_seen_article_id
        ) VALUES (?,?,?,?,?)
        """,
        (
            identity.asset_key,
            identity.public_id,
            identity.original_filename,
            identity.url_generation,
            article_id,
        ),
    )
    target.execute(
        """
        INSERT INTO source_image_occurrences(
            article_id,role,position,raw_url,parse_status,asset_key
        ) VALUES (?,?,?,?,'parsed',?)
        """,
        (article_id, role, position, url, identity.asset_key),
    )
    url_id = next_url_id[0]
    try:
        target.execute(
            """
            INSERT INTO image_urls(
                url_id,asset_key,url,transform_signature,url_generation
            ) VALUES (?,?,?,?,?)
            """,
            (
                url_id,
                identity.asset_key,
                url,
                identity.transform_signature,
                identity.url_generation,
            ),
        )
        next_url_id[0] += 1
    except sqlite3.IntegrityError:
        existing = target.execute(
            "SELECT url_id,asset_key FROM image_urls WHERE url=?", (url,)
        ).fetchone()
        if not existing or existing["asset_key"] != identity.asset_key:
            raise
        url_id = existing["url_id"]

    target.execute(
        """
        INSERT INTO article_image_occurrences(
            article_id,role,position,asset_key,url_id
        ) VALUES (?,?,?,?,?)
        """,
        (article_id, role, position, identity.asset_key, url_id),
    )
    for hint in filename_media_hints(identity):
        target.execute(
            """
            INSERT OR IGNORE INTO image_url_hints(
                asset_key,url_id,hint,confidence,rule_version
            ) VALUES (?,?,?,0.7,?)
            """,
            (identity.asset_key, url_id, hint, URL_HINT_VERSION),
        )
    return True


def import_articles(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    *,
    snapshot_id: int,
    run_id: int,
    limit_rows: Optional[int],
) -> dict[str, int]:
    sql = "SELECT * FROM divisare_projects ORDER BY id"
    params: tuple[Any, ...] = ()
    if limit_rows is not None:
        sql += " LIMIT ?"
        params = (limit_rows,)
    rows = source.execute(sql, params)
    next_url_id = [1]
    metrics = Counter()
    architect_index = {
        int(row["architect_id"]): row["name"]
        for row in target.execute(
            "SELECT architect_id,name FROM source_architects"
        )
    }

    for row in rows:
        article_id = int(row["id"])
        architects_ids = list(dict.fromkeys(
            int(v) for v in parse_json_list(row["architect_ids"])
            if str(v).strip().isdigit()
        ))
        architect_names = [
            clean_scalar(v) for v in parse_json_list(row["architect_names"])
        ]
        architect_names = [v for v in architect_names if v]
        tags = [
            clean_scalar(v) for v in parse_json_list(row["tag_slugs"])
        ]
        tags = list(dict.fromkeys(v for v in tags if v))
        gallery_urls = [
            clean_scalar(v) for v in parse_json_list(row["gallery_urls"])
        ]
        gallery_urls = [v for v in gallery_urls if v]
        cleaned = clean_description(row["description"])
        raw_country = clean_scalar(row["location_country"])
        country = normalize_country(row["location_country"])
        city = clean_location(row["location_city"])
        if (
            country is None
            and city is None
            and raw_country
            and raw_country.startswith("- ")
        ):
            city = clean_location(raw_country[2:])
            if city:
                metrics["recovered_city_from_country_field"] += 1
        abstract = clean_scalar(row["abstract"])
        source_url = f"https://divisare.com/projects/{article_id}-{row['slug']}"
        expected_images = len(gallery_urls) + (1 if row["cover_image_url"] else 0)
        content_score = (
            min(len(cleaned.text or ""), 10000) / 1000.0
            + min(len(gallery_urls), 50) * 0.25
            + len(tags) * 0.15
            + (1.0 if abstract else 0.0)
            + (0.5 if country else 0.0)
            + (0.5 if city else 0.0)
            + (0.5 if row["project_year"] else 0.0)
        )
        target.execute(
            """
            INSERT INTO source_articles(
                article_id,snapshot_id,slug,source_url,name_raw,name_normalized,
                abstract_raw,location_country_raw,location_country,
                location_city_raw,location_city,project_year,area_sqm,
                description_quality,description_ui_markers,source_row_hash,
                tag_count,image_count,content_score,fetched_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                article_id,
                snapshot_id,
                row["slug"],
                source_url,
                row["name"],
                normalize_identity_text(row["name"]),
                abstract,
                row["location_country"],
                country,
                row["location_city"],
                city,
                row["project_year"],
                row["area_sqm"],
                cleaned.quality_status,
                cleaned.removed_ui_markers,
                source_row_hash(row),
                len(tags),
                expected_images,
                content_score,
                row["fetched_at"],
            ),
        )
        if row["description"]:
            insert_text(
                target,
                article_id=article_id,
                text_kind="raw_description",
                text=str(row["description"]),
                quality_status="raw",
                processor_version="raw-v1",
                run_id=run_id,
            )
        if cleaned.text:
            insert_text(
                target,
                article_id=article_id,
                text_kind="clean_description",
                text=cleaned.text,
                quality_status=cleaned.quality_status,
                processor_version=TEXT_PROCESSOR_VERSION,
                run_id=run_id,
            )

        canonical_architect_names: set[str] = set()
        for position, architect_id in enumerate(architects_ids):
            architect_name = architect_index.get(architect_id)
            if not architect_name:
                raise RuntimeError(
                    f"article {article_id} references missing architect {architect_id}"
                )
            canonical_architect_names.add(normalize_identity_text(architect_name))
            target.execute(
                """
                INSERT INTO article_architects(
                    article_id,position,architect_id,architect_name
                ) VALUES (?,?,?,?)
                """,
                (
                    article_id,
                    position,
                    architect_id,
                    architect_name,
                ),
            )
        for source_order, architect_name in enumerate(architect_names):
            if normalize_identity_text(architect_name) in canonical_architect_names:
                continue
            target.execute(
                """
                INSERT OR IGNORE INTO article_attributions(
                    article_id,role,name,source_order,identity_relevance
                ) VALUES (?,'source_architect_name_candidate',?,?,
                          'unverified_architect_display')
                """,
                (article_id, architect_name, source_order),
            )
            metrics["unverified_architect_display_names"] += 1

        for ordinal, tag_slug in enumerate(tags):
            ensure_source_tag(target, tag_slug)
            target.execute(
                "INSERT INTO article_tags(article_id,tag_slug,ordinal) VALUES (?,?,?)",
                (article_id, tag_slug, ordinal),
            )

        imported_images = 0
        cover = clean_scalar(row["cover_image_url"])
        if cover and insert_image_occurrence(
            target,
            article_id=article_id,
            role="cover",
            position=0,
            url=cover,
            next_url_id=next_url_id,
        ):
            imported_images += 1
        for position, url in enumerate(gallery_urls):
            if insert_image_occurrence(
                target,
                article_id=article_id,
                role="gallery",
                position=position,
                url=url,
                next_url_id=next_url_id,
            ):
                imported_images += 1
        if imported_images != expected_images:
            metrics["malformed_image_urls"] += expected_images - imported_images

        metrics["articles"] += 1
        metrics["article_tags"] += len(tags)
        metrics["image_occurrences"] += imported_images
        metrics["ui_markers_removed"] += cleaned.removed_ui_markers
        if cleaned.text:
            metrics["clean_descriptions"] += 1
        if country:
            metrics["with_country"] += 1
        if city:
            metrics["with_city"] += 1
        if row["project_year"]:
            metrics["with_year"] += 1

    target.execute(
        """
        UPDATE source_tags
        SET used_article_count=(
            SELECT COUNT(*) FROM article_tags at WHERE at.tag_slug=source_tags.tag_slug
        )
        """
    )
    target.execute(
        """
        INSERT INTO image_hashes(
            asset_key,algorithm,algorithm_version,status,run_id
        )
        SELECT asset_key,?,?, 'pending',?
        FROM image_assets
        """,
        (PHASH_ALGORITHM, PHASH_ALGORITHM_VERSION, run_id),
    )
    return dict(metrics)


def populate_crosswalk(target: sqlite3.Connection) -> int:
    inserted = 0
    tags = target.execute(
        """
        SELECT tag_slug,label,album_slug
        FROM source_tags
        WHERE album_slug IS NOT NULL
        ORDER BY album_slug,tag_slug
        """
    ).fetchall()
    for tag in tags:
        mappings = mappings_for_tag(tag["album_slug"], tag["tag_slug"], tag["label"])
        for mapping in mappings:
            target.execute(
                """
                INSERT INTO tag_crosswalk(
                    tag_slug,album_slug,target_scope,target_axis,target_value,
                    mapping_kind,base_confidence,priority,search_tier,enabled,
                    mapping_version,notes
                ) VALUES (?,?,?,?,?,?,?,?,?,1,?,?)
                """,
                (
                    tag["tag_slug"],
                    tag["album_slug"],
                    mapping.target_scope,
                    mapping.axis,
                    mapping.value,
                    mapping.mapping_kind,
                    mapping.confidence,
                    mapping.priority,
                    mapping.search_tier,
                    TAXONOMY_VERSION,
                    mapping.notes,
                ),
            )
            target.execute(
                """
                INSERT OR IGNORE INTO controlled_terms(
                    axis,value,label,is_searchable,vocab_version
                ) VALUES (?,?,?,?,?)
                """,
                (
                    mapping.axis,
                    mapping.value,
                    mapping.value,
                    0 if mapping.search_tier == "hidden" else 1,
                    TAXONOMY_VERSION,
                ),
            )
            inserted += 1
    return inserted


def populate_claims(
    target: sqlite3.Connection,
    *,
    run_id: int,
) -> int:
    target.execute(
        """
        INSERT INTO attribute_claims(
            article_id,scope,axis,value_raw,value_normalized,evidence_kind,
            source_ref,source_tag_slug,mapping_id,binding,confidence,
            confidence_class,search_tier,extractor_version,run_id,details_json
        )
        SELECT
            at.article_id,
            cw.target_scope,
            cw.target_axis,
            st.label,
            cw.target_value,
            'source_tag',
            'tag:' || at.tag_slug,
            at.tag_slug,
            cw.mapping_id,
            'atomic',
            cw.base_confidence,
            CASE
              WHEN cw.base_confidence >= 0.9 THEN 'high'
              WHEN cw.base_confidence >= 0.75 THEN 'medium'
              ELSE 'low'
            END,
            cw.search_tier,
            cw.mapping_version,
            ?,
            json_object(
              'album_slug',cw.album_slug,
              'mapping_kind',cw.mapping_kind,
              'priority',cw.priority
            )
        FROM article_tags at
        JOIN source_tags st ON st.tag_slug=at.tag_slug
        JOIN tag_crosswalk cw
          ON cw.tag_slug=at.tag_slug
         AND cw.enabled=1
         AND cw.mapping_version=?
        """,
        (run_id, TAXONOMY_VERSION),
    )

    structured_specs = (
        ("country", "location_country", "location_country_raw"),
        ("city", "location_city", "location_city_raw"),
        ("project_year", "project_year", "project_year"),
        ("area_sqm", "area_sqm", "area_sqm"),
    )
    for axis, normalized_column, raw_column in structured_specs:
        target.execute(
            f"""
            INSERT INTO attribute_claims(
                article_id,scope,axis,value_raw,value_normalized,evidence_kind,
                source_ref,binding,confidence,confidence_class,search_tier,
                extractor_version,run_id
            )
            SELECT
                article_id,'building',?,
                CAST({raw_column} AS TEXT),
                CAST({normalized_column} AS TEXT),
                'structured_field',
                'field:{raw_column}',
                'atomic',1.0,'high','primary',?,?
            FROM source_articles
            WHERE {normalized_column} IS NOT NULL
              AND CAST({normalized_column} AS TEXT) <> ''
            """,
            (axis, BUILDER_VERSION, run_id),
        )

    target.execute(
        """
        INSERT INTO location_claims(
            article_id,source_kind,source_ref,raw_text,country_name,city_name,
            confidence,run_id
        )
        SELECT
            article_id,'structured','fields:location_country/location_city',
            TRIM(COALESCE(location_country_raw,'') || ' - ' || COALESCE(location_city_raw,'')),
            location_country,location_city,1.0,?
        FROM source_articles
        WHERE location_country IS NOT NULL OR location_city IS NOT NULL
        """,
        (run_id,),
    )
    target.execute(
        """
        INSERT INTO location_claims(
            article_id,source_kind,source_ref,raw_text,city_name,confidence,run_id
        )
        SELECT
            c.article_id,'city_tag','tag:' || c.source_tag_slug,c.value_raw,
            c.value_normalized,c.confidence,?
        FROM attribute_claims c
        WHERE c.axis='city_candidate'
        """,
        (run_id,),
    )
    target.execute(
        """
        INSERT INTO location_claims(
            article_id,source_kind,source_ref,raw_text,country_name,confidence,run_id
        )
        SELECT
            c.article_id,'house_country_tag','tag:' || c.source_tag_slug,c.value_raw,
            c.value_normalized,c.confidence,?
        FROM attribute_claims c
        WHERE c.axis='country_candidate'
        """,
        (run_id,),
    )
    target.execute(
        """
        INSERT INTO location_claims(
            article_id,source_kind,source_ref,raw_text,region_name,confidence,run_id
        )
        SELECT
            c.article_id,'regional_tag','tag:' || c.source_tag_slug,c.value_raw,
            c.value_normalized,c.confidence,?
        FROM attribute_claims c
        WHERE c.axis='region_context'
        """,
        (run_id,),
    )

    return target.execute("SELECT COUNT(*) FROM attribute_claims").fetchone()[0]


def architect_tokens(target: sqlite3.Connection) -> dict[int, set[str]]:
    tokens: dict[int, set[str]] = defaultdict(set)
    for row in target.execute(
        """
        SELECT article_id,architect_id
        FROM article_architects
        ORDER BY article_id,position
        """
    ):
        tokens[row["article_id"]].add(f"id:{row['architect_id']}")
    return tokens


def years_compatible(left: Optional[int], right: Optional[int]) -> bool:
    """Return whether both articles carry the same explicit project year."""

    return left is not None and right is not None and left == right


def years_nonconflicting(left: Optional[int], right: Optional[int]) -> bool:
    """Allow missing years only for review candidates, never automatic merges."""

    return left is None or right is None or left == right


def normalized_equal(left: Optional[str], right: Optional[str]) -> bool:
    a = normalize_identity_text(left)
    b = normalize_identity_text(right)
    return bool(a and b and a == b)


def add_candidate(
    candidates: dict[tuple[int, int], dict[str, Any]],
    left: int,
    right: int,
    *,
    kind: str,
    score: float,
    status: str,
    signals: dict[str, Any],
) -> None:
    pair = (min(left, right), max(left, right))
    existing = candidates.get(pair)
    if existing is None or score > existing["score"] or status == "auto_clustered":
        candidates[pair] = {
            "kind": kind,
            "score": min(max(score, 0.0), 1.0),
            "status": status,
            "signals": signals,
        }


def consensus_value(values: Iterable[Any], fallback: Any) -> tuple[Any, bool]:
    clean = [value for value in values if value is not None and value != ""]
    if not clean:
        return fallback, False
    counts = Counter(clean)
    if len(counts) == 1:
        return clean[0], False
    ranked = counts.most_common()
    if len(ranked) == 1 or ranked[0][1] > ranked[1][1]:
        return ranked[0][0], True
    return fallback, True


def create_building_clusters(
    target: sqlite3.Connection,
    *,
    run_id: int,
) -> dict[str, int]:
    articles = {
        int(row["article_id"]): dict(row)
        for row in target.execute(
            """
            SELECT article_id,name_raw,name_normalized,location_country,
                   location_city,project_year,area_sqm,article_kind,
                   content_score
            FROM source_articles
            """
        )
    }
    ids = sorted(articles)
    architects = architect_tokens(target)
    city_tag_candidates: dict[int, set[str]] = defaultdict(set)
    for row in target.execute(
        """
        SELECT article_id,value_normalized
        FROM attribute_claims
        WHERE scope='building'
          AND axis='city_candidate'
          AND polarity='positive'
          AND confidence>=0.9
        """
    ):
        city_tag_candidates[int(row["article_id"])].add(row["value_normalized"])
    union_find = UnionFind(ids)
    candidates: dict[tuple[int, int], dict[str, Any]] = {}

    exact_blocks: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    for article_id, article in articles.items():
        exact_blocks[
            (
                article["name_normalized"],
                normalize_identity_text(article["location_country"]),
                normalize_identity_text(article["location_city"]),
            )
        ].append(article_id)

    for (name_key, country_key, city_key), block in exact_blocks.items():
        if len(block) < 2 or not name_key or not country_key or not city_key:
            continue
        for left, right in itertools.combinations(sorted(block), 2):
            a = articles[left]
            b = articles[right]
            arch_overlap = sorted(architects[left] & architects[right])
            year_exact = years_compatible(a["project_year"], b["project_year"])
            generic = is_generic_building_name(a["name_raw"])
            signals = {
                "name_exact": True,
                "architect_overlap": arch_overlap,
                "country_exact": True,
                "city_exact": True,
                "year_exact": year_exact,
                "generic_name": generic,
            }
            if arch_overlap and year_exact and not generic:
                union_find.union(left, right)
                add_candidate(
                    candidates,
                    left,
                    right,
                    kind="exact_name_architect_location",
                    score=0.99,
                    status="auto_clustered",
                    signals=signals,
                )
            else:
                score = 0.72 + (0.12 if arch_overlap else 0) + (
                    0.06 if year_exact else 0
                )
                add_candidate(
                    candidates,
                    left,
                    right,
                    kind="exact_name_location_review",
                    score=score,
                    status="open",
                    signals=signals,
                )

    architect_blocks: dict[tuple[str, str], set[int]] = defaultdict(set)
    for article_id, article in articles.items():
        country_key = normalize_identity_text(article["location_country"])
        if not country_key:
            continue
        for token in architects[article_id]:
            architect_blocks[(token, country_key)].add(article_id)

    compared: set[tuple[int, int]] = set()
    for (architect_token, country_key), block_set in architect_blocks.items():
        block = sorted(block_set)
        if len(block) < 2:
            continue
        # Pathological portfolios remain reviewable through exact blocking.
        if len(block) > 600:
            continue
        for left, right in itertools.combinations(block, 2):
            pair = (left, right)
            if pair in compared:
                continue
            compared.add(pair)
            a = articles[left]
            b = articles[right]
            if not years_nonconflicting(a["project_year"], b["project_year"]):
                continue
            city_a = normalize_identity_text(a["location_city"])
            city_b = normalize_identity_text(b["location_city"])
            city_compatible = not city_a or not city_b or city_a == city_b
            if not city_compatible:
                continue
            if is_generic_building_name(a["name_raw"]) or is_generic_building_name(b["name_raw"]):
                continue
            similarity = SequenceMatcher(
                None, a["name_normalized"], b["name_normalized"]
            ).ratio()
            if similarity < 0.9 or similarity == 1.0:
                continue
            score = 0.55 + 0.35 * similarity + (0.05 if city_a == city_b else 0)
            add_candidate(
                candidates,
                left,
                right,
                kind="fuzzy_name_same_architect_country",
                score=score,
                status="open",
                signals={
                    "name_similarity": round(similarity, 4),
                    "architect_token": architect_token,
                    "country": country_key,
                    "city_compatible": city_compatible,
                    "year_nonconflicting": True,
                },
            )

    for (left, right), candidate in sorted(candidates.items()):
        target.execute(
            """
            INSERT INTO article_match_candidates(
                article_id_a,article_id_b,candidate_kind,score,signals_json,
                status,cluster_version,created_run_id
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                left,
                right,
                candidate["kind"],
                candidate["score"],
                json_dumps(candidate["signals"]),
                candidate["status"],
                CLUSTER_VERSION,
                run_id,
            ),
        )

    groups: dict[int, list[int]] = defaultdict(list)
    for article_id in ids:
        groups[union_find.find(article_id)].append(article_id)

    now = utc_now()
    merged_groups = 0
    merged_articles = 0
    for member_ids in sorted(groups.values(), key=lambda values: min(values)):
        member_ids = sorted(member_ids)
        if len(member_ids) > 1:
            merged_groups += 1
            merged_articles += len(member_ids)
        building_id = f"div_bld_{min(member_ids):06d}"
        primary_id = max(
            member_ids,
            key=lambda article_id: (
                articles[article_id]["content_score"],
                -article_id,
            ),
        )
        primary = articles[primary_id]
        country, country_conflict = consensus_value(
            [articles[i]["location_country"] for i in member_ids],
            primary["location_country"],
        )
        city, city_conflict = consensus_value(
            [articles[i]["location_city"] for i in member_ids],
            primary["location_city"],
        )
        location_method = (
            "structured_consensus" if country or city else "unresolved"
        )
        location_confidence = 0.8 if country_conflict or city_conflict else (
            1.0 if country or city else 0.0
        )
        if not city:
            candidate_cities = sorted(
                {
                    value
                    for article_id in member_ids
                    for value in city_tag_candidates.get(article_id, set())
                }
            )
            if len(candidate_cities) == 1:
                city = candidate_cities[0]
                location_method = (
                    "structured_country_plus_city_tag"
                    if country
                    else "city_tag_fallback"
                )
                location_confidence = min(
                    location_confidence or 0.95,
                    0.95,
                )
        year, year_conflict = consensus_value(
            [articles[i]["project_year"] for i in member_ids],
            primary["project_year"],
        )
        areas = [articles[i]["area_sqm"] for i in member_ids if articles[i]["area_sqm"]]
        area = areas[0] if areas else None
        description = target.execute(
            """
            SELECT text_id
            FROM article_text_versions
            WHERE article_id=? AND text_kind='clean_description' AND is_current=1
            LIMIT 1
            """,
            (primary_id,),
        ).fetchone()
        needs_review = int(country_conflict or city_conflict or year_conflict)
        target.execute(
            """
            INSERT INTO buildings(
                building_id,primary_article_id,name,name_normalized,
                location_country,location_city,location_resolution_method,
                location_confidence,project_year,year_kind,area_sqm,
                description_text_id,cluster_status,cluster_confidence,
                article_count,needs_review,resolution_version,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                building_id,
                primary_id,
                primary["name_raw"],
                primary["name_normalized"],
                country,
                city,
                location_method,
                location_confidence,
                year,
                "completed" if year and year <= datetime.now().year else (
                    "future" if year else "unknown"
                ),
                area,
                description["text_id"] if description else None,
                "auto_merged" if len(member_ids) > 1 else "singleton",
                0.99 if len(member_ids) == 1 else 0.96,
                len(member_ids),
                needs_review,
                RESOLVER_VERSION,
                now,
                now,
            ),
        )
        for article_id in member_ids:
            article_kind = articles[article_id]["article_kind"]
            if article_id == primary_id:
                role = "primary"
            elif article_kind == "drawing_feature":
                role = "drawing_feature"
            elif article_kind == "photo_feature":
                role = "photo_feature"
            elif article_kind == "concept_editorial":
                role = "concept_editorial"
            else:
                role = "supporting"
            target.execute(
                """
                INSERT INTO building_articles(
                    article_id,building_id,article_role,membership_confidence,
                    decision_method,linked_at
                ) VALUES (?,?,?,?,?,?)
                """,
                (
                    article_id,
                    building_id,
                    role,
                    1.0 if len(member_ids) == 1 else 0.96,
                    "singleton" if len(member_ids) == 1 else "strict_signature_v1",
                    now,
                ),
            )
        target.execute(
            """
            INSERT INTO cluster_events(
                building_id,event_type,article_ids_json,reason_json,
                cluster_version,run_id,created_at
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (
                building_id,
                "created_singleton" if len(member_ids) == 1 else "auto_merge",
                json_dumps(member_ids),
                json_dumps(
                    {
                        "country_conflict": country_conflict,
                        "city_conflict": city_conflict,
                        "year_conflict": year_conflict,
                    }
                ),
                CLUSTER_VERSION,
                run_id,
                now,
            ),
        )

    target.execute(
        """
        UPDATE buildings
        SET needs_review=1
        WHERE building_id IN (
          SELECT DISTINCT ba.building_id
          FROM building_articles ba
          JOIN article_match_candidates c
            ON c.article_id_a=ba.article_id OR c.article_id_b=ba.article_id
          WHERE c.status='open'
        )
        """
    )
    return {
        "buildings": len(groups),
        "merged_groups": merged_groups,
        "merged_articles": merged_articles,
        "match_candidates": len(candidates),
        "open_match_candidates": sum(
            1 for candidate in candidates.values() if candidate["status"] == "open"
        ),
    }


def best_tier(tiers: Iterable[str]) -> str:
    values = list(tiers)
    return max(values, key=search_tier_rank) if values else "hidden"


def resolve_facets(target: sqlite3.Connection) -> dict[str, int]:
    rows = target.execute(
        """
        SELECT
            ba.building_id,
            c.axis,
            c.value_normalized AS value,
            MAX(c.confidence) AS confidence,
            COUNT(*) AS claim_count,
            COUNT(DISTINCT c.article_id) AS article_count,
            SUM(
              CASE
                WHEN COALESCE(
                  json_extract(c.details_json,'$.mapping_kind'),
                  'direct'
                )='direct' THEN 1 ELSE 0
              END
            ) AS direct_claim_count,
            SUM(
              CASE
                WHEN json_extract(c.details_json,'$.mapping_kind')='supporting'
                THEN 1 ELSE 0
              END
            ) AS supporting_claim_count,
            COUNT(
              DISTINCT COALESCE(c.source_ref, 'claim:' || c.claim_id)
            ) AS source_count,
            MAX(
              CASE
                WHEN COALESCE(
                  json_extract(c.details_json,'$.mapping_kind'),
                  'direct'
                )='direct' THEN c.confidence
              END
            ) AS max_direct_confidence,
            MAX(COALESCE(json_extract(c.details_json,'$.priority'),0)) AS max_priority,
            GROUP_CONCAT(DISTINCT c.search_tier) AS tiers
        FROM building_articles ba
        JOIN attribute_claims c ON c.article_id=ba.article_id
        WHERE c.scope='building'
          AND c.polarity='positive'
          AND c.axis NOT IN ('country','city','project_year','area_sqm',
                             'country_candidate','city_candidate')
          AND COALESCE(
                json_extract(c.details_json,'$.mapping_kind'),
                'direct'
              ) IN ('direct','supporting')
        GROUP BY ba.building_id,c.axis,c.value_normalized
        ORDER BY ba.building_id,c.axis,c.value_normalized
        """
    ).fetchall()
    for row in rows:
        confidence = float(row["confidence"])
        tier = best_tier((row["tiers"] or "").split(","))
        direct_count = int(row["direct_claim_count"])
        supporting_count = int(row["supporting_claim_count"])
        source_count = int(row["source_count"])
        max_direct_confidence = row["max_direct_confidence"]
        status = (
            "confirmed"
            if (
                (
                    direct_count > 0
                    and max_direct_confidence is not None
                    and float(max_direct_confidence) >= 0.85
                )
                or (
                    direct_count == 0
                    and supporting_count > 0
                    and source_count >= 2
                    and confidence >= 0.75
                )
            )
            else "candidate"
        )
        target.execute(
            """
            INSERT INTO building_facets(
                building_id,axis,value,status,role,confidence,claim_count,
                article_count,direct_claim_count,supporting_claim_count,
                source_count,max_priority,search_tier,resolver_version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                row["building_id"],
                row["axis"],
                row["value"],
                status,
                "facet",
                confidence,
                row["claim_count"],
                row["article_count"],
                direct_count,
                supporting_count,
                source_count,
                row["max_priority"],
                tier,
                RESOLVER_VERSION,
            ),
        )

    target.execute(
        """
        INSERT INTO building_facet_claims(facet_id,claim_id,weight)
        SELECT f.facet_id,c.claim_id,c.confidence
        FROM building_facets f
        JOIN building_articles ba ON ba.building_id=f.building_id
        JOIN attribute_claims c
          ON c.article_id=ba.article_id
         AND c.scope='building'
         AND c.axis=f.axis
         AND c.value_normalized=f.value
         AND c.polarity='positive'
        """
    )

    scalar_columns = {
        "program": ("program", "program_confidence"),
        "typology": ("typology_primary", "typology_confidence"),
        "style": ("style", None),
        "structural_system": ("structural_system", None),
        "roof_type": ("roof_type", None),
        "facade_pattern": ("facade_pattern", None),
        "facade_system": ("facade_system", None),
    }
    scalar_axes = tuple(scalar_columns)
    placeholders = ",".join("?" for _ in scalar_axes)
    target.execute(
        f"""
        UPDATE building_facets
        SET role='secondary'
        WHERE axis IN ({placeholders})
        """,
        scalar_axes,
    )
    grouped: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in target.execute(
        f"""
        SELECT
          facet_id,building_id,axis,value,confidence,claim_count,max_priority,
          direct_claim_count,supporting_claim_count,source_count
        FROM building_facets
        WHERE status='confirmed'
          AND axis IN ({placeholders})
        ORDER BY
          building_id,axis,direct_claim_count DESC,confidence DESC,
          source_count DESC,max_priority DESC,claim_count DESC,value
        """,
        scalar_axes,
    ):
        grouped[(row["building_id"], row["axis"])].append(row)

    conflicts = 0
    selected: dict[str, list[tuple[Any, ...]]] = defaultdict(list)
    primary_facet_ids: list[tuple[int]] = []
    for (building_id, axis), candidates in grouped.items():
        direct = [
            candidate
            for candidate in candidates
            if int(candidate["direct_claim_count"]) > 0
        ]
        if len(direct) > 1:
            conflicts += 1
            continue
        if len(direct) == 1:
            top = direct[0]
        elif len(candidates) == 1:
            top = candidates[0]
        else:
            conflicts += 1
            continue
        primary_facet_ids.append((int(top["facet_id"]),))
        column, confidence_column = scalar_columns[axis]
        if confidence_column:
            selected[axis].append(
                (
                    top["value"],
                    float(top["confidence"]),
                    building_id,
                )
            )
        else:
            selected[axis].append((top["value"], building_id))

    target.executemany(
        "UPDATE building_facets SET role='primary' WHERE facet_id=?",
        primary_facet_ids,
    )
    for axis, values in selected.items():
        column, confidence_column = scalar_columns[axis]
        if confidence_column:
            target.executemany(
                f"""
                UPDATE buildings
                SET {column}=?,{confidence_column}=?
                WHERE building_id=?
                """,
                values,
            )
        else:
            target.executemany(
                f"UPDATE buildings SET {column}=? WHERE building_id=?",
                values,
            )
    return {
        "building_facets": target.execute(
            "SELECT COUNT(*) FROM building_facets"
        ).fetchone()[0],
        "facet_claim_links": target.execute(
            "SELECT COUNT(*) FROM building_facet_claims"
        ).fetchone()[0],
        "scalar_conflicts": conflicts,
    }


def insert_qa(
    target: sqlite3.Connection,
    *,
    entity_type: str,
    entity_key: str,
    check_code: str,
    severity: str,
    run_id: int,
    details: Optional[dict[str, Any]] = None,
) -> None:
    target.execute(
        """
        INSERT OR IGNORE INTO qa_issues(
            entity_type,entity_key,check_code,severity,status,details_json,
            detected_run_id,created_at
        ) VALUES (?,?,?,?, 'open',?,?,?)
        """,
        (
            entity_type,
            entity_key,
            check_code,
            severity,
            json_dumps(details) if details is not None else None,
            run_id,
            utc_now(),
        ),
    )


def populate_qa(
    target: sqlite3.Connection,
    *,
    run_id: int,
    malformed_image_urls: int,
) -> dict[str, int]:
    unmapped = target.execute("SELECT * FROM v_unmapped_tags").fetchall()
    for row in unmapped:
        insert_qa(
            target,
            entity_type="tag",
            entity_key=row["tag_slug"],
            check_code="unmapped_tag",
            severity="warning",
            run_id=run_id,
            details={
                "album_slug": row["album_slug"],
                "used_article_count": row["used_article_count"],
            },
        )

    for row in target.execute(
        """
        SELECT article_id
        FROM source_articles
        WHERE NOT EXISTS(
          SELECT 1 FROM article_image_occurrences aio
          WHERE aio.article_id=source_articles.article_id
        )
        """
    ):
        insert_qa(
            target,
            entity_type="article",
            entity_key=str(row["article_id"]),
            check_code="no_images",
            severity="warning",
            run_id=run_id,
        )

    for row in target.execute(
        "SELECT building_id FROM buildings WHERE location_country IS NULL OR location_city IS NULL"
    ):
        insert_qa(
            target,
            entity_type="building",
            entity_key=row["building_id"],
            check_code="missing_location",
            severity="warning",
            run_id=run_id,
        )

    for row in target.execute(
        "SELECT building_id FROM buildings WHERE program IS NULL"
    ):
        insert_qa(
            target,
            entity_type="building",
            entity_key=row["building_id"],
            check_code="missing_program",
            severity="info",
            run_id=run_id,
        )

    for row in target.execute(
        """
        SELECT b.building_id,f.axis,COUNT(*) AS n
        FROM buildings b
        JOIN building_facets f ON f.building_id=b.building_id
        WHERE f.axis IN ('program','typology','style','structural_system',
                         'roof_type','facade_pattern','facade_system')
          AND f.status='confirmed'
          AND f.role='secondary'
          AND NOT EXISTS(
            SELECT 1 FROM building_facets p
            WHERE p.building_id=f.building_id AND p.axis=f.axis AND p.role='primary'
          )
        GROUP BY b.building_id,f.axis
        """
    ):
        insert_qa(
            target,
            entity_type="building",
            entity_key=row["building_id"],
            check_code=f"{row['axis']}_conflict",
            severity="warning",
            run_id=run_id,
            details={"candidate_count": row["n"]},
        )
        target.execute(
            "UPDATE buildings SET needs_review=1 WHERE building_id=?",
            (row["building_id"],),
        )

    for row in target.execute(
        """
        SELECT article_id,tag_slug
        FROM article_tags
        WHERE tag_slug IN ('dollhouses','pet-houses')
        """
    ):
        insert_qa(
            target,
            entity_type="article",
            entity_key=str(row["article_id"]),
            check_code="possible_non_building",
            severity="warning",
            run_id=run_id,
            details={"source_tag": row["tag_slug"]},
        )

    open_pairs = target.execute(
        "SELECT COUNT(*) FROM article_match_candidates WHERE status='open'"
    ).fetchone()[0]
    if open_pairs:
        insert_qa(
            target,
            entity_type="dataset",
            entity_key="divisare",
            check_code="unresolved_duplicate_candidates",
            severity="warning",
            run_id=run_id,
            details={"pair_count": open_pairs},
        )

    ui_stats = target.execute(
        """
        SELECT COUNT(*) AS articles,SUM(description_ui_markers) AS markers
        FROM source_articles
        WHERE description_ui_markers > 0
        """
    ).fetchone()
    if ui_stats["articles"]:
        insert_qa(
            target,
            entity_type="dataset",
            entity_key="divisare",
            check_code="description_caption_residue_possible",
            severity="warning",
            run_id=run_id,
            details={
                "affected_articles": ui_stats["articles"],
                "removed_ui_markers": ui_stats["markers"],
                "policy": "known UI removed; flattened caption boundaries cannot be recovered",
            },
        )

    if malformed_image_urls:
        insert_qa(
            target,
            entity_type="dataset",
            entity_key="divisare",
            check_code="malformed_image_url",
            severity="warning",
            run_id=run_id,
            details={"occurrence_count": malformed_image_urls},
        )

    unverified_architect_names = target.execute(
        """
        SELECT COUNT(*)
        FROM article_attributions
        WHERE identity_relevance='unverified_architect_display'
        """
    ).fetchone()[0]
    if unverified_architect_names:
        insert_qa(
            target,
            entity_type="dataset",
            entity_key="divisare",
            check_code="unverified_architect_display_names",
            severity="info",
            run_id=run_id,
            details={
                "name_count": unverified_architect_names,
                "policy": "excluded from architect identity and duplicate matching",
            },
        )

    project_reference_architects = target.execute(
        """
        SELECT
          COUNT(*) AS total,
          SUM(record_source='project_reference_unresolved') AS unresolved
        FROM source_architects
        WHERE record_source<>'architect_index'
        """
    ).fetchone()
    if project_reference_architects["total"]:
        insert_qa(
            target,
            entity_type="dataset",
            entity_key="divisare",
            check_code="architect_index_missing_references",
            severity="warning",
            run_id=run_id,
            details={
                "referenced_only_architects": project_reference_architects["total"],
                "unresolved_names": project_reference_architects["unresolved"] or 0,
                "policy": "retain source ID; infer name only from aligned project arrays",
            },
        )

    country_conflicts = target.execute(
        """
        SELECT a.article_id,a.location_country,c.value_normalized,c.source_tag_slug
        FROM source_articles a
        JOIN attribute_claims c ON c.article_id=a.article_id
        WHERE c.axis='country_candidate'
          AND a.location_country IS NOT NULL
          AND lower(a.location_country) <> lower(c.value_normalized)
        """
    ).fetchall()
    for row in country_conflicts:
        insert_qa(
            target,
            entity_type="article",
            entity_key=str(row["article_id"]),
            check_code="location_country_tag_conflict",
            severity="warning",
            run_id=run_id,
            details={
                "structured_country": row["location_country"],
                "tag_country": row["value_normalized"],
                "source_tag": row["source_tag_slug"],
            },
        )

    counts = {
        row["severity"]: row["n"]
        for row in target.execute(
            "SELECT severity,COUNT(*) AS n FROM qa_issues GROUP BY severity"
        )
    }
    counts["total"] = sum(counts.values())
    return counts


def collect_metrics(
    target: sqlite3.Connection,
    *,
    run_id: int,
    extra: dict[str, Any],
) -> dict[str, Any]:
    count_tables = {
        "articles": "source_articles",
        "architects": "source_architects",
        "architects_from_index": (
            "source_architects WHERE record_source='architect_index'"
        ),
        "architects_from_project_references": (
            "source_architects WHERE record_source<>'architect_index'"
        ),
        "article_tags": "article_tags",
        "crosswalk_rows": "tag_crosswalk",
        "claims": "attribute_claims",
        "buildings": "buildings",
        "building_facets": "building_facets",
        "image_assets": "image_assets",
        "image_urls": "image_urls",
        "source_image_occurrences": "source_image_occurrences",
        "image_occurrences": "article_image_occurrences",
        "malformed_image_occurrences": (
            "source_image_occurrences WHERE parse_status='malformed'"
        ),
        "confirmed_facets": "building_facets WHERE status='confirmed'",
        "candidate_facets": "building_facets WHERE status='candidate'",
        "open_duplicate_candidates": "article_match_candidates WHERE status='open'",
        "auto_cluster_pairs": "article_match_candidates WHERE status='auto_clustered'",
        "phash_pending": "image_hashes WHERE status='pending'",
    }
    metrics: dict[str, Any] = dict(extra)
    for metric, expression in count_tables.items():
        metrics[metric] = target.execute(
            f"SELECT COUNT(*) FROM {expression}"
        ).fetchone()[0]
    buildings = max(int(metrics["buildings"]), 1)
    coverage_columns = {
        "country_coverage": "location_country",
        "city_coverage": "location_city",
        "year_coverage": "project_year",
        "program_coverage": "program",
        "typology_coverage": "typology_primary",
        "description_coverage": "description_text_id",
    }
    for metric, column in coverage_columns.items():
        count = target.execute(
            f"SELECT COUNT(*) FROM buildings WHERE {column} IS NOT NULL"
        ).fetchone()[0]
        metrics[metric] = round(count / buildings, 6)
    image_buildings = target.execute(
        "SELECT COUNT(DISTINCT building_id) FROM v_building_images"
    ).fetchone()[0]
    metrics["image_coverage"] = round(image_buildings / buildings, 6)
    metrics["merged_building_groups"] = target.execute(
        "SELECT COUNT(*) FROM buildings WHERE article_count>1"
    ).fetchone()[0]
    metrics["ui_affected_articles"] = target.execute(
        "SELECT COUNT(*) FROM source_articles WHERE description_ui_markers>0"
    ).fetchone()[0]
    metrics["unmapped_used_tags"] = target.execute(
        "SELECT COUNT(*) FROM v_unmapped_tags"
    ).fetchone()[0]
    metrics["tags_without_normalized_semantics"] = target.execute(
        "SELECT COUNT(*) FROM v_tags_without_normalized_semantics"
    ).fetchone()[0]
    metrics["tags_without_building_projection"] = target.execute(
        "SELECT COUNT(*) FROM v_tags_without_building_projection"
    ).fetchone()[0]
    metrics["unverified_architect_display_names"] = target.execute(
        """
        SELECT COUNT(*)
        FROM article_attributions
        WHERE identity_relevance='unverified_architect_display'
        """
    ).fetchone()[0]
    metrics["qa_open"] = target.execute(
        "SELECT COUNT(*) FROM qa_issues WHERE status='open'"
    ).fetchone()[0]
    metrics["qa_by_code"] = {
        row["check_code"]: int(row["n"])
        for row in target.execute(
            """
            SELECT check_code,COUNT(*) AS n
            FROM qa_issues
            WHERE status='open'
            GROUP BY check_code
            ORDER BY n DESC,check_code
            """
        )
    }
    metrics["duplicate_candidates_by_kind"] = {
        f"{row['status']}:{row['candidate_kind']}": int(row["n"])
        for row in target.execute(
            """
            SELECT status,candidate_kind,COUNT(*) AS n
            FROM article_match_candidates
            GROUP BY status,candidate_kind
            ORDER BY status,candidate_kind
            """
        )
    }
    metrics["location_resolution_methods"] = {
        row["location_resolution_method"]: int(row["n"])
        for row in target.execute(
            """
            SELECT location_resolution_method,COUNT(*) AS n
            FROM buildings
            GROUP BY location_resolution_method
            ORDER BY n DESC
            """
        )
    }
    metrics["description_quality_counts"] = {
        row["description_quality"]: int(row["n"])
        for row in target.execute(
            """
            SELECT description_quality,COUNT(*) AS n
            FROM source_articles
            GROUP BY description_quality
            ORDER BY n DESC
            """
        )
    }
    metrics["image_url_generations"] = {
        row["url_generation"]: int(row["n"])
        for row in target.execute(
            """
            SELECT url_generation,COUNT(*) AS n
            FROM image_assets
            GROUP BY url_generation
            ORDER BY n DESC
            """
        )
    }
    metrics["taxonomy_album_coverage"] = [
        dict(row)
        for row in target.execute(
            """
            SELECT
              st.album_slug,
              COUNT(*) AS used_tags,
              SUM(st.used_article_count) AS occurrences,
              SUM(
                CASE WHEN EXISTS(
                  SELECT 1 FROM tag_crosswalk cw
                  WHERE cw.tag_slug=st.tag_slug
                    AND cw.enabled=1
                    AND cw.mapping_kind IN ('direct','supporting')
                    AND cw.target_axis NOT IN ('source_typology','source_topic')
                ) THEN st.used_article_count ELSE 0 END
              ) AS normalized_semantic_occurrences,
              SUM(
                CASE WHEN EXISTS(
                  SELECT 1 FROM tag_crosswalk cw
                  WHERE cw.tag_slug=st.tag_slug
                    AND cw.enabled=1
                    AND cw.target_scope='building'
                    AND cw.mapping_kind IN ('direct','supporting')
                    AND cw.target_axis NOT IN ('source_typology','source_topic')
                ) THEN st.used_article_count ELSE 0 END
              ) AS building_projection_occurrences
            FROM source_tags st
            WHERE st.used_article_count>0
            GROUP BY st.album_slug
            ORDER BY occurrences DESC
            """
        )
    ]
    metrics["top_normalized_semantic_gaps"] = [
        dict(row)
        for row in target.execute(
            """
            SELECT album_slug,tag_slug,label,used_article_count
            FROM v_tags_without_normalized_semantics
            ORDER BY used_article_count DESC,tag_slug
            LIMIT 20
            """
        )
    ]

    for metric, value in metrics.items():
        if isinstance(value, bool):
            numeric = int(value)
        elif isinstance(value, (int, float)):
            numeric = value
        else:
            continue
        target.execute(
            """
            INSERT OR REPLACE INTO build_metrics(run_id,metric,value)
            VALUES (?,?,?)
            """,
            (run_id, metric, numeric),
        )
    return metrics


def validate_output(
    target: sqlite3.Connection,
    *,
    expected_articles: int,
) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    checks["integrity_check"] = target.execute("PRAGMA integrity_check").fetchone()[0]
    checks["foreign_key_errors"] = len(target.execute("PRAGMA foreign_key_check").fetchall())
    checks["article_count"] = target.execute(
        "SELECT COUNT(*) FROM source_articles"
    ).fetchone()[0]
    checks["unassigned_articles"] = target.execute(
        """
        SELECT COUNT(*) FROM source_articles a
        WHERE NOT EXISTS(SELECT 1 FROM building_articles ba WHERE ba.article_id=a.article_id)
        """
    ).fetchone()[0]
    checks["multi_assigned_articles"] = target.execute(
        """
        SELECT COUNT(*) FROM (
          SELECT article_id,COUNT(*) n FROM building_articles
          GROUP BY article_id HAVING n<>1
        )
        """
    ).fetchone()[0]
    checks["orphan_occurrences"] = target.execute(
        """
        SELECT COUNT(*) FROM article_image_occurrences aio
        LEFT JOIN image_assets ia ON ia.asset_key=aio.asset_key
        LEFT JOIN image_urls iu ON iu.url_id=aio.url_id
        WHERE ia.asset_key IS NULL
           OR iu.url_id IS NULL
           OR iu.asset_key<>aio.asset_key
        """
    ).fetchone()[0]
    checks["source_occurrence_count"] = target.execute(
        "SELECT COUNT(*) FROM source_image_occurrences"
    ).fetchone()[0]
    checks["expected_source_occurrence_count"] = target.execute(
        "SELECT COALESCE(SUM(image_count),0) FROM source_articles"
    ).fetchone()[0]
    checks["parsed_occurrence_mismatch"] = target.execute(
        """
        SELECT ABS(
          (SELECT COUNT(*) FROM source_image_occurrences WHERE parse_status='parsed')
          -
          (SELECT COUNT(*) FROM article_image_occurrences)
        )
        """
    ).fetchone()[0]
    checks["primary_membership_errors"] = target.execute(
        """
        SELECT COUNT(*)
        FROM buildings b
        LEFT JOIN building_articles ba
          ON ba.building_id=b.building_id
         AND ba.article_id=b.primary_article_id
        WHERE ba.article_id IS NULL
        """
    ).fetchone()[0]
    checks["building_article_count_errors"] = target.execute(
        """
        SELECT COUNT(*)
        FROM buildings b
        WHERE b.article_count<>(
          SELECT COUNT(*)
          FROM building_articles ba
          WHERE ba.building_id=b.building_id
        )
        """
    ).fetchone()[0]
    checks["description_membership_errors"] = target.execute(
        """
        SELECT COUNT(*)
        FROM buildings b
        JOIN article_text_versions tv ON tv.text_id=b.description_text_id
        WHERE tv.article_id<>b.primary_article_id
        """
    ).fetchone()[0]
    checks["architect_identity_errors"] = target.execute(
        """
        SELECT COUNT(*)
        FROM article_architects aa
        JOIN source_architects sa ON sa.architect_id=aa.architect_id
        WHERE aa.architect_name<>sa.name
        """
    ).fetchone()[0]
    checks["scalar_resolution_errors"] = target.execute(
        """
        SELECT COUNT(*)
        FROM buildings b
        WHERE
          (b.program IS NOT NULL AND NOT EXISTS(
            SELECT 1 FROM building_facets f
            WHERE f.building_id=b.building_id
              AND f.axis='program'
              AND f.value=b.program
              AND f.status='confirmed'
              AND f.role='primary'
          ))
          OR
          (b.typology_primary IS NOT NULL AND NOT EXISTS(
            SELECT 1 FROM building_facets f
            WHERE f.building_id=b.building_id
              AND f.axis='typology'
              AND f.value=b.typology_primary
              AND f.status='confirmed'
              AND f.role='primary'
          ))
        """
    ).fetchone()[0]
    checks["plans_propagated_to_images"] = target.execute(
        """
        SELECT COUNT(*) FROM image_classifications
        WHERE source_kind='source_tag'
        """
    ).fetchone()[0]

    failures = []
    if checks["integrity_check"] != "ok":
        failures.append("integrity_check")
    if checks["foreign_key_errors"]:
        failures.append("foreign_key_errors")
    if checks["article_count"] != expected_articles:
        failures.append("article_count")
    if checks["unassigned_articles"]:
        failures.append("unassigned_articles")
    if checks["multi_assigned_articles"]:
        failures.append("multi_assigned_articles")
    if checks["orphan_occurrences"]:
        failures.append("orphan_occurrences")
    if checks["source_occurrence_count"] != checks["expected_source_occurrence_count"]:
        failures.append("source_occurrence_count")
    if checks["parsed_occurrence_mismatch"]:
        failures.append("parsed_occurrence_mismatch")
    if checks["primary_membership_errors"]:
        failures.append("primary_membership_errors")
    if checks["building_article_count_errors"]:
        failures.append("building_article_count_errors")
    if checks["description_membership_errors"]:
        failures.append("description_membership_errors")
    if checks["architect_identity_errors"]:
        failures.append("architect_identity_errors")
    if checks["scalar_resolution_errors"]:
        failures.append("scalar_resolution_errors")
    if checks["plans_propagated_to_images"]:
        failures.append("plans_propagated_to_images")
    if failures:
        raise RuntimeError(f"output validation failed: {failures}; checks={checks}")
    return checks


def write_report(
    report_path: Path,
    *,
    source_path: Path,
    output_path: Path,
    source_counts: dict[str, Any],
    metrics: dict[str, Any],
    validation: dict[str, Any],
    elapsed_seconds: float,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_temp_path = report_path.with_suffix(report_path.suffix + ".tmp")
    lines = [
        "# Divisare Curated DB v1",
        "",
        f"- Built at: `{utc_now()}`",
        f"- Source: `{source_path}`",
        f"- Output: `{output_path}`",
        f"- Builder: `{BUILDER_VERSION}`",
        f"- Taxonomy: `{TAXONOMY_VERSION}`",
        f"- Elapsed: `{elapsed_seconds:.1f}s`",
        "- External API / LLM / vector calls: `0`",
        "",
        "## Core counts",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    for key in (
        "articles",
        "buildings",
        "merged_building_groups",
        "open_duplicate_candidates",
        "architects",
        "article_tags",
        "crosswalk_rows",
        "claims",
        "building_facets",
        "confirmed_facets",
        "candidate_facets",
        "image_assets",
        "image_urls",
        "source_image_occurrences",
        "image_occurrences",
        "malformed_image_occurrences",
        "phash_pending",
        "unmapped_used_tags",
        "tags_without_normalized_semantics",
        "tags_without_building_projection",
        "architects_from_project_references",
        "unverified_architect_display_names",
        "qa_open",
    ):
        lines.append(f"| `{key}` | {metrics.get(key, 0):,} |")
    lines.extend(
        [
            "",
            "## Building coverage",
            "",
            "| Field | Coverage |",
            "|---|---:|",
        ]
    )
    for key in (
        "country_coverage",
        "city_coverage",
        "year_coverage",
        "program_coverage",
        "typology_coverage",
        "description_coverage",
        "image_coverage",
    ):
        lines.append(f"| `{key}` | {metrics.get(key, 0) * 100:.2f}% |")
    lines.extend(
        [
            "",
            "## Resolution policy",
            "",
            "- Scalar fields use confirmed facets only.",
            "- A direct claim needs confidence >= 0.85; supporting-only evidence",
            "  needs at least two distinct source references.",
            "- Conflicting direct scalar claims abstain and create QA review items.",
            "- Automatic article merging requires exact normalized name, architect ID,",
            "  country, city, and the same non-null project year.",
            "- Generic names and missing-year pairs remain review candidates.",
            "- Raw credit payloads are intentionally excluded from this curated DB.",
            "  Unverified architect display names are retained only as provenance.",
            "",
            "## Open QA",
            "",
            "| Check | Count |",
            "|---|---:|",
        ]
    )
    for check_code, count in metrics.get("qa_by_code", {}).items():
        lines.append(f"| `{check_code}` | {count:,} |")

    lines.extend(
        [
            "",
            "## Tag coverage by album",
            "",
            "| Album | Used tags | Occurrences | Normalized semantics | Building projection |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for row in metrics.get("taxonomy_album_coverage", []):
        occurrences = int(row["occurrences"] or 0)
        semantic = int(row["normalized_semantic_occurrences"] or 0)
        building = int(row["building_projection_occurrences"] or 0)
        semantic_pct = (semantic / occurrences * 100) if occurrences else 0
        building_pct = (building / occurrences * 100) if occurrences else 0
        lines.append(
            f"| `{row['album_slug']}` | {int(row['used_tags']):,} | "
            f"{occurrences:,} | {semantic_pct:.1f}% | {building_pct:.1f}% |"
        )

    lines.extend(
        [
            "",
            "## Highest-volume normalized semantic gaps",
            "",
            "| Album | Tag | Label | Article count |",
            "|---|---|---|---:|",
        ]
    )
    for row in metrics.get("top_normalized_semantic_gaps", []):
        label = str(row["label"]).replace("|", "\\|")
        lines.append(
            f"| `{row['album_slug']}` | `{row['tag_slug']}` | {label} | "
            f"{int(row['used_article_count']):,} |"
        )

    lines.extend(
        [
            "",
            "## Image URL identities",
            "",
            "| URL generation | Assets |",
            "|---|---:|",
        ]
    )
    for generation, count in metrics.get("image_url_generations", {}).items():
        lines.append(f"| `{generation}` | {count:,} |")
    lines.extend(
        [
            "",
            "## Validation",
            "",
            "```json",
            json.dumps(validation, ensure_ascii=False, indent=2, sort_keys=True),
            "```",
            "",
            "## Deferred work",
            "",
            "- `image_hashes` contains asset-keyed pHash work in `pending` state.",
            "- `image_classifications` is empty; Plans/Topics tags remain article-level priors",
            "  and are not propagated to every image.",
            "- Re-clustering with completed pHash evidence is a later D2 stage; building IDs",
            "  in this DB are provisional within this source snapshot.",
            "- Open fuzzy duplicate pairs are review candidates and are not auto-merged.",
            "- The historical flattened description lost DOM caption boundaries. Known UI text",
            "  is removed, but affected text remains explicitly quality-flagged.",
            "- No vector database or embedding is part of this artifact.",
            "",
            "## Source snapshot",
            "",
            "```json",
            json.dumps(source_counts, ensure_ascii=False, indent=2, sort_keys=True),
            "```",
        ]
    )
    report_temp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(report_temp_path, report_path)


def promote_temp_output(
    *,
    temp_path: Path,
    output_path: Path,
    output_existed_at_start: bool,
    initial_output_sha256: Optional[str],
) -> None:
    """Publish a completed immutable DB without clobbering another file."""

    if output_existed_at_start or initial_output_sha256 is not None:
        raise RuntimeError(
            "curated DB outputs are immutable; publish the rebuild to a new "
            f"versioned path instead of replacing {output_path}"
        )
    try:
        os.link(temp_path, output_path)
    except FileExistsError as exc:
        raise RuntimeError(
            f"output appeared while build was running; refusing to overwrite: "
            f"{output_path}"
        ) from exc
    temp_path.unlink()


def _build_locked(
    *,
    source_path: Path,
    output_path: Path,
    report_path: Path,
    limit_rows: Optional[int],
    replace: bool,
    skip_source_hash: bool,
) -> dict[str, Any]:
    started = time.monotonic()
    source_path = source_path.resolve()
    output_path = output_path.resolve()
    report_path = report_path.resolve()
    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    report_temp_path = report_path.with_suffix(report_path.suffix + ".tmp")
    validate_build_paths(
        source_path,
        output_path,
        report_path,
        temp_path,
        report_temp_path,
    )
    if limit_rows is not None and limit_rows <= 0:
        raise ValueError("--limit must be a positive integer")
    if not source_path.exists():
        raise FileNotFoundError(source_path)
    output_existed_at_start = output_path.exists()
    if output_existed_at_start:
        if replace:
            protected_state = nonregenerable_state(output_path)
            detail = (
                f" Protected state detected: {protected_state}."
                if protected_state
                else ""
            )
            raise RuntimeError(
                "in-place replacement is disabled because curated DB outputs "
                "are immutable; choose a new versioned output path."
                + detail
            )
        raise FileExistsError(
            f"{output_path} exists; choose a new versioned output path"
        )
    initial_output_sha256: Optional[str] = None
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if temp_path.exists():
        temp_path.unlink()

    source = open_source(source_path)
    source.execute("BEGIN")
    source_counts = validate_source(source)
    expected_articles = (
        min(limit_rows, source_counts["divisare_projects"])
        if limit_rows is not None
        else source_counts["divisare_projects"]
    )
    target = sqlite3.connect(temp_path)
    target.row_factory = sqlite3.Row
    target.execute("PRAGMA foreign_keys=ON")
    target.execute("PRAGMA journal_mode=DELETE")
    target.execute("PRAGMA synchronous=NORMAL")
    target.execute("PRAGMA temp_store=MEMORY")
    create_schema(target)

    started_at = utc_now()
    run_id = target.execute(
        """
        INSERT INTO build_runs(
            started_at,status,builder_version,schema_version,taxonomy_version,
            text_processor_version,asset_key_version,cluster_version,
            resolver_version,source_db_path,output_db_path,limit_rows
        ) VALUES (?, 'running',?,?,?,?,?,?,?,?,?,?)
        """,
        (
            started_at,
            BUILDER_VERSION,
            SCHEMA_VERSION,
            TAXONOMY_VERSION,
            TEXT_PROCESSOR_VERSION,
            ASSET_KEY_VERSION,
            CLUSTER_VERSION,
            RESOLVER_VERSION,
            str(source_path),
            str(output_path),
            limit_rows,
        ),
    ).lastrowid
    source_stat = source_path.stat()
    snapshot_id = target.execute(
        """
        INSERT INTO source_snapshots(
            run_id,source_db_path,byte_size,modified_at,sha256,
            sqlite_quick_check,source_counts_json,captured_at
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            str(source_path),
            source_stat.st_size,
            datetime.fromtimestamp(source_stat.st_mtime, timezone.utc).isoformat(),
            None if skip_source_hash else file_sha256(source_path),
            source_counts["quick_check"],
            json_dumps(source_counts),
            utc_now(),
        ),
    ).lastrowid
    target.commit()

    try:
        target.execute("BEGIN")
        import_source_taxonomy(source, target)
        import_architects(source, target)
        import_metrics = import_articles(
            source,
            target,
            snapshot_id=snapshot_id,
            run_id=run_id,
            limit_rows=limit_rows,
        )
        crosswalk_rows = populate_crosswalk(target)
        claim_count = populate_claims(target, run_id=run_id)
        cluster_metrics = create_building_clusters(target, run_id=run_id)
        facet_metrics = resolve_facets(target)
        target.executescript(VIEWS_SQL)
        qa_metrics = populate_qa(
            target,
            run_id=run_id,
            malformed_image_urls=import_metrics.get("malformed_image_urls", 0),
        )
        extra_metrics = {
            **import_metrics,
            **cluster_metrics,
            **facet_metrics,
            "crosswalk_rows_generated": crosswalk_rows,
            "claims_generated": claim_count,
            "qa_total": qa_metrics.get("total", 0),
        }
        metrics = collect_metrics(target, run_id=run_id, extra=extra_metrics)
        validation = validate_output(target, expected_articles=expected_articles)
        target.execute(
            """
            UPDATE build_runs
            SET completed_at=?,status='complete',metrics_json=?
            WHERE run_id=?
            """,
            (utc_now(), json_dumps(metrics), run_id),
        )
        target.commit()
        target.execute("ANALYZE")
        target.execute("PRAGMA optimize")
        target.commit()
    except Exception as exc:
        target.rollback()
        try:
            target.execute(
                """
                UPDATE build_runs
                SET completed_at=?,status='failed',error=?
                WHERE run_id=?
                """,
                (utc_now(), str(exc)[:2000], run_id),
            )
            target.commit()
        finally:
            target.close()
            source.close()
        raise

    target.close()
    source.close()
    promote_temp_output(
        temp_path=temp_path,
        output_path=output_path,
        output_existed_at_start=output_existed_at_start,
        initial_output_sha256=initial_output_sha256,
    )
    elapsed = time.monotonic() - started
    write_report(
        report_path,
        source_path=source_path,
        output_path=output_path,
        source_counts=source_counts,
        metrics=metrics,
        validation=validation,
        elapsed_seconds=elapsed,
    )
    return {
        "output_db": str(output_path),
        "report": str(report_path),
        "elapsed_seconds": round(elapsed, 2),
        "metrics": metrics,
        "validation": validation,
    }


def build(
    *,
    source_path: Path,
    output_path: Path,
    report_path: Path,
    limit_rows: Optional[int],
    replace: bool,
    skip_source_hash: bool,
) -> dict[str, Any]:
    source_path = source_path.resolve()
    output_path = output_path.resolve()
    report_path = report_path.resolve()
    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    report_temp_path = report_path.with_suffix(report_path.suffix + ".tmp")
    lock_path = output_path.with_suffix(output_path.suffix + ".build.lock")
    validate_build_paths(
        source_path,
        output_path,
        report_path,
        temp_path,
        report_temp_path,
        lock_path,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_build_lock(lock_path, output_path):
        return _build_locked(
            source_path=source_path,
            output_path=output_path,
            report_path=report_path,
            limit_rows=limit_rows,
            replace=replace,
            skip_source_hash=skip_source_hash,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument(
        "--output-db",
        type=Path,
        default=ROOT / "data" / "curated" / "divisare_curated_v1.db",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=ROOT / "data" / "reports" / "divisare_curated_v1.md",
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--replace",
        action="store_true",
        help=(
            "Deprecated safety check only. Existing curated DBs are immutable; "
            "choose a new versioned output path."
        ),
    )
    parser.add_argument(
        "--skip-source-hash",
        action="store_true",
        help="Useful only for N=10/N=100 smoke builds.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = build(
            source_path=args.source_db,
            output_path=args.output_db,
            report_path=args.report,
            limit_rows=args.limit,
            replace=args.replace,
            skip_source_hash=args.skip_source_hash,
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
