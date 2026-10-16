"""Offline, result-blind planner for semantic Vision N10 v1.2.

The fixed sample is selected from immutable E2/E3 evidence only.  Eight rows
represent the production population and two bounded rows exercise existing E3
risk strata.  All identities from the completed v1 N10, v1 N100, and v1.1 N10
runs are exclusions; semantic outputs and error labels are never queried.
"""

from __future__ import annotations

from collections import Counter
import hashlib
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping

from canonical.cross_source_image_selection import canonical_json, canonical_sha256
from canonical.cross_source_semantic_coverage import select_building_coverage
from canonical.cross_source_semantic_coverage_sources import (
    ArtifactSnapshot,
    ArtifactSpec,
    collect_inventory,
    inspect_artifact,
    load_building_candidates,
    sha256_file,
    sqlite_sidecars,
)
from canonical.cross_source_semantic_coverage_n10_v11 import (
    PriorIdentityLedger,
    PriorRunSpec,
    _snapshot_still_matches,
    default_prior_specs as default_v1_prior_specs,
    load_prior_identity_ledger,
)


VERSION = "archibe-cross-source-semantic-coverage-n10-v1.2"
MANIFEST_DOMAIN = "archibe-cross-source-semantic-coverage-n10-v1.2-manifest-v1"
DEFAULT_SAMPLE_SEED = "archibe-semantic-coverage-n10-v1.2"
SCORE_DOMAIN = "semantic-coverage-n10-v1.2-two-lane-score-v1"
QUOTA_VERSION = "population-core8-plus-risk2-v1"
EXPECTED_BUILDINGS = 10
EXPECTED_OCCURRENCES = 50

DEFAULT_N10_V11_MANIFEST = Path(
    "data/reports/cross_source_semantic_coverage_n10_v1_1.json"
)
DEFAULT_N10_V11_DB = Path(
    "data/enrichment/divisare_architizer_semantic_vision_n10_v1_1.db"
)

QUOTAS: tuple[tuple[str, str, str, int], ...] = (
    ("population_core", "architizer", "ordinary", 5),
    ("population_core", "architizer", "cross_source_candidate", 1),
    ("population_core", "divisare", "ordinary", 2),
    ("risk_sentinel", "architizer", "cover_quality_risk", 1),
    ("risk_sentinel", "divisare", "gallery_fallback", 1),
)


def default_prior_specs(repo_root: Path | str) -> tuple[PriorRunSpec, ...]:
    """Return the three immutable, terminal semantic runs excluded by v1.2."""

    root = Path(repo_root).resolve()
    return default_v1_prior_specs(root) + (
        PriorRunSpec(
            "semantic_n10_v1_1",
            root / DEFAULT_N10_V11_MANIFEST,
            root / DEFAULT_N10_V11_DB,
            922_575,
            "3315a48188aec4c9461867235999ac7e526b8b011a869cafdd3be9d3cd1cc5b6",
            "91957a822915ae70f56fd1a5f82244098a4508274cc5dbbdd91e1d6bf03ad8c9",
            "archibe-cross-source-semantic-coverage-n10-v1.1-manifest-v1",
            "archibe-cross-source-semantic-coverage-n10-v1.1",
            "archibe-semantic-coverage-n10-v1.1",
            10,
            53,
            888_832,
            "03d99eb3b624a9211154643a3c29fca337f763442fb086b76d25bca2ee106172",
            "912a51380ce57947bd304057849e9d1dbdcc4fc36c3491e2e6c6614bc691ef3e",
            "semn10v11-b1c3c595b0810a2c91e02a61",
            0x53564E31,
            3,
            "complete",
            True,
        ),
    )


def _candidate_collision_records(
    e3: sqlite3.Connection, *, run_id: str, prior: PriorIdentityLedger
) -> tuple[dict[str, Any], ...]:
    """Find prior-identity collisions with one bounded-memory E3 table pass."""

    pixels = prior.expected_e1_pixels | prior.successful_derivative_pixels
    urls = prior.urls
    counts: dict[str, dict[str, Any]] = {}
    rows = e3.execute(
        "SELECT selection_id,source,normalized_pixel_sha256,canonical_url,"
        "fetch_url,final_url FROM image_candidates WHERE run_id=?",
        (run_id,),
    )
    for row in rows:
        pixel_match = str(row[2]) in pixels
        url_match = any(value is not None and str(value) in urls for value in row[3:6])
        if not pixel_match and not url_match:
            continue
        value = counts.setdefault(
            str(row[0]),
            {"pixel_match_count": 0, "url_match_count": 0, "source": str(row[1])},
        )
        value["pixel_match_count"] += int(pixel_match)
        value["url_match_count"] += int(url_match)
    records = []
    for selection_id in sorted(counts):
        value = counts[selection_id]
        record = {
            "pixel_match_count": value["pixel_match_count"],
            "reason": "prior_semantic_expected_derivative_or_url_identity",
            "selection_id": selection_id,
            "source": value["source"],
            "url_match_count": value["url_match_count"],
        }
        record["record_sha256"] = canonical_sha256(record)
        records.append(record)
    return tuple(records)


def deterministic_score(
    seed: str, lane: str, source: str, stratum: str, selection_id: str
) -> str:
    return canonical_sha256(
        {
            "domain": SCORE_DOMAIN,
            "lane": lane,
            "population_stratum": stratum,
            "seed": seed,
            "selection_id": selection_id,
            "source": source,
        }
    )


def select_fixed_sample(
    inventory: Iterable[Any],
    *,
    excluded_selection_ids: Iterable[str],
    seed: str = DEFAULT_SAMPLE_SEED,
) -> tuple[tuple[str, str, str, int, int, str, Any], ...]:
    """Select the fixed two-lane sample without semantic result conditioning."""

    excluded = frozenset(excluded_selection_ids)
    frame = tuple(
        sorted(
            (value for value in inventory if value.selection_id not in excluded),
            key=lambda value: value.selection_id,
        )
    )
    selected_ids: set[str] = set()
    result: list[tuple[str, str, str, int, int, str, Any]] = []
    for lane, source, stratum, quota in QUOTAS:
        scored = sorted(
            (
                deterministic_score(seed, lane, source, stratum, value.selection_id),
                value,
            )
            for value in frame
            if value.source == source
            and value.population_stratum == stratum
            and value.selection_id not in selected_ids
        )
        if len(scored) < quota:
            raise ValueError(f"insufficient candidates for {lane}:{source}:{stratum}")
        for rank_in_quota, (score, building) in enumerate(scored[:quota], 1):
            selected_ids.add(building.selection_id)
            result.append(
                (lane, source, stratum, quota, rank_in_quota, score, building)
            )
    if len(result) != EXPECTED_BUILDINGS:
        raise AssertionError("fixed N10 v1.2 building count mismatch")
    return tuple(result)


def build_manifest(
    e2_spec: ArtifactSpec,
    e3_spec: ArtifactSpec,
    prior_specs: Iterable[PriorRunSpec],
    *,
    seed: str = DEFAULT_SAMPLE_SEED,
) -> dict[str, Any]:
    prior_specs = tuple(prior_specs)
    prior = load_prior_identity_ledger(prior_specs)
    e2_snapshot, e2 = inspect_artifact(e2_spec)
    e3: sqlite3.Connection | None = None
    e3_snapshot: ArtifactSnapshot | None = None
    try:
        e3_snapshot, e3 = inspect_artifact(e3_spec)
        inventory, population = collect_inventory(e3, run_id=e3_spec.expected_run_id)
        collisions = _candidate_collision_records(
            e3, run_id=e3_spec.expected_run_id, prior=prior
        )
        excluded_ids = prior.building_ids | frozenset(
            value["selection_id"] for value in collisions
        )
        frame = tuple(value for value in inventory if value.selection_id not in excluded_ids)
        available = Counter((value.source, value.population_stratum) for value in frame)
        selected_records: list[dict[str, Any]] = []
        selected_pixels: list[str] = []
        selected_urls: list[str] = []
        selected_ids: set[str] = set()
        occurrence_hashes: list[dict[str, Any]] = []
        candidate_hashes: list[dict[str, Any]] = []
        for lane, source, stratum, quota, rank_in_quota, score, building in select_fixed_sample(
            inventory, excluded_selection_ids=excluded_ids, seed=seed
        ):
            selected_ids.add(building.selection_id)
            coverage = select_building_coverage(
                load_building_candidates(
                    e3,
                    e2,
                    e3_run_id=e3_spec.expected_run_id,
                    e2_run_id=e2_spec.expected_run_id,
                    building=building,
                )
            )
            for wrapped in coverage.as_record()["selected_occurrences"]:
                occurrence = wrapped["occurrence"]
                candidate = occurrence["candidate"]
                selected_pixels.append(str(candidate["normalized_pixel_sha256"]))
                selected_urls.append(str(candidate["fetch_url"]))
                occurrence_hashes.append(
                    {
                        "rank": len(occurrence_hashes) + 1,
                        "occurrence_record_sha256": wrapped["occurrence_record_sha256"],
                    }
                )
                candidate_hashes.append(
                    {
                        "rank": len(candidate_hashes) + 1,
                        "candidate_id": candidate["candidate_id"],
                    }
                )
            selected = {
                "available_count": available[(source, stratum)],
                "building": building.as_record(),
                "building_record_sha256": building.record_sha256,
                "coverage_plan": coverage.as_record(),
                "coverage_record_sha256": coverage.record_sha256,
                "lane": lane,
                "model_result_selection_used": False,
                "quota": quota,
                "rank": len(selected_records) + 1,
                "rank_in_quota": rank_in_quota,
                "score_sha256": score,
                "source": source,
                "stratum": stratum,
            }
            selected_records.append(
                {
                    "selected_building": selected,
                    "selected_building_record_sha256": canonical_sha256(selected),
                }
            )
        if len(selected_records) != EXPECTED_BUILDINGS or len(selected_pixels) != EXPECTED_OCCURRENCES:
            raise ValueError("production N10 v1.2 count mismatch")
        selected_url_set = set(selected_urls)
        overlaps = {
            "building": len(selected_ids & prior.building_ids),
            "expected_e1_pixel": len(set(selected_pixels) & prior.expected_e1_pixels),
            "prior_successful_derivative_pixel": len(
                set(selected_pixels) & prior.successful_derivative_pixels
            ),
            "fetch_url": len(selected_url_set & prior.urls),
        }
        if (
            any(overlaps.values())
            or len(set(selected_pixels)) != len(selected_pixels)
            or len(selected_url_set) != len(selected_urls)
        ):
            raise ValueError(f"selected N10 v1.2 identity overlap: {overlaps!r}")
        exclusion_records = {
            "building_records": list(prior.building_records),
            "candidate_collision_records": list(collisions),
            "occurrence_records": list(prior.occurrence_records),
        }
        exclusions = {
            "actual_derivative_runtime_rule": (
                "materialize all fixed inputs before the first model request and reject any actual "
                "derivative pixel matching a prior successful derivative; seven prior source_changed "
                "rows have expected-E1 identity only and no accepted derivative"
            ),
            "candidate_collision_buildings": len(collisions),
            "excluded_buildings": len(excluded_ids),
            "expected_e1_pixel_count": len(prior.expected_e1_pixels),
            "failed_expected_only_occurrences": sum(
                value["derivative_pixel_sha256"] is None
                for value in prior.occurrence_records
            ),
            "identity_ledger_sha256": canonical_sha256(
                {"domain": VERSION + "-prior-identity-ledger", **exclusion_records}
            ),
            "prior_building_count": len(prior.building_ids),
            "prior_run_inputs": list(prior.run_inputs),
            "prior_successful_derivative_pixel_count": len(
                prior.successful_derivative_pixels
            ),
            "prior_unique_url_count": len(prior.urls),
            "records": exclusion_records,
            "selected_plan_overlap": overlaps,
        }
        quota_plan = {
            "quota_version": QUOTA_VERSION,
            "quotas": [
                {"count": count, "lane": lane, "source": source, "stratum": stratum}
                for lane, source, stratum, count in QUOTAS
            ],
            "risk_sentinel_basis": (
                "bounded pre-existing E3 population strata only; no prior semantic labels, "
                "known inference IDs, or result errors participate in selection"
            ),
        }
        config = {
            "creates_final_representative": False,
            "creates_vision_tasks": False,
            "max_occurrences_per_building": 6,
            "model_result_selection_used": False,
            "prior_exclusion_fields": [
                "selection_id",
                "expected_e1_pixel_sha256",
                "successful_derivative_pixel_sha256",
                "canonical_url",
                "fetch_url",
                "planned_final_url",
                "actual_final_url",
            ],
            "quota_version": QUOTA_VERSION,
            "sample_size": EXPECTED_BUILDINGS,
            "score_domain": SCORE_DOMAIN,
            "selection_version": VERSION,
        }
        frame_counts = Counter((value.source, value.population_stratum) for value in frame)
        body: dict[str, Any] = {
            "authoritative": False,
            "config": config,
            "config_sha256": canonical_sha256(config),
            "creates_final_representative": False,
            "creates_vision_tasks": False,
            "e2_input": e2_snapshot.as_record(),
            "e3_input": e3_snapshot.as_record(),
            "exact_reuse_is_provisional_until_vision_input_identity": True,
            "exclusions": exclusions,
            "llm_requests": 0,
            "network_requests": 0,
            "ordered_building_manifest_sha256": canonical_sha256(
                {
                    "ordered_buildings": [
                        {"rank": index, "record_sha256": value["selected_building_record_sha256"]}
                        for index, value in enumerate(selected_records, 1)
                    ],
                    "version": VERSION,
                }
            ),
            "ordered_candidate_id_manifest_sha256": canonical_sha256(
                {"ordered_candidate_ids": candidate_hashes, "version": VERSION}
            ),
            "ordered_e1_pixel_manifest_sha256": canonical_sha256(
                {
                    "ordered_e1_pixels": [
                        {"rank": index, "sha256": value}
                        for index, value in enumerate(selected_pixels, 1)
                    ],
                    "version": VERSION,
                }
            ),
            "ordered_fetch_url_manifest_sha256": canonical_sha256(
                {
                    "ordered_fetch_urls": [
                        {"rank": index, "url": value}
                        for index, value in enumerate(selected_urls, 1)
                    ],
                    "version": VERSION,
                }
            ),
            "ordered_occurrence_manifest_sha256": canonical_sha256(
                {"ordered_occurrences": occurrence_hashes, "version": VERSION}
            ),
            "planned_e1_exact_duplicate_savings": len(selected_pixels) - len(set(selected_pixels)),
            "planned_occurrence_count": len(selected_pixels),
            "planned_unique_e1_pixel_count": len(set(selected_pixels)),
            "population": population,
            "quota_plan": quota_plan,
            "sample_seed": seed,
            "sample_size_buildings": len(selected_records),
            "sampling_frame": {
                "available_buildings": len(frame),
                "available_by_source_stratum": [
                    {"count": count, "source": source, "stratum": stratum}
                    for (source, stratum), count in sorted(frame_counts.items())
                ],
                "candidate_collision_buildings": len(collisions),
                "excluded_buildings": len(excluded_ids),
            },
            "selected_buildings": selected_records,
            "selection_mode": "offline_disjoint_population_core8_plus_e3_risk2_n10_v1.2",
            "version": VERSION,
            "vision_requests": 0,
        }
    finally:
        if e3 is not None:
            e3.close()
        e2.close()
    if e3_snapshot is None:
        raise AssertionError("E3 snapshot missing")
    _snapshot_still_matches(e2_spec, e2_snapshot)
    _snapshot_still_matches(e3_spec, e3_snapshot)
    for spec in prior_specs:
        if (
            sha256_file(spec.manifest_path) != spec.manifest_byte_sha256
            or sha256_file(spec.sidecar_path) != spec.sidecar_byte_sha256
        ):
            raise RuntimeError(f"prior run input changed: {spec.name}")
    body["input_sha_after"] = {
        "e2_evidence": e2_snapshot.byte_sha256,
        "e3_selection": e3_snapshot.byte_sha256,
        **{
            spec.name + "_manifest": spec.manifest_byte_sha256
            for spec in prior_specs
        },
        **{
            spec.name + "_sidecar": spec.sidecar_byte_sha256
            for spec in prior_specs
        },
    }
    body["semantic_coverage_manifest_sha256"] = canonical_sha256(
        {"domain": MANIFEST_DOMAIN, "manifest": body}
    )
    return body


def write_manifest(path: Path | str, manifest: Mapping[str, Any]) -> Path:
    output = Path(path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    raw = (canonical_json(dict(manifest)) + "\n").encode("utf-8")
    with output.open("xb") as handle:
        handle.write(raw)
        handle.flush()
    return output


__all__ = [
    "DEFAULT_SAMPLE_SEED",
    "EXPECTED_BUILDINGS",
    "EXPECTED_OCCURRENCES",
    "MANIFEST_DOMAIN",
    "PriorIdentityLedger",
    "PriorRunSpec",
    "QUOTAS",
    "QUOTA_VERSION",
    "SCORE_DOMAIN",
    "VERSION",
    "build_manifest",
    "default_prior_specs",
    "deterministic_score",
    "load_prior_identity_ledger",
    "select_fixed_sample",
    "write_manifest",
]
