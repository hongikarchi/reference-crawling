#!/usr/bin/env python3
"""Probe fresh Divisare Vision-axis holdout candidates in memory."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from canonical.divisare_vision_axes_holdout_probe import run_holdout_probe  # noqa: E402
from canonical.divisare_vision_probe import ProbeConfig  # noqa: E402


DEFAULT_CANDIDATES = (
    ROOT / "data" / "review" / "divisare_vision_axes_holdout_candidates_n100_v1.json"
)
DEFAULT_PRIOR = (
    ROOT / "data" / "review" / "divisare_vision_gold_candidates_v1_2_probed.json"
)
DEFAULT_OUTPUT = (
    ROOT / "data" / "review" / "divisare_vision_axes_holdout_candidates_n100_v1_probed.json"
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch fresh holdout candidates at max2048, compute transient "
            "512px identity hashes, and compare them with the prior N560 pool."
        )
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--prior-probed-manifest", type=Path, default=DEFAULT_PRIOR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--staging", type=Path)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--max-bytes", type=int, default=10 * 1024 * 1024)
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    parser.add_argument("--read-timeout", type=float, default=30.0)
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--stop-after", type=int)
    args = parser.parse_args(argv)

    result = run_holdout_probe(
        candidate_manifest_path=args.manifest,
        prior_probed_manifest_path=args.prior_probed_manifest,
        output_path=args.output,
        staging_path=args.staging,
        config=ProbeConfig(
            workers=args.workers,
            max_bytes=args.max_bytes,
            connect_timeout=args.connect_timeout,
            read_timeout=args.read_timeout,
            max_attempts=args.max_attempts,
        ),
        resume=args.resume,
        stop_after=args.stop_after,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
