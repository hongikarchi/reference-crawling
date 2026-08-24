#!/usr/bin/env python3
"""Build the immutable WP0a vNext workspace freeze manifest.

The manifest intentionally hashes only the six accepted frozen inputs.
Superseded drafts, partials, WAL/SHM files, locks, caches, temporary
directories, and other Git-status entries are metadata-only inventory. Nothing
is deleted or modified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "data" / "reports" / "vnext_workspace_freeze_v1.json"
MANIFEST_KIND = "architinder-vnext-workspace-freeze-v1"
SCHEMA_VERSION = 1
HASH_CHUNK_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class FrozenInputSpec:
    role: str
    path: str
    expected_size_bytes: int
    expected_sha256: str


FROZEN_INPUT_SPECS: tuple[FrozenInputSpec, ...] = (
    FrozenInputSpec(
        "divisare_curated_metadata",
        "data/curated/divisare_metadata_v2_4.db",
        2_225_299_456,
        "9c523f3393d20ae8732677981c207abd02247ca5ce905dec422a05fa0398f70f",
    ),
    FrozenInputSpec(
        "architizer_curated_metadata",
        "data/curated/architizer_curated_v2_0.db",
        8_767_438_848,
        "605f4f534fc74267d49ea0b7b3f9b3bed6b55acc3a44a18ae7cafdc53633fbbc",
    ),
    FrozenInputSpec(
        "divisare_e1_v1_2",
        "data/enrichment/divisare_image_fingerprints_e1_full_v1_2.db",
        2_646_114_304,
        "869a79fee9fd65ddeffa299fef4dd9e2ba15a9c7c7170964b03fee1f4c96a819",
    ),
    FrozenInputSpec(
        "architizer_e1_v1_2",
        "data/enrichment/architizer_image_fingerprints_e1_full_v1_2.db",
        4_373_962_752,
        "58aecdcda936f7327ef7bb4bf3fe21a39ad070e784ab7061e989b62c2dcfe937",
    ),
    FrozenInputSpec(
        "cross_source_e2_full_v5",
        "data/enrichment/divisare_architizer_image_evidence_e2_full_v5.db",
        10_164_682_752,
        "4728f9f015d7fba303923df40f550c59835a7e2a5c1316eef03c499ee3ca7b19",
    ),
    FrozenInputSpec(
        "cross_source_e3_full_v1",
        "data/enrichment/divisare_architizer_image_selection_e3_full_v1.db",
        10_236_592_128,
        "8512e11f8e1fd581038f790b27a67c0a8b1949067bf53b3ef30c4ea3534141a4",
    ),
)


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


def run_git(root: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        env=_git_env(),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout


def parse_porcelain_v1_z(raw: bytes) -> list[dict[str, str]]:
    fields = raw.split(b"\0")
    records: list[dict[str, str]] = []
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if not field:
            continue
        text = field.decode("utf-8", errors="surrogateescape")
        if len(text) < 4 or text[2] != " ":
            raise ValueError(f"unsupported porcelain record: {text!r}")
        status = text[:2]
        record = {"status": status, "path": text[3:].replace("\\", "/")}
        if "R" in status or "C" in status:
            if index >= len(fields) or not fields[index]:
                raise ValueError("rename/copy record missing source path")
            record["source_path"] = fields[index].decode(
                "utf-8", errors="surrogateescape"
            ).replace("\\", "/")
            index += 1
        records.append(record)
    return records


def git_status_records(root: Path, mode: str) -> list[dict[str, str]]:
    if mode not in {"normal", "all"}:
        raise ValueError(f"unsupported untracked mode: {mode}")
    return parse_porcelain_v1_z(
        run_git(root, "status", "--porcelain=v1", "-z", f"--untracked-files={mode}")
    )


def _top_level(path: str) -> str:
    return path.split("/", 1)[0]


def _snapshot_workspace_entry(root: Path, record: dict[str, str]) -> dict[str, object]:
    relative = record["path"]
    item: dict[str, object] = {"path": relative, "status": record["status"]}
    path = root / Path(relative)
    try:
        stat = path.lstat()
        item["mtime_ns"] = stat.st_mtime_ns
        if path.is_symlink():
            item["kind"] = "symlink"
            item["target"] = os.readlink(path)
        elif path.is_file():
            item["kind"] = "file"
            item["size_bytes"] = stat.st_size
        elif path.is_dir():
            item["kind"] = "directory"
        else:
            item["kind"] = "other"
    except (OSError, PermissionError) as exc:
        item["kind"] = "unreadable"
        item["error_type"] = type(exc).__name__
        item["error"] = str(exc)
    return item


def _sqlite_metadata(path: Path) -> dict[str, object]:
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        schema_rows = connection.execute(
            """SELECT type,name,tbl_name,COALESCE(sql,'')
               FROM sqlite_master
               WHERE name NOT LIKE 'sqlite_%'
               ORDER BY type,name,tbl_name"""
        ).fetchall()
        schema_payload = [list(row) for row in schema_rows]
        return {
            "application_id": application_id,
            "user_version": user_version,
            "schema_object_count": len(schema_payload),
            "schema_sha256": logical_sha256(schema_payload),
        }
    finally:
        connection.close()


def _related_sidecars(root: Path, path: Path) -> list[dict[str, object]]:
    candidates = (
        Path(str(path) + ".lock"),
        Path(str(path) + ".partial"),
        Path(str(path) + "-wal"),
        Path(str(path) + "-shm"),
        path.with_suffix(path.suffix + ".wal"),
        path.with_suffix(path.suffix + ".shm"),
    )
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen or not candidate.exists():
            continue
        seen.add(key)
        stat = candidate.stat()
        rows.append(
            {
                "path": candidate.relative_to(root).as_posix(),
                "size_bytes": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    return rows


def snapshot_frozen_input(root: Path, spec: FrozenInputSpec) -> dict[str, object]:
    path = root / spec.path
    if not path.is_file():
        raise FileNotFoundError(f"missing frozen input: {spec.path}")
    stat = path.stat()
    actual_sha256 = sha256_file(path)
    if stat.st_size != spec.expected_size_bytes:
        raise ValueError(
            f"frozen size mismatch for {spec.path}: "
            f"expected {spec.expected_size_bytes}, got {stat.st_size}"
        )
    if actual_sha256 != spec.expected_sha256:
        raise ValueError(
            f"frozen SHA mismatch for {spec.path}: "
            f"expected {spec.expected_sha256}, got {actual_sha256}"
        )
    return {
        "role": spec.role,
        "path": spec.path,
        "size_bytes": stat.st_size,
        "sha256": actual_sha256,
        "mtime_ns": stat.st_mtime_ns,
        "sqlite": _sqlite_metadata(path),
        "related_sidecars": _related_sidecars(root, path),
    }


def classify_enrichment_entry(name: str, accepted_names: set[str]) -> str:
    lower = name.casefold()
    if name in accepted_names:
        return "accepted_frozen_input"
    if lower.endswith((".partial", ".lock", ".db-wal", ".db-shm", ".wal", ".shm")):
        return "partial_or_runtime_residue"
    if "semantic_" in lower or "semantic-" in lower:
        return "experimental_evidence_only"
    rejected_e2 = any(
        version in lower
        for version in (
            "image_evidence_e2_full_v1",
            "image_evidence_e2_full_v2",
            "image_evidence_e2_full_v3",
            "image_evidence_e2_full_v4",
        )
    )
    rejected_e3 = (
        "image_selection_e3" in lower
        and "image_selection_e3_full_v1" not in lower
    )
    rejected_e1 = lower.endswith(
        (
            "image_fingerprints_e1_full_v1.db",
            "image_fingerprints_e1_full_v1_1.db",
        )
    )
    if rejected_e1 or rejected_e2 or rejected_e3:
        return "rejected_or_superseded_draft"
    return "other_inventory_only"


def enrichment_inventory(
    root: Path, specs: Sequence[FrozenInputSpec]
) -> dict[str, object]:
    directory = root / "data" / "enrichment"
    accepted_names = {
        Path(spec.path).name for spec in specs if Path(spec.path).parent.as_posix() == "data/enrichment"
    }
    entries: list[dict[str, object]] = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
        try:
            stat = path.stat()
            row: dict[str, object] = {
                "path": path.relative_to(root).as_posix(),
                "name": path.name,
                "kind": "directory" if path.is_dir() else "file",
                "size_bytes": stat.st_size if path.is_file() else None,
                "mtime_ns": stat.st_mtime_ns,
                "classification": classify_enrichment_entry(path.name, accepted_names),
            }
        except (OSError, PermissionError) as exc:
            row = {
                "path": path.relative_to(root).as_posix(),
                "name": path.name,
                "kind": "unreadable",
                "size_bytes": None,
                "mtime_ns": None,
                "classification": "unreadable_inventory_only",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        entries.append(row)
    classes = Counter(str(row["classification"]) for row in entries)
    kinds = Counter(str(row["kind"]) for row in entries)
    bytes_by_class: Counter[str] = Counter()
    for row in entries:
        size = row.get("size_bytes")
        if isinstance(size, int):
            bytes_by_class[str(row["classification"])] += size
    return {
        "path": "data/enrichment",
        "entry_count": len(entries),
        "counts_by_classification": dict(sorted(classes.items())),
        "counts_by_kind": dict(sorted(kinds.items())),
        "bytes_by_classification": dict(sorted(bytes_by_class.items())),
        "entries": entries,
    }


def _status_summary(records: Iterable[dict[str, str]]) -> dict[str, object]:
    records_list = list(records)
    statuses = Counter(record["status"] for record in records_list)
    top_levels = Counter(_top_level(record["path"]) for record in records_list)
    return {
        "record_count": len(records_list),
        "counts_by_status": dict(sorted(statuses.items())),
        "counts_by_top_level": dict(sorted(top_levels.items())),
    }


def build_manifest(
    root: Path,
    *,
    specs: Sequence[FrozenInputSpec] = FROZEN_INPUT_SPECS,
) -> dict[str, object]:
    root = root.resolve()
    normal_records = git_status_records(root, "normal")
    all_records = git_status_records(root, "all")
    all_untracked_paths = sorted(
        record["path"] for record in all_records if record["status"] == "??"
    )
    workspace_entries = [
        _snapshot_workspace_entry(root, record) for record in normal_records
    ]
    frozen_inputs = [snapshot_frozen_input(root, spec) for spec in specs]
    payload: dict[str, object] = {
        "manifest_kind": MANIFEST_KIND,
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_root": ".",
        "git": {
            "head_sha": run_git(root, "rev-parse", "HEAD").decode("ascii").strip(),
            "branch": run_git(root, "branch", "--show-current").decode("utf-8").strip(),
            "status_normal": _status_summary(normal_records),
            "status_all": _status_summary(all_records),
            "normal_records": normal_records,
            "all_untracked_path_count": len(all_untracked_paths),
            "all_untracked_paths_sha256": logical_sha256(all_untracked_paths),
        },
        "workspace_entries": workspace_entries,
        "frozen_inputs": frozen_inputs,
        "enrichment_inventory": enrichment_inventory(root, specs),
        "policy": {
            "accepted_inputs_hashed": True,
            "normal_status_readable_files_hashed": False,
            "all_untracked_paths_stored": False,
            "all_untracked_paths_digest_only": True,
            "directories_recursed": False,
            "draft_partial_wal_lock_deleted": False,
            "frozen_inputs_mutated": False,
        },
    }
    payload["manifest_sha256"] = logical_sha256(payload)
    return payload


def write_manifest(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    output = args.output if args.output.is_absolute() else args.root / args.output
    payload = build_manifest(args.root)
    write_manifest(output, payload)
    git_info = payload["git"]
    print(f"vNext workspace freeze: {output}")
    print(f"manifest_sha256: {payload['manifest_sha256']}")
    print(f"normal_status_records: {git_info['status_normal']['record_count']}")
    print(f"all_untracked_paths: {git_info['all_untracked_path_count']}")
    print(f"frozen_inputs: {len(payload['frozen_inputs'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
