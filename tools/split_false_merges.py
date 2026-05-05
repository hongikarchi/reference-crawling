"""Detect and split false-merge clusters in building registry.

False-merge pattern (high confidence):
  • Cluster has ≥ 3 source members
  • Generic name (only contains generic-token words like "house",
    "private", "residence") — no distinctive name-tokens
  • Multiple distinct cities across members → can't be the same building

Action: split the cluster — keep one canonical_bld_id per (city)
group, redirect the smaller groups to new orphan canonicals (one per
source_id when no clear group anchor).

Conservative bias: when in doubt, DON'T split. We'd rather have a
small false-merge than create false-splits via over-correction.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from typing import Optional

from canonical.registry import BuildingRegistry


BUILDINGS_REG    = "data/id_registry_buildings.json"
DIVISARE_DB      = "data/crawl/divisare.db"
ARCHITIZER_DB    = "data/crawl/architizer.db"
ARCHELLO_DB      = "data/crawl/archello.db"
METALOCUS_FINAL  = "data/enrich/4_buildings_final.json"
SPLIT_LOG        = "data/canonical/false_merge_splits.json"

# Tokens that don't add specificity. Any name composed entirely of these
# is a "generic" name. Real building names usually have a distinctive token
# (proper noun, project name, place name, etc.)
GENERIC_TOKENS = {
    # building types
    "house", "home", "residence", "residential", "apartment", "apartments",
    "condo", "condominium", "villa", "loft", "studio", "office", "tower",
    "building", "complex", "project", "centre", "center", "store", "shop",
    "park", "plaza", "garden", "school", "library", "museum", "hotel",
    "restaurant", "cafe", "bar", "spa", "kitchen", "bathroom", "lounge",
    "bedroom", "suite", "showroom", "gallery", "bistro", "lobby",
    # adjectives / qualifiers
    "private", "public", "luxury", "modern", "contemporary", "single", "family",
    "small", "large", "new", "old", "main", "primary", "central",
    "single-family",
    # connectors
    "the", "a", "an", "and", "of", "for", "in", "on", "at", "with", "by", "to",
    # common name shells
    "untitled", "unnamed", "name", "tbd",
}


def _tokenize(name: str) -> set[str]:
    """Lowercase, split on non-alpha, drop short tokens."""
    return {t for t in re.findall(r"[a-zA-ZäöüéèàâñÄÖÜÉÈÀÂÑ]+", name.lower())
            if len(t) >= 3}


def _is_generic_name(names: list[str]) -> bool:
    """All non-generic tokens removed — does anything distinctive remain?"""
    all_tokens: set[str] = set()
    for n in names:
        all_tokens |= _tokenize(n)
    distinctive = all_tokens - GENERIC_TOKENS
    return len(distinctive) == 0


def _norm_city(s: Optional[str]) -> str:
    if not s:
        return ""
    return re.sub(r"[^\w]", "", s.lower())


def _load_per_member_data() -> dict[tuple, dict]:
    """{(source, source_id): {name, city, country}} for each crawled member."""
    out: dict[tuple, dict] = {}
    conn = sqlite3.connect(DIVISARE_DB)
    for r in conn.execute(
        "SELECT id, name, location_city, location_country FROM divisare_projects"
    ):
        out[("divisare", str(r[0]))] = {"name": r[1] or "", "city": r[2], "country": r[3]}
    conn.close()
    conn = sqlite3.connect(ARCHITIZER_DB)
    for r in conn.execute(
        "SELECT id, name, location_city, location_country FROM architizer_projects"
    ):
        out[("architizer", str(r[0]))] = {"name": r[1] or "", "city": r[2], "country": r[3]}
    conn.close()
    conn = sqlite3.connect(ARCHELLO_DB)
    for r in conn.execute(
        "SELECT id, name, location_city, location_country FROM archello_projects"
    ):
        out[("archello", str(r[0]))] = {"name": r[1] or "", "city": r[2], "country": r[3]}
    conn.close()
    final = json.load(open(METALOCUS_FINAL))
    for b in final:
        bid = b.get("building_id")
        if bid:
            out[("metalocus", str(bid))] = {
                "name": b.get("name_en") or b.get("project_name") or "",
                "city": b.get("city"),
                "country": b.get("location_country"),
            }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Apply splits (default: dry-run)")
    ap.add_argument("--min-members", type=int, default=3,
                    help="Min cluster size to consider (default 3)")
    ap.add_argument("--min-cities", type=int, default=2,
                    help="Min distinct cities to flag as false-merge (default 2)")
    args = ap.parse_args()

    print("loading registry …", flush=True)
    reg = BuildingRegistry(BUILDINGS_REG)
    print(f"  active canonicals: {reg.stats()['active']}", flush=True)

    print("loading per-member source data …", flush=True)
    member_data = _load_per_member_data()
    print(f"  {len(member_data)} member records", flush=True)

    print("\nscanning for false-merge clusters …", flush=True)
    suspect: list[tuple[str, dict]] = []
    for cid, e in reg.data.items():
        if e.get("redirected_to"):
            continue
        srcs = e.get("source_refs", {})
        n_members = sum(len(ids) for ids in srcs.values())
        if n_members < args.min_members:
            continue
        # Gather distinct cities + names from members
        cities = set()
        member_views = []
        for src, ids in srcs.items():
            for sid in ids:
                d = member_data.get((src, str(sid)))
                if d:
                    member_views.append(((src, sid), d))
                    if d.get("city"):
                        cities.add(_norm_city(d["city"]))
        if len(cities) < args.min_cities:
            continue
        if not _is_generic_name(e.get("names", [])):
            continue
        suspect.append((cid, {
            "names": e["names"], "n_members": n_members,
            "cities": sorted(cities),
            "members": member_views,
        }))

    print(f"  detected suspect false-merge clusters: {len(suspect)}", flush=True)
    print(f"\nTop-15 suspects:")
    suspect.sort(key=lambda t: -t[1]["n_members"])
    for cid, info in suspect[:15]:
        names_preview = ', '.join(info["names"][:2])[:60]
        cities_preview = ', '.join(info["cities"][:5])[:50]
        print(f'  {cid} n={info["n_members"]} cities={len(info["cities"])} | {names_preview} | [{cities_preview}]')

    if not args.apply:
        print(f"\n(dry-run — pass --apply to perform splits)", flush=True)
        # Save log
        os.makedirs(os.path.dirname(SPLIT_LOG), exist_ok=True)
        log = [{
            "cid": cid, "n_members": info["n_members"],
            "cities": info["cities"], "names": info["names"],
            "members": [{"source": k[0], "id": k[1], "city": v.get("city"), "country": v.get("country")}
                        for k, v in info["members"]],
        } for cid, info in suspect]
        with open(SPLIT_LOG, "w") as f:
            json.dump({"summary": {"n_suspect": len(suspect)}, "suspects": log},
                      f, indent=2, ensure_ascii=False)
        print(f"  log → {SPLIT_LOG}", flush=True)
        return 0

    # Apply splits: for each suspect, split each member into its own
    # source-id-based orphan canonical (drop the merged group entirely).
    print(f"\nsplitting {len(suspect)} clusters …", flush=True)
    n_split = 0
    n_new_orphans = 0
    for cid, info in suspect:
        entry = reg.data[cid]
        # Drop all source_refs; mark as redirected_to None (deleted)
        # Then create new orphan per source_id
        srcs = entry.get("source_refs", {})
        # Mark deleted via a sentinel (no redirect target)
        # We'll just clear source_refs and mark name as "[split-false-merge] ..."
        new_entry_marker = f"[split-false-merge:{cid}]"
        # Clear and demote
        entry["source_refs"] = {}
        entry["names"] = [new_entry_marker]
        entry["redirected_to"] = None  # leave as inactive shell
        # Remove from name_index + source_index
        for src, ids in list(srcs.items()):
            for sid in ids:
                key = (src, str(sid))
                if reg._source_index.get(key) == cid:
                    del reg._source_index[key]
        for n in list(entry.get("names", [])):
            from canonical.registry import _normalize_name
            nk = _normalize_name(n)
            if reg._name_index.get(nk) == cid:
                del reg._name_index[nk]
        # Create new orphan per source_id with the original name
        for src, ids in srcs.items():
            for sid in ids:
                d = member_data.get((src, str(sid)))
                name = (d or {}).get("name") or "[unknown]"
                reg.match_or_create(names={name}, source_refs={src: [sid]})
                n_new_orphans += 1
        n_split += 1
    reg.save()
    # Re-export canonical
    from canonical.match_buildings_sequential import export_clusters
    n_clusters = export_clusters(reg, "data/canonical/buildings_canonical.json")
    print(f"\n✓ split {n_split} clusters → {n_new_orphans} new orphans", flush=True)
    print(f"✓ registry: {reg.stats()}", flush=True)
    print(f"✓ canonical re-exported: {n_clusters} clusters", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
