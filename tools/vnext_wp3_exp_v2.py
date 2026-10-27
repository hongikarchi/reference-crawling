#!/usr/bin/env python3
"""WP3 exp v2: free local experiments on the N10 gold v2 (225 images / 10 buildings), $0.

Runs in the vision venv (needs numpy + scikit-learn; torch only for the v1 parity check
through tools/vnext_s3_local_engine.decide). Inputs: cached embeddings from
tools/vnext_wp3_exp_v2_embed.py, gold_labels_final_v2.json, config v1/v2. Scoring uses
the unchanged tools/vnext_wp3_eval.py `evaluate` (acceptance = gold primary or judged
acceptable alternative). Writes ONLY under data/smoke_vnext/wp3_n10_v1/exp_v2/.

Stages
  zs     zero-shot: v1 parity re-score from cache, config v2 base (untuned), and config v2
         tuned on fold A -> reported on fold B and vice versa (prompt-set choice per field +
         per-value logit offsets, coordinate search on the tuning fold only)
  probe  per-field logistic-regression probe on SigLIP embeddings, leave-one-building-out,
         C chosen by nested inner LOBO; contract applicability applied after prediction;
         variants: emb, emb TTA (full+center+flip mean), zero-shot logits only, hybrid
  budget LOBO learning curves (random image subsets; building-count subsets) + rough
         power-law extrapolation
  hard   camera_angle / legibility softening proposals (merge levels, abstain-as-unknown)

No network, no Neon/R2, no paid calls.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import sys
import time
import warnings
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import vnext_wp3_eval as ev  # noqa: E402

WP = ROOT / "data/smoke_vnext/wp3_n10_v1"
OUT = WP / "exp_v2"
CFG1 = ROOT / "tools/vnext_s3_local_engine_config_v1.json"
CFG2 = ROOT / "tools/vnext_s3_local_engine_config_v2.json"
GOLD = WP / "gold_labels_final_v2.json"
EXP_VERSION = "vnext-wp3-exp-v2"
FOLD_SALT = "vnext-wp3-exp-v2-fold|"

FIELDS = ev.FIELDS
ZS_FIELDS = ["scope", "medium", "spatial_context", "framing_scale", "camera_angle",
             "drawing_kind", "project_state", "project_legibility"]
REJECTS = ["unreadable_or_blank", "non_architectural_subject", "people_or_event",
           "isolated_product_artwork_or_sample", "text_or_logo_only", "no_project_visible",
           "multi_panel_without_dominant_image"]
NA = "not_applicable"
VIEW = ["spatial_context", "framing_scale", "camera_angle"]
OFFSET_GRID = [x / 2 for x in range(-12, 13)]  # -6 .. +6 step 0.5
C_GRID = [0.01, 0.1, 1.0]


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def softmax(a: np.ndarray) -> np.ndarray:
    a = np.asarray(a, dtype=np.float64)
    e = np.exp(a - a.max())
    return e / e.sum()


# ====================================================================== data
class Data:
    def __init__(self):
        self.idx_doc = json.loads((OUT / "emb_index.json").read_text(encoding="utf-8"))
        self.rows = self.idx_doc["rows"]
        self.idx = [r["idx"] for r in self.rows]
        self.pos = {i: k for k, i in enumerate(self.idx)}
        self.bld = [r["canonical_bld_id"] for r in self.rows]
        self.stratum = [r["stratum"] for r in self.rows]
        self.E = np.load(OUT / "emb.npy")
        Ec, Ef = np.load(OUT / "emb_center.npy"), np.load(OUT / "emb_flip.npy")
        t = self.E + Ec + Ef
        self.E_tta = t / np.linalg.norm(t, axis=1, keepdims=True)
        self.scale, self.bias = self.idx_doc["logit_scale"], self.idx_doc["logit_bias"]
        ti = json.loads((OUT / "text_index.json").read_text(encoding="utf-8"))
        T = np.load(OUT / "text_emb.npy")
        self.phrase = {p: T[k] for k, p in enumerate(ti["phrases"])}
        self.cfg1 = json.loads(CFG1.read_text(encoding="utf-8"))
        self.cfg2 = json.loads(CFG2.read_text(encoding="utf-8"))
        self.gold = json.loads(GOLD.read_text(encoding="utf-8"))
        self.g = {x["idx"]: x for x in self.gold["labels"]}
        assert sorted(self.g) == sorted(self.idx)
        self.acc = {i: {f: ev.accepted_set(self.g[i], f) for f in FIELDS} for i in self.idx}
        bl = self.idx_doc["rows"]
        self.blank = np.array([r["pixel_stats"]["gray_std"] <= self.cfg2["decision"]["blank_rule"]["gray_std_max"]
                               and r["pixel_stats"]["edge_mean"] <= self.cfg2["decision"]["blank_rule"]["edge_mean_max"]
                               for r in bl])
        self.buildings = sorted(set(self.bld), key=lambda b: hashlib.sha256((FOLD_SALT + b).encode()).hexdigest())
        self.fold = {"A": self.buildings[:5], "B": self.buildings[5:]}
        self.names = {}
        man = json.loads((WP / "sample_manifest.json").read_text(encoding="utf-8"))
        for b in man["buildings"]:
            self.names[b["canonical_bld_id"]] = (b["name"], b["stratum"])

    def rows_of(self, blds) -> list[int]:
        s = set(blds)
        return [k for k, b in enumerate(self.bld) if b in s]


# ====================================================================== zero-shot
def raw_logits(D: Data, cfg_fields: dict, E: np.ndarray) -> dict:
    """{field: {value: [N] logit}} = max over sub-concepts (normalised phrase means)."""
    out = {}
    for field, values in cfg_fields.items():
        out[field] = {}
        for value, subs in values.items():
            S = []
            for sub, phr in subs.items():
                m = np.mean([D.phrase[p.lower()] for p in phr], axis=0)
                S.append(m / np.linalg.norm(m))
            S = np.stack(S)
            out[field][value] = (E @ S.T).max(axis=1) * D.scale + D.bias
    return out


def raw_sets(D: Data, E: np.ndarray) -> dict:
    v1 = raw_logits(D, D.cfg1["fields"], E)
    v2 = raw_logits(D, D.cfg2["fields"], E)
    union = {}
    for f in ZS_FIELDS:
        union[f] = {}
        for v in set(v1[f]) | set(v2[f]):
            if v in v1[f] and v in v2[f]:
                union[f][v] = np.maximum(v1[f][v], v2[f][v])
            else:
                union[f][v] = v1[f].get(v, v2[f].get(v))
    return {"v1": v1, "v2": v2, "union": union}


def _pick(lg: dict, active: list[str], unknown_min: float):
    a = np.array([lg[k] for k in active])
    p = softmax(a)
    order = sorted(range(len(active)), key=lambda j: (-p[j], active[j]))
    top = active[order[0]]
    margin = p[order[0]] - (p[order[1]] if len(order) > 1 else 0.0)
    label = "unknown" if unknown_min and p[order[0]] < unknown_min else top
    return label, {"label": label, "argmax": top, "top_prob": round(float(p[order[0]]), 4),
                   "margin": round(float(margin), 4)}


def zs_predict(D: Data, raw: dict, choice: dict, offsets: dict, mode: str) -> list[dict]:
    """Per-image labels/scores/cover mirroring tools/vnext_s3_local_engine.decide.

    mode 'v1': exact v1 decision semantics (SigLIP unreadable in the scope competition,
    unknown floor 0.35). mode 'v2': blank rule replaces SigLIP unreadable, no unknown floor.
    """
    cfg = D.cfg1 if mode == "v1" else D.cfg2
    um = cfg["decision"]["unknown_min_top_prob"]
    N = len(D.idx)
    L = {}
    for f in ZS_FIELDS:
        src = raw[choice.get(f, "v2")][f]
        L[f] = {v: src[v] + offsets.get(f, {}).get(v, 0.0) for v in src}
    if mode == "v2":
        L["scope"].pop("unreadable_or_blank", None)
    rej_vals = [r for r in REJECTS if r in L["scope"]]
    recs = []
    for n in range(N):
        lg = {f: {v: float(L[f][v][n]) for v in L[f]} for f in ZS_FIELDS}
        s = lg["scope"]
        best_rej = max(rej_vals, key=lambda k: (s[k], k))
        if mode == "v2" and D.blank[n]:
            in_scope, best_rej = False, "unreadable_or_blank"
        else:
            in_scope = s["in_scope"] >= s[best_rej] - cfg["decision"]["scope_reject_logit_margin"]
        sk = list(s)
        sp = dict(zip(sk, softmax(np.array([s[k] for k in sk]))))
        out = {"scope": {"label": "in_scope" if in_scope else best_rej,
                         "margin": round(abs(sp["in_scope"] - sp.get(best_rej, 0.0)), 4)}}
        lab = {"in_scope": bool(in_scope), "reject_reason": "none" if in_scope else best_rej}
        lab["medium"], out["medium"] = _pick(lg["medium"], list(lg["medium"]), um["medium"])
        if not in_scope:
            lab.update(spatial_context=NA, framing_scale=NA, camera_angle=NA, drawing_kind=NA,
                       project_state=NA, project_legibility="none")
        med = lab["medium"]
        if in_scope and med in ("photograph", "rendering"):
            lab["spatial_context"], out["spatial_context"] = _pick(lg["spatial_context"], list(lg["spatial_context"]), um["spatial_context"])
            lab["framing_scale"], out["framing_scale"] = _pick(lg["framing_scale"], list(lg["framing_scale"]), um["framing_scale"])
            cam = list(lg["camera_angle"]) if lab["spatial_context"] in ("exterior", "unknown") else ["eye_level", "elevated"]
            lab["camera_angle"], out["camera_angle"] = _pick(lg["camera_angle"], cam, um["camera_angle"])
        else:
            for a in VIEW:
                lab[a] = NA
        if in_scope and med in ("drawing", "rendering"):
            lab["drawing_kind"], out["drawing_kind"] = _pick(lg["drawing_kind"], list(lg["drawing_kind"]), um["drawing_kind"])
        else:
            lab["drawing_kind"] = NA
        if in_scope and med == "photograph":
            lab["project_state"], out["project_state"] = _pick(lg["project_state"], list(lg["project_state"]), um["project_state"])
        else:
            lab["project_state"] = NA
        if in_scope:
            lab["project_legibility"], out["project_legibility"] = _pick(lg["project_legibility"], ["high", "medium", "low"], um["project_legibility"])
        pm = {f: dict(zip(lg[f], softmax(np.array(list(lg[f].values()))))) for f in
              ("medium", "spatial_context", "framing_scale", "camera_angle", "project_legibility", "project_state")}
        p_scope = 1 / (1 + math.exp(-(s["in_scope"] - s[best_rej]))) if not (mode == "v2" and D.blank[n]) else 0.0
        comps = [p_scope, pm["medium"]["photograph"], pm["spatial_context"]["exterior"], pm["framing_scale"]["overall"],
                 pm["camera_angle"]["eye_level"] + pm["camera_angle"]["elevated"], pm["project_legibility"]["high"],
                 pm["project_state"]["visibly_finished"]]
        recs.append(_rec(D, n, lab, out, float(np.mean(comps))))
    return recs


def _tier(lab: dict) -> int:
    pe = (lab["in_scope"] and lab["medium"] == "photograph" and lab["spatial_context"] == "exterior"
          and lab["project_state"] == "visibly_finished")
    if pe and lab["framing_scale"] == "overall" and lab["camera_angle"] in ("eye_level", "elevated"):
        return 1
    if pe and ((lab["framing_scale"] == "overall" and lab["camera_angle"] in ("aerial_oblique", "aerial_top_down"))
               or lab["framing_scale"] == "site_context"):
        return 2
    return 3


def _rec(D: Data, n: int, lab: dict, scores: dict, cover_score: float) -> dict:
    return {"idx": D.idx[n], "canonical_bld_id": D.bld[n], "stratum": D.stratum[n], "labels": lab,
            "scores": scores, "cover": {"tier": _tier(lab), "score": round(float(cover_score), 6)}}


def run_doc(D: Data, recs: list[dict]) -> dict:
    """local_run-like {'buildings': ...} with default cover = best tier<3 by (tier, -score, idx)."""
    by = defaultdict(list)
    for r in recs:
        by[r["canonical_bld_id"]].append(r)
    out = {}
    for b, cand in by.items():
        cand = sorted(cand, key=lambda x: (x["cover"]["tier"], -x["cover"]["score"], x["idx"]))
        elig = [x for x in cand if x["cover"]["tier"] < 3]
        out[b] = {"name": D.names[b][0], "stratum": D.names[b][1],
                  "default_cover_idx": elig[0]["idx"] if elig else None,
                  "top3_idx": [x["idx"] for x in elig[:3]], "review_required": not elig,
                  "rank_by_score_top3_any_tier": [x["idx"] for x in sorted(cand, key=lambda x: (-x["cover"]["score"], x["idx"]))[:3]]}
    return {"buildings": out}


def gold_subset(D: Data, blds) -> dict:
    s = set(blds)
    g = copy.deepcopy(D.gold)
    g["labels"] = [x for x in g["labels"] if x["canonical_bld_id"] in s]
    g["covers"] = [c for c in g["covers"] if c["canonical_bld_id"] in s]
    return g


def evaluate(D: Data, recs: list[dict], blds=None) -> dict:
    blds = blds or D.buildings
    s = set(blds)
    rr = [r for r in recs if r["canonical_bld_id"] in s]
    rd = run_doc(D, rr)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        e = ev.evaluate(rr, gold_subset(D, blds), rd)
    return e


def summarize(e: dict) -> dict:
    la, lb = e["lane_a"], e["lane_b"]["gate"]
    il = e["routing"]["image_level"]
    return {
        "n_images": e["scored_images"], "n_buildings": e["scored_buildings"],
        "pooled": la["pooled_9field_acceptance"],
        "per_field": {f: e["per_field"][f]["acceptance"] for f in FIELDS},
        "scope_obvious_errors": la["scope_obvious_errors"], "medium_obvious_errors": la["medium_obvious_errors"],
        "scope_error_kinds": la["scope_error_kinds"], "medium_error_kinds": la["medium_error_kinds"],
        "lane_a_pass": la["pass"],
        "cover_critical": lb["critical_cover_errors"], "cover_top1": lb["top1_in_acceptable"],
        "cover_top3": lb["top3_contains_acceptable"], "lane_b_pass": lb["pass"],
        "cover_rows": [{k: b.get(k) for k in ("canonical_bld_id", "local_default_idx", "local_top3_idx",
                                               "acceptable_default_idx", "top1_ok", "top3_ok", "critical_reasons")}
                       for b in e["lane_b"]["buildings"]],
        "downstream_given_scope_medium_ok": {f: v["acceptance"] for f, v in e["downstream_given_scope_medium_ok"].items()},
        "routing_x95_own": {f: (e["routing"]["own"][f]["routed_pct"] if e["routing"]["own"][f]["reached"] else None)
                            for f in FIELDS},
        "routing_auroc_own": {f: e["routing"]["own"][f]["auroc_margin_vs_correct"] for f in FIELDS},
        "image_level_x95": il["x_for_pooled_95"],
        "union_per_field_x95_pct": il["union_of_per_field_x95_routes"]["routed_pct"],
        "fields_never_95": il["union_of_per_field_x95_routes"]["fields_never_reaching_95"],
        "top_errors": {f: e["per_field"][f]["top_errors_gold_to_local"][:3] for f in FIELDS},
    }


def fast_score(D: Data, recs: list[dict], rows: list[int], w_upstream: float = 1.0) -> float:
    tot = 0.0
    for n in rows:
        lab = recs[n]["labels"]
        acc = D.acc[D.idx[n]]
        for f in FIELDS:
            ok = ev.norm(lab[f]) in acc[f]
            tot += ok * (1 + (w_upstream if f in ("in_scope", "medium") else 0))
    return tot


def tune_zs(D: Data, raws: dict, rows: list[int]) -> tuple[dict, dict, list]:
    choice = dict(D.cfg2["decision"]["prompt_set_choice"])
    offsets: dict = {}
    log = []
    best = fast_score(D, zs_predict(D, raws, choice, offsets, "v2"), rows)
    log.append({"step": "start(v2 base)", "score": best})
    for f in ZS_FIELDS:  # greedy prompt-set choice in cascade order
        for s in ("v1", "v2", "union"):
            if s == choice[f]:
                continue
            c2 = {**choice, f: s}
            sc = fast_score(D, zs_predict(D, raws, c2, offsets, "v2"), rows)
            if sc > best + 1e-9:
                best, choice = sc, c2
        log.append({"step": f"prompt_set {f}={choice[f]}", "score": best})
    ref = {"scope": None, "medium": "photograph", "spatial_context": "exterior", "framing_scale": "overall",
           "camera_angle": "eye_level", "drawing_kind": "plan", "project_state": "visibly_finished",
           "project_legibility": "high"}
    for sweep in range(2):
        for f in ZS_FIELDS:
            vals = sorted(raws[choice[f]][f])
            if f == "scope":
                vals = ["in_scope"]
            else:
                vals = [v for v in vals if v != ref[f]]
            for v in vals:
                cur = offsets.get(f, {}).get(v, 0.0)
                bv, bs = cur, best
                for o in OFFSET_GRID:
                    if o == cur:
                        continue
                    off2 = copy.deepcopy(offsets)
                    off2.setdefault(f, {})[v] = o
                    sc = fast_score(D, zs_predict(D, raws, choice, off2, "v2"), rows)
                    if sc > bs + 1e-9 or (abs(sc - bs) < 1e-9 and abs(o) < abs(bv)):
                        bv, bs = o, sc
                if bv != cur:
                    offsets.setdefault(f, {})[v] = bv
                    best = bs
        log.append({"step": f"offset sweep {sweep + 1}", "score": best})
    offsets = {f: {v: o for v, o in d.items() if o != 0.0} for f, d in offsets.items()}
    return choice, {f: d for f, d in offsets.items() if d}, log


# ====================================================================== probe
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
    m = make_pipeline(StandardScaler(), LogisticRegression(C=C, max_iter=3000))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        m.fit(X, y)
    return m


def proba(m, X) -> list[dict]:
    P = m.predict_proba(X)
    cl = [str(c) for c in m.classes_]
    return [dict(zip(cl, row)) for row in P]


HEADS = ["scope", "reject_reason", "medium", "spatial_context", "framing_scale", "camera_angle",
         "drawing_kind", "project_state", "project_legibility"]


def head_rows(D: Data, head: str, rows: list[int]) -> tuple[list[int], list[str]]:
    """Training rows/targets for a head = gold rows where the field is applicable (gold primary)."""
    R, Y = [], []
    for n in rows:
        g = D.g[D.idx[n]]
        ins, med = g["in_scope"], g["medium"]
        if head == "scope":
            R.append(n); Y.append("true" if ins else "false"); continue
        if head == "reject_reason":
            if not ins:
                R.append(n); Y.append(g["reject_reason"])
            continue
        if head == "medium":
            if med not in (NA, "unknown"):
                R.append(n); Y.append(med)
            continue
        if not ins:
            continue
        if head in VIEW and med in ("photograph", "rendering") and g[head] not in (NA, "unknown"):
            R.append(n); Y.append(g[head])
        elif head == "drawing_kind" and med in ("drawing", "rendering") and g[head] not in (NA, "unknown"):
            R.append(n); Y.append(g[head])
        elif head == "project_state" and med == "photograph" and g[head] not in (NA, "unknown"):
            R.append(n); Y.append(g[head])
        elif head == "project_legibility" and g[head] in ("high", "medium", "low"):
            R.append(n); Y.append(g[head])
    return R, Y


def nested_C(D: Data, X, head: str, train_rows: list[int]) -> float:
    blds = sorted({D.bld[n] for n in train_rows})
    best, bestC = -1.0, C_GRID[len(C_GRID) // 2]
    for C in C_GRID:
        ok = tot = 0
        for b in blds:
            tr = [n for n in train_rows if D.bld[n] != b]
            te = [n for n in train_rows if D.bld[n] == b]
            R, Y = head_rows(D, head, tr)
            Rt, Yt = head_rows(D, head, te)
            if not R or not Rt:
                continue
            m = fit_head(X[R], Y, C)
            for pr, n in zip(proba(m, X[Rt]), Rt):
                pred = max(pr, key=pr.get)
                fld = "in_scope" if head == "scope" else head
                ok += pred in D.acc[D.idx[n]][fld]
                tot += 1
        a = ok / tot if tot else 0
        if a > best + 1e-9:
            best, bestC = a, C
    return bestC


def _top(pr: dict, allowed=None):
    items = [(k, v) for k, v in pr.items() if allowed is None or k in allowed]
    if not items:
        return None, 0.0, {}
    z = sum(v for _, v in items) or 1.0
    items = sorted(((k, v / z) for k, v in items), key=lambda kv: (-kv[1], kv[0]))
    m = items[0][1] - (items[1][1] if len(items) > 1 else 0.0)
    return items[0][0], m, dict(items)


def probe_predict(D: Data, X, train_rows: list[int], test_rows: list[int], Cs: dict,
                  scope_override: dict | None = None) -> list[dict]:
    models = {}
    for h in HEADS:
        R, Y = head_rows(D, h, train_rows)
        models[h] = fit_head(X[R], Y, Cs[h]) if R else None
    P = {h: (proba(models[h], X[test_rows]) if models[h] is not None else [{}] * len(test_rows)) for h in HEADS}
    recs = []
    for k, n in enumerate(test_rows):
        sc = {}
        ps = P["scope"][k]
        pin = ps.get("true", 0.0) if ps else 1.0
        in_scope = pin >= 0.5
        sc["scope"] = {"label": "in_scope" if in_scope else "reject", "margin": round(float(abs(2 * pin - 1)), 4)}
        lab = {"in_scope": bool(in_scope)}
        ov = scope_override.get(D.idx[n]) if scope_override else None
        if ov is not None:  # combo: scope/reject taken from another engine's held-out prediction
            in_scope = ov["labels"]["in_scope"]
            lab = {"in_scope": bool(in_scope), "reject_reason": ov["labels"]["reject_reason"]}
            sc["scope"] = dict(ov["scores"]["scope"])
        elif in_scope:
            lab["reject_reason"] = "none"
        else:
            rr, _, _ = _top(P["reject_reason"][k]) if P["reject_reason"][k] else (None, 0, {})
            lab["reject_reason"] = rr or "isolated_product_artwork_or_sample"
        med, m, pm = _top(P["medium"][k])
        lab["medium"] = med
        sc["medium"] = {"label": med, "margin": round(float(m), 4)}
        view_on = in_scope and med in ("photograph", "rendering")
        if view_on:
            sp, m, _ = _top(P["spatial_context"][k]); lab["spatial_context"] = sp; sc["spatial_context"] = {"margin": round(float(m), 4)}
            fr, m, _ = _top(P["framing_scale"][k]); lab["framing_scale"] = fr; sc["framing_scale"] = {"margin": round(float(m), 4)}
            allowed = None if sp == "exterior" else {"eye_level", "elevated"}
            ca, m, _ = _top(P["camera_angle"][k], allowed)
            lab["camera_angle"] = ca or "eye_level"; sc["camera_angle"] = {"margin": round(float(m), 4)}
        else:
            for a in VIEW:
                lab[a] = NA
        if in_scope and med in ("drawing", "rendering") and P["drawing_kind"][k]:
            dk, m, _ = _top(P["drawing_kind"][k]); lab["drawing_kind"] = dk; sc["drawing_kind"] = {"margin": round(float(m), 4)}
        else:
            lab["drawing_kind"] = NA
        if in_scope and med == "photograph":
            st, m, _ = _top(P["project_state"][k]); lab["project_state"] = st; sc["project_state"] = {"margin": round(float(m), 4)}
        else:
            lab["project_state"] = NA
        if in_scope:
            lg, m, _ = _top(P["project_legibility"][k]); lab["project_legibility"] = lg; sc["project_legibility"] = {"margin": round(float(m), 4)}
        else:
            lab["project_legibility"] = "none"
        if not in_scope:
            lab.update(spatial_context=NA, framing_scale=NA, camera_angle=NA, drawing_kind=NA, project_state=NA)
        cover = (pm.get("photograph", 0.0) * P["spatial_context"][k].get("exterior", 0.0)
                 * P["framing_scale"][k].get("overall", 0.0)
                 * (P["project_state"][k].get("visibly_finished", 0.0) if P["project_state"][k] else 1.0))
        recs.append(_rec(D, n, lab, sc, cover))
    return recs


def lobo(D: Data, X, Cs_fixed: dict | None = None, train_filter=None, seed: int = 0,
         scope_override: dict | None = None) -> tuple[list[dict], dict]:
    """Leave-one-building-out. Cs_fixed=None -> nested inner-LOBO C per head per outer fold."""
    recs = [None] * len(D.idx)
    chosen = defaultdict(list)
    for b in D.buildings:
        tr = [n for n in range(len(D.idx)) if D.bld[n] != b]
        te = [n for n in range(len(D.idx)) if D.bld[n] == b]
        if train_filter is not None:
            tr = train_filter(tr, b, seed)
        if Cs_fixed is None:
            Cs = {h: nested_C(D, X, h, tr) for h in HEADS}
        else:
            Cs = Cs_fixed
        for h in HEADS:
            chosen[h].append(Cs[h])
        for r in probe_predict(D, X, tr, te, Cs, scope_override):
            recs[D.pos[r["idx"]]] = r
    return recs, dict(chosen)


# ====================================================================== budget
def budget_curves(D: Data, X, Cs: dict, sizes=(25, 50, 100, 150), seeds=5) -> dict:
    out = {"image_subsets": [], "building_subsets": []}

    def metrics(recs):
        e = evaluate(D, recs)
        s = summarize(e)
        return {"pooled": s["pooled"], "medium": s["per_field"]["medium"], "in_scope": s["per_field"]["in_scope"],
                "camera_angle": s["per_field"]["camera_angle"], "project_legibility": s["per_field"]["project_legibility"],
                "framing_scale": s["per_field"]["framing_scale"],
                "scope_err": s["scope_obvious_errors"], "medium_err": s["medium_obvious_errors"],
                "cover_top1": s["cover_top1"], "cover_critical": s["cover_critical"]}

    for size in sizes:
        runs = []
        for sd in range(seeds):
            def filt(tr, b, s, size=size):
                rng = np.random.default_rng(int(hashlib.sha256(f"{size}|{s}|{b}".encode()).hexdigest()[:8], 16))
                return sorted(rng.choice(tr, size=min(size, len(tr)), replace=False).tolist())
            recs, _ = lobo(D, X, Cs, filt, sd)
            runs.append(metrics(recs))
        out["image_subsets"].append({"n_train": size, "seeds": seeds, "runs": runs, "mean": _mean(runs)})
    full, _ = lobo(D, X, Cs)
    ntr = np.mean([sum(1 for x in D.bld if x != b) for b in D.buildings])
    out["image_subsets"].append({"n_train": round(float(ntr), 1), "seeds": 1, "runs": [metrics(full)], "mean": metrics(full),
                                 "note": "all 9 other buildings"})
    for kb in (1, 2, 3, 5, 7):
        runs, ns = [], []
        for sd in range(seeds):
            def filt(tr, b, s, kb=kb):
                others = sorted({D.bld[n] for n in tr})
                rng = np.random.default_rng(int(hashlib.sha256(f"b{kb}|{s}|{b}".encode()).hexdigest()[:8], 16))
                pick = set(rng.choice(others, size=kb, replace=False).tolist())
                return [n for n in tr if D.bld[n] in pick]
            recs, _ = lobo(D, X, Cs, filt, sd)
            runs.append(metrics(recs))
        out["building_subsets"].append({"n_buildings": kb, "seeds": seeds, "runs": runs, "mean": _mean(runs),
                                        "approx_images": round(kb * len(D.idx) / len(D.buildings))})
    out["building_subsets"].append({"n_buildings": 9, "seeds": 1, "mean": metrics(full),
                                    "approx_images": round(float(ntr))})
    return out


def _mean(runs):
    keys = [k for k in runs[0] if isinstance(runs[0][k], (int, float)) and not isinstance(runs[0][k], bool)]
    m = {k: round(float(np.mean([r[k] for r in runs])), 4) for k in keys}
    m.update({k + "_sd": round(float(np.std([r[k] for r in runs])), 4) for k in ("pooled", "medium", "in_scope")})
    return m


def extrapolate_budget(curve: list[tuple[float, float]], target: float) -> dict:
    """Power law on error: err(n) = a * n^-b fitted in log-log on (n, acc). Rough guess."""
    pts = [(n, 1 - a) for n, a in curve if n >= 25 and a < 1]
    if len(pts) < 3:
        return {"fit": None}
    x = np.log([p[0] for p in pts]); y = np.log([p[1] for p in pts])
    b, la = np.polyfit(x, y, 1)
    a, b = math.exp(la), -b
    if b <= 0.02:
        return {"a": round(a, 4), "b": round(b, 4), "n_needed": None, "note": "no measurable improvement with n"}
    n = (a / (1 - target)) ** (1 / b)
    return {"a": round(a, 4), "b": round(b, 4), "target": target, "n_needed": int(round(n))}


# ====================================================================== hard fields
CAM_MERGE = {"eye_level": "ground_or_elevated", "elevated": "ground_or_elevated"}
LEG2 = {"high": "readable", "medium": "readable", "low": "low"}
LEG2B = {"high": "high", "medium": "partial", "low": "partial"}


def merged_acceptance(D: Data, recs: list[dict], field: str, mapping: dict) -> dict:
    ok = n = 0
    for r in recs:
        acc = {mapping.get(v, v) for v in D.acc[r["idx"]][field]}
        n += 1
        ok += mapping.get(ev.norm(r["labels"][field]), ev.norm(r["labels"][field])) in acc
    return {"accepted": ok, "n": n, "acceptance": round(ok / n, 4)}


def abstain_curve(D: Data, recs: list[dict], field: str) -> list[dict]:
    """Applicable rows only (local value != NA/none): abstain ('unknown') when own margin < t."""
    items = []
    for r in recs:
        v = ev.norm(r["labels"][field])
        if v in (NA, "none"):
            continue
        m = (r["scores"].get(field) or {}).get("margin")
        if m is None:
            continue
        items.append((m, v in D.acc[r["idx"]][field]))
    items.sort()
    out = []
    for pct in (0, 10, 20, 30, 40, 50, 60):
        k = math.floor(len(items) * pct / 100)
        rest = items[k:]
        out.append({"abstain_pct": pct, "kept": len(rest),
                    "acc_on_kept": round(sum(ok for _, ok in rest) / len(rest), 4) if rest else None})
    return out


def gold_ambiguity(D: Data) -> dict:
    out = {}
    for f in FIELDS:
        multi = sum(1 for i in D.idx if len(D.acc[i][f]) > 1)
        out[f] = {"rows_with_multiple_acceptable": multi, "pct": round(100 * multi / len(D.idx), 1)}
    return out


def catch_all_routing(D: Data, recs: list[dict]) -> dict:
    """Lane A needs 0 scope/medium obvious errors: what fraction must be routed (lowest own margin first,
    per field) so that every remaining auto-accepted scope / medium value is acceptable? Plus the
    union over both fields. In-sample thresholds (optimistic)."""
    out, union = {}, set()
    N = len(recs)
    for f, key in (("in_scope", "scope"), ("medium", "medium")):
        items = sorted(((r["scores"].get(key) or {}).get("margin", 0.0), r["idx"],
                        ev.norm(r["labels"][f]) in D.acc[r["idx"]][f]) for r in recs)
        last_err = max((k for k, (_, _, ok) in enumerate(items) if not ok), default=-1)
        routed = [i for _, i, _ in items[:last_err + 1]]
        union.update(routed)
        out[f] = {"errors": sum(1 for *_, ok in items if not ok), "routed_to_catch_all": len(routed),
                  "routed_pct": round(100 * len(routed) / N, 1),
                  "margin_threshold": round(items[last_err][0], 4) if last_err >= 0 else None}
    out["union_scope_medium"] = {"routed": len(union), "routed_pct": round(100 * len(union) / N, 1)}
    return out


# ====================================================================== main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stages", default="zs,probe,budget,hard")
    ap.add_argument("--seeds", type=int, default=5)
    a = ap.parse_args()
    stages = set(a.stages.split(","))
    t0 = time.time()
    D = Data()
    res_path = OUT / "results.json"
    res = json.loads(res_path.read_text(encoding="utf-8")) if res_path.exists() else {}
    res.update({"version": EXP_VERSION, "tool": "tools/vnext_wp3_exp_v2.py", "tool_sha256": sha256_file(Path(__file__)),
                "inputs": {"gold_labels_final_v2.json": sha256_file(GOLD), "config_v1": sha256_file(CFG1),
                           "config_v2": sha256_file(CFG2), "emb.npy": sha256_file(OUT / "emb.npy"),
                           "eval_module": sha256_file(ROOT / "tools/vnext_wp3_eval.py")},
                "folds": {"rule": f"sha256('{FOLD_SALT}'+canonical_bld_id) ascending; first 5 = A, rest = B",
                          "A": D.fold["A"], "B": D.fold["B"],
                          "A_images": len(D.rows_of(D.fold["A"])), "B_images": len(D.rows_of(D.fold["B"])),
                          "strata": {k: dict(Counter(D.names[b][1] for b in v)) for k, v in D.fold.items()}},
                "blank_rule_fires": int(D.blank.sum()),
                "paid_calls": 0, "network_requests": 0})
    preds = {}

    if "zs" in stages:
        raws = raw_sets(D, D.E)
        # parity: v1 from cache == frozen local_labels.jsonl
        v1 = zs_predict(D, raws, {f: "v1" for f in ZS_FIELDS}, {}, "v1")
        frozen = {json.loads(x)["idx"]: json.loads(x)["labels"]
                  for x in (WP / "local_labels.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()}
        mism = [(r["idx"], f) for r in v1 for f in FIELDS if ev.norm(r["labels"][f]) != ev.norm(frozen[r["idx"]][f])]
        res["zs_parity_v1_vs_frozen_local_labels"] = {"field_mismatches": len(mism), "examples": mism[:10]}
        res["zs_v1_cache_225"] = summarize(evaluate(D, v1))
        base = zs_predict(D, raws, D.cfg2["decision"]["prompt_set_choice"], {}, "v2")
        res["zs_v2_base_untuned_225"] = summarize(evaluate(D, base))
        cross = [None] * len(D.idx)
        res["zs_v2_crossfold"] = {}
        for tune, hold in (("A", "B"), ("B", "A")):
            rows_t = D.rows_of(D.fold[tune])
            choice, offsets, log = tune_zs(D, raws, rows_t)
            tuned = zs_predict(D, raws, choice, offsets, "v2")
            cfg_t = copy.deepcopy(D.cfg2)
            cfg_t["config_version"] = f"vnext-s3-local-engine-config-v2-tuned-fold{tune}"
            cfg_t["decision"]["prompt_set_choice"] = choice
            cfg_t["decision"]["value_logit_offsets"] = offsets
            cfg_t["status"] = f"tuned on fold {tune} buildings {D.fold[tune]} only; report on fold {hold}"
            (OUT / f"config_v2_tuned_fold{tune}.json").write_text(json.dumps(cfg_t, indent=1, ensure_ascii=False), encoding="utf-8")
            for n in D.rows_of(D.fold[hold]):
                cross[n] = tuned[n]
            res["zs_v2_crossfold"][f"tune_{tune}_report_{hold}"] = {
                "prompt_set_choice": choice, "value_logit_offsets": offsets, "tuning_log": log,
                "on_tuning_fold_in_sample": summarize(evaluate(D, tuned, D.fold[tune])),
                "on_heldout_fold": summarize(evaluate(D, tuned, D.fold[hold])),
                "v1_on_heldout_fold": summarize(evaluate(D, v1, D.fold[hold])),
                "v2_base_on_heldout_fold": summarize(evaluate(D, base, D.fold[hold])),
            }
        res["zs_v2_crossfold_merged_225"] = summarize(evaluate(D, cross))
        preds["zs_v2_crossfold"] = cross
        print("zs done", round(time.time() - t0, 1), "s", res["zs_v2_crossfold_merged_225"]["pooled"], flush=True)

    if stages & {"probe", "budget", "hard", "combo"}:
        raws1 = raw_sets(D, D.E)
        Z = np.stack([raws1[s][f][v] for s in ("v1", "v2") for f in ZS_FIELDS for v in sorted(raws1[s][f])], axis=1)
        Zs = (Z - Z.mean(0)) / (Z.std(0) + 1e-6)  # unsupervised standardisation (no labels)
        feats = {"emb": D.E, "emb_tta": D.E_tta, "zs_logits_only": Zs, "hybrid_emb_zs": np.hstack([D.E, Zs])}
        res.setdefault("probe_lobo", {})

    if "probe" in stages:
        for name, X in feats.items():
            t1 = time.time()
            recs, chosen = lobo(D, X)
            s = summarize(evaluate(D, recs))
            s["nested_C_choices"] = {h: dict(Counter(map(str, v))) for h, v in chosen.items()}
            s["wall_s"] = round(time.time() - t1, 1)
            res["probe_lobo"][name] = s
            preds[f"probe_{name}"] = recs
            print("probe", name, s["pooled"], s["scope_obvious_errors"], s["medium_obvious_errors"], s["cover_top1"],
                  s["wall_s"], flush=True)

    best_name = None
    if res.get("probe_lobo"):
        best_name = max(res["probe_lobo"], key=lambda k: res["probe_lobo"][k]["pooled"])
        res["probe_best_variant"] = best_name

    if "combo" in stages and "zs_v2_crossfold" in preds:
        # exploratory (chosen AFTER seeing zs vs probe results): scope/reject from zero-shot v2 cross-fold
        # (each image scored by the config tuned on the other fold), everything else from the probe (LOBO)
        ov = {r["idx"]: r for r in preds["zs_v2_crossfold"]}
        res["combo"] = {}
        for name in ("zs_logits_only", "emb"):
            recs, chosen = lobo(D, feats[name], scope_override=ov)
            s = summarize(evaluate(D, recs))
            s["nested_C_choices"] = {h: dict(Counter(map(str, v))) for h, v in chosen.items()}
            res["combo"][f"zsScope+probe_{name}"] = s
            preds[f"combo_zsScope_probe_{name}"] = recs
            print("combo", name, s["pooled"], s["scope_obvious_errors"], s["medium_obvious_errors"], s["cover_top1"], flush=True)

    if "budget" in stages:
        bn = res.get("probe_best_variant", "emb")
        Cs = {h: float(max(res["probe_lobo"][bn]["nested_C_choices"][h].items(), key=lambda kv: kv[1])[0])
              for h in HEADS}
        bc = budget_curves(D, feats[bn], Cs, seeds=a.seeds)
        bc["features"], bc["C_fixed_mode_of_nested"] = bn, Cs
        ext = {}
        for key, target in (("pooled", 0.90), ("pooled", 0.95), ("medium", 0.95), ("in_scope", 0.95),
                            ("camera_angle", 0.90), ("project_legibility", 0.90)):
            pts = [(p["n_train"], p["mean"][key]) for p in bc["image_subsets"]]
            ext[f"{key}>={target}"] = extrapolate_budget(pts, target)
        bc["extrapolation_rough_guess"] = ext
        res["budget"] = bc
        print("budget done", round(time.time() - t0, 1), flush=True)

    if "hard" in stages:
        bn = res.get("probe_best_variant", "emb")
        if f"probe_{bn}" not in preds:
            preds[f"probe_{bn}"], _ = lobo(D, feats[bn])
        rp = preds[f"probe_{bn}"]
        hard = {"engine": f"probe_{bn}", "gold_ambiguity": gold_ambiguity(D)}
        hard["camera_angle"] = {"as_is": merged_acceptance(D, rp, "camera_angle", {}),
                                "merge_eye_level_elevated": merged_acceptance(D, rp, "camera_angle", CAM_MERGE),
                                "abstain_curve_applicable_rows": abstain_curve(D, rp, "camera_angle")}
        hard["project_legibility"] = {"as_is": merged_acceptance(D, rp, "project_legibility", {}),
                                      "two_level_readable_vs_low": merged_acceptance(D, rp, "project_legibility", LEG2),
                                      "two_level_high_vs_partial": merged_acceptance(D, rp, "project_legibility", LEG2B),
                                      "abstain_curve_applicable_rows": abstain_curve(D, rp, "project_legibility")}
        hard["framing_scale"] = {"abstain_curve_applicable_rows": abstain_curve(D, rp, "framing_scale")}
        hard["medium"] = {"abstain_curve_applicable_rows": abstain_curve(D, rp, "medium")}
        # pooled if camera/legibility merged
        tot = ok = 0
        for r in rp:
            for f in FIELDS:
                mp = CAM_MERGE if f == "camera_angle" else LEG2 if f == "project_legibility" else {}
                acc = {mp.get(v, v) for v in D.acc[r["idx"]][f]}
                v = ev.norm(r["labels"][f])
                ok += mp.get(v, v) in acc
                tot += 1
        hard["pooled_with_both_merges"] = round(ok / tot, 4)
        if "zs_v2_crossfold" in preds:
            rz = preds["zs_v2_crossfold"]
            hard["zs_v2_crossfold_camera_merge"] = merged_acceptance(D, rz, "camera_angle", CAM_MERGE)
            hard["zs_v2_crossfold_legibility_2level"] = merged_acceptance(D, rz, "project_legibility", LEG2)
        res["hard_fields"] = hard

    res["catch_all_scope_medium_routing"] = {k: catch_all_routing(D, v) for k, v in preds.items()}
    for name, recs in preds.items():
        with open(OUT / f"pred_{name}.jsonl", "w", encoding="utf-8") as fh:
            for r in recs:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    res["wall_s_last_run"] = round(time.time() - t0, 1)
    res_path.write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
    print("done", round(time.time() - t0, 1), "s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
