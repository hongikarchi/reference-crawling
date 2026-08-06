#!/usr/bin/env python3
"""Run the asset-keyed Divisare image download/pHash smoke ladder."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from canonical.divisare_image_smoke import run_smoke  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-db",
        type=Path,
        default=ROOT / "data" / "curated" / "divisare_metadata_v2_4.db",
    )
    parser.add_argument("--limit", type=int, choices=(10, 100), required=True)
    parser.add_argument("--workers", type=int)
    parser.add_argument("--output-db", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-attempts", type=int, default=2)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workers = args.workers or (2 if args.limit == 10 else 5)
    if not 1 <= workers <= 5:
        raise SystemExit("workers must be between 1 and 5 for images.divisare.com")
    if not 1 <= args.max_attempts <= 4:
        raise SystemExit("max-attempts must be between 1 and 4")
    output = args.output_db or (
        ROOT / "data" / "smoke" / ("divisare_image_smoke_n%d.db" % args.limit)
    )
    report = args.report or (
        ROOT / "data" / "reports" / "smoke" / ("divisare_image_smoke_n%d.md" % args.limit)
    )
    cache = None
    if not args.no_cache:
        cache = args.cache_dir or (
            ROOT / "data" / "smoke" / "image_cache" / ("n%d" % args.limit)
        )
    result = run_smoke(
        source_db=args.source_db,
        output_db=output,
        report_path=report,
        cache_dir=cache,
        limit=args.limit,
        workers=workers,
        resume=args.resume,
        max_attempts=args.max_attempts,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("status", "complete") == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
