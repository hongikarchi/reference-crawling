#!/usr/bin/env python3
"""L3 audit — prep: stage one self-contained packet per sampled building.

Read-only w.r.t. Neon / crawl DBs. Writes only under data/reports/audit/.
For each sampled canonical_bld_id it gathers:
  * the canonical row from Neon (the AI-produced fields under audit),
  * the raw source row(s) from the cached crawl SQLite DBs (Protocol A truth),
  * the cover image, downloaded locally (Protocol B truth).
Output: l3_shards/pilot.jsonl + shard_NNN.jsonl, images under l3_images/.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.canonical_v2_neon_loader import _connect, TABLE  # noqa: E402

MANIFEST = ROOT / "data/reports/audit/L3_sample_manifest.json"
IMG_DIR = ROOT / "data/reports/audit/l3_images"
SHARD_DIR = ROOT / "data/reports/audit/l3_shards"
PREP_REPORT = ROOT / "data/reports/audit/L3_prep_report.json"
CRAWL = ROOT / "data/crawl"
PILOT_N = 30
SHARD_SIZE = 50
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 audit"

CANON_COLS = [
    "canonical_bld_id", "name", "names_alts", "location_country", "location_city",
    "project_year", "architect_names", "architects_text", "program", "style",
    "color_tone", "atmosphere", "material_visual", "visual_description",
    "image_derived", "n_sources", "confidence_tier", "source_refs", "source_urls",
    "display_cover_url", "cover_image_url_default",
]

SOURCE_QUERY = {
    "divisare": ("divisare.db", "divisare_projects",
                 ["name", "architect_names", "location_country", "location_city",
                  "project_year", "abstract", "description", "tag_slugs", "cover_image_url"]),
    "architizer": ("architizer.db", "architizer_projects",
                   ["name", "firm_name", "description", "description_short",
                    "completion_year", "location_full", "location_country",
                    "location_city", "categories", "constr_status", "cover_image_url"]),
    "archello": ("archello.db", "archello_projects",
                 ["name", "architect_name", "location_full", "location_country",
                  "location_city", "project_year", "category", "description", "cover_image_url"]),
    "metalocus": ("metalocus.db", "buildings",
                  ["title", "architects", "location", "city", "country", "year",
                   "building_type", "description", "materials", "cover_image_url"]),
}


def fetch_source_rows(source_refs: dict) -> dict:
    out = {}
    for src, ids in (source_refs or {}).items():
        spec = SOURCE_QUERY.get(src)
        if not spec or not ids:
            continue
        db_file, table, cols = spec
        db_path = CRAWL / db_file
        if not db_path.exists():
            continue
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rows = []
        for sid in ids:
            try:
                ph = ",".join("?" for _ in cols)
                cur = conn.execute(
                    f"SELECT {','.join(cols)} FROM {table} WHERE id=?", (int(sid),))
                rec = cur.fetchone()
            except (ValueError, sqlite3.Error):
                rec = None
            if rec:
                rows.append({"source_id": sid, **dict(zip(cols, rec))})
            else:
                rows.append({"source_id": sid, "_lookup": "not_found"})
        conn.close()
        if rows:
            out[src] = rows
    return out


def download_image(args) -> tuple[str, bool]:
    cid, url = args
    dest = IMG_DIR / f"{cid}.jpg"
    if dest.exists() and dest.stat().st_size > 0:
        return cid, True
    if not url:
        return cid, False
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        if len(data) < 100:
            return cid, False
        dest.write_bytes(data)
        return cid, True
    except Exception:
        return cid, False


def main() -> int:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    sample = manifest["sample"]
    cids = [s["canonical_bld_id"] for s in sample]
    stratum_of = {s["canonical_bld_id"]: s["stratum"] for s in sample}
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    SHARD_DIR.mkdir(parents=True, exist_ok=True)

    # Neon canonical rows
    conn = _connect()
    canon: dict[str, dict] = {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {','.join(CANON_COLS)} FROM {TABLE} "
                f"WHERE canonical_bld_id = ANY(%s)", (cids,))
            for rec in cur.fetchall():
                row = dict(zip(CANON_COLS, rec))
                canon[row["canonical_bld_id"]] = row
    finally:
        conn.rollback()
        conn.close()

    # build packets (preserve manifest order)
    packets = []
    for cid in cids:
        c = canon.get(cid)
        if not c:
            continue
        cover_url = c.get("display_cover_url") or c.get("cover_image_url_default")
        packets.append({
            "canonical_bld_id": cid,
            "stratum": stratum_of[cid],
            "n_sources": c["n_sources"],
            "confidence_tier": c["confidence_tier"],
            "cover_image_url": cover_url,
            "cover_image_path": f"data/reports/audit/l3_images/{cid}.jpg",
            "canonical": {
                "name": c["name"], "names_alts": c["names_alts"],
                "location_country": c["location_country"],
                "location_city": c["location_city"], "project_year": c["project_year"],
                "architect_names": c["architect_names"],
                "architects_text": c["architects_text"], "program": c["program"],
                "style": c["style"], "color_tone": c["color_tone"],
                "atmosphere": c["atmosphere"], "material_visual": c["material_visual"],
                "visual_description": c["visual_description"],
                "image_derived": c["image_derived"],
            },
            "sources": fetch_source_rows(c["source_refs"]),
            "source_urls": c["source_urls"],
        })

    # download cover images (threaded)
    dl_args = [(p["canonical_bld_id"], p["cover_image_url"]) for p in packets]
    fetched = {}
    with ThreadPoolExecutor(max_workers=16) as ex:
        for cid, ok in ex.map(download_image, dl_args):
            fetched[cid] = ok
    for p in packets:
        p["cover_image_fetched"] = fetched.get(p["canonical_bld_id"], False)
        if not p["cover_image_fetched"]:
            p["cover_image_path"] = None

    # write pilot + shards
    pilot = packets[:PILOT_N]
    rest = packets[PILOT_N:]
    (SHARD_DIR / "pilot.jsonl").write_text(
        "".join(json.dumps(p, ensure_ascii=False) + "\n" for p in pilot), encoding="utf-8")
    shard_files = []
    for i in range(0, len(rest), SHARD_SIZE):
        chunk = rest[i:i + SHARD_SIZE]
        fn = SHARD_DIR / f"shard_{i // SHARD_SIZE:03d}.jsonl"
        fn.write_text("".join(json.dumps(p, ensure_ascii=False) + "\n" for p in chunk),
                      encoding="utf-8")
        shard_files.append(fn.name)

    report = {
        "sampled": len(sample),
        "packets_built": len(packets),
        "missing_canonical_rows": len(cids) - len(packets),
        "images_fetched": sum(1 for v in fetched.values() if v),
        "images_failed": sum(1 for v in fetched.values() if not v),
        "pilot_file": "l3_shards/pilot.jsonl",
        "pilot_count": len(pilot),
        "shard_files": shard_files,
        "shard_count": len(shard_files),
        "shard_size": SHARD_SIZE,
    }
    PREP_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
