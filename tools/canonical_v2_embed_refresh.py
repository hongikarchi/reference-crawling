#!/usr/bin/env python3
"""Refresh embeddings for a patched strict canonical file.

Copies embeddings from the previous embedded strict file for unaffected CIDs and
encodes only affected/missing rows. This avoids re-embedding the whole dataset
after small canonical split repairs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.embed_strict import make_embedding_text  # noqa: E402


REFRESH_DIR = ROOT / "data/canonical/country_conflict_refresh"
DEFAULT_INPUT = REFRESH_DIR / "canonical_buildings_strict.resume10_complete.json"
DEFAULT_BASE = REFRESH_DIR / "canonical_buildings_strict_embedded.resume10_complete.json"
DEFAULT_AFFECTED = REFRESH_DIR / "d2_image_backfill_resume10_embed_affected.json"
DEFAULT_OUTPUT = REFRESH_DIR / "canonical_buildings_strict_embedded.refresh.json"


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _load_affected(path: Path) -> set[str]:
    data = _load_json(path)
    return {str(cid) for cid in data.get("affected_cids") or [] if str(cid)}


def _embedding_is_valid(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 384
        and all(isinstance(v, (int, float)) for v in value)
    )


def _base_embeddings(path: Path) -> dict[str, list[float]]:
    data = _load_json(path)
    out: dict[str, list[float]] = {}
    for row in data.get("buildings") or []:
        cid = str(row.get("canonical_bld_id") or "")
        emb = row.get("embedding")
        if cid and _embedding_is_valid(emb):
            out[cid] = emb
    return out


def _default_encoder(texts: Sequence[str]) -> list[list[float]]:
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        local_files_only=True,
        model_kwargs={"local_files_only": True},
        tokenizer_kwargs={"local_files_only": True},
        config_kwargs={"local_files_only": True},
    )
    embeddings = model.encode(list(texts), batch_size=64, show_progress_bar=True)
    return [emb.tolist() for emb in embeddings]


def refresh_embeddings(
    *,
    input_path: Path = DEFAULT_INPUT,
    base_path: Path = DEFAULT_BASE,
    affected_path: Path = DEFAULT_AFFECTED,
    output_path: Path = DEFAULT_OUTPUT,
    encoder: Callable[[Sequence[str]], list[list[float]]] = _default_encoder,
) -> dict[str, Any]:
    strict = _load_json(input_path)
    buildings = strict.get("buildings") or []
    affected = _load_affected(affected_path)
    base = _base_embeddings(base_path)

    needs_encode: list[dict[str, Any]] = []
    copied = 0
    for row in buildings:
        cid = str(row.get("canonical_bld_id") or "")
        if cid and cid not in affected and cid in base:
            row["embedding"] = base[cid]
            copied += 1
        else:
            row.pop("embedding", None)
            needs_encode.append(row)

    texts = [make_embedding_text(row) for row in needs_encode]
    encoded = encoder(texts) if texts else []
    if len(encoded) != len(needs_encode):
        raise ValueError(f"encoder returned {len(encoded)} embeddings for {len(needs_encode)} texts")
    for row, embedding in zip(needs_encode, encoded):
        if not _embedding_is_valid(embedding):
            raise ValueError(f"bad embedding for {row.get('canonical_bld_id')}")
        row["embedding"] = embedding

    missing = sum(1 for row in buildings if not _embedding_is_valid(row.get("embedding")))
    report = {
        "status": "PASS" if missing == 0 else "FAIL",
        "input": str(input_path),
        "base": str(base_path),
        "output": str(output_path),
        "total_rows": len(buildings),
        "copied_embeddings": copied,
        "encoded_embeddings": len(needs_encode),
        "missing_embeddings": missing,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(strict, f, ensure_ascii=False)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh patched strict embeddings.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--base", type=Path, default=DEFAULT_BASE)
    parser.add_argument("--affected", type=Path, default=DEFAULT_AFFECTED)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    report = refresh_embeddings(
        input_path=args.input,
        base_path=args.base,
        affected_path=args.affected,
        output_path=args.output,
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
