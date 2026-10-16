from __future__ import annotations

import hashlib
import inspect
import re

import pytest

import canonical.cross_source_semantic_vision as v10
import canonical.cross_source_semantic_vision_v11 as v11
import canonical.cross_source_semantic_vision_v12 as v12


def _records() -> tuple[dict[str, object], ...]:
    return (
        {
            "asset_id": "opaque-photo",
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
            "evidence": "A coherent whole facade is clearly visible.",
        },
        {
            "asset_id": "opaque-render",
            "in_scope": True,
            "reject_reason": "none",
            "medium": "rendering",
            "spatial_context": "interior",
            "framing_scale": "overall",
            "camera_angle": "eye_level",
            "drawing_kind": "perspective",
            "project_state": "not_applicable",
            "project_legibility": "high",
            "uncertain_axes": ["medium"],
            "resolution_insufficient": False,
            "evidence": "A synthetic interior is visible, though photography remains plausible.",
        },
        {
            "asset_id": "opaque-drawing",
            "in_scope": True,
            "reject_reason": "none",
            "medium": "drawing",
            "spatial_context": "not_applicable",
            "framing_scale": "not_applicable",
            "camera_angle": "not_applicable",
            "drawing_kind": "section",
            "project_state": "not_applicable",
            "project_legibility": "medium",
            "uncertain_axes": [],
            "resolution_insufficient": False,
            "evidence": "A coherent building section is visibly readable.",
        },
        {
            "asset_id": "opaque-reject",
            "in_scope": False,
            "reject_reason": "isolated_product_artwork_or_sample",
            "medium": "photograph",
            "spatial_context": "not_applicable",
            "framing_scale": "not_applicable",
            "camera_angle": "not_applicable",
            "drawing_kind": "not_applicable",
            "project_state": "not_applicable",
            "project_legibility": "none",
            "uncertain_axes": ["scope"],
            "resolution_insufficient": True,
            "evidence": "A tiny object dominates and more pixels could resolve its architectural status.",
        },
    )


def _lexical_token_proxy(text: str) -> int:
    """Deterministic offline size proxy; not an OpenAI tokenizer count."""

    return len(re.findall(r"\w+|[^\w\s]", text))


def test_v12_versions_prompt_hash_transform_and_size_are_frozen() -> None:
    assert v12.CONTRACT_VERSION == "cross-source-image-semantics-v1.2.0"
    assert v12.PROMPT_VERSION == "cross-source-image-semantics-prompt-v1.2.0"
    assert v12.TRANSFORM_VERSION == v10.TRANSFORM_VERSION == v11.TRANSFORM_VERSION
    assert v12.PROMPT_TEMPLATE_SENTINEL_IDS == (
        "__opaque_asset_1__",
        "__opaque_asset_2__",
    )
    assert (
        v12.PROMPT_TEMPLATE_SHA256
        == "041d6b7b0c6de4abc9989c585cd7a45267932eb6812f0225e0d12f11b99f7d73"
    )
    prompt = v12.compose_prompt(v12.PROMPT_TEMPLATE_SENTINEL_IDS)
    assert len(prompt) == 5116
    assert len(prompt.encode("utf-8")) == 5116
    assert len(prompt) - len(v11.compose_prompt(v12.PROMPT_TEMPLATE_SENTINEL_IDS)) == 314
    assert len(prompt) < len(v10.compose_prompt(v12.PROMPT_TEMPLATE_SENTINEL_IDS))
    assert _lexical_token_proxy(prompt) == 917
    assert v12.prompt_sha256(v12.PROMPT_TEMPLATE_SENTINEL_IDS) == hashlib.sha256(
        prompt.encode("utf-8")
    ).hexdigest()


def test_v12_preserves_every_schema_vocabulary_and_derivation_object() -> None:
    names = (
        "RESULT_SCHEMA",
        "OUTPUT_SCHEMA",
        "MODEL_FIELDS",
        "REJECT_REASONS",
        "MEDIA",
        "SPATIAL_CONTEXTS",
        "FRAMING_SCALES",
        "CAMERA_ANGLES",
        "DRAWING_KINDS",
        "PROJECT_STATES",
        "PROJECT_LEGIBILITY",
        "UNCERTAIN_AXES",
        "normalize_result",
        "normalize_batch",
        "validate_invariants",
        "derive_hero_decision",
        "derive_coverage_slots",
    )
    for name in names:
        assert getattr(v12, name) is getattr(v10, name)
        assert getattr(v12, name) is getattr(v11, name)

    for row in _records():
        asset_id = str(row["asset_id"])
        assert v12.normalize_result(row, asset_id) == v10.normalize_result(row, asset_id)
        assert v12.derive_hero_decision(row) == v10.derive_hero_decision(row)
        assert v12.derive_coverage_slots(row) == v10.derive_coverage_slots(row)


def test_v12_prompt_is_ordered_replayable_and_metadata_blind() -> None:
    assert tuple(inspect.signature(v12.compose_prompt).parameters) == ("asset_ids",)
    prompt = v12.compose_prompt(("opaque-A", "opaque-B"))
    assert prompt.index("1. opaque-A") < prompt.index("2. opaque-B")
    assert prompt.count("opaque-A") == 1
    assert prompt.count("opaque-B") == 1
    assert "using only visible pixels" in prompt
    assert "Do not use or infer filenames, URLs, source/project identity" in prompt
    for forbidden in (
        "Architizer",
        "Divisare",
        "project_name",
        "source_asset_id",
        "hero",
        "coverage",
    ):
        assert forbidden.casefold() not in prompt.casefold()
    replay = v12.compose_prompt(("replacement-A", "replacement-B"))
    assert prompt.replace("opaque-A", "replacement-A").replace(
        "opaque-B", "replacement-B"
    ) == replay


def test_v12_prompt_encodes_overlay_and_medium_measured_delta_once() -> None:
    flat = " ".join(v12.compose_prompt(("opaque-A",)).split())
    assert "Overlays, captions, watermarks, borders, or magazine layout" in flat
    assert "do not make mixed" in flat
    assert (
        "Use mixed only when substantive scene content in two or more representation media is comparably important."
        in flat
    )
    assert "Branding or perfection is not proof of rendering." in flat
    assert "photoreal CGI and photography both remain plausible" in flat
    assert "A photographed 3-D scale/massing model is physical_model" in flat
    assert flat.count("do not make mixed") == 1


def test_v12_prompt_requires_independent_uncertainty_second_pass_and_evidence() -> None:
    flat = " ".join(v12.compose_prompt(("opaque-A",)).split())
    required = (
        "Mandatory uncertainty second pass:",
        "Before returning, recheck scope and each applicable axis",
        "even when the primary is likelier",
        "name the visible ambiguity in evidence",
        "Empty uncertain_axes asserts no credible alternative",
        "do not add speculation",
        "project/product/art/model",
        "photograph/photoreal-rendering",
        "drawing/model/mixed",
        "material/element",
        "element/overall/site",
        "eye_level/elevated",
        "detail/composite",
        "adjacent legibility grades",
    )
    for clause in required:
        assert clause in flat


def test_v12_prompt_tightens_framing_legibility_elevation_and_resolution() -> None:
    flat = " ".join(v12.compose_prompt(("opaque-A",)).split())
    required = (
        "material_detail centers a surface/material/junction",
        "element_detail an integrated component/cropped assembly",
        "overall a coherent whole room/building/facade",
        "site_context essential wider relationships",
        "Surrounding walls alone do not make a crop overall.",
        "eye_level includes a normal standing view tilted up/down",
        "elevated requires evidence that the camera is above normal standing height",
        "high requires a clear coherent whole, not just sharp pixels",
        "a readable partial component/crop is normally medium",
        "more pixels could change a controlled decision, not merely reveal detail",
        "set true and mark those axes",
    )
    for clause in required:
        assert clause in flat


@pytest.mark.parametrize(
    "asset_ids",
    [(), ("",), ("same", "same"), "opaque-A", b"opaque-A"],
)
def test_v12_prompt_rejects_invalid_asset_id_sequences(asset_ids: object) -> None:
    with pytest.raises(ValueError):
        v12.compose_prompt(asset_ids)  # type: ignore[arg-type]


def test_v12_closed_schema_still_excludes_metadata_and_derived_fields() -> None:
    properties = set(v12.RESULT_SCHEMA["properties"])
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
