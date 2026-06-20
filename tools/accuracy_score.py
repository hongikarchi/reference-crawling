#!/usr/bin/env python3
"""Tier-2 veracity estimator — turns compare + adjudication into per-axis accuracy.

Agreement-rate is biased UP by both-wrong-but-agree (LLM-vs-LLM collapse the same
ambiguous cases), so accuracy is estimated from an LLM adjudication of samples of BOTH
the AGREE and the DISAGREE sets (advisor catch):

  accuracy = P(agree)*P(correct|agree) + P(disagree)*P(stored-ok|disagree)

where stored-ok = stored_right OR both_acceptable. CI by Monte-Carlo over Beta
posteriors of the two adjudicated rates. Ceiling is NET of expected fix-regression
(F1 proved ~20% of vision-sided fixes regress a fine label):

  fixable  = P(disagree)*P(stored_wrong|disagree)          (loop-addressable)
  ceiling  = accuracy + 0.8*fixable                        (point; range 0.7..0.9)

typology_primary is reported SEPARATELY: vision is weak on use-type, so its veracity
is a wide/uninformative estimate; the defensible typology signal is the deterministic
contradiction_rate (an error FLOOR), passed in via --contradiction-floor.

Adjudication verdict vocab (per {id, axis}):
  agree set    -> 'agree_correct' | 'agree_both_wrong'
  disagree set -> 'stored_right' | 'both_acceptable' | 'stored_wrong' | 'uncertain'

Usage:
  python3 tools/accuracy_score.py --compare X.json --adjudication V.jsonl \
      --contradiction-floor 0.0245 --out data/reports/accuracy_tier2.json
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

random.seed(1234)
DRAWS = 40000
AXES = ("scale", "structural_system", "roof_type", "facade_pattern",
        "material_visual", "typology_primary")


def wilson(k, n, z=1.96):
    if n == 0:
        return (None, None, None)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (round(p, 4), round(c - h, 4), round(c + h, 4))


def beta_draw(k, n):
    # Jeffreys prior Beta(k+0.5, n-k+0.5); if n==0 -> return None marker
    if n == 0:
        return None
    return random.betavariate(k + 0.5, n - k + 0.5)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--compare", required=True)
    ap.add_argument("--adjudication", required=True)
    ap.add_argument("--contradiction-floor", type=float, default=None)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    comp = json.loads(Path(args.compare).read_text())
    per_axis = comp["per_axis"]

    # adjudication verdicts: {id, axis, set, verdict}
    adj = {a: {"agree": [], "disagree": []} for a in AXES}
    for line in Path(args.adjudication).read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        a, s, v = r["axis"], r.get("set"), r["verdict"]
        if a in adj and s in ("agree", "disagree"):
            adj[a][s].append(v)

    out = {}
    for a in AXES:
        t = per_axis[a]
        agree_n = t["AGREE"]
        disagree_n = t["DISAGREE"] + t.get("NEAR", 0)
        assess = agree_n + disagree_n

        ag = adj[a]["agree"]
        dg = adj[a]["disagree"]
        a_correct = sum(1 for v in ag if v == "agree_correct")
        d_storedok = sum(1 for v in dg if v in ("stored_right", "both_acceptable"))
        d_wrong = sum(1 for v in dg if v == "stored_wrong")

        rec = {
            "assessable": assess, "agree": agree_n, "disagree": disagree_n,
            "vision_null": t["VISION_NULL"], "stored_null": t["STORED_NULL"],
            "adj_agree_n": len(ag), "adj_agree_correct": a_correct,
            "adj_disagree_n": len(dg), "adj_disagree_storedok": d_storedok,
            "adj_disagree_stored_wrong": d_wrong,
            "p_agree_correct": wilson(a_correct, len(ag)),
            "p_disagree_storedok": wilson(d_storedok, len(dg)),
        }

        if assess and len(ag) and len(dg):
            w_a = agree_n / assess
            w_d = disagree_n / assess
            accs, fixs = [], []
            for _ in range(DRAWS):
                pa = beta_draw(a_correct, len(ag))
                pd_ok = beta_draw(d_storedok, len(dg))
                pd_wrong = beta_draw(d_wrong, len(dg))
                acc = w_a * pa + w_d * pd_ok
                accs.append(acc)
                fixs.append(w_d * pd_wrong)
            accs.sort(); fixs.sort()
            acc_pt = sum(accs) / len(accs)
            fix_pt = sum(fixs) / len(fixs)
            rec["accuracy"] = round(acc_pt, 4)
            rec["accuracy_ci95"] = [round(accs[int(0.025 * DRAWS)], 4),
                                    round(accs[int(0.975 * DRAWS)], 4)]
            rec["error_rate"] = round(1 - acc_pt, 4)
            rec["fixable"] = round(fix_pt, 4)
            rec["ceiling"] = round(acc_pt + 0.8 * fix_pt, 4)
            rec["ceiling_range"] = [round(acc_pt + 0.7 * fix_pt, 4),
                                    round(acc_pt + 0.9 * fix_pt, 4)]
        out[a] = rec

    if args.contradiction_floor is not None:
        out["typology_primary"]["NOTE"] = (
            "vision veracity is uninformative for use-type; defensible signal = "
            "contradiction_rate error FLOOR")
        out["typology_primary"]["contradiction_error_floor"] = args.contradiction_floor

    result = {"draws": DRAWS, "per_axis": out}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=2, default=str))
    # compact print
    for a in AXES:
        r = out[a]
        print(f"{a:20s} acc={r.get('accuracy')} ci={r.get('accuracy_ci95')} "
              f"ceil={r.get('ceiling')} assess={r['assessable']} "
              f"visNULL={r['vision_null']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
