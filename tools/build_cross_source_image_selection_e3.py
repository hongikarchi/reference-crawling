#!/usr/bin/env python
"""Build an offline E3 representative-image policy comparison sample."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from canonical.cross_source_image_selection_pipeline import (  # noqa: E402
    DEFAULT_BATCH_SIZE,
    DEFAULT_E2_BUILDER_VERSION,
    DEFAULT_E2_CONTRACT_VERSION,
    DEFAULT_E2_LOGICAL_SHA256,
    DEFAULT_E2_SHA256,
    DEFAULT_E2_SIZE,
    DEFAULT_SAMPLE_SEED,
    DEFAULT_SHORTLIST_SIZE,
    BuildConfig,
    build_cross_source_image_selection,
    default_e2_path,
)


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _sha256(value: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64:
        raise argparse.ArgumentTypeError("value must be a 64-character SHA-256")
    try:
        int(normalized, 16)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be hexadecimal") from exc
    return normalized


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build a no-clobber, candidate-only E3 policy-comparison sample "
            "from the frozen E2 image-evidence artifact. This command makes "
            "no network, Vision, LLM, representative-image, or merge request."
        )
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="new SQLite output path; an existing path is never overwritten",
    )
    parser.add_argument(
        "--sample-size",
        type=_positive_int,
        default=10,
        metavar="N",
        help="deterministic offline sample size (default: 10)",
    )
    parser.add_argument(
        "--sample-seed",
        default=DEFAULT_SAMPLE_SEED,
        help="deterministic sample seed",
    )
    parser.add_argument(
        "--shortlist-size",
        type=_positive_int,
        default=DEFAULT_SHORTLIST_SIZE,
        help=f"policy shortlist cap (default: {DEFAULT_SHORTLIST_SIZE})",
    )
    parser.add_argument(
        "--batch-size",
        type=_positive_int,
        default=DEFAULT_BATCH_SIZE,
        help=f"bounded SQLite read batch size (default: {DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="repository root containing data/ (default: current directory)",
    )
    parser.add_argument(
        "--e2",
        type=Path,
        help="accepted E2 SQLite input (default: frozen path under repo root)",
    )
    parser.add_argument(
        "--expected-e2-size",
        type=_positive_int,
        default=DEFAULT_E2_SIZE,
        help="required E2 byte size",
    )
    parser.add_argument(
        "--expected-e2-sha256",
        type=_sha256,
        default=DEFAULT_E2_SHA256,
        help="required E2 byte SHA-256",
    )
    parser.add_argument(
        "--expected-e2-logical-sha256",
        type=_sha256,
        default=DEFAULT_E2_LOGICAL_SHA256,
        help="required stored E2 logical SHA-256",
    )
    parser.add_argument(
        "--expected-e2-contract-version",
        default=DEFAULT_E2_CONTRACT_VERSION,
        help="required E2 contract version",
    )
    parser.add_argument(
        "--expected-e2-builder-version",
        default=DEFAULT_E2_BUILDER_VERSION,
        help="required E2 builder version",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    e2_path = args.e2 or default_e2_path(args.repo_root)
    config = BuildConfig(
        e2_path=e2_path,
        output_path=args.output,
        expected_e2_size=args.expected_e2_size,
        expected_e2_sha256=args.expected_e2_sha256,
        expected_e2_logical_sha256=args.expected_e2_logical_sha256,
        expected_e2_contract_version=args.expected_e2_contract_version,
        expected_e2_builder_version=args.expected_e2_builder_version,
        sample_size=args.sample_size,
        sample_seed=args.sample_seed,
        shortlist_size=args.shortlist_size,
        batch_size=args.batch_size,
    )
    try:
        result = build_cross_source_image_selection(config)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "authoritative": False,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "llm_requests": 0,
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
                "authoritative": False,
                "elapsed_seconds": result.elapsed_seconds,
                "image_candidates": result.image_candidates,
                "llm_requests": 0,
                "logical_sha256": result.logical_sha256,
                "network_requests": 0,
                "output_path": str(result.output_path),
                "representative_selection": False,
                "run_id": result.run_id,
                "selected_buildings": result.selected_buildings,
                "shortlist_items": result.shortlist_items,
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
