#!/usr/bin/env python3
"""WP3 teach: adapt the pool manifest to the codex engine input format + choose the slice (offline, $0).

`tools/vnext_s3_codex_engine.py::load_items` reads the N10 layout (`buildings[].images[]` +
fetch log keyed by `idx`). The pool manifest (`data/smoke_vnext/wp3_pool_v1/pool_manifest.json`,
built by tools/vnext_wp3_pool_sample.py) is a flat `images[]` list keyed by `idx` whose fetch log
rows are keyed by `plan_no`. This tool writes two NEW derived files (the pool manifest and its
fetch log are opened read-only and SHA-recorded):

- `codex_input/manifest_engine.json`   {"buildings":[{canonical_bld_id, stratum, images:[{idx, image_asset_id, sort_order}]}]}
- `codex_input/fetch_log_engine.jsonl` {"idx", "status", "image_path", "saved_sha256"} (pixel SHA re-verified on disk)
- `codex_input/slice_idx.json`         the smoke slice = whole engine batches (b8, same chunking as the full
                                        run so the full run resumes over them), chosen by
                                        sha256('vnext-wp3-pool-codex-slice-v1|'+batch_id) ascending until >=100 images

No network, no model calls, no Neon/R2.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POOL = ROOT / "data" / "smoke_vnext" / "wp3_pool_v1"
OUT = POOL / "codex_input"
SLICE_SALT = "vnext-wp3-pool-codex-slice-v1|"
BATCH = 8
SLICE_MIN = 100


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> int:
    man_p = POOL / "pool_manifest.json"
    man = json.loads(man_p.read_text(encoding="utf-8"))
    by = {}
    flog = []
    n_fail = 0
    for im in sorted(man["images"], key=lambda x: x["idx"]):
        b = by.setdefault(im["canonical_bld_id"], {"canonical_bld_id": im["canonical_bld_id"],
                                                   "stratum": im["stratum"], "images": []})
        b["images"].append({"idx": im["idx"], "image_asset_id": im["image_asset_id"], "sort_order": im["sort_order"],
                            "selection_reason": im["selection_reason"]})
        if im["fetch_status"] == "ok":
            p = ROOT / im["path"]
            s = sha(p)
            assert s == im["pixel_sha256"], f"pixel sha drift idx {im['idx']}"
            flog.append({"idx": im["idx"], "status": "ok", "image_path": im["path"], "saved_sha256": s})
        else:
            n_fail += 1
            flog.append({"idx": im["idx"], "status": "failed", "image_path": None, "saved_sha256": None,
                         "error": im.get("fetch_error_code")})
    OUT.mkdir(parents=True, exist_ok=True)
    doc = {"version": "vnext-wp3-pool-codex-input-v1", "tool": "tools/vnext_wp3_pool_codex_prep.py",
           "tool_sha256": sha(Path(__file__)), "source_manifest": man_p.relative_to(ROOT).as_posix(),
           "source_manifest_sha256": sha(man_p), "buildings": sorted(by.values(), key=lambda b: b["images"][0]["idx"])}
    (OUT / "manifest_engine.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    with open(OUT / "fetch_log_engine.jsonl", "w", encoding="utf-8") as fh:
        for r in flog:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    ok = [r["idx"] for r in flog if r["status"] == "ok"]  # engine: sorted by idx, image_path present
    batches = [ok[i:i + BATCH] for i in range(0, len(ok), BATCH)]
    bid = lambda ch: f"b{BATCH}_" + "-".join(map(str, ch))  # noqa: E731  (== engine make_batches)
    order = sorted(batches, key=lambda ch: hashlib.sha256((SLICE_SALT + bid(ch)).encode()).hexdigest())
    pick, n = [], 0
    for ch in order:
        if n >= SLICE_MIN:
            break
        if len(ch) < BATCH:
            continue
        pick.append(ch)
        n += len(ch)
    pick.sort(key=lambda ch: ch[0])
    idx = [i for ch in pick for i in ch]
    reason = {im["idx"]: im["selection_reason"] for im in man["images"]}
    from collections import Counter
    sl = {"rule": f"whole b{BATCH} engine batches, sha256('{SLICE_SALT}'+batch_id) ascending until >= {SLICE_MIN} images",
          "batch_ids": [bid(ch) for ch in pick], "idx": idx, "n_images": len(idx),
          "per_selection_reason": dict(Counter(reason[i] for i in idx))}
    (OUT / "slice_idx.json").write_text(json.dumps(sl, indent=1), encoding="utf-8")
    print(json.dumps({"buildings": len(by), "images": len(flog), "ok": len(ok), "failed": n_fail,
                      "batches_full_run": len(batches), "slice_images": len(idx),
                      "slice_reasons": sl["per_selection_reason"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
