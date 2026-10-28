#!/usr/bin/env python3
"""vNext WP3 N10: evaluate the codex compact remote labeler (codex_v1) vs Claude gold, contract v1.4.

- labels: `data/smoke_vnext/wp3_n10_v1/codex_v1/labels.jsonl` (tools/vnext_s3_codex_engine.py)
- covers: `codex_v1/cover.jsonl` (comparative per-building call) + a label-derived deterministic default
- gold: `gold_labels_final_v2.json`, v1.4 mapping and scoring delegated to the unchanged
  `tools/vnext_wp3_eval_v14.py` (map_gold_v14 / map_preds_v14 / score), which itself wraps the
  unchanged v1 scorer `tools/vnext_wp3_eval.py::evaluate`
- uncertainty signal: local (exp_v2 combo + zs_v2 cross-fold) vs codex agreement, per field and
  per image; funnel simulation (local if agree else codex)
- extrapolation from the measured per-call token/second log (cover top-3 checks on 39,248
  buildings; routing X% of 699,038 S6 images)

Failed / not-run codex rows are scored as wrong on every field (value `__failed__`).
Writes only `codex_v1/eval.{json,md}`. Offline: no network, no model calls, no Neon/R2.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import vnext_wp3_eval as ev  # noqa: E402
from tools import vnext_wp3_eval_v14 as v14  # noqa: E402

WP = ROOT / "data" / "smoke_vnext" / "wp3_n10_v1"
CX = WP / "codex_v1"
EVAL_VERSION = "vnext-wp3-codex-eval-v1"
FIELDS = ev.FIELDS
FAILED = "__failed__"
LOCAL_ENGINES = [("combo_zsScope_probe_zs_logits_only", "exp_v2/pred_combo_zsScope_probe_zs_logits_only.jsonl"),
                 ("zs_v2_crossfold", "exp_v2/pred_zs_v2_crossfold.jsonl")]
POP_S6_IMAGES = ev.POP_S6_IMAGES
POP_S6_BUILDINGS = ev.POP_S6_BUILDINGS
ROUTE_PCTS = (5, 10, 13, 20, 30, 56, 100)
LABEL_BATCH = 8


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def rj(p: Path) -> list[dict]:
    with open(p, encoding="utf-8") as fh:
        return [json.loads(x) for x in fh if x.strip()]


def pct(x):
    return "-" if x is None else f"{100 * x:.1f}%"


# ---------------------------------------------------------------- codex records
def codex_records(rows: list[dict]) -> tuple[list[dict], Counter]:
    out, st = [], Counter()
    for r in rows:
        st[r["status"]] += 1
        lab = r["labels"] if r["status"] == "ok" and r.get("labels") else {f: FAILED for f in FIELDS}
        out.append({"idx": r["idx"], "canonical_bld_id": r["canonical_bld_id"], "stratum": r["stratum"],
                    "labels": dict(lab), "scores": {}})
    return out, st


def cover_run_doc(cover_rows: list[dict], names: dict) -> dict:
    b = {}
    for r in cover_rows:
        ok = r.get("status") == "ok"
        ranked = r.get("ranked_idx") or [] if ok else []
        b[r["canonical_bld_id"]] = {"name": names[r["canonical_bld_id"]][0], "stratum": names[r["canonical_bld_id"]][1],
                                    "default_cover_idx": ranked[0] if ranked else None, "top3_idx": ranked[:3],
                                    "review_required": not ranked, "status": r.get("status"),
                                    "acceptable_idx": r.get("acceptable_idx")}
    for bld in names:
        b.setdefault(bld, {"name": names[bld][0], "stratum": names[bld][1], "default_cover_idx": None,
                           "top3_idx": [], "review_required": True, "status": "missing"})
    return {"buildings": b}


def label_derived_cover(recs: list[dict], names: dict, sort_order: dict) -> dict:
    """Deterministic default from codex labels only (contract COVERS §1 default eligibility):
    tier 1 = photograph+exterior+overall non-aerial, tier 2 = photograph+exterior+overall aerial;
    within tier: legibility high first, then source sort_order."""
    by = {}
    for r in recs:
        L = r["labels"]
        if L.get("in_scope") is not True or L["medium"] != "photograph" or L["spatial_context"] != "exterior" \
                or L["framing_scale"] != "overall":
            continue
        tier = 2 if L["camera_angle"] in ("aerial_oblique", "aerial_top_down") else 1
        by.setdefault(r["canonical_bld_id"], []).append((tier, 0 if L["project_legibility"] == "high" else 1,
                                                         sort_order[r["idx"]], r["idx"]))
    out = {}
    for bld in names:
        c = sorted(by.get(bld, []))
        out[bld] = {"name": names[bld][0], "stratum": names[bld][1],
                    "default_cover_idx": c[0][3] if c else None, "top3_idx": [x[3] for x in c[:3]],
                    "review_required": not c}
    return {"buildings": out}


def subset_gold(gold: dict, keep: set[int]) -> dict:
    g = copy.deepcopy(gold)
    g["labels"] = [x for x in g["labels"] if x["idx"] in keep]
    return g


# ---------------------------------------------------------------- local vs codex agreement
def agreement(local: list[dict], codex: list[dict], gold14: dict) -> dict:
    gi = {g["idx"]: g for g in gold14["labels"]}
    li = {r["idx"]: r for r in local}
    ci = {r["idx"]: r for r in codex}
    idxs = sorted(set(li) & set(ci) & set(gi))

    def acc(rec, i, f):
        return ev.norm(rec["labels"][f]) in ev.accepted_set(gi[i], f)

    per_field = {}
    for f in FIELDS:
        A = [i for i in idxs if ev.norm(li[i]["labels"][f]) == ev.norm(ci[i]["labels"][f])]
        D = [i for i in idxs if i not in set(A)]
        per_field[f] = {
            "agree_n": len(A), "disagree_n": len(D), "disagree_pct": round(len(D) / len(idxs), 4),
            "local_acc_on_agree": round(sum(acc(li[i], i, f) for i in A) / len(A), 4) if A else None,
            "local_acc_on_disagree": round(sum(acc(li[i], i, f) for i in D) / len(D), 4) if D else None,
            "codex_acc_on_disagree": round(sum(acc(ci[i], i, f) for i in D) / len(D), 4) if D else None,
            "local_errors_total": sum(not acc(li[i], i, f) for i in idxs),
            "local_errors_caught_by_disagreement": sum(not acc(li[i], i, f) for i in D),
            "both_wrong_on_agree": sum(not acc(li[i], i, f) for i in A),
        }
    # image level
    def any_dis(i, fs):
        return any(ev.norm(li[i]["labels"][f]) != ev.norm(ci[i]["labels"][f]) for f in fs)

    def pooled(rec_of, ids):
        n = len(ids) * len(FIELDS)
        return round(sum(acc(rec_of(i), i, f) for i in ids for f in FIELDS) / n, 4) if ids else None

    img = {}
    for name, fs in (("any_of_9", FIELDS), ("scope_or_medium", ("in_scope", "medium"))):
        D = [i for i in idxs if any_dis(i, fs)]
        A = [i for i in idxs if i not in set(D)]
        img[name] = {"disagree_images": len(D), "routed_pct": round(len(D) / len(idxs), 4),
                     "local_pooled_on_agree": pooled(lambda i: li[i], A),
                     "local_pooled_on_disagree": pooled(lambda i: li[i], D),
                     "codex_pooled_on_disagree": pooled(lambda i: ci[i], D)}
    # funnel simulations
    def funnel(route_rule):
        recs = []
        routed = 0
        for i in idxs:
            r = route_rule(i)
            routed += r
            recs.append({"idx": i, "labels": dict(ci[i]["labels"] if r else li[i]["labels"])})
        ok = sum(acc(x, x["idx"], f) for x in recs for f in FIELDS)
        scope_err = sum(not acc(x, x["idx"], "in_scope") for x in recs)
        med_err = sum(not acc(x, x["idx"], "medium") for x in recs)
        per = {f: round(sum(acc(x, x["idx"], f) for x in recs) / len(recs), 4) for f in FIELDS}
        return {"routed_images": routed, "routed_pct": round(routed / len(idxs), 4),
                "pooled": round(ok / (len(recs) * len(FIELDS)), 4), "scope_errors": scope_err,
                "medium_errors": med_err, "per_field": per}

    fun = {
        "local_only": funnel(lambda i: False),
        "route_if_any_of_9_disagree": funnel(lambda i: any_dis(i, FIELDS)),
        "route_if_scope_or_medium_disagree": funnel(lambda i: any_dis(i, ("in_scope", "medium"))),
        "codex_only": funnel(lambda i: True),
    }
    return {"n": len(idxs), "per_field": per_field, "image_level": img, "funnel": fun}


# ---------------------------------------------------------------- token / time extrapolation
def linfit(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    vx = sum((x - mx) ** 2 for x in xs)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / vx if vx else 0.0
    return my - b * mx, b


def extrapolate(calls: list[dict], label_run: dict, cover_run: dict, quota: list[dict]) -> dict:
    lab = [c for c in calls if c["stage"] == "label" and c["tokens"].get("total")]
    cov = [c for c in calls if c["stage"] == "cover" and c["tokens"].get("total")]
    a_l, b_l = linfit([c["n_images"] for c in lab], [c["tokens"]["total"] for c in lab])
    s_a_l, s_b_l = linfit([c["n_images"] for c in lab], [c["seconds"] for c in lab])
    out = {"label_fit_tokens": {"overhead_per_call": round(a_l), "per_image_768": round(b_l), "calls": len(lab)},
           "label_fit_seconds": {"per_call": round(s_a_l, 2), "per_image": round(s_b_l, 3)}}
    full8 = [c for c in lab if c["n_images"] == LABEL_BATCH and c["attempt"] == 1]
    tpc8 = sum(c["tokens"]["total"] for c in full8) / len(full8) if full8 else a_l + LABEL_BATCH * b_l
    spc8 = sum(c["seconds"] for c in full8) / len(full8) if full8 else s_a_l + LABEL_BATCH * s_b_l
    unc8 = (sum((c["tokens"]["input"] - (c["tokens"]["cached_input"] or 0)) + c["tokens"]["output"] for c in full8)
            / len(full8)) if full8 else None
    out["label_batch8_measured"] = {"calls": len(full8), "mean_total_tokens_per_call": round(tpc8),
                                    "mean_uncached_tokens_per_call": round(unc8) if unc8 else None,
                                    "mean_seconds_per_call": round(spc8, 2),
                                    "total_tokens_per_image": round(tpc8 / LABEL_BATCH)}
    lw = (label_run or {}).get("last_invocation_wall_s")
    lw_imgs = (label_run or {}).get("calls", {}).get("images_sent")
    workers = (label_run or {}).get("workers")
    out["label_observed_throughput"] = {"wall_s": lw, "images_sent": lw_imgs, "workers": workers,
                                        "images_per_hour": round(3600 * lw_imgs / lw) if lw and lw_imgs else None}
    if cov:
        a_c, b_c = linfit([c["n_images"] for c in cov], [c["tokens"]["total"] for c in cov])
        s_a_c, s_b_c = linfit([c["n_images"] for c in cov], [c["seconds"] for c in cov])
        out["cover_fit_tokens"] = {"overhead_per_call": round(a_c), "per_image_512": round(b_c), "calls": len(cov)}
        out["cover_fit_seconds"] = {"per_call": round(s_a_c, 2), "per_image": round(s_b_c, 3)}
        top3 = {"tokens_per_building_512": round(a_c + 3 * b_c), "tokens_per_building_768": round(a_c + 3 * b_l),
                "seconds_per_call": round(s_a_c + 3 * s_b_c, 2)}
        for k in ("512", "768"):
            t = top3[f"tokens_per_building_{k}"]
            top3[f"total_tokens_39248_{k}"] = t * POP_S6_BUILDINGS
        sec = top3["seconds_per_call"] * POP_S6_BUILDINGS
        top3["wall_h_sequential"] = round(sec / 3600, 1)
        top3["wall_h_3_workers"] = round(sec / 3600 / 3, 1)
        top3["full_gallery_cover_call_mean_tokens"] = round(sum(c["tokens"]["total"] for c in cov) / len(cov))
        out["cover_top3_check_39248"] = top3
    rows = []
    iph = out["label_observed_throughput"]["images_per_hour"]
    for p in ROUTE_PCTS:
        n = round(POP_S6_IMAGES * p / 100)
        calls_n = -(-n // LABEL_BATCH)
        tok = calls_n * tpc8
        rows.append({"route_pct": p, "images": n, "calls_batch8": calls_n, "total_tokens": round(tok),
                     "wall_h_sequential": round(calls_n * spc8 / 3600, 1),
                     "wall_h_observed_throughput": round(n / iph, 1) if iph else None})
    out["routing_699038"] = rows
    out["pool_cap_8M_images_at_batch8"] = round(8_000_000 / (tpc8 / LABEL_BATCH))
    # quota
    q = [x for x in quota if x.get("rate_limits")]
    if len(q) >= 2:
        q0, q1 = q[0], q[-1]
        du = q1["rate_limits"]["primary"]["used_percent"] - q0["rate_limits"]["primary"]["used_percent"]
        b0 = float(q0["rate_limits"]["credits"]["balance"])
        b1 = float(q1["rate_limits"]["credits"]["balance"])
        out["quota"] = {"plan_type": q1["rate_limits"].get("plan_type"),
                        "window_minutes": q1["rate_limits"]["primary"]["window_minutes"],
                        "used_percent_first": q0["rate_limits"]["primary"]["used_percent"],
                        "used_percent_last": q1["rate_limits"]["primary"]["used_percent"],
                        "used_percent_delta": du, "credits_balance_first": b0, "credits_balance_last": b1,
                        "credits_delta": round(b1 - b0, 4), "probe_labels": [x.get("label") for x in q],
                        "primary_resets_at_utc": q1.get("primary_resets_at_utc"),
                        "note": "used_percent is an integer-granularity weekly-window figure; deltas below 1 pt are invisible"}
        tok_between = sum(c["tokens"].get("total") or 0 for c in calls
                          if q0["ts"] <= c["ts"] <= q1["ts"])
        out["quota"]["tokens_between_probes"] = tok_between
        if du < 1:
            # displayed value unchanged -> true delta < 1 pt if rounded, < 1 pt if floored as well
            out["quota"]["implied_weekly_window_tokens_lower_bound"] = round(tok_between * 100 / 1.0)
            out["quota"]["implied_note"] = ("display unchanged: true use < ~1 pt (allowing for rounding, < 2 pt) "
                                            "for these tokens -> weekly window >= tokens*100 (>= tokens*50 worst case)")
    return out


# ---------------------------------------------------------------- smoke ladder
def smoke_ladder(gold14: dict) -> dict:
    gi = {g["idx"]: g for g in gold14["labels"]}
    out = {}
    labs = {}
    for b in ("smoke_b4", "smoke_b8"):
        d = CX / b
        if not (d / "labels.jsonl").exists():
            continue
        rows = rj(d / "labels.jsonl")
        recs, _ = v14.map_preds_v14(codex_records(rows)[0])
        labs[b] = {r["idx"]: r["labels"] for r in recs}
        ok = sum(ev.norm(r["labels"][f]) in ev.accepted_set(gi[r["idx"]], f) for r in recs for f in FIELDS)
        scope_err = sum(ev.norm(r["labels"]["in_scope"]) not in ev.accepted_set(gi[r["idx"]], "in_scope") for r in recs)
        cs = summarize(rj(d / "calls.jsonl"))
        out[b] = {"images": len(rows), "valid_rows": sum(r["status"] == "ok" for r in rows),
                  "pooled_vs_gold": round(ok / (len(recs) * len(FIELDS)), 4), "scope_errors": scope_err,
                  "prompt_sha256": rows[0]["prompt_sha256"], "engine_version": rows[0]["engine_version"], **cs}
    if len(labs) == 2:
        a, b = labs["smoke_b4"], labs["smoke_b8"]
        out["b4_vs_b8_identical_fields"] = f"{sum(a[i][f] == b[i][f] for i in a for f in FIELDS)}/{len(a) * len(FIELDS)}"
    return out


def summarize(calls: list[dict]) -> dict:
    tot = sum(c["tokens"]["total"] or 0 for c in calls)
    n = sum(c["n_images"] for c in calls)
    return {"calls": len(calls), "retry_calls": sum(c["attempt"] > 1 for c in calls), "tokens_total": tot,
            "tokens_per_image": round(tot / n) if n else None,
            "mean_seconds_per_call": round(sum(c["seconds"] for c in calls) / len(calls), 2) if calls else None}


# ---------------------------------------------------------------- report
SHORT = v14.SHORT


def render(rep: dict) -> str:
    L = [f"# WP3 N10 remote half: codex compact labeler vs gold (`{EVAL_VERSION}`, contract v1.4)\n"]
    L.append(rep["summary_line"] + "\n")
    m = rep["meta"]
    L.append(f"- engine `{m['engine']}` {m['engine_version']}, model `{m['model']}` effort `{m['effort']}`, "
             f"codex `{m['codex_cli']}`, label batch {m['batch_size']}, images max side 768 JPEG q80 (cover 512)")
    L.append(f"- prompt sha label `{m['prompt_label_sha'][:12]}` / cover `{m['prompt_cover_sha'][:12]}`; "
             f"contract sha `{m['contract_sha'][:12]}`; gold sha `{m['gold_sha'][:12]}`")
    L.append("- codex saw only opaque downscaled image files (no gold, no local labels, no file names, no building names)")
    L.append("- scoring: unchanged v1.4 mapping + scorer (`tools/vnext_wp3_eval_v14.py` → `tools/vnext_wp3_eval.py`); "
             "failed rows count as wrong on every field")
    L.append(f"- status counts: {m['status_counts']}; label-stage retry rate {m['retry_rate']}\n")

    L.append("## 1. Lane A: per-field acceptance (v1.4)\n")
    L.append("| engine | n | " + " | ".join(SHORT[f] for f in FIELDS) + " | **pooled** | scope err | medium err | lane A |")
    L.append("|---|---:|" + "---:|" * len(FIELDS) + "---:|---:|---:|---|")
    for name, r in rep["engines"].items():
        L.append(f"| {name} | {r['n_images']} | " + " | ".join(pct(r['per_field'][f]) for f in FIELDS)
                 + f" | **{pct(r['pooled'])}** | {r['scope_obvious_errors']} | {r['medium_obvious_errors']} | "
                 f"{'PASS' if r['lane_a_pass'] else 'FAIL'} |")
    L.append("")
    L.append("`codex (215, excl. smoke)` drops the 10 smoke images whose gold errors were seen before the single "
             "in_scope prompt revision (0.1.0 → 0.2.0); it is the leakage-free number.\n")
    c = rep["engines"]["codex_v1"]
    L.append("Codex obvious errors (all listed):\n")
    L.append("| idx | bld | field | gold (acceptable) | codex | codex reject_reason |")
    L.append("|---:|---|---|---|---|---|")
    for o in rep["codex_obvious_errors"]:
        L.append(f"| {o['idx']} | {o['canonical_bld_id']} | {o['field']} | {', '.join(o['gold_acceptable'])} | "
                 f"{o['local']} | {o.get('local_reject_reason')} |")
    L.append("")
    L.append(f"- scope error kinds {c['scope_error_kinds']}; medium error kinds {c['medium_error_kinds']}")
    L.append("- top non-scope errors (gold→codex): " + "; ".join(
        f"{f}: " + ", ".join(f"{e['gold']}→{e['local']} ×{e['n']}" for e in rep["codex_top_errors"][f][:3])
        for f in FIELDS if rep["codex_top_errors"][f]) + "\n")

    L.append("## 2. Lane B: default cover\n")
    L.append("| method | top-1 in acceptable | top-3 contains acceptable | critical | lane B |")
    L.append("|---|---|---|---:|---|")
    for name, r in rep["covers"].items():
        L.append(f"| {name} | {r['top1']} | {r['top3']} | {r['critical']} | {'PASS' if r['pass'] else 'FAIL'} |")
    L.append("")
    L.append("| building | gold acceptable | codex comparative (ranked) | codex label-derived default | local combo default |")
    L.append("|---|---|---|---|---|")
    for row in rep["cover_rows"]:
        L.append(f"| {row['bld']} | {row['gold_acceptable']} | {row['codex_cmp_ranked']} {row['codex_cmp_mark']} | "
                 f"{row['codex_lbl_default']} {row['codex_lbl_mark']} | {row['local_default']} {row['local_mark']} |")
    L.append("")

    ag = rep["agreement"]
    for ename, a in ag.items():
        L.append(f"## 3. Local ({ename}) vs codex agreement as an uncertainty signal\n")
        L.append("| field | disagree % | local acc on agree | local acc on disagree | codex acc on disagree | "
                 "local errors | caught by disagreement | both wrong (agree) |")
        L.append("|---|---:|---:|---:|---:|---:|---:|---:|")
        for f in FIELDS:
            p = a["per_field"][f]
            L.append(f"| {f} | {pct(p['disagree_pct'])} | {pct(p['local_acc_on_agree'])} | {pct(p['local_acc_on_disagree'])} | "
                     f"{pct(p['codex_acc_on_disagree'])} | {p['local_errors_total']} | {p['local_errors_caught_by_disagreement']} | "
                     f"{p['both_wrong_on_agree']} |")
        L.append("")
        L.append("| image-level rule | routed | local pooled on agree | local pooled on disagree | codex pooled on disagree |")
        L.append("|---|---:|---:|---:|---:|")
        for k, v in a["image_level"].items():
            L.append(f"| disagree on {k} | {v['disagree_images']} ({pct(v['routed_pct'])}) | {pct(v['local_pooled_on_agree'])} | "
                     f"{pct(v['local_pooled_on_disagree'])} | {pct(v['codex_pooled_on_disagree'])} |")
        L.append("")
        L.append("| funnel (final = local unless routed → codex) | routed | pooled | scope err | medium err |")
        L.append("|---|---:|---:|---:|---:|")
        for k, v in a["funnel"].items():
            L.append(f"| {k} | {pct(v['routed_pct'])} | {pct(v['pooled'])} | {v['scope_errors']} | {v['medium_errors']} |")
        L.append("")

    sl = rep.get("smoke_ladder") or {}
    if sl:
        L.append("## 0. Smoke ladder (N10: one image per building, 5 Divisare + 5 Architizer)\n")
        L.append("| run | engine / prompt | images | valid rows | calls | retries | tokens | tokens/image | s/call | pooled vs gold | scope err |")
        L.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for k in ("smoke_b4", "smoke_b8"):
            if k in sl:
                v = sl[k]
                L.append(f"| {k} | {v['engine_version']} / `{v['prompt_sha256'][:8]}` | {v['images']} | {v['valid_rows']} | {v['calls']} | "
                         f"{v['retry_calls']} | {v['tokens_total']:,} | {v['tokens_per_image']} | {v['mean_seconds_per_call']} | "
                         f"{pct(v['pooled_vs_gold'])} | {v['scope_errors']} |")
        L.append("")
        L.append(f"- batch 4 vs 8 outputs identical on {sl.get('b4_vs_b8_identical_fields')} fields -> batch 8 chosen "
                 "(fewer tokens/image; a full batch of 8 in the 225 run = 1,233 tokens/image).")
        L.append("- both smoke runs rejected the same 2 in-scope interiors (people / furniture in the space). The in_scope "
                 "wording was clarified once (0.1.0 -> 0.2.0, a contract-level definition, no per-image tuning) and then "
                 "frozen for the 225 run. This is mild leakage on those 10 images only; the 215-image row in section 1 "
                 "excludes them.\n")

    x = rep["extrapolation"]
    L.append("## 4. Tokens, time, extrapolation\n")
    L.append(f"- label calls: fit total tokens = {x['label_fit_tokens']['overhead_per_call']} + "
             f"{x['label_fit_tokens']['per_image_768']}·n (768 px, {x['label_fit_tokens']['calls']} calls); "
             f"seconds = {x['label_fit_seconds']['per_call']} + {x['label_fit_seconds']['per_image']}·n")
    b8 = x["label_batch8_measured"]
    L.append(f"- batch-8 calls measured: {b8['calls']} calls, mean {b8['mean_total_tokens_per_call']} total tokens/call "
             f"(uncached {b8['mean_uncached_tokens_per_call']}), **{b8['total_tokens_per_image']} tokens/image**, "
             f"{b8['mean_seconds_per_call']} s/call")
    t = x["label_observed_throughput"]
    L.append(f"- observed throughput: {t['images_sent']} images in {t['wall_s']} s with {t['workers']} workers = "
             f"{t['images_per_hour']} images/h")
    if "cover_fit_tokens" in x:
        L.append(f"- cover calls: fit = {x['cover_fit_tokens']['overhead_per_call']} + {x['cover_fit_tokens']['per_image_512']}·n "
                 f"(512 px); full-gallery cover call mean {x['cover_top3_check_39248']['full_gallery_cover_call_mean_tokens']} tokens")
        c3 = x["cover_top3_check_39248"]
        L.append(f"- **cover top-3 check, 39,248 buildings** (1 call/building, 3 candidates): "
                 f"{c3['tokens_per_building_512']} tokens/bld at 512 px → {c3['total_tokens_39248_512'] / 1e6:.1f}M; "
                 f"{c3['tokens_per_building_768']} at 768 px → {c3['total_tokens_39248_768'] / 1e6:.1f}M; "
                 f"wall {c3['wall_h_sequential']} h sequential / {c3['wall_h_3_workers']} h at 3 workers")
    L.append("")
    L.append("| route X% of 699,038 S6 images (batch 8) | images | calls | total tokens | wall h sequential | wall h at observed 3-worker rate |")
    L.append("|---:|---:|---:|---:|---:|---:|")
    for r in x["routing_699038"]:
        L.append(f"| {r['route_pct']}% | {r['images']:,} | {r['calls_batch8']:,} | {r['total_tokens'] / 1e6:.1f}M | "
                 f"{r['wall_h_sequential']} | {r['wall_h_observed_throughput']} |")
    L.append("")
    L.append(f"- 8.0M pool cap ≈ {x['pool_cap_8M_images_at_batch8']:,} images at batch 8")
    if "quota" in x:
        q = x["quota"]
        L.append(f"- quota (codex rate_limits from rollout of a tiny non-ephemeral probe): plan `{q['plan_type']}`, "
                 f"window {q['window_minutes']} min, used {q['used_percent_first']}% → {q['used_percent_last']}% "
                 f"(Δ {q['used_percent_delta']} pt), credits balance {q['credits_balance_first']} → {q['credits_balance_last']} "
                 f"(Δ {q['credits_delta']}); resets {q['primary_resets_at_utc']}. {q['note']}")
        if "implied_weekly_window_tokens_lower_bound" in q:
            L.append(f"- quota implication: {q['tokens_between_probes']:,} tokens between the probes left the displayed % unchanged -> "
                     f"the weekly window is roughly >= {q['implied_weekly_window_tokens_lower_bound'] / 1e6:.0f}M tokens "
                     f"(>= {q['implied_weekly_window_tokens_lower_bound'] / 2e6:.0f}M worst case with rounding). Coarse; "
                     "a measurable delta needs a run of several million tokens.")
    L.append("- caveats: 10 buildings / 1 reviewer + judge gold; codex model/effort fixed (gpt-5.5 unavailable on the "
             "ChatGPT-account CLI → `gpt-6.1-sol` low, default tier); population extrapolation assumes the N10 image mix; "
             "tokens = input (incl. cached) + output as reported by `codex exec --json`")
    L.append("")
    L.append("## 5. Inputs\n")
    for k, v in rep["inputs"].items():
        L.append(f"- `{k}` `{v}`")
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-write", action="store_true")
    a = ap.parse_args(argv)

    gold = json.loads((WP / "gold_labels_final_v2.json").read_text(encoding="utf-8"))
    gold14, _ = v14.map_gold_v14(gold)
    man = json.loads((WP / "sample_manifest.json").read_text(encoding="utf-8"))
    names = {b["canonical_bld_id"]: (b["name"], b["stratum"]) for b in man["buildings"]}
    sort_order = {im["idx"]: im["sort_order"] for b in man["buildings"] for im in b["images"]}
    rows = rj(CX / "labels.jsonl")
    codex, st = codex_records(rows)
    codex14, _ = v14.map_preds_v14(codex)
    cover_rows = [r for r in rj(CX / "cover.jsonl") if r.get("complete")] if (CX / "cover.jsonl").exists() else []
    run_cmp = cover_run_doc(cover_rows, names)
    run_lbl = label_derived_cover(codex14, names, sort_order)
    smoke = {x["idx"] for x in json.loads((CX / "smoke_idx.json").read_text(encoding="utf-8"))["images"]}
    run_json = json.loads((CX / "run.json").read_text(encoding="utf-8"))

    engines = {}
    engines["codex_v1"] = v14.score(codex14, gold14, run_cmp)
    keep = {g["idx"] for g in gold14["labels"]} - smoke
    engines["codex_v1 (215, excl. smoke)"] = v14.score([r for r in codex14 if r["idx"] in keep],
                                                        subset_gold(gold14, keep), run_cmp)
    local14 = {}
    for name, rel in LOCAL_ENGINES:
        recs, _ = v14.map_preds_v14(rj(WP / rel))
        local14[name] = recs
        engines[f"local {name}"] = v14.score(recs, gold14, v14.run_doc_from_tiers(recs, names))

    # full evaluate for codex error listing
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        full = ev.evaluate(codex14, gold14, run_cmp)
        full_lbl = ev.evaluate(codex14, gold14, run_lbl)
        loc_full = ev.evaluate(local14[LOCAL_ENGINES[0][0]], gold14,
                               v14.run_doc_from_tiers(local14[LOCAL_ENGINES[0][0]], names))
    gi = {g["idx"]: g for g in gold14["labels"]}
    obvious = []
    for i in sorted(gi):
        r = next(x for x in codex14 if x["idx"] == i)
        for f in ("in_scope", "medium"):
            if not ev.is_accepted(r["labels"], gi[i], f):
                obvious.append({"idx": i, "canonical_bld_id": gi[i]["canonical_bld_id"], "field": f,
                                "gold_acceptable": sorted(ev.accepted_set(gi[i], f)),
                                "local": ev.norm(r["labels"][f]), "local_reject_reason": r["labels"].get("reject_reason")})
    covers = {}
    for name, e in (("codex comparative call", full), ("codex label-derived (deterministic)", full_lbl),
                    ("local combo (exp_v2 tiers)", loc_full)):
        g = e["lane_b"]["gate"]
        covers[name] = {"top1": g["top1_in_acceptable"], "top3": g["top3_contains_acceptable"],
                        "critical": g["critical_cover_errors"], "pass": g["pass"],
                        "rows": e["lane_b"]["buildings"]}

    def mark(row):
        if row is None:
            return ""
        return ("ok" if row["top1_ok"] else "MISS") + (" CRIT" if row.get("critical") else "")

    cmp_by = {r["canonical_bld_id"]: r for r in covers["codex comparative call"]["rows"]}
    lbl_by = {r["canonical_bld_id"]: r for r in covers["codex label-derived (deterministic)"]["rows"]}
    loc_by = {r["canonical_bld_id"]: r for r in covers["local combo (exp_v2 tiers)"]["rows"]}
    cover_rows_md = []
    for bld in sorted(names):
        cr = cmp_by.get(bld)
        cover_rows_md.append({"bld": bld, "gold_acceptable": (cr or {}).get("acceptable_default_idx"),
                              "codex_cmp_ranked": run_cmp["buildings"][bld]["top3_idx"], "codex_cmp_mark": mark(cr),
                              "codex_lbl_default": run_lbl["buildings"][bld]["default_cover_idx"],
                              "codex_lbl_mark": mark(lbl_by.get(bld)),
                              "local_default": (loc_by.get(bld) or {}).get("local_default_idx"),
                              "local_mark": mark(loc_by.get(bld))})
    for v in covers.values():
        v.pop("rows")

    agree = {name: agreement(recs, codex14, gold14) for name, recs in local14.items()}
    calls = rj(CX / "calls.jsonl")
    quota = rj(CX / "quota_probes.jsonl") if (CX / "quota_probes.jsonl").exists() else []
    extr = extrapolate(calls, run_json.get("label"), run_json.get("cover"), quota)

    c = engines["codex_v1"]
    cc = covers["codex comparative call"]
    summary = (f"WP3 codex_v1: pooled={pct(c['pooled'])} (excl. smoke {pct(engines['codex_v1 (215, excl. smoke)']['pooled'])}) "
               f"scope_err={c['scope_obvious_errors']} medium_err={c['medium_obvious_errors']} "
               f"lane_A={'PASS' if c['lane_a_pass'] else 'FAIL'}; cover top1={cc['top1']} top3={cc['top3']} "
               f"crit={cc['critical']} lane_B={'PASS' if cc['pass'] else 'FAIL'}; "
               f"{extr['label_batch8_measured']['total_tokens_per_image']} tokens/image at batch 8")
    led = json.loads((CX / "ledger_eval225.json").read_text(encoding="utf-8"))
    rep = {
        "eval_version": EVAL_VERSION, "summary_line": summary,
        "meta": {"engine": rows[0]["engine"], "engine_version": rows[0]["engine_version"], "model": rows[0]["model"],
                 "effort": rows[0]["reasoning_effort"], "codex_cli": run_json.get("codex_cli"),
                 "batch_size": run_json["label"]["batch_size"], "prompt_label_sha": rows[0]["prompt_sha256"],
                 "prompt_cover_sha": run_json["frozen_sha256"]["prompt_cover"], "contract_sha": run_json["contract_sha256"],
                 "gold_sha": sha(WP / "gold_labels_final_v2.json"), "status_counts": dict(st),
                 "retry_rate": run_json["label"]["calls"]["retry_rate"],
                 "ledger_total_tokens": led["total_tokens"], "ledger_cap": led["cap_total_tokens"]},
        "engines": engines, "codex_obvious_errors": obvious,
        "codex_top_errors": {f: full["per_field"][f]["top_errors_gold_to_local"] for f in FIELDS},
        "codex_confusion": {f: full["confusion"][f]["rows_gold_primary_cols_local"] for f in FIELDS},
        "covers": covers, "cover_rows": cover_rows_md, "agreement": agree, "extrapolation": extr,
        "smoke_ladder": smoke_ladder(gold14),
        "inputs": {"gold_labels_final_v2.json": sha(WP / "gold_labels_final_v2.json"),
                   "codex_v1/labels.jsonl": sha(CX / "labels.jsonl"),
                   "codex_v1/cover.jsonl": sha(CX / "cover.jsonl") if (CX / "cover.jsonl").exists() else None,
                   "codex_v1/calls.jsonl": sha(CX / "calls.jsonl"),
                   **{rel: sha(WP / rel) for _, rel in LOCAL_ENGINES},
                   "tools/vnext_wp3_codex_eval_v1.py": sha(Path(__file__)),
                   "tools/vnext_wp3_eval_v14.py": sha(ROOT / "tools" / "vnext_wp3_eval_v14.py"),
                   "tools/vnext_wp3_eval.py": sha(ROOT / "tools" / "vnext_wp3_eval.py")},
        "paid_calls_in_eval": 0, "network_requests_in_eval": 0,
    }
    md = render(rep)
    if not a.no_write:
        (CX / "eval.json").write_text(json.dumps(rep, indent=1, ensure_ascii=False), encoding="utf-8")
        (CX / "eval.md").write_text(md, encoding="utf-8")
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
