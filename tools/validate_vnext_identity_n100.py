#!/usr/bin/env python
"""Run the independent WP2.3 identity-N100 validator."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from canonical.vnext_identity_n100_validator import (  # noqa: E402
    DEFAULT_ARTIFACT,
    DEFAULT_REPORT,
    validate_identity_n100,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Independently validate the WP2.3 identity N100 overlay."
    )
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--phase",
        choices=("sample", "final"),
        default="final",
        help="Validate the sample_frozen boundary or the terminal review artifacts.",
    )
    parser.add_argument("--no-report", action="store_true")
    parser.add_argument(
        "--skip-full-hash",
        action="store_true",
        help="Tests only; production validation must verify full artifact hashes.",
    )
    args = parser.parse_args()
    result = validate_identity_n100(
        repo_root=ROOT,
        artifact_path=args.artifact,
        report_path=None if args.no_report else args.report,
        verify_full_hashes=not args.skip_full_hash,
        phase=args.phase,
    )
    print(
        json.dumps(
            {
                "status": "PASS" if result.passed else "FAIL",
                "phase": result.phase,
                "run_id": result.run_id,
                "passed_checks": sum(row.passed for row in result.checks),
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
