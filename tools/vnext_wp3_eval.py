#!/usr/bin/env python3
"""vNext WP3 N10 local-engine evaluation (offline, $0).

Scores the local S3 engine (`tools/vnext_s3_local_engine.py`, config v1) against the
judged N10 gold (`gold_labels_final.json`) and writes eval_report.{json,md}.

- per-field acceptance (gold primary OR any judged acceptable alternative = correct)
- confusion matrices (rows = gold primary, cols = local value)
- plan §3 lane A N10 gate: scope/medium obvious errors == 0 AND pooled 9-field acceptance >= 90%
- plan §3 lane B N10 gate: critical cover errors == 0 AND local default in acceptable set 10/10
  (top-3-contains-acceptable reported alongside)
- confidence-proxy routing: route the lowest-margin X% to remote; accuracy of the
  auto-accepted rest; minimal X reaching >= 95% (per field, and image level)
- full-run extrapolation (GPU, fetch, disk, remote escalation volume)

Inputs are opened read-only; nothing outside the WP output dir is written.
No network, no Neon/R2, no paid model calls.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WP_DIR = ROOT / "data" / "smoke_vnext" / "wp3_n10_v1"
EVAL_VERSION = "vnext-wp3-n10-eval-v1"

FIELDS = [
    "in_scope",
    "reject_reason",
    "medium",
    "spatial_context",
    "framing_scale",
    "camera_angle",
    "drawing_kind",
    "project_state",
    "project_legibility",
]
# score block in local_labels.scores that drives each field
SCORE_KEY = {
    "in_scope": "scope",
    "reject_reason": "scope",
    "medium": "medium",
    "spatial_context": "spatial_context",
    "framing_scale": "framing_scale",
    "camera_angle": "camera_angle",
    "drawing_kind": "drawing_kind",
    "project_state": "project_state",
    "project_legibility": "project_legibility",
}
LANE_A_ACCEPT_MIN = 0.90
ROUTE_TARGET = 0.95
MIN_AUTO_KEEP = 10  # never declare a threshold that leaves fewer auto-accepted items
CHEAP_FIELD_MAX_ROUTED_PCT = 50.0  # field is 'local-viable' if <=50% must be escalated to reach 95%

# population constants (job cards 20261023_vnext_s6_lite_building_images / 20261020_vnext_wp4_s2_full_run)
POP_S6_IMAGES = 699_038
POP_S6_BUILDINGS = 39_248
POP_S2_GROUPS = 1_416_641
FETCH_SEQ_S_PER_REQ = 1.3
FETCH_RPS_PLAN = (4, 6)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def norm(v):
    """Compare booleans and strings uniformly."""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def accepted_set(gold: dict, field: str) -> set[str]:
    vals = {norm(gold[field])}
    for v in (gold.get("acceptable") or {}).get(field, []) or []:
        vals.add(norm(v))
    return vals


def is_accepted(local_labels: dict, gold: dict, field: str) -> bool:
    return norm(local_labels[field]) in accepted_set(gold, field)


# ---------------------------------------------------------------- confidence proxy
def _margin(scores: dict, key: str):
    blk = scores.get(key)
    if not blk or blk.get("margin") is None:
        return None
    return float(blk["margin"])


def field_confidence(rec: dict, field: str, mode: str = "own") -> float:
    """Margin confidence proxy for one field.

    own:     the field's own score margin; a field the engine did not score (forced
             not_applicable by scope/medium applicability) inherits min(scope, medium).
    chained: min(own, scope, medium) for downstream fields (their correctness depends
             on the upstream gates), own for in_scope/reject_reason/medium.
    """
    sc = rec.get("scores") or {}
    scope_m = _margin(sc, "scope")
    medium_m = _margin(sc, "medium")
    upstream = [m for m in (scope_m, medium_m) if m is not None]
    own = _margin(sc, SCORE_KEY[field])
    if field in ("in_scope", "reject_reason"):
        return scope_m if scope_m is not None else 0.0
    if field == "medium":
        if mode == "chained":
            return min(m for m in (medium_m, scope_m) if m is not None) if upstream else 0.0
        return medium_m if medium_m is not None else 0.0
    if own is None:
        return min(upstream) if upstream else 0.0
    if mode == "chained":
        return min([own] + upstream)
    return own


def image_confidence(rec: dict) -> float:
    sc = rec.get("scores") or {}
    ms = [float(b["margin"]) for b in sc.values() if isinstance(b, dict) and b.get("margin") is not None]
    return min(ms) if ms else 0.0


def auroc(conf: list[float], correct: list[bool]):
    """P(conf of a correct item > conf of a wrong item); ties count 0.5."""
    pos = [c for c, ok in zip(conf, correct) if ok]
    neg = [c for c, ok in zip(conf, correct) if not ok]
    if not pos or not neg:
        return None
    # rank-sum (average ranks for ties)
    allv = sorted((c, i) for i, c in enumerate(conf))
    ranks = [0.0] * len(conf)
    i = 0
    while i < len(allv):
        j = i
        while j + 1 < len(allv) and allv[j + 1][0] == allv[i][0]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[allv[k][1]] = avg
        i = j + 1
    rpos = sum(r for r, ok in zip(ranks, correct) if ok)
    n1, n0 = len(pos), len(neg)
    return round((rpos - n1 * (n1 + 1) / 2) / (n1 * n0), 4)


def routing_curve(items: list[tuple[float, bool, int]], target: float = ROUTE_TARGET,
                  min_keep: int = MIN_AUTO_KEEP) -> dict:
    """items = (confidence, correct, idx). Route the lowest-confidence k to remote.

    Returns the minimal routed fraction whose auto-accepted remainder reaches `target`,
    the margin threshold (auto-accept iff conf >= threshold), and a decile curve.
    Ties are broken by idx so the result is deterministic; the threshold reported is the
    confidence of the first auto-accepted item.
    """
    n = len(items)
    srt = sorted(items, key=lambda t: (t[0], t[2]))
    suffix_ok = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        suffix_ok[i] = suffix_ok[i + 1] + (1 if srt[i][1] else 0)

    def acc_after(k):
        rem = n - k
        return suffix_ok[k] / rem if rem else None

    found = None
    for k in range(0, n - min_keep + 1):
        a = acc_after(k)
        if a is not None and a >= target:
            found = k
            break
    curve = []
    for pct in (0, 10, 20, 30, 40, 50, 60, 70, 80, 90):
        k = math.floor(n * pct / 100)
        a = acc_after(k)
        curve.append({"routed_pct": pct, "routed_n": k, "auto_n": n - k,
                      "auto_accuracy": round(a, 4) if a is not None else None,
                      "threshold": round(srt[k][0], 4) if k < n else None})
    out = {
        "n": n,
        "baseline_accuracy": round(suffix_ok[0] / n, 4) if n else None,
        "auroc_margin_vs_correct": auroc([t[0] for t in items], [t[1] for t in items]),
        "target": target,
        "min_auto_keep": min_keep,
        "reached": found is not None,
        "curve": curve,
    }
    if found is not None:
        routed = srt[:found]
        out.update({
            "routed_n": found,
            "routed_pct": round(100 * found / n, 1),
            "threshold": round(srt[found][0], 4),
            "auto_n": n - found,
            "auto_accuracy": round(acc_after(found), 4),
            "routed_idx": sorted(t[2] for t in routed),
            "routed_that_were_already_correct": sum(1 for t in routed if t[1]),
        })
    return out


# ---------------------------------------------------------------- lane B (covers)
def critical_cover_reasons(gold_lbl: dict) -> list[str]:
    """Contract COVERS §3: drawing/detail/interior/non-architectural default = critical."""
    r = []
    if not gold_lbl.get("in_scope", True):
        r.append("out_of_scope")
    if gold_lbl.get("medium") != "photograph":
        r.append(f"medium={gold_lbl.get('medium')}")
    if gold_lbl.get("spatial_context") == "interior":
        r.append("interior")
    if gold_lbl.get("framing_scale") in ("element_detail", "material_detail"):
        r.append(f"framing={gold_lbl.get('framing_scale')}")
    return r


def eval_covers(gold: dict, local_run: dict, gold_by_idx: dict) -> dict:
    gold_covers = {c["canonical_bld_id"]: c for c in gold.get("covers", [])}
    rows = []
    for bld, b in local_run["buildings"].items():
        lc = b.get("default_cover_idx")
        top3 = list(b.get("top3_idx") or [])
        g = gold_covers.get(bld)
        row = {"canonical_bld_id": bld, "name": b.get("name"), "stratum": b.get("stratum"),
               "local_default_idx": lc, "local_top3_idx": top3,
               "local_review_required": b.get("review_required")}
        if g is None:
            row.update({"gold": False, "top1_ok": None, "top3_ok": None, "critical": None,
                        "note": "no gold (chunk_4 missing) - local pick unverified"})
            rows.append(row)
            continue
        acc = list(g.get("acceptable_default_idx") or [])
        if not acc:  # gold: NULL / review_required is the correct answer
            top1_ok = lc is None
            top3_ok = lc is None
        else:
            top1_ok = lc in acc
            top3_ok = any(i in acc for i in top3)
        crit = []
        if lc is not None:
            gl = gold_by_idx.get(lc)
            crit = critical_cover_reasons(gl) if gl else ["default_without_gold_label"]
        row.update({"gold": True, "acceptable_default_idx": acc, "top1_ok": top1_ok,
                    "top3_ok": top3_ok, "critical": bool(crit), "critical_reasons": crit,
                    "miss_kind": (None if top1_ok else
                                  ("null_instead_of_valid_default" if lc is None else
                                   "non_null_when_gold_null" if not acc else "default_not_in_set")),
                    "judge_note": g.get("judge_note")})
        rows.append(row)
    scored = [r for r in rows if r["gold"]]
    n_bld = len(rows)
    top1 = sum(1 for r in scored if r["top1_ok"])
    top3 = sum(1 for r in scored if r["top3_ok"])
    crit = sum(1 for r in scored if r["critical"])
    gate = {
        "critical_cover_errors": crit,
        "buildings_total": n_bld,
        "buildings_with_gold": len(scored),
        "top1_in_acceptable": f"{top1}/{n_bld}",
        "top3_contains_acceptable": f"{top3}/{n_bld}",
        "top1_in_acceptable_of_scored": f"{top1}/{len(scored)}",
        "top3_contains_acceptable_of_scored": f"{top3}/{len(scored)}",
        "criteria": "critical == 0 AND local top-ranked default acceptable 10/10 (top-3 reported)",
    }
    passed = crit == 0 and top1 == n_bld and len(scored) == n_bld
    gate["pass"] = passed
    reasons = []
    if crit:
        reasons.append(f"{crit} critical cover error(s)")
    if len(scored) < n_bld:
        reasons.append(f"{n_bld - len(scored)} building(s) have no gold -> 10/10 not demonstrable")
    if top1 < len(scored):
        reasons.append(f"{len(scored) - top1} scored building(s) miss the acceptable set")
    gate["fail_reasons"] = reasons
    return {"gate": gate, "buildings": rows}


# ---------------------------------------------------------------- main eval
def evaluate(local_recs: list[dict], gold: dict, local_run: dict | None = None) -> dict:
    local = {r["idx"]: r for r in local_recs}
    gold_labels = gold["labels"]
    gold_by_idx = {g["idx"]: g for g in gold_labels}
    scored_idx = sorted(i for i in gold_by_idx if i in local)

    per_field = {}
    confusion = {}
    pooled_ok = pooled_n = 0
    for f in FIELDS:
        ok = alt = 0
        conf = defaultdict(Counter)
        per_stratum = defaultdict(lambda: [0, 0])
        for i in scored_idx:
            g, l = gold_by_idx[i], local[i]["labels"]
            a = is_accepted(l, g, f)
            ok += a
            if a and norm(l[f]) != norm(g[f]):
                alt += 1
            conf[norm(g[f])][norm(l[f])] += 1
            s = local[i].get("stratum", "?")
            per_stratum[s][0] += a
            per_stratum[s][1] += 1
        n = len(scored_idx)
        per_field[f] = {"n": n, "accepted": ok, "acceptance": round(ok / n, 4) if n else None,
                        "accepted_via_alternative": alt,
                        "exact_primary_match": ok - alt,
                        "by_stratum": {s: {"accepted": v[0], "n": v[1],
                                           "acceptance": round(v[0] / v[1], 4)}
                                       for s, v in sorted(per_stratum.items())}}
        confusion[f] = {"rows_gold_primary_cols_local": {gk: dict(sorted(c.items()))
                                                          for gk, c in sorted(conf.items())}}
        pooled_ok += ok
        pooled_n += n

    # top error pairs per field
    for f in FIELDS:
        pairs = Counter()
        for i in scored_idx:
            g, l = gold_by_idx[i], local[i]["labels"]
            if not is_accepted(l, g, f):
                pairs[(norm(g[f]), norm(l[f]))] += 1
        per_field[f]["top_errors_gold_to_local"] = [
            {"gold": a, "local": b, "n": c} for (a, b), c in pairs.most_common(6)]

    # cascade view: downstream fields on images whose scope AND medium were accepted
    upstream_ok = [i for i in scored_idx
                   if is_accepted(local[i]["labels"], gold_by_idx[i], "in_scope")
                   and is_accepted(local[i]["labels"], gold_by_idx[i], "medium")]
    downstream = {}
    for f in FIELDS[3:]:
        a = sum(is_accepted(local[i]["labels"], gold_by_idx[i], f) for i in upstream_ok)
        downstream[f] = {"accepted": a, "n": len(upstream_ok),
                         "acceptance": round(a / len(upstream_ok), 4) if upstream_ok else None}

    # lane A obvious errors (scope / medium); every one was adjudicated gold_correct by the judge
    obvious = []
    for i in scored_idx:
        g, l = gold_by_idx[i], local[i]["labels"]
        for f in ("in_scope", "medium"):
            if not is_accepted(l, g, f):
                obvious.append({"idx": i, "canonical_bld_id": g["canonical_bld_id"], "field": f,
                                "gold": norm(g[f]), "gold_acceptable": sorted(accepted_set(g, f)),
                                "local": norm(l[f]),
                                "local_reject_reason": l.get("reject_reason")})
    scope_errs = [o for o in obvious if o["field"] == "in_scope"]
    medium_errs = [o for o in obvious if o["field"] == "medium"]
    scope_err_kinds = Counter(
        ("rejected_in_scope_image:" + str(o["local_reject_reason"])) if o["gold"] == "true"
        else "accepted_out_of_scope_image" for o in scope_errs)
    medium_err_kinds = Counter(f"{o['gold']}->{o['local']}" for o in medium_errs)
    pooled = round(pooled_ok / pooled_n, 4) if pooled_n else None
    min_field = min(FIELDS, key=lambda f: per_field[f]["acceptance"])
    lane_a = {
        "scope_obvious_errors": len(scope_errs),
        "medium_obvious_errors": len(medium_errs),
        "scope_error_kinds": dict(scope_err_kinds.most_common()),
        "medium_error_kinds": dict(medium_err_kinds.most_common()),
        "pooled_9field_acceptance": pooled,
        "pooled_accepted": pooled_ok,
        "pooled_n": pooled_n,
        "fields_at_or_above_90pct": [f for f in FIELDS if per_field[f]["acceptance"] >= LANE_A_ACCEPT_MIN],
        "weakest_field": {"field": min_field, "acceptance": per_field[min_field]["acceptance"]},
        "criteria": "scope/medium obvious errors == 0 AND pooled 9-field acceptance >= 0.90",
        "obvious_error_definition": ("local in_scope/medium value outside the judged gold acceptable "
                                     "set; every such case was re-inspected by the judge "
                                     "(decision gold_correct), so none is a borderline call"),
    }
    lane_a["pass"] = (len(scope_errs) == 0 and len(medium_errs) == 0
                      and pooled is not None and pooled >= LANE_A_ACCEPT_MIN)
    fr = []
    if scope_errs:
        fr.append(f"{len(scope_errs)} scope error(s)")
    if medium_errs:
        fr.append(f"{len(medium_errs)} medium error(s)")
    if pooled is not None and pooled < LANE_A_ACCEPT_MIN:
        fr.append(f"pooled acceptance {pooled:.1%} < 90%")
    lane_a["fail_reasons"] = fr

    # confidence routing
    routing = {"own": {}, "chained": {}}
    for mode in ("own", "chained"):
        for f in FIELDS:
            items = [(field_confidence(local[i], f, mode), is_accepted(local[i]["labels"], gold_by_idx[i], f), i)
                     for i in scored_idx]
            r = routing_curve(items)
            r.pop("routed_idx", None)
            routing[mode][f] = r
    # image level: route an image when its min margin is low; metric = pooled field acceptance
    img_items = []
    for i in scored_idx:
        g, l = gold_by_idx[i], local[i]["labels"]
        img_items.append((image_confidence(local[i]), sum(is_accepted(l, g, f) for f in FIELDS), i))
    img_items.sort(key=lambda t: (t[0], t[2]))
    n_img = len(img_items)
    tot = [0] * (n_img + 1)
    allok = [0] * (n_img + 1)
    for k in range(n_img - 1, -1, -1):
        tot[k] = tot[k + 1] + img_items[k][1]
        allok[k] = allok[k + 1] + (1 if img_items[k][1] == len(FIELDS) else 0)
    img_curve = []
    for pct in (0, 10, 20, 30, 40, 50, 60, 70, 80, 90):
        k = math.floor(n_img * pct / 100)
        rem = n_img - k
        img_curve.append({"routed_pct": pct, "auto_n": rem,
                          "auto_pooled_field_acceptance": round(tot[k] / (rem * len(FIELDS)), 4),
                          "auto_images_all9_correct": round(allok[k] / rem, 4),
                          "threshold_min_margin": round(img_items[k][0], 4)})
    img_x95 = None
    for k in range(0, n_img - MIN_AUTO_KEEP + 1):
        if tot[k] / ((n_img - k) * len(FIELDS)) >= ROUTE_TARGET:
            img_x95 = {"routed_n": k, "routed_pct": round(100 * k / n_img, 1),
                       "threshold_min_margin": round(img_items[k][0], 4),
                       "auto_pooled_field_acceptance": round(tot[k] / ((n_img - k) * len(FIELDS)), 4),
                       "auto_images_all9_correct": round(allok[k] / (n_img - k), 4)}
            break
    # union of per-field X95 routes (own mode): images escalated if any reachable field routes them
    union = set()
    union_cheap = set()
    cheap_fields = []
    unreachable = []
    for f in FIELDS:
        items = [(field_confidence(local[i], f, "own"), is_accepted(local[i]["labels"], gold_by_idx[i], f), i)
                 for i in scored_idx]
        r = routing_curve(items)
        if r["reached"]:
            union.update(r["routed_idx"])
            if r["routed_pct"] <= CHEAP_FIELD_MAX_ROUTED_PCT:
                union_cheap.update(r["routed_idx"])
                cheap_fields.append(f)
        else:
            unreachable.append(f)
    routing["image_level"] = {
        "confidence": "min margin over all score blocks of the image",
        "baseline_pooled_acceptance": pooled,
        "baseline_images_all9_correct": round(allok[0] / n_img, 4) if n_img else None,
        "curve": img_curve,
        "x_for_pooled_95": img_x95,
        "union_of_per_field_x95_routes": {
            "routed_images": len(union), "routed_pct": round(100 * len(union) / n_img, 1) if n_img else None,
            "fields_never_reaching_95": unreachable,
            "note": "fields that never reach 95% on the auto side cannot be local-only at any threshold",
        },
        "union_of_x95_routes_local_viable_fields": {
            "fields": cheap_fields,
            "rule": f"own-margin X95 <= {CHEAP_FIELD_MAX_ROUTED_PCT}% routed",
            "routed_images": len(union_cheap),
            "routed_pct": round(100 * len(union_cheap) / n_img, 1) if n_img else None,
        },
    }
    routing["caveat"] = ("in-sample thresholds on 173 images / 8 buildings: optimistic, not transferable "
                         "without N100 holdout; margins are on different scales per field (2-way vs 11-way softmax)")

    lane_b = eval_covers(gold, local_run, gold_by_idx) if local_run else None
    return {
        "scored_images": len(scored_idx),
        "scored_buildings": len({gold_by_idx[i]["canonical_bld_id"] for i in scored_idx}),
        "unscored_images": len(local) - len(scored_idx),
        "per_field": per_field,
        "downstream_given_scope_medium_ok": downstream,
        "confusion": confusion,
        "lane_a": lane_a,
        "lane_a_obvious_errors": obvious,
        "lane_b": lane_b,
        "routing": routing,
    }


# ---------------------------------------------------------------- extrapolation
def extrapolate(local_run: dict, fetch_log: list[dict], labels_bytes: int, n_labels: int,
                escalation_fracs: dict[str, float]) -> dict:
    ips = float(local_run["benchmark"]["images_per_sec_end_to_end"])
    ok = [r for r in fetch_log if r.get("status") == "ok"]
    avg_bytes = sum(int(r.get("bytes") or 0) for r in ok) / len(ok) if ok else 0
    row_bytes = labels_bytes / n_labels if n_labels else 0

    def pop(n):
        gpu_h = n / ips / 3600
        fetch = {"sequential_1.3s_h": round(n * FETCH_SEQ_S_PER_REQ / 3600, 1)}
        for rps in FETCH_RPS_PLAN:
            fetch[f"{rps}rps_h"] = round(n / rps / 3600, 1)
        wall = {f"{rps}rps_pipelined_wall_h": round(max(n / rps, n / ips) / 3600, 1) for rps in FETCH_RPS_PLAN}
        return {
            "images": n,
            "gpu_end_to_end_h": round(gpu_h, 2),
            "fetch": fetch,
            "pipelined": wall,
            "gpu_utilisation_at_plan_rps": {f"{rps}rps": round(rps / ips, 3) for rps in FETCH_RPS_PLAN},
            # sample mean bytes; Divisare-heavy (~698 KB vs Architizer ~155 KB per image)
            "network_transfer_gb": round(n * avg_bytes / 1e9, 1),
            "label_output_gb_full_scores": round(n * row_bytes / 1e9, 2),
        }

    esc = {}
    for name, frac in escalation_fracs.items():
        if frac is None:
            continue
        esc[name] = {"fraction": round(frac, 4),
                     "s6_images": int(round(POP_S6_IMAGES * frac)),
                     "s2_groups": int(round(POP_S2_GROUPS * frac))}
    return {
        "measured": {"images_per_sec_end_to_end": ips,
                     "images_per_sec_gpu_only": local_run["benchmark"]["images_per_sec_gpu_forward_only"],
                     "peak_vram_gb": local_run.get("peak_vram_allocated_gb"),
                     "avg_image_bytes_sample": round(avg_bytes),
                     "label_row_bytes_full_scores": round(row_bytes)},
        "s6_lite_images": pop(POP_S6_IMAGES),
        "s2_gallery_groups": pop(POP_S2_GROUPS),
        "disk_policy": ("stream-and-discard: pixels are fetched, SHA-verified, labelled and dropped; "
                        "resident disk = in-flight buffer only (~batch 32-256 x avg image ≈ "
                        f"{round(256 * avg_bytes / 1e6, 1)} MB at 256) + label output"),
        "remote_escalation": esc,
        "cover_candidates_remote": {
            "buildings": POP_S6_BUILDINGS,
            "top3_candidates": POP_S6_BUILDINGS * 3,
            "note": "contract §7: cover candidates are always remote-promoted, independent of label confidence",
        },
        "remote_cost": "not measured - WP3 remote half (compact Vision N10) has not run; $ per call TBD",
    }


# ---------------------------------------------------------------- report
def pct(x):
    return "-" if x is None else f"{100 * x:.1f}%"


def render_md(rep: dict) -> str:
    ev = rep["eval"]
    la, lb = ev["lane_a"], ev["lane_b"]["gate"]
    L = []
    L.append(f"# vNext WP3 N10 local-engine eval (`{EVAL_VERSION}`)\n")
    L.append(f"WP3 N10: lane A {'PASS' if la['pass'] else 'FAIL'}, lane B {'PASS' if lb['pass'] else 'FAIL'}\n")
    L.append(f"- engine: `{rep['inputs']['engine']}` config `{rep['inputs']['config_version']}` "
             f"(sha `{rep['inputs']['config_sha256'][:12]}`)")
    L.append(f"- scored: {ev['scored_images']} images / {ev['scored_buildings']} buildings "
             f"(unscored {ev['unscored_images']}: chunk_4 gold missing)")
    L.append("- acceptance = local value in judged gold acceptable set (primary + alternatives)\n")
    L.append("## Per-field acceptance\n")
    L.append("| field | accepted/n | acceptance | via alt | divisare | architizer | top error (gold→local) |")
    L.append("|---|---:|---:|---:|---:|---:|---|")
    for f in FIELDS:
        p = ev["per_field"][f]
        bs = p["by_stratum"]
        te = p["top_errors_gold_to_local"][:2]
        tes = "; ".join(f"{e['gold']}→{e['local']} ×{e['n']}" for e in te) or "-"
        L.append(f"| {f} | {p['accepted']}/{p['n']} | {pct(p['acceptance'])} | {p['accepted_via_alternative']} | "
                 f"{pct(bs.get('divisare', {}).get('acceptance'))} | {pct(bs.get('architizer', {}).get('acceptance'))} | {tes} |")
    L.append(f"| **pooled** | {la['pooled_accepted']}/{la['pooled_n']} | **{pct(la['pooled_9field_acceptance'])}** | | | | |\n")
    ds = ev["downstream_given_scope_medium_ok"]
    if ds:
        n_ok = next(iter(ds.values()))["n"]
        L.append(f"Cascade check — downstream acceptance on the {n_ok} images whose scope AND medium were accepted: "
                 + ", ".join(f"{f} {pct(v['acceptance'])}" for f, v in ds.items()) + "\n")
    L.append("## Lane A gate (plan §3, N10)\n")
    L.append(f"- scope obvious errors: **{la['scope_obvious_errors']}** {la['scope_error_kinds']}")
    L.append(f"- medium obvious errors: **{la['medium_obvious_errors']}** {la['medium_error_kinds']}")
    L.append(f"- pooled 9-field acceptance: **{pct(la['pooled_9field_acceptance'])}** (need ≥90%)")
    L.append(f"- result: **{'PASS' if la['pass'] else 'FAIL'}** — {', '.join(la['fail_reasons']) or 'all criteria met'}\n")
    L.append("## Lane B gate (plan §3, N10)\n")
    L.append("| building | stratum | local default | local top-3 | gold acceptable | top1 | top3 | critical |")
    L.append("|---|---|---|---|---|---|---|---|")
    for b in ev["lane_b"]["buildings"]:
        if not b["gold"]:
            L.append(f"| {b['canonical_bld_id']} {b['name']} | {b['stratum']} | {b['local_default_idx']} | "
                     f"{b['local_top3_idx']} | (no gold) | n/a | n/a | n/a |")
        else:
            L.append(f"| {b['canonical_bld_id']} {b['name']} | {b['stratum']} | {b['local_default_idx']} | "
                     f"{b['local_top3_idx']} | {b['acceptable_default_idx'] or 'NULL'} | "
                     f"{'ok' if b['top1_ok'] else 'MISS'} | {'ok' if b['top3_ok'] else 'MISS'} | "
                     f"{'YES ' + ','.join(b['critical_reasons']) if b['critical'] else '0'} |")
    L.append("")
    L.append(f"- critical cover errors: **{lb['critical_cover_errors']}**; top-1 acceptable **{lb['top1_in_acceptable']}** "
             f"(of scored {lb['top1_in_acceptable_of_scored']}); top-3 contains acceptable {lb['top3_contains_acceptable']} "
             f"(of scored {lb['top3_contains_acceptable_of_scored']})")
    L.append(f"- result: **{'PASS' if lb['pass'] else 'FAIL'}** — {', '.join(lb['fail_reasons']) or 'all criteria met'}\n")
    L.append("## Margin confidence → remote routing\n")
    L.append("Route the lowest-margin X% of each field to remote; accuracy on the auto-accepted rest. "
             "`own` = field's own margin (unscored N/A fields inherit min(scope, medium)); "
             "`chained` = min(own, scope, medium).\n")
    L.append("| field | baseline | AUROC own | X for ≥95% (own) | thr | auto acc | X for ≥95% (chained) | AUROC chained |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for f in FIELDS:
        o, c = ev["routing"]["own"][f], ev["routing"]["chained"][f]
        xo = f"{o['routed_pct']}%" if o["reached"] else "not reached"
        xc = f"{c['routed_pct']}%" if c["reached"] else "not reached"
        L.append(f"| {f} | {pct(o['baseline_accuracy'])} | {o['auroc_margin_vs_correct']} | {xo} | "
                 f"{o.get('threshold', '-')} | {pct(o.get('auto_accuracy'))} | {xc} | {c['auroc_margin_vs_correct']} |")
    il = ev["routing"]["image_level"]
    L.append("")
    L.append("Image level (confidence = min margin over the image's score blocks):\n")
    L.append("| routed | auto n | pooled field acc | all-9 correct | min-margin thr |")
    L.append("|---:|---:|---:|---:|---:|")
    for r in il["curve"]:
        L.append(f"| {r['routed_pct']}% | {r['auto_n']} | {pct(r['auto_pooled_field_acceptance'])} | "
                 f"{pct(r['auto_images_all9_correct'])} | {r['threshold_min_margin']} |")
    x = il["x_for_pooled_95"]
    L.append("")
    L.append(f"- X for pooled ≥95% on auto side: **{x['routed_pct']}%** (thr {x['threshold_min_margin']}, "
             f"auto all-9-correct {pct(x['auto_images_all9_correct'])})" if x else
             "- X for pooled ≥95% on auto side: **not reached**")
    u = il["union_of_per_field_x95_routes"]
    L.append(f"- union of per-field X95 routes: {u['routed_images']} images ({u['routed_pct']}%); "
             f"fields never reaching 95%: {u['fields_never_reaching_95'] or 'none'}")
    uc = il["union_of_x95_routes_local_viable_fields"]
    L.append(f"- local-viable fields ({uc['rule']}): {uc['fields']} -> union routed "
             f"{uc['routed_images']} images ({uc['routed_pct']}%)")
    L.append(f"- caveat: {ev['routing']['caveat']}\n")
    ex = rep["extrapolation"]
    L.append("## Full-run extrapolation\n")
    m = ex["measured"]
    L.append(f"Measured {m['images_per_sec_end_to_end']} img/s end-to-end (GPU-bound), peak VRAM {m['peak_vram_gb']} GB, "
             f"avg image {m['avg_image_bytes_sample'] / 1e3:.0f} KB, label row {m['label_row_bytes_full_scores']} B.\n")
    L.append("| population | images | GPU h | fetch seq 1.3 s | fetch 4 rps | fetch 6 rps | wall @4 / @6 rps | transfer GB | label out GB |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for k, nm in (("s6_lite_images", "S6-lite building_images"), ("s2_gallery_groups", "S2 gallery groups")):
        p = ex[k]
        L.append(f"| {nm} | {p['images']:,} | {p['gpu_end_to_end_h']} | {p['fetch']['sequential_1.3s_h']} h | "
                 f"{p['fetch']['4rps_h']} h | {p['fetch']['6rps_h']} h | {p['pipelined']['4rps_pipelined_wall_h']} / "
                 f"{p['pipelined']['6rps_pipelined_wall_h']} h | {p['network_transfer_gb']} | {p['label_output_gb_full_scores']} |")
    L.append("")
    L.append(f"- disk: {ex['disk_policy']}")
    L.append("- remote escalation volume implied by routing (config v1, in-sample):")
    for nm, e in ex["remote_escalation"].items():
        L.append(f"  - {nm}: {pct(e['fraction'])} → {e['s6_images']:,} S6 images / {e['s2_groups']:,} S2 groups")
    cc = ex["cover_candidates_remote"]
    L.append(f"  - plus cover candidates: {cc['buildings']:,} buildings × top-3 = {cc['top3_candidates']:,} images")
    L.append(f"- remote cost: {ex['remote_cost']}\n")
    L.append("## Inputs\n")
    for k, v in rep["inputs"]["sha256"].items():
        L.append(f"- `{k}` `{v}`")
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--wp-dir", default=str(WP_DIR))
    ap.add_argument("--no-write", action="store_true")
    a = ap.parse_args(argv)
    wp = Path(a.wp_dir)
    files = {n: wp / n for n in ("local_labels.jsonl", "local_run.json", "gold_labels_final.json",
                                  "judge_decisions.jsonl", "sample_manifest.json", "fetch_log.jsonl")}
    shas = {n: sha256_file(p) for n, p in files.items()}
    with open(files["local_labels.jsonl"], encoding="utf-8") as fh:
        local_recs = [json.loads(x) for x in fh if x.strip()]
    with open(files["local_run.json"], encoding="utf-8") as fh:
        local_run = json.load(fh)
    with open(files["gold_labels_final.json"], encoding="utf-8") as fh:
        gold = json.load(fh)
    with open(files["fetch_log.jsonl"], encoding="utf-8") as fh:
        fetch_log = [json.loads(x) for x in fh if x.strip()]
    with open(files["judge_decisions.jsonl"], encoding="utf-8") as fh:
        decisions = [json.loads(x) for x in fh if x.strip()]

    # input integrity: gold was built against exactly these local artifacts
    gi = gold.get("inputs", {})
    integrity = {
        "gold_local_labels_sha_match": gi.get("local_labels_sha256") == shas["local_labels.jsonl"],
        "gold_local_run_sha_match": gi.get("local_run_sha256") == shas["local_run.json"],
        "local_config_sha_consistent": len({r.get("config_sha256") for r in local_recs}) == 1,
    }
    ev = evaluate(local_recs, gold, local_run)
    il = ev["routing"]["image_level"]
    fracs = {
        "image_level_x95_pooled": (il["x_for_pooled_95"]["routed_pct"] / 100) if il["x_for_pooled_95"] else None,
        "union_per_field_x95": il["union_of_per_field_x95_routes"]["routed_pct"] / 100,
        "union_x95_local_viable_fields_only": il["union_of_x95_routes_local_viable_fields"]["routed_pct"] / 100,
    }
    labels_bytes = files["local_labels.jsonl"].stat().st_size
    ext = extrapolate(local_run, fetch_log, labels_bytes, len(local_recs), fracs)
    rep = {
        "eval_version": EVAL_VERSION,
        "tool": "tools/vnext_wp3_eval.py",
        "tool_sha256": sha256_file(Path(__file__)),
        "inputs": {
            "engine": local_recs[0].get("engine") if local_recs else None,
            "config_version": local_recs[0].get("config_version") if local_recs else None,
            "config_sha256": local_recs[0].get("config_sha256") if local_recs else None,
            "sha256": shas,
            "integrity": integrity,
            "judge_decisions": {"n": len(decisions),
                                "by_decision": dict(Counter(d["decision"] for d in decisions))},
        },
        "gates": {"lane_a": ev["lane_a"]["pass"], "lane_b": ev["lane_b"]["gate"]["pass"]},
        "eval": ev,
        "extrapolation": ext,
        "paid_calls": 0,
        "network_requests": 0,
    }
    md = render_md(rep)
    if not a.no_write:
        (wp / "eval_report.json").write_text(json.dumps(rep, indent=1, ensure_ascii=False), encoding="utf-8")
        (wp / "eval_report.md").write_text(md, encoding="utf-8")
    print(md.splitlines()[2])
    print(json.dumps({"integrity": integrity, "pooled": ev["lane_a"]["pooled_9field_acceptance"],
                      "lane_b": ev["lane_b"]["gate"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
