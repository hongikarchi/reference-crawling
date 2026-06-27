#!/usr/bin/env python3
"""Phase-0 embedding-space quality eval core (Tier-1, 2026-06-25).

Loads the compact sidecar from neighbor_eval_prep.py and computes cosine top-K
neighbors entirely offline (rows are L2-normalized, so dot == cosine; brute-force
matmul is sub-second at 36,673x384). Vector-source-agnostic: point --embeddings at
any candidate matrix (text baseline, DINOv2, SigLIP, Claude-recaption) that shares
meta.jsonl row order, and the same harness scores it.

`--peek` is a free, no-LLM sanity view of a vector's neighbors. The Opus taste-
coherence judge (cost-gated) is added as a separate step on top of these neighbor sets.

Usage:
  python3 tools/neighbor_eval.py --peek 4 --k 5            # eyeball neighbors (free)
  python3 tools/neighbor_eval.py --peek 4 --seed-id bld_000000
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIR = ROOT / "data/reports/neighbor_eval"


def load(out_dir: Path):
    mat = np.load(out_dir / "embeddings.npy")
    # split on \n only — str.splitlines() also breaks on U+2028/U+2029, which
    # json.dumps(ensure_ascii=False) leaves raw inside fields (would corrupt rows).
    meta = [json.loads(l) for l in (out_dir / "meta.jsonl").read_text().split("\n") if l]
    if mat.shape[0] != len(meta):
        raise SystemExit(f"row mismatch: npy {mat.shape[0]} vs meta {len(meta)}")
    return mat, meta


def topk(mat: np.ndarray, idx: int, k: int) -> list[tuple[int, float]]:
    with np.errstate(all="ignore"):  # numpy2/Accelerate emits spurious matmul warnings
        sims = mat @ mat[idx]        # cosine (rows are unit-norm)
    sims[idx] = -np.inf             # exclude self
    nn = np.argpartition(-sims, k)[:k]
    nn = nn[np.argsort(-sims[nn])]
    return [(int(j), float(sims[j])) for j in nn]


def _fmt(m: dict) -> str:
    return (f"{m.get('name','?')[:46]:46}  "
            f"{(m.get('program') or '-'):14.14} {(m.get('style') or '-'):16.16} "
            f"{(m.get('location_country') or '-'):12.12} {m.get('project_year') or '-'}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    ap.add_argument("--embeddings", type=Path, help="override matrix (same meta order)")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--peek", type=int, default=0, help="N random seeds, print neighbors")
    ap.add_argument("--seed-id", action="append", default=[], help="explicit seed bld id(s)")
    args = ap.parse_args()

    mat, meta = load(args.dir)
    if args.embeddings:
        mat = np.load(args.embeddings)
        if mat.shape[0] != len(meta):
            raise SystemExit("override matrix row count != meta")
    id2idx = {m["canonical_bld_id"]: i for i, m in enumerate(meta)}

    seeds: list[int] = [id2idx[s] for s in args.seed_id if s in id2idx]
    if args.peek:
        rng = np.random.default_rng(20260625)  # fixed (no Math.random equiv needed)
        seeds += [int(x) for x in rng.choice(len(meta), size=args.peek, replace=False)]

    if not seeds:
        print("nothing to do — pass --peek N or --seed-id", file=sys.stderr)
        return 1

    for s in seeds:
        print("\n" + "=" * 110)
        print("SEED  ", _fmt(meta[s]))
        print("-" * 110)
        for rank, (j, sim) in enumerate(topk(mat, s, args.k), 1):
            print(f"  {rank}. cos={sim:.3f}  {_fmt(meta[j])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
