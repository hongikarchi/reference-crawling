#!/usr/bin/env python3
"""Phase-3 cover-image quality/coverage audit (2026-06-27).

A swipe app IS its photos — a non-representative cover hurts the card regardless of
embedding. Classify each publishable building's cover into a controlled vocabulary
via Haiku vision (cheap; batched N-per-call to amortize CLI framework overhead).
Judge-independent — appearance category is objective, no modality confound.

Sample for the RATE first (full census deferred to remediation). Outputs per-category
rates + per-program breakdown + a remediation candidate list (covers that don't
represent the building well for a swipe card).

Usage:
  python3 tools/cover_quality_audit.py --n 10  --batch 5 --out data/reports/cover_audit/smoke
  python3 tools/cover_quality_audit.py --n 150 --batch 8 --out data/reports/cover_audit/s150
"""
from __future__ import annotations

import argparse
import json
import random
import re
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.claude_cli import CLAUDE_BIN  # noqa: E402
from tools.image_dedup_5type import _download_to_tmp  # noqa: E402

SRC_META = ROOT / "data/reports/neighbor_eval/meta.jsonl"  # 36,673 publishable w/ cover

CATS = ["representative_exterior", "detail_closeup", "interior_only",
        "render_or_drawing", "aerial_or_siteplan", "people_or_object_dominant",
        "poor_quality", "not_a_building"]
GOOD = {"representative_exterior", "aerial_or_siteplan"}  # acceptable swipe covers

PROMPT = (
    "You audit building-photo covers for an architecture swipe app. Read EACH image "
    "file listed below and classify its cover into EXACTLY ONE category:\n"
    "- representative_exterior: clear shot of the building's overall exterior form (IDEAL cover)\n"
    "- detail_closeup: zoomed detail/material/fragment, not the whole building\n"
    "- interior_only: interior, no exterior visible\n"
    "- render_or_drawing: CGI/render/drawing/diagram/plan, not a real photo\n"
    "- aerial_or_siteplan: aerial or site overview where the building is legible\n"
    "- people_or_object_dominant: people/objects dominate, building incidental\n"
    "- poor_quality: blurry, dark, low-res, or badly obstructed\n"
    "- not_a_building: no building (logo, portrait, landscape, text card)\n"
    "Output ONLY a JSON object, one key per image label, value "
    '{"category": "<one of the above>", "why": "<=6 words"}. No prose.'
)


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


def classify_batch(rows: list[dict], model: str, timeout: int) -> tuple[dict, float]:
    paths, cleanup, labels = {}, [], {}
    for k, r in enumerate(rows, 1):
        lab = f"IMG{k}"
        try:
            p, dl = _download_to_tmp(r["display_cover_url"])
            paths[lab] = Path(p)
            labels[lab] = r["canonical_bld_id"]
            if dl:
                cleanup.append(Path(p))
        except Exception:
            labels[lab] = r["canonical_bld_id"]  # mark unreachable below
    listing = "\n".join(f"{lab} = {paths[lab]}" for lab in paths)
    add_dirs = {str(p.parent) for p in paths.values()}
    cmd = [CLAUDE_BIN, "-p", f"{PROMPT}\n\nImage files:\n{listing}",
           "--output-format", "json", "--model", model]
    for d in add_dirs:
        cmd += ["--add-dir", d]
    cost = 0.0
    verd = {}
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        env = json.loads(proc.stdout)
        cost = env.get("total_cost_usd") or 0.0
        verd = _extract_json(env.get("result", ""))
    except Exception:
        pass
    finally:
        for p in cleanup:
            try:
                p.unlink()
            except Exception:
                pass
    out = {}
    for lab, bid in labels.items():
        if lab not in paths:
            out[bid] = {"category": "missing_or_broken", "why": "download failed"}
        else:
            v = verd.get(lab) or {}
            cat = v.get("category")
            out[bid] = {"category": cat if cat in CATS else "unparsed",
                        "why": v.get("why")}
    return out, cost


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--seed", type=int, default=20260627)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    meta = [json.loads(l) for l in SRC_META.read_text().split("\n") if l]
    pool = [m for m in meta if m.get("display_cover_url")]
    random.Random(args.seed).shuffle(pool)
    sample = pool[:args.n]
    by_id = {m["canonical_bld_id"]: m for m in sample}

    args.out.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict] = {}
    total_cost = 0.0
    rec_path = args.out / "classified.jsonl"
    with open(rec_path, "w", encoding="utf-8") as f:
        for s in range(0, len(sample), args.batch):
            chunk = sample[s:s + args.batch]
            res, cost = classify_batch(chunk, args.model, args.timeout)
            total_cost += cost
            for bid, v in res.items():
                v["program"] = by_id[bid].get("program")
                results[bid] = v
                f.write(json.dumps({"canonical_bld_id": bid, **v}, ensure_ascii=False) + "\n")
            done = min(s + args.batch, len(sample))
            print(f"  {done}/{len(sample)}  (${total_cost:.3f})", flush=True)

    cats = Counter(v["category"] for v in results.values())
    n = len(results)
    good = sum(cats[c] for c in GOOD)
    prog = defaultdict(Counter)
    for v in results.values():
        prog[v.get("program")][v["category"]] += 1

    report = {
        "n": n, "model": args.model, "total_cost_usd": round(total_cost, 4),
        "good_cover_rate": round(good / n, 3) if n else None,
        "category_rates": {c: round(cats[c] / n, 3) for c in cats},
        "category_counts": dict(cats),
        "remediation_count": n - good,
    }
    (args.out / "report.json").write_text(json.dumps(report, indent=2))
    # remediation candidates = non-good covers
    with open(args.out / "remediation.jsonl", "w", encoding="utf-8") as f:
        for bid, v in results.items():
            if v["category"] not in GOOD:
                f.write(json.dumps({"canonical_bld_id": bid, **v}, ensure_ascii=False) + "\n")
    print("\nREPORT:", json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
