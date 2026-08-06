#!/usr/bin/env python3
"""Stage blinded 1024px inputs for independent axes review."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from canonical.divisare_vision_axes_review_inputs import (  # noqa: E402
    EXPECTED_MANIFEST_FILENAME,
    SUBSET_LIMITS,
    stage_review_inputs,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage immutable, opaque 1024px Divisare axes-review inputs."
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "data" / "review" / EXPECTED_MANIFEST_FILENAME,
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--subset", choices=tuple(SUBSET_LIMITS), default="all"
    )
    args = parser.parse_args(argv)
    result = stage_review_inputs(
        manifest_path=args.manifest,
        output_dir=args.output_dir,
        subset=args.subset,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
