#!/usr/bin/env python3
"""Refetch Divisare projects whose architect fields are empty.

This is intentionally narrow: it only updates rows where architect_ids are
currently empty and the freshly parsed live page returns architect links.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crawl.divisare.crawler import _fetch
from crawl.divisare.parsers import parse_project_page


DEFAULT_DB = ROOT / "data/crawl/divisare.db"
DEFAULT_REPORT = ROOT / "data/reports/divisare_missing_architect_refetch.json"
EMPTY_ARCH_IDS_SQL = "architect_ids IS NULL OR architect_ids = '' OR architect_ids = '[]'"


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _backup_db(db_path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = ROOT / "data/backups" / f"divisare_missing_architect_refetch_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=False)
    backup_path = out_dir / db_path.name
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(backup_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return backup_path


def _candidate_rows(conn: sqlite3.Connection, limit: int | None) -> list[sqlite3.Row]:
    sql = (
        "SELECT id, slug, name FROM divisare_projects "
        f"WHERE {EMPTY_ARCH_IDS_SQL} ORDER BY id"
    )
    params: list[Any] = []
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def run(db_path: Path, report_path: Path, limit: int | None, apply: bool) -> dict[str, Any]:
    conn = _connect(db_path)
    backup_path = None
    try:
        rows = _candidate_rows(conn, limit)
        if apply and rows:
            backup_path = _backup_db(db_path)

        recovered: list[dict[str, Any]] = []
        unresolved: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []

        for row in rows:
            path = f"/projects/{row['id']}-{row['slug']}"
            html = _fetch(path)
            if not html:
                failed.append({"project_id": row["id"], "project_name": row["name"], "reason": "fetch_failed"})
                continue

            data = parse_project_page(html, f"https://divisare.com{path}")
            architect_ids = data.get("architect_ids") or []
            architect_names = data.get("architect_names") or []
            if not architect_ids or not architect_names:
                unresolved.append({"project_id": row["id"], "project_name": row["name"], "project_slug": row["slug"]})
                continue

            item = {
                "project_id": int(row["id"]),
                "project_name": row["name"],
                "project_slug": row["slug"],
                "architect_ids": [int(v) for v in architect_ids],
                "architect_names": [str(v) for v in architect_names],
            }
            recovered.append(item)

            if apply:
                conn.execute(
                    "UPDATE divisare_projects SET architect_ids=?, architect_names=? "
                    f"WHERE id=? AND ({EMPTY_ARCH_IDS_SQL})",
                    (
                        json.dumps(item["architect_ids"], ensure_ascii=False),
                        json.dumps(item["architect_names"], ensure_ascii=False),
                        item["project_id"],
                    ),
                )

        if apply:
            conn.commit()

        report = {
            "db_path": str(db_path),
            "applied": apply,
            "backup_path": str(backup_path) if backup_path else None,
            "total_candidates": len(rows),
            "recovered": len(recovered),
            "unresolved": len(unresolved),
            "failed": len(failed),
            "recovered_examples": recovered[:20],
            "unresolved_examples": unresolved[:20],
            "failed_examples": failed[:20],
        }
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Refetch Divisare rows with empty architect fields.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    report = run(args.db, args.report, args.limit, args.apply)
    print(json.dumps({k: v for k, v in report.items() if not k.endswith("_examples")}, ensure_ascii=False, indent=2))
    print(f"report: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
