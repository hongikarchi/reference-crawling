#!/usr/bin/env python3
"""Build the program-contradiction DIAGNOSIS queue (read-only Neon).

The 2026-Q2 typology re-derive made typology more precise; the typology<->program
contradiction count rose because the COARSE program axis now lags. This dumps the
hard-contradiction rows (stored program OUTSIDE the typology's acceptable-program set,
per audit_full_census.TYP_PROGRAM_OK) WITH source-prose evidence, so a judge can split
each into {program_error / map_artifact_defensible / typology_error / ambiguous} and,
for program_error, suggest the correct controlled program.

NOTE: this is DIAGNOSIS only. No data is changed. Re-derive + Neon write are later,
user-gated steps that act ONLY on confirmed program_error rows.

Usage:
  python3 tools/program_diag_queue.py --limit 20 --out /tmp/prog_diag_smoke.jsonl
  python3 tools/program_diag_queue.py --out data/canonical/program_diag_queue.jsonl
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
from tools.audit_full_census import BLD, TYP_PROGRAM_OK  # noqa: E402
from tools.audit_label_sample import resolve_source_evidence  # noqa: E402
from tools.accuracy_adjq import _evidence  # noqa: E402
from core import vocab  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    items = list(TYP_PROGRAM_OK.items())
    cases = " ".join("WHEN typology_primary=%s THEN %s::text[]" for _ in items)
    flat: list = []
    for t, progs in items:
        flat.extend((t, progs))

    conn = _connect(); conn.set_session(readonly=True)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    lim = f" LIMIT {int(args.limit)}" if args.limit else ""
    cur.execute(f"""
        WITH s AS (
          SELECT canonical_bld_id, name, typology_primary, typology_tags,
                 program, source_refs, source_categories,
                 (CASE {cases} ELSE NULL END) AS ok_programs
          FROM {BLD} WHERE is_publishable AND typology_primary IS NOT NULL)
        SELECT * FROM s
        WHERE ok_programs IS NOT NULL AND NOT (program = ANY(ok_programs))
        ORDER BY canonical_bld_id{lim}""", tuple(flat))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    resolve_source_evidence(rows)
    n_written = n_skip = 0
    allowed = sorted(vocab.PROGRAM)
    with Path(args.out).open("w", encoding="utf-8") as f:
        for r in rows:
            ev = _evidence(r)
            if not ev:
                n_skip += 1
                continue
            f.write(json.dumps({
                "id": r["canonical_bld_id"], "name": r.get("name"),
                "stored_typ": r["typology_primary"],
                "stored_program": r.get("program"),
                "acceptable_programs": list(r.get("ok_programs") or []),
                "allowed_programs": allowed,
                "evidence": ev,
            }, ensure_ascii=False) + "\n")
            n_written += 1
    print(json.dumps({"contradiction_rows": len(rows), "written": n_written,
                      "skipped_no_evidence": n_skip, "out": args.out}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
