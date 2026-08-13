from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from tools.copy_cross_pc_handoff import (
    HandoffError,
    PARTIAL_SUFFIX,
    _windows_exclusive_rename,
    load_manifest,
    main,
    run_handoff,
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _windows_error(winerror: int) -> OSError:
    return OSError(13, "simulated Windows lock", None, winerror)


def _manifest(
    path: Path,
    files: dict[str, bytes],
    *,
    sqlite_paths: set[str] | None = None,
) -> Path:
    sqlite_paths = sqlite_paths or set()
    payload = {
        "schema_version": 1,
        "files": [
            {
                "path": relative,
                "bytes": len(content),
                "sha256": _sha256(content),
                **(
                    {"sqlite_immutable": True}
                    if relative in sqlite_paths
                    else {}
                ),
            }
            for relative, content in files.items()
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    return source, destination


def test_copies_only_manifest_allowlist_and_reports_progress(tmp_path: Path) -> None:
    source, destination = _roots(tmp_path)
    files = {
        "nested/a.bin": b"a" * 37,
        "empty.dat": b"",
    }
    for relative, content in files.items():
        source_path = source / Path(relative)
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(content)
    (source / "not-allowlisted.txt").write_text("do not copy", encoding="utf-8")
    manifest = _manifest(tmp_path / "manifest.json", files)
    messages: list[str] = []

    summary = run_handoff(
        manifest=manifest,
        source_root=source,
        destination_root=destination,
        chunk_size=7,
        progress=messages.append,
    )

    assert summary.files_total == 2
    assert summary.files_copied == 2
    assert summary.files_verified == 2
    assert summary.bytes_copied == 37
    assert (destination / "nested/a.bin").read_bytes() == files["nested/a.bin"]
    assert (destination / "empty.dat").read_bytes() == b""
    assert not (destination / "not-allowlisted.txt").exists()
    assert not list(destination.rglob(f"*{PARTIAL_SUFFIX}"))
    assert messages[-1].startswith("SUMMARY mode=copy total=2")


def test_restart_verifies_complete_prefix_then_appends(tmp_path: Path) -> None:
    source, destination = _roots(tmp_path)
    payload = bytes(range(256)) * 8
    (source / "large.bin").write_bytes(payload)
    manifest = _manifest(tmp_path / "manifest.json", {"large.bin": payload})
    partial = destination / f"large.bin{PARTIAL_SUFFIX}"
    partial.write_bytes(payload[:777])

    summary = run_handoff(
        manifest=manifest,
        source_root=source,
        destination_root=destination,
        chunk_size=31,
        progress=None,
    )

    assert summary.files_copied == 1
    assert summary.bytes_resumed == 777
    assert summary.bytes_copied == len(payload) - 777
    assert (destination / "large.bin").read_bytes() == payload
    assert not partial.exists()


def test_windows_promotion_retries_transient_sharing_violations(
    tmp_path: Path,
) -> None:
    partial = tmp_path / f"artifact.db{PARTIAL_SUFFIX}"
    destination = tmp_path / "artifact.db"
    partial.write_bytes(b"complete artifact")
    actual_rename = os.rename
    calls = 0
    sleeps: list[float] = []

    def flaky_rename(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise _windows_error(32)
        actual_rename(source, target)

    _windows_exclusive_rename(
        partial,
        destination,
        max_attempts=5,
        retry_delay_seconds=0.5,
        sleeper=sleeps.append,
        rename=flaky_rename,
    )

    assert calls == 3
    assert sleeps == [0.5, 0.5]
    assert not partial.exists()
    assert destination.read_bytes() == b"complete artifact"


def test_windows_promotion_retry_exhaustion_preserves_partial(
    tmp_path: Path,
) -> None:
    partial = tmp_path / f"artifact.db{PARTIAL_SUFFIX}"
    destination = tmp_path / "artifact.db"
    payload = b"complete artifact"
    partial.write_bytes(payload)
    calls = 0
    sleeps: list[float] = []

    def always_locked(source: Path, target: Path) -> None:
        nonlocal calls
        del source, target
        calls += 1
        raise _windows_error(33)

    with pytest.raises(HandoffError, match="after 3 attempts"):
        _windows_exclusive_rename(
            partial,
            destination,
            max_attempts=3,
            retry_delay_seconds=0.25,
            sleeper=sleeps.append,
            rename=always_locked,
        )

    assert calls == 3
    assert sleeps == [0.25, 0.25]
    assert partial.read_bytes() == payload
    assert not destination.exists()


def test_windows_promotion_does_not_retry_nontransient_error(
    tmp_path: Path,
) -> None:
    partial = tmp_path / f"artifact.db{PARTIAL_SUFFIX}"
    destination = tmp_path / "artifact.db"
    partial.write_bytes(b"complete artifact")
    calls = 0
    sleeps: list[float] = []

    def access_denied(source: Path, target: Path) -> None:
        nonlocal calls
        del source, target
        calls += 1
        raise _windows_error(5)

    with pytest.raises(HandoffError, match="exclusive atomic promotion failed"):
        _windows_exclusive_rename(
            partial,
            destination,
            sleeper=sleeps.append,
            rename=access_denied,
        )

    assert calls == 1
    assert sleeps == []
    assert partial.exists()
    assert not destination.exists()


def test_corrupt_or_oversized_partial_is_never_rewritten(tmp_path: Path) -> None:
    source, destination = _roots(tmp_path)
    payload = b"trusted source bytes"
    (source / "item.bin").write_bytes(payload)
    manifest = _manifest(tmp_path / "manifest.json", {"item.bin": payload})
    partial = destination / f"item.bin{PARTIAL_SUFFIX}"
    partial.write_bytes(b"corrupt")

    with pytest.raises(HandoffError, match="not an exact prefix"):
        run_handoff(
            manifest=manifest,
            source_root=source,
            destination_root=destination,
            progress=None,
        )
    assert partial.read_bytes() == b"corrupt"
    assert not (destination / "item.bin").exists()

    partial.write_bytes(payload + b"too long")
    with pytest.raises(HandoffError, match="longer than its source"):
        run_handoff(
            manifest=manifest,
            source_root=source,
            destination_root=destination,
            progress=None,
        )
    assert partial.read_bytes() == payload + b"too long"


def test_no_clobber_skips_equal_and_refuses_mismatch(tmp_path: Path) -> None:
    source, destination = _roots(tmp_path)
    payload = b"same"
    (source / "item").write_bytes(payload)
    final = destination / "item"
    final.write_bytes(payload)
    manifest = _manifest(tmp_path / "manifest.json", {"item": payload})

    summary = run_handoff(
        manifest=manifest,
        source_root=source,
        destination_root=destination,
        progress=None,
    )
    assert summary.files_skipped == 1
    assert summary.files_copied == 0

    final.write_bytes(b"different")
    with pytest.raises(HandoffError, match="destination mismatch"):
        run_handoff(
            manifest=manifest,
            source_root=source,
            destination_root=destination,
            progress=None,
        )
    assert final.read_bytes() == b"different"


@pytest.mark.parametrize(
    "relative",
    ["../escape", "/absolute", "C:/drive", "a\\windows", "a//b", "a/./b"],
)
def test_manifest_refuses_noncanonical_or_escaping_paths(
    tmp_path: Path, relative: str
) -> None:
    manifest = _manifest(tmp_path / "manifest.json", {relative: b"x"})
    with pytest.raises(HandoffError):
        load_manifest(manifest)


def test_manifest_refuses_duplicates_prefix_collisions_and_reserved_suffix(
    tmp_path: Path,
) -> None:
    payload = {
        "schema_version": 1,
        "files": [
            {"path": "A", "bytes": 0, "sha256": _sha256(b"")},
            {"path": "a/child", "bytes": 0, "sha256": _sha256(b"")},
        ],
    }
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(HandoffError, match="collision"):
        load_manifest(manifest)

    reserved = _manifest(
        tmp_path / "reserved.json", {f"file{PARTIAL_SUFFIX}": b"x"}
    )
    with pytest.raises(HandoffError, match="reserved"):
        load_manifest(reserved)


def test_source_file_and_destination_parent_symlinks_are_refused(
    tmp_path: Path,
) -> None:
    source, destination = _roots(tmp_path)
    real = source / "real"
    real.write_bytes(b"payload")
    source_link = source / "linked"
    try:
        source_link.symlink_to(real)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks unavailable in this test environment: {exc}")
    manifest = _manifest(tmp_path / "source-link.json", {"linked": b"payload"})
    with pytest.raises(HandoffError, match="symlink|junction"):
        run_handoff(
            manifest=manifest,
            source_root=source,
            destination_root=destination,
            progress=None,
        )

    (source / "nested").mkdir()
    (source / "nested/file").write_bytes(b"payload")
    outside = tmp_path / "outside"
    outside.mkdir()
    destination_link = destination / "nested"
    destination_link.symlink_to(outside, target_is_directory=True)
    nested_manifest = _manifest(
        tmp_path / "destination-link.json", {"nested/file": b"payload"}
    )
    with pytest.raises(HandoffError, match="symlink|junction"):
        run_handoff(
            manifest=nested_manifest,
            source_root=source,
            destination_root=destination,
            progress=None,
        )
    assert not (outside / "file").exists()


def test_verify_only_requires_complete_destination_and_never_writes(
    tmp_path: Path,
) -> None:
    source, destination = _roots(tmp_path)
    payload = b"verify me"
    (source / "item").write_bytes(payload)
    manifest = _manifest(tmp_path / "manifest.json", {"item": payload})

    with pytest.raises(HandoffError, match="required file is missing"):
        run_handoff(
            manifest=manifest,
            source_root=source,
            destination_root=destination,
            verify_only=True,
            progress=None,
        )
    assert list(destination.iterdir()) == []

    run_handoff(
        manifest=manifest,
        source_root=source,
        destination_root=destination,
        progress=None,
    )
    final_mtime = (destination / "item").stat().st_mtime_ns
    summary = run_handoff(
        manifest=manifest,
        source_root=source,
        destination_root=destination,
        verify_only=True,
        progress=None,
    )
    assert summary.mode == "verify"
    assert summary.files_verified == 1
    assert summary.files_copied == 0
    assert (destination / "item").stat().st_mtime_ns == final_mtime

    (destination / f"item{PARTIAL_SUFFIX}").write_bytes(b"")
    with pytest.raises(HandoffError, match="incomplete partial"):
        run_handoff(
            manifest=manifest,
            source_root=source,
            destination_root=destination,
            verify_only=True,
            progress=None,
        )


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_immutable_sqlite_refuses_source_and_destination_sidecars(
    tmp_path: Path, suffix: str
) -> None:
    source, destination = _roots(tmp_path)
    database = b"SQLite format 3\x00fixture"
    source_db = source / "database.db"
    source_db.write_bytes(database)
    manifest = _manifest(
        tmp_path / "manifest.json",
        {"database.db": database},
        sqlite_paths={"database.db"},
    )
    source_sidecar = Path(os.fspath(source_db) + suffix)
    source_sidecar.write_bytes(b"active writer")
    with pytest.raises(HandoffError, match="source immutable SQLite sidecar"):
        run_handoff(
            manifest=manifest,
            source_root=source,
            destination_root=destination,
            progress=None,
        )
    source_sidecar.unlink()

    destination_sidecar = Path(os.fspath(destination / "database.db") + suffix)
    destination_sidecar.write_bytes(b"stale")
    with pytest.raises(HandoffError, match="destination immutable SQLite sidecar"):
        run_handoff(
            manifest=manifest,
            source_root=source,
            destination_root=destination,
            progress=None,
        )
    assert not (destination / "database.db").exists()


def test_source_size_or_sha_tamper_fails_before_destination_write(
    tmp_path: Path,
) -> None:
    source, destination = _roots(tmp_path)
    original = b"original"
    source_file = source / "file"
    source_file.write_bytes(original)
    manifest = _manifest(tmp_path / "manifest.json", {"file": original})
    source_file.write_bytes(b"tampered")

    with pytest.raises(HandoffError, match="SHA-256 mismatch"):
        run_handoff(
            manifest=manifest,
            source_root=source,
            destination_root=destination,
            progress=None,
        )
    assert list(destination.iterdir()) == []


def test_cli_returns_nonzero_on_validation_failure(tmp_path: Path) -> None:
    source, destination = _roots(tmp_path)
    (source / "file").write_bytes(b"wrong")
    manifest = _manifest(tmp_path / "manifest.json", {"file": b"expected"})

    assert (
        main(
            [
                "--manifest",
                os.fspath(manifest),
                "--source-root",
                os.fspath(source),
                "--destination-root",
                os.fspath(destination),
            ]
        )
        == 2
    )
    assert list(destination.iterdir()) == []
