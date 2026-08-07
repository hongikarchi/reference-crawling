"""Read-only source inventory adapters for the shared E1 fingerprint pipeline.

The adapters preserve source identity and URL provenance while selecting one
bounded, no-crop delivery URL. They do not fetch images, write to source
databases, or normalize pixels; :mod:`canonical.image_fingerprint` owns the
local raster and hash contract.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Sequence, TypeAlias
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


@dataclass(frozen=True)
class SourceAssetExclusion:
    """One source-owned asset deliberately excluded from E1 processing.

    ``source_record_json`` is a canonical serialization of the source facts
    used by the adapter.  ``detail_json`` records the mutually-exclusive
    exclusion reason and supporting evidence without changing source identity.
    Both strings are stored verbatim in the sidecar exclusion ledger.
    """

    source: str
    source_asset_id: str
    source_asset_key: str
    reason_code: str
    source_record_json: str
    detail_json: str

    @property
    def source_record_sha256(self) -> str:
        return source_record_sha256(self.source_record_json)


InventoryDecision: TypeAlias = SourceAsset | SourceAssetExclusion


def canonical_source_record_json(value: Mapping[str, object]) -> str:
    """Serialize one adapter source record with a stable byte contract."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def source_record_sha256(source_record_json: str) -> str:
    """Return the SHA-256 of an exact canonical source-record string."""

    if not isinstance(source_record_json, str) or not source_record_json:
        raise ValueError("source_record_json must be a non-empty string")
    return hashlib.sha256(source_record_json.encode("ascii")).hexdigest()


def source_asset_record(asset: SourceAsset) -> dict[str, object]:
    """Return the source-record payload shared by runner and validator."""

    return {
        "fetch_profile_version": asset.fetch_profile_version,
        "format_lane": asset.format_lane,
        "normalized_url": asset.normalized_url,
        "occurrence_count": asset.occurrence_count,
        "parent_count": asset.parent_count,
        "roles": list(asset.roles),
        "selected_raw_url": asset.selected_raw_url,
        "source": asset.source,
        "source_asset_id": asset.source_asset_id,
        "source_asset_key": asset.source_asset_key,
        "source_urls": list(asset.source_urls),
    }


def source_asset_record_json(asset: SourceAsset) -> str:
    """Return one eligible asset's canonical source-record JSON."""

    return canonical_source_record_json(source_asset_record(asset))


def inventory_decision_manifest_record(
    decision: InventoryDecision,
) -> dict[str, object]:
    """Return the canonical row framed into an inventory manifest."""

    if isinstance(decision, SourceAssetExclusion):
        return {
            "decision": "excluded",
            "reason_code": decision.reason_code,
            "source_asset_id": decision.source_asset_id,
            "source_record_sha256": decision.source_record_sha256,
        }
    record_json = source_asset_record_json(decision)
    return {
        "decision": "eligible",
        "source_asset_id": decision.source_asset_id,
        "source_record_sha256": source_record_sha256(record_json),
    }


def inventory_decision_manifest_json(decision: InventoryDecision) -> str:
    """Return canonical JSON for one ordered inventory-manifest row."""

    return canonical_source_record_json(inventory_decision_manifest_record(decision))


def _open_readonly(path: Path) -> sqlite3.Connection:
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    conn = sqlite3.connect(resolved.as_uri() + "?mode=ro&immutable=1", uri=True)
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
WITH occurrence_summary AS (
  SELECT asset_key,
         COUNT(*) AS occurrence_count,
         COUNT(DISTINCT article_id) AS parent_count,
         MAX(CASE WHEN role='cover' THEN 1 ELSE 0 END) AS has_cover,
         MAX(CASE WHEN role='gallery' THEN 1 ELSE 0 END) AS has_gallery
  FROM source_image_occurrences
  WHERE parse_status='parsed' AND asset_key IS NOT NULL
  GROUP BY asset_key
), source_urls AS (
  SELECT asset_key,url AS source_url,0 AS source_kind,
         url_id AS first_order,'' AS second_order,0 AS third_order
  FROM image_urls
  UNION ALL
  SELECT asset_key,raw_url AS source_url,1 AS source_kind,
         article_id AS first_order,role AS second_order,position AS third_order
  FROM source_image_occurrences
  WHERE parse_status='parsed' AND asset_key IS NOT NULL
)
SELECT ia.asset_key,ia.original_filename,ia.url_generation,su.source_url,
       COALESCE(os.occurrence_count,0) AS occurrence_count,
       COALESCE(os.parent_count,0) AS parent_count,
       COALESCE(os.has_cover,0) AS has_cover,
       COALESCE(os.has_gallery,0) AS has_gallery
FROM image_assets AS ia
LEFT JOIN source_urls AS su ON su.asset_key=ia.asset_key
LEFT JOIN occurrence_summary AS os ON os.asset_key=ia.asset_key
ORDER BY ia.asset_key,su.source_kind,su.first_order,su.second_order,su.third_order,
         su.source_url
"""


def _divisare_source_record(
    *,
    asset_key: str,
    original_filename: str | None,
    url_generation: str | None,
    source_urls: tuple[str, ...],
    occurrence_count: int,
    parent_count: int,
    roles: tuple[str, ...],
) -> dict[str, object]:
    return {
        "asset": {
            "asset_key": asset_key,
            "original_filename": original_filename,
            "url_generation": url_generation,
        },
        "occurrence_count": occurrence_count,
        "parent_count": parent_count,
        "roles": list(roles),
        "source": "divisare",
        "source_asset_id": asset_key,
        "source_asset_key": asset_key,
        "source_urls": list(source_urls),
    }


def _exclusion(
    *,
    source: str,
    source_asset_id: str,
    source_asset_key: str,
    reason_code: str,
    source_record: Mapping[str, object],
    evidence: Mapping[str, object],
) -> SourceAssetExclusion:
    return SourceAssetExclusion(
        source=source,
        source_asset_id=source_asset_id,
        source_asset_key=source_asset_key,
        reason_code=reason_code,
        source_record_json=canonical_source_record_json(source_record),
        detail_json=canonical_source_record_json(
            {
                "evidence": dict(evidence),
                "reason_code": reason_code,
            }
        ),
    )


def iter_divisare_source_inventory(db_path: Path) -> Iterator[InventoryDecision]:
    """Yield every Divisare ``image_assets`` row as one sorted decision."""

    conn = _open_readonly(Path(db_path))
    try:
        rows = conn.execute(_DIVISARE_QUERY)
        for asset_key, grouped in itertools.groupby(rows, key=lambda row: row["asset_key"]):
            batch = list(grouped)
            first = batch[0]
            asset_id = str(asset_key)
            source_urls = _unique(
                str(row["source_url"])
                for row in batch
                if row["source_url"] is not None
            )
            roles = _roles(first)
            occurrence_count = int(first["occurrence_count"])
            parent_count = int(first["parent_count"])
            record = _divisare_source_record(
                asset_key=asset_id,
                original_filename=first["original_filename"],
                url_generation=first["url_generation"],
                source_urls=source_urls,
                occurrence_count=occurrence_count,
                parent_count=parent_count,
                roles=roles,
            )
            extensions = _divisare_resource_extensions(
                first["original_filename"], source_urls
            )
            if extensions & _HARD_SKIP_EXTENSIONS:
                yield _exclusion(
                    source="divisare",
                    source_asset_id=asset_id,
                    source_asset_key=asset_id,
                    reason_code="hard_skip_extension",
                    source_record=record,
                    evidence={"extensions": sorted(extensions)},
                )
                continue
            if not source_urls:
                yield _exclusion(
                    source="divisare",
                    source_asset_id=asset_id,
                    source_asset_key=asset_id,
                    reason_code="missing_source_url",
                    source_record=record,
                    evidence={"source_url_count": 0},
                )
                continue
            candidates = [url for url in source_urls if _divisare_supported_source_url(url)]
            if not candidates:
                yield _exclusion(
                    source="divisare",
                    source_asset_id=asset_id,
                    source_asset_key=asset_id,
                    reason_code="unsupported_source_url",
                    source_record=record,
                    evidence={"source_urls": list(source_urls)},
                )
                continue
            selected = min(candidates, key=_divisare_url_rank)
            convertible = bool(extensions & _CONVERTIBLE_EXTENSIONS)
            try:
                effective = divisare_effective_fetch_url(
                    selected, convertible=convertible
                )
            except ValueError as exc:
                yield _exclusion(
                    source="divisare",
                    source_asset_id=asset_id,
                    source_asset_key=asset_id,
                    reason_code="invalid_transform_layout",
                    source_record=record,
                    evidence={
                        "error": str(exc),
                        "selected_raw_url": selected,
                    },
                )
                continue
            yield SourceAsset(
                source="divisare",
                source_asset_id=asset_id,
                source_asset_key=asset_id,
                normalized_url=selected,
                selected_raw_url=selected,
                effective_fetch_url=effective,
                source_urls=source_urls,
                occurrence_count=occurrence_count,
                parent_count=parent_count,
                roles=roles,
                format_lane="convertible" if convertible else "raster",
                fetch_profile_version=DIVISARE_FETCH_PROFILE_VERSION,
            )
    finally:
        conn.close()


def iter_divisare_source_assets(
    db_path: Path, *, limit: int | None = None
) -> Iterator[SourceAsset]:
    """Yield eligible Divisare assets from curated v2.4 in stable key order."""

    _validate_limit(limit)
    yielded = 0
    for decision in iter_divisare_source_inventory(db_path):
        if isinstance(decision, SourceAssetExclusion):
            continue
        yield decision
        yielded += 1
        if limit is not None and yielded >= limit:
            break


_ARCHITIZER_QUERY = """
WITH occurrence_summary AS (
  SELECT asset_id,
         COUNT(*) AS occurrence_count,
         COUNT(DISTINCT source_project_id) AS parent_count,
         MAX(CASE WHEN role='cover' THEN 1 ELSE 0 END) AS has_cover,
         MAX(CASE WHEN role='gallery' THEN 1 ELSE 0 END) AS has_gallery
  FROM source_image_occurrences
  WHERE parse_status!='malformed' AND asset_id IS NOT NULL
  GROUP BY asset_id
), source_urls AS (
  SELECT asset_id,raw_url AS source_url,0 AS source_kind,
         image_url_id AS first_order,'' AS second_order,0 AS third_order
  FROM image_urls
  UNION ALL
  SELECT asset_id,raw_url AS source_url,1 AS source_kind,
         source_project_id AS first_order,role AS second_order,ordinal AS third_order
  FROM source_image_occurrences
  WHERE parse_status!='malformed' AND asset_id IS NOT NULL
)
SELECT a.asset_id,a.asset_key,a.normalized_url,a.host,a.path,
       a.is_placeholder_candidate,su.source_url,
       COALESCE(os.occurrence_count,0) AS occurrence_count,
       COALESCE(os.parent_count,0) AS parent_count,
       COALESCE(os.has_cover,0) AS has_cover,
       COALESCE(os.has_gallery,0) AS has_gallery
FROM image_assets AS a
LEFT JOIN source_urls AS su ON su.asset_id=a.asset_id
LEFT JOIN occurrence_summary AS os ON os.asset_id=a.asset_id
ORDER BY a.asset_id,su.source_kind,su.first_order,su.second_order,su.third_order,
         su.source_url
"""


def _architizer_source_record(
    *,
    asset_id: str,
    asset_key: str,
    normalized_url: str,
    host: str,
    path: str,
    is_placeholder_candidate: int,
    source_urls: tuple[str, ...],
    occurrence_count: int,
    parent_count: int,
    roles: tuple[str, ...],
) -> dict[str, object]:
    return {
        "asset": {
            "asset_id": asset_id,
            "asset_key": asset_key,
            "host": host,
            "is_placeholder_candidate": is_placeholder_candidate,
            "normalized_url": normalized_url,
            "path": path,
        },
        "occurrence_count": occurrence_count,
        "parent_count": parent_count,
        "roles": list(roles),
        "source": "architizer",
        "source_asset_id": asset_id,
        "source_asset_key": asset_key,
        "source_urls": list(source_urls),
    }


def iter_architizer_source_inventory(db_path: Path) -> Iterator[InventoryDecision]:
    """Yield every Architizer ``image_assets`` row as one sorted decision."""

    conn = _open_readonly(Path(db_path))
    try:
        rows = conn.execute(_ARCHITIZER_QUERY)
        for asset_id, grouped in itertools.groupby(rows, key=lambda row: row["asset_id"]):
            batch = list(grouped)
            first = batch[0]
            source_id = str(asset_id)
            source_key = str(first["asset_key"])
            normalized_url = str(first["normalized_url"])
            host = str(first["host"])
            path = str(first["path"])
            placeholder = int(first["is_placeholder_candidate"] or 0)
            source_urls = _unique(
                str(row["source_url"])
                for row in batch
                if row["source_url"] is not None
            )
            roles = _roles(first)
            occurrence_count = int(first["occurrence_count"])
            parent_count = int(first["parent_count"])
            record = _architizer_source_record(
                asset_id=source_id,
                asset_key=source_key,
                normalized_url=normalized_url,
                host=host,
                path=path,
                is_placeholder_candidate=placeholder,
                source_urls=source_urls,
                occurrence_count=occurrence_count,
                parent_count=parent_count,
                roles=roles,
            )
            extension = _suffix(path)
            if placeholder:
                yield _exclusion(
                    source="architizer",
                    source_asset_id=source_id,
                    source_asset_key=source_key,
                    reason_code="placeholder_candidate",
                    source_record=record,
                    evidence={"is_placeholder_candidate": placeholder},
                )
                continue
            if extension in _HARD_SKIP_EXTENSIONS:
                yield _exclusion(
                    source="architizer",
                    source_asset_id=source_id,
                    source_asset_key=source_key,
                    reason_code="hard_skip_extension",
                    source_record=record,
                    evidence={"extension": extension},
                )
                continue
            if not source_urls:
                yield _exclusion(
                    source="architizer",
                    source_asset_id=source_id,
                    source_asset_key=source_key,
                    reason_code="missing_source_url",
                    source_record=record,
                    evidence={"source_url_count": 0},
                )
                continue
            try:
                effective = architizer_effective_fetch_url(normalized_url)
            except ValueError as exc:
                yield _exclusion(
                    source="architizer",
                    source_asset_id=source_id,
                    source_asset_key=source_key,
                    reason_code="unsupported_imgix_url",
                    source_record=record,
                    evidence={
                        "error": str(exc),
                        "host": host,
                        "normalized_url": normalized_url,
                    },
                )
                continue
            selected = min(source_urls, key=lambda url: (len(url), url))
            yield SourceAsset(
                source="architizer",
                source_asset_id=source_id,
                source_asset_key=source_key,
                normalized_url=normalized_url,
                selected_raw_url=selected,
                effective_fetch_url=effective,
                source_urls=source_urls,
                occurrence_count=occurrence_count,
                parent_count=parent_count,
                roles=roles,
                format_lane=(
                    "convertible" if extension in _CONVERTIBLE_EXTENSIONS else "raster"
                ),
                fetch_profile_version=ARCHITIZER_FETCH_PROFILE_VERSION,
            )
    finally:
        conn.close()


def iter_architizer_source_assets(
    db_path: Path, *, limit: int | None = None
) -> Iterator[SourceAsset]:
    """Yield eligible, non-placeholder Architizer assets once per asset ID."""

    _validate_limit(limit)
    yielded = 0
    for decision in iter_architizer_source_inventory(db_path):
        if isinstance(decision, SourceAssetExclusion):
            continue
        yield decision
        yielded += 1
        if limit is not None and yielded >= limit:
            break
