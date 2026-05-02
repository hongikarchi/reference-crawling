"""Apply Sonnet tiebreak decisions to the architect registry.

For each pair in architect_tiebreak_pairs.json with a decision:
  • SAME       → registry.append(canonical_id_b, names={name_a}, source_refs={source_a:[id_a]})
  • DIFFERENT  → if source_a is metalocus → drop (no orphan)
                 else                     → create new orphan via match_or_create

Result files merged:
  data/canonical/tiebreak_results/batch_pilot_sonnet.json  (100 pairs)
  data/canonical/tiebreak_results/batch_00.json … batch_15.json  (7,650 pairs)
Total: 7,750 (= len(architect_tiebreak_pairs.json)).

Re-exports canonical clusters into architects_canonical.json (overwrites).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

from canonical.match_architects_sequential import export_clusters
from canonical.registry import ArchitectRegistry


PILOT_BATCH      = "data/canonical/tiebreak_batches/batch_pilot.json"
PILOT_RESULT     = "data/canonical/tiebreak_results/batch_pilot_sonnet.json"
TIEBREAK_FULL    = "data/canonical/architect_tiebreak_pairs.json"
BATCH_DIR        = "data/canonical/tiebreak_batches"
RESULT_DIR       = "data/canonical/tiebreak_results"
CANONICAL_OUT    = "data/canonical/architects_canonical.json"


def load_all_decisions(n_batches: int) -> dict[int, dict]:
    """Returns {orig_idx: {decision, type, reason, batch}} keyed by index into
    architect_tiebreak_pairs.json. Pilot + batch_NN are merged."""
    decisions: dict[int, dict] = {}

    # Pilot — input rows reference orig directly
    pilot_in = {p["i"]: p for p in json.load(open(PILOT_BATCH))}
    for r in json.load(open(PILOT_RESULT)):
        i = r["i"]
        if i not in pilot_in:
            print(f"  WARN pilot result idx {i} has no input row", file=sys.stderr)
            continue
        orig = pilot_in[i]["orig"]
        decisions[orig] = {
            "decision": r["decision"], "type": r.get("type", "?"),
            "reason":   r.get("reason", ""), "batch": "pilot",
        }

    # Production batches
    for n in range(n_batches):
        in_path  = os.path.join(BATCH_DIR,  f"batch_{n:02d}.json")
        out_path = os.path.join(RESULT_DIR, f"batch_{n:02d}.json")
        if not os.path.exists(out_path):
            print(f"  WARN missing result file {out_path}", file=sys.stderr)
            continue
        in_rows  = {p["i"]: p for p in json.load(open(in_path))}
        for r in json.load(open(out_path)):
            i = r["i"]
            if i not in in_rows:
                print(f"  WARN batch {n} result idx {i} has no input row", file=sys.stderr)
                continue
            orig = in_rows[i]["orig"]
            decisions[orig] = {
                "decision": r["decision"], "type": r.get("type", "?"),
                "reason":   r.get("reason", ""), "batch": f"batch_{n:02d}",
            }

    return decisions


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-batches", type=int, default=16)
    ap.add_argument("--dry-run", action="store_true",
                    help="Tally counts but don't mutate registry/canonical")
    args = ap.parse_args()

    tiebreak = json.load(open(TIEBREAK_FULL))
    print(f"tiebreak pairs: {len(tiebreak)}", flush=True)

    decisions = load_all_decisions(args.n_batches)
    print(f"loaded decisions: {len(decisions)}", flush=True)

    # Coverage check — every tiebreak entry must have a decision
    missing = [i for i in range(len(tiebreak)) if i not in decisions]
    if missing:
        print(f"  ✗ MISSING {len(missing)} decisions; sample idx: {missing[:10]}",
              file=sys.stderr, flush=True)
        if not args.dry_run:
            return 1

    if args.dry_run:
        # Tally without mutation
        verdict_by_source = Counter()
        type_dist = Counter()
        for orig_idx, d in decisions.items():
            if orig_idx >= len(tiebreak):
                continue
            src = tiebreak[orig_idx]["source_a"]
            verdict_by_source[(src, d["decision"])] += 1
            type_dist[d["type"]] += 1
        print("\nDecisions by (source, verdict):")
        for k, v in sorted(verdict_by_source.items()):
            print(f"  {k[0]:>10}  {k[1]:<10}  {v:>5}")
        print("\nType distribution:")
        for k, v in sorted(type_dist.items()):
            print(f"  {k:>3}  {v:>5}")
        return 0

    # Apply
    registry = ArchitectRegistry()
    print(f"registry before: {registry.stats()}", flush=True)

    counts = Counter()
    for orig_idx, d in decisions.items():
        if orig_idx >= len(tiebreak):
            continue
        pair = tiebreak[orig_idx]
        src_a, id_a, name_a = pair["source_a"], pair["id_a"], pair["name_a"]
        canonical_b = pair["id_b"]   # registry canonical_id

        if d["decision"] == "SAME":
            registry.append(canonical_b,
                            names={name_a},
                            source_refs={src_a: [id_a]})
            counts["same_appended"] += 1
        else:
            if src_a == "metalocus":
                counts["different_dropped_metalocus"] += 1
            else:
                # Create new orphan (same logic as phase_match_against_registry's allow_new path)
                registry.match_or_create(
                    names={name_a},
                    source_refs={src_a: [id_a]},
                )
                counts["different_new_orphan"] += 1

    print(f"\napplied counts: {dict(counts)}", flush=True)
    registry.save()
    print(f"✓ registry saved → {registry.path}", flush=True)
    print(f"registry after: {registry.stats()}", flush=True)

    # Re-export canonical clusters
    n = export_clusters(registry, CANONICAL_OUT)
    print(f"✓ canonical clusters: {n} → {CANONICAL_OUT}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
