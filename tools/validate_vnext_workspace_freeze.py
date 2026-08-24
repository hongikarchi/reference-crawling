#!/usr/bin/env python3
"""Independently validate a WP0a vNext workspace freeze manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
from collections import Counter
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "data" / "reports" / "vnext_workspace_freeze_v1.json"
MANIFEST_KIND = "architinder-vnext-workspace-freeze-v1"
SCHEMA_VERSION = 1
HASH_CHUNK_BYTES = 8 * 1024 * 1024


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def logical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        env=_git_env(),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def _check(checks: list[dict[str, object]], name: str, ok: bool, detail: object) -> None:
    checks.append({"name": name, "ok": bool(ok), "detail": detail})


def _sqlite_metadata(path: Path) -> dict[str, object]:
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        schema_rows = connection.execute(
            """SELECT type,name,tbl_name,COALESCE(sql,'')
               FROM sqlite_master
               WHERE name NOT LIKE 'sqlite_%'
               ORDER BY type,name,tbl_name"""
        ).fetchall()
        schema_payload = [list(row) for row in schema_rows]
        return {
            "application_id": int(connection.execute("PRAGMA application_id").fetchone()[0]),
            "user_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
            "schema_object_count": len(schema_payload),
            "schema_sha256": logical_sha256(schema_payload),
        }
    finally:
        connection.close()


def validate_manifest(
    root: Path,
    manifest_path: Path,
    *,
    check_live_head: bool = True,
) -> dict[str, object]:
    root = root.resolve()
    raw = manifest_path.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    checks: list[dict[str, object]] = []

    _check(checks, "manifest_kind", payload.get("manifest_kind") == MANIFEST_KIND, payload.get("manifest_kind"))
    _check(checks, "schema_version", payload.get("schema_version") == SCHEMA_VERSION, payload.get("schema_version"))
    stored_logical = payload.get("manifest_sha256")
    logical_payload = dict(payload)
    logical_payload.pop("manifest_sha256", None)
    actual_logical = logical_sha256(logical_payload)
    _check(checks, "manifest_logical_sha256", stored_logical == actual_logical, {"stored": stored_logical, "actual": actual_logical})

    frozen = payload.get("frozen_inputs")
    _check(checks, "frozen_inputs_shape", isinstance(frozen, list) and len(frozen) == 6, len(frozen) if isinstance(frozen, list) else None)
    frozen_paths: list[str] = []
    if isinstance(frozen, list):
        for row in frozen:
            relative = str(row.get("path"))
            frozen_paths.append(relative)
            path = root / relative
            exists = path.is_file()
            _check(checks, f"frozen_exists:{relative}", exists, exists)
            if not exists:
                continue
            actual_size = path.stat().st_size
            expected_size = row.get("size_bytes")
            _check(checks, f"frozen_size:{relative}", actual_size == expected_size, {"stored": expected_size, "actual": actual_size})
            actual_sha = sha256_file(path)
            expected_sha = row.get("sha256")
            _check(checks, f"frozen_sha256:{relative}", actual_sha == expected_sha, {"stored": expected_sha, "actual": actual_sha})
            try:
                actual_sqlite = _sqlite_metadata(path)
            except (OSError, sqlite3.Error) as exc:
                _check(
                    checks,
                    f"frozen_sqlite_metadata:{relative}",
                    False,
                    {"error_type": type(exc).__name__, "error": str(exc)},
                )
            else:
                _check(
                    checks,
                    f"frozen_sqlite_metadata:{relative}",
                    actual_sqlite == row.get("sqlite"),
                    {"stored": row.get("sqlite"), "actual": actual_sqlite},
                )
    _check(checks, "frozen_paths_unique", len(frozen_paths) == len(set(frozen_paths)), frozen_paths)

    entries = payload.get("workspace_entries")
    _check(checks, "workspace_entries_shape", isinstance(entries, list), type(entries).__name__)
    entry_paths: list[str] = []
    if isinstance(entries, list):
        for row in entries:
            relative = str(row.get("path"))
            entry_paths.append(relative)
            if row.get("kind") != "file":
                continue
            path = root / relative
            exists = path.is_file()
            _check(checks, f"workspace_file_exists:{relative}", exists, exists)
            if not exists:
                continue
            actual_size = path.stat().st_size
            _check(checks, f"workspace_file_size:{relative}", actual_size == row.get("size_bytes"), {"stored": row.get("size_bytes"), "actual": actual_size})
            if row.get("sha256") is not None:
                actual_sha = sha256_file(path)
                _check(checks, f"workspace_file_sha256:{relative}", actual_sha == row.get("sha256"), {"stored": row.get("sha256"), "actual": actual_sha})
    _check(checks, "workspace_paths_unique", len(entry_paths) == len(set(entry_paths)), len(entry_paths))

    inventory = payload.get("enrichment_inventory")
    inventory_entries = inventory.get("entries") if isinstance(inventory, dict) else None
    _check(checks, "enrichment_inventory_shape", isinstance(inventory_entries, list), type(inventory_entries).__name__)
    if isinstance(inventory_entries, list):
        classes = Counter(str(row.get("classification")) for row in inventory_entries)
        kinds = Counter(str(row.get("kind")) for row in inventory_entries)
        bytes_by_class: Counter[str] = Counter()
        paths: list[str] = []
        for row in inventory_entries:
            relative = str(row.get("path"))
            paths.append(relative)
            size = row.get("size_bytes")
            if isinstance(size, int):
                bytes_by_class[str(row.get("classification"))] += size
            path = root / relative
            if row.get("kind") == "file" and path.is_file():
                _check(checks, f"inventory_size:{relative}", path.stat().st_size == size, {"stored": size, "actual": path.stat().st_size})
            elif row.get("kind") == "directory":
                _check(checks, f"inventory_directory:{relative}", path.is_dir(), path.exists())
        _check(checks, "inventory_paths_unique", len(paths) == len(set(paths)), len(paths))
        _check(checks, "inventory_entry_count", inventory.get("entry_count") == len(inventory_entries), {"stored": inventory.get("entry_count"), "actual": len(inventory_entries)})
        _check(checks, "inventory_class_counts", inventory.get("counts_by_classification") == dict(sorted(classes.items())), inventory.get("counts_by_classification"))
        _check(checks, "inventory_kind_counts", inventory.get("counts_by_kind") == dict(sorted(kinds.items())), inventory.get("counts_by_kind"))
        _check(checks, "inventory_byte_counts", inventory.get("bytes_by_classification") == dict(sorted(bytes_by_class.items())), inventory.get("bytes_by_classification"))

    policy = payload.get("policy")
    required_policy = {
        "accepted_inputs_hashed": True,
        "draft_partial_wal_lock_deleted": False,
        "frozen_inputs_mutated": False,
    }
    _check(checks, "freeze_policy", isinstance(policy, dict) and all(policy.get(key) == value for key, value in required_policy.items()), policy)

    if check_live_head:
        current_head = _git(root, "rev-parse", "HEAD")
        stored_head = payload.get("git", {}).get("head_sha") if isinstance(payload.get("git"), dict) else None
        _check(checks, "git_head_unchanged", current_head == stored_head, {"stored": stored_head, "actual": current_head})

    failures = [row for row in checks if not row["ok"]]
    return {
        "status": "PASS" if not failures else "FAIL",
        "manifest_path": manifest_path.as_posix(),
        "manifest_file_sha256": hashlib.sha256(raw).hexdigest(),
        "manifest_logical_sha256": actual_logical,
        "check_count": len(checks),
        "failure_count": len(failures),
        "failures": failures,
        "checks": checks,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--skip-live-head", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    manifest = args.manifest if args.manifest.is_absolute() else args.root / args.manifest
    report = validate_manifest(args.root, manifest, check_live_head=not args.skip_live_head)
    print(f"vNext workspace freeze validation: {report['status']}")
    print(f"checks: {report['check_count']}")
    print(f"failures: {report['failure_count']}")
    print(f"manifest_file_sha256: {report['manifest_file_sha256']}")
    for failure in report["failures"]:
        print(f"FAIL {failure['name']}: {failure['detail']}")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
