#!/usr/bin/env python3
"""Representative stratified-random sample for the Tier-2 veracity audit (2026-06-19).

UNLIKE audit_label_sample.py (suspect-directed S1-S5, which over-samples errors and
is biased for an accuracy %), this draws an UNBIASED sample for estimating per-axis
veracity accuracy. Strata = identity_source x confidence_tier x era, proportional to
the publishable population, random within each cell. Each row carries canonical fields
+ display_cover_url + pre-resolved source evidence. Read-only on Neon.

The vision judge must see ONLY the image (no stored labels) to stay independent — the
stored labels are kept here for the deterministic compare, NOT shown to the judge.

Usage:
  python3 tools/accuracy_sample.py --n 10   --out data/reports/accuracy_sample.smoke.json
  python3 tools/accuracy_sample.py --n 2000 --out data/reports/accuracy_sample.full.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import psycopg2.extras  # noqa: E402

from tools.canonical_v2_neon_loader import _connect  # noqa: E402
from tools.audit_full_census import BLD  # noqa: E402
from tools.audit_label_sample import CANON_FIELDS, resolve_source_evidence  # noqa: E402

SEED = 0.6191  # distinct from audit_label_sample SEED (0.4242) -> disjoint draws


def build_sample(cur, n: int, seed: float = SEED, exclude: set | None = None):
    cur.execute("SELECT setseed(%s)", (seed,))
    # cell populations over publishable rows. Stored `era` column == ERA_CASE
    # (census era_vs_year mismatch = 0), so use it directly (avoids project_year
    # grouping + alias collision with the real `era` column).
    cur.execute(f"""
        SELECT identity_source AS src, confidence_tier AS tier,
               COALESCE(era, 'era_null') AS era_bucket, count(*) AS pop
        FROM {BLD} WHERE is_publishable
        GROUP BY src, tier, era_bucket ORDER BY pop DESC""")
    cells = [dict(r) for r in cur.fetchall()]
    total = sum(c["pop"] for c in cells)

    # proportional allocation (largest-remainder so the total lands on n)
    raw = [(c, n * c["pop"] / total) for c in cells]
    alloc = {id(c): int(f) for c, f in raw}
    used = sum(alloc.values())
    for c, f in sorted(raw, key=lambda x: (x[1] - int(x[1])), reverse=True):
        if used >= n:
            break
        if alloc[id(c)] < c["pop"]:
            alloc[id(c)] += 1
            used += 1

    sel = ", ".join(CANON_FIELDS)
    rows = []
    for c in cells:
        k = alloc[id(c)]
        if k <= 0:
            continue
        is_null = c["era_bucket"] == "era_null"
        era_pred = "era IS NULL" if is_null else "era = %s"
        params = [c["src"], c["tier"]] + ([] if is_null else [c["era_bucket"]])
        excl_pred = ""
        if exclude:
            excl_pred = " AND canonical_bld_id <> ALL(%s)"
            params = params + [list(exclude)]
        cur.execute(f"""
            SELECT {sel} FROM {BLD}
            WHERE is_publishable AND identity_source = %s AND confidence_tier = %s
              AND {era_pred}{excl_pred}
            ORDER BY random() LIMIT {int(k)}""", tuple(params))
        for r in cur.fetchall():
            d = dict(r)
            d["_stratum"] = "REP"  # fetch tool filters on this
            d["_cell"] = f"{c['src']}|{c['tier']}|{c['era_bucket']}"
            rows.append(d)
    return rows, {"population": total, "cells": len(cells), "sampled": len(rows)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=2000)
    ap.add_argument("--seed", type=float, default=SEED)
    ap.add_argument("--exclude-from", action="append", default=[],
                    help="sample json file(s); their canonical_bld_ids are excluded (disjointness)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    exclude = set()
    for fn in args.exclude_from:
        if Path(fn).exists():
            for r in json.loads(Path(fn).read_text())["rows"]:
                exclude.add(r["canonical_bld_id"])

    conn = _connect()
    conn.set_session(readonly=True, autocommit=False)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        rows, summary = build_sample(cur, args.n, seed=args.seed, exclude=exclude)
    conn.close()
    summary["excluded_ids"] = len(exclude)

    resolve_source_evidence(rows)
    summary["with_cover_url"] = sum(1 for r in rows if r.get("display_cover_url"))
    summary["with_source_evidence"] = sum(1 for r in rows if r.get("_source_evidence"))
    payload = {"summary": summary, "rows": rows}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(payload, indent=2, default=str))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
