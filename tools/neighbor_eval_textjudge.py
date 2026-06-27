#!/usr/bin/env python3
"""Confound test: TEXT-ONLY taste-coherence judge (2026-06-25).

The Opus VISION judge shares its modality with how the visual spaces were SELECTED
(vision picks neighbors, vision LLM grades the same pixels) → visual spaces get a
"home game". This judge sees NO images — only each building's text descriptors
(program/style/atmosphere/material/typology/color/era/country, the very fields the
TEXT embedding was built from). It is therefore biased TOWARD text. Re-score the
SAME queues with it:
  - visual still wins/ties  -> robust, modality home-advantage isn't the driver.
  - visual collapses        -> the vision-judge win was largely home-advantage.

Reuses the existing queues; no image downloads; runs on Sonnet (cheap). Same blind
shuffled labels, same precision@5 metric, comparable to the vision-judge numbers.

Usage:
  python3 tools/neighbor_eval_textjudge.py --queue .../Q_siglip.jsonl --out V.jsonl
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SRC_META = ROOT / "data/reports/neighbor_eval/meta.jsonl"

DESC_FIELDS = ("program", "style", "atmosphere", "material_visual",
               "typology_primary", "color_tone", "location_country", "project_year")

PROMPT = (
    "You are judging building-recommendation quality from TEXT DESCRIPTORS ONLY "
    "(no images). A person LIKED the SEED building. For EACH candidate, decide: would "
    "that person PLAUSIBLY ALSO LIKE it, judging overall architectural character from "
    "these descriptors? Do NOT require the same building type.\n"
    "Output ONLY a JSON object, one key per candidate label, value "
    '{"like": true|false, "conf": 0.0-1.0, "why": "<=8 words"}. No prose.'
)


def _card(label: str, m: dict) -> str:
    bits = [f"name={m.get('name')}"]
    for k in DESC_FIELDS:
        v = m.get(k)
        if isinstance(v, list):
            v = ", ".join(map(str, v))
        if v:
            bits.append(f"{k}={v}")
    return f"{label}: " + "; ".join(bits)


def _extract_json(text: str):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    desc = {m["canonical_bld_id"]: m
            for m in (json.loads(l) for l in SRC_META.read_text().split("\n") if l)}
    items = [json.loads(l) for l in Path(args.queue).read_text().split("\n") if l]
    if args.limit:
        items = items[:args.limit]

    results, total_cost = [], 0.0
    with open(args.out, "w", encoding="utf-8") as f:
        for n, it in enumerate(items, 1):
            seed_m = desc.get(it["seed_id"], {"name": it.get("seed_name")})
            lines = ["SEED " + _card("building", seed_m), ""]
            labels = []
            for c in it["candidates"]:
                cm = desc.get(c["id"])
                if not cm:
                    continue
                lines.append(_card(f"Candidate {c['label']}", cm))
                labels.append(c["label"])
            prompt = PROMPT + "\n\n" + "\n".join(lines)
            try:
                proc = subprocess.run(
                    ["claude", "-p", prompt, "--output-format", "json", "--model", args.model],
                    capture_output=True, text=True, timeout=args.timeout)
                env = json.loads(proc.stdout)
                verd = _extract_json(env.get("result", ""))
                cost = env.get("total_cost_usd") or 0.0
            except Exception as e:
                r = {"seed_id": it["seed_id"], "error": str(e)[:120]}
                results.append(r); f.write(json.dumps(r) + "\n"); continue
            likes = [bool(verd.get(lab, {}).get("like")) for lab in labels]
            prec = sum(likes) / len(likes) if likes else None
            r = {"seed_id": it["seed_id"], "seed_program": it.get("seed_program"),
                 "precision_at_k": prec, "cost_usd": cost,
                 "true_rank_by_label": {c["label"]: c["true_rank"] for c in it["candidates"]},
                 "verdicts": {lab: verd.get(lab) for lab in labels}}
            results.append(r); f.write(json.dumps(r, ensure_ascii=False) + "\n")
            total_cost += cost
            print(f"[{n}/{len(items)}] {it['seed_id']} prec={prec if prec is None else round(prec,2)} "
                  f"cost=${cost:.3f}", flush=True)

    ok = [r["precision_at_k"] for r in results if r.get("precision_at_k") is not None]
    summ = {"seeds": len(items), "judged_ok": len(ok),
            "mean_precision_at_k": round(float(np.mean(ok)), 4) if ok else None,
            "total_cost_usd": round(total_cost, 4)}
    Path(str(args.out) + ".summary.json").write_text(json.dumps(summ, indent=2))
    print("\nSUMMARY:", json.dumps(summ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
