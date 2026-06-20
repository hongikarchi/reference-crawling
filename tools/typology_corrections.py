#!/usr/bin/env python3
"""Collect typology corrections from the population re-derive verdicts (pure Python).

Reads rd_b*.jsonl (the Sonnet source-text re-derive output), keeps verdict=='stored_wrong'
as candidate corrections {id, old, new}. Double-guards the Pavilion failure mode (never
replace a real use-type with 'Pavilion') and drops non-vocab / no-op corrections. The
kept set is then net-validated by Opus blind A/B before any apply.

Usage:
  python3 tools/typology_corrections.py --rd-dir /tmp/acc_adj/rd \
      --out data/canonical/typology_corrections_descr.jsonl
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import vocab  # noqa: E402

REAL_USE = vocab.TYPOLOGY - {"Pavilion"}  # never auto-replace a real use with Pavilion


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rd-dir", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    verdicts = {}
    nfiles = 0
    for f in sorted(glob.glob(f"{args.rd_dir}/rd_b*.jsonl")):
        nfiles += 1
        for ln in open(f):
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
                verdicts[r["id"]] = r
            except Exception:
                pass

    from collections import Counter
    dist = Counter(v.get("verdict") for v in verdicts.values())
    corr, dropped = [], Counter()
    for i, v in verdicts.items():
        if v.get("verdict") != "stored_wrong":
            continue
        old, new = v.get("stored_typ"), v.get("indep_typ")
        if not new or new == old:
            dropped["noop_or_missing"] += 1
            continue
        if new not in vocab.TYPOLOGY:
            dropped["new_oov"] += 1
            continue
        if new == "Pavilion" and old in REAL_USE:
            dropped["pavilion_guard"] += 1
            continue
        corr.append({"id": i, "old": old, "new": new})

    with Path(args.out).open("w", encoding="utf-8") as f:
        for c in corr:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    summ = {"rd_files": nfiles, "rows_judged": len(verdicts),
            "verdict_dist": dict(dist), "corrections_kept": len(corr),
            "dropped": dict(dropped),
            "correction_rate": round(len(corr) / len(verdicts), 4) if verdicts else None}
    print(json.dumps(summ, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
