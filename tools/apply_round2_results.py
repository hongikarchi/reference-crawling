"""Apply round-2 Sonnet tiebreak decisions to the architect registry.

Round 2 covers the 9,624 truly-new pairs surfaced after extending the
architizer + archello loaders to include project-discovered architects.

Safety guarantees:
  • Pairs whose source_a/id_a is already attached to ANY canonical
    are skipped (the registry already has the right answer; re-applying
    risks moving the source_id to a different canonical).
  • registry.append is idempotent on duplicate names/source_refs.
  • registry.match_or_create checks source_refs overlap first, so
    DIFFERENT decisions on already-attached items are no-ops too.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

from canonical.match_architects_sequential import export_clusters
from canonical.registry import ArchitectRegistry


TIEBREAK_FULL = "data/canonical/architect_tiebreak_pairs.json"
BATCH_DIR     = "data/canonical/tiebreak_batches_r2"
RESULT_DIR    = "data/canonical/tiebreak_results_r2"
CANONICAL_OUT = "data/canonical/architects_canonical.json"


def load_decisions(n_batches: int) -> dict[int, dict]:
    """Returns {orig_idx: {decision, type, reason, batch}}."""
    decisions: dict[int, dict] = {}
    for n in range(n_batches):
        in_path  = os.path.join(BATCH_DIR,  f"batch_{n:02d}.json")
        out_path = os.path.join(RESULT_DIR, f"batch_{n:02d}.json")
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
    ap.add_argument("--n-batches", type=int, default=16)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    tiebreak = json.load(open(TIEBREAK_FULL))
    print(f"tiebreak pairs: {len(tiebreak)}", flush=True)

    decisions = load_decisions(args.n_batches)
    print(f"loaded R2 decisions: {len(decisions)}", flush=True)

    registry = ArchitectRegistry()
    print(f"registry before: {registry.stats()}", flush=True)

    counts = Counter()
    for orig_idx, d in decisions.items():
        if orig_idx >= len(tiebreak):
            continue
        pair = tiebreak[orig_idx]
        src_a, id_a, name_a = pair["source_a"], pair["id_a"], pair["name_a"]

        # Safety: skip if this source_id is already in any canonical
        if (src_a, str(id_a)) in registry._source_index:
            counts["skip_already_registered"] += 1
            continue

        canonical_b = pair["id_b"]
        # Safety: also confirm the target canonical still exists & isn't redirected
        target = registry.follow(canonical_b)
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
            if src_a == "metalocus":
                counts["different_dropped_metalocus"] += 1
            else:
                if args.dry_run:
                    counts["would_orphan"] += 1
                else:
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
