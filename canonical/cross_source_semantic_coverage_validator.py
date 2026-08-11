"""Read-only replay validator for semantic-coverage selection manifests.

The JSON under review is never used to choose acceptance parameters or input
artifacts.  Callers pin both immutable SQLite files, the sample size, the
sample seed, and the per-building cap.  The validator then rebuilds the
population, guarded building sample, E2/E3 joins, and every per-building image
selection before comparing the canonical manifest byte-for-byte.

No network, image decoding, Vision, or LLM operation exists in this module.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from canonical.cross_source_image_selection import canonical_json, canonical_sha256
from canonical.cross_source_semantic_coverage import (
    BuildingInventoryItem,
    CoverageCandidate,
    MAX_OCCURRENCES_PER_BUILDING,
    SEMANTIC_COVERAGE_MANIFEST_DOMAIN,
    SEMANTIC_COVERAGE_VERSION,
)
from canonical.cross_source_semantic_coverage_sources import (
    ArtifactSpec,
    ArtifactSnapshot,
    inspect_artifact,
)


@dataclass(frozen=True)
class SemanticCoverageValidationCheck:
    name: str
    passed: bool
    expected: str
    actual: str


@dataclass(frozen=True)
class SemanticCoverageValidationResult:
    manifest_path: Path
    passed: bool
    checks: tuple[SemanticCoverageValidationCheck, ...]
    manifest_size_bytes: int
    manifest_byte_sha256: str
    manifest_sha256: str | None
    population_count: int
    selected_building_count: int
    selected_image_count: int
    ordered_building_manifest_sha256: str | None
    ordered_occurrence_manifest_sha256: str | None
    e2_byte_sha256: str | None
    e3_byte_sha256: str | None

    @property
    def failed_check_names(self) -> tuple[str, ...]:
        return tuple(value.name for value in self.checks if not value.passed)

    def as_dict(self) -> dict[str, Any]:
        return {
            "checks": [
                {
                    "actual": value.actual,
                    "expected": value.expected,
                    "name": value.name,
                    "passed": value.passed,
                }
                for value in self.checks
            ],
            "e2_byte_sha256": self.e2_byte_sha256,
            "e3_byte_sha256": self.e3_byte_sha256,
            "failed_check_names": list(self.failed_check_names),
            "llm_requests": 0,
            "manifest_byte_sha256": self.manifest_byte_sha256,
            "manifest_path": str(self.manifest_path),
            "manifest_sha256": self.manifest_sha256,
            "manifest_size_bytes": self.manifest_size_bytes,
            "network_requests": 0,
            "ordered_building_manifest_sha256": (
                self.ordered_building_manifest_sha256
            ),
            "ordered_occurrence_manifest_sha256": (
                self.ordered_occurrence_manifest_sha256
            ),
            "passed": self.passed,
            "population_count": self.population_count,
            "selected_building_count": self.selected_building_count,
            "selected_image_count": self.selected_image_count,
            "vision_requests": 0,
        }


def _sha256_file(path: Path, *, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _sidecars(path: Path) -> tuple[Path, ...]:
    return tuple(
        candidate
        for candidate in (
            Path(str(path) + "-wal"),
            Path(str(path) + "-shm"),
            Path(str(path) + "-journal"),
            Path(str(path) + ".lock"),
        )
        if candidate.exists()
    )


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return canonical_json(value)
    except (TypeError, ValueError):
        return repr(value)


def _safe_sha256(value: Any) -> str:
    try:
        return canonical_sha256(value)
    except (TypeError, ValueError) as exc:
        return f"invalid-canonical-value:{type(exc).__name__}:{exc}"


def _check(
    checks: list[SemanticCoverageValidationCheck],
    name: str,
    passed: bool,
    expected: Any,
    actual: Any,
) -> None:
    checks.append(
        SemanticCoverageValidationCheck(
            name=name,
            passed=bool(passed),
            expected=_text(expected),
            actual=_text(actual),
        )
    )


def _empty_result(
    *,
    path: Path,
    checks: list[SemanticCoverageValidationCheck],
    size: int,
    byte_sha256: str,
) -> SemanticCoverageValidationResult:
    return SemanticCoverageValidationResult(
        manifest_path=path,
        passed=False,
        checks=tuple(checks),
        manifest_size_bytes=size,
        manifest_byte_sha256=byte_sha256,
        manifest_sha256=None,
        population_count=0,
        selected_building_count=0,
        selected_image_count=0,
        ordered_building_manifest_sha256=None,
        ordered_occurrence_manifest_sha256=None,
        e2_byte_sha256=None,
        e3_byte_sha256=None,
    )


def _count_selected_images(payload: Any) -> int:
    if not isinstance(payload, list):
        return 0
    count = 0
    for building in payload:
        if not isinstance(building, dict):
            continue
        wrapped = building.get("selected_building")
        if isinstance(wrapped, dict):
            building = wrapped
        coverage = building.get("coverage_plan")
        if not isinstance(coverage, dict):
            coverage = building.get("coverage")
        if not isinstance(coverage, dict):
            continue
        values = coverage.get("selected_occurrences")
        if isinstance(values, list):
            count += len(values)
    return count


def _inventory_record(value: BuildingInventoryItem) -> dict[str, Any]:
    return {
        "cross_source_candidate": value.cross_source_candidate,
        "name": value.name,
        "p1_rank1_changed": value.p1_rank1_changed,
        "p2_top3_changed": value.p2_top3_changed,
        "population_stratum": value.population_stratum,
        "qa_fallback": value.qa_fallback,
        "selection_id": value.selection_id,
        "selection_record_sha256": value.selection_record_sha256,
        "source": value.source,
        "source_building_id": value.source_building_id,
        "source_record_sha256": value.source_record_sha256,
        "successful_asset_count": value.successful_asset_count,
        "successful_gallery_count": value.successful_gallery_count,
    }


def _inventory_sha256(value: BuildingInventoryItem) -> str:
    return canonical_sha256(_inventory_record(value))


def _guard_predicate(name: str, value: BuildingInventoryItem) -> bool:
    checks = {
        "architizer_qa_fallback": value.qa_fallback,
        "architizer_p1_rank1_changed": value.p1_rank1_changed,
        "divisare_p1_rank1_changed": value.p1_rank1_changed,
        "architizer_p2_top3_changed": value.p2_top3_changed,
        "divisare_p2_top3_changed": value.p2_top3_changed,
        "architizer_gallery_fallback": (
            value.population_stratum == "gallery_fallback"
        ),
        "divisare_gallery_fallback": (
            value.population_stratum == "gallery_fallback"
        ),
        "architizer_cross_source": value.cross_source_candidate,
        "divisare_cross_source": value.cross_source_candidate,
        "divisare_ordinary_long_gallery": (
            value.population_stratum == "ordinary"
            and value.successful_gallery_count >= 20
        ),
    }
    return bool(checks[name])


_GUARDS = (
    ("architizer_qa_fallback", "architizer"),
    ("architizer_p1_rank1_changed", "architizer"),
    ("divisare_p1_rank1_changed", "divisare"),
    ("architizer_p2_top3_changed", "architizer"),
    ("divisare_p2_top3_changed", "divisare"),
    ("architizer_gallery_fallback", "architizer"),
    ("divisare_gallery_fallback", "divisare"),
    ("architizer_cross_source", "architizer"),
    ("divisare_cross_source", "divisare"),
    ("divisare_ordinary_long_gallery", "divisare"),
)


def _guard_score(seed: str, name: str, selection_id: str) -> str:
    return canonical_sha256(
        {
            "domain": "semantic-coverage-guard-score",
            "guard_name": name,
            "seed": seed,
            "selection_id": selection_id,
            "version": SEMANTIC_COVERAGE_VERSION,
        }
    )


def _replay_guarded_selection(
    inventory: Iterable[BuildingInventoryItem], *, seed: str
) -> tuple[tuple[dict[str, Any], ...], tuple[BuildingInventoryItem, ...]]:
    values = tuple(sorted(inventory, key=lambda value: value.selection_id))
    if len({value.selection_id for value in values}) != len(values):
        raise ValueError("validator inventory contains duplicate selection IDs")
    chosen_ids: set[str] = set()
    records: list[dict[str, Any]] = []
    chosen: list[BuildingInventoryItem] = []
    for rank, (guard_name, guard_source) in enumerate(_GUARDS, 1):
        eligible = [
            value
            for value in values
            if value.source == guard_source
            and _guard_predicate(guard_name, value)
        ]
        available = [
            value for value in eligible if value.selection_id not in chosen_ids
        ]
        if not available:
            raise ValueError(
                f"validator guard has no unused candidate: {guard_name}"
            )
        scored = [
            (_guard_score(seed, guard_name, value.selection_id), value)
            for value in available
        ]
        score, winner = min(
            scored, key=lambda pair: (pair[0], pair[1].selection_id)
        )
        chosen_ids.add(winner.selection_id)
        chosen.append(winner)
        building_record = _inventory_record(winner)
        records.append(
            {
                "available_count_before_pick": len(available),
                "building": building_record,
                "building_record_sha256": canonical_sha256(building_record),
                "eligible_count": len(eligible),
                "guard_name": guard_name,
                "guard_source": guard_source,
                "rank": rank,
                "score_sha256": score,
            }
        )
    return tuple(records), tuple(chosen)


def _candidate_record(value: CoverageCandidate) -> dict[str, Any]:
    return {
        "candidate_id": value.candidate_id,
        "canonical_url": value.canonical_url,
        "e2_asset_record_sha256": value.e2_asset_record_sha256,
        "e2_building_relation_record_sha256": (
            value.e2_building_relation_record_sha256
        ),
        "e3_candidate_record_sha256": value.e3_candidate_record_sha256,
        "e3_ranking_record_sha256": value.e3_ranking_record_sha256,
        "e3_shortlist_item_record_sha256": (
            value.e3_shortlist_item_record_sha256
        ),
        "e3_source_record_sha256": value.e3_source_record_sha256,
        "editorial_rank": value.editorial_rank,
        "fetch_url": value.fetch_url,
        "final_url": value.final_url,
        "hard_risk": value.hard_risk,
        "normalized_height": value.normalized_height,
        "normalized_pixel_sha256": value.normalized_pixel_sha256,
        "normalized_width": value.normalized_width,
        "original_height": value.original_height,
        "original_width": value.original_width,
        "p2_shortlist_rank": value.p2_shortlist_rank,
        "phash_hex": value.phash_hex,
        "phash_node_id": value.phash_node_id,
        "qa_fallback": value.qa_fallback,
        "quality_flags": list(value.quality_flags),
        "raw_response_sha256": value.raw_response_sha256,
        "roles": list(value.roles),
        "selection_id": value.selection_id,
        "source": value.source,
        "source_asset_id": value.source_asset_id,
        "source_building_id": value.source_building_id,
        "source_ordinal": value.source_ordinal,
    }


def _phash_distance(left: str, right: str) -> int:
    if len(left) != 64 or len(right) != 64:
        raise ValueError("validator requires 256-bit pHash values")
    return (int(left, 16) ^ int(right, 16)).bit_count()


def _redundancy(
    candidate: CoverageCandidate,
    selected: Sequence[CoverageCandidate],
) -> dict[str, Any] | None:
    for other in selected:
        if candidate.normalized_pixel_sha256 == other.normalized_pixel_sha256:
            return {
                "compared_candidate_id": other.candidate_id,
                "hamming_distance": None,
                "kind": "exact_normalized_pixel",
            }
    for other in selected:
        if candidate.phash_node_id == other.phash_node_id:
            return {
                "compared_candidate_id": other.candidate_id,
                "hamming_distance": 0,
                "kind": "identical_phash",
            }
    for other in selected:
        distance = _phash_distance(candidate.phash_hex, other.phash_hex)
        if distance <= 8:
            return {
                "compared_candidate_id": other.candidate_id,
                "hamming_distance": distance,
                "kind": "direct_phash_le8",
            }
    return None


def _occurrence_record(
    *,
    rank: int,
    candidate: CoverageCandidate,
    origins: Sequence[str],
    probe_slot: str | None = None,
    target_gallery_index: int | None = None,
    actual_gallery_index: int | None = None,
) -> dict[str, Any]:
    candidate_record = _candidate_record(candidate)
    return {
        "actual_gallery_index": actual_gallery_index,
        "candidate": candidate_record,
        "candidate_record_sha256": canonical_sha256(candidate_record),
        "occurrence_rank": rank,
        "origins": list(origins),
        "planned_e1_exact_group_id": "e1px_"
        + candidate.normalized_pixel_sha256,
        "probe_slot": probe_slot,
        "target_gallery_index": target_gallery_index,
    }


def _replay_building_coverage(
    candidates: Iterable[CoverageCandidate],
) -> dict[str, Any]:
    values = tuple(
        sorted(candidates, key=lambda value: (value.editorial_rank, value.candidate_id))
    )
    if not values:
        raise ValueError("validator coverage building has no candidates")
    selection_ids = {value.selection_id for value in values}
    if len(selection_ids) != 1:
        raise ValueError("validator candidates span multiple buildings")
    anchors = sorted(
        (value for value in values if value.p2_shortlist_rank is not None),
        key=lambda value: (int(value.p2_shortlist_rank or 0), value.candidate_id),
    )
    if not 1 <= len(anchors) <= 3:
        raise ValueError("validator requires one to three P2 anchors")
    if [value.p2_shortlist_rank for value in anchors] != list(
        range(1, len(anchors) + 1)
    ):
        raise ValueError("validator found non-contiguous P2 ranks")

    selected_candidates: list[CoverageCandidate] = []
    occurrence_records: list[dict[str, Any]] = []
    for anchor in anchors:
        p2_rank = int(anchor.p2_shortlist_rank or 0)
        origins = [f"representative_p2_rank_{p2_rank}"]
        if p2_rank == 1:
            origins.append("coverage_anchor_p1_rank_1")
        selected_candidates.append(anchor)
        occurrence = _occurrence_record(
            rank=len(occurrence_records) + 1,
            candidate=anchor,
            origins=origins,
        )
        occurrence_records.append(
            {
                "occurrence": occurrence,
                "occurrence_record_sha256": canonical_sha256(occurrence),
            }
        )

    any_non_risk = any(not value.hard_risk for value in values)
    gallery = [
        value
        for value in values
        if "gallery" in value.roles and (not any_non_risk or not value.hard_risk)
    ]
    gallery.sort(
        key=lambda value: (
            value.source_ordinal is None,
            value.source_ordinal
            if value.source_ordinal is not None
            else 2**63 - 1,
            value.editorial_rank,
            value.candidate_id,
        )
    )
    decisions: list[dict[str, Any]] = []
    targets = (
        ("gallery_early", 0 if gallery else None),
        ("gallery_middle", (len(gallery) - 1) // 2 if gallery else None),
        ("gallery_late", len(gallery) - 1 if gallery else None),
    )
    selected_ids = {value.candidate_id for value in selected_candidates}
    for slot, target in targets:
        if target is None:
            decisions.append(
                {
                    "actual_gallery_index": None,
                    "chosen_candidate_id": None,
                    "rejected": [],
                    "slot": slot,
                    "state": "unfilled_no_gallery",
                    "target_gallery_index": None,
                }
            )
            continue
        rejected: list[dict[str, Any]] = []
        chosen: CoverageCandidate | None = None
        chosen_index: int | None = None
        for index in sorted(
            range(len(gallery)),
            key=lambda index: (
                abs(index - target),
                index,
                gallery[index].candidate_id,
            ),
        ):
            candidate = gallery[index]
            if candidate.candidate_id in selected_ids:
                continue
            evidence = _redundancy(candidate, selected_candidates)
            if evidence is not None:
                rejected.append(
                    {"candidate_id": candidate.candidate_id, "evidence": evidence}
                )
                continue
            chosen = candidate
            chosen_index = index
            break
        if chosen is None:
            decisions.append(
                {
                    "actual_gallery_index": None,
                    "chosen_candidate_id": None,
                    "rejected": rejected,
                    "slot": slot,
                    "state": "unfilled_no_nonredundant_candidate",
                    "target_gallery_index": target,
                }
            )
            continue
        selected_ids.add(chosen.candidate_id)
        selected_candidates.append(chosen)
        occurrence = _occurrence_record(
            rank=len(occurrence_records) + 1,
            candidate=chosen,
            origins=("coverage_probe",),
            probe_slot=slot,
            target_gallery_index=target,
            actual_gallery_index=chosen_index,
        )
        occurrence_records.append(
            {
                "occurrence": occurrence,
                "occurrence_record_sha256": canonical_sha256(occurrence),
            }
        )
        decisions.append(
            {
                "actual_gallery_index": chosen_index,
                "chosen_candidate_id": chosen.candidate_id,
                "rejected": rejected,
                "slot": slot,
                "state": "filled",
                "target_gallery_index": target,
            }
        )
    if len(occurrence_records) > MAX_OCCURRENCES_PER_BUILDING:
        raise AssertionError("validator replay exceeded the six-occurrence cap")
    return {
        "gallery_pool_count": len(gallery),
        "probe_decisions": decisions,
        "quality_pool_state": (
            "non_hard_risk" if any_non_risk else "all_risk_qa_fallback"
        ),
        "selected_occurrences": occurrence_records,
        "selection_id": values[0].selection_id,
    }


def _json_string_tuple(value: Any, *, label: str) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be JSON text")
    parsed = json.loads(value)
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ValueError(f"{label} must contain a JSON string array")
    return tuple(sorted(set(parsed)))


def _quality_flags_from_provenance(value: Any) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise ValueError("E2 provenance_json must be JSON text")
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("E2 provenance_json must contain an object")
    flags = parsed.get("quality_flags")
    if not isinstance(flags, list) or not all(isinstance(item, str) for item in flags):
        raise ValueError("E2 provenance quality_flags must be a string array")
    return tuple(sorted(set(flags)))


def _single_column_set(
    connection: sqlite3.Connection,
    sql: str,
    params: Sequence[Any],
) -> set[str]:
    return {str(row[0]) for row in connection.execute(sql, tuple(params))}


def _derived_stratum(
    *,
    successful: int,
    covers: int,
    risk_covers: int,
    cross_source: bool,
) -> str:
    if successful == 0:
        return "no_success"
    if risk_covers > 0:
        return "cover_quality_risk"
    if covers == 0:
        return "gallery_fallback"
    if cross_source:
        return "cross_source_candidate"
    return "ordinary"


def _collect_inventory_independently(
    *,
    e2: sqlite3.Connection,
    e3: sqlite3.Connection,
    e2_run_id: str,
    e3_run_id: str,
) -> tuple[tuple[BuildingInventoryItem, ...], dict[str, Any]]:
    """Re-derive the full eligible population without E3 detail summaries."""

    cross_source = {
        (str(row[0]), str(row[1]))
        for row in e2.execute(
            """
            SELECT source,source_building_id FROM (
              SELECT left_source AS source,
                     left_source_building_id AS source_building_id
              FROM cross_source_building_candidates WHERE run_id=?
              UNION
              SELECT right_source,right_source_building_id
              FROM cross_source_building_candidates WHERE run_id=?
            ) ORDER BY source,source_building_id
            """,
            (e2_run_id, e2_run_id),
        )
    }
    e2_building_sha = {
        (str(row[0]), str(row[1])): str(row[2])
        for row in e2.execute(
            """
            SELECT source,source_building_id,source_record_sha256
            FROM source_buildings WHERE run_id=?
            ORDER BY source,source_building_id
            """,
            (e2_run_id,),
        )
    }
    aggregates = {
        str(row[0]): (int(row[1]), int(row[2]), int(row[3]), int(row[4]))
        for row in e3.execute(
            """
            SELECT selection_id,
                   count(*) AS successful_asset_count,
                   sum(EXISTS(
                     SELECT 1 FROM json_each(image_candidates.roles_json)
                     WHERE value='cover'
                   )) AS successful_cover_count,
                   sum(EXISTS(
                     SELECT 1 FROM json_each(image_candidates.roles_json)
                     WHERE value='cover'
                   ) AND (
                     low_information=1
                     OR min(original_width,original_height)<256
                   )) AS quality_risk_cover_count,
                   sum(EXISTS(
                     SELECT 1 FROM json_each(image_candidates.roles_json)
                     WHERE value='gallery'
                   )) AS successful_gallery_count
            FROM image_candidates WHERE run_id=? GROUP BY selection_id
            """,
            (e3_run_id,),
        )
    }
    p1_changed = _single_column_set(
        e3,
        """
        SELECT p0.selection_id
        FROM shortlist_items p0 JOIN shortlist_items p1
          ON p1.run_id=p0.run_id AND p1.selection_id=p0.selection_id
        WHERE p0.run_id=? AND p0.policy_id='p0_editorial_baseline'
          AND p1.policy_id='p1_quality_gated_editorial'
          AND p0.shortlist_rank=1 AND p1.shortlist_rank=1
          AND p0.candidate_id<>p1.candidate_id
        ORDER BY p0.selection_id
        """,
        (e3_run_id,),
    )
    p2_changed = _single_column_set(
        e3,
        """
        WITH p1_only AS (
          SELECT selection_id,candidate_id FROM shortlist_items
          WHERE run_id=? AND policy_id='p1_quality_gated_editorial'
          EXCEPT
          SELECT selection_id,candidate_id FROM shortlist_items
          WHERE run_id=? AND policy_id='p2_quality_exact_direct_phash_shortlist'
        ), p2_only AS (
          SELECT selection_id,candidate_id FROM shortlist_items
          WHERE run_id=? AND policy_id='p2_quality_exact_direct_phash_shortlist'
          EXCEPT
          SELECT selection_id,candidate_id FROM shortlist_items
          WHERE run_id=? AND policy_id='p1_quality_gated_editorial'
        )
        SELECT selection_id FROM p1_only
        UNION
        SELECT selection_id FROM p2_only
        ORDER BY selection_id
        """,
        (e3_run_id, e3_run_id, e3_run_id, e3_run_id),
    )
    qa_fallback = _single_column_set(
        e3,
        """
        SELECT DISTINCT selection_id FROM policy_rankings
        WHERE run_id=? AND policy_id='p1_quality_gated_editorial'
          AND qa_fallback=1 ORDER BY selection_id
        """,
        (e3_run_id,),
    )

    inventory: list[BuildingInventoryItem] = []
    total = 0
    no_success = 0
    candidate_occurrences = 0
    population_counts: Counter[tuple[str, str]] = Counter()
    summary_mismatches: list[str] = []
    for row in e3.execute(
        """
        SELECT selection_id,selection_rank,source,source_building_id,name,
               e2_source_record_sha256,selection_record_sha256,detail_json
        FROM selected_buildings WHERE run_id=? ORDER BY selection_rank
        """,
        (e3_run_id,),
    ):
        total += 1
        selection_id = str(row[0])
        source = str(row[2])
        building_id = str(row[3])
        key = (source, building_id)
        successful, covers, risk_covers, galleries = aggregates.get(
            selection_id, (0, 0, 0, 0)
        )
        is_cross_source = key in cross_source
        stratum = _derived_stratum(
            successful=successful,
            covers=covers,
            risk_covers=risk_covers,
            cross_source=is_cross_source,
        )
        population_counts[(source, stratum)] += 1
        candidate_occurrences += successful
        expected_building_sha = e2_building_sha.get(key)
        if expected_building_sha is None:
            raise ValueError(f"E3 building missing from E2 source_buildings: {key!r}")
        if str(row[5]) != expected_building_sha:
            raise ValueError(f"E3/E2 building source SHA mismatch: {selection_id}")

        detail = json.loads(str(row[7]))
        stored = detail.get("building_summary") if isinstance(detail, dict) else None
        expected_stored = {
            "cross_source_candidate": is_cross_source,
            "name": None if row[4] is None else str(row[4]),
            "quality_risk_cover_count": risk_covers,
            "source": source,
            "source_building_id": building_id,
            "source_record_sha256": expected_building_sha,
            "stratum": stratum,
            "successful_asset_count": successful,
            "successful_cover_count": covers,
        }
        if stored != expected_stored:
            summary_mismatches.append(selection_id)
        if successful == 0:
            no_success += 1
            continue
        inventory.append(
            BuildingInventoryItem(
                selection_id=selection_id,
                source=source,
                source_building_id=building_id,
                name=None if row[4] is None else str(row[4]),
                population_stratum=stratum,
                successful_asset_count=successful,
                successful_gallery_count=galleries,
                source_record_sha256=expected_building_sha,
                selection_record_sha256=str(row[6]),
                qa_fallback=selection_id in qa_fallback,
                p1_rank1_changed=selection_id in p1_changed,
                p2_top3_changed=selection_id in p2_changed,
                cross_source_candidate=is_cross_source,
            )
        )
    if summary_mismatches:
        raise ValueError(
            "E3 building summaries disagree with independent replay: "
            + ", ".join(summary_mismatches[:10])
        )

    values = tuple(inventory)
    eligible_by_source = Counter(item.source for item in values)
    overlay_by_source = {
        source: {
            "p1_rank1_changed": sum(
                item.source == source and item.p1_rank1_changed for item in values
            ),
            "p2_top3_changed": sum(
                item.source == source and item.p2_top3_changed for item in values
            ),
            "qa_fallback": sum(
                item.source == source and item.qa_fallback for item in values
            ),
        }
        for source in ("architizer", "divisare")
    }
    population = {
        "candidate_occurrences": candidate_occurrences,
        "eligible_buildings": len(values),
        "eligible_by_source": dict(sorted(eligible_by_source.items())),
        "inventory_manifest_sha256": canonical_sha256(
            {
                "ordered_inventory": [
                    {
                        "record_sha256": _inventory_sha256(item),
                        "selection_id": item.selection_id,
                    }
                    for item in values
                ],
                "version": SEMANTIC_COVERAGE_VERSION,
            }
        ),
        "no_success_buildings": no_success,
        "overlay_by_source": overlay_by_source,
        "population_by_source_stratum": [
            {"count": count, "source": source, "stratum": stratum}
            for (source, stratum), count in sorted(population_counts.items())
        ],
        "source_total_buildings": total,
    }
    return values, population


def _load_candidates_independently(
    *,
    e2: sqlite3.Connection,
    e3: sqlite3.Connection,
    e2_run_id: str,
    e3_run_id: str,
    building: BuildingInventoryItem,
) -> tuple[CoverageCandidate, ...]:
    """Rejoin a chosen building's complete E3 candidate set to E2 rows."""

    e2_rows = {
        str(row[2]): row
        for row in e2.execute(
            """
            SELECT a.source,a.source_asset_id,ba.source_asset_id,
                   a.fingerprint_status,a.canonical_url,a.fetch_url,a.final_url,
                   a.raw_response_sha256,a.normalized_pixel_sha256,a.phash_hex,
                   a.original_width,a.original_height,
                   a.normalized_width,a.normalized_height,
                   a.source_record_sha256,a.provenance_json,
                   ba.roles_json,ba.relation_record_sha256,
                   (SELECT min(pa.first_ordinal)
                    FROM source_project_buildings spb
                      INDEXED BY idx_source_project_buildings_building
                    CROSS JOIN project_assets pa INDEXED BY idx_project_assets_asset
                    WHERE spb.run_id=ba.run_id AND spb.source=ba.source
                      AND spb.source_building_id=ba.source_building_id
                      AND pa.run_id=spb.run_id AND pa.source=spb.source
                      AND pa.source_project_id=spb.source_project_id
                      AND pa.source_asset_id=ba.source_asset_id
                   ) AS lowest_project_ordinal,
                   em.cluster_id,pm.node_id
            FROM building_assets ba
            JOIN assets a
              ON a.run_id=ba.run_id AND a.source=ba.source
             AND a.source_asset_id=ba.source_asset_id
            LEFT JOIN exact_pixel_cluster_members em
              ON em.run_id=a.run_id AND em.source=a.source
             AND em.source_asset_id=a.source_asset_id
            LEFT JOIN phash_node_members pm
              ON pm.run_id=a.run_id AND pm.source=a.source
             AND pm.source_asset_id=a.source_asset_id
            WHERE ba.run_id=? AND ba.source=? AND ba.source_building_id=?
              AND a.fingerprint_status='success'
            ORDER BY ba.source_asset_id
            """,
            (e2_run_id, building.source, building.source_building_id),
        )
    }
    shortlist_hashes = {
        str(row[0]): str(row[1])
        for row in e3.execute(
            """
            SELECT candidate_id,item_record_sha256 FROM shortlist_items
            WHERE run_id=? AND selection_id=?
              AND policy_id='p2_quality_exact_direct_phash_shortlist'
            ORDER BY shortlist_rank
            """,
            (e3_run_id, building.selection_id),
        )
    }
    values: list[CoverageCandidate] = []
    for row in e3.execute(
        """
        SELECT c.*,r.editorial_rank,r.shortlist_rank,r.qa_fallback,r.hard_risk,
               r.ranking_record_sha256
        FROM image_candidates c JOIN policy_rankings r
          ON r.run_id=c.run_id AND r.selection_id=c.selection_id
         AND r.candidate_id=c.candidate_id
        WHERE c.run_id=? AND c.selection_id=?
          AND r.policy_id='p2_quality_exact_direct_phash_shortlist'
        ORDER BY r.editorial_rank,c.candidate_id
        """,
        (e3_run_id, building.selection_id),
    ):
        asset_id = str(row["source_asset_id"])
        e2row = e2_rows.pop(asset_id, None)
        if e2row is None:
            raise ValueError(f"E3 candidate has no unique E2 relation: {asset_id}")
        if str(e2row[3]) != "success":
            raise ValueError(f"E3 candidate maps to non-success E2 asset: {asset_id}")
        detail = json.loads(str(row["detail_json"]))
        phash_hex = str(e2row[9])
        roles = _json_string_tuple(row["roles_json"], label="E3 roles_json")
        e2_roles = _json_string_tuple(e2row[16], label="E2 roles_json")
        e2_flags = _quality_flags_from_provenance(e2row[15])
        e3_flags = _json_string_tuple(
            row["quality_flags_json"], label="E3 quality_flags_json"
        )
        expected_equal = {
            "source": (str(row["source"]), str(e2row[0])),
            "canonical_url": (row["canonical_url"], e2row[4]),
            "fetch_url": (row["fetch_url"], e2row[5]),
            "final_url": (row["final_url"], e2row[6]),
            "original_width": (row["original_width"], e2row[10]),
            "original_height": (row["original_height"], e2row[11]),
            "normalized_width": (row["normalized_width"], e2row[12]),
            "normalized_height": (row["normalized_height"], e2row[13]),
            "source_record_sha256": (
                str(row["source_record_sha256"]),
                str(e2row[14]),
            ),
            "normalized_pixel_sha256": (
                str(row["normalized_pixel_sha256"]),
                str(e2row[8]),
            ),
            "phash_hex": (str(detail.get("phash_hex")), phash_hex),
            "phash_node_id": (str(row["phash_node_id"]), str(e2row[20])),
            "exact_cluster_id": (row["exact_cluster_id"], e2row[19]),
            "building_relation_record_sha256": (
                str(row["building_relation_record_sha256"]),
                str(e2row[17]),
            ),
            "roles": (roles, e2_roles),
            "quality_flags": (e3_flags, e2_flags),
            "source_ordinal": (row["source_ordinal"], e2row[18]),
            "ordinal_is_derived": (
                int(row["ordinal_is_derived"]),
                int(e2row[18] is not None),
            ),
        }
        mismatches = {
            key: pair for key, pair in expected_equal.items() if pair[0] != pair[1]
        }
        if mismatches:
            raise ValueError(
                f"E3/E2 candidate field mismatch for {asset_id}: {mismatches!r}"
            )
        raw_sha = e2row[7]
        if raw_sha is None:
            raise ValueError(f"successful E2 asset lacks raw response SHA: {asset_id}")
        values.append(
            CoverageCandidate(
                candidate_id=str(row["candidate_id"]),
                selection_id=building.selection_id,
                source=building.source,
                source_building_id=building.source_building_id,
                source_asset_id=asset_id,
                editorial_rank=int(row["editorial_rank"]),
                p2_shortlist_rank=(
                    None
                    if row["shortlist_rank"] is None
                    else int(row["shortlist_rank"])
                ),
                qa_fallback=bool(row["qa_fallback"]),
                hard_risk=bool(row["hard_risk"]),
                roles=roles,
                source_ordinal=(
                    None if row["source_ordinal"] is None else int(row["source_ordinal"])
                ),
                canonical_url=str(row["canonical_url"]),
                fetch_url=str(e2row[5]),
                final_url=None if e2row[6] is None else str(e2row[6]),
                original_width=None if e2row[10] is None else int(e2row[10]),
                original_height=None if e2row[11] is None else int(e2row[11]),
                normalized_width=None if e2row[12] is None else int(e2row[12]),
                normalized_height=None if e2row[13] is None else int(e2row[13]),
                quality_flags=e2_flags,
                normalized_pixel_sha256=str(e2row[8]),
                phash_node_id=str(e2row[20]),
                phash_hex=phash_hex,
                raw_response_sha256=str(raw_sha),
                e3_source_record_sha256=str(row["source_record_sha256"]),
                e3_candidate_record_sha256=str(row["candidate_record_sha256"]),
                e3_ranking_record_sha256=str(row["ranking_record_sha256"]),
                e3_shortlist_item_record_sha256=shortlist_hashes.get(
                    str(row["candidate_id"])
                ),
                e2_asset_record_sha256=str(e2row[14]),
                e2_building_relation_record_sha256=str(e2row[17]),
            )
        )
    if e2_rows:
        raise ValueError(
            f"E2 building has candidates absent from E3: {sorted(e2_rows)[:10]!r}"
        )
    if len(values) != building.successful_asset_count:
        raise ValueError(
            f"candidate count mismatch for {building.selection_id}: "
            f"{len(values)} != {building.successful_asset_count}"
        )
    return tuple(values)


def _snapshot_record(value: ArtifactSnapshot) -> dict[str, Any]:
    return {
        "application_id": value.application_id,
        "builder_version": value.builder_version,
        "byte_sha256": value.byte_sha256,
        "contract_version": value.contract_version,
        "logical_sha256": value.logical_sha256,
        "name": value.name,
        "path": str(value.path),
        "run_id": value.run_id,
        "size_bytes": value.size_bytes,
        "user_version": value.user_version,
    }


def validate_semantic_coverage_manifest(
    manifest_path: Path | str,
    *,
    e2_spec: ArtifactSpec,
    e3_spec: ArtifactSpec,
    expected_sample_size: int,
    expected_sample_seed: str,
    expected_max_images_per_building: int,
    batch_size: int = 1_000,
) -> SemanticCoverageValidationResult:
    """Replay and validate one canonical, no-request semantic N10 manifest."""

    if expected_sample_size != 10:
        raise ValueError("semantic coverage preflight currently requires fixed N10")
    if expected_max_images_per_building != 6:
        raise ValueError("semantic coverage v1 requires a six-image cap")
    if not expected_sample_seed or expected_sample_seed != expected_sample_seed.strip():
        raise ValueError("expected sample seed must be non-empty without whitespace")
    if batch_size < 1:
        raise ValueError("batch_size must be positive")

    path = Path(manifest_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    manifest_size_before = path.stat().st_size
    manifest_sha_before = _sha256_file(path)
    raw = path.read_bytes()
    checks: list[SemanticCoverageValidationCheck] = []

    try:
        decoded = raw.decode("utf-8")
        payload = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _check(checks, "valid_utf8_json", False, "canonical UTF-8 JSON", str(exc))
        return _empty_result(
            path=path,
            checks=checks,
            size=manifest_size_before,
            byte_sha256=manifest_sha_before,
        )
    if not isinstance(payload, dict):
        _check(checks, "json_object", False, "object", type(payload).__name__)
        return _empty_result(
            path=path,
            checks=checks,
            size=manifest_size_before,
            byte_sha256=manifest_sha_before,
        )

    _check(checks, "valid_utf8_json", True, "valid", "valid")
    try:
        canonical_bytes = (canonical_json(payload) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        canonical_bytes = b""
        _check(
            checks,
            "canonical_json_bytes",
            False,
            "canonical UTF-8 JSON followed by one LF",
            f"{type(exc).__name__}: {exc}",
        )
    else:
        _check(
            checks,
            "canonical_json_bytes",
            raw == canonical_bytes,
            hashlib.sha256(canonical_bytes).hexdigest(),
            manifest_sha_before,
        )

    actual_self_sha = payload.get("semantic_coverage_manifest_sha256")
    payload_without_self_sha = dict(payload)
    payload_without_self_sha.pop("semantic_coverage_manifest_sha256", None)
    replayed_self_sha = _safe_sha256(
        {
            "domain": SEMANTIC_COVERAGE_MANIFEST_DOMAIN,
            "manifest": payload_without_self_sha,
        }
    )
    _check(
        checks,
        "manifest_self_sha256",
        actual_self_sha == replayed_self_sha,
        replayed_self_sha,
        actual_self_sha,
    )

    e2_path = Path(e2_spec.path).resolve()
    e3_path = Path(e3_spec.path).resolve()
    input_before = {
        "e2": {
            "path": str(e2_path),
            "size_bytes": e2_path.stat().st_size,
            "byte_sha256": _sha256_file(e2_path),
            "sidecars": [str(value) for value in _sidecars(e2_path)],
        },
        "e3": {
            "path": str(e3_path),
            "size_bytes": e3_path.stat().st_size,
            "byte_sha256": _sha256_file(e3_path),
            "sidecars": [str(value) for value in _sidecars(e3_path)],
        },
    }
    _check(
        checks,
        "input_sidecars_absent_at_open",
        not input_before["e2"]["sidecars"] and not input_before["e3"]["sidecars"],
        {"e2": [], "e3": []},
        {
            "e2": input_before["e2"]["sidecars"],
            "e3": input_before["e3"]["sidecars"],
        },
    )
    _check(
        checks,
        "caller_pinned_input_bytes",
        input_before["e2"]["size_bytes"] == e2_spec.expected_size
        and input_before["e2"]["byte_sha256"] == e2_spec.expected_sha256
        and input_before["e3"]["size_bytes"] == e3_spec.expected_size
        and input_before["e3"]["byte_sha256"] == e3_spec.expected_sha256,
        {
            "e2": {
                "size_bytes": e2_spec.expected_size,
                "byte_sha256": e2_spec.expected_sha256,
            },
            "e3": {
                "size_bytes": e3_spec.expected_size,
                "byte_sha256": e3_spec.expected_sha256,
            },
        },
        {
            "e2": {
                "size_bytes": input_before["e2"]["size_bytes"],
                "byte_sha256": input_before["e2"]["byte_sha256"],
            },
            "e3": {
                "size_bytes": input_before["e3"]["size_bytes"],
                "byte_sha256": input_before["e3"]["byte_sha256"],
            },
        },
    )

    # The artifact inspector is shared only for immutable SQLite opening and
    # frozen run/application/logical-lineage checks.  Population sampling,
    # candidate joins, and selection below are deliberately reimplemented in
    # this module instead of invoking the planner.
    e2_snapshot, e2 = inspect_artifact(e2_spec)
    e3_snapshot: ArtifactSnapshot | None = None
    e3: sqlite3.Connection | None = None
    try:
        e3_snapshot, e3 = inspect_artifact(e3_spec)
        bound_rows = e3.execute(
            """
            SELECT size_bytes,sha256_before,sha256_after,logical_sha256
            FROM selection_inputs
            WHERE run_id=? AND input_role='e2_evidence'
            """,
            (e3_snapshot.run_id,),
        ).fetchall()
        if len(bound_rows) != 1:
            raise ValueError("E3 must bind exactly one E2 evidence input")
        bound = bound_rows[0]
        expected_bound = (
            e2_snapshot.size_bytes,
            e2_snapshot.byte_sha256,
            e2_snapshot.byte_sha256,
            e2_snapshot.logical_sha256,
        )
        if tuple(bound) != expected_bound:
            raise ValueError("E3 stored E2 lineage does not match supplied E2")

        inventory, population = _collect_inventory_independently(
            e2=e2,
            e3=e3,
            e2_run_id=e2_snapshot.run_id,
            e3_run_id=e3_snapshot.run_id,
        )
        guarded_records, selected_items = _replay_guarded_selection(
            inventory, seed=expected_sample_seed
        )
        selected_records: list[dict[str, Any]] = []
        occurrence_hashes: list[dict[str, Any]] = []
        pixel_occurrences: Counter[str] = Counter()
        for guard_record, building in zip(guarded_records, selected_items):
            candidates = _load_candidates_independently(
                e2=e2,
                e3=e3,
                e2_run_id=e2_snapshot.run_id,
                e3_run_id=e3_snapshot.run_id,
                building=building,
            )
            coverage = _replay_building_coverage(candidates)
            coverage_sha = canonical_sha256(coverage)
            combined = {
                **guard_record,
                "coverage_plan": coverage,
                "coverage_plan_record_sha256": coverage_sha,
            }
            selected_records.append(
                {
                    "selected_building": combined,
                    "selected_building_record_sha256": canonical_sha256(combined),
                }
            )
            for wrapped in coverage["selected_occurrences"]:
                occurrence = wrapped["occurrence"]
                candidate = occurrence["candidate"]
                pixel = str(candidate["normalized_pixel_sha256"])
                pixel_occurrences[pixel] += 1
                occurrence_hashes.append(
                    {
                        "candidate_id": candidate["candidate_id"],
                        "occurrence_record_sha256": wrapped[
                            "occurrence_record_sha256"
                        ],
                        "selection_id": building.selection_id,
                    }
                )

        config = {
            "anchor_policy_id": "p2_quality_exact_direct_phash_shortlist",
            "anchor_ranks": [1, 2, 3],
            "creates_final_representative": False,
            "creates_vision_tasks": False,
            "gallery_probe_slots": [
                "gallery_early",
                "gallery_middle",
                "gallery_late",
            ],
            "hard_risk_fallback_only_when_all_candidates_risky": True,
            "max_occurrences_per_building": expected_max_images_per_building,
            "phash_semantic_reuse_allowed": False,
            "phash_transitive_closure_allowed": False,
            "probe_redundancy_order": [
                "exact_normalized_pixel",
                "identical_phash",
                "direct_phash_le8",
            ],
            "sample_guard_order": [
                record["guard_name"] for record in guarded_records
            ],
            "selection_version": SEMANTIC_COVERAGE_VERSION,
        }
        expected_payload: dict[str, Any] = {
            "authoritative": False,
            "config": config,
            "config_sha256": canonical_sha256(config),
            "creates_final_representative": False,
            "creates_vision_tasks": False,
            "e2_input": _snapshot_record(e2_snapshot),
            "e3_input": _snapshot_record(e3_snapshot),
            "exact_reuse_is_provisional_until_vision_input_identity": True,
            "llm_requests": 0,
            "network_requests": 0,
            "ordered_building_manifest_sha256": canonical_sha256(
                {
                    "ordered_buildings": [
                        {
                            "rank": rank,
                            "record_sha256": value[
                                "selected_building_record_sha256"
                            ],
                        }
                        for rank, value in enumerate(selected_records, 1)
                    ],
                    "version": SEMANTIC_COVERAGE_VERSION,
                }
            ),
            "ordered_occurrence_manifest_sha256": canonical_sha256(
                {
                    "ordered_occurrences": occurrence_hashes,
                    "version": SEMANTIC_COVERAGE_VERSION,
                }
            ),
            "planned_e1_exact_duplicate_savings": (
                sum(pixel_occurrences.values()) - len(pixel_occurrences)
            ),
            "planned_occurrence_count": sum(pixel_occurrences.values()),
            "planned_unique_e1_pixel_count": len(pixel_occurrences),
            "population": population,
            "sample_seed": expected_sample_seed,
            "sample_size_buildings": len(guarded_records),
            "selected_buildings": selected_records,
            "selection_mode": "offline_semantic_coverage_n10_plan",
            "version": SEMANTIC_COVERAGE_VERSION,
            "vision_requests": 0,
        }
    finally:
        if e3 is not None:
            e3.close()
        e2.close()

    input_after = {
        "e2": {
            "path": str(e2_path),
            "size_bytes": e2_path.stat().st_size,
            "byte_sha256": _sha256_file(e2_path),
            "sidecars": [str(value) for value in _sidecars(e2_path)],
        },
        "e3": {
            "path": str(e3_path),
            "size_bytes": e3_path.stat().st_size,
            "byte_sha256": _sha256_file(e3_path),
            "sidecars": [str(value) for value in _sidecars(e3_path)],
        },
    }
    expected_payload["input_sha_after"] = {
        "e2_evidence": input_after["e2"]["byte_sha256"],
        "e3_selection": input_after["e3"]["byte_sha256"],
    }
    expected_payload["semantic_coverage_manifest_sha256"] = canonical_sha256(
        {
            "domain": SEMANTIC_COVERAGE_MANIFEST_DOMAIN,
            "manifest": expected_payload,
        }
    )

    _check(
        checks,
        "acceptance_parameters",
        payload.get("sample_size_buildings") == expected_sample_size
        and payload.get("sample_seed") == expected_sample_seed
        and isinstance(payload.get("config"), dict)
        and payload["config"].get("max_occurrences_per_building")
        == expected_max_images_per_building,
        {
            "sample_size_buildings": expected_sample_size,
            "sample_seed": expected_sample_seed,
            "max_occurrences_per_building": expected_max_images_per_building,
        },
        {
            "sample_size_buildings": payload.get("sample_size_buildings"),
            "sample_seed": payload.get("sample_seed"),
            "max_occurrences_per_building": (
                payload.get("config", {}).get("max_occurrences_per_building")
                if isinstance(payload.get("config"), dict)
                else None
            ),
        },
    )
    _check(
        checks,
        "input_lineage_replay",
        payload.get("e2_input") == expected_payload.get("e2_input")
        and payload.get("e3_input") == expected_payload.get("e3_input")
        and payload.get("input_sha_after")
        == expected_payload.get("input_sha_after"),
        _safe_sha256(
            {
                "e2_input": expected_payload.get("e2_input"),
                "e3_input": expected_payload.get("e3_input"),
                "input_sha_after": expected_payload.get("input_sha_after"),
            }
        ),
        _safe_sha256(
            {
                "e2_input": payload.get("e2_input"),
                "e3_input": payload.get("e3_input"),
                "input_sha_after": payload.get("input_sha_after"),
            }
        ),
    )
    _check(
        checks,
        "population_and_inventory_replay",
        payload.get("population") == expected_payload.get("population"),
        _safe_sha256(expected_payload.get("population")),
        _safe_sha256(payload.get("population")),
    )
    _check(
        checks,
        "guarded_n10_replay",
        [
            value.get("selected_building", {}).get("guard_name")
            for value in payload.get("selected_buildings", [])
            if isinstance(value, dict)
        ]
        == [record["guard_name"] for record in guarded_records],
        [record["guard_name"] for record in guarded_records],
        [
            value.get("selected_building", {}).get("guard_name")
            for value in payload.get("selected_buildings", [])
            if isinstance(value, dict)
        ],
    )
    _check(
        checks,
        "per_building_selection_and_e2_join_replay",
        payload.get("selected_buildings")
        == expected_payload.get("selected_buildings"),
        _safe_sha256(expected_payload.get("selected_buildings")),
        _safe_sha256(payload.get("selected_buildings")),
    )
    _check(
        checks,
        "ordered_building_manifest_replay",
        payload.get("ordered_building_manifest_sha256")
        == expected_payload.get("ordered_building_manifest_sha256"),
        expected_payload.get("ordered_building_manifest_sha256"),
        payload.get("ordered_building_manifest_sha256"),
    )
    _check(
        checks,
        "ordered_occurrence_manifest_replay",
        payload.get("ordered_occurrence_manifest_sha256")
        == expected_payload.get("ordered_occurrence_manifest_sha256"),
        expected_payload.get("ordered_occurrence_manifest_sha256"),
        payload.get("ordered_occurrence_manifest_sha256"),
    )
    _check(
        checks,
        "exact_reuse_accounting_replay",
        all(
            payload.get(key) == expected_payload.get(key)
            for key in (
                "exact_reuse_is_provisional_until_vision_input_identity",
                "planned_e1_exact_duplicate_savings",
                "planned_occurrence_count",
                "planned_unique_e1_pixel_count",
            )
        ),
        {
            key: expected_payload.get(key)
            for key in (
                "exact_reuse_is_provisional_until_vision_input_identity",
                "planned_e1_exact_duplicate_savings",
                "planned_occurrence_count",
                "planned_unique_e1_pixel_count",
            )
        },
        {
            key: payload.get(key)
            for key in (
                "exact_reuse_is_provisional_until_vision_input_identity",
                "planned_e1_exact_duplicate_savings",
                "planned_occurrence_count",
                "planned_unique_e1_pixel_count",
            )
        },
    )
    zero_requests = {
        "network_requests": payload.get("network_requests"),
        "vision_requests": payload.get("vision_requests"),
        "llm_requests": payload.get("llm_requests"),
    }
    _check(
        checks,
        "zero_request_flags",
        all(value == 0 for value in zero_requests.values()),
        {name: 0 for name in zero_requests},
        zero_requests,
    )
    safety = {
        "authoritative": payload.get("authoritative"),
        "creates_final_representative": payload.get(
            "creates_final_representative"
        ),
        "creates_vision_tasks": payload.get("creates_vision_tasks"),
        "selection_mode": payload.get("selection_mode"),
    }
    expected_safety = {
        "authoritative": False,
        "creates_final_representative": False,
        "creates_vision_tasks": False,
        "selection_mode": "offline_semantic_coverage_n10_plan",
    }
    _check(
        checks,
        "candidate_only_safety_flags",
        safety == expected_safety,
        expected_safety,
        safety,
    )
    _check(
        checks,
        "entire_manifest_replay",
        payload == expected_payload,
        _safe_sha256(expected_payload),
        _safe_sha256(payload),
    )

    _check(
        checks,
        "inputs_read_only_unchanged",
        input_before["e2"] == input_after["e2"]
        and input_before["e3"] == input_after["e3"],
        input_before,
        input_after,
    )
    manifest_size_after = path.stat().st_size
    manifest_sha_after = _sha256_file(path)
    _check(
        checks,
        "manifest_read_only_unchanged",
        (manifest_size_before, manifest_sha_before)
        == (manifest_size_after, manifest_sha_after),
        {"size_bytes": manifest_size_before, "sha256": manifest_sha_before},
        {"size_bytes": manifest_size_after, "sha256": manifest_sha_after},
    )

    selected = payload.get("selected_buildings")
    population = payload.get("population")
    population_count = 0
    if isinstance(population, dict):
        for key in ("eligible_buildings", "population_count", "total_buildings"):
            value = population.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                population_count = value
                break
    selected_count = len(selected) if isinstance(selected, list) else 0
    return SemanticCoverageValidationResult(
        manifest_path=path,
        passed=all(value.passed for value in checks),
        checks=tuple(checks),
        manifest_size_bytes=manifest_size_after,
        manifest_byte_sha256=manifest_sha_after,
        manifest_sha256=(
            str(actual_self_sha) if isinstance(actual_self_sha, str) else None
        ),
        population_count=population_count,
        selected_building_count=selected_count,
        selected_image_count=_count_selected_images(selected),
        ordered_building_manifest_sha256=(
            str(payload.get("ordered_building_manifest_sha256"))
            if payload.get("ordered_building_manifest_sha256") is not None
            else None
        ),
        ordered_occurrence_manifest_sha256=(
            str(payload.get("ordered_occurrence_manifest_sha256"))
            if payload.get("ordered_occurrence_manifest_sha256") is not None
            else None
        ),
        e2_byte_sha256=str(input_after["e2"]["byte_sha256"]),
        e3_byte_sha256=str(input_after["e3"]["byte_sha256"]),
    )


__all__ = [
    "SemanticCoverageValidationCheck",
    "SemanticCoverageValidationResult",
    "validate_semantic_coverage_manifest",
]
