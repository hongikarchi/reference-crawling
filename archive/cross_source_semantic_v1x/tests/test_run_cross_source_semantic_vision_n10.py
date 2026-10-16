from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import tools.run_cross_source_semantic_vision_n10 as cli


FROZEN_MANIFEST = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "reports"
    / "cross_source_semantic_coverage_n10_v1.json"
)


def test_canonical_manifest_preflight_accepts_only_the_frozen_population(
    tmp_path: Path,
) -> None:
    payload = cli._verify_canonical_manifest(FROZEN_MANIFEST)
    assert payload["sample_seed"] == cli.CANONICAL_SAMPLE_SEED
    assert (
        payload["population"]["inventory_manifest_sha256"]
        == cli.CANONICAL_POPULATION_MANIFEST_SHA256
    )

    exact_copy = tmp_path / "exact-copy.json"
    exact_copy.write_bytes(FROZEN_MANIFEST.read_bytes())
    assert cli._verify_canonical_manifest(exact_copy) == payload


def test_changed_manifest_fails_before_runner_is_called(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    changed = tmp_path / "changed.json"
    raw = FROZEN_MANIFEST.read_bytes()
    changed.write_bytes(raw.replace(b'"sample_size_buildings":10', b'"sample_size_buildings":11', 1))
    called = False

    def forbidden_runner(**_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("runner must not be reached")

    monkeypatch.setattr(cli, "run_semantic_vision_n10", forbidden_runner)
    assert cli.main(["--manifest", str(changed)]) == 2
    assert called is False


def test_manifest_sha_override_arguments_are_not_exposed() -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.build_parser().parse_args(
            ["--expected-manifest-sha256", "0" * 64]
        )
    assert exc_info.value.code == 2

    with pytest.raises(SystemExit) as exc_info:
        cli.build_parser().parse_args(
            ["--expected-manifest-self-sha256", "0" * 64]
        )
    assert exc_info.value.code == 2


def test_runner_receives_literal_frozen_hashes_after_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_runner(**kwargs: object) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(
            already_complete=False,
            logical_sha256="1" * 64,
            metrics={},
            output_db=tmp_path / "out.db",
            report_path=tmp_path / "report.md",
            requests_made=0,
            resumed=False,
            status="complete",
            vision_requests=0,
        )

    monkeypatch.setattr(cli, "run_semantic_vision_n10", fake_runner)
    assert cli.main(["--manifest", str(FROZEN_MANIFEST)]) == 0
    assert (
        captured["expected_manifest_byte_sha256"]
        == cli.CANONICAL_MANIFEST_BYTE_SHA256
    )
    assert (
        captured["expected_manifest_self_sha256"]
        == cli.CANONICAL_MANIFEST_SELF_SHA256
    )
