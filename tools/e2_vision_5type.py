"""E-2: classify E-1 image clusters into 5 architectural image types."""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Optional

from tools.e1_phash_dedup import DEFAULT_OUTPUT_PATH as DEFAULT_E1_PATH
from tools.image_dedup_5type import (
    IMAGE_TYPES,
    PROJECT_ROOT,
    _filename_heuristic,
    _load_done_cids,
    _source_priority,
    vision_classify_image,
)


DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "data" / "canonical" / "e2_image_types.jsonl"


def _iter_jsonl(path: Path):
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def _classify_image(image: dict, classifier: Callable[[str], str]) -> str:
    heuristic = _filename_heuristic(image.get("url", ""), image.get("kind"))
    if heuristic:
        return heuristic
    try:
        label = classifier(image["url"]).strip().lower()
    except Exception:
        label = "exterior"
    return label if label in IMAGE_TYPES else "exterior"


def _cover_sort_key(image: dict):
    area = (image.get("w") or 0) * (image.get("h") or 0)
    size = image.get("bytes") or 0
    return (
        image.get("rank", 999),
        image.get("image_order", 999),
        -area,
        -size,
        -_source_priority(image.get("source", "unknown")),
    )


def process_e1_row(row: dict, *, classifier: Callable[[str], str] = vision_classify_image) -> dict:
    image_types: dict[str, str] = {}
    best_by_cluster = row.get("best_image_per_cluster") or {}
    for cluster_id, image in best_by_cluster.items():
        if isinstance(image, dict):
            image_types[str(cluster_id)] = _classify_image(image, classifier)

    typed_images = []
    for image in row.get("all_images") or []:
        if not isinstance(image, dict):
            continue
        cluster_id = str(image.get("phash_cluster_id"))
        typed = dict(image)
        typed["type"] = image_types.get(cluster_id, "exterior")
        typed_images.append(typed)

    covers_by_type: dict[str, Optional[str]] = {image_type: None for image_type in IMAGE_TYPES}
    for image_type in IMAGE_TYPES:
        candidates = [image for image in typed_images if image.get("type") == image_type]
        if candidates:
            covers_by_type[image_type] = sorted(candidates, key=_cover_sort_key)[0]["url"]

    return {
        "cid": row.get("cid"),
        "covers_by_type": covers_by_type,
        "image_types": image_types,
        "all_images_with_type": typed_images,
    }


def run_all(
    *,
    input_path: Path = DEFAULT_E1_PATH,
    output_path: Path = DEFAULT_OUTPUT_PATH,
    workers: int = 32,
    limit: Optional[int] = None,
    classifier: Callable[[str], str] = vision_classify_image,
) -> dict[str, int]:
    done = _load_done_cids(output_path)
    rows = [row for row in _iter_jsonl(input_path) if str(row.get("cid")) not in done]
    if limit is not None:
        rows = rows[:limit]
    output_path.parent.mkdir(parents=True, exist_ok=True)

    summary = {
        "rows_skipped_done": len(done),
        "rows_processed": 0,
        "clusters_classified": 0,
    }

    with output_path.open("a", encoding="utf-8") as f:
        with ThreadPoolExecutor(max_workers=max(workers, 1)) as ex:
            futures = [ex.submit(process_e1_row, row, classifier=classifier) for row in rows]
            for fut in as_completed(futures):
                rec = fut.result()
                f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
                f.flush()
                summary["rows_processed"] += 1
                summary["clusters_classified"] += len(rec.get("image_types") or {})
                if summary["rows_processed"] % 100 == 0:
                    print(json.dumps(summary, sort_keys=True), flush=True)

    print(json.dumps(summary, indent=2, sort_keys=True), flush=True)
    return summary


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="E-2 Vision 5-type image classification.")
    parser.add_argument("--input", type=Path, default=DEFAULT_E1_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)
    run_all(
        input_path=args.input,
        output_path=args.output,
        workers=args.workers,
        limit=args.limit,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
