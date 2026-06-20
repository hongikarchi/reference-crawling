#!/usr/bin/env python3
"""Apply the 2026-Q2 description-based typology corrections to Neon. USER-GATED.

Default = --dry-run (transaction + ROLLBACK, prints counts + in-txn QC). A live write
requires BOTH --apply and --confirm-db-write. Reads the net-validated corrections
(tools/typology_corrections.py output: {id, old, new}).

Per row: typology_primary := new, typology_primary_source := 'descr_rederive_2026q2',
and typology_tags := DISTINCT(typology_tags || [new]) so the primary∈tags invariant holds
(old primary kept as a tag — conservative; multi-valued axis). Only rows whose new != old.

After a real commit, typology_primary is not a tag axis, but rebuild tag tables anyway to
refresh contradiction-derived stats + architect top_typologies (separate gated steps).
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

CORR = ROOT / "data/canonical/typology_corrections_descr.jsonl"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corrections", default=str(CORR))
    ap.add_argument("--apply", action="store_true", help="attempt live write (also needs --confirm-db-write)")
    ap.add_argument("--confirm-db-write", action="store_true")
    args = ap.parse_args()
    live = args.apply and args.confirm_db_write

    corr = [json.loads(l) for l in open(args.corrections) if l.strip()]
    # safety filters (defense in depth)
    rows = [(c["id"], c["new"]) for c in corr
            if c.get("new") and c["new"] in vocab.TYPOLOGY and c["new"] != c.get("old")]
    report = {"mode": "LIVE-COMMIT" if live else "dry-run(rollback)",
              "corrections_in": len(corr), "corrections_applicable": len(rows)}

    conn = _connect()
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT count(*) n FROM canonical_v2_buildings WHERE is_publishable")
            psycopg2.extras.execute_values(cur, """
                UPDATE canonical_v2_buildings b SET
                  typology_primary = v.newp,
                  typology_primary_source = 'descr_rederive_2026q2',
                  typology_tags = (SELECT array(
                      SELECT DISTINCT unnest(b.typology_tags || ARRAY[v.newp]) ORDER BY 1))
                FROM (VALUES %s) AS v(id, newp)
                WHERE b.canonical_bld_id = v.id
                  AND b.typology_primary IS DISTINCT FROM v.newp
            """, rows, template="(%s, %s)", page_size=len(rows) + 1)
            report["rows_affected"] = cur.rowcount

            # in-txn QC
            cur.execute("""SELECT count(*) n FROM canonical_v2_buildings
                WHERE typology_primary IS NOT NULL AND NOT (typology_primary = ANY(%s))""",
                (sorted(vocab.TYPOLOGY),))
            report["typology_oov_after"] = cur.fetchone()["n"]
            cur.execute("""SELECT count(*) n FROM canonical_v2_buildings
                WHERE typology_primary IS NOT NULL AND NOT (typology_primary = ANY(typology_tags))""")
            report["primary_not_in_tags_after"] = cur.fetchone()["n"]
            cur.execute("""SELECT typology_primary_source, count(*) n FROM canonical_v2_buildings
                WHERE typology_primary_source IS NOT NULL GROUP BY 1 ORDER BY 2 DESC""")
            report["source_breakdown_after"] = [dict(r) for r in cur.fetchall()]

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
