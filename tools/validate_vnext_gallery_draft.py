#!/usr/bin/env python
"""Run the independent WP1 gallery-draft validator."""

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

from canonical.vnext_gallery_draft_validator import (  # noqa: E402
    VALIDATOR_VERSION,
    validate_gallery_draft,
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
    parser = argparse.ArgumentParser(description="Independently validate WP1 Stage A or final Stage B.")
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--verify-input-hashes", action="store_true")
    parser.add_argument("--require-stage-b", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = validate_gallery_draft(
        args.artifact,
        repo_root=ROOT,
        verify_input_hashes=args.verify_input_hashes,
        require_stage_b=args.require_stage_b,
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
        if args.output.exists():
            parser.error(f"refusing to clobber existing report: {args.output}")
        args.output.write_text(rendered + "\n", encoding="utf-8")
    if args.json:
        print(rendered)
    else:
        print("WP1 GALLERY VALIDATION: " + ("PASS" if report.passed else "FAIL"))
        print(f"artifact: {report.artifact_path}")
        print(f"run_id: {report.run_id or '-'}")
        print(f"logical_sha256: {report.logical_sha256 or '-'}")
        print(f"checks: {sum(row.passed for row in report.checks)}/{len(report.checks)}")
        for row in report.checks:
            print(f"  [{'PASS' if row.passed else 'FAIL'}] {row.name}")
            if not row.passed:
                print("    expected=" + json.dumps(_safe(row.expected), ensure_ascii=False, sort_keys=True))
                print("    actual=" + json.dumps(_safe(row.actual), ensure_ascii=False, sort_keys=True))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
