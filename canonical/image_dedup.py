"""Phase 10 — Cross-source image dedupe + quality ranking.

For each canonical building, gather image URLs from every matched source,
compute perceptual hashes, cluster near-duplicates within the building,
pick the highest-quality URL per cluster.

Output: data/canonical/canonical_image_gallery.json — per-building list
of deduped {url, source, kind, w, h, bytes, phash, cluster_id, rank}.
Subsequent canonical/build.py runs read this to set the canonical row's
final cover_image_url + gallery_image_urls.

Dependencies (in requirements.txt as of Phase 9 commit):
  imagehash >= 4.3.1
  Pillow    >= 10.0.0

Cost / time:
  • ~25K-200K image fetches (1 per source URL on each canonical row),
    each ~500 KB-2 MB
  • 8-parallel: ~2-4 hours for the strict canonical 2,523 rows;
    full multi-source after Phase 9.5 will be longer (~8-12 hours)
  • CPU-bound after fetch (phash compute ~10 ms each)

Source priority (used as tertiary tiebreaker after dimensions + bytes):
  divisare > architizer > archello > metalocus

This module is pure Python — no AI calls. Downloads images, computes
hashes, discards bytes (keeps only metadata).
"""

from __future__ import annotations

import io
import json
import os
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
from urllib.parse import urlparse

# Lazy-import heavyweight deps only when actually fetching
# (so smoke tests + module imports stay free)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CANONICAL_PATH = "data/canonical/canonical_buildings_strict.json"
OUTPUT_PATH    = "data/canonical/canonical_image_gallery.json"

# Hamming-distance threshold for "same image" within a building.
# 256-bit hash (hash_size=16) → ≤ 8 = ~3% bit difference = visually same.
PHASH_HASH_SIZE = 16
PHASH_THRESHOLD = 8

# Max image size to download (skip oversized — likely raw architectural
# print files of 50+ MB which we don't need)
MAX_IMAGE_BYTES = 8 * 1024 * 1024

# Per-source priority for tiebreaks (higher = better)
SOURCE_PRIORITY = {
    "divisare":   4,
    "architizer": 3,
    "archello":   2,
    "metalocus":  1,
    "unknown":    0,
}

# URL hostname → source mapping
_HOST_RE = re.compile(r"^(?:[a-zA-Z0-9-]+\.)*([^.]+)\.[^.]+$")
_HOST_TO_SOURCE = {
    "divisare":          "divisare",
    "architizer-prod":   "architizer",
    "imgix":             "architizer",   # architizer uses imgix CDN
    "architizer":        "architizer",
    "archello":          "archello",
    "amazonaws":         "archello",     # archello stores on s3
    "metalocus":         "metalocus",
}


def _classify_source(url: str) -> str:
    """Best-effort source identification from URL hostname."""
    try:
        host = urlparse(url).netloc.lower()
        for needle, src in _HOST_TO_SOURCE.items():
            if needle in host:
                return src
    except Exception:
        pass
    return "unknown"


# ---------------------------------------------------------------------------
# Image-type classification (5 types): exterior / interior / drawing /
# aerial / detail. See ~/.claude/plans/db-fuzzy-lerdorf.md Phase 13/14
# decision #2 for rationale.
# ---------------------------------------------------------------------------

# Five image-type categories used to populate covers_by_type per canonical
# row. make_web picks the displayed cover based on user intent (Task #19).
IMAGE_TYPES = ("exterior", "interior", "drawing", "aerial", "detail")

# Filename-keyword cascades. First matching cascade wins (drawing > aerial >
# interior > detail > exterior fallback). Strings are compared in lowercase
# against the filename + URL path + alt_text concatenation.
_DRAWING_KEYWORDS = (
    "plan", "section", "elevation", "axonometr", "diagram", "sketch",
    "drawing", "constructive", "isometric",
)
_AERIAL_KEYWORDS  = ("aerial", "drone", "birdseye", "bird-eye", "topdown", "top-down", "rooftop")
_INTERIOR_KEYWORDS = ("interior", "indoor", "lobby", "kitchen", "bedroom",
                      "living", "bathroom", "hall", "atrium", "corridor")
_DETAIL_KEYWORDS  = ("detail", "closeup", "close-up", "texture", "joint", "macro")


def classify_image_type(
    url: str,
    *,
    alt_text: Optional[str] = None,
    kind_hint: Optional[str] = None,
) -> tuple[str, str]:
    """Filename-based 5-type classifier (no LLM).

    Returns (type, confidence): type ∈ IMAGE_TYPES.
    confidence ∈ {'high', 'low'}. 'low' means the heuristic defaulted to
    'exterior' without a positive signal — caller may run a Vision pass
    to refine.

    kind_hint is the source-supplied broad bucket ('cover' / 'gallery' /
    'drawing'). 'drawing' kind is treated as a high-confidence drawing.
    """
    # Strong source signal: metalocus already split drawings from photos
    if kind_hint == "drawing":
        return "drawing", "high"

    # Combined search text (lowercased)
    text = (url + " " + (alt_text or "")).lower()

    for kw in _DRAWING_KEYWORDS:
        if kw in text:
            return "drawing", "high"
    for kw in _AERIAL_KEYWORDS:
        if kw in text:
            return "aerial", "high"
    for kw in _INTERIOR_KEYWORDS:
        if kw in text:
            return "interior", "high"
    for kw in _DETAIL_KEYWORDS:
        if kw in text:
            return "detail", "high"
    # Default — exterior is the dominant category for architecture photos.
    # Mark as 'low' so a Vision pass can refine if cost-approved.
    return "exterior", "low"


def pick_per_type_covers(images: list[dict]) -> dict[str, dict]:
    """Given the deduped image list (each dict has at minimum
    {url, source, type, image_order, source_priority}), pick the single
    best image per type. Tiebreak per Phase 13/14 decision #5:
       (1) image_order ascending (cover_order=0 first)
       (2) source priority (architizer > divisare > archello > metalocus)
    Returns {type: image_dict} for types that have at least one image.
    """
    by_type: dict[str, list[dict]] = defaultdict(list)
    for img in images:
        t = img.get("type") or "exterior"
        by_type[t].append(img)

    out: dict[str, dict] = {}
    for t, imgs in by_type.items():
        ranked = sorted(imgs, key=lambda im: (
            im.get("image_order", 999),                     # cover order=0 first
            -SOURCE_PRIORITY.get(im.get("source"), 0),      # higher priority first
        ))
        out[t] = ranked[0]
    return out


# ---------------------------------------------------------------------------
# Per-row image enumeration
# ---------------------------------------------------------------------------

def collect_image_candidates(row: dict) -> list[dict]:
    """For one canonical row, return a flat list of image candidates with
    source + kind attribution. Each candidate is a dict:
      {url, source, kind}
    where kind ∈ {'cover', 'gallery', 'drawing'}.

    Currently reads:
      • cover_image_url, gallery_image_urls, drawing_image_urls
        (Phase 11.0 unified URL fields)
      • cover_image_url_divisare, divisare_gallery_urls
        (legacy Divisare-specific fields)

    After Phase 9.5 (multi-source canonical), this should also read
    Architizer / Archello image URL maps. Until then, those are
    inferred from URL hostname.
    """
    out: list[dict] = []
    seen_urls: set[str] = set()

    def _add(url: Optional[str], kind: str) -> None:
        if not url or not isinstance(url, str):
            return
        if url in seen_urls:
            return
        seen_urls.add(url)
        out.append({
            "url": url,
            "source": _classify_source(url),
            "kind": kind,
        })

    # Phase 11.0 unified fields
    _add(row.get("cover_image_url"), "cover")
    for u in (row.get("gallery_image_urls") or []):
        _add(u, "gallery")
    for u in (row.get("drawing_image_urls") or []):
        _add(u, "drawing")
    # Legacy Divisare-specific fields (kept on existing canonical for
    # backward compat; Phase 9.5 may eventually consolidate)
    _add(row.get("cover_image_url_divisare"), "cover")
    for u in (row.get("divisare_gallery_urls") or []):
        _add(u, "gallery")

    return out


# ---------------------------------------------------------------------------
# Image metadata (perceptual hash + dimensions + size)
# ---------------------------------------------------------------------------

def fetch_image_metadata(url: str, *, timeout: int = 30,
                         max_bytes: int = MAX_IMAGE_BYTES) -> Optional[dict]:
    """Download an image, return {phash, w, h, bytes} or None on failure.
    Discards the raw bytes — we keep metadata only."""
    import imagehash
    import requests
    from PIL import Image

    try:
        headers = {
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0.0.0 Safari/537.36"),
        }
        resp = requests.get(url, headers=headers, stream=True, timeout=timeout)
        resp.raise_for_status()
        buf = io.BytesIO()
        total = 0
        for chunk in resp.iter_content(chunk_size=16384):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                return None
            buf.write(chunk)
        raw = buf.getvalue()
        img = Image.open(io.BytesIO(raw))
        img.load()
        if img.mode != "RGB":
            img = img.convert("RGB")
        ph = imagehash.phash(img, hash_size=PHASH_HASH_SIZE)
        return {
            "phash": str(ph),
            "w": img.width,
            "h": img.height,
            "bytes": total,
        }
    except Exception:
        return None


def fetch_image_metadata_parallel(urls: list[str], max_workers: int = 8) -> dict:
    """Fetch metadata for many URLs in parallel. Returns {url: meta or None}."""
    out: dict = {}
    if not urls:
        return out
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(fetch_image_metadata, u): u for u in urls}
        for fut in as_completed(futures):
            url = futures[fut]
            try:
                out[url] = fut.result()
            except Exception:
                out[url] = None
    return out


# ---------------------------------------------------------------------------
# Cluster + rank within a building
# ---------------------------------------------------------------------------

def _hamming(a_hex: str, b_hex: str) -> int:
    """Hamming distance between two hex hash strings."""
    import imagehash
    a = imagehash.hex_to_hash(a_hex)
    b = imagehash.hex_to_hash(b_hex)
    return a - b


def cluster_by_phash(images: list[dict],
                     threshold: int = PHASH_THRESHOLD) -> list[list[int]]:
    """Group images by perceptual similarity. Returns a list of clusters,
    each cluster is a list of indices into `images`. Linear union-find
    approach: O(n²) but n is small per building (<100)."""
    n = len(images)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        for j in range(i + 1, n):
            ph_i = images[i].get("phash")
            ph_j = images[j].get("phash")
            if not ph_i or not ph_j:
                continue
            if _hamming(ph_i, ph_j) <= threshold:
                union(i, j)

    clusters: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        clusters[find(i)].append(i)
    return list(clusters.values())


def rank_within_cluster(images: list[dict], indices: list[int]) -> list[int]:
    """Sort cluster indices best-to-worst. Tiebreakers in order:
       1. larger pixel area (w * h)
       2. larger byte size
       3. higher source priority
    """
    def key(i: int):
        img = images[i]
        area = (img.get("w") or 0) * (img.get("h") or 0)
        size = img.get("bytes") or 0
        src_pri = SOURCE_PRIORITY.get(img.get("source", "unknown"), 0)
        return (-area, -size, -src_pri)
    return sorted(indices, key=key)


def dedupe_building(row: dict, *, fetch_metadata: bool = True,
                    metadata_cache: Optional[dict] = None) -> dict:
    """Process one canonical row — return a per-building dedup record:
      {
        "metalocus_building_id": "...",
        "name":                   "...",
        "n_input_urls":           17,
        "n_output_clusters":      6,
        "best_cover_url":         "https://...",  # winner of cover-kind cluster
        "images":                 [
            {url, source, kind, phash, w, h, bytes, cluster_id, rank,
             also_in_sources: [...]},
            ...
        ]
      }

    If `fetch_metadata=False`, skip fetching (returns candidates with
    NULL metadata; useful for dry-run / structure inspection).
    `metadata_cache` if provided is a dict url→meta to skip re-fetches.
    """
    candidates = collect_image_candidates(row)
    cache = metadata_cache if metadata_cache is not None else {}

    if fetch_metadata:
        # Only fetch URLs not already in cache
        urls_to_fetch = [c["url"] for c in candidates if c["url"] not in cache]
        new_meta = fetch_image_metadata_parallel(urls_to_fetch)
        cache.update(new_meta)

    images = []
    for c in candidates:
        meta = cache.get(c["url"]) if fetch_metadata else None
        images.append({**c, **(meta or {"phash": None, "w": None,
                                         "h": None, "bytes": None})})

    # Cluster (skip rows where no metadata succeeded — pHash is None)
    clusters = cluster_by_phash(
        [i for i in images if i.get("phash")],
    ) if any(i.get("phash") for i in images) else [[i] for i in range(len(images))]
    # Re-map cluster indices to absolute image indices when filtered
    if any(i.get("phash") for i in images):
        ph_indices = [n for n, i in enumerate(images) if i.get("phash")]
        clusters = [[ph_indices[c] for c in cl] for cl in clusters]
        # Add NO-PHASH images each as their own singleton cluster
        for n, i in enumerate(images):
            if not i.get("phash"):
                clusters.append([n])

    # Rank inside each cluster
    cluster_id = 0
    annotated = list(images)
    for cl in clusters:
        ranked = rank_within_cluster(annotated, cl)
        also_in = [annotated[idx]["source"] for idx in cl]
        for rank_idx, ann_idx in enumerate(ranked):
            annotated[ann_idx]["cluster_id"] = cluster_id
            annotated[ann_idx]["rank"] = rank_idx
            annotated[ann_idx]["also_in_sources"] = sorted(set(also_in))
        cluster_id += 1

    # Best cover: rank=0 image among kind='cover' clusters
    best_cover_url = None
    for img in annotated:
        if img.get("kind") == "cover" and img.get("rank") == 0:
            best_cover_url = img["url"]
            break
    # Fallback: best of any cluster if no cover-kind survived
    if not best_cover_url and annotated:
        best_first_rank = next((i for i in annotated if i.get("rank") == 0), None)
        if best_first_rank:
            best_cover_url = best_first_rank["url"]

    return {
        "metalocus_building_id": row.get("metalocus_building_id"),
        "name":                  row.get("name"),
        "n_input_urls":          len(candidates),
        "n_output_clusters":     len(clusters),
        "best_cover_url":        best_cover_url,
        "images":                annotated,
    }


# ---------------------------------------------------------------------------
# Aggregate runner
# ---------------------------------------------------------------------------

def run_all(canonical_path: str = CANONICAL_PATH,
            output_path: str = OUTPUT_PATH,
            *,
            fetch_metadata: bool = True,
            limit: Optional[int] = None) -> dict:
    """Process every canonical row, write per-building dedup record to
    `output_path`. Uses a global metadata cache so the same URL across
    rows (rare but possible) is fetched once.
    """
    print(f"loading canonical from {canonical_path}...")
    with open(canonical_path) as f:
        rows = json.load(f)
    if limit:
        rows = rows[:limit]
    print(f"  {len(rows)} canonical rows")

    cache: dict = {}  # url → meta
    output: list[dict] = []

    total_urls = total_unique_clusters = 0

    for i, row in enumerate(rows, 1):
        rec = dedupe_building(row, fetch_metadata=fetch_metadata,
                              metadata_cache=cache)
        output.append(rec)
        total_urls += rec["n_input_urls"]
        total_unique_clusters += rec["n_output_clusters"]
        if i % 50 == 0 or i == len(rows):
            print(f"  processed {i}/{len(rows)}  "
                  f"(total urls so far: {total_urls}, "
                  f"unique clusters: {total_unique_clusters}, "
                  f"cache size: {len(cache)})")

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    tmp = output_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=1)
    os.replace(tmp, output_path)

    print(f"\n✓ wrote {output_path}")
    summary = {
        "rows_processed":      len(rows),
        "total_input_urls":    total_urls,
        "total_unique_clusters": total_unique_clusters,
        "dedupe_ratio":        round(
            (total_urls - total_unique_clusters) / max(total_urls, 1), 3),
        "metadata_cache_size": len(cache),
    }
    print(json.dumps(summary, indent=2))
    return summary


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    """Smoke + run."""
    import argparse
    p = argparse.ArgumentParser(description="Phase 10 image dedupe")
    p.add_argument("--canonical", default=CANONICAL_PATH)
    p.add_argument("--output", default=OUTPUT_PATH)
    p.add_argument("--limit", type=int, default=None,
                   help="process only first N rows (for smoke / cost test)")
    p.add_argument("--no-fetch", action="store_true",
                   help="skip image fetching (structure-only output, NULL "
                        "phash/w/h/bytes; useful to confirm wiring)")
    p.add_argument("--smoke", action="store_true",
                   help="run a tiny in-process test (no canonical / no fetch)")
    args = p.parse_args(argv)

    if args.smoke:
        return _run_smoke()
    return 0 if run_all(args.canonical, args.output,
                        fetch_metadata=not args.no_fetch,
                        limit=args.limit) else 1


def _run_smoke() -> int:
    """In-process test with synthetic phashes."""
    print("=== Phase 10 image_dedup smoke test (no fetch, synthetic phashes) ===\n")
    fake_row = {
        "metalocus_building_id": "B00001",
        "name": "Test Building",
        "cover_image_url":            "https://divisare.com/abc/cover.jpg",
        "gallery_image_urls":         [
            "https://architizer-prod.imgix.net/abc/photo1.jpg",
            "https://archello.s3.amazonaws.com/abc/photo2.jpg",
        ],
        "drawing_image_urls":         [
            "https://www.metalocus.es/abc/plan.jpg",
        ],
        "cover_image_url_divisare":   "https://divisare.com/abc/cover.jpg",  # dup of cover
    }
    rec = dedupe_building(fake_row, fetch_metadata=False)
    print(f"input urls: {rec['n_input_urls']}")
    print(f"output clusters (without phash, all singletons): {rec['n_output_clusters']}")
    print(f"best_cover_url: {rec['best_cover_url']!r}")
    print(f"\nimage candidates:")
    for img in rec["images"]:
        print(f"  [{img['source']:>10s} / {img['kind']:>7s}]  "
              f"cluster={img['cluster_id']}  rank={img['rank']}  "
              f"url={img['url'][:60]}")
    # Sanity assertions
    assert rec["n_input_urls"] == 4, "duplicate URL should be deduped at collect"
    sources_seen = {img["source"] for img in rec["images"]}
    assert "divisare" in sources_seen
    assert "architizer" in sources_seen
    assert "archello" in sources_seen
    assert "metalocus" in sources_seen
    print("\n✓ All assertions pass")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
