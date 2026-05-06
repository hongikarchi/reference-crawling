"""Phase 15 phash false-merge gate for building matching."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Iterable

import imagehash
from rapidfuzz import fuzz


PHASH_CACHE_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "canonical" / "phash_cache.json"
)
_MISSING = object()


def _cache_key(source: str, source_id: str) -> str:
    return f"{source}:{source_id}"


@lru_cache(maxsize=1)
def _load_cache() -> dict[str, list[str]] | None:
    """Return the phash cache, or None when it has not been built yet."""
    path = Path(PHASH_CACHE_PATH)
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return {}
    return data


def _source_phashes(cache: dict[str, list[str]], source: str, ids: Iterable[str]) -> list[str]:
    phashes: list[str] = []
    for source_id in ids:
        values = cache.get(_cache_key(source, str(source_id)), [])
        if isinstance(values, list):
            phashes.extend(v for v in values if isinstance(v, str) and v)
    return phashes


def _hamming_distance(a_hex: str, b_hex: str) -> int:
    return imagehash.hex_to_hash(a_hex) - imagehash.hex_to_hash(b_hex)


def _overlap_count(a_phashes: list[str], b_phashes: list[str], threshold: int) -> int:
    """Count cross-source phash clusters with at least one image from each side."""
    parent = list(range(len(a_phashes) + len(b_phashes)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri = find(i)
        rj = find(j)
        if ri != rj:
            parent[rj] = ri

    all_phashes = a_phashes + b_phashes
    for i in range(len(all_phashes)):
        for j in range(i + 1, len(all_phashes)):
            if _hamming_distance(all_phashes[i], all_phashes[j]) <= threshold:
                union(i, j)

    clusters: dict[int, set[str]] = {}
    for idx in range(len(all_phashes)):
        side = "a" if idx < len(a_phashes) else "b"
        clusters.setdefault(find(idx), set()).add(side)

    return sum(1 for sides in clusters.values() if sides == {"a", "b"})


def _parse_year(value: object) -> int | None:
    if value is None or value is _MISSING:
        return None
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    return year


def _text_confirms_block(
    *,
    name_a: object = _MISSING,
    name_b: object = _MISSING,
    year_a: object = _MISSING,
    year_b: object = _MISSING,
) -> bool | None:
    """Return text disagreement decision, or None if text was not supplied."""
    if name_a is _MISSING or name_b is _MISSING:
        return None
    if year_a is _MISSING or year_b is _MISSING:
        return None

    name_sim = fuzz.token_set_ratio(str(name_a or ""), str(name_b or ""))
    name_disagrees = name_sim < 90

    ya = _parse_year(year_a)
    yb = _parse_year(year_b)
    year_disagrees = ya is None or yb is None or ya != yb
    return name_disagrees and year_disagrees


def has_phash_overlap(
    src_a_ids: Iterable[str],
    src_b_ids: Iterable[str],
    src_a: str,
    src_b: str,
    threshold: int = 8,
    *,
    name_a: object = _MISSING,
    name_b: object = _MISSING,
    year_a: object = _MISSING,
    year_b: object = _MISSING,
) -> dict[str, int | str]:
    """Return whether two source-id sets share at least one visual image.

    BLOCK is only emitted when both sides have enough images to make the
    absence of overlap meaningful.
    """
    cache = _load_cache()
    if cache is None:
        return {"verdict": "PASS", "reason": "no cache"}

    a_phashes = _source_phashes(cache, src_a, src_a_ids)
    b_phashes = _source_phashes(cache, src_b, src_b_ids)
    a_n = len(a_phashes)
    b_n = len(b_phashes)
    overlap = _overlap_count(a_phashes, b_phashes, threshold)
    phash_blocks = a_n >= 2 and b_n >= 2 and overlap == 0
    verdict = "PASS"
    if phash_blocks:
        text_blocks = _text_confirms_block(
            name_a=name_a,
            name_b=name_b,
            year_a=year_a,
            year_b=year_b,
        )
        if text_blocks is None or text_blocks:
            verdict = "BLOCK"
        else:
            verdict = "TIEBREAKER_PASS"

    return {
        "verdict": verdict,
        "overlap": overlap,
        "a_n": a_n,
        "b_n": b_n,
    }
