#!/usr/bin/env python3
"""Build a no-clobber transfer manifest for the current image pipeline.

Only the explicitly approved E1/E2/E3/semantic artifacts are inventoried.
The source metadata databases are deliberately recorded as external
requirements and are not part of the transfer set.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Sequence


MANIFEST_VERSION = "archibe-current-image-pipeline-handoff-v1"
COPY_TOOL_SCHEMA_VERSION = 1


class HandoffManifestError(RuntimeError):
    """Raised when the handoff inventory cannot be frozen safely."""


@dataclass(frozen=True)
class FileSpec:
    path: str
    role: str
    category: str
    expected_size_bytes: int | None = None
    expected_sha256: str | None = None
    allow_persistent_lock: bool = False


@dataclass(frozen=True)
class DirectorySpec:
    path: str
    role: str
    category: str


@dataclass(frozen=True)
class ExternalRequirement:
    path: str
    role: str
    expected_size_bytes: int
    expected_sha256: str
    transfer_policy: str = "required_on_destination_not_included"


PRIMARY_FILES: tuple[FileSpec, ...] = (
    FileSpec(
        "data/enrichment/divisare_image_fingerprints_e1_full_v1_2.db",
        "divisare_e1_v1_2",
        "primary_database",
        2_646_114_304,
        "869a79fee9fd65ddeffa299fef4dd9e2ba15a9c7c7170964b03fee1f4c96a819",
    ),
    FileSpec(
        "data/enrichment/architizer_image_fingerprints_e1_full_v1_2.db",
        "architizer_e1_v1_2",
        "primary_database",
        4_373_962_752,
        "58aecdcda936f7327ef7bb4bf3fe21a39ad070e784ab7061e989b62c2dcfe937",
    ),
    FileSpec(
        "data/enrichment/divisare_architizer_image_evidence_e2_full_v5.db",
        "cross_source_e2_full_v5",
        "primary_database",
        10_164_682_752,
        "4728f9f015d7fba303923df40f550c59835a7e2a5c1316eef03c499ee3ca7b19",
    ),
    FileSpec(
        "data/enrichment/divisare_architizer_image_selection_e3_full_v1.db",
        "cross_source_e3_full_v1",
        "primary_database",
        10_236_592_128,
        "8512e11f8e1fd581038f790b27a67c0a8b1949067bf53b3ef30c4ea3534141a4",
    ),
    FileSpec(
        "data/enrichment/divisare_architizer_semantic_vision_n10_v1.db",
        "semantic_vision_v1_n10",
        "primary_database",
        835_584,
        "30cfdce39b8ac0ecc0d1de0b52f05a5f1f5d7bec7390c03439096118e08ee31a",
    ),
    FileSpec(
        "data/enrichment/divisare_architizer_semantic_vision_n100_v1.db",
        "semantic_vision_v1_n100",
        "primary_database",
        7_106_560,
        "e2a03e004a27ce662a93f60ad16190014789abf60f9433926ff9e38e747c8a8e",
    ),
    FileSpec(
        "data/enrichment/divisare_architizer_semantic_vision_n10_v1_1.db",
        "semantic_vision_v1_1_n10",
        "primary_database",
        888_832,
        "03d99eb3b624a9211154643a3c29fca337f763442fb086b76d25bca2ee106172",
    ),
    FileSpec(
        "data/enrichment/divisare_architizer_semantic_vision_n10_v1_2.db",
        "semantic_vision_v1_2_n10",
        "primary_database",
        851_968,
        "3b37136dae709c1d47945e40a0596600b18b4cafee6fb64f8fbd872ea34e7c95",
    ),
)


PERSISTENT_LOCKS: tuple[FileSpec, ...] = (
    FileSpec(
        "data/enrichment/divisare_architizer_semantic_vision_n100_v1.db.lock",
        "semantic_vision_v1_n100_persistent_lock",
        "persistent_lock",
        allow_persistent_lock=True,
    ),
    FileSpec(
        "data/enrichment/divisare_architizer_semantic_vision_n10_v1_1.db.lock",
        "semantic_vision_v1_1_n10_persistent_lock",
        "persistent_lock",
        allow_persistent_lock=True,
    ),
    FileSpec(
        "data/enrichment/divisare_architizer_semantic_vision_n10_v1_2.db.lock",
        "semantic_vision_v1_2_n10_persistent_lock",
        "persistent_lock",
        allow_persistent_lock=True,
    ),
)


_REPORT_NAMES = (
    "cross_source_semantic_coverage_n10_v1.json",
    "cross_source_semantic_coverage_n100_v1.json",
    "cross_source_semantic_coverage_n10_v1_1.json",
    "cross_source_semantic_coverage_n10_v1_2.json",
    "divisare_architizer_semantic_vision_n10_v1.md",
    "divisare_architizer_semantic_vision_n10_v1_blind_qa.md",
    "divisare_architizer_semantic_vision_n100_v1.md",
    "divisare_architizer_semantic_vision_n100_v1_agent_reference_frozen.json",
    "divisare_architizer_semantic_vision_n100_v1_inspection.json",
    "divisare_architizer_semantic_vision_n10_v1_1.md",
    "divisare_architizer_semantic_vision_n10_v1_1_agent_reference_frozen.json",
    "divisare_architizer_semantic_vision_n10_v1_1_inspection.json",
    "divisare_architizer_semantic_vision_n10_v1_2.md",
    "divisare_architizer_semantic_vision_n10_v1_2_agent_reference_a_frozen.json",
    "divisare_architizer_semantic_vision_n10_v1_2_agent_reference_b_frozen.json",
    "divisare_architizer_semantic_vision_n10_v1_2_reconciled_agent_reference_frozen.json",
    "divisare_architizer_semantic_vision_n10_v1_2_inspection.json",
)

SUPPORTING_FILES: tuple[FileSpec, ...] = tuple(
    FileSpec(
        f"data/reports/{name}",
        f"semantic_report:{name}",
        "report_or_manifest",
    )
    for name in _REPORT_NAMES
)


RECURSIVE_DIRECTORIES: tuple[DirectorySpec, ...] = (
    DirectorySpec(
        "data/enrichment/divisare_architizer_semantic_vision_n10_v1_1_review_cache",
        "semantic_vision_v1_1_n10_review_cache",
        "review_cache",
    ),
    DirectorySpec(
        "data/enrichment/divisare_architizer_semantic_vision_n10_v1_1_review_sheets",
        "semantic_vision_v1_1_n10_review_sheets",
        "review_sheet",
    ),
    DirectorySpec(
        "data/enrichment/divisare_architizer_semantic_vision_n10_v1_2_review_cache",
        "semantic_vision_v1_2_n10_review_cache",
        "review_cache",
    ),
    DirectorySpec(
        "data/enrichment/divisare_architizer_semantic_vision_n10_v1_2_review_sheets",
        "semantic_vision_v1_2_n10_review_sheets",
        "review_sheet",
    ),
)


EXTERNAL_REQUIREMENTS: tuple[ExternalRequirement, ...] = (
    ExternalRequirement(
        "data/curated/divisare_metadata_v2_4.db",
        "divisare_curated_metadata_v2_4",
        2_225_299_456,
        "9c523f3393d20ae8732677981c207abd02247ca5ce905dec422a05fa0398f70f",
    ),
    ExternalRequirement(
        "data/curated/architizer_curated_v2_0.db",
        "architizer_curated_v2_0",
        8_767_438_848,
        "605f4f534fc74267d49ea0b7b3f9b3bed6b55acc3a44a18ae7cafdc53633fbbc",
    ),
)


_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_GIT_OBJECT = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
_FORBIDDEN_EXACT_NAMES = {
    ".env",
    "auth.json",
    "credentials.json",
    "cookies.json",
}
_FORBIDDEN_NAME_FRAGMENTS = (
    "credential",
    "secret",
    "access_token",
    "refresh_token",
    "cookie",
)
_FORBIDDEN_SUFFIXES = (".log", ".tmp", ".temp", ".partial", ".wal", ".shm", ".journal")
_FORBIDDEN_ENDINGS = ("-wal", "-shm", "-journal")
_FORBIDDEN_DIRECTORY_NAMES = {"log", "logs", "tmp", "temp", "temporary"}


def _canonical_relative_path(value: str) -> PurePosixPath:
    if not value or "\\" in value:
        raise HandoffManifestError(f"non-canonical artifact path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or value.startswith("./") or ".." in path.parts:
        raise HandoffManifestError(f"unsafe artifact path: {value!r}")
    if path.as_posix() != value:
        raise HandoffManifestError(f"non-canonical artifact path: {value!r}")
    return path


def _repo_path(repo_root: Path, relative: str) -> Path:
    pure = _canonical_relative_path(relative)
    root = repo_root.resolve()
    candidate = root.joinpath(*pure.parts)
    try:
        candidate.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise HandoffManifestError(f"artifact escapes repository: {relative}") from exc
    return candidate


def _forbidden_reason(relative: str, *, allow_persistent_lock: bool = False) -> str | None:
    pure = _canonical_relative_path(relative)
    names = [part.casefold() for part in pure.parts]
    basename = names[-1]
    if any(name in _FORBIDDEN_EXACT_NAMES for name in names):
        return "credential or environment file"
    if any(fragment in name for name in names for fragment in _FORBIDDEN_NAME_FRAGMENTS):
        return "credential-like filename"
    if any(name.startswith(".") for name in names):
        return "hidden file or directory"
    if any(name in _FORBIDDEN_DIRECTORY_NAMES for name in names[:-1]):
        return "log or temporary directory"
    if allow_persistent_lock and basename.endswith(".db.lock"):
        return None
    if basename.endswith(".lock"):
        return "non-allowlisted lock"
    if basename.endswith(_FORBIDDEN_SUFFIXES) or basename.endswith(_FORBIDDEN_ENDINGS):
        return "log, temporary, or SQLite transient sidecar"
    return None


def _sha256_stable_file(path: Path) -> tuple[int, str]:
    if path.is_symlink():
        raise HandoffManifestError(f"symlinks are not transferable artifacts: {path}")
    if not path.is_file():
        raise HandoffManifestError(f"required artifact is missing: {path}")
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise HandoffManifestError(f"artifact changed while hashing: {path}")
    return after.st_size, digest.hexdigest()


def _validate_expected_identity(spec: FileSpec, size_bytes: int, sha256: str) -> bool:
    if (spec.expected_size_bytes is None) != (spec.expected_sha256 is None):
        raise HandoffManifestError(f"incomplete pinned identity for {spec.path}")
    if spec.expected_sha256 is None:
        return False
    expected_sha = spec.expected_sha256.casefold()
    if not _HEX_64.fullmatch(expected_sha):
        raise HandoffManifestError(f"invalid pinned SHA-256 for {spec.path}")
    if size_bytes != spec.expected_size_bytes or sha256 != expected_sha:
        raise HandoffManifestError(
            f"pinned identity mismatch for {spec.path}: "
            f"expected {spec.expected_size_bytes}/{expected_sha}, "
            f"observed {size_bytes}/{sha256}"
        )
    return True


def _file_entry(repo_root: Path, spec: FileSpec) -> dict[str, Any]:
    reason = _forbidden_reason(spec.path, allow_persistent_lock=spec.allow_persistent_lock)
    if reason is not None:
        raise HandoffManifestError(f"forbidden artifact ({reason}): {spec.path}")
    size_bytes, sha256 = _sha256_stable_file(_repo_path(repo_root, spec.path))
    pinned = _validate_expected_identity(spec, size_bytes, sha256)
    entry: dict[str, Any] = {
        "path": spec.path,
        "role": spec.role,
        "category": spec.category,
        "size_bytes": size_bytes,
        "sha256": sha256,
        "pinned_identity_validated": pinned,
    }
    if pinned:
        entry["expected_size_bytes"] = spec.expected_size_bytes
        entry["expected_sha256"] = spec.expected_sha256.casefold()  # type: ignore[union-attr]
    return entry


def _directory_entries(repo_root: Path, spec: DirectorySpec) -> list[dict[str, Any]]:
    root = _repo_path(repo_root, spec.path)
    if root.is_symlink() or not root.is_dir():
        raise HandoffManifestError(f"required review directory is missing or unsafe: {spec.path}")
    entries: list[dict[str, Any]] = []
    descendants = sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
    for candidate in descendants:
        if candidate.is_symlink():
            raise HandoffManifestError(f"symlink inside review directory: {candidate}")
        if not candidate.is_file():
            continue
        relative = candidate.relative_to(repo_root.resolve()).as_posix()
        reason = _forbidden_reason(relative)
        if reason is not None:
            raise HandoffManifestError(f"forbidden artifact ({reason}): {relative}")
        size_bytes, sha256 = _sha256_stable_file(candidate)
        entries.append(
            {
                "path": relative,
                "role": spec.role,
                "category": spec.category,
                "size_bytes": size_bytes,
                "sha256": sha256,
                "pinned_identity_validated": False,
            }
        )
    if not entries:
        raise HandoffManifestError(f"required review directory is empty: {spec.path}")
    return entries


def _git_rev_parse(repo_root: Path, reference: str) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", reference],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    value = completed.stdout.strip()
    if completed.returncode != 0 or not _GIT_OBJECT.fullmatch(value):
        detail = completed.stderr.strip() or value or f"exit {completed.returncode}"
        raise HandoffManifestError(f"cannot resolve git {reference}: {detail}")
    return value.casefold()


def _read_git_state(repo_root: Path) -> dict[str, str]:
    return {
        "head": _git_rev_parse(repo_root, "HEAD"),
        "origin_main": _git_rev_parse(repo_root, "origin/main"),
    }


def _validate_external_requirements(
    requirements: Iterable[ExternalRequirement],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for requirement in requirements:
        _canonical_relative_path(requirement.path)
        sha256 = requirement.expected_sha256.casefold()
        if requirement.expected_size_bytes < 0 or not _HEX_64.fullmatch(sha256):
            raise HandoffManifestError(
                f"invalid external requirement identity: {requirement.path}"
            )
        output.append(
            {
                "path": requirement.path,
                "role": requirement.role,
                "expected_size_bytes": requirement.expected_size_bytes,
                "expected_sha256": sha256,
                "transfer_policy": requirement.transfer_policy,
            }
        )
    return output


def _totals(entries: Sequence[dict[str, Any]]) -> dict[str, Any]:
    by_category: dict[str, dict[str, int]] = {}
    for entry in entries:
        category = str(entry["category"])
        aggregate = by_category.setdefault(category, {"file_count": 0, "size_bytes": 0})
        aggregate["file_count"] += 1
        aggregate["size_bytes"] += int(entry["size_bytes"])
    return {
        "file_count": len(entries),
        "size_bytes": sum(int(entry["size_bytes"]) for entry in entries),
        "by_category": dict(sorted(by_category.items())),
    }


def _copy_tool_files(entries: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for entry in entries:
        item: dict[str, Any] = {
            "path": str(entry["path"]),
            "bytes": int(entry["size_bytes"]),
            "sha256": str(entry["sha256"]),
        }
        if (
            entry["category"] == "primary_database"
            and PurePosixPath(str(entry["path"])).suffix.casefold() == ".db"
        ):
            item["sqlite_immutable"] = True
        files.append(item)
    return files


def build_current_image_handoff_manifest(
    *,
    repo_root: Path,
    output_path: Path,
    now: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Hash the allowlisted artifact set and publish one JSON manifest."""

    root = repo_root.resolve()
    if not root.is_dir():
        raise HandoffManifestError(f"repository root does not exist: {repo_root}")
    if not output_path.parent.is_dir():
        raise HandoffManifestError(
            f"output parent must already exist (no implicit writes): {output_path.parent}"
        )
    if output_path.exists():
        raise HandoffManifestError(f"manifest already exists: {output_path}")

    entries = [_file_entry(root, spec) for spec in PRIMARY_FILES]
    entries.extend(_file_entry(root, spec) for spec in PERSISTENT_LOCKS)
    entries.extend(_file_entry(root, spec) for spec in SUPPORTING_FILES)
    for spec in RECURSIVE_DIRECTORIES:
        entries.extend(_directory_entries(root, spec))
    entries.sort(key=lambda entry: str(entry["path"]))

    paths = [str(entry["path"]) for entry in entries]
    duplicates = sorted({path for path in paths if paths.count(path) > 1})
    if duplicates:
        raise HandoffManifestError(f"duplicate allowlisted artifacts: {duplicates}")

    clock = now or (lambda: datetime.now(timezone.utc))
    generated_at = clock()
    if generated_at.tzinfo is None:
        raise HandoffManifestError("manifest timestamp must be timezone-aware")
    payload: dict[str, Any] = {
        "schema_version": COPY_TOOL_SCHEMA_VERSION,
        "manifest_version": MANIFEST_VERSION,
        "generated_at_utc": generated_at.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "repository": _read_git_state(root),
        "artifact_scope": {
            "pipeline_stages": [
                "E1 image fingerprints v1.2 (Divisare and Architizer)",
                "E2 cross-source image evidence full v5",
                "E3 image selection full v1",
                "semantic Vision calibration artifacts through v1.2 N10",
            ],
            "selection_policy": "explicit_allowlist_only",
            "directory_policy": (
                "recursive_individual_file_inventory_for_v1_1_and_v1_2_review_material"
            ),
            "excluded": [
                "curated/source metadata databases (external requirements only)",
                "logs and temporary files",
                "SQLite WAL/SHM/journal files",
                "credentials, cookies, environment files, and secrets",
                "rejected or superseded E1/E2/E3 drafts",
            ],
        },
        "artifacts": entries,
        "files": _copy_tool_files(entries),
        "external_requirements": _validate_external_requirements(EXTERNAL_REQUIREMENTS),
        "totals": _totals(entries),
    }

    try:
        with output_path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
    except FileExistsError as exc:
        raise HandoffManifestError(f"manifest already exists: {output_path}") from exc
    return payload


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="reference-crawling repository root",
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        manifest = build_current_image_handoff_manifest(
            repo_root=args.repo_root,
            output_path=args.output,
        )
    except (HandoffManifestError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "output": str(args.output),
                "file_count": manifest["totals"]["file_count"],
                "size_bytes": manifest["totals"]["size_bytes"],
                "head": manifest["repository"]["head"],
                "origin_main": manifest["repository"]["origin_main"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
