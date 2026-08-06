"""Transient image probe for the frozen Divisare Vision candidate pool.

Downloaded bytes remain in worker memory only. A SQLite staging file stores
metadata and hashes for crash-safe resume; the immutable final artifact is an
enriched candidate manifest accepted by the human-review UI.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import platform
import sqlite3
import struct
import tempfile
import time
import warnings
from collections import Counter, defaultdict
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

from canonical.divisare_image_smoke import (
    FetchFailure,
    FetchPayload,
    canonical_json,
    file_sha256,
    fixed_derivative_url,
    network_fetch,
    utc_now,
)
from canonical.divisare_vision_gold import (
    CANDIDATE_MANIFEST_VERSION,
    CLASSES,
    GENERATION_GROUPS,
    IDENTITY_PROFILE,
    PHASH_VERSION,
    PIXEL_HASH_VERSION,
    SOURCE_PROFILE,
    manifest_sha256,
    validate_candidate_manifest,
)


PROBE_VERSION = "divisare-vision-candidate-image-probe-v1.0.0"
STAGING_SCHEMA_VERSION = 1
NORMALIZED_LONG_EDGE = 512
SOURCE_LONG_EDGE = 2048
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_READ_TIMEOUT = 30.0
DEFAULT_MAX_ATTEMPTS = 2
MAX_WORKERS = 5
RETRYABLE_HTTP = frozenset({403, 408, 425, 429, 500, 502, 503, 504})
RUNTIME_VERSION_KEYS = ("python", "pillow", "imagehash", "numpy")
PROBE_CELL_ORDER = tuple(
    (discovery_class, generation_group)
    for discovery_class in CLASSES
    for generation_group in GENERATION_GROUPS
)


STAGING_SCHEMA = """
PRAGMA foreign_keys=ON;

CREATE TABLE probe_run (
    run_id                       INTEGER PRIMARY KEY CHECK(run_id=1),
    status                       TEXT NOT NULL CHECK(status IN
                                  ('running','interrupted','complete','complete_with_failures')),
    probe_version                TEXT NOT NULL,
    staging_schema_version       INTEGER NOT NULL,
    identity_profile             TEXT NOT NULL,
    pixel_hash_version           TEXT NOT NULL,
    phash_version                TEXT NOT NULL,
    runtime_versions_json        TEXT NOT NULL,
    input_manifest_path          TEXT NOT NULL,
    input_manifest_file_sha256   TEXT NOT NULL,
    input_manifest_sha256        TEXT NOT NULL,
    source_db_sha256             TEXT NOT NULL,
    candidate_count              INTEGER NOT NULL,
    workers                      INTEGER NOT NULL,
    max_bytes                    INTEGER NOT NULL,
    connect_timeout              REAL NOT NULL,
    read_timeout                 REAL NOT NULL,
    max_attempts                 INTEGER NOT NULL,
    started_at                   TEXT NOT NULL,
    updated_at                   TEXT NOT NULL,
    completed_at                 TEXT,
    error                        TEXT
);

CREATE TABLE candidate_results (
    candidate_rank               INTEGER PRIMARY KEY,
    candidate_id                 TEXT NOT NULL UNIQUE,
    asset_key                    TEXT NOT NULL UNIQUE,
    request_url                  TEXT NOT NULL UNIQUE,
    status                       TEXT NOT NULL CHECK(status IN ('pending','success','failed')),
    attempt_count                INTEGER NOT NULL DEFAULT 0,
    elapsed_ms                   INTEGER,
    final_url                    TEXT,
    http_status                  INTEGER,
    response_mime                TEXT,
    response_bytes               INTEGER,
    content_sha256               TEXT,
    original_format              TEXT,
    original_mode                TEXT,
    original_width               INTEGER,
    original_height              INTEGER,
    frame_count                  INTEGER,
    exif_orientation             INTEGER,
    orientation_applied          INTEGER,
    oriented_width               INTEGER,
    oriented_height              INTEGER,
    alpha_composited             INTEGER,
    icc_profile_present          INTEGER,
    color_normalization          TEXT,
    normalized_width             INTEGER,
    normalized_height            INTEGER,
    pixel_sha256                 TEXT,
    phash_256                    TEXT,
    error_kind                   TEXT,
    error_message                TEXT,
    completed_at                 TEXT,
    CHECK(
      (status='pending' AND completed_at IS NULL)
      OR
      (status='success'
       AND http_status BETWEEN 200 AND 299
       AND response_bytes > 0
       AND length(content_sha256)=64
       AND original_format IS NOT NULL
       AND original_width > 0 AND original_height > 0
       AND normalized_width > 0 AND normalized_height > 0
       AND normalized_width <= 512 AND normalized_height <= 512
       AND length(pixel_sha256)=64 AND length(phash_256)=64
       AND error_kind IS NULL AND completed_at IS NOT NULL)
      OR
      (status='failed' AND error_kind IS NOT NULL AND completed_at IS NOT NULL)
    )
);

CREATE TABLE fetch_attempts (
    candidate_id                 TEXT NOT NULL REFERENCES candidate_results(candidate_id),
    attempt_no                   INTEGER NOT NULL,
    started_at                   TEXT NOT NULL,
    elapsed_ms                   INTEGER NOT NULL,
    outcome                      TEXT NOT NULL CHECK(outcome IN ('success','failed')),
    final_url                    TEXT,
    http_status                  INTEGER,
    response_mime                TEXT,
    response_bytes               INTEGER,
    content_sha256               TEXT,
    error_kind                   TEXT,
    error_message                TEXT,
    PRIMARY KEY(candidate_id,attempt_no)
);

CREATE INDEX idx_probe_status ON candidate_results(status,candidate_rank);
CREATE INDEX idx_probe_pixel ON candidate_results(pixel_sha256) WHERE status='success';
CREATE INDEX idx_probe_phash ON candidate_results(phash_256) WHERE status='success';
CREATE INDEX idx_probe_attempt_outcome ON fetch_attempts(outcome,error_kind);
"""


@dataclass(frozen=True)
class ProbeConfig:
    workers: int = 4
    max_bytes: int = DEFAULT_MAX_BYTES
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    read_timeout: float = DEFAULT_READ_TIMEOUT
    max_attempts: int = DEFAULT_MAX_ATTEMPTS

    def validate(self) -> None:
        if not 1 <= self.workers <= MAX_WORKERS:
            raise ValueError("workers must be between 1 and %d" % MAX_WORKERS)
        if not 1 <= self.max_attempts <= 4:
            raise ValueError("max_attempts must be between 1 and 4")
        if not 1024 <= self.max_bytes <= 50 * 1024 * 1024:
            raise ValueError("max_bytes must be between 1 KiB and 50 MiB")
        if not 0 < self.connect_timeout <= 60 or not 0 < self.read_timeout <= 120:
            raise ValueError("timeouts must be positive and bounded")


@dataclass(frozen=True)
class NormalizedImageEvidence:
    original_format: str
    original_mode: str
    original_width: int
    original_height: int
    frame_count: int
    exif_orientation: int
    orientation_applied: bool
    oriented_width: int
    oriented_height: int
    alpha_composited: bool
    icc_profile_present: bool
    color_normalization: str
    normalized_width: int
    normalized_height: int
    pixel_sha256: str
    phash_256: str


def probe_runtime_versions() -> dict[str, str]:
    """Return pixel-affecting runtime versions under stable field names."""
    from importlib.metadata import version

    return {
        "python": platform.python_version(),
        "pillow": version("Pillow"),
        "imagehash": version("ImageHash"),
        "numpy": version("numpy"),
    }


def _validate_runtime_versions(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != set(RUNTIME_VERSION_KEYS):
        raise ValueError("runtime_versions must contain python, pillow, imagehash, and numpy")
    normalized = {key: value[key] for key in RUNTIME_VERSION_KEYS}
    if any(
        not isinstance(item, str) or not item or item != item.strip()
        for item in normalized.values()
    ):
        raise ValueError("runtime_versions values must be non-empty strings")
    return normalized


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _pixel_sha256(image: Any) -> str:
    return hashlib.sha256(
        b"RGB\0" + struct.pack(">II", image.width, image.height) + image.tobytes()
    ).hexdigest()


def _rgb_with_white_background(image: Any) -> tuple[Any, bool]:
    from PIL import Image

    has_alpha = "A" in image.getbands() or "transparency" in image.info
    if not has_alpha:
        return image.convert("RGB"), False
    rgba = image.convert("RGBA")
    background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    background.alpha_composite(rgba)
    return background.convert("RGB"), True


def decode_normalize_hash(raw: bytes) -> NormalizedImageEvidence:
    """Decode one response in memory and hash deterministic max-512 RGB pixels."""
    import imagehash
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as opened:
                original_format = str(opened.format or "").upper()
                original_mode = str(opened.mode or "")
                original_width, original_height = opened.size
                frame_count = int(getattr(opened, "n_frames", 1) or 1)
                exif_orientation = int(opened.getexif().get(274, 1) or 1)
                if original_width <= 0 or original_height <= 0:
                    raise FetchFailure("invalid_dimensions", "decoded dimensions are non-positive")
                if max(original_width, original_height) > SOURCE_LONG_EDGE:
                    raise FetchFailure(
                        "transform_not_applied",
                        "decoded dimensions exceed max2048 request: %dx%d"
                        % (original_width, original_height),
                    )
                opened.seek(0)
                icc_profile = opened.info.get("icc_profile")
                image = ImageOps.exif_transpose(opened)
                image.load()
                oriented_width, oriented_height = image.size

                rgb, alpha_composited = _rgb_with_white_background(image)
                color_normalization = (
                    "rgb_alpha_white" if alpha_composited else "rgb"
                )
                if max(rgb.size) > NORMALIZED_LONG_EDGE:
                    rgb.thumbnail(
                        (NORMALIZED_LONG_EDGE, NORMALIZED_LONG_EDGE),
                        Image.Resampling.LANCZOS,
                    )
                rgb.load()
    except FetchFailure:
        raise
    except (Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise FetchFailure("decompression_bomb", str(exc)) from exc
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise FetchFailure("decode", str(exc)) from exc

    pixel_sha = _pixel_sha256(rgb)
    phash = str(imagehash.phash(rgb, hash_size=16)).casefold()
    if len(phash) != 64 or any(char not in "0123456789abcdef" for char in phash):
        raise FetchFailure("invalid_phash", "pHash is not lowercase 256-bit hex")
    return NormalizedImageEvidence(
        original_format=original_format,
        original_mode=original_mode,
        original_width=original_width,
        original_height=original_height,
        frame_count=frame_count,
        exif_orientation=exif_orientation,
        orientation_applied=exif_orientation not in (0, 1),
        oriented_width=oriented_width,
        oriented_height=oriented_height,
        alpha_composited=alpha_composited,
        icc_profile_present=bool(icc_profile),
        color_normalization=color_normalization,
        normalized_width=rgb.width,
        normalized_height=rgb.height,
        pixel_sha256=pixel_sha,
        phash_256=phash,
    )


def _validate_probe_manifest(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    validate_candidate_manifest(payload)
    if payload.get("manifest_version") != CANDIDATE_MANIFEST_VERSION:
        raise ValueError("candidate manifest version mismatch")
    contract = payload.get("contract")
    if not isinstance(contract, Mapping) or contract.get("source_profile") != SOURCE_PROFILE:
        raise ValueError("candidate manifest does not use the max2048 source profile")
    for field, expected in (
        ("identity_profile", IDENTITY_PROFILE),
        ("pixel_hash_version", PIXEL_HASH_VERSION),
        ("phash_version", PHASH_VERSION),
    ):
        if contract.get(field) != expected:
            raise ValueError("candidate manifest identity contract mismatch: %s" % field)
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("candidate manifest candidates must be a list")
    expected_ranks = list(range(1, len(candidates) + 1))
    actual_ranks = [row.get("candidate_rank") for row in candidates]
    if actual_ranks != expected_ranks:
        raise ValueError("candidate ranks must be ordered and contiguous")
    for row in candidates:
        candidate_id = str(row.get("candidate_id") or "")
        request_url = str(row.get("request_url") or "")
        parsed = urlsplit(request_url)
        if parsed.scheme != "https" or parsed.hostname != "images.divisare.com":
            raise ValueError("candidate request_url must use images.divisare.com HTTPS")
        expected_url = fixed_derivative_url(str(row.get("source_url") or ""), SOURCE_PROFILE)
        if request_url != expected_url:
            raise ValueError("candidate request_url contract mismatch: %s" % candidate_id)
    return [dict(row) for row in candidates]


def load_probe_manifest(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("candidate manifest must be a JSON object")
    _validate_probe_manifest(payload)
    return payload, _sha256_bytes(raw)


def _attempt_failure(
    *,
    row: Mapping[str, Any],
    attempt_no: int,
    started_at: str,
    elapsed_ms: int,
    error: FetchFailure,
    payload: FetchPayload | None,
    content_sha256: str | None,
) -> dict[str, Any]:
    return {
        "candidate_id": row["candidate_id"],
        "attempt_no": attempt_no,
        "started_at": started_at,
        "elapsed_ms": elapsed_ms,
        "outcome": "failed",
        "final_url": payload.final_url if payload else None,
        "http_status": payload.http_status if payload else error.http_status,
        "response_mime": payload.mime_type if payload else None,
        "response_bytes": len(payload.raw) if payload else None,
        "content_sha256": content_sha256,
        "error_kind": error.kind,
        "error_message": str(error)[:1000],
    }


def _validate_fetch_payload(
    payload: FetchPayload,
    *,
    max_bytes: int,
) -> None:
    if not isinstance(payload.raw, bytes) or not payload.raw:
        raise FetchFailure("empty_response", "image response is empty")
    if len(payload.raw) > max_bytes:
        raise FetchFailure("too_large", "response exceeded %d bytes" % max_bytes)
    if not 200 <= int(payload.http_status) <= 299:
        status = int(payload.http_status)
        raise FetchFailure(
            "http_%d" % status,
            "HTTP %d" % status,
            http_status=status,
            retryable=status in RETRYABLE_HTTP,
        )
    final = urlsplit(payload.final_url)
    if final.scheme != "https" or final.hostname != "images.divisare.com":
        raise FetchFailure(
            "redirect_host_rejected",
            "final response URL left images.divisare.com",
        )


def _probe_candidate(
    row: Mapping[str, Any],
    *,
    config: ProbeConfig,
    fetcher: Callable[..., FetchPayload],
    sleep: Callable[[float], None],
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    total_started = time.monotonic()
    last_error: FetchFailure | None = None
    last_payload: FetchPayload | None = None
    last_content_sha: str | None = None
    for attempt_no in range(1, config.max_attempts + 1):
        started_at = utc_now()
        started = time.monotonic()
        payload: FetchPayload | None = None
        content_sha: str | None = None
        try:
            payload = fetcher(
                str(row["request_url"]),
                timeout=(config.connect_timeout, config.read_timeout),
                max_bytes=config.max_bytes,
            )
            _validate_fetch_payload(payload, max_bytes=config.max_bytes)
            content_sha = _sha256_bytes(payload.raw)
            evidence = decode_normalize_hash(payload.raw)
            elapsed_ms = max(0, round((time.monotonic() - started) * 1000))
            attempts.append(
                {
                    "candidate_id": row["candidate_id"],
                    "attempt_no": attempt_no,
                    "started_at": started_at,
                    "elapsed_ms": elapsed_ms,
                    "outcome": "success",
                    "final_url": payload.final_url,
                    "http_status": payload.http_status,
                    "response_mime": payload.mime_type,
                    "response_bytes": len(payload.raw),
                    "content_sha256": content_sha,
                    "error_kind": None,
                    "error_message": None,
                }
            )
            return {
                "candidate_rank": row["candidate_rank"],
                "candidate_id": row["candidate_id"],
                "asset_key": row["asset_key"],
                "request_url": row["request_url"],
                "status": "success",
                "attempt_count": attempt_no,
                "elapsed_ms": max(0, round((time.monotonic() - total_started) * 1000)),
                "final_url": payload.final_url,
                "http_status": payload.http_status,
                "response_mime": payload.mime_type,
                "response_bytes": len(payload.raw),
                "content_sha256": content_sha,
                **evidence.__dict__,
                "error_kind": None,
                "error_message": None,
                "completed_at": utc_now(),
                "attempts": attempts,
            }
        except FetchFailure as exc:
            error = exc
        except Exception as exc:  # row-level accounting; raw bytes still die with this frame
            error = FetchFailure("internal_%s" % exc.__class__.__name__, str(exc))
        last_error = error
        last_payload = payload
        last_content_sha = content_sha
        elapsed_ms = max(0, round((time.monotonic() - started) * 1000))
        attempts.append(
            _attempt_failure(
                row=row,
                attempt_no=attempt_no,
                started_at=started_at,
                elapsed_ms=elapsed_ms,
                error=error,
                payload=payload,
                content_sha256=content_sha,
            )
        )
        retryable = error.retryable or error.http_status in RETRYABLE_HTTP
        if not retryable or attempt_no >= config.max_attempts:
            break
        delay = error.retry_after
        if delay is None:
            jitter = int(hashlib.sha256(str(row["candidate_id"]).encode()).hexdigest()[:2], 16) / 1024
            delay = min(4.0, 0.5 * (2 ** (attempt_no - 1))) + jitter
        sleep(min(10.0, max(0.0, delay)))

    assert last_error is not None
    return {
        "candidate_rank": row["candidate_rank"],
        "candidate_id": row["candidate_id"],
        "asset_key": row["asset_key"],
        "request_url": row["request_url"],
        "status": "failed",
        "attempt_count": len(attempts),
        "elapsed_ms": max(0, round((time.monotonic() - total_started) * 1000)),
        "final_url": last_payload.final_url if last_payload else None,
        "http_status": last_payload.http_status if last_payload else last_error.http_status,
        "response_mime": last_payload.mime_type if last_payload else None,
        "response_bytes": len(last_payload.raw) if last_payload else None,
        "content_sha256": last_content_sha,
        "error_kind": last_error.kind,
        "error_message": str(last_error)[:1000],
        "completed_at": utc_now(),
        "attempts": attempts,
    }


def _create_staging(
    staging_path: Path,
    *,
    manifest_path: Path,
    manifest_file_sha: str,
    manifest: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    config: ProbeConfig,
) -> None:
    staging_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=staging_path.name + ".", suffix=".init", dir=staging_path.parent
    )
    os.close(fd)
    temp = Path(temp_name)
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(temp)
        try:
            conn.executescript(STAGING_SCHEMA)
            now = utc_now()
            conn.execute(
                """
                INSERT INTO probe_run(
                  run_id,status,probe_version,staging_schema_version,
                  identity_profile,pixel_hash_version,phash_version,
                  runtime_versions_json,
                  input_manifest_path,input_manifest_file_sha256,
                  input_manifest_sha256,source_db_sha256,candidate_count,
                  workers,max_bytes,connect_timeout,read_timeout,max_attempts,
                  started_at,updated_at,completed_at,error
                ) VALUES(
                  1,'running',:probe_version,:staging_schema_version,
                  :identity_profile,:pixel_hash_version,:phash_version,
                  :runtime_versions_json,
                  :input_manifest_path,:input_manifest_file_sha256,
                  :input_manifest_sha256,:source_db_sha256,:candidate_count,
                  :workers,:max_bytes,:connect_timeout,:read_timeout,:max_attempts,
                  :started_at,:updated_at,NULL,NULL
                )
                """,
                {
                    "probe_version": PROBE_VERSION,
                    "staging_schema_version": STAGING_SCHEMA_VERSION,
                    "identity_profile": IDENTITY_PROFILE,
                    "pixel_hash_version": PIXEL_HASH_VERSION,
                    "phash_version": PHASH_VERSION,
                    "runtime_versions_json": canonical_json(probe_runtime_versions()),
                    "input_manifest_path": str(manifest_path),
                    "input_manifest_file_sha256": manifest_file_sha,
                    "input_manifest_sha256": manifest["manifest_sha256"],
                    "source_db_sha256": manifest["source_db_sha256"],
                    "candidate_count": len(candidates),
                    "workers": config.workers,
                    "max_bytes": config.max_bytes,
                    "connect_timeout": config.connect_timeout,
                    "read_timeout": config.read_timeout,
                    "max_attempts": config.max_attempts,
                    "started_at": now,
                    "updated_at": now,
                },
            )
            conn.executemany(
                """
                INSERT INTO candidate_results(
                  candidate_rank,candidate_id,asset_key,request_url,status
                ) VALUES(?,?,?,?,'pending')
                """,
                [
                    (
                        row["candidate_rank"],
                        row["candidate_id"],
                        row["asset_key"],
                        row["request_url"],
                    )
                    for row in candidates
                ],
            )
            conn.commit()
        finally:
            conn.close()
            conn = None
        try:
            os.link(temp, staging_path)
        except FileExistsError as exc:
            raise FileExistsError("staging artifact already exists: %s" % staging_path) from exc
    finally:
        if conn is not None:
            conn.close()
        temp.unlink(missing_ok=True)


def _verify_staging(
    conn: sqlite3.Connection,
    *,
    manifest_path: Path,
    manifest_file_sha: str,
    manifest: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    config: ProbeConfig,
) -> None:
    row = conn.execute("SELECT * FROM probe_run WHERE run_id=1").fetchone()
    if row is None:
        raise RuntimeError("staging artifact is missing probe_run")
    expected = {
        "probe_version": PROBE_VERSION,
        "staging_schema_version": STAGING_SCHEMA_VERSION,
        "identity_profile": IDENTITY_PROFILE,
        "pixel_hash_version": PIXEL_HASH_VERSION,
        "phash_version": PHASH_VERSION,
        "runtime_versions_json": canonical_json(probe_runtime_versions()),
        "input_manifest_path": str(manifest_path),
        "input_manifest_file_sha256": manifest_file_sha,
        "input_manifest_sha256": manifest["manifest_sha256"],
        "source_db_sha256": manifest["source_db_sha256"],
        "candidate_count": len(candidates),
        "workers": config.workers,
        "max_bytes": config.max_bytes,
        "connect_timeout": config.connect_timeout,
        "read_timeout": config.read_timeout,
        "max_attempts": config.max_attempts,
    }
    for field, value in expected.items():
        if row[field] != value:
            raise RuntimeError(
                "staging resume contract mismatch for %s: %r != %r"
                % (field, row[field], value)
            )
    stored = [
        tuple(value)
        for value in conn.execute(
            "SELECT candidate_rank,candidate_id,asset_key,request_url "
            "FROM candidate_results ORDER BY candidate_rank"
        )
    ]
    wanted = [
        (row["candidate_rank"], row["candidate_id"], row["asset_key"], row["request_url"])
        for row in candidates
    ]
    if stored != wanted:
        raise RuntimeError("staging candidate identities do not match the input manifest")


_RESULT_COLUMNS = (
    "status",
    "attempt_count",
    "elapsed_ms",
    "final_url",
    "http_status",
    "response_mime",
    "response_bytes",
    "content_sha256",
    "original_format",
    "original_mode",
    "original_width",
    "original_height",
    "frame_count",
    "exif_orientation",
    "orientation_applied",
    "oriented_width",
    "oriented_height",
    "alpha_composited",
    "icc_profile_present",
    "color_normalization",
    "normalized_width",
    "normalized_height",
    "pixel_sha256",
    "phash_256",
    "error_kind",
    "error_message",
    "completed_at",
)


def _write_probe_result(conn: sqlite3.Connection, result: Mapping[str, Any]) -> None:
    attempts = result.get("attempts") or []
    conn.executemany(
        """
        INSERT INTO fetch_attempts VALUES(
          :candidate_id,:attempt_no,:started_at,:elapsed_ms,:outcome,:final_url,
          :http_status,:response_mime,:response_bytes,:content_sha256,
          :error_kind,:error_message
        )
        """,
        attempts,
    )
    values = []
    for field in _RESULT_COLUMNS:
        value = result.get(field)
        if field in {"orientation_applied", "alpha_composited", "icc_profile_present"}:
            value = None if value is None else int(bool(value))
        values.append(value)
    assignments = ",".join("%s=?" % field for field in _RESULT_COLUMNS)
    conn.execute(
        "UPDATE candidate_results SET %s WHERE candidate_id=? AND status='pending'"
        % assignments,
        (*values, result["candidate_id"]),
    )
    if conn.execute("SELECT changes()").fetchone()[0] != 1:
        raise RuntimeError("candidate result was not pending: %s" % result["candidate_id"])
    conn.execute(
        "UPDATE probe_run SET status='running',updated_at=?,error=NULL WHERE run_id=1",
        (utc_now(),),
    )
    conn.commit()


def phash_distance(left: str, right: str) -> int:
    if len(left) != 64 or len(right) != 64:
        raise ValueError("pHash values must be 256-bit hexadecimal strings")
    try:
        return (int(left, 16) ^ int(right, 16)).bit_count()
    except ValueError as exc:
        raise ValueError("pHash values must be hexadecimal") from exc


def build_duplicate_evidence(
    successful: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    ordered = sorted(successful, key=lambda row: int(row["candidate_rank"]))
    exact_buckets: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in ordered:
        exact_buckets[str(row["pixel_sha256"])].append(row)
    exact_groups = []
    duplicate_sets = sorted(
        (rows for rows in exact_buckets.values() if len(rows) > 1),
        key=lambda rows: int(rows[0]["candidate_rank"]),
    )
    for index, rows in enumerate(duplicate_sets, 1):
        exact_groups.append(
            {
                "group_id": "exact-pixel-%04d" % index,
                "pixel_sha256": rows[0]["pixel_sha256"],
                "representative_candidate_id": rows[0]["candidate_id"],
                "member_candidate_ids": [row["candidate_id"] for row in rows],
                "member_count": len(rows),
            }
        )

    duplicate_pairs: list[dict[str, Any]] = []
    audit_pairs: list[dict[str, Any]] = []
    for left_index, left in enumerate(ordered):
        for right in ordered[left_index + 1 :]:
            distance = phash_distance(str(left["phash_256"]), str(right["phash_256"]))
            if distance > 16:
                continue
            pair = {
                "candidate_id_a": left["candidate_id"],
                "candidate_id_b": right["candidate_id"],
                "phash_distance": distance,
                "exact_pixel_duplicate": left["pixel_sha256"] == right["pixel_sha256"],
            }
            (duplicate_pairs if distance <= 8 else audit_pairs).append(pair)
    order = lambda row: (  # noqa: E731
        row["phash_distance"], row["candidate_id_a"], row["candidate_id_b"]
    )
    duplicate_pairs.sort(key=order)
    audit_pairs.sort(key=order)
    return exact_groups, duplicate_pairs, audit_pairs


def _metrics(results: Sequence[Mapping[str, Any]], attempts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    statuses = Counter(str(row["status"]) for row in results)
    errors = Counter(str(row["error_kind"]) for row in results if row["status"] == "failed")
    return {
        "candidate_count": len(results),
        "success_count": statuses.get("success", 0),
        "failure_count": statuses.get("failed", 0),
        "pending_count": statuses.get("pending", 0),
        "attempt_count": len(attempts),
        "successful_probe_attempts": sum(row["outcome"] == "success" for row in attempts),
        "http_2xx_attempts": sum(
            row.get("http_status") is not None
            and 200 <= int(row["http_status"]) <= 299
            for row in attempts
        ),
        "failed_attempts": sum(row["outcome"] == "failed" for row in attempts),
        "downloaded_bytes": sum(int(row.get("response_bytes") or 0) for row in attempts),
        "errors_by_kind": dict(sorted(errors.items())),
    }


def _select_smoke_pending(
    pending: Sequence[Mapping[str, Any]], limit: int
) -> list[Mapping[str, Any]]:
    """Round-robin pending rows across the ten class/generation cells."""
    buckets: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in sorted(pending, key=lambda item: int(item["candidate_rank"])):
        key = (str(row["discovery_class"]), str(row["generation_group"]))
        if key not in PROBE_CELL_ORDER:
            raise ValueError("unsupported probe smoke cell: %s/%s" % key)
        buckets[key].append(row)
    offsets = {key: 0 for key in PROBE_CELL_ORDER}
    selected: list[Mapping[str, Any]] = []
    while len(selected) < limit:
        progressed = False
        for key in PROBE_CELL_ORDER:
            offset = offsets[key]
            if offset >= len(buckets[key]):
                continue
            selected.append(buckets[key][offset])
            offsets[key] += 1
            progressed = True
            if len(selected) == limit:
                break
        if not progressed:
            break
    return selected


def _logical_sha256(
    *,
    manifest: Mapping[str, Any],
    manifest_file_sha: str,
    config: ProbeConfig,
    runtime_versions: Mapping[str, str],
    results: Sequence[Mapping[str, Any]],
    exact_groups: Sequence[Mapping[str, Any]],
    duplicate_pairs: Sequence[Mapping[str, Any]],
    audit_pairs: Sequence[Mapping[str, Any]],
) -> str:
    stable_fields = (
        "candidate_rank",
        "candidate_id",
        "asset_key",
        "request_url",
        "status",
        "attempt_count",
        "final_url",
        "http_status",
        "response_mime",
        "response_bytes",
        "content_sha256",
        "original_format",
        "original_mode",
        "original_width",
        "original_height",
        "frame_count",
        "exif_orientation",
        "orientation_applied",
        "oriented_width",
        "oriented_height",
        "alpha_composited",
        "icc_profile_present",
        "color_normalization",
        "normalized_width",
        "normalized_height",
        "pixel_sha256",
        "phash_256",
        "error_kind",
    )
    value = {
        "probe_version": PROBE_VERSION,
        "identity_profile": IDENTITY_PROFILE,
        "pixel_hash_version": PIXEL_HASH_VERSION,
        "phash_version": PHASH_VERSION,
        "runtime_versions": dict(runtime_versions),
        "input_manifest_sha256": manifest["manifest_sha256"],
        "input_manifest_file_sha256": manifest_file_sha,
        "config": {
            "max_bytes": config.max_bytes,
            "connect_timeout": config.connect_timeout,
            "read_timeout": config.read_timeout,
            "max_attempts": config.max_attempts,
        },
        "results": [],
        "exact_pixel_duplicate_groups": list(exact_groups),
        "phash_duplicate_pairs_le_8": list(duplicate_pairs),
        "phash_audit_pairs_9_16": list(audit_pairs),
    }
    for row in sorted(results, key=lambda item: int(item["candidate_rank"])):
        normalized = {field: row.get(field) for field in stable_fields}
        for field in ("orientation_applied", "alpha_composited", "icc_profile_present"):
            if normalized[field] is not None:
                normalized[field] = bool(normalized[field])
        value["results"].append(normalized)
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _build_enriched_manifest(
    conn: sqlite3.Connection,
    *,
    manifest: Mapping[str, Any],
    manifest_file_sha: str,
    config: ProbeConfig,
    candidate_validator: Callable[[Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    results = [dict(row) for row in conn.execute("SELECT * FROM candidate_results ORDER BY candidate_rank")]
    attempts = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM fetch_attempts ORDER BY candidate_id,attempt_no"
        )
    ]
    if any(row["status"] == "pending" for row in results):
        raise RuntimeError("cannot build final manifest with pending candidates")
    successful = [row for row in results if row["status"] == "success"]
    exact_groups, duplicate_pairs, audit_pairs = build_duplicate_evidence(successful)
    duplicate_by_id: dict[str, tuple[str, str | None]] = {}
    for group in exact_groups:
        representative = str(group["representative_candidate_id"])
        for candidate_id in group["member_candidate_ids"]:
            duplicate_by_id[str(candidate_id)] = (
                str(group["group_id"]),
                None if candidate_id == representative else representative,
            )
    phash_matches: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for pair in duplicate_pairs:
        left = str(pair["candidate_id_a"])
        right = str(pair["candidate_id_b"])
        distance = int(pair["phash_distance"])
        phash_matches[left].append({"candidate_id": right, "distance": distance})
        phash_matches[right].append({"candidate_id": left, "distance": distance})
    for values in phash_matches.values():
        values.sort(key=lambda row: (row["distance"], row["candidate_id"]))

    output = copy.deepcopy(dict(manifest))
    output.pop("manifest_sha256", None)
    result_by_id = {str(row["candidate_id"]): row for row in results}
    for candidate in output["candidates"]:
        result = result_by_id[str(candidate["candidate_id"])]
        group_id, duplicate_of = duplicate_by_id.get(
            str(candidate["candidate_id"]), (None, None)
        )
        candidate.update(
            {
                "probe_status": result["status"],
                "probe_final_url": result["final_url"],
                "http_status": result["http_status"],
                "response_mime": result["response_mime"],
                "response_bytes": result["response_bytes"],
                "content_sha256": result["content_sha256"],
                "original_format": result["original_format"],
                "original_mode": result["original_mode"],
                "original_width": result["original_width"],
                "original_height": result["original_height"],
                "frame_count": result["frame_count"],
                "exif_orientation": result["exif_orientation"],
                "orientation_applied": (
                    bool(result["orientation_applied"])
                    if result["orientation_applied"] is not None
                    else None
                ),
                "oriented_width": result["oriented_width"],
                "oriented_height": result["oriented_height"],
                "alpha_composited": (
                    bool(result["alpha_composited"])
                    if result["alpha_composited"] is not None
                    else None
                ),
                "icc_profile_present": (
                    bool(result["icc_profile_present"])
                    if result["icc_profile_present"] is not None
                    else None
                ),
                "color_normalization": result["color_normalization"],
                "normalized_width": result["normalized_width"],
                "normalized_height": result["normalized_height"],
                "pixel_sha256": result["pixel_sha256"],
                "phash_256": result["phash_256"],
                "exact_duplicate_group": group_id,
                "is_exact_pixel_duplicate": group_id is not None,
                "duplicate_of": duplicate_of,
                "auto_exclude_exact_duplicate": duplicate_of is not None,
                "phash_le8_matches": phash_matches.get(str(candidate["candidate_id"]), []),
                "has_phash_le8_candidate": bool(
                    phash_matches.get(str(candidate["candidate_id"]), [])
                ),
                "probe_attempt_count": result["attempt_count"],
                "probe_elapsed_ms": result["elapsed_ms"],
                "probe_completed_at": result["completed_at"],
                "probe_error_kind": result["error_kind"],
                "probe_error_message": result["error_message"],
            }
        )

    run = dict(conn.execute("SELECT * FROM probe_run WHERE run_id=1").fetchone())
    runtime_versions = _validate_runtime_versions(
        json.loads(str(run["runtime_versions_json"]))
    )
    metrics = _metrics(results, attempts)
    logical = _logical_sha256(
        manifest=manifest,
        manifest_file_sha=manifest_file_sha,
        config=config,
        runtime_versions=runtime_versions,
        results=results,
        exact_groups=exact_groups,
        duplicate_pairs=duplicate_pairs,
        audit_pairs=audit_pairs,
    )
    output["probe_contract"] = {
        "probe_version": PROBE_VERSION,
        "identity_profile": IDENTITY_PROFILE,
        "pixel_hash_version": PIXEL_HASH_VERSION,
        "phash_version": PHASH_VERSION,
        "runtime_versions": runtime_versions,
        "input_manifest_sha256": manifest["manifest_sha256"],
        "input_manifest_file_sha256": manifest_file_sha,
        "input_manifest_filename": Path(run["input_manifest_path"]).name,
        "source_request_profile": SOURCE_PROFILE,
        "normalized_long_edge": NORMALIZED_LONG_EDGE,
        "max_bytes": config.max_bytes,
        "connect_timeout": config.connect_timeout,
        "read_timeout": config.read_timeout,
        "max_attempts": config.max_attempts,
        "workers": config.workers,
        "images_persisted": False,
        "started_at": run["started_at"],
        "completed_at": run["completed_at"],
        "metrics": metrics,
        "logical_sha256": logical,
    }
    output["exact_pixel_duplicate_groups"] = exact_groups
    output["phash_duplicate_pairs_le_8"] = duplicate_pairs
    output["phash_audit_pairs_9_16"] = audit_pairs
    output["probe_attempts"] = attempts
    output["manifest_sha256"] = manifest_sha256(output)
    (candidate_validator or validate_candidate_manifest)(output)
    return output


def validate_enriched_manifest(
    payload: Mapping[str, Any],
    *,
    input_manifest: Mapping[str, Any],
    input_manifest_file_sha256: str,
    candidate_validator: Callable[[Mapping[str, Any]], Any] | None = None,
) -> None:
    """Recompute the final accounting, pair evidence, and logical digest."""
    (candidate_validator or validate_candidate_manifest)(payload)
    if payload.get("contract") != input_manifest.get("contract"):
        raise ValueError("enriched manifest changed the selection contract")
    probe_contract = payload.get("probe_contract")
    if not isinstance(probe_contract, Mapping):
        raise ValueError("enriched manifest is missing probe_contract")
    expected_contract = {
        "probe_version": PROBE_VERSION,
        "identity_profile": IDENTITY_PROFILE,
        "pixel_hash_version": PIXEL_HASH_VERSION,
        "phash_version": PHASH_VERSION,
        "input_manifest_sha256": input_manifest["manifest_sha256"],
        "input_manifest_file_sha256": input_manifest_file_sha256,
        "source_request_profile": SOURCE_PROFILE,
        "normalized_long_edge": NORMALIZED_LONG_EDGE,
        "images_persisted": False,
    }
    for field, expected in expected_contract.items():
        if probe_contract.get(field) != expected:
            raise ValueError("enriched probe contract mismatch: %s" % field)
    runtime_versions = _validate_runtime_versions(probe_contract.get("runtime_versions"))

    output_candidates = payload.get("candidates")
    input_candidates = input_manifest.get("candidates")
    if not isinstance(output_candidates, list) or not isinstance(input_candidates, list):
        raise ValueError("candidate lists are required")
    if len(output_candidates) != len(input_candidates):
        raise ValueError("enriched candidate count changed")
    reconstructed: list[dict[str, Any]] = []
    for original, enriched in zip(input_candidates, output_candidates):
        if any(enriched.get(field) != value for field, value in original.items()):
            raise ValueError("enriched manifest changed candidate source evidence")
        status = enriched.get("probe_status")
        if status not in {"success", "failed"}:
            raise ValueError("every enriched candidate must have a terminal probe_status")
        if status == "success":
            hashes = (
                enriched.get("content_sha256"),
                enriched.get("pixel_sha256"),
                enriched.get("phash_256"),
            )
            if any(
                not isinstance(value, str)
                or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)
                for value in hashes
            ):
                raise ValueError("successful candidate has invalid image hashes")
            if not (
                1 <= int(enriched.get("normalized_width") or 0) <= NORMALIZED_LONG_EDGE
                and 1 <= int(enriched.get("normalized_height") or 0) <= NORMALIZED_LONG_EDGE
            ):
                raise ValueError("successful candidate has invalid normalized dimensions")
            if enriched.get("probe_error_kind") is not None:
                raise ValueError("successful candidate carries probe_error_kind")
        elif not enriched.get("probe_error_kind"):
            raise ValueError("failed candidate is missing probe_error_kind")
        reconstructed.append(
            {
                "candidate_rank": enriched["candidate_rank"],
                "candidate_id": enriched["candidate_id"],
                "asset_key": enriched["asset_key"],
                "request_url": enriched["request_url"],
                "status": status,
                "attempt_count": enriched.get("probe_attempt_count"),
                "elapsed_ms": enriched.get("probe_elapsed_ms"),
                "final_url": enriched.get("probe_final_url"),
                "http_status": enriched.get("http_status"),
                "response_mime": enriched.get("response_mime"),
                "response_bytes": enriched.get("response_bytes"),
                "content_sha256": enriched.get("content_sha256"),
                "original_format": enriched.get("original_format"),
                "original_mode": enriched.get("original_mode"),
                "original_width": enriched.get("original_width"),
                "original_height": enriched.get("original_height"),
                "frame_count": enriched.get("frame_count"),
                "exif_orientation": enriched.get("exif_orientation"),
                "orientation_applied": enriched.get("orientation_applied"),
                "oriented_width": enriched.get("oriented_width"),
                "oriented_height": enriched.get("oriented_height"),
                "alpha_composited": enriched.get("alpha_composited"),
                "icc_profile_present": enriched.get("icc_profile_present"),
                "color_normalization": enriched.get("color_normalization"),
                "normalized_width": enriched.get("normalized_width"),
                "normalized_height": enriched.get("normalized_height"),
                "pixel_sha256": enriched.get("pixel_sha256"),
                "phash_256": enriched.get("phash_256"),
                "error_kind": enriched.get("probe_error_kind"),
            }
        )

    successful = [row for row in reconstructed if row["status"] == "success"]
    exact_groups, duplicate_pairs, audit_pairs = build_duplicate_evidence(successful)
    if payload.get("exact_pixel_duplicate_groups") != exact_groups:
        raise ValueError("exact pixel duplicate groups are incomplete or unordered")
    if payload.get("phash_duplicate_pairs_le_8") != duplicate_pairs:
        raise ValueError("pHash <=8 pairs are incomplete or unordered")
    if payload.get("phash_audit_pairs_9_16") != audit_pairs:
        raise ValueError("pHash 9..16 audit pairs are incomplete or unordered")

    expected_exact: dict[str, tuple[str, str | None]] = {}
    for group in exact_groups:
        representative = str(group["representative_candidate_id"])
        for candidate_id in group["member_candidate_ids"]:
            expected_exact[str(candidate_id)] = (
                str(group["group_id"]),
                None if candidate_id == representative else representative,
            )
    expected_matches: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for pair in duplicate_pairs:
        left, right = str(pair["candidate_id_a"]), str(pair["candidate_id_b"])
        distance = int(pair["phash_distance"])
        expected_matches[left].append({"candidate_id": right, "distance": distance})
        expected_matches[right].append({"candidate_id": left, "distance": distance})
    for values in expected_matches.values():
        values.sort(key=lambda row: (row["distance"], row["candidate_id"]))
    for candidate in output_candidates:
        group_id, duplicate_of = expected_exact.get(candidate["candidate_id"], (None, None))
        if candidate.get("exact_duplicate_group") != group_id:
            raise ValueError("candidate exact duplicate group flag mismatch")
        if candidate.get("duplicate_of") != duplicate_of:
            raise ValueError("candidate duplicate_of flag mismatch")
        if candidate.get("is_exact_pixel_duplicate") is not (group_id is not None):
            raise ValueError("candidate exact duplicate boolean mismatch")
        if candidate.get("auto_exclude_exact_duplicate") is not (duplicate_of is not None):
            raise ValueError("candidate auto-exclude duplicate flag mismatch")
        if candidate.get("phash_le8_matches") != expected_matches.get(
            candidate["candidate_id"], []
        ):
            raise ValueError("candidate pHash match flags are incomplete")
        if candidate.get("has_phash_le8_candidate") is not bool(
            expected_matches.get(candidate["candidate_id"], [])
        ):
            raise ValueError("candidate pHash match boolean mismatch")

    attempts = payload.get("probe_attempts")
    if not isinstance(attempts, list):
        raise ValueError("probe_attempts must be a list")
    attempts_by_id: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for attempt in attempts:
        if not isinstance(attempt, Mapping):
            raise ValueError("every probe attempt must be an object")
        attempts_by_id[str(attempt.get("candidate_id"))].append(attempt)
    for candidate in output_candidates:
        values = attempts_by_id.get(str(candidate["candidate_id"]), [])
        if len(values) != candidate.get("probe_attempt_count"):
            raise ValueError("candidate attempt accounting mismatch")
        if [row.get("attempt_no") for row in values] != list(range(1, len(values) + 1)):
            raise ValueError("candidate attempt numbering is not contiguous")

    metrics = _metrics(reconstructed, attempts)
    if probe_contract.get("metrics") != metrics:
        raise ValueError("probe metrics do not match candidate/attempt accounting")
    config = ProbeConfig(
        workers=int(probe_contract["workers"]),
        max_bytes=int(probe_contract["max_bytes"]),
        connect_timeout=float(probe_contract["connect_timeout"]),
        read_timeout=float(probe_contract["read_timeout"]),
        max_attempts=int(probe_contract["max_attempts"]),
    )
    config.validate()
    logical = _logical_sha256(
        manifest=input_manifest,
        manifest_file_sha=input_manifest_file_sha256,
        config=config,
        runtime_versions=runtime_versions,
        results=reconstructed,
        exact_groups=exact_groups,
        duplicate_pairs=duplicate_pairs,
        audit_pairs=audit_pairs,
    )
    if probe_contract.get("logical_sha256") != logical:
        raise ValueError("probe logical SHA mismatch")


def _write_json_no_clobber(path: Path, payload: Mapping[str, Any]) -> None:
    if path.exists():
        raise FileExistsError("immutable output already exists: %s" % path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical_json(payload) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp, path)
        except FileExistsError as exc:
            raise FileExistsError("immutable output already exists: %s" % path) from exc
    finally:
        temp.unlink(missing_ok=True)


def run_candidate_probe(
    *,
    manifest_path: Path,
    output_path: Path,
    staging_path: Path | None = None,
    config: ProbeConfig = ProbeConfig(),
    resume: bool = False,
    stop_after: int | None = None,
    fetcher: Callable[..., FetchPayload] = network_fetch,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    config.validate()
    if stop_after is not None and (
        isinstance(stop_after, bool) or not isinstance(stop_after, int) or stop_after < 1
    ):
        raise ValueError("stop_after must be a positive integer")
    manifest_path = manifest_path.resolve()
    output_path = output_path.resolve()
    staging_path = (
        staging_path.resolve()
        if staging_path is not None
        else output_path.with_name(output_path.name + ".staging.sqlite")
    )
    if len({manifest_path, output_path, staging_path}) != 3:
        raise ValueError("manifest, staging, and output paths must be distinct")
    if output_path.exists():
        raise FileExistsError("immutable output already exists: %s" % output_path)

    manifest, manifest_file_sha = load_probe_manifest(manifest_path)
    candidates = _validate_probe_manifest(manifest)
    if staging_path.exists() and not resume:
        raise FileExistsError("staging artifact exists; pass --resume: %s" % staging_path)
    if not staging_path.exists() and resume:
        raise FileNotFoundError("cannot resume missing staging artifact: %s" % staging_path)
    if not staging_path.exists():
        _create_staging(
            staging_path,
            manifest_path=manifest_path,
            manifest_file_sha=manifest_file_sha,
            manifest=manifest,
            candidates=candidates,
            config=config,
        )

    conn = sqlite3.connect(staging_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    executor: ThreadPoolExecutor | None = None
    try:
        _verify_staging(
            conn,
            manifest_path=manifest_path,
            manifest_file_sha=manifest_file_sha,
            manifest=manifest,
            candidates=candidates,
            config=config,
        )
        pending_ids = {
            str(row[0])
            for row in conn.execute(
                "SELECT candidate_id FROM candidate_results WHERE status='pending'"
            )
        }
        pending = [row for row in candidates if str(row["candidate_id"]) in pending_ids]
        selected_pending = (
            pending if stop_after is None else _select_smoke_pending(pending, stop_after)
        )
        executor = ThreadPoolExecutor(
            max_workers=config.workers,
            thread_name_prefix="divisare-probe",
        )
        futures: dict[Future[dict[str, Any]], str] = {
            executor.submit(
                _probe_candidate,
                row,
                config=config,
                fetcher=fetcher,
                sleep=sleep,
            ): str(row["candidate_id"])
            for row in selected_pending
        }
        for future in as_completed(futures):
            _write_probe_result(conn, future.result())
        executor.shutdown(wait=True, cancel_futures=True)
        executor = None

        if file_sha256(manifest_path) != manifest_file_sha:
            raise RuntimeError("candidate manifest changed during probe")
        pending_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM candidate_results WHERE status='pending'"
            ).fetchone()[0]
        )
        if stop_after is not None:
            completed_at = utc_now()
            conn.execute(
                """
                UPDATE probe_run
                SET status='running',updated_at=?,completed_at=NULL,error=NULL
                WHERE run_id=1
                """,
                (completed_at,),
            )
            conn.commit()
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("staging SQLite quick_check failed")
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("staging SQLite foreign-key check failed")
            staged_results = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM candidate_results ORDER BY candidate_rank"
                )
            ]
            staged_attempts = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM fetch_attempts ORDER BY candidate_id,attempt_no"
                )
            ]
            return {
                "status": "running",
                "final_output_written": False,
                "staging_path": str(staging_path),
                "input_manifest_sha256": manifest["manifest_sha256"],
                "stop_after": stop_after,
                "processed_this_invocation": len(selected_pending),
                "selected_candidate_ids": [
                    str(row["candidate_id"]) for row in selected_pending
                ],
                "selected_cells": [
                    {
                        "discovery_class": str(row["discovery_class"]),
                        "generation_group": str(row["generation_group"]),
                    }
                    for row in selected_pending
                ],
                **_metrics(staged_results, staged_attempts),
            }
        if pending_count:
            raise RuntimeError("probe has %d pending candidates" % pending_count)
        failure_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM candidate_results WHERE status='failed'"
            ).fetchone()[0]
        )
        completed_at = utc_now()
        status = "complete_with_failures" if failure_count else "complete"
        conn.execute(
            "UPDATE probe_run SET status=?,updated_at=?,completed_at=?,error=NULL WHERE run_id=1",
            (status, completed_at, completed_at),
        )
        conn.commit()
        payload = _build_enriched_manifest(
            conn,
            manifest=manifest,
            manifest_file_sha=manifest_file_sha,
            config=config,
        )
        validate_enriched_manifest(
            payload,
            input_manifest=manifest,
            input_manifest_file_sha256=manifest_file_sha,
        )
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("staging SQLite quick_check failed")
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("staging SQLite foreign-key check failed")
    except BaseException as exc:
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)
        try:
            conn.execute(
                "UPDATE probe_run SET status='interrupted',updated_at=?,error=? WHERE run_id=1",
                (utc_now(), str(exc)[:2000]),
            )
            conn.commit()
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

    _write_json_no_clobber(output_path, payload)
    staging_path.unlink()
    return {
        "output_path": str(output_path),
        "manifest_sha256": payload["manifest_sha256"],
        "input_manifest_sha256": manifest["manifest_sha256"],
        "logical_sha256": payload["probe_contract"]["logical_sha256"],
        **payload["probe_contract"]["metrics"],
        "exact_duplicate_group_count": len(payload["exact_pixel_duplicate_groups"]),
        "phash_duplicate_pair_count": len(payload["phash_duplicate_pairs_le_8"]),
        "phash_audit_pair_count": len(payload["phash_audit_pairs_9_16"]),
    }
