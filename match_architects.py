#!/usr/bin/env python3
"""Stage B-1: match metalocus architect clusters → Divisare architect IDs.

Inputs:
  data/metalocus_architect_clusters.json   (Stage A output)
  data/divisare.db                         (Phase 1 crawl output)

Output:
  data/match/metalocus_architect_to_divisare.json
    {
      "summary": {...},
      "matches": [
        {
          "metaloc_id": "metaloc_arch_0000",
          "metaloc_name": "Foster + Partners",
          "metaloc_building_count": 40,
          "divisare_id": 8909,
          "divisare_name": "Foster + Partners",
          "divisare_country": "United Kingdom",
          "verdict": "auto_accept" | "accept_with_country" | "needs_tiebreak" | "no_match",
          "name_sim": 100.0,
          "country_match": true,
          "alt_candidates": [...]
        }, ...
      ]
    }

Verdict tiers (decreasing confidence):
  • auto_accept           — name_sim ≥ AUTO_ACCEPT_SIM (very high, no ambiguity)
  • accept_with_country   — name_sim ≥ STRONG_SIM AND country matches
  • needs_tiebreak        — name_sim ≥ TIEBREAK_FLOOR but ambiguous (Opus or hand)
  • no_match              — best candidate below TIEBREAK_FLOOR
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from typing import Optional

from rapidfuzz import fuzz, process

CLUSTERS_PATH = "data/metalocus_architect_clusters.json"
DIVISARE_DB   = "data/divisare.db"
OUTPUT_PATH   = "data/match/metalocus_architect_to_divisare.json"

AUTO_ACCEPT_SIM       = 95.0   # confident regardless of country
STRONG_SIM            = 85.0   # confident IF country matches
TIEBREAK_FLOOR        = 78.0   # below this, no signal
MAX_CANDIDATES_PER_CLUSTER = 8

# Country-name variants — Divisare uses full names ('United Kingdom'),
# metalocus may store ISO/short forms. Add as needed when hand-reviewing.
COUNTRY_ALIASES = {
    "uk": "united kingdom", "gb": "united kingdom", "england": "united kingdom",
    "scotland": "united kingdom", "wales": "united kingdom",
    "us": "united states", "usa": "united states",
    "south korea": "korea", "republic of korea": "korea",
    "russia": "russian federation",
    "uae": "united arab emirates",
    "czechia": "czech republic",
}


def _normalize_country(s: Optional[str]) -> str:
    if not s:
        return ""
    s = s.strip().lower()
    return COUNTRY_ALIASES.get(s, s)


def _normalize_name(s: str) -> str:
    """Normalize for fuzzy matching — same generic-token strip as Stage A."""
    from metalocus_consolidate import _extract_core
    return _extract_core(s)


def _looks_like_country(s: str) -> bool:
    """Reject country fields that contain co-founder names from Divisare's
    parser bugs (e.g. 'Ascan Mergenthaler' under 'country').

    Heuristic: real country names are short, contain no person-name patterns.
    """
    if not s:
        return False
    s = s.strip()
    if not s or len(s) > 40:
        return False
    # Person names typically have multi-word capitalized patterns
    if re.search(r"\b(?:[A-Z][a-z]{2,}\s+[A-Z][a-z]{2,})\b", s) and " " in s:
        words = s.split()
        # Country names occasionally have 2+ words ('United Kingdom', 'New Zealand')
        # but ALL-uppercase first letters with > 2 words usually = person
        if len(words) >= 3:
            return False
    return True


def _load_divisare_architects(db_path: str) -> list[dict]:
    rows = []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        for r in conn.execute(
            "SELECT id, slug, name, country, city, project_count_seen "
            "FROM divisare_architects"
        ):
            country = r["country"] if _looks_like_country(r["country"]) else None
            rows.append({
                "id": r["id"],
                "slug": r["slug"],
                "name": r["name"],
                "country": country,
                "country_norm": _normalize_country(country),
                "city": r["city"],
                "project_count": r["project_count_seen"],
                "name_core": _normalize_name(r["name"]) or r["name"].lower(),
            })
    return rows


def _load_metaloc_clusters(path: str) -> list[dict]:
    with open(path) as f:
        data = json.load(f)
    out = []
    for c in data["clusters"]:
        if c.get("is_noise"):
            continue
        norm_countries = sorted({_normalize_country(co) for co in c.get("countries") or []
                                 if co})
        out.append({
            "metaloc_id":      c["canonical_id"],
            "name":            c["canonical_name"],
            "building_ids":    c["building_ids"],
            "building_count":  c["building_count"],
            "raw_aliases":     c["raw_aliases"],
            "countries":       norm_countries,
            "name_core":       _normalize_name(c["canonical_name"])
                              or c["canonical_name"].lower(),
        })
    out.sort(key=lambda x: -x["building_count"])
    return out


def _candidates_for(cluster: dict, divisare: list[dict]) -> list[tuple[dict, float]]:
    """Top-K name-similar candidates. Score = max(token_sort_ratio, token_set_ratio)
    — token_set tolerates word-order changes ('BIG Bjarke Ingels' ↔ 'Bjarke Ingels BIG'),
    token_sort prevents shared-token bloat."""
    if not divisare:
        return []
    target = cluster["name_core"] or cluster["name"].lower()
    if not target:
        return []
    cand_strings = [d["name_core"] for d in divisare]
    sort_scores = process.cdist([target], cand_strings, scorer=fuzz.token_sort_ratio)[0]
    set_scores  = process.cdist([target], cand_strings, scorer=fuzz.token_set_ratio)[0]
    paired = [(divisare[i], max(float(sort_scores[i]), float(set_scores[i])))
              for i in range(len(divisare))]
    paired.sort(key=lambda t: -t[1])
    return paired[:MAX_CANDIDATES_PER_CLUSTER]


def _country_match(cluster_countries: list[str], div_country: str) -> bool:
    if not div_country:
        return False
    if not cluster_countries:
        return False
    return div_country in cluster_countries


def _is_substring_match(short: str, long: str) -> bool:
    """short's tokens are all in long's tokens. Demand at least one token of
    length ≥ 3 (catches famous short firm names like 'MAD', 'BIG', 'OMA',
    'KAAN' that fail a stricter rare-token gate).
    Generic tokens are already pre-stripped from cores, so any surviving
    3-char token is substantive (not 'the', 'and', etc).
    """
    short_toks = short.split()
    long_toks = long.split()
    if not short_toks or not long_toks:
        return False
    if not any(len(t) >= 3 for t in short_toks):
        return False
    return set(short_toks).issubset(set(long_toks))


def _verdict_for(top_sim: float, second_sim: float, country_match: bool,
                 exact_core_match: bool, subset_match: bool) -> str:
    # Exact normalized-core match is the strongest signal.
    if exact_core_match and top_sim >= AUTO_ACCEPT_SIM:
        return "auto_accept"
    # Subset (one core contained in the other on substantive tokens) is nearly
    # as strong: handles 'KAAN' vs 'KAAN Kees Kaan Vincent…', 'Filipe Pina' vs
    # 'FPA Filipe Pina Arquitectura', 'SANAA' vs 'SANAA Kazuyo Sejima…'.
    if subset_match and top_sim >= 80.0:
        return "auto_accept"
    if top_sim >= AUTO_ACCEPT_SIM and (top_sim - second_sim) >= 5.0:
        return "auto_accept"
    if top_sim >= STRONG_SIM and country_match and (top_sim - second_sim) >= 3.0:
        return "accept_with_country"
    if top_sim >= TIEBREAK_FLOOR:
        return "needs_tiebreak"
    return "no_match"


def match_all(clusters_path: str = CLUSTERS_PATH,
              divisare_db: str  = DIVISARE_DB,
              output_path: str  = OUTPUT_PATH) -> dict:
    print(f"loading clusters from {clusters_path}...")
    clusters = _load_metaloc_clusters(clusters_path)
    print(f"loading divisare architects from {divisare_db}...")
    divisare = _load_divisare_architects(divisare_db)
    print(f"  {len(clusters)} metalocus clusters × {len(divisare)} Divisare architects")

    # No country pre-filter: Divisare's `country` column is ~95% clean but
    # the famous-firm rows (MVRDV, HERZOG & DE MEURON, Studioninedots) have
    # corrupted values where a co-founder name leaked into the country slot.
    # Any country-based pre-filter therefore EXCLUDES exactly the high-impact
    # firms we most want to match. Country stays as a soft scoring signal.
    matches: list[dict] = []
    verdict_counts = Counter()

    for i, cluster in enumerate(clusters, 1):
        cands = _candidates_for(cluster, divisare)
        if not cands:
            verdict = "no_match"
            top_sim = 0.0
            picked = None
            country_match = False
            alt = []
        else:
            # Reorder cands so an exact-core match (if any) becomes top
            cluster_core = cluster["name_core"]
            exact_idx = next((i for i, (d, _) in enumerate(cands)
                              if d["name_core"] == cluster_core and cluster_core), None)
            if exact_idx is not None and exact_idx > 0:
                cands.insert(0, cands.pop(exact_idx))
            top_div, top_sim = cands[0]
            second_sim = cands[1][1] if len(cands) > 1 else 0.0
            country_match = _country_match(cluster["countries"], top_div["country_norm"])
            exact_core_match = (cluster_core != "" and top_div["name_core"] == cluster_core)
            subset_match = (
                cluster_core != "" and top_div["name_core"] != "" and not exact_core_match
                and (_is_substring_match(cluster_core, top_div["name_core"])
                     or _is_substring_match(top_div["name_core"], cluster_core))
            )
            verdict = _verdict_for(top_sim, second_sim, country_match,
                                   exact_core_match, subset_match)
            picked = top_div if verdict in ("auto_accept", "accept_with_country") else None
            alt = [
                {"divisare_id": d["id"], "divisare_name": d["name"],
                 "country": d["country"], "sim": round(s, 1),
                 "country_match": _country_match(cluster["countries"], d["country_norm"])}
                for d, s in cands[1:5]
            ]

        verdict_counts[verdict] += 1
        matches.append({
            "metaloc_id": cluster["metaloc_id"],
            "metaloc_name": cluster["name"],
            "metaloc_countries": cluster["countries"],
            "metaloc_building_count": cluster["building_count"],
            "verdict": verdict,
            "name_sim": round(top_sim, 1),
            "country_match": country_match,
            "divisare_id": picked["id"] if picked else None,
            "divisare_name": picked["name"] if picked else None,
            "divisare_country": picked["country"] if picked else None,
            "top_candidate": {
                "divisare_id": cands[0][0]["id"], "divisare_name": cands[0][0]["name"],
                "divisare_country": cands[0][0]["country"], "sim": round(cands[0][1], 1),
            } if cands else None,
            "alt_candidates": alt,
        })

        if i % 250 == 0:
            print(f"  scored {i}/{len(clusters)}; current verdict tally: {dict(verdict_counts)}")

    print(f"\nfinal verdicts: {dict(verdict_counts)}")
    matched_buildings = sum(
        m["metaloc_building_count"] for m in matches
        if m["verdict"] in ("auto_accept", "accept_with_country")
    )
    total_buildings = sum(m["metaloc_building_count"] for m in matches)
    print(f"buildings covered by confident matches: {matched_buildings}/{total_buildings} "
          f"({matched_buildings/max(total_buildings,1):.1%})")

    summary = {
        "input_clusters": len(clusters),
        "input_divisare_architects": len(divisare),
        "verdict_counts": dict(verdict_counts),
        "buildings_total": total_buildings,
        "buildings_covered_by_confident_matches": matched_buildings,
        "thresholds": {
            "AUTO_ACCEPT_SIM": AUTO_ACCEPT_SIM,
            "STRONG_SIM": STRONG_SIM,
            "TIEBREAK_FLOOR": TIEBREAK_FLOOR,
        },
    }
    payload = {"summary": summary, "matches": matches}

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(f"\n✓ saved → {output_path}")
    return payload


def main(argv: list[str]) -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--clusters", default=CLUSTERS_PATH)
    p.add_argument("--divisare-db", default=DIVISARE_DB)
    p.add_argument("--output", default=OUTPUT_PATH)
    args = p.parse_args(argv)
    match_all(args.clusters, args.divisare_db, args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
