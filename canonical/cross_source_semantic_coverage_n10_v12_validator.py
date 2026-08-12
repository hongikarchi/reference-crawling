"""Independent full-byte replay validator for semantic N10 v1.2.

This file does not import the v1.2 planner.  It supplies a separately pinned
three-run prior ledger and temporarily parameterizes the already independent
v1.1 replay primitives with the v1.2 domain, quotas, and counts.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

import canonical.cross_source_semantic_coverage_n10_v11_validator as _base
from canonical.cross_source_image_selection import canonical_json, canonical_sha256
from canonical.cross_source_semantic_coverage_sources import ArtifactSpec


VERSION = "archibe-cross-source-semantic-coverage-n10-v1.2"
MANIFEST_DOMAIN = "archibe-cross-source-semantic-coverage-n10-v1.2-manifest-v1"
DEFAULT_SAMPLE_SEED = "archibe-semantic-coverage-n10-v1.2"
SCORE_DOMAIN = "semantic-coverage-n10-v1.2-two-lane-score-v1"
QUOTA_VERSION = "population-core8-plus-risk2-v1"
EXPECTED_BUILDINGS = 10
EXPECTED_OCCURRENCES = 50
QUOTAS = (
    ("population_core", "architizer", "ordinary", 5),
    ("population_core", "architizer", "cross_source_candidate", 1),
    ("population_core", "divisare", "ordinary", 2),
    ("risk_sentinel", "architizer", "cover_quality_risk", 1),
    ("risk_sentinel", "divisare", "gallery_fallback", 1),
)

PriorValidationSpec = _base.PriorValidationSpec
ValidationCheck = _base.ValidationCheck
ValidationResult = _base.ValidationResult


def default_prior_specs(repo_root: Path | str) -> tuple[PriorValidationSpec, ...]:
    root = Path(repo_root).resolve()
    return _base.default_prior_specs(root) + (
        PriorValidationSpec(
            "semantic_n10_v1_1",
            root / "data/reports/cross_source_semantic_coverage_n10_v1_1.json",
            root / "data/enrichment/divisare_architizer_semantic_vision_n10_v1_1.db",
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


def _collisions(e3: Any, run_id: str, pixels: set[str], urls: set[str]) -> list[dict[str, Any]]:
    """Independent O(N) collision replay, avoiding repeated unindexed scans."""

    from canonical.cross_source_image_selection import canonical_sha256

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
    result = []
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
        result.append(record)
    return result


def _replay(
    e2_spec: ArtifactSpec,
    e3_spec: ArtifactSpec,
    prior_specs: Iterable[PriorValidationSpec],
    seed: str,
) -> dict[str, Any]:
    """Independently replay every v1.2 manifest field and ordered record."""

    specs = tuple(prior_specs)
    if [value.name for value in specs] != [
        "semantic_n10_v1",
        "semantic_n100_v1",
        "semantic_n10_v1_1",
    ]:
        raise ValueError("validator v1.2 prior run order mismatch")
    run_inputs: list[dict[str, Any]] = []
    building_records: list[dict[str, Any]] = []
    occurrence_records: list[dict[str, Any]] = []
    for spec in specs:
        run_input, buildings, occurrences = _base._prior_replay(spec)
        run_inputs.append(run_input)
        building_records.extend(buildings)
        occurrence_records.extend(occurrences)
    prior_buildings = {str(value["selection_id"]) for value in building_records}
    expected_pixels, derivative_pixels, prior_urls = _base._prior_sets(occurrence_records)
    if (
        len(prior_buildings) != 120
        or len(expected_pixels) != 672
        or len(derivative_pixels) != 665
    ):
        raise ValueError("validator v1.2 combined prior identity count mismatch")

    e2_snapshot, e2 = _base.inspect_artifact(e2_spec)
    e3 = None
    try:
        e3_snapshot, e3 = _base.inspect_artifact(e3_spec)
        inventory, population = _base._collect_inventory_independently(
            e2=e2,
            e3=e3,
            e2_run_id=e2_spec.expected_run_id,
            e3_run_id=e3_spec.expected_run_id,
        )
        collisions = _collisions(
            e3, e3_spec.expected_run_id, expected_pixels | derivative_pixels, prior_urls
        )
        excluded = prior_buildings | {
            str(value["selection_id"]) for value in collisions
        }
        frame = tuple(value for value in inventory if value.selection_id not in excluded)
        available = Counter((value.source, value.population_stratum) for value in frame)
        selected_ids: set[str] = set()
        selected_records: list[dict[str, Any]] = []
        selected_pixels: list[str] = []
        selected_urls: list[str] = []
        occurrence_hashes: list[dict[str, Any]] = []
        candidate_hashes: list[dict[str, Any]] = []

        def score(lane: str, source: str, stratum: str, selection_id: str) -> str:
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

        for lane, source, stratum, quota in QUOTAS:
            scored = sorted(
                (score(lane, source, stratum, value.selection_id), value)
                for value in frame
                if value.source == source
                and value.population_stratum == stratum
                and value.selection_id not in selected_ids
            )
            if len(scored) < quota:
                raise ValueError("validator v1.2 quota lacks candidates")
            for rank_in_quota, (score_sha, building) in enumerate(scored[:quota], 1):
                selected_ids.add(building.selection_id)
                coverage = _base._replay_building_coverage(
                    _base._load_candidates_independently(
                        e2=e2,
                        e3=e3,
                        e2_run_id=e2_spec.expected_run_id,
                        e3_run_id=e3_spec.expected_run_id,
                        building=building,
                    )
                )
                for wrapped in coverage["selected_occurrences"]:
                    candidate = wrapped["occurrence"]["candidate"]
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
                    "building": _base._inventory_record(building),
                    "building_record_sha256": _base._inventory_sha256(building),
                    "coverage_plan": coverage,
                    "coverage_record_sha256": canonical_sha256(coverage),
                    "lane": lane,
                    "model_result_selection_used": False,
                    "quota": quota,
                    "rank": len(selected_records) + 1,
                    "rank_in_quota": rank_in_quota,
                    "score_sha256": score_sha,
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
            raise ValueError("validator v1.2 production selection count mismatch")
        overlaps = {
            "building": len(selected_ids & prior_buildings),
            "expected_e1_pixel": len(set(selected_pixels) & expected_pixels),
            "prior_successful_derivative_pixel": len(set(selected_pixels) & derivative_pixels),
            "fetch_url": len(set(selected_urls) & prior_urls),
        }
        if (
            any(overlaps.values())
            or len(set(selected_pixels)) != EXPECTED_OCCURRENCES
            or len(set(selected_urls)) != EXPECTED_OCCURRENCES
        ):
            raise ValueError("validator v1.2 selection overlap/duplicate")
        records = {
            "building_records": building_records,
            "candidate_collision_records": collisions,
            "occurrence_records": occurrence_records,
        }
        exclusions = {
            "actual_derivative_runtime_rule": (
                "materialize all fixed inputs before the first model request and reject any actual "
                "derivative pixel matching a prior successful derivative; seven prior source_changed "
                "rows have expected-E1 identity only and no accepted derivative"
            ),
            "candidate_collision_buildings": len(collisions),
            "excluded_buildings": len(excluded),
            "expected_e1_pixel_count": len(expected_pixels),
            "failed_expected_only_occurrences": sum(
                value["derivative_pixel_sha256"] is None for value in occurrence_records
            ),
            "identity_ledger_sha256": canonical_sha256(
                {"domain": VERSION + "-prior-identity-ledger", **records}
            ),
            "prior_building_count": len(prior_buildings),
            "prior_run_inputs": run_inputs,
            "prior_successful_derivative_pixel_count": len(derivative_pixels),
            "prior_unique_url_count": len(prior_urls),
            "records": records,
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
                {"ordered_e1_pixels": [{"rank": index, "sha256": value} for index, value in enumerate(selected_pixels, 1)], "version": VERSION}
            ),
            "ordered_fetch_url_manifest_sha256": canonical_sha256(
                {"ordered_fetch_urls": [{"rank": index, "url": value} for index, value in enumerate(selected_urls, 1)], "version": VERSION}
            ),
            "ordered_occurrence_manifest_sha256": canonical_sha256(
                {"ordered_occurrences": occurrence_hashes, "version": VERSION}
            ),
            "planned_e1_exact_duplicate_savings": 0,
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
                "excluded_buildings": len(excluded),
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
    body["input_sha_after"] = {
        "e2_evidence": e2_snapshot.byte_sha256,
        "e3_selection": e3_snapshot.byte_sha256,
        **{spec.name + "_manifest": spec.manifest_byte_sha256 for spec in specs},
        **{spec.name + "_sidecar": spec.sidecar_byte_sha256 for spec in specs},
    }
    body["semantic_coverage_manifest_sha256"] = canonical_sha256(
        {"domain": MANIFEST_DOMAIN, "manifest": body}
    )
    return body


def validate_manifest(
    manifest_path: Path | str,
    *,
    e2_spec: ArtifactSpec,
    e3_spec: ArtifactSpec,
    prior_specs: Iterable[PriorValidationSpec],
    expected_seed: str = DEFAULT_SAMPLE_SEED,
) -> ValidationResult:
    path = Path(manifest_path).resolve()
    raw = path.read_bytes()
    byte_sha = hashlib.sha256(raw).hexdigest()
    checks: list[ValidationCheck] = []
    self_sha = None
    buildings = 0
    occurrences = 0
    try:
        payload = json.loads(raw.decode("utf-8"))
        canonical_raw = (canonical_json(payload) + "\n").encode("utf-8")
        checks.append(ValidationCheck("canonical_json_bytes", raw == canonical_raw, hashlib.sha256(canonical_raw).hexdigest(), byte_sha))
        if not isinstance(payload, dict):
            raise ValueError("manifest root must be object")
        self_sha = payload.get("semantic_coverage_manifest_sha256")
        buildings = len(payload.get("selected_buildings", []))
        occurrences = int(payload.get("planned_occurrence_count", 0))
        expected = _replay(e2_spec, e3_spec, tuple(prior_specs), expected_seed)
        expected_raw = (canonical_json(expected) + "\n").encode("utf-8")
        checks.append(ValidationCheck("entire_canonical_manifest_byte_equality", raw == expected_raw, hashlib.sha256(expected_raw).hexdigest(), byte_sha))
        checks.extend(
            (
                ValidationCheck("sample_seed", payload.get("sample_seed") == expected_seed, expected_seed, str(payload.get("sample_seed"))),
                ValidationCheck("building_count", buildings == EXPECTED_BUILDINGS, str(EXPECTED_BUILDINGS), str(buildings)),
                ValidationCheck("occurrence_count", occurrences == EXPECTED_OCCURRENCES, str(EXPECTED_OCCURRENCES), str(occurrences)),
                ValidationCheck("zero_requests", all(payload.get(key) == 0 for key in ("network_requests", "vision_requests", "llm_requests")), "network=vision=llm=0", canonical_json({key: payload.get(key) for key in ("network_requests", "vision_requests", "llm_requests")})),
                ValidationCheck("selection_not_result_conditioned", payload.get("config", {}).get("model_result_selection_used") is False, "false", str(payload.get("config", {}).get("model_result_selection_used"))),
            )
        )
    except Exception as exc:
        checks.append(ValidationCheck("independent_replay", False, "complete", f"{type(exc).__name__}: {exc}"))
    return ValidationResult(path, bool(checks) and all(value.passed for value in checks), tuple(checks), len(raw), byte_sha, None if self_sha is None else str(self_sha), buildings, occurrences)


__all__ = [
    "PriorValidationSpec",
    "ValidationCheck",
    "ValidationResult",
    "default_prior_specs",
    "validate_manifest",
]
