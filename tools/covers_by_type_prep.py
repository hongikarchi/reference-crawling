#!/usr/bin/env python3
"""Interior-slot candidate shortlist for covers_by_type (sample-scale, 2026-07).

covers_by_type.interior is ~empty (3/273 on the repick sample) because the
filename heuristic rarely fires. This tool builds, for each target building,
a SigLIP-scored interior shortlist from its all_images pool; a stronger judge
then picks the best (or none) and ambiguous cases go to the review app.

  --download   pull candidate images (cluster of the repick set) to --dest
  --score      SigLIP 7-category zero-shot on downloaded candidates
               -> cbt_interior_shortlist.jsonl (top interior candidates/bld)

Local + free (SigLIP on CPU); network only to the public image CDNs.
Read-only on Neon and on all artifacts.
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.cover_repick_recheck_prep import _download_one, atomic_write_json, now_iso  # noqa: E402
from tools.cover_classify_siglip import CAT_PROMPTS  # noqa: E402

REPORT_DIR = ROOT / "data/reports/cover_audit/repick_chunk1k"
ALL_IMAGES = ROOT / "data/reports/cover_audit/all_images.jsonl"
DECISIONS = REPORT_DIR / "cover_repick_decisions.json"
MANIFEST = REPORT_DIR / "cbt_image_manifest.json"
SCORES = REPORT_DIR / "cbt_image_scores.jsonl"
SHORTLIST = REPORT_DIR / "cbt_interior_shortlist.jsonl"

INTERIOR_CAT = "interior_only"


def target_ids() -> list[str]:
    d = json.loads(DECISIONS.read_text(encoding="utf-8"))
    return sorted({r["canonical_bld_id"] for r in d["decisions"].values()})


def load_pools(ids: list[str], cap: int) -> dict[str, list[dict]]:
    """Candidate images per building: display cover excluded, capped by order."""
    d = json.loads(DECISIONS.read_text(encoding="utf-8"))
    display_now = {}
    for r in d["decisions"].values():
        display_now[r["canonical_bld_id"]] = (
            r["new_display_cover_url"] if r["decision"] == "approve_swap"
            else r["old_display_cover_url"])
    idset = set(ids)
    pools: dict[str, list[dict]] = {}
    with ALL_IMAGES.open(encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            bid = row["canonical_bld_id"]
            if bid not in idset:
                continue
            cands = [c for c in row.get("candidates") or []
                     if c.get("url") and c["url"] != display_now.get(bid)]
            cands.sort(key=lambda c: c.get("order") or 0)
            pools[bid] = cands[:cap]
    return pools


def cmd_download(ids: list[str], dest: Path, cap: int, workers: int) -> int:
    pools = load_pools(ids, cap)
    dest.mkdir(parents=True, exist_ok=True)
    jobs = []
    for bid, cands in pools.items():
        for k, c in enumerate(cands):
            jobs.append((bid, k, c["url"], dest / f"{bid}_{k:02d}.jpg", c.get("kind")))
    entries: dict[str, dict] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_download_one, url, path): (bid, k, url, path, kind)
                for (bid, k, url, path, kind) in jobs}
        for fut in as_completed(futs):
            bid, k, url, path, kind = futs[fut]
            status, nbytes, err = fut.result()
            entries[f"{bid}_{k:02d}"] = {
                "canonical_bld_id": bid, "url": url, "path": str(path),
                "kind": kind, "status": status, "bytes": nbytes,
                **({"error": err} if err else {})}
            done += 1
            if done % 200 == 0:
                print(f"  {done}/{len(jobs)} downloaded")
    ok = sum(1 for e in entries.values() if e["status"] == "ok")
    atomic_write_json(MANIFEST, {
        "generated_at": now_iso(), "dest": str(dest), "cap": cap,
        "buildings": len(pools), "images_total": len(jobs), "images_ok": ok,
        "entries": entries})
    print(f"downloaded {ok}/{len(jobs)} candidate images "
          f"for {len(pools)} buildings -> {MANIFEST}")
    return 0


def cmd_score(batch: int, thresh: float, top: int, max_new: int | None = None) -> int:
    import torch
    import torch.nn.functional as F
    from PIL import Image
    from transformers import AutoModel, AutoProcessor

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    entries = [e for e in manifest["entries"].values() if e["status"] == "ok"]

    scored: dict[str, dict] = {}
    if SCORES.exists():  # resume: skip already-scored paths
        for line in SCORES.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                scored[r["path"]] = r
    todo = [e for e in entries if e["path"] not in scored]
    remaining_after = max(0, len(todo) - max_new) if max_new else 0
    if max_new:
        todo = todo[:max_new]
    print(f"scoring {len(todo)} images ({len(scored)} already done, "
          f"{remaining_after} deferred to next chunk)")

    model = AutoModel.from_pretrained("google/siglip-base-patch16-224")
    proc = AutoProcessor.from_pretrained("google/siglip-base-patch16-224")
    model.eval()
    cats = list(CAT_PROMPTS)

    def feat(x):  # transformers 5.x returns ModelOutput, 4.x a bare tensor
        return x if torch.is_tensor(x) else x.pooler_output

    with torch.no_grad():
        t = proc(text=[CAT_PROMPTS[c] for c in cats], padding="max_length",
                 return_tensors="pt")
        tf = F.normalize(feat(model.get_text_features(**t)), dim=-1)

    with SCORES.open("a", encoding="utf-8") as out:
        for i in range(0, len(todo), batch):
            chunk = todo[i:i + batch]
            imgs, kept = [], []
            for e in chunk:
                try:
                    imgs.append(Image.open(e["path"]).convert("RGB"))
                    kept.append(e)
                except Exception as exc:  # noqa: BLE001 — unreadable file = skip
                    out.write(json.dumps({"path": e["path"], "error": str(exc)[:120]}) + "\n")
            if not imgs:
                continue
            with torch.no_grad():
                v = proc(images=imgs, return_tensors="pt")
                vf = F.normalize(feat(model.get_image_features(**v)), dim=-1)
                logits = vf @ tf.T * model.logit_scale.exp() + model.logit_bias
                probs = torch.sigmoid(logits)          # SigLIP is sigmoid, not softmax
                rel = torch.softmax(logits, dim=-1)     # relative category mix
            for e, p, r in zip(kept, probs, rel):
                rec = {"path": e["path"], "canonical_bld_id": e["canonical_bld_id"],
                       "url": e["url"], "kind": e["kind"],
                       "probs": {c: round(float(x), 4) for c, x in zip(cats, r)},
                       "top_cat": cats[int(r.argmax())]}
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                scored[e["path"]] = rec
            if (i // batch) % 10 == 0:
                print(f"  {min(i + batch, len(todo))}/{len(todo)} scored")

    # shortlist: top interior candidates per building
    per_bld: dict[str, list[dict]] = {}
    for r in scored.values():
        if "probs" not in r:
            continue
        per_bld.setdefault(r["canonical_bld_id"], []).append(r)
    n_with = 0
    with SHORTLIST.open("w", encoding="utf-8") as f:
        for bid in sorted(per_bld):
            cands = sorted(per_bld[bid],
                           key=lambda r: r["probs"][INTERIOR_CAT], reverse=True)
            picks = [c for c in cands if c["probs"][INTERIOR_CAT] >= thresh][:top]
            if picks:
                n_with += 1
            f.write(json.dumps({
                "canonical_bld_id": bid,
                "n_scored": len(cands),
                "shortlist": [{"url": c["url"], "path": c["path"],
                               "interior_prob": c["probs"][INTERIOR_CAT],
                               "top_cat": c["top_cat"]} for c in picks],
            }, ensure_ascii=False) + "\n")
    print(f"shortlist: {n_with}/{len(per_bld)} buildings have >= 1 interior candidate "
          f"(thresh {thresh}) -> {SHORTLIST}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--download", action="store_true")
    ap.add_argument("--dest", type=Path)
    ap.add_argument("--cap", type=int, default=10, help="max candidates per building")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, help="cap buildings (smoke)")
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--thresh", type=float, default=0.5,
                    help="min relative interior prob for the shortlist")
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--max", type=int, dest="max_new",
                    help="score at most N new images this run (chunked foreground runs)")
    args = ap.parse_args()

    if args.download:
        if not args.dest:
            ap.error("--download requires --dest")
        ids = target_ids()
        return cmd_download(ids[:args.limit] if args.limit else ids,
                            args.dest, args.cap, args.workers)
    if args.score:
        return cmd_score(args.batch, args.thresh, args.top, args.max_new)
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
