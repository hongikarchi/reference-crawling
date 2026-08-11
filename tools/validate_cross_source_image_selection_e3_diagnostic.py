#!/usr/bin/env python
"""Independently replay and validate an E3 diagnostic JSON manifest."""

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
)
from canonical.cross_source_image_selection_diagnostic_validator import (  # noqa: E402
    validate_diagnostic_manifest,
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
            "Replay immutable E2 evidence and independently validate a "
            "canonical E3 P2 diagnostic manifest."
        )
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--expected-sample-size", type=_positive_int, required=True
    )
    parser.add_argument(
        "--expected-diagnostic-seed", default=DEFAULT_DIAGNOSTIC_SEED
    )
    parser.add_argument(
        "--batch-size", type=_positive_int, default=DEFAULT_BATCH_SIZE
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--e2", type=Path)
    parser.add_argument(
        "--expected-e2-size", type=_positive_int, default=DEFAULT_E2_SIZE
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
        result = validate_diagnostic_manifest(
            args.manifest,
            e2_spec=spec,
            expected_sample_size=args.expected_sample_size,
            expected_sample_seed=args.expected_diagnostic_seed,
            batch_size=args.batch_size,
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
            {**result.as_dict(), "status": "pass" if result.passed else "fail"},
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
