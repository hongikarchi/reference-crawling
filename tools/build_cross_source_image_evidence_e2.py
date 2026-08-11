#!/usr/bin/env python
"""Build the offline Divisare--Architizer E2 image-evidence SQLite artifact."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from canonical.cross_source_image_evidence_pipeline import (  # noqa: E402
    DEFAULT_BATCH_SIZE,
    DEFAULT_SAMPLE_SEED,
    BuildConfig,
    build_cross_source_image_evidence,
    default_input_specs,
)


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _sample_size(value: str) -> int:
    parsed = _positive_int(value)
    if parsed < 10:
        raise argparse.ArgumentTypeError("sample size must be at least 10")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build a no-clobber, evidence-only E2 SQLite artifact from the "
            "frozen curated metadata and E1 fingerprint sidecars. This command "
            "performs no network, Vision, representative-image, or merge work."
        )
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="new SQLite output path; an existing file is never overwritten",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--full",
        action="store_true",
        help="process the complete frozen E1 inventory",
    )
    mode.add_argument(
        "--sample-size",
        type=_sample_size,
        metavar="N",
        help="run one deterministic offline smoke sample (N >= 10)",
    )
    parser.add_argument(
        "--sample-seed",
        default=DEFAULT_SAMPLE_SEED,
        help="deterministic sample seed; ignored for --full",
    )
    parser.add_argument(
        "--batch-size",
        type=_positive_int,
        default=DEFAULT_BATCH_SIZE,
        help=f"bounded SQLite batch size (default: {DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="repository root containing data/ (default: current directory)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = BuildConfig(
        output_path=args.output,
        inputs=default_input_specs(args.repo_root),
        sample_size=None if args.full else args.sample_size,
        sample_seed=args.sample_seed,
        batch_size=args.batch_size,
    )
    try:
        result = build_cross_source_image_evidence(config)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "mode": config.mode,
                    "network_requests": 0,
                    "output_path": str(config.output_path.resolve()),
                    "representative_selection": False,
                    "status": "error",
                    "vision_requests": 0,
                },
                ensure_ascii=True,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2

    print(
        json.dumps(
            {
                "elapsed_seconds": result.elapsed_seconds,
                "logical_sha256": result.logical_sha256,
                "metrics": result.metrics,
                "mode": config.mode,
                "network_requests": 0,
                "output_path": str(result.output_path),
                "representative_selection": False,
                "run_id": result.run_id,
                "status": result.status,
                "vision_requests": 0,
            },
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if result.status == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
