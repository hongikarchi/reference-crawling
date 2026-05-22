#!/usr/bin/env python3
"""L3 audit — aggregate sub-agent verdicts into accuracy estimates.

Reads every data/reports/audit/l3_verdicts/*.jsonl and the sample manifest,
computes per-field accuracy with Wilson 95% CIs per stratum, plus a
population-projected (stratified) estimate. Tolerates partial / missing shards.

Writes data/reports/audit/L3_aggregate.json.
"""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "data/reports/audit/L3_sample_manifest.json"
VERDICT_DIR = ROOT / "data/reports/audit/l3_verdicts"
REPORT = ROOT / "data/reports/audit/L3_aggregate.json"
TOTAL_POP = 39776

PROTO_A = ["name", "location_country", "location_city", "project_year",
           "architect_names", "program", "style", "color_tone", "atmosphere",
           "material_visual", "visual_description"]
PROTO_B = ["cover_is_building", "image_derived_style", "image_derived_color_tone",
           "image_derived_material_visual", "image_derived_visual_description"]


def wilson(pass_n: int, total: int, z: float = 1.96) -> list[float]:
    if total == 0:
        return [0.0, 0.0]
    p = pass_n / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return [round(max(0.0, center - half), 4), round(min(1.0, center + half), 4)]


def main() -> int:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    strata_pop = {k: v["population"] for k, v in manifest["strata"].items()}

    verdicts: dict[str, dict] = {}
    files = sorted(VERDICT_DIR.glob("*.jsonl"))
    parse_errors = 0
    for fp in files:
        for line in fp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                v = json.loads(line)
            except json.JSONDecodeError:
                parse_errors += 1
                continue
            cid = v.get("canonical_bld_id")
            if cid:
                verdicts[cid] = v

    # Protocol A — per field, per stratum
    a_counts: dict[str, dict[str, dict[str, int]]] = {
        f: defaultdict(lambda: {"PASS": 0, "FAIL": 0, "INSUFFICIENT_INFO": 0})
        for f in PROTO_A}
    b_counts: dict[str, dict[str, dict[str, int]]] = {
        f: defaultdict(lambda: {"MATCH": 0, "MISMATCH": 0, "PARTIAL": 0, "NA": 0})
        for f in PROTO_B}
    same_b = {"SAME": 0, "DIFFERENT": 0, "UNCERTAIN": 0, "NA": 0}
    img_unavailable = 0
    n = 0
    for v in verdicts.values():
        n += 1
        st = v.get("stratum", "?")
        pa = v.get("protocol_a", {})
        for f in PROTO_A:
            verdict = pa.get(f)
            if verdict in ("PASS", "FAIL", "INSUFFICIENT_INFO"):
                a_counts[f][st][verdict] += 1
        pb = v.get("protocol_b", {})
        if pb.get("image_status") == "IMAGE_UNAVAILABLE":
            img_unavailable += 1
        for f in PROTO_B:
            verdict = pb.get(f)
            if verdict in ("MATCH", "MISMATCH", "PARTIAL", "NA"):
                b_counts[f][st][verdict] += 1
        sb = v.get("same_building", "NA")
        same_b[sb] = same_b.get(sb, 0) + 1

    # Protocol A — per field results
    proto_a_result = {}
    for f in PROTO_A:
        per_stratum = {}
        proj_acc_num = 0.0
        proj_var = 0.0
        pooled_pass = pooled_fail = pooled_insuff = 0
        for st, pop in strata_pop.items():
            c = a_counts[f].get(st, {"PASS": 0, "FAIL": 0, "INSUFFICIENT_INFO": 0})
            judged = c["PASS"] + c["FAIL"]
            acc = c["PASS"] / judged if judged else None
            per_stratum[st] = {
                "pass": c["PASS"], "fail": c["FAIL"], "insufficient": c["INSUFFICIENT_INFO"],
                "accuracy": round(acc, 4) if acc is not None else None,
                "wilson_ci": wilson(c["PASS"], judged) if judged else None,
            }
            pooled_pass += c["PASS"]
            pooled_fail += c["FAIL"]
            pooled_insuff += c["INSUFFICIENT_INFO"]
            if acc is not None and judged:
                w = pop / TOTAL_POP
                proj_acc_num += w * acc
                proj_var += w * w * acc * (1 - acc) / judged
        pooled_judged = pooled_pass + pooled_fail
        se = math.sqrt(proj_var) if proj_var > 0 else 0.0
        proto_a_result[f] = {
            "pooled": {"pass": pooled_pass, "fail": pooled_fail,
                       "insufficient": pooled_insuff,
                       "accuracy": round(pooled_pass / pooled_judged, 4) if pooled_judged else None,
                       "wilson_ci": wilson(pooled_pass, pooled_judged) if pooled_judged else None},
            "population_projected_accuracy": round(proj_acc_num, 4),
            "population_projected_ci": [round(max(0.0, proj_acc_num - 1.96 * se), 4),
                                        round(min(1.0, proj_acc_num + 1.96 * se), 4)],
            "per_stratum": per_stratum,
        }

    # Protocol B — per field results
    proto_b_result = {}
    for f in PROTO_B:
        pooled = {"MATCH": 0, "MISMATCH": 0, "PARTIAL": 0, "NA": 0}
        per_stratum = {}
        for st in strata_pop:
            c = b_counts[f].get(st, {"MATCH": 0, "MISMATCH": 0, "PARTIAL": 0, "NA": 0})
            for k in pooled:
                pooled[k] += c[k]
            judged = c["MATCH"] + c["MISMATCH"] + c["PARTIAL"]
            per_stratum[st] = {**c,
                               "match_rate": round(c["MATCH"] / judged, 4) if judged else None}
        judged = pooled["MATCH"] + pooled["MISMATCH"] + pooled["PARTIAL"]
        proto_b_result[f] = {
            "pooled": pooled,
            "match_rate": round(pooled["MATCH"] / judged, 4) if judged else None,
            "ok_rate_match_or_partial": round((pooled["MATCH"] + pooled["PARTIAL"]) / judged, 4)
            if judged else None,
            "wilson_ci_match": wilson(pooled["MATCH"], judged) if judged else None,
            "per_stratum": per_stratum,
        }

    # collect all FAIL/MISMATCH reasons for the issue list
    issues = []
    for cid, v in verdicts.items():
        reasons = v.get("fail_reasons") or []
        if reasons:
            issues.append({"canonical_bld_id": cid, "stratum": v.get("stratum"),
                           "reasons": reasons})

    report = {
        "layer": "L3",
        "verdict_files": [f.name for f in files],
        "buildings_judged": n,
        "sample_target": manifest["total_sampled"],
        "coverage": round(n / manifest["total_sampled"], 4) if manifest["total_sampled"] else 0,
        "json_parse_errors": parse_errors,
        "images_unavailable": img_unavailable,
        "protocol_a_text_fidelity": proto_a_result,
        "protocol_b_image_accuracy": proto_b_result,
        "same_building_check": same_b,
        "issue_count": len(issues),
        "issues": issues,
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = {
        "buildings_judged": n, "coverage": report["coverage"],
        "protocol_a_population_projected": {
            f: {"accuracy": proto_a_result[f]["population_projected_accuracy"],
                "ci": proto_a_result[f]["population_projected_ci"],
                "pooled_fail": proto_a_result[f]["pooled"]["fail"]}
            for f in PROTO_A},
        "protocol_b_match_rate": {f: proto_b_result[f]["match_rate"] for f in PROTO_B},
        "same_building": same_b,
        "issue_count": len(issues),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"report: {REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
