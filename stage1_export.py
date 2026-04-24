#!/usr/bin/env python3
"""Stage 1: Export SQLite → data/1_buildings_raw.json.

Usage:
    python3 stage1_export.py

Reads completed articles from metalocus.db, renames image files,
classifies images as photo/drawing, selects 3+3 for upload,
and writes 1_buildings_raw.json.
"""

import json
import os
import re
import sqlite3
import sys

import config


def _to_int(value):
    if value is None:
        return None
    try:
        m = re.search(r"\d{4}", str(value))
        return int(m.group()) if m else None
    except Exception:
        return None


def _to_float(value):
    """Parse area strings, handling thousands separators and European decimals."""
    if value is None:
        return None
    try:
        m = re.search(r"\d+(?:[.,]\d+)*", str(value))
        if not m:
            return None
        num_str = m.group()
        if re.fullmatch(r"\d{1,3}(,\d{3})+", num_str):
            return float(num_str.replace(",", ""))
        return float(num_str.replace(",", "."))
    except Exception:
        return None


_DRAWING_KEYWORDS = [
    "plan", "section", "elevation", "axonometr", "diagram",
    "detail", "sketch", "drawing", "constructive",
]


def _classify_image(alt_text):
    alt_lower = (alt_text or "").lower()
    if any(kw in alt_lower for kw in _DRAWING_KEYWORDS):
        return "drawing"
    return "photo"


def _select_upload_images(images, max_photos=3, max_drawings=3):
    """Mark best 3 photos + 3 drawings with upload=True."""
    photos   = sorted([i for i in images if i["type"] == "photo"],   key=lambda x: x["order"])
    drawings = sorted([i for i in images if i["type"] == "drawing"], key=lambda x: x["order"])
    selected = {i["filename"] for i in photos[:max_photos] + drawings[:max_drawings]}
    for img in images:
        img["upload"] = img["filename"] in selected
    return images


_ID_REGISTRY = None


def _get_id_registry():
    global _ID_REGISTRY
    if _ID_REGISTRY is None:
        if os.path.exists(config.REGISTRY_JSON):
            with open(config.REGISTRY_JSON, encoding="utf-8") as f:
                _ID_REGISTRY = json.load(f)
        else:
            _ID_REGISTRY = {}
    return _ID_REGISTRY


def _rename_images(slug, images_from_db):
    """Rename image files from {filename} to {order}_{filename} on disk.

    Falls back to images/{building_id}/ if images/{slug}/ doesn't exist
    (happens after stage2_dedup reorganizes images by building_id).
    Returns list of image dicts for files that exist on disk.
    """
    img_dir = os.path.join(config.IMAGE_BASE_DIR, slug)
    if not os.path.exists(img_dir):
        building_id = _get_id_registry().get(slug)
        if building_id:
            img_dir = os.path.join(config.IMAGE_BASE_DIR, building_id)

    result = []
    for img in images_from_db:
        order    = img["image_order"]
        orig     = img["filename"]
        renamed  = f"{order}_{orig}"
        alt      = img["alt_text"] or None

        if os.path.exists(os.path.join(img_dir, renamed)):
            result.append({"filename": renamed, "alt_text": alt,
                           "order": order, "type": _classify_image(alt)})
        elif os.path.exists(os.path.join(img_dir, orig)):
            os.rename(os.path.join(img_dir, orig), os.path.join(img_dir, renamed))
            result.append({"filename": renamed, "alt_text": alt,
                           "order": order, "type": _classify_image(alt)})
    return result


def export_buildings():
    """Read SQLite, write data/1_buildings_raw.json. Returns count exported."""
    os.makedirs(config.DATA_DIR, exist_ok=True)

    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row

    articles = conn.execute("""
        SELECT a.id, a.url, a.slug,
               b.title, b.architects, b.city, b.country, b.year,
               b.area_sqm, b.building_type, b.description, b.materials
        FROM articles a
        JOIN buildings b ON b.article_id = a.id
        WHERE a.status = 'completed'
        ORDER BY a.id
    """).fetchall()

    out = []
    skipped_no_title = 0
    skipped_no_cover = 0

    for row in articles:
        slug  = row["slug"]
        title = row["title"]

        if not title or not title.strip():
            skipped_no_title += 1
            continue

        images_db = conn.execute("""
            SELECT image_order, filename, alt_text
            FROM images
            WHERE article_id = ? AND status = 'completed'
            ORDER BY image_order
        """, (row["id"],)).fetchall()

        images = _rename_images(slug, [dict(i) for i in images_db])
        images = _select_upload_images(images)

        if not any(img["order"] == 0 for img in images):
            skipped_no_cover += 1
            continue

        tags = [r["name"] for r in conn.execute("""
            SELECT t.name FROM tags t
            JOIN article_tags at ON at.tag_id = t.id
            WHERE at.article_id = ?
        """, (row["id"],)).fetchall()]

        out.append({
            "slug":             slug,
            "project_name":     title.strip(),
            "architect":        row["architects"] or None,
            "location_country": row["country"] or None,
            "city":             row["city"] or None,
            "year":             _to_int(row["year"]),
            "area_sqm":         _to_float(row["area_sqm"]),
            "building_type":    row["building_type"] or None,
            "material":         row["materials"] or None,
            "description":      row["description"] or None,
            "url":              row["url"],
            "images":           images,
            "tags":             tags,
        })

    conn.close()

    with open(config.RAW_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"\nExport complete:")
    print(f"  Exported:            {len(out)}")
    print(f"  Skipped (no title):  {skipped_no_title}")
    print(f"  Skipped (no cover):  {skipped_no_cover}")
    print(f"  Output:              {config.RAW_JSON}\n")
    return len(out)


if __name__ == "__main__":
    count = export_buildings()
    if count == 0:
        print("No buildings exported. Run the crawler first.")
        sys.exit(1)
