"""SQLite-backed task ledger for the AI pipeline (Phase 3).

Design decisions (see ~/.claude/plans/db-fuzzy-lerdorf.md):
- JSON files (`2_/3_/4_buildings_*.json`) remain canonical for downstream tools.
- This ledger is the source-of-truth for "which API calls have already been made"
  — enabling crash-safe, idempotent, resumable processing.
- The UNIQUE key (`building_id`, `task_type`, `prompt_version`, `input_hash`)
  means re-running the worker with the same inputs + prompt is free.
- `mark_done` commits BEFORE the worker appends to the canonical JSON, so a
  mid-write crash cannot cause duplicate API calls on restart (at the price of
  requiring reconciliation — see `sync_outputs_from_db_to_json`).

Crash model:
- `claim_next` atomically flips pending → running. The `claimed_at` timestamp
  lets `reset_stale` recover tasks from a worker that died silently.
- Generic failures re-open the task (mark_pending) with exponential backoff
  up to HARNESS_MAX_RETRIES attempts.
- Hard failures (no_images, unrecoverable parse errors) mark_failed and don't
  retry.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

import config

TASKS_DB_PATH = os.path.join(config.DATA_DIR, "tasks.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_tasks (
    id              INTEGER PRIMARY KEY,
    building_id     TEXT NOT NULL,
    task_type       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    input_hash      TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,
    vocab_version   TEXT NOT NULL,
    output_json     TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    claimed_at      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE(building_id, task_type, prompt_version, input_hash)
);
CREATE INDEX IF NOT EXISTS idx_tasks_status_type ON ai_tasks(status, task_type);
CREATE INDEX IF NOT EXISTS idx_tasks_building    ON ai_tasks(building_id);
"""


@contextmanager
def _conn():
    os.makedirs(config.DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(TASKS_DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        yield conn
    finally:
        conn.close()


def init() -> None:
    with _conn() as c:
        c.executescript(_SCHEMA)
        c.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_json(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Input hashes — pinned to the exact fields each prompt consumes
# ---------------------------------------------------------------------------

def compute_input_hash_enrich(building: dict) -> str:
    """Fields used by agent_llm_parser._build_prompt. Excludes enriched output."""
    payload = {
        "project_name":     building.get("project_name") or "",
        "architect":        building.get("architect") or "",
        "city":             building.get("city") or "",
        "location_country": building.get("location_country") or "",
        "year":             building.get("year"),
        "building_type":    building.get("building_type") or "",
        "material_raw":     building.get("material") or "",
        "description":      (building.get("description") or "")[:800],
        "tags":             sorted(building.get("tags") or []),
    }
    return _sha256_json(payload)


def compute_input_hash_analyze(building: dict) -> str:
    """Fields used by agent_image_analysis._build_prompt + selected image (name, size)."""
    img_dir = os.path.join(config.IMAGE_BASE_DIR, building["building_id"])
    images = building.get("images") or []
    photos = sorted(
        [img for img in images if img.get("type") == "photo"],
        key=lambda x: x.get("order", 999),
    )
    upload_photos = [img for img in photos if img.get("upload")]
    candidates = upload_photos if upload_photos else photos

    selected = []
    for img in candidates[: config.HARNESS_MAX_IMAGES]:
        fp = os.path.join(img_dir, img["filename"])
        if os.path.exists(fp):
            selected.append([img["filename"], os.path.getsize(fp)])
        if len(selected) >= config.HARNESS_MAX_IMAGES:
            break

    payload = {
        "building_id":      building["building_id"],
        "name_en":          building.get("name_en") or "",
        "program":          building.get("program") or "",
        "city":             building.get("city") or "",
        "location_country": building.get("location_country") or "",
        "images":           selected,
    }
    return _sha256_json(payload)


# ---------------------------------------------------------------------------
# Queue operations
# ---------------------------------------------------------------------------

def enqueue(*, building_id: str, task_type: str, input_hash: str,
            prompt_version: str, vocab_version: str) -> bool:
    """Insert a pending task. Returns True if a new row was created,
    False if the UNIQUE key already exists (treated as idempotent no-op)."""
    with _conn() as c:
        try:
            c.execute("""
                INSERT INTO ai_tasks
                    (building_id, task_type, status, input_hash,
                     prompt_version, vocab_version, created_at, updated_at)
                VALUES (?, ?, 'pending', ?, ?, ?, ?, ?)
            """, (building_id, task_type, input_hash, prompt_version,
                  vocab_version, _now(), _now()))
            c.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def find_done_output(*, building_id: str, task_type: str,
                     input_hash: str, prompt_version: str) -> Optional[dict]:
    """Return the cached output of a done task with matching key, or None."""
    with _conn() as c:
        row = c.execute("""
            SELECT output_json FROM ai_tasks
            WHERE building_id = ? AND task_type = ? AND input_hash = ?
              AND prompt_version = ? AND status = 'done'
            LIMIT 1
        """, (building_id, task_type, input_hash, prompt_version)).fetchone()
    if row is None or row["output_json"] is None:
        return None
    return json.loads(row["output_json"])


def claim_next(task_type: str) -> Optional[dict]:
    """Atomically pick the oldest pending task of task_type, mark as running.

    Returns a plain dict (id, building_id, task_type, prompt_version, vocab_version,
    input_hash, attempts) or None if the queue is empty.
    """
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("""
            SELECT * FROM ai_tasks
            WHERE task_type = ? AND status = 'pending'
            ORDER BY id ASC LIMIT 1
        """, (task_type,)).fetchone()
        if row is None:
            c.execute("COMMIT")
            return None
        c.execute("""
            UPDATE ai_tasks
            SET status = 'running', attempts = attempts + 1,
                claimed_at = ?, updated_at = ?
            WHERE id = ?
        """, (_now(), _now(), row["id"]))
        c.commit()
    return {
        "id":             row["id"],
        "building_id":    row["building_id"],
        "task_type":      row["task_type"],
        "prompt_version": row["prompt_version"],
        "vocab_version":  row["vocab_version"],
        "input_hash":     row["input_hash"],
        "attempts":       row["attempts"] + 1,   # post-update value
    }


def mark_done(task_id: int, output: dict) -> None:
    """Commit task as complete. Must be called BEFORE the canonical JSON write
    so that a mid-write crash doesn't cause the worker to repeat the API call."""
    with _conn() as c:
        c.execute("""
            UPDATE ai_tasks
            SET status = 'done', output_json = ?, last_error = NULL, updated_at = ?
            WHERE id = ?
        """, (json.dumps(output, ensure_ascii=False, default=str), _now(), task_id))
        c.commit()


def mark_failed(task_id: int, error: str) -> None:
    """Terminal failure — task will not retry."""
    with _conn() as c:
        c.execute("""
            UPDATE ai_tasks
            SET status = 'failed', last_error = ?, updated_at = ?
            WHERE id = ?
        """, (error[:500], _now(), task_id))
        c.commit()


def mark_pending(task_id: int, error: Optional[str] = None) -> None:
    """Return task to pending (retryable failure)."""
    with _conn() as c:
        c.execute("""
            UPDATE ai_tasks
            SET status = 'pending',
                last_error = ?,
                claimed_at = NULL,
                updated_at = ?
            WHERE id = ?
        """, (error[:500] if error else None, _now(), task_id))
        c.commit()


def reset_stale(older_than_minutes: int = 10) -> int:
    """Reset 'running' tasks whose claimed_at is older than the cutoff to 'pending'.
    Returns the number of tasks reset.

    Date math is done in Python (not via SQLite's strftime) so behavior is
    consistent across SQLite versions regardless of how they parse ISO-8601
    strings with microseconds + timezone offsets.
    """
    cutoff = datetime.now(timezone.utc).timestamp() - older_than_minutes * 60

    with _conn() as c:
        rows = c.execute(
            "SELECT id, claimed_at FROM ai_tasks WHERE status = 'running'"
        ).fetchall()

        stale_ids: list = []
        for row in rows:
            claimed = row["claimed_at"]
            if not claimed:
                stale_ids.append(row["id"])
                continue
            try:
                claimed_ts = datetime.fromisoformat(claimed).timestamp()
            except (TypeError, ValueError):
                stale_ids.append(row["id"])
                continue
            if claimed_ts < cutoff:
                stale_ids.append(row["id"])

        if not stale_ids:
            return 0

        placeholders = ",".join("?" * len(stale_ids))
        c.execute(
            f"UPDATE ai_tasks SET status = 'pending', updated_at = ? "
            f"WHERE id IN ({placeholders})",
            [_now(), *stale_ids],
        )
        c.commit()
        return len(stale_ids)


# ---------------------------------------------------------------------------
# Status / observability
# ---------------------------------------------------------------------------

def counts_by_status() -> dict:
    """Returns {(task_type, status): count}."""
    with _conn() as c:
        rows = c.execute("""
            SELECT task_type, status, COUNT(*) AS cnt
            FROM ai_tasks
            GROUP BY task_type, status
            ORDER BY task_type, status
        """).fetchall()
    return {(r["task_type"], r["status"]): r["cnt"] for r in rows}


def list_failed(limit: int = 20) -> list:
    with _conn() as c:
        rows = c.execute("""
            SELECT id, building_id, task_type, attempts, last_error, updated_at
            FROM ai_tasks
            WHERE status = 'failed'
            ORDER BY updated_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def all_done_for_reconcile(task_type: str) -> list:
    """All done tasks of a given type with their output_json — used to re-sync
    the canonical JSON file if it was lost between mark_done and the append."""
    with _conn() as c:
        rows = c.execute("""
            SELECT building_id, prompt_version, vocab_version, output_json
            FROM ai_tasks
            WHERE task_type = ? AND status = 'done'
        """, (task_type,)).fetchall()
    return [dict(r) for r in rows]


def delete_for_reprocess(building_id: str, task_type: str) -> int:
    """Delete all task rows for a (building, task_type) pair, regardless of
    status. Bypasses the UNIQUE constraint so a fresh pending row can be
    inserted — called by `reprocess.py` when the user has explicitly chosen
    to re-process specific buildings (e.g., atmosphere-drift migration).

    Returns the number of rows deleted.
    """
    with _conn() as c:
        cur = c.execute(
            "DELETE FROM ai_tasks WHERE building_id = ? AND task_type = ?",
            (building_id, task_type),
        )
        c.commit()
        return cur.rowcount
