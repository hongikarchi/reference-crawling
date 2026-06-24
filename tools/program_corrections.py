#!/usr/bin/env python3
"""Collect CONFIRMED program corrections from classify + independent Opus verify.

A program_error candidate is CONFIRMED only if the independent Opus blind A/B verdict
picked the SUGGESTED program (not the stored one, not "equal"). This closes circularity:
the Sonnet classifier proposes, an independent Opus judge disposes. Emits {id, old, new}.

Inputs:
  data/canonical/program_diag_classify.json   (Sonnet classify: id, category, suggested_program)
  data/canonical/program_verify_map.json       (A/B -> stored/suggested reverse map)
  data/canonical/program_verify_verdicts.json  (Opus: {verdicts:[{id, better, why}]})
Output:
  data/canonical/program_corrections.jsonl     ({id, old, new, why})
Also prints a net-validation summary (confirmed / rejected / equal) for the user gate.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import vocab  # noqa: E402

CLASSIFY = ROOT / "data/canonical/program_diag_classify.json"
MAP = ROOT / "data/canonical/program_verify_map.json"
VERDICTS = ROOT / "data/canonical/program_verify_verdicts.json"
OUT = ROOT / "data/canonical/program_corrections.jsonl"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    classify = json.loads(CLASSIFY.read_text())
    rows = classify.get("results", classify) if isinstance(classify, dict) else classify
    mapping = json.loads(MAP.read_text())
    vraw = json.loads(VERDICTS.read_text())
    verdicts = {v["id"]: v for v in (vraw.get("verdicts", vraw) if isinstance(vraw, dict) else vraw)}

    confirmed, rejected, equal, missing = [], [], [], []
    for r in rows:
        if r.get("category") != "program_error" or not r.get("suggested_program"):
            continue
        bid = r["id"]
        m = mapping.get(bid)
        v = verdicts.get(bid)
        if not m or not v:
            missing.append(bid)
            continue
        better = v["better"]
        picked = m.get(better) if better in ("A", "B") else None
        if better == "equal":
            equal.append(bid)
        elif picked == m["suggested"] and m["suggested"] != m["stored"]:
            if m["suggested"] in vocab.PROGRAM:  # defense in depth
                confirmed.append({"id": bid, "old": m["stored"], "new": m["suggested"],
                                  "why": v.get("why", "")})
            else:
                rejected.append(bid)
        else:
            rejected.append(bid)

    with Path(args.out).open("w", encoding="utf-8") as f:
        for c in confirmed:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    n_err = sum(1 for r in rows if r.get("category") == "program_error")
    summary = {
        "program_error_candidates": n_err,
        "confirmed": len(confirmed),
        "rejected_kept_stored": len(rejected),
        "equal_kept_stored": len(equal),
        "missing_verdict": len(missing),
        "precision_confirmed_pct": round(100 * len(confirmed) / max(1, len(confirmed) + len(rejected) + len(equal)), 1),
        "out": args.out,
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
