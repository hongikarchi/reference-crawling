#!/usr/bin/env python3
"""WP3 teach: SigLIP-2 so400m image embeddings for the training pool (offline, $0).

Same model, transform and code path as tools/vnext_wp3_exp_v2_embed.py (`emb.npy` variant =
squash resize 384 bilinear == local engine v1 transform; reuses tools/vnext_s3_local_engine
load_model / load_resized / to_pixel_values / image_features unchanged). Runs in the vision venv.

Writes ONLY under data/smoke_vnext/wp3_pool_v1/:
- emb.npy         [N,1152] float32, L2-normalised, rows = pool images with fetch_status ok, idx ascending
- emb_index.json  row order (idx, bld, stratum, path, pixel sha), model, snapshot, parity check

Parity check: re-embeds the first 32 N10 eval images with this code and compares to the frozen
data/smoke_vnext/wp3_n10_v1/exp_v2/emb.npy (read-only) -> max |diff| and min cosine recorded.
No network (HF_HUB_OFFLINE=1), no Neon/R2, no paid calls.

Usage: C:/Users/user/venvs/vnext-vision/Scripts/python.exe tools/vnext_wp3_pool_embed.py
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import vnext_s3_local_engine as eng  # noqa: E402

POOL = ROOT / "data/smoke_vnext/wp3_pool_v1"
EXP2 = ROOT / "data/smoke_vnext/wp3_n10_v1/exp_v2"
CFG1 = ROOT / "tools/vnext_s3_local_engine_config_v1.json"


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> int:
    cfg1 = json.loads(CFG1.read_text(encoding="utf-8"))
    man_p = POOL / "pool_manifest.json"
    man = json.loads(man_p.read_text(encoding="utf-8"))
    rows, paths = [], []
    for im in sorted(man["images"], key=lambda x: x["idx"]):
        if im["fetch_status"] != "ok":
            continue
        p = ROOT / im["path"]
        s = sha256_file(p)
        assert s == im["pixel_sha256"], f"pixel sha drift idx {im['idx']}"
        rows.append({"idx": im["idx"], "canonical_bld_id": im["canonical_bld_id"], "stratum": im["stratum"],
                     "selection_reason": im["selection_reason"], "image_path": im["path"], "pixel_sha256": s})
        paths.append(str(p))
    device, dtype = "cuda", torch.float16
    t0 = time.perf_counter()
    model_id, proc, model, errs = eng.load_model(cfg1, device, dtype)

    def embed(ps):
        out = []
        for i in range(0, len(ps), 32):
            arrs = [eng.load_resized(p) for p in ps[i:i + 32]]
            pv = eng.to_pixel_values(arrs, cfg1, device, dtype)
            out.append(eng.image_features(model, pv).cpu().numpy())
        return np.concatenate(out).astype(np.float32)

    E = embed(paths)
    np.save(POOL / "emb.npy", E)
    # parity vs frozen exp_v2 eval embeddings
    ei = json.loads((EXP2 / "emb_index.json").read_text(encoding="utf-8"))
    ev_paths = [str(ROOT / r["image_path"]) for r in ei["rows"][:32]]
    Eref = np.load(EXP2 / "emb.npy")[:32]
    Ee = embed(ev_paths)
    parity = {"n": 32, "max_abs_diff": float(np.abs(Ee - Eref).max()),
              "min_cosine": float((Ee * Eref).sum(1).min())}
    snap = None
    try:
        from huggingface_hub import scan_cache_dir
        for repo in scan_cache_dir().repos:
            if repo.repo_id == model_id:
                snap = sorted(r.commit_hash for r in repo.revisions)
    except Exception:
        pass
    doc = {"version": "vnext-wp3-pool-emb-v1", "tool": "tools/vnext_wp3_pool_embed.py",
           "tool_sha256": sha256_file(Path(__file__)), "model": model_id, "model_snapshot": snap,
           "model_load_errors": errs, "transform": "squash resize 384 bilinear (== exp_v2 emb.npy / engine v1)",
           "dtype": "float16 forward, float32 stored, L2-normalised",
           "logit_scale": float(model.logit_scale.exp()), "logit_bias": float(model.logit_bias),
           "pool_manifest_sha256": sha256_file(man_p), "emb_sha256": sha256_file(POOL / "emb.npy"),
           "parity_vs_exp_v2_emb": parity, "n_images": len(rows), "wall_s": round(time.perf_counter() - t0, 2),
           "rows": rows}
    (POOL / "emb_index.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: doc[k] for k in ("model", "model_snapshot", "n_images", "wall_s", "parity_vs_exp_v2_emb",
                                          "logit_scale", "logit_bias")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
