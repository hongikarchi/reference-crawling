#!/usr/bin/env python3
"""Run a sitemap-only Architizer source census against an immutable input DB."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from crawl.architizer.recrawl_v2 import (  # noqa: E402
    DEFAULT_SNAPSHOT_DIR,
    DEFAULT_SOURCE_DB,
    DEFAULT_STATE_DB,
    OFFICIAL_SITEMAP_URL,
    run_source_census,
    write_json_no_clobber,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch only Architizer's official sitemap index and the project/"
            "firm children explicitly registered there, then compare them with "
            "the immutable legacy crawler DB."
        )
    )
    parser.add_argument("--source-db", type=Path, default=DEFAULT_SOURCE_DB)
    parser.add_argument("--state-db", type=Path, default=DEFAULT_STATE_DB)
    parser.add_argument(
        "--snapshot-dir", type=Path, default=DEFAULT_SNAPSHOT_DIR
    )
    parser.add_argument("--sitemap-url", default=OFFICIAL_SITEMAP_URL)
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--unchanged-sample-size", type=int, default=200)
    parser.add_argument(
        "--manifest",
        type=Path,
        help="Optional new JSON manifest path; existing files are not overwritten.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_source_census(
        source_path=args.source_db,
        state_path=args.state_db,
        snapshot_root=args.snapshot_dir,
        sitemap_url=args.sitemap_url,
        delay_seconds=args.delay,
        timeout_seconds=args.timeout,
        max_attempts=args.max_attempts,
        unchanged_sample_size=args.unchanged_sample_size,
    )
    if args.manifest:
        write_json_no_clobber(args.manifest, result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
