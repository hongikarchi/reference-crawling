#!/usr/bin/env python3
"""Recover Divisare project architect refs lost by empty deep-fetch overwrites.

Divisare project slugs are designer-prefixed:
  /projects/<id>-<designer-slug>-<project-slug>

For rows where architect_ids/architect_names are empty, this script walks the
slug prefix against divisare_architects.slug and updates only deterministic
prefix matches. It does not use LLMs or fuzzy matching.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data/crawl/divisare.db"
DEFAULT_REPORT = ROOT / "data/reports/divisare_architect_ref_backfill.json"


EMPTY_ARCH_IDS_SQL = "architect_ids IS NULL OR architect_ids = '' OR architect_ids = '[]'"


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _backup_db(db_path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = ROOT / "data/backups" / f"divisare_architect_ref_backfill_{stamp}"
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


def _load_architects(conn: sqlite3.Connection) -> tuple[dict[str, dict[str, Any]], list[str]]:
    by_slug: dict[str, dict[str, Any]] = {}
    for row in conn.execute("SELECT id, slug, name FROM divisare_architects"):
        slug = str(row["slug"] or "").strip()
        if not slug:
            continue
        by_slug[slug] = {
            "id": int(row["id"]),
            "slug": slug,
            "name": str(row["name"] or "").strip(),
        }
    return by_slug, sorted(by_slug, key=len, reverse=True)


def _prefix_hits(project_slug: str, by_slug: dict[str, dict[str, Any]], slugs: list[str]) -> list[dict[str, Any]]:
    rest = str(project_slug or "").strip()
    hits: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for _ in range(12):
        hit_slug = None
        for slug in slugs:
            if rest == slug or rest.startswith(slug + "-"):
                hit_slug = slug
                break
        if not hit_slug:
            break
        arch = by_slug[hit_slug]
        if arch["id"] not in seen_ids:
            seen_ids.add(arch["id"])
            hits.append(arch)
        rest = rest[len(hit_slug):].lstrip("-")
    return hits


def _candidate_rows(conn: sqlite3.Connection, limit: int | None) -> list[sqlite3.Row]:
    sql = (
        "SELECT id, slug, name, architect_ids, architect_names "
        f"FROM divisare_projects WHERE {EMPTY_ARCH_IDS_SQL} ORDER BY id"
    )
    params: list[Any] = []
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def run(db_path: Path, report_path: Path, apply: bool, limit: int | None) -> dict[str, Any]:
    conn = _connect(db_path)
    try:
        by_slug, slugs = _load_architects(conn)
        rows = _candidate_rows(conn, limit)
        recovered: list[dict[str, Any]] = []
        unresolved: list[dict[str, Any]] = []

        for row in rows:
            hits = _prefix_hits(str(row["slug"]), by_slug, slugs)
            if hits:
                recovered.append(
                    {
                        "project_id": int(row["id"]),
                        "project_name": row["name"],
                        "project_slug": row["slug"],
                        "architect_ids": [h["id"] for h in hits],
                        "architect_names": [h["name"] for h in hits],
                        "architect_slugs": [h["slug"] for h in hits],
                    }
                )
            else:
                unresolved.append(
                    {
                        "project_id": int(row["id"]),
                        "project_name": row["name"],
                        "project_slug": row["slug"],
                    }
                )

        backup_path = None
        if apply and recovered:
            backup_path = _backup_db(db_path)
            for item in recovered:
                conn.execute(
                    "UPDATE divisare_projects "
                    "SET architect_ids=?, architect_names=? "
                    f"WHERE id=? AND ({EMPTY_ARCH_IDS_SQL})",
                    (
                        json.dumps(item["architect_ids"], ensure_ascii=False),
                        json.dumps(item["architect_names"], ensure_ascii=False),
                        item["project_id"],
                    ),
                )
            conn.commit()

        report = {
            "db_path": str(db_path),
            "applied": apply,
            "backup_path": str(backup_path) if backup_path else None,
            "total_candidates": len(rows),
            "recovered": len(recovered),
            "unresolved": len(unresolved),
            "recovered_examples": recovered[:20],
            "unresolved_examples": unresolved[:20],
        }
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    report = run(Path(args.db), Path(args.report), args.apply, args.limit)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
