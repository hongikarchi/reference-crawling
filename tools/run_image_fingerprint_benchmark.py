#!/usr/bin/env python3
"""Run the shared image fingerprint calibration against local cached images."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from canonical.image_fingerprint_benchmark import (  # noqa: E402
    DEFAULT_THRESHOLDS,
    BenchmarkError,
    run_benchmark,
    write_json_report,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=ROOT / "data" / "smoke" / "image_cache",
        help="Read-only directory containing cached image responses.",
    )
    parser.add_argument("--sample-size", type=int, default=100)
    parser.add_argument(
        "--thresholds",
        type=int,
        nargs="+",
        default=list(DEFAULT_THRESHOLDS),
        metavar="N",
    )
    parser.add_argument("--minimum-side", type=int, default=64)
    parser.add_argument("--max-file-mib", type=float, default=16.0)
    parser.add_argument("--max-source-megapixels", type=float, default=80.0)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Omit pair-level rows from the JSON result.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.max_file_mib <= 0:
        raise SystemExit("--max-file-mib must be positive")
    if args.max_source_megapixels <= 0:
        raise SystemExit("--max-source-megapixels must be positive")
    try:
        result = run_benchmark(
            args.cache_dir,
            sample_size=args.sample_size,
            thresholds=args.thresholds,
            minimum_side=args.minimum_side,
            max_file_bytes=round(args.max_file_mib * 1024 * 1024),
            max_source_pixels=round(args.max_source_megapixels * 1_000_000),
            include_pairs=not args.summary_only,
        )
    except BenchmarkError as exc:
        raise SystemExit(f"benchmark input error: {exc}") from exc

    if args.output:
        write_json_report(result, args.output)
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
