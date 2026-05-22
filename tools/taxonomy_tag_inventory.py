#!/usr/bin/env python3
"""Taxonomy tag inventory — read-only scan of the 4 crawl DBs.

Counts every source-native taxonomy tag (divisare `tag_slugs`, architizer
`categories`, archello `category`, metalocus `buildings.building_type`),
proposes a first-pass bucket (typology / architectural_element / drop) for each
distinct tag, and writes a report that drives the typology vocab + crosswalk
design (plan: Track B step 1).

Read-only: opens the crawl DBs, writes one JSON report. No DB writes, no LLM.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CRAWL = ROOT / "data" / "crawl"
REPORT = ROOT / "data" / "reports" / "taxonomy_tag_inventory.json"
KNOWN_TAGS = ROOT / "canonical" / "_known_building_tags.json"

# (source, db file, table, column, format)
SOURCES = [
    ("divisare",   "divisare.db",   "divisare_projects",   "tag_slugs",     "json_array"),
    ("architizer", "architizer.db", "architizer_projects", "categories",    "json_array"),
    ("archello",   "archello.db",   "archello_projects",   "category",      "scalar"),
    ("metalocus",  "metalocus.db",  "buildings",           "building_type", "scalar"),
]

# Bucket heuristic keyword sets — substring match against the slug with
# hyphens/underscores normalised to spaces. FIRST-PASS ONLY; the operator and
# user finalise the real buckets when building the crosswalk.
_TYPOLOGY_KW = {
    "house", "home", "apartment", "flat", "villa", "residence", "residential",
    "housing", "dwelling", "office", "headquarter", "museum", "gallery",
    "library", "librar", "mediatheque", "archive", "school", "kindergarten",
    "university", "campus", "college", "classroom", "church", "chapel",
    "cathedral", "mosque", "synagogue", "temple", "shrine", "hotel", "hostel",
    "resort", "restaurant", "cafe", "canteen", "theater", "theatre",
    "cinema", "auditorium", "concert", "stadium", "arena", "sports", "hospital",
    "clinic", "health", "pavilion", "station", "airport", "terminal", "bridge",
    "factory", "warehouse", "industrial", "store", "shop", "retail", "mall",
    "market", "showroom", "bank", "prison", "cemetery", "crematorium",
    "winery", "brewery", "farm", "barn", "stable", "kiosk", "tower",
    "skyscraper", "embassy", "courthouse", "cityhall", "townhall", "police",
    "nursery", "daycare", "park", "plaza", "square", "playground",
    "landscape", "waterfront", "promenade",
}
_ELEMENT_KW = {
    "stair", "staircase", "roof", "facade", "wall", "window", "door",
    "courtyard", "atrium", "terrace", "balcony", "patio", "loggia", "porch",
    "ceiling", "column", "beam", "vault", "dome", "ramp", "elevator",
    "escalator", "corridor", "hallway", "lobby", "entrance", "skylight",
    "canopy", "awning", "shutter", "louver", "railing", "parapet", "chimney",
    "fireplace", "mezzanine", "basement", "attic", "garden", "rooftop",
    "fence", "gate",
}


def _norm(slug: str) -> str:
    return re.sub(r"[-_]+", " ", str(slug).strip().lower())


def _load_known() -> set[str]:
    if not KNOWN_TAGS.exists():
        return set()
    try:
        data = json.loads(KNOWN_TAGS.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    return {_norm(t) for t in data if t} if isinstance(data, list) else set()


def _tok_match(norm_slug: str, kwset: set[str]) -> bool:
    # prefix-of-token match — keeps plural/variant stems (libraries~librar)
    # without the mid-word false positives of raw substring (spanish~spa).
    return any(tok.startswith(kw)
               for tok in norm_slug.split() for kw in kwset)


def _bucket_guess(slug: str, known: set[str]) -> str:
    n = _norm(slug)
    if n in known:
        return "typology"
    if _tok_match(n, _TYPOLOGY_KW):
        return "typology"
    if _tok_match(n, _ELEMENT_KW):
        return "architectural_element"
    return "drop"


def _count_json_array(db_path: Path, table: str, col: str):
    counts: Counter = Counter()
    scanned = with_tags = 0
    conn = sqlite3.connect(str(db_path))
    try:
        for (raw,) in conn.execute(f"SELECT {col} FROM {table}"):
            scanned += 1
            try:
                tags = json.loads(raw) if raw else []
            except (json.JSONDecodeError, TypeError):
                tags = []
            if not isinstance(tags, list):
                tags = []
            tags = [str(t).strip() for t in tags if t and str(t).strip()]
            if tags:
                with_tags += 1
            counts.update(tags)
    finally:
        conn.close()
    return counts, scanned, with_tags


def _count_scalar(db_path: Path, table: str, col: str):
    counts: Counter = Counter()
    scanned = with_tags = 0
    conn = sqlite3.connect(str(db_path))
    try:
        for (raw,) in conn.execute(f"SELECT {col} FROM {table}"):
            scanned += 1
            value = str(raw).strip() if raw is not None and str(raw).strip() else None
            if value:
                with_tags += 1
                counts[value] += 1
    finally:
        conn.close()
    return counts, scanned, with_tags


def main() -> int:
    known = _load_known()
    report = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "known_building_tags_seed": len(known),
        "sources": {},
    }

    for name, db_file, table, col, fmt in SOURCES:
        db_path = CRAWL / db_file
        if not db_path.exists():
            print(f"FATAL: crawl DB missing: {db_path}", file=sys.stderr)
            return 2
        counter, scanned, with_tags = (
            _count_json_array if fmt == "json_array" else _count_scalar
        )(db_path, table, col)

        buckets: Counter = Counter()
        tags = []
        for slug, count in counter.most_common():
            guess = _bucket_guess(slug, known)
            buckets[guess] += 1
            tags.append({"slug": slug, "count": count, "bucket_guess": guess})

        report["sources"][name] = {
            "table": table,
            "column": col,
            "format": fmt,
            "rows_scanned": scanned,
            "rows_with_tags": with_tags,
            "unique_tags": len(counter),
            "total_assignments": sum(counter.values()),
            "bucket_guess_counts": dict(buckets),
            "tags": tags,
        }

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"taxonomy tag inventory -> {REPORT.relative_to(ROOT)}")
    for name, _, _, _, _ in SOURCES:
        s = report["sources"][name]
        print(f"\n=== {name} ({s['table']}.{s['column']}) ===")
        print(f"  rows {s['rows_scanned']:,} | with tags {s['rows_with_tags']:,} | "
              f"unique {s['unique_tags']:,} | assignments {s['total_assignments']:,}")
        print(f"  bucket guess: {s['bucket_guess_counts']}")
        print(f"  top tags:")
        for t in s["tags"][:12]:
            print(f"    {t['count']:>6,}  {t['bucket_guess']:<22}  {t['slug']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
