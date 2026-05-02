"""Split the remaining 7,650 tiebreak pairs into N enriched batches.

The pilot (100 pairs) already lives in tiebreak_batches/batch_pilot.json
+ tiebreak_results/batch_pilot_sonnet.json. This script processes the
remainder by mod-N partitioning over the original tiebreak index, then
enriches each batch with up to 5 sample projects per side (using the
same logic as tools/build_tiebreak_projects.py).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from tools.build_tiebreak_projects import (
    fmt_project, load_project_index, projects_for_registry_entry,
    PROJECTS_PER_SIDE,
)


TIEBREAK_FULL    = "data/canonical/architect_tiebreak_pairs.json"
PILOT_BATCH      = "data/canonical/tiebreak_batches/batch_pilot.json"
BATCH_DIR        = "data/canonical/tiebreak_batches"
REGISTRY_PATH    = "data/id_registry_architects.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-batches", type=int, default=16)
    args = ap.parse_args()

    tiebreak = json.load(open(TIEBREAK_FULL))
    print(f"total tiebreak pairs: {len(tiebreak)}", flush=True)

    # Identify the 100 already-processed pilot pairs
    pilot = json.load(open(PILOT_BATCH))
    processed_orig = {p["orig"] for p in pilot}
    print(f"pilot already processed: {len(processed_orig)}", flush=True)

    remaining: list[tuple[int, dict]] = [
        (i, p) for i, p in enumerate(tiebreak) if i not in processed_orig
    ]
    print(f"remaining pairs: {len(remaining)}", flush=True)

    print("loading project index …", flush=True)
    project_idx = load_project_index()
    print(f"  {len(project_idx)} (source, id) keys", flush=True)
    registry_data = json.load(open(REGISTRY_PATH))

    os.makedirs(BATCH_DIR, exist_ok=True)
    # Even mod-N partition (deterministic, balanced)
    batches: list[list[dict]] = [[] for _ in range(args.n_batches)]
    for k, (orig_idx, pair) in enumerate(remaining):
        a_key = (pair["source_a"], str(pair["id_a"]))
        a_projs = project_idx.get(a_key, [])[:PROJECTS_PER_SIDE]
        b_projs = projects_for_registry_entry(
            pair["id_b"], registry_data, project_idx
        )[:PROJECTS_PER_SIDE]
        entry = {
            "i":          k,             # index within the FULL remaining set
            "orig":       orig_idx,
            "a_name":     pair["name_a"],
            "a_source":   pair["source_a"],
            "a_projects": [fmt_project(x) for x in a_projs],
            "b_name":     pair["name_b"],
            "b_projects": [fmt_project(x) for x in b_projs],
        }
        batches[k % args.n_batches].append(entry)

    sizes = []
    for n, batch in enumerate(batches):
        path = os.path.join(BATCH_DIR, f"batch_{n:02d}.json")
        with open(path, "w") as f:
            json.dump(batch, f, indent=2, ensure_ascii=False)
        sizes.append(len(batch))
        print(f"  batch_{n:02d}: {len(batch):>4} → {path}", flush=True)

    print(f"\n✓ {args.n_batches} batches ({min(sizes)}-{max(sizes)} pairs each, "
          f"{sum(sizes)} total)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
