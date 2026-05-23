#!/usr/bin/env python3
"""Load canonical_v2 architects (firms) into Neon.

Modes:
  --inspect-table     read-only count/schema summary from Neon
  --dry-run-upsert    open DB transaction, create/upsert, then ROLLBACK
  --create-table      idempotent CREATE TABLE only
  --upsert            committed write (requires --confirm-db-write)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.canonical_v2_neon_loader import _connect, _vec_literal  # noqa: E402

TABLE = "canonical_v2_architects"
DEFAULT_INPUT = ROOT / "data/canonical/canonical_architects_v2.json"

SCHEMA_SQL = f"""
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS {TABLE} (
    canonical_arch_id        TEXT PRIMARY KEY,
    canonical_name           TEXT        NOT NULL,
    name_alts                TEXT[]      NOT NULL DEFAULT '{{}}',
    description              TEXT,
    primary_country          TEXT,
    primary_city             TEXT,
    office_locations         JSONB       NOT NULL DEFAULT '[]'::jsonb,
    website                  TEXT,
    email                    TEXT,
    phone                    TEXT,
    social_links             JSONB       NOT NULL DEFAULT '{{}}'::jsonb,
    building_ids             TEXT[]      NOT NULL DEFAULT '{{}}',
    n_buildings              INTEGER     NOT NULL DEFAULT 0,
    n_buildings_publishable  INTEGER     NOT NULL DEFAULT 0,
    countries                TEXT[]      NOT NULL DEFAULT '{{}}',
    cities                   TEXT[]      NOT NULL DEFAULT '{{}}',
    top_programs             TEXT[]      NOT NULL DEFAULT '{{}}',
    top_styles               TEXT[]      NOT NULL DEFAULT '{{}}',
    top_color_tones          TEXT[]      NOT NULL DEFAULT '{{}}',
    top_atmospheres          TEXT[]      NOT NULL DEFAULT '{{}}',
    top_materials            TEXT[]      NOT NULL DEFAULT '{{}}',
    top_typologies           TEXT[]      NOT NULL DEFAULT '{{}}',
    top_arch_elements        TEXT[]      NOT NULL DEFAULT '{{}}',
    feature_distribution     JSONB       NOT NULL DEFAULT '{{}}'::jsonb,
    earliest_project_year    INTEGER,
    latest_project_year      INTEGER,
    source_refs              JSONB       NOT NULL,
    source_urls              JSONB       NOT NULL DEFAULT '{{}}'::jsonb,
    source_descriptions      JSONB       NOT NULL DEFAULT '{{}}'::jsonb,
    n_sources                INTEGER     NOT NULL CHECK (n_sources >= 1),
    confidence_tier          TEXT        NOT NULL CHECK (confidence_tier IN ('T1','T2','T3')),
    logo_url                 TEXT,
    hero_building_id         TEXT,
    portfolio_embedding      VECTOR(384) NOT NULL,
    is_recommendable         BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_{TABLE}_country
    ON {TABLE} (primary_country);
CREATE INDEX IF NOT EXISTS idx_{TABLE}_city
    ON {TABLE} (primary_city);
CREATE INDEX IF NOT EXISTS idx_{TABLE}_recommend
    ON {TABLE} (is_recommendable);
CREATE INDEX IF NOT EXISTS idx_{TABLE}_tier
    ON {TABLE} (confidence_tier);
CREATE INDEX IF NOT EXISTS idx_{TABLE}_program
    ON {TABLE} USING GIN (top_programs);
CREATE INDEX IF NOT EXISTS idx_{TABLE}_typology
    ON {TABLE} USING GIN (top_typologies);
CREATE INDEX IF NOT EXISTS idx_{TABLE}_country_arr
    ON {TABLE} USING GIN (countries);
CREATE INDEX IF NOT EXISTS idx_{TABLE}_embed_hnsw
    ON {TABLE} USING hnsw (portfolio_embedding vector_cosine_ops);
"""

COLUMNS = (
    "canonical_arch_id", "canonical_name", "name_alts", "description",
    "primary_country", "primary_city", "office_locations",
    "website", "email", "phone", "social_links",
    "building_ids", "n_buildings", "n_buildings_publishable",
    "countries", "cities",
    "top_programs", "top_styles", "top_color_tones", "top_atmospheres",
    "top_materials", "top_typologies", "top_arch_elements",
    "feature_distribution",
    "earliest_project_year", "latest_project_year",
    "source_refs", "source_urls", "source_descriptions",
    "n_sources", "confidence_tier",
    "logo_url", "hero_building_id",
    "portfolio_embedding", "is_recommendable",
)

UPSERT_SQL = f"""
INSERT INTO {TABLE} ({", ".join(COLUMNS)}, created_at, updated_at)
VALUES %s
ON CONFLICT (canonical_arch_id) DO UPDATE SET
    canonical_name          = EXCLUDED.canonical_name,
    name_alts               = EXCLUDED.name_alts,
    description             = EXCLUDED.description,
    primary_country         = EXCLUDED.primary_country,
    primary_city            = EXCLUDED.primary_city,
    office_locations        = EXCLUDED.office_locations,
    website                 = EXCLUDED.website,
    email                   = EXCLUDED.email,
    phone                   = EXCLUDED.phone,
    social_links            = EXCLUDED.social_links,
    building_ids            = EXCLUDED.building_ids,
    n_buildings             = EXCLUDED.n_buildings,
    n_buildings_publishable = EXCLUDED.n_buildings_publishable,
    countries               = EXCLUDED.countries,
    cities                  = EXCLUDED.cities,
    top_programs            = EXCLUDED.top_programs,
    top_styles              = EXCLUDED.top_styles,
    top_color_tones         = EXCLUDED.top_color_tones,
    top_atmospheres         = EXCLUDED.top_atmospheres,
    top_materials           = EXCLUDED.top_materials,
    top_typologies          = EXCLUDED.top_typologies,
    top_arch_elements       = EXCLUDED.top_arch_elements,
    feature_distribution    = EXCLUDED.feature_distribution,
    earliest_project_year   = EXCLUDED.earliest_project_year,
    latest_project_year     = EXCLUDED.latest_project_year,
    source_refs             = EXCLUDED.source_refs,
    source_urls             = EXCLUDED.source_urls,
    source_descriptions     = EXCLUDED.source_descriptions,
    n_sources               = EXCLUDED.n_sources,
    confidence_tier         = EXCLUDED.confidence_tier,
    logo_url                = EXCLUDED.logo_url,
    hero_building_id        = EXCLUDED.hero_building_id,
    portfolio_embedding     = EXCLUDED.portfolio_embedding,
    is_recommendable        = EXCLUDED.is_recommendable,
    updated_at              = NOW();
"""


def iter_architects(path: Path):
    data = json.load(path.open(encoding="utf-8"))
    for r in data.get("architects", []):
        yield r


def _row_tuple(r: dict[str, Any]):
    return (
        r["canonical_arch_id"],
        r["canonical_name"],
        r.get("name_alts") or [],
        r.get("description"),
        r.get("primary_country"),
        r.get("primary_city"),
        psycopg2.extras.Json(r.get("office_locations") or []),
        r.get("website"),
        r.get("email"),
        r.get("phone"),
        psycopg2.extras.Json(r.get("social_links") or {}),
        r.get("building_ids") or [],
        r.get("n_buildings", 0),
        r.get("n_buildings_publishable", 0),
        r.get("countries") or [],
        r.get("cities") or [],
        r.get("top_programs") or [],
        r.get("top_styles") or [],
        r.get("top_color_tones") or [],
        r.get("top_atmospheres") or [],
        r.get("top_materials") or [],
        r.get("top_typologies") or [],
        r.get("top_arch_elements") or [],
        psycopg2.extras.Json(r.get("feature_distribution") or {}),
        r.get("earliest_project_year"),
        r.get("latest_project_year"),
        psycopg2.extras.Json(r.get("source_refs") or {}),
        psycopg2.extras.Json(r.get("source_urls") or {}),
        psycopg2.extras.Json(r.get("source_descriptions") or {}),
        r["n_sources"],
        r["confidence_tier"],
        r.get("logo_url"),
        r.get("hero_building_id"),
        _vec_literal(r["portfolio_embedding"]),
        bool(r.get("is_recommendable")),
    )


def _inspect(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE is_recommendable) AS recommendable, "
            f"COUNT(DISTINCT primary_country) AS countries, "
            f"COUNT(*) FILTER (WHERE n_buildings_publishable >= 3) AS gte3_pub "
            f"FROM {TABLE}"
        )
        row = cur.fetchone()
        return {"total": row[0], "recommendable": row[1],
                "distinct_countries": row[2], "n_buildings_publishable_gte_3": row[3]}


def _load_rows(input_path, dry_run, batch_size=100):
    conn = _connect()
    n_loaded = 0
    counts = {}
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
            batch = []
            for r in iter_architects(input_path):
                tup = _row_tuple(r) + (datetime.now(timezone.utc), datetime.now(timezone.utc))
                batch.append(tup)
                if len(batch) >= batch_size:
                    psycopg2.extras.execute_values(cur, UPSERT_SQL, batch, page_size=batch_size)
                    n_loaded += len(batch)
                    batch.clear()
                    if not dry_run and n_loaded % 1000 == 0:
                        # commit per 1000 to avoid huge transaction + SSL timeout
                        conn.commit()
                        print(f"  committed {n_loaded} rows", file=sys.stderr)
            if batch:
                psycopg2.extras.execute_values(cur, UPSERT_SQL, batch, page_size=batch_size)
                n_loaded += len(batch)
            counts = _inspect(conn)
            if dry_run:
                conn.rollback()
            else:
                conn.commit()
    finally:
        conn.close()
    return n_loaded, counts


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--inspect-table", action="store_true")
    mode.add_argument("--create-table", action="store_true")
    mode.add_argument("--dry-run-upsert", action="store_true")
    mode.add_argument("--upsert", action="store_true")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--confirm-db-write", action="store_true")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")

    if args.inspect_table:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT EXISTS (SELECT FROM information_schema.tables "
                    f"WHERE table_name = '{TABLE}')"
                )
                exists = cur.fetchone()[0]
            if not exists:
                print(json.dumps({"table": TABLE, "exists": False}, indent=2))
                return 0
            counts = _inspect(conn)
            print(json.dumps({"table": TABLE, "exists": True, "counts": counts},
                             indent=2))
        finally:
            conn.close()
        return 0

    if args.create_table:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
                conn.commit()
                print(json.dumps({"status": "PASS", "mode": "create-table"}, indent=2))
        finally:
            conn.close()
        return 0

    if args.upsert and not args.confirm_db_write:
        raise SystemExit("--confirm-db-write required for committed write")

    dry_run = args.dry_run_upsert
    n_loaded, counts = _load_rows(args.input, dry_run=dry_run)
    print(json.dumps({
        "status": "PASS",
        "mode": "dry-run-upsert" if dry_run else "upsert",
        "input": str(args.input.relative_to(ROOT)),
        "rows_loaded_in_transaction": n_loaded,
        "counts_seen_in_transaction": counts,
        "writes": "rolled back" if dry_run else "committed",
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
