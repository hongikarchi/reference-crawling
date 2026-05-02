"""Plan B: generic pair-wise architect matcher.

Reuses the proven helpers from canonical/match_architects.py
(_normalize_name, _normalize_country, _candidates_for, _country_match,
_is_substring_match, _verdict_for, AUTO_ACCEPT_SIM, STRONG_SIM,
TIEBREAK_FLOOR) but takes any two architect lists as input. Six runs
of this — one per source pair — produces the verdict tables that
match_architects_plan_b.py unions into canonical clusters.

Item dict shape (uniform across sources):
  {
    "id":            str,    # source-native ID, str-cast
    "name":          str,    # display name
    "name_core":     str,    # _normalize_name(name) or name.lower()
    "country":       str?,   # raw country (display)
    "country_norm":  str?,   # _normalize_country(country)
    "countries":     list[str],  # for multi-country (metalocus clusters);
                                  # else == [country_norm] if present, else []
    "project_count": int?,
    "source":        str,    # 'metalocus'|'divisare'|'architizer'|'archello'
  }
"""
from __future__ import annotations

import json
from collections import Counter
from typing import Optional

from rapidfuzz import fuzz, process

from canonical.match_architects import (
    _normalize_name, _normalize_country, _country_match, _is_substring_match,
    _verdict_for,
    AUTO_ACCEPT_SIM, STRONG_SIM, TIEBREAK_FLOOR, MAX_CANDIDATES_PER_CLUSTER,
)


def _candidates_for(item: dict, side_b: list[dict]) -> list[tuple[dict, float]]:
    """Top-K name-similar candidates from side_b for this item.
    Mirrors the existing 2-source matcher's _candidates_for so verdicts
    on metalocus×divisare reproduce 1:1.
    """
    if not side_b:
        return []
    target = item["name_core"] or item["name"].lower()
    if not target:
        return []
    cand_strings = [b["name_core"] for b in side_b]
    sort_scores = process.cdist([target], cand_strings, scorer=fuzz.token_sort_ratio)[0]
    set_scores  = process.cdist([target], cand_strings, scorer=fuzz.token_set_ratio)[0]
    paired = [(side_b[i], max(float(sort_scores[i]), float(set_scores[i])))
              for i in range(len(side_b))]
    paired.sort(key=lambda t: -t[1])
    return paired[:MAX_CANDIDATES_PER_CLUSTER]


def match_pair(
    side_a: list[dict],
    side_b: list[dict],
    *,
    label_a: str,
    label_b: str,
    output_path: Optional[str] = None,
) -> dict:
    """Match every item in side_a to its best candidate in side_b.

    Verdict tiers mirror match_architects.py:
      • auto_accept           — name_sim ≥ AUTO_ACCEPT_SIM
      • accept_with_country   — name_sim ≥ STRONG_SIM AND country matches
      • needs_tiebreak        — name_sim ≥ TIEBREAK_FLOOR but ambiguous
      • no_match              — best below TIEBREAK_FLOOR
    """
    matches: list[dict] = []
    verdict_counts: Counter = Counter()

    for i, a in enumerate(side_a, 1):
        cands = _candidates_for(a, side_b)
        if not cands:
            verdict = "no_match"
            top_sim = 0.0
            picked = None
            country_match = False
            alt = []
        else:
            a_core = a["name_core"]
            # Bring exact core match (if any) to top
            exact_idx = next((idx for idx, (b, _) in enumerate(cands)
                              if b["name_core"] == a_core and a_core), None)
            if exact_idx is not None and exact_idx > 0:
                cands.insert(0, cands.pop(exact_idx))
            top_b, top_sim = cands[0]
            second_sim = cands[1][1] if len(cands) > 1 else 0.0
            country_match = _country_match(a.get("countries") or [], top_b.get("country_norm") or "")
            exact_core_match = (a_core != "" and top_b["name_core"] == a_core)
            subset_match = (
                a_core != "" and top_b["name_core"] != "" and not exact_core_match
                and (_is_substring_match(a_core, top_b["name_core"])
                     or _is_substring_match(top_b["name_core"], a_core))
            )
            verdict = _verdict_for(top_sim, second_sim, country_match,
                                   exact_core_match, subset_match)
            picked = top_b if verdict in ("auto_accept", "accept_with_country") else None
            alt = [
                {"id": b["id"], "name": b["name"], "country": b.get("country"),
                 "sim": round(s, 1),
                 "country_match": _country_match(a.get("countries") or [],
                                                 b.get("country_norm") or "")}
                for b, s in cands[1:5]
            ]

        verdict_counts[verdict] += 1
        matches.append({
            f"{label_a}_id":            a["id"],
            f"{label_a}_name":          a["name"],
            f"{label_a}_countries":     a.get("countries") or [],
            f"{label_a}_project_count": a.get("project_count") or 0,
            "verdict":                  verdict,
            "name_sim":                 round(top_sim, 1),
            "country_match":            country_match,
            f"{label_b}_id":            picked["id"] if picked else None,
            f"{label_b}_name":          picked["name"] if picked else None,
            f"{label_b}_country":       picked.get("country") if picked else None,
            "top_candidate": ({"id": cands[0][0]["id"], "name": cands[0][0]["name"],
                               "country": cands[0][0].get("country"),
                               "sim": round(cands[0][1], 1)} if cands else None),
            "alt_candidates": alt,
        })

        if i % 500 == 0:
            print(f"  [{label_a}×{label_b}] scored {i}/{len(side_a)}; "
                  f"tally: {dict(verdict_counts)}", flush=True)

    print(f"\n[{label_a}×{label_b}] final verdicts: {dict(verdict_counts)}",
          flush=True)

    summary = {
        "side_a":            label_a,
        "side_b":            label_b,
        "input_a_size":      len(side_a),
        "input_b_size":      len(side_b),
        "verdict_counts":    dict(verdict_counts),
        "thresholds": {
            "AUTO_ACCEPT_SIM": AUTO_ACCEPT_SIM,
            "STRONG_SIM":      STRONG_SIM,
            "TIEBREAK_FLOOR":  TIEBREAK_FLOOR,
        },
    }
    payload = {"summary": summary, "matches": matches}
    if output_path:
        import os
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        print(f"  → saved to {output_path}", flush=True)
    return payload


def normalize_for_match(item: dict, source: str,
                        clusters_data: Optional[dict] = None) -> dict:
    """Convert a _source_loaders dict (or metalocus cluster dict) into the
    uniform match-item shape used by match_pair.

    For metalocus, the input is a cluster dict from
    metalocus_architect_clusters.json (multi-country, has building_ids).
    For other sources, it's the per-architect dict from _source_loaders.
    """
    name = item["name"] if source != "metalocus" else item["canonical_name"]
    core = _normalize_name(name) or name.lower()
    if source == "metalocus":
        countries_norm = sorted({_normalize_country(c) for c in (item.get("countries") or []) if c})
        return {
            "id":            item["canonical_id"],
            "name":          name,
            "name_core":     core,
            "country":       None,
            "country_norm":  None,
            "countries":     countries_norm,
            "project_count": item.get("building_count") or 0,
            "source":        "metalocus",
        }
    country = item.get("country")
    country_norm = _normalize_country(country) if country else None
    return {
        "id":            str(item["source_id"]),
        "name":          name,
        "name_core":     core,
        "country":       country,
        "country_norm":  country_norm,
        "countries":     [country_norm] if country_norm else [],
        "project_count": item.get("project_count") or 0,
        "source":        source,
    }
