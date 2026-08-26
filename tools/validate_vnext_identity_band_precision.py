#!/usr/bin/env python
"""Run the independent WP2.5 identity band-precision validator."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from canonical.vnext_identity_band_precision_validator import (  # noqa: E402
    DEFAULT_ARTIFACT,
    DEFAULT_ADJUDICATION_LOCKED,
    DEFAULT_FETCH_MANIFEST,
    DEFAULT_FETCH_REPORT,
    DEFAULT_JOB_CARD,
    DEFAULT_MANIFEST,
    DEFAULT_REPORT,
    DEFAULT_SAMPLE_MANIFEST,
    DEFAULT_STAGE_REPORT,
    validate_identity_band_precision,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Independently validate the WP2.5 band-precision overlay."
    )
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--sample-manifest", type=Path, default=DEFAULT_SAMPLE_MANIFEST
    )
    parser.add_argument("--fetch-manifest", type=Path, default=DEFAULT_FETCH_MANIFEST)
    parser.add_argument("--fetch-report", type=Path, default=DEFAULT_FETCH_REPORT)
    parser.add_argument(
        "--locked-adjudication", type=Path, default=DEFAULT_ADJUDICATION_LOCKED
    )
    parser.add_argument("--stage-report", type=Path, default=DEFAULT_STAGE_REPORT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--job-card", type=Path, default=DEFAULT_JOB_CARD)
    parser.add_argument(
        "--phase",
        choices=("sample", "final"),
        default="final",
        help="Validate the sample_frozen boundary or terminal review artifacts.",
    )
    parser.add_argument("--no-report", action="store_true")
    parser.add_argument(
        "--skip-full-hash",
        action="store_true",
        help="Tests only; production validation must verify all full file/tree pins.",
    )
    args = parser.parse_args()
    result = validate_identity_band_precision(
        repo_root=ROOT,
        artifact_path=args.artifact,
        report_path=None if args.no_report else args.report,
        sample_manifest_path=args.sample_manifest,
        fetch_manifest_path=args.fetch_manifest,
        fetch_report_path=args.fetch_report,
        locked_adjudication_path=args.locked_adjudication,
        stage_report_path=args.stage_report,
        manifest_path=args.manifest,
        job_card_path=args.job_card,
        verify_full_hashes=not args.skip_full_hash,
        phase=args.phase,
    )
    print(
        json.dumps(
            {
                "status": "PASS" if result.passed else "FAIL",
                "phase": result.phase,
                "run_id": result.run_id,
                "passed_checks": sum(check.passed for check in result.checks),
                "total_checks": len(result.checks),
                "failed_checks": list(result.failed_check_names),
                "report": None if result.report_path is None else str(result.report_path),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
