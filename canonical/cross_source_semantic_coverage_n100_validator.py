"""Independent immutable replay validator for the semantic-coverage N100.

The validator does not call the N100 planner's quota or selection functions.
It re-derives the E2/E3 population, prior-N10 exclusions, hierarchical
Hamilton quotas, deterministic scores, and coverage plans, then compares the
entire canonical JSON byte stream.  It has no network or model capability.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from canonical.cross_source_image_selection import canonical_json, canonical_sha256
from canonical.cross_source_semantic_coverage import MAX_OCCURRENCES_PER_BUILDING, P2_POLICY_ID
from canonical.cross_source_semantic_coverage_n100 import (
    DEFAULT_N100_SAMPLE_SEED,
    DEFAULT_N100_SAMPLE_SIZE,
    EXPECTED_PRODUCTION_OCCURRENCES,
    EXPECTED_PRODUCTION_UNIQUE_E1_PIXELS,
    N100_QUOTA_VERSION,
    N100_SCORE_DOMAIN,
    N10_MANIFEST_DOMAIN,
    SEMANTIC_COVERAGE_N100_MANIFEST_DOMAIN,
    SEMANTIC_COVERAGE_N100_VERSION,
    PriorN10Spec,
)
from canonical.cross_source_semantic_coverage_sources import (
    ArtifactSnapshot,
    ArtifactSpec,
    inspect_artifact,
    sha256_file,
    sqlite_sidecars,
)
from canonical.cross_source_semantic_coverage_validator import (
    _collect_inventory_independently,
    _inventory_record,
    _inventory_sha256,
    _load_candidates_independently,
    _replay_building_coverage,
)


@dataclass(frozen=True)
class N100ValidationCheck:
    name: str
    passed: bool
    expected: str
    actual: str


@dataclass(frozen=True)
class N100ValidationResult:
    manifest_path: Path
    passed: bool
    checks: tuple[N100ValidationCheck, ...]
    manifest_size_bytes: int
    manifest_byte_sha256: str
    manifest_self_sha256: str | None
    selected_building_count: int
    selected_occurrence_count: int

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
            "failed_check_names": list(self.failed_check_names),
            "llm_requests": 0,
            "manifest_byte_sha256": self.manifest_byte_sha256,
            "manifest_path": str(self.manifest_path),
            "manifest_self_sha256": self.manifest_self_sha256,
            "manifest_size_bytes": self.manifest_size_bytes,
            "network_requests": 0,
            "passed": self.passed,
            "selected_building_count": self.selected_building_count,
            "selected_occurrence_count": self.selected_occurrence_count,
            "vision_requests": 0,
        }


@dataclass(frozen=True)
class _PriorReplay:
    manifest_input: dict[str, Any]
    sidecar_input: dict[str, Any]
    building_records: tuple[dict[str, Any], ...]
    occurrence_records: tuple[dict[str, Any], ...]

    @property
    def building_ids(self) -> frozenset[str]:
        return frozenset(str(value["selection_id"]) for value in self.building_records)

    @property
    def e1_pixels(self) -> frozenset[str]:
        return frozenset(
            str(value[key])
            for value in self.occurrence_records
            for key in ("expected_e1_pixel_sha256", "actual_e1_pixel_sha256")
        )

    @property
    def derivative_pixels(self) -> frozenset[str]:
        return frozenset(str(value["derivative_pixel_sha256"]) for value in self.occurrence_records)

    @property
    def urls(self) -> frozenset[str]:
        return frozenset(
            value
            for record in self.occurrence_records
            for key in ("canonical_url", "fetch_url", "planned_final_url", "actual_final_url")
            for value in (record.get(key),)
            if isinstance(value, str) and value
        )


def _open_immutable(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _replay_prior_n10(spec: PriorN10Spec) -> _PriorReplay:
    manifest_path = spec.manifest_path.resolve()
    sidecar_path = spec.sidecar_path.resolve()
    if sqlite_sidecars(sidecar_path):
        raise ValueError("prior N10 sidecar has SQLite sidecars")
    raw = manifest_path.read_bytes()
    if len(raw) != spec.expected_manifest_size:
        raise ValueError("prior N10 manifest size mismatch")
    byte_sha = hashlib.sha256(raw).hexdigest()
    if byte_sha != spec.expected_manifest_byte_sha256:
        raise ValueError("prior N10 manifest byte SHA mismatch")
    payload = json.loads(raw.decode("utf-8"))
    if raw != (canonical_json(payload) + "\n").encode("utf-8"):
        raise ValueError("prior N10 manifest canonical bytes mismatch")
    body = dict(payload)
    stored_self = body.pop("semantic_coverage_manifest_sha256", None)
    replayed_self = canonical_sha256({"domain": N10_MANIFEST_DOMAIN, "manifest": body})
    if stored_self != spec.expected_manifest_self_sha256 or replayed_self != stored_self:
        raise ValueError("prior N10 manifest self SHA mismatch")
    if sidecar_path.stat().st_size != spec.expected_sidecar_size:
        raise ValueError("prior N10 sidecar size mismatch")
    sidecar_sha = sha256_file(sidecar_path)
    if sidecar_sha != spec.expected_sidecar_byte_sha256:
        raise ValueError("prior N10 sidecar byte SHA mismatch")
    connection = _open_immutable(sidecar_path)
    try:
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = list(connection.execute("PRAGMA foreign_key_check"))
        runs = connection.execute("SELECT * FROM semantic_runs").fetchall()
        if len(runs) != 1:
            raise ValueError("prior N10 sidecar run cardinality mismatch")
        run = runs[0]
        expected_run = {
            "application_id": (application_id, spec.expected_application_id),
            "user_version": (user_version, spec.expected_user_version),
            "run_id": (str(run["run_id"]), spec.expected_run_id),
            "status": (str(run["status"]), "complete"),
            "logical_sha256": (
                str(run["logical_sha256"]),
                spec.expected_sidecar_logical_sha256,
            ),
            "manifest_size": (int(run["manifest_size"]), len(raw)),
            "manifest_byte_sha256": (
                str(run["manifest_byte_sha256"]),
                spec.expected_manifest_byte_sha256,
            ),
            "manifest_self_sha256": (
                str(run["manifest_self_sha256"]),
                spec.expected_manifest_self_sha256,
            ),
        }
        mismatches = {key: pair for key, pair in expected_run.items() if pair[0] != pair[1]}
        if mismatches or quick != "ok" or integrity != "ok" or foreign_keys:
            raise ValueError(f"prior N10 sidecar validation mismatch: {mismatches!r}")
        building_records: list[dict[str, Any]] = []
        for row in connection.execute("SELECT * FROM selected_buildings ORDER BY building_rank"):
            record = {
                "building_rank": int(row["building_rank"]),
                "reason": "prior_n10_building",
                "selection_id": str(row["selection_id"]),
                "source": str(row["source"]),
                "source_building_id": str(row["source_building_id"]),
            }
            record["record_sha256"] = canonical_sha256(record)
            building_records.append(record)
        occurrence_records: list[dict[str, Any]] = []
        rows = connection.execute(
            """
            SELECT o.*,i.actual_e1_pixel_sha256,i.derivative_pixel_sha256,
                   f.final_url AS actual_final_url
            FROM selected_occurrences o
            JOIN vision_inputs i USING(run_id,inference_id)
            JOIN fetch_attempts f
              ON f.run_id=i.run_id AND f.inference_id=i.inference_id
             AND f.attempt_no=i.selected_fetch_attempt_no
            ORDER BY o.input_rank
            """
        )
        for row in rows:
            candidate = json.loads(str(row["manifest_json"]))["occurrence"]["candidate"]
            record = {
                "actual_e1_pixel_sha256": str(row["actual_e1_pixel_sha256"]),
                "actual_final_url": None if row["actual_final_url"] is None else str(row["actual_final_url"]),
                "candidate_id": str(row["candidate_id"]),
                "canonical_url": str(candidate["canonical_url"]),
                "derivative_pixel_sha256": str(row["derivative_pixel_sha256"]),
                "expected_e1_pixel_sha256": str(row["expected_e1_pixel_sha256"]),
                "fetch_url": str(row["fetch_url"]),
                "inference_id": str(row["inference_id"]),
                "input_rank": int(row["input_rank"]),
                "planned_final_url": candidate.get("final_url"),
                "reason": "prior_n10_image_identity",
                "selection_id": str(row["selection_id"]),
                "source": str(row["source"]),
                "source_asset_id": str(row["source_asset_id"]),
            }
            record["record_sha256"] = canonical_sha256(record)
            occurrence_records.append(record)
        if len(building_records) != 10 or len(occurrence_records) != 57:
            raise ValueError("prior N10 exclusion count mismatch")
        manifest_input = {
            "byte_sha256": byte_sha,
            "manifest_self_sha256": str(stored_self),
            "ordered_building_manifest_sha256": payload["ordered_building_manifest_sha256"],
            "ordered_occurrence_manifest_sha256": payload["ordered_occurrence_manifest_sha256"],
            "path": str(manifest_path),
            "sample_seed": payload["sample_seed"],
            "size_bytes": len(raw),
        }
        sidecar_input = {
            "application_id": application_id,
            "byte_sha256": sidecar_sha,
            "logical_sha256": str(run["logical_sha256"]),
            "path": str(sidecar_path),
            "run_id": str(run["run_id"]),
            "size_bytes": sidecar_path.stat().st_size,
            "sqlite_foreign_key_violations": len(foreign_keys),
            "sqlite_integrity_check": integrity,
            "sqlite_quick_check": quick,
            "status": str(run["status"]),
            "user_version": user_version,
        }
    finally:
        connection.close()
    return _PriorReplay(
        manifest_input,
        sidecar_input,
        tuple(building_records),
        tuple(occurrence_records),
    )


def _hamilton_replay(counts: Mapping[str, int], seats: int) -> tuple[dict[str, int], list[dict[str, Any]]]:
    ordered = tuple(sorted(counts))
    total = sum(counts.values())
    if total <= 0 or seats < 0 or seats > total:
        raise ValueError("invalid validator Hamilton inputs")
    floors = {key: counts[key] * seats // total for key in ordered}
    remainders = {key: counts[key] * seats % total for key in ordered}
    remaining = seats - sum(floors.values())
    winners = set(sorted(ordered, key=lambda key: (-remainders[key], key))[:remaining])
    quotas = {key: floors[key] + int(key in winners) for key in ordered}
    records = [
        {
            "allocated_quota": quotas[key],
            "available_count": counts[key],
            "floor_quota": floors[key],
            "ideal_denominator": total,
            "ideal_numerator": counts[key] * seats,
            "key": key,
            "largest_remainder_awarded": key in winners,
            "remainder_numerator": remainders[key],
        }
        for key in ordered
    ]
    return quotas, records


def _quota_replay(frame: Sequence[Any]) -> tuple[dict[tuple[str, str], int], dict[str, Any]]:
    source_counts = Counter(value.source for value in frame)
    source_quotas, source_records = _hamilton_replay(source_counts, DEFAULT_N100_SAMPLE_SIZE)
    quotas: dict[tuple[str, str], int] = {}
    strata: list[dict[str, Any]] = []
    for source in sorted(source_counts):
        counts = Counter(value.population_stratum for value in frame if value.source == source)
        local, records = _hamilton_replay(counts, source_quotas[source])
        for record in records:
            stratum = str(record.pop("key"))
            quotas[(source, stratum)] = local[stratum]
            strata.append({"source": source, "stratum": stratum, **record})
    return quotas, {
        "quota_version": N100_QUOTA_VERSION,
        "sample_size": DEFAULT_N100_SAMPLE_SIZE,
        "source_level": [
            {"source": str(record.pop("key")), **record} for record in source_records
        ],
        "stratum_level": strata,
    }


def _score_replay(seed: str, source: str, stratum: str, selection_id: str) -> str:
    return canonical_sha256(
        {
            "domain": N100_SCORE_DOMAIN,
            "population_stratum": stratum,
            "seed": seed,
            "selection_id": selection_id,
            "source": source,
        }
    )


def _collision_replay(
    e3: sqlite3.Connection, run_id: str, prior: _PriorReplay
) -> tuple[dict[str, Any], ...]:
    pixels = tuple(sorted(prior.e1_pixels))
    urls = tuple(sorted(prior.urls))
    pm = ",".join("?" for _ in pixels)
    um = ",".join("?" for _ in urls)
    sql = f"""
      SELECT selection_id,source,
             sum(normalized_pixel_sha256 IN ({pm})),
             sum(canonical_url IN ({um}) OR fetch_url IN ({um}) OR final_url IN ({um}))
      FROM image_candidates
      WHERE run_id=? AND (normalized_pixel_sha256 IN ({pm})
        OR canonical_url IN ({um}) OR fetch_url IN ({um}) OR final_url IN ({um}))
      GROUP BY selection_id,source ORDER BY source,selection_id
    """
    params: list[Any] = [
        *pixels, *urls, *urls, *urls, run_id, *pixels, *urls, *urls, *urls
    ]
    records: list[dict[str, Any]] = []
    for row in e3.execute(sql, params):
        record = {
            "pixel_match_count": int(row[2]),
            "reason": "prior_n10_e1_or_url_identity",
            "selection_id": str(row[0]),
            "source": str(row[1]),
            "url_match_count": int(row[3]),
        }
        record["record_sha256"] = canonical_sha256(record)
        records.append(record)
    return tuple(records)


def _replay_manifest(
    e2_spec: ArtifactSpec,
    e3_spec: ArtifactSpec,
    n10_spec: PriorN10Spec,
    *,
    seed: str,
) -> dict[str, Any]:
    prior = _replay_prior_n10(n10_spec)
    e2_snapshot, e2 = inspect_artifact(e2_spec)
    e3_snapshot: ArtifactSnapshot | None = None
    e3: sqlite3.Connection | None = None
    try:
        e3_snapshot, e3 = inspect_artifact(e3_spec)
        inventory, population = _collect_inventory_independently(
            e2=e2,
            e3=e3,
            e2_run_id=e2_snapshot.run_id,
            e3_run_id=e3_snapshot.run_id,
        )
        collisions = _collision_replay(e3, e3_snapshot.run_id, prior)
        excluded_ids = prior.building_ids | {
            str(value["selection_id"]) for value in collisions
        }
        frame = tuple(value for value in inventory if value.selection_id not in excluded_ids)
        quotas, quota_plan = _quota_replay(frame)
        eligible_counts = Counter((value.source, value.population_stratum) for value in inventory)
        available_counts = Counter((value.source, value.population_stratum) for value in frame)
        selected: list[tuple[Any, int, int, str, int]] = []
        for source, stratum in sorted(quotas):
            quota = quotas[(source, stratum)]
            candidates = sorted(
                (
                    _score_replay(seed, source, stratum, value.selection_id),
                    value,
                )
                for value in frame
                if value.source == source and value.population_stratum == stratum
            )
            for local_rank, (score, building) in enumerate(candidates[:quota], 1):
                selected.append((building, local_rank, quota, score, available_counts[(source, stratum)]))
        selected_records: list[dict[str, Any]] = []
        occurrence_hashes: list[dict[str, Any]] = []
        url_hashes: list[dict[str, Any]] = []
        pixel_hashes: list[dict[str, Any]] = []
        selected_pixels: list[str] = []
        selected_urls: list[str] = []
        input_rank = 0
        for rank, (building, local_rank, quota, score, available_count) in enumerate(selected, 1):
            candidates = _load_candidates_independently(
                e2=e2,
                e3=e3,
                e2_run_id=e2_snapshot.run_id,
                e3_run_id=e3_snapshot.run_id,
                building=building,
            )
            plan = _replay_building_coverage(candidates)
            plan_sha = canonical_sha256(plan)
            source_quota = sum(
                value for (quota_source, _), value in quotas.items() if quota_source == building.source
            )
            selection_record = {
                "available_count_before_pick": available_count - local_rank + 1,
                "building": _inventory_record(building),
                "building_record_sha256": _inventory_sha256(building),
                "eligible_count": eligible_counts[(building.source, building.population_stratum)],
                "guard_name": f"n100_hamilton_{building.source}_{building.population_stratum}",
                "guard_source": building.source,
                "quota_version": N100_QUOTA_VERSION,
                "rank": rank,
                "rank_in_stratum": local_rank,
                "score_sha256": score,
                "source_quota": source_quota,
                "stratum_key": f"{building.source}:{building.population_stratum}",
                "stratum_quota": quota,
                "coverage_plan": plan,
                "coverage_plan_record_sha256": plan_sha,
            }
            selection_sha = canonical_sha256(selection_record)
            selected_records.append(
                {"selected_building": selection_record, "selected_building_record_sha256": selection_sha}
            )
            for wrapper in plan["selected_occurrences"]:
                input_rank += 1
                occurrence = wrapper["occurrence"]
                candidate = occurrence["candidate"]
                pixel = str(candidate["normalized_pixel_sha256"])
                url = str(candidate["fetch_url"])
                selected_pixels.append(pixel)
                selected_urls.append(url)
                occurrence_hashes.append(
                    {
                        "candidate_id": candidate["candidate_id"],
                        "occurrence_record_sha256": wrapper["occurrence_record_sha256"],
                        "selection_id": building.selection_id,
                    }
                )
                url_hashes.append({"fetch_url": url, "input_rank": input_rank, "source": candidate["source"]})
                pixel_hashes.append(
                    {"input_rank": input_rank, "normalized_pixel_sha256": pixel, "source": candidate["source"]}
                )
        if len(selected) != DEFAULT_N100_SAMPLE_SIZE:
            raise ValueError("validator N100 building count mismatch")
        if len(selected_pixels) != EXPECTED_PRODUCTION_OCCURRENCES:
            raise ValueError("validator N100 occurrence count mismatch")
        if len(set(selected_pixels)) != EXPECTED_PRODUCTION_UNIQUE_E1_PIXELS:
            raise ValueError("validator N100 E1 identity count mismatch")
        overlap = {
            "building_count": len({value[0].selection_id for value in selected} & prior.building_ids),
            "e1_pixel_count": len(set(selected_pixels) & prior.e1_pixels),
            "fetch_url_count": len(set(selected_urls) & prior.urls),
        }
        if any(overlap.values()):
            raise ValueError("validator replay overlaps prior N10")
        original_counts = Counter((value.source, value.population_stratum) for value in inventory)
        frame_counts = Counter((value.source, value.population_stratum) for value in frame)
        selected_counts = Counter((value[0].source, value[0].population_stratum) for value in selected)
        sampling_frame = {
            "available_buildings": len(frame),
            "available_by_source_stratum": [
                {
                    "available_count": frame_counts[key],
                    "eligible_count": original_counts[key],
                    "excluded_count": original_counts[key] - frame_counts[key],
                    "selected_count": selected_counts[key],
                    "source": key[0],
                    "stratum": key[1],
                }
                for key in sorted(original_counts)
            ],
            "eligible_buildings": len(inventory),
            "excluded_buildings": len(inventory) - len(frame),
        }
        exclusion_ledger = {
            "building_records": list(prior.building_records),
            "candidate_collision_records": list(collisions),
            "derivative_identity_gate": (
                "runtime_must_materialize_all_inputs_and prove zero overlap with "
                "the prior derivative_pixel_sha256 set before the first Vision request"
            ),
            "exclusion_record_sha256": canonical_sha256(
                {
                    "building_records": list(prior.building_records),
                    "candidate_collision_records": list(collisions),
                    "occurrence_records": list(prior.occurrence_records),
                    "version": SEMANTIC_COVERAGE_N100_VERSION,
                }
            ),
            "n10_actual_derivative_pixel_count": len(prior.derivative_pixels),
            "n10_building_count": len(prior.building_ids),
            "n10_e1_pixel_count": len(prior.e1_pixels),
            "n10_occurrence_records": list(prior.occurrence_records),
            "n10_unique_url_count": len(prior.urls),
            "selected_overlap": overlap,
        }
        config = {
            "anchor_policy_id": P2_POLICY_ID,
            "anchor_ranks": [1, 2, 3],
            "creates_final_representative": False,
            "creates_vision_tasks": False,
            "gallery_probe_slots": ["gallery_early", "gallery_middle", "gallery_late"],
            "hard_risk_fallback_only_when_all_candidates_risky": True,
            "max_occurrences_per_building": MAX_OCCURRENCES_PER_BUILDING,
            "n10_exclusion_fields": [
                "selection_id",
                "normalized_pixel_sha256",
                "canonical_url",
                "fetch_url",
                "final_url",
            ],
            "phash_semantic_reuse_allowed": False,
            "phash_transitive_closure_allowed": False,
            "quota_version": N100_QUOTA_VERSION,
            "sample_size": DEFAULT_N100_SAMPLE_SIZE,
            "score_domain": N100_SCORE_DOMAIN,
            "selection_version": SEMANTIC_COVERAGE_N100_VERSION,
        }
        body: dict[str, Any] = {
            "authoritative": False,
            "config": config,
            "config_sha256": canonical_sha256(config),
            "creates_final_representative": False,
            "creates_vision_tasks": False,
            "e2_input": e2_snapshot.as_record(),
            "e3_input": e3_snapshot.as_record(),
            "exact_reuse_is_provisional_until_vision_input_identity": True,
            "exclusions": exclusion_ledger,
            "llm_requests": 0,
            "n10_manifest_input": prior.manifest_input,
            "n10_semantic_sidecar_input": prior.sidecar_input,
            "network_requests": 0,
            "ordered_building_manifest_sha256": canonical_sha256(
                {
                    "ordered_buildings": [
                        {"rank": index, "record_sha256": value["selected_building_record_sha256"]}
                        for index, value in enumerate(selected_records, 1)
                    ],
                    "version": SEMANTIC_COVERAGE_N100_VERSION,
                }
            ),
            "ordered_e1_pixel_manifest_sha256": canonical_sha256(
                {"ordered_e1_pixels": pixel_hashes, "version": SEMANTIC_COVERAGE_N100_VERSION}
            ),
            "ordered_fetch_url_manifest_sha256": canonical_sha256(
                {"ordered_fetch_urls": url_hashes, "version": SEMANTIC_COVERAGE_N100_VERSION}
            ),
            "ordered_occurrence_manifest_sha256": canonical_sha256(
                {"ordered_occurrences": occurrence_hashes, "version": SEMANTIC_COVERAGE_N100_VERSION}
            ),
            "planned_e1_exact_duplicate_savings": len(selected_pixels) - len(set(selected_pixels)),
            "planned_occurrence_count": len(selected_pixels),
            "planned_unique_e1_pixel_count": len(set(selected_pixels)),
            "population": population,
            "quota_plan": quota_plan,
            "sample_seed": seed,
            "sample_size_buildings": len(selected),
            "sampling_frame": sampling_frame,
            "selected_buildings": selected_records,
            "selection_mode": "offline_semantic_coverage_n100_population_sample",
            "version": SEMANTIC_COVERAGE_N100_VERSION,
            "vision_requests": 0,
        }
    finally:
        if e3 is not None:
            e3.close()
        e2.close()
    if e3_snapshot is None:
        raise AssertionError("validator E3 snapshot missing")
    for spec, snapshot in ((e2_spec, e2_snapshot), (e3_spec, e3_snapshot)):
        if sqlite_sidecars(spec.path) or spec.path.stat().st_size != snapshot.size_bytes:
            raise ValueError(f"validator input changed: {spec.name}")
        if sha256_file(spec.path) != snapshot.byte_sha256:
            raise ValueError(f"validator input SHA changed: {spec.name}")
    if sha256_file(n10_spec.manifest_path) != prior.manifest_input["byte_sha256"]:
        raise ValueError("validator prior N10 manifest changed")
    if sha256_file(n10_spec.sidecar_path) != prior.sidecar_input["byte_sha256"]:
        raise ValueError("validator prior N10 sidecar changed")
    body["input_sha_after"] = {
        "e2_evidence": e2_snapshot.byte_sha256,
        "e3_selection": e3_snapshot.byte_sha256,
        "n10_manifest": prior.manifest_input["byte_sha256"],
        "n10_semantic_sidecar": prior.sidecar_input["byte_sha256"],
    }
    body["semantic_coverage_manifest_sha256"] = canonical_sha256(
        {"domain": SEMANTIC_COVERAGE_N100_MANIFEST_DOMAIN, "manifest": body}
    )
    return body


def validate_semantic_coverage_n100_manifest(
    manifest_path: Path | str,
    *,
    e2_spec: ArtifactSpec,
    e3_spec: ArtifactSpec,
    n10_spec: PriorN10Spec,
    expected_seed: str = DEFAULT_N100_SAMPLE_SEED,
) -> N100ValidationResult:
    path = Path(manifest_path).resolve()
    raw = path.read_bytes()
    byte_sha = hashlib.sha256(raw).hexdigest()
    parsed: Any = None
    checks: list[N100ValidationCheck] = []
    self_sha: str | None = None
    building_count = 0
    occurrence_count = 0
    try:
        parsed = json.loads(raw.decode("utf-8"))
        canonical_raw = (canonical_json(parsed) + "\n").encode("utf-8")
        checks.append(N100ValidationCheck("canonical_json_bytes", raw == canonical_raw, hashlib.sha256(canonical_raw).hexdigest(), byte_sha))
        if not isinstance(parsed, dict):
            raise ValueError("manifest root must be an object")
        self_sha = parsed.get("semantic_coverage_manifest_sha256")
        building_count = len(parsed.get("selected_buildings", []))
        occurrence_count = int(parsed.get("planned_occurrence_count", 0))
        expected = _replay_manifest(
            e2_spec,
            e3_spec,
            n10_spec,
            seed=expected_seed,
        )
        expected_raw = (canonical_json(expected) + "\n").encode("utf-8")
        checks.append(
            N100ValidationCheck(
                "entire_canonical_manifest_byte_equality",
                raw == expected_raw,
                hashlib.sha256(expected_raw).hexdigest(),
                byte_sha,
            )
        )
        checks.extend(
            (
                N100ValidationCheck("sample_seed", parsed.get("sample_seed") == expected_seed, expected_seed, str(parsed.get("sample_seed"))),
                N100ValidationCheck("building_count", building_count == DEFAULT_N100_SAMPLE_SIZE, str(DEFAULT_N100_SAMPLE_SIZE), str(building_count)),
                N100ValidationCheck("occurrence_count", occurrence_count == EXPECTED_PRODUCTION_OCCURRENCES, str(EXPECTED_PRODUCTION_OCCURRENCES), str(occurrence_count)),
                N100ValidationCheck(
                    "unique_e1_count",
                    parsed.get("planned_unique_e1_pixel_count") == EXPECTED_PRODUCTION_UNIQUE_E1_PIXELS,
                    str(EXPECTED_PRODUCTION_UNIQUE_E1_PIXELS),
                    str(parsed.get("planned_unique_e1_pixel_count")),
                ),
                N100ValidationCheck(
                    "zero_requests",
                    all(parsed.get(key) == 0 for key in ("network_requests", "vision_requests", "llm_requests")),
                    "network=vision=llm=0",
                    canonical_json({key: parsed.get(key) for key in ("network_requests", "vision_requests", "llm_requests")}),
                ),
            )
        )
    except Exception as exc:
        checks.append(N100ValidationCheck("independent_replay", False, "complete", f"{type(exc).__name__}: {exc}"))
    passed = bool(checks) and all(value.passed for value in checks)
    return N100ValidationResult(
        path,
        passed,
        tuple(checks),
        len(raw),
        byte_sha,
        None if self_sha is None else str(self_sha),
        building_count,
        occurrence_count,
    )


__all__ = [
    "N100ValidationCheck",
    "N100ValidationResult",
    "validate_semantic_coverage_n100_manifest",
]
