#!/usr/bin/env python3
"""C8: refresh architects' top_typologies + top_arch_elements from LIVE Neon.

After the F1/F3 remediation changed building typology_tags + architectural_elements,
the architects' derived top-5 aggregates are stale. The full architects build reads the
stale C23 local artifact, so instead recompute these two columns directly from live
post-remediation buildings (recommendation-neutral: portfolio_embedding is unchanged).

USER-GATED: default dry-run (rollback); --confirm-db-write commits. In-memory join
(14k architects x 39k buildings), top-5 by frequency over PUBLISHABLE buildings.
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
    cur.execute("SELECT canonical_bld_id id, is_publishable pub, typology_tags tt, architectural_elements ae FROM canonical_v2_buildings")
    bld = {r["id"]: r for r in cur.fetchall()}
    cur.execute("SELECT canonical_arch_id id, building_ids bids, top_typologies tt, top_arch_elements ae FROM canonical_v2_architects")
    archs = cur.fetchall()

    updates = []
    for a in archs:
        typ, elem = Counter(), Counter()
        for bid in a["bids"] or []:
            b = bld.get(bid)
            if not b or not b["pub"]:
                continue
            for t in b["tt"] or []:
                typ[t] += 1
            for e in b["ae"] or []:
                elem[e] += 1
        new_tt, new_ae = top_k(typ), top_k(elem)
        if new_tt != (a["tt"] or []) or new_ae != (a["ae"] or []):
            updates.append((a["id"], new_tt, new_ae))

    if updates:
        psycopg2.extras.execute_values(cur, """
            UPDATE canonical_v2_architects a SET
              top_typologies = v.tt, top_arch_elements = v.ae
            FROM (VALUES %s) AS v(id, tt, ae)
            WHERE a.canonical_arch_id = v.id
        """, updates, template="(%s, %s::text[], %s::text[])", page_size=len(updates) + 1)
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
