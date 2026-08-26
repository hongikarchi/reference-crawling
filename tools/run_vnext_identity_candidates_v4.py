#!/usr/bin/env python
"""Build the offline WP2.4 identity policy v4 artifact and four-gate report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from canonical.vnext_identity_candidates_v4 import (  # noqa: E402
    DEFAULT_GATE,
    DEFAULT_MANIFEST,
    DEFAULT_OUTPUT,
    DEFAULT_REPORT,
    build_identity_candidates_v4,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the offline WP2.4 N100-diagnosed identity candidate v4 replay."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--gate", type=Path, default=DEFAULT_GATE)
    parser.add_argument(
        "--skip-full-hash",
        action="store_true",
        help="Tests only; production execution must not use this option.",
    )
    args = parser.parse_args(argv)
    result = build_identity_candidates_v4(
        repo_root=ROOT,
        output_path=args.output,
        manifest_path=args.manifest,
        report_path=args.report,
        gate_path=args.gate,
        verify_full_hashes=not args.skip_full_hash,
    )
    print(
        json.dumps(
            {
                "action": "build_wp24_identity_candidates_v4",
                "status": "PASS",
                "run_id": result.run_id,
                "output": str(result.output_path),
                "manifest": str(result.manifest_path),
                "report": str(result.report_path),
                "gate": str(result.gate_path),
                "candidate_count": result.candidate_count,
                "merge_candidate_count": result.merge_candidate_count,
                "auto_merge_eligible_count": result.auto_merge_eligible_count,
                "human_review_queue_count": result.human_review_queue_count,
                "changed_transition_count": result.changed_transition_count,
                "regression_gate_status": result.regression_gate_status,
                "stage_a_logical_sha256": result.logical_sha256,
                "full_logical_sha256": result.full_logical_sha256,
                "elapsed_seconds": round(result.elapsed_seconds, 3),
                "fresh_n10_runs": 0,
                "network_requests": 0,
                "vision_requests": 0,
                "llm_requests": 0,
                "merge_executions": 0,
                "vnx_id_issuances": 0,
                "neon_writes": 0,
                "r2_writes": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
