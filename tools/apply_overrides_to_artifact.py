#!/usr/bin/env python3
"""Sync the production canonical artifact with a Neon change via override sidecars.

Streams a source embedded artifact, overlays a per-id override sidecar, and writes
a new embedded artifact == live Neon. Closes the re-upsert revert path (an upsert
from the stale artifact would otherwise undo the change). Read-only on Neon.

Defaults reproduce the 2026-Q2 remediation bake (c23_final -> c26_rem2026q2).
Each sidecar carries its own column set via --cols — e.g. the 2026-Q3 cover
re-pick overlays ONLY display_cover_url on top of c26:

  python tools/apply_overrides_to_artifact.py \
    --src  ...c26_rem2026q2.json --out ...c27_cover2026q3.json \
    --overrides data/canonical/cover_repick_overrides_2026q3.jsonl \
    --cols display_cover_url
"""
from __future__ import annotations
import argparse, json, sys
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
        "is_publishable", "publishability_reasons",
        "program")  # program: 2026-06-21 contradiction re-derive (bake into c26)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", type=Path, default=SRC)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--overrides", type=Path, default=OVR)
    ap.add_argument("--cols", default=",".join(COLS),
                    help="comma-separated columns this sidecar is authoritative for")
    args = ap.parse_args()
    cols = tuple(c.strip() for c in args.cols.split(",") if c.strip())
    if args.out.resolve() == args.src.resolve():
        raise SystemExit("--out must differ from --src")

    overrides = {}
    with args.overrides.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                r = json.loads(line)
                missing = [c for c in cols if c not in r]
                if missing:
                    raise SystemExit(f"override {r.get('id')}: missing cols {missing}")
                overrides[r["id"]] = r
    total = applied = 0
    with args.out.open("w", encoding="utf-8") as out:
        out.write('{"buildings": [')
        for row in iter_buildings(args.src):
            ov = overrides.get(row.get("canonical_bld_id"))
            if ov:
                for c in cols:
                    row[c] = ov[c]
                applied += 1
            out.write(("," if total else "") + json.dumps(row, ensure_ascii=False))
            total += 1
        out.write("]}")
    if applied != len(overrides):
        print(f"WARNING: {len(overrides) - applied} override ids not found in src",
              file=sys.stderr)
    print(json.dumps({"src_rows": total, "overrides": len(overrides),
                      "applied": applied, "cols": list(cols),
                      "out": str(args.out)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
