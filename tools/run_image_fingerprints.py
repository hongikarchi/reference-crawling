#!/usr/bin/env python
"""Build a Divisare or Architizer E1 fingerprint sidecar."""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from canonical.image_fingerprint_pipeline import run_image_fingerprint_pipeline


def _sample_size(value: str) -> int | None:
    if value == "full":
        return None
    if value in {"10", "100", "1000"}:
        return int(value)
    raise argparse.ArgumentTypeError("N must be 10, 100, 1000, or full")


def _workers(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 8:
        raise argparse.ArgumentTypeError("workers must be between 1 and 8")
    return parsed


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
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
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--n", required=True, type=_sample_size, metavar="10|100|1000|full")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--sample-seed", default="archibe-e1-smoke-v1")
    parser.add_argument("--max-response-bytes", type=int, default=25 * 1024 * 1024)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--connect-timeout", type=_positive_float, default=10.0)
    parser.add_argument("--read-timeout", type=_positive_float, default=30.0)
    parser.add_argument("--workers", type=_workers, default=4)
    parser.add_argument(
        "--requests-per-second",
        type=_positive_float,
        default=2.0,
        help="Site-wide request start rate shared by all workers.",
    )
    parser.add_argument(
        "--circuit-breaker-threshold",
        type=_positive_int,
        default=8,
        help="Stop scheduling after this many consecutive HTTP 429/5xx responses.",
    )
    parser.add_argument(
        "--cooldown-seconds",
        type=_nonnegative_float,
        default=30.0,
        help="Site-wide overload cooldown applied after HTTP 429/5xx responses.",
    )
    parser.add_argument(
        "--batch-size",
        type=_positive_int,
        help="Optional bounded pending-work batch size override.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    pipeline_args = dict(
        source=args.source,
        source_db=args.source_db,
        output=args.output,
        sample_size=args.n,
        resume=args.resume,
        sample_seed=args.sample_seed,
        max_response_bytes=args.max_response_bytes,
        max_attempts=args.max_attempts,
        connect_timeout=args.connect_timeout,
        read_timeout=args.read_timeout,
        workers=args.workers,
        requests_per_second=args.requests_per_second,
        circuit_breaker_threshold=args.circuit_breaker_threshold,
        cooldown_seconds=args.cooldown_seconds,
    )
    if args.batch_size is not None:
        pipeline_args["batch_size"] = args.batch_size
    result = run_image_fingerprint_pipeline(**pipeline_args)
    payload = asdict(result)
    payload["output_path"] = str(result.output_path)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
