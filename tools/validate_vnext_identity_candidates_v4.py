#!/usr/bin/env python
"""Run the independent WP2.4 identity-candidate v4 validator."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from canonical.vnext_identity_candidates_v4_validator import (  # noqa: E402
    DEFAULT_ARTIFACT,
    DEFAULT_MANIFEST,
    DEFAULT_REGRESSION_GATE_REPORT,
    DEFAULT_REPORT,
    DEFAULT_STAGE_A_REPORT,
    validate_identity_candidates_v4,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Independently validate the WP2.4 R1-R5 v4 identity replay."
    )
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--volume-report", type=Path, default=DEFAULT_STAGE_A_REPORT)
    parser.add_argument("--regression-gate", type=Path, default=DEFAULT_REGRESSION_GATE_REPORT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--no-report", action="store_true")
    parser.add_argument(
        "--skip-full-hash",
        action="store_true",
        help="Tests only; final production validation must verify full hashes.",
    )
    args = parser.parse_args(argv)
    result = validate_identity_candidates_v4(
        repo_root=ROOT,
        artifact_path=args.artifact,
        manifest_path=args.manifest,
        stage_a_report_path=args.volume_report,
        regression_gate_path=args.regression_gate,
        report_path=None if args.no_report else args.report,
        verify_full_hashes=not args.skip_full_hash,
    )
    print(
        json.dumps(
            {
                "action": "validate_wp24_identity_candidates_v4",
                "status": "PASS" if result.passed else "FAIL",
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
