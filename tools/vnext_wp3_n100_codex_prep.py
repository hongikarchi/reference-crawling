#!/usr/bin/env python3
"""vNext WP3 N100: engine-shaped holdout manifest for tools/vnext_s3_codex_engine.py ($0, offline).

The holdout manifest (data/smoke_vnext/wp3_pool_v1/holdout_n100_manifest.json, frozen) carries no
`idx`; the pre-registered idx rule is "0-based position in manifest order". This writes
data/smoke_vnext/wp3_n100_v1/codex_input/manifest_engine.json
  {"buildings":[{canonical_bld_id, stratum, images:[{idx, image_asset_id, sort_order}]}]}
(same shape as tools/vnext_wp3_pool_codex_prep.py) and cross-checks every idx against the frozen
fetch_log (idx -> canonical_bld_id + image_asset_id + sort_order must match). No names, URLs or
local predictions are written (codex only ever receives image files).
Refuses to run if the pre-registration SHA or the manifest SHA drift.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WP = ROOT / "data" / "smoke_vnext" / "wp3_n100_v1"
PREREG = WP / "preregistration.json"
PREREG_SHA = "59eee674e96aa1582fd4a68950cc645b247c2e023a8b00df17775cfbc49b375b"
MANIFEST = ROOT / "data" / "smoke_vnext" / "wp3_pool_v1" / "holdout_n100_manifest.json"
FETCH_LOG = WP / "fetch_log.jsonl"
OUT = WP / "codex_input" / "manifest_engine.json"


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> int:
    if sha(PREREG) != PREREG_SHA:
        raise SystemExit("preregistration SHA drift")
    pr = json.loads(PREREG.read_text(encoding="utf-8"))
    if sha(MANIFEST) != pr["holdout"]["manifest_sha256"]:
        raise SystemExit("holdout manifest SHA drift")
    m = json.loads(MANIFEST.read_text(encoding="utf-8"))
    fl = {}
    with open(FETCH_LOG, encoding="utf-8") as fh:
        for x in fh:
            if x.strip():
                r = json.loads(x)
                fl[r["idx"]] = r
    blds, idx, mism = [], 0, []
    for b in m["buildings"]:
        ims = []
        for im in b["images"]:
            f = fl.get(idx)
            if f is None or f["canonical_bld_id"] != b["canonical_bld_id"] or \
                    f["image_asset_id"] != im["image_asset_id"] or f["sort_order"] != im["sort_order"]:
                mism.append(idx)
            ims.append({"idx": idx, "image_asset_id": im["image_asset_id"], "sort_order": im["sort_order"]})
            idx += 1
        blds.append({"canonical_bld_id": b["canonical_bld_id"], "stratum": b["stratum"], "images": ims})
    if mism:
        raise SystemExit(f"fetch_log mismatch at idx {mism[:10]} ({len(mism)})")
    doc = {"version": "vnext-wp3-n100-manifest-engine-v1", "tool": "tools/vnext_wp3_n100_codex_prep.py",
           "tool_sha256": sha(Path(__file__)), "source_manifest": str(MANIFEST.relative_to(ROOT)).replace("\\", "/"),
           "source_manifest_sha256": sha(MANIFEST), "preregistration_sha256": PREREG_SHA,
           "idx_rule": pr["holdout"]["idx_rule"], "buildings": blds}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"out": str(OUT.relative_to(ROOT)), "buildings": len(blds), "images": idx,
                      "fetch_ok": sum(1 for r in fl.values() if r.get("status") == "ok"),
                      "sha256": sha(OUT)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
