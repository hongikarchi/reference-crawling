from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from tools.build_vnext_workspace_freeze import (
    FrozenInputSpec,
    build_manifest,
    classify_enrichment_entry,
    sha256_file,
    write_manifest,
)
from tools.validate_vnext_workspace_freeze import validate_manifest


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _sqlite(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO sample(value) VALUES ('ok')")
        connection.commit()
    finally:
        connection.close()


def _repo(tmp_path: Path) -> tuple[Path, tuple[FrozenInputSpec, ...]]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    tracked = root / "tracked.txt"
    tracked.write_text("tracked\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-m", "init")
    specs: list[FrozenInputSpec] = []
    for index in range(6):
        frozen = root / "data" / "enrichment" / f"accepted_{index}.db"
        _sqlite(frozen)
        specs.append(
            FrozenInputSpec(
                role=f"test_input_{index}",
                path=f"data/enrichment/accepted_{index}.db",
                expected_size_bytes=frozen.stat().st_size,
                expected_sha256=sha256_file(frozen),
            )
        )
    (root / "scratch.txt").write_text("untracked\n", encoding="utf-8")
    return root, tuple(specs)


def test_build_and_validate_manifest(tmp_path: Path) -> None:
    root, specs = _repo(tmp_path)
    manifest_path = root / "data" / "reports" / "vnext_workspace_freeze_v1.json"
    payload = build_manifest(root, specs=specs)
    write_manifest(manifest_path, payload)

    report = validate_manifest(root, manifest_path)

    assert report["status"] == "PASS"
    assert payload["git"]["all_untracked_path_count"] == 7
    scratch = next(row for row in payload["workspace_entries"] if row["path"] == "scratch.txt")
    assert scratch["size_bytes"] == (root / "scratch.txt").stat().st_size
    assert "sha256" not in scratch


def test_validator_detects_frozen_input_tamper(tmp_path: Path) -> None:
    root, specs = _repo(tmp_path)
    manifest_path = root / "data" / "reports" / "vnext_workspace_freeze_v1.json"
    payload = build_manifest(root, specs=specs)
    write_manifest(manifest_path, payload)
    (root / specs[0].path).write_bytes(b"tampered")

    report = validate_manifest(root, manifest_path)

    assert report["status"] == "FAIL"
    assert any(row["name"].startswith("frozen_size:") for row in report["failures"])


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("accepted.db", "accepted_frozen_input"),
        ("run.db.partial", "partial_or_runtime_residue"),
        ("run.db-wal", "partial_or_runtime_residue"),
        ("semantic_vision_n10.db", "experimental_evidence_only"),
        ("divisare_architizer_image_evidence_e2_full_v3.db", "rejected_or_superseded_draft"),
    ],
)
def test_enrichment_classification(name: str, expected: str) -> None:
    assert classify_enrichment_entry(name, {"accepted.db"}) == expected
