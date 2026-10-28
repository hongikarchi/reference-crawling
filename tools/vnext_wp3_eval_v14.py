#!/usr/bin/env python3
"""vNext WP3 N10 re-score under gallery-semantics contract v1.4 (offline, $0).

Contract `docs/vnext/CONTRACT_GALLERY_SEMANTICS.md` §4.1 (v1.4, 2026-10-28):
- project_legibility emitted values = high | low | none | unknown; `medium` maps to `high`
  on BOTH sides (gold primary + judged acceptable alternatives, and predictions)
- camera_angle: for gold primary in {eye_level, elevated} a predicted `unknown` is accepted;
  aerial values must be explicit (no change to aerial / not_applicable gold rows)

Scoring itself is delegated to the unchanged v1 tool `tools/vnext_wp3_eval.py::evaluate`
(acceptance = mapped gold primary OR mapped judged acceptable alternative). The v1 tool is
imported, never modified. Engines re-scored: config v1 frozen (`local_labels.jsonl` +
`local_run.json`) and every `exp_v2/pred_*.jsonl`. Each engine is scored under v1.3 (as-is,
parity with exp_v2/results.json) and v1.4. Lane B (covers) is unaffected by the mapping
(cover tier does not read legibility; no existing engine emits camera `unknown`).

Writes only `exp_v2/rescore_v14.{json,md}`. Inputs are opened read-only.
No network, no Neon/R2, no model calls.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
import warnings
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import vnext_wp3_eval as ev  # noqa: E402  (unchanged v1 scorer)

WP = ROOT / "data" / "smoke_vnext" / "wp3_n10_v1"
EXP = WP / "exp_v2"
GOLD = WP / "gold_labels_final_v2.json"
CONTRACT = ROOT / "docs" / "vnext" / "CONTRACT_GALLERY_SEMANTICS.md"
EVAL_VERSION = "vnext-wp3-n10-eval-v1.4"
CONTRACT_VERSION = "vnext-gallery-semantics-contract-v1.4"
FIELDS = ev.FIELDS

LEGIB_MAP = {"medium": "high"}
CAMERA_UNKNOWN_OK_FOR = ("eye_level", "elevated")
ABSTAIN_PCTS = (10, 20, 30)

# engine name -> (pred file relative to WP, cover source); order = report order
ENGINES = [
    ("v1_frozen", "local_labels.jsonl", "local_run"),
    ("zs_v2_crossfold", "exp_v2/pred_zs_v2_crossfold.jsonl", "tiers"),
    ("probe_emb", "exp_v2/pred_probe_emb.jsonl", "tiers"),
    ("probe_emb_tta", "exp_v2/pred_probe_emb_tta.jsonl", "tiers"),
    ("probe_zs_logits_only", "exp_v2/pred_probe_zs_logits_only.jsonl", "tiers"),
    ("probe_hybrid_emb_zs", "exp_v2/pred_probe_hybrid_emb_zs.jsonl", "tiers"),
    ("combo_zsScope_probe_zs_logits_only", "exp_v2/pred_combo_zsScope_probe_zs_logits_only.jsonl", "tiers"),
    ("combo_zsScope_probe_emb", "exp_v2/pred_combo_zsScope_probe_emb.jsonl", "tiers"),
]
# exp_v2/results.json key of the same engine (v1.3 parity check)
PARITY_KEY = {
    "v1_frozen": ("zs_v1_cache_225",),
    "zs_v2_crossfold": ("zs_v2_crossfold_merged_225",),
    "probe_emb": ("probe_lobo", "emb"),
    "probe_emb_tta": ("probe_lobo", "emb_tta"),
    "probe_zs_logits_only": ("probe_lobo", "zs_logits_only"),
    "probe_hybrid_emb_zs": ("probe_lobo", "hybrid_emb_zs"),
    "combo_zsScope_probe_zs_logits_only": ("combo", "zsScope+probe_zs_logits_only"),
    "combo_zsScope_probe_emb": ("combo", "zsScope+probe_emb"),
}


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def read_jsonl(p: Path) -> list[dict]:
    with open(p, encoding="utf-8") as fh:
        return [json.loads(x) for x in fh if x.strip()]


# ---------------------------------------------------------------- v1.4 mapping
def map_legibility(v):
    return LEGIB_MAP.get(v, v)


def map_gold_v14(gold: dict) -> tuple[dict, dict]:
    """Deep-copied gold with the v1.4 mapping; returns (gold_v14, stats)."""
    g = copy.deepcopy(gold)
    st = Counter()
    for lab in g["labels"]:
        acc = lab.get("acceptable")
        if not isinstance(acc, dict):
            acc = {}
            lab["acceptable"] = acc
        # legibility: medium -> high on primary and alternatives
        if lab["project_legibility"] == "medium":
            st["legibility_primary_medium_to_high"] += 1
        lab["project_legibility"] = map_legibility(lab["project_legibility"])
        alts = acc.get("project_legibility") or []
        if alts:
            if "medium" in alts:
                st["legibility_alt_medium_to_high"] += 1
            new = []
            for v in alts:
                m = map_legibility(v)
                if m not in new:
                    new.append(m)
            acc["project_legibility"] = new
        lset = {lab["project_legibility"], *acc.get("project_legibility", [])}
        if {"high", "low"} <= lset:
            st["legibility_rows_high_and_low_both_accepted"] += 1
        # camera: unknown accepted for eye_level / elevated gold primaries
        if lab["camera_angle"] in CAMERA_UNKNOWN_OK_FOR:
            calts = list(acc.get("camera_angle") or [])
            if "unknown" in calts:
                st["camera_unknown_already_acceptable"] += 1
            else:
                calts.append("unknown")
                st["camera_unknown_added"] += 1
            acc["camera_angle"] = calts
    g["contract_version_scoring"] = CONTRACT_VERSION
    return g, dict(st)


def map_preds_v14(recs: list[dict]) -> tuple[list[dict], dict]:
    out = copy.deepcopy(recs)
    st = Counter()
    for r in out:
        lab = r["labels"]
        if lab["project_legibility"] == "medium":
            st["pred_legibility_medium_to_high"] += 1
        lab["project_legibility"] = map_legibility(lab["project_legibility"])
        if lab["camera_angle"] == "unknown":
            st["pred_camera_unknown"] += 1
    return out, dict(st)


# ---------------------------------------------------------------- covers (mirrors exp_v2 run_doc)
def run_doc_from_tiers(recs: list[dict], names: dict) -> dict:
    """local_run-like doc: default = best tier<3 by (tier, -score, idx); same rule as
    tools/vnext_wp3_exp_v2.py::run_doc (re-implemented here to avoid numpy)."""
    by = defaultdict(list)
    for r in recs:
        by[r["canonical_bld_id"]].append(r)
    out = {}
    for b, cand in by.items():
        cand = sorted(cand, key=lambda x: (x["cover"]["tier"], -x["cover"]["score"], x["idx"]))
        elig = [x for x in cand if x["cover"]["tier"] < 3]
        out[b] = {"name": names[b][0], "stratum": names[b][1],
                  "default_cover_idx": elig[0]["idx"] if elig else None,
                  "top3_idx": [x["idx"] for x in elig[:3]], "review_required": not elig}
    return {"buildings": out}


# ---------------------------------------------------------------- what-if: camera abstention
def camera_abstain_whatif(recs: list[dict], gold_v14: dict) -> list[dict]:
    """IN-SAMPLE what-if: relabel the lowest-margin k% of predicted eye_level/elevated rows as
    `unknown` and score camera under v1.4. Existing engines never emit `unknown`, so this is
    the only way to see what the v1.4 boundary rule could buy. Thresholds are in-sample."""
    gidx = {g["idx"]: g for g in gold_v14["labels"]}
    cand = []
    for r in recs:
        if r["labels"]["camera_angle"] in CAMERA_UNKNOWN_OK_FOR:
            m = ((r.get("scores") or {}).get("camera_angle") or {}).get("margin")
            cand.append((float(m) if m is not None else 0.0, r["idx"]))
    cand.sort()
    rows = []
    n = len(recs)
    for pct in ABSTAIN_PCTS:
        k = round(len(cand) * pct / 100)
        abst = {i for _, i in cand[:k]}
        ok = 0
        unknown_on_aerial_or_na = 0
        for r in recs:
            v = "unknown" if r["idx"] in abst else r["labels"]["camera_angle"]
            g = gidx[r["idx"]]
            ok += ev.norm(v) in ev.accepted_set(g, "camera_angle")
            if r["idx"] in abst and g["camera_angle"] not in CAMERA_UNKNOWN_OK_FOR:
                unknown_on_aerial_or_na += 1
        rows.append({"abstain_pct_of_eye_level_elevated_preds": pct, "abstained_n": k,
                     "camera_acceptance": round(ok / n, 4),
                     "abstained_where_gold_not_eye_level_elevated": unknown_on_aerial_or_na})
    return rows


# ---------------------------------------------------------------- per-engine scoring
def score(recs: list[dict], gold: dict, run: dict) -> dict:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        e = ev.evaluate(recs, gold, run)
    la, lb = e["lane_a"], e["lane_b"]["gate"]
    return {
        "n_images": e["scored_images"], "n_buildings": e["scored_buildings"],
        "pooled": la["pooled_9field_acceptance"], "pooled_accepted": la["pooled_accepted"],
        "pooled_n": la["pooled_n"],
        "per_field": {f: e["per_field"][f]["acceptance"] for f in FIELDS},
        "per_field_accepted": {f: e["per_field"][f]["accepted"] for f in FIELDS},
        "scope_obvious_errors": la["scope_obvious_errors"],
        "medium_obvious_errors": la["medium_obvious_errors"],
        "scope_error_kinds": la["scope_error_kinds"], "medium_error_kinds": la["medium_error_kinds"],
        "fields_at_or_above_90pct": la["fields_at_or_above_90pct"],
        "lane_a_pass": la["pass"], "lane_a_fail_reasons": la["fail_reasons"],
        "cover_top1": lb["top1_in_acceptable"], "cover_top3": lb["top3_contains_acceptable"],
        "cover_critical": lb["critical_cover_errors"], "lane_b_pass": lb["pass"],
        "top_errors": {f: e["per_field"][f]["top_errors_gold_to_local"][:3]
                       for f in ("camera_angle", "project_legibility")},
        "confusion": {f: e["confusion"][f]["rows_gold_primary_cols_local"]
                      for f in ("camera_angle", "project_legibility")},
        "routing_x95_own": {f: (e["routing"]["own"][f]["routed_pct"] if e["routing"]["own"][f]["reached"] else None)
                            for f in FIELDS},
        "routing_auroc_own": {f: e["routing"]["own"][f]["auroc_margin_vs_correct"] for f in FIELDS},
    }


def _get(d: dict, path: tuple):
    for k in path:
        d = d.get(k) if isinstance(d, dict) else None
    return d


def pct(x):
    return "-" if x is None else f"{100 * x:.1f}%"


SHORT = {"in_scope": "scope", "reject_reason": "reject", "medium": "medium", "spatial_context": "spatial",
         "framing_scale": "framing", "camera_angle": "camera", "drawing_kind": "drawing_k",
         "project_state": "state", "project_legibility": "legib"}


def render_md(rep: dict) -> str:
    L = [f"# WP3 N10 re-score under contract v1.4 (`{EVAL_VERSION}`)\n"]
    best = rep["summary_line"]
    L.append(best + "\n")
    L.append(f"- contract: `{rep['contract']['path']}` v1.4 sha `{rep['contract']['sha256']}`")
    L.append(f"- gold: `gold_labels_final_v2.json` (225 images / 10 buildings), sha `{rep['inputs']['gold_sha256'][:12]}`")
    L.append("- v1.4 mapping (both sides): legibility `medium`→`high`; camera `unknown` accepted when gold primary ∈ {eye_level, elevated}")
    gs = rep["gold_mapping_stats"]
    L.append(f"- gold mapping: {gs}")
    L.append("- scorer: unchanged `tools/vnext_wp3_eval.py::evaluate` on mapped copies; cost $0, network 0, Neon/R2 0, model calls 0")
    L.append("- honest-number caveats from exp_v2 still apply: probe = LOBO, zs_v2 = cross-fold, combo = post-hoc selection (exploratory)\n")
    L.append("## 1. v1.4 acceptance per engine (225 images)\n")
    hdr = "| engine | " + " | ".join(SHORT[f] for f in FIELDS) + " | **pooled v1.4** | pooled v1.3 | Δ | scope err | medium err | lane A | cover top1 / top3 / crit |"
    L.append(hdr)
    L.append("|---|" + "---:|" * len(FIELDS) + "---:|---:|---:|---:|---:|---|---|")
    for name, r in rep["engines"].items():
        v4, v3 = r["v14"], r["v13"]
        d = v4["pooled"] - v3["pooled"]
        L.append(f"| {name} | " + " | ".join(pct(v4["per_field"][f]) for f in FIELDS)
                 + f" | **{pct(v4['pooled'])}** | {pct(v3['pooled'])} | {100 * d:+.1f}pp | {v4['scope_obvious_errors']} | "
                 f"{v4['medium_obvious_errors']} | {'PASS' if v4['lane_a_pass'] else 'FAIL'} | "
                 f"{v4['cover_top1']} / {v4['cover_top3']} / {v4['cover_critical']} |")
    L.append("")
    L.append("Lane A (plan §3 N10) = scope/medium obvious errors 0 AND pooled ≥ 90%. Lane B unchanged by v1.4.\n")
    L.append("## 2. Changed fields: v1.3 → v1.4\n")
    L.append("| engine | legib v1.3 | legib v1.4 | camera v1.3 | camera v1.4 | pred legib medium→high | fields ≥90% (v1.4) |")
    L.append("|---|---:|---:|---:|---:|---:|---|")
    for name, r in rep["engines"].items():
        v4, v3 = r["v14"], r["v13"]
        L.append(f"| {name} | {pct(v3['per_field']['project_legibility'])} | {pct(v4['per_field']['project_legibility'])} | "
                 f"{pct(v3['per_field']['camera_angle'])} | {pct(v4['per_field']['camera_angle'])} | "
                 f"{r['pred_mapping_stats'].get('pred_legibility_medium_to_high', 0)} | "
                 f"{len(v4['fields_at_or_above_90pct'])}/9 |")
    L.append("")
    L.append("- camera is unchanged for every engine: no existing prediction emits `unknown` (the v1.4 rule only "
             "pays off once an engine abstains at the boundary). In-sample what-if below.")
    L.append("- legibility rows where both `high` and `low` are accepted after mapping (gold `medium` primary with a `low` alternative, "
             f"or `low` with a `medium` alternative): {gs.get('legibility_rows_high_and_low_both_accepted', 0)} — these rows no longer discriminate.\n")
    L.append("## 3. Remaining legibility / camera errors under v1.4 (gold→pred)\n")
    L.append("| engine | legibility top errors | camera top errors |")
    L.append("|---|---|---|")
    for name, r in rep["engines"].items():
        te = r["v14"]["top_errors"]
        f = lambda xs: "; ".join(f"{e['gold']}→{e['local']} ×{e['n']}" for e in xs) or "-"  # noqa: E731
        L.append(f"| {name} | {f(te['project_legibility'])} | {f(te['camera_angle'])} |")
    L.append("")
    L.append("## 4. What-if (IN-SAMPLE, informational): camera abstention to `unknown`\n")
    L.append("Relabel the lowest-own-margin k% of predicted eye_level/elevated rows as `unknown`, score camera under v1.4.\n")
    L.append("| engine | camera v1.4 as-is | " + " | ".join(f"abstain {p}%" for p in ABSTAIN_PCTS) + " |")
    L.append("|---|---:|" + "---:|" * len(ABSTAIN_PCTS))
    for name, r in rep["engines"].items():
        cells = [f"{pct(w['camera_acceptance'])} (n={w['abstained_n']}, wrong-gold {w['abstained_where_gold_not_eye_level_elevated']})"
                 for w in r["camera_abstain_whatif"]]
        L.append(f"| {name} | {pct(r['v14']['per_field']['camera_angle'])} | " + " | ".join(cells) + " |")
    L.append("")
    L.append("`wrong-gold` = abstained rows whose gold is aerial/not_applicable (not rescued by the v1.4 rule).\n")
    L.append("## 5. Parity (v1.3 scoring here vs exp_v2/results.json)\n")
    for name, r in rep["engines"].items():
        p = r["parity_v13"]
        L.append(f"- {name}: pooled {p['here']} vs {p['results_json']} → {'OK' if p['match'] else 'MISMATCH'}; "
                 f"scope/medium err {p['errs_here']} vs {p['errs_results_json']}; cover top1 {p['top1_here']} vs {p['top1_results_json']}")
    L.append("")
    L.append("## 6. Inputs\n")
    for k, v in rep["inputs"]["sha256"].items():
        L.append(f"- `{k}` `{v}`")
    L.append(f"- tool `tools/vnext_wp3_eval_v14.py` `{rep['tool_sha256']}`; v1 scorer `tools/vnext_wp3_eval.py` `{rep['v1_scorer_sha256']}`")
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-write", action="store_true")
    a = ap.parse_args(argv)

    gold = json.loads(GOLD.read_text(encoding="utf-8"))
    gold14, gstats = map_gold_v14(gold)
    man = json.loads((WP / "sample_manifest.json").read_text(encoding="utf-8"))
    names = {b["canonical_bld_id"]: (b["name"], b["stratum"]) for b in man["buildings"]}
    local_run = json.loads((WP / "local_run.json").read_text(encoding="utf-8"))
    results = json.loads((EXP / "results.json").read_text(encoding="utf-8"))

    shas = {"gold_labels_final_v2.json": sha256_file(GOLD),
            "local_run.json": sha256_file(WP / "local_run.json"),
            "sample_manifest.json": sha256_file(WP / "sample_manifest.json"),
            "exp_v2/results.json": sha256_file(EXP / "results.json")}
    engines = {}
    for name, rel, cover_src in ENGINES:
        p = WP / rel
        shas[rel] = sha256_file(p)
        recs = read_jsonl(p)
        run = local_run if cover_src == "local_run" else run_doc_from_tiers(recs, names)
        v13 = score(recs, gold, run)
        recs14, pstats = map_preds_v14(recs)
        v14 = score(recs14, gold14, run)
        ref = _get(results, PARITY_KEY[name]) or {}
        parity = {"here": v13["pooled"], "results_json": ref.get("pooled"),
                  "match": (ref.get("pooled") == v13["pooled"]
                            and ref.get("scope_obvious_errors") == v13["scope_obvious_errors"]
                            and ref.get("medium_obvious_errors") == v13["medium_obvious_errors"]
                            and ref.get("cover_top1") == v13["cover_top1"]),
                  "errs_here": [v13["scope_obvious_errors"], v13["medium_obvious_errors"]],
                  "errs_results_json": [ref.get("scope_obvious_errors"), ref.get("medium_obvious_errors")],
                  "top1_here": v13["cover_top1"], "top1_results_json": ref.get("cover_top1")}
        engines[name] = {"pred_file": rel, "cover_source": cover_src, "pred_mapping_stats": pstats,
                         "v13": v13, "v14": v14, "parity_v13": parity,
                         "camera_abstain_whatif": camera_abstain_whatif(recs14, gold14)}

    best = max(engines, key=lambda n: engines[n]["v14"]["pooled"])
    b = engines[best]["v14"]
    summary = (f"WP3 rescore v1.4: best={best} pooled={pct(b['pooled'])} scope_err={b['scope_obvious_errors']} "
               f"medium_err={b['medium_obvious_errors']} lane_A={'PASS' if b['lane_a_pass'] else 'FAIL'} "
               f"cover_top1={b['cover_top1']} crit={b['cover_critical']}")
    rep = {
        "eval_version": EVAL_VERSION,
        "contract": {"path": CONTRACT.relative_to(ROOT).as_posix(), "version": CONTRACT_VERSION,
                     "sha256": sha256_file(CONTRACT)},
        "tool": "tools/vnext_wp3_eval_v14.py", "tool_sha256": sha256_file(Path(__file__)),
        "v1_scorer": "tools/vnext_wp3_eval.py", "v1_scorer_sha256": sha256_file(Path(ev.__file__)),
        "mapping": {"project_legibility": LEGIB_MAP,
                    "camera_angle": f"gold primary in {list(CAMERA_UNKNOWN_OK_FOR)} -> predicted 'unknown' accepted"},
        "gold_mapping_stats": gstats,
        "inputs": {"gold_sha256": shas["gold_labels_final_v2.json"], "sha256": shas},
        "summary_line": summary,
        "engines": engines,
        "parity_all_ok": all(e["parity_v13"]["match"] for e in engines.values()),
        "paid_calls": 0, "network_requests": 0,
    }
    md = render_md(rep)
    if not a.no_write:
        (EXP / "rescore_v14.json").write_text(json.dumps(rep, indent=1, ensure_ascii=False), encoding="utf-8")
        (EXP / "rescore_v14.md").write_text(md, encoding="utf-8")
    print(summary)
    print(json.dumps({"parity_all_ok": rep["parity_all_ok"], "gold_mapping": gstats,
                      "contract_sha256": rep["contract"]["sha256"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
