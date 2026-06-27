#!/usr/bin/env python3
"""Cover re-pick smoke — SigLIP proposes a better exterior cover (2026-06-27).

For buildings whose CURRENT cover is non-representative, score the current cover and
up to --cap candidates from all_images on an "exterior" prompt (SigLIP, free) and
propose the best candidate IF it beats the current cover by --margin. Output before/
after proposals for eyeball validation + the "better exterior exists" hit-rate.

(Full-run hybrid would add a batched Haiku confirm on the proposed pairs; this smoke
measures whether the SigLIP proposal is worth confirming.)

Usage:
  python3 tools/cover_repick.py --bad-from data/reports/cover_audit/s150/classified.jsonl \
      --n 40 --cap 6 --out data/reports/cover_audit/repick_smoke
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.image_dedup_5type import _download_to_tmp  # noqa: E402

ALL_IMAGES = ROOT / "data/reports/cover_audit/all_images.jsonl"
GOOD = {"representative_exterior", "aerial_or_siteplan"}
EXT_PROMPT = "a photo of a building seen from outside, its exterior facade and overall form"
NEG_PROMPTS = [
    "an interior photo taken inside a building, a room or lobby",
    "an extreme close-up of an architectural material or facade detail",
    "a 3D architectural CGI render or drawing",
    "a landscape, nature scene, portrait or logo with no building",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bad-from", type=Path, help="haiku classified.jsonl (bad ids)")
    ap.add_argument("--sample", type=int, default=0,
                    help="instead: sample N random publishable; bad = low current ext-score")
    ap.add_argument("--bad-threshold", type=float, default=0.5,
                    help="--sample mode: current ext-score below this = bad cover")
    ap.add_argument("--seed", type=int, default=20260627)
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--cap", type=int, default=6, help="max candidates scored per bld")
    ap.add_argument("--margin", type=float, default=0.05, help="min exterior-score gain")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    import torch
    import torch.nn.functional as F
    from PIL import Image
    from transformers import AutoModel, AutoProcessor

    allimg_list = [json.loads(l) for l in ALL_IMAGES.read_text().split("\n") if l]
    allimg = {r["canonical_bld_id"]: r for r in allimg_list}
    if args.sample:
        import random as _r
        pool = [r["canonical_bld_id"] for r in allimg_list
                if r.get("current_cover") and r.get("n_candidates", 0) >= 2]
        _r.Random(args.seed).shuffle(pool)
        ids = pool[:args.sample]
    else:
        bf = [json.loads(l) for l in args.bad_from.read_text().split("\n") if l]
        ids = [b["canonical_bld_id"] for b in bf if b.get("category") not in GOOD][:args.n]

    name = "google/siglip-base-patch16-224"
    dev = "mps" if torch.backends.mps.is_available() else "cpu"
    proc = AutoProcessor.from_pretrained(name)
    model = AutoModel.from_pretrained(name).to(dev).eval()
    prompts = [EXT_PROMPT] + NEG_PROMPTS
    ti = proc(text=prompts, padding="max_length", return_tensors="pt").to(dev)
    with torch.no_grad():
        tf = F.normalize(model.get_text_features(**ti), dim=-1)

    def ext_score(url):
        """softmax prob of the exterior prompt vs the negatives (0..1).

        Streaming: download to a temp file, score, delete immediately — never hoards
        disk (peak = one image). Politeness/disk concern addressed at scale.
        """
        tmp, dl = None, False
        try:
            tmp, dl = _download_to_tmp(url)
            im = Image.open(tmp).convert("RGB").copy()
        except Exception:
            return None
        finally:
            if dl and tmp:  # delete only files we downloaded (not pre-existing local)
                Path(tmp).unlink(missing_ok=True)
        ii = proc(images=[im], return_tensors="pt").to(dev)
        with torch.no_grad():
            vf = F.normalize(model.get_image_features(**ii), dim=-1)
            sims = (vf @ tf.T)[0]
            prob = torch.softmax(sims * 100, dim=0)[0]  # SigLIP logit scale ~100
        return float(prob)

    args.out.mkdir(parents=True, exist_ok=True)
    hit = downloads = n_bad = 0
    with open(args.out / "proposals.jsonl", "w", encoding="utf-8") as f:
        for k, bid in enumerate(ids, 1):
            rec = allimg.get(bid)
            if not rec:
                continue
            cur = rec["current_cover"]
            cur_s = ext_score(cur)
            downloads += 1
            # --sample mode: a high-exterior current cover is already good -> skip
            if args.sample and cur_s is not None and cur_s >= args.bad_threshold:
                f.write(json.dumps({"canonical_bld_id": bid, "verdict": "keep_good",
                                    "current_ext_score": round(cur_s, 3),
                                    "improved": False}, ensure_ascii=False) + "\n")
                continue
            n_bad += 1
            cands = [c["url"] for c in rec["candidates"] if c["url"] != cur][:args.cap]
            best_u, best_s = None, -1.0
            for u in cands:
                s = ext_score(u)
                downloads += 1
                if s is not None and s > best_s:
                    best_s, best_u = s, u
            improved = (cur_s is not None and best_u is not None
                        and best_s - cur_s >= args.margin)
            if improved:
                hit += 1
            f.write(json.dumps({
                "canonical_bld_id": bid, "current_cover": cur,
                "current_ext_score": None if cur_s is None else round(cur_s, 3),
                "proposed_cover": best_u if improved else None,
                "proposed_ext_score": round(best_s, 3) if best_u else None,
                "improved": improved, "n_candidates_scored": len(cands),
            }, ensure_ascii=False) + "\n")
            if k % 50 == 0:
                print(f"  {k}/{len(ids)} scanned, {n_bad} bad, {hit} re-picks, "
                      f"${0} (free) {downloads} dl", flush=True)

    summ = {"mode": "sample" if args.sample else "bad-from",
            "n_scanned": len(ids), "n_bad": n_bad,
            "bad_rate": round(n_bad / len(ids), 3) if (args.sample and ids) else None,
            "improved_found": hit,
            "hit_rate_of_bad": round(hit / n_bad, 3) if n_bad else None,
            "downloads": downloads, "cap": args.cap, "margin": args.margin,
            "bad_threshold": args.bad_threshold}
    (args.out / "summary.json").write_text(json.dumps(summ, indent=2))
    print("\nSUMMARY:", json.dumps(summ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
