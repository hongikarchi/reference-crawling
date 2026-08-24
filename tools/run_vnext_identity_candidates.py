#!/usr/bin/env python
"""Build the offline WP2 identity candidate universe and frozen N10 sample."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from canonical.vnext_identity_candidates import (  # noqa: E402
    DEFAULT_FREEZE,
    DEFAULT_MANIFEST,
    DEFAULT_OUTPUT,
    DEFAULT_REPORT,
    IdentityCandidateError,
    build_identity_candidates,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline WP2 candidate union + six-family evaluation + frozen stratified N10."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--freeze", type=Path, default=DEFAULT_FREEZE)
    parser.add_argument("--skip-full-hash", action="store_true", help="Tests only; production must not use this.")
    args = parser.parse_args(argv)
    try:
        result = build_identity_candidates(
            repo_root=ROOT,
            output_path=args.output,
            manifest_path=args.manifest,
            report_path=args.report,
            freeze_path=args.freeze,
            verify_full_hashes=not args.skip_full_hash,
        )
    except Exception as exc:
        failure = {
            "action": "wp2_stage_a",
            "status": "FAIL",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "network_requests": 0,
            "vision_requests": 0,
            "llm_requests": 0,
            "merge_executions": 0,
            "vnx_id_issuances": 0,
        }
        print(json.dumps(failure, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "action": "wp2_stage_a",
                "status": "PASS",
                "run_id": result.run_id,
                "output_path": str(result.output_path),
                "manifest_path": str(result.manifest_path),
                "report_path": str(result.report_path),
                "logical_sha256": result.logical_sha256,
                "source_building_count": result.source_building_count,
                "candidate_count": result.candidate_count,
                "auto_merge_eligible_count": result.auto_merge_eligible_count,
                "sample_count": result.sample_count,
                "network_requests": 0,
                "vision_requests": 0,
                "llm_requests": 0,
                "merge_executions": 0,
                "vnx_id_issuances": 0,
                "elapsed_seconds": round(result.elapsed_seconds, 3),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
