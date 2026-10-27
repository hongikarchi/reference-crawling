#!/usr/bin/env python3
"""vNext S3 local shallow-semantics engine (WP3 / P4B engine comparison, local half).

Runs in the separate vision venv (C:/Users/user/venvs/vnext-vision; torch cu128).
Zero-shot SigLIP-2 (fallback SigLIP) over the contract's compact fields
(docs/vnext/CONTRACT_GALLERY_SEMANTICS.md §4-§6), deterministic applicability
(§5), per-image confidence proxy (softmax top prob + margin to runner-up) and a
per-building default-cover ranking (docs/vnext/CONTRACT_COVERS.md §1/§3).

Prompts/decision config are frozen in tools/vnext_s3_local_engine_config_v1.json
(SHA recorded in the run record). No network except Hugging Face weights.

Usage (from repo root):
  C:/Users/user/venvs/vnext-vision/Scripts/python.exe tools/vnext_s3_local_engine.py \
      --wp-dir data/smoke_vnext/wp3_n10_v1 --bench 500
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import torch
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "tools/vnext_s3_local_engine_config_v1.json"

REJECTS = ["unreadable_or_blank", "non_architectural_subject", "people_or_event",
           "isolated_product_artwork_or_sample", "text_or_logo_only", "no_project_visible",
           "multi_panel_without_dominant_image"]
VIEW_AXES = ["spatial_context", "framing_scale", "camera_angle"]


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ---------------------------------------------------------------- model
def load_model(cfg: dict, device: str, dtype):
    from transformers import AutoModel, AutoProcessor
    errors = []
    for mid in cfg["model_preference"]:
        try:
            proc = AutoProcessor.from_pretrained(mid)
            model = AutoModel.from_pretrained(mid, dtype=dtype).to(device).eval()
            return mid, proc, model, errors
        except Exception as e:  # record and fall back
            errors.append({"model": mid, "error": repr(e)[:500]})
    raise RuntimeError(f"no model loaded: {errors}")


def _feat(x):
    if isinstance(x, torch.Tensor):
        return x
    for k in ("pooler_output", "image_embeds", "text_embeds"):
        v = getattr(x, k, None)
        if v is not None:
            return v
    raise TypeError(type(x))


def build_text_bank(cfg, proc, model, device):
    """-> {field: {value: tensor[n_sub, D]}} normalised sub-concept embeddings."""
    tp = cfg["text_preprocess"]
    flat, keys = [], []
    for field, values in cfg["fields"].items():
        for value, subs in values.items():
            for sub, phr in subs.items():
                for p in phr:
                    flat.append(p.lower() if tp["lowercase"] else p)
                    keys.append((field, value, sub))
    tok = proc.tokenizer(flat, padding=tp["padding"], max_length=tp["max_length"],
                         truncation=True, return_tensors="pt").to(device)
    with torch.no_grad():
        emb = _feat(model.get_text_features(**tok)).float()
    emb = torch.nn.functional.normalize(emb, dim=-1)
    groups: dict = {}
    for (f, v, s), e in zip(keys, emb):
        groups.setdefault(f, {}).setdefault(v, {}).setdefault(s, []).append(e)
    bank = {}
    for f, vals in groups.items():
        bank[f] = {}
        for v, subs in vals.items():
            sub_embs = [torch.nn.functional.normalize(torch.stack(es).mean(0), dim=-1) for es in subs.values()]
            bank[f][v] = torch.stack(sub_embs)
    return bank, len(flat)


# ---------------------------------------------------------------- images
def load_resized(path: str, size=(384, 384)) -> np.ndarray:
    with Image.open(path) as im:
        im = im.convert("RGB").resize(size, Image.Resampling.BILINEAR)
        return np.asarray(im, dtype=np.uint8)


def to_pixel_values(arrs: list[np.ndarray], cfg, device, dtype):
    ip = cfg["image_preprocess"]
    x = torch.from_numpy(np.stack(arrs)).to(device).permute(0, 3, 1, 2).float() * ip["rescale"]
    mean = torch.tensor(ip["mean"], device=device).view(1, 3, 1, 1)
    std = torch.tensor(ip["std"], device=device).view(1, 3, 1, 1)
    return ((x - mean) / std).to(dtype)


def image_features(model, pv):
    with torch.no_grad():
        return torch.nn.functional.normalize(_feat(model.get_image_features(pixel_values=pv)).float(), dim=-1)


# ---------------------------------------------------------------- decisions
def value_logits(img_f, bank_field, scale, bias):
    """img_f [D] -> {value: logit} (max over sub-concepts)."""
    return {v: float((e @ img_f).max() * scale + bias) for v, e in bank_field.items()}


def softmax(d: dict) -> dict:
    ks = list(d)
    a = np.array([d[k] for k in ks], dtype=np.float64)
    a = np.exp(a - a.max())
    a /= a.sum()
    return dict(zip(ks, a.tolist()))


def pick(logits: dict, active: list[str], unknown_min: float | None):
    sub = {k: logits[k] for k in active}
    p = softmax(sub)
    order = sorted(p, key=lambda k: (-p[k], k))
    top, top_p = order[0], p[order[0]]
    margin = top_p - (p[order[1]] if len(order) > 1 else 0.0)
    label = top
    if unknown_min is not None and top_p < unknown_min:
        label = "unknown"
    return {"label": label, "argmax": top, "top_prob": round(top_p, 4), "margin": round(margin, 4),
            "sigmoid": round(1 / (1 + np.exp(-logits[top])), 4),
            "probs": {k: round(v, 4) for k, v in sorted(p.items(), key=lambda kv: -kv[1])}}


def decide(img_f, bank, scale, bias, cfg):
    dc = cfg["decision"]
    um = dc["unknown_min_top_prob"]
    L = {f: value_logits(img_f, bank[f], scale, bias) for f in bank}
    out, flags = {}, []
    # scope
    s = L["scope"]
    best_rej = max(REJECTS, key=lambda k: (s[k], k))
    in_scope = s["in_scope"] >= s[best_rej] - dc["scope_reject_logit_margin"]
    sp = softmax(s)
    out["scope"] = {"label": "in_scope" if in_scope else best_rej,
                    "top_prob": round(sp["in_scope"] if in_scope else sp[best_rej], 4),
                    "margin": round(abs(sp["in_scope"] - sp[best_rej]), 4),
                    "best_reject": best_rej, "logit_in_scope": round(s["in_scope"], 3),
                    "logit_best_reject": round(s[best_rej], 3),
                    "probs": {k: round(v, 4) for k, v in sorted(sp.items(), key=lambda kv: -kv[1])}}
    lab = {"in_scope": bool(in_scope), "reject_reason": "none" if in_scope else best_rej}
    na = "not_applicable"
    # medium has no not_applicable value in §4, so it is always the engine's medium decision;
    # out-of-scope rows get every other axis not_applicable and legibility none (§5).
    out["medium"] = pick(L["medium"], list(L["medium"]), um["medium"])
    lab["medium"] = out["medium"]["label"]
    if not in_scope:
        lab.update(spatial_context=na, framing_scale=na, camera_angle=na,
                   drawing_kind=na, project_state=na, project_legibility="none")
    med = lab["medium"]
    view_on = in_scope and med in ("photograph", "rendering")
    dk_on = in_scope and med in ("drawing", "rendering")
    st_on = in_scope and med == "photograph"
    if view_on:
        out["spatial_context"] = pick(L["spatial_context"], list(L["spatial_context"]), um["spatial_context"])
        out["framing_scale"] = pick(L["framing_scale"], list(L["framing_scale"]), um["framing_scale"])
        sc = out["spatial_context"]["label"]
        cam_active = list(L["camera_angle"]) if sc in ("exterior", "unknown") else ["eye_level", "elevated"]
        out["camera_angle"] = pick(L["camera_angle"], cam_active, um["camera_angle"])
        raw = max(L["camera_angle"], key=lambda k: (L["camera_angle"][k], k))
        out["camera_angle"]["raw_argmax"] = raw
        if raw != out["camera_angle"]["argmax"]:
            flags.append("camera_aerial_masked_by_spatial_context")
        for a in VIEW_AXES:
            lab[a] = out[a]["label"]
    else:
        for a in VIEW_AXES:
            lab[a] = na
    if dk_on:
        out["drawing_kind"] = pick(L["drawing_kind"], list(L["drawing_kind"]), um["drawing_kind"])
        lab["drawing_kind"] = out["drawing_kind"]["label"]
        if lab["drawing_kind"] == "unknown":
            flags.append(dc["drawing_kind_unknown_advisory_flag"])
    else:
        lab["drawing_kind"] = na
    if st_on:
        out["project_state"] = pick(L["project_state"], list(L["project_state"]), um["project_state"])
        lab["project_state"] = out["project_state"]["label"]
    else:
        lab["project_state"] = na
    if in_scope:
        out["project_legibility"] = pick(L["project_legibility"], ["high", "medium", "low"], um["project_legibility"])
        lab["project_legibility"] = out["project_legibility"]["label"]
    # derived semantic types (§6)
    tags = []
    sc, fr, ca, ps = lab["spatial_context"], lab["framing_scale"], lab["camera_angle"], lab["project_state"]
    if in_scope:
        if sc == "exterior" and fr == "overall":
            tags.append("exterior_overall")
        if sc == "exterior" and fr == "site_context":
            tags.append("exterior_context")
        if sc == "interior":
            tags.append("interior")
        if sc == "threshold":
            tags.append("threshold")
        if lab["medium"] in ("drawing", "rendering", "physical_model"):
            tags.append(lab["medium"])
        if ca in ("aerial_oblique", "aerial_top_down"):
            tags.append("aerial")
        if fr in ("element_detail", "material_detail"):
            tags.append("detail")
        if ps in ("construction_visible", "demolition_visible"):
            tags.append("construction_archive")
    # cover
    pm = {f: softmax(L[f]) for f in ("medium", "spatial_context", "framing_scale", "camera_angle",
                                     "project_legibility", "project_state")}
    p_scope = 1 / (1 + np.exp(-(s["in_scope"] - s[best_rej])))
    comps = [p_scope, pm["medium"]["photograph"], pm["spatial_context"]["exterior"],
             pm["framing_scale"]["overall"], pm["camera_angle"]["eye_level"] + pm["camera_angle"]["elevated"],
             pm["project_legibility"]["high"], pm["project_state"]["visibly_finished"]]
    cover_score = float(np.mean(comps))
    photo_ext = in_scope and lab["medium"] == "photograph" and sc == "exterior" and ps == "visibly_finished"
    if photo_ext and fr == "overall" and ca in ("eye_level", "elevated"):
        tier = 1
    elif photo_ext and ((fr == "overall" and ca in ("aerial_oblique", "aerial_top_down")) or fr == "site_context"):
        tier = 2
    else:
        tier = 3
    return lab, out, tags, flags, {"tier": tier, "score": round(cover_score, 4)}


# ---------------------------------------------------------------- main
def run_batches(model, paths, cfg, device, dtype, bs, pool):
    """Yield (batch_index, feats, t_load, t_gpu). Loading of batch k+1 overlaps GPU of k."""
    chunks = [paths[i:i + bs] for i in range(0, len(paths), bs)]
    fut = pool.submit(lambda c: list(pool.map(load_resized, c)), chunks[0]) if chunks else None
    for k, c in enumerate(chunks):
        t0 = time.perf_counter()
        arrs = fut.result()
        t_load = time.perf_counter() - t0
        if k + 1 < len(chunks):
            fut = pool.submit(lambda c: list(pool.map(load_resized, c)), chunks[k + 1])
        t1 = time.perf_counter()
        pv = to_pixel_values(arrs, cfg, device, dtype)
        f = image_features(model, pv)
        torch.cuda.synchronize()
        yield k, f, t_load, time.perf_counter() - t1, len(c)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wp-dir", type=Path, default=ROOT / "data/smoke_vnext/wp3_n10_v1")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--bench", type=int, default=500)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
    wp = args.wp_dir if args.wp_dir.is_absolute() else ROOT / args.wp_dir
    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    cfg_sha = sha256_file(args.config)
    assert torch.cuda.is_available(), "CUDA required"
    device, dtype = "cuda", getattr(torch, cfg["dtype"])
    bs = cfg["batch_size"]

    manifest = json.loads((wp / "sample_manifest.json").read_text(encoding="utf-8"))
    flog = {}
    for line in (wp / "fetch_log.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            flog[r["idx"]] = r
    items = []
    for b in manifest["buildings"]:
        for im in b["images"]:
            items.append((b, im, flog.get(im["idx"])))

    torch.cuda.reset_peak_memory_stats()
    t_load_model = time.perf_counter()
    model_id, proc, model, load_errors = load_model(cfg, device, dtype)
    t_load_model = time.perf_counter() - t_load_model
    scale = float(model.logit_scale.exp())
    bias = float(model.logit_bias)
    bank, n_prompts = build_text_bank(cfg, proc, model, device)

    ok_items = [(b, im, r) for b, im, r in items if r and r.get("status") == "ok"]
    paths = [str(ROOT / r["image_path"]) for _, _, r in ok_items]

    # preprocessing parity vs the HF processor (first 4 images)
    pv_ours = to_pixel_values([load_resized(p) for p in paths[:4]], cfg, device, torch.float32)
    pv_hf = proc.image_processor(images=[Image.open(p).convert("RGB") for p in paths[:4]],
                                 return_tensors="pt")["pixel_values"].to(device)
    parity = float((pv_ours - pv_hf).abs().max())

    pool = ThreadPoolExecutor(max_workers=args.workers)
    # warmup (excluded)
    for _ in run_batches(model, paths[:bs], cfg, device, dtype, bs, pool):
        pass

    feats, t_gpu, t_wall = [], 0.0, time.perf_counter()
    for k, f, tl, tg, n in run_batches(model, paths, cfg, device, dtype, bs, pool):
        feats.append(f)
        t_gpu += tg
    torch.cuda.synchronize()
    t_wall = time.perf_counter() - t_wall
    feats = torch.cat(feats) if feats else torch.empty(0)
    sample_ips_e2e = len(paths) / t_wall
    sample_ips_gpu = len(paths) / t_gpu

    # benchmark: sample images repeated to N, warmup batch excluded
    bench = {}
    if args.bench:
        bpaths = [paths[i % len(paths)] for i in range(args.bench + bs)]
        tw, tg_sum, n_img, t_start = 0.0, 0.0, 0, None
        for k, f, tl, tg, n in run_batches(model, bpaths, cfg, device, dtype, bs, pool):
            if k == 0:
                t_start = time.perf_counter()
                continue
            n_img += n
            tg_sum += tg
        tw = time.perf_counter() - t_start
        bench = {"images_timed": n_img, "warmup_images_excluded": bs, "wall_s": round(tw, 3),
                 "images_per_sec_end_to_end": round(n_img / tw, 2),
                 "images_per_sec_gpu_forward_only": round(n_img / tg_sum, 2),
                 "note": "end_to_end = disk JPEG decode + resize(8 threads, overlapped) + GPU forward; excludes network fetch"}
    peak = torch.cuda.max_memory_allocated()
    pool.shutdown()

    rows = []
    fi = 0
    feat_of = {}
    for (b, im, r) in ok_items:
        feat_of[im["idx"]] = feats[fi]
        fi += 1
    for b, im, r in items:
        base = {"idx": im["idx"], "canonical_bld_id": b["canonical_bld_id"], "stratum": b["stratum"],
                "image_asset_id": im["image_asset_id"], "sort_order": im["sort_order"],
                "engine": cfg["engine"], "engine_version": cfg["engine_version"], "model": model_id,
                "contract_version": cfg["contract_version"], "config_version": cfg["config_version"],
                "config_sha256": cfg_sha, "transform_version": "vnext-wp3-fetch-v1+resize384bilinear"}
        if im["idx"] not in feat_of:
            rows.append({**base, "status": "failed_input", "error_code": (r or {}).get("error_code", "missing_fetch_row")})
            continue
        lab, sc, tags, flags, cover = decide(feat_of[im["idx"]], bank, scale, bias, cfg)
        rows.append({**base, "status": "ok", "image_path": r["image_path"], "pixel_sha256": r["saved_sha256"],
                     "labels": lab, "scores": sc, "derived_semantic_types": tags,
                     "advisory_flags": flags, "cover": cover})
    # per-building default-cover ranking
    buildings = {}
    for b in manifest["buildings"]:
        cand = [x for x in rows if x["canonical_bld_id"] == b["canonical_bld_id"] and x["status"] == "ok"]
        cand.sort(key=lambda x: (x["cover"]["tier"], -x["cover"]["score"], x["idx"]))
        for rank, x in enumerate(cand, 1):
            x["cover"]["rank_in_building"] = rank
        elig = [x for x in cand if x["cover"]["tier"] < 3]
        top = elig[0] if elig else None
        buildings[b["canonical_bld_id"]] = {
            "name": b["name"], "stratum": b["stratum"], "n_images": len(cand),
            "n_tier1": sum(x["cover"]["tier"] == 1 for x in cand),
            "n_tier2": sum(x["cover"]["tier"] == 2 for x in cand),
            "default_cover_idx": top["idx"] if top else None,
            "default_cover_tier": top["cover"]["tier"] if top else None,
            "default_cover_score": top["cover"]["score"] if top else None,
            "default_cover_margin_to_next": (round(elig[0]["cover"]["score"] - elig[1]["cover"]["score"], 4)
                                             if len(elig) > 1 and elig[0]["cover"]["tier"] == elig[1]["cover"]["tier"] else None),
            "top3_idx": [x["idx"] for x in elig[:3]],
            "review_required": top is None,
        }
    with open(wp / "local_labels.jsonl", "w", encoding="utf-8") as f:
        for x in rows:
            f.write(json.dumps(x, ensure_ascii=False) + "\n")

    import transformers
    import PIL
    snap = None
    try:
        from huggingface_hub import scan_cache_dir
        for repo in scan_cache_dir().repos:
            if repo.repo_id == model_id:
                snap = sorted(r.commit_hash for r in repo.revisions)
    except Exception:
        pass
    dist = {}
    for a in ["in_scope", "reject_reason", "medium", "spatial_context", "framing_scale", "camera_angle",
              "drawing_kind", "project_state", "project_legibility"]:
        c = {}
        for x in rows:
            if x["status"] == "ok":
                v = str(x["labels"][a])
                c[v] = c.get(v, 0) + 1
        dist[a] = dict(sorted(c.items(), key=lambda kv: -kv[1]))
    run = {
        "run_version": "vnext-wp3-n10-local-v1", "tool": "tools/vnext_s3_local_engine.py",
        "tool_sha256": sha256_file(Path(__file__)),
        "config": str(args.config.relative_to(ROOT)).replace("\\", "/"), "config_sha256": cfg_sha,
        "manifest_sha256": sha256_file(wp / "sample_manifest.json"),
        "fetch_log_sha256": sha256_file(wp / "fetch_log.jsonl"),
        "model": model_id, "model_snapshot": snap, "model_load_fallback_errors": load_errors,
        "logit_scale": round(scale, 4), "logit_bias": round(bias, 4), "n_prompts": n_prompts,
        "versions": {"python": platform.python_version(), "torch": torch.__version__, "cuda": torch.version.cuda,
                     "cudnn": torch.backends.cudnn.version(), "transformers": transformers.__version__,
                     "pillow": PIL.__version__, "numpy": np.__version__},
        "device": {"name": torch.cuda.get_device_name(0), "capability": list(torch.cuda.get_device_capability(0)),
                   "total_vram_gb": round(torch.cuda.get_device_properties(0).total_memory / 2**30, 2)},
        "dtype": cfg["dtype"], "batch_size": bs, "loader_threads": args.workers,
        "model_load_s": round(t_load_model, 2), "preprocess_parity_max_abs_diff_vs_hf": round(parity, 5),
        "sample": {"images_total": len(items), "images_labeled": len(paths),
                   "images_failed_input": len(items) - len(paths), "wall_s": round(t_wall, 3),
                   "images_per_sec_end_to_end": round(sample_ips_e2e, 2),
                   "images_per_sec_gpu_forward_only": round(sample_ips_gpu, 2), "warmup_excluded": True},
        "benchmark": bench,
        "peak_vram_allocated_gb": round(peak / 2**30, 3),
        "extrapolation_699038": ({"images": 699038,
                                  "hours_end_to_end_local_disk": round(699038 / bench["images_per_sec_end_to_end"] / 3600, 2),
                                  "hours_gpu_forward_only": round(699038 / bench["images_per_sec_gpu_forward_only"] / 3600, 2),
                                  "excludes": "image download (network-bound; separate verified-pixel pass)"} if bench else None),
        "label_distribution": dist,
        "buildings": buildings,
    }
    (wp / "local_run.json").write_text(json.dumps(run, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: run[k] for k in ("model", "sample", "benchmark", "peak_vram_allocated_gb", "extrapolation_699038")}, indent=1))
    print(json.dumps(dist, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
