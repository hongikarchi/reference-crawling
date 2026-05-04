"""Unified apply: merge rule_decided + LLM batch results, apply to building registry.

Sources:
  data/canonical/tiebreak_results_buildings/rule_decided.json
    [{"orig": int, "decision": "SAME"|"DIFFERENT", "type": "rule", "reason": str}, ...]

  data/canonical/tiebreak_batches_buildings_llm/batch_NN.json
  data/canonical/tiebreak_results_buildings/batch_llm_NN.json
    LLM batches: each result has 'i' which maps via batch input's 'orig'.

For each tiebreak pair with a decision:
  • SAME → registry.append(cid_b, names, source_refs)
  • DIFFERENT → match_or_create (creates orphan, idempotent if already attached)

Skip pairs with no decision (they remain as Pass-1 orphans — no harm).
Skip pairs where source_a/id_a is already attached to any canonical (safety).
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
RULE_DECIDED  = "data/canonical/tiebreak_results_buildings/rule_decided.json"
LLM_BATCH_DIR = "data/canonical/tiebreak_batches_buildings_llm"
LLM_RESULT_DIR = "data/canonical/tiebreak_results_buildings"
CANONICAL_OUT = "data/canonical/buildings_canonical.json"


def load_all_decisions() -> dict[int, dict]:
    """Returns {orig_idx: {decision, type, reason, source}}."""
    decisions: dict[int, dict] = {}

    # 1. Rule-based
    if os.path.exists(RULE_DECIDED):
        for r in json.load(open(RULE_DECIDED)):
            decisions[r["orig"]] = {**r, "source": "rule"}
        print(f"  rule_decided: {len(decisions)}", flush=True)

    # 2. LLM batches
    n_llm = 0
    for fn in sorted(os.listdir(LLM_BATCH_DIR)):
        if not (fn.startswith("batch_") and fn.endswith(".json")):
            continue
        in_path = os.path.join(LLM_BATCH_DIR, fn)
        # Output path mirrors LLM_RESULT_DIR/batch_llm_NN.json
        out_path = os.path.join(LLM_RESULT_DIR, fn.replace("batch_", "batch_llm_"))
        if not os.path.exists(out_path):
            continue
        in_rows = {p["i"]: p for p in json.load(open(in_path))}
        for r in json.load(open(out_path)):
            i = r["i"]
            if i not in in_rows:
                continue
            orig = in_rows[i]["orig"]
            # LLM overrides rule? No — rule is for clear-cut cases. Don't override.
            if orig in decisions:
                continue
            decisions[orig] = {**r, "orig": orig, "source": "llm"}
            n_llm += 1
    print(f"  llm: +{n_llm} (total decisions: {len(decisions)})", flush=True)
    return decisions


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    tiebreak = json.load(open(TIEBREAK_FULL))
    print(f"tiebreak total: {len(tiebreak)}", flush=True)

    decisions = load_all_decisions()
    coverage = len(decisions) / len(tiebreak)
    print(f"coverage: {coverage:.1%} ({len(decisions)}/{len(tiebreak)})", flush=True)

    registry = BuildingRegistry()
    print(f"registry before: {registry.stats()}", flush=True)

    counts: Counter = Counter()
    for orig_idx, d in decisions.items():
        if orig_idx >= len(tiebreak):
            continue
        pair = tiebreak[orig_idx]
        src_a, id_a, name_a = pair["source_a"], pair["id_a"], pair["name_a"]

        # Safety: skip already-attached
        if (src_a, str(id_a)) in registry._source_index:
            counts["skip_already_registered"] += 1
            continue

        target = registry.follow(pair["cid_b"])
        if target not in registry.data:
            counts["skip_target_missing"] += 1
            continue

        if d["decision"] == "SAME":
            if args.dry_run:
                counts[f"would_same_{d['source']}"] += 1
            else:
                registry.append(target,
                                names={name_a},
                                source_refs={src_a: [id_a]})
                counts[f"same_{d['source']}"] += 1
        else:
            if args.dry_run:
                counts[f"would_orphan_{d['source']}"] += 1
            else:
                registry.match_or_create(
                    names={name_a},
                    source_refs={src_a: [id_a]},
                )
                counts[f"orphan_{d['source']}"] += 1

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
