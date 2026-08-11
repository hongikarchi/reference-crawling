#!/usr/bin/env python
"""Create a no-clobber failure-only child run from a terminal E1 sidecar."""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from canonical.image_fingerprint_recovery import (
    ALL_NON404_PLUS_404_SAMPLE,
    DEFAULT_HTTP_404_SAMPLE_SIZE,
    DEFAULT_RECOVERY_SEED,
    PER_ERROR_N10,
    recovery_result_json,
    run_failure_recovery,
)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _workers(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 8:
        raise argparse.ArgumentTypeError("workers must be between 1 and 8")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _nonnegative_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, choices=("divisare", "architizer"))
    parser.add_argument("--source-db", required=True, type=Path)
    parser.add_argument("--base-sidecar", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument(
        "--strategy",
        required=True,
        choices=(PER_ERROR_N10, ALL_NON404_PLUS_404_SAMPLE),
    )
    parser.add_argument("--recovery-seed", default=DEFAULT_RECOVERY_SEED)
    parser.add_argument(
        "--http-404-sample-size",
        type=_positive_int,
        default=DEFAULT_HTTP_404_SAMPLE_SIZE,
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-response-bytes", type=_positive_int, default=25 * 1024 * 1024)
    parser.add_argument("--max-attempts", type=_positive_int, default=3)
    parser.add_argument("--connect-timeout", type=_positive_float, default=10.0)
    parser.add_argument("--read-timeout", type=_positive_float, default=30.0)
    parser.add_argument("--workers", type=_workers, default=4)
    parser.add_argument("--requests-per-second", type=_positive_float, default=2.0)
    parser.add_argument("--circuit-breaker-threshold", type=_positive_int, default=8)
    parser.add_argument("--cooldown-seconds", type=_nonnegative_float, default=30.0)
    parser.add_argument("--batch-size", type=_positive_int, default=128)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_failure_recovery(
        source=args.source,
        source_db=args.source_db,
        base_sidecar=args.base_sidecar,
        output=args.output,
        manifest=args.manifest,
        strategy=args.strategy,
        resume=args.resume,
        recovery_seed=args.recovery_seed,
        http_404_sample_size=args.http_404_sample_size,
        max_response_bytes=args.max_response_bytes,
        max_attempts=args.max_attempts,
        connect_timeout=args.connect_timeout,
        read_timeout=args.read_timeout,
        workers=args.workers,
        requests_per_second=args.requests_per_second,
        circuit_breaker_threshold=args.circuit_breaker_threshold,
        cooldown_seconds=args.cooldown_seconds,
        batch_size=args.batch_size,
    )
    print(recovery_result_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
