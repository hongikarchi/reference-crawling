from __future__ import annotations

from pathlib import Path

from canonical.cross_source_semantic_coverage_n10_v11_validator import (
    ValidationCheck,
    ValidationResult,
)
import tools.plan_cross_source_semantic_coverage_n10_v11 as planner_cli
import tools.validate_cross_source_semantic_coverage_n10_v11 as validator_cli


def test_plan_cli_reports_zero_requests(monkeypatch, tmp_path: Path, capsys) -> None:
    output = tmp_path / "manifest.json"
    manifest = {
        "semantic_coverage_manifest_sha256": "a" * 64,
        "planned_occurrence_count": 53,
        "planned_unique_e1_pixel_count": 53,
        "sample_size_buildings": 10,
    }
    monkeypatch.setattr(planner_cli, "default_specs", lambda root: (object(), object()))
    monkeypatch.setattr(planner_cli, "default_prior_specs", lambda root: ())
    monkeypatch.setattr(planner_cli, "build_manifest", lambda *args, **kwargs: manifest)
    assert planner_cli.main(["--output", str(output), "--repo-root", str(tmp_path)]) == 0
    stdout = capsys.readouterr().out
    assert '"network_requests": 0' in stdout
    assert '"vision_requests": 0' in stdout
    assert output.exists()


def test_validate_cli_returns_nonzero_for_failed_replay(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    path = tmp_path / "manifest.json"
    path.write_text("{}\n", encoding="utf-8")
    result = ValidationResult(
        path,
        False,
        (ValidationCheck("entire_canonical_manifest_byte_equality", False, "x", "y"),),
        3,
        "b" * 64,
        None,
        0,
        0,
    )
    monkeypatch.setattr(validator_cli, "default_specs", lambda root: (object(), object()))
    monkeypatch.setattr(validator_cli, "default_prior_specs", lambda root: ())
    monkeypatch.setattr(validator_cli, "validate_manifest", lambda *args, **kwargs: result)
    assert validator_cli.main(["--manifest", str(path), "--repo-root", str(tmp_path)]) == 1
    assert '"passed": false' in capsys.readouterr().out
