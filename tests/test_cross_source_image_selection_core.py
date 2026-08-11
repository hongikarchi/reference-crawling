from __future__ import annotations

import math
from collections import Counter

import pytest

from canonical.cross_source_image_selection import (
    DEFAULT_SHORTLIST_SIZE,
    P0_POLICY_ID,
    P1_POLICY_ID,
    P2_POLICY_ID,
    Candidate,
    DirectPHashEdge,
    SamplingItem,
    allocate_stratified_quotas,
    canonical_json,
    canonical_sha256,
    compare_standard_policies,
    deterministic_stratified_sample,
    editorial_sort_key,
    evaluate_policy,
    ordered_sample_manifest_sha256,
    policy_definitions,
    selection_stratum,
    stable_candidate_id,
)


SOURCE_SHA = "a" * 64


def _candidate(
    asset_id: str,
    *,
    source: str = "divisare",
    building_id: str = "building-1",
    status: str = "success",
    role: str = "gallery",
    ordinal: int | None = 0,
    width: int | None = 1200,
    height: int | None = 800,
    flags: tuple[str, ...] = (),
    exact: str | None = None,
    phash: str | None = None,
    source_sha: str = SOURCE_SHA,
) -> Candidate:
    return Candidate(
        source=source,
        source_building_id=building_id,
        source_asset_id=asset_id,
        fingerprint_status=status,
        role=role,
        ordinal=ordinal,
        original_width=width,
        original_height=height,
        quality_flags=flags,
        source_record_sha256=source_sha,
        exact_cluster_id=exact,
        phash_node_id=phash,
        canonical_url=f"https://images.example/{asset_id}.jpg",
    )


def _by_asset(result):
    return {
        row.candidate_id: row
        for row in result.evaluations
    }


def _selected_assets(result, candidates):
    by_id = {candidate.candidate_id: candidate.source_asset_id for candidate in candidates}
    return [by_id[value] for value in result.selected_candidate_ids]


def _sample_item(identity: str, source: str, stratum: str) -> SamplingItem:
    return SamplingItem(identity, source, stratum, canonical_sha256({"id": identity}))


def test_canonical_json_and_sha_are_stable_and_strict() -> None:
    assert canonical_json({"b": 2, "a": ["서울"]}) == '{"a":["서울"],"b":2}'
    assert canonical_sha256({"a": 1, "b": 2}) == canonical_sha256(
        {"b": 2, "a": 1}
    )
    with pytest.raises(ValueError):
        canonical_json({"bad": math.nan})


def test_candidate_normalizes_features_and_binds_stable_and_record_ids() -> None:
    left = _candidate(
        "asset-1",
        source="DIVISARE",
        role="COVER",
        flags=("low_information", "LOW_INFORMATION"),
    )
    right = _candidate(
        "asset-1",
        role="cover",
        flags=("low_information",),
    )
    assert left.source == "divisare"
    assert left.role == "cover"
    assert left.quality_flags == ("low_information",)
    assert left.candidate_id == right.candidate_id == stable_candidate_id(
        "divisare", "building-1", "asset-1"
    )
    assert left.record_sha256 == right.record_sha256

    changed = _candidate("asset-1", role="cover", ordinal=1, flags=("low_information",))
    assert changed.candidate_id == left.candidate_id
    assert changed.record_sha256 != left.record_sha256


@pytest.mark.parametrize(
    ("kwargs", "reasons"),
    [
        ({"flags": ("low_information",)}, ("low_information",)),
        ({"width": 255, "height": 1000}, ("short_edge_below_256",)),
        (
            {"width": 200, "height": 1000, "flags": ("low_information",)},
            ("low_information", "short_edge_below_256"),
        ),
    ],
)
def test_hard_risk_contract(kwargs, reasons) -> None:
    candidate = _candidate("risk", **kwargs)
    assert candidate.is_hard_risk
    assert candidate.hard_risk_reasons == reasons


def test_missing_dimensions_are_audited_but_not_an_extra_hard_risk_rule() -> None:
    candidate = _candidate("missing", width=None, height=None)
    assert not candidate.is_hard_risk
    assert candidate.pixel_area == candidate.short_edge == candidate.long_edge == 0


def test_candidate_rejects_invalid_contract_values() -> None:
    with pytest.raises(ValueError, match="ordinal"):
        _candidate("bad", ordinal=-1)
    with pytest.raises(ValueError, match="original_width"):
        _candidate("bad", width=0)
    with pytest.raises(ValueError, match="fingerprint_status"):
        _candidate("bad", status="mystery")
    with pytest.raises(ValueError, match="source record"):
        _candidate("bad", source_sha="A" * 64)
    with pytest.raises(TypeError, match="quality_flags"):
        _candidate("bad", flags=["low_information"])  # type: ignore[arg-type]


def test_editorial_order_is_role_then_ordinal_then_dimensions_then_asset_id() -> None:
    candidates = [
        _candidate("gallery-big", role="gallery", ordinal=0, width=4000, height=3000),
        _candidate("cover-later", role="cover", ordinal=9, width=300, height=300),
        _candidate("cover-small", role="cover", ordinal=1, width=800, height=600),
        _candidate("cover-big-b", role="cover", ordinal=1, width=1600, height=1200),
        _candidate("cover-big-a", role="cover", ordinal=1, width=1600, height=1200),
        _candidate("other", role="drawing", ordinal=0, width=5000, height=5000),
    ]
    ordered = sorted(candidates, key=editorial_sort_key)
    assert [row.source_asset_id for row in ordered] == [
        "cover-big-a",
        "cover-big-b",
        "cover-small",
        "cover-later",
        "gallery-big",
        "other",
    ]


def test_standard_policy_contract_is_shortlist_only_and_default_three() -> None:
    p0, p1, p2 = policy_definitions()
    assert [value.policy_id for value in (p0, p1, p2)] == [
        P0_POLICY_ID,
        P1_POLICY_ID,
        P2_POLICY_ID,
    ]
    assert {value.shortlist_size for value in (p0, p1, p2)} == {
        DEFAULT_SHORTLIST_SIZE
    }
    for policy in (p0, p1, p2):
        config = policy.as_config()
        assert config["output_kind"] == "policy_shortlist_only"
        assert config["creates_final_representative"] is False
        assert config["creates_vision_tasks"] is False
        assert config["phash_semantic_reuse_allowed"] is False
        assert config["phash_transitive_closure_allowed"] is False
        assert len(policy.config_sha256) == 64
    assert policy_definitions(2)[0].config_sha256 != p0.config_sha256


def test_p0_is_editorial_success_baseline_and_records_components() -> None:
    candidates = [
        _candidate("risky-cover", role="cover", flags=("low_information",)),
        _candidate("safe-gallery", role="gallery"),
        _candidate("failed", role="cover", ordinal=0, status="failed"),
    ]
    result = evaluate_policy(candidates, policy_definitions(2)[0])
    assert _selected_assets(result, candidates) == ["risky-cover", "safe-gallery"]
    rows = _by_asset(result)
    failed = rows[candidates[2].candidate_id]
    assert not failed.selected
    assert "excluded_non_success" in failed.reasons
    risky = rows[candidates[0].candidate_id]
    assert dict(risky.component_scores)["hard_risk"] == 1
    assert "quality_hard_risk:low_information" in risky.reasons
    assert "selected_shortlist" in risky.reasons
    assert not result.qa_fallback


def test_p1_hard_gates_risky_assets_when_any_safe_success_exists() -> None:
    candidates = [
        _candidate("risky-cover", role="cover", flags=("low_information",)),
        _candidate("safe-gallery", role="gallery"),
    ]
    result = evaluate_policy(candidates, policy_definitions(3)[1])
    assert _selected_assets(result, candidates) == ["safe-gallery"]
    risky = _by_asset(result)[candidates[0].candidate_id]
    assert not risky.selected
    assert "excluded_quality_hard_risk" in risky.reasons
    assert not result.qa_fallback


def test_p1_uses_deterministic_qa_fallback_only_when_all_successes_are_risky() -> None:
    candidates = [
        _candidate("gallery-risk", role="gallery", width=200, height=900),
        _candidate("cover-risk", role="cover", flags=("low_information",)),
        _candidate("failed-safe", role="cover", status="failed"),
    ]
    result = evaluate_policy(list(reversed(candidates)), policy_definitions(2)[1])
    assert _selected_assets(result, candidates) == ["cover-risk", "gallery-risk"]
    assert result.qa_fallback
    for candidate_id in result.selected_candidate_ids:
        row = _by_asset(result)[candidate_id]
        assert row.qa_fallback
        assert "selected_qa_fallback" in row.reasons


def test_policy_output_is_input_order_independent_and_manifest_bound() -> None:
    candidates = [_candidate(f"asset-{index}", ordinal=index) for index in range(5)]
    policy = policy_definitions(3)[0]
    left = evaluate_policy(candidates, policy)
    right = evaluate_policy(list(reversed(candidates)), policy)
    assert left.selected_candidate_ids == right.selected_candidate_ids
    assert left.ordered_manifest_sha256 == right.ordered_manifest_sha256
    assert left.record_sha256 == right.record_sha256
    assert left.as_record()["creates_final_representative"] is False
    assert left.as_record()["creates_vision_tasks"] is False


def test_p2_suppresses_exact_and_identical_phash_redundancy() -> None:
    candidates = [
        _candidate("a", ordinal=0, exact="exact-1", phash="phash-a"),
        _candidate("b", ordinal=1, exact="exact-1", phash="phash-b"),
        _candidate("c", ordinal=2, exact="exact-2", phash="phash-a"),
        _candidate("d", ordinal=3, exact="exact-3", phash="phash-d"),
    ]
    result = evaluate_policy(candidates, policy_definitions(3)[2])
    assert _selected_assets(result, candidates) == ["a", "d"]
    rows = _by_asset(result)
    assert "suppressed_exact_pixel" in rows[candidates[1].candidate_id].reasons
    assert "suppressed_identical_phash" in rows[candidates[2].candidate_id].reasons
    assert rows[candidates[1].candidate_id].suppressed_by_candidate_id == candidates[0].candidate_id


def test_p2_direct_phash_suppression_is_chosen_star_not_transitive() -> None:
    # A--B and B--C are direct, but A--C is not. A is chosen, B is suppressed,
    # and C must still be chosen because the algorithm never traverses B.
    candidates = [
        _candidate("a", ordinal=0),
        _candidate("b", ordinal=1),
        _candidate("c", ordinal=2),
    ]
    edges = [
        DirectPHashEdge(candidates[0].candidate_id, candidates[1].candidate_id, 8),
        DirectPHashEdge(candidates[1].candidate_id, candidates[2].candidate_id, 8),
    ]
    p0, p1, p2 = compare_standard_policies(
        candidates, shortlist_size=3, direct_phash_edges=edges
    )
    assert _selected_assets(p0, candidates) == ["a", "b", "c"]
    assert _selected_assets(p1, candidates) == ["a", "b", "c"]
    assert _selected_assets(p2, candidates) == ["a", "c"]
    b_row = _by_asset(p2)[candidates[1].candidate_id]
    assert "suppressed_direct_phash_le8" in b_row.reasons
    assert b_row.suppressed_by_candidate_id == candidates[0].candidate_id


def test_direct_edge_contract_rejects_invalid_or_unknown_edges() -> None:
    candidate = _candidate("a")
    with pytest.raises(ValueError, match="between 0 and 8"):
        DirectPHashEdge(candidate.candidate_id, "other", 9)
    with pytest.raises(ValueError, match="two candidates"):
        DirectPHashEdge(candidate.candidate_id, candidate.candidate_id, 0)
    edge = DirectPHashEdge(candidate.candidate_id, "unknown", 1)
    with pytest.raises(ValueError, match="unknown candidate"):
        evaluate_policy([candidate], policy_definitions()[2], direct_phash_edges=[edge])


def test_evaluate_policy_rejects_multiple_buildings_and_duplicate_candidates() -> None:
    first = _candidate("a")
    second_building = _candidate("b", building_id="building-2")
    with pytest.raises(ValueError, match="one source-qualified building"):
        evaluate_policy([first, second_building], policy_definitions()[0])
    with pytest.raises(ValueError, match="candidate IDs must be unique"):
        evaluate_policy([first, first], policy_definitions()[0])


def test_selection_strata_separate_status_risk_and_editorial_role() -> None:
    assert selection_stratum(_candidate("failed", status="failed")) == "non_success"
    assert (
        selection_stratum(_candidate("risk", flags=("low_information",)))
        == "success_hard_risk"
    )
    assert selection_stratum(_candidate("cover", role="cover")) == "success_cover"
    assert selection_stratum(_candidate("gallery", role="gallery")) == "success_gallery"
    assert selection_stratum(_candidate("other", role="drawing")) == "success_other"


def test_largest_remainder_quota_has_minimum_cell_coverage_when_feasible() -> None:
    items = [
        *[_sample_item(f"d-cover-{i}", "divisare", "cover") for i in range(7)],
        _sample_item("d-risk", "divisare", "risk"),
        _sample_item("a-cover", "architizer", "cover"),
        _sample_item("a-risk", "architizer", "risk"),
    ]
    quotas = allocate_stratified_quotas(items, 6)
    assert sum(quotas.values()) == 6
    assert all(value >= 1 for value in quotas.values())
    assert quotas[("divisare", "cover")] == 3
    assert quotas[("divisare", "risk")] == 1
    assert quotas[("architizer", "cover")] == 1
    assert quotas[("architizer", "risk")] == 1


def test_largest_remainder_is_proportional_and_deterministic_when_full_coverage_impossible() -> None:
    items = [
        *[_sample_item(f"large-{i}", "divisare", "large") for i in range(8)],
        _sample_item("small-a", "architizer", "small-a"),
        _sample_item("small-b", "architizer", "small-b"),
    ]
    forward = allocate_stratified_quotas(items, 2)
    reverse = allocate_stratified_quotas(reversed(items), 2)
    assert forward == reverse
    assert forward[("divisare", "large")] == 2
    assert sum(forward.values()) == 2


def test_deterministic_stratified_sample_reproduces_quota_and_order() -> None:
    items = [
        *[_sample_item(f"d-{i}", "divisare", "safe") for i in range(6)],
        *[_sample_item(f"a-{i}", "architizer", "safe") for i in range(3)],
        _sample_item("risk", "architizer", "risk"),
    ]
    forward = deterministic_stratified_sample(items, sample_size=6, seed="fixed-seed")
    reverse = deterministic_stratified_sample(
        reversed(items), sample_size=6, seed="fixed-seed"
    )
    assert forward == reverse
    assert len(forward) == len({value.identity for value in forward}) == 6
    expected = allocate_stratified_quotas(items, 6)
    assert Counter(value.cell for value in forward) == Counter(expected)
    assert ordered_sample_manifest_sha256(forward) == ordered_sample_manifest_sha256(
        reverse
    )
    assert ordered_sample_manifest_sha256(forward) != ordered_sample_manifest_sha256(
        reversed(forward)
    )


def test_sampling_contract_rejects_bad_counts_and_duplicate_identities() -> None:
    item = _sample_item("same", "divisare", "safe")
    assert allocate_stratified_quotas([], 0) == {}
    with pytest.raises(ValueError, match="cannot exceed"):
        allocate_stratified_quotas([item], 2)
    with pytest.raises(ValueError, match="unique"):
        allocate_stratified_quotas([item, item], 1)
    with pytest.raises(ValueError, match="sample_size"):
        allocate_stratified_quotas([item], -1)
