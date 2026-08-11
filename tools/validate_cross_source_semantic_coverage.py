#!/usr/bin/env python
"""Independently validate an offline semantic-coverage JSON manifest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from canonical.cross_source_semantic_coverage import (  # noqa: E402
    DEFAULT_SAMPLE_SEED,
)
from canonical.cross_source_semantic_coverage_sources import (  # noqa: E402
    ArtifactSpec,
    DEFAULT_E2_APPLICATION_ID,
    DEFAULT_E2_RUN_ID,
    DEFAULT_E3_APPLICATION_ID,
    DEFAULT_E3_RUN_ID,
    DEFAULT_USER_VERSION,
)
from canonical.cross_source_semantic_coverage_validator import (  # noqa: E402
    validate_semantic_coverage_manifest,
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
            "Rehash immutable E2/E3 inputs, independently replay the fixed "
            "semantic-coverage N10, and validate its canonical JSON manifest."
        )
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--e2", type=Path, required=True)
    parser.add_argument("--e3", type=Path, required=True)
    parser.add_argument(
        "--expected-e2-size", type=_positive_int, required=True
    )
    parser.add_argument(
        "--expected-e2-sha256", type=_sha256, required=True
    )
    parser.add_argument(
        "--expected-e2-logical-sha256", type=_sha256, required=True
    )
    parser.add_argument("--expected-e2-run-id", default=DEFAULT_E2_RUN_ID)
    parser.add_argument(
        "--expected-e2-application-id",
        type=int,
        default=DEFAULT_E2_APPLICATION_ID,
    )
    parser.add_argument(
        "--expected-e3-size", type=_positive_int, required=True
    )
    parser.add_argument(
        "--expected-e3-sha256", type=_sha256, required=True
    )
    parser.add_argument(
        "--expected-e3-logical-sha256", type=_sha256, required=True
    )
    parser.add_argument("--expected-e3-run-id", default=DEFAULT_E3_RUN_ID)
    parser.add_argument(
        "--expected-e3-application-id",
        type=int,
        default=DEFAULT_E3_APPLICATION_ID,
    )
    parser.add_argument(
        "--expected-user-version", type=int, default=DEFAULT_USER_VERSION
    )
    parser.add_argument(
        "--expected-sample-size", type=_positive_int, default=10
    )
    parser.add_argument(
        "--expected-sample-seed", default=DEFAULT_SAMPLE_SEED
    )
    parser.add_argument(
        "--expected-max-images-per-building", type=_positive_int, default=6
    )
    parser.add_argument("--batch-size", type=_positive_int, default=1_000)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    e2_spec = ArtifactSpec(
        name="e2_evidence",
        path=args.e2,
        expected_size=args.expected_e2_size,
        expected_sha256=args.expected_e2_sha256,
        expected_logical_sha256=args.expected_e2_logical_sha256,
        expected_run_id=args.expected_e2_run_id,
        expected_application_id=args.expected_e2_application_id,
        expected_user_version=args.expected_user_version,
    )
    e3_spec = ArtifactSpec(
        name="e3_selection",
        path=args.e3,
        expected_size=args.expected_e3_size,
        expected_sha256=args.expected_e3_sha256,
        expected_logical_sha256=args.expected_e3_logical_sha256,
        expected_run_id=args.expected_e3_run_id,
        expected_application_id=args.expected_e3_application_id,
        expected_user_version=args.expected_user_version,
    )
    try:
        result = validate_semantic_coverage_manifest(
            args.manifest,
            e2_spec=e2_spec,
            e3_spec=e3_spec,
            expected_sample_size=args.expected_sample_size,
            expected_sample_seed=args.expected_sample_seed,
            expected_max_images_per_building=(
                args.expected_max_images_per_building
            ),
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
