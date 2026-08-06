#!/usr/bin/env python3
"""Build the frozen Divisare orthogonal-axis N50 development candidate set."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from canonical.divisare_vision_axes_devset import (  # noqa: E402
    build_devset_files,
)


DEFAULT_CANDIDATES = (
    ROOT / "data" / "review" / "divisare_vision_gold_candidates_v1_2_probed.json"
)
DEFAULT_REVIEWED_POOL = (
    ROOT / "data" / "review" / "divisare_vision_reviewed_pool_agent_v2.json"
)
DEFAULT_OLD_GOLD = ROOT / "data" / "review" / "divisare_vision_gold_n100_v1.json"
DEFAULT_OLD_N100 = (
    ROOT / "data" / "smoke" / "divisare_vision_resolution_n100_v1.db"
)
DEFAULT_OUTPUT = (
    ROOT
    / "data"
    / "review"
    / "divisare_vision_axes_dev_n50_candidates_v1.json"
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build a deterministic, development-only N50 candidate manifest "
            "for the 1024px Divisare image-axis benchmark."
        )
    )
    parser.add_argument("--candidate-manifest", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--reviewed-pool", type=Path, default=DEFAULT_REVIEWED_POOL)
    parser.add_argument("--old-gold", type=Path, default=DEFAULT_OLD_GOLD)
    parser.add_argument("--old-n100-db", type=Path, default=DEFAULT_OLD_N100)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    payload = build_devset_files(
        candidate_manifest_path=args.candidate_manifest,
        reviewed_pool_path=args.reviewed_pool,
        old_gold_path=args.old_gold,
        old_n100_db_path=args.old_n100_db,
        output_path=args.output,
    )
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "manifest_version": payload["manifest_version"],
                "manifest_sha256": payload["manifest_sha256"],
                "logical_sha256": payload["logical_sha256"],
                "development_only": payload["development_only"],
                "sample_count": payload["selection_metrics"]["sample_count"],
                "subset_counts": payload["selection_metrics"]["subset_counts"],
                "stratum_counts": payload["selection_metrics"]["stratum_counts"],
                "generation_counts": payload["selection_metrics"][
                    "generation_counts"
                ],
                "selected_id_set_sha256": payload["selection_metrics"][
                    "selected_id_set_sha256"
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
