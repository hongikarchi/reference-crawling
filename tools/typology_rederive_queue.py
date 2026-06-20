#!/usr/bin/env python3
"""Build the population typology re-derivation queue from SOURCE DESCRIPTIONS.

F1 (2026-Q2) re-derived typology from source TAGS. The residual ~8-9% error (found by
the accuracy audit) is where tag-derivation failed; the new lever is the source PROSE
description, which F1 never used. This dumps {id, name, stored_typ, program, evidence}
for publishable rows that HAVE a usable description, to feed the source-text judge.
Read-only on Neon.

Usage:
  python3 tools/typology_rederive_queue.py --limit 100 --out /tmp/typ_rederive_smoke.jsonl
  python3 tools/typology_rederive_queue.py --out data/canonical/typ_rederive_pop.jsonl
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
from tools.audit_label_sample import resolve_source_evidence  # noqa: E402
from tools.accuracy_adjq import _evidence  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--only-source-tags", action="store_true",
                    help="restrict to typology_primary_source='source_tags' (F1 leftovers)")
    args = ap.parse_args()

    conn = _connect(); conn.set_session(readonly=True)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    where = "is_publishable AND typology_primary IS NOT NULL"
    if args.only_source_tags:
        where += " AND typology_primary_source = 'source_tags'"
    lim = f" LIMIT {int(args.limit)}" if args.limit else ""
    cur.execute(f"""SELECT canonical_bld_id, name, typology_primary, typology_tags,
                       program, source_refs, source_categories
                    FROM {BLD} WHERE {where} ORDER BY canonical_bld_id{lim}""")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    resolve_source_evidence(rows)
    n_written = n_skip = 0
    with Path(args.out).open("w", encoding="utf-8") as f:
        for r in rows:
            ev = _evidence(r)
            if not ev:
                n_skip += 1
                continue
            f.write(json.dumps({
                "id": r["canonical_bld_id"], "name": r.get("name"),
                "stored_typ": r["typology_primary"], "program": r.get("program"),
                "evidence": ev,
            }, ensure_ascii=False) + "\n")
            n_written += 1
    print(json.dumps({"candidates": len(rows), "written": n_written,
                      "skipped_no_evidence": n_skip, "out": args.out}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
