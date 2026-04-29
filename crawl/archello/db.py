"""SQLite layer for the Archello crawler (Phase 8).

Separate database (`data/crawl/archello.db`). Schema mirrors the Divisare
+ Architizer pattern (entity rows + queue rows). Archello's distinguishing
feature — per-project structured product/material specs — gets its own
detail table.

Tables:
  archello_projects         — project rows (id from data-key.project_id)
  archello_project_details  — one row per BIM-source-list entry (architects,
                              photographers, manufacturers, products) with
                              the project_id FK + brand_id
  archello_brands_seen      — brands referenced from project pages, for
                              optional follow-up brand crawl later
  pending_projects          — discovery queue: project URLs to fetch
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
    conn = sqlite3.connect(config.ARCHELLO_DB_PATH)
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
            CREATE TABLE IF NOT EXISTS archello_projects (
                id                  INTEGER PRIMARY KEY,    -- data-key.project_id
                slug                TEXT UNIQUE NOT NULL,
                name                TEXT NOT NULL,
                architect_brand_id  INTEGER,                -- primary architect from credits
                architect_name      TEXT,                   -- title-tag middle segment
                location_full       TEXT,                   -- 'City, Region, Country | View Map'
                location_country    TEXT,
                location_city       TEXT,
                project_year        INTEGER,
                category            TEXT,                   -- 'City Halls' or 'Housing — Private Houses'
                building_area_m2    REAL,
                description         TEXT,
                cover_image_url     TEXT,
                gallery_image_urls  TEXT,                   -- JSON array
                fetched_at          TIMESTAMP
            );

            -- One row per detail item — the BIM-source-list (architects,
            -- photographers, manufacturers, products). Some details link
            -- multiple products (data-key.brand_id may be NULL for those).
            CREATE TABLE IF NOT EXISTS archello_project_details (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id        INTEGER NOT NULL,         -- FK archello_projects.id
                brand_id          INTEGER,                  -- data-key.brand_id (nullable)
                role_or_category  TEXT,                     -- 'Photographers', 'Kitchen', etc.
                brand_slug        TEXT,                     -- /brand/{slug}
                brand_name        TEXT,
                product_slugs     TEXT,                     -- JSON array of /product/{slug}
                product_names     TEXT,                     -- JSON array (display names)
                FOREIGN KEY(project_id) REFERENCES archello_projects(id)
            );

            -- Brands observed during project parsing — popullated for
            -- optional later brand-page crawl. Schema kept minimal here.
            CREATE TABLE IF NOT EXISTS archello_brands_seen (
                slug          TEXT PRIMARY KEY,
                brand_id      INTEGER UNIQUE,
                name          TEXT,
                first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- Architect firms (deep-fetched from /brand/{slug} pages).
            -- Subset of archello_brands_seen filtered to those that have
            -- appeared as an 'Architect' role on any project. Distinct
            -- from product manufacturers (which share /brand/{slug}
            -- namespace per Archello's URL design).
            CREATE TABLE IF NOT EXISTS archello_firms (
                slug                  TEXT PRIMARY KEY,
                brand_id              INTEGER UNIQUE,
                name                  TEXT NOT NULL,
                description_short     TEXT,                   -- og:description
                about                 TEXT,                   -- full About text
                location_country      TEXT,                   -- HQ country
                location_city         TEXT,                   -- HQ city
                offices               TEXT,                   -- JSON array of {city,country,address}
                project_count_archello INTEGER,               -- '246 Projects' string
                website               TEXT,
                social_links          TEXT,                   -- JSON object
                cover_image_url       TEXT,                   -- og:image (usually firm logo)
                fetched_at            TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS pending_projects (
                url            TEXT PRIMARY KEY,            -- /project/{slug}
                source_url     TEXT,                        -- which sitemap shard
                lastmod        TEXT,                        -- from sitemap <lastmod>
                status         TEXT DEFAULT 'pending',      -- pending|done|failed
                discovered_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                fetched_at     TIMESTAMP,
                error          TEXT
            );

            CREATE TABLE IF NOT EXISTS pending_firms (
                url            TEXT PRIMARY KEY,            -- /brand/{slug} (firm subset only)
                source         TEXT DEFAULT 'architect_role', -- how we decided this is a firm
                status         TEXT DEFAULT 'pending',
                discovered_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                fetched_at     TIMESTAMP,
                error          TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_arc_pending_proj_status  ON pending_projects(status);
            CREATE INDEX IF NOT EXISTS idx_arc_pending_firm_status  ON pending_firms(status);
            CREATE INDEX IF NOT EXISTS idx_arc_proj_country         ON archello_projects(location_country);
            CREATE INDEX IF NOT EXISTS idx_arc_proj_year            ON archello_projects(project_year);
            CREATE INDEX IF NOT EXISTS idx_arc_proj_arch_brand      ON archello_projects(architect_brand_id);
            CREATE INDEX IF NOT EXISTS idx_arc_firm_country         ON archello_firms(location_country);
            CREATE INDEX IF NOT EXISTS idx_arc_pdet_project        ON archello_project_details (project_id);
            CREATE INDEX IF NOT EXISTS idx_arc_pdet_brand          ON archello_project_details (brand_id);
        """)


# ---------------------------------------------------------------------------
# Queue ops
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


def bulk_enqueue_projects(rows: list[tuple[str, str, Optional[str]]]) -> int:
    if not rows:
        return 0
    with get_db() as conn:
        cur = conn.executemany(
            "INSERT OR IGNORE INTO pending_projects(url, source_url, lastmod) "
            "VALUES (?, ?, ?)",
            rows,
        )
        return cur.rowcount


def get_pending(limit: Optional[int] = None,
                table: str = "pending_projects") -> list:
    """Read pending rows from either pending_projects or pending_firms."""
    assert table in ("pending_projects", "pending_firms")
    sql = f"SELECT * FROM {table} WHERE status = 'pending' ORDER BY discovered_at"
    if limit:
        sql += f" LIMIT {int(limit)}"
    with get_db() as conn:
        return [dict(r) for r in conn.execute(sql).fetchall()]


def mark_done(url: str, table: str = "pending_projects") -> None:
    assert table in ("pending_projects", "pending_firms")
    with get_db() as conn:
        conn.execute(
            f"UPDATE {table} SET status='done', fetched_at=CURRENT_TIMESTAMP, "
            f"error=NULL WHERE url = ?",
            (url,),
        )


def mark_failed(url: str, error: str, table: str = "pending_projects") -> None:
    assert table in ("pending_projects", "pending_firms")
    with get_db() as conn:
        conn.execute(
            f"UPDATE {table} SET status='failed', "
            f"fetched_at=CURRENT_TIMESTAMP, error=? WHERE url = ?",
            (error[:500], url),
        )


# ---------------------------------------------------------------------------
# Firm queue ops
# ---------------------------------------------------------------------------

def enqueue_firms_from_architect_role() -> int:
    """Find every brand_slug that has appeared as 'Architect' role on
    any project and enqueue its /brand/{slug} URL into pending_firms.
    Idempotent (INSERT OR IGNORE)."""
    with get_db() as conn:
        cur = conn.execute("""
            INSERT OR IGNORE INTO pending_firms (url, source)
            SELECT DISTINCT '/brand/' || brand_slug, 'architect_role'
            FROM archello_project_details
            WHERE brand_slug IS NOT NULL
              AND LOWER(role_or_category) LIKE '%architect%'
        """)
        return cur.rowcount


# ---------------------------------------------------------------------------
# Entity ops
# ---------------------------------------------------------------------------

_PROJECT_COLS = [
    "id", "slug", "name",
    "architect_brand_id", "architect_name",
    "location_full", "location_country", "location_city",
    "project_year", "category", "building_area_m2",
    "description", "cover_image_url", "gallery_image_urls",
]
_PROJECT_JSON_FIELDS = ("gallery_image_urls",)


def upsert_project(data: dict) -> None:
    """Insert/update one archello_projects row."""
    payload = {k: v for k, v in data.items()}
    for f in _PROJECT_JSON_FIELDS:
        if f in payload and not isinstance(payload[f], (str, type(None))):
            payload[f] = json.dumps(payload[f], ensure_ascii=False)
    payload = {k: payload.get(k) for k in _PROJECT_COLS}
    placeholders = ", ".join(":" + c for c in _PROJECT_COLS)
    update_clause = ", ".join(f"{c}=excluded.{c}"
                              for c in _PROJECT_COLS if c != "id")
    sql = (f"INSERT INTO archello_projects ({', '.join(_PROJECT_COLS)}, fetched_at) "
           f"VALUES ({placeholders}, CURRENT_TIMESTAMP) "
           f"ON CONFLICT(id) DO UPDATE SET {update_clause}, fetched_at=CURRENT_TIMESTAMP")
    with get_db() as conn:
        conn.execute(sql, payload)


def replace_project_details(project_id: int, details: list[dict]) -> None:
    """Wipe + reinsert all detail rows for one project. (Re-running deep
    fetch on the same project should give a clean slate, not duplicates.)"""
    with get_db() as conn:
        conn.execute("DELETE FROM archello_project_details WHERE project_id = ?",
                     (project_id,))
        rows = []
        for d in details:
            rows.append((
                project_id,
                d.get("brand_id"),
                d.get("role_or_category"),
                d.get("brand_slug"),
                d.get("brand_name"),
                json.dumps(d.get("product_slugs") or [], ensure_ascii=False),
                json.dumps(d.get("product_names") or [], ensure_ascii=False),
            ))
        conn.executemany(
            "INSERT INTO archello_project_details "
            "(project_id, brand_id, role_or_category, brand_slug, brand_name, "
            " product_slugs, product_names) VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


def upsert_brand_seen(slug: str, brand_id: Optional[int],
                      name: Optional[str]) -> None:
    """Idempotent observation log of brands referenced from project pages."""
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO archello_brands_seen (slug, brand_id, name) "
            "VALUES (?, ?, ?)",
            (slug, brand_id, name),
        )


_FIRM_COLS = [
    "slug", "brand_id", "name", "description_short", "about",
    "location_country", "location_city", "offices",
    "project_count_archello", "website", "social_links", "cover_image_url",
]
_FIRM_JSON_FIELDS = ("offices", "social_links")


def upsert_firm(data: dict) -> None:
    """Insert/update one archello_firms row."""
    payload = {k: v for k, v in data.items()}
    for f in _FIRM_JSON_FIELDS:
        if f in payload and not isinstance(payload[f], (str, type(None))):
            payload[f] = json.dumps(payload[f], ensure_ascii=False)
    payload = {k: payload.get(k) for k in _FIRM_COLS}
    placeholders = ", ".join(":" + c for c in _FIRM_COLS)
    update_clause = ", ".join(f"{c}=excluded.{c}"
                              for c in _FIRM_COLS if c != "slug")
    sql = (f"INSERT INTO archello_firms ({', '.join(_FIRM_COLS)}, fetched_at) "
           f"VALUES ({placeholders}, CURRENT_TIMESTAMP) "
           f"ON CONFLICT(slug) DO UPDATE SET {update_clause}, fetched_at=CURRENT_TIMESTAMP")
    with get_db() as conn:
        conn.execute(sql, payload)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def stats() -> dict:
    with get_db() as conn:
        out = {
            "archello_projects":
                conn.execute("SELECT COUNT(*) FROM archello_projects").fetchone()[0],
            "archello_project_details":
                conn.execute("SELECT COUNT(*) FROM archello_project_details").fetchone()[0],
            "archello_brands_seen":
                conn.execute("SELECT COUNT(*) FROM archello_brands_seen").fetchone()[0],
            "archello_firms":
                conn.execute("SELECT COUNT(*) FROM archello_firms").fetchone()[0],
        }
        for table in ("pending_projects", "pending_firms"):
            counts = dict(conn.execute(
                f"SELECT status, COUNT(*) FROM {table} GROUP BY status"
            ).fetchall())
            out[table] = counts
        return out
