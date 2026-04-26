#!/usr/bin/env python3
"""Stage B-3: assemble canonical_buildings.json from all matching outputs.

Inputs:
  data/4_buildings_final.json                       — metalocus content
  data/metalocus_architect_clusters.json             — Stage A output
  data/match/metalocus_architect_to_divisare.json    — Stage B-1 output
  data/match/metalocus_to_divisare_buildings.json    — Stage B-2 output
  data/divisare.db                                   — Phase 1 crawl

Output:
  data/canonical_buildings.json — list of CanonicalBuilding-shaped dicts

Field-merge policy (per `~/.claude/plans/db-fuzzy-lerdorf.md`):
  • Structure (name, location, year, area_sqm, divisare_credits, gallery URLs)
    → Divisare authoritative when matched; metalocus fallback when Divisare NULL.
  • Content (description, visual_description, image_paths, embedding,
    program/style/atmosphere/color_tone) → metalocus authoritative (Divisare
    lite has no enrichment).
  • Each set field is recorded in `provenance` — see CanonicalBuilding doc.

Orphan handling: metalocus buildings without a Divisare PROJECT match still
become canonical rows (divisare_id=None). If their architect cluster matched
a Divisare architect (Stage B-1), architect_canonical_ids is populated even
without a project link — preserves the consolidated identity.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from typing import Optional

import vocab
from canonical import (
    CanonicalBuilding, SOURCE_DIVISARE, SOURCE_METALOCUS, SOURCE_DERIVED,
)

METALOC_PATH      = "data/4_buildings_final.json"
CLUSTERS_PATH     = "data/metalocus_architect_clusters.json"
ARCH_MATCH_PATH   = "data/match/metalocus_architect_to_divisare.json"
BLDG_MATCH_PATH   = "data/match/metalocus_to_divisare_buildings.json"
DIVISARE_DB       = "data/divisare.db"
OUTPUT_PATH       = "data/canonical_buildings.json"


def _load_json(path: str):
    with open(path) as f:
        return json.load(f)


def _load_arch_map() -> dict[str, dict]:
    """{cluster_id: {divisare_id, divisare_name, divisare_country}}"""
    data = _load_json(ARCH_MATCH_PATH)
    out = {}
    for m in data["matches"]:
        if m["verdict"] in ("auto_accept", "accept_with_country") and m["divisare_id"]:
            out[m["metaloc_id"]] = {
                "divisare_id": m["divisare_id"],
                "divisare_name": m["divisare_name"],
                "divisare_country": m["divisare_country"],
            }
    return out


def _load_building_match_map() -> tuple[dict[str, dict], dict[int, str]]:
    """({bid: match_dict for confident matches},
       {divisare_id: bid that primarily owns this divisare_id})

    When several metalocus buildings claim the same Divisare project (real
    metalocus duplicates that stage2_dedup missed), the first encountered
    bid keeps the divisare_id; later bids fall back to orphan + a note in
    the canonical record's provenance.
    """
    data = _load_json(BLDG_MATCH_PATH)
    out_match = {}
    primary_owner: dict[int, str] = {}
    duplicates: dict[str, int] = {}  # bid → div_id it would have claimed
    for m in data["matches"]:
        if m["verdict"] not in ("accept_high", "accept_medium"):
            continue
        bid = m["metalocus_building_id"]
        did = m.get("divisare_id")
        if did is None:
            continue
        if did in primary_owner:
            duplicates[bid] = did
            continue
        primary_owner[did] = bid
        out_match[bid] = m
    if duplicates:
        print(f"  ⚠ {len(duplicates)} metalocus duplicates detected "
              f"(multiple bids → same divisare_id); later bids will be orphaned")
    return out_match, duplicates


def _load_divisare_projects(ids_needed: set[int]) -> dict[int, dict]:
    """Bulk load Divisare project rows for given IDs."""
    if not ids_needed:
        return {}
    out = {}
    with sqlite3.connect(DIVISARE_DB) as conn:
        conn.row_factory = sqlite3.Row
        # SQLite has a parameter-count limit; chunk to be safe
        ids_list = list(ids_needed)
        chunk = 500
        for start in range(0, len(ids_list), chunk):
            sub = ids_list[start:start + chunk]
            placeholders = ",".join("?" * len(sub))
            for r in conn.execute(
                f"SELECT id, slug, name, architect_ids, architect_names, "
                f"location_country, location_city, project_year, area_sqm, "
                f"abstract, description, tag_slugs, cover_image_url, "
                f"gallery_urls, credits "
                f"FROM divisare_projects WHERE id IN ({placeholders})",
                sub,
            ):
                d = dict(r)
                # Decode JSON fields
                for jk in ("architect_ids", "architect_names", "tag_slugs",
                           "gallery_urls", "credits"):
                    raw = d.get(jk)
                    if raw:
                        try:
                            d[jk] = json.loads(raw)
                        except (json.JSONDecodeError, TypeError):
                            d[jk] = None
                out[d["id"]] = d
    return out


def _building_image_paths(b: dict) -> list[str]:
    """Return relative image paths. metalocus stores entries as
    {"filename": "...jpg", "order": N, ...} with the convention that the
    file lives at `images/{building_id}/{filename}` (post stage2_dedup
    reorganization).
    """
    out: list[str] = []
    bid = b.get("building_id")
    for img in b.get("images") or []:
        if isinstance(img, str):
            out.append(img)
            continue
        if not isinstance(img, dict):
            continue
        # Some saves include an explicit path/local_path; honour it
        p = img.get("path") or img.get("local_path")
        if p:
            out.append(p)
            continue
        fn = img.get("filename")
        if fn and bid:
            out.append(f"images/{bid}/{fn}")
    return out


def _build_one(b: dict, *, arch_map: dict, building_match: Optional[dict],
               div_projects: dict[int, dict],
               building_to_clusters: dict[str, list[str]]) -> CanonicalBuilding:
    cb = CanonicalBuilding()
    bid = b["building_id"]

    # ---- Identity --------------------------------------------------------
    cb.set_field("metalocus_building_id", bid, SOURCE_METALOCUS)

    div_proj = None
    if building_match and building_match.get("divisare_id"):
        div_proj = div_projects.get(building_match["divisare_id"])
        if div_proj:
            cb.set_field("divisare_id", div_proj["id"], SOURCE_DIVISARE)
            cb.set_field("divisare_slug", div_proj["slug"], SOURCE_DIVISARE)

    # ---- Architect canonical IDs (from Stage A + B-1) -------------------
    cluster_ids = building_to_clusters.get(bid) or []
    matched_arch_ids: list[int] = []
    matched_arch_names: list[str] = []
    for cid in cluster_ids:
        am = arch_map.get(cid)
        if am:
            matched_arch_ids.append(am["divisare_id"])
            if am.get("divisare_name"):
                matched_arch_names.append(am["divisare_name"])
    if matched_arch_ids:
        cb.set_field("architect_canonical_ids", matched_arch_ids, SOURCE_DERIVED)
    if matched_arch_names:
        cb.set_field("architect_names", matched_arch_names, SOURCE_DIVISARE)
    elif b.get("architect"):
        # Fallback to raw metalocus architect string when no Divisare match
        cb.set_field("architect_names", [b["architect"]], SOURCE_METALOCUS)

    # ---- Name / location / year (Divisare authoritative when matched) ---
    div_name = div_proj.get("name") if div_proj else None
    metaloc_name = b.get("name_en") or b.get("project_name")
    if div_name:
        cb.set_field("name", div_name, SOURCE_DIVISARE)
    elif metaloc_name:
        cb.set_field("name", metaloc_name, SOURCE_METALOCUS)

    div_country = div_proj.get("location_country") if div_proj else None
    if div_country:
        cb.set_field("location_country", div_country, SOURCE_DIVISARE)
    elif b.get("location_country"):
        cb.set_field("location_country", b["location_country"], SOURCE_METALOCUS)

    div_city = div_proj.get("location_city") if div_proj else None
    if div_city:
        cb.set_field("location_city", div_city, SOURCE_DIVISARE)
    elif b.get("city"):
        cb.set_field("location_city", b["city"], SOURCE_METALOCUS)

    div_year = div_proj.get("project_year") if div_proj else None
    if div_year:
        cb.set_field("project_year", div_year, SOURCE_DIVISARE)
    elif b.get("year"):
        try:
            cb.set_field("project_year", int(b["year"]), SOURCE_METALOCUS)
        except (TypeError, ValueError):
            pass

    # ---- Divisare-only structural fields (only when matched) ------------
    if div_proj:
        if div_proj.get("area_sqm") is not None:
            cb.set_field("area_sqm", float(div_proj["area_sqm"]), SOURCE_DIVISARE)
        if div_proj.get("credits"):
            cb.set_field("divisare_credits", div_proj["credits"], SOURCE_DIVISARE)
        if div_proj.get("tag_slugs"):
            cb.set_field("divisare_tags", div_proj["tag_slugs"], SOURCE_DIVISARE)
        if div_proj.get("cover_image_url"):
            cb.set_field("cover_image_url_divisare",
                         div_proj["cover_image_url"], SOURCE_DIVISARE)
        if div_proj.get("gallery_urls"):
            cb.set_field("divisare_gallery_urls",
                         div_proj["gallery_urls"], SOURCE_DIVISARE)
        if div_proj.get("abstract"):
            cb.set_field("abstract", div_proj["abstract"], SOURCE_DIVISARE)

    # ---- metalocus content (description, vocab fields, images, embed) ---
    # Divisare lite typically has no description; metalocus's enriched
    # description is the user-facing text.
    if b.get("description"):
        cb.set_field("description", b["description"], SOURCE_METALOCUS)
    elif div_proj and div_proj.get("description"):
        # Divisare deep-fetch description (rare in lite mode but possible)
        cb.set_field("description", div_proj["description"], SOURCE_DIVISARE)

    if b.get("visual_description"):
        cb.set_field("visual_description", b["visual_description"], SOURCE_METALOCUS)

    for fname in ("program", "style", "color_tone", "atmosphere"):
        v = b.get(fname)
        if v and vocab.is_valid(fname, v):
            cb.set_field(fname, v, SOURCE_METALOCUS)

    if b.get("material_visual"):
        cb.set_field("material_visual", b["material_visual"], SOURCE_METALOCUS)

    img_paths = _building_image_paths(b)
    if img_paths:
        cb.set_field("image_paths", img_paths, SOURCE_METALOCUS)

    if b.get("embedding"):
        # Embedding doesn't carry provenance (not user-meaningful), but we
        # still need to store it.
        cb.embedding = b["embedding"]

    cb.vocab_version = vocab.VOCAB_VERSION

    return cb


def build_all() -> dict:
    print(f"loading metalocus buildings from {METALOC_PATH}...")
    metaloc = _load_json(METALOC_PATH)
    print(f"  {len(metaloc)} buildings")

    print(f"loading architect map from {ARCH_MATCH_PATH}...")
    arch_map = _load_arch_map()
    print(f"  {len(arch_map)} confident architect matches")

    print(f"loading building match map from {BLDG_MATCH_PATH}...")
    bldg_match, duplicate_bids = _load_building_match_map()
    print(f"  {len(bldg_match)} confident project matches "
          f"(after deduping {len(duplicate_bids)} metalocus dupes)")

    print(f"loading building→cluster index from {CLUSTERS_PATH}...")
    building_to_clusters = _load_json(CLUSTERS_PATH).get("building_to_canonical") or {}

    needed_div_ids = {m["divisare_id"] for m in bldg_match.values() if m.get("divisare_id")}
    print(f"loading {len(needed_div_ids)} Divisare project rows from {DIVISARE_DB}...")
    div_projects = _load_divisare_projects(needed_div_ids)

    print(f"\nbuilding canonical records...")
    canonical_records: list[dict] = []
    matched_count = 0
    arch_only_count = 0
    pure_orphan_count = 0
    for i, b in enumerate(metaloc, 1):
        bm = bldg_match.get(b["building_id"])
        cb = _build_one(
            b,
            arch_map=arch_map,
            building_match=bm,
            div_projects=div_projects,
            building_to_clusters=building_to_clusters,
        )
        canonical_records.append(cb.to_dict())
        if cb.divisare_id is not None:
            matched_count += 1
        elif cb.architect_canonical_ids:
            arch_only_count += 1
        else:
            pure_orphan_count += 1
        if i % 500 == 0:
            print(f"  built {i}/{len(metaloc)}  "
                  f"(matched={matched_count}, arch_only={arch_only_count}, "
                  f"orphan={pure_orphan_count})")

    print(f"\n=== summary ===")
    print(f"  total canonical records:          {len(canonical_records)}")
    print(f"  matched to Divisare project:      {matched_count} "
          f"({matched_count/len(canonical_records):.1%})")
    print(f"  orphan WITH Divisare architect:   {arch_only_count} "
          f"({arch_only_count/len(canonical_records):.1%})")
    print(f"  pure orphans (no Divisare ID):    {pure_orphan_count} "
          f"({pure_orphan_count/len(canonical_records):.1%})")

    print(f"\nwriting → {OUTPUT_PATH} ...")
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(canonical_records, f, ensure_ascii=False)
    size_mb = os.path.getsize(OUTPUT_PATH) / (1024 * 1024)
    print(f"  wrote {len(canonical_records)} records ({size_mb:.1f} MB)")
    return {"records": len(canonical_records), "matched": matched_count,
            "arch_only": arch_only_count, "pure_orphan": pure_orphan_count,
            "size_mb": round(size_mb, 1)}


def main(argv: list[str]) -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.parse_args(argv)
    build_all()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
