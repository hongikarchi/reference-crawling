from __future__ import annotations

import hashlib
import inspect

import pytest

import canonical.cross_source_semantic_vision as v10
import canonical.cross_source_semantic_vision_v11 as v11


def _photograph() -> dict[str, object]:
    return {
        "asset_id": "opaque-A",
        "in_scope": True,
        "reject_reason": "none",
        "medium": "photograph",
        "spatial_context": "exterior",
        "framing_scale": "overall",
        "camera_angle": "eye_level",
        "drawing_kind": "not_applicable",
        "project_state": "visibly_finished",
        "project_legibility": "high",
        "uncertain_axes": [],
        "resolution_insufficient": False,
        "evidence": "A coherent building facade is visibly dominant.",
    }


def test_v11_versions_prompt_hash_and_transform_are_frozen() -> None:
    assert v11.CONTRACT_VERSION == "cross-source-image-semantics-v1.1.0"
    assert v11.PROMPT_VERSION == "cross-source-image-semantics-prompt-v1.1.0"
    assert v11.TRANSFORM_VERSION == v10.TRANSFORM_VERSION
    assert v11.PROMPT_TEMPLATE_SENTINEL_IDS == (
        "__opaque_asset_1__",
        "__opaque_asset_2__",
    )
    assert (
        v11.PROMPT_TEMPLATE_SHA256
        == "42b43a9168ea3315809182da5ff6a5ecdf280cbaabbfa2a369a88806cc7d50ba"
    )
    prompt = v11.compose_prompt(v11.PROMPT_TEMPLATE_SENTINEL_IDS)
    assert v11.prompt_sha256(v11.PROMPT_TEMPLATE_SENTINEL_IDS) == hashlib.sha256(
        prompt.encode("utf-8")
    ).hexdigest()


def test_v11_preserves_closed_schema_normalization_and_derivations() -> None:
    assert v11.RESULT_SCHEMA is v10.RESULT_SCHEMA
    assert v11.OUTPUT_SCHEMA is v10.OUTPUT_SCHEMA
    assert v11.MODEL_FIELDS is v10.MODEL_FIELDS
    assert v11.REJECT_REASONS is v10.REJECT_REASONS
    assert v11.MEDIA is v10.MEDIA
    assert v11.UNCERTAIN_AXES is v10.UNCERTAIN_AXES
    assert v11.normalize_result is v10.normalize_result
    assert v11.normalize_batch is v10.normalize_batch
    assert v11.validate_invariants is v10.validate_invariants
    assert v11.derive_hero_decision is v10.derive_hero_decision
    assert v11.derive_coverage_slots is v10.derive_coverage_slots

    row = _photograph()
    assert v11.normalize_result(row, "opaque-A") == v10.normalize_result(
        row, "opaque-A"
    )
    assert v11.derive_hero_decision(row) == v10.derive_hero_decision(row)
    assert v11.derive_coverage_slots(row) == v10.derive_coverage_slots(row)


def test_v11_prompt_is_lean_ordered_and_metadata_blind() -> None:
    assert tuple(inspect.signature(v11.compose_prompt).parameters) == ("asset_ids",)
    prompt = v11.compose_prompt(("opaque-A", "opaque-B"))
    assert prompt.index("1. opaque-A") < prompt.index("2. opaque-B")
    assert prompt.count("opaque-A") == 1
    assert prompt.count("opaque-B") == 1
    assert len(prompt) < len(v10.compose_prompt(("opaque-A", "opaque-B")))
    assert "using only visible pixels" in prompt
    assert "Do not use or infer filenames, URLs, source/project identity" in prompt
    assert "Architizer" not in prompt
    assert "Divisare" not in prompt
    assert "hero" not in prompt.casefold()
    assert "coverage" not in prompt.casefold()

    replay = v11.compose_prompt(("replacement-A", "replacement-B"))
    assert prompt.replace("opaque-A", "replacement-A").replace(
        "opaque-B", "replacement-B"
    ) == replay


def test_v11_prompt_encodes_measured_scope_and_medium_corrections() -> None:
    prompt = v11.compose_prompt(("opaque-A",))
    flat = " ".join(prompt.split())
    required_clauses = (
        "Being indoors does not make it architectural.",
        "designed project is not independently readable",
        "Keep a component/material detail when visibly integrated",
        "Reject a multi-panel board with no dominant or coherent project view.",
        "include scope in uncertain_axes",
        "Perfection alone is not CGI evidence.",
        "Rendering requires positive visible synthesis/illustration evidence.",
        "include medium in uncertain_axes",
        "A photographed 3-D scale/massing model is physical_model",
        "Layout alone does not change medium.",
    )
    for clause in required_clauses:
        assert clause in flat


def test_v11_prompt_encodes_state_legibility_and_ambiguity_audit() -> None:
    prompt = v11.compose_prompt(("opaque-A",))
    flat = " ".join(prompt.split())
    assert "exposed services/substrate" in flat
    assert "Weathering/overgrowth/deterioration" in flat
    assert "Architecture merely being present does not justify medium." in flat
    assert "a second controlled value remains credibly plausible" in flat
    assert "Uncertainty needs no tie and does not replace the best value." in flat
    assert "photo/photoreal-rendering" in flat
    assert "finished/construction/ruin" in flat
    assert "Use unknown only when no controlled value can be preferred" in flat
    assert "resolution_insufficient is true only when additional pixels" in flat


@pytest.mark.parametrize(
    "asset_ids",
    [(), ("",), ("same", "same"), "opaque-A", b"opaque-A"],
)
def test_v11_prompt_rejects_invalid_asset_id_sequences(asset_ids: object) -> None:
    with pytest.raises(ValueError):
        v11.compose_prompt(asset_ids)  # type: ignore[arg-type]


def test_v11_schema_keeps_metadata_and_derived_fields_out() -> None:
    properties = set(v11.RESULT_SCHEMA["properties"])
    assert {
        "source",
        "source_asset_id",
        "source_building_id",
        "project_id",
        "project_name",
        "fetch_url",
        "filename",
        "rank",
        "hero_tier",
        "coverage_slots",
    }.isdisjoint(properties)
