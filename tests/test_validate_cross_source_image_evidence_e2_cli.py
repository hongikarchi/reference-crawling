from __future__ import annotations

import json

import pytest

from canonical.cross_source_image_evidence_validator import (
    EvidenceValidationReport,
    InputFileCheck,
    ValidationCheck,
)
from tools import validate_cross_source_image_evidence_e2 as cli


def _report(*, passed: bool) -> EvidenceValidationReport:
    return EvidenceValidationReport(
        artifact_path="C:/e2.db",
        run_id="e2-test",
        logical_sha256="a" * 64,
        table_manifests={"assets": {"count": 2, "sha256": "b" * 64}},
        input_files=(
            InputFileCheck(
                input_name="divisare_curated",
                path="C:/div.db",
                size_bytes=10,
                sha256="c" * 64,
                sidecars=(),
                unchanged_during_read=True,
                passed=True,
            ),
        ),
        checks=(
            ValidationCheck(
                name="fixture_check",
                passed=passed,
                expected={"beta", "alpha"},
                actual={"beta", "alpha"} if passed else {"gamma"},
                detail={"nested_set": {"two", "one"}},
            ),
        ),
    )


def test_human_cli_passes_expected_sha_to_read_only_validator(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: dict[str, object] = {}

    def fake_validate(path: object, **kwargs: object) -> EvidenceValidationReport:
        captured["path"] = path
        captured.update(kwargs)
        return _report(passed=True)

    monkeypatch.setattr(cli, "validate_e2_artifact", fake_validate)
    result = cli.main(
        ["artifact.db", "--expected-logical-sha256", "d" * 64]
    )
    output = capsys.readouterr()
    assert result == 0
    assert "E2 VALIDATION: PASS" in output.out
    assert str(captured["path"]) == "artifact.db"
    assert captured["expected_logical_sha256"] == "d" * 64
    assert captured["verify_input_file_hashes"] is True


def test_json_cli_returns_one_for_a_validation_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli, "validate_e2_artifact", lambda *args, **kwargs: _report(passed=False)
    )
    result = cli.main(["artifact.db", "--json", "--compact"])
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert result == 1
    assert payload["passed"] is False
    assert payload["failed_check_names"] == ["fixture_check"]
    assert payload["checks"][0]["expected"] == ["alpha", "beta"]
    assert payload["checks"][0]["actual"] == ["gamma"]
    assert payload["checks"][0]["detail"]["nested_set"] == ["one", "two"]
    assert output.err == ""


def test_human_cli_serializes_sets_on_failed_checks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli, "validate_e2_artifact", lambda *args, **kwargs: _report(passed=False)
    )
    result = cli.main(["artifact.db"])
    output = capsys.readouterr()
    assert result == 1
    assert 'expected=["alpha", "beta"]' in output.out
    assert 'actual=["gamma"]' in output.out
    assert output.err == ""


@pytest.mark.parametrize("json_mode", [False, True])
def test_cli_returns_two_for_execution_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    json_mode: bool,
) -> None:
    def fail(*args: object, **kwargs: object) -> EvidenceValidationReport:
        raise OSError("fixture failure")

    monkeypatch.setattr(cli, "validate_e2_artifact", fail)
    arguments = ["missing.db"] + (["--json"] if json_mode else [])
    result = cli.main(arguments)
    output = capsys.readouterr()
    assert result == 2
    assert output.out == ""
    if json_mode:
        payload = json.loads(output.err)
        assert payload["passed"] is False
        assert payload["error_type"] == "OSError"
    else:
        assert "E2 VALIDATION ERROR: OSError" in output.err


def test_compact_requires_json() -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(["artifact.db", "--compact"])
    assert error.value.code == 2
