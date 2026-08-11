from __future__ import annotations

import hashlib

import pytest

from canonical.cross_source_semantic_coverage import (
    BuildingInventoryItem,
    CoverageCandidate,
    phash_hamming,
    select_building_coverage,
    select_guarded_n10,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _building(
    source: str,
    key: str,
    stratum: str = "ordinary",
    *,
    qa: bool = False,
    p1: bool = False,
    p2: bool = False,
    cross: bool = False,
    galleries: int = 3,
) -> BuildingInventoryItem:
    return BuildingInventoryItem(
        selection_id=f"{source}:building:{key}",
        source=source,
        source_building_id=key,
        name=key,
        population_stratum=stratum,
        successful_asset_count=max(3, galleries),
        successful_gallery_count=galleries,
        source_record_sha256=_sha(f"source:{key}"),
        selection_record_sha256=_sha(f"selection:{key}"),
        qa_fallback=qa,
        p1_rank1_changed=p1,
        p2_top3_changed=p2,
        cross_source_candidate=cross,
    )


def _inventory() -> tuple[BuildingInventoryItem, ...]:
    return (
        _building("architizer", "a-qa", qa=True),
        _building("architizer", "a-p1", p1=True),
        _building("divisare", "d-p1", p1=True),
        _building("architizer", "a-p2", p2=True),
        _building("divisare", "d-p2", p2=True),
        _building("architizer", "a-gallery", "gallery_fallback"),
        _building("divisare", "d-gallery", "gallery_fallback"),
        _building("architizer", "a-cross", cross=True),
        _building("divisare", "d-cross", cross=True),
        _building("divisare", "d-control", galleries=20),
    )


def test_guarded_n10_is_fixed_source_balanced_and_order_independent() -> None:
    first = select_guarded_n10(_inventory(), seed="fixed-seed")
    replay = select_guarded_n10(reversed(_inventory()), seed="fixed-seed")
    assert [x.building.identity for x in first] == [x.building.identity for x in replay]
    assert [x.guard_name for x in first] == [
        "architizer_qa_fallback",
        "architizer_p1_rank1_changed",
        "divisare_p1_rank1_changed",
        "architizer_p2_top3_changed",
        "divisare_p2_top3_changed",
        "architizer_gallery_fallback",
        "divisare_gallery_fallback",
        "architizer_cross_source",
        "divisare_cross_source",
        "divisare_ordinary_long_gallery",
    ]
    assert sum(x.building.source == "architizer" for x in first) == 5
    assert sum(x.building.source == "divisare" for x in first) == 5


def test_guarded_n10_fails_closed_when_a_guard_is_empty() -> None:
    with pytest.raises(ValueError, match="architizer_qa_fallback"):
        select_guarded_n10([x for x in _inventory() if not x.qa_fallback])


def _candidate(
    index: int,
    *,
    shortlist_rank: int | None = None,
    pixel: str | None = None,
    phash: int | None = None,
    hard_risk: bool = False,
    role: str = "gallery",
) -> CoverageCandidate:
    asset = f"asset-{index}"
    phash_value = int(_sha(f"phash:{index}"), 16) if phash is None else phash
    return CoverageCandidate(
        candidate_id=f"candidate-{index}",
        selection_id="architizer:building:a",
        source="architizer",
        source_building_id="a",
        source_asset_id=asset,
        editorial_rank=index + 1,
        p2_shortlist_rank=shortlist_rank,
        qa_fallback=False,
        hard_risk=hard_risk,
        roles=(role,),
        source_ordinal=index,
        canonical_url=f"https://canonical/{asset}",
        fetch_url=f"https://fetch/{asset}",
        final_url=None,
        original_width=1024,
        original_height=768,
        normalized_width=512,
        normalized_height=384,
        quality_flags=(),
        normalized_pixel_sha256=pixel or _sha(f"pixel:{index}"),
        phash_node_id=f"node-{phash_value}",
        phash_hex=f"{phash_value:064x}",
        raw_response_sha256=_sha(f"raw:{index}"),
        e3_source_record_sha256=_sha(f"asset-record:{index}"),
        e3_candidate_record_sha256=_sha(f"candidate-record:{index}"),
        e3_ranking_record_sha256=_sha(f"ranking-record:{index}"),
        e3_shortlist_item_record_sha256=(
            _sha(f"shortlist-record:{index}") if shortlist_rank else None
        ),
        e2_asset_record_sha256=_sha(f"asset-record:{index}"),
        e2_building_relation_record_sha256=_sha(f"relation:{index}"),
    )


def test_building_plan_is_anchor_first_bounded_and_spreads_gallery() -> None:
    candidates = tuple(
        _candidate(index, shortlist_rank=index + 1 if index < 3 else None)
        for index in range(10)
    )
    plan = select_building_coverage(reversed(candidates))
    assert len(plan.selected_occurrences) == 6
    assert [x.candidate.candidate_id for x in plan.selected_occurrences[:3]] == [
        "candidate-0",
        "candidate-1",
        "candidate-2",
    ]
    assert [x.probe_slot for x in plan.selected_occurrences[3:]] == [
        "gallery_early",
        "gallery_middle",
        "gallery_late",
    ]
    assert "coverage_anchor_p1_rank_1" in plan.selected_occurrences[0].origins


def test_redundancy_is_chosen_star_not_transitive() -> None:
    # A-B and B-C are distance <=8, while A-C is >8. B is rejected against
    # selected A; C remains eligible because the rejected B is never traversed.
    a = _candidate(0, shortlist_rank=1, phash=0)
    b = _candidate(1, phash=(1 << 8) - 1)
    c = _candidate(2, phash=((1 << 8) - 1) | (((1 << 8) - 1) << 8))
    distant = _candidate(3, phash=1 << 80)
    plan = select_building_coverage((a, b, c, distant))
    chosen = {x.candidate.candidate_id for x in plan.selected_occurrences}
    assert "candidate-1" not in chosen
    assert "candidate-2" in chosen
    assert phash_hamming(a.phash_hex, c.phash_hex) == 16


def test_non_risk_pool_excludes_risky_gallery_but_all_risk_is_explicit_qa() -> None:
    mixed = (
        _candidate(0, shortlist_rank=1, hard_risk=False),
        _candidate(1, shortlist_rank=2, hard_risk=False),
        _candidate(2, shortlist_rank=3, hard_risk=True),
        _candidate(3, hard_risk=True),
        _candidate(4, hard_risk=False),
    )
    plan = select_building_coverage(mixed)
    assert plan.quality_pool_state == "non_hard_risk"
    assert all(
        not x.candidate.hard_risk
        for x in plan.selected_occurrences
        if x.probe_slot is not None
    )
    all_risk = tuple(
        _candidate(i, shortlist_rank=i + 1 if i < 3 else None, hard_risk=True)
        for i in range(5)
    )
    assert select_building_coverage(all_risk).quality_pool_state == "all_risk_qa_fallback"
