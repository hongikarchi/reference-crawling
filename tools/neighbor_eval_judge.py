#!/usr/bin/env python3
"""Phase-1 taste-coherence judge for the embedding-space quality eval (2026-06-25).

Two steps:
  build-queue  sample N seeds from the neighbor_eval sidecar, attach each seed's
               cosine top-K neighbors as BLIND labelled candidates (A..) in shuffled
               order (true rank stored for analysis, hidden from the judge).
  judge        per seed, download seed + candidate covers, ask Opus (vision, via the
               `claude` CLI — repo's working auth path) the TASTE-COHERENCE question:
               "would someone who liked the SEED plausibly also like this candidate,
               judging overall architectural character — NOT superficial appearance?"
               → precision@K + real cost from the CLI's total_cost_usd.

Judge sees ONLY images (no stored labels) → independent. Vector-source-agnostic:
`build-queue --embeddings <matrix.npy>` scores any candidate space the same way.

Usage:
  python3 tools/neighbor_eval_judge.py build-queue --n 10 --k 5 --out Q.jsonl
  python3 tools/neighbor_eval_judge.py judge --queue Q.jsonl --model opus --out V.jsonl
"""
from __future__ import annotations

import argparse
import json
import random
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.neighbor_eval import load, topk  # noqa: E402
from tools.image_dedup_5type import _download_to_tmp  # noqa: E402

LABELS = "ABCDEFGH"


# --------------------------------------------------------------------------- queue
def build_queue(args) -> int:
    mat, meta = load(args.dir)
    if args.embeddings:
        mat = np.load(args.embeddings)
        if mat.shape[0] != len(meta):
            raise SystemExit("override matrix row count != meta")

    rng = random.Random(args.seed)
    pool = [i for i, m in enumerate(meta) if m.get("display_cover_url")]
    rng.shuffle(pool)

    seeds, seen_prog = [], set()
    for i in pool:                       # prefer program diversity for a fair sample
        p = meta[i].get("program") or "?"
        if p in seen_prog and len(seen_prog) < args.n:
            continue
        seeds.append(i); seen_prog.add(p)
        if len(seeds) >= args.n:
            break
    for i in pool:                       # backfill if diversity ran short
        if len(seeds) >= args.n:
            break
        if i not in seeds:
            seeds.append(i)

    with open(args.out, "w", encoding="utf-8") as f:
        for s in seeds:
            nbrs = topk(mat, s, args.k)
            cands = [{"id": meta[j]["canonical_bld_id"],
                      "cover": meta[j].get("display_cover_url"),
                      "true_rank": r, "cos": round(c, 4),
                      "name": meta[j].get("name")}
                     for r, (j, c) in enumerate(nbrs, 1)]
            random.Random(args.seed + s).shuffle(cands)  # blind label order
            for lab, c in zip(LABELS, cands):
                c["label"] = lab
            f.write(json.dumps({
                "seed_id": meta[s]["canonical_bld_id"],
                "seed_name": meta[s].get("name"),
                "seed_program": meta[s].get("program"),
                "seed_cover": meta[s].get("display_cover_url"),
                "candidates": cands,
            }, ensure_ascii=False) + "\n")
    print(f"wrote {len(seeds)} seeds (k={args.k}) -> {args.out}")
    return 0


# --------------------------------------------------------------------------- judge
PROMPT = (
    "You are judging building-recommendation quality, blind (images only).\n"
    "A person LIKED the SEED building. For EACH candidate, decide: would that person "
    "PLAUSIBLY ALSO LIKE it — judging the building's overall ARCHITECTURAL CHARACTER "
    "(style, material, era, form, massing, mood). Do NOT reward mere visual/photographic "
    "similarity (camera angle, sky, crop) and do NOT require the same building type.\n"
    "Output ONLY a JSON object, one key per candidate label, value "
    '{"like": true|false, "conf": 0.0-1.0, "why": "<=8 words"}. No prose.'
)


def _extract_json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def run_judge_call(item: dict, model: str, timeout: int) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="neval_"))
    paths, cleanup = {}, []
    # seed
    try:
        sp, dl = _download_to_tmp(item["seed_cover"])
        paths["SEED"] = sp
        if dl:
            cleanup.append(sp)
    except Exception as e:
        return {"seed_id": item["seed_id"], "error": f"seed download: {e}"}
    # candidates
    avail = []
    for c in item["candidates"]:
        try:
            cp, dl = _download_to_tmp(c["cover"])
            paths[c["label"]] = cp
            if dl:
                cleanup.append(cp)
            avail.append(c["label"])
        except Exception:
            pass  # drop unreachable candidate

    listing = f"SEED building = {paths['SEED']}\n" + "\n".join(
        f"Candidate {lab} = {paths[lab]}" for lab in avail)
    add_dirs = {str(p.parent) for p in paths.values()}
    cmd = ["claude", "-p", f"{PROMPT}\n\nRead these image files:\n{listing}",
           "--output-format", "json", "--model", model]
    for d in add_dirs:
        cmd += ["--add-dir", d]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        envelope = json.loads(proc.stdout)
        verdicts = _extract_json(envelope.get("result", "")) or {}
    except subprocess.TimeoutExpired:
        return {"seed_id": item["seed_id"], "error": "timeout"}
    except Exception as e:
        return {"seed_id": item["seed_id"], "error": f"call: {e} :: {proc.stdout[:200] if 'proc' in dir() else ''}"}
    finally:
        for p in cleanup:
            try:
                p.unlink()
            except Exception:
                pass

    likes = [bool(verdicts.get(lab, {}).get("like")) for lab in avail]
    prec = sum(likes) / len(likes) if likes else None
    return {
        "seed_id": item["seed_id"], "seed_program": item["seed_program"],
        "n_candidates": len(avail), "precision_at_k": prec,
        "verdicts": {lab: verdicts.get(lab) for lab in avail},
        "true_rank_by_label": {c["label"]: c["true_rank"] for c in item["candidates"]},
        "cost_usd": envelope.get("total_cost_usd"),
        "usage": envelope.get("usage", {}).get("input_tokens") and {
            "input": envelope["usage"].get("input_tokens"),
            "output": envelope["usage"].get("output_tokens"),
            "cache_read": envelope["usage"].get("cache_read_input_tokens"),
        },
    }


def judge(args) -> int:
    items = [json.loads(l) for l in Path(args.queue).read_text().split("\n") if l]
    results, total_cost = [], 0.0
    with open(args.out, "w", encoding="utf-8") as f:
        for n, item in enumerate(items, 1):
            r = run_judge_call(item, args.model, args.timeout)
            results.append(r)
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            total_cost += (r.get("cost_usd") or 0.0)
            p = r.get("precision_at_k")
            print(f"[{n}/{len(items)}] {r['seed_id']} "
                  f"prec={p if p is None else round(p,2)} "
                  f"cost=${r.get('cost_usd') or 0:.3f}"
                  + (f"  ERR:{r['error']}" if r.get("error") else ""), flush=True)

    ok = [r["precision_at_k"] for r in results if r.get("precision_at_k") is not None]
    summary = {
        "seeds": len(items), "judged_ok": len(ok), "errors": len(items) - len(ok),
        "mean_precision_at_k": round(float(np.mean(ok)), 4) if ok else None,
        "total_cost_usd": round(total_cost, 4),
        "mean_cost_per_seed": round(total_cost / len(items), 4) if items else None,
    }
    Path(str(args.out) + ".summary.json").write_text(json.dumps(summary, indent=2))
    print("\nSUMMARY:", json.dumps(summary, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("build-queue")
    q.add_argument("--dir", type=Path, default=ROOT / "data/reports/neighbor_eval")
    q.add_argument("--embeddings", type=Path)
    q.add_argument("--n", type=int, default=10)
    q.add_argument("--k", type=int, default=5)
    q.add_argument("--seed", type=int, default=20260625)
    q.add_argument("--out", type=Path, required=True)
    q.set_defaults(fn=build_queue)
    j = sub.add_parser("judge")
    j.add_argument("--queue", type=Path, required=True)
    j.add_argument("--model", default="opus")
    j.add_argument("--timeout", type=int, default=240)
    j.add_argument("--out", type=Path, required=True)
    j.set_defaults(fn=judge)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
