"""Phase 15 one-time + incremental perceptual-hash cache builder.

Produces data/canonical/phash_cache.json in the shape:
    {"<source>:<source_id>": ["<phash_hex>", ...]}
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional

from canonical.image_dedup import fetch_image_metadata


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_DIR = PROJECT_ROOT / "data" / "canonical"
CACHE_PATH = CANONICAL_DIR / "phash_cache.json"
PROGRESS_PATH = CANONICAL_DIR / "phash_cache_progress.json"
METALOCUS_FINAL_PATH = PROJECT_ROOT / "data" / "enrich" / "4_buildings_final.json"


@dataclass(frozen=True)
class SourceSpec:
    name: str
    db_path: Path
    table: str
    id_col: str
    cover_col: str
    gallery_col: str


SOURCE_SPECS: dict[str, SourceSpec] = {
    "divisare": SourceSpec(
        name="divisare",
        db_path=PROJECT_ROOT / "data" / "crawl" / "divisare.db",
        table="divisare_projects",
        id_col="id",
        cover_col="cover_image_url",
        gallery_col="gallery_urls",
    ),
    "architizer": SourceSpec(
        name="architizer",
        db_path=PROJECT_ROOT / "data" / "crawl" / "architizer.db",
        table="architizer_projects",
        id_col="id",
        cover_col="cover_image_url",
        gallery_col="gallery_image_urls",
    ),
    "archello": SourceSpec(
        name="archello",
        db_path=PROJECT_ROOT / "data" / "crawl" / "archello.db",
        table="archello_projects",
        id_col="id",
        cover_col="cover_image_url",
        gallery_col="gallery_image_urls",
    ),
    "metalocus": SourceSpec(
        name="metalocus",
        db_path=PROJECT_ROOT / "data" / "crawl" / "metalocus.db",
        table="buildings",
        id_col="id",
        cover_col="cover_image_url",
        gallery_col="gallery_image_urls",
    ),
}


def _cache_key(source: str, source_id: str) -> str:
    return f"{source}:{source_id}"


def _read_json(path: Path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _write_json_atomic(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def _load_cache(path: Path) -> dict[str, list[str]]:
    data = _read_json(path, {})
    return data if isinstance(data, dict) else {}


def _load_progress(path: Path) -> set[str]:
    data = _read_json(path, [])
    if isinstance(data, list):
        return {str(v) for v in data}
    if isinstance(data, dict):
        done = data.get("done")
        if isinstance(done, list):
            return {str(v) for v in done}
        return {str(k) for k, v in data.items() if v}
    return set()


def _write_progress(path: Path, done: set[str]) -> None:
    _write_json_atomic(path, {"done": sorted(done)})


def _parse_url_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [u for u in value if isinstance(u, str) and u.strip()]
    if not isinstance(value, str):
        return []
    text = value.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return [text]
    if isinstance(parsed, list):
        return [u for u in parsed if isinstance(u, str) and u.strip()]
    return []


def _pick_urls(cover_url: Optional[str], gallery_urls) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    def add(url: Optional[str]) -> None:
        if not url or not isinstance(url, str):
            return
        clean = url.strip()
        if clean and clean not in seen:
            seen.add(clean)
            urls.append(clean)

    add(cover_url)
    for url in _parse_url_list(gallery_urls)[:3]:
        add(url)
    return urls


def _metalocus_slug_to_building_id(path: Path = METALOCUS_FINAL_PATH) -> dict[str, str]:
    if not path.exists():
        return {}
    rows = _read_json(path, [])
    if not isinstance(rows, list):
        return {}
    out: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        slug = row.get("slug")
        building_id = row.get("building_id")
        if slug and building_id:
            out[str(slug)] = str(building_id)
    return out


def _iter_sql_source_rows(spec: SourceSpec) -> Iterator[dict]:
    if not spec.db_path.exists():
        return
    conn = sqlite3.connect(spec.db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"SELECT {spec.id_col} AS source_id, "
            f"{spec.cover_col} AS cover_url, "
            f"{spec.gallery_col} AS gallery_urls "
            f"FROM {spec.table} "
            f"WHERE ({spec.cover_col} IS NOT NULL AND {spec.cover_col} != '') "
            f"   OR ({spec.gallery_col} IS NOT NULL AND {spec.gallery_col} != '')"
        )
        for row in rows:
            urls = _pick_urls(row["cover_url"], row["gallery_urls"])
            if urls:
                yield {
                    "source": spec.name,
                    "source_id": str(row["source_id"]),
                    "urls": urls,
                }
    finally:
        conn.close()


def _iter_metalocus_rows(spec: SourceSpec) -> Iterator[dict]:
    if not spec.db_path.exists():
        return
    slug_to_building_id = _metalocus_slug_to_building_id()
    conn = sqlite3.connect(spec.db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT b.id AS db_id, a.slug AS slug, "
            "       b.cover_image_url AS cover_url, "
            "       b.gallery_image_urls AS gallery_urls "
            "FROM buildings b "
            "LEFT JOIN articles a ON b.article_id = a.id "
            "WHERE (b.cover_image_url IS NOT NULL AND b.cover_image_url != '') "
            "   OR (b.gallery_image_urls IS NOT NULL AND b.gallery_image_urls != '')"
        )
        for row in rows:
            urls = _pick_urls(row["cover_url"], row["gallery_urls"])
            if not urls:
                continue
            source_id = slug_to_building_id.get(row["slug"]) or str(row["db_id"])
            yield {
                "source": spec.name,
                "source_id": str(source_id),
                "urls": urls,
            }
    finally:
        conn.close()


def iter_source_rows(
    *,
    source: Optional[str] = None,
    source_specs: Optional[dict[str, SourceSpec]] = None,
) -> Iterator[dict]:
    specs = source_specs or SOURCE_SPECS
    names = [source] if source else list(specs.keys())
    for name in names:
        if name not in specs:
            raise ValueError(f"unknown source: {name}")
        spec = specs[name]
        if name == "metalocus":
            yield from _iter_metalocus_rows(spec)
        else:
            yield from _iter_sql_source_rows(spec)


def _iter_pending_chunks(
    *,
    source: Optional[str],
    source_specs: Optional[dict[str, SourceSpec]],
    done: set[str],
    limit: Optional[int],
    chunk_size: int,
    summary: dict[str, int],
) -> Iterator[list[dict]]:
    chunk: list[dict] = []
    queued = 0
    for item in iter_source_rows(source=source, source_specs=source_specs):
        key = _cache_key(item["source"], item["source_id"])
        if key in done:
            summary["rows_skipped_done"] += 1
            continue
        if limit is not None and queued >= limit:
            break

        summary["rows_seen"] += 1
        chunk.append({**item, "key": key})
        queued += 1
        if len(chunk) >= chunk_size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def _fetch_chunk(
    chunk: list[dict],
    *,
    workers: int,
    fetcher: Callable[..., Optional[dict]],
) -> Iterator[tuple[str, list[str], int]]:
    """Fetch all URLs in a chunk and yield rows after every URL has returned."""
    state: dict[str, dict] = {}
    futures = {}
    for item in chunk:
        key = item["key"]
        urls = list(item.get("urls") or [])
        state[key] = {
            "remaining": len(urls),
            "phashes": {},
            "n_urls": len(urls),
        }
        for idx, url in enumerate(urls):
            futures[(key, idx)] = url

    if not futures:
        for key, row in state.items():
            yield key, [], row["n_urls"]
        return

    with ThreadPoolExecutor(max_workers=max(workers, 1)) as ex:
        future_map = {
            ex.submit(fetcher, url, timeout=20): (key, idx)
            for (key, idx), url in futures.items()
        }
        for fut in as_completed(future_map):
            key, idx = future_map[fut]
            try:
                meta = fut.result()
            except Exception:
                meta = None
            phash = meta.get("phash") if isinstance(meta, dict) else None
            if isinstance(phash, str) and phash:
                state[key]["phashes"][idx] = phash

            state[key]["remaining"] -= 1
            if state[key]["remaining"] == 0:
                phashes_by_index = state[key]["phashes"]
                ordered_phashes = [
                    phashes_by_index[i]
                    for i in sorted(phashes_by_index)
                ]
                yield key, ordered_phashes, state[key]["n_urls"]


def build_cache(
    *,
    limit: Optional[int] = None,
    source: Optional[str] = None,
    workers: int = 32,
    cache_path: Path = CACHE_PATH,
    progress_path: Path = PROGRESS_PATH,
    source_specs: Optional[dict[str, SourceSpec]] = None,
    fetcher: Callable[..., Optional[dict]] = fetch_image_metadata,
    write_every: int = 100,
    chunk_size: int = 1000,
) -> dict[str, int]:
    cache = _load_cache(cache_path)
    done = _load_progress(progress_path) | set(cache.keys())

    summary = {
        "rows_seen": 0,
        "rows_skipped_done": 0,
        "rows_processed": 0,
        "rows_with_phashes": 0,
        "phashes_written": 0,
        "urls_attempted": 0,
        "fetch_errors": 0,
    }

    completed_since_flush = 0
    for chunk in _iter_pending_chunks(
        source=source,
        source_specs=source_specs,
        done=done,
        limit=limit,
        chunk_size=max(chunk_size, 1),
        summary=summary,
    ):
        for key, phashes, n_urls in _fetch_chunk(
            chunk,
            workers=workers,
            fetcher=fetcher,
        ):
            cache[key] = phashes
            done.add(key)
            summary["rows_processed"] += 1
            summary["urls_attempted"] += n_urls
            summary["phashes_written"] += len(phashes)
            summary["fetch_errors"] += max(n_urls - len(phashes), 0)
            if phashes:
                summary["rows_with_phashes"] += 1

            completed_since_flush += 1
            if completed_since_flush >= max(write_every, 1):
                _write_json_atomic(cache_path, cache)
                _write_progress(progress_path, done)
                completed_since_flush = 0

    _write_json_atomic(cache_path, cache)
    _write_progress(progress_path, done)
    return summary


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build data/canonical/phash_cache.json for Phase 15 matching."
    )
    parser.add_argument("--build", action="store_true", help="build/update the cache")
    parser.add_argument("--limit", type=int, default=None, help="process at most N new rows")
    parser.add_argument("--source", choices=sorted(SOURCE_SPECS), default=None)
    parser.add_argument("--workers", type=int, default=32)
    args = parser.parse_args(argv)

    if not args.build:
        parser.print_help()
        return 2

    summary = build_cache(limit=args.limit, source=args.source, workers=args.workers)
    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f"wrote {CACHE_PATH}")
    print(f"progress {PROGRESS_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
