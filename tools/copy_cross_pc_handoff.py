#!/usr/bin/env python3
"""Copy a manifest allowlist between local roots without clobbering files.

The tool deliberately has no discovery or network behavior.  Every copied file
must be named by the manifest, and every byte count and SHA-256 is verified.
Interrupted transfers are retained as ``<name>.handoff-partial`` and may be
resumed only after their complete prefix is compared with the source file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable, Iterable, Mapping


MANIFEST_SCHEMA_VERSION = 1
DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024
PARTIAL_SUFFIX = ".handoff-partial"
SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
WINDOWS_TRANSIENT_RENAME_WINERRORS = frozenset({32, 33})
WINDOWS_RENAME_ATTEMPTS = 20
WINDOWS_RENAME_RETRY_DELAY_SECONDS = 0.5
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class HandoffError(RuntimeError):
    """Raised when a handoff invariant is not satisfied."""


@dataclass(frozen=True)
class ManifestEntry:
    path: str
    bytes: int
    sha256: str
    sqlite_immutable: bool = False

    @property
    def parts(self) -> tuple[str, ...]:
        return tuple(PurePosixPath(self.path).parts)


@dataclass(frozen=True)
class HandoffSummary:
    mode: str
    files_total: int
    files_copied: int
    files_skipped: int
    files_verified: int
    bytes_copied: int
    bytes_resumed: int


Progress = Callable[[str], None]


def _parse_manifest_entry(raw: object, index: int) -> ManifestEntry:
    if not isinstance(raw, Mapping):
        raise HandoffError(f"manifest files[{index}] must be an object")
    allowed_keys = {"path", "bytes", "sha256", "sqlite_immutable"}
    unexpected = sorted(set(raw) - allowed_keys)
    if unexpected:
        raise HandoffError(
            f"manifest files[{index}] has unsupported keys: {unexpected}"
        )

    raw_path = raw.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raise HandoffError(f"manifest files[{index}].path must be a non-empty string")
    if "\\" in raw_path or "\x00" in raw_path or ":" in raw_path:
        raise HandoffError(
            f"manifest files[{index}].path must be a portable relative POSIX path"
        )
    posix_path = PurePosixPath(raw_path)
    windows_path = PureWindowsPath(raw_path)
    if posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive:
        raise HandoffError(f"manifest path is absolute: {raw_path!r}")
    if (
        not posix_path.parts
        or any(part in {"", ".", ".."} for part in posix_path.parts)
        or posix_path.as_posix() != raw_path
    ):
        raise HandoffError(f"manifest path is not canonical and relative: {raw_path!r}")
    if posix_path.name.endswith(PARTIAL_SUFFIX):
        raise HandoffError(
            f"manifest path uses the reserved partial suffix: {raw_path!r}"
        )

    raw_bytes = raw.get("bytes")
    if isinstance(raw_bytes, bool) or not isinstance(raw_bytes, int) or raw_bytes < 0:
        raise HandoffError(f"manifest files[{index}].bytes must be a non-negative integer")
    raw_sha256 = raw.get("sha256")
    if not isinstance(raw_sha256, str) or not _SHA256_RE.fullmatch(raw_sha256):
        raise HandoffError(f"manifest files[{index}].sha256 must contain 64 hex digits")
    sqlite_immutable = raw.get("sqlite_immutable", False)
    if not isinstance(sqlite_immutable, bool):
        raise HandoffError(
            f"manifest files[{index}].sqlite_immutable must be boolean"
        )
    return ManifestEntry(
        path=raw_path,
        bytes=raw_bytes,
        sha256=raw_sha256.lower(),
        sqlite_immutable=sqlite_immutable,
    )


def load_manifest(path: Path | str) -> tuple[ManifestEntry, ...]:
    """Load and strictly validate the file allowlist in *path*."""

    manifest_path = _absolute(path)
    _assert_no_link_components(manifest_path)
    if not _lexists(manifest_path) or _is_linklike(manifest_path):
        raise HandoffError(f"manifest must be a regular non-link file: {manifest_path}")
    try:
        if not stat.S_ISREG(manifest_path.lstat().st_mode):
            raise HandoffError(f"manifest must be a regular file: {manifest_path}")
    except OSError as exc:
        raise HandoffError(f"cannot inspect manifest {manifest_path}: {exc}") from exc
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HandoffError(f"cannot read manifest {manifest_path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise HandoffError("manifest root must be an object")
    schema_version = payload.get("schema_version", MANIFEST_SCHEMA_VERSION)
    if schema_version != MANIFEST_SCHEMA_VERSION:
        raise HandoffError(
            f"unsupported manifest schema_version {schema_version!r}; "
            f"expected {MANIFEST_SCHEMA_VERSION}"
        )
    raw_files = payload.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise HandoffError("manifest files must be a non-empty array")
    entries = tuple(
        _parse_manifest_entry(raw_entry, index)
        for index, raw_entry in enumerate(raw_files)
    )

    # Case folding makes a manifest safe to move between case-sensitive and
    # case-insensitive machines.  Prefix collisions would make one entry a
    # directory while another entry requires it to be a regular file.
    folded_paths: dict[tuple[str, ...], str] = {}
    for entry in entries:
        folded = tuple(part.casefold() for part in entry.parts)
        if folded in folded_paths:
            raise HandoffError(
                f"duplicate manifest paths: {folded_paths[folded]!r} and {entry.path!r}"
            )
        folded_paths[folded] = entry.path
    ordered_paths = sorted(folded_paths)
    for earlier, later in zip(ordered_paths, ordered_paths[1:]):
        if len(earlier) < len(later) and later[: len(earlier)] == earlier:
            raise HandoffError(
                f"manifest file/directory collision: {folded_paths[earlier]!r} and "
                f"{folded_paths[later]!r}"
            )
    return entries


def _absolute(path: Path | str) -> Path:
    # abspath normalizes lexical components without silently following links.
    return Path(os.path.abspath(os.fspath(path)))


def _lexists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _is_linklike(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _assert_no_link_components(path: Path, *, allow_missing_leaf: bool = False) -> None:
    absolute = _absolute(path)
    parts = absolute.parts
    if not parts:
        raise HandoffError(f"cannot validate empty path: {path}")
    current = Path(parts[0])
    for index, part in enumerate(parts[1:], start=1):
        current /= part
        if not _lexists(current):
            if allow_missing_leaf and index == len(parts) - 1:
                return
            # Missing destination parents are created later, so there can be no
            # link component below the first missing component yet.
            return
        if _is_linklike(current):
            raise HandoffError(f"symlink or junction is not allowed: {current}")


def _validated_root(path: Path | str, label: str) -> Path:
    root = _absolute(path)
    _assert_no_link_components(root)
    if not _lexists(root):
        raise HandoffError(f"{label} root does not exist: {root}")
    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise HandoffError(f"cannot inspect {label} root {root}: {exc}") from exc
    if not stat.S_ISDIR(root_stat.st_mode):
        raise HandoffError(f"{label} root is not a directory: {root}")
    return root


def _under_root(root: Path, entry: ManifestEntry) -> Path:
    candidate = root.joinpath(*entry.parts)
    try:
        common = os.path.commonpath((os.fspath(root), os.fspath(candidate)))
    except ValueError as exc:
        raise HandoffError(f"manifest path escapes root: {entry.path!r}") from exc
    if os.path.normcase(common) != os.path.normcase(os.fspath(root)):
        raise HandoffError(f"manifest path escapes root: {entry.path!r}")
    return candidate


def _ensure_destination_parent(root: Path, path: Path) -> None:
    relative_parent = path.parent.relative_to(root)
    current = root
    for part in relative_parent.parts:
        current /= part
        if _lexists(current):
            if _is_linklike(current):
                raise HandoffError(f"symlink or junction is not allowed: {current}")
            try:
                current_stat = current.lstat()
            except OSError as exc:
                raise HandoffError(
                    f"cannot inspect destination directory {current}: {exc}"
                ) from exc
            if not stat.S_ISDIR(current_stat.st_mode):
                raise HandoffError(f"destination parent is not a directory: {current}")
            continue
        try:
            current.mkdir()
        except FileExistsError:
            # Another process may have won the mkdir race.  Validate it instead
            # of trusting it.
            pass
        except OSError as exc:
            raise HandoffError(f"cannot create destination directory {current}: {exc}") from exc
        if _is_linklike(current) or not stat.S_ISDIR(current.lstat().st_mode):
            raise HandoffError(f"unsafe destination directory appeared: {current}")


def _open_readonly(path: Path) -> int:
    _assert_no_link_components(path)
    if not _lexists(path):
        raise HandoffError(f"required file is missing: {path}")
    if _is_linklike(path):
        raise HandoffError(f"symlink or junction is not allowed: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise HandoffError(f"cannot open file read-only {path}: {exc}") from exc
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise HandoffError(f"path is not a regular file: {path}")
    return descriptor


def _sha256_descriptor(descriptor: int, chunk_size: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        chunk = os.read(descriptor, chunk_size)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def _file_signature(file_stat: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
        file_stat.st_dev,
        file_stat.st_ino,
    )


def _validate_open_source(
    source: Path, entry: ManifestEntry, chunk_size: int
) -> tuple[int, tuple[int, int, int, int, int]]:
    descriptor = _open_readonly(source)
    source_stat = os.fstat(descriptor)
    signature = _file_signature(source_stat)
    try:
        if source_stat.st_size != entry.bytes:
            raise HandoffError(
                f"source byte count mismatch for {entry.path}: "
                f"expected {entry.bytes}, got {source_stat.st_size}"
            )
        actual_sha256 = _sha256_descriptor(descriptor, chunk_size)
        if actual_sha256 != entry.sha256:
            raise HandoffError(
                f"source SHA-256 mismatch for {entry.path}: "
                f"expected {entry.sha256}, got {actual_sha256}"
            )
        if _file_signature(os.fstat(descriptor)) != signature:
            raise HandoffError(f"source changed while hashing: {source}")
        return descriptor, signature
    except BaseException:
        os.close(descriptor)
        raise


def _hash_regular_path(path: Path, chunk_size: int) -> tuple[int, str]:
    descriptor = _open_readonly(path)
    try:
        file_stat = os.fstat(descriptor)
        digest = _sha256_descriptor(descriptor, chunk_size)
        return file_stat.st_size, digest
    finally:
        os.close(descriptor)


def _assert_expected_file(path: Path, entry: ManifestEntry, chunk_size: int) -> None:
    actual_bytes, actual_sha256 = _hash_regular_path(path, chunk_size)
    if actual_bytes != entry.bytes or actual_sha256 != entry.sha256:
        raise HandoffError(
            f"destination mismatch for {entry.path}: expected "
            f"{entry.bytes}/{entry.sha256}, got {actual_bytes}/{actual_sha256}"
        )


def _sqlite_sidecars(database: Path) -> Iterable[Path]:
    for suffix in SQLITE_SIDECAR_SUFFIXES:
        yield Path(os.fspath(database) + suffix)


def _assert_sqlite_sidecars_absent(database: Path, label: str) -> None:
    present = [sidecar for sidecar in _sqlite_sidecars(database) if _lexists(sidecar)]
    if present:
        display = ", ".join(os.fspath(sidecar) for sidecar in present)
        raise HandoffError(f"{label} immutable SQLite sidecar(s) present: {display}")


def _open_partial(path: Path) -> tuple[int, bool]:
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    existed = _lexists(path)
    if existed:
        _assert_no_link_components(path)
        if _is_linklike(path):
            raise HandoffError(f"partial file must not be a link: {path}")
    else:
        flags |= os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise HandoffError(f"cannot open partial file {path}: {exc}") from exc
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise HandoffError(f"partial path is not a regular file: {path}")
    return descriptor, existed


def _verify_prefix(
    source_descriptor: int,
    partial_descriptor: int,
    size: int,
    chunk_size: int,
) -> None:
    os.lseek(source_descriptor, 0, os.SEEK_SET)
    os.lseek(partial_descriptor, 0, os.SEEK_SET)
    remaining = size
    while remaining:
        request = min(chunk_size, remaining)
        source_chunk = os.read(source_descriptor, request)
        partial_chunk = os.read(partial_descriptor, request)
        if len(source_chunk) != request or source_chunk != partial_chunk:
            raise HandoffError("partial file is not an exact prefix of the source")
        remaining -= request


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise HandoffError("short write while appending to partial file")
        view = view[written:]


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _windows_exclusive_rename(
    partial: Path,
    destination: Path,
    *,
    max_attempts: int = WINDOWS_RENAME_ATTEMPTS,
    retry_delay_seconds: float = WINDOWS_RENAME_RETRY_DELAY_SECONDS,
    sleeper: Callable[[float], None] = time.sleep,
    rename: Callable[[Path, Path], None] = os.rename,
) -> None:
    """Rename without replacement, retrying only transient Windows locks."""

    if max_attempts <= 0:
        raise HandoffError("Windows rename max_attempts must be positive")
    if retry_delay_seconds < 0:
        raise HandoffError("Windows rename retry_delay_seconds must be non-negative")
    for attempt in range(1, max_attempts + 1):
        # Re-check before every attempt so a file created during a retry delay
        # is never replaced by the promotion.
        if _lexists(destination):
            raise HandoffError(
                f"destination appeared before promotion: {destination}"
            )
        try:
            # Windows os.rename is atomic on the same volume and refuses to
            # replace an existing destination.
            rename(partial, destination)
            return
        except FileExistsError as exc:
            raise HandoffError(
                f"destination appeared before promotion: {destination}"
            ) from exc
        except OSError as exc:
            winerror = getattr(exc, "winerror", None)
            retryable = winerror in WINDOWS_TRANSIENT_RENAME_WINERRORS
            if retryable and attempt < max_attempts:
                sleeper(retry_delay_seconds)
                continue
            attempts = f" after {attempt} attempts" if retryable else ""
            raise HandoffError(
                f"exclusive atomic promotion failed for {destination}{attempts}: {exc}"
            ) from exc


def _exclusive_atomic_promote(partial: Path, destination: Path) -> None:
    if _lexists(destination):
        raise HandoffError(f"destination appeared before promotion: {destination}")
    if os.name == "nt":
        _windows_exclusive_rename(partial, destination)
    else:
        try:
            # POSIX rename replaces its target.  A hard-link publication gives
            # us atomic visibility plus O_EXCL semantics, then the staging name
            # can be removed.  Both names are necessarily on the same volume.
            os.link(partial, destination, follow_symlinks=False)
            os.unlink(partial)
        except FileExistsError as exc:
            raise HandoffError(
                f"destination appeared before promotion: {destination}"
            ) from exc
        except OSError as exc:
            raise HandoffError(
                f"exclusive atomic promotion failed for {destination}: {exc}"
            ) from exc
    _fsync_directory(destination.parent)


def _copy_one(
    source: Path,
    destination: Path,
    destination_root: Path,
    entry: ManifestEntry,
    chunk_size: int,
) -> tuple[str, int, int]:
    if entry.sqlite_immutable:
        _assert_sqlite_sidecars_absent(source, "source")
        _assert_sqlite_sidecars_absent(destination, "destination")
    source_descriptor, source_signature = _validate_open_source(
        source, entry, chunk_size
    )
    partial = destination.with_name(destination.name + PARTIAL_SUFFIX)
    try:
        if entry.sqlite_immutable:
            _assert_sqlite_sidecars_absent(source, "source")
            _assert_sqlite_sidecars_absent(destination, "destination")
        if _lexists(partial):
            _assert_no_link_components(partial)
            if _is_linklike(partial) or not stat.S_ISREG(partial.lstat().st_mode):
                raise HandoffError(f"partial path is not a regular non-link file: {partial}")
        if _lexists(destination):
            _assert_expected_file(destination, entry, chunk_size)
            return "skipped", 0, 0

        _ensure_destination_parent(destination_root, destination)
        # Re-check all destination components after creating the parent tree.
        _assert_no_link_components(destination, allow_missing_leaf=True)
        partial_descriptor, _ = _open_partial(partial)
        try:
            partial_size = os.fstat(partial_descriptor).st_size
            if partial_size > entry.bytes:
                raise HandoffError(
                    f"partial file is longer than its source for {entry.path}: "
                    f"{partial_size} > {entry.bytes}"
                )
            _verify_prefix(
                source_descriptor, partial_descriptor, partial_size, chunk_size
            )
            os.lseek(source_descriptor, partial_size, os.SEEK_SET)
            os.lseek(partial_descriptor, partial_size, os.SEEK_SET)
            bytes_copied = 0
            remaining = entry.bytes - partial_size
            while remaining:
                chunk = os.read(source_descriptor, min(chunk_size, remaining))
                if not chunk:
                    raise HandoffError(f"source ended during copy: {source}")
                _write_all(partial_descriptor, chunk)
                bytes_copied += len(chunk)
                remaining -= len(chunk)
            os.fsync(partial_descriptor)
        finally:
            os.close(partial_descriptor)

        if _file_signature(os.fstat(source_descriptor)) != source_signature:
            raise HandoffError(f"source changed while copying: {source}")
        _assert_expected_file(partial, entry, chunk_size)
        if entry.sqlite_immutable:
            _assert_sqlite_sidecars_absent(source, "source")
            _assert_sqlite_sidecars_absent(destination, "destination")
        _assert_no_link_components(destination, allow_missing_leaf=True)
        _exclusive_atomic_promote(partial, destination)
        _assert_expected_file(destination, entry, chunk_size)
        if entry.sqlite_immutable:
            _assert_sqlite_sidecars_absent(destination, "destination")
        return "copied", bytes_copied, partial_size
    finally:
        os.close(source_descriptor)


def run_handoff(
    *,
    manifest: Path | str,
    source_root: Path | str,
    destination_root: Path | str,
    verify_only: bool = False,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    progress: Progress | None = print,
) -> HandoffSummary:
    """Copy or verify the exact allowlist described by *manifest*."""

    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0:
        raise HandoffError("chunk_size must be a positive integer")
    entries = load_manifest(manifest)
    source_root_path = _validated_root(source_root, "source")
    destination_root_path = _validated_root(destination_root, "destination")

    copied = skipped = verified = copied_bytes = resumed_bytes = 0
    for entry in entries:
        source = _under_root(source_root_path, entry)
        destination = _under_root(destination_root_path, entry)
        _assert_no_link_components(source)
        partial = destination.with_name(destination.name + PARTIAL_SUFFIX)
        if verify_only:
            if entry.sqlite_immutable:
                _assert_sqlite_sidecars_absent(source, "source")
                _assert_sqlite_sidecars_absent(destination, "destination")
            source_descriptor, _ = _validate_open_source(source, entry, chunk_size)
            os.close(source_descriptor)
            if entry.sqlite_immutable:
                _assert_sqlite_sidecars_absent(source, "source")
                _assert_sqlite_sidecars_absent(destination, "destination")
            if _lexists(partial):
                raise HandoffError(f"incomplete partial exists during verification: {partial}")
            _assert_expected_file(destination, entry, chunk_size)
            verified += 1
            if progress:
                progress(f"VERIFY {entry.path} bytes={entry.bytes} sha256={entry.sha256}")
            continue

        status, new_bytes, prior_bytes = _copy_one(
            source, destination, destination_root_path, entry, chunk_size
        )
        verified += 1
        copied_bytes += new_bytes
        resumed_bytes += prior_bytes
        if status == "copied":
            copied += 1
            if progress:
                progress(
                    f"COPY {entry.path} bytes={entry.bytes} resumed={prior_bytes} "
                    f"sha256={entry.sha256}"
                )
        else:
            skipped += 1
            if progress:
                progress(
                    f"SKIP {entry.path} existing file matches bytes/SHA-256"
                )

    summary = HandoffSummary(
        mode="verify" if verify_only else "copy",
        files_total=len(entries),
        files_copied=copied,
        files_skipped=skipped,
        files_verified=verified,
        bytes_copied=copied_bytes,
        bytes_resumed=resumed_bytes,
    )
    if progress:
        progress(
            "SUMMARY "
            f"mode={summary.mode} total={summary.files_total} "
            f"copied={summary.files_copied} skipped={summary.files_skipped} "
            f"verified={summary.files_verified} bytes_copied={summary.bytes_copied} "
            f"bytes_resumed={summary.bytes_resumed}"
        )
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Copy a SHA-256 manifest allowlist between two local roots."
    )
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--destination-root", required=True, type=Path)
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="verify source and destination without creating or changing files",
    )
    parser.add_argument(
        "--chunk-mib",
        type=int,
        default=DEFAULT_CHUNK_SIZE // (1024 * 1024),
        help="streaming buffer size in MiB (default: 8)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.chunk_mib <= 0:
        parser.error("--chunk-mib must be positive")
    try:
        run_handoff(
            manifest=args.manifest,
            source_root=args.source_root,
            destination_root=args.destination_root,
            verify_only=args.verify_only,
            chunk_size=args.chunk_mib * 1024 * 1024,
        )
    except HandoffError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
