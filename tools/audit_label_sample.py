#!/usr/bin/env python3
"""Build the suspect-directed sample for the paid label-correctness audit.

Per the 2026-Q2 plan + advisor restructure: do NOT draw a flat demographic-random
sample. Build strata that route each candidate class to the verifier that can
actually adjudicate it:

  S1_typ_prog   typology<->program HARD contradictions (source_tags side) -> SOURCE
  S2_style      style='Contemporary' overuse                              -> VISION
  S3_r4_visual  R4 visual axes (roof/facade/structural/material)          -> VISION
  S4_identity   leaked-name / name_needs_review survivors                 -> SOURCE/WEB
  S5_baseline   random all-looks-fine publishable rows (base-rate)        -> VISION+SOURCE

Rows are assigned to ONE stratum by priority (S4>S1>S2>S3>S5) so the sample is a
partition. Each row carries canonical fields + display_cover_url + pre-resolved
source evidence (from data/crawl/*.db via source_refs) so the text verifier reads
ready-made evidence. Read-only.

Usage:
  python3 tools/audit_label_sample.py --per-stratum 10  --out data/reports/audit_label_sample.smoke.json
  python3 tools/audit_label_sample.py --full            --out data/reports/audit_label_sample.full.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import psycopg2.extras  # noqa: E402

from tools.canonical_v2_neon_loader import _connect  # noqa: E402
from tools.audit_full_census import TYP_PROGRAM_OK, BLD  # noqa: E402

FULL_SIZES = {"S4_identity": 120, "S1_typ_prog": 400, "S2_style": 400,
              "S3_r4_visual": 300, "S5_baseline": 400}

SEED = 0.4242

# source -> (db file, table, id col, fields to pull as evidence)
CRAWL = {
    "divisare": ("divisare", "divisare_projects", "id",
                 ["name", "project_year", "tag_slugs", "abstract", "description"]),
    "architizer": ("architizer", "architizer_projects", "id",
                   ["name", "completion_year", "categories", "description_short", "description"]),
    "archello": ("archello", "archello_projects", "id",
                 ["name", "project_year", "category", "description"]),
    "metalocus": ("metalocus", "buildings", "id",
                  ["title", "year", "building_type", "materials", "description"]),
}

CANON_FIELDS = [
    "canonical_bld_id", "name", "program", "style", "color_tone", "atmosphere",
    "typology_primary", "typology_primary_source", "typology_tags",
    "material_visual", "structural_system", "roof_type", "facade_pattern", "scale",
    "era", "project_year", "architect_names", "location_country", "location_city",
    "visual_description", "display_cover_url", "source_refs", "source_categories",
]


def _typ_prog_case():
    items = list(TYP_PROGRAM_OK.items())
    cases = " ".join("WHEN typology_primary = %s THEN %s::text[]" for _ in items)
    flat = []
    for t, ok in items:
        flat += [t, ok]
    return cases, flat


def build_strata(cur, per_stratum, full):
    cases, flat = _typ_prog_case()
    sel = ", ".join(CANON_FIELDS)
    cur.execute("SELECT setseed(%s)", (SEED,))

    # candidate WHERE clauses per stratum (all over publishable rows)
    contradiction = f"""typology_primary_source = 'source_tags'
        AND (CASE {cases} ELSE NULL END) IS NOT NULL
        AND NOT (program = ANY(CASE {cases} ELSE NULL END))"""
    leaked = r"""(name ~ '^[A-Z][a-z]+( [A-Z][a-z]+)? - [A-Z]' AND length(name) < 30)
        OR 'name_needs_review' = ANY(publishability_reasons)"""
    strata = {
        "S4_identity": leaked,
        "S1_typ_prog": contradiction,
        "S2_style": "style = 'Contemporary'",
        "S3_r4_visual": "(roof_type IS NOT NULL OR facade_pattern IS NOT NULL OR structural_system IS NOT NULL)",
        "S5_baseline": "TRUE",
    }
    priority = ["S4_identity", "S1_typ_prog", "S2_style", "S3_r4_visual", "S5_baseline"]

    taken: set[str] = set()
    out: dict[str, list] = {}
    for name in priority:
        size = per_stratum if not full else FULL_SIZES[name]
        where = strata[name]
        # contradiction/leaked need the flat params (cases used twice in contradiction)
        params = []
        if name == "S1_typ_prog":
            params = flat + flat
        excl = ""
        if taken:
            excl = " AND canonical_bld_id <> ALL(%s)"
        sql = f"""SELECT {sel} FROM {BLD}
            WHERE is_publishable AND ({where}){excl}
            ORDER BY random() LIMIT {int(size)}"""
        args = tuple(params) + ((list(taken),) if taken else ())
        cur.execute(sql, args)
        rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            r["_stratum"] = name
            taken.add(r["canonical_bld_id"])
        out[name] = rows
    return out


def resolve_source_evidence(rows):
    conns = {}
    for src, (dbname, *_rest) in CRAWL.items():
        p = ROOT / "data" / "crawl" / f"{dbname}.db"
        if p.exists():
            conns[src] = sqlite3.connect(str(p))
    try:
        for r in rows:
            refs = r.get("source_refs") or {}
            ev = {}
            for src, ids in refs.items():
                if src not in CRAWL or src not in conns or not ids:
                    continue
                _db, tbl, idcol, fields = CRAWL[src]
                cur = conns[src].cursor()
                sid = str(ids[0])
                cur.execute(
                    f"SELECT {', '.join(fields)} FROM {tbl} WHERE {idcol} = ? LIMIT 1",
                    (sid,))
                row = cur.fetchone()
                if row:
                    d = {}
                    for k, v in zip(fields, row):
                        if v is None:
                            continue
                        s = str(v)
                        d[k] = s[:700] if k in ("description", "abstract") else s[:200]
                    ev[src] = d
            r["_source_evidence"] = ev
    finally:
        for c in conns.values():
            c.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-stratum", type=int, default=10)
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    conn = _connect()
    conn.set_session(readonly=True, autocommit=False)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        strata = build_strata(cur, args.per_stratum, args.full)
    conn.close()

    all_rows = [r for rows in strata.values() for r in rows]
    resolve_source_evidence(all_rows)

    summary = {name: len(rows) for name, rows in strata.items()}
    summary["total"] = len(all_rows)
    summary["with_source_evidence"] = sum(1 for r in all_rows if r.get("_source_evidence"))
    summary["with_cover_url"] = sum(1 for r in all_rows if r.get("display_cover_url"))
    payload = {"summary": summary, "rows": all_rows}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(payload, indent=2, default=str))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
