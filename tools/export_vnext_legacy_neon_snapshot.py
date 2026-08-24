#!/usr/bin/env python3
"""Export the full WP0b legacy Neon baseline, excluding only ``embedding``.

The export is canonical, ID-sorted JSONL. The preferred connection is the
SELECT-only make_web role in ``.env.make-web``; a writer-role ``.env`` fallback
is still forced into a repeatable-read, read-only transaction. No SQL write is
issued and immutable output/report paths are never clobbered.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402
from psycopg2 import sql  # noqa: E402
from psycopg2.extensions import ISOLATION_LEVEL_REPEATABLE_READ  # noqa: E402

from tools.canonical_v2_neon_loader import _connect  # noqa: E402


TABLE = "canonical_v2_buildings"
DEFAULT_OUTPUT = ROOT / "data" / "canonical" / "legacy_neon_snapshot_2026q3.jsonl"
DEFAULT_EXPECTED_ROWS = 39_478

# Known live serving schema. An information_schema exact-set check fails closed
# if Neon drifts, so a new live column cannot be silently omitted.
EXPECTED_DB_COLUMNS = (
    "canonical_bld_id",
    "name",
    "names_alts",
    "location_city",
    "location_country",
    "project_year",
    "architect_canonical_ids",
    "architect_names",
    "architects_text",
    "program",
    "style",
    "color_tone",
    "atmosphere",
    "material_visual",
    "visual_description",
    "image_derived",
    "covers_by_type",
    "all_images",
    "best_image_per_cluster",
    "source_refs",
    "source_urls",
    "identity_source",
    "confidence_tier",
    "n_sources",
    "cover_image_url_default",
    "cover_image_cdn_url",
    "cover_blurhash",
    "display_cover_url",
    "is_publishable",
    "publishability_reasons",
    "needs_image_derived_backfill",
    "typology_primary",
    "typology_primary_source",
    "typology_tags",
    "architectural_elements",
    "source_categories",
    "year_kind",
    "era",
    "scale",
    "structural_system",
    "roof_type",
    "facade_pattern",
    "embedding",
    "created_at",
    "updated_at",
)
EXPECTED_EXPORT_COLUMNS = tuple(
    column for column in EXPECTED_DB_COLUMNS if column != "embedding"
)
EXPECTED_EXPORT_KEYS = frozenset(EXPECTED_EXPORT_COLUMNS)


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def default_report_path(output: Path) -> Path:
    return output.with_name(f"{output.stem}.report.json")


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _connect_prefer_select_only() -> tuple[Any, str]:
    make_web_path = ROOT / ".env.make-web"
    if not make_web_path.is_file():
        return _connect(), ".env writer fallback"

    values = _read_env_file(make_web_path)
    required = (
        "BUILDINGS_DB_HOST",
        "BUILDINGS_DB_NAME",
        "BUILDINGS_DB_USER",
        "BUILDINGS_DB_PASSWORD",
    )
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise SystemExit(
            ".env.make-web exists but is incomplete: " + ", ".join(missing)
        )
    connection = psycopg2.connect(
        host=values["BUILDINGS_DB_HOST"],
        port=int(values.get("BUILDINGS_DB_PORT", "5432")),
        dbname=values["BUILDINGS_DB_NAME"],
        user=values["BUILDINGS_DB_USER"],
        password=values["BUILDINGS_DB_PASSWORD"],
        sslmode=values.get("BUILDINGS_DB_SSLMODE", "require"),
        application_name="vnext_wp0b_readonly_export",
        options="-c default_transaction_read_only=on",
    )
    return connection, ".env.make-web SELECT-only"


def _json_safe(value: Any) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite float is not canonical JSON: {value!r}")
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError(f"naive database timestamp is not allowed: {value!r}")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(f"JSON object key must be a string: {key!r}")
            normalized[key] = _json_safe(child)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_json_safe(child) for child in value]
    raise ValueError(f"unsupported database value type: {type(value).__name__}")


def _normalized_row(row: Mapping[str, Any]) -> dict[str, object]:
    actual_keys = frozenset(row)
    if actual_keys != EXPECTED_EXPORT_KEYS:
        missing = sorted(EXPECTED_EXPORT_KEYS - actual_keys)
        extra = sorted(actual_keys - EXPECTED_EXPORT_KEYS)
        raise ValueError(f"snapshot row schema mismatch; missing={missing}, extra={extra}")
    normalized = {
        column: _json_safe(row[column]) for column in EXPECTED_EXPORT_COLUMNS
    }
    canonical_bld_id = normalized["canonical_bld_id"]
    source_refs = normalized["source_refs"]
    if not isinstance(canonical_bld_id, str) or not canonical_bld_id:
        raise ValueError(f"invalid canonical_bld_id: {canonical_bld_id!r}")
    if not isinstance(source_refs, (dict, list)):
        raise ValueError(
            f"source_refs for {canonical_bld_id} must be object/array, "
            f"got {type(source_refs).__name__}"
        )
    return normalized


def _assert_paths_available(output: Path, report_path: Path | None = None) -> None:
    paths = [output, output.with_name(output.name + ".partial")]
    if report_path is not None:
        paths.extend(
            [report_path, report_path.with_name(report_path.name + ".partial")]
        )
    collisions = [path.resolve() for path in paths if path.exists()]
    if collisions:
        raise FileExistsError(
            "refusing to overwrite immutable artifact(s): "
            + ", ".join(path.as_posix() for path in collisions)
        )


def export_rows(
    rows: Iterable[Mapping[str, Any]],
    output: Path,
    *,
    expected_rows: int,
) -> dict[str, object]:
    """Write a full-schema canonical JSONL without clobbering an artifact."""

    if expected_rows < 1:
        raise ValueError("expected_rows must be positive")
    output = output.resolve()
    partial = output.with_name(output.name + ".partial")
    _assert_paths_available(output)
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
        "columns": list(EXPECTED_EXPORT_COLUMNS),
        "column_count": len(EXPECTED_EXPORT_COLUMNS),
        "excluded_columns": ["embedding"],
        "source_refs_root_types": dict(sorted(root_types.items())),
        "file_sha256": digest.hexdigest(),
        "bytes": output.stat().st_size,
    }


def _write_report(report: Mapping[str, object], path: Path) -> None:
    path = path.resolve()
    partial = path.with_name(path.name + ".partial")
    if path.exists() or partial.exists():
        raise FileExistsError(f"refusing to overwrite immutable report: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    with partial.open("xb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    partial.rename(path)


def _validate_live_columns(live_columns: Sequence[str]) -> None:
    actual = frozenset(live_columns)
    expected = frozenset(EXPECTED_DB_COLUMNS)
    if len(live_columns) != len(actual):
        raise ValueError("information_schema returned duplicate column names")
    if actual != expected:
        raise ValueError(
            "live table schema drift; update the exporter before snapshotting: "
            f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )


def export_from_neon(
    output: Path,
    *,
    report_path: Path | None = None,
    expected_rows: int = DEFAULT_EXPECTED_ROWS,
    batch_size: int = 1_000,
) -> dict[str, object]:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    output = output.resolve()
    report_path = (report_path or default_report_path(output)).resolve()
    _assert_paths_available(output, report_path)

    connection, connection_profile = _connect_prefer_select_only()
    connection.set_session(
        readonly=True,
        autocommit=False,
        isolation_level=ISOLATION_LEVEL_REPEATABLE_READ,
    )
    try:
        with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute(
                """SELECT current_database() AS database_name,
                          current_user AS database_user,
                          current_schema() AS table_schema,
                          current_setting('transaction_read_only') AS transaction_read_only,
                          txid_current_snapshot()::text AS transaction_snapshot,
                          has_table_privilege(current_user, %s, 'SELECT') AS can_select,
                          has_table_privilege(current_user, %s, 'INSERT,UPDATE,DELETE') AS can_write
                   """,
                (TABLE, TABLE),
            )
            metadata = dict(cursor.fetchone())
            cursor.execute(
                """SELECT column_name
                     FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s
                    ORDER BY ordinal_position""",
                (metadata["table_schema"], TABLE),
            )
            live_columns = [str(row["column_name"]) for row in cursor.fetchall()]
            _validate_live_columns(live_columns)
            cursor.execute(
                sql.SQL(
                    "SELECT COUNT(*) AS table_rows, "
                    "COUNT(DISTINCT canonical_bld_id) AS unique_ids FROM {}"
                ).format(sql.Identifier(TABLE))
            )
            counts = dict(cursor.fetchone())

        if metadata["transaction_read_only"] != "on":
            raise RuntimeError("database transaction is not read-only")
        if metadata["can_select"] is not True:
            raise RuntimeError("database user lacks SELECT on canonical_v2_buildings")
        if connection_profile.startswith(".env.make-web") and metadata["can_write"] is True:
            raise RuntimeError(".env.make-web role unexpectedly has table write privileges")
        if int(counts["table_rows"]) != expected_rows:
            raise ValueError(
                f"live row-count mismatch: expected {expected_rows}, got {counts['table_rows']}"
            )
        if int(counts["unique_ids"]) != expected_rows:
            raise ValueError(
                f"live PK uniqueness mismatch: expected {expected_rows}, got {counts['unique_ids']}"
            )

        select_columns = sql.SQL(", ").join(
            sql.Identifier(column) for column in EXPECTED_EXPORT_COLUMNS
        )
        query = sql.SQL('SELECT {} FROM {} ORDER BY canonical_bld_id COLLATE "C"').format(
            select_columns,
            sql.Identifier(TABLE),
        )
        with connection.cursor(
            name="vnext_legacy_neon_snapshot",
            cursor_factory=psycopg2.extras.RealDictCursor,
        ) as cursor:
            cursor.itersize = batch_size
            cursor.execute(query)
            report = export_rows(cursor, output, expected_rows=expected_rows)

        connection.rollback()
        report.update(
            {
                "report": report_path.as_posix(),
                "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "database": metadata["database_name"],
                "database_user": metadata["database_user"],
                "connection_profile": connection_profile,
                "table": f"{metadata['table_schema']}.{TABLE}",
                "live_columns": live_columns,
                "live_column_count": len(live_columns),
                "transaction_read_only": metadata["transaction_read_only"],
                "transaction_snapshot": metadata["transaction_snapshot"],
                "role_can_select": bool(metadata["can_select"]),
                "role_can_write_table": bool(metadata["can_write"]),
                "table_rows": int(counts["table_rows"]),
                "unique_ids": int(counts["unique_ids"]),
                "sql_writes": 0,
                "exporter_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            }
        )
        _write_report(report, report_path)
        return report
    finally:
        if not connection.closed:
            connection.rollback()
            connection.close()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--report",
        type=Path,
        help="immutable PASS report path (default: <output-stem>.report.json)",
    )
    parser.add_argument("--expected-rows", type=int, default=DEFAULT_EXPECTED_ROWS)
    parser.add_argument("--batch-size", type=int, default=1_000)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    report = export_from_neon(
        args.output,
        report_path=args.report,
        expected_rows=args.expected_rows,
        batch_size=args.batch_size,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
