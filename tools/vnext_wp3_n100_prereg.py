#!/usr/bin/env python3
"""vNext WP3 N100 pre-registration writer (run BEFORE any holdout fetch / prediction).

Writes data/smoke_vnext/wp3_n100_v1/preregistration.json and preregistration.sha256.
Refuses to run if any holdout artefact (images/, fetch_log.jsonl, local_pred.jsonl, local_covers.jsonl)
already exists, or if preregistration.json exists with different content (no silent re-registration).
Inputs read-only: probe_v1 manifest + pickles, eval_probe_v1.json (pool-OOF routing thresholds),
calib/cover_calib_report.json (pool + eval-225 cover calibration, no holdout), codex engine frozen SHAs.
No network, no model calls, no Neon/R2.

Usage: .venv/Scripts/python.exe tools/vnext_wp3_n100_prereg.py --cover-variant s5 --A 0.xx --M 0.yy
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import vnext_s3_codex_engine as cxe  # noqa: E402

OUT = ROOT / "data/smoke_vnext/wp3_n100_v1"
POOL = ROOT / "data/smoke_vnext/wp3_pool_v1"
WP = ROOT / "data/smoke_vnext/wp3_n10_v1"
EXP2 = WP / "exp_v2"
PREREG = OUT / "preregistration.json"
PREREG_SHA = OUT / "preregistration.sha256"
HOLDOUT = POOL / "holdout_n100_manifest.json"


def sha(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def rel(p: Path) -> str:
    return str(Path(p).relative_to(ROOT)).replace("\\", "/")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cover-variant", required=True, choices=["s5", "s4"])
    ap.add_argument("--A", type=float, required=True)
    ap.add_argument("--M", type=float, required=True)
    ap.add_argument("--cover-rationale", required=True)
    a = ap.parse_args(argv)
    for p in ("images", "fetch_log.jsonl", "local_pred.jsonl", "local_covers.jsonl"):
        assert not (OUT / p).exists(), f"holdout artefact {p} exists - pre-registration must come first"
    pm = json.loads((POOL / "probe_v1/manifest.json").read_text(encoding="utf-8"))
    ep = json.loads((POOL / "eval_probe_v1.json").read_text(encoding="utf-8"))
    prim = ep["variants"]["hybrid_emb_zs2"]["scope"]
    thr = prim["pool_oof_routing_thresholds"]["scope_medium"]
    rt = prim["routing"]["scope_medium"]["pool_oof_thresholds"]
    cal = json.loads((OUT / "calib/cover_calib_report.json").read_text(encoding="utf-8"))
    cv = cal["variants"][a.cover_variant]
    g = next(x for x in cv["grid"] if x["A"] == a.A and x["M"] == a.M)
    probs_meta = json.loads((OUT / "calib/probs_meta.json").read_text(encoding="utf-8"))
    calib_bld = json.loads((OUT / "calib/cover_calib_buildings.json").read_text(encoding="utf-8"))
    cover_run = json.loads((OUT / "calib/codex_cover_pool/run.json").read_text(encoding="utf-8"))
    ledger = json.loads((OUT / "ledger_wp3_n100.json").read_text(encoding="utf-8"))
    hm = json.loads(HOLDOUT.read_text(encoding="utf-8"))
    tools = ["tools/vnext_wp3_probe_v1.py", "tools/vnext_s3_local_engine.py", "tools/vnext_s3_local_engine_config_v1.json",
             "tools/vnext_s3_local_engine_config_v2.json", "tools/vnext_wp3_n100_cover_calib.py", "tools/vnext_wp3_n100_local.py",
             "tools/vnext_wp3_n100_fetch.py", "tools/vnext_wp3_pool_sample.py", "tools/vnext_s3_codex_engine.py",
             "tools/vnext_wp3_eval.py", "tools/vnext_wp3_eval_v14.py", "tools/vnext_wp3_n100_prereg.py"]
    doc = {
        "version": "vnext-wp3-n100-prereg-v1",
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "status": "registered before any holdout image fetch, holdout prediction, holdout codex call or holdout judging",
        "user_decisions_2026_10_28": [
            "N100 gold = codex labels + Claude judge on probe-vs-codex disagreements (Claude views only those images + a small random agreement spot check)",
            "covers: codex comparative call used in the funnel only for buildings whose local pick is not confident",
        ],
        "contracts": {"docs/vnext/CONTRACT_GALLERY_SEMANTICS.md": sha(ROOT / "docs/vnext/CONTRACT_GALLERY_SEMANTICS.md"),
                      "docs/vnext/CONTRACT_COVERS.md": sha(ROOT / "docs/vnext/CONTRACT_COVERS.md"),
                      "contract_version": "vnext-gallery-semantics-contract-v1.4"},
        "holdout": {
            "manifest": rel(HOLDOUT), "manifest_sha256": sha(HOLDOUT), "buildings": len(hm["buildings"]),
            "images": sum(len(b["images"]) for b in hm["buildings"]), "summary": hm["summary"],
            "idx_rule": "idx = 0-based position in manifest order (buildings as listed, images as listed within each building)",
            "fetch": "once, <= 4 rps, fetch profile vnext-wp3-fetch-v1 (tools/vnext_wp3_n100_fetch.py), failures explicit rows, "
                     "retryable failure retried once, no post-hoc replacement; failed images are excluded from scoring and reported",
            "disjointness": "holdout buildings excluded from the pool and from the N10 eval by construction (tools/vnext_wp3_pool_sample.py rule 0)",
        },
        "engine": {
            "name": "probe v1 primary hybrid_emb_zs2 (scope head mode 'scope')",
            "pickle": "data/smoke_vnext/wp3_pool_v1/probe_v1/hybrid_emb_zs2.pkl",
            "pickle_sha256": sha(POOL / "probe_v1/hybrid_emb_zs2.pkl"),
            "probe_manifest": "data/smoke_vnext/wp3_pool_v1/probe_v1/manifest.json",
            "probe_manifest_sha256": sha(POOL / "probe_v1/manifest.json"),
            "all_frozen_pickles_sha256": {k: v["sha256"] for k, v in pm["files"].items()},
            "pickles_on_disk_match_manifest": all(sha(POOL / "probe_v1" / k) == v["sha256"] for k, v in pm["files"].items()),
            "cam_t": prim["cam_t"], "C": pm["files"]["hybrid_emb_zs2.pkl"]["C"],
            "embedding": {"model": "google/siglip2-so400m-patch14-384", "snapshot": "e8e487298228002f3d8a82e0cd5c8ea9c567f57f",
                          "transform": "squash resize 384 bilinear, fp16 forward, L2 (engine v1 functions, == pool emb.npy)"},
            "features": "concat(SigLIP-2 emb 1152, config v2 zero-shot value logits) via tools/vnext_wp3_probe_v1.build_features",
            "cascade": "tools/vnext_wp3_probe_v1.cascade (contract §5 applicability after prediction)",
            "frozen_inputs_sha256": {rel(p): sha(p) for p in (
                POOL / "probe_v1/hybrid_emb_zs2.pkl", EXP2 / "text_emb.npy", EXP2 / "text_index.json", EXP2 / "emb_index.json",
                EXP2 / "emb.npy", ROOT / "tools/vnext_s3_local_engine_config_v1.json",
                ROOT / "tools/vnext_s3_local_engine_config_v2.json")},
            "eval225_reference": {"pooled_v14": prim["eval"]["pooled"], "scope_err": prim["eval"]["scope_obvious_errors"],
                                  "medium_err": prim["eval"]["medium_obvious_errors"],
                                  "cover_top1": prim["eval"]["cover_top1"], "cover_critical": prim["eval"]["cover_critical"]},
        },
        "label_routing": {
            "rule": "route an image to codex iff min(scope margin, medium margin) < T; scope margin = |2*P(in_scope)-1|, "
                    "medium margin = top1 - top2 of the normalised medium head probabilities (cascade scores)",
            "T_primary": 0.9347, "T_secondary": 0.9968,
            "funnel_label": "routed image -> codex label (v1.4), unrouted -> probe label",
            "evidence": {
                "source": "data/smoke_vnext/wp3_pool_v1/eval_probe_v1.json (pool 5-fold building OOF, probe vs codex scope/medium disagreements)",
                "pool_oof_n": thr["n_pool"], "pool_oof_disagreements": thr["n_disagree_scope_or_medium"],
                "T_primary_is": "catch_90 quantile", "T_primary_pool_route_pct": thr["catch_90"]["pool_route_pct"],
                "T_secondary_is": "catch_all (max)", "T_secondary_pool_route_pct": thr["catch_all"]["pool_route_pct"],
                "eval225_T_primary": rt["catch_90"], "eval225_T_secondary": rt["catch_all"]},
        },
        "cover_rule": {
            "score_variant": a.cover_variant,
            "score_definition": {"s5": "P_in_scope * P_photograph * P_exterior * P_overall * P_visibly_finished",
                                 "s4": "P_in_scope * P_photograph * P_exterior * P_overall"}[a.cover_variant],
            "eligibility": "hard probe tier < 3 (tools/vnext_wp3_probe_v1.tier: in_scope photograph exterior visibly_finished; "
                           "tier 1 overall non-aerial incl. camera unknown, tier 2 aerial overall or site_context)",
            "order": "(tier, -score, idx); local default = #1, local top-3 = first 3; no eligible image -> default NULL (review_required_local)",
            "confidence": "c1 = score of #1, margin = c1 - score of #2 (0 if only one eligible)",
            "A_top1_min": a.A, "M_margin_min": a.M,
            "secondary_report_only": {"A_top1_min": 0.95, "M_margin_min": 0.0,
                                      "use": "conservative what-if reported next to the primary; does not change the funnel or gates"},
            "local_confident": "default not NULL AND c1 >= A AND margin >= M",
            "funnel": "local_confident -> local default (no codex cover call in the funnel); else codex comparative cover call "
                      "(ranked[0], empty ranked -> NULL/review_required)",
            "rationale": a.cover_rationale,
            "evidence": {
                "calibration_sets": cal["per_set"],
                "eval225": "N10 Claude gold acceptable sets (10 buildings), frozen probe applied to exp_v2/emb.npy (0 label mismatch vs frozen pred)",
                "pool": f"{len(calib_bld['buildings'])} pool random-part buildings ({calib_bld['rule']}), probe 5-fold building OOF probabilities "
                        "(frozen C), reference = codex comparative cover acceptable set, critical via pool codex image labels "
                        "(COVERS §3: out_of_scope / non-photo / interior / element|material detail)",
                "pool_cover_codex_tokens": cover_run["cover"]["ledger_this_invocation"], "pool_cover_codex_calls": cover_run["cover"]["calls"],
                "secondary_report_point_not_gating": next(x for x in cv["grid"] if x["A"] == 0.95 and x["M"] == 0.0),
                "unconditional_local_pick": cv["unconditional"],
                "max_c1_critical": cv["max_c1_critical"], "max_c1_miss": cv["max_c1_miss"],
                "chosen_point": g,
                "bad_rows": cv["bad_rows"],
                "files": {rel(OUT / "calib" / f): sha(OUT / "calib" / f) for f in (
                    "cover_calib_report.json", "cover_calib_buildings.json", "probs_eval225.jsonl", "probs_pool_oof.jsonl",
                    "probs_meta.json", "codex_cover_pool/cover.jsonl")},
                "probs_meta": {k: probs_meta[k] for k in ("pickle_sha256", "eval_label_mismatch_vs_frozen_pred", "pool_oof_n")},
            },
        },
        "codex": {
            "tool": "tools/vnext_s3_codex_engine.py", "tool_sha256": sha(ROOT / "tools/vnext_s3_codex_engine.py"),
            "engine": cxe.ENGINE, "engine_version": cxe.ENGINE_VERSION, "model": cxe.DEFAULT_MODEL, "effort": cxe.DEFAULT_EFFORT,
            "batch_size": 8, "label_max_side": cxe.LABEL_MAX_SIDE, "cover_max_side": cxe.COVER_MAX_SIDE,
            "frozen_sha256": cxe.frozen_shas(),
            "calls_planned": {
                "label": "every fetched holdout image (gold source for all images; the funnel uses the label only where routed)",
                "cover": "comparative cover call for all 100 holdout buildings (gold source; the funnel uses it only for non-confident buildings)",
            },
            "token_cap_total_wp": 4_000_000, "ledger": rel(OUT / "ledger_wp3_n100.json"),
            "ledger_tokens_at_registration": ledger["total_tokens"],
            "stop_rules": ["mean tokens/image > 6,000 (engine guard)", "retry rate > 5% (engine guard)",
                           "ledger total + estimate > 4.0M cap", "weekly codex quota used > 70% (plan §4: remaining <= 30%) -> no new calls",
                           "input SHA drift -> stop", "critical cover error in the final funnel pick -> stop and report (plan §4)"],
            "token_estimate": "labels ~1,656 x 1,227 = ~2.03M; covers 100 x ~8.9k = ~0.9M; total incl. calibration ~3.5M",
        },
        "gold": {
            "labels": "codex v1.4 label per image = provisional gold; for every image sent to the judge the judge decision replaces it "
                      "(judge may also add acceptable alternatives); camera unknown accepted for eye_level/elevated gold (v1.4 scorer)",
            "covers": "acceptable set = codex comparative acceptable_idx, replaced by the judge's acceptable set for every judged building",
            "limitation": "routed images take the codex label in the funnel and codex is also the provisional gold -> funnel accuracy on routed "
                          "images is only tested where the judge reviews; reported separately",
        },
        "judge_protocol": {
            "judge": "Claude (this orchestration), views ONLY the images listed below",
            "image_set_labels": [
                "every image where probe != codex on any of the 9 core fields after the v1.4 mapping "
                "(camera unknown vs eye_level/elevated counts as agreement; legibility medium->high)",
                "random agreement spot check: 60 images with probe == codex on all 9 fields, ordered by "
                "sha256('vnext-wp3-n100-spot-v1|'+str(idx)) ascending, first 60",
            ],
            "label_judging": "per disagreeing field the judge sees the image and the two candidate values in an order fixed by "
                             "sha256('vnext-wp3-n100-judge-order-v1|'+idx+'|'+field) (engine identity hidden), and decides: "
                             "A correct / B correct / both acceptable / neither (judge value). Spot-check images: confirm or correct each field.",
            "building_set_covers": [
                "buildings where the local-confident pick is not in the codex acceptable set",
                "buildings where codex best (ranked[0]) is not in the local top-3",
                "any building whose final funnel pick has a critical codex image label",
            ],
            "cover_judging": "the judge sees all ok images of the building (opaque order) and writes the acceptable default set "
                             "(COVERS §1/§3; empty = NULL is correct)",
            "no_other_viewing": "no other holdout image is viewed by Claude",
        },
        "gates_n100": {
            "lane_A": {"criterion": "funnel (T_primary) pooled 9-field acceptance >= 95% vs N100 gold, contract v1.4 scorer "
                                    "(tools/vnext_wp3_eval_v14 mapping -> unchanged v1 scorer)",
                       "report": ["per-axis confusion matrix gold x funnel for every field", "per-field acceptance",
                                  "scope / medium obvious error counts", "T_secondary funnel, probe-only, codex-only (comparators)",
                                  "routed vs unrouted acceptance", "per stratum"]},
            "lane_B": {"criterion": "final funnel cover pick: top-1 in gold acceptable >= 95% of buildings (NULL correct iff gold set empty), "
                                    "top-3 >= 99% (top-3 = local top-3 for confident buildings, codex ranked for others), critical 0, "
                                    "no regression vs P0 baseline",
                       "p0_baseline": "legacy display_cover_url of the holdout manifest mapped to the holdout image with the identical URL; "
                                      "no regression = final top-1 rate >= baseline top-1 rate AND final critical <= baseline critical, "
                                      "over buildings where the baseline maps (unmapped buildings counted and reported)",
                       "report": ["local-confident subset top-1 / critical", "codex-only cover top-1", "per stratum"]},
            "independence": "lane A and lane B judged independently (plan §3)",
        },
        "tool_sha256": {t: sha(ROOT / t) for t in tools},
    }
    text = json.dumps(doc, indent=1, ensure_ascii=False)
    if PREREG.exists():
        assert PREREG.read_text(encoding="utf-8") == text, "preregistration.json exists with different content - refuse"
    PREREG.write_text(text, encoding="utf-8")
    h = sha(PREREG)
    PREREG_SHA.write_text(f"{h}  preregistration.json\n", encoding="utf-8")
    print(h)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
