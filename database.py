"""SQLite database layer for the Metalocus crawler."""

import json
import sqlite3
from contextlib import contextmanager

import config
from models import BuildingData


def get_connection():
    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS crawl_pages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                url             TEXT UNIQUE NOT NULL,
                category        TEXT NOT NULL,
                page_number     INTEGER NOT NULL,
                status          TEXT DEFAULT 'pending',
                discovered_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                crawled_at      TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS articles (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                url             TEXT UNIQUE NOT NULL,
                slug            TEXT UNIQUE NOT NULL,
                status          TEXT DEFAULT 'pending',
                source_page_url TEXT,
                discovered_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                crawled_at      TIMESTAMP,
                error_message   TEXT
            );

            CREATE TABLE IF NOT EXISTS buildings (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id       INTEGER UNIQUE NOT NULL REFERENCES articles(id),
                title            TEXT,
                architects       TEXT,
                location         TEXT,
                city             TEXT,
                country          TEXT,
                year             TEXT,
                area_sqm         TEXT,
                building_type    TEXT,
                description      TEXT,
                credits          TEXT,
                materials        TEXT,
                publication_date TEXT,
                raw_metadata     TEXT
            );

            CREATE TABLE IF NOT EXISTS tags (
                id   INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL
            );

            CREATE TABLE IF NOT EXISTS article_tags (
                article_id INTEGER NOT NULL REFERENCES articles(id),
                tag_id     INTEGER NOT NULL REFERENCES tags(id),
                PRIMARY KEY (article_id, tag_id)
            );

            CREATE TABLE IF NOT EXISTS images (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id      INTEGER NOT NULL REFERENCES articles(id),
                url             TEXT NOT NULL,
                filename        TEXT NOT NULL,
                local_path      TEXT,
                alt_text        TEXT,
                image_order     INTEGER,
                status          TEXT DEFAULT 'pending',
                downloaded_at   TIMESTAMP,
                file_size_bytes INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_articles_status ON articles(status);
            CREATE INDEX IF NOT EXISTS idx_images_status ON images(status);
            CREATE INDEX IF NOT EXISTS idx_images_article ON images(article_id);
            CREATE INDEX IF NOT EXISTS idx_crawl_pages_status ON crawl_pages(status);
        """)


# --- Crawl pages ---

def add_crawl_page(url, category, page_number):
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO crawl_pages (url, category, page_number) VALUES (?, ?, ?)",
            (url, category, page_number),
        )


def get_pending_crawl_pages(category=None):
    with get_db() as conn:
        if category:
            rows = conn.execute(
                "SELECT * FROM crawl_pages WHERE status='pending' AND category=? ORDER BY page_number",
                (category,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM crawl_pages WHERE status='pending' ORDER BY page_number"
            ).fetchall()
        return [dict(r) for r in rows]


def mark_crawl_page_done(url):
    with get_db() as conn:
        conn.execute(
            "UPDATE crawl_pages SET status='completed', crawled_at=CURRENT_TIMESTAMP WHERE url=?",
            (url,),
        )


def mark_crawl_page_failed(url, error=""):
    with get_db() as conn:
        conn.execute(
            "UPDATE crawl_pages SET status='failed' WHERE url=?",
            (url,),
        )


# --- Articles ---

def add_article(url, slug, source_page_url=None):
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO articles (url, slug, source_page_url) VALUES (?, ?, ?)",
            (url, slug, source_page_url),
        )


def get_pending_articles(limit=None):
    with get_db() as conn:
        query = "SELECT * FROM articles WHERE status='pending' ORDER BY id"
        if limit:
            query += f" LIMIT {int(limit)}"
        rows = conn.execute(query).fetchall()
        return [dict(r) for r in rows]


def mark_article_done(article_id):
    with get_db() as conn:
        conn.execute(
            "UPDATE articles SET status='completed', crawled_at=CURRENT_TIMESTAMP WHERE id=?",
            (article_id,),
        )


def mark_article_failed(article_id, error=""):
    with get_db() as conn:
        conn.execute(
            "UPDATE articles SET status='failed', error_message=? WHERE id=?",
            (error, article_id),
        )


def mark_article_skipped(article_id, reason=""):
    with get_db() as conn:
        conn.execute(
            "UPDATE articles SET status='skipped', error_message=? WHERE id=?",
            (reason, article_id),
        )


# --- Buildings ---

def save_building(article_id, data: BuildingData):
    with get_db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO buildings
               (article_id, title, architects, location, city, country, year,
                area_sqm, building_type, description, credits, materials,
                publication_date, raw_metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                article_id,
                data.title,
                data.architects,
                data.location,
                data.city,
                data.country,
                data.year,
                data.area_sqm,
                data.building_type,
                data.description,
                data.credits,
                data.materials,
                data.publication_date,
                data.raw_metadata,
            ),
        )


# --- Tags ---

def save_tags(article_id, tag_names):
    with get_db() as conn:
        for name in tag_names:
            name = name.strip()
            if not name:
                continue
            conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
            tag_row = conn.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()
            if tag_row:
                conn.execute(
                    "INSERT OR IGNORE INTO article_tags (article_id, tag_id) VALUES (?, ?)",
                    (article_id, tag_row["id"]),
                )


# --- Images ---

def add_image(article_id, url, filename, alt_text="", image_order=0):
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM images WHERE article_id=? AND url=?",
            (article_id, url),
        ).fetchone()
        if not existing:
            conn.execute(
                """INSERT INTO images (article_id, url, filename, alt_text, image_order)
                   VALUES (?, ?, ?, ?, ?)""",
                (article_id, url, filename, alt_text, image_order),
            )


def get_pending_images(limit=None):
    with get_db() as conn:
        query = """SELECT i.*, a.slug FROM images i
                   JOIN articles a ON i.article_id = a.id
                   WHERE i.status='pending' ORDER BY i.id"""
        if limit:
            query += f" LIMIT {int(limit)}"
        rows = conn.execute(query).fetchall()
        return [dict(r) for r in rows]


def mark_image_done(image_id, local_path, file_size):
    with get_db() as conn:
        conn.execute(
            """UPDATE images SET status='completed', local_path=?,
               file_size_bytes=?, downloaded_at=CURRENT_TIMESTAMP WHERE id=?""",
            (local_path, file_size, image_id),
        )


def mark_image_failed(image_id, error=""):
    with get_db() as conn:
        conn.execute("UPDATE images SET status='failed' WHERE id=?", (image_id,))


def get_completed_article_count():
    """Return number of articles with status='completed'."""
    with get_db() as conn:
        return conn.execute(
            "SELECT COUNT(*) as c FROM articles WHERE status='completed'"
        ).fetchone()["c"]


# --- Stats ---

def get_stats():
    with get_db() as conn:
        stats = {}
        for table in ["crawl_pages", "articles", "images"]:
            total = conn.execute(f"SELECT COUNT(*) as c FROM {table}").fetchone()["c"]
            pending = conn.execute(
                f"SELECT COUNT(*) as c FROM {table} WHERE status='pending'"
            ).fetchone()["c"]
            completed = conn.execute(
                f"SELECT COUNT(*) as c FROM {table} WHERE status='completed'"
            ).fetchone()["c"]
            failed = conn.execute(
                f"SELECT COUNT(*) as c FROM {table} WHERE status='failed'"
            ).fetchone()["c"]
            row = {"total": total, "pending": pending, "completed": completed, "failed": failed}
            if table == "articles":
                row["skipped"] = conn.execute(
                    "SELECT COUNT(*) as c FROM articles WHERE status='skipped'"
                ).fetchone()["c"]
            stats[table] = row

        buildings_count = conn.execute("SELECT COUNT(*) as c FROM buildings").fetchone()["c"]
        tags_count = conn.execute("SELECT COUNT(*) as c FROM tags").fetchone()["c"]
        stats["buildings"] = {"total": buildings_count}
        stats["tags"] = {"total": tags_count}
        return stats


# --- Retry ---

def reset_failed(table=None):
    tables = [table] if table else ["crawl_pages", "articles", "images"]
    with get_db() as conn:
        for t in tables:
            conn.execute(f"UPDATE {t} SET status='pending' WHERE status='failed'")
