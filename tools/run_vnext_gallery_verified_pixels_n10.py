#!/usr/bin/env python
"""Execute or terminal-resume WP1 Stage B; apply an opaque blind review."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from canonical.vnext_gallery_verified_pixels import (  # noqa: E402
    DEFAULT_ARTIFACT,
    DEFAULT_REVIEW_FORM,
    apply_blind_review,
    run_verified_pixels,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="WP1 N10 HTTP-only verified-pixel/cache/local-feature pass.")
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--apply-review", type=Path)
    args = parser.parse_args(argv)
    if args.apply_review:
        payload = apply_blind_review(repo_root=ROOT, artifact_path=args.artifact, review_path=args.apply_review)
        print(json.dumps({"action": "apply_blind_review", **payload}, ensure_ascii=False, sort_keys=True))
        return 0 if payload["passed"] else 1
    result = run_verified_pixels(repo_root=ROOT, artifact_path=args.artifact, workers=args.workers)
    print(
        json.dumps(
            {
                "action": "wp1_stage_b",
                "status": result.status,
                "run_id": result.run_id,
                "artifact_path": str(result.artifact_path),
                "http_requests": result.http_requests,
                "resume_http_requests": result.resume_http_requests,
                "vision_requests": 0,
                "llm_requests": 0,
                "pixel_status_counts": dict(result.pixel_status_counts),
                "derivative_cache_count": result.derivative_cache_count,
                "local_pair_count": result.local_pair_count,
                "review_sheet_paths": [str(path) for path in result.review_sheet_paths],
                "review_form_path": str(result.review_form_path),
                "elapsed_seconds": round(result.elapsed_seconds, 3),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
