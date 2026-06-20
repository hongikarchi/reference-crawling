#!/usr/bin/env python3
"""Tier-1 conformance scorecard — the deterministic, 100%-coverage, FREE metric vector.

Reduces the audit_full_census sections to a compact quantified vector the accuracy
loop tracks across rounds. These are GATES / necessary conditions, NOT the accuracy
number (that is Tier-2 veracity, see accuracy_score.py). Read-only on live Neon.

Vector:
  oov_total              sum of out-of-vocab values across all 11 controlled axes (target 0)
  invariant_violations   pk dup + dangling refs (both dirs) + tag (axis,tag) parity x4
                         + idf-formula + centroid-norm + embedding bad/zero
                         + typology_primary NOT in typology_tags  (target 0)
  contradiction_rate     hard typology<->program contradictions / checkable  (typology error FLOOR)
  vagueness_index        mean generic-or-NULL discriminative axes per publishable row / 9
  catch_all              per-axis catch-all share among publishable
  year_kind_drift        year_kind != recomputed rule (time-drift, deterministic-fixable)

Usage:
  python3 tools/accuracy_metrics.py --out data/reports/accuracy_tier1.json
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
from tools.audit_full_census import (  # noqa: E402
    BLD, section_l1, section_l2, section_l3, section_l6, section_l7a, section_l7b, _one,
)


def reduce_vector(cur) -> dict:
    l1 = section_l1(cur)
    l2 = section_l2(cur)
    l3 = section_l3(cur)
    l6 = section_l6(cur)
    l7a = section_l7a(cur)
    l7b = section_l7b(cur)

    # OOV total across controlled axes
    oov_total = 0
    for k, v in l2.items():
        if isinstance(v, dict) and "oov" in v:
            oov_total += sum(r["n"] for r in v["oov"])

    # primary NOT in tags
    prim_not_tag = _one(cur, f"""
        SELECT count(*) n FROM {BLD}
        WHERE typology_primary IS NOT NULL
          AND NOT (typology_primary = ANY(typology_tags))""")["n"]

    pk = l1["pk"]
    emb = l1["embedding"]
    tt = l3["tag_tables"]
    par = tt["key_parity"]
    inv = {
        "pk_dup": pk["dup_pk"],
        "building_to_architect_dangling": l3["building_to_architect"]["dangling"],
        "architect_to_building_dangling": l3["architect_to_building"]["dangling"],
        "tag_parity": sum(par.values()),
        "idf_formula": tt["idf_formula_violations"]["n"],
        "centroid_not_normalized": tt["centroid_norm"]["not_normalized"],
        "centroid_zero": tt["centroid_norm"]["zero_vec"],
        "embedding_bad_dim": emb["bad_dim"],
        "embedding_zero": emb["zero_vec"],
        "primary_not_in_tags": prim_not_tag,
    }
    invariant_violations = sum(inv.values())

    contr = l7a["typology_program_contradiction"]
    cab = l7b["catch_all_rates"]
    info = l7b["info_score"]
    pub = cab["total"]

    return {
        "counts": l1["pk"] | {"publishable": pub},
        "oov_total": oov_total,
        "invariant_violations": invariant_violations,
        "invariant_detail": inv,
        "contradiction_rate": round(contr["hard_contradictions"] / contr["checkable"], 4),
        "contradiction_n": contr["hard_contradictions"],
        "contradiction_checkable": contr["checkable"],
        "vagueness_index": round(float(info["avg_generic_axes"]) / 9.0, 4),
        "avg_generic_axes": float(info["avg_generic_axes"]),
        "rows_5plus_generic": info["rows_5plus_generic"],
        "catch_all": {
            "program_other": round(cab["program_other"] / pub, 4),
            "style_contemporary": round(cab["style_contemporary"] / pub, 4),
            "typology_mixed": round(cab["typ_mixed"] / pub, 4),
            "typology_null": round(cab["typ_null"] / pub, 4),
            "material_empty_or_unspec": round(cab["material_empty_or_unspec"] / pub, 4),
        },
        "year_kind_drift": l6["year_kind_vs_year"]["mismatch"],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    conn = _connect()
    conn.set_session(readonly=True, autocommit=False)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        vec = reduce_vector(cur)
    conn.close()
    text = json.dumps(vec, indent=2, default=str)
    print(text)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
