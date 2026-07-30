#!/usr/bin/env python3
"""Read-only inspection of the Architizer recrawl-v2 sidecar."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from crawl.architizer.recrawl_v2 import (  # noqa: E402
    DEFAULT_SNAPSHOT_DIR,
    DEFAULT_STATE_DB,
    open_state_readonly,
    preview_full_recrawl,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect Architizer recrawl sidecar without writing it."
    )
    parser.add_argument("--state-db", type=Path, default=DEFAULT_STATE_DB)
    parser.add_argument(
        "--snapshot-dir", type=Path, default=DEFAULT_SNAPSHOT_DIR
    )
    parser.add_argument("--delay", type=float, default=2.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    connection = open_state_readonly(args.state_db)
    try:
        runs = [
            {
                **dict(row),
                "summary_json": json.loads(row["summary_json"])
                if row["summary_json"]
                else None,
            }
            for row in connection.execute(
                """
                SELECT id,run_kind,started_at,finished_at,status,selected_count,
                       source_db_sha256_before,source_db_sha256_after,
                       summary_json,error
                FROM runs ORDER BY id
                """
            )
        ]
        targets = [
            dict(row)
            for row in connection.execute(
                """
                SELECT entity_type,status,retryable,primary_reason
                FROM targets
                """
            )
        ]
        output = {
            "runs": runs,
            "targets": {
                "count": len(targets),
                "by_entity_type": dict(
                    sorted(Counter(row["entity_type"] for row in targets).items())
                ),
                "by_status": dict(
                    sorted(Counter(row["status"] for row in targets).items())
                ),
                "by_primary_reason": dict(
                    sorted(Counter(row["primary_reason"] for row in targets).items())
                ),
                "retryable": sum(row["retryable"] for row in targets),
            },
            "metadata_versions": connection.execute(
                "SELECT COUNT(*) FROM metadata_versions"
            ).fetchone()[0],
            "current_fields": connection.execute(
                "SELECT COUNT(*) FROM current_fields"
            ).fetchone()[0],
            "snapshots": {
                "sitemap": connection.execute(
                    "SELECT COUNT(*) FROM sitemap_snapshots"
                ).fetchone()[0],
                "page_attempts": connection.execute(
                    """
                    SELECT COUNT(*) FROM http_attempts
                    WHERE target_url IS NOT NULL AND sha256 IS NOT NULL
                    """
                ).fetchone()[0],
            },
            "full_preview": preview_full_recrawl(
                state_path=args.state_db,
                snapshot_root=args.snapshot_dir,
                delay_seconds=args.delay,
            ),
        }
    finally:
        connection.close()
    print(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
