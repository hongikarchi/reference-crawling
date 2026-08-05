"""Immutable SQLite builder for parsed Architizer A+Awards snapshots.

The recrawl-v2 state database and its gzip snapshots are read-only inputs.
This module verifies the selected award-census run, gzip/content hashes, and
the census discovery sets before publishing a separate no-overwrite database.
It never mutates the recrawl sidecar, the legacy Architizer database, or the
source snapshots.
"""

from __future__ import annotations

import ast
import contextlib
import gzip
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import urllib.parse
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional

from bs4 import BeautifulSoup

from crawl.architizer.awards_v2 import (
    PARSER_VERSION,
    _entity_from_url,
    parse_awards_track_snapshot,
)
from crawl.architizer.recrawl_v2 import (
    STATE_SCHEMA_VERSION,
    LockHeldError,
    SidecarLock,
)


BUILDER_VERSION = "architizer-awards-store-v2.3.0"
SCHEMA_VERSION = "architizer-awards-source-v2.3.0"
POLICY_VERSION = "architizer-awards-projection-policy-v1"
READY_VERSION = "architizer-awards-ready-v3"
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PRODUCTION_OUTPUT = (
    REPO_ROOT / "data" / "enrichment" / "architizer_awards_v2.db"
).resolve()


class AwardsBuildError(RuntimeError):
    """Raised when immutable input or output invariants are not satisfied."""


OUTPUT_SCHEMA = """
PRAGMA foreign_keys=ON;

CREATE TABLE schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE input_lineage (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    input_kind TEXT NOT NULL,
    sidecar_path TEXT NOT NULL,
    sidecar_size_bytes INTEGER NOT NULL,
    sidecar_sha256_before TEXT NOT NULL,
    sidecar_sha256_after TEXT NOT NULL,
    sqlite_open_mode TEXT NOT NULL,
    recrawl_run_id INTEGER NOT NULL,
    recrawl_run_kind TEXT NOT NULL,
    recrawl_run_status TEXT NOT NULL,
    recrawl_parser_version TEXT NOT NULL,
    recrawl_started_at TEXT NOT NULL,
    recrawl_finished_at TEXT NOT NULL,
    legacy_source_db_path TEXT NOT NULL,
    legacy_source_db_sha256 TEXT NOT NULL,
    snapshot_root TEXT NOT NULL,
    selected_snapshot_count INTEGER NOT NULL,
    snapshot_manifest_sha256 TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);

CREATE TABLE award_page_versions (
    id INTEGER PRIMARY KEY,
    input_lineage_id INTEGER NOT NULL REFERENCES input_lineage(id),
    recrawl_run_id INTEGER NOT NULL,
    http_attempt_id INTEGER NOT NULL UNIQUE,
    page_kind TEXT NOT NULL,
    award_year INTEGER NOT NULL,
    award_track TEXT,
    requested_url TEXT NOT NULL,
    final_url TEXT NOT NULL,
    final_url_policy TEXT NOT NULL,
    http_status INTEGER NOT NULL,
    content_type TEXT NOT NULL,
    response_bytes INTEGER NOT NULL,
    snapshot_content_sha256 TEXT NOT NULL,
    snapshot_gzip_sha256 TEXT NOT NULL,
    snapshot_gzip_path TEXT NOT NULL,
    snapshot_gzip_bytes INTEGER NOT NULL,
    parser_version TEXT NOT NULL,
    parsed_at TEXT NOT NULL,
    parse_status TEXT NOT NULL,
    source_record_count INTEGER NOT NULL,
    selected_record_count INTEGER NOT NULL DEFAULT 0,
    status_counts_json TEXT NOT NULL,
    duplicate_attribution_ids_json TEXT NOT NULL,
    UNIQUE(recrawl_run_id, award_track, snapshot_content_sha256, parser_version)
);

CREATE TABLE award_attributions (
    id INTEGER PRIMARY KEY,
    page_version_id INTEGER NOT NULL REFERENCES award_page_versions(id),
    selection_order INTEGER NOT NULL UNIQUE,
    source_group_ordinal INTEGER NOT NULL,
    source_card_ordinal INTEGER NOT NULL,
    award_year INTEGER NOT NULL,
    award_track TEXT NOT NULL,
    attribution_pk INTEGER,
    attribution_global_id TEXT,
    category_raw TEXT,
    category_path_json TEXT NOT NULL,
    subject_kind TEXT,
    subject_slug TEXT,
    subject_name TEXT,
    subject_url TEXT,
    description_raw TEXT,
    image_url_resolved TEXT,
    parse_status TEXT NOT NULL,
    missing_json TEXT NOT NULL,
    conflicts_json TEXT NOT NULL,
    warnings_json TEXT NOT NULL,
    raw_attributes_json TEXT NOT NULL,
    dom_values_json TEXT NOT NULL,
    source_url TEXT NOT NULL,
    UNIQUE(page_version_id, source_group_ordinal, source_card_ordinal)
);

CREATE TRIGGER award_attributions_parent_parity_insert
BEFORE INSERT ON award_attributions
BEGIN
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM award_page_versions AS p
        WHERE p.id=NEW.page_version_id
          AND p.page_kind='track'
          AND p.award_year=NEW.award_year
          AND p.award_track=NEW.award_track
          AND p.requested_url=NEW.source_url
    ) THEN 1 ELSE RAISE(ABORT,'award attribution parent parity mismatch') END;
END;

CREATE TRIGGER award_attributions_parent_parity_update
BEFORE UPDATE OF page_version_id,award_year,award_track,source_url
ON award_attributions
BEGIN
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM award_page_versions AS p
        WHERE p.id=NEW.page_version_id
          AND p.page_kind='track'
          AND p.award_year=NEW.award_year
          AND p.award_track=NEW.award_track
          AND p.requested_url=NEW.source_url
    ) THEN 1 ELSE RAISE(ABORT,'award attribution parent parity mismatch') END;
END;

CREATE TRIGGER award_page_versions_child_parity_update
BEFORE UPDATE OF page_kind,award_year,award_track,requested_url
ON award_page_versions
WHEN EXISTS (
    SELECT 1 FROM award_attributions AS a
    WHERE a.page_version_id=OLD.id
      AND (
          NEW.page_kind!='track'
          OR a.award_year IS NOT NEW.award_year
          OR a.award_track IS NOT NEW.award_track
          OR a.source_url IS NOT NEW.requested_url
      )
)
BEGIN
    SELECT RAISE(ABORT,'award page child parity mismatch');
END;

CREATE TABLE award_attribution_tiers (
    attribution_id INTEGER NOT NULL REFERENCES award_attributions(id),
    position INTEGER NOT NULL,
    normalized_tier TEXT,
    raw_attribute_label TEXT,
    raw_dom_label TEXT,
    parse_status TEXT NOT NULL,
    PRIMARY KEY(attribution_id, position)
);

CREATE TABLE award_attribution_companies (
    attribution_id INTEGER NOT NULL REFERENCES award_attributions(id),
    position INTEGER NOT NULL,
    entity_kind TEXT,
    slug TEXT,
    name TEXT,
    url TEXT,
    attribute_observation_json TEXT NOT NULL,
    dom_observation_json TEXT NOT NULL,
    parse_status TEXT NOT NULL,
    PRIMARY KEY(attribution_id, position)
);

CREATE TABLE corpus_projection_policy (
    entity_kind TEXT PRIMARY KEY,
    preserve_in_source_corpus INTEGER NOT NULL CHECK(preserve_in_source_corpus = 1),
    corpus_role TEXT NOT NULL,
    project_firm_curated_projection TEXT NOT NULL,
    policy_version TEXT NOT NULL
);

CREATE TABLE build_manifest (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    built_at TEXT NOT NULL,
    builder_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    build_limit INTEGER,
    is_full_snapshot_projection INTEGER NOT NULL,
    page_count INTEGER NOT NULL,
    source_record_count INTEGER NOT NULL,
    selected_record_count INTEGER NOT NULL,
    status_counts_json TEXT NOT NULL,
    subject_counts_json TEXT NOT NULL,
    company_counts_json TEXT NOT NULL,
    summary_json TEXT NOT NULL
);

CREATE INDEX idx_award_attributions_year_track
ON award_attributions(award_year, award_track, attribution_pk);
CREATE INDEX idx_award_attributions_subject
ON award_attributions(subject_kind, subject_slug);
CREATE INDEX idx_award_attributions_global_id
ON award_attributions(attribution_global_id);
CREATE UNIQUE INDEX idx_award_attributions_global_id_unique
ON award_attributions(attribution_global_id)
WHERE attribution_global_id IS NOT NULL;
CREATE INDEX idx_award_companies_entity
ON award_attribution_companies(entity_kind, slug);
"""


_RUN_KIND_RE = re.compile(r"^award_seed_census_(\d{4})$")
_ATTRIBUTION_ID_RE = re.compile(r"^projects\.awardattribution\.(\d+)$")
_REQUIRED_SIDECAR_COLUMNS = {
    "state_meta": {"key", "value"},
    "runs": {
        "id",
        "run_kind",
        "started_at",
        "finished_at",
        "status",
        "parser_version",
        "arguments_json",
        "source_db_path",
        "source_db_sha256_before",
        "source_db_sha256_after",
        "source_db_size",
        "selected_count",
        "summary_json",
        "error",
    },
    "http_attempts": {
        "id",
        "run_id",
        "target_url",
        "request_kind",
        "requested_url",
        "attempt_number",
        "started_at",
        "finished_at",
        "duration_ms",
        "outcome",
        "http_status",
        "final_url",
        "content_type",
        "response_bytes",
        "sha256",
        "gzip_path",
        "retryable",
        "block_signals_json",
        "error",
    },
    "award_discoveries": {
        "run_id",
        "award_year",
        "award_track",
        "entity_type",
        "slug",
        "source_url",
        "discovered_url",
        "discovered_at",
    },
}


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
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _ready_path(output_path: Path) -> Path:
    return Path(str(output_path) + ".READY.json")


def _stable_path_label(path: Path | str) -> str:
    candidate = Path(path)
    resolved = candidate.resolve()
    try:
        return resolved.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return resolved.name


def _sqlite_sidecars(path: Path) -> dict[str, Path]:
    return {
        "wal": Path(str(path) + "-wal"),
        "shm": Path(str(path) + "-shm"),
        "journal": Path(str(path) + "-journal"),
    }


def _validate_paths(sidecar: Path, snapshot_root: Path, output: Path) -> None:
    sidecar = sidecar.resolve()
    snapshot_root = snapshot_root.resolve()
    output = output.resolve()
    if not sidecar.is_file():
        raise AwardsBuildError(f"recrawl sidecar does not exist: {sidecar}")
    if not snapshot_root.is_dir():
        raise AwardsBuildError(f"snapshot root does not exist: {snapshot_root}")
    if sidecar == output:
        raise AwardsBuildError("output must not alias the recrawl sidecar")
    if output == snapshot_root or output.is_relative_to(snapshot_root):
        raise AwardsBuildError("output must not be inside the snapshot root")
    if output.exists():
        raise AwardsBuildError(f"immutable output already exists: {output}")
    ready = _ready_path(output)
    if ready.exists():
        raise AwardsBuildError(f"immutable READY receipt already exists: {ready}")
    for label, path in _sqlite_sidecars(output).items():
        if path.exists():
            raise AwardsBuildError(
                f"stale output SQLite {label} sidecar already exists: {path}"
            )


def _validate_sidecar_storage(sidecar: Path) -> None:
    active = {
        label: path.stat().st_size
        for label, path in _sqlite_sidecars(sidecar).items()
        if path.exists()
    }
    if active:
        raise AwardsBuildError(
            f"recrawl sidecar has SQLite sidecars; immutable read refused: {active}"
        )


def _validate_build_request(
    *,
    output_path: Path,
    run_id: Optional[int],
    award_year: Optional[int],
    limit: Optional[int],
) -> None:
    if limit is None and run_id is None and award_year is None:
        raise AwardsBuildError(
            "production build requires an explicit --run-id or --award-year"
        )
    if limit is not None and output_path.resolve() == DEFAULT_PRODUCTION_OUTPUT:
        raise AwardsBuildError(
            "limited smoke build requires an explicit non-production output path"
        )


def _open_sidecar_readonly(path: Path) -> sqlite3.Connection:
    connection: Optional[sqlite3.Connection] = None
    try:
        connection = sqlite3.connect(
            path.resolve().as_uri() + "?mode=ro&immutable=1",
            uri=True,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
            raise AwardsBuildError("recrawl sidecar is not query_only")
        quick_rows = [
            str(row[0]) for row in connection.execute("PRAGMA quick_check")
        ]
        if quick_rows != ["ok"]:
            raise AwardsBuildError(
                f"recrawl sidecar quick_check failed: {quick_rows[:10]}"
            )
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise AwardsBuildError(
                "recrawl sidecar foreign_key_check failed: "
                f"{len(foreign_keys)} violation(s)"
            )
        actual_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        missing_tables = sorted(set(_REQUIRED_SIDECAR_COLUMNS) - actual_tables)
        if missing_tables:
            raise AwardsBuildError(
                "not a compatible Architizer recrawl sidecar; missing tables: "
                f"{missing_tables}"
            )
        for table, required_columns in _REQUIRED_SIDECAR_COLUMNS.items():
            actual_columns = {
                str(row[1])
                for row in connection.execute(f'PRAGMA table_info("{table}")')
            }
            missing_columns = sorted(required_columns - actual_columns)
            if missing_columns:
                raise AwardsBuildError(
                    f"recrawl sidecar table {table} is missing columns: "
                    f"{missing_columns}"
                )
        schema_row = connection.execute(
            "SELECT value FROM state_meta WHERE key='schema_version'"
        ).fetchone()
        if schema_row is None or str(schema_row[0]) != STATE_SCHEMA_VERSION:
            actual_version = None if schema_row is None else str(schema_row[0])
            raise AwardsBuildError(
                "unsupported recrawl sidecar schema version: "
                f"{actual_version!r} != {STATE_SCHEMA_VERSION!r}"
            )
        return connection
    except sqlite3.DatabaseError as exc:
        if connection is not None:
            connection.close()
        raise AwardsBuildError(f"recrawl sidecar SQLite validation failed: {exc}") from exc
    except Exception:
        if connection is not None:
            connection.close()
        raise


def _select_run(
    connection: sqlite3.Connection,
    *,
    run_id: Optional[int],
    award_year: Optional[int],
) -> dict[str, Any]:
    where = ["status='completed'", "run_kind LIKE 'award_seed_census_%'"]
    params: list[Any] = []
    if run_id is not None:
        where.append("id=?")
        params.append(run_id)
    if award_year is not None:
        where.append("run_kind=?")
        params.append(f"award_seed_census_{award_year}")
    rows = connection.execute(
        f"SELECT * FROM runs WHERE {' AND '.join(where)} ORDER BY id DESC",
        params,
    ).fetchall()
    if not rows:
        raise AwardsBuildError("no matching completed award seed census run")
    if run_id is not None and len(rows) != 1:
        raise AwardsBuildError(f"award census run id is not unique: {run_id}")
    run = dict(rows[0])
    match = _RUN_KIND_RE.fullmatch(run["run_kind"])
    if not match:
        raise AwardsBuildError(f"invalid award census run kind: {run['run_kind']}")
    run["award_year"] = int(match.group(1))
    if not run.get("finished_at"):
        raise AwardsBuildError("selected award census run has no finish time")
    if (
        not run.get("source_db_sha256_after")
        or run["source_db_sha256_before"] != run["source_db_sha256_after"]
    ):
        raise AwardsBuildError("legacy source DB was not immutable during census")
    try:
        summary = json.loads(run.get("summary_json") or "{}")
    except json.JSONDecodeError as exc:
        raise AwardsBuildError("award census summary_json is invalid") from exc
    tracks = summary.get("tracks")
    if not isinstance(tracks, list) or not tracks or not all(
        isinstance(value, str) and value for value in tracks
    ):
        raise AwardsBuildError("award census summary has no valid official tracks")
    if len(tracks) != len(set(tracks)):
        raise AwardsBuildError("award census summary contains duplicate tracks")
    run["summary"] = summary
    run["tracks"] = sorted(set(tracks), key=str.casefold)
    return run


def _canonical_winners_url(
    url: str, award_year: int, track: Optional[str]
) -> Optional[str]:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.netloc != "winners.architizer.com"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
    ):
        return None
    parts = [urllib.parse.unquote(value) for value in parsed.path.split("/") if value]
    expected = [str(award_year)] if track is None else [str(award_year), track]
    if parts != expected:
        return None
    suffix = "" if track is None else f"{urllib.parse.quote(track, safe='-._~')}/"
    canonical = f"https://winners.architizer.com/{award_year}/{suffix}"
    return canonical if url == canonical else None


def _track_from_url(url: str, award_year: int) -> Optional[str]:
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    parts = [urllib.parse.unquote(value) for value in parsed.path.split("/") if value]
    if len(parts) != 2:
        return None
    track = parts[1]
    return track if _canonical_winners_url(url, award_year, track) else None


def _validate_http_attempt(
    attempt: dict[str, Any],
    *,
    expected_url: str,
    label: str,
) -> str:
    if attempt.get("outcome") != "success" or attempt.get("http_status") != 200:
        raise AwardsBuildError(f"{label} HTTP attempt is not a successful 200")
    content_type = str(attempt.get("content_type") or "")
    mime = content_type.split(";", 1)[0].strip().lower()
    if mime != "text/html":
        raise AwardsBuildError(
            f"{label} snapshot is non-HTML: {content_type or '<missing>'}"
        )
    try:
        block_signals = json.loads(attempt.get("block_signals_json") or "[]")
    except json.JSONDecodeError as exc:
        raise AwardsBuildError(f"{label} block_signals_json is invalid") from exc
    if not isinstance(block_signals, list):
        raise AwardsBuildError(f"{label} block_signals_json is not a list")
    if block_signals:
        raise AwardsBuildError(
            f"{label} has block/login signals: {block_signals}"
        )
    final_url = attempt.get("final_url")
    if final_url == expected_url:
        return "exact"
    raise AwardsBuildError(
        f"{label} final URL mismatch: {final_url!r} != {expected_url!r}"
    )


def _official_track_urls_from_root(
    html: str, *, root_url: str, award_year: int
) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    tracks: dict[str, str] = {}
    for anchor in soup.find_all("a", href=True):
        try:
            absolute = urllib.parse.urljoin(root_url, anchor.get("href"))
        except ValueError:
            continue
        track = _track_from_url(absolute, award_year)
        if not track or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{1,50}", track):
            continue
        tracks.setdefault(
            track,
            f"https://winners.architizer.com/{award_year}/{track}/",
        )
    if not tracks:
        raise AwardsBuildError("award year root exposes no official track URLs")
    return dict(sorted(tracks.items(), key=lambda item: item[0].casefold()))


def _load_snapshot(
    snapshot_root: Path,
    attempt: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    raw_relative = attempt.get("gzip_path")
    if not raw_relative:
        raise AwardsBuildError(
            f"HTTP attempt {attempt['id']} has no gzip snapshot path"
        )
    snapshot_path = (snapshot_root / raw_relative).resolve()
    if not snapshot_path.is_relative_to(snapshot_root.resolve()):
        raise AwardsBuildError(
            f"snapshot path escapes snapshot root: {raw_relative}"
        )
    if not snapshot_path.is_file():
        raise AwardsBuildError(f"snapshot is missing: {snapshot_path}")
    gzip_sha = sha256_file(snapshot_path)
    gzip_bytes = snapshot_path.stat().st_size
    try:
        with gzip.open(snapshot_path, "rb") as handle:
            body = handle.read()
    except (OSError, EOFError) as exc:
        raise AwardsBuildError(f"snapshot gzip is unreadable: {snapshot_path}") from exc
    content_sha = sha256_bytes(body)
    if content_sha != str(attempt.get("sha256") or "").upper():
        raise AwardsBuildError(
            f"snapshot content SHA mismatch for HTTP attempt {attempt['id']}"
        )
    if len(body) != int(attempt.get("response_bytes") or -1):
        raise AwardsBuildError(
            f"snapshot byte count mismatch for HTTP attempt {attempt['id']}"
        )
    try:
        html = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AwardsBuildError(
            f"award snapshot is not valid UTF-8: {snapshot_path}"
        ) from exc
    return html, {
        "relative_path": str(raw_relative).replace("\\", "/"),
        "content_sha256": content_sha,
        "gzip_sha256": gzip_sha,
        "content_bytes": len(body),
        "gzip_bytes": gzip_bytes,
    }


def _load_pages(
    connection: sqlite3.Connection,
    *,
    run: dict[str, Any],
    snapshot_root: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    expected_root_url = f"https://winners.architizer.com/{run['award_year']}/"
    if run["summary"].get("award_year") != run["award_year"]:
        raise AwardsBuildError("run summary award_year differs from the run kind")
    if run["summary"].get("official_root") != expected_root_url:
        raise AwardsBuildError("run summary official_root differs from the award year")
    root_attempts = [
        dict(row)
        for row in connection.execute(
            """
            SELECT * FROM http_attempts
            WHERE run_id=? AND request_kind='award_year_root'
              AND outcome='success' AND http_status=200
            ORDER BY id
            """,
            (run["id"],),
        )
    ]
    if len(root_attempts) != 1:
        raise AwardsBuildError(
            "award census run must have exactly one successful year-root snapshot"
        )
    root_attempt = root_attempts[0]
    if root_attempt.get("requested_url") != expected_root_url:
        raise AwardsBuildError(
            f"award year-root requested URL mismatch: {root_attempt.get('requested_url')}"
        )
    root_final_url_policy = _validate_http_attempt(
        root_attempt,
        expected_url=expected_root_url,
        label="award year root",
    )
    root_html, root_snapshot = _load_snapshot(snapshot_root, root_attempt)
    official_track_urls = _official_track_urls_from_root(
        root_html,
        root_url=expected_root_url,
        award_year=run["award_year"],
    )
    if set(official_track_urls) != set(run["tracks"]):
        raise AwardsBuildError(
            "official year-root tracks differ from the run summary: "
            f"root={sorted(official_track_urls)}, summary={run['tracks']}"
        )
    summary_track_urls = run["summary"].get("track_urls")
    if not isinstance(summary_track_urls, dict) or set(summary_track_urls) != set(
        official_track_urls
    ):
        raise AwardsBuildError("run summary track_urls differ from the year root")
    for track, official_url in official_track_urls.items():
        if summary_track_urls.get(track) != official_url:
            raise AwardsBuildError(
                f"run summary URL differs from year root for track {track}"
            )
    root_page = {
        "attempt": root_attempt,
        "page_kind": "year_root",
        "track": None,
        "final_url_policy": root_final_url_policy,
        "snapshot": root_snapshot,
        "parsed": {
            "parse_status": "complete",
            "record_count": 0,
            "status_counts": {},
            "duplicate_award_attribution_ids": [],
        },
        "official_track_urls": official_track_urls,
    }

    attempts = [
        dict(row)
        for row in connection.execute(
            """
            SELECT * FROM http_attempts
            WHERE run_id=? AND request_kind='award_track_root'
              AND outcome='success' AND http_status=200
            ORDER BY requested_url,id
            """,
            (run["id"],),
        )
    ]
    if not attempts:
        raise AwardsBuildError("award census run has no successful track snapshots")
    pages: list[dict[str, Any]] = []
    seen_tracks: set[str] = set()
    for attempt in attempts:
        track = _track_from_url(attempt["requested_url"], run["award_year"])
        if not track:
            raise AwardsBuildError(
                f"invalid award track URL: {attempt['requested_url']}"
            )
        if track in seen_tracks:
            raise AwardsBuildError(f"duplicate successful track snapshot: {track}")
        seen_tracks.add(track)
        expected_url = official_track_urls.get(track)
        if not expected_url or attempt.get("requested_url") != expected_url:
            raise AwardsBuildError(
                f"track attempt is not registered by the year root: {attempt.get('requested_url')}"
            )
        final_url_policy = _validate_http_attempt(
            attempt,
            expected_url=expected_url,
            label=f"award track {track}",
        )
        html, snapshot = _load_snapshot(snapshot_root, attempt)
        if track == "Typology" and (
            snapshot["content_sha256"] != root_snapshot["content_sha256"]
            or snapshot["gzip_sha256"] != root_snapshot["gzip_sha256"]
            or snapshot["relative_path"] != root_snapshot["relative_path"]
        ):
            raise AwardsBuildError(
                "award track Typology deduplicated year-root snapshot differs "
                "from the verified year-root snapshot"
            )
        parsed = parse_awards_track_snapshot(
            html,
            source_url=attempt["requested_url"],
            award_year=run["award_year"],
            award_track=track,
        )
        if parsed["record_count"] == 0 or parsed["parse_status"] == "no_content":
            raise AwardsBuildError(
                f"official award track {track} parsed zero attribution records"
            )
        pages.append(
            {
                "attempt": attempt,
                "page_kind": "track",
                "track": track,
                "final_url_policy": final_url_policy,
                "snapshot": snapshot,
                "parsed": parsed,
            }
        )
    if seen_tracks != set(official_track_urls):
        raise AwardsBuildError(
            "successful track snapshots do not match the year root: "
            f"snapshots={sorted(seen_tracks)}, root={sorted(official_track_urls)}"
        )
    pages = sorted(pages, key=lambda value: value["track"].casefold())
    id_pages: dict[str, set[int]] = defaultdict(set)
    for page in pages:
        for record in page["parsed"]["records"]:
            global_id = record.get("award_attribution_global_id")
            if global_id:
                id_pages[global_id].add(page["attempt"]["id"])
    cross_page_duplicates = sorted(
        global_id for global_id, page_ids in id_pages.items() if len(page_ids) > 1
    )
    if cross_page_duplicates:
        raise AwardsBuildError(
            "award attribution IDs repeat across track pages: "
            f"{cross_page_duplicates[:10]}"
        )
    return root_page, pages


def _parsed_discovery_tuples(
    pages: list[dict[str, Any]],
    *,
    award_year: int,
) -> set[tuple[int, str, str, str, str, str]]:
    result: set[tuple[int, str, str, str, str, str]] = set()
    for page in pages:
        track = page["track"]
        source_url = page["attempt"]["requested_url"]
        for record in page["parsed"]["records"]:
            # Only identities resolved from agreeing attribute and DOM evidence
            # are eligible.  Conflict/no-content observations remain in the
            # structured corpus, but never become crawler discovery seeds.
            subject = record.get("subject")
            if subject and subject["kind"] in {"project", "firm"}:
                result.add(
                    (
                        award_year,
                        track,
                        subject["kind"],
                        subject["slug"],
                        source_url,
                        subject["url"],
                    )
                )
            for company in record.get("companies") or []:
                if company["kind"] in {"project", "firm"}:
                    result.add(
                        (
                            award_year,
                            track,
                            company["kind"],
                            company["slug"],
                            source_url,
                            company["url"],
                        )
                    )
    return result


def _validate_discoveries(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    pages: list[dict[str, Any]],
) -> dict[str, dict[str, int]]:
    page_urls = {
        page["track"]: page["attempt"]["requested_url"] for page in pages
    }
    run_years = {page["parsed"]["award_year"] for page in pages}
    if len(run_years) != 1:
        raise AwardsBuildError(f"parsed award pages disagree on year: {sorted(run_years)}")
    award_year = next(iter(run_years))
    rows = list(
        connection.execute(
        """
        SELECT award_year,award_track,entity_type,slug,source_url,discovered_url
        FROM award_discoveries
        WHERE run_id=?
        ORDER BY award_year,award_track,entity_type,slug,source_url,discovered_url
        """,
        (run_id,),
        )
    )
    discovered_url_counts = Counter(
        (
            str(row["award_track"]),
            str(row["entity_type"]),
            str(row["discovered_url"]),
        )
        for row in rows
    )
    duplicate_urls = sorted(
        key for key, count in discovered_url_counts.items() if count > 1
    )
    if duplicate_urls:
        raise AwardsBuildError(
            "award discoveries contain a duplicate discovered URL: "
            f"{duplicate_urls[:10]}"
        )
    stored: set[tuple[int, str, str, str, str, str]] = set()
    for row in rows:
        track = str(row["award_track"])
        entity_type = str(row["entity_type"])
        discovered_url = str(row["discovered_url"])
        if int(row["award_year"]) != award_year:
            raise AwardsBuildError(
                "award discovery year differs from selected run: "
                f"{row['award_year']} != {award_year}"
            )
        if row["entity_type"] not in {"project", "firm"}:
            raise AwardsBuildError(
                f"unsupported award discovery entity type: {row['entity_type']}"
            )
        expected_source_url = page_urls.get(track)
        if expected_source_url is None:
            raise AwardsBuildError(
                "award discoveries contain a non-official track: "
                f"{track}"
            )
        if row["source_url"] != expected_source_url:
            raise AwardsBuildError(
                f"award discovery source URL differs for {track}: "
                f"{row['source_url']!r} != {expected_source_url!r}"
            )
        entity = _entity_from_url(discovered_url, "https://architizer.com/")
        if (
            entity is None
            or entity["kind"] != entity_type
            or entity["url"] != discovered_url
        ):
            raise AwardsBuildError(
                f"award discovery URL is not canonical {entity_type}: "
                f"{discovered_url!r}"
            )
        if row["slug"] != entity["slug"]:
            raise AwardsBuildError(
                f"award discovery slug differs from its URL: "
                f"{row['slug']!r} != {entity['slug']!r}"
            )
        stored.add(
            (
                award_year,
                track,
                entity_type,
                str(row["slug"]),
                str(row["source_url"]),
                discovered_url,
            )
        )
    if len(stored) != len(rows):
        raise AwardsBuildError(
            "award discovery tuple cardinality differs from stored row count"
        )
    parsed = _parsed_discovery_tuples(pages, award_year=award_year)
    if stored != parsed:
        stored_only = sorted(stored - parsed)[:10]
        parsed_only = sorted(parsed - stored)[:10]
        raise AwardsBuildError(
            "award discovery tuples differ from resolved parsed identities; "
            f"stored_only={stored_only}, parsed_only={parsed_only}"
        )
    return {
        track: {
            entity_type: sum(
                1
                for value in parsed
                if value[1] == track and value[2] == entity_type
            )
            for entity_type in ("project", "firm")
        }
        for track in sorted({page["track"] for page in pages})
    }


def _round_robin_records(
    pages: list[dict[str, Any]], limit: Optional[int]
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    if limit is not None and limit <= 0:
        raise ValueError("limit must be positive")
    if limit is None:
        return [
            (page, record)
            for page in pages
            for record in page["parsed"]["records"]
        ]
    selected: list[tuple[dict[str, Any], dict[str, Any]]] = []
    positions = defaultdict(int)
    while len(selected) < limit:
        advanced = False
        for page_index, page in enumerate(pages):
            records = page["parsed"]["records"]
            position = positions[page_index]
            if position >= len(records):
                continue
            selected.append((page, records[position]))
            positions[page_index] += 1
            advanced = True
            if len(selected) == limit:
                break
        if not advanced:
            break
    return selected


def _attribute_company_observations(record: dict[str, Any]) -> list[dict[str, Any]]:
    raw = record["attribute_values"]
    try:
        names = ast.literal_eval(raw.get("data-company-names") or "[]")
        urls = ast.literal_eval(raw.get("data-company-urls") or "[]")
    except (SyntaxError, ValueError):
        return []
    if not isinstance(names, (list, tuple)) or not isinstance(urls, (list, tuple)):
        return []
    return [
        {"name": name, "url": url}
        for name, url in zip(names, urls)
        if isinstance(name, str) and isinstance(url, str)
    ]


def _insert_output(
    connection: sqlite3.Connection,
    *,
    built_at: str,
    sidecar_path: Path,
    sidecar_size: int,
    sidecar_sha_before: str,
    sidecar_sha_after: str,
    snapshot_root: Path,
    snapshot_manifest_sha: str,
    snapshot_manifest_size: int,
    run: dict[str, Any],
    root_page: dict[str, Any],
    pages: list[dict[str, Any]],
    selected: list[tuple[dict[str, Any], dict[str, Any]]],
    limit: Optional[int],
    discovery_counts: dict[str, dict[str, int]],
) -> dict[str, Any]:
    connection.executescript(OUTPUT_SCHEMA)
    all_pages = [root_page, *pages]
    distinct_physical_snapshots = {
        (
            page["snapshot"]["relative_path"],
            page["snapshot"]["gzip_sha256"],
        )
        for page in all_pages
    }
    meta = _expected_schema_meta()
    connection.executemany(
        "INSERT INTO schema_meta(key,value) VALUES (?,?)",
        sorted(meta.items()),
    )
    connection.execute(
        """
        INSERT INTO input_lineage VALUES (
            1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
        )
        """,
        (
            "architizer_recrawl_v2_award_census",
            _stable_path_label(sidecar_path),
            sidecar_size,
            sidecar_sha_before,
            sidecar_sha_after,
            "mode=ro&immutable=1;query_only=ON",
            run["id"],
            run["run_kind"],
            run["status"],
            run["parser_version"],
            run["started_at"],
            run["finished_at"],
            _stable_path_label(run["source_db_path"]),
            run["source_db_sha256_before"],
            _stable_path_label(snapshot_root),
            len(all_pages),
            snapshot_manifest_sha,
            canonical_json(
                {
                    "run_summary": run["summary"],
                    "discovery_counts": discovery_counts,
                    "selected_snapshot_count_semantics": "page_versions",
                    "selected_page_version_count": len(all_pages),
                    "distinct_physical_snapshot_count": len(
                        distinct_physical_snapshots
                    ),
                    "snapshot_manifest_size_bytes": snapshot_manifest_size,
                }
            ),
        ),
    )

    page_ids: dict[int, int] = {}
    selected_per_page = Counter(page["attempt"]["id"] for page, _ in selected)
    for page_id, page in enumerate(all_pages, 1):
        attempt = page["attempt"]
        parsed = page["parsed"]
        snapshot = page["snapshot"]
        page_ids[attempt["id"]] = page_id
        connection.execute(
            """
            INSERT INTO award_page_versions VALUES (
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
            )
            """,
            (
                page_id,
                1,
                run["id"],
                attempt["id"],
                page["page_kind"],
                run["award_year"],
                page["track"],
                attempt["requested_url"],
                attempt.get("final_url") or attempt["requested_url"],
                page["final_url_policy"],
                attempt["http_status"],
                attempt.get("content_type") or "",
                attempt["response_bytes"],
                snapshot["content_sha256"],
                snapshot["gzip_sha256"],
                snapshot["relative_path"],
                snapshot["gzip_bytes"],
                PARSER_VERSION,
                built_at,
                parsed["parse_status"],
                parsed["record_count"],
                selected_per_page[attempt["id"]],
                canonical_json(parsed["status_counts"]),
                canonical_json(parsed["duplicate_award_attribution_ids"]),
            ),
        )

    status_counts: Counter[str] = Counter()
    subject_counts: Counter[str] = Counter()
    company_counts: Counter[str] = Counter()
    for attribution_id, (page, record) in enumerate(selected, 1):
        global_id = record.get("award_attribution_global_id")
        match = _ATTRIBUTION_ID_RE.fullmatch(global_id or "")
        subject = record.get("subject")
        tiers = record.get("award_tiers")
        companies = record.get("companies")
        category = record.get("award_category")
        subject_values = subject or {}
        connection.execute(
            """
            INSERT INTO award_attributions VALUES (
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
            )
            """,
            (
                attribution_id,
                page_ids[page["attempt"]["id"]],
                attribution_id,
                record["source_group_ordinal"],
                record["source_card_ordinal"],
                record["award_year"],
                record["award_track"],
                int(match.group(1)) if match else None,
                global_id,
                category,
                canonical_json(record.get("award_category_path") or []),
                subject_values.get("kind"),
                subject_values.get("slug"),
                subject_values.get("name"),
                subject_values.get("url"),
                record.get("description"),
                record.get("image_url"),
                record["parse_status"],
                canonical_json(record["missing"]),
                canonical_json(record["conflicts"]),
                canonical_json(record["warnings"]),
                canonical_json(
                    {
                        "card": record["card_attribute_values"],
                        "winner": record["attribute_values"],
                    }
                ),
                canonical_json(record["dom_values"]),
                record["source_url"],
            ),
        )
        raw_attribute_tiers = list(record.get("raw_tier_attribute_labels") or [])
        raw_dom_tiers = list(record.get("raw_tier_dom_labels") or [])
        tier_count = max(
            len(raw_attribute_tiers), len(raw_dom_tiers), len(tiers or [])
        )
        for position in range(tier_count):
            tier = tiers[position] if tiers and position < len(tiers) else None
            connection.execute(
                "INSERT INTO award_attribution_tiers VALUES (?,?,?,?,?,?)",
                (
                    attribution_id,
                    position,
                    tier,
                    raw_attribute_tiers[position]
                    if position < len(raw_attribute_tiers)
                    else None,
                    raw_dom_tiers[position]
                    if position < len(raw_dom_tiers)
                    else None,
                    "agreed" if tiers is not None else record["parse_status"],
                ),
            )
        attribute_companies = _attribute_company_observations(record)
        dom_companies = record["dom_values"].get("companies") or []
        company_count = max(
            len(attribute_companies), len(dom_companies), len(companies or [])
        )
        for position in range(company_count):
            company = (
                companies[position]
                if companies is not None and position < len(companies)
                else {}
            )
            connection.execute(
                "INSERT INTO award_attribution_companies VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    attribution_id,
                    position,
                    company.get("kind"),
                    company.get("slug"),
                    company.get("name"),
                    company.get("url"),
                    canonical_json(
                        attribute_companies[position]
                        if position < len(attribute_companies)
                        else None
                    ),
                    canonical_json(
                        dom_companies[position]
                        if position < len(dom_companies)
                        else None
                    ),
                    "agreed" if companies is not None else record["parse_status"],
                ),
            )
            company_counts[company.get("kind") or "unresolved_observation"] += 1
        status_counts[record["parse_status"]] += 1
        subject_counts[subject_values.get("kind") or "unresolved"] += 1

    policies = [
        (
            "project",
            1,
            "award subject",
            "eligible for project curated reconciliation after identity validation",
            POLICY_VERSION,
        ),
        (
            "firm",
            1,
            "award subject or company relation",
            "eligible for firm curated reconciliation after identity validation",
            POLICY_VERSION,
        ),
        (
            "product",
            1,
            "award subject",
            "source-corpus only; excluded from project/firm curated projection",
            POLICY_VERSION,
        ),
        (
            "brand",
            1,
            "award company relation",
            "source-corpus only; excluded from project/firm curated projection",
            POLICY_VERSION,
        ),
    ]
    connection.executemany(
        "INSERT INTO corpus_projection_policy VALUES (?,?,?,?,?)",
        policies,
    )
    source_record_count = sum(page["parsed"]["record_count"] for page in pages)
    summary = {
        "award_year": run["award_year"],
        "tracks": [page["track"] for page in pages],
        "root_alias_tracks": [],
        "discovery_counts": discovery_counts,
        "product_brand_policy": "preserve_source_only",
    }
    connection.execute(
        """
        INSERT INTO build_manifest VALUES (
            1,?,?,?,?,?,?,?,?,?,?,?,?,?,?
        )
        """,
        (
            built_at,
            BUILDER_VERSION,
            SCHEMA_VERSION,
            PARSER_VERSION,
            POLICY_VERSION,
            limit,
            int(limit is None or len(selected) == source_record_count),
            len(all_pages),
            source_record_count,
            len(selected),
            canonical_json(dict(sorted(status_counts.items()))),
            canonical_json(dict(sorted(subject_counts.items()))),
            canonical_json(dict(sorted(company_counts.items()))),
            canonical_json(summary),
        ),
    )
    return {
        "page_count": len(all_pages),
        "track_page_count": len(pages),
        "source_record_count": source_record_count,
        "selected_record_count": len(selected),
        "status_counts": dict(sorted(status_counts.items())),
        "subject_counts": dict(sorted(subject_counts.items())),
        "company_counts": dict(sorted(company_counts.items())),
        "discovery_counts": discovery_counts,
        "root_alias_tracks": summary["root_alias_tracks"],
        "selected_page_version_count": len(all_pages),
        "distinct_physical_snapshot_count": len(distinct_physical_snapshots),
        "snapshot_manifest_size_bytes": snapshot_manifest_size,
    }


def _expected_schema_meta() -> dict[str, str]:
    return {
        "builder_version": BUILDER_VERSION,
        "schema_version": SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "policy_version": POLICY_VERSION,
        "source_scope": "Architizer A+Awards source corpus",
        "immutability": "no-overwrite output; recrawl sidecar mode=ro&immutable=1",
        "selected_snapshot_count_semantics": (
            "page-version count (year root plus official track pages); "
            "distinct physical gzip count is recorded in input_lineage.metadata_json"
        ),
    }


def _snapshot_manifest_bytes_from_output_pages(
    pages: list[sqlite3.Row],
) -> bytes:
    """Rebuild the exact manifest committed by ``build_awards_database``."""

    lines = [
        "|".join(
            (
                str(page["http_attempt_id"]),
                str(page["page_kind"]),
                str(page["award_track"] or ""),
                str(page["requested_url"]),
                str(page["final_url"]),
                str(page["final_url_policy"]),
                str(page["http_status"]),
                str(page["content_type"]),
                str(page["response_bytes"]),
                str(page["snapshot_gzip_path"]),
                str(page["snapshot_content_sha256"]),
                str(page["snapshot_gzip_sha256"]),
                str(page["response_bytes"]),
                str(page["snapshot_gzip_bytes"]),
            )
        )
        for page in pages
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def validate_release_contract(connection: sqlite3.Connection) -> dict[str, Any]:
    """Recompute the self-contained awards release contract from SQLite rows."""

    connection.row_factory = sqlite3.Row
    meta = dict(connection.execute("SELECT key,value FROM schema_meta"))
    if meta != _expected_schema_meta():
        raise AwardsBuildError("output schema_meta contract mismatch")
    lineage_rows = connection.execute("SELECT * FROM input_lineage").fetchall()
    manifest_rows = connection.execute("SELECT * FROM build_manifest").fetchall()
    if len(lineage_rows) != 1 or len(manifest_rows) != 1:
        raise AwardsBuildError("output lineage/manifest cardinality mismatch")
    lineage = lineage_rows[0]
    manifest = manifest_rows[0]
    if (
        lineage["id"] != 1
        or lineage["input_kind"] != "architizer_recrawl_v2_award_census"
        or lineage["sqlite_open_mode"]
        != "mode=ro&immutable=1;query_only=ON"
        or lineage["recrawl_run_kind"] != "award_seed_census_2026"
        or lineage["recrawl_run_status"] != "completed"
        or not str(lineage["recrawl_parser_version"] or "").strip()
        or lineage["sidecar_sha256_before"] != lineage["sidecar_sha256_after"]
    ):
        raise AwardsBuildError("output input_lineage run contract mismatch")
    if (
        manifest["builder_version"] != BUILDER_VERSION
        or manifest["schema_version"] != SCHEMA_VERSION
        or manifest["parser_version"] != PARSER_VERSION
        or manifest["policy_version"] != POLICY_VERSION
    ):
        raise AwardsBuildError("output build_manifest version contract mismatch")
    try:
        manifest_summary = json.loads(manifest["summary_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise AwardsBuildError("output build_manifest summary is invalid") from exc
    if (
        not isinstance(manifest_summary, dict)
        or manifest_summary.get("root_alias_tracks") != []
    ):
        raise AwardsBuildError("output exact final-URL policy contract mismatch")

    pages = connection.execute(
        "SELECT * FROM award_page_versions ORDER BY id"
    ).fetchall()
    if (
        len(pages) != int(manifest["page_count"])
        or len(pages) != int(lineage["selected_snapshot_count"])
    ):
        raise AwardsBuildError("output page/lineage cardinality mismatch")
    try:
        lineage_metadata = json.loads(lineage["metadata_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise AwardsBuildError("output input_lineage metadata is invalid") from exc
    if not isinstance(lineage_metadata, dict):
        raise AwardsBuildError("output input_lineage metadata is invalid")
    manifest_bytes = _snapshot_manifest_bytes_from_output_pages(pages)
    manifest_sha = sha256_bytes(manifest_bytes)
    distinct_snapshots = {
        (str(page["snapshot_gzip_path"]), str(page["snapshot_gzip_sha256"]))
        for page in pages
    }
    if (
        lineage["snapshot_manifest_sha256"] != manifest_sha
        or lineage_metadata.get("snapshot_manifest_size_bytes")
        != len(manifest_bytes)
        or lineage_metadata.get("selected_snapshot_count_semantics")
        != "page_versions"
        or lineage_metadata.get("selected_page_version_count") != len(pages)
        or lineage_metadata.get("distinct_physical_snapshot_count")
        != len(distinct_snapshots)
    ):
        raise AwardsBuildError("output snapshot manifest evidence mismatch")
    roots = [page for page in pages if page["page_kind"] == "year_root"]
    try:
        root_status_counts = (
            json.loads(roots[0]["status_counts_json"]) if len(roots) == 1 else None
        )
    except (TypeError, json.JSONDecodeError) as exc:
        raise AwardsBuildError("output award year-root evidence mismatch") from exc
    if (
        len(roots) != 1
        or roots[0]["award_track"] is not None
        or roots[0]["parse_status"] != "complete"
        or int(roots[0]["source_record_count"]) != 0
        or int(roots[0]["selected_record_count"]) != 0
        or root_status_counts != {}
    ):
        raise AwardsBuildError("output award year-root evidence mismatch")
    root_page = roots[0]
    canonical_root_url = _canonical_winners_url(
        str(root_page["requested_url"]), int(root_page["award_year"]), None
    )
    if (
        canonical_root_url is None
        or root_page["final_url"] != canonical_root_url
        or root_page["final_url_policy"] != "exact"
    ):
        raise AwardsBuildError("output award year-root exact final-URL mismatch")
    for page in pages:
        content_type = str(page["content_type"] or "").split(";", 1)[0].strip().lower()
        try:
            duplicate_ids = json.loads(page["duplicate_attribution_ids_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise AwardsBuildError("output page duplicate-ID evidence is invalid") from exc
        if (
            page["input_lineage_id"] != lineage["id"]
            or page["recrawl_run_id"] != lineage["recrawl_run_id"]
            or page["parser_version"] != PARSER_VERSION
            or page["http_status"] != 200
            or content_type != "text/html"
            or int(page["response_bytes"] or 0) <= 0
            or int(page["snapshot_gzip_bytes"] or 0) <= 0
            or not str(page["requested_url"] or "").strip()
            or not str(page["final_url"] or "").strip()
            or not str(page["snapshot_gzip_path"] or "").strip()
            or re.fullmatch(
                r"[0-9A-F]{64}", str(page["snapshot_content_sha256"] or "")
            )
            is None
            or re.fullmatch(
                r"[0-9A-F]{64}", str(page["snapshot_gzip_sha256"] or "")
            )
            is None
            or duplicate_ids != []
        ):
            raise AwardsBuildError("output award page run/HTTP/parser contract mismatch")
        if page["page_kind"] == "track":
            canonical_track_url = _canonical_winners_url(
                str(page["requested_url"]),
                int(page["award_year"]),
                str(page["award_track"]),
            )
            if (
                canonical_track_url is None
                or page["final_url"] != canonical_track_url
                or page["final_url_policy"] != "exact"
            ):
                raise AwardsBuildError("output award track exact final-URL mismatch")
        if int(page["selected_record_count"]) == int(page["source_record_count"]):
            status_counts = {
                str(row["parse_status"]): int(row["n"])
                for row in connection.execute(
                    "SELECT parse_status,COUNT(*) AS n FROM award_attributions "
                    "WHERE page_version_id=? GROUP BY parse_status ORDER BY parse_status",
                    (page["id"],),
                )
            }
            try:
                stored_counts = json.loads(page["status_counts_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise AwardsBuildError("output page status counts are invalid") from exc
            if stored_counts != status_counts:
                raise AwardsBuildError("output page status-count parity mismatch")

    for typology in (
        page
        for page in pages
        if page["page_kind"] == "track" and page["award_track"] == "Typology"
    ):
        if (
            typology["snapshot_content_sha256"]
            != root_page["snapshot_content_sha256"]
            or typology["snapshot_gzip_sha256"]
            != root_page["snapshot_gzip_sha256"]
            or typology["snapshot_gzip_path"] != root_page["snapshot_gzip_path"]
        ):
            raise AwardsBuildError(
                "output Typology deduplicated year-root snapshot mismatch"
            )

    parent_mismatches = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM award_attributions AS a
            LEFT JOIN award_page_versions AS p ON p.id=a.page_version_id
            WHERE p.id IS NULL OR p.page_kind!='track'
               OR a.award_year IS NOT p.award_year
               OR a.award_track IS NOT p.award_track
               OR a.source_url IS NOT p.requested_url
            """
        ).fetchone()[0]
    )
    invalid_global_ids = int(
        connection.execute(
            """
            SELECT COUNT(*) FROM award_attributions
            WHERE attribution_pk IS NULL OR attribution_global_id IS NULL
               OR trim(attribution_global_id)=''
               OR attribution_global_id
                  !='projects.awardattribution.' || attribution_pk
            """
        ).fetchone()[0]
    )
    duplicate_global_ids = int(
        connection.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT attribution_global_id
                FROM award_attributions
                GROUP BY attribution_global_id HAVING COUNT(*)>1
            )
            """
        ).fetchone()[0]
    )
    complete_without_tier = int(
        connection.execute(
            """
            SELECT COUNT(*) FROM award_attributions AS a
            WHERE a.parse_status='complete' AND NOT EXISTS (
                SELECT 1 FROM award_attribution_tiers AS t
                WHERE t.attribution_id=a.id
                  AND t.normalized_tier IS NOT NULL
                  AND trim(t.normalized_tier)!=''
                  AND t.parse_status='agreed'
            )
            """
        ).fetchone()[0]
    )
    if parent_mismatches:
        raise AwardsBuildError(
            f"output award attribution parent parity mismatch: {parent_mismatches}"
        )
    if invalid_global_ids or duplicate_global_ids:
        raise AwardsBuildError(
            "output award attribution global-ID contract mismatch: "
            f"invalid={invalid_global_ids} duplicate={duplicate_global_ids}"
        )
    if complete_without_tier:
        raise AwardsBuildError(
            "output complete attribution tier contract mismatch: "
            f"{complete_without_tier}"
        )
    return {
        "page_count": len(pages),
        "snapshot_manifest_size_bytes": len(manifest_bytes),
        "snapshot_manifest_sha256": manifest_sha,
        "parent_parity_mismatch_count": parent_mismatches,
        "invalid_global_id_count": invalid_global_ids,
        "duplicate_global_id_count": duplicate_global_ids,
        "complete_without_agreed_tier_count": complete_without_tier,
    }


def _validate_output(path: Path, expected_count: int) -> dict[str, Any]:
    connection = sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
    )
    try:
        connection.execute("PRAGMA query_only=ON")
        quick = connection.execute("PRAGMA quick_check").fetchone()[0]
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        count = connection.execute("SELECT COUNT(*) FROM award_attributions").fetchone()[0]
        if quick != "ok" or integrity != "ok" or foreign_keys:
            raise AwardsBuildError(
                "output SQLite validation failed: "
                f"quick={quick}, integrity={integrity}, foreign_keys={len(foreign_keys)}"
            )
        if count != expected_count:
            raise AwardsBuildError(
                f"output attribution count mismatch: {count} != {expected_count}"
            )
        validate_release_contract(connection)
        return {
            "quick_check": quick,
            "integrity_check": integrity,
            "foreign_key_violation_count": len(foreign_keys),
        }
    finally:
        connection.close()


def _is_owned_link(link_path: Path, temp_path: Path) -> bool:
    try:
        return os.path.samefile(link_path, temp_path)
    except (FileNotFoundError, OSError):
        return False


def _unlink_owned_link(link_path: Path, temp_path: Path, owned: bool) -> None:
    if owned and _is_owned_link(link_path, temp_path):
        with contextlib.suppress(FileNotFoundError):
            link_path.unlink()


def _publish_no_overwrite(source: Path, destination: Path, *, label: str) -> None:
    if destination.exists():
        raise AwardsBuildError(
            f"immutable {label} appeared during build: {destination}"
        )
    try:
        os.link(source, destination)
    except FileExistsError as exc:
        raise AwardsBuildError(
            f"immutable {label} appeared during publish: {destination}"
        ) from exc
    except OSError as exc:
        raise AwardsBuildError(
            f"atomic no-overwrite publish failed for {label} {destination}: {exc}"
        ) from exc


def build_awards_database(
    *,
    sidecar_path: Path,
    snapshot_root: Path,
    output_path: Path,
    run_id: Optional[int] = None,
    award_year: Optional[int] = None,
    limit: Optional[int] = None,
) -> dict[str, Any]:
    """Build and atomically publish an immutable DB plus READY-last receipt."""

    sidecar_path = sidecar_path.resolve()
    snapshot_root = snapshot_root.resolve()
    output_path = output_path.resolve()
    ready_path = _ready_path(output_path)
    _validate_build_request(
        output_path=output_path,
        run_id=run_id,
        award_year=award_year,
        limit=limit,
    )
    _validate_paths(sidecar_path, snapshot_root, output_path)
    try:
        with SidecarLock(sidecar_path):
            # The same lock used by recrawl-v2 spans input identity, immutable
            # reads, DB construction, and READY-last publication.
            _validate_sidecar_storage(sidecar_path)
            sidecar_size = sidecar_path.stat().st_size
            sidecar_sha_before = sha256_file(sidecar_path)
            connection = _open_sidecar_readonly(sidecar_path)
            try:
                run = _select_run(
                    connection,
                    run_id=run_id,
                    award_year=award_year,
                )
                root_page, pages = _load_pages(
                    connection,
                    run=run,
                    snapshot_root=snapshot_root,
                )
                discovery_counts = _validate_discoveries(
                    connection,
                    run_id=run["id"],
                    pages=pages,
                )
            finally:
                connection.close()
            sidecar_sha_after = sha256_file(sidecar_path)
            if sidecar_sha_after != sidecar_sha_before:
                raise AwardsBuildError("recrawl sidecar changed during immutable read")
            if sidecar_path.stat().st_size != sidecar_size:
                raise AwardsBuildError("recrawl sidecar size changed during immutable read")
            _validate_sidecar_storage(sidecar_path)

            manifest_pages = [root_page, *pages]
            manifest_lines = [
                "|".join(
                    (
                        str(page["attempt"]["id"]),
                        page["page_kind"],
                        page["track"] or "",
                        page["attempt"]["requested_url"],
                        page["attempt"].get("final_url") or "",
                        page["final_url_policy"],
                        str(page["attempt"]["http_status"]),
                        page["attempt"].get("content_type") or "",
                        str(page["attempt"]["response_bytes"]),
                        page["snapshot"]["relative_path"],
                        page["snapshot"]["content_sha256"],
                        page["snapshot"]["gzip_sha256"],
                        str(page["snapshot"]["content_bytes"]),
                        str(page["snapshot"]["gzip_bytes"]),
                    )
                )
                for page in manifest_pages
            ]
            snapshot_manifest_bytes = (
                "\n".join(manifest_lines) + "\n"
            ).encode("utf-8")
            snapshot_manifest_sha = sha256_bytes(snapshot_manifest_bytes)
            snapshot_manifest_size = len(snapshot_manifest_bytes)
            selected = _round_robin_records(pages, limit)
            if not selected:
                raise AwardsBuildError("award build selected no attribution records")

            built_at = str(run["finished_at"])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temp_name = tempfile.mkstemp(
                prefix=f".{output_path.name}.",
                suffix=".tmp",
                dir=output_path.parent,
            )
            os.close(descriptor)
            temp_path = Path(temp_name)
            temp_ready_path: Optional[Path] = None
            owned_output = False
            owned_ready = False
            success = False
            try:
                output_connection = sqlite3.connect(temp_path)
                try:
                    output_connection.execute("PRAGMA journal_mode=DELETE")
                    output_connection.execute("PRAGMA foreign_keys=ON")
                    output_connection.execute("PRAGMA synchronous=FULL")
                    summary = _insert_output(
                        output_connection,
                        built_at=built_at,
                        sidecar_path=sidecar_path,
                        sidecar_size=sidecar_size,
                        sidecar_sha_before=sidecar_sha_before,
                        sidecar_sha_after=sidecar_sha_after,
                        snapshot_root=snapshot_root,
                        snapshot_manifest_sha=snapshot_manifest_sha,
                        snapshot_manifest_size=snapshot_manifest_size,
                        run=run,
                        root_page=root_page,
                        pages=pages,
                        selected=selected,
                        limit=limit,
                        discovery_counts=discovery_counts,
                    )
                    output_connection.commit()
                    output_connection.execute("VACUUM")
                finally:
                    output_connection.close()
                validation = _validate_output(temp_path, len(selected))
                output_size = temp_path.stat().st_size
                output_sha = sha256_file(temp_path)

                run_identity = {
                    "id": run["id"],
                    "run_kind": run["run_kind"],
                    "status": run["status"],
                    "parser_version": run["parser_version"],
                    "started_at": run["started_at"],
                    "finished_at": run["finished_at"],
                    "source_db_sha256": run["source_db_sha256_before"],
                    "source_db_size_bytes": run["source_db_size"],
                    "summary_sha256": sha256_bytes(
                        canonical_json(run["summary"]).encode("utf-8")
                    ),
                }
                run_identity_bytes = (
                    canonical_json(run_identity) + "\n"
                ).encode("utf-8")
                ready_payload = {
                    "artifact": "architizer_awards_v2",
                    "ready_version": READY_VERSION,
                    "builder_version": BUILDER_VERSION,
                    "schema_version": SCHEMA_VERSION,
                    "parser_version": PARSER_VERSION,
                    "policy_version": POLICY_VERSION,
                    "built_at": built_at,
                    "award_year": run["award_year"],
                    "build_limit": limit,
                    "database": {
                        "path": _stable_path_label(output_path),
                        "size_bytes": output_size,
                        "sha256": output_sha,
                    },
                    "input_sidecar": {
                        "path": _stable_path_label(sidecar_path),
                        "schema_version": STATE_SCHEMA_VERSION,
                        "size_bytes": sidecar_size,
                        "sha256_before": sidecar_sha_before,
                        "sha256_after": sidecar_sha_after,
                    },
                    "recrawl_run": {
                        **run_identity,
                        "identity_size_bytes": len(run_identity_bytes),
                        "identity_sha256": sha256_bytes(run_identity_bytes),
                    },
                    "snapshot_manifest": {
                        "size_bytes": snapshot_manifest_size,
                        "sha256": snapshot_manifest_sha,
                        "page_version_count": summary[
                            "selected_page_version_count"
                        ],
                        "distinct_physical_snapshot_count": summary[
                            "distinct_physical_snapshot_count"
                        ],
                    },
                    "validation": validation,
                }
                ready_bytes = (canonical_json(ready_payload) + "\n").encode(
                    "utf-8"
                )
                ready_descriptor, temp_ready_name = tempfile.mkstemp(
                    prefix=f".{ready_path.name}.",
                    suffix=".tmp",
                    dir=output_path.parent,
                )
                temp_ready_path = Path(temp_ready_name)
                with os.fdopen(ready_descriptor, "wb") as handle:
                    handle.write(ready_bytes)
                    handle.flush()
                    os.fsync(handle.fileno())

                # Reconfirm the input identity immediately before publication.
                if sha256_file(sidecar_path) != sidecar_sha_before:
                    raise AwardsBuildError(
                        "recrawl sidecar changed before immutable publish"
                    )
                if sidecar_path.stat().st_size != sidecar_size:
                    raise AwardsBuildError(
                        "recrawl sidecar size changed before immutable publish"
                    )
                _validate_sidecar_storage(sidecar_path)
                _publish_no_overwrite(temp_path, output_path, label="output DB")
                owned_output = True
                _publish_no_overwrite(
                    temp_ready_path,
                    ready_path,
                    label="READY receipt",
                )
                owned_ready = True

                if sha256_file(sidecar_path) != sidecar_sha_before:
                    raise AwardsBuildError(
                        "recrawl sidecar changed during immutable publish"
                    )
                _validate_sidecar_storage(sidecar_path)
                if sha256_file(output_path) != output_sha:
                    raise AwardsBuildError("published output DB SHA mismatch")
                ready_sha = sha256_file(ready_path)
                if ready_sha != sha256_bytes(ready_bytes):
                    raise AwardsBuildError("published READY receipt SHA mismatch")
                success = True
                return {
                    "builder_version": BUILDER_VERSION,
                    "schema_version": SCHEMA_VERSION,
                    "parser_version": PARSER_VERSION,
                    "policy_version": POLICY_VERSION,
                    "output_path": str(output_path),
                    "output_size_bytes": output_size,
                    "output_sha256": output_sha,
                    "ready_path": str(ready_path),
                    "ready_size_bytes": len(ready_bytes),
                    "ready_sha256": ready_sha,
                    "input_sidecar_sha256_before": sidecar_sha_before,
                    "input_sidecar_sha256_after": sidecar_sha_after,
                    "snapshot_manifest_sha256": snapshot_manifest_sha,
                    "snapshot_manifest_size_bytes": snapshot_manifest_size,
                    "recrawl_run_id": run["id"],
                    "award_year": run["award_year"],
                    "build_limit": limit,
                    "validation": validation,
                    **summary,
                }
            finally:
                if not success:
                    if temp_ready_path is not None:
                        _unlink_owned_link(
                            ready_path,
                            temp_ready_path,
                            owned_ready,
                        )
                    _unlink_owned_link(output_path, temp_path, owned_output)
                if temp_ready_path is not None and temp_ready_path.exists():
                    temp_ready_path.unlink()
                if temp_path.exists():
                    temp_path.unlink()
    except LockHeldError as exc:
        raise AwardsBuildError(str(exc)) from exc
