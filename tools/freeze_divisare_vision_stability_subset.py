#!/usr/bin/env python3
"""Freeze the pre-result Divisare Vision N50 same-batch stability subset."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from canonical.divisare_vision_stability_subset import (  # noqa: E402
    freeze_stability_subset_file,
)


DEFAULT_GOLD = ROOT / "data" / "review" / "divisare_vision_gold_n100_v1.json"
DEFAULT_OUTPUT = (
    ROOT / "data" / "review" / "divisare_vision_stability_n50_subset_v1.json"
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold-manifest", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    payload = freeze_stability_subset_file(
        gold_manifest_path=args.gold_manifest,
        output_path=args.output,
    )
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "selected_batch_numbers": payload["selection_metrics"][
                    "selected_batch_numbers"
                ],
                "selected_sample_count": len(payload["selected_samples"]),
                "combinations_evaluated": payload["selection_metrics"][
                    "combinations_evaluated"
                ],
                "logical_sha256": payload["logical_sha256"],
                "subset_manifest_sha256": payload["subset_manifest_sha256"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
