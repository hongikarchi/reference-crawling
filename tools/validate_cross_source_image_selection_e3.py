#!/usr/bin/env python
"""Independently validate one terminal sample/full E3 selection artifact."""

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

from canonical.cross_source_image_selection_validator import (
    VALIDATOR_VERSION,
    SelectionValidationReport,
    validate_e3_artifact,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Independently validate a terminal sample or full candidate-only "
            "E3 selection artifact and its immutable E2 input without network "
            "or DB writes. Full mode uses bounded merge-stream recomputation."
        )
    )
    parser.add_argument("artifact", type=Path, help="terminal E3 SQLite artifact")
    parser.add_argument(
        "--expected-logical-sha256",
        help="also require this independently supplied logical SHA-256",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit deterministic machine-readable JSON",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="emit compact JSON (requires --json)",
    )
    return parser


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        converted = [_json_safe(item) for item in value]
        return sorted(
            converted,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _payload(report: SelectionValidationReport) -> dict[str, Any]:
    payload = _json_safe(asdict(report))
    payload["passed"] = report.passed
    payload["failed_check_names"] = list(report.failed_check_names)
    payload["validator_version"] = VALIDATOR_VERSION
    return payload


def _print_json(payload: dict[str, Any], *, compact: bool, stream: Any) -> None:
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":") if compact else None,
            indent=None if compact else 2,
        ),
        file=stream,
    )


def _print_human(report: SelectionValidationReport) -> None:
    print("E3 VALIDATION: " + ("PASS" if report.passed else "FAIL"))
    print(f"artifact: {report.artifact_path}")
    print(f"run_id: {report.run_id or '-'}")
    print(f"logical_sha256: {report.logical_sha256 or '-'}")
    print(
        f"checks: {sum(check.passed for check in report.checks)}/"
        f"{len(report.checks)} passed"
    )
    if report.failed_check_names:
        print("failed_checks:")
        for name in report.failed_check_names:
            print(f"  - {name}")
    print("inputs:")
    if not report.input_files:
        print("  - none recorded or input validation was not reached")
    for item in report.input_files:
        state = "PASS" if item.passed else "FAIL"
        size = str(item.size_bytes) if item.size_bytes is not None else "unknown"
        digest = item.sha256 or "not computed"
        print(f"  - [{state}] {item.input_name}: {item.path}")
        print(f"    size={size} sha256={digest}")
        if item.sidecars:
            print("    sidecars=" + ", ".join(item.sidecars))
        if item.error:
            print(f"    error={item.error}")
    print("check_results:")
    for check in report.checks:
        state = "PASS" if check.passed else "FAIL"
        print(f"  - [{state}] {check.name}")
        if not check.passed:
            print(
                "    expected="
                + json.dumps(_json_safe(check.expected), ensure_ascii=False, sort_keys=True)
            )
            print(
                "    actual="
                + json.dumps(_json_safe(check.actual), ensure_ascii=False, sort_keys=True)
            )


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.compact and not args.json:
        parser.error("--compact requires --json")
    try:
        report = validate_e3_artifact(
            args.artifact,
            expected_logical_sha256=args.expected_logical_sha256,
            verify_input_file_hashes=True,
        )
    except Exception as exc:
        error = {
            "error": str(exc),
            "error_type": type(exc).__name__,
            "passed": False,
            "validator_version": VALIDATOR_VERSION,
        }
        if args.json:
            _print_json(error, compact=args.compact, stream=sys.stderr)
        else:
            print(
                f"E3 VALIDATION ERROR: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
        return 2
    if args.json:
        _print_json(_payload(report), compact=args.compact, stream=sys.stdout)
    else:
        _print_human(report)
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
