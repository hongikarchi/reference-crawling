#!/usr/bin/env python3
"""Run the independent WP2.8-R packet-repair validator."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from canonical.vnext_identity_instrument_v21_probe_repair_validator import (
    DEFAULT_ARTIFACT,
    DEFAULT_COMPAT_SOURCE,
    DEFAULT_CORE_MANIFEST,
    DEFAULT_CORE_REPORT,
    DEFAULT_MANIFEST,
    DEFAULT_PROJECTION_MANIFEST,
    DEFAULT_REPORT,
    DEFAULT_SOURCE_V1,
    DEFAULT_SOURCE_V2,
    DEFAULT_VALIDATION_REPORT,
    validate_identity_instrument_v21_probe_repair,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--source-v1", type=Path, default=DEFAULT_SOURCE_V1)
    parser.add_argument("--source-v2", type=Path, default=DEFAULT_SOURCE_V2)
    parser.add_argument("--projection", type=Path, default=DEFAULT_COMPAT_SOURCE)
    parser.add_argument(
        "--projection-manifest",
        type=Path,
        default=DEFAULT_PROJECTION_MANIFEST,
    )
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--core-report", type=Path, default=DEFAULT_CORE_REPORT)
    parser.add_argument("--core-manifest", type=Path, default=DEFAULT_CORE_MANIFEST)
    parser.add_argument(
        "--validation-report",
        type=Path,
        default=DEFAULT_VALIDATION_REPORT,
    )
    parser.add_argument("--phase", choices=("sample", "final"), default="final")
    parser.add_argument("--skip-full-hashes", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = validate_identity_instrument_v21_probe_repair(
        repo_root=args.repo_root,
        artifact_path=args.artifact,
        source_v1_path=args.source_v1,
        source_v2_path=args.source_v2,
        projection_path=args.projection,
        projection_manifest_path=args.projection_manifest,
        report_path=args.report,
        manifest_path=args.manifest,
        core_report_path=args.core_report,
        core_manifest_path=args.core_manifest,
        validation_report_path=args.validation_report,
        phase=args.phase,
        verify_full_hashes=not args.skip_full_hashes,
    )
    passed = sum(check.passed for check in result.checks)
    print(
        f"WP2.8-R independent validation: {'PASS' if result.passed else 'FAIL'} "
        f"({passed}/{len(result.checks)})"
    )
    if result.failed_check_names:
        print("failed checks: " + ", ".join(result.failed_check_names))
    if result.report_path is not None:
        print(f"report: {result.report_path}")
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
