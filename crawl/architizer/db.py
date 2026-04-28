"""SQLite layer for the Architizer crawler (Phase 7).

Separate database (`data/crawl/architizer.db`) so other sources stay
untouched. Schema mirrors the Divisare pattern (entity rows + queue rows)
but uses Architizer's own canonical PKs:
  - project: `data-data.pk` (e.g. 279741) — Django primary key
  - firm: slug only (Architizer firms don't expose a numeric ID in HTML)
  - award: surrogate auto-increment ID; uniqueness enforced via composite
    UNIQUE on (year, track, category, tier, project_slug, firm_slug)

Tables:
  architizer_projects   — canonical project rows (id from `data-data.pk`)
  architizer_firms      — canonical firm rows (slug PK)
  architizer_awards     — A+Awards entries (year × track × tier × project_or_firm)
  pending_projects      — discovery queue: project URLs to fetch
  pending_firms         — discovery queue: firm URLs to fetch
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Optional

from core import config


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(config.ARCHITIZER_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def get_db():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS architizer_projects (
                id                    INTEGER PRIMARY KEY,    -- data-data.pk
                global_id             TEXT UNIQUE,            -- 'projects.project.{pk}'
                slug                  TEXT UNIQUE NOT NULL,   -- from URL
                name                  TEXT NOT NULL,
                firm_slug             TEXT,                   -- /firms/{slug}/
                firm_name             TEXT,
                description           TEXT,
                description_short     TEXT,
                completion_year       INTEGER,                -- parsed from completion_date ISO
                building_size_slug    TEXT,                   -- 'sqft_100_300'
                building_size_display TEXT,                   -- '100,000 sqft - 300,000 sqft'
                constr_status         TEXT,                   -- 'built'|'concept'|...
                budget                REAL,
                location_full         TEXT,                   -- 'New York, NY, United States'
                location_country      TEXT,
                location_city         TEXT,
                categories            TEXT,                   -- JSON array (article:tag values)
                cover_image_url       TEXT,
                gallery_image_urls    TEXT,                   -- JSON array
                image_global_ids      TEXT,                   -- JSON array
                published_time        TEXT,                   -- ISO datetime
                modified_time         TEXT,
                fetched_at            TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS architizer_firms (
                slug                TEXT PRIMARY KEY,
                name                TEXT NOT NULL,
                office_locations    TEXT,                     -- JSON array
                description         TEXT,
                awards_summary      TEXT,                     -- header badge string
                project_count_seen  INTEGER DEFAULT 0,
                social_links        TEXT,                     -- JSON object
                fetched_at          TIMESTAMP
            );

            -- A+Awards: hierarchical, lives at winners.architizer.com.
            -- Either project_slug or firm_slug is non-NULL (track-dependent).
            CREATE TABLE IF NOT EXISTS architizer_awards (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                award_year      INTEGER NOT NULL,
                award_track     TEXT NOT NULL,                -- 'Typology'|'Firms'|'Products'|'Plus'
                award_category  TEXT,                         -- e.g. 'Commercial > Office'
                award_tier      TEXT NOT NULL,                -- 'Jury'|'Popular'|'Finalist'|'Special Mention'
                project_slug    TEXT,                         -- NULL for firm-only awards
                firm_slug       TEXT,                         -- NULL for product-only awards
                source_url      TEXT NOT NULL,
                fetched_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(award_year, award_track, award_category, award_tier,
                       project_slug, firm_slug)
            );

            CREATE TABLE IF NOT EXISTS pending_projects (
                url            TEXT PRIMARY KEY,              -- /projects/{slug}/
                source_url     TEXT,                          -- which sitemap shard
                lastmod        TEXT,                          -- from sitemap <lastmod>
                status         TEXT DEFAULT 'pending',        -- pending|done|failed
                discovered_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                fetched_at     TIMESTAMP,
                error          TEXT
            );

            CREATE TABLE IF NOT EXISTS pending_firms (
                url            TEXT PRIMARY KEY,              -- /firms/{slug}/
                source_url     TEXT,
                lastmod        TEXT,
                status         TEXT DEFAULT 'pending',
                discovered_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                fetched_at     TIMESTAMP,
                error          TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_atz_pending_proj_status ON pending_projects(status);
            CREATE INDEX IF NOT EXISTS idx_atz_pending_firm_status ON pending_firms(status);
            CREATE INDEX IF NOT EXISTS idx_atz_proj_country        ON architizer_projects(location_country);
            CREATE INDEX IF NOT EXISTS idx_atz_proj_year           ON architizer_projects(completion_year);
            CREATE INDEX IF NOT EXISTS idx_atz_proj_firm           ON architizer_projects(firm_slug);
            CREATE INDEX IF NOT EXISTS idx_atz_award_project       ON architizer_awards(project_slug);
            CREATE INDEX IF NOT EXISTS idx_atz_award_firm          ON architizer_awards(firm_slug);
        """)


# ---------------------------------------------------------------------------
# Queue ops — discovery
# ---------------------------------------------------------------------------

def enqueue_project(url: str, source_url: Optional[str] = None,
                    lastmod: Optional[str] = None) -> bool:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO pending_projects(url, source_url, lastmod) "
            "VALUES (?, ?, ?)",
            (url, source_url, lastmod),
        )
        return cur.rowcount > 0


def enqueue_firm(url: str, source_url: Optional[str] = None,
                 lastmod: Optional[str] = None) -> bool:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO pending_firms(url, source_url, lastmod) "
            "VALUES (?, ?, ?)",
            (url, source_url, lastmod),
        )
        return cur.rowcount > 0


def bulk_enqueue_projects(urls: list[tuple[str, str, Optional[str]]]) -> int:
    """Bulk insert (url, source_url, lastmod) tuples. Returns rows added."""
    if not urls:
        return 0
    with get_db() as conn:
        cur = conn.executemany(
            "INSERT OR IGNORE INTO pending_projects(url, source_url, lastmod) "
            "VALUES (?, ?, ?)",
            urls,
        )
        return cur.rowcount


def bulk_enqueue_firms(urls: list[tuple[str, str, Optional[str]]]) -> int:
    if not urls:
        return 0
    with get_db() as conn:
        cur = conn.executemany(
            "INSERT OR IGNORE INTO pending_firms(url, source_url, lastmod) "
            "VALUES (?, ?, ?)",
            urls,
        )
        return cur.rowcount


# ---------------------------------------------------------------------------
# Queue ops — claim/mark
# ---------------------------------------------------------------------------

def get_pending(table: str, limit: Optional[int] = None) -> list:
    assert table in ("pending_projects", "pending_firms")
    sql = f"SELECT * FROM {table} WHERE status = 'pending' ORDER BY discovered_at"
    if limit:
        sql += f" LIMIT {int(limit)}"
    with get_db() as conn:
        return [dict(r) for r in conn.execute(sql).fetchall()]


def mark_done(table: str, key_field: str, key_value: str) -> None:
    assert table in ("pending_projects", "pending_firms")
    with get_db() as conn:
        conn.execute(
            f"UPDATE {table} SET status='done', fetched_at=CURRENT_TIMESTAMP, error=NULL "
            f"WHERE {key_field} = ?",
            (key_value,),
        )


def mark_failed(table: str, key_field: str, key_value: str, error: str) -> None:
    assert table in ("pending_projects", "pending_firms")
    with get_db() as conn:
        conn.execute(
            f"UPDATE {table} SET status='failed', fetched_at=CURRENT_TIMESTAMP, error=? "
            f"WHERE {key_field} = ?",
            (error[:500], key_value),
        )


# ---------------------------------------------------------------------------
# Entity ops — upsert
# ---------------------------------------------------------------------------

_PROJECT_COLS = [
    "id", "global_id", "slug", "name",
    "firm_slug", "firm_name",
    "description", "description_short",
    "completion_year",
    "building_size_slug", "building_size_display",
    "constr_status", "budget",
    "location_full", "location_country", "location_city",
    "categories", "cover_image_url", "gallery_image_urls", "image_global_ids",
    "published_time", "modified_time",
]
_PROJECT_JSON_FIELDS = ("categories", "gallery_image_urls", "image_global_ids")


def upsert_project(data: dict) -> None:
    """Insert or update a row in architizer_projects. List/dict fields
    are JSON-serialized."""
    payload = {k: v for k, v in data.items()}
    for f in _PROJECT_JSON_FIELDS:
        if f in payload and not isinstance(payload[f], (str, type(None))):
            payload[f] = json.dumps(payload[f], ensure_ascii=False)
    payload = {k: payload.get(k) for k in _PROJECT_COLS}
    placeholders = ", ".join(":" + c for c in _PROJECT_COLS)
    update_clause = ", ".join(f"{c}=excluded.{c}"
                              for c in _PROJECT_COLS if c != "id")
    sql = (f"INSERT INTO architizer_projects ({', '.join(_PROJECT_COLS)}, fetched_at) "
           f"VALUES ({placeholders}, CURRENT_TIMESTAMP) "
           f"ON CONFLICT(id) DO UPDATE SET {update_clause}, fetched_at=CURRENT_TIMESTAMP")
    with get_db() as conn:
        conn.execute(sql, payload)


_FIRM_COLS = ["slug", "name", "office_locations", "description",
              "awards_summary", "project_count_seen", "social_links"]
_FIRM_JSON_FIELDS = ("office_locations", "social_links")


def upsert_firm(data: dict) -> None:
    payload = {k: v for k, v in data.items()}
    for f in _FIRM_JSON_FIELDS:
        if f in payload and not isinstance(payload[f], (str, type(None))):
            payload[f] = json.dumps(payload[f], ensure_ascii=False)
    payload = {k: payload.get(k) for k in _FIRM_COLS}
    placeholders = ", ".join(":" + c for c in _FIRM_COLS)
    update_clause = ", ".join(f"{c}=excluded.{c}"
                              for c in _FIRM_COLS if c != "slug")
    sql = (f"INSERT INTO architizer_firms ({', '.join(_FIRM_COLS)}, fetched_at) "
           f"VALUES ({placeholders}, CURRENT_TIMESTAMP) "
           f"ON CONFLICT(slug) DO UPDATE SET {update_clause}, fetched_at=CURRENT_TIMESTAMP")
    with get_db() as conn:
        conn.execute(sql, payload)


def insert_award(data: dict) -> None:
    """Insert one A+Award row. Idempotent via the composite UNIQUE constraint —
    re-running phase_awards is safe."""
    cols = ["award_year", "award_track", "award_category", "award_tier",
            "project_slug", "firm_slug", "source_url"]
    payload = {k: data.get(k) for k in cols}
    placeholders = ", ".join(":" + c for c in cols)
    sql = (f"INSERT OR IGNORE INTO architizer_awards ({', '.join(cols)}) "
           f"VALUES ({placeholders})")
    with get_db() as conn:
        conn.execute(sql, payload)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def stats() -> dict:
    with get_db() as conn:
        out = {}
        for table in ("architizer_projects", "architizer_firms",
                      "architizer_awards"):
            out[table] = conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
        for table in ("pending_projects", "pending_firms"):
            counts = dict(conn.execute(
                f"SELECT status, COUNT(*) FROM {table} GROUP BY status"
            ).fetchall())
            out[table] = counts
        return out
