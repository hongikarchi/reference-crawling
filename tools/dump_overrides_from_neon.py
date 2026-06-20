#!/usr/bin/env python3
"""Dump the authoritative per-id override columns from LIVE Neon (read-only).

Regenerates remediation_overrides_rem2026q2.jsonl from current production state so the
artifact sync (apply_overrides_to_artifact.py) bakes ALL applied remediations — including
the 2026-Q2 description-based typology corrections — into the c26 artifact. Without this,
a re-upsert from c26 would silently revert the new typology corrections.
"""
from __future__ import annotations
import json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import psycopg2.extras  # noqa: E402
from tools.canonical_v2_neon_loader import _connect  # noqa: E402

OUT = ROOT / "data/canonical/remediation_overrides_rem2026q2.jsonl"
COLS = ("typology_primary", "typology_primary_source", "typology_tags",
        "material_visual", "architectural_elements",
        "is_publishable", "publishability_reasons")


def main() -> int:
    conn = _connect(); conn.set_session(readonly=True)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(f"SELECT canonical_bld_id, {', '.join(COLS)} FROM canonical_v2_buildings")
    n = 0
    with OUT.open("w", encoding="utf-8") as f:
        for r in cur.fetchall():
            rec = {"id": r["canonical_bld_id"]}
            for c in COLS:
                rec[c] = r[c]
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    conn.close()
    print(json.dumps({"rows": n, "out": str(OUT)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
