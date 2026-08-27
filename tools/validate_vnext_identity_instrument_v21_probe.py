#!/usr/bin/env python3
"""Run the independent WP2.8 identity-instrument v2.1 validator."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from canonical.vnext_identity_instrument_v21_probe_validator import (
    DEFAULT_ARTIFACT,
    DEFAULT_MANIFEST,
    DEFAULT_REPORT,
    DEFAULT_VALIDATION_REPORT,
    validate_identity_instrument_v21_probe,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--validation-report", type=Path, default=DEFAULT_VALIDATION_REPORT)
    parser.add_argument("--phase", choices=("sample", "final"), default="final")
    parser.add_argument("--skip-full-hashes", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = validate_identity_instrument_v21_probe(
        repo_root=args.repo_root, artifact_path=args.artifact,
        report_path=args.report, manifest_path=args.manifest,
        validation_report_path=args.validation_report, phase=args.phase,
        verify_full_hashes=not args.skip_full_hashes,
    )
    passed = sum(check.passed for check in result.checks)
    print(f"WP2.8 independent validation: {'PASS' if result.passed else 'FAIL'} "
          f"({passed}/{len(result.checks)})")
    if result.failed_check_names:
        print("failed checks: " + ", ".join(result.failed_check_names))
    if result.report_path is not None:
        print(f"report: {result.report_path}")
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
