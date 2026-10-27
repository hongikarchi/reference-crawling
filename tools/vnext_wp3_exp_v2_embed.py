#!/usr/bin/env python3
"""WP3 exp v2: cache SigLIP-2 image + prompt embeddings for the N10 sample (offline, $0).

Runs in the vision venv (C:/Users/user/venvs/vnext-vision). Reads the frozen WP3 N10
images (already on disk, SHA-verified against fetch_log) and writes ONLY under
data/smoke_vnext/wp3_n10_v1/exp_v2/:

- emb.npy          [N,1152] float32  full image squash-resized 384 (== engine v1 transform)
- emb_center.npy   [N,1152] float32  aspect-preserving resize (short side 384) + center crop
- emb_flip.npy     [N,1152] float32  horizontal flip of the full transform
- emb_index.json   row order (idx), image paths, pixel sha, pixel stats, model, logit scale/bias
- text_emb.npy + text_index.json     L2-normalised embedding per (lowercased) phrase of
                                     config v1 and config v2

No network (model must already be in the HF cache), no Neon/R2, no paid calls.

Usage (repo root):
  C:/Users/user/venvs/vnext-vision/Scripts/python.exe tools/vnext_wp3_exp_v2_embed.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import numpy as np
import torch
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import vnext_s3_local_engine as eng  # noqa: E402  (reuse model load + v1 transform exactly)

WP = ROOT / "data/smoke_vnext/wp3_n10_v1"
OUT = WP / "exp_v2"
CONFIGS = [ROOT / "tools/vnext_s3_local_engine_config_v1.json", ROOT / "tools/vnext_s3_local_engine_config_v2.json"]


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def load_center(path: str, size: int = 384) -> np.ndarray:
    with Image.open(path) as im:
        im = im.convert("RGB")
        w, h = im.size
        s = size / min(w, h)
        im = im.resize((max(size, round(w * s)), max(size, round(h * s))), Image.Resampling.BILINEAR)
        w, h = im.size
        l, t = (w - size) // 2, (h - size) // 2
        return np.asarray(im.crop((l, t, l + size, t + size)), dtype=np.uint8)


def pixel_stats(arr: np.ndarray, path: str) -> dict:
    g = arr.astype(np.float32) @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    dx = np.abs(np.diff(g, axis=1)).mean()
    dy = np.abs(np.diff(g, axis=0)).mean()
    with Image.open(path) as im:
        w, h = im.size
    return {"gray_mean": round(float(g.mean()), 2), "gray_std": round(float(g.std()), 2),
            "edge_mean": round(float(dx + dy), 3),
            "frac_near_white": round(float((g > 245).mean()), 4),
            "frac_near_black": round(float((g < 10).mean()), 4), "orig_w": w, "orig_h": h}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    cfg1 = json.loads(CONFIGS[0].read_text(encoding="utf-8"))
    flog = {}
    for line in (WP / "fetch_log.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            flog[r["idx"]] = r
    manifest = json.loads((WP / "sample_manifest.json").read_text(encoding="utf-8"))
    items = [(b["canonical_bld_id"], b["stratum"], im["idx"]) for b in manifest["buildings"] for im in b["images"]]
    items.sort(key=lambda t: t[2])
    paths, rows = [], []
    for bld, stratum, idx in items:
        r = flog[idx]
        assert r["status"] == "ok"
        p = ROOT / r["image_path"]
        sha = sha256_file(p)
        assert sha == r["saved_sha256"], f"pixel sha drift idx {idx}"
        paths.append(str(p))
        rows.append({"idx": idx, "canonical_bld_id": bld, "stratum": stratum,
                     "image_path": r["image_path"], "pixel_sha256": sha})

    device, dtype = "cuda", torch.float16
    t0 = time.perf_counter()
    model_id, proc, model, errs = eng.load_model(cfg1, device, dtype)
    scale, bias = float(model.logit_scale.exp()), float(model.logit_bias)

    def embed(arrs):
        out = []
        for i in range(0, len(arrs), 32):
            pv = eng.to_pixel_values(arrs[i:i + 32], cfg1, device, dtype)
            out.append(eng.image_features(model, pv).cpu().numpy())
        return np.concatenate(out).astype(np.float32)

    full = [eng.load_resized(p) for p in paths]
    for r, a, p in zip(rows, full, paths):
        r["pixel_stats"] = pixel_stats(a, p)
    e_full = embed(full)
    e_flip = embed([np.ascontiguousarray(a[:, ::-1]) for a in full])
    e_center = embed([load_center(p) for p in paths])
    np.save(OUT / "emb.npy", e_full)
    np.save(OUT / "emb_flip.npy", e_flip)
    np.save(OUT / "emb_center.npy", e_center)

    # prompts of v1 + v2 (lowercased like the engine)
    phrases = []
    for c in CONFIGS:
        cfg = json.loads(c.read_text(encoding="utf-8"))
        for field, values in cfg["fields"].items():
            for value, subs in values.items():
                for sub, phr in subs.items():
                    for p in phr:
                        q = p.lower()
                        if q not in phrases:
                            phrases.append(q)
    tp = cfg1["text_preprocess"]
    tembs = []
    for i in range(0, len(phrases), 64):
        tok = proc.tokenizer(phrases[i:i + 64], padding=tp["padding"], max_length=tp["max_length"],
                             truncation=True, return_tensors="pt").to(device)
        with torch.no_grad():
            e = eng._feat(model.get_text_features(**tok)).float()
        tembs.append(torch.nn.functional.normalize(e, dim=-1).cpu().numpy())
    tembs = np.concatenate(tembs).astype(np.float32)
    np.save(OUT / "text_emb.npy", tembs)
    (OUT / "text_index.json").write_text(json.dumps({"phrases": phrases}, ensure_ascii=False, indent=0), encoding="utf-8")

    snap = None
    try:
        from huggingface_hub import scan_cache_dir
        for repo in scan_cache_dir().repos:
            if repo.repo_id == model_id:
                snap = sorted(r.commit_hash for r in repo.revisions)
    except Exception:
        pass
    idx_doc = {
        "version": "vnext-wp3-exp-v2-emb-v1", "tool": "tools/vnext_wp3_exp_v2_embed.py",
        "tool_sha256": sha256_file(Path(__file__)), "model": model_id, "model_snapshot": snap,
        "logit_scale": scale, "logit_bias": bias, "dtype": "float16 forward, float32 stored, L2-normalised",
        "variants": {"emb.npy": "squash resize 384 bilinear (== engine v1 transform)",
                     "emb_center.npy": "short side 384 + center crop 384",
                     "emb_flip.npy": "horizontal flip of emb.npy transform"},
        "config_sha256": {c.name: sha256_file(c) for c in CONFIGS},
        "n_images": len(rows), "n_phrases": len(phrases), "wall_s": round(time.perf_counter() - t0, 2),
        "rows": rows,
    }
    (OUT / "emb_index.json").write_text(json.dumps(idx_doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: idx_doc[k] for k in ("model", "n_images", "n_phrases", "wall_s", "logit_scale", "logit_bias")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
