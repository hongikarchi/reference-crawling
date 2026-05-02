"""Enrich tiebreak pairs with per-architect project samples for Haiku.

For each pair, attach up to 5 projects per side as
"name | city, country, year" strings. Project overlap (same project
appearing on both sides; or congruent geography) is a stronger signal
than name similarity alone.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict


PROJECTS_PER_SIDE = 5


def load_project_index() -> dict[tuple, list[dict]]:
    """{(source, source_id): [{'name','city','country','year'}, ...]} for all 4 sources."""
    idx: dict[tuple, list[dict]] = defaultdict(list)

    # Divisare — JSON array of architect_ids per project
    conn = sqlite3.connect("data/crawl/divisare.db")
    for row in conn.execute(
        "SELECT name, location_city, location_country, project_year, architect_ids "
        "FROM divisare_projects WHERE architect_ids IS NOT NULL"
    ):
        proj = {"name": row[0], "city": row[1], "country": row[2], "year": row[3]}
        try:
            for aid in json.loads(row[4]) or []:
                idx[("divisare", str(aid))].append(proj)
        except (json.JSONDecodeError, TypeError):
            continue
    conn.close()

    # Architizer — firm_slug per project
    conn = sqlite3.connect("data/crawl/architizer.db")
    for row in conn.execute(
        "SELECT name, location_city, location_country, completion_year, firm_slug "
        "FROM architizer_projects WHERE firm_slug IS NOT NULL"
    ):
        idx[("architizer", row[4])].append(
            {"name": row[0], "city": row[1], "country": row[2], "year": row[3]}
        )
    conn.close()

    # Archello — architect_brand_id per project
    conn = sqlite3.connect("data/crawl/archello.db")
    for row in conn.execute(
        "SELECT name, location_city, location_country, project_year, architect_brand_id "
        "FROM archello_projects WHERE architect_brand_id IS NOT NULL"
    ):
        idx[("archello", str(row[4]))].append(
            {"name": row[0], "city": row[1], "country": row[2], "year": row[3]}
        )
    conn.close()

    # Metalocus — clusters file → building_ids → 4_buildings_final.json rows
    final = {b["building_id"]: b
             for b in json.load(open("data/enrich/4_buildings_final.json"))}
    clusters = json.load(open("data/canonical/metalocus_architect_clusters.json"))["clusters"]
    for c in clusters:
        if c.get("is_noise"):
            continue
        for bid in c["building_ids"]:
            row = final.get(bid)
            if not row:
                continue
            idx[("metalocus", c["canonical_id"])].append({
                "name":    row.get("name_en") or row.get("project_name"),
                "city":    row.get("city"),
                "country": row.get("location_country"),
                "year":    row.get("year"),
            })

    return dict(idx)


def projects_for_registry_entry(
    canonical_id: str, registry_data: dict, project_idx: dict
) -> list[dict]:
    """B side = registry canonical_id. Expand source_refs to fetch projects
    from each contributing source."""
    entry = registry_data.get(canonical_id)
    if not entry:
        return []
    out: list[dict] = []
    for src, ids in entry.get("source_refs", {}).items():
        for sid in ids:
            out.extend(project_idx.get((src, str(sid)), []))
    return out


def fmt_project(p: dict) -> str:
    bits = [str(x) for x in (p.get("city"), p.get("country"), p.get("year")) if x]
    loc = ", ".join(bits)
    name = (p.get("name") or "").strip()
    return f"{name} | {loc}" if loc else name


def enrich_batch(
    batch_in_path: str, batch_out_path: str,
    project_idx: dict, registry_data: dict,
    tiebreak_full: list[dict],
) -> int:
    pilot = json.load(open(batch_in_path))
    # tiebreak_full is the original 7,750-row file; pilot rows reference 'orig' index into it
    enriched: list[dict] = []
    for p in pilot:
        orig = tiebreak_full[p["orig"]]
        a_key = (orig["source_a"], str(orig["id_a"]))
        a_projs = project_idx.get(a_key, [])[:PROJECTS_PER_SIDE]
        b_projs = projects_for_registry_entry(
            orig["id_b"], registry_data, project_idx
        )[:PROJECTS_PER_SIDE]
        enriched.append({
            "i":          p["i"],
            "orig":       p["orig"],
            "a_name":     orig["name_a"],
            "a_source":   orig["source_a"],
            "a_projects": [fmt_project(x) for x in a_projs],
            "b_name":     orig["name_b"],
            "b_projects": [fmt_project(x) for x in b_projs],
        })
    with open(batch_out_path, "w") as f:
        json.dump(enriched, f, indent=2, ensure_ascii=False)
    return len(enriched)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True,
                    help="input slim batch JSON (with i, orig, a, b)")
    ap.add_argument("--out", required=True,
                    help="output enriched batch JSON")
    args = ap.parse_args()

    print("loading project index …", flush=True)
    project_idx = load_project_index()
    print(f"  {len(project_idx)} (source, id) keys with project lists", flush=True)

    registry_data = json.load(open("data/id_registry_architects.json"))
    tiebreak_full = json.load(open("data/canonical/architect_tiebreak_pairs.json"))

    n = enrich_batch(args.inp, args.out, project_idx, registry_data, tiebreak_full)
    print(f"✓ enriched {n} pairs → {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
