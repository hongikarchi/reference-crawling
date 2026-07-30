"""Build an immutable, source-specific Architizer curated SQLite database.

The raw crawler SQLite is the read-only source of truth.  This builder makes
no network, LLM, image download, embedding, Neon, or R2 calls.  It preserves
source occurrences before projecting reviewed category mappings into typed
claims, and it keeps provisional within-source building identity separate
from Architizer project identity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from canonical.architizer_curated import (
    ASSET_KEY_VERSION,
    BROAD_CATEGORIES,
    CATEGORY_PARENT,
    CLUSTER_VERSION,
    POLICY_VERSION,
    SCHEMA_VERSION,
    TAXONOMY_VERSION,
    clean_scalar,
    image_identity,
    is_generic_project_name,
    mappings_for_category,
    name_similarity,
    normalize_identity_text,
    parse_json_dict,
    parse_json_list,
    parse_size_bucket,
    text_has_mojibake,
    valid_or_candidate_year,
)


EXPECTED_SOURCE_SIZE = 90_918_912
EXPECTED_SOURCE_SHA256 = (
    "35FAA8AD2B4681033E1F7F74148499B29009777977204C7A65923D8FABB5C985"
)
BUILDER_VERSION = "architizer-curated-builder-v1.6"
SELECTION_VERSION = "architizer-deterministic-subset-v1.2"
RESOLVER_VERSION = "architizer-facet-resolver-v1.0"
CURRENT_YEAR = 2026
MIN_SQLITE_VERSION = (3, 37, 0)

DEFAULT_SOURCE = Path("data/crawl/architizer.db")
DEFAULT_OUTPUT = Path("data/curated/architizer_curated_v1.db")
DEFAULT_REPORT = Path("data/reports/architizer_curated_v1.md")

REQUIRED_SOURCE_COLUMNS: dict[str, set[str]] = {
    "architizer_projects": {
        "id",
        "global_id",
        "slug",
        "name",
        "firm_slug",
        "firm_name",
        "description",
        "description_short",
        "completion_year",
        "building_size_slug",
        "building_size_display",
        "constr_status",
        "budget",
        "location_full",
        "location_country",
        "location_city",
        "categories",
        "cover_image_url",
        "gallery_image_urls",
        "image_global_ids",
        "published_time",
        "modified_time",
        "fetched_at",
    },
    "architizer_firms": {
        "slug",
        "name",
        "office_locations",
        "description",
        "awards_summary",
        "project_count_seen",
        "social_links",
        "fetched_at",
    },
    "architizer_awards": {
        "id",
        "award_year",
        "award_track",
        "award_category",
        "award_tier",
        "project_slug",
        "firm_slug",
        "source_url",
        "fetched_at",
    },
    "pending_projects": {
        "url",
        "source_url",
        "lastmod",
        "status",
        "discovered_at",
        "fetched_at",
        "error",
    },
    "pending_firms": {
        "url",
        "source_url",
        "lastmod",
        "status",
        "discovered_at",
        "fetched_at",
        "error",
    },
}


class BuildError(RuntimeError):
    """Raised before publication when a source or curated invariant fails."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stable_id(prefix: str, *parts: Any, length: int = 20) -> str:
    payload = "\x1f".join("" if part is None else str(part) for part in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]
    return f"{prefix}{digest}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def _source_label(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return resolved.name


def _source_sidecar_sizes(path: Path) -> dict[str, int]:
    return {
        "wal": Path(str(path) + "-wal").stat().st_size
        if Path(str(path) + "-wal").exists()
        else 0,
        "shm": Path(str(path) + "-shm").stat().st_size
        if Path(str(path) + "-shm").exists()
        else 0,
        "journal": Path(str(path) + "-journal").stat().st_size
        if Path(str(path) + "-journal").exists()
        else 0,
    }


def _assert_source_sidecars_clean(path: Path) -> dict[str, int]:
    sizes = _source_sidecar_sizes(path)
    if sizes["wal"] or sizes["journal"]:
        raise BuildError(
            "source has an uncheckpointed SQLite sidecar; refusing immutable read: "
            f"wal={sizes['wal']} bytes, journal={sizes['journal']} bytes"
        )
    return sizes


def validate_build_paths(source: Path, output: Path, report: Path) -> None:
    source = source.resolve()
    output = output.resolve()
    report = report.resolve()
    paths = {
        "source": source,
        "source_wal": Path(str(source) + "-wal"),
        "source_shm": Path(str(source) + "-shm"),
        "source_journal": Path(str(source) + "-journal"),
        "output": output,
        "report": report,
        "output_wal": Path(str(output) + "-wal"),
        "output_shm": Path(str(output) + "-shm"),
        "output_journal": Path(str(output) + "-journal"),
        "lock": Path(str(output) + ".build.lock"),
    }
    keys: dict[str, str] = {}
    for label, path in paths.items():
        key = _path_key(path)
        if key in keys:
            raise BuildError(f"build path collision: {label} == {keys[key]} ({path})")
        keys[key] = label
    if output.exists():
        raise BuildError(f"immutable output already exists: {output}")
    if report.exists():
        raise BuildError(f"immutable report already exists: {report}")
    for label in ("output_wal", "output_shm", "output_journal"):
        if paths[label].exists():
            raise BuildError(
                f"stale output SQLite sidecar already exists: {paths[label]}"
            )
    if source == output or source == report:
        raise BuildError("source path cannot be an output path")


@contextmanager
def build_lock(output: Path) -> Iterator[Path]:
    lock = Path(str(output) + ".build.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise BuildError(f"build lock already exists: {lock}") from exc
    try:
        payload = _json(
            {
                "builder": BUILDER_VERSION,
                "output": str(output.resolve()),
                "pid": os.getpid(),
            }
        )
        os.write(fd, payload.encode("utf-8"))
        os.close(fd)
        yield lock
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


def open_source(path: Path) -> sqlite3.Connection:
    """Open the local crawler copy read-only and ignore empty WAL sidecars."""

    _assert_source_sidecars_clean(path)
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in connection.execute(f'PRAGMA table_info("{table}")')}


def validate_source(
    connection: sqlite3.Connection,
    source_path: Path,
    *,
    expected_sha256: str = EXPECTED_SOURCE_SHA256,
    expected_size: int = EXPECTED_SOURCE_SIZE,
) -> dict[str, Any]:
    if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
        required = ".".join(str(part) for part in MIN_SQLITE_VERSION)
        raise BuildError(
            f"SQLite {required}+ is required for STRICT curated tables; "
            f"found {sqlite3.sqlite_version}"
        )
    sidecar_sizes = _assert_source_sidecars_clean(source_path)
    actual_size = source_path.stat().st_size
    actual_sha = sha256_file(source_path)
    if actual_size != expected_size:
        raise BuildError(
            f"source size mismatch: expected {expected_size}, found {actual_size}"
        )
    if actual_sha != expected_sha256.upper():
        raise BuildError(
            f"source SHA-256 mismatch: expected {expected_sha256.upper()}, found {actual_sha}"
        )
    query_only = connection.execute("PRAGMA query_only").fetchone()[0]
    if query_only != 1:
        raise BuildError("source connection is not query_only")
    quick = [row[0] for row in connection.execute("PRAGMA quick_check")]
    integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
    foreign_keys = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
    if quick != ["ok"]:
        raise BuildError(f"source quick_check failed: {quick[:10]}")
    if integrity != ["ok"]:
        raise BuildError(f"source integrity_check failed: {integrity[:10]}")
    if foreign_keys:
        raise BuildError(f"source foreign_key_check failed: {foreign_keys[:10]}")
    objects = {
        row["name"]: row["type"]
        for row in connection.execute(
            "SELECT name,type FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
    }
    missing_tables = sorted(set(REQUIRED_SOURCE_COLUMNS) - set(objects))
    if missing_tables:
        raise BuildError(f"source tables missing: {missing_tables}")
    for table, required in REQUIRED_SOURCE_COLUMNS.items():
        missing = sorted(required - _table_columns(connection, table))
        if missing:
            raise BuildError(f"source columns missing from {table}: {missing}")
    table_counts = {
        table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        for table in REQUIRED_SOURCE_COLUMNS
    }
    queue_counts: dict[str, dict[str, int]] = {}
    for table in ("pending_projects", "pending_firms"):
        queue_counts[table] = {
            str(row["status"]): int(row["n"])
            for row in connection.execute(
                f'SELECT status,COUNT(*) AS n FROM "{table}" GROUP BY status ORDER BY status'
            )
        }
    return {
        "path": _source_label(source_path),
        "sidecar_sizes": sidecar_sizes,
        "size_bytes": actual_size,
        "sha256": actual_sha,
        "quick_check": quick[0],
        "integrity_check": integrity[0],
        "foreign_key_violations": len(foreign_keys),
        "query_only": query_only,
        "objects": objects,
        "table_counts": table_counts,
        "queue_counts": queue_counts,
    }


def _decode_json_list(value: Any) -> tuple[list[Any], Optional[str]]:
    if value is None or value == "":
        return [], None
    if isinstance(value, list):
        return value, None
    if isinstance(value, tuple):
        return list(value), "expected JSON list text, found tuple"
    text = str(value)
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        return [text], f"{type(exc).__name__}: {exc}"
    if not isinstance(parsed, list):
        return [parsed], f"expected list, found {type(parsed).__name__}"
    return parsed, None


def _decode_json_dict(value: Any) -> tuple[dict[str, Any], Optional[str]]:
    if value is None or value == "":
        return {}, None
    try:
        parsed = json.loads(str(value))
    except (json.JSONDecodeError, TypeError) as exc:
        return {}, f"{type(exc).__name__}: {exc}"
    if not isinstance(parsed, dict):
        return {}, f"expected object, found {type(parsed).__name__}"
    return parsed, None


def _selection_key(row: dict[str, Any]) -> str:
    return hashlib.sha256(
        f"{SELECTION_VERSION}|{row['id']}|{row['slug']}".encode("utf-8")
    ).hexdigest()


def _edge_anchor_ids(
    source: sqlite3.Connection,
    all_rows: Sequence[dict[str, Any]],
) -> list[int]:
    """Choose source-derived edge anchors without hard-coding project ids."""

    anchors: list[int] = []

    def add_first(predicate: Any) -> None:
        match = next((row for row in all_rows if predicate(row)), None)
        if match is not None:
            anchors.append(int(match["id"]))

    add_first(lambda row: row["global_id"] != f"projects.project.{row['id']}")
    add_first(
        lambda row: row["completion_year"] is not None
        and (
            int(row["completion_year"]) < 1800
            or int(row["completion_year"]) > CURRENT_YEAR + 10
        )
    )
    add_first(
        lambda row: bool(row["cover_image_url"])
        and "facebook-default-thumb" in str(row["cover_image_url"])
    )
    add_first(lambda row: len(_decode_json_list(row["categories"])[0]) >= 40)
    add_first(lambda row: not clean_scalar(row["description"]))
    add_first(lambda row: row["completion_year"] is None)
    add_first(lambda row: not clean_scalar(row["location_country"]))
    add_first(lambda row: not clean_scalar(row["building_size_slug"]))

    crawled_firms = {
        str(row[0]) for row in source.execute("SELECT slug FROM architizer_firms")
    }
    add_first(lambda row: str(row["firm_slug"]) in crawled_firms)
    add_first(lambda row: str(row["firm_slug"]) not in crawled_firms)

    strict_groups: dict[
        tuple[str, str, str, str, int], list[dict[str, Any]]
    ] = defaultdict(list)
    same_identity_groups: dict[
        tuple[str, str, str, str], list[dict[str, Any]]
    ] = defaultdict(list)
    for row in all_rows:
        name_key = _match_name(row["name"])
        country, _, _ = _location_part_policy(
            row["location_full"], row["location_country"], part="country"
        )
        city, _, _ = _location_part_policy(
            row["location_full"], row["location_city"], part="city"
        )
        firm = clean_scalar(row["firm_slug"])
        year, _ = valid_or_candidate_year(
            row["completion_year"],
            row["constr_status"],
            current_year=CURRENT_YEAR,
        )
        if (
            name_key
            and firm
            and country
            and city
            and not is_generic_project_name(row["name"])
            and not _phase_marker(str(row["name"]), str(row["slug"]))
        ):
            same_identity_groups[
                (
                    name_key,
                    firm,
                    normalize_identity_text(country),
                    normalize_identity_text(city),
                )
            ].append(row)
            if year is not None:
                strict_groups[
                    (
                        name_key,
                        firm,
                        normalize_identity_text(country),
                        normalize_identity_text(city),
                        int(year),
                    )
                ].append(row)
    strict = next(
        (
            sorted(group, key=lambda row: int(row["id"]))
            for _, group in sorted(strict_groups.items())
            if len(group) >= 2
        ),
        [],
    )
    anchors.extend(int(row["id"]) for row in strict[:2])
    conflict = next(
        (
            sorted(group, key=lambda row: int(row["id"]))
            for _, group in sorted(same_identity_groups.items())
            if len(
                {
                    valid_or_candidate_year(
                        row["completion_year"],
                        row["constr_status"],
                        current_year=CURRENT_YEAR,
                    )[0]
                    for row in group
                }
                - {None}
            )
            >= 2
        ),
        [],
    )
    anchors.extend(int(row["id"]) for row in conflict[:2])
    return list(dict.fromkeys(anchors))


def select_projects(
    source: sqlite3.Connection, limit: Optional[int]
) -> tuple[list[dict[str, Any]], int]:
    all_rows = [
        dict(row)
        for row in source.execute("SELECT * FROM architizer_projects ORDER BY id")
    ]
    source_total = len(all_rows)
    if limit is None:
        selected = all_rows
    else:
        if limit <= 0:
            raise BuildError("--limit must be a positive integer")
        ranked = sorted(all_rows, key=lambda row: (_selection_key(row), row["id"]))
        selected = ranked[:limit]
        if limit >= 100:
            protected = {int(row["id"]) for row in ranked[: min(10, limit)]}
            required = set(_edge_anchor_ids(source, all_rows))
            selected_ids = {int(row["id"]) for row in selected}
            by_id = {int(row["id"]): row for row in all_rows}
            for required_id in sorted(required):
                if required_id in selected_ids:
                    continue
                removable = next(
                    (
                        row
                        for row in reversed(selected)
                        if int(row["id"]) not in protected
                        and int(row["id"]) not in required
                    ),
                    None,
                )
                if removable is None:
                    raise BuildError("cannot reserve deterministic edge anchors")
                selected.remove(removable)
                selected_ids.remove(int(removable["id"]))
                selected.append(by_id[required_id])
                selected_ids.add(required_id)
        selected.sort(key=lambda row: row["id"])
    return selected, source_total


DDL = r"""
PRAGMA foreign_keys=ON;
PRAGMA journal_mode=DELETE;
PRAGMA synchronous=FULL;
PRAGMA temp_store=MEMORY;
PRAGMA trusted_schema=OFF;

CREATE TABLE build_runs (
    build_id TEXT PRIMARY KEY,
    builder_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    taxonomy_version TEXT NOT NULL,
    asset_key_version TEXT NOT NULL,
    cluster_version TEXT NOT NULL,
    resolver_version TEXT NOT NULL,
    selection_version TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    source_size_bytes INTEGER NOT NULL,
    selected_project_limit INTEGER,
    selected_project_count INTEGER NOT NULL,
    deterministic_timestamp TEXT NOT NULL,
    external_calls TEXT NOT NULL CHECK (external_calls = 'none'),
    validation_json TEXT NOT NULL CHECK (json_valid(validation_json))
) STRICT;

CREATE TABLE source_snapshots (
    build_id TEXT PRIMARY KEY REFERENCES build_runs(build_id),
    source_path TEXT NOT NULL,
    source_sha256_before TEXT NOT NULL,
    source_sha256_after TEXT NOT NULL,
    source_size_bytes INTEGER NOT NULL,
    source_wal_size_bytes INTEGER NOT NULL CHECK (source_wal_size_bytes = 0),
    source_journal_size_bytes INTEGER NOT NULL CHECK (source_journal_size_bytes = 0),
    quick_check TEXT NOT NULL,
    integrity_check TEXT NOT NULL,
    foreign_key_violations INTEGER NOT NULL,
    query_only INTEGER NOT NULL CHECK (query_only = 1),
    source_table_counts_json TEXT NOT NULL CHECK (json_valid(source_table_counts_json)),
    source_queue_counts_json TEXT NOT NULL CHECK (json_valid(source_queue_counts_json))
) STRICT;

CREATE TABLE source_queue_summary (
    queue_name TEXT NOT NULL,
    status TEXT NOT NULL,
    row_count INTEGER NOT NULL CHECK (row_count >= 0),
    PRIMARY KEY (queue_name,status)
) STRICT;

CREATE TABLE source_firms (
    source_firm_slug TEXT PRIMARY KEY,
    source_name TEXT,
    record_origin TEXT NOT NULL
        CHECK (record_origin IN ('crawled','project_stub','award_stub')),
    office_locations_raw_json TEXT,
    description TEXT,
    awards_summary TEXT,
    project_count_seen INTEGER,
    social_links_raw_json TEXT,
    fetched_at TEXT,
    source_url TEXT NOT NULL
) STRICT;

CREATE TABLE firm_office_occurrences (
    source_firm_slug TEXT NOT NULL REFERENCES source_firms(source_firm_slug),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    raw_value_json TEXT NOT NULL CHECK (json_valid(raw_value_json)),
    display_value TEXT,
    parse_status TEXT NOT NULL CHECK (parse_status IN ('parsed','unresolved')),
    PRIMARY KEY (source_firm_slug,ordinal)
) WITHOUT ROWID, STRICT;

CREATE TABLE firm_social_links (
    source_firm_slug TEXT NOT NULL REFERENCES source_firms(source_firm_slug),
    platform TEXT NOT NULL,
    raw_url TEXT NOT NULL,
    source_field TEXT NOT NULL,
    PRIMARY KEY (source_firm_slug,platform,raw_url)
) WITHOUT ROWID, STRICT;

CREATE TABLE source_projects (
    source_project_id INTEGER PRIMARY KEY,
    global_id TEXT NOT NULL UNIQUE,
    slug TEXT NOT NULL UNIQUE,
    source_url TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    source_firm_slug TEXT NOT NULL REFERENCES source_firms(source_firm_slug),
    source_firm_name TEXT,
    description TEXT,
    description_short TEXT,
    completion_year_raw INTEGER,
    year_claim_status TEXT NOT NULL
        CHECK (year_claim_status IN ('confirmed','candidate','review','missing')),
    building_size_slug TEXT,
    building_size_display TEXT,
    area_min_sqft INTEGER,
    area_max_sqft INTEGER,
    area_open_ended INTEGER NOT NULL DEFAULT 0 CHECK (area_open_ended IN (0,1)),
    constr_status_raw TEXT,
    budget_raw REAL,
    location_full TEXT,
    location_country_raw TEXT,
    location_city_raw TEXT,
    categories_source_text TEXT,
    gallery_image_urls_source_text TEXT,
    image_global_ids_source_text TEXT,
    categories_raw_json TEXT NOT NULL CHECK (json_valid(categories_raw_json)),
    gallery_image_urls_raw_json TEXT NOT NULL CHECK (json_valid(gallery_image_urls_raw_json)),
    image_global_ids_raw_json TEXT NOT NULL CHECK (json_valid(image_global_ids_raw_json)),
    published_time TEXT,
    modified_time TEXT,
    fetched_at TEXT,
    acceptance_status TEXT NOT NULL CHECK (acceptance_status IN ('accepted','excluded')),
    exclusion_reason TEXT,
    category_occurrence_count INTEGER NOT NULL CHECK (category_occurrence_count >= 0),
    gallery_occurrence_count INTEGER NOT NULL CHECK (gallery_occurrence_count >= 0),
    image_global_id_occurrence_count INTEGER NOT NULL CHECK (image_global_id_occurrence_count >= 0)
) STRICT;

CREATE TABLE project_firms (
    source_project_id INTEGER NOT NULL REFERENCES source_projects(source_project_id),
    source_firm_slug TEXT NOT NULL REFERENCES source_firms(source_firm_slug),
    relationship_kind TEXT NOT NULL CHECK (relationship_kind = 'source_primary_author'),
    raw_firm_name TEXT,
    evidence_field TEXT NOT NULL CHECK (evidence_field = 'article:author'),
    status TEXT NOT NULL CHECK (status = 'source_asserted'),
    PRIMARY KEY (source_project_id,source_firm_slug)
) WITHOUT ROWID, STRICT;

CREATE TABLE project_text_versions (
    text_version_id TEXT PRIMARY KEY,
    source_project_id INTEGER NOT NULL REFERENCES source_projects(source_project_id),
    source_field TEXT NOT NULL CHECK (source_field IN ('description','description_short')),
    raw_text TEXT,
    text_sha256 TEXT,
    quality_status TEXT NOT NULL
        CHECK (quality_status IN ('present','missing','og_snippet','mojibake_possible')),
    processor_version TEXT NOT NULL,
    UNIQUE (source_project_id,source_field)
) STRICT;

CREATE TABLE source_categories (
    category_id TEXT PRIMARY KEY,
    raw_value TEXT NOT NULL UNIQUE,
    normalized_key TEXT NOT NULL,
    source_occurrence_count INTEGER NOT NULL CHECK (source_occurrence_count >= 0),
    project_count INTEGER NOT NULL CHECK (project_count >= 0),
    mapping_status TEXT NOT NULL CHECK (mapping_status IN ('mapped','unmapped','review')),
    taxonomy_version TEXT NOT NULL
) STRICT;

CREATE TABLE project_category_occurrences (
    occurrence_id TEXT PRIMARY KEY,
    source_project_id INTEGER NOT NULL REFERENCES source_projects(source_project_id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    category_id TEXT NOT NULL REFERENCES source_categories(category_id),
    raw_value TEXT NOT NULL,
    source_field TEXT NOT NULL CHECK (source_field = 'article:tag'),
    parse_status TEXT NOT NULL
        CHECK (parse_status IN ('parsed','non_text','malformed_container')),
    UNIQUE (source_project_id,ordinal)
) STRICT;

CREATE TABLE category_mappings (
    mapping_id TEXT PRIMARY KEY,
    category_id TEXT NOT NULL REFERENCES source_categories(category_id),
    axis TEXT,
    normalized_value TEXT,
    target_scope TEXT NOT NULL CHECK (target_scope IN ('project','building')),
    mapping_kind TEXT NOT NULL
        CHECK (mapping_kind IN ('direct','supporting','work_type','unmapped','review')),
    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    evidence TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('confirmed','candidate','unmapped','review')),
    taxonomy_version TEXT NOT NULL,
    CHECK (
        (status = 'unmapped' AND axis IS NULL AND normalized_value IS NULL)
        OR (status != 'unmapped' AND axis IS NOT NULL AND normalized_value IS NOT NULL)
    ),
    UNIQUE (category_id,axis,normalized_value,rule_id)
) STRICT;

CREATE TABLE attribute_claims (
    claim_id TEXT PRIMARY KEY,
    source_project_id INTEGER NOT NULL REFERENCES source_projects(source_project_id),
    category_occurrence_id TEXT REFERENCES project_category_occurrences(occurrence_id),
    axis TEXT NOT NULL,
    raw_value TEXT,
    normalized_value TEXT,
    target_scope TEXT NOT NULL CHECK (target_scope IN ('project','building')),
    claim_kind TEXT NOT NULL
        CHECK (claim_kind IN ('source_structured','category_direct','category_supporting',
                              'work_type','unmapped','derived_range')),
    evidence_type TEXT NOT NULL,
    evidence_ref TEXT NOT NULL,
    evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json)),
    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    status TEXT NOT NULL
        CHECK (status IN ('confirmed','candidate','review','unmapped','abstained')),
    rule_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    CHECK (status != 'confirmed' OR normalized_value IS NOT NULL)
) STRICT;

CREATE TABLE source_awards (
    source_award_id INTEGER PRIMARY KEY,
    award_year INTEGER NOT NULL,
    award_track TEXT NOT NULL,
    award_category_raw TEXT,
    award_tier TEXT NOT NULL,
    project_slug_raw TEXT,
    firm_slug_raw TEXT,
    source_url TEXT NOT NULL,
    fetched_at TEXT,
    source_composite_key TEXT NOT NULL,
    logical_duplicate_group_size INTEGER NOT NULL CHECK (logical_duplicate_group_size >= 1)
) STRICT;

CREATE TABLE award_entity_links (
    source_award_id INTEGER PRIMARY KEY REFERENCES source_awards(source_award_id),
    target_type TEXT NOT NULL CHECK (target_type IN ('project','firm')),
    raw_slug TEXT NOT NULL,
    resolved_source_project_id INTEGER REFERENCES source_projects(source_project_id),
    resolved_source_firm_slug TEXT REFERENCES source_firms(source_firm_slug),
    link_status TEXT NOT NULL CHECK (link_status IN ('resolved','stub_only','unresolved')),
    evidence_field TEXT NOT NULL,
    CHECK (
        (target_type = 'project' AND resolved_source_firm_slug IS NULL)
        OR (target_type = 'firm' AND resolved_source_project_id IS NULL)
    )
) STRICT;

CREATE TABLE image_assets (
    asset_id TEXT PRIMARY KEY,
    asset_key TEXT NOT NULL UNIQUE,
    normalized_url TEXT NOT NULL,
    host TEXT NOT NULL,
    path TEXT NOT NULL,
    is_placeholder_candidate INTEGER NOT NULL CHECK (is_placeholder_candidate IN (0,1)),
    asset_key_version TEXT NOT NULL
) STRICT;

CREATE TABLE image_urls (
    image_url_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL REFERENCES image_assets(asset_id),
    raw_url TEXT NOT NULL UNIQUE,
    normalized_url TEXT NOT NULL,
    source_host TEXT NOT NULL
) STRICT;

CREATE TABLE source_image_occurrences (
    occurrence_id TEXT PRIMARY KEY,
    source_project_id INTEGER NOT NULL REFERENCES source_projects(source_project_id),
    role TEXT NOT NULL CHECK (role IN ('cover','gallery')),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    raw_url TEXT NOT NULL,
    image_url_id TEXT REFERENCES image_urls(image_url_id),
    asset_id TEXT REFERENCES image_assets(asset_id),
    parse_status TEXT NOT NULL
        CHECK (parse_status IN ('parsed','malformed','placeholder_candidate')),
    parse_error TEXT,
    source_field TEXT NOT NULL CHECK (source_field IN ('og:image:cover','og:image:gallery')),
    image_type TEXT,
    CHECK (
        (parse_status = 'malformed' AND asset_id IS NULL AND image_url_id IS NULL
         AND parse_error IS NOT NULL)
        OR (parse_status != 'malformed' AND asset_id IS NOT NULL AND image_url_id IS NOT NULL)
    ),
    UNIQUE (source_project_id,role,ordinal)
) STRICT;

CREATE TABLE project_image_global_id_occurrences (
    global_id_occurrence_id TEXT PRIMARY KEY,
    source_project_id INTEGER NOT NULL REFERENCES source_projects(source_project_id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    raw_global_id TEXT NOT NULL,
    alignment_status TEXT NOT NULL CHECK (alignment_status = 'unresolved'),
    alignment_reason TEXT NOT NULL,
    UNIQUE (source_project_id,ordinal)
) STRICT;

CREATE TABLE image_work_queue (
    asset_id TEXT PRIMARY KEY REFERENCES image_assets(asset_id),
    phash_status TEXT NOT NULL CHECK (phash_status = 'pending'),
    classification_status TEXT NOT NULL CHECK (classification_status = 'pending'),
    queue_reason TEXT NOT NULL,
    network_calls_made INTEGER NOT NULL CHECK (network_calls_made = 0)
) STRICT;

CREATE TABLE duplicate_candidates (
    candidate_id TEXT PRIMARY KEY,
    left_project_id INTEGER NOT NULL REFERENCES source_projects(source_project_id),
    right_project_id INTEGER NOT NULL REFERENCES source_projects(source_project_id),
    candidate_kind TEXT NOT NULL CHECK (candidate_kind IN ('strict','exact_review','fuzzy_review')),
    normalized_name_left TEXT NOT NULL,
    normalized_name_right TEXT NOT NULL,
    name_similarity REAL NOT NULL CHECK (name_similarity >= 0.0 AND name_similarity <= 1.0),
    exact_name INTEGER NOT NULL CHECK (exact_name IN (0,1)),
    same_firm INTEGER NOT NULL CHECK (same_firm IN (0,1)),
    same_country INTEGER NOT NULL CHECK (same_country IN (0,1)),
    same_city INTEGER NOT NULL CHECK (same_city IN (0,1)),
    same_nonnull_year INTEGER NOT NULL CHECK (same_nonnull_year IN (0,1)),
    generic_name INTEGER NOT NULL CHECK (generic_name IN (0,1)),
    phase_marker INTEGER NOT NULL CHECK (phase_marker IN (0,1)),
    score REAL NOT NULL CHECK (score >= 0.0 AND score <= 1.0),
    score_breakdown_json TEXT NOT NULL CHECK (json_valid(score_breakdown_json)),
    evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json)),
    decision_status TEXT NOT NULL CHECK (decision_status IN ('auto_clustered','review')),
    rule_id TEXT NOT NULL,
    phash_available INTEGER NOT NULL CHECK (phash_available = 0),
    vision_available INTEGER NOT NULL CHECK (vision_available = 0),
    CHECK (left_project_id < right_project_id),
    CHECK (
        (exact_name = 1 AND normalized_name_left = normalized_name_right)
        OR (exact_name = 0 AND normalized_name_left != normalized_name_right)
    ),
    CHECK (
        decision_status != 'auto_clustered'
        OR (
            candidate_kind = 'strict' AND exact_name = 1 AND same_firm = 1
            AND same_country = 1 AND same_city = 1 AND same_nonnull_year = 1
            AND generic_name = 0 AND phase_marker = 0
        )
    ),
    UNIQUE (left_project_id,right_project_id)
) STRICT;

CREATE TABLE buildings (
    building_id TEXT PRIMARY KEY,
    identity_status TEXT NOT NULL CHECK (identity_status = 'provisional'),
    preferred_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    primary_project_id INTEGER NOT NULL UNIQUE REFERENCES source_projects(source_project_id),
    project_count INTEGER NOT NULL CHECK (project_count >= 1),
    cluster_decision TEXT NOT NULL CHECK (cluster_decision IN ('singleton','strict_auto_cluster')),
    cluster_rule_id TEXT NOT NULL,
    source_firm_slug TEXT NOT NULL REFERENCES source_firms(source_firm_slug),
    location_country TEXT,
    location_country_status TEXT NOT NULL
        CHECK (location_country_status IN ('confirmed','candidate','review','missing')),
    location_city TEXT,
    location_city_status TEXT NOT NULL
        CHECK (location_city_status IN ('confirmed','candidate','review','missing')),
    completion_year INTEGER,
    year_status TEXT NOT NULL CHECK (year_status IN ('confirmed','candidate','review','missing')),
    p_hash_used INTEGER NOT NULL CHECK (p_hash_used = 0),
    vision_used INTEGER NOT NULL CHECK (vision_used = 0),
    CHECK (
        (location_country IS NOT NULL AND location_country_status IN ('confirmed','candidate'))
        OR (location_country IS NULL AND location_country_status IN ('review','missing'))
    ),
    CHECK (
        (location_city IS NOT NULL AND location_city_status IN ('confirmed','candidate'))
        OR (location_city IS NULL AND location_city_status IN ('review','missing'))
    )
) STRICT;

CREATE TABLE building_projects (
    building_id TEXT NOT NULL REFERENCES buildings(building_id),
    source_project_id INTEGER NOT NULL UNIQUE REFERENCES source_projects(source_project_id),
    is_primary INTEGER NOT NULL CHECK (is_primary IN (0,1)),
    membership_status TEXT NOT NULL CHECK (membership_status IN ('singleton','auto_clustered')),
    rule_id TEXT NOT NULL,
    evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json)),
    PRIMARY KEY (building_id,source_project_id)
) WITHOUT ROWID, STRICT;

CREATE UNIQUE INDEX idx_building_one_primary
ON building_projects(building_id) WHERE is_primary = 1;

CREATE TABLE cluster_events (
    cluster_event_id TEXT PRIMARY KEY,
    building_id TEXT NOT NULL REFERENCES buildings(building_id),
    event_type TEXT NOT NULL CHECK (event_type IN ('singleton_created','strict_cluster_created')),
    member_project_ids_json TEXT NOT NULL CHECK (json_valid(member_project_ids_json)),
    rule_id TEXT NOT NULL,
    evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json))
) STRICT;

CREATE TABLE building_facets (
    building_id TEXT NOT NULL REFERENCES buildings(building_id),
    axis TEXT NOT NULL,
    value TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('confirmed','candidate','conflict','review')),
    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    claim_count INTEGER NOT NULL CHECK (claim_count >= 1),
    independent_project_count INTEGER NOT NULL CHECK (independent_project_count >= 1),
    resolution_rule TEXT NOT NULL,
    PRIMARY KEY (building_id,axis,value)
) WITHOUT ROWID, STRICT;

CREATE TABLE building_facet_claims (
    building_id TEXT NOT NULL,
    axis TEXT NOT NULL,
    value TEXT NOT NULL,
    claim_id TEXT NOT NULL REFERENCES attribute_claims(claim_id),
    PRIMARY KEY (building_id,axis,value,claim_id),
    FOREIGN KEY (building_id,axis,value)
        REFERENCES building_facets(building_id,axis,value)
) WITHOUT ROWID, STRICT;

CREATE TABLE project_completeness (
    source_project_id INTEGER PRIMARY KEY REFERENCES source_projects(source_project_id),
    has_firm INTEGER NOT NULL CHECK (has_firm IN (0,1)),
    has_location INTEGER NOT NULL CHECK (has_location IN (0,1)),
    has_confirmed_year INTEGER NOT NULL CHECK (has_confirmed_year IN (0,1)),
    has_description INTEGER NOT NULL CHECK (has_description IN (0,1)),
    has_category INTEGER NOT NULL CHECK (has_category IN (0,1)),
    has_image INTEGER NOT NULL CHECK (has_image IN (0,1)),
    completeness_score REAL NOT NULL CHECK (completeness_score >= 0.0 AND completeness_score <= 1.0)
) STRICT;

CREATE TABLE building_completeness (
    building_id TEXT PRIMARY KEY REFERENCES buildings(building_id),
    completeness_score REAL NOT NULL CHECK (completeness_score >= 0.0 AND completeness_score <= 1.0),
    missing_fields_json TEXT NOT NULL CHECK (json_valid(missing_fields_json))
) STRICT;

CREATE TABLE qa_issues (
    qa_issue_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    issue_code TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('info','warning','error')),
    status TEXT NOT NULL CHECK (status IN ('open','resolved','ignored')),
    details_json TEXT NOT NULL CHECK (json_valid(details_json)),
    policy_version TEXT NOT NULL,
    UNIQUE (entity_type,entity_id,issue_code)
) STRICT;

CREATE TABLE build_metrics (
    metric_name TEXT PRIMARY KEY,
    metric_value_json TEXT NOT NULL CHECK (json_valid(metric_value_json))
) STRICT;

CREATE INDEX idx_source_projects_firm ON source_projects(source_firm_slug);
CREATE INDEX idx_source_projects_location ON source_projects(location_country_raw,location_city_raw);
CREATE INDEX idx_project_categories_project ON project_category_occurrences(source_project_id);
CREATE INDEX idx_attribute_claims_project ON attribute_claims(source_project_id,axis,status);
CREATE INDEX idx_image_occurrences_project ON source_image_occurrences(source_project_id,role,ordinal);
CREATE INDEX idx_image_occurrences_asset ON source_image_occurrences(asset_id);
CREATE INDEX idx_award_project_slug ON source_awards(project_slug_raw);
CREATE INDEX idx_award_firm_slug ON source_awards(firm_slug_raw);
CREATE INDEX idx_duplicate_review ON duplicate_candidates(decision_status,candidate_kind,score);
CREATE INDEX idx_facet_axis_status ON building_facets(axis,status,value);
CREATE INDEX idx_qa_status_code ON qa_issues(status,issue_code);

CREATE VIEW v_project_category_provenance AS
SELECT
    p.source_project_id,
    p.slug AS project_slug,
    o.ordinal,
    o.raw_value AS raw_category,
    m.axis,
    m.normalized_value,
    m.mapping_kind,
    m.confidence,
    m.status AS mapping_status,
    m.rule_id,
    m.evidence
FROM source_projects p
JOIN project_category_occurrences o USING (source_project_id)
LEFT JOIN category_mappings m USING (category_id);

CREATE VIEW v_building_project_provenance AS
SELECT
    b.building_id,
    b.identity_status,
    bp.source_project_id,
    p.slug AS project_slug,
    p.source_url,
    bp.is_primary,
    bp.membership_status,
    bp.rule_id,
    bp.evidence_json
FROM buildings b
JOIN building_projects bp USING (building_id)
JOIN source_projects p USING (source_project_id);

CREATE VIEW v_building_images AS
SELECT
    bp.building_id,
    i.asset_id,
    MIN(i.raw_url) AS representative_raw_url,
    MIN(a.normalized_url) AS normalized_url,
    MIN(CASE i.role WHEN 'cover' THEN 0 ELSE 1 END) AS role_rank,
    COUNT(*) AS source_occurrence_count,
    MAX(a.is_placeholder_candidate) AS is_placeholder_candidate
FROM building_projects bp
JOIN source_image_occurrences i USING (source_project_id)
JOIN image_assets a USING (asset_id)
GROUP BY bp.building_id,i.asset_id;

CREATE VIEW v_search_facets AS
SELECT
    f.building_id,
    f.axis,
    f.value,
    f.status,
    f.confidence,
    f.claim_count,
    f.independent_project_count,
    f.resolution_rule
FROM building_facets f
WHERE f.status IN ('confirmed','candidate','conflict','review');

CREATE VIEW v_duplicate_review_queue AS
SELECT *
FROM duplicate_candidates
WHERE decision_status = 'review';

CREATE VIEW v_unmapped_categories AS
SELECT
    c.category_id,
    c.raw_value,
    c.source_occurrence_count,
    c.project_count,
    c.mapping_status
FROM source_categories c
WHERE c.mapping_status != 'mapped';

CREATE VIEW v_qa_open AS
SELECT * FROM qa_issues WHERE status = 'open';

CREATE VIEW v_image_hash_queue AS
SELECT
    q.asset_id,
    a.normalized_url,
    q.phash_status,
    q.queue_reason
FROM image_work_queue q
JOIN image_assets a USING (asset_id)
WHERE q.phash_status = 'pending';

CREATE VIEW v_image_classification_queue AS
SELECT
    q.asset_id,
    a.normalized_url,
    q.classification_status,
    q.queue_reason
FROM image_work_queue q
JOIN image_assets a USING (asset_id)
WHERE q.classification_status = 'pending';

CREATE VIEW v_architizer_buildings_export AS
SELECT
    b.building_id,
    b.preferred_name AS name,
    b.source_firm_slug,
    sf.source_name AS firm_name,
    b.location_city,
    b.location_city_status,
    b.location_country,
    b.location_country_status,
    CASE WHEN b.year_status = 'confirmed' THEN b.completion_year END AS completion_year,
    b.year_status,
    (
        SELECT CASE WHEN COUNT(*) = 1 THEN MIN(value) END
        FROM building_facets f
        WHERE f.building_id = b.building_id AND f.axis = 'program'
          AND f.status = 'confirmed'
    ) AS program_primary,
    (
        SELECT CASE WHEN COUNT(*) = 1 THEN MIN(value) END
        FROM building_facets f
        WHERE f.building_id = b.building_id AND f.axis = 'typology'
          AND f.status = 'confirmed'
    ) AS typology_primary,
    COALESCE((
        SELECT json_group_array(value)
        FROM (
            SELECT f.value
            FROM building_facets f
            WHERE f.building_id = b.building_id AND f.axis = 'program'
              AND f.status IN ('confirmed','conflict')
            ORDER BY f.value
        )
    ), json('[]')) AS program_tags_json,
    COALESCE((
        SELECT json_group_array(value)
        FROM (
            SELECT f.value
            FROM building_facets f
            WHERE f.building_id = b.building_id AND f.axis = 'typology'
              AND f.status IN ('confirmed','conflict')
            ORDER BY f.value
        )
    ), json('[]')) AS typology_tags_json,
    COALESCE((
        SELECT json_group_array(value)
        FROM (
            SELECT f.value
            FROM building_facets f
            WHERE f.building_id = b.building_id AND f.axis = 'work_type'
              AND f.status IN ('confirmed','conflict')
            ORDER BY f.value
        )
    ), json('[]')) AS work_type_tags_json,
    COALESCE((
        SELECT json_group_array(value)
        FROM (
            SELECT f.value
            FROM building_facets f
            WHERE f.building_id = b.building_id AND f.axis = 'material'
              AND f.status IN ('confirmed','conflict')
            ORDER BY f.value
        )
    ), json('[]')) AS material_tags_json,
    p.description,
    p.description_short,
    (
        SELECT CASE WHEN COUNT(*) = 1 THEN MIN(value) END
        FROM building_facets f
        WHERE f.building_id = b.building_id AND f.axis = 'area_bucket'
          AND f.status = 'confirmed'
    ) AS area_bucket,
    (
        SELECT CASE WHEN COUNT(*) = 1 THEN MIN(value) END
        FROM building_facets f
        WHERE f.building_id = b.building_id AND f.axis = 'project_status'
          AND f.status = 'confirmed'
    ) AS project_status,
    COALESCE((
        SELECT json_group_array(source_project_id)
        FROM (
            SELECT bp.source_project_id
            FROM building_projects bp
            WHERE bp.building_id = b.building_id
            ORDER BY bp.source_project_id
        )
    ), json('[]')) AS source_project_ids_json,
    COALESCE((
        SELECT json_group_array(source_url)
        FROM (
            SELECT sp.source_url
            FROM building_projects bp
            JOIN source_projects sp USING (source_project_id)
            WHERE bp.building_id = b.building_id
            ORDER BY bp.source_project_id
        )
    ), json('[]')) AS source_urls_json,
    (
        SELECT i.raw_url
        FROM building_projects bp
        JOIN source_image_occurrences i USING (source_project_id)
        JOIN image_assets a USING (asset_id)
        WHERE bp.building_id = b.building_id AND i.role = 'cover'
          AND i.parse_status = 'parsed'
          AND a.is_placeholder_candidate = 0
        ORDER BY bp.is_primary DESC,bp.source_project_id,i.ordinal
        LIMIT 1
    ) AS cover_image_url,
    COALESCE((
        SELECT json_group_array(representative_raw_url)
        FROM (
            SELECT vi.representative_raw_url
            FROM v_building_images vi
            WHERE vi.building_id = b.building_id
              AND vi.is_placeholder_candidate = 0
            ORDER BY vi.role_rank,vi.asset_id
        )
    ), json('[]')) AS image_urls_json,
    b.project_count,
    bc.completeness_score,
    CASE
        WHEN EXISTS (
            SELECT 1 FROM building_facets f
            WHERE f.building_id=b.building_id
              AND f.axis IN ('program','typology','work_type','material')
              AND f.status='conflict'
        ) THEN 'conflict'
        WHEN EXISTS (
            SELECT 1 FROM building_facets f
            WHERE f.building_id=b.building_id
              AND f.axis IN ('program','typology','work_type','material')
              AND f.status='confirmed'
        ) THEN 'confirmed'
        WHEN EXISTS (
            SELECT 1 FROM building_facets f
            WHERE f.building_id=b.building_id
              AND f.axis IN ('program','typology','work_type','material')
              AND f.status IN ('candidate','review')
        ) THEN 'candidate'
        ELSE 'unmapped'
    END AS taxonomy_status
FROM buildings b
JOIN source_projects p ON p.source_project_id=b.primary_project_id
JOIN source_firms sf ON sf.source_firm_slug=b.source_firm_slug
JOIN building_completeness bc USING (building_id);
"""


class UnionFind:
    def __init__(self, values: Iterable[int]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: int) -> int:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: int, right: int) -> None:
        a = self.find(left)
        b = self.find(right)
        if a == b:
            return
        if a < b:
            self.parent[b] = a
        else:
            self.parent[a] = b

    def components(self) -> list[list[int]]:
        groups: dict[int, list[int]] = defaultdict(list)
        for value in sorted(self.parent):
            groups[self.find(value)].append(value)
        return sorted(groups.values(), key=lambda values: (values[0], values))


def create_output(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(DDL)
    return connection


def _add_qa(
    output: sqlite3.Connection,
    *,
    entity_type: str,
    entity_id: Any,
    issue_code: str,
    severity: str,
    details: Any,
) -> None:
    qa_id = _stable_id("qa_", entity_type, entity_id, issue_code)
    output.execute(
        """
        INSERT OR IGNORE INTO qa_issues(
            qa_issue_id,entity_type,entity_id,issue_code,severity,status,
            details_json,policy_version
        ) VALUES (?,?,?,?,?,'open',?,?)
        """,
        (
            qa_id,
            entity_type,
            str(entity_id),
            issue_code,
            severity,
            _json(details),
            POLICY_VERSION,
        ),
    )


def _add_claim(
    output: sqlite3.Connection,
    *,
    project_id: int,
    axis: str,
    raw_value: Any,
    normalized_value: Optional[str],
    claim_kind: str,
    evidence_type: str,
    evidence_ref: str,
    evidence: Any,
    confidence: float,
    status: str,
    rule_id: str,
    category_occurrence_id: Optional[str] = None,
    target_scope: str = "building",
) -> str:
    claim_id = _stable_id(
        "claim_",
        project_id,
        axis,
        normalized_value,
        category_occurrence_id,
        evidence_ref,
        rule_id,
    )
    output.execute(
        """
        INSERT INTO attribute_claims(
            claim_id,source_project_id,category_occurrence_id,axis,raw_value,
            normalized_value,target_scope,claim_kind,evidence_type,evidence_ref,
            evidence_json,confidence,status,rule_id,policy_version
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            claim_id,
            project_id,
            category_occurrence_id,
            axis,
            None if raw_value is None else str(raw_value),
            normalized_value,
            target_scope,
            claim_kind,
            evidence_type,
            evidence_ref,
            _json(evidence),
            confidence,
            status,
            rule_id,
            POLICY_VERSION,
        ),
    )
    return claim_id


def _preferred_name(values: Iterable[Optional[str]]) -> Optional[str]:
    counts = Counter(
        clean_scalar(value) for value in values if clean_scalar(value) is not None
    )
    if not counts:
        return None
    top = max(counts.values())
    return sorted(value for value, count in counts.items() if count == top)[0]


def select_awards(
    source: sqlite3.Connection,
    projects: Sequence[dict[str, Any]],
    *,
    full: bool,
) -> list[dict[str, Any]]:
    awards = [
        dict(row)
        for row in source.execute("SELECT * FROM architizer_awards ORDER BY id")
    ]
    if full:
        return awards
    project_slugs = {str(row["slug"]) for row in projects}
    firm_slugs = {str(row["firm_slug"]) for row in projects if row["firm_slug"]}
    return [
        row
        for row in awards
        if row["project_slug"] in project_slugs or row["firm_slug"] in firm_slugs
    ]


def import_firms(
    source: sqlite3.Connection,
    output: sqlite3.Connection,
    projects: Sequence[dict[str, Any]],
    awards: Sequence[dict[str, Any]],
    *,
    full: bool,
) -> dict[str, str]:
    crawled_rows = {
        str(row["slug"]): dict(row)
        for row in source.execute("SELECT * FROM architizer_firms ORDER BY slug")
    }
    project_names: dict[str, list[Optional[str]]] = defaultdict(list)
    for project in projects:
        slug = clean_scalar(project["firm_slug"])
        if slug:
            project_names[slug].append(project["firm_name"])
    required_slugs = set(project_names)
    required_slugs.update(
        str(award["firm_slug"])
        for award in awards
        if clean_scalar(award["firm_slug"])
    )
    if full:
        required_slugs.update(crawled_rows)

    origins: dict[str, str] = {}
    for slug in sorted(required_slugs):
        crawled = crawled_rows.get(slug)
        if crawled:
            origin = "crawled"
            source_name = crawled["name"]
            offices_raw = crawled["office_locations"]
            description = crawled["description"]
            awards_summary = crawled["awards_summary"]
            project_count_seen = crawled["project_count_seen"]
            social_raw = crawled["social_links"]
            fetched_at = crawled["fetched_at"]
        elif slug in project_names:
            origin = "project_stub"
            source_name = _preferred_name(project_names[slug])
            offices_raw = "[]"
            description = None
            awards_summary = None
            project_count_seen = None
            social_raw = "{}"
            fetched_at = None
        else:
            origin = "award_stub"
            source_name = None
            offices_raw = "[]"
            description = None
            awards_summary = None
            project_count_seen = None
            social_raw = "{}"
            fetched_at = None
        origins[slug] = origin
        office_values, office_error = _decode_json_list(offices_raw)
        social_values, social_error = _decode_json_dict(social_raw)
        output.execute(
            """
            INSERT INTO source_firms(
                source_firm_slug,source_name,record_origin,
                office_locations_raw_json,description,awards_summary,
                project_count_seen,social_links_raw_json,fetched_at,source_url
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                slug,
                source_name,
                origin,
                offices_raw if office_error is None else None,
                description,
                awards_summary,
                project_count_seen,
                social_raw if social_error is None else None,
                fetched_at,
                f"https://architizer.com/firms/{slug}/",
            ),
        )
        if office_error:
            _add_qa(
                output,
                entity_type="firm",
                entity_id=slug,
                issue_code="malformed_office_locations_json",
                severity="warning",
                details={"error": office_error},
            )
        for ordinal, raw_value in enumerate(office_values):
            display = raw_value if isinstance(raw_value, str) else None
            output.execute(
                """
                INSERT INTO firm_office_occurrences(
                    source_firm_slug,ordinal,raw_value_json,display_value,parse_status
                ) VALUES (?,?,?,?,?)
                """,
                (
                    slug,
                    ordinal,
                    _json(raw_value),
                    display,
                    "parsed" if display is not None else "unresolved",
                ),
            )
        if social_error:
            _add_qa(
                output,
                entity_type="firm",
                entity_id=slug,
                issue_code="malformed_social_links_json",
                severity="warning",
                details={"error": social_error},
            )
        for platform, raw_url in sorted(social_values.items()):
            if isinstance(raw_url, str) and raw_url.strip():
                output.execute(
                    """
                    INSERT INTO firm_social_links(
                        source_firm_slug,platform,raw_url,source_field
                    ) VALUES (?,?,?,'social_links')
                    """,
                    (slug, str(platform), raw_url),
                )
    return origins


def _insert_text_version(
    output: sqlite3.Connection,
    project_id: int,
    source_field: str,
    text: Optional[str],
) -> None:
    if not text:
        quality = "missing"
        digest = None
    elif source_field == "description_short":
        quality = "mojibake_possible" if text_has_mojibake(text) else "og_snippet"
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    else:
        quality = "mojibake_possible" if text_has_mojibake(text) else "present"
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    output.execute(
        """
        INSERT INTO project_text_versions(
            text_version_id,source_project_id,source_field,raw_text,text_sha256,
            quality_status,processor_version
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (
            _stable_id("txt_", project_id, source_field),
            project_id,
            source_field,
            text,
            digest,
            quality,
            "architizer-raw-text-preserve-v1",
        ),
    )


def _phase_marker(name: str, slug: Optional[str] = None) -> bool:
    normalized = normalize_identity_text(f"{name} {slug or ''}")
    tokens = set(normalized.split())
    return bool(
        tokens.intersection(
            {
                "phase",
                "stage",
                "extension",
                "addition",
                "expansion",
                "renovation",
                "variant",
                "version",
            }
        )
        or any(token.startswith(("phase", "stage")) for token in tokens)
    )


def _location_part_policy(
    location_full: Any,
    raw_value: Any,
    *,
    part: str,
) -> tuple[Optional[str], str, str]:
    """Return a normalized location part, claim status, and policy reason.

    The crawler stores the first and last comma-separated header tokens as
    city and country.  The first token may actually be a street or
    state/province rather than a city, so every plausible first token remains
    a candidate.  Abstain when it looks like an administrative abbreviation
    or contains no letters.
    """

    if part not in {"city", "country"}:
        raise ValueError(f"unsupported location part: {part}")
    text = clean_scalar(raw_value)
    if not text:
        return None, "missing", "source_value_missing"
    full = clean_scalar(location_full)
    tokens = (
        [token.strip() for token in full.split(",") if token.strip()]
        if full
        else []
    )
    if len(tokens) < 2:
        return None, "review", "location_header_shape_unverified"
    expected = tokens[0] if part == "city" else tokens[-1]
    if normalize_identity_text(text) != normalize_identity_text(expected):
        return None, "review", "parsed_value_header_mismatch"
    if not any(character.isalpha() for character in text):
        return None, "review", f"non_alphabetic_{part}_token"
    if part == "country":
        if re.fullmatch(r"[A-Z]{2,3}", text) or re.search(
            r"\b(?:of|the|and)$", text, re.IGNORECASE
        ):
            return None, "review", "country_token_semantics_incomplete"
        return text, "candidate", "last_header_token_semantics_unverified"
    if re.fullmatch(r"[A-Z]{2}", text):
        return None, "review", "likely_admin_area_abbreviation"
    return text, "candidate", "first_header_token_semantics_unverified"


def import_projects(
    output: sqlite3.Connection,
    projects: Sequence[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    imported: dict[int, dict[str, Any]] = {}
    for raw in projects:
        project = dict(raw)
        project_id = int(project["id"])
        categories, categories_error = _decode_json_list(project["categories"])
        gallery, gallery_error = _decode_json_list(project["gallery_image_urls"])
        global_ids, global_ids_error = _decode_json_list(project["image_global_ids"])
        year_value, year_status = valid_or_candidate_year(
            project["completion_year"],
            project["constr_status"],
            current_year=CURRENT_YEAR,
        )
        size = parse_size_bucket(
            project["building_size_slug"], project["building_size_display"]
        )
        size = size or {}
        firm_slug = clean_scalar(project["firm_slug"])
        if not firm_slug:
            raise BuildError(f"project {project_id} has no firm_slug")
        expected_global_id = f"projects.project.{project_id}"
        if project["global_id"] == expected_global_id:
            acceptance_status = "accepted"
            exclusion_reason = None
        else:
            acceptance_status = "excluded"
            exclusion_reason = "global_id_entity_type_mismatch"
        output.execute(
            """
            INSERT INTO source_projects(
                source_project_id,global_id,slug,source_url,name,normalized_name,
                source_firm_slug,source_firm_name,description,description_short,
                completion_year_raw,year_claim_status,building_size_slug,
                building_size_display,area_min_sqft,area_max_sqft,area_open_ended,
                constr_status_raw,budget_raw,location_full,location_country_raw,
                location_city_raw,categories_source_text,
                gallery_image_urls_source_text,image_global_ids_source_text,
                categories_raw_json,gallery_image_urls_raw_json,
                image_global_ids_raw_json,published_time,modified_time,fetched_at,
                acceptance_status,exclusion_reason,category_occurrence_count,
                gallery_occurrence_count,image_global_id_occurrence_count
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                      ?,?,?,?,?,?,?)
            """,
            (
                project_id,
                str(project["global_id"]),
                str(project["slug"]),
                f"https://architizer.com/projects/{project['slug']}/",
                str(project["name"]),
                normalize_identity_text(project["name"]),
                firm_slug,
                clean_scalar(project["firm_name"]),
                project["description"],
                project["description_short"],
                project["completion_year"],
                year_status,
                clean_scalar(project["building_size_slug"]),
                clean_scalar(project["building_size_display"]),
                size.get("min_sqft"),
                size.get("max_sqft"),
                int(bool(size.get("is_open_ended"))),
                clean_scalar(project["constr_status"]),
                project["budget"],
                clean_scalar(project["location_full"]),
                clean_scalar(project["location_country"]),
                clean_scalar(project["location_city"]),
                None if project["categories"] is None else str(project["categories"]),
                (
                    None
                    if project["gallery_image_urls"] is None
                    else str(project["gallery_image_urls"])
                ),
                (
                    None
                    if project["image_global_ids"] is None
                    else str(project["image_global_ids"])
                ),
                _json(categories),
                _json(gallery),
                _json(global_ids),
                clean_scalar(project["published_time"]),
                clean_scalar(project["modified_time"]),
                clean_scalar(project["fetched_at"]),
                acceptance_status,
                exclusion_reason,
                len(categories),
                len(gallery),
                len(global_ids),
            ),
        )
        output.execute(
            """
            INSERT INTO project_firms(
                source_project_id,source_firm_slug,relationship_kind,raw_firm_name,
                evidence_field,status
            ) VALUES (?,?,'source_primary_author',?,'article:author','source_asserted')
            """,
            (project_id, firm_slug, clean_scalar(project["firm_name"])),
        )
        _insert_text_version(
            output, project_id, "description", project["description"]
        )
        _insert_text_version(
            output, project_id, "description_short", project["description_short"]
        )
        if categories_error:
            _add_qa(
                output,
                entity_type="project",
                entity_id=project_id,
                issue_code="malformed_categories_json",
                severity="error",
                details={"error": categories_error, "raw": project["categories"]},
            )
        if gallery_error:
            _add_qa(
                output,
                entity_type="project",
                entity_id=project_id,
                issue_code="malformed_gallery_json",
                severity="error",
                details={"error": gallery_error, "raw": project["gallery_image_urls"]},
            )
        if global_ids_error:
            _add_qa(
                output,
                entity_type="project",
                entity_id=project_id,
                issue_code="malformed_image_global_ids_json",
                severity="error",
                details={"error": global_ids_error, "raw": project["image_global_ids"]},
            )
        if year_status == "review":
            _add_qa(
                output,
                entity_type="project",
                entity_id=project_id,
                issue_code="completion_year_review",
                severity="warning",
                details={
                    "raw_year": project["completion_year"],
                    "construction_status": project["constr_status"],
                },
            )
        if acceptance_status == "excluded":
            _add_qa(
                output,
                entity_type="project",
                entity_id=project_id,
                issue_code="global_id_entity_type_mismatch",
                severity="error",
                details={
                    "global_id": project["global_id"],
                    "expected_global_id": expected_global_id,
                    "exclusion_reason": exclusion_reason,
                },
            )
        if not clean_scalar(project["constr_status"]):
            _add_qa(
                output,
                entity_type="project",
                entity_id=project_id,
                issue_code="missing_construction_status",
                severity="warning",
                details={},
            )
        if text_has_mojibake(project["description"]) or text_has_mojibake(
            project["description_short"]
        ):
            _add_qa(
                output,
                entity_type="project",
                entity_id=project_id,
                issue_code="text_mojibake_possible",
                severity="warning",
                details={"fields": ["description", "description_short"]},
            )
        project["_categories"] = categories
        project["_categories_error"] = categories_error
        project["_gallery"] = gallery
        project["_global_ids"] = global_ids
        project["_year_value"] = year_value
        project["_year_status"] = year_status
        project["_size"] = size
        project["_phase_marker"] = _phase_marker(
            str(project["name"]), str(project["slug"])
        )
        (
            project["_location_country_value"],
            project["_location_country_status"],
            project["_location_country_reason"],
        ) = _location_part_policy(
            project["location_full"],
            project["location_country"],
            part="country",
        )
        (
            project["_location_city_value"],
            project["_location_city_status"],
            project["_location_city_reason"],
        ) = _location_part_policy(
            project["location_full"],
            project["location_city"],
            part="city",
        )
        project["_acceptance_status"] = acceptance_status
        project["_exclusion_reason"] = exclusion_reason
        imported[project_id] = project
    return imported


def import_taxonomy_and_claims(
    output: sqlite3.Connection,
    projects: dict[int, dict[str, Any]],
) -> None:
    occurrence_counts: Counter[str] = Counter()
    project_counts: Counter[str] = Counter()
    non_text_values: set[str] = set()
    for project in projects.values():
        raw_values: list[str] = []
        for value in project["_categories"]:
            if isinstance(value, str):
                raw_values.append(value)
            else:
                serialized = _json(value)
                raw_values.append(serialized)
                non_text_values.add(serialized)
        occurrence_counts.update(raw_values)
        project_counts.update(set(raw_values))

    category_ids: dict[str, str] = {}
    category_mapping_rows: dict[str, list[Any]] = {}
    for raw_value in sorted(occurrence_counts, key=lambda value: (value.casefold(), value)):
        category_id = _stable_id("cat_", raw_value)
        category_ids[raw_value] = category_id
        mappings = (
            [] if raw_value in non_text_values else list(mappings_for_category(raw_value))
        )
        category_mapping_rows[raw_value] = mappings
        if raw_value in non_text_values:
            mapping_status = "review"
        elif mappings:
            mapping_status = (
                "mapped"
                if any(mapping.status in {"confirmed", "candidate"} for mapping in mappings)
                else "review"
            )
        else:
            mapping_status = "unmapped"
        output.execute(
            """
            INSERT INTO source_categories(
                category_id,raw_value,normalized_key,source_occurrence_count,
                project_count,mapping_status,taxonomy_version
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (
                category_id,
                raw_value,
                normalize_identity_text(raw_value),
                occurrence_counts[raw_value],
                project_counts[raw_value],
                mapping_status,
                TAXONOMY_VERSION,
            ),
        )
        if not mappings:
            non_text = raw_value in non_text_values
            output.execute(
                """
                INSERT INTO category_mappings(
                    mapping_id,category_id,axis,normalized_value,target_scope,
                    mapping_kind,confidence,evidence,rule_id,status,taxonomy_version
                ) VALUES (?,?,NULL,NULL,'building','unmapped',0.0,?,
                          ?,'unmapped',?)
                """,
                (
                    _stable_id("map_", raw_value, "unmapped"),
                    category_id,
                    (
                        "Non-text source category value."
                        if non_text
                        else "No reviewed normalized semantic mapping."
                    ),
                    (
                        "architizer-category-non-text-v1"
                        if non_text
                        else "architizer-category-explicit-unmapped-v1"
                    ),
                    TAXONOMY_VERSION,
                ),
            )
        else:
            for mapping in mappings:
                output.execute(
                    """
                    INSERT INTO category_mappings(
                        mapping_id,category_id,axis,normalized_value,target_scope,
                        mapping_kind,confidence,evidence,rule_id,status,taxonomy_version
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        _stable_id(
                            "map_",
                            raw_value,
                            mapping.axis,
                            mapping.value,
                            mapping.rule_id,
                        ),
                        category_id,
                        mapping.axis,
                        mapping.value,
                        mapping.target_scope,
                        mapping.mapping_kind,
                        mapping.confidence,
                        mapping.evidence,
                        mapping.rule_id,
                        mapping.status,
                        TAXONOMY_VERSION,
                    ),
                )

    for project_id in sorted(projects):
        project = projects[project_id]
        categories = project["_categories"]
        category_strings = {value for value in categories if isinstance(value, str)}
        parent_count = len(category_strings.intersection(BROAD_CATEGORIES))
        severe_outlier = len(categories) >= 40 or (
            parent_count >= 7
            and not any(value in CATEGORY_PARENT for value in category_strings)
        )
        soft_outlier = len(categories) > 10 or parent_count >= 4
        if soft_outlier:
            _add_qa(
                output,
                entity_type="project",
                entity_id=project_id,
                issue_code="category_metadata_overloaded",
                severity="warning",
                details={
                    "category_count": len(categories),
                    "parent_count": parent_count,
                    "scalar_quarantined": True,
                    "severe_outlier": severe_outlier,
                },
            )
        for parent in sorted(category_strings.intersection(BROAD_CATEGORIES)):
            if not any(
                CATEGORY_PARENT.get(category) == parent
                for category in category_strings
            ):
                _add_qa(
                    output,
                    entity_type="project_category",
                    entity_id=f"{project_id}:{parent}",
                    issue_code="parent_without_leaf",
                    severity="info",
                    details={
                        "parent": parent,
                        "behavior": "supporting candidate only; no scalar default",
                    },
                )
        for ordinal, raw_item in enumerate(categories):
            if isinstance(raw_item, str):
                raw_value = raw_item
                parse_status = (
                    "malformed_container"
                    if project["_categories_error"]
                    else "parsed"
                )
            else:
                raw_value = _json(raw_item)
                parse_status = "non_text"
            category_id = category_ids[raw_value]
            occurrence_id = _stable_id("catocc_", project_id, ordinal, raw_value)
            output.execute(
                """
                INSERT INTO project_category_occurrences(
                    occurrence_id,source_project_id,ordinal,category_id,raw_value,
                    source_field,parse_status
                ) VALUES (?,?,?,?,?,'article:tag',?)
                """,
                (
                    occurrence_id,
                    project_id,
                    ordinal,
                    category_id,
                    raw_value,
                    parse_status,
                ),
            )
            mappings = category_mapping_rows.get(raw_value, [])
            if not mappings:
                _add_claim(
                    output,
                    project_id=project_id,
                    axis="raw_category",
                    raw_value=raw_value,
                    normalized_value=None,
                    claim_kind="unmapped",
                    evidence_type="article_tag",
                    evidence_ref=occurrence_id,
                    evidence={
                        "source_field": "article:tag",
                        "ordinal": ordinal,
                        "raw_category": raw_value,
                    },
                    confidence=0.0,
                    status="unmapped",
                    rule_id="architizer-category-explicit-unmapped-v1",
                    category_occurrence_id=occurrence_id,
                )
                continue
            expected_parent = CATEGORY_PARENT.get(raw_value)
            parent_mismatch = bool(
                expected_parent and expected_parent not in category_strings
            )
            if parent_mismatch:
                _add_qa(
                    output,
                    entity_type="project_category",
                    entity_id=f"{project_id}:{raw_value}",
                    issue_code="leaf_parent_mismatch",
                    severity="warning",
                    details={"leaf": raw_value, "expected_parent": expected_parent},
                )
            evidence_group = (
                expected_parent
                if expected_parent is not None
                else raw_value
            )
            for mapping in mappings:
                claim_status = mapping.status
                if claim_status == "confirmed" and (parent_mismatch or soft_outlier):
                    claim_status = "candidate"
                if mapping.mapping_kind == "direct":
                    claim_kind = "category_direct"
                elif mapping.mapping_kind == "work_type":
                    claim_kind = "work_type"
                else:
                    claim_kind = "category_supporting"
                _add_claim(
                    output,
                    project_id=project_id,
                    axis=mapping.axis,
                    raw_value=raw_value,
                    normalized_value=mapping.value,
                    claim_kind=claim_kind,
                    evidence_type="article_tag",
                    evidence_ref=occurrence_id,
                    evidence={
                        "source_field": "article:tag",
                        "ordinal": ordinal,
                        "raw_category": raw_value,
                        "canonical_parent": expected_parent,
                        "evidence_group": (
                            f"project:{project_id}:article_tag_path:{evidence_group}"
                        ),
                        "parent_mismatch": parent_mismatch,
                        "category_outlier_quarantine": soft_outlier,
                        "severe_category_outlier": severe_outlier,
                    },
                    confidence=mapping.confidence,
                    status=claim_status,
                    rule_id=mapping.rule_id,
                    category_occurrence_id=occurrence_id,
                    target_scope=mapping.target_scope,
                )

        status_raw = clean_scalar(project["constr_status"])
        if status_raw in {"built", "concept", "under-construction"}:
            _add_claim(
                output,
                project_id=project_id,
                axis="project_status",
                raw_value=status_raw,
                normalized_value=status_raw,
                claim_kind="source_structured",
                evidence_type="data_data_json",
                evidence_ref=f"project:{project_id}:constr_status",
                evidence={"source_field": "constr_status"},
                confidence=1.0,
                status="confirmed",
                rule_id="architizer-structured-status-v1",
            )
        elif status_raw:
            _add_claim(
                output,
                project_id=project_id,
                axis="project_status",
                raw_value=status_raw,
                normalized_value=status_raw,
                claim_kind="source_structured",
                evidence_type="data_data_json",
                evidence_ref=f"project:{project_id}:constr_status",
                evidence={"source_field": "constr_status", "unreviewed": True},
                confidence=0.5,
                status="review",
                rule_id="architizer-structured-status-unreviewed-v1",
            )
        if project["_year_status"] != "missing":
            normalized_year = (
                str(project["_year_value"])
                if project["_year_value"] is not None
                else None
            )
            _add_claim(
                output,
                project_id=project_id,
                axis="completion_year",
                raw_value=project["completion_year"],
                normalized_value=normalized_year,
                claim_kind="source_structured",
                evidence_type="data_data_json",
                evidence_ref=f"project:{project_id}:completion_year",
                evidence={
                    "source_field": "completion_date",
                    "precision": "year",
                    "construction_status": status_raw,
                },
                confidence=1.0 if project["_year_status"] == "confirmed" else 0.7,
                status=project["_year_status"],
                rule_id="architizer-completion-year-v1",
            )
        for axis, raw_field in (
            ("location_country", "location_country"),
            ("location_city", "location_city"),
        ):
            raw_value = clean_scalar(project[raw_field])
            if raw_value:
                key_prefix = (
                    "_location_city" if axis == "location_city" else "_location_country"
                )
                normalized_location = project[f"{key_prefix}_value"]
                location_status = project[f"{key_prefix}_status"]
                location_reason = project[f"{key_prefix}_reason"]
                _add_claim(
                    output,
                    project_id=project_id,
                    axis=axis,
                    raw_value=raw_value,
                    normalized_value=normalized_location,
                    claim_kind="source_structured",
                    evidence_type="parsed_location_header",
                    evidence_ref=f"project:{project_id}:{raw_field}",
                    evidence={
                        "source_field": raw_field,
                        "location_full": clean_scalar(project["location_full"]),
                        "parser_rule": "first_and_last_comma_tokens",
                        "policy_reason": location_reason,
                    },
                    confidence={
                        "confirmed": 0.95,
                        "candidate": 0.65,
                        "review": 0.3,
                    }[location_status],
                    status=location_status,
                    rule_id="architizer-location-header-parser-v2",
                )
                if axis == "location_city" and location_status == "review":
                    _add_qa(
                        output,
                        entity_type="project",
                        entity_id=project_id,
                        issue_code="suspicious_location_city",
                        severity="warning",
                        details={
                            "location_full": clean_scalar(project["location_full"]),
                            "location_city_raw": raw_value,
                            "normalized_value": None,
                            "policy_reason": location_reason,
                        },
                    )
        size = project["_size"]
        if size:
            area_status = str(size["status"])
            _add_claim(
                output,
                project_id=project_id,
                axis="area_bucket",
                raw_value=project["building_size_display"],
                normalized_value=size["slug"],
                claim_kind="derived_range",
                evidence_type="data_data_json",
                evidence_ref=f"project:{project_id}:building_size",
                evidence={
                    "source_slug": size["slug"],
                    "source_display": size["display"],
                    "min_sqft": size["min_sqft"],
                    "max_sqft": size["max_sqft"],
                    "is_open_ended": size["is_open_ended"],
                    "exact_area": False,
                },
                confidence=1.0 if area_status == "confirmed" else 0.7,
                status=area_status,
                rule_id="architizer-area-bucket-v1",
            )
            if area_status != "confirmed":
                _add_qa(
                    output,
                    entity_type="project",
                    entity_id=project_id,
                    issue_code="area_bucket_review",
                    severity="warning",
                    details=size,
                )


def import_images(
    output: sqlite3.Connection,
    projects: dict[int, dict[str, Any]],
) -> None:
    queued_assets: set[str] = set()
    for project_id in sorted(projects):
        project = projects[project_id]
        source_occurrences: list[tuple[str, int, Any, str]] = [
            ("cover", 0, project["cover_image_url"], "og:image:cover")
        ]
        source_occurrences.extend(
            ("gallery", ordinal, raw_url, "og:image:gallery")
            for ordinal, raw_url in enumerate(project["_gallery"])
        )
        for role, ordinal, raw_url, source_field in source_occurrences:
            raw_url_text = raw_url if isinstance(raw_url, str) else _json(raw_url)
            occurrence_id = _stable_id(
                "imgocc_", project_id, role, ordinal, raw_url_text
            )
            identity = image_identity(raw_url_text)
            if identity is None:
                output.execute(
                    """
                    INSERT INTO source_image_occurrences(
                        occurrence_id,source_project_id,role,ordinal,raw_url,
                        image_url_id,asset_id,parse_status,parse_error,source_field,
                        image_type
                    ) VALUES (?,?,?,?,?,NULL,NULL,'malformed',?,?,NULL)
                    """,
                    (
                        occurrence_id,
                        project_id,
                        role,
                        ordinal,
                        raw_url_text,
                        "URL is not a supported absolute Architizer image URL",
                        source_field,
                    ),
                )
                continue
            output.execute(
                """
                INSERT OR IGNORE INTO image_assets(
                    asset_id,asset_key,normalized_url,host,path,
                    is_placeholder_candidate,asset_key_version
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    identity.asset_id,
                    identity.asset_key,
                    identity.normalized_url,
                    identity.host,
                    identity.path,
                    int(identity.is_placeholder_candidate),
                    ASSET_KEY_VERSION,
                ),
            )
            image_url_id = _stable_id("imgurl_", raw_url_text)
            output.execute(
                """
                INSERT OR IGNORE INTO image_urls(
                    image_url_id,asset_id,raw_url,normalized_url,source_host
                ) VALUES (?,?,?,?,?)
                """,
                (
                    image_url_id,
                    identity.asset_id,
                    raw_url_text,
                    identity.normalized_url,
                    identity.host,
                ),
            )
            parse_status = (
                "placeholder_candidate"
                if identity.is_placeholder_candidate
                else "parsed"
            )
            output.execute(
                """
                INSERT INTO source_image_occurrences(
                    occurrence_id,source_project_id,role,ordinal,raw_url,
                    image_url_id,asset_id,parse_status,parse_error,source_field,
                    image_type
                ) VALUES (?,?,?,?,?,?,?,?,NULL,?,NULL)
                """,
                (
                    occurrence_id,
                    project_id,
                    role,
                    ordinal,
                    raw_url_text,
                    image_url_id,
                    identity.asset_id,
                    parse_status,
                    source_field,
                ),
            )
            queued_assets.add(identity.asset_id)
            if identity.is_placeholder_candidate:
                _add_qa(
                    output,
                    entity_type="image_occurrence",
                    entity_id=occurrence_id,
                    issue_code="placeholder_image_candidate",
                    severity="warning",
                    details={"raw_url": raw_url_text, "role": role},
                )
        for ordinal, raw_global_id in enumerate(project["_global_ids"]):
            raw_global_id_text = (
                raw_global_id
                if isinstance(raw_global_id, str)
                else _json(raw_global_id)
            )
            output.execute(
                """
                INSERT INTO project_image_global_id_occurrences(
                    global_id_occurrence_id,source_project_id,ordinal,raw_global_id,
                    alignment_status,alignment_reason
                ) VALUES (?,?,?,?,'unresolved',?)
                """,
                (
                    _stable_id("gidocc_", project_id, ordinal, raw_global_id_text),
                    project_id,
                    ordinal,
                    raw_global_id_text,
                    (
                        "Crawler did not preserve a positional or DOM relationship "
                        "between data-globalid values and og:image URLs."
                    ),
                ),
            )
    for asset_id in sorted(queued_assets):
        output.execute(
            """
            INSERT INTO image_work_queue(
                asset_id,phash_status,classification_status,queue_reason,
                network_calls_made
            ) VALUES (?,'pending','pending',?,0)
            """,
            (
                asset_id,
                "Future asset-keyed pHash and image classification; no image was downloaded.",
            ),
        )


def import_awards(
    output: sqlite3.Connection,
    awards: Sequence[dict[str, Any]],
    projects: dict[int, dict[str, Any]],
    firm_origins: dict[str, str],
) -> None:
    project_by_slug = {str(row["slug"]): project_id for project_id, row in projects.items()}
    composite_keys = [
        _json(
            [
                award["award_year"],
                award["award_track"],
                award["award_category"],
                award["award_tier"],
                award["project_slug"],
                award["firm_slug"],
            ]
        )
        for award in awards
    ]
    group_sizes = Counter(composite_keys)
    for award, composite_key in zip(awards, composite_keys):
        award_id = int(award["id"])
        output.execute(
            """
            INSERT INTO source_awards(
                source_award_id,award_year,award_track,award_category_raw,
                award_tier,project_slug_raw,firm_slug_raw,source_url,fetched_at,
                source_composite_key,logical_duplicate_group_size
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                award_id,
                award["award_year"],
                award["award_track"],
                award["award_category"],
                award["award_tier"],
                award["project_slug"],
                award["firm_slug"],
                award["source_url"],
                award["fetched_at"],
                composite_key,
                group_sizes[composite_key],
            ),
        )
        project_slug = clean_scalar(award["project_slug"])
        firm_slug = clean_scalar(award["firm_slug"])
        if project_slug:
            resolved_id = project_by_slug.get(project_slug)
            output.execute(
                """
                INSERT INTO award_entity_links(
                    source_award_id,target_type,raw_slug,
                    resolved_source_project_id,resolved_source_firm_slug,
                    link_status,evidence_field
                ) VALUES (?,'project',?,?,NULL,?,'project_slug')
                """,
                (
                    award_id,
                    project_slug,
                    resolved_id,
                    "resolved" if resolved_id is not None else "unresolved",
                ),
            )
            if resolved_id is None:
                _add_qa(
                    output,
                    entity_type="award",
                    entity_id=award_id,
                    issue_code="unresolved_award_project_slug",
                    severity="warning",
                    details={"project_slug": project_slug},
                )
        elif firm_slug:
            origin = firm_origins.get(firm_slug)
            link_status = (
                "resolved"
                if origin in {"crawled", "project_stub"}
                else "stub_only"
                if origin == "award_stub"
                else "unresolved"
            )
            output.execute(
                """
                INSERT INTO award_entity_links(
                    source_award_id,target_type,raw_slug,
                    resolved_source_project_id,resolved_source_firm_slug,
                    link_status,evidence_field
                ) VALUES (?,'firm',?,NULL,?,?, 'firm_slug')
                """,
                (
                    award_id,
                    firm_slug,
                    firm_slug if origin is not None else None,
                    link_status,
                ),
            )
            if link_status != "resolved":
                _add_qa(
                    output,
                    entity_type="award",
                    entity_id=award_id,
                    issue_code="award_firm_stub_only",
                    severity="warning",
                    details={"firm_slug": firm_slug, "record_origin": origin},
                )
        else:
            _add_qa(
                output,
                entity_type="award",
                entity_id=award_id,
                issue_code="award_without_entity_slug",
                severity="error",
                details={},
            )
        if award["award_category"]:
            _add_qa(
                output,
                entity_type="award",
                entity_id=award_id,
                issue_code="award_category_parser_unverified",
                severity="info",
                details={
                    "award_category_raw": award["award_category"],
                    "reason": "Ancestor text heuristic can contain entire winner-card text.",
                },
            )


def import_queue_audit(
    source: sqlite3.Connection,
    output: sqlite3.Connection,
    source_audit: dict[str, Any],
) -> None:
    for queue_name, statuses in sorted(source_audit["queue_counts"].items()):
        for status, count in sorted(statuses.items()):
            output.execute(
                "INSERT INTO source_queue_summary(queue_name,status,row_count) VALUES (?,?,?)",
                (queue_name, status, count),
            )
    for queue_name in ("pending_projects", "pending_firms"):
        for row in source.execute(
            f'SELECT * FROM "{queue_name}" WHERE status != "done" ORDER BY url'
        ):
            record = dict(row)
            _add_qa(
                output,
                entity_type=queue_name,
                entity_id=record["url"],
                issue_code="crawl_queue_not_done",
                severity="warning",
                details=record,
            )
    project_slugs = {
        str(row["slug"])
        for row in source.execute("SELECT slug FROM architizer_projects")
    }
    for row in source.execute(
        "SELECT * FROM pending_projects WHERE status='done' ORDER BY url"
    ):
        record = dict(row)
        slug = str(record["url"]).rstrip("/").rsplit("/", 1)[-1]
        if slug not in project_slugs:
            _add_qa(
                output,
                entity_type="pending_projects",
                entity_id=record["url"],
                issue_code="done_queue_without_project",
                severity="error",
                details=record,
            )
    firm_slugs = {
        str(row["slug"])
        for row in source.execute("SELECT slug FROM architizer_firms")
    }
    for row in source.execute(
        "SELECT * FROM pending_firms WHERE status='done' ORDER BY url"
    ):
        record = dict(row)
        slug = str(record["url"]).rstrip("/").rsplit("/", 1)[-1]
        if slug not in firm_slugs:
            _add_qa(
                output,
                entity_type="pending_firms",
                entity_id=record["url"],
                issue_code="done_queue_without_firm",
                severity="error",
                details=record,
            )


def _match_name(value: Any) -> str:
    return normalize_identity_text(value)


def _same_text(left: Any, right: Any) -> bool:
    a = normalize_identity_text(left)
    b = normalize_identity_text(right)
    return bool(a and b and a == b)


def _candidate_payload(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    candidate_kind: str,
) -> dict[str, Any]:
    left_id = int(left["id"])
    right_id = int(right["id"])
    if left_id > right_id:
        left, right = right, left
        left_id, right_id = right_id, left_id
    normalized_left = normalize_identity_text(left["name"])
    normalized_right = normalize_identity_text(right["name"])
    exact_name = int(_match_name(left["name"]) == _match_name(right["name"]))
    same_firm = int(
        bool(left["firm_slug"])
        and str(left["firm_slug"]) == str(right["firm_slug"])
    )
    same_country = int(
        _same_text(
            left["_location_country_value"], right["_location_country_value"]
        )
    )
    same_city = int(
        _same_text(left["_location_city_value"], right["_location_city_value"])
    )
    same_year = int(
        left["_year_value"] is not None
        and left["_year_value"] == right["_year_value"]
    )
    generic = int(
        is_generic_project_name(str(left["name"]))
        or is_generic_project_name(str(right["name"]))
    )
    phase = int(bool(left["_phase_marker"] or right["_phase_marker"]))
    similarity = 1.0 if exact_name else name_similarity(
        normalized_left, normalized_right
    )
    breakdown = {
        "name_similarity": round(similarity, 6),
        "exact_name": exact_name,
        "same_firm": same_firm,
        "same_country": same_country,
        "same_city": same_city,
        "same_nonnull_year": same_year,
        "generic_name": generic,
        "phase_marker": phase,
        "weights": {
            "name_similarity": 0.40,
            "same_firm": 0.25,
            "same_country": 0.10,
            "same_city": 0.10,
            "same_nonnull_year": 0.15,
        },
    }
    score = (
        similarity * 0.40
        + same_firm * 0.25
        + same_country * 0.10
        + same_city * 0.10
        + same_year * 0.15
    )
    strict = bool(
        exact_name
        and same_firm
        and same_country
        and same_city
        and same_year
        and not generic
        and not phase
    )
    return {
        "candidate_id": _stable_id("dupcand_", left_id, right_id),
        "left_project_id": left_id,
        "right_project_id": right_id,
        "candidate_kind": "strict" if strict else candidate_kind,
        "normalized_name_left": normalized_left,
        "normalized_name_right": normalized_right,
        "name_similarity": similarity,
        "exact_name": exact_name,
        "same_firm": same_firm,
        "same_country": same_country,
        "same_city": same_city,
        "same_nonnull_year": same_year,
        "generic_name": generic,
        "phase_marker": phase,
        "score": round(min(max(score, 0.0), 1.0), 6),
        "score_breakdown_json": _json(breakdown),
        "evidence_json": _json(
            {
                "left": {
                    "project_id": left_id,
                    "slug": left["slug"],
                    "name": left["name"],
                    "firm_slug": left["firm_slug"],
                    "location_country": left["_location_country_value"],
                    "location_city": left["_location_city_value"],
                    "completion_year": left["_year_value"],
                    "year_status": left["_year_status"],
                },
                "right": {
                    "project_id": right_id,
                    "slug": right["slug"],
                    "name": right["name"],
                    "firm_slug": right["firm_slug"],
                    "location_country": right["_location_country_value"],
                    "location_city": right["_location_city_value"],
                    "completion_year": right["_year_value"],
                    "year_status": right["_year_status"],
                },
                "membership_provenance": "source_project_rows",
                "phash_available": False,
                "vision_available": False,
            }
        ),
        "decision_status": "auto_clustered" if strict else "review",
        "rule_id": (
            "architizer-strict-cluster-v2"
            if strict
            else "architizer-exact-review-v1"
            if candidate_kind == "exact_review"
            else "architizer-fuzzy-review-v1"
        ),
    }


def build_duplicate_candidates(
    output: sqlite3.Connection,
    projects: dict[int, dict[str, Any]],
) -> UnionFind:
    accepted = {
        project_id: project
        for project_id, project in projects.items()
        if project["_acceptance_status"] == "accepted"
    }
    union_find = UnionFind(accepted)
    candidates: dict[tuple[int, int], dict[str, Any]] = {}

    exact_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for project in accepted.values():
        exact_groups[_match_name(project["name"])].append(project)
    for normalized_name, group in sorted(exact_groups.items()):
        if not normalized_name or len(group) < 2:
            continue
        group = sorted(group, key=lambda project: int(project["id"]))
        for index, left in enumerate(group):
            for right in group[index + 1 :]:
                payload = _candidate_payload(
                    left, right, candidate_kind="exact_review"
                )
                key = (payload["left_project_id"], payload["right_project_id"])
                candidates[key] = payload
                if payload["decision_status"] == "auto_clustered":
                    union_find.union(*key)

    fuzzy_blocks: dict[tuple[str, str, str, int], list[dict[str, Any]]] = (
        defaultdict(list)
    )
    for project in accepted.values():
        if (
            project["firm_slug"]
            and project["_location_country_value"]
            and project["_location_city_value"]
            and project["_year_value"] is not None
        ):
            key = (
                str(project["firm_slug"]),
                normalize_identity_text(project["_location_country_value"]),
                normalize_identity_text(project["_location_city_value"]),
                int(project["_year_value"]),
            )
            fuzzy_blocks[key].append(project)
    for _, group in sorted(fuzzy_blocks.items()):
        group = sorted(group, key=lambda project: int(project["id"]))
        for index, left in enumerate(group):
            for right in group[index + 1 :]:
                pair = tuple(sorted((int(left["id"]), int(right["id"]))))
                if pair in candidates:
                    continue
                similarity = name_similarity(
                    normalize_identity_text(left["name"]),
                    normalize_identity_text(right["name"]),
                )
                if similarity < 0.88:
                    continue
                payload = _candidate_payload(
                    left, right, candidate_kind="fuzzy_review"
                )
                payload["candidate_kind"] = "fuzzy_review"
                payload["decision_status"] = "review"
                payload["rule_id"] = "architizer-fuzzy-review-v1"
                candidates[pair] = payload

    for key in sorted(candidates):
        payload = candidates[key]
        output.execute(
            """
            INSERT INTO duplicate_candidates(
                candidate_id,left_project_id,right_project_id,candidate_kind,
                normalized_name_left,normalized_name_right,name_similarity,
                exact_name,same_firm,same_country,same_city,same_nonnull_year,
                generic_name,phase_marker,score,score_breakdown_json,evidence_json,
                decision_status,rule_id,phash_available,vision_available
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0)
            """,
            (
                payload["candidate_id"],
                payload["left_project_id"],
                payload["right_project_id"],
                payload["candidate_kind"],
                payload["normalized_name_left"],
                payload["normalized_name_right"],
                payload["name_similarity"],
                payload["exact_name"],
                payload["same_firm"],
                payload["same_country"],
                payload["same_city"],
                payload["same_nonnull_year"],
                payload["generic_name"],
                payload["phase_marker"],
                payload["score"],
                payload["score_breakdown_json"],
                payload["evidence_json"],
                payload["decision_status"],
                payload["rule_id"],
            ),
        )
    return union_find


def _project_content_score(project: dict[str, Any]) -> tuple[float, int]:
    cover = image_identity(str(project["cover_image_url"] or ""))
    score = 0.0
    score += 4.0 if clean_scalar(project["description"]) else 0.0
    score += 2.0 if project["_year_status"] == "confirmed" else 0.0
    score += (
        2.0
        if project["_location_country_value"] and project["_location_city_value"]
        else 0.0
    )
    score += 2.0 if cover is not None and not cover.is_placeholder_candidate else 0.0
    score += min(len(project["_gallery"]), 30) / 30.0
    score += 1.0 if 0 < len(project["_categories"]) <= 10 else 0.0
    return score, -int(project["id"])


def build_buildings(
    output: sqlite3.Connection,
    projects: dict[int, dict[str, Any]],
    union_find: UnionFind,
) -> dict[int, str]:
    project_to_building: dict[int, str] = {}
    for members in union_find.components():
        member_rows = [projects[project_id] for project_id in members]
        primary = max(member_rows, key=_project_content_score)
        building_id = _stable_id(
            "atz_bld_",
            CLUSTER_VERSION,
            ",".join(str(project_id) for project_id in members),
            length=24,
        )
        cluster_decision = (
            "strict_auto_cluster" if len(members) > 1 else "singleton"
        )
        rule_id = (
            "architizer-strict-cluster-v2"
            if len(members) > 1
            else "architizer-singleton-v1"
        )
        output.execute(
            """
            INSERT INTO buildings(
                building_id,identity_status,preferred_name,normalized_name,
                primary_project_id,project_count,cluster_decision,cluster_rule_id,
                source_firm_slug,location_country,location_country_status,
                location_city,location_city_status,completion_year,year_status,
                p_hash_used,vision_used
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                building_id,
                "provisional",
                str(primary["name"]),
                normalize_identity_text(primary["name"]),
                int(primary["id"]),
                len(members),
                cluster_decision,
                rule_id,
                str(primary["firm_slug"]),
                primary["_location_country_value"],
                primary["_location_country_status"],
                primary["_location_city_value"],
                primary["_location_city_status"],
                primary["_year_value"],
                primary["_year_status"],
                0,
                0,
            ),
        )
        for project_id in members:
            project_to_building[project_id] = building_id
            output.execute(
                """
                INSERT INTO building_projects(
                    building_id,source_project_id,is_primary,membership_status,
                    rule_id,evidence_json
                ) VALUES (?,?,?,?,?,?)
                """,
                (
                    building_id,
                    project_id,
                    int(project_id == int(primary["id"])),
                    "auto_clustered" if len(members) > 1 else "singleton",
                    rule_id,
                    _json(
                        {
                            "member_source_project_ids": members,
                            "source_identity_not_building_identity": True,
                            "strict_key": (
                                {
                                    "normalized_name": _match_name(primary["name"]),
                                    "firm_slug": primary["firm_slug"],
                                    "country": primary["_location_country_value"],
                                    "city": primary["_location_city_value"],
                                    "non_null_year": primary["completion_year"],
                                }
                                if len(members) > 1
                                else None
                            ),
                            "phash_used": False,
                            "vision_used": False,
                        }
                    ),
                ),
            )
        event_type = (
            "strict_cluster_created" if len(members) > 1 else "singleton_created"
        )
        output.execute(
            """
            INSERT INTO cluster_events(
                cluster_event_id,building_id,event_type,member_project_ids_json,
                rule_id,evidence_json
            ) VALUES (?,?,?,?,?,?)
            """,
            (
                _stable_id("clusterevt_", building_id, event_type),
                building_id,
                event_type,
                _json(members),
                rule_id,
                _json(
                    {
                        "source": "Architizer",
                        "provisional": True,
                        "no_cross_site_matching": True,
                        "no_phash": True,
                        "no_vision": True,
                    }
                ),
            ),
        )
    return project_to_building


SCALAR_FACET_AXES = {
    "program",
    "typology",
    "project_status",
    "completion_year",
    "location_country",
    "location_city",
    "area_bucket",
}


def resolve_facets(
    output: sqlite3.Connection,
    project_to_building: dict[int, str],
) -> None:
    claims_by_building_axis: dict[
        tuple[str, str], list[dict[str, Any]]
    ] = defaultdict(list)
    for row in output.execute(
        """
        SELECT * FROM attribute_claims
        WHERE normalized_value IS NOT NULL
          AND status IN ('confirmed','candidate','review')
        ORDER BY source_project_id,axis,normalized_value,claim_id
        """
    ):
        claim = dict(row)
        building_id = project_to_building.get(int(claim["source_project_id"]))
        if building_id is not None:
            claims_by_building_axis[(building_id, claim["axis"])].append(claim)

    for (building_id, axis), claims in sorted(claims_by_building_axis.items()):
        by_value: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for claim in claims:
            by_value[str(claim["normalized_value"])].append(claim)
        confirmed_values = {
            value
            for value, value_claims in by_value.items()
            if any(claim["status"] == "confirmed" for claim in value_claims)
        }
        has_scalar_conflict = (
            axis in SCALAR_FACET_AXES and len(confirmed_values) > 1
        )
        if has_scalar_conflict:
            _add_qa(
                output,
                entity_type="building_facet",
                entity_id=f"{building_id}:{axis}",
                issue_code="scalar_conflict_abstained",
                severity="warning",
                details={
                    "axis": axis,
                    "confirmed_values": sorted(confirmed_values),
                    "primary_value": None,
                },
            )
        for value, value_claims in sorted(by_value.items()):
            statuses = {str(claim["status"]) for claim in value_claims}
            if value in confirmed_values:
                facet_status = "conflict" if has_scalar_conflict else "confirmed"
            elif "candidate" in statuses:
                facet_status = "candidate"
            else:
                facet_status = "review"
            confidence = max(float(claim["confidence"]) for claim in value_claims)
            projects = {int(claim["source_project_id"]) for claim in value_claims}
            resolution_rule = (
                "abstain_on_multiple_confirmed_scalar_values"
                if facet_status == "conflict"
                else "confirmed_from_reviewed_direct_source_claim"
                if facet_status == "confirmed"
                else "preserve_candidate_without_default"
            )
            output.execute(
                """
                INSERT INTO building_facets(
                    building_id,axis,value,status,confidence,claim_count,
                    independent_project_count,resolution_rule
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    building_id,
                    axis,
                    value,
                    facet_status,
                    confidence,
                    len(value_claims),
                    len(projects),
                    resolution_rule,
                ),
            )
            for claim in value_claims:
                output.execute(
                    """
                    INSERT INTO building_facet_claims(
                        building_id,axis,value,claim_id
                    ) VALUES (?,?,?,?)
                    """,
                    (building_id, axis, value, claim["claim_id"]),
                )


def build_completeness(
    output: sqlite3.Connection,
    projects: dict[int, dict[str, Any]],
    project_to_building: dict[int, str],
) -> None:
    usable_image_projects = {
        int(row[0])
        for row in output.execute(
            """
            SELECT DISTINCT i.source_project_id
            FROM source_image_occurrences i
            JOIN image_assets a USING (asset_id)
            WHERE i.parse_status='parsed'
              AND a.is_placeholder_candidate=0
            """
        )
    }
    for project_id in sorted(projects):
        project = projects[project_id]
        values = {
            "firm": int(bool(project["firm_slug"])),
            "location": int(
                bool(
                    project["_location_country_value"]
                    and project["_location_city_value"]
                )
            ),
            "confirmed_year": int(project["_year_status"] == "confirmed"),
            "description": int(bool(clean_scalar(project["description"]))),
            "category": int(bool(project["_categories"])),
            "image": int(project_id in usable_image_projects),
        }
        score = sum(values.values()) / len(values)
        output.execute(
            """
            INSERT INTO project_completeness(
                source_project_id,has_firm,has_location,has_confirmed_year,
                has_description,has_category,has_image,completeness_score
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                project_id,
                values["firm"],
                values["location"],
                values["confirmed_year"],
                values["description"],
                values["category"],
                values["image"],
                score,
            ),
        )
    for building_id in sorted(set(project_to_building.values())):
        primary = output.execute(
            """
            SELECT pc.*
            FROM buildings b
            JOIN project_completeness pc
              ON pc.source_project_id=b.primary_project_id
            WHERE b.building_id=?
            """,
            (building_id,),
        ).fetchone()
        checks = {
            "firm": int(primary["has_firm"]),
            "location": int(primary["has_location"]),
            "confirmed_year": int(primary["has_confirmed_year"]),
            "description": int(primary["has_description"]),
            "category": int(primary["has_category"]),
            "image": int(primary["has_image"]),
        }
        missing = sorted(key for key, present in checks.items() if not present)
        output.execute(
            """
            INSERT INTO building_completeness(
                building_id,completeness_score,missing_fields_json
            ) VALUES (?,?,?)
            """,
            (
                building_id,
                sum(checks.values()) / len(checks),
                _json(missing),
            ),
        )


def _scalar(connection: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> Any:
    return connection.execute(sql, params).fetchone()[0]


def collect_metrics(output: sqlite3.Connection) -> dict[str, Any]:
    table_names = [
        row[0]
        for row in output.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    table_counts = {
        table: int(_scalar(output, f'SELECT COUNT(*) FROM "{table}"'))
        for table in table_names
    }
    project_total = table_counts["source_projects"]
    building_total = table_counts["buildings"]
    metrics: dict[str, Any] = {
        "table_counts": table_counts,
        "projects": {
            "total": project_total,
            "accepted": int(
                _scalar(
                    output,
                    "SELECT COUNT(*) FROM source_projects "
                    "WHERE acceptance_status='accepted'",
                )
            ),
            "excluded": int(
                _scalar(
                    output,
                    "SELECT COUNT(*) FROM source_projects "
                    "WHERE acceptance_status='excluded'",
                )
            ),
        },
        "buildings": {
            "total": building_total,
            "strict_auto_clusters": int(
                _scalar(
                    output,
                    "SELECT COUNT(*) FROM buildings "
                    "WHERE cluster_decision='strict_auto_cluster'",
                )
            ),
            "clustered_source_projects": int(
                _scalar(
                    output,
                    "SELECT COUNT(*) FROM building_projects "
                    "WHERE membership_status='auto_clustered'",
                )
            ),
        },
        "firms": {
            str(row["record_origin"]): int(row["n"])
            for row in output.execute(
                "SELECT record_origin,COUNT(*) AS n FROM source_firms "
                "GROUP BY record_origin ORDER BY record_origin"
            )
        },
        "categories": {
            "vocabulary": table_counts["source_categories"],
            "raw_occurrences": table_counts["project_category_occurrences"],
            "mapping_status": {
                str(row["mapping_status"]): int(row["n"])
                for row in output.execute(
                    "SELECT mapping_status,COUNT(*) AS n FROM source_categories "
                    "GROUP BY mapping_status ORDER BY mapping_status"
                )
            },
            "claim_status": {
                str(row["status"]): int(row["n"])
                for row in output.execute(
                    "SELECT status,COUNT(*) AS n FROM attribute_claims "
                    "WHERE category_occurrence_id IS NOT NULL "
                    "GROUP BY status ORDER BY status"
                )
            },
        },
        "images": {
            "raw_occurrences": table_counts["source_image_occurrences"],
            "assets": table_counts["image_assets"],
            "raw_urls": table_counts["image_urls"],
            "global_id_occurrences": table_counts[
                "project_image_global_id_occurrences"
            ],
            "malformed_urls": int(
                _scalar(
                    output,
                    "SELECT COUNT(*) FROM source_image_occurrences "
                    "WHERE parse_status='malformed'",
                )
            ),
            "placeholder_occurrences": int(
                _scalar(
                    output,
                    "SELECT COUNT(*) FROM source_image_occurrences "
                    "WHERE parse_status='placeholder_candidate'",
                )
            ),
        },
        "awards": {
            "rows": table_counts["source_awards"],
            "logical_duplicate_rows": int(
                _scalar(
                    output,
                    "SELECT COUNT(*) FROM source_awards "
                    "WHERE logical_duplicate_group_size > 1",
                )
            ),
            "unresolved_links": int(
                _scalar(
                    output,
                    "SELECT COUNT(*) FROM award_entity_links "
                    "WHERE link_status != 'resolved'",
                )
            ),
        },
        "duplicate_candidates": {
            f"{row['candidate_kind']}:{row['decision_status']}": int(row["n"])
            for row in output.execute(
                "SELECT candidate_kind,decision_status,COUNT(*) AS n "
                "FROM duplicate_candidates "
                "GROUP BY candidate_kind,decision_status "
                "ORDER BY candidate_kind,decision_status"
            )
        },
        "facets": {
            str(row["status"]): int(row["n"])
            for row in output.execute(
                "SELECT status,COUNT(*) AS n FROM building_facets "
                "GROUP BY status ORDER BY status"
            )
        },
        "qa_open_by_code": {
            str(row["issue_code"]): int(row["n"])
            for row in output.execute(
                "SELECT issue_code,COUNT(*) AS n FROM qa_issues "
                "WHERE status='open' GROUP BY issue_code ORDER BY issue_code"
            )
        },
        "coverage": {},
    }
    for column in (
        "source_firm_slug",
        "location_country_raw",
        "location_city_raw",
        "completion_year_raw",
        "building_size_slug",
        "description",
        "description_short",
    ):
        present = int(
            _scalar(
                output,
                f"SELECT COUNT(*) FROM source_projects "
                f"WHERE {column} IS NOT NULL AND TRIM(CAST({column} AS TEXT)) != ''",
            )
        )
        metrics["coverage"][column] = {
            "present": present,
            "total": project_total,
            "ratio": round(present / project_total, 6) if project_total else 0.0,
        }
    metrics["coverage"]["confirmed_program_buildings"] = {
        "present": int(
            _scalar(
                output,
                "SELECT COUNT(DISTINCT building_id) FROM building_facets "
                "WHERE axis='program' AND status='confirmed'",
            )
        ),
        "total": building_total,
    }
    metrics["coverage"]["confirmed_typology_buildings"] = {
        "present": int(
            _scalar(
                output,
                "SELECT COUNT(DISTINCT building_id) FROM building_facets "
                "WHERE axis='typology' AND status='confirmed'",
            )
        ),
        "total": building_total,
    }
    return metrics


def validate_output(
    output: sqlite3.Connection,
    *,
    expected_projects: int,
    expected_awards: int,
    expected_source_sha: str,
    source_sha_after: str,
) -> dict[str, Any]:
    integrity_rows = [row[0] for row in output.execute("PRAGMA integrity_check")]
    foreign_key_rows = [tuple(row) for row in output.execute("PRAGMA foreign_key_check")]
    expected_category_occurrences = int(
        _scalar(
            output,
            "SELECT COALESCE(SUM(category_occurrence_count),0) FROM source_projects",
        )
    )
    expected_image_occurrences = int(
        _scalar(
            output,
            "SELECT COALESCE(SUM(1 + gallery_occurrence_count),0) "
            "FROM source_projects",
        )
    )
    expected_global_id_occurrences = int(
        _scalar(
            output,
            "SELECT COALESCE(SUM(image_global_id_occurrence_count),0) "
            "FROM source_projects",
        )
    )
    checks: dict[str, Any] = {
        "integrity_check": integrity_rows,
        "foreign_key_violations": len(foreign_key_rows),
        "source_sha_before": expected_source_sha,
        "source_sha_after": source_sha_after,
        "source_sha_unchanged": source_sha_after == expected_source_sha,
        "source_projects_expected": expected_projects,
        "source_projects_actual": int(
            _scalar(output, "SELECT COUNT(*) FROM source_projects")
        ),
        "source_project_accounting_error": int(
            _scalar(
                output,
                "SELECT COUNT(*) FROM source_projects "
                "WHERE acceptance_status NOT IN ('accepted','excluded') "
                "OR (acceptance_status='excluded' AND exclusion_reason IS NULL) "
                "OR (acceptance_status='accepted' AND exclusion_reason IS NOT NULL)",
            )
        ),
        "accepted_membership_error": int(
            _scalar(
                output,
                """
                SELECT COUNT(*) FROM (
                    SELECT p.source_project_id,COUNT(bp.building_id) AS n
                    FROM source_projects p
                    LEFT JOIN building_projects bp USING (source_project_id)
                    WHERE p.acceptance_status='accepted'
                    GROUP BY p.source_project_id
                    HAVING n != 1
                )
                """,
            )
        ),
        "excluded_membership_error": int(
            _scalar(
                output,
                """
                SELECT COUNT(*)
                FROM source_projects p
                JOIN building_projects bp USING (source_project_id)
                WHERE p.acceptance_status='excluded'
                """,
            )
        ),
        "building_project_count_error": int(
            _scalar(
                output,
                """
                SELECT COUNT(*) FROM (
                    SELECT b.building_id,b.project_count,COUNT(bp.source_project_id) AS n
                    FROM buildings b
                    LEFT JOIN building_projects bp USING (building_id)
                    GROUP BY b.building_id
                    HAVING b.project_count != n
                )
                """,
            )
        ),
        "building_primary_count_error": int(
            _scalar(
                output,
                """
                SELECT COUNT(*) FROM (
                    SELECT b.building_id,SUM(bp.is_primary) AS n
                    FROM buildings b JOIN building_projects bp USING (building_id)
                    GROUP BY b.building_id
                    HAVING n != 1
                )
                """,
            )
        ),
        "category_occurrences_expected": expected_category_occurrences,
        "category_occurrences_actual": int(
            _scalar(output, "SELECT COUNT(*) FROM project_category_occurrences")
        ),
        "category_occurrences_without_policy": int(
            _scalar(
                output,
                """
                SELECT COUNT(*)
                FROM project_category_occurrences o
                LEFT JOIN category_mappings m USING (category_id)
                WHERE m.mapping_id IS NULL
                """,
            )
        ),
        "category_master_occurrence_count_error": int(
            _scalar(
                output,
                """
                SELECT COUNT(*) FROM (
                    SELECT c.category_id,c.source_occurrence_count,
                           COUNT(o.occurrence_id) AS actual_count
                    FROM source_categories c
                    LEFT JOIN project_category_occurrences o USING (category_id)
                    GROUP BY c.category_id
                    HAVING c.source_occurrence_count != actual_count
                )
                """,
            )
        ),
        "category_master_project_count_error": int(
            _scalar(
                output,
                """
                SELECT COUNT(*) FROM (
                    SELECT c.category_id,c.project_count,
                           COUNT(DISTINCT o.source_project_id) AS actual_count
                    FROM source_categories c
                    LEFT JOIN project_category_occurrences o USING (category_id)
                    GROUP BY c.category_id
                    HAVING c.project_count != actual_count
                )
                """,
            )
        ),
        "image_occurrences_expected": expected_image_occurrences,
        "image_occurrences_actual": int(
            _scalar(output, "SELECT COUNT(*) FROM source_image_occurrences")
        ),
        "global_id_occurrences_expected": expected_global_id_occurrences,
        "global_id_occurrences_actual": int(
            _scalar(
                output,
                "SELECT COUNT(*) FROM project_image_global_id_occurrences",
            )
        ),
        "parsed_image_without_asset": int(
            _scalar(
                output,
                "SELECT COUNT(*) FROM source_image_occurrences "
                "WHERE parse_status != 'malformed' "
                "AND (asset_id IS NULL OR image_url_id IS NULL)",
            )
        ),
        "article_category_image_type_propagation_rows": int(
            _scalar(
                output,
                "SELECT COUNT(*) FROM source_image_occurrences "
                "WHERE image_type IS NOT NULL",
            )
        ),
        "auto_cluster_rule_violations": int(
            _scalar(
                output,
                """
                SELECT COUNT(*) FROM duplicate_candidates
                WHERE decision_status='auto_clustered'
                  AND NOT (
                    candidate_kind='strict' AND exact_name=1 AND same_firm=1
                    AND same_country=1 AND same_city=1 AND same_nonnull_year=1
                    AND generic_name=0 AND phase_marker=0
                  )
                """,
            )
        ),
        "fuzzy_candidate_auto_merge_rows": int(
            _scalar(
                output,
                "SELECT COUNT(*) FROM duplicate_candidates "
                "WHERE candidate_kind='fuzzy_review' "
                "AND decision_status!='review'",
            )
        ),
        "review_candidate_same_building_rows": int(
            _scalar(
                output,
                """
                SELECT COUNT(*)
                FROM duplicate_candidates d
                JOIN building_projects l ON l.source_project_id=d.left_project_id
                JOIN building_projects r ON r.source_project_id=d.right_project_id
                WHERE d.decision_status='review'
                  AND l.building_id=r.building_id
                """,
            )
        ),
        "confirmed_claim_without_evidence": int(
            _scalar(
                output,
                "SELECT COUNT(*) FROM attribute_claims "
                "WHERE status='confirmed' AND "
                "(normalized_value IS NULL OR evidence_ref='' OR evidence_json='{}')",
            )
        ),
        "confirmed_facet_without_claim": int(
            _scalar(
                output,
                """
                SELECT COUNT(*)
                FROM building_facets f
                LEFT JOIN building_facet_claims c
                  ON c.building_id=f.building_id
                 AND c.axis=f.axis AND c.value=f.value
                WHERE f.status='confirmed' AND c.claim_id IS NULL
                """,
            )
        ),
        "overloaded_project_confirmed_category_claims": int(
            _scalar(
                output,
                """
                SELECT COUNT(*)
                FROM attribute_claims c
                WHERE c.category_occurrence_id IS NOT NULL
                  AND c.status='confirmed'
                  AND EXISTS (
                      SELECT 1
                      FROM qa_issues q
                      WHERE q.entity_type='project'
                        AND q.entity_id=CAST(c.source_project_id AS TEXT)
                        AND q.issue_code='category_metadata_overloaded'
                  )
                """,
            )
        ),
        "scalar_conflict_export_primary_rows": int(
            _scalar(
                output,
                """
                SELECT COUNT(*)
                FROM v_architizer_buildings_export e
                WHERE (
                    e.program_primary IS NOT NULL
                    AND EXISTS (
                        SELECT 1 FROM building_facets f
                        WHERE f.building_id=e.building_id
                          AND f.axis='program' AND f.status='conflict'
                    )
                ) OR (
                    e.typology_primary IS NOT NULL
                    AND EXISTS (
                        SELECT 1 FROM building_facets f
                        WHERE f.building_id=e.building_id
                          AND f.axis='typology' AND f.status='conflict'
                    )
                ) OR (
                    e.area_bucket IS NOT NULL
                    AND EXISTS (
                        SELECT 1 FROM building_facets f
                        WHERE f.building_id=e.building_id
                          AND f.axis='area_bucket' AND f.status='conflict'
                    )
                ) OR (
                    e.project_status IS NOT NULL
                    AND EXISTS (
                        SELECT 1 FROM building_facets f
                        WHERE f.building_id=e.building_id
                          AND f.axis='project_status' AND f.status='conflict'
                    )
                )
                """,
            )
        ),
        "material_claims_without_source_evidence": int(
            _scalar(
                output,
                "SELECT COUNT(*) FROM attribute_claims WHERE axis='material'",
            )
        ),
        "export_rows": int(
            _scalar(output, "SELECT COUNT(*) FROM v_architizer_buildings_export")
        ),
        "building_rows": int(_scalar(output, "SELECT COUNT(*) FROM buildings")),
        "export_duplicate_building_ids": int(
            _scalar(
                output,
                """
                SELECT COUNT(*) FROM (
                    SELECT building_id,COUNT(*) AS n
                    FROM v_architizer_buildings_export
                    GROUP BY building_id HAVING n != 1
                )
                """,
            )
        ),
        "award_rows_expected": expected_awards,
        "award_rows_actual": int(
            _scalar(output, "SELECT COUNT(*) FROM source_awards")
        ),
        "award_rows_without_link_accounting": int(
            _scalar(
                output,
                """
                SELECT COUNT(*)
                FROM source_awards a
                LEFT JOIN award_entity_links l USING (source_award_id)
                WHERE l.source_award_id IS NULL
                  AND (a.project_slug_raw IS NOT NULL OR a.firm_slug_raw IS NOT NULL)
                """,
            )
        ),
    }
    failed: dict[str, Any] = {}
    if integrity_rows != ["ok"]:
        failed["integrity_check"] = integrity_rows
    if foreign_key_rows:
        failed["foreign_key_violations"] = foreign_key_rows[:20]
    equality_pairs = (
        ("source_projects_expected", "source_projects_actual"),
        ("category_occurrences_expected", "category_occurrences_actual"),
        ("image_occurrences_expected", "image_occurrences_actual"),
        ("global_id_occurrences_expected", "global_id_occurrences_actual"),
        ("export_rows", "building_rows"),
        ("award_rows_expected", "award_rows_actual"),
    )
    for expected_key, actual_key in equality_pairs:
        if checks[expected_key] != checks[actual_key]:
            failed[f"{expected_key}_vs_{actual_key}"] = [
                checks[expected_key],
                checks[actual_key],
            ]
    zero_keys = (
        "source_project_accounting_error",
        "accepted_membership_error",
        "excluded_membership_error",
        "building_project_count_error",
        "building_primary_count_error",
        "category_occurrences_without_policy",
        "category_master_occurrence_count_error",
        "category_master_project_count_error",
        "parsed_image_without_asset",
        "article_category_image_type_propagation_rows",
        "auto_cluster_rule_violations",
        "fuzzy_candidate_auto_merge_rows",
        "review_candidate_same_building_rows",
        "confirmed_claim_without_evidence",
        "confirmed_facet_without_claim",
        "overloaded_project_confirmed_category_claims",
        "scalar_conflict_export_primary_rows",
        "material_claims_without_source_evidence",
        "export_duplicate_building_ids",
        "award_rows_without_link_accounting",
    )
    for key in zero_keys:
        if checks[key] != 0:
            failed[key] = checks[key]
    if not checks["source_sha_unchanged"]:
        failed["source_sha_unchanged"] = [
            checks["source_sha_before"],
            checks["source_sha_after"],
        ]
    checks["passed"] = not failed
    checks["failures"] = failed
    if failed:
        raise BuildError(f"curated validation failed: {_json(failed)}")
    return checks


def logical_database_digest(path: Path) -> str:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    digest = hashlib.sha256()
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    for table in tables:
        columns = [
            row["name"] for row in connection.execute(f'PRAGMA table_info("{table}")')
        ]
        digest.update(f"TABLE\0{table}\0{','.join(columns)}\n".encode("utf-8"))
        order = ",".join(f'"{column}"' for column in columns)
        for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY {order}'):
            digest.update(
                (_json([row[column] for column in columns]) + "\n").encode("utf-8")
            )
    connection.close()
    return digest.hexdigest().upper()


def add_global_qa(
    source: sqlite3.Connection,
    output: sqlite3.Connection,
    projects: dict[int, dict[str, Any]],
) -> None:
    budgets = [
        project["budget"]
        for project in projects.values()
        if project["budget"] is not None
    ]
    sentinel_counts = Counter(str(value) for value in budgets if value <= 1)
    _add_qa(
        output,
        entity_type="build",
        entity_id="source_budget",
        issue_code="budget_semantics_unverified",
        severity="warning",
        details={
            "selected_non_null_count": len(budgets),
            "selected_sentinel_or_nonpositive": sum(
                count for value, count in sentinel_counts.items()
            ),
            "selected_sentinel_counts": dict(sorted(sentinel_counts.items())),
            "reason": (
                "The source contains only a small set of repeated numeric values, "
                "including -2, -1, 0, and 1. No exact currency/amount semantics "
                "are asserted."
            ),
        },
    )
    total_gallery = sum(len(project["_gallery"]) for project in projects.values())
    total_global_ids = sum(
        len(project["_global_ids"]) for project in projects.values()
    )
    equal_length = sum(
        len(project["_gallery"]) == len(project["_global_ids"])
        for project in projects.values()
    )
    _add_qa(
        output,
        entity_type="build",
        entity_id="image_attribution_alignment",
        issue_code="image_global_id_alignment_unverified",
        severity="warning",
        details={
            "selected_projects": len(projects),
            "gallery_occurrences": total_gallery,
            "global_id_occurrences": total_global_ids,
            "projects_with_equal_lengths": equal_length,
            "positional_join_performed": False,
        },
    )
    _add_qa(
        output,
        entity_type="build",
        entity_id="credit_team_provenance",
        issue_code="credit_team_fields_unavailable",
        severity="info",
        details={
            "reason": (
                "The crawler database has no credit/team attribution table or "
                "payload. Firm membership and unaligned image global IDs are the "
                "only identity/attribution evidence preserved."
            )
        },
    )
    project_stub_count = int(
        _scalar(
            output,
            "SELECT COUNT(*) FROM source_firms WHERE record_origin='project_stub'",
        )
    )
    _add_qa(
        output,
        entity_type="build",
        entity_id="firm_index_coverage",
        issue_code="project_firm_missing_from_crawled_index",
        severity="warning",
        details={
            "project_stub_firms": project_stub_count,
            "behavior": "preserved as explicit source firm stubs",
        },
    )
    source_count_mismatches = int(
        _scalar(
            source,
            """
            SELECT COUNT(*) FROM (
                SELECT f.slug
                FROM architizer_firms f
                LEFT JOIN architizer_projects p ON p.firm_slug=f.slug
                GROUP BY f.slug
                HAVING COALESCE(f.project_count_seen,0) != COUNT(p.id)
            )
            """,
        )
    )
    _add_qa(
        output,
        entity_type="build",
        entity_id="firm_project_count_seen",
        issue_code="firm_project_count_seen_not_relation_count",
        severity="info",
        details={
            "source_firms_with_mismatch": source_count_mismatches,
            "reason": (
                "project_count_seen is a page-anchor observation, not a foreign-key "
                "cardinality."
            ),
        },
    )
    protocol_relative_social = int(
        _scalar(
            output,
            "SELECT COUNT(*) FROM firm_social_links WHERE raw_url LIKE '//%'",
        )
    )
    _add_qa(
        output,
        entity_type="build",
        entity_id="firm_social_links",
        issue_code="firm_social_link_parser_heuristic",
        severity="info",
        details={
            "protocol_relative_urls": protocol_relative_social,
            "raw_values_preserved": True,
        },
    )
    short_160 = sum(
        len(str(project["description_short"])) == 160
        for project in projects.values()
        if project["description_short"] is not None
    )
    _add_qa(
        output,
        entity_type="build",
        entity_id="description_short",
        issue_code="description_short_source_cap",
        severity="info",
        details={
            "selected_exactly_160_chars": short_160,
            "behavior": "preserved as OG snippet; never substituted for full description",
        },
    )
    duplicate_awards = int(
        _scalar(
            output,
            "SELECT COUNT(*) FROM source_awards "
            "WHERE logical_duplicate_group_size > 1",
        )
    )
    _add_qa(
        output,
        entity_type="build",
        entity_id="award_logical_duplicates",
        issue_code="award_nullable_unique_duplicates",
        severity="warning",
        details={
            "raw_rows_in_duplicate_groups": duplicate_awards,
            "raw_rows_preserved": True,
            "derived_key": (
                "year|track|category-or-null|tier|project-or-null|firm-or-null"
            ),
        },
    )
    placeholder_only_projects = int(
        _scalar(
            output,
            """
            SELECT COUNT(*) FROM (
                SELECT p.source_project_id,
                       COUNT(DISTINCT i.asset_id) AS assets,
                       MIN(a.is_placeholder_candidate) AS all_placeholder
                FROM source_projects p
                JOIN source_image_occurrences i USING (source_project_id)
                JOIN image_assets a USING (asset_id)
                GROUP BY p.source_project_id
                HAVING assets >= 1 AND all_placeholder = 1
            )
            """,
        )
    )
    _add_qa(
        output,
        entity_type="build",
        entity_id="placeholder_only_projects",
        issue_code="placeholder_only_project_images",
        severity="warning",
        details={
            "selected_project_count": placeholder_only_projects,
            "publishability_inferred": False,
        },
    )


def _deterministic_timestamp(source: sqlite3.Connection) -> str:
    values = []
    for table in ("architizer_projects", "architizer_firms", "architizer_awards"):
        value = _scalar(source, f"SELECT MAX(fetched_at) FROM {table}")
        if value is not None:
            values.append(str(value))
    return max(values) if values else "1970-01-01 00:00:00"


def materialize_database(
    *,
    source_path: Path,
    target_path: Path,
    limit: Optional[int],
    expected_sha256: str,
    expected_size: int,
) -> dict[str, Any]:
    source = open_source(source_path)
    try:
        source_audit = validate_source(
            source,
            source_path,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
        )
        source.execute("BEGIN")
        projects_raw, source_project_total = select_projects(source, limit)
        awards = select_awards(source, projects_raw, full=limit is None)
        build_id = _stable_id(
            "atz_build_",
            source_audit["sha256"],
            limit,
            BUILDER_VERSION,
            SCHEMA_VERSION,
            POLICY_VERSION,
            TAXONOMY_VERSION,
            CLUSTER_VERSION,
            length=24,
        )
        output = create_output(target_path)
        try:
            output.execute(
                """
                INSERT INTO build_runs(
                    build_id,builder_version,schema_version,policy_version,
                    taxonomy_version,asset_key_version,cluster_version,
                    resolver_version,selection_version,source_sha256,
                    source_size_bytes,selected_project_limit,
                    selected_project_count,deterministic_timestamp,
                    external_calls,validation_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'none','{}')
                """,
                (
                    build_id,
                    BUILDER_VERSION,
                    SCHEMA_VERSION,
                    POLICY_VERSION,
                    TAXONOMY_VERSION,
                    ASSET_KEY_VERSION,
                    CLUSTER_VERSION,
                    RESOLVER_VERSION,
                    SELECTION_VERSION,
                    source_audit["sha256"],
                    source_audit["size_bytes"],
                    limit,
                    len(projects_raw),
                    _deterministic_timestamp(source),
                ),
            )
            firm_origins = import_firms(
                source,
                output,
                projects_raw,
                awards,
                full=limit is None,
            )
            projects = import_projects(output, projects_raw)
            import_taxonomy_and_claims(output, projects)
            import_images(output, projects)
            import_awards(output, awards, projects, firm_origins)
            import_queue_audit(source, output, source_audit)
            union_find = build_duplicate_candidates(output, projects)
            project_to_building = build_buildings(output, projects, union_find)
            resolve_facets(output, project_to_building)
            build_completeness(output, projects, project_to_building)
            add_global_qa(source, output, projects)
            _assert_source_sidecars_clean(source_path)
            source_sha_after = sha256_file(source_path)
            output.execute(
                """
                INSERT INTO source_snapshots(
                    build_id,source_path,source_sha256_before,source_sha256_after,
                    source_size_bytes,source_wal_size_bytes,source_journal_size_bytes,
                    quick_check,integrity_check,
                    foreign_key_violations,query_only,source_table_counts_json,
                    source_queue_counts_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    build_id,
                    source_audit["path"],
                    source_audit["sha256"],
                    source_sha_after,
                    source_audit["size_bytes"],
                    source_audit["sidecar_sizes"]["wal"],
                    source_audit["sidecar_sizes"]["journal"],
                    source_audit["quick_check"],
                    source_audit["integrity_check"],
                    source_audit["foreign_key_violations"],
                    source_audit["query_only"],
                    _json(source_audit["table_counts"]),
                    _json(source_audit["queue_counts"]),
                ),
            )
            metrics = collect_metrics(output)
            for metric_name, metric_value in sorted(metrics.items()):
                output.execute(
                    "INSERT INTO build_metrics(metric_name,metric_value_json) "
                    "VALUES (?,?)",
                    (metric_name, _json(metric_value)),
                )
            validation = validate_output(
                output,
                expected_projects=len(projects_raw),
                expected_awards=len(awards),
                expected_source_sha=source_audit["sha256"],
                source_sha_after=source_sha_after,
            )
            output.execute(
                "UPDATE build_runs SET validation_json=? WHERE build_id=?",
                (_json(validation), build_id),
            )
            output.commit()
            output.execute("ANALYZE")
            output.execute("PRAGMA optimize")
            output.commit()
            output.execute("VACUUM")
            output.commit()
            final_validation = validate_output(
                output,
                expected_projects=len(projects_raw),
                expected_awards=len(awards),
                expected_source_sha=source_audit["sha256"],
                source_sha_after=sha256_file(source_path),
            )
            output.execute(
                "UPDATE build_runs SET validation_json=? WHERE build_id=?",
                (_json(final_validation), build_id),
            )
            output.commit()
        finally:
            output.close()
        source.execute("ROLLBACK")
    finally:
        source.close()
    return {
        "build_id": build_id,
        "source_audit": source_audit,
        "source_project_total": source_project_total,
        "selected_projects": len(projects_raw),
        "selected_awards": len(awards),
        "metrics": metrics,
        "validation": final_validation,
        "database_sha256": sha256_file(target_path),
        "database_logical_sha256": logical_database_digest(target_path),
        "database_size_bytes": target_path.stat().st_size,
    }


def _report_samples(database_path: Path, limit: int = 10) -> list[dict[str, Any]]:
    connection = sqlite3.connect(
        database_path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    rows: list[dict[str, Any]] = []
    for project in connection.execute(
        """
        SELECT
            p.source_project_id,p.slug,p.name,p.source_firm_slug,
            p.location_city_raw,p.location_country_raw,p.completion_year_raw,
            p.year_claim_status,p.constr_status_raw,p.category_occurrence_count,
            p.gallery_occurrence_count,p.acceptance_status,p.exclusion_reason,
            bp.building_id,bp.membership_status,
            pc.completeness_score
        FROM source_projects p
        LEFT JOIN building_projects bp USING (source_project_id)
        JOIN project_completeness pc USING (source_project_id)
        ORDER BY p.source_project_id
        LIMIT ?
        """,
        (limit,),
    ):
        record = dict(project)
        record["categories"] = [
            row[0]
            for row in connection.execute(
                "SELECT raw_value FROM project_category_occurrences "
                "WHERE source_project_id=? ORDER BY ordinal",
                (record["source_project_id"],),
            )
        ]
        record["facets"] = [
            dict(row)
            for row in connection.execute(
                """
                SELECT axis,value,status,confidence
                FROM building_facets
                WHERE building_id=?
                ORDER BY axis,value
                """,
                (record["building_id"],),
            )
        ] if record["building_id"] else []
        rows.append(record)
    connection.close()
    return rows


def render_report(
    *,
    result: dict[str, Any],
    database_path: Path,
    output_path: Path,
    limit: Optional[int],
    elapsed_seconds: float,
    deterministic_verified: bool,
    deterministic_shadow_sha256: Optional[str],
) -> str:
    metrics = result["metrics"]
    validation = result["validation"]
    samples = _report_samples(database_path, 10)
    stage = "full" if limit is None else f"N{limit}"
    lines = [
        f"# Architizer curated SQLite v1 — {stage}",
        "",
        "## Build lineage",
        "",
        f"- Build ID: `{result['build_id']}`",
        f"- Builder/schema/policy: `{BUILDER_VERSION}` / `{SCHEMA_VERSION}` / `{POLICY_VERSION}`",
        f"- Taxonomy/cluster/resolver: `{TAXONOMY_VERSION}` / `{CLUSTER_VERSION}` / `{RESOLVER_VERSION}`",
        f"- Source: `{result['source_audit']['path']}` opened with SQLite `mode=ro&immutable=1` and `query_only=ON`",
        f"- Source bytes: `{result['source_audit']['size_bytes']:,}`",
        f"- Source sidecars (WAL/SHM/journal bytes): "
        f"`{result['source_audit']['sidecar_sizes']['wal']}` / "
        f"`{result['source_audit']['sidecar_sizes']['shm']}` / "
        f"`{result['source_audit']['sidecar_sizes']['journal']}`",
        f"- Source SHA-256 before/after: `{validation['source_sha_before']}` / `{validation['source_sha_after']}`",
        f"- Output SHA-256: `{result['database_sha256']}`",
        f"- Output logical SHA-256: `{result['database_logical_sha256']}`",
        f"- Output bytes: `{result['database_size_bytes']:,}`",
        f"- Deterministic rerun: `{'PASS' if deterministic_verified else 'NOT REQUESTED'}`"
        + (
            f" (`{deterministic_shadow_sha256}`)"
            if deterministic_shadow_sha256
            else ""
        ),
        f"- Elapsed: `{elapsed_seconds:.3f}s`",
        "- Network/API/LLM/Vision/embedding/Neon/R2 cost: `$0`",
        "",
        "## Read-only source audit",
        "",
        f"- `quick_check={result['source_audit']['quick_check']}`; "
        f"`integrity_check={result['source_audit']['integrity_check']}`; "
        f"`foreign_key_check={result['source_audit']['foreign_key_violations']}`",
        f"- Source tables: `{_json(result['source_audit']['table_counts'])}`",
        f"- Crawl queues: `{_json(result['source_audit']['queue_counts'])}`",
        "- The source declares no foreign keys; curated relationship checks are explicit.",
        "- Project completion date is year precision, size is a sqft bucket, and budget semantics remain unverified.",
        "- Category arrays are ordered `article:tag` occurrences; award categories are separate heuristic payloads.",
        "- Gallery URLs and image global IDs have no positional join evidence.",
        "",
        "## Curated counts",
        "",
        f"- Projects: `{metrics['projects']}`",
        f"- Buildings: `{metrics['buildings']}`",
        f"- Firms by origin: `{metrics['firms']}`",
        f"- Categories: `{metrics['categories']}`",
        f"- Images: `{metrics['images']}`",
        f"- Awards: `{metrics['awards']}`",
        f"- Duplicate candidates: `{metrics['duplicate_candidates']}`",
        f"- Facets: `{metrics['facets']}`",
        "",
        "## Core coverage",
        "",
    ]
    for field, value in metrics["coverage"].items():
        lines.append(f"- `{field}`: `{value}`")
    lines.extend(
        [
            "",
            "## Policy",
            "",
            "- Raw project, firm, award, category, text, image URL, and global-ID occurrences are retained before normalization.",
            "- Broad category parents are candidates; reviewed leaf categories can create direct program/typology/work-type claims.",
            "- Parent and leaf tags share one project/category-path evidence group and never count as two independent sources.",
            "- Conflicting confirmed scalar values are kept as conflict facets; the export primary scalar remains `NULL`.",
            "- Material is never inferred because this source snapshot has no material category evidence.",
            "- Four non-project `global_id` rows are preserved but excluded from provisional building membership with an explicit reason.",
            "- Auto-clustering requires normalized name, stable firm slug, country, city, and identical non-null year, plus non-generic/non-phase names.",
            "- Exact weak and fuzzy matches remain in `v_duplicate_review_queue`; pHash and Vision are explicitly absent.",
            "- Project/category hints are never propagated to gallery-level image classifications.",
            "",
            "## Validation",
            "",
            "```json",
            json.dumps(validation, ensure_ascii=False, sort_keys=True, indent=2),
            "```",
            "",
            "## Open QA",
            "",
        ]
    )
    for code, count in metrics["qa_open_by_code"].items():
        lines.append(f"- `{code}`: `{count}`")
    lines.extend(
        [
            "",
            "## Row-level review sample",
            "",
        ]
    )
    for sample in samples:
        lines.extend(
            [
                f"### {sample['source_project_id']} — {sample['name']}",
                "",
                f"- Slug / firm: `{sample['slug']}` / `{sample['source_firm_slug']}`",
                f"- Location / year / status: `{sample['location_city_raw']}`, "
                f"`{sample['location_country_raw']}` / `{sample['completion_year_raw']}` "
                f"(`{sample['year_claim_status']}`) / `{sample['constr_status_raw']}`",
                f"- Acceptance / membership: `{sample['acceptance_status']}` / "
                f"`{sample['membership_status']}` / `{sample['building_id']}`",
                f"- Categories ({sample['category_occurrence_count']}): `{_json(sample['categories'])}`",
                f"- Gallery occurrences: `{sample['gallery_occurrence_count']}`",
                f"- Completeness: `{sample['completeness_score']:.4f}`",
                f"- Facets: `{_json(sample['facets'])}`",
                "",
            ]
        )
    lines.extend(
        [
            "## Deferred Architizer-only work",
            "",
            "- Review source-firm stubs and heuristic social/award payloads.",
            "- Decide publishability for placeholder-only projects.",
            "- Review overloaded category rows and scalar conflicts.",
            "- Run future asset downloads/pHash/image classification only in a separately approved stage.",
            "- Revisit provisional memberships with pHash/manual evidence while preserving redirects.",
            "",
        ]
    )
    return "\n".join(lines)


def _publish_pair(
    database_temp: Path,
    report_temp: Path,
    output: Path,
    report: Path,
) -> None:
    if output.exists() or report.exists():
        raise BuildError("immutable output or report appeared during build")
    database_linked = False
    try:
        os.link(database_temp, output)
        database_linked = True
        os.link(report_temp, report)
    except Exception:
        if database_linked and output.exists():
            try:
                if os.path.samefile(database_temp, output):
                    output.unlink()
            except (FileNotFoundError, OSError):
                pass
        raise


def build(
    *,
    source_path: Path,
    output_path: Path,
    report_path: Path,
    limit: Optional[int] = None,
    expected_sha256: str = EXPECTED_SOURCE_SHA256,
    expected_size: int = EXPECTED_SOURCE_SIZE,
    verify_deterministic: bool = False,
) -> dict[str, Any]:
    started = time.perf_counter()
    source_path = source_path.resolve()
    output_path = output_path.resolve()
    report_path = report_path.resolve()
    if not source_path.exists():
        raise BuildError(f"source database not found: {source_path}")
    validate_build_paths(source_path, output_path, report_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with build_lock(output_path):
        database_temp_handle = tempfile.NamedTemporaryFile(
            prefix=output_path.name + ".",
            suffix=".tmp",
            dir=output_path.parent,
            delete=False,
        )
        database_temp = Path(database_temp_handle.name)
        database_temp_handle.close()
        database_temp.unlink()
        report_temp_handle = tempfile.NamedTemporaryFile(
            prefix=report_path.name + ".",
            suffix=".tmp",
            dir=report_path.parent,
            delete=False,
        )
        report_temp = Path(report_temp_handle.name)
        report_temp_handle.close()
        shadow_temp: Optional[Path] = None
        published = False
        try:
            result = materialize_database(
                source_path=source_path,
                target_path=database_temp,
                limit=limit,
                expected_sha256=expected_sha256,
                expected_size=expected_size,
            )
            shadow_sha: Optional[str] = None
            if verify_deterministic:
                shadow_handle = tempfile.NamedTemporaryFile(
                    prefix=output_path.name + ".determinism.",
                    suffix=".tmp",
                    dir=output_path.parent,
                    delete=False,
                )
                shadow_temp = Path(shadow_handle.name)
                shadow_handle.close()
                shadow_temp.unlink()
                shadow_result = materialize_database(
                    source_path=source_path,
                    target_path=shadow_temp,
                    limit=limit,
                    expected_sha256=expected_sha256,
                    expected_size=expected_size,
                )
                shadow_sha = shadow_result["database_sha256"]
                if result["database_sha256"] != shadow_sha:
                    raise BuildError(
                        "deterministic byte rerun mismatch: "
                        f"{result['database_sha256']} != {shadow_sha}; "
                        "logical digests "
                        f"{result['database_logical_sha256']} / "
                        f"{shadow_result['database_logical_sha256']}"
                    )
            _assert_source_sidecars_clean(source_path)
            final_source_sha = sha256_file(source_path)
            if final_source_sha != expected_sha256.upper():
                raise BuildError(
                    "source SHA-256 changed during build: "
                    f"{expected_sha256.upper()} -> {final_source_sha}"
                )
            report_text = render_report(
                result=result,
                database_path=database_temp,
                output_path=output_path,
                limit=limit,
                elapsed_seconds=time.perf_counter() - started,
                deterministic_verified=verify_deterministic,
                deterministic_shadow_sha256=shadow_sha,
            )
            report_temp.write_text(report_text, encoding="utf-8", newline="\n")
            _publish_pair(
                database_temp,
                report_temp,
                output_path,
                report_path,
            )
            published = True
            result.update(
                {
                    "output_path": str(output_path),
                    "report_path": str(report_path),
                    "deterministic_verified": verify_deterministic,
                    "deterministic_shadow_sha256": shadow_sha,
                    "elapsed_seconds": time.perf_counter() - started,
                }
            )
            return result
        finally:
            for temporary in (database_temp, report_temp, shadow_temp):
                if temporary is not None:
                    try:
                        temporary.unlink()
                    except FileNotFoundError:
                        pass
            if not published:
                # Final immutable destinations are never removed here.  Any
                # hard-link rollback is handled synchronously by _publish_pair.
                pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build immutable Architizer source-specific curated SQLite v1"
    )
    parser.add_argument("--source-db", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output-db", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--limit",
        type=int,
        help=(
            "Deterministic nested project subset size. Omit for the full source."
        ),
    )
    parser.add_argument(
        "--verify-deterministic",
        action="store_true",
        help="Build a second temporary database and require byte-identical SHA-256.",
    )
    parser.add_argument(
        "--expected-source-sha256",
        default=EXPECTED_SOURCE_SHA256,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--expected-source-size",
        type=int,
        default=EXPECTED_SOURCE_SIZE,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)
    try:
        result = build(
            source_path=args.source_db,
            output_path=args.output_db,
            report_path=args.report,
            limit=args.limit,
            expected_sha256=args.expected_source_sha256,
            expected_size=args.expected_source_size,
            verify_deterministic=args.verify_deterministic,
        )
    except (BuildError, sqlite3.Error, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        _json(
            {
                "build_id": result["build_id"],
                "output": result["output_path"],
                "report": result["report_path"],
                "output_size_bytes": result["database_size_bytes"],
                "output_sha256": result["database_sha256"],
                "logical_sha256": result["database_logical_sha256"],
                "selected_projects": result["selected_projects"],
                "deterministic_verified": result["deterministic_verified"],
                "elapsed_seconds": round(result["elapsed_seconds"], 3),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
