"""E-1: phash dedupe for canonical building images.

This stage does no Vision calls. It reads canonical source_refs, resolves all
source-side image URLs, attaches phashes from data/canonical/phash_cache.json,
fetches only missing phashes, and clusters near-duplicates across different
sources only.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Optional

import imagehash

from canonical.image_dedup import PHASH_THRESHOLD, fetch_image_metadata, rank_within_cluster
from tools.image_dedup_5type import (
    DEFAULT_CANONICAL_PATH,
    DEFAULT_PHASH_CACHE_PATH,
    PROJECT_ROOT,
    SourceImageSpec,
    _canonical_rows,
    _fetch_missing_metadata,
    _load_done_cids,
    _load_phash_cache,
    _public_image_record,
    _read_json,
    collect_cluster_images,
    load_source_image_index,
)


DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "data" / "canonical" / "e1_clusters.jsonl"


def _hamming_distance(a_hex: str, b_hex: str) -> int:
    return imagehash.hex_to_hash(a_hex) - imagehash.hex_to_hash(b_hex)


def _cross_source_clusters(images: list[dict], threshold: int = PHASH_THRESHOLD) -> list[list[int]]:
    """Cluster by phash, comparing only images from different sources."""
    if not images:
        return []

    parent = list(range(len(images)))
    sources_by_root = [{str(image.get("source"))} for image in images]

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri = find(i)
        rj = find(j)
        if ri != rj and sources_by_root[ri].isdisjoint(sources_by_root[rj]):
            parent[rj] = ri
            sources_by_root[ri].update(sources_by_root[rj])

    for i in range(len(images)):
        ph_i = images[i].get("phash")
        src_i = images[i].get("source")
        if not ph_i:
            continue
        for j in range(i + 1, len(images)):
            ph_j = images[j].get("phash")
            if not ph_j or src_i == images[j].get("source"):
                continue
            try:
                if _hamming_distance(ph_i, ph_j) <= threshold:
                    union(i, j)
            except Exception:
                continue

    grouped: dict[int, list[int]] = defaultdict(list)
    for idx in range(len(images)):
        grouped[find(idx)].append(idx)
    return list(grouped.values())


def process_cluster(
    cluster: dict,
    *,
    source_index: dict[str, list[dict]],
    fetcher: Callable[..., Optional[dict]] = fetch_image_metadata,
) -> dict:
    cid = cluster.get("canonical_bld_id") or cluster.get("cid") or cluster.get("id")
    images = collect_cluster_images(cluster, source_index)
    _fetch_missing_metadata(images, fetcher)

    best_by_cluster: dict[str, dict] = {}
    for cluster_id, indices in enumerate(_cross_source_clusters(images)):
        ranked = rank_within_cluster(images, indices)
        for rank, image_idx in enumerate(ranked):
            images[image_idx]["phash_cluster_id"] = cluster_id
            images[image_idx]["rank"] = rank
        best_by_cluster[str(cluster_id)] = _public_image_record(images[ranked[0]])

    return {
        "cid": cid,
        "all_images": [_public_image_record(image) for image in images],
        "best_image_per_cluster": best_by_cluster,
    }


def run_all(
    *,
    canonical_path: Path = DEFAULT_CANONICAL_PATH,
    output_path: Path = DEFAULT_OUTPUT_PATH,
    phash_cache_path: Path = DEFAULT_PHASH_CACHE_PATH,
    workers: int = 32,
    limit: Optional[int] = None,
    source_specs: Optional[dict[str, SourceImageSpec]] = None,
    fetcher: Callable[..., Optional[dict]] = fetch_image_metadata,
) -> dict[str, int]:
    rows = _canonical_rows(_read_json(canonical_path, {}))
    done = _load_done_cids(output_path)
    pending = [row for row in rows if str(row.get("canonical_bld_id")) not in done]
    if limit is not None:
        pending = pending[:limit]

    source_index = load_source_image_index(
        source_specs=source_specs,
        phash_cache=_load_phash_cache(phash_cache_path),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    summary = {
        "rows_total": len(rows),
        "rows_skipped_done": len(done),
        "rows_processed": 0,
        "images_written": 0,
        "clusters_written": 0,
    }

    def work(row: dict) -> dict:
        return process_cluster(row, source_index=source_index, fetcher=fetcher)

    with output_path.open("a", encoding="utf-8") as f:
        with ThreadPoolExecutor(max_workers=max(workers, 1)) as ex:
            futures = [ex.submit(work, row) for row in pending]
            for fut in as_completed(futures):
                rec = fut.result()
                f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
                f.flush()
                summary["rows_processed"] += 1
                summary["images_written"] += len(rec.get("all_images") or [])
                summary["clusters_written"] += len(rec.get("best_image_per_cluster") or {})
                if summary["rows_processed"] % 100 == 0:
                    print(json.dumps(summary, sort_keys=True), flush=True)

    print(json.dumps(summary, indent=2, sort_keys=True), flush=True)
    return summary


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="E-1 phash dedupe for canonical images.")
    parser.add_argument("--canonical", type=Path, default=DEFAULT_CANONICAL_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--phash-cache", type=Path, default=DEFAULT_PHASH_CACHE_PATH)
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)
    run_all(
        canonical_path=args.canonical,
        output_path=args.output,
        phash_cache_path=args.phash_cache,
        workers=args.workers,
        limit=args.limit,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
