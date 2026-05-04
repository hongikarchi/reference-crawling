"""Split building tiebreak pairs into N batches for parallel Sonnet processing.

Each tiebreak entry is already self-contained (has all context: name, arch
names, country, city, year, typology, cover URL — both sides). This script
just slices the queue into per-batch JSON files.

Optional --skip-already-merged: filter out pairs where the source's id is
already attached to a canonical (defensive for re-runs after Hybrid
feedback loop).
"""
from __future__ import annotations

import argparse
import json
import os
import sys


TIEBREAK_FULL = "data/canonical/building_tiebreak_pairs.json"
BATCH_DIR     = "data/canonical/tiebreak_batches_buildings"
REGISTRY_PATH = "data/id_registry_buildings.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-batches", type=int, default=16)
    ap.add_argument("--skip-already-merged", action="store_true")
    ap.add_argument("--in",  dest="in_path",  default=TIEBREAK_FULL)
    ap.add_argument("--dir", dest="batch_dir", default=BATCH_DIR)
    args = ap.parse_args()

    tiebreak = json.load(open(args.in_path))
    print(f"total tiebreak pairs: {len(tiebreak)}", flush=True)

    if args.skip_already_merged:
        from canonical.registry import BuildingRegistry
        reg = BuildingRegistry(REGISTRY_PATH)
        # Build (source, source_id) → canonical_id index
        src_idx: dict[tuple, str] = {}
        for cid, e in reg.data.items():
            if e.get("redirected_to"):
                continue
            for src, ids in e.get("source_refs", {}).items():
                for sid in ids:
                    src_idx[(src, str(sid))] = cid
        before = len(tiebreak)
        tiebreak = [
            p for p in tiebreak
            if (p["source_a"], str(p["id_a"])) not in src_idx
        ]
        print(f"  filtered already-merged: {before} → {len(tiebreak)}", flush=True)

    os.makedirs(args.batch_dir, exist_ok=True)
    batches: list[list[dict]] = [[] for _ in range(args.n_batches)]
    for k, p in enumerate(tiebreak):
        # Local index per batch + back-reference to original
        entry = dict(p)
        entry["i"]    = k
        entry["orig"] = k  # original tiebreak index = local index in this case
        batches[k % args.n_batches].append(entry)

    sizes = []
    for n, batch in enumerate(batches):
        path = os.path.join(args.batch_dir, f"batch_{n:02d}.json")
        with open(path, "w") as f:
            json.dump(batch, f, indent=2, ensure_ascii=False)
        sizes.append(len(batch))
        print(f"  batch_{n:02d}: {len(batch):>5} → {path}", flush=True)

    print(f"\n✓ {args.n_batches} batches ({min(sizes)}-{max(sizes)} pairs each, "
          f"{sum(sizes)} total)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
