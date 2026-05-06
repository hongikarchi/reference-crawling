"""Stage E image dedupe + 5-type cover classification.

Output JSONL:
  data/canonical/e_image_results.jsonl

Each line:
  {
    "cid": "...",
    "all_images": [
      {url, type, source, source_id, image_order, phash_cluster_id, rank, ...}
    ],
    "covers_by_type": {exterior, interior, drawing, aerial, detail}
  }
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from canonical.image_dedup import (
    PHASH_THRESHOLD,
    SOURCE_PRIORITY,
    cluster_by_phash,
    fetch_image_metadata,
    rank_within_cluster,
)
from canonical.phash_cache import _metalocus_slug_to_building_id


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CANONICAL_PATH = PROJECT_ROOT / "data" / "canonical" / "canonical_buildings_4source.json"
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "data" / "canonical" / "e_image_results.jsonl"
DEFAULT_PHASH_CACHE_PATH = PROJECT_ROOT / "data" / "canonical" / "phash_cache.json"

IMAGE_TYPES = ("exterior", "interior", "drawing", "aerial", "detail")
PROMPT_5TYPE = (
    "Classify this architectural image into ONE of: exterior, interior, "
    "drawing, aerial, detail. Output only the single word, lowercase."
)

_DRAWING_RE = re.compile(r"(drawing|plan|section|elevation)", re.I)
_AERIAL_RE = re.compile(r"(aerial|drone|birds[-_ ]?eye|bird[-_ ]?s[-_ ]?eye)", re.I)


@dataclass(frozen=True)
class SourceImageSpec:
    source: str
    db_path: Path
    table: str
    id_col: str
    cover_col: str
    gallery_col: str
    drawing_col: Optional[str] = None


SOURCE_SPECS: dict[str, SourceImageSpec] = {
    "divisare": SourceImageSpec(
        source="divisare",
        db_path=PROJECT_ROOT / "data" / "crawl" / "divisare.db",
        table="divisare_projects",
        id_col="id",
        cover_col="cover_image_url",
        gallery_col="gallery_urls",
    ),
    "architizer": SourceImageSpec(
        source="architizer",
        db_path=PROJECT_ROOT / "data" / "crawl" / "architizer.db",
        table="architizer_projects",
        id_col="id",
        cover_col="cover_image_url",
        gallery_col="gallery_image_urls",
    ),
    "archello": SourceImageSpec(
        source="archello",
        db_path=PROJECT_ROOT / "data" / "crawl" / "archello.db",
        table="archello_projects",
        id_col="id",
        cover_col="cover_image_url",
        gallery_col="gallery_image_urls",
    ),
    "metalocus": SourceImageSpec(
        source="metalocus",
        db_path=PROJECT_ROOT / "data" / "crawl" / "metalocus.db",
        table="buildings",
        id_col="id",
        cover_col="cover_image_url",
        gallery_col="gallery_image_urls",
        drawing_col="drawing_image_urls",
    ),
}


def _cache_key(source: str, source_id: str) -> str:
    return f"{source}:{source_id}"


def _read_json(path: Path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _canonical_rows(payload) -> list[dict]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        rows = payload.get("clusters") or payload.get("buildings") or []
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _parse_url_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [u.strip() for u in value if isinstance(u, str) and u.strip()]
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
        return [u.strip() for u in parsed if isinstance(u, str) and u.strip()]
    if isinstance(parsed, str) and parsed.strip():
        return [parsed.strip()]
    return []


def _source_priority(source: str) -> int:
    return SOURCE_PRIORITY.get(source, SOURCE_PRIORITY.get("unknown", 0))


def _make_image(source: str, source_id: str, url: str, kind: str, image_order: int) -> dict:
    return {
        "url": url,
        "source": source,
        "source_id": str(source_id),
        "kind": kind,
        "image_order": image_order,
        "source_priority": _source_priority(source),
    }


def _row_images(source: str, source_id: str, cover_url, gallery_urls, drawing_urls=None) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()

    def add(url: str, kind: str) -> None:
        clean = url.strip()
        if not clean or clean in seen:
            return
        seen.add(clean)
        out.append(_make_image(source, source_id, clean, kind, len(out)))

    if isinstance(cover_url, str) and cover_url.strip():
        add(cover_url, "cover")
    for url in _parse_url_list(gallery_urls):
        add(url, "gallery")
    for url in _parse_url_list(drawing_urls):
        add(url, "drawing")
    return out


def _load_phash_cache(path: Path = DEFAULT_PHASH_CACHE_PATH) -> dict[str, list[str]]:
    data = _read_json(path, {})
    if not isinstance(data, dict):
        return {}
    return {
        str(k): [v for v in values if isinstance(v, str) and v]
        for k, values in data.items()
        if isinstance(values, list)
    }


def _attach_cached_phashes(images: list[dict], cached_phashes: list[str]) -> None:
    """Attach source-row phashes to URL-ordered images when available.

    data/canonical/phash_cache.json is row-keyed, not URL-keyed. The builder
    emits phashes in cover + gallery order, so Stage E can avoid re-fetching
    those URLs when a positional phash is present. URLs beyond the cached prefix
    are fetched normally.
    """
    for image, phash in zip(images, cached_phashes):
        image["phash"] = phash


def _load_standard_source_index(
    spec: SourceImageSpec,
    phash_cache: dict[str, list[str]],
) -> dict[str, list[dict]]:
    if not spec.db_path.exists():
        return {}
    out: dict[str, list[dict]] = {}
    conn = sqlite3.connect(spec.db_path)
    conn.row_factory = sqlite3.Row
    try:
        drawing_select = f", {spec.drawing_col} AS drawing_urls" if spec.drawing_col else ", NULL AS drawing_urls"
        rows = conn.execute(
            f"SELECT {spec.id_col} AS source_id, "
            f"{spec.cover_col} AS cover_url, "
            f"{spec.gallery_col} AS gallery_urls"
            f"{drawing_select} "
            f"FROM {spec.table} "
            f"WHERE ({spec.cover_col} IS NOT NULL AND {spec.cover_col} != '') "
            f"   OR ({spec.gallery_col} IS NOT NULL AND {spec.gallery_col} != '')"
            + (f" OR ({spec.drawing_col} IS NOT NULL AND {spec.drawing_col} != '')" if spec.drawing_col else "")
        )
        for row in rows:
            source_id = str(row["source_id"])
            images = _row_images(
                spec.source,
                source_id,
                row["cover_url"],
                row["gallery_urls"],
                row["drawing_urls"],
            )
            if not images:
                continue
            key = _cache_key(spec.source, source_id)
            _attach_cached_phashes(images, phash_cache.get(key, []))
            out[key] = images
    finally:
        conn.close()
    return out


def _load_metalocus_source_index(
    spec: SourceImageSpec,
    phash_cache: dict[str, list[str]],
) -> dict[str, list[dict]]:
    if not spec.db_path.exists():
        return {}
    slug_to_building_id = _metalocus_slug_to_building_id()
    out: dict[str, list[dict]] = {}
    conn = sqlite3.connect(spec.db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT b.id AS db_id, a.slug AS slug, "
            "       b.cover_image_url AS cover_url, "
            "       b.gallery_image_urls AS gallery_urls, "
            "       b.drawing_image_urls AS drawing_urls "
            "FROM buildings b "
            "LEFT JOIN articles a ON b.article_id = a.id "
            "WHERE (b.cover_image_url IS NOT NULL AND b.cover_image_url != '') "
            "   OR (b.gallery_image_urls IS NOT NULL AND b.gallery_image_urls != '') "
            "   OR (b.drawing_image_urls IS NOT NULL AND b.drawing_image_urls != '')"
        )
        for row in rows:
            source_id = slug_to_building_id.get(row["slug"]) or str(row["db_id"])
            images = _row_images(
                spec.source,
                source_id,
                row["cover_url"],
                row["gallery_urls"],
                row["drawing_urls"],
            )
            if not images:
                continue
            key = _cache_key(spec.source, source_id)
            _attach_cached_phashes(images, phash_cache.get(key, []))
            out[key] = images
    finally:
        conn.close()
    return out


def load_source_image_index(
    *,
    source_specs: Optional[dict[str, SourceImageSpec]] = None,
    phash_cache: Optional[dict[str, list[str]]] = None,
) -> dict[str, list[dict]]:
    specs = source_specs or SOURCE_SPECS
    cache = phash_cache if phash_cache is not None else _load_phash_cache()
    out: dict[str, list[dict]] = {}
    for source, spec in specs.items():
        if source == "metalocus":
            out.update(_load_metalocus_source_index(spec, cache))
        else:
            out.update(_load_standard_source_index(spec, cache))
    return out


def collect_cluster_images(cluster: dict, source_index: dict[str, list[dict]]) -> list[dict]:
    refs = cluster.get("source_refs") or {}
    if not isinstance(refs, dict):
        return []
    out: list[dict] = []
    seen_urls: set[str] = set()
    for source, ids in refs.items():
        if not isinstance(ids, list):
            continue
        for source_id in ids:
            key = _cache_key(str(source), str(source_id))
            for image in source_index.get(key, []):
                url = image.get("url")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                out.append(dict(image))
    return out


def _fetch_missing_metadata(images: list[dict], fetcher: Callable[..., Optional[dict]]) -> None:
    for image in images:
        if image.get("phash"):
            image.setdefault("w", None)
            image.setdefault("h", None)
            image.setdefault("bytes", None)
            continue
        try:
            meta = fetcher(image["url"])
        except Exception:
            meta = None
        if not isinstance(meta, dict):
            meta = {}
        image["phash"] = meta.get("phash")
        image["w"] = meta.get("w")
        image["h"] = meta.get("h")
        image["bytes"] = meta.get("bytes")


def _clusters_for_images(images: list[dict]) -> list[list[int]]:
    if not images:
        return []
    phash_indices = [idx for idx, image in enumerate(images) if image.get("phash")]
    if not phash_indices:
        return [[idx] for idx in range(len(images))]

    phash_images = [images[idx] for idx in phash_indices]
    try:
        clusters = [
            [phash_indices[idx] for idx in cluster]
            for cluster in cluster_by_phash(phash_images, PHASH_THRESHOLD)
        ]
    except Exception:
        return [[idx] for idx in range(len(images))]
    for idx in range(len(images)):
        if idx not in phash_indices:
            clusters.append([idx])
    return clusters


def _filename_heuristic(url: str, kind: Optional[str] = None) -> Optional[str]:
    text = " ".join(part for part in (urlparse(url).path, url, kind or "") if part)
    if kind == "drawing" or _DRAWING_RE.search(text):
        return "drawing"
    if _AERIAL_RE.search(text):
        return "aerial"
    return None


def _download_to_tmp(url: str, *, timeout: int = 30) -> tuple[Optional[Path], bool]:
    path = Path(url)
    if path.exists():
        return path, False

    import requests

    suffix = Path(urlparse(url).path).suffix or ".jpg"
    fd, tmp_name = tempfile.mkstemp(prefix="stage_e_image_", suffix=suffix)
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }
        resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        resp.raise_for_status()
        tmp_path.write_bytes(resp.content)
        return tmp_path, True
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        return None, False


def vision_classify_image(url_or_path: str) -> str:
    image_path, should_delete = _download_to_tmp(url_or_path)
    if image_path is None:
        return "exterior"
    try:
        proc = subprocess.run(
            [
                "codex",
                "exec",
                "--skip-git-check",
                "-c",
                "model=gpt-5.5",
                "-c",
                "model_reasoning_effort=xhigh",
                "-c",
                "service_tier=fast",
                "-i",
                str(image_path),
                PROMPT_5TYPE,
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        answer = (proc.stdout or proc.stderr or "").strip().lower().split()
        if answer and answer[0] in IMAGE_TYPES:
            return answer[0]
        return "exterior"
    finally:
        if should_delete:
            try:
                image_path.unlink()
            except FileNotFoundError:
                pass


def classify_best_image(
    image: dict,
    *,
    classifier: Callable[[str], str] = vision_classify_image,
) -> str:
    heuristic = _filename_heuristic(image.get("url", ""), image.get("kind"))
    if heuristic:
        return heuristic
    try:
        label = classifier(image["url"]).strip().lower()
    except Exception:
        label = "exterior"
    return label if label in IMAGE_TYPES else "exterior"


def _annotate_clusters(
    images: list[dict],
    *,
    classifier: Callable[[str], str],
) -> list[dict]:
    clusters = _clusters_for_images(images)
    for cluster_id, indices in enumerate(clusters):
        ranked = rank_within_cluster(images, indices)
        best_idx = ranked[0]
        image_type = classify_best_image(images[best_idx], classifier=classifier)
        for rank, idx in enumerate(ranked):
            images[idx]["phash_cluster_id"] = cluster_id
            images[idx]["rank"] = rank
            images[idx]["type"] = image_type
    return images


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


def _pick_covers_by_type(images: list[dict]) -> dict[str, Optional[str]]:
    covers: dict[str, Optional[str]] = {image_type: None for image_type in IMAGE_TYPES}
    for image_type in IMAGE_TYPES:
        candidates = [image for image in images if image.get("type") == image_type]
        if candidates:
            covers[image_type] = sorted(candidates, key=_cover_sort_key)[0]["url"]
    return covers


def _public_image_record(image: dict) -> dict:
    return {
        "url": image.get("url"),
        "type": image.get("type"),
        "source": image.get("source"),
        "source_id": image.get("source_id"),
        "kind": image.get("kind"),
        "image_order": image.get("image_order"),
        "phash_cluster_id": image.get("phash_cluster_id"),
        "rank": image.get("rank"),
        "phash": image.get("phash"),
        "w": image.get("w"),
        "h": image.get("h"),
        "bytes": image.get("bytes"),
    }


def process_cluster(
    cluster: dict,
    *,
    source_index: dict[str, list[dict]],
    fetcher: Callable[..., Optional[dict]] = fetch_image_metadata,
    classifier: Callable[[str], str] = vision_classify_image,
) -> dict:
    cid = cluster.get("canonical_bld_id") or cluster.get("cid") or cluster.get("id")
    images = collect_cluster_images(cluster, source_index)
    _fetch_missing_metadata(images, fetcher)
    _annotate_clusters(images, classifier=classifier)
    return {
        "cid": cid,
        "all_images": [_public_image_record(image) for image in images],
        "covers_by_type": _pick_covers_by_type(images),
    }


def _load_done_cids(output_path: Path) -> set[str]:
    done: set[str] = set()
    if not output_path.exists():
        return done
    with output_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            cid = row.get("cid")
            if cid:
                done.add(str(cid))
    return done


def run_all(
    *,
    canonical_path: Path = DEFAULT_CANONICAL_PATH,
    output_path: Path = DEFAULT_OUTPUT_PATH,
    phash_cache_path: Path = DEFAULT_PHASH_CACHE_PATH,
    workers: int = 32,
    limit: Optional[int] = None,
    source_specs: Optional[dict[str, SourceImageSpec]] = None,
    fetcher: Callable[..., Optional[dict]] = fetch_image_metadata,
    classifier: Callable[[str], str] = vision_classify_image,
) -> dict[str, int]:
    payload = _read_json(canonical_path, {})
    rows = _canonical_rows(payload)
    done = _load_done_cids(output_path)
    pending = [row for row in rows if str(row.get("canonical_bld_id")) not in done]
    if limit is not None:
        pending = pending[:limit]

    phash_cache = _load_phash_cache(phash_cache_path)
    source_index = load_source_image_index(
        source_specs=source_specs,
        phash_cache=phash_cache,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "rows_total": len(rows),
        "rows_skipped_done": len(done),
        "rows_processed": 0,
        "images_written": 0,
    }

    def work(row: dict) -> dict:
        return process_cluster(
            row,
            source_index=source_index,
            fetcher=fetcher,
            classifier=classifier,
        )

    with output_path.open("a", encoding="utf-8") as f:
        with ThreadPoolExecutor(max_workers=max(workers, 1)) as ex:
            futures = {ex.submit(work, row): row for row in pending}
            for fut in as_completed(futures):
                rec = fut.result()
                f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
                f.flush()
                summary["rows_processed"] += 1
                summary["images_written"] += len(rec.get("all_images") or [])
                if summary["rows_processed"] % 100 == 0:
                    print(json.dumps(summary, sort_keys=True), flush=True)

    print(json.dumps(summary, indent=2, sort_keys=True), flush=True)
    return summary


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage E image dedupe + 5-type classification for canonical clusters."
    )
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
