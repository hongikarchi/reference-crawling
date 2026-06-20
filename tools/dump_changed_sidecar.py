#!/usr/bin/env python3
"""Regenerate the changed-only typology+material sidecar for the c11 build fold.

Diffs current live-Neon state (remediation_overrides_rem2026q2.jsonl, freshly dumped)
against the c23_final build baseline; emits {id + typ/mat cols} ONLY for rows that differ.
Changed-only (not full) so a future re-enrichment of OTHER rows is not frozen. This now
includes the 2026-Q2 description-based typology corrections. Read-only on disk.
"""
from __future__ import annotations
import json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.canonical_v2_upload_validator import iter_buildings  # noqa: E402

CCR = ROOT / "data/canonical/country_conflict_refresh"
SRC = CCR / "canonical_buildings_strict_embedded.completeness_c23_final.json"
OVR = ROOT / "data/canonical/remediation_overrides_rem2026q2.jsonl"
OUT = ROOT / "data/canonical/remediation_typmat_changed_rem2026q2.jsonl"
COLS = ("typology_primary", "typology_primary_source", "typology_tags",
        "material_visual", "architectural_elements")


def _norm(v):
    return tuple(v) if isinstance(v, list) else v


def main() -> int:
    ovr = {}
    with OVR.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                r = json.loads(line)
                ovr[r["id"]] = r
    changed = 0
    with OUT.open("w", encoding="utf-8") as out:
        for row in iter_buildings(SRC):
            bid = row.get("canonical_bld_id")
            o = ovr.get(bid)
            if not o:
                continue
            if any(_norm(o.get(c)) != _norm(row.get(c)) for c in COLS):
                rec = {"id": bid}
                for c in COLS:
                    rec[c] = o.get(c)
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                changed += 1
    print(json.dumps({"changed_rows": changed, "out": str(OUT)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
