from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import tools.run_cross_source_semantic_vision_n100 as cli


def test_production_manifest_and_literal_runtime_envelope_are_pinned() -> None:
    payload = cli._verify_frozen_manifest(cli.DEFAULT_MANIFEST)
    assert payload["semantic_coverage_manifest_sha256"] == (
        cli.CANONICAL_MANIFEST_SELF_SHA256
    )
    assert payload["ordered_fetch_url_manifest_sha256"] == (
        cli.CANONICAL_ORDERED_FETCH_URL_MANIFEST_SHA256
    )
    assert payload["ordered_e1_pixel_manifest_sha256"] == (
        cli.CANONICAL_ORDERED_E1_PIXEL_MANIFEST_SHA256
    )
    assert cli.CONTRACT.building_count == 100
    assert cli.CONTRACT.occurrence_count == 562
    assert cli.CONTRACT.planned_batch_count == 113
    assert cli.MAX_APPROVED_MODEL_CALLS == 125
    cli._assert_approved_envelope()


def test_tampered_manifest_fails_before_preflight_or_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}\n", encoding="utf-8")
    calls = {"preflight": 0, "runtime": 0}

    def forbidden_preflight(*_args, **_kwargs):
        calls["preflight"] += 1
        raise AssertionError("independent replay reached after bad byte pin")

    def forbidden_runtime(*_args, **_kwargs):
        calls["runtime"] += 1
        raise AssertionError("runtime reached after bad byte pin")

    monkeypatch.setattr(
        cli, "validate_frozen_n100_manifest_preflight", forbidden_preflight
    )
    monkeypatch.setattr(cli, "run_semantic_vision_n100", forbidden_runtime)
    assert cli.main(["--manifest", str(manifest)]) == 2
    assert calls == {"preflight": 0, "runtime": 0}


def test_main_replays_manifest_then_wires_exact_contract_and_review_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = {
        name: tmp_path / name
        for name in (
            "manifest.json",
            "e2.db",
            "e3.db",
            "n10-manifest.json",
            "n10.db",
            "n100.db",
            "n100.md",
            "review-cache",
        )
    }
    order: list[str] = []
    captured: dict[str, object] = {}

    monkeypatch.setattr(cli, "_verify_frozen_manifest", lambda _path: order.append("pin"))

    def preflight(*_args, **_kwargs):
        order.append("replay")
        return {"passed": True}

    def runtime(**kwargs):
        order.append("runtime")
        captured.update(kwargs)
        return SimpleNamespace(
            already_complete=False,
            logical_sha256="a" * 64,
            metrics={"results": 562},
            output_db=paths["n100.db"],
            report_path=paths["n100.md"],
            requests_made=562,
            resumed=False,
            status="complete",
            vision_requests=113,
        )

    monkeypatch.setattr(cli, "validate_frozen_n100_manifest_preflight", preflight)
    monkeypatch.setattr(cli, "run_semantic_vision_n100", runtime)
    args = [
        "--manifest", str(paths["manifest.json"]),
        "--e2", str(paths["e2.db"]),
        "--e3", str(paths["e3.db"]),
        "--n10-manifest", str(paths["n10-manifest.json"]),
        "--n10-sidecar", str(paths["n10.db"]),
        "--output-db", str(paths["n100.db"]),
        "--report", str(paths["n100.md"]),
        "--review-cache-dir", str(paths["review-cache"]),
    ]
    assert cli.main(args) == 0
    assert order == ["pin", "replay", "runtime"]
    assert captured["contract"] is cli.CONTRACT
    assert captured["review_cache_dir"] == paths["review-cache"]
    assert captured["expected_n10_sidecar_sha256"] == cli.FROZEN_N10_SIDECAR_SHA256
    output = json.loads(capsys.readouterr().out)
    assert output["manifest_preflight_passed"] is True
    assert output["approved_envelope"] == {
        "input_tokens": 2_600_000,
        "model_calls": 125,
        "output_tokens": 100_000,
        "total_tokens": 2_700_000,
    }


def test_changed_token_cap_fails_before_preflight_and_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def forbidden(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("request-capable phase was reached")

    monkeypatch.setattr(cli, "MAX_TOTAL_TOKENS", 2_700_001)
    monkeypatch.setattr(cli, "validate_frozen_n100_manifest_preflight", forbidden)
    monkeypatch.setattr(cli, "run_semantic_vision_n100", forbidden)
    assert cli.main([]) == 2
    assert calls == 0
