#!/usr/bin/env python3
"""FREE local cover classifier — SigLIP zero-shot (2026-06-27).

Classifies a building cover into the same representativeness vocabulary as the
Haiku audit, but on-device (MPS), $0. Enables full-census cover re-pick without
LLM cost. Validate against the Haiku-labelled sample first (--compare).

  classify  download+zero-shot-classify covers for a set of ids -> jsonl
  (--compare <haiku_classified.jsonl>  report good/bad-binary agreement)

Usage:
  python3 tools/cover_classify_siglip.py --ids-file data/reports/cover_audit/s150/classified.jsonl \
      --out data/reports/cover_audit/s150_siglip.jsonl --compare data/reports/cover_audit/s150/classified.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.image_dedup_5type import _download_to_tmp  # noqa: E402

SRC_META = ROOT / "data/reports/neighbor_eval/meta.jsonl"
GOOD = {"representative_exterior", "aerial_or_siteplan"}

# zero-shot category -> descriptive prompt(s)
CAT_PROMPTS = {
    "representative_exterior": "a photo of a building seen from outside, its exterior facade and overall form",
    "interior_only": "an interior photo taken inside a building, a room or lobby",
    "render_or_drawing": "a 3D architectural CGI render, drawing, diagram or floor plan",
    "detail_closeup": "an extreme close-up of an architectural material or facade detail",
    "aerial_or_siteplan": "an aerial or bird's-eye view of a building and its site",
    "people_or_object_dominant": "a photo where people or objects dominate and the building is incidental",
    "not_a_building": "a landscape, nature scene, portrait or logo with no building",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids-file", type=Path, help="jsonl with canonical_bld_id per line")
    ap.add_argument("--all", action="store_true", help="classify every publishable cover")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--compare", type=Path, help="haiku classified.jsonl -> agreement")
    ap.add_argument("--batch", type=int, default=64)
    args = ap.parse_args()

    import torch
    import torch.nn.functional as F
    from PIL import Image
    from transformers import AutoModel, AutoProcessor

    meta = {m["canonical_bld_id"]: m
            for m in (json.loads(l) for l in SRC_META.read_text().split("\n") if l)}
    if args.all:
        ids = [m for m in meta if meta[m].get("display_cover_url")]
    else:
        ids = [json.loads(l)["canonical_bld_id"]
               for l in args.ids_file.read_text().split("\n") if l]

    name = "google/siglip-base-patch16-224"
    dev = "mps" if torch.backends.mps.is_available() else "cpu"
    proc = AutoProcessor.from_pretrained(name)
    model = AutoModel.from_pretrained(name).to(dev).eval()
    labels = list(CAT_PROMPTS)
    ti = proc(text=[CAT_PROMPTS[k] for k in labels], padding="max_length",
              return_tensors="pt").to(dev)
    with torch.no_grad():
        tf = F.normalize(model.get_text_features(**ti), dim=-1)

    out_rows, cache = [], ROOT / "data/reports/cover_audit/_cache"
    cache.mkdir(parents=True, exist_ok=True)
    buf_imgs, buf_ids = [], []

    def flush():
        if not buf_imgs:
            return
        ii = proc(images=buf_imgs, return_tensors="pt").to(dev)
        with torch.no_grad():
            vf = F.normalize(model.get_image_features(**ii), dim=-1)
            sims = vf @ tf.T
        for bid, row_sims in zip(buf_ids, sims):
            top = int(row_sims.argmax())
            out_rows.append({"canonical_bld_id": bid, "category": labels[top],
                             "score": round(float(row_sims[top]), 3)})
        buf_imgs.clear(); buf_ids.clear()

    for n, bid in enumerate(ids, 1):
        url = meta[bid].get("display_cover_url")
        dest = cache / f"{bid}.img"
        try:
            if not dest.exists():
                tmp, dl = _download_to_tmp(url)
                dest.write_bytes(Path(tmp).read_bytes())
                if dl:
                    Path(tmp).unlink(missing_ok=True)
            buf_imgs.append(Image.open(dest).convert("RGB")); buf_ids.append(bid)
        except Exception:
            out_rows.append({"canonical_bld_id": bid, "category": "missing_or_broken"})
        if len(buf_imgs) >= args.batch:
            flush()
        if n % 500 == 0:
            print(f"  ...{n}/{len(ids)}", file=sys.stderr)
    flush()

    with open(args.out, "w", encoding="utf-8") as f:
        for r in out_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"wrote {len(out_rows)} -> {args.out}")

    if args.compare:
        hk = {json.loads(l)["canonical_bld_id"]: json.loads(l)["category"]
              for l in args.compare.read_text().split("\n") if l}
        sg = {r["canonical_bld_id"]: r["category"] for r in out_rows}
        ids2 = [i for i in hk if i in sg]
        exact = sum(hk[i] == sg[i] for i in ids2)
        binok = sum((hk[i] in GOOD) == (sg[i] in GOOD) for i in ids2)
        from collections import Counter
        conf = Counter((hk[i] in GOOD, sg[i] in GOOD) for i in ids2)
        print(f"\n=== vs Haiku (n={len(ids2)}) ===")
        print(f"  exact-category agreement: {exact/len(ids2):.1%}")
        print(f"  GOOD/BAD binary agreement: {binok/len(ids2):.1%}")
        print(f"  both-GOOD {conf[(True,True)]} | both-BAD {conf[(False,False)]} "
              f"| Haiku-GOOD/SigLIP-BAD {conf[(True,False)]} "
              f"| Haiku-BAD/SigLIP-GOOD {conf[(False,True)]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
