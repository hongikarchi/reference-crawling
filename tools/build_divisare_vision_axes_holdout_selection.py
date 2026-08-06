from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from canonical.divisare_vision_axes_holdout_selection import (  # noqa: E402
    write_selection_manifest,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Freeze a blinded N50 from the fresh Divisare axes holdout."
    )
    parser.add_argument(
        "--probed",
        type=Path,
        default=ROOT / "data" / "review" / "divisare_vision_axes_holdout_candidates_n100_v1_probed.json",
    )
    parser.add_argument(
        "--candidates",
        type=Path,
        default=ROOT / "data" / "review" / "divisare_vision_axes_holdout_candidates_n100_v1.json",
    )
    parser.add_argument(
        "--prior",
        type=Path,
        default=ROOT / "data" / "review" / "divisare_vision_gold_candidates_v1_2_probed.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "data" / "review" / "divisare_vision_axes_holdout_n50_candidates_v1_1.json",
    )
    args = parser.parse_args()
    payload = write_selection_manifest(
        probed_path=args.probed,
        candidate_path=args.candidates,
        prior_path=args.prior,
        output_path=args.output,
    )
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "sample_count": payload["selection_metrics"]["selected_count"],
                "logical_sha256": payload["logical_sha256"],
                "manifest_sha256": payload["manifest_sha256"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
