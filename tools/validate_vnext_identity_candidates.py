#!/usr/bin/env python
"""Run the independent WP2 identity-candidate validator."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from canonical.vnext_identity_candidates_validator import (  # noqa: E402
    DEFAULT_ARTIFACT,
    DEFAULT_REPORT,
    validate_identity_candidates,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Independently replay and validate the WP2 candidate/gold artifact.")
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--no-report", action="store_true")
    parser.add_argument("--skip-full-hash", action="store_true", help="Tests only; production must not use this.")
    args = parser.parse_args(argv)
    result = validate_identity_candidates(
        repo_root=ROOT,
        artifact_path=args.artifact,
        report_path=None if args.no_report else args.report,
        verify_full_hashes=not args.skip_full_hash,
    )
    print(
        json.dumps(
            {
                "action": "validate_wp2_identity_candidates",
                "status": "PASS" if result.passed else "FAIL",
                "run_id": result.run_id,
                "passed_checks": sum(row.passed for row in result.checks),
                "total_checks": len(result.checks),
                "failed_checks": list(result.failed_check_names),
                "metrics": result.metrics,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
