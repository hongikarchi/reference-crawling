"""Resumable authenticated HTML recrawl for Divisare metadata v2.4."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

from requests.exceptions import TooManyRedirects


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import config  # noqa: E402
from core.utils import RateLimiter  # noqa: E402
from crawl.divisare.auth import do_login, get_authenticated_session  # noqa: E402
from crawl.divisare.metadata_v2 import (  # noqa: E402
    PARSER_VERSION,
    looks_like_login_wall,
    parse_project_metadata,
)


CRAWLER_VERSION = "divisare-metadata-recrawl-v2.4.1"
STATE_SCHEMA_VERSION = 1
EXPECTED_PARENT_SCHEMA = 4
EXPECTED_METADATA_VERSION = "divisare-metadata-v2.1"
FETCH_STATUSES = (
    "pending",
    "running",
    "success",
    "not_modified",
    "not_found",
    "blocked",
    "failed",
)
PARSE_STATUSES = (
    "pending",
    "success",
    "partial",
    "no_content",
    "failed",
    "skipped",
)


class AuthenticationExpiredError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def json_dumps(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@contextmanager
def exclusive_state_lock(state_path: Path) -> Iterator[None]:
    lock_path = state_path.with_name(state_path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise RuntimeError(
            "recrawl state is already locked by another process: %s"
            % lock_path
        ) from exc
    try:
        yield
    finally:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def open_parent(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        "file:%s?mode=ro" % path.resolve().as_posix(),
        uri=True,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def validate_parent(conn: sqlite3.Connection) -> Dict[str, Any]:
    user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if user_version != EXPECTED_PARENT_SCHEMA:
        raise RuntimeError(
            "expected metadata parent schema %d, found %d"
            % (EXPECTED_PARENT_SCHEMA, user_version)
        )
    lineage = conn.execute(
        """
        SELECT *
        FROM artifact_lineage_v2
        WHERE lineage_id=1
        """
    ).fetchone()
    if lineage is None:
        raise RuntimeError("metadata parent has no artifact_lineage_v2")
    if lineage["metadata_version"] != EXPECTED_METADATA_VERSION:
        raise RuntimeError(
            "expected %s, found %s"
            % (EXPECTED_METADATA_VERSION, lineage["metadata_version"])
        )
    queue_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM article_recrawl_queue_v2"
        ).fetchone()[0]
    )
    article_count = int(
        conn.execute("SELECT COUNT(*) FROM source_articles").fetchone()[0]
    )
    if queue_count != article_count:
        raise RuntimeError(
            "recrawl queue/article mismatch: %d/%d"
            % (queue_count, article_count)
        )
    return {
        "user_version": user_version,
        "lineage": dict(lineage),
        "queue_count": queue_count,
        "article_count": article_count,
    }


STATE_SCHEMA_SQL = """
CREATE TABLE recrawl_lineage (
    lineage_id                 INTEGER PRIMARY KEY CHECK(lineage_id=1),
    parent_db_path             TEXT NOT NULL,
    parent_sha256              TEXT NOT NULL CHECK(length(parent_sha256)=64),
    parent_metadata_version    TEXT NOT NULL,
    parent_schema_version      INTEGER NOT NULL,
    crawler_version            TEXT NOT NULL,
    parser_version             TEXT NOT NULL,
    snapshot_root              TEXT NOT NULL,
    created_at                 TEXT NOT NULL
);

CREATE TABLE recrawl_runs (
    run_id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at                 TEXT NOT NULL,
    completed_at               TEXT,
    status                     TEXT NOT NULL
        CHECK(status IN ('running','complete','failed')),
    max_items                  INTEGER,
    delay_seconds              REAL NOT NULL,
    refresh_mode               INTEGER NOT NULL CHECK(refresh_mode IN (0,1)),
    processed                  INTEGER NOT NULL DEFAULT 0,
    metrics_json               TEXT CHECK(metrics_json IS NULL OR json_valid(metrics_json)),
    error                      TEXT
);

CREATE TABLE article_html_jobs (
    article_id                 INTEGER PRIMARY KEY,
    source_url                 TEXT NOT NULL,
    priority                   INTEGER NOT NULL,
    reasons_json               TEXT NOT NULL CHECK(json_valid(reasons_json)),
    fetch_status               TEXT NOT NULL DEFAULT 'pending'
        CHECK(fetch_status IN (
          'pending','running','success','not_modified',
          'not_found','blocked','failed'
        )),
    parse_status               TEXT NOT NULL DEFAULT 'pending'
        CHECK(parse_status IN (
          'pending','success','partial','no_content','failed','skipped'
        )),
    attempt_count              INTEGER NOT NULL DEFAULT 0,
    http_status                INTEGER,
    final_url                  TEXT,
    etag                       TEXT,
    last_modified              TEXT,
    content_type               TEXT,
    current_html_sha256        TEXT,
    snapshot_path              TEXT,
    html_byte_size             INTEGER,
    queued_at                  TEXT NOT NULL,
    started_at                 TEXT,
    fetched_at                 TEXT,
    parsed_at                  TEXT,
    last_error                 TEXT,
    updated_at                 TEXT NOT NULL
);

CREATE TABLE article_html_snapshots (
    snapshot_id                INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id                 INTEGER NOT NULL REFERENCES article_html_jobs(article_id),
    html_sha256                TEXT NOT NULL CHECK(length(html_sha256)=64),
    byte_size                  INTEGER NOT NULL,
    snapshot_path              TEXT NOT NULL,
    http_status                INTEGER NOT NULL,
    final_url                  TEXT NOT NULL,
    response_headers_json      TEXT NOT NULL CHECK(json_valid(response_headers_json)),
    fetched_at                 TEXT NOT NULL,
    run_id                     INTEGER NOT NULL REFERENCES recrawl_runs(run_id),
    is_current                 INTEGER NOT NULL CHECK(is_current IN (0,1)),
    UNIQUE(article_id,html_sha256)
);

CREATE UNIQUE INDEX uq_article_html_snapshot_current
ON article_html_snapshots(article_id)
WHERE is_current=1;

CREATE TABLE article_metadata_versions (
    metadata_id                INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id                 INTEGER NOT NULL REFERENCES article_html_jobs(article_id),
    snapshot_id                INTEGER NOT NULL REFERENCES article_html_snapshots(snapshot_id),
    name                       TEXT,
    abstract                   TEXT,
    location_country           TEXT,
    location_city              TEXT,
    project_year               INTEGER,
    area_sqm                   REAL,
    area_raw                   TEXT,
    description_prose          TEXT,
    description_quality        TEXT NOT NULL,
    explicit_article_kind      TEXT,
    explicit_article_kind_raw  TEXT,
    parser_version             TEXT NOT NULL,
    details_json               TEXT NOT NULL CHECK(json_valid(details_json)),
    parsed_at                  TEXT NOT NULL,
    is_current                 INTEGER NOT NULL CHECK(is_current IN (0,1)),
    UNIQUE(article_id,snapshot_id,parser_version)
);

CREATE UNIQUE INDEX uq_article_metadata_current
ON article_metadata_versions(article_id)
WHERE is_current=1;

CREATE INDEX idx_html_jobs_work
ON article_html_jobs(fetch_status,priority DESC,article_id);
CREATE INDEX idx_metadata_area
ON article_metadata_versions(area_sqm)
WHERE is_current=1;
CREATE INDEX idx_metadata_kind
ON article_metadata_versions(explicit_article_kind)
WHERE is_current=1;
"""


def initialize_state(
    *,
    state_path: Path,
    parent_path: Path,
    parent_sha256: str,
    parent_info: Mapping[str, Any],
    snapshot_root: Path,
) -> sqlite3.Connection:
    existed = state_path.exists()
    conn = sqlite3.connect(state_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    if not existed:
        conn.executescript(STATE_SCHEMA_SQL)
        conn.execute("PRAGMA user_version=%d" % STATE_SCHEMA_VERSION)
        conn.execute(
            """
            INSERT INTO recrawl_lineage(
                lineage_id,parent_db_path,parent_sha256,
                parent_metadata_version,parent_schema_version,
                crawler_version,parser_version,snapshot_root,created_at
            ) VALUES (1,?,?,?,?,?,?,?,?)
            """,
            (
                str(parent_path),
                parent_sha256,
                parent_info["lineage"]["metadata_version"],
                parent_info["user_version"],
                CRAWLER_VERSION,
                PARSER_VERSION,
                str(snapshot_root),
                utc_now(),
            ),
        )
        conn.commit()
    else:
        lineage = conn.execute(
            "SELECT * FROM recrawl_lineage WHERE lineage_id=1"
        ).fetchone()
        if lineage is None:
            raise RuntimeError("existing recrawl DB has no lineage")
        if lineage["parent_sha256"] != parent_sha256:
            raise RuntimeError(
                "existing recrawl DB belongs to a different parent artifact"
            )
        if int(conn.execute("PRAGMA user_version").fetchone()[0]) != STATE_SCHEMA_VERSION:
            raise RuntimeError("unsupported recrawl state schema")
    recovery_time = utc_now()
    conn.execute(
        """
        UPDATE recrawl_runs
        SET completed_at=?,
            status='failed',
            error=COALESCE(error,'interrupted before resume')
        WHERE status='running'
        """,
        (recovery_time,),
    )
    conn.execute(
        """
        UPDATE article_html_jobs
        SET fetch_status='pending',
            parse_status='pending',
            last_error='recovered interrupted running job',
            updated_at=?
        WHERE fetch_status='running'
        """,
        (recovery_time,),
    )
    conn.commit()
    return conn


def seed_jobs(
    parent: sqlite3.Connection,
    state: sqlite3.Connection,
    *,
    seed_limit: Optional[int],
) -> int:
    query = """
        SELECT article_id,source_url,priority,reasons_json
        FROM article_recrawl_queue_v2
        ORDER BY priority DESC,article_id
    """
    params: Tuple[Any, ...] = ()
    if seed_limit is not None:
        if seed_limit <= 0:
            raise ValueError("--seed-limit must be positive")
        query += " LIMIT ?"
        params = (seed_limit,)
    now = utc_now()
    rows = [
        (
            row["article_id"],
            row["source_url"],
            row["priority"],
            row["reasons_json"],
            now,
            now,
        )
        for row in parent.execute(query, params)
    ]
    existing_ids = {
        int(row["article_id"])
        for row in state.execute("SELECT article_id FROM article_html_jobs")
    }
    incoming_ids = {int(row[0]) for row in rows}
    if existing_ids and existing_ids != incoming_ids:
        raise RuntimeError(
            "existing recrawl state has a different seed scope; "
            "reuse the original --seed-limit or create a new state DB"
        )
    state.executemany(
        """
        INSERT INTO article_html_jobs(
            article_id,source_url,priority,reasons_json,queued_at,updated_at
        ) VALUES (?,?,?,?,?,?)
        ON CONFLICT(article_id) DO UPDATE SET
          source_url=excluded.source_url,
          priority=excluded.priority,
          reasons_json=excluded.reasons_json,
          updated_at=excluded.updated_at
        """,
        rows,
    )
    state.commit()
    return len(rows)


def snapshot_path_for(
    snapshot_root: Path,
    article_id: int,
    html_sha256: str,
) -> Path:
    return (
        snapshot_root
        / ("%03d" % (article_id % 1000))
        / ("%s_%s.html.gz" % (article_id, html_sha256[:16]))
    )


def write_snapshot(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(temp_path, "wb", compresslevel=6) as handle:
        handle.write(content)
    os.replace(temp_path, path)


def parse_status_for(parsed: Any) -> str:
    if not parsed.details.get("has_project_dom"):
        return "failed"
    if parsed.details.get("project_dom_count") != 1:
        return "failed"
    if not parsed.details.get("project_dom_id_matches_url"):
        return "failed"
    if not parsed.details.get("page_url_matches_expected"):
        return "failed"
    if parsed.description_quality in {"no_description_dom", "no_prose_content"}:
        return "no_content"
    if parsed.description_quality == "dom_text_fallback_review":
        return "partial"
    return "success"


def parse_error_for(parsed: Any, status: str) -> Optional[str]:
    if status != "failed":
        return None
    if not parsed.details.get("has_project_dom"):
        return "project DOM missing from successful HTTP response"
    if parsed.details.get("project_dom_count") != 1:
        return "expected exactly one project DOM"
    if not parsed.details.get("project_dom_id_matches_url"):
        return "project DOM ID does not match requested project URL"
    if not parsed.details.get("page_url_matches_expected"):
        return "final URL does not match requested project ID"
    return "metadata parse failed"


def update_failure_streaks(
    fetch_status: str,
    *,
    consecutive_blocked: int,
    consecutive_failures: int,
    max_consecutive_blocked: int,
    max_consecutive_failures: int,
) -> Tuple[int, int]:
    blocked = consecutive_blocked + 1 if fetch_status == "blocked" else 0
    failures = (
        consecutive_failures + 1
        if fetch_status in {"blocked", "failed"}
        else 0
    )
    if blocked >= max_consecutive_blocked:
        raise RuntimeError(
            "crawl circuit breaker: %d consecutive blocked responses"
            % blocked
        )
    if failures >= max_consecutive_failures:
        raise RuntimeError(
            "crawl circuit breaker: %d consecutive blocked/failed responses"
            % failures
        )
    return blocked, failures


def circuit_breaker_status(fetch_status: str, parse_status: str) -> str:
    return "failed" if parse_status == "failed" else fetch_status


def save_metadata_version(
    state: sqlite3.Connection,
    *,
    job: Mapping[str, Any],
    snapshot_id: int,
    content: bytes,
    page_url: Optional[str] = None,
) -> Tuple[str, Optional[str]]:
    parsed = parse_project_metadata(
        content.decode("utf-8", errors="replace"),
        page_url or job["source_url"],
        expected_article_id=int(job["article_id"]),
    )
    status = parse_status_for(parsed)
    make_current = status != "failed"
    now = utc_now()
    if make_current:
        state.execute(
            """
            UPDATE article_metadata_versions
            SET is_current=0
            WHERE article_id=? AND is_current=1
            """,
            (job["article_id"],),
        )
    state.execute(
        """
        INSERT INTO article_metadata_versions(
            article_id,snapshot_id,name,abstract,location_country,
            location_city,project_year,area_sqm,area_raw,description_prose,
            description_quality,explicit_article_kind,
            explicit_article_kind_raw,parser_version,details_json,
            parsed_at,is_current
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(article_id,snapshot_id,parser_version) DO UPDATE SET
          name=excluded.name,
          abstract=excluded.abstract,
          location_country=excluded.location_country,
          location_city=excluded.location_city,
          project_year=excluded.project_year,
          area_sqm=excluded.area_sqm,
          area_raw=excluded.area_raw,
          description_prose=excluded.description_prose,
          description_quality=excluded.description_quality,
          explicit_article_kind=excluded.explicit_article_kind,
          explicit_article_kind_raw=excluded.explicit_article_kind_raw,
          details_json=excluded.details_json,
          parsed_at=excluded.parsed_at,
          is_current=excluded.is_current
        """,
        (
            job["article_id"],
            snapshot_id,
            parsed.name,
            parsed.abstract,
            parsed.location_country,
            parsed.location_city,
            parsed.project_year,
            parsed.area_sqm,
            parsed.area_raw,
            parsed.description_prose,
            parsed.description_quality,
            parsed.explicit_article_kind,
            parsed.explicit_article_kind_raw,
            PARSER_VERSION,
            json_dumps(parsed.details),
            now,
            int(make_current),
        ),
    )
    return status, parse_error_for(parsed, status)


def save_success(
    state: sqlite3.Connection,
    *,
    run_id: int,
    job: sqlite3.Row,
    response: Any,
    snapshot_root: Path,
) -> str:
    content = bytes(response.content)
    html_sha256 = hashlib.sha256(content).hexdigest()
    snapshot_path = snapshot_path_for(
        snapshot_root,
        int(job["article_id"]),
        html_sha256,
    )
    write_snapshot(snapshot_path, content)
    now = utc_now()
    state.execute(
        """
        UPDATE article_html_snapshots
        SET is_current=0
        WHERE article_id=? AND is_current=1
        """,
        (job["article_id"],),
    )
    headers = {
        key: response.headers.get(key)
        for key in (
            "Content-Type",
            "ETag",
            "Last-Modified",
            "Cache-Control",
        )
        if response.headers.get(key) is not None
    }
    state.execute(
        """
        INSERT INTO article_html_snapshots(
            article_id,html_sha256,byte_size,snapshot_path,http_status,
            final_url,response_headers_json,fetched_at,run_id,is_current
        ) VALUES (?,?,?,?,?,?,?,?,?,1)
        ON CONFLICT(article_id,html_sha256) DO UPDATE SET
          snapshot_path=excluded.snapshot_path,
          http_status=excluded.http_status,
          final_url=excluded.final_url,
          response_headers_json=excluded.response_headers_json,
          fetched_at=excluded.fetched_at,
          run_id=excluded.run_id,
          is_current=1
        """,
        (
            job["article_id"],
            html_sha256,
            len(content),
            str(snapshot_path),
            response.status_code,
            response.url,
            json_dumps(headers),
            now,
            run_id,
        ),
    )
    snapshot_id = int(
        state.execute(
            """
            SELECT snapshot_id
            FROM article_html_snapshots
            WHERE article_id=? AND html_sha256=?
            """,
            (job["article_id"], html_sha256),
        ).fetchone()[0]
    )

    status, error = save_metadata_version(
        state,
        job=job,
        snapshot_id=snapshot_id,
        content=content,
        page_url=response.url,
    )
    state.execute(
        """
        UPDATE article_html_jobs
        SET fetch_status='success',
            parse_status=?,
            http_status=?,
            final_url=?,
            etag=?,
            last_modified=?,
            content_type=?,
            current_html_sha256=?,
            snapshot_path=?,
            html_byte_size=?,
            fetched_at=?,
            parsed_at=?,
            last_error=?,
            updated_at=?
        WHERE article_id=?
        """,
        (
            status,
            response.status_code,
            response.url,
            response.headers.get("ETag"),
            response.headers.get("Last-Modified"),
            response.headers.get("Content-Type"),
            html_sha256,
            str(snapshot_path),
            len(content),
            now,
            now,
            error,
            now,
            job["article_id"],
        ),
    )
    return status


def reparse_current_snapshots(
    state: sqlite3.Connection,
    *,
    max_items: Optional[int],
    run_id: Optional[int] = None,
) -> int:
    query = """
        SELECT
            j.*,
            s.snapshot_id,
            s.snapshot_path AS current_snapshot_path,
            s.final_url AS current_snapshot_final_url,
            s.html_sha256 AS snapshot_html_sha256,
            s.byte_size AS snapshot_byte_size
        FROM article_html_jobs AS j
        JOIN article_html_snapshots AS s
          ON s.article_id=j.article_id AND s.is_current=1
        WHERE NOT EXISTS (
          SELECT 1
          FROM article_metadata_versions AS m
          WHERE m.article_id=j.article_id
            AND m.snapshot_id=s.snapshot_id
            AND m.parser_version=?
        )
        ORDER BY j.priority DESC,j.article_id
    """
    params: Tuple[Any, ...] = (PARSER_VERSION,)
    if max_items is not None:
        query += " LIMIT ?"
        params = (PARSER_VERSION, max_items)
    rows = list(state.execute(query, params))
    processed = 0
    for row in rows:
        path = Path(row["current_snapshot_path"])
        with gzip.open(path, "rb") as handle:
            content = handle.read()
        actual_sha256 = hashlib.sha256(content).hexdigest()
        if (
            actual_sha256 != row["snapshot_html_sha256"]
            or len(content) != int(row["snapshot_byte_size"])
        ):
            raise RuntimeError(
                "snapshot integrity mismatch for article %s"
                % row["article_id"]
            )
        status, error = save_metadata_version(
            state,
            job=row,
            snapshot_id=int(row["snapshot_id"]),
            content=content,
            page_url=row["current_snapshot_final_url"],
        )
        now = utc_now()
        state.execute(
            """
            UPDATE article_html_jobs
            SET parse_status=?,
                parsed_at=?,
                last_error=?,
                updated_at=?
            WHERE article_id=?
            """,
            (status, now, error, now, row["article_id"]),
        )
        if run_id is not None:
            state.execute(
                "UPDATE recrawl_runs SET processed=? WHERE run_id=?",
                (processed + 1, run_id),
            )
        state.commit()
        processed += 1
    return processed


def terminal_fetch_status(status_code: int) -> str:
    if status_code in {404, 410}:
        return "not_found"
    if status_code in {401, 403}:
        return "blocked"
    return "failed"


def mark_terminal(
    state: sqlite3.Connection,
    *,
    article_id: int,
    fetch_status: str,
    parse_status: str,
    http_status: Optional[int],
    final_url: Optional[str],
    error: str,
) -> None:
    now = utc_now()
    state.execute(
        """
        UPDATE article_html_jobs
        SET fetch_status=?,
            parse_status=?,
            http_status=?,
            final_url=?,
            fetched_at=?,
            parsed_at=CASE WHEN ?='skipped' THEN ? ELSE parsed_at END,
            last_error=?,
            updated_at=?
        WHERE article_id=?
        """,
        (
            fetch_status,
            parse_status,
            http_status,
            final_url,
            now,
            parse_status,
            now,
            error[:2000],
            now,
            article_id,
        ),
    )


def fetch_job(
    state: sqlite3.Connection,
    *,
    run_id: int,
    job: sqlite3.Row,
    session: Any,
    rate_limiter: RateLimiter,
    snapshot_root: Path,
    refresh: bool,
) -> Tuple[str, str]:
    article_id = int(job["article_id"])
    now = utc_now()
    state.execute(
        """
        UPDATE article_html_jobs
        SET fetch_status='running',
            attempt_count=attempt_count+1,
            started_at=?,
            last_error=NULL,
            updated_at=?
        WHERE article_id=?
        """,
        (now, now, article_id),
    )
    state.commit()
    headers: Dict[str, str] = {}
    if refresh and job["etag"]:
        headers["If-None-Match"] = job["etag"]
    if refresh and job["last_modified"]:
        headers["If-Modified-Since"] = job["last_modified"]

    last_error = ""
    for attempt in range(1, 4):
        rate_limiter.wait()
        try:
            response = session.get(
                job["source_url"],
                headers=headers,
                timeout=config.REQUEST_TIMEOUT,
                allow_redirects=True,
            )
        except TooManyRedirects as exc:
            raise AuthenticationExpiredError(
                "Divisare redirect loop detected for article %s"
                % article_id
            ) from exc
        except Exception as exc:
            last_error = "%s: %s" % (type(exc).__name__, exc)
            if attempt < 3:
                time.sleep(min(2 ** attempt, 10))
                continue
            mark_terminal(
                state,
                article_id=article_id,
                fetch_status="failed",
                parse_status="skipped",
                http_status=None,
                final_url=None,
                error=last_error,
            )
            state.commit()
            return "failed", "skipped"

        if response.status_code == 304:
            if not job["current_html_sha256"] or not job["snapshot_path"]:
                mark_terminal(
                    state,
                    article_id=article_id,
                    fetch_status="failed",
                    parse_status="skipped",
                    http_status=304,
                    final_url=response.url,
                    error="HTTP 304 without a prior current snapshot",
                )
                state.commit()
                return "failed", "skipped"
            state.execute(
                """
                UPDATE article_html_jobs
                SET fetch_status='not_modified',
                    http_status=304,
                    final_url=?,
                    fetched_at=?,
                    last_error=NULL,
                    updated_at=?
                WHERE article_id=?
                """,
                (response.url, utc_now(), utc_now(), article_id),
            )
            state.commit()
            return "not_modified", job["parse_status"]

        if response.status_code == 200:
            if looks_like_login_wall(
                response.text,
                response.url,
                response.status_code,
            ):
                raise AuthenticationExpiredError(
                    "Divisare login wall detected for article %s"
                    % article_id
                )
            parse_status = save_success(
                state,
                run_id=run_id,
                job=job,
                response=response,
                snapshot_root=snapshot_root,
            )
            state.commit()
            return "success", parse_status

        if response.status_code in {401, 403}:
            raise AuthenticationExpiredError(
                "Divisare authentication failed with HTTP %s for article %s"
                % (response.status_code, article_id)
            )
        if response.status_code == 429 and attempt < 3:
            retry_after = response.headers.get("Retry-After", "30")
            try:
                wait_seconds = min(max(int(retry_after), 1), 120)
            except ValueError:
                wait_seconds = 30
            time.sleep(wait_seconds)
            continue
        if response.status_code >= 500 and attempt < 3:
            time.sleep(min(2 ** attempt, 10))
            continue

        fetch_status = terminal_fetch_status(response.status_code)
        mark_terminal(
            state,
            article_id=article_id,
            fetch_status=fetch_status,
            parse_status="skipped",
            http_status=response.status_code,
            final_url=response.url,
            error="terminal HTTP %s" % response.status_code,
        )
        state.commit()
        return fetch_status, "skipped"

    mark_terminal(
        state,
        article_id=article_id,
        fetch_status="failed",
        parse_status="skipped",
        http_status=None,
        final_url=None,
        error=last_error or "retry loop exhausted",
    )
    state.commit()
    return "failed", "skipped"


def collect_metrics(
    state: sqlite3.Connection,
    parent: sqlite3.Connection,
) -> Dict[str, Any]:
    metrics: Dict[str, Any] = {
        "jobs": int(
            state.execute("SELECT COUNT(*) FROM article_html_jobs").fetchone()[0]
        ),
        "snapshots": int(
            state.execute(
                "SELECT COUNT(*) FROM article_html_snapshots"
            ).fetchone()[0]
        ),
        "metadata_versions": int(
            state.execute(
                "SELECT COUNT(*) FROM article_metadata_versions"
            ).fetchone()[0]
        ),
    }
    metrics["fetch_status"] = {
        row["fetch_status"]: int(row["n"])
        for row in state.execute(
            """
            SELECT fetch_status,COUNT(*) AS n
            FROM article_html_jobs
            GROUP BY fetch_status
            ORDER BY fetch_status
            """
        )
    }
    metrics["parse_status"] = {
        row["parse_status"]: int(row["n"])
        for row in state.execute(
            """
            SELECT parse_status,COUNT(*) AS n
            FROM article_html_jobs
            GROUP BY parse_status
            ORDER BY parse_status
            """
        )
    }
    current = list(
        state.execute(
            """
            SELECT *
            FROM article_metadata_versions
            WHERE is_current=1
            ORDER BY article_id
            """
        )
    )
    metrics["current_metadata"] = len(current)
    metrics["with_area"] = sum(row["area_sqm"] is not None for row in current)
    metrics["with_description"] = sum(
        bool(row["description_prose"]) for row in current
    )
    metrics["with_explicit_article_kind"] = sum(
        row["explicit_article_kind"] is not None for row in current
    )
    metrics["description_quality"] = {}
    for row in current:
        quality = row["description_quality"]
        metrics["description_quality"][quality] = (
            metrics["description_quality"].get(quality, 0) + 1
        )

    if current:
        parent_rows = {
            int(row["article_id"]): row
            for row in parent.execute(
                """
                SELECT article_id,name_raw,location_country,location_city,
                       project_year,area_sqm
                FROM source_articles
                WHERE article_id IN (%s)
                """
                % ",".join("?" for _ in current),
                tuple(int(row["article_id"]) for row in current),
            )
        }
        metrics["name_conflicts"] = 0
        metrics["country_conflicts"] = 0
        metrics["city_conflicts"] = 0
        metrics["year_conflicts"] = 0
        for row in current:
            old = parent_rows[int(row["article_id"])]
            if row["name"] and old["name_raw"]:
                metrics["name_conflicts"] += (
                    row["name"].casefold() != old["name_raw"].casefold()
                )
            for metric, new_column, old_column in (
                ("country_conflicts", "location_country", "location_country"),
                ("city_conflicts", "location_city", "location_city"),
            ):
                new_value = row[new_column]
                old_value = old[old_column]
                if new_value and old_value:
                    metrics[metric] += new_value.casefold() != old_value.casefold()
            if row["project_year"] is not None and old["project_year"] is not None:
                metrics["year_conflicts"] += (
                    int(row["project_year"]) != int(old["project_year"])
                )
    return metrics


def validate_state(state: sqlite3.Connection) -> Dict[str, Any]:
    checks: Dict[str, Any] = {}
    checks["integrity"] = state.execute("PRAGMA integrity_check").fetchone()[0]
    checks["foreign_key_errors"] = len(
        state.execute("PRAGMA foreign_key_check").fetchall()
    )
    checks["duplicate_current_snapshots"] = int(
        state.execute(
            """
            SELECT COUNT(*)
            FROM (
              SELECT article_id
              FROM article_html_snapshots
              WHERE is_current=1
              GROUP BY article_id
              HAVING COUNT(*)<>1
            )
            """
        ).fetchone()[0]
    )
    checks["duplicate_current_metadata"] = int(
        state.execute(
            """
            SELECT COUNT(*)
            FROM (
              SELECT article_id
              FROM article_metadata_versions
              WHERE is_current=1
              GROUP BY article_id
              HAVING COUNT(*)<>1
            )
            """
        ).fetchone()[0]
    )
    checks["success_without_snapshot"] = int(
        state.execute(
            """
            SELECT COUNT(*)
            FROM article_html_jobs
            WHERE fetch_status='success'
              AND (
                http_status<>200
                OR current_html_sha256 IS NULL
                OR snapshot_path IS NULL
                OR html_byte_size IS NULL
              )
            """
        ).fetchone()[0]
    )
    checks["terminal_without_error"] = int(
        state.execute(
            """
            SELECT COUNT(*)
            FROM article_html_jobs
            WHERE fetch_status IN ('blocked','failed')
              AND COALESCE(last_error,'')=''
            """
        ).fetchone()[0]
    )
    checks["pending_with_fetch_result"] = int(
        state.execute(
            """
            SELECT COUNT(*)
            FROM article_html_jobs
            WHERE fetch_status='pending'
              AND (
                http_status IS NOT NULL
                OR current_html_sha256 IS NOT NULL
                OR fetched_at IS NOT NULL
              )
            """
        ).fetchone()[0]
    )
    failed = {
        name: value
        for name, value in checks.items()
        if (name == "integrity" and value != "ok")
        or (name != "integrity" and value != 0)
    }
    if failed:
        raise RuntimeError("recrawl state validation failed: %s" % failed)
    return checks


def write_report(
    path: Path,
    *,
    parent_path: Path,
    state_path: Path,
    snapshot_root: Path,
    metrics: Mapping[str, Any],
    validation: Mapping[str, Any],
    elapsed_seconds: float,
    run_mode: str,
) -> None:
    lines = [
        "# Divisare metadata HTML recrawl",
        "",
        "- Crawler: `%s`" % CRAWLER_VERSION,
        "- Parser: `%s`" % PARSER_VERSION,
        "- Parent: `%s`" % parent_path,
        "- State DB: `%s`" % state_path,
        "- Snapshot root: `%s`" % snapshot_root,
        "- Run mode: `%s`" % run_mode,
        "- Elapsed: `%.2f seconds`" % elapsed_seconds,
        "- API/LLM cost: `$0`",
        "",
        "## Metrics",
        "",
        "```json",
        json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True),
        "```",
        "",
        "## Validation",
        "",
        "```json",
        json.dumps(validation, ensure_ascii=False, indent=2, sort_keys=True),
        "```",
        "",
        "Credits and image semantics are intentionally excluded. HTML snapshots",
        "are retained separately so parsing can be rerun without refetching.",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text("\n".join(lines), encoding="utf-8")
    os.replace(temp, path)


def _run_locked(
    *,
    parent_path: Path,
    state_path: Path,
    snapshot_root: Path,
    report_path: Path,
    seed_limit: Optional[int],
    max_items: Optional[int],
    delay_seconds: float,
    seed_only: bool,
    reparse_current: bool,
    refresh: bool,
    retry_terminal: bool,
    max_consecutive_blocked: int,
    max_consecutive_failures: int,
    auto_relogin: bool,
) -> Dict[str, Any]:
    started = time.monotonic()
    if seed_only and reparse_current:
        raise ValueError("--seed-only and --reparse-current are mutually exclusive")
    if reparse_current and refresh:
        raise ValueError("--reparse-current and --refresh are mutually exclusive")
    if not seed_only and not reparse_current and delay_seconds < 1.0:
        raise ValueError("--delay must be at least 1 second")
    if max_items is not None and max_items <= 0:
        raise ValueError("--max-items must be positive")
    if max_consecutive_blocked <= 0:
        raise ValueError("--max-consecutive-blocked must be positive")
    if max_consecutive_failures <= 0:
        raise ValueError("--max-consecutive-failures must be positive")
    parent_path = parent_path.resolve()
    state_path = state_path.resolve()
    snapshot_root = snapshot_root.resolve()
    report_path = report_path.resolve()
    if not parent_path.exists():
        raise FileNotFoundError(parent_path)
    if parent_path == state_path:
        raise ValueError("parent and state DB paths must differ")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_root.mkdir(parents=True, exist_ok=True)

    parent_sha = file_sha256(parent_path)
    parent = open_parent(parent_path)
    parent_info = validate_parent(parent)
    state = initialize_state(
        state_path=state_path,
        parent_path=parent_path,
        parent_sha256=parent_sha,
        parent_info=parent_info,
        snapshot_root=snapshot_root,
    )
    seeded = 0
    processed = 0
    run_id: Optional[int] = None
    run_mode = "seed_only" if seed_only else (
        "reparse" if reparse_current else "fetch"
    )
    try:
        seeded = seed_jobs(
            parent,
            state,
            seed_limit=seed_limit,
        )
        if not seed_only:
            run_id = int(
                state.execute(
                    """
                    INSERT INTO recrawl_runs(
                        started_at,status,max_items,delay_seconds,refresh_mode
                    ) VALUES (?,'running',?,?,?)
                    """,
                    (
                        utc_now(),
                        max_items,
                        delay_seconds,
                        int(refresh),
                    ),
                ).lastrowid
            )
            state.commit()
            if reparse_current:
                processed = reparse_current_snapshots(
                    state,
                    max_items=max_items,
                    run_id=run_id,
                )
            else:
                statuses = ["pending"]
                if retry_terminal:
                    statuses.extend(["failed", "blocked"])
                if refresh:
                    statuses.extend(["success", "not_modified"])
                placeholders = ",".join("?" for _ in statuses)
                query = """
                    SELECT *
                    FROM article_html_jobs
                    WHERE fetch_status IN (%s)
                    ORDER BY
                      CASE
                        WHEN fetch_status='pending' THEN 0
                        WHEN fetch_status IN ('failed','blocked') THEN 1
                        ELSE 2
                      END,
                      CASE
                        WHEN fetch_status='pending' THEN priority
                      END DESC,
                      CASE
                        WHEN fetch_status IN ('failed','blocked')
                          THEN updated_at
                      END,
                      CASE
                        WHEN fetch_status IN ('success','not_modified')
                          THEN fetched_at
                      END,
                      article_id
                """ % placeholders
                params: List[Any] = list(statuses)
                if max_items is not None:
                    query += " LIMIT ?"
                    params.append(max_items)
                jobs = list(state.execute(query, tuple(params)))
                session = get_authenticated_session()
                rate_limiter = RateLimiter(delay_seconds)
                consecutive_blocked = 0
                consecutive_failures = 0
                for job in jobs:
                    try:
                        fetch_status, _parse_status = fetch_job(
                            state,
                            run_id=run_id,
                            job=job,
                            session=session,
                            rate_limiter=rate_limiter,
                            snapshot_root=snapshot_root,
                            refresh=refresh,
                        )
                    except AuthenticationExpiredError:
                        if not auto_relogin:
                            raise
                        email = os.environ.get("DIVISARE_EMAIL")
                        password = os.environ.get("DIVISARE_PASSWORD")
                        if not email or not password:
                            raise RuntimeError(
                                "auto-relogin requires DIVISARE_EMAIL and "
                                "DIVISARE_PASSWORD"
                            )
                        if not do_login(email, password, verbose=True):
                            raise RuntimeError("Divisare auto-relogin failed")
                        session = get_authenticated_session()
                        fetch_status, _parse_status = fetch_job(
                            state,
                            run_id=run_id,
                            job=job,
                            session=session,
                            rate_limiter=rate_limiter,
                            snapshot_root=snapshot_root,
                            refresh=refresh,
                        )
                    processed += 1
                    state.execute(
                        "UPDATE recrawl_runs SET processed=? WHERE run_id=?",
                        (processed, run_id),
                    )
                    state.commit()
                    (
                        consecutive_blocked,
                        consecutive_failures,
                    ) = update_failure_streaks(
                        circuit_breaker_status(fetch_status, _parse_status),
                        consecutive_blocked=consecutive_blocked,
                        consecutive_failures=consecutive_failures,
                        max_consecutive_blocked=max_consecutive_blocked,
                        max_consecutive_failures=max_consecutive_failures,
                    )

        state.execute(
            """
            UPDATE recrawl_lineage
            SET crawler_version=?,parser_version=?
            WHERE lineage_id=1
            """,
            (CRAWLER_VERSION, PARSER_VERSION),
        )
        state.commit()
        metrics = collect_metrics(state, parent)
        metrics["seeded_from_parent"] = seeded
        metrics["processed_this_run"] = processed
        metrics["run_mode"] = run_mode
        validation = validate_state(state)
        if run_id is not None:
            state.execute(
                """
                UPDATE recrawl_runs
                SET completed_at=?,status='complete',processed=?,metrics_json=?
                WHERE run_id=?
                """,
                (
                    utc_now(),
                    processed,
                    json_dumps(metrics),
                    run_id,
                ),
            )
            state.commit()
        elapsed = time.monotonic() - started
        write_report(
            report_path,
            parent_path=parent_path,
            state_path=state_path,
            snapshot_root=snapshot_root,
            metrics=metrics,
            validation=validation,
            elapsed_seconds=elapsed,
            run_mode=run_mode,
        )
        return {
            "state_db": str(state_path),
            "report": str(report_path),
            "snapshot_root": str(snapshot_root),
            "elapsed_seconds": round(elapsed, 2),
            "metrics": metrics,
            "validation": validation,
        }
    except (Exception, KeyboardInterrupt) as exc:
        state.rollback()
        if run_id is not None:
            processed = int(
                state.execute(
                    "SELECT processed FROM recrawl_runs WHERE run_id=?",
                    (run_id,),
                ).fetchone()[0]
            )
            recovery_time = utc_now()
            state.execute(
                """
                UPDATE article_html_jobs
                SET fetch_status=CASE
                      WHEN current_html_sha256 IS NULL THEN 'pending'
                      ELSE 'success'
                    END,
                    parse_status=CASE
                      WHEN current_html_sha256 IS NULL THEN 'pending'
                      ELSE parse_status
                    END,
                    last_error=?,
                    updated_at=?
                WHERE fetch_status='running'
                """,
                (
                    "recovered failed run: %s" % str(exc)[:1900],
                    recovery_time,
                ),
            )
            state.execute(
                """
                UPDATE recrawl_runs
                SET completed_at=?,status='failed',processed=?,error=?
                WHERE run_id=?
                """,
                (utc_now(), processed, str(exc)[:2000], run_id),
            )
            state.commit()
        raise
    finally:
        state.close()
        parent.close()


def run(
    *,
    parent_path: Path,
    state_path: Path,
    snapshot_root: Path,
    report_path: Path,
    seed_limit: Optional[int],
    max_items: Optional[int],
    delay_seconds: float,
    seed_only: bool,
    reparse_current: bool,
    refresh: bool,
    retry_terminal: bool,
    max_consecutive_blocked: int,
    max_consecutive_failures: int,
    auto_relogin: bool,
) -> Dict[str, Any]:
    resolved_state = state_path.resolve()
    with exclusive_state_lock(resolved_state):
        return _run_locked(
            parent_path=parent_path,
            state_path=resolved_state,
            snapshot_root=snapshot_root,
            report_path=report_path,
            seed_limit=seed_limit,
            max_items=max_items,
            delay_seconds=delay_seconds,
            seed_only=seed_only,
            reparse_current=reparse_current,
            refresh=refresh,
            retry_terminal=retry_terminal,
            max_consecutive_blocked=max_consecutive_blocked,
            max_consecutive_failures=max_consecutive_failures,
            auto_relogin=auto_relogin,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-db", type=Path, required=True)
    parser.add_argument(
        "--state-db",
        type=Path,
        default=(
            ROOT
            / "data"
            / "enrichment"
            / "divisare_metadata_recrawl_v2_4.db"
        ),
    )
    parser.add_argument(
        "--snapshot-dir",
        type=Path,
        default=(
            ROOT
            / "data"
            / "enrichment"
            / "divisare_html_snapshots_v2_4"
        ),
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=(
            ROOT
            / "data"
            / "reports"
            / "divisare_metadata_recrawl_v2_4.md"
        ),
    )
    parser.add_argument("--seed-limit", type=int, default=None)
    parser.add_argument("--max-items", type=int, default=None)
    parser.add_argument("--delay", type=float, default=3.0)
    parser.add_argument("--seed-only", action="store_true")
    parser.add_argument("--reparse-current", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--retry-terminal", action="store_true")
    parser.add_argument("--max-consecutive-blocked", type=int, default=3)
    parser.add_argument("--max-consecutive-failures", type=int, default=10)
    parser.add_argument("--auto-relogin", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = run(
            parent_path=args.parent_db,
            state_path=args.state_db,
            snapshot_root=args.snapshot_dir,
            report_path=args.report,
            seed_limit=args.seed_limit,
            max_items=args.max_items,
            delay_seconds=args.delay,
            seed_only=args.seed_only,
            reparse_current=args.reparse_current,
            refresh=args.refresh,
            retry_terminal=args.retry_terminal,
            max_consecutive_blocked=args.max_consecutive_blocked,
            max_consecutive_failures=args.max_consecutive_failures,
            auto_relogin=args.auto_relogin,
        )
    except Exception as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
