#!/usr/bin/env python3
"""Hybrid gate: Haiku-confirm SigLIP cover re-pick proposals (2026-06-28).

SigLIP proposes (free, ~78% reliable); Haiku confirms each current-vs-proposed pair
("is the proposed a better representative-exterior cover?"). Batched + streaming
(download, judge, delete). Produces the user-approvable confirmed re-pick list.
LOCAL only — applying to Neon (`display_cover_url`, reversible) stays user-gated.

Usage:
  python3 tools/cover_repick_confirm.py --proposals data/reports/cover_audit/repick_chunk1k/proposals.jsonl \
      --batch 4 --out data/reports/cover_audit/repick_chunk1k/confirmed.jsonl
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.image_dedup_5type import _download_to_tmp  # noqa: E402

PROMPT = (
    "You confirm building-cover replacements for an architecture swipe app. For each "
    "PAIR, image A is the CURRENT cover and image B is a PROPOSED replacement. Decide: "
    "is B a BETTER representative cover — does it show the building's overall EXTERIOR "
    "form more clearly than A (not an interior, detail, render, or non-building)? "
    "Output ONLY JSON, one key per pair label, value "
    '{"better": true|false, "why": "<=6 words"}. No prose.'
)


def _extract(t):
    m = re.search(r"\{.*\}", t, re.S)
    try:
        return json.loads(m.group(0)) if m else {}
    except Exception:
        return {}


def confirm_batch(pairs, model, timeout):
    paths, cleanup, labels = {}, [], {}
    for k, p in enumerate(pairs, 1):
        lab = f"P{k}"
        ok = True
        for side, url in (("A", p["current_cover"]), ("B", p["proposed_cover"])):
            try:
                tmp, dl = _download_to_tmp(url)
                paths[f"{lab}_{side}"] = Path(tmp)
                if dl:
                    cleanup.append(Path(tmp))
            except Exception:
                ok = False
        labels[lab] = (p["canonical_bld_id"], ok)
    listing = "\n".join(f"{k} = {v}" for k, v in paths.items())
    add_dirs = {str(v.parent) for v in paths.values()}
    cmd = ["claude", "-p", f"{PROMPT}\n\nImage files:\n{listing}",
           "--output-format", "json", "--model", model]
    for d in add_dirs:
        cmd += ["--add-dir", d]
    cost, verd = 0.0, {}
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        env = json.loads(proc.stdout)
        cost = env.get("total_cost_usd") or 0.0
        verd = _extract(env.get("result", ""))
    except Exception:
        pass
    finally:
        for p in cleanup:
            p.unlink(missing_ok=True)
    out = []
    for lab, (bid, ok) in labels.items():
        v = verd.get(lab) or {}
        out.append({"canonical_bld_id": bid,
                    "better": bool(v.get("better")) if ok else None,
                    "why": v.get("why")})
    return out, cost


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proposals", type=Path, required=True)
    ap.add_argument("--batch", type=int, default=4)
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    props = [json.loads(l) for l in args.proposals.read_text().split("\n") if l]
    props = [p for p in props if p.get("improved") and p.get("proposed_cover")]
    pmap = {p["canonical_bld_id"]: p for p in props}

    confirmed, cost, yes = [], 0.0, 0
    with open(args.out, "w", encoding="utf-8") as f:
        for s in range(0, len(props), args.batch):
            res, c = confirm_batch(props[s:s + args.batch], args.model, args.timeout)
            cost += c
            for r in res:
                p = pmap[r["canonical_bld_id"]]
                rec = {**r, "current_cover": p["current_cover"],
                       "proposed_cover": p["proposed_cover"],
                       "current_ext_score": p.get("current_ext_score"),
                       "proposed_ext_score": p.get("proposed_ext_score")}
                if r["better"]:
                    yes += 1
                    confirmed.append(rec)
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(f"  {min(s+args.batch,len(props))}/{len(props)}  confirmed={yes}  "
                  f"${cost:.2f}", flush=True)

    summ = {"proposals": len(props), "confirmed_better": yes,
            "confirm_rate": round(yes / len(props), 3) if props else None,
            "rejected": len(props) - yes, "cost_usd": round(cost, 3), "model": args.model}
    Path(str(args.out) + ".summary.json").write_text(json.dumps(summ, indent=2))
    print("\nSUMMARY:", json.dumps(summ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
