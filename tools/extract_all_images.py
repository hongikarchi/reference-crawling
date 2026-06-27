#!/usr/bin/env python3
"""Extract per-building candidate image URLs from the 1GB source (2026-06-27).

For the cover re-pick: each publishable building's `all_images` is the candidate pool
to choose a better exterior cover from. Streams the source once -> compact sidecar.

Output: all_images.jsonl, one row per publishable building:
  {canonical_bld_id, current_cover, n_candidates, candidates:[{url, kind, order}]}

Read-only on the local artifact. No Neon, no network.

Usage:
  python3 tools/extract_all_images.py            # full publishable
  python3 tools/extract_all_images.py --limit 200
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import ijson

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "data/canonical/country_conflict_refresh/"
       "canonical_buildings_strict_embedded.completeness_c26_rem2026q2.json")
OUT = ROOT / "data/reports/cover_audit/all_images.jsonl"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, default=SRC)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    kept = 0
    with open(args.src, "rb") as f, open(args.out, "w", encoding="utf-8") as fo:
        for b in ijson.items(f, "buildings.item"):
            if not b.get("is_publishable"):
                continue
            imgs = b.get("all_images") or []
            seen, cands = set(), []
            for im in imgs:
                u = im.get("url")
                if not u or u in seen:
                    continue
                seen.add(u)
                cands.append({"url": u, "kind": im.get("kind"),
                              "order": im.get("image_order")})
            fo.write(json.dumps({
                "canonical_bld_id": b["canonical_bld_id"],
                "current_cover": b.get("display_cover_url"),
                "n_candidates": len(cands),
                "candidates": cands,
            }, ensure_ascii=False) + "\n")
            kept += 1
            if kept % 5000 == 0:
                print(f"  ...{kept}", file=sys.stderr)
            if args.limit and kept >= args.limit:
                break
    print(f"wrote {kept} publishable -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
