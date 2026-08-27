#!/usr/bin/env python3
"""Run the independent WP2.7 identity-instrument v2 probe validator."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from canonical.vnext_identity_instrument_v2_probe_validator import (
    DEFAULT_ARTIFACT,
    DEFAULT_MANIFEST,
    DEFAULT_REPORT,
    DEFAULT_VALIDATION_REPORT,
    validate_identity_instrument_v2_probe,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--validation-report", type=Path, default=DEFAULT_VALIDATION_REPORT
    )
    parser.add_argument("--phase", choices=("sample", "final"), default="final")
    parser.add_argument(
        "--skip-full-hashes",
        action="store_true",
        help="Skip expensive source and artifact file hashes; structural checks remain enabled.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    result = validate_identity_instrument_v2_probe(
        repo_root=arguments.repo_root,
        artifact_path=arguments.artifact,
        report_path=arguments.report,
        manifest_path=arguments.manifest,
        validation_report_path=arguments.validation_report,
        phase=arguments.phase,
        verify_full_hashes=not arguments.skip_full_hashes,
    )
    passed = sum(check.passed for check in result.checks)
    print(
        f"WP2.7 independent validation: {'PASS' if result.passed else 'FAIL'} "
        f"({passed}/{len(result.checks)})"
    )
    if result.failed_check_names:
        print("failed checks: " + ", ".join(result.failed_check_names))
    if result.report_path is not None:
        print(f"report: {result.report_path}")
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
