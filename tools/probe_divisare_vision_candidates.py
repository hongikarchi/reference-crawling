#!/usr/bin/env python3
"""Fetch/hash a frozen Divisare Vision candidate manifest without image storage."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from canonical.divisare_vision_probe import (  # noqa: E402
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_BYTES,
    DEFAULT_READ_TIMEOUT,
    ProbeConfig,
    run_candidate_probe,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--staging", type=Path)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--connect-timeout", type=float, default=DEFAULT_CONNECT_TIMEOUT)
    parser.add_argument("--read-timeout", type=float, default=DEFAULT_READ_TIMEOUT)
    parser.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--stop-after",
        type=int,
        help="process only the first N pending candidates and retain staging",
    )
    args = parser.parse_args(argv)

    result = run_candidate_probe(
        manifest_path=args.manifest,
        output_path=args.output,
        staging_path=args.staging,
        config=ProbeConfig(
            workers=args.workers,
            max_bytes=args.max_bytes,
            connect_timeout=args.connect_timeout,
            read_timeout=args.read_timeout,
            max_attempts=args.max_attempts,
        ),
        resume=args.resume,
        stop_after=args.stop_after,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
