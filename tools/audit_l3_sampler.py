#!/usr/bin/env python3
"""L3 audit — stratified sampler.

Read-only. Draws a ~3,800-building sample across 8 disjoint primary strata
from Neon canonical_v2_buildings (= completeness_c8), deterministic by a
recorded seed. Writes data/reports/audit/L3_sample_manifest.json.

Strata are priority-assigned so each building belongs to exactly one — clean
population projection. Priority (high to low):
  flagged > image_derived_oov > has_gaps > T1 > T2 > T3_<source>
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.canonical_v2_neon_loader import _connect, TABLE  # noqa: E402
from core import vocab  # noqa: E402

L4L5 = ROOT / "data/reports/audit/L4L5.json"
MANIFEST = ROOT / "data/reports/audit/L3_sample_manifest.json"
SEED = "make_db-audit-2026-05"

# None => census the whole stratum; int => deterministic sample of that size
TARGETS = {
    "flagged": None,
    "image_derived_oov": 550,
    "has_gaps": 550,
    "T1": None,
    "T2": 600,
    "T3_divisare": 700,
    "T3_architizer": 650,
    "T3_archello": None,
}


def _rank(cid: str) -> str:
    return hashlib.md5((cid + SEED).encode("utf-8")).hexdigest()


def main() -> int:
    l4l5 = json.loads(L4L5.read_text(encoding="utf-8"))
    flag_cids: set[str] = set(l4l5["L4_embedding"]["near_duplicate_pairs"]["all_cids"])
    flag_cids |= {r["canonical_bld_id"]
                  for r in l4l5["L5_coherence"]["divisare_leaked_name_bug"]["reached_canonical"]}

    style_vocab = {str(v) for v in vocab.STYLE}
    tone_vocab = {str(v) for v in vocab.COLOR_TONE}

    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT canonical_bld_id, confidence_tier, project_year,
                       location_country, location_city,
                       cardinality(architect_canonical_ids),
                       source_refs,
                       image_derived->>'style', image_derived->>'color_tone'
                FROM {TABLE}
            """)
            rows = cur.fetchall()
    finally:
        conn.rollback()
        conn.close()

    strata: dict[str, list[str]] = {k: [] for k in TARGETS}
    for (cid, tier, year, country, city, n_arch, srefs, idstyle, idtone) in rows:
        srefs = srefs or {}
        is_flagged = (
            cid in flag_cids
            or (year is not None and (year < 1850 or year > 2027))
            or (n_arch or 0) >= 5
        )
        oov = ((idstyle is not None and idstyle not in style_vocab)
               or (idtone is not None and idtone not in tone_vocab))
        has_gap = country is None or city is None or year is None

        if is_flagged:
            s = "flagged"
        elif oov:
            s = "image_derived_oov"
        elif has_gap:
            s = "has_gaps"
        elif tier == "T1":
            s = "T1"
        elif tier == "T2":
            s = "T2"
        else:  # T3 single-source
            keys = list(srefs.keys())
            src = keys[0] if keys else "unknown"
            s = f"T3_{src}" if f"T3_{src}" in TARGETS else "T3_divisare"
        strata[s].append(cid)

    manifest: dict = {"seed": SEED, "table": TABLE, "baseline": "completeness_c8",
                      "strata": {}, "sample": []}
    total = 0
    for name, cids in strata.items():
        cids_sorted = sorted(cids, key=_rank)
        target = TARGETS[name]
        chosen = cids_sorted if target is None else cids_sorted[:target]
        manifest["strata"][name] = {"population": len(cids), "sampled": len(chosen)}
        for c in chosen:
            manifest["sample"].append({"canonical_bld_id": c, "stratum": name})
        total += len(chosen)
    manifest["total_population"] = sum(len(c) for c in strata.values())
    manifest["total_sampled"] = total

    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"strata": manifest["strata"],
                       "total_population": manifest["total_population"],
                       "total_sampled": total}, ensure_ascii=False, indent=2))
    print(f"manifest: {MANIFEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
