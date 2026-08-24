#!/usr/bin/env python
"""Build or review the network-zero WP1.1 drift overlay."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from canonical.vnext_gallery_drift_overlay import (  # noqa: E402
    DEFAULT_BASE,
    DEFAULT_CONTRACT,
    DEFAULT_OUTPUT,
    apply_blind_review,
    build_overlay,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WP1.1 N10 immutable v1 drift-overlay runner (network 0).")
    parser.add_argument("--base", type=Path, default=DEFAULT_BASE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--apply-review", type=Path)
    args = parser.parse_args(argv)
    if args.apply_review:
        result = apply_blind_review(repo_root=ROOT, artifact_path=args.output, review_path=args.apply_review)
        print(json.dumps({"action": "apply_wp11_blind_review", **result}, ensure_ascii=False, sort_keys=True))
        return 0 if result["passed"] else 1
    result = build_overlay(repo_root=ROOT, base_path=args.base, output_path=args.output, contract_path=args.contract)
    print(
        json.dumps(
            {
                "action": "build_wp11_drift_overlay", "status": result.status,
                "overlay_run_id": result.run_id, "artifact_path": str(result.artifact_path),
                "base_groups": result.base_group_count, "effective_groups": result.effective_group_count,
                "exact_match": result.exact_match_count, "pixel_unverified": result.pixel_unverified_count,
                "admissible_pairs": result.admissible_pair_count, "split_pairs": result.split_pair_count,
                "review_page_path": str(result.review_page_path), "review_form_path": str(result.review_form_path),
                "http_requests": 0, "vision_requests": 0, "llm_requests": 0,
                "neon_writes": 0, "r2_writes": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
