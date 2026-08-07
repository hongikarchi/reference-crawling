"""Read-only source inventory adapters for the shared E1 fingerprint pipeline.

The adapters preserve source identity and URL provenance while selecting one
bounded, no-crop delivery URL. They do not fetch images, write to source
databases, or normalize pixels; :mod:`canonical.image_fingerprint` owns the
local raster and hash contract.
"""

from __future__ import annotations

import itertools
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence
from urllib.parse import urlencode, urlsplit, urlunsplit


SOURCE_MAX_EDGE = 1024
DIVISARE_FETCH_PROFILE = "c_limit,f_jpg,h_1024,q_85,w_1024"
DIVISARE_CONVERTIBLE_FETCH_PROFILE = (
    "pg_1,c_limit,f_jpg,h_1024,q_85,w_1024"
)
DIVISARE_FETCH_PROFILE_VERSION = "divisare-cloudinary-max1024-jpeg-v1"
ARCHITIZER_FETCH_PROFILE_VERSION = "architizer-imgix-max1024-jpeg-v1"

_DIVISARE_HOST = "images.divisare.com"
_ARCHITIZER_HOST = "architizer-prod.imgix.net"
_DIVISARE_SKIP_PATH_PARTS = ("/videos/", "/files/", "/raw/upload/")
_CONVERTIBLE_EXTENSIONS = frozenset(
    {"ai", "dwg", "dxf", "eps", "pdf", "psd", "svg"}
)
_HARD_SKIP_EXTENSIONS = frozenset(
    {
        "7z",
        "avi",
        "doc",
        "docx",
        "flac",
        "m4a",
        "mkv",
        "mov",
        "mp3",
        "mp4",
        "mpeg",
        "mpg",
        "ogg",
        "ppt",
        "pptx",
        "rar",
        "tar",
        "wav",
        "webm",
        "xls",
        "xlsx",
        "zip",
    }
)
_VERSION_SEGMENT = re.compile(r"v\d+", re.ASCII)
_ARCHITIZER_RENDER_KEYS = frozenset(
    {
        "ar",
        "auto",
        "bg",
        "blur",
        "bri",
        "ch",
        "chromasub",
        "con",
        "crop",
        "cs",
        "dpr",
        "dl",
        "duotone",
        "exp",
        "faceindex",
        "fill",
        "fit",
        "fm",
        "fp-debug",
        "fp-x",
        "fp-y",
        "gam",
        "h",
        "high",
        "invert",
        "lossless",
        "mark",
        "mark-align",
        "mark-alpha",
        "mark-fit",
        "mark-h",
        "mark-pad",
        "mark-rot",
        "mark-scale",
        "mark-w",
        "mark-x",
        "mark-y",
        "mask",
        "max-h",
        "max-w",
        "min-h",
        "min-w",
        "monochrome",
        "nr",
        "orient",
        "pad",
        "page",
        "palette",
        "q",
        "rect",
        "rot",
        "s",
        "sat",
        "sepia",
        "sharp",
        "trim",
        "trim-color",
        "trim-md",
        "trim-pad",
        "trim-sd",
        "usm",
        "usmrad",
        "vib",
        "w",
    }
)
_ARCHITIZER_PROFILE_PAIRS = (
    ("auto", "compress"),
    ("fit", "max"),
    ("fm", "jpg"),
    ("h", str(SOURCE_MAX_EDGE)),
    ("q", "85"),
    ("w", str(SOURCE_MAX_EDGE)),
)


@dataclass(frozen=True)
class SourceAsset:
    """One source-owned asset prepared for a shared local fingerprint run.

    ``selected_raw_url`` and ``source_urls`` are immutable provenance.
    ``effective_fetch_url`` is the bounded delivery request and must never be
    used as source identity. ``source_asset_id`` is the sidecar join key.
    """

    source: str
    source_asset_id: str
    source_asset_key: str
    normalized_url: str
    selected_raw_url: str
    effective_fetch_url: str
    source_urls: tuple[str, ...]
    occurrence_count: int
    parent_count: int
    roles: tuple[str, ...]
    format_lane: str
    fetch_profile_version: str


def _open_readonly(path: Path) -> sqlite3.Connection:
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    conn = sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _validate_limit(limit: int | None) -> None:
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _roles(row: sqlite3.Row) -> tuple[str, ...]:
    return tuple(
        role
        for role, column in (("cover", "has_cover"), ("gallery", "has_gallery"))
        if int(row[column] or 0)
    )


def _suffix(value: str | None) -> str:
    if not value:
        return ""
    path = urlsplit(value).path if "://" in value else value
    leaf = path.rstrip("/").rsplit("/", 1)[-1]
    if "." not in leaf:
        return ""
    return leaf.rsplit(".", 1)[-1].casefold()


def _divisare_resource_extensions(
    original_filename: str | None, urls: Sequence[str]
) -> set[str]:
    extensions = {_suffix(original_filename)} if _suffix(original_filename) else set()
    for url in urls:
        parts = [part for part in urlsplit(url).path.split("/") if part]
        if parts:
            extension = _suffix(parts[-1])
            if extension:
                extensions.add(extension)
        try:
            project_index = parts.index("project_images")
        except ValueError:
            continue
        if project_index + 2 < len(parts):
            extension = _suffix(parts[project_index + 2])
            if extension:
                extensions.add(extension)
    return extensions


def _divisare_supported_source_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    path = parsed.path.casefold()
    return (
        parsed.scheme.casefold() == "https"
        and (parsed.hostname or "").casefold().rstrip(".") == _DIVISARE_HOST
        and parsed.username is None
        and parsed.password is None
        and port is None
        and ("/images/" in path or "/image/upload/" in path)
        and not any(part in path for part in _DIVISARE_SKIP_PATH_PARTS)
    )


def _divisare_url_rank(url: str) -> tuple[int, int, str]:
    path = urlsplit(url).path.casefold()
    return (0 if "/image/upload/" in path else 1, len(url), url)


def divisare_effective_fetch_url(source_url: str, *, convertible: bool = False) -> str:
    """Return a bounded Cloudinary derivative without crop normalization."""

    if not _divisare_supported_source_url(source_url):
        raise ValueError("unsupported Divisare image source URL")
    parsed = urlsplit(source_url)
    parts = [part for part in parsed.path.split("/") if part]
    if parts[:1] == ["images"]:
        anchor = 0
    elif parts[:2] == ["image", "upload"]:
        anchor = 1
    else:
        raise ValueError("unsupported Divisare Cloudinary path")

    profile = (
        DIVISARE_CONVERTIBLE_FETCH_PROFILE
        if convertible
        else DIVISARE_FETCH_PROFILE
    )
    if anchor + 1 >= len(parts):
        raise ValueError("Divisare URL is missing a delivery version")
    if _VERSION_SEGMENT.fullmatch(parts[anchor + 1]):
        parts.insert(anchor + 1, profile)
    elif anchor + 2 < len(parts) and _VERSION_SEGMENT.fullmatch(parts[anchor + 2]):
        parts[anchor + 1] = profile
    else:
        raise ValueError("unrecognized Divisare Cloudinary transform layout")
    return urlunsplit(
        ("https", _DIVISARE_HOST, "/" + "/".join(parts), parsed.query, "")
    )


def architizer_effective_fetch_url(normalized_url: str) -> str:
    """Return an HTTPS Imgix URL bounded to 1024px with no crop."""

    try:
        parsed = urlsplit(normalized_url)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid Architizer image URL") from exc
    host = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or host != _ARCHITIZER_HOST
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or not parsed.path.startswith("/")
        or parsed.path == "/"
        or "\\" in parsed.path
        or bool(parsed.query)
        or bool(parsed.fragment)
    ):
        raise ValueError("unsupported Architizer Imgix URL")

    query = urlencode(sorted(_ARCHITIZER_PROFILE_PAIRS))
    return urlunsplit(("https", host, parsed.path, query, ""))


_DIVISARE_QUERY = """
WITH selected_assets AS MATERIALIZED (
  SELECT asset_key
  FROM image_assets
  ORDER BY asset_key
), occurrence_summary AS (
  SELECT asset_key,
         COUNT(*) AS occurrence_count,
         COUNT(DISTINCT article_id) AS parent_count,
         MAX(CASE WHEN role='cover' THEN 1 ELSE 0 END) AS has_cover,
         MAX(CASE WHEN role='gallery' THEN 1 ELSE 0 END) AS has_gallery
  FROM source_image_occurrences
  JOIN selected_assets USING (asset_key)
  WHERE parse_status='parsed' AND asset_key IS NOT NULL
  GROUP BY asset_key
), source_urls AS (
  SELECT asset_key,url AS source_url,0 AS source_kind,
         url_id AS first_order,'' AS second_order,0 AS third_order
  FROM image_urls
  JOIN selected_assets USING (asset_key)
  UNION ALL
  SELECT asset_key,raw_url AS source_url,1 AS source_kind,
         article_id AS first_order,role AS second_order,position AS third_order
  FROM source_image_occurrences
  JOIN selected_assets USING (asset_key)
  WHERE parse_status='parsed' AND asset_key IS NOT NULL
)
SELECT ia.asset_key,ia.original_filename,ia.url_generation,su.source_url,
       COALESCE(os.occurrence_count,0) AS occurrence_count,
       COALESCE(os.parent_count,0) AS parent_count,
       COALESCE(os.has_cover,0) AS has_cover,
       COALESCE(os.has_gallery,0) AS has_gallery
FROM selected_assets AS selected
JOIN image_assets AS ia ON ia.asset_key=selected.asset_key
JOIN source_urls AS su ON su.asset_key=ia.asset_key
LEFT JOIN occurrence_summary AS os ON os.asset_key=ia.asset_key
ORDER BY ia.asset_key,su.source_kind,su.first_order,su.second_order,su.third_order,
         su.source_url
"""


def iter_divisare_source_assets(
    db_path: Path, *, limit: int | None = None
) -> Iterator[SourceAsset]:
    """Yield eligible Divisare assets from curated v2.4 in stable key order."""

    _validate_limit(limit)
    conn = _open_readonly(Path(db_path))
    yielded = 0
    try:
        rows = conn.execute(_DIVISARE_QUERY)
        for asset_key, grouped in itertools.groupby(rows, key=lambda row: row["asset_key"]):
            batch = list(grouped)
            first = batch[0]
            source_urls = _unique(str(row["source_url"]) for row in batch)
            extensions = _divisare_resource_extensions(
                first["original_filename"], source_urls
            )
            if extensions & _HARD_SKIP_EXTENSIONS:
                continue
            candidates = [url for url in source_urls if _divisare_supported_source_url(url)]
            if not candidates:
                continue
            selected = min(candidates, key=_divisare_url_rank)
            convertible = bool(extensions & _CONVERTIBLE_EXTENSIONS)
            try:
                effective = divisare_effective_fetch_url(
                    selected, convertible=convertible
                )
            except ValueError:
                continue
            yield SourceAsset(
                source="divisare",
                source_asset_id=str(asset_key),
                source_asset_key=str(asset_key),
                normalized_url=selected,
                selected_raw_url=selected,
                effective_fetch_url=effective,
                source_urls=source_urls,
                occurrence_count=int(first["occurrence_count"]),
                parent_count=int(first["parent_count"]),
                roles=_roles(first),
                format_lane="convertible" if convertible else "raster",
                fetch_profile_version=DIVISARE_FETCH_PROFILE_VERSION,
            )
            yielded += 1
            if limit is not None and yielded >= limit:
                break
    finally:
        conn.close()


_ARCHITIZER_QUERY = """
WITH selected_assets AS MATERIALIZED (
  SELECT a.asset_id
  FROM image_assets AS a
  WHERE a.is_placeholder_candidate=0
  ORDER BY a.asset_id
), occurrence_summary AS (
  SELECT asset_id,
         COUNT(*) AS occurrence_count,
         COUNT(DISTINCT source_project_id) AS parent_count,
         MAX(CASE WHEN role='cover' THEN 1 ELSE 0 END) AS has_cover,
         MAX(CASE WHEN role='gallery' THEN 1 ELSE 0 END) AS has_gallery
  FROM source_image_occurrences
  JOIN selected_assets USING (asset_id)
  WHERE parse_status!='malformed' AND asset_id IS NOT NULL
  GROUP BY asset_id
), source_urls AS (
  SELECT asset_id,raw_url AS source_url,0 AS source_kind,
         image_url_id AS first_order,'' AS second_order,0 AS third_order
  FROM image_urls
  JOIN selected_assets USING (asset_id)
  UNION ALL
  SELECT asset_id,raw_url AS source_url,1 AS source_kind,
         source_project_id AS first_order,role AS second_order,ordinal AS third_order
  FROM source_image_occurrences
  JOIN selected_assets USING (asset_id)
  WHERE parse_status!='malformed' AND asset_id IS NOT NULL
)
SELECT a.asset_id,a.asset_key,a.normalized_url,a.host,a.path,su.source_url,
       COALESCE(os.occurrence_count,0) AS occurrence_count,
       COALESCE(os.parent_count,0) AS parent_count,
       COALESCE(os.has_cover,0) AS has_cover,
       COALESCE(os.has_gallery,0) AS has_gallery
FROM selected_assets AS selected
JOIN image_assets AS a ON a.asset_id=selected.asset_id
JOIN source_urls AS su ON su.asset_id=a.asset_id
LEFT JOIN occurrence_summary AS os ON os.asset_id=a.asset_id
ORDER BY a.asset_id,su.source_kind,su.first_order,su.second_order,su.third_order,
         su.source_url
"""


def iter_architizer_source_assets(
    db_path: Path, *, limit: int | None = None
) -> Iterator[SourceAsset]:
    """Yield queued, non-placeholder Architizer assets once per asset ID."""

    _validate_limit(limit)
    conn = _open_readonly(Path(db_path))
    yielded = 0
    try:
        rows = conn.execute(_ARCHITIZER_QUERY)
        for asset_id, grouped in itertools.groupby(rows, key=lambda row: row["asset_id"]):
            batch = list(grouped)
            first = batch[0]
            source_urls = _unique(str(row["source_url"]) for row in batch)
            extension = _suffix(str(first["path"]))
            if extension in _HARD_SKIP_EXTENSIONS:
                continue
            try:
                effective = architizer_effective_fetch_url(
                    str(first["normalized_url"])
                )
            except ValueError:
                continue
            selected = min(source_urls, key=lambda url: (len(url), url))
            yield SourceAsset(
                source="architizer",
                source_asset_id=str(asset_id),
                source_asset_key=str(first["asset_key"]),
                normalized_url=str(first["normalized_url"]),
                selected_raw_url=selected,
                effective_fetch_url=effective,
                source_urls=source_urls,
                occurrence_count=int(first["occurrence_count"]),
                parent_count=int(first["parent_count"]),
                roles=_roles(first),
                format_lane=(
                    "convertible" if extension in _CONVERTIBLE_EXTENSIONS else "raster"
                ),
                fetch_profile_version=ARCHITIZER_FETCH_PROFILE_VERSION,
            )
            yielded += 1
            if limit is not None and yielded >= limit:
                break
    finally:
        conn.close()
