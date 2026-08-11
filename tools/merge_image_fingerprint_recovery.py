#!/usr/bin/env python
"""Merge failure-recovery E1 sidecars into a new immutable full sidecar."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from canonical.image_fingerprint_merge import (
    MERGE_BATCH_SIZE,
    merge_image_fingerprint_recoveries,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", required=True, type=Path)
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--recovery", required=True, action="append", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="resume an exact-manifest-matching .partial or verify a completed output",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=MERGE_BATCH_SIZE,
        help=f"durable merge checkpoint size (default: {MERGE_BATCH_SIZE})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = merge_image_fingerprint_recoveries(
        source_db=args.source_db,
        base_sidecar=args.base,
        recovery_sidecars=args.recovery,
        output=args.output,
        resume=args.resume,
        batch_size=args.batch_size,
    )
    payload = asdict(result)
    payload["output_path"] = str(result.output_path)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
