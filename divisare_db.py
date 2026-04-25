"""SQLite layer for the Divisare crawler (Phase 1).

Separate database (`data/divisare.db`) so the metalocus pipeline's `data/metalocus.db`
stays untouched. Schema mirrors the metalocus pattern (queue rows + content rows)
but with Divisare's canonical numeric IDs as primary keys.

Three entity tables + two queue tables:
  divisare_architects  — canonical architect/firm rows (id from Divisare URL)
  divisare_projects    — canonical project rows (id from Divisare URL)
  divisare_tags        — canonical tag/album rows (slug as PK; Divisare doesn't
                          publish a numeric tag ID we can rely on)
  pending_architects   — discovery queue: architect URLs to fetch (status pending|done|failed)
  pending_projects     — discovery queue: project URLs to fetch
  pending_tags         — discovery queue: tag slugs to fetch
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Optional

import config


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DIVISARE_DB_PATH)
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
            CREATE TABLE IF NOT EXISTS divisare_architects (
                id              INTEGER PRIMARY KEY,        -- Divisare numeric author id
                slug            TEXT UNIQUE NOT NULL,
                name            TEXT NOT NULL,
                description     TEXT,
                country         TEXT,
                city            TEXT,
                website         TEXT,
                phone           TEXT,
                project_count_seen INTEGER DEFAULT 0,
                fetched_at      TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS divisare_projects (
                id              INTEGER PRIMARY KEY,        -- Divisare numeric project id
                slug            TEXT UNIQUE NOT NULL,
                name            TEXT NOT NULL,
                architect_ids   TEXT,                       -- JSON array
                architect_names TEXT,                       -- JSON array (display)
                location_country TEXT,
                location_city    TEXT,
                project_year     INTEGER,
                area_sqm         REAL,                      -- parsed when sidebar has "Area"
                abstract         TEXT,
                description      TEXT,
                tag_slugs        TEXT,                      -- JSON array of tag slugs
                cover_image_url  TEXT,
                gallery_urls     TEXT,                      -- JSON array of CDN URLs (lazy-loaded gallery)
                credits          TEXT,                      -- JSON dict: {role: [firm_names]}
                                                            -- (Design/Designer keys skipped — already
                                                            --  in architect_ids/architect_names)
                fetched_at       TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS divisare_tags (
                slug            TEXT PRIMARY KEY,
                name            TEXT NOT NULL,
                curated         INTEGER DEFAULT 0,           -- 1 if title says "curated by Divisare"
                project_count_seen INTEGER DEFAULT 0,
                fetched_at      TIMESTAMP
            );

            -- Top-level curated browsing categories from the homepage mega-menu
            -- (Elements, Cities, Houses, Ideas, Materiality, Plans & Details,
            --  Private Interiors, Public Interiors, Topics, Types,
            --  designers by Country, designers by City,
            --  photographers by Country, photographers by City)
            CREATE TABLE IF NOT EXISTS divisare_albums (
                slug            TEXT PRIMARY KEY,            -- normalized: "elements", "plans-details", ...
                name            TEXT NOT NULL,               -- display: "Elements", "Plans & Details"
                kind            TEXT NOT NULL,               -- 'tag_album'|'designer_index'|'photographer_index'
                child_count     INTEGER DEFAULT 0,
                fetched_at      TIMESTAMP
            );

            -- Which child (tag slug or designer-index path) belongs to which album
            CREATE TABLE IF NOT EXISTS divisare_album_membership (
                album_slug      TEXT NOT NULL,               -- FK divisare_albums.slug
                child_slug      TEXT NOT NULL,               -- last URL segment (e.g. 'aarhus', 'albania')
                child_name      TEXT NOT NULL,               -- display name
                child_url       TEXT NOT NULL,               -- original href (full path)
                PRIMARY KEY (album_slug, child_slug)
            );

            CREATE TABLE IF NOT EXISTS pending_architects (
                url            TEXT PRIMARY KEY,             -- /authors/{id}-{slug}
                status         TEXT DEFAULT 'pending',       -- pending|done|failed
                discovered_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                fetched_at     TIMESTAMP,
                error          TEXT
            );

            CREATE TABLE IF NOT EXISTS pending_projects (
                url            TEXT PRIMARY KEY,             -- /projects/{id}-{slug}
                source_url     TEXT,                         -- where we discovered it
                status         TEXT DEFAULT 'pending',
                discovered_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                fetched_at     TIMESTAMP,
                error          TEXT
            );

            CREATE TABLE IF NOT EXISTS pending_tags (
                slug           TEXT PRIMARY KEY,             -- e.g. 'chapels'
                status         TEXT DEFAULT 'pending',
                discovered_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                fetched_at     TIMESTAMP,
                error          TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_pending_arch_status ON pending_architects(status);
            CREATE INDEX IF NOT EXISTS idx_pending_proj_status ON pending_projects(status);
            CREATE INDEX IF NOT EXISTS idx_pending_tag_status  ON pending_tags(status);
            CREATE INDEX IF NOT EXISTS idx_proj_country        ON divisare_projects(location_country);
            CREATE INDEX IF NOT EXISTS idx_proj_year           ON divisare_projects(project_year);
            CREATE INDEX IF NOT EXISTS idx_album_member_child  ON divisare_album_membership(child_slug);
        """)


# ---------------------------------------------------------------------------
# Queue ops — discovery
# ---------------------------------------------------------------------------

def enqueue_architect(url: str) -> bool:
    """Insert a pending architect URL. Returns True if newly added."""
    with get_db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO pending_architects(url) VALUES (?)", (url,)
        )
        return cur.rowcount > 0


def enqueue_project(url: str, source_url: Optional[str] = None) -> bool:
    """Insert a pending project URL. Returns True if newly added."""
    with get_db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO pending_projects(url, source_url) VALUES (?, ?)",
            (url, source_url),
        )
        return cur.rowcount > 0


def enqueue_tag(slug: str) -> bool:
    """Insert a pending tag slug. Returns True if newly added."""
    with get_db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO pending_tags(slug) VALUES (?)", (slug,)
        )
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Queue ops — claim/mark
# ---------------------------------------------------------------------------

def get_pending(table: str, limit: Optional[int] = None) -> list:
    assert table in ("pending_architects", "pending_projects", "pending_tags")
    sql = f"SELECT * FROM {table} WHERE status = 'pending' ORDER BY discovered_at"
    if limit:
        sql += f" LIMIT {int(limit)}"
    with get_db() as conn:
        return [dict(r) for r in conn.execute(sql).fetchall()]


def mark_done(table: str, key_field: str, key_value: str) -> None:
    assert table in ("pending_architects", "pending_projects", "pending_tags")
    with get_db() as conn:
        conn.execute(
            f"UPDATE {table} SET status='done', fetched_at=CURRENT_TIMESTAMP, error=NULL "
            f"WHERE {key_field} = ?",
            (key_value,),
        )


def mark_failed(table: str, key_field: str, key_value: str, error: str) -> None:
    assert table in ("pending_architects", "pending_projects", "pending_tags")
    with get_db() as conn:
        conn.execute(
            f"UPDATE {table} SET status='failed', fetched_at=CURRENT_TIMESTAMP, error=? "
            f"WHERE {key_field} = ?",
            (error[:500], key_value),
        )


# ---------------------------------------------------------------------------
# Entity ops — upsert
# ---------------------------------------------------------------------------

def upsert_architect(data: dict) -> None:
    """Insert or update a row in divisare_architects. `data` keys must match columns."""
    cols = ["id", "slug", "name", "description", "country", "city",
            "website", "phone", "project_count_seen"]
    payload = {k: data.get(k) for k in cols}
    placeholders = ", ".join(":" + c for c in cols)
    update_clause = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "id")
    sql = (f"INSERT INTO divisare_architects ({', '.join(cols)}, fetched_at) "
           f"VALUES ({placeholders}, CURRENT_TIMESTAMP) "
           f"ON CONFLICT(id) DO UPDATE SET {update_clause}, fetched_at=CURRENT_TIMESTAMP")
    with get_db() as conn:
        conn.execute(sql, payload)


def upsert_project(data: dict) -> None:
    """Insert or update a row in divisare_projects. List/dict fields are
    JSON-serialized into their TEXT columns."""
    payload = {k: v for k, v in data.items()}
    json_list_fields = ("architect_ids", "architect_names", "tag_slugs", "gallery_urls")
    for f in json_list_fields:
        if f in payload and not isinstance(payload[f], (str, type(None))):
            payload[f] = json.dumps(payload[f], ensure_ascii=False)
    if "credits" in payload and not isinstance(payload["credits"], (str, type(None))):
        payload["credits"] = json.dumps(payload["credits"], ensure_ascii=False)

    cols = ["id", "slug", "name", "architect_ids", "architect_names",
            "location_country", "location_city", "project_year", "area_sqm",
            "abstract", "description",
            "tag_slugs", "cover_image_url", "gallery_urls", "credits"]
    payload = {k: payload.get(k) for k in cols}
    placeholders = ", ".join(":" + c for c in cols)
    update_clause = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "id")
    sql = (f"INSERT INTO divisare_projects ({', '.join(cols)}, fetched_at) "
           f"VALUES ({placeholders}, CURRENT_TIMESTAMP) "
           f"ON CONFLICT(id) DO UPDATE SET {update_clause}, fetched_at=CURRENT_TIMESTAMP")
    with get_db() as conn:
        conn.execute(sql, payload)


def upsert_project_lite(data: dict, primary_architect_id: int) -> None:
    """Upsert a lightweight project row from architect-page parsing.

    Unlike `upsert_project` (full deep parse), this preserves existing
    architect_canonical_ids by UNIONING new IDs into the JSON array, so
    collaborated projects (e.g. Serie + Multiply Architects) accumulate
    co-architect IDs as each architect's page is processed.

    `primary_architect_id` is the architect whose /projects/built page we're
    currently parsing — guaranteed to be a real Divisare ID.
    """
    payload = {k: v for k, v in data.items()}

    with get_db() as conn:
        # Read existing row to merge architect_ids if present
        existing = conn.execute(
            "SELECT architect_ids, architect_names FROM divisare_projects WHERE id = ?",
            (data["id"],),
        ).fetchone()

        if existing:
            existing_ids = json.loads(existing["architect_ids"]) if existing["architect_ids"] else []
            existing_names = json.loads(existing["architect_names"]) if existing["architect_names"] else []
        else:
            existing_ids, existing_names = [], []

        # Merge: keep existing + add primary_architect_id (dedupe)
        merged_ids = list(dict.fromkeys(existing_ids + [primary_architect_id]))
        # Names: union (preserve order, dedupe)
        new_names = data.get("architect_names") or []
        merged_names = list(dict.fromkeys(existing_names + new_names))

        payload["architect_ids"] = merged_ids
        payload["architect_names"] = merged_names

        # Photographer goes into credits dict (consistent with deep-fetch shape)
        photographer = data.get("photographer")
        if photographer:
            payload["credits"] = {"photo": [photographer]}

        # Co-architects names go into a separate role bucket too (for visibility)
        co_archs = data.get("co_architects")
        if co_archs:
            credits = payload.get("credits") or {}
            credits["collaborators"] = co_archs
            payload["credits"] = credits

    # Now delegate to the regular upsert (which serializes JSON, etc.)
    upsert_project(payload)


def upsert_album(data: dict) -> None:
    """Insert/update an album row + replace its membership rows."""
    cols = ["slug", "name", "kind", "child_count"]
    payload = {k: data.get(k) for k in cols}
    placeholders = ", ".join(":" + c for c in cols)
    update_clause = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "slug")
    sql = (f"INSERT INTO divisare_albums ({', '.join(cols)}, fetched_at) "
           f"VALUES ({placeholders}, CURRENT_TIMESTAMP) "
           f"ON CONFLICT(slug) DO UPDATE SET {update_clause}, fetched_at=CURRENT_TIMESTAMP")
    with get_db() as conn:
        conn.execute(sql, payload)


def replace_album_membership(album_slug: str, members: list[dict]) -> None:
    """Wipe and re-insert all child rows for one album.
    `members` is a list of {child_slug, child_name, child_url} dicts."""
    with get_db() as conn:
        conn.execute("DELETE FROM divisare_album_membership WHERE album_slug = ?",
                     (album_slug,))
        conn.executemany(
            "INSERT INTO divisare_album_membership (album_slug, child_slug, child_name, child_url) "
            "VALUES (?, ?, ?, ?)",
            [(album_slug, m["child_slug"], m["child_name"], m["child_url"]) for m in members],
        )


def upsert_tag(data: dict) -> None:
    cols = ["slug", "name", "curated", "project_count_seen"]
    payload = {k: data.get(k) for k in cols}
    placeholders = ", ".join(":" + c for c in cols)
    update_clause = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "slug")
    sql = (f"INSERT INTO divisare_tags ({', '.join(cols)}, fetched_at) "
           f"VALUES ({placeholders}, CURRENT_TIMESTAMP) "
           f"ON CONFLICT(slug) DO UPDATE SET {update_clause}, fetched_at=CURRENT_TIMESTAMP")
    with get_db() as conn:
        conn.execute(sql, payload)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def stats() -> dict:
    with get_db() as conn:
        out = {}
        for table in ("divisare_architects", "divisare_projects", "divisare_tags"):
            out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("pending_architects", "pending_projects", "pending_tags"):
            counts = dict(conn.execute(
                f"SELECT status, COUNT(*) FROM {table} GROUP BY status"
            ).fetchall())
            out[table] = counts
        return out
