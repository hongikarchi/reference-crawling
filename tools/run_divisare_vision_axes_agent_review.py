#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from canonical.divisare_vision_axes_agent_review import (  # noqa: E402
    ORDER_MODES,
    run_agent_review,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one blinded, resumable Codex-assisted axes reviewer lane."
    )
    parser.add_argument(
        "--candidate-manifest",
        type=Path,
        default=ROOT / "data" / "review" / "divisare_vision_axes_holdout_n50_candidates_v1_1.json",
    )
    parser.add_argument("--staging-dir", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reviewer-id", required=True)
    parser.add_argument("--review-context-id", required=True)
    parser.add_argument("--codex-bin", required=True)
    parser.add_argument("--scratch-root", type=Path, default=Path(r"C:\tmp"))
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--reasoning", default="high")
    parser.add_argument("--service-tier", default="fast")
    parser.add_argument("--order-mode", choices=ORDER_MODES, default="forward")
    parser.add_argument("--timeout-seconds", type=float, default=900)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--stop-after-batches", type=int)
    args = parser.parse_args()
    result = run_agent_review(
        candidate_manifest_path=args.candidate_manifest,
        staging_dir=args.staging_dir,
        checkpoint_path=args.checkpoint,
        output_path=args.output,
        reviewer_id=args.reviewer_id,
        review_context_id=args.review_context_id,
        codex_bin=args.codex_bin,
        scratch_root=args.scratch_root,
        model=args.model,
        reasoning=args.reasoning,
        service_tier=args.service_tier,
        order_mode=args.order_mode,
        timeout_seconds=args.timeout_seconds,
        resume=args.resume,
        stop_after_batches=args.stop_after_batches,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
