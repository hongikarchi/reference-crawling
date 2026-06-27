#!/usr/bin/env python3
"""Step-2 experiment: does re-captioning the NOISY image_derived.visual_description
lift text-embedding taste-coherence? (2026-06-28, measurement-only)

Production embedding (tools/embed_strict.make_embedding_text) concatenates many fields
incl. top-level visual_description (D-1, clean) AND image_derived.visual_description
(D-2 vision, ~24% out-of-vocab — the known-noisy field). Hypothesis: replacing the
noisy D-2 caption with a clean vision caption improves the coordinate.

  sanity   FREE — re-embed a sub-pool with ORIGINAL fields, compare to the production
           embedding. Must match (cosine ~1) or the recipe is wrong and any later
           comparison is invalid.
  recap    re-caption sub-pool covers (Haiku), swap image_derived.visual_description,
           re-embed -> recap.npy (aligned to sub-pool). Judge later vs baseline.

Sub-pool = first N of the existing 1500-pool (so baseline = production pool/embeddings).
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
from tools.embed_strict import make_embedding_text  # exact production recipe
from tools.image_dedup_5type import _download_to_tmp  # noqa: E402

SRC = (ROOT / "data/canonical/country_conflict_refresh/"
       "canonical_buildings_strict_embedded.completeness_c26_rem2026q2.json")
POOL = ROOT / "data/reports/neighbor_eval/pool"
OUT = ROOT / "data/reports/recap_exp"
MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
FIELDS = ("canonical_bld_id", "name", "architects_text", "location_city",
          "location_country", "project_year", "program", "typology_primary",
          "typology_tags", "style", "color_tone", "atmosphere", "material_visual",
          "visual_description", "image_derived", "display_cover_url")


def _subpool_ids(n):
    rows = [json.loads(l) for l in (POOL / "meta.jsonl").read_text().split("\n") if l]
    return [r["canonical_bld_id"] for r in rows[:n]]


def _extract(ids):
    import ijson
    want = set(ids)
    found = {}
    with open(SRC, "rb") as f:
        for b in ijson.items(f, "buildings.item"):
            if b["canonical_bld_id"] in want:
                found[b["canonical_bld_id"]] = {k: b.get(k) for k in FIELDS}
                if len(found) == len(want):
                    break
    return [found[i] for i in ids if i in found], [i for i in ids if i in found]


def _encode(texts):
    from sentence_transformers import SentenceTransformer
    m = SentenceTransformer(MODEL)
    v = m.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    return np.asarray(v, dtype=np.float32)


def cmd_sanity(args):
    ids = _subpool_ids(args.n)
    recs, ids2 = _extract(ids)
    print(f"extracted {len(recs)}/{len(ids)}", file=sys.stderr)
    mine = _encode([make_embedding_text(r) for r in recs])
    prod_all = np.load(POOL / "embeddings.npy")
    pool_ids = [json.loads(l)["canonical_bld_id"]
                for l in (POOL / "meta.jsonl").read_text().split("\n") if l]
    idx = {b: i for i, b in enumerate(pool_ids)}
    prod = np.vstack([prod_all[idx[i]] for i in ids2])
    cos = (mine * prod).sum(1)  # both unit-norm
    print(f"\n=== recipe fidelity (mine vs production) n={len(ids2)} ===")
    print(f"  per-row cosine: median {np.median(cos):.4f}  min {cos.min():.4f}  "
          f"mean {cos.mean():.4f}")
    print("  -> FAITHFUL (proceed to recap)" if np.median(cos) > 0.98
          else "  -> MISMATCH: recipe differs, recap comparison would be invalid")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "sanity.json").write_text(json.dumps(
        {"n": len(ids2), "median_cos": float(np.median(cos)),
         "min_cos": float(cos.min()), "faithful": bool(np.median(cos) > 0.98)}, indent=2))
    return 0


CAPTION_PROMPT = (
    "You are writing a precise visual description of a building from its photo, for a "
    "search embedding. In 2-3 sentences describe ONLY what is visible: form/massing, "
    "primary materials, facade character, colour, and overall architectural mood. No "
    "preamble, no guesses about program or location. Read the image file: "
)


def _caption(url, model, timeout):
    tmp, dl = None, False
    try:
        tmp, dl = _download_to_tmp(url)
        proc = subprocess.run(
            ["claude", "-p", CAPTION_PROMPT + str(tmp), "--add-dir", str(Path(tmp).parent),
             "--output-format", "json", "--model", model],
            capture_output=True, text=True, timeout=timeout)
        env = json.loads(proc.stdout)
        return (env.get("result") or "").strip(), env.get("total_cost_usd") or 0.0
    except Exception:
        return None, 0.0
    finally:
        if dl and tmp:
            Path(tmp).unlink(missing_ok=True)


def cmd_recap(args):
    ids = _subpool_ids(args.n)
    recs, ids2 = _extract(ids)
    OUT.mkdir(parents=True, exist_ok=True)
    cost = 0.0
    new_recs = []
    with open(OUT / "captions.jsonl", "w", encoding="utf-8") as f:
        for k, r in enumerate(recs, 1):
            cap, c = _caption(r["display_cover_url"], args.model, args.timeout)
            cost += c
            r2 = dict(r)
            imgd = dict(r.get("image_derived") or {})
            if cap:
                imgd["visual_description"] = cap  # swap the noisy field
            r2["image_derived"] = imgd
            new_recs.append(r2)
            f.write(json.dumps({"canonical_bld_id": r["canonical_bld_id"],
                                "caption": cap}, ensure_ascii=False) + "\n")
            if k % 25 == 0:
                print(f"  captioned {k}/{len(recs)}  (${cost:.2f})", flush=True)
    recap = _encode([make_embedding_text(r) for r in new_recs])
    np.save(OUT / "recap.npy", recap)
    # build the aligned baseline + meta dir for the judge (same ids/order)
    prod_all = np.load(POOL / "embeddings.npy")
    pool_rows = [json.loads(l) for l in (POOL / "meta.jsonl").read_text().split("\n") if l]
    idx = {r["canonical_bld_id"]: i for i, r in enumerate(pool_rows)}
    base = np.vstack([prod_all[idx[i]] for i in ids2])
    jd = OUT / "judge_pool"
    jd.mkdir(exist_ok=True)
    np.save(jd / "embeddings.npy", base)        # baseline = production
    np.save(jd / "recap.npy", recap)
    with open(jd / "meta.jsonl", "w", encoding="utf-8") as f:
        for i in ids2:
            r = pool_rows[idx[i]]
            f.write(json.dumps({k: r.get(k) for k in
                    ("canonical_bld_id", "name", "program", "style",
                     "display_cover_url", "location_country", "project_year")},
                    ensure_ascii=False) + "\n")
    print(f"\nrecap done: {len(new_recs)} re-embedded, caption cost ${cost:.2f}")
    print(f"judge dir: {jd}  (baseline embeddings.npy vs recap.npy, same ids)")
    (OUT / "recap_summary.json").write_text(json.dumps(
        {"n": len(new_recs), "caption_cost_usd": round(cost, 3), "model": args.model}, indent=2))
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sanity"); s.add_argument("--n", type=int, default=400)
    s.set_defaults(fn=cmd_sanity)
    r = sub.add_parser("recap")
    r.add_argument("--n", type=int, default=400)
    r.add_argument("--model", default="haiku")
    r.add_argument("--timeout", type=int, default=180)
    r.set_defaults(fn=cmd_recap)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
