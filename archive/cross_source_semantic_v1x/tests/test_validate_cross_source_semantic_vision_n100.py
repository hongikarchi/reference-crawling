from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import tools.validate_cross_source_semantic_vision_n100 as cli


def test_validator_cli_wires_all_immutable_inputs_and_reports_zero_requests(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: dict[str, object] = {}

    def validate(sidecar, **kwargs):
        captured["sidecar"] = sidecar
        captured.update(kwargs)
        return SimpleNamespace(
            passed=True,
            as_dict=lambda: {
                "llm_requests": 0,
                "network_requests": 0,
                "passed": True,
                "vision_requests": 0,
            },
        )

    monkeypatch.setattr(cli, "validate_semantic_vision_n100", validate)
    assert cli.main([]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "llm_requests": 0,
        "network_requests": 0,
        "passed": True,
        "vision_requests": 0,
    }
    assert captured["sidecar"] == cli.DEFAULT_SIDECAR
    assert captured["n10_manifest_path"] == cli.DEFAULT_N10_MANIFEST
    assert captured["review_cache_dir"] == cli.DEFAULT_REVIEW_CACHE


def test_validator_cli_exception_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(*_args, **_kwargs):
        raise RuntimeError("synthetic validation failure")

    monkeypatch.setattr(cli, "validate_semantic_vision_n100", fail)
    assert cli.main([]) == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["passed"] is False
    assert payload["network_requests"] == 0
    assert payload["vision_requests"] == 0
