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

# Subset auto-accept floor (handles 'BIG' ↔ 'Bjarke Ingels Group' acronym
# expansion — same logic as the existing matcher).
SUBSET_AUTO_FLOOR = 80.0


def _build_pool() -> list[dict]:
    pool = load_all()
    out: list[dict] = []
    for a in pool:
        core = _normalize_name(a["name"]) or a["name"].lower()
        if not core:
            continue
        a["name_core"]    = core
        a["country_norm"] = _normalize_country(a["country"]) if a["country"] else ""
        out.append(a)
    return out


def _classify_pair(a: dict, b: dict, sim: float) -> str:
    """Return one of: 'auto', 'tiebreak', 'drop'.

    Auto-accept rules (in priority order):
      1. Exact name_core match AND sim ≥ AUTO_ACCEPT_SIM       → 'auto'
      2. Subset match (BIG ↔ Bjarke Ingels Group) AND sim ≥ 80 → 'auto'
      3. sim ≥ AUTO_ACCEPT_SIM                                  → 'auto'
      4. sim ≥ STRONG_SIM AND country matches                   → 'auto'
      5. sim ≥ TIEBREAK_FLOOR                                   → 'tiebreak'
    """
    exact = (a["name_core"] == b["name_core"])
    if exact and sim >= AUTO_ACCEPT_SIM:
        return "auto"
    subset = (
        not exact
        and (_is_substring_match(a["name_core"], b["name_core"])
             or _is_substring_match(b["name_core"], a["name_core"]))
    )
    if subset and sim >= SUBSET_AUTO_FLOOR:
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
                sim = max(sort_s, set_s)
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
    pool = _build_pool()
    by_source: dict[str, int] = defaultdict(int)
    for a in pool:
        by_source[a["source"]] += 1
    print(f"  pool size: {len(pool)} "
          f"(metalocus={by_source['metalocus']}, divisare={by_source['divisare']}, "
          f"architizer={by_source['architizer']}, archello={by_source['archello']})",
          flush=True)

    print("scoring pairs (cdist row-chunked) ...", flush=True)
    auto_edges, tiebreak_pairs = _score_pairs(pool, chunk=args.chunk)
    print(f"  auto-accept edges:  {len(auto_edges)}", flush=True)
    print(f"  tiebreak pairs:     {len(tiebreak_pairs)}", flush=True)

    print("connected components on auto-accept edges only ...", flush=True)
    components = _connected_components(len(pool), auto_edges)
    multi = sum(1 for c in components if len({pool[m]["source"] for m in c}) >= 2)
    print(f"  components: {len(components)}  (multi-source: {multi})", flush=True)

    clusters = _build_clusters(pool, components)

    payload = {
        "summary": {
            "input_pool_size":      len(pool),
            "by_source":            dict(by_source),
            "canonical_clusters":   len(clusters),
            "multi_source_clusters": multi,
            "auto_accept_edges":    len(auto_edges),
            "tiebreak_pairs_pending": len(tiebreak_pairs),
            "thresholds": {
                "AUTO_ACCEPT_SIM":   AUTO_ACCEPT_SIM,
                "STRONG_SIM":        STRONG_SIM,
                "TIEBREAK_FLOOR":    TIEBREAK_FLOOR,
                "SUBSET_AUTO_FLOOR": SUBSET_AUTO_FLOOR,
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
         "name_a": pool[i]["name"], "source_a": pool[i]["source"],
         "name_b": pool[j]["name"], "source_b": pool[j]["source"],
         "sim": round(sim, 1), "country_match": cm}
        for i, j, sim, cm in tiebreak_pairs
    ]
    with open(args.tiebreak_output, "w") as f:
        json.dump(tb_payload, f, indent=2, ensure_ascii=False)
    print(f"✓ {args.tiebreak_output}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
