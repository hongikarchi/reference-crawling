#!/usr/bin/env python3
"""Export the vNext WP0b legacy-ID crosswalk basis from Neon, read-only.

The output contains exactly ``canonical_bld_id`` and ``source_refs`` in a
canonical, ID-sorted JSONL representation. The connection is repeatable-read
and read-only; the transaction is rolled back and no SQL write is issued.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import psycopg2.extras  # noqa: E402
from psycopg2.extensions import ISOLATION_LEVEL_REPEATABLE_READ  # noqa: E402

from tools.canonical_v2_neon_loader import _connect  # noqa: E402


TABLE = "canonical_v2_buildings"
DEFAULT_OUTPUT = ROOT / "data" / "canonical" / "legacy_neon_snapshot_2026q3.jsonl"
DEFAULT_EXPECTED_ROWS = 39_478


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _normalized_row(row: Mapping[str, Any]) -> dict[str, object]:
    canonical_bld_id = row.get("canonical_bld_id")
    source_refs = row.get("source_refs")
    if not isinstance(canonical_bld_id, str) or not canonical_bld_id:
        raise ValueError(f"invalid canonical_bld_id: {canonical_bld_id!r}")
    if not isinstance(source_refs, (dict, list)):
        raise ValueError(
            f"source_refs for {canonical_bld_id} must be object/array, "
            f"got {type(source_refs).__name__}"
        )
    return {
        "canonical_bld_id": canonical_bld_id,
        "source_refs": source_refs,
    }


def export_rows(
    rows: Iterable[Mapping[str, Any]],
    output: Path,
    *,
    expected_rows: int,
) -> dict[str, object]:
    """Write canonical JSONL without clobbering an existing artifact."""

    if expected_rows < 1:
        raise ValueError("expected_rows must be positive")
    output = output.resolve()
    partial = output.with_name(output.name + ".partial")
    if output.exists():
        raise FileExistsError(f"refusing to overwrite immutable output: {output}")
    if partial.exists():
        raise FileExistsError(f"partial output already exists: {partial}")
    output.parent.mkdir(parents=True, exist_ok=True)

    digest = hashlib.sha256()
    root_types: Counter[str] = Counter()
    count = 0
    first_id: str | None = None
    previous_id: str | None = None
    with partial.open("xb") as handle:
        for raw_row in rows:
            row = _normalized_row(raw_row)
            canonical_bld_id = str(row["canonical_bld_id"])
            if previous_id is not None and canonical_bld_id <= previous_id:
                raise ValueError(
                    "rows must be strictly ordered by canonical_bld_id: "
                    f"{previous_id!r} then {canonical_bld_id!r}"
                )
            encoded = (canonical_json(row) + "\n").encode("utf-8")
            handle.write(encoded)
            digest.update(encoded)
            root_types[type(row["source_refs"]).__name__] += 1
            if first_id is None:
                first_id = canonical_bld_id
            previous_id = canonical_bld_id
            count += 1
        handle.flush()
        os.fsync(handle.fileno())

    if count != expected_rows:
        raise ValueError(
            f"row-count mismatch: expected {expected_rows}, exported {count}; "
            f"partial retained at {partial}"
        )
    partial.rename(output)
    return {
        "status": "PASS",
        "output": output.as_posix(),
        "rows": count,
        "first_id": first_id,
        "last_id": previous_id,
        "source_refs_root_types": dict(sorted(root_types.items())),
        "file_sha256": digest.hexdigest(),
        "bytes": output.stat().st_size,
    }


def export_from_neon(
    output: Path,
    *,
    expected_rows: int = DEFAULT_EXPECTED_ROWS,
    batch_size: int = 1_000,
) -> dict[str, object]:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    connection = _connect()
    connection.set_session(
        readonly=True,
        autocommit=False,
        isolation_level=ISOLATION_LEVEL_REPEATABLE_READ,
    )
    try:
        with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute(
                f"""SELECT current_database() AS database_name,
                           current_user AS database_user,
                           current_setting('transaction_read_only') AS transaction_read_only,
                           txid_current_snapshot()::text AS transaction_snapshot,
                           COUNT(*) AS table_rows,
                           COUNT(DISTINCT canonical_bld_id) AS unique_ids
                    FROM {TABLE}"""
            )
            metadata = dict(cursor.fetchone())
        if metadata["transaction_read_only"] != "on":
            raise RuntimeError("database transaction is not read-only")
        if int(metadata["table_rows"]) != expected_rows:
            raise ValueError(
                f"live row-count mismatch: expected {expected_rows}, "
                f"got {metadata['table_rows']}"
            )
        if int(metadata["unique_ids"]) != expected_rows:
            raise ValueError(
                f"live PK uniqueness mismatch: expected {expected_rows}, "
                f"got {metadata['unique_ids']}"
            )

        with connection.cursor(
            name="vnext_legacy_neon_snapshot",
            cursor_factory=psycopg2.extras.RealDictCursor,
        ) as cursor:
            cursor.itersize = batch_size
            cursor.execute(
                f"""SELECT canonical_bld_id, source_refs
                    FROM {TABLE}
                    ORDER BY canonical_bld_id COLLATE "C"
                    """  # nosec B608: fixed table constant
            )
            report = export_rows(cursor, output, expected_rows=expected_rows)
        connection.rollback()
        report["database"] = metadata["database_name"]
        report["database_user"] = metadata["database_user"]
        report["transaction_read_only"] = metadata["transaction_read_only"]
        report["transaction_snapshot"] = metadata["transaction_snapshot"]
        report["table_rows"] = int(metadata["table_rows"])
        report["unique_ids"] = int(metadata["unique_ids"])
        report["sql_writes"] = 0
        return report
    finally:
        if not connection.closed:
            connection.rollback()
            connection.close()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--expected-rows", type=int, default=DEFAULT_EXPECTED_ROWS)
    parser.add_argument("--batch-size", type=int, default=1_000)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    report = export_from_neon(
        args.output,
        expected_rows=args.expected_rows,
        batch_size=args.batch_size,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
