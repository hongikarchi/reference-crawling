"""Deterministic Divisare image fetch and pHash smoke pipeline.

The v2.3 metadata database is immutable evidence.  This module reads it in
SQLite read-only mode and writes an asset-keyed sidecar database.  Source URL
variants are retained explicitly so a failed request can never shift a hash to
the following image, which was possible in the legacy positional JSON cache.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sqlite3
import struct
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
from urllib.parse import urlsplit, urlunsplit


SCHEMA_VERSION = 3
RUNNER_VERSION = "divisare-image-smoke-v1.3.0"
COMPATIBLE_RUNNER_VERSIONS = frozenset({RUNNER_VERSION})
SELECTION_VERSION = "divisare-image-smoke-stratified-v1.1.0"
DERIVATIVE_VERSION = "divisare-cloudinary-max512-profile-v1.1.0"
PIXEL_HASH_VERSION = "pillow-exif-transpose-rgb-pixels-v1.0.0"
PHASH_VERSION = "imagehash-phash-hash_size_16-max512-exif-rgb-v1.1.0"
DEFAULT_PROFILE = "c_limit,f_jpg,h_512,q_80,w_512"
PDF_PROFILE = "pg_1,c_limit,f_jpg,h_512,q_80,w_512"
KNOWN_SPLIT_PUBLIC_ID = "7f2fedf69ca074197bf77b221731ff5cca8a0812"
KNOWN_SPLIT_PRIMARY_ASSET = f"divisare|{KNOWN_SPLIT_PUBLIC_ID}|v1678438203"
KNOWN_SPLIT_SECONDARY_ASSET = f"divisare|{KNOWN_SPLIT_PUBLIC_ID}|v1678438207"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
RETRYABLE_HTTP = frozenset({408, 425, 429, 500, 502, 503, 504})

_RASTER_EXTENSIONS = frozenset(
    {"jpg", "jpeg", "png", "webp", "gif", "tif", "tiff", "bmp", "avif", "heic"}
)
_CONVERTIBLE_EXTENSIONS = frozenset(
    {"pdf", "ai", "psd", "eps", "svg", "dwg", "dxf"}
)
_VECTOR_EXTENSIONS = frozenset({"ai", "psd", "eps", "svg", "dwg", "dxf"})
_HARD_SKIP_PATH_PARTS = ("/videos/", "/files/", "/raw/upload/")
_VERSION_RE = re.compile(r"/(v\d+)/")
_THREAD_LOCAL = threading.local()


SIDECAR_SCHEMA = """
PRAGMA foreign_keys=ON;

CREATE TABLE smoke_run (
    run_id                    INTEGER PRIMARY KEY CHECK(run_id=1),
    status                    TEXT NOT NULL CHECK(status IN
                               ('running','complete','failed_validation','failed')),
    runner_version            TEXT NOT NULL,
    schema_version            INTEGER NOT NULL,
    selection_version         TEXT NOT NULL,
    derivative_version        TEXT NOT NULL,
    pixel_hash_version        TEXT NOT NULL,
    phash_version             TEXT NOT NULL,
    source_db_path            TEXT NOT NULL,
    source_sha256_before      TEXT NOT NULL,
    source_sha256_after       TEXT,
    requested_limit           INTEGER NOT NULL,
    workers                   INTEGER NOT NULL,
    sample_manifest_sha256    TEXT NOT NULL,
    started_at                TEXT NOT NULL,
    completed_at              TEXT,
    metrics_json              TEXT CHECK(metrics_json IS NULL OR json_valid(metrics_json)),
    logical_sha256            TEXT,
    error                     TEXT
);

CREATE TABLE sample_assets (
    sample_rank               INTEGER PRIMARY KEY,
    asset_key                 TEXT NOT NULL UNIQUE,
    cohort                    TEXT NOT NULL CHECK(cohort IN
                               ('modern_raster','legacy_raster','convertible','hard_skip','edge')),
    selection_reason          TEXT NOT NULL,
    url_generation            TEXT NOT NULL,
    original_filename         TEXT,
    format_lane               TEXT NOT NULL CHECK(format_lane IN
                               ('raster','convertible','hard_skip')),
    roles_json                TEXT NOT NULL CHECK(json_valid(roles_json)),
    hints_json                TEXT NOT NULL CHECK(json_valid(hints_json)),
    source_url_count          INTEGER NOT NULL CHECK(source_url_count >= 1),
    primary_source_url        TEXT NOT NULL
);

CREATE TABLE sample_asset_urls (
    asset_key                 TEXT NOT NULL REFERENCES sample_assets(asset_key),
    url_id                    INTEGER NOT NULL,
    source_url                TEXT NOT NULL,
    source_transform          TEXT,
    source_generation         TEXT NOT NULL,
    source_delivery_version   TEXT,
    request_url               TEXT,
    request_profile           TEXT,
    PRIMARY KEY(asset_key, url_id),
    UNIQUE(asset_key, source_url)
);

CREATE TABLE fetch_attempts (
    asset_key                 TEXT NOT NULL REFERENCES sample_assets(asset_key),
    request_url               TEXT NOT NULL,
    attempt_no                INTEGER NOT NULL,
    started_at                TEXT NOT NULL,
    elapsed_ms                INTEGER NOT NULL,
    http_status               INTEGER,
    response_mime             TEXT,
    response_bytes            INTEGER,
    final_url                 TEXT,
    outcome                   TEXT NOT NULL CHECK(outcome IN ('success','failed')),
    error_kind                TEXT,
    error_message             TEXT,
    PRIMARY KEY(asset_key, request_url, attempt_no)
);

CREATE TABLE image_variant_results (
    asset_key                 TEXT NOT NULL REFERENCES sample_assets(asset_key),
    request_url               TEXT NOT NULL,
    status                    TEXT NOT NULL CHECK(status IN ('success','failed')),
    attempt_count             INTEGER NOT NULL CHECK(attempt_count >= 1),
    selected_source_url       TEXT NOT NULL,
    final_url                 TEXT,
    http_status               INTEGER,
    response_mime             TEXT,
    decoded_format            TEXT,
    response_bytes            INTEGER,
    derivative_response_sha256 TEXT,
    normalized_pixel_sha256   TEXT,
    width                     INTEGER,
    height                    INTEGER,
    phash_hex                 TEXT,
    error_kind                TEXT,
    error_message             TEXT,
    completed_at              TEXT NOT NULL,
    PRIMARY KEY(asset_key, request_url),
    CHECK(
      (status='success'
       AND derivative_response_sha256 IS NOT NULL
       AND normalized_pixel_sha256 IS NOT NULL
       AND phash_hex IS NOT NULL
       AND length(derivative_response_sha256)=64
       AND length(normalized_pixel_sha256)=64 AND length(phash_hex)=64
       AND decoded_format IS NOT NULL AND length(decoded_format)>0
       AND width > 0 AND height > 0 AND response_bytes > 0)
      OR
      (status='failed' AND error_kind IS NOT NULL)
    )
);

CREATE TABLE image_asset_results (
    asset_key                 TEXT PRIMARY KEY REFERENCES sample_assets(asset_key),
    status                    TEXT NOT NULL CHECK(status IN ('pending','success','failed','skipped')),
    request_count             INTEGER NOT NULL DEFAULT 0,
    attempt_count             INTEGER NOT NULL DEFAULT 0,
    success_variant_count     INTEGER NOT NULL DEFAULT 0,
    failed_variant_count      INTEGER NOT NULL DEFAULT 0,
    distinct_pixel_sha_count  INTEGER NOT NULL DEFAULT 0,
    identity_status           TEXT NOT NULL CHECK(identity_status IN
                               ('pending','consistent','conflict','insufficient','skipped')),
    representative_request_url TEXT,
    derivative_response_sha256 TEXT,
    normalized_pixel_sha256   TEXT,
    width                     INTEGER,
    height                    INTEGER,
    phash_hex                 TEXT,
    skip_or_error_kind        TEXT,
    completed_at              TEXT,
    CHECK(status <> 'success' OR
          (derivative_response_sha256 IS NOT NULL
           AND normalized_pixel_sha256 IS NOT NULL
           AND phash_hex IS NOT NULL
           AND length(derivative_response_sha256)=64
           AND length(normalized_pixel_sha256)=64
           AND length(phash_hex)=64 AND width > 0 AND height > 0))
);

CREATE TABLE validations (
    validation_name           TEXT PRIMARY KEY,
    severity                  TEXT NOT NULL CHECK(severity IN ('info','warning','error')),
    passed                    INTEGER NOT NULL CHECK(passed IN (0,1)),
    expected                  TEXT,
    actual                    TEXT NOT NULL,
    detail                    TEXT
);

CREATE INDEX idx_sample_assets_cohort ON sample_assets(cohort, sample_rank);
CREATE INDEX idx_fetch_attempts_outcome ON fetch_attempts(outcome, error_kind);
CREATE INDEX idx_variant_pixel_sha ON image_variant_results(normalized_pixel_sha256);
CREATE INDEX idx_asset_results_status ON image_asset_results(status, identity_status);
"""


@dataclass(frozen=True)
class SampleAsset:
    sample_rank: int
    asset_key: str
    cohort: str
    selection_reason: str
    url_generation: str
    original_filename: Optional[str]
    format_lane: str
    roles: tuple[str, ...]
    hints: tuple[str, ...]
    source_url_count: int
    primary_source_url: str


@dataclass(frozen=True)
class FetchPayload:
    raw: bytes
    http_status: int
    mime_type: Optional[str]
    final_url: str


class FetchFailure(RuntimeError):
    def __init__(
        self,
        kind: str,
        message: str,
        *,
        http_status: Optional[int] = None,
        retryable: bool = False,
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.http_status = http_status
        self.retryable = retryable
        self.retry_after = retry_after


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def open_source_readonly(path: Path) -> sqlite3.Connection:
    uri = "file:%s?mode=ro" % path.resolve().as_posix()
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _suffix(value: Optional[str]) -> str:
    if not value:
        return ""
    clean = value.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    leaf = clean.rsplit("/", 1)[-1]
    if "." not in leaf:
        return ""
    return leaf.rsplit(".", 1)[-1].casefold()


def _is_hard_skip_url(url: str) -> bool:
    path = urlsplit(url).path.casefold()
    supported_endpoint = "/images/" in path or "/image/upload/" in path
    return any(part in path for part in _HARD_SKIP_PATH_PARTS) or not supported_endpoint


def _url_resource_extensions(url: str) -> set[str]:
    """Return exact suffixes on the delivered leaf and legacy resource leaf."""
    parts = [part for part in urlsplit(url).path.split("/") if part]
    extensions: set[str] = set()
    if parts:
        leaf_extension = _suffix(parts[-1])
        if leaf_extension:
            extensions.add(leaf_extension)
    try:
        project_index = parts.index("project_images")
    except ValueError:
        project_index = -1
    if project_index >= 0 and project_index + 2 < len(parts):
        resource_extension = _suffix(parts[project_index + 2])
        if resource_extension:
            extensions.add(resource_extension)
    return extensions


def _asset_extensions(original_filename: Optional[str], primary_url: str) -> set[str]:
    extensions = _url_resource_extensions(primary_url)
    original_extension = _suffix(original_filename)
    if original_extension:
        extensions.add(original_extension)
    return extensions


def _basic_lane(original_filename: Optional[str], primary_url: str) -> str:
    if _is_hard_skip_url(primary_url):
        return "hard_skip"
    if _asset_extensions(original_filename, primary_url) & _CONVERTIBLE_EXTENSIONS:
        return "convertible"
    return "raster"


def _stable_order(asset_key: str) -> bytes:
    return hashlib.sha256((SELECTION_VERSION + "\0" + asset_key).encode("utf-8")).digest()


def _base_cohort(row: Mapping[str, Any]) -> str:
    lane = row["format_lane"]
    if lane == "hard_skip":
        return "hard_skip"
    if lane == "convertible":
        return "convertible"
    if row["url_generation"] == "cloudinary_public_id":
        return "modern_raster"
    return "legacy_raster"


def select_stratified_sample(source_db: Path, limit: int) -> list[SampleAsset]:
    """Return a stable, edge-heavy sample; the N10 list prefixes N100."""
    if limit < 1:
        raise ValueError("limit must be positive")

    with open_source_readonly(source_db) as conn:
        inventory: list[dict[str, Any]] = []
        for row in conn.execute(
            """
            SELECT ia.asset_key, ia.url_generation, ia.original_filename,
                   COUNT(iu.url_id) AS url_count,
                   COALESCE(
                     MIN(CASE WHEN
                       (INSTR(LOWER(iu.url),'/images/')>0
                        OR INSTR(LOWER(iu.url),'/image/upload/')>0)
                       AND INSTR(LOWER(iu.url),'/videos/')=0
                       AND INSTR(LOWER(iu.url),'/files/')=0
                       AND INSTR(LOWER(iu.url),'/raw/upload/')=0
                     THEN iu.url END),
                     MIN(iu.url)
                   ) AS primary_url,
                   SUM(CASE WHEN
                     (INSTR(LOWER(iu.url),'/images/')>0
                      OR INSTR(LOWER(iu.url),'/image/upload/')>0)
                     AND INSTR(LOWER(iu.url),'/videos/')=0
                     AND INSTR(LOWER(iu.url),'/files/')=0
                     AND INSTR(LOWER(iu.url),'/raw/upload/')=0
                   THEN 1 ELSE 0 END) AS supported_url_count,
                   MAX(LENGTH(iu.url)) AS max_url_length
            FROM image_assets ia
            JOIN image_urls iu ON iu.asset_key=ia.asset_key
            GROUP BY ia.asset_key, ia.url_generation, ia.original_filename
            """
        ):
            item = dict(row)
            item["format_lane"] = _basic_lane(
                item.get("original_filename"), str(item["primary_url"])
            )
            item["extensions"] = _asset_extensions(
                item.get("original_filename"), str(item["primary_url"])
            )
            item["roles"] = set()
            item["hints"] = set()
            inventory.append(item)

        by_key = {str(row["asset_key"]): row for row in inventory}
        for row in conn.execute(
            """
            SELECT asset_key,
                   MAX(CASE WHEN role='cover' THEN 1 ELSE 0 END) AS is_cover,
                   MAX(CASE WHEN role='gallery' THEN 1 ELSE 0 END) AS is_gallery
            FROM article_image_occurrences GROUP BY asset_key
            """
        ):
            target = by_key.get(str(row["asset_key"]))
            if target is None:
                continue
            if row["is_cover"]:
                target["roles"].add("cover")
            if row["is_gallery"]:
                target["roles"].add("gallery")
        for row in conn.execute("SELECT asset_key,hint FROM image_url_hints"):
            target = by_key.get(str(row["asset_key"]))
            if target is not None:
                target["hints"].add(str(row["hint"]))

    ordered = sorted(inventory, key=lambda row: _stable_order(str(row["asset_key"])))
    selected: list[dict[str, Any]] = []
    selected_keys: set[str] = set()

    def reserved_edge(row: Mapping[str, Any]) -> bool:
        return (
            str(row["asset_key"])
            in {KNOWN_SPLIT_PRIMARY_ASSET, KNOWN_SPLIT_SECONDARY_ASSET}
            or int(row["url_count"]) > 2
            or 0 < int(row["supported_url_count"]) < int(row["url_count"])
            or len(row["hints"]) > 1
            or "%" in str(row["primary_url"])
        )

    def choose(reason: str, cohort: str, predicate: Callable[[Mapping[str, Any]], bool]) -> None:
        for row in ordered:
            key = str(row["asset_key"])
            if key not in selected_keys and predicate(row):
                copy = dict(row)
                copy["selection_reason"] = reason
                copy["cohort"] = cohort
                selected.append(copy)
                selected_keys.add(key)
                return

    # Ten fixed edge strata make N10 a meaningful prefix rather than a random sample.
    choose(
        "modern_cover_gallery",
        "modern_raster",
        lambda r: _base_cohort(r) == "modern_raster"
        and {"cover", "gallery"}.issubset(r["roles"])
        and not reserved_edge(r),
    )
    choose(
        "modern_gallery_only",
        "modern_raster",
        lambda r: _base_cohort(r) == "modern_raster"
        and r["roles"] == {"gallery"}
        and not reserved_edge(r),
    )
    choose(
        "legacy_cover_gallery",
        "legacy_raster",
        lambda r: _base_cohort(r) == "legacy_raster"
        and {"cover", "gallery"}.issubset(r["roles"])
        and not reserved_edge(r),
    )
    choose(
        "legacy_gallery_only",
        "legacy_raster",
        lambda r: _base_cohort(r) == "legacy_raster"
        and r["roles"] == {"gallery"}
        and not reserved_edge(r),
    )
    choose(
        "legacy_png_or_gif",
        "legacy_raster",
        lambda r: _base_cohort(r) == "legacy_raster"
        and bool(r["extensions"] & {"png", "gif"})
        and not reserved_edge(r),
    )
    choose(
        "pdf_first_page",
        "convertible",
        lambda r: r["format_lane"] == "convertible"
        and "pdf" in r["extensions"]
        and not reserved_edge(r),
    )
    choose(
        "vector_or_layered_source",
        "convertible",
        lambda r: r["format_lane"] == "convertible"
        and bool(r["extensions"] & _VECTOR_EXTENSIONS)
        and not reserved_edge(r),
    )
    choose("hard_skip_resource", "hard_skip", lambda r: r["format_lane"] == "hard_skip")
    choose(
        "percent_encoded_long_url",
        "edge",
        lambda r: "%" in str(r["primary_url"]) and int(r["max_url_length"]) >= 140,
    )
    choose(
        "known_split_same_version_duplicate",
        "edge",
        lambda r: str(r["asset_key"]) == KNOWN_SPLIT_PRIMARY_ASSET,
    )

    if limit > 10:
        # The next delivery version is a distinct image asset. Keep it adjacent
        # to the N10 prefix so N100 tests both sides of the identity rule.
        choose(
            "known_split_distinct_version",
            "edge",
            lambda r: str(r["asset_key"]) == KNOWN_SPLIT_SECONDARY_ASSET,
        )
        targets = {
            "modern_raster": round(limit * 0.50),
            "legacy_raster": round(limit * 0.25),
            "convertible": round(limit * 0.15),
            "hard_skip": round(limit * 0.05),
        }
        targets["edge"] = limit - sum(targets.values())

        predicates: dict[str, Callable[[Mapping[str, Any]], bool]] = {
            "modern_raster": lambda r: _base_cohort(r) == "modern_raster" and not reserved_edge(r),
            "legacy_raster": lambda r: _base_cohort(r) == "legacy_raster" and not reserved_edge(r),
            "convertible": lambda r: _base_cohort(r) == "convertible" and not reserved_edge(r),
            "hard_skip": lambda r: _base_cohort(r) == "hard_skip" and not reserved_edge(r),
            "edge": reserved_edge,
        }
        counts = Counter(str(row["cohort"]) for row in selected)
        for cohort in ("modern_raster", "legacy_raster", "convertible", "hard_skip", "edge"):
            needed = max(0, targets[cohort] - counts[cohort])
            for row in ordered:
                if needed <= 0:
                    break
                key = str(row["asset_key"])
                if key in selected_keys or not predicates[cohort](row):
                    continue
                copy = dict(row)
                copy["selection_reason"] = "quota_" + cohort
                copy["cohort"] = cohort
                selected.append(copy)
                selected_keys.add(key)
                needed -= 1

    for row in ordered:
        if len(selected) >= limit:
            break
        key = str(row["asset_key"])
        if key in selected_keys:
            continue
        copy = dict(row)
        copy["cohort"] = _base_cohort(row)
        copy["selection_reason"] = "deterministic_fill"
        selected.append(copy)
        selected_keys.add(key)

    selected = selected[:limit]
    if len(selected) != limit:
        raise RuntimeError("source contains fewer assets than requested sample")
    return [
        SampleAsset(
            sample_rank=index,
            asset_key=str(row["asset_key"]),
            cohort=str(row["cohort"]),
            selection_reason=str(row["selection_reason"]),
            url_generation=str(row["url_generation"]),
            original_filename=row.get("original_filename"),
            format_lane=str(row["format_lane"]),
            roles=tuple(sorted(str(value) for value in row["roles"])),
            hints=tuple(sorted(str(value) for value in row["hints"])),
            source_url_count=int(row["url_count"]),
            primary_source_url=str(row["primary_url"]),
        )
        for index, row in enumerate(selected, 1)
    ]


def sample_manifest_sha256(sample: Sequence[SampleAsset]) -> str:
    rows = [
        {
            "sample_rank": item.sample_rank,
            "asset_key": item.asset_key,
            "cohort": item.cohort,
            "selection_reason": item.selection_reason,
            "url_generation": item.url_generation,
            "original_filename": item.original_filename,
            "format_lane": item.format_lane,
            "roles": item.roles,
            "hints": item.hints,
            "source_url_count": item.source_url_count,
            "primary_source_url": item.primary_source_url,
        }
        for item in sample
    ]
    return hashlib.sha256(canonical_json(rows).encode("utf-8")).hexdigest()


def derivative_profile(original_filename: Optional[str], source_url: str) -> str:
    extensions = _asset_extensions(original_filename, source_url)
    return PDF_PROFILE if "pdf" in extensions else DEFAULT_PROFILE


def fixed_derivative_url(source_url: str, profile: str) -> str:
    """Replace an existing Cloudinary transform without decoding path segments."""
    parsed = urlsplit(source_url)
    if parsed.scheme.casefold() != "https" or parsed.hostname != "images.divisare.com":
        raise ValueError("source URL is outside the Divisare HTTPS image host")
    parts = [part for part in parsed.path.split("/") if part]
    if "images" in parts:
        anchor = parts.index("images")
    elif "image" in parts and parts.index("image") + 1 < len(parts) \
            and parts[parts.index("image") + 1] == "upload":
        anchor = parts.index("image") + 1
    else:
        raise ValueError("unsupported Divisare resource path")
    if anchor + 1 >= len(parts):
        raise ValueError("image URL is missing a delivery version")
    if re.fullmatch(r"v\d+", parts[anchor + 1]):
        parts.insert(anchor + 1, profile)
    elif anchor + 2 < len(parts) and re.fullmatch(r"v\d+", parts[anchor + 2]):
        parts[anchor + 1] = profile
    else:
        raise ValueError("image URL has an unrecognized Cloudinary transform layout")
    return urlunsplit(("https", "images.divisare.com", "/" + "/".join(parts), parsed.query, ""))


def _delivery_version(url: str) -> Optional[str]:
    match = _VERSION_RE.search(urlsplit(url).path)
    return match.group(1) if match else None


def _source_urls(source_db: Path, asset_keys: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with open_source_readonly(source_db) as conn:
        for start in range(0, len(asset_keys), 400):
            batch = asset_keys[start : start + 400]
            placeholders = ",".join("?" for _ in batch)
            sql = (
                "SELECT url_id,asset_key,url,transform_signature,url_generation "
                "FROM image_urls WHERE asset_key IN (%s) ORDER BY asset_key,url_id" % placeholders
            )
            for row in conn.execute(sql, batch):
                grouped[str(row["asset_key"])].append(dict(row))
    return grouped


def _session():
    import requests

    current = getattr(_THREAD_LOCAL, "session", None)
    if current is None:
        current = requests.Session()
        current.headers.update(
            {
                "User-Agent": "Archibe-Divisare-Image-Audit/1.0",
                "Accept": "image/jpeg",
                "Accept-Encoding": "identity",
            }
        )
        _THREAD_LOCAL.session = current
    return current


def network_fetch(
    url: str,
    *,
    timeout: tuple[float, float] = (10.0, 30.0),
    max_bytes: int = MAX_IMAGE_BYTES,
) -> FetchPayload:
    import requests

    parsed = urlsplit(url)
    if parsed.scheme.casefold() != "https" or parsed.hostname != "images.divisare.com":
        raise FetchFailure("host_rejected", "request host is not images.divisare.com")
    try:
        response = _session().get(url, stream=True, timeout=timeout, allow_redirects=True)
    except requests.Timeout as exc:
        raise FetchFailure("timeout", str(exc), retryable=True) from exc
    except requests.ConnectionError as exc:
        raise FetchFailure("connection", str(exc), retryable=True) from exc
    except requests.RequestException as exc:
        raise FetchFailure("request", str(exc), retryable=False) from exc

    final = urlsplit(response.url)
    if final.scheme.casefold() != "https" or final.hostname != "images.divisare.com":
        response.close()
        raise FetchFailure("redirect_host_rejected", "redirect left images.divisare.com")
    status = int(response.status_code)
    if status < 200 or status >= 300:
        retry_after = response.headers.get("Retry-After")
        response.close()
        try:
            retry_seconds = float(retry_after) if retry_after else None
        except ValueError:
            retry_seconds = None
        raise FetchFailure(
            "http_%d" % status,
            "HTTP %d" % status,
            http_status=status,
            retryable=status in RETRYABLE_HTTP or status == 403,
            retry_after=retry_seconds,
        )

    chunks: list[bytes] = []
    total = 0
    try:
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise FetchFailure("too_large", "response exceeded %d bytes" % max_bytes)
            chunks.append(chunk)
    except requests.RequestException as exc:
        raise FetchFailure("stream", str(exc), retryable=True) from exc
    finally:
        response.close()
    return FetchPayload(
        raw=b"".join(chunks),
        http_status=status,
        mime_type=response.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold() or None,
        final_url=response.url,
    )


def _decode_hash(raw: bytes) -> dict[str, Any]:
    import imagehash
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(raw)) as opened:
            source_format = str(opened.format or "").upper()
            image = ImageOps.exif_transpose(opened)
            if getattr(image, "is_animated", False):
                image.seek(0)
            image.load()
            rgb = image.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise FetchFailure("decode", str(exc)) from exc
    if rgb.width <= 0 or rgb.height <= 0:
        raise FetchFailure("invalid_dimensions", "decoded image has non-positive dimensions")
    if rgb.width > 512 or rgb.height > 512:
        raise FetchFailure(
            "transform_not_applied",
            "decoded dimensions exceed fixed derivative: %dx%d" % (rgb.width, rgb.height),
        )
    pixels = rgb.tobytes()
    pixel_digest = hashlib.sha256(
        b"RGB\0" + struct.pack(">II", rgb.width, rgb.height) + pixels
    ).hexdigest()
    phash = str(imagehash.phash(rgb, hash_size=16)).casefold()
    if len(phash) != 64 or any(char not in "0123456789abcdef" for char in phash):
        raise FetchFailure("invalid_phash", "pHash is not a lowercase 256-bit hex value")
    return {
        "normalized_pixel_sha256": pixel_digest,
        "decoded_format": source_format,
        "width": rgb.width,
        "height": rgb.height,
        "phash_hex": phash,
    }


def _cache_response(cache_dir: Optional[Path], raw: bytes, digest: str) -> None:
    if cache_dir is None:
        return
    directory = cache_dir / digest[:2]
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (digest + ".jpg")
    if path.exists():
        return
    temp = directory / (
        digest + ".tmp-%d-%d" % (os.getpid(), threading.get_ident())
    )
    with temp.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.link(temp, path)
    except FileExistsError:
        pass
    finally:
        temp.unlink(missing_ok=True)


def _request_with_retries(
    asset_key: str,
    source_url: str,
    request_url: str,
    *,
    fetcher: Callable[..., FetchPayload],
    cache_dir: Optional[Path],
    max_attempts: int,
    sleep: Callable[[float], None],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    attempts: list[dict[str, Any]] = []
    last_failure: Optional[FetchFailure] = None
    for attempt_no in range(1, max_attempts + 1):
        started_at = utc_now()
        started = time.monotonic()
        try:
            payload = fetcher(request_url, timeout=(10.0, 30.0), max_bytes=MAX_IMAGE_BYTES)
            response_sha = hashlib.sha256(payload.raw).hexdigest()
            decoded = _decode_hash(payload.raw)
            _cache_response(cache_dir, payload.raw, response_sha)
            elapsed_ms = max(0, round((time.monotonic() - started) * 1000))
            attempts.append(
                {
                    "asset_key": asset_key,
                    "request_url": request_url,
                    "attempt_no": attempt_no,
                    "started_at": started_at,
                    "elapsed_ms": elapsed_ms,
                    "http_status": payload.http_status,
                    "response_mime": payload.mime_type,
                    "response_bytes": len(payload.raw),
                    "final_url": payload.final_url,
                    "outcome": "success",
                    "error_kind": None,
                    "error_message": None,
                }
            )
            return (
                {
                    "asset_key": asset_key,
                    "request_url": request_url,
                    "status": "success",
                    "attempt_count": attempt_no,
                    "selected_source_url": source_url,
                    "final_url": payload.final_url,
                    "http_status": payload.http_status,
                    "response_mime": payload.mime_type,
                    "response_bytes": len(payload.raw),
                    "derivative_response_sha256": response_sha,
                    **decoded,
                    "error_kind": None,
                    "error_message": None,
                    "completed_at": utc_now(),
                },
                attempts,
            )
        except FetchFailure as exc:
            last_failure = exc
            elapsed_ms = max(0, round((time.monotonic() - started) * 1000))
            attempts.append(
                {
                    "asset_key": asset_key,
                    "request_url": request_url,
                    "attempt_no": attempt_no,
                    "started_at": started_at,
                    "elapsed_ms": elapsed_ms,
                    "http_status": exc.http_status,
                    "response_mime": None,
                    "decoded_format": None,
                    "response_bytes": None,
                    "final_url": None,
                    "outcome": "failed",
                    "error_kind": exc.kind,
                    "error_message": str(exc)[:1000],
                }
            )
            if not exc.retryable or attempt_no >= max_attempts:
                break
            delay = exc.retry_after
            if delay is None:
                jitter = int(hashlib.sha256(asset_key.encode("utf-8")).hexdigest()[:2], 16) / 1024
                delay = min(4.0, 0.5 * (2 ** (attempt_no - 1))) + jitter
            sleep(min(10.0, max(0.0, delay)))

    assert last_failure is not None
    return (
        {
            "asset_key": asset_key,
            "request_url": request_url,
            "status": "failed",
            "attempt_count": len(attempts),
            "selected_source_url": source_url,
            "final_url": None,
            "http_status": last_failure.http_status,
            "response_mime": None,
            "decoded_format": None,
            "response_bytes": None,
            "derivative_response_sha256": None,
            "normalized_pixel_sha256": None,
            "width": None,
            "height": None,
            "phash_hex": None,
            "error_kind": last_failure.kind,
            "error_message": str(last_failure)[:1000],
            "completed_at": utc_now(),
        },
        attempts,
    )


def _process_asset(
    sample: SampleAsset,
    source_urls: Sequence[Mapping[str, Any]],
    *,
    fetcher: Callable[..., FetchPayload],
    cache_dir: Optional[Path],
    max_attempts: int,
    sleep: Callable[[float], None],
) -> dict[str, Any]:
    if sample.format_lane == "hard_skip":
        return {
            "sample": sample,
            "attempts": [],
            "variants": [],
            "asset": {
                "asset_key": sample.asset_key,
                "status": "skipped",
                "request_count": 0,
                "attempt_count": 0,
                "success_variant_count": 0,
                "failed_variant_count": 0,
                "distinct_pixel_sha_count": 0,
                "identity_status": "skipped",
                "representative_request_url": None,
                "derivative_response_sha256": None,
                "normalized_pixel_sha256": None,
                "width": None,
                "height": None,
                "phash_hex": None,
                "skip_or_error_kind": "unsupported_resource_path",
                "completed_at": utc_now(),
            },
        }

    request_sources: dict[str, str] = {}
    invalid_urls: list[dict[str, Any]] = []
    for row in source_urls:
        source_url = str(row["url"])
        profile = derivative_profile(sample.original_filename, source_url)
        try:
            request_url = fixed_derivative_url(source_url, profile)
        except ValueError as exc:
            synthetic = "invalid://" + hashlib.sha256(source_url.encode("utf-8")).hexdigest()
            invalid_urls.append(
                {
                    "asset_key": sample.asset_key,
                    "request_url": synthetic,
                    "status": "failed",
                    "attempt_count": 1,
                    "selected_source_url": source_url,
                    "final_url": None,
                    "http_status": None,
                    "response_mime": None,
                    "decoded_format": None,
                    "response_bytes": None,
                    "derivative_response_sha256": None,
                    "normalized_pixel_sha256": None,
                    "width": None,
                    "height": None,
                    "phash_hex": None,
                    "error_kind": "invalid_source_url",
                    "error_message": str(exc),
                    "completed_at": utc_now(),
                }
            )
            continue
        request_sources.setdefault(request_url, source_url)

    variants = list(invalid_urls)
    attempts: list[dict[str, Any]] = []
    for request_url, source_url in sorted(request_sources.items()):
        variant, request_attempts = _request_with_retries(
            sample.asset_key,
            source_url,
            request_url,
            fetcher=fetcher,
            cache_dir=cache_dir,
            max_attempts=max_attempts,
            sleep=sleep,
        )
        variants.append(variant)
        attempts.extend(request_attempts)

    successes = [row for row in variants if row["status"] == "success"]
    failures = [row for row in variants if row["status"] == "failed"]
    pixel_hashes = {str(row["normalized_pixel_sha256"]) for row in successes}
    representative = min(successes, key=lambda row: str(row["request_url"])) if successes else None
    if successes:
        status = "success"
        identity_status = "conflict" if len(pixel_hashes) > 1 else "consistent"
        error_kind = "asset_identity_conflict" if identity_status == "conflict" else None
    else:
        status = "failed"
        identity_status = "insufficient"
        error_kind = failures[0]["error_kind"] if failures else "no_requestable_url"
    asset = {
        "asset_key": sample.asset_key,
        "status": status,
        "request_count": len(variants),
        "attempt_count": len(attempts),
        "success_variant_count": len(successes),
        "failed_variant_count": len(failures),
        "distinct_pixel_sha_count": len(pixel_hashes),
        "identity_status": identity_status,
        "representative_request_url": representative["request_url"] if representative else None,
        "derivative_response_sha256": (
            representative["derivative_response_sha256"] if representative else None
        ),
        "normalized_pixel_sha256": (
            representative["normalized_pixel_sha256"] if representative else None
        ),
        "width": representative["width"] if representative else None,
        "height": representative["height"] if representative else None,
        "phash_hex": representative["phash_hex"] if representative else None,
        "skip_or_error_kind": error_kind,
        "completed_at": utc_now(),
    }
    return {"sample": sample, "attempts": attempts, "variants": variants, "asset": asset}


def _create_sidecar(
    path: Path,
    *,
    source_db: Path,
    source_sha: str,
    sample: Sequence[SampleAsset],
    source_urls: Mapping[str, Sequence[Mapping[str, Any]]],
    workers: int,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SIDECAR_SCHEMA)
        manifest_sha = sample_manifest_sha256(sample)
        conn.execute(
            """
            INSERT INTO smoke_run(
              run_id,status,runner_version,schema_version,selection_version,
              derivative_version,pixel_hash_version,phash_version,source_db_path,
              source_sha256_before,requested_limit,workers,sample_manifest_sha256,started_at
            ) VALUES(1,'running',?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                RUNNER_VERSION,
                SCHEMA_VERSION,
                SELECTION_VERSION,
                DERIVATIVE_VERSION,
                PIXEL_HASH_VERSION,
                PHASH_VERSION,
                str(source_db.resolve()),
                source_sha,
                len(sample),
                workers,
                manifest_sha,
                utc_now(),
            ),
        )
        for item in sample:
            conn.execute(
                """
                INSERT INTO sample_assets VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    item.sample_rank,
                    item.asset_key,
                    item.cohort,
                    item.selection_reason,
                    item.url_generation,
                    item.original_filename,
                    item.format_lane,
                    canonical_json(item.roles),
                    canonical_json(item.hints),
                    item.source_url_count,
                    item.primary_source_url,
                ),
            )
            conn.execute(
                "INSERT INTO image_asset_results(asset_key,status,identity_status) VALUES(?,'pending','pending')",
                (item.asset_key,),
            )
            for row in source_urls[item.asset_key]:
                source_url = str(row["url"])
                profile = (
                    None
                    if item.format_lane == "hard_skip"
                    else derivative_profile(item.original_filename, source_url)
                )
                try:
                    request_url = fixed_derivative_url(source_url, profile) if profile else None
                except ValueError:
                    request_url = None
                conn.execute(
                    """
                    INSERT INTO sample_asset_urls(
                      asset_key,url_id,source_url,source_transform,source_generation,
                      source_delivery_version,request_url,request_profile
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (
                        item.asset_key,
                        int(row["url_id"]),
                        source_url,
                        row.get("transform_signature"),
                        str(row["url_generation"]),
                        _delivery_version(source_url),
                        request_url,
                        profile,
                    ),
                )
        conn.commit()
    finally:
        conn.close()


def _write_processed(conn: sqlite3.Connection, processed: Mapping[str, Any]) -> None:
    asset = processed["asset"]
    with conn:
        for row in processed["attempts"]:
            conn.execute(
                """
                INSERT INTO fetch_attempts VALUES(
                  :asset_key,:request_url,:attempt_no,:started_at,:elapsed_ms,
                  :http_status,:response_mime,:response_bytes,:final_url,:outcome,
                  :error_kind,:error_message
                )
                """,
                row,
            )
        for row in processed["variants"]:
            conn.execute(
                """
                INSERT INTO image_variant_results(
                  asset_key,request_url,status,attempt_count,selected_source_url,
                  final_url,http_status,response_mime,decoded_format,response_bytes,
                  derivative_response_sha256,normalized_pixel_sha256,width,height,
                  phash_hex,error_kind,error_message,completed_at
                ) VALUES(
                  :asset_key,:request_url,:status,:attempt_count,:selected_source_url,
                  :final_url,:http_status,:response_mime,:decoded_format,:response_bytes,
                  :derivative_response_sha256,:normalized_pixel_sha256,:width,:height,
                  :phash_hex,:error_kind,:error_message,:completed_at
                )
                """,
                row,
            )
        conn.execute(
            """
            UPDATE image_asset_results SET
              status=:status,request_count=:request_count,attempt_count=:attempt_count,
              success_variant_count=:success_variant_count,
              failed_variant_count=:failed_variant_count,
              distinct_pixel_sha_count=:distinct_pixel_sha_count,
              identity_status=:identity_status,
              representative_request_url=:representative_request_url,
              derivative_response_sha256=:derivative_response_sha256,
              normalized_pixel_sha256=:normalized_pixel_sha256,
              width=:width,height=:height,phash_hex=:phash_hex,
              skip_or_error_kind=:skip_or_error_kind,completed_at=:completed_at
            WHERE asset_key=:asset_key
            """,
            asset,
        )


def _metrics(conn: sqlite3.Connection) -> dict[str, Any]:
    by_status = dict(conn.execute("SELECT status,COUNT(*) FROM image_asset_results GROUP BY status"))
    by_cohort = {
        row[0]: {"total": row[1], "success": row[2], "failed": row[3], "skipped": row[4]}
        for row in conn.execute(
            """
            SELECT s.cohort,COUNT(*),
                   SUM(r.status='success'),SUM(r.status='failed'),SUM(r.status='skipped')
            FROM sample_assets s JOIN image_asset_results r USING(asset_key)
            GROUP BY s.cohort ORDER BY s.cohort
            """
        )
    }
    raster_total, raster_success = conn.execute(
        """
        SELECT COUNT(*),SUM(r.status='success')
        FROM sample_assets s JOIN image_asset_results r USING(asset_key)
        WHERE s.format_lane='raster'
        """
    ).fetchone()
    return {
        "sample_assets": conn.execute("SELECT COUNT(*) FROM sample_assets").fetchone()[0],
        "source_urls": conn.execute("SELECT COUNT(*) FROM sample_asset_urls").fetchone()[0],
        "unique_request_urls": conn.execute(
            "SELECT COUNT(DISTINCT request_url) FROM sample_asset_urls WHERE request_url IS NOT NULL"
        ).fetchone()[0],
        "fetch_attempts": conn.execute("SELECT COUNT(*) FROM fetch_attempts").fetchone()[0],
        "successful_attempts": conn.execute(
            "SELECT COUNT(*) FROM fetch_attempts WHERE outcome='success'"
        ).fetchone()[0],
        "by_status": by_status,
        "by_cohort": by_cohort,
        "raster_success_rate": (float(raster_success or 0) / raster_total if raster_total else 0.0),
        "identity_conflicts": conn.execute(
            "SELECT COUNT(*) FROM image_asset_results WHERE identity_status='conflict'"
        ).fetchone()[0],
        "distinct_response_sha": conn.execute(
            "SELECT COUNT(DISTINCT derivative_response_sha256) FROM image_variant_results WHERE status='success'"
        ).fetchone()[0],
        "decoded_formats": dict(
            conn.execute(
                "SELECT decoded_format,COUNT(*) FROM image_variant_results "
                "WHERE status='success' GROUP BY decoded_format ORDER BY decoded_format"
            )
        ),
        "response_bytes": conn.execute(
            "SELECT COALESCE(SUM(response_bytes),0) FROM fetch_attempts WHERE outcome='success'"
        ).fetchone()[0],
        "elapsed_fetch_ms": conn.execute(
            "SELECT COALESCE(SUM(elapsed_ms),0) FROM fetch_attempts"
        ).fetchone()[0],
    }


def logical_sha256(conn: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    provenance = conn.execute(
        """
        SELECT runner_version,schema_version,selection_version,derivative_version,
               pixel_hash_version,phash_version,source_sha256_before,
               requested_limit,sample_manifest_sha256
        FROM smoke_run WHERE run_id=1
        """
    ).fetchone()
    if provenance is None:
        raise RuntimeError("cannot hash sidecar without smoke_run provenance")
    digest.update(canonical_json(list(provenance)).encode("utf-8"))
    digest.update(b"\n")
    queries = (
        "SELECT sample_rank,asset_key,cohort,selection_reason,url_generation,"
        "COALESCE(original_filename,''),format_lane,roles_json,hints_json,source_url_count,primary_source_url "
        "FROM sample_assets ORDER BY sample_rank",
        "SELECT asset_key,url_id,source_url,COALESCE(source_transform,''),source_generation,"
        "COALESCE(source_delivery_version,''),COALESCE(request_url,''),COALESCE(request_profile,'') "
        "FROM sample_asset_urls ORDER BY asset_key,url_id",
        "SELECT asset_key,status,request_count,attempt_count,success_variant_count,failed_variant_count,"
        "distinct_pixel_sha_count,identity_status,COALESCE(representative_request_url,''),"
        "COALESCE(derivative_response_sha256,''),COALESCE(normalized_pixel_sha256,''),"
        "COALESCE(width,0),COALESCE(height,0),COALESCE(phash_hex,''),COALESCE(skip_or_error_kind,'') "
        "FROM image_asset_results ORDER BY asset_key",
        "SELECT asset_key,request_url,status,attempt_count,selected_source_url,COALESCE(http_status,0),"
        "COALESCE(response_mime,''),COALESCE(decoded_format,''),COALESCE(response_bytes,0),"
        "COALESCE(derivative_response_sha256,''),"
        "COALESCE(normalized_pixel_sha256,''),COALESCE(width,0),COALESCE(height,0),"
        "COALESCE(phash_hex,''),COALESCE(error_kind,'') FROM image_variant_results "
        "ORDER BY asset_key,request_url",
    )
    for query in queries:
        for row in conn.execute(query):
            digest.update(canonical_json(list(row)).encode("utf-8"))
            digest.update(b"\n")
    return digest.hexdigest()


def validate_sidecar(
    conn: sqlite3.Connection,
    source_db: Path,
    *,
    expected_source_sha: str,
) -> list[dict[str, Any]]:
    validations: list[dict[str, Any]] = []

    def add(name: str, severity: str, passed: bool, expected: Any, actual: Any, detail: str = ""):
        validations.append(
            {
                "validation_name": name,
                "severity": severity,
                "passed": 1 if passed else 0,
                "expected": str(expected),
                "actual": str(actual),
                "detail": detail or None,
            }
        )

    quick = conn.execute("PRAGMA quick_check").fetchone()[0]
    add("sqlite_quick_check", "error", quick == "ok", "ok", quick)
    foreign_key_violations = len(conn.execute("PRAGMA foreign_key_check").fetchall())
    add("foreign_key_check", "error", foreign_key_violations == 0, 0, foreign_key_violations)
    requested = conn.execute("SELECT requested_limit FROM smoke_run WHERE run_id=1").fetchone()[0]
    sample_count = conn.execute("SELECT COUNT(*) FROM sample_assets").fetchone()[0]
    result_count = conn.execute("SELECT COUNT(*) FROM image_asset_results").fetchone()[0]
    terminal = conn.execute(
        "SELECT COUNT(*) FROM image_asset_results WHERE status IN ('success','failed','skipped')"
    ).fetchone()[0]
    add("sample_accounting", "error", sample_count == requested, requested, sample_count)
    add("result_accounting", "error", result_count == requested, requested, result_count)
    add("terminal_accounting", "error", terminal == requested, requested, terminal)
    invalid_success = conn.execute(
        """
        SELECT COUNT(*) FROM image_asset_results
        WHERE status='success' AND (
          derivative_response_sha256 IS NULL OR normalized_pixel_sha256 IS NULL
          OR phash_hex IS NULL OR length(derivative_response_sha256)<>64
          OR length(normalized_pixel_sha256)<>64 OR length(phash_hex)<>64
          OR width NOT BETWEEN 1 AND 512 OR height NOT BETWEEN 1 AND 512)
        """
    ).fetchone()[0]
    add("success_shape", "error", invalid_success == 0, 0, invalid_success)
    skipped_attempts = conn.execute(
        """
        SELECT COUNT(*) FROM fetch_attempts f JOIN sample_assets s USING(asset_key)
        WHERE s.format_lane='hard_skip'
        """
    ).fetchone()[0]
    add("hard_skip_no_network", "error", skipped_attempts == 0, 0, skipped_attempts)

    source_after = file_sha256(source_db)
    add("source_sha_unchanged", "error", source_after == expected_source_sha, expected_source_sha, source_after)
    sample_refs = {
        row[0]
        for row in conn.execute("SELECT asset_key FROM sample_assets")
    }
    url_refs = {
        (row[0], int(row[1]))
        for row in conn.execute("SELECT asset_key,url_id FROM sample_asset_urls")
    }
    with open_source_readonly(source_db) as source:
        found_assets: set[str] = set()
        found_urls: set[tuple[str, int]] = set()
        keys = sorted(sample_refs)
        for start in range(0, len(keys), 400):
            batch = keys[start : start + 400]
            marks = ",".join("?" for _ in batch)
            found_assets.update(
                str(row[0]) for row in source.execute(
                    "SELECT asset_key FROM image_assets WHERE asset_key IN (%s)" % marks, batch
                )
            )
            found_urls.update(
                (str(row[0]), int(row[1]))
                for row in source.execute(
                    "SELECT asset_key,url_id FROM image_urls WHERE asset_key IN (%s)" % marks, batch
                )
            )
    add("source_asset_refs", "error", found_assets == sample_refs, len(sample_refs), len(found_assets))
    add("source_url_refs", "error", found_urls == url_refs, len(url_refs), len(found_urls))

    invalid_version_groups = conn.execute(
        """
        SELECT COUNT(*) FROM (
          SELECT s.asset_key
          FROM sample_assets s
          JOIN sample_asset_urls u USING(asset_key)
          WHERE s.url_generation='cloudinary_public_id'
          GROUP BY s.asset_key
          HAVING COUNT(DISTINCT u.source_delivery_version)<>1
             OR SUM(CASE WHEN u.source_delivery_version IS NULL THEN 1 ELSE 0 END)>0
        )
        """
    ).fetchone()[0]
    add(
        "source_asset_single_delivery_version",
        "error",
        invalid_version_groups == 0,
        0,
        invalid_version_groups,
        "Each Cloudinary asset must contain URLs from exactly one delivery version.",
    )
    version_key_mismatches = conn.execute(
        """
        SELECT COUNT(DISTINCT s.asset_key)
        FROM sample_assets s
        JOIN sample_asset_urls u USING(asset_key)
        WHERE s.url_generation='cloudinary_public_id'
          AND (
            u.source_delivery_version IS NULL
            OR substr(
                 s.asset_key,
                 -length('|' || u.source_delivery_version)
               ) <> '|' || u.source_delivery_version
          )
        """
    ).fetchone()[0]
    add(
        "source_asset_key_delivery_version",
        "error",
        version_key_mismatches == 0,
        0,
        version_key_mismatches,
        "Version-aware Cloudinary asset keys must end with their delivery version.",
    )

    metrics = _metrics(conn)
    rate = float(metrics["raster_success_rate"])
    add("raster_success_rate", "error", rate >= 0.95, ">=0.95", "%.4f" % rate)
    conflicts = int(metrics["identity_conflicts"])
    add(
        "asset_identity_consistency",
        "error",
        conflicts == 0,
        0,
        conflicts,
        "A conflict means one asset_key produced more than one normalized pixel hash.",
    )
    return validations


def _insert_validations(conn: sqlite3.Connection, rows: Sequence[Mapping[str, Any]]) -> None:
    conn.execute("DELETE FROM validations")
    conn.executemany(
        "INSERT INTO validations VALUES(:validation_name,:severity,:passed,:expected,:actual,:detail)",
        rows,
    )


def render_report(conn: sqlite3.Connection, *, artifact_path: Path) -> str:
    run = dict(conn.execute("SELECT * FROM smoke_run WHERE run_id=1").fetchone())
    metrics = json.loads(run["metrics_json"] or "{}")
    validations = [dict(row) for row in conn.execute("SELECT * FROM validations ORDER BY severity,validation_name")]
    failures = [
        dict(row)
        for row in conn.execute(
            """
            SELECT s.sample_rank,s.asset_key,s.cohort,s.selection_reason,r.status,
                   r.identity_status,r.skip_or_error_kind
            FROM sample_assets s JOIN image_asset_results r USING(asset_key)
            WHERE r.status<>'success' OR r.identity_status='conflict'
            ORDER BY s.sample_rank
            """
        )
    ]
    lines = [
        "# Divisare image smoke N%d" % run["requested_limit"],
        "",
        "## Run",
        "",
        "- Status: `%s`" % run["status"],
        "- Runner: `%s`" % run["runner_version"],
        "- Source: `%s`" % run["source_db_path"],
        "- Source SHA before: `%s`" % run["source_sha256_before"],
        "- Source SHA after: `%s`" % run["source_sha256_after"],
        "- Sample manifest SHA: `%s`" % run["sample_manifest_sha256"],
        "- Logical SHA: `%s`" % run["logical_sha256"],
        "- Artifact: `%s`" % artifact_path.as_posix(),
        "- Model/API tokens: `0`",
        "",
        "## Metrics",
        "",
        "- Assets: `%s`" % metrics.get("sample_assets"),
        "- Source URLs: `%s`" % metrics.get("source_urls"),
        "- Unique derivative requests: `%s`" % metrics.get("unique_request_urls"),
        "- Fetch attempts: `%s`" % metrics.get("fetch_attempts"),
        "- Asset status: `%s`" % canonical_json(metrics.get("by_status", {})),
        "- Raster success rate: `%.2f%%`" % (100 * float(metrics.get("raster_success_rate", 0))),
        "- Identity conflicts: `%s`" % metrics.get("identity_conflicts"),
        "- Decoded formats: `%s`" % canonical_json(metrics.get("decoded_formats", {})),
        "- Downloaded bytes: `%s`" % metrics.get("response_bytes"),
        "",
        "### Cohorts",
        "",
        "| Cohort | Total | Success | Failed | Skipped |",
        "|---|---:|---:|---:|---:|",
    ]
    for cohort, values in sorted(metrics.get("by_cohort", {}).items()):
        lines.append(
            "| %s | %d | %d | %d | %d |"
            % (
                cohort,
                values["total"],
                values["success"],
                values["failed"],
                values["skipped"],
            )
        )
    lines.extend(
        [
            "",
            "## Validations",
            "",
            "| Validation | Severity | Result | Expected | Actual |",
            "|---|---|---|---|---|",
        ]
    )
    for row in validations:
        lines.append(
            "| %s | %s | %s | %s | %s |"
            % (
                row["validation_name"],
                row["severity"],
                "PASS" if row["passed"] else "FAIL",
                row["expected"],
                row["actual"],
            )
        )
    lines.extend(["", "## Exceptions", ""])
    if not failures:
        lines.append("None.")
    else:
        lines.extend(
            [
                "| Rank | Asset | Cohort | Reason | Status | Identity | Error/skip |",
                "|---:|---|---|---|---|---|---|",
            ]
        )
        for row in failures:
            lines.append(
                "| %d | `%s` | %s | %s | %s | %s | %s |"
                % (
                    row["sample_rank"],
                    row["asset_key"],
                    row["cohort"],
                    row["selection_reason"],
                    row["status"],
                    row["identity_status"],
                    row["skip_or_error_kind"] or "",
                )
            )
    lines.append("")
    return "\n".join(lines)


def _publish_no_clobber(
    partial_db: Path,
    output_db: Path,
    partial_report: Path,
    report_path: Path,
) -> None:
    published: list[Path] = []
    try:
        os.link(partial_report, report_path)
        published.append(report_path)
        os.link(partial_db, output_db)
        published.append(output_db)
    except Exception:
        for path in reversed(published):
            path.unlink(missing_ok=True)
        raise
    partial_report.unlink()
    partial_db.unlink()


def _validate_completed_output(
    *,
    output_db: Path,
    report_path: Path,
    source_db: Path,
    limit: int,
) -> dict[str, Any]:
    if not report_path.exists():
        raise RuntimeError("completed sidecar is missing its report: %s" % report_path)
    with sqlite3.connect(output_db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM smoke_run WHERE run_id=1").fetchone()
        if row is None:
            raise RuntimeError("sidecar is missing smoke_run provenance")
        checks = {
            "status": (row["status"], "complete"),
            "runner_version": (row["runner_version"], COMPATIBLE_RUNNER_VERSIONS),
            "schema_version": (row["schema_version"], SCHEMA_VERSION),
            "selection_version": (row["selection_version"], SELECTION_VERSION),
            "derivative_version": (row["derivative_version"], DERIVATIVE_VERSION),
            "pixel_hash_version": (row["pixel_hash_version"], PIXEL_HASH_VERSION),
            "phash_version": (row["phash_version"], PHASH_VERSION),
            "source_db_path": (Path(row["source_db_path"]).resolve(), source_db.resolve()),
            "requested_limit": (row["requested_limit"], limit),
        }
        for name, (actual, expected) in checks.items():
            passed = actual in expected if isinstance(expected, frozenset) else actual == expected
            if not passed:
                raise RuntimeError(
                    "completed sidecar %s mismatch: actual=%r expected=%r"
                    % (name, actual, expected)
                )
        current_source_sha = file_sha256(source_db)
        if current_source_sha not in {
            row["source_sha256_before"],
            row["source_sha256_after"],
        }:
            raise RuntimeError("source SHA no longer matches completed sidecar lineage")
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("completed sidecar failed SQLite quick_check")
        failed_errors = conn.execute(
            "SELECT COUNT(*) FROM validations WHERE severity='error' AND passed=0"
        ).fetchone()[0]
        if failed_errors:
            raise RuntimeError("completed sidecar has failed error-level validations")
        metrics = _metrics(conn)
        return {
            **metrics,
            "status": str(row["status"]),
            "logical_sha256": row["logical_sha256"],
            "source_sha256": current_source_sha,
            "requests_made": 0,
            "resumed_complete": True,
        }


def run_smoke(
    *,
    source_db: Path,
    output_db: Path,
    report_path: Path,
    cache_dir: Optional[Path],
    limit: int,
    workers: int,
    resume: bool = False,
    fetcher: Callable[..., FetchPayload] = network_fetch,
    sleep: Callable[[float], None] = time.sleep,
    max_attempts: int = 2,
) -> dict[str, Any]:
    source_db = source_db.resolve()
    output_db = output_db.resolve()
    report_path = report_path.resolve()
    if not 1 <= workers <= 5:
        raise ValueError("workers must be between 1 and 5 for images.divisare.com")
    if not 1 <= max_attempts <= 4:
        raise ValueError("max_attempts must be between 1 and 4")
    if output_db.exists():
        if not resume:
            raise FileExistsError("immutable output already exists: %s" % output_db)
        return _validate_completed_output(
            output_db=output_db,
            report_path=report_path,
            source_db=source_db,
            limit=limit,
        )
    if report_path.exists():
        raise FileExistsError("immutable report already exists: %s" % report_path)
    partial_db = output_db.with_name(output_db.name + ".partial")
    partial_report = report_path.with_name(report_path.name + ".partial")
    critical_paths = {source_db, output_db, report_path, partial_db, partial_report}
    if len(critical_paths) != 5:
        raise ValueError("source, output, report, and partial paths must be distinct")
    if partial_db.exists() and not resume:
        raise FileExistsError("partial output exists; pass resume or inspect it: %s" % partial_db)

    source_sha = file_sha256(source_db)
    sample = select_stratified_sample(source_db, limit)
    urls = _source_urls(source_db, [item.asset_key for item in sample])
    if any(not urls.get(item.asset_key) for item in sample):
        raise RuntimeError("sample contains an asset without source URL rows")
    if not partial_db.exists():
        _create_sidecar(
            partial_db,
            source_db=source_db,
            source_sha=source_sha,
            sample=sample,
            source_urls=urls,
            workers=workers,
        )
    else:
        with sqlite3.connect(partial_db) as conn:
            stored = conn.execute(
                """
                SELECT source_sha256_before,requested_limit,sample_manifest_sha256,
                       runner_version,schema_version,selection_version,derivative_version,
                       pixel_hash_version,phash_version
                FROM smoke_run WHERE run_id=1
                """
            ).fetchone()
            expected_manifest = sample_manifest_sha256(sample)
            expected = (
                source_sha,
                limit,
                expected_manifest,
                stored[3] if stored[3] in COMPATIBLE_RUNNER_VERSIONS else RUNNER_VERSION,
                SCHEMA_VERSION,
                SELECTION_VERSION,
                DERIVATIVE_VERSION,
                PIXEL_HASH_VERSION,
                PHASH_VERSION,
            )
            if tuple(stored) != expected:
                raise RuntimeError("partial sidecar does not match the requested source/sample")

    conn = sqlite3.connect(partial_db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        pending_keys = {
            str(row[0])
            for row in conn.execute("SELECT asset_key FROM image_asset_results WHERE status='pending'")
        }
        pending = [item for item in sample if item.asset_key in pending_keys]
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    _process_asset,
                    item,
                    urls[item.asset_key],
                    fetcher=fetcher,
                    cache_dir=cache_dir,
                    max_attempts=max_attempts,
                    sleep=sleep,
                ): item.asset_key
                for item in pending
            }
            for future in as_completed(futures):
                _write_processed(conn, future.result())

        source_after = file_sha256(source_db)
        validation_rows = validate_sidecar(conn, source_db, expected_source_sha=source_sha)
        _insert_validations(conn, validation_rows)
        metrics = _metrics(conn)
        logical = logical_sha256(conn)
        error_failures = [
            row for row in validation_rows if row["severity"] == "error" and not row["passed"]
        ]
        status = "failed_validation" if error_failures else "complete"
        conn.execute(
            """
            UPDATE smoke_run SET status=?,source_sha256_after=?,completed_at=?,
              metrics_json=?,logical_sha256=? WHERE run_id=1
            """,
            (status, source_after, utc_now(), canonical_json(metrics), logical),
        )
        conn.commit()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        partial_report.write_text(
            render_report(conn, artifact_path=output_db), encoding="utf-8", newline="\n"
        )
    except Exception as exc:
        try:
            conn.execute(
                "UPDATE smoke_run SET status='failed',completed_at=?,error=? WHERE run_id=1",
                (utc_now(), str(exc)[:2000]),
            )
            conn.commit()
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

    _publish_no_clobber(partial_db, output_db, partial_report, report_path)
    return {
        **metrics,
        "status": status,
        "logical_sha256": logical,
        "source_sha256": source_sha,
        "output_db": str(output_db),
        "report_path": str(report_path),
        "requests_made": metrics["fetch_attempts"],
        "resumed_complete": False,
    }
