#!/usr/bin/env python3
"""Freeze the trusted input universe for a full Architizer reconciliation.

This tool is deliberately read-only with respect to all three inputs.  It
participates in the recrawler's real ``SidecarLock`` protocol for the complete
sidecar hash -> validation -> manifest publication interval and publishes a
single deterministic JSON file with no-overwrite semantics.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from crawl.architizer.recrawl_v2 import (  # noqa: E402
    METADATA_VERSION,
    PARSER_VERSION,
    SNAPSHOT_REPARSE_GATE_POLICY_VERSION,
    STATE_SCHEMA_VERSION,
    LockHeldError,
    SidecarLock,
)
from tools.reconcile_architizer_curated_v2 import (  # noqa: E402
    FIXED_BASELINE_SHA256,
    FIXED_BASELINE_SIZE_BYTES,
    FIXED_RAW_SHA256,
    FIXED_RAW_SIZE_BYTES,
    TRUSTED_MANIFEST_VERSION,
    ReconciliationError,
    _fetch_evidence_for_version,
)


class ManifestError(RuntimeError):
    """Raised when a trusted manifest cannot be safely frozen."""


REQUIRED_COLUMNS: Mapping[str, frozenset[str]] = {
    "state_meta": frozenset({"key", "value"}),
    "runs": frozenset(
        {
            "id",
            "run_kind",
            "finished_at",
            "status",
            "parser_version",
            "source_db_sha256_before",
            "source_db_sha256_after",
            "source_db_size",
            "selected_count",
            "summary_json",
        }
    ),
    "targets": frozenset(
        {
            "url",
            "entity_type",
            "status",
            "retryable",
            "attempt_count",
            "next_retry_at",
            "last_attempt_at",
            "last_error",
            "last_http_status",
            "last_snapshot_sha256",
            "last_parse_status",
            "last_good_version_id",
        }
    ),
    "metadata_versions": frozenset(
        {
            "id",
            "run_id",
            "target_url",
            "entity_type",
            "snapshot_sha256",
            "parser_version",
            "metadata_version",
            "identity_status",
            "raw_embedded_json",
            "dom_json",
        }
    ),
    "run_targets": frozenset({"run_id", "url"}),
    "http_attempts": frozenset(
        {
            "id",
            "run_id",
            "target_url",
            "request_kind",
            "requested_url",
            "outcome",
            "http_status",
            "final_url",
            "content_type",
            "response_bytes",
            "sha256",
            "gzip_path",
            "block_signals_json",
            "error",
        }
    ),
    "snapshot_reparse_inputs": frozenset(
        {
            "run_id",
            "target_url",
            "selection_order",
            "entity_type",
            "selection_kind",
            "source_run_id",
            "source_metadata_version_id",
            "source_http_attempt_id",
            "request_kind",
            "requested_url",
            "http_outcome",
            "http_status",
            "block_signals_json",
            "attempt_error",
            "content_sha256",
            "final_url",
            "content_type",
            "response_bytes",
            "gzip_path",
            "gzip_sha256",
            "integrity_status",
            "target_network_state_json",
            "frozen_at",
        }
    ),
    "snapshot_reparse_lineage": frozenset(
        {
            "reparse_version_id",
            "reparse_run_id",
            "entity_type",
            "selection_kind",
            "source_run_id",
            "source_metadata_version_id",
            "source_http_attempt_id",
            "request_kind",
            "requested_url",
            "http_outcome",
            "http_status",
            "block_signals_json",
            "attempt_error",
            "target_url",
            "content_sha256",
            "final_url",
            "content_type",
            "response_bytes",
            "gzip_path",
            "gzip_sha256",
            "integrity_status",
            "verified_at",
        }
    ),
}


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def url_set_sha256(urls: Iterable[str]) -> str:
    payload = "\n".join(sorted(set(urls))).encode("utf-8")
    return hashlib.sha256(payload).hexdigest().upper()


def _identity(path: Path) -> dict[str, Any]:
    return {
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def _assert_regular_file(path: Path, role: str) -> None:
    if not path.is_file():
        raise ManifestError(f"missing {role} input: {path}")
    for suffix in ("-wal", "-journal"):
        candidate = Path(str(path) + suffix)
        if candidate.exists() and candidate.stat().st_size:
            raise ManifestError(
                f"{role} has uncheckpointed SQLite state: {candidate}"
            )


def _assert_fixed_inputs(
    raw_identity: Mapping[str, Any],
    baseline_identity: Mapping[str, Any],
) -> None:
    expected = {
        "legacy_raw": (FIXED_RAW_SHA256, FIXED_RAW_SIZE_BYTES, raw_identity),
        "curated_v1_3": (
            FIXED_BASELINE_SHA256,
            FIXED_BASELINE_SIZE_BYTES,
            baseline_identity,
        ),
    }
    for role, (expected_sha, expected_size, actual) in expected.items():
        if (
            str(actual["sha256"]).upper() != str(expected_sha).upper()
            or int(actual["size_bytes"]) != int(expected_size)
        ):
            raise ManifestError(
                f"{role} is not the fixed production input: "
                f"{actual['sha256']}/{actual['size_bytes']}"
            )


def _open_sidecar(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro&immutable=1",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
        connection.close()
        raise ManifestError("could not enable query_only on recrawl sidecar")
    return connection


def _require_schema(connection: sqlite3.Connection) -> None:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    missing_tables = sorted(set(REQUIRED_COLUMNS) - tables)
    if missing_tables:
        raise ManifestError(f"sidecar missing tables: {missing_tables}")
    for table, required in REQUIRED_COLUMNS.items():
        columns = {
            str(row[1])
            for row in connection.execute(f'PRAGMA table_info("{table}")')
        }
        missing = sorted(required - columns)
        if missing:
            raise ManifestError(f"sidecar {table} missing columns: {missing}")


def _validate_integrity(connection: sqlite3.Connection) -> dict[str, Any]:
    quick_rows = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
    if quick_rows != ["ok"]:
        raise ManifestError(f"sidecar quick_check failed: {quick_rows[:10]}")
    foreign_keys = [
        list(row) for row in connection.execute("PRAGMA foreign_key_check")
    ]
    if foreign_keys:
        raise ManifestError(
            "sidecar foreign_key_check failed: "
            + canonical_json(foreign_keys[:10])
        )
    return {
        "quick_check": "ok",
        "foreign_key_violation_count": 0,
    }


def _parse_summary(raw_value: Any, run_id: int) -> dict[str, Any]:
    try:
        value = json.loads(str(raw_value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ManifestError(f"run {run_id} has invalid summary_json") from exc
    if not isinstance(value, dict):
        raise ManifestError(f"run {run_id} summary_json is not an object")
    return value


def _required_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ManifestError(f"{label} is not an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ManifestError(f"{label} is not an integer") from exc


def _freeze_sidecar_contract(
    connection: sqlite3.Connection,
    *,
    raw_identity: Mapping[str, Any],
) -> dict[str, Any]:
    _require_schema(connection)
    integrity = _validate_integrity(connection)
    state_meta = {
        str(row[0]): str(row[1])
        for row in connection.execute("SELECT key,value FROM state_meta")
    }
    if state_meta.get("schema_version") != STATE_SCHEMA_VERSION:
        raise ManifestError(
            "unsupported sidecar schema version: "
            f"{state_meta.get('schema_version')!r}"
        )
    if state_meta.get("source_db_sha256", "").upper() != raw_identity["sha256"]:
        raise ManifestError("sidecar source SHA does not match fixed legacy raw DB")
    bound_size = _required_int(
        state_meta.get("source_db_size"), "sidecar source size binding"
    )
    if bound_size != raw_identity["size_bytes"]:
        raise ManifestError("sidecar source size does not match fixed legacy raw DB")

    active_rows = [
        dict(row)
        for row in connection.execute(
            "SELECT id,status,finished_at FROM runs "
            "WHERE finished_at IS NULL OR status='running' ORDER BY id"
        )
    ]
    if active_rows:
        raise ManifestError(
            "sidecar has active/incomplete runs: " + canonical_json(active_rows)
        )
    pending_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM targets WHERE status!='done' "
            "AND NOT (status='failed' AND retryable=0)"
        ).fetchone()[0]
    )
    if pending_count:
        raise ManifestError(f"sidecar is not converged: {pending_count} targets remain")

    done_without_last_good = int(
        connection.execute(
            "SELECT COUNT(*) FROM targets "
            "WHERE status='done' AND last_good_version_id IS NULL"
        ).fetchone()[0]
    )
    if done_without_last_good:
        raise ManifestError(
            "sidecar has done targets without last-good metadata: "
            f"{done_without_last_good}"
        )

    invalid_links = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM targets AS t
            LEFT JOIN metadata_versions AS m ON m.id=t.last_good_version_id
            WHERE t.last_good_version_id IS NOT NULL
              AND (
                m.id IS NULL OR m.target_url!=t.url
                OR m.entity_type!=t.entity_type OR m.identity_status!='valid'
              )
            """
        ).fetchone()[0]
    )
    if invalid_links:
        raise ManifestError(f"invalid last-good metadata links: {invalid_links}")

    last_good_rows = list(
        connection.execute(
            """
            SELECT t.url,t.entity_type,m.id AS last_good_version_id,m.run_id,
                   m.snapshot_sha256,m.parser_version,m.metadata_version,
                   m.identity_status
            FROM targets AS t
            JOIN metadata_versions AS m ON m.id=t.last_good_version_id
            WHERE t.last_good_version_id IS NOT NULL
            ORDER BY m.run_id,t.url
            """
        )
    )
    if not last_good_rows:
        raise ManifestError("sidecar has no last-good metadata universe")
    evidence_kind_counts: dict[str, int] = {}
    try:
        for row in last_good_rows:
            evidence = _fetch_evidence_for_version(connection, dict(row))
            kind = str(evidence["evidence_kind"])
            evidence_kind_counts[kind] = evidence_kind_counts.get(kind, 0) + 1
    except ReconciliationError as exc:
        raise ManifestError(f"invalid last-good fetch lineage: {exc}") from exc
    run_ids = sorted({int(row["run_id"]) for row in last_good_rows})
    required_runs: list[dict[str, Any]] = []
    for run_id in run_ids:
        run = connection.execute(
            """
            SELECT id,run_kind,finished_at,status,parser_version,
                   source_db_sha256_before,source_db_sha256_after,
                   source_db_size,selected_count,summary_json
            FROM runs WHERE id=?
            """,
            (run_id,),
        ).fetchone()
        if run is None:
            raise ManifestError(f"last-good metadata references missing run {run_id}")
        if run["finished_at"] is None or not str(run["status"]).startswith(
            "completed"
        ):
            raise ManifestError(f"last-good metadata references incomplete run {run_id}")
        if (
            str(run["source_db_sha256_before"]).upper() != raw_identity["sha256"]
            or str(run["source_db_sha256_after"]).upper()
            != raw_identity["sha256"]
            or _required_int(run["source_db_size"], f"run {run_id} source size")
            != raw_identity["size_bytes"]
        ):
            raise ManifestError(f"run {run_id} source lineage mismatch")
        summary = _parse_summary(run["summary_json"], run_id)
        snapshot_reparse_gate = None
        if str(run["run_kind"]) in {
            "snapshot_reparse_n10",
            "snapshot_reparse_n100",
            "snapshot_reparse_full",
        }:
            snapshot_reparse_gate = {
                "gate_policy_version": summary.get("gate_policy_version"),
                "gate_passed": summary.get("gate_passed"),
                "state_schema_version": summary.get("state_schema_version"),
                "parser_version": summary.get("parser_version"),
                "metadata_version": summary.get("metadata_version"),
            }
            if snapshot_reparse_gate != {
                "gate_policy_version": SNAPSHOT_REPARSE_GATE_POLICY_VERSION,
                "gate_passed": True,
                "state_schema_version": STATE_SCHEMA_VERSION,
                "parser_version": PARSER_VERSION,
                "metadata_version": METADATA_VERSION,
            }:
                raise ManifestError(
                    f"snapshot reparse run {run_id} gate contract mismatch"
                )
        run_target_urls = [
            str(row[0])
            for row in connection.execute(
                "SELECT url FROM run_targets WHERE run_id=? ORDER BY url",
                (run_id,),
            )
        ]
        if _required_int(run["selected_count"], f"run {run_id} selected_count") != len(
            run_target_urls
        ):
            raise ManifestError(f"run {run_id} selected_count mismatch")
        referenced_urls = [
            str(row["url"])
            for row in last_good_rows
            if int(row["run_id"]) == run_id
        ]
        referenced_parser_versions = sorted(
            {
                str(row["parser_version"])
                for row in last_good_rows
                if int(row["run_id"]) == run_id
            }
        )
        referenced_metadata_versions = sorted(
            {
                str(row["metadata_version"])
                for row in last_good_rows
                if int(row["run_id"]) == run_id
            }
        )
        if not set(referenced_urls).issubset(set(run_target_urls)):
            raise ManifestError(
                f"run {run_id} last-good target is absent from frozen run_targets"
            )
        frozen_sha = url_set_sha256(run_target_urls)
        summary_sha = str(summary.get("frozen_target_urls_sha256") or "").upper()
        if summary_sha and summary_sha != frozen_sha:
            raise ManifestError(f"run {run_id} frozen target SHA mismatch")
        summary_count = summary.get("frozen_target_count")
        if summary_count is not None and _required_int(
            summary_count, f"run {run_id} frozen_target_count"
        ) != len(run_target_urls):
            raise ManifestError(f"run {run_id} frozen target count mismatch")
        required_runs.append(
            {
                "id": run_id,
                "run_kind": str(run["run_kind"]),
                "status": str(run["status"]),
                "finished_at": str(run["finished_at"]),
                "parser_version": str(run["parser_version"]),
                "selected_count": len(run_target_urls),
                "frozen_target_urls_sha256": frozen_sha,
                "referenced_last_good_count": len(referenced_urls),
                "referenced_last_good_urls_sha256": url_set_sha256(referenced_urls),
                "referenced_parser_versions": referenced_parser_versions,
                "referenced_metadata_versions": referenced_metadata_versions,
                "snapshot_reparse_gate": snapshot_reparse_gate,
            }
        )

    parser_versions = sorted({str(row["parser_version"]) for row in last_good_rows})
    metadata_versions = sorted(
        {str(row["metadata_version"]) for row in last_good_rows}
    )
    return {
        "schema_version": state_meta["schema_version"],
        "source_db_sha256": state_meta["source_db_sha256"].upper(),
        "source_db_size": bound_size,
        "pending_target_count": 0,
        "active_run_count": 0,
        "done_without_last_good_count": 0,
        "invalid_last_good_link_count": 0,
        "last_good_target_count": len(last_good_rows),
        "last_good_target_urls_sha256": url_set_sha256(
            str(row["url"]) for row in last_good_rows
        ),
        "last_good_evidence_kind_counts": dict(sorted(evidence_kind_counts.items())),
        "parser_versions": parser_versions,
        "metadata_versions": metadata_versions,
        "required_completed_runs": required_runs,
        "input_integrity": integrity,
    }


def _publish_no_overwrite(
    path: Path,
    payload: Mapping[str, Any],
    *,
    publication_check: Callable[[], None] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ManifestError(f"immutable manifest already exists: {path}")
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp_path = Path(temp_name)
    linked = False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if publication_check is not None:
            publication_check()
        try:
            os.link(temp_path, path)
        except FileExistsError as exc:
            raise ManifestError(f"immutable manifest already exists: {path}") from exc
        linked = True
        # Recheck after the hard-link as well.  The input could change inside
        # the publication syscall boundary after the pre-link check.
        if publication_check is not None:
            publication_check()
    except BaseException:
        if linked:
            try:
                if path.exists() and os.path.samefile(temp_path, path):
                    path.unlink()
            except (FileNotFoundError, OSError):
                pass
        raise
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def build_trusted_manifest(
    *,
    raw_path: Path,
    baseline_path: Path,
    sidecar_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    raw_path = raw_path.resolve()
    baseline_path = baseline_path.resolve()
    sidecar_path = sidecar_path.resolve()
    output_path = output_path.resolve()
    for role, path in (
        ("legacy_raw", raw_path),
        ("curated_v1_3", baseline_path),
        ("recrawl_sidecar", sidecar_path),
    ):
        _assert_regular_file(path, role)
    if output_path in {raw_path, baseline_path, sidecar_path}:
        raise ManifestError("manifest output collides with an input")

    raw_identity = _identity(raw_path)
    baseline_identity = _identity(baseline_path)
    _assert_fixed_inputs(raw_identity, baseline_identity)

    try:
        with SidecarLock(sidecar_path):
            sidecar_identity = _identity(sidecar_path)
            connection = _open_sidecar(sidecar_path)
            try:
                contract = _freeze_sidecar_contract(
                    connection,
                    raw_identity=raw_identity,
                )
            finally:
                connection.close()
            if _identity(sidecar_path) != sidecar_identity:
                raise ManifestError("recrawl sidecar changed during manifest freeze")
            payload = {
                "manifest_version": TRUSTED_MANIFEST_VERSION,
                "artifact_kind": "trusted_architizer_reconciliation_inputs",
                "inputs": {
                    "curated_v1_3": baseline_identity,
                    "legacy_raw": raw_identity,
                    "recrawl_sidecar": sidecar_identity,
                },
                "sidecar_contract": contract,
            }

            def assert_inputs_unchanged() -> None:
                for role, path, expected in (
                    ("legacy_raw", raw_path, raw_identity),
                    ("curated_v1_3", baseline_path, baseline_identity),
                    ("recrawl_sidecar", sidecar_path, sidecar_identity),
                ):
                    _assert_regular_file(path, role)
                    if _identity(path) != expected:
                        raise ManifestError(
                            f"{role} changed during manifest freeze"
                        )

            _publish_no_overwrite(
                output_path,
                payload,
                publication_check=assert_inputs_unchanged,
            )
    except LockHeldError as exc:
        raise ManifestError(str(exc)) from exc
    except sqlite3.DatabaseError as exc:
        raise ManifestError(f"invalid recrawl sidecar SQLite: {exc}") from exc
    except OSError as exc:
        raise ManifestError(f"manifest I/O failed: {exc}") from exc
    return {
        "output": str(output_path),
        "sha256": sha256_file(output_path),
        "size_bytes": output_path.stat().st_size,
        "payload": payload,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Freeze trusted fixed inputs for Architizer full reconciliation."
    )
    parser.add_argument("--raw-db", type=Path, required=True)
    parser.add_argument("--curated-v1-3-db", type=Path, required=True)
    parser.add_argument("--recrawl-sidecar-db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = build_trusted_manifest(
            raw_path=args.raw_db,
            baseline_path=args.curated_v1_3_db,
            sidecar_path=args.recrawl_sidecar_db,
            output_path=args.output,
        )
    except ManifestError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
