#!/usr/bin/env python3
"""Fetch up to K spread views per building (from all_images) for the multi-image
vision probe. Tests whether judging on >1 view raises vision self-consistency on the
cover-under-determined axes (scale/structural/facade/roof). Read-only on Neon.

Usage:
  python3 tools/accuracy_fetch_multi.py --ids-from data/reports/accuracy_sample.n100.json \
      --limit 30 --k 4 --out-dir /tmp/acc_multi --manifest data/reports/accuracy_multi_manifest.json
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import psycopg2.extras  # noqa: E402
from PIL import Image  # noqa: E402

from tools.canonical_v2_neon_loader import _connect  # noqa: E402

UA = "Mozilla/5.0 (audit-bot; archi-tinder QC)"


def pick_urls(images, k):
    imgs = [im for im in images if isinstance(im, dict) and im.get("url")]
    imgs.sort(key=lambda im: (im.get("image_order") if im.get("image_order") is not None else 999))
    if len(imgs) <= k:
        return [im["url"] for im in imgs]
    # cover (first) + evenly spread across the rest
    idx = [0] + [round(i * (len(imgs) - 1) / (k - 1)) for i in range(1, k)]
    seen, out = set(), []
    for j in idx:
        if j not in seen:
            seen.add(j); out.append(imgs[j]["url"])
    return out


def fetch(url, dest, max_dim=1024):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = resp.read()
        img = Image.open(io.BytesIO(data)); img.verify()
        img = Image.open(io.BytesIO(data)).convert("RGB")
        w, h = img.size
        if max(w, h) > max_dim:
            s = max_dim / max(w, h)
            img = img.resize((int(w * s), int(h * s)))
        img.save(dest, "JPEG", quality=85)
        return True
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids-from", required=True)
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--k", type=int, default=4)
    ap.add_argument("--out-dir", default="/tmp/acc_multi")
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()

    ids = [r["canonical_bld_id"]
           for r in json.loads(Path(args.ids_from).read_text())["rows"]][: args.limit]
    conn = _connect(); conn.set_session(readonly=True)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT canonical_bld_id, all_images FROM canonical_v2_buildings
                   WHERE canonical_bld_id = ANY(%s)""", (ids,))
    rows = {r["canonical_bld_id"]: r["all_images"] for r in cur.fetchall()}
    conn.close()

    manifest = {}
    tasks = []
    for bid in ids:
        urls = pick_urls(rows.get(bid) or [], args.k)
        d = Path(args.out_dir) / bid
        d.mkdir(parents=True, exist_ok=True)
        paths = []
        for i, u in enumerate(urls):
            p = d / f"img{i}.jpg"
            tasks.append((u, p))
            paths.append(str(p))
        manifest[bid] = {"urls": urls, "paths": paths, "ok_paths": []}

    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(lambda t: (t[1], fetch(t[0], t[1])), tasks))
    ok = {str(p) for p, good in results if good}
    for bid, m in manifest.items():
        m["ok_paths"] = [p for p in m["paths"] if p in ok]
        m["ok"] = len(m["ok_paths"]) > 0

    Path(args.manifest).write_text(json.dumps(manifest, indent=2))
    n_ok = sum(1 for m in manifest.values() if m["ok"])
    print(json.dumps({"buildings": len(manifest), "with_images": n_ok,
                      "total_imgs_ok": len(ok)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
