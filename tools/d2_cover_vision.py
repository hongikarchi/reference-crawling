"""D-2: cover-image Vision enrichment for canonical buildings."""

from __future__ import annotations

import argparse
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Optional

from tools.e1_phash_dedup import DEFAULT_OUTPUT_PATH as DEFAULT_E1_PATH
from tools.e2_vision_5type import _iter_jsonl
from tools.image_dedup_5type import (
    DEFAULT_CANONICAL_PATH,
    PROJECT_ROOT,
    _canonical_rows,
    _download_to_tmp,
    _load_done_cids,
    _read_json,
    _source_priority,
)
from core import vocab
from tools.normalize_image_derived import normalize_image_derived


DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "data" / "canonical" / "d2_results.jsonl"

# Term lists are built from core/vocab.py so the prompt can never drift out of
# the controlled vocabulary (audit 2026-05 — the previous hardcoded list used
# lowercase + non-vocab terms and produced ~24% out-of-vocab values).
PROMPT_D2_COVER = (
    "Describe the architectural building in this image. Output JSON only: "
    "{style_image, color_tone_image, material_visual_image:[...], "
    "visual_description_image}. "
    f"style_image MUST be exactly one of: {', '.join(sorted(vocab.STYLE))}. "
    f"color_tone_image MUST be exactly one of: {', '.join(sorted(vocab.COLOR_TONE))}. "
    "Use the exact spelling and capitalization shown above. "
    "material_visual_image is short material labels; "
    "visual_description_image is 60-100 words, present tense."
)


def _load_canonical_cids(path: Path) -> list[str]:
    rows = _canonical_rows(_read_json(path, {}))
    return [
        str(row.get("canonical_bld_id"))
        for row in rows
        if row.get("canonical_bld_id")
    ]


def _best_cover_sort_key(image: dict):
    return (
        0 if image.get("kind") == "cover" or image.get("image_order") == 0 else 1,
        image.get("image_order", 999),
        -_source_priority(image.get("source", "unknown")),
        -((image.get("w") or 0) * (image.get("h") or 0)),
        -(image.get("bytes") or 0),
    )


def _load_e1_best_covers(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in _iter_jsonl(path):
        cid = row.get("cid")
        if not cid:
            continue
        images = [img for img in row.get("all_images") or [] if isinstance(img, dict) and img.get("url")]
        if images:
            out[str(cid)] = sorted(images, key=_best_cover_sort_key)[0]
    return out


def vision_enrich_cover(url_or_path: str, *, attempts: int = 2) -> dict:
    try:
        image_path, should_delete = _download_to_tmp(url_or_path)
    except Exception:
        return {}
    try:
        for _ in range(max(attempts, 1)):
            try:
                proc = subprocess.run(
                    [
                        "codex",
                        "exec",
                        "--skip-git-repo-check",
                        "-c",
                        "model=gpt-5.5",
                        "-c",
                        "model_reasoning_effort=low",
                        "-c",
                        "service_tier=fast",
                        "-i",
                        str(image_path),
                        "--",
                        PROMPT_D2_COVER,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
                text = (proc.stdout or proc.stderr or "").strip()
                start = text.find("{")
                end = text.rfind("}")
                if start >= 0 and end >= start:
                    text = text[start : end + 1]
                parsed = json.loads(text)
                if isinstance(parsed, dict) and parsed:
                    return parsed
            except Exception:
                continue
        return {}
    finally:
        if should_delete:
            try:
                image_path.unlink()
            except FileNotFoundError:
                pass


def _normalize_result(cid: str, image: Optional[dict], payload: dict) -> dict:
    if not isinstance(payload, dict):
        payload = {}
    materials = payload.get("material_visual_image")
    if isinstance(materials, str):
        materials = [materials]
    if not isinstance(materials, list):
        materials = []
    # validate style/color against core/vocab — out-of-vocab values are
    # case-fixed, remapped, or dropped (audit 2026-05 recurrence guard).
    norm, _ = normalize_image_derived({
        "style": payload.get("style_image"),
        "color_tone": payload.get("color_tone_image"),
    })
    return {
        "cid": cid,
        "cover_url": image.get("url") if image else None,
        "style_image": norm.get("style"),
        "color_tone_image": norm.get("color_tone"),
        "material_visual_image": [str(v) for v in materials if v],
        "visual_description_image": payload.get("visual_description_image"),
    }


def process_cid(
    cid: str,
    *,
    best_covers: dict[str, dict],
    classifier: Callable[[str], dict] = vision_enrich_cover,
) -> dict:
    image = best_covers.get(str(cid))
    if not image:
        return _normalize_result(str(cid), None, {})
    try:
        payload = classifier(image["url"])
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return _normalize_result(str(cid), image, payload)


def run_all(
    *,
    canonical_path: Path = DEFAULT_CANONICAL_PATH,
    e1_path: Path = DEFAULT_E1_PATH,
    output_path: Path = DEFAULT_OUTPUT_PATH,
    workers: int = 32,
    limit: Optional[int] = None,
    classifier: Callable[[str], dict] = vision_enrich_cover,
) -> dict[str, int]:
    done = _load_done_cids(output_path)
    cids = [cid for cid in _load_canonical_cids(canonical_path) if cid not in done]
    if limit is not None:
        cids = cids[:limit]
    best_covers = _load_e1_best_covers(e1_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    summary = {
        "rows_skipped_done": len(done),
        "rows_processed": 0,
        "rows_with_cover": 0,
        "rows_with_payload": 0,
    }

    with output_path.open("a", encoding="utf-8") as f:
        with ThreadPoolExecutor(max_workers=max(workers, 1)) as ex:
            futures = [
                ex.submit(process_cid, cid, best_covers=best_covers, classifier=classifier)
                for cid in cids
            ]
            for fut in as_completed(futures):
                rec = fut.result()
                f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
                f.flush()
                summary["rows_processed"] += 1
                if rec.get("cover_url"):
                    summary["rows_with_cover"] += 1
                if rec.get("visual_description_image"):
                    summary["rows_with_payload"] += 1
                if summary["rows_processed"] % 100 == 0:
                    print(json.dumps(summary, sort_keys=True), flush=True)

    print(json.dumps(summary, indent=2, sort_keys=True), flush=True)
    return summary


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="D-2 cover-image Vision enrichment.")
    parser.add_argument("--canonical", type=Path, default=DEFAULT_CANONICAL_PATH)
    parser.add_argument("--e1", type=Path, default=DEFAULT_E1_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)
    run_all(
        canonical_path=args.canonical,
        e1_path=args.e1,
        output_path=args.output,
        workers=args.workers,
        limit=args.limit,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
