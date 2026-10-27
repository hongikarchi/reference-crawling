#!/usr/bin/env python3
"""WP3 exp v2 step 1: re-score frozen config v1 local labels on all 225 gold images (offline, $0).

Uses tools/vnext_wp3_eval.py (unchanged; imported) against gold_labels_final_v2.json and
writes ONLY data/smoke_vnext/wp3_n10_v1/exp_v2/baseline_v1_225_eval_report.{json,md}.
The v1 eval_report.* files are not touched.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import vnext_wp3_eval as ev  # noqa: E402

WP = ROOT / "data/smoke_vnext/wp3_n10_v1"
OUT = WP / "exp_v2"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    files = {n: WP / n for n in ("local_labels.jsonl", "local_run.json", "gold_labels_final_v2.json",
                                  "judge_decisions.jsonl", "judge_decisions_chunk4.jsonl",
                                  "sample_manifest.json", "fetch_log.jsonl")}
    shas = {n: ev.sha256_file(p) for n, p in files.items()}
    local_recs = [json.loads(x) for x in files["local_labels.jsonl"].read_text(encoding="utf-8").splitlines() if x.strip()]
    local_run = json.loads(files["local_run.json"].read_text(encoding="utf-8"))
    gold = json.loads(files["gold_labels_final_v2.json"].read_text(encoding="utf-8"))
    fetch_log = [json.loads(x) for x in files["fetch_log.jsonl"].read_text(encoding="utf-8").splitlines() if x.strip()]
    decisions = []
    for n in ("judge_decisions.jsonl", "judge_decisions_chunk4.jsonl"):
        decisions += [json.loads(x) for x in files[n].read_text(encoding="utf-8").splitlines() if x.strip()]
    gi = gold.get("inputs", {})
    integrity = {
        "gold_local_labels_sha_match": gi.get("local_labels_sha256") == shas["local_labels.jsonl"],
        "gold_local_run_sha_match": gi.get("local_run_sha256") == shas["local_run.json"],
        "local_config_sha_consistent": len({r.get("config_sha256") for r in local_recs}) == 1,
    }
    e = ev.evaluate(local_recs, gold, local_run)
    e["routing"]["caveat"] = e["routing"]["caveat"].replace("173 images / 8 buildings", "225 images / 10 buildings")
    il = e["routing"]["image_level"]
    fracs = {
        "image_level_x95_pooled": (il["x_for_pooled_95"]["routed_pct"] / 100) if il["x_for_pooled_95"] else None,
        "union_per_field_x95": il["union_of_per_field_x95_routes"]["routed_pct"] / 100,
        "union_x95_local_viable_fields_only": il["union_of_x95_routes_local_viable_fields"]["routed_pct"] / 100,
    }
    ext = ev.extrapolate(local_run, fetch_log, files["local_labels.jsonl"].stat().st_size, len(local_recs), fracs)
    rep = {
        "eval_version": "vnext-wp3-n10-eval-v1 (re-run on gold v2 by vnext-wp3-exp-v2-baseline)",
        "tool": "tools/vnext_wp3_exp_v2_baseline.py", "tool_sha256": ev.sha256_file(Path(__file__)),
        "eval_module": "tools/vnext_wp3_eval.py", "eval_module_sha256": ev.sha256_file(ROOT / "tools/vnext_wp3_eval.py"),
        "inputs": {
            "engine": local_recs[0].get("engine"), "config_version": local_recs[0].get("config_version"),
            "config_sha256": local_recs[0].get("config_sha256"), "sha256": shas, "integrity": integrity,
            "judge_decisions": {"n": len(decisions), "by_decision": dict(Counter(d["decision"] for d in decisions))},
        },
        "gates": {"lane_a": e["lane_a"]["pass"], "lane_b": e["lane_b"]["gate"]["pass"]},
        "eval": e, "extrapolation": ext, "paid_calls": 0, "network_requests": 0,
    }
    md = ev.render_md(rep)
    md = md.replace("# vNext WP3 N10 local-engine eval (`vnext-wp3-n10-eval-v1`)",
                    "# vNext WP3 N10 local-engine eval - config v1 baseline on gold v2 (225 images / 10 buildings)")
    md = md.replace(" (unscored 0: chunk_4 gold missing)", " (unscored 0)")
    (OUT / "baseline_v1_225_eval_report.json").write_text(json.dumps(rep, indent=1, ensure_ascii=False), encoding="utf-8")
    (OUT / "baseline_v1_225_eval_report.md").write_text(md, encoding="utf-8")
    la, lb = e["lane_a"], e["lane_b"]["gate"]
    print(json.dumps({"integrity": integrity, "scored": e["scored_images"], "pooled": la["pooled_9field_acceptance"],
                      "scope_err": la["scope_obvious_errors"], "medium_err": la["medium_obvious_errors"],
                      "per_field": {f: e["per_field"][f]["acceptance"] for f in ev.FIELDS},
                      "lane_b": {k: lb[k] for k in ("critical_cover_errors", "top1_in_acceptable", "top3_contains_acceptable")}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
