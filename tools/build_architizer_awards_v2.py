"""Build an immutable Architizer A+Awards source-corpus SQLite artifact."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from crawl.architizer.awards_store_v2 import (  # noqa: E402
    AwardsBuildError,
    build_awards_database,
)


DEFAULT_SIDECAR = Path("data/enrichment/architizer_source_recrawl_v2.db")
DEFAULT_SNAPSHOTS = Path("data/enrichment/architizer_html_snapshots_v2")
DEFAULT_OUTPUT = Path("data/enrichment/architizer_awards_v2.db")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build immutable awards-v2 SQLite plus a READY-last receipt from "
            "a completed Architizer award census and verified gzip snapshots"
        )
    )
    parser.add_argument("--sidecar-db", type=Path, default=DEFAULT_SIDECAR)
    parser.add_argument("--snapshot-root", type=Path, default=DEFAULT_SNAPSHOTS)
    parser.add_argument(
        "--output-db",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Immutable DB path; receipt is written to <path>.READY.json",
    )
    parser.add_argument("--run-id", type=int)
    parser.add_argument("--award-year", type=int)
    parser.add_argument(
        "--limit",
        type=int,
        help="Deterministic round-robin attribution limit for offline smoke builds",
    )
    args = parser.parse_args(argv)
    try:
        result = build_awards_database(
            sidecar_path=args.sidecar_db,
            snapshot_root=args.snapshot_root,
            output_path=args.output_db,
            run_id=args.run_id,
            award_year=args.award_year,
            limit=args.limit,
        )
    except (AwardsBuildError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
