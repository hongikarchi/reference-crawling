#!/usr/bin/env python3
"""Build a blind A/B queue to net-validate typology corrections with an INDEPENDENT
judge (Opus), closing the circularity in source-text re-derive (baseline judge == fixer).

For each correction {id, old, new}: present old/new in RANDOM order (optA/optB), with the
building's source evidence + name + program + cover image, blind to which is incumbent.
Opus later picks which fits better; we map back to old-better / new-better / tie. Read-only.

Usage:
  python3 tools/typology_ab_build.py --corrections C.jsonl --evidence POP.jsonl \
      --n 60 --seed 7 --out-queue Q.jsonl --out-fetch F.json
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import psycopg2.extras  # noqa: E402
from tools.canonical_v2_neon_loader import _connect  # noqa: E402
from tools.audit_full_census import BLD  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corrections", required=True)
    ap.add_argument("--evidence", required=True, help="pop queue jsonl with id->evidence/name/program")
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--ids-exclude", default=None, help="jsonl/txt of ids to exclude (disjointness)")
    ap.add_argument("--out-queue", required=True)
    ap.add_argument("--out-fetch", required=True)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    corr = [json.loads(l) for l in open(args.corrections) if l.strip()]
    ev = {}
    for l in open(args.evidence):
        l = l.strip()
        if l:
            r = json.loads(l)
            ev[r["id"]] = r
    exclude = set()
    if args.ids_exclude and Path(args.ids_exclude).exists():
        for l in open(args.ids_exclude):
            l = l.strip()
            if l:
                exclude.add(l.split()[0])

    corr = [c for c in corr if c["id"] in ev and c["id"] not in exclude]
    rng.shuffle(corr)
    corr = corr[: args.n]

    ids = [c["id"] for c in corr]
    conn = _connect(); conn.set_session(readonly=True)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(f"SELECT canonical_bld_id, display_cover_url FROM {BLD} WHERE canonical_bld_id = ANY(%s)", (ids,))
    cover = {r["canonical_bld_id"]: r["display_cover_url"] for r in cur.fetchall()}
    conn.close()

    items, fetch_rows = [], []
    for c in corr:
        e = ev[c["id"]]
        flip = rng.random() < 0.5
        A, B = (c["old"], c["new"]) if flip else (c["new"], c["old"])
        items.append({"id": c["id"], "name": e.get("name"), "program": e.get("program"),
                      "evidence": e.get("evidence"), "optA": A, "optB": B,
                      "_old": c["old"], "_new": c["new"], "_A_is": "old" if flip else "new"})
        fetch_rows.append({"canonical_bld_id": c["id"],
                           "display_cover_url": cover.get(c["id"]), "_stratum": "AB"})

    Path(args.out_queue).write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in items))
    Path(args.out_fetch).write_text(json.dumps({"rows": fetch_rows}))
    print(json.dumps({"corrections_in": len(corr), "queue": len(items),
                      "with_cover": sum(1 for r in fetch_rows if r['display_cover_url'])}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
