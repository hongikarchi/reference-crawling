from __future__ import annotations

import hashlib
import inspect
from pathlib import Path

from canonical.cross_source_image_selection import canonical_json
from canonical.cross_source_semantic_coverage_n100 import PriorN10Spec
from canonical.cross_source_semantic_coverage_n100_validator import (
    validate_semantic_coverage_n100_manifest,
)
from canonical.cross_source_semantic_coverage_sources import ArtifactSpec
import canonical.cross_source_semantic_coverage_n100_validator as validator_module


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _dummy_specs(tmp_path: Path) -> tuple[ArtifactSpec, ArtifactSpec, PriorN10Spec]:
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    artifact = ArtifactSpec("fixture", empty, 0, _sha(""), _sha("logical"), "run", 1)
    prior = PriorN10Spec(
        empty,
        empty,
        expected_manifest_size=0,
        expected_manifest_byte_sha256=_sha(""),
        expected_manifest_self_sha256=_sha("self"),
        expected_sidecar_size=0,
        expected_sidecar_byte_sha256=_sha(""),
        expected_sidecar_logical_sha256=_sha("logical"),
        expected_run_id="run",
    )
    return artifact, artifact, prior


def test_validator_compares_entire_canonical_byte_stream(
    tmp_path: Path, monkeypatch
) -> None:
    expected = {
        "llm_requests": 0,
        "network_requests": 0,
        "planned_occurrence_count": 562,
        "planned_unique_e1_pixel_count": 562,
        "sample_seed": "archibe-semantic-coverage-n100-v1",
        "selected_buildings": [{} for _ in range(100)],
        "semantic_coverage_manifest_sha256": _sha("self"),
        "vision_requests": 0,
    }
    monkeypatch.setattr(validator_module, "_replay_manifest", lambda *args, **kwargs: expected)
    path = tmp_path / "manifest.json"
    path.write_text(canonical_json(expected) + "\n", encoding="utf-8", newline="\n")
    e2, e3, prior = _dummy_specs(tmp_path)
    result = validate_semantic_coverage_n100_manifest(
        path, e2_spec=e2, e3_spec=e3, n10_spec=prior
    )
    assert result.passed
    assert not result.failed_check_names

    tampered = tmp_path / "tampered.json"
    tampered.write_text(canonical_json({**expected, "extra": True}) + "\n", encoding="utf-8", newline="\n")
    result = validate_semantic_coverage_n100_manifest(
        tampered, e2_spec=e2, e3_spec=e3, n10_spec=prior
    )
    assert not result.passed
    assert "entire_canonical_manifest_byte_equality" in result.failed_check_names


def test_validator_does_not_call_n100_planner_selection_or_builder() -> None:
    source = inspect.getsource(validator_module)
    assert "select_stratified_n100(" not in source
    assert "build_semantic_coverage_n100_manifest(" not in source
    assert "_collect_inventory_independently(" in source
    assert "_replay_building_coverage(" in source
