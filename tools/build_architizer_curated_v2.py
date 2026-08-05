"""Build the final immutable Architizer source-specific curated SQLite v2.0.

Inputs are four separately frozen artifacts: the legacy raw snapshot, curated
v1.3, an eligible reconciliation plan bundle, and structured A+Awards v2.  The
legacy v1.3 materializer is reused against a temporary reconciled source copy;
the resulting complete v1.3 contract is then extended with lossless v2
reconciliation and awards provenance.

This tool is offline-only.  Every SQLite input is opened read-only/immutable,
all byte identities are pinned, and READY is published last.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from canonical.architizer_curated_v2 import (  # noqa: E402
    EXTENSION_DDL,
    MATERIALIZATION_POLICY_VERSION,
    MATERIALIZATION_SELECTION_VERSION,
    MATERIALIZER_VERSION,
    READY_VERSION,
    SCHEMA_VERSION,
)
from canonical.architizer_reconciliation import (  # noqa: E402
    FINAL_TARGET_SCHEMA_VERSION,
    RECONCILIATION_POLICY_VERSION,
    RECONCILIATION_READY_VERSION,
    RECONCILIATION_SCHEMA_VERSION,
    RECONCILIATION_TOOL_VERSION,
)
from crawl.architizer.awards_store_v2 import (  # noqa: E402
    AwardsBuildError as AwardsStoreBuildError,
    BUILDER_VERSION as AWARDS_BUILDER_VERSION,
    PARSER_VERSION as AWARDS_PARSER_VERSION,
    POLICY_VERSION as AWARDS_POLICY_VERSION,
    READY_VERSION as AWARDS_READY_VERSION,
    SCHEMA_VERSION as AWARDS_SCHEMA_VERSION,
    validate_release_contract as validate_awards_release_contract,
)
from crawl.architizer.recrawl_v2 import STATE_SCHEMA_VERSION  # noqa: E402
from tools import build_architizer_curated as curated_v1  # noqa: E402


FIXED_RAW_SHA256 = "35FAA8AD2B4681033E1F7F74148499B29009777977204C7A65923D8FABB5C985"
FIXED_RAW_SIZE_BYTES = 90_918_912
FIXED_BASELINE_SHA256 = "5AEA8A85FA54B139F31585069C50A19AFA7D87B500A44AAAD32CCDC29937B089"
FIXED_BASELINE_SIZE_BYTES = 687_579_136

DEFAULT_RAW = REPO_ROOT / "data" / "crawl" / "architizer.db"
DEFAULT_BASELINE = REPO_ROOT / "data" / "curated" / "architizer_curated_v1_3.db"
DEFAULT_RECONCILIATION = (
    REPO_ROOT / "data" / "enrichment" / "architizer_curated_v2_reconciliation.db"
)
DEFAULT_RECONCILIATION_REPORT = (
    REPO_ROOT / "data" / "reports" / "architizer_curated_v2_reconciliation.md"
)
DEFAULT_AWARDS = REPO_ROOT / "data" / "enrichment" / "architizer_awards_v2.db"
DEFAULT_OUTPUT = REPO_ROOT / "data" / "curated" / "architizer_curated_v2_0.db"
DEFAULT_REPORT = REPO_ROOT / "data" / "reports" / "architizer_curated_v2_0.md"

OFFICIAL_2026_AWARD_TRACKS = frozenset(
    {"Firm", "Plus", "Products", "Sustainability", "Typology"}
)
OFFICIAL_2026_AWARD_ROOT = "https://winners.architizer.com/2026/"
PRODUCTION_2026_AWARD_TRACK_COUNTS = {
    "Firm": 104,
    "Plus": 211,
    "Products": 153,
    "Sustainability": 90,
    "Typology": 472,
}
EXPECTED_AWARD_PROJECTION_POLICIES = {
    "project": {
        "entity_kind": "project",
        "preserve_in_source_corpus": 1,
        "corpus_role": "award subject",
        "project_firm_curated_projection": (
            "eligible for project curated reconciliation after identity validation"
        ),
        "policy_version": AWARDS_POLICY_VERSION,
    },
    "firm": {
        "entity_kind": "firm",
        "preserve_in_source_corpus": 1,
        "corpus_role": "award subject or company relation",
        "project_firm_curated_projection": (
            "eligible for firm curated reconciliation after identity validation"
        ),
        "policy_version": AWARDS_POLICY_VERSION,
    },
    "product": {
        "entity_kind": "product",
        "preserve_in_source_corpus": 1,
        "corpus_role": "award subject",
        "project_firm_curated_projection": (
            "source-corpus only; excluded from project/firm curated projection"
        ),
        "policy_version": AWARDS_POLICY_VERSION,
    },
    "brand": {
        "entity_kind": "brand",
        "preserve_in_source_corpus": 1,
        "corpus_role": "award company relation",
        "project_firm_curated_projection": (
            "source-corpus only; excluded from project/firm curated projection"
        ),
        "policy_version": AWARDS_POLICY_VERSION,
    },
}

FULL_DEFERRED_INDEX_DDL = (
    (
        "idx_v2_reconciliation_entities_type",
        "CREATE INDEX idx_v2_reconciliation_entities_type "
        "ON v2_reconciliation_entities(entity_type,inclusion_status,source_slug)",
    ),
    (
        "idx_v2_reconciliation_conflicts",
        "CREATE INDEX idx_v2_reconciliation_conflicts "
        "ON v2_reconciliation_field_conflicts(conflict_kind,field_name)",
    ),
    (
        "idx_v2_reconciliation_target_reasons",
        "CREATE INDEX idx_v2_reconciliation_target_reasons "
        "ON v2_reconciliation_target_reasons(reason,target_entity_type,target_status)",
    ),
    (
        "idx_v2_awards_track",
        "CREATE INDEX idx_v2_awards_track "
        "ON v2_structured_award_attributions(award_year,award_track,attribution_pk)",
    ),
    (
        "idx_v2_awards_subject",
        "CREATE INDEX idx_v2_awards_subject "
        "ON v2_structured_award_attributions(subject_kind,subject_slug)",
    ),
    (
        "idx_v2_award_links",
        "CREATE INDEX idx_v2_award_links "
        "ON v2_structured_award_entity_links(entity_kind,raw_slug,link_status)",
    ),
    (
        "idx_v2_award_base_projection",
        "CREATE INDEX idx_v2_award_base_projection "
        "ON v2_structured_award_base_projections"
        "(projection_status,projected_source_award_id)",
    ),
)


class CuratedV2Error(RuntimeError):
    """Raised before any immutable v2 output is considered ready."""


def _available_memory_bytes() -> Optional[int]:
    """Best-effort host available-memory reading without a new dependency."""

    try:
        if os.name == "nt":
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_ulong),
                    ("memory_load", ctypes.c_ulong),
                    ("total_physical", ctypes.c_ulonglong),
                    ("available_physical", ctypes.c_ulonglong),
                    ("total_page_file", ctypes.c_ulonglong),
                    ("available_page_file", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong),
                    ("available_virtual", ctypes.c_ulonglong),
                    ("available_extended_virtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.available_physical)
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        available_pages = int(os.sysconf("SC_AVPHYS_PAGES"))
        return page_size * available_pages
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def preflight_full_resources(
    *,
    raw_path: Path,
    baseline_path: Path,
    reconciliation_path: Path,
    awards_path: Path,
    output_path: Path,
    verify_deterministic: bool,
) -> dict[str, Any]:
    """Fail before hashing/building when full-build temp capacity is unsafe."""

    sizes = {
        "raw": raw_path.stat().st_size,
        "baseline": baseline_path.stat().st_size,
        "reconciliation": reconciliation_path.stat().st_size,
        "awards": awards_path.stat().st_size,
    }
    table_groups = {
        "baseline": (
            baseline_path,
            ("source_projects", "source_firms", "source_awards"),
        ),
        "reconciliation": (
            reconciliation_path,
            (
                "entities",
                "source_target_reasons",
                "field_candidates",
                "field_decisions",
                "field_lineage",
                "field_conflicts",
                "entity_aliases",
            ),
        ),
        "awards": (
            awards_path,
            (
                "award_page_versions",
                "award_attributions",
                "award_attribution_tiers",
                "award_attribution_companies",
            ),
        ),
    }
    input_cardinalities: dict[str, int] = {}
    try:
        for role, (path, tables) in table_groups.items():
            connection = open_readonly(path)
            try:
                for table in tables:
                    input_cardinalities[f"{role}.{table}"] = int(
                        connection.execute(
                            f'SELECT COUNT(*) FROM "{table}"'
                        ).fetchone()[0]
                    )
            finally:
                connection.close()
    except sqlite3.Error as exc:
        raise CuratedV2Error(
            f"full materialization cardinality preflight failed: {exc}"
        ) from exc

    estimated_output_bytes = (
        sizes["baseline"] + sizes["reconciliation"] + sizes["awards"]
    )
    # Full deterministic publication can have the primary output, a shadow
    # build, and a SQLite VACUUM/publication temp copy live at once.  The raw
    # reconciled legacy copy is additional working space.
    output_workspace_copies = 3 if verify_deterministic else 2
    estimated_temp_copy_bytes = (
        estimated_output_bytes * output_workspace_copies + sizes["raw"]
    )
    safety_margin_bytes = 512 * 1024**2
    required_free_disk_bytes = max(
        3 * estimated_output_bytes + safety_margin_bytes,
        estimated_temp_copy_bytes + safety_margin_bytes,
    )
    available_disk_bytes = int(shutil.disk_usage(output_path.parent).free)
    cardinality_memory_bytes = min(
        4 * 1024**3,
        input_cardinalities.get("reconciliation.entities", 0) * 1024
        + input_cardinalities.get("reconciliation.field_candidates", 0) * 192
        + input_cardinalities.get("reconciliation.field_lineage", 0) * 128
        + input_cardinalities.get("reconciliation.source_target_reasons", 0)
        * 256
        + input_cardinalities.get("awards.award_attributions", 0) * 512,
    )
    required_available_memory_bytes = (
        512 * 1024**2
        + min(sizes["reconciliation"] // 16, 2 * 1024**3)
        + min(sizes["raw"] // 16, 1024**3)
        + cardinality_memory_bytes
    )
    available_memory_bytes = _available_memory_bytes()
    if available_disk_bytes < required_free_disk_bytes:
        raise CuratedV2Error(
            "full materialization disk preflight failed: "
            f"available={available_disk_bytes} required={required_free_disk_bytes} "
            f"estimated_temp_copy={estimated_temp_copy_bytes}"
        )
    if (
        available_memory_bytes is None
        or available_memory_bytes < required_available_memory_bytes
    ):
        raise CuratedV2Error(
            "full materialization RAM preflight failed: "
            f"available={available_memory_bytes} "
            f"required={required_available_memory_bytes}"
        )
    return {
        **{f"{key}_input_bytes": int(value) for key, value in sizes.items()},
        "input_cardinalities": dict(sorted(input_cardinalities.items())),
        "estimated_output_bytes": estimated_output_bytes,
        "output_workspace_copies": output_workspace_copies,
        "estimated_temp_copy_bytes": estimated_temp_copy_bytes,
        "safety_margin_bytes": safety_margin_bytes,
        "required_free_disk_bytes": required_free_disk_bytes,
        "available_disk_bytes": available_disk_bytes,
        "required_available_memory_bytes": required_available_memory_bytes,
        "cardinality_memory_bytes": cardinality_memory_bytes,
        "available_memory_bytes": available_memory_bytes,
    }


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def stable_id(prefix: str, *parts: Any, length: int = 24) -> str:
    payload = "\x1f".join("" if part is None else str(part) for part in parts)
    return prefix + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def path_label(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return path.name


def sqlite_sidecars(path: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    for suffix in ("-wal", "-journal"):
        candidate = Path(str(path) + suffix)
        result[suffix[1:]] = candidate.stat().st_size if candidate.exists() else 0
    return result


def assert_quiescent(path: Path, *, lock_suffixes: Sequence[str] = ()) -> None:
    sidecars = sqlite_sidecars(path)
    if any(sidecars.values()):
        raise CuratedV2Error(f"input has uncheckpointed SQLite state: {path} {sidecars}")
    for suffix in lock_suffixes:
        lock = Path(str(path) + suffix)
        if lock.exists():
            raise CuratedV2Error(f"input lock exists: {lock}")


def open_readonly(path: Path, *, lock_suffixes: Sequence[str] = ()) -> sqlite3.Connection:
    assert_quiescent(path, lock_suffixes=lock_suffixes)
    connection = sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro&immutable=1",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
        connection.close()
        raise CuratedV2Error(f"input is not query_only: {path}")
    return connection


def sqlite_audit(connection: sqlite3.Connection) -> dict[str, Any]:
    quick = connection.execute("PRAGMA quick_check").fetchone()[0]
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    query_only = connection.execute("PRAGMA query_only").fetchone()[0]
    if quick != "ok" or integrity != "ok" or foreign_keys or query_only != 1:
        raise CuratedV2Error(
            "SQLite input validation failed: "
            f"quick={quick}, integrity={integrity}, foreign_keys={len(foreign_keys)}, "
            f"query_only={query_only}"
        )
    return {
        "quick_check": quick,
        "integrity_check": integrity,
        "foreign_key_violations": len(foreign_keys),
        "query_only": query_only,
    }


def logical_database_digest(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    objects = connection.execute(
        "SELECT type,name,sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ).fetchall()
    for row in objects:
        digest.update(canonical_json(list(row)).encode("utf-8"))
        if row[0] != "table":
            continue
        table_info = list(
            connection.execute(f'PRAGMA table_info("{row[1]}")')
        )
        columns = [item[1] for item in table_info]
        if not columns:
            continue
        quoted = ",".join(f'"{column}"' for column in columns)
        primary_key_columns = [
            item[1]
            for item in sorted(
                (item for item in table_info if int(item[5]) > 0),
                key=lambda item: int(item[5]),
            )
        ]
        order_columns = primary_key_columns or columns
        quoted_order = ",".join(f'"{column}"' for column in order_columns)
        for values in connection.execute(
            f'SELECT {quoted} FROM "{row[1]}" ORDER BY {quoted_order}'
        ):
            digest.update(canonical_json(list(values)).encode("utf-8"))
    return digest.hexdigest().upper()


def fsync_file(path: Path) -> None:
    with path.open("rb+") as handle:
        os.fsync(handle.fileno())


def table_names(connection: sqlite3.Connection, kind: str = "table") -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type=?", (kind,)
        )
    }


def one_row(connection: sqlite3.Connection, table: str) -> sqlite3.Row:
    rows = connection.execute(f'SELECT * FROM "{table}"').fetchall()
    if len(rows) != 1:
        raise CuratedV2Error(f"{table} must contain exactly one row; found {len(rows)}")
    return rows[0]


def json_value(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list, int, float, bool)):
        return value
    try:
        return json.loads(str(value))
    except (json.JSONDecodeError, TypeError, ValueError):
        return default


def canonical_json_text(value: Any, default: Any) -> str:
    parsed = json_value(value, default)
    if not isinstance(parsed, type(default)):
        parsed = default
    return canonical_json(parsed)


def contract_objects(connection: sqlite3.Connection) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for row in connection.execute(
        "SELECT type,name,sql FROM sqlite_master "
        "WHERE type IN ('table','view','index') "
        "AND name NOT LIKE 'sqlite_%' AND sql IS NOT NULL ORDER BY type,name"
    ):
        sql = str(row["sql"])
        columns: list[dict[str, Any]] = []
        if row["type"] in {"table", "view"}:
            columns = [
                {
                    "cid": item["cid"],
                    "name": item["name"],
                    "type": item["type"],
                    "notnull": item["notnull"],
                    "default": item["dflt_value"],
                    "pk": item["pk"],
                }
                for item in connection.execute(
                    f'PRAGMA table_info("{row["name"]}")'
                )
            ]
        result[(row["type"], row["name"])] = {
            "sql": sql,
            "sql_sha256": hashlib.sha256(sql.encode("utf-8")).hexdigest().upper(),
            "columns_json": canonical_json(columns),
        }
    return result


def validate_file_identity(
    path: Path,
    *,
    expected_sha256: str,
    expected_size: int,
    label: str,
) -> dict[str, Any]:
    if not path.is_file():
        raise CuratedV2Error(f"missing {label}: {path}")
    size = path.stat().st_size
    digest = sha256_file(path)
    if size != expected_size:
        raise CuratedV2Error(
            f"{label} size mismatch: expected {expected_size}, found {size}"
        )
    if digest != expected_sha256.upper():
        raise CuratedV2Error(
            f"{label} SHA mismatch: expected {expected_sha256.upper()}, found {digest}"
        )
    return {
        "path_label": path_label(path),
        "sha256": digest,
        "size_bytes": size,
    }


def validate_baseline(
    baseline: sqlite3.Connection,
    *,
    raw_identity: Mapping[str, Any],
) -> dict[str, Any]:
    required = set(curated_v1.REQUIRED_SOURCE_COLUMNS)
    del required  # The baseline contract is validated by its captured objects below.
    build = one_row(baseline, "build_runs")
    if build["schema_version"] != curated_v1.SCHEMA_VERSION:
        raise CuratedV2Error(
            f"baseline schema mismatch: {build['schema_version']} != {curated_v1.SCHEMA_VERSION}"
        )
    snapshot = one_row(baseline, "source_snapshots")
    if (
        snapshot["source_sha256_before"] != raw_identity["sha256"]
        or snapshot["source_sha256_after"] != raw_identity["sha256"]
        or snapshot["source_size_bytes"] != raw_identity["size_bytes"]
    ):
        raise CuratedV2Error("curated v1.3 lineage does not match the supplied legacy raw DB")
    return {
        "build": dict(build),
        "source_snapshot": dict(snapshot),
        "contract": contract_objects(baseline),
    }


def validate_reconciliation(
    connection: sqlite3.Connection,
    *,
    identity: Mapping[str, Any],
    raw_identity: Mapping[str, Any],
    baseline_identity: Mapping[str, Any],
    baseline_contract: Mapping[tuple[str, str], Mapping[str, Any]],
    report_path: Path,
    ready_path: Path,
) -> dict[str, Any]:
    required_tables = {
        "reconciliation_runs",
        "input_snapshots",
        "trusted_input_manifest",
        "baseline_contract_objects",
        "entities",
        "entity_aliases",
        "source_target_reasons",
        "field_candidates",
        "field_decisions",
        "field_lineage",
        "field_conflicts",
        "qa_issues",
        "reconciliation_metrics",
    }
    required_views = {"v_reconciled_projects", "v_reconciled_firms"}
    missing = sorted(required_tables - table_names(connection))
    missing_views = sorted(required_views - table_names(connection, "view"))
    if missing or missing_views:
        raise CuratedV2Error(
            f"reconciliation contract missing: tables={missing}, views={missing_views}"
        )
    run = one_row(connection, "reconciliation_runs")
    if run["plan_schema_version"] != RECONCILIATION_SCHEMA_VERSION:
        raise CuratedV2Error("unsupported reconciliation plan schema")
    if (
        run["tool_version"] != RECONCILIATION_TOOL_VERSION
        or run["policy_version"] != RECONCILIATION_POLICY_VERSION
        or run["artifact_kind"] != "intermediate_reconciliation_plan"
        or run["baseline_schema_version"] != curated_v1.SCHEMA_VERSION
    ):
        raise CuratedV2Error("unsupported reconciliation run contract")
    if run["final_target_schema_version"] != FINAL_TARGET_SCHEMA_VERSION:
        raise CuratedV2Error("reconciliation target schema is not curated v2.0")
    if run["publication_eligibility"] != "eligible_materialization_input":
        raise CuratedV2Error("reconciliation plan is not eligible_materialization_input")
    if (
        run["project_limit"] is not None
        or run["firm_limit"] is not None
        or run["pending_target_count"] != 0
    ):
        raise CuratedV2Error("reconciliation plan is not full and converged")
    if connection.execute("SELECT COUNT(*) FROM trusted_input_manifest").fetchone()[0] != 1:
        raise CuratedV2Error("reconciliation trusted manifest is missing")
    trusted_manifest_row = one_row(connection, "trusted_input_manifest")
    trusted_manifest_payload = json_value(trusted_manifest_row["manifest_json"], None)
    if not isinstance(trusted_manifest_payload, dict):
        raise CuratedV2Error("reconciliation trusted manifest payload is invalid")
    trusted_manifest_bytes = (
        json.dumps(
            trusted_manifest_payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    if (
        trusted_manifest_row["manifest_version"]
        != trusted_manifest_payload.get("manifest_version")
        or trusted_manifest_row["sha256"]
        != hashlib.sha256(trusted_manifest_bytes).hexdigest().upper()
        or trusted_manifest_row["size_bytes"] != len(trusted_manifest_bytes)
    ):
        raise CuratedV2Error("reconciliation trusted manifest receipt mismatch")

    input_rows = {
        row["input_role"]: row
        for row in connection.execute("SELECT * FROM input_snapshots")
    }
    if set(input_rows) != {"legacy_raw", "curated_v1_3", "recrawl_sidecar"}:
        raise CuratedV2Error("reconciliation input role set is incomplete")
    for role, expected in (
        ("legacy_raw", raw_identity),
        ("curated_v1_3", baseline_identity),
    ):
        row = input_rows[role]
        if (
            row["sha256_before"] != expected["sha256"]
            or row["sha256_after"] != expected["sha256"]
            or row["size_bytes"] != expected["size_bytes"]
        ):
            raise CuratedV2Error(f"reconciliation {role} lineage mismatch")

    captured_contract = {
        (row["object_type"], row["object_name"]): row
        for row in connection.execute("SELECT * FROM baseline_contract_objects")
    }
    if set(captured_contract) != set(baseline_contract):
        raise CuratedV2Error("reconciliation baseline contract object set mismatch")
    for key, actual in baseline_contract.items():
        captured = captured_contract[key]
        if (
            captured["sql_sha256"] != actual["sql_sha256"]
            or captured["sql_text"] != actual["sql"]
            or captured["columns_json"] != actual["columns_json"]
        ):
            raise CuratedV2Error(f"reconciliation baseline contract mismatch: {key}")

    if not report_path.is_file() or not ready_path.is_file():
        raise CuratedV2Error("reconciliation report/READY bundle is incomplete")
    try:
        ready = json.loads(ready_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise CuratedV2Error(f"invalid reconciliation READY marker: {exc}") from exc
    ready_keys = {
        "artifact_kind",
        "ready_version",
        "tool_version",
        "plan_schema_version",
        "policy_version",
        "baseline_schema_version",
        "final_target_schema_version",
        "publication_eligibility",
        "reconciliation_id",
        "project_limit",
        "firm_limit",
        "selected_project_count",
        "selected_firm_count",
        "pending_target_count",
        "database",
        "report",
        "trusted_manifest",
        "validation",
    }
    if not isinstance(ready, dict) or set(ready) != ready_keys:
        raise CuratedV2Error("reconciliation READY top-level contract mismatch")
    expected_receipt = {
        "artifact_kind": "architizer_reconciliation_plan",
        "ready_version": RECONCILIATION_READY_VERSION,
        "tool_version": run["tool_version"],
        "plan_schema_version": run["plan_schema_version"],
        "policy_version": run["policy_version"],
        "baseline_schema_version": run["baseline_schema_version"],
        "final_target_schema_version": run["final_target_schema_version"],
        "publication_eligibility": run["publication_eligibility"],
        "reconciliation_id": run["reconciliation_id"],
        "project_limit": run["project_limit"],
        "firm_limit": run["firm_limit"],
        "selected_project_count": run["selected_project_count"],
        "selected_firm_count": run["selected_firm_count"],
        "pending_target_count": run["pending_target_count"],
    }
    for key, expected in expected_receipt.items():
        if ready.get(key) != expected:
            raise CuratedV2Error(f"reconciliation READY {key} mismatch")

    ready_database = _required_mapping(ready.get("database"), "database")
    if set(ready_database) != {"path", "sha256", "logical_sha256", "size_bytes"}:
        raise CuratedV2Error("reconciliation READY database contract mismatch")
    if (
        ready_database.get("path") != identity["path_label"]
        or ready_database.get("sha256") != identity["sha256"]
        or ready_database.get("size_bytes") != identity["size_bytes"]
    ):
        raise CuratedV2Error("reconciliation READY database identity mismatch")
    report_sha = sha256_file(report_path)
    ready_report = _required_mapping(ready.get("report"), "report")
    if set(ready_report) != {"path", "sha256", "size_bytes"}:
        raise CuratedV2Error("reconciliation READY report contract mismatch")
    if (
        ready_report.get("path") != path_label(report_path)
        or ready_report.get("sha256") != report_sha
        or ready_report.get("size_bytes") != report_path.stat().st_size
    ):
        raise CuratedV2Error("reconciliation READY report identity mismatch")
    logical_sha = logical_database_digest(connection)
    if ready_database.get("logical_sha256") != logical_sha:
        raise CuratedV2Error("reconciliation READY logical SHA mismatch")

    ready_manifest = _required_mapping(
        ready.get("trusted_manifest"), "trusted_manifest"
    )
    if set(ready_manifest) != {
        "path",
        "manifest_version",
        "sha256",
        "size_bytes",
    }:
        raise CuratedV2Error("reconciliation READY trusted manifest contract mismatch")
    if ready_manifest != {
        "path": trusted_manifest_row["path_label"],
        "manifest_version": trusted_manifest_row["manifest_version"],
        "sha256": trusted_manifest_row["sha256"],
        "size_bytes": trusted_manifest_row["size_bytes"],
    }:
        raise CuratedV2Error("reconciliation READY trusted manifest mismatch")

    run_validation = json_value(run["validation_json"], None)
    output_validation = {
        "quick_check": connection.execute("PRAGMA quick_check").fetchone()[0],
        "foreign_key_violation_count": len(
            connection.execute("PRAGMA foreign_key_check").fetchall()
        ),
    }
    if ready.get("validation") != {
        "inputs": run_validation,
        "output": output_validation,
    }:
        raise CuratedV2Error("reconciliation READY validation receipt mismatch")

    selected_project_count = connection.execute(
        "SELECT COUNT(*) FROM entities WHERE entity_type='project'"
    ).fetchone()[0]
    selected_firm_count = connection.execute(
        "SELECT COUNT(*) FROM entities WHERE entity_type='firm'"
    ).fetchone()[0]
    project_count = connection.execute(
        "SELECT COUNT(*) FROM v_reconciled_projects"
    ).fetchone()[0]
    firm_count = connection.execute(
        "SELECT COUNT(*) FROM v_reconciled_firms"
    ).fetchone()[0]
    included_project_count = connection.execute(
        "SELECT COUNT(*) FROM entities "
        "WHERE entity_type='project' AND inclusion_status='included'"
    ).fetchone()[0]
    included_firm_count = connection.execute(
        "SELECT COUNT(*) FROM entities "
        "WHERE entity_type='firm' AND inclusion_status='included'"
    ).fetchone()[0]
    if project_count <= 0 or firm_count <= 0:
        raise CuratedV2Error("reconciliation effective corpus is empty")
    if (
        int(selected_project_count) != int(run["selected_project_count"])
        or int(selected_firm_count) != int(run["selected_firm_count"])
    ):
        raise CuratedV2Error(
            "reconciliation selected counts do not match entity ledger"
        )
    if (
        int(project_count) != int(included_project_count)
        or int(firm_count) != int(included_firm_count)
    ):
        raise CuratedV2Error("reconciliation effective views do not match included ledger")
    return {
        "run": dict(run),
        "ready": ready,
        "ready_identity": {
            "path_label": path_label(ready_path),
            "sha256": sha256_file(ready_path),
            "size_bytes": ready_path.stat().st_size,
        },
        "report_identity": {
            "path_label": path_label(report_path),
            "sha256": report_sha,
            "size_bytes": report_path.stat().st_size,
        },
        "project_count": int(project_count),
        "firm_count": int(firm_count),
        "qa_only_project_count": int(selected_project_count - included_project_count),
        "qa_only_firm_count": int(selected_firm_count - included_firm_count),
        "input_rows": {key: dict(value) for key, value in input_rows.items()},
        "trusted_manifest": dict(trusted_manifest_row),
    }


def _required_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CuratedV2Error(f"structured awards READY {label} is missing")
    return value


def validate_awards(
    connection: sqlite3.Connection,
    *,
    identity: Mapping[str, Any],
    awards_path: Path,
    ready_path: Path,
    enforce_production_counts: bool = True,
) -> dict[str, Any]:
    required = {
        "schema_meta",
        "input_lineage",
        "award_page_versions",
        "award_attributions",
        "award_attribution_tiers",
        "award_attribution_companies",
        "corpus_projection_policy",
        "build_manifest",
    }
    missing = sorted(required - table_names(connection))
    if missing:
        raise CuratedV2Error(f"structured awards contract missing: {missing}")
    meta = dict(connection.execute("SELECT key,value FROM schema_meta"))
    if meta.get("schema_version") != AWARDS_SCHEMA_VERSION:
        raise CuratedV2Error("unsupported structured awards schema")
    manifest = one_row(connection, "build_manifest")
    lineage = one_row(connection, "input_lineage")
    count = connection.execute("SELECT COUNT(*) FROM award_attributions").fetchone()[0]
    if (
        manifest["build_limit"] is not None
        or manifest["is_full_snapshot_projection"] != 1
        or manifest["source_record_count"] != count
        or manifest["selected_record_count"] != count
    ):
        raise CuratedV2Error("structured awards input is not a full snapshot projection")

    pages = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM award_page_versions ORDER BY page_kind,award_track,id"
        )
    ]
    roots = [page for page in pages if page["page_kind"] == "year_root"]
    tracks = [page for page in pages if page["page_kind"] == "track"]
    track_names = {str(page["award_track"]) for page in tracks}
    if (
        len(roots) != 1
        or len(tracks) != len(OFFICIAL_2026_AWARD_TRACKS)
        or track_names != set(OFFICIAL_2026_AWARD_TRACKS)
        or len(pages) != 6
    ):
        raise CuratedV2Error(
            "structured awards official 2026 page/track contract mismatch"
        )
    root_page = roots[0]
    if (
        root_page["award_year"] != 2026
        or root_page["award_track"] is not None
        or root_page["requested_url"] != OFFICIAL_2026_AWARD_ROOT
        or root_page["final_url"] != OFFICIAL_2026_AWARD_ROOT
        or root_page["source_record_count"] != 0
        or root_page["selected_record_count"] != 0
    ):
        raise CuratedV2Error("structured awards official year-root contract mismatch")
    parent_parity_mismatches = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM award_attributions AS a
            LEFT JOIN award_page_versions AS p ON p.id=a.page_version_id
            WHERE p.id IS NULL OR p.page_kind!='track'
               OR a.award_year IS NOT p.award_year
               OR a.award_track IS NOT p.award_track
               OR a.source_url IS NOT p.requested_url
            """
        ).fetchone()[0]
    )
    if parent_parity_mismatches:
        raise CuratedV2Error(
            "structured awards attribution parent page parity mismatch: "
            f"{parent_parity_mismatches}"
        )
    attribution_counts = {
        int(row["page_version_id"]): int(row["n"])
        for row in connection.execute(
            "SELECT page_version_id,COUNT(*) AS n FROM award_attributions "
            "GROUP BY page_version_id"
        )
    }
    page_track_record_counts = {
        str(page["award_track"]): attribution_counts.get(int(page["id"]), 0)
        for page in tracks
    }
    actual_track_record_counts = {
        str(row["award_track"]): int(row["n"])
        for row in connection.execute(
            "SELECT award_track,COUNT(*) AS n FROM award_attributions "
            "GROUP BY award_track ORDER BY award_track"
        )
    }
    actual_year_record_counts = {
        int(row["award_year"]): int(row["n"])
        for row in connection.execute(
            "SELECT award_year,COUNT(*) AS n FROM award_attributions "
            "GROUP BY award_year ORDER BY award_year"
        )
    }
    if enforce_production_counts and (
        page_track_record_counts != PRODUCTION_2026_AWARD_TRACK_COUNTS
        or actual_track_record_counts != PRODUCTION_2026_AWARD_TRACK_COUNTS
        or actual_year_record_counts != {2026: 1030}
        or count != sum(PRODUCTION_2026_AWARD_TRACK_COUNTS.values())
    ):
        raise CuratedV2Error(
            "structured awards production run4 record-count contract mismatch"
        )
    for page in tracks:
        track = str(page["award_track"])
        official_url = f"{OFFICIAL_2026_AWARD_ROOT}{track}/"
        page_count = attribution_counts.get(int(page["id"]), 0)
        if (
            page["award_year"] != 2026
            or page["requested_url"] != official_url
            or page["final_url"] != official_url
            or page["final_url_policy"] != "exact"
            or page["parse_status"] != "complete"
            or page["source_record_count"] <= 0
            or page["source_record_count"] != page_count
            or page["selected_record_count"] != page_count
        ):
            raise CuratedV2Error(
                f"structured awards official track page mismatch: {track}"
            )
        wrong_sources = connection.execute(
            "SELECT COUNT(*) FROM award_attributions "
            "WHERE page_version_id=? AND source_url!=?",
            (page["id"], official_url),
        ).fetchone()[0]
        if wrong_sources:
            raise CuratedV2Error(
                f"structured awards attribution source mismatch: {track}"
            )
    typology = next(page for page in tracks if page["award_track"] == "Typology")
    if (
        typology["snapshot_content_sha256"]
        != root_page["snapshot_content_sha256"]
        or typology["snapshot_gzip_sha256"] != root_page["snapshot_gzip_sha256"]
        or typology["snapshot_gzip_path"] != root_page["snapshot_gzip_path"]
    ):
        raise CuratedV2Error(
            "structured awards Typology deduplicated year-root snapshot mismatch"
        )

    manifest_summary = json_value(manifest["summary_json"], None)
    if not isinstance(manifest_summary, dict) or set(manifest_summary) != {
        "award_year",
        "tracks",
        "root_alias_tracks",
        "discovery_counts",
        "product_brand_policy",
    }:
        raise CuratedV2Error("structured awards build manifest summary contract mismatch")
    if (
        manifest["page_count"] != len(pages)
        or manifest["source_record_count"]
        != sum(int(page["source_record_count"]) for page in tracks)
        or manifest["selected_record_count"]
        != sum(int(page["selected_record_count"]) for page in tracks)
        or manifest_summary["award_year"] != 2026
        or set(manifest_summary["tracks"]) != set(OFFICIAL_2026_AWARD_TRACKS)
        or len(manifest_summary["tracks"]) != len(OFFICIAL_2026_AWARD_TRACKS)
        or manifest_summary["root_alias_tracks"] != []
        or manifest_summary["product_brand_policy"] != "preserve_source_only"
    ):
        raise CuratedV2Error("structured awards build manifest parity mismatch")
    manifest_count_contracts = {
        "status_counts_json": {
            str(row["parse_status"]): int(row["n"])
            for row in connection.execute(
                "SELECT parse_status,COUNT(*) AS n FROM award_attributions "
                "GROUP BY parse_status"
            )
        },
        "subject_counts_json": {
            str(row["subject_kind"]): int(row["n"])
            for row in connection.execute(
                "SELECT subject_kind,COUNT(*) AS n FROM award_attributions "
                "WHERE subject_kind IS NOT NULL GROUP BY subject_kind"
            )
        },
        "company_counts_json": {
            str(row["entity_kind"]): int(row["n"])
            for row in connection.execute(
                "SELECT entity_kind,COUNT(*) AS n FROM award_attribution_companies "
                "WHERE entity_kind IS NOT NULL GROUP BY entity_kind"
            )
        },
    }
    for column, expected_counts in manifest_count_contracts.items():
        if json_value(manifest[column], None) != expected_counts:
            raise CuratedV2Error(
                f"structured awards build manifest {column} mismatch"
            )
    years = {
        int(row[0])
        for row in connection.execute(
            "SELECT DISTINCT award_year FROM award_attributions"
        )
    }
    if years != {2026}:
        raise CuratedV2Error(f"structured awards year set is not exactly 2026: {years}")
    policy_rows = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM corpus_projection_policy ORDER BY entity_kind"
        )
    ]
    policies = {str(row["entity_kind"]): row for row in policy_rows}
    if (
        len(policy_rows) != len(EXPECTED_AWARD_PROJECTION_POLICIES)
        or set(policies) != set(EXPECTED_AWARD_PROJECTION_POLICIES)
        or any(
            policies[kind] != expected
            for kind, expected in EXPECTED_AWARD_PROJECTION_POLICIES.items()
        )
    ):
        raise CuratedV2Error(
            "structured awards corpus projection policy contract mismatch"
        )
    kinds = {
        row[0]
        for row in connection.execute(
            "SELECT DISTINCT subject_kind FROM award_attributions "
            "WHERE subject_kind IS NOT NULL"
        )
    }
    company_kinds = {
        row[0]
        for row in connection.execute(
            "SELECT DISTINCT entity_kind FROM award_attribution_companies "
            "WHERE entity_kind IS NOT NULL"
        )
    }
    if "product" not in kinds or "brand" not in company_kinds:
        raise CuratedV2Error("structured 2026 product/brand evidence is missing")
    no_content = connection.execute(
        "SELECT COUNT(*) FROM award_page_versions "
        "WHERE page_kind='track' AND (source_record_count=0 OR parse_status='no_content')"
    ).fetchone()[0]
    if no_content:
        raise CuratedV2Error("structured awards contains an empty official track")
    if not ready_path.is_file():
        raise CuratedV2Error(f"structured awards READY receipt is missing: {ready_path}")
    try:
        ready = json.loads(ready_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CuratedV2Error(f"invalid structured awards READY receipt: {exc}") from exc
    if not isinstance(ready, dict):
        raise CuratedV2Error("structured awards READY receipt must be a JSON object")
    ready_keys = {
        "artifact",
        "ready_version",
        "builder_version",
        "schema_version",
        "parser_version",
        "policy_version",
        "built_at",
        "award_year",
        "build_limit",
        "database",
        "input_sidecar",
        "recrawl_run",
        "snapshot_manifest",
        "validation",
    }
    if set(ready) != ready_keys:
        raise CuratedV2Error("structured awards READY top-level contract mismatch")
    expected_top_level = {
        "artifact": "architizer_awards_v2",
        "ready_version": AWARDS_READY_VERSION,
        "builder_version": AWARDS_BUILDER_VERSION,
        "schema_version": AWARDS_SCHEMA_VERSION,
        "parser_version": AWARDS_PARSER_VERSION,
        "policy_version": AWARDS_POLICY_VERSION,
        "built_at": manifest["built_at"],
        "award_year": 2026,
        "build_limit": manifest["build_limit"],
    }
    for key, expected in expected_top_level.items():
        if ready.get(key) != expected:
            raise CuratedV2Error(
                f"structured awards READY {key} mismatch: "
                f"{ready.get(key)!r} != {expected!r}"
            )

    ready_database = _required_mapping(ready.get("database"), "database")
    if set(ready_database) != {"path", "size_bytes", "sha256"}:
        raise CuratedV2Error("structured awards READY database contract mismatch")
    if (
        ready_database.get("path") != path_label(awards_path)
        or ready_database.get("sha256") != identity["sha256"]
        or ready_database.get("size_bytes") != identity["size_bytes"]
    ):
        raise CuratedV2Error("structured awards READY database identity mismatch")

    ready_sidecar = _required_mapping(ready.get("input_sidecar"), "input_sidecar")
    if set(ready_sidecar) != {
        "path",
        "schema_version",
        "size_bytes",
        "sha256_before",
        "sha256_after",
    }:
        raise CuratedV2Error("structured awards READY input sidecar contract mismatch")
    if (
        ready_sidecar.get("path") != lineage["sidecar_path"]
        or ready_sidecar.get("schema_version") != STATE_SCHEMA_VERSION
        or ready_sidecar.get("size_bytes") != lineage["sidecar_size_bytes"]
        or ready_sidecar.get("sha256_before") != lineage["sidecar_sha256_before"]
        or ready_sidecar.get("sha256_after") != lineage["sidecar_sha256_after"]
        or lineage["sidecar_sha256_before"] != lineage["sidecar_sha256_after"]
    ):
        raise CuratedV2Error("structured awards READY input sidecar lineage mismatch")

    metadata = json_value(lineage["metadata_json"], {})
    if not isinstance(metadata, dict) or not isinstance(metadata.get("run_summary"), dict):
        raise CuratedV2Error("structured awards input lineage lacks run_summary")
    run_summary = metadata["run_summary"]
    expected_track_urls = {
        track: f"{OFFICIAL_2026_AWARD_ROOT}{track}/"
        for track in sorted(OFFICIAL_2026_AWARD_TRACKS)
    }
    if (
        run_summary.get("award_year") != 2026
        or run_summary.get("official_root") != OFFICIAL_2026_AWARD_ROOT
        or not isinstance(run_summary.get("tracks"), list)
        or set(run_summary["tracks"]) != set(OFFICIAL_2026_AWARD_TRACKS)
        or len(run_summary["tracks"]) != len(OFFICIAL_2026_AWARD_TRACKS)
        or run_summary.get("track_urls") != expected_track_urls
    ):
        raise CuratedV2Error("structured awards recrawl run4 contract mismatch")
    direct_urls: dict[str, dict[str, set[str]]] = {
        track: {"project": set(), "firm": set()}
        for track in sorted(OFFICIAL_2026_AWARD_TRACKS)
    }
    for row in connection.execute(
        "SELECT award_track,subject_kind,subject_url FROM award_attributions "
        "WHERE subject_kind IN ('project','firm') AND subject_url IS NOT NULL"
    ):
        direct_urls[str(row["award_track"])][str(row["subject_kind"])].add(
            str(row["subject_url"])
        )
    for row in connection.execute(
        """
        SELECT a.award_track,c.entity_kind,c.url
        FROM award_attribution_companies AS c
        JOIN award_attributions AS a ON a.id=c.attribution_id
        WHERE c.entity_kind IN ('project','firm') AND c.url IS NOT NULL
        """
    ):
        direct_urls[str(row["award_track"])][str(row["entity_kind"])].add(
            str(row["url"])
        )
    recomputed_discovery_counts = {
        track: {
            entity_type: len(urls)
            for entity_type, urls in sorted(entity_sets.items())
        }
        for track, entity_sets in sorted(direct_urls.items())
    }
    compact_track_counts = {
        track: {
            entity_type: count
            for entity_type, count in sorted(entity_counts.items())
            if count
        }
        for track, entity_counts in sorted(recomputed_discovery_counts.items())
    }
    distinct_seed_counts = {
        entity_type: len(
            set().union(
                *(
                    direct_urls[track][entity_type]
                    for track in sorted(direct_urls)
                )
            )
        )
        for entity_type in ("project", "firm")
    }
    if (
        metadata.get("discovery_counts") != recomputed_discovery_counts
        or manifest_summary.get("discovery_counts") != recomputed_discovery_counts
        or run_summary.get("track_direct_link_counts") != compact_track_counts
        or run_summary.get("distinct_project_seed_urls")
        != distinct_seed_counts["project"]
        or run_summary.get("distinct_firm_seed_urls") != distinct_seed_counts["firm"]
    ):
        raise CuratedV2Error(
            "structured awards run4 discovery-count parity mismatch"
        )
    ready_run = _required_mapping(ready.get("recrawl_run"), "recrawl_run")
    if set(ready_run) != {
        "id",
        "run_kind",
        "status",
        "parser_version",
        "started_at",
        "finished_at",
        "source_db_sha256",
        "source_db_size_bytes",
        "summary_sha256",
        "identity_size_bytes",
        "identity_sha256",
    }:
        raise CuratedV2Error("structured awards READY recrawl run contract mismatch")
    run_identity = {
        "id": lineage["recrawl_run_id"],
        "run_kind": lineage["recrawl_run_kind"],
        "status": lineage["recrawl_run_status"],
        "parser_version": lineage["recrawl_parser_version"],
        "started_at": lineage["recrawl_started_at"],
        "finished_at": lineage["recrawl_finished_at"],
        "source_db_sha256": lineage["legacy_source_db_sha256"],
        "source_db_size_bytes": ready_run.get("source_db_size_bytes"),
        "summary_sha256": hashlib.sha256(
            canonical_json(metadata["run_summary"]).encode("utf-8")
        ).hexdigest().upper(),
    }
    if (
        not isinstance(run_identity["source_db_size_bytes"], int)
        or isinstance(run_identity["source_db_size_bytes"], bool)
        or run_identity["source_db_size_bytes"] < 0
    ):
        raise CuratedV2Error("structured awards READY source DB size is invalid")
    if any(ready_run.get(key) != value for key, value in run_identity.items()):
        raise CuratedV2Error("structured awards READY recrawl run lineage mismatch")
    run_identity_bytes = (canonical_json(run_identity) + "\n").encode("utf-8")
    if (
        ready_run.get("identity_size_bytes") != len(run_identity_bytes)
        or ready_run.get("identity_sha256")
        != hashlib.sha256(run_identity_bytes).hexdigest().upper()
    ):
        raise CuratedV2Error("structured awards READY recrawl run identity digest mismatch")

    ready_snapshots = _required_mapping(
        ready.get("snapshot_manifest"), "snapshot_manifest"
    )
    if set(ready_snapshots) != {
        "size_bytes",
        "sha256",
        "page_version_count",
        "distinct_physical_snapshot_count",
    }:
        raise CuratedV2Error("structured awards READY snapshot manifest contract mismatch")
    page_version_count = int(lineage["selected_snapshot_count"])
    distinct_snapshot_count = metadata.get("distinct_physical_snapshot_count")
    snapshot_manifest_size = metadata.get("snapshot_manifest_size_bytes")
    if (
        ready_snapshots.get("sha256") != lineage["snapshot_manifest_sha256"]
        or ready_snapshots.get("page_version_count") != page_version_count
        or ready_snapshots.get("distinct_physical_snapshot_count")
        != distinct_snapshot_count
        or ready_snapshots.get("size_bytes") != snapshot_manifest_size
        or page_version_count != len(pages)
        or page_version_count != 6
        or metadata.get("selected_page_version_count") != len(pages)
        or distinct_snapshot_count != 5
        or not isinstance(distinct_snapshot_count, int)
        or isinstance(distinct_snapshot_count, bool)
        or distinct_snapshot_count <= 0
        or distinct_snapshot_count > page_version_count
        or not isinstance(snapshot_manifest_size, int)
        or isinstance(snapshot_manifest_size, bool)
        or snapshot_manifest_size <= 0
    ):
        raise CuratedV2Error("structured awards READY snapshot manifest mismatch")
    expected_validation = {
        "quick_check": "ok",
        "integrity_check": "ok",
        "foreign_key_violation_count": 0,
    }
    if ready.get("validation") != expected_validation:
        raise CuratedV2Error("structured awards READY validation receipt mismatch")
    try:
        release_contract = validate_awards_release_contract(connection)
    except AwardsStoreBuildError as exc:
        raise CuratedV2Error(
            f"structured awards release contract mismatch: {exc}"
        ) from exc
    return {
        "meta": meta,
        "manifest": dict(manifest),
        "lineage": dict(lineage),
        "record_count": int(count),
        "actual_track_record_counts": actual_track_record_counts,
        "actual_year_record_counts": actual_year_record_counts,
        "ready": ready,
        "ready_identity": {
            "path_label": path_label(ready_path),
            "sha256": sha256_file(ready_path),
            "size_bytes": ready_path.stat().st_size,
        },
        "release_contract": release_contract,
    }


def validate_shared_sidecar_lineage(
    *,
    reconciliation_validation: Mapping[str, Any],
    awards_validation: Mapping[str, Any],
    raw_identity: Mapping[str, Any],
) -> dict[str, Any]:
    """Require reconciliation and awards to derive from one immutable sidecar."""

    reconciliation_sidecar = reconciliation_validation["input_rows"][
        "recrawl_sidecar"
    ]
    reconciliation_meta = json_value(
        reconciliation_sidecar["lineage_json"], None
    )
    awards_lineage = awards_validation["lineage"]
    awards_ready_sidecar = awards_validation["ready"]["input_sidecar"]
    if not isinstance(reconciliation_meta, dict):
        raise CuratedV2Error("reconciliation recrawl sidecar schema lineage missing")
    reconciliation_identity = {
        "schema_version": reconciliation_meta.get("schema_version"),
        "sha256": reconciliation_sidecar["sha256_before"],
        "size_bytes": reconciliation_sidecar["size_bytes"],
    }
    if reconciliation_sidecar["sha256_after"] != reconciliation_identity["sha256"]:
        raise CuratedV2Error("reconciliation recrawl sidecar changed during build")
    awards_identity = {
        "schema_version": awards_ready_sidecar.get("schema_version"),
        "sha256": awards_lineage["sidecar_sha256_before"],
        "size_bytes": awards_lineage["sidecar_size_bytes"],
    }
    if (
        awards_lineage["sidecar_sha256_after"] != awards_identity["sha256"]
        or awards_ready_sidecar.get("sha256_before") != awards_identity["sha256"]
        or awards_ready_sidecar.get("sha256_after") != awards_identity["sha256"]
        or awards_ready_sidecar.get("size_bytes") != awards_identity["size_bytes"]
    ):
        raise CuratedV2Error("structured awards recrawl sidecar lineage is inconsistent")
    if reconciliation_identity != awards_identity:
        raise CuratedV2Error(
            "mixed recrawl sidecar lineage between reconciliation and awards"
        )
    ready_run = awards_validation["ready"]["recrawl_run"]
    if (
        awards_lineage["legacy_source_db_sha256"] != raw_identity["sha256"]
        or ready_run.get("source_db_sha256") != raw_identity["sha256"]
        or ready_run.get("source_db_size_bytes") != raw_identity["size_bytes"]
    ):
        raise CuratedV2Error("structured awards legacy source lineage mismatch")
    return reconciliation_identity


def effective_project_rows(reconciliation: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = [dict(row) for row in reconciliation.execute(
        "SELECT * FROM v_reconciled_projects ORDER BY id,slug"
    )]
    required = ("id", "global_id", "slug", "name", "firm_slug")
    seen_ids: set[int] = set()
    seen_slugs: set[str] = set()
    for row in rows:
        missing = [field for field in required if row.get(field) in (None, "")]
        if missing:
            raise CuratedV2Error(
                f"included reconciled project lacks required fields {missing}: {row.get('source_url')}"
            )
        try:
            project_id = int(row["id"])
        except (TypeError, ValueError) as exc:
            raise CuratedV2Error("reconciled project id is not an integer") from exc
        if project_id in seen_ids or str(row["slug"]) in seen_slugs:
            raise CuratedV2Error("reconciled project id/slug is not unique")
        seen_ids.add(project_id)
        seen_slugs.add(str(row["slug"]))
    return rows


def effective_firm_rows(reconciliation: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = [dict(row) for row in reconciliation.execute(
        "SELECT * FROM v_reconciled_firms ORDER BY slug"
    )]
    seen: set[str] = set()
    for row in rows:
        slug = row.get("slug")
        if not slug or slug in seen:
            raise CuratedV2Error("reconciled firm slug is missing or duplicated")
        seen.add(str(slug))
    return rows


def build_effective_source(
    *,
    raw: sqlite3.Connection,
    reconciliation: sqlite3.Connection,
    awards: sqlite3.Connection,
    target_path: Path,
    reconciliation_id: str,
    deterministic_cutoff: str,
    structured_attribution_ids: set[int],
) -> dict[str, Any]:
    projects = effective_project_rows(reconciliation)
    firms = effective_firm_rows(reconciliation)
    award_projections: dict[tuple[int, int], dict[str, Any]] = {}
    target = sqlite3.connect(target_path)
    direct_firm_count = 0
    deferred_legacy_award_stub_slugs: list[str] = []
    try:
        raw.backup(target)
        target.execute("PRAGMA journal_mode=DELETE")
        target.execute("BEGIN IMMEDIATE")
        target.execute("DELETE FROM architizer_projects")
        target.execute("DELETE FROM architizer_firms")
        target.execute("DELETE FROM pending_projects")
        target.execute("DELETE FROM pending_firms")
        project_columns = (
            "id,global_id,slug,name,firm_slug,firm_name,description,description_short,"
            "completion_year,building_size_slug,building_size_display,constr_status,"
            "budget,location_full,location_country,location_city,categories,"
            "cover_image_url,gallery_image_urls,image_global_ids,published_time,"
            "modified_time,fetched_at"
        )
        placeholders = ",".join("?" for _ in range(23))
        for row in projects:
            target.execute(
                f"INSERT INTO architizer_projects({project_columns}) VALUES ({placeholders})",
                (
                    int(row["id"]),
                    row["global_id"],
                    row["slug"],
                    row["name"],
                    row["firm_slug"],
                    row.get("firm_name"),
                    row.get("description"),
                    row.get("description_short"),
                    row.get("completion_year"),
                    row.get("building_size_slug"),
                    row.get("building_size_display"),
                    row.get("constr_status"),
                    row.get("budget"),
                    row.get("location_full"),
                    row.get("location_country"),
                    row.get("location_city"),
                    canonical_json_text(row.get("categories"), []),
                    row.get("cover_image_url"),
                    canonical_json_text(row.get("gallery_image_urls"), []),
                    canonical_json_text(row.get("image_global_ids"), []),
                    row.get("published_time"),
                    row.get("modified_time"),
                    row.get("fetched_at") or deterministic_cutoff,
                ),
            )
            target.execute(
                "INSERT INTO pending_projects(url,source_url,lastmod,status,discovered_at,fetched_at,error) "
                "VALUES (?,?,?,'done',?,?,NULL)",
                (
                    row["source_url"],
                    f"reconciliation:{reconciliation_id}",
                    row.get("modified_time"),
                    deterministic_cutoff,
                    row.get("fetched_at") or deterministic_cutoff,
                ),
            )
        for row in firms:
            firm_name = row.get("name")
            if firm_name is None or not str(firm_name).strip():
                legacy_award_references = target.execute(
                    "SELECT COUNT(*) FROM architizer_awards WHERE firm_slug=?",
                    (row["slug"],),
                ).fetchone()[0]
                if (
                    row.get("origin") != "baseline_only"
                    or row.get("identity_status") != "baseline_only"
                    or int(legacy_award_references) <= 0
                ):
                    raise CuratedV2Error(
                        "included reconciled firm lacks a source name without "
                        f"legacy award-stub evidence: {row.get('source_url')}"
                    )
                # Do not invent a display name for a legacy award-only stub.  The
                # v1.3 materializer deterministically recreates it from the
                # preserved legacy award row, including its nullable source_name.
                deferred_legacy_award_stub_slugs.append(str(row["slug"]))
                continue
            target.execute(
                """
                INSERT INTO architizer_firms(
                    slug,name,office_locations,description,awards_summary,
                    project_count_seen,social_links,fetched_at
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    row["slug"],
                    firm_name,
                    canonical_json_text(row.get("office_locations"), []),
                    row.get("description"),
                    row.get("awards_summary"),
                    row.get("project_count_seen"),
                    canonical_json_text(row.get("social_links"), {}),
                    row.get("fetched_at") or deterministic_cutoff,
                ),
            )
            direct_firm_count += 1
            target.execute(
                "INSERT INTO pending_firms(url,source_url,lastmod,status,discovered_at,fetched_at,error) "
                "VALUES (?,? ,NULL,'done',?,?,NULL)",
                (
                    row["source_url"],
                    f"reconciliation:{reconciliation_id}",
                    deterministic_cutoff,
                    row.get("fetched_at") or deterministic_cutoff,
                ),
            )
        next_award_id = int(
            target.execute("SELECT COALESCE(MAX(id),0) FROM architizer_awards").fetchone()[0]
        )
        tier_rows: dict[int, list[sqlite3.Row]] = defaultdict(list)
        for tier in awards.execute(
            "SELECT * FROM award_attribution_tiers "
            "WHERE normalized_tier IS NOT NULL ORDER BY attribution_id,position"
        ):
            tier_rows[int(tier["attribution_id"])].append(tier)
        seen_projection_keys: set[str] = set()
        for attribution in awards.execute(
            """
            SELECT a.*,p.parsed_at AS page_parsed_at
            FROM award_attributions a
            JOIN award_page_versions p ON p.id=a.page_version_id
            ORDER BY a.award_year,a.award_track,a.id
            """
        ):
            if int(attribution["id"]) not in structured_attribution_ids:
                continue
            if (
                attribution["parse_status"] != "complete"
                or attribution["subject_kind"] not in {"project", "firm"}
                or not attribution["subject_slug"]
            ):
                continue
            for tier in tier_rows[int(attribution["id"])]:
                projection_key = stable_id(
                    "atzawardv2_",
                    attribution["award_year"],
                    attribution["award_track"],
                    attribution["attribution_global_id"],
                    tier["position"],
                    tier["normalized_tier"],
                    length=28,
                )
                if projection_key in seen_projection_keys:
                    raise CuratedV2Error(
                        f"duplicate structured award projection key: {projection_key}"
                    )
                seen_projection_keys.add(projection_key)
                next_award_id += 1
                target.execute(
                    """
                    INSERT INTO architizer_awards(
                        id,award_year,award_track,award_category,award_tier,
                        project_slug,firm_slug,source_url,fetched_at
                    ) VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        next_award_id,
                        attribution["award_year"],
                        attribution["award_track"],
                        attribution["category_raw"],
                        tier["normalized_tier"],
                        attribution["subject_slug"]
                        if attribution["subject_kind"] == "project"
                        else None,
                        attribution["subject_slug"]
                        if attribution["subject_kind"] == "firm"
                        else None,
                        attribution["source_url"],
                        attribution["page_parsed_at"] or deterministic_cutoff,
                    ),
                )
                award_projections[(int(attribution["id"]), int(tier["position"]))] = {
                    "projection_key": projection_key,
                    "source_award_id": next_award_id,
                }
        target.commit()
        target.execute("VACUUM")
    finally:
        target.close()
    return {
        "project_count": len(projects),
        "firm_count": len(firms),
        "direct_firm_count": direct_firm_count,
        "deferred_legacy_award_stub_count": len(
            deferred_legacy_award_stub_slugs
        ),
        "deferred_legacy_award_stub_slugs": deferred_legacy_award_stub_slugs,
        "sha256": sha256_file(target_path),
        "size_bytes": target_path.stat().st_size,
        "award_projections": award_projections,
    }


def _insert_row(
    output: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
    values: Sequence[Any],
) -> None:
    names = ",".join(f'"{column}"' for column in columns)
    placeholders = ",".join("?" for _ in columns)
    output.execute(
        f'INSERT INTO "{table}"({names}) VALUES ({placeholders})',
        tuple(values),
    )


def _table_columns(
    connection: sqlite3.Connection, table: str, *, schema: str = "main"
) -> list[str]:
    if not table.replace("_", "").isalnum() or not schema.replace("_", "").isalnum():
        raise CuratedV2Error("unsafe SQLite identifier in bulk-copy contract")
    return [
        str(row[1])
        for row in connection.execute(f"PRAGMA {schema}.table_info({table})")
    ]


def _quoted_columns(columns: Sequence[str]) -> str:
    return ",".join('"' + column.replace('"', '""') + '"' for column in columns)


def _copy_full_reconciliation_attached(
    *,
    reconciliation: sqlite3.Connection,
    output: sqlite3.Connection,
    reconciliation_id: str,
) -> dict[str, int]:
    """Bulk-copy the complete frozen plan without Python row round-trips."""

    database_rows = reconciliation.execute("PRAGMA database_list").fetchall()
    source_file = next(
        (str(row[2]) for row in database_rows if str(row[1]) == "main"), ""
    )
    if not source_file:
        raise CuratedV2Error("reconciliation database path is unavailable")
    source_uri = Path(source_file).resolve().as_uri() + "?mode=ro&immutable=1"
    output.commit()
    output.execute("ATTACH DATABASE ? AS reconciliation_source", (source_uri,))
    try:
        source_entity_columns = _table_columns(
            output, "entities", schema="reconciliation_source"
        )
        target_entity_columns = _table_columns(output, "v2_reconciliation_entities")
        expected_entity_columns = [
            source_entity_columns[0],
            "reconciliation_id",
            *source_entity_columns[1:],
        ]
        if target_entity_columns != expected_entity_columns:
            raise CuratedV2Error("reconciliation entity bulk-copy contract mismatch")
        entity_insert_columns = _quoted_columns(target_entity_columns)
        entity_select_columns = _quoted_columns(source_entity_columns)
        source_select = entity_select_columns.replace(
            '"entity_key",', '"entity_key",?,' , 1
        )
        output.execute(
            "INSERT INTO v2_reconciliation_entities"
            f"({entity_insert_columns}) "
            f"SELECT {source_select} FROM reconciliation_source.entities "
            "ORDER BY entity_key",
            (reconciliation_id,),
        )

        copy_tables = (
            (
                "source_target_reasons",
                "v2_reconciliation_target_reasons",
                "target_url,reason,discovery_source",
            ),
            (
                "field_candidates",
                "v2_reconciliation_field_candidates",
                "candidate_id",
            ),
            (
                "entity_aliases",
                "v2_reconciliation_entity_aliases",
                "entity_key,target_url",
            ),
            (
                "field_decisions",
                "v2_reconciliation_field_decisions",
                "entity_key,field_name",
            ),
            (
                "field_conflicts",
                "v2_reconciliation_field_conflicts",
                "conflict_id",
            ),
            (
                "qa_issues",
                "v2_reconciliation_qa_issues",
                "qa_issue_id",
            ),
            (
                "field_lineage",
                "v2_reconciliation_field_lineage",
                "entity_key,field_name,candidate_id",
            ),
            (
                "reconciliation_metrics",
                "v2_reconciliation_metrics",
                "metric_name",
            ),
        )
        for source_table, target_table, order_by in copy_tables:
            source_columns = _table_columns(
                output, source_table, schema="reconciliation_source"
            )
            target_columns = _table_columns(output, target_table)
            if source_columns != target_columns:
                raise CuratedV2Error(
                    f"reconciliation bulk-copy contract mismatch: {source_table}"
                )
            columns_sql = _quoted_columns(source_columns)
            output.execute(
                f'INSERT INTO "{target_table}"({columns_sql}) '
                f'SELECT {columns_sql} FROM reconciliation_source."{source_table}" '
                f"ORDER BY {order_by}"
            )
        output.commit()
    finally:
        if output.in_transaction:
            output.rollback()
        output.execute("DETACH DATABASE reconciliation_source")

    return {
        "entity_count": int(
            output.execute(
                "SELECT COUNT(*) FROM v2_reconciliation_entities"
            ).fetchone()[0]
        ),
        "alias_count": int(
            output.execute(
                "SELECT COUNT(*) FROM v2_reconciliation_entity_aliases"
            ).fetchone()[0]
        ),
        "candidate_count": int(
            output.execute(
                "SELECT COUNT(*) FROM v2_reconciliation_field_candidates"
            ).fetchone()[0]
        ),
        "conflict_count": int(
            output.execute(
                "SELECT COUNT(*) FROM v2_reconciliation_field_conflicts"
            ).fetchone()[0]
        ),
        "target_reason_count": int(
            output.execute(
                "SELECT COUNT(*) FROM v2_reconciliation_target_reasons"
            ).fetchone()[0]
        ),
    }


def _selected_reconciliation_entity_keys(
    reconciliation: sqlite3.Connection,
    output: sqlite3.Connection,
    *,
    full: bool,
) -> set[str]:
    if full:
        return {
            str(row[0])
            for row in reconciliation.execute("SELECT entity_key FROM entities")
        }
    project_urls = {
        str(row[0])
        for row in output.execute("SELECT source_url FROM source_projects")
    }
    firm_slugs = {
        str(row[0])
        for row in output.execute("SELECT source_firm_slug FROM source_firms")
    }
    return {
        str(row["entity_key"])
        for row in reconciliation.execute(
            "SELECT entity_key,entity_type,source_url,source_slug FROM entities"
        )
        if (
            row["entity_type"] == "project" and row["source_url"] in project_urls
        )
        or (row["entity_type"] == "firm" and row["source_slug"] in firm_slugs)
    }


def copy_reconciliation_evidence(
    *,
    reconciliation: sqlite3.Connection,
    output: sqlite3.Connection,
    materialization_id: str,
    full: bool,
) -> dict[str, int]:
    run = one_row(reconciliation, "reconciliation_runs")
    run_columns = [
        "reconciliation_id",
        "materialization_id",
        "tool_version",
        "plan_schema_version",
        "policy_version",
        "baseline_schema_version",
        "final_target_schema_version",
        "artifact_kind",
        "publication_eligibility",
        "project_limit",
        "firm_limit",
        "selected_project_count",
        "selected_firm_count",
        "pending_target_count",
        "deterministic_cutoff",
        "validation_json",
    ]
    _insert_row(
        output,
        "v2_reconciliation_runs",
        run_columns,
        (
            run["reconciliation_id"],
            materialization_id,
            *(run[column] for column in run_columns[2:]),
        ),
    )
    for row in reconciliation.execute("SELECT * FROM input_snapshots ORDER BY input_role"):
        columns = list(row.keys())
        _insert_row(output, "v2_reconciliation_input_snapshots", columns, row)
    trusted = one_row(reconciliation, "trusted_input_manifest")
    _insert_row(
        output,
        "v2_reconciliation_trusted_manifest",
        list(trusted.keys()),
        trusted,
    )
    for row in reconciliation.execute(
        "SELECT * FROM baseline_contract_objects ORDER BY object_type,object_name"
    ):
        _insert_row(
            output,
            "v2_reconciliation_baseline_contract",
            list(row.keys()),
            row,
        )

    if full:
        return _copy_full_reconciliation_attached(
            reconciliation=reconciliation,
            output=output,
            reconciliation_id=str(run["reconciliation_id"]),
        )

    entity_keys = _selected_reconciliation_entity_keys(
        reconciliation,
        output,
        full=full,
    )
    entity_rows = [
        row
        for row in reconciliation.execute("SELECT * FROM entities ORDER BY entity_key")
        if row["entity_key"] in entity_keys
    ]
    for row in entity_rows:
        columns = ["entity_key", "reconciliation_id", *list(row.keys())[1:]]
        _insert_row(
            output,
            "v2_reconciliation_entities",
            columns,
            (row["entity_key"], run["reconciliation_id"], *tuple(row)[1:]),
        )

    target_reason_count = 0
    for row in reconciliation.execute(
        "SELECT * FROM source_target_reasons "
        "ORDER BY target_url,reason,discovery_source"
    ):
        if not full and (
            row["entity_key"] is None or row["entity_key"] not in entity_keys
        ):
            continue
        _insert_row(
            output,
            "v2_reconciliation_target_reasons",
            list(row.keys()),
            row,
        )
        target_reason_count += 1

    candidate_count = 0
    for row in reconciliation.execute(
        "SELECT * FROM field_candidates ORDER BY candidate_id"
    ):
        if row["entity_key"] not in entity_keys:
            continue
        _insert_row(
            output,
            "v2_reconciliation_field_candidates",
            list(row.keys()),
            row,
        )
        candidate_count += 1
    for source_table, target_table, order_by in (
        ("entity_aliases", "v2_reconciliation_entity_aliases", "entity_key,target_url"),
        ("field_decisions", "v2_reconciliation_field_decisions", "entity_key,field_name"),
        ("field_conflicts", "v2_reconciliation_field_conflicts", "conflict_id"),
        ("qa_issues", "v2_reconciliation_qa_issues", "qa_issue_id"),
    ):
        for row in reconciliation.execute(
            f'SELECT * FROM "{source_table}" ORDER BY {order_by}'
        ):
            if row["entity_key"] not in entity_keys:
                continue
            _insert_row(output, target_table, list(row.keys()), row)
    for row in reconciliation.execute(
        "SELECT * FROM field_lineage ORDER BY entity_key,field_name,candidate_id"
    ):
        if row["entity_key"] not in entity_keys:
            continue
        try:
            _insert_row(
                output,
                "v2_reconciliation_field_lineage",
                list(row.keys()),
                row,
            )
        except sqlite3.IntegrityError as exc:
            raise CuratedV2Error(
                "selected reconciliation lineage lost its candidate"
            ) from exc
    for row in reconciliation.execute(
        "SELECT * FROM reconciliation_metrics ORDER BY metric_name"
    ):
        _insert_row(
            output,
            "v2_reconciliation_metrics",
            list(row.keys()),
            row,
        )
    return {
        "entity_count": len(entity_rows),
        "alias_count": output.execute(
            "SELECT COUNT(*) FROM v2_reconciliation_entity_aliases"
        ).fetchone()[0],
        "candidate_count": candidate_count,
        "conflict_count": output.execute(
            "SELECT COUNT(*) FROM v2_reconciliation_field_conflicts"
        ).fetchone()[0],
        "target_reason_count": target_reason_count,
    }


def select_award_rows(
    awards: sqlite3.Connection,
    limit: Optional[int],
) -> list[sqlite3.Row]:
    rows = awards.execute(
        "SELECT * FROM award_attributions "
        "ORDER BY award_track,source_group_ordinal,source_card_ordinal,selection_order"
    ).fetchall()
    if limit is None:
        return sorted(rows, key=lambda row: int(row["selection_order"]))
    groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        groups[str(row["award_track"])].append(row)
    positions = defaultdict(int)
    selected: list[sqlite3.Row] = []
    while len(selected) < limit:
        advanced = False
        for track in sorted(groups, key=str.casefold):
            position = positions[track]
            if position >= len(groups[track]):
                continue
            selected.append(groups[track][position])
            positions[track] += 1
            advanced = True
            if len(selected) == limit:
                break
        if not advanced:
            break
    if len(selected) != limit:
        raise CuratedV2Error(
            f"structured awards cannot satisfy {limit}-record smoke: found {len(rows)}"
        )
    return selected


def _award_link_status(
    *,
    kind: Optional[str],
    slug: Optional[str],
    project_ids: Mapping[str, int],
    firm_slugs: set[str],
    reconciliation_project_slugs: set[str],
    reconciliation_firm_slugs: set[str],
    full: bool,
) -> tuple[str, Optional[int], Optional[str]]:
    if kind in {"product", "brand"}:
        return "source_only", None, None
    if not kind or not slug:
        return "missing_identity", None, None
    if kind == "project":
        if slug in project_ids:
            return "resolved", project_ids[slug], None
        if not full and slug in reconciliation_project_slugs:
            return "not_in_materialization_subset", None, None
        return "unresolved", None, None
    if kind == "firm":
        if slug in firm_slugs:
            return "resolved", None, slug
        if not full and slug in reconciliation_firm_slugs:
            return "not_in_materialization_subset", None, None
        return "unresolved", None, None
    return "unresolved", None, None


def copy_structured_awards(
    *,
    awards: sqlite3.Connection,
    reconciliation: sqlite3.Connection,
    output: sqlite3.Connection,
    selected_rows: Sequence[sqlite3.Row],
    base_award_projections: Mapping[tuple[int, int], Mapping[str, Any]],
    full: bool,
) -> dict[str, Any]:
    lineage = dict(one_row(awards, "input_lineage"))
    manifest = dict(one_row(awards, "build_manifest"))
    meta = dict(awards.execute("SELECT key,value FROM schema_meta"))
    output.execute(
        "INSERT INTO v2_structured_award_lineage VALUES (1,?,?,?)",
        (
            canonical_json(lineage),
            canonical_json(manifest),
            canonical_json(meta),
        ),
    )
    selected_ids = {int(row["id"]) for row in selected_rows}
    page_counts = Counter(int(row["page_version_id"]) for row in selected_rows)
    page_columns = [
        "source_page_id",
        "page_kind",
        "award_year",
        "award_track",
        "requested_url",
        "final_url",
        "final_url_policy",
        "http_status",
        "content_type",
        "response_bytes",
        "snapshot_content_sha256",
        "snapshot_gzip_sha256",
        "snapshot_gzip_path",
        "snapshot_gzip_bytes",
        "parser_version",
        "parsed_at",
        "parse_status",
        "source_record_count",
        "source_selected_record_count",
        "materialized_record_count",
        "status_counts_json",
        "duplicate_attribution_ids_json",
    ]
    for row in awards.execute("SELECT * FROM award_page_versions ORDER BY id"):
        _insert_row(
            output,
            "v2_structured_award_pages",
            page_columns,
            (
                row["id"],
                row["page_kind"],
                row["award_year"],
                row["award_track"],
                row["requested_url"],
                row["final_url"],
                row["final_url_policy"],
                row["http_status"],
                row["content_type"],
                row["response_bytes"],
                row["snapshot_content_sha256"],
                row["snapshot_gzip_sha256"],
                row["snapshot_gzip_path"],
                row["snapshot_gzip_bytes"],
                row["parser_version"],
                row["parsed_at"],
                row["parse_status"],
                row["source_record_count"],
                row["selected_record_count"],
                page_counts[int(row["id"])],
                row["status_counts_json"],
                row["duplicate_attribution_ids_json"],
            ),
        )
    attribution_columns = [
        "source_attribution_id",
        "source_page_id",
        "materialization_order",
        "source_selection_order",
        "source_group_ordinal",
        "source_card_ordinal",
        "award_year",
        "award_track",
        "attribution_pk",
        "attribution_global_id",
        "category_raw",
        "category_path_json",
        "subject_kind",
        "subject_slug",
        "subject_name",
        "subject_url",
        "description_raw",
        "image_url_resolved",
        "parse_status",
        "missing_json",
        "conflicts_json",
        "warnings_json",
        "raw_attributes_json",
        "dom_values_json",
        "source_url",
    ]
    for order, row in enumerate(selected_rows, 1):
        _insert_row(
            output,
            "v2_structured_award_attributions",
            attribution_columns,
            (
                row["id"],
                row["page_version_id"],
                order,
                row["selection_order"],
                row["source_group_ordinal"],
                row["source_card_ordinal"],
                row["award_year"],
                row["award_track"],
                row["attribution_pk"],
                row["attribution_global_id"],
                row["category_raw"],
                row["category_path_json"],
                row["subject_kind"],
                row["subject_slug"],
                row["subject_name"],
                row["subject_url"],
                row["description_raw"],
                row["image_url_resolved"],
                row["parse_status"],
                row["missing_json"],
                row["conflicts_json"],
                row["warnings_json"],
                row["raw_attributes_json"],
                row["dom_values_json"],
                row["source_url"],
            ),
        )
    for source_table, target_table, order_by in (
        ("award_attribution_tiers", "v2_structured_award_tiers", "attribution_id,position"),
        ("award_attribution_companies", "v2_structured_award_companies", "attribution_id,position"),
    ):
        for row in awards.execute(f'SELECT * FROM "{source_table}" ORDER BY {order_by}'):
            if int(row["attribution_id"]) not in selected_ids:
                continue
            columns = [
                "source_attribution_id" if key == "attribution_id" else key
                for key in row.keys()
            ]
            _insert_row(output, target_table, columns, row)
    for row in awards.execute(
        "SELECT * FROM corpus_projection_policy ORDER BY entity_kind"
    ):
        _insert_row(
            output,
            "v2_structured_award_projection_policy",
            list(row.keys()),
            row,
        )

    project_ids = {
        str(row["slug"]): int(row["source_project_id"])
        for row in output.execute("SELECT source_project_id,slug FROM source_projects")
    }
    firm_slugs = {
        str(row[0])
        for row in output.execute("SELECT source_firm_slug FROM source_firms")
    }
    reconciliation_project_slugs = {
        str(row[0])
        for row in reconciliation.execute(
            "SELECT source_slug FROM entities "
            "WHERE entity_type='project' AND inclusion_status='included'"
        )
    }
    reconciliation_firm_slugs = {
        str(row[0])
        for row in reconciliation.execute(
            "SELECT source_slug FROM entities "
            "WHERE entity_type='firm' AND inclusion_status='included'"
        )
    }
    link_counts: Counter[str] = Counter()
    selected_by_id = {int(row["id"]): row for row in selected_rows}
    company_rows: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in awards.execute(
        "SELECT * FROM award_attribution_companies ORDER BY attribution_id,position"
    ):
        if int(row["attribution_id"]) in selected_ids:
            company_rows[int(row["attribution_id"])].append(row)
    for attribution_id in sorted(selected_by_id):
        attribution = selected_by_id[attribution_id]
        relations: list[tuple[str, int, Optional[str], Optional[str], Optional[str], Mapping[str, Any]]] = [
            (
                "subject",
                0,
                attribution["subject_kind"],
                attribution["subject_slug"],
                attribution["subject_url"],
                {
                    "name": attribution["subject_name"],
                    "parse_status": attribution["parse_status"],
                },
            )
        ]
        relations.extend(
            (
                "company",
                int(company["position"]),
                company["entity_kind"],
                company["slug"],
                company["url"],
                {
                    "name": company["name"],
                    "parse_status": company["parse_status"],
                },
            )
            for company in company_rows[attribution_id]
        )
        for role, position, kind, slug, raw_url, evidence in relations:
            status, project_id, firm_slug = _award_link_status(
                kind=kind,
                slug=slug,
                project_ids=project_ids,
                firm_slugs=firm_slugs,
                reconciliation_project_slugs=reconciliation_project_slugs,
                reconciliation_firm_slugs=reconciliation_firm_slugs,
                full=full,
            )
            output.execute(
                """
                INSERT INTO v2_structured_award_entity_links(
                    source_attribution_id,relation_role,position,entity_kind,
                    raw_slug,raw_url,resolved_source_project_id,
                    resolved_source_firm_slug,link_status,evidence_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    attribution_id,
                    role,
                    position,
                    kind,
                    slug,
                    raw_url,
                    project_id,
                    firm_slug,
                    status,
                    canonical_json(evidence),
                ),
            )
            link_counts[status] += 1
    subject_counts = Counter(
        str(row["subject_kind"] or "unresolved") for row in selected_rows
    )
    company_counts = Counter(
        str(row["entity_kind"] or "unresolved")
        for rows in company_rows.values()
        for row in rows
    )
    projected_source_award_ids = {
        int(row[0]) for row in output.execute("SELECT source_award_id FROM source_awards")
    }
    projection_counts: Counter[str] = Counter()
    selected_tiers = awards.execute(
        "SELECT * FROM award_attribution_tiers ORDER BY attribution_id,position"
    ).fetchall()
    for tier in selected_tiers:
        attribution_id = int(tier["attribution_id"])
        if attribution_id not in selected_ids:
            continue
        attribution = selected_by_id[attribution_id]
        projection = base_award_projections.get(
            (attribution_id, int(tier["position"]))
        )
        kind = attribution["subject_kind"]
        slug = attribution["subject_slug"]
        projected_id: Optional[int] = None
        projection_key: Optional[str] = None
        if kind in {"product", "brand"}:
            status = "source_only"
        elif attribution["parse_status"] != "complete":
            status = "conflict_or_partial"
        elif kind not in {"project", "firm"} or not slug:
            status = "missing_identity"
        elif projection is None:
            raise CuratedV2Error(
                "complete project/firm award tier lacks a deterministic base projection"
            )
        else:
            projection_key = str(projection["projection_key"])
            candidate_id = int(projection["source_award_id"])
            if candidate_id in projected_source_award_ids:
                status = "projected"
                projected_id = candidate_id
            else:
                status = "not_in_materialization_subset"
        output.execute(
            """
            INSERT INTO v2_structured_award_base_projections(
                source_attribution_id,tier_position,projection_key,
                projected_source_award_id,projection_status,policy_version,
                evidence_json
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (
                attribution_id,
                tier["position"],
                projection_key,
                projected_id,
                status,
                MATERIALIZATION_POLICY_VERSION,
                canonical_json(
                    {
                        "award_year": attribution["award_year"],
                        "award_track": attribution["award_track"],
                        "attribution_global_id": attribution["attribution_global_id"],
                        "subject_kind": kind,
                        "subject_slug": slug,
                        "tier": tier["normalized_tier"],
                        "tier_parse_status": tier["parse_status"],
                    }
                ),
            ),
        )
        projection_counts[status] += 1
    return {
        "selected_award_count": len(selected_rows),
        "subject_counts": dict(sorted(subject_counts.items())),
        "company_counts": dict(sorted(company_counts.items())),
        "link_counts": dict(sorted(link_counts.items())),
        "base_projection_counts": dict(sorted(projection_counts.items())),
    }


def _group_counts(
    connection: sqlite3.Connection,
    query: str,
) -> dict[str, int]:
    return {
        ":".join(str(value) for value in tuple(row)[:-1]): int(tuple(row)[-1])
        for row in connection.execute(query)
    }


def _coverage_summary(connection: sqlite3.Connection) -> dict[str, Any]:
    fields = (
        "has_firm",
        "has_location",
        "has_confirmed_year",
        "has_description",
        "has_category",
        "has_image",
    )
    row = connection.execute(
        "SELECT COUNT(*) AS n,"
        + ",".join(f"SUM({field}) AS {field}" for field in fields)
        + ",AVG(completeness_score) AS avg_score FROM project_completeness"
    ).fetchone()
    total = int(row["n"])
    counts = {field: int(row[field] or 0) for field in fields}
    return {
        "project_count": total,
        "counts": counts,
        "rates": {
            field: (counts[field] / total if total else None) for field in fields
        },
        "average_completeness_score": (
            float(row["avg_score"]) if row["avg_score"] is not None else None
        ),
    }


PROJECT_CORE_FIELD_PREDICATES: Mapping[str, tuple[str, str]] = {
    "project_id": (
        "source_project_id is present",
        "source_project_id IS NOT NULL",
    ),
    "global_id": ("global_id is nonempty", "trim(global_id)!=''"),
    "slug": ("slug is nonempty", "trim(slug)!=''"),
    "name": ("name is nonempty", "trim(name)!=''"),
    "firm_relation": (
        "source_firm_slug is nonempty",
        "trim(source_firm_slug)!=''",
    ),
    "location": (
        "at least one full/country/city location field is nonempty",
        "trim(COALESCE(location_full,''))!='' OR "
        "trim(COALESCE(location_country_raw,''))!='' OR "
        "trim(COALESCE(location_city_raw,''))!=''",
    ),
    "completion_year": (
        "completion_year_raw is present",
        "completion_year_raw IS NOT NULL",
    ),
    "construction_status": (
        "constr_status_raw is nonempty",
        "trim(COALESCE(constr_status_raw,''))!=''",
    ),
    "size_bucket": (
        "building size slug or display is nonempty",
        "trim(COALESCE(building_size_slug,''))!='' OR "
        "trim(COALESCE(building_size_display,''))!=''",
    ),
    "description_full": (
        "description is nonempty",
        "trim(COALESCE(description,''))!=''",
    ),
    "description_short": (
        "description_short is nonempty",
        "trim(COALESCE(description_short,''))!=''",
    ),
    "category_tag": (
        "at least one parsed source category/tag occurrence exists",
        "category_occurrence_count>0",
    ),
    "cover_image": (
        "at least one non-malformed cover image occurrence exists",
        "EXISTS (SELECT 1 FROM source_image_occurrences AS i "
        "WHERE i.source_project_id=source_projects.source_project_id "
        "AND i.role='cover' AND i.parse_status!='malformed')",
    ),
    "gallery_images": (
        "at least one gallery occurrence exists",
        "gallery_occurrence_count>0",
    ),
    "image_global_ids": (
        "at least one image global-ID occurrence exists",
        "image_global_id_occurrence_count>0",
    ),
    "published_time": (
        "published_time is nonempty",
        "trim(COALESCE(published_time,''))!=''",
    ),
    "modified_time": (
        "modified_time is nonempty",
        "trim(COALESCE(modified_time,''))!=''",
    ),
}

FIRM_CORE_FIELD_PREDICATES: Mapping[str, tuple[str, str]] = {
    "slug": ("source_firm_slug is nonempty", "trim(source_firm_slug)!=''"),
    "name": ("source_name is nonempty", "trim(COALESCE(source_name,''))!=''"),
    "office_locations": (
        "at least one parsed office location occurrence exists",
        "EXISTS (SELECT 1 FROM firm_office_occurrences AS o "
        "WHERE o.source_firm_slug=source_firms.source_firm_slug "
        "AND o.parse_status='parsed')",
    ),
    "description": (
        "description is nonempty",
        "trim(COALESCE(description,''))!=''",
    ),
    "awards_summary": (
        "awards_summary is nonempty",
        "trim(COALESCE(awards_summary,''))!=''",
    ),
    "project_count_seen": (
        "project_count_seen is present",
        "project_count_seen IS NOT NULL",
    ),
    "social_links": (
        "at least one source social link exists",
        "EXISTS (SELECT 1 FROM firm_social_links AS s "
        "WHERE s.source_firm_slug=source_firms.source_firm_slug)",
    ),
    "source_url": ("source_url is nonempty", "trim(source_url)!=''"),
    "fetched_at": (
        "fetched_at is nonempty",
        "trim(COALESCE(fetched_at,''))!=''",
    ),
}


def _core_field_snapshot(
    connection: sqlite3.Connection,
    *,
    table: str,
    predicates: Mapping[str, tuple[str, str]],
) -> dict[str, Any]:
    total = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    fields: dict[str, Any] = {}
    for field, (definition, predicate) in predicates.items():
        count = int(
            connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE ({predicate})"
            ).fetchone()[0]
        )
        fields[field] = {
            "definition": definition,
            "count": count,
            "rate": count / total if total else None,
        }
    return {"entity_count": total, "fields": fields}


def _compare_core_field_coverage(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    comparison_status: str,
    full: bool,
) -> dict[str, Any]:
    return {
        "comparison_status": comparison_status,
        "entity_count_before": before["entity_count"],
        "entity_count_after": after["entity_count"],
        "fields": {
            field: {
                "definition": before["fields"][field]["definition"],
                "before_count": before["fields"][field]["count"],
                "before_rate": before["fields"][field]["rate"],
                "after_count": after["fields"][field]["count"],
                "after_rate": after["fields"][field]["rate"],
                "count_delta": (
                    after["fields"][field]["count"]
                    - before["fields"][field]["count"]
                    if full
                    else None
                ),
                "rate_delta": (
                    after["fields"][field]["rate"]
                    - before["fields"][field]["rate"]
                    if full
                    and before["fields"][field]["rate"] is not None
                    and after["fields"][field]["rate"] is not None
                    else None
                ),
            }
            for field in predicates_sorted(before["fields"])
        },
    }


def predicates_sorted(fields: Mapping[str, Any]) -> list[str]:
    return sorted(str(field) for field in fields)


def compute_operational_metrics(
    *,
    baseline: sqlite3.Connection,
    output: sqlite3.Connection,
    reconciliation: sqlite3.Connection,
    full: bool,
) -> dict[str, Any]:
    """Compute named before/after facts without assigning ambiguous semantics."""

    comparison_status = "comparable_full" if full else "not_comparable_smoke"
    stub_origins = ("project_stub", "award_stub")
    baseline_stubs = {
        origin: {
            str(row[0])
            for row in baseline.execute(
                "SELECT source_firm_slug FROM source_firms WHERE record_origin=?",
                (origin,),
            )
        }
        for origin in stub_origins
    }
    final_stubs = {
        origin: {
            str(row[0])
            for row in output.execute(
                "SELECT source_firm_slug FROM source_firms WHERE record_origin=?",
                (origin,),
            )
        }
        for origin in stub_origins
    }
    baseline_stub_union = set().union(*baseline_stubs.values())
    final_stub_union = set().union(*final_stubs.values())
    final_crawled = {
        str(row[0])
        for row in output.execute(
            "SELECT source_firm_slug FROM source_firms WHERE record_origin='crawled'"
        )
    }
    firm_stub = {
        "definition": "source_firms rows whose record_origin is project_stub or award_stub, split by discovery relation and also counted as a slug union",
        "comparison_status": comparison_status,
        "before": {
            **{origin: len(baseline_stubs[origin]) for origin in stub_origins},
            "total_union": len(baseline_stub_union),
        },
        "after": {
            **{origin: len(final_stubs[origin]) for origin in stub_origins},
            "total_union": len(final_stub_union),
        },
        "net_decrease": (
            {
                **{
                    origin: len(baseline_stubs[origin]) - len(final_stubs[origin])
                    for origin in stub_origins
                },
                "total_union": len(baseline_stub_union) - len(final_stub_union),
            }
            if full
            else None
        ),
        "promoted_to_crawled": {
            **{
                f"from_{origin}": len(baseline_stubs[origin] & final_crawled)
                for origin in stub_origins
            },
            "total_union": len(baseline_stub_union & final_crawled),
        },
        "new_stubs": {
            **{
                origin: len(final_stubs[origin] - baseline_stub_union)
                for origin in stub_origins
            },
            "total_union": len(final_stub_union - baseline_stub_union),
        },
    }

    def legacy_link_rows(
        connection: sqlite3.Connection, *, legacy_year_filter: bool
    ) -> list[sqlite3.Row]:
        year_clause = "AND a.award_year<=2025" if legacy_year_filter else ""
        return connection.execute(
            """
            SELECT a.source_composite_key,l.target_type,l.raw_slug,l.link_status
            FROM award_entity_links AS l
            JOIN source_awards AS a USING(source_award_id)
            WHERE 1=1
            """
            + year_clause
            + " ORDER BY a.source_composite_key,l.target_type,l.raw_slug,l.source_award_id"
        ).fetchall()

    baseline_link_rows = legacy_link_rows(baseline, legacy_year_filter=False)
    final_link_rows = legacy_link_rows(output, legacy_year_filter=True)

    def link_key(row: sqlite3.Row) -> tuple[str, str, str]:
        return (
            str(row["source_composite_key"]),
            str(row["target_type"]),
            str(row["raw_slug"]),
        )

    def unresolved(rows: Sequence[sqlite3.Row]) -> list[sqlite3.Row]:
        return [row for row in rows if row["link_status"] in {"unresolved", "stub_only"}]

    baseline_unresolved = unresolved(baseline_link_rows)
    final_unresolved = unresolved(final_link_rows)
    baseline_unresolved_keys = {link_key(row) for row in baseline_unresolved}
    final_unresolved_keys = {link_key(row) for row in final_unresolved}
    final_statuses: defaultdict[tuple[str, str, str], set[str]] = defaultdict(set)
    for row in final_link_rows:
        final_statuses[link_key(row)].add(str(row["link_status"]))
    resolved_transitions = {
        key
        for key in baseline_unresolved_keys
        if final_statuses.get(key) == {"resolved"}
    }
    missing_after = {
        key for key in baseline_unresolved_keys if key not in final_statuses
    }
    structured_rows = output.execute(
        "SELECT entity_kind,raw_slug,link_status "
        "FROM v2_structured_award_entity_links "
        "WHERE entity_kind IN ('project','firm') AND link_status!='resolved'"
    ).fetchall()
    award_unresolved = {
        "definition": "legacy link rows with status unresolved or stub_only; stable transition key is (source_composite_key,target_type,raw_slug), while row and distinct target-slug counts remain separate; structured 2026 project/firm relations are reported separately",
        "comparison_status": comparison_status,
        "legacy_link_rows_before": len(baseline_unresolved),
        "legacy_link_rows_after": len(final_unresolved),
        "legacy_link_rows_net_decrease": (
            len(baseline_unresolved) - len(final_unresolved) if full else None
        ),
        "legacy_distinct_target_slugs_before": len(
            {(str(row["target_type"]), str(row["raw_slug"])) for row in baseline_unresolved}
        ),
        "legacy_distinct_target_slugs_after": len(
            {(str(row["target_type"]), str(row["raw_slug"])) for row in final_unresolved}
        ),
        "legacy_stable_unresolved_keys_before": len(baseline_unresolved_keys),
        "legacy_stable_unresolved_keys_after": len(final_unresolved_keys),
        "legacy_resolved_transition_count": len(resolved_transitions),
        "legacy_still_unresolved_key_count": len(
            baseline_unresolved_keys & final_unresolved_keys
        ),
        "legacy_missing_after_key_count": len(missing_after),
        "legacy_newly_unresolved_key_count": len(
            final_unresolved_keys - baseline_unresolved_keys
        ),
        "structured_2026_project_firm_unresolved_link_rows": len(structured_rows),
        "structured_2026_project_firm_unresolved_distinct_target_slugs": len(
            {
                (str(row["entity_kind"]), str(row["raw_slug"]))
                for row in structured_rows
            }
        ),
    }

    before_coverage = _coverage_summary(baseline)
    after_coverage = _coverage_summary(output)
    coverage_delta = None
    if full:
        coverage_delta = {
            field: (
                after_coverage["rates"][field] - before_coverage["rates"][field]
                if before_coverage["rates"][field] is not None
                and after_coverage["rates"][field] is not None
                else None
            )
            for field in after_coverage["rates"]
        }
        coverage_delta["average_completeness_score"] = (
            after_coverage["average_completeness_score"]
            - before_coverage["average_completeness_score"]
            if before_coverage["average_completeness_score"] is not None
            and after_coverage["average_completeness_score"] is not None
            else None
        )
    field_coverage = {
        "definition": "legacy six-field project_completeness summary plus per-field nonempty counts/rates for required project parser fields and core firm fields",
        "comparison_status": comparison_status,
        "before": before_coverage,
        "after": after_coverage,
        "rate_delta": coverage_delta,
        "project_core_fields": _compare_core_field_coverage(
            _core_field_snapshot(
                baseline,
                table="source_projects",
                predicates=PROJECT_CORE_FIELD_PREDICATES,
            ),
            _core_field_snapshot(
                output,
                table="source_projects",
                predicates=PROJECT_CORE_FIELD_PREDICATES,
            ),
            comparison_status=comparison_status,
            full=full,
        ),
        "firm_core_fields": _compare_core_field_coverage(
            _core_field_snapshot(
                baseline,
                table="source_firms",
                predicates=FIRM_CORE_FIELD_PREDICATES,
            ),
            _core_field_snapshot(
                output,
                table="source_firms",
                predicates=FIRM_CORE_FIELD_PREDICATES,
            ),
            comparison_status=comparison_status,
            full=full,
        ),
    }

    taxonomy_before = _group_counts(
        baseline,
        "SELECT axis,status,COUNT(*) FROM attribute_claims "
        "GROUP BY axis,status ORDER BY axis,status",
    )
    taxonomy_after = _group_counts(
        output,
        "SELECT axis,status,COUNT(*) FROM attribute_claims "
        "GROUP BY axis,status ORDER BY axis,status",
    )
    taxonomy = {
        "definition": "attribute_claims row counts grouped by axis and status; count movement is not evidence of semantic improvement",
        "comparison_status": comparison_status,
        "before": taxonomy_before,
        "after": taxonomy_after,
        "delta": (
            {
                key: taxonomy_after.get(key, 0) - taxonomy_before.get(key, 0)
                for key in sorted(set(taxonomy_before) | set(taxonomy_after))
            }
            if full
            else None
        ),
    }

    duplicate_before = _group_counts(
        baseline,
        "SELECT candidate_kind,decision_status,COUNT(*) FROM duplicate_candidates "
        "GROUP BY candidate_kind,decision_status ORDER BY candidate_kind,decision_status",
    )
    duplicate_after = _group_counts(
        output,
        "SELECT candidate_kind,decision_status,COUNT(*) FROM duplicate_candidates "
        "GROUP BY candidate_kind,decision_status ORDER BY candidate_kind,decision_status",
    )
    baseline_candidate_ids = {
        str(row[0]) for row in baseline.execute("SELECT candidate_id FROM duplicate_candidates")
    }
    final_candidate_ids = {
        str(row[0]) for row in output.execute("SELECT candidate_id FROM duplicate_candidates")
    }
    duplicates = {
        "definition": "duplicate_candidates counts grouped by candidate_kind and decision_status; added/removed IDs require manual review",
        "comparison_status": comparison_status,
        "before": duplicate_before,
        "after": duplicate_after,
        "delta": (
            {
                key: duplicate_after.get(key, 0) - duplicate_before.get(key, 0)
                for key in sorted(set(duplicate_before) | set(duplicate_after))
            }
            if full
            else None
        ),
        "candidate_ids_added": len(final_candidate_ids - baseline_candidate_ids),
        "candidate_ids_removed": len(baseline_candidate_ids - final_candidate_ids),
    }
    recovery_row = reconciliation.execute(
        "SELECT metric_value_json FROM reconciliation_metrics "
        "WHERE metric_name='source_recovery_counts'"
    ).fetchone()
    source_recovery = json_value(recovery_row[0], {}) if recovery_row else {}
    return {
        "comparison_status": comparison_status,
        "firm_stub_decrease": firm_stub,
        "award_unresolved_decrease": award_unresolved,
        "field_coverage_change": field_coverage,
        "taxonomy_claim_change": taxonomy,
        "duplicate_candidate_change": duplicates,
        "source_recovery": source_recovery,
        "open_qa": [
            "taxonomy claim count changes do not establish a change in source meaning",
            "duplicate candidate changes require pair-level review before interpretation",
            "structured award unresolved counts are relations, not unique entities",
            "gallery image semantics are not inferred from article or category context",
        ],
    }


def insert_input_snapshots(
    output: sqlite3.Connection,
    *,
    materialization_id: str,
    inputs: Mapping[str, Mapping[str, Any]],
) -> None:
    for role in (
        "legacy_raw",
        "curated_v1_3",
        "reconciliation_plan",
        "reconciliation_report",
        "reconciliation_ready",
        "structured_awards_v2",
        "structured_awards_ready",
    ):
        item = inputs[role]
        audit = item.get("audit")
        is_sqlite = audit is not None
        output.execute(
            """
            INSERT INTO curated_v2_input_snapshots(
                materialization_id,input_role,path_label,sha256_before,
                sha256_after,size_bytes,is_sqlite,query_only,quick_check,
                integrity_check,foreign_key_violations,lineage_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                materialization_id,
                role,
                item["path_label"],
                item["sha256"],
                item["sha256"],
                item["size_bytes"],
                int(is_sqlite),
                audit["query_only"] if audit else None,
                audit["quick_check"] if audit else None,
                audit["integrity_check"] if audit else None,
                audit["foreign_key_violations"] if audit else None,
                canonical_json(item.get("lineage") or {}),
            ),
        )


def final_contract_validation(
    output: sqlite3.Connection,
    *,
    baseline_contract: Mapping[tuple[str, str], Mapping[str, Any]],
    expected_projects: int,
    expected_awards: int,
    full: bool,
) -> dict[str, Any]:
    quick = output.execute("PRAGMA quick_check").fetchone()[0]
    integrity = output.execute("PRAGMA integrity_check").fetchone()[0]
    foreign_keys = output.execute("PRAGMA foreign_key_check").fetchall()
    if quick != "ok" or integrity != "ok" or foreign_keys:
        raise CuratedV2Error(
            "curated v2 validation failed: "
            f"quick={quick}, integrity={integrity}, foreign_keys={len(foreign_keys)}"
        )
    build = one_row(output, "build_runs")
    v2_run = one_row(output, "curated_v2_runs")
    if (
        build["schema_version"] != SCHEMA_VERSION
        or build["builder_version"] != MATERIALIZER_VERSION
        or v2_run["schema_version"] != SCHEMA_VERSION
    ):
        raise CuratedV2Error("curated v2 build metadata was not promoted to v2.0")
    project_count = output.execute("SELECT COUNT(*) FROM source_projects").fetchone()[0]
    award_count = output.execute(
        "SELECT COUNT(*) FROM v2_structured_award_attributions"
    ).fetchone()[0]
    if project_count != expected_projects or award_count != expected_awards:
        raise CuratedV2Error(
            "curated v2 selected count mismatch: "
            f"projects={project_count}/{expected_projects}, awards={award_count}/{expected_awards}"
        )
    final_contract = contract_objects(output)
    for key, expected in baseline_contract.items():
        actual = final_contract.get(key)
        if actual is None or (
            actual["sql_sha256"] != expected["sql_sha256"]
            or actual["sql"] != expected["sql"]
            or actual["columns_json"] != expected["columns_json"]
        ):
            raise CuratedV2Error(f"v1.3 contract regression in final v2 output: {key}")
    input_mismatch = output.execute(
        "SELECT COUNT(*) FROM curated_v2_input_snapshots "
        "WHERE sha256_before<>sha256_after"
    ).fetchone()[0]
    if input_mismatch:
        raise CuratedV2Error("curated v2 input lineage contains a changed input")
    product_projection = output.execute(
        """
        SELECT COUNT(*)
        FROM v2_structured_award_base_projections p
        JOIN v2_structured_award_attributions a
          ON a.source_attribution_id=p.source_attribution_id
        WHERE a.subject_kind IN ('product','brand')
          AND p.projection_status='projected'
        """
    ).fetchone()[0]
    if product_projection:
        raise CuratedV2Error("product/brand award leaked into the project/firm base projection")
    projected_2026 = output.execute(
        "SELECT COUNT(*) FROM source_awards WHERE award_year=2026"
    ).fetchone()[0]
    expected_projected = output.execute(
        "SELECT COUNT(*) FROM v2_structured_award_base_projections "
        "WHERE projection_status='projected'"
    ).fetchone()[0]
    if projected_2026 != expected_projected:
        raise CuratedV2Error(
            "2026 base award projection count differs from structured projection lineage"
        )
    if full and expected_projected == 0:
        raise CuratedV2Error("full curated v2 contains no project/firm 2026 base projections")
    legacy_year_range = output.execute(
        "SELECT MIN(award_year),MAX(award_year) FROM source_awards WHERE award_year<2026"
    ).fetchone()
    return {
        "quick_check": quick,
        "integrity_check": integrity,
        "foreign_key_violations": len(foreign_keys),
        "selected_project_count": int(project_count),
        "selected_structured_award_count": int(award_count),
        "projected_2026_source_award_count": int(projected_2026),
        "legacy_award_min_year": legacy_year_range[0],
        "legacy_award_max_year": legacy_year_range[1],
        "baseline_contract_object_count": len(baseline_contract),
        "reconciliation_conflict_count": output.execute(
            "SELECT COUNT(*) FROM v2_reconciliation_field_conflicts"
        ).fetchone()[0],
        "reconciliation_alias_count": output.execute(
            "SELECT COUNT(*) FROM v2_reconciliation_entity_aliases"
        ).fetchone()[0],
    }


def materialize_once(
    *,
    raw: sqlite3.Connection,
    baseline: sqlite3.Connection,
    reconciliation: sqlite3.Connection,
    awards: sqlite3.Connection,
    target_path: Path,
    limit: Optional[int],
    input_records: Mapping[str, Mapping[str, Any]],
    baseline_validation: Mapping[str, Any],
    reconciliation_validation: Mapping[str, Any],
) -> dict[str, Any]:
    full = limit is None
    mode = "full" if full else f"N{limit}"
    reconciliation_run = reconciliation_validation["run"]
    selected_awards = select_award_rows(awards, limit)
    synthetic_handle = tempfile.NamedTemporaryFile(
        prefix="architizer-curated-v2-effective.",
        suffix=".db",
        dir=target_path.parent,
        delete=False,
    )
    synthetic_path = Path(synthetic_handle.name)
    synthetic_handle.close()
    synthetic_path.unlink()
    try:
        effective_source = build_effective_source(
            raw=raw,
            reconciliation=reconciliation,
            awards=awards,
            target_path=synthetic_path,
            reconciliation_id=reconciliation_run["reconciliation_id"],
            deterministic_cutoff=reconciliation_run["deterministic_cutoff"],
            structured_attribution_ids={int(row["id"]) for row in selected_awards},
        )
        base_result = curated_v1.materialize_database(
            source_path=synthetic_path,
            target_path=target_path,
            limit=limit,
            expected_sha256=effective_source["sha256"],
            expected_size=effective_source["size_bytes"],
        )
    finally:
        for suffix in ("", "-wal", "-shm", "-journal"):
            candidate = Path(str(synthetic_path) + suffix)
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass

    selected_project_count = int(base_result["selected_projects"])
    if limit is not None and selected_project_count != limit:
        raise CuratedV2Error(
            f"reconciliation cannot satisfy {mode}: selected {selected_project_count} projects"
        )
    materialization_id = stable_id(
        "atzv2_",
        *(input_records[role]["sha256"] for role in sorted(input_records)),
        mode,
        MATERIALIZER_VERSION,
        MATERIALIZATION_POLICY_VERSION,
        MATERIALIZATION_SELECTION_VERSION,
    )
    # URI handling is enabled so the full-build fast path can ATTACH the
    # reconciliation input with mode=ro&immutable=1.
    output = sqlite3.connect(target_path, uri=True)
    output.row_factory = sqlite3.Row
    output.execute("PRAGMA foreign_keys=ON")
    try:
        base_build = one_row(output, "build_runs")
        base_build_id = str(base_build["build_id"])
        output.execute(
            "UPDATE build_runs SET builder_version=?,schema_version=?,policy_version=?",
            (MATERIALIZER_VERSION, SCHEMA_VERSION, MATERIALIZATION_POLICY_VERSION),
        )
        output.execute(
            "UPDATE source_snapshots SET source_path='reconciled_effective_source_v2'"
        )
        output.executescript(EXTENSION_DDL)
        if full:
            # The destination is a disposable temp artifact until READY is
            # atomically published.  Avoid per-page rollback/sync cost during
            # the multi-gigabyte evidence copy, then restore the durable final
            # pragmas and verify the complete file before publication.
            output.execute("PRAGMA foreign_keys=OFF")
            output.execute("PRAGMA journal_mode=OFF")
            output.execute("PRAGMA synchronous=OFF")
            for index_name, _ddl in FULL_DEFERRED_INDEX_DDL:
                output.execute(f'DROP INDEX "{index_name}"')
        selected_firm_count = output.execute(
            "SELECT COUNT(*) FROM source_firms"
        ).fetchone()[0]
        output.execute(
            """
            INSERT INTO curated_v2_runs(
                materialization_id,base_build_id,materializer_version,
                schema_version,policy_version,selection_version,build_mode,
                project_limit,award_limit,is_full_materialization,
                deterministic_timestamp,reconciliation_id,
                selected_project_count,selected_firm_count,
                selected_award_count,validation_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'{}')
            """,
            (
                materialization_id,
                base_build_id,
                MATERIALIZER_VERSION,
                SCHEMA_VERSION,
                MATERIALIZATION_POLICY_VERSION,
                MATERIALIZATION_SELECTION_VERSION,
                mode,
                limit,
                limit,
                int(full),
                reconciliation_run["deterministic_cutoff"],
                reconciliation_run["reconciliation_id"],
                selected_project_count,
                selected_firm_count,
                len(selected_awards),
            ),
        )
        insert_input_snapshots(
            output,
            materialization_id=materialization_id,
            inputs=input_records,
        )
        reconciliation_metrics = copy_reconciliation_evidence(
            reconciliation=reconciliation,
            output=output,
            materialization_id=materialization_id,
            full=full,
        )
        awards_metrics = copy_structured_awards(
            awards=awards,
            reconciliation=reconciliation,
            output=output,
            selected_rows=selected_awards,
            base_award_projections=effective_source["award_projections"],
            full=full,
        )
        operational_metrics = compute_operational_metrics(
            baseline=baseline,
            output=output,
            reconciliation=reconciliation,
            full=full,
        )
        year_counts = {
            str(row["award_year"]): int(row["n"])
            for row in output.execute(
                "SELECT award_year,COUNT(*) AS n FROM source_awards "
                "GROUP BY award_year ORDER BY award_year"
            )
        }
        metrics = {
            "award_year_counts_in_v1_compatible_projection": year_counts,
            "awards": awards_metrics,
            "effective_source_project_count": effective_source["project_count"],
            "effective_source_firm_count": effective_source["firm_count"],
            "effective_source_direct_firm_count": effective_source[
                "direct_firm_count"
            ],
            "effective_source_deferred_legacy_award_stubs": {
                "count": effective_source["deferred_legacy_award_stub_count"],
                "slugs": effective_source["deferred_legacy_award_stub_slugs"],
            },
            "reconciliation": reconciliation_metrics,
            "operational_changes": operational_metrics,
            "structured_product_brand_policy": (
                "preserve in v2 structured source corpus; never project into "
                "v1-compatible project/firm source_awards"
            ),
        }
        for name, value in sorted(metrics.items()):
            output.execute(
                "INSERT INTO curated_v2_metrics(metric_name,metric_value_json) VALUES (?,?)",
                (name, canonical_json(value)),
            )
        output.commit()
        if full:
            for _index_name, ddl in FULL_DEFERRED_INDEX_DDL:
                output.execute(ddl)
            output.commit()
        output.execute("ANALYZE")
        output.commit()
        if full:
            output.execute("PRAGMA foreign_keys=ON")
        validation = final_contract_validation(
            output,
            baseline_contract=baseline_validation["contract"],
            expected_projects=selected_project_count,
            expected_awards=len(selected_awards),
            full=full,
        )
        output.execute(
            "UPDATE curated_v2_runs SET validation_json=? WHERE materialization_id=?",
            (canonical_json(validation), materialization_id),
        )
        output.commit()
        if full:
            output.execute("PRAGMA synchronous=FULL")
        output.execute("VACUUM")
        if full:
            journal_mode = output.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            synchronous = output.execute("PRAGMA synchronous").fetchone()[0]
            if str(journal_mode).lower() != "delete" or int(synchronous) != 2:
                raise CuratedV2Error(
                    "final SQLite durability pragmas were not restored"
                )
        final_validation = final_contract_validation(
            output,
            baseline_contract=baseline_validation["contract"],
            expected_projects=selected_project_count,
            expected_awards=len(selected_awards),
            full=full,
        )
        if final_validation != validation:
            raise CuratedV2Error("curated v2 validation changed after final VACUUM")
        logical_sha = logical_database_digest(output)
    finally:
        output.close()
    fsync_file(target_path)
    for suffix in ("-wal", "-shm", "-journal"):
        if Path(str(target_path) + suffix).exists():
            raise CuratedV2Error(f"final SQLite sidecar remains: {target_path}{suffix}")
    return {
        "materialization_id": materialization_id,
        "build_mode": mode,
        "selected_project_count": selected_project_count,
        "selected_firm_count": int(selected_firm_count),
        "selected_award_count": len(selected_awards),
        "reconciliation_metrics": reconciliation_metrics,
        "award_metrics": awards_metrics,
        "operational_metrics": operational_metrics,
        "validation": final_validation,
        "database_sha256": sha256_file(target_path),
        "database_logical_sha256": logical_sha,
        "database_size_bytes": target_path.stat().st_size,
    }


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def validate_paths(
    *,
    inputs: Mapping[str, Path],
    output: Path,
    report: Path,
    ready: Path,
) -> None:
    paths = {**inputs, "output": output, "report": report, "ready": ready}
    seen: dict[str, str] = {}
    for role, path in paths.items():
        key = _path_key(path)
        if key in seen:
            raise CuratedV2Error(f"path collision: {role} == {seen[key]} ({path})")
        seen[key] = role
    for role, path in inputs.items():
        if not path.is_file():
            raise CuratedV2Error(f"missing input {role}: {path}")
    for role, path in (("output", output), ("report", report), ("READY", ready)):
        if path.exists():
            raise CuratedV2Error(f"immutable {role} already exists: {path}")
    for suffix in ("-wal", "-shm", "-journal"):
        candidate = Path(str(output) + suffix)
        if candidate.exists():
            raise CuratedV2Error(f"stale output SQLite sidecar exists: {candidate}")


@contextmanager
def build_lock(output: Path) -> Iterator[None]:
    lock = Path(str(output) + ".build.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json(
        {
            "materializer": MATERIALIZER_VERSION,
            "owner_token": secrets.token_hex(32),
            "pid": os.getpid(),
        }
    ).encode("utf-8")
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise CuratedV2Error(f"build lock already exists: {lock}") from exc
    try:
        try:
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)
        yield
    finally:
        try:
            if lock.read_bytes() == payload:
                lock.unlink()
        except FileNotFoundError:
            pass


def publish_bundle(
    database_temp: Path,
    report_temp: Path,
    ready_temp: Path,
    output: Path,
    report: Path,
    ready: Path,
    *,
    pre_ready_check: Callable[[], None] | None = None,
) -> None:
    if output.exists() or report.exists() or ready.exists():
        raise CuratedV2Error("immutable curated v2 bundle appeared during build")
    linked: list[tuple[Path, Path]] = []
    try:
        for source, destination in (
            (database_temp, output),
            (report_temp, report),
            (ready_temp, ready),
        ):
            if destination == ready and pre_ready_check is not None:
                pre_ready_check()
            os.link(source, destination)
            linked.append((source, destination))
            if destination == ready and pre_ready_check is not None:
                # Recheck while rollback still owns the READY hard link; an
                # input can drift inside the link-call boundary itself.
                pre_ready_check()
    except BaseException:
        for source, destination in reversed(linked):
            try:
                if destination.exists() and os.path.samefile(source, destination):
                    destination.unlink()
            except (FileNotFoundError, OSError):
                pass
        raise


def render_report(result: Mapping[str, Any]) -> str:
    validation = result["validation"]
    awards = result["award_metrics"]
    operational = result["operational_metrics"]
    return "\n".join(
        [
            "# Architizer Curated SQLite v2.0",
            "",
            "> Immutable final source-specific materialization. READY is required.",
            "",
            f"- Materialization ID: `{result['materialization_id']}`",
            f"- Mode: `{result['build_mode']}`",
            f"- Database SHA-256: `{result['database_sha256']}`",
            f"- Database logical SHA-256: `{result['database_logical_sha256']}`",
            f"- Database bytes: {result['database_size_bytes']:,}",
            f"- Projects: {result['selected_project_count']:,}",
            f"- Firms: {result['selected_firm_count']:,}",
            f"- Structured 2026 award attributions: {result['selected_award_count']:,}",
            f"- 2026 project/firm tier projections: {validation['projected_2026_source_award_count']:,}",
            f"- Structured subject kinds: `{canonical_json(awards['subject_counts'])}`",
            f"- Structured company kinds: `{canonical_json(awards['company_counts'])}`",
            f"- Reconciliation aliases: {validation['reconciliation_alias_count']:,}",
            f"- Reconciliation conflicts: {validation['reconciliation_conflict_count']:,}",
            f"- v1.3 contract objects verified: {validation['baseline_contract_object_count']:,}",
            f"- Byte determinism verified: `{str(bool(result['deterministic_verified'])).lower()}`",
            f"- Resource preflight: `{canonical_json(result.get('resource_preflight'))}`",
            "",
            "## Award projection policy",
            "",
            "Complete 2026 project/firm attribution tiers are projected into the v1.3-compatible `source_awards` table with deterministic keys. Product/brand and conflict/partial evidence stays only in the structured v2 source-corpus tables. Legacy pre-2026 award rows are preserved.",
            "",
            "## Operational before/after metrics",
            "",
            f"- Comparison status: `{operational['comparison_status']}`",
            f"- Firm stub decrease: `{canonical_json(operational['firm_stub_decrease'])}`",
            f"- Award unresolved decrease: `{canonical_json(operational['award_unresolved_decrease'])}`",
            f"- Field coverage change: `{canonical_json(operational['field_coverage_change'])}`",
            f"- Taxonomy claim change: `{canonical_json(operational['taxonomy_claim_change'])}`",
            f"- Duplicate candidate change: `{canonical_json(operational['duplicate_candidate_change'])}`",
            f"- Source recovery: `{canonical_json(operational['source_recovery'])}`",
            "",
            "### Open QA",
            "",
            *[f"- {item}" for item in operational["open_qa"]],
            "",
            "## Validation",
            "",
            "```json",
            json.dumps(validation, ensure_ascii=False, indent=2, sort_keys=True),
            "```",
            "",
        ]
    )


def build(
    *,
    raw_path: Path,
    baseline_path: Path,
    reconciliation_path: Path,
    reconciliation_report_path: Path,
    reconciliation_ready_path: Optional[Path],
    awards_path: Path,
    awards_ready_path: Optional[Path] = None,
    output_path: Path,
    report_path: Path,
    ready_path: Optional[Path],
    expected_raw_sha256: str,
    expected_raw_size: int,
    expected_baseline_sha256: str,
    expected_baseline_size: int,
    expected_reconciliation_sha256: str,
    expected_reconciliation_size: int,
    expected_awards_sha256: str,
    expected_awards_size: int,
    limit: Optional[int] = None,
    confirm_full: bool = False,
    verify_deterministic: bool = False,
    enforce_production_identities: bool = True,
) -> dict[str, Any]:
    if limit not in (None, 10, 100):
        raise CuratedV2Error("curated v2 smoke limit must be exactly 10 or 100")
    full = limit is None
    if full and not confirm_full:
        raise CuratedV2Error("full materialization requires explicit confirmation")
    if not full and confirm_full:
        raise CuratedV2Error("N10/N100 smoke cannot use full confirmation")
    if full and not verify_deterministic:
        raise CuratedV2Error("full materialization requires byte determinism verification")
    raw_path = raw_path.resolve()
    baseline_path = baseline_path.resolve()
    reconciliation_path = reconciliation_path.resolve()
    reconciliation_report_path = reconciliation_report_path.resolve()
    reconciliation_ready_path = (
        reconciliation_ready_path.resolve()
        if reconciliation_ready_path is not None
        else Path(str(reconciliation_path) + ".READY.json").resolve()
    )
    awards_path = awards_path.resolve()
    awards_ready_path = (
        awards_ready_path.resolve()
        if awards_ready_path is not None
        else Path(str(awards_path) + ".READY.json").resolve()
    )
    output_path = output_path.resolve()
    report_path = report_path.resolve()
    ready_path = (
        ready_path.resolve()
        if ready_path is not None
        else Path(str(output_path) + ".READY.json").resolve()
    )
    if not full and (
        output_path == DEFAULT_OUTPUT.resolve()
        or report_path == DEFAULT_REPORT.resolve()
        or ready_path == Path(str(DEFAULT_OUTPUT.resolve()) + ".READY.json")
    ):
        raise CuratedV2Error(
            "N10/N100 requires explicit non-production output, report, and READY paths"
        )
    if enforce_production_identities and full:
        if (
            expected_raw_sha256.upper() != FIXED_RAW_SHA256
            or expected_raw_size != FIXED_RAW_SIZE_BYTES
            or expected_baseline_sha256.upper() != FIXED_BASELINE_SHA256
            or expected_baseline_size != FIXED_BASELINE_SIZE_BYTES
        ):
            raise CuratedV2Error("full production materialization requires fixed raw/v1.3 identities")

    input_paths = {
        "legacy_raw": raw_path,
        "curated_v1_3": baseline_path,
        "reconciliation_plan": reconciliation_path,
        "reconciliation_report": reconciliation_report_path,
        "reconciliation_ready": reconciliation_ready_path,
        "structured_awards_v2": awards_path,
        "structured_awards_ready": awards_ready_path,
    }
    validate_paths(
        inputs=input_paths,
        output=output_path,
        report=report_path,
        ready=ready_path,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    ready_path.parent.mkdir(parents=True, exist_ok=True)

    resource_preflight = (
        preflight_full_resources(
            raw_path=raw_path,
            baseline_path=baseline_path,
            reconciliation_path=reconciliation_path,
            awards_path=awards_path,
            output_path=output_path,
            verify_deterministic=verify_deterministic,
        )
        if full
        else None
    )

    with build_lock(output_path):
        raw_identity = validate_file_identity(
            raw_path,
            expected_sha256=expected_raw_sha256,
            expected_size=expected_raw_size,
            label="legacy raw DB",
        )
        baseline_identity = validate_file_identity(
            baseline_path,
            expected_sha256=expected_baseline_sha256,
            expected_size=expected_baseline_size,
            label="curated v1.3 DB",
        )
        reconciliation_identity = validate_file_identity(
            reconciliation_path,
            expected_sha256=expected_reconciliation_sha256,
            expected_size=expected_reconciliation_size,
            label="reconciliation plan DB",
        )
        awards_identity = validate_file_identity(
            awards_path,
            expected_sha256=expected_awards_sha256,
            expected_size=expected_awards_size,
            label="structured awards DB",
        )
        raw = open_readonly(raw_path, lock_suffixes=(".build.lock",))
        baseline = open_readonly(baseline_path, lock_suffixes=(".build.lock",))
        reconciliation = open_readonly(
            reconciliation_path,
            lock_suffixes=(".build.lock",),
        )
        awards = open_readonly(
            awards_path,
            lock_suffixes=(".lock", ".build.lock"),
        )
        database_temp: Optional[Path] = None
        shadow_temp: Optional[Path] = None
        report_temp: Optional[Path] = None
        ready_temp: Optional[Path] = None
        published = False
        try:
            raw_audit = sqlite_audit(raw)
            curated_v1.validate_source(
                raw,
                raw_path,
                expected_sha256=raw_identity["sha256"],
                expected_size=raw_identity["size_bytes"],
            )
            baseline_audit = sqlite_audit(baseline)
            reconciliation_audit = sqlite_audit(reconciliation)
            awards_audit = sqlite_audit(awards)
            baseline_validation = validate_baseline(
                baseline,
                raw_identity=raw_identity,
            )
            reconciliation_validation = validate_reconciliation(
                reconciliation,
                identity=reconciliation_identity,
                raw_identity=raw_identity,
                baseline_identity=baseline_identity,
                baseline_contract=baseline_validation["contract"],
                report_path=reconciliation_report_path,
                ready_path=reconciliation_ready_path,
            )
            awards_validation = validate_awards(
                awards,
                identity=awards_identity,
                awards_path=awards_path,
                ready_path=awards_ready_path,
                enforce_production_counts=enforce_production_identities,
            )
            shared_sidecar_identity = validate_shared_sidecar_lineage(
                reconciliation_validation=reconciliation_validation,
                awards_validation=awards_validation,
                raw_identity=raw_identity,
            )
            legacy_range = raw.execute(
                "SELECT MIN(award_year),MAX(award_year),COUNT(*) FROM architizer_awards"
            ).fetchone()
            if legacy_range[1] is not None and int(legacy_range[1]) > 2025:
                raise CuratedV2Error("legacy raw awards unexpectedly contain post-2025 rows")
            if enforce_production_identities and full and (
                int(legacy_range[0]) != 2013 or int(legacy_range[1]) != 2025
            ):
                raise CuratedV2Error("production legacy award range must be 2013-2025")

            report_identity = reconciliation_validation["report_identity"]
            ready_identity = reconciliation_validation["ready_identity"]
            input_records: dict[str, dict[str, Any]] = {
                "legacy_raw": {
                    **raw_identity,
                    "audit": raw_audit,
                    "lineage": {"award_year_range": list(legacy_range[:2])},
                },
                "curated_v1_3": {
                    **baseline_identity,
                    "audit": baseline_audit,
                    "lineage": baseline_validation["build"],
                },
                "reconciliation_plan": {
                    **reconciliation_identity,
                    "audit": reconciliation_audit,
                    "lineage": reconciliation_validation["run"],
                },
                "reconciliation_report": {
                    **report_identity,
                    "lineage": {"ready_report_sha256": report_identity["sha256"]},
                },
                "reconciliation_ready": {
                    **ready_identity,
                    "lineage": reconciliation_validation["ready"],
                },
                "structured_awards_v2": {
                    **awards_identity,
                    "audit": awards_audit,
                    "lineage": {
                        "schema_meta": awards_validation["meta"],
                        "build_manifest": awards_validation["manifest"],
                    },
                },
                "structured_awards_ready": {
                    **awards_validation["ready_identity"],
                    "lineage": awards_validation["ready"],
                },
            }
            input_records["reconciliation_plan"]["lineage"][
                "shared_recrawl_sidecar"
            ] = shared_sidecar_identity

            database_handle = tempfile.NamedTemporaryFile(
                prefix=output_path.name + ".",
                suffix=".tmp",
                dir=output_path.parent,
                delete=False,
            )
            database_temp = Path(database_handle.name)
            database_handle.close()
            database_temp.unlink()
            result = materialize_once(
                raw=raw,
                baseline=baseline,
                reconciliation=reconciliation,
                awards=awards,
                target_path=database_temp,
                limit=limit,
                input_records=input_records,
                baseline_validation=baseline_validation,
                reconciliation_validation=reconciliation_validation,
            )
            shadow_sha: Optional[str] = None
            if verify_deterministic:
                shadow_handle = tempfile.NamedTemporaryFile(
                    prefix=output_path.name + ".determinism.",
                    suffix=".tmp",
                    dir=output_path.parent,
                    delete=False,
                )
                shadow_temp = Path(shadow_handle.name)
                shadow_handle.close()
                shadow_temp.unlink()
                shadow_result = materialize_once(
                    raw=raw,
                    baseline=baseline,
                    reconciliation=reconciliation,
                    awards=awards,
                    target_path=shadow_temp,
                    limit=limit,
                    input_records=input_records,
                    baseline_validation=baseline_validation,
                    reconciliation_validation=reconciliation_validation,
                )
                shadow_sha = shadow_result["database_sha256"]
                if result["database_sha256"] != shadow_sha:
                    raise CuratedV2Error(
                        "curated v2 byte determinism mismatch: "
                        f"{result['database_sha256']} != {shadow_sha}"
                    )
            result["deterministic_verified"] = verify_deterministic
            result["deterministic_shadow_sha256"] = shadow_sha
            result["resource_preflight"] = resource_preflight

            for role, path in input_paths.items():
                after = {
                    "sha256": sha256_file(path),
                    "size_bytes": path.stat().st_size,
                }
                before = input_records[role]
                if after != {
                    "sha256": before["sha256"],
                    "size_bytes": before["size_bytes"],
                }:
                    raise CuratedV2Error(f"input changed during materialization: {role}")
            assert_quiescent(raw_path, lock_suffixes=(".build.lock",))
            assert_quiescent(baseline_path, lock_suffixes=(".build.lock",))
            assert_quiescent(
                reconciliation_path,
                lock_suffixes=(".build.lock",),
            )
            assert_quiescent(
                awards_path,
                lock_suffixes=(".lock", ".build.lock"),
            )

            report_handle = tempfile.NamedTemporaryFile(
                prefix=report_path.name + ".",
                suffix=".tmp",
                dir=report_path.parent,
                delete=False,
                mode="w",
                encoding="utf-8",
                newline="\n",
            )
            report_temp = Path(report_handle.name)
            report_handle.write(render_report(result))
            report_handle.flush()
            os.fsync(report_handle.fileno())
            report_handle.close()
            report_sha = sha256_file(report_temp)
            ready_payload = {
                "artifact_kind": READY_VERSION,
                "schema_version": SCHEMA_VERSION,
                "materialization_id": result["materialization_id"],
                "build_mode": result["build_mode"],
                "database_sha256": result["database_sha256"],
                "database_logical_sha256": result["database_logical_sha256"],
                "database_size_bytes": result["database_size_bytes"],
                "report_sha256": report_sha,
                "report_size_bytes": report_temp.stat().st_size,
                "deterministic_verified": verify_deterministic,
                "input_sha256": {
                    role: input_records[role]["sha256"] for role in sorted(input_records)
                },
            }
            ready_handle = tempfile.NamedTemporaryFile(
                prefix=ready_path.name + ".",
                suffix=".tmp",
                dir=ready_path.parent,
                delete=False,
                mode="w",
                encoding="utf-8",
                newline="\n",
            )
            ready_temp = Path(ready_handle.name)
            ready_handle.write(canonical_json(ready_payload) + "\n")
            ready_handle.flush()
            os.fsync(ready_handle.fileno())
            ready_handle.close()

            def assert_publication_inputs_unchanged() -> None:
                for role, path in input_paths.items():
                    try:
                        actual = {
                            "sha256": sha256_file(path),
                            "size_bytes": path.stat().st_size,
                        }
                    except OSError as exc:
                        raise CuratedV2Error(
                            f"input unavailable before READY publication: {role}"
                        ) from exc
                    expected = {
                        "sha256": input_records[role]["sha256"],
                        "size_bytes": input_records[role]["size_bytes"],
                    }
                    if actual != expected:
                        raise CuratedV2Error(
                            f"input changed before READY publication: {role}"
                        )
                assert_quiescent(raw_path, lock_suffixes=(".build.lock",))
                assert_quiescent(
                    baseline_path,
                    lock_suffixes=(".build.lock",),
                )
                assert_quiescent(
                    reconciliation_path,
                    lock_suffixes=(".build.lock",),
                )
                assert_quiescent(
                    awards_path,
                    lock_suffixes=(".lock", ".build.lock"),
                )

            publish_bundle(
                database_temp,
                report_temp,
                ready_temp,
                output_path,
                report_path,
                ready_path,
                pre_ready_check=assert_publication_inputs_unchanged,
            )
            published = True
            return {
                **result,
                "output_path": str(output_path),
                "report_path": str(report_path),
                "ready_path": str(ready_path),
                "ready": ready_payload,
            }
        finally:
            for connection in (awards, reconciliation, baseline, raw):
                connection.close()
            for temporary in (
                database_temp,
                shadow_temp,
                report_temp,
                ready_temp,
            ):
                if temporary is not None:
                    try:
                        temporary.unlink()
                    except FileNotFoundError:
                        pass
            if not published:
                # Final destinations are never removed here; publish_bundle only
                # rolls back hard links proven to belong to this process.
                pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build immutable final Architizer curated SQLite v2.0"
    )
    parser.add_argument("--raw-db", type=Path, default=DEFAULT_RAW)
    parser.add_argument("--baseline-db", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument(
        "--reconciliation-db",
        type=Path,
        default=DEFAULT_RECONCILIATION,
    )
    parser.add_argument(
        "--reconciliation-report",
        type=Path,
        default=DEFAULT_RECONCILIATION_REPORT,
    )
    parser.add_argument("--reconciliation-ready", type=Path)
    parser.add_argument("--awards-db", type=Path, default=DEFAULT_AWARDS)
    parser.add_argument(
        "--awards-ready",
        type=Path,
        help="Defaults to <awards-db>.READY.json",
    )
    parser.add_argument("--output-db", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--ready-marker", type=Path)
    parser.add_argument("--limit", type=int, choices=(10, 100))
    parser.add_argument("--confirm-full-materialization", action="store_true")
    parser.add_argument("--verify-deterministic", action="store_true")
    parser.add_argument("--expected-reconciliation-sha256", required=True)
    parser.add_argument("--expected-reconciliation-size", type=int, required=True)
    parser.add_argument("--expected-awards-sha256", required=True)
    parser.add_argument("--expected-awards-size", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        result = build(
            raw_path=args.raw_db,
            baseline_path=args.baseline_db,
            reconciliation_path=args.reconciliation_db,
            reconciliation_report_path=args.reconciliation_report,
            reconciliation_ready_path=args.reconciliation_ready,
            awards_path=args.awards_db,
            awards_ready_path=args.awards_ready,
            output_path=args.output_db,
            report_path=args.report,
            ready_path=args.ready_marker,
            expected_raw_sha256=FIXED_RAW_SHA256,
            expected_raw_size=FIXED_RAW_SIZE_BYTES,
            expected_baseline_sha256=FIXED_BASELINE_SHA256,
            expected_baseline_size=FIXED_BASELINE_SIZE_BYTES,
            expected_reconciliation_sha256=args.expected_reconciliation_sha256,
            expected_reconciliation_size=args.expected_reconciliation_size,
            expected_awards_sha256=args.expected_awards_sha256,
            expected_awards_size=args.expected_awards_size,
            limit=args.limit,
            confirm_full=args.confirm_full_materialization,
            verify_deterministic=args.verify_deterministic,
            enforce_production_identities=True,
        )
    except (CuratedV2Error, curated_v1.BuildError, sqlite3.Error, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
