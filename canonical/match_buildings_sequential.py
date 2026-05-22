"""Stage B — Sequential 4-phase building matcher with 2-pass + tiebreak.

Pass 1 (architect-anchored, strict):
  Phase 1 Divisare:   29,955 → 1:1 register (base)
  Phase 2 Architizer: 10,632 → match against pool, pre-filter
                                (canonical_arch_id, year±2). Fallback
                                (country, year±2) when no arch_id.
  Phase 3 Archello:   127,219 → same
  Phase 4 Metalocus:  3,465  → same; allow_new=True (decision #6: keep all)

Auto-accept gate (decision #4):
  arch_id✓ AND year exact AND name strict_sim ≥ 90

Tiebreak floor: name loose_sim ≥ 78 AND (arch_id✓ OR country✓).
  Below floor → no_match → orphan.

Multi-architect (decision #8): a building's `canonical_arch_ids` list of
length N contributes to N pre-filter groups in the pool index. Same logic
on the matcher side (we expand to all (arch_id, year±2) groups when looking
up candidates).

Pass 2 (orphan rescue, no arch_id required):
  After all phases, take each orphan canonical_building. Look for merges
  using (country, year±1) AND name strict_sim ≥ 90 AND name token overlap
  ≥ 2 (≥3-char tokens). Auto-accept those; tiebreak the borderline.

Outputs:
  data/id_registry_buildings.json
  data/canonical/buildings_canonical.json
  data/canonical/building_tiebreak_pairs.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from typing import Iterable, Optional

from rapidfuzz import fuzz, process

from canonical._building_loaders import (
    _arch_lookup, load_divisare, load_architizer, load_archello, load_metalocus,
)
from canonical.match_architects import (
    _normalize_name, _is_substring_match,
)
from canonical.match_phash_check import has_phash_overlap
from canonical.registry import ArchitectRegistry, BuildingRegistry


CANONICAL_OUTPUT = "data/canonical/buildings_canonical.json"
TIEBREAK_OUTPUT  = "data/canonical/building_tiebreak_pairs.json"
PHASH_BLOCKS_OUTPUT = "data/reports/phash_blocks.json"

# Auto-accept gate (per decision #4): strictest combo
AUTO_ACCEPT_NAME_SIM = 90.0
TIEBREAK_FLOOR       = 78.0
# Pass-2 fallback gate (per decision #9): tighter on name to compensate for
# missing arch_id signal
PASS2_AUTO_NAME_SIM  = 90.0
PASS2_MIN_TOKEN_OVERLAP = 2
YEAR_TOLERANCE       = 2   # Pass 1 ±N years
PASS2_YEAR_TOLERANCE = 1   # Pass 2 tighter
PHASH_GATE_ENABLED   = True

# Generic name tokens that don't count toward token overlap
GENERIC_TOKENS = {
    "house", "home", "office", "tower", "building", "project", "studio",
    "the", "a", "an", "and", "of", "for", "by", "with", "in", "on",
    "centre", "center", "store", "shop", "park", "garden", "plaza",
    "school", "library", "museum", "hotel", "restaurant", "cafe", "bar",
    "residence", "residential", "apartment", "apartments", "complex",
}

PHASH_BLOCK_COUNTER: Counter = Counter()
PHASH_BLOCK_LOG: list[dict] = []


def _name_tokens(core: str) -> set[str]:
    return {t for t in core.split() if len(t) >= 3 and t not in GENERIC_TOKENS}


def _write_phash_blocks(path: str = PHASH_BLOCKS_OUTPUT) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(PHASH_BLOCK_LOG, f, indent=2, ensure_ascii=False)


def _reset_phash_blocks() -> None:
    PHASH_BLOCK_COUNTER.clear()
    PHASH_BLOCK_LOG.clear()


def _record_phash_block(
    *,
    existing_cid: str,
    incoming_id: str,
    src_a: str,
    src_b: str,
    result: dict,
    phase: str,
) -> None:
    if result.get("verdict") == "BLOCK":
        PHASH_BLOCK_COUNTER["total"] += 1
        PHASH_BLOCK_COUNTER[phase] += 1
    PHASH_BLOCK_LOG.append({
        "phase": phase,
        "cluster_id_a": existing_cid,
        "cluster_id_b": incoming_id,
        "src_a": src_a,
        "src_b": src_b,
        "verdict": str(result.get("verdict", "")).lower(),
        "overlap": result.get("overlap", 0),
        "a_n": result.get("a_n", 0),
        "b_n": result.get("b_n", 0),
    })
    _write_phash_blocks(PHASH_BLOCKS_OUTPUT)


def _phash_allows_merge(
    registry: BuildingRegistry,
    existing_cid: str,
    incoming_refs: dict[str, list[str]],
    *,
    incoming_id: str,
    phase: str,
    name_a: str | None = None,
    name_b: str | None = None,
    year_a: object = None,
    year_b: object = None,
) -> bool:
    if not PHASH_GATE_ENABLED:
        return True
    existing_cid = registry.follow(existing_cid)
    existing_refs = registry.data.get(existing_cid, {}).get("source_refs", {})
    for src_a, src_a_ids in existing_refs.items():
        for src_b, src_b_ids in incoming_refs.items():
            if src_a == src_b:
                continue
            result = has_phash_overlap(
                src_a_ids,
                src_b_ids,
                src_a,
                src_b,
                name_a=name_a,
                name_b=name_b,
                year_a=year_a,
                year_b=year_b,
            )
            if result.get("verdict") == "BLOCK":
                _record_phash_block(
                    existing_cid=existing_cid,
                    incoming_id=incoming_id,
                    src_a=src_a,
                    src_b=src_b,
                    result=result,
                    phase=phase,
                )
                return False
            if result.get("verdict") == "TIEBREAKER_PASS":
                _record_phash_block(
                    existing_cid=existing_cid,
                    incoming_id=incoming_id,
                    src_a=src_a,
                    src_b=src_b,
                    result=result,
                    phase=phase,
                )
    return True


def _append_tiebreak(
    tiebreak_queue: list,
    *,
    pass_no: int,
    item: dict,
    top: dict,
    strict: float,
    loose: float,
    phash_blocked: bool = False,
) -> None:
    tiebreak_queue.append({
        "pass":           pass_no,
        "source_a":       item["source"],
        "id_a":           item["id"],
        "name_a":         item["name"],
        "arch_names_a":   item["canonical_arch_ids"],
        "country_a":      item.get("country"),
        "city_a":         item.get("city"),
        "year_a":         item.get("year"),
        "typology_a":     item.get("typology"),
        "cover_a":        item.get("cover_image_url"),
        "cid_b":          top["cid"],
        "name_b":         top["name"],
        "arch_names_b":   top.get("canonical_arch_ids") or [],
        "country_b":      top.get("country"),
        "city_b":         top.get("city"),
        "year_b":         top.get("year"),
        "typology_b":     top.get("typology"),
        "cover_b":        top.get("cover_image_url"),
        "strict_sim":     round(strict, 1),
        "loose_sim":      round(loose, 1),
        "phash_blocked":  phash_blocked,
    })


def _print_phase_summary(phase: str, counts: dict[str, int]) -> None:
    print(
        f"Phase {phase}: {counts.get('auto_accept', 0)} auto-accepts, "
        f"{counts.get('phash_block', 0)} phash-blocks",
        flush=True,
    )


# ---------------------------------------------------------------------------
# Pool indexing
# ---------------------------------------------------------------------------

class BuildingPool:
    """Pool of registered canonical_buildings indexed for fast candidate lookup.

    Two indices:
      idx_by_arch:  (canonical_arch_id) -> list of pool entries
      idx_by_geo:   (country)           -> list of pool entries (Pass 2)

    A pool entry is a dict mirroring the loader output plus 'cid' (canonical
    building id) for back-reference.
    """
    def __init__(self) -> None:
        self.entries: list[dict] = []
        self.idx_by_arch: dict[str, list[int]] = defaultdict(list)
        self.idx_by_geo:  dict[str, list[int]] = defaultdict(list)

    def add(self, item: dict, cid: str) -> None:
        i = len(self.entries)
        e = {**item, "cid": cid}
        self.entries.append(e)
        for aid in item["canonical_arch_ids"]:
            self.idx_by_arch[aid].append(i)
        if item.get("country"):
            self.idx_by_geo[item["country"]].append(i)

    def candidates_pass1(self, item: dict) -> list[dict]:
        """Pre-filter (canonical_arch_id, year±2). Multi-arch buildings union
        all groups."""
        seen: set[int] = set()
        target_year = item.get("year")
        for aid in item["canonical_arch_ids"]:
            for i in self.idx_by_arch.get(aid, []):
                if i in seen:
                    continue
                e = self.entries[i]
                ey = e.get("year")
                if target_year is None or ey is None:
                    seen.add(i)
                elif abs(target_year - ey) <= YEAR_TOLERANCE:
                    seen.add(i)
        return [self.entries[i] for i in seen]

    def candidates_pass1_country_fallback(self, item: dict) -> list[dict]:
        """When no arch_id available, fall back to (country, year±2)."""
        country = item.get("country")
        if not country:
            return []
        target_year = item.get("year")
        out = []
        for i in self.idx_by_geo.get(country, []):
            e = self.entries[i]
            ey = e.get("year")
            if target_year is None or ey is None:
                out.append(e)
            elif abs(target_year - ey) <= YEAR_TOLERANCE:
                out.append(e)
        return out

    def candidates_pass2(self, item: dict) -> list[dict]:
        """Pass 2 orphan rescue: (country, year±1) only — no arch_id needed."""
        country = item.get("country")
        if not country:
            return []
        target_year = item.get("year")
        out = []
        for i in self.idx_by_geo.get(country, []):
            e = self.entries[i]
            if e["cid"] == item.get("_self_cid"):
                continue  # don't match self
            ey = e.get("year")
            if target_year is None or ey is None:
                continue
            if abs(target_year - ey) <= PASS2_YEAR_TOLERANCE:
                out.append(e)
        return out


# ---------------------------------------------------------------------------
# Scoring + verdict
# ---------------------------------------------------------------------------

def _name_scores(a_core: str, b_cores: list[str]) -> list[tuple[float, float]]:
    """Returns [(strict_sim, loose_sim), ...] for each b_core."""
    if not b_cores or not a_core:
        return []
    sort_scores = process.cdist([a_core], b_cores, scorer=fuzz.token_sort_ratio)[0]
    set_scores  = process.cdist([a_core], b_cores, scorer=fuzz.token_set_ratio)[0]
    return [
        (min(float(sort_scores[i]), float(set_scores[i])),
         max(float(sort_scores[i]), float(set_scores[i])))
        for i in range(len(b_cores))
    ]


def _classify_pass1(item: dict, top: dict, strict: float, loose: float) -> str:
    """Pass 1 verdict: strict gate uses arch_id + year + strict_sim."""
    a_arch = set(item.get("canonical_arch_ids") or [])
    b_arch = set(top.get("canonical_arch_ids") or [])
    arch_match = bool(a_arch & b_arch)
    year_match = (item.get("year") is not None
                  and top.get("year") is not None
                  and item["year"] == top["year"])

    # Auto-accept: all strong signals aligned
    if arch_match and year_match and strict >= AUTO_ACCEPT_NAME_SIM:
        return "auto_accept"
    # Strong same-name even without exact year (within tolerance) — auto only
    # if the names also share a distinctive (non-generic) token, so two
    # generically-named projects by one architect cannot silently auto-merge.
    if (arch_match and strict >= 95.0
            and _name_tokens(item["name_core"]) & _name_tokens(top["name_core"])):
        return "auto_accept"
    # Tiebreak floor
    if loose >= TIEBREAK_FLOOR and (arch_match or
                                     (item.get("country") and item["country"] == top.get("country"))):
        return "needs_tiebreak"
    return "no_match"


def _classify_pass2(item: dict, top: dict, strict: float, loose: float) -> str:
    """Pass 2 (no arch_id): tighter name + token overlap requirement."""
    if item.get("country") != top.get("country"):
        return "no_match"
    if item.get("year") is None or top.get("year") is None:
        return "no_match"
    if abs(item["year"] - top["year"]) > PASS2_YEAR_TOLERANCE:
        return "no_match"
    overlap = len(_name_tokens(item["name_core"]) & _name_tokens(top["name_core"]))
    if overlap < PASS2_MIN_TOKEN_OVERLAP:
        # Without enough distinctive token overlap, very risky
        return "no_match"
    if strict >= PASS2_AUTO_NAME_SIM:
        return "auto_accept"
    if loose >= TIEBREAK_FLOOR:
        return "needs_tiebreak"
    return "no_match"


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------

def phase_1_divisare(registry: BuildingRegistry, pool: BuildingPool) -> int:
    """Each Divisare project → unique canonical (1:1, no within-source merge)."""
    n = 0
    for item in load_divisare(_arch_idx):
        cid, _status = registry.match_or_create(
            names={item["name"]},
            source_refs={"divisare": [item["id"]]},
        )
        pool.add(item, cid)
        n += 1
        if n % 5000 == 0:
            print(f"  phase 1 progress: {n}", flush=True)
    return n


def phase_match_against_pool(
    registry: BuildingRegistry, pool: BuildingPool, items: Iterable[dict],
    *, label: str, allow_new: bool, tiebreak_queue: list,
) -> dict[str, int]:
    counts: Counter = Counter()
    dropped: list[dict] = []
    n = 0
    for item in items:
        n += 1
        # Get candidates (arch-anchored first, country-fallback second)
        cands = pool.candidates_pass1(item)
        if not cands and not item["canonical_arch_ids"]:
            cands = pool.candidates_pass1_country_fallback(item)
        if not cands:
            counts["no_candidates"] += 1
            if allow_new:
                cid, _ = registry.match_or_create(
                    names={item["name"]},
                    source_refs={item["source"]: [item["id"]]},
                )
                pool.add(item, cid)
                counts["new_orphan"] += 1
            continue

        b_cores = [c["name_core"] for c in cands]
        scores = _name_scores(item["name_core"], b_cores)
        # Best by loose_sim
        ranked = sorted(zip(cands, scores), key=lambda t: -t[1][1])
        top, (strict, loose) = ranked[0]
        verdict = _classify_pass1(item, top, strict, loose)

        if verdict == "auto_accept":
            incoming_refs = {item["source"]: [item["id"]]}
            if not _phash_allows_merge(
                registry,
                top["cid"],
                incoming_refs,
                incoming_id=item["id"],
                phase=label,
                name_a=top.get("name"),
                name_b=item.get("name"),
                year_a=top.get("year"),
                year_b=item.get("year"),
            ):
                counts["phash_block"] += 1
                counts["needs_tiebreak"] += 1
                _append_tiebreak(
                    tiebreak_queue,
                    pass_no=1,
                    item=item,
                    top=top,
                    strict=strict,
                    loose=loose,
                    phash_blocked=True,
                )
                continue
            registry.append(top["cid"],
                            names={item["name"]},
                            source_refs=incoming_refs)
            # Pool entry gets new source_ref but stays under same cid (no new pool entry needed)
            counts["auto_accept"] += 1
        elif verdict == "needs_tiebreak":
            counts["needs_tiebreak"] += 1
            _append_tiebreak(
                tiebreak_queue,
                pass_no=1,
                item=item,
                top=top,
                strict=strict,
                loose=loose,
            )
        elif allow_new:
            counts[verdict] += 1
            cid, _ = registry.match_or_create(
                names={item["name"]},
                source_refs={item["source"]: [item["id"]]},
            )
            pool.add(item, cid)
            counts["new_orphan"] += 1
        else:
            # match-or-drop: no candidate accepted and allow_new is False, so
            # this source row is discarded — record it for an audit trail.
            counts[verdict] += 1
            dropped.append({
                "source": item["source"],
                "id": item["id"],
                "name": item.get("name"),
                "verdict": verdict,
                "nearest_cid": top["cid"],
                "strict": round(strict, 1),
                "loose": round(loose, 1),
            })

        if n % 5000 == 0:
            print(f"  [{label}] progress: {n} {dict(counts)}", flush=True)

    if dropped:
        drop_path = f"data/reports/match_drops_{label}.json"
        os.makedirs(os.path.dirname(drop_path), exist_ok=True)
        with open(drop_path, "w", encoding="utf-8") as f:
            json.dump(dropped, f, indent=2, ensure_ascii=False)
        print(f"  [{label}] {len(dropped)} dropped items logged -> {drop_path}",
              flush=True)
    return dict(counts)


def pass_2_orphan_rescue(
    registry: BuildingRegistry, pool: BuildingPool, tiebreak_queue: list,
) -> dict[str, int]:
    """For each canonical_building that's still single-source, try (country,
    year±1, name overlap) match against the pool."""
    counts: Counter = Counter()
    # Build cid → pool entry index for O(1) self-lookup
    cid_to_entry: dict[str, dict] = {}
    for e in pool.entries:
        # Multiple pool entries can share a cid (after auto_accept appends);
        # keep the first one as representative
        cid_to_entry.setdefault(e["cid"], e)
    # Snapshot current orphans (single-source canonical IDs)
    orphan_cids = [
        cid for cid, e in registry.data.items()
        if not e.get("redirected_to") and len(e.get("source_refs", {})) == 1
    ]
    print(f"  pass2: {len(orphan_cids)} orphan canonicals to rescue", flush=True)
    n = 0
    for cid in orphan_cids:
        n += 1
        e0 = cid_to_entry.get(cid)
        if e0 is None:
            continue
        item = dict(e0)
        item["_self_cid"] = cid
        cands = pool.candidates_pass2(item)
        if not cands:
            counts["no_candidates"] += 1
            continue
        # Filter cands to NOT include self-cid clusters
        cands = [c for c in cands if c["cid"] != cid]
        if not cands:
            counts["no_candidates"] += 1
            continue
        b_cores = [c["name_core"] for c in cands]
        scores = _name_scores(item["name_core"], b_cores)
        ranked = sorted(zip(cands, scores), key=lambda t: -t[1][1])
        top, (strict, loose) = ranked[0]
        verdict = _classify_pass2(item, top, strict, loose)

        if verdict == "auto_accept":
            # Merge cid → top["cid"]: redirect cid; copy source_refs over
            entry = registry.data[cid]
            if not _phash_allows_merge(
                registry,
                top["cid"],
                entry.get("source_refs", {}),
                incoming_id=cid,
                phase="pass2",
                name_a=top.get("name"),
                name_b=item.get("name"),
                year_a=top.get("year"),
                year_b=item.get("year"),
            ):
                counts["phash_block"] += 1
                counts["needs_tiebreak"] += 1
                _append_tiebreak(
                    tiebreak_queue,
                    pass_no=2,
                    item=item,
                    top=top,
                    strict=strict,
                    loose=loose,
                    phash_blocked=True,
                )
                continue
            for src, ids in entry.get("source_refs", {}).items():
                registry.append(top["cid"],
                                names=set(entry.get("names", [])),
                                source_refs={src: ids})
            entry["redirected_to"] = top["cid"]
            counts["auto_accept"] += 1
            counts["pass2_merged"] += 1
        elif verdict == "needs_tiebreak":
            counts["needs_tiebreak"] += 1
            _append_tiebreak(
                tiebreak_queue,
                pass_no=2,
                item=item,
                top=top,
                strict=strict,
                loose=loose,
            )
        else:
            counts[verdict] += 1

        if n % 5000 == 0:
            print(f"  [pass2] progress: {n}/{len(orphan_cids)} {dict(counts)}",
                  flush=True)
    return dict(counts)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_clusters(registry: BuildingRegistry, path: str) -> int:
    clusters = []
    for cid, entry in registry.data.items():
        if entry.get("redirected_to"):
            continue
        clusters.append({
            "canonical_bld_id":   cid,
            "canonical_name":     entry["names"][0] if entry.get("names") else "",
            "names":              entry.get("names", []),
            "source_refs":        entry.get("source_refs", {}),
            "n_sources":          len(entry.get("source_refs", {})),
            "n_members":          sum(len(ids) for ids in entry.get("source_refs", {}).values()),
            "first_seen":         entry.get("first_seen"),
            "last_seen":          entry.get("last_seen"),
        })
    clusters.sort(key=lambda c: -c["n_members"])
    payload = {
        "summary": {
            "n_canonicals":  len(clusters),
            "multi_source":  sum(1 for c in clusters if c["n_sources"] >= 2),
            "by_n_sources":  {n: sum(1 for c in clusters if c["n_sources"] == n)
                              for n in (1, 2, 3, 4)},
        },
        "clusters": clusters,
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return len(clusters)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# Module-level ref so phase_1_divisare can use the loader without re-fetching idx
_arch_idx: dict = {}


def main() -> int:
    global _arch_idx
    ap = argparse.ArgumentParser()
    ap.add_argument("--canonical-output", default=CANONICAL_OUTPUT)
    ap.add_argument("--tiebreak-output",  default=TIEBREAK_OUTPUT)
    ap.add_argument("--reset", action="store_true",
                    help="Start with empty registry")
    args = ap.parse_args()

    if args.reset and os.path.exists("data/id_registry_buildings.json"):
        os.rename("data/id_registry_buildings.json",
                  "data/id_registry_buildings.before_reset.json")
        print("reset: existing registry → .before_reset.json", flush=True)

    print("loading Stage A architect registry …", flush=True)
    arch_reg = ArchitectRegistry()
    _arch_idx = _arch_lookup(arch_reg)
    print(f"  arch index size: {len(_arch_idx)}", flush=True)

    registry = BuildingRegistry()
    pool = BuildingPool()
    tiebreak_queue: list = []
    _reset_phash_blocks()
    if PHASH_GATE_ENABLED:
        _write_phash_blocks()
    print(f"initial registry: {registry.stats()}", flush=True)

    print("\n=== PASS 1 ===", flush=True)

    print("\n--- Phase 1: Divisare base (1:1) ---", flush=True)
    n1 = phase_1_divisare(registry, pool)
    _print_phase_summary("1", {})
    print(f"phase 1 done: {n1}; registry: {registry.stats()}", flush=True)

    print("\n--- Phase 2: Architizer (allow_new) ---", flush=True)
    counts = phase_match_against_pool(
        registry, pool, load_architizer(_arch_idx),
        label="architizer", allow_new=True, tiebreak_queue=tiebreak_queue,
    )
    _print_phase_summary("2", counts)
    print(f"phase 2 done: {counts}; registry: {registry.stats()}", flush=True)

    # NEW DESIGN (2026-05-05): Metalocus + Archello are MATCH-OR-DROP only.
    # Rationale: divisare + architizer have clean 1-source-id-per-building
    # data. Metalocus + Archello have known data-quality issues (multi-listing,
    # supplier-as-architect). Treat them as enrichment, not as new canonicals.

    print("\n--- Phase 3: Metalocus (match-or-drop) ---", flush=True)
    counts = phase_match_against_pool(
        registry, pool, load_metalocus(_arch_idx),
        label="metalocus", allow_new=False, tiebreak_queue=tiebreak_queue,
    )
    _print_phase_summary("3", counts)
    print(f"phase 3 done: {counts}; registry: {registry.stats()}", flush=True)

    print("\n--- Phase 4: Archello (match-or-drop) ---", flush=True)
    counts = phase_match_against_pool(
        registry, pool, load_archello(_arch_idx),
        label="archello", allow_new=False, tiebreak_queue=tiebreak_queue,
    )
    _print_phase_summary("4", counts)
    print(f"phase 4 done: {counts}; registry: {registry.stats()}", flush=True)

    # No Pass 2 in new design — div+arc base is already authoritative,
    # no orphan rescue needed since archello/metalocus orphans were dropped
    # rather than created.

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
