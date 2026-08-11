#!/usr/bin/env python
"""Create a no-clobber offline E3 P2-evidence diagnostic sample manifest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from canonical.cross_source_image_selection_diagnostic import (  # noqa: E402
    DEFAULT_DIAGNOSTIC_SEED,
    build_diagnostic_sample_plan,
    write_diagnostic_manifest,
)
from canonical.cross_source_image_selection_pipeline import (  # noqa: E402
    DEFAULT_BATCH_SIZE,
    DEFAULT_E2_BUILDER_VERSION,
    DEFAULT_E2_CONTRACT_VERSION,
    DEFAULT_E2_LOGICAL_SHA256,
    DEFAULT_E2_SHA256,
    DEFAULT_E2_SIZE,
    default_e2_path,
)
from canonical.cross_source_image_selection_sources import (  # noqa: E402
    E2ArtifactSpec,
    open_e2_selection_sources,
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
            "Plan a deterministic E3 diagnostic sample that intentionally "
            "contains real within-building P2 duplicate evidence. The output "
            "is a small JSON selection manifest, not a final representative "
            "decision or Vision queue."
        )
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="new JSON manifest path; existing files are never overwritten",
    )
    parser.add_argument(
        "--sample-size",
        type=_positive_int,
        required=True,
        metavar="N",
        help="diagnostic selection count (N10 or N100)",
    )
    parser.add_argument(
        "--diagnostic-seed",
        default=DEFAULT_DIAGNOSTIC_SEED,
        help="fixed deterministic diagnostic seed",
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
    parser.add_argument("--e2", type=Path, help="accepted E2 SQLite input")
    parser.add_argument(
        "--expected-e2-size",
        type=_positive_int,
        default=DEFAULT_E2_SIZE,
    )
    parser.add_argument(
        "--expected-e2-sha256", type=_sha256, default=DEFAULT_E2_SHA256
    )
    parser.add_argument(
        "--expected-e2-logical-sha256",
        type=_sha256,
        default=DEFAULT_E2_LOGICAL_SHA256,
    )
    parser.add_argument(
        "--expected-e2-contract-version",
        default=DEFAULT_E2_CONTRACT_VERSION,
    )
    parser.add_argument(
        "--expected-e2-builder-version",
        default=DEFAULT_E2_BUILDER_VERSION,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    e2_path = (args.e2 or default_e2_path(args.repo_root)).resolve()
    spec = E2ArtifactSpec(
        path=e2_path,
        expected_size=args.expected_e2_size,
        expected_sha256=args.expected_e2_sha256,
        expected_logical_sha256=args.expected_e2_logical_sha256,
        expected_contract_version=args.expected_e2_contract_version,
        expected_builder_version=args.expected_e2_builder_version,
    )
    try:
        with open_e2_selection_sources(spec, batch_size=args.batch_size) as source:
            plan = build_diagnostic_sample_plan(
                source,
                sample_size=args.sample_size,
                seed=args.diagnostic_seed,
            )
            output = write_diagnostic_manifest(
                args.output,
                plan,
                e2_path=e2_path,
                e2_size_bytes=source.lineage.artifact_size,
                e2_byte_sha256=source.lineage.artifact_sha256,
                e2_logical_sha256=source.lineage.stored_logical_sha256,
            )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "llm_requests": 0,
                    "network_requests": 0,
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
                "diagnostic_population": len(plan.inventory),
                "inventory_manifest_sha256": plan.inventory_manifest_sha256,
                "llm_requests": 0,
                "network_requests": 0,
                "ordered_selection_manifest_sha256": (
                    plan.ordered_selection_manifest_sha256
                ),
                "output_path": str(output),
                "sample_size": len(plan.selected),
                "selection_mode": "diagnostic_sample",
                "status": "complete",
                "vision_requests": 0,
            },
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
