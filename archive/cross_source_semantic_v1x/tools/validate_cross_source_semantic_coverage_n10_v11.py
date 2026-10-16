#!/usr/bin/env python3
"""Independently replay a fixed semantic-coverage N10 v1.1 manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from canonical.cross_source_semantic_coverage_n10_v11_validator import (  # noqa: E402
    DEFAULT_SAMPLE_SEED,
    default_prior_specs,
    validate_manifest,
)
from canonical.cross_source_semantic_coverage_sources import default_specs  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay N10 v1.1 offline without importing the production planner."
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--expected-seed", default=DEFAULT_SAMPLE_SEED)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.repo_root.resolve()
    try:
        e2, e3 = default_specs(root)
        result = validate_manifest(
            args.manifest,
            e2_spec=e2,
            e3_spec=e3,
            prior_specs=default_prior_specs(root),
            expected_seed=args.expected_seed,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "error": str(exc), "error_type": type(exc).__name__,
                    "llm_requests": 0, "network_requests": 0,
                    "passed": False, "vision_requests": 0,
                }, sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result.as_record(), indent=2, sort_keys=True))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
