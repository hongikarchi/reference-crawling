#!/usr/bin/env python3
"""Fix OOV typology values that the F1 LLM re-derivation introduced (~23 rows).

The LLM occasionally returned program-vocab ("Hospitality","Healthcare","Infrastructure")
or invented values ("Exhibition Centre","Public Building","Studio","Bus Station",
"Cultural Center","Monument","Research Building","Transport Infrastructure") that are not
in the 35-value TYPOLOGY vocab. Remap to the nearest vocab value, or NULL when ambiguous.

USER-GATED: default dry-run (rollback); --confirm-db-write commits. Fixes both
typology_primary and typology_tags. Verifies 0 OOV inside the txn before committing.
"""
from __future__ import annotations
import argparse, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.canonical_v2_neon_loader import _connect  # noqa: E402
from core import vocab  # noqa: E402

REMAP = {
    "Hospitality": None, "Infrastructure": None, "Transport Infrastructure": None,
    "Exhibition Centre": "Gallery", "Public Building": "Civic Building", "Studio": "Office",
    "Bus Station": "Train Station", "Research Building": "University", "Monument": "Memorial",
    "Cultural Center": "Civic Building", "Healthcare": "Hospital",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm-db-write", action="store_true")
    args = ap.parse_args()
    allowed = set(vocab.TYPOLOGY)
    conn = _connect(); conn.autocommit = False
    cur = conn.cursor()
    cur.execute("SET statement_timeout=60000")
    n_p = 0
    for oov, new in REMAP.items():
        if new is None:
            cur.execute("UPDATE canonical_v2_buildings SET typology_primary=NULL WHERE typology_primary=%s", (oov,))
        else:
            cur.execute("UPDATE canonical_v2_buildings SET typology_primary=%s WHERE typology_primary=%s", (new, oov))
        n_p += cur.rowcount
    cur.execute("SELECT canonical_bld_id, typology_tags FROM canonical_v2_buildings "
                "WHERE EXISTS(SELECT 1 FROM unnest(typology_tags) t WHERE NOT(t=ANY(%s)))", (sorted(allowed),))
    rows = cur.fetchall(); n_t = len(rows)
    for bid, tags in rows:
        newt = set(t if t in allowed else REMAP.get(t) for t in tags)
        newt.discard(None)
        cur.execute("UPDATE canonical_v2_buildings SET typology_tags=%s WHERE canonical_bld_id=%s", (sorted(newt), bid))
    cur.execute("SELECT count(*) FROM (SELECT DISTINCT unnest(typology_tags) t FROM canonical_v2_buildings) x WHERE NOT(t=ANY(%s))", (sorted(allowed),))
    tag_oov = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM canonical_v2_buildings WHERE typology_primary IS NOT NULL AND NOT(typology_primary=ANY(%s))", (sorted(allowed),))
    prim_oov = cur.fetchone()[0]
    clean = (tag_oov == 0 and prim_oov == 0)
    committed = False
    if args.confirm_db_write and clean:
        conn.commit(); committed = True
    else:
        conn.rollback()
    conn.close()
    import json
    print(json.dumps({"mode": "commit" if committed else "dry-run/rollback",
                      "primary_remapped": n_p, "tag_rows_fixed": n_t,
                      "after_prim_oov": prim_oov, "after_tag_oov": tag_oov,
                      "committed": committed}, indent=2))
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
