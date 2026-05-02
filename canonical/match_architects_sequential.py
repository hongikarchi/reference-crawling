"""Stage A — Sequential 4-phase architect matching with id_registry.

Algorithm (per user direction 2026-05-02):
  Phase 1 (Divisare): 12,763 → registry 1:1 (within-source-merge X — divisare
                      is treated as authoritative unique base).
  Phase 2 (Architizer): each → best-1 match against registry
                                - match → append source_ref
                                - no match → new canonical (orphan)
  Phase 3 (Archello):  each → best-1 match against registry (now grown)
                                - match → append
                                - no match → new canonical (orphan)
  Phase 4 (Metalocus): each cluster → best-1 match against registry
                                - match → append
                                - **no match → DROP** (don't create new — metalocus
                                  has no per-architect page so 'BIG' / 'Bjarke
                                  Ingels' / 'B.I.G' may exist as separate clusters
                                  and shouldn't pollute the canonical with duplicates)

False-merge guards:
  • within-source merge never happens (each source is 1:1 in its own scope)
  • cross-source matching picks BEST 1 candidate only (no fan-out → no
    transitive chains)
  • ambiguous mid-band (TIEBREAK_FLOOR ≤ sim < AUTO_ACCEPT_SIM) → tiebreak
    queue (Haiku-resolved later, gated on user cost approval)

Outputs:
  data/id_registry_architects.json     — stable canonical_arch_id ↔ entries
  data/canonical/architects_canonical.json — same data in cluster form for QC
  data/canonical/architect_tiebreak_pairs.json — Haiku queue
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from typing import Optional

from rapidfuzz import fuzz, process

from canonical._source_loaders import (
    METALOCUS_CLUSTERS, load_divisare, load_architizer, load_archello,
)
from canonical.match_architects import (
    _normalize_name, _normalize_country, _is_substring_match,
    _verdict_for, AUTO_ACCEPT_SIM, STRONG_SIM, TIEBREAK_FLOOR,
    MAX_CANDIDATES_PER_CLUSTER,
)
from canonical.registry import ArchitectRegistry


CANONICAL_OUTPUT  = "data/canonical/architects_canonical.json"
TIEBREAK_OUTPUT   = "data/canonical/architect_tiebreak_pairs.json"

# Per advisor (after sequential v1 found 'landscape' hub false-merge):
# Generic words that survive _normalize_name and become 1-token hubs.
# Items whose name_core is in this set or shorter than 3 chars are excluded
# from cross-source matching (treated as degenerate-noise singletons).
MIN_CORE_LEN = 3
GENERIC_NOISE_CORES = {
    "and", "the", "co", "design", "studio", "studios", "architecture",
    "architects", "office", "group", "associates", "atelier", "team",
    "lab", "labs", "ltd", "inc", "llc", "corp", "company",
    "landscape", "interior", "interiors", "engineering", "planning",
    "consultants", "consulting", "partners", "partnership", "works",
    "international", "global",
}


def _is_degenerate_core(core: str) -> bool:
    return (not core) or len(core) < MIN_CORE_LEN or core in GENERIC_NOISE_CORES


# ---------------------------------------------------------------------------
# Source loaders → uniform match-item shape
# ---------------------------------------------------------------------------

def _to_item(raw: dict, source: str) -> dict:
    """Convert raw loader dict (or metaloc cluster) → uniform shape."""
    if source == "metalocus":
        name = raw["canonical_name"]
        countries = sorted({_normalize_country(c) for c in (raw.get("countries") or []) if c})
        return {
            "id":            raw["canonical_id"],
            "name":          name,
            "name_core":     _normalize_name(name) or name.lower(),
            "country":       None,
            "country_norm":  None,
            "countries":     countries,
            "project_count": raw.get("building_count") or 0,
            "source":        "metalocus",
        }
    name = raw["name"]
    country = raw.get("country")
    country_norm = _normalize_country(country) if country else None
    return {
        "id":            str(raw["source_id"]),
        "name":          name,
        "name_core":     _normalize_name(name) or name.lower(),
        "country":       country,
        "country_norm":  country_norm,
        "countries":     [country_norm] if country_norm else [],
        "project_count": raw.get("project_count") or 0,
        "source":        source,
    }


def _load_metalocus_clusters() -> list[dict]:
    with open(METALOCUS_CLUSTERS) as f:
        data = json.load(f)
    return [c for c in data["clusters"] if not c.get("is_noise")]


# ---------------------------------------------------------------------------
# Best-1 match against the current registry
# ---------------------------------------------------------------------------

def _registry_to_pool(registry: ArchitectRegistry) -> list[dict]:
    """Flatten the registry's active canonicals into a list of match-items.

    Each canonical entry expands to ONE pool item per (name, source) so
    matching against any of its names / source_refs works. The match-item's
    canonical_id field tracks back to the registry entry.

    Degenerate-core names (e.g. 'landscape', 'studio') are EXCLUDED from
    the pool so they cannot become hubs that swallow unrelated firms.
    The canonical entry itself stays in the registry — it just isn't a
    valid merge target by name alone.
    """
    pool: list[dict] = []
    for cid, entry in registry.data.items():
        if entry.get("redirected_to"):
            continue
        for name in entry.get("names", []):
            core = _normalize_name(name) or name.lower()
            if _is_degenerate_core(core):
                continue
            pool.append({
                "id":               cid,         # canonical_id (the merge target)
                "name":             name,
                "name_core":        core,
                "country":          None,
                "country_norm":     None,
                "countries":        [],
                "project_count":    sum(len(ids) for ids in entry.get("source_refs", {}).values()),
                "source":           "_registry",
                "source_refs":      entry.get("source_refs", {}),
            })
    return pool


def _best_match(
    item: dict, pool_cores: list[str], pool: list[dict],
) -> Optional[tuple[dict, float, float, float]]:
    """Top-1 candidate from pool.

    Returns (best_item, strict_sim, loose_sim, second_loose_sim) or None.

    Two scores per pair:
      • strict_sim = min(token_sort_ratio, token_set_ratio) — used as the
        auto-accept gate. The min() rejects subset coincidences like
        token_set_ratio('arup', 'arup engineering') = 100.
      • loose_sim  = max(token_sort_ratio, token_set_ratio) — used as the
        tiebreak floor and for ranking candidates so plausible but
        ambiguous matches still get queued for Haiku review.
    """
    if not pool_cores or not item["name_core"]:
        return None
    sort_scores = process.cdist([item["name_core"]], pool_cores,
                                scorer=fuzz.token_sort_ratio)[0]
    set_scores  = process.cdist([item["name_core"]], pool_cores,
                                scorer=fuzz.token_set_ratio)[0]
    rows: list[tuple[int, float, float]] = []
    for i in range(len(pool_cores)):
        s = float(sort_scores[i]); t = float(set_scores[i])
        rows.append((i, min(s, t), max(s, t)))
    rows.sort(key=lambda r: -r[2])  # rank by loose_sim
    if not rows or rows[0][2] < TIEBREAK_FLOOR:
        return None
    top_idx, strict_sim, loose_sim = rows[0]
    second_loose_sim = rows[1][2] if len(rows) > 1 else 0.0
    return pool[top_idx], strict_sim, loose_sim, second_loose_sim


def _classify(
    item: dict, top_match: dict,
    strict_sim: float, loose_sim: float, second_loose_sim: float,
) -> str:
    """Verdict tiers, with strict/loose split per advisor.

    auto_accept           ← strict_sim ≥ AUTO_ACCEPT_SIM (≥95 on min(sort,set))
    accept_with_country   ← strict_sim ≥ STRONG_SIM AND country matches
    needs_tiebreak        ← loose_sim ≥ TIEBREAK_FLOOR (max(sort,set) covers
                            plausible-but-ambiguous middle band)
    no_match              ← otherwise

    Subset core-pairs (e.g. 'arup' inside 'arup engineering') no longer
    auto-accept because strict_sim drops below threshold; they fall to
    tiebreak instead.
    """
    a_core = item["name_core"]
    b_core = top_match["name_core"]
    exact_core = (a_core != "" and a_core == b_core)
    country_match = bool(item.get("countries")) and any(
        c == top_match.get("country_norm") for c in item.get("countries") or []
    )
    # Exact-core auto-accept short-circuit (handles trivial case-only diffs)
    if exact_core:
        return "auto_accept"
    if strict_sim >= AUTO_ACCEPT_SIM:
        return "auto_accept"
    if strict_sim >= STRONG_SIM and country_match:
        return "accept_with_country"
    if loose_sim >= TIEBREAK_FLOOR:
        return "needs_tiebreak"
    return "no_match"


# ---------------------------------------------------------------------------
# Phase orchestration
# ---------------------------------------------------------------------------

def phase_1_divisare(registry: ArchitectRegistry) -> int:
    """Each divisare architect → unique canonical (1:1, no within-merge)."""
    n = 0
    for raw in load_divisare():
        item = _to_item(raw, "divisare")
        cid, status = registry.match_or_create(
            names={item["name"]},
            source_refs={"divisare": [item["id"]]},
        )
        n += 1
        if n % 2000 == 0:
            print(f"  phase 1 progress: {n}", flush=True)
    return n


def phase_match_against_registry(
    registry: ArchitectRegistry,
    new_items: list[dict],
    *,
    label: str,
    allow_new: bool = True,
    tiebreak_queue: list[dict],
) -> dict[str, int]:
    """Match each new_item against the current registry pool. Track verdicts.

    If allow_new=False and verdict ∉ {auto_accept, accept_with_country}, the
    item is dropped (used for metalocus phase 4 — no orphans created).
    """
    counts = defaultdict(int)
    pool = _registry_to_pool(registry)
    pool_cores = [p["name_core"] for p in pool]

    for i, item in enumerate(new_items, 1):
        # Degenerate-core items (1-2 char or generic noise) bypass cross-source
        # matching to avoid hub formation. They still get a stable canonical_id
        # via match_or_create — just by source_id, never by fuzzy name.
        if _is_degenerate_core(item["name_core"]):
            counts["degenerate_skip"] += 1
            if allow_new:
                registry.match_or_create(
                    names={item["name"]},
                    source_refs={item["source"]: [item["id"]]},
                )
                counts["new_orphan"] += 1
            continue

        match_result = _best_match(item, pool_cores, pool)
        if match_result is None:
            verdict = "no_match"
        else:
            top, strict_sim, loose_sim, second_loose_sim = match_result
            verdict = _classify(item, top, strict_sim, loose_sim, second_loose_sim)

        counts[verdict] += 1

        if verdict in ("auto_accept", "accept_with_country"):
            cid = match_result[0]["id"]   # registry canonical_id
            registry.append(cid,
                            names={item["name"]},
                            source_refs={item["source"]: [item["id"]]})
        elif verdict == "needs_tiebreak":
            top, strict_sim, loose_sim, _ = match_result
            tiebreak_queue.append({
                "source_a":   item["source"],
                "id_a":       item["id"],
                "name_a":     item["name"],
                "source_b":   "_registry",
                "id_b":       top["id"],     # registry canonical_id
                "name_b":     top["name"],
                "strict_sim": round(strict_sim, 1),
                "loose_sim":  round(loose_sim, 1),
            })
            # Until Haiku resolves, treat as no match (don't append, don't create
            # new — the tiebreaker pass will append later)
        elif allow_new:
            # Brand-new orphan canonical
            registry.match_or_create(
                names={item["name"]},
                source_refs={item["source"]: [item["id"]]},
            )
            counts["new_orphan"] += 1
        # else (allow_new=False, no_match): DROP

        if i % 2000 == 0:
            print(f"  [{label}] progress: {i}/{len(new_items)} {dict(counts)}",
                  flush=True)
            # Re-snapshot pool every 2000 to pick up newly-appended items
            pool = _registry_to_pool(registry)
            pool_cores = [p["name_core"] for p in pool]

    return dict(counts)


# ---------------------------------------------------------------------------
# Final canonical export (cluster form for QC)
# ---------------------------------------------------------------------------

def export_clusters(registry: ArchitectRegistry, path: str) -> int:
    clusters = []
    for cid, entry in registry.data.items():
        if entry.get("redirected_to"):
            continue
        clusters.append({
            "canonical_arch_id":  cid,
            "canonical_name":     entry["names"][0] if entry["names"] else "",
            "names":              entry["names"],
            "source_refs":        entry["source_refs"],
            "n_sources":          len(entry["source_refs"]),
            "n_members":          sum(len(ids) for ids in entry["source_refs"].values()),
            "first_seen":         entry.get("first_seen"),
            "last_seen":          entry.get("last_seen"),
        })
    clusters.sort(key=lambda c: -c["n_members"])
    payload = {
        "summary": {
            "n_canonicals":          len(clusters),
            "multi_source":          sum(1 for c in clusters if c["n_sources"] >= 2),
            "by_n_sources":          {n: sum(1 for c in clusters if c["n_sources"] == n)
                                       for n in (1, 2, 3, 4)},
        },
        "clusters": clusters,
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return len(clusters)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--canonical-output", default=CANONICAL_OUTPUT)
    ap.add_argument("--tiebreak-output",  default=TIEBREAK_OUTPUT)
    ap.add_argument("--reset", action="store_true",
                    help="Start with empty registry (delete existing JSON first)")
    args = ap.parse_args()

    if args.reset and os.path.exists("data/id_registry_architects.json"):
        os.rename("data/id_registry_architects.json",
                  "data/id_registry_architects.before_reset.json")
        print("reset: existing registry → .before_reset.json", flush=True)

    registry = ArchitectRegistry()
    print(f"initial registry: {registry.stats()}", flush=True)

    tiebreak_queue: list[dict] = []

    print("\n=== Phase 1: Divisare base (1:1) ===", flush=True)
    n1 = phase_1_divisare(registry)
    print(f"phase 1 done: divisare added → {n1}", flush=True)
    print(f"registry stats: {registry.stats()}", flush=True)

    print("\n=== Phase 2: Architizer ===", flush=True)
    arch_z_items = [_to_item(r, "architizer") for r in load_architizer()]
    counts = phase_match_against_registry(
        registry, arch_z_items, label="architizer",
        allow_new=True, tiebreak_queue=tiebreak_queue,
    )
    print(f"phase 2 done: {counts}", flush=True)
    print(f"registry stats: {registry.stats()}", flush=True)

    print("\n=== Phase 3: Archello ===", flush=True)
    arch_o_items = [_to_item(r, "archello") for r in load_archello()]
    counts = phase_match_against_registry(
        registry, arch_o_items, label="archello",
        allow_new=True, tiebreak_queue=tiebreak_queue,
    )
    print(f"phase 3 done: {counts}", flush=True)
    print(f"registry stats: {registry.stats()}", flush=True)

    print("\n=== Phase 4: Metalocus (append-only — no_match dropped) ===",
          flush=True)
    metaloc_items = [_to_item(c, "metalocus") for c in _load_metalocus_clusters()]
    counts = phase_match_against_registry(
        registry, metaloc_items, label="metalocus",
        allow_new=False, tiebreak_queue=tiebreak_queue,
    )
    print(f"phase 4 done: {counts}", flush=True)
    print(f"registry stats: {registry.stats()}", flush=True)

    registry.save()
    print(f"\n✓ registry saved → {registry.path}", flush=True)

    n_clusters = export_clusters(registry, args.canonical_output)
    print(f"✓ canonical clusters: {n_clusters} → {args.canonical_output}",
          flush=True)

    with open(args.tiebreak_output, "w") as f:
        json.dump(tiebreak_queue, f, indent=2, ensure_ascii=False)
    print(f"✓ tiebreak queue: {len(tiebreak_queue)} → {args.tiebreak_output}",
          flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
