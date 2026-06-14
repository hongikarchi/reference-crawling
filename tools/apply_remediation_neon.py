#!/usr/bin/env python3
"""Apply 2026-Q2 audit remediation (F1 typology + F3 material) to Neon.

USER-GATED. Default = --dry-run (transaction + ROLLBACK, prints row counts). A live
write requires BOTH --apply and --confirm-db-write. Reads the validated corrections
artifacts produced by tools/fix_typology_primary.py + the material classification.

F1: typology_primary := new, typology_primary_source := 'llm_rederive_2026q2'
    (only rows whose new != old; from typology_corrections_llm.json)
F3: material_visual := new_mv; architectural_elements := union(existing, add_elem)
    (from material_corrections.json)

After a real commit, the material_visual tag axis changed -> rebuild the tag tables:
  python3 tools/canonical_v2_tag_stats_build.py --build --with-r4 --confirm-db-write
(separate user-gated step; not done here).
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

F1 = ROOT / "data/reports/typology_corrections_full.json"  # primary + tags reconciled
F3 = ROOT / "data/reports/material_corrections.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="attempt live write (also needs --confirm-db-write)")
    ap.add_argument("--confirm-db-write", action="store_true")
    args = ap.parse_args()
    live = args.apply and args.confirm_db_write

    f1 = json.loads(F1.read_text())["corrections"]
    f3 = json.loads(F3.read_text())
    report = {"mode": "LIVE-COMMIT" if live else "dry-run(rollback)",
              "f1_typology_updates(primary+tags)": len(f1), "f3_material_updates": len(f3)}

    conn = _connect()
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # F1 — typology_primary + typology_tags (reconciled, primary in tags)
            psycopg2.extras.execute_values(cur, """
                UPDATE canonical_v2_buildings b SET
                  typology_primary = v.newp,
                  typology_tags = v.newtags,
                  typology_primary_source = 'llm_rederive_2026q2'
                FROM (VALUES %s) AS v(id, newp, newtags)
                WHERE b.canonical_bld_id = v.id
                  AND (b.typology_primary IS DISTINCT FROM v.newp
                       OR b.typology_tags IS DISTINCT FROM v.newtags)
            """, [(c["id"], c["new_primary"], c["new_tags"]) for c in f1],
                template="(%s, %s, %s::text[])", page_size=len(f1) + 1)
            report["f1_rows_affected"] = cur.rowcount

            # F3 — material_visual (+ element union)
            psycopg2.extras.execute_values(cur, """
                UPDATE canonical_v2_buildings b SET
                  material_visual = v.mv,
                  architectural_elements = (
                    SELECT array(SELECT DISTINCT unnest(b.architectural_elements || v.ae) ORDER BY 1))
                FROM (VALUES %s) AS v(id, mv, ae)
                WHERE b.canonical_bld_id = v.id
            """, [(c["id"], c["new_mv"], c["add_elem"]) for c in f3],
                template="(%s, %s::text[], %s::text[])", page_size=len(f3) + 1)
            report["f3_rows_affected"] = cur.rowcount

            # post-write QC (inside txn): material noise classified-terms remaining, typ domain
            cur.execute("SELECT count(*) n FROM canonical_v2_buildings WHERE is_publishable AND cardinality(material_visual)=0")
            report["publishable_empty_material_after"] = cur.fetchone()["n"]

        if live:
            conn.commit()
            report["committed"] = True
        else:
            conn.rollback()
            report["committed"] = False
    finally:
        conn.close()

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
