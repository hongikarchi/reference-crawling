#!/usr/bin/env python3
"""Stage 1: Run crawler and/or export 1_buildings_raw.json from SQLite.

Usage:
    python stage1_crawl.py --crawl          # run full 4-phase crawl
    python stage1_crawl.py --export         # export SQLite → data/1_buildings_raw.json
    python stage1_crawl.py --crawl --export # crawl then export
"""

import argparse
import json
import os
import sqlite3
import sys

import config
import database as db
from crawler import run_all
from utils import logger


DATA_DIR = os.path.join(config.BASE_DIR, "data")
OUTPUT_PATH = os.path.join(DATA_DIR, "1_buildings_raw.json")


def _to_int(value):
    """Convert a raw string/int value to int, or None if not parseable."""
    if value is None:
        return None
    try:
        # year may be "2012" or "2012-01-01" — take first 4-digit run
        import re
        m = re.search(r"\d{4}", str(value))
        return int(m.group()) if m else None
    except Exception:
        return None


def _to_float(value):
    """Convert a raw area string to float, or None if not parseable.

    Handles:
    - Thousands-separator commas: "27,394 sqm" → 27394.0
    - Multi-value strings: "Site area.- 1,784 sqm.Built up area.- 299 sqm." → 1784.0
      (takes only the first number)
    - European decimal notation: "516,51" → 516.51
    """
    if value is None:
        return None
    try:
        import re
        # Extract the first number pattern (digits with optional comma/dot separators)
        m = re.search(r"\d+(?:[.,]\d+)*", str(value))
        if not m:
            return None
        num_str = m.group()
        # Comma followed by exactly 3 digits = thousands separator (e.g. "27,394")
        if re.fullmatch(r"\d{1,3}(,\d{3})+", num_str):
            return float(num_str.replace(",", ""))
        # Otherwise treat comma as decimal separator (European notation)
        return float(num_str.replace(",", "."))
    except Exception:
        return None


def _rename_images(slug, images_from_db):
    """
    Rename downloaded image files from {filename} to {order}_{filename}.
    Returns a list of image dicts with updated filenames, only for files
    that exist on disk. Cover image (order=0) must exist.
    """
    slug_dir = os.path.join(config.IMAGE_BASE_DIR, slug)
    result = []

    for img in images_from_db:
        order = img["image_order"]
        original_filename = img["filename"]
        original_path = os.path.join(slug_dir, original_filename)

        new_filename = f"{order}_{original_filename}"
        new_path = os.path.join(slug_dir, new_filename)

        if os.path.exists(new_path):
            # Already renamed in a previous export run
            result.append({
                "filename": new_filename,
                "alt_text": img["alt_text"] or None,
                "order": order,
            })
        elif os.path.exists(original_path):
            os.rename(original_path, new_path)
            result.append({
                "filename": new_filename,
                "alt_text": img["alt_text"] or None,
                "order": order,
            })
        # If file doesn't exist on disk, skip this image

    return result


def export_buildings():
    """Read SQLite and write data/1_buildings_raw.json."""
    os.makedirs(DATA_DIR, exist_ok=True)

    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row

    # Fetch all completed articles with their building data
    articles = conn.execute("""
        SELECT a.id, a.url, a.slug,
               b.title, b.architects, b.city, b.country, b.year,
               b.area_sqm, b.building_type, b.description, b.materials
        FROM articles a
        JOIN buildings b ON b.article_id = a.id
        WHERE a.status = 'completed'
        ORDER BY a.id
    """).fetchall()

    buildings_out = []
    skipped_no_title = 0
    skipped_no_cover = 0

    for row in articles:
        article_id = row["id"]
        slug = row["slug"]
        title = row["title"]

        # Skip if no title
        if not title or not title.strip():
            skipped_no_title += 1
            continue

        # Fetch downloaded images for this article, sorted by order
        images_db = conn.execute("""
            SELECT image_order, filename, alt_text
            FROM images
            WHERE article_id = ? AND status = 'completed'
            ORDER BY image_order
        """, (article_id,)).fetchall()
        images_db = [dict(i) for i in images_db]

        # Rename to {order}_{filename} and filter to existing files
        images_out = _rename_images(slug, images_db)

        # Skip if no cover image (order=0)
        has_cover = any(img["order"] == 0 for img in images_out)
        if not has_cover:
            skipped_no_cover += 1
            continue

        # Fetch tags
        tags_rows = conn.execute("""
            SELECT t.name FROM tags t
            JOIN article_tags at ON at.tag_id = t.id
            WHERE at.article_id = ?
        """, (article_id,)).fetchall()
        tags = [r["name"] for r in tags_rows]

        record = {
            "slug": slug,
            "project_name": title.strip(),
            "architect": row["architects"] or None,
            "location_country": row["country"] or None,
            "city": row["city"] or None,
            "year": _to_int(row["year"]),
            "area_sqm": _to_float(row["area_sqm"]),
            "building_type": row["building_type"] or None,
            "mood": None,
            "material": row["materials"] or None,
            "description": row["description"] or None,
            "url": row["url"],
            "images": images_out,
            "tags": tags,
        }
        buildings_out.append(record)

    conn.close()

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(buildings_out, f, ensure_ascii=False, indent=2)

    total = len(buildings_out)
    print(f"\nExport complete:")
    print(f"  Exported:            {total}")
    print(f"  Skipped (no title):  {skipped_no_title}")
    print(f"  Skipped (no cover):  {skipped_no_cover}")
    print(f"  Output:              {OUTPUT_PATH}\n")

    return total


def main():
    parser = argparse.ArgumentParser(
        description="Stage 1: crawl metalocus.es and/or export 1_buildings_raw.json"
    )
    parser.add_argument("--crawl", action="store_true", help="Run full 4-phase crawl")
    parser.add_argument("--export", action="store_true", help="Export SQLite → 1_buildings_raw.json")
    args = parser.parse_args()

    if not args.crawl and not args.export:
        parser.print_help()
        sys.exit(1)

    if args.crawl:
        logger.info("Stage 1: starting crawl...")
        db.init_db()
        run_all()
        logger.info("Stage 1: crawl complete.")

    if args.export:
        logger.info("Stage 1: exporting to 1_buildings_raw.json...")
        count = export_buildings()
        if count == 0:
            logger.warning("No buildings exported. Run --crawl first or check crawler progress with: python run.py stats")


if __name__ == "__main__":
    main()
