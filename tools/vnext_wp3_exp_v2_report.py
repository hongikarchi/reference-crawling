#!/usr/bin/env python3
"""WP3 exp v2: render data/smoke_vnext/wp3_n10_v1/exp_v2/report.md from results.json (+ baseline).

Pure formatting; no model, no network. Run with any python (repo .venv is fine).
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data/smoke_vnext/wp3_n10_v1/exp_v2"
FIELDS = ["in_scope", "reject_reason", "medium", "spatial_context", "framing_scale", "camera_angle",
          "drawing_kind", "project_state", "project_legibility"]
SHORT = {"in_scope": "scope", "reject_reason": "reject", "medium": "medium", "spatial_context": "spatial",
         "framing_scale": "framing", "camera_angle": "camera", "drawing_kind": "drawing_k",
         "project_state": "state", "project_legibility": "legib"}


def pct(x):
    return "-" if x is None else f"{100 * x:.1f}%"


def base_summary(b: dict) -> dict:
    e = b["eval"]
    la, lb = e["lane_a"], e["lane_b"]["gate"]
    return {"pooled": la["pooled_9field_acceptance"], "per_field": {f: e["per_field"][f]["acceptance"] for f in FIELDS},
            "scope_obvious_errors": la["scope_obvious_errors"], "medium_obvious_errors": la["medium_obvious_errors"],
            "cover_top1": lb["top1_in_acceptable"], "cover_top3": lb["top3_contains_acceptable"],
            "cover_critical": lb["critical_cover_errors"], "n_images": e["scored_images"]}


def row(name, s, note=""):
    pf = s["per_field"]
    return (f"| {name} | {s.get('n_images', '-')} | " + " | ".join(pct(pf[f]) for f in FIELDS)
            + f" | **{pct(s['pooled'])}** | {s['scope_obvious_errors']} | {s['medium_obvious_errors']} | "
              f"{s['cover_top1']} | {s['cover_top3']} | {s['cover_critical']} | {note} |")


HEADER = ("| engine | n | " + " | ".join(SHORT[f] for f in FIELDS)
          + " | pooled | scope err | medium err | cover top1 | cover top3 | crit | note |\n|---|---:|"
          + "---:|" * len(FIELDS) + "---:|---:|---:|---|---|---:|---|")


def main() -> int:
    r = json.loads((OUT / "results.json").read_text(encoding="utf-8"))
    b = json.loads((OUT / "baseline_v1_225_eval_report.json").read_text(encoding="utf-8"))
    bs = base_summary(b)
    pl = r.get("probe_lobo", {})
    best_probe = r.get("probe_best_variant")
    cands = {"zs_v2_crossfold": r["zs_v2_crossfold_merged_225"]}
    for k, v in pl.items():
        cands[f"probe_{k}"] = v
    for k, v in r.get("combo", {}).items():
        cands[f"combo_{k}"] = v
    best = max(cands, key=lambda k: cands[k]["pooled"])
    s = cands[best]
    first = (f"WP3 exp v2: best={best} pooled={pct(s['pooled'])} medium={pct(s['per_field']['medium'])} "
             f"scope_err={s['scope_obvious_errors']} medium_err={s['medium_obvious_errors']} cover_top1={s['cover_top1']}")
    L = [f"# WP3 exp v2 - local engine experiments on N10 gold v2 (2026-10-27)\n", first + "\n"]
    L.append("- gold: `gold_labels_final_v2.json` (225 images / 10 buildings), acceptance = gold primary or judged "
             "acceptable alternative (unchanged `tools/vnext_wp3_eval.py` `evaluate`)")
    L.append("- bars (plan §3 N10): lane A scope/medium obvious errors 0 AND pooled 9-field >= 90%; lane B "
             "default acceptable 10/10 AND critical 0")
    L.append(f"- cost $0 (local GPU RTX 5070 Ti + CPU sklearn), network 0 (scikit-learn install only), Neon/R2 0, "
             f"frozen v1 inputs/outputs untouched")
    f = r["folds"]
    L.append(f"- folds ({f['rule']}): A = {', '.join(f['A'])} ({f['A_images']} img); "
             f"B = {', '.join(f['B'])} ({f['B_images']} img)\n")

    L.append("## 1. All engines (honest numbers = held-out buildings only, except where marked in-sample)\n")
    L.append(HEADER)
    L.append(row("v1 frozen (baseline, 225)", bs, "re-score of local_labels.jsonl"))
    L.append(row("v2 base untuned", r["zs_v2_base_untuned_225"], "prompts written after v1 aggregate report; no fold tuning"))
    L.append(row("**v2 cross-fold (A→B + B→A)**", r["zs_v2_crossfold_merged_225"], "each image scored by the config tuned on the other fold"))
    for k, v in pl.items():
        L.append(row(f"**probe LOBO {k}**", v, "train 9 bld, test held-out bld; nested-LOBO C"))
    for k, v in r.get("combo", {}).items():
        L.append(row(f"**combo {k}**", v, "EXPLORATORY (combination chosen after seeing results): scope/reject = v2 cross-fold, rest = probe LOBO"))
    L.append("")
    ca = r.get("catch_all_scope_medium_routing", {})
    if ca:
        L.append("Routing needed for lane A 'obvious errors 0' (route lowest own-margin first until every scope / medium "
                 "error is routed; in-sample threshold, optimistic):\n")
        L.append("| engine | scope err | route to catch all | medium err | route to catch all | union routed |")
        L.append("|---|---:|---:|---:|---:|---:|")
        for k, v in ca.items():
            L.append(f"| {k} | {v['in_scope']['errors']} | {v['in_scope']['routed_pct']}% | {v['medium']['errors']} | "
                     f"{v['medium']['routed_pct']}% | {v['union_scope_medium']['routed_pct']}% |")
        L.append("")
    L.append(f"- v1 parity: re-scoring config v1 from the embedding cache reproduces frozen `local_labels.jsonl` with "
             f"**{r['zs_parity_v1_vs_frozen_local_labels']['field_mismatches']} field mismatches** / 2,025")
    L.append(f"- blank pixel rule (v2) fires on {r['blank_rule_fires']} / 225 images (gold has no unreadable image)\n")

    L.append("## 2. Baseline: config v1 on all 225 images\n")
    e = b["eval"]
    L.append(f"- pooled **{pct(bs['pooled'])}** (173-image v1 report: 76.2%); scope errors **{bs['scope_obvious_errors']}** "
             f"{e['lane_a']['scope_error_kinds']}; medium errors **{bs['medium_obvious_errors']}** {e['lane_a']['medium_error_kinds']}")
    L.append(f"- lane B: top-1 **{bs['cover_top1']}**, top-3 {bs['cover_top3']}, critical **{bs['cover_critical']}** "
             f"(bld_006875 default 87 = gold element_detail) -> lane A FAIL, lane B FAIL")
    L.append(f"- full report: `exp_v2/baseline_v1_225_eval_report.{{md,json}}`\n")

    L.append("## 3. Zero-shot prompt v2 (cross-fold)\n")
    L.append("Tuning = greedy per-field prompt-set choice {v1, v2, union} in cascade order, then 2 sweeps of per-value "
             "logit offsets (grid -6..+6 step 0.5) maximising pooled acceptance (scope+medium double-weighted) on the "
             "tuning fold only. Tuned configs: `exp_v2/config_v2_tuned_foldA.json`, `config_v2_tuned_foldB.json`.\n")
    L.append(HEADER)
    for k, v in r["zs_v2_crossfold"].items():
        t, h = k.split("_")[1], k.split("_")[3]
        L.append(row(f"tuned {t}: in-sample on {t}", v["on_tuning_fold_in_sample"], "optimistic"))
        L.append(row(f"tuned {t}: **held-out {h}**", v["on_heldout_fold"], "honest"))
        L.append(row(f"v1 on {h}", v["v1_on_heldout_fold"], "reference"))
        L.append(row(f"v2 base on {h}", v["v2_base_on_heldout_fold"], "reference"))
    L.append("")
    for k, v in r["zs_v2_crossfold"].items():
        L.append(f"- `{k}`: prompt sets {v['prompt_set_choice']}; offsets {v['value_logit_offsets']}")
    z = r["zs_v2_crossfold_merged_225"]
    L.append(f"- merged cross-fold errors: scope {z['scope_error_kinds']}; medium {z['medium_error_kinds']}")
    L.append(f"- top residual errors (gold→pred): " + "; ".join(
        f"{SHORT[f]}: " + ", ".join(f"{x['gold']}→{x['local']}×{x['n']}" for x in z['top_errors'][f][:2])
        for f in ("medium", "framing_scale", "camera_angle", "project_state", "project_legibility")))
    L.append("- offsets differ between folds (e.g. camera `elevated` -2.5 vs -3.5; framing/state offsets only in one fold) "
             "and add +1.2 / +3.3 pp on the held-out fold over v2 base; most of the v1→v2 gain is structural "
             "(no `unknown` floor, pixel blank rule instead of SigLIP 'unreadable', reworded prompts)\n")

    if pl:
        L.append("## 4. Linear probe, leave-one-building-out\n")
        L.append("Per head logistic regression (StandardScaler + L2 LR) on the 1152-d SigLIP-2 embedding; training rows = "
                 "gold rows where the field is applicable (gold primary as target); prediction cascade = scope → reject → "
                 "medium → contract §5 applicability → spatial/framing/camera (aerial only if exterior) / drawing_kind / "
                 "state / legibility. C per head chosen by nested inner LOBO on the 9 training buildings (grid "
                 "0.01/0.1/1). Cover score = P(photograph)·P(exterior)·P(overall)·P(visibly_finished); default = best of "
                 "predicted tier 1/2 (v1 tier rule on predicted labels), NULL if none.\n")
        L.append("| variant | features | pooled | scope err | medium err | cover top1 | top3 | crit | X95 routed (image-level) | fields never 95% |")
        L.append("|---|---|---:|---:|---:|---|---|---:|---|---|")
        feat = {"emb": "embedding (squash 384)", "emb_tta": "mean(full, center-crop, flip)",
                "zs_logits_only": "v1+v2 zero-shot value logits only (standardised)",
                "hybrid_emb_zs": "embedding + zero-shot logits"}
        for k, v in pl.items():
            x = v.get("image_level_x95")
            L.append(f"| {k} | {feat.get(k, k)} | **{pct(v['pooled'])}** | {v['scope_obvious_errors']} | "
                     f"{v['medium_obvious_errors']} | {v['cover_top1']} | {v['cover_top3']} | {v['cover_critical']} | "
                     f"{(str(x['routed_pct']) + '%') if x else 'not reached'} | {', '.join(v['fields_never_95']) or 'none'} |")
        L.append("")
        if best_probe:
            v = pl[best_probe]
            L.append(f"Best probe `{best_probe}`: scope errors {v['scope_error_kinds']}; medium errors {v['medium_error_kinds']}.")
            L.append("Per-field own-margin routing X for ≥95% auto accuracy: " + ", ".join(
                f"{SHORT[f]} {('%s%%' % v['routing_x95_own'][f]) if v['routing_x95_own'][f] is not None else 'n/r'}"
                for f in FIELDS) + ".")
            L.append("Cover rows: " + "; ".join(
                f"{c['canonical_bld_id']} {c['local_default_idx']}{'' if c['top1_ok'] else ' MISS'}"
                f"{' CRIT ' + ','.join(c['critical_reasons']) if c['critical_reasons'] else ''}" for c in v["cover_rows"]))
            L.append(f"Nested C choices: {v.get('nested_C_choices')}\n")

    bud = r.get("budget")
    if bud:
        L.append("## 5. Label-budget curve (LOBO, probe features `%s`, C fixed = mode of nested choices)\n" % bud["features"])
        L.append("Training images sampled at random from the 9 non-held-out buildings (5 seeds), evaluated on the held-out "
                 "building; all 10 held-out buildings pooled.\n")
        L.append("| train images | pooled | ±sd | medium | in_scope | camera | legib | framing | scope err | medium err |")
        L.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for p in bud["image_subsets"]:
            m = p["mean"]
            L.append(f"| {p['n_train']} | {pct(m['pooled'])} | {pct(m.get('pooled_sd'))} | {pct(m['medium'])} | "
                     f"{pct(m['in_scope'])} | {pct(m['camera_angle'])} | {pct(m['project_legibility'])} | "
                     f"{pct(m['framing_scale'])} | {m['scope_err']} | {m['medium_err']} |")
        L.append("")
        L.append("| train buildings | ≈images | pooled | ±sd | medium | in_scope | scope err | medium err |")
        L.append("|---:|---:|---:|---:|---:|---:|---:|---:|")
        for p in bud["building_subsets"]:
            m = p["mean"]
            L.append(f"| {p['n_buildings']} | {p['approx_images']} | {pct(m['pooled'])} | {pct(m.get('pooled_sd'))} | "
                     f"{pct(m['medium'])} | {pct(m['in_scope'])} | {m['scope_err']} | {m['medium_err']} |")
        L.append("")
        L.append("**Rough extrapolation (power law err = a·n^-b on the image-subset curve; ROUGH GUESS — 10 buildings, "
                 "one corpus mix, gold from 1 reviewer + judge, no holdout of the curve itself):**\n")
        L.append("| target | a | b | images needed (≈) |")
        L.append("|---|---:|---:|---:|")
        for k, v in bud["extrapolation_rough_guess"].items():
            L.append(f"| {k} | {v.get('a', '-')} | {v.get('b', '-')} | {v.get('n_needed') or v.get('note', 'n/a')} |")
        L.append("")

    h = r.get("hard_fields")
    if h:
        L.append(f"## 6. Inherently hard fields (engine `{h['engine']}`)\n")
        L.append("Gold ambiguity (rows whose judged acceptable set has >1 value): " + ", ".join(
            f"{SHORT[k]} {v['pct']}%" for k, v in h["gold_ambiguity"].items()) + "\n")
        c, g = h["camera_angle"], h["project_legibility"]
        L.append("| field | as-is | merged proposal | merged acceptance |")
        L.append("|---|---:|---|---:|")
        L.append(f"| camera_angle | {pct(c['as_is']['acceptance'])} | eye_level+elevated → one 'ground_or_elevated' (aerial kept) | {pct(c['merge_eye_level_elevated']['acceptance'])} |")
        L.append(f"| project_legibility | {pct(g['as_is']['acceptance'])} | high+medium → readable vs low | {pct(g['two_level_readable_vs_low']['acceptance'])} |")
        L.append(f"| project_legibility | {pct(g['as_is']['acceptance'])} | high vs medium+low (partial) | {pct(g['two_level_high_vs_partial']['acceptance'])} |")
        L.append(f"\nPooled 9-field with both merges (camera merge + legibility readable/low): **{pct(h['pooled_with_both_merges'])}**\n")
        L.append("Abstain-as-`unknown` curves (applicable rows; accuracy on the kept rows when the lowest-margin X% abstain):\n")
        L.append("| field | " + " | ".join(f"{p['abstain_pct']}%" for p in c["abstain_curve_applicable_rows"]) + " |")
        L.append("|---|" + "---:|" * len(c["abstain_curve_applicable_rows"]))
        for fld in ("camera_angle", "project_legibility", "framing_scale", "medium"):
            cur = h[fld]["abstain_curve_applicable_rows"]
            L.append(f"| {fld} | " + " | ".join(pct(p["acc_on_kept"]) for p in cur) + " |")
        L.append("")
    rec = (OUT / "recommendation.md")
    if rec.exists():
        L.append(rec.read_text(encoding="utf-8"))
    L.append("\n## Files\n")
    for p in ["tools/vnext_s3_local_engine_config_v2.json", "tools/vnext_wp3_exp_v2_embed.py", "tools/vnext_wp3_exp_v2_baseline.py",
              "tools/vnext_wp3_exp_v2.py", "tools/vnext_wp3_exp_v2_report.py"]:
        L.append(f"- `{p}`")
    for p in sorted(OUT.iterdir()):
        if p.name not in ("recommendation.md",):
            L.append(f"- `exp_v2/{p.name}`")
    L.append(f"\nInputs sha256: {json.dumps(r['inputs'])}")
    (OUT / "report.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print(first)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
