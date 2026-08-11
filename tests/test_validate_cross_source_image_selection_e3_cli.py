from __future__ import annotations

import json
from pathlib import Path

import pytest

from canonical.cross_source_image_selection_sidecar import (
    initialize_sidecar,
    recover_sidecar,
)
from tools import validate_cross_source_image_selection_e3 as cli
from tests.test_cross_source_image_selection_validator import _build_valid_fixture


def test_json_safe_is_recursive_and_deterministic() -> None:
    value = {
        "path": Path("z.db"),
        "set": {"beta", "alpha"},
        "nested": ({"items": frozenset({3, 1, 2})},),
    }
    first = cli._json_safe(value)
    second = cli._json_safe(value)

    assert first == second
    assert first == {
        "path": "z.db",
        "set": ["alpha", "beta"],
        "nested": [{"items": [1, 2, 3]}],
    }
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_missing_artifact_is_operational_error_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main([str(tmp_path / "missing.db"), "--json", "--compact"])

    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert code == 2
    assert captured.out == ""
    assert payload["passed"] is False
    assert payload["error_type"] == "FileNotFoundError"
    assert payload["validator_version"] == cli.VALIDATOR_VERSION


def test_invalid_artifact_is_validation_failure_exit_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    artifact = tmp_path / "empty-e3.db"
    connection = initialize_sidecar(artifact)
    connection.close()
    recover_sidecar(artifact, switch_to_delete=True)

    code = cli.main([str(artifact), "--json", "--compact"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 1
    assert captured.err == ""
    assert payload["passed"] is False
    assert "exactly_one_complete_candidate_run" in payload["failed_check_names"]


def test_valid_artifact_is_success_exit_0_and_json_is_deterministic(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    artifact, _e2 = _build_valid_fixture(tmp_path / "valid")

    first_code = cli.main([str(artifact), "--json", "--compact"])
    first = capsys.readouterr()
    second_code = cli.main([str(artifact), "--json", "--compact"])
    second = capsys.readouterr()

    first_payload = json.loads(first.out)
    second_payload = json.loads(second.out)
    assert first_code == second_code == 0
    assert first.err == second.err == ""
    assert first_payload == second_payload
    assert first_payload["passed"] is True
    assert first_payload["failed_check_names"] == []


def test_expected_logical_sha_mismatch_is_validation_failure_exit_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    artifact, _e2 = _build_valid_fixture(tmp_path / "expected-sha")

    code = cli.main(
        [
            str(artifact),
            "--expected-logical-sha256",
            "0" * 64,
            "--json",
            "--compact",
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code == 1
    assert captured.err == ""
    assert "logical_manifest_matches_expected" in payload["failed_check_names"]


def test_compact_requires_json() -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["unused.db", "--compact"])
    assert exc_info.value.code == 2
