#!/usr/bin/env python3
"""Phase-2 visual-coordinate builder for the embedding eval (2026-06-25).

Low-cost DIRECTIONAL A/B: does a PIXEL-based coordinate beat the text coordinate?
Builds a shared candidate pool (fair A/B — every space uses the SAME buildings) and
encodes their covers with DINOv2 + SigLIP locally on MPS (free). Output npy matrices
align row-for-row with the pool's meta.jsonl, so neighbor_eval_judge can score each
space with the SAME seeds (paired comparison).

  pool    sample N publishable w/ cover, download+validate covers, keep the common
          set, write meta.jsonl + embeddings.npy (= text coordinate, for the baseline).
  encode  encode the pool's cached covers with --model dinov2|siglip → <model>.npy.

Claude-recaption (branch B) is deferred — it needs LLM captioning at pool scale; run
it only if pixel is ambiguous (it is the costly branch).

Usage:
  python3 tools/neighbor_eval_visual.py pool --n 1500 --out data/reports/neighbor_eval/pool
  python3 tools/neighbor_eval_visual.py encode --model dinov2 --dir .../pool
  python3 tools/neighbor_eval_visual.py encode --model siglip --dir .../pool
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.image_dedup_5type import _download_to_tmp  # noqa: E402

SRC_DIR = ROOT / "data/reports/neighbor_eval"
POOL_META_FIELDS = ("canonical_bld_id", "name", "program", "style",
                    "display_cover_url", "location_country", "project_year")
MODELS = {
    "dinov2": "facebook/dinov2-base",          # 768-dim, self-supervised pure-visual
    "siglip": "google/siglip-base-patch16-224",  # image-text aligned
}


# ------------------------------------------------------------------------- pool
def cmd_pool(args) -> int:
    main_meta = [json.loads(l) for l in (SRC_DIR / "meta.jsonl").read_text().split("\n") if l]
    main_npy = np.load(SRC_DIR / "embeddings.npy")
    idx_by_id = {m["canonical_bld_id"]: i for i, m in enumerate(main_meta)}

    rng = random.Random(args.seed)
    cand = [m for m in main_meta if m.get("display_cover_url")]
    rng.shuffle(cand)

    out = Path(args.out)
    covers = out / "covers"
    covers.mkdir(parents=True, exist_ok=True)
    from PIL import Image

    kept_meta, kept_text = [], []
    tried = 0
    for m in cand:
        if len(kept_meta) >= args.n:
            break
        tried += 1
        bid = m["canonical_bld_id"]
        dest = covers / f"{bid}.img"
        try:
            if not dest.exists():
                tmp, dl = _download_to_tmp(m["display_cover_url"])
                data = Path(tmp).read_bytes()
                if dl:
                    Path(tmp).unlink(missing_ok=True)
                dest.write_bytes(data)
            with Image.open(dest) as im:   # validate decodable
                im.convert("RGB").load()
        except Exception:
            dest.unlink(missing_ok=True)
            continue
        kept_meta.append({k: m.get(k) for k in POOL_META_FIELDS})
        kept_text.append(main_npy[idx_by_id[bid]])
        if len(kept_meta) % 200 == 0:
            print(f"  ...{len(kept_meta)} kept / {tried} tried", file=sys.stderr)

    with open(out / "meta.jsonl", "w", encoding="utf-8") as f:
        for m in kept_meta:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    np.save(out / "embeddings.npy", np.vstack(kept_text).astype(np.float32))
    (out / "pool_manifest.json").write_text(json.dumps(
        {"pool_size": len(kept_meta), "requested": args.n, "tried": tried,
         "covers_dir": str(covers), "text_npy": "embeddings.npy"}, indent=2))
    print(f"pool ready: {len(kept_meta)} buildings (tried {tried}) -> {out}")
    return 0


# ----------------------------------------------------------------------- encode
def cmd_encode(args) -> int:
    import torch
    from PIL import Image
    from transformers import AutoModel, AutoImageProcessor

    out = Path(args.dir)
    meta = [json.loads(l) for l in (out / "meta.jsonl").read_text().split("\n") if l]
    covers = out / "covers"
    name = MODELS[args.model]
    dev = "mps" if torch.backends.mps.is_available() else "cpu"
    print(f"loading {name} on {dev} ...", file=sys.stderr)

    # image processor only (avoids SigLIP's text tokenizer -> no sentencepiece dep)
    proc = AutoImageProcessor.from_pretrained(name)
    model = AutoModel.from_pretrained(name).to(dev).eval()

    vecs = []
    bs = args.batch
    for s in range(0, len(meta), bs):
        chunk = meta[s:s + bs]
        imgs = [Image.open(covers / f"{m['canonical_bld_id']}.img").convert("RGB")
                for m in chunk]
        with torch.no_grad():
            if args.model == "dinov2":
                inp = proc(images=imgs, return_tensors="pt").to(dev)
                feat = model(**inp).last_hidden_state[:, 0]      # CLS token
            else:
                inp = proc(images=imgs, return_tensors="pt").to(dev)
                feat = model.get_image_features(**inp)
        feat = torch.nn.functional.normalize(feat, dim=-1)       # L2 → dot==cosine
        vecs.append(feat.float().cpu().numpy())
        print(f"  encoded {min(s+bs,len(meta))}/{len(meta)}", file=sys.stderr)

    mat = np.vstack(vecs).astype(np.float32)
    np.save(out / f"{args.model}.npy", mat)
    print(f"saved {args.model}.npy {mat.shape} -> {out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pool")
    p.add_argument("--n", type=int, default=1500)
    p.add_argument("--seed", type=int, default=20260625)
    p.add_argument("--out", type=Path, default=SRC_DIR / "pool")
    p.set_defaults(fn=cmd_pool)
    e = sub.add_parser("encode")
    e.add_argument("--model", choices=list(MODELS), required=True)
    e.add_argument("--dir", type=Path, default=SRC_DIR / "pool")
    e.add_argument("--batch", type=int, default=16)
    e.set_defaults(fn=cmd_encode)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
