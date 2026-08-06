#!/usr/bin/env python3
"""Build the immutable Divisare v2.4 image-identity overlay.

The v2.3 metadata artifact remains the source of truth for buildings, text,
taxonomy, area, and D2 review.  This overlay changes only image asset identity:
modern Cloudinary assets use ``public_id + delivery_version`` while legacy
``project_images`` identities remain unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from canonical.divisare_curated import divisare_asset_identity  # noqa: E402


SCHEMA_VERSION = 7
PARENT_SCHEMA_VERSION = 6
PARENT_METADATA_VERSION = "divisare-metadata-v2.3"
METADATA_VERSION = "divisare-metadata-v2.4"
POLICY_VERSION = "divisare-image-identity-v2.4.0"
BUILDER_VERSION = "divisare-image-identity-builder-v2.4.0"
ASSET_KEY_VERSION = "divisare-asset-key-v1.1"
FROZEN_AT = "2026-08-04T00:00:00+00:00"

EXPECTED_PARENT_SHA256 = (
    "7c263f430709dbfe4a2747407d736268331976cd77f66fe25d7037b77fd68038"
)
VERSION_COLLISION_PUBLIC_ID = "7f2fedf69ca074197bf77b221731ff5cca8a0812"

DEFAULT_PARENT = ROOT / "data" / "curated" / "divisare_metadata_v2_3.db"
DEFAULT_OUTPUT = ROOT / "data" / "curated" / "divisare_metadata_v2_4.db"
DEFAULT_REPORT = ROOT / "data" / "reports" / "divisare_metadata_v2_4.md"

REQUIRED_TABLES = {
    "metadata_review_lineage_v2_3",
    "metadata_review_validation_v2_3",
    "image_assets",
    "image_urls",
    "source_image_occurrences",
    "article_image_occurrences",
    "image_url_hints",
    "image_hashes",
    "image_hash_bands",
    "image_classifications",
    "image_match_candidates",
    "attribute_claims",
    "building_images_materialized_v2",
    "building_images_materialized_v2_3",
    "active_building_membership_v2",
    "active_building_membership_v2_3",
}

CHANGED_PARENT_TABLES = {
    "image_assets",
    "image_urls",
    "source_image_occurrences",
    "article_image_occurrences",
    "image_url_hints",
    "image_hashes",
    "building_images_materialized_v2",
    "building_images_materialized_v2_3",
}

LOGICAL_TABLES = (
    "image_identity_lineage_v2_4",
    "image_asset_key_map_v2_4",
    "image_assets",
    "image_urls",
    "source_image_occurrences",
    "article_image_occurrences",
    "image_url_hints",
    "image_hashes",
    "building_images_materialized_v2",
    "building_images_materialized_v2_3",
    "image_identity_metrics_v2_4",
    "image_identity_validation_v2_4",
)

_VERSION_RE = re.compile(r"/(v\d+)/")


SCHEMA_SQL = """
CREATE TABLE image_identity_lineage_v2_4 (
    lineage_id                 INTEGER PRIMARY KEY CHECK(lineage_id=1),
    parent_db_path             TEXT NOT NULL,
    parent_sha256              TEXT NOT NULL CHECK(length(parent_sha256)=64),
    parent_byte_size           INTEGER NOT NULL,
    parent_schema_version      INTEGER NOT NULL,
    parent_metadata_version    TEXT NOT NULL,
    builder_version            TEXT NOT NULL,
    policy_version             TEXT NOT NULL,
    metadata_version           TEXT NOT NULL,
    schema_version             INTEGER NOT NULL,
    asset_key_version          TEXT NOT NULL,
    frozen_at                  TEXT NOT NULL,
    counts_json                TEXT NOT NULL CHECK(json_valid(counts_json)),
    scope_json                 TEXT NOT NULL CHECK(json_valid(scope_json))
);

CREATE TABLE image_asset_key_map_v2_4 (
    old_asset_key              TEXT NOT NULL,
    new_asset_key              TEXT NOT NULL REFERENCES image_assets(asset_key),
    public_id                  TEXT NOT NULL,
    delivery_version           TEXT NOT NULL,
    url_generation             TEXT NOT NULL CHECK(url_generation='cloudinary_public_id'),
    url_count                  INTEGER NOT NULL CHECK(url_count>0),
    PRIMARY KEY(old_asset_key,new_asset_key)
) WITHOUT ROWID;

CREATE INDEX idx_v24_asset_key_map_new
ON image_asset_key_map_v2_4(new_asset_key);

CREATE TABLE image_identity_metrics_v2_4 (
    metric                     TEXT PRIMARY KEY,
    value_json                 TEXT NOT NULL CHECK(json_valid(value_json))
) WITHOUT ROWID;

CREATE TABLE image_identity_validation_v2_4 (
    check_name                 TEXT PRIMARY KEY,
    passed                     INTEGER NOT NULL CHECK(passed IN (0,1)),
    actual_json                TEXT NOT NULL CHECK(json_valid(actual_json)),
    expected_json              TEXT NOT NULL CHECK(json_valid(expected_json)),
    frozen_at                  TEXT NOT NULL
) WITHOUT ROWID;

CREATE VIEW v_building_images_v2_4 AS
SELECT building_id,asset_key,representative_url,role_rank,first_position
FROM building_images_materialized_v2_3;

CREATE VIEW v_divisare_buildings_export_v2_4 AS
SELECT * FROM v_divisare_buildings_export_v2_3;
"""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _stable_artifact_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def open_readonly(path: Path) -> sqlite3.Connection:
    uri = "file:%s?mode=ro" % path.resolve().as_posix()
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _parent_metadata_version(conn: sqlite3.Connection) -> Optional[str]:
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(metadata_review_lineage_v2_3)")
    }
    if "metadata_version" not in columns:
        return None
    row = conn.execute(
        "SELECT metadata_version FROM metadata_review_lineage_v2_3 WHERE lineage_id=1"
    ).fetchone()
    return str(row[0]) if row is not None else None


def _preflight_image_state(conn: sqlite3.Connection) -> Dict[str, int]:
    queries = {
        "non_pending_hashes": """
            SELECT COUNT(*) FROM image_hashes
            WHERE status<>'pending' OR attempt_count<>0 OR hash_bits IS NOT NULL
               OR hash_hex IS NOT NULL OR last_error IS NOT NULL
               OR computed_at IS NOT NULL
        """,
        "fetched_assets": """
            SELECT COUNT(*) FROM image_assets
            WHERE fetch_status<>'pending' OR mime_type IS NOT NULL
               OR width IS NOT NULL OR height IS NOT NULL OR byte_size IS NOT NULL
               OR content_sha256 IS NOT NULL OR last_fetch_error IS NOT NULL
               OR fetched_at IS NOT NULL
        """,
        "hash_bands": "SELECT COUNT(*) FROM image_hash_bands",
        "classifications": "SELECT COUNT(*) FROM image_classifications",
        "match_candidates": "SELECT COUNT(*) FROM image_match_candidates",
        "image_scoped_claims": (
            "SELECT COUNT(*) FROM attribute_claims WHERE image_asset_key IS NOT NULL"
        ),
    }
    return {name: int(conn.execute(sql).fetchone()[0]) for name, sql in queries.items()}


def inspect_parent(
    parent_path: Path,
    *,
    production_contract: bool,
    compute_content_hashes: bool,
) -> Dict[str, Any]:
    parent_path = parent_path.resolve()
    if not parent_path.is_file():
        raise FileNotFoundError(parent_path)
    parent_sha = file_sha256(parent_path)
    if production_contract and parent_sha != EXPECTED_PARENT_SHA256:
        raise RuntimeError("parent SHA does not match the pinned v2.3 artifact")

    conn = open_readonly(parent_path)
    try:
        quick = str(conn.execute("PRAGMA quick_check").fetchone()[0])
        if quick != "ok":
            raise RuntimeError("parent quick_check failed: %s" % quick)
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if user_version != PARENT_SCHEMA_VERSION:
            raise RuntimeError(
                "expected parent user_version %d, found %d"
                % (PARENT_SCHEMA_VERSION, user_version)
            )
        missing = sorted(REQUIRED_TABLES - _table_names(conn))
        if missing:
            raise RuntimeError("parent DB missing required tables: %s" % missing)
        if any(name.endswith("_v2_4") for name in _table_names(conn)):
            raise RuntimeError("parent already contains a v2.4 image identity overlay")
        metadata_version = _parent_metadata_version(conn)
        if metadata_version != PARENT_METADATA_VERSION:
            raise RuntimeError(
                "expected parent metadata version %s, found %s"
                % (PARENT_METADATA_VERSION, metadata_version)
            )
        failed_parent_checks = int(
            conn.execute(
                "SELECT COUNT(*) FROM metadata_review_validation_v2_3 WHERE passed<>1"
            ).fetchone()[0]
        )
        if failed_parent_checks:
            raise RuntimeError("parent contains failed v2.3 validation rows")

        protected_state = _preflight_image_state(conn)
        if any(protected_state.values()):
            raise RuntimeError(
                "v2.4 re-key requires untouched pending image state: %s"
                % protected_state
            )

        counts = {
            "image_assets": int(conn.execute("SELECT COUNT(*) FROM image_assets").fetchone()[0]),
            "image_urls": int(conn.execute("SELECT COUNT(*) FROM image_urls").fetchone()[0]),
            "source_occurrences": int(
                conn.execute("SELECT COUNT(*) FROM source_image_occurrences").fetchone()[0]
            ),
            "article_occurrences": int(
                conn.execute("SELECT COUNT(*) FROM article_image_occurrences").fetchone()[0]
            ),
            "modern_assets": int(
                conn.execute(
                    "SELECT COUNT(*) FROM image_assets "
                    "WHERE url_generation='cloudinary_public_id'"
                ).fetchone()[0]
            ),
            "legacy_assets": int(
                conn.execute(
                    "SELECT COUNT(*) FROM image_assets "
                    "WHERE url_generation='project_images'"
                ).fetchone()[0]
            ),
            "hashes": int(conn.execute("SELECT COUNT(*) FROM image_hashes").fetchone()[0]),
            "building_images_v2": int(
                conn.execute("SELECT COUNT(*) FROM building_images_materialized_v2").fetchone()[0]
            ),
            "building_images_v2_3": int(
                conn.execute("SELECT COUNT(*) FROM building_images_materialized_v2_3").fetchone()[0]
            ),
        }
        if counts["source_occurrences"] != counts["image_urls"]:
            raise RuntimeError("parent source occurrence accounting is incomplete")
        if counts["article_occurrences"] != counts["image_urls"]:
            raise RuntimeError("parent article occurrence accounting is incomplete")
        if counts["hashes"] != counts["image_assets"]:
            raise RuntimeError("parent pending hash accounting is incomplete")

        if production_contract:
            expected = {
                "image_assets": 547_222,
                "image_urls": 577_112,
                "source_occurrences": 577_112,
                "article_occurrences": 577_112,
                "modern_assets": 429_291,
                "legacy_assets": 117_931,
                "hashes": 547_222,
                "building_images_v2": 547_222,
                "building_images_v2_3": 547_222,
            }
            if counts != expected:
                raise RuntimeError(
                    "production v2.3 population contract changed: actual=%s expected=%s"
                    % (counts, expected)
                )

        hashes = user_table_hashes(conn) if compute_content_hashes else {}
        schemas = schema_objects(conn) if compute_content_hashes else {}
    finally:
        conn.close()
    return {
        "sha256": parent_sha,
        "byte_size": parent_path.stat().st_size,
        "schema_version": PARENT_SCHEMA_VERSION,
        "metadata_version": PARENT_METADATA_VERSION,
        "counts": counts,
        "protected_state": protected_state,
        "table_hashes": hashes,
        "schema_objects": schemas,
    }


def derive_v24_identity(url: str) -> Tuple[str, Optional[str], Optional[str], str]:
    """Return new key, public ID, delivery version, and URL generation."""

    identity = divisare_asset_identity(url)
    if identity is None:
        raise ValueError("unsupported Divisare URL: %s" % url)
    generation = str(identity.url_generation)
    public_id = identity.public_id
    if generation != "cloudinary_public_id":
        return identity.asset_key, public_id, None, generation
    if not public_id:
        raise ValueError("Cloudinary identity is missing public_id: %s" % url)

    delivery_version = getattr(identity, "delivery_version", None)
    path = urlsplit(url).path or ""
    expected_pattern = re.compile(
        r"/(v\d+)/%s(?:[/.]|$)" % re.escape(str(public_id))
    )
    match = expected_pattern.search(path)
    parsed_version = match.group(1) if match else None
    if not parsed_version:
        raise ValueError("Cloudinary delivery version is missing: %s" % url)
    if delivery_version is not None and str(delivery_version) != parsed_version:
        raise ValueError("parser and URL delivery versions disagree: %s" % url)
    return (
        "divisare|%s|%s" % (public_id, parsed_version),
        str(public_id),
        parsed_version,
        generation,
    )


def _create_url_mapping(conn: sqlite3.Connection) -> Dict[str, int]:
    conn.executescript(
        """
        CREATE TEMP TABLE _url_asset_map_v2_4 (
            url_id              INTEGER PRIMARY KEY,
            source_url          TEXT NOT NULL UNIQUE,
            old_asset_key       TEXT NOT NULL,
            new_asset_key       TEXT NOT NULL,
            public_id           TEXT,
            delivery_version    TEXT,
            url_generation      TEXT NOT NULL
        );
        """
    )
    cursor = conn.execute(
        """
        SELECT iu.url_id,iu.asset_key AS old_asset_key,iu.url,iu.url_generation,
               ia.public_id
        FROM image_urls iu
        JOIN image_assets ia ON ia.asset_key=iu.asset_key
        ORDER BY iu.url_id
        """
    )
    inserted = 0
    while True:
        rows = cursor.fetchmany(5_000)
        if not rows:
            break
        mapped = []
        for row in rows:
            new_key, public_id, version, generation = derive_v24_identity(str(row["url"]))
            if generation != str(row["url_generation"]):
                raise RuntimeError(
                    "URL generation mismatch at url_id=%s" % row["url_id"]
                )
            stored_public_id = row["public_id"]
            if stored_public_id is not None and str(stored_public_id) != str(public_id):
                raise RuntimeError(
                    "public_id mismatch at url_id=%s" % row["url_id"]
                )
            mapped.append(
                (
                    int(row["url_id"]),
                    str(row["url"]),
                    str(row["old_asset_key"]),
                    new_key,
                    public_id,
                    version,
                    generation,
                )
            )
        conn.executemany(
            "INSERT INTO _url_asset_map_v2_4 VALUES (?,?,?,?,?,?,?)", mapped
        )
        inserted += len(mapped)

    url_count = int(conn.execute("SELECT COUNT(*) FROM image_urls").fetchone()[0])
    if inserted != url_count:
        raise RuntimeError("URL mapping accounting mismatch")
    conn.executescript(
        """
        CREATE INDEX _idx_url_asset_map_old
        ON _url_asset_map_v2_4(old_asset_key);
        CREATE INDEX _idx_url_asset_map_new
        ON _url_asset_map_v2_4(new_asset_key);
        """
    )
    missing_assets = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM image_assets ia
            WHERE NOT EXISTS (
              SELECT 1 FROM _url_asset_map_v2_4 m
              WHERE m.old_asset_key=ia.asset_key
            )
            """
        ).fetchone()[0]
    )
    if missing_assets:
        raise RuntimeError("image assets without URLs: %d" % missing_assets)
    merged_old_assets = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM (
              SELECT new_asset_key
              FROM _url_asset_map_v2_4
              GROUP BY new_asset_key
              HAVING COUNT(DISTINCT old_asset_key)>1
            )
            """
        ).fetchone()[0]
    )
    if merged_old_assets:
        raise RuntimeError("v2.4 identity unexpectedly merges old asset keys")
    preexisting_key_collisions = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM (
              SELECT DISTINCT m.new_asset_key
              FROM _url_asset_map_v2_4 m
              JOIN image_assets ia ON ia.asset_key=m.new_asset_key
              WHERE m.new_asset_key<>m.old_asset_key
            )
            """
        ).fetchone()[0]
    )
    if preexisting_key_collisions:
        raise RuntimeError("a new v2.4 key collides with an existing old key")
    changed_non_cloudinary = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM _url_asset_map_v2_4
            WHERE url_generation<>'cloudinary_public_id'
              AND new_asset_key<>old_asset_key
            """
        ).fetchone()[0]
    )
    if changed_non_cloudinary:
        raise RuntimeError(
            "v2.4 must not change project_images or fallback asset keys"
        )

    return {
        "url_rows": inserted,
        "old_assets": int(
            conn.execute(
                "SELECT COUNT(DISTINCT old_asset_key) FROM _url_asset_map_v2_4"
            ).fetchone()[0]
        ),
        "new_assets": int(
            conn.execute(
                "SELECT COUNT(DISTINCT new_asset_key) FROM _url_asset_map_v2_4"
            ).fetchone()[0]
        ),
        "modern_old_assets": int(
            conn.execute(
                """
                SELECT COUNT(DISTINCT old_asset_key) FROM _url_asset_map_v2_4
                WHERE url_generation='cloudinary_public_id'
                """
            ).fetchone()[0]
        ),
        "modern_new_assets": int(
            conn.execute(
                """
                SELECT COUNT(DISTINCT new_asset_key) FROM _url_asset_map_v2_4
                WHERE url_generation='cloudinary_public_id'
                """
            ).fetchone()[0]
        ),
        "legacy_assets": int(
            conn.execute(
                """
                SELECT COUNT(DISTINCT new_asset_key) FROM _url_asset_map_v2_4
                WHERE url_generation='project_images'
                """
            ).fetchone()[0]
        ),
    }


def _materialize_building_images(
    conn: sqlite3.Connection,
    *,
    membership_table: str,
    output_table: str,
) -> int:
    membership = _quote_identifier(membership_table)
    output = _quote_identifier(output_table)
    conn.execute("DELETE FROM %s" % output)
    conn.execute(
        """
        INSERT INTO %s(building_id,asset_key,representative_url,role_rank,first_position)
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
          FROM %s m
          JOIN article_image_occurrences aio ON aio.article_id=m.article_id
          JOIN image_urls iu ON iu.url_id=aio.url_id
        )
        SELECT building_id,asset_key,representative_url,role_rank,first_position
        FROM ranked WHERE rn=1
        ORDER BY building_id,role_rank,first_position,asset_key
        """
        % (output, membership)
    )
    return int(conn.execute("SELECT COUNT(*) FROM %s" % output).fetchone()[0])


def _apply_rekey(conn: sqlite3.Connection) -> Dict[str, int]:
    mapping = _create_url_mapping(conn)

    conn.execute(
        """
        INSERT INTO image_asset_key_map_v2_4(
          old_asset_key,new_asset_key,public_id,delivery_version,
          url_generation,url_count
        )
        SELECT old_asset_key,new_asset_key,public_id,delivery_version,
               url_generation,COUNT(*)
        FROM _url_asset_map_v2_4
        WHERE new_asset_key<>old_asset_key
          AND url_generation='cloudinary_public_id'
        GROUP BY old_asset_key,new_asset_key,public_id,delivery_version,url_generation
        ORDER BY old_asset_key,new_asset_key
        """
    )

    conn.execute(
        """
        INSERT INTO image_assets(
          asset_key,provider,public_id,original_filename,url_generation,
          first_seen_article_id,mime_type,width,height,byte_size,content_sha256,
          fetch_status,last_fetch_error,fetched_at
        )
        SELECT DISTINCT
          m.new_asset_key,ia.provider,ia.public_id,ia.original_filename,
          ia.url_generation,ia.first_seen_article_id,ia.mime_type,ia.width,
          ia.height,ia.byte_size,ia.content_sha256,ia.fetch_status,
          ia.last_fetch_error,ia.fetched_at
        FROM _url_asset_map_v2_4 m
        JOIN image_assets ia ON ia.asset_key=m.old_asset_key
        WHERE m.new_asset_key<>m.old_asset_key
        ORDER BY m.new_asset_key
        """
    )

    conn.executescript(
        """
        CREATE TEMP TABLE _image_hashes_v2_4 AS
        SELECT DISTINCT
          m.new_asset_key AS asset_key,h.algorithm,h.algorithm_version,
          h.hash_bits,h.hash_hex,h.status,h.attempt_count,h.last_error,
          h.computed_at,h.run_id
        FROM _url_asset_map_v2_4 m
        JOIN image_hashes h ON h.asset_key=m.old_asset_key;

        UPDATE image_urls
        SET asset_key=(
          SELECT m.new_asset_key FROM _url_asset_map_v2_4 m
          WHERE m.url_id=image_urls.url_id
        );

        UPDATE source_image_occurrences
        SET asset_key=(
          SELECT m.new_asset_key
          FROM _url_asset_map_v2_4 m
          WHERE m.source_url=source_image_occurrences.raw_url
        )
        WHERE parse_status='parsed';

        UPDATE article_image_occurrences
        SET asset_key=(
          SELECT m.new_asset_key FROM _url_asset_map_v2_4 m
          WHERE m.url_id=article_image_occurrences.url_id
        );

        UPDATE image_url_hints
        SET asset_key=(
          SELECT m.new_asset_key FROM _url_asset_map_v2_4 m
          WHERE m.url_id=image_url_hints.url_id
        );

        DELETE FROM image_hashes;
        INSERT INTO image_hashes(
          asset_key,algorithm,algorithm_version,hash_bits,hash_hex,status,
          attempt_count,last_error,computed_at,run_id
        )
        SELECT asset_key,algorithm,algorithm_version,hash_bits,hash_hex,status,
               attempt_count,last_error,computed_at,run_id
        FROM _image_hashes_v2_4
        ORDER BY asset_key,algorithm,algorithm_version;
        """
    )

    mapping["building_images_v2"] = _materialize_building_images(
        conn,
        membership_table="active_building_membership_v2",
        output_table="building_images_materialized_v2",
    )
    mapping["building_images_v2_3"] = _materialize_building_images(
        conn,
        membership_table="active_building_membership_v2_3",
        output_table="building_images_materialized_v2_3",
    )

    conn.execute(
        """
        DELETE FROM image_assets
        WHERE NOT EXISTS (
          SELECT 1 FROM _url_asset_map_v2_4 m
          WHERE m.new_asset_key=image_assets.asset_key
        )
        """
    )
    mapping["changed_key_rows"] = int(
        conn.execute("SELECT COUNT(*) FROM image_asset_key_map_v2_4").fetchone()[0]
    )
    mapping["asset_delta"] = mapping["new_assets"] - mapping["old_assets"]
    mapping["hash_rows"] = int(
        conn.execute("SELECT COUNT(*) FROM image_hashes").fetchone()[0]
    )
    return mapping


def _current_metrics(conn: sqlite3.Connection, mapping: Mapping[str, int]) -> Dict[str, Any]:
    metrics: Dict[str, Any] = dict(mapping)
    metrics.update(
        {
            "image_assets": int(conn.execute("SELECT COUNT(*) FROM image_assets").fetchone()[0]),
            "image_urls": int(conn.execute("SELECT COUNT(*) FROM image_urls").fetchone()[0]),
            "source_occurrences": int(
                conn.execute("SELECT COUNT(*) FROM source_image_occurrences").fetchone()[0]
            ),
            "article_occurrences": int(
                conn.execute("SELECT COUNT(*) FROM article_image_occurrences").fetchone()[0]
            ),
            "modern_assets": int(
                conn.execute(
                    "SELECT COUNT(*) FROM image_assets "
                    "WHERE url_generation='cloudinary_public_id'"
                ).fetchone()[0]
            ),
            "legacy_assets_after": int(
                conn.execute(
                    "SELECT COUNT(*) FROM image_assets "
                    "WHERE url_generation='project_images'"
                ).fetchone()[0]
            ),
            "pending_hashes": int(
                conn.execute("SELECT COUNT(*) FROM image_hashes WHERE status='pending'").fetchone()[0]
            ),
            "gyaan_assets": int(
                conn.execute(
                    "SELECT COUNT(*) FROM image_assets "
                    "WHERE public_id=? AND url_generation='cloudinary_public_id'",
                    (VERSION_COLLISION_PUBLIC_ID,),
                ).fetchone()[0]
            ),
            "gyaan_urls": int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM image_urls iu
                    JOIN image_assets ia ON ia.asset_key=iu.asset_key
                    WHERE ia.public_id=? AND ia.url_generation='cloudinary_public_id'
                    """,
                    (VERSION_COLLISION_PUBLIC_ID,),
                ).fetchone()[0]
            ),
        }
    )
    return metrics


def _validation_checks(
    conn: sqlite3.Connection,
    *,
    parent: Mapping[str, Any],
    metrics: Mapping[str, Any],
    production_contract: bool,
) -> list[Dict[str, Any]]:
    checks: list[Dict[str, Any]] = []

    def add(name: str, actual: Any, expected: Any, passed: Optional[bool] = None) -> None:
        checks.append(
            {
                "name": name,
                "actual": actual,
                "expected": expected,
                "passed": bool(actual == expected if passed is None else passed),
            }
        )

    add("schema_version", int(conn.execute("PRAGMA user_version").fetchone()[0]), SCHEMA_VERSION)
    add("image_assets", metrics["image_assets"], metrics["new_assets"])
    add("image_urls_preserved", metrics["image_urls"], parent["counts"]["image_urls"])
    add(
        "source_occurrences_preserved",
        metrics["source_occurrences"],
        parent["counts"]["source_occurrences"],
    )
    add(
        "article_occurrences_preserved",
        metrics["article_occurrences"],
        parent["counts"]["article_occurrences"],
    )
    add("legacy_assets_preserved", metrics["legacy_assets_after"], parent["counts"]["legacy_assets"])
    add("pending_hash_accounting", metrics["pending_hashes"], metrics["image_assets"])
    add("hash_accounting", metrics["hash_rows"], metrics["image_assets"])
    add(
        "changed_keys_cover_all_modern_assets",
        metrics["changed_key_rows"],
        metrics["modern_new_assets"],
    )
    add(
        "modern_assets_have_one_key_map_row",
        int(
            conn.execute(
                """
                SELECT COUNT(*) FROM image_assets ia
                WHERE ia.url_generation='cloudinary_public_id'
                  AND (
                    SELECT COUNT(*) FROM image_asset_key_map_v2_4 m
                    WHERE m.new_asset_key=ia.asset_key
                  )<>1
                """
            ).fetchone()[0]
        ),
        0,
    )
    add(
        "url_keys_match_mapping",
        int(
            conn.execute(
                """
                SELECT COUNT(*) FROM image_urls iu
                JOIN _url_asset_map_v2_4 m ON m.url_id=iu.url_id
                WHERE iu.asset_key<>m.new_asset_key
                """
            ).fetchone()[0]
        ),
        0,
    )
    add(
        "source_occurrence_keys_match",
        int(
            conn.execute(
                """
                SELECT COUNT(*) FROM source_image_occurrences sio
                LEFT JOIN image_urls iu ON iu.url=sio.raw_url
                WHERE sio.parse_status='parsed'
                  AND (iu.url_id IS NULL OR sio.asset_key<>iu.asset_key)
                """
            ).fetchone()[0]
        ),
        0,
    )
    add(
        "article_occurrence_keys_match",
        int(
            conn.execute(
                """
                SELECT COUNT(*) FROM article_image_occurrences aio
                JOIN image_urls iu ON iu.url_id=aio.url_id
                WHERE aio.asset_key<>iu.asset_key
                """
            ).fetchone()[0]
        ),
        0,
    )
    add(
        "hint_keys_match",
        int(
            conn.execute(
                """
                SELECT COUNT(*) FROM image_url_hints h
                JOIN image_urls iu ON iu.url_id=h.url_id
                WHERE h.asset_key<>iu.asset_key
                """
            ).fetchone()[0]
        ),
        0,
    )
    for membership, output, name in (
        (
            "active_building_membership_v2",
            "building_images_materialized_v2",
            "building_images_v2_complete",
        ),
        (
            "active_building_membership_v2_3",
            "building_images_materialized_v2_3",
            "building_images_v2_3_complete",
        ),
    ):
        expected = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM (
                  SELECT DISTINCT m.building_id,aio.asset_key
                  FROM %s m
                  JOIN article_image_occurrences aio ON aio.article_id=m.article_id
                )
                """ % _quote_identifier(membership)
            ).fetchone()[0]
        )
        actual = int(
            conn.execute("SELECT COUNT(*) FROM %s" % _quote_identifier(output)).fetchone()[0]
        )
        add(name, actual, expected)

    target_schema = schema_objects(conn)
    changed_schema = {
        "%s:%s" % key: {"expected": sql, "actual": target_schema.get(key)}
        for key, sql in parent["schema_objects"].items()
        if target_schema.get(key) != sql
    }
    add("parent_schema_objects_preserved", changed_schema, {})
    for table, expected_hash in sorted(parent["table_hashes"].items()):
        if table in CHANGED_PARENT_TABLES:
            continue
        add(
            "preserved_table_%s" % table,
            table_logical_sha256(conn, table),
            expected_hash,
        )

    if production_contract:
        add("production_asset_delta", metrics["asset_delta"], 30)
        add("production_image_assets", metrics["image_assets"], 547_252)
        add("production_modern_assets", metrics["modern_assets"], 429_321)
        add("gyaan_split_assets", metrics["gyaan_assets"], 31)
        add("gyaan_urls", metrics["gyaan_urls"], 32)
        add(
            "old_gyaan_key_removed",
            int(
                conn.execute(
                    "SELECT COUNT(*) FROM image_assets WHERE asset_key=?",
                    ("divisare|%s" % VERSION_COLLISION_PUBLIC_ID,),
                ).fetchone()[0]
            ),
            0,
        )
        add(
            "gyaan_cover_gallery_version_pair",
            int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM image_urls
                    WHERE asset_key=?
                    """,
                    (
                        "divisare|%s|v1678438203"
                        % VERSION_COLLISION_PUBLIC_ID,
                    ),
                ).fetchone()[0]
            ),
            2,
        )

    add("foreign_keys", len(conn.execute("PRAGMA foreign_key_check").fetchall()), 0)
    add("integrity", str(conn.execute("PRAGMA integrity_check").fetchone()[0]), "ok")
    return checks


def logical_sha256(conn: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for table in LOGICAL_TABLES:
        digest.update(table.encode("utf-8"))
        digest.update(table_logical_sha256(conn, table).encode("ascii"))
    return digest.hexdigest()


def _insert_lineage(
    conn: sqlite3.Connection,
    *,
    parent_path: Path,
    parent: Mapping[str, Any],
    counts: Mapping[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO image_identity_lineage_v2_4(
          lineage_id,parent_db_path,parent_sha256,parent_byte_size,
          parent_schema_version,parent_metadata_version,builder_version,
          policy_version,metadata_version,schema_version,asset_key_version,
          frozen_at,counts_json,scope_json
        ) VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            _stable_artifact_path(parent_path),
            parent["sha256"],
            parent["byte_size"],
            PARENT_SCHEMA_VERSION,
            PARENT_METADATA_VERSION,
            BUILDER_VERSION,
            POLICY_VERSION,
            METADATA_VERSION,
            SCHEMA_VERSION,
            ASSET_KEY_VERSION,
            FROZEN_AT,
            canonical_json(dict(counts)),
            canonical_json(
                {
                    "identity_rule": "cloudinary_public_id_plus_delivery_version",
                    "legacy_project_images": "unchanged",
                    "parent_is_immutable": True,
                    "image_content_work": "must_be_unstarted",
                    "excluded": [
                        "download",
                        "phash",
                        "classification",
                        "vision",
                        "building_identity",
                        "metadata_review",
                    ],
                }
            ),
        ),
    )


def _build_temp_artifact(
    *,
    temp_path: Path,
    parent_path: Path,
    parent: Mapping[str, Any],
    production_contract: bool,
) -> Tuple[Dict[str, Any], Dict[str, Any], str]:
    shutil.copyfile(parent_path, temp_path)
    conn = sqlite3.connect(temp_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA synchronous=FULL")
        conn.executescript(SCHEMA_SQL)
        conn.execute("BEGIN IMMEDIATE")
        mapping = _apply_rekey(conn)
        conn.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
        metrics = _current_metrics(conn, mapping)
        _insert_lineage(
            conn,
            parent_path=parent_path,
            parent=parent,
            counts=metrics,
        )
        conn.executemany(
            "INSERT INTO image_identity_metrics_v2_4(metric,value_json) VALUES (?,?)",
            [
                (key, canonical_json(value))
                for key, value in sorted(metrics.items())
            ],
        )
        checks = _validation_checks(
            conn,
            parent=parent,
            metrics=metrics,
            production_contract=production_contract,
        )
        conn.executemany(
            """
            INSERT INTO image_identity_validation_v2_4(
              check_name,passed,actual_json,expected_json,frozen_at
            ) VALUES (?,?,?,?,?)
            """,
            [
                (
                    check["name"],
                    int(check["passed"]),
                    canonical_json(check["actual"]),
                    canonical_json(check["expected"]),
                    FROZEN_AT,
                )
                for check in checks
            ],
        )
        failed = [check["name"] for check in checks if not check["passed"]]
        if failed:
            raise RuntimeError("v2.4 validation failed: %s" % ", ".join(failed))
        conn.commit()
        conn.execute("ANALYZE")
        conn.commit()
        conn.execute("VACUUM")
        conn.execute("PRAGMA foreign_keys=ON")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("post-VACUUM integrity check failed")
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("post-VACUUM foreign key check failed")
        logical = logical_sha256(conn)
        validation = {
            "passed": len(checks),
            "failed": 0,
            "checks": checks,
        }
        return metrics, validation, logical
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def exclusive_build_lock(lock_path: Path, output_path: Path) -> Iterable[None]:
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RuntimeError("build lock already exists: %s" % lock_path) from exc
    try:
        payload = canonical_json(
            {"pid": os.getpid(), "output": str(output_path)}
        ).encode("utf-8")
        os.write(fd, payload)
        yield
    finally:
        os.close(fd)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _validate_paths(paths: Sequence[Path]) -> None:
    resolved = [str(path.resolve()).casefold() for path in paths]
    if len(resolved) != len(set(resolved)):
        raise ValueError("parent/output/report/staging/lock paths must be distinct")


def publish_no_clobber(
    *,
    temp_path: Path,
    output_path: Path,
    report_temp_path: Path,
    report_path: Path,
) -> None:
    output_linked = False
    report_linked = False
    try:
        os.link(temp_path, output_path)
        output_linked = True
        os.link(report_temp_path, report_path)
        report_linked = True
    except FileExistsError as exc:
        if report_linked:
            report_path.unlink()
        if output_linked:
            output_path.unlink()
        raise RuntimeError("immutable output appeared during publication") from exc
    temp_path.unlink()
    report_temp_path.unlink()


def write_report(
    path: Path,
    *,
    parent_path: Path,
    output_path: Path,
    parent_sha256: str,
    output_sha256: str,
    logical_sha256_value: str,
    metrics: Mapping[str, Any],
    validation: Mapping[str, Any],
) -> None:
    lines = [
        "# Divisare image identity v2.4",
        "",
        "- Parent: `%s`" % _stable_artifact_path(parent_path),
        "- Parent SHA-256: `%s`" % parent_sha256,
        "- Output: `%s`" % _stable_artifact_path(output_path),
        "- Output SHA-256: `%s`" % output_sha256,
        "- Logical SHA-256: `%s`" % logical_sha256_value,
        "- Builder: `%s`" % BUILDER_VERSION,
        "- Policy: `%s`" % POLICY_VERSION,
        "- Asset key: `%s`" % ASSET_KEY_VERSION,
        "- Schema: `%d`" % SCHEMA_VERSION,
        "- Frozen at: `%s`" % FROZEN_AT,
        "- External API / LLM / Vision / Neon / R2 cost: `$0`",
        "",
        "## Result",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    for name in (
        "old_assets",
        "new_assets",
        "asset_delta",
        "modern_old_assets",
        "modern_new_assets",
        "legacy_assets_after",
        "image_urls",
        "source_occurrences",
        "article_occurrences",
        "pending_hashes",
        "building_images_v2",
        "building_images_v2_3",
        "gyaan_assets",
        "gyaan_urls",
    ):
        lines.append("| `%s` | %s |" % (name, f"{int(metrics[name]):,}"))
    lines.extend(
        [
            "",
            "## Validation",
            "",
            "- Passed: `%d`" % int(validation["passed"]),
            "- Failed: `%d`" % int(validation["failed"]),
            "",
            "```json",
            json.dumps(validation["checks"], ensure_ascii=False, indent=2, sort_keys=True),
            "```",
            "",
            "## Scope",
            "",
            "All modern Cloudinary asset keys include their delivery version. Legacy",
            "project-image keys are unchanged. No image was downloaded or classified,",
            "and all building/text/taxonomy/area/D2 review tables remain unchanged.",
            "Historical `build_runs.run_id` values are retained on regenerated pending",
            "hash rows because they identify the original placeholder creation run; the",
            "new identity policy and parent lineage are recorded separately in the v2.4",
            "lineage and key-map tables.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def validate_only(
    *,
    parent_path: Path,
    production_contract: bool = True,
) -> Dict[str, Any]:
    parent = inspect_parent(
        parent_path,
        production_contract=production_contract,
        compute_content_hashes=False,
    )
    return {
        "status": "validated",
        "parent_sha256": parent["sha256"],
        "parent_counts": parent["counts"],
        "output_created": False,
    }


def build_artifact(
    *,
    parent_path: Path,
    output_path: Path,
    report_path: Path,
    production_contract: bool = True,
) -> Dict[str, Any]:
    started = time.monotonic()
    parent_path = Path(parent_path).resolve()
    output_path = Path(output_path).resolve()
    report_path = Path(report_path).resolve()
    lock_path = output_path.with_name(output_path.name + ".lock")
    temp_path = output_path.with_name("%s.tmp.%s" % (output_path.name, os.getpid()))
    report_temp_path = report_path.with_name(
        "%s.tmp.%s" % (report_path.name, os.getpid())
    )
    _validate_paths(
        [parent_path, output_path, report_path, lock_path, temp_path, report_temp_path]
    )
    if output_path.exists() or report_path.exists():
        raise FileExistsError("immutable output or report already exists")
    if temp_path.exists() or report_temp_path.exists():
        raise FileExistsError("build staging path already exists")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    with exclusive_build_lock(lock_path, output_path):
        parent = inspect_parent(
            parent_path,
            production_contract=production_contract,
            compute_content_hashes=True,
        )
        try:
            metrics, validation, logical = _build_temp_artifact(
                temp_path=temp_path,
                parent_path=parent_path,
                parent=parent,
                production_contract=production_contract,
            )
            if file_sha256(parent_path) != parent["sha256"]:
                raise RuntimeError("immutable v2.3 parent changed during build")
            output_sha = file_sha256(temp_path)
            write_report(
                report_temp_path,
                parent_path=parent_path,
                output_path=output_path,
                parent_sha256=parent["sha256"],
                output_sha256=output_sha,
                logical_sha256_value=logical,
                metrics=metrics,
                validation=validation,
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
        "logical_sha256": logical,
        "parent_sha256": parent["sha256"],
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "metrics": metrics,
        "validation": {"passed": validation["passed"], "failed": 0},
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", type=Path, default=DEFAULT_PARENT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="validate the pinned v2.3 parent without creating output",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        if args.validate_only:
            result = validate_only(parent_path=args.parent)
        else:
            result = build_artifact(
                parent_path=args.parent,
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
