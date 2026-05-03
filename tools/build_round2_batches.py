"""Round-2 tiebreak: filter pairs whose source_a is already registered,
then split the truly-new remainder into N enriched batches.

Round-1 SAME decisions already live in the registry; running Sonnet on
those pairs again could produce inconsistent decisions and corrupt the
source-id index. This script keeps only pairs where the new item has no
existing canonical attachment.
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


TIEBREAK_FULL = "data/canonical/architect_tiebreak_pairs.json"
BATCH_DIR     = "data/canonical/tiebreak_batches_r2"
REGISTRY_PATH = "data/id_registry_architects.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-batches", type=int, default=16)
    args = ap.parse_args()

    tiebreak = json.load(open(TIEBREAK_FULL))
    print(f"total tiebreak pairs: {len(tiebreak)}", flush=True)

    registry_data = json.load(open(REGISTRY_PATH))
    src_idx: dict[tuple, str] = {}
    for cid, e in registry_data.items():
        if e.get("redirected_to"):
            continue
        for src, ids in e.get("source_refs", {}).items():
            for sid in ids:
                src_idx[(src, str(sid))] = cid

    # Filter: keep only pairs whose source_a/id_a is NOT yet registered
    truly_new: list[tuple[int, dict]] = []
    skipped = 0
    for orig_idx, p in enumerate(tiebreak):
        key = (p["source_a"], str(p["id_a"]))
        if key in src_idx:
            skipped += 1
            continue
        truly_new.append((orig_idx, p))
    print(f"  already-registered (skipped): {skipped}", flush=True)
    print(f"  truly-new (will batch):        {len(truly_new)}", flush=True)

    print("loading project index …", flush=True)
    project_idx = load_project_index()
    print(f"  {len(project_idx)} (source, id) keys", flush=True)

    os.makedirs(BATCH_DIR, exist_ok=True)
    batches: list[list[dict]] = [[] for _ in range(args.n_batches)]
    for k, (orig_idx, pair) in enumerate(truly_new):
        a_key = (pair["source_a"], str(pair["id_a"]))
        a_projs = project_idx.get(a_key, [])[:PROJECTS_PER_SIDE]
        b_projs = projects_for_registry_entry(
            pair["id_b"], registry_data, project_idx
        )[:PROJECTS_PER_SIDE]
        entry = {
            "i":          k,
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
