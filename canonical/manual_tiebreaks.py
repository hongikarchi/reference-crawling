#!/usr/bin/env python3
"""One-shot: apply Opus's in-conversation manual tiebreak decisions on top of
the auto-merge cluster output, since this dev environment has no
ANTHROPIC_API_KEY (the user runs Claude via Max subscription, not API).

Re-runs union-find with the auto-merge pairs PLUS the manual MERGE pairs,
recomputes clusters, picks canonical names, and rewrites the same JSON file.
"""

import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from canonical.consolidate import (
    INPUT_PATH, OUTPUT_PATH,
    _UnionFind, _extract_mentions, _pairwise, _pick_canonical_name,
)

# (a, b, decision, canonical_name_override_or_None)
# Decisions made by Opus in-conversation (covered by Claude Max), not API.
MANUAL_DECISIONS = [
    ("Luppa Architects. Arquitecto", "Luppa Arquitects", True, "Luppa Architects"),
    ("Niall McLaughlin Architects", "Níall McLaughlin Architects", True, "Níall McLaughlin Architects"),
    ("Atelier Sergio Rebelo", "Atelier Sérgio Rebelo", True, "Atelier Sérgio Rebelo"),
    ("SOM. Skidmore, Owings & Merril", "Skidmore, Owings & Merrill", True, "Skidmore, Owings & Merrill (SOM)"),
    ("Fenwick Iribarren Architects", "Fenwick Iribarren Architects(FIA)", True, "Fenwick Iribarren Architects"),
    ("OMA/Shohei Shigematsu", "Shohei Shigematsu", True, "Shohei Shigematsu (OMA NY)"),
    ("Pablo Manuel Millán Millán", "Pablo-M. Millán-Millán", True, "Pablo Manuel Millán Millán"),
    ("Estudio Muñoz Miranda", "Muñoz Miranda Arquitectos", True, "Muñoz Miranda Arquitectos"),
    ("IUA Ignacio Urquiza Arquitectos", "Ignacio Urquiza Arquitectos", True, "Ignacio Urquiza Arquitectos"),
    ("OMA Octavio Mestre Arquitectos", "Octavio Mestre", True, "Octavio Mestre Arquitectos"),
    ("OMA Octavio Mestre Arquitectos", "Octavio Mestre Arquitectos", True, "Octavio Mestre Arquitectos"),
    ("Zaha Hadid Architects (ZHA)", "Zaha Hadid Architects (ZHA).ZHA Design", True, "Zaha Hadid Architects (ZHA)"),
    ("Zaha Hadid Architects (ZHA).Design", "Zaha Hadid Architects (ZHA).ZHA Design", True, "Zaha Hadid Architects (ZHA)"),
    ("Zaha Hadid Architects (ZHA).ZHA Design", "Zaha Hadid Architects(ZHA)", True, "Zaha Hadid Architects (ZHA)"),
    ("Zaha Hadid Architects (ZHA).ZHA Design", "Zaha Hadid Architects(ZHA). Design", True, "Zaha Hadid Architects (ZHA)"),
    ("Sancho-Madridejos Architecture Office", "Sancho-Madridejos. Leed architects", True, "Sancho-Madridejos Architecture Office"),
    ("Snøhetta. Architecture, Interior Architecture, Landscape Architecture and Design",
     "Snøhetta. Architecture, Interior Architecture, Landscape Architecture and Graphic design", True, "Snøhetta"),
    ("BIG - Bjarke Ingels group", "Bjarke Ingels Group", True, "Bjarke Ingels Group (BIG)"),
    ("BIG – Bjarke Ingels Group", "Bjarke Ingels Group", True, "Bjarke Ingels Group (BIG)"),
    ("Barozzi Veiga", "Barozzi Veiga,Tab Architects", True, "Barozzi Veiga"),
    ("J.MAYER.H und Partner, Architekten", "J.MAYER.H und Partner,Architekten mbB", True, "J.MAYER.H und Partner Architekten mbB"),
    ("Muñoz Miranda Arquitectos", "Muñoz Miranda Arquitectos S.L.P", True, "Muñoz Miranda Arquitectos S.L.P"),
    ("Anna & Eugeni Bach", "Anna & Eugeni Bach/ Bach arquitectes", True, "Anna & Eugeni Bach (Bach arquitectes)"),
    ("FAAB Architektura", "QARTA Architektura", False, None),
    ("Taller d'Arquitectura", "Taller de Arquitectura", False, None),
    ("Taller de Arquitectura", "V Taller", False, None),
    # Acronym/branch merges that fall below fuzzy threshold but are well-known.
    ("BIG", "Bjarke Ingels Group", True, "Bjarke Ingels Group (BIG)"),
    ("OMA", "OMANew York", True, "OMA"),
    ("OMA", "OMA New York", True, "OMA"),
    ("Foster", "Foster Partners", True, "Foster + Partners"),
]


def apply():
    with open(INPUT_PATH) as f:
        buildings = json.load(f)

    mentions, building_to_mentions = _extract_mentions(buildings)
    auto, llm_q = _pairwise(mentions)

    print(f"unique mentions: {len(mentions)}")
    print(f"auto pairs: {len(auto)}; tiebreak pairs queued: {len(llm_q)}")

    uf = _UnionFind(list(mentions.keys()))
    for a, b, _ in auto:
        uf.union(a, b)

    pre_manual_count = len(uf.groups())
    print(f"clusters after auto-merge: {pre_manual_count}")

    # Apply manual decisions
    canonical_overrides: dict[frozenset, str] = {}  # cluster_key → preferred name
    decisions_log = []
    merged = 0
    for a, b, decide_merge, override in MANUAL_DECISIONS:
        if a not in mentions or b not in mentions:
            decisions_log.append({"a": a, "b": b, "skipped": "mention_not_found"})
            continue
        if decide_merge:
            uf.union(a, b)
            merged += 1
            if override:
                canonical_overrides.setdefault(frozenset({a, b}), override)
        decisions_log.append({
            "a": a, "b": b, "merged": decide_merge,
            "decided_by": "opus_manual",
            "canonical_override": override if decide_merge else None,
        })

    final = uf.groups()
    print(f"clusters after manual tiebreak: {len(final)} (merged {merged}/24)")

    # Build output
    final.sort(key=lambda c: -sum(len(mentions[m]["building_ids"]) for m in c))
    output_clusters = []
    mention_to_canonical: dict[str, str] = {}
    for i, cluster in enumerate(final):
        cid = f"metaloc_arch_{i:04d}"
        # Override canonical name if this cluster contains any manually-decided
        # pair (mark the cluster with the first matching override).
        manual_canonical = None
        for key, name in canonical_overrides.items():
            if key.issubset(cluster):
                manual_canonical = name
                break
        canonical_name = manual_canonical or _pick_canonical_name(cluster, [])
        all_bids: list[str] = []
        all_countries: set[str] = set()
        all_full_raws: set[str] = set()
        for m in cluster:
            mention_to_canonical[m] = cid
            all_bids.extend(mentions[m]["building_ids"])
            all_countries |= mentions[m]["countries"]
            all_full_raws |= mentions[m]["raw_full_strings"]
        decided_via_manual = any(
            (a in cluster and b in cluster and decide)
            for a, b, decide, _ in MANUAL_DECISIONS
        )
        is_noise = all(mentions[m]["is_noise"] for m in cluster)
        output_clusters.append({
            "canonical_id": cid,
            "canonical_name": canonical_name,
            "raw_aliases": sorted(cluster),
            "raw_full_source_strings_sample": sorted(all_full_raws)[:5],
            "building_ids": sorted(set(all_bids)),
            "building_count": len(set(all_bids)),
            "countries": sorted(all_countries),
            "decided_by": "opus_manual" if decided_via_manual else "auto",
            "is_noise": is_noise,
        })

    bld_to_canonical: dict[str, list[str]] = {}
    for bid, ms in building_to_mentions.items():
        cids = sorted({mention_to_canonical[m] for m in ms if m in mention_to_canonical})
        bld_to_canonical[bid] = cids

    summary = {
        "input_file": INPUT_PATH,
        "buildings_with_architect": len(building_to_mentions),
        "unique_mentions": len(mentions),
        "noise_mentions_excluded": sum(1 for r in mentions.values() if r["is_noise"]),
        "auto_merge_pairs": len(auto),
        "tiebreak_candidate_pairs": len(llm_q),
        "manual_tiebreak_decisions": len(MANUAL_DECISIONS),
        "manual_tiebreak_merged": merged,
        "final_cluster_count": len(output_clusters),
        "noise_clusters": sum(1 for c in output_clusters if c["is_noise"]),
        "buildings_with_n_canonical_architects": dict(
            Counter(len(v) for v in bld_to_canonical.values())
        ),
        "tiebreak_method": "opus_manual_in_conversation",
        "clusters": output_clusters,
        "building_to_canonical": bld_to_canonical,
        "tiebreak_decision_log": decisions_log,
    }

    with open(OUTPUT_PATH, "w") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    print(f"\n✓ saved → {OUTPUT_PATH}")
    print(f"  final_cluster_count: {len(output_clusters)}")
    print(f"  reduction: {len(mentions)} mentions → {len(output_clusters)} clusters")


if __name__ == "__main__":
    apply()
