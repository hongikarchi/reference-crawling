#!/usr/bin/env python3
"""Phase-0 prep for the embedding-space quality eval (Tier-1, 2026-06-25).

Streams the 1 GB `canonical_buildings_strict_embedded.*.json` (one giant
`{"buildings":[...]}` line) ONCE and emits a compact sidecar so neighbor
computation never re-parses the big file:

  embeddings.npy   float32 (N, 384), L2-normalized rows  (publishable only)
  meta.jsonl       one row per embedding row, SAME order: id + cover + strata
                   + the human-readable fields a judge/spot-check needs
  manifest.json    counts, dim, source path, norm flag

Read-only on the local artifact. No Neon, no network. Idempotent.

Usage:
  python3 tools/neighbor_eval_prep.py --limit 500   # smoke
  python3 tools/neighbor_eval_prep.py               # full (publishable pool)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import ijson
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SRC = (ROOT / "data/canonical/country_conflict_refresh/"
               "canonical_buildings_strict_embedded.completeness_c26_rem2026q2.json")
OUT_DIR = ROOT / "data/reports/neighbor_eval"

EMBED_DIM = 384

# Fields kept in meta.jsonl. Strata mirror accuracy_sample.py (identity_source x
# confidence_tier x era-proxy). Display fields let a judge read context and let the
# human spot-check render a card. Stored labels are NOT shown to the vision judge
# (independence) — they live here for stratification + post-hoc analysis only.
META_FIELDS = (
    "canonical_bld_id", "name", "display_cover_url", "cover_image_url_default",
    "identity_source", "confidence_tier", "project_year", "year_kind",
    "location_country", "location_city", "program", "style", "atmosphere",
    "material_visual", "typology_primary", "color_tone",
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC)
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    ap.add_argument("--limit", type=int, default=0, help="0 = all publishable")
    ap.add_argument("--include-nonpublishable", action="store_true")
    args = ap.parse_args()

    if not args.src.exists():
        print(f"ERROR: source not found: {args.src}", file=sys.stderr)
        return 1
    args.out.mkdir(parents=True, exist_ok=True)

    vecs: list[np.ndarray] = []
    meta_path = args.out / "meta.jsonl"
    kept = scanned = skipped_dim = skipped_nonpub = 0

    with open(args.src, "rb") as fsrc, open(meta_path, "w", encoding="utf-8") as fmeta:
        for b in ijson.items(fsrc, "buildings.item"):
            scanned += 1
            if not args.include_nonpublishable and not b.get("is_publishable"):
                skipped_nonpub += 1
                continue
            emb = b.get("embedding")
            if not isinstance(emb, list) or len(emb) != EMBED_DIM:
                skipped_dim += 1
                continue
            v = np.asarray(emb, dtype=np.float32)
            n = float(np.linalg.norm(v))
            if n == 0.0:
                skipped_dim += 1
                continue
            vecs.append(v / n)  # L2-normalize so dot == cosine
            row = {k: b.get(k) for k in META_FIELDS}
            row["is_publishable"] = bool(b.get("is_publishable"))
            fmeta.write(json.dumps(row, ensure_ascii=False) + "\n")
            kept += 1
            if kept % 5000 == 0:
                print(f"  ...{kept} kept ({scanned} scanned)", file=sys.stderr)
            if args.limit and kept >= args.limit:
                break

    mat = np.vstack(vecs).astype(np.float32) if vecs else np.zeros((0, EMBED_DIM), np.float32)
    np.save(args.out / "embeddings.npy", mat)
    manifest = {
        "source": str(args.src),
        "rows": kept, "scanned": scanned,
        "skipped_nonpublishable": skipped_nonpub, "skipped_bad_embedding": skipped_dim,
        "dim": EMBED_DIM, "l2_normalized": True,
        "embeddings_npy": str(args.out / "embeddings.npy"),
        "meta_jsonl": str(meta_path),
        "limit": args.limit or None,
    }
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
