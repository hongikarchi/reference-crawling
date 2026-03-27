#!/usr/bin/env python3
"""Stage 3: Bulk INSERT 4_buildings_processed.json into PostgreSQL architecture_vectors.

Usage:
    python stage3_postgres.py           # UPSERT (insert new, update existing)
    python stage3_postgres.py --reset   # DROP + recreate table, then insert

Requires .env with:
    DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME
"""

import argparse
import json
import os
import sys

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

import config

DATA_DIR = os.path.join(config.BASE_DIR, "data")
PROCESSED_JSON = os.path.join(DATA_DIR, "4_buildings_processed.json")

CREATE_TABLE_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS architecture_vectors (
    building_id      TEXT PRIMARY KEY,
    slug             TEXT UNIQUE NOT NULL,
    name_en          TEXT NOT NULL,
    project_name     TEXT NOT NULL,
    architect        TEXT,
    location_country TEXT,
    city             TEXT,
    year             INTEGER,
    area_sqm         NUMERIC,
    program          TEXT NOT NULL,
    mood             TEXT NOT NULL,
    material         TEXT NOT NULL,
    description      TEXT,
    url              TEXT,
    tags             TEXT[],
    source_slugs     TEXT[],
    image_cover      TEXT,
    embedding        VECTOR(384) NOT NULL
);
"""

SCALAR_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_av_program  ON architecture_vectors (program);
CREATE INDEX IF NOT EXISTS idx_av_country  ON architecture_vectors (location_country);
CREATE INDEX IF NOT EXISTS idx_av_year     ON architecture_vectors (year);
"""

UPSERT_SQL = """
INSERT INTO architecture_vectors (
    building_id, slug, name_en, project_name, architect,
    location_country, city, year, area_sqm, program, mood, material,
    description, url, tags, source_slugs, image_cover, embedding
) VALUES (
    %(building_id)s, %(slug)s, %(name_en)s, %(project_name)s, %(architect)s,
    %(location_country)s, %(city)s, %(year)s, %(area_sqm)s, %(program)s,
    %(mood)s, %(material)s, %(description)s, %(url)s, %(tags)s,
    %(source_slugs)s, %(image_cover)s, %(embedding)s
)
ON CONFLICT (building_id) DO UPDATE SET
    slug             = EXCLUDED.slug,
    name_en          = EXCLUDED.name_en,
    project_name     = EXCLUDED.project_name,
    architect        = EXCLUDED.architect,
    location_country = EXCLUDED.location_country,
    city             = EXCLUDED.city,
    year             = EXCLUDED.year,
    area_sqm         = EXCLUDED.area_sqm,
    program          = EXCLUDED.program,
    mood             = EXCLUDED.mood,
    material         = EXCLUDED.material,
    description      = EXCLUDED.description,
    url              = EXCLUDED.url,
    tags             = EXCLUDED.tags,
    source_slugs     = EXCLUDED.source_slugs,
    image_cover      = EXCLUDED.image_cover,
    embedding        = EXCLUDED.embedding;
"""


def get_db_connection():
    load_dotenv(os.path.join(config.BASE_DIR, ".env"))
    required = ["DB_HOST", "DB_PORT", "DB_USER", "DB_NAME"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"ERROR: Missing .env variables: {', '.join(missing)}")
        print("Add them to .env in the project root.")
        sys.exit(1)

    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ["DB_PORT"]),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        dbname=os.environ["DB_NAME"],
    )


def setup_table(conn, reset=False):
    with conn.cursor() as cur:
        if reset:
            print("  Dropping existing table...")
            cur.execute("DROP TABLE IF EXISTS architecture_vectors;")
        cur.execute(CREATE_TABLE_SQL)
        cur.execute(SCALAR_INDEXES_SQL)
    conn.commit()


def create_vector_index(conn, row_count):
    with conn.cursor() as cur:
        cur.execute("DROP INDEX IF EXISTS idx_av_embedding;")
        if row_count < 300:
            print(f"  Creating hnsw index ({row_count} rows < 300)...")
            cur.execute("""
                CREATE INDEX idx_av_embedding
                ON architecture_vectors
                USING hnsw (embedding vector_cosine_ops);
            """)
        else:
            print(f"  Creating ivfflat index ({row_count} rows >= 300)...")
            cur.execute("""
                CREATE INDEX idx_av_embedding
                ON architecture_vectors
                USING ivfflat (embedding vector_cosine_ops)
                WITH (lists = 100);
            """)
    conn.commit()


def check_existing(conn, building_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM architecture_vectors WHERE building_id = %s",
            (building_id,)
        )
        return cur.fetchone() is not None


def load_buildings():
    if not os.path.exists(PROCESSED_JSON):
        print(f"ERROR: {PROCESSED_JSON} not found. Run stage2_ml.py --post-enrich first.")
        sys.exit(1)
    with open(PROCESSED_JSON, "r", encoding="utf-8") as f:
        return json.load(f)


def prepare_row(b):
    """Convert a building dict to a psycopg2-ready parameter dict."""
    # Extract cover image filename (order=0)
    cover = next((img["filename"] for img in b.get("images", []) if img.get("order") == 0), None)

    # Convert embedding list to pgvector string format
    embedding = b.get("embedding")
    if not embedding:
        return None  # skip — no embedding
    embedding_str = "[" + ",".join(f"{v:.8f}" for v in embedding) + "]"

    return {
        "building_id":      b["building_id"],
        "slug":             b["slug"],
        "name_en":          b["name_en"],
        "project_name":     b["project_name"],
        "architect":        b.get("architect"),
        "location_country": b.get("location_country"),
        "city":             b.get("city"),
        "year":             b.get("year"),
        "area_sqm":         b.get("area_sqm"),
        "program":          b["program"],
        "mood":             b.get("mood") or "N/A",
        "material":         b.get("material") or "N/A",
        "description":      b.get("description"),
        "url":              b.get("url"),
        "tags":             b.get("tags") or [],
        "source_slugs":     b.get("source_slugs") or [b["slug"]],
        "image_cover":      cover,
        "embedding":        embedding_str,
    }


def main_programmatic(reset=False):
    """Callable entry point for use by run.py (no argparse)."""
    print("Loading 4_buildings_processed.json...")
    buildings = load_buildings()
    print(f"  {len(buildings)} buildings")

    print("\nConnecting to PostgreSQL...")
    conn = get_db_connection()
    print("  Connected.")

    print("\nSetting up table...")
    setup_table(conn, reset=reset)
    print("  Table ready.")

    print("\nInserting buildings...")
    inserted = 0
    updated = 0
    skipped = 0

    for b in buildings:
        row = prepare_row(b)
        if row is None:
            skipped += 1
            print(f"  SKIP (no embedding): {b.get('slug')}")
            continue

        existed = not reset and check_existing(conn, b["building_id"])

        with conn.cursor() as cur:
            cur.execute(UPSERT_SQL, row)
        conn.commit()

        if existed:
            updated += 1
        else:
            inserted += 1

    total = inserted + updated
    print(f"\nIngest complete:")
    print(f"  Inserted: {inserted}")
    print(f"  Updated:  {updated}")
    print(f"  Skipped:  {skipped} (no embedding)")
    print(f"  Total:    {total}")

    if total > 0:
        print("\nCreating vector index...")
        create_vector_index(conn, total)
        print("  Vector index created.")

    conn.close()
    print("\nDone. architecture_vectors is ready.")


def main():
    parser = argparse.ArgumentParser(description="Stage 3: load 4_buildings_processed.json into PostgreSQL")
    parser.add_argument("--reset", action="store_true",
                        help="DROP and recreate architecture_vectors table before loading")
    args = parser.parse_args()
    main_programmatic(reset=args.reset)


if __name__ == "__main__":
    main()
