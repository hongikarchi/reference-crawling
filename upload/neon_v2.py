#!/usr/bin/env python3
"""Upload canonical_buildings_strict_embedded.patched.json → Postgres canonical_v2_buildings.

Per .claude/CLAUDE.md: never run without explicit user approval.
  python3 -m upload.neon_v2 --dry-run                # validate, no writes
  python3 -m upload.neon_v2 --create-table-only      # CREATE TABLE only
  python3 -m upload.neon_v2 --confirm                # full UPSERT all rows
  python3 -m upload.neon_v2 --confirm --limit 5      # smoke test (first 5 rows)

Idempotent: re-runs UPSERT on canonical_bld_id PK.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

from tools.canonical_v2_upload_validator import iter_buildings, map_row


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "data/canonical/country_conflict_refresh/canonical_buildings_strict_embedded.patched.json"
TABLE = "canonical_v2_buildings"


CREATE_EXTENSION_SQL = "CREATE EXTENSION IF NOT EXISTS vector;"


CREATE_TABLE_SQL = f"""
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
    material_visual               TEXT[]      NOT NULL,
    visual_description            TEXT        NOT NULL,
    image_derived                 JSONB       NOT NULL DEFAULT '{{}}',
    covers_by_type                JSONB       NOT NULL,
    all_images                    JSONB       NOT NULL DEFAULT '[]',
    best_image_per_cluster        JSONB       NOT NULL DEFAULT '{{}}',
    source_refs                   JSONB       NOT NULL,
    source_urls                   JSONB       NOT NULL DEFAULT '{{}}',
    identity_source               TEXT,
    confidence_tier               TEXT        NOT NULL CHECK (confidence_tier IN ('T1','T2','T3')),
    n_sources                     INTEGER     NOT NULL,
    cover_image_url_default       TEXT,
    display_cover_url             TEXT,
    is_publishable                BOOLEAN     NOT NULL DEFAULT FALSE,
    publishability_reasons        TEXT[]      NOT NULL DEFAULT '{{}}',
    needs_image_derived_backfill  BOOLEAN     NOT NULL DEFAULT FALSE,
    embedding                     VECTOR(384) NOT NULL,
    updated_at                    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_{TABLE}_country  ON {TABLE} (location_country);
CREATE INDEX IF NOT EXISTS idx_{TABLE}_year     ON {TABLE} (project_year);
CREATE INDEX IF NOT EXISTS idx_{TABLE}_program  ON {TABLE} (program);
CREATE INDEX IF NOT EXISTS idx_{TABLE}_style    ON {TABLE} (style);
CREATE INDEX IF NOT EXISTS idx_{TABLE}_tier     ON {TABLE} (confidence_tier);
CREATE INDEX IF NOT EXISTS idx_{TABLE}_publish  ON {TABLE} (is_publishable);
CREATE INDEX IF NOT EXISTS idx_{TABLE}_arch_ids ON {TABLE} USING GIN (architect_canonical_ids);
"""


COLUMNS = (
    "canonical_bld_id", "name", "names_alts",
    "location_city", "location_country", "project_year",
    "architect_canonical_ids", "architect_names", "architects_text",
    "program", "style", "color_tone", "atmosphere",
    "material_visual", "visual_description",
    "image_derived", "covers_by_type", "all_images", "best_image_per_cluster",
    "source_refs", "source_urls",
    "identity_source", "confidence_tier", "n_sources",
    "cover_image_url_default", "display_cover_url",
    "is_publishable", "publishability_reasons", "needs_image_derived_backfill",
    "embedding",
)


UPSERT_SQL = f"""
INSERT INTO {TABLE} ({", ".join(COLUMNS)}, updated_at)
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
    embedding                    = EXCLUDED.embedding,
    updated_at                   = NOW();
"""


def _connect():
    load_dotenv(ROOT / ".env")
    required = ["DB_HOST", "DB_PORT", "DB_USER", "DB_NAME"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"ERROR: Missing .env keys: {', '.join(missing)}", file=sys.stderr)
        sys.exit(2)
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ["DB_PORT"]),
        user=os.environ["DB_USER"],
        password=os.environ.get("DB_PASSWORD", ""),
        dbname=os.environ["DB_NAME"],
        sslmode=os.environ.get("DB_SSLMODE", "require"),
    )


def _vec_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{v:.8f}" for v in values) + "]"


def _row_tuple(mapped: dict) -> tuple:
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
        _vec_literal(mapped["embedding"]),
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    p.add_argument("--dry-run", action="store_true",
                   help="BEGIN + apply + ROLLBACK (no permanent writes)")
    p.add_argument("--confirm", action="store_true",
                   help="actually commit. required for non-dry-run.")
    p.add_argument("--create-table-only", action="store_true")
    p.add_argument("--limit", type=int, default=None,
                   help="upload only first N rows (smoke test)")
    p.add_argument("--batch-size", type=int, default=200)
    args = p.parse_args()

    if not args.dry_run and not args.confirm and not args.create_table_only:
        print("ERROR: must pass --dry-run, --create-table-only, or --confirm",
              file=sys.stderr)
        sys.exit(2)

    if not args.input.exists() and not args.create_table_only:
        print(f"ERROR: {args.input} not found", file=sys.stderr)
        sys.exit(2)

    print(f"target: {os.environ.get('DB_HOST', '?')} / table {TABLE}")
    if not args.create_table_only:
        print(f"input:  {args.input}")
    print(f"mode:   {'DRY-RUN' if args.dry_run else ('CREATE-TABLE-ONLY' if args.create_table_only else 'CONFIRM (real upsert)')}")

    conn = _connect()
    cur = conn.cursor()

    print(f"\n[1/3] CREATE EXTENSION + CREATE TABLE …")
    cur.execute(CREATE_EXTENSION_SQL)
    cur.execute(CREATE_TABLE_SQL)
    if args.create_table_only:
        conn.commit()
        cur.close(); conn.close()
        print("done. table created (or already existed). no rows inserted.")
        return

    print(f"\n[2/3] streaming rows from {args.input.name} (limit={args.limit}) …")
    batch: list[tuple] = []
    total = 0
    failed = 0
    for raw in iter_buildings(args.input, limit=args.limit):
        try:
            mapped = map_row(raw)
            batch.append(_row_tuple(mapped))
        except Exception as exc:
            failed += 1
            if failed <= 5:
                print(f"  row failed: {raw.get('canonical_bld_id')!r}: {exc}",
                      file=sys.stderr)
            continue
        if len(batch) >= args.batch_size:
            psycopg2.extras.execute_values(
                cur, UPSERT_SQL, batch,
                template="(" + ",".join(["%s"] * len(COLUMNS)) + ", NOW())",
            )
            total += len(batch)
            batch = []
            if total % 1000 == 0:
                print(f"  {total} rows upserted")
    if batch:
        psycopg2.extras.execute_values(
            cur, UPSERT_SQL, batch,
            template="(" + ",".join(["%s"] * len(COLUMNS)) + ", NOW())",
        )
        total += len(batch)

    cur.execute(f"SELECT COUNT(*) FROM {TABLE};")
    table_count = cur.fetchone()[0]

    print(f"\n[3/3] {'DRY-RUN ROLLBACK' if args.dry_run else 'COMMIT'}")
    if args.dry_run:
        conn.rollback()
        print(f"   rolled back. would have upserted {total} rows.")
        print(f"   table count BEFORE this run: {table_count} "
              f"(includes uncommitted {total} from this dry-run)")
    else:
        conn.commit()
        print(f"   committed {total} rows.")
        print(f"   table count NOW: {table_count}")

    if failed:
        print(f"\n  WARN: {failed} rows failed mapping (skipped).")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
