#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from canonical.divisare_vision_axes_agent_adjudication import (  # noqa: E402
    run_agent_adjudication,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Adjudicate two blinded Divisare axes reviews."
    )
    parser.add_argument(
        "--candidate",
        type=Path,
        default=ROOT / "data" / "review" / "divisare_vision_axes_holdout_n50_candidates_v1_1.json",
    )
    parser.add_argument("--reviews", type=Path, nargs=2, required=True)
    parser.add_argument("--staging-dir", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--adjudicator-id", required=True)
    parser.add_argument("--codex-bin", required=True)
    parser.add_argument("--scratch-root", type=Path, default=Path(r"C:\tmp"))
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--reasoning", default="high")
    parser.add_argument("--service-tier", default="fast")
    parser.add_argument("--timeout-seconds", type=float, default=900)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    result = run_agent_adjudication(
        candidate_path=args.candidate,
        review_paths=args.reviews,
        staging_dir=args.staging_dir,
        checkpoint_path=args.checkpoint,
        output_path=args.output,
        adjudicator_id=args.adjudicator_id,
        codex_bin=args.codex_bin,
        scratch_root=args.scratch_root,
        model=args.model,
        reasoning=args.reasoning,
        service_tier=args.service_tier,
        timeout_seconds=args.timeout_seconds,
        resume=args.resume,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
