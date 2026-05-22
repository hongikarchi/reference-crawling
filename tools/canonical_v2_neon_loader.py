#!/usr/bin/env python3
"""Create and load the fresh canonical_v2_buildings Neon table.

This tool intentionally lives under tools/, not upload/. Project guardrails
forbid modifying or running upload/*.py in Codex ops sessions.

Safe modes:
  --emit-sql          print schema SQL only
  --check-env         report whether required DB env is present, no connection
  --inspect-table     read-only count/schema summary from Neon
  --dry-run-upsert    open DB transaction, create/upsert, then ROLLBACK

Live-write modes require --confirm-db-write:
  --create-table      CREATE EXTENSION/TABLE/INDEX and COMMIT
  --upsert            CREATE EXTENSION/TABLE/INDEX, UPSERT rows, and COMMIT
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.canonical_v2_upload_validator import iter_buildings, map_row, validate_rows  # noqa: E402


DEFAULT_INPUT = ROOT / "data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.resume10_complete.json"
DEFAULT_REPORT = ROOT / "data/reports/canonical_v2_neon_loader_report.json"
TABLE = "canonical_v2_buildings"

SCHEMA_SQL = f"""
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS {TABLE} (
    canonical_bld_id              TEXT        PRIMARY KEY,
    name                          TEXT        NOT NULL,
    names_alts                    TEXT[]      NOT NULL DEFAULT '{{}}',
    location_city                 TEXT,
    location_country              TEXT,
    project_year                  INTEGER,
    architect_canonical_ids       TEXT[]      NOT NULL DEFAULT '{{}}',
    architect_names               TEXT[]      NOT NULL DEFAULT '{{}}',
    architects_text               TEXT,
    program                       TEXT        NOT NULL,
    style                         TEXT        NOT NULL,
    color_tone                    TEXT        NOT NULL,
    atmosphere                    TEXT        NOT NULL,
    material_visual               TEXT[]      NOT NULL DEFAULT '{{}}',
    visual_description            TEXT        NOT NULL,
    image_derived                 JSONB       NOT NULL DEFAULT '{{}}'::jsonb,
    covers_by_type                JSONB       NOT NULL DEFAULT '{{}}'::jsonb,
    all_images                    JSONB       NOT NULL DEFAULT '[]'::jsonb,
    best_image_per_cluster        JSONB       NOT NULL DEFAULT '{{}}'::jsonb,
    source_refs                   JSONB       NOT NULL,
    source_urls                   JSONB       NOT NULL DEFAULT '{{}}'::jsonb,
    identity_source               TEXT,
    confidence_tier               TEXT        NOT NULL CHECK (confidence_tier IN ('T1','T2','T3')),
    n_sources                     INTEGER     NOT NULL CHECK (n_sources >= 1),
    cover_image_url_default       TEXT,
    cover_image_cdn_url           TEXT,
    cover_blurhash                TEXT,
    display_cover_url             TEXT,
    is_publishable                BOOLEAN     NOT NULL DEFAULT FALSE,
    publishability_reasons        TEXT[]      NOT NULL DEFAULT '{{}}',
    needs_image_derived_backfill  BOOLEAN     NOT NULL DEFAULT FALSE,
    typology_primary              TEXT,
    typology_primary_source       TEXT,
    typology_tags                 TEXT[]      NOT NULL DEFAULT '{{}}',
    architectural_elements        TEXT[]      NOT NULL DEFAULT '{{}}',
    source_categories             JSONB       NOT NULL DEFAULT '{{}}'::jsonb,
    embedding                     VECTOR(384) NOT NULL,
    created_at                    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_{TABLE}_country
    ON {TABLE} (location_country);
CREATE INDEX IF NOT EXISTS idx_{TABLE}_city
    ON {TABLE} (location_city);
CREATE INDEX IF NOT EXISTS idx_{TABLE}_year
    ON {TABLE} (project_year);
CREATE INDEX IF NOT EXISTS idx_{TABLE}_program
    ON {TABLE} (program);
CREATE INDEX IF NOT EXISTS idx_{TABLE}_style
    ON {TABLE} (style);
CREATE INDEX IF NOT EXISTS idx_{TABLE}_tier
    ON {TABLE} (confidence_tier);
CREATE INDEX IF NOT EXISTS idx_{TABLE}_publish
    ON {TABLE} (is_publishable);
CREATE INDEX IF NOT EXISTS idx_{TABLE}_arch_ids
    ON {TABLE} USING GIN (architect_canonical_ids);
CREATE INDEX IF NOT EXISTS idx_{TABLE}_source_refs
    ON {TABLE} USING GIN (source_refs);
CREATE INDEX IF NOT EXISTS idx_{TABLE}_embedding_hnsw
    ON {TABLE} USING hnsw (embedding vector_cosine_ops);
"""

# Runs after SCHEMA_SQL — additive migration for a live table. New-column
# indexes live here (not in SCHEMA_SQL) because the columns must exist before
# CREATE INDEX; on a fresh table SCHEMA_SQL already created the columns and
# these IF NOT EXISTS statements are no-ops.
SCHEMA_EVOLUTION_SQL = f"""
ALTER TABLE {TABLE}
    ADD COLUMN IF NOT EXISTS cover_image_cdn_url TEXT,
    ADD COLUMN IF NOT EXISTS cover_blurhash TEXT,
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS typology_primary TEXT,
    ADD COLUMN IF NOT EXISTS typology_primary_source TEXT,
    ADD COLUMN IF NOT EXISTS typology_tags TEXT[] NOT NULL DEFAULT '{{}}',
    ADD COLUMN IF NOT EXISTS architectural_elements TEXT[] NOT NULL DEFAULT '{{}}',
    ADD COLUMN IF NOT EXISTS source_categories JSONB NOT NULL DEFAULT '{{}}'::jsonb;

CREATE INDEX IF NOT EXISTS idx_{TABLE}_typology_primary
    ON {TABLE} (typology_primary);
CREATE INDEX IF NOT EXISTS idx_{TABLE}_typology_tags
    ON {TABLE} USING GIN (typology_tags);
CREATE INDEX IF NOT EXISTS idx_{TABLE}_arch_elements
    ON {TABLE} USING GIN (architectural_elements);
CREATE INDEX IF NOT EXISTS idx_{TABLE}_source_categories
    ON {TABLE} USING GIN (source_categories);
"""

COLUMNS = (
    "canonical_bld_id",
    "name",
    "names_alts",
    "location_city",
    "location_country",
    "project_year",
    "architect_canonical_ids",
    "architect_names",
    "architects_text",
    "program",
    "style",
    "color_tone",
    "atmosphere",
    "material_visual",
    "visual_description",
    "image_derived",
    "covers_by_type",
    "all_images",
    "best_image_per_cluster",
    "source_refs",
    "source_urls",
    "identity_source",
    "confidence_tier",
    "n_sources",
    "cover_image_url_default",
    "display_cover_url",
    "is_publishable",
    "publishability_reasons",
    "needs_image_derived_backfill",
    "typology_primary",
    "typology_primary_source",
    "typology_tags",
    "architectural_elements",
    "source_categories",
    "embedding",
)

UPSERT_SQL = f"""
INSERT INTO {TABLE} ({", ".join(COLUMNS)}, created_at, updated_at)
VALUES %s
ON CONFLICT (canonical_bld_id) DO UPDATE SET
    name                         = EXCLUDED.name,
    names_alts                   = EXCLUDED.names_alts,
    location_city                = EXCLUDED.location_city,
    location_country             = EXCLUDED.location_country,
    project_year                 = EXCLUDED.project_year,
    architect_canonical_ids      = EXCLUDED.architect_canonical_ids,
    architect_names              = EXCLUDED.architect_names,
    architects_text              = EXCLUDED.architects_text,
    program                      = EXCLUDED.program,
    style                        = EXCLUDED.style,
    color_tone                   = EXCLUDED.color_tone,
    atmosphere                   = EXCLUDED.atmosphere,
    material_visual              = EXCLUDED.material_visual,
    visual_description           = EXCLUDED.visual_description,
    image_derived                = EXCLUDED.image_derived,
    covers_by_type               = EXCLUDED.covers_by_type,
    all_images                   = EXCLUDED.all_images,
    best_image_per_cluster       = EXCLUDED.best_image_per_cluster,
    source_refs                  = EXCLUDED.source_refs,
    source_urls                  = EXCLUDED.source_urls,
    identity_source              = EXCLUDED.identity_source,
    confidence_tier              = EXCLUDED.confidence_tier,
    n_sources                    = EXCLUDED.n_sources,
    cover_image_url_default      = EXCLUDED.cover_image_url_default,
    display_cover_url            = EXCLUDED.display_cover_url,
    is_publishable               = EXCLUDED.is_publishable,
    publishability_reasons       = EXCLUDED.publishability_reasons,
    needs_image_derived_backfill = EXCLUDED.needs_image_derived_backfill,
    typology_primary             = EXCLUDED.typology_primary,
    typology_primary_source      = EXCLUDED.typology_primary_source,
    typology_tags                = EXCLUDED.typology_tags,
    architectural_elements       = EXCLUDED.architectural_elements,
    source_categories            = EXCLUDED.source_categories,
    embedding                    = EXCLUDED.embedding,
    updated_at                   = NOW();
"""


def _load_env() -> None:
    load_dotenv(ROOT / ".env")


def _env_report() -> dict[str, Any]:
    _load_env()
    url_key = "DATABASE_URL" if os.environ.get("DATABASE_URL") else "DB_URL" if os.environ.get("DB_URL") else None
    discrete_keys = ["DB_HOST", "DB_PORT", "DB_USER", "DB_NAME"]
    missing_discrete = [key for key in discrete_keys if not os.environ.get(key)]
    return {
        "has_database_url": bool(url_key),
        "database_url_key": url_key,
        "has_discrete_connection": not missing_discrete,
        "missing_discrete_keys": missing_discrete,
        "has_password": bool(os.environ.get("DB_PASSWORD")) or bool(url_key),
        "sslmode": os.environ.get("DB_SSLMODE", "require"),
    }


def _connect():
    report = _env_report()
    if report["has_database_url"]:
        return psycopg2.connect(os.environ[report["database_url_key"]])
    if report["missing_discrete_keys"]:
        missing = ", ".join(report["missing_discrete_keys"])
        raise SystemExit(f"missing DB env keys: {missing}; set DATABASE_URL or DB_* keys")
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ["DB_PORT"]),
        user=os.environ["DB_USER"],
        password=os.environ.get("DB_PASSWORD", ""),
        dbname=os.environ["DB_NAME"],
        sslmode=os.environ.get("DB_SSLMODE", "require"),
    )


def _vec_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{float(value):.8f}" for value in values) + "]"


def _row_tuple(mapped: dict[str, Any]) -> tuple[Any, ...]:
    return (
        mapped["canonical_bld_id"],
        mapped["name"],
        mapped["names_alts"],
        mapped["location_city"],
        mapped["location_country"],
        mapped["project_year"],
        mapped["architect_canonical_ids"],
        mapped["architect_names"],
        mapped["architects_text"],
        mapped["program"],
        mapped["style"],
        mapped["color_tone"],
        mapped["atmosphere"],
        mapped["material_visual"],
        mapped["visual_description"],
        psycopg2.extras.Json(mapped["image_derived"]),
        psycopg2.extras.Json(mapped["covers_by_type"]),
        psycopg2.extras.Json(mapped["all_images"]),
        psycopg2.extras.Json(mapped["best_image_per_cluster"]),
        psycopg2.extras.Json(mapped["source_refs"]),
        psycopg2.extras.Json(mapped["source_urls"]),
        mapped["identity_source"],
        mapped["confidence_tier"],
        mapped["n_sources"],
        mapped["cover_image_url_default"],
        mapped["display_cover_url"],
        mapped["is_publishable"],
        mapped["publishability_reasons"],
        mapped["needs_image_derived_backfill"],
        mapped["typology_primary"],
        mapped["typology_primary_source"],
        mapped["typology_tags"],
        mapped["architectural_elements"],
        psycopg2.extras.Json(mapped["source_categories"]),
        _vec_literal(mapped["embedding"]),
    )


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _read_removed_ids(path: Path | None) -> list[str]:
    """Extract removed_canonical_ids from a C10 recovery report; [] if no path."""
    if path is None:
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [str(x) for x in (data.get("removed_canonical_ids") or [])]


def _preflight(input_path: Path, limit: int | None) -> dict[str, Any]:
    return validate_rows(iter_buildings(input_path, limit=limit))


def _load_rows(
    *,
    input_path: Path,
    limit: int | None,
    batch_size: int,
    dry_run: bool,
    removed_ids: list[str] | None = None,
) -> dict[str, Any]:
    conn = _connect()
    total = 0
    failed = 0
    deleted = 0
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
            cur.execute(SCHEMA_EVOLUTION_SQL)
            # DELETE merged-away ids before the upsert — DELETE-first so a
            # stray id is reinstated by the upsert rather than lost.
            if removed_ids:
                cur.execute(
                    f"DELETE FROM {TABLE} WHERE canonical_bld_id = ANY(%s)",
                    (list(removed_ids),),
                )
                deleted = cur.rowcount
            batch: list[tuple[Any, ...]] = []
            for raw in iter_buildings(input_path, limit=limit):
                try:
                    batch.append(_row_tuple(map_row(raw)))
                except Exception:
                    failed += 1
                    continue
                if len(batch) >= batch_size:
                    psycopg2.extras.execute_values(
                        cur,
                        UPSERT_SQL,
                        batch,
                        template="(" + ",".join(["%s"] * len(COLUMNS)) + ", NOW(), NOW())",
                    )
                    total += len(batch)
                    batch = []
            if batch:
                psycopg2.extras.execute_values(
                    cur,
                    UPSERT_SQL,
                    batch,
                    template="(" + ",".join(["%s"] * len(COLUMNS)) + ", NOW(), NOW())",
                )
                total += len(batch)
            cur.execute(
                f"""
                SELECT
                    COUNT(*) AS total_rows,
                    COUNT(DISTINCT canonical_bld_id) AS unique_pk,
                    COUNT(*) FILTER (WHERE is_publishable) AS publishable_rows,
                    COUNT(*) FILTER (WHERE NOT is_publishable) AS nonpublishable_rows,
                    COUNT(*) FILTER (WHERE embedding IS NULL) AS missing_embedding,
                    COUNT(*) FILTER (WHERE display_cover_url IS NULL) AS missing_display_cover_url,
                    COUNT(*) FILTER (WHERE needs_image_derived_backfill) AS needs_image_derived_backfill
                FROM {TABLE}
                """
            )
            row = cur.fetchone()
            counts = {desc.name: row[idx] for idx, desc in enumerate(cur.description)}
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
        return {
            "status": "PASS" if failed == 0 else "WARN",
            "mode": "dry-run-upsert" if dry_run else "upsert",
            "input": str(input_path),
            "limit": limit,
            "rows_deleted": deleted,
            "rows_loaded_in_transaction": total,
            "row_mapping_failures": failed,
            "counts_seen_in_transaction": counts,
            "writes": "rolled back" if dry_run else "committed",
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _create_table(*, dry_run: bool = False) -> dict[str, Any]:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
            cur.execute(SCHEMA_EVOLUTION_SQL)
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
        return {
            "status": "PASS",
            "mode": "create-table",
            "table": TABLE,
            "writes": "rolled back" if dry_run else "committed",
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _inspect_table() -> dict[str, Any]:
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT column_name, data_type, udt_name, is_nullable
                FROM information_schema.columns
                WHERE table_name = %s
                ORDER BY ordinal_position
                """,
                (TABLE,),
            )
            columns = [dict(row) for row in cur.fetchall()]
            if not columns:
                return {"status": "MISSING", "mode": "inspect-table", "table": TABLE}
            cur.execute(
                f"""
                SELECT
                    COUNT(*) AS total_rows,
                    COUNT(DISTINCT canonical_bld_id) AS unique_pk,
                    COUNT(*) FILTER (WHERE is_publishable) AS publishable_rows,
                    COUNT(*) FILTER (WHERE NOT is_publishable) AS nonpublishable_rows,
                    COUNT(*) FILTER (WHERE embedding IS NULL) AS missing_embedding,
                    COUNT(*) FILTER (WHERE display_cover_url IS NULL) AS missing_display_cover_url,
                    COUNT(*) FILTER (WHERE needs_image_derived_backfill) AS needs_image_derived_backfill,
                    MIN(updated_at) AS min_updated_at,
                    MAX(updated_at) AS max_updated_at
                FROM {TABLE}
                """
            )
            counts = dict(cur.fetchone())
        return {
            "status": "PASS",
            "mode": "inspect-table",
            "table": TABLE,
            "columns": columns,
            "counts": counts,
            "writes": "none; read-only inspect",
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare or load canonical_v2_buildings into Neon.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--emit-sql", action="store_true")
    mode.add_argument("--check-env", action="store_true")
    mode.add_argument("--inspect-table", action="store_true")
    mode.add_argument("--dry-run-upsert", action="store_true")
    mode.add_argument("--create-table", action="store_true")
    mode.add_argument("--upsert", action="store_true")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--confirm-db-write", action="store_true")
    parser.add_argument("--removed-ids", type=Path, default=None,
                        help="C10 recovery report JSON; its removed_canonical_ids "
                             "are DELETEd from the table before upsert")
    args = parser.parse_args()
    removed_ids = _read_removed_ids(args.removed_ids)

    if args.emit_sql:
        print(SCHEMA_SQL.strip())
        print("\n-- additive migration (runs after SCHEMA_SQL on every load) --")
        print(SCHEMA_EVOLUTION_SQL.strip())
        return 0

    if args.check_env:
        report = {"status": "PASS", "mode": "check-env", **_env_report()}
        _write_report(args.report, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if args.inspect_table:
        report = _inspect_table()
        _write_report(args.report, report)
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0 if report["status"] == "PASS" else 1

    if args.create_table or args.upsert:
        if not args.confirm_db_write:
            raise SystemExit("--confirm-db-write is required for committed DB writes")

    if args.dry_run_upsert or args.upsert:
        preflight = _preflight(args.input, args.limit)
        if preflight["status"] != "PASS":
            report = {
                "status": "FAIL",
                "mode": "preflight",
                "input": str(args.input),
                "limit": args.limit,
                "preflight": preflight,
            }
            _write_report(args.report, report)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 1

    if args.create_table:
        report = _create_table()
    elif args.dry_run_upsert:
        report = _load_rows(
            input_path=args.input,
            limit=args.limit,
            batch_size=args.batch_size,
            dry_run=True,
            removed_ids=removed_ids,
        )
    else:
        report = _load_rows(
            input_path=args.input,
            limit=args.limit,
            batch_size=args.batch_size,
            dry_run=False,
            removed_ids=removed_ids,
        )

    _write_report(args.report, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
