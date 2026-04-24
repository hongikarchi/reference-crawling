#!/usr/bin/env python3
"""Upload 4_buildings_final.json to PostgreSQL + images to Cloudflare R2.

Usage:
    python3 upload.py             # UPSERT all buildings + upload new images
    python3 upload.py --dry-run   # show what would be uploaded, no writes
    python3 upload.py --reset     # DROP + recreate table, re-upload all images

Requires .env with:
    DB_HOST, DB_PORT, DB_USER, DB_NAME          (PostgreSQL)
    R2_ACCOUNT_ID, R2_ACCESS_KEY_ID,
    R2_SECRET_ACCESS_KEY, R2_BUCKET             (Cloudflare R2)

IMPORTANT: Only run after explicit user approval.
"""

import json
import os
import sys

import boto3
import psycopg2
import psycopg2.extras
from botocore.config import Config
from dotenv import load_dotenv

import config


# ---------------------------------------------------------------------------
# PostgreSQL schema
# ---------------------------------------------------------------------------

CREATE_TABLE_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS architecture_vectors (
    building_id        TEXT PRIMARY KEY,
    slug               TEXT UNIQUE NOT NULL,
    name_en            TEXT NOT NULL,
    project_name       TEXT NOT NULL,
    architect          TEXT,
    location_country   TEXT,
    city               TEXT,
    year               INTEGER,
    area_sqm           NUMERIC,
    program            TEXT NOT NULL,
    style              TEXT,
    atmosphere         TEXT,
    color_tone         TEXT,
    material           TEXT,
    material_visual    TEXT[],
    description        TEXT,
    visual_description TEXT,
    url                TEXT,
    tags               TEXT[],
    source_slugs       TEXT[],
    image_photos       TEXT[],
    image_drawings     TEXT[],
    vocab_version      TEXT DEFAULT 'v1',
    prompt_version     TEXT,
    embedding          VECTOR(384) NOT NULL
);
"""

SCALAR_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_av_program      ON architecture_vectors (program);
CREATE INDEX IF NOT EXISTS idx_av_style        ON architecture_vectors (style);
CREATE INDEX IF NOT EXISTS idx_av_color_tone   ON architecture_vectors (color_tone);
CREATE INDEX IF NOT EXISTS idx_av_country      ON architecture_vectors (location_country);
CREATE INDEX IF NOT EXISTS idx_av_year         ON architecture_vectors (year);
"""

MIGRATE_SQL = """
ALTER TABLE architecture_vectors
    ADD COLUMN IF NOT EXISTS style              TEXT,
    ADD COLUMN IF NOT EXISTS atmosphere         TEXT,
    ADD COLUMN IF NOT EXISTS color_tone         TEXT,
    ADD COLUMN IF NOT EXISTS material_visual    TEXT[],
    ADD COLUMN IF NOT EXISTS visual_description TEXT,
    ADD COLUMN IF NOT EXISTS vocab_version      TEXT DEFAULT 'v1',
    ADD COLUMN IF NOT EXISTS prompt_version     TEXT;
"""

UPSERT_SQL = """
INSERT INTO architecture_vectors (
    building_id, slug, name_en, project_name, architect,
    location_country, city, year, area_sqm,
    program, style, atmosphere, color_tone,
    material, material_visual, description, visual_description,
    url, tags, source_slugs, image_photos, image_drawings,
    vocab_version, prompt_version, embedding
) VALUES (
    %(building_id)s, %(slug)s, %(name_en)s, %(project_name)s, %(architect)s,
    %(location_country)s, %(city)s, %(year)s, %(area_sqm)s,
    %(program)s, %(style)s, %(atmosphere)s, %(color_tone)s,
    %(material)s, %(material_visual)s, %(description)s, %(visual_description)s,
    %(url)s, %(tags)s, %(source_slugs)s, %(image_photos)s, %(image_drawings)s,
    %(vocab_version)s, %(prompt_version)s, %(embedding)s
)
ON CONFLICT (building_id) DO UPDATE SET
    slug               = EXCLUDED.slug,
    name_en            = EXCLUDED.name_en,
    project_name       = EXCLUDED.project_name,
    architect          = EXCLUDED.architect,
    location_country   = EXCLUDED.location_country,
    city               = EXCLUDED.city,
    year               = EXCLUDED.year,
    area_sqm           = EXCLUDED.area_sqm,
    program            = EXCLUDED.program,
    style              = EXCLUDED.style,
    atmosphere         = EXCLUDED.atmosphere,
    color_tone         = EXCLUDED.color_tone,
    material           = EXCLUDED.material,
    material_visual    = EXCLUDED.material_visual,
    description        = EXCLUDED.description,
    visual_description = EXCLUDED.visual_description,
    url                = EXCLUDED.url,
    tags               = EXCLUDED.tags,
    source_slugs       = EXCLUDED.source_slugs,
    image_photos       = EXCLUDED.image_photos,
    image_drawings     = EXCLUDED.image_drawings,
    vocab_version      = EXCLUDED.vocab_version,
    prompt_version     = EXCLUDED.prompt_version,
    embedding          = EXCLUDED.embedding;
"""


# ---------------------------------------------------------------------------
# PostgreSQL helpers
# ---------------------------------------------------------------------------

def _get_pg_conn():
    load_dotenv(os.path.join(config.BASE_DIR, ".env"))
    required = ["DB_HOST", "DB_PORT", "DB_USER", "DB_NAME"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"ERROR: Missing .env variables: {', '.join(missing)}")
        sys.exit(1)
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ["DB_PORT"]),
        user=os.environ["DB_USER"],
        password=os.environ.get("DB_PASSWORD", ""),
        dbname=os.environ["DB_NAME"],
        sslmode=os.environ.get("DB_SSLMODE", "prefer"),
    )


def _setup_table(conn, reset=False):
    with conn.cursor() as cur:
        if reset:
            print("  Dropping existing table...")
            cur.execute("DROP TABLE IF EXISTS architecture_vectors;")
            cur.execute(CREATE_TABLE_SQL)
        else:
            cur.execute(CREATE_TABLE_SQL)
            cur.execute(MIGRATE_SQL)
        cur.execute(SCALAR_INDEXES_SQL)
    conn.commit()


def _create_vector_index(conn, row_count):
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


def _prepare_row(b):
    """Convert a building dict to a psycopg2-ready parameter dict."""
    embedding = b.get("embedding")
    if not embedding:
        return None
    embedding_str = "[" + ",".join(f"{v:.8f}" for v in embedding) + "]"

    images = b.get("images", [])
    photos = [img["filename"] for img in images
              if img.get("type") == "photo" and img.get("upload")]
    drawings = [img["filename"] for img in images
                if img.get("type") == "drawing" and img.get("upload")]

    return {
        "building_id":        b["building_id"],
        "slug":               b["slug"],
        "name_en":            b["name_en"],
        "project_name":       b["project_name"],
        "architect":          b.get("architect"),
        "location_country":   b.get("location_country"),
        "city":               b.get("city"),
        "year":               b.get("year"),
        "area_sqm":           b.get("area_sqm"),
        "program":            b.get("program", "Other"),
        "style":              b.get("style"),
        "atmosphere":         b.get("atmosphere"),
        "color_tone":         b.get("color_tone"),
        "material":           b.get("material"),
        "material_visual":    b.get("material_visual") or [],
        "description":        b.get("description"),
        "visual_description": b.get("visual_description"),
        "url":                b.get("url"),
        "tags":               b.get("tags") or [],
        "source_slugs":       b.get("source_slugs") or [b["slug"]],
        "image_photos":       photos,
        "image_drawings":     drawings,
        "vocab_version":      b.get("vocab_version") or "v1",
        "prompt_version":     b.get("prompt_version"),
        "embedding":          embedding_str,
    }


def upload_postgres(buildings, dry_run=False, reset=False):
    print("\n--- PostgreSQL ---")
    print("Connecting...")
    conn = _get_pg_conn()
    print("  Connected.")

    print("Setting up table...")
    if not dry_run:
        _setup_table(conn, reset=reset)
    print("  Table ready.")

    inserted = updated = skipped = 0

    for b in buildings:
        row = _prepare_row(b)
        if row is None:
            skipped += 1
            print(f"  SKIP (no embedding): {b.get('slug')}")
            continue

        if dry_run:
            inserted += 1
            continue

        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM architecture_vectors WHERE building_id = %s",
                (b["building_id"],)
            )
            existed = cur.fetchone() is not None
            cur.execute(UPSERT_SQL, row)
        conn.commit()

        if existed:
            updated += 1
        else:
            inserted += 1

    total = inserted + updated
    action = "Would insert" if dry_run else "Inserted"
    print(f"\n  {action}: {inserted}")
    if not dry_run:
        print(f"  Updated:  {updated}")
    print(f"  Skipped:  {skipped} (no embedding)")
    print(f"  Total:    {total}")

    if total > 0 and not dry_run:
        print("\n  Creating vector index...")
        _create_vector_index(conn, total)
        print("  Vector index created.")

    conn.close()
    return total


# ---------------------------------------------------------------------------
# R2 helpers
# ---------------------------------------------------------------------------

CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def _get_r2_client():
    load_dotenv(os.path.join(config.BASE_DIR, ".env"))
    required = ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"ERROR: Missing .env variables: {', '.join(missing)}")
        sys.exit(1)
    return boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def _get_existing_r2_keys(client, bucket):
    keys = set()
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        for obj in page.get("Contents", []):
            keys.add(obj["Key"])
    return keys


def upload_r2(buildings, dry_run=False, reset=False):
    print("\n--- Cloudflare R2 ---")
    client = _get_r2_client()
    bucket = os.environ["R2_BUCKET"]

    # Build manifest: {building_id}/{filename} for all upload=True images
    manifest = {}
    for b in buildings:
        building_id = b["building_id"]
        for img in b.get("images", []):
            if img.get("upload"):
                key = f"{building_id}/{img['filename']}"
                manifest[key] = os.path.join(config.IMAGE_BASE_DIR, building_id, img["filename"])

    print(f"  Manifest: {len(manifest)} images selected for upload.")

    existing = set() if reset else _get_existing_r2_keys(client, bucket)
    if existing:
        print(f"  {len(existing)} files already in R2 (will skip).")

    uploaded = skipped = failed = 0

    for key, filepath in sorted(manifest.items()):
        if not os.path.exists(filepath):
            skipped += 1
            continue
        if key in existing:
            skipped += 1
            continue

        if dry_run:
            uploaded += 1
            continue

        ext = os.path.splitext(filepath)[1].lower()
        content_type = CONTENT_TYPES.get(ext, "application/octet-stream")
        try:
            client.upload_file(
                filepath, bucket, key,
                ExtraArgs={"ContentType": content_type},
            )
            uploaded += 1
            if uploaded % 50 == 0:
                print(f"  Uploaded {uploaded} files...")
        except Exception as e:
            print(f"  FAILED: {key} — {e}")
            failed += 1

    action = "Would upload" if dry_run else "Uploaded"
    print(f"\n  {action}: {uploaded}")
    print(f"  Skipped:  {skipped}")
    if not dry_run:
        print(f"  Failed:   {failed}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Upload 4_buildings_final.json to PostgreSQL + R2"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be uploaded without writing anything")
    parser.add_argument("--reset", action="store_true",
                        help="DROP PostgreSQL table and re-upload all R2 images")
    args = parser.parse_args()

    if not os.path.exists(config.FINAL_JSON):
        print(f"ERROR: {config.FINAL_JSON} not found.")
        print("Run stage3_embed.py first.")
        sys.exit(1)

    print(f"Loading {config.FINAL_JSON}...")
    with open(config.FINAL_JSON, encoding="utf-8") as f:
        buildings = json.load(f)
    print(f"  {len(buildings)} buildings")

    if args.dry_run:
        print("\n[DRY RUN] No writes will be made.\n")

    pg_total = upload_postgres(buildings, dry_run=args.dry_run, reset=args.reset)
    upload_r2(buildings, dry_run=args.dry_run, reset=args.reset)

    print("\n" + ("=" * 40))
    if args.dry_run:
        print(f"DRY RUN complete. Would upload {pg_total} buildings to PostgreSQL.")
    else:
        print(f"Upload complete. {pg_total} buildings in architecture_vectors.")


if __name__ == "__main__":
    main()
