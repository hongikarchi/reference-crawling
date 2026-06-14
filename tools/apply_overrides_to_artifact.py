#!/usr/bin/env python3
"""Sync the production canonical artifact with the 2026-Q2 Neon remediation.

Streams the c23_final embedded artifact, overlays the authoritative per-id override
sidecar (typology_primary/source/tags + material_visual + architectural_elements), and
writes a c26_rem2026q2 embedded artifact == live Neon. Closes the re-upsert revert path
(an upsert from c23_final would otherwise undo the remediation). Read-only on Neon.
"""
from __future__ import annotations
import json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.canonical_v2_upload_validator import iter_buildings  # noqa: E402

CCR = ROOT / "data/canonical/country_conflict_refresh"
SRC = CCR / "canonical_buildings_strict_embedded.completeness_c23_final.json"
OUT = CCR / "canonical_buildings_strict_embedded.completeness_c26_rem2026q2.json"
OVR = ROOT / "data/canonical/remediation_overrides_rem2026q2.jsonl"
COLS = ("typology_primary", "typology_primary_source", "typology_tags",
        "material_visual", "architectural_elements",
        "is_publishable", "publishability_reasons")


def main() -> int:
    overrides = {}
    with OVR.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                r = json.loads(line)
                overrides[r["id"]] = r
    total = applied = 0
    with OUT.open("w", encoding="utf-8") as out:
        out.write('{"buildings": [')
        for row in iter_buildings(SRC):
            ov = overrides.get(row.get("canonical_bld_id"))
            if ov:
                for c in COLS:
                    row[c] = ov[c]
                applied += 1
            out.write(("," if total else "") + json.dumps(row, ensure_ascii=False))
            total += 1
        out.write("]}")
    print(json.dumps({"src_rows": total, "overrides": len(overrides),
                      "applied": applied, "out": str(OUT)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
