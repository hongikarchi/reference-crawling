"""Per-source building loaders for Stage B canonical match.

Each loader yields uniform dicts:

    {
      "id":              str,        # source-native building ID
      "name":            str,
      "name_core":       str,        # _normalize_name(name)
      "canonical_arch_ids": list[str],  # via Stage A registry; multi-arch expanded
      "country":         str | None,
      "city":            str | None,
      "year":            int | None,
      "typology":        str | None, # source's typology / category / program
      "cover_image_url": str | None, # only used in mid-band tiebreak
      "source":          str,
    }

The canonical_arch_ids list is computed by joining each source's native
architect IDs through Stage A's registry. Multi-architect buildings get
ALL their canonical_arch_ids in this list (the Stage B matcher expands
the pre-filter group accordingly).
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Iterator, Optional

from canonical.match_architects import _normalize_name, _normalize_country
from canonical.registry import ArchitectRegistry


METALOCUS_BUILDINGS_FINAL = "data/enrich/4_buildings_final.json"
METALOCUS_CLUSTERS        = "data/canonical/metalocus_architect_clusters.json"
DIVISARE_DB               = "data/crawl/divisare.db"
ARCHITIZER_DB             = "data/crawl/architizer.db"
ARCHELLO_DB               = "data/crawl/archello.db"


_YEAR_RE = re.compile(r"\b(\d{4})\b")


def _parse_year(v) -> Optional[int]:
    """Robust year parse: accepts int, '2018', '2018-12', 'Built 2018', etc."""
    if v is None:
        return None
    if isinstance(v, int):
        return v if 1800 <= v <= 2100 else None
    m = _YEAR_RE.search(str(v))
    if m:
        y = int(m.group(1))
        return y if 1800 <= y <= 2100 else None
    return None


def _arch_lookup(registry: ArchitectRegistry) -> dict[tuple, str]:
    """Build {(source, str(source_id)): canonical_arch_id} index from registry."""
    idx: dict[tuple, str] = {}
    for cid, e in registry.data.items():
        if e.get("redirected_to"):
            continue
        for src, ids in e.get("source_refs", {}).items():
            for sid in ids:
                idx[(src, str(sid))] = cid
    return idx


def load_divisare(arch_idx: dict) -> Iterator[dict]:
    conn = sqlite3.connect(DIVISARE_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, name, architect_ids, location_country, location_city, "
        "       project_year, tag_slugs, cover_image_url "
        "FROM divisare_projects WHERE name IS NOT NULL AND name != ''"
    ).fetchall()
    for r in rows:
        try:
            arch_ids = json.loads(r["architect_ids"]) if r["architect_ids"] else []
        except (json.JSONDecodeError, TypeError):
            arch_ids = []
        canonical_arch_ids = sorted({
            arch_idx[("divisare", str(a))]
            for a in arch_ids
            if ("divisare", str(a)) in arch_idx
        })
        try:
            tags = json.loads(r["tag_slugs"]) if r["tag_slugs"] else []
        except (json.JSONDecodeError, TypeError):
            tags = []
        typology = tags[0] if tags else None
        name = r["name"]
        yield {
            "id":                 str(r["id"]),
            "name":               name,
            "name_core":          _normalize_name(name) or name.lower(),
            "canonical_arch_ids": canonical_arch_ids,
            "country":            _normalize_country(r["location_country"]) if r["location_country"] else None,
            "city":               r["location_city"],
            "year":               _parse_year(r["project_year"]),
            "typology":           typology,
            "cover_image_url":    r["cover_image_url"],
            "source":             "divisare",
        }
    conn.close()


def load_architizer(arch_idx: dict) -> Iterator[dict]:
    conn = sqlite3.connect(ARCHITIZER_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, slug, name, firm_slug, completion_year, location_country, "
        "       location_city, categories, cover_image_url "
        "FROM architizer_projects WHERE name IS NOT NULL AND name != ''"
    ).fetchall()
    for r in rows:
        canonical_arch_ids: list[str] = []
        if r["firm_slug"]:
            cid = arch_idx.get(("architizer", r["firm_slug"]))
            if cid:
                canonical_arch_ids.append(cid)
        try:
            cats = json.loads(r["categories"]) if r["categories"] else []
        except (json.JSONDecodeError, TypeError):
            cats = []
        typology = cats[0] if cats else None
        name = r["name"]
        yield {
            "id":                 str(r["id"]),
            "name":               name,
            "name_core":          _normalize_name(name) or name.lower(),
            "canonical_arch_ids": canonical_arch_ids,
            "country":            _normalize_country(r["location_country"]) if r["location_country"] else None,
            "city":               r["location_city"],
            "year":               _parse_year(r["completion_year"]),
            "typology":           typology,
            "cover_image_url":    r["cover_image_url"],
            "source":             "architizer",
        }
    conn.close()


def load_archello(arch_idx: dict) -> Iterator[dict]:
    conn = sqlite3.connect(ARCHELLO_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, slug, name, architect_brand_id, location_country, "
        "       location_city, project_year, category, cover_image_url "
        "FROM archello_projects WHERE name IS NOT NULL AND name != ''"
    ).fetchall()
    for r in rows:
        canonical_arch_ids: list[str] = []
        if r["architect_brand_id"] is not None:
            cid = arch_idx.get(("archello", str(r["architect_brand_id"])))
            if cid:
                canonical_arch_ids.append(cid)
        name = r["name"]
        yield {
            "id":                 str(r["id"]),
            "name":               name,
            "name_core":          _normalize_name(name) or name.lower(),
            "canonical_arch_ids": canonical_arch_ids,
            "country":            _normalize_country(r["location_country"]) if r["location_country"] else None,
            "city":               r["location_city"],
            "year":               _parse_year(r["project_year"]),
            "typology":           r["category"],
            "cover_image_url":    r["cover_image_url"],
            "source":             "archello",
        }
    conn.close()


def load_metalocus(arch_idx: dict) -> Iterator[dict]:
    """Loads from 4_buildings_final.json (3,465 production rows) and joins
    canonical_arch_ids via the metalocus cluster file's building_to_canonical
    mapping. Newer crawled-but-not-yet-enriched metalocus buildings (Phase 11
    pending) are intentionally skipped — they can be added in a future Stage
    B re-run after the enrichment pipeline catches up."""
    final = json.load(open(METALOCUS_BUILDINGS_FINAL))
    clusters = json.load(open(METALOCUS_CLUSTERS))
    # building_to_canonical maps building_id → list of metaloc_arch_xxxx IDs
    bld_to_arch = clusters.get("building_to_canonical", {})

    for b in final:
        bid = b.get("building_id")
        if not bid:
            continue
        meta_arch_ids = bld_to_arch.get(bid, [])
        canonical_arch_ids = sorted({
            arch_idx[("metalocus", a)]
            for a in meta_arch_ids
            if ("metalocus", a) in arch_idx
        })
        name = b.get("name_en") or b.get("project_name") or ""
        # cover URL: row may have cover_image_url (Phase 11) or first images entry filename
        cover = b.get("cover_image_url")
        if not cover:
            imgs = b.get("images") or []
            for img in imgs:
                if img.get("order") == 0 and img.get("upload"):
                    cover = f"images/{bid}/{img['filename']}"
                    break
        yield {
            "id":                 str(bid),
            "name":               name,
            "name_core":          _normalize_name(name) or name.lower(),
            "canonical_arch_ids": canonical_arch_ids,
            "country":            _normalize_country(b.get("location_country")) if b.get("location_country") else None,
            "city":               b.get("city"),
            "year":               _parse_year(b.get("year")),
            "typology":           b.get("program") or b.get("building_type"),
            "cover_image_url":    cover,
            "source":             "metalocus",
        }


if __name__ == "__main__":
    # Smoke test
    reg = ArchitectRegistry()
    aidx = _arch_lookup(reg)
    print(f"arch index size: {len(aidx)}")
    by_source: dict[str, dict] = {}
    for src, loader in (
        ("divisare",   load_divisare),
        ("architizer", load_architizer),
        ("archello",   load_archello),
        ("metalocus",  load_metalocus),
    ):
        items = list(loader(aidx))
        with_arch = sum(1 for i in items if i["canonical_arch_ids"])
        with_year = sum(1 for i in items if i["year"])
        with_country = sum(1 for i in items if i["country"])
        with_cover = sum(1 for i in items if i["cover_image_url"])
        by_source[src] = {
            "total": len(items), "with_arch": with_arch,
            "with_year": with_year, "with_country": with_country,
            "with_cover": with_cover,
        }
        print(f"  {src:<12} {len(items):>6} items  arch:{with_arch:>5} "
              f"year:{with_year:>5} country:{with_country:>5} cover:{with_cover:>5}")
    print(f"\nTOTAL buildings loaded: {sum(s['total'] for s in by_source.values())}")
