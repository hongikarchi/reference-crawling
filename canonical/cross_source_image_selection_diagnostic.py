"""Deterministic, offline P2-evidence diagnostic sampling for E3.

The ordinary E3 smoke sample is representative of source/building strata.  It
is therefore allowed to contain no within-building duplicate evidence at all.
This module builds a *separate* diagnostic selection plan that deliberately
covers the three P2 suppression mechanisms:

* exact normalized pixels;
* identical pHash with different normalized pixels; and
* an explicit, direct pHash-distance <= 8 edge between different pHash nodes.

Only relations that actually produce the corresponding P2 suppression reason
with the frozen shortlist policy are eligible.  This makes the diagnostic
useful for policy testing without claiming that it is a representative quality
sample.  The module reads the accepted E2 SQLite through its immutable source
adapter and performs no network, Vision, LLM, or final representative-image
work.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from canonical.cross_source_image_selection import (
    Candidate,
    DirectPHashEdge,
    P2_POLICY_ID,
    SamplingItem,
    canonical_json,
    canonical_sha256,
    compare_standard_policies,
    deterministic_sample_score,
    deterministic_stratified_sample,
)
from canonical.cross_source_image_selection_sources import (
    BuildingImageCandidate,
    BuildingSummary,
    E2SelectionSources,
)


E3_DIAGNOSTIC_SAMPLE_VERSION = "archibe-e3-p2-evidence-diagnostic-sample-v1"
E3_DIAGNOSTIC_MANIFEST_DOMAIN = "archibe-e3-p2-diagnostic-manifest-v1"
DEFAULT_DIAGNOSTIC_SEED = "archibe-e3-p2-evidence-diagnostic-v1"

EXACT_PIXEL = "exact_pixel"
IDENTICAL_PHASH = "identical_phash_distinct_pixel"
DIRECT_PHASH_LE8 = "direct_phash_le8"
EVIDENCE_KINDS = (EXACT_PIXEL, IDENTICAL_PHASH, DIRECT_PHASH_LE8)

_SUPPRESSION_REASON_TO_KIND = {
    "suppressed_exact_pixel": EXACT_PIXEL,
    "suppressed_identical_phash": IDENTICAL_PHASH,
    "suppressed_direct_phash_le8": DIRECT_PHASH_LE8,
}


def _primary_role(roles: Sequence[str]) -> str:
    values = tuple(sorted(set(str(role) for role in roles)))
    if not values:
        raise ValueError("E2 diagnostic candidate has no source role")
    if "cover" in values:
        return "cover"
    if "gallery" in values:
        return "gallery"
    return values[0]


def _map_candidate(value: BuildingImageCandidate) -> Candidate:
    return Candidate(
        source=value.source,
        source_building_id=value.source_building_id,
        source_asset_id=value.source_asset_id,
        fingerprint_status="success",
        role=_primary_role(value.roles),
        ordinal=value.lowest_project_ordinal,
        original_width=value.original_width,
        original_height=value.original_height,
        quality_flags=tuple(value.quality_flags),
        source_record_sha256=value.source_asset_record_sha256,
        exact_cluster_id=value.exact_cluster_id,
        phash_node_id=value.phash_node_id,
        canonical_url=value.canonical_url,
    )


def _summary_record(summary: BuildingSummary) -> dict[str, Any]:
    return {
        "cross_source_candidate": summary.cross_source_candidate,
        "name": summary.name,
        "quality_risk_cover_count": summary.quality_risk_cover_count,
        "source": summary.source,
        "source_building_id": summary.source_building_id,
        "source_record_sha256": summary.source_record_sha256,
        "stratum": summary.stratum,
        "successful_asset_count": summary.successful_asset_count,
        "successful_cover_count": summary.successful_cover_count,
    }


def _candidate_evidence_record(value: BuildingImageCandidate) -> dict[str, Any]:
    return {
        "building_relation_record_sha256": value.building_relation_record_sha256,
        "exact_cluster_id": value.exact_cluster_id,
        "normalized_pixel_sha256": value.normalized_pixel_sha256,
        "phash_node_id": value.phash_node_id,
        "source": value.source,
        "source_asset_id": value.source_asset_id,
        "source_asset_record_sha256": value.source_asset_record_sha256,
        "source_building_id": value.source_building_id,
    }


@dataclass(frozen=True)
class DiagnosticBuilding:
    """One building where frozen P2 actually suppresses duplicate evidence."""

    summary: BuildingSummary
    candidate_count: int
    intrinsic_pair_counts: tuple[tuple[str, int], ...]
    p2_suppression_counts: tuple[tuple[str, int], ...]
    candidate_evidence_manifest_sha256: str
    direct_edge_manifest_sha256: str

    def __post_init__(self) -> None:
        if self.candidate_count < 2:
            raise ValueError("a diagnostic building needs at least two candidates")
        intrinsic = dict(self.intrinsic_pair_counts)
        suppressed = dict(self.p2_suppression_counts)
        if set(intrinsic) != set(EVIDENCE_KINDS):
            raise ValueError("intrinsic_pair_counts must contain every evidence kind")
        if set(suppressed) != set(EVIDENCE_KINDS):
            raise ValueError("p2_suppression_counts must contain every evidence kind")
        if any(value < 0 for value in (*intrinsic.values(), *suppressed.values())):
            raise ValueError("diagnostic evidence counts must be non-negative")
        if not any(suppressed.values()):
            raise ValueError("diagnostic building must produce a P2 suppression")

    @property
    def source(self) -> str:
        return self.summary.source

    @property
    def source_building_id(self) -> str:
        return self.summary.source_building_id

    @property
    def identity(self) -> str:
        return f"{self.source}:building:{self.source_building_id}"

    @property
    def evidence_kinds(self) -> tuple[str, ...]:
        counts = dict(self.p2_suppression_counts)
        return tuple(kind for kind in EVIDENCE_KINDS if counts[kind] > 0)

    @property
    def signature(self) -> str:
        return "+".join(self.evidence_kinds)

    def as_record(self) -> dict[str, Any]:
        return {
            "candidate_count": self.candidate_count,
            "candidate_evidence_manifest_sha256": (
                self.candidate_evidence_manifest_sha256
            ),
            "direct_edge_manifest_sha256": self.direct_edge_manifest_sha256,
            "evidence_kinds": list(self.evidence_kinds),
            "identity": self.identity,
            "intrinsic_pair_counts": dict(self.intrinsic_pair_counts),
            "p2_suppression_counts": dict(self.p2_suppression_counts),
            "source_summary": _summary_record(self.summary),
            "version": E3_DIAGNOSTIC_SAMPLE_VERSION,
        }

    @property
    def record_sha256(self) -> str:
        return canonical_sha256(self.as_record())

    @property
    def sampling_item(self) -> SamplingItem:
        return SamplingItem(
            identity=self.identity,
            source=self.source,
            stratum=f"p2_evidence:{self.signature}",
            input_record_sha256=self.record_sha256,
        )


@dataclass(frozen=True)
class DiagnosticSamplePlan:
    """Immutable selection plan consumed by an E3 diagnostic sidecar build."""

    sample_size: int
    sample_seed: str
    inventory: tuple[DiagnosticBuilding, ...]
    selected: tuple[DiagnosticBuilding, ...]
    inventory_manifest_sha256: str
    ordered_selection_manifest_sha256: str
    population_by_source_and_kind: tuple[tuple[str, str, int], ...]
    selected_by_source_and_kind: tuple[tuple[str, str, int], ...]

    @property
    def selected_sampling_items(self) -> tuple[SamplingItem, ...]:
        return tuple(value.sampling_item for value in self.selected)

    def as_manifest(self) -> dict[str, Any]:
        return {
            "authoritative": False,
            "creates_final_representative": False,
            "creates_vision_tasks": False,
            "evidence_kinds": list(EVIDENCE_KINDS),
            "inventory_count": len(self.inventory),
            "inventory_manifest_sha256": self.inventory_manifest_sha256,
            "llm_requests": 0,
            "network_requests": 0,
            "ordered_selection_manifest_sha256": (
                self.ordered_selection_manifest_sha256
            ),
            "population_by_source_and_kind": [
                {"count": count, "evidence_kind": kind, "source": source}
                for source, kind, count in self.population_by_source_and_kind
            ],
            "sample_seed": self.sample_seed,
            "sample_size": self.sample_size,
            "selected": [
                {
                    "diagnostic_record": value.as_record(),
                    "diagnostic_record_sha256": value.record_sha256,
                    "rank": rank,
                    "sampling_item": value.sampling_item.as_record(),
                    "sampling_item_record_sha256": value.sampling_item.record_sha256,
                }
                for rank, value in enumerate(self.selected, 1)
            ],
            "selected_by_source_and_kind": [
                {"count": count, "evidence_kind": kind, "source": source}
                for source, kind, count in self.selected_by_source_and_kind
            ],
            "selection_mode": "diagnostic_sample",
            "version": E3_DIAGNOSTIC_SAMPLE_VERSION,
            "vision_requests": 0,
        }


def _grouped_candidates(
    values: Iterable[BuildingImageCandidate],
) -> Iterable[tuple[tuple[str, str], tuple[BuildingImageCandidate, ...]]]:
    current_key: tuple[str, str] | None = None
    group: list[BuildingImageCandidate] = []
    for value in values:
        key = (value.source, value.source_building_id)
        if current_key is not None and key != current_key:
            yield current_key, tuple(group)
            group = []
        current_key = key
        group.append(value)
    if current_key is not None:
        yield current_key, tuple(group)


def _pair_count(values: Iterable[int]) -> int:
    return sum(value * (value - 1) // 2 for value in values)


def _building_diagnostic(
    summary: BuildingSummary,
    candidates: Sequence[BuildingImageCandidate],
    *,
    source_edges_by_node: Mapping[str, Sequence[Any]],
) -> DiagnosticBuilding | None:
    by_pixel = Counter(value.normalized_pixel_sha256 for value in candidates)
    by_node: dict[str, list[BuildingImageCandidate]] = defaultdict(list)
    for value in candidates:
        by_node[value.phash_node_id].append(value)

    exact_pairs = _pair_count(by_pixel.values())
    identical_distinct_pairs = 0
    for node_values in by_node.values():
        node_pixels = Counter(value.normalized_pixel_sha256 for value in node_values)
        identical_distinct_pairs += (
            len(node_values) * (len(node_values) - 1) // 2
            - _pair_count(node_pixels.values())
        )

    seen_source_edges: dict[str, Any] = {}
    direct_pairs = 0
    for node in sorted(by_node):
        for edge in source_edges_by_node.get(node, ()):
            other = (
                edge.right_node_id
                if edge.left_node_id == node
                else edge.left_node_id
            )
            if other not in by_node or edge.edge_id in seen_source_edges:
                continue
            seen_source_edges[edge.edge_id] = edge
            direct_pairs += len(by_node[node]) * len(by_node[other])

    # Avoid running the policy engine over the roughly ninety thousand
    # ordinary multi-image buildings.  Only a building with an intrinsic P2
    # relation can produce a suppression reason.
    if not (exact_pairs or identical_distinct_pairs or direct_pairs):
        return None

    mapped = tuple(_map_candidate(value) for value in candidates)
    mapped_by_asset = {
        value.source_asset_id: mapped_value
        for value, mapped_value in zip(candidates, mapped)
    }
    direct_candidate_edges: dict[tuple[str, str], DirectPHashEdge] = {}
    for edge in seen_source_edges.values():
        for left in by_node[edge.left_node_id]:
            for right in by_node[edge.right_node_id]:
                mapped_edge = DirectPHashEdge(
                    left_candidate_id=mapped_by_asset[
                        left.source_asset_id
                    ].candidate_id,
                    right_candidate_id=mapped_by_asset[
                        right.source_asset_id
                    ].candidate_id,
                    distance=edge.hamming_distance,
                )
                direct_candidate_edges[mapped_edge.pair] = mapped_edge

    p2 = next(
        value
        for value in compare_standard_policies(
            mapped,
            direct_phash_edges=tuple(direct_candidate_edges.values()),
        )
        if value.policy.policy_id == P2_POLICY_ID
    )
    suppression_counts = Counter({kind: 0 for kind in EVIDENCE_KINDS})
    for evaluation in p2.evaluations:
        for reason in evaluation.reasons:
            kind = _SUPPRESSION_REASON_TO_KIND.get(reason)
            if kind is not None:
                suppression_counts[kind] += 1
    if not any(suppression_counts.values()):
        return None

    candidate_manifest = canonical_sha256(
        {
            "ordered_candidates": [
                {
                    "candidate": _candidate_evidence_record(value),
                    "record_sha256": canonical_sha256(
                        _candidate_evidence_record(value)
                    ),
                }
                for value in candidates
            ],
            "version": E3_DIAGNOSTIC_SAMPLE_VERSION,
        }
    )
    edge_manifest = canonical_sha256(
        {
            "ordered_edges": [
                {
                    "edge_id": edge.edge_id,
                    "edge_record_sha256": edge.edge_record_sha256,
                    "hamming_distance": edge.hamming_distance,
                    "left_node_id": edge.left_node_id,
                    "right_node_id": edge.right_node_id,
                }
                for edge in sorted(
                    seen_source_edges.values(), key=lambda value: value.edge_id
                )
            ],
            "version": E3_DIAGNOSTIC_SAMPLE_VERSION,
        }
    )
    return DiagnosticBuilding(
        summary=summary,
        candidate_count=len(candidates),
        intrinsic_pair_counts=tuple(
            (kind, count)
            for kind, count in (
                (EXACT_PIXEL, exact_pairs),
                (IDENTICAL_PHASH, identical_distinct_pairs),
                (DIRECT_PHASH_LE8, direct_pairs),
            )
        ),
        p2_suppression_counts=tuple(
            (kind, suppression_counts[kind]) for kind in EVIDENCE_KINDS
        ),
        candidate_evidence_manifest_sha256=candidate_manifest,
        direct_edge_manifest_sha256=edge_manifest,
    )


def collect_diagnostic_inventory(
    source: E2SelectionSources,
) -> tuple[DiagnosticBuilding, ...]:
    """Stream E2 and return buildings with a real frozen-P2 suppression.

    The global direct-edge ledger is small (about fifty thousand accepted E2
    rows).  It is indexed in memory once; candidate images remain bounded to a
    single source-qualified building while streaming.
    """

    summaries = {
        (value.source, value.source_building_id): value
        for value in source.iter_building_summaries()
    }
    edges_by_node: dict[str, list[Any]] = defaultdict(list)
    for edge in source.direct_phash_pairs():
        edges_by_node[edge.left_node_id].append(edge)
        edges_by_node[edge.right_node_id].append(edge)

    inventory: list[DiagnosticBuilding] = []
    for key, candidates in _grouped_candidates(source.iter_all_candidates()):
        if len(candidates) < 2:
            continue
        summary = summaries.get(key)
        if summary is None:
            raise ValueError(f"E2 candidate building lacks summary: {key!r}")
        diagnostic = _building_diagnostic(
            summary,
            candidates,
            source_edges_by_node=edges_by_node,
        )
        if diagnostic is not None:
            inventory.append(diagnostic)
    return tuple(sorted(inventory, key=lambda value: (value.source, value.source_building_id)))


def _score(seed: str, phase: str, item: DiagnosticBuilding) -> str:
    return canonical_sha256(
        {
            "identity": item.identity,
            "phase": phase,
            "seed": seed,
            "version": E3_DIAGNOSTIC_SAMPLE_VERSION,
        }
    )


def select_diagnostic_sample(
    inventory: Iterable[DiagnosticBuilding],
    *,
    sample_size: int,
    seed: str = DEFAULT_DIAGNOSTIC_SEED,
) -> DiagnosticSamplePlan:
    """Select a deterministic targeted sample with explicit coverage guards.

    All three globally available evidence kinds are mandatory.  At N>=9 the
    algorithm also has enough worst-case budget to cover every available
    source x evidence cell (at most six); in practice multi-label buildings may
    satisfy several guards at once.  Remaining rows are stratified by source x
    exact evidence signature.  This is diagnostic oversampling, not a rate or
    quality estimate of the full population.
    """

    values = tuple(sorted(inventory, key=lambda value: value.identity))
    if isinstance(sample_size, bool) or not isinstance(sample_size, int):
        raise TypeError("sample_size must be an integer")
    if sample_size < len(EVIDENCE_KINDS):
        raise ValueError("diagnostic sample_size must be at least three")
    if sample_size > len(values):
        raise ValueError("sample_size exceeds diagnostic population")
    if not seed or seed != seed.strip():
        raise ValueError("diagnostic seed must be non-empty without outer whitespace")
    identities = [value.identity for value in values]
    if len(set(identities)) != len(identities):
        raise ValueError("diagnostic inventory identities must be unique")

    available_kinds = {
        kind for value in values for kind in value.evidence_kinds
    }
    missing = set(EVIDENCE_KINDS) - available_kinds
    if missing:
        raise ValueError(
            "diagnostic population lacks required P2 evidence: "
            + ", ".join(sorted(missing))
        )

    selected: list[DiagnosticBuilding] = []
    selected_ids: set[str] = set()

    def choose_for_token(
        token: tuple[str | None, str], phase: str
    ) -> None:
        source_name, kind = token
        if any(
            kind in value.evidence_kinds
            and (source_name is None or value.source == source_name)
            for value in selected
        ):
            return
        choices = [
            value
            for value in values
            if value.identity not in selected_ids
            and kind in value.evidence_kinds
            and (source_name is None or value.source == source_name)
        ]
        if not choices or len(selected) >= sample_size:
            return
        winner = min(
            choices,
            key=lambda value: (
                _score(seed, f"{phase}:{source_name or '*'}:{kind}", value),
                value.identity,
            ),
        )
        selected.append(winner)
        selected_ids.add(winner.identity)

    # First guarantee the three mechanisms globally.
    for kind in EVIDENCE_KINDS:
        choose_for_token((None, kind), "global-kind")

    source_kind_cells = sorted(
        {
            (value.source, kind)
            for value in values
            for kind in value.evidence_kinds
        }
    )
    # A conservative 3 + number-of-cells budget guarantees this second layer
    # even if none of the global picks happened to satisfy a new cell.
    if sample_size >= len(EVIDENCE_KINDS) + len(source_kind_cells):
        for cell in source_kind_cells:
            choose_for_token(cell, "source-kind")

    remaining_count = sample_size - len(selected)
    if remaining_count:
        remaining = [
            value for value in values if value.identity not in selected_ids
        ]
        sampling_by_id = {
            value.identity: SamplingItem(
                identity=value.identity,
                source=value.source,
                stratum=f"p2_evidence:{value.signature}",
                input_record_sha256=value.record_sha256,
            )
            for value in remaining
        }
        fill = deterministic_stratified_sample(
            sampling_by_id.values(),
            sample_size=remaining_count,
            seed=f"{seed}:diagnostic-fill",
        )
        by_id = {value.identity: value for value in remaining}
        selected.extend(by_id[item.identity] for item in fill)

    population_counts = Counter(
        (value.source, kind)
        for value in values
        for kind in value.evidence_kinds
    )
    selected_counts = Counter(
        (value.source, kind)
        for value in selected
        for kind in value.evidence_kinds
    )
    inventory_manifest = canonical_sha256(
        {
            "ordered_inventory": [
                {
                    "diagnostic_record": value.as_record(),
                    "diagnostic_record_sha256": value.record_sha256,
                    "rank": rank,
                }
                for rank, value in enumerate(values, 1)
            ],
            "version": E3_DIAGNOSTIC_SAMPLE_VERSION,
        }
    )
    ordered_manifest = canonical_sha256(
        {
            "inventory_manifest_sha256": inventory_manifest,
            "ordered_selected": [
                {
                    "diagnostic_record_sha256": value.record_sha256,
                    "identity": value.identity,
                    "rank": rank,
                    "sampling_item_record_sha256": value.sampling_item.record_sha256,
                }
                for rank, value in enumerate(selected, 1)
            ],
            "sample_seed": seed,
            "sample_size": sample_size,
            "version": E3_DIAGNOSTIC_SAMPLE_VERSION,
        }
    )
    return DiagnosticSamplePlan(
        sample_size=sample_size,
        sample_seed=seed,
        inventory=values,
        selected=tuple(selected),
        inventory_manifest_sha256=inventory_manifest,
        ordered_selection_manifest_sha256=ordered_manifest,
        population_by_source_and_kind=tuple(
            (source_name, kind, population_counts[(source_name, kind)])
            for source_name, kind in sorted(population_counts)
        ),
        selected_by_source_and_kind=tuple(
            (source_name, kind, selected_counts[(source_name, kind)])
            for source_name, kind in sorted(population_counts)
        ),
    )


def build_diagnostic_sample_plan(
    source: E2SelectionSources,
    *,
    sample_size: int,
    seed: str = DEFAULT_DIAGNOSTIC_SEED,
) -> DiagnosticSamplePlan:
    """Collect the immutable E2 diagnostic population and select from it."""

    return select_diagnostic_sample(
        collect_diagnostic_inventory(source),
        sample_size=sample_size,
        seed=seed,
    )


def write_diagnostic_manifest(
    path: Path | str,
    plan: DiagnosticSamplePlan,
    *,
    e2_path: Path | str,
    e2_size_bytes: int,
    e2_byte_sha256: str,
    e2_logical_sha256: str,
) -> Path:
    """Write a small no-clobber JSON manifest for an offline diagnostic plan."""

    output = Path(path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = plan.as_manifest()
    payload["e2_input"] = {
        "byte_sha256": e2_byte_sha256,
        "logical_sha256": e2_logical_sha256,
        "path": str(Path(e2_path).resolve()),
        "size_bytes": e2_size_bytes,
    }
    payload["diagnostic_manifest_sha256"] = canonical_sha256(
        {
            "domain": E3_DIAGNOSTIC_MANIFEST_DOMAIN,
            "manifest": payload,
        }
    )
    with output.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical_json(payload))
        handle.write("\n")
    return output


__all__ = [
    "DEFAULT_DIAGNOSTIC_SEED",
    "DIRECT_PHASH_LE8",
    "DiagnosticBuilding",
    "DiagnosticSamplePlan",
    "E3_DIAGNOSTIC_SAMPLE_VERSION",
    "E3_DIAGNOSTIC_MANIFEST_DOMAIN",
    "EVIDENCE_KINDS",
    "EXACT_PIXEL",
    "IDENTICAL_PHASH",
    "build_diagnostic_sample_plan",
    "collect_diagnostic_inventory",
    "select_diagnostic_sample",
    "write_diagnostic_manifest",
]
