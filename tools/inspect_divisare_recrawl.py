"""Read-only progress inspection for a Divisare recrawl state DB."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def status_counts(
    conn: sqlite3.Connection,
    column: str,
) -> Dict[str, int]:
    return {
        str(row[0]): int(row[1])
        for row in conn.execute(
            "SELECT %s,COUNT(*) FROM article_html_jobs GROUP BY %s"
            % (column, column)
        )
    }


def inspect(state_path: Path) -> Dict[str, Any]:
    resolved = state_path.resolve()
    if not resolved.exists():
        raise FileNotFoundError(resolved)
    conn = sqlite3.connect(
        "file:%s?mode=ro" % resolved.as_posix(),
        uri=True,
        timeout=10,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    try:
        fetch = status_counts(conn, "fetch_status")
        parse = status_counts(conn, "parse_status")
        latest = conn.execute(
            """
            SELECT *
            FROM recrawl_runs
            ORDER BY run_id DESC
            LIMIT 1
            """
        ).fetchone()
        now = utc_now()
        run: Dict[str, Any] = dict(latest) if latest is not None else {}
        if run.get("started_at"):
            started = datetime.fromisoformat(run["started_at"])
            ended = (
                datetime.fromisoformat(run["completed_at"])
                if run.get("completed_at")
                else now
            )
            elapsed = max((ended - started).total_seconds(), 0.0)
            processed = int(run.get("processed") or 0)
            seconds_per_item = elapsed / processed if processed else None
            remaining = int(fetch.get("pending", 0))
            if seconds_per_item:
                eta_seconds = remaining * max(seconds_per_item, 3.0)
                run["seconds_per_item"] = round(seconds_per_item, 3)
                run["resume_eta_hours"] = round(eta_seconds / 3600.0, 2)
                if run.get("status") == "running":
                    run["estimated_completion_utc"] = (
                        now + timedelta(seconds=eta_seconds)
                    ).replace(microsecond=0).isoformat()
            run["elapsed_seconds"] = round(elapsed, 1)
        terminal_jobs = [
            dict(row)
            for row in conn.execute(
                """
                SELECT
                    article_id,source_url,fetch_status,parse_status,
                    http_status,final_url,attempt_count,last_error,updated_at
                FROM article_html_jobs
                WHERE fetch_status IN ('failed','blocked','not_found')
                ORDER BY updated_at DESC,article_id
                LIMIT 25
                """
            )
        ]
        run_history = [
            dict(row)
            for row in conn.execute(
                """
                SELECT
                    run_id,started_at,completed_at,status,max_items,
                    delay_seconds,processed,error
                FROM recrawl_runs
                ORDER BY run_id DESC
                LIMIT 10
                """
            )
        ]
        return {
            "state_db": str(resolved),
            "checked_at_utc": now.replace(microsecond=0).isoformat(),
            "fetch_status": fetch,
            "parse_status": parse,
            "current_metadata": int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM article_metadata_versions
                    WHERE is_current=1
                    """
                ).fetchone()[0]
            ),
            "snapshots": int(
                conn.execute(
                    "SELECT COUNT(*) FROM article_html_snapshots"
                ).fetchone()[0]
            ),
            "latest_job_update": conn.execute(
                "SELECT MAX(updated_at) FROM article_html_jobs"
            ).fetchone()[0],
            "latest_run": run,
            "terminal_jobs": terminal_jobs,
            "run_history": run_history,
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-db", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = inspect(args.state_db)
    except Exception as exc:
        print("ERROR: %s" % exc)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
