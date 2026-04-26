#!/usr/bin/env python3
"""Upload data/canonical_buildings_strict.json to PostgreSQL (architecture_vectors).

This is the (a1) in-place migration:
  1. ALTER architecture_vectors to add any missing columns (idempotent).
  2. DELETE rows whose building_id is NOT in the strict canonical
     — the 928 pure orphans + 49 article-style entries that build_canonical
     --strict dropped from the upload-target set.
  3. UPSERT all 2,488 strict canonical rows with cleaned data.

The existing `upload.py` is left alone for the legacy metalocus-only flow.
This script is purpose-built for the destructive DELETE step and the
canonical→table field-name mapping (canonical uses `name`, `location_city`,
`project_year`, `architect_names[]` whereas the table column names are
`name_en`, `city`, `year`, `architect`).

Per .claude/CLAUDE.md: never run upload scripts without explicit user
approval. The `--confirm` flag is required for any non-dry-run.

Usage:
    python3 upload_strict.py --dry-run                  # show plan, no writes
    python3 upload_strict.py --confirm                  # actual upload
    python3 upload_strict.py --confirm --skip-delete    # UPSERT only (a2-style)
"""

import json
import os
import sys
from typing import Optional

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

import config

CANONICAL_PATH = "data/canonical_buildings_strict.json"
METALOC_PATH   = "data/4_buildings_final.json"


# Idempotent ALTER for the columns that exist in the strict canonical but
# may not yet exist in Neon's architecture_vectors. The other Divisare
# columns (divisare_id, architect_canonical_ids, divisare_tags, etc.)
# were already added by upload.py's MIGRATE_SQL in earlier work.
EXTRA_MIGRATE_SQL = """
ALTER TABLE architecture_vectors
    ADD COLUMN IF NOT EXISTS divisare_slug            TEXT,
    ADD COLUMN IF NOT EXISTS abstract                 TEXT,
    ADD COLUMN IF NOT EXISTS divisare_credits         JSONB,
    ADD COLUMN IF NOT EXISTS cover_image_url_divisare TEXT,
    ADD COLUMN IF NOT EXISTS divisare_gallery_urls    TEXT[];
"""


UPSERT_SQL = """
INSERT INTO architecture_vectors (
    building_id, slug, name_en, project_name, architect,
    location_country, city, year, area_sqm,
    program, style, atmosphere, color_tone,
    material, material_visual, description, visual_description,
    url, tags, source_slugs, image_photos, image_drawings,
    vocab_version, prompt_version,
    divisare_id, divisare_slug, abstract,
    architect_canonical_ids, divisare_tags, divisare_credits,
    cover_image_url_divisare, divisare_gallery_urls,
    provenance, embedding
) VALUES (
    %(building_id)s, %(slug)s, %(name_en)s, %(project_name)s, %(architect)s,
    %(location_country)s, %(city)s, %(year)s, %(area_sqm)s,
    %(program)s, %(style)s, %(atmosphere)s, %(color_tone)s,
    %(material)s, %(material_visual)s, %(description)s, %(visual_description)s,
    %(url)s, %(tags)s, %(source_slugs)s, %(image_photos)s, %(image_drawings)s,
    %(vocab_version)s, %(prompt_version)s,
    %(divisare_id)s, %(divisare_slug)s, %(abstract)s,
    %(architect_canonical_ids)s, %(divisare_tags)s, %(divisare_credits)s,
    %(cover_image_url_divisare)s, %(divisare_gallery_urls)s,
    %(provenance)s, %(embedding)s
)
ON CONFLICT (building_id) DO UPDATE SET
    slug                     = EXCLUDED.slug,
    name_en                  = EXCLUDED.name_en,
    project_name             = EXCLUDED.project_name,
    architect                = EXCLUDED.architect,
    location_country         = EXCLUDED.location_country,
    city                     = EXCLUDED.city,
    year                     = EXCLUDED.year,
    area_sqm                 = EXCLUDED.area_sqm,
    program                  = EXCLUDED.program,
    style                    = EXCLUDED.style,
    atmosphere               = EXCLUDED.atmosphere,
    color_tone               = EXCLUDED.color_tone,
    material                 = EXCLUDED.material,
    material_visual          = EXCLUDED.material_visual,
    description              = EXCLUDED.description,
    visual_description       = EXCLUDED.visual_description,
    url                      = EXCLUDED.url,
    tags                     = EXCLUDED.tags,
    source_slugs             = EXCLUDED.source_slugs,
    image_photos             = EXCLUDED.image_photos,
    image_drawings           = EXCLUDED.image_drawings,
    vocab_version            = EXCLUDED.vocab_version,
    prompt_version           = EXCLUDED.prompt_version,
    divisare_id              = EXCLUDED.divisare_id,
    divisare_slug            = EXCLUDED.divisare_slug,
    abstract                 = EXCLUDED.abstract,
    architect_canonical_ids  = EXCLUDED.architect_canonical_ids,
    divisare_tags            = EXCLUDED.divisare_tags,
    divisare_credits         = EXCLUDED.divisare_credits,
    cover_image_url_divisare = EXCLUDED.cover_image_url_divisare,
    divisare_gallery_urls    = EXCLUDED.divisare_gallery_urls,
    provenance               = EXCLUDED.provenance,
    embedding                = EXCLUDED.embedding;
"""


def _get_pg_conn():
    load_dotenv(os.path.join(config.BASE_DIR, ".env"))
    required = ["DB_HOST", "DB_PORT", "DB_USER", "DB_NAME"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"ERROR: Missing .env variables: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ["DB_PORT"]),
        user=os.environ["DB_USER"],
        password=os.environ.get("DB_PASSWORD", ""),
        dbname=os.environ["DB_NAME"],
        sslmode=os.environ.get("DB_SSLMODE", "prefer"),
    )


def _load_canonical(path: str) -> list[dict]:
    with open(path) as f:
        return json.load(f)


def _load_metaloc_index(path: str) -> dict:
    """{building_id: full_metalocus_record} — for image type/upload metadata
    that doesn't make it into the canonical (canonical's image_paths is just
    a list of relative paths; the table needs photo/drawing arrays of
    filenames-only for the existing R2 layout)."""
    with open(path) as f:
        rows = json.load(f)
    return {r["building_id"]: r for r in rows}


def _prepare_row(c: dict, metaloc_idx: dict) -> Optional[dict]:
    """Map canonical building dict → row params for architecture_vectors."""
    bid = c.get("metalocus_building_id")
    if not bid:
        return None  # canonical entry without metalocus link cannot be uploaded
    embedding = c.get("embedding")
    if not embedding:
        return None
    embedding_str = "[" + ",".join(f"{v:.8f}" for v in embedding) + "]"

    metaloc = metaloc_idx.get(bid, {})
    images = metaloc.get("images", [])
    photos = [img["filename"] for img in images
              if img.get("type") == "photo" and img.get("upload")]
    drawings = [img["filename"] for img in images
                if img.get("type") == "drawing" and img.get("upload")]

    architect_names = c.get("architect_names") or []
    architect_str = ", ".join(architect_names) if architect_names else None

    provenance = c.get("provenance")
    if provenance is not None and not isinstance(provenance, str):
        provenance = json.dumps(provenance, ensure_ascii=False)

    divisare_credits = c.get("divisare_credits")
    if divisare_credits is not None and not isinstance(divisare_credits, str):
        divisare_credits = json.dumps(divisare_credits, ensure_ascii=False)

    return {
        "building_id":        bid,
        "slug":               metaloc.get("slug") or bid,
        "name_en":            c.get("name") or metaloc.get("name_en") or "",
        "project_name":       metaloc.get("project_name") or c.get("name") or "",
        "architect":          architect_str,
        "location_country":   c.get("location_country"),
        "city":               c.get("location_city"),
        "year":               c.get("project_year"),
        "area_sqm":           c.get("area_sqm"),
        "program":            c.get("program") or "Other",
        "style":              c.get("style"),
        "atmosphere":         c.get("atmosphere"),
        "color_tone":         c.get("color_tone"),
        "material":           metaloc.get("material"),
        "material_visual":    c.get("material_visual") or [],
        "description":        c.get("description"),
        "visual_description": c.get("visual_description"),
        "url":                metaloc.get("url"),
        "tags":               metaloc.get("tags") or [],
        "source_slugs":       metaloc.get("source_slugs") or [bid],
        "image_photos":       photos,
        "image_drawings":     drawings,
        "vocab_version":      c.get("vocab_version") or "v2",
        "prompt_version":     c.get("prompt_version"),
        "divisare_id":              c.get("divisare_id"),
        "divisare_slug":            c.get("divisare_slug"),
        "abstract":                 c.get("abstract"),
        "architect_canonical_ids":  c.get("architect_canonical_ids") or None,
        "divisare_tags":            c.get("divisare_tags") or None,
        "divisare_credits":         divisare_credits,
        "cover_image_url_divisare": c.get("cover_image_url_divisare"),
        "divisare_gallery_urls":    c.get("divisare_gallery_urls") or None,
        "provenance":               provenance,
        "embedding":          embedding_str,
    }


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dry-run", action="store_true",
                   help="print the plan and a summary; no writes")
    p.add_argument("--confirm", action="store_true",
                   help="REQUIRED for an actual run (the DELETE step is irreversible)")
    p.add_argument("--skip-delete", action="store_true",
                   help="skip the DELETE step — only ALTER + UPSERT (turns this into "
                        "an (a2)-style additive migration that leaves dropped rows in place)")
    p.add_argument("--canonical", default=CANONICAL_PATH)
    p.add_argument("--metalocus", default=METALOC_PATH)
    args = p.parse_args(argv)

    if not args.dry_run and not args.confirm:
        print("ERROR: real run requires --confirm (this DELETEs production rows).",
              file=sys.stderr)
        print("Try `python3 upload_strict.py --dry-run` first.", file=sys.stderr)
        return 2

    if not os.path.exists(args.canonical):
        print(f"ERROR: {args.canonical} not found. Run "
              f"`python3 build_canonical.py --strict` first.", file=sys.stderr)
        return 1

    print(f"Loading canonical from {args.canonical}...")
    canonical = _load_canonical(args.canonical)
    print(f"  {len(canonical)} strict canonical records")

    print(f"Loading metalocus index from {args.metalocus} (for image metadata)...")
    metaloc_idx = _load_metaloc_index(args.metalocus)
    print(f"  {len(metaloc_idx)} metalocus records indexed")

    keep_ids = {c["metalocus_building_id"] for c in canonical
                if c.get("metalocus_building_id")}
    print(f"  building_ids to keep: {len(keep_ids)}")

    print("\nConnecting to PostgreSQL (Neon)...")
    conn = _get_pg_conn()
    print("  Connected.")

    # ---- Step 1: ALTER ---------------------------------------------------
    print("\nStep 1 — ALTER architecture_vectors (idempotent ADD COLUMN IF NOT EXISTS)")
    if args.dry_run:
        print("  [DRY-RUN] would run EXTRA_MIGRATE_SQL")
    else:
        with conn.cursor() as cur:
            cur.execute(EXTRA_MIGRATE_SQL)
        conn.commit()
        print("  ALTER complete.")

    # ---- Step 2: DELETE --------------------------------------------------
    print("\nStep 2 — DELETE rows not in strict canonical")
    with conn.cursor() as cur:
        cur.execute("SELECT building_id FROM architecture_vectors;")
        existing_ids = {r[0] for r in cur.fetchall()}
    to_delete = existing_ids - keep_ids
    print(f"  Currently in DB:   {len(existing_ids)}")
    print(f"  Strict canonical:  {len(keep_ids)}")
    print(f"  To DELETE:         {len(to_delete)}")

    if args.skip_delete:
        print("  --skip-delete set: leaving these rows in place")
    elif args.dry_run:
        print(f"  [DRY-RUN] would DELETE {len(to_delete)} rows. First 10:")
        for bid in sorted(to_delete)[:10]:
            print(f"    {bid}")
    elif to_delete:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                "DELETE FROM architecture_vectors WHERE building_id IN (VALUES %s)",
                [(bid,) for bid in to_delete],
            )
        conn.commit()
        print(f"  Deleted {len(to_delete)} rows.")
    else:
        print("  Nothing to delete.")

    # ---- Step 3: UPSERT --------------------------------------------------
    print(f"\nStep 3 — UPSERT {len(canonical)} strict canonical rows")
    inserted = updated = skipped_no_emb = skipped_no_bid = 0
    for i, c in enumerate(canonical, 1):
        row = _prepare_row(c, metaloc_idx)
        if row is None:
            if not c.get("metalocus_building_id"):
                skipped_no_bid += 1
            else:
                skipped_no_emb += 1
            continue
        if args.dry_run:
            inserted += 1
            continue
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM architecture_vectors WHERE building_id = %s",
                        (c["metalocus_building_id"],))
            existed = cur.fetchone() is not None
            cur.execute(UPSERT_SQL, row)
        conn.commit()
        if existed:
            updated += 1
        else:
            inserted += 1
        if i % 250 == 0:
            print(f"  ...processed {i}/{len(canonical)}")

    action = "Would" if args.dry_run else "Result:"
    print(f"\n{action} insert={inserted}, update={updated}, "
          f"skip_no_embedding={skipped_no_emb}, skip_no_metalocus_id={skipped_no_bid}")

    # ---- Step 4: Final summary ------------------------------------------
    if not args.dry_run:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM architecture_vectors;")
            final_count = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM architecture_vectors WHERE divisare_id IS NOT NULL;"
            )
            with_divisare = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM architecture_vectors "
                "WHERE architect_canonical_ids IS NOT NULL "
                "AND array_length(architect_canonical_ids, 1) > 0;"
            )
            with_arch_canon = cur.fetchone()[0]
        print(f"\n=== Final state ===")
        print(f"  rows in architecture_vectors:                {final_count}")
        print(f"  rows with divisare_id (full match):          {with_divisare}")
        print(f"  rows with architect_canonical_ids populated: {with_arch_canon}")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
