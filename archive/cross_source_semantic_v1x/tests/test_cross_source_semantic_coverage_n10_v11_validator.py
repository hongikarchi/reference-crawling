from __future__ import annotations

import hashlib
import json
from pathlib import Path

from canonical.cross_source_image_selection import canonical_json
import canonical.cross_source_semantic_coverage_n10_v11_validator as validator


def _manifest() -> dict[str, object]:
    return {
        "config": {"model_result_selection_used": False},
        "llm_requests": 0,
        "network_requests": 0,
        "planned_occurrence_count": 53,
        "sample_seed": validator.DEFAULT_SAMPLE_SEED,
        "selected_buildings": [{} for _ in range(10)],
        "semantic_coverage_manifest_sha256": "a" * 64,
        "vision_requests": 0,
    }


def test_validator_accepts_complete_independent_byte_replay(
    monkeypatch, tmp_path: Path
) -> None:
    manifest = _manifest()
    path = tmp_path / "manifest.json"
    raw = (canonical_json(manifest) + "\n").encode()
    path.write_bytes(raw)
    monkeypatch.setattr(validator, "_replay", lambda *args, **kwargs: manifest)
    result = validator.validate_manifest(
        path, e2_spec=object(), e3_spec=object(), prior_specs=()
    )
    assert result.passed
    assert result.manifest_byte_sha256 == hashlib.sha256(raw).hexdigest()
    assert {value.name for value in result.checks} >= {
        "canonical_json_bytes",
        "entire_canonical_manifest_byte_equality",
        "selection_not_result_conditioned",
    }


def test_validator_detects_canonical_manifest_tamper(
    monkeypatch, tmp_path: Path
) -> None:
    expected = _manifest()
    actual = dict(expected)
    actual["planned_occurrence_count"] = 52
    path = tmp_path / "manifest.json"
    path.write_text(canonical_json(actual) + "\n", encoding="utf-8", newline="")
    monkeypatch.setattr(validator, "_replay", lambda *args, **kwargs: expected)
    result = validator.validate_manifest(
        path, e2_spec=object(), e3_spec=object(), prior_specs=()
    )
    assert not result.passed
    assert not next(
        value for value in result.checks
        if value.name == "entire_canonical_manifest_byte_equality"
    ).passed


def test_validator_is_not_coupled_to_production_planner() -> None:
    source = Path(
        "canonical/cross_source_semantic_coverage_n10_v11_validator.py"
    ).read_text(encoding="utf-8")
    assert "from canonical.cross_source_semantic_coverage_n10_v11 import" not in source
    assert "import canonical.cross_source_semantic_coverage_n10_v11" not in source
