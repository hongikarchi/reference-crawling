#!/usr/bin/env python3
"""Option B architect_canonical_ids backfill.

For each building in canonical_buildings_strict.json:
  - resolve all architects from source_refs via source DBs
  - reverse-lookup arch_canonical_id from architects_canonical.json
  - SAFE: only commit when n_sources==1 OR all sources point to same arch_id
  - multi-arch ambiguity → leave architect_canonical_ids = [] (no false data)

Reads:
  data/canonical/canonical_buildings_strict.json
  data/canonical/architects_canonical.json
  data/crawl/{metalocus,divisare,architizer,archello}.db

Writes (NEW file, original untouched):
  data/canonical/canonical_buildings_strict_v2.json
"""
from __future__ import annotations
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "data/canonical/canonical_buildings_strict.json"
ARCH = ROOT / "data/canonical/architects_canonical.json"
OUTPUT = ROOT / "data/canonical/canonical_buildings_strict_v2.json"
SOURCE_DBS = {
    "divisare":   ROOT / "data/crawl/divisare.db",
    "architizer": ROOT / "data/crawl/architizer.db",
    "archello":   ROOT / "data/crawl/archello.db",
    "metalocus":  ROOT / "data/crawl/metalocus.db",
}


def build_arch_index(arch_path: Path) -> dict:
    arch = json.load(arch_path.open())
    index = {}  # (source, source_id) -> canonical_arch_id
    arch_by_id = {}
    for c in arch["clusters"]:
        aid = c["canonical_arch_id"]
        arch_by_id[aid] = c
        for src, ids in (c.get("source_refs") or {}).items():
            for sid in ids:
                index[(src, str(sid))] = aid
    print(f"[arch] {len(arch_by_id):,} architect canonicals, "
          f"{len(index):,} (source,id)→arch_id mappings")
    return index, arch_by_id


def building_arch_refs(conn: sqlite3.Connection, source: str, source_id: str) -> list[str]:
    """Return architect source_ids referenced by this building project."""
    try:
        if source == "divisare":
            row = conn.execute(
                "SELECT architect_ids FROM divisare_projects WHERE id=?",
                (source_id,)).fetchone()
            if row and row[0]:
                try:
                    return [str(a) for a in json.loads(row[0]) if a]
                except (json.JSONDecodeError, TypeError):
                    return []
            return []
        if source == "archello":
            rows = conn.execute(
                "SELECT architect_brand_id FROM archello_projects WHERE id=?",
                (source_id,)).fetchall()
            return [str(r[0]) for r in rows if r[0]]
        if source == "architizer":
            rows = conn.execute(
                "SELECT firm_slug FROM architizer_projects WHERE id=?",
                (source_id,)).fetchall()
            return [str(r[0]) for r in rows if r[0]]
        # metalocus has no stable arch IDs
    except sqlite3.OperationalError as exc:
        print(f"  warn: {source} db query failed: {exc}")
    return []


def main():
    print(f"loading {INPUT}")
    data = json.load(INPUT.open())
    buildings = data["buildings"]
    print(f"  {len(buildings):,} buildings")

    arch_index, arch_by_id = build_arch_index(ARCH)
    conns = {s: sqlite3.connect(p) for s, p in SOURCE_DBS.items() if p.exists()}
    print(f"opened source DBs: {list(conns)}")

    stats = {
        "total":           0,
        "single_source":   0,  # n_sources == 1, took whatever arch was found
        "multi_unanimous": 0,  # n_sources > 1, all sources agreed on arch set
        "multi_ambiguous": 0,  # n_sources > 1, sources disagreed → empty
        "no_arch_data":    0,  # no arch found in any source DB → empty
        "filled":          0,
    }

    for b in buildings:
        stats["total"] += 1
        cid = b["canonical_bld_id"]
        src_refs = b.get("source_refs") or {}

        per_source_arch = {}  # source -> set of arch_canonical_ids
        for src, sids in src_refs.items():
            if src not in conns:
                continue
            arches = set()
            for sid in sids:
                for arch_sid in building_arch_refs(conns[src], src, str(sid)):
                    aid = arch_index.get((src, arch_sid))
                    if aid:
                        arches.add(aid)
            per_source_arch[src] = arches

        non_empty = {s: a for s, a in per_source_arch.items() if a}

        if not non_empty:
            stats["no_arch_data"] += 1
            chosen = []
        elif len(non_empty) == 1:
            stats["single_source"] += 1
            chosen = sorted(next(iter(non_empty.values())))
        else:
            # Multi-source: take intersection (all sources agree)
            sets = list(non_empty.values())
            intersect = set.intersection(*sets)
            if intersect:
                stats["multi_unanimous"] += 1
                chosen = sorted(intersect)
            else:
                stats["multi_ambiguous"] += 1
                chosen = []

        if chosen:
            stats["filled"] += 1
            b["architect_canonical_ids"] = chosen
            b["architect_names"] = [
                (arch_by_id.get(aid) or {}).get("canonical_name") for aid in chosen
            ]
            b["architect_names"] = [n for n in b["architect_names"] if n]

    print()
    print("backfill stats (Option B):")
    for k, v in stats.items():
        pct = v / stats["total"] * 100
        print(f"  {k:>16}: {v:>7,} ({pct:5.1f}%)")

    print(f"\nwriting {OUTPUT}")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w") as f:
        json.dump(data, f, ensure_ascii=False)
    print("done")


if __name__ == "__main__":
    main()
