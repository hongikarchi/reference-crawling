"""Python rule-based pre-resolver for building tiebreak pairs.

Resolves the unambiguous cases deterministically (same country/year/arch
combos and clear DIFFERENT signals), leaving only the genuinely ambiguous
middle for LLM processing.

Output:
  data/canonical/tiebreak_results_buildings/rule_decided.json
    [{"orig": <orig_idx>, "decision": "SAME"|"DIFFERENT", "type": "rule",
      "reason": "<which rule>"}, ...]
  data/canonical/tiebreak_batches_buildings_llm/batch_NN.json
    Only the remaining ambiguous pairs, split into N small batches for LLM.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter

from rapidfuzz import fuzz


TIEBREAK_FULL = "data/canonical/building_tiebreak_pairs.json"
RULE_OUT      = "data/canonical/tiebreak_results_buildings/rule_decided.json"
LLM_BATCH_DIR = "data/canonical/tiebreak_batches_buildings_llm"


def _norm_city(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"[^\w]", "", s.lower())


def _arch_match(a: list, b: list) -> bool:
    if not a or not b:
        return False
    return bool(set(a) & set(b))


def _decide(p: dict) -> tuple[str, str] | None:
    """Returns (decision, reason) if rule-decided, else None."""
    arch_match = _arch_match(p.get("arch_names_a") or [], p.get("arch_names_b") or [])
    country_a, country_b = (p.get("country_a") or "").strip(), (p.get("country_b") or "").strip()
    city_a, city_b = _norm_city(p.get("city_a")), _norm_city(p.get("city_b"))
    year_a, year_b = p.get("year_a"), p.get("year_b")
    typology_a = (p.get("typology_a") or "").lower().strip()
    typology_b = (p.get("typology_b") or "").lower().strip()
    name_a, name_b = p.get("name_a", ""), p.get("name_b", "")
    strict_sim = p.get("strict_sim", 0)
    loose_sim  = p.get("loose_sim", 0)

    # Recompute name similarity if missing (defensive)
    if strict_sim == 0 and name_a and name_b:
        s = fuzz.token_sort_ratio(name_a.lower(), name_b.lower())
        t = fuzz.token_set_ratio(name_a.lower(), name_b.lower())
        strict_sim = min(s, t)
        loose_sim  = max(s, t)

    year_diff: int | None = None
    if year_a is not None and year_b is not None:
        year_diff = abs(year_a - year_b)

    # === STRONG DIFFERENT rules ===
    # Different country (both populated) → DIFFERENT
    if country_a and country_b and country_a != country_b:
        return ("DIFFERENT", "different countries")

    # Big year gap → DIFFERENT (different real-world buildings)
    if year_diff is not None and year_diff >= 5:
        return ("DIFFERENT", f"year_diff={year_diff} >= 5")

    # === STRONG SAME rules ===
    # Best signal: arch + year exact + city + high name sim
    if arch_match and year_diff == 0 and city_a and city_a == city_b and strict_sim >= 80:
        return ("SAME", "arch+year+city+name>=80")

    # arch + year exact + typology + decent name
    if (arch_match and year_diff == 0 and typology_a and typology_a == typology_b
            and strict_sim >= 70):
        return ("SAME", "arch+year+typology+name>=70")

    # Very high name sim + arch match (basically obvious dup)
    if arch_match and strict_sim >= 95:
        return ("SAME", "arch+strict_sim>=95")

    # arch + city + year close (≤1) + decent name
    if (arch_match and city_a and city_a == city_b
            and year_diff is not None and year_diff <= 1
            and strict_sim >= 75):
        return ("SAME", "arch+city+year<=1+name>=75")

    # === MEDIUM DIFFERENT signals ===
    # Different city + different typology + same architect — likely 2 different projects
    if (arch_match and city_a and city_b and city_a != city_b
            and typology_a and typology_b and typology_a != typology_b
            and strict_sim < 80):
        return ("DIFFERENT", "diff city+typology, same arch but low sim")

    # No arch match AND different city AND name not very similar → probably different
    if not arch_match and city_a and city_b and city_a != city_b and strict_sim < 85:
        return ("DIFFERENT", "no arch + diff city + low name")

    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in",  dest="in_path",  default=TIEBREAK_FULL)
    ap.add_argument("--rule-out", default=RULE_OUT)
    ap.add_argument("--llm-batch-dir", default=LLM_BATCH_DIR)
    ap.add_argument("--n-llm-batches", type=int, default=16)
    args = ap.parse_args()

    tiebreak = json.load(open(args.in_path))
    print(f"input pairs: {len(tiebreak)}", flush=True)

    decided: list[dict] = []
    remaining: list[tuple[int, dict]] = []
    counts = Counter()
    rule_breakdown = Counter()

    for orig_idx, p in enumerate(tiebreak):
        result = _decide(p)
        if result is not None:
            decision, reason = result
            decided.append({
                "orig":     orig_idx,
                "decision": decision,
                "type":     "rule",
                "reason":   reason,
            })
            counts[decision] += 1
            rule_breakdown[reason] += 1
        else:
            remaining.append((orig_idx, p))

    print(f"\nrule-decided: {len(decided)}  ({counts['SAME']} SAME, "
          f"{counts['DIFFERENT']} DIFFERENT)", flush=True)
    print(f"remaining for LLM: {len(remaining)}", flush=True)
    print(f"\nrule breakdown:")
    for r, n in rule_breakdown.most_common():
        print(f"  {n:>5}  {r}")

    os.makedirs(os.path.dirname(args.rule_out), exist_ok=True)
    with open(args.rule_out, "w") as f:
        json.dump(decided, f, indent=2, ensure_ascii=False)
    print(f"\n✓ rule decisions → {args.rule_out}", flush=True)

    # Emit LLM batches
    os.makedirs(args.llm_batch_dir, exist_ok=True)
    batches = [[] for _ in range(args.n_llm_batches)]
    for k, (orig_idx, p) in enumerate(remaining):
        entry = dict(p)
        entry["i"]    = k
        entry["orig"] = orig_idx
        batches[k % args.n_llm_batches].append(entry)
    sizes = []
    for n, batch in enumerate(batches):
        path = os.path.join(args.llm_batch_dir, f"batch_{n:02d}.json")
        with open(path, "w") as f:
            json.dump(batch, f, indent=2, ensure_ascii=False)
        sizes.append(len(batch))
    print(f"\n✓ {args.n_llm_batches} LLM batches "
          f"({min(sizes)}-{max(sizes)} pairs each, {sum(sizes)} total) "
          f"→ {args.llm_batch_dir}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
