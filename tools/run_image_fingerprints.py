#!/usr/bin/env python
"""Build a Divisare or Architizer E1 fingerprint sidecar."""

from __future__ import annotations

import argparse
import json
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
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    parser.add_argument("--read-timeout", type=float, default=30.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_image_fingerprint_pipeline(
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
    )
    payload = asdict(result)
    payload["output_path"] = str(result.output_path)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
