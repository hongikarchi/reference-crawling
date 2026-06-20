#!/usr/bin/env python3
"""Build the adjudication queue from the compare output (pure Python, no LLM).

Per axis, sample up to --agree-max AGREE rows and --disagree-max DISAGREE/NEAR rows.
Adjudicating BOTH sets is required: agreement-rate is biased up by both-wrong-but-agree
(advisor catch). Each queue item carries the cover image path + a short source-text
snippet so the LLM adjudicator can judge against richer evidence than the bare label —
critical for typology (use-type) where the single cover under-determines the answer.

Output: jsonl, one item per {id, axis, set}. The adjudicator fills `verdict`.

Usage:
  python3 tools/accuracy_adjq.py --compare C.json --sample S.json \
      --manifest M.json --agree-max 70 --disagree-max 70 --out Q.jsonl
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

random.seed(77)
AXES = ("scale", "structural_system", "roof_type", "facade_pattern",
        "material_visual", "typology_primary")
VIS_KEY = {"material_visual": "materials", "typology_primary": "typology"}


def _evidence(srow: dict) -> str:
    ev = srow.get("_source_evidence") or {}
    bits = []
    for src, d in ev.items():
        for k in ("category", "categories", "building_type", "tag_slugs",
                  "materials", "abstract", "description", "description_short"):
            if d.get(k):
                bits.append(f"{src}.{k}: {d[k]}")
    return " | ".join(bits)[:900]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--compare", required=True)
    ap.add_argument("--sample", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--agree-max", type=int, default=70)
    ap.add_argument("--disagree-max", type=int, default=70)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    comp = json.loads(Path(args.compare).read_text())
    sample = {r["canonical_bld_id"]: r
              for r in json.loads(Path(args.sample).read_text())["rows"]}
    manifest = json.loads(Path(args.manifest).read_text())

    buckets = {a: {"agree": [], "disagree": []} for a in AXES}
    for row in comp["rows"]:
        bid = row["id"]
        srow = sample.get(bid, {})
        man = manifest.get(bid, {})
        path = man.get("path") if man.get("ok") else None
        for a in AXES:
            v = row["verdict"][a]
            grp = "agree" if v == "AGREE" else ("disagree" if v in ("DISAGREE", "NEAR") else None)
            if not grp or not path:
                continue
            vk = VIS_KEY.get(a, a)
            buckets[a][grp].append({
                "id": bid, "axis": a, "set": grp,
                "stored_val": row["stored"].get(a),
                "vision_val": row["vision"].get(vk),
                "img_path": path,
                "evidence": _evidence(srow),
                "name": srow.get("name"),
                "verdict": None,
            })

    items = []
    for a in AXES:
        ag = buckets[a]["agree"]; dg = buckets[a]["disagree"]
        random.shuffle(ag); random.shuffle(dg)
        items += ag[: args.agree_max] + dg[: args.disagree_max]

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with Path(args.out).open("w") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    summ = {a: {"agree_q": min(len(buckets[a]["agree"]), args.agree_max),
                "disagree_q": min(len(buckets[a]["disagree"]), args.disagree_max)}
            for a in AXES}
    summ["total_items"] = len(items)
    print(json.dumps(summ, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
