from __future__ import annotations

import hashlib
from pathlib import Path

from canonical.cross_source_image_selection import canonical_json
import canonical.cross_source_semantic_coverage_n10_v12_validator as validator


def _manifest() -> dict[str, object]:
    return {"config": {"model_result_selection_used": False}, "llm_requests": 0, "network_requests": 0, "planned_occurrence_count": 50, "sample_seed": validator.DEFAULT_SAMPLE_SEED, "selected_buildings": [{} for _ in range(10)], "semantic_coverage_manifest_sha256": "a" * 64, "vision_requests": 0}


def test_validator_accepts_complete_byte_replay(monkeypatch, tmp_path: Path) -> None:
    manifest = _manifest()
    path = tmp_path / "manifest.json"
    raw = (canonical_json(manifest) + "\n").encode()
    path.write_bytes(raw)
    monkeypatch.setattr(validator, "_replay", lambda *args, **kwargs: manifest)
    result = validator.validate_manifest(path, e2_spec=object(), e3_spec=object(), prior_specs=())
    assert result.passed and result.selected_occurrences == 50
    assert result.manifest_byte_sha256 == hashlib.sha256(raw).hexdigest()


def test_validator_is_not_coupled_to_v12_planner() -> None:
    source = Path("canonical/cross_source_semantic_coverage_n10_v12_validator.py").read_text("utf-8")
    assert "from canonical.cross_source_semantic_coverage_n10_v12 import" not in source
    assert "import canonical.cross_source_semantic_coverage_n10_v12" not in source


def test_v12_prior_order_and_counts_are_pinned_in_source() -> None:
    source = Path("canonical/cross_source_semantic_coverage_n10_v12_validator.py").read_text("utf-8")
    assert '"semantic_n10_v1_1"' in source
    assert "!= 120" in source and "!= 672" in source and "!= 665" in source
