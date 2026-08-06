"""Architizer source census and sidecar recrawl primitives.

This module deliberately does not import the legacy Architizer crawler.  The
legacy ``data/crawl/architizer.db`` is opened read-only and immutable; every
network observation is written to a separate state database and to
content-addressed gzip snapshots.

Only sitemap children explicitly listed by ``/sitemap.xml`` are accepted.
Project/firm page fetching is opt-in through the smoke/full runner.
"""

from __future__ import annotations

import contextlib
import ctypes
import dataclasses
import email.utils
import gzip
import hashlib
import html
import io
import json
import math
import os
import random
import re
import sqlite3
import statistics
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE_DB = REPO_ROOT / "data" / "crawl" / "architizer.db"
DEFAULT_STATE_DB = (
    REPO_ROOT / "data" / "enrichment" / "architizer_source_recrawl_v2.db"
)
DEFAULT_SNAPSHOT_DIR = (
    REPO_ROOT / "data" / "enrichment" / "architizer_html_snapshots_v2"
)
OFFICIAL_SITEMAP_URL = "https://architizer.com/sitemap.xml"
ARCHITIZER_HOST = "architizer.com"
WINNERS_HOST = "winners.architizer.com"
PARSER_VERSION = "architizer-source-parser-v2.3.0"
STATE_SCHEMA_VERSION = "2.2"
METADATA_VERSION = "architizer-source-metadata-v2.3"
SMOKE_GATE_POLICY_VERSION = "architizer-smoke-gate-v2"
SNAPSHOT_REPARSE_GATE_POLICY_VERSION = "architizer-snapshot-reparse-gate-v1"
NETWORK_SNAPSHOT_INTEGRITY_POLICY_VERSION = (
    "architizer-network-snapshot-integrity-v1"
)
FULL_RUN_POSTPROCESS_RECOVERY_VERSION = (
    "architizer-full-run-postprocess-recovery-v1"
)
FULL_RUN_SQL_VARIABLE_ERROR = "OperationalError: too many SQL variables"
MINIMUM_FULL_DELAY_SECONDS = 2.0
MAX_SNAPSHOT_RESPONSE_BYTES = 50 * 1024 * 1024
MAX_COMPRESSED_SNAPSHOT_BYTES = 64 * 1024 * 1024
KNOWN_IDENTITY_EXCEPTIONS = {
    "https://architizer.com/projects/requiem-for-ruins-2/": {
        "reason": (
            "legacy done-row mismatch currently resolves as firm "
            "firms.firm.183312"
        ),
        "identity_status": "conflict",
        "parse_status": "no_content",
        "final_url": (
            "https://architizer.com/firms/multitude-of-sins/"
            "?notfound_project=1"
        ),
        "required_errors": (
            "final_url_slug_mismatch",
            "canonical_slug_mismatch",
            "global_id_wrong_entity_type",
        ),
    },
}
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36 "
    "ArchitizerSourceAudit/2.0"
)
PROJECT_FIELDS = (
    "project_id",
    "global_id",
    "slug",
    "name",
    "firm_slug",
    "firm_name",
    "location",
    "completion_year",
    "construction_status",
    "size_bucket",
    "description",
    "description_short",
    "categories",
    "cover_image_url",
    "gallery_image_urls",
    "image_global_ids",
    "published_time",
    "modified_time",
)
FIRM_FIELDS = (
    "slug",
    "name",
    "description",
    "office_locations",
    "project_urls",
    "social_links",
)
TARGET_NETWORK_STATE_FIELDS = (
    "status",
    "retryable",
    "attempt_count",
    "next_retry_at",
    "last_attempt_at",
    "last_error",
    "last_http_status",
    "last_snapshot_sha256",
    "last_parse_status",
)


class RecrawlError(RuntimeError):
    """Base error for the source-census/recrawl workflow."""


class LockHeldError(RecrawlError):
    """Raised when a second process attempts to use the same sidecar."""


class CircuitOpenError(RecrawlError):
    """Raised after repeated blocking/rate-limit responses."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest().upper()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _measure_stable_file_identity(path: Path) -> dict[str, Any]:
    """Measure SHA/size while rejecting replacement or mutation mid-read."""

    try:
        before = path.stat()
        digest = sha256_file(path)
        after = path.stat()
    except OSError as exc:
        raise RecrawlError(f"cannot measure immutable source DB: {path}") from exc
    before_file_id = (before.st_dev, before.st_ino)
    after_file_id = (after.st_dev, after.st_ino)
    if (
        not path.is_file()
        or before_file_id != after_file_id
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise RecrawlError(
            f"immutable source DB changed while it was measured: {path}"
        )
    return {"sha256": digest, "size": int(after.st_size)}


def _require_same_file_identity(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> None:
    if (
        before.get("sha256") != after.get("sha256")
        or before.get("size") != after.get("size")
    ):
        raise RecrawlError(
            "legacy source DB SHA-256/size changed during offline finalization"
        )


def normalize_entity_url(url: str, entity_type: str | None = None) -> str:
    parsed = urllib.parse.urlsplit(url.strip())
    host = (parsed.hostname or "").lower()
    if host == "www.architizer.com":
        host = ARCHITIZER_HOST
    if parsed.scheme and parsed.scheme.lower() not in {"http", "https"}:
        raise RecrawlError(f"unsupported entity URL scheme: {url}")
    if host and host != ARCHITIZER_HOST:
        raise RecrawlError(f"external entity URL is not allowed: {url}")
    if not host:
        raise RecrawlError(f"entity URL must be absolute: {url}")
    path = re.sub(r"/+", "/", parsed.path or "/")
    if entity_type in {"project", "firm"} and not path.endswith("/"):
        path += "/"
    return urllib.parse.urlunsplit(("https", host, path, "", ""))


def normalize_sitemap_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url.strip())
    host = (parsed.hostname or "").lower()
    if host == "www.architizer.com":
        host = ARCHITIZER_HOST
    if parsed.scheme.lower() != "https" or host != ARCHITIZER_HOST:
        raise RecrawlError(f"non-official sitemap URL is not allowed: {url}")
    if parsed.username or parsed.password or parsed.port not in {None, 443}:
        raise RecrawlError(f"invalid official sitemap authority: {url}")
    query = urllib.parse.urlencode(
        urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    )
    return urllib.parse.urlunsplit(("https", host, parsed.path, query, ""))


def validate_official_sitemap_url(
    url: str,
    *,
    index: bool = False,
) -> str:
    normalized = normalize_sitemap_url(url)
    parsed = urllib.parse.urlsplit(normalized)
    if index:
        if normalized != OFFICIAL_SITEMAP_URL:
            raise RecrawlError(
                "source census accepts only the official sitemap index: "
                f"{OFFICIAL_SITEMAP_URL}"
            )
        return normalized
    if not re.fullmatch(r"/sitemap-(?:projects|firms)\.xml", parsed.path):
        raise RecrawlError(f"unrecognized official entity sitemap path: {url}")
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    if query and (
        set(query) != {"p"}
        or len(query["p"]) != 1
        or not query["p"][0].isdigit()
        or int(query["p"][0]) < 1
    ):
        raise RecrawlError(f"invalid official entity sitemap query: {url}")
    return normalized


def slug_from_url(url: str, entity_type: str) -> str | None:
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host == f"www.{ARCHITIZER_HOST}":
        host = ARCHITIZER_HOST
    if parsed.scheme and parsed.scheme.lower() not in {"http", "https"}:
        return None
    if host and host != ARCHITIZER_HOST:
        return None
    expected = "projects" if entity_type == "project" else "firms"
    parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[-2] == expected:
        return parts[-1]
    return None


def _safe_text(value: Any) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def _safe_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        match = re.search(r"\b(\d{1,10})\b", value)
        if match:
            return int(match.group(1))
    return None


def _year(value: Any) -> int | None:
    text = _safe_text(value)
    if not text:
        return None
    match = re.search(r"\b(18\d{2}|19\d{2}|20\d{2}|21\d{2})\b", text)
    return int(match.group(1)) if match else None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _dedupe(values: Iterable[Any]) -> list[Any]:
    output: list[Any] = []
    seen: set[str] = set()
    for value in values:
        if value is None or value == "":
            continue
        key = canonical_json(value)
        if key in seen:
            continue
        seen.add(key)
        output.append(value)
    return output


class SidecarLock:
    """Portable exclusive lock implemented with O_EXCL.

    The lock file contains only PID/timestamp/state path.  Normal exit removes
    it.  A stale lock is intentionally not auto-deleted because two operators
    must never silently overlap; recovery is an explicit CLI action.
    """

    def __init__(self, state_path: Path):
        self.state_path = state_path.resolve()
        self.path = Path(str(self.state_path) + ".lock")
        self._held = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = canonical_json(
            {
                "pid": os.getpid(),
                "acquired_at": utc_now(),
                "state": str(self.state_path),
            }
        ).encode("utf-8")
        try:
            descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError as exc:
            detail = ""
            with contextlib.suppress(OSError, UnicodeDecodeError):
                detail = self.path.read_text(encoding="utf-8")
            raise LockHeldError(
                f"sidecar lock already exists: {self.path}; {detail}"
            ) from exc
        try:
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)
        self._held = True

    def release(self) -> None:
        if self._held:
            with contextlib.suppress(FileNotFoundError):
                self.path.unlink()
            self._held = False

    def __enter__(self) -> "SidecarLock":
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


def inspect_sidecar_lock(state_path: Path) -> dict[str, Any]:
    state_path = state_path.resolve()
    lock_path = Path(str(state_path) + ".lock")
    if not lock_path.exists():
        return {"exists": False, "path": str(lock_path)}
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {
            "exists": True,
            "path": str(lock_path),
            "valid": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    pid = payload.get("pid")
    acquired_at = payload.get("acquired_at")
    state_value = payload.get("state")
    valid = (
        isinstance(pid, int)
        and pid > 0
        and isinstance(acquired_at, str)
        and isinstance(state_value, str)
        and Path(state_value).resolve() == state_path
    )
    process_alive = _process_alive(pid) if isinstance(pid, int) and pid > 0 else None
    age_seconds: float | None = None
    if isinstance(acquired_at, str):
        with contextlib.suppress(ValueError):
            parsed = datetime.fromisoformat(acquired_at)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            age_seconds = max(
                0.0,
                (datetime.now(timezone.utc) - parsed).total_seconds(),
            )
    return {
        "exists": True,
        "path": str(lock_path),
        "valid": valid,
        "pid": pid,
        "process_alive": process_alive,
        "acquired_at": acquired_at,
        "age_seconds": age_seconds,
        "state": state_value,
    }


def _process_alive(pid: int) -> bool | None:
    if os.name == "nt":
        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ctypes.set_last_error(0)
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if not handle:
            error = ctypes.get_last_error()
            if error in {5}:  # access denied: existence cannot be disproven
                return None
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return None
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return None


def recover_stale_sidecar_lock(
    state_path: Path,
    *,
    confirmed: bool,
    minimum_age_seconds: float = 300.0,
) -> dict[str, Any]:
    """Explicitly remove a validated lock only when its PID is not alive."""

    if not confirmed:
        raise RecrawlError(
            "stale-lock recovery requires explicit confirmation"
        )
    inspection = inspect_sidecar_lock(state_path)
    if not inspection.get("exists"):
        return {**inspection, "removed": False}
    if not inspection.get("valid"):
        raise RecrawlError(
            "lock metadata is invalid; refuse automatic removal"
        )
    if inspection.get("process_alive") is not False:
        raise RecrawlError(
            "lock owner is alive or cannot be proven dead; refuse removal"
        )
    age = inspection.get("age_seconds")
    if age is None or age < minimum_age_seconds:
        raise RecrawlError(
            f"lock is younger than minimum age {minimum_age_seconds}s"
        )
    lock_path = Path(inspection["path"])
    lock_path.unlink()
    return {**inspection, "removed": True, "removed_at": utc_now()}


def _same_existing_file(left: Path, right: Path) -> bool:
    if left.resolve() == right.resolve():
        return True
    if left.exists() and right.exists():
        with contextlib.suppress(OSError):
            return os.path.samefile(left, right)
    return False


def validate_runtime_paths(
    *,
    source_path: Path,
    state_path: Path,
    snapshot_root: Path,
) -> None:
    source = source_path.resolve()
    state = state_path.resolve()
    snapshots = snapshot_root.resolve()
    if _same_existing_file(source, state):
        raise RecrawlError("state DB must not alias the immutable source DB")
    if _same_existing_file(source, snapshots):
        raise RecrawlError("snapshot directory must not alias the source DB")
    if _same_existing_file(state, snapshots):
        raise RecrawlError("snapshot directory must not alias the state DB")
    if source == snapshots or source.is_relative_to(snapshots):
        raise RecrawlError("snapshot root must not contain the source DB")
    if state == snapshots or state.is_relative_to(snapshots):
        raise RecrawlError("snapshot root must not contain the state DB")
    if snapshots.exists() and not snapshots.is_dir():
        raise RecrawlError(f"snapshot root is not a directory: {snapshots}")


def open_legacy_readonly(path: Path) -> sqlite3.Connection:
    path = path.resolve()
    if not path.exists():
        raise RecrawlError(f"legacy source DB not found: {path}")
    connection = sqlite3.connect(
        path.as_uri() + "?mode=ro&immutable=1",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
        connection.close()
        raise RecrawlError("legacy source DB is not query_only")
    return connection


STATE_SCHEMA = """
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS state_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_kind TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    source_db_path TEXT NOT NULL,
    source_db_sha256_before TEXT NOT NULL,
    source_db_sha256_after TEXT,
    source_db_size INTEGER NOT NULL,
    selected_count INTEGER DEFAULT 0,
    summary_json TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS sitemap_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    entity_type TEXT NOT NULL,
    sitemap_url TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    http_status INTEGER,
    final_url TEXT,
    content_type TEXT,
    content_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    gzip_path TEXT NOT NULL,
    parse_status TEXT NOT NULL,
    url_count INTEGER NOT NULL,
    lastmod_min TEXT,
    lastmod_max TEXT,
    error TEXT,
    UNIQUE(run_id, sitemap_url)
);

CREATE TABLE IF NOT EXISTS sitemap_entries (
    run_id INTEGER NOT NULL REFERENCES runs(id),
    snapshot_id INTEGER NOT NULL REFERENCES sitemap_snapshots(id),
    entity_type TEXT NOT NULL,
    source_url TEXT NOT NULL,
    lastmod TEXT,
    discovery_source TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    PRIMARY KEY(run_id, entity_type, source_url)
);

CREATE INDEX IF NOT EXISTS idx_sitemap_entries_run_type
ON sitemap_entries(run_id, entity_type, source_url);

CREATE TABLE IF NOT EXISTS sitemap_entry_occurrences (
    run_id INTEGER NOT NULL REFERENCES runs(id),
    snapshot_id INTEGER NOT NULL REFERENCES sitemap_snapshots(id),
    entity_type TEXT NOT NULL,
    source_url TEXT NOT NULL,
    lastmod TEXT,
    discovery_source TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    PRIMARY KEY(snapshot_id, ordinal)
);

CREATE TABLE IF NOT EXISTS targets (
    url TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    source_lastmod TEXT,
    priority INTEGER NOT NULL,
    primary_reason TEXT NOT NULL,
    status TEXT NOT NULL,
    retryable INTEGER NOT NULL DEFAULT 1,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    last_attempt_at TEXT,
    last_error TEXT,
    last_http_status INTEGER,
    last_snapshot_sha256 TEXT,
    last_parse_status TEXT,
    current_metadata_version_id INTEGER,
    last_good_version_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_targets_schedule
ON targets(status, retryable, priority, entity_type, url);

CREATE TABLE IF NOT EXISTS target_reasons (
    url TEXT NOT NULL REFERENCES targets(url),
    reason TEXT NOT NULL,
    discovery_source TEXT NOT NULL,
    priority INTEGER NOT NULL,
    source_lastmod TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    input_lineage_json TEXT NOT NULL,
    PRIMARY KEY(url, reason, discovery_source)
);

CREATE TABLE IF NOT EXISTS run_targets (
    run_id INTEGER NOT NULL REFERENCES runs(id),
    url TEXT NOT NULL REFERENCES targets(url),
    selection_order INTEGER NOT NULL,
    selected_reason TEXT NOT NULL,
    status_before TEXT NOT NULL,
    status_after TEXT,
    PRIMARY KEY(run_id, url),
    UNIQUE(run_id, selection_order)
);

CREATE TABLE IF NOT EXISTS http_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    target_url TEXT,
    request_kind TEXT NOT NULL,
    requested_url TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    http_status INTEGER,
    final_url TEXT,
    content_type TEXT,
    response_bytes INTEGER NOT NULL,
    sha256 TEXT,
    gzip_path TEXT,
    retryable INTEGER NOT NULL,
    block_signals_json TEXT NOT NULL,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_http_attempts_run_target
ON http_attempts(run_id, target_url, attempt_number);

CREATE TABLE IF NOT EXISTS metadata_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    target_url TEXT NOT NULL REFERENCES targets(url),
    entity_type TEXT NOT NULL,
    snapshot_sha256 TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    metadata_version TEXT NOT NULL,
    parsed_at TEXT NOT NULL,
    parse_status TEXT NOT NULL,
    quality TEXT NOT NULL,
    identity_status TEXT NOT NULL,
    identity_json TEXT NOT NULL,
    raw_embedded_json TEXT NOT NULL,
    dom_json TEXT NOT NULL,
    resolved_json TEXT NOT NULL,
    conflict_json TEXT NOT NULL,
    UNIQUE(target_url, snapshot_sha256, parser_version)
);

CREATE TABLE IF NOT EXISTS run_metadata_versions (
    run_id INTEGER NOT NULL REFERENCES runs(id),
    version_id INTEGER NOT NULL REFERENCES metadata_versions(id),
    target_url TEXT NOT NULL REFERENCES targets(url),
    PRIMARY KEY(run_id, version_id),
    UNIQUE(run_id, target_url)
);

CREATE TABLE IF NOT EXISTS field_observations (
    version_id INTEGER NOT NULL REFERENCES metadata_versions(id),
    field_name TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    raw_value_json TEXT,
    normalized_value_json TEXT,
    parse_status TEXT NOT NULL,
    quality TEXT NOT NULL,
    PRIMARY KEY(version_id, field_name, source_kind)
);

CREATE TABLE IF NOT EXISTS resolved_fields (
    version_id INTEGER NOT NULL REFERENCES metadata_versions(id),
    field_name TEXT NOT NULL,
    value_json TEXT,
    status TEXT NOT NULL,
    quality TEXT NOT NULL,
    conflict_json TEXT,
    PRIMARY KEY(version_id, field_name)
);

CREATE TABLE IF NOT EXISTS current_fields (
    target_url TEXT NOT NULL REFERENCES targets(url),
    field_name TEXT NOT NULL,
    value_json TEXT NOT NULL,
    status TEXT NOT NULL,
    quality TEXT NOT NULL,
    version_id INTEGER NOT NULL REFERENCES metadata_versions(id),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(target_url, field_name)
);

CREATE TABLE IF NOT EXISTS relationships (
    version_id INTEGER NOT NULL REFERENCES metadata_versions(id),
    relation_kind TEXT NOT NULL,
    related_entity_type TEXT NOT NULL,
    related_slug TEXT,
    related_url TEXT,
    source_kind TEXT NOT NULL,
    parse_status TEXT NOT NULL,
    PRIMARY KEY(
        version_id, relation_kind, related_entity_type,
        related_slug, related_url, source_kind
    )
);

CREATE TABLE IF NOT EXISTS legacy_field_comparisons (
    version_id INTEGER NOT NULL REFERENCES metadata_versions(id),
    field_name TEXT NOT NULL,
    legacy_value_json TEXT,
    observed_value_json TEXT,
    comparison_status TEXT NOT NULL,
    PRIMARY KEY(version_id, field_name)
);

CREATE TABLE IF NOT EXISTS award_discoveries (
    run_id INTEGER NOT NULL REFERENCES runs(id),
    award_year INTEGER NOT NULL,
    award_track TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    slug TEXT NOT NULL,
    source_url TEXT NOT NULL,
    discovered_url TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    PRIMARY KEY(run_id, award_year, award_track, entity_type, slug, source_url)
);
"""


SNAPSHOT_REPARSE_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshot_reparse_inputs (
    run_id INTEGER NOT NULL REFERENCES runs(id),
    target_url TEXT NOT NULL REFERENCES targets(url),
    selection_order INTEGER NOT NULL,
    entity_type TEXT NOT NULL CHECK (entity_type IN ('project','firm')),
    selection_kind TEXT NOT NULL CHECK (
        selection_kind IN (
            'firm_last_good_parser_upgrade',
            'project_parser_regression_recovery'
        )
    ),
    source_run_id INTEGER NOT NULL REFERENCES runs(id),
    source_metadata_version_id INTEGER NOT NULL REFERENCES metadata_versions(id),
    source_http_attempt_id INTEGER NOT NULL REFERENCES http_attempts(id),
    request_kind TEXT NOT NULL CHECK (
        request_kind IN ('project_page','firm_page')
    ),
    requested_url TEXT NOT NULL,
    http_outcome TEXT NOT NULL CHECK (http_outcome='success'),
    http_status INTEGER NOT NULL CHECK (http_status=200),
    block_signals_json TEXT NOT NULL CHECK (block_signals_json='[]'),
    attempt_error TEXT CHECK (attempt_error IS NULL),
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256)=64),
    final_url TEXT NOT NULL,
    content_type TEXT NOT NULL,
    response_bytes INTEGER NOT NULL CHECK (response_bytes > 0),
    gzip_path TEXT NOT NULL,
    gzip_sha256 TEXT NOT NULL CHECK (length(gzip_sha256)=64),
    integrity_status TEXT NOT NULL CHECK (integrity_status='verified'),
    target_network_state_json TEXT NOT NULL
        CHECK (json_valid(target_network_state_json)),
    frozen_at TEXT NOT NULL,
    PRIMARY KEY(run_id,target_url),
    UNIQUE(run_id,selection_order)
);

CREATE TABLE IF NOT EXISTS snapshot_reparse_lineage (
    reparse_version_id INTEGER PRIMARY KEY REFERENCES metadata_versions(id),
    reparse_run_id INTEGER NOT NULL REFERENCES runs(id),
    entity_type TEXT NOT NULL CHECK (entity_type IN ('project','firm')),
    selection_kind TEXT NOT NULL CHECK (
        selection_kind IN (
            'firm_last_good_parser_upgrade',
            'project_parser_regression_recovery'
        )
    ),
    source_run_id INTEGER NOT NULL REFERENCES runs(id),
    source_metadata_version_id INTEGER NOT NULL REFERENCES metadata_versions(id),
    source_http_attempt_id INTEGER NOT NULL REFERENCES http_attempts(id),
    request_kind TEXT NOT NULL CHECK (
        request_kind IN ('project_page','firm_page')
    ),
    requested_url TEXT NOT NULL,
    http_outcome TEXT NOT NULL CHECK (http_outcome='success'),
    http_status INTEGER NOT NULL CHECK (http_status=200),
    block_signals_json TEXT NOT NULL CHECK (block_signals_json='[]'),
    attempt_error TEXT CHECK (attempt_error IS NULL),
    target_url TEXT NOT NULL REFERENCES targets(url),
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256)=64),
    final_url TEXT NOT NULL,
    content_type TEXT NOT NULL,
    response_bytes INTEGER NOT NULL CHECK (response_bytes > 0),
    gzip_path TEXT NOT NULL,
    gzip_sha256 TEXT NOT NULL CHECK (length(gzip_sha256)=64),
    integrity_status TEXT NOT NULL CHECK (integrity_status='verified'),
    verified_at TEXT NOT NULL,
    UNIQUE(reparse_run_id,target_url),
    FOREIGN KEY(reparse_run_id,target_url)
        REFERENCES snapshot_reparse_inputs(run_id,target_url)
);

CREATE TRIGGER IF NOT EXISTS snapshot_reparse_inputs_validate_insert
BEFORE INSERT ON snapshot_reparse_inputs
BEGIN
    SELECT CASE WHEN (
        SELECT COUNT(*)
        FROM runs r
        WHERE r.id=NEW.run_id
          AND r.run_kind IN (
              'snapshot_reparse_n10',
              'snapshot_reparse_n100',
              'snapshot_reparse_full'
          )
          AND r.status='running'
    )=1 THEN 1 ELSE RAISE(ABORT,'invalid snapshot reparse run') END;
    SELECT CASE WHEN (
        SELECT COUNT(*)
        FROM metadata_versions m
        JOIN runs r ON r.id=m.run_id
        JOIN targets t ON t.url=m.target_url
        WHERE m.id=NEW.source_metadata_version_id
          AND m.run_id=NEW.source_run_id
          AND m.target_url=NEW.target_url
          AND m.entity_type=NEW.entity_type
          AND (
              (
                  NEW.selection_kind='firm_last_good_parser_upgrade'
                  AND NEW.entity_type='firm'
                  AND m.identity_status='valid'
              )
              OR (
                  NEW.selection_kind='project_parser_regression_recovery'
                  AND NEW.entity_type='project'
                  AND m.identity_status<>'valid'
                  AND t.status='failed'
                  AND t.retryable=0
                  AND t.attempt_count>0
                  AND t.last_attempt_at IS NOT NULL
                  AND t.last_error IS NOT NULL
                  AND t.last_http_status=200
                  AND t.last_parse_status='no_content'
                  AND t.last_snapshot_sha256=m.snapshot_sha256
              )
          )
          AND m.snapshot_sha256=NEW.content_sha256
          AND r.status LIKE 'completed%'
    )=1 THEN 1 ELSE RAISE(ABORT,'invalid source metadata lineage') END;
    SELECT CASE WHEN (
        SELECT COUNT(*)
        FROM http_attempts a
        JOIN metadata_versions m ON m.run_id=a.run_id
        WHERE m.id=NEW.source_metadata_version_id
          AND a.target_url=NEW.target_url
          AND a.request_kind=NEW.request_kind
          AND (
              (NEW.entity_type='firm' AND NEW.request_kind='firm_page')
              OR (
                  NEW.entity_type='project'
                  AND NEW.request_kind='project_page'
              )
          )
          AND a.requested_url=NEW.requested_url
          AND a.outcome=NEW.http_outcome
          AND a.http_status=NEW.http_status
          AND a.block_signals_json=NEW.block_signals_json
          AND a.error IS NEW.attempt_error
          AND a.sha256=NEW.content_sha256
    )=1 THEN 1 ELSE RAISE(ABORT,'source HTTP evidence is not exact') END;
    SELECT CASE WHEN (
        SELECT COUNT(*)
        FROM http_attempts a
        WHERE a.id=NEW.source_http_attempt_id
          AND a.run_id=NEW.source_run_id
          AND a.target_url=NEW.target_url
          AND a.request_kind=NEW.request_kind
          AND a.requested_url=NEW.requested_url
          AND a.outcome=NEW.http_outcome
          AND a.http_status=NEW.http_status
          AND a.block_signals_json=NEW.block_signals_json
          AND a.error IS NEW.attempt_error
          AND a.sha256=NEW.content_sha256
          AND a.final_url=NEW.final_url
          AND a.content_type=NEW.content_type
          AND a.response_bytes=NEW.response_bytes
          AND a.gzip_path=NEW.gzip_path
          AND NEW.source_run_id=(
              SELECT m.run_id FROM metadata_versions m
              WHERE m.id=NEW.source_metadata_version_id
          )
    )=1 THEN 1 ELSE RAISE(ABORT,'source HTTP attempt mismatch') END;
END;

CREATE TRIGGER IF NOT EXISTS snapshot_reparse_lineage_validate_insert
BEFORE INSERT ON snapshot_reparse_lineage
BEGIN
    SELECT CASE WHEN (
        SELECT COUNT(*)
        FROM snapshot_reparse_inputs i
        WHERE i.run_id=NEW.reparse_run_id
          AND i.target_url=NEW.target_url
          AND i.entity_type=NEW.entity_type
          AND i.selection_kind=NEW.selection_kind
          AND i.source_run_id=NEW.source_run_id
          AND i.source_metadata_version_id=NEW.source_metadata_version_id
          AND i.source_http_attempt_id=NEW.source_http_attempt_id
          AND i.request_kind=NEW.request_kind
          AND i.requested_url=NEW.requested_url
          AND i.http_outcome=NEW.http_outcome
          AND i.http_status=NEW.http_status
          AND i.block_signals_json=NEW.block_signals_json
          AND i.attempt_error IS NEW.attempt_error
          AND i.content_sha256=NEW.content_sha256
          AND i.final_url=NEW.final_url
          AND i.content_type=NEW.content_type
          AND i.response_bytes=NEW.response_bytes
          AND i.gzip_path=NEW.gzip_path
          AND i.gzip_sha256=NEW.gzip_sha256
          AND i.integrity_status=NEW.integrity_status
    )=1 THEN 1 ELSE RAISE(ABORT,'snapshot reparse input mismatch') END;
    SELECT CASE WHEN (
        SELECT COUNT(*)
        FROM metadata_versions m
        JOIN runs r ON r.id=m.run_id
        WHERE m.id=NEW.reparse_version_id
          AND m.run_id=NEW.reparse_run_id
          AND m.target_url=NEW.target_url
          AND m.entity_type=NEW.entity_type
          AND m.snapshot_sha256=NEW.content_sha256
          AND m.parser_version=r.parser_version
          AND m.id<>NEW.source_metadata_version_id
    )=1 THEN 1 ELSE RAISE(ABORT,'reparse metadata lineage mismatch') END;
END;

CREATE TRIGGER IF NOT EXISTS snapshot_reparse_inputs_no_update
BEFORE UPDATE ON snapshot_reparse_inputs
BEGIN
    SELECT RAISE(ABORT,'snapshot reparse inputs are append-only');
END;

CREATE TRIGGER IF NOT EXISTS snapshot_reparse_inputs_no_delete
BEFORE DELETE ON snapshot_reparse_inputs
BEGIN
    SELECT RAISE(ABORT,'snapshot reparse inputs are append-only');
END;

CREATE TRIGGER IF NOT EXISTS snapshot_reparse_lineage_no_update
BEFORE UPDATE ON snapshot_reparse_lineage
BEGIN
    SELECT RAISE(ABORT,'snapshot reparse lineage is append-only');
END;

CREATE TRIGGER IF NOT EXISTS snapshot_reparse_lineage_no_delete
BEFORE DELETE ON snapshot_reparse_lineage
BEGIN
    SELECT RAISE(ABORT,'snapshot reparse lineage is append-only');
END;
"""


def _state_meta_readonly(path: Path) -> dict[str, str]:
    if not path.exists() or path.stat().st_size == 0:
        return {}
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        has_meta = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='state_meta'
            """
        ).fetchone()
        if not has_meta:
            raise RecrawlError(
                f"existing DB is not an Architizer recrawl sidecar: {path}"
            )
        return {
            str(key): str(value)
            for key, value in connection.execute(
                "SELECT key,value FROM state_meta"
            )
        }
    finally:
        connection.close()


def _validate_source_binding(
    meta: Mapping[str, str],
    *,
    source_path: Path,
    source_sha256: str,
    source_size: int,
) -> None:
    stored_path = meta.get("source_db_path")
    stored_sha = meta.get("source_db_sha256")
    stored_size = meta.get("source_db_size")
    bound_values = [stored_path, stored_sha, stored_size]
    if any(value is not None for value in bound_values) and not all(
        value is not None for value in bound_values
    ):
        raise RecrawlError("sidecar source binding is incomplete")
    if stored_path is None:
        return
    same_path = Path(stored_path).resolve() == source_path.resolve()
    if os.name == "nt":
        same_path = str(Path(stored_path).resolve()).casefold() == str(
            source_path.resolve()
        ).casefold()
    if (
        not same_path
        or stored_sha != source_sha256
        or int(stored_size) != source_size
    ):
        raise RecrawlError(
            "sidecar is bound to a different immutable source DB: "
            f"path={stored_path}, sha256={stored_sha}, size={stored_size}"
        )


def _same_resolved_path(first: Path | str, second: Path | str) -> bool:
    first_text = str(Path(first).resolve())
    second_text = str(Path(second).resolve())
    if os.name == "nt":
        return first_text.casefold() == second_text.casefold()
    return first_text == second_text


def _validate_unbound_existing_lineage(
    path: Path,
    *,
    source_path: Path,
    source_sha256: str,
    source_size: int,
) -> None:
    """Refuse to bind populated legacy sidecars without matching run lineage."""

    if not path.exists() or path.stat().st_size == 0:
        return
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "runs" not in tables:
            return
        run_rows = connection.execute(
            """
            SELECT DISTINCT source_db_path,source_db_sha256_before,source_db_size
            FROM runs
            """
        ).fetchall()
        if not run_rows:
            populated = 0
            for table in (
                "targets",
                "metadata_versions",
                "sitemap_snapshots",
                "http_attempts",
            ):
                if table in tables:
                    populated += int(
                        connection.execute(
                            f"SELECT COUNT(*) FROM {table}"
                        ).fetchone()[0]
                    )
            if populated:
                raise RecrawlError(
                    "populated unbound sidecar has no source run lineage"
                )
            return
        for row in run_rows:
            stored_path = row["source_db_path"]
            stored_sha = row["source_db_sha256_before"]
            stored_size = row["source_db_size"]
            if (
                not stored_path
                or not _same_resolved_path(stored_path, source_path)
                or stored_sha != source_sha256
                or stored_size is None
                or int(stored_size) != source_size
            ):
                raise RecrawlError(
                    "unbound sidecar run lineage does not match the "
                    "requested immutable source DB"
                )
    finally:
        connection.close()


def connect_state(
    path: Path,
    *,
    source_path: Path | None = None,
    source_sha256: str | None = None,
    source_size: int | None = None,
) -> sqlite3.Connection:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_meta = _state_meta_readonly(path)
    existing_version = existing_meta.get("schema_version")
    if existing_version not in {None, "2.0", "2.1", STATE_SCHEMA_VERSION}:
        raise RecrawlError(
            f"unsupported sidecar schema version: {existing_version}"
        )
    if source_path is not None:
        if source_sha256 is None or source_size is None:
            raise ValueError("complete source identity is required")
        _validate_source_binding(
            existing_meta,
            source_path=source_path,
            source_sha256=source_sha256,
            source_size=source_size,
        )
        if existing_meta.get("source_db_path") is None:
            _validate_unbound_existing_lineage(
                path,
                source_path=source_path,
                source_sha256=source_sha256,
                source_size=source_size,
            )
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    if existing_version != STATE_SCHEMA_VERSION:
        backfill = ""
        if existing_version == "2.0":
            backfill = """
            INSERT OR IGNORE INTO run_metadata_versions(
                run_id,version_id,target_url
            )
            SELECT run_id,id,target_url FROM metadata_versions;
            """
        migration = f"""
        BEGIN IMMEDIATE;
        {STATE_SCHEMA}
        {SNAPSHOT_REPARSE_SCHEMA}
        {backfill}
        INSERT INTO state_meta(key,value)
        VALUES ('schema_version','{STATE_SCHEMA_VERSION}')
        ON CONFLICT(key) DO UPDATE SET value=excluded.value;
        COMMIT;
        """
        try:
            connection.executescript(migration)
        except Exception:
            connection.rollback()
            connection.close()
            raise
    required_reparse_columns = {
        "entity_type",
        "selection_kind",
        "source_run_id",
        "source_metadata_version_id",
        "source_http_attempt_id",
        "request_kind",
        "requested_url",
        "http_outcome",
        "http_status",
        "block_signals_json",
        "attempt_error",
        "content_sha256",
        "final_url",
        "content_type",
        "response_bytes",
        "gzip_path",
        "gzip_sha256",
        "integrity_status",
    }
    for table in ("snapshot_reparse_inputs", "snapshot_reparse_lineage"):
        columns = {
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if not required_reparse_columns.issubset(columns):
            connection.close()
            raise RecrawlError(
                f"incomplete sidecar schema {STATE_SCHEMA_VERSION}: {table}"
            )
    if source_path is not None:
        binding = {
            "source_db_path": str(source_path.resolve()),
            "source_db_sha256": source_sha256,
            "source_db_size": str(source_size),
        }
        for key, value in binding.items():
            connection.execute(
                """
                INSERT INTO state_meta(key,value) VALUES (?,?)
                ON CONFLICT(key) DO NOTHING
                """,
                (key, value),
            )
    connection.commit()
    return connection


def start_run(
    connection: sqlite3.Connection,
    *,
    run_kind: str,
    source_path: Path,
    source_sha256: str,
    source_size: int,
    arguments: Mapping[str, Any],
    commit: bool = True,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO runs(
            run_kind,started_at,status,parser_version,arguments_json,
            source_db_path,source_db_sha256_before,source_db_size
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            run_kind,
            utc_now(),
            "running",
            PARSER_VERSION,
            canonical_json(dict(arguments)),
            str(source_path.resolve()),
            source_sha256,
            source_size,
        ),
    )
    if commit:
        connection.commit()
    return int(cursor.lastrowid)


def finish_run(
    connection: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    source_sha256_after: str,
    summary: Mapping[str, Any] | None = None,
    error: str | None = None,
    selected_count: int | None = None,
    commit: bool = True,
) -> None:
    connection.execute(
        """
        UPDATE runs
        SET finished_at=?,status=?,source_db_sha256_after=?,
            summary_json=?,error=?,
            selected_count=COALESCE(?,selected_count)
        WHERE id=?
        """,
        (
            utc_now(),
            status,
            source_sha256_after,
            canonical_json(dict(summary or {})),
            error,
            selected_count,
            run_id,
        ),
    )
    if commit:
        connection.commit()


def _write_gzip_snapshot(
    snapshot_root: Path,
    *,
    kind: str,
    content: bytes,
    extension: str,
) -> tuple[str, str, int]:
    digest = sha256_bytes(content)
    relative = Path(kind) / digest[:2] / f"{digest}.{extension}.gz"
    destination = snapshot_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    compressed = gzip.compress(content, compresslevel=9, mtime=0)
    if destination.exists() and verify_snapshot(
        snapshot_root, relative.as_posix(), digest
    ):
        return digest, relative.as_posix(), destination.stat().st_size
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(compressed)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary_path, destination)
        except FileExistsError:
            if not verify_snapshot(
                snapshot_root, relative.as_posix(), digest
            ):
                os.replace(temporary_path, destination)
                temporary_path = None
        except OSError as exc:
            if destination.exists() and verify_snapshot(
                snapshot_root, relative.as_posix(), digest
            ):
                pass
            elif destination.exists():
                os.replace(temporary_path, destination)
                temporary_path = None
            else:
                raise RecrawlError(
                    f"atomic snapshot publish failed: {destination}: {exc}"
                ) from exc
        if not verify_snapshot(snapshot_root, relative.as_posix(), digest):
            raise RecrawlError(f"snapshot integrity mismatch: {destination}")
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return digest, relative.as_posix(), destination.stat().st_size


def verify_snapshot(snapshot_root: Path, relative_path: str, expected_sha: str) -> bool:
    path = snapshot_root / Path(relative_path)
    if not path.exists():
        return False
    try:
        content = gzip.decompress(path.read_bytes())
    except (OSError, EOFError):
        return False
    return sha256_bytes(content) == expected_sha


@dataclasses.dataclass
class HttpAttempt:
    requested_url: str
    attempt_number: int
    started_at: str
    finished_at: str
    duration_ms: int
    outcome: str
    http_status: int | None
    final_url: str | None
    content_type: str | None
    body: bytes
    retryable: bool
    block_signals: list[str]
    error: str | None


@dataclasses.dataclass
class HttpResult:
    attempts: list[HttpAttempt]

    @property
    def final(self) -> HttpAttempt:
        if not self.attempts:
            raise RecrawlError("HTTP result has no attempts")
        return self.attempts[-1]


class _RestrictedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: Iterable[str]):
        super().__init__()
        self.allowed_hosts = {
            str(host).lower().rstrip(".") for host in allowed_hosts
        }

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        host = (urllib.parse.urlsplit(newurl).hostname or "").lower().rstrip(".")
        if host not in self.allowed_hosts:
            raise urllib.error.URLError(
                "redirect target host is outside allowlist: "
                f"{host or '<missing>'}"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def detect_block_signals(
    *,
    final_url: str | None,
    status: int | None,
    content_type: str | None,
    body: bytes,
) -> list[str]:
    signals: list[str] = []
    path = urllib.parse.urlsplit(final_url or "").path.lower()
    sample = body[:500_000].decode("utf-8", errors="ignore").lower()
    if status in {401, 403, 407, 429}:
        signals.append(f"http_{status}")
    if "/login" in path or "/sign-in" in path or "/signin" in path:
        signals.append("login_redirect")
    block_markers = (
        ("cf-chl-", "cloudflare_challenge"),
        ("attention required! | cloudflare", "cloudflare_block"),
        ("captcha", "captcha"),
        ("access denied", "access_denied"),
        ("unusual traffic", "unusual_traffic"),
        ("temporarily blocked", "temporarily_blocked"),
    )
    for marker, label in block_markers:
        if marker in sample:
            signals.append(label)
    login_title = re.search(r"<title[^>]*>\s*(log\s*in|sign\s*in)\b", sample)
    login_form = re.search(
        r"<form[^>]+(?:action|id)=[\"'][^\"']*(?:login|signin|sign-in)",
        sample,
    )
    if login_title and login_form:
        signals.append("login_wall")
    if (
        status == 200
        and content_type
        and not any(
            marker in content_type.lower()
            for marker in ("html", "xml", "json", "text/plain")
        )
    ):
        signals.append("non_html_200")
    return sorted(set(signals))


class PoliteHttpClient:
    """Small stdlib HTTP client with per-attempt pacing and circuit breaker."""

    RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}

    def __init__(
        self,
        *,
        delay_seconds: float = 2.0,
        timeout_seconds: float = 30.0,
        max_attempts: int = 3,
        max_response_bytes: int = 10 * 1024 * 1024,
        user_agent: str = DEFAULT_USER_AGENT,
        circuit_breaker_threshold: int = 3,
        jitter_seed: str = "architizer-recrawl-v2",
        allowed_hosts: Iterable[str] | None = None,
    ):
        if delay_seconds < 0:
            raise ValueError("delay_seconds must be non-negative")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.delay_seconds = delay_seconds
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.max_response_bytes = max_response_bytes
        self.user_agent = user_agent
        self.circuit_breaker_threshold = circuit_breaker_threshold
        self.allowed_hosts = (
            {
                str(host).lower().rstrip(".")
                for host in allowed_hosts
            }
            if allowed_hosts is not None
            else None
        )
        self._opener = urllib.request.build_opener(
            *(
                [_RestrictedRedirectHandler(self.allowed_hosts)]
                if self.allowed_hosts is not None
                else []
            )
        )
        self._last_request_monotonic = 0.0
        self._consecutive_blocked = 0
        self._random = random.Random(jitter_seed)

    def _pace(self) -> None:
        elapsed = time.monotonic() - self._last_request_monotonic
        remaining = self.delay_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_request_monotonic = time.monotonic()

    def _retry_after(
        self, headers: Mapping[str, str] | Any, attempt_number: int
    ) -> float:
        raw = headers.get("Retry-After") if headers else None
        if raw:
            with contextlib.suppress(ValueError):
                return min(60.0, max(0.0, float(raw)))
            with contextlib.suppress(TypeError, ValueError, OverflowError):
                parsed = email.utils.parsedate_to_datetime(raw)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return min(
                    60.0,
                    max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds()),
                )
        base = min(30.0, float(2 ** attempt_number))
        return base + self._random.uniform(0.0, min(1.0, base / 4.0))

    def fetch(self, url: str) -> HttpResult:
        request_host = (
            urllib.parse.urlsplit(url).hostname or ""
        ).lower().rstrip(".")
        if self.allowed_hosts is not None and request_host not in self.allowed_hosts:
            raise RecrawlError(
                f"request host is outside HTTP client allowlist: {request_host}"
            )
        if self._consecutive_blocked >= self.circuit_breaker_threshold:
            raise CircuitOpenError(
                "circuit breaker open after "
                f"{self._consecutive_blocked} consecutive block responses"
            )
        attempts: list[HttpAttempt] = []
        for attempt_number in range(1, self.max_attempts + 1):
            self._pace()
            started_at = utc_now()
            start = time.monotonic()
            status: int | None = None
            final_url: str | None = None
            content_type: str | None = None
            body = b""
            error: str | None = None
            headers: Any = {}
            try:
                request = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": self.user_agent,
                        "Accept": (
                            "text/html,application/xhtml+xml,application/xml;"
                            "q=0.9,*/*;q=0.8"
                        ),
                        "Accept-Language": "en-US,en;q=0.7",
                    },
                )
                with self._opener.open(
                    request,
                    timeout=self.timeout_seconds,
                ) as response:
                    status = int(response.status)
                    final_url = response.geturl()
                    headers = response.headers
                    content_type = response.headers.get("Content-Type")
                    body = response.read(self.max_response_bytes + 1)
                    if len(body) > self.max_response_bytes:
                        error = (
                            "response exceeds max_response_bytes="
                            f"{self.max_response_bytes}"
                        )
                        body = body[: self.max_response_bytes]
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
                final_url = exc.geturl()
                headers = exc.headers
                content_type = exc.headers.get("Content-Type")
                with contextlib.suppress(OSError):
                    body = exc.read(self.max_response_bytes)
                error = f"HTTP {status}: {exc.reason}"
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                error = f"{type(exc).__name__}: {exc}"

            signals = detect_block_signals(
                final_url=final_url,
                status=status,
                content_type=content_type,
                body=body,
            )
            retryable = status in self.RETRY_STATUSES or status is None
            success = (
                error is None
                and status is not None
                and 200 <= status < 300
                and "non_html_200" not in signals
            )
            outcome = "success" if success else "error"
            if signals:
                outcome = "blocked" if any(
                    signal
                    in {
                        "http_403",
                        "http_429",
                        "cloudflare_challenge",
                        "cloudflare_block",
                        "captcha",
                        "access_denied",
                        "unusual_traffic",
                        "temporarily_blocked",
                        "login_redirect",
                        "login_wall",
                    }
                    for signal in signals
                ) else outcome
            attempts.append(
                HttpAttempt(
                    requested_url=url,
                    attempt_number=attempt_number,
                    started_at=started_at,
                    finished_at=utc_now(),
                    duration_ms=max(0, round((time.monotonic() - start) * 1000)),
                    outcome=outcome,
                    http_status=status,
                    final_url=final_url,
                    content_type=content_type,
                    body=body,
                    retryable=retryable,
                    block_signals=signals,
                    error=error,
                )
            )
            is_blocked = outcome == "blocked"
            self._consecutive_blocked = (
                self._consecutive_blocked + 1 if is_blocked else 0
            )
            if success or not retryable or attempt_number >= self.max_attempts:
                break
            wait_seconds = self._retry_after(headers, attempt_number)
            if wait_seconds > 0:
                time.sleep(wait_seconds)
        return HttpResult(attempts)


def parse_sitemap_index(content: bytes) -> list[dict[str, str | None]]:
    if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
        raise RecrawlError("DTD/entity declarations are not accepted")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise RecrawlError(f"invalid sitemap index XML: {exc}") from exc
    if root.tag.rsplit("}", 1)[-1] != "sitemapindex":
        raise RecrawlError("official sitemap root is not sitemapindex")
    output: list[dict[str, str | None]] = []
    for node in root:
        if node.tag.rsplit("}", 1)[-1] != "sitemap":
            continue
        fields = {
            child.tag.rsplit("}", 1)[-1]: (child.text or "").strip()
            for child in node
        }
        if fields.get("loc"):
            output.append(
                {"loc": fields["loc"], "lastmod": fields.get("lastmod") or None}
            )
    return output


def parse_sitemap_urls(content: bytes) -> list[dict[str, str | None]]:
    if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
        raise RecrawlError("DTD/entity declarations are not accepted")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise RecrawlError(f"invalid sitemap urlset XML: {exc}") from exc
    if root.tag.rsplit("}", 1)[-1] != "urlset":
        raise RecrawlError("leaf sitemap root is not urlset")
    output: list[dict[str, str | None]] = []
    for node in root:
        if node.tag.rsplit("}", 1)[-1] != "url":
            continue
        fields = {
            child.tag.rsplit("}", 1)[-1]: (child.text or "").strip()
            for child in node
        }
        if fields.get("loc"):
            output.append(
                {"loc": fields["loc"], "lastmod": fields.get("lastmod") or None}
            )
    return output


def official_entity_sitemaps(
    index_entries: Sequence[Mapping[str, str | None]],
) -> dict[str, list[str]]:
    output: dict[str, list[str]] = {"project": [], "firm": []}
    patterns = {
        "project": re.compile(r"^/sitemap-projects\.xml$"),
        "firm": re.compile(r"^/sitemap-firms\.xml$"),
    }
    for entry in index_entries:
        raw = entry.get("loc")
        if not raw:
            continue
        parsed = urllib.parse.urlsplit(raw)
        host = (parsed.hostname or "").lower()
        if host not in {ARCHITIZER_HOST, f"www.{ARCHITIZER_HOST}"}:
            continue
        for entity_type, pattern in patterns.items():
            if pattern.fullmatch(parsed.path):
                normalized = validate_official_sitemap_url(raw)
                if normalized not in output[entity_type]:
                    output[entity_type].append(normalized)
    return output


def validate_sitemap_entity_url(url: str, entity_type: str) -> str:
    normalized = normalize_entity_url(url, entity_type)
    parsed = urllib.parse.urlsplit(normalized)
    expected_prefix = "/projects/" if entity_type == "project" else "/firms/"
    if parsed.hostname != ARCHITIZER_HOST or not parsed.path.startswith(expected_prefix):
        raise RecrawlError(
            f"unexpected {entity_type} URL in official sitemap: {url}"
        )
    if not slug_from_url(normalized, entity_type):
        raise RecrawlError(f"missing slug in sitemap URL: {url}")
    return normalized


class _ArchitizerHTMLParser(HTMLParser):
    """Loss-minimizing HTML scanner; it intentionally does not infer layout."""

    CAPTURE_TAGS = {"title", "h1", "h2"}
    VOID_TAGS = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, list[str]] = defaultdict(list)
        self.canonical_urls: list[str] = []
        self.data_data_blobs: list[str] = []
        self.data_data_records: list[dict[str, str | None]] = []
        self.scripts: list[dict[str, Any]] = []
        self.global_ids: list[str] = []
        self.links: list[dict[str, str]] = []
        self.captured: dict[str, list[str]] = defaultdict(list)
        self.firm_location_dom: dict[str, list[str]] = defaultdict(list)
        self._capture_stack: list[tuple[str, list[str]]] = []
        self._script_attrs: dict[str, str] | None = None
        self._script_buffer: list[str] = []
        self._link_href: str | None = None
        self._link_buffer: list[str] = []
        self._all_text: list[str] = []
        self._element_stack: list[dict[str, Any]] = []

    @property
    def visible_text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self._all_text)).strip()

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        tag = tag.lower()
        attributes = {key.lower(): value or "" for key, value in attrs}
        parent = self._element_stack[-1] if self._element_stack else None
        firm_location_slug = (
            parent.get("firm_location_slug") if parent is not None else None
        )
        element_id = attributes.get("id", "").strip()
        location_match = (
            re.fullmatch(r"(.+)-locations", element_id)
            if tag == "div" and element_id
            else None
        )
        if location_match:
            firm_location_slug = location_match.group(1)
        classes = set(attributes.get("class", "").split())
        is_location_marker = bool(
            firm_location_slug
            and tag == "span"
            and {"icon", "marker"}.issubset(classes)
        )
        captures_location = bool(
            firm_location_slug
            and tag == "span"
            and {
                "placeholder",
                "single-line",
                "js-rendered-content",
            }.issubset(classes)
            and parent is not None
            and parent.get("last_child_was_location_marker")
        )
        if parent is not None:
            parent["last_child_was_location_marker"] = False
        frame = {
            "tag": tag,
            "firm_location_slug": firm_location_slug,
            "is_location_marker": is_location_marker,
            "captures_location": captures_location,
            "location_buffer": [],
            "last_child_was_location_marker": False,
        }
        if tag not in self.VOID_TAGS:
            self._element_stack.append(frame)
        if tag == "meta":
            key = (
                attributes.get("property")
                or attributes.get("name")
                or attributes.get("itemprop")
                or ""
            ).strip().lower()
            content = attributes.get("content", "").strip()
            if key and content:
                self.meta[key].append(content)
        elif tag == "link":
            rel = attributes.get("rel", "").lower().split()
            href = attributes.get("href", "").strip()
            if "canonical" in rel and href:
                self.canonical_urls.append(href)
        if attributes.get("data-data"):
            self.data_data_blobs.append(attributes["data-data"])
            self.data_data_records.append(
                {
                    "raw": attributes["data-data"],
                    "firm_location_slug": firm_location_slug,
                }
            )
        global_id = attributes.get("data-globalid", "").strip()
        if global_id:
            self.global_ids.append(global_id)
        if tag in self.CAPTURE_TAGS:
            self._capture_stack.append((tag, []))
        if tag == "script":
            self._script_attrs = attributes
            self._script_buffer = []
        if tag == "a":
            self._link_href = attributes.get("href", "").strip() or None
            self._link_buffer = []

    def handle_data(self, data: str) -> None:
        if self._script_attrs is not None:
            self._script_buffer.append(data)
            return
        for frame in self._element_stack:
            if frame["captures_location"]:
                frame["location_buffer"].append(data)
        if data.strip() and self._element_stack:
            self._element_stack[-1]["last_child_was_location_marker"] = False
        value = data.strip()
        if value:
            self._all_text.append(value)
            for _, buffer in self._capture_stack:
                buffer.append(value)
            if self._link_href is not None:
                self._link_buffer.append(value)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "script" and self._script_attrs is not None:
            self.scripts.append(
                {
                    "attrs": self._script_attrs,
                    "raw": "".join(self._script_buffer).strip(),
                }
            )
            self._script_attrs = None
            self._script_buffer = []
        if tag == "a" and self._link_href is not None:
            self.links.append(
                {
                    "href": self._link_href,
                    "text": re.sub(
                        r"\s+", " ", " ".join(self._link_buffer)
                    ).strip(),
                }
            )
            self._link_href = None
            self._link_buffer = []
        for index in range(len(self._capture_stack) - 1, -1, -1):
            capture_tag, buffer = self._capture_stack[index]
            if capture_tag == tag:
                del self._capture_stack[index]
                value = re.sub(r"\s+", " ", " ".join(buffer)).strip()
                if value:
                    self.captured[tag].append(value)
                break
        for index in range(len(self._element_stack) - 1, -1, -1):
            frame = self._element_stack[index]
            if frame["tag"] != tag:
                continue
            del self._element_stack[index:]
            parent = self._element_stack[-1] if self._element_stack else None
            if frame["captures_location"]:
                location = re.sub(
                    r"\s+", " ", "".join(frame["location_buffer"])
                ).strip()
                slug = frame["firm_location_slug"]
                if slug and location:
                    self.firm_location_dom[slug].append(location)
            if parent is not None:
                parent["last_child_was_location_marker"] = bool(
                    frame["is_location_marker"]
                )
            break


def _parse_json_blob(raw: str, source: str) -> dict[str, Any]:
    observed = raw.strip()
    record: dict[str, Any] = {
        "source": source,
        "raw": observed,
        "parse_status": "empty" if not observed else "unparsed",
        "parse_variant": None,
        "value": None,
    }
    if not observed:
        return record
    variants = [("raw", observed)]
    fallback = html.unescape(observed)
    if fallback != observed:
        variants.append(("html_unescape_fallback", fallback))
    for variant_name, variant in variants:
        candidates = [variant]
        assignment = re.search(
            r"(?:window\.)?(?:__INITIAL_STATE__|__NEXT_DATA__)\s*=\s*(\{.*\})\s*;?\s*$",
            variant,
            flags=re.DOTALL,
        )
        if assignment:
            candidates.insert(0, assignment.group(1))
        for candidate in candidates:
            try:
                value = json.loads(candidate)
                if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
                    value = json.loads(value)
                record["value"] = value
                record["parse_status"] = "parsed"
                record["parse_variant"] = variant_name
                return record
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
    record["parse_status"] = "malformed"
    return record


def _walk_json(value: Any, max_nodes: int = 100_000) -> Iterator[Any]:
    stack = [value]
    visited = 0
    while stack and visited < max_nodes:
        current = stack.pop()
        visited += 1
        yield current
        if isinstance(current, dict):
            stack.extend(reversed(list(current.values())))
        elif isinstance(current, list):
            stack.extend(reversed(current))


def _project_candidate_score(candidate: Mapping[str, Any]) -> int:
    score = 0
    global_id = _safe_text(
        candidate.get("global_id") or candidate.get("globalId")
    )
    if global_id and global_id.startswith("projects.project."):
        score += 10
    if candidate.get("pk") is not None or candidate.get("id") is not None:
        score += 3
    if _safe_text(candidate.get("name") or candidate.get("headline")):
        score += 2
    if any(
        key in candidate
        for key in (
            "completion_date",
            "building_size",
            "constr_status",
            "absolute_url",
        )
    ):
        score += 3
    type_value = _safe_text(candidate.get("@type"))
    if type_value and type_value.lower() in {
        "creativework",
        "project",
        "article",
    }:
        score += 1
    return score


def _firm_candidate_score(candidate: Mapping[str, Any]) -> int:
    score = 0
    global_id = _safe_text(
        candidate.get("global_id") or candidate.get("globalId")
    )
    if global_id and global_id.startswith("firms.firm."):
        score += 10
    if _safe_text(candidate.get("name")):
        score += 2
    type_value = _safe_text(candidate.get("@type"))
    if type_value and type_value.lower() in {
        "organization",
        "architecturalbusiness",
    }:
        score += 3
    return score


def _best_json_candidate(
    embedded_records: Sequence[Mapping[str, Any]],
    entity_type: str,
    expected_slug: str | None = None,
) -> dict[str, Any]:
    scored: list[tuple[int, dict[str, Any]]] = []
    scorer = (
        _project_candidate_score
        if entity_type == "project"
        else _firm_candidate_score
    )
    for record in embedded_records:
        if record.get("parse_status") != "parsed":
            continue
        for value in _walk_json(record.get("value")):
            if not isinstance(value, dict):
                continue
            score = scorer(value)
            explicit_slug = _safe_text(value.get("slug"))
            if not explicit_slug:
                candidate_url = _url_from_maybe_object(
                    _first(value, "absolute_url", "url", "canonical_url", "@id")
                )
                if candidate_url:
                    explicit_slug = slug_from_url(candidate_url, entity_type)
            if expected_slug and explicit_slug:
                score += 20 if explicit_slug == expected_slug else -20
            if score:
                scored.append((score, value))
    if not scored:
        return {}
    scored.sort(
        key=lambda item: (
            item[0],
            len(canonical_json(item[1])),
        ),
        reverse=True,
    )
    return scored[0][1]


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _url_from_maybe_object(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        return _safe_text(
            _first(value, "url", "absolute_url", "@id", "href", "contentUrl")
        )
    return None


def _person_or_firm_name(value: Any) -> str | None:
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, dict):
        return _safe_text(_first(value, "name", "title", "display_name"))
    if isinstance(value, list):
        names = [_person_or_firm_name(item) for item in value]
        names = [name for name in names if name]
        return "; ".join(names) if names else None
    return None


def _location_value(value: Any) -> str | None:
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, list):
        values = [_location_value(item) for item in value]
        values = [item for item in values if item]
        return "; ".join(values) if values else None
    if isinstance(value, dict):
        address = value.get("address")
        if address is not None and address is not value:
            nested = _location_value(address)
            if nested:
                return nested
        pieces = [
            _safe_text(
                _first(
                    value,
                    "streetAddress",
                    "addressLocality",
                    "city",
                    "region",
                    "addressRegion",
                )
            ),
            _safe_text(
                _first(value, "addressCountry", "country", "country_name")
            ),
        ]
        pieces = [piece for piece in pieces if piece]
        return ", ".join(_dedupe(pieces)) if pieces else None
    return None


def _extract_urls(value: Any, *, image_only: bool = False) -> list[str]:
    urls: list[str] = []
    for item in _walk_json(value, max_nodes=50_000):
        candidate: str | None = None
        if isinstance(item, str) and item.startswith(("http://", "https://")):
            candidate = item
        elif isinstance(item, dict):
            candidate = _url_from_maybe_object(item)
        if not candidate:
            continue
        parsed = urllib.parse.urlsplit(candidate)
        if not parsed.hostname:
            continue
        if image_only:
            lower_path = parsed.path.lower()
            host = parsed.hostname.lower()
            if not (
                lower_path.endswith(
                    (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")
                )
                or "imgix" in host
                or "/media/" in lower_path
                or "/images/" in lower_path
            ):
                continue
        urls.append(candidate)
    return _dedupe(urls)


def _clean_project_title(value: str | None) -> str | None:
    text = _safe_text(value)
    if not text:
        return None
    text = re.sub(r"\s*\|\s*Architizer\s*$", "", text, flags=re.I)
    if " by " in text:
        text = text.split(" by ", 1)[0].strip()
    return text or None


def _firm_from_title(value: str | None) -> str | None:
    text = _safe_text(value)
    if not text or " by " not in text:
        return None
    return re.sub(
        r"\s*\|\s*Architizer\s*$",
        "",
        text.split(" by ", 1)[1],
        flags=re.I,
    ).strip() or None


def _normalize_field_value(field: str, value: Any) -> Any:
    if value is None:
        return None
    if field in {
        "categories",
        "gallery_image_urls",
        "image_global_ids",
        "office_locations",
        "project_urls",
    }:
        normalized = [_safe_text(item) for item in _as_list(value)]
        return _dedupe(item for item in normalized if item)
    if field == "social_links" and isinstance(value, dict):
        return {
            str(key): _safe_text(item)
            for key, item in sorted(value.items())
            if _safe_text(item)
        }
    if field in {"project_id", "completion_year"}:
        return _safe_int(value)
    return _safe_text(value)


def _comparison_key(value: Any) -> str:
    if isinstance(value, list):
        return canonical_json(sorted(canonical_json(item) for item in value))
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip().casefold()
    return canonical_json(value)


def _resolve_field(
    field: str,
    embedded_value: Any,
    dom_value: Any,
    *,
    embedded_quality: str = "high",
    dom_quality: str = "medium",
) -> dict[str, Any]:
    embedded = _normalize_field_value(field, embedded_value)
    dom = _normalize_field_value(field, dom_value)
    if embedded in (None, "", [], {}):
        embedded = None
    if dom in (None, "", [], {}):
        dom = None
    if embedded is None and dom is None:
        return {
            "value": None,
            "status": "missing",
            "quality": "none",
            "conflict": None,
        }
    if embedded is not None and dom is not None:
        if _comparison_key(embedded) == _comparison_key(dom):
            return {
                "value": embedded,
                "status": "confirmed",
                "quality": "high",
                "conflict": None,
            }
        return {
            "value": None,
            "status": "conflict",
            "quality": "review",
            "conflict": {"embedded_json": embedded, "dom": dom},
        }
    if embedded is not None:
        return {
            "value": embedded,
            "status": "single_source",
            "quality": embedded_quality,
            "conflict": None,
        }
    return {
        "value": dom,
        "status": "single_source",
        "quality": dom_quality,
        "conflict": None,
    }


def _project_embedded_fields(candidate: Mapping[str, Any]) -> dict[str, Any]:
    global_id = _safe_text(_first(candidate, "global_id", "globalId"))
    project_id = _safe_int(_first(candidate, "pk", "id"))
    absolute_url = _url_from_maybe_object(
        _first(candidate, "absolute_url", "url", "canonical_url")
    )
    slug = _safe_text(candidate.get("slug"))
    if not slug and absolute_url:
        slug = slug_from_url(absolute_url, "project")
    firm = _first(
        candidate,
        "firm",
        "firms",
        "architect",
        "architects",
        "author",
    )
    firm_url = _url_from_maybe_object(firm)
    firm_slug = (
        slug_from_url(firm_url, "firm")
        if firm_url
        else _safe_text(
            _first(
                candidate,
                "firm_slug",
                "architect_slug",
            )
        )
    )
    categories = _first(candidate, "categories", "category", "tags", "keywords")
    normalized_categories: list[str] = []
    for item in _as_list(categories):
        if isinstance(item, dict):
            item = _first(item, "name", "title", "slug")
        text = _safe_text(item)
        if text:
            normalized_categories.append(text)
    cover = _first(
        candidate,
        "cover_image_url",
        "cover_image",
        "primary_image",
        "thumbnail",
    )
    cover_url = _url_from_maybe_object(cover)
    image_value = _first(candidate, "gallery", "images", "media", "image")
    gallery_urls = _extract_urls(image_value, image_only=True)
    if cover_url and cover_url not in gallery_urls:
        gallery_urls.insert(0, cover_url)
    location = _location_value(
        _first(
            candidate,
            "location",
            "address",
            "location_full",
            "project_location",
        )
    )
    return {
        "project_id": project_id,
        "global_id": global_id,
        "slug": slug,
        "name": _safe_text(_first(candidate, "name", "headline", "title")),
        "firm_slug": firm_slug,
        "firm_name": _person_or_firm_name(firm)
        or _safe_text(_first(candidate, "firm_name", "architect_name")),
        "location": location,
        "completion_year": _year(
            _first(
                candidate,
                "completion_date",
                "completion_year",
                "dateCompleted",
                "year",
            )
        ),
        "construction_status": _safe_text(
            _first(
                candidate,
                "constr_status",
                "construction_status",
                "status",
            )
        ),
        "size_bucket": _safe_text(
            _first(candidate, "building_size", "size", "building_size_slug")
        ),
        "description": _safe_text(
            _first(candidate, "description", "articleBody", "text")
        ),
        "description_short": _safe_text(
            _first(candidate, "description_short", "abstract")
        ),
        "categories": _dedupe(normalized_categories),
        "cover_image_url": cover_url,
        "gallery_image_urls": gallery_urls,
        "image_global_ids": _dedupe(
            _safe_text(item)
            for item in _as_list(
                _first(candidate, "image_global_ids", "media_global_ids")
            )
            if _safe_text(item)
        ),
        "published_time": _safe_text(
            _first(candidate, "published_time", "datePublished", "created")
        ),
        "modified_time": _safe_text(
            _first(candidate, "modified_time", "dateModified", "updated")
        ),
        "_absolute_url": absolute_url,
    }


def _project_embedded_raw_fields(
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    firm = _first(
        candidate,
        "firm",
        "firms",
        "architect",
        "architects",
        "author",
    )
    image_value = _first(candidate, "gallery", "images", "media", "image")
    return {
        "project_id": _first(candidate, "pk", "id"),
        "global_id": _first(candidate, "global_id", "globalId"),
        "slug": _first(
            candidate, "slug", "absolute_url", "url", "canonical_url"
        ),
        "name": _first(candidate, "name", "headline", "title"),
        "firm_slug": firm
        if firm is not None
        else _first(candidate, "firm_slug", "architect_slug"),
        "firm_name": firm
        if firm is not None
        else _first(candidate, "firm_name", "architect_name"),
        "location": _first(
            candidate,
            "location",
            "address",
            "location_full",
            "project_location",
        ),
        "completion_year": _first(
            candidate,
            "completion_date",
            "completion_year",
            "dateCompleted",
            "year",
        ),
        "construction_status": _first(
            candidate,
            "constr_status",
            "construction_status",
            "status",
        ),
        "size_bucket": _first(
            candidate, "building_size", "size", "building_size_slug"
        ),
        "description": _first(
            candidate, "description", "articleBody", "text"
        ),
        "description_short": _first(
            candidate, "description_short", "abstract"
        ),
        "categories": _first(
            candidate, "categories", "category", "tags", "keywords"
        ),
        "cover_image_url": _first(
            candidate,
            "cover_image_url",
            "cover_image",
            "primary_image",
            "thumbnail",
        ),
        "gallery_image_urls": image_value,
        "image_global_ids": _first(
            candidate, "image_global_ids", "media_global_ids"
        ),
        "published_time": _first(
            candidate, "published_time", "datePublished", "created"
        ),
        "modified_time": _first(
            candidate, "modified_time", "dateModified", "updated"
        ),
    }


def _project_dom_fields(scanner: _ArchitizerHTMLParser) -> dict[str, Any]:
    title = (scanner.captured.get("title") or [None])[0]
    h1 = (scanner.captured.get("h1") or [None])[0]
    h2_values = scanner.captured.get("h2") or []
    location = next(
        (
            value
            for value in h2_values
            if "," in value and 3 <= len(value) <= 200
        ),
        None,
    )
    author_url = (scanner.meta.get("article:author") or [None])[0]
    image_urls = _dedupe(scanner.meta.get("og:image") or [])
    categories = _dedupe(scanner.meta.get("article:tag") or [])
    canonical_url = scanner.canonical_urls[0] if scanner.canonical_urls else None
    return {
        "project_id": None,
        "global_id": None,
        "slug": slug_from_url(canonical_url, "project")
        if canonical_url
        else None,
        "name": _clean_project_title(h1 or title),
        "firm_slug": slug_from_url(author_url, "firm") if author_url else None,
        "firm_name": _firm_from_title(title),
        "location": location,
        "completion_year": None,
        "construction_status": None,
        "size_bucket": None,
        "description": None,
        "description_short": (
            scanner.meta.get("og:description") or [None]
        )[0],
        "categories": categories,
        "cover_image_url": image_urls[0] if image_urls else None,
        "gallery_image_urls": image_urls,
        "image_global_ids": _dedupe(
            value
            for value in scanner.global_ids
            if value.startswith("media.mediaitemattribution")
        ),
        "published_time": (
            scanner.meta.get("article:published_time") or [None]
        )[0],
        "modified_time": (
            scanner.meta.get("article:modified_time") or [None]
        )[0],
        "_canonical_url": canonical_url,
        "_title": title,
    }


def _firm_location_values_from_embedded(
    embedded_records: Sequence[Mapping[str, Any]],
    *,
    expected_slug: str | None,
) -> list[str]:
    """Read only explicit firm-location records scoped to its DOM section."""

    if not expected_slug:
        return []
    locations: list[str] = []
    for record in embedded_records:
        context = record.get("context")
        if not isinstance(context, Mapping):
            continue
        if context.get("firm_location_slug") != expected_slug:
            continue
        if record.get("parse_status") != "parsed":
            continue
        value = record.get("value")
        if not isinstance(value, Mapping):
            continue
        global_id = _safe_text(value.get("global_id"))
        if not global_id or re.fullmatch(
            r"locations\.geolocation\.\d+", global_id
        ) is None:
            continue
        raw_location = value.get("for_humans")
        if not isinstance(raw_location, str):
            continue
        location = _safe_text(raw_location)
        if location:
            locations.append(location)
    return _dedupe(locations)


def _firm_embedded_fields(
    candidate: Mapping[str, Any],
    *,
    office_locations: Sequence[str] = (),
) -> dict[str, Any]:
    absolute_url = _url_from_maybe_object(
        _first(candidate, "absolute_url", "url", "canonical_url")
    )
    slug = _safe_text(candidate.get("slug"))
    if not slug and absolute_url:
        slug = slug_from_url(absolute_url, "firm")
    project_urls = [
        url
        for url in _extract_urls(
            _first(candidate, "projects", "portfolio"), image_only=False
        )
        if slug_from_url(url, "project")
    ]
    return {
        "slug": slug,
        "name": _safe_text(_first(candidate, "name", "headline", "title")),
        "description": _safe_text(
            _first(candidate, "description", "about", "text")
        ),
        "office_locations": _dedupe(office_locations),
        "project_urls": _dedupe(
            normalize_entity_url(url, "project") for url in project_urls
        ),
        "social_links": {},
        "_global_id": _safe_text(_first(candidate, "global_id", "globalId")),
        "_absolute_url": absolute_url,
    }


def _firm_embedded_raw_fields(
    candidate: Mapping[str, Any],
    *,
    office_locations: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "slug": _first(
            candidate, "slug", "absolute_url", "url", "canonical_url"
        ),
        "name": _first(candidate, "name", "headline", "title"),
        "description": _first(candidate, "description", "about", "text"),
        "office_locations": list(office_locations),
        "project_urls": _first(candidate, "projects", "portfolio"),
        "social_links": _first(candidate, "social_links", "social"),
    }


def _firm_dom_fields(
    scanner: _ArchitizerHTMLParser,
    *,
    expected_slug: str | None,
) -> dict[str, Any]:
    title = (scanner.captured.get("title") or [None])[0]
    h1 = (scanner.captured.get("h1") or [None])[0]
    canonical_url = scanner.canonical_urls[0] if scanner.canonical_urls else None
    project_urls: list[str] = []
    social: dict[str, str] = {}
    for link in scanner.links:
        href = urllib.parse.urljoin(
            f"https://{ARCHITIZER_HOST}/",
            link["href"],
        )
        if slug_from_url(href, "project"):
            project_urls.append(normalize_entity_url(href, "project"))
        host = (urllib.parse.urlsplit(href).hostname or "").lower()
        for network in (
            "instagram",
            "facebook",
            "linkedin",
            "youtube",
            "vimeo",
            "twitter",
            "x",
        ):
            if host == f"{network}.com" or host.endswith(f".{network}.com"):
                social.setdefault(network, href)
    name = _safe_text(h1 or title)
    if name:
        name = re.sub(r"\s*\|\s*Architizer\s*$", "", name, flags=re.I)
    return {
        "slug": slug_from_url(canonical_url, "firm")
        if canonical_url
        else None,
        "name": name,
        "description": (scanner.meta.get("og:description") or [None])[0],
        "office_locations": _dedupe(
            scanner.firm_location_dom.get(expected_slug or "", [])
        ),
        "project_urls": _dedupe(project_urls),
        "social_links": social,
        "_canonical_url": canonical_url,
        "_title": title,
    }


def _page_classification(
    scanner: _ArchitizerHTMLParser,
    *,
    final_url: str,
    status: int,
    content_type: str | None,
    body: bytes,
) -> dict[str, Any]:
    signals = detect_block_signals(
        final_url=final_url,
        status=status,
        content_type=content_type,
        body=body,
    )
    title = _safe_text((scanner.captured.get("title") or [None])[0])
    if title and re.search(
        r"\b(404|not found|server error|something went wrong)\b",
        title,
        flags=re.I,
    ):
        signals.append("error_page_title")
    if len(body) < 500:
        signals.append("too_short")
    if status != 200:
        signals.append(f"unexpected_status_{status}")
    classification = "normal"
    if any("login" in signal for signal in signals):
        classification = "login"
    elif any(
        signal
        in {
            "http_403",
            "http_429",
            "cloudflare_challenge",
            "cloudflare_block",
            "captcha",
            "access_denied",
            "unusual_traffic",
            "temporarily_blocked",
        }
        for signal in signals
    ):
        classification = "block"
    elif signals:
        classification = "error"
    return {
        "classification": classification,
        "signals": sorted(set(signals)),
        "title": title,
        "visible_text_length": len(scanner.visible_text),
    }


def parse_entity_page(
    body: bytes,
    *,
    requested_url: str,
    final_url: str,
    http_status: int,
    content_type: str | None,
    entity_type: str,
) -> dict[str, Any]:
    """Parse a project/firm while preserving embedded and DOM observations."""

    if entity_type not in {"project", "firm"}:
        raise ValueError(f"unsupported entity_type: {entity_type}")
    charset = "utf-8"
    if content_type:
        match = re.search(r"charset=([^;\s]+)", content_type, flags=re.I)
        if match:
            charset = match.group(1).strip("\"'")
    try:
        page = body.decode(charset, errors="replace")
    except LookupError:
        page = body.decode("utf-8", errors="replace")
    expected_slug = slug_from_url(requested_url, entity_type)
    scanner = _ArchitizerHTMLParser()
    scanner.feed(page)
    scanner.close()
    embedded_records: list[dict[str, Any]] = []
    for index, data_record in enumerate(scanner.data_data_records):
        record = _parse_json_blob(
            data_record["raw"],
            f"data-data[{index}]",
        )
        location_slug = data_record.get("firm_location_slug")
        record["context"] = (
            {"firm_location_slug": location_slug} if location_slug else {}
        )
        embedded_records.append(record)
    for index, script in enumerate(scanner.scripts):
        attributes = script["attrs"]
        script_type = attributes.get("type", "").lower()
        script_id = attributes.get("id", "").lower()
        raw = script["raw"]
        relevant = (
            "json" in script_type
            or script_id in {"__next_data__", "__initial_state__"}
            or "__INITIAL_STATE__" in raw[:200]
            or "__NEXT_DATA__" in raw[:200]
        )
        if relevant and raw:
            embedded_records.append(
                _parse_json_blob(raw, f"script[{index}]#{script_id or script_type}")
            )
    candidate = _best_json_candidate(
        embedded_records,
        entity_type,
        expected_slug=expected_slug,
    )
    if entity_type == "project":
        embedded_fields = _project_embedded_fields(candidate)
        embedded_raw_fields = _project_embedded_raw_fields(candidate)
        dom_fields = _project_dom_fields(scanner)
        fields = PROJECT_FIELDS
    else:
        office_locations = _firm_location_values_from_embedded(
            embedded_records,
            expected_slug=expected_slug,
        )
        embedded_fields = _firm_embedded_fields(
            candidate,
            office_locations=office_locations,
        )
        embedded_raw_fields = _firm_embedded_raw_fields(
            candidate,
            office_locations=office_locations,
        )
        dom_fields = _firm_dom_fields(
            scanner,
            expected_slug=expected_slug,
        )
        fields = FIRM_FIELDS
    classification = _page_classification(
        scanner,
        final_url=final_url,
        status=http_status,
        content_type=content_type,
        body=body,
    )
    final_slug = slug_from_url(final_url, entity_type)
    canonical_url = dom_fields.get("_canonical_url")
    canonical_slug = (
        slug_from_url(canonical_url, entity_type) if canonical_url else None
    )
    embedded_slug = embedded_fields.get("slug")
    identity_errors: list[str] = []
    identity_missing: list[str] = []
    final_host = (urllib.parse.urlsplit(final_url).hostname or "").lower()
    canonical_host = (
        (urllib.parse.urlsplit(canonical_url).hostname or "").lower()
        if canonical_url
        else None
    )
    if not expected_slug:
        identity_errors.append("requested_url_wrong_entity_path")
    if final_host not in {ARCHITIZER_HOST, f"www.{ARCHITIZER_HOST}"}:
        identity_errors.append("final_url_external_host")
    if final_slug != expected_slug:
        identity_errors.append("final_url_slug_mismatch")
    if canonical_url:
        if canonical_host not in {ARCHITIZER_HOST, f"www.{ARCHITIZER_HOST}"}:
            identity_errors.append("canonical_external_host")
        if canonical_slug != expected_slug:
            identity_errors.append("canonical_slug_mismatch")
    if embedded_slug and embedded_slug != expected_slug:
        identity_errors.append("embedded_slug_mismatch")
    if classification["classification"] != "normal":
        identity_errors.append(
            f"abnormal_page:{classification['classification']}"
        )
    if entity_type == "project":
        project_id = embedded_fields.get("project_id")
        global_id = embedded_fields.get("global_id")
        global_id_match = re.fullmatch(
            r"projects\.project\.(\d+)",
            str(global_id or ""),
        )
        if project_id is None:
            identity_missing.append("project_id")
        if not global_id:
            identity_missing.append("global_id")
        elif global_id_match is None:
            identity_errors.append(
                "global_id_invalid_format"
                if str(global_id).startswith("projects.project.")
                else "global_id_wrong_entity_type"
            )
        elif project_id is not None:
            if int(global_id_match.group(1)) != int(project_id):
                identity_errors.append("project_id_global_id_mismatch")
        absolute_url = embedded_fields.get("_absolute_url")
        if absolute_url:
            absolute_host = (
                urllib.parse.urlsplit(absolute_url).hostname or ""
            ).lower()
            if absolute_host and absolute_host not in {
                ARCHITIZER_HOST,
                f"www.{ARCHITIZER_HOST}",
            }:
                identity_errors.append("embedded_absolute_url_external_host")
            absolute_slug = slug_from_url(absolute_url, "project")
            if absolute_slug and absolute_slug != expected_slug:
                identity_errors.append("embedded_absolute_url_slug_mismatch")
        if not (embedded_fields.get("name") or dom_fields.get("name")):
            identity_missing.append("name")
    else:
        global_id = embedded_fields.get("_global_id")
        global_id_match = re.fullmatch(
            r"firms\.firm\.(\d+)",
            str(global_id or ""),
        )
        if global_id and global_id_match is None:
            identity_errors.append(
                "global_id_invalid_format"
                if str(global_id).startswith("firms.firm.")
                else "global_id_wrong_entity_type"
            )
        absolute_url = embedded_fields.get("_absolute_url")
        if absolute_url:
            absolute_host = (
                urllib.parse.urlsplit(absolute_url).hostname or ""
            ).lower()
            if absolute_host and absolute_host not in {
                ARCHITIZER_HOST,
                f"www.{ARCHITIZER_HOST}",
            }:
                identity_errors.append("embedded_absolute_url_external_host")
            absolute_slug = slug_from_url(absolute_url, "firm")
            if absolute_slug and absolute_slug != expected_slug:
                identity_errors.append("embedded_absolute_url_slug_mismatch")
        positive_identity = (
            canonical_slug == expected_slug
            or embedded_slug == expected_slug
            or global_id_match is not None
        )
        if not positive_identity:
            identity_missing.append("firm_identity_signal")
        if not (embedded_fields.get("name") or dom_fields.get("name")):
            identity_missing.append("name")
    if identity_errors:
        identity_status = "conflict"
    elif identity_missing:
        identity_status = "missing"
    else:
        identity_status = "valid"
    resolved: dict[str, dict[str, Any]] = {}
    observations: dict[str, dict[str, Any]] = {}
    conflicts: dict[str, Any] = {}
    for field in fields:
        embedded_value = embedded_fields.get(field)
        dom_value = dom_fields.get(field)
        observations[field] = {
            "embedded_json": _normalize_field_value(field, embedded_value),
            "dom": _normalize_field_value(field, dom_value),
            "raw": {
                "embedded_json": embedded_raw_fields.get(field),
                "dom": dom_value,
            },
        }
        result = _resolve_field(field, embedded_value, dom_value)
        resolved[field] = result
        if result["conflict"] is not None:
            conflicts[field] = result["conflict"]
    if identity_status != "valid":
        parse_status = "no_content"
        quality = "invalid"
    else:
        non_identity_conflicts = len(conflicts)
        missing_count = sum(
            1 for item in resolved.values() if item["status"] == "missing"
        )
        if non_identity_conflicts:
            parse_status = "conflict"
            quality = "review"
        elif missing_count:
            parse_status = "partial"
            quality = "medium"
        else:
            parse_status = "complete"
            quality = "high"
    relationships: list[dict[str, Any]] = []
    if entity_type == "project":
        for source_kind, value in (
            ("embedded_json", embedded_fields.get("firm_slug")),
            ("dom", dom_fields.get("firm_slug")),
        ):
            firm_slug = _safe_text(value)
            if firm_slug:
                relationships.append(
                    {
                        "relation_kind": "project_firm",
                        "related_entity_type": "firm",
                        "related_slug": firm_slug,
                        "related_url": normalize_entity_url(
                            f"https://{ARCHITIZER_HOST}/firms/{firm_slug}/",
                            "firm",
                        ),
                        "source_kind": source_kind,
                        "parse_status": "observed",
                    }
                )
    else:
        for source_kind, values in (
            ("embedded_json", embedded_fields.get("project_urls") or []),
            ("dom", dom_fields.get("project_urls") or []),
        ):
            for related_url in values:
                relationships.append(
                    {
                        "relation_kind": "firm_project",
                        "related_entity_type": "project",
                        "related_slug": slug_from_url(related_url, "project"),
                        "related_url": normalize_entity_url(
                            related_url, "project"
                        ),
                        "source_kind": source_kind,
                        "parse_status": "observed",
                    }
                )
    relationships = [
        json.loads(value)
        for value in _dedupe(canonical_json(value) for value in relationships)
    ]
    return {
        "entity_type": entity_type,
        "parser_version": PARSER_VERSION,
        "metadata_version": METADATA_VERSION,
        "page_classification": classification,
        "identity": {
            "status": identity_status,
            "expected_slug": expected_slug,
            "final_slug": final_slug,
            "canonical_slug": canonical_slug,
            "embedded_slug": embedded_slug,
            "project_id": embedded_fields.get("project_id"),
            "global_id": embedded_fields.get("global_id")
            if entity_type == "project"
            else embedded_fields.get("_global_id"),
            "errors": identity_errors,
            "missing": identity_missing,
        },
        "parse_status": parse_status,
        "quality": quality,
        "embedded_records": embedded_records,
        "embedded_fields": embedded_fields,
        "dom_fields": dom_fields,
        "observations": observations,
        "resolved": resolved,
        "conflicts": conflicts,
        "relationships": relationships,
    }


def _record_http_attempts(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    result: HttpResult,
    request_kind: str,
    target_url: str | None,
    snapshot_root: Path,
    snapshot_kind: str,
    extension: str,
) -> list[dict[str, Any]]:
    recorded: list[dict[str, Any]] = []
    for attempt in result.attempts:
        digest: str | None = None
        gzip_path: str | None = None
        compressed_bytes = 0
        if attempt.body:
            digest, gzip_path, compressed_bytes = _write_gzip_snapshot(
                snapshot_root,
                kind=snapshot_kind,
                content=attempt.body,
                extension=extension,
            )
        connection.execute(
            """
            INSERT INTO http_attempts(
                run_id,target_url,request_kind,requested_url,attempt_number,
                started_at,finished_at,duration_ms,outcome,http_status,final_url,
                content_type,response_bytes,sha256,gzip_path,retryable,
                block_signals_json,error
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                target_url,
                request_kind,
                attempt.requested_url,
                attempt.attempt_number,
                attempt.started_at,
                attempt.finished_at,
                attempt.duration_ms,
                attempt.outcome,
                attempt.http_status,
                attempt.final_url,
                attempt.content_type,
                len(attempt.body),
                digest,
                gzip_path,
                int(attempt.retryable),
                canonical_json(attempt.block_signals),
                attempt.error,
            ),
        )
        recorded.append(
            {
                "sha256": digest,
                "gzip_path": gzip_path,
                "compressed_bytes": compressed_bytes,
                "response_bytes": len(attempt.body),
            }
        )
    connection.commit()
    return recorded


def _legacy_source_audit(connection: sqlite3.Connection) -> dict[str, Any]:
    project_queue_rows = [
        dict(row)
        for row in connection.execute(
            """
            SELECT url,source_url,lastmod,status,discovered_at,fetched_at,error
            FROM pending_projects ORDER BY url
            """
        )
    ]
    firm_queue_rows = [
        dict(row)
        for row in connection.execute(
            """
            SELECT url,source_url,lastmod,status,discovered_at,fetched_at,error
            FROM pending_firms ORDER BY url
            """
        )
    ]
    project_rows = [
        dict(row)
        for row in connection.execute(
            """
            SELECT id,global_id,slug,name,firm_slug,firm_name,description,
                   description_short,completion_year,building_size_slug,
                   building_size_display,constr_status,location_full,categories,
                   cover_image_url,gallery_image_urls,image_global_ids,
                   published_time,modified_time,fetched_at
            FROM architizer_projects ORDER BY slug
            """
        )
    ]
    firm_rows = [
        dict(row)
        for row in connection.execute(
            """
            SELECT slug,name,office_locations,description,awards_summary,
                   project_count_seen,social_links,fetched_at
            FROM architizer_firms ORDER BY slug
            """
        )
    ]
    award_rows = [
        dict(row)
        for row in connection.execute(
            """
            SELECT award_year,award_track,award_category,award_tier,
                   project_slug,firm_slug,source_url,fetched_at
            FROM architizer_awards
            ORDER BY award_year,award_track,id
            """
        )
    ]
    project_queue: dict[str, dict[str, Any]] = {
        normalize_entity_url(row["url"], "project"): row
        for row in project_queue_rows
    }
    firm_queue: dict[str, dict[str, Any]] = {
        normalize_entity_url(row["url"], "firm"): row for row in firm_queue_rows
    }
    projects_by_slug = {row["slug"]: row for row in project_rows}
    firms_by_slug = {row["slug"]: row for row in firm_rows}
    project_status = Counter(row["status"] for row in project_queue_rows)
    firm_status = Counter(row["status"] for row in firm_queue_rows)
    failed_projects = [
        row for row in project_queue_rows if row["status"] == "failed"
    ]
    done_project_mismatch = [
        row
        for row in project_queue_rows
        if row["status"] == "done"
        and slug_from_url(row["url"], "project") not in projects_by_slug
    ]
    done_firm_mismatch = [
        row
        for row in firm_queue_rows
        if row["status"] == "done"
        and slug_from_url(row["url"], "firm") not in firms_by_slug
    ]
    project_firm_slugs = {
        row["firm_slug"] for row in project_rows if row.get("firm_slug")
    }
    missing_project_firm_slugs = sorted(project_firm_slugs - set(firms_by_slug))
    missing_project_firm_rows = sum(
        1
        for row in project_rows
        if row.get("firm_slug") in set(missing_project_firm_slugs)
    )
    award_project_slugs = {
        row["project_slug"] for row in award_rows if row.get("project_slug")
    }
    award_firm_slugs = {
        row["firm_slug"] for row in award_rows if row.get("firm_slug")
    }
    queued_project_slugs = {
        slug_from_url(url, "project") for url in project_queue
    }
    unresolved_award_projects = sorted(
        award_project_slugs - set(projects_by_slug) - queued_project_slugs
    )
    unresolved_award_firms = sorted(award_firm_slugs - set(firms_by_slug))
    award_only_firms = sorted(
        award_firm_slugs - set(firms_by_slug) - project_firm_slugs
    )
    year_counts = Counter(row["award_year"] for row in award_rows)
    track_year_counts: dict[str, Counter[int]] = defaultdict(Counter)
    for row in award_rows:
        track_year_counts[row["award_track"]][row["award_year"]] += 1
    identity_contamination = [
        {
            "slug": row["slug"],
            "id": row["id"],
            "global_id": row["global_id"],
        }
        for row in project_rows
        if row.get("global_id")
        and not str(row["global_id"]).startswith("projects.project.")
    ]

    def shard_stats(
        queue_rows: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in queue_rows:
            grouped[str(row["source_url"])].append(row)
        return [
            {
                "source_url": source,
                "count": len(rows),
                "lastmod_min": min(
                    (row["lastmod"] for row in rows if row["lastmod"]),
                    default=None,
                ),
                "lastmod_max": max(
                    (row["lastmod"] for row in rows if row["lastmod"]),
                    default=None,
                ),
            }
            for source, rows in sorted(grouped.items())
        ]

    return {
        "project_queue_rows": project_queue_rows,
        "firm_queue_rows": firm_queue_rows,
        "project_rows": project_rows,
        "firm_rows": firm_rows,
        "award_rows": award_rows,
        "project_queue": project_queue,
        "firm_queue": firm_queue,
        "projects_by_slug": projects_by_slug,
        "firms_by_slug": firms_by_slug,
        "summary": {
            "project": {
                "queue_count": len(project_queue_rows),
                "row_count": len(project_rows),
                "status_counts": dict(sorted(project_status.items())),
                "failed": failed_projects,
                "done_row_mismatch": done_project_mismatch,
                "lastmod_min": min(
                    (
                        row["lastmod"]
                        for row in project_queue_rows
                        if row["lastmod"]
                    ),
                    default=None,
                ),
                "lastmod_max": max(
                    (
                        row["lastmod"]
                        for row in project_queue_rows
                        if row["lastmod"]
                    ),
                    default=None,
                ),
                "lastmod_null_count": sum(
                    row["lastmod"] is None for row in project_queue_rows
                ),
                "lastmod_distinct_count": len(
                    {
                        row["lastmod"]
                        for row in project_queue_rows
                        if row["lastmod"]
                    }
                ),
                "sitemap_shards": shard_stats(project_queue_rows),
                "identity_contamination": identity_contamination,
            },
            "firm": {
                "queue_count": len(firm_queue_rows),
                "row_count": len(firm_rows),
                "status_counts": dict(sorted(firm_status.items())),
                "failed": [
                    row for row in firm_queue_rows if row["status"] == "failed"
                ],
                "done_row_mismatch": done_firm_mismatch,
                "lastmod_min": min(
                    (
                        row["lastmod"]
                        for row in firm_queue_rows
                        if row["lastmod"]
                    ),
                    default=None,
                ),
                "lastmod_max": max(
                    (
                        row["lastmod"]
                        for row in firm_queue_rows
                        if row["lastmod"]
                    ),
                    default=None,
                ),
                "lastmod_null_count": sum(
                    row["lastmod"] is None for row in firm_queue_rows
                ),
                "lastmod_distinct_count": len(
                    {
                        row["lastmod"]
                        for row in firm_queue_rows
                        if row["lastmod"]
                    }
                ),
                "sitemap_shards": shard_stats(firm_queue_rows),
            },
            "firm_stubs": {
                "distinct_project_firm_slugs": len(project_firm_slugs),
                "missing_firm_slugs": len(missing_project_firm_slugs),
                "project_rows_referencing_missing_firm": missing_project_firm_rows,
                "slugs": missing_project_firm_slugs,
            },
            "awards": {
                "row_count": len(award_rows),
                "year_counts": {
                    str(year): count for year, count in sorted(year_counts.items())
                },
                "year_min": min(year_counts, default=None),
                "year_max": max(year_counts, default=None),
                "has_2026": 2026 in year_counts,
                "track_year_counts": {
                    track: {
                        str(year): count
                        for year, count in sorted(counts.items())
                    }
                    for track, counts in sorted(track_year_counts.items())
                },
                "distinct_project_slugs": len(award_project_slugs),
                "unresolved_project_slugs": len(unresolved_award_projects),
                "unresolved_project_rows": sum(
                    row.get("project_slug") in set(unresolved_award_projects)
                    for row in award_rows
                ),
                "distinct_firm_slugs": len(award_firm_slugs),
                "unresolved_firm_slugs": len(unresolved_award_firms),
                "unresolved_firm_rows": sum(
                    row.get("firm_slug") in set(unresolved_award_firms)
                    for row in award_rows
                ),
                "award_only_firm_slugs": len(award_only_firms),
            },
        },
        "seed_sets": {
            "firm_stubs": missing_project_firm_slugs,
            "award_projects": unresolved_award_projects,
            "award_firms": award_only_firms,
        },
    }


def _comparison_summary(
    *,
    entity_type: str,
    current: Mapping[str, str | None],
    legacy_queue: Mapping[str, Mapping[str, Any]],
    entity_slugs: set[str],
    occurrence_count: int,
    occurrence_duplicates: Mapping[str, int],
) -> dict[str, Any]:
    current_urls = set(current)
    legacy_urls = set(legacy_queue)
    overlap = current_urls & legacy_urls
    current_new = current_urls - legacy_urls
    legacy_missing = legacy_urls - current_urls
    changed = {
        url
        for url in overlap
        if (legacy_queue[url].get("lastmod") or None) != (current[url] or None)
    }
    current_without_row = {
        url
        for url in current_urls
        if slug_from_url(url, entity_type) not in entity_slugs
    }
    lastmods = [lastmod for lastmod in current.values() if lastmod]
    return {
        "legacy_queue_count": len(legacy_urls),
        "current_sitemap_entry_occurrences": occurrence_count,
        "current_sitemap_url_count": len(current_urls),
        "duplicate_occurrence_count": occurrence_count - len(current_urls),
        "duplicate_url_count": len(occurrence_duplicates),
        "duplicate_urls": dict(sorted(occurrence_duplicates.items())),
        "overlap": len(overlap),
        "current_new": len(current_new),
        "legacy_not_in_current": len(legacy_missing),
        "overlap_lastmod_changed": len(changed),
        "current_without_entity_row": len(current_without_row),
        "current_lastmod_min": min(lastmods, default=None),
        "current_lastmod_max": max(lastmods, default=None),
        "current_lastmod_null_count": len(current_urls) - len(lastmods),
        "sets": {
            "overlap": sorted(overlap),
            "current_new": sorted(current_new),
            "legacy_not_in_current": sorted(legacy_missing),
            "lastmod_changed": sorted(changed),
            "current_without_entity_row": sorted(current_without_row),
        },
    }


def _incoming_lastmod_changed(
    existing_source_lastmod: str | None,
    incoming_source_lastmod: str | None,
) -> bool:
    """Match the target upsert's NULL-aware lastmod scheduling rule."""

    return (
        incoming_source_lastmod is not None
        and incoming_source_lastmod != existing_source_lastmod
    )


def upsert_target(
    connection: sqlite3.Connection,
    *,
    url: str,
    entity_type: str,
    source_lastmod: str | None,
    priority: int,
    reason: str,
    discovery_source: str,
    input_lineage: Mapping[str, Any],
    reschedule: bool = False,
) -> None:
    url = normalize_entity_url(url, entity_type)
    now = utc_now()
    existing = connection.execute(
        "SELECT * FROM targets WHERE url=?",
        (url,),
    ).fetchone()
    if existing is None:
        connection.execute(
            """
            INSERT INTO targets(
                url,entity_type,source_lastmod,priority,primary_reason,status,
                retryable,created_at,updated_at
            ) VALUES (?,?,?,?,?,'pending',1,?,?)
            """,
            (
                url,
                entity_type,
                source_lastmod,
                priority,
                reason,
                now,
                now,
            ),
        )
    else:
        lastmod_changed = _incoming_lastmod_changed(
            existing["source_lastmod"],
            source_lastmod,
        )
        new_priority = min(priority, int(existing["priority"]))
        new_reason = (
            reason if priority < int(existing["priority"]) else existing["primary_reason"]
        )
        new_status = existing["status"]
        new_retryable = int(existing["retryable"])
        if reschedule or lastmod_changed:
            new_status = "pending"
            new_retryable = 1
        connection.execute(
            """
            UPDATE targets
            SET source_lastmod=COALESCE(?,source_lastmod),
                priority=?,primary_reason=?,status=?,retryable=?,updated_at=?
            WHERE url=?
            """,
            (
                source_lastmod,
                new_priority,
                new_reason,
                new_status,
                new_retryable,
                now,
                url,
            ),
        )
    connection.execute(
        """
        INSERT INTO target_reasons(
            url,reason,discovery_source,priority,source_lastmod,
            first_seen_at,last_seen_at,input_lineage_json
        ) VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(url,reason,discovery_source) DO UPDATE SET
            priority=MIN(target_reasons.priority,excluded.priority),
            source_lastmod=COALESCE(excluded.source_lastmod,
                                    target_reasons.source_lastmod),
            last_seen_at=excluded.last_seen_at,
            input_lineage_json=excluded.input_lineage_json
        """,
        (
            url,
            reason,
            discovery_source,
            priority,
            source_lastmod,
            now,
            now,
            canonical_json(dict(input_lineage)),
        ),
    )


def _seed_targets(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    source_path: Path,
    current: Mapping[str, Mapping[str, str | None]],
    comparisons: Mapping[str, Mapping[str, Any]],
    legacy: Mapping[str, Any],
    unchanged_sample_size: int,
) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for entity_type in ("project", "firm"):
        entity_comparison = comparisons[entity_type]
        legacy_queue = (
            legacy["project_queue"]
            if entity_type == "project"
            else legacy["firm_queue"]
        )
        for reason, priority in (
            ("sitemap_new", 10),
            ("current_without_entity_row", 15),
            ("sitemap_modified", 20),
        ):
            set_key = {
                "sitemap_new": "current_new",
                "current_without_entity_row": "current_without_entity_row",
                "sitemap_modified": "lastmod_changed",
            }[reason]
            for url in entity_comparison["sets"][set_key]:
                upsert_target(
                    connection,
                    url=url,
                    entity_type=entity_type,
                    source_lastmod=current[entity_type].get(url),
                    priority=priority,
                    reason=reason,
                    discovery_source=f"census_run:{run_id}",
                    input_lineage={
                        "census_run_id": run_id,
                        "legacy_lastmod": legacy_queue.get(url, {}).get("lastmod"),
                        "current_lastmod": current[entity_type].get(url),
                    },
                    reschedule=False,
                )
                counts[reason] += 1
    project_recovery = (
        legacy["summary"]["project"]["failed"]
        + legacy["summary"]["project"]["done_row_mismatch"]
    )
    for row in project_recovery:
        reason = (
            "legacy_failed_retry"
            if row["status"] == "failed"
            else "legacy_done_row_mismatch"
        )
        upsert_target(
            connection,
            url=row["url"],
            entity_type="project",
            source_lastmod=row.get("lastmod"),
            priority=30,
            reason=reason,
            discovery_source="legacy:pending_projects",
            input_lineage={
                "legacy_status": row["status"],
                "legacy_error": row.get("error"),
                "legacy_source_url": row.get("source_url"),
            },
            reschedule=False,
        )
        counts[reason] += 1
    for slug in legacy["seed_sets"]["firm_stubs"]:
        upsert_target(
            connection,
            url=f"https://{ARCHITIZER_HOST}/firms/{slug}/",
            entity_type="firm",
            source_lastmod=None,
            priority=40,
            reason="legacy_project_firm_stub",
            discovery_source="legacy:architizer_projects.firm_slug",
            input_lineage={"legacy_source_db": str(source_path.resolve())},
        )
        counts["legacy_project_firm_stub"] += 1
    for slug in legacy["seed_sets"]["award_projects"]:
        upsert_target(
            connection,
            url=f"https://{ARCHITIZER_HOST}/projects/{slug}/",
            entity_type="project",
            source_lastmod=None,
            priority=50,
            reason="legacy_award_project_seed",
            discovery_source="legacy:architizer_awards.project_slug",
            input_lineage={"legacy_award_only": True},
        )
        counts["legacy_award_project_seed"] += 1
    for slug in legacy["seed_sets"]["award_firms"]:
        upsert_target(
            connection,
            url=f"https://{ARCHITIZER_HOST}/firms/{slug}/",
            entity_type="firm",
            source_lastmod=None,
            priority=50,
            reason="legacy_award_firm_seed",
            discovery_source="legacy:architizer_awards.firm_slug",
            input_lineage={"legacy_award_only": True},
        )
        counts["legacy_award_firm_seed"] += 1
    unchanged = comparisons["project"]["sets"]["overlap"]
    changed = set(comparisons["project"]["sets"]["lastmod_changed"])
    unchanged = [url for url in unchanged if url not in changed]
    unchanged.sort(
        key=lambda url: hashlib.sha256(
            f"architizer-unchanged-v1:{url}".encode()
        ).hexdigest()
    )
    for url in unchanged[:unchanged_sample_size]:
        upsert_target(
            connection,
            url=url,
            entity_type="project",
            source_lastmod=current["project"].get(url),
            priority=60,
            reason="deterministic_unchanged_sample",
            discovery_source=f"census_run:{run_id}",
            input_lineage={
                "sample_algorithm": "sha256(architizer-unchanged-v1:url)",
                "census_run_id": run_id,
            },
        )
        counts["deterministic_unchanged_sample"] += 1
    connection.commit()
    return dict(sorted(counts.items()))


def _public_census_manifest(
    *,
    run_id: int,
    observed_at: str,
    source_path: Path,
    source_sha: str,
    source_size: int,
    source_sha_after: str,
    index_info: Mapping[str, Any],
    child_info: Sequence[Mapping[str, Any]],
    comparisons: Mapping[str, Mapping[str, Any]],
    legacy_summary: Mapping[str, Any],
    target_seed_counts: Mapping[str, int],
) -> dict[str, Any]:
    public_comparisons: dict[str, Any] = {}
    for entity_type, value in comparisons.items():
        public_comparisons[entity_type] = {
            key: item for key, item in value.items() if key != "sets"
        }
        public_comparisons[entity_type]["current_new_urls_sha256"] = sha256_bytes(
            "\n".join(value["sets"]["current_new"]).encode("utf-8")
        )
        public_comparisons[entity_type][
            "legacy_not_in_current_urls_sha256"
        ] = sha256_bytes(
            "\n".join(value["sets"]["legacy_not_in_current"]).encode("utf-8")
        )
        public_comparisons[entity_type][
            "lastmod_changed_urls_sha256"
        ] = sha256_bytes(
            "\n".join(value["sets"]["lastmod_changed"]).encode("utf-8")
        )
        public_comparisons[entity_type][
            "current_without_entity_row_urls"
        ] = value["sets"]["current_without_entity_row"][:100]
    compact_legacy = json.loads(canonical_json(legacy_summary))
    compact_legacy["firm_stubs"].pop("slugs", None)
    return {
        "manifest_version": "architizer-source-census-v2",
        "run_id": run_id,
        "observed_at": observed_at,
        "network_scope": "official sitemap index and registered project/firm children only",
        "official_sitemap": dict(index_info),
        "registered_children": [dict(item) for item in child_info],
        "legacy_input": {
            "path": str(source_path.resolve()),
            "size": source_size,
            "sha256_before": source_sha,
            "sha256_after": source_sha_after,
            "immutable": source_sha == source_sha_after,
            "sqlite_open_mode": "mode=ro&immutable=1; query_only=ON",
        },
        "comparison": public_comparisons,
        "legacy_summary": compact_legacy,
        "target_seed_counts": dict(target_seed_counts),
        "assessment": {
            "source_complete": False,
            "rolling_window": "strong_evidence",
            "missing_from_current_action": "retain; never auto-delete",
            "reason": (
                "Current and legacy project/firm sitemap ranges each span "
                "approximately one year; sitemap absence does not distinguish "
                "window expiry from deletion or hiding."
            ),
        },
        "open_qa": [
            "Sitemap absence cannot distinguish deletion, hiding, or window expiry.",
            "The semantic trigger behind Architizer lastmod is undocumented.",
            "Day-level ties at page boundaries may create duplicates or omissions.",
        ],
    }


def run_source_census(
    *,
    source_path: Path = DEFAULT_SOURCE_DB,
    state_path: Path = DEFAULT_STATE_DB,
    snapshot_root: Path = DEFAULT_SNAPSHOT_DIR,
    sitemap_url: str = OFFICIAL_SITEMAP_URL,
    delay_seconds: float = 1.0,
    timeout_seconds: float = 30.0,
    max_attempts: int = 3,
    unchanged_sample_size: int = 200,
) -> dict[str, Any]:
    """Fetch only the official sitemap index/registered project+firm leaves."""

    source_path = source_path.resolve()
    state_path = state_path.resolve()
    snapshot_root = snapshot_root.resolve()
    sitemap_url = validate_official_sitemap_url(sitemap_url, index=True)
    validate_runtime_paths(
        source_path=source_path,
        state_path=state_path,
        snapshot_root=snapshot_root,
    )
    source_sha = sha256_file(source_path)
    source_size = source_path.stat().st_size
    observed_at = utc_now()
    arguments = {
        "sitemap_url": sitemap_url,
        "delay_seconds": delay_seconds,
        "timeout_seconds": timeout_seconds,
        "max_attempts": max_attempts,
        "unchanged_sample_size": unchanged_sample_size,
    }
    with SidecarLock(state_path):
        state = connect_state(
            state_path,
            source_path=source_path,
            source_sha256=source_sha,
            source_size=source_size,
        )
        run_id = start_run(
            state,
            run_kind="sitemap_census",
            source_path=source_path,
            source_sha256=source_sha,
            source_size=source_size,
            arguments=arguments,
        )
        try:
            legacy_connection = open_legacy_readonly(source_path)
            try:
                legacy = _legacy_source_audit(legacy_connection)
            finally:
                legacy_connection.close()
            client = PoliteHttpClient(
                delay_seconds=delay_seconds,
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
                jitter_seed=f"census:{run_id}",
                allowed_hosts={ARCHITIZER_HOST, f"www.{ARCHITIZER_HOST}"},
            )
            index_result = client.fetch(sitemap_url)
            index_attempt = index_result.final
            if (
                not index_attempt.final_url
                or validate_official_sitemap_url(
                    index_attempt.final_url,
                    index=True,
                )
                != sitemap_url
            ):
                raise RecrawlError(
                    "official sitemap index redirected to an unexpected URL"
                )
            index_attempt_records = _record_http_attempts(
                state,
                run_id=run_id,
                result=index_result,
                request_kind="sitemap_index",
                target_url=None,
                snapshot_root=snapshot_root,
                snapshot_kind="sitemaps",
                extension="xml",
            )
            index_record = index_attempt_records[-1]
            if index_attempt.outcome != "success" or index_attempt.http_status != 200:
                raise RecrawlError(
                    f"official sitemap fetch failed: {index_attempt.error}"
                )
            index_entries = parse_sitemap_index(index_attempt.body)
            registered = official_entity_sitemaps(index_entries)
            if not registered["project"] or not registered["firm"]:
                raise RecrawlError(
                    "official sitemap index has no registered project/firm children"
                )
            index_snapshot = state.execute(
                """
                INSERT INTO sitemap_snapshots(
                    run_id,entity_type,sitemap_url,discovered_at,fetched_at,
                    http_status,final_url,content_type,content_bytes,sha256,
                    gzip_path,parse_status,url_count,lastmod_min,lastmod_max,error
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,'parsed',?,?,?,NULL)
                """,
                (
                    run_id,
                    "index",
                    normalize_sitemap_url(sitemap_url),
                    observed_at,
                    index_attempt.finished_at,
                    index_attempt.http_status,
                    index_attempt.final_url,
                    index_attempt.content_type,
                    len(index_attempt.body),
                    index_record["sha256"],
                    index_record["gzip_path"],
                    len(index_entries),
                    min(
                        (
                            item["lastmod"]
                            for item in index_entries
                            if item.get("lastmod")
                        ),
                        default=None,
                    ),
                    max(
                        (
                            item["lastmod"]
                            for item in index_entries
                            if item.get("lastmod")
                        ),
                        default=None,
                    ),
                ),
            )
            del index_snapshot
            state.commit()
            current: dict[str, dict[str, str | None]] = {
                "project": {},
                "firm": {},
            }
            occurrences: dict[str, Counter[str]] = {
                "project": Counter(),
                "firm": Counter(),
            }
            occurrence_counts: Counter[str] = Counter()
            child_info: list[dict[str, Any]] = []
            for entity_type in ("project", "firm"):
                for child_url in registered[entity_type]:
                    child_url = validate_official_sitemap_url(child_url)
                    result = client.fetch(child_url)
                    attempt = result.final
                    if (
                        not attempt.final_url
                        or validate_official_sitemap_url(attempt.final_url)
                        != child_url
                    ):
                        raise RecrawlError(
                            "registered sitemap redirected to an unexpected "
                            f"URL: {child_url}"
                        )
                    records = _record_http_attempts(
                        state,
                        run_id=run_id,
                        result=result,
                        request_kind=f"{entity_type}_sitemap",
                        target_url=None,
                        snapshot_root=snapshot_root,
                        snapshot_kind="sitemaps",
                        extension="xml",
                    )
                    record = records[-1]
                    if attempt.outcome != "success" or attempt.http_status != 200:
                        raise RecrawlError(
                            f"registered sitemap fetch failed: {child_url}: "
                            f"{attempt.error}"
                        )
                    entries = parse_sitemap_urls(attempt.body)
                    normalized_entries: list[dict[str, str | None]] = []
                    for entry in entries:
                        normalized_url = validate_sitemap_entity_url(
                            str(entry["loc"]),
                            entity_type,
                        )
                        normalized_entries.append(
                            {
                                "url": normalized_url,
                                "lastmod": entry.get("lastmod"),
                            }
                        )
                    lastmods = [
                        item["lastmod"]
                        for item in normalized_entries
                        if item["lastmod"]
                    ]
                    snapshot_cursor = state.execute(
                        """
                        INSERT INTO sitemap_snapshots(
                            run_id,entity_type,sitemap_url,discovered_at,fetched_at,
                            http_status,final_url,content_type,content_bytes,sha256,
                            gzip_path,parse_status,url_count,lastmod_min,lastmod_max,error
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,'parsed',?,?,?,NULL)
                        """,
                        (
                            run_id,
                            entity_type,
                            child_url,
                            observed_at,
                            attempt.finished_at,
                            attempt.http_status,
                            attempt.final_url,
                            attempt.content_type,
                            len(attempt.body),
                            record["sha256"],
                            record["gzip_path"],
                            len(normalized_entries),
                            min(lastmods, default=None),
                            max(lastmods, default=None),
                        ),
                    )
                    snapshot_id = int(snapshot_cursor.lastrowid)
                    for ordinal, item in enumerate(normalized_entries):
                        url = str(item["url"])
                        lastmod = item["lastmod"]
                        occurrence_counts[entity_type] += 1
                        occurrences[entity_type][url] += 1
                        state.execute(
                            """
                            INSERT INTO sitemap_entry_occurrences(
                                run_id,snapshot_id,entity_type,source_url,lastmod,
                                discovery_source,ordinal
                            ) VALUES (?,?,?,?,?,?,?)
                            """,
                            (
                                run_id,
                                snapshot_id,
                                entity_type,
                                url,
                                lastmod,
                                child_url,
                                ordinal,
                            ),
                        )
                        if url in current[entity_type]:
                            prior = current[entity_type][url]
                            if prior != lastmod:
                                raise RecrawlError(
                                    "same URL has conflicting lastmod across "
                                    f"registered sitemaps: {url}: "
                                    f"{prior!r} != {lastmod!r}"
                                )
                        else:
                            current[entity_type][url] = lastmod
                            state.execute(
                                """
                                INSERT INTO sitemap_entries(
                                    run_id,snapshot_id,entity_type,source_url,
                                    lastmod,discovery_source,discovered_at
                                ) VALUES (?,?,?,?,?,?,?)
                                """,
                                (
                                    run_id,
                                    snapshot_id,
                                    entity_type,
                                    url,
                                    lastmod,
                                    child_url,
                                    observed_at,
                                ),
                            )
                    state.commit()
                    child_info.append(
                        {
                            "entity_type": entity_type,
                            "url": child_url,
                            "sha256": record["sha256"],
                            "content_bytes": len(attempt.body),
                            "entry_count": len(normalized_entries),
                            "lastmod_min": min(lastmods, default=None),
                            "lastmod_max": max(lastmods, default=None),
                        }
                    )
            comparisons = {
                "project": _comparison_summary(
                    entity_type="project",
                    current=current["project"],
                    legacy_queue=legacy["project_queue"],
                    entity_slugs=set(legacy["projects_by_slug"]),
                    occurrence_count=occurrence_counts["project"],
                    occurrence_duplicates={
                        url: count
                        for url, count in occurrences["project"].items()
                        if count > 1
                    },
                ),
                "firm": _comparison_summary(
                    entity_type="firm",
                    current=current["firm"],
                    legacy_queue=legacy["firm_queue"],
                    entity_slugs=set(legacy["firms_by_slug"]),
                    occurrence_count=occurrence_counts["firm"],
                    occurrence_duplicates={
                        url: count
                        for url, count in occurrences["firm"].items()
                        if count > 1
                    },
                ),
            }
            seed_counts = _seed_targets(
                state,
                run_id=run_id,
                source_path=source_path,
                current=current,
                comparisons=comparisons,
                legacy=legacy,
                unchanged_sample_size=unchanged_sample_size,
            )
            source_sha_after = sha256_file(source_path)
            if source_sha_after != source_sha:
                raise RecrawlError("legacy source DB changed during read-only census")
            index_info = {
                "url": normalize_sitemap_url(sitemap_url),
                "sha256": index_record["sha256"],
                "content_bytes": len(index_attempt.body),
                "child_count": len(index_entries),
                "registered_project_sitemaps": len(registered["project"]),
                "registered_firm_sitemaps": len(registered["firm"]),
                "http_status": index_attempt.http_status,
                "final_url": index_attempt.final_url,
                "content_type": index_attempt.content_type,
            }
            manifest = _public_census_manifest(
                run_id=run_id,
                observed_at=observed_at,
                source_path=source_path,
                source_sha=source_sha,
                source_size=source_size,
                source_sha_after=source_sha_after,
                index_info=index_info,
                child_info=child_info,
                comparisons=comparisons,
                legacy_summary=legacy["summary"],
                target_seed_counts=seed_counts,
            )
            finish_run(
                state,
                run_id,
                status="completed",
                source_sha256_after=source_sha_after,
                summary=manifest,
            )
            return manifest
        except Exception as exc:
            source_sha_after = (
                sha256_file(source_path) if source_path.exists() else source_sha
            )
            finish_run(
                state,
                run_id,
                status="failed",
                source_sha256_after=source_sha_after,
                summary={},
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        finally:
            state.close()


def recover_interrupted_state(connection: sqlite3.Connection) -> int:
    """Resume work left in-progress by an interrupted prior process."""

    now = utc_now()
    connection.execute(
        """
        UPDATE runs
        SET status='interrupted',finished_at=COALESCE(finished_at,?),
            error=COALESCE(error,'process ended while run status was running')
        WHERE status='running'
        """,
        (now,),
    )
    cursor = connection.execute(
        """
        UPDATE targets
        SET status='pending',updated_at=?,
            last_error=COALESCE(last_error,'recovered interrupted claim')
        WHERE status='in_progress'
        """,
        (now,),
    )
    connection.commit()
    return int(cursor.rowcount)


def _target_reason_sets(
    connection: sqlite3.Connection,
) -> dict[str, set[str]]:
    output: dict[str, set[str]] = defaultdict(set)
    for row in connection.execute(
        "SELECT url,reason FROM target_reasons ORDER BY url,reason"
    ):
        output[row["url"]].add(row["reason"])
    return output


def _selected_in_prior_smoke(
    connection: sqlite3.Connection,
    *,
    parser_version: str = PARSER_VERSION,
) -> set[str]:
    return {
        row["url"]
        for row in connection.execute(
            """
            SELECT DISTINCT rt.url
            FROM run_targets rt
            JOIN runs r ON r.id=rt.run_id
            WHERE r.run_kind IN ('network_smoke_n10','network_smoke_n100')
              AND r.status='completed'
              AND r.parser_version=?
              AND json_extract(
                    r.summary_json,'$.gate_policy_version'
                  )=?
              AND json_extract(r.summary_json,'$.gate_passed')=1
            """,
            (parser_version, SMOKE_GATE_POLICY_VERSION),
        )
    }


def select_network_targets(
    connection: sqlite3.Connection,
    *,
    smoke_size: int,
    run_kind: str,
) -> list[dict[str, Any]]:
    """Select a deterministic, priority-stratified, previously-unseen smoke."""

    if smoke_size not in {10, 100}:
        raise ValueError("network smoke size must be 10 or 100")
    reasons = _target_reason_sets(connection)
    excluded = _selected_in_prior_smoke(connection)
    rows = [
        dict(row)
        for row in connection.execute(
            """
            SELECT *
            FROM targets AS t
            WHERE (
                status='pending'
                OR (status='failed' AND retryable=1)
                OR (
                    status='done'
                    AND NOT EXISTS (
                        SELECT 1
                        FROM metadata_versions AS mv
                        WHERE mv.target_url=t.url
                          AND mv.parser_version=?
                    )
                )
                OR (
                    EXISTS (
                        SELECT 1
                        FROM target_reasons AS tr
                        WHERE tr.url=t.url
                          AND tr.reason IN (
                              'legacy_failed_retry',
                              'legacy_done_row_mismatch'
                          )
                    )
                )
            )
            AND (next_retry_at IS NULL OR next_retry_at <= ?)
            ORDER BY priority,entity_type,url
            """,
            (PARSER_VERSION, utc_now()),
        )
        if row["url"] not in excluded
    ]
    if len(rows) < smoke_size:
        raise RecrawlError(
            f"only {len(rows)} previously-unseen eligible targets; "
            f"{smoke_size} required"
        )
    if smoke_size == 10:
        quotas = (
            ("sitemap_new_project", 2),
            ("sitemap_modified_project", 2),
            ("legacy_recovery", 2),
            ("firm_stub", 2),
            ("award_seed", 1),
            ("unchanged", 1),
        )
    else:
        quotas = (
            ("sitemap_new_project", 25),
            ("sitemap_modified_project", 20),
            ("legacy_recovery", 10),
            ("firm_stub", 20),
            ("award_seed", 15),
            ("unchanged", 10),
        )

    def bucket(row: Mapping[str, Any], name: str) -> bool:
        row_reasons = reasons[row["url"]]
        if name == "sitemap_new_project":
            return row["entity_type"] == "project" and "sitemap_new" in row_reasons
        if name == "sitemap_modified_project":
            return (
                row["entity_type"] == "project"
                and "sitemap_modified" in row_reasons
            )
        if name == "legacy_recovery":
            return bool(
                {
                    "legacy_failed_retry",
                    "legacy_done_row_mismatch",
                }
                & row_reasons
            )
        if name == "firm_stub":
            return (
                row["entity_type"] == "firm"
                and "legacy_project_firm_stub" in row_reasons
            )
        if name == "award_seed":
            return any(
                reason.startswith("legacy_award_")
                or (reason.startswith("award_") and reason.endswith("_seed"))
                for reason in row_reasons
            )
        if name == "unchanged":
            return "deterministic_unchanged_sample" in row_reasons
        return False

    def order_key(row: Mapping[str, Any], bucket_name: str) -> str:
        digest = hashlib.sha256(
            f"{run_kind}:{bucket_name}:{row['url']}".encode("utf-8")
        ).hexdigest()
        if bucket_name != "legacy_recovery":
            return digest
        recovery_rank = int(
            "legacy_failed_retry" not in reasons[row["url"]]
        )
        return f"{recovery_rank}:{digest}"

    selected: list[dict[str, Any]] = []
    selected_urls: set[str] = set()
    for bucket_name, quota in quotas:
        candidates = [
            row
            for row in rows
            if row["url"] not in selected_urls and bucket(row, bucket_name)
        ]
        candidates.sort(key=lambda row: order_key(row, bucket_name))
        for row in candidates[:quota]:
            row["selected_reason"] = bucket_name
            selected.append(row)
            selected_urls.add(row["url"])
    if len(selected) < smoke_size:
        candidates = [row for row in rows if row["url"] not in selected_urls]
        candidates.sort(
            key=lambda row: (
                int(row["priority"]),
                hashlib.sha256(
                    f"{run_kind}:fill:{row['url']}".encode("utf-8")
                ).hexdigest(),
            )
        )
        for row in candidates[: smoke_size - len(selected)]:
            row["selected_reason"] = "priority_fill"
            selected.append(row)
            selected_urls.add(row["url"])
    if len(selected) != smoke_size:
        raise RecrawlError(
            f"selection produced {len(selected)} targets, expected {smoke_size}"
        )
    return selected


def _legacy_value_map(
    legacy_connection: sqlite3.Connection,
    *,
    entity_type: str,
    slug: str,
) -> dict[str, Any]:
    if entity_type == "project":
        row = legacy_connection.execute(
            """
            SELECT id,global_id,slug,name,firm_slug,firm_name,description,
                   description_short,completion_year,building_size_slug,
                   building_size_display,constr_status,location_full,categories,
                   cover_image_url,gallery_image_urls,image_global_ids,
                   published_time,modified_time
            FROM architizer_projects WHERE slug=?
            """,
            (slug,),
        ).fetchone()
        if row is None:
            return {}
        values = dict(row)

        def json_list(raw: Any) -> list[Any]:
            if not raw:
                return []
            try:
                parsed = json.loads(raw)
                return parsed if isinstance(parsed, list) else [parsed]
            except (json.JSONDecodeError, TypeError):
                return [raw]

        return {
            "project_id": values["id"],
            "global_id": values["global_id"],
            "slug": values["slug"],
            "name": values["name"],
            "firm_slug": values["firm_slug"],
            "firm_name": values["firm_name"],
            "location": values["location_full"],
            "completion_year": values["completion_year"],
            "construction_status": values["constr_status"],
            "size_bucket": values["building_size_slug"]
            or values["building_size_display"],
            "description": values["description"],
            "description_short": values["description_short"],
            "categories": json_list(values["categories"]),
            "cover_image_url": values["cover_image_url"],
            "gallery_image_urls": json_list(values["gallery_image_urls"]),
            "image_global_ids": json_list(values["image_global_ids"]),
            "published_time": values["published_time"],
            "modified_time": values["modified_time"],
        }
    row = legacy_connection.execute(
        """
        SELECT slug,name,description,office_locations,social_links
        FROM architizer_firms WHERE slug=?
        """,
        (slug,),
    ).fetchone()
    if row is None:
        return {}
    values = dict(row)

    def parse_json(raw: Any, default: Any) -> Any:
        if not raw:
            return default
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw

    return {
        "slug": values["slug"],
        "name": values["name"],
        "description": values["description"],
        "office_locations": parse_json(values["office_locations"], []),
        "project_urls": [],
        "social_links": parse_json(values["social_links"], {}),
    }


def _promote_parse_result(
    connection: sqlite3.Connection,
    *,
    target_url: str,
    version_id: int,
    parsed: Mapping[str, Any],
) -> None:
    """Promote only non-missing, non-conflicting fields from a valid parse."""

    if parsed["identity"]["status"] != "valid":
        return
    quality_rank = {"none": 0, "low": 1, "medium": 2, "high": 3}
    for field_name, result in parsed["resolved"].items():
        if result["status"] not in {"confirmed", "single_source"}:
            continue
        if result["value"] is None:
            continue
        existing = connection.execute(
            """
            SELECT status,quality FROM current_fields
            WHERE target_url=? AND field_name=?
            """,
            (target_url, field_name),
        ).fetchone()
        should_promote = existing is None
        if existing is not None:
            should_promote = (
                result["status"] == "confirmed"
                or quality_rank.get(result["quality"], 0)
                >= quality_rank.get(existing["quality"], 0)
            )
        if should_promote:
            connection.execute(
                """
                INSERT INTO current_fields(
                    target_url,field_name,value_json,status,quality,
                    version_id,updated_at
                ) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(target_url,field_name) DO UPDATE SET
                    value_json=excluded.value_json,
                    status=excluded.status,
                    quality=excluded.quality,
                    version_id=excluded.version_id,
                    updated_at=excluded.updated_at
                """,
                (
                    target_url,
                    field_name,
                    canonical_json(result["value"]),
                    result["status"],
                    result["quality"],
                    version_id,
                    utc_now(),
                ),
            )
    connection.execute(
        """
        UPDATE targets
        SET current_metadata_version_id=?,last_good_version_id=?
        WHERE url=?
        """,
        (version_id, version_id, target_url),
    )


def _promote_stored_metadata_version(
    connection: sqlite3.Connection,
    *,
    target_url: str,
    version_id: int,
) -> None:
    metadata = connection.execute(
        "SELECT identity_status FROM metadata_versions WHERE id=? AND target_url=?",
        (version_id, target_url),
    ).fetchone()
    if metadata is None:
        raise RecrawlError("stored metadata version disappeared before promotion")
    resolved: dict[str, dict[str, Any]] = {}
    for row in connection.execute(
        """
        SELECT field_name,value_json,status,quality,conflict_json
        FROM resolved_fields WHERE version_id=?
        """,
        (version_id,),
    ):
        resolved[row["field_name"]] = {
            "value": json.loads(row["value_json"])
            if row["value_json"] is not None
            else None,
            "status": row["status"],
            "quality": row["quality"],
            "conflict": json.loads(row["conflict_json"])
            if row["conflict_json"] is not None
            else None,
        }
    _promote_parse_result(
        connection,
        target_url=target_url,
        version_id=version_id,
        parsed={
            "identity": {"status": metadata["identity_status"]},
            "resolved": resolved,
        },
    )


def _store_parse_result(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    target: Mapping[str, Any],
    snapshot_sha: str,
    parsed: Mapping[str, Any],
    legacy_connection: sqlite3.Connection,
    promote_valid: bool = True,
    commit: bool = True,
) -> int:
    raw_embedded = [
        {
            "source": record["source"],
            "raw": record["raw"],
            "parse_status": record["parse_status"],
            "parse_variant": record.get("parse_variant"),
            "context": record.get("context", {}),
        }
        for record in parsed["embedded_records"]
    ]
    cursor = connection.execute(
        """
        INSERT INTO metadata_versions(
            run_id,target_url,entity_type,snapshot_sha256,parser_version,
            metadata_version,parsed_at,parse_status,quality,identity_status,
            identity_json,raw_embedded_json,dom_json,resolved_json,conflict_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(target_url,snapshot_sha256,parser_version) DO NOTHING
        """,
        (
            run_id,
            target["url"],
            target["entity_type"],
            snapshot_sha,
            PARSER_VERSION,
            METADATA_VERSION,
            utc_now(),
            parsed["parse_status"],
            parsed["quality"],
            parsed["identity"]["status"],
            canonical_json(parsed["identity"]),
            canonical_json(raw_embedded),
            canonical_json(parsed["dom_fields"]),
            canonical_json(parsed["resolved"]),
            canonical_json(parsed["conflicts"]),
        ),
    )
    row = connection.execute(
        """
        SELECT id FROM metadata_versions
        WHERE target_url=? AND snapshot_sha256=? AND parser_version=?
        """,
        (target["url"], snapshot_sha, PARSER_VERSION),
    ).fetchone()
    if row is None:
        raise RecrawlError("metadata version upsert failed")
    version_id = int(row["id"])
    connection.execute(
        """
        INSERT OR IGNORE INTO run_metadata_versions(
            run_id,version_id,target_url
        ) VALUES (?,?,?)
        """,
        (run_id, version_id, target["url"]),
    )
    for field_name, observation in parsed["observations"].items():
        for source_kind in ("embedded_json", "dom"):
            value = observation[source_kind]
            raw_value = observation.get("raw", {}).get(source_kind)
            status = (
                "missing" if value in (None, "", [], {}) else "observed"
            )
            quality = "high" if source_kind == "embedded_json" else "medium"
            connection.execute(
                """
                INSERT OR IGNORE INTO field_observations(
                    version_id,field_name,source_kind,raw_value_json,
                    normalized_value_json,parse_status,quality
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    version_id,
                    field_name,
                    source_kind,
                    canonical_json(raw_value),
                    canonical_json(value),
                    status,
                    quality if status == "observed" else "none",
                ),
            )
    for field_name, result in parsed["resolved"].items():
        connection.execute(
            """
            INSERT OR IGNORE INTO resolved_fields(
                version_id,field_name,value_json,status,quality,conflict_json
            ) VALUES (?,?,?,?,?,?)
            """,
            (
                version_id,
                field_name,
                canonical_json(result["value"])
                if result["value"] is not None
                else None,
                result["status"],
                result["quality"],
                canonical_json(result["conflict"])
                if result["conflict"] is not None
                else None,
            ),
        )
    for relation in parsed["relationships"]:
        connection.execute(
            """
            INSERT OR IGNORE INTO relationships(
                version_id,relation_kind,related_entity_type,related_slug,
                related_url,source_kind,parse_status
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (
                version_id,
                relation["relation_kind"],
                relation["related_entity_type"],
                relation.get("related_slug") or "",
                relation.get("related_url") or "",
                relation["source_kind"],
                relation["parse_status"],
            ),
        )
    expected_slug = parsed["identity"].get("expected_slug")
    legacy_values = (
        _legacy_value_map(
            legacy_connection,
            entity_type=target["entity_type"],
            slug=expected_slug,
        )
        if expected_slug
        else {}
    )
    for field_name, result in parsed["resolved"].items():
        observed = result["value"]
        legacy_value = legacy_values.get(field_name)
        normalized_observed = _normalize_field_value(field_name, observed)
        normalized_legacy = _normalize_field_value(field_name, legacy_value)
        if normalized_observed in (None, "", [], {}):
            normalized_observed = None
        if normalized_legacy in (None, "", [], {}):
            normalized_legacy = None
        if normalized_observed is None and normalized_legacy is None:
            comparison_status = "missing_both"
        elif normalized_observed is None:
            comparison_status = "observed_missing"
        elif normalized_legacy is None:
            comparison_status = "legacy_missing"
        elif _comparison_key(normalized_observed) == _comparison_key(
            normalized_legacy
        ):
            comparison_status = "match"
        else:
            comparison_status = "conflict"
        connection.execute(
            """
            INSERT OR IGNORE INTO legacy_field_comparisons(
                version_id,field_name,legacy_value_json,observed_value_json,
                comparison_status
            ) VALUES (?,?,?,?,?)
            """,
            (
                version_id,
                field_name,
                canonical_json(normalized_legacy)
                if normalized_legacy is not None
                else None,
                canonical_json(normalized_observed)
                if normalized_observed is not None
                else None,
                comparison_status,
            ),
        )
    if promote_valid:
        _promote_parse_result(
            connection,
            target_url=target["url"],
            version_id=version_id,
            parsed=parsed,
        )
    if commit:
        connection.commit()
    return version_id


def _schedule_discovered_relationships(
    connection: sqlite3.Connection,
    *,
    version_id: int,
    legacy_connection: sqlite3.Connection,
) -> int:
    metadata = connection.execute(
        """
        SELECT entity_type,identity_status
        FROM metadata_versions WHERE id=?
        """,
        (version_id,),
    ).fetchone()
    if metadata is None or metadata["identity_status"] != "valid":
        return 0
    relation_field = (
        "firm_slug" if metadata["entity_type"] == "project" else "project_urls"
    )
    resolved_relation = connection.execute(
        """
        SELECT value_json,status
        FROM resolved_fields
        WHERE version_id=? AND field_name=?
        """,
        (version_id, relation_field),
    ).fetchone()
    if (
        resolved_relation is None
        or resolved_relation["status"] not in {"confirmed", "single_source"}
        or not resolved_relation["value_json"]
    ):
        return 0
    resolved_value = json.loads(resolved_relation["value_json"])
    allowed_slugs: set[str] = set()
    allowed_urls: set[str] = set()
    if metadata["entity_type"] == "project":
        resolved_slug = _safe_text(resolved_value)
        if resolved_slug:
            allowed_slugs.add(resolved_slug)
    else:
        values = resolved_value if isinstance(resolved_value, list) else []
        for value in values:
            try:
                allowed_urls.add(normalize_entity_url(str(value), "project"))
            except RecrawlError:
                continue
    count = 0
    for row in connection.execute(
        """
        SELECT relation_kind,related_entity_type,related_slug,related_url
        FROM relationships WHERE version_id=?
        """,
        (version_id,),
    ):
        slug = row["related_slug"] or None
        url = row["related_url"] or None
        if not slug or not url:
            continue
        if (
            metadata["entity_type"] == "project"
            and slug not in allowed_slugs
        ):
            continue
        if metadata["entity_type"] == "firm":
            try:
                normalized_relation_url = normalize_entity_url(url, "project")
            except RecrawlError:
                continue
            if normalized_relation_url not in allowed_urls:
                continue
            url = normalized_relation_url
        table = (
            "architizer_firms"
            if row["related_entity_type"] == "firm"
            else "architizer_projects"
        )
        exists = legacy_connection.execute(
            f"SELECT 1 FROM {table} WHERE slug=? LIMIT 1",
            (slug,),
        ).fetchone()
        if exists:
            continue
        priority = 40 if row["related_entity_type"] == "firm" else 55
        reason = (
            "recrawl_project_firm_relation"
            if row["relation_kind"] == "project_firm"
            else "recrawl_firm_project_relation"
        )
        upsert_target(
            connection,
            url=url,
            entity_type=row["related_entity_type"],
            source_lastmod=None,
            priority=priority,
            reason=reason,
            discovery_source=f"metadata_version:{version_id}",
            input_lineage={
                "metadata_version_id": version_id,
                "relation_kind": row["relation_kind"],
            },
        )
        count += 1
    connection.commit()
    return count


def _url_set_sha256(urls: Iterable[str]) -> str:
    payload = "\n".join(sorted(set(urls))).encode("utf-8")
    return sha256_bytes(payload)


def _pending_discoveries_for_run(
    connection: sqlite3.Connection,
    *,
    run_id: int,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            """
            SELECT DISTINCT t.url,t.entity_type,t.status,t.retryable,
                            t.primary_reason
            FROM targets AS t
            JOIN target_reasons AS tr ON tr.url=t.url
            WHERE tr.discovery_source IN (
                SELECT 'metadata_version:' || version_id
                FROM run_metadata_versions
                WHERE run_id=?
            )
              AND NOT EXISTS (
                SELECT 1 FROM run_targets AS rt
                WHERE rt.run_id=? AND rt.url=t.url
            )
              AND (
                  t.status='pending'
                  OR (t.status='failed' AND t.retryable=1)
              )
            ORDER BY t.entity_type,t.url
            """,
            (run_id, run_id),
        )
    ]


def measure_runtime_storage(
    state_path: Path,
    snapshot_root: Path,
) -> dict[str, int]:
    state_bytes = 0
    for candidate in (
        state_path,
        Path(str(state_path) + "-wal"),
        Path(str(state_path) + "-shm"),
    ):
        if candidate.exists() and candidate.is_file():
            state_bytes += candidate.stat().st_size
    snapshot_bytes = 0
    if snapshot_root.exists():
        snapshot_bytes = sum(
            path.stat().st_size
            for path in snapshot_root.rglob("*")
            if path.is_file()
        )
    return {
        "state_bytes": state_bytes,
        "snapshot_bytes": snapshot_bytes,
        "combined_bytes": state_bytes + snapshot_bytes,
    }


def _known_identity_exception_reason(
    detail: Mapping[str, Any],
) -> str | None:
    policy = KNOWN_IDENTITY_EXCEPTIONS.get(str(detail.get("url") or ""))
    if not policy:
        return None
    if detail.get("identity_status") != policy["identity_status"]:
        return None
    if detail.get("parse_status") != policy["parse_status"]:
        return None
    if policy.get("final_url") and detail.get("final_url") != policy["final_url"]:
        return None
    observed_errors = set(detail.get("errors") or [])
    if not set(policy["required_errors"]).issubset(observed_errors):
        return None
    return str(policy["reason"])


def _verified_source_absence_reason(
    detail: Mapping[str, Any],
) -> str | None:
    if detail.get("identity_status") not in {"conflict", "missing"}:
        return None
    if detail.get("parse_status") != "no_content":
        return None
    requested = urllib.parse.urlsplit(str(detail.get("url") or ""))
    final = urllib.parse.urlsplit(str(detail.get("final_url") or ""))
    if (
        requested.scheme != "https"
        or (requested.hostname or "").lower() != ARCHITIZER_HOST
        or final.scheme != "https"
        or (final.hostname or "").lower() != ARCHITIZER_HOST
    ):
        return None
    requested_parts = [part for part in requested.path.split("/") if part]
    if (
        len(requested_parts) != 2
        or requested_parts[0] not in {"projects", "firms"}
        or not requested_parts[1]
    ):
        return None
    expected_final_path = f"/{requested_parts[0]}/"
    if final.path != expected_final_path:
        return None
    if urllib.parse.parse_qs(final.query) != {"notfound": ["1"]}:
        return None
    required_errors = {
        "final_url_slug_mismatch",
        "canonical_slug_mismatch",
    }
    if not required_errors.issubset(set(detail.get("errors") or [])):
        return None
    return "verified Architizer ?notfound=1 terminal source absence"


def _classify_expected_identity_exception(
    detail: Mapping[str, Any],
) -> tuple[str | None, str | None]:
    known_reason = _known_identity_exception_reason(detail)
    if known_reason:
        return "known_identity_redirect", known_reason
    absence_reason = _verified_source_absence_reason(detail)
    if absence_reason:
        return "verified_source_absence", absence_reason
    return None, None


def _run_scoped_entity_type(
    target_url: str,
    metadata_entity_type: str | None,
) -> str:
    if metadata_entity_type in {"project", "firm"}:
        return metadata_entity_type
    path_parts = [
        part for part in urllib.parse.urlsplit(target_url).path.split("/") if part
    ]
    if len(path_parts) == 2 and path_parts[0] in {"projects", "firms"}:
        return "project" if path_parts[0] == "projects" else "firm"
    raise RecrawlError(
        f"run target has no immutable entity type evidence: {target_url}"
    )


def _network_snapshot_integrity_summary(
    *,
    final_attempts: Mapping[str, Mapping[str, Any]],
    versions: Sequence[Mapping[str, Any]],
    snapshot_root: Path,
) -> tuple[dict[str, Any], list[int]]:
    """Read and attest every final content snapshot with bounded memory."""

    version_snapshot_sha = {
        str(row["target_url"]): str(row["snapshot_sha256"]).upper()
        for row in versions
    }
    evidence_digest = hashlib.sha256()
    content_digest = hashlib.sha256()
    gzip_digest = hashlib.sha256()
    path_digest = hashlib.sha256()
    compressed_sizes: list[int] = []
    total_response_bytes = 0
    total_gzip_bytes = 0
    validated_count = 0

    def update_manifest(digest: Any, payload: Mapping[str, Any]) -> None:
        digest.update(canonical_json(dict(payload)).encode("utf-8"))
        digest.update(b"\n")

    for target_url in sorted(final_attempts):
        row = final_attempts[target_url]
        content_sha = row.get("sha256")
        relative_path = row.get("gzip_path")
        response_bytes = row.get("response_bytes")
        has_snapshot_evidence = bool(content_sha or relative_path)
        if not has_snapshot_evidence:
            if response_bytes:
                raise RecrawlError(
                    "HTTP attempt body has no snapshot evidence: "
                    f"{target_url}"
                )
            continue
        if not content_sha or not relative_path:
            raise RecrawlError(
                f"HTTP attempt has incomplete snapshot evidence: {target_url}"
            )
        normalized_path = _validate_content_addressed_snapshot_path(
            str(relative_path),
            content_sha256=str(content_sha),
            expected_kind="pages",
        )
        path = _safe_snapshot_path(snapshot_root, normalized_path)
        stat_before = path.stat()
        _, actual_gzip_sha = _read_verified_snapshot(
            snapshot_root,
            relative_path=normalized_path,
            content_sha256=str(content_sha),
            response_bytes=response_bytes,
            expected_kind="pages",
        )
        stat_after = path.stat()
        if (
            stat_before.st_size != stat_after.st_size
            or stat_before.st_mtime_ns != stat_after.st_mtime_ns
        ):
            raise RecrawlError(
                f"snapshot gzip changed during verification: {normalized_path}"
            )
        metadata_sha = version_snapshot_sha.get(target_url)
        if metadata_sha is not None and metadata_sha != str(content_sha).upper():
            raise RecrawlError(
                "final HTTP snapshot disagrees with run metadata: "
                f"{target_url}"
            )
        evidence = {
            "target_url": target_url,
            "http_attempt_id": int(row["id"]),
            "content_sha256": str(content_sha).upper(),
            "gzip_sha256": actual_gzip_sha,
            "gzip_path": normalized_path,
            "response_bytes": int(response_bytes),
            "gzip_bytes": int(stat_after.st_size),
        }
        update_manifest(evidence_digest, evidence)
        update_manifest(
            content_digest,
            {
                "target_url": target_url,
                "content_sha256": evidence["content_sha256"],
            },
        )
        update_manifest(
            gzip_digest,
            {
                "target_url": target_url,
                "gzip_sha256": actual_gzip_sha,
            },
        )
        update_manifest(
            path_digest,
            {
                "target_url": target_url,
                "gzip_path": normalized_path,
            },
        )
        compressed_sizes.append(int(stat_after.st_size))
        total_response_bytes += int(response_bytes)
        total_gzip_bytes += int(stat_after.st_size)
        validated_count += 1

    return (
        {
            "policy_version": NETWORK_SNAPSHOT_INTEGRITY_POLICY_VERSION,
            "validated_count": validated_count,
            "evidence_manifest_sha256": evidence_digest.hexdigest().upper(),
            "content_sha256_manifest_sha256": (
                content_digest.hexdigest().upper()
            ),
            "gzip_sha256_manifest_sha256": gzip_digest.hexdigest().upper(),
            "gzip_path_manifest_sha256": path_digest.hexdigest().upper(),
            "total_response_bytes": total_response_bytes,
            "total_gzip_bytes": total_gzip_bytes,
            "manifest_record_format": (
                "canonical-json-utf8-lines; target_url ascending"
            ),
        },
        compressed_sizes,
    )


def _network_run_summary(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    source_sha_before: str,
    source_sha_after: str,
    delay_seconds: float,
    state_path: Path,
    snapshot_root: Path,
    storage_before: Mapping[str, int],
    elapsed_seconds: float,
) -> dict[str, Any]:
    selected = int(
        connection.execute(
            "SELECT COUNT(*) FROM run_targets WHERE run_id=?", (run_id,)
        ).fetchone()[0]
    )
    attempts = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM http_attempts WHERE run_id=? ORDER BY id",
            (run_id,),
        )
    ]
    versions = [
        dict(row)
        for row in connection.execute(
            """
            SELECT m.*
            FROM run_metadata_versions rm
            JOIN metadata_versions m ON m.id=rm.version_id
            WHERE rm.run_id=?
            ORDER BY m.id
            """,
            (run_id,),
        )
    ]
    run_targets = [
        dict(row)
        for row in connection.execute(
            """
            SELECT rt.*,m.entity_type AS run_entity_type,
                   m.parse_status AS run_parse_status
            FROM run_targets rt
            LEFT JOIN run_metadata_versions rm
              ON rm.run_id=rt.run_id AND rm.target_url=rt.url
            LEFT JOIN metadata_versions m ON m.id=rm.version_id
            WHERE rt.run_id=? ORDER BY rt.selection_order
            """,
            (run_id,),
        )
    ]
    for row in run_targets:
        row["entity_type"] = _run_scoped_entity_type(
            str(row["url"]),
            row.get("run_entity_type"),
        )
    final_attempts: dict[str, dict[str, Any]] = {}
    for row in attempts:
        if row["target_url"]:
            final_attempts[row["target_url"]] = row
    identity_counts = Counter(row["identity_status"] for row in versions)
    parse_counts = Counter(row["parse_status"] for row in versions)
    selected_entity_counts = Counter(row["entity_type"] for row in run_targets)
    type_stats: dict[str, Counter[str]] = defaultdict(Counter)
    for row in run_targets:
        type_stats[row["selected_reason"]]["selected"] += 1
        final = final_attempts.get(row["url"])
        if final and final["outcome"] == "success":
            type_stats[row["selected_reason"]]["http_success"] += 1
        if row["run_parse_status"] in {"complete", "partial", "conflict"}:
            type_stats[row["selected_reason"]]["parse_success"] += 1
        else:
            type_stats[row["selected_reason"]]["failure"] += 1
    durations = [row["duration_ms"] for row in attempts]
    final_response_bytes = [
        row["response_bytes"] for row in final_attempts.values() if row["sha256"]
    ]
    snapshot_integrity, compressed_sizes = (
        _network_snapshot_integrity_summary(
            final_attempts=final_attempts,
            versions=versions,
            snapshot_root=snapshot_root,
        )
    )
    field_coverage: dict[str, int] = Counter()
    if versions:
        for row in connection.execute(
            """
            SELECT rf.field_name,COUNT(*) AS n
            FROM resolved_fields rf
            JOIN run_metadata_versions rm ON rm.version_id=rf.version_id
            JOIN metadata_versions m ON m.id=rf.version_id
            WHERE rm.run_id=?
              AND m.identity_status='valid'
              AND rf.value_json IS NOT NULL
              AND rf.status IN ('confirmed','single_source')
            GROUP BY rf.field_name
            """,
            (run_id,),
        ):
            field_coverage[row["field_name"]] = row["n"]
        comparison_counts = {
            row["comparison_status"]: row["n"]
            for row in connection.execute(
                """
                SELECT lc.comparison_status,COUNT(*) AS n
                FROM legacy_field_comparisons lc
                JOIN run_metadata_versions rm ON rm.version_id=lc.version_id
                WHERE rm.run_id=?
                GROUP BY lc.comparison_status
                """,
                (run_id,),
            )
        }
    else:
        comparison_counts = {}
    signal_counts: Counter[str] = Counter()
    for row in attempts:
        for signal in json.loads(row["block_signals_json"]):
            signal_counts[signal] += 1
    successful_http = sum(
        row["outcome"] == "success" for row in final_attempts.values()
    )
    snapshots_saved = int(snapshot_integrity["validated_count"])
    valid_identity = identity_counts.get("valid", 0)
    average_response_bytes = (
        round(statistics.mean(final_response_bytes))
        if final_response_bytes
        else 0
    )
    average_compressed_bytes = (
        round(statistics.mean(compressed_sizes)) if compressed_sizes else 0
    )
    if signal_counts:
        recommended_delay = max(4.0, delay_seconds * 2)
        delay_reason = "block/rate/login signal observed; increase delay"
    else:
        recommended_delay = max(2.0, delay_seconds)
        delay_reason = "no block signal; retain conservative 2s minimum"
    field_denominators: dict[str, int] = {}
    all_fields = set(PROJECT_FIELDS) | set(FIRM_FIELDS)
    for field_name in all_fields:
        in_project = field_name in PROJECT_FIELDS
        in_firm = field_name in FIRM_FIELDS
        if in_project and in_firm:
            denominator = selected
        elif in_project:
            denominator = selected_entity_counts["project"]
        else:
            denominator = selected_entity_counts["firm"]
        field_denominators[field_name] = denominator
    storage_after = measure_runtime_storage(state_path, snapshot_root)
    storage_delta = {
        key: storage_after[key] - int(storage_before.get(key, 0))
        for key in ("state_bytes", "snapshot_bytes", "combined_bytes")
    }
    observed_delta_per_target = (
        storage_delta["combined_bytes"] / selected if selected else 0.0
    )
    identity_exception_details: list[dict[str, Any]] = []
    parse_exception_details: list[dict[str, Any]] = []
    for version in versions:
        identity_payload = json.loads(version["identity_json"] or "{}")
        if version["identity_status"] != "valid":
            detail = {
                "url": version["target_url"],
                "final_url": (
                    final_attempts.get(version["target_url"], {}).get(
                        "final_url"
                    )
                ),
                "identity_status": version["identity_status"],
                "parse_status": version["parse_status"],
                "errors": identity_payload.get("errors", []),
                "missing": identity_payload.get("missing", []),
            }
            exception_kind, exception_reason = (
                _classify_expected_identity_exception(detail)
            )
            detail["expected_exception_kind"] = exception_kind
            detail["known_exception_reason"] = exception_reason
            identity_exception_details.append(detail)
        if version["parse_status"] == "no_content":
            parse_exception_details.append(
                {
                    "url": version["target_url"],
                    "final_url": (
                        final_attempts.get(version["target_url"], {}).get(
                            "final_url"
                        )
                    ),
                    "identity_status": version["identity_status"],
                    "known_exception_reason": None,
                }
            )
    return {
        "run_id": run_id,
        "selected": selected,
        "physical_request_attempts": len(attempts),
        "metadata_version_count": len(versions),
        "http_success": successful_http,
        "http_success_rate": successful_http / selected if selected else 0.0,
        "snapshot_saved": snapshots_saved,
        "snapshot_integrity": snapshot_integrity,
        "identity_valid": valid_identity,
        "identity_valid_rate": valid_identity / selected if selected else 0.0,
        "identity_status_counts": dict(sorted(identity_counts.items())),
        "identity_exception_details": identity_exception_details,
        "parse_status_counts": dict(sorted(parse_counts.items())),
        "parse_exception_details": parse_exception_details,
        "type_stats": {
            key: dict(sorted(value.items()))
            for key, value in sorted(type_stats.items())
        },
        "field_coverage_counts": dict(sorted(field_coverage.items())),
        "field_coverage_denominators": dict(sorted(field_denominators.items())),
        "field_coverage_rates": {
            key: value / field_denominators.get(key, selected)
            if field_denominators.get(key, selected)
            else 0.0
            for key, value in sorted(field_coverage.items())
        },
        "legacy_field_comparison_counts": comparison_counts,
        "block_signal_counts": dict(sorted(signal_counts.items())),
        "duration_ms": {
            "mean": round(statistics.mean(durations), 1) if durations else 0,
            "median": round(statistics.median(durations), 1) if durations else 0,
            "max": max(durations, default=0),
        },
        "run_elapsed_seconds": round(elapsed_seconds, 3),
        "average_response_bytes": average_response_bytes,
        "average_snapshot_gzip_bytes": average_compressed_bytes,
        "runtime_storage": {
            "before": dict(storage_before),
            "after": storage_after,
            "delta": storage_delta,
            "observed_combined_delta_per_target": round(
                observed_delta_per_target, 1
            ),
            "estimate_low_bytes_per_target": max(
                0, round(observed_delta_per_target)
            ),
            "estimate_high_bytes_per_target": max(
                0, round(observed_delta_per_target * 2.0)
            ),
            "high_factor_reason": (
                "2x safety factor for retries, SQLite page/WAL allocation, "
                "and response-size variance"
            ),
        },
        "request_delay_seconds": delay_seconds,
        "recommended_request_delay_seconds": recommended_delay,
        "recommended_delay_reason": delay_reason,
        "input_db_sha256_before": source_sha_before,
        "input_db_sha256_after": source_sha_after,
        "input_db_unchanged": source_sha_before == source_sha_after,
        "no_content_rate": parse_counts.get("no_content", 0) / selected
        if selected
        else 0.0,
        "partial_rate": parse_counts.get("partial", 0) / selected
        if selected
        else 0.0,
        "conflict_rate": parse_counts.get("conflict", 0) / selected
        if selected
        else 0.0,
    }


def evaluate_smoke_quality(
    summary: Mapping[str, Any],
    *,
    smoke_size: int,
) -> dict[str, Any]:
    """Attach a versioned, machine-enforced quality verdict to a smoke."""

    if smoke_size not in {10, 100}:
        raise ValueError("smoke_size must be 10 or 100")
    evaluated = dict(summary)
    failures: list[str] = []
    required_types = {
        "sitemap_new_project",
        "sitemap_modified_project",
        "legacy_recovery",
        "firm_stub",
        "award_seed",
        "unchanged",
    }
    selected = int(summary.get("selected") or 0)
    if selected != smoke_size:
        failures.append(f"selected={selected}, expected={smoke_size}")
    if int(summary.get("http_success") or 0) != smoke_size:
        failures.append("not every selected URL had a successful final HTTP attempt")
    if int(summary.get("snapshot_saved") or 0) != smoke_size:
        failures.append("not every selected URL produced a content snapshot")
    if int(summary.get("metadata_version_count") or 0) != smoke_size:
        failures.append("not every selected URL produced a metadata version")
    if not summary.get("input_db_unchanged"):
        failures.append("legacy input DB immutability was not proven")
    block_counts = summary.get("block_signal_counts") or {}
    if any(int(value) for value in block_counts.values()):
        failures.append("block/login/rate-limit signal observed")
    type_stats = summary.get("type_stats") or {}
    missing_types = sorted(
        name
        for name in required_types
        if int((type_stats.get(name) or {}).get("selected") or 0) < 1
    )
    if missing_types:
        failures.append(
            "missing required stratified target types: " + ",".join(missing_types)
        )
    identity_details: list[dict[str, Any]] = []
    for raw_detail in summary.get("identity_exception_details") or []:
        detail = dict(raw_detail)
        exception_kind, exception_reason = (
            _classify_expected_identity_exception(detail)
        )
        detail["expected_exception_kind"] = exception_kind
        detail["known_exception_reason"] = exception_reason
        identity_details.append(detail)
    unexpected_identity = [
        item for item in identity_details if not item.get("known_exception_reason")
    ]
    if unexpected_identity:
        failures.append(
            f"unexpected identity exceptions={len(unexpected_identity)}"
        )
    expected_identity = [
        item for item in identity_details if item.get("known_exception_reason")
    ]
    verified_source_absences = [
        item
        for item in expected_identity
        if item.get("expected_exception_kind") == "verified_source_absence"
    ]
    max_verified_source_absences = smoke_size // 20
    if len(verified_source_absences) > max_verified_source_absences:
        failures.append(
            "verified source absences exceed 5% smoke allowance: "
            f"{len(verified_source_absences)}>{max_verified_source_absences}"
        )
    valid_identity = int(summary.get("identity_valid") or 0)
    if valid_identity + len(expected_identity) != smoke_size:
        failures.append(
            "valid plus explicitly-known identity outcomes do not cover selection"
        )
    parse_counts = summary.get("parse_status_counts") or {}
    usable_parse = sum(
        int(parse_counts.get(name) or 0)
        for name in ("complete", "partial", "conflict")
    )
    expected_identity_by_url = {
        str(item["url"]): str(item["known_exception_reason"])
        for item in expected_identity
        if item.get("url") and item.get("known_exception_reason")
    }
    parse_exceptions: list[dict[str, Any]] = []
    for raw_detail in summary.get("parse_exception_details") or []:
        detail = dict(raw_detail)
        detail["known_exception_reason"] = (
            expected_identity_by_url[str(detail["url"])]
            if str(detail.get("url") or "") in expected_identity_by_url
            else None
        )
        parse_exceptions.append(detail)
    unexpected_no_content = [
        item for item in parse_exceptions
        if not item.get("known_exception_reason")
    ]
    if unexpected_no_content:
        failures.append(
            f"unexpected no_content outcomes={len(unexpected_no_content)}"
        )
    expected_no_content = [
        item for item in parse_exceptions if item.get("known_exception_reason")
    ]
    if usable_parse + len(expected_no_content) != smoke_size:
        failures.append(
            "usable plus explicitly-known parse outcomes do not cover selection"
        )
    coverage = summary.get("field_coverage_rates") or {}
    for field_name, threshold in (("name", 0.95), ("slug", 0.95)):
        rate = float(coverage.get(field_name) or 0.0)
        if rate < threshold:
            failures.append(
                f"{field_name} coverage={rate:.3f}, required>={threshold:.3f}"
            )
    evaluated.update(
        {
            "gate_policy_version": SMOKE_GATE_POLICY_VERSION,
            "gate_thresholds": {
                "http_success": smoke_size,
                "snapshot_saved": smoke_size,
                "metadata_version_count": smoke_size,
                "required_selection_types": sorted(required_types),
                "unexpected_identity_exceptions": 0,
                "unexpected_no_content": 0,
                "verified_source_absence_max": (
                    max_verified_source_absences
                ),
                "block_signal_count": 0,
                "name_coverage_min": 0.95,
                "slug_coverage_min": 0.95,
            },
            "known_identity_exceptions_observed": [
                item
                for item in expected_identity
                if item.get("expected_exception_kind")
                == "known_identity_redirect"
            ],
            "verified_source_absences_observed": verified_source_absences,
            "identity_exception_details": identity_details,
            "parse_exception_details": parse_exceptions,
            "gate_failures": failures,
            "gate_passed": not failures,
        }
    )
    return evaluated


def _latest_completed_census(
    connection: sqlite3.Connection,
    *,
    source_sha256: str,
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT *
        FROM runs
        WHERE run_kind='sitemap_census'
          AND status='completed'
          AND source_db_sha256_before=?
          AND source_db_sha256_after=?
        ORDER BY id DESC LIMIT 1
        """,
        (source_sha256, source_sha256),
    ).fetchone()
    if row is None:
        raise RecrawlError(
            "no completed sitemap census for the bound source SHA"
        )
    return row


def _enrich_smoke_summary_evidence(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    summary: Mapping[str, Any],
) -> dict[str, Any]:
    enriched = dict(summary)
    final_urls: dict[str, str | None] = {}
    for row in connection.execute(
        """
        SELECT target_url,final_url
        FROM http_attempts
        WHERE run_id=? AND target_url IS NOT NULL
        ORDER BY id
        """,
        (run_id,),
    ):
        final_urls[row["target_url"]] = row["final_url"]
    for key in ("identity_exception_details", "parse_exception_details"):
        details: list[dict[str, Any]] = []
        for raw_detail in summary.get(key) or []:
            detail = dict(raw_detail)
            detail["final_url"] = final_urls.get(str(detail.get("url") or ""))
            details.append(detail)
        enriched[key] = details
    return enriched


def _completed_smoke_after(
    connection: sqlite3.Connection,
    *,
    run_kind: str,
    minimum_run_id: int,
    source_sha256: str,
    selected_count: int,
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT *
        FROM runs
        WHERE run_kind=?
          AND status IN ('completed','quality_failed')
          AND id>?
          AND parser_version=?
          AND source_db_sha256_before=?
          AND source_db_sha256_after=?
          AND selected_count=?
        ORDER BY id DESC LIMIT 1
        """,
        (
            run_kind,
            minimum_run_id,
            PARSER_VERSION,
            source_sha256,
            source_sha256,
            selected_count,
        ),
    ).fetchone()
    if row is None:
        raise RecrawlError(
            f"required completed {run_kind} for current census/source/parser "
            "was not found"
        )
    summary = json.loads(row["summary_json"] or "{}")
    summary = _enrich_smoke_summary_evidence(
        connection,
        run_id=int(row["id"]),
        summary=summary,
    )
    if not summary.get("input_db_unchanged"):
        raise RecrawlError(f"{run_kind} did not prove input DB immutability")
    if summary.get("gate_policy_version") != SMOKE_GATE_POLICY_VERSION:
        raise RecrawlError(
            f"{run_kind} does not use the current smoke quality gate policy"
        )
    recomputed = evaluate_smoke_quality(
        summary,
        smoke_size=selected_count,
    )
    if not recomputed["gate_passed"]:
        raise RecrawlError(
            f"{run_kind} did not pass smoke quality gates: "
            f"{recomputed['gate_failures']}"
        )
    return row


def validate_smoke_ladder_for_n100(
    connection: sqlite3.Connection,
    *,
    census_run_id: int,
    source_sha256: str,
) -> sqlite3.Row:
    return _completed_smoke_after(
        connection,
        run_kind="network_smoke_n10",
        minimum_run_id=census_run_id,
        source_sha256=source_sha256,
        selected_count=10,
    )


def validate_full_ladder(
    connection: sqlite3.Connection,
    *,
    source_sha256: str,
) -> dict[str, int]:
    census = _latest_completed_census(
        connection,
        source_sha256=source_sha256,
    )
    n10 = validate_smoke_ladder_for_n100(
        connection,
        census_run_id=int(census["id"]),
        source_sha256=source_sha256,
    )
    n100 = _completed_smoke_after(
        connection,
        run_kind="network_smoke_n100",
        minimum_run_id=max(int(census["id"]), int(n10["id"])),
        source_sha256=source_sha256,
        selected_count=100,
    )
    return {
        "census_run_id": int(census["id"]),
        "n10_run_id": int(n10["id"]),
        "n100_run_id": int(n100["id"]),
    }


def run_network_smoke(
    *,
    smoke_size: int,
    source_path: Path = DEFAULT_SOURCE_DB,
    state_path: Path = DEFAULT_STATE_DB,
    snapshot_root: Path = DEFAULT_SNAPSHOT_DIR,
    delay_seconds: float = 2.0,
    timeout_seconds: float = 30.0,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Run an N10 or N100 stratified network smoke against sidecar targets."""

    if smoke_size not in {10, 100}:
        raise ValueError("smoke_size must be 10 or 100")
    run_kind = f"network_smoke_n{smoke_size}"
    source_path = source_path.resolve()
    state_path = state_path.resolve()
    snapshot_root = snapshot_root.resolve()
    validate_runtime_paths(
        source_path=source_path,
        state_path=state_path,
        snapshot_root=snapshot_root,
    )
    source_sha = sha256_file(source_path)
    source_size = source_path.stat().st_size
    arguments = {
        "smoke_size": smoke_size,
        "delay_seconds": delay_seconds,
        "timeout_seconds": timeout_seconds,
        "max_attempts": max_attempts,
    }
    run_start = time.monotonic()
    with SidecarLock(state_path):
        state = connect_state(
            state_path,
            source_path=source_path,
            source_sha256=source_sha,
            source_size=source_size,
        )
        recover_interrupted_state(state)
        latest_census = _latest_completed_census(
            state,
            source_sha256=source_sha,
        )
        if smoke_size == 100:
            validate_smoke_ladder_for_n100(
                state,
                census_run_id=int(latest_census["id"]),
                source_sha256=source_sha,
            )
        arguments["census_run_id"] = int(latest_census["id"])
        storage_before = measure_runtime_storage(state_path, snapshot_root)
        run_id = start_run(
            state,
            run_kind=run_kind,
            source_path=source_path,
            source_sha256=source_sha,
            source_size=source_size,
            arguments=arguments,
        )
        try:
            selected = select_network_targets(
                state,
                smoke_size=smoke_size,
                run_kind=run_kind,
            )
            for ordinal, target in enumerate(selected, start=1):
                state.execute(
                    """
                    INSERT INTO run_targets(
                        run_id,url,selection_order,selected_reason,status_before
                    ) VALUES (?,?,?,?,?)
                    """,
                    (
                        run_id,
                        target["url"],
                        ordinal,
                        target["selected_reason"],
                        target["status"],
                    ),
                )
            state.execute(
                "UPDATE runs SET selected_count=? WHERE id=?",
                (len(selected), run_id),
            )
            state.commit()
            legacy_connection = open_legacy_readonly(source_path)
            client = PoliteHttpClient(
                delay_seconds=delay_seconds,
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
                jitter_seed=f"{run_kind}:{run_id}",
                allowed_hosts={ARCHITIZER_HOST, f"www.{ARCHITIZER_HOST}"},
            )
            circuit_open = False
            try:
                for target in selected:
                    now = utc_now()
                    state.execute(
                        """
                        UPDATE targets
                        SET status='in_progress',last_attempt_at=?,updated_at=?
                        WHERE url=?
                        """,
                        (now, now, target["url"]),
                    )
                    state.commit()
                    try:
                        result = client.fetch(target["url"])
                    except CircuitOpenError:
                        circuit_open = True
                        state.execute(
                            """
                            UPDATE targets
                            SET status='pending',updated_at=? WHERE url=?
                            """,
                            (utc_now(), target["url"]),
                        )
                        state.commit()
                        break
                    records = _record_http_attempts(
                        state,
                        run_id=run_id,
                        result=result,
                        request_kind=f"{target['entity_type']}_page",
                        target_url=target["url"],
                        snapshot_root=snapshot_root,
                        snapshot_kind="pages",
                        extension="html",
                    )
                    attempt = result.final
                    final_record = records[-1]
                    parse_status = "no_content"
                    target_status = "failed"
                    retryable = int(
                        attempt.retryable
                        or attempt.outcome == "blocked"
                        or any("login" in item for item in attempt.block_signals)
                    )
                    last_error = attempt.error
                    version_id: int | None = None
                    if (
                        attempt.outcome == "success"
                        and attempt.http_status == 200
                        and attempt.body
                        and final_record["sha256"]
                    ):
                        parsed = parse_entity_page(
                            attempt.body,
                            requested_url=target["url"],
                            final_url=attempt.final_url or target["url"],
                            http_status=attempt.http_status,
                            content_type=attempt.content_type,
                            entity_type=target["entity_type"],
                        )
                        parse_status = parsed["parse_status"]
                        version_id = _store_parse_result(
                            state,
                            run_id=run_id,
                            target=target,
                            snapshot_sha=final_record["sha256"],
                            parsed=parsed,
                            legacy_connection=legacy_connection,
                        )
                        _schedule_discovered_relationships(
                            state,
                            version_id=version_id,
                            legacy_connection=legacy_connection,
                        )
                        if parsed["identity"]["status"] == "valid":
                            target_status = "done"
                            retryable = 0
                            last_error = None
                        else:
                            target_status = "failed"
                            retryable = int(
                                parsed["page_classification"]["classification"]
                                in {"block", "login"}
                            )
                            last_error = ";".join(
                                parsed["identity"]["errors"]
                                + parsed["identity"]["missing"]
                            )
                    state.execute(
                        """
                        UPDATE targets
                        SET status=?,retryable=?,
                            attempt_count=attempt_count+?,
                            last_attempt_at=?,last_error=?,last_http_status=?,
                            last_snapshot_sha256=?,last_parse_status=?,updated_at=?
                        WHERE url=?
                        """,
                        (
                            target_status,
                            retryable,
                            len(result.attempts),
                            utc_now(),
                            last_error,
                            attempt.http_status,
                            final_record["sha256"],
                            parse_status,
                            utc_now(),
                            target["url"],
                        ),
                    )
                    state.execute(
                        """
                        UPDATE run_targets SET status_after=?
                        WHERE run_id=? AND url=?
                        """,
                        (target_status, run_id, target["url"]),
                    )
                    state.commit()
            finally:
                legacy_connection.close()
            source_sha_after = sha256_file(source_path)
            if source_sha_after != source_sha:
                raise RecrawlError("legacy source DB changed during network smoke")
            summary = _network_run_summary(
                state,
                run_id=run_id,
                source_sha_before=source_sha,
                source_sha_after=source_sha_after,
                delay_seconds=delay_seconds,
                state_path=state_path,
                snapshot_root=snapshot_root,
                storage_before=storage_before,
                elapsed_seconds=time.monotonic() - run_start,
            )
            summary = evaluate_smoke_quality(summary, smoke_size=smoke_size)
            status = (
                "circuit_open"
                if circuit_open
                else "completed"
                if summary["gate_passed"]
                else "quality_failed"
            )
            finish_run(
                state,
                run_id,
                status=status,
                source_sha256_after=source_sha_after,
                summary=summary,
                selected_count=len(selected),
            )
            if circuit_open:
                raise CircuitOpenError(
                    "network smoke stopped by circuit breaker; see sidecar run"
                )
            if not summary["gate_passed"]:
                raise RecrawlError(
                    "network smoke failed quality gates: "
                    + "; ".join(summary["gate_failures"])
                )
            return summary
        except Exception as exc:
            source_sha_after = sha256_file(source_path)
            existing = state.execute(
                "SELECT status FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            if existing and existing["status"] == "running":
                finish_run(
                    state,
                    run_id,
                    status="failed",
                    source_sha256_after=source_sha_after,
                    summary={},
                    error=f"{type(exc).__name__}: {exc}",
                )
            raise
        finally:
            state.close()


def _scan_links(body: bytes, content_type: str | None = None) -> list[str]:
    charset = "utf-8"
    if content_type:
        match = re.search(r"charset=([^;\s]+)", content_type, flags=re.I)
        if match:
            charset = match.group(1).strip("\"'")
    try:
        page = body.decode(charset, errors="replace")
    except LookupError:
        page = body.decode("utf-8", errors="replace")
    scanner = _ArchitizerHTMLParser()
    scanner.feed(page)
    scanner.close()
    return [link["href"] for link in scanner.links if link.get("href")]


def run_award_seed_census(
    *,
    award_year: int,
    source_path: Path = DEFAULT_SOURCE_DB,
    state_path: Path = DEFAULT_STATE_DB,
    snapshot_root: Path = DEFAULT_SNAPSHOT_DIR,
    delay_seconds: float = 2.0,
    timeout_seconds: float = 30.0,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Discover current award tracks and direct Architizer entity links.

    This is intentionally separate from the sitemap-only source census.  It
    does not infer track names: only exact ``/{year}/{track}/`` links found on
    the official year root are fetched.
    """

    if award_year < 2013 or award_year > datetime.now().year + 1:
        raise ValueError(f"implausible award year: {award_year}")
    source_path = source_path.resolve()
    state_path = state_path.resolve()
    snapshot_root = snapshot_root.resolve()
    validate_runtime_paths(
        source_path=source_path,
        state_path=state_path,
        snapshot_root=snapshot_root,
    )
    source_sha = sha256_file(source_path)
    source_size = source_path.stat().st_size
    root_url = f"https://{WINNERS_HOST}/{award_year}/"
    arguments = {
        "award_year": award_year,
        "root_url": root_url,
        "delay_seconds": delay_seconds,
        "timeout_seconds": timeout_seconds,
        "max_attempts": max_attempts,
    }
    with SidecarLock(state_path):
        state = connect_state(
            state_path,
            source_path=source_path,
            source_sha256=source_sha,
            source_size=source_size,
        )
        recover_interrupted_state(state)
        run_id = start_run(
            state,
            run_kind=f"award_seed_census_{award_year}",
            source_path=source_path,
            source_sha256=source_sha,
            source_size=source_size,
            arguments=arguments,
        )
        try:
            client = PoliteHttpClient(
                delay_seconds=delay_seconds,
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
                jitter_seed=f"award-census:{award_year}:{run_id}",
                allowed_hosts={WINNERS_HOST, f"www.{WINNERS_HOST}"},
            )

            def fetch_award_page(url: str, request_kind: str) -> HttpAttempt:
                result = client.fetch(url)
                _record_http_attempts(
                    state,
                    run_id=run_id,
                    result=result,
                    request_kind=request_kind,
                    target_url=None,
                    snapshot_root=snapshot_root,
                    snapshot_kind="awards",
                    extension="html",
                )
                attempt = result.final
                if attempt.outcome != "success" or attempt.http_status != 200:
                    raise RecrawlError(
                        f"award census fetch failed: {url}: {attempt.error}"
                    )
                return attempt

            root_attempt = fetch_award_page(root_url, "award_year_root")
            track_urls: dict[str, str] = {}
            for href in _scan_links(
                root_attempt.body, root_attempt.content_type
            ):
                absolute = urllib.parse.urljoin(root_url, href)
                parsed = urllib.parse.urlsplit(absolute)
                if (parsed.hostname or "").lower() != WINNERS_HOST:
                    continue
                parts = [
                    urllib.parse.unquote(part)
                    for part in parsed.path.split("/")
                    if part
                ]
                if len(parts) != 2 or parts[0] != str(award_year):
                    continue
                track = parts[1]
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{1,50}", track):
                    continue
                track_urls.setdefault(
                    track,
                    f"https://{WINNERS_HOST}/{award_year}/{track}/",
                )
            if not track_urls:
                raise RecrawlError(
                    f"no official award track links found on {root_url}"
                )
            entity_sets: dict[str, set[str]] = {
                "project": set(),
                "firm": set(),
            }
            track_entity_sets: dict[str, dict[str, set[str]]] = defaultdict(
                lambda: {"project": set(), "firm": set()}
            )
            for track, track_url in sorted(track_urls.items()):
                track_entity_sets[track]
                attempt = fetch_award_page(track_url, "award_track_root")
                for href in _scan_links(attempt.body, attempt.content_type):
                    absolute = urllib.parse.urljoin(track_url, href)
                    parsed = urllib.parse.urlsplit(absolute)
                    host = (parsed.hostname or "").lower()
                    if host == f"www.{ARCHITIZER_HOST}":
                        host = ARCHITIZER_HOST
                    if host != ARCHITIZER_HOST:
                        continue
                    for entity_type in ("project", "firm"):
                        slug = slug_from_url(absolute, entity_type)
                        if not slug:
                            continue
                        entity_url = normalize_entity_url(absolute, entity_type)
                        state.execute(
                            """
                            INSERT OR IGNORE INTO award_discoveries(
                                run_id,award_year,award_track,entity_type,slug,
                                source_url,discovered_url,discovered_at
                            ) VALUES (?,?,?,?,?,?,?,?)
                            """,
                            (
                                run_id,
                                award_year,
                                track,
                                entity_type,
                                slug,
                                track_url,
                                entity_url,
                                utc_now(),
                            ),
                        )
                        entity_sets[entity_type].add(entity_url)
                        track_entity_sets[track][entity_type].add(entity_url)
                        upsert_target(
                            state,
                            url=entity_url,
                            entity_type=entity_type,
                            source_lastmod=None,
                            priority=45,
                            reason=f"award_{award_year}_{entity_type}_seed",
                            discovery_source=track_url,
                            input_lineage={
                                "award_year": award_year,
                                "award_track": track,
                                "source_url": track_url,
                            },
                        )
            state.commit()
            source_sha_after = sha256_file(source_path)
            if source_sha_after != source_sha:
                raise RecrawlError(
                    "legacy source DB changed during award seed census"
                )
            summary = {
                "run_id": run_id,
                "award_year": award_year,
                "official_root": root_url,
                "tracks": sorted(track_urls),
                "track_urls": dict(sorted(track_urls.items())),
                "track_direct_link_counts": {
                    track: {
                        entity_type: len(urls)
                        for entity_type, urls in sorted(entity_sets_for_track.items())
                        if urls
                    }
                    for track, entity_sets_for_track in sorted(
                        track_entity_sets.items()
                    )
                },
                "distinct_project_seed_urls": len(entity_sets["project"]),
                "distinct_firm_seed_urls": len(entity_sets["firm"]),
                "input_db_sha256_before": source_sha,
                "input_db_sha256_after": source_sha_after,
                "input_db_unchanged": source_sha == source_sha_after,
            }
            finish_run(
                state,
                run_id,
                status="completed",
                source_sha256_after=source_sha_after,
                summary=summary,
            )
            return summary
        except Exception as exc:
            source_sha_after = sha256_file(source_path)
            finish_run(
                state,
                run_id,
                status="failed",
                source_sha256_after=source_sha_after,
                summary={},
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        finally:
            state.close()


def _target_network_state(target: Mapping[str, Any]) -> dict[str, Any]:
    return {field: target[field] for field in TARGET_NETWORK_STATE_FIELDS}


def select_snapshot_reparse_targets(
    connection: sqlite3.Connection,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Freeze firm upgrades plus exact parser-regression project recoveries."""

    firms = [
        dict(row)
        for row in connection.execute(
            """
            SELECT t.*,m.id AS source_metadata_version_id,
                   m.run_id AS source_metadata_run_id,
                   m.snapshot_sha256 AS content_sha256,
                   m.parser_version AS source_parser_version,
                   m.identity_status AS source_identity_status,
                   m.dom_json AS source_dom_json
            FROM targets t
            JOIN metadata_versions m ON m.id=t.last_good_version_id
            JOIN runs r ON r.id=m.run_id
            WHERE t.entity_type='firm'
              AND m.entity_type='firm'
              AND m.identity_status='valid'
              AND m.parser_version<>?
              AND r.status LIKE 'completed%'
            """,
            (PARSER_VERSION,),
        )
    ]
    for row in firms:
        row["selection_kind"] = "firm_last_good_parser_upgrade"
    projects = [
        dict(row)
        for row in connection.execute(
            """
            SELECT t.*,m.id AS source_metadata_version_id,
                   m.run_id AS source_metadata_run_id,
                   m.snapshot_sha256 AS content_sha256,
                   m.parser_version AS source_parser_version,
                   m.identity_status AS source_identity_status,
                   m.dom_json AS source_dom_json
            FROM targets t
            JOIN metadata_versions m ON m.target_url=t.url
            JOIN runs r ON r.id=m.run_id
            WHERE t.entity_type='project'
              AND t.status='failed'
              AND t.retryable=0
              AND t.attempt_count>0
              AND t.last_attempt_at IS NOT NULL
              AND t.last_error IS NOT NULL
              AND t.last_http_status=200
              AND t.last_parse_status='no_content'
              AND m.entity_type='project'
              AND m.identity_status<>'valid'
              AND m.parser_version<>?
              AND m.snapshot_sha256=t.last_snapshot_sha256
              AND r.status LIKE 'completed%'
              AND json_valid(m.dom_json)
              AND json_extract(m.dom_json,'$._canonical_url')=t.url
              AND m.id=(
                  SELECT MAX(m2.id) FROM metadata_versions m2
                  WHERE m2.target_url=t.url AND m2.parser_version<>?
              )
              AND NOT EXISTS (
                  SELECT 1 FROM metadata_versions current
                  WHERE current.target_url=t.url
                    AND current.snapshot_sha256=m.snapshot_sha256
                    AND current.parser_version=?
              )
            """,
            (PARSER_VERSION, PARSER_VERSION, PARSER_VERSION),
        )
    ]
    for row in projects:
        row["selection_kind"] = "project_parser_regression_recovery"
    rows = projects + firms
    rows.sort(
        key=lambda row: (
            int(row["selection_kind"] != "project_parser_regression_recovery"),
            hashlib.sha256(row["url"].encode("utf-8")).hexdigest(),
            row["url"],
        )
    )
    if limit is not None:
        if limit < 0:
            raise ValueError("snapshot reparse limit cannot be negative")
        rows = rows[:limit]
    return rows


def _safe_snapshot_path(snapshot_root: Path, relative_path: str) -> Path:
    if not relative_path or "\x00" in relative_path:
        raise RecrawlError("snapshot gzip path is empty or invalid")
    portable = relative_path.replace("\\", "/")
    if (
        portable.startswith("/")
        or re.match(r"^[A-Za-z]:", portable)
        or ".." in portable.split("/")
    ):
        raise RecrawlError(f"unsafe snapshot gzip path: {relative_path}")
    try:
        root = snapshot_root.resolve(strict=True)
    except FileNotFoundError as exc:
        raise RecrawlError(
            f"snapshot root not found: {snapshot_root.resolve()}"
        ) from exc
    if not root.is_dir():
        raise RecrawlError(f"snapshot root is not a directory: {root}")
    try:
        path = (root / Path(relative_path)).resolve(strict=True)
    except FileNotFoundError as exc:
        raise RecrawlError(
            f"snapshot gzip not found: {relative_path}"
        ) from exc
    if not path.is_relative_to(root) or not path.is_file():
        raise RecrawlError(f"snapshot gzip escapes its root: {relative_path}")
    return path


def _validate_content_addressed_snapshot_path(
    relative_path: str,
    *,
    content_sha256: str,
    expected_kind: str | None = None,
) -> str:
    """Validate the writer's portable ``kind/prefix/SHA.ext.gz`` contract."""

    if re.fullmatch(r"[0-9A-Fa-f]{64}", content_sha256 or "") is None:
        raise RecrawlError("invalid source content SHA-256")
    portable = str(relative_path or "").replace("\\", "/")
    parts = portable.split("/")
    if len(parts) != 3 or any(not part for part in parts):
        raise RecrawlError(
            f"invalid content-addressed snapshot path: {relative_path}"
        )
    kind, prefix, filename = parts
    if re.fullmatch(r"[A-Za-z0-9_-]+", kind) is None:
        raise RecrawlError(f"invalid snapshot kind path: {relative_path}")
    if expected_kind is not None and kind != expected_kind:
        raise RecrawlError(
            f"unexpected snapshot kind path: {relative_path}"
        )
    digest = content_sha256.upper()
    if prefix.upper() != digest[:2]:
        raise RecrawlError(
            f"snapshot path SHA-256 prefix mismatch: {relative_path}"
        )
    match = re.fullmatch(
        r"([0-9A-Fa-f]{64})\.([A-Za-z0-9_-]+)\.gz",
        filename,
    )
    if match is None or match.group(1).upper() != digest:
        raise RecrawlError(
            f"snapshot path content SHA-256 mismatch: {relative_path}"
        )
    return "/".join(parts)


def _read_verified_snapshot(
    snapshot_root: Path,
    *,
    relative_path: str,
    content_sha256: str,
    response_bytes: int,
    gzip_sha256: str | None = None,
    expected_kind: str | None = None,
) -> tuple[bytes, str]:
    if expected_kind is None:
        if re.fullmatch(r"[0-9A-Fa-f]{64}", content_sha256 or "") is None:
            raise RecrawlError("invalid source content SHA-256")
        normalized_relative_path = str(relative_path or "").replace("\\", "/")
    else:
        normalized_relative_path = _validate_content_addressed_snapshot_path(
            relative_path,
            content_sha256=content_sha256,
            expected_kind=expected_kind,
        )
    if (
        isinstance(response_bytes, bool)
        or not isinstance(response_bytes, int)
        or response_bytes <= 0
        or response_bytes > MAX_SNAPSHOT_RESPONSE_BYTES
    ):
        raise RecrawlError(f"unsafe snapshot response size: {response_bytes}")
    path = _safe_snapshot_path(snapshot_root, normalized_relative_path)
    try:
        compressed_size = path.stat().st_size
        if (
            compressed_size <= 0
            or compressed_size > MAX_COMPRESSED_SNAPSHOT_BYTES
        ):
            raise RecrawlError(
                f"unsafe compressed snapshot size: {compressed_size}"
            )
        with path.open("rb") as handle:
            compressed = handle.read(MAX_COMPRESSED_SNAPSHOT_BYTES + 1)
    except OSError as exc:
        raise RecrawlError(f"cannot read snapshot gzip: {relative_path}") from exc
    if (
        len(compressed) != compressed_size
        or len(compressed) > MAX_COMPRESSED_SNAPSHOT_BYTES
    ):
        raise RecrawlError(
            f"snapshot gzip changed during read: {normalized_relative_path}"
        )
    actual_gzip_sha = sha256_bytes(compressed)
    if gzip_sha256 is not None and actual_gzip_sha != gzip_sha256.upper():
        raise RecrawlError(
            "snapshot gzip SHA-256 changed: "
            f"{normalized_relative_path}"
        )
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(compressed), mode="rb") as handle:
            body = handle.read(response_bytes + 1)
    except (OSError, EOFError) as exc:
        raise RecrawlError(
            f"invalid snapshot gzip: {normalized_relative_path}"
        ) from exc
    if len(body) != int(response_bytes):
        raise RecrawlError(
            "snapshot response byte count mismatch: "
            f"{normalized_relative_path}"
        )
    if sha256_bytes(body) != content_sha256.upper():
        raise RecrawlError(
            "snapshot content SHA-256 mismatch: "
            f"{normalized_relative_path}"
        )
    return body, actual_gzip_sha


def _validate_snapshot_final_url(
    target_url: str,
    final_url: str,
    *,
    entity_type: str,
) -> None:
    try:
        parsed = urllib.parse.urlsplit(final_url)
        port = parsed.port
    except ValueError as exc:
        raise RecrawlError(f"invalid source final URL: {final_url}") from exc
    host = (parsed.hostname or "").lower()
    if host == f"www.{ARCHITIZER_HOST}":
        host = ARCHITIZER_HOST
    if (
        parsed.scheme.lower() != "https"
        or host != ARCHITIZER_HOST
        or parsed.username
        or parsed.password
        or port not in {None, 443}
        or parsed.query
        or parsed.fragment
    ):
        raise RecrawlError(f"unsafe source final URL: {final_url}")
    if normalize_entity_url(final_url, entity_type) != target_url:
        raise RecrawlError(
            f"source final URL identity mismatch: {final_url} != {target_url}"
        )


def _has_raw_first_json_recovery(parsed: Mapping[str, Any]) -> bool:
    for record in parsed.get("embedded_records", []):
        raw = record.get("raw")
        if record.get("parse_variant") != "raw" or not isinstance(raw, str):
            continue
        fallback = html.unescape(raw)
        if fallback == raw:
            continue
        try:
            json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        try:
            json.loads(fallback)
        except (json.JSONDecodeError, TypeError, ValueError):
            return True
    return False


def _build_snapshot_reparse_input(
    connection: sqlite3.Connection,
    *,
    target: Mapping[str, Any],
    snapshot_root: Path,
) -> dict[str, Any]:
    attempts = connection.execute(
        """
        SELECT * FROM http_attempts
        WHERE run_id=? AND target_url=?
          AND outcome='success' AND http_status=200 AND sha256=?
        ORDER BY id
        """,
        (
            target["source_metadata_run_id"],
            target["url"],
            target["content_sha256"],
        ),
    ).fetchall()
    if len(attempts) != 1:
        raise RecrawlError(
            "source metadata must resolve to exactly one successful 200 HTTP "
            f"attempt: {target['url']}: found {len(attempts)}"
        )
    attempt = dict(attempts[0])
    expected_request_kind = f"{target['entity_type']}_page"
    if attempt.get("request_kind") != expected_request_kind:
        raise RecrawlError(
            f"source attempt entity kind mismatch: {target['url']}"
        )
    if attempt.get("requested_url") != target["url"]:
        raise RecrawlError(f"source requested URL mismatch: {target['url']}")
    try:
        block_signals = json.loads(attempt.get("block_signals_json") or "null")
    except json.JSONDecodeError as exc:
        raise RecrawlError(f"invalid source block evidence: {target['url']}") from exc
    if block_signals != [] or attempt.get("error") is not None:
        raise RecrawlError(f"source success attempt has error signals: {target['url']}")
    final_url = attempt.get("final_url")
    content_type = attempt.get("content_type")
    gzip_path = attempt.get("gzip_path")
    if not isinstance(final_url, str):
        raise RecrawlError(f"source final URL missing: {target['url']}")
    _validate_snapshot_final_url(
        target["url"],
        final_url,
        entity_type=target["entity_type"],
    )
    if (
        not isinstance(content_type, str)
        or content_type.split(";", 1)[0].strip().lower() != "text/html"
    ):
        raise RecrawlError(f"source content type is not HTML: {target['url']}")
    if not isinstance(gzip_path, str):
        raise RecrawlError(f"source gzip path missing: {target['url']}")
    response_bytes = int(attempt.get("response_bytes") or 0)
    body, gzip_sha = _read_verified_snapshot(
        snapshot_root,
        relative_path=gzip_path,
        content_sha256=target["content_sha256"],
        response_bytes=response_bytes,
    )
    selection_kind = target["selection_kind"]
    if selection_kind == "firm_last_good_parser_upgrade":
        if (
            target["entity_type"] != "firm"
            or target["source_identity_status"] != "valid"
        ):
            raise RecrawlError("firm reparse input lacks valid last-good identity")
    elif selection_kind == "project_parser_regression_recovery":
        source_dom = json.loads(target["source_dom_json"])
        if (
            target["entity_type"] != "project"
            or target["status"] != "failed"
            or int(target["retryable"]) != 0
            or int(target["attempt_count"]) <= 0
            or target["last_attempt_at"] is None
            or target["last_error"] is None
            or int(target["last_http_status"] or 0) != 200
            or target["last_parse_status"] != "no_content"
            or target["last_snapshot_sha256"] != target["content_sha256"]
            or target["source_identity_status"] == "valid"
            or source_dom.get("_canonical_url") != target["url"]
        ):
            raise RecrawlError("project parser-regression evidence is incomplete")
        reparsed = parse_entity_page(
            body,
            requested_url=target["url"],
            final_url=final_url,
            http_status=200,
            content_type=content_type,
            entity_type="project",
        )
        if (
            reparsed["identity"]["status"] != "valid"
            or not _has_raw_first_json_recovery(reparsed)
        ):
            raise RecrawlError(
                "project snapshot is not an exact raw-first parser recovery"
            )
    else:
        raise RecrawlError(f"unknown snapshot reparse selection kind: {selection_kind}")
    return {
        "target_url": target["url"],
        "entity_type": target["entity_type"],
        "selection_kind": selection_kind,
        "source_run_id": int(target["source_metadata_run_id"]),
        "source_metadata_version_id": int(
            target["source_metadata_version_id"]
        ),
        "source_http_attempt_id": int(attempt["id"]),
        "request_kind": str(attempt["request_kind"]),
        "requested_url": str(attempt["requested_url"]),
        "http_outcome": str(attempt["outcome"]),
        "http_status": int(attempt["http_status"]),
        "block_signals_json": "[]",
        "attempt_error": None,
        "content_sha256": str(target["content_sha256"]).upper(),
        "final_url": final_url,
        "content_type": content_type,
        "response_bytes": response_bytes,
        "gzip_path": gzip_path,
        "gzip_sha256": gzip_sha,
        "integrity_status": "verified",
        "target_network_state_json": canonical_json(
            _target_network_state(target)
        ),
    }


def _snapshot_reparse_descriptor_sha(
    descriptors: Iterable[Mapping[str, Any]],
) -> str:
    fields = (
        "target_url",
        "entity_type",
        "selection_kind",
        "source_run_id",
        "source_metadata_version_id",
        "source_http_attempt_id",
        "request_kind",
        "requested_url",
        "http_outcome",
        "http_status",
        "block_signals_json",
        "attempt_error",
        "content_sha256",
        "final_url",
        "content_type",
        "response_bytes",
        "gzip_path",
        "gzip_sha256",
        "integrity_status",
    )
    payload = "\n".join(
        sorted(
            canonical_json({field: item[field] for field in fields})
            for item in descriptors
        )
    ).encode("utf-8")
    return sha256_bytes(payload)


def _validate_frozen_snapshot_evidence(
    connection: sqlite3.Connection,
    frozen: Mapping[str, Any],
) -> None:
    referenced = connection.execute(
        """
        SELECT COUNT(*)
        FROM metadata_versions m
        JOIN runs r ON r.id=m.run_id
        JOIN targets t ON t.url=m.target_url
        JOIN http_attempts a ON a.id=?
        WHERE m.id=? AND m.run_id=? AND m.target_url=?
          AND m.entity_type=?
          AND (
              (?='firm_last_good_parser_upgrade' AND m.identity_status='valid')
              OR (
                  ?='project_parser_regression_recovery'
                  AND m.identity_status<>'valid'
                  AND t.status='failed'
                  AND t.retryable=0
                  AND t.attempt_count>0
                  AND t.last_attempt_at IS NOT NULL
                  AND t.last_error IS NOT NULL
                  AND t.last_http_status=200
                  AND t.last_parse_status='no_content'
                  AND t.last_snapshot_sha256=m.snapshot_sha256
              )
          )
          AND m.snapshot_sha256=? AND r.status LIKE 'completed%'
          AND a.run_id=m.run_id AND a.target_url=m.target_url
          AND a.request_kind=? AND a.requested_url=?
          AND a.outcome=? AND a.http_status=?
          AND a.block_signals_json=? AND a.error IS ?
          AND a.sha256=? AND a.final_url=? AND a.content_type=?
          AND a.response_bytes=? AND a.gzip_path=?
        """,
        (
            frozen["source_http_attempt_id"],
            frozen["source_metadata_version_id"],
            frozen["source_run_id"],
            frozen["target_url"],
            frozen["entity_type"],
            frozen["selection_kind"],
            frozen["selection_kind"],
            frozen["content_sha256"],
            frozen["request_kind"],
            frozen["requested_url"],
            frozen["http_outcome"],
            frozen["http_status"],
            frozen["block_signals_json"],
            frozen["attempt_error"],
            frozen["content_sha256"],
            frozen["final_url"],
            frozen["content_type"],
            frozen["response_bytes"],
            frozen["gzip_path"],
        ),
    ).fetchone()[0]
    exact_attempts = connection.execute(
        """
        SELECT COUNT(*) FROM http_attempts
        WHERE run_id=? AND target_url=? AND request_kind=? AND requested_url=?
          AND outcome=? AND http_status=? AND block_signals_json=?
          AND error IS ? AND sha256=?
        """,
        (
            frozen["source_run_id"],
            frozen["target_url"],
            frozen["request_kind"],
            frozen["requested_url"],
            frozen["http_outcome"],
            frozen["http_status"],
            frozen["block_signals_json"],
            frozen["attempt_error"],
            frozen["content_sha256"],
        ),
    ).fetchone()[0]
    if referenced != 1 or exact_attempts != 1:
        raise RecrawlError("frozen source metadata/HTTP evidence changed")


def _completed_snapshot_reparse_run(
    connection: sqlite3.Connection,
    *,
    run_kind: str,
    source_sha256: str,
    selected_count: int,
    minimum_run_id: int = 0,
) -> sqlite3.Row:
    rows = connection.execute(
        """
        SELECT * FROM runs
        WHERE run_kind=? AND status='completed'
          AND id>? AND parser_version=?
          AND source_db_sha256_before=?
          AND source_db_sha256_after=?
          AND selected_count=?
        ORDER BY id DESC
        """,
        (
            run_kind,
            minimum_run_id,
            PARSER_VERSION,
            source_sha256,
            source_sha256,
            selected_count,
        ),
    ).fetchall()
    for row in rows:
        try:
            summary = json.loads(row["summary_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if (
            summary.get("gate_policy_version")
            == SNAPSHOT_REPARSE_GATE_POLICY_VERSION
            and summary.get("gate_passed") is True
            and summary.get("state_schema_version") == STATE_SCHEMA_VERSION
            and summary.get("parser_version") == PARSER_VERSION
            and summary.get("metadata_version") == METADATA_VERSION
        ):
            return row
    raise RecrawlError(
        f"missing current-parser/current-schema successful {run_kind} gate"
    )


def validate_snapshot_reparse_ladder(
    connection: sqlite3.Connection,
    *,
    run_kind: str,
    source_sha256: str,
) -> dict[str, int]:
    if run_kind == "snapshot_reparse_n10":
        return {}
    n10 = _completed_snapshot_reparse_run(
        connection,
        run_kind="snapshot_reparse_n10",
        source_sha256=source_sha256,
        selected_count=10,
    )
    if run_kind == "snapshot_reparse_n100":
        return {"n10_run_id": int(n10["id"])}
    if run_kind != "snapshot_reparse_full":
        raise ValueError(f"unknown snapshot reparse run kind: {run_kind}")
    n100 = _completed_snapshot_reparse_run(
        connection,
        run_kind="snapshot_reparse_n100",
        source_sha256=source_sha256,
        selected_count=100,
        minimum_run_id=int(n10["id"]),
    )
    return {
        "n10_run_id": int(n10["id"]),
        "n100_run_id": int(n100["id"]),
    }


def _insert_snapshot_reparse_input(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    selection_order: int,
    status_before: str,
    frozen: Mapping[str, Any],
) -> None:
    connection.execute(
        """
        INSERT INTO run_targets(
            run_id,url,selection_order,selected_reason,status_before
        ) VALUES (?,?,?,?,?)
        """,
        (
            run_id,
            frozen["target_url"],
            selection_order,
            frozen["selection_kind"],
            status_before,
        ),
    )
    connection.execute(
        """
        INSERT INTO snapshot_reparse_inputs(
            run_id,target_url,selection_order,
            entity_type,selection_kind,
            source_run_id,source_metadata_version_id,
            source_http_attempt_id,request_kind,requested_url,
            http_outcome,http_status,block_signals_json,
            attempt_error,content_sha256,final_url,content_type,
            response_bytes,gzip_path,gzip_sha256,
            integrity_status,target_network_state_json,frozen_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id,
            frozen["target_url"],
            selection_order,
            frozen["entity_type"],
            frozen["selection_kind"],
            frozen["source_run_id"],
            frozen["source_metadata_version_id"],
            frozen["source_http_attempt_id"],
            frozen["request_kind"],
            frozen["requested_url"],
            frozen["http_outcome"],
            frozen["http_status"],
            frozen["block_signals_json"],
            frozen["attempt_error"],
            frozen["content_sha256"],
            frozen["final_url"],
            frozen["content_type"],
            frozen["response_bytes"],
            frozen["gzip_path"],
            frozen["gzip_sha256"],
            frozen["integrity_status"],
            frozen["target_network_state_json"],
            utc_now(),
        ),
    )


def _insert_snapshot_reparse_lineage(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    version_id: int,
    frozen: Mapping[str, Any],
) -> None:
    connection.execute(
        """
        INSERT INTO snapshot_reparse_lineage(
            reparse_version_id,reparse_run_id,entity_type,selection_kind,
            source_run_id,
            source_metadata_version_id,source_http_attempt_id,request_kind,
            requested_url,http_outcome,http_status,block_signals_json,
            attempt_error,target_url,content_sha256,final_url,content_type,
            response_bytes,gzip_path,gzip_sha256,integrity_status,verified_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            version_id,
            run_id,
            frozen["entity_type"],
            frozen["selection_kind"],
            frozen["source_run_id"],
            frozen["source_metadata_version_id"],
            frozen["source_http_attempt_id"],
            frozen["request_kind"],
            frozen["requested_url"],
            frozen["http_outcome"],
            frozen["http_status"],
            frozen["block_signals_json"],
            frozen["attempt_error"],
            frozen["target_url"],
            frozen["content_sha256"],
            frozen["final_url"],
            frozen["content_type"],
            frozen["response_bytes"],
            frozen["gzip_path"],
            frozen["gzip_sha256"],
            frozen["integrity_status"],
            utc_now(),
        ),
    )


def _snapshot_reparse_summary(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    source_sha_before: str,
    source_sha_after: str,
    elapsed_seconds: float,
    verified_gzip_count: int,
) -> dict[str, Any]:
    inputs = connection.execute(
        """
        SELECT i.*,rt.status_before,rt.status_after
        FROM snapshot_reparse_inputs i
        JOIN run_targets rt ON rt.run_id=i.run_id AND rt.url=i.target_url
        WHERE i.run_id=? ORDER BY i.selection_order
        """,
        (run_id,),
    ).fetchall()
    versions = connection.execute(
        """
        SELECT l.target_url,m.identity_status,m.parse_status,m.id
        FROM snapshot_reparse_lineage l
        JOIN metadata_versions m ON m.id=l.reparse_version_id
        WHERE l.reparse_run_id=? ORDER BY l.target_url
        """,
        (run_id,),
    ).fetchall()
    identity_counts = Counter(row["identity_status"] for row in versions)
    parse_counts = Counter(row["parse_status"] for row in versions)
    office_counts = Counter(
        row["status"]
        for row in connection.execute(
            """
            SELECT rf.status
            FROM snapshot_reparse_lineage l
            JOIN resolved_fields rf ON rf.version_id=l.reparse_version_id
            WHERE l.reparse_run_id=? AND rf.field_name='office_locations'
            """,
            (run_id,),
        )
    )
    selected_urls = [row["target_url"] for row in inputs]
    unchanged_statuses = sum(
        row["status_before"] == row["status_after"] for row in inputs
    )
    reparse_http_attempt_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM http_attempts WHERE run_id=?", (run_id,)
        ).fetchone()[0]
    )
    summary: dict[str, Any] = {
        "run_id": run_id,
        "gate_policy_version": SNAPSHOT_REPARSE_GATE_POLICY_VERSION,
        "state_schema_version": STATE_SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "metadata_version": METADATA_VERSION,
        "selected": len(inputs),
        "processed": len(versions),
        "selected_urls_sha256": _url_set_sha256(selected_urls),
        "frozen_descriptor_sha256": _snapshot_reparse_descriptor_sha(inputs),
        "entity_type_counts": dict(
            sorted(Counter(row["entity_type"] for row in inputs).items())
        ),
        "selection_kind_counts": dict(
            sorted(Counter(row["selection_kind"] for row in inputs).items())
        ),
        "source_metadata_version_count": len(
            {row["source_metadata_version_id"] for row in inputs}
        ),
        "source_http_attempt_count": len(
            {row["source_http_attempt_id"] for row in inputs}
        ),
        "reparse_http_attempt_count": reparse_http_attempt_count,
        "verified_gzip_count": verified_gzip_count,
        "verified_content_bytes": sum(row["response_bytes"] for row in inputs),
        "identity_counts": dict(sorted(identity_counts.items())),
        "parse_status_counts": dict(sorted(parse_counts.items())),
        "office_location_status_counts": dict(sorted(office_counts.items())),
        "target_status_unchanged_count": unchanged_statuses,
        "input_db_sha256_before": source_sha_before,
        "input_db_sha256_after": source_sha_after,
        "input_db_unchanged": source_sha_before == source_sha_after,
        "elapsed_seconds": elapsed_seconds,
    }
    failures: list[str] = []
    if summary["processed"] != summary["selected"]:
        failures.append("not every frozen snapshot produced reparse lineage")
    if verified_gzip_count != summary["selected"]:
        failures.append("not every frozen gzip passed final integrity verification")
    if identity_counts.get("valid", 0) != summary["selected"]:
        failures.append("one or more reparses failed firm identity validation")
    if unchanged_statuses != summary["selected"]:
        failures.append("target network status changed during offline reparse")
    if reparse_http_attempt_count != 0:
        failures.append("offline reparse run recorded an HTTP attempt")
    if not summary["input_db_unchanged"]:
        failures.append("immutable legacy source DB changed")
    summary["gate_failures"] = failures
    summary["gate_passed"] = not failures
    return summary


def _resume_snapshot_reparse_run(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    run_kind: str,
    source_path: Path,
    source_sha256: str,
    source_size: int,
) -> sqlite3.Row:
    run = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if run is None:
        raise RecrawlError(f"snapshot reparse resume run not found: {run_id}")
    if run["status"] not in {"running", "failed", "interrupted"}:
        raise RecrawlError("completed or quality-gated reparse runs are immutable")
    if (
        run["run_kind"] != run_kind
        or run["parser_version"] != PARSER_VERSION
        or not _same_resolved_path(run["source_db_path"], source_path)
        or run["source_db_sha256_before"] != source_sha256
        or int(run["source_db_size"]) != source_size
    ):
        raise RecrawlError("snapshot reparse resume identity mismatch")
    try:
        arguments = json.loads(run["arguments_json"])
    except json.JSONDecodeError as exc:
        raise RecrawlError("snapshot reparse resume arguments are invalid") from exc
    if (
        arguments.get("state_schema_version") != STATE_SCHEMA_VERSION
        or arguments.get("parser_version") != PARSER_VERSION
        or arguments.get("metadata_version") != METADATA_VERSION
    ):
        raise RecrawlError("snapshot reparse resume parser/schema mismatch")
    input_count = connection.execute(
        "SELECT COUNT(*) FROM snapshot_reparse_inputs WHERE run_id=?",
        (run_id,),
    ).fetchone()[0]
    if int(input_count) != int(run["selected_count"]):
        raise RecrawlError("snapshot reparse frozen input set is incomplete")
    frozen_inputs = connection.execute(
        "SELECT * FROM snapshot_reparse_inputs WHERE run_id=?",
        (run_id,),
    ).fetchall()
    if (
        arguments.get("frozen_url_count") != len(frozen_inputs)
        or arguments.get("frozen_urls_sha256")
        != _url_set_sha256(row["target_url"] for row in frozen_inputs)
        or arguments.get("frozen_descriptor_sha256")
        != _snapshot_reparse_descriptor_sha(frozen_inputs)
    ):
        raise RecrawlError("snapshot reparse frozen descriptor set changed")
    connection.execute(
        """
        UPDATE runs SET status='running',finished_at=NULL,summary_json=NULL,error=NULL
        WHERE id=?
        """,
        (run_id,),
    )
    connection.commit()
    return run


def run_snapshot_reparse(
    *,
    smoke_size: int | None,
    confirmed_full: bool = False,
    resume_run_id: int | None = None,
    source_path: Path = DEFAULT_SOURCE_DB,
    state_path: Path = DEFAULT_STATE_DB,
    snapshot_root: Path = DEFAULT_SNAPSHOT_DIR,
) -> dict[str, Any]:
    """Reparse frozen firm gzip snapshots without issuing any HTTP request."""

    if smoke_size not in {10, 100, None}:
        raise ValueError("snapshot reparse smoke_size must be 10, 100, or None")
    if smoke_size is None and not confirmed_full:
        raise RecrawlError("full snapshot reparse requires explicit confirmation")
    run_kind = (
        "snapshot_reparse_full"
        if smoke_size is None
        else f"snapshot_reparse_n{smoke_size}"
    )
    source_path = source_path.resolve()
    state_path = state_path.resolve()
    snapshot_root = snapshot_root.resolve()
    validate_runtime_paths(
        source_path=source_path,
        state_path=state_path,
        snapshot_root=snapshot_root,
    )
    run_start = time.monotonic()
    run_id: int | None = None
    with SidecarLock(state_path):
        source_sha = sha256_file(source_path)
        source_size = source_path.stat().st_size
        state = connect_state(
            state_path,
            source_path=source_path,
            source_sha256=source_sha,
            source_size=source_size,
        )
        try:
            ladder = validate_snapshot_reparse_ladder(
                state,
                run_kind=run_kind,
                source_sha256=source_sha,
            )
            if resume_run_id is not None:
                _resume_snapshot_reparse_run(
                    state,
                    run_id=resume_run_id,
                    run_kind=run_kind,
                    source_path=source_path,
                    source_sha256=source_sha,
                    source_size=source_size,
                )
                run_id = resume_run_id
            else:
                eligible = select_snapshot_reparse_targets(state)
                if smoke_size is not None and len(eligible) < smoke_size:
                    raise RecrawlError(
                        f"only {len(eligible)} eligible firm snapshots; "
                        f"{smoke_size} required"
                    )
                if smoke_size is None:
                    selected = eligible
                else:
                    recovery = [
                        row
                        for row in eligible
                        if row["selection_kind"]
                        == "project_parser_regression_recovery"
                    ][:1]
                    firms = [
                        row
                        for row in eligible
                        if row["selection_kind"]
                        == "firm_last_good_parser_upgrade"
                    ]
                    selected = recovery + firms[: smoke_size - len(recovery)]
                    if len(selected) != smoke_size:
                        raise RecrawlError(
                            "insufficient firm snapshots after reserving parser "
                            "regression recovery coverage"
                        )
                frozen = [
                    _build_snapshot_reparse_input(
                        state,
                        target=target,
                        snapshot_root=snapshot_root,
                    )
                    for target in selected
                ]
                arguments = {
                    "state_schema_version": STATE_SCHEMA_VERSION,
                    "parser_version": PARSER_VERSION,
                    "metadata_version": METADATA_VERSION,
                    "eligible_url_count": len(eligible),
                    "eligible_urls_sha256": _url_set_sha256(
                        row["url"] for row in eligible
                    ),
                    "frozen_url_count": len(selected),
                    "frozen_urls_sha256": _url_set_sha256(
                        row["url"] for row in selected
                    ),
                    "frozen_descriptor_sha256": (
                        _snapshot_reparse_descriptor_sha(frozen)
                    ),
                    "ladder": ladder,
                    "confirmed_full": confirmed_full,
                }
                state.execute("BEGIN IMMEDIATE")
                run_id = start_run(
                    state,
                    run_kind=run_kind,
                    source_path=source_path,
                    source_sha256=source_sha,
                    source_size=source_size,
                    arguments=arguments,
                    commit=False,
                )
                for ordinal, (target, item) in enumerate(
                    zip(selected, frozen), start=1
                ):
                    _insert_snapshot_reparse_input(
                        state,
                        run_id=run_id,
                        selection_order=ordinal,
                        status_before=target["status"],
                        frozen=item,
                    )
                state.execute(
                    "UPDATE runs SET selected_count=? WHERE id=?",
                    (len(selected), run_id),
                )
                state.commit()
            inputs = state.execute(
                """
                SELECT * FROM snapshot_reparse_inputs
                WHERE run_id=? ORDER BY selection_order
                """,
                (run_id,),
            ).fetchall()
            legacy = open_legacy_readonly(source_path)
            try:
                for frozen_row in inputs:
                    frozen = dict(frozen_row)
                    existing_lineage = state.execute(
                        """
                        SELECT reparse_version_id FROM snapshot_reparse_lineage
                        WHERE reparse_run_id=? AND target_url=?
                        """,
                        (run_id, frozen["target_url"]),
                    ).fetchone()
                    if existing_lineage is not None:
                        continue
                    _validate_frozen_snapshot_evidence(state, frozen)
                    body, actual_gzip_sha = _read_verified_snapshot(
                        snapshot_root,
                        relative_path=frozen["gzip_path"],
                        content_sha256=frozen["content_sha256"],
                        response_bytes=frozen["response_bytes"],
                        gzip_sha256=frozen["gzip_sha256"],
                    )
                    if actual_gzip_sha != frozen["gzip_sha256"]:
                        raise RecrawlError("frozen snapshot gzip identity changed")
                    target_row = state.execute(
                        "SELECT * FROM targets WHERE url=?",
                        (frozen["target_url"],),
                    ).fetchone()
                    if target_row is None:
                        raise RecrawlError("frozen snapshot target disappeared")
                    expected_network_state = json.loads(
                        frozen["target_network_state_json"]
                    )
                    if _target_network_state(target_row) != expected_network_state:
                        raise RecrawlError(
                            "target network state changed after reparse freeze"
                        )
                    source_metadata = state.execute(
                        "SELECT * FROM metadata_versions WHERE id=?",
                        (frozen["source_metadata_version_id"],),
                    ).fetchone()
                    if (
                        source_metadata is None
                        or source_metadata["target_url"] != frozen["target_url"]
                        or source_metadata["entity_type"]
                        != frozen["entity_type"]
                        or source_metadata["snapshot_sha256"]
                        != frozen["content_sha256"]
                        or (
                            frozen["selection_kind"]
                            == "firm_last_good_parser_upgrade"
                            and source_metadata["identity_status"] != "valid"
                        )
                        or (
                            frozen["selection_kind"]
                            == "project_parser_regression_recovery"
                            and source_metadata["identity_status"] == "valid"
                        )
                    ):
                        raise RecrawlError("frozen source metadata identity changed")
                    parsed = parse_entity_page(
                        body,
                        requested_url=frozen["target_url"],
                        final_url=frozen["final_url"],
                        http_status=200,
                        content_type=frozen["content_type"],
                        entity_type=frozen["entity_type"],
                    )
                    state.execute("BEGIN IMMEDIATE")
                    try:
                        current_target = dict(
                            state.execute(
                                "SELECT * FROM targets WHERE url=?",
                                (frozen["target_url"],),
                            ).fetchone()
                        )
                        if (
                            _target_network_state(current_target)
                            != expected_network_state
                        ):
                            raise RecrawlError(
                                "target network state changed before commit"
                            )
                        prior_current_parser = state.execute(
                            """
                            SELECT id FROM metadata_versions
                            WHERE target_url=? AND snapshot_sha256=?
                              AND parser_version=?
                            """,
                            (
                                frozen["target_url"],
                                frozen["content_sha256"],
                                PARSER_VERSION,
                            ),
                        ).fetchone()
                        if prior_current_parser is not None:
                            raise RecrawlError(
                                "current-parser metadata exists without frozen lineage"
                            )
                        version_id = _store_parse_result(
                            state,
                            run_id=run_id,
                            target=current_target,
                            snapshot_sha=frozen["content_sha256"],
                            parsed=parsed,
                            legacy_connection=legacy,
                            promote_valid=False,
                            commit=False,
                        )
                        _insert_snapshot_reparse_lineage(
                            state,
                            run_id=run_id,
                            version_id=version_id,
                            frozen=frozen,
                        )
                        final_target = state.execute(
                            "SELECT * FROM targets WHERE url=?",
                            (frozen["target_url"],),
                        ).fetchone()
                        if (
                            _target_network_state(final_target)
                            != expected_network_state
                        ):
                            raise RecrawlError(
                                "offline reparse mutated target network state"
                            )
                        state.execute(
                            """
                            UPDATE run_targets SET status_after=?
                            WHERE run_id=? AND url=?
                            """,
                            (
                                expected_network_state["status"],
                                run_id,
                                frozen["target_url"],
                            ),
                        )
                        state.commit()
                    except Exception:
                        state.rollback()
                        raise
            finally:
                legacy.close()
            final_verified_gzip_count = 0
            for frozen in inputs:
                _validate_frozen_snapshot_evidence(state, frozen)
                _read_verified_snapshot(
                    snapshot_root,
                    relative_path=frozen["gzip_path"],
                    content_sha256=frozen["content_sha256"],
                    response_bytes=frozen["response_bytes"],
                    gzip_sha256=frozen["gzip_sha256"],
                )
                final_verified_gzip_count += 1
            source_sha_after = sha256_file(source_path)
            summary = _snapshot_reparse_summary(
                state,
                run_id=run_id,
                source_sha_before=source_sha,
                source_sha_after=source_sha_after,
                elapsed_seconds=time.monotonic() - run_start,
                verified_gzip_count=final_verified_gzip_count,
            )
            run_arguments = json.loads(
                state.execute(
                    "SELECT arguments_json FROM runs WHERE id=?", (run_id,)
                ).fetchone()[0]
            )
            summary.update(
                {
                    "eligible_url_count": run_arguments["eligible_url_count"],
                    "eligible_urls_sha256": run_arguments[
                        "eligible_urls_sha256"
                    ],
                    "frozen_url_count": run_arguments["frozen_url_count"],
                    "frozen_urls_sha256": run_arguments["frozen_urls_sha256"],
                    "expected_frozen_descriptor_sha256": run_arguments[
                        "frozen_descriptor_sha256"
                    ],
                    "ladder": run_arguments["ladder"],
                }
            )
            if (
                summary["frozen_descriptor_sha256"]
                != summary["expected_frozen_descriptor_sha256"]
            ):
                summary["gate_failures"].append(
                    "frozen snapshot descriptor SHA changed"
                )
                summary["gate_passed"] = False
            status = "completed" if summary["gate_passed"] else "quality_failed"
            if summary["gate_passed"]:
                state.execute("BEGIN IMMEDIATE")
                try:
                    for lineage in state.execute(
                        """
                        SELECT reparse_version_id,target_url
                        FROM snapshot_reparse_lineage
                        WHERE reparse_run_id=? ORDER BY target_url
                        """,
                        (run_id,),
                    ):
                        _promote_stored_metadata_version(
                            state,
                            target_url=lineage["target_url"],
                            version_id=lineage["reparse_version_id"],
                        )
                    for frozen in state.execute(
                        """
                        SELECT target_url,target_network_state_json
                        FROM snapshot_reparse_inputs WHERE run_id=?
                        """,
                        (run_id,),
                    ):
                        target = state.execute(
                            "SELECT * FROM targets WHERE url=?",
                            (frozen["target_url"],),
                        ).fetchone()
                        if _target_network_state(target) != json.loads(
                            frozen["target_network_state_json"]
                        ):
                            raise RecrawlError(
                                "target network state changed during promotion"
                            )
                    finish_run(
                        state,
                        run_id,
                        status=status,
                        source_sha256_after=source_sha_after,
                        summary=summary,
                        selected_count=summary["selected"],
                        commit=False,
                    )
                    state.commit()
                except Exception:
                    state.rollback()
                    raise
            else:
                finish_run(
                    state,
                    run_id,
                    status=status,
                    source_sha256_after=source_sha_after,
                    summary=summary,
                    selected_count=summary["selected"],
                )
            if not summary["gate_passed"]:
                raise RecrawlError(
                    "snapshot reparse failed quality gates: "
                    + "; ".join(summary["gate_failures"])
                )
            return summary
        except Exception as exc:
            state.rollback()
            source_sha_after = sha256_file(source_path)
            if run_id is not None:
                existing = state.execute(
                    "SELECT status FROM runs WHERE id=?", (run_id,)
                ).fetchone()
                if existing and existing["status"] == "running":
                    finish_run(
                        state,
                        run_id,
                        status="failed",
                        source_sha256_after=source_sha_after,
                        summary={},
                        error=f"{type(exc).__name__}: {exc}",
                    )
            raise
        finally:
            state.close()


def open_state_readonly(path: Path) -> sqlite3.Connection:
    path = path.resolve()
    if not path.exists():
        raise RecrawlError(f"sidecar state DB not found: {path}")
    connection = sqlite3.connect(
        path.as_uri() + "?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _latest_completed_census_id(connection: sqlite3.Connection) -> int:
    latest = connection.execute(
        """
        SELECT id FROM runs
        WHERE run_kind='sitemap_census' AND status='completed'
        ORDER BY id DESC LIMIT 1
        """
    ).fetchone()
    if latest is None:
        raise RecrawlError("no completed sitemap census")
    return int(latest["id"])


def _full_sitemap_entries(
    connection: sqlite3.Connection,
    *,
    census_run_id: int,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            """
            SELECT entity_type,source_url,lastmod,discovery_source
            FROM sitemap_entries
            WHERE run_id=?
            ORDER BY entity_type,source_url
            """,
            (census_run_id,),
        )
    ]


def _simulate_full_target_expansion(
    connection: sqlite3.Connection,
    *,
    sitemap_entries: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Apply ``expand_full_targets`` scheduling semantics without DB writes."""

    targets = {
        row["url"]: dict(row)
        for row in connection.execute(
            """
            SELECT url,entity_type,source_lastmod,priority,primary_reason,
                   status,retryable
            FROM targets
            ORDER BY entity_type,url
            """
        )
    }
    inserted_urls: set[str] = set()
    would_reschedule_urls: set[str] = set()
    for entry in sitemap_entries:
        url = normalize_entity_url(entry["source_url"], entry["entity_type"])
        existing = targets.get(url)
        if existing is None:
            targets[url] = {
                "url": url,
                "entity_type": entry["entity_type"],
                "source_lastmod": entry["lastmod"],
                "priority": 70,
                "primary_reason": "full_current_sitemap",
                "status": "pending",
                "retryable": 1,
            }
            inserted_urls.add(url)
            continue

        lastmod_changed = _incoming_lastmod_changed(
            existing["source_lastmod"],
            entry["lastmod"],
        )
        existing_priority = int(existing["priority"])
        if 70 < existing_priority:
            existing["primary_reason"] = "full_current_sitemap"
        existing["priority"] = min(70, existing_priority)
        if entry["lastmod"] is not None:
            existing["source_lastmod"] = entry["lastmod"]
        if lastmod_changed:
            existing["status"] = "pending"
            existing["retryable"] = 1
            would_reschedule_urls.add(url)

    eligible_targets = sorted(
        (
            target
            for target in targets.values()
            if target["status"] == "pending"
            or (target["status"] == "failed" and target["retryable"])
        ),
        key=lambda target: (
            int(target["priority"]),
            target["entity_type"],
            target["url"],
        ),
    )
    return {
        "targets": targets,
        "eligible_targets": eligible_targets,
        "inserted_urls": sorted(inserted_urls),
        "would_reschedule_urls": sorted(would_reschedule_urls),
    }


def expand_full_targets(
    connection: sqlite3.Connection,
) -> dict[str, int]:
    """Materialize every current sitemap URL as a full-refresh target."""

    census_run_id = _latest_completed_census_id(connection)
    counts: Counter[str] = Counter()
    for row in _full_sitemap_entries(
        connection,
        census_run_id=census_run_id,
    ):
        upsert_target(
            connection,
            url=row["source_url"],
            entity_type=row["entity_type"],
            source_lastmod=row["lastmod"],
            priority=70,
            reason="full_current_sitemap",
            discovery_source=row["discovery_source"],
            input_lineage={
                "census_run_id": census_run_id,
                "source_sitemap": row["discovery_source"],
            },
        )
        counts[row["entity_type"]] += 1
    connection.commit()
    return dict(sorted(counts.items()))


def _estimate_full_timing(
    smoke_summary: Mapping[str, Any],
    *,
    delay_seconds: float,
) -> dict[str, float | str]:
    """Estimate full-run throughput without double-counting request pacing.

    ``PoliteHttpClient`` applies ``delay_seconds`` as a minimum start-to-start
    interval.  The N100 wall time already includes that pacing plus parsing,
    snapshots, SQLite writes, and retries, so adding network latency to the
    delay would count normal request time twice.
    """

    selected = max(1, int(smoke_summary.get("selected") or 0))
    median_network_seconds = (
        float(smoke_summary["duration_ms"]["median"]) / 1000.0
    )
    attempt_multiplier = max(
        1.0,
        float(smoke_summary["physical_request_attempts"]) / selected,
    )
    run_elapsed = float(smoke_summary.get("run_elapsed_seconds") or 0.0)
    observed_elapsed_per_target = (
        run_elapsed / selected if run_elapsed > 0 else 0.0
    )
    network_fallback = median_network_seconds * attempt_multiplier
    seconds_per_target = max(
        float(delay_seconds),
        observed_elapsed_per_target,
        network_fallback,
    )
    return {
        "request_delay_seconds": float(delay_seconds),
        "median_network_seconds": median_network_seconds,
        "attempt_multiplier": attempt_multiplier,
        "observed_elapsed_seconds_per_target": observed_elapsed_per_target,
        "seconds_per_target": seconds_per_target,
        "model": (
            "max(request delay, observed N100 wall time per target, "
            "median network time adjusted for attempts); request delay is a "
            "start-to-start minimum"
        ),
    }


def preview_full_recrawl(
    *,
    state_path: Path = DEFAULT_STATE_DB,
    snapshot_root: Path = DEFAULT_SNAPSHOT_DIR,
    delay_seconds: float = 2.0,
) -> dict[str, Any]:
    connection = open_state_readonly(state_path)
    connection.execute("BEGIN")
    try:
        state_meta = {
            row["key"]: row["value"]
            for row in connection.execute("SELECT key,value FROM state_meta")
        }
        bound_sha = state_meta.get("source_db_sha256")
        ladder: dict[str, int] | None = None
        ladder_error: str | None = None
        if bound_sha:
            try:
                ladder = validate_full_ladder(
                    connection,
                    source_sha256=bound_sha,
                )
            except RecrawlError as exc:
                ladder_error = str(exc)
        else:
            ladder_error = "sidecar has no immutable source binding"
        latest_census_id = _latest_completed_census_id(connection)
        current_entries = _full_sitemap_entries(
            connection,
            census_run_id=latest_census_id,
        )
        expansion = _simulate_full_target_expansion(
            connection,
            sitemap_entries=current_entries,
        )
        targets = list(expansion["targets"].values())
        universe = {
            row["url"]: row["entity_type"]
            for row in targets
        }
        remaining_urls = [
            row["url"] for row in expansion["eligible_targets"]
        ]
        terminal_failures = [
            row
            for row in targets
            if row["status"] == "failed" and not row["retryable"]
        ]
        latest_n100 = (
            connection.execute(
                "SELECT id,summary_json FROM runs WHERE id=?",
                (ladder["n100_run_id"],),
            ).fetchone()
            if ladder
            else None
        )
        smoke_summary = (
            json.loads(latest_n100["summary_json"]) if latest_n100 else None
        )
        if smoke_summary:
            estimate_basis = _estimate_full_timing(
                smoke_summary,
                delay_seconds=delay_seconds,
            )
            seconds_per_target = float(estimate_basis["seconds_per_target"])
            estimated_seconds = seconds_per_target * len(remaining_urls)
            runtime_storage = smoke_summary.get("runtime_storage", {})
            low_per_target = runtime_storage.get(
                "estimate_low_bytes_per_target",
                smoke_summary["average_snapshot_gzip_bytes"],
            )
            high_per_target = runtime_storage.get(
                "estimate_high_bytes_per_target",
                max(
                    smoke_summary["average_snapshot_gzip_bytes"] * 2,
                    low_per_target * 2,
                ),
            )
            estimated_storage_low = low_per_target * len(remaining_urls)
            estimated_storage_high = high_per_target * len(remaining_urls)
        else:
            estimate_basis = {
                "request_delay_seconds": delay_seconds,
                "median_network_seconds": None,
                "attempt_multiplier": None,
                "observed_elapsed_seconds_per_target": None,
                "seconds_per_target": None,
                "model": (
                    "requires a validated N100 summary; request delay is a "
                    "start-to-start minimum"
                ),
            }
            seconds_per_target = None
            estimated_seconds = None
            low_per_target = None
            high_per_target = None
            estimated_storage_low = None
            estimated_storage_high = None
        return {
            "latest_census_run_id": latest_census_id,
            "latest_n100_run_id": int(latest_n100["id"])
            if latest_n100
            else None,
            "validated_ladder": ladder,
            "ladder_error": ladder_error,
            "current_sitemap": dict(
                sorted(Counter(row["entity_type"] for row in current_entries).items())
            ),
            "full_target_universe": {
                "total": len(universe),
                "by_entity_type": dict(
                    sorted(Counter(universe.values()).items())
                ),
            },
            "already_done": sum(
                row["status"] == "done" for row in targets if row["url"] in universe
            ),
            "remaining_network_targets": len(remaining_urls),
            "remaining_network_target_urls_sha256": _url_set_sha256(
                remaining_urls
            ),
            "would_insert_targets": len(expansion["inserted_urls"]),
            "would_insert_target_urls_sha256": _url_set_sha256(
                expansion["inserted_urls"]
            ),
            "would_reschedule_lastmod_targets": len(
                expansion["would_reschedule_urls"]
            ),
            "would_reschedule_lastmod_urls_sha256": _url_set_sha256(
                expansion["would_reschedule_urls"]
            ),
            "terminal_failures_excluded_until_explicit_retry": len(
                terminal_failures
            ),
            "terminal_failure_urls": [row["url"] for row in terminal_failures],
            "estimate_basis": estimate_basis,
            "estimated_elapsed_seconds": estimated_seconds,
            "estimated_runtime_storage_bytes": {
                "low": estimated_storage_low,
                "high": estimated_storage_high,
                "low_per_target": low_per_target,
                "high_per_target": high_per_target,
                "includes": (
                    "content-addressed attempt snapshots plus observed sidecar "
                    "SQLite growth; high estimate applies retry/page-allocation "
                    "safety factor"
                ),
            },
            "full_command": (
                "python tools/recrawl_architizer_source_v2.py full "
                "--confirm-full-network-crawl"
            ),
            "approval_required": True,
            "ladder_ready": ladder is not None,
        }
    finally:
        connection.rollback()
        connection.close()


def _full_eligible_targets(
    connection: sqlite3.Connection,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            """
            SELECT * FROM targets
            WHERE status='pending'
               OR (status='failed' AND retryable=1)
            ORDER BY priority,entity_type,url
            """
        )
    ]


def _validate_full_delay(
    n100_summary: Mapping[str, Any],
    *,
    delay_seconds: float,
) -> float:
    """Enforce the conservative N100 pacing recommendation for a full run."""

    recommended = float(
        n100_summary.get("recommended_request_delay_seconds")
        or MINIMUM_FULL_DELAY_SECONDS
    )
    minimum = max(MINIMUM_FULL_DELAY_SECONDS, recommended)
    if not float(delay_seconds) >= minimum:
        raise RecrawlError(
            "full request delay is below the validated N100 minimum: "
            f"{delay_seconds}<{minimum}"
        )
    return minimum


def run_full_recrawl(
    *,
    confirmed: bool,
    source_path: Path = DEFAULT_SOURCE_DB,
    state_path: Path = DEFAULT_STATE_DB,
    snapshot_root: Path = DEFAULT_SNAPSHOT_DIR,
    delay_seconds: float = 2.0,
    timeout_seconds: float = 30.0,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Run the full sidecar refresh only after an explicit CLI confirmation."""

    if not confirmed:
        raise RecrawlError(
            "full network crawl is blocked; pass "
            "--confirm-full-network-crawl only after user approval"
        )
    source_path = source_path.resolve()
    state_path = state_path.resolve()
    snapshot_root = snapshot_root.resolve()
    validate_runtime_paths(
        source_path=source_path,
        state_path=state_path,
        snapshot_root=snapshot_root,
    )
    source_sha = sha256_file(source_path)
    source_size = source_path.stat().st_size
    run_start = time.monotonic()
    arguments = {
        "confirmed": True,
        "delay_seconds": delay_seconds,
        "timeout_seconds": timeout_seconds,
        "max_attempts": max_attempts,
    }
    with SidecarLock(state_path):
        state = connect_state(
            state_path,
            source_path=source_path,
            source_sha256=source_sha,
            source_size=source_size,
        )
        try:
            recover_interrupted_state(state)
            ladder = validate_full_ladder(
                state,
                source_sha256=source_sha,
            )
            arguments["ladder"] = ladder
            n100_row = state.execute(
                "SELECT summary_json FROM runs WHERE id=?",
                (ladder["n100_run_id"],),
            ).fetchone()
            if n100_row is None:
                raise RecrawlError("validated N100 run disappeared")
            n100_summary = json.loads(n100_row["summary_json"] or "{}")
            arguments["validated_minimum_delay_seconds"] = (
                _validate_full_delay(
                    n100_summary,
                    delay_seconds=delay_seconds,
                )
            )
            expanded_counts = expand_full_targets(state)
            selected = _full_eligible_targets(state)
            frozen_urls = [target["url"] for target in selected]
            arguments["frozen_target_count"] = len(frozen_urls)
            arguments["frozen_target_urls_sha256"] = _url_set_sha256(
                frozen_urls
            )
            storage_before = measure_runtime_storage(
                state_path,
                snapshot_root,
            )
            run_id = start_run(
                state,
                run_kind="full_recrawl_v2",
                source_path=source_path,
                source_sha256=source_sha,
                source_size=source_size,
                arguments={**arguments, "expanded_counts": expanded_counts},
            )
        except Exception:
            state.close()
            raise
        try:
            for ordinal, target in enumerate(selected, start=1):
                state.execute(
                    """
                    INSERT INTO run_targets(
                        run_id,url,selection_order,selected_reason,status_before
                    ) VALUES (?,?,?,?,?)
                    """,
                    (
                        run_id,
                        target["url"],
                        ordinal,
                        target["primary_reason"],
                        target["status"],
                    ),
                )
            state.execute(
                "UPDATE runs SET selected_count=? WHERE id=?",
                (len(selected), run_id),
            )
            state.commit()
            legacy_connection = open_legacy_readonly(source_path)
            client = PoliteHttpClient(
                delay_seconds=delay_seconds,
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
                jitter_seed=f"full-recrawl:{run_id}",
                allowed_hosts={ARCHITIZER_HOST, f"www.{ARCHITIZER_HOST}"},
            )
            circuit_open = False
            try:
                for target in selected:
                    state.execute(
                        """
                        UPDATE targets
                        SET status='in_progress',last_attempt_at=?,updated_at=?
                        WHERE url=?
                        """,
                        (utc_now(), utc_now(), target["url"]),
                    )
                    state.commit()
                    try:
                        result = client.fetch(target["url"])
                    except CircuitOpenError:
                        circuit_open = True
                        state.execute(
                            """
                            UPDATE targets SET status='pending',updated_at=?
                            WHERE url=?
                            """,
                            (utc_now(), target["url"]),
                        )
                        state.commit()
                        break
                    records = _record_http_attempts(
                        state,
                        run_id=run_id,
                        result=result,
                        request_kind=f"{target['entity_type']}_page",
                        target_url=target["url"],
                        snapshot_root=snapshot_root,
                        snapshot_kind="pages",
                        extension="html",
                    )
                    attempt = result.final
                    record = records[-1]
                    target_status = "failed"
                    parse_status = "no_content"
                    retryable = int(
                        attempt.retryable or attempt.outcome == "blocked"
                    )
                    error = attempt.error
                    if (
                        attempt.outcome == "success"
                        and attempt.http_status == 200
                        and attempt.body
                        and record["sha256"]
                    ):
                        parsed = parse_entity_page(
                            attempt.body,
                            requested_url=target["url"],
                            final_url=attempt.final_url or target["url"],
                            http_status=attempt.http_status,
                            content_type=attempt.content_type,
                            entity_type=target["entity_type"],
                        )
                        parse_status = parsed["parse_status"]
                        version_id = _store_parse_result(
                            state,
                            run_id=run_id,
                            target=target,
                            snapshot_sha=record["sha256"],
                            parsed=parsed,
                            legacy_connection=legacy_connection,
                        )
                        _schedule_discovered_relationships(
                            state,
                            version_id=version_id,
                            legacy_connection=legacy_connection,
                        )
                        if parsed["identity"]["status"] == "valid":
                            target_status = "done"
                            retryable = 0
                            error = None
                        else:
                            retryable = int(
                                parsed["page_classification"]["classification"]
                                in {"block", "login"}
                            )
                            error = ";".join(
                                parsed["identity"]["errors"]
                                + parsed["identity"]["missing"]
                            )
                    state.execute(
                        """
                        UPDATE targets
                        SET status=?,retryable=?,attempt_count=attempt_count+?,
                            last_attempt_at=?,last_error=?,last_http_status=?,
                            last_snapshot_sha256=?,last_parse_status=?,updated_at=?
                        WHERE url=?
                        """,
                        (
                            target_status,
                            retryable,
                            len(result.attempts),
                            utc_now(),
                            error,
                            attempt.http_status,
                            record["sha256"],
                            parse_status,
                            utc_now(),
                            target["url"],
                        ),
                    )
                    state.execute(
                        """
                        UPDATE run_targets SET status_after=?
                        WHERE run_id=? AND url=?
                        """,
                        (target_status, run_id, target["url"]),
                    )
                    state.commit()
            finally:
                legacy_connection.close()
            source_sha_after = sha256_file(source_path)
            if source_sha_after != source_sha:
                raise RecrawlError("legacy source DB changed during full recrawl")
            summary = _network_run_summary(
                state,
                run_id=run_id,
                source_sha_before=source_sha,
                source_sha_after=source_sha_after,
                delay_seconds=delay_seconds,
                state_path=state_path,
                snapshot_root=snapshot_root,
                storage_before=storage_before,
                elapsed_seconds=time.monotonic() - run_start,
            )
            summary["expanded_current_sitemap_counts"] = expanded_counts
            pending_discoveries = _pending_discoveries_for_run(
                state,
                run_id=run_id,
            )
            summary["frozen_target_count"] = len(frozen_urls)
            summary["frozen_target_urls_sha256"] = _url_set_sha256(frozen_urls)
            summary["newly_discovered_pending_count"] = len(
                pending_discoveries
            )
            summary["newly_discovered_pending_urls_sha256"] = _url_set_sha256(
                row["url"] for row in pending_discoveries
            )
            summary["newly_discovered_pending_by_entity_type"] = dict(
                sorted(
                    Counter(
                        row["entity_type"] for row in pending_discoveries
                    ).items()
                )
            )
            summary["additional_full_phase_approval_required"] = bool(
                pending_discoveries
            )
            run_status = (
                "circuit_open"
                if circuit_open
                else "completed_with_pending_discoveries"
                if pending_discoveries
                else "completed"
            )
            finish_run(
                state,
                run_id,
                status=run_status,
                source_sha256_after=source_sha_after,
                summary=summary,
                selected_count=len(selected),
            )
            if circuit_open:
                raise CircuitOpenError(
                    "full recrawl stopped by circuit breaker; resume only after audit"
                )
            return summary
        except Exception as exc:
            source_sha_after = sha256_file(source_path)
            existing = state.execute(
                "SELECT status FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            if existing and existing["status"] == "running":
                finish_run(
                    state,
                    run_id,
                    status="failed",
                    source_sha256_after=source_sha_after,
                    summary={},
                    error=f"{type(exc).__name__}: {exc}",
                )
            raise
        finally:
            state.close()


def _open_state_without_migration_for_full_finalization(
    state_path: Path,
    *,
    source_path: Path,
    source_sha256: str,
    source_size: int,
) -> tuple[sqlite3.Connection, str]:
    """Open a failed full-run sidecar without changing its historical schema."""

    state_path = state_path.resolve()
    if not state_path.exists() or not state_path.is_file():
        raise RecrawlError(f"sidecar DB does not exist: {state_path}")
    meta = _state_meta_readonly(state_path)
    schema_version = meta.get("schema_version")
    if schema_version not in {"2.1", STATE_SCHEMA_VERSION}:
        raise RecrawlError(
            "full-run finalization requires sidecar schema 2.1 or "
            f"{STATE_SCHEMA_VERSION}, got {schema_version!r}"
        )
    if not all(
        meta.get(key)
        for key in ("source_db_path", "source_db_sha256", "source_db_size")
    ):
        raise RecrawlError("full-run finalization requires a bound sidecar")
    _validate_source_binding(
        meta,
        source_path=source_path,
        source_sha256=source_sha256,
        source_size=source_size,
    )
    connection = sqlite3.connect(
        state_path.as_uri() + "?mode=rw",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    required_tables = {
        "state_meta",
        "runs",
        "targets",
        "target_reasons",
        "run_targets",
        "http_attempts",
        "metadata_versions",
        "run_metadata_versions",
        "resolved_fields",
        "legacy_field_comparisons",
    }
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    missing = sorted(required_tables - tables)
    if missing:
        connection.close()
        raise RecrawlError(
            "sidecar is missing full-run finalization tables: "
            + ",".join(missing)
        )
    return connection, schema_version


def _parse_aware_run_timestamp(value: Any, *, field_name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise RecrawlError(f"recovery run has no {field_name}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RecrawlError(f"recovery run has invalid {field_name}") from exc
    if parsed.tzinfo is None:
        raise RecrawlError(f"recovery run {field_name} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _prior_runtime_storage_for_full_finalization(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    run_started_at: datetime,
    source_sha256: str,
) -> tuple[dict[str, int], dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT id,run_kind,status,started_at,finished_at,summary_json
        FROM runs
        WHERE id<?
          AND status LIKE 'completed%'
          AND source_db_sha256_before=?
          AND source_db_sha256_after=?
        ORDER BY id DESC
        """,
        (run_id, source_sha256, source_sha256),
    ).fetchall()
    for row in rows:
        try:
            summary = json.loads(row["summary_json"] or "{}")
        except json.JSONDecodeError:
            continue
        raw_after = (summary.get("runtime_storage") or {}).get("after")
        if not isinstance(raw_after, Mapping):
            continue
        storage_after: dict[str, int] = {}
        valid = True
        for key in ("state_bytes", "snapshot_bytes", "combined_bytes"):
            value = raw_after.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                valid = False
                break
            storage_after[key] = value
        if not valid or storage_after["combined_bytes"] != (
            storage_after["state_bytes"] + storage_after["snapshot_bytes"]
        ):
            continue
        prior_finished_at = _parse_aware_run_timestamp(
            row["finished_at"], field_name="prior finished_at"
        )
        if prior_finished_at > run_started_at:
            continue
        provenance = {
            "kind": "prior_completed_run_runtime_storage_after",
            "run_id": int(row["id"]),
            "run_kind": row["run_kind"],
            "run_status": row["status"],
            "run_started_at": row["started_at"],
            "run_finished_at": row["finished_at"],
            "summary_sha256": sha256_bytes(
                canonical_json(summary).encode("utf-8")
            ),
        }
        return storage_after, provenance
    raise RecrawlError(
        "no prior completed run has a valid runtime_storage.after baseline"
    )


def _validate_full_finalization_evidence(
    connection: sqlite3.Connection,
    *,
    run: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    if run["run_kind"] != "full_recrawl_v2":
        raise RecrawlError("finalization target is not a full_recrawl_v2 run")
    selected_count = run["selected_count"]
    if (
        isinstance(selected_count, bool)
        or not isinstance(selected_count, int)
        or selected_count < 1
    ):
        raise RecrawlError("failed full run has an invalid selected_count")
    try:
        arguments = json.loads(run["arguments_json"] or "{}")
    except json.JSONDecodeError as exc:
        raise RecrawlError("failed full run arguments are not valid JSON") from exc
    if not isinstance(arguments, dict):
        raise RecrawlError("failed full run arguments must be a JSON object")
    rows = connection.execute(
        """
        SELECT url,selection_order,status_after
        FROM run_targets WHERE run_id=? ORDER BY selection_order
        """,
        (run["id"],),
    ).fetchall()
    if len(rows) != selected_count:
        raise RecrawlError(
            "failed full run selected_count does not match run_targets"
        )
    expected_orders = list(range(1, selected_count + 1))
    if [int(row["selection_order"]) for row in rows] != expected_orders:
        raise RecrawlError("failed full run selection_order is not contiguous")
    if any(row["status_after"] not in {"done", "failed"} for row in rows):
        raise RecrawlError("failed full run still has non-terminal frozen targets")
    frozen_urls = [str(row["url"]) for row in rows]
    frozen_hash = _url_set_sha256(frozen_urls)
    if arguments.get("frozen_target_count") != selected_count:
        raise RecrawlError("failed full run frozen_target_count does not match")
    if arguments.get("frozen_target_urls_sha256") != frozen_hash:
        raise RecrawlError("failed full run frozen target hash does not match")
    metadata_links = int(
        connection.execute(
            "SELECT COUNT(*) FROM run_metadata_versions WHERE run_id=?",
            (run["id"],),
        ).fetchone()[0]
    )
    valid_links = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM run_metadata_versions rm
            JOIN metadata_versions m
              ON m.id=rm.version_id AND m.target_url=rm.target_url
            JOIN run_targets rt
              ON rt.run_id=rm.run_id AND rt.url=rm.target_url
            WHERE rm.run_id=?
            """,
            (run["id"],),
        ).fetchone()[0]
    )
    if metadata_links != selected_count or valid_links != selected_count:
        raise RecrawlError(
            "failed full run selected targets do not match metadata links"
        )
    attempted_targets = int(
        connection.execute(
            """
            SELECT COUNT(DISTINCT target_url)
            FROM http_attempts
            WHERE run_id=? AND target_url IS NOT NULL
            """,
            (run["id"],),
        ).fetchone()[0]
    )
    attempts_outside_frozen_set = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM http_attempts AS h
            WHERE h.run_id=? AND h.target_url IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM run_targets AS rt
                  WHERE rt.run_id=h.run_id AND rt.url=h.target_url
              )
            """,
            (run["id"],),
        ).fetchone()[0]
    )
    if attempted_targets != selected_count or attempts_outside_frozen_set:
        raise RecrawlError(
            "failed full run HTTP attempts do not match frozen targets"
        )
    target_state_mismatches = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM run_targets AS rt
            JOIN targets AS t ON t.url=rt.url
            WHERE rt.run_id=? AND t.status<>rt.status_after
            """,
            (run["id"],),
        ).fetchone()[0]
    )
    done_identity_mismatches = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM run_targets AS rt
            LEFT JOIN run_metadata_versions AS rm
              ON rm.run_id=rt.run_id AND rm.target_url=rt.url
            LEFT JOIN metadata_versions AS m ON m.id=rm.version_id
            WHERE rt.run_id=? AND rt.status_after='done'
              AND COALESCE(m.identity_status,'')<>'valid'
            """,
            (run["id"],),
        ).fetchone()[0]
    )
    if target_state_mismatches or done_identity_mismatches:
        raise RecrawlError(
            "failed full run terminal target evidence is inconsistent"
        )
    return arguments, frozen_urls


def _is_idempotent_full_finalization(
    run: Mapping[str, Any],
) -> dict[str, Any] | None:
    if run["status"] not in {"completed", "completed_with_pending_discoveries"}:
        return None
    try:
        summary = json.loads(run["summary_json"] or "{}")
    except json.JSONDecodeError as exc:
        raise RecrawlError("completed recovery summary is not valid JSON") from exc
    marker = summary.get("postprocess_recovery") if isinstance(summary, dict) else None
    if not isinstance(marker, dict):
        raise RecrawlError("completed full run was not finalized by this recovery")
    expected = {
        "contract_version": FULL_RUN_POSTPROCESS_RECOVERY_VERSION,
        "run_id": int(run["id"]),
        "original_status": "failed",
        "original_error": FULL_RUN_SQL_VARIABLE_ERROR,
        "original_summary": {},
        "original_finished_at": run["finished_at"],
        "network_requests": 0,
    }
    if any(marker.get(key) != value for key, value in expected.items()):
        raise RecrawlError("completed recovery marker does not match the run")
    expected_status = (
        "completed_with_pending_discoveries"
        if summary.get("newly_discovered_pending_count")
        else "completed"
    )
    if run["status"] != expected_status:
        raise RecrawlError("completed recovery status disagrees with its summary")
    return summary


def finalize_full_run(
    *,
    run_id: int,
    confirmed: bool = False,
    source_path: Path = DEFAULT_SOURCE_DB,
    state_path: Path = DEFAULT_STATE_DB,
    snapshot_root: Path = DEFAULT_SNAPSHOT_DIR,
) -> dict[str, Any]:
    """Finalize a fully fetched run that failed only in post-summary SQL."""

    if not confirmed:
        raise RecrawlError(
            "offline full-run finalization requires explicit confirmation"
        )
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id < 1:
        raise ValueError("run_id must be a positive integer")
    source_path = source_path.resolve()
    state_path = state_path.resolve()
    snapshot_root = snapshot_root.resolve()
    validate_runtime_paths(
        source_path=source_path,
        state_path=state_path,
        snapshot_root=snapshot_root,
    )
    if not source_path.exists() or not source_path.is_file():
        raise RecrawlError(f"immutable source DB does not exist: {source_path}")
    if not snapshot_root.exists() or not snapshot_root.is_dir():
        raise RecrawlError(f"snapshot directory does not exist: {snapshot_root}")
    inspection = inspect_sidecar_lock(state_path)
    if inspection.get("exists"):
        raise LockHeldError(
            "sidecar lock exists; inspect or explicitly recover it before "
            "offline finalization"
        )
    source_identity_before = _measure_stable_file_identity(source_path)
    source_sha = str(source_identity_before["sha256"])
    source_size = int(source_identity_before["size"])
    with SidecarLock(state_path):
        connection, schema_version = (
            _open_state_without_migration_for_full_finalization(
                state_path,
                source_path=source_path,
                source_sha256=source_sha,
                source_size=source_size,
            )
        )
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise RecrawlError(f"full run does not exist: {run_id}")
            run = dict(row)
            if run["run_kind"] != "full_recrawl_v2":
                raise RecrawlError("finalization target is not a full_recrawl_v2 run")
            if (
                not run["source_db_path"]
                or not _same_resolved_path(run["source_db_path"], source_path)
                or run["source_db_sha256_before"] != source_sha
                or run["source_db_sha256_after"] != source_sha
                or int(run["source_db_size"] or -1) != source_size
            ):
                raise RecrawlError("failed full run source lineage is not immutable")
            started_at = _parse_aware_run_timestamp(
                run["started_at"], field_name="started_at"
            )
            finished_at = _parse_aware_run_timestamp(
                run["finished_at"], field_name="finished_at"
            )
            if finished_at < started_at:
                raise RecrawlError("failed full run finished before it started")
            existing_summary = _is_idempotent_full_finalization(run)
            if existing_summary is not None:
                source_identity_after = _measure_stable_file_identity(
                    source_path
                )
                _require_same_file_identity(
                    source_identity_before,
                    source_identity_after,
                )
                connection.rollback()
                return existing_summary
            later_run = connection.execute(
                "SELECT id,run_kind,status FROM runs WHERE id>? ORDER BY id LIMIT 1",
                (run_id,),
            ).fetchone()
            if later_run is not None:
                # The failed post-summary transaction did not freeze the
                # pending-discovery state separately.  A later run can mutate
                # both selected targets and newly discovered stubs, so a
                # first-time historical reconstruction would be ambiguous.
                # Already recovered runs returned above remain idempotent.
                raise RecrawlError(
                    "cannot finalize a failed full run after a subsequent run: "
                    f"{later_run['id']} ({later_run['run_kind']}, "
                    f"{later_run['status']})"
                )
            arguments, frozen_urls = _validate_full_finalization_evidence(
                connection,
                run=run,
            )
            if run["status"] != "failed":
                raise RecrawlError("full run is not in the failed recovery state")
            if run["error"] != FULL_RUN_SQL_VARIABLE_ERROR:
                raise RecrawlError("full run failure is not the exact SQL variable error")
            if run["summary_json"] != "{}":
                raise RecrawlError("failed full run summary is not exactly empty")
            delay_seconds = arguments.get("delay_seconds")
            if (
                isinstance(delay_seconds, bool)
                or not isinstance(delay_seconds, (int, float))
                or not math.isfinite(float(delay_seconds))
                or float(delay_seconds) < 0
            ):
                raise RecrawlError("failed full run delay_seconds is invalid")
            storage_before, storage_provenance = (
                _prior_runtime_storage_for_full_finalization(
                    connection,
                    run_id=run_id,
                    run_started_at=started_at,
                    source_sha256=source_sha,
                )
            )
            summary = _network_run_summary(
                connection,
                run_id=run_id,
                source_sha_before=source_sha,
                source_sha_after=source_sha,
                delay_seconds=float(delay_seconds),
                state_path=state_path,
                snapshot_root=snapshot_root,
                storage_before=storage_before,
                elapsed_seconds=(finished_at - started_at).total_seconds(),
            )
            expanded_counts = arguments.get("expanded_counts")
            if not isinstance(expanded_counts, dict):
                raise RecrawlError("failed full run expanded_counts is missing")
            pending_discoveries = _pending_discoveries_for_run(
                connection,
                run_id=run_id,
            )
            pending_urls = [row["url"] for row in pending_discoveries]
            summary["expanded_current_sitemap_counts"] = expanded_counts
            summary["frozen_target_count"] = len(frozen_urls)
            summary["frozen_target_urls_sha256"] = _url_set_sha256(frozen_urls)
            summary["newly_discovered_pending_count"] = len(pending_discoveries)
            summary["newly_discovered_pending_urls_sha256"] = _url_set_sha256(
                pending_urls
            )
            summary["newly_discovered_pending_by_entity_type"] = dict(
                sorted(
                    Counter(
                        row["entity_type"] for row in pending_discoveries
                    ).items()
                )
            )
            summary["additional_full_phase_approval_required"] = bool(
                pending_discoveries
            )
            summary["runtime_storage"]["before_provenance"] = storage_provenance
            source_identity_after = _measure_stable_file_identity(source_path)
            _require_same_file_identity(
                source_identity_before,
                source_identity_after,
            )
            summary["input_db_size_before"] = source_size
            summary["input_db_size_after"] = int(
                source_identity_after["size"]
            )
            recovered_at = utc_now()
            summary["postprocess_recovery"] = {
                "contract_version": FULL_RUN_POSTPROCESS_RECOVERY_VERSION,
                "run_id": run_id,
                "recovered_at": recovered_at,
                "network_requests": 0,
                "sidecar_schema_version": schema_version,
                "elapsed_source": "runs.started_at_to_original_finished_at",
                "original_status": "failed",
                "original_error": run["error"],
                "original_summary": {},
                "original_finished_at": run["finished_at"],
                "runtime_storage_before_provenance": storage_provenance,
                "source_identity_before": dict(source_identity_before),
                "source_identity_after": dict(source_identity_after),
            }
            run_status = (
                "completed_with_pending_discoveries"
                if pending_discoveries
                else "completed"
            )
            cursor = connection.execute(
                """
                UPDATE runs
                SET status=?,summary_json=?,error=NULL
                WHERE id=?
                  AND run_kind='full_recrawl_v2'
                  AND status='failed'
                  AND error=?
                  AND summary_json='{}'
                  AND finished_at=?
                  AND source_db_sha256_before=?
                  AND source_db_sha256_after=?
                  AND selected_count=?
                """,
                (
                    run_status,
                    canonical_json(summary),
                    run_id,
                    FULL_RUN_SQL_VARIABLE_ERROR,
                    run["finished_at"],
                    source_sha,
                    source_sha,
                    len(frozen_urls),
                ),
            )
            if cursor.rowcount != 1:
                raise RecrawlError("failed full run changed during finalization")
            connection.commit()
            return summary
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def write_json_no_clobber(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(
            payload,
            handle,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        handle.write("\n")


def render_network_report(summary: Mapping[str, Any], title: str) -> str:
    lines = [
        f"# {title}",
        "",
        f"- Run ID: `{summary['run_id']}`",
        f"- Selected: {summary['selected']:,}",
        (
            f"- HTTP success: {summary['http_success']:,}/"
            f"{summary['selected']:,} "
            f"({summary['http_success_rate']:.1%})"
        ),
        (
            f"- Valid identity: {summary['identity_valid']:,}/"
            f"{summary['selected']:,} "
            f"({summary['identity_valid_rate']:.1%})"
        ),
        f"- Snapshot saved: {summary['snapshot_saved']:,}",
        (
            "- Input DB unchanged: "
            f"{'yes' if summary['input_db_unchanged'] else 'NO'}"
        ),
        (
            f"- Duration: {summary['run_elapsed_seconds']:.3f}s; "
            f"median response {summary['duration_ms']['median']:.1f}ms"
        ),
        (
            "- Recommended delay: "
            f"{summary['recommended_request_delay_seconds']:.1f}s "
            f"({summary['recommended_delay_reason']})"
        ),
    ]
    if "newly_discovered_pending_count" in summary:
        lines.extend(
            [
                "",
                "## Full discovery gate",
                "",
                (
                    "- Frozen target count: "
                    f"{int(summary.get('frozen_target_count') or 0):,}"
                ),
                (
                    "- Frozen target URL-set SHA-256: `"
                    f"{summary.get('frozen_target_urls_sha256') or ''}`"
                ),
                (
                    "- Newly discovered pending: "
                    f"{int(summary['newly_discovered_pending_count']):,}"
                ),
                (
                    "- Pending by entity type: "
                    f"{canonical_json(summary.get('newly_discovered_pending_by_entity_type', {}))}"
                ),
                (
                    "- Pending URL-set SHA-256: `"
                    f"{summary.get('newly_discovered_pending_urls_sha256') or ''}`"
                ),
                (
                    "- Additional full-phase approval required: "
                    f"{'yes' if summary.get('additional_full_phase_approval_required') else 'no'}"
                ),
            ]
        )
    lines.extend(["", "## Parse outcomes", ""])
    for key, value in summary["parse_status_counts"].items():
        lines.append(f"- {key}: {value:,}")
    lines.extend(["", "## Selection types", ""])
    for key, values in summary["type_stats"].items():
        lines.append(f"- {key}: {canonical_json(values)}")
    lines.extend(["", "## Field coverage", ""])
    for key, value in summary["field_coverage_counts"].items():
        rate = summary["field_coverage_rates"][key]
        denominator = summary.get("field_coverage_denominators", {}).get(
            key, summary["selected"]
        )
        lines.append(f"- {key}: {value:,}/{denominator:,} ({rate:.1%})")
    lines.extend(
        [
            "",
            "## Block/login/error signals",
            "",
            (
                canonical_json(summary["block_signal_counts"])
                if summary["block_signal_counts"]
                else "- None"
            ),
            "",
            "## Legacy field comparisons",
            "",
            canonical_json(summary["legacy_field_comparison_counts"]),
            "",
        ]
    )
    return "\n".join(lines)


def write_text_no_clobber(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        if not text.endswith("\n"):
            handle.write("\n")
