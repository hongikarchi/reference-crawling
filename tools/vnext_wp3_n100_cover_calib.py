#!/usr/bin/env python3
"""WP3 N100 pre-registration support: local cover-confidence calibration (NO holdout data).

Uses ONLY the training pool (data/smoke_vnext/wp3_pool_v1) and the N10 eval-225 set
(data/smoke_vnext/wp3_n10_v1). Never opens the N100 holdout images or any holdout prediction.
All inputs are read-only; outputs go to data/smoke_vnext/wp3_n100_v1/calib/ (new files).

Stages (vision venv C:/Users/user/venvs/vnext-vision, repo root):
  select   deterministic choice of <= 60 pool random-part buildings for a codex comparative
           cover call (calibration reference). Rule: random_building part only, 8 <= ok images
           <= 30 (= holdout eligibility range), sha256('vnext-wp3-n100-cover-calib-v1|'+bld)
           ascending, first 60. Writes calib/cover_calib_buildings.json.
  probs    per-image head probabilities of the FROZEN primary probe (probe_v1/hybrid_emb_zs2.pkl):
           - eval-225: frozen pickle applied to exp_v2/emb.npy (parity vs pred_probe_v1_hybrid_emb_zs2.jsonl)
           - pool: 5-fold building OOF with the frozen per-head C and the probe's own fold function
             (the frozen pickle saw every pool image, so in-sample pool probabilities would be optimistic)
           -> calib/probs_eval225.jsonl, calib/probs_pool_oof.jsonl
  analyze  cover-confidence rule grid on eval-225 (Claude gold acceptable sets) + pool calibration
           buildings (codex comparative cover acceptable sets, critical via pool codex image labels)
           -> calib/cover_calib_report.json (+ .md)
No network, no Neon/R2, no model calls (the codex cover call itself is run with tools/vnext_s3_codex_engine.py).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pickle
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import vnext_wp3_probe_v1 as pv  # noqa: E402  (unchanged frozen probe code)
from tools import vnext_wp3_eval as ev  # noqa: E402

POOL = ROOT / "data/smoke_vnext/wp3_pool_v1"
WP = ROOT / "data/smoke_vnext/wp3_n10_v1"
EXP2 = WP / "exp_v2"
OUT = ROOT / "data/smoke_vnext/wp3_n100_v1/calib"
PKL = POOL / "probe_v1/hybrid_emb_zs2.pkl"
CALIB_SALT = "vnext-wp3-n100-cover-calib-v1|"
CALIB_N = 60
VERSION = "vnext-wp3-n100-cover-calib-v1"

A_GRID = [round(x * 0.05, 2) for x in range(0, 20)] + [0.97, 0.98, 0.99]
M_GRID = [0.0, 0.02, 0.05, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5]
SCORE_VARIANTS = ("s5", "s4")  # s5 = P_in*P_photo*P_ext*P_overall*P_finished ; s4 = without P_finished


def sha(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def rj(p: Path) -> list[dict]:
    with open(p, encoding="utf-8") as fh:
        return [json.loads(x) for x in fh if x.strip()]


def wj(p: Path, rows):
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


# ====================================================================== cover score (shared with the N100 local tool)
def cover_scores(P: dict) -> dict:
    g = lambda h, k, d=0.0: float((P.get(h) or {}).get(k, d))  # noqa: E731
    pin = g("scope", "true", 1.0)
    base = pin * g("medium", "photograph") * g("spatial_context", "exterior") * g("framing_scale", "overall")
    return {"p_in_scope": pin, "s4": base, "s5": base * g("project_state", "visibly_finished", 1.0)}


def rank_building(recs: list[dict], variant: str) -> dict:
    """recs: [{idx, labels, cover_scores}] of one building.
    Eligible = hard probe tier < 3 (tier() of the frozen probe: in_scope photo exterior visibly_finished +
    overall non-aerial (1) / aerial overall or site_context (2)). Order (tier, -score, idx).
    confidence c1 = score of #1, margin = c1 - score of #2 (0 if only one eligible)."""
    el = []
    for r in recs:
        t = pv.tier(r["labels"])
        if t < 3:
            el.append((t, -r["cover_scores"][variant], r["idx"]))
    el.sort()
    if not el:
        return {"default_idx": None, "top3_idx": [], "c1": 0.0, "c2": 0.0, "margin": 0.0, "n_eligible": 0,
                "tier1": None}
    c1 = -el[0][1]
    c2 = -el[1][1] if len(el) > 1 else 0.0
    return {"default_idx": el[0][2], "top3_idx": [x[2] for x in el[:3]], "c1": round(c1, 6), "c2": round(c2, 6),
            "margin": round(c1 - c2, 6), "n_eligible": len(el), "tier1": el[0][0]}


def is_confident(rk: dict, A: float, M: float) -> bool:
    return rk["default_idx"] is not None and rk["c1"] >= A and rk["margin"] >= M


# ====================================================================== select
def stage_select(_a) -> int:
    man = json.loads((POOL / "pool_manifest.json").read_text(encoding="utf-8"))
    by = defaultdict(list)
    for im in man["images"]:
        if im["selection_reason"] == "random_building" and im["fetch_status"] == "ok":
            by[im["canonical_bld_id"]].append(im)
    elig = [b for b, v in by.items() if 8 <= len(v) <= 30]
    order = sorted(elig, key=lambda b: hashlib.sha256((CALIB_SALT + b).encode()).hexdigest())
    pick = order[:CALIB_N]
    doc = {"version": VERSION, "rule": f"pool random_building part, 8<=ok images<=30, sha256('{CALIB_SALT}'+bld) asc, first {CALIB_N}",
           "pool_manifest_sha256": sha(POOL / "pool_manifest.json"), "eligible_buildings": len(elig),
           "random_buildings": len(by), "buildings": sorted(pick),
           "images": sum(len(by[b]) for b in pick),
           "per_stratum": dict(Counter(by[b][0]["stratum"] for b in pick))}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "cover_calib_buildings.json").write_text(json.dumps(doc, indent=1), encoding="utf-8")
    print(json.dumps({k: doc[k] for k in ("eligible_buildings", "images", "per_stratum")}))
    return 0


# ====================================================================== probs
def load_inputs():
    pidx = json.loads((POOL / "emb_index.json").read_text(encoding="utf-8"))
    Ep = np.load(POOL / "emb.npy")
    eidx = json.loads((EXP2 / "emb_index.json").read_text(encoding="utf-8"))
    Ee = np.load(EXP2 / "emb.npy")
    ti = json.loads((EXP2 / "text_index.json").read_text(encoding="utf-8"))
    T = np.load(EXP2 / "text_emb.npy")
    phrase = {p: T[k] for k, p in enumerate(ti["phrases"])}
    cfg2 = json.loads(pv.CFG2.read_text(encoding="utf-8"))
    return pidx, Ep, eidx, Ee, phrase, cfg2


def stage_probs(_a) -> int:
    pidx, Ep, eidx, Ee, phrase, cfg2 = load_inputs()
    scale, bias = eidx["logit_scale"], eidx["logit_bias"]
    with open(PKL, "rb") as fh:
        M = pickle.load(fh)
    assert M["variant"] == "hybrid_emb_zs2" and M["scope_mode"] == "scope"
    cam_t, Cs, models = M["cam_t"], M["C"], M["models"]
    heads = pv.HEADS
    # ---- eval-225 with the frozen models
    Xe = pv.build_features(Ee, phrase, cfg2, scale, bias)["hybrid_emb_zs2"]
    PE = {h: pv.proba(models[h], Xe) for h in heads}
    ref = {r["idx"]: r for r in rj(POOL / "pred_probe_v1_hybrid_emb_zs2.jsonl")}
    out_e, mism = [], 0
    for n, r in enumerate(eidx["rows"]):
        P = {h: PE[h][n] for h in heads}
        lab, sc, _ = pv.cascade(P, cam_t, "scope")
        mism += lab != ref[r["idx"]]["labels"]
        out_e.append({"idx": r["idx"], "canonical_bld_id": r["canonical_bld_id"], "labels": lab, "scores": sc,
                      "P": P, "cover_scores": cover_scores(P)})
    wj(OUT / "probs_eval225.jsonl", out_e)
    # ---- pool OOF with frozen C, probe fold function
    cx = {r["idx"]: r for r in rj(POOL / "codex_labels.jsonl")}
    keep = [k for k, r in enumerate(pidx["rows"]) if cx.get(r["idx"], {}).get("status") == "ok"]
    prow = [pidx["rows"][k] for k in keep]
    labs = [cx[r["idx"]]["labels"] for r in prow]
    folds = pv.group_folds([r["canonical_bld_id"] for r in prow], pv.N_FOLDS)
    Xp = pv.build_features(Ep, phrase, cfg2, scale, bias)["hybrid_emb_zs2"][keep]
    oof = {h: [None] * len(prow) for h in heads}
    for h in heads:
        tgt = [pv.head_target(l, h) for l in labs]
        rows = [n for n, t in enumerate(tgt) if t is not None]
        for k in range(pv.N_FOLDS):
            tr = [n for n in rows if folds[n] != k]
            te = [n for n in range(len(prow)) if folds[n] == k]
            m = pv.fit_head(Xp[tr], [tgt[n] for n in tr], Cs[h])
            for n, pr in zip(te, pv.proba(m, Xp[te])):
                oof[h][n] = pr
    out_p = []
    for n, r in enumerate(prow):
        P = {h: oof[h][n] for h in heads}
        lab, sc, _ = pv.cascade(P, cam_t, "scope")
        out_p.append({"idx": r["idx"], "canonical_bld_id": r["canonical_bld_id"], "labels": lab, "scores": sc,
                      "P": P, "cover_scores": cover_scores(P)})
    wj(OUT / "probs_pool_oof.jsonl", out_p)
    meta = {"version": VERSION, "pickle": str(PKL.relative_to(ROOT)).replace("\\", "/"), "pickle_sha256": sha(PKL),
            "cam_t": cam_t, "C": Cs, "eval_label_mismatch_vs_frozen_pred": int(mism), "eval_n": len(out_e),
            "pool_oof_n": len(out_p),
            "inputs": {"pool_emb.npy": sha(POOL / "emb.npy"), "codex_labels.jsonl": sha(POOL / "codex_labels.jsonl"),
                       "exp_v2_emb.npy": sha(EXP2 / "emb.npy"), "text_emb.npy": sha(EXP2 / "text_emb.npy"),
                       "config_v2": sha(pv.CFG2)},
            "probs_eval225.jsonl": sha(OUT / "probs_eval225.jsonl"), "probs_pool_oof.jsonl": sha(OUT / "probs_pool_oof.jsonl")}
    (OUT / "probs_meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    print(json.dumps({k: meta[k] for k in ("eval_label_mismatch_vs_frozen_pred", "eval_n", "pool_oof_n")}))
    return 0


# ====================================================================== analyze
def crit_from_codex(lab: dict) -> list[str]:
    return ev.critical_cover_reasons(lab)


def build_cases():
    """-> list of calibration buildings: {set, bld, recs, acceptable, ref_label_by_idx}"""
    cases = []
    gold = json.loads((WP / "gold_labels_final_v2.json").read_text(encoding="utf-8"))
    gl = {g["idx"]: g for g in gold["labels"]}
    gc = {c["canonical_bld_id"]: c for c in gold.get("covers", [])}
    pe = rj(OUT / "probs_eval225.jsonl")
    by = defaultdict(list)
    for r in pe:
        by[r["canonical_bld_id"]].append(r)
    for b, recs in sorted(by.items()):
        if b not in gc:
            continue
        cases.append({"set": "eval225_claude_gold", "bld": b, "recs": recs,
                      "acceptable": list(gc[b].get("acceptable_default_idx") or []), "ref": gl})
    sel = json.loads((OUT / "cover_calib_buildings.json").read_text(encoding="utf-8"))["buildings"]
    cov = {r["canonical_bld_id"]: r for r in rj(OUT / "codex_cover_pool/cover.jsonl") if r.get("status") == "ok"}
    cxl = {r["idx"]: r["labels"] for r in rj(POOL / "codex_labels.jsonl") if r.get("status") == "ok"}
    pp = rj(OUT / "probs_pool_oof.jsonl")
    byp = defaultdict(list)
    for r in pp:
        byp[r["canonical_bld_id"]].append(r)
    for b in sel:
        if b not in cov:
            continue
        cases.append({"set": "pool_codex_cover", "bld": b, "recs": byp[b], "acceptable": cov[b]["acceptable_idx"],
                      "codex_ranked": cov[b]["ranked_idx"], "ref": cxl})
    return cases


def eval_case(c, variant):
    rk = rank_building(c["recs"], variant)
    d = rk["default_idx"]
    acc = c["acceptable"]
    if d is None:
        status = "null_ok" if not acc else "null_miss"
        crit = []
    else:
        crit = crit_from_codex(c["ref"][d]) if d in c["ref"] else ["no_reference_label"]
        status = "ok" if d in acc else "miss"
    top3_ok = (d is None and not acc) or any(i in acc for i in rk["top3_idx"])
    return {**rk, "status": status, "critical": crit, "top3_ok": top3_ok}


def stage_analyze(_a) -> int:
    cases = build_cases()
    rep = {"version": VERSION, "n_cases": len(cases), "per_set": dict(Counter(c["set"] for c in cases)), "variants": {}}
    for v in SCORE_VARIANTS:
        rows = []
        for c in cases:
            e = eval_case(c, v)
            rows.append({"set": c["set"], "bld": c["bld"], **e, "acceptable": c["acceptable"],
                         "n_images": len(c["recs"])})
        grid = []
        for A in A_GRID:
            for M in M_GRID:
                conf = [r for r in rows if is_confident(r, A, M)]
                grid.append({"A": A, "M": M, "confident": len(conf),
                             "confident_eval": sum(r["set"] == "eval225_claude_gold" for r in conf),
                             "confident_pool": sum(r["set"] == "pool_codex_cover" for r in conf),
                             "confident_critical": sum(1 for r in conf if r["critical"]),
                             "confident_miss": sum(1 for r in conf if r["status"] != "ok")})
        uncond = {"buildings": len(rows), "top1_ok": sum(r["status"] in ("ok", "null_ok") for r in rows),
                  "critical": sum(1 for r in rows if r["critical"]),
                  "null": sum(1 for r in rows if r["default_idx"] is None),
                  "top3_ok": sum(1 for r in rows if r["top3_ok"])}
        bad = sorted([r for r in rows if r["critical"] or r["status"] == "miss"], key=lambda r: -r["c1"])
        rep["variants"][v] = {"unconditional": uncond, "grid": grid, "rows": rows,
                              "max_c1_critical": max([r["c1"] for r in rows if r["critical"]], default=None),
                              "max_c1_miss": max([r["c1"] for r in rows if r["status"] == "miss"], default=None),
                              "bad_rows": [{k: r[k] for k in ("set", "bld", "default_idx", "c1", "margin", "status",
                                                               "critical", "acceptable", "top3_idx")} for r in bad]}
    (OUT / "cover_calib_report.json").write_text(json.dumps(rep, indent=1, ensure_ascii=False), encoding="utf-8")
    for v, r in rep["variants"].items():
        print(v, json.dumps(r["unconditional"]), "max_c1_crit", r["max_c1_critical"], "max_c1_miss", r["max_c1_miss"])
        for b in r["bad_rows"]:
            print("   bad", json.dumps(b))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("stage", choices=["select", "probs", "analyze"])
    a = ap.parse_args(argv)
    return {"select": stage_select, "probs": stage_probs, "analyze": stage_analyze}[a.stage](a)


if __name__ == "__main__":
    raise SystemExit(main())
