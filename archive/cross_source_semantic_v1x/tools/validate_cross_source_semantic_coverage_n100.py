#!/usr/bin/env python3
"""Independently replay and validate a fixed semantic-coverage N100 manifest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from canonical.cross_source_semantic_coverage_n100_validator import (  # noqa: E402
    validate_semantic_coverage_n100_manifest,
)
from tools.plan_cross_source_semantic_coverage_n100 import (  # noqa: E402
    _specs,
    build_parser as _planner_parser,
)


def build_parser() -> argparse.ArgumentParser:
    parser = _planner_parser()
    parser.description = "Independently replay a canonical semantic-coverage N100 manifest."
    for action in parser._actions:
        if action.dest == "output":
            action.required = False
            break
    parser.add_argument("--manifest", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        e2, e3, n10 = _specs(args)
        result = validate_semantic_coverage_n100_manifest(
            args.manifest,
            e2_spec=e2,
            e3_spec=e3,
            n10_spec=n10,
            expected_seed=args.sample_seed,
        )
    except Exception as exc:
        print(json.dumps({
            "error": str(exc), "error_type": type(exc).__name__,
            "llm_requests": 0, "network_requests": 0,
            "passed": False, "vision_requests": 0,
        }, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    return 0 if result.passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
