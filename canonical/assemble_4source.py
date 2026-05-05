"""Stage F — Assemble 4-source canonical buildings JSON.

Reads:
  data/id_registry_buildings.json           (Stage B output)
  data/id_registry_architects.json          (Stage A output)
  data/canonical/metalocus_architect_clusters.json
  per-source DBs (divisare, architizer, archello) + 4_buildings_final.json (metalocus)

Produces:
  data/canonical/canonical_buildings_4source.json
    [
      {
        "canonical_bld_id":         "bld_NNNNNN",
        "canonical_arch_ids":       [...],
        "primary_name":             str,        # picked from longest non-generic name
        "all_names":                [str, ...],
        "source_refs":              {source: [source_id, ...]},
        "country":                  str | None,
        "city":                     str | None,
        "year":                     int | None,
        "typology":                 str | None,
        "covers_by_source": {                    # one cover URL per source
          "divisare":   url | None,
          "architizer": url | None,
          "archello":   url | None,
          "metalocus":  url | None
        },
        "gallery_urls_by_source": {              # all gallery URLs per source
          "divisare":   [url, ...],
          ...
        },
        "n_sources":                int,
        "n_members":                int,
      },
      ...
    ]

Field-merge policy (no description / enrichment merge — that's Stage D):
  primary_name: longest non-generic name (longer = more specific)
  country/city/year/typology: first non-NULL across sources, in priority
                              order divisare → architizer → archello → metalocus
  covers_by_source: each source's own cover URL preserved separately
  gallery_urls_by_source: same — preserved per source for downstream cover
                              selection (make_web responsibility)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from typing import Iterator, Optional

from canonical.registry import ArchitectRegistry, BuildingRegistry


BUILDINGS_REG    = "data/id_registry_buildings.json"
ARCHITECTS_REG   = "data/id_registry_architects.json"
METALOCUS_FINAL  = "data/enrich/4_buildings_final.json"
METALOCUS_CLUSTERS = "data/canonical/metalocus_architect_clusters.json"
DIVISARE_DB      = "data/crawl/divisare.db"
ARCHITIZER_DB    = "data/crawl/architizer.db"
ARCHELLO_DB      = "data/crawl/archello.db"
OUTPUT           = "data/canonical/canonical_buildings_4source.json"

GENERIC_NAME_TOKENS = {
    "house", "tower", "office", "building", "project", "studio",
    "untitled", "private", "residence",
}

# Source priority for field-pick (highest-quality metadata first)
SOURCE_PRIORITY = ["divisare", "architizer", "archello", "metalocus"]


def _load_source_data() -> dict[tuple, dict]:
    """Returns {(source, source_id): {name, country, city, year, typology,
    cover_url, gallery_urls}} across all 4 sources."""
    out: dict[tuple, dict] = {}

    # Divisare
    conn = sqlite3.connect(DIVISARE_DB)
    conn.row_factory = sqlite3.Row
    for r in conn.execute(
        "SELECT id, name, location_country, location_city, project_year, "
        "       tag_slugs, cover_image_url, gallery_urls "
        "FROM divisare_projects WHERE name IS NOT NULL"
    ):
        try:
            tags = json.loads(r["tag_slugs"]) if r["tag_slugs"] else []
        except (json.JSONDecodeError, TypeError):
            tags = []
        try:
            gallery = json.loads(r["gallery_urls"]) if r["gallery_urls"] else []
        except (json.JSONDecodeError, TypeError):
            gallery = []
        out[("divisare", str(r["id"]))] = {
            "name":         r["name"],
            "country":      r["location_country"],
            "city":         r["location_city"],
            "year":         r["project_year"],
            "typology":     tags[0] if tags else None,
            "cover_url":    r["cover_image_url"],
            "gallery_urls": gallery,
        }
    conn.close()

    # Architizer
    conn = sqlite3.connect(ARCHITIZER_DB)
    conn.row_factory = sqlite3.Row
    for r in conn.execute(
        "SELECT id, name, completion_year, location_country, location_city, "
        "       categories, cover_image_url, gallery_image_urls "
        "FROM architizer_projects WHERE name IS NOT NULL"
    ):
        try:
            cats = json.loads(r["categories"]) if r["categories"] else []
        except (json.JSONDecodeError, TypeError):
            cats = []
        try:
            gallery = json.loads(r["gallery_image_urls"]) if r["gallery_image_urls"] else []
        except (json.JSONDecodeError, TypeError):
            gallery = []
        out[("architizer", str(r["id"]))] = {
            "name":         r["name"],
            "country":      r["location_country"],
            "city":         r["location_city"],
            "year":         r["completion_year"],
            "typology":     cats[0] if cats else None,
            "cover_url":    r["cover_image_url"],
            "gallery_urls": gallery,
        }
    conn.close()

    # Archello
    conn = sqlite3.connect(ARCHELLO_DB)
    conn.row_factory = sqlite3.Row
    for r in conn.execute(
        "SELECT id, name, project_year, location_country, location_city, "
        "       category, cover_image_url, gallery_image_urls "
        "FROM archello_projects WHERE name IS NOT NULL"
    ):
        try:
            gallery = json.loads(r["gallery_image_urls"]) if r["gallery_image_urls"] else []
        except (json.JSONDecodeError, TypeError):
            gallery = []
        out[("archello", str(r["id"]))] = {
            "name":         r["name"],
            "country":      r["location_country"],
            "city":         r["location_city"],
            "year":         r["project_year"],
            "typology":     r["category"],
            "cover_url":    r["cover_image_url"],
            "gallery_urls": gallery,
        }
    conn.close()

    # Metalocus (from 4_buildings_final.json)
    final = json.load(open(METALOCUS_FINAL))
    for b in final:
        bid = b.get("building_id")
        if not bid:
            continue
        # Cover URL: prefer cover_image_url field, else first uploaded image local path
        cover = b.get("cover_image_url")
        if not cover:
            for img in (b.get("images") or []):
                if img.get("order") == 0 and img.get("upload"):
                    cover = f"images/{bid}/{img['filename']}"
                    break
        gallery = []
        for img in (b.get("images") or []):
            if img.get("upload"):
                gallery.append(f"images/{bid}/{img['filename']}")
        out[("metalocus", str(bid))] = {
            "name":         b.get("name_en") or b.get("project_name") or "",
            "country":      b.get("location_country"),
            "city":         b.get("city"),
            "year":         b.get("year"),
            "typology":     b.get("program") or b.get("building_type"),
            "cover_url":    cover,
            "gallery_urls": gallery,
        }

    return out


def _pick_primary_name(names: list[str]) -> str:
    """Longest non-generic name wins. Falls back to longest if all generic."""
    if not names:
        return ""
    def score(n: str) -> tuple[int, int]:
        # Prefer non-generic (no generic-only tokens), then longest
        toks = {t for t in re.split(r"\W+", n.lower()) if t}
        is_generic = bool(toks) and toks.issubset(GENERIC_NAME_TOKENS)
        return (0 if is_generic else 1, len(n))
    return max(names, key=score)


def _pick_first(values: list, none_ok=True):
    """First non-empty value across source-priority order."""
    for v in values:
        if v not in (None, "", [], {}):
            return v
    return None if none_ok else ""


def _parse_year(v) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, int):
        return v if 1800 <= v <= 2100 else None
    m = re.search(r"\b(\d{4})\b", str(v))
    if m:
        y = int(m.group(1))
        return y if 1800 <= y <= 2100 else None
    return None


def _build_arch_src_idx(arch_reg: ArchitectRegistry) -> dict[tuple, str]:
    """Pre-build (source, native_arch_id) → canonical_arch_id index. ONCE."""
    idx: dict[tuple, str] = {}
    for arch_cid, ae in arch_reg.data.items():
        if ae.get("redirected_to"):
            continue
        for src, ids in ae.get("source_refs", {}).items():
            for sid in ids:
                idx[(src, str(sid))] = arch_cid
    return idx


def _arch_lookup_for_building(
    cid: str, registry: BuildingRegistry, arch_reg: ArchitectRegistry,
    bld_to_native_arch: dict, arch_src_idx: dict,
) -> list[str]:
    """Get all canonical_arch_ids for a building canonical via its source members.
    arch_src_idx must be pre-built once via _build_arch_src_idx (don't rebuild
    per call — that's O(N_arch × N_buildings) = blow-up)."""
    entry = registry.data.get(cid, {})
    arch_canonicals: set[str] = set()
    for src, ids in entry.get("source_refs", {}).items():
        for sid in ids:
            for native_arch_id in bld_to_native_arch.get((src, str(sid)), []):
                cid_arch = arch_src_idx.get((src, native_arch_id))
                if cid_arch:
                    arch_canonicals.add(arch_reg.follow(cid_arch))
    return sorted(arch_canonicals)


def _load_building_to_native_arch() -> dict[tuple, list[str]]:
    """{(source, building_source_id): [native_arch_id, ...]}"""
    out: dict[tuple, list[str]] = {}
    # Divisare
    conn = sqlite3.connect(DIVISARE_DB)
    for bid, raw in conn.execute(
        "SELECT id, architect_ids FROM divisare_projects WHERE architect_ids IS NOT NULL"
    ):
        try:
            ids = json.loads(raw) if raw else []
            out[("divisare", str(bid))] = [str(a) for a in ids]
        except (json.JSONDecodeError, TypeError):
            pass
    conn.close()
    # Architizer
    conn = sqlite3.connect(ARCHITIZER_DB)
    for bid, slug in conn.execute(
        "SELECT id, firm_slug FROM architizer_projects WHERE firm_slug IS NOT NULL"
    ):
        out[("architizer", str(bid))] = [slug]
    conn.close()
    # Archello
    conn = sqlite3.connect(ARCHELLO_DB)
    for bid, aid in conn.execute(
        "SELECT id, architect_brand_id FROM archello_projects WHERE architect_brand_id IS NOT NULL"
    ):
        out[("archello", str(bid))] = [str(aid)]
    conn.close()
    # Metalocus (via cluster file)
    bld_to_arch = json.load(open(METALOCUS_CLUSTERS)).get("building_to_canonical", {})
    for bid, arch_ids in bld_to_arch.items():
        out[("metalocus", str(bid))] = list(arch_ids)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=OUTPUT)
    args = ap.parse_args()

    print("loading registries …", flush=True)
    bld_reg  = BuildingRegistry(BUILDINGS_REG)
    arch_reg = ArchitectRegistry(ARCHITECTS_REG)
    print(f"  buildings: {bld_reg.stats()['active']}", flush=True)
    print(f"  architects: {arch_reg.stats()['active']}", flush=True)

    print("\nloading per-source building data …", flush=True)
    src_data = _load_source_data()
    print(f"  {len(src_data)} source-side buildings indexed", flush=True)

    print("loading building → native_arch_id …", flush=True)
    bld_to_native_arch = _load_building_to_native_arch()
    print(f"  {len(bld_to_native_arch)} mappings", flush=True)

    print("building arch_src_idx (once) …", flush=True)
    arch_src_idx = _build_arch_src_idx(arch_reg)
    print(f"  {len(arch_src_idx)} (source, sid) keys", flush=True)

    print("\nassembling 4-source canonical records …", flush=True)
    out: list[dict] = []
    n = 0
    for cid, entry in bld_reg.data.items():
        if entry.get("redirected_to"):
            continue
        n += 1
        srcs = entry.get("source_refs", {})
        # Gather member data in source-priority order
        member_data: dict[str, dict] = {}  # source → first member's data
        all_names: set[str] = set(entry.get("names", []))
        for src in SOURCE_PRIORITY:
            for sid in srcs.get(src, []):
                d = src_data.get((src, str(sid)))
                if d:
                    if src not in member_data:
                        member_data[src] = d
                    if d.get("name"):
                        all_names.add(d["name"])

        primary_name = _pick_primary_name(sorted(all_names))
        country  = _pick_first([member_data.get(s, {}).get("country")  for s in SOURCE_PRIORITY])
        city     = _pick_first([member_data.get(s, {}).get("city")     for s in SOURCE_PRIORITY])
        year     = _pick_first([_parse_year(member_data.get(s, {}).get("year")) for s in SOURCE_PRIORITY])
        typology = _pick_first([member_data.get(s, {}).get("typology") for s in SOURCE_PRIORITY])

        covers_by_source: dict[str, Optional[str]] = {}
        gallery_by_source: dict[str, list] = {}
        for src in SOURCE_PRIORITY:
            d = member_data.get(src, {})
            covers_by_source[src]  = d.get("cover_url")
            gallery_by_source[src] = d.get("gallery_urls") or []

        canonical_arch_ids = _arch_lookup_for_building(
            cid, bld_reg, arch_reg, bld_to_native_arch, arch_src_idx
        )

        out.append({
            "canonical_bld_id":      cid,
            "canonical_arch_ids":    canonical_arch_ids,
            "primary_name":          primary_name,
            "all_names":             sorted(all_names),
            "source_refs":           srcs,
            "country":               country,
            "city":                  city,
            "year":                  year,
            "typology":              typology,
            "covers_by_source":      covers_by_source,
            "gallery_urls_by_source": gallery_by_source,
            "n_sources":             len(srcs),
            "n_members":             sum(len(ids) for ids in srcs.values()),
        })

        if n % 20000 == 0:
            print(f"  progress: {n}", flush=True)

    out.sort(key=lambda r: -r["n_members"])

    summary = {
        "n_canonicals":   len(out),
        "by_n_sources":   {k: sum(1 for r in out if r["n_sources"] == k)
                            for k in (1, 2, 3, 4)},
        "with_arch_id":   sum(1 for r in out if r["canonical_arch_ids"]),
        "with_year":      sum(1 for r in out if r["year"]),
        "with_country":   sum(1 for r in out if r["country"]),
        "with_city":      sum(1 for r in out if r["city"]),
        "with_typology":  sum(1 for r in out if r["typology"]),
        "with_div_cover": sum(1 for r in out if r["covers_by_source"].get("divisare")),
        "with_arz_cover": sum(1 for r in out if r["covers_by_source"].get("architizer")),
        "with_arc_cover": sum(1 for r in out if r["covers_by_source"].get("archello")),
        "with_met_cover": sum(1 for r in out if r["covers_by_source"].get("metalocus")),
    }
    print(f"\nsummary: {summary}", flush=True)

    payload = {"summary": summary, "buildings": out}
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(f"\n✓ {len(out)} canonical buildings → {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
