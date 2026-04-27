#!/usr/bin/env python3
"""Stage B-2: match metalocus buildings → Divisare projects (lite-index).

Strategy: chain on the architect matcher (Stage B-1).
  • For each metalocus building, look up its architect cluster(s).
  • Map cluster → divisare_architect_id via metalocus_architect_to_divisare.json.
  • Candidate pool = all Divisare projects by those architect_ids (typically 5-15).
  • Score each candidate: name token_set_ratio + country match + year proximity.
  • Verdict tiers:
        accept_high       — name_sim ≥ 90 AND country match
                            (or name_sim ≥ 95 with no country signal in either)
        accept_medium     — name_sim ≥ 80 AND year match (year ± 2)
        no_match          — best name_sim < 80, or no architect link

Output: data/canonical/match/metalocus_to_divisare_buildings.json
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from typing import Optional

from rapidfuzz import fuzz

CLUSTERS_PATH        = "data/canonical/metalocus_architect_clusters.json"
ARCH_MATCH_PATH      = "data/canonical/match/metalocus_architect_to_divisare.json"
METALOC_FINAL_PATH   = "data/enrich/4_buildings_final.json"
DIVISARE_DB          = "data/crawl/divisare.db"
OUTPUT_PATH          = "data/canonical/match/metalocus_to_divisare_buildings.json"

NAME_SIM_HIGH        = 90.0
NAME_SIM_NO_COUNTRY  = 95.0
NAME_SIM_MEDIUM      = 80.0
NAME_SIM_FLOOR       = 70.0
YEAR_TOLERANCE       = 2


def _norm_country(s: Optional[str]) -> str:
    if not s:
        return ""
    return s.strip().lower()


def _norm_name(s: str) -> str:
    """Lowercase + strip punctuation; keeps every substantive token (we do
    NOT want to drop 'house' / 'tower' / 'museum' here — they're the building
    type and matter for matching)."""
    if not s:
        return ""
    import re
    s = re.sub(r"[^\w\s]", " ", s).lower()
    return re.sub(r"\s+", " ", s).strip()


def _load_arch_map() -> dict[str, int]:
    """{metaloc_cluster_id: divisare_architect_id} for confident matches only."""
    with open(ARCH_MATCH_PATH) as f:
        data = json.load(f)
    out = {}
    for m in data["matches"]:
        if m["verdict"] in ("auto_accept", "accept_with_country") and m["divisare_id"]:
            out[m["metaloc_id"]] = m["divisare_id"]
    return out


def _load_building_to_clusters() -> dict[str, list[str]]:
    with open(CLUSTERS_PATH) as f:
        data = json.load(f)
    return data.get("building_to_canonical") or {}


def _load_metalocus_buildings() -> list[dict]:
    with open(METALOC_FINAL_PATH) as f:
        return json.load(f)


def _load_divisare_projects_by_architect(db_path: str) -> dict[int, list[dict]]:
    """{divisare_architect_id: [project_dict, ...]}"""
    out: dict[int, list[dict]] = defaultdict(list)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        for r in conn.execute(
            "SELECT id, slug, name, architect_ids, architect_names, "
            "location_country, location_city, project_year "
            "FROM divisare_projects"
        ):
            arch_ids_raw = r["architect_ids"] or "[]"
            try:
                arch_ids = json.loads(arch_ids_raw)
            except json.JSONDecodeError:
                continue
            entry = {
                "id": r["id"], "slug": r["slug"], "name": r["name"],
                "architect_ids": arch_ids,
                "architect_names": json.loads(r["architect_names"] or "[]"),
                "location_country": r["location_country"],
                "location_city": r["location_city"],
                "project_year": r["project_year"],
                "name_norm": _norm_name(r["name"]),
                "country_norm": _norm_country(r["location_country"]),
            }
            for aid in arch_ids:
                out[aid].append(entry)
    return out


def _score_candidate(building: dict, project: dict) -> tuple[float, dict]:
    """Returns (combined_score, signals_dict)."""
    metaloc_name = _norm_name(building.get("name_en") or building.get("project_name") or "")
    proj_name = project["name_norm"]
    name_sim = float(fuzz.token_set_ratio(metaloc_name, proj_name)) if metaloc_name and proj_name else 0.0

    metaloc_country = _norm_country(building.get("location_country"))
    proj_country = project["country_norm"]
    country_match = bool(metaloc_country and proj_country and metaloc_country == proj_country)

    metaloc_year = building.get("year")
    proj_year = project["project_year"]
    year_match = (metaloc_year and proj_year
                  and abs(int(metaloc_year) - int(proj_year)) <= YEAR_TOLERANCE)

    return name_sim, {
        "name_sim": round(name_sim, 1),
        "country_match": country_match,
        "year_match": bool(year_match),
        "metaloc_year": metaloc_year, "proj_year": proj_year,
    }


def _verdict_for(name_sim: float, country_match: bool,
                 year_match: bool, has_country_signal: bool) -> str:
    # name_sim ≥ 95 is strong enough to override country mismatch — country
    # signal is noisy (architect-page parser bugs, free-text city/country
    # variants). Architect is already verified to be the same firm, so a
    # near-perfect name match almost certainly identifies the same project.
    if name_sim >= 95.0:
        return "accept_high"
    if name_sim >= NAME_SIM_HIGH and country_match:
        return "accept_high"
    if name_sim >= NAME_SIM_NO_COUNTRY and not has_country_signal:
        return "accept_high"
    if name_sim >= NAME_SIM_MEDIUM and country_match:
        return "accept_medium"
    if name_sim >= NAME_SIM_MEDIUM and year_match:
        return "accept_medium"
    if name_sim >= NAME_SIM_FLOOR:
        return "needs_tiebreak"
    return "no_match"


def match_all() -> dict:
    print(f"loading architect map from {ARCH_MATCH_PATH}...")
    arch_map = _load_arch_map()
    print(f"  {len(arch_map)} confident architect matches")

    print(f"loading building→cluster map from {CLUSTERS_PATH}...")
    building_to_clusters = _load_building_to_clusters()

    print(f"loading metalocus buildings from {METALOC_FINAL_PATH}...")
    buildings = _load_metalocus_buildings()
    print(f"  {len(buildings)} buildings")

    print(f"loading Divisare projects by architect from {DIVISARE_DB}...")
    proj_by_arch = _load_divisare_projects_by_architect(DIVISARE_DB)
    print(f"  {sum(len(v) for v in proj_by_arch.values())} project-rows indexed under "
          f"{len(proj_by_arch)} architects")

    matches: list[dict] = []
    verdict_counts = Counter()

    for i, b in enumerate(buildings, 1):
        bid = b["building_id"]
        clusters = building_to_clusters.get(bid, [])
        # Map clusters → Divisare architect IDs (only those with confident match)
        div_arch_ids = []
        for cid in clusters:
            div_aid = arch_map.get(cid)
            if div_aid:
                div_arch_ids.append(div_aid)

        if not div_arch_ids:
            verdict = "no_match"
            picked = None
            signals = {"reason": "no_architect_link"}
            alt = []
            verdict_counts[verdict] += 1
            matches.append({
                "metalocus_building_id": bid,
                "metalocus_name": b.get("name_en") or b.get("project_name"),
                "metalocus_country": b.get("location_country"),
                "metalocus_year": b.get("year"),
                "verdict": verdict, "signals": signals,
                "divisare_id": None, "divisare_name": None,
                "divisare_architect_ids_searched": div_arch_ids,
                "alt_candidates": alt,
            })
            continue

        # Build candidate pool — projects by ANY of the matched architects
        seen = set()
        pool: list[dict] = []
        for aid in div_arch_ids:
            for p in proj_by_arch.get(aid, []):
                if p["id"] not in seen:
                    seen.add(p["id"])
                    pool.append(p)

        if not pool:
            verdict = "no_match"
            verdict_counts[verdict] += 1
            matches.append({
                "metalocus_building_id": bid,
                "metalocus_name": b.get("name_en") or b.get("project_name"),
                "metalocus_country": b.get("location_country"),
                "metalocus_year": b.get("year"),
                "verdict": verdict, "signals": {"reason": "architect_has_no_projects"},
                "divisare_id": None, "divisare_name": None,
                "divisare_architect_ids_searched": div_arch_ids,
                "alt_candidates": [],
            })
            continue

        # Score every candidate; pick best
        scored = [(p, *_score_candidate(b, p)) for p in pool]
        scored.sort(key=lambda t: -t[1])
        top_p, top_sim, top_sig = scored[0]
        has_country_signal = bool(_norm_country(b.get("location_country"))
                                  and top_p["country_norm"])
        verdict = _verdict_for(top_sim, top_sig["country_match"],
                               top_sig["year_match"], has_country_signal)
        verdict_counts[verdict] += 1

        picked = top_p if verdict in ("accept_high", "accept_medium") else None
        alt = [
            {"divisare_id": p["id"], "divisare_name": p["name"],
             "country": p["location_country"], "year": p["project_year"],
             "name_sim": round(s, 1)}
            for p, s, _ in scored[1:5]
        ]

        matches.append({
            "metalocus_building_id": bid,
            "metalocus_name": b.get("name_en") or b.get("project_name"),
            "metalocus_country": b.get("location_country"),
            "metalocus_year": b.get("year"),
            "verdict": verdict, "signals": top_sig,
            "divisare_id": picked["id"] if picked else None,
            "divisare_slug": picked["slug"] if picked else None,
            "divisare_name": picked["name"] if picked else None,
            "divisare_architect_ids_searched": div_arch_ids,
            "candidate_pool_size": len(pool),
            "alt_candidates": alt,
        })

        if i % 500 == 0:
            print(f"  scored {i}/{len(buildings)}; verdicts: {dict(verdict_counts)}")

    print(f"\nfinal verdicts: {dict(verdict_counts)}")
    confident = sum(verdict_counts[v] for v in ("accept_high", "accept_medium"))
    print(f"buildings with confident project match: {confident}/{len(buildings)} "
          f"({confident/len(buildings):.1%})")

    summary = {
        "input_buildings": len(buildings),
        "input_arch_map_size": len(arch_map),
        "verdict_counts": dict(verdict_counts),
        "buildings_with_confident_match": confident,
        "thresholds": {
            "NAME_SIM_HIGH": NAME_SIM_HIGH, "NAME_SIM_NO_COUNTRY": NAME_SIM_NO_COUNTRY,
            "NAME_SIM_MEDIUM": NAME_SIM_MEDIUM, "NAME_SIM_FLOOR": NAME_SIM_FLOOR,
            "YEAR_TOLERANCE": YEAR_TOLERANCE,
        },
    }

    payload = {"summary": summary, "matches": matches}
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(f"\n✓ saved → {OUTPUT_PATH}")
    return payload


def main(argv: list[str]) -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    args = p.parse_args(argv)
    match_all()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
