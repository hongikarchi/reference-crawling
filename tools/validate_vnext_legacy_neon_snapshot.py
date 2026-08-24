#!/usr/bin/env python3
"""Independently validate the canonical WP0b legacy Neon JSONL snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "data" / "canonical" / "legacy_neon_snapshot_2026q3.jsonl"
DEFAULT_EXPECTED_ROWS = 39_478
EXPECTED_KEYS = {"canonical_bld_id", "source_refs"}
MAX_REPORTED_ERRORS = 20


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def validate_snapshot(path: Path, *, expected_rows: int) -> dict[str, object]:
    if expected_rows < 1:
        raise ValueError("expected_rows must be positive")
    digest = hashlib.sha256()
    root_types: Counter[str] = Counter()
    errors: list[dict[str, object]] = []
    error_count = 0
    row_count = 0
    previous_id: str | None = None
    first_id: str | None = None
    last_id: str | None = None

    def record_error(line_number: int, code: str, detail: object) -> None:
        nonlocal error_count
        error_count += 1
        if len(errors) < MAX_REPORTED_ERRORS:
            errors.append({"line": line_number, "code": code, "detail": detail})

    with path.open("rb") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            digest.update(raw_line)
            row_count += 1
            if not raw_line.endswith(b"\n"):
                record_error(line_number, "missing_lf", None)
                body = raw_line
            else:
                body = raw_line[:-1]
            if body.endswith(b"\r"):
                record_error(line_number, "crlf_not_canonical", None)
                body = body[:-1]
            if not body:
                record_error(line_number, "blank_line", None)
                continue
            try:
                text = body.decode("utf-8", errors="strict")
                row = json.loads(text)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                record_error(line_number, "invalid_json_utf8", str(exc))
                continue
            if not isinstance(row, dict):
                record_error(line_number, "row_not_object", type(row).__name__)
                continue
            if set(row) != EXPECTED_KEYS:
                record_error(line_number, "unexpected_keys", sorted(row))
                continue
            try:
                canonical = canonical_json(row)
            except (TypeError, ValueError) as exc:
                record_error(line_number, "noncanonical_json_value", str(exc))
                continue
            if canonical != text:
                record_error(line_number, "noncanonical_encoding", None)

            canonical_bld_id = row.get("canonical_bld_id")
            if not isinstance(canonical_bld_id, str) or not canonical_bld_id:
                record_error(line_number, "invalid_id", canonical_bld_id)
                continue
            if previous_id is not None and canonical_bld_id <= previous_id:
                record_error(
                    line_number,
                    "id_not_strictly_increasing",
                    {"previous": previous_id, "current": canonical_bld_id},
                )
            if first_id is None:
                first_id = canonical_bld_id
            previous_id = canonical_bld_id
            last_id = canonical_bld_id

            source_refs = row.get("source_refs")
            if not isinstance(source_refs, (dict, list)):
                record_error(
                    line_number,
                    "invalid_source_refs_root",
                    type(source_refs).__name__,
                )
            else:
                root_types[type(source_refs).__name__] += 1

    if row_count != expected_rows:
        record_error(
            0,
            "row_count_mismatch",
            {"expected": expected_rows, "actual": row_count},
        )
    return {
        "status": "PASS" if error_count == 0 else "FAIL",
        "input": path.resolve().as_posix(),
        "expected_rows": expected_rows,
        "rows": row_count,
        "first_id": first_id,
        "last_id": last_id,
        "source_refs_root_types": dict(sorted(root_types.items())),
        "bytes": path.stat().st_size,
        "file_sha256": digest.hexdigest(),
        "error_count": error_count,
        "errors": errors,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--expected-rows", type=int, default=DEFAULT_EXPECTED_ROWS)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    report = validate_snapshot(args.input, expected_rows=args.expected_rows)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
