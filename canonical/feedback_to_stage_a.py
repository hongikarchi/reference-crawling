"""Hybrid corrective loop: building matching → architect merge evidence.

For each canonical_building with members from ≥2 sources, look at the
canonical_arch_ids contributed by each member's source. If those arch IDs
DIFFER across sources, that's evidence Stage A failed to merge those
architects (e.g., divisare's "Foster + Partners" canonical_arch_X and
architizer's "Foster Partners" canonical_arch_Y — building cross-match
proves they're the same firm).

Output:
  data/canonical/architect_merge_evidence.json   list of merge proposals
                                                  with confidence scores
  Optionally applied directly via --apply: redirect Y → X in the architect
  registry and re-export architects_canonical.json.

Single iteration only — re-running Stage B after this catches the
newly-aligned architects in pre-filter groups.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import sqlite3
from collections import Counter, defaultdict
from typing import Optional

from canonical.registry import ArchitectRegistry, BuildingRegistry


BUILDINGS_REGISTRY  = "data/id_registry_buildings.json"
ARCHITECTS_REGISTRY = "data/id_registry_architects.json"
EVIDENCE_OUT        = "data/canonical/architect_merge_evidence.json"
DIVISARE_DB         = "data/crawl/divisare.db"
ARCHITIZER_DB       = "data/crawl/architizer.db"
ARCHELLO_DB         = "data/crawl/archello.db"
METALOCUS_FINAL     = "data/enrich/4_buildings_final.json"
METALOCUS_CLUSTERS  = "data/canonical/metalocus_architect_clusters.json"

# Confidence: at least N independent buildings must agree before we merge
MIN_BUILDING_EVIDENCE = 2

# Name similarity gate (rapidfuzz strict_sim min(token_sort, token_set))
# True architect dup will share name tokens; collab/supplier pairs won't.
# Stage A's degenerate-core filter handles common words like "studio".
MIN_NAME_SIM = 75.0


def _build_arch_lookup(arch_reg: ArchitectRegistry) -> dict[tuple, str]:
    idx: dict[tuple, str] = {}
    for cid, e in arch_reg.data.items():
        if e.get("redirected_to"):
            continue
        for src, ids in e.get("source_refs", {}).items():
            for sid in ids:
                idx[(src, str(sid))] = cid
    return idx


def _load_building_arch_ids() -> dict[tuple, list[str]]:
    """Returns {(source, building_source_id): [native_arch_id, ...]} across
    all 4 sources. Used to back-trace each building member to its native
    architect IDs (then mapped through arch registry to canonical_arch_id)."""
    out: dict[tuple, list[str]] = {}

    # Divisare
    conn = sqlite3.connect(DIVISARE_DB)
    for bid, raw in conn.execute(
        "SELECT id, architect_ids FROM divisare_projects WHERE architect_ids IS NOT NULL"
    ):
        try:
            ids = json.loads(raw) if raw else []
        except (json.JSONDecodeError, TypeError):
            ids = []
        out[("divisare", str(bid))] = [str(a) for a in ids]
    conn.close()

    # Architizer (firm_slug, single)
    conn = sqlite3.connect(ARCHITIZER_DB)
    for bid, slug in conn.execute(
        "SELECT id, firm_slug FROM architizer_projects WHERE firm_slug IS NOT NULL"
    ):
        out[("architizer", str(bid))] = [slug]
    conn.close()

    # Archello (architect_brand_id, single)
    conn = sqlite3.connect(ARCHELLO_DB)
    for bid, aid in conn.execute(
        "SELECT id, architect_brand_id FROM archello_projects WHERE architect_brand_id IS NOT NULL"
    ):
        out[("archello", str(bid))] = [str(aid)]
    conn.close()

    # Metalocus (via cluster file)
    final = json.load(open(METALOCUS_FINAL))
    final_ids = {b["building_id"] for b in final}
    bld_to_arch = json.load(open(METALOCUS_CLUSTERS)).get("building_to_canonical", {})
    for bid, arch_ids in bld_to_arch.items():
        if bid in final_ids:
            out[("metalocus", str(bid))] = list(arch_ids)

    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Apply merge proposals (redirect arch canonicals + re-export)")
    args = ap.parse_args()

    print("loading registries …", flush=True)
    bld_reg  = BuildingRegistry(BUILDINGS_REGISTRY)
    arch_reg = ArchitectRegistry(ARCHITECTS_REGISTRY)
    arch_idx = _build_arch_lookup(arch_reg)
    print(f"  building canonicals : {bld_reg.stats()['active']:>6}", flush=True)
    print(f"  architect canonicals: {arch_reg.stats()['active']:>6}", flush=True)

    print("\nloading building → native_arch_id index …", flush=True)
    bld_arch_native = _load_building_arch_ids()
    print(f"  {len(bld_arch_native)} buildings indexed", flush=True)

    print("\nscanning building canonicals for arch divergence …", flush=True)
    # For each multi-source building canonical, collect the canonical_arch_ids
    # contributed by EACH (source, source_id) member
    pair_evidence: dict[tuple, int] = Counter()  # (arch_X, arch_Y) → count
    pair_examples: dict[tuple, list] = defaultdict(list)
    n_multi = 0

    for cid, e in bld_reg.data.items():
        if e.get("redirected_to"):
            continue
        srcs = e.get("source_refs", {})
        if len(srcs) < 2:
            continue
        n_multi += 1
        # Gather all canonical_arch_ids per member
        per_member_arch: list[set[str]] = []
        for src, ids in srcs.items():
            for sid in ids:
                native_arch_ids = bld_arch_native.get((src, str(sid)), [])
                cas = {arch_idx[(src, a)] for a in native_arch_ids
                       if (src, a) in arch_idx}
                if cas:
                    per_member_arch.append(cas)
        if len(per_member_arch) < 2:
            continue
        # Find pairs (X, Y) where X in member1 but not member2 etc.
        # Simpler: pairwise across members — if their arch sets are disjoint,
        # it's evidence X and Y should merge. Use representative ID from each.
        for i in range(len(per_member_arch)):
            for j in range(i + 1, len(per_member_arch)):
                a, b = per_member_arch[i], per_member_arch[j]
                if not (a & b):  # disjoint
                    # one representative pair per disjoint set crossing
                    for x in sorted(a):
                        for y in sorted(b):
                            key = tuple(sorted((x, y)))
                            pair_evidence[key] += 1
                            if len(pair_examples[key]) < 5:
                                bld_name = e.get("names", [""])[0]
                                pair_examples[key].append({
                                    "bld_cid": cid, "bld_name": bld_name,
                                })

    print(f"  multi-source buildings scanned: {n_multi}", flush=True)
    print(f"  candidate arch pairs (with disjoint evidence): {len(pair_evidence)}",
          flush=True)

    # Filter to high-confidence: ≥ MIN_BUILDING_EVIDENCE buildings agree
    # AND name similarity ≥ MIN_NAME_SIM (excludes collab/supplier false-merges)
    from rapidfuzz import fuzz
    proposals = []
    rejected_low_sim = 0
    for pair in pair_evidence:
        if pair_evidence[pair] < MIN_BUILDING_EVIDENCE:
            continue
        name_a = arch_reg.data.get(pair[0], {}).get("names", [""])[0] if pair[0] in arch_reg.data else ""
        name_b = arch_reg.data.get(pair[1], {}).get("names", [""])[0] if pair[1] in arch_reg.data else ""
        if name_a and name_b:
            s = fuzz.token_sort_ratio(name_a.lower(), name_b.lower())
            t = fuzz.token_set_ratio(name_a.lower(), name_b.lower())
            sim = min(s, t)
        else:
            sim = 0
        if sim < MIN_NAME_SIM:
            rejected_low_sim += 1
            continue
        proposals.append({
            "arch_a":   pair[0],
            "arch_b":   pair[1],
            "name_a":   name_a,
            "name_b":   name_b,
            "name_sim": round(sim, 1),
            "evidence": pair_evidence[pair],
            "examples": pair_examples[pair][:3],
        })
    proposals.sort(key=lambda p: -p["evidence"])
    print(f"  rejected for low name_sim (<{MIN_NAME_SIM}): {rejected_low_sim}",
          flush=True)

    print(f"\nhigh-confidence merge proposals (≥ {MIN_BUILDING_EVIDENCE} bld evidence): "
          f"{len(proposals)}", flush=True)
    print(f"\nTop-15 proposals:")
    for p in proposals[:15]:
        print(f"  ev={p['evidence']:>3}  {p['arch_a']} ({p['name_a'][:30]:<30}) "
              f"↔ {p['arch_b']} ({p['name_b'][:30]:<30})")

    os.makedirs(os.path.dirname(EVIDENCE_OUT), exist_ok=True)
    with open(EVIDENCE_OUT, "w") as f:
        json.dump({
            "summary": {
                "n_proposals":           len(proposals),
                "min_evidence_required": MIN_BUILDING_EVIDENCE,
                "total_disjoint_pairs":  len(pair_evidence),
                "multi_source_buildings_scanned": n_multi,
            },
            "proposals": proposals,
        }, f, indent=2, ensure_ascii=False)
    print(f"\n✓ evidence saved → {EVIDENCE_OUT}", flush=True)

    if args.apply and proposals:
        print(f"\napplying {len(proposals)} merges to architect registry …",
              flush=True)
        # Merge B into A (lexically smaller cid wins, stable)
        merged = 0
        for p in proposals:
            a, b = p["arch_a"], p["arch_b"]
            # Resolve current targets (in case prior merges rerouted)
            a_t = arch_reg.follow(a)
            b_t = arch_reg.follow(b)
            if a_t == b_t:
                continue
            # B → A
            entry_b = arch_reg.data[b_t]
            arch_reg.append(a_t,
                            names=set(entry_b.get("names", [])),
                            source_refs=entry_b.get("source_refs", {}))
            entry_b["redirected_to"] = a_t
            merged += 1
        arch_reg.save()
        print(f"✓ {merged} merges applied; arch registry: {arch_reg.stats()}",
              flush=True)
        # Re-export architects canonical
        from canonical.match_architects_sequential import export_clusters as exp_arch
        exp_arch(arch_reg, "data/canonical/architects_canonical.json")
        print(f"✓ architects canonical re-exported", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
