#!/usr/bin/env python3
"""Apply CONFIRMED program corrections to Neon. USER-GATED.

Default = --dry-run (transaction + ROLLBACK, prints counts + in-txn QC). A live write
requires BOTH --apply and --confirm-db-write. Reads program_corrections.jsonl ({id,old,new}).

Per row: program := new, ONLY where program IS DISTINCT FROM new AND old still matches the
stored value (guards against a concurrent change). program is a single controlled axis, so
no tag-array reconciliation; tag tables are rebuilt as a separate gated step afterwards.
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
from core import vocab  # noqa: E402

CORR = ROOT / "data/canonical/program_corrections.jsonl"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corrections", default=str(CORR))
    ap.add_argument("--apply", action="store_true", help="attempt live write (also needs --confirm-db-write)")
    ap.add_argument("--confirm-db-write", action="store_true")
    args = ap.parse_args()
    live = args.apply and args.confirm_db_write

    corr = [json.loads(l) for l in open(args.corrections) if l.strip()]
    rows = [(c["id"], c["old"], c["new"]) for c in corr
            if c.get("new") and c["new"] in vocab.PROGRAM and c["new"] != c.get("old")]
    report = {"mode": "LIVE-COMMIT" if live else "dry-run(rollback)",
              "corrections_in": len(corr), "corrections_applicable": len(rows)}

    conn = _connect()
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            psycopg2.extras.execute_values(cur, """
                UPDATE canonical_v2_buildings b SET program = v.newp
                FROM (VALUES %s) AS v(id, oldp, newp)
                WHERE b.canonical_bld_id = v.id
                  AND b.program IS NOT DISTINCT FROM v.oldp
                  AND b.program IS DISTINCT FROM v.newp
            """, rows, template="(%s, %s, %s)", page_size=len(rows) + 1)
            report["rows_affected"] = cur.rowcount

            # in-txn QC: program OOV must stay 0
            cur.execute("""SELECT count(*) n FROM canonical_v2_buildings
                WHERE program IS NOT NULL AND NOT (program = ANY(%s))""",
                (sorted(vocab.PROGRAM),))
            report["program_oov_after"] = cur.fetchone()["n"]
            cur.execute("""SELECT program, count(*) n FROM canonical_v2_buildings
                WHERE is_publishable GROUP BY 1 ORDER BY 2 DESC""")
            report["program_dist_after"] = [dict(r) for r in cur.fetchall()]

        if live:
            conn.commit(); report["committed"] = True
        else:
            conn.rollback(); report["committed"] = False
    finally:
        conn.close()

    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
