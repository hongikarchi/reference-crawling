#!/usr/bin/env python3
"""Finalize the immutable Divisare Vision N100 human gold manifest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from canonical.divisare_vision_gold_finalize import finalize_gold_files  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-manifest", type=Path, required=True)
    parser.add_argument("--reviewed-pool", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    payload = finalize_gold_files(
        candidate_manifest_path=args.candidate_manifest,
        reviewed_pool_path=args.reviewed_pool,
        output_path=args.output,
    )
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "sample_count": len(payload["samples"]),
                "logical_sha256": payload["logical_sha256"],
                "gold_manifest_sha256": payload["gold_manifest_sha256"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
