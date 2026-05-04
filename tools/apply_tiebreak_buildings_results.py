"""Apply Sonnet building-tiebreak decisions to the building registry.

For each pair:
  • SAME       → registry.append(cid_b, names, source_refs)
  • DIFFERENT  → match_or_create(names, source_refs) — creates a new orphan
                 canonical (decision #6: All keep, even for metalocus)

Safety guards:
  • Skip pairs whose (source_a, id_a) is already in source_index (avoid
    moving source_id between canonicals on re-runs).
  • Skip pairs where target cid_b is missing or fully redirected to nothing.

Re-exports buildings_canonical.json after applying.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

from canonical.match_buildings_sequential import export_clusters
from canonical.registry import BuildingRegistry


TIEBREAK_FULL = "data/canonical/building_tiebreak_pairs.json"
BATCH_DIR     = "data/canonical/tiebreak_batches_buildings"
RESULT_DIR    = "data/canonical/tiebreak_results_buildings"
CANONICAL_OUT = "data/canonical/buildings_canonical.json"


def load_decisions(n_batches: int, batch_dir: str, result_dir: str) -> dict[int, dict]:
    """Returns {orig_idx: {decision, type, reason, batch}}."""
    decisions: dict[int, dict] = {}
    for n in range(n_batches):
        in_path  = os.path.join(batch_dir,  f"batch_{n:02d}.json")
        out_path = os.path.join(result_dir, f"batch_{n:02d}.json")
        if not os.path.exists(out_path):
            print(f"  WARN missing result file {out_path}", file=sys.stderr)
            continue
        in_rows = {p["i"]: p for p in json.load(open(in_path))}
        for r in json.load(open(out_path)):
            i = r["i"]
            if i not in in_rows:
                print(f"  WARN batch {n} result idx {i} has no input row",
                      file=sys.stderr)
                continue
            decisions[in_rows[i]["orig"]] = {
                "decision": r["decision"], "type": r.get("type", "?"),
                "reason":   r.get("reason", ""), "batch": f"batch_{n:02d}",
            }
    return decisions


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-batches",  type=int, default=16)
    ap.add_argument("--in",         dest="tb_path", default=TIEBREAK_FULL)
    ap.add_argument("--batch-dir",  default=BATCH_DIR)
    ap.add_argument("--result-dir", default=RESULT_DIR)
    ap.add_argument("--dry-run",    action="store_true")
    args = ap.parse_args()

    tiebreak = json.load(open(args.tb_path))
    print(f"tiebreak pairs: {len(tiebreak)}", flush=True)

    decisions = load_decisions(args.n_batches, args.batch_dir, args.result_dir)
    print(f"loaded decisions: {len(decisions)}", flush=True)

    registry = BuildingRegistry()
    print(f"registry before: {registry.stats()}", flush=True)

    counts: Counter = Counter()
    for orig_idx, d in decisions.items():
        if orig_idx >= len(tiebreak):
            continue
        pair = tiebreak[orig_idx]
        src_a, id_a, name_a = pair["source_a"], pair["id_a"], pair["name_a"]

        # Skip if already attached to any canonical
        if (src_a, str(id_a)) in registry._source_index:
            counts["skip_already_registered"] += 1
            continue

        target = registry.follow(pair["cid_b"])
        if target not in registry.data:
            counts["skip_target_missing"] += 1
            continue

        if d["decision"] == "SAME":
            if args.dry_run:
                counts["would_same"] += 1
            else:
                registry.append(target,
                                names={name_a},
                                source_refs={src_a: [id_a]})
                counts["same_appended"] += 1
        else:
            if args.dry_run:
                counts["would_orphan"] += 1
            else:
                # decision #6: All keep — even metalocus DIFFERENT becomes orphan
                registry.match_or_create(
                    names={name_a},
                    source_refs={src_a: [id_a]},
                )
                counts["different_new_orphan"] += 1

    print(f"\napplied counts: {dict(counts)}", flush=True)
    if args.dry_run:
        print("(dry-run — no changes saved)", flush=True)
        return 0
    registry.save()
    print(f"✓ registry saved → {registry.path}", flush=True)
    print(f"registry after: {registry.stats()}", flush=True)
    n = export_clusters(registry, CANONICAL_OUT)
    print(f"✓ canonical clusters: {n} → {CANONICAL_OUT}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
