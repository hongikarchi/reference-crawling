#!/usr/bin/env python3
"""Deterministic compare: stored labels vs independent vision labels (no LLM).

For each in-scope axis, classify every sampled row into:
  AGREE        stored == vision (after per-axis synonym normalization)
  DISAGREE     both non-null, differ
  VISION_NULL  vision could not assess from the single image (uninformative -> excluded
               from the accuracy denominator; counted as vision-coverage)
  STORED_NULL  stored is null (a COVERAGE gap, not a veracity error)

Veracity is then estimated on the ASSESSABLE set (both non-null) by an LLM
adjudication pass (accuracy_adjudicate) over samples of BOTH agree and disagree —
agreement-rate alone is biased up by both-wrong-but-agree (advisor catch).

material_visual is multi-valued + advisory: AGREE iff >=1 normalized term overlaps.
scale off-by-one is recorded as NEAR (left for the adjudicator, counted disagree here).

Usage:
  python3 tools/accuracy_compare.py --sample data/reports/accuracy_sample.full.json \
      --vision /tmp/acc_vision/labels.jsonl --out data/reports/accuracy_compare.full.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

AXES = ("scale", "structural_system", "roof_type", "facade_pattern",
        "material_visual", "typology_primary")

SCALE_ORDER = ["XS", "S", "M", "L", "XL"]

# vision free-term -> stored controlled value, per axis (lowercased keys)
STRUCT_SYN = {
    "concrete": "Reinforced Concrete", "reinforced concrete": "Reinforced Concrete",
    "rc": "Reinforced Concrete", "cast-in-place concrete": "Reinforced Concrete",
    "steel": "Steel Frame", "steel frame": "Steel Frame",
    "timber": "Timber Frame", "wood": "Timber Frame", "wood frame": "Timber Frame",
    "mass timber": "Timber Frame", "clt": "Timber Frame",
    "masonry": "Masonry", "brick": "Masonry", "stone": "Masonry",
    "hybrid": "Hybrid", "shell": "Shell/Membrane", "membrane": "Shell/Membrane",
    "earth": "Earth", "rammed earth": "Earth", "adobe": "Earth",
}
# material term normalization (vision + stored both pass through this)
MAT_SYN = {
    "wood": "timber", "timber": "timber", "clt": "timber", "bamboo": "timber",
    "reinforced concrete": "concrete", "concrete": "concrete", "rc": "concrete",
    "glazing": "glass", "glass": "glass",
    "aluminum": "metal", "aluminium": "metal", "steel": "metal", "metal": "metal",
    "copper": "metal", "zinc": "metal", "corten": "metal", "corten steel": "metal",
    "brick": "brick", "brickwork": "brick",
    "stone": "stone", "marble": "stone", "granite": "stone", "limestone": "stone",
    "plaster": "plaster", "render": "plaster", "stucco": "plaster",
    "tile": "tile", "ceramic": "tile", "terracotta": "tile",
}


def _norm_mat(term: str) -> str:
    t = (term or "").strip().lower()
    return MAT_SYN.get(t, t)


def _norm_struct(term):
    if term is None:
        return None
    return STRUCT_SYN.get(str(term).strip().lower(), str(term).strip())


def compare_row(stored: dict, vis: dict) -> dict:
    out = {}
    for axis in AXES:
        sv = stored.get(axis)
        if axis == "material_visual":
            vv = vis.get("materials")
        elif axis == "typology_primary":
            vv = vis.get("typology")
        else:
            vv = vis.get(axis)

        if axis == "material_visual":
            s_terms = {_norm_mat(x) for x in (sv or []) if x and x != "unspecified"}
            v_terms = {_norm_mat(x) for x in (vv or []) if x}
            if not s_terms:
                out[axis] = "STORED_NULL"
            elif not v_terms:
                out[axis] = "VISION_NULL"
            elif s_terms & v_terms:
                out[axis] = "AGREE"
            else:
                out[axis] = "DISAGREE"
            continue

        if axis == "structural_system":
            vv = _norm_struct(vv)

        if sv is None or sv == "":
            out[axis] = "STORED_NULL"
        elif vv is None or vv == "":
            out[axis] = "VISION_NULL"
        elif str(sv) == str(vv):
            out[axis] = "AGREE"
        elif axis == "scale" and sv in SCALE_ORDER and vv in SCALE_ORDER \
                and abs(SCALE_ORDER.index(sv) - SCALE_ORDER.index(vv)) == 1:
            out[axis] = "NEAR"
        else:
            out[axis] = "DISAGREE"
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", required=True)
    ap.add_argument("--vision", required=True, help="jsonl of {id, scale, ...}")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    sample = {r["canonical_bld_id"]: r
              for r in json.loads(Path(args.sample).read_text())["rows"]}
    vision = {}
    for line in Path(args.vision).read_text().splitlines():
        line = line.strip()
        if line:
            r = json.loads(line)
            vision[r["id"]] = r

    per_axis = {a: {"AGREE": 0, "DISAGREE": 0, "NEAR": 0,
                    "VISION_NULL": 0, "STORED_NULL": 0} for a in AXES}
    rows = []
    for bid, vis in vision.items():
        st = sample.get(bid)
        if not st:
            continue
        verdict = compare_row(st, vis)
        for a, v in verdict.items():
            per_axis[a][v] += 1
        rows.append({"id": bid, "cell": st.get("_cell"), "verdict": verdict,
                     "stored": {a: st.get(a) for a in AXES}, "vision": vis})

    # assessable = AGREE + DISAGREE + NEAR (both non-null). naive agreement only.
    summary = {}
    for a, t in per_axis.items():
        assessable = t["AGREE"] + t["DISAGREE"] + t["NEAR"]
        summary[a] = dict(t)
        summary[a]["assessable"] = assessable
        summary[a]["naive_agreement"] = round(t["AGREE"] / assessable, 4) if assessable else None
    out = {"n_vision": len(vision), "n_matched": len(rows),
           "per_axis": summary, "rows": rows}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2, default=str))
    print(json.dumps({a: {"assessable": summary[a]["assessable"],
                          "naive_agreement": summary[a]["naive_agreement"],
                          "vision_null": summary[a]["VISION_NULL"],
                          "stored_null": summary[a]["STORED_NULL"]}
                      for a in AXES}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
