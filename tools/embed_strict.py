#!/usr/bin/env python3
"""Stage G: Encode canonical_buildings_strict.json into 384-dim embeddings.

Reads data/canonical/canonical_buildings_strict.json
Writes data/canonical/canonical_buildings_strict_embedded.json
(same shape; each building gains an `embedding` field).
"""
import json
import sys
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "data/canonical/canonical_buildings_strict.json"
OUTPUT = ROOT / "data/canonical/canonical_buildings_strict_embedded.json"


def make_embedding_text(b: dict) -> str:
    image_d = b.get("image_derived") or {}
    parts = [
        b.get("name") or "",
        b.get("architects_text") or "",
        b.get("location_city") or "",
        b.get("location_country") or "",
        str(b.get("project_year") or ""),
        b.get("program") or "",
        b.get("typology_primary") or "",
        " ".join(b.get("typology_tags") or []),
        b.get("style") or "",
        b.get("color_tone") or "",
        b.get("atmosphere") or "",
        " ".join(b.get("material_visual") or []),
        b.get("visual_description") or "",
        image_d.get("visual_description") or "",
    ]
    return " ".join(p for p in parts if p)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--input", type=Path, default=INPUT)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument(
        "--model",
        default="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    )
    args = parser.parse_args()

    if not args.input.exists():
        print(f"ERROR: {args.input} not found", file=sys.stderr)
        sys.exit(1)
    print(f"loading {args.input}")
    with args.input.open() as f:
        data = json.load(f)
    buildings = data["buildings"]
    print(f"  {len(buildings)} buildings")

    print("loading sentence-transformers model...")
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(args.model)

    texts = [make_embedding_text(b) for b in buildings]
    print(f"encoding {len(texts)} buildings...")
    embeddings = model.encode(
        texts, batch_size=64, show_progress_bar=True
    )
    for b, emb in zip(buildings, embeddings):
        b["embedding"] = emb.tolist()

    print(f"writing {args.output}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w") as f:
        json.dump(data, f, ensure_ascii=False)

    no_emb = sum(1 for b in buildings if not b.get("embedding"))
    print(f"done — {len(buildings)} buildings, {no_emb} missing embeddings")


if __name__ == "__main__":
    main()
