#!/usr/bin/env python
"""Independently validate a frozen cross-source semantic Vision N10 DB."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from canonical.cross_source_semantic_vision_validator import (  # noqa: E402
    validate_semantic_vision_sidecar,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Open a semantic Vision N10 sidecar immutable, rehash its frozen "
            "manifest and E2/E3 inputs, replay all semantic derivations, and "
            "emit independent validation JSON. No network or model is used."
        )
    )
    parser.add_argument("--sidecar", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--e2", type=Path, required=True)
    parser.add_argument("--e3", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = validate_semantic_vision_sidecar(
            args.sidecar,
            manifest_path=args.manifest,
            e2_path=args.e2,
            e3_path=args.e3,
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
                **result.as_dict(),
                "status": "pass" if result.passed else "fail",
            },
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
