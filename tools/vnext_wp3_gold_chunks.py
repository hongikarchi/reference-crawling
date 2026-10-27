#!/usr/bin/env python3
"""vNext WP3 N10: split fetched images into 4 blind gold-review chunks (whole buildings).

Assignment: buildings sorted by (ok image count desc, canonical_bld_id), each to the
chunk with the fewest images so far (ties -> lowest chunk number). Chunk files are
JSON lists of {idx, image_path (absolute), canonical_bld_id, building_name} only:
NO local labels, scores or cover ranks (gold must be blind). Failed fetches are not
reviewable and are listed in index.json.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WP = ROOT / "data/smoke_vnext/wp3_n10_v1"
N_CHUNKS = 4


def main() -> int:
    m = json.loads((WP / "sample_manifest.json").read_text(encoding="utf-8"))
    flog = {json.loads(l)["idx"]: json.loads(l) for l in (WP / "fetch_log.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()}
    blds, failed = [], []
    for b in m["buildings"]:
        items = []
        for im in b["images"]:
            r = flog.get(im["idx"])
            if r and r["status"] == "ok":
                items.append({"idx": im["idx"], "image_path": str((ROOT / r["image_path"]).resolve()),
                              "canonical_bld_id": b["canonical_bld_id"], "building_name": b["name"]})
            else:
                failed.append({"idx": im["idx"], "canonical_bld_id": b["canonical_bld_id"],
                               "error_code": (r or {}).get("error_code", "missing_fetch_row")})
        blds.append((b["canonical_bld_id"], items))
    blds.sort(key=lambda x: (-len(x[1]), x[0]))
    chunks = [[] for _ in range(N_CHUNKS)]
    members = [[] for _ in range(N_CHUNKS)]
    for bid, items in blds:
        k = min(range(N_CHUNKS), key=lambda i: (len(chunks[i]), i))
        chunks[k].extend(items)
        members[k].append(bid)
    out = WP / "gold_chunks"
    out.mkdir(parents=True, exist_ok=True)
    index = {"rule": __doc__.strip(), "manifest_sha256": hashlib.sha256((WP / "sample_manifest.json").read_bytes()).hexdigest(),
             "chunks": [], "not_reviewable_failed_fetch": failed}
    for i, c in enumerate(chunks, 1):
        c.sort(key=lambda x: x["idx"])
        p = out / f"chunk_{i}.json"
        p.write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding="utf-8")
        index["chunks"].append({"file": str(p.resolve()), "images": len(c), "buildings": members[i - 1],
                                "sha256": hashlib.sha256(p.read_bytes()).hexdigest()})
        print(p.resolve(), len(c), members[i - 1])
    (out / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
