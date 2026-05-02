"""Plan B (Stage A): 6 pair-wise matchers → union → canonical clusters.

Architecture (per advisor recommendation after iter4 regression):
  • Each pair matcher uses the proven 2-source thresholds (95/85/78) +
    helpers from match_architects.py. Verdicts on metalocus×divisare
    reproduce the existing 1,489 baseline 1:1.
  • 6 pairs (4 sources × 3/2): metalocus×div, metalocus×arch_z,
    metalocus×arch_o, div×arch_z, div×arch_o, arch_z×arch_o.
  • auto_accept verdicts → undirected edges → connected components →
    canonical clusters (one per component).
  • needs_tiebreak verdicts → separate Haiku queue, processed later
    after user cost confirmation.

Inputs (loaded via _source_loaders + metaloc cluster file):
  data/canonical/metalocus_architect_clusters.json
  data/crawl/{divisare,architizer,archello}.db

Outputs:
  data/canonical/match/pair_{a}_{b}.json   — 6 per-pair verdict tables
  data/canonical/architects_canonical.json — final clusters
  data/canonical/architect_tiebreak_pairs.json — Haiku queue
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from typing import Optional

from canonical._source_loaders import (
    METALOCUS_CLUSTERS, load_divisare, load_architizer, load_archello,
)
from canonical.match_pair import match_pair, normalize_for_match


PAIR_OUTPUT_DIR = "data/canonical/match"
CANONICAL_OUTPUT = "data/canonical/architects_canonical.json"
TIEBREAK_OUTPUT  = "data/canonical/architect_tiebreak_pairs.json"

SOURCE_PRIORITY = {"architizer": 0, "divisare": 1, "archello": 2, "metalocus": 3}


def _load_metalocus_clusters() -> list[dict]:
    with open(METALOCUS_CLUSTERS) as f:
        data = json.load(f)
    return [c for c in data["clusters"] if not c.get("is_noise")]


def load_normalized() -> dict[str, list[dict]]:
    """Load all 4 sources and convert to uniform match-item shape."""
    print("loading 4 sources ...", flush=True)
    metaloc_raw = _load_metalocus_clusters()
    out = {
        "metalocus":  [normalize_for_match(c, "metalocus")  for c in metaloc_raw],
        "divisare":   [normalize_for_match(d, "divisare")   for d in load_divisare()],
        "architizer": [normalize_for_match(d, "architizer") for d in load_architizer()],
        "archello":   [normalize_for_match(d, "archello")   for d in load_archello()],
    }
    for src, items in out.items():
        print(f"  {src}: {len(items)}", flush=True)
    return out


def run_six_pair_matchers(pools: dict[str, list[dict]]) -> dict[tuple[str, str], dict]:
    """Run all 6 pair matchers. Returns {(label_a, label_b): verdict_payload}."""
    pair_results: dict[tuple[str, str], dict] = {}
    pairs = [
        ("metalocus",  "divisare"),
        ("metalocus",  "architizer"),
        ("metalocus",  "archello"),
        ("divisare",   "architizer"),
        ("divisare",   "archello"),
        ("architizer", "archello"),
    ]
    os.makedirs(PAIR_OUTPUT_DIR, exist_ok=True)
    for a, b in pairs:
        print(f"\n=== matching {a} × {b} ===", flush=True)
        out_path = os.path.join(PAIR_OUTPUT_DIR, f"pair_{a}_{b}.json")
        payload = match_pair(
            pools[a], pools[b],
            label_a=a, label_b=b,
            output_path=out_path,
        )
        pair_results[(a, b)] = payload
    return pair_results


def build_canonical_clusters(
    pools: dict[str, list[dict]],
    pair_results: dict[tuple[str, str], dict],
) -> tuple[list[dict], list[dict]]:
    """Union auto-accept edges across all 6 pairs → connected components.
    Returns (clusters, tiebreak_pairs).
    """
    # Global node index: source_label → {item_id → global_idx}
    item_index: dict[str, dict[str, int]] = {src: {} for src in pools}
    nodes: list[dict] = []
    for src, items in pools.items():
        for item in items:
            item_index[src][item["id"]] = len(nodes)
            nodes.append({**item, "_global_idx": len(nodes)})

    # Collect auto-accept edges and tiebreak pairs across all 6 pairs
    auto_edges: list[tuple[int, int, float, str]] = []
    tiebreak_pairs: list[dict] = []
    for (a, b), payload in pair_results.items():
        for m in payload["matches"]:
            ai = item_index[a].get(m[f"{a}_id"])
            verdict = m["verdict"]
            if verdict in ("auto_accept", "accept_with_country"):
                bi = item_index[b].get(m[f"{b}_id"])
                if ai is None or bi is None:
                    continue
                auto_edges.append((ai, bi, m["name_sim"], verdict))
            elif verdict == "needs_tiebreak":
                top = m.get("top_candidate")
                if not top:
                    continue
                bi = item_index[b].get(str(top["id"]))
                if bi is None:
                    continue
                tiebreak_pairs.append({
                    "a_idx": ai, "b_idx": bi,
                    "source_a": a, "name_a": m[f"{a}_name"],
                    "source_b": b, "name_b": top["name"],
                    "sim": m["name_sim"],
                    "country_match": m["country_match"],
                })

    print(f"\nAuto-accept edges (6 pairs combined): {len(auto_edges)}",
          flush=True)
    print(f"Tiebreak pairs (advisory queue):       {len(tiebreak_pairs)}",
          flush=True)

    # Connected components on auto-accept edges only
    parent = list(range(len(nodes)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    for ai, bi, _sim, _verdict in auto_edges:
        union(ai, bi)

    components: dict[int, list[int]] = defaultdict(list)
    for i in range(len(nodes)):
        components[find(i)].append(i)
    print(f"Connected components: {len(components)}", flush=True)

    multi_source = sum(1 for members in components.values()
                       if len({nodes[m]["source"] for m in members}) >= 2)
    print(f"Multi-source clusters: {multi_source}", flush=True)

    # Build canonical clusters
    clusters: list[dict] = []
    for cid, members in enumerate(components.values()):
        member_objs = [nodes[m] for m in members]
        # Representative = highest project_count, then source priority
        member_objs.sort(key=lambda a: (
            -(a.get("project_count") or 0),
            SOURCE_PRIORITY.get(a["source"], 99),
        ))
        rep = member_objs[0]
        source_refs: dict[str, list] = defaultdict(list)
        for a in member_objs:
            source_refs[a["source"]].append(a["id"])
        countries = sorted({c for a in member_objs for c in (a.get("countries") or []) if c})
        clusters.append({
            "canonical_arch_id":   f"arch_{cid:06d}",
            "canonical_name":      rep["name"],
            "names_by_source":     {a["source"]: a["name"] for a in member_objs},
            "source_refs":         dict(source_refs),
            "aliases":             sorted({a["name"] for a in member_objs}),
            "countries":           countries,
            "n_sources":           len(source_refs),
            "n_members":           len(member_objs),
            "project_count_total": sum((a.get("project_count") or 0)
                                       for a in member_objs),
        })
    clusters.sort(key=lambda c: -c["project_count_total"])
    return clusters, tiebreak_pairs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--canonical-output", default=CANONICAL_OUTPUT)
    ap.add_argument("--tiebreak-output",  default=TIEBREAK_OUTPUT)
    args = ap.parse_args()

    pools = load_normalized()
    pair_results = run_six_pair_matchers(pools)
    clusters, tiebreak = build_canonical_clusters(pools, pair_results)

    # Aggregate verdict tallies across all 6 pairs
    pair_summary = {f"{a}×{b}": p["summary"]["verdict_counts"]
                    for (a, b), p in pair_results.items()}

    summary = {
        "input_pool_sizes":      {src: len(items) for src, items in pools.items()},
        "input_pool_total":      sum(len(v) for v in pools.values()),
        "canonical_clusters":    len(clusters),
        "multi_source_clusters": sum(1 for c in clusters if c["n_sources"] >= 2),
        "tiebreak_pairs":        len(tiebreak),
        "per_pair_verdicts":     pair_summary,
    }
    payload = {"summary": summary, "clusters": clusters}

    os.makedirs(os.path.dirname(args.canonical_output), exist_ok=True)
    with open(args.canonical_output, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(f"\n✓ {args.canonical_output}", flush=True)

    with open(args.tiebreak_output, "w") as f:
        json.dump(tiebreak, f, indent=2, ensure_ascii=False)
    print(f"✓ {args.tiebreak_output}", flush=True)

    print("\nNext step: Haiku tiebreaker pass on the {} queued pairs "
          "(canonical/match_tiebreaker.py — gated on user cost approval)."
          .format(len(tiebreak)), flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
