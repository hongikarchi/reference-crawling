#!/usr/bin/env python3
"""Refresh architects' top_programs from LIVE Neon after the 2026-Q2 program re-derive.

The 223 program corrections (contradiction re-derive) shift a handful of architects'
top_programs aggregate. The full architects build reads a stale local artifact, so
recompute this one column directly from live post-correction buildings. Mirror of
refresh_architect_typologies.py (recommendation-neutral: portfolio_embedding unchanged).

USER-GATED: default dry-run (rollback); --confirm-db-write commits. top-5 program by
frequency over ALL the architect's buildings — MATCHING canonical_v2_architects_build.py
(its singular-field counters are NOT publishability-gated). This isolates the refresh to
exactly the 223 program corrections instead of also flipping the build's all-buildings
semantics to publishable-only.
"""
from __future__ import annotations
import argparse, json, sys
from collections import Counter
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import psycopg2.extras  # noqa: E402
from tools.canonical_v2_neon_loader import _connect  # noqa: E402

TOPK = 5


def top_k(counter):
    return [t for t, _ in counter.most_common(TOPK)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm-db-write", action="store_true")
    args = ap.parse_args()
    conn = _connect(); conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SET statement_timeout=120000")
    cur.execute("SELECT canonical_bld_id id, program FROM canonical_v2_buildings")
    bld = {r["id"]: r for r in cur.fetchall()}
    cur.execute("SELECT canonical_arch_id id, building_ids bids, top_programs tp FROM canonical_v2_architects")
    archs = cur.fetchall()

    updates = []
    for a in archs:
        prog = Counter()
        for bid in a["bids"] or []:
            b = bld.get(bid)
            if not b or not b["program"]:  # all buildings, matching the build (not pub-gated)
                continue
            prog[b["program"]] += 1
        new_tp = top_k(prog)
        if new_tp != (a["tp"] or []):
            updates.append((a["id"], new_tp))

    if updates:
        psycopg2.extras.execute_values(cur, """
            UPDATE canonical_v2_architects a SET top_programs = v.tp
            FROM (VALUES %s) AS v(id, tp)
            WHERE a.canonical_arch_id = v.id
        """, updates, template="(%s, %s::text[])", page_size=len(updates) + 1)
        affected = cur.rowcount
    else:
        affected = 0

    committed = False
    if args.confirm_db_write:
        conn.commit(); committed = True
    else:
        conn.rollback()
    conn.close()
    print(json.dumps({"mode": "commit" if committed else "dry-run/rollback",
                      "architects": len(archs), "changed": len(updates),
                      "rows_affected": affected, "committed": committed}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
