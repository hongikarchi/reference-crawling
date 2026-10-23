#!/usr/bin/env python
"""Run the independent vNext WP4 S2 full gallery validator."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from canonical.vnext_gallery_full_validator import (  # noqa: E402
    VALIDATOR_VERSION,
    validate_gallery_full,
)


def _safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(_safe(item) for item in value)
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Independently validate a WP4 S2 full gallery artifact.")
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--verify-input-hashes", action="store_true", help="re-hash E2/E3 (10 GB each)")
    parser.add_argument("--skip-integrity-check", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--manifest", type=Path, help="also check the build manifest (output SHA, logical SHA, row counts, pins)")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.output and args.output.exists():
        parser.error(f"refusing to clobber existing report: {args.output}")
    report = validate_gallery_full(
        args.artifact,
        repo_root=ROOT,
        verify_input_hashes=args.verify_input_hashes,
        integrity_check=not args.skip_integrity_check,
        manifest_path=None if args.manifest is None else args.manifest.resolve(),
    )
    payload = _safe(asdict(report))
    payload.update(
        {
            "passed": report.passed,
            "failed_check_names": list(report.failed_check_names),
            "validator_version": VALIDATOR_VERSION,
        }
    )
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered + "\n")
    if args.json:
        print(rendered)
    else:
        print("WP4 S2 FULL GALLERY VALIDATION: " + ("PASS" if report.passed else "FAIL"))
        print(f"artifact: {report.artifact_path}")
        print(f"run_id: {report.run_id or '-'}  scope: {report.scope or '-'}")
        print(f"logical_sha256: {report.logical_sha256 or '-'}")
        print(f"checks: {sum(row.passed for row in report.checks)}/{len(report.checks)}")
        for row in report.checks:
            print(f"  [{'PASS' if row.passed else 'FAIL'}] {row.name}")
            if not row.passed:
                print("    expected=" + json.dumps(_safe(row.expected), ensure_ascii=False, sort_keys=True)[:2000])
                print("    actual=" + json.dumps(_safe(row.actual), ensure_ascii=False, sort_keys=True)[:2000])
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
