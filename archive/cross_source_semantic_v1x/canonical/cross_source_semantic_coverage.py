"""Pure offline helpers for the cross-source semantic-coverage preflight.

This module chooses *candidate occurrences* only.  It never downloads an
image, creates a Vision task, assigns an image type, chooses a final hero, or
uses pHash as semantic identity.  The frozen E3 P2 shortlist supplies up to
three representative anchors.  Early/middle/late gallery probes may fill the
remaining slots up to six occurrences per source-qualified building.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Sequence

from canonical.cross_source_image_selection import canonical_sha256


SEMANTIC_COVERAGE_VERSION = "archibe-cross-source-semantic-coverage-v1"
SEMANTIC_COVERAGE_MANIFEST_DOMAIN = (
    "archibe-cross-source-semantic-coverage-manifest-v1"
)
DEFAULT_SAMPLE_SEED = "archibe-semantic-coverage-n10-v1"
DEFAULT_SAMPLE_SIZE = 10
MAX_OCCURRENCES_PER_BUILDING = 6
P2_POLICY_ID = "p2_quality_exact_direct_phash_shortlist"


def _identity(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be a non-empty string without outer whitespace")
    return value


@dataclass(frozen=True)
class BuildingInventoryItem:
    selection_id: str
    source: str
    source_building_id: str
    name: str | None
    population_stratum: str
    successful_asset_count: int
    successful_gallery_count: int
    source_record_sha256: str
    selection_record_sha256: str
    qa_fallback: bool = False
    p1_rank1_changed: bool = False
    p2_top3_changed: bool = False
    cross_source_candidate: bool = False

    def __post_init__(self) -> None:
        _identity(self.selection_id, label="selection_id")
        source = _identity(self.source, label="source").casefold()
        if source not in {"architizer", "divisare"}:
            raise ValueError(f"unsupported source: {source}")
        object.__setattr__(self, "source", source)
        _identity(self.source_building_id, label="source_building_id")
        _identity(self.population_stratum, label="population_stratum")
        if self.successful_asset_count <= 0:
            raise ValueError("inventory contains only buildings with successful assets")
        if not 0 <= self.successful_gallery_count <= self.successful_asset_count:
            raise ValueError("successful_gallery_count is outside the asset count")

    @property
    def identity(self) -> str:
        return self.selection_id

    @property
    def gallery_fallback(self) -> bool:
        return self.population_stratum == "gallery_fallback"

    @property
    def ordinary_long_gallery(self) -> bool:
        return (
            self.population_stratum == "ordinary"
            and self.successful_gallery_count >= 20
        )

    def as_record(self) -> dict[str, Any]:
        return {
            "cross_source_candidate": self.cross_source_candidate,
            "name": self.name,
            "p1_rank1_changed": self.p1_rank1_changed,
            "p2_top3_changed": self.p2_top3_changed,
            "population_stratum": self.population_stratum,
            "qa_fallback": self.qa_fallback,
            "selection_id": self.selection_id,
            "selection_record_sha256": self.selection_record_sha256,
            "source": self.source,
            "source_building_id": self.source_building_id,
            "source_record_sha256": self.source_record_sha256,
            "successful_asset_count": self.successful_asset_count,
            "successful_gallery_count": self.successful_gallery_count,
        }

    @property
    def record_sha256(self) -> str:
        return canonical_sha256(self.as_record())


@dataclass(frozen=True)
class SampleGuard:
    name: str
    source: str
    predicate: Callable[[BuildingInventoryItem], bool]


@dataclass(frozen=True)
class GuardedSelection:
    rank: int
    guard_name: str
    guard_source: str
    eligible_count: int
    available_count: int
    score_sha256: str
    building: BuildingInventoryItem

    def as_record(self) -> dict[str, Any]:
        return {
            "available_count_before_pick": self.available_count,
            "building": self.building.as_record(),
            "building_record_sha256": self.building.record_sha256,
            "eligible_count": self.eligible_count,
            "guard_name": self.guard_name,
            "guard_source": self.guard_source,
            "rank": self.rank,
            "score_sha256": self.score_sha256,
        }


def default_n10_guards() -> tuple[SampleGuard, ...]:
    """Return the frozen, source-balanced branch-coverage guard order."""

    return (
        SampleGuard("architizer_qa_fallback", "architizer", lambda x: x.qa_fallback),
        SampleGuard(
            "architizer_p1_rank1_changed",
            "architizer",
            lambda x: x.p1_rank1_changed,
        ),
        SampleGuard(
            "divisare_p1_rank1_changed", "divisare", lambda x: x.p1_rank1_changed
        ),
        SampleGuard(
            "architizer_p2_top3_changed",
            "architizer",
            lambda x: x.p2_top3_changed,
        ),
        SampleGuard(
            "divisare_p2_top3_changed", "divisare", lambda x: x.p2_top3_changed
        ),
        SampleGuard(
            "architizer_gallery_fallback",
            "architizer",
            lambda x: x.gallery_fallback,
        ),
        SampleGuard(
            "divisare_gallery_fallback", "divisare", lambda x: x.gallery_fallback
        ),
        SampleGuard(
            "architizer_cross_source",
            "architizer",
            lambda x: x.cross_source_candidate,
        ),
        SampleGuard(
            "divisare_cross_source", "divisare", lambda x: x.cross_source_candidate
        ),
        SampleGuard(
            "divisare_ordinary_long_gallery",
            "divisare",
            lambda x: x.ordinary_long_gallery,
        ),
    )


def deterministic_guard_score(
    seed: str, guard_name: str, selection_id: str
) -> str:
    return canonical_sha256(
        {
            "domain": "semantic-coverage-guard-score",
            "guard_name": guard_name,
            "seed": seed,
            "selection_id": selection_id,
            "version": SEMANTIC_COVERAGE_VERSION,
        }
    )


def select_guarded_n10(
    inventory: Iterable[BuildingInventoryItem],
    *,
    seed: str = DEFAULT_SAMPLE_SEED,
    guards: Sequence[SampleGuard] | None = None,
) -> tuple[GuardedSelection, ...]:
    """Choose ten fixed buildings, one for each frozen coverage guard."""

    seed = _identity(seed, label="seed")
    values = tuple(sorted(inventory, key=lambda x: x.identity))
    if len({x.identity for x in values}) != len(values):
        raise ValueError("inventory selection_id values must be unique")
    chosen_ids: set[str] = set()
    result: list[GuardedSelection] = []
    frozen_guards = tuple(guards or default_n10_guards())
    if len(frozen_guards) != DEFAULT_SAMPLE_SIZE:
        raise ValueError("the v1 guarded smoke contract requires exactly ten guards")
    for guard in frozen_guards:
        eligible = [
            item
            for item in values
            if item.source == guard.source and guard.predicate(item)
        ]
        available = [item for item in eligible if item.identity not in chosen_ids]
        if not available:
            raise ValueError(f"semantic coverage guard has no unused candidate: {guard.name}")
        scored = [
            (deterministic_guard_score(seed, guard.name, item.identity), item)
            for item in available
        ]
        score, winner = min(scored, key=lambda pair: (pair[0], pair[1].identity))
        chosen_ids.add(winner.identity)
        result.append(
            GuardedSelection(
                rank=len(result) + 1,
                guard_name=guard.name,
                guard_source=guard.source,
                eligible_count=len(eligible),
                available_count=len(available),
                score_sha256=score,
                building=winner,
            )
        )
    source_counts = {
        source: sum(value.building.source == source for value in result)
        for source in ("architizer", "divisare")
    }
    if source_counts != {"architizer": 5, "divisare": 5}:
        raise ValueError(f"N10 source balance violated: {source_counts!r}")
    return tuple(result)


@dataclass(frozen=True)
class CoverageCandidate:
    candidate_id: str
    selection_id: str
    source: str
    source_building_id: str
    source_asset_id: str
    editorial_rank: int
    p2_shortlist_rank: int | None
    qa_fallback: bool
    hard_risk: bool
    roles: tuple[str, ...]
    source_ordinal: int | None
    canonical_url: str
    fetch_url: str
    final_url: str | None
    original_width: int | None
    original_height: int | None
    normalized_width: int | None
    normalized_height: int | None
    quality_flags: tuple[str, ...]
    normalized_pixel_sha256: str
    phash_node_id: str
    phash_hex: str
    raw_response_sha256: str
    e3_source_record_sha256: str
    e3_candidate_record_sha256: str
    e3_ranking_record_sha256: str
    e3_shortlist_item_record_sha256: str | None
    e2_asset_record_sha256: str
    e2_building_relation_record_sha256: str

    @property
    def is_gallery(self) -> bool:
        return "gallery" in self.roles

    def as_record(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "canonical_url": self.canonical_url,
            "e2_asset_record_sha256": self.e2_asset_record_sha256,
            "e2_building_relation_record_sha256": (
                self.e2_building_relation_record_sha256
            ),
            "e3_candidate_record_sha256": self.e3_candidate_record_sha256,
            "e3_ranking_record_sha256": self.e3_ranking_record_sha256,
            "e3_shortlist_item_record_sha256": (
                self.e3_shortlist_item_record_sha256
            ),
            "e3_source_record_sha256": self.e3_source_record_sha256,
            "editorial_rank": self.editorial_rank,
            "fetch_url": self.fetch_url,
            "final_url": self.final_url,
            "hard_risk": self.hard_risk,
            "normalized_height": self.normalized_height,
            "normalized_pixel_sha256": self.normalized_pixel_sha256,
            "normalized_width": self.normalized_width,
            "original_height": self.original_height,
            "original_width": self.original_width,
            "p2_shortlist_rank": self.p2_shortlist_rank,
            "phash_hex": self.phash_hex,
            "phash_node_id": self.phash_node_id,
            "qa_fallback": self.qa_fallback,
            "quality_flags": list(self.quality_flags),
            "raw_response_sha256": self.raw_response_sha256,
            "roles": list(self.roles),
            "selection_id": self.selection_id,
            "source": self.source,
            "source_asset_id": self.source_asset_id,
            "source_building_id": self.source_building_id,
            "source_ordinal": self.source_ordinal,
        }

    @property
    def record_sha256(self) -> str:
        return canonical_sha256(self.as_record())


@dataclass(frozen=True)
class RedundancyEvidence:
    kind: str
    compared_candidate_id: str
    hamming_distance: int | None = None

    def as_record(self) -> dict[str, Any]:
        return {
            "compared_candidate_id": self.compared_candidate_id,
            "hamming_distance": self.hamming_distance,
            "kind": self.kind,
        }


@dataclass(frozen=True)
class SelectedOccurrence:
    occurrence_rank: int
    candidate: CoverageCandidate
    origins: tuple[str, ...]
    probe_slot: str | None = None
    target_gallery_index: int | None = None
    actual_gallery_index: int | None = None

    def as_record(self) -> dict[str, Any]:
        return {
            "actual_gallery_index": self.actual_gallery_index,
            "candidate": self.candidate.as_record(),
            "candidate_record_sha256": self.candidate.record_sha256,
            "occurrence_rank": self.occurrence_rank,
            "origins": list(self.origins),
            "planned_e1_exact_group_id": "e1px_"
            + self.candidate.normalized_pixel_sha256,
            "probe_slot": self.probe_slot,
            "target_gallery_index": self.target_gallery_index,
        }

    @property
    def record_sha256(self) -> str:
        return canonical_sha256(self.as_record())


@dataclass(frozen=True)
class ProbeDecision:
    slot: str
    target_gallery_index: int | None
    chosen_candidate_id: str | None
    actual_gallery_index: int | None
    state: str
    rejected: tuple[tuple[str, RedundancyEvidence], ...]

    def as_record(self) -> dict[str, Any]:
        return {
            "actual_gallery_index": self.actual_gallery_index,
            "chosen_candidate_id": self.chosen_candidate_id,
            "rejected": [
                {"candidate_id": candidate_id, "evidence": evidence.as_record()}
                for candidate_id, evidence in self.rejected
            ],
            "slot": self.slot,
            "state": self.state,
            "target_gallery_index": self.target_gallery_index,
        }


@dataclass(frozen=True)
class BuildingCoveragePlan:
    selection_id: str
    quality_pool_state: str
    gallery_pool_count: int
    selected_occurrences: tuple[SelectedOccurrence, ...]
    probe_decisions: tuple[ProbeDecision, ...]

    def as_record(self) -> dict[str, Any]:
        return {
            "gallery_pool_count": self.gallery_pool_count,
            "probe_decisions": [value.as_record() for value in self.probe_decisions],
            "quality_pool_state": self.quality_pool_state,
            "selected_occurrences": [
                {
                    "occurrence": value.as_record(),
                    "occurrence_record_sha256": value.record_sha256,
                }
                for value in self.selected_occurrences
            ],
            "selection_id": self.selection_id,
        }

    @property
    def record_sha256(self) -> str:
        return canonical_sha256(self.as_record())


def phash_hamming(left: str, right: str) -> int:
    if len(left) != 64 or len(right) != 64:
        raise ValueError("pHash values must be 256-bit hexadecimal strings")
    try:
        return (int(left, 16) ^ int(right, 16)).bit_count()
    except ValueError as exc:
        raise ValueError("pHash values must be hexadecimal") from exc


def redundancy_against(
    candidate: CoverageCandidate,
    selected: Sequence[CoverageCandidate],
) -> RedundancyEvidence | None:
    """Return first chosen-star redundancy evidence, never transitive closure."""

    for other in selected:
        if candidate.normalized_pixel_sha256 == other.normalized_pixel_sha256:
            return RedundancyEvidence("exact_normalized_pixel", other.candidate_id)
    for other in selected:
        if candidate.phash_node_id == other.phash_node_id:
            return RedundancyEvidence("identical_phash", other.candidate_id, 0)
    for other in selected:
        distance = phash_hamming(candidate.phash_hex, other.phash_hex)
        if distance <= 8:
            return RedundancyEvidence("direct_phash_le8", other.candidate_id, distance)
    return None


def select_building_coverage(
    candidates: Iterable[CoverageCandidate],
) -> BuildingCoveragePlan:
    """Select frozen P2 anchors plus early/middle/late non-redundant probes."""

    values = tuple(sorted(candidates, key=lambda x: (x.editorial_rank, x.candidate_id)))
    if not values:
        raise ValueError("coverage planning requires at least one successful candidate")
    selection_ids = {value.selection_id for value in values}
    if len(selection_ids) != 1:
        raise ValueError("coverage candidates must belong to one selection_id")
    if len({value.candidate_id for value in values}) != len(values):
        raise ValueError("candidate IDs must be unique within one building")
    anchors = sorted(
        (value for value in values if value.p2_shortlist_rank is not None),
        key=lambda x: (int(x.p2_shortlist_rank or 0), x.candidate_id),
    )
    if not anchors or len(anchors) > 3:
        raise ValueError("P2 must contain between one and three anchors")
    expected_ranks = list(range(1, len(anchors) + 1))
    if [value.p2_shortlist_rank for value in anchors] != expected_ranks:
        raise ValueError("P2 shortlist ranks must be contiguous from one")

    selected: list[SelectedOccurrence] = []
    selected_candidates: list[CoverageCandidate] = []
    for anchor in anchors:
        rank = int(anchor.p2_shortlist_rank or 0)
        origins = [f"representative_p2_rank_{rank}"]
        if rank == 1:
            origins.append("coverage_anchor_p1_rank_1")
        selected_candidates.append(anchor)
        selected.append(
            SelectedOccurrence(
                occurrence_rank=len(selected) + 1,
                candidate=anchor,
                origins=tuple(origins),
            )
        )

    any_non_risk = any(not value.hard_risk for value in values)
    gallery = [
        value
        for value in values
        if value.is_gallery and (not any_non_risk or value.hard_risk is False)
    ]
    gallery.sort(
        key=lambda value: (
            value.source_ordinal is None,
            value.source_ordinal if value.source_ordinal is not None else 2**63 - 1,
            value.editorial_rank,
            value.candidate_id,
        )
    )
    quality_pool_state = "non_hard_risk" if any_non_risk else "all_risk_qa_fallback"
    decisions: list[ProbeDecision] = []
    targets = (
        ("gallery_early", 0 if gallery else None),
        ("gallery_middle", (len(gallery) - 1) // 2 if gallery else None),
        ("gallery_late", len(gallery) - 1 if gallery else None),
    )
    selected_ids = {value.candidate_id for value in selected_candidates}
    for slot, target in targets:
        if target is None:
            decisions.append(ProbeDecision(slot, None, None, None, "unfilled_no_gallery", ()))
            continue
        ordered_indexes = sorted(
            range(len(gallery)),
            key=lambda index: (
                abs(index - target),
                index,
                gallery[index].candidate_id,
            ),
        )
        rejected: list[tuple[str, RedundancyEvidence]] = []
        chosen: CoverageCandidate | None = None
        chosen_index: int | None = None
        for index in ordered_indexes:
            candidate = gallery[index]
            if candidate.candidate_id in selected_ids:
                continue
            evidence = redundancy_against(candidate, selected_candidates)
            if evidence is not None:
                rejected.append((candidate.candidate_id, evidence))
                continue
            chosen = candidate
            chosen_index = index
            break
        if chosen is None:
            decisions.append(
                ProbeDecision(
                    slot,
                    target,
                    None,
                    None,
                    "unfilled_no_nonredundant_candidate",
                    tuple(rejected),
                )
            )
            continue
        selected_ids.add(chosen.candidate_id)
        selected_candidates.append(chosen)
        selected.append(
            SelectedOccurrence(
                occurrence_rank=len(selected) + 1,
                candidate=chosen,
                origins=("coverage_probe",),
                probe_slot=slot,
                target_gallery_index=target,
                actual_gallery_index=chosen_index,
            )
        )
        decisions.append(
            ProbeDecision(
                slot,
                target,
                chosen.candidate_id,
                chosen_index,
                "filled",
                tuple(rejected),
            )
        )
    if len(selected) > MAX_OCCURRENCES_PER_BUILDING:
        raise AssertionError("semantic coverage exceeded the per-building cap")
    # Re-number defensively if a future caller supplied non-canonical ranks.
    selected = [replace(value, occurrence_rank=index) for index, value in enumerate(selected, 1)]
    return BuildingCoveragePlan(
        selection_id=values[0].selection_id,
        quality_pool_state=quality_pool_state,
        gallery_pool_count=len(gallery),
        selected_occurrences=tuple(selected),
        probe_decisions=tuple(decisions),
    )


__all__ = [
    "BuildingCoveragePlan",
    "BuildingInventoryItem",
    "CoverageCandidate",
    "DEFAULT_SAMPLE_SEED",
    "DEFAULT_SAMPLE_SIZE",
    "GuardedSelection",
    "MAX_OCCURRENCES_PER_BUILDING",
    "P2_POLICY_ID",
    "ProbeDecision",
    "RedundancyEvidence",
    "SEMANTIC_COVERAGE_MANIFEST_DOMAIN",
    "SEMANTIC_COVERAGE_VERSION",
    "SelectedOccurrence",
    "default_n10_guards",
    "deterministic_guard_score",
    "phash_hamming",
    "redundancy_against",
    "select_building_coverage",
    "select_guarded_n10",
]
