#!/usr/bin/env python3
"""vNext WP3 N100: final gold + lane A / lane B gate evaluation + full-run projection ($0, offline).

Gold (preregistration.json `gold`, user decision 2026-10-28):
- labels: codex v1.4 label per image = provisional gold. For every judged image field the Claude judge
  decision replaces it (`judge_decisions.jsonl`): codex_correct -> codex value, probe_correct -> probe
  value, both_acceptable -> judge value (or codex) primary + the other engine value as acceptable
  alternative, both_wrong -> judge value (neither engine accepted). Spot-check corrections (both_wrong on
  an agreement image) replace the shared value. Agreed fields that differ only by the v1.4 equivalence
  (camera unknown ~ eye_level/elevated, legibility medium->high) accept the probe value as well.
- covers: codex comparative acceptable_idx, replaced by the judge acceptable set for judged buildings.

Scoring: v1.4 mapping (`tools/vnext_wp3_eval_v14.map_gold_v14/map_preds_v14`) -> unchanged v1 scorer
(`tools/vnext_wp3_eval.evaluate`). Engines: probe (frozen local_pred), codex (labels.jsonl), routed funnel
at T_primary / T_secondary (probe, routed images replaced by codex).

Lane B: production policy = local default if local-confident else codex ranked[0] (empty -> NULL);
baselines codex-all, local-all, conservative what-if (A=0.95, M=0) and the P0 legacy display_cover_url.

Inputs SHA-checked (preregistration, freeze manifest, judge_queue inputs and chunk files); inputs opened
read-only. Writes only gold_n100.json, eval.json, eval.md in data/smoke_vnext/wp3_n100_v1/.
No network, no model calls, no Neon/R2.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import warnings
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import vnext_wp3_eval as ev  # noqa: E402  (unchanged v1 scorer)
from tools import vnext_wp3_eval_v14 as v14  # noqa: E402  (v1.4 mapping)

WP = ROOT / "data" / "smoke_vnext" / "wp3_n100_v1"
PREREG = WP / "preregistration.json"
PREREG_SHA = "59eee674e96aa1582fd4a68950cc645b247c2e023a8b00df17775cfbc49b375b"
FREEZE = WP / "freeze_manifest.json"
QUEUE = WP / "judge_queue.json"
DECISIONS = WP / "judge_decisions.jsonl"
CODEX = WP / "codex"
CALIB_COVER_CALLS = WP / "calib" / "codex_cover_pool" / "calls.jsonl"
EVAL_VERSION = "vnext-wp3-n100-eval-v1"
FIELDS = ev.FIELDS
LANE_A_MIN = 0.95
LANE_B_TOP1_MIN = 0.95
LANE_B_TOP3_MIN = 0.99

# full-run population (job card 20261023_vnext_s6_lite_building_images: building_images prod)
POP_IMAGES = 699_038
POP_BUILDINGS = 39_248
QUOTA_USABLE_FRAC = 0.70  # plan §4: no new paid calls once remaining weekly quota <= 30%
ARCH_OLD_PAT = r"^https?://architizer-prod\.imgix\.net(/[^\"?\s]*)(\?[^\"\s]*)?$"
ARCH_NEW_REP = r"https://images.architizer.com\1?width=1680&quality=60"


# ---------------------------------------------------------------- io helpers
def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def rj(p: Path) -> list[dict]:
    with open(p, encoding="utf-8") as fh:
        return [json.loads(x) for x in fh if x.strip()]


def rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


def nv(field: str, v):
    """Normalise a judge/engine value to the label type (in_scope bool, others str)."""
    if field == "in_scope":
        if isinstance(v, bool):
            return v
        s = str(v).strip().lower()
        if s in ("true", "false"):
            return s == "true"
        raise ValueError(f"bad in_scope value {v!r}")
    return None if v is None else str(v)


def m14(field: str, v):
    return "high" if field == "project_legibility" and v == "medium" else v


def agree14(field: str, a, b) -> bool:
    a, b = m14(field, a), m14(field, b)
    if a == b:
        return True
    return field == "camera_angle" and {a, b} in ({"unknown", "eye_level"}, {"unknown", "elevated"})


def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, c - h), min(1.0, c + h))


def pct(x, d=1):
    return "-" if x is None else f"{100 * x:.{d}f}%"


# ---------------------------------------------------------------- integrity
def check_inputs() -> dict:
    if sha(PREREG) != PREREG_SHA:
        raise SystemExit("preregistration SHA drift")
    fz = json.loads(FREEZE.read_text(encoding="utf-8"))
    if fz.get("preregistration_sha256") != PREREG_SHA:
        raise SystemExit("freeze manifest points at another preregistration")
    for r, s in fz["files"].items():
        if sha(ROOT / r) != s:
            raise SystemExit(f"frozen file drift: {r}")
    q = json.loads(QUEUE.read_text(encoding="utf-8"))
    if q.get("preregistration_sha256") != PREREG_SHA:
        raise SystemExit("judge queue points at another preregistration")
    for r, s in q["inputs_sha256"].items():
        if sha(ROOT / r) != s:
            raise SystemExit(f"judge-queue input drift: {r}")
    for r, s in q["chunk_files_sha256"].items():
        if sha(ROOT / r) != s:
            raise SystemExit(f"judge chunk drift: {r}")
    shas = {**fz["files"], **q["inputs_sha256"], **q["chunk_files_sha256"],
            rel(PREREG): PREREG_SHA, rel(FREEZE): sha(FREEZE), rel(QUEUE): sha(QUEUE),
            rel(DECISIONS): sha(DECISIONS)}
    return {"freeze": fz, "queue": q, "sha256": shas}


# ---------------------------------------------------------------- judge decisions
def load_decisions(queue: dict, probe: dict, codex: dict) -> dict:
    img_q = {e["idx"]: e for e in queue["entries"]["image"]}
    spot_q = {e["idx"]: e for e in queue["entries"]["spot"]}
    cov_q = {e["canonical_bld_id"]: e for e in queue["entries"]["cover"]}
    image, spot, cover = {}, defaultdict(list), {}
    issues = defaultdict(list)
    for d in rj(DECISIONS):
        k = d["kind"]
        if k == "image":
            key = (d["idx"], d["field"])
            if key in image:
                issues["duplicate_image_decision"].append(list(key))
            if d["idx"] not in img_q or d["field"] not in img_q[d["idx"]]["differing_fields"]:
                issues["image_decision_not_in_queue"].append(list(key))
                continue
            if d.get("canonical_bld_id") and d["canonical_bld_id"] != probe[d["idx"]]["canonical_bld_id"]:
                issues["image_decision_bld_mismatch"].append(list(key))
            image[key] = d
        elif k == "spot":
            if d["idx"] not in spot_q:
                issues["spot_decision_not_in_queue"].append(d["idx"])
                continue
            spot[d["idx"]].append(d)
        elif k == "cover":
            if d["canonical_bld_id"] not in cov_q:
                issues["cover_decision_not_in_queue"].append(d["canonical_bld_id"])
                continue
            cover[d["canonical_bld_id"]] = d
        else:
            issues["unknown_kind"].append(k)
    for idx, e in img_q.items():
        for f in e["differing_fields"]:
            if (idx, f) not in image:
                issues["image_field_without_decision"].append([idx, f])
    for idx in spot_q:
        if idx not in spot:
            issues["spot_without_decision"].append(idx)
    for b in cov_q:
        if b not in cover:
            issues["cover_without_decision"].append(b)
    # verdict / correct_value consistency
    for (idx, f), d in image.items():
        p, c = m14(f, probe[idx]["labels"][f]), m14(f, codex[idx]["labels"][f])
        cv = d.get("correct_value")
        cvn = m14(f, nv(f, cv)) if cv is not None else None
        v = d["verdict"]
        if v == "codex_correct" and cvn is not None and cvn != c:
            issues["codex_correct_value_mismatch"].append([idx, f, cv, c])
        elif v == "probe_correct" and cvn is not None and cvn != p:
            issues["probe_correct_value_mismatch"].append([idx, f, cv, p])
        elif v == "both_acceptable" and cvn is not None and cvn not in (p, c):
            issues["both_acceptable_value_outside_pair"].append([idx, f, cv, p, c])
        elif v == "both_wrong" and (cvn is None or cvn in (p, c)):
            issues["both_wrong_value_invalid"].append([idx, f, cv, p, c])
        elif v not in ("codex_correct", "probe_correct", "both_acceptable", "both_wrong"):
            issues["unknown_verdict"].append([idx, f, v])
    return {"image": image, "spot": dict(spot), "cover": cover, "issues": {k: v for k, v in issues.items()},
            "counts": {"image": len(image), "spot_images": len(spot),
                       "spot_rows": sum(len(v) for v in spot.values()), "cover": len(cover)}}


# ---------------------------------------------------------------- gold
def build_gold(order: list[int], probe: dict, codex: dict, queue: dict, dec: dict) -> tuple[dict, dict]:
    img_q = {e["idx"]: e for e in queue["entries"]["image"]}
    spot_q = {e["idx"] for e in queue["entries"]["spot"]}
    labels, prov_ct = [], Counter()
    spot_corr = []
    for idx in order:
        p, c = probe[idx]["labels"], codex[idx]["labels"]
        g = {f: c[f] for f in FIELDS}
        acc = {f: [] for f in FIELDS}
        prov = {f: "codex_unjudged" for f in FIELDS}
        diff = img_q[idx]["differing_fields"] if idx in img_q else []
        for f in FIELDS:
            if f in diff:
                d = dec["image"].get((idx, f))
                if d is None:
                    prov[f] = "codex_judge_missing"
                    continue
                cv = nv(f, d.get("correct_value")) if d.get("correct_value") is not None else None
                v = d["verdict"]
                if v == "codex_correct":
                    g[f], prov[f] = c[f], "judge_codex"
                elif v == "probe_correct":
                    g[f], prov[f] = p[f], "judge_probe"
                elif v == "both_acceptable":
                    prim = cv if cv is not None and m14(f, cv) in (m14(f, p[f]), m14(f, c[f])) else c[f]
                    g[f] = prim
                    for alt in (c[f], p[f]):
                        if m14(f, alt) != m14(f, prim) and alt not in acc[f]:
                            acc[f].append(alt)
                    prov[f] = "judge_both"
                elif v == "both_wrong":
                    g[f], prov[f] = cv, "judge_neither"
            else:
                if c[f] != p[f] and agree14(f, p[f], c[f]):
                    # pre-registered v1.4 agreement (never sent to the judge): accept the probe value too
                    if m14(f, p[f]) != m14(f, c[f]) and p[f] not in acc[f]:
                        acc[f].append(p[f])
                    prov[f] = "codex_unjudged_v14_equiv"
                if idx in spot_q:
                    prov[f] = "spot_confirmed"
        if idx in spot_q:
            for d in dec["spot"].get(idx, []):
                f = d["field"]
                if f == "__ok__":
                    continue
                if d["verdict"] == "both_wrong":
                    old = g[f]
                    g[f] = nv(f, d["correct_value"])
                    acc[f] = []
                    prov[f] = "spot_corrected"
                    spot_corr.append({"idx": idx, "canonical_bld_id": probe[idx]["canonical_bld_id"],
                                      "field": f, "shared_value": old, "judge_value": g[f], "note": d.get("note")})
        for f in FIELDS:
            prov_ct[f"{f}:{prov[f]}"] += 1
        labels.append({"idx": idx, "canonical_bld_id": probe[idx]["canonical_bld_id"],
                       "stratum": probe[idx]["stratum"], **g, "acceptable": {f: v for f, v in acc.items() if v},
                       "provenance": prov, "judged": idx in img_q, "spot": idx in spot_q})
    return {"labels": labels}, {"provenance_counts": dict(sorted(prov_ct.items())), "spot_corrections": spot_corr}


CRIT_RE = re.compile(r"Critical if picked:(.*?)(?:\.\s|\.$|$)", re.S)


def parse_idx_list(s: str) -> list[int]:
    out = []
    for a, b in re.findall(r"(\d+)(?:-(\d+))?", s):
        out.extend(range(int(a), int(b) + 1) if b else [int(a)])
    return sorted(set(out))


def build_cover_gold(lcov: dict, ccov: dict, dec: dict, bld_imgs: dict) -> tuple[dict, list]:
    out, issues = {}, []
    for b in sorted(lcov):
        d = dec["cover"].get(b)
        if d is not None:
            acc = parse_idx_list(d.get("correct_value") or "")
            m = CRIT_RE.search(d.get("note") or "")
            crit = parse_idx_list(m.group(1)) if m else []
            bad = [i for i in acc + crit if i not in bld_imgs[b]]
            if bad:
                issues.append({"bld": b, "idx_not_in_building": bad})
            out[b] = {"acceptable_default_idx": acc, "source": "judge", "judge_critical_idx": crit,
                      "judge_note": d.get("note")}
        else:
            cc = ccov[b]
            out[b] = {"acceptable_default_idx": list(cc["acceptable_idx"]), "source": "codex",
                      "judge_critical_idx": None}
    return out, issues


# ---------------------------------------------------------------- lane A
def score_engine(name: str, recs: list[dict], gold14: dict, routed: set | None, judged: set,
                 by_idx_stratum: dict) -> dict:
    recs14, pst = v14.map_preds_v14(recs)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        e = ev.evaluate(recs14, gold14, None)
    la = e["lane_a"]
    gidx = {g["idx"]: g for g in gold14["labels"]}
    local = {r["idx"]: r for r in recs14}

    def pooled_on(idxs):
        idxs = [i for i in idxs if i in gidx]
        ok = sum(ev.is_accepted(local[i]["labels"], gidx[i], f) for i in idxs for f in FIELDS)
        n = len(idxs) * len(FIELDS)
        return {"images": len(idxs), "accepted": ok, "n": n, "acceptance": round(ok / n, 4) if n else None}

    all_idx = sorted(gidx)
    strata = defaultdict(list)
    for i in all_idx:
        strata[by_idx_stratum[i]].append(i)
    out = {
        "engine": name, "n_images": e["scored_images"], "pred_mapping_stats": pst,
        "pooled": la["pooled_9field_acceptance"], "pooled_accepted": la["pooled_accepted"], "pooled_n": la["pooled_n"],
        "per_field": {f: {k: e["per_field"][f][k] for k in ("acceptance", "accepted", "n", "accepted_via_alternative",
                                                           "by_stratum", "top_errors_gold_to_local")}
                      for f in FIELDS},
        "confusion": {f: e["confusion"][f]["rows_gold_primary_cols_local"] for f in FIELDS},
        "scope_obvious_errors": la["scope_obvious_errors"], "medium_obvious_errors": la["medium_obvious_errors"],
        "scope_error_kinds": la["scope_error_kinds"], "medium_error_kinds": la["medium_error_kinds"],
        "fields_at_or_above_95pct": [f for f in FIELDS if e["per_field"][f]["acceptance"] >= LANE_A_MIN],
        "per_stratum_pooled": {s: pooled_on(v) for s, v in sorted(strata.items())},
        "judged_images_pooled": pooled_on(sorted(judged)),
        "unjudged_images_pooled": pooled_on([i for i in all_idx if i not in judged]),
    }
    if routed is not None:
        out["routed_images_pooled"] = pooled_on(sorted(routed))
        out["unrouted_images_pooled"] = pooled_on([i for i in all_idx if i not in routed])
        out["routed_n"] = len(routed)
        out["routed_frac"] = round(len(routed) / len(all_idx), 4)
    out["_obvious"] = [o for o in e.get("obvious_errors", [])] if "obvious_errors" in e else None
    return out


def obvious_list(recs: list[dict], gold14: dict) -> list[dict]:
    recs14, _ = v14.map_preds_v14(recs)
    gidx = {g["idx"]: g for g in gold14["labels"]}
    rows = []
    for r in recs14:
        g = gidx[r["idx"]]
        for f in ("in_scope", "medium"):
            if not ev.is_accepted(r["labels"], g, f):
                rows.append({"idx": r["idx"], "canonical_bld_id": g["canonical_bld_id"], "field": f,
                             "gold": ev.norm(g[f]), "gold_acceptable": sorted(ev.accepted_set(g, f)),
                             "pred": ev.norm(r["labels"][f]), "gold_provenance": g["provenance"][f]})
    return rows


# ---------------------------------------------------------------- lane B
def cover_policy(name: str, picks: dict, cgold: dict, gold_by_idx: dict, subsets: dict) -> dict:
    rows = []
    for b, (pick, top3, src) in sorted(picks.items()):
        cg = cgold[b]
        acc = cg["acceptable_default_idx"]
        if not acc:
            t1 = pick is None
            t3 = pick is None
        else:
            t1 = pick in acc
            t3 = any(i in acc for i in (top3 or []))
        crit = ev.critical_cover_reasons(gold_by_idx[pick]) if pick is not None else []
        jcrit = (pick in cg["judge_critical_idx"]) if (cg["judge_critical_idx"] is not None and pick is not None) else None
        rows.append({"canonical_bld_id": b, "pick": pick, "top3": top3, "pick_source": src, "gold_source": cg["source"],
                     "acceptable": acc, "top1_ok": t1, "top3_ok": t3, "critical": bool(crit),
                     "critical_reasons": crit, "judge_listed_critical": jcrit,
                     "miss_kind": (None if t1 else "null_instead_of_valid_default" if pick is None else
                                   "non_null_when_gold_null" if not acc else "pick_not_in_acceptable")})

    def summ(rs):
        n = len(rs)
        if not n:
            return {"n": 0}
        t1 = sum(r["top1_ok"] for r in rs)
        t3 = sum(r["top3_ok"] for r in rs)
        return {"n": n, "top1_ok": t1, "top1": round(t1 / n, 4), "top3_ok": t3, "top3": round(t3 / n, 4),
                "critical": sum(r["critical"] for r in rs),
                "judge_listed_critical": sum(1 for r in rs if r["judge_listed_critical"]),
                "null_picks": sum(1 for r in rs if r["pick"] is None)}

    out = {"policy": name, "all": summ(rows)}
    for sname, members in subsets.items():
        out[sname] = summ([r for r in rows if r["canonical_bld_id"] in members])
    out["misses"] = [r for r in rows if not r["top1_ok"] or r["critical"]]
    out["rows"] = rows
    return out


# ---------------------------------------------------------------- projection
def linfit(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    b = sxy / sxx
    a = my - b * mx
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    ss_tot = sum((y - my) ** 2 for y in ys)
    return a, b, 1 - ss_res / ss_tot if ss_tot else 1.0


def projection(local_run: dict, fetch_run: dict, lcov: dict, ledger: dict, quota: list[dict]) -> dict:
    calls = rj(CODEX / "calls.jsonl")
    lab = [c for c in calls if c["stage"] == "label"]
    cov = [c for c in calls if c["stage"] == "cover" and (c.get("tokens") or {}).get("total")]
    lab_tok = sum(c["tokens"]["total"] for c in lab)
    lab_img = sum(c["n_images"] for c in lab)
    tpi_label = lab_tok / lab_img
    calib = [c for c in rj(CALIB_COVER_CALLS) if c["stage"] == "cover" and (c.get("tokens") or {}).get("total")]
    xs = [c["n_images"] for c in cov + calib]
    ys = [c["tokens"]["total"] for c in cov + calib]
    a, b, r2 = linfit(xs, ys)
    hold_tpb = sum(c["tokens"]["total"] for c in cov) / len(cov)
    nb = len(lcov)
    nc = [x for x in lcov.values() if not x["local_confident"]]
    n_img_all = sum(x["n_images_ok"] for x in lcov.values())
    mean_all = n_img_all / nb
    mean_nc = sum(x["n_images_ok"] for x in nc) / len(nc)
    pop_mean = POP_IMAGES / POP_BUILDINGS
    pop_mean_nc = pop_mean * mean_nc / mean_all
    frac_nc = len(nc) / nb
    cover_blds = POP_BUILDINGS * frac_nc
    tpb_pop = a + b * pop_mean_nc
    cover_tokens = cover_blds * tpb_pop
    ips = local_run["throughput"]["embed_images_per_s_end_to_end"]
    rps = fetch_run["mean_rps_this_run"]
    bpi = fetch_run["bytes_saved_total"] / fetch_run["items"]
    # quota: tokens between the pre_label (0%, fresh window) and post_codex (1%) probes
    qp = {q["label"]: q for q in quota}
    t0, t1 = qp["wp3_n100_pre_label"]["ts"], qp["wp3_n100_post_codex"]["ts"]
    tok_window = sum(r["tokens"] for r in ledger["runs"] if t0 < r["ts"] <= t1)
    u0 = qp["wp3_n100_pre_label"]["rate_limits"]["primary"]["used_percent"]
    u1 = qp["wp3_n100_post_codex"]["rate_limits"]["primary"]["used_percent"]
    # displayed integer percent: worst case (floor display) true delta < (u1 - u0) + 1 point
    cap_lb_floor = tok_window / ((u1 - u0 + 1) / 100)
    cap_lb_round = tok_window / ((u1 - u0 + 0.5) / 100)
    usable_lb = cap_lb_floor * QUOTA_USABLE_FRAC
    scen = {}
    for key, frac in (("T_primary", local_run["routed_frac_T_primary"]),
                      ("T_secondary", local_run["routed_frac_T_secondary"])):
        rimg = POP_IMAGES * frac
        ltok = rimg * tpi_label
        tot = ltok + cover_tokens
        scen[key] = {"routed_frac_holdout": frac, "routed_images": round(rimg), "label_tokens": round(ltok),
                     "cover_buildings": round(cover_blds), "cover_tokens": round(cover_tokens),
                     "total_tokens": round(tot),
                     "weeks_at_quota_lower_bound_usable70": round(tot / usable_lb, 2),
                     "weeks_at_quota_lower_bound_full100": round(tot / cap_lb_floor, 2),
                     "label_calls_batch8": math.ceil(rimg / 8)}
    return {
        "population": {"images": POP_IMAGES, "buildings": POP_BUILDINGS, "mean_gallery": round(pop_mean, 2)},
        "local_gpu": {"images_per_s_end_to_end": ips, "hours": round(POP_IMAGES / ips / 3600, 2),
                      "model_load_s": local_run["throughput"]["model_load_s"],
                      "gpu": local_run.get("gpu"), "note": "sequential single process, warm cache, local JPEG read"},
        "fetch": {"measured_rps": rps, "hours_at_measured_rps": round(POP_IMAGES / rps / 3600, 1),
                  "hours_at_4rps_cap": round(POP_IMAGES / 4 / 3600, 1),
                  "bytes_per_image": round(bpi), "disk_gb": round(POP_IMAGES * bpi / 1e9, 1)},
        "codex_label": {"calls": len(lab), "images_sent": lab_img, "tokens": lab_tok,
                        "tokens_per_image_sent": round(tpi_label, 1), "batch": 8},
        "codex_cover": {"holdout_calls_with_tokens": len(cov), "calib_calls": len(calib),
                        "fit_tokens_eq": f"{a:.0f} + {b:.1f} * n_images", "fit_a": round(a, 1), "fit_b": round(b, 2),
                        "fit_r2": round(r2, 4), "holdout_mean_tokens_per_building": round(hold_tpb),
                        "holdout_mean_gallery_all": round(mean_all, 2), "holdout_mean_gallery_not_confident": round(mean_nc, 2),
                        "pop_mean_gallery_not_confident_est": round(pop_mean_nc, 2),
                        "tokens_per_building_pop_est": round(tpb_pop),
                        "frac_buildings_to_codex_holdout": round(frac_nc, 4)},
        "quota": {"window_tokens": tok_window, "used_percent_before": u0, "used_percent_after": u1,
                  "weekly_capacity_lower_bound_floor_display": round(cap_lb_floor),
                  "weekly_capacity_lower_bound_round_display": round(cap_lb_round),
                  "usable_fraction": QUOTA_USABLE_FRAC, "usable_tokens_per_week_lower_bound": round(usable_lb),
                  "caveat": ("integer used_percent display: 0%->1% over the window; floor-display worst case "
                             "(true delta < 2 points) gives the lower bound. The same quota moved 5%->14% "
                             "between the pool run and this WP with no WP3 calls (shared quota), so other usage "
                             "reduces the share available to vNext.")},
        "scenarios": scen,
    }


# ---------------------------------------------------------------- report
SHORT = v14.SHORT


def render_md(rep: dict) -> str:
    L = []
    A, B, P = rep["lane_a"], rep["lane_b"], rep["projection"]
    L.append(f"# WP3 N100 eval (`{EVAL_VERSION}`)\n")
    L.append(rep["summary_line"] + "\n")
    L.append(f"- preregistration sha `{PREREG_SHA}`; freeze + judge-queue inputs re-verified (0 drift)")
    L.append(f"- gold: codex v1.4 labels, corrected by {rep['gold']['judge_counts']['image']} judge field decisions + "
             f"{rep['gold']['judge_counts']['spot_images']} spot images ({len(rep['gold']['spot_corrections'])} spot corrections) + "
             f"{rep['gold']['judge_counts']['cover']} judged cover sets; decision issues: "
             f"{ {k: len(v) for k, v in rep['gold']['decision_issues'].items()} or 'none'}")
    L.append("- scoring: v1.4 mapping (legibility medium->high; camera unknown accepted for eye_level/elevated gold) "
             "-> unchanged v1 scorer `tools/vnext_wp3_eval.py::evaluate`")
    L.append("- $0: no network, no model calls, no Neon/R2\n")

    L.append("## 1. Lane A: per-field acceptance (1,656 images)\n")
    L.append("| engine | " + " | ".join(SHORT[f] for f in FIELDS) + " | **pooled** | scope err | medium err | routed |")
    L.append("|---|" + "---:|" * len(FIELDS) + "---:|---:|---:|---:|")
    for k, r in A["engines"].items():
        L.append(f"| {k} | " + " | ".join(pct(r["per_field"][f]["acceptance"]) for f in FIELDS)
                 + f" | **{pct(r['pooled'], 2)}** | {r['scope_obvious_errors']} | {r['medium_obvious_errors']} | "
                 + (f"{r['routed_n']} ({pct(r['routed_frac'])})" if "routed_n" in r else "-") + " |")
    L.append("")
    g = A["gate"]
    L.append(f"**Lane A gate** (prereg: funnel T_primary pooled >= 95%): pooled {pct(g['pooled'], 2)} -> "
             f"**{'PASS' if g['pass'] else 'FAIL'}**. Spot-adjusted point {pct(g['pooled_spot_adjusted'], 2)}, "
             f"pessimistic (Wilson upper) {pct(g['pooled_spot_adjusted_pessimistic'], 2)}.\n")
    L.append("### Subsets (pooled 9-field)\n")
    L.append("| engine | judged imgs | unjudged imgs | routed | unrouted | divisare | architizer |")
    L.append("|---|---:|---:|---:|---:|---:|---:|")
    for k, r in A["engines"].items():
        cell = lambda d: f"{pct(d['acceptance'], 2)} (n={d['images']})" if d and d.get("n") else "-"  # noqa: E731
        L.append(f"| {k} | {cell(r['judged_images_pooled'])} | {cell(r['unjudged_images_pooled'])} | "
                 f"{cell(r.get('routed_images_pooled'))} | {cell(r.get('unrouted_images_pooled'))} | "
                 f"{cell(r['per_stratum_pooled'].get('divisare'))} | {cell(r['per_stratum_pooled'].get('architizer'))} |")
    L.append("")
    L.append("- unjudged images: codex == probe under v1.4, gold = that shared value -> correct by construction except the "
             "spot-check corrections (below); the judged subset is where the engines are actually compared.")
    rc = A["routed_probe_vs_codex"]
    L.append("- on the routed images: " + "; ".join(
        f"{k} ({v['images']} imgs) probe {pct(v['probe_pooled'], 2)} (scope/medium err {v['probe_scope_medium_err']}) vs codex "
        f"{pct(v['codex_pooled'], 2)} ({v['codex_scope_medium_err']})" for k, v in rc.items()))
    L.append("- routed images in the funnel carry the codex label and codex is the provisional gold; there the funnel is "
             "only tested where the judge reviewed (prereg `gold.limitation`).\n")
    s = A["codex_error_on_agreement"]
    L.append("### Codex (= shared) error on agreement images, from the 60-image spot check\n")
    L.append(f"- images with >=1 wrong field: {s['images_wrong']}/{s['spot_images']} = {pct(s['image_rate'])} "
             f"(95% CI {pct(s['image_ci'][0])}-{pct(s['image_ci'][1])}); fields wrong {s['fields_wrong']}/{s['spot_fields']} = "
             f"{pct(s['field_rate'], 2)} (CI {pct(s['field_ci'][0], 2)}-{pct(s['field_ci'][1], 2)})")
    L.append(f"- extrapolated to the {s['unspotted_agreement_images']} un-spotted agreement images: ~{s['est_undetected_field_errors']} "
             f"field errors (CI upper {s['est_undetected_field_errors_ci_upper']}), ~{s['est_images_with_error']} images; these "
             f"are shared by probe, codex and the funnel, so every pooled number above is high by ~{s['pooled_bias_pp']:.2f}pp "
             f"(<= {s['pooled_bias_pp_ci_upper']:.2f}pp).")
    for c in s["corrections"]:
        L.append(f"  - idx {c['idx']} {c['field']}: shared `{c['shared_value']}` -> `{c['judge_value']}` ({c['note']})")
    L.append("")
    L.append("### Scope / medium obvious errors of the funnel (T_primary)\n")
    ol = A["obvious_errors_funnel_T_primary"]
    if not ol:
        L.append("- none\n")
    for o in ol:
        L.append(f"- idx {o['idx']} {o['canonical_bld_id']} {o['field']}: gold `{o['gold']}` (acc {o['gold_acceptable']}, "
                 f"{o['gold_provenance']}) vs funnel `{o['pred']}`")
    L.append("")
    L.append("### Confusion: funnel T_primary (rows = gold primary, cols = funnel)\n")
    fr = A["engines"]["funnel_T_primary"]
    for f in FIELDS:
        cm = fr["confusion"][f]
        cols = sorted({c for row in cm.values() for c in row})
        L.append(f"**{f}** (acceptance {pct(fr['per_field'][f]['acceptance'])})\n")
        L.append("| gold \\ funnel | " + " | ".join(cols) + " |")
        L.append("|---|" + "---:|" * len(cols))
        for gk in sorted(cm):
            L.append(f"| {gk} | " + " | ".join(str(cm[gk].get(c, "")) for c in cols) + " |")
        L.append("")
    L.append("Probe-only / codex-only / T_secondary matrices: `eval.json` -> lane_a.engines.*.confusion.\n")

    L.append("## 2. Lane B: covers (100 buildings)\n")
    L.append("| policy | top-1 | top-3 | critical | NULL picks | judged subset top-1 (n) | local-confident top-1 / crit | divisare top-1 | architizer top-1 |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for k, r in B["policies"].items():
        a = r["all"]
        j, lc = r.get("judged", {}), r.get("local_confident", {})
        L.append(f"| {k} | {a['top1_ok']}/{a['n']} ({pct(a['top1'])}) | {a['top3_ok']}/{a['n']} ({pct(a['top3'])}) | "
                 f"{a['critical']} | {a['null_picks']} | "
                 + (f"{j['top1_ok']}/{j['n']}" if j.get("n") else "-") + " | "
                 + (f"{lc['top1_ok']}/{lc['n']} / {lc['critical']}" if lc.get("n") else "-") + " | "
                 + " | ".join(f"{r[s]['top1_ok']}/{r[s]['n']}" if r[s].get("n") else "-"
                              for s in ("divisare", "architizer")) + " |")
    L.append("")
    p0 = B["p0_baseline"]
    L.append(f"- P0 baseline (legacy display_cover_url, exact URL match): mapped {p0['mapped']}/100, unmapped {p0['unmapped']}; "
             f"on mapped buildings baseline top-1 {p0['baseline_top1_ok']}/{p0['mapped']} crit {p0['baseline_critical']} vs production "
             f"top-1 {p0['production_top1_ok']}/{p0['mapped']} crit {p0['production_critical']} -> regression: "
             f"{'NO' if p0['no_regression'] else 'YES'}")
    ph = p0["supplementary_hostmapped"]
    L.append(f"- supplementary (not registered): Architizer host rewrite (imgix -> images.architizer.com) maps "
             f"{ph['mapped']}/100; baseline top-1 {ph['baseline_top1_ok']}/{ph['mapped']} crit {ph['baseline_critical']} vs production "
             f"top-1 {ph['production_top1_ok']}/{ph['mapped']} crit {ph['production_critical']} -> regression: "
             f"{'NO' if ph['no_regression'] else 'YES'}")
    di = rep["gold"]["decision_issues"]
    if di:
        L.append(f"- judge decision issues (gold uses the judge value as written): {di}")
    L.append(f"- codex sent covers (production funnel): {B['codex_sent']}/100 = {pct(B['codex_sent_frac'])}")
    L.append(f"- codex ranked[0] outside its own acceptable set: {B['codex_rank0_not_in_own_acceptable']}")
    gb = B["gate"]
    L.append(f"\n**Lane B gate** (top-1 >= 95%, top-3 >= 99%, critical 0, no P0 regression): top-1 {pct(gb['top1'])}, "
             f"top-3 {pct(gb['top3'])}, critical {gb['critical']}, P0 no-regression {gb['no_regression']} -> "
             f"**{'PASS' if gb['pass'] else 'FAIL'}** ({'; '.join(gb['fail_reasons']) or 'all criteria met'})\n")
    L.append("Limitation: for the 87 non-confident buildings that were not judged, gold = codex acceptable set and the pick "
             "= codex ranked[0], so they are correct by construction; the informative buildings are the judged 23 and the "
             "13 local-confident ones.\n")
    L.append("### Production misses / critical\n")
    L.append("| bld | pick (source) | gold source | acceptable | miss | critical | judge-listed critical |")
    L.append("|---|---|---|---|---|---|---|")
    for r in B["policies"]["production"]["misses"]:
        L.append(f"| {r['canonical_bld_id']} | {r['pick']} ({r['pick_source']}) | {r['gold_source']} | {r['acceptable']} | "
                 f"{r['miss_kind'] or '-'} | {r['critical_reasons'] or '-'} | {r['judge_listed_critical']} |")
    L.append("")
    L.append("## 3. Full-run projection (699,038 images / 39,248 buildings)\n")
    lg, fe, cl, cc, q = P["local_gpu"], P["fetch"], P["codex_label"], P["codex_cover"], P["quota"]
    L.append(f"- local GPU: {lg['images_per_s_end_to_end']} img/s end-to-end ({lg['gpu']}) -> **{lg['hours']} h** (+ model load {lg['model_load_s']} s)")
    L.append(f"- fetch: measured {fe['measured_rps']} req/s -> **{fe['hours_at_measured_rps']} h** ({fe['hours_at_4rps_cap']} h at the 4 rps cap); "
             f"disk ~{fe['disk_gb']} GB ({fe['bytes_per_image']} B/img)")
    L.append(f"- codex label: {cl['tokens_per_image_sent']} tokens/img sent (holdout {cl['calls']} calls / {cl['images_sent']} imgs, batch 8)")
    L.append(f"- codex cover: tokens/building = {cc['fit_tokens_eq']} (R² {cc['fit_r2']}, {cc['holdout_calls_with_tokens']} holdout + "
             f"{cc['calib_calls']} calib calls); holdout mean {cc['holdout_mean_tokens_per_building']}/bld; non-confident gallery "
             f"holdout {cc['holdout_mean_gallery_not_confident']} vs all {cc['holdout_mean_gallery_all']} -> pop est "
             f"{cc['pop_mean_gallery_not_confident_est']} imgs -> {cc['tokens_per_building_pop_est']} tokens/bld; "
             f"codex fraction {pct(cc['frac_buildings_to_codex_holdout'])}")
    L.append(f"- quota: {q['window_tokens']:,} tokens moved {q['used_percent_before']}%->{q['used_percent_after']}% -> weekly capacity "
             f">= {q['weekly_capacity_lower_bound_floor_display']:,} (floor display; {q['weekly_capacity_lower_bound_round_display']:,} if rounded); "
             f"usable 70% -> {q['usable_tokens_per_week_lower_bound']:,}/week. {q['caveat']}\n")
    L.append("| scenario | routed imgs | label tokens | cover blds | cover tokens | **total** | weeks (70% usable, LB) | weeks (100%, LB) |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for k, s in P["scenarios"].items():
        L.append(f"| {k} ({pct(s['routed_frac_holdout'], 2)}) | {s['routed_images']:,} | {s['label_tokens']:,} | {s['cover_buildings']:,} | "
                 f"{s['cover_tokens']:,} | **{s['total_tokens']:,}** | {s['weeks_at_quota_lower_bound_usable70']} | "
                 f"{s['weeks_at_quota_lower_bound_full100']} |")
    L.append("")
    L.append("## 4. Inputs\n")
    for k, v in rep["inputs_sha256"].items():
        L.append(f"- `{k}` `{v}`")
    L.append(f"- tool `tools/vnext_wp3_n100_eval.py` `{rep['tool_sha256']}`")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-write", action="store_true")
    a = ap.parse_args(argv)

    chk = check_inputs()
    queue = chk["queue"]
    prereg = json.loads(PREREG.read_text(encoding="utf-8"))
    probe = {r["idx"]: r for r in rj(WP / "local_pred.jsonl")}
    codex = {r["idx"]: r for r in rj(CODEX / "labels.jsonl")}
    fetch = {r["idx"]: r for r in rj(WP / "fetch_log.jsonl") if r.get("status") == "ok"}
    lcov = {r["canonical_bld_id"]: r for r in rj(WP / "local_covers.jsonl")}
    ccov = {r["canonical_bld_id"]: r for r in rj(CODEX / "cover.jsonl") if r.get("complete")}
    local_run = json.loads((WP / "local_run.json").read_text(encoding="utf-8"))
    fetch_run = json.loads((WP / "fetch_run.json").read_text(encoding="utf-8"))
    ledger = json.loads((WP / "ledger_wp3_n100.json").read_text(encoding="utf-8"))
    quota = rj(WP / "quota_probes.jsonl")
    man_p = ROOT / prereg["holdout"]["manifest"]
    if sha(man_p) != prereg["holdout"]["manifest_sha256"]:
        raise SystemExit("holdout manifest SHA drift")
    man = json.loads(man_p.read_text(encoding="utf-8"))
    chk["sha256"][rel(man_p)] = prereg["holdout"]["manifest_sha256"]

    order = sorted(fetch)
    bad = [i for i in order if i not in codex or codex[i].get("status") != "ok" or i not in probe]
    if bad:
        raise SystemExit(f"{len(bad)} images without ok codex/probe label")
    dec = load_decisions(queue, probe, codex)
    gold, gstats = build_gold(order, probe, codex, queue, dec)
    gold14, gmap = v14.map_gold_v14(gold)
    bld_imgs = defaultdict(list)
    for i in order:
        bld_imgs[probe[i]["canonical_bld_id"]].append(i)
    cgold, cissues = build_cover_gold(lcov, ccov, dec, bld_imgs)
    gold_by_idx = {g["idx"]: g for g in gold["labels"]}
    stratum = {i: probe[i]["stratum"] for i in order}
    judged = {e["idx"] for e in queue["entries"]["image"]}

    # ---------------- lane A
    probe_recs = [probe[i] for i in order]
    codex_recs = [{**codex[i], "stratum": probe[i]["stratum"]} for i in order]
    r1 = {i for i in order if probe[i]["routed_T_primary"]}
    r2 = {i for i in order if probe[i]["routed_T_secondary"]}

    def funnel(routed):
        return [{**probe[i], "labels": (codex[i]["labels"] if i in routed else probe[i]["labels"]),
                 "label_source": "codex" if i in routed else "probe"} for i in order]

    engines = {
        "probe": score_engine("probe", probe_recs, gold14, None, judged, stratum),
        "codex": score_engine("codex", codex_recs, gold14, None, judged, stratum),
        "funnel_T_primary": score_engine("funnel_T_primary", funnel(r1), gold14, r1, judged, stratum),
        "funnel_T_secondary": score_engine("funnel_T_secondary", funnel(r2), gold14, r2, judged, stratum),
    }
    for e in engines.values():
        e.pop("_obvious", None)
    # probe vs codex on the routed images (what routing buys)
    routed_compare = {}
    for key, rs in (("T_primary", r1), ("T_secondary", r2)):
        pe = score_engine("p", [probe[i] for i in sorted(rs)], {"labels": [gold14["labels"][order.index(i)] for i in sorted(rs)]},
                          None, set(), stratum)
        ce = score_engine("c", [codex_recs[order.index(i)] for i in sorted(rs)],
                          {"labels": [gold14["labels"][order.index(i)] for i in sorted(rs)]}, None, set(), stratum)
        routed_compare[key] = {"images": len(rs), "probe_pooled": pe["pooled"], "codex_pooled": ce["pooled"],
                               "probe_scope_medium_err": [pe["scope_obvious_errors"], pe["medium_obvious_errors"]],
                               "codex_scope_medium_err": [ce["scope_obvious_errors"], ce["medium_obvious_errors"]]}

    # spot-check estimate of the shared (agreement) error
    spot_idx = [e["idx"] for e in queue["entries"]["spot"]]
    corr = gstats["spot_corrections"]
    img_wrong = len({c["idx"] for c in corr})
    f_wrong = len(corr)
    ns, nf = len(spot_idx), len(spot_idx) * len(FIELDS)
    agree_pool = queue["summary"]["agreement_pool"]
    unspotted = agree_pool - ns
    pf = f_wrong / nf
    pf_ci = wilson(f_wrong, nf)
    pi_ci = wilson(img_wrong, ns)
    est = unspotted * len(FIELDS) * pf
    est_hi = unspotted * len(FIELDS) * pf_ci[1]
    pooled_n = engines["funnel_T_primary"]["pooled_n"]
    spot_est = {"spot_images": ns, "spot_fields": nf, "images_wrong": img_wrong, "fields_wrong": f_wrong,
                "image_rate": round(img_wrong / ns, 4), "image_ci": [round(x, 4) for x in pi_ci],
                "field_rate": round(pf, 5), "field_ci": [round(x, 5) for x in pf_ci],
                "agreement_pool": agree_pool, "unspotted_agreement_images": unspotted,
                "est_undetected_field_errors": round(est, 1), "est_undetected_field_errors_ci_upper": round(est_hi, 1),
                "est_images_with_error": round(unspotted * img_wrong / ns, 1),
                "pooled_bias_pp": 100 * est / pooled_n, "pooled_bias_pp_ci_upper": 100 * est_hi / pooled_n,
                "corrections": corr,
                "note": "spot images are a random sample of the agreement pool; their corrections are already in gold"}
    f1 = engines["funnel_T_primary"]
    adj = (f1["pooled_accepted"] - est) / pooled_n
    adj_hi = (f1["pooled_accepted"] - est_hi) / pooled_n
    gate_a = {"criterion": prereg["gates_n100"]["lane_A"]["criterion"], "pooled": f1["pooled"],
              "pooled_spot_adjusted": round(adj, 4), "pooled_spot_adjusted_pessimistic": round(adj_hi, 4),
              "pass": f1["pooled"] >= LANE_A_MIN, "pass_spot_adjusted": adj >= LANE_A_MIN,
              "pass_spot_adjusted_pessimistic": adj_hi >= LANE_A_MIN,
              "scope_obvious_errors": f1["scope_obvious_errors"], "medium_obvious_errors": f1["medium_obvious_errors"],
              "fields_below_95pct": [f for f in FIELDS if f1["per_field"][f]["acceptance"] < LANE_A_MIN]}
    lane_a = {"engines": engines, "gate": gate_a, "routed_probe_vs_codex": routed_compare,
              "codex_error_on_agreement": spot_est,
              "obvious_errors_funnel_T_primary": obvious_list(funnel(r1), gold14),
              "obvious_errors_probe": obvious_list(probe_recs, gold14),
              "obvious_errors_codex": obvious_list(codex_recs, gold14)}

    # ---------------- lane B
    conf = {b for b, r in lcov.items() if r["local_confident"]}
    conf_cons = {b for b, r in lcov.items() if r["default_idx"] is not None and r["confidence_top1"] >= 0.95}
    rank0_bad = [b for b, c in ccov.items() if c["ranked_idx"] and c["ranked_idx"][0] not in c["acceptable_idx"]]

    def cx_pick(b):
        rk = list(ccov[b]["ranked_idx"])
        return (rk[0] if rk else None), rk[:3]

    def prod_picks(cset):
        out = {}
        for b, r in lcov.items():
            if b in cset:
                out[b] = (r["default_idx"], list(r["top3_idx"]), "local_confident")
            else:
                p, t3 = cx_pick(b)
                out[b] = (p, t3, "codex_cover")
        return out

    url_to_idx = {}
    for i in order:
        f = fetch[i]
        url_to_idx[(probe[i]["canonical_bld_id"], f["manifest_url"])] = i
    p0, p0h = {}, {}
    unmapped, unmapped_h = [], []
    for mb in man["buildings"]:
        b = mb["canonical_bld_id"]
        u = mb.get("display_cover_url")
        i = url_to_idx.get((b, u))
        if i is None:
            unmapped.append(b)
        else:
            p0[b] = (i, [i], "p0_legacy_display_cover_url")
        # supplementary (NOT the registered rule): Architizer host move, same rewrite as
        # tools/architizer_image_host_migrate.py (old imgix host -> images.architizer.com<path>?width=1680&quality=60)
        uh = re.sub(ARCH_OLD_PAT, ARCH_NEW_REP, u) if u else u
        ih = url_to_idx.get((b, uh))
        if ih is None:
            unmapped_h.append(b)
        else:
            p0h[b] = (ih, [ih], "p0_legacy_display_cover_url_hostmapped")
    subsets = {"judged": set(dec["cover"]), "local_confident": conf, "not_local_confident": set(lcov) - conf,
               "divisare": {b for b, r in lcov.items() if r["stratum"] == "divisare"},
               "architizer": {b for b, r in lcov.items() if r["stratum"] == "architizer"}}
    policies = {
        "production": cover_policy("production", prod_picks(conf), cgold, gold_by_idx, subsets),
        "whatif_conservative_A095_M0": cover_policy("whatif", prod_picks(conf_cons), cgold, gold_by_idx, subsets),
        "codex_all": cover_policy("codex_all", {b: (*cx_pick(b), "codex_cover") for b in lcov}, cgold, gold_by_idx, subsets),
        "local_all": cover_policy("local_all", {b: (r["default_idx"], list(r["top3_idx"]), "local_default")
                                                for b, r in lcov.items()}, cgold, gold_by_idx, subsets),
        "p0_baseline_mapped": cover_policy("p0", p0, cgold, gold_by_idx, subsets),
        "p0_baseline_hostmapped_supplementary": cover_policy("p0h", p0h, cgold, gold_by_idx, subsets),
    }
    p0h_rows = policies["p0_baseline_hostmapped_supplementary"]["rows"]
    p0h_s = {"mapped": len(p0h_rows), "unmapped": len(unmapped_h), "unmapped_buildings": unmapped_h,
             "baseline_top1_ok": sum(r["top1_ok"] for r in p0h_rows),
             "baseline_critical": sum(r["critical"] for r in p0h_rows)}
    prod_rows = {r["canonical_bld_id"]: r for r in policies["production"]["rows"]}
    p0_rows = policies["p0_baseline_mapped"]["rows"]
    mapped = [r["canonical_bld_id"] for r in p0_rows]
    p0s = {"mapped": len(mapped), "unmapped": len(unmapped), "unmapped_buildings": unmapped,
           "baseline_top1_ok": sum(r["top1_ok"] for r in p0_rows), "baseline_critical": sum(r["critical"] for r in p0_rows),
           "production_top1_ok": sum(prod_rows[b]["top1_ok"] for b in mapped),
           "production_critical": sum(prod_rows[b]["critical"] for b in mapped)}
    p0s["no_regression"] = (p0s["production_top1_ok"] >= p0s["baseline_top1_ok"]
                            and p0s["production_critical"] <= p0s["baseline_critical"])
    prod_rows_h = {r["canonical_bld_id"] for r in p0h_rows}
    p0h_s["production_top1_ok"] = sum(prod_rows[b]["top1_ok"] for b in prod_rows_h)
    p0h_s["production_critical"] = sum(prod_rows[b]["critical"] for b in prod_rows_h)
    p0h_s["no_regression"] = (p0h_s["production_top1_ok"] >= p0h_s["baseline_top1_ok"]
                              and p0h_s["production_critical"] <= p0h_s["baseline_critical"])
    p0h_s["note"] = ("supplementary, not the registered rule: Architizer display_cover_url rewritten from the retired "
                     "imgix host with the tools/architizer_image_host_migrate.py rule before exact matching")
    p0s["supplementary_hostmapped"] = p0h_s
    pa = policies["production"]["all"]
    fr = []
    if pa["top1"] < LANE_B_TOP1_MIN:
        fr.append(f"top-1 {pa['top1_ok']}/{pa['n']} < 95%")
    if pa["top3"] < LANE_B_TOP3_MIN:
        fr.append(f"top-3 {pa['top3_ok']}/{pa['n']} < 99%")
    if pa["critical"]:
        fr.append(f"{pa['critical']} critical")
    if not p0s["no_regression"]:
        fr.append("regression vs P0")
    gate_b = {"criterion": prereg["gates_n100"]["lane_B"]["criterion"], "top1": pa["top1"], "top3": pa["top3"],
              "critical": pa["critical"], "no_regression": p0s["no_regression"], "pass": not fr, "fail_reasons": fr,
              "stop_rule_critical_final_pick": pa["critical"] > 0}
    lane_b = {"policies": policies, "p0_baseline": p0s, "gate": gate_b,
              "codex_sent": len(lcov) - len(conf), "codex_sent_frac": round((len(lcov) - len(conf)) / len(lcov), 4),
              "codex_sent_whatif_conservative": len(lcov) - len(conf_cons),
              "codex_rank0_not_in_own_acceptable": rank0_bad, "cover_gold_issues": cissues}

    proj = projection(local_run, fetch_run, lcov, ledger, quota)

    summary = (f"WP3 N100: lane A {'PASS' if gate_a['pass'] else 'FAIL'} ({100 * f1['pooled']:.2f}%), "
               f"lane B {'PASS' if gate_b['pass'] else 'FAIL'} (top1 {100 * pa['top1']:.1f}%, crit {pa['critical']}), "
               f"codex sent covers {100 * lane_b['codex_sent_frac']:.1f}%, tokens {ledger['total_tokens']}")
    gold_doc = {"version": "vnext-wp3-n100-gold-v1", "preregistration_sha256": PREREG_SHA,
                "contract_version": "vnext-gallery-semantics-contract-v1.4",
                "rule": prereg["gold"], "labels": gold["labels"],
                "covers": [{"canonical_bld_id": b, **v} for b, v in cgold.items()],
                "judge_decisions_sha256": sha(DECISIONS), "judge_queue_sha256": sha(QUEUE)}
    rep = {"eval_version": EVAL_VERSION, "tool": "tools/vnext_wp3_n100_eval.py", "tool_sha256": sha(Path(__file__)),
           "summary_line": summary, "preregistration_sha256": PREREG_SHA,
           "gold": {"judge_counts": dec["counts"], "decision_issues": dec["issues"], **gstats,
                    "v14_gold_mapping_stats": gmap,
                    "cover_gold_sources": dict(Counter(v["source"] for v in cgold.values())),
                    "gold_file": rel(WP / "gold_n100.json")},
           "lane_a": lane_a, "lane_b": lane_b, "projection": proj,
           "codex_tokens_wp_total": ledger["total_tokens"], "codex_token_cap": ledger["cap_total_tokens"],
           "inputs_sha256": chk["sha256"], "paid_calls": 0, "network_requests": 0}
    md = render_md(rep)
    if not a.no_write:
        (WP / "gold_n100.json").write_text(json.dumps(gold_doc, indent=1, ensure_ascii=False), encoding="utf-8")
        rep["gold"]["gold_file_sha256"] = sha(WP / "gold_n100.json")
        (WP / "eval.json").write_text(json.dumps(rep, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
        (WP / "eval.md").write_text(md, encoding="utf-8")
    print(summary)
    print(json.dumps({"decision_issues": {k: len(v) for k, v in dec["issues"].items()},
                      "lane_a_gate": gate_a, "lane_b_gate": gate_b,
                      "pooled": {k: e["pooled"] for k, e in engines.items()},
                      "routed_compare": routed_compare,
                      "cover_policies": {k: {kk: v["all"][kk] for kk in ("top1_ok", "top3_ok", "critical", "null_picks")}
                                         for k, v in policies.items()},
                      "p0": {k: v for k, v in p0s.items() if k != "unmapped_buildings"},
                      "spot": {k: spot_est[k] for k in ("images_wrong", "fields_wrong", "image_ci", "pooled_bias_pp")},
                      "scenarios": proj["scenarios"]}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
