#!/usr/bin/env python3
"""vNext WP3 N100: frozen local predictions on the holdout (SigLIP-2 embeddings + zs v2 logits + probe v1).

Pre-registered engine (data/smoke_vnext/wp3_n100_v1/preregistration.json, SHA checked fail-closed):
  probe v1 primary `hybrid_emb_zs2` (data/smoke_vnext/wp3_pool_v1/probe_v1/hybrid_emb_zs2.pkl, SHA checked),
  scope head mode `scope`, camera abstention t from the pickle, contract §5 cascade = tools/vnext_wp3_probe_v1.cascade.
Embedding: google/siglip2-so400m-patch14-384 via the unchanged tools/vnext_s3_local_engine functions
  (squash resize 384 bilinear, fp16 forward, L2) == pool / exp_v2 emb.npy code path. HF offline.
zs2 logits: tools/vnext_wp3_probe_v1.build_features with exp_v2 text embeddings + config v2 (SHA recorded).
Routing (pre-registered): route = min(scope margin, medium margin) < T, T_primary 0.9347, T_secondary 0.9968.
Covers (pre-registered): tools/vnext_wp3_n100_cover_calib.rank_building with the prereg score variant and
  thresholds A (top-1 score) / M (top-1 - top-2 margin) -> local default, top-3, confidence, local_confident.

Writes ONLY under data/smoke_vnext/wp3_n100_v1/: emb.npy, emb_index.json, zs2_logits.npy, local_pred.jsonl,
local_covers.jsonl, local_run.json, freeze_manifest.json. Images are read-only. No network, no Neon/R2,
no model calls beyond the local GPU. Run in the vision venv.
"""
from __future__ import annotations

import hashlib
import json
import os
import pickle
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import vnext_s3_local_engine as eng  # noqa: E402
from tools import vnext_wp3_probe_v1 as pv  # noqa: E402
from tools import vnext_wp3_n100_cover_calib as cc  # noqa: E402

OUT = ROOT / "data/smoke_vnext/wp3_n100_v1"
PREREG = OUT / "preregistration.json"
PREREG_SHA = OUT / "preregistration.sha256"
MANIFEST = ROOT / "data/smoke_vnext/wp3_pool_v1/holdout_n100_manifest.json"
EXP2 = ROOT / "data/smoke_vnext/wp3_n10_v1/exp_v2"
CFG1 = ROOT / "tools/vnext_s3_local_engine_config_v1.json"
VERSION = "vnext-wp3-n100-local-v1"
BATCH = 32


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def rel(p: Path) -> str:
    return str(Path(p).relative_to(ROOT)).replace("\\", "/")


def rj(p: Path) -> list[dict]:
    with open(p, encoding="utf-8") as fh:
        return [json.loads(x) for x in fh if x.strip()]


def wj(p: Path, rows):
    with open(p, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def main() -> int:
    # ---------------- fail-closed input checks
    rec = PREREG_SHA.read_text(encoding="utf-8").split()[0]
    assert sha(PREREG) == rec, "preregistration SHA drift"
    pre = json.loads(PREREG.read_text(encoding="utf-8"))
    E = pre["engine"]
    pkl = ROOT / E["pickle"]
    assert sha(pkl) == E["pickle_sha256"], "probe pickle SHA drift"
    for k, v in E["frozen_inputs_sha256"].items():
        assert sha(ROOT / k) == v, f"frozen input SHA drift: {k}"
    T1, T2 = pre["label_routing"]["T_primary"], pre["label_routing"]["T_secondary"]
    CR = pre["cover_rule"]
    variant, A, M = CR["score_variant"], CR["A_top1_min"], CR["M_margin_min"]
    with open(pkl, "rb") as fh:
        PK = pickle.load(fh)
    assert PK["variant"] == "hybrid_emb_zs2" and PK["scope_mode"] == "scope"
    cam_t, models = PK["cam_t"], PK["models"]
    # ---------------- images (fetch log, pixel SHA re-verified)
    flog = {r["idx"]: r for r in rj(OUT / "fetch_log.jsonl")}
    man = json.loads(MANIFEST.read_text(encoding="utf-8"))
    items, idx = [], 0
    for b in man["buildings"]:
        for im in b["images"]:
            items.append({"idx": idx, "canonical_bld_id": b["canonical_bld_id"], "stratum": b["stratum"],
                          "sort_order": im["sort_order"], "image_asset_id": im["image_asset_id"]})
            idx += 1
    ok = []
    for it in items:
        r = flog.get(it["idx"])
        if r and r["status"] == "ok":
            p = ROOT / r["image_path"]
            s = sha(p)
            assert s == r["saved_sha256"], f"pixel SHA drift idx {it['idx']}"
            ok.append({**it, "image_path": r["image_path"], "pixel_sha256": s})
    # ---------------- embed (end-to-end timing: read + decode + resize + GPU forward + copy back)
    cfg1 = json.loads(CFG1.read_text(encoding="utf-8"))
    t_load0 = time.perf_counter()
    model_id, proc, model, errs = eng.load_model(cfg1, "cuda", torch.float16)
    torch.cuda.synchronize()
    t_load = time.perf_counter() - t_load0
    # warm-up on the first batch (not timed), then the timed full pass
    arrs = [eng.load_resized(str(ROOT / x["image_path"])) for x in ok[:BATCH]]
    eng.image_features(model, eng.to_pixel_values(arrs, cfg1, "cuda", torch.float16))
    torch.cuda.synchronize()
    torch.cuda.reset_peak_memory_stats()
    t0 = time.perf_counter()
    t_io = 0.0
    out = []
    for i in range(0, len(ok), BATCH):
        a0 = time.perf_counter()
        arrs = [eng.load_resized(str(ROOT / x["image_path"])) for x in ok[i:i + BATCH]]
        t_io += time.perf_counter() - a0
        pvv = eng.to_pixel_values(arrs, cfg1, "cuda", torch.float16)
        out.append(eng.image_features(model, pvv).cpu().numpy())
    torch.cuda.synchronize()
    t_emb = time.perf_counter() - t0
    Emb = np.concatenate(out).astype(np.float32)
    peak_vram = torch.cuda.max_memory_allocated() / 2 ** 30
    scale, bias = float(model.logit_scale.exp()), float(model.logit_bias)
    eidx = json.loads((EXP2 / "emb_index.json").read_text(encoding="utf-8"))
    assert abs(scale - eidx["logit_scale"]) < 1e-3 and abs(bias - eidx["logit_bias"]) < 1e-3
    # parity: re-embed 32 frozen eval images and compare with exp_v2/emb.npy
    ev_paths = [str(ROOT / r["image_path"]) for r in eidx["rows"][:32]]
    Eref = np.load(EXP2 / "emb.npy")[:32]
    Ee = eng.image_features(model, eng.to_pixel_values([eng.load_resized(p) for p in ev_paths], cfg1, "cuda",
                                                       torch.float16)).cpu().numpy()
    parity = {"n": 32, "max_abs_diff": float(np.abs(Ee - Eref).max()), "min_cosine": float((Ee * Eref).sum(1).min())}
    # ---------------- zs2 + probe
    ti = json.loads((EXP2 / "text_index.json").read_text(encoding="utf-8"))
    Tm = np.load(EXP2 / "text_emb.npy")
    phrase = {p: Tm[k] for k, p in enumerate(ti["phrases"])}
    cfg2 = json.loads(pv.CFG2.read_text(encoding="utf-8"))
    t1 = time.perf_counter()
    F = pv.build_features(Emb, phrase, cfg2, eidx["logit_scale"], eidx["logit_bias"])
    _, zs_names = pv.zs2_logits(Emb[:1], phrase, cfg2, eidx["logit_scale"], eidx["logit_bias"])
    X = F["hybrid_emb_zs2"]
    PE = {h: pv.proba(models[h], X) for h in pv.HEADS}
    t_probe = time.perf_counter() - t1
    preds = []
    for n, it in enumerate(ok):
        P = {h: PE[h][n] for h in pv.HEADS}
        lab, sc, _ = pv.cascade(P, cam_t, "scope")
        rs = min(sc["scope"]["margin"], sc["medium"]["margin"])
        preds.append({"idx": it["idx"], "canonical_bld_id": it["canonical_bld_id"], "stratum": it["stratum"],
                      "sort_order": it["sort_order"], "pixel_sha256": it["pixel_sha256"], "status": "ok",
                      "engine": "vnext_wp3_probe_v1/hybrid_emb_zs2", "contract_version": "vnext-gallery-semantics-contract-v1.4",
                      "labels": lab, "scores": sc, "route_score": round(rs, 6),
                      "routed_T_primary": bool(rs < T1), "routed_T_secondary": bool(rs < T2),
                      "cover_scores": {k: round(v, 6) for k, v in cc.cover_scores(P).items()},
                      "tier": pv.tier(lab),
                      "P": {h: {k: round(v, 6) for k, v in P[h].items()} for h in pv.HEADS}})
    for it in items:  # explicit not_run rows for fetch failures
        if it["idx"] not in {x["idx"] for x in ok}:
            preds.append({"idx": it["idx"], "canonical_bld_id": it["canonical_bld_id"], "stratum": it["stratum"],
                          "sort_order": it["sort_order"], "status": "not_run", "reason": "fetch_failed",
                          "routed_T_primary": None, "routed_T_secondary": None})
    preds.sort(key=lambda r: r["idx"])
    # ---------------- covers
    byb = defaultdict(list)
    for r in preds:
        if r["status"] == "ok":
            byb[r["canonical_bld_id"]].append({"idx": r["idx"], "labels": r["labels"], "cover_scores": r["cover_scores"]})
    covers = []
    for b in man["buildings"]:
        bid = b["canonical_bld_id"]
        rk = cc.rank_building(byb.get(bid, []), variant)
        conf = cc.is_confident(rk, A, M)
        covers.append({"canonical_bld_id": bid, "stratum": b["stratum"], "n_images_ok": len(byb.get(bid, [])),
                       "n_images_manifest": len(b["images"]), "default_idx": rk["default_idx"], "top3_idx": rk["top3_idx"],
                       "confidence_top1": rk["c1"], "confidence_top2": rk["c2"], "confidence_margin": rk["margin"],
                       "n_eligible": rk["n_eligible"], "default_tier": rk["tier1"], "score_variant": variant,
                       "local_confident": bool(conf), "review_required_local": rk["default_idx"] is None,
                       "codex_cover_needed_in_funnel": not conf})
    # ---------------- write
    np.save(OUT / "emb.npy", Emb)
    np.save(OUT / "zs2_logits.npy", F["zs2"])
    snap = None
    try:
        from huggingface_hub import scan_cache_dir
        for repo in scan_cache_dir().repos:
            if repo.repo_id == model_id:
                snap = sorted(r.commit_hash for r in repo.revisions)
    except Exception:
        pass
    eix = {"version": VERSION, "model": model_id, "model_snapshot": snap, "model_load_errors": errs,
           "transform": "squash resize 384 bilinear (== exp_v2 emb.npy / pool emb.npy / engine v1)",
           "dtype": "float16 forward, float32 stored, L2-normalised", "logit_scale": scale, "logit_bias": bias,
           "zs2_columns": zs_names, "parity_vs_exp_v2_emb": parity,
           "rows": [{k: x[k] for k in ("idx", "canonical_bld_id", "stratum", "image_path", "pixel_sha256")} for x in ok]}
    (OUT / "emb_index.json").write_text(json.dumps(eix, indent=1, ensure_ascii=False), encoding="utf-8")
    wj(OUT / "local_pred.jsonl", preds)
    wj(OUT / "local_covers.jsonl", covers)
    okp = [r for r in preds if r["status"] == "ok"]
    n = len(okp)
    run = {"version": VERSION, "tool": rel(Path(__file__)), "tool_sha256": sha(Path(__file__)),
           "preregistration_sha256": rec, "images_manifest": len(items), "images_ok": n,
           "images_not_run": len(items) - n, "buildings": len(covers),
           "routed_T_primary": sum(r["routed_T_primary"] for r in okp), "routed_T_secondary": sum(r["routed_T_secondary"] for r in okp),
           "routed_frac_T_primary": round(sum(r["routed_T_primary"] for r in okp) / n, 4),
           "routed_frac_T_secondary": round(sum(r["routed_T_secondary"] for r in okp) / n, 4),
           "local_confident_buildings": sum(c["local_confident"] for c in covers),
           "local_review_required_buildings": sum(c["review_required_local"] for c in covers),
           "label_distribution": {f: dict(Counter(str(r["labels"][f]) for r in okp).most_common()) for f in pv.FIELDS},
           "throughput": {"model_load_s": round(t_load, 2), "embed_images": n, "embed_wall_s": round(t_emb, 2),
                          "embed_images_per_s_end_to_end": round(n / t_emb, 2),
                          "embed_io_decode_resize_s": round(t_io, 2), "batch": BATCH, "peak_vram_gb": round(peak_vram, 2),
                          "probe_features_and_heads_s": round(t_probe, 3),
                          "note": "end-to-end = local JPEG read + decode + 384 squash resize + normalise + fp16 GPU forward + copy back; "
                                  "sequential single process, warm-up batch excluded, model load excluded"},
           "gpu": torch.cuda.get_device_name(0), "torch": torch.__version__, "parity_vs_exp_v2_emb": parity}
    (OUT / "local_run.json").write_text(json.dumps(run, indent=1, ensure_ascii=False), encoding="utf-8")
    fm = {"version": "vnext-wp3-n100-freeze-v1", "addendum_to": rel(PREREG), "preregistration_sha256": rec,
          "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
          "statement": "local predictions and local cover picks frozen BEFORE any codex label/cover call on holdout images and before any Claude judging",
          "files": {rel(OUT / f): sha(OUT / f) for f in ("local_pred.jsonl", "local_covers.jsonl", "emb.npy", "emb_index.json",
                                                         "zs2_logits.npy", "local_run.json", "fetch_log.jsonl", "fetch_run.json")},
          "tool": {rel(Path(__file__)): sha(Path(__file__))}}
    (OUT / "freeze_manifest.json").write_text(json.dumps(fm, indent=1), encoding="utf-8")
    print(json.dumps({k: run[k] for k in ("images_ok", "routed_frac_T_primary", "routed_frac_T_secondary",
                                          "local_confident_buildings", "local_review_required_buildings")}))
    print(json.dumps(run["throughput"]), json.dumps(parity))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
