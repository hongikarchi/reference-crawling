#!/usr/bin/env python3
"""WP3 teach: per-field linear probes trained on codex teacher labels, scored on Claude gold (offline, $0).

Train  : pool images (data/smoke_vnext/wp3_pool_v1: emb.npy + emb_index.json + codex_labels.jsonl).
         Teacher = codex compact labeler (tools/vnext_s3_codex_engine.py 0.2.0, frozen prompt d30e629e).
         Target values are the contract v1.4 emitted values (legibility high|low; camera may be
         `unknown`). Only `status == ok` codex rows are used; `unknown` / not_applicable targets are
         excluded from a head's training rows (exp_v2 rule).
Heads  : scope, reject_reason, medium, spatial_context, framing_scale, camera_angle, drawing_kind,
         project_state, project_legibility (+ optional joint scope head `oos9` = in_scope vs each
         reject reason). StandardScaler + LogisticRegression(class_weight='balanced'), C per head by
         5-fold GroupKFold over pool buildings (grid 0.01/0.1/1), agreement with codex.
         Contract §5 applicability is applied AFTER prediction (cascade scope -> reject -> medium ->
         view axes (aerial only if exterior) / drawing_kind / state / legibility).
Camera : v1.4 boundary abstention - predicted eye_level/elevated with margin < t becomes `unknown`;
         t chosen on pool out-of-fold predictions vs codex only (never on the eval set).
Features (variants): emb (SigLIP-2 so400m 1152-d), zs2 (config v2 zero-shot value logits only),
         hybrid_emb_zs2 (emb + zs2). zs2 logits = max over sub-concept prompt means, scale/bias of the
         model (exp_v2 raw_logits rule), prompt embeddings read from exp_v2/text_emb.npy.
Eval   : the 225 Claude-gold N10 images (never used for training; buildings and pixels disjoint from
         the pool by construction), v1.4 mapping + scoring via the unchanged tools/vnext_wp3_eval_v14.py
         (-> unchanged v1 scorer). Comparators recomputed the same way: exp_v2 combo, codex_v1.
Routing: route the lowest-margin X% eval images to codex (their codex_v1 labels replace the probe's);
         curves + the X that reaches scope/medium obvious errors 0 and pooled >= 95% (in-sample,
         optimistic) + a threshold fixed on pool OOF (honest).

Frozen models: data/smoke_vnext/wp3_pool_v1/probe_v1/<variant>.pkl + manifest.json (SHA per file).
Writes only under data/smoke_vnext/wp3_pool_v1/ (probe_v1/, eval_probe_v1.{md,json}, pred_probe_v1_*.jsonl).
No network, no Neon/R2, no model calls. Runs in the vision venv (numpy + scikit-learn).
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import pickle
import sys
import time
import warnings
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import vnext_wp3_eval as ev  # noqa: E402
from tools import vnext_wp3_eval_v14 as v14  # noqa: E402
from tools import vnext_wp3_codex_eval_v1 as cxe  # noqa: E402

POOL = ROOT / "data/smoke_vnext/wp3_pool_v1"
WP = ROOT / "data/smoke_vnext/wp3_n10_v1"
EXP2 = WP / "exp_v2"
OUT = POOL / "probe_v1"
CFG2 = ROOT / "tools/vnext_s3_local_engine_config_v2.json"
GOLD = WP / "gold_labels_final_v2.json"
VERSION = "vnext-wp3-probe-v1"
FIELDS = ev.FIELDS
NA = "not_applicable"
VIEW = ["spatial_context", "framing_scale", "camera_angle"]
ZS_FIELDS = ["scope", "medium", "spatial_context", "framing_scale", "camera_angle",
             "drawing_kind", "project_state", "project_legibility"]
HEADS = ["scope", "reject_reason", "medium", "spatial_context", "framing_scale", "camera_angle",
         "drawing_kind", "project_state", "project_legibility"]
C_GRID = [0.01, 0.1, 1.0]
N_FOLDS = 5
CAM_T_GRID = [round(x * 0.05, 2) for x in range(0, 13)]  # 0 .. 0.60
ROUTE_PCTS = (0, 5, 10, 13, 15, 20, 25, 30, 40, 50, 60, 80, 100)
VARIANTS = ["emb", "zs2", "hybrid_emb_zs2"]


def sha(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def rj(p: Path) -> list[dict]:
    with open(p, encoding="utf-8") as fh:
        return [json.loads(x) for x in fh if x.strip()]


def pct(x):
    return "-" if x is None else f"{100 * x:.1f}%"


# ====================================================================== features
def zs2_logits(E: np.ndarray, phrase: dict, cfg2: dict, scale: float, bias: float) -> tuple[np.ndarray, list]:
    cols, names = [], []
    for f in ZS_FIELDS:
        for v in sorted(cfg2["fields"][f]):
            S = []
            for sub, phr in cfg2["fields"][f][v].items():
                m = np.mean([phrase[p.lower()] for p in phr], axis=0)
                S.append(m / np.linalg.norm(m))
            cols.append((E @ np.stack(S).T).max(axis=1) * scale + bias)
            names.append(f"{f}:{v}")
    return np.stack(cols, axis=1).astype(np.float32), names


def build_features(E, phrase, cfg2, scale, bias) -> dict:
    Z, _ = zs2_logits(E, phrase, cfg2, scale, bias)
    return {"emb": E, "zs2": Z, "hybrid_emb_zs2": np.hstack([E, Z])}


# ====================================================================== targets (codex teacher)
def head_target(lab: dict, head: str):
    """Training target of one head from a codex v1.4 label row (None = row not used)."""
    ins, med = lab["in_scope"], lab["medium"]
    if head == "scope":
        return "true" if ins else "false"
    if head == "oos9":
        return "in_scope" if ins else lab["reject_reason"]
    if head == "reject_reason":
        return lab["reject_reason"] if not ins else None
    if head == "medium":
        return med if med not in (NA, "unknown") else None
    if not ins:
        return None
    v = lab[head]
    if v in (NA, "unknown"):
        return None
    if head in VIEW:
        return v if med in ("photograph", "rendering") else None
    if head == "drawing_kind":
        return v if med in ("drawing", "rendering") else None
    if head == "project_state":
        return v if med == "photograph" else None
    if head == "project_legibility":
        return v if v in ("high", "low") else None
    return None


class Const:
    def __init__(self, c):
        self.classes_ = np.array([c])

    def predict_proba(self, X):
        return np.ones((len(X), 1))


def fit_head(X, y, C):
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    y = np.asarray(y)
    if len(set(y.tolist())) < 2:
        return Const(y[0])
    m = make_pipeline(StandardScaler(), LogisticRegression(C=C, max_iter=5000, class_weight="balanced"))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        m.fit(X, y)
    return m


def proba(m, X) -> list[dict]:
    P = m.predict_proba(X)
    cl = [str(c) for c in m.classes_]
    return [dict(zip(cl, map(float, row))) for row in P]


def group_folds(groups: list[str], k: int) -> list[int]:
    """Deterministic building -> fold assignment by sha256 order (no RNG)."""
    blds = sorted(set(groups), key=lambda b: hashlib.sha256(("vnext-wp3-probe-v1-fold|" + b).encode()).hexdigest())
    f = {b: i % k for i, b in enumerate(blds)}
    return [f[g] for g in groups]


def select_C_and_oof(X, labs, folds, head) -> tuple[float, dict, list]:
    """-> (best C, {C: agreement}, OOF proba of best C (None where the head has no target row
    AND for every row we also return OOF proba so the cascade can run on all rows))."""
    tgt = [head_target(l, head) for l in labs]
    rows = [n for n, t in enumerate(tgt) if t is not None]
    res, oof_all = {}, {}
    for C in C_GRID:
        oof = [None] * len(labs)
        for k in range(N_FOLDS):
            tr = [n for n in rows if folds[n] != k]
            te = [n for n in range(len(labs)) if folds[n] == k]
            if not tr or not te:
                continue
            m = fit_head(X[tr], [tgt[n] for n in tr], C)
            for n, pr in zip(te, proba(m, X[te])):
                oof[n] = pr
        ok = sum(1 for n in rows if oof[n] and max(oof[n], key=oof[n].get) == tgt[n])
        res[C] = round(ok / len(rows), 4) if rows else 0.0
        oof_all[C] = oof
    best = max(C_GRID, key=lambda c: (res[c], -abs(np.log10(c) + 1)))  # tie -> closest to 0.1
    return best, res, oof_all[best]


# ====================================================================== cascade
def _top(pr: dict, allowed=None):
    items = [(k, v) for k, v in (pr or {}).items() if allowed is None or k in allowed]
    if not items:
        return None, 0.0
    z = sum(v for _, v in items) or 1.0
    items = sorted(((k, v / z) for k, v in items), key=lambda kv: (-kv[1], kv[0]))
    return items[0][0], items[0][1] - (items[1][1] if len(items) > 1 else 0.0)


def cascade(P: dict, cam_t: float, scope_mode: str = "scope") -> tuple[dict, dict, float]:
    """P = {head: proba dict} for one image -> (labels, scores, cover_score)."""
    sc = {}
    if scope_mode == "oos9" and P.get("oos9"):
        po = P["oos9"]
        pin = po.get("in_scope", 0.0)
        rej = {k: v for k, v in po.items() if k != "in_scope"}
        in_scope = pin >= 0.5
        best_rej = max(rej, key=rej.get) if rej else "isolated_product_artwork_or_sample"
    else:
        ps = P.get("scope") or {}
        pin = ps.get("true", 0.0) if ps else 1.0
        in_scope = pin >= 0.5
        best_rej = None
    sc["scope"] = {"label": "in_scope" if in_scope else "reject", "margin": round(float(abs(2 * pin - 1)), 4)}
    lab = {"in_scope": bool(in_scope)}
    if in_scope:
        lab["reject_reason"] = "none"
    else:
        if best_rej is None:
            best_rej, _ = _top(P.get("reject_reason"))
        lab["reject_reason"] = best_rej or "isolated_product_artwork_or_sample"
    med, m = _top(P["medium"])
    lab["medium"] = med
    sc["medium"] = {"label": med, "margin": round(float(m), 4)}
    if in_scope and med in ("photograph", "rendering"):
        sp, m = _top(P["spatial_context"]); lab["spatial_context"] = sp; sc["spatial_context"] = {"margin": round(float(m), 4)}
        fr, m = _top(P["framing_scale"]); lab["framing_scale"] = fr; sc["framing_scale"] = {"margin": round(float(m), 4)}
        allowed = None if sp == "exterior" else {"eye_level", "elevated"}
        ca, m = _top(P["camera_angle"], allowed)
        ca = ca or "eye_level"
        if ca in ("eye_level", "elevated") and m < cam_t:
            ca = "unknown"
        lab["camera_angle"] = ca; sc["camera_angle"] = {"margin": round(float(m), 4)}
    else:
        for a in VIEW:
            lab[a] = NA
    if in_scope and med in ("drawing", "rendering") and P.get("drawing_kind"):
        dk, m = _top(P["drawing_kind"]); lab["drawing_kind"] = dk; sc["drawing_kind"] = {"margin": round(float(m), 4)}
    else:
        lab["drawing_kind"] = NA
    if in_scope and med == "photograph" and P.get("project_state"):
        st, m = _top(P["project_state"]); lab["project_state"] = st; sc["project_state"] = {"margin": round(float(m), 4)}
    else:
        lab["project_state"] = NA
    if in_scope:
        lg, m = _top(P["project_legibility"]); lab["project_legibility"] = lg or "high"
        sc["project_legibility"] = {"margin": round(float(m), 4)}
    else:
        lab["project_legibility"] = "none"
        lab.update(spatial_context=NA, framing_scale=NA, camera_angle=NA, drawing_kind=NA, project_state=NA)
    cover = (P["medium"].get("photograph", 0.0) * (P.get("spatial_context") or {}).get("exterior", 0.0)
             * (P.get("framing_scale") or {}).get("overall", 0.0)
             * ((P.get("project_state") or {}).get("visibly_finished", 1.0)))
    return lab, sc, float(cover)


def tier(lab: dict) -> int:
    """exp_v2 _tier rule; v1.4 camera `unknown` (between eye_level and elevated) counts as non-aerial."""
    pe = (lab["in_scope"] and lab["medium"] == "photograph" and lab["spatial_context"] == "exterior"
          and lab["project_state"] == "visibly_finished")
    if pe and lab["framing_scale"] == "overall" and lab["camera_angle"] in ("eye_level", "elevated", "unknown"):
        return 1
    if pe and ((lab["framing_scale"] == "overall" and lab["camera_angle"] in ("aerial_oblique", "aerial_top_down"))
               or lab["framing_scale"] == "site_context"):
        return 2
    return 3


def cam_accept_vs_codex(pred: str, cx: str) -> bool:
    if pred == cx:
        return True
    if pred == "unknown" and cx in ("eye_level", "elevated"):
        return True
    if cx == "unknown" and pred in ("eye_level", "elevated"):
        return True
    return False


def choose_cam_t(oofP: list[dict], labs: list[dict], scope_mode: str) -> tuple[float, list]:
    curve = []
    for t in CAM_T_GRID:
        ok = n = 0
        for P, cl in zip(oofP, labs):
            if not (cl["in_scope"] and cl["medium"] in ("photograph", "rendering")):
                continue
            lab, _, _ = cascade(P, t, scope_mode)
            if lab["camera_angle"] == NA:
                continue
            n += 1
            ok += cam_accept_vs_codex(lab["camera_angle"], cl["camera_angle"])
        curve.append({"t": t, "camera_agreement_v14": round(ok / n, 4) if n else None, "n": n})
    best = max(curve, key=lambda r: (r["camera_agreement_v14"] or 0, -r["t"]))
    return best["t"], curve


# ====================================================================== scoring helpers
def score_v14(recs, gold14, names):
    run = v14.run_doc_from_tiers(recs, names)
    recs14, _ = v14.map_preds_v14(recs)
    s = v14.score(recs14, gold14, run)
    s["cover_rows"] = None
    return s


def cover_rows(recs, gold14, names):
    run = v14.run_doc_from_tiers(recs, names)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        e = ev.evaluate(v14.map_preds_v14(recs)[0], gold14, run)
    return [{k: b.get(k) for k in ("canonical_bld_id", "local_default_idx", "local_top3_idx", "acceptable_default_idx",
                                   "top1_ok", "top3_ok", "critical_reasons")} for b in e["lane_b"]["buildings"]]


def err_lists(recs, gold14):
    gi = {g["idx"]: g for g in gold14["labels"]}
    sc, md = [], []
    for r in recs:
        g = gi.get(r["idx"])
        if g is None:
            continue
        if not ev.is_accepted(r["labels"], g, "in_scope"):
            sc.append({"idx": r["idx"], "bld": r["canonical_bld_id"], "gold_in_scope": g["in_scope"],
                       "gold_reject": g["reject_reason"], "pred_reject": r["labels"]["reject_reason"]})
        if not ev.is_accepted(r["labels"], g, "medium"):  # == v1 scorer obvious-error rule (independent of scope)
            md.append({"idx": r["idx"], "bld": r["canonical_bld_id"], "gold": g["medium"], "pred": r["labels"]["medium"]})
    return sc, md


def route_score(rec, mode):
    s = rec["scores"]
    if mode == "scope_medium":
        return min(s["scope"]["margin"], s["medium"]["margin"])
    return min(float(b["margin"]) for b in s.values() if isinstance(b, dict) and b.get("margin") is not None)


def routed_merge(recs, codex_by_idx, routed: set):
    out = []
    for r in recs:
        if r["idx"] in routed:
            x = copy.deepcopy(r)
            x["labels"] = dict(codex_by_idx[r["idx"]]["labels"])
            out.append(x)
        else:
            out.append(r)
    return out


def routing_analysis(recs, codex_recs, gold14, names, pool_thresholds: dict) -> dict:
    cbi = {r["idx"]: r for r in codex_recs}
    gi = {g["idx"]: g for g in gold14["labels"]}
    out = {}
    for mode in ("scope_medium", "all_fields"):
        order = sorted(recs, key=lambda r: (route_score(r, mode), r["idx"]))
        curve = []
        n = len(order)
        for p in ROUTE_PCTS:
            k = round(n * p / 100)
            routed = {r["idx"] for r in order[:k]}
            merged = routed_merged = routed_merge(recs, cbi, routed)
            s = score_v14(merged, gold14, names)
            se, me = err_lists(merged, gold14)
            local_se = sum(1 for e in se if e["idx"] not in routed)
            local_me = sum(1 for e in me if e["idx"] not in routed)
            curve.append({"route_pct": p, "routed": k, "pooled": s["pooled"], "scope_err": s["scope_obvious_errors"],
                          "medium_err": s["medium_obvious_errors"], "scope_err_unrouted": local_se,
                          "medium_err_unrouted": local_me})
            del routed_merged
        # minimal k (exact, in-sample) to route every probe scope/medium error, and to reach pooled >= 95%
        se0, me0 = err_lists(recs, gold14)
        err_idx = {e["idx"] for e in se0 + me0}
        pos = {r["idx"]: i for i, r in enumerate(order)}
        k_catch = (max(pos[i] for i in err_idx) + 1) if err_idx else 0
        k95 = None
        for k in range(0, n + 1):
            routed = {r["idx"] for r in order[:k]}
            if score_v14(routed_merge(recs, cbi, routed), gold14, names)["pooled"] >= 0.95:
                k95 = k
                break
        merged = routed_merge(recs, cbi, {r["idx"] for r in order[:k_catch]})
        s = score_v14(merged, gold14, names)
        res = {"curve": curve,
               "catch_all_probe_scope_medium_errors": {"routed": k_catch, "route_pct": round(k_catch / n, 4),
                                                       "pooled_after": s["pooled"], "scope_err_after": s["scope_obvious_errors"],
                                                       "medium_err_after": s["medium_obvious_errors"]},
               "pooled_95": {"routed": k95, "route_pct": round(k95 / n, 4) if k95 is not None else None}}
        res["pool_oof_thresholds"] = {}
        for key in ("catch_all", "catch_95", "catch_90"):
            t = (pool_thresholds.get(mode) or {}).get(key)
            if t is None:
                continue
            routed = {r["idx"] for r in recs if route_score(r, mode) < t["threshold"]}
            merged = routed_merge(recs, cbi, routed)
            s = score_v14(merged, gold14, names)
            se, me = err_lists(merged, gold14)
            res["pool_oof_thresholds"][key] = {**t, "eval_routed": len(routed), "eval_route_pct": round(len(routed) / n, 4),
                                               "pooled": s["pooled"], "scope_err": s["scope_obvious_errors"],
                                               "medium_err": s["medium_obvious_errors"],
                                               "scope_err_unrouted": sum(1 for e in se if e["idx"] not in routed),
                                               "medium_err_unrouted": sum(1 for e in me if e["idx"] not in routed)}
        out[mode] = res
    return out


def pool_oof_thresholds(oof_recs, labs) -> dict:
    """Honest routing threshold from pool OOF only: the smallest margin threshold that routes every
    OOF probe-vs-codex scope/medium disagreement (catch-all), and the one routing 99% of them."""
    out = {}
    for mode in ("scope_medium", "all_fields"):
        dis = []
        allm = []
        for r, cl in zip(oof_recs, labs):
            m = route_score(r, mode)
            allm.append(m)
            d_scope = r["labels"]["in_scope"] != cl["in_scope"]
            d_med = (r["labels"]["in_scope"] and cl["in_scope"] and cl["medium"] != "unknown"
                     and r["labels"]["medium"] != cl["medium"])
            if d_scope or d_med:
                dis.append(m)
        allm = np.array(allm)
        res = {"n_pool": len(allm), "n_disagree_scope_or_medium": len(dis)}
        for q, name in ((1.0, "catch_all"), (0.95, "catch_95"), (0.90, "catch_90")):
            if not dis:
                continue
            thr = float(np.quantile(np.array(dis), q)) + 1e-9
            res[name] = {"threshold": round(thr, 6), "pool_route_pct": round(float((allm < thr).mean()), 4)}
        out[mode] = res
    return out


# ====================================================================== main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--variants", default=",".join(VARIANTS))
    ap.add_argument("--labels", default=str(POOL / "codex_labels.jsonl"), help="teacher labels")
    ap.add_argument("--out-root", default=str(POOL), help="output root (scratch dir for dry tests)")
    a = ap.parse_args(argv)
    global OUT
    out_root = Path(a.out_root)
    OUT = out_root / "probe_v1"
    labels_p = Path(a.labels)
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    # ---------------- inputs
    pidx = json.loads((POOL / "emb_index.json").read_text(encoding="utf-8"))
    Ep = np.load(POOL / "emb.npy")
    eidx = json.loads((EXP2 / "emb_index.json").read_text(encoding="utf-8"))
    Ee = np.load(EXP2 / "emb.npy")
    assert pidx["model"] == eidx["model"]
    ti = json.loads((EXP2 / "text_index.json").read_text(encoding="utf-8"))
    T = np.load(EXP2 / "text_emb.npy")
    phrase = {p: T[k] for k, p in enumerate(ti["phrases"])}
    cfg2 = json.loads(CFG2.read_text(encoding="utf-8"))
    scale, bias = eidx["logit_scale"], eidx["logit_bias"]
    cx_rows = {r["idx"]: r for r in rj(labels_p)}
    keep = [k for k, r in enumerate(pidx["rows"]) if cx_rows.get(r["idx"], {}).get("status") == "ok"]
    prow = [pidx["rows"][k] for k in keep]
    labs = [cx_rows[r["idx"]]["labels"] for r in prow]
    groups = [r["canonical_bld_id"] for r in prow]
    folds = group_folds(groups, N_FOLDS)
    feats_p = {k: v[keep] for k, v in build_features(Ep, phrase, cfg2, scale, bias).items()}
    feats_e = build_features(Ee, phrase, cfg2, scale, bias)
    # pixel / building disjointness re-check (eval vs pool)
    ev_px = {r["pixel_sha256"] for r in eidx["rows"]}
    ev_bld = {r["canonical_bld_id"] for r in eidx["rows"]}
    overlap = {"pixel": len(ev_px & {r["pixel_sha256"] for r in prow}),
               "building": len(ev_bld & set(groups))}
    assert overlap == {"pixel": 0, "building": 0}, overlap
    gold = json.loads(GOLD.read_text(encoding="utf-8"))
    gold14, gstats = v14.map_gold_v14(gold)
    man = json.loads((WP / "sample_manifest.json").read_text(encoding="utf-8"))
    names = {b["canonical_bld_id"]: (b["name"], b["stratum"]) for b in man["buildings"]}

    # teacher label distribution
    dist = {f: dict(Counter(str(l[f]) for l in labs).most_common()) for f in FIELDS}

    # ---------------- comparators (recomputed, v1.4)
    comp = {}
    combo = rj(EXP2 / "pred_combo_zsScope_probe_zs_logits_only.jsonl")
    s = score_v14(combo, gold14, names)
    comp["exp_v2_combo_zsScope_probe_zs_logits_only"] = s
    cx_eval_rows = rj(WP / "codex_v1/labels.jsonl")
    codex_recs, _ = cxe.codex_records(cx_eval_rows)
    cover_run = cxe.cover_run_doc(rj(WP / "codex_v1/cover.jsonl"), names)
    recs14, _ = v14.map_preds_v14(codex_recs)
    comp["codex_v1"] = v14.score(recs14, gold14, cover_run)
    comp["codex_v1"]["cover_source"] = "comparative cover call (codex_v1/cover.jsonl)"

    # ---------------- train / select / eval per variant
    report = {"version": VERSION, "tool": "tools/vnext_wp3_probe_v1.py", "tool_sha256": sha(Path(__file__)),
              "inputs": {"pool_emb.npy": sha(POOL / "emb.npy"), "pool_emb_index.json": sha(POOL / "emb_index.json"),
                         "codex_labels.jsonl": sha(labels_p),
                         "eval_emb.npy (exp_v2)": sha(EXP2 / "emb.npy"), "text_emb.npy (exp_v2)": sha(EXP2 / "text_emb.npy"),
                         "config_v2": sha(CFG2), "gold_labels_final_v2.json": sha(GOLD),
                         "codex_v1/labels.jsonl": sha(WP / "codex_v1/labels.jsonl"),
                         "codex_v1/cover.jsonl": sha(WP / "codex_v1/cover.jsonl"),
                         "exp_v2 combo pred": sha(EXP2 / "pred_combo_zsScope_probe_zs_logits_only.jsonl")},
              "train": {"pool_images_with_ok_codex": len(prow), "pool_images_total": len(pidx["rows"]),
                        "buildings": len(set(groups)), "folds": N_FOLDS, "teacher_distribution": dist,
                        "head_train_rows": {h: sum(1 for l in labs if head_target(l, h) is not None)
                                            for h in HEADS + ["oos9"]}},
              "eval_pool_overlap": overlap, "gold_mapping_stats": gstats, "comparators": comp, "variants": {},
              "paid_calls": 0, "network_requests": 0}
    preds_all = {}
    manifest = {"version": VERSION, "files": {}, "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "sklearn": __import__("sklearn").__version__, "numpy": np.__version__}
    for var in a.variants.split(","):
        t1 = time.time()
        Xp, Xe = feats_p[var], feats_e[var]
        Cs, Cres, oof = {}, {}, {}
        for h in HEADS + ["oos9"]:
            Cs[h], Cres[h], oof[h] = select_C_and_oof(Xp, labs, folds, h)
        oofP = [{h: oof[h][n] for h in HEADS + ["oos9"]} for n in range(len(labs))]
        vr = {"C": Cs, "C_grid_agreement_oof": {h: {str(c): v for c, v in d.items()} for h, d in Cres.items()}}
        for scope_mode in ("scope", "oos9"):
            cam_t, cam_curve = choose_cam_t(oofP, labs, scope_mode)
            # pool OOF cascade (for honest routing thresholds + OOF agreement with the teacher)
            oof_recs = []
            for n, P in enumerate(oofP):
                lab, sc, cov = cascade(P, cam_t, scope_mode)
                oof_recs.append({"idx": prow[n]["idx"], "labels": lab, "scores": sc})
            agree = {f: round(float(np.mean([ev.norm(r["labels"][f]) == ev.norm(l[f]) or
                                             (f == "camera_angle" and cam_accept_vs_codex(r["labels"][f], l[f]))
                                             for r, l in zip(oof_recs, labs)])), 4) for f in FIELDS}
            thr = pool_oof_thresholds(oof_recs, labs)
            # final models on the full pool
            models = {h: fit_head(Xp[[n for n, l in enumerate(labs) if head_target(l, h) is not None]],
                                  [head_target(l, h) for l in labs if head_target(l, h) is not None], Cs[h])
                      for h in HEADS + ["oos9"]}
            PE = {h: proba(models[h], Xe) for h in HEADS + ["oos9"]}
            recs = []
            for n, r in enumerate(eidx["rows"]):
                P = {h: PE[h][n] for h in PE}
                lab, sc, cov = cascade(P, cam_t, scope_mode)
                recs.append({"idx": r["idx"], "canonical_bld_id": r["canonical_bld_id"], "stratum": r["stratum"],
                             "labels": lab, "scores": sc, "cover": {"tier": tier(lab), "score": round(cov, 6)}})
            name = f"{var}" + ("" if scope_mode == "scope" else "+oos9")
            s = score_v14(recs, gold14, names)
            s["cover_rows"] = cover_rows(recs, gold14, names)
            se, me = err_lists(recs, gold14)
            s["scope_errors_list"], s["medium_errors_list"] = se, me
            vr[scope_mode] = {"name": name, "cam_t": cam_t, "cam_t_curve_pool_oof": cam_curve,
                              "pool_oof_agreement_with_codex": agree,
                              "pool_oof_pooled_agreement": round(float(np.mean(list(agree.values()))), 4),
                              "pool_oof_routing_thresholds": thr, "eval": s,
                              "routing": routing_analysis(recs, codex_recs, gold14, names,
                                                          thr)}
            # transparency: same models without camera abstention (t = 0)
            recs0 = []
            for n, r in enumerate(eidx["rows"]):
                lab0, sc0, cov0 = cascade({h: PE[h][n] for h in PE}, 0.0, scope_mode)
                recs0.append({"idx": r["idx"], "canonical_bld_id": r["canonical_bld_id"], "stratum": r["stratum"],
                              "labels": lab0, "scores": sc0, "cover": {"tier": tier(lab0), "score": round(cov0, 6)}})
            s0 = score_v14(recs0, gold14, names)
            vr[scope_mode]["eval_cam_t0"] = {"pooled": s0["pooled"], "camera_angle": s0["per_field"]["camera_angle"],
                                             "cover_top1": s0["cover_top1"], "cover_critical": s0["cover_critical"]}
            vr[scope_mode]["eval_camera_unknown_n"] = sum(1 for x in recs if x["labels"]["camera_angle"] == "unknown")
            preds_all[name] = recs
            if scope_mode == "scope" or True:
                fp = OUT / f"{name}.pkl"
                with open(fp, "wb") as fh:
                    pickle.dump({"version": VERSION, "variant": var, "scope_mode": scope_mode, "cam_t": cam_t,
                                 "C": Cs, "models": models, "feature": var,
                                 "feature_spec": {"emb": "SigLIP-2 so400m squash-384 L2 image embedding",
                                                  "zs2": "config v2 zero-shot value logits (sorted field:value)",
                                                  "hybrid_emb_zs2": "concat(emb, zs2)"}[var],
                                 "config_v2_sha256": sha(CFG2)}, fh)
                manifest["files"][fp.name] = {"sha256": sha(fp), "variant": var, "scope_mode": scope_mode,
                                              "cam_t": cam_t, "C": Cs}
            print(var, scope_mode, "pooled", s["pooled"], "scope", s["scope_obvious_errors"], "medium",
                  s["medium_obvious_errors"], "cover", s["cover_top1"], s["cover_critical"], "cam_t", cam_t,
                  round(time.time() - t1, 1), "s", flush=True)
        report["variants"][var] = vr
    # best by eval pooled (selection on eval is reported as such)
    cand = [(v, m) for v in report["variants"] for m in ("scope", "oos9")]
    best_v, best_m = max(cand, key=lambda vm: (report["variants"][vm[0]][vm[1]]["eval"]["pooled"],
                                               -report["variants"][vm[0]][vm[1]]["eval"]["scope_obvious_errors"]))
    # pre-registered primary = hybrid_emb_zs2 + scope head (named in the task); best-on-eval = post hoc
    prim = report["variants"].get("hybrid_emb_zs2", report["variants"][best_v])["scope"]
    report["primary"] = prim["name"]
    report["best_on_eval_post_hoc"] = report["variants"][best_v][best_m]["name"]
    for name, recs in preds_all.items():
        with open(out_root / f"pred_probe_v1_{name}.jsonl", "w", encoding="utf-8") as fh:
            for r in recs:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    report["model_manifest_sha256"] = sha(OUT / "manifest.json")
    report["wall_s"] = round(time.time() - t0, 1)
    (out_root / "eval_probe_v1.json").write_text(json.dumps(report, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    (out_root / "eval_probe_v1.md").write_text(render(report), encoding="utf-8")
    p = prim["eval"]
    print(f"primary {prim['name']} pooled={pct(p['pooled'])} scope={p['scope_obvious_errors']} medium={p['medium_obvious_errors']} "
          f"cover={p['cover_top1']} crit={p['cover_critical']}; best={report['best_on_eval_post_hoc']}; "
          f"codex pooled={pct(comp['codex_v1']['pooled'])}; wall {report['wall_s']} s")
    return 0


# ====================================================================== report
SHORT = v14.SHORT


def _row(name, s, note=""):
    return ("| " + name + " | " + " | ".join(pct(s["per_field"][f]) for f in FIELDS)
            + f" | **{pct(s['pooled'])}** | {s['scope_obvious_errors']} | {s['medium_obvious_errors']} | "
              f"{s['cover_top1']} / {s['cover_top3']} / {s['cover_critical']} | {note} |")


def render(rep: dict) -> str:
    L = []
    prim_name = rep["primary"]
    pv = next(rep["variants"][v][m] for v in rep["variants"] for m in ("scope", "oos9")
              if rep["variants"][v][m]["name"] == prim_name)
    p = pv["eval"]
    cx = rep["comparators"]["codex_v1"]
    L.append(f"# WP3 teach: codex teacher -> linear probe v1, scored on Claude gold (`{VERSION}`)\n")
    L.append(f"WP3 teach: probe pooled={pct(p['pooled'])} scope_err={p['scope_obvious_errors']} "
             f"medium_err={p['medium_obvious_errors']} cover_top1={p['cover_top1']} | codex pooled={pct(cx['pooled'])}\n")
    tr = rep["train"]
    L.append(f"- train: {tr['pool_images_with_ok_codex']} pool images with an ok codex label (of {tr['pool_images_total']} "
             f"embedded) / {tr['buildings']} buildings; teacher = codex compact 0.2.0 (gpt-6.1-sol, effort low, frozen label prompt)")
    L.append(f"- eval: 225 Claude-gold N10 images / 10 buildings, contract v1.4 mapping, unchanged v1.4 scorer; "
             f"pool∩eval overlap pixels {rep['eval_pool_overlap']['pixel']}, buildings {rep['eval_pool_overlap']['building']}")
    L.append("- probe = StandardScaler + LogisticRegression(class_weight=balanced), C per head by 5-fold GroupKFold over pool "
             "buildings vs codex; §5 applicability after prediction; camera `unknown` threshold t from pool OOF only")
    L.append(f"- primary (pre-registered) = `{prim_name}`; best on eval (post hoc) = `{rep['best_on_eval_post_hoc']}`")
    L.append("- cost $0 (local GPU + CPU); no network, no Neon/R2, no model calls in this stage\n")
    L.append("## 1. Eval-225 under v1.4\n")
    L.append("| engine | " + " | ".join(SHORT[f] for f in FIELDS) + " | pooled | scope err | medium err | cover top1 / top3 / crit | note |")
    L.append("|---|" + "---:|" * len(FIELDS) + "---:|---:|---:|---|---|")
    for v, vr in rep["variants"].items():
        for m in ("scope", "oos9"):
            r = vr[m]
            L.append(_row(f"probe {r['name']}", r["eval"],
                          f"cam_t={r['cam_t']} (eval unknown n={r['eval_camera_unknown_n']}; t=0: pooled "
                          f"{pct(r['eval_cam_t0']['pooled'])}, camera {pct(r['eval_cam_t0']['camera_angle'])}); "
                          f"pool OOF agreement {pct(r['pool_oof_pooled_agreement'])}"))
    L.append(_row("exp_v2 combo zsScope+probe_zs_logits_only", rep["comparators"]["exp_v2_combo_zsScope_probe_zs_logits_only"],
                  "trained on gold LOBO (exploratory)"))
    L.append(_row("codex_v1 (teacher)", cx, "cover = comparative cover call"))
    L.append("")
    L.append("Lane A = scope/medium obvious errors 0 AND pooled ≥ 90%. Lane B = cover top-1 10/10 AND critical 0.\n")
    L.append(f"## 2. Primary `{prim_name}`: errors\n")
    L.append(f"- scope errors ({p['scope_obvious_errors']}): {p['scope_error_kinds']}")
    for e in p["scope_errors_list"]:
        L.append(f"  - idx {e['idx']} {e['bld']}: gold in_scope={e['gold_in_scope']} ({e['gold_reject']}), pred reject={e['pred_reject']}")
    L.append(f"- medium errors ({p['medium_obvious_errors']}): {p['medium_error_kinds']}")
    for e in p["medium_errors_list"]:
        L.append(f"  - idx {e['idx']} {e['bld']}: gold {e['gold']} → pred {e['pred']}")
    L.append("- cover rows:")
    for c in p["cover_rows"]:
        L.append(f"  - {c['canonical_bld_id']}: default {c['local_default_idx']} top3 {c['local_top3_idx']} acceptable "
                 f"{c['acceptable_default_idx']} top1 {'OK' if c['top1_ok'] else 'MISS'}"
                 + (f" CRIT {c['critical_reasons']}" if c.get("critical_reasons") else ""))
    L.append("")
    L.append("## 3. Routing lowest-margin X% of eval images to codex (codex_v1 labels replace the probe's)\n")
    for v, vr in rep["variants"].items():
        for m in ("scope", "oos9"):
            r = vr[m]
            if r["name"] not in (prim_name, rep["best_on_eval_post_hoc"]):
                continue
            for mode, ra in r["routing"].items():
                L.append(f"### `{r['name']}`, routing score = {mode} margin\n")
                L.append("| route % | routed | pooled | scope err | medium err | scope err unrouted | medium err unrouted |")
                L.append("|---:|---:|---:|---:|---:|---:|---:|")
                for c in ra["curve"]:
                    L.append(f"| {c['route_pct']} | {c['routed']} | {pct(c['pooled'])} | {c['scope_err']} | {c['medium_err']} | "
                             f"{c['scope_err_unrouted']} | {c['medium_err_unrouted']} |")
                ca, p95 = ra["catch_all_probe_scope_medium_errors"], ra["pooled_95"]
                L.append(f"\n- in-sample (optimistic): routing **{pct(ca['route_pct'])}** ({ca['routed']}) catches every probe "
                         f"scope/medium error → pooled {pct(ca['pooled_after'])}, remaining scope/medium err "
                         f"{ca['scope_err_after']}/{ca['medium_err_after']} (= codex's own errors on routed images)")
                L.append(f"- in-sample pooled ≥ 95% first reached at routing {pct(p95['route_pct'])} ({p95['routed']})")
                for key, po in (ra.get("pool_oof_thresholds") or {}).items():
                    L.append(f"- honest: threshold fixed on pool OOF ({key} of probe-vs-codex scope/medium disagreements; "
                             f"{po['threshold']:.4f}, pool route {pct(po['pool_route_pct'])}) → eval route {pct(po['eval_route_pct'])}, "
                             f"pooled {pct(po['pooled'])}, scope/medium err {po['scope_err']}/{po['medium_err']} "
                             f"(unrouted {po['scope_err_unrouted']}/{po['medium_err_unrouted']})")
                L.append("")
    L.append("## 4. Pool OOF (probe vs codex teacher, 5-fold by building)\n")
    L.append("| variant | " + " | ".join(SHORT[f] for f in FIELDS) + " | mean | cam_t |")
    L.append("|---|" + "---:|" * len(FIELDS) + "---:|---:|")
    for v, vr in rep["variants"].items():
        for m in ("scope", "oos9"):
            r = vr[m]
            L.append(f"| {r['name']} | " + " | ".join(pct(r["pool_oof_agreement_with_codex"][f]) for f in FIELDS)
                     + f" | {pct(r['pool_oof_pooled_agreement'])} | {r['cam_t']} |")
    L.append("")
    L.append("## 5. Teacher label distribution (pool, codex ok rows)\n")
    for f, d in tr["teacher_distribution"].items():
        L.append(f"- {f}: {d}")
    L.append(f"\nhead training rows: {tr['head_train_rows']}\n")
    L.append("## 6. Frozen models\n")
    L.append(f"- `data/smoke_vnext/wp3_pool_v1/probe_v1/manifest.json` sha `{rep['model_manifest_sha256']}` (per-file sha inside)")
    for v, vr in rep["variants"].items():
        L.append(f"- {v}: C {vr['C']}")
    L.append("\n## 7. Inputs\n")
    for k, v in rep["inputs"].items():
        L.append(f"- `{k}` `{v}`")
    L.append(f"- tool `tools/vnext_wp3_probe_v1.py` `{rep['tool_sha256']}`")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
