#!/usr/bin/env python3
"""Download + validate + downscale cover images for the vision audit strata.

Vision token cost scales with image size, so every image is re-encoded to a max
dimension (default 1024px) JPEG. Dead URLs / non-images are skipped and reported.
Writes a manifest {id: {path, ok, w, h, stratum, error}} the vision workflow reads.

Usage:
  python3 tools/audit_fetch_images.py --sample data/reports/audit_label_sample.full.json \
      --strata S2_style,S3_r4_visual,S5_baseline --out-dir /tmp/audit_imgs \
      --manifest data/reports/audit_image_manifest.json [--per-stratum 10]
"""
from __future__ import annotations

import argparse
import io
import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

UA = "Mozilla/5.0 (audit-bot; archi-tinder QC)"


def fetch_one(row, out_dir, max_dim):
    bid = row["canonical_bld_id"]
    url = row.get("display_cover_url")
    rec = {"id": bid, "stratum": row.get("_stratum"), "ok": False, "path": None,
           "w": None, "h": None, "error": None, "url": url}
    if not url:
        rec["error"] = "no_url"
        return rec
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = resp.read()
        img = Image.open(io.BytesIO(data))
        img.verify()
        img = Image.open(io.BytesIO(data)).convert("RGB")
        w, h = img.size
        if max(w, h) > max_dim:
            scale = max_dim / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)))
        p = Path(out_dir) / f"{bid}.jpg"
        img.save(p, "JPEG", quality=85)
        rec.update(ok=True, path=str(p), w=w, h=h)
    except Exception as e:  # noqa: BLE001
        rec["error"] = str(e)[:160]
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", required=True)
    ap.add_argument("--strata", required=True)
    ap.add_argument("--out-dir", default="/tmp/audit_imgs")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--max-dim", type=int, default=1024)
    ap.add_argument("--per-stratum", type=int, default=0, help="0 = all")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    Path(args.out_dir).mkdir(parents=True, exist_ok=True)
    want = set(args.strata.split(","))
    data = json.loads(Path(args.sample).read_text())
    rows = [r for r in data["rows"] if r.get("_stratum") in want]
    if args.per_stratum:
        by: dict[str, list] = {}
        for r in rows:
            by.setdefault(r["_stratum"], []).append(r)
        rows = [r for s in by.values() for r in s[: args.per_stratum]]

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        recs = list(ex.map(lambda r: fetch_one(r, args.out_dir, args.max_dim), rows))

    manifest = {r["id"]: r for r in recs}
    Path(args.manifest).parent.mkdir(parents=True, exist_ok=True)
    Path(args.manifest).write_text(json.dumps(manifest, indent=2))
    ok = sum(1 for r in recs if r["ok"])
    summary = {"requested": len(recs), "ok": ok, "failed": len(recs) - ok}
    by_str: dict[str, dict] = {}
    for r in recs:
        s = by_str.setdefault(r["stratum"], {"ok": 0, "fail": 0})
        s["ok" if r["ok"] else "fail"] += 1
    summary["by_stratum"] = by_str
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
