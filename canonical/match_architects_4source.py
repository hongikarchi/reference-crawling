"""Stage A: 4-source architect matching → canonical clusters.

Reads the 4-source architect pool (canonical._source_loaders.load_all)
and groups them into canonical clusters using auto-accept edges only.

Pre-existing `match_architects.py` (metalocus → Divisare 1-to-1) stays
as a regression baseline; this file extends to N sources.

Per advisor:
  • Use existing thresholds + helpers (no re-derivation).
  • cdist for full pool (chunked rows for memory safety).
  • Connected-components on AUTO-ACCEPT edges only — tiebreaker pairs
    become an ADVISORY queue, never graph edges.
  • Tiebreaker pairs → separate JSON; Haiku pass is gated/run later.

Outputs:
  data/canonical/architects_canonical.json
    {"summary": {...}, "clusters": [{canonical_arch_id, names_by_source,
                                     source_refs, countries, ...}]}
  data/canonical/architect_tiebreak_pairs.json
    [{i, j, name_a, name_b, source_a, source_b, sim, country_match}, ...]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict

from rapidfuzz import fuzz, process

from canonical._source_loaders import load_all
from canonical.match_architects import (
    _normalize_name, _normalize_country, _is_substring_match,
    AUTO_ACCEPT_SIM, STRONG_SIM, TIEBREAK_FLOOR,
)


OUTPUT_PATH        = "data/canonical/architects_canonical.json"
TIEBREAK_PAIRS_PATH = "data/canonical/architect_tiebreak_pairs.json"

# Source priority for picking representative name (lower = higher priority)
SOURCE_PRIORITY = {"architizer": 0, "divisare": 1, "archello": 2, "metalocus": 3}

# Subset auto-accept floor (REMOVED for 4-source — see _classify_pair docstring)
SUBSET_AUTO_FLOOR = 80.0

# Pool filter: cores below this length are too noisy to compare cross-source.
# They become single-source canonical clusters via a separate pass.
MIN_CORE_LEN = 3

# Component size cap: any cluster larger than this is suspect — re-route every
# member edge to the tiebreak queue and split into singletons. Real prolific
# firms (ARUP, Foster) have ~5-15 aliases across 4 sources, never 30+.
MAX_COMPONENT_SIZE = 30

# Generic-word cores that are noise even at length ≥ 3.
GENERIC_NOISE_CORES = {
    "and", "the", "co", "design", "studio", "studios", "architecture",
    "architects", "office", "group", "associates", "atelier", "team",
    "lab", "labs", "ltd", "inc", "llc", "corp", "company",
}


def _build_pool() -> tuple[list[dict], list[dict]]:
    """Returns (compare_pool, degenerate_pool).

    compare_pool: architects safe to compare cross-source (core ≥ MIN_CORE_LEN
                  and not in generic noise list).
    degenerate_pool: architects whose cores are too short or too generic to
                  participate in cross-source matching. They become single-source
                  canonical clusters in the output, never merged with anyone.
    """
    raw = load_all()
    compare: list[dict] = []
    degenerate: list[dict] = []
    for a in raw:
        core = _normalize_name(a["name"]) or a["name"].lower()
        if not core:
            degenerate.append(a)
            continue
        a["name_core"]    = core
        a["country_norm"] = _normalize_country(a["country"]) if a["country"] else ""
        if len(core) < MIN_CORE_LEN or core in GENERIC_NOISE_CORES:
            degenerate.append(a)
        else:
            compare.append(a)
    return compare, degenerate


def _classify_pair(a: dict, b: dict, sim: float) -> str:
    """Return one of: 'auto', 'tiebreak', 'drop'.

    Auto-accept rules (in priority order):
      1. Exact name_core match AND sim ≥ AUTO_ACCEPT_SIM       → 'auto'
      2. sim ≥ AUTO_ACCEPT_SIM                                  → 'auto'
      3. sim ≥ STRONG_SIM AND country matches                   → 'auto'
      4. sim ≥ TIEBREAK_FLOOR                                   → 'tiebreak'

    Note: subset auto-rule (BIG ↔ Bjarke Ingels Group at sim≥80) was REMOVED
    after a false-merge incident where every short-acronym core (ARUP, AEDAS,
    HENN…) subset-matched every long core that contained or shared tokens
    with it, producing one giant 2,902-member cluster. The 2-source matcher's
    subset auto-rule worked because it had a top-K candidate prefilter; in
    all-pairs cdist there is no such filter, so subset cases are demoted to
    the tiebreak queue and resolved by Haiku later.
    """
    exact = (a["name_core"] == b["name_core"])
    if exact and sim >= AUTO_ACCEPT_SIM:
        return "auto"
    if sim >= AUTO_ACCEPT_SIM:
        return "auto"
    country_match = bool(a["country_norm"]) and a["country_norm"] == b["country_norm"]
    if sim >= STRONG_SIM and country_match:
        return "auto"
    if sim >= TIEBREAK_FLOOR:
        return "tiebreak"
    return "drop"


def _score_pairs(pool: list[dict], chunk: int = 200) -> tuple[list, list]:
    """cdist row-chunked: for each chunk of query rows vs full pool, take the
    max(token_sort_ratio, token_set_ratio) per pair, classify, and bucket.

    Returns (auto_edges, tiebreak_pairs):
      auto_edges:    [(i, j, sim, kind), ...]
      tiebreak_pairs:[(i, j, sim, country_match), ...]
    """
    cores = [a["name_core"] for a in pool]
    n = len(cores)
    auto_edges: list = []
    tiebreak_pairs: list = []
    for start in range(0, n, chunk):
        end = min(start + chunk, n)
        queries = cores[start:end]
        sort_m = process.cdist(queries, cores, scorer=fuzz.token_sort_ratio,
                               score_cutoff=TIEBREAK_FLOOR)
        set_m  = process.cdist(queries, cores, scorer=fuzz.token_set_ratio,
                               score_cutoff=TIEBREAK_FLOOR)
        for ki, i in enumerate(range(start, end)):
            for j in range(i + 1, n):
                sort_s = float(sort_m[ki][j])
                set_s  = float(set_m[ki][j])
                # min — both metrics must agree. Using max would let
                # token_set_ratio's subset behavior ('arup' ↔ 'arup engineering'
                # = 100) chain unrelated firms via shared tokens.
                sim = min(sort_s, set_s)
                if sim < TIEBREAK_FLOOR:
                    continue
                a, b = pool[i], pool[j]
                if a["source"] == b["source"]:
                    continue  # within-source dedup is a separate concern
                kind = _classify_pair(a, b, sim)
                if kind == "auto":
                    auto_edges.append((i, j, sim))
                elif kind == "tiebreak":
                    cm = bool(a["country_norm"]) and a["country_norm"] == b["country_norm"]
                    tiebreak_pairs.append((i, j, sim, cm))
        if (start // chunk) % 10 == 0:
            print(f"  chunk {start}-{end} / {n}: "
                  f"auto={len(auto_edges)} tiebreak={len(tiebreak_pairs)}",
                  flush=True)
    return auto_edges, tiebreak_pairs


def _connected_components(n: int, edges: list) -> list[list[int]]:
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    for edge in edges:
        union(edge[0], edge[1])
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)
    return list(groups.values())


def _split_oversize_components(
    components: list[list[int]],
    auto_edges: list,
    tiebreak_pairs: list,
    pool: list[dict],
) -> tuple[list[list[int]], list, list]:
    """Cap component size: any component > MAX_COMPONENT_SIZE is suspect
    (real prolific firms cluster at <30 across 4 sources). Move every
    member-edge of an oversize component to the tiebreak queue and split
    the component into singletons.
    """
    member_to_comp: dict[int, int] = {}
    for ci, members in enumerate(components):
        for m in members:
            member_to_comp[m] = ci
    oversize_ci = {ci for ci, members in enumerate(components)
                   if len(members) > MAX_COMPONENT_SIZE}
    if not oversize_ci:
        return components, auto_edges, tiebreak_pairs

    # Re-route edges that touch oversize components → tiebreak
    new_auto: list = []
    new_tb = list(tiebreak_pairs)
    for i, j, sim in auto_edges:
        if member_to_comp[i] in oversize_ci or member_to_comp[j] in oversize_ci:
            a, b = pool[i], pool[j]
            cm = bool(a["country_norm"]) and a["country_norm"] == b["country_norm"]
            new_tb.append((i, j, sim, cm))
        else:
            new_auto.append((i, j, sim))

    # Singletons replace oversize components
    new_comps: list[list[int]] = []
    for ci, members in enumerate(components):
        if ci in oversize_ci:
            new_comps.extend([[m] for m in members])
        else:
            new_comps.append(members)
    print(f"  oversize-cap: split {len(oversize_ci)} components "
          f"({sum(len(components[ci]) for ci in oversize_ci)} members → singletons)",
          flush=True)
    print(f"    edges re-routed to tiebreak: {len(auto_edges) - len(new_auto)}",
          flush=True)
    return new_comps, new_auto, new_tb


def _build_clusters(pool: list[dict], components: list[list[int]]) -> list[dict]:
    out: list[dict] = []
    for cid, members in enumerate(components):
        objs = [pool[m] for m in members]
        # Representative = highest project_count, then source priority
        objs.sort(key=lambda a: (
            -(a.get("project_count") or 0),
            SOURCE_PRIORITY.get(a["source"], 99),
        ))
        rep = objs[0]
        # source_refs: latest wins per source (multiple metalocus IDs collapse)
        source_refs: dict[str, list] = defaultdict(list)
        for a in objs:
            source_refs[a["source"]].append(a["source_id"])
        # Aliases = all unique names across the cluster
        aliases = sorted({a["name"] for a in objs})
        countries = sorted({a["country"] for a in objs if a["country"]})
        out.append({
            "canonical_arch_id":   f"arch_{cid:06d}",
            "canonical_name":      rep["name"],
            "names_by_source":     {a["source"]: a["name"] for a in objs},
            "source_refs":         dict(source_refs),
            "aliases":             aliases,
            "countries":           countries,
            "n_sources":           len(source_refs),
            "n_members":           len(objs),
            "project_count_total": sum((a.get("project_count") or 0) for a in objs),
        })
    out.sort(key=lambda x: -x["project_count_total"])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--output", default=OUTPUT_PATH)
    ap.add_argument("--tiebreak-output", default=TIEBREAK_PAIRS_PATH)
    ap.add_argument("--chunk", type=int, default=200)
    args = ap.parse_args()

    print("loading pool ...", flush=True)
    compare_pool, degenerate_pool = _build_pool()
    print(f"  compare pool: {len(compare_pool)}  (cross-source matched)", flush=True)
    print(f"  degenerate pool: {len(degenerate_pool)}  (single-source only — "
          f"core too short or generic)", flush=True)
    by_source: dict[str, int] = defaultdict(int)
    for a in compare_pool:
        by_source[a["source"]] += 1
    print(f"  compare by source: "
          f"metalocus={by_source['metalocus']}, divisare={by_source['divisare']}, "
          f"architizer={by_source['architizer']}, archello={by_source['archello']}",
          flush=True)

    print("scoring pairs (cdist row-chunked) ...", flush=True)
    auto_edges, tiebreak_pairs = _score_pairs(compare_pool, chunk=args.chunk)
    print(f"  auto-accept edges:  {len(auto_edges)}", flush=True)
    print(f"  tiebreak pairs:     {len(tiebreak_pairs)}", flush=True)

    print("connected components on auto-accept edges only ...", flush=True)
    components = _connected_components(len(compare_pool), auto_edges)
    multi = sum(1 for c in components if len({compare_pool[m]["source"] for m in c}) >= 2)
    print(f"  components: {len(components)}  (multi-source: {multi})", flush=True)
    # Note: oversize-cap removed once sim metric was fixed (min(sort,set) instead
    # of max). With sort+set both required, false-merges no longer reach 30+.

    clusters = _build_clusters(compare_pool, components)
    # Append degenerate architects as single-source canonical clusters
    next_cid = len(clusters)
    for a in degenerate_pool:
        clusters.append({
            "canonical_arch_id":   f"arch_{next_cid:06d}",
            "canonical_name":      a["name"],
            "names_by_source":     {a["source"]: a["name"]},
            "source_refs":         {a["source"]: [a["source_id"]]},
            "aliases":             [a["name"]],
            "countries":           [a["country"]] if a["country"] else [],
            "n_sources":           1,
            "n_members":           1,
            "project_count_total": a.get("project_count") or 0,
            "degenerate_core":     True,
        })
        next_cid += 1
    clusters.sort(key=lambda x: -x["project_count_total"])

    payload = {
        "summary": {
            "input_pool_total":     len(compare_pool) + len(degenerate_pool),
            "compare_pool_size":    len(compare_pool),
            "degenerate_pool_size": len(degenerate_pool),
            "by_source":            dict(by_source),
            "canonical_clusters":   len(clusters),
            "multi_source_clusters": multi,
            "auto_accept_edges":    len(auto_edges),
            "tiebreak_pairs_pending": len(tiebreak_pairs),
            "thresholds": {
                "AUTO_ACCEPT_SIM":   AUTO_ACCEPT_SIM,
                "STRONG_SIM":        STRONG_SIM,
                "TIEBREAK_FLOOR":    TIEBREAK_FLOOR,
                "MIN_CORE_LEN":      MIN_CORE_LEN,
                "MAX_COMPONENT_SIZE": MAX_COMPONENT_SIZE,
            },
        },
        "clusters": clusters,
    }

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(f"\n✓ {args.output}", flush=True)

    tb_payload = [
        {"i": i, "j": j,
         "name_a": compare_pool[i]["name"], "source_a": compare_pool[i]["source"],
         "name_b": compare_pool[j]["name"], "source_b": compare_pool[j]["source"],
         "sim": round(sim, 1), "country_match": cm}
        for i, j, sim, cm in tiebreak_pairs
    ]
    with open(args.tiebreak_output, "w") as f:
        json.dump(tb_payload, f, indent=2, ensure_ascii=False)
    print(f"✓ {args.tiebreak_output}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
